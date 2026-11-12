"""Handoff rules: the last message of a turn must be one of the allowed card
types, and each type has phrases that send the session back to think again.

This is the second layer of Smart termination
(``src/engine/smart_termination.py``):

1. **Format.**  A session's final message ends with ONE fenced block tagged
   ``handoff`` holding a JSON object of one of the card types: done,
   needs_you, working, blocked, deliverable, explain.  Anything else is sent
   back.
2. **Banned phrases, by type.**  Each group below names the habit it stands
   for, because the habit is what is banned; the phrases are how it shows
   up.  One habit (calling work "done" with unfinished work attached) shows
   up under a dozen wordings, so a group is banned, not a phrase.

Every string in the card is checked, including the folded-away ones (Worth
knowing, Technical details): those are where a caveat moves when it is not
allowed anywhere else.  Prose written above the block is checked too.

Pure and backend-neutral: no I/O, no SessionManager.  ``review()`` returns the
reason to send the session back, or None to let the turn end.
"""

from __future__ import annotations

import json
import os
import re
import urllib.parse
from pathlib import Path
from typing import Iterator, Optional

FENCE_LANG = "handoff"

TYPES = ("done", "needs_you", "working", "blocked", "deliverable", "explain")
# How a deliverable's body is drawn (last-card.js dvFormat; "plain" and
# "plaintext" are read as "text").
DELIVERABLE_FORMATS = ("markdown", "html", "text", "plain", "plaintext")
NEEDS_KINDS = ("none", "decision", "approve", "action")

# Every kind of card a session can end on, named the way the user sees it.
# The "Redo as…" button on the newest card (src/web/static/js/last-card.js,
# REDO_KINDS) lists exactly these, word for word; tests/test_card_redo.py
# fails when a type or a needs.kind above has no entry here, or when the two
# lists differ.  (key, type, needs.kind or None for any, name, what the user gets)
CARD_KINDS = (
    ("explain", "explain", None, "Answer", "Just answer my question"),
    ("done", "done", None, "Done", "Do the work, then show me what changed"),
    ("decision", "needs_you", "decision", "Decide", "Show me the options first, I pick"),
    ("approve", "needs_you", "approve", "Approve", "Ask me yes or no before you go on"),
    ("action", "needs_you", "action", "To do", "Tell me the one step only I can do"),
    ("deliverable", "deliverable", None, "Deliverable", "Give me the text or file itself"),
    ("working", "working", None, "Working", "Keep going and tell me when it will be done"),
    ("blocked", "blocked", None, "Blocked", "Tell me what is stopping you and what you tried"),
)

# The message the "Redo as…" button sends as the user (last-card.js
# _redoMessage): "Wrong kind of card: you ended on a Done card. I wanted a
# Decide card: show me the options first, I pick. Redo your last reply that way."
REDO_PREFIX = "Wrong kind of card:"
_REDO_WANT = re.compile(r"\bI wanted (?:an?\s+)?(.+?) card\b", re.I)


def redo_wanted(text) -> Optional[str]:
    """The CARD_KINDS key a "Redo as…" message asks for, or None."""
    t = (text or "").strip() if isinstance(text, str) else ""
    if not t.startswith(REDO_PREFIX):
        return None
    m = _REDO_WANT.search(t.split("\n", 1)[0])
    if not m:
        return None
    name = m.group(1).strip().lower()
    return next((k for k, _t, _n, nm, _d in CARD_KINDS if nm.lower() == name), None)


def card_is_kind(card, key: str) -> bool:
    """Is ``card`` the CARD_KINDS kind ``key``?"""
    row = next((r for r in CARD_KINDS if r[0] == key), None)
    if row is None or not isinstance(card, dict):
        return False
    needs = card.get("needs") if isinstance(card.get("needs"), dict) else {}
    return card.get("type") == row[1] and (row[2] is None or needs.get("kind") == row[2])

# Fields that are not prose the user reads as the session's words: links,
# commands, button keys, enum values, and the reply text a button sends AS
# the user.
_SKIP_KEYS = frozenset({"url", "href", "command", "key", "kind", "type", "when", "reply", "recommended", "ok"})

# ── Phrase groups ─────────────────────────────────────────────────────────
# (name, what to tell the session, regex).  Matching is case-insensitive on
# text with curly quotes straightened.

def _rx(*alts: str) -> re.Pattern:
    return re.compile(r"(?:" + "|".join(alts) + r")", re.I | re.M)


UNFINISHED = (
    "unfinished work",
    "This lists unfinished work in a card that says it is finished. Finish it "
    "now. If what is left truly needs the user (a decision that is theirs, "
    "access you lack, an action you may not take), use type \"needs_you\"; if "
    "something outside your control stops you, use \"blocked\". A caveat, an "
    "open item or a flag is not a reason to stop.",
    _rx(r"\bcaveats?\b", r"\bstill\s+open\b", r"\bopen\s+(?:items?|issues?|questions?|problems?)\b",
        r"\bleftovers?\b", r"\bdeferred\b", r"\bparked\b", r"\bfor\s+later\b", r"\bfollow[\s-]?ups?\b",
        r"\bnot\s+(?:yet\s+)?(?:verified|validated|tested|live)\b", r"\bunverified\b", r"\buntested\b",
        r"\bhaven'?t\s+(?:yet\s+)?(?:checked|tested|verified|run|seen|watched|clicked|confirmed)\b",
        r"\bhasn'?t\s+(?:been\s+through|had)\s+a\s+full\b", r"\bshould\s+(?:now\s+)?work\s+now\b",
        r"\bshould\s+work\b", r"\breload\s+to\s+see\b", r"\bleft\s+(?:it\s+)?alone\b",
        r"\bflag(?:ged|ging)?,?\s+(?:not|but\s+not|rather\s+than|only)\b", r"\bflags?,\s+not\b",
        r"\bnot\s+acted\s+on\b", r"\bno\s+action\s+taken\b", r"\bknown\s+(?:issues?|gaps?|limitations?)\b",
        r"\bout\s+of\s+scope\b", r"\bnot\s+done\b", r"\bnot\s+(?:yet\s+)?(?:fixed|finished)\b",
        r"\bleaving\s+(?:it\s+|them\s+|this\s+)?open\b",
        # A heading in Full that parks a known problem ("## Flag").
        r"^#{1,6}\s*(?:flags?|known\s+(?:issues?|gaps?)|open\s+items?|caveats?)\s*:?\s*$"),
)

BLAME = (
    "not my fault",
    "This explains a problem away as someone else's instead of dealing with it. "
    "If it stops the work you were asked to do, fix it. If you truly cannot, "
    "use type \"blocked\" and say what blocks you and what you tried.",
    _rx(r"\bunrelated\b", r"\bnot\s+(?:from\s+|caused\s+by\s+)?(?:my|this)\s+change\b",
        r"\b(?:not|isn'?t)\s+mine\b", r"\banother\s+session'?s\b", r"\bpre-?existing\b", r"\bpredates?\b",
        r"\bfail(?:s|ed|ing)?\s+(?:the\s+same\s+way\s+)?(?:on\s+develop|without\s+my\s+change|before\s+(?:this|my)\s+change)",
        r"\bfails\s+on\s+develop\s+too\b", r"\bpass(?:es|ed)?\s+(?:when\s+run\s+)?alone\b",
        r"(?<!\bno\s)(?<!\bzero\s)(?<!\b0\s)\bflak(?:y|e|es)\b", r"\bfails?\s+occasionally\b", r"\b(?:been\s+)?like\s+(?:this|that)\s+all\s+along\b"),
)

HAND_BACK = (
    "handing the job back",
    "This hands the user a job or a question that the card's buttons, or you, "
    "should handle. If it is yours to do, do it. If it truly needs the user, "
    "use type \"needs_you\" with the choices as reply buttons, each spelled out "
    "in full. An idea you would like to offer is not a decision: say it in "
    "needs.next as what could come next.",
    _rx(r"\bwant\s+me\s+to\b", r"\bshould\s+I\b", r"\bdo\s+you\s+want\b", r"\bwhich\s+(?:one\s+)?do\s+you\s+want\b",
        r"\b(?:still\s+)?your\s+call\b", r"\byours\s+to\s+decide\b", r"\bdecisions?\s+(?:still\s+)?(?:for\s+you|yours)\b",
        r"\bopen\s+decisions?\b", r"\bI\s+need\s+(?:one|a)\s+decision\b",
        r"\bsay\s+[\"']?(?:go|yes|push|ship|release|build|the\s+word)\b",
        r"\breply\s+[\"'][^\"']{1,30}[\"']\s+(?:to|and)\b", r"\bmy\s+pick\b", r"\bI'?m\s+not\s+starting\b",
        r"\bI\s+won'?t\s+[^.]{0,40}\bunless\s+you\b", r"\bnothing\s+changes\s+until\s+you\b",
        r"\bdo\s+one\s+of\s+these\b", r"\bpaste\s+it\s+(?:in\s+)?yourself\b", r"\bI'?d\s+ship\b",
        r"\bwhat\s+I'?d\s+build\b"),
)

CHORES = (
    "physical work pushed onto the user",
    "This asks the user to do hands-on troubleshooting or says what you cannot "
    "do. Find a way to do it yourself. Only access you truly lack (a login, a "
    "password) belongs in a \"needs_you\" action card.",
    _rx(r"\bunplug\b", r"\bcheck\s+the\s+cables?\b", r"\b(?:different|another)\s+(?:wall\s+)?outlet\b",
        r"\b(?:reboot|restart)\s+(?:your|the)\s+(?:computer|machine|laptop|phone|router|pc|mac)\b",
        r"\bI\s+(?:can'?t|cannot)\s+control\b", r"\ba\s+\d+[-\s]minute\s+test\b", r"\blet\s+me\s+run\s+an\s+experiment\b"),
)

PARKED = (
    "parked and calling it waiting",
    "This is the session's own bookkeeping, or a wait with nothing behind it. "
    "Do not send notes to yourself. If you are really waiting on your own "
    "background work, use type \"working\" with what it is and when it ends; "
    "if you are waiting on the user, use \"needs_you\".",
    _rx(r"\bprivately\b", r"\brequestable\b", r"\bwatching\s+for\s+your\s+tap\b",
        r"\bI'?ll\s+come\s+back\s+(?:when|once|after)\b", r"\bI'?ll\s+report\s+(?:back\s+)?when\s+it\s+lands\b"),
)

APOLOGY = (
    "ritual apology",
    "Drop the apology and the promise. The next session will not remember "
    "it. Say what was done.",
    _rx(r"\bfrom\s+now\s+on,?\s+I\b", r"\bthat'?s\s+on\s+me\b", r"\bthat\s+is\s+on\s+me\b", r"\bmy\s+mistake\b",
        r"\bI\s+should\s+have\b", r"^\W*you'?re\s+right\b", r"^\W*understood\b", r"^\W*noted\b",
        r"\bI\s+hear\s+you\b", r"\bI'?m\s+listening\b", r"^\W*listening\b",
        r"\bI'?ve\s+(?:saved|written|recorded)\s+(?:a|that|the|this)\s+(?:lesson|rule|memory)\b",
        r"\bwon'?t\s+(?:raise|mention|bring\s+up)\s+(?:it|this|that|them)\s+again\b"),
)

AI_TELL = (
    "words that sound like AI",
    "Rewrite these in plain words.",
    _rx(r"\bquietly\b", r"\bgenuinely\b", r"\bhonest(?:ly)?\b", r"\bpick\s+a\s+voice\b"),
)

NOISE = (
    "status noise",
    "The card already says where things stand. Cut the filler.",
    _rx(r"\bhere'?s\s+where\s+(?:it|things|we)\s+stands?\b", r"\bhere'?s\s+the\s+map\b", r"^\W*status\s*:",
        r"\breminders\s+you\s+asked\s+for\b", r"\bmy\s+list\s+is\s+empty\b",
        r"\bnothing\s+(?:is\s+)?needed\s+from\s+you\b", r"\bnothing\s+needs\s+you\b",
        r"\bnothing\s+for\s+you\s+to\s+do\b"),
)

# A file named in the card's text: the user reads it in a chat window where
# a path cannot be opened.  Files go in "links" / needs.look, which the card
# draws as rows the user can tap.  URLs are not matched
# (they are clickable); neither are product names like "Node.js".
FILE_PATHS = (
    "file names in the text",
    "The user reads this in a chat window and can't open a path from text. "
    "Say what the file does in plain words, and put the file itself in "
    "\"links\" (or needs.look) so the card shows it as a row the user can tap.",
    _rx(r"(?<![\w/:.])(?:~|\.{1,2})?/?(?:[\w.-]+/)+[\w.-]+\.[a-z][a-z0-9]{0,4}\b(?!/)",
        # Case-sensitive: "style.css" is a file, "Node.js" is a product.
        r"(?<![\w/:.@-])(?-i:[a-z_][\w-]*\.(?:py|pyi|js|mjs|cjs|ts|tsx|jsx|css|scss|html|md|json|ya?ml|toml|sh|ini|cfg|sql|go|rs|rb|java))\b"),
)

# Which groups each type may NOT contain.  Every type gets the always-on
# groups.  Two deliberate exceptions:
# - "blocked" may say why the problem is not its own: that is what a
#   blocker is.  "explain" may too, when the user asked whose it is.
# - "needs_you" carries the ask in its buttons, so hand-back wording is
#   allowed inside needs.ask, nowhere else (see _checks()).
_ALWAYS = (APOLOGY, AI_TELL, NOISE, PARKED, CHORES, HAND_BACK, FILE_PATHS)
BANNED_BY_TYPE = {
    "done": _ALWAYS + (UNFINISHED, BLAME),
    "deliverable": _ALWAYS + (UNFINISHED, BLAME),
    "explain": _ALWAYS + (UNFINISHED,),
    "needs_you": _ALWAYS + (BLAME,),
    "working": _ALWAYS + (BLAME,),
    "blocked": _ALWAYS,
}

# A bare label from earlier in the session ("option C", "point 5", "B or C").
# Same rule as smart_termination: explained when followed by ( : = or a dash.
_LABEL = re.compile(
    r"\b(?i:option|options|point|step|phase|task|item|variant|choice|part|"
    r"fix|issue|finding|trial|question|decision)\s+#?(?:[A-Z]|\d+[a-z]?)\b(?!\s*[(:=—–-])"
    r"|\b(?![AI]\b)[A-Z]\s+or\s+(?![AI]\b)[A-Z]\b(?!\s*[(:=—–-])"
    r"|\bQ\d+\b(?!\s*[(:=—–-])"
)


# ── Parsing ───────────────────────────────────────────────────────────────

_FENCE = re.compile(r"(?ms)^[ \t]*(`{3,}|~{3,})[ \t]*" + FENCE_LANG + r"[ \t]*\n(.*?)^[ \t]*\1[ \t]*$")


def _norm(text: str) -> str:
    return (text or "").replace("’", "'").replace("‘", "'").replace("\r\n", "\n")


def extract(message: str) -> tuple[Optional[dict], str, Optional[str]]:
    """``(card, prose_above, error)`` for *message*.

    The card is the LAST ``handoff`` block, which must be the last thing in
    the message.  ``error`` says what is wrong when there is no usable card.
    """
    text = _norm(message)
    blocks = list(_FENCE.finditer(text))
    if not blocks:
        return None, text, "Your final message has no ```" + FENCE_LANG + " block."
    m = blocks[-1]
    if text[m.end():].strip():
        return None, text, "The ```" + FENCE_LANG + " block must be the last thing in your final message."
    prose = _FENCE.sub("", text[:m.start()])   # an earlier card is not prose
    try:
        card = json.loads(m.group(2))
    except ValueError as e:
        return None, prose, f"The ```{FENCE_LANG} block is not valid JSON ({e})."
    if not isinstance(card, dict):
        return None, prose, f"The ```{FENCE_LANG} block must hold one JSON object."
    return card, prose, None


def _strings(node, path: str = "") -> Iterator[tuple[str, str]]:
    """Every user-facing string in the card, with its path."""
    if isinstance(node, str):
        yield path, node
    elif isinstance(node, dict):
        for k, v in node.items():
            if k in _SKIP_KEYS:
                continue
            yield from _strings(v, f"{path}.{k}" if path else k)
    elif isinstance(node, list):
        for i, v in enumerate(node):
            yield from _strings(v, f"{path}[{i}]")


def _s(v) -> str:
    return v.strip() if isinstance(v, str) else ""


def _items(v) -> list:
    return v if isinstance(v, list) else []


def _item_text(it) -> str:
    if isinstance(it, str):
        return it.strip()
    if isinstance(it, dict):
        return _s(it.get("what")) or _s(it.get("label")) or _s(it.get("check"))
    return ""


# A job for the user hidden in a "nothing needed" card: an instruction at the
# start of a sentence ("Reload VibeNode and ..."), or "you need to / must ...".
_CHORE_IN_NOTHING = re.compile(
    r"(?:^|[.;:!?]\s+)(?:please\s+)?(?:reload|refresh|restart|reopen|re-open|open\s+(?:the|a|your|it)\b|click|tap|press|run|log\s+in|"
    r"sign\s+in|install|update|clear|close)\b"
    r"|\byou(?:'ll)?\s+(?:need|have|must|should|will\s+need)\s+to\b"
    r"|\b(?:after|once|when)\s+you\s+(?:reload|refresh|restart|reopen|open|log\s+in|sign\s+in)\b"
    r"|\b(?:after|on|with)\s+(?:a|the|your)?\s*(?:next\s+)?(?:page\s+|browser\s+|tab\s+)?(?:reload|refresh)\b", re.I)

# "Nothing needed" whose next step waits on the user's yes, with no button to
# give it ("Nothing needed. Version B goes in once you confirm").
_WAITS_ON_USER = re.compile(
    # "once/when/after you ..." is a wait; "if you want it shorter, tell me"
    # is an open offer and passes.
    r"\b(?:once|when|after|as\s+soon\s+as)\s+you(?:'?ve)?\s+(?:have\s+)?(?:confirm|approve|okay|ok|o\.k\.|"
    r"agree|say|said|pick|picked|choose|chose|decide|decided|sign\s+off|give|gave|tell|told|let\s+me\s+know|"
    r"reply|answer|green-?light)\w*\b"
    r"|\b(?<!nothing\sis\s)(?<!nothing's\s)(?<!nothing\s)(?<!not\s)(?<!isn't\s)(?<!no\sone\sis\s)(?:on|with|after|pending|awaiting|waiting\s+(?:on|for))\s+your\s+(?:go-?ahead|ok(?:ay)?|confirmation|approval|"
    r"sign-?off|word|yes|pick|choice|decision|answer|reply|call|green\s+light)\b"
    r"|\b(?:the\s+one|whichever|whatever|the\s+version|the\s+option)\s+you\s+(?:pick|choose|approve)\b"
    # "The real app stays unchanged until a picker is chosen": a Done card
    # that dropped an open choice.
    r"|\buntil\s+(?:you\s+(?:pick|choose|decide|approve|confirm|say)\b|(?:\w+\s+){1,4}(?:is|are|gets?|has\s+been|have\s+been)\s+"
    r"(?:chosen|picked|approved|decided|confirmed|selected|agreed)\b)"
    r"|\b(?<!nothing\sis\s)(?<!nothing's\s)(?<!nothing\s)(?<!not\s)(?<!isn't\s)(?<!no\sone\sis\s)waiting\s+(?:on|for)\s+you\b|\bup\s+to\s+you\b|\bwhen(?:ever)?\s+you'?re\s+ready\b"
    # "Then your pick of how the page starts": the next step is the user's.
    r"|^\W*then\s+your\s+(?:pick|choice|decision|call)\b", re.I)


# A choice about how something looks or is presented must SHOW the options
# (a markdown outline is not a preview).  The words are matched only in what
# names the choice (title,
# headline, needs.ask, the replies' label and detail), whole words.
_VISUAL_CHOICE = re.compile(
    r"\b(?:designs?|layouts?|look(?:s|-and-feel)?|looks?\s+like|styles?|styling|visuals?|"
    r"mock-?ups?|wireframes?|previews?|ui|screens?|buttons?|icons?|colou?rs?|themes?|fonts?|"
    r"typography|banners?|badges?|callouts?|menus?|toolbars?|sidebars?|popups?|pop-ups?|"
    r"modals?|dialogs?|tooltips?|toasts?|chips?|pills?|placement|position(?:ed|ing)?|"
    r"animations?|slides?|slide\s*decks?|decks?|powerpoint|presentations?|one-pager|"
    r"dashboards?|charts?|diagrams?|logos?|illustrations?|branding|appearance)\b", re.I)


def _viewable(it) -> bool:
    """A link row the user can SEE: a picture, or a page the app can show."""
    if not isinstance(it, dict):
        return False
    if _s(it.get("kind")) == "image":
        return True
    url = _s(it.get("url") or it.get("href"))
    if re.match(r"^https?://", url, re.I):
        return True
    return url.startswith("/") and not url.startswith("//") and not re.search(r"\.(?:md|txt|py|json)$", url, re.I)


def visual_choice_problem(card: dict) -> Optional[str]:
    """Why a decision about looks/presentation fails to show its options, or None."""
    needs = card.get("needs") if isinstance(card.get("needs"), dict) else {}
    replies = [r for r in _items(needs.get("replies")) if isinstance(r, dict)]
    naming = [_s(card.get("title")), _s(card.get("headline")), _s(needs.get("ask"))]
    for r in replies:
        naming += [_s(r.get("label")), _s(r.get("detail"))]
    m = _VISUAL_CHOICE.search(_norm(" \n ".join(naming)))
    if not m:
        return None
    if any(_viewable(s) for r in replies for s in _items(r.get("screens"))):
        return None
    if any(_viewable(it) for it in _items(needs.get("look")) + _items(card.get("links"))):
        return None
    return (f"This decision is about how something looks or is presented (\"{m.group(0)}\"), "
            "but nothing SHOWS the choices: a file to read or a text comparison is not a "
            "preview. Build a scratch page, src/web/static/scratch/<topic>.html (served at /static/scratch/), with every option as "
            "a real, clickable mockup side by side, each tagged data-option=\"<key>\" (and the "
            "app as it is today tagged data-option=\"now\", when there is one). Put the page "
            "in needs.look and give each reply \"screens\": [{\"label\": \"...\", \"url\": "
            "\"/static/scratch/<topic>.html#<key>\", \"device\": \"web|phone|both\"}]. "
            "A screenshot per option (a \"kind\": \"image\" link) also works. In a project "
            "whose instructions show options in its real app (design trials in that "
            "app), follow them instead: each reply's \"screens\" are that app's live "
            "pages on this machine, with their pictures in \"image\".")


