"""
Main route -- serves the single-page application.
"""

import json
import logging
import os
import subprocess
import sys

from flask import Blueprint, Response, jsonify, render_template, request, send_from_directory
from ..platform_utils import NO_WINDOW as _NO_WINDOW
from .. import approval_watch
from .. import mobile_command
from .. import socketio
# Same rule as the message hook: only a process on this machine. A phone on the
# tailnet and a page in a browser both arrive from 127.0.0.1 too.
from .live_api import _hook_caller_is_local as _caller_is_local

bp = Blueprint('main', __name__)
log = logging.getLogger(__name__)


def _web_port() -> int:
    """Web port this process is bound to.  Respects VIBENODE_TEST_PORT
    (legacy test-skip mode) and VIBENODE_WEB_PORT (production-mode port
    override for side-by-side instances).  Without this, ``/api/restart``
    always killed 5050 even when the web was bound elsewhere — the silent
    footgun behind 'restart doesn't work on non-standard installs'."""
    return (
        int(os.environ.get("VIBENODE_TEST_PORT", "0"))
        or int(os.environ.get("VIBENODE_WEB_PORT", "0"))
        or 5050
    )


def _daemon_port() -> int:
    """Daemon port this process is paired with.  Same env-aware policy
    as _web_port()."""
    return int(os.environ.get("VIBENODE_DAEMON_PORT", "0")) or 5051


@bp.route("/")
def index():
    # a2hs_title is the label the phone's Home-Screen icon shows for THIS computer
    # (defaults to the hostname), so multiple machines are distinguishable. Cheap —
    # device_name() reads cached config + hostname, no Tailscale call. See mobile_command.
    return render_template('index.html', a2hs_title=mobile_command.device_name())


@bp.route("/manifest.webmanifest")
def web_manifest():
    """Per-machine PWA manifest so the installed app name matches THIS computer.

    Served dynamically (not the static file) so the name follows the user's device
    label. iOS keys the Add-to-Home-Screen name off apple-mobile-web-app-title (set
    in index.html); this covers the manifest short_name for Android/Chrome installs.
    """
    name = mobile_command.device_name()
    manifest = {
        "name": name,
        "short_name": name[:12] or "VibeNode",
        "description": "Run and drive your Claude sessions from your phone.",
        "start_url": "/",
        "scope": "/",
        "display": "standalone",
        "orientation": "portrait",
        "background_color": "#0b0d10",
        "theme_color": "#0b0d10",
        "icons": [{
            "src": "/static/vibenode.png",
            "sizes": "256x256",
            "type": "image/png",
            "purpose": "any maskable",
        }],
    }
    return Response(json.dumps(manifest), mimetype="application/manifest+json")


@bp.route("/api/ping")
def ping():
    """Tiny liveness probe for the UI's server-reachable health check.

    Returns {"ok": true} with no side effects. Cheaper than /api/auth-status
    (which shells out to the Claude CLI) and decoupled from auth semantics,
    so a slow auth check never produces a false 'server unreachable' overlay.
    """
    return jsonify(ok=True)


@bp.route("/api/usage-limits")
def usage_limits():
    """Account usage windows (session / weekly / Fable) for the input-bar pill.

    Reads the daemon's persisted snapshot (daemon/usage_limits.py) instead of
    making an IPC round-trip, so it is cheap on page load and still answers
    while the daemon restarts.  Live changes arrive as the ``usage_limits``
    Socket.IO event.  Values are as of the last Claude turn on this machine.
    """
    from daemon import usage_limits as _ul
    state = _ul.load()
    # No Fable reading (it is only reported on a Fable reply)?  Fetch one in
    # the background; it arrives as a ``usage_limits`` push.  See usage_probe.
    try:
        from flask import current_app
        from .. import usage_probe
        usage_probe.maybe_start(getattr(current_app, "session_manager", None), state)
    except Exception:
        pass
    return jsonify(ok=True, **_ul.public_view(state))


@bp.route("/api/usage-limits/refresh", methods=["POST"])
def usage_limits_refresh():
    """User asked for a fresh Fable reading (click on the gauges).

    Starts the hidden one-word Fable turn described in app/usage_probe.py
    unless one ran in the last minute.  The result arrives as a
    ``usage_limits`` push, not in this response.
    """
    from daemon import usage_limits as _ul
    from flask import current_app
    from .. import usage_probe
    started = usage_probe.maybe_start(
        getattr(current_app, "session_manager", None), _ul.load(), manual=True)
    return jsonify(ok=True, started=started)


