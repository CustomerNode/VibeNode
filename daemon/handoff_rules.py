"""Handoff rules: the last message of a turn must be one of the allowed card
types, and each type has phrases that send the session back to think again.

The owner asked for this on 2026-10-27.  It is the second layer of Smart
termination (``daemon/smart_termination.py``):

1. **Format.**  A session's final message ends with ONE fenced block tagged
   ``handoff`` holding a JSON object of one of the types in the last-message
   library (``static/scratch/last-message.html``): done, needs_you, working,
   blocked, deliverable, explain.  Anything else is sent back.
2. **Banned phrases, by type.**  The phrases come from mining the owner's own
   chat history on two machines (2026-10-27): the messages that came right
   before the replies where he swore.  Each group below names the habit it
   stands for, because the habit is what is banned; the phrases are how it
   shows up.  "honest caveat" itself appeared only once in seven weeks; the
   habit behind it ("done", with unfinished work attached) showed up under a
   dozen wordings.  So a group is banned, not a phrase.

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
# The "Redo as…" button on the newest card (static/js/last-card.js,
# REDO_KINDS) lists exactly these, word for word; tests/test_card_redo.py
# fails when a type or a needs.kind above has no entry here, or when the two
# lists differ (owner, 2026-10-29: "you are missing card types, lets make sure
# thats impossible").  (key, type, needs.kind or None for any, name, what he gets)
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
        r"\bleaving\s+(?:it\s+|them\s+|this\s+)?open\b"),
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

# A file named in the card's text: the owner reads it in a chat window where
# a path is something he can't open (2026-10-27: "you just listed 4 filenames
# in a text blob ... thats worse than useless").  Files go in "links" /
# needs.look, which the card draws as rows he can tap.  URLs are not matched
# (they are clickable); neither are product names like "Node.js".
FILE_PATHS = (
    "file names in the text",
    "The user reads this in a chat window and can't open a path from text. "
    "Say what the file does in plain words, and put the file itself in "
    "\"links\" (or needs.look) so the card shows it as a row he can tap.",
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
# give it.  The owner, 2026-10-28, on "Nothing needed. Version B goes into the
# real cards once you confirm": "why the fuck does next say nothing needed and
# then something about once i confirm with no ctas. that is fucking useless and
# sessions keep doing it".
_WAITS_ON_USER = re.compile(
    # "once/when/after you ..." is a wait; "if you want it shorter, tell me"
    # is an open offer and passes.
    r"\b(?:once|when|after|as\s+soon\s+as)\s+you(?:'?ve)?\s+(?:have\s+)?(?:confirm|approve|okay|ok|o\.k\.|"
    r"agree|say|said|pick|picked|choose|chose|decide|decided|sign\s+off|give|gave|tell|told|let\s+me\s+know|"
    r"reply|answer|green-?light)\w*\b"
    r"|\b(?<!nothing\sis\s)(?<!nothing's\s)(?<!nothing\s)(?<!not\s)(?<!isn't\s)(?<!no\sone\sis\s)(?:on|with|after|pending|awaiting|waiting\s+(?:on|for))\s+your\s+(?:go-?ahead|ok(?:ay)?|confirmation|approval|"
    r"sign-?off|word|yes|pick|choice|decision|answer|reply|call|green\s+light)\b"
    r"|\b(?:the\s+one|whichever|whatever|the\s+version|the\s+option)\s+you\s+(?:pick|choose|approve)\b"
    r"|\b(?<!nothing\sis\s)(?<!nothing's\s)(?<!nothing\s)(?<!not\s)(?<!isn't\s)(?<!no\sone\sis\s)waiting\s+(?:on|for)\s+you\b|\bup\s+to\s+you\b|\bwhen(?:ever)?\s+you'?re\s+ready\b", re.I)


# A choice about how something looks or is presented must SHOW the options.
# The owner, 2026-10-28, on a "Live HTML slides / PowerPoint deck / one-page
# brief" decision whose only thing to look at was a markdown outline: "it gave
# me a choice with no visual where a mockup or preview would've helped, please
# fix that".  The words are matched only in what names the choice (title,
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
            "preview. Build a scratch page, static/scratch/<topic>.html, with every option as "
            "a real, clickable mockup side by side, each tagged data-option=\"<key>\" (and the "
            "app as it is today tagged data-option=\"now\", when there is one). Put the page "
            "in needs.look and give each reply \"screens\": [{\"label\": \"...\", \"url\": "
            "\"/static/scratch/<topic>.html#<key>\", \"device\": \"web|phone|both\"}]. "
            "A screenshot per option (a \"kind\": \"image\" link) also works. In a project "
            "whose instructions show options in its real app (CustomerNode's design "
            "trials), follow them instead: each reply's \"screens\" are that app's live "
            "pages on this machine, with their pictures in \"image\".")


# A screen that is another app's live page on this machine (CustomerNode's
# design trials, 2026-10-28: "live pages plug into the vibenode stuff") is
# framed signed in on this computer, but the phone cannot reach it and the
# preview proxy cannot sign in to it, so the phone shows the screen's picture
# ("image", drawn by _tplStage in static/js/last-card.js).  Without one, the
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
# card must come with pictures of it: a bare link hid the options (owner,
# 2026-10-28, on an answer card offering three button mockups as one link:
# "why did you not show me the mockups here, another failure").  A decision
# shows them through its replies' "screens" in the Decide popup.
_MOCKUP_PAGE = re.compile(r"^(?:https?://[^/]+)?/static/scratch/[^?#]+\.html?(?:[?#].*)?$", re.I)


def mockup_picture_problem(card: dict) -> Optional[str]:
    """Why a card links a mockup page without showing it, or None."""
    needs = card.get("needs") if isinstance(card.get("needs"), dict) else {}
    entries = _items(card.get("links")) + _items(needs.get("look"))
    pages = [_s(it.get("url")) for it in entries
             if isinstance(it, dict) and _s(it.get("kind")) != "image" and _MOCKUP_PAGE.match(_s(it.get("url")))]
    if not pages:
        return None
    if any(isinstance(it, dict) and _s(it.get("kind")) == "image" for it in entries):
        return None
    replies = [r for r in _items(needs.get("replies")) if isinstance(r, dict)]
    if _s(needs.get("kind")) == "decision" and any(_items(r.get("screens")) for r in replies):
        return None
    if _s(needs.get("kind")) == "decision" and replies:
        # The choices are shown once, inside Decide: never as a Screens band
        # as well (owner, 2026-10-28: "screens and decide, both with the same
        # choices, confused the fuck out of me").
        return (f"This Decide card links a mockup page ({pages[0]}) but its choices show nothing "
                "of it. Give each reply its own picture in \"screens\": [{\"label\": \"...\", "
                "\"url\": \"/static/scratch/<topic>.html#<key>\"}], its own section of the page, shown like Now. "
                "They are shown inside the Decide popup, one per choice. Do not add them as "
                "\"links\" too: the card would show the same choices twice.")
    return (f"This card links a mockup page ({pages[0]}) but shows nothing of it: the user sees "
            "a link, not the options. Add a screenshot of each option inside the card: a "
            "\"links\" entry {\"label\": \"what it shows\", \"kind\": \"image\", \"url\": \"...\"} per "
            "option (a PNG saved in static/scratch/ is served at /static/scratch/<name>.png).")


# A Decide card shows its choices once.  Pictures of the choices go in each
# reply's "screens" (drawn inside the Decide popup); as "links" they became a
# Screens band beside the Decide button with the same choices (owner,
# 2026-10-28: "screens and decide, both with the same choices, confused the
# fuck out of me. thats a smart termination failure").
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


# Every choice is shown the way "Now" is (owner, 2026-10-28: "idk why im
# getting these shitty zoomed in dark theme variants when the now is the way
# you should be presenting that ... i also need a check on variants that
# forces consistency").  The Decide popup's Now tab is the page's live
# data-option="now" section, in his theme and at real size; a reply whose
# screen was a PNG came out zoomed (shot at 2x) and in the theme it was shot
# in, so the choices could not be compared with Now or with each other.
# When the card has a mockup page, every reply's screen is its own section of
# that same page.  Live pages of another app (CustomerNode's design trials)
# are live too and pass.
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


# Variants on screen always come with a way to pick one (owner, 2026-10-28:
# "screens with no next cta makes zero fucking sense ... i blame smart
# termination").  An answer card that showed three mockups of a button he
# had asked for ended on "Name one" because the question rule forced it to
# be an explain card.  A mockup page is the scratch page with two or more
# data-option variants besides "now"; the page lives in this repo's
# static/scratch, so it is read from disk.
_SCRATCH_DIR = Path(__file__).resolve().parents[1] / "static" / "scratch"
_JS_BUILT_OPTION = re.compile(r"""data-option=(?:\\?["'])?\s*['"]?\s*\+|data-option=["']\$\{""")
_JS_OPTION_KEY = re.compile(r"""\bkey\s*:\s*["']([A-Za-z0-9]{1,3})["']""")
_OPTION_ATTR = re.compile(r"""data-option\s*=\s*["']([^"']+)["']""", re.I)
_JS_OPTION_ROW = re.compile(r"""\[\s*["']([A-Z][0-9]?)["']\s*,\s*["']""")


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
            "\"device\": \"both\"}], tied to his request in \"asks\" (state \"needs_you\"). If he "
            "also asked a question, answer it in \"answer\" on the same card.")