_SHIP_IT = re.compile(r"\b(?:ship|look(?:s)?\s+(?:right|good|ok|okay|fine)|approve|accept|go\s+live|publish|"
                      r"roll\s+out|release|keep\s+(?:it|this|them|both)|use\s+(?:it|this))\b", re.I)
_STEP_ASK = re.compile(r"\b(?:restart|reload|reboot|log\s*in|sign\s*in|merge)\b", re.I)


def approve_look_problem(card: dict) -> Optional[str]:
    """Why an approval of something the user must see ("ship the new
    dashboard?") shows nothing to look at, or None.  The thing goes in
    needs.look: a screenshot (kind "image") or a page this app can show."""
    needs = card.get("needs") if isinstance(card.get("needs"), dict) else {}
    ask = _norm(_s(needs.get("ask")))
    # Only an approval of the thing itself: a restart or a merge "so the
    # sidebar loads" asks about a step, not about how something looks.
    if not _SHIP_IT.search(ask) or _STEP_ASK.search(ask):
        return None
    naming = [_s(card.get("title")), _s(card.get("headline")), _s(needs.get("ask"))]
    naming += [_s(r.get("label")) for r in _items(needs.get("replies")) if isinstance(r, dict)]
    m = _VISUAL_CHOICE.search(_norm(" \n ".join(naming)))
    if not m:
        return None
    if any(_viewable(it) for it in _items(needs.get("look"))):
        return None
    return (f"This approval asks the user to OK something they must see (\"{m.group(0)}\"), "
            "but needs.look shows none of it, so the Approve section has nothing to open. Put "
            "ONE page to review first in needs.look: the live page when this app can show it, "
            "else a review page in src/web/static/scratch/ laying out every screen the way it is "
            "judged (Before | After side by side for each place, each labelled), plus at most two "
            "screenshots of it ({\"label\": \"...\", \"kind\": \"image\", \"url\": "
            "\"/static/scratch/<name>.png\"}). Links under Checked are what you "
            "looked at, not what the user approves.")


# A pile of screenshots is not a review.  Six or eight loose pictures on a
# card (Before / After for three places, each a separate image row) leave
# the user to pair them up and judge them one thumbnail at a time, with no
# page to try.  What the user reviews is ONE page that lays the pictures out
# the way they are judged (Before | After side by side, a row per place),
# opened from the card's main row: validate.url on a done card, needs.look[0]
# on an Approve or To do, a link on an answer.  Up to two loose pictures
# need no page; up to four may sit beside a page as "OR just look"; more is
# always one page.  needs.pairs (the side-by-side popup) is such a page.
MAX_LOOSE_PICTURES = 2
MAX_CARD_PICTURES = 4


def _review_page(url) -> bool:
    """A page the user opens to review, not a picture or a file to read."""
    url = _s(url)
    if not url or _PIC.search(url) or re.search(r"\.(?:md|txt|py|json)(?:[?#].*)?$", url, re.I):
        return False
    if url.startswith("/") and not url.startswith("//"):
        return True
    return bool(_LOOPBACK_PAGE.match(url))


def picture_pile_problem(card: dict) -> Optional[str]:
    """Why the card hands over a pile of screenshots instead of one page to
    review them on, or None."""
    if not isinstance(card, dict):
        return None
    needs = card.get("needs") if isinstance(card.get("needs"), dict) else {}
    if _items(needs.get("pairs")):
        return None
    n = len(_card_pictures(card))
    if n <= MAX_LOOSE_PICTURES:
        return None
    v = card.get("validate") if isinstance(card.get("validate"), dict) else {}
    pages = [v.get("url")] + [it.get("url") for it in _items(needs.get("look")) + _items(card.get("links"))
                              if isinstance(it, dict) and _s(it.get("kind")) != "image"]
    has_page = any(_review_page(u) for u in pages)
    if has_page and n <= MAX_CARD_PICTURES:
        return None
    why = (f"{n} screenshots, more than the {MAX_CARD_PICTURES} a card may show" if has_page
           else f"{n} loose screenshots and no page to open them on")
    return (f"This card hands the user {why}: a pile of pictures to pair up and judge one "
            "thumbnail at a time is not a review. Build ONE review page, "
            "src/web/static/scratch/<topic>-review.html, that lays them out the way they are "
            "judged (Before | After side by side, a row per place, each labelled, at the size "
            "they were taken), make it the page the user opens (validate.url on a done card, "
            "needs.look[0] on an Approve or To do, a link on an answer), and show at most "
            f"{MAX_LOOSE_PICTURES} screenshots of that page. To confirm a change looks the same, "
            "a decision with needs.pairs is that page.")


# Pictures of a change to something that already existed.  A fix to how a
# menu, a bar or a page looks, shown as screenshots named only after the
# place ("Phone menu, journey page"), leaves the user unable to tell whether
# a picture is the problem or the fix, and with only the new state there is
# nothing to compare it with.  So when the card says it changed or fixed how
# something looks and shows pictures, they come in pairs the user can read:
# at least one labelled as the old state and one as the new.  A side-by-side
# "needs.pairs" review covers it too.
# Only a fix to how something already looked: words that say the old state
# was wrong.  An addition ("now has a toolbar") has no "before" worth showing.
_CHANGED_LOOK = re.compile(
    r"\b(?:fix(?:ed|es)?|no\s+longer|instead\s+of|used\s+to|"
    r"now\s+(?:opens?\s+as|sits?|fits?|lines?\s+up|aligns?|stays?|lands?|wraps?|matches)|"
    r"redesign(?:ed)?|restyled?|reworked|re-?laid\s+out|tightened|realigned)\b", re.I)
# A change meant to leave the look as it was ("pixel-identical") is a
# refactor; a fix to a check or a rule is not a change to a look.
_LOOK_UNCHANGED = re.compile(
    r"\b(?:pixel[- ]identical|looks?\s+(?:the\s+same|unchanged|identical)|unchanged\s+on\s+screen|"
    r"nothing\s+(?:you\s+can\s+see|on\s+screen)\s+changes)\b", re.I)
_MOTION_FIX = re.compile(r"\b(?:flash(?:es|ing)?|flicker(?:s|ing)?|jitter|jumps?|jumping|fades?|"
                         r"shifts?|blinks?|stutter(?:s|ing)?)\b", re.I)
_PIC_BEFORE = re.compile(r"\b(?:before|was|were|old|previous(?:ly)?|original|until\s+now|as\s+it\s+was|"
                         r"the\s+problem|broken|bug|wrong|stuck|cramped)\b", re.I)
_PIC_AFTER = re.compile(r"\b(?:after|now|new|fixed|with\s+the\s+(?:fix|change))\b", re.I)


def _card_pictures(card: dict) -> list:
    """The screenshots a card shows as itself (links and needs.look), not a
    Decide choice's screens."""
    needs = card.get("needs") if isinstance(card.get("needs"), dict) else {}
    out = []
    for it in _items(card.get("links")) + _items(needs.get("look")):
        if isinstance(it, dict) and (_s(it.get("kind")) == "image" or _PIC.search(_s(it.get("url")))):
            out.append(it)
    return out


def before_after_problem(card: dict) -> Optional[str]:
    """Why pictures of a changed look do not show the old and the new state,
    each labelled, or None."""
    # Reports of finished or blocked work, and approvals of it; an answer to
    # a question or a text the user asked for is not a report of a fix.
    if not isinstance(card, dict) or _s(card.get("type")) not in ("done", "needs_you", "blocked"):
        return None
    needs = card.get("needs") if isinstance(card.get("needs"), dict) else {}
    if _items(needs.get("pairs")):
        return None
    pics = _card_pictures(card)
    if not pics:
        return None
    said = _norm(" \n ".join([_s(card.get("title")), _s(card.get("headline")), _s(needs.get("ask"))]))
    if not (_VISUAL_CHOICE.search(said) and _CHANGED_LOOK.search(said)):
        return None
    if _LOOK_UNCHANGED.search(_norm(" \n ".join([said, _s(card.get("answer"))]))):
        return None
    # A fix to motion (a flash, a jump) has no still picture of the old state.
    if _MOTION_FIX.search(said):
        return None
    labels = [_s(it.get("label")) for it in pics]
    has_before = any(_PIC_BEFORE.search(lb) for lb in labels)
    has_after = any(_PIC_AFTER.search(lb) for lb in labels)
    if has_before and has_after:
        return None
    shown = "; ".join(f"\"{_short_text(lb, 40)}\"" for lb in labels[:3] if lb) or "no labels"
    return ("This card says it changed how something looks, and its pictures (" + shown + ") do not "
            "show which is before and which is after, " + ("and only the new state is shown" if has_after
            else "so the user cannot tell the problem from the fix") + ". Show BOTH: a screenshot of "
            "how it was, labelled \"Before: ...\", and one of how it is now, labelled \"After: ...\", "
            "for each place you changed (take the before picture from the old code, e.g. a copy at "
            "the previous commit, if you no longer have one). More than one place changed: put the "
            "pairs on ONE review page (Before | After side by side, a row per place), make it the "
            "page to open, and show at most two screenshots of it. On a card that asks the user "
            "to confirm the two match or differ, use needs.pairs instead.")


# A pick between options is a Decide card.  Only the Decide popup draws a
# reply's "screens" (one choice at a time, Web / Phone / Both); an Approve
# draws its replies as plain buttons, so options with mockups sent as an
# Approve reach the user as bare letters with nothing to look at.  And a
# question opening on "which" is a pick, not a yes-or-no.
_WHICH_ASK = re.compile(r"^\W*(?:which|what\s+(?:one|look|design|layout|version|style|option))\b", re.I)


def approve_pick_problem(card: dict) -> Optional[str]:
    """Why an Approve card is really a pick between options, or shows its
    options only in reply screens it never draws, or None."""
    needs = card.get("needs") if isinstance(card.get("needs"), dict) else {}
    if _s(needs.get("kind")) != "approve":
        return None
    replies = [r for r in _items(needs.get("replies")) if isinstance(r, dict)]
    screened = [str(r.get("key") or r.get("label") or i) for i, r in enumerate(replies) if _items(r.get("screens"))]
    which = len(replies) >= 2 and _WHICH_ASK.match(_norm(_s(needs.get("ask"))))
    if which or (len(replies) >= 3 and len(screened) >= 2):
        return ("This Approve card is a choice between options, and that is a pick, not a yes-or-no. "
                "Make it needs.kind \"decision\", one reply per option with its \"screens\" (only the "
                "Decide popup draws them; an Approve shows bare buttons), tied to the user's request in "
                "\"asks\" (state \"needs_you\"). If the user's message read like a question, keep the "
                "decision and put your answer in \"answer\" on the same card: options on screen always "
                "get a reply per option.")
    if screened and not any(_viewable(it) for it in _items(needs.get("look"))):
        return (f"This Approve card's replies carry \"screens\" ({', '.join(screened[:6])}), but an "
                "Approve never draws them, so the user is asked to OK something with nothing to look "
                "at. Put what is approved in needs.look: its page first, plus at most two "
                "screenshots of it ({\"label\": \"...\", \"kind\": \"image\", \"url\": \"...\"}). If the user is picking "
                "between options, make it needs.kind \"decision\" instead.")
    return None


def _is_picture(it) -> bool:
    if not isinstance(it, dict):
        return False
    url = _s(it.get("url"))
    return (_s(it.get("kind")) == "image" or "/api/preview/asset/" in url
            or bool(re.search(r"\.(?:png|jpe?g|gif|webp|svg)(?:[?#].*)?$", url, re.I)))


def approval_page_picture_problem(card: dict) -> Optional[str]:
    """Why an approval answered on its own page also carries a picture, or
    None.  The page IS the button: the Approve band draws pictures as big
    rows next to it, so a screenshot of that same page (or any other picture
    of the process) stands between the reader and the one thing to tap.  A
    picture stays when the approval is about how something looks ("ship the
    new header?"): then it shows what changes, not the page."""
    needs = card.get("needs") if isinstance(card.get("needs"), dict) else {}
    if _s(needs.get("kind")) != "approve" or _items(needs.get("replies")):
        return None
    look = [it for it in _items(needs.get("look")) if isinstance(it, dict)]
    pics = [it for it in look if _is_picture(it)]
    if not pics or len(pics) == len(look):
        return None
    naming = " \n ".join([_s(card.get("title")), _s(card.get("headline")), _s(needs.get("ask"))])
    if _VISUAL_CHOICE.search(_norm(naming)):
        return None
    names = ", ".join(f"\"{_s(p.get('label')) or _s(p.get('url'))}\"" for p in pics[:3])
    return (f"This approval is answered on its own page, but needs.look also carries a picture "
            f"({names}). The page is the one thing to tap; a screenshot of it, or of the "
            "process, is drawn beside it as noise. Keep only the page in needs.look. A picture "
            "belongs only when the approval is about how something looks.")


# A screen that is another app's live page on this machine is framed signed in on this computer, but the phone cannot reach it and the
# preview proxy cannot sign in to it, so the phone shows the screen's picture
# ("image", drawn by _tplStage in src/web/static/js/last-card.js).  Without one, the
# phone would show that app's login.
# The host as the browser reads it: after any user-info, with or without a port.
_LOOPBACK_PAGE = re.compile(r"^(https?)://(?:[^/?#@]*@)?(?:localhost|127\.0\.0\.1|\[::1\])(?::(\d+))?(?:[/?#]|$)", re.I)
_PIC = re.compile(r"\.(?:png|jpe?g|webp|gif)(?:[?#].*)?$", re.I)
# An address the card can load (isImg in last-card.js): not a relative path,
# not a file:// with two slashes.
_LOADABLE = re.compile(r"^(?:/(?!/)|https?://|~/|file:///|[A-Za-z]:[\\/])", re.I)


def _own_port() -> str:
    import os
    return os.environ.get("VIBENODE_WEB_PORT") or "5050"


def _screen_image_ok(v) -> bool:
    """The same shapes the popup draws (_screenImage): a picture, or one per
    device {"web", "phone"}, each a picture or one per theme {"light", "dark"}."""
    pic = lambda x: isinstance(x, str) and bool(_PIC.search(x.strip())) and bool(_LOADABLE.match(x.strip()))  # noqa: E731
    themed = lambda x: pic(x) or (isinstance(x, dict) and any(pic(x.get(t)) for t in ("light", "dark")))  # noqa: E731
    return pic(v) or (isinstance(v, dict) and any(themed(v.get(d)) for d in ("web", "phone")))


def live_screen_picture_problem(card: dict) -> Optional[str]:
    """Why a reply's screen on another port of this machine has no picture, or None."""
    needs = card.get("needs") if isinstance(card.get("needs"), dict) else {}
    for r in _items(needs.get("replies")):
        for s in _items(r.get("screens") if isinstance(r, dict) else None):
            if not isinstance(s, dict):
                continue
            m = _LOOPBACK_PAGE.match(_s(s.get("url")))
            port = m and (m.group(2) or ("443" if m.group(1).lower() == "https" else "80"))
            if m and port != _own_port() and not _screen_image_ok(s.get("image")):
                return (f"The screen \"{_s(s.get('label')) or _s(s.get('url'))}\" is a live page of another "
                        f"app on this machine ({_s(s.get('url'))}). It shows on this computer, but the phone "
                        "cannot reach it: give every such screen its picture in \"image\": "
                        "{\"web\": {\"light\": \"<png>\", \"dark\": \"<png>\"}, \"phone\": {...}} "
                        "(a file path on this machine works).")
    return None


# A mockup page (the scratch folder) linked from a card that is not a Decide
# card must come with pictures of it: a bare link hides the options.  A
# decision shows them through its replies' "screens" in the Decide popup.
_MOCKUP_PAGE = re.compile(r"^(?:https?://[^/]+)?/static/scratch/[^?#]+\.html?(?:[?#].*)?$", re.I)


def mockup_picture_problem(card: dict) -> Optional[str]:
    """Why a card links a mockup page without showing it, or None."""
    needs = card.get("needs") if isinstance(card.get("needs"), dict) else {}
    entries = _items(card.get("links")) + _items(needs.get("look"))
    pages = [_s(it.get("url")) for it in entries
             if isinstance(it, dict) and _s(it.get("kind")) != "image" and _MOCKUP_PAGE.match(_s(it.get("url")))]
    if not pages:
        return None
    # Approve, To do and deliverable cards draw no screenshots: their own
    # button is the main thing, a picture is noise.
    if _s(needs.get("kind")) in ("approve", "action") or isinstance(card.get("deliverable"), dict):
        return None
    if any(isinstance(it, dict) and _s(it.get("kind")) == "image" for it in entries):
        return None
    replies = [r for r in _items(needs.get("replies")) if isinstance(r, dict)]
    if _s(needs.get("kind")) == "decision" and any(_items(r.get("screens")) for r in replies):
        return None
    if _s(needs.get("kind")) == "decision" and replies:
        # The choices are shown once, inside Decide: never as a Screens band
        # as well.
        return (f"This Decide card links a mockup page ({pages[0]}) but its choices show nothing "
                "of it. Give each reply its own picture in \"screens\": [{\"label\": \"...\", "
                "\"url\": \"/static/scratch/<topic>.html#<key>\"}], its own section of the page, shown like Now. "
                "They are shown inside the Decide popup, one per choice. Do not add them as "
                "\"links\" too: the card would show the same choices twice.")
    return (f"This card links a mockup page ({pages[0]}) but shows nothing of it: the user sees "
            "a link, not the options. Add a screenshot of each option inside the card: a "
            "\"links\" entry {\"label\": \"what it shows\", \"kind\": \"image\", \"url\": \"...\"} per "
            "option (a PNG saved in src/web/static/scratch/ is served at /static/scratch/<name>.png).")


# A Decide card shows its choices once.  Pictures of the choices go in each
# reply's "screens" (drawn inside the Decide popup); as "links" they would
# become a Screens band beside the Decide button with the same choices.
_OPTION_LABEL = re.compile(r"^\s*(?:option\s+|choice\s+|variant\s+)?([A-Za-z0-9]{1,3})\s*(?:[:.)\u00b7\-\u2013\u2014]|$)", re.I)


def option_image_keys(card: dict) -> list:
    """Reply keys that also have a picture in "links" / needs.look."""
    needs = card.get("needs") if isinstance(card.get("needs"), dict) else {}
    replies = [r for r in _items(needs.get("replies")) if isinstance(r, dict)]
    if _s(needs.get("kind")) != "decision" or not replies:
        return []
    keys = {_s(r.get("key")).upper(): _s(r.get("label")).lower() for r in replies if _s(r.get("key"))}
    hits = []
    for it in _items(card.get("links")) + _items(needs.get("look")):
        if not isinstance(it, dict) or _s(it.get("kind")) != "image":
            continue
        label = _s(it.get("label"))
        m = _OPTION_LABEL.match(label)
        k = m.group(1).upper() if m else ""
        if k in keys or any(l and l in label.lower() for l in keys.values()):
            hits.append(k or label)
    return hits


def duplicate_choice_problem(card: dict) -> Optional[str]:
    hits = option_image_keys(card)
    if not hits:
        return None
    return ("This Decide card shows its choices twice: as pictures in \"links\" (" + ", ".join(hits[:4]) +
            ") and again behind Decide. Move each picture into its reply's \"screens\" "
            "[{\"label\": \"...\", \"url\": \"/static/scratch/<page>.html#<key>\"}] and take it out of "
            "\"links\": the Decide popup shows it with that choice.")


# Every choice is shown the way "Now" is.  The Decide popup's Now tab is the
# page's live data-option="now" section, in the user's theme and at real
# size; a reply whose screen is a PNG comes out zoomed (shot at 2x) and in
# the theme it was shot
# in, so the choices cannot be compared with Now or with each other.
# When the card has a mockup page, every reply's screen is its own section of
# that same page.  Live pages of another app on this machine are live too
# and pass.
def screens_like_now_problem(card: dict) -> Optional[str]:
    needs = card.get("needs") if isinstance(card.get("needs"), dict) else {}
    replies = [r for r in _items(needs.get("replies")) if isinstance(r, dict)]
    if _s(needs.get("kind")) != "decision" or not replies:
        return None
    base = lambda u: re.sub(r"[?#].*$", "", u or "")  # noqa: E731
    pages = [_s(it.get("url")) for it in _items(needs.get("look")) + _items(card.get("links"))
             if isinstance(it, dict) and _s(it.get("kind")) != "image" and _MOCKUP_PAGE.match(_s(it.get("url")))]
    pages += [_s(s.get("url")) for r in replies for s in _items(r.get("screens"))
              if isinstance(s, dict) and _MOCKUP_PAGE.match(_s(s.get("url")))]
    if not pages:
        return None
    page = base(pages[0])
    bad = []
    for r in replies:
        key = _s(r.get("key")) or _s(r.get("label"))[:20]
        scr = [s for s in _items(r.get("screens")) if isinstance(s, dict)]
        if not scr:
            bad.append(f"{key} has no screen")
        for s in scr:
            u = _s(s.get("url"))
            if _PIC.search(u):
                bad.append(f"{key} is a screenshot")
            elif _MOCKUP_PAGE.match(u) and base(u) != page:
                bad.append(f"{key} is on another page")
    try:
        name = page.rsplit("/", 1)[-1]
        f = (_SCRATCH_DIR / name).resolve()
        if f.parent == _SCRATCH_DIR.resolve() and f.is_file() and \
                not re.search(r"""data-option\s*=\s*["']now["']""", f.read_text(encoding="utf-8", errors="replace"), re.I):
            bad.append("the page has no Now section (data-option=\"now\")")
    except OSError:
        pass
    if not bad:
        return None
    return ("Show every choice the way the Now tab shows today's app: as its own section of the same "
            f"live page ({page}), never a screenshot. Found: " + "; ".join(dict.fromkeys(bad)) + ". Give "
            "each reply \"screens\": [{\"label\": \"...\", \"url\": \"" + page + "#<section id>\", \"device\": "
            "\"web\"}] and tag the page's sections data-option=\"now\", \"A\", \"B\"… A screenshot comes out "
            "at another size, zoom and theme than Now, so the choices can't be compared.")


# Variants on screen always come with a way to pick one, even on a card that
# answers a question.  A mockup page is the scratch page with two or more
# data-option variants besides "now"; the page lives in this repo's
# src/web/static/scratch, so it is read from disk.
_SCRATCH_DIR = Path(__file__).resolve().parents[2] / "src" / "web" / "static" / "scratch"
_JS_BUILT_OPTION = re.compile(r"""data-option=(?:\\?["'])?\s*['"]?\s*\+|data-option=["']\$\{""")
_JS_OPTION_KEY = re.compile(r"""\bkey\s*:\s*["']([A-Za-z0-9]{1,3})["']""")
_OPTION_ATTR = re.compile(r"""data-option\s*=\s*["']([^"']+)["']""", re.I)
_JS_OPTION_ROW = re.compile(r"""\[\s*["']([A-Z][0-9]?)["']\s*,\s*["']""")


