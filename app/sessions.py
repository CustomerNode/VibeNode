"""
Session loading -- summary parser and full session parser.
"""

import json
import os
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path

from .config import (
    _sessions_dir,
    _load_names,
    _load_names_cached,
    _format_size,
    _summary_cache,
    _summary_cache_index,
    _get_deleted_ids,
    _get_utility_ids,
    _get_remapped_ids,
    _is_aititle_only_orphan,
    _load_session_access_cached,
)


def _overlay_access_ts(summary: dict, path: Path, access_map: dict | None = None) -> dict:
    """Return *summary* with ``effective_ts`` bumped by the project's
    recorded last-access time for this session if it is newer.

    Access timestamps live in ``_session_access.json`` and are updated by
    UI hooks (session open, send_message, etc).  They are NOT part of the
    file-content cache key, so we must overlay them on every call rather
    than baking them into the cached summary dict.

    *access_map* lets ``all_sessions()`` load the project's map ONCE and
    reuse it for every row; without it each summary resolved the project
    directory and stat'ed the access file again (two stats per session,
    ~170 ms of a 1,009-session listing).
    """
    try:
        if access_map is None:
            project = path.parent.name
            access_map = _load_session_access_cached(project)
        access_ts = access_map.get(path.stem, 0)
    except Exception:
        return summary
    if access_ts <= summary.get("effective_ts", 0):
        return summary
    return {**summary, "effective_ts": access_ts}


# ai-title-orphan verdicts, one entry per path: str(path) -> (mtime_ns, size,
# verdict).  ``_is_aititle_only_orphan`` stats AND fully reads every file under
# 100 KB; doing that on every listing cost ~210 ms for 1,009 transcripts.  A
# verdict can only change when the file changes, so it is keyed like the
# summary cache.  One entry per path means the cache cannot leak across turns.
_orphan_cache: dict = {}


def _is_orphan_cached(path: Path, st: os.stat_result) -> bool:
    key = (st.st_mtime_ns, st.st_size)
    hit = _orphan_cache.get(str(path))
    if hit is not None and hit[0] == key[0] and hit[1] == key[1]:
        return hit[2]
    # Mirrors the size cap inside _is_aititle_only_orphan: a real session
    # with even one turn is well past 100 KB, so skip the read entirely.
    verdict = False if st.st_size > 100_000 else _is_aititle_only_orphan(path)
    _orphan_cache[str(path)] = (key[0], key[1], verdict)
    return verdict


def _next_step_of(content) -> dict | None:
    """``{"verb", "line"}`` from the final assistant message's handoff card."""
    if content is None:
        return None
    if isinstance(content, list):
        content = "\n".join(b.get("text", "") for b in content
                            if isinstance(b, dict) and b.get("type") == "text")
    if "handoff" not in content:
        return None
    from common.handoff_status import next_step
    return next_step(content)


def _summary_cache_key(path: Path, st: os.stat_result) -> tuple:
    return (str(path), st.st_mtime, st.st_size)