# A validate.url the card can open: a page on this app, or a web address.
_VALIDATE_URL = re.compile(r"^(?:/(?!/)|https?://\S)", re.I)


def is_app_home(url) -> bool:
    """True when ``url`` is VibeNode's own front page and nothing more: "/",
    or this app's port on this machine or the tailnet, with no path, query or
    fragment.  2026-10-29: a done card's Validate row for an Update fix linked
    "http://localhost:5050/", the app the owner was already in ("telling me to
    validate via a link to the interface i am already in was manic").  A deep
    link ("/?session=…", "#kanban") points at something and passes."""
    u = _s(url).strip()
    if u in ("/", ""):
        return u == "/"
    m = re.match(r"^https?://(?:[^@/?#]*@)?(\[[^\]]*\]|[^:/?#]+)(?::(\d+))?(/?)$", u, re.I)
    if not m:
        return False
    host, port = m.group(1).lower(), m.group(2)
    local = host in ("localhost", "127.0.0.1", "[::1]", "0.0.0.0") or host.endswith(".ts.net")
    return local and (port == _own_port() or (port is None and host.endswith(".ts.net")))

# A file to download, not a page to look at.  2026-10-28: a presentation was
# handed over as a "done" card whose Validate row pointed at a .zip, so "Try
# it" framed the archive full screen and showed nothing (the owner: "the
# download here is fucked ... that should be a deliverable type, and what it
# did needs to get rejected").  Files go in deliverable.files, drawn as
# Download buttons.
_DOWNLOAD_FILE = re.compile(
    r"\.(?:zip|tar|tgz|gz|bz2|xz|7z|rar|pdf|pptx?|key|odp|docx?|odt|rtf|xlsx?|ods|csv|tsv"
    r"|epub|dmg|pkg|exe|msi|apk|deb|rpm|appimage|whl|iso|mp4|mov|m4v|webm|mkv|avi|mp3|m4a|wav|flac)"
    r"(?:[?#].*)?$", re.I)
