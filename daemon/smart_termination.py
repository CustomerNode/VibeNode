"""Smart termination: a session ends every turn with a handoff the user can
act on, and thinks again before it stops with unfinished work.

The owner asked for this on 2026-10-27.  Sessions went idle on messages that
assumed he had read every intermediate message ("Pick B or C on the board to
finish point 5"), answered a task notification with a bare "nothing needed
from you" on a major open issue, or listed "open issues" and "honest caveats"
for work they had simply decided to stop doing.  He runs many sessions at
once; when one goes idle he needs to see, every time:

    What we're working on:
    Where it stands:
    What's needed from you:

This module is backend-neutral and pure: the instruction text appended to
every session's system prompt, and ``review()``, which looks at the final
message of a turn and returns the reason to send the session back (or None
to let the turn end).  The backend wires ``review`` into its turn-end hook;
SessionManager decides whether the preference is on and whether the session
is one that talks to a person.

Preference: ``smart_termination`` in kanban_config.json (System →
Preferences → "Smart termination"), opt-in (off unless set to true).
"""

from __future__ import annotations

import re
from typing import Optional

PREF_KEY = "smart_termination"

#: How many times one stop may be sent back.  The CLI has its own cap (8
#: consecutive blocks); this one is lower so a session that cannot comply
#: costs the user at most two short extra replies.
MAX_SENDBACKS = 2

# The approved last-message card (docs/plans/last-message-cards-spec.md):
# the schema, the types and the banned-phrase rules live in
# daemon/handoff_rules.py; this module adds the guards around them.
from daemon import handoff_rules as _rules

# Zoom lines are capped in characters, not words (the owner, 2026-10-28: "a
# char limit that fits the lines").  Lines of 74-85 characters wrapped to
# three lines on a phone; 40 fits one line on desktop and most phones and
# never more than two (measured in Chrome with last-card.css, 340-700px cards).
ZOOM_MAX_CHARS = 40

# "Explain more" (owner's pick B of static/scratch/card-lost.html, 2026-10-28:
# "im thinking a im lost or confused on what this is telling me button with a
# more detailed version with less conciseness constraints").  Every card
# carries the long version, so it opens instantly, even on a sleeping session.
EXPLAINED_MIN_WORDS = 40

INSTRUCTIONS = _rules.INSTRUCTIONS + (
    "\nYour final message is the card ALONE: no text before or after it. The "
    "user reads only the card; anything you want to say goes inside it.\n"
    "Every card also has \"zoom\": {\"big\": \"what the whole session is for\", "
    "\"task\": \"the piece of work right now\", \"now\": \"what happened this turn\"}, "
    f"one line each, at most {ZOOM_MAX_CHARS} characters, written for someone who read nothing else. "
    "\"big\" is how the user tells this session apart from his others: keep it the "
    "same on every card unless he starts a new topic. \"task\" is his latest ask in "
    "his words; \"now\" is what changed for him.\n"
    "Every card also has \"explained\": the same card written out for someone who is "
    "lost, drawn behind an \"Explain more\" button under the zoom lines. Markdown with "
    "short ## headings: what this is about, what happened and why, what it means for "
    "him, and what (if anything) he does next. Plain words, his names for things, "
    f"no file names; at least {EXPLAINED_MIN_WORDS} words and no upper limit. It is "
    "not counted in the card's 120 words.\n"
    "An approval that is answered on its own page (a release approval page): "
    "type needs_you, needs.kind approve, the page in needs.look, and NO replies. "
    "The user answers on the page; never answer it for them.\n"
    "Screenshots and previews go inside the card as well: a \"links\" entry "
    "{\"label\": \"what it shows\", \"kind\": \"image\", \"url\": \"/api/preview/asset/<id>\"} "
    "is drawn as a picture in the card; on a card with a Validate row it is drawn inside "
    "Validate, under the row to try. Never put a VN-PREVIEW marker in the final message.\n"
    "Mockups are SHOWN, never only linked: a card that links a mockup page "
    "(static/scratch/) carries a screenshot of each option as a \"kind\": \"image\" "
    "link (a decision may use its replies' \"screens\" instead).\n"
    "A decision's replies may also fill the Decide popup's templates, drawn one "
    "choice at a time and not counted in the card's length. A design choice: "
    "\"strengths\", \"weaknesses\", \"opportunities\", \"risks\" (lists of short "
    "lines), \"effort\" (none|small|medium|large), \"undo\" (easy|medium|hard), "
    "\"risk\" (low|medium|high), \"fit\" (\"Pick this if\" + the rest). A UI choice: "
    "\"screens\": [{\"label\": \"...\", \"url\": \"/static/scratch/<page>.html#a\", "
    "\"device\": \"web|phone|both\"}], pages this app can show; the user flips "
    "Web, Phone or Both. Use them whenever the choice is a design, UI or "
    "presentation call (slides, a deck, a page): build the scratch page first, "
    "one mockup per option tagged data-option=\"<key>\", so he picks by looking.\n"
    "A project whose own instructions show design options in its real app "
    "(CustomerNode's design trials) does that instead of a scratch page here: each "
    "screen's \"url\" is that app's live page on this machine "
    "(\"http://127.0.0.1:<port>/...\", shown signed in on this computer) and its "
    "\"image\" is its picture for the phone, which cannot reach it: "
    "{\"web\": {\"light\": \"<png>\", \"dark\": \"<png>\"}, \"phone\": {...}}.\n")