# A choice is never captioned inside its own mockup.  The popup's top bar
# already names the choice (key, label, detail) and the Now tab is
# labelled Now, so a caption, tag, badge or "Option A" heading inside a
# data-option section is noise drawn on the design being judged.  Read from

# the page on disk: each section runs from its data-option tag to the next.
# Flagged inside a section: an "Option A" / "A ·" style heading; a caption
# element (a class like tag/label/caption/badge, or <label>/<figcaption>/
# <legend>) whose text repeats words of that choice's label, what or
# detail; and in Now, a caption saying now/today/current.
_CAPTION_CLASS = re.compile(
    r"(?:^|[\s_-])(?:tag|tags|label|caption|badge|chip|pill|eyebrow|legend|ribbon|kicker|stamp|"
    r"vlabel|vtag|opt|optname|variant|option|choice|name)(?:$|[\s_-])", re.I)
_CAPTION_TAG = re.compile(r"^(?:label|figcaption|legend)$", re.I)
_KEY_CAPTION = re.compile(
    r"^\s*(?:(?:option|variant|choice|version|design|layout|mockup)\s+[A-Z0-9]{1,2}\b"
    r"|[A-Z][0-9]?\s*[:·.)—–|-]\s+\S)", re.I)
_NOW_CAPTION = re.compile(r"^\s*(?:now|today|current(?:ly)?|before|as (?:it is|shown|today))\b", re.I)
_CAPTION_STOP = {"the", "and", "with", "for", "from", "into", "onto", "that", "this", "line", "one",
                 "its", "your", "you", "all", "but", "not", "are", "has", "out", "off", "own", "per"}
_OPEN_TAG = re.compile(r"<([a-zA-Z][a-zA-Z0-9]*)\b([^>]*)>([^<]*)")


def _section_html(text: str, key: str) -> str:
    starts = [(m.start(), m.group(1).strip()) for m in _OPTION_ATTR.finditer(text)]
    for i, (pos, k) in enumerate(starts):
        if k == key:
            begin = text.rfind("<", 0, pos)
            end = starts[i + 1][0] if i + 1 < len(starts) else len(text)
            end = text.rfind("<", 0, end) if i + 1 < len(starts) else end
            return text[begin if begin >= 0 else pos:end]
    return ""


def _content_words(s: str) -> set:
    return {w for w in re.findall(r"[a-z0-9]+", s.lower()) if len(w) >= 3 and w not in _CAPTION_STOP}


def _inner_text(section: str, m) -> str:
    """The text inside the element m opens (up to its first closing tag)."""
    close = section.find("</" + m.group(1), m.end(2))
    inner = section[m.end(2) + 1: close if close > 0 else m.end(2) + 400]
    return " ".join(re.sub(r"<[^>]*>", " ", inner).split())


def _captions_in(section: str, key: str, words: set, now: bool) -> list:
    found = []
    seen_text = 0
    lone = {"now"} if now else {key.lower()}
    for i, m in enumerate(_OPEN_TAG.finditer(section)):
        tag, attrs, txt = m.group(1), m.group(2), " ".join(m.group(3).split())
        if i == 0:
            txt = ""  # the section's own tag: its class names the section, not a caption
        if tag.lower() in ("script", "style", "button"):
            continue
        if txt:
            seen_text += 1
            # The choice's key on its own ("A", "NOW"), or "Option A" / "A · …".
            if txt.lower() in lone or (_KEY_CAPTION.match(txt) and len(txt) <= 80):
                found.append(txt)
                continue
        cls = re.search(r"""\bclass\s*=\s*["']([^"']*)["']""", attrs, re.I)
        # A heading is a caption only when it opens the section (a heading
        # deep inside the mockup is the design's own content).
        lead_heading = bool(re.match(r"^h[1-6]$", tag, re.I)) and seen_text <= 1
        captiony = (bool(_CAPTION_TAG.match(tag)) or lead_heading
                    or bool(cls and _CAPTION_CLASS.search(cls.group(1))))
        if not captiony or i == 0:
            continue
        inner = _inner_text(section, m)
        head = " ".join(inner.split()[:8])
        if not head:
            continue
        if now and _NOW_CAPTION.match(head):
            found.append(head)
        elif not now and (_content_words(head) & words
                          or re.match(re.escape(key) + r"\b\s+\S", head)):
            found.append(head)
    return found


def variant_caption_problem(card: dict) -> Optional[str]:
    """Why a decision's mockup page captions its own variants, or None."""
    needs = card.get("needs") if isinstance(card.get("needs"), dict) else {}
    replies = [r for r in _items(needs.get("replies")) if isinstance(r, dict)]
    if _s(needs.get("kind")) != "decision" or not replies:
        return None
    pages = {}
    for r in replies:
        for s in _items(r.get("screens")):
            u = _s(s.get("url")) if isinstance(s, dict) else ""
            if _MOCKUP_PAGE.match(u):
                frag = u.split("#", 1)[1] if "#" in u else ""
                pages.setdefault(re.sub(r"[?#].*$", "", u), []).append((r, frag))
    bad = []
    for page, items in pages.items():
        try:
            f = (_SCRATCH_DIR / page.rsplit("/", 1)[-1]).resolve()
            if f.parent != _SCRATCH_DIR.resolve() or not f.is_file():
                continue
            text = f.read_text(encoding="utf-8", errors="replace")[:600_000]
        except OSError:
            continue
        keys = {k.strip() for k in _OPTION_ATTR.findall(text)}
        now = next((k for k in keys if k.lower() == "now"), None)
        if now:
            for txt in _captions_in(_section_html(text, now), now, set(), True):
                bad.append(f"Now: \"{txt[:40]}\"")
        for r, frag in items:
            key = _s(r.get("key"))
            sec_key = key if key in keys else (frag if frag in keys else "")
            if not sec_key:
                m = re.search(r"""id\s*=\s*["']""" + re.escape(frag) + r"""["'][^>]*data-option\s*=\s*["']([^"']+)""",
                              text) if frag else None
                sec_key = m.group(1) if m else ""
            if not sec_key:
                continue
            words = _content_words(" ".join(_s(r.get(k)) for k in ("label", "what", "detail")))
            for txt in _captions_in(_section_html(text, sec_key), sec_key, words, False):
                bad.append(f"{key or sec_key}: \"{txt[:40]}\"")
    if not bad:
        return None
    return ("Take the captions off the mockups: the Decide popup's top bar already names each choice, so a "
            "tag, label, badge or \"Option A\" heading inside a variant (or a \"Today\" tag on Now) is noise "
            "drawn on the design the user is judging. Found: " + "; ".join(list(dict.fromkeys(bad))[:6]) + ". "
            "Each data-option section shows only the design itself; its name and description go in the "
            "reply's \"label\", \"detail\" and \"what\".")


def _variant_keys(url: str) -> list:
    m = _MOCKUP_PAGE.match(url or "")
    if not m:
        return []
    name = re.sub(r"[?#].*$", "", url).rsplit("/", 1)[-1]
    try:
        f = (_SCRATCH_DIR / name).resolve()
        if f.parent != _SCRATCH_DIR.resolve():
            return []
        text = f.read_text(encoding="utf-8", errors="replace")[:400_000]
    except OSError:
        return []
    keys = []
    found = _OPTION_ATTR.findall(text)
    # Pages that build their variants in script ('data-option="' + o.key + '"')
    # name them in the option list instead: key: 'A'.
    # Only when the page really builds its option tags in script: a demo
    # page's data (reply keys of a sample card) is not a set of variants.
    if _JS_BUILT_OPTION.search(text):
        found += _JS_OPTION_KEY.findall(text)
        # ...or as rows of a list: ['A', 'A · One top bar', '...'].
        found += _JS_OPTION_ROW.findall(text)
    for k in found:
        k = k.strip()
        if k and k.lower() != "now" and k not in keys and "'" not in k and "+" not in k:
            keys.append(k)
    return keys


def shows_variants(card: dict) -> list:
    """The variant keys of a mockup page the card links, or []."""
    needs = card.get("needs") if isinstance(card.get("needs"), dict) else {}
    for it in _items(card.get("links")) + _items(needs.get("look")):
        if isinstance(it, dict) and _s(it.get("kind")) != "image":
            keys = _variant_keys(_s(it.get("url")))
            if len(keys) >= 2:
                return keys
    return []


def variants_pick_problem(card: dict) -> Optional[str]:
    """Why a card shows variants without a way to pick one, or None."""
    if card.get("type") not in ("explain", "needs_you"):
        return None
    needs = card.get("needs") if isinstance(card.get("needs"), dict) else {}
    if _s(needs.get("kind")) == "decision":
        return None
    keys = shows_variants(card)
    if not keys:
        return None
    return (f"This card shows variants ({', '.join(keys[:6])}) but gives no way to pick one. "
            "Make it type \"needs_you\" with needs.kind \"decision\": the page in needs.look, one "
            "reply per variant, each with \"screens\": [{\"label\": \"...\", \"url\": \"<page>#<key>\", "
            "\"device\": \"both\"}], tied to the user's request in \"asks\" (state \"needs_you\"). If the user "
            "also asked a question, answer it in \"answer\" on the same card.")


# A validate.url the card can open: a page on this app, or a web address.
_VALIDATE_URL = re.compile(r"^(?:/(?!/)|https?://\S)", re.I)


def is_app_home(url) -> bool:
    """True when ``url`` is VibeNode's own front page and nothing more: "/",
    or this app's port on this machine or the tailnet, with no path, query or
    fragment: a Validate row there opens the app the user is already in.  A
    deep link ("/?session=…", "#kanban") points at something and passes."""
    u = _s(url).strip()
    if u in ("/", ""):
        return u == "/"
    m = re.match(r"^https?://(?:[^@/?#]*@)?(\[[^\]]*\]|[^:/?#]+)(?::(\d+))?(/?)$", u, re.I)
    if not m:
        return False
    host, port = m.group(1).lower(), m.group(2)
    local = host in ("localhost", "127.0.0.1", "[::1]", "0.0.0.0") or host.endswith(".ts.net")
    return local and (port == _own_port() or (port is None and host.endswith(".ts.net")))


# A picture as the place to try the work.  Validate is "Try it yourself OR
# just look": the screenshots are the "just look" half, so a Validate row
# whose address is itself a screenshot leaves the user nothing to try, only the
# same picture twice.  Rendered previews and saved card pictures are pictures
# too, whatever their extension.
_VALIDATE_PICTURE = re.compile(
    r"(?:\.(?:png|jpe?g|webp|gif|svg|avif|bmp)(?:[?#].*)?$"
    r"|/api/preview/asset/|/api/card-picture\b)", re.I)


def validate_picture_problem(card: dict) -> Optional[str]:
    """Why the card's Validate row opens a picture instead of a place to try
    the work, or None."""
    if not isinstance(card, dict):
        return None
    v = card.get("validate") if isinstance(card.get("validate"), dict) else {}
    url = _s(v.get("url")).strip()
    if not url or not _VALIDATE_PICTURE.search(url):
        return None
    return (f"validate.url ({url}) is a screenshot: the user cannot try anything in a picture, so "
            "this Validate row checks nothing the user asked for. validate.url is where the user "
            "does what validate.how says: the live page or a deep link to the exact place, or a "
            "demo page in src/web/static/scratch/ that loads the real code (for a phone change, one "
            "that frames the live page at phone width). Screenshots go in \"links\" as "
            "\"kind\": \"image\"; they are drawn beside the row as \"OR just look\".")

# A file to download, not a page to look at: a Validate row pointing at a
# .zip frames the archive full screen and shows nothing.  Files go in
# deliverable.files, drawn as Download buttons.
_DOWNLOAD_FILE = re.compile(
    r"\.(?:zip|tar|tgz|gz|bz2|xz|7z|rar|pdf|pptx?|key|odp|docx?|odt|rtf|xlsx?|ods|csv|tsv"
    r"|epub|dmg|pkg|exe|msi|apk|deb|rpm|appimage|whl|iso|mp4|mov|m4v|webm|mkv|avi|mp3|m4a|wav|flac)"
    r"(?:[?#].*)?$", re.I)
# Where the browser can fetch a file from: this app's static files or API,
# or a web address.  A path on disk ("/home/…", "~/…") is not one.
_SERVED_URL = re.compile(r"^(?:/(?:static|api)/\S|https?://\S)", re.I)


def is_download(url) -> bool:
    return bool(_DOWNLOAD_FILE.search(_s(url).strip()))


# An email deliverable ends at its sign-off: anything after the signature
# (a table of changes, notes) would be copied into the email.  Notes about
# the letter go in "full_answer".
_SIGNOFF = re.compile(r"^\s*(best(\s+regards)?|kind\s+regards|warm\s+regards|regards|sincerely|"
                      r"yours\s+(truly|sincerely)|thanks|thank\s+you|many\s+thanks|cheers)\s*[,.!]?\s*$",
                      re.IGNORECASE | re.MULTILINE)
_NOT_LETTER = re.compile(r"^\s*(#{1,6}\s|\|.*\||(-{3,}|\*{3,}|_{3,})\s*$)", re.MULTILINE)


def email_tail_problems(card: dict) -> list[str]:
    """An email deliverable with notes, a table or a divider after its
    sign-off, or after a divider near its end."""
    d = card.get("deliverable") if isinstance(card, dict) else None
    if not isinstance(d, dict) or _s(d.get("kind")).strip().lower() != "email":
        return []
    body = _s(d.get("body"))
    sign = list(_SIGNOFF.finditer(body))
    start = sign[-1].end() if sign else -1
    if start < 0:
        return []
    if _NOT_LETTER.search(body, start):
        return ["The email in \"deliverable.body\" goes on after its sign-off (a divider, a heading or "
                "a table). Copy and Draft in Gmail send the whole body, so that would reach the "
                "recipient. End the body at the signature; put notes about the letter in \"full_answer\"."]
    return []


# A deliverable's body IS the text.  The box's Copy and Draft in Gmail copy
# the body, so a body that only says where the text is ("use the Copy button
# on the file above", "the full prompt is in the attached file") hands the
# user a note about the deliverable instead of the deliverable.  And "files"
# is read only inside "deliverable": a card-level "files" key is drawn
# nowhere, so the text it points at never reaches the screen.
_POINTER_BODY = re.compile(
    r"\b(use|tap|click|press|hit)\s+(the\s+)?(copy|download|save)\b"
    r"|\b(copy|download)\s+(button|row|link)\b"
    r"|\b(file|attachment|button|link|row)\s+(above|below|attached)\b"
    r"|\b(see|open|in)\s+the\s+(attached\s+)?(file|attachment|copy\s+row)\b"
    r"|\b(attached|saved)\s+(as\s+a\s+)?(file|copy(able)?\s+file)\b"
    r"|\bthe\s+full\s+(text|prompt|email|letter|notes?|report|draft|message)\s+(is|lives|sits)\s+in\b",
    re.IGNORECASE)
POINTER_BODY_MAX_WORDS = 80


def deliverable_pointer_problems(card: dict) -> list[str]:
    """A deliverable body that points at the text instead of being it, and
    files put outside "deliverable" where nothing draws them."""
    if not isinstance(card, dict):
        return []
    out = []
    if card.get("files") is not None:
        out.append("\"files\" sits at the top of the card, where nothing draws it: the user never sees "
                   "those files. Files go inside \"deliverable\": {\"deliverable\": {\"files\": [...]}}. "
                   "A text the user asked for (a prompt, an email, notes) goes straight into "
                   "\"deliverable.body\" instead.")
    d = card.get("deliverable") if isinstance(card.get("deliverable"), dict) else None
    body = _s(d.get("body")) if d else ""
    if body and len(body.split()) <= POINTER_BODY_MAX_WORDS and _POINTER_BODY.search(body):
        out.append("\"deliverable.body\" says where the text is instead of being the text: \""
                   + " ".join(body.split())[:120] + "\". The box's Copy button copies the body, so the "
                   "user copies that note, not what they asked for. Put the whole text in "
                   "\"deliverable.body\" (it has no length limit); say how it was made in \"full_answer\".")
    return out


def download_problems(card: dict) -> list[str]:
    """A file to download handed over as a page to try or a link to open,
    and a deliverable file the browser cannot fetch."""
    out = []
    needs = card.get("needs") if isinstance(card.get("needs"), dict) else {}
    v = card.get("validate") if isinstance(card.get("validate"), dict) else {}
    misplaced = []
    if is_download(v.get("url")):
        misplaced.append(("validate.url", _s(v.get("url"))))
    for where, items in (("links", card.get("links")), ("needs.look", needs.get("look"))):
        for x in _items(items):
            if isinstance(x, dict) and x.get("kind") != "image" and is_download(x.get("url")):
                misplaced.append((where, _s(x.get("url"))))
    if misplaced:
        where, url = misplaced[0]
        out.append(f"{where} is a file to download ({url}), not a page to try: \"Try it\" and \"Open\" "
                   "show nothing for it. Hand files over as type \"deliverable\" with \"deliverable\": "
                   "{\"kind\": \"download\", \"title\": \"…\", \"files\": [{\"label\": \"what it is\", "
                   "\"url\": \"/static/scratch/<name>\", \"size\": \"3.4 MB\"}]}: the card draws a "
                   "Download button per file. No validate row and no file links for them.")
    d = card.get("deliverable") if isinstance(card.get("deliverable"), dict) else {}
    files = d.get("files")
    if files is not None:
        if not isinstance(files, list) or not files:
            out.append("\"deliverable.files\" is a list of {\"label\", \"url\", \"size\"}.")
        else:
            for i, f in enumerate(files):
                f = f if isinstance(f, dict) else {}
                if not _s(f.get("label")):
                    out.append(f"deliverable.files[{i}] needs \"label\": what the file is, in plain words.")
                if not _SERVED_URL.match(_s(f.get("url")).strip()):
                    out.append(f"deliverable.files[{i}].url ({_s(f.get('url')) or 'empty'}) must be an "
                               "address the browser can download from: put the file in src/web/static/scratch/ "
                               "(not published) and give \"/static/scratch/<name>\", or a web address. "
                               "A path on disk downloads nothing, on the phone least of all.")
    return out


# A link is the work, never the session's proof.  A commit page on a private
# repository is a 404 to any window not signed in, and the user does not
# review what the session checked.  A link is something the user opens to
# try or see the work; what the session checked is said in plain words, and
# commit ids, refs and hashes go in Full.
_CODE_PAGE = re.compile(
    r"^https?://[^/\s]+/\S*?/(?:-/)?(?:commits?|compare|blob|tree|blame)(?:/|$)", re.I)
_PROOF_JARGON = re.compile(
    r"\b(?=[0-9a-f]*\d)(?=[0-9a-f]*[a-f])[0-9a-f]{7,40}\b"
    r"|\b(?:refs?|sha(?:-?\d+)?|diffs?|stash(?:ed)?|origin/[\w./-]+|(?-i:HEAD)~?\d*)\b"
    r"|\bgit\s+(?:notes?|log|show|diff|rev-parse|status|ls-files|fetch|push)\b"
    r"|\b(?:matches|vs\.?|against|same\s+as)\s+mine\b", re.I)


def is_code_page(url) -> bool:
    return bool(_CODE_PAGE.match(_s(url).strip()))


def proof_link_problems(card: dict) -> list[str]:
    """A commit or code page handed over as something to open, and a Checked
    line written in the session's own git words."""
    out = []
    needs = card.get("needs") if isinstance(card.get("needs"), dict) else {}
    v = card.get("validate") if isinstance(card.get("validate"), dict) else {}
    bad = []
    if is_code_page(v.get("url")):
        bad.append(("validate.url", _s(v.get("url"))))
    for where, items in (("links", card.get("links")), ("needs.look", needs.get("look"))):
        for x in _items(items):
            if isinstance(x, dict) and (x.get("kind") == "commit" or is_code_page(x.get("url"))):
                bad.append((where, _s(x.get("url")) or _s(x.get("label"))))
    if bad:
        where, url = bad[0]
        out.append(f"{where} sends the user to a commit or code page ({url}). A link or Try-it row "
                   "is something the user opens to try or see the work itself, never your proof: the user does "
                   "not review what you checked, and a private repository shows the user a 404. Remove "
                   "it. Say what you checked in plain words in \"checked\" and \"verified\"; commit "
                   "ids go in Full. If there is nothing of the work to open (a fix to records, "
                   "history or notes), a done card shows a \"compare\" block of what reads "
                   "differently now, before and after, in the user's words.")
    lines = [("checked", _s(card.get("checked")))]
    for i, x in enumerate(_items(card.get("verified"))):
        if isinstance(x, dict):
            lines += [(f"verified[{i}].check", _s(x.get("check"))),
                      (f"verified[{i}].result", _s(x.get("result")))]
    for where, text in lines:
        hits = [m.group(0) for m in _PROOF_JARGON.finditer(text)]
        if hits:
            out.append(f"{where} is written in your own git words (\"" + "\", \"".join(dict.fromkeys(hits[:4]))
                       + f"\": \"{_short_text(text)}\"). Checked is drawn for the user, who will not "
                       "look at your proof: say what it means for the user (\"The record now names both "
                       "pieces of work and all 18 files\"). Refs, hashes and commands go in Full.")
            break
    return out


def _short_text(t: str, n: int = 70) -> str:
    t = " ".join(t.split())
    return t if len(t) <= n else t[: n - 1] + "…"


# ── Layer 1: the format ───────────────────────────────────────────────────

# How many minutes a Working card promised: needs.eta_minutes, or an
# estimate in words the card can count down ("about 40 minutes", "1h 20m",
# "4:30 pm").  The browser twin is etaMinutes in src/web/static/js/last-card.js.
_ETA_UNIT = re.compile(r"\d+(?:\.\d+)?\s*(?:h|hr|hrs|hours?|m|min|mins|minutes?)(?![a-z])", re.I)
_ETA_CLOCK = re.compile(r"^(?:~|by|at|around|about)?\s*\d{1,2}(?::\d{2})?\s*[ap]\.?m\.?$|"
                        r"^(?:~|by|at|around|about)?\s*\d{1,2}:\d{2}$", re.I)


def eta_minutes_of(needs) -> bool:
    """True when the card's estimate can be counted down."""
    if not isinstance(needs, dict):
        return False
    v = needs.get("eta_minutes")
    try:
        if v is not None and not isinstance(v, bool) and float(v) > 0:
            return True
    except (TypeError, ValueError):
        pass
    t = _s(needs.get("eta")).lower()
    return bool(t and (_ETA_UNIT.search(t) or _ETA_CLOCK.match(t)
                       or "half an hour" in t or re.search(r"\ban hour\b", t)))