# Where the browser can fetch a file from: this app's static files or API,
# or a web address.  A path on disk ("/home/…", "~/…") is not one.
_SERVED_URL = re.compile(r"^(?:/(?:static|api)/\S|https?://\S)", re.I)


def is_download(url) -> bool:
    return bool(_DOWNLOAD_FILE.search(_s(url).strip()))


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
                               "address the browser can download from: put the file in static/scratch/ "
                               "(not published) and give \"/static/scratch/<name>\", or a web address. "
                               "A path on disk downloads nothing, on the phone least of all.")
    return out


# ── Layer 1: the format ───────────────────────────────────────────────────

def format_problems(card: dict) -> list[str]:
    """What is wrong with the card's shape, by the rules of its type."""
    out = []
    t = card.get("type")
    if t not in TYPES:
        # Keep going: returning here hid every other fault until the type was
        # fixed, so a summary with no answer buttons used up a send-back on the
        # type alone and was let through untested (2026-10-29).  The checks
        # below that depend on the type simply do not fire.
        out.append(f"\"type\" must be one of: {', '.join(TYPES)} (got {json.dumps(t)}).")
    if not _s(card.get("headline")):
        out.append("\"headline\" is empty: one sentence saying what happened.")
    # A short title drawn big above the headline, so a card skims (owner,
    # 2026-10-28: "a char limited title length with a subtitle").
    title = _s(card.get("title"))
    if not title:
        out.append(f"\"title\" is empty: the thing itself, a short name, at most {MAX_TITLE_CHARS} "
                   "characters, drawn big at the top of the card (\"Header image\").")
    elif len(title) > MAX_TITLE_CHARS:
        out.append(f"\"title\" is {len(title)} characters; the limit is {MAX_TITLE_CHARS}. "
                   "Cut it to the name of the thing.")
    out += title_path_problems(card)
    # A bare "cards" in the title: the app has sidebar cards, Workflow cards,
    # copy cards and these end-of-turn summaries.  "Cards say the problem
    # first" left the owner unsure which (2026-10-28).
    if title:
        bare = [m.group(0) for m in _CARD_WORD.finditer(title)
                if (title[:m.start()].split() or [""])[-1].lower().strip("-") not in _CARD_QUALIFIERS]
        if bare:
            out.append(f"\"title\" says \"{bare[0]}\" without saying which: VibeNode has sidebar cards, "
                       "Workflow cards and end-of-turn summaries. Name the one he sees, or what he "
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
        # A done card asks the user to Review it (session-verb.js).  The
        # owner, 2026-10-27, on a done card with only text: "YOU GAVE ME
        # NOTHING TO REVIEW, THIS IS A HORRIBLE SYSTEM FAILURE".
        v = card.get("validate") if isinstance(card.get("validate"), dict) else {}
        if not (_items(card.get("links")) or _items(needs.get("look"))
                or isinstance(card.get("compare"), dict) or _s(v.get("url"))):
            out.append("A \"done\" card asks the user to review the work, so give him something to "
                       "review: \"links\" to what changed (the page or preview to try, the commit, "
                       "the files), or a \"compare\" block showing before and after.")
        # The owner, 2026-10-28: "we need to force a validate row so i have a
        # way to check."  Test results are the session's check, not his.
        if not _s(v.get("how")):
            out.append("A \"done\" card needs \"validate\": {\"how\": \"what the user does to see it work "
                       "himself, one line\", \"url\": \"the page, preview or screen to try it on\"}. "
                       "Your test results are your check, not his: give him a way to check it.")
        # The owner's pick B, 2026-10-28 (static/scratch/validate-later.html):
        # a Validate row with no address looked like a button and did nothing.
        # It must always open something; when the real moment comes later, a
        # demo that shows it happening, and the real moment in "later".
        elif not _VALIDATE_URL.match(_s(v.get("url"))):
            out.append("validate.url must open something he can try NOW: the page or preview "
                       "(\"/static/...\", \"/api/preview/asset/<id>\" or \"http://localhost:...\"), "
                       "not a file path or nothing. If the real check only happens later (the next "
                       "time X occurs), build a demo page that shows it happening, put it in "
                       "validate.url, and say when the real moment is in validate.later.")
        elif is_app_home(v.get("url")):
            out.append(f"validate.url ({_s(v.get('url'))}) is VibeNode's front page, the app he is "
                       "already in: it opens nothing he can check. Link the exact place the change "
                       "shows (a deep link, a preview, a screenshot), or, when the real check only "
                       "happens later, build a demo page that shows it happening and say when the "
                       "real moment is in validate.later.")
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
        if not _s(needs.get("eta")):
            out.append("A \"working\" card needs needs.eta: when the user will hear back.")
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
                   "\"where it stands\", \"reply\": \"the message its Back to it button sends\"}.")
    out += finding_problems(card)
    out += plain_choice_problems(card)
    mock = mockup_picture_problem(card)
    if mock:
        out.append(mock)
    dup = duplicate_choice_problem(card)
    if dup:
        out.append(dup)
    like_now = screens_like_now_problem(card)
    if like_now:
        out.append(like_now)
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
        # The owner, 2026-10-28, on "Nothing needed. Reload VibeNode and every
        # card shows the new heading": a card that says nothing is needed and
        # then gives him a job.
        chore = _CHORE_IN_NOTHING.search(_norm(_s(needs.get("next")) + " " + _s(needs.get("why"))))
        if chore:
            out.append(f"needs.kind \"none\" says nothing is needed, but it tells the user to do something "
                       f"(\"{chore.group(0).strip()}\"). Do it yourself, or if only he can, use "
                       "\"needs_you\" with needs.kind \"action\", the steps and an \"I did it\" button.")
        waits = _WAITS_ON_USER.search(_norm(_s(needs.get("next")) + " " + _s(needs.get("why"))))
        if waits:
            out.append(f"needs.kind \"none\" says nothing is needed, but what happens next waits on the user "
                       f"(\"{waits.group(0).strip()}\") and the card gives him no button to answer. If he "
                       "already said what he wants, do it now. If it truly waits on his yes, end on "
                       "\"needs_you\" with needs.kind \"approve\" and a reply button for each answer; "
                       "on a question, the answer goes in \"answer\" on that same card.")
    else:
        if not _s(needs.get("ask")):
            out.append("needs.ask is empty: the one thing the user must decide or do.")
        if not _s(needs.get("why")):
            out.append("needs.why is empty: why this needs the user and not you.")
        replies = [r for r in _items(needs.get("replies")) if isinstance(r, dict)]
        if kind == "decision":
            if len(replies) < 2:
                out.append("A decision needs at least two reply buttons in needs.replies.")
            bare = [r for r in replies if len(_s(r.get("label")).split()) < 3]
            if bare:
                out.append("Every choice must be spelled out in its \"label\" (three words or more), "
                           "not a bare letter or name.")
            # Choices that carry their own screens are something to look at
            # (CustomerNode's trials, 2026-10-28: today's look is choice A, so a
            # page in needs.look would add a "Now" tab showing it twice).
            if (not _items(needs.get("look")) and not isinstance(card.get("compare"), dict)
                    and not any(_items(r.get("screens")) for r in replies)):
                out.append("A decision needs something to look at: a link in needs.look, a \"compare\" "
                           "block, or \"screens\" on its replies.")
            seen = visual_choice_problem(card)
            if seen:
                out.append(seen)
        elif kind == "approve":
            # An approval answered on its own page (CustomerNode's release
            # approval page, 2026-10-28) has no reply buttons: the user
            # answers on the page, opened full screen from needs.look, never
            # through the session.
            if not replies and not _items(needs.get("look")):
                out.append("An approval needs reply buttons in needs.replies, or, when it is "
                           "answered on its own page, that page in needs.look.")
        elif kind == "action":
            if not _items(needs.get("steps")):
                out.append("An action needs needs.steps: exactly what the user does.")
            if not replies:
                out.append("An action needs a reply button the user taps when it is done.")
    return out


