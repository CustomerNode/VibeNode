"""A Working summary's estimate runs out: tell the session, then the owner.

Job 7 of ``src/engine/health_monitor.py`` (hot-reloadable like jobs 4-6).
When a Working summary's estimate passes with no report, the session is told
the user is owed an apology, the reason and a new time, and must answer at
once with a ``late`` block; every later miss is told again, firmer; from the
second miss on the owner is alerted directly.

Pure functions here; the monitor owns the clock and the act-once bookkeeping.
A session woken by each job event rewrites its summary with the same promise,
so a promise is identified by (task, estimate, late block), and its clock
starts at the first summary that made it.
"""
from __future__ import annotations

import json
import re

_CARD = re.compile(r"```handoff\s*\n(.*?)\n```", re.S)
_UNIT = re.compile(r"(\d+(?:\.\d+)?)(?:\s*(?:-|–|to)\s*(\d+(?:\.\d+)?))?\s*"
                   r"(h|hr|hrs|hours?|m|min|mins|minutes?)(?![a-z])", re.I)

# After the estimate runs out, give the work this long before acting: a job
# that lands a few seconds late is not a broken promise worth an apology.
GRACE_SECONDS = 120.0

PREFIX = "[VibeNode estimate watch]"


def card_of(text: str) -> dict | None:
    """The last handoff card in an assistant message, or None."""
    found = _CARD.findall(text or "")
    if not found:
        return None
    try:
        card = json.loads(found[-1])
    except (ValueError, TypeError):
        return None
    return card if isinstance(card, dict) else None


def minutes_of(card: dict) -> float:
    """Minutes the Working card promised (0 when it promised none it can be held to).

    A late block's new time wins: it is the newest promise.  Clock times
    ("4:30 pm") are left to the card's own countdown; this watch holds the
    session to durations only.
    """
    late = card.get("late") if isinstance(card.get("late"), dict) else {}
    needs = card.get("needs") if isinstance(card.get("needs"), dict) else {}
    for v in (late.get("new_eta_minutes"), needs.get("eta_minutes")):
        try:
            if v is not None and not isinstance(v, bool) and float(v) > 0:
                return float(v)
        except (TypeError, ValueError):
            pass
    t = str(needs.get("eta") or "").lower()
    total = 0.0
    for a, b, unit in _UNIT.findall(t):
        total += float(b or a) * (60.0 if unit.startswith("h") else 1.0)
    if not total and "half an hour" in t:
        total = 30.0
    elif not total and re.search(r"\ban hour\b", t):
        total = 60.0
    return total


def promise(card: dict | None) -> tuple | None:
    """``(key, minutes)`` for a Working card that promised a duration, else None."""
    if not card or card.get("type") != "working":
        return None
    minutes = minutes_of(card)
    if minutes <= 0:
        return None
    zoom = card.get("zoom") if isinstance(card.get("zoom"), dict) else {}
    needs = card.get("needs") if isinstance(card.get("needs"), dict) else {}
    late = card.get("late") if isinstance(card.get("late"), dict) else {}
    key = (str(zoom.get("task") or card.get("title") or ""),
           str(needs.get("eta_minutes") or needs.get("eta") or ""),
           json.dumps(late, sort_keys=True) if late else "")
    return key, minutes


def task_of(key: tuple) -> str:
    return key[0] if key else ""


def build_message(minutes: float, miss: int) -> str:
    """The nudge sent to the session, first miss and later ones."""
    eta = int(round(minutes))
    if miss <= 1:
        return (f"{PREFIX} You told the user this would take about {eta} minutes. That time "
                "has passed and you haven't reported. The user is not happy and is owed three "
                "things, now: an apology, a plain explanation of why it's late, and a new time "
                "and how sure you are. Answer right away with a card that has a `late` block "
                "(\"late\": {\"apology\", \"why\", \"new_eta_minutes\", \"confidence\"}). Do not "
                "wait for the work to finish to send it.")
    nth = {2: "second", 3: "third"}.get(miss, f"{miss}th")
    return (f"{PREFIX} This is the {nth} time you've missed. Your last update said {eta} more "
            "minutes, and that has passed too. The user is not happy and is owed three things, "
            "now: an apology, a plain explanation of why it's late, and a new time and how sure "
            "you are. Measure the remaining work before you give a time; don't guess again. "
            "Answer right away with a card that has a `late` block (\"late\": {\"apology\", "
            "\"why\", \"new_eta_minutes\", \"confidence\"}). Do not wait for the work to finish "
            "to send it.")
