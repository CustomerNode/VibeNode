"""
Git status and sync routes.
"""

from flask import Blueprint, jsonify, request

from ..git_ops import get_git_cache, refresh_if_idle, do_git_sync

bp = Blueprint('git_api', __name__)


@bp.route("/api/git-status")
def api_git_status():
    # Return cached result instantly; trigger a refresh in background
    refresh_if_idle()
    return jsonify(get_git_cache())


@bp.route("/api/git-sync", methods=["POST"])
def api_git_sync():
    action = (request.get_json() or {}).get("action", "both")
    result = do_git_sync(action)
    return jsonify(result)