# ── A home for findings, and a yes-or-no the user can picture ────────────
# The owner, 2026-10-28, on an Approve card that asked "Should Claude Auto in
# VibeNode refuse the Windows delete-everything commands (del /s /q, rmdir
# /s /q, Remove-Item -Recurse -Force) like rm -rf?": "I don't know what you
# are talking about."  He had asked for a test cleanup; the test found a
# permission gap the session may not fix alone.  The rules gave that finding
# no home: a done card may not mention it (UNFINISHED), so the only way
# through was a Decide/Approve, and that question came out in code names and
# raw command syntax, with no recommendation, about a thing he never raised.
# Three checks:
#   1. "finding": a sanctioned place for something noticed outside his ask
#      (what, risk, recommend), drawn as a quiet "Also found" band with one
#      button.  It is neither unfinished work nor a decision.
#   2. plain_choice_problems: a Decide/Approve question and its buttons carry
#      no command syntax, flags or code names, and one reply is marked
#      "recommended" (a pick between mockups is his taste: exempt).
#   3. handoff_evidence.reviewer_problems: an Approve, like a Decide, must be
#      tied to something he asked for, in his words.
FINDING_KEYS = ("what", "risk", "recommend")


def finding_problems(card: dict) -> list[str]:
    f = card.get("finding")
    if f is None:
        return []
    shape = ("\"finding\" is {\"what\": \"what you noticed, one line\", \"risk\": \"what it risks "
             "for the user\", \"recommend\": \"what you recommend\", \"reply\": \"optional: the "
             "message its Deal with it button sends\"}: something outside his ask that you may "
             "not act on alone. It is not unfinished work and not a decision.")
    if not isinstance(f, dict):
        return [shape]
    missing = [k for k in FINDING_KEYS if not _s(f.get(k))]
    if missing:
        return [shape + " Missing: " + ", ".join(missing) + "."]
    return []


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
                   "name may go in a reply's \"detail\", in context, or in links.")
    # A pick between mockups is his taste; every other choice comes with the
    # session's recommendation, so a yes-or-no is never handed over bare.
    picks = shows_variants(card) or any(_items(r.get("screens")) for r in replies)
    if replies and not picks:
        rec = [r for r in replies if r.get("recommended") is True]
        if not rec:
            out.append("Mark the reply you recommend with \"recommended\": true (exactly one). The user "
                       "reads the question between many sessions; the card says which way you would go "
                       "and why (in needs.why), so he can agree in one tap or disagree knowingly.")
        elif len(rec) > 1:
            out.append("More than one reply is marked \"recommended\"; mark exactly one.")
    return out


