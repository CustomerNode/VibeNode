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

_kanban_config_cache: dict | None = None
_kanban_config_cache_time: float = 0.0
_KANBAN_CONFIG_CACHE_TTL = 10.0  # seconds


def get_kanban_config() -> dict:
    """Load kanban configuration from kanban_config.json.

    Missing keys are filled from defaults so callers always see the full set.
    Results are cached with a 10-second TTL.  The cache is invalidated
    immediately on save so writes are never stale.
    """
    global _kanban_config_cache, _kanban_config_cache_time
    now = time.monotonic()
    if _kanban_config_cache is not None and (now - _kanban_config_cache_time) < _KANBAN_CONFIG_CACHE_TTL:
        return dict(_kanban_config_cache)  # shallow copy — callers may mutate
    try:
        stored = json.loads(_KANBAN_CONFIG_FILE.read_text(encoding="utf-8"))
        merged = _kanban_config_defaults()
        merged.update(stored)
        # Migrate old key
        if "kanban_auto_advance" in stored and "auto_advance_to_validating" not in stored:
            merged["auto_advance_to_validating"] = bool(stored["kanban_auto_advance"])
        _kanban_config_cache = merged
        _kanban_config_cache_time = time.monotonic()
        return dict(merged)
    except Exception:
        result = _kanban_config_defaults()
        _kanban_config_cache = result
        _kanban_config_cache_time = time.monotonic()
        return dict(result)


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
        # ── AI autonomy preferences ──
        # AI sessions can change task statuses (working, validating, etc.)
        "ai_can_modify_status": True,
        # AI planner / sessions can mark tasks as complete
        "ai_can_mark_complete": True,
        # ── Cross-session awareness ──
        # Inject other active sessions' names/status/files into system prompts
        "cross_session_awareness": True,
        # ── Validation URL preferences ──
        "validation_url_enabled": False,
        "validation_base_url": "",
        "validation_url_dismissed": False,
        # ── Performance preferences ──
        # File tracking snapshots every source file's mtime each turn for
        # undo/rewind support.  Disable to speed up sessions on large repos.
        "file_tracking_enabled": True,
    }


def save_kanban_config(config: dict) -> None:
    """Save kanban configuration to kanban_config.json."""
    global _kanban_config_cache, _kanban_config_cache_time
    _KANBAN_CONFIG_FILE.write_text(
        json.dumps(config, indent=2, ensure_ascii=False), encoding="utf-8"
    )
    # Invalidate cache so the next read sees the new values immediately
    _kanban_config_cache = None
    _kanban_config_cache_time = 0.0


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

def _sessions_dir(project: str = "") -> Path:
    """Return a project's session directory, auto-detecting if needed.

    When *project* is provided (encoded dir name), use it directly.
    Otherwise fall back to the global ``_active_project``, then auto-detect.

    Priority (when no explicit project):
      1. Explicit _active_project (set by UI project picker)
      2. Match based on server working directory (_VIBENODE_DIR)
      3. Most recently modified .jsonl across all projects

    IMPORTANT: This function never mutates ``_active_project``.
    """
    # --- explicit project parameter ---
    if project:
        if not project.startswith("subagents"):
            p = _CLAUDE_PROJECTS / project
            if p.is_dir():
                return p

    # --- global _active_project ---
    if _active_project:
        if not _active_project.startswith("subagents"):
            p = _CLAUDE_PROJECTS / _active_project
            if p.is_dir():
                return p

    # Derive from server's own repo path — Claude encodes paths with dashes
    # e.g. C-drive path -> C--path-segments-joined-with-dashes
    if not _CLAUDE_PROJECTS.is_dir():
        _CLAUDE_PROJECTS.mkdir(parents=True, exist_ok=True)
        return _CLAUDE_PROJECTS
    repo_path = str(_VIBENODE_DIR).replace("\\", "-").replace("/", "-").replace(":", "-")
    for d in _CLAUDE_PROJECTS.iterdir():
        if not d.is_dir() or d.name.startswith("subagents") or d.name.startswith("_"):
            continue
        # Case-insensitive match — Claude's encoding may differ in case
        if d.name.lower() == repo_path.lower():
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
        return best
    return _CLAUDE_PROJECTS



def _encode_cwd(cwd: str) -> str:
    """Encode a filesystem path into the Claude project directory name format.

    E.g. ``C:\\Users\\foo\\Bar`` → ``C--Users-foo-Bar``

    IMPORTANT: Underscores must also be replaced with dashes.  Claude Code's
    own project directory encoding converts underscores to dashes (e.g.
    ``customerNode_root`` → ``customerNode-root``), but the daemon's CWD
    preserves the original filesystem underscores.  Without this, sessions
    whose CWD contains underscores fail the project filter silently —
    they appear to not exist even though the daemon has them running.
    """
    return cwd.replace("\\", "-").replace("/", "-").replace(":", "-").replace("_", "-")


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
    _mark_utility,
    _get_utility_ids,
    _mark_remapped,
    _get_remapped_ids,
    _resolve_remapped_id,
    _load_remaps,
)
