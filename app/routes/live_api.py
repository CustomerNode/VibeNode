"""
Live session routes -- log streaming, waiting detection, respond, close.
"""

import json
import subprocess

from flask import Blueprint, jsonify, request

from ..config import _sessions_dir
from ..process_detection import (
    _get_running_session_ids,
    _parse_waiting_state,
    _parse_session_kind,
    send_to_session,
)

bp = Blueprint('live_api', __name__)


@bp.route("/api/session-log/<session_id>")
def api_session_log(session_id):
    """Return structured log entries for the live terminal panel."""
    path = _sessions_dir() / f"{session_id}.jsonl"
    if not path.exists():
        return jsonify({"error": "Not found"}), 404
    since = int(request.args.get("since", 0))
    try:
        raw_lines = [l for l in path.read_text(encoding="utf-8").splitlines() if l.strip()]
    except Exception:
        return jsonify({"entries": [], "total_lines": 0})
    total = len(raw_lines)
    entries = []
    for raw in raw_lines[since:]:
        try:
            obj = json.loads(raw)
        except Exception:
            continue
        t = obj.get("type", "")
        if t in ("file-history-snapshot", "custom-title", "progress"):
            continue
        if t == "user":
            msg = obj.get("message", {})
            content = msg.get("content", "")
            if isinstance(content, str) and content.strip():
                entries.append({"kind": "user", "text": content.strip()[:2000]})
            elif isinstance(content, list):
                for block in content:
                    bt = block.get("type", "")
                    if bt == "text" and block.get("text", "").strip():
                        entries.append({"kind": "user", "text": block["text"].strip()[:2000]})
                    elif bt == "tool_result":
                        rc = block.get("content", "")
                        if isinstance(rc, list):
                            rt = " ".join(b.get("text", "") for b in rc if isinstance(b, dict) and b.get("type") == "text")
                        else:
                            rt = str(rc)
                        entries.append({
                            "kind": "tool_result",
                            "tool_use_id": block.get("tool_use_id", ""),
                            "text": rt[:600],
                            "is_error": bool(block.get("is_error"))
                        })
        elif t == "assistant":
            msg = obj.get("message", {})
            content = msg.get("content", "")
            if isinstance(content, str) and content.strip():
                entries.append({"kind": "asst", "text": content.strip()[:3000]})
            elif isinstance(content, list):
                for block in content:
                    bt = block.get("type", "")
                    if bt == "text" and block.get("text", "").strip():
                        entries.append({"kind": "asst", "text": block["text"].strip()[:3000]})
                    elif bt == "tool_use":
                        inp = block.get("input") or {}
                        if "command" in inp:
                            desc = inp["command"][:300]
                        elif "path" in inp:
                            desc = inp["path"]
                            if "content" in inp:
                                desc += f" (write {len(str(inp.get('content','')))} chars)"
                        elif "pattern" in inp:
                            desc = inp["pattern"][:200]
                        elif inp:
                            first_key = next(iter(inp))
                            desc = f"{first_key}: {str(inp[first_key])[:200]}"
                        else:
                            desc = ""
                        entries.append({
                            "kind": "tool_use",
                            "id": block.get("id", ""),
                            "name": block.get("name", ""),
                            "desc": desc
                        })
    return jsonify({"entries": entries, "total_lines": total})


@bp.route("/api/waiting")
def api_waiting():
    """Return all running sessions with kind: 'question' | 'working' | 'idle'."""
    running = _get_running_session_ids()
    current_dir = _sessions_dir()
    result = []
    for sid, raw_pid in running.items():
        path = current_dir / f"{sid}.jsonl"
        if not path.exists():
            continue  # session belongs to a different project
        apid = abs(raw_pid)
        safe = raw_pid > 0  # positive = UUID confirmed, safe to kill
        state = _parse_waiting_state(path)
        if state is not None:
            result.append({"id": sid, "pid": apid, "safe": safe,
                           "question": state["question"],
                           "options":  state["options"],
                           "kind":     "question"})
        else:
            kind = _parse_session_kind(path)
            result.append({"id": sid, "pid": apid, "safe": safe,
                           "question": None, "options": None, "kind": kind})
    return jsonify(result)


@bp.route("/api/respond/<session_id>", methods=["POST"])
def api_respond(session_id):
    """Send text to a waiting Claude session."""
    data = request.get_json() or {}
    text = (data.get("text") or "").strip()
    if not text:
        return jsonify({"error": "No text provided"}), 400

    running = _get_running_session_ids()
    raw_pid = running.get(session_id)
    pid = abs(raw_pid) if raw_pid else None

    if pid:
        result = send_to_session(pid, text)
        return jsonify(result)

    # Session not running — tell the client so it can resume
    return jsonify({"ok": False, "method": "not_running"})


@bp.route("/api/close/<session_id>", methods=["POST"])
def api_close_session(session_id):
    """Terminate the running Claude process and its parent cmd window."""
    # SAFETY: Only close sessions where we can verify the UUID in the command line
    # AND the session belongs to the current project.
    current_dir = _sessions_dir()
    if not (current_dir / f"{session_id}.jsonl").exists():
        return jsonify({"ok": False, "error": "Session not in current project"})
    running = _get_running_session_ids()
    pid = running.get(session_id)
    if not pid:
        return jsonify({"ok": False, "error": "Session not running"})
    if pid < 0:
        return jsonify({"ok": False, "error": "Cannot close \u2014 session was not launched from GUI. Close it from its terminal instead."})
    try:
        # Get parent PID before killing (wmic query while process still exists)
        parent_pid = None
        try:
            r = subprocess.run(
                ["wmic", "process", "where", f"ProcessId={pid}", "get", "ParentProcessId"],
                capture_output=True, text=True, timeout=5)
            lines = [l.strip() for l in r.stdout.strip().splitlines() if l.strip() and not l.strip().startswith("Parent")]
            if lines:
                parent_pid = int(lines[0])
        except Exception:
            pass
        # Kill the Claude process
        subprocess.run(["taskkill", "/F", "/PID", str(pid)], capture_output=True, timeout=5)
        # Kill the parent cmd window if it exists
        if parent_pid:
            try:
                subprocess.run(["taskkill", "/F", "/PID", str(parent_pid)], capture_output=True, timeout=5)
            except Exception:
                pass
        return jsonify({"ok": True})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)})
