"""Weekly usage pace pop-ups: what is waiting, the project list, the two
actions (stop every session, start a spare-usage job).  The detection lives
in src/web/usage_pace.py."""

from __future__ import annotations

import uuid
from pathlib import Path

from flask import Blueprint, current_app, jsonify, request

from .. import usage_pace as up
from ..config import (_CLAUDE_PROJECTS, _SYSTEM_UTILITY_CWD,
                      _load_project_names, _save_name, _sessions_dir,
                      get_kanban_config)

bp = Blueprint("usage_pace_api", __name__)


def _emit(event, data):
    try:
        from .. import socketio
        socketio.emit(event, data)
    except Exception:
        pass


@bp.route("/api/usage-pace")
def usage_pace_pending():
    """Pop-ups fired but not answered yet, for a tab opened after the push.
    A pop-up whose Preferences switch is now off is not returned."""
    cfg = get_kanban_config() or {}
    return jsonify({"pending": [e for e in up.pending() if up.enabled(e.get("kind", ""), cfg)]})


@bp.route("/api/usage-pace/ack", methods=["POST"])
def usage_pace_ack():
    """One tab answered a pop-up: forget it and close it in every other tab."""
    kind = (request.get_json(silent=True) or {}).get("kind", "")
    if kind not in up.PREF_KEYS:
        return jsonify({"error": "unknown kind"}), 400
    up.ack(kind)
    _emit("usage_pace_cleared", {"kind": kind})
    return jsonify({"ok": True})


def _projects():
    """Busiest first, only real projects: under the home folder (as the
    project picker shows them) and never VibeNode's hidden utility project."""
    home = str(Path.home()).replace("\\", "/").lower() + "/"
    skip = str(Path(_SYSTEM_UTILITY_CWD)).replace("\\", "/").lower()
    out = []
    for p in up.busiest_projects(_CLAUDE_PROJECTS):
        c = p["cwd"].replace("\\", "/").lower()
        if c == skip or not c.startswith(home):
            continue
        out.append(p)
    return out


@bp.route("/api/usage-pace/projects")
def usage_pace_projects():
    """Projects ranked by sessions active in the last 30 days, busiest first."""
    names = _load_project_names()
    out = []
    for p in _projects():
        p["name"] = names.get(p["encoded"]) or Path(p["cwd"]).name
        out.append(p)
    return jsonify({"projects": out})


@bp.route("/api/usage-pace/stop-all", methods=["POST"])
def usage_pace_stop_all():
    """Stop every running session in every project, the same stop the
    sidebar's Sleep uses.  Returns the ids so tabs mark them user-stopped."""
    sm = current_app.session_manager
    closed = []
    for s in sm.get_all_states() or []:
        sid = s.get("session_id")
        if not sid or s.get("state") == "stopped":
            continue
        try:
            r = sm.close_session(sid)
        except Exception:
            continue
        if r.get("ok") or r.get("error") == "Session not found":
            closed.append(sid)
    _emit("usage_pace_stopped", {"session_ids": closed})
    return jsonify({"ok": True, "closed": closed})


@bp.route("/api/usage-pace/start", methods=["POST"])
def usage_pace_start():
    """Start a long autonomous session on a task in a chosen project."""
    data = request.get_json(silent=True) or {}
    task = (data.get("task") or "").strip()
    cwd = (data.get("cwd") or "").strip()
    if not task:
        return jsonify({"error": "task is required"}), 400
    known = {p["cwd"]: p["encoded"] for p in _projects()}
    if cwd not in known or not Path(cwd).is_dir():
        return jsonify({"error": "unknown project"}), 400
    new_id = str(uuid.uuid4())
    project = known[cwd]  # the project folder its sessions already live in
    path = _sessions_dir(project=project) / f"{new_id}.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    if not path.exists():
        path.write_text("", encoding="utf-8")
    name = (data.get("name") or task)[:60]
    _save_name(new_id, name, project=project)
    sm = current_app.session_manager
    r = sm.start_session(session_id=new_id, prompt=up.task_prompt(task),
                         cwd=cwd, name=name, resume=False)
    if not r.get("ok"):
        return jsonify({"error": r.get("error", "Failed to start session")}), 500
    return jsonify({"ok": True, "session_id": new_id, "project": project})
