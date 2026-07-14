#!/usr/bin/env python3
"""VibeNode -- thin entrypoint. All logic lives in app/.

On Windows, when launched via pythonw.exe (no console), stdout/stderr are None.
We redirect them to a log file so nothing crashes on print().
Shows a boot splash so the user sees startup progress.
"""
import os
import sys
import tempfile
from pathlib import Path

# Lock down the working directory to this script's folder regardless
# of how/where the shortcut launches us.
_HERE = Path(__file__).resolve().parent
os.chdir(_HERE)

# ---------------------------------------------------------------------------
# Augment PATH so claude CLI is findable regardless of launch method.
# When launched via a .desktop file or pythonw.exe the login shell is not
# sourced, so nvm shims, npm-global bins, and Volta are absent from PATH.
# This runs once before any imports so auth_api.py's shutil.which("claude")
# resolves correctly and the daemon subprocess inherits the corrected PATH.
# ---------------------------------------------------------------------------
if sys.platform != "win32":
    _extra = [
        str(Path.home() / ".local" / "bin"),
        str(Path.home() / ".npm-global" / "bin"),
        str(Path.home() / ".npm" / "bin"),
        str(Path.home() / ".volta" / "bin"),
        "/opt/homebrew/bin",
        "/usr/local/bin",
    ]
    # nvm: resolve active version from NVM_BIN env var or ~/.nvm/alias/default
    _nvm_dir = Path.home() / ".nvm"
    if _nvm_dir.is_dir():
        _nvm_bin = os.environ.get("NVM_BIN", "")
        if _nvm_bin and os.path.isdir(_nvm_bin):
            _extra.append(_nvm_bin)
        else:
            try:
                _alias = (_nvm_dir / "alias" / "default").read_text(encoding="utf-8").strip().lstrip("v")
                if "/" in _alias:
                    _lts = _nvm_dir / "alias" / _alias
                    if _lts.exists():
                        _alias = _lts.read_text(encoding="utf-8").strip().lstrip("v")
                _nb = _nvm_dir / "versions" / "node" / ("v" + _alias) / "bin"
                if _nb.is_dir():
                    _extra.append(str(_nb))
            except Exception:
                pass
    _cur = os.environ.get("PATH", "")
    _add = [d for d in _extra if d not in _cur]
    if _add:
        os.environ["PATH"] = os.pathsep.join(_add) + os.pathsep + _cur

# pythonw.exe sets stdout and stderr to None — any print() would crash.
# Detect this and redirect to a log file.
if sys.stdout is None or sys.stderr is None:
    (_HERE / "logs").mkdir(exist_ok=True)
    _log = open(_HERE / "logs" / "_server.log", "a", encoding="utf-8")
    if sys.stdout is None:
        sys.stdout = _log
    if sys.stderr is None:
        sys.stderr = _log

# Spawn-mode probe — surfaces in logs/_server.log whether this process is
# running detached (pythonw on Windows, nohup/setsid on POSIX) or attached
# to a controlling terminal. The dead-window failure mode we fixed in
# launch.bat / launch.sh stops being silent here: if a future change ever
# regresses the launcher and the server ends up foregrounded again, this
# line tells anyone reading the log immediately.
try:
    import time as _time
    _spawn_facts = []
    _exe = (sys.executable or "").lower()
    _spawn_facts.append("exe=" + (Path(_exe).name if _exe else "?"))
    if sys.platform == "win32":
        # pythonw has no console; python.exe attaches one.
        _spawn_facts.append("mode=" + ("detached(pythonw)" if "pythonw" in _exe else "attached(python)"))
    else:
        try:
            _sid = os.getsid(0)
            _pgid = os.getpgrp()
            _detached = (_sid == os.getpid())
            _spawn_facts.append("mode=" + ("detached(setsid)" if _detached else "attached(tty)"))
            _spawn_facts.append("sid=%d pgid=%d pid=%d" % (_sid, _pgid, os.getpid()))
        except Exception:
            pass
    (_HERE / "logs").mkdir(exist_ok=True)
    with open(_HERE / "logs" / "_server.log", "a", encoding="utf-8") as _slog:
        _slog.write("[%s] session_manager spawn %s\n" % (
            _time.strftime("%Y-%m-%d %H:%M:%S"), " ".join(_spawn_facts)))
except Exception:
    pass


def _show_notification(title, message, icon_path=None):
    """Show a desktop notification. Best-effort, never crashes."""
    try:
        import subprocess
        import threading

        def _notify():
            try:
                if sys.platform == "win32":
                    ps = (
                        "[void][System.Reflection.Assembly]::LoadWithPartialName('System.Windows.Forms');"
                        "$n = New-Object System.Windows.Forms.NotifyIcon;"
                        "$n.Icon = [System.Drawing.SystemIcons]::Information;"
                        f"$n.BalloonTipTitle = '{title}';"
                        f"$n.BalloonTipText = '{message}';"
                        "$n.Visible = $true;"
                        "$n.ShowBalloonTip(3000);"
                        "Start-Sleep -Milliseconds 3500;"
                        "$n.Dispose();"
                    )
                    subprocess.Popen(
                        ["powershell", "-NoProfile", "-WindowStyle", "Hidden", "-Command", ps],
                        creationflags=subprocess.CREATE_NO_WINDOW,
                    )
                elif sys.platform == "darwin":
                    subprocess.Popen([
                        "osascript", "-e",
                        f'display notification "{message}" with title "{title}"',
                    ])
                elif sys.platform == "linux":
                    subprocess.Popen(["notify-send", title, message])
            except Exception:
                pass

        threading.Thread(target=_notify, daemon=True).start()
    except Exception:
        pass


