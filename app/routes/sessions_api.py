"""
Session CRUD routes -- list, view, rename, auto-name, delete, duplicate, continue, open.
"""

import json
import shutil
from datetime import datetime, timezone as tz
from pathlib import Path

from flask import Blueprint, current_app, jsonify, request

from ..config import (
    _sessions_dir,
    _load_names,
    _save_name,
    _delete_name,
    _remap_name,
    _decode_project,
    get_active_project,
    _summary_cache,
    _mark_deleted,
    _mark_deleted_bulk,
)
from ..sessions import load_session, load_session_timeline, all_sessions
from ..titling import smart_title

bp = Blueprint('sessions_api', __name__)


@bp.route("/api/sessions")
def api_sessions():
    sessions = all_sessions(summary_only=True)

    # Merge in SDK-managed sessions that haven't written a .jsonl yet.
    # Without this, sessions started via the GUI disappear on page refresh
    # until their first .jsonl flush (i.e. first response completes).
    existing_ids = {s["id"] for s in sessions}
    sm = current_app.session_manager
    names = _load_names()  # check _session_names.json for auto-named titles
    for state in sm.get_all_states():
        sid = state.get("session_id", "")
        if sid and sid not in existing_ids and state.get("state") != "stopped":
            saved_name = names.get(sid, "")
            title = saved_name or state.get("name") or "New Session"
            sessions.insert(0, {
                "id": sid,
                "display_title": title,
                "custom_title": saved_name or state.get("name") or "",
                "user_named": bool(saved_name),
                "date": "",
                "last_activity": "",
                "last_activity_ts": 0,
                "sort_ts": 0,
                "size": "",
                "file_bytes": 0,
                "message_count": 0,
                "preview": "",
            })

    return jsonify(sessions)


@bp.route("/api/resolve-session/<session_id>")
def api_resolve_session(session_id):
    """Resolve a session ID through SDK aliases (old client UUID -> new server UUID).

    Returns the canonical session ID. Used on page load to recover from
    ID remaps that happened before the browser refreshed.
    """
    sm = current_app.session_manager
    resolved = sm._resolve_id(session_id) if hasattr(sm, '_resolve_id') else session_id
    return jsonify({"id": resolved, "remapped": resolved != session_id})


@bp.route("/api/session/<session_id>")
def api_session(session_id):
    meta_only = request.args.get("meta_only") == "1"

    path = _sessions_dir() / f"{session_id}.jsonl"
    if not path.exists():
        # Check if it's an SDK-managed session with no .jsonl yet
        sm = current_app.session_manager
        if sm.has_session(session_id):
            if meta_only:
                saved_title = _load_names().get(session_id, "")
                return jsonify({
                    "id": session_id,
                    "display_title": saved_title or "New Session",
                    "custom_title": saved_title,
                })
            entries = sm.get_entries(session_id)
            state = sm.get_session_state(session_id) or "idle"
            saved_title = _load_names().get(session_id, "")
            return jsonify({
                "id": session_id,
                "display_title": saved_title or "New Session",
                "custom_title": saved_title,
                "date": "",
                "size": "0 B",
                "message_count": len(entries),
                "messages": [{"role": "user" if e.get("kind") == "user" else "assistant",
                              "content": e.get("text", ""),
                              "type": e.get("kind", "")} for e in entries],
                "preview": entries[0].get("text", "")[:100] if entries else "",
            })
        return jsonify({"error": "Not found"}), 404
    if meta_only:
        data = load_session(path)
        return jsonify({
            "id": data.get("id", session_id),
            "display_title": data.get("display_title", ""),
            "custom_title": data.get("custom_title", ""),
        })
    return jsonify(load_session(path))


