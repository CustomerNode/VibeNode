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


def _unmark_deleted(session_id: str, project: str = "") -> None:
    """Remove a session's tombstone.  Used when restoring from trash so
    all_sessions() stops hiding the resurrected session."""
    with _tombstone_lock:
        tombstones = _load_tombstones(project)
        if session_id in tombstones:
            tombstones.pop(session_id)
            _save_tombstones(tombstones, project)


# ---------------------------------------------------------------------------
# Soft-delete trash -- recoverable session deletion
# ---------------------------------------------------------------------------
# Session deletion used to permanently unlink the .jsonl, with no undo.  A
# single misclick (easy while clicking around the UI) lost an entire
# transcript forever.  move_to_trash() relocates the .jsonl into a per-project
# ``_trash/`` folder and records the deletion time + saved name in
# ``_trash/_trash_index.json`` so deletes are reversible.  all_sessions()
# globs ``*.jsonl`` non-recursively and skips ``_``-prefixed stems, so trashed
# files never reappear in the UI.  Entries are pruned lazily (and their files
# removed) on the next trash operation, per the user's retention policy
# (default Forever) and honoring per-entry grandfather protection
# (``protected_until``).  See ``_retention_seconds`` and ``_prune_trash``.

_TRASH_MAX_AGE = 2592000  # seconds (30 days) — legacy fallback / test hook only
_trash_lock = threading.Lock()

# ---------------------------------------------------------------------------
# Retention policy resolution
# ---------------------------------------------------------------------------
# The active retention window is driven by the user's selection in the
# "Recently Deleted" modal, stored as ``session_retention_days`` in the
# daemon-owned ``~/.claude/gui_ui_prefs.json``.  This module is a READ-ONLY
# consumer of that file (one writer = the daemon's PermissionManager; many
# readers).  Reading the file directly (rather than via the daemon socket)
# means pruning still works when the daemon is down and avoids a socket
# round-trip on every trash op.
#
# SAFETY (load-bearing): every path that resolves retention treats a missing
# key / non-int / bool / non-positive / unreadable value as Forever (36500),
# NEVER 30.  Time-based deletion must require a deliberate user selection.
_RETENTION_DEFAULT_DAYS = 36500  # "Forever"
_RETENTION_PREFS_KEY = "session_retention_days"
_UI_PREFS_PATH = Path.home() / ".claude" / "gui_ui_prefs.json"  # daemon-owned; READ only


def _retention_days() -> int:
    """Resolve the active retention window in days.

    Returns ``_RETENTION_DEFAULT_DAYS`` (Forever) on any missing/corrupt/
    non-positive value.  30 is only ever returned if explicitly chosen by
    the user.  Read-only consumer of the daemon-owned gui_ui_prefs.json.
    """
    try:
        data = json.loads(_UI_PREFS_PATH.read_text(encoding="utf-8"))
        val = data.get(_RETENTION_PREFS_KEY) if isinstance(data, dict) else None
        if isinstance(val, bool):          # bool is an int subclass — reject
            return _RETENTION_DEFAULT_DAYS
        if isinstance(val, int) and val > 0:
            return val
    except Exception:
        pass
    return _RETENTION_DEFAULT_DAYS


def _retention_seconds() -> int:
    return _retention_days() * 86400


def _trash_dir(project: str = "") -> Path:
    return _sessions_dir(project) / "_trash"


def _trash_index_file(project: str = "") -> Path:
    return _trash_dir(project) / "_trash_index.json"


def _load_trash_index(project: str = "") -> dict:
    """Return {session_id: {deleted_at: float, name: str}} for trashed sessions."""
    try:
        data = json.loads(_trash_index_file(project).read_text(encoding="utf-8"))
        if isinstance(data, dict):
            return data
    except Exception:
        pass
    return {}


def _save_trash_index(index: dict, project: str = "") -> None:
    # The _trash/ folder is created lazily by move_to_trash, but list/restore/
    # purge can call this before any session has been trashed — ensure the
    # directory exists so the write never fails with FileNotFoundError.
    f = _trash_index_file(project)
    try:
        f.parent.mkdir(parents=True, exist_ok=True)
    except Exception:
        pass
    f.write_text(json.dumps(index, ensure_ascii=False, indent=2), encoding="utf-8")


def _prune_trash(index: dict, project: str = "") -> dict:
    """Drop entries past the active retention window (honoring per-entry
    grandfather protection) and unlink their backing files.

    An entry is purged only when BOTH conditions hold:
      * it has aged past the active window: ``now - deleted_at >= max_age``
      * it is no longer grandfather-protected: ``now >= protected_until``
    ``protected_until`` (epoch) is stamped by ``reconcile_retention_on_shorten``
    so a policy change can never instantly delete an item the prior policy
    promised to keep.  Missing ``protected_until`` is treated as 0 (unprotected).

    Test/back-compat: an explicit monkeypatch of ``_TRASH_MAX_AGE`` wins so the
    existing ``test_expired_entries_are_pruned`` (which sets it to 10) keeps
    working.  Otherwise the live user policy applies (default Forever).
    """
    now = time.time()
    max_age = _TRASH_MAX_AGE if _TRASH_MAX_AGE != 2592000 else _retention_seconds()
    td = _trash_dir(project)
    result = {}
    for sid, meta in index.items():
        meta = meta or {}
        ts = meta.get("deleted_at", 0)
        protected_until = meta.get("protected_until", 0) or 0
        aged_out = (now - ts) >= max_age
        unprotected = now >= protected_until
        if aged_out and unprotected:
            try:
                (td / f"{sid}.jsonl").unlink()
            except Exception:
                pass
        else:
            result[sid] = meta
    return result