def format_problems(card: dict) -> list[str]:
    """What is wrong with the card's shape, by the rules of its type."""
    out = []
    t = card.get("type")
    if t not in TYPES:
        # Keep going: returning here hid every other fault until the type was
        # fixed, so a summary with no answer buttons used up a send-back on the
        # type alone and was let through untested.  The checks
        # below that depend on the type simply do not fire.
        out.append(f"\"type\" must be one of: {', '.join(TYPES)} (got {json.dumps(t)}).")
    if not _s(card.get("headline")):
        out.append("\"headline\" is empty: one sentence saying what happened.")
    # Pictures of a fixed look show the old state and the new, each labelled.
    ba = before_after_problem(card)
    if ba:
        out.append(ba)
    # A short title drawn big above the headline, so a card skims.
    title = _s(card.get("title"))
    if not title:
        out.append(f"\"title\" is empty: the thing itself, a short name, at most {MAX_TITLE_CHARS} "
                   "characters, drawn big at the top of the card (\"Header image\").")
    elif len(title) > MAX_TITLE_CHARS:
        out.append(f"\"title\" is {len(title)} characters; the limit is {MAX_TITLE_CHARS}. "
                   "Cut it to the name of the thing.")
    out += title_path_problems(card)
    # A bare "cards" in the title: the app has sidebar cards, Workflow cards,
    # copy cards and these end-of-turn summaries, so "cards" alone is
    # ambiguous.
    if title:
        bare = [m.group(0) for m in _CARD_WORD.finditer(title)
                if (title[:m.start()].split() or [""])[-1].lower().strip("-") not in _CARD_QUALIFIERS]
        if bare:
            out.append(f"\"title\" says \"{bare[0]}\" without saying which: VibeNode has sidebar cards, "
                       "Workflow cards and end-of-turn summaries. Name the one the user sees, or what the user "
                       "gets: \"Cards\" becomes \"Summary card titles\".")
    if not _s(card.get("context")):
        out.append("\"context\" is empty: what we are working on, for someone who read nothing else.")

    status = card.get("status") if isinstance(card.get("status"), dict) else {}
    remaining = [x for x in _items(status.get("remaining")) if _item_text(x)]
    broken = [x for x in _items(status.get("broken")) if _item_text(x)]
    failed_checks = [v for v in _items(card.get("verified")) if isinstance(v, dict) and v.get("ok") is False]

    needs = card.get("needs")
    if not isinstance(needs, dict) or needs.get("kind") not in NEEDS_KINDS:
        out.append("\"needs\" must be an object whose \"kind\" is one of: " + ", ".join(NEEDS_KINDS) + ".")
        return out
    kind = needs["kind"]

    if t in ("done", "deliverable", "explain"):
        if remaining or broken:
            out.append(f"A \"{t}\" card cannot list work left over or broken. Finish it, or use "
                       "\"needs_you\" / \"blocked\" if it truly needs the user or is out of your hands.")
        if kind != "none":
            out.append(f"A \"{t}\" card has needs.kind \"none\". If the user must act, use \"needs_you\".")
    if t == "done":
        if not _items(card.get("verified")):
            out.append("A \"done\" card needs \"verified\": how you checked it works, not just that tests ran.")
        # A done card asks the user to Review it (session-verb.js), so it
        # must carry something to review.
        v = card.get("validate") if isinstance(card.get("validate"), dict) else {}
        if not (_items(card.get("links")) or _items(needs.get("look"))
                or isinstance(card.get("compare"), dict) or _s(v.get("url"))):
            out.append("A \"done\" card asks the user to review the work, so give the user something to "
                       "review: \"links\" to the work itself (the page or preview to try, a "
                       "screenshot, a file the user asked for), or a \"compare\" block showing before "
                       "and after. Not a commit or code page: that is your proof, not the user's to review.")
        # A done card always has a Validate row: test results are the
        # session's check, not the user's.
        if not _s(v.get("how")):
            out.append("A \"done\" card needs \"validate\": {\"how\": \"what the user does to see it work "
                       "himself, one line\", \"url\": \"the page, preview or screen to try it on\"}. "
                       "Your test results are your check, not the user's: give the user a way to check it.")
        # A Validate row with no address looks like a button and does nothing.

        # It must always open something; when the real moment comes later, a
        # demo that shows it happening, and the real moment in "later".
        elif not _VALIDATE_URL.match(_s(v.get("url"))):
            out.append("validate.url must open something the user can try NOW: the page "
                       "(\"/static/....html\", a deep link, or \"http://localhost:...\"), never a screenshot, "
                       "not a file path or nothing. If the real check only happens later (the next "
                       "time X occurs), build a demo page that shows it happening, put it in "
                       "validate.url, and say when the real moment is in validate.later.")
        elif is_app_home(v.get("url")):
            out.append(f"validate.url ({_s(v.get('url'))}) is VibeNode's front page, the app the user is "
                       "already in: it opens nothing the user can check. Link the exact place the change "
                       "shows (a deep link, or a demo page that loads the real code), or, when the real "
                       "check only happens later, build a demo page that shows it happening and say "
                       "when the real moment is in validate.later.")
        elif validate_picture_problem(card):
            out.append(validate_picture_problem(card))
        if failed_checks:
            out.append("A \"done\" card cannot carry a failed check: "
                       + "; ".join(_s(v.get("check")) or "(unnamed)" for v in failed_checks)
                       + ". Fix it, whoever caused it, or use \"blocked\".")
    if t == "deliverable":
        d = card.get("deliverable")
        if not isinstance(d, dict) or not (_s(d.get("body")) or _items(d.get("files"))):
            out.append("A \"deliverable\" card needs \"deliverable.body\" (the text itself) or "
                       "\"deliverable.files\" (the files to download).")
        elif _s(d.get("format")) and _s(d.get("format")).lower() not in DELIVERABLE_FORMATS:
            out.append("\"deliverable.format\" is \"markdown\", \"html\" or \"text\".")
    if t == "explain" and not (_s(card.get("answer")) or isinstance(card.get("compare"), dict)):
        out.append("An \"explain\" card needs \"answer\" (or a \"compare\" block).")
    if "full_answer" in card:
        if not _s(card.get("full_answer")):
            out.append("\"full_answer\" is the detailed answer in markdown; leave it out rather than "
                       "sending it empty.")
        elif not _s(card.get("answer")):
            out.append("\"full_answer\" is the answer's Full version, beside its Short one: it needs "
                       "\"answer\" (the short one) too.")
    if t == "working":
        if kind != "none":
            out.append("A \"working\" card has needs.kind \"none\". If the user must act, use \"needs_you\".")
        if not _s(needs.get("eta")) and needs.get("eta_minutes") in (None, ""):
            out.append("A \"working\" card needs needs.eta_minutes: how many minutes until the user "
                       "hears back (a number), so its countdown runs live.")
        elif not eta_minutes_of(needs):
            out.append("A \"working\" card needs needs.eta_minutes as a number of minutes (for "
                       "example 40), so its countdown runs live. Words like \"" +
                       _s(needs.get("eta"))[:40] + "\" cannot be counted down.")
        st = needs.get("stages")
        if st is not None and (not isinstance(st, list) or not 2 <= len(st) <= 8
                               or not all(isinstance(x, str) and 0 < len(x.split()) <= 3 for x in st)):
            out.append("needs.stages is the background job's stages in order: 2 to 8 short names "
                       "of at most 3 words each, as strings.")
    if t == "needs_you" and kind == "none":
        out.append("A \"needs_you\" card must say what is needed: needs.kind decision, approve or action.")
    if t == "blocked":
        if not (_items(card.get("tried")) or _items(card.get("did"))):
            out.append("A \"blocked\" card needs \"tried\": what you already tried.")
        if not _s(needs.get("why")):
            out.append("A \"blocked\" card needs needs.why: exactly what blocks you.")
    bt = card.get("back_to")
    if bt is not None and not (isinstance(bt, dict) and _s(bt.get("label"))):
        out.append("\"back_to\" is {\"label\": \"the earlier work, in a few words\", \"status\": "
                   "\"where it stands\", \"reply\": \"the message its Back to it button sends\", "
                   "\"detail\": \"optional: what it was waiting on, shown behind Details\"}.")
    out += finding_problems(card)
    out += offer_info_problems(card)
    out += plain_choice_problems(card)
    out += choice_explain_problems(card)
    mock = mockup_picture_problem(card)
    if mock:
        out.append(mock)
    pile = picture_pile_problem(card)
    if pile:
        out.append(pile)
    dup = duplicate_choice_problem(card)
    if dup:
        out.append(dup)
    like_now = screens_like_now_problem(card)
    if like_now:
        out.append(like_now)
    caption = variant_caption_problem(card)
    if caption:
        out.append(caption)
    pick = variants_pick_problem(card)
    if pick:
        out.append(pick)
    live = live_screen_picture_problem(card)
    if live:
        out.append(live)

    # The needs block itself.
    if kind == "none":
        if not _s(needs.get("why")) or not _s(needs.get("next")):
            out.append("needs.kind \"none\" still needs \"why\" (why nothing is needed) and "
                       "\"next\" (what happens next).")
        # A card that says nothing is needed and then gives the user a job
        # ("Nothing needed. Reload VibeNode and ...").
        chore = _CHORE_IN_NOTHING.search(_norm(_s(needs.get("next")) + " " + _s(needs.get("why"))))
        if chore:
            out.append(f"needs.kind \"none\" says nothing is needed, but it tells the user to do something "
                       f"(\"{chore.group(0).strip()}\"). Do it yourself, or if only the user can, use "
                       "\"needs_you\" with needs.kind \"action\", the steps and an \"I did it\" button.")
        waits = _WAITS_ON_USER.search(_norm(_s(needs.get("next")) + " " + _s(needs.get("why"))))
        if waits:
            out.append(f"needs.kind \"none\" says nothing is needed, but what happens next waits on the user "
                       f"(\"{waits.group(0).strip()}\") and the card gives the user no button to answer. If the user "
                       "already said what the user wants, do it now. If it truly waits on the user's yes, end on "
                       "\"needs_you\" with needs.kind \"approve\" and a reply button for each answer; "
                       "on a question, the answer goes in \"answer\" on that same card.")
    else:
        if not _s(needs.get("ask")):
            out.append("needs.ask is empty: the one thing the user must decide or do.")
        if not _s(needs.get("why")):
            out.append("needs.why is empty: why this needs the user and not you.")
        replies = [r for r in _items(needs.get("replies")) if isinstance(r, dict)]
        if kind == "decision" and split_field(needs):
            # A split-view template (a form, pairs): its answers live inside its items.
            out += split_problems(card)
        elif kind == "decision":
            if len(replies) < 2:
                out.append("A decision needs at least two reply buttons in needs.replies.")
            bare = [r for r in replies if len(_s(r.get("label")).split()) < 3]
            if bare:
                out.append("Every choice must be spelled out in its \"label\" (three words or more), "
                           "not a bare letter or name.")
            # Choices that carry their own screens are something to look at
            # (when today's look is one of the choices, a page in needs.look
            # would add a "Now" tab showing it twice).
            if (not _items(needs.get("look")) and not isinstance(card.get("compare"), dict)
                    and not any(_items(r.get("screens")) for r in replies)):
                out.append("A decision needs something to look at: a link in needs.look, a \"compare\" "
                           "block, or \"screens\" on its replies.")
            seen = visual_choice_problem(card)
            if seen:
                out.append(seen)
        elif kind == "approve":
            # An approval answered on its own page (a project's release
            # approval page) has no reply buttons: the user
            # answers on the page, opened full screen from needs.look, never
            # through the session.
            if not replies and not _items(needs.get("look")):
                out.append("An approval needs reply buttons in needs.replies, or, when it is "
                           "answered on its own page, that page in needs.look.")
            out += [p for p in (approve_look_problem(card), approve_pick_problem(card),
                                approval_page_picture_problem(card)) if p]
        elif kind == "action":
            if not _items(needs.get("steps")):
                out.append("An action needs needs.steps: exactly what the user does.")
            # An action only the user's words can answer has no button: the
            # reply box answers it (words_card_problems refuses one).
            if not replies and not asks_for_own_words(card):
                out.append("An action needs a reply button the user taps when it is done.")
    return out


# ── A home for findings, and a yes-or-no the user can picture ────────────
# Something a session notices outside the user's ask and may not fix alone
# needs a home: a done card may not mention it (UNFINISHED), and turning it
# into a Decide/Approve produces a question in code names and raw command
# syntax, with no recommendation, about a thing the user never raised.
# Three checks:
#   1. "finding": a sanctioned place for something noticed outside the ask
#      (what, risk, recommend), drawn as a quiet "Also found" band with one
#      button.  It is neither unfinished work nor a decision.
#   2. plain_choice_problems: a Decide/Approve question and its buttons carry
#      no command syntax, flags or code names, and one reply is marked
#      "recommended" (a pick between mockups is a matter of taste: exempt).
#   3. handoff_evidence.reviewer_problems: an Approve, like a Decide, must be
#      tied to something the user asked for, in their words.
FINDING_KEYS = ("what", "risk", "recommend")
# The finding's own explanation behind its info button, read like the
# summary's switch: Short, Plain words, Full.
FINDING_DEPTH_KEYS = ("answer", "explained", "full_answer")
FINDING_EXPLAINED_MIN_WORDS = 30


def finding_problems(card: dict) -> list[str]:
    f = card.get("finding")
    if f is None:
        return []
    shape = ("\"finding\" is {\"what\": \"what you noticed, one line\", \"risk\": \"what it risks "
             "for the user\", \"recommend\": \"what you recommend\", \"answer\": \"Short: two or "
             "three sentences on what it is and why it matters\", \"explained\": \"Plain words: "
             "the same for someone lost, markdown, no jargon, at least "
             f"{FINDING_EXPLAINED_MIN_WORDS} words\", \"full_answer\": \"Full: everything behind "
             "it in markdown, no limit\", \"reply\": \"optional: the message its Deal with it "
             "button sends\"}: something outside the user's ask that you may not act on alone. It is "
             "not unfinished work and not a decision. Its info button opens Short / Plain words / Full.")
    if not isinstance(f, dict):
        return [shape]
    missing = [k for k in FINDING_KEYS + FINDING_DEPTH_KEYS if not _s(f.get(k))]
    if missing:
        return [shape + " Missing: " + ", ".join(missing) + "."]
    out = []
    ex, short = _s(f.get("explained")), _s(f.get("answer"))
    if _words(ex) < FINDING_EXPLAINED_MIN_WORDS or _words(ex) <= _words(short):
        out.append(f"finding.explained (Plain words) is {_words(ex)} words: write at least "
                   f"{FINDING_EXPLAINED_MIN_WORDS}, and more than finding.answer (Short), for "
                   "someone who has read nothing else.")
    if _words(short) > MAX_ANSWER_WORDS:
        out.append(f"finding.answer (Short) is {_words(short)} words (limit {MAX_ANSWER_WORDS}).")
    return out


# The "Do what it suggested" button gets an info button too, read the same
# way.  A card with an "offer" carries "offer_info".

OFFER_INFO_EXPLAINED_MIN_WORDS = 30


def offer_info_problems(card: dict) -> list[str]:
    info = card.get("offer_info")
    if not _s(card.get("offer")):
        return ["\"offer_info\" explains the card's \"offer\"; there is no offer."] if info is not None else []
    shape = ("With an \"offer\", add \"offer_info\": {\"answer\": \"Short: what you would do and "
             "why, two or three sentences\", \"explained\": \"Plain words for someone lost: what "
             "it is, what it changes for the user, what it costs or risks, markdown, at least "
             f"{OFFER_INFO_EXPLAINED_MIN_WORDS} words\", \"full_answer\": \"Full: the plan in "
             "detail, markdown\"}. The info button on Do what it suggested opens it as Short / "
             "Plain words / Full.")
    if not isinstance(info, dict):
        return [shape]
    missing = [k for k in FINDING_DEPTH_KEYS if not _s(info.get(k))]
    if missing:
        return [shape + " Missing: " + ", ".join(missing) + "."]
    out = []
    ex, short = _s(info.get("explained")), _s(info.get("answer"))
    if _words(ex) < OFFER_INFO_EXPLAINED_MIN_WORDS or _words(ex) <= _words(short):
        out.append(f"offer_info.explained (Plain words) is {_words(ex)} words: write at least "
                   f"{OFFER_INFO_EXPLAINED_MIN_WORDS}, and more than offer_info.answer (Short).")
    if _words(short) > MAX_ANSWER_WORDS:
        out.append(f"offer_info.answer (Short) is {_words(short)} words (limit {MAX_ANSWER_WORDS}).")
    return out


# Command syntax and code names where the user must answer a question.
_CODE_TOKEN = re.compile(
    r"`[^`\n]+`"                                              # `anything`
    r"|(?<![\w-])--?[A-Za-z][\w-]*(?=[\s,.;:)]|$)"              # -rf, --force, -Recurse
    r"|(?<=\s)/[A-Za-z]{1,2}\b"                                 # /s /q /F
    # a command with its first argument: a flag or switch ("rm -rf", "del /s",
    # "Remove-Item -Recurse"), or a known subcommand ("git push", "pip
    # install").  "git" alone is English in "git calls" (seen in the replay).
    r"|\b(?:rm|del|rmdir|rd|taskkill|killall|curl|wget|chmod|chown|Remove-Item|Stop-Process|"
    r"Get-ChildItem|Set-Item)\s+(?:-{1,2}|/)[\w./-]+"
    r"|\bgit\s+(?:push|pull|reset|clean|checkout|rebase|branch|commit|stash|merge|fetch|rm|add)\b"
    r"|\b(?:pip\s+install|npm\s+(?:run|install|publish)|npx\s+\w+|sudo\s+\w+)\b"
    r"|\b[a-z][a-z0-9]*_[a-z0-9_]+\b"                            # snake_case names
    r"|\b\w+(?:\.\w+)*\([^)\n]*\)"                               # foo.bar(), run()
    r"|\$\{?[A-Za-z_]\w*",                                       # $VAR
)
# Where a Decide/Approve question is read: the question, its reason, what
# follows, and the buttons.  A reply's "detail" is the fine print and may
# name the exact thing; the title, headline and zoom lines often repeat the
# user's own task name (Code Pipeline's cards quote it), so they are left to
# machinery_problems.
PLAIN_CHOICE_FIELDS = ("needs.ask", "needs.why", "needs.next")
# A quoted span is a name the card repeats (the user's task title, a button
# label), not the session's own words: left out of the scan.
_QUOTED = re.compile(r"“[^”\n]*”|\"[^\"\n]*\"")


def plain_choice_problems(card: dict) -> list[str]:
    needs = card.get("needs") if isinstance(card.get("needs"), dict) else {}
    kind = needs.get("kind")
    if kind not in ("decision", "approve"):
        return []
    fields = {"needs.ask": needs.get("ask"), "needs.why": needs.get("why"), "needs.next": needs.get("next")}
    replies = [r for r in _items(needs.get("replies")) if isinstance(r, dict)]
    for i, r in enumerate(replies):
        fields[f"needs.replies[{i}].label"] = r.get("label")
    hits = []
    for where, text in fields.items():
        if not isinstance(text, str):
            continue
        for m in _CODE_TOKEN.finditer(_QUOTED.sub(" ", _norm(text))):
            hits.append(f"\"{m.group(0).strip()}\" ({where})")
    out = []
    if hits:
        out.append("The question and its buttons read like code: " + ", ".join(dict.fromkeys(hits)) + ". "
                   "A yes-or-no the user can picture names the outcome, not the syntax: not \"refuse del "
                   "/s /q and Remove-Item -Recurse -Force like rm -rf\" but \"stop a session from wiping "
                   "a folder on Windows the way it is already stopped on Mac\". The exact command or "
                   "name may go in a reply's \"detail\", in context, or in Full.")
    # A pick between mockups is a matter of taste; every other choice comes with the
    # session's recommendation, so a yes-or-no is never handed over bare.
    picks = shows_variants(card) or any(_items(r.get("screens")) for r in replies)
    if replies and not picks:
        rec = [r for r in replies if r.get("recommended") is True]
        if not rec:
            out.append("Mark the reply you recommend with \"recommended\": true (exactly one). The user "
                       "reads the question between many sessions; the card says which way you would go "
                       "and why (in needs.why), so the user can agree in one tap or disagree knowingly.")
        elif len(rec) > 1:
            out.append("More than one reply is marked \"recommended\"; mark exactly one.")
    return out


# ── Each Decide choice explained ─────────────────────────────────────────
# A label plus a commit hash and one shared reason does not explain a choice.
# Every reply of a decision carries three plain lines, drawn one choice at a
# time in the popup (_explainHTML, last-card.js):
#   what     what the choice is, in plain words (no hashes, paths or code names)
#   gain     what it gets the user: its real advantage, even when not recommended
#   verdict  recommended: why it beats the others;
#            otherwise: why the recommended one wins over this one.  The
#            popup's heading already names the recommended choice ("Why A ·
#            <label> is recommended instead"), so the verdict gives only the
#            reason and never opens on that name again.
# A pick between mockups is a matter of taste, so it is exempt, as for
# "recommended".

CHOICE_EXPLAIN_FIELDS = ("what", "gain", "verdict")
CHOICE_EXPLAIN_MIN_WORDS = 6
# A commit hash or similar id: hex with at least one digit and one letter.
_HEX_ID = re.compile(r"\b(?=[0-9a-f]*\d)(?=[0-9a-f]*[a-f])[0-9a-f]{7,40}\b")


