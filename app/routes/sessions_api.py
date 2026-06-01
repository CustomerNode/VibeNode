"""
Session CRUD routes -- list, view, rename, auto-name, delete, duplicate, continue, open.
"""

import json
import logging
import shutil
import sys
import time as _time
from datetime import datetime, timezone as tz
from pathlib import Path

from flask import Blueprint, current_app, jsonify, request

from ..config import (
    _CLAUDE_PROJECTS,
    _sessions_dir,
    _load_names,
    _names_file,
    _save_name,
    _delete_name,
    _remap_name,
    _decode_project,
    get_active_project,
    cwd_matches_active_project,
    _summary_cache,
    _mark_deleted,
    _mark_deleted_bulk,
    _unmark_deleted,
    move_to_trash,
    list_trash,
    restore_from_trash,
    purge_from_trash,
    _get_utility_ids,
    _resolve_remapped_id,
    _load_remaps,
    _record_session_access,
)
from ..sessions import load_session, load_session_timeline, all_sessions
from ..titling import smart_title

bp = Blueprint('sessions_api', __name__)
log = logging.getLogger(__name__)


# Windows + AV/Defender hold the .jsonl handle for tens to hundreds of
# milliseconds after the CLI subprocess dies. The original 4-phase delete
# handled this with a single 0.3 s retry then silently fell through; that
# silent-pass produced "0-byte JSONL on disk" forensic surprises (see
# docs/plans/phantom-sessions-fix-spec.md LEAK C). ``_unlink_with_retry``
# replaces the single retry with a bounded loop: ``retries=5`` attempts at
# ``delay=0.2`` s each gives a worst-case ~1 s wait — long enough for AV to
# release the handle in practice. Callers in all three delete endpoints
# (api_delete, api_delete_all, api_delete_empty) use this helper so the
# retry behaviour is uniform.
def _unlink_with_retry(path: Path, retries: int = 5, delay: float = 0.2) -> bool:
    """Attempt to unlink *path*, retrying on Windows file-lock errors.

    Returns ``True`` if the file is gone (either unlinked or never existed),
    ``False`` if all retries were exhausted while the file remained locked.
    On exhaustion we log at WARNING with the path so AV/Defender holds are
    visible in production logs.
    """
    for attempt in range(retries):
        try:
            if not path.exists():
                return True
            path.unlink()
            return True
        except PermissionError:
            if attempt < retries - 1:
                _time.sleep(delay)
                continue
            log.warning("Unable to unlink %s after %d retries (file locked?)",
                        path, retries)
            return False
        except FileNotFoundError:
            return True
        except Exception as e:
            log.debug("_unlink_with_retry: %s -> %s", path, e)
            return False
    return False


def _latest_custom_title_in_jsonl(path: Path) -> "str | None":
    """Return the customTitle of the most recent ``custom-title`` entry in
    the JSONL at *path*, or ``None`` if there is no such entry.

    Read backwards in 8 KiB chunks so this stays O(rename history) on
    multi-megabyte session files instead of scanning the whole file. Used
    by api_rename / api_autoname to skip an append when the title is
    already the current one — without dedup, every UI rename/autoname
    call pushed another identical ``custom-title`` line onto disk. One
    Aras session in production accumulated 52 copies of the same title.
    """
    if not path or not path.exists():
        return None
    try:
        with open(path, "rb") as f:
            f.seek(0, 2)
            pos = f.tell()
            if pos == 0:
                return None
            chunk_size = 8192
            tail = b""
            while pos > 0:
                read = min(chunk_size, pos)
                pos -= read
                f.seek(pos)
                tail = f.read(read) + tail
                lines = tail.split(b"\n")
                # Stash the first (possibly partial) line for the next iter;
                # process the rest from end to start.
                if pos > 0:
                    tail = lines[0]
                    candidates = lines[1:]
                else:
                    tail = b""
                    candidates = lines
                for raw in reversed(candidates):
                    raw = raw.strip()
                    if not raw:
                        continue
                    try:
                        obj = json.loads(raw)
                    except Exception:
                        continue
                    if obj.get("type") == "custom-title":
                        return obj.get("customTitle", "") or ""
    except Exception:
        return None
    return None


def _append_custom_title_if_changed(path: Path, title: str, session_id: str) -> bool:
    """Append ``{"type":"custom-title", "customTitle": title, ...}`` to *path*
    only when the latest custom-title in the file isn't already *title*.

    Returns ``True`` if a line was appended, ``False`` if the append was
    skipped because the title matches the current one. Callers that need
    to know whether the file was touched can use the return value; most
    callers just want the side-effect.
    """
    if not path or not path.exists():
        return False
    current = _latest_custom_title_in_jsonl(path)
    if current == title:
        return False
    entry = json.dumps({"type": "custom-title", "customTitle": title,
                        "sessionId": session_id})
    with open(path, "a", encoding="utf-8") as f:
        f.write("\n" + entry + "\n")
    return True


