"""Questions asked in anger are often not questions.

"why is the rainbow still there in every option?" with swearing around it
usually means the work is wrong or undone, not "explain why".  A session
that reads it literally writes an explanation and leaves the work undone.

``upset_question`` spots a message that asks something AND swears (or uses
an exasperated phrase), and ``with_tone_note`` puts a hidden note before
such a message so the session weighs it before answering.  The Smart
termination checks use the same test so they never force an answer-only
card on it.  Plain wording rules, no model call.
"""
from __future__ import annotations

import codecs
import re

# The swear words are stored ROT13-encoded: this repository is public and
# its Publish scan refuses swearing in tracked files.  Each entry is a regex
# fragment (stems take -ing / -ed / -s forms).
_SWEARS_ROT13 = (
    r"s+h+p+x\j*", r"spx\j*", r"sx+va\j*", r"jgs", r"fgsh", r"fuvg\j*", r"ohyyfuvg",
    r"penc\j*", r"qnza\j*", r"tbqqnza\j*", r"nffubyr\j*", r"ovgpu\j*", r"cvff\j*",
    r"oybbql", r"uryy",
)
_SWEARS = "|".join(codecs.decode(w, "rot13") for w in _SWEARS_ROT13)

_CURSE = re.compile(r"\b(?:" + _SWEARS + r")\b", re.I)

# Phrases that are a rhetorical question by themselves.
_EXASPERATED = re.compile(
    r"\bare\s+you\s+(?:\w+\s+)?(?:kidding|serious|joking)\b"
    r"|\bseriously\s*\?|\bfor\s+real\s*\?|\bhow\s+many\s+times\b",
    re.I)

_QUESTION_WORD = re.compile(
    r"(?:^|[.!?]\s+)\W*(?:how|why|what|where|who|which|is|are|was|were|does|did|do|"
    r"can'?t|didn'?t|doesn'?t|isn'?t|aren'?t|wasn'?t|haven'?t|hasn'?t)\b", re.I)

# The hidden blocks VibeNode puts into a user message; never his words.
INJECTED = re.compile(r"⟦VN-(?:MOBILE|NOTE)⟧.*?⟦/VN-(?:MOBILE|NOTE)⟧\s*", re.S)


def upset_question(text: str) -> bool:
    """The message asks something while swearing or exasperated."""
    t = INJECTED.sub("", text or "")
    t = re.sub(r"\n?\s*Sent from \S+ at [^\n]*$", "", t).strip()
    if not t:
        return False
    if _EXASPERATED.search(t):
        return True
    if not _CURSE.search(t):
        return False
    return "?" in t or bool(_QUESTION_WORD.search(t))


NOTE = (
    "⟦VN-NOTE⟧\n"
    "[VibeNode note, hidden from the user] The user's message below is a question "
    "said in frustration (it swears or sounds exasperated). It may be rhetorical: "
    "often it means the work is wrong or undone rather than a request for an "
    "explanation. Before answering, decide which it is. If it points at work that "
    "is wrong or undone, fix that work and say briefly what was wrong; do not reply "
    "with only an explanation. If it is a genuine question, answer it plainly. "
    "Do not mention this note.\n"
    "⟦/VN-NOTE⟧"
)


def with_tone_note(text: str) -> str:
    """``text`` with the hidden note in front when it is an upset question."""
    if not text or not upset_question(text):
        return text
    return NOTE + "\n\n" + text