def move_to_trash(session_id: str, project: str = "", name: str = "",
                  retries: int = 5, delay: float = 0.2) -> bool:
    """Move a session's .jsonl into the per-project ``_trash/`` folder so the
    delete is recoverable.

    Returns True if a file was trashed, False if there was no .jsonl to move
    (already gone / never created) or every retry hit a Windows file lock.
    Retries on PermissionError just like _unlink_with_retry so an AV/CLI hold
    on the handle doesn't drop us straight into the hard-delete fallback.
    """
    src = _sessions_dir(project) / f"{session_id}.jsonl"
    with _trash_lock:
        td = _trash_dir(project)
        try:
            td.mkdir(parents=True, exist_ok=True)
        except Exception:
            return False
        # Opportunistic prune so the trash folder self-bounds over time.
        index = _prune_trash(_load_trash_index(project), project)
        dest = td / f"{session_id}.jsonl"
        moved = False
        for attempt in range(retries):
            if not src.exists():
                break
            try:
                if dest.exists():
                    dest.unlink()
                src.replace(dest)
                moved = True
                break
            except PermissionError:
                if attempt < retries - 1:
                    time.sleep(delay)
                    continue
                # Last-ditch: copy bytes then unlink the source.
                try:
                    dest.write_bytes(src.read_bytes())
                    src.unlink()
                    moved = True
                except Exception:
                    moved = False
                break
            except FileNotFoundError:
                break
            except Exception:
                # Cross-device or other failure — try copy+unlink once.
                try:
                    dest.write_bytes(src.read_bytes())
                    src.unlink()
                    moved = True
                except Exception:
                    moved = False
                break
        if moved:
            index[session_id] = {"deleted_at": time.time(), "name": name or ""}
            _save_trash_index(index, project)
        return moved


def list_trash(project: str = "") -> list:
    """Return [{id, name, deleted_at, size}] for restorable trashed sessions,
    newest-deleted first.  Prunes expired entries as a side effect."""
    with _trash_lock:
        original = _load_trash_index(project)
        index = _prune_trash(original, project)
        # Only persist when pruning actually removed entries — avoids creating
        # an empty _trash/ folder just because the user opened the trash view.
        if len(index) != len(original):
            _save_trash_index(index, project)
        td = _trash_dir(project)
        # Resolve the active window once for purge-date computation.  Under
        # "Forever", purge_at is None (never auto-deletes).
        ret = _retention_seconds()
        forever = ret >= _RETENTION_DEFAULT_DAYS * 86400
        out = []
        for sid, meta in index.items():
            f = td / f"{sid}.jsonl"
            if not f.exists():
                continue
            try:
                size = f.stat().st_size
            except Exception:
                size = 0
            meta = meta or {}
            deleted_at = meta.get("deleted_at", 0)
            protected_until = meta.get("protected_until", 0) or 0
            # The effective purge time is the later of the window expiry and
            # any grandfather protection (matches _prune_trash's AND-of-both).
            purge_at = None if forever else max(deleted_at + ret, protected_until)
            out.append({
                "id": sid,
                "name": meta.get("name", ""),
                "deleted_at": deleted_at,
                "size": size,
                "purge_at": purge_at,
            })
    out.sort(key=lambda e: e.get("deleted_at", 0), reverse=True)
    return out


def restore_from_trash(session_id: str, project: str = ""):
    """Move a trashed .jsonl back into the sessions dir and drop its trash
    index entry.  Returns the saved name (possibly '') on success, or None if
    there was nothing to restore (or a live file already occupies the slot).
    Callers are responsible for clearing the tombstone and re-saving the name.
    """
    with _trash_lock:
        src = _trash_dir(project) / f"{session_id}.jsonl"
        if not src.exists():
            return None
        dest = _sessions_dir(project) / f"{session_id}.jsonl"
        if dest.exists():
            # A live session already owns this id — refuse to clobber it.
            return None
        try:
            src.replace(dest)
        except Exception:
            try:
                dest.write_bytes(src.read_bytes())
                src.unlink()
            except Exception:
                return None
        index = _load_trash_index(project)
        meta = index.pop(session_id, {}) or {}
        _save_trash_index(index, project)
    return meta.get("name", "")


def purge_from_trash(session_id: str, project: str = "") -> bool:
    """Permanently delete a single trashed session (file + index entry)."""
    with _trash_lock:
        index = _load_trash_index(project)
        existed = session_id in index
        index.pop(session_id, None)
        _save_trash_index(index, project)
        try:
            (_trash_dir(project) / f"{session_id}.jsonl").unlink()
            existed = True
        except FileNotFoundError:
            pass
        except Exception:
            pass
    return existed