@bp.route("/api/notify/approval", methods=["POST"])
def notify_approval():
    """A local script tells the user something: every open tab chimes.

    Purpose
    -------
    Some external processes (notably CustomerNode's agentic release pipeline,
    which serves a branded approval page on 127.0.0.1 for a human YES/NO
    click) need to alert the user. On desktop the page opening is obvious; on
    mobile, or when the user has switched away, it is easy to miss for hours.

    Body (all optional):
        {
          "title": "Production Release",       // short label
          "message": "Release v1.2.3 ready",   // longer body text
          "source": "customernode",            // slug for grouping
          "url": "http://127.0.0.1:PORT/"      // the approval page, if any
        }

    Two things happen.

    1. Always: the title, message and source are broadcast once as the
       Socket.IO event ``external_notice``. Each tab pings once
       (static/js/notify.js): a chime, and a one-off OS notification if the
       tab is in the background. That is all a ping without an approval page
       gets ("release is live", "first test red"): there is nothing to
       answer, so nothing may keep flashing.

    2. When the ping names an approval page (``url`` is the front page of a
       port on this machine, there is a ``title``, and the caller is a
       process on this machine), app/approval_watch.py starts watching that
       page. For as long as the page is verifiably still waiting, background
       tabs flash their title; they stop when it is answered. See that module
       for what "still waiting" means. ``id`` in the reply and in the
       broadcast names the watched page; it is "" when nothing is watched.

    Response: ``{"ok": true, "id": str}``. This endpoint deliberately never
    returns 4xx for malformed input: a broken alert must not break the release
    script that emitted it.
    """
    try:
        payload = request.get_json(silent=True) or {}
    except Exception:
        payload = {}
    if not isinstance(payload, dict):
        payload = {}
    data = {
        "title": str(payload.get("title") or "Approval needed"),
        "message": str(payload.get("message") or ""),
        "source": str(payload.get("source") or ""),
        "id": "",
    }
    url = str(payload.get("url") or "")
    try:
        # The script's own title, never the default above: the watch
        # recognises the page by it. Local callers only, because the watch
        # fetches the address it is given.
        if payload.get("title") and url and _caller_is_local():
            card = approval_watch.watch.register(data["title"], data["message"], data["source"], url)
            if card:
                data["id"] = card["id"]
    except Exception:
        # The ping still goes out below, as one that names no page.
        log.exception("notify/approval: could not start watching %r", url[:200])
    try:
        socketio.emit("external_notice", data)
    except Exception:
        # Never let a failed broadcast propagate — the caller (a release
        # script) does not care whether the ping actually reached a client.
        pass
    return jsonify(ok=True, id=data["id"])


@bp.route("/api/notify/approval/resolve", methods=["POST"])
def notify_approval_resolve():
    """A local script says its approval page has been answered.

    Body: ``{"id": str}`` (from the reply to ``POST /api/notify/approval``) or
    ``{"url": str}`` (the page that was announced).

    Optional: app/approval_watch.py notices an answered page by itself at its
    next check (every half second while the approval is young, every three
    seconds after ten minutes). A script that calls this ends the flash at
    once. Reply: ``{"ok": true, "resolved": int}``; unknown ids and malformed
    bodies resolve nothing and are not an error. Local callers only (403
    otherwise).
    """
    if not _caller_is_local():
        return jsonify(ok=False, resolved=0, error="local callers only"), 403
    payload = request.get_json(silent=True)
    if not isinstance(payload, dict):
        payload = {}
    try:
        resolved = approval_watch.watch.resolve(payload.get("id"), payload.get("url"))
    except Exception:
        log.exception("notify/approval/resolve failed")
        resolved = 0
    return jsonify(ok=True, resolved=resolved)


@bp.route("/api/notify/approvals")
def notify_approvals():
    """The approval pages that are open right now.

    The ``approvals_open`` Socket.IO event, plus each page's address.
    Side-effect-free: it reads the watch's state and checks nothing. For
    answering "why is this tab flashing" (or "why is it not").
    """
    return jsonify(ok=True, open=approval_watch.watch.open_now(with_urls=True),
                   ttl_ms=int(approval_watch.CLIENT_TTL_S * 1000))