# The owner, 2026-10-27: "I never want to see a wall of text outside one of my
# approved formats again."  Text above the card is sent back.
OUTSIDE_CARD = ("Your final message has text outside the ```handoff card. The user "
                "reads only the card: move everything into it (headline, status, "
                "links, details) and send the card alone, with nothing before or after it. "
                "A screenshot or preview goes inside the card too: a \"links\" entry "
                "{\"label\": \"...\", \"kind\": \"image\", \"url\": \"/api/preview/asset/<id>\"}, "
                "never a VN-PREVIEW marker.")

# ── Parsing ──────────────────────────────────────────────────────────────

# The three headings, tolerant of markdown decoration (**, ##, >, -),
# curly apostrophes and a missing colon.
_H_WORK = r"what\s+(?:we\s*'?\s*re|we\s+are|i\s*'?\s*m|i\s+am)\s+working\s+on"
_H_STANDS = r"where\s+(?:it|things|we|this)\s+stands?"
_H_NEEDS = r"what\s*'?\s*s\s+needed\s+from\s+you|what\s+is\s+needed\s+from\s+you|needed\s+from\s+you"

_HEADING = r"^[ \t>*_#\-]*(?:\*\*|__)?[ \t]*(?:{h})[ \t]*(?:\*\*|__)?[ \t]*:?[ \t]*(?:\*\*|__)?"
_RE_WORK = re.compile(_HEADING.format(h=_H_WORK), re.I | re.M)
_RE_STANDS = re.compile(_HEADING.format(h=_H_STANDS), re.I | re.M)
_RE_NEEDS = re.compile(_HEADING.format(h=_H_NEEDS), re.I | re.M)


def _norm(text: str) -> str:
    return (text or "").replace("’", "'").replace("‘", "'").replace("\r\n", "\n")


def handoff_sections(message: str) -> Optional[dict]:
    """``{"work", "stands", "needs"}`` bodies of the LAST handoff block in
    *message*, or None when any heading is missing or out of order."""
    text = _norm(message)
    needs = list(_RE_NEEDS.finditer(text))
    if not needs:
        return None
    n = needs[-1]
    stands = [m for m in _RE_STANDS.finditer(text) if m.start() < n.start()]
    if not stands:
        return None
    s = stands[-1]
    work = [m for m in _RE_WORK.finditer(text) if m.start() < s.start()]
    if not work:
        return None
    w = work[-1]
    return {
        "work": text[w.end():s.start()].strip(),
        "stands": text[s.end():n.start()].strip(),
        "needs": text[n.end():].strip(),
    }


# "Nothing" with no why / what next / when.
_NOTHING = re.compile(r"^\W*(?:nothing|none|n/?a|no action)\b", re.I)

# A label from earlier in the session used as if the reader knew it: "option C",
# "point 5", "step 3b", "F2", "B or C".  Explained when followed by ( : = or a dash.
_LABEL = re.compile(
    r"\b(?i:option|options|point|step|phase|task|item|variant|choice|stage|part|"
    r"fix|issue|finding|trial)\s+#?(?:[A-Z]|\d+[a-z]?)\b(?!\s*[(:=—–-])"
    r"|\b(?![AI]\b)[A-Z]\s+or\s+(?![AI]\b)[A-Z]\b(?!\s*[(:=—–-])"
)

# Words a session uses when it is stopping with work left over.
_UNFINISHED = re.compile(
    r"\b(?:not\s+yet|haven't|have\s+not|didn't\s+(?:get\s+to|finish|run|test|verify|fix|check)"
    r"|still\s+(?:need|needs|to\s+do|open|broken|fail|failing|missing|unresolved)"
    r"|remaining\s+(?:work|issues?|items?|steps?)|left\s+to\s+do|open\s+(?:issues?|questions?|items?)"
    r"|known\s+(?:gaps?|issues?|limitations?)|caveats?|untested|unverified|not\s+verified"
    r"|not\s+tested|TODO|follow[\s-]?ups?|next\s+steps?|deferred|skipped|out\s+of\s+scope"
    r"|not\s+(?:fixed|done|finished|implemented|wired)|is\s+failing|are\s+failing|still\s+fails)\b",
    re.I,
)