def choice_explain_problems(card: dict) -> list[str]:
    needs = card.get("needs") if isinstance(card.get("needs"), dict) else {}
    if needs.get("kind") != "decision":
        return []
    replies = [r for r in _items(needs.get("replies")) if isinstance(r, dict)]
    if len(replies) < 2 or shows_variants(card) or any(_items(r.get("screens")) for r in replies):
        return []
    missing, short, code, verdicts = [], [], [], {}
    for i, r in enumerate(replies):
        name = str(r.get("key") or r.get("label") or i)
        for f in CHOICE_EXPLAIN_FIELDS:
            v = r.get(f)
            if not isinstance(v, str) or not v.strip():
                missing.append(f"{name}.{f}")
                continue
            if _words(v) < CHOICE_EXPLAIN_MIN_WORDS:
                short.append(f"{name}.{f}")
            text = _QUOTED.sub(" ", _norm(v))
            for m in list(_CODE_TOKEN.finditer(text)) + list(_HEX_ID.finditer(text)):
                code.append(f"\"{m.group(0).strip()}\" ({name}.{f})")
        vd = r.get("verdict")
        if isinstance(vd, str) and vd.strip():
            verdicts.setdefault(_norm(vd).strip().lower(), []).append(name)
    out = []
    if missing or short:
        out.append(
            "Every choice of a Decide card explains itself in three plain lines, drawn under it in the "
            "popup: \"what\" (what this choice is, in the user's words), \"gain\" (what it gets the user, its "
            "real advantage, even when you do not recommend it) and \"verdict\" (on the recommended "
            "choice: why it beats the others; on every other choice: why the recommended one wins over "
            "this one, the reason only, since the popup's heading already names it). At least " + str(CHOICE_EXPLAIN_MIN_WORDS) + " words each. "
            + ("Missing: " + ", ".join(missing) + ". " if missing else "")
            + ("Too short: " + ", ".join(short) + "." if short else ""))
    if code:
        out.append("A choice's explanation reads like code: " + ", ".join(dict.fromkeys(code)) + ". Say what "
                   "the thing is (\"the app as it was on Oct 29\"), not its hash, path or name; the exact id "
                   "may go in the reply's \"detail\".")
    rec = next((r for r in replies if r.get("recommended") is True), None)
    rec_label = _norm(str(rec.get("label") or "")).strip().lower() if rec else ""
    echo = [str(r.get("key") or r.get("label")) for r in replies
            if r is not rec and rec_label and isinstance(r.get("verdict"), str)
            and _norm(r["verdict"]).strip().lower().startswith(rec_label)]
    if echo:
        out.append("The verdict of " + ", ".join(echo) + " opens on the recommended choice's name, which the "
                   "popup's heading above it already says (\"Why ... is recommended instead\"). Give only "
                   "the reason: not \"Oct 29 app is recommended instead: here the app changed too\" but "
                   "\"Here the app changed too, so ...\".")
    same = [names for names in verdicts.values() if len(names) > 1]
    if same:
        out.append("Choices " + ", ".join("/".join(n) for n in same) + " share one verdict. Each choice "
                   "needs its own: the recommended one says why it wins, each other one says why it lost.")
    return out


# ── Split-view templates: several items answered in one go ──────────────
# A decision may carry, instead of needs.replies, a list of items the Decide
# popup shows in one split view (the items down the left with their answer,
# the open one on the right) and answers in ONE message that starts with the
# template's prefix (_SPLIT and _openSplit in last-card.js):
#   form   several questions, each with options and one recommended
#          (several architecture choices blocking one task)
#   pairs  before-and-after pairs marked Same or Not the same (a change that
#          should leave something looking identical)
# SPLIT_TEMPLATES is the one table of fields, sizes and answer prefixes
# (handoff_evidence.py reads it too).  Drawn only in the popup, so outside the card's
# 120 words (_POPUP_ONLY); each line keeps its cap, except a form question's
# context (FORM_CONTEXT_MAX_WORDS) and a pair's own html or text (none).
# To add a template: an entry in SPLIT_TEMPLATES, its item check in
# _SPLIT_CHECKS, its entry in _SPLIT in last-card.js; tests/test_split_templates.py
# holds the three together.

SPLIT_TEMPLATES = {
    "form": {
        "prefix": "My answers to the", "title": "question", "min": 2, "max": 8,
        # when to use it: the card guide and the summary starter both read this
        "when": "several independent choices block one task (architecture choices, say)",
        "does": "the popup lists the questions down the left and shows the open one with its "
                "options as cards and a note box",
        "example": {"id": "<short id>", "question": "<one short question, plain words, at most 15 words>",
                    "context": "<why it matters: 6 to 30 words>",
                    "options": [{"key": "A", "label": "<the choice, at most 8 words>",
                                 "gain": "<4 to 12 words: what it gets the user>",
                                 "verdict": "<4 to 12 words: why it wins>", "recommended": True},
                                {"key": "B", "label": "<the other choice>",
                                 "gain": "<4 to 12 words: what it gets the user>",
                                 "verdict": "<4 to 12 words: why A wins>"}]},
        "rules": "each question is ONE short sentence of at most 15 words (the popup lists every question down its left "
                 "side; the card shows only the button, so never copy a question into the card's text); \"context\" says in "
                 "6 to 30 plain words what changes for the user, never how it is built (tool counts, tokens, timings and "
                 "history go in Full); 2 to 6 options and exactly one recommended per question, each label at most 8 words, "
                 "its gain and verdict 4 to 12. The popup shows one question at a time, so the question, its context and "
                 "its options together are read as one screen: at most 60 words with two options, 15 more for each "
                 "further option. Open the popup and look at it before you send",
    },
    "pairs": {
        "prefix": "My review of the", "title": "title", "min": 1, "max": 12,
        "when": "a change should leave things looking identical (a refactor, a move, a new "
                "stylesheet) and the user confirms before and after",
        "does": "the popup lists the pairs down the left and shows the open pair side by side "
                "with Same and Not the same",
        "example": {"id": "<short id>", "title": "<what should match, 2+ words>",
                    "a": {"kind": "image", "url": "</static/... or /api/preview/asset/<id>>", "label": "Before"},
                    "b": {"kind": "image", "url": "</static/... or /api/preview/asset/<id>>", "label": "After"}},
        "rules": "a side's kind is image or page (with \"url\", an address this app shows), "
                 "html (with \"html\", drawn with no scripts) or text (with \"text\")",
    },
}


def split_guide() -> str:
    """The card guide's lines for the split-view templates."""
    return "".join(f"- {t['when'][0].upper() + t['when'][1:]} → that same decision, as ONE card with "
                   f"needs.{k} instead of needs.replies (start it with --start {k}).\n"
                   for k, t in SPLIT_TEMPLATES.items())


def split_instructions() -> str:
    """What every session is told about each split-view template."""
    out = []
    for k, t in SPLIT_TEMPLATES.items():
        out.append(
            f"When {t['when']}: ONE decision with needs.{k} instead of needs.replies, "
            f"{t['min']} to {t['max']} items, each shaped like {json.dumps(t['example'], ensure_ascii=False)}; "
            f"{t['rules']}. {t['does'][0].upper() + t['does'][1:]}, and sends every answer back in one "
            f"message starting \"{t['prefix']}\". No needs.look or screens needed; not counted in "
            f"the card's length. The summary starter prints a blank one: --start {k}.\n")
    return "".join(out)
FORM_MIN_OPTIONS, FORM_MAX_OPTIONS = 2, 6
# A form question is listed on the card AND down the popup's left side, so it
# must read at a glance.  A question carrying its own background ("Nearly
# every X does Y, doubling Z. Keep A, or B?") turns eight of them into a wall
# that hides the form; the background belongs in "context".
FORM_QUESTION_MAX_WORDS = 15
# The popup shows ONE question at a time: its question, context and every
# option's label, gain and verdict are read as one screen before a single
# tap.  Per field caps alone still allowed about 110 words a question (every
# local form averaged 99), a wall of text for one pick; so the screen has a
# budget of its own, and each part a cap.
FORM_CONTEXT_MAX_WORDS = 30
FORM_LABEL_MAX_WORDS = 8
FORM_LINE_MIN_WORDS, FORM_LINE_MAX_WORDS = 4, 12
FORM_SCREEN_WORDS, FORM_SCREEN_PER_EXTRA_OPTION = 60, 15


def form_screen_budget(n_options: int) -> int:
    """Words one question's screen may hold: question, context and options."""
    return FORM_SCREEN_WORDS + FORM_SCREEN_PER_EXTRA_OPTION * max(0, n_options - 2)


def form_screen_words(q: dict) -> int:
    """Words the popup shows for one form question."""
    opts = [o for o in _items(q.get("options")) if isinstance(o, dict)]
    return (_words(_s(q.get("question"))) + _words(_s(q.get("context")))
            + sum(_words(_s(o.get(f))) for o in opts for f in ("label", "gain", "verdict")))
# A sentence ends before the last one: ". " / "? " / "! " / "; " followed by
# more words.  Decimals ("2.4") and abbreviations without a space pass.
_INNER_SENTENCE = re.compile(r"[.?!;]\s+\S")
PAIR_KINDS = {"image": "url", "page": "url", "html": "html", "text": "text"}
FORM_ANSWER_PREFIX = SPLIT_TEMPLATES["form"]["prefix"]
PAIRS_ANSWER_PREFIX = SPLIT_TEMPLATES["pairs"]["prefix"]


def split_field(needs) -> str:
    """The split-view template a decision uses ("form", "pairs"), or ''."""
    if not isinstance(needs, dict):
        return ""
    return next((k for k in SPLIT_TEMPLATES if k in needs), "")


def split_items(needs, field: str = "") -> list:
    """The template's items, as given (dicts only)."""
    field = field or split_field(needs)
    if not field or not isinstance(needs, dict):
        return []
    return [x for x in _items(needs.get(field)) if isinstance(x, dict)]


def _form_question_faults(name: str, q: dict) -> list:
    faults = []
    question = _s(q.get("question"))
    if not question:
        faults.append(f"{name} has no \"question\"")
    elif _words(question) > FORM_QUESTION_MAX_WORDS or _INNER_SENTENCE.search(question):
        faults.append(f"{name}: \"question\" is one short sentence of at most {FORM_QUESTION_MAX_WORDS} "
                      "words (now " + str(_words(question)) + "); move its background into \"context\"")
    ctx = _words(_s(q.get("context")))
    if ctx < CHOICE_EXPLAIN_MIN_WORDS:
        faults.append(f"{name} needs \"context\": why it matters, at least {CHOICE_EXPLAIN_MIN_WORDS} words")
    elif ctx > FORM_CONTEXT_MAX_WORDS:
        faults.append(f"{name}: \"context\" is at most {FORM_CONTEXT_MAX_WORDS} words (now {ctx}): say what changes "
                      "for the user; how it is built, figures and history go in Full")
    opts = [o for o in _items(q.get("options")) if isinstance(o, dict)]
    if not (FORM_MIN_OPTIONS <= len(opts) <= FORM_MAX_OPTIONS):
        return faults + [f"{name} needs {FORM_MIN_OPTIONS} to {FORM_MAX_OPTIONS} options"]
    keys = [_s(o.get("key")) or chr(65 + j) for j, o in enumerate(opts)]
    if len(set(keys)) != len(keys):
        faults.append(f"{name} repeats an option key")
    for k, o in zip(keys, opts):
        lw = _words(_s(o.get("label")))
        if lw < 2:
            faults.append(f"{name}, option {k}: spell the choice out in \"label\"")
        elif lw > FORM_LABEL_MAX_WORDS:
            faults.append(f"{name}, option {k}: \"label\" is at most {FORM_LABEL_MAX_WORDS} words (now {lw})")
        for f in ("gain", "verdict"):
            fw = _words(_s(o.get(f)))
            if fw < FORM_LINE_MIN_WORDS:
                faults.append(f"{name}, option {k}: \"{f}\" needs at least {FORM_LINE_MIN_WORDS} words")
            elif fw > FORM_LINE_MAX_WORDS:
                faults.append(f"{name}, option {k}: \"{f}\" is at most {FORM_LINE_MAX_WORDS} words (now {fw})")
        if _HEX_ID.search(_s(o.get("gain")) + " " + _s(o.get("verdict"))):
            faults.append(f"{name}, option {k}: say what the thing is, not its hash")
    if len([o for o in opts if o.get("recommended") is True]) != 1:
        faults.append(f"{name} must mark exactly one option \"recommended\": true")
    screen, budget = form_screen_words(q), form_screen_budget(len(opts))
    if screen > budget:
        faults.append(f"{name} fills its popup screen with {screen} words, over {budget} for {len(opts)} options "
                      "(question, context and every label, gain and verdict, read before one tap): cut it to what "
                      "the user needs to pick, in his words")
    verdicts = [_norm(_s(o.get("verdict"))).lower() for o in opts if _s(o.get("verdict"))]
    if len(set(verdicts)) != len(verdicts):
        faults.append(f"{name}: two options share one verdict")
    return faults


def _pair_faults(name: str, p: dict) -> list:
    faults = []
    if len(_s(p.get("title")).split()) < 2:
        faults.append(f"{name} needs a \"title\" saying what should match")
    for side in ("a", "b"):
        x = p.get(side)
        if not isinstance(x, dict) or x.get("kind") not in PAIR_KINDS:
            faults.append(f"{name}, side {side}: \"kind\" is one of image, page, html, text")
            continue
        field = PAIR_KINDS[x["kind"]]
        if not _s(x.get(field)):
            faults.append(f"{name}, side {side}: a {x['kind']} needs \"{field}\"")
        elif field == "url" and not _VALIDATE_URL.match(_s(x.get(field))):
            faults.append(f"{name}, side {side}: \"url\" must be an address the app can show "
                          "(\"/static/...\", \"/api/preview/asset/<id>\" or http(s)://), not a file path")
    return faults


# (item check, word for one item, the shape to quote when the list is wrong,
#  what a complete item gives)
_SPLIT_CHECKS = {
    "form": (_form_question_faults, "question",
             "{\"id\", \"question\", \"context\", \"options\": [{\"key\", \"label\", \"gain\", \"verdict\", \"recommended\"}]}",
             "Each question is one short sentence, gives its \"context\" (why it matters to the user, at most 30 words), and each option a short \"label\", a \"gain\" (what it "
             "gets the user) and a \"verdict\" (recommended: why it wins; any other: why the recommended one beats "
             "it), with exactly one option recommended per question."),
    "pairs": (_pair_faults, "pair",
              "{\"id\", \"title\", \"a\": {\"kind\": \"image|page|html|text\", \"url|html|text\": ..., \"label\"}, \"b\": {...}}",
              "Each pair has a \"title\" and two sides, each a kind with its content."),
}


def split_problems(card: dict) -> list[str]:
    """What is wrong with a decision's split-view template, if it has one."""
    needs = card.get("needs") if isinstance(card.get("needs"), dict) else {}
    field = split_field(needs)
    if not field:
        return []
    t = SPLIT_TEMPLATES[field]
    check, noun, shape, complete = _SPLIT_CHECKS[field]
    out = []
    if needs.get("kind") != "decision":
        out.append(f"needs.{field} belongs on a decision (needs.kind \"decision\").")
    others = [f"needs.{k}" for k in ("replies", *SPLIT_TEMPLATES) if k != field and (k in needs if k != "replies" else _items(needs.get(k)))]
    if others:
        out.append(f"needs.{field} carries its own answers; leave " + " and ".join(others) + " out of the "
                   "same card, or the popup would show two sets of choices.")
    items = split_items(needs, field)
    lo, hi = t["min"], t["max"]
    if not isinstance(needs.get(field), list) or not (lo <= len(items) <= hi):
        return out + [f"needs.{field} is a list of {lo} to {hi} {noun}s, each {shape}."
                      + (" For one question use needs.replies." if field == "form" else "")]
    ids, faults = set(), []
    for i, x in enumerate(items):
        name = f"{noun} {i + 1}"
        xid = _s(x.get("id"))
        if not xid:
            faults.append(f"{name} has no \"id\"")
        elif xid in ids:
            faults.append(f"{name} repeats the id \"{xid}\"")
        ids.add(xid)
        faults += check(name, x)
    if faults:
        out.append(f"The {noun}s need fixing: " + "; ".join(faults[:8]) + ". " + complete)
    return out + _split_on_face(card, field, items)


# Where a card's own words are drawn, outside the popup.
_FACE_FIELDS = ("headline", "context", "answer", "explained")
_FACE_NEEDS = ("ask", "why", "next")


def _norm_words(text: str) -> str:
    return " ".join(re.findall(r"[a-z0-9]+", (text or "").lower()))


def _split_on_face(card: dict, field: str, items: list) -> list[str]:
    """A split template's items belong in its popup only.  The card draws one
    button naming how many there are; a card that copies its questions (or
    pair titles) into the headline, Short, Plain words or the ask puts the
    wall of text back on the card the popup exists to keep it off."""
    key = SPLIT_TEMPLATES[field]["title"]
    needs = card.get("needs") if isinstance(card.get("needs"), dict) else {}
    face = " | ".join(_norm_words(_s(card.get(k))) for k in _FACE_FIELDS)
    face += " | " + " | ".join(_norm_words(_s(needs.get(k))) for k in _FACE_NEEDS)
    copied = [i + 1 for i, x in enumerate(items)
              if len(_norm_words(_s(x.get(key))).split()) >= 4 and _norm_words(_s(x.get(key))) in face]
    if not copied:
        return []
    noun = _SPLIT_CHECKS[field][1]
    return [f"The {field} items are shown only in the popup: the card draws one button that names how many. "
            f"Take {noun} " + ", ".join(map(str, copied)) + " out of the headline, Short, Plain words and "
            "the ask; say what the decisions are about in a few words instead."]


def split_usable(needs) -> bool:
    """Is there enough to answer?  The card's own test (_SPLIT[...].parse in
    last-card.js): a form needs two questions with two options each, pairs one
    pair with a title and two sides."""
    field = split_field(needs)
    items = split_items(needs, field)
    if field == "form":
        ok = [q for q in items if _s(q.get("question"))
              and len([o for o in _items(q.get("options")) if isinstance(o, dict)]) >= FORM_MIN_OPTIONS]
    else:
        ok = [p for p in items if _s(p.get("title")) and all(
            isinstance(p.get(s), dict) and p[s].get("kind") in PAIR_KINDS for s in ("a", "b"))]
    return len(ok) >= SPLIT_TEMPLATES[field]["min"] if field else False


# ── Layer 1a: handing over work the session could do ─────────────────────
# The class: a session that hits a missing tool (or a command it could run)
# and hands the job to the user instead of finding another way (for example
# "open a terminal and run claude --chrome").  Two checks, on the card alone
# so the settings-file copy runs them too:
#   1. a step that gives the user a command to run, a terminal to open, or a new
#      session to start is work with a shell behind it: the session runs it;
#   2. a "needs_you" or "blocked" card that gives up on a missing ability
#      ("I can't reach", "this session has no browser") must show at least
#      two other ways it tried and why only the user can do it.
# Steps only a person can do (a login, a password or code, an approval on
# a phone, restarting the Session Engine, hands-on work) pass.

_RUNNABLE_STEP = re.compile(
    r"\b(?:open|start|launch)\s+(?:up\s+)?(?:a\s+|your\s+|the\s+)?(?:new\s+)?(?:terminal|command\s+prompt|cmd|"
    r"powershell|shell|console|bash)\b"
    r"|\bin\s+(?:(?:a|your|the)\s+)?(?:new\s+)?(?:terminal|command\s+prompt|powershell|shell|console)\b"
    r"|\b(?:start|open|launch|run)\s+(?:a\s+|another\s+)?(?:new\s+|separate\s+|second\s+)?(?:claude|claude\s+code)"
    r"(?:\s+(?:code\s+)?session)?\b"
    r"|\b(?:start|open|launch)\s+(?:a\s+|another\s+)?(?:new\s+|separate\s+|second\s+)session\b", re.I)
_HUMAN_ONLY = re.compile(
    r"\b(?:log\s*in|login|log\s+into|sign(?:ed)?\s*in|password|passcode|passphrase|2fa|mfa|two-factor|"
    r"verification\s+code|one-time\s+code|captcha|sudo|administrator|admin\s+rights|uac|"
    r"auth(?:enticate|orize)?|credentials?|session\s+engine|unplug|plug\s+in|on\s+your\s+phone)\b", re.I)
_CANT_DO = re.compile(
    r"\bI\s+(?:can'?t|cannot|am\s+unable\s+to|have\s+no\s+way\s+to|don'?t\s+have\s+(?:a\s+way|access|"
    r"the\s+tools?|tools?|any\s+(?:way|tools?)))\b"
    r"|\b(?:this|the|my)\s+session\s+(?:can'?t|cannot|has\s+no|doesn'?t\s+have|lacks|still\s+can'?t)\b"
    r"|\bno\s+(?:browser|tools?|access)\s+(?:control\s+)?(?:here|to|for|in\s+this|available)\b"
    r"|\bnot\s+available\s+to\s+me\b|\b(?:outside|beyond)\s+(?:my|this\s+session'?s)\s+reach\b"
    r"|\bcan'?t\s+(?:reach|open|control|access|get\s+(?:to|into))\b", re.I)
_CANT_FIELDS = ("headline", "context", "needs.ask", "needs.why")
MIN_TRIED = 2


def _tried(card: dict) -> list:
    """What the card says it already tried, wherever it put it."""
    needs = card.get("needs") if isinstance(card.get("needs"), dict) else {}
    out = []
    for it in _items(needs.get("tried")) + _items(card.get("tried")) + _items(card.get("did")):
        txt = _s(it.get("what")) if isinstance(it, dict) else _s(it)
        if txt:
            out.append(txt)
    return list(dict.fromkeys(out))


#: A reply button that only stands in for the user typing: its message is a
#: lead-in with nothing after it ("Here is the first change:"), or it says the
#: answer is (or will be) in the reply box.  Tapping it sends an empty answer.
_TYPING_STANDIN = re.compile(
    r"(?:[:…]|\.\.\.)\s*$"
    r"|\b(?:i'?ll|i will|let me|going to)\s+(?:type|write|describe|tell|explain|say)\b"
    r"|\b(?:reply|text|message|chat)\s+box\b"
    r"|\b(?:typed?|wrote|written|sent)\s+(?:it|the \w+)\s+(?:above|below|in)\b"
    r"|^\s*(?:here(?:'s| is)|it is|my answer|the change is)\b",
    re.IGNORECASE)


def typing_button_problems(card: dict) -> list[str]:
    """Reply buttons that only stand in for typing.  Their message carries
    no answer, so the session asks again and the user has gained nothing."""
    needs = card.get("needs") if isinstance(card, dict) and isinstance(card.get("needs"), dict) else {}
    bad = []
    for r in needs.get("replies") or []:
        if not isinstance(r, dict):
            continue
        text = str(r.get("reply") or r.get("label") or "").strip()
        label = str(r.get("label") or "").strip()
        if _TYPING_STANDIN.search(text) or _TYPING_STANDIN.search(label):
            bad.append(label or text)
    if not bad:
        return []
    return [f"Reply button{'s' if len(bad) > 1 else ''} {', '.join(repr(b) for b in bad)} only stand"
            f"{'' if len(bad) > 1 else 's'} in for typing: tapping sends a message with no answer in it. "
            "Every button must send a real answer: offer concrete choices the user can tap."]