@bp.route("/api/sessions")
def api_sessions():
    # ── Extract project from client request ──
    # The client passes ?project=<encoded> so the server uses the correct
    # project without mutating the global _active_project.
    project = request.args.get("project", "").strip()
    if project and not (_CLAUDE_PROJECTS / project).is_dir():
        project = get_active_project()

    sessions = all_sessions(summary_only=True, project=project)

    # Merge in SDK-managed sessions that haven't written a .jsonl yet.
    # Without this, sessions started via the GUI disappear on page refresh
    # until their first .jsonl flush (i.e. first response completes).
    sm = current_app.session_manager

    # Resolve aliased (pre-remap) session IDs so JSONL-based sessions
    # appear under their canonical (SDK-assigned) ID, preventing duplicates
    # with daemon stubs that use the new ID.
    # Merge in-memory aliases with disk-persisted remaps so resolution
    # still works after a web restart (when _id_aliases is empty).
    aliases = dict(sm._id_aliases) if hasattr(sm, '_id_aliases') else {}
    for old_id, entry in _load_remaps(project).items():
        if old_id not in aliases and entry.get("new_id"):
            aliases[old_id] = entry["new_id"]

    if aliases:
        for s in sessions:
            new_id = aliases.get(s["id"])
            if new_id:
                s["id"] = new_id

    # Always deduplicate — keeps the first (richer JSONL-based) entry
    # when both an old and new ID resolve to the same canonical ID.
    deduped = []
    _seen = set()
    for s in sessions:
        if s["id"] not in _seen:
            _seen.add(s["id"])
            deduped.append(s)
    sessions = deduped

    # Final pass: filter out any sessions whose resolved ID is a utility session
    utility_ids = _get_utility_ids(project)
    sessions = [s for s in sessions if s["id"] not in utility_ids]

    existing_ids = {s["id"] for s in sessions}
    names = _load_names(project)  # check _session_names.json for auto-named titles
    # Subsessions (spec §4.4): build a sid -> daemon-state lookup so the
    # disk-sourced session list can be decorated with the parent pointer
    # + inbox-dirty flag.  Without this the sidebar can't render the
    # tree variant for closed-but-still-managed parents.
    _state_by_sid = {}
    for state in sm.get_all_states():
        sid = state.get("session_id", "")
        if state.get("session_type") in ("planner", "title"):
            continue
        if sid.startswith("_"):
            continue
        # ── Project isolation: skip sessions belonging to other projects ──
        state_cwd = state.get("cwd", "")
        if state_cwd and not cwd_matches_active_project(state_cwd, project=project):
            continue
        _state_by_sid[sid] = state
        if sid and sid not in existing_ids and state.get("state") != "stopped":
            saved_name = names.get(sid, "")
            title = saved_name or state.get("name") or "New Session"
            # Live sessions with no .jsonl yet get effective_ts=now so they
            # surface at the top of the date-sorted sidebar — they're the
            # most-recently-interacted-with thing in the project.
            _now_ts = _time.time()
            new_entry = {
                "id": sid,
                "display_title": title,
                "custom_title": saved_name or state.get("name") or "",
                "user_named": bool(saved_name),
                "date": "",
                "last_activity": "",
                "last_activity_ts": 0,
                "effective_ts": _now_ts,
                "sort_ts": 0,
                "size": "",
                "file_bytes": 0,
                "message_count": 0,
                "preview": "",
            }
            # Subsessions decoration — same fields as the disk-side merge below.
            if state.get("parent_session_id"):
                new_entry["parent_session_id"] = state["parent_session_id"]
                new_entry["subsession_origin_turn"] = state.get(
                    "subsession_origin_turn", 0
                )
            if state.get("session_type") == "subsession":
                new_entry["session_type"] = "subsession"
            if state.get("inbox_dirty"):
                new_entry["inbox_dirty"] = True
            sessions.insert(0, new_entry)

    # Decorate disk-sourced sessions with daemon-side subsession metadata
    # (parent_session_id, inbox_dirty, session_type=subsession).  Closed
    # sessions that aren't daemon-managed don't get decorated and render
    # as plain top-level rows — same as today's behaviour.
    for s in sessions:
        st = _state_by_sid.get(s["id"])
        if not st:
            continue
        if st.get("parent_session_id") and "parent_session_id" not in s:
            s["parent_session_id"] = st["parent_session_id"]
            s["subsession_origin_turn"] = st.get(
                "subsession_origin_turn", 0
            )
        if st.get("session_type") == "subsession" and s.get("session_type") != "subsession":
            s["session_type"] = "subsession"
        if st.get("inbox_dirty"):
            s["inbox_dirty"] = True

    return jsonify(sessions)


@bp.route("/api/resolve-session/<session_id>")
def api_resolve_session(session_id):
    """Resolve a session ID through SDK aliases (old client UUID -> new server UUID).

    Returns the canonical session ID. Used on page load to recover from
    ID remaps that happened before the browser refreshed.
    Checks in-memory aliases first, then falls back to the persisted
    remap file so resolution works even before aliases are synced.
    """
    project = request.args.get("project", "").strip()
    sm = current_app.session_manager
    resolved = sm._resolve_id(session_id) if hasattr(sm, '_resolve_id') else session_id
    if resolved == session_id:
        # In-memory alias not found — check disk-persisted remaps
        disk_resolved = _resolve_remapped_id(session_id, project)
        if disk_resolved:
            resolved = disk_resolved
    return jsonify({"id": resolved, "remapped": resolved != session_id})


@bp.route("/api/session/<session_id>")
def api_session(session_id):
    meta_only = request.args.get("meta_only") == "1"
    project = request.args.get("project", "").strip()

    # If this ID was remapped (old temp UUID -> real SDK UUID), redirect to
    # the canonical ID so stale cached references never serve a ghost session.
    canonical = _resolve_remapped_id(session_id, project)
    if canonical:
        session_id = canonical

    # Record this read as an interaction so the sidebar's date sort bubbles
    # the session up.  meta_only requests come from background widgets
    # (live-panel existence checks, etc.) — they should NOT count as a
    # user-initiated open, or every page render would touch every session.
    if not meta_only:
        _record_session_access(session_id, project)

    path = _sessions_dir(project) / f"{session_id}.jsonl"
    if not path.exists():
        # Check if it's an SDK-managed session with no .jsonl yet
        sm = current_app.session_manager
        if sm.has_session(session_id):
            if meta_only:
                saved_title = _load_names(project).get(session_id, "")
                return jsonify({
                    "id": session_id,
                    "display_title": saved_title or "New Session",
                    "custom_title": saved_title,
                })
            entries = sm.get_entries(session_id)
            state = sm.get_session_state(session_id) or "idle"
            saved_title = _load_names(project).get(session_id, "")
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


@bp.route("/api/session/<session_id>/touch", methods=["POST"])
def api_session_touch(session_id):
    """Explicit "I interacted with this session" signal from the UI.

    The sidebar sort uses ``effective_ts = max(last_msg_ts, mtime, access_ts)``
    and ``api_session`` records access on GET, but a session that was opened
    before the page loaded (or restored from a cached view) won't fire that
    GET.  This endpoint lets the JS bump the timestamp explicitly — e.g.,
    when the user clicks a session that's already mounted, or when a live
    panel takes focus.

    Always returns 200; the access store is best-effort sort state, not
    load-bearing data.
    """
    project = request.args.get("project", "").strip()
    canonical = _resolve_remapped_id(session_id, project)
    if canonical:
        session_id = canonical
    _record_session_access(session_id, project)
    return jsonify({"ok": True})


@bp.route("/api/rename/<session_id>", methods=["POST"])
def api_rename(session_id):
    data = request.get_json(silent=True) or {}
    new_title = data.get("title", "").strip()
    project = (data.get("project") or request.args.get("project", "")).strip()
    if not new_title:
        return jsonify({"error": "Title cannot be empty"}), 400

    # Save to the persistent names store FIRST -- this always succeeds even if
    # the .jsonl doesn't exist yet (new sessions before first message).
    _save_name(session_id, new_title, project)

    # Also write to the .jsonl so Claude Code's own UI sees the name (if file exists).
    # Dedup so renaming a session to the same name (a no-op the UI can fire
    # on focus-out) doesn't keep appending identical custom-title lines.
    path = _sessions_dir(project) / f"{session_id}.jsonl"
    _append_custom_title_if_changed(path, new_title, session_id)

    # Rename is a deliberate user interaction — bubble the session in the
    # sidebar sort.  effective_ts no longer uses file mtime (SDK
    # background writes pollute it), so we record access explicitly here.
    _record_session_access(session_id, project)

    return jsonify({"ok": True, "title": new_title})


