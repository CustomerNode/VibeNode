"""Weekly usage pace: warn on a fast day, suggest work when there is spare.

The weekly window (``seven_day`` in src/engine/usage_limits.py) is the only cap
left once the 5-hour session limit is gone, so a busy day can spend several
days of the week.  This module turns the CLI's weekly utilization readings
into two signals, both optional:

* **Fast day** -- in any rolling 24 hours, every whole "day of usage"
  (1/7 of the weekly limit) spent raises a level: 1 day, 2 days, ...
  Each new level fires once.  The level falls back as the 24 hours roll on,
  so the same level can fire again on a later fast day.
* **Spare** -- the usage left in the week is at least ``SPARE_FACTOR``
  times what the days left until the reset would normally spend.  Fires at
  most once per weekly window.

Where the readings come from: the persisted tracker state
(``engine.usage_limits.load()``), sampled by a web-server thread.  A sample
is kept per reading change, so the history is small (one week, pruned).
The pure functions below take the history and the clock so tests drive them
directly.
"""

from __future__ import annotations

import json
import os
import tempfile
import threading
import time
from pathlib import Path
from typing import Optional

DAY = 86400.0
WEEK_DAYS = 7
DAY_SHARE = 1.0 / WEEK_DAYS          # one day of usage, as a utilization share
SPARE_FACTOR = 2.0                   # spare when left >= 2x the normal pace
WINDOW = "seven_day"
SAMPLE_SECONDS = 60

ROOT = Path(__file__).resolve().parents[2]
HISTORY_PATH = ROOT / "data" / "usage_history.json"


# ---------------------------------------------------------------- history

def add_sample(history: dict, util: float, resets_at: int, now: float) -> dict:
    """Append one reading.  A reading equal to the last one is not stored
    again.  Samples older than 8 days are dropped."""
    samples = [s for s in (history or {}).get("samples", [])
               if isinstance(s, dict) and now - s.get("t", 0) <= 8 * DAY]
    last = samples[-1] if samples else None
    if not last or last.get("u") != util or last.get("r") != resets_at:
        samples.append({"t": now, "u": float(util), "r": int(resets_at)})
    out = dict(history or {})
    out["samples"] = samples
    return out


def used_in_last_day(samples: list, now: float) -> float:
    """Weekly utilization spent in the last 24 hours.

    Sums the rises between consecutive readings inside the last 24 hours,
    within the same weekly window.  A reset (new ``resets_at``, utilization
    falling) counts the new window's reading from zero, so usage across a
    reset is not lost and the reset itself is never negative usage.
    """
    since = now - DAY
    prev = None
    total = 0.0
    for s in samples:
        t, u, r = s.get("t", 0), float(s.get("u", 0)), s.get("r", 0)
        if prev is not None and t > since:
            if r == prev["r"] and u >= prev["u"]:
                total += u - prev["u"]
            elif r != prev["r"]:
                total += u          # new window: everything in it is new
        prev = {"u": u, "r": r}
    return max(0.0, total)


def day_curve(samples: list, now: float) -> list:
    """Cumulative usage over the last 24 hours as ``[[hours_ago, share]]``,
    oldest first, for the warning's curve.  Starts at ``[24, 0]``."""
    since = now - DAY
    pts = [[24.0, 0.0]]
    prev, total = None, 0.0
    for s in samples:
        t, u, r = s.get("t", 0), float(s.get("u", 0)), s.get("r", 0)
        if prev is not None and t > since:
            if r == prev["r"] and u >= prev["u"]:
                total += u - prev["u"]
            elif r != prev["r"]:
                total += u
            pts.append([round((now - t) / 3600, 2), round(total, 4)])
        prev = {"u": u, "r": r}
    pts.append([0.0, round(total, 4)])
    return pts