@bp.route("/api/rename/<session_id>", methods=["POST"])
def api_rename(session_id):
    data = request.get_json(silent=True) or {}
    new_title = data.get("title", "").strip()
    if not new_title:
        return jsonify({"error": "Title cannot be empty"}), 400

    # Save to the persistent names store FIRST -- this always succeeds even if
    # the .jsonl doesn't exist yet (new sessions before first message).
    _save_name(session_id, new_title)

    # Also write to the .jsonl so Claude Code's own UI sees the name (if file exists)
    path = _sessions_dir() / f"{session_id}.jsonl"
    if path.exists():
        entry = json.dumps({"type": "custom-title", "customTitle": new_title, "sessionId": session_id})
        with open(path, "a", encoding="utf-8") as f:
            f.write("\n" + entry + "\n")

    return jsonify({"ok": True, "title": new_title})


@bp.route("/api/remap-name", methods=["POST"])
def api_remap_name():
    """Re-persist a user-set name under a new session ID after SDK remaps."""
    data = request.get_json(silent=True) or {}
    old_id = data.get("old_id", "").strip()
    new_id = data.get("new_id", "").strip()
    if not old_id or not new_id:
        return jsonify({"error": "old_id and new_id required"}), 400

    title = _remap_name(old_id, new_id)
    if not title:
        return jsonify({"ok": True, "skipped": True})

    # Write custom-title entry to the new .jsonl if it exists
    path = _sessions_dir() / f"{new_id}.jsonl"
    if path.exists():
        entry = json.dumps({"type": "custom-title", "customTitle": title, "sessionId": new_id})
        with open(path, "a", encoding="utf-8") as f:
            f.write("\n" + entry + "\n")

    return jsonify({"ok": True, "title": title})


@bp.route("/api/autonname/<session_id>", methods=["POST"])
def api_autoname(session_id):
    # Never override a name the user manually set
    existing = _load_names().get(session_id)
    if existing:
        return jsonify({"ok": True, "title": existing, "skipped": True,
                        "reason": "User-set name preserved"})

    # Accept prompt text directly (for immediate naming before JSONL exists)
    data = request.get_json(silent=True) or {}
    prompt_text = (data.get("prompt") or "").strip()
    messages = []
    if prompt_text:
        messages = [{"role": "user", "content": prompt_text}]
    else:
        # Fall back to reading from JSONL
        path = _sessions_dir() / f"{session_id}.jsonl"
        if not path.exists():
            from ..config import _CLAUDE_PROJECTS
            for d in _CLAUDE_PROJECTS.iterdir():
                if not d.is_dir() or d.name.startswith("subagents"):
                    continue
                candidate = d / f"{session_id}.jsonl"
                if candidate.exists():
                    path = candidate
                    break
        if not path.exists():
            return jsonify({"error": "Not found"}), 404

        session = load_session(path)
        messages = [m for m in session["messages"] if m.get("content")]

    # Fallback: if load_session found no messages with content, scan the raw
    # JSONL for user text (queue-operation entries, user role with message.content)
    if not messages:
        import json as json_mod
        try:
            with open(path, encoding="utf-8") as _f:
                for _line in _f:
                    _line = _line.strip()
                    if not _line:
                        continue
                    _entry = json_mod.loads(_line)
                    _t = _entry.get("type", "")
                    # queue-operation entries contain the original user prompt
                    if _t == "queue-operation" and _entry.get("content"):
                        messages.append({"role": "user", "content": _entry["content"]})
                        break
                    # user entries may have content in message.content blocks
                    if _entry.get("role") == "user":
                        mc = _entry.get("message", {}).get("content", [])
                        if isinstance(mc, list):
                            for block in mc:
                                if isinstance(block, dict) and block.get("type") == "text" and block.get("text"):
                                    messages.append({"role": "user", "content": block["text"]})
                                    break
                        if messages:
                            break
        except Exception:
            pass

    # Check if this is a re-evaluate request (session already has an auto-title)
    session = session if 'session' in dir() else {}
    path = path if 'path' in dir() else None
    old_title = session.get("custom_title", "") if isinstance(session, dict) else ""
    is_re_evaluate = data.get("re_evaluate", False) and old_title

    if not messages:
        all_s = all_sessions(summary_only=True)
        empty_count = sum(
            1 for s in all_s
            if (s.get("custom_title") or "").startswith("Empty Session")
            or (not s.get("custom_title") and s.get("message_count", 0) == 0)
        )
        title = f"Empty Session ({empty_count})"
        if path:
            entry = json.dumps({"type": "custom-title", "customTitle": title, "sessionId": session_id})
            with open(path, "a", encoding="utf-8") as f:
                f.write("\n" + entry + "\n")
        return jsonify({"ok": True, "title": title})

    try:
        title = smart_title(messages)

        # If re-evaluating, only update if the new title is meaningfully different
        if is_re_evaluate and title == old_title:
            return jsonify({"ok": True, "title": old_title, "skipped": True,
                            "reason": "Title unchanged"})

        # Persist title — write to JSONL if available, always save to names file
        if path and path.exists():
            entry = json.dumps({"type": "custom-title", "customTitle": title, "sessionId": session_id})
            with open(path, "a", encoding="utf-8") as f:
                f.write("\n" + entry + "\n")
        _save_name(session_id, title)

        return jsonify({"ok": True, "title": title, "renamed": is_re_evaluate})

    except Exception as e:
        return jsonify({"error": str(e)}), 500