@bp.route("/api/remap-name", methods=["POST"])
def api_remap_name():
    """Re-persist a user-set name under a new session ID after SDK remaps."""
    data = request.get_json(silent=True) or {}
    old_id = data.get("old_id", "").strip()
    new_id = data.get("new_id", "").strip()
    project = data.get("project", "").strip()
    if not old_id or not new_id:
        return jsonify({"error": "old_id and new_id required"}), 400

    title = _remap_name(old_id, new_id, project)
    if not title:
        return jsonify({"ok": True, "skipped": True})

    # Write custom-title entry to the new .jsonl if it exists. Dedup so
    # repeat remap-name calls don't pile identical lines onto the file.
    path = _sessions_dir(project) / f"{new_id}.jsonl"
    _append_custom_title_if_changed(path, title, new_id)

    return jsonify({"ok": True, "title": title})


@bp.route("/api/autonname/<session_id>", methods=["POST"])
def api_autoname(session_id):
    data = request.get_json(silent=True) or {}
    project = (data.get("project") or request.args.get("project", "")).strip()

    # Never override a name the user manually set
    existing = _load_names(project).get(session_id)
    if existing:
        return jsonify({"ok": True, "title": existing, "skipped": True,
                        "reason": "User-set name preserved"})

    # Accept prompt text directly (for immediate naming before JSONL exists)
    prompt_text = (data.get("prompt") or "").strip()
    messages = []
    if prompt_text:
        messages = [{"role": "user", "content": prompt_text}]
    else:
        # Fall back to reading from JSONL
        path = _sessions_dir(project) / f"{session_id}.jsonl"
        if not path.exists():
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
        all_s = all_sessions(summary_only=True, project=project)
        empty_count = sum(
            1 for s in all_s
            if (s.get("custom_title") or "").startswith("Empty Session")
            or (not s.get("custom_title") and s.get("message_count", 0) == 0)
        )
        title = f"Empty Session ({empty_count})"
        if path:
            _append_custom_title_if_changed(path, title, session_id)
        return jsonify({"ok": True, "title": title})

    try:
        title = smart_title(messages)

        # If re-evaluating, only update if the new title is meaningfully different
        if is_re_evaluate and title == old_title:
            return jsonify({"ok": True, "title": old_title, "skipped": True,
                            "reason": "Title unchanged"})

        # PHANTOM-PREVENTION (docs/plans/phantom-sessions-fix-spec.md LEAK B):
        # Decide whether to persist the title into _session_names.json. We
        # skip the save in two cases that produced phantoms in production:
        #
        #   1. ``"Untitled Session"`` — the heuristic fallback's last-resort
        #      string. Persisting it pollutes the names registry with rows
        #      whose only purpose is to display the same default the UI
        #      would render anyway from a missing entry.
        #
        #   2. The session has no .jsonl on disk AND the daemon doesn't
        #      know about it. Such an autoname call came from a fresh
        #      ``prompt_text`` for a session that may be abandoned before
        #      flush — saving the name would create a phantom row that
        #      survives even if the session never materialises.
        should_save = True
        if title == "Untitled Session":
            should_save = False
        elif (path is None or not (path and path.exists())):
            sm = current_app.session_manager
            try:
                in_flight = bool(sm.has_session(session_id))
            except Exception:
                in_flight = False
            if not in_flight:
                should_save = False

        # Persist title — write to JSONL if available, always save to names file
        # (subject to the phantom-prevention guard above). Dedup the JSONL
        # append so repeat autoname calls (e.g. re-evaluations that landed on
        # the same title) don't pile identical custom-title lines onto disk.
        if path and path.exists():
            _append_custom_title_if_changed(path, title, session_id)
        if should_save:
            _save_name(session_id, title, project)

        return jsonify({"ok": True, "title": title, "renamed": is_re_evaluate})

    except Exception as e:
        return jsonify({"error": str(e)}), 500


@bp.route("/api/delete/<session_id>", methods=["DELETE"])
def api_delete(session_id):
    import os
    import signal
    import time

    project = request.args.get("project", "").strip()
    path = _sessions_dir(project) / f"{session_id}.jsonl"
    folder = _sessions_dir(project) / session_id

    # Phase 1: Close the SDK session SYNCHRONOUSLY so the CLI subprocess is
    # fully dead before we remove the file (prevents it from being recreated).
    sm = current_app.session_manager
    if sm.has_session(session_id):
        sm.close_session_sync(session_id)
        sm.remove_session(session_id)

    # ── Subsessions cleanup (spec §6.2): orphan children + remove inbox ──
    # Any in-memory child of this session gets parent_deleted_at + cleared
    # parent_session_id so the sidebar stops indenting it.  The parent's
    # per-parent inbox directory under ~/.claude/vibenode-state/<sid>/ is
    # removed so deleted reports don't linger as orphan storage.
    try:
        if hasattr(sm, "orphan_children_of"):
            orphaned = sm.orphan_children_of(session_id)
            if orphaned:
                log.info(
                    "Subsessions: orphaned %d child(ren) of deleted parent %s",
                    len(orphaned), session_id,
                )
    except Exception as e:
        log.debug("orphan_children_of soft-fail: %s", e)
    try:
        from daemon.subsession_inbox import remove_inbox
        remove_inbox(session_id)
    except Exception as e:
        log.debug("remove_inbox soft-fail for %s: %s", session_id, e)

    # Phase 2: Kill any orphaned CLI process for this session.
    # Always attempt this, even after SDK close, as a safety net -- the SDK
    # close may time out or the process may have been spawned by an earlier
    # server instance that we have no handle to.
    try:
        from ..process_detection import _get_running_session_ids
        running = _get_running_session_ids()
        pid = running.get(session_id)
        # SUICIDE GUARD: refuse to signal pid <= 1 or our own pid.  pid==0
        # would broadcast SIGTERM to our entire process group (which on
        # Linux includes the daemon when the web/daemon are launched
        # together); our-own-pid would kill the web server.  The "pid > 0"
        # check below already excluded the negative "display-only" sentinel
        # from process_detection.
        if pid and pid > 1 and pid != os.getpid():
            try:
                os.kill(pid, signal.SIGTERM)
            except (OSError, ProcessLookupError):
                pass
            time.sleep(0.5)  # let process die
    except Exception:
        pass  # best-effort

    # Capture the user-set name BEFORE we remove it from the names store, so
    # restore-from-trash can re-create the session with its title intact.
    saved_name = _load_names(project).get(session_id, "")

    # Phase 3: Tombstone + soft-delete the file.
    # Write the tombstone FIRST so all_sessions() hides this ID immediately,
    # even if a dying process recreates the .jsonl after we move it.
    _mark_deleted(session_id, project)

    if not path.exists():
        sm._save_registry_now()
        return jsonify({"ok": True})  # Already gone or never created

    # Soft-delete (recoverable): move the .jsonl into the per-project
    # ``_trash/`` folder instead of unlinking it, so an accidental delete can
    # be undone via /api/trash/<id>/restore. move_to_trash retries on Windows
    # file locks (same bounded loop as _unlink_with_retry) and records the
    # deletion time + saved name in _trash_index.json.
    trashed = move_to_trash(session_id, project, name=saved_name)
    if folder.exists() and folder.is_dir():
        try:
            shutil.rmtree(folder)
        except (PermissionError, OSError):
            pass

    # Phase 4: If the move didn't succeed (file vanished mid-flight, or stayed
    # locked through every retry), fall back to the hard-delete path so we
    # never leave a visible zombie .jsonl behind. The tombstone above is the
    # authoritative "deleted" signal; truncate-to-zero is the last resort if
    # AV refuses to release the handle.
    if not trashed and path.exists():
        if not _unlink_with_retry(path, retries=3, delay=0.2):
            try:
                path.write_bytes(b"")
                log.warning("Truncated locked JSONL to 0 bytes: %s", path)
            except Exception as e:
                log.warning("Failed to truncate locked JSONL %s: %s", path, e)
    if folder.exists() and folder.is_dir():
        try:
            shutil.rmtree(folder)
        except Exception:
            pass

    # Evict from summary cache (all keys whose path matches)
    path_str = str(path)
    for key in [k for k in _summary_cache if k[0] == path_str]:
        del _summary_cache[key]

    # Clean up user-set name from persistent store (the title is preserved in
    # the trash index, so a later restore re-applies it).
    _delete_name(session_id, project)

    # Force immediate registry save so recovery can't resurrect this session
    sm._save_registry_now()

    return jsonify({"ok": True})


