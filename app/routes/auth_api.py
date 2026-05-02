"""
Auth routes -- check Claude Code login status and trigger login flow.
"""

import json
import subprocess
import shutil
import sys

from flask import Blueprint, jsonify

bp = Blueprint('auth_api', __name__)

_claude_bin = shutil.which("claude") or "claude"

from ..platform_utils import NO_WINDOW as _NO_WINDOW


@bp.route("/api/auth-status")
def api_auth_status():
    """Return Claude Code auth status JSON."""
    try:
        result = subprocess.run(
            [_claude_bin, "auth", "status", "--json"],
            capture_output=True, text=True, timeout=10,
            creationflags=_NO_WINDOW,
        )
        data = json.loads(result.stdout)
        return jsonify(data)
    except Exception as e:
        return jsonify({"loggedIn": False, "error": str(e)})


@bp.route("/api/auth-login", methods=["POST"])
def api_auth_login():
    """Kick off `claude auth login` in a visible terminal so the user can complete OAuth."""
    try:
        if sys.platform == "win32":
            subprocess.Popen(
                ["start", "cmd", "/c", _claude_bin, "auth", "login"],
                shell=True,
            )
        elif sys.platform == "darwin":
            subprocess.Popen([
                "osascript", "-e",
                f'tell app "Terminal" to do script "{_claude_bin} auth login"',
            ])
        elif sys.platform == "linux":
            # Try common terminal emulators in order of popularity.
            # Each entry: [binary, <terminal args...>, _claude_bin, "auth", "login"]
            # Covers GNOME, KDE, XFCE, MATE, LXDE, tiling WMs, and GPU terminals.
            for term_cmd in [
                ["x-terminal-emulator", "-e", f"{_claude_bin} auth login"],
                ["gnome-terminal", "--", _claude_bin, "auth", "login"],
                ["konsole", "-e", _claude_bin, "auth", "login"],
                ["xfce4-terminal", "-e", f"{_claude_bin} auth login"],
                ["tilix", "-e", _claude_bin, "auth", "login"],
                ["mate-terminal", "-e", f"{_claude_bin} auth login"],
                ["lxterminal", "-e", f"{_claude_bin} auth login"],
                ["alacritty", "-e", _claude_bin, "auth", "login"],
                ["kitty", _claude_bin, "auth", "login"],
                ["wezterm", "start", "--", _claude_bin, "auth", "login"],
                ["xterm", "-e", _claude_bin, "auth", "login"],
            ]:
                try:
                    subprocess.Popen(term_cmd)
                    break
                except FileNotFoundError:
                    continue
            else:
                return jsonify({"ok": False, "error": "No terminal emulator found. Run 'claude auth login' in a terminal."}), 500
        return jsonify({"ok": True})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500