@bp.route("/api/delete/<session_id>", methods=["DELETE"])
def api_delete(session_id):
    import os
    import signal
    import time

    path = _sessions_dir() / f"{session_id}.jsonl"
    folder = _sessions_dir() / session_id

    # Phase 1: Close the SDK session SYNCHRONOUSLY so the CLI subprocess is
    # fully dead before we remove the file (prevents it from being recreated).
    sm = current_app.session_manager
    if sm.has_session(session_id):
        sm.close_session_sync(session_id)
        sm.remove_session(session_id)

    # Phase 2: Kill any orphaned CLI process for this session.
    # Always attempt this, even after SDK close, as a safety net -- the SDK
    # close may time out or the process may have been spawned by an earlier
    # server instance that we have no handle to.
    try:
        from ..process_detection import _get_running_session_ids
        running = _get_running_session_ids()
        pid = running.get(session_id)
        if pid and pid > 0:
            try:
                os.kill(pid, signal.SIGTERM)
            except (OSError, ProcessLookupError):
                pass
            time.sleep(0.5)  # let process die
    except Exception:
        pass  # best-effort

    # Phase 3: Tombstone + delete the file.
    # Write the tombstone FIRST so all_sessions() hides this ID immediately,
    # even if a dying process recreates the .jsonl after we unlink it.
    _mark_deleted(session_id)

    if not path.exists():
        sm._save_registry_now()
        return jsonify({"ok": True})  # Already gone or never created

    # On Windows the CLI process may still hold the .jsonl open even after
    # Phase 1/2 — unlink raises PermissionError in that case.  Wrap in
    # try/except so the endpoint still returns ok; the tombstone written
    # above is the authoritative "this session is deleted" signal and
    # all_sessions() will hide it regardless of whether the file is gone.
    try:
        path.unlink()
    except PermissionError:
        pass  # tombstone will hide it; Phase 4 retries below
    if folder.exists() and folder.is_dir():
        try:
            shutil.rmtree(folder)
        except (PermissionError, OSError):
            pass

    # Phase 4: Sweep for file re-created by a dying process.
    # Without this, a subprocess that hasn't fully exited can write the
    # .jsonl back to disk right after we unlink it.
    time.sleep(0.3)
    if path.exists():
        try:
            path.unlink()
        except Exception:
            # Last resort: truncate the file so even if the tombstone
            # eventually expires, the session would appear empty and be
            # auto-cleaned on the next "delete empty" sweep.
            try:
                path.write_bytes(b"")
            except Exception:
                pass
    if folder.exists() and folder.is_dir():
        try:
            shutil.rmtree(folder)
        except Exception:
            pass

    # Evict from summary cache (all keys whose path matches)
    path_str = str(path)
    for key in [k for k in _summary_cache if k[0] == path_str]:
        del _summary_cache[key]

    # Clean up user-set name from persistent store
    _delete_name(session_id)

    # Force immediate registry save so recovery can't resurrect this session
    sm._save_registry_now()

    return jsonify({"ok": True})


