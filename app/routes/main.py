"""
Main route -- serves the single-page application.
"""

import json
import os
import subprocess
import sys

from flask import Blueprint, Response, jsonify, render_template, request, send_from_directory
from ..platform_utils import NO_WINDOW as _NO_WINDOW
from .. import mobile_command

bp = Blueprint('main', __name__)


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
            restart_cmd = (
                "powershell -NoProfile -Command \""
                "$maxTries = 10; "
                "for ($i = 0; $i -lt $maxTries; $i++) { "
                f"  $pids = @(Get-NetTCPConnection -LocalPort {port_list} -ErrorAction SilentlyContinue | "
                "    Select-Object -ExpandProperty OwningProcess -Unique); "
                "  if ($pids.Count -eq 0) { break }; "
                "  $pids | ForEach-Object { Stop-Process -Id $_ -Force -ErrorAction SilentlyContinue }; "
                "  Start-Sleep -Milliseconds 500 "
                "}; "
                f"Get-ChildItem -Path '{project_dir}' -Recurse -Directory -Filter '__pycache__' | "
                "Remove-Item -Recurse -Force -ErrorAction SilentlyContinue; "
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
            kill_cmds = " ".join(
                f"lsof -ti :{p} | xargs kill -9 2>/dev/null;" for p in ports
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
            restart_cmd = (
                f"bash -c '"
                f"for i in $(seq 1 10); do {kill_cmds} sleep 0.5; done; "
                f"find \"{project_dir}\" -type d -name __pycache__ -exec rm -rf {{}} + 2>/dev/null; "
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
