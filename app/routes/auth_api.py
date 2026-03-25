"""
Auth routes -- check Claude Code login status and trigger login flow.
"""

import json
import subprocess
import shutil

from flask import Blueprint, jsonify

bp = Blueprint('auth_api', __name__)

_claude_bin = shutil.which("claude") or "claude"


@bp.route("/api/auth-status")
def api_auth_status():
    """Return Claude Code auth status JSON."""
    try:
        result = subprocess.run(
            [_claude_bin, "auth", "status", "--json"],
            capture_output=True, text=True, timeout=10,
        )
        data = json.loads(result.stdout)
        return jsonify(data)
    except Exception as e:
        return jsonify({"loggedIn": False, "error": str(e)})


@bp.route("/api/auth-login", methods=["POST"])
def api_auth_login():
    """Kick off `claude auth login` in a visible terminal so the user can complete OAuth."""
    try:
        # Open login in a new visible terminal window so the user can interact
        subprocess.Popen(
            ["start", "cmd", "/c", _claude_bin, "auth", "login"],
            shell=True,
        )
        return jsonify({"ok": True})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500
