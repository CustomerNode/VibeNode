"""
Configuration, path helpers, and session-name persistence.
"""

# this comment means nothing

import json
import time
from pathlib import Path

# ---------------------------------------------------------------------------
# Path constants
# ---------------------------------------------------------------------------

import os as _os

_CLAUDE_PROJECTS = Path.home() / ".claude" / "projects"
_active_project: str = ""   # encoded dir name; empty = auto-detect
_VIBENODE_DIR = Path(__file__).resolve().parent.parent  # always the VibeNode repo

# Utility sessions (title gen, AI planner) use this cwd so their JSONL files
# land in a separate project directory, never polluting the user's project.
_SYSTEM_UTILITY_CWD = str(Path.home() / ".claude" / "_system")
_SYSTEM_UTILITY_MAX_AGE = 86400  # 24 hours


def _cleanup_system_sessions() -> None:
    """Delete utility session JSONL files older than 24 hours."""
    import time as _time
    # The encoded project dir for _SYSTEM_UTILITY_CWD
    encoded = _SYSTEM_UTILITY_CWD.replace("\\", "-").replace("/", "-").replace(":", "-")
    sys_dir = _CLAUDE_PROJECTS / encoded
    if not sys_dir.is_dir():
        return
    now = _time.time()
    for f in sys_dir.glob("*.jsonl"):
        try:
            if now - f.stat().st_mtime > _SYSTEM_UTILITY_MAX_AGE:
                f.unlink()
        except Exception:
            pass
# Allow tests to override config path via env var
_KANBAN_CONFIG_FILE = Path(_os.environ["VIBENODE_CONFIG"]) if _os.environ.get("VIBENODE_CONFIG") else _VIBENODE_DIR / "kanban_config.json"


# ---------------------------------------------------------------------------
# Kanban config store
# ---------------------------------------------------------------------------

def get_kanban_config() -> dict:
    """Load kanban configuration from kanban_config.json.

    Missing keys are filled from defaults so callers always see the full set.
    """
    try:
        stored = json.loads(_KANBAN_CONFIG_FILE.read_text(encoding="utf-8"))
        merged = _kanban_config_defaults()
        merged.update(stored)
        # Migrate old key
        if "kanban_auto_advance" in stored and "auto_advance_to_validating" not in stored:
            merged["auto_advance_to_validating"] = bool(stored["kanban_auto_advance"])
        return merged
    except Exception:
        return _kanban_config_defaults()


def _kanban_config_defaults() -> dict:
    return {
        "kanban_backend": "sqlite",
        "supabase_url": "",
        "supabase_secret_key": "",
        "supabase_publishable_key": "",
        "kanban_depth_limit": 5,
        # ── Behavior preferences ──
        # Session starts → task moves to Working
        "auto_start_on_session": True,
        # Child Working → parent moves from Not Started to Working
        "auto_parent_working": True,
        # Child Remediating → parent reverts from Complete to Remediating
        "auto_parent_reopen": True,
        # All children/sessions done → task moves to Validating
        "auto_advance_to_validating": False,
    }


def save_kanban_config(config: dict) -> None:
    """Save kanban configuration to kanban_config.json."""
    _KANBAN_CONFIG_FILE.write_text(
        json.dumps(config, indent=2, ensure_ascii=False), encoding="utf-8"
    )


# ---------------------------------------------------------------------------
# Active project getter / setter
# ---------------------------------------------------------------------------

def get_active_project() -> str:
    return _active_project


def set_active_project(value: str) -> None:
    global _active_project
    _active_project = value


# ---------------------------------------------------------------------------
# Directory helpers
# ---------------------------------------------------------------------------

def _sessions_dir() -> Path:
    """Return the active project's session directory, auto-detecting if needed.

    Priority:
      1. Explicit _active_project (set by UI project picker)
      2. Match based on server working directory (_VIBENODE_DIR)
      3. Most recently modified .jsonl across all projects
    """
    global _active_project
    if _active_project:
        if not _active_project.startswith("subagents"):
            p = _CLAUDE_PROJECTS / _active_project
            if p.is_dir():
                return p
        _active_project = ""

    # Derive from server's own repo path — Claude encodes paths with dashes
    # e.g. C:\Users\foo\Documents\ClaudeGUI -> C--Users-foo-Documents-ClaudeGUI
    repo_path = str(_VIBENODE_DIR).replace("\\", "-").replace("/", "-").replace(":", "-")
    for d in _CLAUDE_PROJECTS.iterdir():
        if not d.is_dir() or d.name.startswith("subagents") or d.name.startswith("_"):
            continue
        # Case-insensitive match — Claude's encoding may differ in case
        if d.name.lower() == repo_path.lower():
            _active_project = d.name
            return d

    # Fallback: most recently modified .jsonl
    best, best_ts = None, 0.0
    if not _CLAUDE_PROJECTS.is_dir():
        return _CLAUDE_PROJECTS
    for d in _CLAUDE_PROJECTS.iterdir():
        if not d.is_dir() or d.name.startswith("subagents") or d.name.startswith("_"):
            continue
        for f in d.glob("*.jsonl"):
            if f.stat().st_mtime > best_ts:
                best_ts = f.stat().st_mtime
                best = d
    if best:
        _active_project = best.name
        return best
    return _CLAUDE_PROJECTS


