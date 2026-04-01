# this comment means nothing
# this comment also means nothing
"""
Entry point for the VibeNode Flask application.
Run with: python run.py
Then open: http://localhost:5050

Architecture: The Web UI (this process) connects to a separate Session Daemon
that manages Claude Code SDK sessions. The daemon survives Web UI restarts,
so running sessions continue uninterrupted when you restart the server.
"""

import logging
import os
import shutil
import socket
import subprocess

# ---------------------------------------------------------------------------
# Boot-splash status helper (must be defined early — before any boot phases)
# ---------------------------------------------------------------------------
def _update_boot_status(msg):
    """Write a status line for the boot splash window (if running).

    The splash subprocess polls the status file for lines like:
        STEP:cache      — activate a named step
        DONE            — all steps complete, close splash
        ERROR:<text>    — show error, keep splash open with dismiss button
    """
    _sf = os.environ.get("VIBENODE_BOOT_STATUS_FILE")
    if not _sf:
        return
    try:
        with open(_sf, "a", encoding="utf-8") as fh:
            fh.write(msg + "\n")
            fh.flush()
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Purge __pycache__ on every boot so code changes always take effect
# ---------------------------------------------------------------------------
_update_boot_status("STEP:cache")
for _cache_dir in __import__('pathlib').Path(__file__).parent.rglob('__pycache__'):
    shutil.rmtree(_cache_dir, ignore_errors=True)
import sys
import threading
import time
import webbrowser
from pathlib import Path

# Test mode: VIBENODE_TEST_PORT=5099 starts a separate instance on that port
# with no port killing, no singleton, no browser, no daemon dependency.
_TEST_PORT = int(os.environ.get("VIBENODE_TEST_PORT", 0))

DAEMON_PORT = int(os.environ.get("VIBENODE_DAEMON_PORT", 5051))

from app.singleton import acquire_web_singleton


def ensure_daemon():
    """Make sure the session daemon is running. Start it if not."""
    # Try to connect to an existing daemon
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.settimeout(1)
        sock.connect(("127.0.0.1", DAEMON_PORT))
        sock.close()
        print("  Session daemon already running on port %d" % DAEMON_PORT, flush=True)
        return
    except (ConnectionRefusedError, OSError):
        pass

    # Start daemon as a detached subprocess (daemon enforces its own singleton)
    daemon_script = Path(__file__).parent / "daemon" / "daemon_server.py"
    if not daemon_script.exists():
        print("  WARNING: daemon_server.py not found at %s" % daemon_script, flush=True)
        return

    try:
        # Windows: CREATE_NO_WINDOW so it doesn't pop up a console
        # Also CREATE_NEW_PROCESS_GROUP so it survives this process dying
        creation_flags = 0
        if sys.platform == "win32":
            creation_flags = (
                subprocess.CREATE_NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP
            )
        daemon_log = Path(__file__).parent / "logs" / "daemon_debug.log"
        daemon_log.parent.mkdir(exist_ok=True)
        _daemon_fh = open(daemon_log, "a")
        subprocess.Popen(
            [sys.executable, str(daemon_script)],
            cwd=str(daemon_script.parent.parent),
            creationflags=creation_flags,
            stdout=_daemon_fh,
            stderr=_daemon_fh,
        )
    except Exception as e:
        print("  WARNING: Could not start daemon: %s" % e, flush=True)
        return

    # Wait for it to be ready
    for _ in range(50):
        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            sock.settimeout(1)
            sock.connect(("127.0.0.1", DAEMON_PORT))
            sock.close()
            print("  Session daemon started on port %d" % DAEMON_PORT, flush=True)
            return
        except (ConnectionRefusedError, OSError):
            time.sleep(0.1)

    print("  WARNING: Daemon started but not responding on port %d" % DAEMON_PORT, flush=True)


