"""
Configuration, path helpers, and project management.

Session bookkeeping (names, tombstones, utility tracking, remaps) has been
extracted to ``session_store.py``.  This module retains path constants,
kanban config, project getter/setter, directory helpers, project display-name
persistence, and format utilities.
"""

import json
import sys
import time
from pathlib import Path

# ---------------------------------------------------------------------------
# Path constants
# ---------------------------------------------------------------------------

import os as _os

from common.paths import encode_cwd as _common_encode_cwd  # noqa: E402
from common.paths import resolve_sessions_dir as _resolve_sessions_dir  # noqa: E402

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
    encoded = _encode_cwd(_SYSTEM_UTILITY_CWD)
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


def _is_aititle_only_orphan(path) -> bool:
    """Return True when *path* is a JSONL whose only records are metadata.

    The Claude CLI eagerly writes ``{"type":"ai-title", ...}`` to the
    project JSONL when it auto-generates a session title.  It may also
    write ``{"type":"file-history-snapshot", ...}`` records during session
    bootstrap (tracking files in the cwd before the first user turn).  If
    the conversation then fails to land on disk under that same filename —
    e.g. the user closes the tab before typing, the CLI subprocess dies
    mid-write and silently recreates the file after `api_delete` unlinked
    it, or the SDK remaps the session to a different on-disk ID — the
    user is left with a stub file containing only an ``ai-title`` record
    and (optionally) file-history-snapshot records.  ``app/sessions.py``
    doesn't recognise these as conversation types, so the chat surfaces in
    the sidebar with the bare UUID as its display name.

    These orphans are safe to delete: they have no user/assistant turns
    and are not reachable from any live session (active sessions append
    user/assistant records as soon as the first turn flushes).

    Scan bounded to 100 KB.  Real sessions with even one user/assistant
    turn easily exceed this in practice.  The largest stub-with-snapshots
    observed in the wild was 73 KB (the file-history-snapshot machinery
    can attach dozens of records before the first turn).  Files larger
    than the cap return False without scanning so the per-request
    ``all_sessions()`` filter stays cheap on real sidebar contents.

    To declare an orphan we require ``ai-title`` to be present somewhere
    in the file.  A pure file-history-snapshot file (no title) is left
    alone — the function specifically targets the CLI's title-eager
    leak path; it is not a generic "delete any file without messages"
    sweeper.  Malformed lines also cause us to bail out (return False)
    because we can't be sure what they contain.
    """
    import json as _json
    try:
        if path.stat().st_size > 100_000:
            return False
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            lines = [ln.strip() for ln in fh.readlines() if ln.strip()]
    except Exception:
        return False
    if not lines:
        return False
    saw_ai_title = False
    for line in lines:
        try:
            obj = _json.loads(line)
        except Exception:
            return False  # malformed line — be conservative, don't auto-delete
        t = obj.get("type")
        if t == "ai-title":
            saw_ai_title = True
        elif t == "file-history-snapshot":
            continue
        else:
            # Any user/assistant/summary/tool_*/etc. record => real session
            return False
    return saw_ai_title


def _cleanup_aititle_orphans() -> int:
    """Delete ai-title-only orphan JSONL files across every project directory.

    Run at startup (see ``app/__init__.py``) to keep the project directories
    free of these zero-message stubs.  Returns the count of files removed
    for logging.

    Skips the encoded ``_SYSTEM_UTILITY_CWD`` project — those files are
    legitimate title-generation transcripts and are pruned by their own
    age-based sweep above.
    """
    if not _CLAUDE_PROJECTS.is_dir():
        return 0
    sys_encoded = _encode_cwd(_SYSTEM_UTILITY_CWD)
    removed = 0
    for d in _CLAUDE_PROJECTS.iterdir():
        if not d.is_dir():
            continue
        if d.name == sys_encoded or d.name.startswith("subagents") or d.name.startswith("_"):
            continue
        for f in d.glob("*.jsonl"):
            if _is_aititle_only_orphan(f):
                try:
                    f.unlink()
                    removed += 1
                except Exception:
                    pass
    return removed
# ---------------------------------------------------------------------------
# Kanban config store: lives in common/config.py (shared with the Session
# Engine, which must not import this package).  PERF-CRITICAL #11 (the 10s
# cache + invalidation on save) is implemented THERE; do not re-implement it
# here.  The module-level state (``_KANBAN_CONFIG_FILE`` and the cache fields)
# is forwarded, reads and writes, so ``app.config._KANBAN_CONFIG_FILE = x``,
# ``app.config._kanban_config_cache = None`` and monkeypatching either still
# act on the one real cache.
# ---------------------------------------------------------------------------
from common import config as _common_config  # noqa: E402
from common.config import (  # noqa: E402, F401
    get_kanban_config,
    save_kanban_config,
    _kanban_config_defaults,
)

