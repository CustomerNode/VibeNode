#!/usr/bin/env python3
"""VibeNode -- thin entrypoint. All logic lives in app/.

On Windows, when launched via pythonw.exe (no console), stdout/stderr are None.
We redirect them to a log file so nothing crashes on print().
Shows a platform-specific notification so the user knows it's starting.
"""
import os
import sys
from pathlib import Path

# Lock down the working directory to this script's folder regardless
# of how/where the shortcut launches us.
_HERE = Path(__file__).resolve().parent
os.chdir(_HERE)

# pythonw.exe sets stdout and stderr to None — any print() would crash.
# Detect this and redirect to a log file.
if sys.stdout is None or sys.stderr is None:
    _log = open(_HERE / "_server.log", "a", encoding="utf-8")
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


_show_notification("VibeNode", "Starting up\u2026")

# Now import and run the real app.
import runpy
runpy.run_path(str(_HERE / "run.py"), run_name='__main__')
