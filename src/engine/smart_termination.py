"""Smart termination: a session ends every turn with a handoff the user can
act on, and thinks again before it stops with unfinished work.

Without it, sessions go idle on messages that assume the reader saw every
intermediate message, answer a task notification with a bare "nothing needed"
on a major open issue, or list "open issues" for work they simply stopped
doing.  Someone running many sessions at once needs to see, every time one
goes idle:

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

import json
import os
import re
import time
from pathlib import Path
from typing import Optional

PREF_KEY = "smart_termination"

#: How many times one stop may be sent back.  The CLI has its own cap (8
#: consecutive blocks); this one is lower so a session that cannot comply
#: costs the user at most two short extra replies.
MAX_SENDBACKS = 2

#: Past MAX_SENDBACKS, a summary is still sent back while it cannot be used
#: at all (no type, a question with no answer buttons), up to this many
#: send-backs in total; then it is let through and flagged on screen.  Below
#: the CLI's own cap of 8 consecutive blocks, so a session still never loops.
HARD_CEILING = 5

# The last-message card: the schema, the types and the banned-phrase rules live in
# src/engine/handoff_rules.py; this module adds the guards around them.
from engine import handoff_rules as _rules

# Zoom lines are capped in characters, not words, so they fit.  Lines of 74-85 characters wrapped to
# three lines on a phone; 40 fits one line on desktop and most phones and
# never more than two (measured in Chrome with last-card.css, 340-700px cards).
ZOOM_MAX_CHARS = 40

# "Explain more": a more detailed version with looser length limits.  Every
# card carries the long version, so it opens instantly, even on a sleeping session.
EXPLAINED_MIN_WORDS = 40

def _check_command() -> str:
    """The self-check's command line (src/engine/handoff_check.py), with absolute
    forward-slash paths in double quotes so bash, cmd and PowerShell all run it
    from any project folder.  python.exe, never pythonw.exe: the check prints."""
    import sys as _sys
    from pathlib import Path as _P
    exe = _P(_sys.executable or "python")
    if exe.name.lower() == "pythonw.exe" and (exe.parent / "python.exe").exists():
        exe = exe.parent / "python.exe"
    script = _P(__file__).resolve().parent / "handoff_check.py"
    return '"%s" "%s"' % (exe.as_posix(), script.as_posix())


_CHECK_COMMAND = _check_command()

INSTRUCTIONS = _rules.INSTRUCTIONS + (
    "\nYour final message is the card ALONE: no text before or after it. The "
    "user reads only the card; anything you want to say goes inside it.\n"
    "Every card also has \"zoom\": {\"big\": \"what the whole session is for\", "
    "\"task\": \"the piece of work right now\", \"now\": \"what happened this turn\"}, "
    f"one line each, at most {ZOOM_MAX_CHARS} characters, written for someone who read nothing else. "
    "\"big\" is how the user tells this session apart from the user's others: keep it the "
    "same on every card unless the user starts a new topic. \"task\" is the user's latest ask in "
    "the user's words; \"now\" is what changed for the user.\n"
    "Every card also has \"explained\": the same card written out for someone who is "
    "lost, drawn as the \"Plain words\" version in the card's Short / Plain words / "
    "Full switch. Markdown with "
    "short ## headings: what this is about, what happened and why, what it means for "
    "the user, and what (if anything) the user does next. Plain words, the user's names for things, "
    f"no file names; at least {EXPLAINED_MIN_WORDS} words and no upper limit. It is "
    "longer than Short and keeps every fact in it; explain each insider term the "
    "first time (a code, a check name, a project word) instead of repeating it. It is "
    "not counted in the card's 120 words: never shorten it to fit.\n"
    "An approval that is answered on its own page (a release approval page): "
    "type needs_you, needs.kind approve, the page in needs.look, and NO replies. "
    "The user answers on the page; never answer it for them. The page is the "
    "only thing in needs.look: no screenshot of it and no picture of the process "
    "(a picture stays only when the approval is about how something looks). The "
    "page already holds the details, so Short says in one or two sentences what "
    "goes live and what the user will notice after; leave version numbers, commit "
    "lists and build steps to Full.\n"
    "Review & respond holds ONE main thing. Screenshots are drawn only in Validate: "
    "beside the page to try (\"Try it yourself / OR just look\") or alone (\"Just look\"). "
    "The page to try (validate.url) is never a screenshot: it is where the user does the thing "
    "the user asked for, so a picture there is sent back. "
    "A card that asks the user something (Decide, Approve, To do) or hands the user a deliverable "
    "draws no Validate and no screenshots, so add none; a picture of a choice goes in "
    "that reply's \"screens\"; an Approve of something to see puts ONE page to review in "
    "needs.look, with at most two screenshots of it.\n"
    "A pile of screenshots is never the review: more than two loose pictures with no page "
    "to open, or more than four in all, is sent back. Lay them out on ONE review page in "
    "the scratch folder the way they are judged (Before | After side by side, a row per "
    "place, each labelled), make it the page the user opens, and show one or two "
    "screenshots of that page.\n"
    "Screenshots and previews go inside the card as well: a \"links\" entry "
    "{\"label\": \"what it shows\", \"kind\": \"image\", \"url\": \"/api/preview/asset/<id>\"} "
    "is drawn as a picture in the card; on a card with a Validate row it is drawn inside "
    "Validate, under the row to try. Never put a VN-PREVIEW marker in the final message. "
    "Link only addresses that exist: <id> is the one POST /api/preview/render returned; a "
    "picture you already have (an upload, a saved image) is copied into src/web/static/scratch/ and "
    "linked as /static/scratch/<name>. A link to this app that loads nothing is sent back.\n"
    "Mockups are SHOWN, never only linked: a card that links any page in "
    "src/web/static/scratch/ (a mockup, and a demo page for Validate too) carries a screenshot "
    "of it, one per option, as a \"kind\": \"image\" "
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
    "one mockup per option tagged data-option=\"<key>\", so the user picks by looking. "
    "Give the user range: 3 or 4 options from close to Now to a clearly bolder take, "
    "each a different idea, not one design in shades. "
    "A section shows only the design: no caption, tag, badge or \"Option A\" / "
    "\"Now\" heading inside it, since the popup's top bar already names the choice; "
    "its name and description go in the reply.\n"
    "{split_instructions}"
    "A project whose own instructions show design options in its real app "
    "(design trials in that app) does that instead of a scratch page here: each "
    "screen's \"url\" is that app's live page on this machine "
    "(\"http://127.0.0.1:<port>/...\", shown signed in on this computer) and its "
    "\"image\" is its picture for the phone, which cannot reach it: "
    "{\"web\": {\"light\": \"<png>\", \"dark\": \"<png>\"}, \"phone\": {...}}.\n"
    + "\nBUILD THE CARD FROM ITS SKELETON, THEN CHECK IT, BEFORE YOU STOP. First run:\n"
    + _CHECK_COMMAND +
    " --start\n"
    "It prints a skeleton of the right type for this moment with every required key, "
    "each value naming its budget, and what this session's history already fixes filled "
    "in (the path's first step, the big picture, the user's asks, a choice the user has not answered). "
    "Fill it in; keep every key. You cannot count your own words, and a card the stop "
    "sends back costs the user a wait, so pipe the finished card (or the final message) "
    "straight into the check; never save it to a file, because every session shares the "
    "temp folder and other sessions overwrote a common draft file:\n"
    + _CHECK_COMMAND +
    " <<'CARD'\n{ ...the card... }\nCARD\n"
    "It runs the same rules as the stop, against this session's own history, and "
    "prints the word count, every problem at once and, when too long, where the words "
    "are. Fix them all and run it again until it prints OK, then send exactly that card, "
    "unchanged. THE STOP REFUSES A CARD THE CHECK DID NOT PASS: a card never checked, or "
    "changed after its last check, is sent back whatever else is right about it.\n").replace("{split_instructions}", _rules.split_instructions())


# ── The check is not optional ─────────────────────────────────────────────
# Some models skip the self-check unless it is enforced.  So the check leaves a stamp for every
# card it passed, named by the card's own content, and the stop refuses a card
# with no stamp: never checked, or changed after the check.  The stamp proves
# the check ran on these exact words; the stop still runs every rule itself,
# so a stamp never lets a bad card through.  It keeps going back past
# MAX_SENDBACKS (an unchecked card is the failure this exists to stop) and is
# named in the red line when released at the ceiling.
# VIBENODE_HANDOFF_SELF_CHECK=0 turns the requirement off (the test suite does;
# tests/test_handoff_check.py turns it on per test).

STAMP_MAX_AGE_S = 6 * 3600


def card_digest(card: dict) -> str:
    """One name for one card: the parsed JSON, keys sorted, so spacing and
    key order in the message do not matter but a changed word does."""
    import hashlib
    raw = json.dumps(card, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:32]


def stamp_dir() -> "Path":
    import tempfile
    return Path(tempfile.gettempdir()) / "vibenode-smart-termination" / "checked"


def _stamp_name(card: dict, session_id: str = "") -> str:
    # The stamp carries the session that ran the check: named by the card
    # alone, a card checked in one session would pass another session's stop.
    safe = re.sub(r"[^A-Za-z0-9_-]", "", session_id or "")
    return "ok-" + card_digest(card) + ("-" + safe if safe else "")


def stamp_card(card: dict, session_id: str = "") -> None:
    """Record that the self-check passed this card for this session
    (handoff_check only).  The file holds the FIRST time this session passed
    it (who checked a summary first owns it, ``first_owner``); its mtime is
    the latest pass (freshness, ``STAMP_MAX_AGE_S``)."""
    try:
        d = stamp_dir()
        d.mkdir(parents=True, exist_ok=True)
        p = d / _stamp_name(card, session_id)
        if p.exists():
            os.utime(p, None)
        else:
            p.write_text(str(time.time()), "utf-8")
    except Exception:
        pass


def first_owner(card: dict) -> str:
    """The session that checked this exact summary first ('' when no
    session-named stamp exists).  Two sessions never write the identical
    summary, so a second session holding it took another session's draft."""
    try:
        digest = card_digest(card)
        best = None
        for p in stamp_dir().glob("ok-" + digest + "-*"):
            try:
                t = float(p.read_text("utf-8").strip() or p.stat().st_mtime)
            except (OSError, ValueError):
                continue
            if best is None or t < best[0]:
                best = (t, p.name[len("ok-" + digest + "-"):])
        return best[1] if best else ""
    except Exception:
        return ""


