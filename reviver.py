#!/usr/bin/env python3
"""
VibeNode Reviver — the "Start VibeNode" button for your phone.

THE PROBLEM
-----------
Mobile Command exposes VibeNode to your phone over Tailscale:

    phone --HTTPS--> <machine>.ts.net --(tailscale serve)--> 127.0.0.1:5050

That ``tailscale serve`` mapping lives inside ``tailscaled``, so it stays
configured even when VibeNode itself is completely down. The catch: when the
web server on 5050 is not running, nothing answers there, and Tailscale returns
its own bare "502 Bad Gateway" page. There is no VibeNode left to render a
"Start" button — so from your phone, a killed VibeNode is a dead link with no
way back short of walking to the computer.

THE FIX (this file)
-------------------
A tiny, dependency-free helper that keeps a "Start VibeNode" page reachable at
127.0.0.1:5050 *whenever the real web server is not there*. Because Tailscale
serve always points at 5050, whoever is bound there is exactly what the phone
sees:

    * VibeNode up   -> the phone sees the real app.
    * VibeNode down -> the phone sees this reviver's "Start VibeNode" page,
                       and one tap re-launches VibeNode (same entry point as
                       the desktop shortcut: session_manager.py).

DESIGN INVARIANTS
-----------------
1. ZERO HOT-PATH FOOTPRINT. When VibeNode is running, the reviver holds NO web
   port at all — it just polls "is 5050 alive?" every couple of seconds. It is
   never a proxy; it never sits in front of your live Socket.IO / voice traffic.
   It only binds 5050 during the window when VibeNode is *down*.

2. STAYS PRIVATE. Both the serve socket (5050) and the control socket
   (VIBENODE_REVIVER_PORT, default 5052) bind 127.0.0.1 only. Nothing here
   rebinds VibeNode or exposes anything new to the tailnet: the phone reaches
   the Start page through the *existing* ``tailscale serve`` -> 5050 mapping,
   exactly like it reaches the real app. The control port is loopback-only and
   never served to the tailnet.

3. SINGLETON. The reviver binds the control port for its whole lifetime; a
   second instance fails that bind and exits. So ``session_manager.py`` can
   spawn it on every launch without ever stacking duplicates.

4. GETS OUT OF THE WAY CLEANLY. When a real VibeNode start happens (desktop
   shortcut / launch script / in-app restart), ``session_manager.py`` first
   POSTs ``/yield`` to the control port. The reviver synchronously releases 5050
   so ``run.py`` can bind it — WITHOUT the reviver process dying. It then waits
   for the real server to come up and goes dormant. No port fight, and the
   supervisor survives to help again next time.

5. OPT-IN + SELF-RETIRING. The reviver only exists for users who turned Mobile
   Command on. It re-checks that flag every loop and exits if the feature is
   disabled, so turning Mobile Command off tears the helper down on its own.

6. NO NEW DEPENDENCY. Pure stdlib. It must run even when VibeNode's own
   packages are half-installed or broken — that is often *why* the server is
   down.

Survivability note: because the reviver never matches the Stop shortcut's kill
patterns (it is ``reviver.py``, not ``session_manager.py`` / ``run.py``, and it
does not sit on 5050/5051 while dormant), a normal "Stop VibeNode" leaves the
reviver alive — which is the whole point: after a Stop, your phone can bring
VibeNode back. It cannot defend against an indiscriminate "kill every python"
sweep, but it collapses the common failure window from "forever" to "one tap".
"""

from __future__ import annotations

import getpass
import hashlib
import json
import os
import plistlib
import shutil
import socket
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

_HERE = Path(__file__).resolve().parent

WEB_PORT = int(os.environ.get("VIBENODE_WEB_PORT", 0)) or 5050
CONTROL_PORT = int(os.environ.get("VIBENODE_REVIVER_PORT", 0)) or 5052
# The guardian is a second, tiny process that does nothing but respawn the
# reviver if it dies (and the reviver respawns the guardian) — mutual
# resurrection so a single-process death never leaves the phone without a Start
# button while the machine is up. It holds this loopback port as its liveness
# beacon + singleton.
GUARDIAN_PORT = int(os.environ.get("VIBENODE_GUARDIAN_PORT", 0)) or 5053

# How long to wait for a real VibeNode to bind 5050 after we yield / launch it
# before we assume the start failed and re-show the Start page.
_START_GRACE_SECONDS = 90
# Main-loop cadence. Small enough that the phone sees the Start page promptly
# after a crash; large enough to be effectively free when idle.
_POLL_SECONDS = 2.0

# Sentinel embedded in every page the reviver serves. The phone-side poller uses
# it to tell "still the reviver" from "the real app is up now".
_SENTINEL = "vibenode-reviver-page"

_LOG_PATH = _HERE / "logs" / "reviver.log"


