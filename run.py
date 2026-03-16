"""
Entry point for the ClaudeCodeGUI Flask application.
Run with: python run.py
Then open: http://localhost:5050
"""

import logging
import sys
import threading
import webbrowser

from app import create_app

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

    print("\n  ClaudeCodeGUI is running.\n"
          "  Open your browser to: http://localhost:5050\n\n"
          "  This is a local server for personal use.\n"
          "  Leave this window open while using ClaudeCodeGUI.\n"
          "  Close it or press Ctrl+C to stop.\n", flush=True)

    threading.Thread(target=open_browser, daemon=True).start()
    app.run(host="0.0.0.0", port=5050, debug=False, threaded=True)