@bp.route("/api/trash", methods=["GET"])
def api_trash_list():
    """List recoverable (soft-deleted) sessions for the active project.

    Returns the trashed sessions newest-deleted first, each with its saved
    title, deletion timestamp, and transcript size so the UI can offer a
    one-click restore.  Entries expire per the user's retention policy
    (``session_retention_days``; default Forever), honoring per-entry
    grandfather protection — see ``session_store._prune_trash``.  Each item
    also carries an additive ``purge_at`` epoch (None == kept forever).
    """
    project = request.args.get("project", "").strip()
    items = list_trash(project)
    # Add human-friendly ISO timestamps without changing the raw epochs.
    for it in items:
        try:
            it["deleted_at_iso"] = datetime.fromtimestamp(
                it.get("deleted_at", 0), tz.utc
            ).isoformat()
        except Exception:
            it["deleted_at_iso"] = ""
        pa = it.get("purge_at")
        try:
            it["purge_at_iso"] = (
                datetime.fromtimestamp(pa, tz.utc).isoformat()
                if pa is not None else None
            )
        except Exception:
            it["purge_at_iso"] = None
    return jsonify({"ok": True, "trash": items})


@bp.route("/api/trash/<session_id>/restore", methods=["POST"])
def api_trash_restore(session_id):
    """Restore a soft-deleted session: move its .jsonl back, clear the
    tombstone so it stops being hidden, and re-apply its saved title."""
    project = (
        (request.get_json(silent=True) or {}).get("project")
        or request.args.get("project", "")
    ).strip()

    name = restore_from_trash(session_id, project)
    if name is None:
        return jsonify({"error": "Not found in trash"}), 404

    # Clear the tombstone so all_sessions() surfaces the session again.
    _unmark_deleted(session_id, project)
    # Re-apply the saved title (if any) so it sorts/labels correctly.
    if name:
        _save_name(session_id, name, project)

    return jsonify({"ok": True, "id": session_id, "title": name})


@bp.route("/api/trash/<session_id>", methods=["DELETE"])
def api_trash_purge(session_id):
    """Permanently delete a single trashed session (no further undo)."""
    project = request.args.get("project", "").strip()
    purged = purge_from_trash(session_id, project)
    return jsonify({"ok": True, "purged": purged})


@bp.route("/api/delete-all", methods=["DELETE"])
def api_delete_all():
    """Delete every session in the active workspace in one shot."""
    from concurrent.futures import ThreadPoolExecutor

    project = request.args.get("project", "").strip()
    sd = _sessions_dir(project)
    sm = current_app.session_manager

    sids_to_delete = [f.stem for f in sd.glob("*.jsonl")]
    if not sids_to_delete:
        return jsonify({"ok": True, "deleted": 0})

    # Phase 1: tombstone ALL immediately so UI hides them right away
    _mark_deleted_bulk(sids_to_delete, project)

    # Phase 2: close SDK sessions in parallel (don't wait for each one)
    def _close(sid):
        try:
            if sm.has_session(sid):
                sm.close_session_sync(sid)
                sm.remove_session(sid)
        except Exception:
            pass

    with ThreadPoolExecutor(max_workers=8) as pool:
        pool.map(_close, sids_to_delete)

    # Phase 3: delete all files (with retry helper for Windows file locks)
    deleted = 0
    for f in list(sd.glob("*.jsonl")):
        if _unlink_with_retry(f, retries=3, delay=0.2):
            folder = sd / f.stem
            if folder.exists() and folder.is_dir():
                shutil.rmtree(folder, ignore_errors=True)
            deleted += 1
        elif f.exists():
            # Last resort: truncate locked file so it can be cleaned later.
            try:
                f.write_bytes(b"")
            except Exception:
                pass

    # Phase 4: clear names file in one write instead of per-session
    try:
        nf = _names_file(project)
        nf.write_text("{}", encoding="utf-8")
    except Exception:
        pass

    _summary_cache.clear()
    sm._save_registry_now()

    return jsonify({"ok": True, "deleted": deleted})