def foreign_card_problem(card, session_id: str = "") -> list:
    """A summary another session checked first is that session's, not this one's."""
    if not session_id or not isinstance(card, dict):
        return []
    owner = first_owner(card)
    safe = re.sub(r"[^A-Za-z0-9_-]", "", session_id)
    if owner and owner != safe:
        return ["This summary was written and checked in another session first (" + owner[:8]
                + "), so it is not yours: it describes that session's work. Write your own summary "
                "of THIS session's work, in this session's own draft file, and check that."]
    return []


def self_check_required() -> bool:
    return os.environ.get("VIBENODE_HANDOFF_SELF_CHECK", "1") != "0"


def card_checked(card: dict, session_id: str = "") -> bool:
    """True when the self-check passed exactly this card recently, in this
    session.  A stop that names its session accepts only that session's
    stamp; one that cannot (an older engine) accepts any."""
    try:
        if session_id:
            paths = [stamp_dir() / _stamp_name(card, session_id)]
        else:
            paths = list(stamp_dir().glob("ok-" + card_digest(card) + "*"))
        now = time.time()
        return any(p.exists() and (now - p.stat().st_mtime) < STAMP_MAX_AGE_S for p in paths)
    except Exception:
        return False


def unchecked_problems(card, err=None, session_id: str = "") -> list:
    """The one problem a card has when the self-check never passed it."""
    if err or not isinstance(card, dict) or not self_check_required():
        return []
    foreign = foreign_card_problem(card, session_id)
    if foreign:
        return foreign
    if card_checked(card, session_id):
        return []
    return ["Not checked: the self-check did not pass this exact card (never run, or the card "
            "changed after it). Write the card to a file and run\n" + _CHECK_COMMAND
            + " <file>\nuntil it prints OK, then send that card unchanged."]

