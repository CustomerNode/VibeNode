"""Disk: how full the drive is, what VibeNode's sessions take on it, and
cleanup that still works when the disk is full.

When the disk is full nothing can be written, which is also why no Claude
session can run then.  So every action here runs inside the web server with
no session, and frees space BEFORE it writes anything: a session is moved to
the Trash with a rename (no new bytes); when the disk is nearly full, or the
move fails, the file is removed outright.  The small bookkeeping files (the
hidden-sessions list, the Trash list, names) are written afterwards, into the
space just freed, and a failure there never stops the delete.

The cap ("VibeNode may use") is saved as ``disk_cap_bytes`` in kanban_config.
Missing, 0, or at least the disk's size means no cap (the default).  Over it,
the oldest Trash is emptied first, then the sessions quiet the longest are
removed for good, until sessions and Trash fit.  Pinned and running sessions
are never removed for the cap.
"""

from __future__ import annotations

import json
import os
import shutil
import threading
import time
from pathlib import Path

DAY = 86400
_HEAD_BYTES = 64 * 1024
FULL_FREE_BYTES = 256 << 20     # under this much free, delete without the Trash
CAP_KEY = "disk_cap_bytes"
CAP_MIN = 1 << 30
CAP_CHECK_SECONDS = 300
_SHARE_TTL = 30.0
_share_cache: dict = {}


# ── Readings ──────────────────────────────────────────────────────────────

def disk_reading(root: Path) -> dict:
    """The drive that holds the conversations: size, used, free, percent."""
    try:
        u = shutil.disk_usage(str(root if root.exists() else Path.home()))
    except OSError:
        return {"known": False}
    return {"known": True, "total": u.total, "used": u.total - u.free, "free": u.free,
            "percent": round(100 * (u.total - u.free) / u.total) if u.total else 0}


def _tree_bytes(p: Path) -> int:
    total = 0
    try:
        with os.scandir(p) as it:
            for e in it:
                try:
                    if e.is_dir(follow_symlinks=False):
                        total += _tree_bytes(Path(e.path))
                    elif e.is_file(follow_symlinks=False):
                        total += e.stat(follow_symlinks=False).st_size
                except OSError:
                    continue
    except OSError:
        pass
    return total


def share(root: Path, extra=(), fresh: bool = False) -> dict:
    """Bytes VibeNode takes: ``sessions`` (every project folder except its
    Trash), ``trash``, and ``other`` (its own data and logs).  Cached briefly:
    the walk is cheap but the bar asks every minute."""
    key = str(root)
    hit = _share_cache.get(key)
    if hit and not fresh and time.time() - hit[0] < _SHARE_TTL:
        return hit[1]
    sessions = trash = 0
    try:
        for d in root.iterdir():
            if not d.is_dir():
                continue
            t = _tree_bytes(d / "_trash") if (d / "_trash").is_dir() else 0
            trash += t
            sessions += _tree_bytes(d) - t
    except OSError:
        pass
    other = sum(_tree_bytes(Path(x)) for x in extra if Path(x).is_dir())
    out = {"sessions": sessions, "trash": trash, "other": other}
    _share_cache[key] = (time.time(), out)
    return out


def _call_with_timeout(fn, timeout: float):
    box = {}

    def run():
        try:
            box["r"] = fn()
        except Exception as e:  # noqa: BLE001
            box["e"] = e

    t = threading.Thread(target=run, daemon=True)
    t.start()
    t.join(timeout)
    return box.get("r")


def running_ids(sm, timeout: float = 3.0) -> set:
    """Sessions the engine has running.  If it does not answer, transcripts
    written in the last 10 minutes count as running, so a wedged engine
    never lets a live session be deleted."""
    states = _call_with_timeout(lambda: sm.get_all_states(), timeout) if sm else None
    if isinstance(states, list):
        return {s.get("session_id") for s in states
                if s.get("session_id") and s.get("state") != "stopped"}
    return {"__recent__"}


def _is_running(sid: str, mtime: float, running: set, now: float) -> bool:
    return sid in running or ("__recent__" in running and now - mtime < 600)


def _session_files(root: Path):
    """``(project, sid, path, stat)`` for every listed session."""
    from .session_store import _get_deleted_ids, _get_utility_ids
    from . import session_pins
    try:
        dirs = [d for d in root.iterdir() if d.is_dir()]
    except OSError:
        return
    for d in dirs:
        if d.name.endswith("-_system") or d.name.endswith("--claude--system"):
            continue
        try:
            skip = set(_get_deleted_ids(d.name)) | set(_get_utility_ids(d.name))
            pinned = set(session_pins.load(d).keys())
        except Exception:
            skip, pinned = set(), set()
        for f in d.glob("*.jsonl"):
            if f.stem in skip:
                continue
            try:
                st = f.stat()
            except OSError:
                continue
            yield d.name, f.stem, f, st, f.stem in pinned