def _names_file() -> Path:
    return _sessions_dir() / "_session_names.json"


def _tombstone_file() -> Path:
    return _sessions_dir() / "_deleted_sessions.json"


def _decode_project(encoded: str) -> str:
    """Convert encoded project name back to filesystem path.
    Handles ambiguity where '-' could be '/' or '_' or '-' in the original."""
    if "--" not in encoded:
        return encoded
    drive, rest = encoded.split("--", 1)
    simple = drive + ":/" + rest.replace("-", "/")
    if Path(simple).is_dir():
        return simple
    # Rebuild path segment by segment, checking which variant exists
    parts = rest.split("-")
    path = drive + ":/"
    i = 0
    while i < len(parts):
        found = False
        for lookahead in range(min(4, len(parts) - i), 0, -1):
            for sep in ['_', '-', '/']:
                candidate = sep.join(parts[i:i+lookahead])
                test_path = path + candidate
                if Path(test_path).is_dir():
                    path = test_path + "/"
                    i += lookahead
                    found = True
                    break
            if found:
                break
        if not found:
            path += parts[i] + "/"
            i += 1
    result = path.rstrip("/")
    # Final validation: if the reconstructed path isn't a real directory,
    # fall back to scanning Documents for a matching encoded name
    if not Path(result).is_dir():
        docs = Path.home() / "Documents"
        if docs.is_dir():
            try:
                for child in docs.iterdir():
                    if not child.is_dir():
                        continue
                    enc = str(child).replace("\\", "/").replace(":", "-").replace("/", "-")
                    if enc == encoded:
                        return str(child)
                    try:
                        for sub in child.iterdir():
                            if not sub.is_dir():
                                continue
                            enc2 = str(sub).replace("\\", "/").replace(":", "-").replace("/", "-")
                            if enc2 == encoded:
                                return str(sub)
                    except (PermissionError, OSError):
                        continue
            except (PermissionError, OSError):
                pass
    return result


# ---------------------------------------------------------------------------
# User-set name store -- survives Claude Code's own auto-naming
# ---------------------------------------------------------------------------

def _load_names() -> dict:
    """Return {session_id: name} for all user-manually-set names."""
    try:
        return json.loads(_names_file().read_text(encoding="utf-8"))
    except Exception:
        return {}


def _save_name(session_id: str, name: str) -> None:
    """Persist a user-set name. Creates or updates _session_names.json.

    Snapshots the names file path ONCE to avoid writing to a different
    project's file if _active_project changes between load and save.
    """
    nf = _names_file()  # snapshot path
    try:
        names = json.loads(nf.read_text(encoding="utf-8"))
    except Exception:
        names = {}
    names[session_id] = name
    nf.write_text(json.dumps(names, indent=2, ensure_ascii=False), encoding="utf-8")


def _delete_name(session_id: str) -> None:
    """Remove a session from the user-names store (e.g. on delete)."""
    names = _load_names()
    if session_id in names:
        names.pop(session_id)
        _names_file().write_text(json.dumps(names, indent=2, ensure_ascii=False), encoding="utf-8")


def _remap_name(old_id: str, new_id: str):
    """Move a user-set name from old_id to new_id. Returns the title or None."""
    names = _load_names()
    title = names.pop(old_id, None)
    if title:
        names[new_id] = title
        _names_file().write_text(json.dumps(names, indent=2, ensure_ascii=False), encoding="utf-8")
    return title


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
import threading as _threading
_tombstone_lock = _threading.Lock()


