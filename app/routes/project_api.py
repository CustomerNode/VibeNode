"""
Project picker routes — list and switch active projects.
"""

from pathlib import Path

from flask import Blueprint, jsonify, request

from ..config import (
    _CLAUDE_PROJECTS,
    _decode_project,
    get_active_project,
    set_active_project,
)

bp = Blueprint('project_api', __name__)


@bp.route("/api/projects")
def api_projects():
    docs = str(Path.home() / "Documents").replace("\\", "/").lower()
    active_project = get_active_project()
    results = []
    for d in sorted(_CLAUDE_PROJECTS.iterdir()):
        if not d.is_dir() or d.name.startswith("subagents"):
            continue
        display = _decode_project(d.name)
        # Only show projects that live inside the user's Documents folder
        if not display.replace("\\", "/").lower().startswith(docs + "/"):
            continue
        count = sum(1 for _ in d.glob("*.jsonl"))
        results.append({
            "encoded": d.name,
            "display": display,
            "session_count": count,
            "active": d.name == active_project,
        })
    return jsonify(results)


@bp.route("/api/set-project", methods=["POST"])
def api_set_project():
    encoded = (request.get_json() or {}).get("project", "").strip()
    target = _CLAUDE_PROJECTS / encoded
    if not target.is_dir():
        return jsonify({"error": "Not found"}), 404
    set_active_project(encoded)
    return jsonify({"ok": True, "project": encoded, "display": _decode_project(encoded)})
