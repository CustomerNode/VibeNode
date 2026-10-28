"""What the PERSON has to do next, read from a session's handoff card.

The owner decided this on 2026-10-27: sidebar status is oriented to the human,
not the process.  "Idle" is split by what the last message needs from him:

    decide   pick between options it spelled out
    unblock  only he can do it: log in, restart, give access ("blocked")
    review   it says the work is done; check it
    read     a report, an answer, findings
    send     a draft for someone else (an email, a post, a reply)
    waiting  it continues by itself (its own background work)

Approve / Working / Waiting-on-a-timer / Idle / Sleeping come from live state
in the browser (``static/js/session-verb.js``); this module only reads the
card.  The card is the fenced ``handoff`` JSON block Smart termination asks
every session to end with (``daemon/handoff_rules.py``).

Pure, no I/O.  The browser has a mirror of ``verb_from_card`` in
``session-verb.js``; ``tests/test_session_verb.py`` keeps the two in step.
"""

from __future__ import annotations

import json
import re
from typing import Optional

FENCE_LANG = "handoff"
VERBS = ("decide", "unblock", "review", "read", "send", "waiting")

# Deliverable kinds that are read, not sent somewhere.
_READ_KINDS = re.compile(r"report|doc|summary|notes|analysis|audit|findings|plan|research", re.I)

# Both fences start a line, as in the checker (daemon/handoff_rules.py _FENCE):
# a ``` inside the card's own text must not end it (2026-10-28).
_BLOCK = re.compile(r"(?ms)^[ \t]*(`{3,}|~{3,})[ \t]*" + FENCE_LANG + r"[^\n]*\n(.*?)^[ \t]*\1[`~]*[ \t]*$")


def last_card(text: str) -> Optional[dict]:
    """The JSON object in the LAST ```handoff block of *text*, or None."""
    if not text or FENCE_LANG not in text:
        return None
    blocks = [m.group(2) for m in _BLOCK.finditer(text)]
    if not blocks:
        return None
    try:
        card = json.loads(blocks[-1])
    except Exception:
        return None
    return card if isinstance(card, dict) else None


def verb_from_card(card: dict) -> Optional[str]:
    """Map a card to the person's verb (see the module docstring)."""
    if not isinstance(card, dict):
        return None
    t = str(card.get("type") or "").lower()
    needs = card.get("needs") if isinstance(card.get("needs"), dict) else {}
    nk = str(needs.get("kind") or "").lower()
    if nk in ("decision", "approve"):
        return "decide"
    if nk == "action" or t == "blocked":
        return "unblock"
    if t == "needs_you":
        return "decide"
    if t == "working":
        return "waiting"
    if t == "deliverable":
        d = card.get("deliverable") if isinstance(card.get("deliverable"), dict) else {}
        # Files to download are taken, not sent: Got it (2026-10-28).
        if isinstance(d.get("files"), list) and d.get("files"):
            return "read"
        return "read" if _READ_KINDS.search(str(d.get("kind") or "")) else "send"
    if t == "explain":
        return "read"
    if t == "done":
        return "review"
    return None


def card_line(card: dict) -> str:
    """One plain line for a tooltip: the ask when there is one, else the headline."""
    if not isinstance(card, dict):
        return ""
    needs = card.get("needs") if isinstance(card.get("needs"), dict) else {}
    ask = needs.get("ask") if str(needs.get("kind") or "none") != "none" else ""
    return str(ask or card.get("headline") or "")[:200]


def next_step(text: str) -> Optional[dict]:
    """``{"verb", "line"}`` for a session whose final message is *text*, or
    None when that message carries no usable card or handoff."""
    card = last_card(text)
    verb = verb_from_card(card) if card is not None else None
    # Status comes from the card's type only; never guessed from free text.
    return {"verb": verb, "line": card_line(card)} if verb is not None else None