def _folder_bytes(f: Path) -> int:
    folder = f.with_suffix("")
    return _tree_bytes(folder) if folder.is_dir() else 0


def _title(project: str, sid: str, f: Path) -> str:
    try:
        from .session_store import _load_names
        n = _load_names(project).get(sid, "")
        if n:
            return n
    except Exception:
        pass
    try:
        from .sessions import load_session
        t = load_session(f).get("display_title") or ""
        return "" if t == sid else t
    except Exception:
        return ""


def biggest(root: Path, running: set, limit: int = 6, now: float | None = None) -> list:
    """The largest sessions that could be deleted (not running, not pinned)."""
    now = time.time() if now is None else now
    rows = []
    for project, sid, f, st, pinned in _session_files(root):
        if pinned or _is_running(sid, st.st_mtime, running, now):
            continue
        rows.append((st.st_size, project, sid, f, st))
    rows.sort(key=lambda r: -r[0])
    # A session's folder (tool output, pictures) can outweigh its transcript:
    # count both for the largest few, then keep the biggest totals.
    sized = [(size + _folder_bytes(f), project, sid, f, st)
             for size, project, sid, f, st in rows[:limit * 5]]
    sized.sort(key=lambda r: -r[0])
    return [{"id": sid, "project": project, "name": _title(project, sid, f),
             "bytes": total, "last_active": st.st_mtime}
            for total, project, sid, f, st in sized[:limit]]


def reading(sm, root: Path, extra=()) -> dict:
    run = running_ids(sm)
    return {"ok": True, "disk": disk_reading(root), "share": share(root, extra),
            "biggest": biggest(root, run), "at": time.time()}


# ── Which sessions a rule picks ──────────────────────────────────────────

def _started_at(path: Path) -> float:
    try:
        with open(path, "rb") as fh:
            head = fh.read(_HEAD_BYTES)
        for line in head.split(b"\n")[:20]:
            if not line.strip():
                continue
            try:
                ts = json.loads(line).get("timestamp")
            except Exception:
                continue
            if ts:
                from datetime import datetime
                return datetime.fromisoformat(str(ts).replace("Z", "+00:00")).timestamp()
    except Exception:
        pass
    try:
        return path.stat().st_mtime
    except OSError:
        return 0


def delete_candidates(root: Path, *, created_days=None, inactive_days=None,
                      running=(), now: float | None = None) -> list:
    """Sessions matching EVERY rule given: ``created_days`` (started more than
    N days ago) and ``inactive_days`` (nothing new for more than N days).  No
    rule picks nothing.  Running, pinned, deleted and utility sessions never."""
    if created_days is None and inactive_days is None:
        return []
    now = time.time() if now is None else now
    running = set(running)
    out = []
    for project, sid, f, st, pinned in _session_files(root):
        if pinned or _is_running(sid, st.st_mtime, running, now):
            continue
        if inactive_days is not None and now - st.st_mtime <= inactive_days * DAY:
            continue
        if created_days is not None and now - _started_at(f) <= created_days * DAY:
            continue
        out.append({"id": sid, "project": project, "last_active": st.st_mtime,
                    "bytes": st.st_size + _folder_bytes(f)})
    out.sort(key=lambda r: r["last_active"])
    return out


# ── Actions ──────────────────────────────────────────────────────────────

def _quiet(fn, *a, **kw):
    """Bookkeeping after the space is freed: a failure (disk still full)
    never stops the delete."""
    try:
        return fn(*a, **kw)
    except Exception:
        return None


