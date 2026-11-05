"""5-hour pacing: a 5-hour limit VibeNode sets itself from the weekly limit,
working the way the account's own 5-hour limit used to.

The window.  It opens with the first usage after the last one closed and
lasts five hours; its reset time is fixed when it opens.  Its share is set at
the same moment: the weekly usage left, spread evenly over the 5-hour blocks
until the weekly reset ("Linear"), times the slider's factor.  Unused share
is not lost: the next window is figured from what is left of the week.

Hitting it.  When the weekly reading has risen by the share since the window
opened, the window is "hit": a turn already running finishes, new messages
are refused until the reset, and a turn that starts after the hit anyway (a
queued message, a watchdog nudge) is interrupted, its queued messages kept.
At the reset, every session it interrupted is told to continue, as sessions
continue after a usage limit resets.

The reset button.  ``reset()`` ends the window at once and continues the
sessions.  Only a person may press it: the route takes it only from the
app's own page, and every session is refused a tool call that names it
(``src/engine/tool_guards``).

Readings come from the weekly utilization the CLI reports on every turn
(``engine.usage_limits``); nothing here calls Anthropic.  Setting:
``pace_5h_mult`` in kanban_config, 0 (the default) = Off.  State:
``data/pace_5h.json`` (gitignored).
"""

from __future__ import annotations

import json
import os
import tempfile
import threading
import time
from pathlib import Path
from typing import Optional

WINDOW = 5 * 3600
CHECK_SECONDS = 15
SETTING_NAME = "pace_5h_mult"
# Slider positions, Linear first, Off last (0 = no limit per 5 hours).
STEPS = [1.0, 1.5, 2.0, 3.0, 5.0, 0.0]
MIN_SHARE = 0.5          # percent of the week; never a window with nothing in it
ROOT = Path(__file__).resolve().parents[2]
STATE_PATH = ROOT / "data" / "pace_5h.json"
CONTINUE_TEXT = ("[VibeNode] The 5-hour pace limit has reset. Continue where you "
                 "left off.")
_lock = threading.Lock()


def mult_of(cfg: dict) -> float:
    try:
        v = float((cfg or {}).get(SETTING_NAME) or 0)
    except (TypeError, ValueError):
        return 0.0
    return v if v > 0 else 0.0


def weekly(state: dict, now: float):
    """``(percent used, resets_at)`` of the weekly window, or ``(None, 0)``."""
    w = ((state or {}).get("windows") or {}).get("seven_day")
    if not isinstance(w, dict):
        return None, 0
    try:
        resets = int(w.get("resets_at") or 0)
        pct = float(w.get("utilization") or 0) * 100
    except (TypeError, ValueError):
        return None, 0
    if resets and resets <= now:
        return 0.0, resets          # the week reset; its next reading starts at 0
    return pct, resets


def share_for(pct: float, week_resets: int, now: float, mult: float) -> float:
    """The window's share, in percent of the week: what is left of the week,
    spread evenly over the 5-hour blocks to its reset, times ``mult``."""
    left = max(0.0, 100.0 - pct)
    blocks = max(1.0, (week_resets - now) / WINDOW) if week_resets else 7 * 24 / 5
    return max(MIN_SHARE, left / blocks * mult)


def load(path: Path = STATE_PATH) -> dict:
    try:
        d = json.loads(Path(path).read_text(encoding="utf-8"))
        return d if isinstance(d, dict) else {}
    except Exception:
        return {}


def save(st: dict, path: Path = STATE_PATH) -> None:
    try:
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=str(path.parent), suffix=".tmp")
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(st, fh)
        os.replace(tmp, str(path))
    except Exception:
        pass


def step(st: dict, *, pct, week_resets: int, mult: float, now: float) -> tuple:
    """Advance the pacing state one check.  Returns ``(state, events)``:
    events are ``"hit"`` (the window's share is used) and ``"reset"`` (a hit
    window ended: continue the stopped sessions).  Pure: no I/O."""
    st = dict(st or {})
    events = []
    win = st.get("window")
    if not mult:
        if win and win.get("hit"):
            events.append("reset")
        st["window"] = None
        st["last_pct"] = pct
        return st, events
    if pct is None:
        return st, events
    if win and now >= win["reset_at"]:
        if win.get("hit"):
            events.append("reset")
        st["window"] = win = None
    last = st.get("last_pct")
    if win is None:
        # A window opens with the first usage after the last one closed.
        if last is not None and pct > last + 1e-9:
            win = {"start": now, "reset_at": now + WINDOW, "start_pct": last,
                   "share": round(share_for(last, week_resets, now, mult), 3),
                   "mult": mult, "hit": False}
            st["window"] = win
    if win is not None:
        if pct < win["start_pct"]:        # the week reset under an open window
            win["start_pct"] = 0.0
        win["used"] = round(max(0.0, pct - win["start_pct"]), 3)
        if not win.get("hit") and win["used"] >= win["share"]:
            win["hit"] = True
            win["hit_at"] = now
            events.append("hit")
    st["last_pct"] = pct
    return st, events