@bp.route("/api/delete-all", methods=["DELETE"])
def api_delete_all():
    """Delete every session in the active workspace in one shot."""
    import os
    import signal
    import time
    from ..process_detection import _get_running_session_ids

    sd = _sessions_dir()
    sm = current_app.session_manager
    deleted = 0

    # Phase 1: close all SDK-managed sessions synchronously
    sids_to_delete = [f.stem for f in sd.glob("*.jsonl")]
    for sid in sids_to_delete:
        if sm.has_session(sid):
            sm.close_session_sync(sid)
            sm.remove_session(sid)

    # Phase 2: kill orphaned claude.exe processes that the session manager
    # doesn't know about (e.g. from previous server instances).  Without this,
    # the CLI subprocess is still alive and will re-create the .jsonl file
    # after we delete it.
    try:
        running = _get_running_session_ids()  # {sid: pid}
        for sid, pid in running.items():
            if pid > 0:  # positive PIDs are confirmed safe to kill
                try:
                    os.kill(pid, signal.SIGTERM)
                except (OSError, ProcessLookupError):
                    pass
        if running:
            time.sleep(0.5)  # let processes die
    except Exception:
        pass  # process detection failed -- continue with best-effort deletion

    # Phase 3: tombstone ALL session IDs, then delete files.
    # Tombstones go first so all_sessions() hides them immediately.
    all_sids = [f.stem for f in sd.glob("*.jsonl")]
    if all_sids:
        _mark_deleted_bulk(all_sids)

    for f in list(sd.glob("*.jsonl")):
        sid = f.stem
        f.unlink()
        folder = sd / sid
        if folder.exists() and folder.is_dir():
            shutil.rmtree(folder)
        _delete_name(sid)
        deleted += 1

    # Phase 4: sweep for files re-created by dying processes
    time.sleep(0.3)
    for f in list(sd.glob("*.jsonl")):
        try:
            f.unlink()
            folder = sd / f.stem
            if folder.exists() and folder.is_dir():
                shutil.rmtree(folder)
        except Exception:
            pass

    # Clear entire summary cache and force registry save
    _summary_cache.clear()
    sm._save_registry_now()

    return jsonify({"ok": True, "deleted": deleted})


@bp.route("/api/delete-empty", methods=["DELETE"])
def api_delete_empty():
    import os
    import signal
    import time

    sm = current_app.session_manager
    deleted = []
    deleted_paths = []  # track for Phase 4 sweep

    # First pass: identify empty sessions and tombstone them immediately
    empty_files = []
    for f in _sessions_dir().glob("*.jsonl"):
        s = load_session(f)
        if s.get("message_count", 0) == 0:
            empty_files.append(f)

    # Tombstone all empty session IDs BEFORE deleting anything
    if empty_files:
        _mark_deleted_bulk([f.stem for f in empty_files])

    # Second pass: close, kill, and delete
    for f in empty_files:
        sid = f.stem
        if sm.has_session(sid):
            sm.close_session_sync(sid)
            sm.remove_session(sid)

        # Kill any orphaned CLI process for this session
        try:
            from ..process_detection import _get_running_session_ids
            running = _get_running_session_ids()
            pid = running.get(sid)
            if pid and pid > 0:
                try:
                    os.kill(pid, signal.SIGTERM)
                except (OSError, ProcessLookupError):
                    pass
        except Exception:
            pass

        folder = _sessions_dir() / sid
        path_str = str(f)
        if f.exists():
            f.unlink()
        if folder.exists() and folder.is_dir():
            shutil.rmtree(folder)
        for key in [k for k in _summary_cache if k[0] == path_str]:
            del _summary_cache[key]
        _delete_name(sid)
        deleted.append(sid)
        deleted_paths.append((f, folder))

    # Sweep for files re-created by dying processes
    if deleted_paths:
        time.sleep(0.3)
        for f, folder in deleted_paths:
            try:
                if f.exists():
                    f.unlink()
            except Exception:
                pass
            try:
                if folder.exists() and folder.is_dir():
                    shutil.rmtree(folder)
            except Exception:
                pass

    if deleted:
        sm._save_registry_now()
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


