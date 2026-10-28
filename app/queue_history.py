"""Queued messages a session handled since the user last looked.

The owner, 2026-10-28: "if multiple queued messages were handled since the
user did anything we add a row to click to show queue history since they
last reviewed (only if queued messages were handled)".  Picked: a line under
"Just now" on the session's newest card, with a history-clock icon
(static/scratch/card-queue.html, C with icon G).

The Session Engine pops a queued message and sends it; nothing kept it
afterwards.  The web server sees all three moments, so it records them here:
the user queues a message (``note_queued``), the engine sends it
(``note_dispatched``, from the ``queue_dispatched`` push), and the user sends
something himself (``note_direct``), which is "looking" again and clears the
list.  ``GET /api/queue-history/<sid>`` (live_api.py) serves it.

Stored in data/queue_history.json (gitignored: it holds his messages).
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time

from common.paths import DATA_DIR

logger = logging.getLogger(__name__)

PATH = DATA_DIR / "queue_history.json"
MAX_ITEMS = 40
MAX_PENDING = 60
MAX_SESSIONS = 300

_lock = threading.Lock()
_state = None


def _load() -> dict:
    global _state
    if _state is None:
        try:
            _state = json.loads(PATH.read_text("utf-8"))
            if not isinstance(_state, dict):
                _state = {}
        except Exception:
            _state = {}
    return _state


def _save() -> None:
    try:
        PATH.parent.mkdir(parents=True, exist_ok=True)
        tmp = PATH.with_name(PATH.name + ".tmp")
        tmp.write_text(json.dumps(_state), "utf-8")
        os.replace(tmp, PATH)
    except Exception as e:   # history is a nicety; never break a send
        logger.warning("queue history: could not save: %s", e)


def _rec(sid: str) -> dict:
    s = _load()
    rec = s.get(sid)
    if not isinstance(rec, dict):
        rec = s[sid] = {"reviewed_at": 0, "pending": [], "items": []}
        if len(s) > MAX_SESSIONS:   # forget the sessions touched longest ago
            for old in sorted(s, key=lambda k: (s[k] or {}).get("touched", 0))[:len(s) - MAX_SESSIONS]:
                s.pop(old, None)
    rec["touched"] = time.time()
    return rec


def note_queued(sid: str, text: str) -> None:
    if not sid or not text:
        return
    with _lock:
        rec = _rec(sid)
        rec["pending"] = (rec.get("pending") or [])[-(MAX_PENDING - 1):] + [{"text": text, "queued_at": time.time()}]
        _save()


def note_dispatched(sid: str, text: str) -> None:
    if not sid or not text:
        return
    with _lock:
        rec = _rec(sid)
        pending = rec.get("pending") or []
        i = next((k for k, p in enumerate(pending) if p.get("text") == text), -1)
        queued_at = pending.pop(i).get("queued_at") if i >= 0 else None
        rec["pending"] = pending
        rec["items"] = (rec.get("items") or [])[-(MAX_ITEMS - 1):] + [
            {"text": text, "queued_at": queued_at, "sent_at": time.time()}]
        _save()


def note_direct(sid: str) -> None:
    """The user sent something himself: he is looking, so the list starts over."""
    if not sid:
        return
    with _lock:
        rec = _rec(sid)
        rec["reviewed_at"] = time.time()
        rec["items"] = []
        _save()


def note_remap(old: str, new: str) -> None:
    if not old or not new or old == new:
        return
    with _lock:
        s = _load()
        if old in s:
            if new in s:   # merge: keep both lists, newest review wins
                a, b = s.pop(old), s[new]
                b["reviewed_at"] = max(a.get("reviewed_at", 0), b.get("reviewed_at", 0))
                b["items"] = sorted((a.get("items") or []) + (b.get("items") or []), key=lambda i: i.get("sent_at", 0))[-MAX_ITEMS:]
                b["pending"] = ((a.get("pending") or []) + (b.get("pending") or []))[-MAX_PENDING:]
            else:
                s[new] = s.pop(old)
            _save()


def since_review(sid: str) -> dict:
    with _lock:
        rec = _load().get(sid) or {}
        seen = rec.get("reviewed_at", 0) or 0
        items = [dict(i) for i in (rec.get("items") or []) if (i.get("sent_at") or 0) > seen]
        return {"reviewed_at": seen, "items": items}


def _reset_for_tests(path=None) -> None:
    global _state, PATH
    _state = None
    if path is not None:
        PATH = path