# ── Layer 1a: handing over work the session could do ─────────────────────
# The owner, 2026-10-28, on a ChatGPT cleanup session that found no browser
# tools and ended on "open a terminal, run claude --chrome, type: organize my
# ChatGPT chats": "this looks like a session that should be taking action ...
# fix smart termination so this type of error does not occur again, not just
# the specific case".  The class: a session that hits a missing tool (or a
# command it could run) and hands the job to him instead of finding another
# way.  Two checks, on the card alone so the settings-file copy runs them too:
#   1. a step that gives him a command to run, a terminal to open, or a new
#      session to start is work with a shell behind it: the session runs it;
#   2. a "needs_you" or "blocked" card that gives up on a missing ability
#      ("I can't reach", "this session has no browser") must show at least
#      two other ways it tried and why only he can do it.
# Steps only a person can do (a login, a password or code, an approval on
# his phone, restarting the Session Engine, hands-on work) pass.
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
                   "run the command, write the script, or call the tool. Never send him to a terminal or "
                   "to start another session. If a step truly needs him (a login, a password or code, "
                   "an approval only he can give), say that in the step.")
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
                       "...}} and say in needs.only_you why only he can do the rest. If one works, do the "
                       "work instead.")
        if gap and t == "needs_you" and needs.get("kind") == "action" and not _s(needs.get("only_you")):
            if len(_tried(card)) >= MIN_TRIED:
                out.append("needs.only_you is empty: say why only the user can do this step, now that "
                           "the other ways failed.")
    return out


# ── Layer 1b: length ──────────────────────────────────────────────────────
# The owner, 2026-10-27, on a 600-word needs_you card: "this response is way
# too fucking long, fix the system to not allow this".  The card is read at a
# glance across many sessions.  The library's own good examples run 44-90
# words with no line over 18; the cards that drew the complaint ran 500+.
# Not counted: the deliverable (the text itself is the point) and "asks" (the
# user's own words); reply texts, urls and commands are already skipped by
# _strings().
MAX_WORDS = 120
MAX_HEADLINE_WORDS = 20
MAX_TITLE_CHARS = 32
_CARD_WORD = re.compile(r"\bcards?\b", re.I)
_CARD_QUALIFIERS = {"sidebar", "session", "summary", "end-of-turn", "workflow", "task", "copy",
                    "compose", "section", "job", "decide", "credit", "gift", "business", "index"}
MAX_LINE_WORDS = 25

