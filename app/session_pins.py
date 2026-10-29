"""Pinned sessions (owner, 2026-10-29).

The owner asked for a way to keep a session at the top of the sidebar
whatever the sort: "Pin to top" in the right-click menu, plus a faint pin on
hover that turns purple once pinned (his pick B of
``static/scratch/session-pin.html``), and then for dragging pinned sessions
to put them in his own order.  A pin is the person's choice and is not in the
transcript, so it is kept here, per project, next to ``_session_holds.json``:

    _session_pins.json  {session_id: {"set": ts, "rank": n}}

``rank`` is the place among the pinned sessions, smallest first.  A new pin
goes to the top (one less than the smallest rank); dragging writes 0..n-1.
Unlike a Standby hold, a pin does not end with the next message: it stays
until he unpins it.  Saved on the server (not per device) so the phone and
the computer show the same order.
"""

from __future__ import annotations

import json
import os
import threading
import time
from pathlib import Path

FILENAME = "_session_pins.json"
_MAX_AGE = 90 * 86400        # a pin whose transcript is gone is dropped after this
_lock = threading.Lock()
_cache: dict = {}            # path -> (mtime, data)


def pins_path(sessions_dir: Path) -> Path:
    return Path(sessions_dir) / FILENAME


def load(sessions_dir: Path) -> dict:
    """``{sid: {"set", "rank"}}`` for a project, cached by the file's mtime."""
    p = pins_path(sessions_dir)
    try:
        mt = p.stat().st_mtime
    except OSError:
        return {}
    hit = _cache.get(str(p))
    if hit and hit[0] == mt:
        return hit[1]
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            data = {}
    except Exception:
        data = {}
    _cache[str(p)] = (mt, data)
    return data


def rank_of(entry) -> float:
    """A pin's place; a pin saved without one sorts as 0."""
    try:
        return float(entry.get("rank") or 0) if isinstance(entry, dict) else 0.0
    except (TypeError, ValueError):
        return 0.0


def _num(r: float):
    """Ranks are whole numbers; send them as ints so the JSON stays tidy."""
    return int(r) if float(r).is_integer() else r


def overlay(sessions: list, sessions_dir: Path, aliases: dict | None = None) -> list:
    """Add ``pinned: True`` and ``pin_rank`` to each pinned summary (in place).

    ``aliases`` maps a launch id to the id the CLI gave the session later
    (``api_sessions``): a pin saved under the launch id during the first turn
    still finds the row listed under the new id.  Rows without a pin are left
    untouched, so a project with no pins lists exactly as before."""
    pins = load(sessions_dir)
    if not pins:
        return sessions
    by_id = dict(pins)
    if aliases:
        for old, entry in pins.items():
            new = aliases.get(old)
            if new and new not in by_id:
                by_id[new] = entry
    for s in sessions:
        entry = by_id.get(s.get("id"))
        if entry is not None:
            s["pinned"] = True
            s["pin_rank"] = _num(rank_of(entry))
    return sessions


def _read(p: Path) -> dict:
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _write(p: Path, sessions_dir: Path, data: dict) -> None:
    # Forget pins of sessions whose transcript is gone (deleted, moved to
    # another project) once they are old; a live pin is never dropped.
    now = time.time()
    sd = Path(sessions_dir)
    data = {sid: v for sid, v in data.items()
            if isinstance(v, dict) and ((now - float(v.get("set") or 0)) < _MAX_AGE
                                        or (sd / f"{sid}.jsonl").exists())}
    tmp = p.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, p)
    _cache.pop(str(p), None)


def set_pin(sessions_dir: Path, session_id: str, pinned: bool, old_ids=()):
    """Pin (or with ``pinned=False`` unpin) a session.  Returns the pin's
    rank (a new pin goes to the top), or None when unpinned.

    ``old_ids`` are launch ids that now resolve to ``session_id``: unpinning
    drops a pin saved under any of them too, or ``overlay`` would find it
    through the alias and the session would stay pinned."""
    p = pins_path(sessions_dir)
    with _lock:
        data = _read(p)
        rank = None
        if pinned:
            if session_id in data and isinstance(data[session_id], dict):
                rank = rank_of(data[session_id])
            else:
                rank = min((rank_of(v) for v in data.values()), default=1.0) - 1
                data[session_id] = {"set": time.time(), "rank": _num(rank)}
        else:
            data.pop(session_id, None)
            for old in old_ids or ():
                data.pop(old, None)
        _write(p, sessions_dir, data)
        return None if rank is None else _num(rank)


def set_order(sessions_dir: Path, order: list, aliases: dict | None = None) -> list:
    """Put the pinned sessions in ``order`` (ids as the sidebar shows them).

    Ids that are not pinned are skipped; a pin saved under a launch id is
    found through ``aliases`` (launch id -> shown id).  Pinned sessions left
    out of ``order`` keep their place after the listed ones.  Returns the
    ids that were placed, in order."""
    p = pins_path(sessions_dir)
    back = {new: old for old, new in (aliases or {}).items()}
    with _lock:
        data = _read(p)
        placed, keys = [], []
        for sid in order:
            key = sid if sid in data else back.get(sid)
            if key in data and key not in keys and isinstance(data[key], dict):
                keys.append(key)
                placed.append(sid)
        rest = sorted((k for k in data if k not in keys and isinstance(data[k], dict)),
                      key=lambda k: rank_of(data[k]))
        for i, key in enumerate(keys + rest):
            data[key]["rank"] = i
        _write(p, sessions_dir, data)
        return placed
