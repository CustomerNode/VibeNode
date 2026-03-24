"""
Entry point for the ClaudeCodeGUI Flask application.
Run with: python run.py
Then open: http://localhost:5050

Architecture: The Web UI (this process) connects to a separate Session Daemon
that manages Claude Code SDK sessions. The daemon survives Web UI restarts,
so running sessions continue uninterrupted when you restart the server.
"""

import logging
import socket
import subprocess
import sys
import threading
import time
import webbrowser
from pathlib import Path

DAEMON_PORT = 5051


def ensure_daemon():
    """Make sure the session daemon is running. Start it if not."""
    # Try to connect
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.settimeout(1)
        sock.connect(("127.0.0.1", DAEMON_PORT))
        sock.close()
        print("  Session daemon already running on port %d" % DAEMON_PORT, flush=True)
        return
    except (ConnectionRefusedError, OSError):
        pass

    # Start daemon as a detached subprocess
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
        subprocess.Popen(
            [sys.executable, str(daemon_script)],
            cwd=str(daemon_script.parent.parent),
            creationflags=creation_flags,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
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


# Ensure daemon is running before creating the Flask app
ensure_daemon()

from app import create_app, socketio

app = create_app()


def open_browser():
    import time
    time.sleep(0.8)
    webbrowser.open("http://localhost:5050")


if __name__ == "__main__":
    # Suppress Flask/Werkzeug request logging and startup banner
    log = logging.getLogger("werkzeug")
    log.setLevel(logging.ERROR)
    cli = sys.modules.get("flask.cli")
    if cli:
        cli.show_server_banner = lambda *a, **k: None

    print("\n"
          "  =========================================================\n"
          "    CLAUDE CODE GUI RUNNING - KEEP THIS TERMINAL OPEN\n"
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

    threading.Thread(target=open_browser, daemon=True).start()
    socketio.run(app, host="0.0.0.0", port=5050, debug=False, allow_unsafe_werkzeug=True)