# Text outside the card is sent back.
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
           background_in_flight: bool = False, facts=None,
           self_check: bool = True, session_id: str = "") -> Optional[str]:
    """``_review``, then, when the summary passes, a copy of every picture it
    shows (``src/shared/card_pictures.py``): sessions delete or move their
    screenshots after the summary, and it must still draw them."""
    reason = _review(message, continued=continued, sendbacks=sendbacks,
                     background_in_flight=background_in_flight, facts=facts,
                     self_check=self_check, session_id=session_id)
    if reason is None and message:
        try:
            card, _prose, err = _rules.extract(_norm(message).strip())
            if card and not err:
                from shared import card_pictures   # imported here: hot reload into an older engine
                card_pictures.save_card(card)
        except Exception:  # noqa: BLE001 — saving pictures never traps a turn
            pass
    return reason


def _review(message: str, *, continued: bool, sendbacks: int,
            background_in_flight: bool = False, facts=None,
            self_check: bool = True, session_id: str = "") -> Optional[str]:
    """The reason to send the session back, or None to let the turn end.

    The final message must end with the approved ```handoff card
    (``src/engine/handoff_rules.review``).  ``continued``/``background_in_flight``
    are accepted for the callers' sake; the card rules apply either way (a
    session waiting on its own work writes a "working" card).  Never more
    than ``MAX_SENDBACKS`` per stop, never on an error or usage-limit stop.
    ``self_check=False`` is the self-check itself (``handoff_check.py``)
    asking: it leaves out the stamp requirement, which it is about to satisfy.
    """
    if message is None or sendbacks >= HARD_CEILING:
        return None
    text = _norm(message).strip()
    if not text or _ERROR_STOP.search(text[:400]):
        return None
    card, prose, err = _rules.extract(text)
    if facts is not None and background_in_flight:
        facts.background_live = True   # the CLI itself says work is out
    unchecked = unchecked_problems(card, err, session_id) if self_check else []
    # A project's own add-ons (src/engine/stop_plugins): never polish, so they
    # keep sending back past MAX_SENDBACKS too, up to HARD_CEILING.
    addons = addon_problems(card, text, facts) if not err else []
    if sendbacks >= MAX_SENDBACKS:
        # The polish rules have had their two tries; only a summary the user
        # cannot use at all goes back again.
        # A success claim with nothing behind it ("tests pass", "pushed")
        # keeps going back too: let through, it reads as verified.
        # The three tabs keep going back too: otherwise a session that cuts its
        # card for "too long" can shrink Plain words below Short and get through.
        bad = (unusable_problems(card, err) + tab_problems(card, err)
               + unproven_claims(card, facts) + false_working(card, facts) + unchecked + addons)
        if not bad:
            return None
        return ("Smart termination: this summary cannot be used as it stands, so it was sent back "
                f"again (try {sendbacks + 1} of {HARD_CEILING}). Fix exactly these:\n"
                + "\n".join("- " + b for b in bad)
                + "\n\nRewrite only your final message: the ```handoff card alone.")
    outside = (not err) and bool(prose.strip())
    reason = _rules.review(text, facts)
    if outside:
        # Report the text outside the card AND every fault in the card in the
        # same pass, so no send-back is spent on one layer alone.
        reason = ("Smart termination: " + OUTSIDE_CARD
                  + ("\n\n" + reason.split("\n\n", 1)[1] if reason and "\n\n" in reason else ""))
    zoom = zoom_problems(card) if not err else []
    expl = (explained_problems(card) + depth_problems(card)) if not err else []
    if not zoom and not expl and not unchecked and not addons:
        return reason
    note = "\n\n".join(([("Zoom levels:\n" + "\n".join("- " + z for z in zoom))] if zoom else [])
                       + (["Short / Plain words / Full:\n" + "\n".join("- " + x for x in expl)] if expl else [])
                       + (["This project's own checks:\n" + "\n".join("- " + a for a in addons)] if addons else [])
                       + (["Self-check:\n" + "\n".join("- " + u for u in unchecked)] if unchecked else []))
    if reason:
        return reason + "\n\n" + note
    return "Smart termination: your handoff card was sent back.\n\n" + note