def fast_day_level(samples: list, now: float) -> int:
    """Whole days of usage spent in the last 24 hours (0, 1, 2, ...).

    A small epsilon absorbs the CLI's 1%-step rounding (1/7 = 14.29%, so 14%
    must not miss 1 day forever, and 15% must not read as 1.05 days twice).
    """
    return int((used_in_last_day(samples, now) + 0.005) / DAY_SHARE)


def spare(util: float, resets_at: int, now: float) -> Optional[dict]:
    """The spare-capacity reading, or None when there is no spare.

    ``days_left`` is usage left in days of usage; ``days_to_reset`` is the
    calendar time left in the window.  Spare means days_left >=
    SPARE_FACTOR * days_to_reset.  Within the last 6 hours before the reset
    nothing is suggested: a long task would not get to use it.
    """
    if not resets_at or resets_at <= now:
        return None
    days_to_reset = (resets_at - now) / DAY
    if days_to_reset < 0.25:
        return None
    days_left = max(0.0, 1.0 - float(util)) * WEEK_DAYS
    if days_left < SPARE_FACTOR * days_to_reset:
        return None
    return {"days_left": round(days_left, 1),
            "days_to_reset": round(days_to_reset, 1),
            "resets_at": int(resets_at)}


def evaluate(history: dict, util: float, resets_at: int, now: float) -> tuple:
    """Decide what fires now.  Returns ``(new_history, events)``.

    ``history`` keeps the bookkeeping: ``warned_level`` (the highest
    fast-day level announced while it held; lowered as the day rolls on) and
    ``spare_window`` (the ``resets_at`` of the window already suggested in).
    """
    h = add_sample(history, util, resets_at, now)
    events = []
    level = fast_day_level(h["samples"], now)
    warned = int(h.get("warned_level", 0) or 0)
    if level > warned:
        events.append({"kind": "fast_day", "days": level,
                       "used_percent": round(used_in_last_day(h["samples"], now) * 100),
                       "week_percent": round(float(util) * 100),
                       "curve": day_curve(h["samples"], now),
                       "resets_at": int(resets_at)})
    h["warned_level"] = level            # follows the rolling day down too
    sp = spare(util, resets_at, now)
    if sp and h.get("spare_window") != int(resets_at):
        events.append({"kind": "spare", **sp})
        h["spare_window"] = int(resets_at)
    return h, events


# ---------------------------------------------------------------- storage

def load(path: Optional[Path] = None) -> dict:
    path = path or HISTORY_PATH
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def save(history: dict, path: Optional[Path] = None) -> None:
    path = path or HISTORY_PATH
    try:
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=str(path.parent), suffix=".tmp")
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(history, f)
        os.replace(tmp, str(path))
    except Exception:
        pass


def tick(read_state, path: Optional[Path] = None, now: Optional[float] = None) -> list:
    """One sampling pass: read the tracker, update the history, return the
    events that fire.  A missing or expired weekly reading does nothing."""
    path = path or HISTORY_PATH
    now = time.time() if now is None else now
    w = ((read_state() or {}).get("windows") or {}).get(WINDOW)
    if not isinstance(w, dict):
        return []
    resets = int(w.get("resets_at") or 0)
    if not resets or resets <= now:
        return []
    history, events = evaluate(load(path), float(w.get("utilization", 0)), resets, now)
    save(history, path)
    return events


# ---------------------------------------------------------------- pop-ups

PENDING_MAX_AGE = 6 * 3600   # a pop-up nobody saw within 6 hours is dropped
PREF_KEYS = {"fast_day": "usage_pace_warning", "spare": "usage_spare_suggest"}


def enabled(kind: str, cfg: dict) -> bool:
    """Both pop-ups are on unless their Preferences switch is turned off."""
    return (cfg or {}).get(PREF_KEYS[kind], True) is not False


def pending(path: Optional[Path] = None, now: Optional[float] = None) -> list:
    """Pop-ups fired but not yet answered in any tab (newest per kind)."""
    path = path or HISTORY_PATH
    now = time.time() if now is None else now
    return [e for e in load(path).get("pending", [])
            if isinstance(e, dict) and now - e.get("at", 0) <= PENDING_MAX_AGE]