def _launch_splash():
    """Launch the boot splash window as a subprocess.

    Creates a temp status file for IPC and exports its path via
    VIBENODE_BOOT_STATUS_FILE so run.py can write progress updates.
    Returns True on success.
    """
    try:
        import subprocess as _sp

        splash_script = _HERE / "app" / "boot_splash.py"
        if not splash_script.exists():
            return False

        # Pre-flight: the splash subprocess silently sys.exit(0) on ImportError
        # (boot_splash.py top of file). On Debian/Ubuntu, tkinter is a
        # separate apt package (python3-tk) and is missing by default. If we
        # can't import it here, the subprocess can't either — return False so
        # the notify-send fallback fires instead of the user seeing nothing.
        try:
            import tkinter  # noqa: F401
        except ImportError:
            return False

        # Create the status file that run.py will write to
        status_file = os.path.join(
            tempfile.gettempdir(),
            "vibenode_boot_%d.status" % os.getpid(),
        )
        with open(status_file, "w", encoding="utf-8"):
            pass  # create empty

        os.environ["VIBENODE_BOOT_STATUS_FILE"] = status_file

        # Prefer pythonw on Windows so the splash subprocess has no console
        exe = sys.executable
        if sys.platform == "win32":
            pythonw = Path(sys.executable).parent / "pythonw.exe"
            if pythonw.exists():
                exe = str(pythonw)

        kwargs = {}
        if sys.platform == "win32":
            kwargs["creationflags"] = _sp.CREATE_NO_WINDOW

        _sp.Popen([exe, str(splash_script), status_file], **kwargs)
        return True
    except Exception:
        return False


# Try the splash first; fall back to a simple OS notification.
# On Linux, if tkinter is the missing piece, surface the apt hint in the
# toast itself \u2014 otherwise the user just sees a generic "starting up" with
# no indication that the splash *would* work after one apt-get away.
if not _launch_splash():
    _msg = "Starting up\u2026"
    if sys.platform == "linux":
        try:
            import tkinter  # noqa: F401
        except ImportError:
            _msg = "Starting up\u2026 (install python3-tk for the boot splash)"
    _show_notification("VibeNode", _msg)

# ---------------------------------------------------------------------------
# Mobile Command reviver hook.
# The reviver (reviver.py) keeps a "Start VibeNode" page reachable from the
# phone (over the existing tailscale-serve -> 5050 mapping) whenever the web
# server is down, so a killed VibeNode is a one-tap fix from mobile instead of
# a dead link. See reviver.py for the full rationale.
#
# session_manager.py is the universal launch choke point — the desktop
# shortcut, launch.bat, launch.sh, and /api/restart all run it — so this is the
# one place that covers every start path. Two best-effort steps, only when
# Mobile Command is enabled (otherwise a no-op for normal users):
#   1. Tell any already-running reviver to release port 5050 NOW, so run.py can
#      bind it without a fight and WITHOUT the reviver process dying.
#   2. Make sure a reviver exists for next time (its own singleton guard makes
#      this idempotent — a duplicate spawn simply exits).
# ---------------------------------------------------------------------------
def _reviver_hook():
    try:
        cfg_path = os.environ.get("VIBENODE_CONFIG") or str(_HERE / "kanban_config.json")
        import json as _json
        with open(cfg_path, "r", encoding="utf-8") as _fh:
            _cfg = _json.load(_fh)
        if not _cfg.get("mobile_command_enabled", False):
            return  # feature off — nothing to do
    except Exception:
        return  # no/unreadable config — feature can't be on

    control_port = int(os.environ.get("VIBENODE_REVIVER_PORT", 0) or 5052)

    # 1. Yield: ask a live reviver to free 5050 before run.py binds it.
    try:
        import urllib.request
        req = urllib.request.Request(
            "http://127.0.0.1:%d/yield" % control_port, data=b"", method="POST")
        urllib.request.urlopen(req, timeout=2)  # noqa: S310 (loopback only)
    except Exception:
        pass  # no reviver running yet, or already yielded — both fine

    # 2. Ensure a reviver is running for next time (singleton-guarded).
    try:
        import subprocess
        reviver_script = _HERE / "reviver.py"
        if reviver_script.exists():
            exe = sys.executable
            if sys.platform == "win32":
                _pw = Path(sys.executable).parent / "pythonw.exe"
                if _pw.exists():
                    exe = str(_pw)
                subprocess.Popen(
                    [exe, str(reviver_script)],
                    cwd=str(_HERE),
                    creationflags=subprocess.CREATE_NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP,
                )
            else:
                subprocess.Popen(
                    [exe, str(reviver_script)],
                    cwd=str(_HERE),
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    start_new_session=True,
                )
    except Exception:
        pass  # best effort — never block startup


_reviver_hook()

# Now import and run the real app.
import runpy
runpy.run_path(str(_HERE / "run.py"), run_name='__main__')