@bp.route("/api/delete-empty", methods=["DELETE"])
def api_delete_empty():
    import os
    import signal

    project = request.args.get("project", "").strip()
    sm = current_app.session_manager
    deleted = []
    deleted_paths = []  # track for Phase 4 sweep

    # First pass: identify empty sessions and tombstone them immediately
    empty_files = []
    for f in _sessions_dir(project).glob("*.jsonl"):
        s = load_session(f)
        if s.get("message_count", 0) == 0:
            empty_files.append(f)

    # Tombstone all empty session IDs BEFORE deleting anything
    if empty_files:
        _mark_deleted_bulk([f.stem for f in empty_files], project)

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
            # SUICIDE GUARD: same reasoning as the api_delete endpoint above
            # — never signal pid <= 1 or our own pid.  Process-detection
            # filters by /comm in ("claude","node") so a python3 web/daemon
            # pid should never appear here, but the suicide guard is
            # belt-and-suspenders.
            if pid and pid > 1 and pid != os.getpid():
                try:
                    os.kill(pid, signal.SIGTERM)
                except (OSError, ProcessLookupError):
                    pass
        except Exception:
            pass

        folder = _sessions_dir(project) / sid
        path_str = str(f)
        # Use the retry helper so locked files don't silently leak
        _unlink_with_retry(f)
        if folder.exists() and folder.is_dir():
            try:
                shutil.rmtree(folder)
            except Exception:
                pass
        for key in [k for k in _summary_cache if k[0] == path_str]:
            del _summary_cache[key]
        _delete_name(sid, project)
        deleted.append(sid)
        deleted_paths.append((f, folder))

    # Sweep for files re-created by dying processes
    if deleted_paths:
        for f, folder in deleted_paths:
            if f.exists():
                _unlink_with_retry(f, retries=3, delay=0.2)
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
    project = request.args.get("project", "").strip()
    src = _sessions_dir(project) / f"{session_id}.jsonl"
    if not src.exists():
        return jsonify({"error": "Not found"}), 404

    new_id = str(uuid_mod.uuid4())
    dst = _sessions_dir(project) / f"{new_id}.jsonl"

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

    project = request.args.get("project", "").strip()
    src = _sessions_dir(project) / f"{session_id}.jsonl"
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
    _cwd = _decode_project(active_project)
    if sys.platform == "win32":
        _cwd = _cwd.replace("/", "\\")
    user_entry = {"parentUuid": None, "isSidechain": False, "userType": "external",
                  "cwd": _cwd,
                  "sessionId": new_id, "version": "2.1.71", "gitBranch": "main",
                  "type": "user", "message": {"role": "user", "content": handoff},
                  "uuid": msg_uuid, "timestamp": now}
    title_entry = {"type": "custom-title", "customTitle": f"[cont] {topic[:55]}", "sessionId": new_id}

    dst = _sessions_dir(project) / f"{new_id}.jsonl"
    with open(dst, "w", encoding="utf-8") as f:
        f.write(json.dumps(snapshot) + "\n")
        f.write(json.dumps(user_entry) + "\n")
        f.write(json.dumps(title_entry) + "\n")

    return jsonify({"ok": True, "new_id": new_id, "title": f"[cont] {topic[:55]}"})


@bp.route("/api/session-timeline/<session_id>")
def api_session_timeline(session_id):
    """Return lightweight message list for the fork/rewind timeline picker."""
    project = request.args.get("project", "").strip()
    canonical = _resolve_remapped_id(session_id, project)
    if canonical:
        session_id = canonical
    path = _sessions_dir(project) / f"{session_id}.jsonl"
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

    project = request.args.get("project", "").strip()
    src = _sessions_dir(project) / f"{session_id}.jsonl"
    if not src.exists():
        return jsonify({"error": "Not found"}), 404

    data = request.get_json(silent=True) or {}
    up_to_line = data.get("up_to_line")
    if not up_to_line or not isinstance(up_to_line, int):
        return jsonify({"error": "up_to_line is required"}), 400

    new_id = str(uuid_mod.uuid4())
    dst = _sessions_dir(project) / f"{new_id}.jsonl"

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


