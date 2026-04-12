"""
Session bookkeeping persistence — names, tombstones, utility tracking, and remaps.

Extracted from config.py to separate session-state management from application
configuration.  All functions accept an optional ``project`` parameter that
defaults to the currently active project via ``config._sessions_dir()``.
"""

import json
import threading
import time
from pathlib import Path

from .config import _sessions_dir

# ---------------------------------------------------------------------------
# User-set name store -- survives Claude Code's own auto-naming
# ---------------------------------------------------------------------------

def _names_file(project: str = "") -> Path:
    return _sessions_dir(project) / "_session_names.json"


def _load_names(project: str = "") -> dict:
    """Return {session_id: name} for all user-manually-set names."""
    try:
        return json.loads(_names_file(project).read_text(encoding="utf-8"))
    except Exception:
        return {}


def _save_name(session_id: str, name: str, project: str = "") -> None:
    """Persist a user-set name. Creates or updates _session_names.json.

    Snapshots the names file path ONCE to avoid writing to a different
    project's file if _active_project changes between load and save.
    """
    nf = _names_file(project)  # snapshot path
    try:
        names = json.loads(nf.read_text(encoding="utf-8"))
    except Exception:
        names = {}
    names[session_id] = name
    nf.write_text(json.dumps(names, indent=2, ensure_ascii=False), encoding="utf-8")


def _delete_name(session_id: str, project: str = "") -> None:
    """Remove a session from the user-names store (e.g. on delete)."""
    names = _load_names(project)
    if session_id in names:
        names.pop(session_id)
        _names_file(project).write_text(json.dumps(names, indent=2, ensure_ascii=False), encoding="utf-8")


def _remap_name(old_id: str, new_id: str, project: str = ""):
    """Move a user-set name from old_id to new_id. Returns the title or None."""
    names = _load_names(project)
    title = names.pop(old_id, None)
    if title:
        names[new_id] = title
        _names_file(project).write_text(json.dumps(names, indent=2, ensure_ascii=False), encoding="utf-8")
    return title


# ---------------------------------------------------------------------------
# Names cache
# ---------------------------------------------------------------------------

_names_cache: dict = {}  # key: project -> {"data": {}, "mtime": float}


def _load_names_cached(project: str = "") -> dict:
    """Load session names with caching based on file mtime.

    Cache is keyed by project so different projects don't collide.
    """
    nf = _names_file(project)
    try:
        mt = nf.stat().st_mtime
    except Exception:
        return {}
    cache_key = project
    entry = _names_cache.get(cache_key)
    if entry is None or mt != entry["mtime"]:
        _names_cache[cache_key] = {"data": _load_names(project), "mtime": mt}
    return _names_cache[cache_key]["data"]


# ---------------------------------------------------------------------------
# Deletion tombstones -- prevent zombie sessions from reappearing
# ---------------------------------------------------------------------------
# When a session is deleted, its ID is recorded here BEFORE the .jsonl file
# is removed.  all_sessions() filters out any session whose ID appears in this
# set, so even if a dying claude.exe recreates the file, it stays hidden.
# Tombstones older than 2 hours are auto-pruned on every load.

_TOMBSTONE_MAX_AGE = 7200  # seconds (2 hours)

# Lock protects the read-modify-write cycle on the tombstone file.
# Without this, _get_deleted_ids() (which prunes & saves) can race with
# _mark_deleted() and overwrite a freshly-written tombstone, causing the
# deleted session to reappear on the next page load.
_tombstone_lock = threading.Lock()


def _tombstone_file(project: str = "") -> Path:
    return _sessions_dir(project) / "_deleted_sessions.json"