@bp.route("/api/health")
def health():
    """Full-stack readiness: web AND daemon.

    /api/ping only proves the WEB server (5050) is alive. But VibeNode has a
    second failure mode that looks 'up but broken': the web server survives
    while the session DAEMON (5051) dies (crash, or killed under load). In that
    state the UI loads, /api/ping returns 200, yet nothing works — the user
    just gets a 'Lost connection to daemon' toast over a dead UI. This endpoint
    exposes the daemon's real connection state so the client can surface a
    proper recovery action, and so the mobile reviver only hands the user into
    the app once it is FULLY ready (web + daemon), not mid-boot when the daemon
    is still coming up (which renders as an unstyled/half-broken page).

    Side-effect-free: reads the DaemonClient's cached connection flag; no
    shell-out, no daemon round-trip.
    """
    from flask import current_app
    daemon_up = False
    try:
        sm = getattr(current_app, "session_manager", None)
        daemon_up = bool(getattr(sm, "is_connected", False))
    except Exception:
        daemon_up = False
    return jsonify(ok=True, web=True, daemon=daemon_up, ready=daemon_up)


@bp.route("/api/docs")
def api_docs():
    """Serve the API documentation page (Redoc)."""
    docs_dir = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), 'docs', 'api')
    return send_from_directory(docs_dir, 'index.html')


@bp.route("/api/docs/<path:filename>")
def api_docs_assets(filename):
    """Serve supporting API doc files (openapi.yaml, etc.)."""
    docs_dir = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), 'docs', 'api')
    return send_from_directory(docs_dir, filename)


