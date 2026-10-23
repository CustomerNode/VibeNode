"""The per-project session name and remap files: one owner, both layers.

* ``_session_names.json``: ``{session_id: user-set name}``.
* ``_remapped_sessions.json``: ``{old_id: {"new_id", "ts"}}``, the temp ids a
  session ran under before the CLI announced its real id; the web hides their
  stale ``.jsonl`` files.

The Session Engine writes both when it adopts a CLI id
(``SessionManager._remap_session_id``) through :func:`remap_name` and
:func:`mark_remapped`.  The web (``app/session_store.py``) reads and writes
them through the ``*_at`` / ``*_in`` primitives, resolving the project
directory with its own ``_sessions_dir`` so its tests can redirect it.
"""

import json
import threading
import time
from pathlib import Path

from common.paths import sessions_dir

NAMES_FILENAME = "_session_names.json"
REMAPS_FILENAME = "_remapped_sessions.json"
REMAP_MAX_AGE = 86400  # 24 hours: old temp ids are pruned after this

# Protects the read-modify-write of the remap file within one process.
REMAP_LOCK = threading.Lock()


# ── Names ─────────────────────────────────────────────────────────────────

def names_path(project_dir: Path) -> Path:
    return Path(project_dir) / NAMES_FILENAME


def load_names_at(path: Path) -> dict:
    """``{session_id: name}`` from ``path``; ``{}`` if missing or unreadable."""
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except Exception:
        return {}


def write_names_at(path: Path, names: dict) -> None:
    Path(path).write_text(json.dumps(names, indent=2, ensure_ascii=False), encoding="utf-8")


def remap_name_at(path: Path, old_id: str, new_id: str):
    """Move a user-set name from ``old_id`` to ``new_id``.  Returns the name
    or None.  Writes only when there was a name to move."""
    names = load_names_at(path)
    title = names.pop(old_id, None)
    if title:
        names[new_id] = title
        write_names_at(path, names)
    return title


# ── Remaps ────────────────────────────────────────────────────────────────

def remaps_path(project_dir: Path) -> Path:
    return Path(project_dir) / REMAPS_FILENAME


def load_remaps_at(path: Path) -> dict:
    """``{old_id: {"new_id": str, "ts": float}}``; ``{}`` if missing/bad."""
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        if isinstance(data, dict):
            return data
    except Exception:
        pass
    return {}


def save_remaps_at(path: Path, data: dict) -> None:
    Path(path).write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")


def mark_remapped_in(project_dir: Path, old_session_id: str, new_session_id: str = "") -> None:
    """Record an old (pre-remap) session id so its JSONL is hidden, and
    lazily prune entries older than 24h whose ``.jsonl`` is gone."""
    project_dir = Path(project_dir)
    path = remaps_path(project_dir)
    with REMAP_LOCK:
        data = load_remaps_at(path)
        data[old_session_id] = {"new_id": new_session_id, "ts": time.time()}
        now = time.time()
        data = {
            sid: entry for sid, entry in data.items()
            if now - entry.get("ts", 0) < REMAP_MAX_AGE
            or (project_dir / f"{sid}.jsonl").exists()
        }
        save_remaps_at(path, data)


# ── Engine alias index (data/session_aliases.json) ────────────────────────
#
# One file for every project: ``{old_id: {"new_id", "ts"}}``, the same shape as
# the per-project remap file.  The engine writes it on every id adoption so a
# restarted engine can rehydrate ``SessionManager._id_aliases`` (a stale tab
# that still holds the launch id keeps reaching the session after a restart);
# the web merges it into its remap reads.  Module global so tests can sandbox
# it (tests/conftest.py).

ENGINE_ALIASES_PATH: Path = Path(__file__).resolve().parents[1] / "data" / "session_aliases.json"
ENGINE_ALIAS_MAX_AGE = 7 * 86400


def load_engine_aliases() -> dict:
    """``{old_id: {"new_id", "ts"}}`` from the engine's alias index; ``{}``
    when missing or unreadable."""
    return load_remaps_at(ENGINE_ALIASES_PATH)


def record_engine_alias(old_id: str, new_id: str) -> None:
    """Add ``old_id -> new_id`` to the engine's alias index (atomic write,
    entries older than a week pruned)."""
    import os
    import tempfile
    if not old_id or not new_id or old_id == new_id:
        return
    path = Path(ENGINE_ALIASES_PATH)
    with REMAP_LOCK:
        data = load_remaps_at(path)
        now = time.time()
        data[old_id] = {"new_id": new_id, "ts": now}
        data = {k: v for k, v in data.items()
                if isinstance(v, dict) and now - float(v.get("ts", 0) or 0) < ENGINE_ALIAS_MAX_AGE}
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(prefix=".aliases.", suffix=".tmp", dir=str(path.parent))
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(data, fh, ensure_ascii=False)
            os.replace(tmp, path)
        except Exception:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise


# ── Engine entry points ───────────────────────────────────────────────────

def remap_name(old_id: str, new_id: str, project: str = ""):
    """Engine side of :func:`remap_name_at` (project resolved without the
    web's active-project picker, as it always was inside the engine)."""
    return remap_name_at(names_path(sessions_dir(project)), old_id, new_id)


def mark_remapped(old_session_id: str, new_session_id: str = "", project: str = "") -> None:
    """Engine side of :func:`mark_remapped_in`."""
    mark_remapped_in(sessions_dir(project), old_session_id, new_session_id)