def _load_tombstones() -> dict:
    """Return {session_id: unix_timestamp} of deleted sessions."""
    tf = _tombstone_file()
    try:
        data = json.loads(tf.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            return {}
        return data
    except Exception:
        return {}


def _save_tombstones(tombstones: dict) -> None:
    tf = _tombstone_file()
    tf.write_text(json.dumps(tombstones, ensure_ascii=False), encoding="utf-8")


def _prune_tombstones(tombstones: dict) -> dict:
    """Remove entries older than _TOMBSTONE_MAX_AGE, but only when the
    corresponding .jsonl file is also gone.  If the file still exists
    (e.g. Windows couldn't delete it due to a lock), the tombstone must
    stay so all_sessions() keeps hiding the zombie session."""
    now = time.time()
    sd = _sessions_dir()
    result = {}
    for sid, ts in tombstones.items():
        if now - ts < _TOMBSTONE_MAX_AGE:
            result[sid] = ts  # not expired yet — always keep
        elif (sd / f"{sid}.jsonl").exists():
            result[sid] = ts  # file still on disk — keep hiding it
    return result


def _mark_deleted(session_id: str) -> None:
    """Record a session as deleted (tombstone).  Must be called BEFORE
    unlinking the .jsonl file so the tombstone is in place before any race."""
    with _tombstone_lock:
        tombstones = _load_tombstones()
        tombstones[session_id] = time.time()
        tombstones = _prune_tombstones(tombstones)
        _save_tombstones(tombstones)


def _mark_deleted_bulk(session_ids: list) -> None:
    """Record multiple sessions as deleted in a single write."""
    with _tombstone_lock:
        tombstones = _load_tombstones()
        now = time.time()
        for sid in session_ids:
            tombstones[sid] = now
        tombstones = _prune_tombstones(tombstones)
        _save_tombstones(tombstones)


def _get_deleted_ids() -> set:
    """Return the set of session IDs that are tombstoned (recently deleted).

    This is a read-only helper — it never writes back to the tombstone file.
    Pruning (removing expired entries) happens lazily inside _mark_deleted()
    whenever a new tombstone is recorded.  Keeping _get_deleted_ids() write-free
    avoids any possibility of a save here clobbering a concurrent _mark_deleted().
    """
    tombstones = _load_tombstones()
    pruned = _prune_tombstones(tombstones)
    return set(pruned.keys())


# ---------------------------------------------------------------------------
# Utility session tracking -- hide system sessions (title, planner, etc.)
# ---------------------------------------------------------------------------
# When a utility session is spawned, its ID is recorded here.  When the SDK
# remaps the ID, the new ID is also recorded.  all_sessions() filters out
# any session whose ID appears in this set, so utility sessions never render
# in the sidebar, grid, or list view — even after page refresh or restart.

_UTILITY_MAX_AGE = 86400  # seconds (24 hours) — prune stale entries
_utility_lock = _threading.Lock()


def _utility_file() -> Path:
    return _sessions_dir() / "_utility_sessions.json"


def _load_utility() -> dict:
    """Return {session_id: unix_timestamp} of utility sessions."""
    uf = _utility_file()
    try:
        data = json.loads(uf.read_text(encoding="utf-8"))
        if isinstance(data, dict):
            return data
    except Exception:
        pass
    return {}


def _save_utility(data: dict) -> None:
    _utility_file().write_text(
        json.dumps(data, ensure_ascii=False), encoding="utf-8"
    )


def _mark_utility(session_id: str) -> None:
    """Record a session as a utility/system session (hidden from UI).

    Called when starting title-generation, planner, or other ephemeral
    system sessions.  Also called on ID remap so the new ID is tracked.
    """
    with _utility_lock:
        data = _load_utility()
        data[session_id] = time.time()
        # Lazy prune — drop entries older than 24 h
        now = time.time()
        data = {
            sid: ts for sid, ts in data.items()
            if now - ts < _UTILITY_MAX_AGE
        }
        _save_utility(data)


def _get_utility_ids() -> set:
    """Return the set of session IDs marked as utility sessions."""
    data = _load_utility()
    now = time.time()
    return {sid for sid, ts in data.items() if now - ts < _UTILITY_MAX_AGE}


# ---------------------------------------------------------------------------
# Project display-name store (separate from session names)
# ---------------------------------------------------------------------------

_PROJECT_NAMES_FILE = _CLAUDE_PROJECTS / "_project_names.json"


def _load_project_names() -> dict:
    try:
        return json.loads(_PROJECT_NAMES_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _save_project_names(names: dict):
    _PROJECT_NAMES_FILE.write_text(json.dumps(names, indent=2), encoding="utf-8")


# ---------------------------------------------------------------------------
# Caches
# ---------------------------------------------------------------------------

_summary_cache: dict = {}  # key: (path_str, mtime, size) -> summary dict
_names_cache: dict = {"data": {}, "mtime": 0}  # cached session names


def _load_names_cached() -> dict:
    """Load session names with caching based on file mtime."""
    nf = _names_file()
    try:
        mt = nf.stat().st_mtime
    except Exception:
        return {}
    if mt != _names_cache["mtime"]:
        _names_cache["data"] = _load_names()
        _names_cache["mtime"] = mt
    return _names_cache["data"]


# ---------------------------------------------------------------------------
# Utilities
# ---------------------------------------------------------------------------

def _format_size(file_bytes: int) -> str:
    if file_bytes < 1024:
        return f"{file_bytes} B"
    elif file_bytes < 1024 * 1024:
        return f"{file_bytes / 1024:.0f} KB"
    return f"{file_bytes / (1024*1024):.0f} MB"