def add_pending(events: list, path: Optional[Path] = None, now: Optional[float] = None) -> None:
    path = path or HISTORY_PATH
    now = time.time() if now is None else now
    h = load(path)
    keep = {e.get("kind"): e for e in pending(path, now)}
    for e in events:
        keep[e["kind"]] = {**e, "at": now}
    h["pending"] = list(keep.values())
    save(h, path)


def ack(kind: str, path: Optional[Path] = None) -> None:
    path = path or HISTORY_PATH
    h = load(path)
    h["pending"] = [e for e in h.get("pending", []) if e.get("kind") != kind]
    save(h, path)


def start(app, socketio) -> None:
    """One daemon thread in the web server: sample every minute and push
    ``usage_pace`` to every open tab for each pop-up that fires and is on.
    Never in a test app.  Kill switch: VIBENODE_USAGE_PACE=0."""
    if os.environ.get("VIBENODE_USAGE_PACE", "1") == "0":
        return
    import logging
    log = logging.getLogger("web.usage_pace")

    def _loop():
        from engine import usage_limits as _ul
        from shared.config import get_kanban_config
        while True:
            try:
                events = tick(_ul.load)
                cfg = get_kanban_config() or {}
                events = [e for e in events if enabled(e["kind"], cfg)]
                if events:
                    add_pending(events)
                    for e in events:
                        socketio.emit("usage_pace", e)
            except Exception:
                log.exception("usage pace check failed")
            time.sleep(SAMPLE_SECONDS)

    threading.Thread(target=_loop, daemon=True, name="usage-pace").start()


# ---------------------------------------------------------------- projects

def busiest_projects(projects_dir: Path, now: Optional[float] = None,
                     days: int = 30) -> list:
    """Project folders ranked by sessions active in the last ``days`` days
    (a transcript written to in that time), most first.  Each entry carries
    the folder the sessions ran in, read from a transcript's own ``cwd`` so a
    folder name with dashes is never guessed.  Folders that no longer exist
    are left out."""
    now = time.time() if now is None else now
    since = now - days * DAY
    out = []
    try:
        dirs = [d for d in Path(projects_dir).iterdir() if d.is_dir()]
    except OSError:
        return []
    for d in dirs:
        recent = []
        for f in d.glob("*.jsonl"):
            try:
                m = f.stat().st_mtime
            except OSError:
                continue
            if m >= since:
                recent.append((m, f))
        if not recent:
            continue
        recent.sort(reverse=True)
        cwd = ""
        for _, f in recent[:5]:
            cwd = _transcript_cwd(f)
            if cwd:
                break
        if not cwd or not Path(cwd).is_dir():
            continue
        out.append({"encoded": d.name, "cwd": cwd, "recent": len(recent),
                    "last": recent[0][0]})
    out.sort(key=lambda p: (-p["recent"], -p["last"]))
    return out


def _transcript_cwd(path: Path) -> str:
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            for i, line in enumerate(fh):
                if i > 40:
                    break
                if '"cwd"' not in line:
                    continue
                try:
                    c = json.loads(line).get("cwd")
                except Exception:
                    continue
                if isinstance(c, str) and c:
                    return c
    except OSError:
        pass
    return ""


def task_prompt(task: str) -> str:
    """The prompt a spare-usage job starts with: the person's task, told to
    run long and on its own."""
    return (
        task.strip() + "\n\n"
        "This is a long-running autonomous job, started to use spare weekly "
        "usage. Work through it end to end without waiting for me: plan, "
        "measure where it applies, make the changes, verify each with tests "
        "or real measurements, and keep going to the next improvement until "
        "the job is thoroughly done. Do not stop to ask for routine choices; "
        "pick the sensible default and note it. Do not publish, push or "
        "delete anything outside this project. At the end, report what "
        "changed and what you verified."
    )