# The title names the thing and the path is the way to it; the headline says
# what happened (owner, 2026-10-28, static/scratch/card-title-crumb.html:
# "the subtitle is the sentence ... the title becomes the last crumb").  A
# name cannot be a fragment, an order or a session nickname in disguise, and
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
        out.append(f"\"path\" has {len(steps)} steps; at most {MAX_PATH_STEPS}. Keep the ones he needs to "
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
_LONG_LISTS = ("asks", "links", "needs.look")
# A decision reply's template fields (owner's pick, 2026-10-28,
# static/scratch/decide-motion.html) are drawn only inside the full-screen
# Decide popup, one choice at a time, never on the card, so they are not
# counted toward the card's length or its list limit.  Each line still has
# the per-line cap.
_POPUP_ONLY = re.compile(r"^needs\.replies\[\d+\]\.(?:strengths|weaknesses|opportunities|risks|fit|effort|undo|risk|screens)\b")


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


def length_problems(card: dict) -> list[str]:
    """What makes the card too long to read at a glance."""
    total = 0
    long_lines = []
    for where, text in _strings(card):
        # zoom: smart_termination.zoom_problems; explained: the "Explain more"
        # text, long on purpose (smart_termination.explained_problems);
        # full_answer: the answer's "Full" version, behind the Answer band's
        # switch and long on purpose (owner's pick C, 2026-10-28,
        # static/scratch/answer-depth.html).
        # finding: the "Also found" band, three capped lines outside the 120
        # (finding_problems); the per-line cap below still applies.
        if where.startswith(("deliverable", "asks", "zoom", "explained", "full_answer")):
            continue
        if where.startswith("finding"):
            if n_f := _words(text):
                if n_f > MAX_LINE_WORDS:
                    long_lines.append(f"{where} is {n_f} words (limit {MAX_LINE_WORDS})")
            continue
        n = _words(text)
        if _POPUP_ONLY.match(where):
            if n > MAX_LINE_WORDS:
                long_lines.append(f"{where} is {n} words (limit {MAX_LINE_WORDS})")
            continue
        total += n
        cap = (MAX_HEADLINE_WORDS if where == "headline"
               else MAX_ANSWER_WORDS if where == "answer" else MAX_LINE_WORDS)
        if n > cap:
            long_lines.append(f"{where} is {n} words (limit {cap})")
    long_lists = [f"{where} has {len(items)} items (limit {MAX_LIST_ITEMS})"
                  for where, items in _lists(card)
                  if len(items) > MAX_LIST_ITEMS
                  and not where.startswith(("deliverable",) + _LONG_LISTS)
                  and not _POPUP_ONLY.match(where)]
    if total <= MAX_WORDS and not long_lines and not long_lists:
        return []
    out = [f"Too long: the card is {total} words; the limit is {MAX_WORDS}. The user reads it "
           "at a glance between many sessions. Keep what happened, where it stands and what he "
           "must do; cut explanation, background and anything he does not need to act. "
           "If he asked for the long text itself (an email, a report, a detailed "
           "explanation), use type \"deliverable\": its body has no limit."]
    out += long_lines[:5] + long_lists[:3]
    return out


# ── Machinery words where the user looks first ───────────────────────────
# The owner, 2026-10-28, on a card titled "Card rules load live" whose
# headline said the "Session Engine was checking cards with stale rules":
# "this descended into technical jargon rather than the problem at hand".
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
    # task / Just now (the owner, 2026-10-28, confused by "zoom lines capped")
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
            "headline, zoom lines and ask say what the user sees, asked or gets, in his words; the "
            "machinery goes in context or links. \"Card rules load live\" becomes \"Why my card "
            "skipped the title rule\"."]


# A choice named by its key where the user skims (owner, 2026-10-28, on a done
# card titled "Header A, more frustrated": "got a dogshit title talking by
# variant name, should be illegal").  The key ("A", "Layout 4") meant
# something on a mockup page hours ago; read between ten other sessions it
# names nothing.  Unlike _LABEL, an explanation after it does not excuse it:
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
VARIANT_FIELDS = MACHINERY_FIELDS


def variant_name_problems(card: dict) -> list[str]:
    needs = card.get("needs") if isinstance(card.get("needs"), dict) else {}
    zoom = card.get("zoom") if isinstance(card.get("zoom"), dict) else {}
    fields = {"path": _path_text(card), "title": card.get("title"), "headline": card.get("headline"),
              "zoom.big": zoom.get("big"), "zoom.task": zoom.get("task"), "zoom.now": zoom.get("now"),
              "needs.ask": needs.get("ask")}
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
            "path, title, headline, zoom lines and ask never call a variant by its key, even with an "
            "explanation after it: the user no longer has the options in front of him. Name what "
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
 "links": [{"label": "...", "kind": "link|file|commit", "url": "..."}],
 "validate": {"how": "what the user does to see it work himself", "url": "the page to try it on",
              "later": "only when the real moment is later: when it happens for real"},
 "needs": {"kind": "none | decision | approve | action",
           "ask": "...", "why": "...", "next": "...", "eta": "...",
           "look": [{"label": "...", "kind": "link", "url": "..."}],
           "steps": [{"text": "...", "command": "..."}],
           "replies": [{"key": "A", "label": "the choice spelled out", "detail": "...", "reply": "...",
                        "recommended": true}]},
 "finding": {"what": "something you noticed outside his ask", "risk": "what it risks for him",
             "recommend": "what you recommend", "reply": "optional: what its button sends"}}