def _load_tombstones(project: str = "") -> dict:
    """Return {session_id: unix_timestamp} of deleted sessions."""
    tf = _tombstone_file(project)
    try:
        data = json.loads(tf.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            return {}
        return data
    except Exception:
        return {}


def _save_tombstones(tombstones: dict, project: str = "") -> None:
    tf = _tombstone_file(project)
    tf.write_text(json.dumps(tombstones, ensure_ascii=False), encoding="utf-8")


def _prune_tombstones(tombstones: dict, project: str = "") -> dict:
    """Remove entries older than _TOMBSTONE_MAX_AGE, but only when the
    corresponding .jsonl file is also gone.  If the file still exists
    (e.g. Windows couldn't delete it due to a lock), the tombstone must
    stay so all_sessions() keeps hiding the zombie session."""
    now = time.time()
    sd = _sessions_dir(project)
    result = {}
    for sid, ts in tombstones.items():
        if now - ts < _TOMBSTONE_MAX_AGE:
            result[sid] = ts  # not expired yet — always keep
        elif (sd / f"{sid}.jsonl").exists():
            result[sid] = ts  # file still on disk — keep hiding it
    return result


def _mark_deleted(session_id: str, project: str = "") -> None:
    """Record a session as deleted (tombstone).  Must be called BEFORE
    unlinking the .jsonl file so the tombstone is in place before any race."""
    with _tombstone_lock:
        tombstones = _load_tombstones(project)
        tombstones[session_id] = time.time()
        tombstones = _prune_tombstones(tombstones, project)
        _save_tombstones(tombstones, project)


def _mark_deleted_bulk(session_ids: list, project: str = "") -> None:
    """Record multiple sessions as deleted in a single write."""
    with _tombstone_lock:
        tombstones = _load_tombstones(project)
        now = time.time()
        for sid in session_ids:
            tombstones[sid] = now
        tombstones = _prune_tombstones(tombstones, project)
        _save_tombstones(tombstones, project)


def _get_deleted_ids(project: str = "") -> set:
    """Return the set of session IDs that are tombstoned (recently deleted).

    This is a read-only helper — it never writes back to the tombstone file.
    Pruning (removing expired entries) happens lazily inside _mark_deleted()
    whenever a new tombstone is recorded.  Keeping _get_deleted_ids() write-free
    avoids any possibility of a save here clobbering a concurrent _mark_deleted().
    """
    tombstones = _load_tombstones(project)
    pruned = _prune_tombstones(tombstones, project)
    return set(pruned.keys())


# ---------------------------------------------------------------------------
# Utility session tracking -- hide system sessions (title, planner, etc.)
# ---------------------------------------------------------------------------
# When a utility session is spawned, its ID is recorded here.  When the SDK
# remaps the ID, the new ID is also recorded.  all_sessions() filters out
# any session whose ID appears in this set, so utility sessions never render
# in the sidebar, grid, or list view — even after page refresh or restart.

_UTILITY_MAX_AGE = 86400  # seconds (24 hours) — prune stale entries
_utility_lock = threading.Lock()


def _utility_file(project: str = "") -> Path:
    return _sessions_dir(project) / "_utility_sessions.json"


def _load_utility(project: str = "") -> dict:
    """Return {session_id: unix_timestamp} of utility sessions."""
    uf = _utility_file(project)
    try:
        data = json.loads(uf.read_text(encoding="utf-8"))
        if isinstance(data, dict):
            return data
    except Exception:
        pass
    return {}


def _save_utility(data: dict, project: str = "") -> None:
    _utility_file(project).write_text(
        json.dumps(data, ensure_ascii=False), encoding="utf-8"
    )


def _mark_utility(session_id: str, project: str = "") -> None:
    """Record a session as a utility/system session (hidden from UI).

    Called when starting title-generation, planner, or other ephemeral
    system sessions.  Also called on ID remap so the new ID is tracked.
    """
    with _utility_lock:
        data = _load_utility(project)
        data[session_id] = time.time()
        # Lazy prune — drop entries older than 24 h
        now = time.time()
        data = {
            sid: ts for sid, ts in data.items()
            if now - ts < _UTILITY_MAX_AGE
        }
        _save_utility(data, project)


def _get_utility_ids(project: str = "") -> set:
    """Return the set of session IDs marked as utility sessions."""
    data = _load_utility(project)
    now = time.time()
    return {sid for sid, ts in data.items() if now - ts < _UTILITY_MAX_AGE}


# ---------------------------------------------------------------------------
# Remapped session tracking -- hide stale temp-ID JSONL files
# ---------------------------------------------------------------------------
# When the SDK remaps a temp client UUID to its real server-assigned ID,
# the old temp-ID .jsonl file stays on disk.  We record the old ID here
# so all_sessions() filters it out, preventing a stale "sleeping" duplicate
# from appearing on page refresh — even if in-memory aliases haven't synced.

_REMAP_MAX_AGE = 86400  # 24 hours — old temp IDs are pruned after this
_remap_lock = threading.Lock()


def _remap_file(project: str = "") -> Path:
    return _sessions_dir(project) / "_remapped_sessions.json"


def _load_remaps(project: str = "") -> dict:
    """Return {old_session_id: {"new_id": str, "ts": float}} of remapped sessions."""
    rf = _remap_file(project)
    try:
        data = json.loads(rf.read_text(encoding="utf-8"))
        if isinstance(data, dict):
            return data
    except Exception:
        pass
    return {}


def _save_remaps(data: dict, project: str = "") -> None:
    _remap_file(project).write_text(
        json.dumps(data, ensure_ascii=False), encoding="utf-8"
    )


def _mark_remapped(old_session_id: str, new_session_id: str = "", project: str = "") -> None:
    """Record an old (pre-remap) session ID so its JSONL is hidden."""
    with _remap_lock:
        data = _load_remaps(project)
        data[old_session_id] = {"new_id": new_session_id, "ts": time.time()}
        # Lazy prune — drop entries older than 24 h whose .jsonl is gone
        now = time.time()
        sd = _sessions_dir(project)
        data = {
            sid: entry for sid, entry in data.items()
            if now - entry.get("ts", 0) < _REMAP_MAX_AGE
            or (sd / f"{sid}.jsonl").exists()
        }
        _save_remaps(data, project)


def _get_remapped_ids(project: str = "") -> set:
    """Return the set of old session IDs that were remapped (should be hidden)."""
    data = _load_remaps(project)
    now = time.time()
    return {sid for sid, entry in data.items()
            if now - entry.get("ts", 0) < _REMAP_MAX_AGE}


def _resolve_remapped_id(old_session_id: str, project: str = "") -> str | None:
    """Look up the new (canonical) ID for a remapped session, or None."""
    data = _load_remaps(project)
    entry = data.get(old_session_id)
    if entry and entry.get("new_id"):
        return entry["new_id"]
    return None