# ---- Kill any stale processes on our ports before starting ----
def _kill_port(port):
    """Kill ALL processes listening on a port. Retries until clear."""
    import subprocess as _sp
    for attempt in range(5):
        try:
            killed_any = False
            seen_pids = set()

            if sys.platform == "win32":
                r = _sp.run(
                    ["netstat", "-ano"],
                    capture_output=True, text=True, timeout=5,
                    creationflags=_sp.CREATE_NO_WINDOW,
                )
                for line in r.stdout.splitlines():
                    if (":%d " % port) in line and "LISTENING" in line:
                        parts = line.split()
                        pid = int(parts[-1])
                        if pid > 0 and pid != os.getpid() and pid not in seen_pids:
                            seen_pids.add(pid)
                            _sp.run(["taskkill", "/PID", str(pid), "/F"],
                                    capture_output=True, timeout=5,
                                    creationflags=_sp.CREATE_NO_WINDOW)
                            print("  Killed stale process %d on port %d" % (pid, port), flush=True)
                            killed_any = True

            elif sys.platform == "darwin":
                r = _sp.run(
                    ["lsof", "-ti", ":%d" % port],
                    capture_output=True, text=True, timeout=5,
                )
                for pid_str in r.stdout.split():
                    try:
                        pid = int(pid_str)
                    except ValueError:
                        continue
                    if pid > 0 and pid != os.getpid() and pid not in seen_pids:
                        seen_pids.add(pid)
                        _sp.run(["kill", "-9", str(pid)],
                                capture_output=True, timeout=5)
                        print("  Killed stale process %d on port %d" % (pid, port), flush=True)
                        killed_any = True

            elif sys.platform == "linux":
                r = _sp.run(
                    ["lsof", "-ti", ":%d" % port],
                    capture_output=True, text=True, timeout=5,
                )
                for pid_str in r.stdout.split():
                    try:
                        pid = int(pid_str)
                    except ValueError:
                        continue
                    if pid > 0 and pid != os.getpid() and pid not in seen_pids:
                        seen_pids.add(pid)
                        _sp.run(["kill", "-9", str(pid)],
                                capture_output=True, timeout=5)
                        print("  Killed stale process %d on port %d" % (pid, port), flush=True)
                        killed_any = True

            if not killed_any:
                break
            time.sleep(0.5)
        except Exception:
            break

if not _TEST_PORT:
    _update_boot_status("STEP:ports")
    _kill_port(5050)
    # Only kill the daemon port on a cold start.  When the web server is
    # restarted via /api/restart with scope="web", it sets
    # VIBENODE_PRESERVE_DAEMON=1 so the living daemon (and all its active
    # sessions/CLI subprocesses) survive the restart.
    if os.environ.get("VIBENODE_PRESERVE_DAEMON") != "1":
        _kill_port(DAEMON_PORT)
    else:
        print("  VIBENODE_PRESERVE_DAEMON=1 -- skipping daemon port kill", flush=True)
    time.sleep(0.05)  # brief pause for ports to release

    # ---- Singleton gate: only one web server allowed ----
    if not acquire_web_singleton():
        # Mutex held but we just killed the ports — stale mutex. Proceed.
        print("  Stale singleton detected. Starting anyway.", flush=True)

# ---------------------------------------------------------------------------
# Self-healing desktop shortcut.
# After a git pull the shortcut may still point to py.exe (console flash) or
# python.exe instead of pythonw.exe (windowless).  Fix it on every launch so
# the user never has to think about it.
# ---------------------------------------------------------------------------
def _fix_shortcut():
    if sys.platform != "win32":
        return
    try:
        lnk_path = Path.home() / "Desktop" / "VibeNode.lnk"
        if not lnk_path.exists():
            return
        # Find pythonw.exe next to the running python.exe
        pythonw = Path(sys.executable).parent / "pythonw.exe"
        if not pythonw.exists():
            return
        script = Path(__file__).resolve().parent / "session_manager.py"
        icon = Path(__file__).resolve().parent / "static" / "claudecodegui.ico"
        # Read current shortcut target to see if it already points to pythonw
        # (avoid rewriting on every launch if already correct)
        ps_read = (
            "$ws = New-Object -ComObject WScript.Shell;"
            f"$lnk = $ws.CreateShortcut('{lnk_path}');"
            "Write-Output $lnk.TargetPath"
        )
        r = subprocess.run(
            ["powershell", "-NoProfile", "-Command", ps_read],
            capture_output=True, text=True, timeout=5,
            creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0,
        )
        current_target = r.stdout.strip().lower()
        if "pythonw" in current_target:
            return  # Already correct
        # Rewrite the shortcut
        ps_write = (
            "$ws = New-Object -ComObject WScript.Shell;"
            f"$lnk = $ws.CreateShortcut('{lnk_path}');"
            f"$lnk.TargetPath = '{pythonw}';"
            f"$lnk.Arguments = '\"{script}\"';"
            f"$lnk.WorkingDirectory = '{script.parent}';"
            f"$lnk.IconLocation = '{icon},0';"
            "$lnk.WindowStyle = 7;"
            "$lnk.Save()"
        )
        subprocess.Popen(
            ["powershell", "-NoProfile", "-Command", ps_write],
            creationflags=subprocess.CREATE_NO_WINDOW,
        )
    except Exception:
        pass  # Best effort — never block startup