# ── Subsessions: spawn endpoint (spec §4.2) ──────────────────────────────
# POST /api/sessions/<parent_sid>/spawn-subsession
#
# Reuses the JSONL slice + sessionId-rewrite machinery from /api/fork.
# A subsession is just a normal SDK session whose JSONL is seeded with
# the parent's transcript up to the spawn moment, plus a parent pointer
# in the registry (parent_session_id, subsession_origin_turn) and a
# session_type of "subsession" for the sidebar discriminator.
#
# Guards (spec §4.2 + §6.8):
#   1. Parent must exist and be in the active project (cross-project
#      spawn is rejected — spec §6.5).
#   2. Parent.session_type != "planner" — planners self-recycle and
#      cannot legally be parents (spec §4.2).
#   3. Cycle guard: walk the parent chain up to 32 hops via the daemon's
#      in-memory SessionInfo + the registry snapshot, reject if the
#      proposed child SID appears anywhere in the chain.  This is
#      impossible in practice for a freshly-generated UUID4, but the
#      guard codifies the invariant for any future re-parent flow.
#
# Returns: {ok: True, new_id, parent_id, title} on success;
#          {error: "<reason>"} with 400 / 404 / 409 on rejection.
@bp.route("/api/sessions/<parent_sid>/spawn-subsession", methods=["POST"])
def api_spawn_subsession(parent_sid):
    """Spawn a subsession from an existing parent session."""
    import uuid as uuid_mod
    from daemon.subsession_inbox import _validate_sid

    project = request.args.get("project", "").strip()
    sm = current_app.session_manager

    # Phase 6.5 P1-2: validate the SID shape BEFORE we use it to compose
    # filesystem paths.  Path-traversal SIDs (e.g. "..\\..\\evil") would
    # otherwise resolve outside the sessions dir or the vibenode-state dir.
    try:
        _validate_sid(parent_sid)
    except ValueError as e:
        return jsonify({"error": f"Invalid parent_sid: {e}"}), 400

    # Resolve any in-memory alias on the parent SID.
    canonical_parent = _resolve_remapped_id(parent_sid, project)
    if canonical_parent:
        parent_sid = canonical_parent

    src = _sessions_dir(project) / f"{parent_sid}.jsonl"
    if not src.exists():
        return jsonify({"error": "Parent session not found"}), 404

    # ── Guard 1: Parent project = active project (cross-project guard) ──
    # The parent's cwd (when daemon-managed) must match the active
    # project's cwd.  When the parent is only on-disk (no daemon entry)
    # the request's project arg has already located the file under the
    # active project's sessions_dir, so file-existence is the guard.
    parent_meta = sm.get_subsession_meta(parent_sid)
    if parent_meta:
        parent_cwd = parent_meta.get("cwd") or ""
        if parent_cwd and not cwd_matches_active_project(parent_cwd, project=project):
            return jsonify({
                "error": "Cross-project spawn rejected: parent belongs to a different project"
            }), 400

        # ── Guard 2: Planner parents are rejected (spec §4.2) ──
        if parent_meta.get("session_type") == "planner":
            return jsonify({
                "error": "Cannot spawn a subsession from a planner session"
            }), 400

    # Generate the new child SID up front so we can include it in the
    # cycle guard and the JSONL rewrite.
    new_id = str(uuid_mod.uuid4())

    # ── Guard 3: Cycle prevention (spec §6.8) ──
    # Walk the parent chain up to 32 hops and abort if new_id appears.
    # In practice impossible for a freshly-generated UUID4; the guard
    # codifies the invariant for future re-parent code paths.
    _MAX_PARENT_CHAIN = 32
    cursor = parent_sid
    visited = {new_id}
    for _ in range(_MAX_PARENT_CHAIN):
        if cursor in visited:
            return jsonify({
                "error": "Subsession parent chain contains a cycle — aborted"
            }), 409
        visited.add(cursor)
        cursor_meta = sm.get_subsession_meta(cursor)
        if not cursor_meta:
            break
        next_parent = cursor_meta.get("parent_session_id")
        if not next_parent:
            break
        cursor = next_parent

    # ── Slice the parent JSONL at the current line count ──
    # Mirrors api_fork (line ~849) but appends a "[sub] <parent_name>"
    # custom-title and persists the parent pointer on the new SessionInfo.
    dst = _sessions_dir(project) / f"{new_id}.jsonl"
    lines_out = []
    line_count = 0
    parent_name = (parent_meta or {}).get("name") or ""
    original_title = parent_name or parent_sid[:8]

    with open(src, encoding="utf-8") as f:
        for line in f:
            line_count += 1
            raw = line.strip()
            if not raw:
                continue
            try:
                obj = json.loads(raw)
            except Exception:
                lines_out.append(raw)
                continue
            if obj.get("type") == "custom-title" and not parent_name:
                original_title = obj.get("customTitle", original_title)
            if "sessionId" in obj:
                obj["sessionId"] = new_id
            lines_out.append(json.dumps(obj))

    # subsession_origin_turn captures the parent's JSONL line count at
    # the spawn moment.  Used by the rewind-past-spawn detector in
    # Phase 6 to flag children whose anchor disappeared after a rewind.
    subsession_origin_turn = line_count

    sub_title = f"[sub] {original_title[:55]}"
    lines_out.append(json.dumps({
        "type": "custom-title",
        "customTitle": sub_title,
        "sessionId": new_id,
    }))

    with open(dst, "w", encoding="utf-8") as f:
        f.write("\n".join(lines_out) + "\n")

    # Persist the human-readable title for sidebar rendering before
    # start_session fires its first state emit.
    try:
        _save_name(new_id, sub_title, project)
    except Exception as e:
        log.debug("spawn_subsession: _save_name failed for %s: %s", new_id, e)

    # ── Start the child SDK session with the parent pointer in-place ──
    active_project = get_active_project()
    proj_dir = _decode_project(active_project) if active_project else str(Path.home())
    try:
        result = sm.start_session(
            session_id=new_id,
            prompt="",
            cwd=proj_dir,
            name=sub_title,
            resume=True,
            session_type="subsession",
            parent_session_id=parent_sid,
            subsession_origin_turn=subsession_origin_turn,
        )
    except Exception as e:
        return jsonify({"error": f"Failed to start subsession: {e}"}), 500

    if not result.get("ok"):
        return jsonify({"error": result.get("error", "Failed to start subsession")}), 500

    return jsonify({
        "ok": True,
        "new_id": new_id,
        "parent_id": parent_sid,
        "title": sub_title,
        "subsession_origin_turn": subsession_origin_turn,
    })


# ── Subsessions: report-to-parent endpoint (spec §4.3.3) ─────────────────
# POST /api/sessions/<child_sid>/report-to-parent
# Body: {"summary": str, "attachments"?: list}
#
# Looks up the child's parent_session_id via SessionManager, appends a
# new entry to the parent's inbox.json under
# ~/.claude/vibenode-state/<parent_sid>/, marks the in-memory
# inbox_dirty flag on the parent if it's still loaded, and emits an
# inbox_updated WS event so the sidebar badge updates without polling.
@bp.route("/api/sessions/<child_sid>/report-to-parent", methods=["POST"])
def api_report_to_parent(child_sid):
    """Write a report from a subsession into its parent's inbox."""
    from daemon.subsession_inbox import (
        _validate_sid,
        append_report,
        undelivered_count,
    )

    project = request.args.get("project", "").strip()
    sm = current_app.session_manager

    # Phase 6.5 P1-2: validate the SID shape before any disk access.
    try:
        _validate_sid(child_sid)
    except ValueError as e:
        return jsonify({"error": f"Invalid child_sid: {e}"}), 400

    canonical = _resolve_remapped_id(child_sid, project)
    if canonical:
        child_sid = canonical

    data = request.get_json(silent=True) or {}
    summary = (data.get("summary") or "").strip()
    if not summary:
        return jsonify({"error": "summary is required"}), 400
    attachments = data.get("attachments") or []
    if not isinstance(attachments, list):
        return jsonify({"error": "attachments must be a list"}), 400

    # Find the child's daemon-side metadata so we can locate the parent
    # SID and child display name.
    child_meta = sm.get_subsession_meta(child_sid)
    if not child_meta:
        return jsonify({"error": "child session not found"}), 404

    parent_sid = child_meta.get("parent_session_id")
    if not parent_sid:
        return jsonify({"error": "this session has no parent"}), 404

    # Verify the parent still exists somewhere — either daemon-managed
    # or on disk.  Parent deletion (spec §6.2) leaves an orphaned child;
    # the endpoint must refuse the write in that case so the UI can
    # toast a "Reports to: (parent deleted)" affordance.
    parent_meta = sm.get_subsession_meta(parent_sid)
    parent_jsonl = _sessions_dir(project) / f"{parent_sid}.jsonl"
    if not parent_meta and not parent_jsonl.exists():
        return jsonify({"error": "parent session not found"}), 404

    child_name = child_meta.get("name") or ""

    try:
        entry = append_report(
            parent_sid=parent_sid,
            child_sid=child_sid,
            child_name=child_name,
            summary=summary,
            attachments=attachments,
        )
    except Exception as e:
        log.warning(
            "report-to-parent: append_report failed for child=%s parent=%s: %s",
            child_sid, parent_sid, e,
        )
        return jsonify({"error": f"failed to write inbox: {e}"}), 500

    # Best-effort: flip the parent's in-memory inbox_dirty flag so the
    # next send_message picks up the report without reading the file.
    # Falls through harmlessly if the parent isn't daemon-managed (the
    # next time it loads, it will re-derive inbox_dirty from disk).
    try:
        if hasattr(sm, "mark_inbox_dirty"):
            sm.mark_inbox_dirty(parent_sid)
    except Exception as e:
        log.debug("report-to-parent: mark_inbox_dirty soft-fail: %s", e)

    # Emit a real-time inbox_updated event so any connected client can
    # update the parent's badge immediately.  Best-effort — a missing
    # socketio binding (e.g. in unit tests) silently falls through.
    try:
        from .. import socketio
        socketio.emit("inbox_updated", {
            "parent_session_id": parent_sid,
            "undelivered_count": undelivered_count(parent_sid),
            "from_child_session_id": child_sid,
        })
    except Exception as e:
        log.debug("report-to-parent: socketio emit soft-fail: %s", e)

    return jsonify({
        "ok": True,
        "report_id": entry.get("report_id"),
        "parent_session_id": parent_sid,
        "undelivered_count": undelivered_count(parent_sid),
    })