def _stop_plugins():
    """The add-on loader as it is on disk now.  This file is reloaded live by the
    Session Engine; the loader is imported once, so a change to it would
    otherwise wait for an engine restart.  A version that does not load keeps
    the running one."""
    import importlib
    from engine import stop_plugins as sp
    try:
        mtime = os.stat(sp.__file__).st_mtime_ns
    except OSError:
        return sp
    # Never seen counts as changed: in a long-running engine the loader may have
    # been imported, unmarked, before this check existed.
    if getattr(sp, "_vn_mtime", None) != mtime:
        try:
            sp = importlib.reload(sp)
        except Exception:   # noqa: BLE001 - keep the running loader
            import logging
            logging.getLogger(__name__).exception("Smart termination: project add-on loader did not reload")
        sp._vn_mtime = mtime
    return sp


def addon_problems(card, message: str, facts=None) -> list:
    """The project add-ons' reasons to send the card back (``src/engine/stop_plugins``)."""
    try:
        return _stop_plugins().problems(card, message, facts)
    except Exception as e:   # the slot itself broke: say so, never pass silently
        return [f"the project add-on slot failed ({type(e).__name__}: {e}); the card cannot be checked."]


def unusable_problems(card, err=None) -> list:
    """Faults that leave the summary useless to the user, as opposed to rough: no
    card, no type, or a request for an answer with nothing to answer with.
    These keep being sent back past MAX_SENDBACKS, up to HARD_CEILING."""
    if err or not isinstance(card, dict):
        return [err or "There is no ```handoff card at the end of the message."]
    out = []
    t = card.get("type")
    if t not in _rules.TYPES:
        out.append(f"\"type\" must be one of: {', '.join(_rules.TYPES)}.")
    needs = card.get("needs") if isinstance(card.get("needs"), dict) else {}
    kind = needs.get("kind")
    replies = [r for r in (needs.get("replies") or []) if isinstance(r, dict)]
    if t == "needs_you" or kind in ("decision", "approve", "action"):
        if kind not in ("decision", "approve", "action"):
            out.append("needs.kind must say what the user does: decision, approve or action.")
        if not str(needs.get("ask") or "").strip():
            out.append("needs.ask is empty: the one thing the user must decide or do.")
        if kind == "decision" and _rules.split_field(needs):
            if not _rules.split_usable(needs):
                out.append(f"needs.{_rules.split_field(needs)} has nothing the user can answer: a form needs at "
                           "least two questions with two options each, pairs at least one pair with two sides.")
        elif kind == "decision" and len(replies) < 2:
            out.append("A decision needs at least two buttons in needs.replies "
                       "(not at the top level of the card).")
        if kind == "approve" and not replies and not (needs.get("look") or []):
            out.append("An approval needs its buttons in needs.replies (not at the top level of "
                       "the card), or the page it is answered on in needs.look.")
        if kind == "action" and not replies and not _rules.asks_for_own_words(card):
            out.append("An action needs a done button in needs.replies.")
        if replies:
            out.extend(_rules.typing_button_problems(card))
            out.extend(_rules.words_card_problems(card))
            out.extend(_rules.errand_button_problems(card))
    return out