def delete_sessions(items: list, root: Path, *, to_trash: bool = True) -> dict:
    """Delete sessions, freeing their space first.  To the Trash (a rename)
    when ``to_trash`` and the disk has room; otherwise removed for good.
    Returns ``{"deleted", "trashed", "removed", "freed"}``."""
    from .config import _summary_cache
    from .session_store import _mark_deleted, move_to_trash, _delete_name, _load_names
    trashed = removed = freed = 0
    for it in items:
        sid, project = it["id"], it["project"]
        d = root / project
        f = d / f"{sid}.jsonl"
        folder = d / sid
        size = 0
        try:
            size = f.stat().st_size
        except OSError:
            pass
        room = disk_reading(root).get("free", 0) >= FULL_FREE_BYTES
        moved = False
        if to_trash and room:
            name = _quiet(lambda: _load_names(project).get(sid, "")) or ""
            moved = bool(_quiet(move_to_trash, sid, project, name=name, retries=2, delay=0.1))
        if not moved and not f.exists() and (d / "_trash" / f.name).exists():
            moved = True   # it moved; only writing the Trash list failed
        if not moved:
            try:
                f.unlink()
                removed += 1
                freed += size
            except FileNotFoundError:
                pass
            except OSError:
                continue
        else:
            trashed += 1
        if folder.is_dir():
            freed += _tree_bytes(folder)
            shutil.rmtree(folder, ignore_errors=True)
        # Now there is room for the records.
        _quiet(_mark_deleted, sid, project)
        _quiet(_delete_name, sid, project)
        path = str(f)
        for key in [k for k in list(_summary_cache) if k[0] == path]:
            _summary_cache.pop(key, None)
    _share_cache.clear()
    return {"deleted": trashed + removed, "trashed": trashed, "removed": removed, "freed": freed}


def empty_trash(root: Path, keep_newest_seconds: float | None = None) -> int:
    """Remove every session in every project's Trash for good (or only those
    deleted longer ago than ``keep_newest_seconds``).  Bytes freed."""
    from .session_store import _load_trash_index, _save_trash_index
    freed = 0
    now = time.time()
    try:
        dirs = [d for d in root.iterdir() if (d / "_trash").is_dir()]
    except OSError:
        return 0
    for d in dirs:
        index = _quiet(_load_trash_index, d.name) or {}
        kept = {}
        for f in (d / "_trash").iterdir():
            if f.suffix != ".jsonl" and not f.is_dir():
                continue   # the Trash list itself
            sid = f.stem if f.suffix == ".jsonl" else f.name
            meta = index.get(sid) or {}
            at = meta.get("deleted_at") or 0
            try:
                at = float(at)
            except (TypeError, ValueError):
                at = 0
            if keep_newest_seconds is not None and at and now - at < keep_newest_seconds:
                if sid in index:
                    kept[sid] = index[sid]
                continue
            try:
                if f.is_dir():
                    freed += _tree_bytes(f)
                    shutil.rmtree(f, ignore_errors=True)
                else:
                    freed += f.stat().st_size
                    f.unlink()
            except OSError:
                continue
        _quiet(_save_trash_index, kept, d.name)
    _share_cache.clear()
    return freed


# ── The cap ──────────────────────────────────────────────────────────────

def cap_of(cfg: dict, total: int) -> int:
    try:
        v = int((cfg or {}).get(CAP_KEY) or 0)
    except (TypeError, ValueError):
        return 0
    return v if 0 < v < total else 0


def enforce_cap(sm, root: Path, cap: int) -> dict:
    """Bring sessions + Trash under ``cap``: empty the Trash, then remove the
    sessions quiet the longest for good.  Returns what was done."""
    if not cap:
        return {}
    s = share(root, fresh=True)
    used = s["sessions"] + s["trash"]
    if used <= cap:
        return {}
    done = {"trash_freed": 0, "removed": 0}
    if s["trash"]:
        done["trash_freed"] = empty_trash(root)
        used -= done["trash_freed"]
    if used <= cap:
        return done
    pick = []
    for it in delete_candidates(root, inactive_days=0, running=running_ids(sm)):
        if used <= cap:
            break
        pick.append(it)
        used -= it["bytes"]
    if pick:
        done["removed"] = delete_sessions(pick, root, to_trash=False)["removed"]
    return done


def start_cap_watch(app, socketio, root: Path) -> None:
    """One web-server thread checking the cap every CAP_CHECK_SECONDS.
    Never in a test app.  Kill switch: VIBENODE_DISK_CAP=0."""
    if os.environ.get("VIBENODE_DISK_CAP", "1") == "0":
        return
    import logging
    log = logging.getLogger("web.disk_cap")

    def _loop():
        from shared.config import get_kanban_config
        while True:
            time.sleep(CAP_CHECK_SECONDS)
            try:
                cap = cap_of(get_kanban_config() or {}, disk_reading(root).get("total") or 0)
                done = enforce_cap(getattr(app, "session_manager", None), root, cap)
                if done:
                    log.info("disk cap %d MB: %s", cap >> 20, done)
                    socketio.emit("sessions_refresh", {"reason": "disk-cap"})
            except Exception:
                log.exception("disk cap check failed")

    threading.Thread(target=_loop, daemon=True, name="disk-cap").start()
