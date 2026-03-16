"""
Session CRUD routes — list, view, rename, auto-name, delete, duplicate, continue, open.
"""

import json
import shutil
import subprocess
from datetime import datetime, timezone as tz
from pathlib import Path

from flask import Blueprint, jsonify, request

from ..config import (
    _sessions_dir,
    _load_names,
    _save_name,
    _decode_project,
    get_active_project,
)
from ..sessions import load_session, all_sessions
from ..titling import smart_title

bp = Blueprint('sessions_api', __name__)


@bp.route("/api/sessions")
def api_sessions():
    return jsonify(all_sessions(summary_only=True))


@bp.route("/api/session/<session_id>")
def api_session(session_id):
    path = _sessions_dir() / f"{session_id}.jsonl"
    if not path.exists():
        return jsonify({"error": "Not found"}), 404
    return jsonify(load_session(path))


@bp.route("/api/rename/<session_id>", methods=["POST"])
def api_rename(session_id):
    path = _sessions_dir() / f"{session_id}.jsonl"
    if not path.exists():
        return jsonify({"error": "Not found"}), 404

    data = request.json
    new_title = data.get("title", "").strip()
    if not new_title:
        return jsonify({"error": "Title cannot be empty"}), 400

    # Save to the persistent names store -- this survives Claude Code's own auto-naming
    _save_name(session_id, new_title)

    # Also write to the .jsonl so Claude Code's own UI sees the name
    entry = json.dumps({"type": "custom-title", "customTitle": new_title, "sessionId": session_id})
    with open(path, "a", encoding="utf-8") as f:
        f.write("\n" + entry + "\n")

    return jsonify({"ok": True, "title": new_title})


@bp.route("/api/autonname/<session_id>", methods=["POST"])
def api_autoname(session_id):
    path = _sessions_dir() / f"{session_id}.jsonl"
    if not path.exists():
        return jsonify({"error": "Not found"}), 404

    # Never override a name the user manually set
    existing = _load_names().get(session_id)
    if existing:
        return jsonify({"ok": True, "title": existing, "skipped": True,
                        "reason": "User-set name preserved"})

    session = load_session(path)
    messages = [m for m in session["messages"] if m["content"]]

    if not messages:
        all_s = all_sessions(summary_only=True)
        empty_count = sum(
            1 for s in all_s
            if (s.get("custom_title") or "").startswith("Empty Session")
            or (not s.get("custom_title") and s.get("message_count", 0) == 0)
        )
        title = f"Empty Session ({empty_count})"
        entry = json.dumps({"type": "custom-title", "customTitle": title, "sessionId": session_id})
        with open(path, "a", encoding="utf-8") as f:
            f.write("\n" + entry + "\n")
        return jsonify({"ok": True, "title": title})

    try:
        title = smart_title(session["messages"])

        entry = json.dumps({"type": "custom-title", "customTitle": title, "sessionId": session_id})
        with open(path, "a", encoding="utf-8") as f:
            f.write("\n" + entry + "\n")

        return jsonify({"ok": True, "title": title})

    except Exception as e:
        return jsonify({"error": str(e)}), 500


@bp.route("/api/delete/<session_id>", methods=["DELETE"])
def api_delete(session_id):
    path = _sessions_dir() / f"{session_id}.jsonl"
    folder = _sessions_dir() / session_id

    if not path.exists():
        return jsonify({"error": "Not found"}), 404

    path.unlink()
    if folder.exists() and folder.is_dir():
        shutil.rmtree(folder)

    return jsonify({"ok": True})


@bp.route("/api/delete-empty", methods=["DELETE"])
def api_delete_empty():
    deleted = []
    for f in _sessions_dir().glob("*.jsonl"):
        s = load_session(f)
        if s.get("message_count", 0) == 0:
            folder = _sessions_dir() / f.stem
            f.unlink()
            if folder.exists() and folder.is_dir():
                shutil.rmtree(folder)
            deleted.append(f.stem)
    return jsonify({"ok": True, "deleted": len(deleted)})