def unproven_claims(card, facts) -> list:
    """The card's success claims (tests pass, checked in the app, pushed)
    that the session's own tool calls do not back up; [] without facts."""
    if facts is None or not isinstance(card, dict):
        return []
    try:
        from engine import handoff_evidence as _ev
        return ["Not proven: " + p for p in _ev.claim_problems(card, facts)]
    except Exception:
        return []


def false_working(card, facts) -> list:
    """A "working" card while nothing this session launched is still out.
    Let through, the user waits on work that will never report back, so it
    keeps going back past MAX_SENDBACKS too; [] without facts."""
    if facts is None or not isinstance(card, dict):
        return []
    try:
        from engine import handoff_evidence as _ev
        return list(_ev.working_problems(card, facts))
    except Exception:
        return []


def released_faults(message: str, sendbacks: int, facts=None) -> list:
    """When the summary is let through at HARD_CEILING still unusable or
    still claiming what it did not prove, the faults it carries, so the
    engine can say so on screen; [] otherwise."""
    if message is None or sendbacks < HARD_CEILING:
        return []
    text = _norm(message).strip()
    if not text or _ERROR_STOP.search(text[:400]):
        return []
    card, _prose, err = _rules.extract(text)
    return (unusable_problems(card, err) + tab_problems(card, err)
            + unproven_claims(card, facts) + false_working(card, facts)
            + unchecked_problems(card, err))