@bp.route("/api/session-timeline/<session_id>")
def api_session_timeline(session_id):
    """Return lightweight message list for the fork/rewind timeline picker."""
    path = _sessions_dir() / f"{session_id}.jsonl"
    if not path.exists():
        # Check SDK-managed sessions with no .jsonl yet
        sm = current_app.session_manager
        if sm.has_session(session_id):
            return jsonify({"messages": [], "has_snapshots": False,
                            "title": "New Session",
                            "error": "This session is still in-memory (no .jsonl file yet). Try again after some messages have been exchanged."})
        return jsonify({"error": "Session not found. The .jsonl file does not exist at: " + str(path)}), 404
    return jsonify(load_session_timeline(path))


@bp.route("/api/fork/<session_id>", methods=["POST"])
def api_fork(session_id):
    """Create a new session containing only JSONL lines up to a given line number."""
    import uuid as uuid_mod

    src = _sessions_dir() / f"{session_id}.jsonl"
    if not src.exists():
        return jsonify({"error": "Not found"}), 404

    data = request.get_json(silent=True) or {}
    up_to_line = data.get("up_to_line")
    if not up_to_line or not isinstance(up_to_line, int):
        return jsonify({"error": "up_to_line is required"}), 400

    new_id = str(uuid_mod.uuid4())
    dst = _sessions_dir() / f"{new_id}.jsonl"

    lines_out = []
    line_num = 0
    original_title = session_id[:8]
    with open(src, encoding="utf-8") as f:
        for line in f:
            line_num += 1
            if line_num > up_to_line:
                break
            raw = line.strip()
            if not raw:
                continue
            try:
                obj = json.loads(raw)
            except Exception:
                lines_out.append(raw)
                continue
            if obj.get("type") == "custom-title":
                original_title = obj.get("customTitle", original_title)
            if "sessionId" in obj:
                obj["sessionId"] = new_id
            lines_out.append(json.dumps(obj))

    # Append a fork title
    fork_title = f"[fork] {original_title[:55]}"
    lines_out.append(json.dumps({
        "type": "custom-title",
        "customTitle": fork_title,
        "sessionId": new_id,
    }))

    with open(dst, "w", encoding="utf-8") as f:
        f.write("\n".join(lines_out) + "\n")

    return jsonify({"ok": True, "new_id": new_id, "title": fork_title})