@bp.route("/api/duplicate/<session_id>", methods=["POST"])
def api_duplicate(session_id):
    import uuid as uuid_mod
    src = _sessions_dir() / f"{session_id}.jsonl"
    if not src.exists():
        return jsonify({"error": "Not found"}), 404

    new_id = str(uuid_mod.uuid4())
    dst = _sessions_dir() / f"{new_id}.jsonl"

    # Copy file, rewriting sessionId in every line
    lines_out = []
    with open(src, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            obj = json.loads(line)
            if "sessionId" in obj:
                obj["sessionId"] = new_id
            lines_out.append(json.dumps(obj))

    with open(dst, "w", encoding="utf-8") as f:
        f.write("\n".join(lines_out) + "\n")

    return jsonify({"ok": True, "new_id": new_id})


@bp.route("/api/continue/<session_id>", methods=["POST"])
def api_continue(session_id):
    import uuid as uuid_mod

    src = _sessions_dir() / f"{session_id}.jsonl"
    if not src.exists():
        return jsonify({"error": "Not found"}), 404

    session = load_session(src)
    messages = session.get("messages", [])

    # Build context: topic from smart_title, last 6 exchanges for recent state
    topic = smart_title(messages)
    user_msgs = [m for m in messages if m.get("role") == "user" and m.get("content")]
    asst_msgs = [m for m in messages if m.get("role") == "assistant" and m.get("content")]

    # Recent exchanges (last 3 user + last 3 assistant, interleaved)
    recent = messages[-12:] if len(messages) > 12 else messages
    recent_text = "\n".join(
        f"{'User' if m['role']=='user' else 'Claude'}: {m['content'][:300]}"
        for m in recent if m.get("content")
    )

    # Key facts from early in the session (first 3 user messages)
    early_context = "\n".join(
        f"- {m['content'][:200]}"
        for m in user_msgs[:3]
    )

    handoff = (
        f"This is a continuation of a previous session that got too long.\n\n"
        f"**What we were working on:** {topic}\n\n"
        f"**Key context from the start of that session:**\n{early_context}\n\n"
        f"**Most recent exchanges:**\n{recent_text}\n\n"
        f"Please pick up right where we left off. "
        f"You have full context above \u2014 continue helping me with this work."
    )

    new_id = str(uuid_mod.uuid4())
    now = datetime.now(tz.utc).isoformat().replace("+00:00", "Z")
    msg_uuid = str(uuid_mod.uuid4())

    snapshot = {"type": "file-history-snapshot", "messageId": msg_uuid,
                "snapshot": {"messageId": msg_uuid, "trackedFileBackups": {}, "timestamp": now},
                "isSnapshotUpdate": False}
    active_project = get_active_project()
    user_entry = {"parentUuid": None, "isSidechain": False, "userType": "external",
                  "cwd": _decode_project(active_project).replace("/", "\\"),
                  "sessionId": new_id, "version": "2.1.71", "gitBranch": "main",
                  "type": "user", "message": {"role": "user", "content": handoff},
                  "uuid": msg_uuid, "timestamp": now}
    title_entry = {"type": "custom-title", "customTitle": f"[cont] {topic[:55]}", "sessionId": new_id}

    dst = _sessions_dir() / f"{new_id}.jsonl"
    with open(dst, "w", encoding="utf-8") as f:
        f.write(json.dumps(snapshot) + "\n")
        f.write(json.dumps(user_entry) + "\n")
        f.write(json.dumps(title_entry) + "\n")

    return jsonify({"ok": True, "new_id": new_id, "title": f"[cont] {topic[:55]}"})


@bp.route("/api/open/<session_id>", methods=["POST"])
def api_open(session_id):
    path = _sessions_dir() / f"{session_id}.jsonl"
    if not path.exists():
        return jsonify({"error": "Not found"}), 404
    try:
        active_project = get_active_project()
        proj_dir = _decode_project(active_project) if active_project else str(Path.home())
        subprocess.Popen(
            f'start cmd /k "cd /d {proj_dir} && claude --resume {session_id}"',
            shell=True
        )
        return jsonify({"ok": True})
    except Exception as e:
        return jsonify({"error": str(e)}), 500