def load_session_summary(path: Path, st: os.stat_result | None = None,
                         access_map: dict | None = None) -> dict:
    """Cached summary — reads the whole file, no head/tail tricks.

    *st* is an optional pre-fetched ``stat`` result (``all_sessions`` gets it
    for free from ``os.scandir`` on Windows); *access_map* is an optional
    pre-loaded ``_session_access.json`` dict (see ``_overlay_access_ts``).
    """
    _err = {"id": path.stem, "error": "", "custom_title": None,
            "display_title": path.stem, "date": "", "last_activity": "", "preview": "",
            "last_activity_ts": 0, "effective_ts": 0, "sort_ts": 0,
            "size": "0 B", "file_bytes": 0, "message_count": 0}
    if st is None:
        try:
            st = path.stat()
        except Exception:
            return _err

    cache_key = _summary_cache_key(path, st)
    cached = _summary_cache.get(cache_key)
    if cached is not None:
        # Even on cache hit, overlay access_ts: access can update without
        # invalidating the file-content cache (clicking a session in the
        # sidebar doesn't change mtime/size), and the sort needs the live
        # value.  See _overlay_access_ts below.
        # Return a COPY, never the cached dict itself.  Callers decorate the
        # summary in place (/api/sessions stamps ``last_state``, ``model``,
        # ``inbox_dirty``...).  Handing out the cached object made those
        # per-request decorations permanent: a session tagged
        # ``last_state="idle"`` kept that tag for the life of the web
        # process, so a session the user slept came back "idle" on the next
        # list load (project switch / refresh) — the "Sleep won't stick
        # across a project switch" bug.
        return dict(_overlay_access_ts(cached, path, access_map))

    custom_title = None
    first_user_content = ""
    first_ts = None
    last_ts = None
    message_count = 0
    last_asst_content = None

    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except Exception:
                    continue

                t = obj.get("type", "")

                if t == "custom-title":
                    # Always overwrite — last one wins
                    custom_title = obj.get("customTitle", "")

                elif t in ("user", "assistant"):
                    message_count += 1
                    # The final message's handoff card (sidebar status, see
                    # common/handoff_status.py).  Kept by reference; parsed once
                    # below.  Any user text after it means a newer turn began.
                    if not obj.get("isSidechain"):
                        _c = (obj.get("message") or {}).get("content", "")
                        _has_text = isinstance(_c, str) and bool(_c.strip()) or (
                            isinstance(_c, list) and any(
                                isinstance(b, dict) and b.get("type") == "text" for b in _c))
                        if _has_text:
                            last_asst_content = _c if t == "assistant" else None
                    ts_str = obj.get("timestamp", "")
                    if ts_str:
                        try:
                            ts = datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
                            if first_ts is None:
                                first_ts = ts
                            last_ts = ts
                        except Exception:
                            pass
                    if t == "user" and not first_user_content:
                        msg = obj.get("message", {})
                        raw_c = msg.get("content", "")
                        if isinstance(raw_c, str):
                            first_user_content = raw_c.strip()
                        elif isinstance(raw_c, list):
                            parts = [b.get("text", "") for b in raw_c
                                     if isinstance(b, dict) and b.get("type") == "text"]
                            first_user_content = " ".join(parts).strip()

    except Exception:
        return _err

    if first_ts:
        local_ts = first_ts.astimezone() if first_ts.tzinfo else first_ts
        date_str = local_ts.strftime("%b %d, %Y  %I:%M %p")
    else:
        date_str = datetime.fromtimestamp(st.st_mtime).strftime("%b %d, %Y  %I:%M %p")
        first_ts = datetime.fromtimestamp(st.st_mtime, tz=timezone.utc)

    if last_ts is None:
        last_ts = datetime.fromtimestamp(st.st_mtime, tz=timezone.utc)
    local_last = last_ts.astimezone() if last_ts.tzinfo else last_ts
    last_activity_str = local_last.strftime("%b %d, %Y  %I:%M %p")

    preview = first_user_content[:120] + ("\u2026" if len(first_user_content) > 120 else "")

    _proj = path.parent.name  # encoded project name from path
    names = _load_names_cached(project=_proj)
    user_set_name = names.get(path.stem)
    effective_title = user_set_name or custom_title

    last_activity_ts = last_ts.timestamp() if last_ts else 0
    # effective_ts seeds from the last real conversation message.  We do
    # NOT max with file mtime — the Claude SDK appends untimestamped
    # state entries (ai-title, mode, last-prompt, file-history-snapshot)
    # to sessions in the background, which bumps mtime without any
    # actual user interaction.  Maxing with mtime made those background
    # writes bubble unrelated sessions to the top of the sidebar (one
    # CustomerNode incident clustered 7 sessions at the same mtime
    # minute and pushed the user's actually-recent conversation 9
    # positions down).  Non-message user interactions bubble via the
    # access_ts overlay (see _overlay_access_ts) which is fed by
    # explicit UI hooks: click, send_message, rename, touch.
    effective_ts = last_activity_ts
    result = {
        "id": path.stem,
        "custom_title": effective_title,
        "user_named": bool(user_set_name),
        "display_title": effective_title if effective_title else (first_user_content[:60] + ("\u2026" if len(first_user_content) > 60 else "")) or path.stem,
        "date": date_str,
        "last_activity": last_activity_str,
        "last_activity_ts": last_activity_ts,
        "effective_ts": effective_ts,
        "sort_ts": first_ts.timestamp() if first_ts else 0,
        "file_bytes": st.st_size,
        "size": _format_size(st.st_size),
        "preview": preview,
        "message_count": message_count,
        "next_step": _next_step_of(last_asst_content),
    }
    # Drop the key this path held before (previous mtime/size) so a transcript
    # that grows every turn keeps exactly ONE entry in the cache.
    path_str = str(path)
    old_key = _summary_cache_index.get(path_str)
    if old_key is not None and old_key != cache_key:
        _summary_cache.pop(old_key, None)
    _summary_cache_index[path_str] = cache_key
    _summary_cache[cache_key] = result
    # Copy for the same reason as the cache-hit path above: the caller must
    # never be able to mutate the cached summary.
    return dict(_overlay_access_ts(result, path, access_map))