#: An action whose answer is the user's own words: the ask (or a step) opens
#: on telling, typing, describing, naming or pointing at something, or on
#: pasting into this chat.  Pressing a button elsewhere, saving a file or
#: signing in is not one of these: those keep their done button.
_OWN_WORDS_ASK = re.compile(
    r"^\s*(?:please\s+)?(?:(?:now|first|then)\s+)?"
    r"(?:tell\s+me|let\s+me\s+know|type|write(?:\s+(?:me|down|out))?|describe|name|say|explain"
    r"|point\s+(?:at|to|out)|reply\s+with|answer\s+with"
    r"|paste\b[^.]*\b(?:here|in\s+(?:the\s+|this\s+)?(?:reply|chat|message)))\b", re.IGNORECASE)


def asks_for_own_words(card: dict) -> bool:
    """True for an action card that only the user's typed words can answer."""
    if not isinstance(card, dict) or card.get("type") != "needs_you":
        return False
    needs = card.get("needs") if isinstance(card.get("needs"), dict) else {}
    if needs.get("kind") != "action":
        return False
    texts = [_s(needs.get("ask"))] + [_s(st.get("text")) if isinstance(st, dict) else _s(st)
                                      for st in _items(needs.get("steps"))]
    if any(_HUMAN_ONLY.search(t) for t in texts):
        return False
    return bool(_OWN_WORDS_ASK.search(texts[0])) and all(
        _OWN_WORDS_ASK.search(t) for t in texts[1:] if t)


def words_card_problems(card: dict) -> list[str]:
    """Buttons on an action only the user's own words can answer.  The reply
    box is the answer; a button there is either a "done" that carries nothing
    or an errand the session could have run itself, and the user taps it and
    is asked again."""
    if not asks_for_own_words(card):
        return []
    replies = [r for r in _items(card["needs"].get("replies")) if isinstance(r, dict)]
    if not replies:
        return []
    shown = ", ".join(repr(_s(r.get("label")) or _s(r.get("reply"))) for r in replies[:3])
    return [f"This To do asks for the user's own words, so the reply box is the answer: remove the "
            f"button{'s' if len(replies) > 1 else ''} ({shown}) and leave needs.replies empty. A button here "
            "only sends a \"done\" with nothing in it, or asks you for an errand you could run yourself. "
            "Before you stop, put what the answer is about in front of the user (screenshots of it in "
            "needs.look). If you can name the likely answers, make it a decision with each one spelled out."]


#: A reply that sends the session on an errand before the user can answer:
#: show, pull up or photograph the thing, "so I can point at it".  Showing it
#: is the session's job, done before it stops.
_ERRAND_REPLY = re.compile(
    r"^\s*(?:(?:please|first|ok|okay|yes)[, ]+)?(?:show\s+me|pull\s+(?:it\s+|them\s+|\w+\s+)?up|bring\s+up"
    r"|put\s+(?:it|them|the\s+\w+(?:\s+\w+)?)\s+on\s+(?:the\s+)?screen"
    r"|take\s+(?:pictures|screenshots|a\s+screenshot|a\s+picture|photos)|screenshot\b"
    r"|let\s+me\s+see|i(?:'d|\s+would)?\s+(?:want|like)\s+to\s+see)\b"
    r"|\bso\s+(?:that\s+)?i\s+can\s+(?:point|pick|see\s+(?:what|which|it)|look|compare|decide|choose)\b",
    re.IGNORECASE)


def errand_button_problems(card: dict) -> list[str]:
    """Reply buttons that ask the session to show something before the user
    answers.  The session should have shown it: the button costs the user a
    tap and a wait, and the card asks again."""
    needs = card.get("needs") if isinstance(card, dict) and isinstance(card.get("needs"), dict) else {}
    if not isinstance(card, dict) or card.get("type") != "needs_you":
        return []
    bad = [_s(r.get("label")) or _s(r.get("reply")) for r in _items(needs.get("replies"))
           if isinstance(r, dict) and (_ERRAND_REPLY.search(_s(r.get("reply")))
                                       or _ERRAND_REPLY.search(_s(r.get("label"))))]
    if not bad:
        return []
    return [f"Reply button{'s' if len(bad) > 1 else ''} {', '.join(repr(b) for b in bad)} ask"
            f"{'' if len(bad) > 1 else 's'} you to show the user something first. Do that before you stop: "
            "take the screenshots or build the page, put them in needs.look (or the replies' screens), "
            "and ask with the thing already on screen."]


def handover_problems(card: dict) -> list[str]:
    """Work handed to the user that the session could do, or find a way to do."""
    out: list[str] = []
    t = card.get("type")
    needs = card.get("needs") if isinstance(card.get("needs"), dict) else {}
    runnable = []
    for st in _items(needs.get("steps")):
        text = _s(st.get("text")) if isinstance(st, dict) else _s(st)
        cmd = _s(st.get("command")) if isinstance(st, dict) else ""
        both = _norm(text + " " + cmd)
        if (cmd or _RUNNABLE_STEP.search(both)) and not _HUMAN_ONLY.search(both):
            runnable.append(cmd or text)
    if runnable:
        shown = ", ".join(f"\"{r[:60]}\"" for r in runnable[:3])
        out.append(f"needs.steps hands the user work you can run yourself ({shown}). You have a shell: "
                   "run the command, write the script, or call the tool. Never send the user to a terminal or "
                   "to start another session. If a step truly needs the user (a login, a password or code, "
                   "an approval only the user can give), say that in the step.")
    if t in ("needs_you", "blocked"):
        fields = {"headline": card.get("headline"), "context": card.get("context"),
                  "needs.ask": needs.get("ask"), "needs.why": needs.get("why")}
        gap = None
        for where in _CANT_FIELDS:
            m = _CANT_DO.search(_norm(_s(fields.get(where))))
            if m:
                gap = (where, m.group(0).strip())
                break
        if gap and len(_tried(card)) < MIN_TRIED:
            out.append(f"The card gives up on something you lack (\"{gap[1]}\" in {gap[0]}) without "
                       f"showing other ways you tried. A missing tool is a reason to find another way, "
                       "not to hand the job over: write a script, use a library (Playwright, requests), "
                       "call the service's API or the app's own routes, or use a different tool. Try at "
                       f"least {MIN_TRIED}, then list them in needs.tried as {{\"what\": ..., \"result\": "
                       "...}} and say in needs.only_you why only the user can do the rest. If one works, do the "
                       "work instead.")
        if gap and t == "needs_you" and needs.get("kind") == "action" and not _s(needs.get("only_you")):
            if len(_tried(card)) >= MIN_TRIED:
                out.append("needs.only_you is empty: say why only the user can do this step, now that "
                           "the other ways failed.")
    return out


# A card that tells the user which device to pick up to see the work ("open it
# on your phone").  The user chooses the device, and may be at the computer;
# a change that only shows at phone size is shown at phone size inside this
# app (a page framing the live page in phone-width frames, a phone
# screenshot), so the check never depends on what the user is holding.
_DEVICE = r"(?:phone|iphone|android|mobile(?:\s+phone)?|cell\s*phone|smartphone|tablet|ipad|laptop|desktop|computer|pc|mac)"
_DEVICE_YOUR = re.compile(
    r"\b(?:on|from|with|using|via|in)\s+your\s+(?:own\s+)?" + _DEVICE + r"(?:'?s)?\b"
    r"|\b(?:grab|pick\s+up|take\s+out|unlock|open|use|get|switch\s+to|move\s+to|go\s+to)\s+your\s+" + _DEVICE + r"\b",
    re.I)
#: Only in a field that is an instruction by nature: "on a phone" there is
#: still an order to change device.
_DEVICE_ANY = re.compile(
    r"\b(?:on|from|with|using)\s+(?:a|the|any)\s+(?:real\s+)?" + _DEVICE + r"\b"
    r"|\bswitch\s+(?:over\s+)?to\s+(?:a\s+|the\s+)?" + _DEVICE + r"\b", re.I)
#: Things only a device in the user's hand does: a sign-in code, a scan, the
#: microphone or camera, a notification arriving.
_DEVICE_BOUND = re.compile(
    r"\b(?:sign(?:ed)?\s*in|log\s*in|login|2fa|mfa|two-factor|authenticator|verification|one-time|passcode|code|"
    r"qr|scan|camera|microphone|mic|voice|dictat\w*|speak|notification|push\s+alert|buzz|vibrat\w*|"
    r"install|pair(?:ing)?|text\s+message|sms)\b", re.I)
#: Fields that are an instruction to the user whatever their wording.
_DEVICE_ORDER_FIELDS = ("validate.how", "needs.steps")
#: Fields that tell only when a clause starts with an order to the user ("on
#: a phone" there usually names what changed, and "I verify it on a phone" is
#: the session's own work).
_DEVICE_SAY_FIELDS = ("headline", "answer", "needs.ask", "needs.next", "needs.why")
_IMPERATIVE_CLAUSE = re.compile(
    r"(?:^|[.!?;:]\s+|\bthen\s+|\band\s+)(?:open|grab|pick\s+up|take|check|try|look|view|see|go|load|use|"
    r"press|tap|swipe|scroll|visit|test|unlock|reload)\b[^.!?;]*$", re.I)


def device_order_problems(card: dict) -> list[str]:
    """The card sends the user to a particular device to see or try the work."""
    if not isinstance(card, dict):
        return []
    needs = card.get("needs") if isinstance(card.get("needs"), dict) else {}
    val = card.get("validate") if isinstance(card.get("validate"), dict) else {}
    steps = " ".join((_s(st.get("text")) if isinstance(st, dict) else _s(st)) for st in _items(needs.get("steps")))
    fields = {"validate.how": _s(val.get("how")), "needs.ask": _s(needs.get("ask")),
              "needs.next": _s(needs.get("next")), "needs.why": _s(needs.get("why")),
              "needs.steps": steps, "headline": _s(card.get("headline")), "answer": _s(card.get("answer"))}
    found = []
    for where in _DEVICE_ORDER_FIELDS + _DEVICE_SAY_FIELDS:
        text = _norm(fields.get(where) or "")
        if not text:
            continue
        for sentence in re.split(r"(?<=[.!?;])\s+", text):
            m = _DEVICE_YOUR.search(sentence) or (where in _DEVICE_ORDER_FIELDS and _DEVICE_ANY.search(sentence))
            if not m or _DEVICE_BOUND.search(sentence):
                continue
            if where in _DEVICE_SAY_FIELDS and not _IMPERATIVE_CLAUSE.search(sentence[:m.end()]):
                continue
            found.append((where, m.group(0).strip()))
            break
    if not found:
        return []
    shown = ", ".join(f"\"{p}\" ({w})" for w, p in found[:3])
    return [f"The card tells the user which device to use to see the work ({shown}). The user picks the "
            "device and may not have that one in hand. Show the change at that size inside this app instead: "
            "link a page that frames the live page at phone (or desktop) width as validate.url, and add a "
            "screenshot at that size as a \"kind\": \"image\" link. Never send the user to another device to "
            "check your work."]


# ── Layer 1b: length ──────────────────────────────────────────────────────
# The card is read at a glance across many sessions.  Good examples run
# 44-90 words with no line over 18.
# Not counted: the deliverable (the text itself is the point) and "asks" (the
# user's own words); reply texts, urls and commands are already skipped by
# _strings().
MAX_WORDS = 120
MAX_FULL_WORDS = 400
MAX_HEADLINE_WORDS = 20
MAX_TITLE_CHARS = 32
_CARD_WORD = re.compile(r"\bcards?\b", re.I)
_CARD_QUALIFIERS = {"sidebar", "session", "summary", "end-of-turn", "workflow", "task", "copy",
                    "compose", "section", "job", "decide", "credit", "gift", "business", "index"}
MAX_LINE_WORDS = 25

# The title names the thing and the path is the way to it; the headline says
# what happened.  A name cannot be a fragment, an order or a session nickname in disguise, and
# all of it is checked by counting.
MAX_TITLE_WORDS = 5
MAX_PATH_STEPS = 3
MAX_STEP_CHARS = 28
MAX_STEP_WORDS = 4
MAX_PATH_CHARS = 48
_NAME_PUNCT = re.compile(r"[:;,!?]|(?<!\d)\.|\.(?!\d)")
_NOT_A_NAME = re.compile(
    r"^(?:pick|choose|make|fix|add|build|move|remove|catch|stop|use|ban|try|restart|reload|approve|confirm|"
    r"give|send|write|tell|rename|draft|decide|check|why|how|what|which|when|where|who|should|can|could|"
    r"does|do|did|is|are|was|were|will|would)\b", re.I)


def title_path_problems(card: dict) -> list[str]:
    out = []
    title = _s(card.get("title"))
    if title:
        why = []
        if len(title.split()) > MAX_TITLE_WORDS:
            why.append(f"{len(title.split())} words (at most {MAX_TITLE_WORDS})")
        if _NAME_PUNCT.search(title):
            why.append(f"it has \"{_NAME_PUNCT.search(title).group(0)}\"")
        m = _NOT_A_NAME.match(title)
        if m:
            why.append(f"it starts with \"{m.group(0)}\", so it reads as an order or a question")
        if why:
            out.append(f"\"title\" (\"{title}\") is not the name of a thing: " + "; ".join(why) + ". The "
                       "title only names the thing (\"Header image\", \"Session right-click menu\"); the "
                       "headline under it says what happened to it.")
    path = card.get("path")
    steps = [x.strip() for x in path if isinstance(x, str) and x.strip()] if isinstance(path, list) else []
    if not steps:
        out.append("\"path\" is missing: one to three short names leading to the thing the title names, "
                   "broad to specific, drawn small above it. [\"Article series\", \"Gen vs Validation "
                   "Leverage\"] above \"Header image\"; [\"Sidebar\"] above \"Session right-click menu\". "
                   "Not the project: the app already shows it.")
        return out
    if len(steps) > MAX_PATH_STEPS:
        out.append(f"\"path\" has {len(steps)} steps; at most {MAX_PATH_STEPS}. Keep the ones the user needs to "
                   "find the thing.")
    bad = [x for x in steps if len(x) > MAX_STEP_CHARS or len(x.split()) > MAX_STEP_WORDS or _NAME_PUNCT.search(x)]
    if bad:
        out.append("\"path\" steps are short names (at most " + str(MAX_STEP_WORDS) + " words and "
                   + str(MAX_STEP_CHARS) + " characters, no punctuation): " + ", ".join(f"\"{x}\"" for x in bad) + ".")
    joined = " › ".join(steps)
    if len(joined) > MAX_PATH_CHARS:
        out.append(f"\"path\" runs {len(joined)} characters; at most {MAX_PATH_CHARS} so it fits one line "
                   "on a phone. Shorten the steps.")
    if title and steps[-1].lower() == title.lower():
        out.append("\"path\" ends with the title itself; the path is the way TO it, the title is the last step.")
    return out

MAX_ANSWER_WORDS = 60
MAX_LIST_ITEMS = 4
# Lists that may run longer: the user's own requests, and link rows.
# needs.stages: a Working card's stage track, at most 8 short names
# (format_problems), drawn as a bar, not read as lines.
_LONG_LISTS = ("asks", "links", "needs.look", "needs.stages")
# A decision reply's template fields are drawn only inside the full-screen
# Decide popup, one choice at a time, never on the card, so they are not
# counted toward the card's length or its list limit.  Each line still has
# the per-line cap.
_POPUP_ONLY = re.compile(r"^(?:needs\.replies\[\d+\]\.(?:strengths|weaknesses|opportunities|risks|fit|effort|undo|risk|screens|what|gain|verdict)\b"
                         r"|needs\.(?:" + "|".join(SPLIT_TEMPLATES) + r")\b)")
# A pair's own text or page source is content to compare, not a line to read.
_PAIR_CONTENT = re.compile(r"^needs\.pairs\[\d+\]\.[ab]\.(?:html|text)$")
# A form question's context may run longer than a line: it is the paragraph
# the popup shows above that question's options (capped by _form_question_faults).
_FORM_CONTEXT = re.compile(r"^needs\.form\[\d+\]\.context$")


def _words(s: str) -> int:
    return len(s.split())


def _lists(node, path: str = "") -> Iterator[tuple[str, list]]:
    if isinstance(node, dict):
        for k, v in node.items():
            if k in _SKIP_KEYS:
                continue
            yield from _lists(v, f"{path}.{k}" if path else k)
    elif isinstance(node, list):
        yield path, node
        for i, v in enumerate(node):
            yield from _lists(v, f"{path}[{i}]")


def card_words(card: dict) -> int:
    """The card's word count as the length rule counts it (the self-check,
    ``src/engine/handoff_check.py``, prints it)."""
    return _length_tally(card)[0]


def length_problems(card: dict) -> list[str]:
    """What makes the card too long to read at a glance."""
    total, long_lines = _length_tally(card)
    long_lists = [f"{where} has {len(items)} items (limit {MAX_LIST_ITEMS})"
                  for where, items in _lists(card)
                  if len(items) > MAX_LIST_ITEMS
                  and not where.startswith(("deliverable",) + _LONG_LISTS + _addon_keys())
                  and not _POPUP_ONLY.match(where)]
    # Full is the deep version, not a log of commits, ports and process ids.
    # A deliverable's body is the text the user asked for and has no cap.
    fa = card.get("full_answer")
    full_long = []
    if isinstance(fa, str) and (n_full := _words(fa)) > MAX_FULL_WORDS:
        full_long = [f"Full (full_answer) is {n_full} words; the limit is {MAX_FULL_WORDS}. Keep what it does "
                     "for the user now, how to see it, and what is left; move commit lists, file lists and logs "
                     "into links. A long text the user asked for goes in a deliverable."]
    if total <= MAX_WORDS and not long_lines and not long_lists:
        return full_long
    out = [f"Too long: the card is {total} words; the limit is {MAX_WORDS}. The user reads it "
           "at a glance between many sessions. Keep what happened, where it stands and what the user "
           "must do; cut explanation, background and anything the user does not need to act. "
           "If the user asked for the long text itself (an email, a report, a detailed "
           "explanation), use type \"deliverable\": its body has no limit. "
           "\"explained\" (Plain words) and \"full_answer\" (Full) are not counted: "
           "do NOT shorten them to fit, cut the short lines instead."]
    out += long_lines[:5] + long_lists[:3] + full_long
    return out


def _addon_keys() -> tuple:
    """Card keys a project's stop add-on owns (``src/engine/stop_plugins``): outside the 120 words."""
    try:
        from engine import stop_plugins
        return tuple(stop_plugins.uncounted_keys())
    except Exception:
        return ()


def word_table(card: dict) -> list:
    """Every string in the card as the length rule sees it:
    ``[(where, words, counted toward the 120, cap or None)]``.  The
    self-check prints it, so a session cuts where the words are."""
    rows = []
    for where, text in _strings(card):
        n = _words(text)
        # zoom: smart_termination.zoom_problems; explained: the "Explain more"
        # text, long on purpose (smart_termination.explained_problems);
        # full_answer: the answer's "Full" version, behind the Answer band's
        # switch and long on purpose.
        # finding: the "Also found" band, three capped lines outside the 120
        # (finding_problems); the per-line cap below still applies.
        # approval: the Approve band (what, and its Short / Plain words / Full
        # behind the info button), drawn in Review & respond; outside the 120.
        # same_set_because: never drawn, kept only for debugging the variant
        # rule (handoff_evidence.refine_variants_problems).
        if where.startswith(("deliverable", "asks", "zoom", "explained", "full_answer", "same_set_because",
                             "finding.answer", "finding.explained", "finding.full_answer",
                             "offer_info", "approval", "needs.stages", "needs.stage", "back_to.detail")
                            + _addon_keys()):
            # finding.answer / explained / full_answer: the finding's info
            # button text, capped by finding_problems.
            rows.append((where, n, False, None))
            continue
        # what_was_done / checked: the two lines every summary carries in its
        # What was done and Checked sections; outside the 120,
        # each still capped like any line.  Popup-only reply fields likewise.
        if _PAIR_CONTENT.match(where):
            rows.append((where, n, False, None))
            continue
        if where.startswith(("finding", "what_was_done", "checked")) or _POPUP_ONLY.match(where):
            rows.append((where, n, False, MAX_ANSWER_WORDS if _FORM_CONTEXT.match(where) else MAX_LINE_WORDS))
            continue
        cap = (MAX_HEADLINE_WORDS if where == "headline"
               else MAX_ANSWER_WORDS if where == "answer" else MAX_LINE_WORDS)
        rows.append((where, n, True, cap))
    return rows


def _length_tally(card: dict):
    """(words counted toward the 120, lines over their own cap)."""
    total = 0
    long_lines = []
    for where, n, counted, cap in word_table(card):
        if counted:
            total += n
        if cap is not None and n > cap:
            long_lines.append(f"{where} is {n} words (limit {cap})")
    return total, long_lines


# ── Machinery words where the user looks first ───────────────────────────
# The title, headline and zoom lines say what the user sees or asked; the
# words for the machinery belong in context, links or a deliverable.
_MACHINERY = re.compile(
    r"\b(?:daemon|hooks?|loaders?|modules?|"
    # a browser reload is the user's own word ("reload the tab", "without a reload")
    r"(?<!a\s)(?<!last\s)(?<!one\s)reload(?:s|ed|ing)?(?!\s+(?:the\s+|your\s+|this\s+)?(?:tab|page|browser|window|app)\b)|callbacks?|config(?:uration)?s?|"
    r"cach(?:e|es|ed|ing)|regex(?:es|ps)?|endpoints?|stale|refactor(?:s|ed|ing)?|payloads?|schemas?|"
    r"backend|frontend|IPC|websockets?|subprocess(?:es)?|stdout|stderr|mtime|stack\s*traces?|"
    r"monkey-?patch\w*|race\s+conditions?|null|undefined|async|serializ\w+|"
    r"handlers?|listeners?|parsers?|renderers?|(?<!Session\s)engine|"
    # the card's own field names: on screen they read Big picture / This
    # task / Just now, so "zoom lines" means nothing to the user
    r"zoom(?:\s+lines?)?)\b", re.I)
MACHINERY_FIELDS = ("path", "title", "headline", "zoom.big", "zoom.task", "zoom.now", "needs.ask")


def _path_text(card: dict) -> str:
    p = card.get("path")
    return " › ".join(x for x in p if isinstance(x, str)) if isinstance(p, list) else ""


