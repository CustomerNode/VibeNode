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
_VIBENODE_DIR = Path(__file__).resolve().parent.parent  # always the VibeNode repo


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
        # Reject subagent directories — they contain internal Claude state
        if not _active_project.startswith("subagents"):
            p = _CLAUDE_PROJECTS / _active_project
            if p.is_dir():
                return p
        # Active project is invalid or a subagent dir — fall through to auto-detect
        _active_project = ""
    # Auto-detect: pick the project with the most recent .jsonl file
    best, best_ts = None, 0.0
    if not _CLAUDE_PROJECTS.is_dir():
        return _CLAUDE_PROJECTS
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