def load_session(path: Path) -> dict:
    """Parse a .jsonl session file and return a summary dict."""
    messages = []
    custom_title = None
    first_ts = None

    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except Exception:
                    continue   # skip partial/corrupt lines (e.g. mid-write)
                t = obj.get("type", "")

                if t == "custom-title":
                    custom_title = obj.get("customTitle", "")

                elif t in ("user", "assistant"):
                    role = t
                    content = ""
                    block_type = ""
                    msg = obj.get("message", {})
                    raw = msg.get("content", "")
                    if isinstance(raw, str):
                        content = raw
                    elif isinstance(raw, list):
                        text_parts = []
                        tool_names = []
                        for block in raw:
                            if not isinstance(block, dict):
                                continue
                            bt = block.get("type", "")
                            if bt == "text":
                                text_parts.append(block.get("text", ""))
                            elif bt == "tool_use":
                                tool_names.append(block.get("name", "tool"))
                            elif bt == "tool_result":
                                block_type = "tool_result"
                                tr_content = block.get("content", "")
                                if isinstance(tr_content, str) and tr_content.strip():
                                    text_parts.append(tr_content)
                                elif isinstance(tr_content, list):
                                    for sub in tr_content:
                                        if isinstance(sub, dict) and sub.get("type") == "text":
                                            text_parts.append(sub.get("text", ""))
                        content = " ".join(text_parts)
                        if not content and tool_names:
                            content = "[" + ", ".join(tool_names) + "]"
                            block_type = "tool"
                        elif tool_names and not block_type:
                            block_type = "tool"
                        # User messages with only tool_results are system output
                        if role == "user" and block_type == "tool_result":
                            block_type = "tool_result"

                    ts_str = obj.get("timestamp", "")
                    ts = None
                    if ts_str:
                        try:
                            ts = datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
                        except Exception:
                            pass

                    if ts and first_ts is None:
                        first_ts = ts

                    # Skip empty thinking-only messages
                    if content.strip():
                        msg_data = {"role": role, "content": content.strip(), "ts": ts_str}
                        if block_type:
                            msg_data["type"] = block_type
                        messages.append(msg_data)

    except Exception as e:
        return {"id": path.stem, "error": str(e), "messages": [], "custom_title": None,
                "display_title": path.stem, "date": "", "last_activity": "", "preview": "",
                "last_activity_ts": 0, "effective_ts": 0, "sort_ts": 0,
                "size": "0 B", "file_bytes": 0, "message_count": 0}

    # Date: prefer first message timestamp, fall back to file mtime
    if first_ts:
        date_str = first_ts.strftime("%b %d, %Y  %I:%M %p")
    else:
        mtime = path.stat().st_mtime
        date_str = datetime.fromtimestamp(mtime).strftime("%b %d, %Y  %I:%M %p")
        first_ts = datetime.fromtimestamp(mtime, tz=timezone.utc)

    first_user = next((m["content"] for m in messages if m["role"] == "user" and m["content"]), "")
    preview = first_user[:120] + ("\u2026" if len(first_user) > 120 else "")

    # Last activity: latest message timestamp or file mtime
    last_ts = None
    for m in reversed(messages):
        ts_str = m.get("ts", "")
        if ts_str:
            try:
                last_ts = datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
                break
            except Exception:
                pass
    if last_ts is None:
        last_ts = datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc)
    local_last = last_ts.astimezone() if last_ts.tzinfo else last_ts
    last_activity_str = local_last.strftime("%b %d, %Y  %I:%M %p")

    # File size (bytes of the .jsonl file only)
    file_bytes = path.stat().st_size
    if file_bytes < 1024:
        size_str = f"{file_bytes} B"
    elif file_bytes < 1024 * 1024:
        size_str = f"{file_bytes / 1024:.0f} KB"
    else:
        size_str = f"{file_bytes / (1024*1024):.0f} MB"

    # User-set names in _session_names.json always win over anything in the .jsonl
    _proj = path.parent.name  # encoded project name from path
    user_set_name = _load_names(project=_proj).get(path.stem)
    effective_title = user_set_name or custom_title

    last_activity_ts = last_ts.timestamp() if last_ts else 0
    # See load_session_summary for the rationale: mtime is too noisy
    # because the SDK appends untimestamped state entries in the
    # background.  Non-message interactions reach the sort via the
    # access_ts overlay applied below.
    effective_ts = last_activity_ts
    payload = {
        "id": path.stem,
        "custom_title": effective_title,
        "display_title": effective_title if effective_title else (first_user[:60] + ("\u2026" if len(first_user) > 60 else "")) or path.stem,
        "date": date_str,
        "last_activity": last_activity_str,
        "last_activity_ts": last_activity_ts,
        "effective_ts": effective_ts,
        "sort_ts": first_ts.timestamp() if first_ts else 0,
        "file_bytes": file_bytes,
        "size": size_str,
        "preview": preview,
        "message_count": len(messages),
        "messages": messages,
    }
    return _overlay_access_ts(payload, path)