_FORWARDED_TO_COMMON_CONFIG = frozenset({
    "_KANBAN_CONFIG_FILE",
    "_kanban_config_cache",
    "_kanban_config_cache_time",
    "_KANBAN_CONFIG_CACHE_TTL",
})


def __getattr__(name):
    if name in _FORWARDED_TO_COMMON_CONFIG:
        return getattr(_common_config, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


class _ForwardingConfigModule(type(sys)):
    def __setattr__(self, name, value):
        if name in _FORWARDED_TO_COMMON_CONFIG:
            setattr(_common_config, name, value)
        else:
            super().__setattr__(name, value)

    def __delattr__(self, name):
        if name in _FORWARDED_TO_COMMON_CONFIG:
            raise AttributeError(f"{name} is forwarded to common.config and cannot be deleted")
        super().__delattr__(name)


sys.modules[__name__].__class__ = _ForwardingConfigModule


# ---------------------------------------------------------------------------
# Active project getter / setter
# ---------------------------------------------------------------------------

def get_active_project() -> str:
    return _active_project


def set_active_project(value: str) -> None:
    global _active_project
    _active_project = value


def resolve_project_alias(project_id: str) -> str:
    """Map a local-path-derived project_id to a remote one for kanban use only.

    VibeNode keys all kanban tasks by ``project_id``, which ``_encode_cwd()``
    derives from the absolute filesystem path of the project folder. That's
    fine for single-machine use but breaks shared Supabase boards: two users
    on Windows + Linux pointing at the same DB end up in different silos
    because their absolute paths differ.

    This function checks ``kanban_config.json["project_id_aliases"]`` for an
    entry whose key matches ``project_id`` and returns the aliased value;
    otherwise returns ``project_id`` unchanged. Only kanban routes call this
    — sessions, git, file paths still use the real local-derived id, since
    those legitimately depend on the actual folder on this machine.

    Example kanban_config.json fragment::

        {
          "project_id_aliases": {
            "-home-me-code-VibeNode": "C--Users-other-Documents-VibeNode"
          }
        }
    """
    if not project_id:
        return project_id
    aliases = (get_kanban_config().get("project_id_aliases") or {})
    return aliases.get(project_id, project_id)


# ---------------------------------------------------------------------------
# Directory helpers
# ---------------------------------------------------------------------------

def _sessions_dir(project: str = "") -> Path:
    """Return a project's session directory, auto-detecting if needed.

    When *project* is provided (encoded dir name), use it directly.
    Otherwise fall back to the global ``_active_project``, then auto-detect.
    See ``common.paths.resolve_sessions_dir`` for the priority order; this
    passes THIS module's globals so tests that monkeypatch
    ``_CLAUDE_PROJECTS`` / ``_active_project`` keep working.

    IMPORTANT: This function never mutates ``_active_project``.
    """
    return _resolve_sessions_dir(project, _active_project, _CLAUDE_PROJECTS, _VIBENODE_DIR)



# The encoding lives in common/paths.py (the Session Engine needs it too and
# must not import this package).  Do NOT remove its ``.replace('.', '-')``.
_encode_cwd = _common_encode_cwd


def cwd_matches_active_project(cwd: str, project: str = "") -> bool:
    """Return True if *cwd* belongs to the currently active project.

    The active project directory name is the encoded form of the project
    path.  We encode *cwd* the same way and do a case-insensitive compare.

    When *project* is provided, use it instead of the global.
    """
    active = project or get_active_project()
    if not active:
        return True  # no project context → don't filter anything
    return _encode_cwd(cwd).lower() == active.lower()


def _resolve_encoded_segments(root, parts):
    """Find the real directory under ``root`` whose encoding is ``parts``.

    ``parts`` is the encoded project name split on "-".  At each level the
    actual child directories are listed and matched by their own encoding, so
    a name is found however many "-" it encodes to and whatever mix of
    ``-`` / ``_`` / ``.`` it really contains.  The fixed four-segment lookahead
    in ``_decode_project`` cannot do that: ``my_long_project_name_here`` (five
    segments) or ``a_b-c`` (mixed separators) decoded to a path that does not
    exist.  Backtracks when a shorter match at one level strands the rest.
    """
    def walk(path, i, budget):
        if i >= len(parts):
            return path.rstrip("/") or "/"
        if budget[0] <= 0:
            return None
        budget[0] -= 1
        try:
            children = {}
            with _os.scandir(path) as it:
                for entry in it:
                    try:
                        if entry.is_dir():
                            children.setdefault(_encode_cwd(entry.name), []).append(entry.name)
                    except OSError:
                        continue
        except OSError:
            return None
        for k in range(len(parts) - i, 0, -1):
            for name in children.get("-".join(parts[i:i + k]), ()):
                found = walk(path + name + "/", i + k, budget)
                if found:
                    return found
        return None

    # The budget bounds directory listings so a pathological tree cannot stall
    # a request; a real project path needs one listing per path component.
    return walk(root if root.endswith("/") else root + "/", 0, [200])


def _decode_project(encoded: str) -> str:
    """Convert encoded project name back to filesystem path.
    Handles ambiguity where '-' could be '/' or '_' or '-' in the original."""

    # ---- Windows-style encoding: contains "--" for drive letter (e.g. "C--Users-foo") ----
    if "--" in encoded:
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
        if Path(result).is_dir():
            return result
        resolved = _resolve_encoded_segments(drive + ":/", parts)
        if resolved:
            return resolved
    elif sys.platform != "win32":
        # ---- Unix-style encoding: no drive letter, starts with "-" (e.g. "-home-user-proj") ----
        # Try simple reconstruction: replace all dashes with "/"
        simple = "/" + encoded.lstrip("-").replace("-", "/")
        if Path(simple).is_dir():
            return simple
        # Rebuild segment by segment with ambiguity resolution
        parts = encoded.lstrip("-").split("-")
        path = "/"
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
        if Path(result).is_dir():
            return result
        resolved = _resolve_encoded_segments("/", parts)
        if resolved:
            return resolved
    else:
        # Windows but no "--" — return as-is
        return encoded

    # Final validation: if the reconstructed path isn't a real directory,
    # fall back to scanning common directories for a matching encoded name
    _fallback_scan_dirs = [Path.home() / "Documents"]
    if sys.platform == "darwin":
        _fallback_scan_dirs.append(Path.home() / "Developer")
    elif sys.platform != "win32":
        for name in ("projects", "src", "code", "dev", "repos"):
            _fallback_scan_dirs.append(Path.home() / name)

    for scan_dir in _fallback_scan_dirs:
        if not scan_dir.is_dir():
            continue
        try:
            for child in scan_dir.iterdir():
                if not child.is_dir():
                    continue
                enc = _encode_cwd(str(child))
                if enc == encoded:
                    return str(child)
                try:
                    for sub in child.iterdir():
                        if not sub.is_dir():
                            continue
                        enc2 = _encode_cwd(str(sub))
                        if enc2 == encoded:
                            return str(sub)
                except (PermissionError, OSError):
                    continue
        except (PermissionError, OSError):
            pass
    return result if '--' in encoded or sys.platform != "win32" else encoded


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
# path_str -> the key currently held in _summary_cache for that path.  Every
# turn changes a transcript's (mtime, size), so without this index each turn
# left the previous key behind for the life of the process (only delete /
# rename evicted).  ``load_session_summary`` pops the indexed key before it
# stores a new one.  Code that evicts keys directly (delete, move, admin
# scrub, tests calling ``_summary_cache.clear()``) may leave a dangling index
# entry; that is harmless because the pop is ``pop(key, None)``.
_summary_cache_index: dict = {}


# ---------------------------------------------------------------------------
# Utilities
# ---------------------------------------------------------------------------

def _format_size(file_bytes: int) -> str:
    if file_bytes < 1024:
        return f"{file_bytes} B"
    elif file_bytes < 1024 * 1024:
        return f"{file_bytes / 1024:.0f} KB"
    return f"{file_bytes / (1024*1024):.0f} MB"


# ---------------------------------------------------------------------------
# Backwards-compatible re-exports from session_store
# ---------------------------------------------------------------------------
# These were originally defined here and are imported by many modules.
# Re-export from session_store so existing ``from .config import X``
# statements continue to work without modification.

from .session_store import (  # noqa: E402, F401
    _names_file,
    _load_names,
    _save_name,
    _delete_name,
    _remap_name,
    _load_names_cached,
    _load_tombstones,
    _save_tombstones,
    _prune_tombstones,
    _mark_deleted,
    _mark_deleted_bulk,
    _get_deleted_ids,
    _unmark_deleted,
    move_to_trash,
    move_session,
    list_trash,
    restore_from_trash,
    purge_from_trash,
    _mark_utility,
    _get_utility_ids,
    _mark_remapped,
    _get_remapped_ids,
    _resolve_remapped_id,
    _load_remaps,
    _record_session_access,
    _load_session_access_cached,
)