def machinery_problems(card: dict) -> list[str]:
    needs = card.get("needs") if isinstance(card.get("needs"), dict) else {}
    zoom = card.get("zoom") if isinstance(card.get("zoom"), dict) else {}
    fields = {"path": _path_text(card), "title": card.get("title"), "headline": card.get("headline"),
              "zoom.big": zoom.get("big"), "zoom.task": zoom.get("task"), "zoom.now": zoom.get("now"),
              "needs.ask": needs.get("ask")}
    hits = []
    for where in MACHINERY_FIELDS:
        text = fields.get(where)
        if not isinstance(text, str):
            continue
        # "Session Engine" is what the app calls it and may be named in an
        # ask to restart it; anywhere else it is still machinery.
        t = _norm(text) if where == "needs.ask" else _norm(text).replace("Session Engine", "Session engine")
        for m in _MACHINERY.finditer(t if where == "needs.ask" else t.replace("Session engine", "engine")):
            hits.append(f"\"{m.group(0)}\" ({where})")
    if not hits:
        return []
    return ["Written from the implementer's seat: " + ", ".join(dict.fromkeys(hits)) + ". The title, "
            "headline, zoom lines and ask say what the user sees, asked or gets, in the user's words; the "
            "machinery goes in context or links. \"Card rules load live\" becomes \"Why my card "
            "skipped the title rule\"."]


# A choice named by its key where the user skims ("Header A, more
# frustrated").  The key ("A", "Layout 4") meant something on a mockup page
# hours ago; read between ten other sessions it names nothing.  Unlike _LABEL, an explanation after it does not excuse it:
# these lines have no room for one, so they name the thing itself.
# Two forms:
#  - any word followed by a lone capital A-H ("Header A", "your pick B",
#    "B or C"), minus idioms ("Plan B", "Vitamin C", "Type A", "Series A").
#    "A" is also the article: in a Title Case title the next word is
#    capitalised ("A Bigger Person"), so "A" only counts when the next word is
#    lowercase, punctuation or the end ("Header A drawn", "Header A,").
#  - a variant noun followed by a number ("Layout 4", "option 2").
_VARIANT_NOUNS = (r"option|options|variant|variants|choice|design|layout|mockup|mock-up|sketch|"
                  r"pick|idea|approach|concept|proposal|alternative|candidate|style|direction|"
                  r"treatment|take")
_VARIANT_IDIOMS = {"plan", "vitamin", "type", "series", "class", "grade", "hepatitis", "section",
                   "schedule", "form", "exhibit", "appendix", "annex", "model", "drive", "size"}
_VARIANT_KEY = re.compile(
    r"(?<![\w/+#.-])(?:(?P<word>[A-Za-z][\w'-]*)\s+)"
    r"(?P<key>A(?=\s*$|\s*[,.;:!?)—–]|\s+[a-z])|[B-H](?![\w/+#'-]))"
    # a line that opens on the key ("D without the big picture"); a leading
    # "A" is the article ("A clearer header") and is left alone
    r"|^\s*[B-H](?![\w/+#'.-])"
    r"|\b(?i:" + _VARIANT_NOUNS + r")\s+#?\d{1,2}\b(?!\.\d)"
)
# needs.why / needs.next too: "Why I'm asking: it is your D" names a choice
# the user no longer has in front of them.
VARIANT_FIELDS = MACHINERY_FIELDS + ("needs.why", "needs.next")


def variant_name_problems(card: dict) -> list[str]:
    needs = card.get("needs") if isinstance(card.get("needs"), dict) else {}
    zoom = card.get("zoom") if isinstance(card.get("zoom"), dict) else {}
    fields = {"path": _path_text(card), "title": card.get("title"), "headline": card.get("headline"),
              "zoom.big": zoom.get("big"), "zoom.task": zoom.get("task"), "zoom.now": zoom.get("now"),
              "needs.ask": needs.get("ask"), "needs.why": needs.get("why"), "needs.next": needs.get("next")}
    hits = []
    for where in VARIANT_FIELDS:
        text = fields.get(where)
        if not isinstance(text, str):
            continue
        for m in _VARIANT_KEY.finditer(_norm(text)):
            word = (m.group("word") or "").lower()
            if word in _VARIANT_IDIOMS:
                continue
            hits.append(f"\"{m.group(0).strip()}\" ({where})")
    if not hits:
        return []
    return ["Names a choice by its letter or number: " + ", ".join(dict.fromkeys(hits)) + ". The "
            "path, title, headline, zoom lines, ask, why and next never call a variant by its key, even with an "
            "explanation after it: the user no longer has the options in front of the user. Name what "
            "it is: \"Header A, more frustrated\" becomes the title \"Header image\" with the headline "
            "\"The person in the header image is now more frustrated\"."]


# ── Layer 2: banned phrases by type ───────────────────────────────────────

def _checks(card: dict, prose: str) -> list[tuple[str, str, str, str]]:
    """``(group name, advice, where, phrase)`` for every banned phrase found."""
    t = card.get("type")
    groups = BANNED_BY_TYPE.get(t, _ALWAYS)
    texts = list(_strings(card))
    if prose.strip():
        texts.insert(0, ("text above the card", prose))
    hits = []
    for where, text in texts:
        text = _norm(text)
        # The deliverable is text for someone else (an email may well say
        # "should I" or "follow up"), and a compare block describes how
        # things behave.  Neither is the session talking about its own work,
        # so only the writing-style check applies there.
        quoted = where.startswith(("deliverable", "compare"))
        if where.startswith("asks"):
            continue   # the user's own words ("fix the open issues you left")
        # A finding is, by definition, something noticed and not acted on
        # (finding_problems); its words for that are not unfinished work.
        finding = where.startswith("finding")
        for name, advice, rx in groups:
            if quoted and rx is not AI_TELL[2]:
                continue
            if finding and rx in (UNFINISHED[2], BLAME[2]):
                continue
            # offer_info describes work not yet done that the user may ask for: its
            # words for that are neither unfinished work nor a job handed back.
            if where.startswith("offer_info") and rx in (UNFINISHED[2], HAND_BACK[2]):
                continue
            if t == "needs_you" and rx is HAND_BACK[2] and where == "needs.ask":
                continue
            if rx is FILE_PATHS[2] and where.startswith(("links", "needs.look")):
                continue   # link rows are where files belong: the card makes them tappable
            for m in rx.finditer(text):
                hits.append((name, advice, where, m.group(0).strip()))
    return hits


def label_problems(card: dict, prose: str) -> list[tuple[str, str]]:
    """``(where, label)`` for each label the user may not know."""
    out = []
    texts = list(_strings(card))
    if prose.strip():
        texts.insert(0, ("text above the card", prose))
    for where, text in texts:
        if where.startswith(("deliverable", "asks")):
            continue  # text for someone else, or the user's own words
        for m in _LABEL.finditer(_norm(text)):
            out.append((where, m.group(0).strip()))
    return out


# ── Decision ──────────────────────────────────────────────────────────────

INSTRUCTIONS = """\
# Ending a turn: the handoff card (Smart termination)

The user runs many sessions at once and does NOT read your intermediate \
messages. Every turn, for any reason (finished, blocked, a notification, a \
watchdog nudge), ends with ONE fenced block tagged `handoff` as the very last \
thing in your message. The app draws it as a card. Write it for someone who \
has read nothing else in this session.

```handoff
{"type": "done | needs_you | working | blocked | deliverable | explain",
 "path": ["the way to the thing, broad to specific", "..."],
 "title": "the thing itself, a short name, drawn big",
 "headline": "one sentence under the title: what happened to it",
 "context": "what we are working on, in plain words",
 "status": {"done": ["..."], "remaining": ["..."], "broken": ["..."]},
 "verified": [{"check": "...", "result": "...", "ok": true}],
 "asks": [{"ask": "the user's request, in a few of their own words",
           "state": "done | answered | replaced | needs_you | blocked | working"}],
 "links": [{"label": "...", "kind": "link|file|image", "url": "..."}],
 "validate": {"how": "what the user does to see it work himself", "url": "the page to try it on",
              "later": "only when the real moment is later: when it happens for real"},
 "needs": {"kind": "none | decision | approve | action",
           "ask": "...", "why": "...", "next": "...", "eta_minutes": 40,
           "stages": ["working: the job's stages, in order"], "stage": "the one running",
           "look": [{"label": "...", "kind": "link", "url": "..."}],
           "steps": [{"text": "...", "command": "..."}],
           "replies": [{"key": "A", "label": "the choice spelled out", "detail": "...", "reply": "...",
                        "what": "what it is", "gain": "what it gets the user",
                        "verdict": "why it wins, or why the recommended one beats it",
                        "recommended": true}]},
 "finding": {"what": "something you noticed outside the user's ask", "risk": "what it risks for the user",
             "recommend": "what you recommend",
             "answer": "Short: what it is and why it matters, two or three sentences",
             "explained": "Plain words: the same for someone lost, markdown",
             "full_answer": "Full: everything behind it, markdown",
             "reply": "optional: what its button sends"}}
```

Which card to end on: go by what the user's last message was, then by what \
is true now.
- The user asked a question → "explain" with the answer in "answer" ("deliverable" \
when it is long). Never a Decide card, even if you have something to offer, \
with one exception: mockups of something the user asked for are on screen (a \
scratch page, or "screens" on the replies). Then it is a decision with a reply \
per variant, and the answer goes in "answer". An outburst that ends in "?" \
("are you kidding me?") about something you then rebuild is that case too. \
A question asked in frustration, with swearing, is often rhetorical: it means \
the work is wrong or undone. Fix the work and end on the card that work \
calls for, not an explain card that only answers it.
- The user asked for a text (an email, notes, a plan) → "deliverable".
- The user asked for a file (a download, a zip, a PDF, a deck) → "deliverable" \
with "files".
- The user gave an order, and it is finished and checked → "done".
- The user gave an order, and a choice that only the user can make blocks it → \
"needs_you" with needs.kind "decision", tied to that order in "asks" (state \
"needs_you"). One decision, the one that blocks the work.
{split_guide}- A matter of taste in the user's order (how something looks, reads or is laid \
out, its tone, wording or naming) IS such a choice: lean toward showing \
variants instead of picking one yourself and calling it done. Build 3 or 4 \
that span a real range, from the safe one closest to how it is now to a \
clearly bolder one, never near-copies of one idea. Skip the variants only when the user spelled out exactly what the user \
wants, or the call is not a matter of taste (a bug, a fact, a rule); then \
start a line of Full with "One version because" and say why.
- The user answers variants with feedback instead of a pick ("I like X, but change \
Y", "the text is wrong") → that set is finished. Keep everything the user did not \
object to, and make the part the user objected to the ONLY thing that varies: 3 or 4 \
NEW variants of exactly that part. Bringing the set back with one variant changed \
or added is sent back; when the same set truly is right, give the reason in a \
top-level field "same_set_because" (not shown to the user, kept for debugging).
- The user answers variants with a direction of the user's own instead of a pick \
("how about we make X the button", "what if it were a list") → that is still a \
matter of taste: the direction is set, how it looks is not. Show 3 or 4 variants \
of THAT direction and end on a Decide card; building one version and calling it \
Done is sent back unless the user spelled the result out (then "One version \
because" in Full).
- Only the user can do the next step (log in, restart, approve) → "needs_you" with \
"action" or "approve".
A missing tool or ability is never a reason to hand the user the job. Find \
another way first: write a script, use a library (Playwright, requests), \
call the service's API or the app's own routes, or use a different tool. A \
step that sends the user to a terminal, gives the user a command to run, or has the user \
start another session is sent back: you have a shell, so run it. If at \
least two other ways truly failed, an action card lists them in \
needs.tried ([{"what": "...", "result": "..."}]) and says in \
needs.only_you why only the user can do the rest; a card that says "I can't" \
without them is sent back.
- Your own background work is still running → "working". Something outside \
your control stops you → "blocked".
- The user's message starts "Wrong kind of card:" → the user flagged your last card \
and picked another kind. End this reply on the kind the user names, doing whatever work that kind \
needs (a Done means doing the work, a Decide means showing the options). \
Another kind is sent back.
- The user's message starts "Flagged summary:" → the user flagged your last card as bad \
for the reasons it names. Redo that reply as the same kind of card with those faults fixed, \
unless the fault is the kind itself (a pick between options sent as an Approve is a Decide). \
The redo stays on the same task: its title, headline and Short are still the \
result of what the user asked for (what the user now sees), not the hurdle \
you met on the way. A snag in a step the user did not ask about (shipping \
refused because of someone else's code, a tool that broke) comes after the \
result, never as the card's headline or its whole Short.
- A pick between options is always needs.kind "decision", never "approve": only \
the Decide popup shows each option's "screens"; an Approve draws bare buttons.
- Work the user asked for waits only on the user's yes (the user asked a question about it, or \
must OK a step) → "needs_you" with needs.kind "approve" and a button per \
answer; on a question the answer goes in "answer" on that card. If the user \
already said what the user wants, do it instead of asking.
- Shipping is part of an order. When the user ordered a change and this \
session already commits or pushes its work without asking, ship the finished \
change the same way and end on "done" with its Validate; an "OK to ship?" \
approval the user never asked for hands the user a job you could do, and is \
sent back. Ask only when this project's rules need the user's OK for that \
step, and name the rule in needs.only_you.
- Pictures of a fix to how something looks show BOTH states, each labelled: \
"Before: ..." (how it was, from the old code if you no longer have a picture) \
and "After: ..." for each place you changed. Pictures named only after the \
place leave the user guessing which is the problem and which is the fix; such \
a card is sent back.
An idea, an improvement or a follow-up you would like to offer is never a \
decision: name it in needs.next and let the user ask for it. On an "explain", \
"deliverable" or "done" card also put it in "offer", a few words naming what \
you would do ("Give answer cards their own buttons", "Point your tools at the \
stand-in"): it is drawn as a "Do what it suggested" button that sends "Yes, \
do that" (on a done card, ahead of Done). With an "offer" add \
"offer_info": {"answer", "explained", "full_answer"}: its info button opens \
them as Short / Plain words / Full, saying what you would do, why, what it \
changes for the user and what it costs. A "none" card's next \
never waits on the user ("once you confirm", "when you say go"): such a card is \
sent back.
Something you NOTICED outside the user's ask and may not act on alone (a permission \
rule, a security gap, a cost) goes in "finding": {"what", "risk", \
"recommend", "answer", "explained", "full_answer"}, drawn as a quiet "Also \
found" band with one button; its info button opens the finding's own Short \
(answer) / Plain words (explained) / Full (full_answer), like the summary. It is \
neither unfinished work nor a decision, so a done card may carry it, and it \
is never turned into a Decide/Approve: a decision or approval must be tied to \
something THE USER asked for, in the user's words, in "asks" (state "needs_you"); one that \
is not is sent back.
A Decide or Approve question is a yes-or-no the user can picture. Its ask, why, \
next and button labels name the outcome in plain words ("stop a session from \
wiping a folder on Windows, as it is already stopped on Mac"), never command \
syntax, flags or code names ("del /s /q", "--force", "chrome_control"): those \
go in a reply's "detail", in context or in Full. Mark the reply you \
recommend with "recommended": true (exactly one), and say why in needs.why; \
a pick between mockups is the user's taste and needs no recommendation. Every other \
Decide choice explains itself in three plain lines, drawn under it in the \
popup: "what" (what the choice is, in the user's words, no hashes or paths), "gain" \
(what it gets the user, its real advantage even when not recommended) and \
"verdict" (on the recommended one: why it beats the others; on each other \
one: why the recommended one wins over it, the reason only, because the \
popup's heading already names the recommended choice).

Types:
- done: everything asked for is finished and checked. Nothing in remaining or \
broken, no failed check, needs.kind "none" with why and next. It must give \
the user something to review: "links" to the work itself (the page to try, \
a screenshot, a file the user asked for) or a "compare" block showing before and \
after. A link is something the user opens to try or see the work, never your \
proof: no commit, diff or code-host page (the user does not review what you \
checked, and a private repository shows the user a 404). Say what you checked in \
plain words in "checked" and "verified", never in refs, hashes or commands; \
those go in Full. When there is nothing of the work to open (a fix to \
records or history), show a "compare" of what reads differently now. It must \
have "validate": one line on how the user checks it works himself, and in \
"url" the page the user opens to try it NOW and do what "how" says, on the \
very thing the user asked for (each part of it: asked for a phone version too, \
the user tries it at phone size). A screenshot is never that page: pictures go \
in "links" and are drawn beside the row as "OR just look". When the app's own \
page cannot be linked to the exact spot, build a demo page in the scratch \
folder that loads the real code and opens on it. When the real check \
only happens later (the next time something occurs), build a demo page that \
shows it happening, link it in "url", and put the real moment in "later". A \
Validate row with nothing to open, or one that only opens VibeNode's front \
page (the app the user is already in), is sent back. The page must open \
where the user is already signed in: one that lands on a sign-in page (a \
server you started on a new port, a second copy, an address the user's browser \
holds no sign-in for) leaves the user a job and is sent back. Link the copy and \
address the user already uses, or bring the change there; only when the work is \
sign-in itself may "url" be the sign-in page. Never tell the user which \
device to use ("open it on your phone"): the user may be at the computer. A \
change that shows only at phone size is shown at that size here: a page that \
frames the live page at phone width in "url", plus a phone-size screenshot.
- needs_you: only the user can do it: a decision that is theirs (each choice \
spelled out, plus a link to look at), an approval, or an action such as a \
login (steps plus an "I did it" button). Every button sends a real answer: a \
button that only stands in for typing ("I'll type it", "Here is the change:") \
sends a message with nothing in it and is sent back. When you need the user's \
direction, offer concrete choices the user can tap. When only the user's own \
words can answer (what to change, a name), typing them already answers the \
card: the reply box is the answer, so that To do has NO button (needs.replies \
empty). A "Done" or "Sent it" button carries nothing, and an errand button \
("Show me the panel", "Pull it up first") asks for work that was yours: \
before you ask, put the thing in front of the user yourself (screenshots in \
needs.look). If you can name the likely answers, make it a decision with each \
one spelled out instead. A button that only asks you to show something first \
is sent back on every card. A choice about how something looks \
or is presented (a design, layout, style, slides, a deck) must SHOW every \
option before you ask: a scratch page of clickable mockups in needs.look and \
each reply's "screens" pointing at its own section of that page \
("<page>.html#<id>"), shown live like the Now tab, never a screenshot. A file \
to read is not a preview; such a card is sent back. An approval of something \
the user must see ("ship the new dashboard?") puts ONE page to review in needs.look[0] \
(the live page, or a review page in the scratch folder laying the screens out the way they \
are judged: Before | After side by side for each place, each labelled) and at most two \
screenshots of it; links are only what you looked at. Never a pile of pictures: a card \
with more than two loose screenshots and no page to open them on, or more than four in \
all, is sent back.
- working: idle on purpose while YOUR background work runs. needs.eta_minutes is required \
(a number: the card counts it down live). Give the job's stages too: needs.stages (2 to 8 \
short names in order) and needs.stage (the one running). The card fills them in and shows a \
live feed from your background job's events while the user waits, so make every event the job \
sends name its stage and say done or passed when one ends ("Unit tests passed").
- blocked: something outside your control stops you. Add "tried": [...] and \
say in needs.why exactly what blocks you.
- deliverable: the point was a piece of text, or files to download. Put \
it in "deliverable": \
{"kind": "email|notes|report|…", "title": "…", "body": "the text, in \
markdown (## headings, - lists, **bold**)"}. Optional "meta" is an object \
of labelled lines, e.g. {"To": "…", "Subject": "…"}; leave it out otherwise. \
Optional "format": "markdown" (the default), "html" (a whole page, drawn \
as a web page; no scripts run) or "text" (plain text, shown as written); \
the user opens it full screen with a Copy button. Files to download (a zip, \
a PDF, a deck) go in "files": [{"label": "what it is", "url": \
"/static/scratch/<name>", "size": "3.4 MB"}], drawn as Download buttons; \
a text file there with "copy": true is drawn as a Copy button instead (a long \
text to paste). "files" goes inside "deliverable"; at the top of the card \
nothing draws it. The body IS the text the user asked for (a prompt, an \
email, notes), whole: the box's Copy copies the body, so a body that only \
says where the text is ("use the Copy button on the file", "the full prompt \
is in the file above") hands the user that note. Such a card is sent back. Never hand a file over as a Validate row or a \
link: such a card is sent back.
- explain: an answer to a question, in "answer" (or a "compare" block).
EVERY card, of every type, reads the same: the title, the headline, then one \
switch with three versions, Short / Plain words / Full. "answer" is Short: \
two or three sentences that go one level past the headline, never a repeat \
of it (on an explain card, the answer; on any other, what happened or what \
is asked). "explained" is Plain words. "full_answer" is Full: everything \
behind it in markdown (## headings, - lists, tables), at most 400 words. A \
deliverable's "body" is NOT its Full: it is drawn in its own box in Review & \
respond with Copy and Draft in Gmail, so the card still needs "full_answer" \
(the reasoning, what changed and why), and an email body ends at its \
sign-off. A card missing one of the three is sent back.
Every card is drawn in the same six sections: the header (path, title, \
headline), Summary, What was done, Checked, History, Review & respond. Two of them come from one plain line each, \
required on every card: "what_was_done" says what you actually did this \
turn ("Answered directly from training data; no web search, no tools used", \
"Changed the archive menu and its filter") and "checked" says what you \
looked at to be sure ("No research beyond base training data", "One web \
search; two news sources below", "Ran the archive tests: 14 passed"). Say \
what is TRUE: both lines are compared with the tools you ran this turn, and \
a line that claims a search, file read or test you did not run, or denies \
one you did, is sent back. Status items and "did" add to What was done; \
"verified" and links add to Checked; screenshots go in Validate.
When earlier work in this session is still unfinished and this card is \
about something else, add "back_to": {"label": "that work, in a few words", \
"status": "where it stands", "reply": "the message to send to resume it", \
"detail": "what it was waiting on, in plain words, behind its Details fold"}. \
The card draws it as a "Left waiting" strip with a Back to it button and a \
cross the user can hide it with; do not also put it in needs.next. Carry it only \
while it is truly unanswered. A choice the user has not made about the very thing \
you just worked on (you refined the preview it asks the user to ship or approve) \
is never "back_to" under a Done: end on that choice again, with what changed \
in "answer".

Name the thing, then say what happened to it. Picture the user switching in \
with no memory of this session: "path" and "title" tell the user WHERE, the \
headline tells the user WHAT. The app already shows the project, so neither \
names it.
- "title" is the thing itself, a short name drawn big: at most 5 words and at most \
32 characters, no verb needed, no ":" "," ";" "." "?" "!" ("Header image", \
"Session right-click menu", "Top bar"). Never a sentence, an order, a \
question or a fragment ("A more frustrated person", "Pick a header", "Why \
Done woke up").
- "path" is the way to it, one to three short names from broad to specific, \
drawn small above the title: ["Article series", "Gen vs Validation \
Leverage"] above "Header image"; ["Sidebar"] above "Session right-click \
menu". Use names the user would recognise: the user's own words, an area of the app, a \
document's title. Never the project, a choice letter or a name you made up. \
Keep its first step the same all session unless the user's message starts a new \
topic.
- The headline is the sentence: what happened to the thing, or what the user \
must do about it.
Never name a choice by its letter or number in the path, title, headline, \
zoom lines or ask ("Header A", "Layout 4", "your pick B"): say what it is.

The headline and needs.ask are shown ON THEIR OWN (sidebar, Needs you list, \
notifications) to a user working across many sessions. Each must make sense \
with no other context: name the project and the exact thing ("Pick a design \
for the buttons on VibeNode's end-of-turn summary card", not "Pick a button \
design for the card"). No bare "the card", "the fix", "this", "it".

Write from the reviewer's seat, not the implementer's. The user reads the \
title, headline and zoom lines first, between ten other sessions: they say \
what THE USER asked, sees or gets, in the user's words, never the name of your change or \
the machinery you touched (engine, hook, loader, module, cache, config, \
stale...: those go in context or links). If the user's last message asks a \
question, answer it first in "answer" (one or two plain sentences), as type \
"explain" (or "deliverable" when long) with needs.kind "none": never turn \
an answer into a decision or add a new choice to it (unless mockups the user asked \
for are on screen: those always get a reply per variant). Not \
"Card rules load live" / "Engine reloads card rules on change", but "Why my \
card skipped the title rule" / "The checker ran an old copy of the rules, so \
my untitled card got through". Call things by the names the user sees on screen, \
not your field names, and say the user's problem before your fix: not "Zoom lines \
capped at 40 characters" but "The lines under each card title wrapped to \
three lines on the phone; now one or two". The title is the user's report, not \
yours: what the user gets, named so the user knows which thing without reading on \
("Summaries now in your words", not "Cards say the problem first").

Keep it SHORT. The whole card is at most 120 words (the deliverable, \
"full_answer" and "asks" don't count): headline at most 20 words, any other line at most 25, \
an explain answer at most 60, and at most 4 items in any list. Say what \
happened, where it stands and what the user must do; leave out background \
and explanation. A longer card is sent back. Long text the user asked for \
(an email, a report, a detailed explanation as kind "notes") goes in "deliverable", which has \
no limit; only the card around it must be short.

"asks": when the user made more than one request in this task, list every one \
with its state. "replaced": a later message from the user changed it, so it \
was not done as first asked. A done card has every ask done or answered. Something the user \
has asked for more than once must be done before you stop.

A message the user sends while you work (a queued message) arrives as their \
next message the moment you stop, and your stop is not checked while one is \
waiting. Adapt to it: change course if it says so, and make your next card \
cover everything they asked in this task, not only the newest message.

Claims are checked against what you actually did in this session: "tests \
pass" needs a test run that passed (the latest one), "checked in the app" \
needs a browser, screenshot or preview step, "pushed" needs a push that went \
through. If a claim is about earlier work, say when it happened.

Before you stop, re-read what the user asked. Work you can do yourself is not \
a reason to stop. These get the card sent back: listing unfinished work as \
caveats, open items, flags or follow-ups in a done card; blaming a problem on \
another change or session instead of fixing it; file names or paths in the \
text (the user can't open them from a chat; put files in "links"); asking "want me to" or \
"should I" in place of doing it; apologies and promises ("from now on I'll", \
"that's on me"); notes to yourself ("privately"); bare labels from earlier \
("option C", "point 5") without saying what they are.

Stay on the user's task. A message marked as an automated session message (a \
background job finished, a check-in you scheduled, the watchdog, an \
automatic continue) is not from the user: it never changes the task, the title or \
the This task line, and the card after it says where THE USER'S task stands. The \
title, headline and Short are about what the user asked, never about how you worked: \
commits, pushes, branches, test counts, ports and pixel offsets go in Full or \
links. Full stays under 400 words. Never leave the user a job in any tab ("run X", \
"X clears it"), and stop your own servers, jobs and spare copies before a \
Done. When the user redirects you ("focus", "what was your task", "get back to the \
actual work"), put the user's whole message in asks, say where the task the user first \
gave this session stands, and go back to it.
""".replace("{split_guide}", split_guide())

