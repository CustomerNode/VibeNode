"""Standby and marked-done sessions (owner, 2026-10-28).

The sidebar draws what the person has to do (Needs you / Standby / Working /
Done, ``static/js/session-verb.js``).  Standby means "I've seen it, not now,
keep it on my list"; a marked Done means "nothing more needed", which also
lets a session that ended on a question leave Needs you.  Neither is in the
transcript, so the person's choice is kept here, per project, next to
``_session_names.json``:

    _session_holds.json  {session_id: {"state": "standby"|"done", "at": ts}}

``at`` is the session's ``last_activity_ts`` (its newest conversation
message) when the choice was made.  The hold counts only while that is still
the newest message: any new message, from the person or the session, ends it
everywhere, on every device and after a reload, with nothing else to clear.
"""

from __future__ import annotations

import json
import os
import threading
import time
from pathlib import Path

STATES = ("standby", "done")
FILENAME = "_session_holds.json"
_MAX_AGE = 90 * 86400        # a hold whose transcript is gone is dropped after this
_SLACK = 0.5                 # timestamps round-trip through JSON and two clocks
_lock = threading.Lock()
_cache: dict = {}            # path -> (mtime, data)


def holds_path(sessions_dir: Path) -> Path:
    return Path(sessions_dir) / FILENAME


def load(sessions_dir: Path) -> dict:
    """``{sid: {"state", "at"}}`` for a project, cached by the file's mtime."""
    p = holds_path(sessions_dir)
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


def state_for(entry, last_activity_ts) -> str | None:
    """The hold's state while it still stands, else None."""
    if not isinstance(entry, dict) or entry.get("state") not in STATES:
        return None
    try:
        at = float(entry.get("at") or 0)
        last = float(last_activity_ts or 0)
    except (TypeError, ValueError):
        return None
    return entry["state"] if last <= at + _SLACK else None


def overlay(sessions: list, sessions_dir: Path) -> list:
    """Add ``hold`` to each summary whose hold still stands (in place).
    Rows without one are left untouched, so a project with no holds lists
    exactly as before."""
    holds = load(sessions_dir)
    if not holds:
        return sessions
    for s in sessions:
        entry = holds.get(s.get("id"))
        st = state_for(entry, s.get("last_activity_ts"))
        if st:
            s["hold"] = st
            # When the person made the choice: the text box says "Set aside
            # 12 min ago" (static/scratch/standby-bar.html option A).
            try:
                since = float(entry.get("set") or 0)
            except (TypeError, ValueError):
                since = 0.0
            if since > 0:
                s["hold_since"] = since
    return sessions


def set_hold(sessions_dir: Path, session_id: str, state: str | None, at: float) -> None:
    """Record (or with ``state=None`` clear) the person's choice."""
    if state is not None and state not in STATES:
        raise ValueError("state must be standby, done or null")
    p = holds_path(sessions_dir)
    with _lock:
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
            if not isinstance(data, dict):
                data = {}
        except Exception:
            data = {}
        if state is None:
            data.pop(session_id, None)
        else:
            data[session_id] = {"state": state, "at": float(at or 0), "set": time.time()}
        now = time.time()
        sd = Path(sessions_dir)
        data = {sid: v for sid, v in data.items()
                if isinstance(v, dict) and ((now - float(v.get("set") or 0)) < _MAX_AGE
                                            or (sd / f"{sid}.jsonl").exists())}
        tmp = p.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, p)
        _cache.pop(str(p), None)