@bp.route("/api/restart", methods=["POST"])
def restart_server():
    """Restart the web server, daemon, or both based on the `scope` param.

    Body JSON (optional):
      scope: "web" (default) | "daemon" | "both"

    "web"    — kills only port 5050, leaves daemon alive
    "daemon" — kills only port 5051, leaves web alive
    "both"   — kills both 5050 and 5051
    """
    try:
        data = request.get_json(silent=True) or {}
        scope = data.get("scope", "web")

        # Resolve ports from env so test/side-by-side instances don't
        # accidentally murder the user's production 5050/5051.  Previously
        # hardcoded — a known cause of the "restart didn't restart" symptom
        # on non-default installs.
        web_port = _web_port()
        daemon_port = _daemon_port()
        ports = []
        if scope in ("web", "both"):
            ports.append(web_port)
        if scope in ("daemon", "both"):
            ports.append(daemon_port)
        if not ports:
            ports = [web_port]

        # Launch via session_manager.py (not run.py) so the boot splash shows
        entry_script = os.path.join(os.path.dirname(os.path.dirname(__file__)), "..", "session_manager.py")
        entry_script = os.path.abspath(entry_script)
        project_dir = os.path.dirname(entry_script)

        # When doing a web-only restart, tell run.py to preserve the daemon
        # so active sessions and their CLI subprocesses are not killed.
        preserve_daemon = scope == "web"

        if sys.platform == "win32":
            pythonw = os.path.join(os.path.dirname(sys.executable), "pythonw.exe")
            if not os.path.exists(pythonw):
                pythonw = sys.executable

            port_list = ",".join(str(p) for p in ports)
            env_set = "$env:VIBENODE_PRESERVE_DAEMON='1'; " if preserve_daemon else ""
            # PORT-5050 RESTART-RACE FIX (Step 5, 2026-09-26, part 2 of 2).
            #
            # The kill loop MUST capture the target PIDs upfront and never
            # re-query port owners between iterations.  The moment the web
            # process on 5050 dies, reviver.py (polling every 2s) can bind
            # 5050 to serve the Start page while VibeNode is between web
            # PIDs.  The pre-fix loop then saw the reviver as a "port
            # owner" and killed it on the next iteration — the guardian
            # respawned the reviver with a new PID, self-healing but
            # tripping the strict Step 5 cutover assertion (`[RECOVERY]
            # reviver unaffected`), and briefly leaving the phone with
            # no Start page.
            #
            # By capturing PIDs before killing and polling only those PIDs
            # for death, the reviver's bystander bind never enters the
            # kill decision.  ``reclaim_port()`` in run.py negotiates the
            # reviver's yield later via the /yield control endpoint —
            # no kill required.
            restart_cmd = (
                "powershell -NoProfile -Command \""
                f"$targetPids = @(Get-NetTCPConnection -LocalPort {port_list} -ErrorAction SilentlyContinue | "
                "  Select-Object -ExpandProperty OwningProcess -Unique); "
                "$maxTries = 10; "
                "for ($i = 0; $i -lt $maxTries; $i++) { "
                "  $alive = @($targetPids | Where-Object { "
                "    (Get-Process -Id $_ -ErrorAction SilentlyContinue) -ne $null "
                "  }); "
                "  if ($alive.Count -eq 0) { break }; "
                "  $alive | ForEach-Object { Stop-Process -Id $_ -Force -ErrorAction SilentlyContinue }; "
                "  Start-Sleep -Milliseconds 500 "
                "}; "
                # __pycache__ cleanup MUST NOT descend into .venv* trees.
                # Get-ChildItem -Recurse walks every subdirectory even
                # with -Exclude, and .venv-arm64-candidate (used during
                # ARM64 cutover) lives inside the project tree with tens
                # of thousands of __pycache__ dirs under site-packages.
                # Walking that tree stretched the restart window enough
                # for reviver.py to bind port 5050 as a bystander; the
                # kill loop then killed the reviver, and the strict
                # Step 5 cutover assertion tripped even though the
                # guardian respawned it. Stack-based traversal here
                # prunes any .venv* subtree BEFORE descending, so no
                # virtualenv's bytecode is touched or scanned.
                f"$stk = [System.Collections.Stack]::new(); "
                f"$stk.Push('{project_dir}'); "
                "while ($stk.Count -gt 0) { "
                "  $d = $stk.Pop(); "
                "  Get-ChildItem -LiteralPath $d -Directory -Force -ErrorAction SilentlyContinue | ForEach-Object { "
                "    if ($_.Name -like '.venv*') { return }; "
                "    if ($_.Name -eq '__pycache__') { Remove-Item -LiteralPath $_.FullName -Recurse -Force -ErrorAction SilentlyContinue } "
                "    else { $stk.Push($_.FullName) } "
                "  } "
                "}; "
                "Start-Sleep -Seconds 1; "
                f"{env_set}"
                f"Start-Process -FilePath '{pythonw}' -ArgumentList '\"{entry_script}\"' "
                f"-WorkingDirectory '{project_dir}'"
                "\""
            )
            subprocess.Popen(
                restart_cmd,
                shell=True,
                creationflags=_NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP,
            )

        elif sys.platform in ("darwin", "linux"):
            # POSIX restart flow.  This path was previously broken on
            # Linux for "Restart Server → Daemon": the new python died
            # silently and the daemon never came back.  Two correctness
            # requirements were missing — both are fixed here.
            #
            # 1. ``start_new_session=True`` on the outer ``subprocess.Popen``.
            #    Without it the bash subprocess inherits the web server's
            #    process group / controlling terminal.  The new python
            #    instance launched by bash later kills port 5050 (the
            #    old web); without isolation, that kill propagates to
            #    bash via the shared process group, killing it before
            #    it finishes spawning the replacement python.  Mirrors
            #    what Windows gets implicitly via ``Start-Process``.
            #
            # 2. stdout/stderr redirected to ``logs/restart.log`` instead
            #    of ``/dev/null``.  When a restart breaks (and they do
            #    break — see the bug this fix addresses), the user can
            #    read the log instead of staring at a dead UI.
            #
            # ``nohup ... &`` is preserved (portable across Linux and
            # macOS, no external ``setsid`` binary required — macOS does
            # not install setsid by default).  With the outer
            # ``start_new_session=True`` in place, the launched python
            # already lives outside the dying web server's session, so
            # nohup+& is sufficient detachment for the inner spawn.
            # PORT-5050 RESTART-RACE FIX (Step 5, 2026-09-26, part 2 of 2).
            #
            # Mirror the Windows change: capture the target PIDs upfront
            # and never re-query port owners between iterations.  The
            # pre-fix loop ran ``lsof -ti :5050 | xargs kill -9`` in a
            # tight loop, which caught reviver.py as a bystander the
            # instant it re-bound 5050 to serve the Start page.  Now
            # we track only the initial PIDs and stop as soon as they
            # are all dead, regardless of who owns the port after.
            # See the Windows branch above for full rationale.
            port_list_bash = " ".join(str(p) for p in ports)
            # NOTE ON QUOTING: this entire block will be spliced inside
            # ``bash -c '...'`` (single-quoted outer).  A single quote
            # inside these strings would terminate the outer shell
            # quoting.  Do not use ``tr '\n' ' '`` or any ``sed`` with
            # single-quoted args here.  Newlines in ``target_pids`` are
            # handled by word-splitting in ``for _pid in $target_pids``,
            # which is bash's default IFS behavior.
            kill_cmds = (
                f"target_pids=$(for _pp in {port_list_bash}; do "
                "lsof -ti :$_pp 2>/dev/null; done | sort -u); "
                "for _i in $(seq 1 10); do "
                "_alive=; "
                "for _pid in $target_pids; do "
                "kill -0 $_pid 2>/dev/null && _alive=\"$_alive $_pid\"; "
                "done; "
                "[ -z \"$_alive\" ] && break; "
                "for _pid in $_alive; do kill -9 $_pid 2>/dev/null; done; "
                "sleep 0.5; "
                "done;"
            )
            # BUGFIX: ``nohup VAR=value cmd`` is NOT valid — env-prefix syntax
            # is a bash builtin (only works for "simple commands"), but here
            # bash sees ``nohup`` as the command and ``VAR=value`` as its first
            # argument, so nohup tries to execute a file literally named
            # "VAR=value" and dies with "No such file or directory".  This
            # silently broke every Linux "Restart Web" (the only scope that
            # sets preserve_daemon): the bash subshell killed port 5050, then
            # nohup failed to launch the new web, and the user was left with
            # no web server and an unresponsive UI.  Fix: ``export`` the var
            # in the shell BEFORE calling nohup, so nohup inherits it via the
            # environment (the standard pattern).
            env_export = (
                "export VIBENODE_PRESERVE_DAEMON=1; " if preserve_daemon else ""
            )
            restart_log = os.path.join(project_dir, "logs", "restart.log")
            # Ensure logs dir exists so the bash redirect can't fail.
            try:
                os.makedirs(os.path.dirname(restart_log), exist_ok=True)
            except Exception:
                pass
            # __pycache__ cleanup MUST NOT descend into .venv* trees.
            # A ``.venv-arm64-candidate`` or similar virtualenv inside the
            # project tree carries tens of thousands of __pycache__ dirs
            # under site-packages; a plain recursive ``find`` walks all of
            # them. On the Windows equivalent that stretched the restart
            # window enough for reviver.py to bind port 5050 as a
            # bystander (see Step 5 cutover diagnosis); the same risk
            # applies on POSIX, so mirror the pruning here. ``-prune``
            # on .venv* short-circuits the descent BEFORE any of the
            # virtualenv's bytecode is scanned.
            restart_cmd = (
                f"bash -c '"
                # kill_cmds is now the complete kill loop (initial-PID
                # capture + poll-for-death), so no outer for/sleep
                # wrapper is required. See the Step 5 fix comment above.
                f"{kill_cmds} "
                f"find \"{project_dir}\" \\( -type d -name \".venv*\" -prune \\) -o \\( -type d -name __pycache__ -exec rm -rf {{}} + \\) 2>/dev/null; "
                f"sleep 1; "
                f"{env_export}"
                f"nohup \"{sys.executable}\" \"{entry_script}\" "
                f"</dev/null >>\"{restart_log}\" 2>&1 &'"
            )
            subprocess.Popen(
                restart_cmd,
                shell=True,
                # Detach the bash subprocess from this web server's
                # process group so it survives the upcoming kill of
                # port 5050.  Mirrors what Windows gets implicitly via
                # Start-Process.
                start_new_session=True,
            )

        return jsonify({"ok": True, "message": f"Restarting ({scope})..."})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