def reconcile_retention_on_shorten(old_days: int, new_days: int, project: str = "") -> int:
    """Grandfather existing trash when the retention policy is shortened.

    When ``new_days < old_days``, stamp every existing trash entry in the
    given project with ``protected_until = deleted_at + old_window`` where it
    is missing or smaller (idempotent via ``max``).  This guarantees that
    shortening the policy can NEVER instantly delete an item the prior policy
    promised to keep — the item only ages out once it passes BOTH its
    grandfathered protection AND the new (shorter) active window.

    No-op (returns 0) when the policy is unchanged or lengthened.  Returns the
    number of entries stamped.  Never raises — a locked index just means
    grandfathering is skipped this run (re-running is safe and idempotent).
    """
    try:
        old_days = int(old_days)
        new_days = int(new_days)
    except (TypeError, ValueError):
        return 0
    if new_days >= old_days:
        return 0
    old_window = old_days * 86400
    stamped = 0
    with _trash_lock:
        try:
            index = _load_trash_index(project)
        except Exception:
            return 0
        if not index:
            return 0
        changed = False
        for sid, meta in index.items():
            meta = meta or {}
            deleted_at = meta.get("deleted_at", 0)
            new_protect = deleted_at + old_window
            existing = meta.get("protected_until", 0) or 0
            if new_protect > existing:
                meta["protected_until"] = new_protect
                index[sid] = meta
                changed = True
                stamped += 1
        if changed:
            try:
                _save_trash_index(index, project)
            except Exception:
                return 0
    return stamped


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


# ---------------------------------------------------------------------------
# Session access tracking -- "last interacted with" bookkeeping for sort
# ---------------------------------------------------------------------------
# The sidebar's date sort uses ``effective_ts`` (see app/sessions.py) which
# is ``max(last_message_ts, file_mtime, last_access_ts)``.  This file holds
# the third component: the wall-clock time of the most recent UI interaction
# with a session, recorded server-side so it survives page refresh and is
# consistent across browsers.
#
# Hooks that bump access_ts:
#   - GET /api/session/<id>           (user opens the session)
#   - POST /api/session/<id>/touch    (explicit "I clicked this" signal)
#   - WS  send_message                (user typed in the live panel)
#   - WS  start_session               (fresh session started)
#
# Without this layer, a session that the user *interacts with* but doesn't
# *write to* (resume that gets SDK-remapped, view-only reads, daemon-side
# writes routed to a different file) stays frozen at its last on-disk
# activity timestamp and never bubbles up — the exact failure mode behind
# the Aras-session report on 2026-05-27.
#
# Lazy prune drops entries older than _ACCESS_MAX_AGE whose .jsonl is gone.

_ACCESS_MAX_AGE = 7776000  # seconds (90 days)
_access_lock = threading.Lock()

# Read-through cache: project -> {"data": {sid: ts}, "mtime": float}
_access_cache: dict = {}


def _access_file(project: str = "") -> Path:
    return _sessions_dir(project) / "_session_access.json"


def _load_session_access(project: str = "") -> dict:
    """Return ``{session_id: unix_ts}`` of recorded UI interactions."""
    af = _access_file(project)
    try:
        data = json.loads(af.read_text(encoding="utf-8"))
        if isinstance(data, dict):
            return data
    except Exception:
        pass
    return {}


def _load_session_access_cached(project: str = "") -> dict:
    """Mtime-keyed read-through cache for the per-project access map.

    Sidebar renders call this once per session summary so the cost has to
    stay sub-ms even with hundreds of sessions.  Cache key is the access
    file's mtime; writes by ``_record_session_access`` bump that mtime and
    naturally invalidate the cache on the next read.
    """
    af = _access_file(project)
    try:
        mt = af.stat().st_mtime
    except Exception:
        return {}
    entry = _access_cache.get(project)
    if entry is None or mt != entry["mtime"]:
        _access_cache[project] = {"data": _load_session_access(project),
                                   "mtime": mt}
    return _access_cache[project]["data"]


def _record_session_access(session_id: str, project: str = "") -> None:
    """Record ``now()`` as the last-interaction time for ``session_id``.

    Cheap and idempotent — safe to call from request handlers and WS
    events.  Failures are swallowed because this is a sort hint, not
    load-bearing state."""
    if not session_id:
        return
    with _access_lock:
        try:
            data = _load_session_access(project)
            now = time.time()
            data[session_id] = now
            # Lazy prune: drop entries that are both old AND have no .jsonl
            # on disk anymore, so the file can't grow unbounded across years.
            sd = _sessions_dir(project)
            pruned = {
                sid: ts for sid, ts in data.items()
                if (now - ts) < _ACCESS_MAX_AGE
                or (sd / f"{sid}.jsonl").exists()
            }
            _access_file(project).write_text(
                json.dumps(pruned, ensure_ascii=False), encoding="utf-8"
            )
        except Exception:
            pass
