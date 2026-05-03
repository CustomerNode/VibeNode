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

# Now import and run the real app.
import runpy
runpy.run_path(str(_HERE / "run.py"), run_name='__main__')