def load_session_timeline(path: Path) -> dict:
    """Parse a .jsonl session for the message timeline picker.

    Returns a lightweight list of user/assistant messages with metadata
    (preview, timestamp, change counts) for the fork/rewind UI.
    """
    messages = []
    snapshots = {}
    has_any_snapshot = False
    line_num = 0
    custom_title = None

    try:
        with open(path, encoding="utf-8", errors="replace") as f:
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

                if t == "custom-title":
                    custom_title = obj.get("customTitle", "")

                elif t == "file-history-snapshot":
                    snap = obj.get("snapshot", {})
                    mid = obj.get("messageId", "")
                    if mid:
                        snapshots[mid] = snap
                    # Also index by inner snapshot.messageId (handles isSnapshotUpdate
                    # entries where outer and inner messageIds differ)
                    inner_mid = snap.get("messageId", "")
                    if inner_mid and inner_mid != mid:
                        snapshots[inner_mid] = snap
                    # Only count as a valid snapshot if at least one backup has
                    # a non-null backupFileName (null means file wasn't backed up)
                    backups = snap.get("trackedFileBackups", {})
                    if backups and any(
                        (v.get("backupFileName") if isinstance(v, dict) else v)
                        for v in backups.values()
                    ):
                        has_any_snapshot = True

                elif t in ("user", "assistant"):
                    role = t
                    msg = obj.get("message", {})
                    raw_c = msg.get("content", "")
                    content = ""
                    block_type = ""
                    added = 0
                    removed = 0
                    changed_files = []

                    if isinstance(raw_c, str):
                        content = raw_c
                    elif isinstance(raw_c, list):
                        text_parts = []
                        tool_names = []
                        for block in raw_c:
                            if not isinstance(block, dict):
                                continue
                            bt = block.get("type", "")
                            if bt == "text":
                                text_parts.append(block.get("text", ""))
                            elif bt == "tool_use":
                                tname = block.get("name", "tool")
                                tool_names.append(tname)
                                inp = block.get("input", {})
                                if tname in ("Edit", "MultiEdit"):
                                    fp = inp.get("file_path", "")
                                    if fp:
                                        fname = fp.rsplit("/", 1)[-1].rsplit("\\", 1)[-1]
                                        if fname not in changed_files:
                                            changed_files.append(fname)
                                    old_s = inp.get("old_string", "")
                                    new_s = inp.get("new_string", "")
                                    if old_s or new_s:
                                        ol = old_s.count("\n") + (1 if old_s else 0)
                                        nl = new_s.count("\n") + (1 if new_s else 0)
                                        added += max(0, nl - ol)
                                        removed += max(0, ol - nl)
                                elif tname == "Write":
                                    fp = inp.get("file_path", "")
                                    if fp:
                                        fname = fp.rsplit("/", 1)[-1].rsplit("\\", 1)[-1]
                                        if fname not in changed_files:
                                            changed_files.append(fname)
                                    wc = inp.get("content", "")
                                    if wc:
                                        added += wc.count("\n") + 1
                            elif bt == "tool_result":
                                block_type = "tool_result"
                                tr_content = block.get("content", "")
                                if isinstance(tr_content, str) and tr_content.strip():
                                    text_parts.append(tr_content)
                                elif isinstance(tr_content, list):
                                    for sub in tr_content:
                                        if isinstance(sub, dict) and sub.get("type") == "text":
                                            text_parts.append(sub.get("text", ""))
                        content = " ".join(text_parts)
                        if not content and tool_names:
                            content = "[" + ", ".join(tool_names) + "]"
                            block_type = "tool"
                        elif tool_names and not block_type:
                            block_type = "tool"
                        if role == "user" and block_type == "tool_result":
                            block_type = "tool_result"

                    # Skip user tool_result messages (system feedback)
                    if role == "user" and block_type == "tool_result":
                        continue
                    # For assistant messages: keep if they have text or changes,
                    # even if they also have tool_use blocks
                    cleaned = content.strip()
                    if not cleaned and block_type == "tool":
                        continue
                    if not cleaned:
                        continue
                    if role == "user" and _is_system_content(cleaned):
                        continue

                    ts_str = obj.get("timestamp", "")
                    uuid = obj.get("uuid", "")

                    messages.append({
                        "index": len(messages),
                        "line_number": line_num,
                        "role": role,
                        "preview": cleaned[:140],
                        "ts": ts_str,
                        "uuid": uuid,
                        "has_snapshot": False,  # filled in post-pass below
                        "changes": {
                            "added": added,
                            "removed": removed,
                            "files": changed_files[:5],
                        },
                    })

    except Exception:
        return {"messages": [], "has_snapshots": False, "title": path.stem}

    # Post-pass: mark messages that have an associated file-history snapshot.
    # Snapshots appear AFTER the message they reference in the JSONL, so the
    # single-pass lookup above always misses them.  Fix up now that all
    # snapshot entries have been collected.
    if snapshots:
        for m in messages:
            if m["uuid"] and m["uuid"] in snapshots:
                snap = snapshots[m["uuid"]]
                backups = snap.get("trackedFileBackups", {})
                m["has_snapshot"] = bool(backups and any(
                    (v.get("backupFileName") if isinstance(v, dict) else v)
                    for v in backups.values()
                ))

    # Fallback: if no explicit snapshots exist but there ARE Edit/Write
    # tool uses, check file-history directories for backup files created
    # by the daemon (which may not have flushed JSONL entries due to
    # session ID remap timing).  If backups exist, mark the first user
    # message as having a snapshot so the rewind UI is available.
    if not has_any_snapshot:
        has_edits = any(
            m.get("changes", {}).get("files")
            for m in messages
        )
        if has_edits:
            # Check if ANY file-history directory has backups for this session.
            # The session ID is the JSONL filename stem.
            sid = path.stem
            history_base = Path.home() / ".claude" / "file-history"
            # Check primary dir and scan for backup files
            found_backups = False
            for candidate_sid in [sid]:
                hdir = history_base / candidate_sid
                if hdir.is_dir() and any(hdir.iterdir()):
                    found_backups = True
                    break
            # Also check all history dirs for files matching our edited files
            if not found_backups and history_base.is_dir():
                for hdir in history_base.iterdir():
                    if hdir.is_dir() and any(hdir.iterdir()):
                        # Check if any backup in this dir could belong to our session
                        # by looking at recently modified dirs
                        try:
                            mtime = hdir.stat().st_mtime
                            # If this dir was modified within 60s of the JSONL
                            jsonl_mtime = path.stat().st_mtime
                            if abs(mtime - jsonl_mtime) < 60:
                                found_backups = True
                                break
                        except Exception:
                            pass
            if found_backups:
                has_any_snapshot = True
                # Mark the first user message as having a snapshot
                for m in messages:
                    if m["role"] == "user":
                        m["has_snapshot"] = True
                        break

    return {
        "messages": messages,
        "has_snapshots": has_any_snapshot,
        "title": custom_title or path.stem,
    }


