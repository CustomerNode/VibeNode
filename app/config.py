"""
Configuration, path helpers, and session-name persistence.
"""

import json
from pathlib import Path

# ---------------------------------------------------------------------------
# Path constants
# ---------------------------------------------------------------------------

_CLAUDE_PROJECTS = Path.home() / ".claude" / "projects"
_active_project: str = ""   # encoded dir name; empty = auto-detect
_CLAUDECODEGUI_DIR = Path(__file__).resolve().parent.parent  # always the ClaudeCodeGUI repo


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
    """Return the active project's session directory, auto-detecting if needed."""
    global _active_project
    if _active_project:
        p = _CLAUDE_PROJECTS / _active_project
        if p.is_dir():
            return p
    # Auto-detect: pick the project with the most recent .jsonl file
    best, best_ts = None, 0.0
    for d in _CLAUDE_PROJECTS.iterdir():
        if not d.is_dir() or d.name.startswith("subagents"):
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


def _decode_project(encoded: str) -> str:
    """Convert C--Users-donca-Documents-FileTaskNode -> C:/Users/donca/Documents/FileTaskNode (display only)."""
    if "--" in encoded:
        drive, rest = encoded.split("--", 1)
        return drive + ":/" + rest.replace("-", "/")
    return encoded


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
    """Persist a user-set name. Creates or updates _session_names.json."""
    names = _load_names()
    names[session_id] = name
    _names_file().write_text(json.dumps(names, indent=2, ensure_ascii=False), encoding="utf-8")


def _delete_name(session_id: str) -> None:
    """Remove a session from the user-names store (e.g. on delete)."""
    names = _load_names()
    if session_id in names:
        names.pop(session_id)
        _names_file().write_text(json.dumps(names, indent=2, ensure_ascii=False), encoding="utf-8")


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
        return f"{file_bytes / 1024:.1f} KB"
    return f"{file_bytes / (1024*1024):.1f} MB"