def _log(msg: str) -> None:
    """Best-effort line to logs/reviver.log (gitignored). Never raises."""
    try:
        _LOG_PATH.parent.mkdir(exist_ok=True)
        with open(_LOG_PATH, "a", encoding="utf-8") as fh:
            fh.write("[%s] %s\n" % (time.strftime("%Y-%m-%d %H:%M:%S"), msg))
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Config reads (cheap, direct JSON — no app.* imports, which may be broken when
# VibeNode is down; that is often the whole reason the reviver is needed).
# ---------------------------------------------------------------------------

def _config_path() -> Path:
    env = os.environ.get("VIBENODE_CONFIG")
    return Path(env) if env else _HERE / "kanban_config.json"


def _read_config():
    """Parsed config dict, or a distinct sentinel per failure mode:
      * {}   — the file is ABSENT (never configured) => treat as disabled.
      * None — the file EXISTS but couldn't be read/parsed right now (e.g. a
               concurrent save wrote it partially, or a momentary lock).
               Callers must treat None as 'unknown — tear NOTHING down'.
    The distinction is load-bearing: a partial-write race must never be read as
    'Mobile Command was turned off'."""
    p = _config_path()
    try:
        if not p.exists():
            return {}
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return None


def _mobile_enabled() -> bool:
    """True if Mobile Command is on. A transient read failure on an EXISTING
    config returns True (keep running) so a config-save race can never tear the
    reviver + guardian down; only an explicit ``false`` flag — or a missing,
    never-configured file — counts as off."""
    cfg = _read_config()
    if cfg is None:      # exists but unreadable this instant — do NOT self-destruct
        return True
    return bool(cfg.get("mobile_command_enabled", False))


def _device_name() -> str:
    cfg = _read_config() or {}
    name = (cfg.get("mobile_command_device_name") or "").strip()
    if name:
        return name
    try:
        host = (socket.gethostname() or "").split(".")[0].strip()
    except Exception:
        host = ""
    return host or "VibeNode"


# ---------------------------------------------------------------------------
# Liveness probe
# ---------------------------------------------------------------------------

def _something_listening(port: int) -> bool:
    """True if a TCP listener answers on 127.0.0.1:port right now."""
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.settimeout(0.75)
    try:
        s.connect(("127.0.0.1", port))
        return True
    except OSError:
        return False
    finally:
        try:
            s.close()
        except Exception:
            pass


# ---------------------------------------------------------------------------
# Launching the real VibeNode (same entry point the desktop shortcut uses)
# ---------------------------------------------------------------------------

def _python_for_spawn() -> str:
    """Prefer a windowless interpreter on Windows (pythonw) so the spawned
    server has no stray console; otherwise the current interpreter."""
    exe = sys.executable or "python"
    if sys.platform == "win32":
        pythonw = Path(exe).parent / "pythonw.exe"
        if pythonw.exists():
            return str(pythonw)
    return exe


def _spawn_reviver_process(extra_args: list[str] | None = None) -> None:
    """Spawn another copy of reviver.py detached (no args -> reviver, --guardian
    -> guardian). Used for mutual resurrection. Best-effort, never raises."""
    py = _python_for_spawn()
    cmd = [py, str(_HERE / "reviver.py")] + list(extra_args or [])
    try:
        kwargs = {"cwd": str(_HERE)}
        if sys.platform == "win32":
            kwargs["creationflags"] = (
                subprocess.CREATE_NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP)
        else:
            kwargs["start_new_session"] = True
            kwargs["stdout"] = subprocess.DEVNULL
            kwargs["stderr"] = subprocess.DEVNULL
        subprocess.Popen(cmd, **kwargs)
    except Exception as e:
        _log("spawn %s failed: %s" % (extra_args or "reviver", e))


def _launch_vibenode() -> None:
    """Spawn session_manager.py fully detached — mirrors launch.bat/launch.sh."""
    entry = _HERE / "session_manager.py"
    if not entry.exists():
        _log("cannot launch: session_manager.py not found at %s" % entry)
        return
    py = _python_for_spawn()

    # If the session daemon is STILL ALIVE (a web-only death — the common case
    # when "load" killed just the web server), tell run.py to preserve it so we
    # don't needlessly kill live sessions. Only a full cold start (daemon also
    # down) gets the default port-kill. This makes the phone's Start button at
    # least as safe as the desktop shortcut, and safer when the daemon survived.
    daemon_port = int(os.environ.get("VIBENODE_DAEMON_PORT", 0) or 5051)
    env = dict(os.environ)
    if _something_listening(daemon_port):
        env["VIBENODE_PRESERVE_DAEMON"] = "1"
        _log("daemon on %d is alive — launching with VIBENODE_PRESERVE_DAEMON=1"
             % daemon_port)
    else:
        env.pop("VIBENODE_PRESERVE_DAEMON", None)  # ensure a true cold start

    _log("launching VibeNode via %s %s" % (py, entry))
    try:
        kwargs = {"cwd": str(_HERE), "env": env}
        if sys.platform == "win32":
            kwargs["creationflags"] = (
                subprocess.CREATE_NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP
            )
            subprocess.Popen([py, str(entry)], **kwargs)
        else:
            # Detach from the reviver's session so the server outlives us, and
            # tee output into the same log the launchers use.
            log_fh = None
            try:
                (_HERE / "logs").mkdir(exist_ok=True)
                log_fh = open(_HERE / "logs" / "_server.log", "a", encoding="utf-8")
            except Exception:
                log_fh = None
            kwargs["start_new_session"] = True
            if log_fh is not None:
                kwargs["stdout"] = log_fh
                kwargs["stderr"] = log_fh
            subprocess.Popen([py, str(entry)], **kwargs)
    except Exception as e:
        _log("launch failed: %s" % e)


# ---------------------------------------------------------------------------
# Always-on supervisor (the "anytime" guarantee).
#
# The lightweight reviver alone is just a process: whatever kills VibeNode (a
# broad "kill every python" sweep) or a reboot can take it down too, and then
# the phone is back to a dead link. To make "if VibeNode is down, the phone
# ALWAYS shows a Start button" hold unconditionally, we register a per-checkout
# OS mechanism that (re)starts the reviver whenever it isn't running and after a
# reboot. The reviver is thus self-installing: the OS starts the reviver, and
# the reviver ensures the OS mechanism exists — and self-uninstalls when Mobile
# Command is turned off (so nothing lingers or restart-loops).
#
# Per-OS mechanism (all run in the plain logged-in USER context — no admin, no
# sudo, no daemon install; honoring the "don't make users do anything" promise):
#   * Windows : a .vbs in the user's Startup folder (plain file I/O — the
#               load-bearing autostart that can't be virtualized/denied) runs
#               the reviver at logon, PLUS a best-effort Task Scheduler task
#               (/SC MINUTE /MO 2) for mid-session respawn on top.
#   * macOS   : launchd LaunchAgent (~/Library/LaunchAgents) with KeepAlive +
#               RunAtLoad — launchd restarts the reviver the instant it dies and
#               loads it at login. Instant, not polled.
#   * Linux   : systemd --user service with Restart=always + WantedBy=
#               default.target — restarts on death, starts at login. Linger is
#               enabled best-effort so it can also come up before login.
#               CAVEAT: linger cannot help on per-user ENCRYPTED homes
#               (ecryptfs/fscrypt/homed) — pre-login, both the unit file in
#               ~/.config/systemd/user and reviver.py itself are ciphertext,
#               so nothing of ours can run until the user logs in. Covering
#               that window requires a machine-local system-level stub
#               installed OUTSIDE the home (root/one-time-sudo territory,
#               which this self-installer deliberately never touches).
#
# PRE-LOGIN WINDOW (all platforms): every mechanism above fires at LOGIN, not
# at boot. If the machine reboots unattended, the phone link stays dead until
# someone logs in. Opt-in, one-time setup scripts that close that window
# (Windows boot task, Linux linger/ecryptfs unlock page) live in
# scripts/boot_access/ — see its README.
#
# All names are salted with the checkout path so multiple VibeNode clones never
# fight over one task/agent/unit. Everything is best-effort: any failure just
# logs and falls back to lightweight (armed-each-launch) behavior — never a
# crash. Note the OS-spawned reviver uses DEFAULT ports (5050/5051/5052); custom
# side-by-side instances are covered only within a session, not across reboots.
# ---------------------------------------------------------------------------

def _salt() -> str:
    return hashlib.md5(str(_HERE).encode("utf-8")).hexdigest()[:8]


def _run(cmd: list[str], timeout: int = 30) -> tuple[int, str]:
    """Run a command, capturing output. Windowless on Windows. Never raises."""
    try:
        r = subprocess.run(
            cmd, capture_output=True, text=True, timeout=timeout,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        return r.returncode, ((r.stdout or "") + (r.stderr or "")).strip()
    except Exception as e:
        return 1, str(e)


def register_supervisor() -> None:
    """Idempotently register the OS mechanism that keeps the reviver alive.

    Dispatches per platform; best-effort. On unknown platforms it logs and
    leaves the reviver in lightweight mode.
    """
    try:
        if sys.platform == "win32":
            _register_windows()
        elif sys.platform == "darwin":
            _register_macos()
        elif sys.platform.startswith("linux"):
            _register_linux()
        else:
            _log("OS supervisor not implemented on %s — lightweight mode"
                 % sys.platform)
    except Exception as e:  # pragma: no cover - never let this crash the reviver
        _log("register_supervisor failed (non-fatal): %s" % e)


def unregister_supervisor() -> None:
    """Remove the OS supervisor mechanism (best-effort, per platform)."""
    try:
        if sys.platform == "win32":
            _unregister_windows()
        elif sys.platform == "darwin":
            _unregister_macos()
        elif sys.platform.startswith("linux"):
            _unregister_linux()
    except Exception as e:  # pragma: no cover
        _log("unregister_supervisor failed (non-fatal): %s" % e)


# ---- Windows: Task Scheduler --------------------------------------------------

def _win_task_name() -> str:
    return "VibeNodeReviver_%s" % _salt()


def _schtasks_bin() -> str:
    return (shutil.which("schtasks")
            or os.path.join(os.environ.get("SystemRoot", r"C:\Windows"),
                            "System32", "schtasks.exe"))


def _startup_dir() -> str:
    """The current user's Startup folder — anything here runs at every logon."""
    return os.path.join(os.environ.get("APPDATA", ""), "Microsoft", "Windows",
                        "Start Menu", "Programs", "Startup")


def _startup_vbs_path() -> str:
    return os.path.join(_startup_dir(), "VibeNodeReviver_%s.vbs" % _salt())


def _register_windows() -> None:
    py = _python_for_spawn()
    script_path = str(_HERE / "reviver.py")

    # PRIMARY — filesystem autostart. A tiny .vbs in the user's Startup folder
    # launches the reviver (windowless) at every logon. This is plain file I/O:
    # no service API, no admin, nothing that a locked-down or virtualized
    # environment can silently no-op. It is the load-bearing guarantee — the
    # same principle macOS/Linux use (a plist/unit FILE). The .vbs (not a .bat)
    # keeps logon flash-free. Covers reboot / log-off-on.
    startup_ok = False
    try:
        startup = _startup_dir()
        if startup:
            os.makedirs(startup, exist_ok=True)
            # In VBScript a literal " inside a string is written as "".
            inner = '""%s"" ""%s""' % (py, script_path)
            vbs = 'CreateObject("WScript.Shell").Run "%s", 0, False\r\n' % inner
            with open(_startup_vbs_path(), "w", encoding="utf-8") as fh:
                fh.write(vbs)
            startup_ok = os.path.isfile(_startup_vbs_path())  # verify it landed
    except Exception as e:
        _log("startup-folder autostart write failed: %s" % e)

    # SECONDARY — Task Scheduler, best-effort. Adds mid-session respawn (every
    # 2 min, whenever the reviver isn't running) on top of the logon autostart,
    # so a broad kill is recovered without waiting for the next logon. Verified
    # with a follow-up /Query so we never claim a task that didn't persist.
    task_ok = False
    exe = _schtasks_bin()
    if os.path.isfile(exe):
        name = _win_task_name()
        tr = '"%s" "%s"' % (py, script_path)
        rc, _ = _run([exe, "/Create", "/TN", name, "/TR", tr, "/SC", "MINUTE",
                      "/MO", "2", "/RL", "LIMITED", "/F"])
        if rc == 0:
            q, _ = _run([exe, "/Query", "/TN", name])
            task_ok = (q == 0)

    if startup_ok and task_ok:
        _log("Windows supervisor: logon autostart + respawn task installed")
    elif startup_ok:
        _log("Windows supervisor: logon autostart installed "
             "(scheduler task unavailable — reboot/logon still covered)")
    elif task_ok:
        _log("Windows supervisor: respawn task installed (no Startup folder)")
    else:
        _log("Windows supervisor: could NOT install autostart — lightweight only")


def _unregister_windows() -> None:
    try:
        p = _startup_vbs_path()
        if os.path.isfile(p):
            os.remove(p)
    except Exception:
        pass
    exe = _schtasks_bin()
    if os.path.isfile(exe):
        _run([exe, "/Delete", "/TN", _win_task_name(), "/F"])
    _log("Windows supervisor removed (startup autostart + scheduler task)")


# ---- macOS: launchd LaunchAgent ----------------------------------------------

def _launchd_label() -> str:
    return "com.vibenode.reviver.%s" % _salt()


def _launchd_plist_path() -> Path:
    return Path.home() / "Library" / "LaunchAgents" / (_launchd_label() + ".plist")


def _launchd_plist_bytes() -> bytes:
    log_path = str(_HERE / "logs" / "reviver_launchd.log")
    plist = {
        "Label": _launchd_label(),
        "ProgramArguments": [sys.executable, str(_HERE / "reviver.py")],
        "WorkingDirectory": str(_HERE),
        "RunAtLoad": True,     # start at login
        "KeepAlive": True,     # restart the instant it dies (broad kill, crash)
        "ProcessType": "Background",
        "StandardOutPath": log_path,
        "StandardErrorPath": log_path,
    }
    return plistlib.dumps(plist)


def _register_macos() -> None:
    launchctl = shutil.which("launchctl") or "/bin/launchctl"
    plist_path = _launchd_plist_path()
    try:
        plist_path.parent.mkdir(parents=True, exist_ok=True)
        plist_path.write_bytes(_launchd_plist_bytes())
    except Exception as e:
        _log("could not write LaunchAgent plist: %s" % e)
        return
    # Load WITHOUT an unload-first: unloading our own agent would SIGTERM this
    # very process. `load` of an already-loaded agent just errors harmlessly, so
    # we ignore rc — KeepAlive already keeps the running instance alive.
    rc, out = _run([launchctl, "load", "-w", str(plist_path)])
    _log("registered macOS LaunchAgent '%s' (load rc=%s%s)"
         % (_launchd_label(), rc, "" if rc == 0 else " — likely already loaded"))


def _unregister_macos() -> None:
    launchctl = shutil.which("launchctl") or "/bin/launchctl"
    plist_path = _launchd_plist_path()
    _run([launchctl, "unload", "-w", str(plist_path)])
    try:
        if plist_path.exists():
            plist_path.unlink()
    except Exception:
        pass
    _log("removed macOS LaunchAgent '%s'" % _launchd_label())


# ---- Linux: systemd --user service -------------------------------------------

def _systemd_unit_name() -> str:
    return "vibenode-reviver-%s.service" % _salt()


def _systemd_unit_path() -> Path:
    return Path.home() / ".config" / "systemd" / "user" / _systemd_unit_name()


def _systemd_unit_text() -> str:
    # VIBENODE_REVIVER_STANDBY: under Restart=always, "control port busy ->
    # exit 0" would be respawned by systemd every RestartSec forever while a
    # session-spawned reviver owns the port — a permanent, pointless restart
    # loop (one python spawn every 5s). The flag makes THIS copy wait in
    # standby and take over in-process instead (see _standby_acquire_control).
    return (
        "[Unit]\n"
        "Description=VibeNode mobile Start-page reviver "
        "(keeps VibeNode restartable from your phone)\n"
        "After=default.target\n\n"
        "[Service]\n"
        "Type=simple\n"
        "Environment=VIBENODE_REVIVER_STANDBY=1\n"
        "ExecStart=%s %s\n"
        "WorkingDirectory=%s\n"
        "Restart=always\n"
        "RestartSec=5\n\n"
        "[Install]\n"
        "WantedBy=default.target\n"
        % (sys.executable, str(_HERE / "reviver.py"), str(_HERE))
    )


def _register_linux() -> None:
    systemctl = shutil.which("systemctl")
    if not systemctl:
        _log("systemctl not found — lightweight mode (no systemd)")
        return
    unit_path = _systemd_unit_path()
    try:
        unit_path.parent.mkdir(parents=True, exist_ok=True)
        unit_path.write_text(_systemd_unit_text(), encoding="utf-8")
    except Exception as e:
        _log("could not write systemd unit: %s" % e)
        return
    _run([systemctl, "--user", "daemon-reload"])
    # enable --now is idempotent: it won't restart an already-running unit, so
    # it can't kill the current reviver. It just ensures enabled + started.
    rc, out = _run([systemctl, "--user", "enable", "--now", _systemd_unit_name()])
    # Best-effort: let the service come up before login too (headless/boot).
    try:
        _run(["loginctl", "enable-linger", getpass.getuser()])
    except Exception:
        pass
    if rc == 0:
        _log("registered Linux systemd user unit '%s'" % _systemd_unit_name())
    else:
        _log("could not enable unit '%s' (rc=%s): %s"
             % (_systemd_unit_name(), rc, out[:300]))


def _unregister_linux() -> None:
    systemctl = shutil.which("systemctl")
    unit_path = _systemd_unit_path()
    if systemctl:
        _run([systemctl, "--user", "disable", "--now", _systemd_unit_name()])
    try:
        if unit_path.exists():
            unit_path.unlink()
    except Exception:
        pass
    if systemctl:
        _run([systemctl, "--user", "daemon-reload"])
    _log("removed Linux systemd user unit '%s'" % _systemd_unit_name())


# ---------------------------------------------------------------------------
# Reviver controller — the shared state between the control server, the serve
# server, and the main loop.
# ---------------------------------------------------------------------------

class Reviver:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._serve_httpd: ThreadingHTTPServer | None = None
        # state: "dormant" (real app up), "serving" (we show Start page),
        # "waiting" (we yielded/launched; waiting for the real app to bind).
        self._state = "dormant"
        self._wait_deadline = 0.0
        # Set by the phone-side POST /start handler; the main loop consumes it
        # so the yield+launch happens off the serve thread (no self-deadlock).
        self._start_requested = threading.Event()

    # ---- serve server (port 5050) lifecycle ----

    def _start_serving(self) -> None:
        with self._lock:
            if self._serve_httpd is not None:
                return
            try:
                httpd = ThreadingHTTPServer(("127.0.0.1", WEB_PORT), _ServeHandler)
            except OSError as e:
                # Port grabbed by someone between our probe and our bind — treat
                # as "not ours", stay out of the way, retry next loop.
                _log("could not bind serve port %d: %s" % (WEB_PORT, e))
                return
            httpd.reviver = self  # type: ignore[attr-defined]
            self._serve_httpd = httpd
            self._state = "serving"
            threading.Thread(
                target=httpd.serve_forever, name="reviver-serve", daemon=True
            ).start()
            _log("serving Start page on 127.0.0.1:%d" % WEB_PORT)

    def _stop_serving_locked(self) -> None:
        httpd = self._serve_httpd
        self._serve_httpd = None
        if httpd is not None:
            # shutdown() must not be called from the serve thread; every caller
            # here is the main loop or the control thread, so this is safe.
            try:
                httpd.shutdown()
            except Exception:
                pass
            try:
                httpd.server_close()
            except Exception:
                pass
            _log("released serve port %d" % WEB_PORT)

    # ---- transitions ----

    def yield_now(self, launch: bool) -> None:
        """Release 5050 so a real VibeNode can bind it, and enter 'waiting'.

        Called synchronously from the control server's /yield handler (desktop /
        launcher / restart path) and from the main loop when the phone pressed
        Start. Safe from any thread except the serve thread.
        """
        with self._lock:
            self._stop_serving_locked()
            self._state = "waiting"
            self._wait_deadline = time.monotonic() + _START_GRACE_SECONDS
        if launch:
            _launch_vibenode()

    def note_start_request(self) -> None:
        """Called by the phone-side POST /start handler (serve thread)."""
        self._start_requested.set()

    # ---- main loop ----

    def run(self) -> None:
        _log("reviver up: web_port=%d control_port=%d pid=%d"
             % (WEB_PORT, CONTROL_PORT, os.getpid()))
        while True:
            # Feature switched off in the meantime -> retire cleanly.
            if not _mobile_enabled():
                _log("Mobile Command disabled — reviver exiting")
                with self._lock:
                    self._stop_serving_locked()
                return

            # Mutual resurrection: make sure our guardian is alive. If it died,
            # respawn it — so if WE die next, it can respawn us. This is what
            # guarantees a single-process death never leaves the phone without a
            # Start button while the machine is up.
            if not _something_listening(GUARDIAN_PORT):
                _log("guardian down — respawning it")
                _spawn_reviver_process(["--guardian"])

            # The phone pressed Start: yield + launch off the serve thread.
            if self._start_requested.is_set():
                self._start_requested.clear()
                _log("Start pressed on phone — launching VibeNode")
                self.yield_now(launch=True)

            with self._lock:
                state = self._state
                deadline = self._wait_deadline
                serving = self._serve_httpd is not None

            if state == "waiting":
                if _something_listening(WEB_PORT):
                    # Real server came up — step aside.
                    with self._lock:
                        self._state = "dormant"
                    _log("real VibeNode is up — going dormant")
                elif time.monotonic() > deadline:
                    # Start never completed — show the Start page again.
                    _log("start grace expired — re-showing Start page")
                    with self._lock:
                        self._state = "dormant"  # re-evaluated below
                # else: keep waiting quietly
            elif serving:
                # We are the Start page. If a real server somehow took 5050
                # anyway, our next bind attempt would fail — but normally we
                # hold it until a yield. Nothing to do here.
                pass
            else:
                # dormant / idle: is the real web server present?
                if not _something_listening(WEB_PORT):
                    self._start_serving()
                else:
                    with self._lock:
                        if self._state != "serving":
                            self._state = "dormant"

            time.sleep(_POLL_SECONDS)


# ---------------------------------------------------------------------------
# HTTP: the phone-facing Start page (served on 5050 only while VibeNode is down)
# ---------------------------------------------------------------------------

def _start_page(device: str) -> bytes:
    return f"""<!doctype html>
<html lang="en"><head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<title>{device} — VibeNode is stopped</title>
<!-- {_SENTINEL} -->
<style>
  :root {{ color-scheme: dark; }}
  * {{ box-sizing: border-box; }}
  html, body {{ height: 100%; margin: 0; }}
  body {{
    background: #0f1115; color: #e7e9ee;
    font: 16px/1.5 -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
    display: flex; align-items: center; justify-content: center;
    padding: max(24px, env(safe-area-inset-top)) 24px max(24px, env(safe-area-inset-bottom));
  }}
  .card {{ width: 100%; max-width: 420px; text-align: center; }}
  .dot {{ width: 12px; height: 12px; border-radius: 50%; background: #f0553b;
    display: inline-block; margin-right: 8px; box-shadow: 0 0 12px #f0553b88; }}
  .status {{ font-size: 14px; color: #9aa0ad; letter-spacing: .02em;
    text-transform: uppercase; margin-bottom: 18px; }}
  h1 {{ font-size: 24px; margin: 0 0 6px; }}
  .sub {{ color: #9aa0ad; margin: 0 0 32px; }}
  button {{
    -webkit-appearance: none; appearance: none; border: 0; cursor: pointer;
    width: 100%; padding: 18px 20px; border-radius: 14px;
    font-size: 18px; font-weight: 600; color: #fff;
    background: linear-gradient(180deg, #4f8cff, #2f6ff0);
    box-shadow: 0 8px 24px #2f6ff055; transition: transform .05s ease, opacity .2s;
  }}
  button:active {{ transform: translateY(1px) scale(.995); }}
  button[disabled] {{ opacity: .6; cursor: default; }}
  .hint {{ margin-top: 20px; font-size: 13px; color: #6c7280; }}
  .spin {{ width: 34px; height: 34px; border-radius: 50%;
    border: 3px solid #2a2f3a; border-top-color: #4f8cff;
    animation: r 0.9s linear infinite; margin: 0 auto 20px; }}
  @keyframes r {{ to {{ transform: rotate(360deg); }} }}
</style></head>
<body>
  <div class="card" id="card">
    <div class="status"><span class="dot"></span>Stopped</div>
    <h1>{device}</h1>
    <p class="sub">VibeNode isn't running on this computer.</p>
    <button id="go" onclick="start()">Start VibeNode</button>
    <div class="hint">Brings the server back up over your Tailscale link.</div>
  </div>
  <div class="card" id="booting" style="display:none">
    <div class="spin"></div>
    <h1>Starting VibeNode…</h1>
    <p class="sub" id="booting-sub">Booting up — this takes a few seconds.</p>
    <div class="hint" id="booting-elapsed">0s</div>
  </div>
<script>
  var _polling = false, _t0 = 0, _timer = null;
  function showBooting() {{
    document.getElementById('card').style.display = 'none';
    document.getElementById('booting').style.display = '';
    _t0 = Date.now();
    _timer = setInterval(function() {{
      document.getElementById('booting-elapsed').textContent =
        Math.floor((Date.now() - _t0) / 1000) + 's';
    }}, 1000);
  }}
  async function start() {{
    showBooting();                       // instant visual feedback
    try {{ await fetch('/start', {{ method: 'POST' }}); }} catch (e) {{}}
    poll();
  }}
  async function _readyCheck() {{
    // Advance ONLY when VibeNode is FULLY ready — web AND daemon. /api/health
    // distinguishes all three states cleanly:
    //   * us (reviver)           -> 503                 -> keep waiting
    //   * web booting, no daemon -> 200 {{daemon:false}} -> keep waiting (spinner)
    //   * fully ready            -> 200 {{daemon:true}}  -> navigate in
    // This is the fix for landing on a half-booted, unstyled app: we never hand
    // the phone in until the engine behind it is actually up.
    try {{
      const r = await fetch('/api/health?_=' + Date.now(), {{ cache: 'no-store' }});
      if (!r.ok) return;                 // reviver 503 / 502 mid-boot — wait
      const d = await r.json();
      if (d && d.daemon) location.href = '/';   // web + daemon both up
    }} catch (e) {{ /* connection refused mid-restart — keep polling */ }}
  }}
  function poll() {{
    if (_polling) return;                // never stack intervals
    _polling = true;
    setInterval(_readyCheck, 1500);
    // Mobile freezes timers when backgrounded; re-check the instant we return
    // so a phone parked here navigates in immediately once VibeNode is up,
    // instead of waiting for the next resumed tick (or a manual refresh).
    document.addEventListener('visibilitychange', () => {{
      if (document.visibilityState === 'visible') _readyCheck();
    }});
    window.addEventListener('pageshow', _readyCheck);
    window.addEventListener('focus', _readyCheck);
  }}
  poll();      // auto-recover on load — do not wait for a tap
  _readyCheck();   // and check once right now
</script>
</body></html>""".encode("utf-8")


def _starting_page(device: str) -> bytes:
    return f"""<!doctype html>
<html lang="en"><head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<title>Starting {device}…</title>
<!-- {_SENTINEL} -->
<style>
  :root {{ color-scheme: dark; }}
  html, body {{ height: 100%; margin: 0; }}
  body {{ background: #0f1115; color: #e7e9ee;
    font: 16px/1.5 -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
    display: flex; align-items: center; justify-content: center; text-align: center; padding: 24px; }}
  .spin {{ width: 34px; height: 34px; border-radius: 50%;
    border: 3px solid #2a2f3a; border-top-color: #4f8cff;
    animation: r 0.9s linear infinite; margin: 0 auto 20px; }}
  @keyframes r {{ to {{ transform: rotate(360deg); }} }}
  h1 {{ font-size: 20px; margin: 0 0 6px; }}
  p {{ color: #9aa0ad; margin: 0; }}
</style></head>
<body>
  <div>
    <div class="spin"></div>
    <h1>Starting VibeNode…</h1>
    <p>This can take a few seconds. You'll be taken in automatically.</p>
  </div>
<script>
  async function _readyCheck() {{
    try {{
      const r = await fetch('/api/health?_=' + Date.now(), {{ cache: 'no-store' }});
      if (!r.ok) return;                 // reviver 503 / 502 mid-boot — wait
      const d = await r.json();
      if (d && d.daemon) location.href = '/';   // web + daemon both up
    }} catch (e) {{ /* mid-restart — keep polling */ }}
  }}
  setInterval(_readyCheck, 1500);
  document.addEventListener('visibilitychange', () => {{
    if (document.visibilityState === 'visible') _readyCheck();
  }});
  window.addEventListener('pageshow', _readyCheck);
  window.addEventListener('focus', _readyCheck);
  _readyCheck();
</script>
</body></html>""".encode("utf-8")


class _ServeHandler(BaseHTTPRequestHandler):
    server_version = "VibeNodeReviver"

    def log_message(self, *args):  # silence default stderr logging
        pass

    def _send(self, body: bytes, status: int = 200, ctype: str = "text/html; charset=utf-8"):
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        try:
            self.wfile.write(body)
        except Exception:
            pass

    def do_GET(self):
        # API calls must FAIL, not receive our HTML. The loaded app polls
        # /api/ping to detect a dead backend; if we answered 200 + Start-page
        # HTML the app would think VibeNode is alive and never surface the
        # "server down" state. Return 503 for anything under /api/ so the app's
        # health check trips and reloads into our Start page.
        if self.path.split("?", 1)[0].startswith("/api/"):
            self._send(b'{"ok":false,"error":"vibenode-down","reviver":true}',
                       status=503, ctype="application/json")
            return
        device = _device_name()
        rev = getattr(self.server, "reviver", None)
        # If a start is already in flight, keep showing the spinner so a manual
        # refresh doesn't look like nothing happened.
        if rev is not None and rev._start_requested.is_set():
            self._send(_starting_page(device))
        else:
            self._send(_start_page(device))

    def do_POST(self):
        if self.path.rstrip("/") == "/start":
            rev = getattr(self.server, "reviver", None)
            if rev is not None:
                rev.note_start_request()
            # Respond immediately; the main loop performs the yield+launch off
            # this (serve) thread to avoid shutting down our own server here.
            self._send(_starting_page(_device_name()))
        else:
            self._send(b"not found", status=404, ctype="text/plain; charset=utf-8")


# ---------------------------------------------------------------------------
# HTTP: the loopback control server (singleton guard + /yield)
# ---------------------------------------------------------------------------

class _ControlServer(ThreadingHTTPServer):
    """Control server with an EXCLUSIVE bind so it doubles as the singleton
    guard. HTTPServer defaults ``allow_reuse_address = 1``; on Windows that sets
    SO_REUSEADDR, which lets a *second* process bind the same port (hijack)
    instead of failing — defeating the "second instance can't bind -> exits"
    guarantee. We force an exclusive bind so a duplicate reliably fails and the
    extra reviver retires."""

    allow_reuse_address = False

    def server_bind(self):
        if sys.platform == "win32":
            try:
                self.socket.setsockopt(
                    socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
            except (AttributeError, OSError):
                pass
        super().server_bind()


class _ControlHandler(BaseHTTPRequestHandler):
    server_version = "VibeNodeReviverControl"

    def log_message(self, *args):
        pass

    def _json(self, obj: dict, status: int = 200):
        body = json.dumps(obj).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        try:
            self.wfile.write(body)
        except Exception:
            pass

    def do_POST(self):
        rev = getattr(self.server, "reviver", None)
        if self.path.rstrip("/") == "/yield":
            # A real VibeNode is starting — release 5050 synchronously so its
            # run.py can bind it, but keep this reviver process alive.
            if rev is not None:
                rev.yield_now(launch=False)
            self._json({"ok": True, "yielded": True})
        else:
            self._json({"ok": False, "error": "unknown"}, status=404)

    def do_GET(self):
        rev = getattr(self.server, "reviver", None)
        state = getattr(rev, "_state", "unknown") if rev else "unknown"
        self._json({"ok": True, "service": "vibenode-reviver", "state": state,
                    "web_port": WEB_PORT})


def _standby_acquire_control() -> "_ControlServer | None":
    """OS-supervised copies (the systemd --user unit sets
    VIBENODE_REVIVER_STANDBY=1) must NOT exit when the control port is busy:
    with Restart=always, a clean exit is respawned every RestartSec forever
    while a session-spawned reviver owns the port — a permanent restart loop
    burning a python spawn every 5 seconds. Instead, wait quietly and take
    over the instant the owner dies. Still self-retiring: returns None (caller
    exits, and the unit self-unregisters) when Mobile Command is turned off.
    """
    _log("control port %d busy — standby mode (waiting to take over)"
         % CONTROL_PORT)
    while True:
        time.sleep(_POLL_SECONDS * 2)
        if not _mobile_enabled():
            _log("Mobile Command disabled — standby reviver exiting")
            return None
        try:
            return _ControlServer(("127.0.0.1", CONTROL_PORT), _ControlHandler)
        except OSError:
            continue


def run_guardian() -> int:
    """The guardian: a minimal sidecar whose only job is to respawn the reviver
    if it dies. The reviver likewise respawns the guardian — mutual resurrection
    with no single point of failure and no dependency on the OS scheduler. The
    guardian never binds the web port or serves anything; it just watches.
    """
    if not _mobile_enabled():
        return 0
    # Singleton + liveness beacon: hold the guardian port. A second guardian
    # fails this bind and exits.
    beacon = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    if sys.platform == "win32":
        try:
            beacon.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        except (AttributeError, OSError):
            pass
    try:
        beacon.bind(("127.0.0.1", GUARDIAN_PORT))
        beacon.listen(16)
    except OSError:
        _log("guardian port %d busy — another guardian running; exiting"
             % GUARDIAN_PORT)
        return 0

    # Drain probe connections so the listen backlog never fills (the reviver
    # checks liveness by connecting here).
    def _drain():
        while True:
            try:
                c, _ = beacon.accept()
                c.close()
            except OSError:
                return
    threading.Thread(target=_drain, name="guardian-accept", daemon=True).start()

    _log("guardian up: pid=%d watching reviver on %d" % (os.getpid(), CONTROL_PORT))
    while True:
        if not _mobile_enabled():
            _log("Mobile Command disabled — guardian exiting")
            return 0
        if not _something_listening(CONTROL_PORT):
            _log("reviver down — guardian respawning it")
            _spawn_reviver_process()
        time.sleep(2)


def main() -> int:
    # Guardian mode: run the sidecar that keeps the reviver alive.
    if "--guardian" in sys.argv[1:]:
        try:
            return run_guardian()
        except Exception as e:
            _log("guardian crashed: %s" % e)
            return 1

    # Cleanup mode: tear down the OS supervisor task and exit. Invoked by
    # mobile_command.disable() so turning the feature off removes the task
    # promptly instead of waiting for a running reviver to notice.
    if "--unregister" in sys.argv[1:]:
        unregister_supervisor()
        return 0

    # Only run for users who opted into Mobile Command. If it is off, also make
    # sure no stale supervisor task lingers (e.g. the task fired after the user
    # disabled the feature) — the reviver is self-uninstalling.
    if not _mobile_enabled():
        _log("Mobile Command not enabled — reviver not needed; exiting")
        unregister_supervisor()
        return 0

    # Singleton guard: whoever owns the control port is THE reviver.
    try:
        control = _ControlServer(("127.0.0.1", CONTROL_PORT), _ControlHandler)
    except OSError:
        if os.environ.get("VIBENODE_REVIVER_STANDBY"):
            control = _standby_acquire_control()
            if control is None:  # Mobile Command turned off while waiting
                return 0
        else:
            # Session-spawned copy (session_manager launches one per start):
            # exiting fast IS the singleton contract — nothing respawns us.
            _log("control port %d busy — another reviver is running; exiting"
                 % CONTROL_PORT)
            return 0

    # Ensure the always-on supervisor exists so we survive a broad kill / reboot.
    # Run it OFF the main path: OS calls (schtasks/launchctl/systemctl) can be
    # slow, and they must never delay the reviver's core duties (serving the
    # Start page, watching the guardian). Registration is a background nicety.
    threading.Thread(target=register_supervisor, name="reviver-register",
                     daemon=True).start()

    reviver = Reviver()
    control.reviver = reviver  # type: ignore[attr-defined]
    threading.Thread(target=control.serve_forever, name="reviver-control",
                     daemon=True).start()

    try:
        reviver.run()
    except KeyboardInterrupt:
        pass
    except Exception as e:
        _log("reviver crashed: %s" % e)
        return 1
    finally:
        try:
            control.shutdown()
        except Exception:
            pass

    # run() returns cleanly only when Mobile Command was switched off — in that
    # case retire the supervisor task too so nothing respawns us.
    if not _mobile_enabled():
        unregister_supervisor()
    return 0


if __name__ == "__main__":
    sys.exit(main())