def tab_problems(card, err=None) -> list:
    """Short / Plain words / Full: a tab that is missing, too thin, or a Plain
    words that says less than Short leaves a tab the user cannot use, so these
    keep going back past MAX_SENDBACKS, up to HARD_CEILING."""
    if err or not isinstance(card, dict):
        return []
    return explained_problems(card) + depth_problems(card)


# Big picture / this task / just now: three lines under the title.  Every card
# carries them; the renderer draws them
# right under the headline (src/web/static/js/last-card.js).
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
                "what it means for the user, what the user does next), plain words, no length limit. "
                "The card shows it as the \"Plain words\" version."]
    n = len(v.split())
    if n < EXPLAINED_MIN_WORDS:
        return [f'"explained" is {n} words; it must be at least {EXPLAINED_MIN_WORDS}. It is the '
                "long version for when the card is not enough: say what the card leaves out."]
    # Plain words sits after Short and must say more than it.
    a = card.get("answer")
    na = len(a.split()) if isinstance(a, str) else 0
    if na and n <= na:
        return [f'"explained" (Plain words, {n} words) says less than "answer" (Short, {na} words). '
                "Plain words is the longer, slower version: keep every fact Short has, explain the "
                "terms, and say what it means for the user and what the user does next."]
    return []


def depth_problems(card) -> list:
    """Short and Full on every card, beside Plain words ("explained"), so
    every summary has the same shape.  A deliverable's body is not its Full:
    it is drawn in Review & respond, so a deliverable card needs its own
    full_answer too."""
    if not isinstance(card, dict):
        return []
    out = []
    if not (isinstance(card.get("answer"), str) and card["answer"].strip()):
        out.append('Add "answer": the Short version, two or three sentences that go one level past '
                   "the headline (on an explain card the answer; on any other card what happened or "
                   "what is asked). Every card shows Short / Plain words / Full.")
    # The What was done and Checked sections each open on one plain line the
    # session writes, saying what is actually true.  handoff_evidence
    # holds them to the tools the session ran.
    for key, what in (("what_was_done", 'what you actually did this turn, e.g. "Answered directly from '
                                         'training data; no web search, no tools used"'),
                      ("checked", 'what you looked at to be sure, e.g. "No research beyond base training '
                                  'data" or "One web search; two news sources below"')):
        v = card.get(key)
        if not (isinstance(v, str) and v.strip()):
            out.append(f'Add "{key}": one plain, true line, {what}.')
    fa = card.get("full_answer")
    if not (isinstance(fa, str) and fa.strip()):
        out.append('Add "full_answer": the Full version in markdown, everything behind the Short one '
                   "(the detail, numbers, tables, checks), at most 400 words.")
    return out