_REWRITE = "Rewrite only your final message unless the problem is unfinished work; then do the work first."


# Every picture and page a summary points at on this app must load (for
# example /api/preview/asset/<upload name> serves only screenshots the
# preview tool rendered, so an upload name there opens to nothing).  Only this app's own
# addresses are checked, against the files that serve them: /static/... must be
# a file under src/web/static/, /api/preview/asset/<id> a rendered screenshot.  Other
# addresses are not fetched (that would slow every summary).
_STATIC_DIR = Path(__file__).resolve().parents[2] / "src" / "web" / "static"
_PREVIEW_DIR = Path(__file__).resolve().parents[2] / "data" / "previews"
_PREVIEW_ASSET = re.compile(r"^/api/preview/(?:asset|thumb)/([^/?#]*)")


def _card_urls(card: dict):
    """(where, url) for every address the summary shows or opens."""
    needs = card.get("needs") if isinstance(card.get("needs"), dict) else {}
    for i, l in enumerate(_items(card.get("links"))):
        if isinstance(l, dict):
            yield f"links[{i}]", _s(l.get("url"))
    for i, l in enumerate(_items(needs.get("look"))):
        if isinstance(l, dict):
            yield f"needs.look[{i}]", _s(l.get("url"))
    v = card.get("validate")
    if isinstance(v, dict):
        yield "validate.url", _s(v.get("url"))
    for i, r in enumerate(_items(needs.get("replies"))):
        if isinstance(r, dict):
            for j, s in enumerate(_items(r.get("screens"))):
                if isinstance(s, dict):
                    yield f"needs.replies[{i}].screens[{j}]", _s(s.get("url"))
                    for k, pic in _screen_pictures(s.get("image")):
                        yield f"needs.replies[{i}].screens[{j}].image{k}", pic
    for i, pr in enumerate(split_items(needs, "pairs") if split_field(needs) == "pairs" else []):
        for side in ("a", "b"):
            x = pr.get(side) if isinstance(pr.get(side), dict) else {}
            if x.get("kind") in ("image", "page") and _s(x.get("url")):
                yield f"needs.pairs[{i}].{side}.url", _s(x.get("url"))
    d = card.get("deliverable")
    if isinstance(d, dict):
        for i, f in enumerate(_items(d.get("files"))):
            if isinstance(f, dict):
                yield f"deliverable.files[{i}]", _s(f.get("url"))


def _screen_pictures(img, where=""):
    """(suffix, address) for a Decide screen's ``image``: a string, or
    {web, phone}, each a string or {light, dark}."""
    if isinstance(img, str):
        if img.strip():
            yield where, img.strip()
    elif isinstance(img, dict):
        for k, v in img.items():
            yield from _screen_pictures(v, f"{where}.{k}")


def _disk_path(url: str) -> Optional[Path]:
    """The file a picture or link given as a path on this machine names
    (a deleted temp-folder screenshot draws as a broken picture).
    ``file://``, ``~/``, a
    drive letter, or an absolute path whose first folder exists on this
    machine (``/home/...``, ``/tmp/...``); an app page such as ``/settings``
    is not one."""
    u = (url or "").strip()
    if u.lower().startswith("file://"):
        u = urllib.parse.unquote(u[7:])
    if u.startswith("~/") or re.match(r"^[A-Za-z]:[\\/]", u):
        return Path(u).expanduser()
    m = re.match(r"^/([^/?#]+)/", u)
    if not m or m.group(1) in ("api", "static", "socket.io"):
        return None
    try:
        if not Path("/" + m.group(1)).is_dir():
            return None
    except OSError:
        return None
    return Path(re.sub(r"[?#].*$", "", u))


def _app_address_missing(url: str) -> bool:
    """True when *url* is one of this app's own addresses, or a file on this
    machine, and nothing is there."""
    u = re.sub(r"^https?://(?:localhost|127\.0\.0\.1)(?::5050)?(?=/)", "", url or "", flags=re.I)
    disk = _disk_path(u)
    if disk is not None:
        try:
            return not disk.exists()   # a folder is a fine link
        except OSError:
            return True
    m = _PREVIEW_ASSET.match(u)
    if m:
        pid = m.group(1)
        return not (re.fullmatch(r"[0-9a-f]{32}", pid) and (_PREVIEW_DIR / (pid + ".png")).is_file())
    if u.startswith("/static/"):
        rel = urllib.parse.unquote(re.sub(r"[?#].*$", "", u)[len("/static/"):])
        try:
            f = (_STATIC_DIR / rel).resolve()
            f.relative_to(_STATIC_DIR.resolve())
        except (OSError, ValueError):
            return True
        return not f.is_file()
    return False


def broken_link_problems(card: dict) -> list[str]:
    # VIBENODE_LINK_CHECK=0 turns it off (the test suite does, for its
    # made-up addresses; see tests/conftest.py).
    if not isinstance(card, dict) or os.environ.get("VIBENODE_LINK_CHECK") == "0":
        return []
    urls = [(w, u) for w, u in _card_urls(card) if u]
    bad = [f"{where} ({url})" for where, url in urls if _app_address_missing(url)]
    stopped = [f"{where} ({url})" for where, url in _stopped_ports(urls)]
    out = []
    if bad:
        out.append("These addresses open to nothing, so the user taps them and sees a blank: "
                   + "; ".join(bad[:4]) + ". Link only what exists. A screenshot of a page: render it with "
                   "POST /api/preview/render and use the \"src\" it returns. A picture you already have (an "
                   "upload, a saved image, a file in the temp folder): copy it into src/web/static/scratch/ and link "
                   "/static/scratch/<name>.")
    if stopped:
        out.append("These pages are on a server of this machine that is not running, so they open blank: "
                   + "; ".join(stopped[:4]) + ". Start the server and leave it running while the user reviews, "
                   "or show a screenshot instead (POST /api/preview/render while it runs).")
    return out


_LOOPBACK_URL = re.compile(r"^https?://(localhost|127\.0\.0\.1|\[::1\])(?::(\d+))?(?:[/?#]|$)", re.I)


def _stopped_ports(urls):
    """(where, url) for pages on another port of this machine that refuse a
    connection (a demo or approval page goes blank once its server stops).
    A preview-proxy address is judged by

    the page it relays.  A short connect only, at most 6 ports, VibeNode's
    own port never.

    A port that refuses is tried again for ``_PORT_GRACE_S`` before it counts
    as stopped: a dev server that reloads itself when a session edits code
    (Django's runserver, Vite) refuses for a second or two while it restarts,
    and one refusal sent cards back saying a running server was down."""
    import socket
    import time
    hosts: dict = {}
    order = []
    for where, url in urls:
        target = url
        m = re.search(r"/api/preview/proxy\?(?:.*&)?u=([^&#]+)", url)
        if m:
            target = urllib.parse.unquote(m.group(1))
        lm = _LOOPBACK_URL.match(target)
        if not lm or not lm.group(2) or lm.group(2) == "5050":
            continue
        port = int(lm.group(2))
        if port not in hosts:
            if len(hosts) >= 6:
                continue
            host = "::1" if lm.group(1).startswith("[") else "127.0.0.1"
            hosts[port] = [host, "::1"] if lm.group(1).lower() == "localhost" else [host]
        order.append((where, url, port))

    def _answers(port):
        for h in hosts[port]:
            try:
                with socket.create_connection((h, port), timeout=0.4):
                    return True
            except OSError:
                continue
        return False

    dead = {p for p in hosts if not _answers(p)}
    deadline = time.monotonic() + _PORT_GRACE_S
    while dead and time.monotonic() < deadline:
        time.sleep(0.25)
        dead = {p for p in dead if not _answers(p)}
    for where, url, port in order:
        if port in dead:
            yield where, url


# How long a refusing port on this machine is retried before a card's link to
# it counts as a stopped server (see _stopped_ports).
_PORT_GRACE_S = 6.0


# A Validate page that lands on a sign-in page hands the user a job: sign in
# before trying anything (and, on a second copy of an app sharing the host,
# possibly signing out of the copy the user uses).  Checked only for pages on
# another port of this machine, and only when the browsers VibeNode opens in
# (Chrome-family, any profile, and VibeNode's own window profile) hold no
# sign-in cookie for that host, so a page the user is signed in to is never
# loaded.  The page is loaded the way a signed-out visitor sees it: a plain
# request following redirects, then, when that ends on a page of scripts, a
# headless browser (a single-page app redirects in script).
# VIBENODE_SIGNIN_CHECK=0 or VIBENODE_LINK_CHECK=0 turns it off (the suite).
_SIGNIN_PATH = re.compile(
    r"/(?:accounts/)?(?:log-?in|sign-?in|signon|auth(?:enticate)?|sso|session/new|users?/sign_in)(?:[/?#.]|$)",
    re.I)
_SIGNIN_WORK = re.compile(r"\b(?:log-?in|sign-?in|sign(?:ing)? in|log(?:ging)? in|auth\w*|password|sso)\b", re.I)
_SESSION_COOKIE = re.compile(r"sess|^sid$|auth|jwt|login|remember|access|refresh|^token$|_token$|bearer", re.I)
_NOT_SESSION_COOKIE = re.compile(r"csrf|xsrf", re.I)
_PASSWORD_FIELD = re.compile(r"<input[^>]+type\s*=\s*[\"']?password", re.I)
_signin_cache: dict = {}


def _browser_cookie_dbs():
    """Cookie stores of the browsers VibeNode opens in on this machine
    (Chrome, Chromium, Edge, Brave: every profile; VibeNode's own window
    profile).  Only host and cookie NAME are read, never a value."""
    home = Path.home()
    roots = [
        home / ".config/google-chrome", home / ".config/chromium",
        home / ".config/microsoft-edge", home / ".config/BraveSoftware/Brave-Browser",
        home / "Library/Application Support/Google/Chrome",
        home / "Library/Application Support/Chromium",
        Path(__file__).resolve().parents[2] / "data" / "chrome-profile",
    ]
    local = os.environ.get("LOCALAPPDATA")
    if local:
        roots += [Path(local) / "Google/Chrome/User Data", Path(local) / "Microsoft/Edge/User Data",
                  Path(local) / "Chromium/User Data"]
    for root in roots:
        try:
            yield from list(root.glob("*/Cookies")) + list(root.glob("*/Network/Cookies"))
        except OSError:
            continue


def _signed_in_hosts() -> Optional[set]:
    """Hosts those browsers hold a sign-in cookie for, or None when no
    cookie store could be read (then nothing is judged)."""
    import sqlite3
    found, read_any = set(), False
    for path in _browser_cookie_dbs():
        try:
            uri = "file:" + urllib.parse.quote(str(path)) + "?mode=ro&immutable=1"
            db = sqlite3.connect(uri, uri=True, timeout=0.5)
            try:
                rows = db.execute("SELECT host_key, name FROM cookies").fetchall()
            finally:
                db.close()
        except Exception:
            continue
        read_any = True
        for host, name in rows:
            name = str(name or "")
            if _SESSION_COOKIE.search(name) and not _NOT_SESSION_COOKIE.search(name):
                found.add(str(host or "").lstrip(".").lower())
    return found if read_any else None


def _visited_past_signin(origin: str) -> bool:
    """True when a browser VibeNode opens in has history of a page on
    *origin* (scheme://host:port) that is not a sign-in page.  Read only, and
    only the addresses of that origin."""
    import sqlite3
    for cookies in _browser_cookie_dbs():
        hist = cookies.parent.parent / "History" if cookies.parent.name == "Network" else cookies.parent / "History"
        if not hist.is_file():
            continue
        try:
            db = sqlite3.connect("file:" + urllib.parse.quote(str(hist)) + "?mode=ro&immutable=1",
                                 uri=True, timeout=0.5)
            try:
                rows = db.execute("SELECT url FROM urls WHERE substr(url, 1, ?) = ? LIMIT 500",
                                  (len(origin) + 1, origin + "/")).fetchall()
            finally:
                db.close()
        except Exception:
            continue
        if any(not _SIGNIN_PATH.search(urllib.parse.urlsplit(u).path) for (u,) in rows):
            return True
    return False


def _lands_on_signin(url: str, timeout: float = 4.0) -> Optional[str]:
    """Where a signed-out visit to *url* ends when that is a sign-in page,
    else None (also None when it cannot tell)."""
    import urllib.request
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "VibeNode link check"})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            final = r.geturl()
            body = r.read(400_000).decode("utf-8", "replace")
    except Exception:
        return None
    if _SIGNIN_PATH.search(urllib.parse.urlsplit(final).path) or _PASSWORD_FIELD.search(body):
        return final
    if "<script" not in body.lower():
        return None
    return _browser_lands_on_signin(url)


_PROBE = r"""
import sys, re
from urllib.parse import urlsplit
from playwright.sync_api import sync_playwright
url, pat = sys.argv[1], re.compile(sys.argv[2], re.I)
with sync_playwright() as p:
    b = p.chromium.launch()
    try:
        pg = b.new_context().new_page()
        pg.goto(url, wait_until="domcontentloaded", timeout=8000)
        for _ in range(16):
            if pat.search(urlsplit(pg.url).path) or pg.locator("input[type=password]").count():
                print("SIGNIN " + pg.url); break
            pg.wait_for_timeout(250)
        else:
            print("OK " + pg.url)
    finally:
        b.close()
"""


def _browser_lands_on_signin(url: str) -> Optional[str]:
    """The same question for a page that redirects in script, asked of a
    headless browser in its own process (no cookies).  None when no
    interpreter here has a browser, or it is slow: the check never guesses."""
    import shutil
    import subprocess
    import sys
    for exe in dict.fromkeys(x for x in (sys.executable, shutil.which("python3")) if x):
        try:
            out = subprocess.run([exe, "-c", _PROBE, url, _SIGNIN_PATH.pattern],
                                 capture_output=True, text=True, timeout=20).stdout
        except Exception:
            continue
        m = re.search(r"^(SIGNIN|OK) (\S+)", out or "", re.M)
        if m:   # this interpreter had a browser: its answer stands
            return m.group(2) if m.group(1) == "SIGNIN" else None
    return None


def signin_wall_problems(card: dict) -> list[str]:
    if (not isinstance(card, dict) or os.environ.get("VIBENODE_SIGNIN_CHECK") == "0"
            or os.environ.get("VIBENODE_LINK_CHECK") == "0"):
        return []
    v = card.get("validate")
    url = _s(v.get("url")) if isinstance(v, dict) else ""
    m = re.search(r"/api/preview/proxy\?(?:.*&)?u=([^&#]+)", url)
    if m:
        url = urllib.parse.unquote(m.group(1))
    lm = _LOOPBACK_URL.match(url)
    if not lm or not lm.group(2) or lm.group(2) == "5050":
        return []
    if _SIGNIN_PATH.search(urllib.parse.urlsplit(url).path):
        return []   # the sign-in page itself, linked on purpose
    zoom = card.get("zoom") if isinstance(card.get("zoom"), dict) else {}
    about = " ".join(_s(x) for x in (card.get("title"), card.get("headline"), zoom.get("task")))
    if _SIGNIN_WORK.search(about):
        return []   # the work is sign-in itself
    host = lm.group(1).lower().strip("[]")
    signed = _signed_in_hosts()
    if signed is None:
        return []
    # A cookie is the host's, whatever the port, so a sign-in on the copy the
    # user uses (one port) says nothing about a second copy (another port)
    # with its own sign-in.  The host's cookie counts only when the user's
    # browser has also been past the sign-in page on this exact port.
    if host in signed and _visited_past_signin(f"{url.split(':')[0].lower()}://{lm.group(1)}:{lm.group(2)}"):
        return []
    import time
    hit = _signin_cache.get(url)
    if not hit or time.monotonic() - hit[0] > 60:
        hit = (time.monotonic(), _lands_on_signin(url))
        _signin_cache[url] = hit
    landed = hit[1]
    if not landed:
        return []
    return [f"validate.url ({url}) opens on a sign-in page ({landed}): the user's browser has never "
            f"been signed in at {lm.group(1)}:{lm.group(2)}, so the user would have to sign in before trying anything, a job left to the user "
            "(and on a second copy of an app, signing in there can sign the user out of the copy the user "
            "uses). Link the page on the copy and address the user is already signed in to, or bring the "
            "change there. Only when the work is sign-in itself may the link be the sign-in page."]


def review(message: str, facts=None) -> Optional[str]:
    """The reason to send the session back, or None when the card passes.

    ``facts``: a ``handoff_evidence.TurnFacts`` for this session (its tool
    calls and the user's requests in this task).  With it, the card's claims
    are checked against what the session did, and the card must account for
    every request (``src/engine/handoff_evidence.py``).  Without it, those checks
    are skipped.

    Caller decides when this applies (preference on, a session that talks to
    a person, not an error or usage-limit stop, send-back cap not reached).
    """
    card, prose, err = extract(message)
    if err:
        return ("Smart termination: " + err + " End every turn with one ```" + FENCE_LANG
                + " card as the last thing in your message.\n\n" + INSTRUCTIONS)
    problems = (format_problems(card) + download_problems(card) + proof_link_problems(card)
                + email_tail_problems(card) + deliverable_pointer_problems(card)
                + handover_problems(card) + typing_button_problems(card)
                + words_card_problems(card) + errand_button_problems(card)
                + device_order_problems(card)
                + length_problems(card) + machinery_problems(card) + variant_name_problems(card)
                + broken_link_problems(card) + signin_wall_problems(card))
    hits = _checks(card, prose)
    labels = label_problems(card, prose)
    from engine import handoff_evidence
    evidence = handoff_evidence.problems(card, facts)
    if not problems and not hits and not labels and not evidence:
        return None

    parts = ["Smart termination: your handoff card was sent back."]
    if problems:
        parts.append("Format:\n" + "\n".join("- " + p for p in problems))
    if evidence:
        parts.append("Read on its own, and against what actually happened in this session:\n"
                     + "\n".join("- " + p for p in evidence))
    if hits:
        by_group: dict[str, list] = {}
        advice = {}
        for name, adv, where, phrase in hits:
            by_group.setdefault(name, []).append(f"\"{phrase}\" ({where})")
            advice[name] = adv
        for name, found in by_group.items():
            seen = list(dict.fromkeys(found))[:5]
            parts.append(f"Not allowed in a \"{card.get('type')}\" card, {name}: "
                         + ", ".join(seen) + ". " + advice[name])
    if labels:
        seen = list(dict.fromkeys(f"\"{lab}\" ({where})" for where, lab in labels))[:6]
        parts.append("Labels the user may not know: " + ", ".join(seen)
                     + ". Say what each one is, e.g. \"option C (the Expanded/Collapsed links "
                       "in the preview header)\".")
    parts.append(_REWRITE)
    return "\n\n".join(parts)