# An API error or usage-limit stop: sending it back would only hit the error again.
_ERROR_STOP = re.compile(
    r"^\s*(?:API\s+Error|Error:)|you'?ve\s+hit\s+your\b|\blimit\b.{0,40}\bresets?\b",
    re.I | re.S,
)


def unexplained_labels(text: str) -> list:
    return sorted({m.group(0).strip() for m in _LABEL.finditer(_norm(text))})


def unfinished_signals(text: str) -> list:
    seen, out = set(), []
    for m in _UNFINISHED.finditer(_norm(text)):
        w = m.group(0).lower()
        if w not in seen:
            seen.add(w)
            out.append(m.group(0))
    return out


# ── Decision ─────────────────────────────────────────────────────────────

_FORMAT_HELP = (
    "End your final message with this block, written for someone who has read "
    "nothing else in this session:\n\n"
    "**What we're working on:** one or two plain sentences on the goal.\n"
    "**Where it stands:** what is done, what is not, anything broken.\n"
    "**What's needed from you:** the exact thing to do or look at (link, file "
    "path or card, plus the decision with every choice spelled out), or "
    "\"Nothing right now\" plus why, what happens next, and when the user will "
    "hear back.\n\n"
    "Rewrite only your final message; do not redo any work."
)


def review(message: str, *, continued: bool, sendbacks: int,
           background_in_flight: bool = False, facts=None) -> Optional[str]:
    """The reason to send the session back, or None to let the turn end.

    The final message must end with the approved ```handoff card
    (``daemon/handoff_rules.review``).  ``continued``/``background_in_flight``
    are accepted for the callers' sake; the card rules apply either way (a
    session waiting on its own work writes a "working" card).  Never more
    than ``MAX_SENDBACKS`` per stop, never on an error or usage-limit stop.
    """
    if message is None or sendbacks >= MAX_SENDBACKS:
        return None
    text = _norm(message).strip()
    if not text or _ERROR_STOP.search(text[:400]):
        return None
    card, prose, err = _rules.extract(text)
    if not err and prose.strip():
        return "Smart termination: " + OUTSIDE_CARD
    reason = _rules.review(text, facts)
    zoom = zoom_problems(card) if not err else []
    expl = explained_problems(card) if not err else []
    if not zoom and not expl:
        return reason
    note = "\n\n".join(([("Zoom levels:\n" + "\n".join("- " + z for z in zoom))] if zoom else [])
                       + (["Explain more:\n" + "\n".join("- " + x for x in expl)] if expl else []))
    if reason:
        return reason + "\n\n" + note
    return "Smart termination: your handoff card was sent back.\n\n" + note


# Big picture / this task / just now (the owner picked "three lines under
# the title", 2026-10-28).  Every card carries them; the renderer draws them
# right under the headline (static/js/last-card.js).
ZOOM_KEYS = (("big", "Big picture: what the whole session is for"),
             ("task", "This task: the piece of work right now"),
             ("now", "Just now: what happened this turn"))


def zoom_problems(card) -> list:
    if not isinstance(card, dict):
        return []
    z = card.get("zoom")
    if not isinstance(z, dict):
        return ['Add "zoom": {"big": "...", "task": "...", "now": "..."}: three one-line '
                "answers, big picture (the whole session's goal), this task, and just now "
                "(what happened this turn)."]
    out = []
    for key, what in ZOOM_KEYS:
        v = z.get(key)
        v = v.strip() if isinstance(v, str) else ""
        if not v:
            out.append(f'zoom.{key} is empty ({what}).')
        elif len(v) > ZOOM_MAX_CHARS:
            out.append(f"zoom.{key} is {len(v)} characters (limit {ZOOM_MAX_CHARS}). "
                       "Name the thing in a few words; drop the how and the why.")
    return out


def explained_problems(card) -> list:
    """The card's "Explain more" text: required on every card, long enough to
    say more than the card itself."""
    if not isinstance(card, dict):
        return []
    v = card.get("explained")
    v = v.strip() if isinstance(v, str) else ""
    if not v:
        return ['Add "explained": the same card written out for someone who is lost, in '
                "markdown with short ## headings (what this is about, what happened and why, "
                "what it means for him, what he does next), plain words, no length limit. "
                "The card shows it behind an \"Explain more\" button."]
    n = len(v.split())
    if n < EXPLAINED_MIN_WORDS:
        return [f'"explained" is {n} words; it must be at least {EXPLAINED_MIN_WORDS}. It is the '
                "long version for when the card is not enough: say what the card leaves out."]
    return []