def public(st: dict, mult: float, pct, week_resets: int, now: float) -> dict:
    """What the 5-hour tile and its section show."""
    win = (st or {}).get("window")
    out = {"mult": mult, "steps": STEPS, "now": now,
           "next_share": round(share_for(pct or 0, week_resets, now, mult or 1), 2)}
    if win and mult:
        out.update({"active": True, "share": win["share"], "used": win.get("used", 0),
                    "reset_at": win["reset_at"], "hit": bool(win.get("hit")),
                    "percent": min(100, round(100 * win.get("used", 0) / win["share"])) if win["share"] else 0})
    else:
        out["active"] = False
    return out


def is_hit(path: Path = STATE_PATH, now: Optional[float] = None) -> Optional[float]:
    """The reset time when the 5-hour pace is hit right now, else None."""
    now = time.time() if now is None else now
    win = load(path).get("window")
    if win and win.get("hit") and now < win.get("reset_at", 0):
        return win["reset_at"]
    return None


# ── Acting on sessions ────────────────────────────────────────────────────

_WORKING = ("working", "starting")


def _states(sm):
    try:
        return sm.get_all_states() or []
    except Exception:
        return []


def stop_working(sm, st: dict) -> list:
    """Interrupt every turn that started after the hit (queued messages
    kept) and remember those sessions.  The turns running at the moment of
    the hit (``finishing``) are left to end; once such a session is idle, a
    later turn of it is interrupted like any other."""
    stopped = []
    states = _states(sm)
    finishing = set(st.get("finishing") or [])
    finishing &= {s.get("session_id") for s in states if s.get("state") in _WORKING}
    st["finishing"] = sorted(finishing)
    for s in states:
        sid = s.get("session_id")
        if not sid or s.get("state") not in _WORKING or sid in finishing:
            continue
        try:
            r = sm.interrupt_session(sid, clear_queue=False)
        except TypeError:
            r = sm.interrupt_session(sid)
        except Exception:
            continue
        if isinstance(r, dict) and r.get("ok"):
            stopped.append(sid)
    if stopped:
        st["paused"] = list(dict.fromkeys(list(st.get("paused") or []) + stopped))
    return stopped


def continue_paused(sm, st: dict) -> list:
    """Tell each session the pace stopped to carry on (not ones slept since)."""
    states = {s.get("session_id"): s.get("state") for s in _states(sm)}
    done = []
    for sid in st.get("paused") or []:
        if states.get(sid) in (None, "stopped"):
            continue
        try:
            r = sm.send_message(sid, CONTINUE_TEXT)
        except Exception:
            continue
        if isinstance(r, dict) and r.get("ok"):
            done.append(sid)
    st["paused"] = []
    return done


def reset(sm, path: Path = STATE_PATH) -> dict:
    """End the window now (the person's Reset button) and continue sessions."""
    with _lock:
        st = load(path)
        st["window"] = None
        resumed = continue_paused(sm, st)
        save(st, path)
    return {"resumed": resumed}


def tick(sm, read_usage, cfg: dict, path: Path = STATE_PATH, now: Optional[float] = None) -> dict:
    """One check: advance the window, act on a hit or a reset."""
    now = time.time() if now is None else now
    pct, week_resets = weekly(read_usage(), now)
    mult = mult_of(cfg)
    with _lock:
        st, events = step(load(path), pct=pct, week_resets=week_resets, mult=mult, now=now)
        out = {"events": events, "stopped": [], "resumed": []}
        if "reset" in events:
            out["resumed"] = continue_paused(sm, st)
        win = st.get("window")
        if "hit" in events:
            # The turns running now may finish; nothing after them may start.
            st["finishing"] = [s.get("session_id") for s in _states(sm)
                               if s.get("session_id") and s.get("state") in _WORKING]
        if win and win.get("hit") and now < win["reset_at"]:
            out["stopped"] = stop_working(sm, st)
        elif not win or not win.get("hit"):
            st["finishing"] = []
        save(st, path)
        out["public"] = public(st, mult, pct, week_resets, now)
    return out


def start(app, socketio) -> None:
    """One web-server thread.  Never in a test app.  Kill switch:
    VIBENODE_PACE_5H=0."""
    if os.environ.get("VIBENODE_PACE_5H", "1") == "0":
        return
    import logging
    log = logging.getLogger("web.pace_5h")

    def _loop():
        from engine import usage_limits as _ul
        from shared.config import get_kanban_config
        while True:
            try:
                sm = getattr(app, "session_manager", None)
                if sm is not None:
                    out = tick(sm, _ul.load, get_kanban_config() or {})
                    if out["events"] or out["stopped"] or out["resumed"]:
                        log.info("5-hour pace: %s stopped=%s resumed=%s",
                                 out["events"], out["stopped"], out["resumed"])
                        socketio.emit("pace_5h", out["public"])
                        if out["stopped"]:
                            socketio.emit("usage_pace_stopped", {"session_ids": out["stopped"]})
            except Exception:
                log.exception("5-hour pace check failed")
            time.sleep(CHECK_SECONDS)

    threading.Thread(target=_loop, daemon=True, name="pace-5h").start()