@bp.route("/api/rewind/<session_id>", methods=["POST"])
def api_rewind(session_id):
    """Rewind tracked files to the state at a given message line number."""
    src = _sessions_dir() / f"{session_id}.jsonl"
    if not src.exists():
        return jsonify({"error": "Not found"}), 404

    data = request.get_json(silent=True) or {}
    up_to_line = data.get("up_to_line")
    if not up_to_line or not isinstance(up_to_line, int):
        return jsonify({"error": "up_to_line is required"}), 400

    # Collect edits AFTER the target line.  We'll reverse them to
    # reconstruct pre-edit file state.  This works without daemon
    # snapshots — the JSONL itself has old_string/new_string.
    edits_after = []  # [(file_path, old_string, new_string, tool_name)]
    msg_uuids_before = set()
    all_snapshots = []
    line_num = 0
    with open(src, encoding="utf-8") as f:
        for line in f:
            line_num += 1
            raw = line.strip()
            if not raw:
                continue
            try:
                obj = json.loads(raw)
            except Exception:
                continue
            t = obj.get("type", "")
            if t in ("user", "assistant") and line_num <= up_to_line:
                uid = obj.get("uuid", "")
                if uid:
                    msg_uuids_before.add(uid)
            if t == "assistant" and line_num > up_to_line:
                for blk in obj.get("message", {}).get("content", []):
                    if not isinstance(blk, dict) or blk.get("type") != "tool_use":
                        continue
                    tname = blk.get("name", "")
                    inp = blk.get("input", {})
                    fp = inp.get("file_path", "") or inp.get("path", "")
                    if not fp:
                        continue
                    if tname == "Edit":
                        edits_after.append((fp, inp.get("old_string", ""),
                                            inp.get("new_string", ""), "Edit"))
                    elif tname == "Write":
                        edits_after.append((fp, None, inp.get("content", ""), "Write"))
            if t == "file-history-snapshot":
                mid = obj.get("messageId", "")
                snap = obj.get("snapshot", {})
                inner_mid = snap.get("messageId", "")
                all_snapshots.append((mid, inner_mid, snap))

    # Also try snapshot-based restore (works when daemon wrote proper entries)
    merged_backups = {}
    for mid, inner_mid, snap in all_snapshots:
        if mid in msg_uuids_before or inner_mid in msg_uuids_before:
            for fp, binfo in snap.get("trackedFileBackups", {}).items():
                if isinstance(binfo, dict) and binfo.get("backupFileName"):
                    merged_backups[fp] = binfo

    if not edits_after and not merged_backups:
        return jsonify({"error": "No edits found after this message to rewind"}), 400

    active_project = get_active_project()
    proj_dir = _decode_project(active_project) if active_project else str(Path.home())

    restored = []
    skipped = []

    # Primary method: reverse edits from the JSONL (no daemon needed).
    # Process in REVERSE order so nested edits undo correctly.
    files_reversed = set()
    for fp, old_s, new_s, tname in reversed(edits_after):
        norm = fp.replace("\\", "/")
        target = Path(norm) if Path(norm).is_absolute() else Path(proj_dir) / norm
        if not target.exists():
            continue
        try:
            content = target.read_text(encoding="utf-8")
            if tname == "Edit" and old_s is not None and new_s:
                if new_s in content:
                    content = content.replace(new_s, old_s, 1)
                    target.write_text(content, encoding="utf-8")
                    files_reversed.add(fp)
            elif tname == "Write":
                # For Write, we need a backup or git — can't reverse
                pass
        except Exception:
            pass

    for fp in files_reversed:
        restored.append(fp)

    # Fallback: snapshot-based restore for files not handled by reversal
    if merged_backups:
        history_base = Path.home() / ".claude" / "file-history"
        candidate_dirs = [history_base / session_id]
        if history_base.is_dir():
            for d in sorted(history_base.iterdir(),
                            key=lambda x: x.stat().st_mtime, reverse=True):
                if d.is_dir() and d != candidate_dirs[0]:
                    candidate_dirs.append(d)

        for rel_path, backup_info in merged_backups.items():
            if rel_path in files_reversed:
                continue
            if not isinstance(backup_info, dict):
                continue
            backup_name = backup_info.get("backupFileName", "")
            if not backup_name:
                continue
            backup_path = None
            for d in candidate_dirs:
                p = d / backup_name
                if p.exists():
                    backup_path = p
                    break
            if not backup_path:
                skipped.append(rel_path)
                continue
            norm = rel_path.replace("\\", "/")
            target = Path(norm) if Path(norm).is_absolute() else Path(proj_dir) / norm
            try:
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(backup_path.read_bytes())
                restored.append(rel_path)
            except Exception:
                skipped.append(rel_path)

    return jsonify({
        "ok": True,
        "files_restored": restored,
        "files_skipped": skipped,
    })