@bp.route("/api/shutdown", methods=["POST"])
def shutdown_server():
    """Shut down both the web server and daemon without restarting.

    Sends the response first, then kills ports 5050 and 5051 after a
    short delay so the client receives the acknowledgement.
    """
    try:
        project_dir = os.path.abspath(
            os.path.join(os.path.dirname(os.path.dirname(__file__)), "..")
        )
        web_port = _web_port()
        daemon_port = _daemon_port()
        ports = f"{web_port},{daemon_port}"

        if sys.platform == "win32":
            shutdown_cmd = (
                'powershell -NoProfile -Command "'
                "Start-Sleep -Seconds 2; "
                f"$pids = @(Get-NetTCPConnection -LocalPort {ports} -ErrorAction SilentlyContinue | "
                "Select-Object -ExpandProperty OwningProcess -Unique); "
                "$pids | ForEach-Object { Stop-Process -Id $_ -Force -ErrorAction SilentlyContinue }"
                '"'
            )
            subprocess.Popen(
                shutdown_cmd,
                shell=True,
                creationflags=_NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP,
            )
        else:
            shutdown_cmd = (
                "bash -c '"
                "sleep 2; "
                f"lsof -ti :{web_port} | xargs kill -9 2>/dev/null; "
                f"lsof -ti :{daemon_port} | xargs kill -9 2>/dev/null"
                "'"
            )
            # start_new_session=True so the bash isn't taken down with
            # the web server when the kill -9 above lands on us.  Without
            # it the bash dies before reaching the port-5051 kill, leaving
            # the daemon orphaned (same root cause as the restart bug
            # fixed above in restart_server()).
            subprocess.Popen(shutdown_cmd, shell=True, start_new_session=True)

        return jsonify({"ok": True, "message": "Server shutting down..."})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500
