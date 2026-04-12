"""
Git status and sync routes.
"""

import subprocess
import sys
from pathlib import Path

from ..platform_utils import NO_WINDOW as _NO_WINDOW

from flask import Blueprint, jsonify, request, Response

from ..git_ops import get_git_cache, refresh_if_idle, do_git_sync
from ..config import get_active_project, _decode_project, _sessions_dir, _CLAUDE_PROJECTS

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
    # Include the freshly-updated git status so the frontend can use it
    # directly instead of making a separate poll that might race.
    result["git_status"] = get_git_cache()
    return jsonify(result)


@bp.route("/api/git-scan")
def api_git_scan():
    """Run deterministic security scan on uncommitted/staged files."""
    from ..git_scanner import scan_staged_files
    return jsonify(scan_staged_files())


@bp.route("/api/git-scan-stream")
def api_git_scan_stream():
    """SSE endpoint — streams real file-by-file scan progress."""
    from ..git_scanner import scan_staged_files_stream

    def generate():
        for line in scan_staged_files_stream():
            yield f"data: {line}\n\n"

    return Response(generate(), mimetype='text/event-stream',
                    headers={'Cache-Control': 'no-cache', 'X-Accel-Buffering': 'no'})


@bp.route("/api/project-git-status")
def project_git_status():
    """Get git status for the active project directory (not the GUI app)."""
    try:
        # Resolve the active project's filesystem path
        proj = get_active_project()
        if proj:
            proj_path = Path(_decode_project(proj))
        else:
            sd = _sessions_dir(project=proj)
            if sd != _CLAUDE_PROJECTS:
                proj_path = Path(_decode_project(sd.name))
            else:
                proj_path = Path.cwd()

        if not proj_path.is_dir():
            return jsonify({"is_git": False, "error": "Project directory not found"})

        proj_str = str(proj_path)

        # Check if it's a git repo
        check = subprocess.run(
            ["git", "-C", proj_str, "rev-parse", "--is-inside-work-tree"],
            capture_output=True, text=True, timeout=5, creationflags=_NO_WINDOW,
        )
        if check.returncode != 0:
            return jsonify({"is_git": False, "project_path": proj_str})

        # Get current branch
        branch = ""
        try:
            br = subprocess.run(
                ["git", "-C", proj_str, "branch", "--show-current"],
                capture_output=True, text=True, timeout=5,
            )
            if br.returncode == 0:
                branch = br.stdout.strip()
        except Exception:
            pass

        # Get dirty file count
        dirty_count = 0
        try:
            st = subprocess.run(
                ["git", "-C", proj_str, "status", "--porcelain"],
                capture_output=True, text=True, timeout=5,
            )
            if st.returncode == 0:
                lines = [l for l in st.stdout.splitlines() if l.strip()]
                dirty_count = len(lines)
        except Exception:
            pass

        # Get recent commits
        recent_commits = []
        try:
            lg = subprocess.run(
                ["git", "-C", proj_str, "log", "--oneline", "-5"],
                capture_output=True, text=True, timeout=5,
            )
            if lg.returncode == 0:
                for line in lg.stdout.strip().splitlines():
                    line = line.strip()
                    if line:
                        parts = line.split(" ", 1)
                        recent_commits.append({
                            "hash": parts[0],
                            "message": parts[1] if len(parts) > 1 else "",
                        })
        except Exception:
            pass

        return jsonify({
            "is_git": True,
            "branch": branch,
            "dirty_count": dirty_count,
            "recent_commits": recent_commits,
            "project_path": proj_str,
        })

    except Exception as e:
        return jsonify({"is_git": False, "error": str(e)}), 500