# Run shortcut repair in background — it's best-effort and involves
# slow PowerShell calls that shouldn't block startup.
threading.Thread(target=_fix_shortcut, daemon=True).start()

# ---------------------------------------------------------------------------
# Dependency health check — auto-install missing packages at boot
# ---------------------------------------------------------------------------
def _check_dependencies():
    """Verify required Python packages are installed. Auto-install any missing."""
    # (package_import_name, pip_install_name)
    required = [
        ("flask", "flask"),
        ("flask_socketio", "flask-socketio"),
        ("anthropic", "anthropic"),
        ("supabase", "supabase"),
        ("pg8000", "pg8000"),
    ]
    missing = []
    for import_name, pip_name in required:
        try:
            __import__(import_name)
        except ImportError:
            missing.append(pip_name)

    if missing:
        print("  Installing missing packages: %s" % ", ".join(missing), flush=True)
        try:
            subprocess.run(
                [sys.executable, "-m", "pip", "install", "--quiet"] + missing,
                timeout=120,
                creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0,
            )
            print("  Packages installed successfully.", flush=True)
        except Exception as e:
            print("  WARNING: Could not install packages: %s" % e, flush=True)

_update_boot_status("STEP:deps")
_check_dependencies()

# Ensure daemon is running before creating the Flask app (skip in test mode)
if not _TEST_PORT:
    _update_boot_status("STEP:daemon")
    ensure_daemon()

_update_boot_status("STEP:server")
try:
    from app import create_app, socketio
    app = create_app()
except Exception as _init_err:
    _update_boot_status("ERROR:Failed to initialize server: %s" % _init_err)
    raise


def open_browser():
    import time
    import urllib.request
    url = "http://localhost:5050"
    # Wait until the server is actually accepting connections before opening
    for _ in range(60):
        try:
            urllib.request.urlopen(url, timeout=1)
            break
        except Exception:
            time.sleep(1)
    else:
        return  # server never came up, don't open a broken tab
    if sys.platform == "win32":
        chrome = r"C:\Program Files\Google\Chrome\Application\chrome.exe"
        try:
            subprocess.Popen([chrome, url])
        except FileNotFoundError:
            webbrowser.open(url)
    elif sys.platform == "darwin":
        try:
            subprocess.Popen(["open", url])
        except FileNotFoundError:
            webbrowser.open(url)
    elif sys.platform == "linux":
        try:
            subprocess.Popen(["xdg-open", url])
        except FileNotFoundError:
            webbrowser.open(url)
    else:
        webbrowser.open(url)


if __name__ == "__main__":
    # Suppress Flask/Werkzeug request logging and startup banner
    log = logging.getLogger("werkzeug")
    log.setLevel(logging.ERROR)
    cli = sys.modules.get("flask.cli")
    if cli:
        cli.show_server_banner = lambda *a, **k: None

    print("\n"
          "  =========================================================\n"
          "    VIBENODE RUNNING - KEEP THIS TERMINAL OPEN\n"
          "  =========================================================\n\n"
          "  Open your browser to: http://localhost:5050\n\n"
          "  This is a local server for personal use.\n"
          "  Close it or press Ctrl+C to stop.\n\n"
          "  ---------------------------------------------------------\n\n"
          "  Building something complex and need to sell it?\n\n"
          "  CustomerNode.com\n"
          "  Turns Complex Deals Into Executable Journeys.\n"
          "  One shared path from discovery to mutual success.\n"
          "  Guided for buyers. Repeatable for sellers.\n"
          "  Powered by First-Party AI(TM).\n\n"
          "  Developed by the team at customernode.com\n"
          "  MIT License | Copyright 2026 CustomerNode LLC\n\n"
          "  ---------------------------------------------------------\n",
          flush=True)

    _port = _TEST_PORT or 5050
    if not _TEST_PORT:
        _update_boot_status("STEP:browser")
        _update_boot_status("DONE")
        # Clean up the status file after the splash has had time to read DONE
        _sf = os.environ.get("VIBENODE_BOOT_STATUS_FILE")
        if _sf:
            def _cleanup_status_file(path):
                try:
                    os.unlink(path)
                except Exception:
                    pass
            threading.Timer(5.0, _cleanup_status_file, args=[_sf]).start()
        # Only open browser on cold start. On web restarts
        # (VIBENODE_PRESERVE_DAEMON=1) the user already has the tab open
        # and Socket.IO will auto-reconnect — no duplicate window needed.
        if os.environ.get("VIBENODE_PRESERVE_DAEMON") != "1":
            threading.Thread(target=open_browser, daemon=True).start()
    socketio.run(app, host="0.0.0.0", port=_port, debug=False, allow_unsafe_werkzeug=True)