# The timeline's "starts like injected context" test, shared via common/ (the
# engine's live-stream test is a different question; see common/transcripts.py).
from common.transcripts import is_timeline_system_content as _is_system_content  # noqa: E402


def all_sessions(summary_only: bool = False, project: str = "") -> list:
    # Filter out tombstoned (recently deleted) sessions so zombie .jsonl files
    # recreated by dying claude.exe processes never appear in the UI.
    # Also drop ai-title-only orphans — the CLI eagerly writes an ai-title
    # record on session start, and if the conversation never lands under that
    # filename (CLI dying post-delete; SDK remap edge cases) the leftover
    # 100-byte stub would surface as a bare-UUID empty chat.  The startup
    # sweep in app/__init__.py deletes them on disk; this filter is the
    # in-flight guard for orphans created between sweeps.  The check is a
    # stat() + 1-line read — only fires when the file is <500 bytes, so it
    # adds no cost on real chats.
    #
    # Listing cost (measured on a 1,009-transcript project, 2026-10-23): the
    # directory is read ONCE with os.scandir, whose DirEntry.stat() is free on
    # Windows (the listing carries the data), and that one stat feeds the
    # orphan check, the summary-cache key and the summary itself.  Summary
    # cache hits are served inline; only misses go to the thread pool, so a
    # fully warm listing spins up no threads at all.
    deleted_ids = _get_deleted_ids(project)
    utility_ids = _get_utility_ids(project)
    remapped_ids = _get_remapped_ids(project)
    sdir = _sessions_dir(project)
    hidden = deleted_ids | utility_ids | remapped_ids
    entries = []  # (Path, stat_result)
    try:
        with os.scandir(sdir) as it:
            for de in it:
                name = de.name
                if not name.endswith(".jsonl") or name.startswith("_"):
                    continue
                stem = name[:-6]
                if stem in hidden:
                    continue
                try:
                    if not de.is_file():
                        continue
                    st = de.stat()
                except OSError:
                    continue
                p = sdir / name
                if _is_orphan_cached(p, st):
                    continue
                entries.append((p, st))
    except OSError:
        entries = []

    if not summary_only:
        sessions = [load_session(p) for p, _ in entries]
        sessions.sort(key=lambda x: x["sort_ts"], reverse=True)
        return sessions

    access_map = _load_session_access_cached(sdir.name)
    sessions = []
    misses = []
    for p, st in entries:
        cached = _summary_cache.get(_summary_cache_key(p, st))
        if cached is not None:
            sessions.append(dict(_overlay_access_ts(cached, p, access_map)))
        else:
            misses.append((p, st))
    if misses:
        def _load(item):
            return load_session_summary(item[0], item[1], access_map)
        if len(misses) > 10:
            with ThreadPoolExecutor(max_workers=min(16, len(misses))) as pool:
                sessions.extend(pool.map(_load, misses))
        else:
            sessions.extend(_load(m) for m in misses)
    # Standby / marked Done (app/session_holds.py): only rows that have one
    # get a "hold" key, so a project without holds lists exactly as before.
    from .session_holds import overlay as _hold_overlay
    _hold_overlay(sessions, sdir)
    sessions.sort(key=lambda x: x["sort_ts"], reverse=True)
    return sessions