@bp.route("/api/fork-rewind/<session_id>", methods=["POST"])
def api_fork_rewind(session_id):
    """Fork conversation AND rewind code to a given message."""
    import uuid as uuid_mod

    src = _sessions_dir() / f"{session_id}.jsonl"
    if not src.exists():
        return jsonify({"error": "Not found"}), 404

    data = request.get_json(silent=True) or {}
    up_to_line = data.get("up_to_line")
    if not up_to_line or not isinstance(up_to_line, int):
        return jsonify({"error": "up_to_line is required"}), 400

    # --- Fork ---
    new_id = str(uuid_mod.uuid4())
    dst = _sessions_dir() / f"{new_id}.jsonl"

    lines_out = []
    line_num = 0
    original_title = session_id[:8]
    merged_backups = {}

    with open(src, encoding="utf-8") as f:
        for line in f:
            line_num += 1
            if line_num > up_to_line:
                break
            raw = line.strip()
            if not raw:
                continue
            try:
                obj = json.loads(raw)
            except Exception:
                lines_out.append(raw)
                continue
            if obj.get("type") == "custom-title":
                original_title = obj.get("customTitle", original_title)
            if obj.get("type") == "file-history-snapshot":
                snap = obj.get("snapshot", {})
                for fp, binfo in snap.get("trackedFileBackups", {}).items():
                    if isinstance(binfo, dict) and binfo.get("backupFileName"):
                        merged_backups[fp] = binfo
                    elif fp not in merged_backups:
                        merged_backups[fp] = binfo
            if "sessionId" in obj:
                obj["sessionId"] = new_id
            lines_out.append(json.dumps(obj))

    fork_title = f"[fork+rewind] {original_title[:48]}"
    lines_out.append(json.dumps({
        "type": "custom-title",
        "customTitle": fork_title,
        "sessionId": new_id,
    }))

    with open(dst, "w", encoding="utf-8") as f:
        f.write("\n".join(lines_out) + "\n")

    # --- Rewind ---
    restored = []
    skipped = []
    if merged_backups:
        active_project = get_active_project()
        proj_dir = _decode_project(active_project) if active_project else str(Path.home())
        history_dir = Path.home() / ".claude" / "file-history" / session_id

        for rel_path, backup_info in merged_backups.items():
            if not isinstance(backup_info, dict):
                continue
            backup_name = backup_info.get("backupFileName", "")
            if not backup_name:
                continue
            backup_path = history_dir / backup_name
            if not backup_path.exists():
                skipped.append(rel_path)
                continue
            # Handle both absolute and relative paths
            norm = rel_path.replace("\\", "/")
            if Path(norm).is_absolute():
                target = Path(norm)
            else:
                target = Path(proj_dir) / norm
            try:
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(backup_path.read_bytes())
                restored.append(rel_path)
            except Exception:
                skipped.append(rel_path)

    return jsonify({
        "ok": True,
        "new_id": new_id,
        "title": fork_title,
        "files_restored": restored,
        "files_skipped": skipped,
    })


@bp.route("/api/open/<session_id>", methods=["POST"])
def api_open(session_id):
    """Open/resume a session via the SDK SessionManager."""
    path = _sessions_dir() / f"{session_id}.jsonl"
    if not path.exists():
        return jsonify({"error": "Not found"}), 404

    try:
        active_project = get_active_project()
        proj_dir = _decode_project(active_project) if active_project else str(Path.home())

        sm = current_app.session_manager

        # If already managed and running, just return ok
        if sm.has_session(session_id):
            state = sm.get_session_state(session_id)
            if state and state != "stopped":
                return jsonify({"ok": True, "already_running": True})

        result = sm.start_session(
            session_id=session_id,
            prompt="",
            cwd=proj_dir,
            name="",
            resume=True,
        )

        if result.get("ok"):
            return jsonify({"ok": True})
        return jsonify({"error": result.get("error", "Failed to open session")}), 500

    except Exception as e:
        return jsonify({"error": str(e)}), 500