# ── Subsessions: pull-subsession-updates endpoint (phase 6.5 P0-1/P0-2) ──
# POST /api/sessions/<parent_sid>/pull-subsession-updates
#
# Explicit REST endpoint behind the live-panel "Pull updates" button.
# Replaces the earlier attempt to route Pull-updates through the WS
# `send_message` handler — that handler rejects empty text before the
# inbox drain branch ever runs, so the only way to invoke the empty-text
# Pull-updates path (spec §4.3.5) is via this dedicated endpoint.
#
# Behavior:
#   1. Resolve the parent SID; 404 if not daemon-managed.
#   2. If the parent has no undelivered reports on disk AND no in-memory
#      inbox_dirty flag, return {ok: True, pulled: False, undelivered_count: 0}
#      without invoking send_message.  This keeps a no-op button click
#      from spamming the parent with empty turns.
#   3. Call SessionManager.send_message(parent_sid, "") — the existing
#      empty-text branch in send_message (daemon/session_manager.py §4.3.5)
#      drains the inbox and sends the block as the entire user turn.
#   4. Forward send_message's queued/ok/error shape back to the caller.
@bp.route(
    "/api/sessions/<parent_sid>/pull-subsession-updates",
    methods=["POST"],
)
def api_pull_subsession_updates(parent_sid):
    """Deliver pending subsession reports to the parent as the next turn."""
    from daemon.subsession_inbox import (
        _validate_sid,
        has_undelivered,
        undelivered_count,
    )

    project = request.args.get("project", "").strip()
    sm = current_app.session_manager

    # Phase 6.5 P1-2: validate the SID shape before any disk access.
    try:
        _validate_sid(parent_sid)
    except ValueError as e:
        return jsonify({"error": f"Invalid parent_sid: {e}"}), 400

    canonical = _resolve_remapped_id(parent_sid, project)
    if canonical:
        parent_sid = canonical

    # 1. Parent must be daemon-managed; otherwise there's no send_message
    # to fire and the drain block never reaches the SDK.  We deliberately
    # do NOT fall back to writing into the inbox on disk — the inbox is
    # the *write* side; this endpoint is the *deliver* side.
    parent_meta = sm.get_subsession_meta(parent_sid)
    if not parent_meta:
        return jsonify({"error": "parent session not found"}), 404

    # 2. No-op guard.  If the in-memory flag is False AND the on-disk
    # file is empty, do nothing.  This protects against a button mash
    # from injecting a stream of empty turns into the parent.
    has_pending = False
    try:
        has_pending = has_undelivered(parent_sid)
    except Exception as e:
        log.debug("pull-subsession-updates: has_undelivered soft-fail: %s", e)
        has_pending = False
    # Also honor the in-memory flag — it can be True while disk says
    # otherwise for the brief window between report-to-parent's append
    # and the next persist cycle.
    inmem_dirty = False
    try:
        if hasattr(sm, "_sessions"):
            with sm._lock:
                info = sm._sessions.get(parent_sid)
                if info:
                    inmem_dirty = bool(getattr(info, "inbox_dirty", False))
    except Exception:
        inmem_dirty = False

    if not has_pending and not inmem_dirty:
        return jsonify({
            "ok": True,
            "pulled": False,
            "undelivered_count": 0,
        })

    # 3. Fire send_message with empty text — the daemon's inbox-drain
    # branch (spec §4.3.5) turns this into "deliver the drain block as
    # the entire user turn."
    result = sm.send_message(parent_sid, "")
    if not isinstance(result, dict):
        return jsonify({"error": "send_message returned non-dict"}), 500

    # 4. Mirror send_message's response shape so the frontend can use the
    # same success/failed/queued handling it uses for normal sends.
    pulled = bool(result.get("ok") or result.get("queued"))
    payload = {
        "ok": bool(result.get("ok") or result.get("queued")),
        "pulled": pulled,
        "queued": bool(result.get("queued")),
        "undelivered_count": undelivered_count(parent_sid),
    }
    if not pulled and result.get("error"):
        payload["error"] = result.get("error")
        return jsonify(payload), 400
    return jsonify(payload)


# ── Subsessions: re-anchor / detach for rewind-orphaned children (P1-5) ──
# POST /api/sessions/<child_sid>/reanchor
# Body: {"origin_turn": int}     # optional; defaults to current parent tip
#
# Called by the rewind-orphan UI prompt when the user chooses "Re-anchor at
# current parent tip" for a child whose subsession_origin_turn fell past
# the parent's new line count after a rewind.  Updates the child's
# in-memory ``subsession_origin_turn`` so the spawn-point badge clears.
@bp.route("/api/sessions/<child_sid>/reanchor", methods=["POST"])
def api_reanchor_subsession(child_sid):
    """Re-anchor a rewind-orphaned subsession at a new parent tip."""
    from daemon.subsession_inbox import _validate_sid

    project = request.args.get("project", "").strip()
    sm = current_app.session_manager

    try:
        _validate_sid(child_sid)
    except ValueError as e:
        return jsonify({"error": f"Invalid child_sid: {e}"}), 400

    canonical = _resolve_remapped_id(child_sid, project)
    if canonical:
        child_sid = canonical

    data = request.get_json(silent=True) or {}
    new_origin_turn = data.get("origin_turn")

    # If no explicit origin_turn given, derive from the parent's current
    # JSONL line count.  This is the spec §6.3 "Re-anchor at current parent
    # tip" affordance.
    if new_origin_turn is None:
        meta = sm.get_subsession_meta(child_sid)
        if not meta:
            return jsonify({"error": "child session not found"}), 404
        parent_sid = meta.get("parent_session_id")
        if not parent_sid:
            return jsonify({"error": "child has no parent"}), 400
        parent_jsonl = _sessions_dir(project) / f"{parent_sid}.jsonl"
        if not parent_jsonl.exists():
            return jsonify({"error": "parent session not found"}), 404
        try:
            with open(parent_jsonl, encoding="utf-8") as f:
                new_origin_turn = sum(1 for _ in f)
        except Exception as e:
            return jsonify({"error": f"failed to read parent jsonl: {e}"}), 500

    if not isinstance(new_origin_turn, int) or new_origin_turn < 0:
        return jsonify({"error": "origin_turn must be a non-negative integer"}), 400

    if not hasattr(sm, "reanchor_subsession"):
        return jsonify({"error": "reanchor not supported"}), 500
    ok = bool(sm.reanchor_subsession(child_sid, new_origin_turn))
    if not ok:
        return jsonify({"error": "child session not found"}), 404
    return jsonify({"ok": True, "subsession_origin_turn": new_origin_turn})