```

Which card to end on: go by what the user's last message was, then by what \
is true now.
- He asked a question → "explain" with the answer in "answer" ("deliverable" \
when it is long). Never a Decide card, even if you have something to offer, \
with one exception: mockups of something he asked for are on screen. Then \
it is a decision with a reply per variant, and the answer goes in "answer".
- He asked for a text (an email, notes, a plan) → "deliverable".
- He asked for a file (a download, a zip, a PDF, a deck) → "deliverable" \
with "files".
- He gave an order, and it is finished and checked → "done".
- He gave an order, and a choice that only he can make blocks it → \
"needs_you" with needs.kind "decision", tied to that order in "asks" (state \
"needs_you"). One decision, the one that blocks the work.
- Only he can do the next step (log in, restart, approve) → "needs_you" with \
"action" or "approve".
A missing tool or ability is never a reason to hand him the job. Find \
another way first: write a script, use a library (Playwright, requests), \
call the service's API or the app's own routes, or use a different tool. A \
step that sends him to a terminal, gives him a command to run, or has him \
start another session is sent back: you have a shell, so run it. If at \
least two other ways truly failed, an action card lists them in \
needs.tried ([{"what": "...", "result": "..."}]) and says in \
needs.only_you why only he can do the rest; a card that says "I can't" \
without them is sent back.
- Your own background work is still running → "working". Something outside \
your control stops you → "blocked".
- His message starts "Wrong kind of card:" → he tapped "Redo as…" on your \
last card. End this reply on the kind he names, doing whatever work that kind \
needs (a Done means doing the work, a Decide means showing the options). \
Another kind is sent back.
- Work he asked for waits only on his yes (he asked a question about it, or \
must OK a step) → "needs_you" with needs.kind "approve" and a button per \
answer; on a question the answer goes in "answer" on that card. If he \
already said what he wants, do it instead of asking.
An idea, an improvement or a follow-up you would like to offer is never a \
decision: name it in needs.next and let him ask for it. On an "explain" or \
"deliverable" card put it in "offer" instead, a few words naming what you \
would do ("Give answer cards their own buttons"): it is drawn as a "Do what \
it suggested" button that sends "Yes, do that". A "none" card's next \
never waits on him ("once you confirm", "when you say go"): such a card is \
sent back.
Something you NOTICED outside his ask and may not act on alone (a permission \
rule, a security gap, a cost) goes in "finding": {"what", "risk", \
"recommend"}, drawn as a quiet "Also found" band with one button. It is \
neither unfinished work nor a decision, so a done card may carry it, and it \
is never turned into a Decide/Approve: a decision or approval must be tied to \
something HE asked for, in his words, in "asks" (state "needs_you"); one that \
is not is sent back.
A Decide or Approve question is a yes-or-no he can picture. Its ask, why, \
next and button labels name the outcome in plain words ("stop a session from \
wiping a folder on Windows, as it is already stopped on Mac"), never command \
syntax, flags or code names ("del /s /q", "--force", "chrome_control"): those \
go in a reply's "detail", in context or in links. Mark the reply you \
recommend with "recommended": true (exactly one), and say why in needs.why; \
a pick between mockups is his taste and needs no recommendation.

Types:
- done: everything asked for is finished and checked. Nothing in remaining or \
broken, no failed check, needs.kind "none" with why and next. It must give \
the user something to review: "links" to what changed (the page to try, the \
commit, the files) or a "compare" block showing before and after. It must \
have "validate": one line on how the user checks it works himself, and in \
"url" the page, preview or screen he opens to try it NOW. When the real check \
only happens later (the next time something occurs), build a demo page that \
shows it happening, link it in "url", and put the real moment in "later". A \
Validate row with nothing to open, or one that only opens VibeNode's front \
page (the app he is already in), is sent back.
- needs_you: only the user can do it: a decision that is theirs (each choice \
spelled out, plus a link to look at), an approval, or an action such as a \
login (steps plus an "I did it" button). A choice about how something looks \
or is presented (a design, layout, style, slides, a deck) must SHOW every \
option before you ask: a scratch page of clickable mockups in needs.look and \
each reply's "screens" pointing at its own section of that page \
("<page>.html#<id>"), shown live like the Now tab, never a screenshot. A file \
to read is not a preview; such a card is sent back.
- working: idle on purpose while YOUR background work runs. needs.eta is required.
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
text to paste). "body" is then optional. Never hand a file over as a Validate row or a \
link: such a card is sent back.
- explain: an answer to a question, in "answer" (or a "compare" block).
EVERY card, of every type, reads the same: the title, the headline, then one \
switch with three versions, Short / Plain words / Full. "answer" is Short: \
two or three sentences that go one level past the headline, never a repeat \
of it (on an explain card, the answer; on any other, what happened or what \
is asked). "explained" is Plain words. "full_answer" is Full: everything \
behind it in markdown (## headings, - lists, tables), no length limit. On a \
deliverable with a "body", that body is its Full, so it needs no \
full_answer. A card missing one of the three is sent back.
When earlier work in this session is still unfinished and this card is \
about something else, add "back_to": {"label": "that work, in a few words", \
"status": "where it stands", "reply": "the message to send to resume it"}. \
The card draws it as a "Left waiting" strip with a Back to it button; do \
not also put it in needs.next.

Name the thing, then say what happened to it. Picture him switching in \
with no memory of this session: "path" and "title" tell him WHERE, the \
headline tells him WHAT. The app already shows the project, so neither \
names it.
- "title" is the thing itself, a short name drawn big: at most 5 words and at most \
32 characters, no verb needed, no ":" "," ";" "." "?" "!" ("Header image", \
"Session right-click menu", "Top bar"). Never a sentence, an order, a \
question or a fragment ("A more frustrated person", "Pick a header", "Why \
Done woke up").
- "path" is the way to it, one to three short names from broad to specific, \
drawn small above the title: ["Article series", "Gen vs Validation \
Leverage"] above "Header image"; ["Sidebar"] above "Session right-click \
menu". Use names he would recognise: his own words, an area of the app, a \
document's title. Never the project, a choice letter or a name you made up. \
Keep its first step the same all session unless his message starts a new \
topic.
- The headline is the sentence: what happened to the thing, or what he \
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
what HE asked, sees or gets, in his words, never the name of your change or \
the machinery you touched (engine, hook, loader, module, cache, config, \
stale...: those go in context or links). If his last message asks a \
question, answer it first in "answer" (one or two plain sentences), as type \
"explain" (or "deliverable" when long) with needs.kind "none": never turn \
an answer into a decision or add a new choice to it (unless mockups he asked \
for are on screen: those always get a reply per variant). Not \
"Card rules load live" / "Engine reloads card rules on change", but "Why my \
card skipped the title rule" / "The checker ran an old copy of the rules, so \
my untitled card got through". Call things by the names he sees on screen, \
not your field names, and say his problem before your fix: not "Zoom lines \
capped at 40 characters" but "The lines under each card title wrapped to \
three lines on the phone; now one or two". The title is his report, not \
yours: what he gets, named so he knows which thing without reading on \
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
"""

_REWRITE = "Rewrite only your final message unless the problem is unfinished work; then do the work first."


# Every picture and page a summary points at on this app must load (2026-10-30:
# a session linked two pictures as /api/preview/asset/<upload name>, an address
# that only serves screenshots the preview tool rendered; on the owner's iPhone
# both opened to nothing, and nothing had checked them).  Only this app's own
# addresses are checked, against the files that serve them: /static/... must be
# a file under static/, /api/preview/asset/<id> a rendered screenshot.  Other
# addresses are not fetched (that would slow every summary).
_STATIC_DIR = Path(__file__).resolve().parents[1] / "static"
_PREVIEW_DIR = Path(__file__).resolve().parents[1] / "data" / "previews"
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
    d = card.get("deliverable")
    if isinstance(d, dict):
        for i, f in enumerate(_items(d.get("files"))):
            if isinstance(f, dict):
                yield f"deliverable.files[{i}]", _s(f.get("url"))


def _app_address_missing(url: str) -> bool:
    """True when *url* is one of this app's own addresses and nothing serves it."""
    u = re.sub(r"^https?://(?:localhost|127\.0\.0\.1)(?::5050)?(?=/)", "", url or "", flags=re.I)
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
    bad = [f"{where} ({url})" for where, url in _card_urls(card) if url and _app_address_missing(url)]
    if not bad:
        return []
    return ["These addresses open to nothing, so the user taps them and sees a blank: "
            + "; ".join(bad[:4]) + ". Link only what exists. A screenshot of a page: render it with "
            "POST /api/preview/render and use the \"src\" it returns. A picture you already have (an "
            "upload, a saved image): copy it into static/scratch/ and link /static/scratch/<name>."]


def review(message: str, facts=None) -> Optional[str]:
    """The reason to send the session back, or None when the card passes.

    ``facts``: a ``handoff_evidence.TurnFacts`` for this session (its tool
    calls and the user's requests in this task).  With it, the card's claims
    are checked against what the session did, and the card must account for
    every request (``daemon/handoff_evidence.py``).  Without it, those checks
    are skipped.

    Caller decides when this applies (preference on, a session that talks to
    a person, not an error or usage-limit stop, send-back cap not reached).
    """
    card, prose, err = extract(message)
    if err:
        return ("Smart termination: " + err + " End every turn with one ```" + FENCE_LANG
                + " card as the last thing in your message.\n\n" + INSTRUCTIONS)
    problems = (format_problems(card) + download_problems(card) + handover_problems(card)
                + length_problems(card) + machinery_problems(card) + variant_name_problems(card)
                + broken_link_problems(card))
    hits = _checks(card, prose)
    labels = label_problems(card, prose)
    from daemon import handoff_evidence
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