# POST /api/sessions/<child_sid>/detach
# Detaches a subsession from its parent (sets parent_session_id=None),
# behaving like §6.2 orphan.  Used by the rewind-orphan UI prompt when
# the user chooses "Detach" over "Re-anchor."
@bp.route("/api/sessions/<child_sid>/detach", methods=["POST"])
def api_detach_subsession(child_sid):
    """Detach a subsession from its parent (orphan-like state)."""
    from daemon.subsession_inbox import _validate_sid

    project = request.args.get("project", "").strip()
    sm = current_app.session_manager

    try:
        _validate_sid(child_sid)
    except ValueError as e:
        return jsonify({"error": f"Invalid child_sid: {e}"}), 400

    canonical = _resolve_remapped_id(child_sid, project)
    if canonical:
        child_sid = canonical

    if not hasattr(sm, "detach_subsession"):
        return jsonify({"error": "detach not supported"}), 500
    ok = bool(sm.detach_subsession(child_sid))
    if not ok:
        return jsonify({"error": "child session not found"}), 404
    return jsonify({"ok": True})


# ── Subsessions: auto-report toggle (phase 6.5 P1-4) ─────────────────────
# POST /api/sessions/<child_sid>/auto-report-toggle
# Body: {"on": bool}
#
# Sets the in-memory + persisted ``auto_report_on_idle`` preference on a
# subsession.  When True, the daemon writes the last assistant message
# to the parent's inbox on each IDLE transition that has a fresh turn
# since the last auto-report.  Idempotent across multiple IDLE emits via
# the SessionInfo ``_last_auto_report_entry_count`` counter.
@bp.route(
    "/api/sessions/<child_sid>/auto-report-toggle",
    methods=["POST"],
)
def api_auto_report_toggle(child_sid):
    """Toggle auto-report-on-idle for a subsession."""
    from daemon.subsession_inbox import _validate_sid

    project = request.args.get("project", "").strip()
    sm = current_app.session_manager

    try:
        _validate_sid(child_sid)
    except ValueError as e:
        return jsonify({"error": f"Invalid child_sid: {e}"}), 400

    canonical = _resolve_remapped_id(child_sid, project)
    if canonical:
        child_sid = canonical

    data = request.get_json(silent=True) or {}
    on = bool(data.get("on"))

    ok = False
    try:
        if hasattr(sm, "set_auto_report_on_idle"):
            ok = bool(sm.set_auto_report_on_idle(child_sid, on))
    except Exception as e:
        log.warning(
            "auto-report-toggle: set_auto_report_on_idle failed for %s: %s",
            child_sid, e,
        )
        return jsonify({"error": str(e)}), 500

    if not ok:
        return jsonify({"error": "subsession not found"}), 404
    return jsonify({"ok": True, "auto_report_on_idle": on})


@bp.route("/api/rewind/<session_id>", methods=["POST"])
def api_rewind(session_id):
    """Rewind tracked files to the state at a given message line number."""
    project = request.args.get("project", "").strip()
    src = _sessions_dir(project) / f"{session_id}.jsonl"
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
                all_snapshots.append((mid, inner_mid, snap, line_num))

    # Also try snapshot-based restore (works when daemon wrote proper entries)
    # Use snapshots that appear at or before the target line
    merged_backups = {}
    for mid, inner_mid, snap, snap_line in all_snapshots:
        if snap_line <= up_to_line or mid in msg_uuids_before or inner_mid in msg_uuids_before:
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
                # Write overwrites the entire file — we can't reverse it by
                # string replacement like Edit.  The only way to restore is
                # via a file-history snapshot (handled in the snapshot
                # fallback below).  We skip it here so that the snapshot
                # loop gets a chance to find a backup.  If no snapshot
                # exists on disk, the catch-all at the end of this function
                # will add the file to 'skipped'.
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

    # Catch any files from edits_after that weren't handled by either
    # the Edit reversal or the snapshot fallback.  This ensures Write
    # tool_use files without a snapshot don't silently vanish from the
    # response — they appear in files_skipped so the caller knows.
    _all_edit_files = set(fp for fp, _, _, _ in edits_after)
    _handled = set(restored) | set(skipped)
    for fp in _all_edit_files:
        if fp not in _handled:
            skipped.append(fp)

    # ── Subsessions rewind-orphan detection (spec §6.3) ──
    # If this parent has any in-memory children whose subsession_origin_turn
    # is now past the rewound line count, surface them so the UI can show a
    # "Spawn point no longer in parent's history" badge and prompt the
    # user to Re-anchor or Detach.  We do NOT auto-detach.
    rewind_orphans = []
    try:
        sm = current_app.session_manager
        if hasattr(sm, "detect_rewind_orphans"):
            candidate = sm.detect_rewind_orphans(session_id, int(up_to_line))
            # Only accept a real list — test stubs (MagicMock) return a
            # non-list and we'd otherwise crash flask's JSON encoder.
            if isinstance(candidate, list):
                rewind_orphans = candidate
    except Exception as e:
        log.debug("detect_rewind_orphans soft-fail: %s", e)

    return jsonify({
        "ok": True,
        "files_restored": restored,
        "files_skipped": skipped,
        "rewind_orphans": rewind_orphans,
    })


@bp.route("/api/fork-rewind/<session_id>", methods=["POST"])
def api_fork_rewind(session_id):
    """Fork conversation AND rewind code to a given message."""
    import uuid as uuid_mod

    project = request.args.get("project", "").strip()
    src = _sessions_dir(project) / f"{session_id}.jsonl"
    if not src.exists():
        return jsonify({"error": "Not found"}), 404

    data = request.get_json(silent=True) or {}
    up_to_line = data.get("up_to_line")
    if not up_to_line or not isinstance(up_to_line, int):
        return jsonify({"error": "up_to_line is required"}), 400

    # --- Fork ---
    new_id = str(uuid_mod.uuid4())
    dst = _sessions_dir(project) / f"{new_id}.jsonl"

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
    project = request.args.get("project", "").strip()
    path = _sessions_dir(project) / f"{session_id}.jsonl"
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
