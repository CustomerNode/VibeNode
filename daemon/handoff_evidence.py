"""Handoff evidence: the card's claims must match what the session did, and
the card must account for everything the user asked for in this task.

Third layer of Smart termination, after the card format and the banned
phrases (``daemon/handoff_rules.py``).  Phrase lists lag behind: a session
kept from saying "caveat" says the same thing another way.  These two checks
look at behaviour instead, so they cannot be reworded around:

1. **Claims against actions.**  "Tests pass", "checked it in the real app"
   and "pushed" are checked against the session's own tool calls.  A claim
   with nothing behind it, or one the latest run contradicts (the last test
   run failed), sends the card back.  This is the "fourth time you've told me
   it's fixed" pattern from the owner's history.
2. **The card against the requests.**  Every request the user made in this
   task must appear in the card's ``asks`` list with its state.  A request
   missing from the card sends it back, and a request the user has had to
   make more than once must be done, not parked.  Making him repeat himself
   was the biggest single theme in his history (2026-10-27).

Pure and backend-neutral.  ``facts_from_entries`` reads the session's log
entries (anything with ``kind``/``name``/``desc``/``text``/``is_error``);
``problems`` compares a parsed card with those facts.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Iterable, Optional

# ── Facts ─────────────────────────────────────────────────────────────────


@dataclass
class Action:
    tool: str
    detail: str          # command line, file path or short description
    ok: bool             # the tool result came back and was not an error
    output: str = ""
    returned: bool = True   # False while the call has no result: it proves nothing


@dataclass
class TurnFacts:
    actions: list = field(default_factory=list)    # every tool call still in memory, oldest first
    requests: list = field(default_factory=list)   # the user's messages in this task, oldest first
    complete: bool = True   # False when older entries were trimmed: "no evidence" can't be concluded
    prev_big: str = ""      # the "Big picture" line of the session's last card that ended a turn
    prev_path: str = ""     # the first "path" step of that same card
    last_user: str = ""     # the user's latest message, whatever it is ("ok" included)
    history: list = field(default_factory=list)    # every request still in memory, oldest first
    # Other sessions in this project that are working right now, as the
    # Session Engine sees them at the stop (names or short ids).  None when
    # the caller cannot tell (the settings-file copy, an older engine).
    others_working: Optional[list] = None
    # Background work this session launched that has not reported back
    # (a background shell or agent, a scheduled wake-up).  None when it
    # cannot be told (history trimmed).
    background_live: Optional[bool] = None


# A user message that carries no request of its own.
_ACK = re.compile(r"^\W*(?:yes|yeah|yep|yup|ok(?:ay)?|sure|go|go\s+ahead|do\s+it|continue|proceed|"
                  r"thanks?|thank\s+you|cool|great|perfect|nice|y|k|approved?|lgtm|ship\s+it)\W*$", re.I)
_MOBILE = re.compile(r"⟦VN-MOBILE⟧.*?⟦/VN-MOBILE⟧", re.S)
_SENT_FROM = re.compile(r"\n?\s*Sent from \S+ at [^\n]*$")


_NOT_ASKED = ("<", "⟦VN-EVENT", "[VibeNode", "[Image: original ", "[Image #")


def clean_request(text: str) -> str:
    """The user's own words: no injected mobile preamble, no 'Sent from' line."""
    t = _MOBILE.sub("", text or "")
    t = _SENT_FROM.sub("", t)
    return t.strip()


def _is_request(text: str) -> bool:
    # "<…>" is CLI-injected; "⟦VN-EVENT …⟧" is an app event posted into the
    # session (a release went live, an approval was tapped); "[VibeNode …]"
    # is the watchdog's own nudge; "[Image: original WxH, displayed at …]" is
    # the CLI's note on a screenshot the session read (2026-10-28: it was
    # listed back to the session as a request it left out).  None of them is
    # the user asking.
    if not text or text.startswith(_NOT_ASKED) or _ACK.match(text):
        return False
    return len(text.split()) >= 3


_BG_LAUNCHED = re.compile(r"running in (?:the )?background with ID|Async agent launched", re.I)
_WAKE_TOOLS = ("ScheduleWakeup", "Monitor", "CronCreate")


def _background_live(entries: list, last_human: int) -> bool:
    """Something this session launched is still out: more background
    launches than completion notices, or a wake-up scheduled since his
    latest message."""
    launched = notified = 0
    wakes = set()   # wake-up calls since his latest message, by call id
    for i, e in enumerate(entries):
        kind = getattr(e, "kind", "")
        if kind == "tool_result" and getattr(e, "tool_use_id", "") in wakes:
            # A wake-up counts only once it came back without an error
            # (2026-10-29: a failed or unanswered one kept a false Working card).
            if not getattr(e, "is_error", False):
                return True
        elif kind == "tool_result" and _BG_LAUNCHED.search((getattr(e, "text", "") or "")[:400]):
            launched += 1
        elif kind == "user" and (getattr(e, "text", "") or "").lstrip().startswith("<task-notification>"):
            notified += 1
        elif kind == "tool_use" and i > last_human and (getattr(e, "name", "") or "") in _WAKE_TOOLS:
            if getattr(e, "id", ""):
                wakes.add(e.id)
    return launched > notified


def working_problems(card: dict, facts: TurnFacts) -> list:
    """A "working" card says background work is running; the session's own
    calls must show some (2026-10-29: the only check was an ETA)."""
    if card.get("type") != "working" or facts.background_live is not False:
        return []
    return ["This is a \"working\" card, but nothing this session launched is still running: no "
            "background command, agent or scheduled wake-up is out. Finish the work now, or use the "
            "card type that says where it really stands."]


def _closes_task(text: str) -> bool:
    """An assistant message whose handoff card closed a task."""
    if "```" not in (text or ""):
        return False
    from daemon import handoff_rules  # local: handoff_rules imports this module
    card, _, err = handoff_rules.extract(text)
    return not err and isinstance(card, dict) and card.get("type") in ("done", "deliverable", "explain")


def facts_from_entries(entries: Iterable, *, trimmed: bool = False, max_requests: int = 12) -> TurnFacts:
    """Facts for the stop that is happening now.

    Requests are the user's messages since the last card that closed a task
    (done, deliverable or explain) BEFORE the user's latest message, so a
    task the user keeps adding to keeps all of its asks.
    """
    entries = list(entries or ())
    facts = TurnFacts(complete=not trimmed)
    pending = {}
    for e in entries:
        kind = getattr(e, "kind", "")
        if kind == "tool_use":
            # Not a success until its result comes back (2026-10-29): a call
            # cut off or still hanging must not prove tests, a push or a check.
            a = Action(tool=getattr(e, "name", "") or "", detail=getattr(e, "desc", "") or "",
                       ok=False, returned=False)
            facts.actions.append(a)
            if getattr(e, "id", ""):
                pending[e.id] = a
        elif kind == "tool_result":
            a = pending.pop(getattr(e, "tool_use_id", ""), None)
            if a is None:   # a result with no call in memory still counts as output
                a = Action(tool="", detail="", ok=True)
                facts.actions.append(a)
            a.returned = True
            a.ok = not bool(getattr(e, "is_error", False))
            a.output = getattr(e, "text", "") or ""

    human = [i for i, e in enumerate(entries)
             if getattr(e, "kind", "") == "user" and _is_request(clean_request(getattr(e, "text", "")))]
    if not trimmed:
        facts.background_live = _background_live(entries, human[-1] if human else -1)
    if human:
        last_human = human[-1]
        start = -1
        for i in range(last_human - 1, -1, -1):
            e = entries[i]
            if getattr(e, "kind", "") == "asst" and _closes_task(getattr(e, "text", "")):
                start = i
                break
        facts.requests = [clean_request(entries[i].text) for i in human if i > start][-max_requests:]
    for e in reversed(entries):
        t = clean_request(getattr(e, "text", "")) if getattr(e, "kind", "") == "user" else ""
        if t and not t.startswith(_NOT_ASKED + ("Stop hook feedback",)):
            facts.last_user = t
            break
    facts.prev_big, facts.prev_path = _prev_big(entries, with_path=True)
    facts.history = [clean_request(entries[i].text) for i in human][-60:]
    return facts


def _prev_big(entries: list, with_path: bool = False):
    """The big-picture line of the last card that ended a turn.

    A card counts once the user's next message follows it; a card that was
    sent back (a ``sent_back`` entry follows) and the card being checked now
    (nothing follows yet) do not."""
    from daemon import handoff_rules  # local: handoff_rules imports this module
    found = ""
    path0 = ""
    for i, e in enumerate(entries):
        if getattr(e, "kind", "") != "asst" or "```handoff" not in (getattr(e, "text", "") or ""):
            continue
        nxt = next((x for x in entries[i + 1:] if getattr(x, "kind", "") not in ("asst", "tool_use", "tool_result", "system", "stream")), None)
        if nxt is None or getattr(nxt, "kind", "") != "user" or \
                (getattr(nxt, "text", "") or "").lstrip().startswith("Stop hook feedback"):
            continue
        card, _, err = handoff_rules.extract(e.text)
        z = card.get("zoom") if not err and isinstance(card, dict) else None
        if isinstance(z, dict) and isinstance(z.get("big"), str) and z["big"].strip():
            found = z["big"].strip()
        p = card.get("path") if not err and isinstance(card, dict) else None
        if isinstance(p, list) and p and isinstance(p[0], str) and p[0].strip():
            path0 = p[0].strip()
    return (found, path0) if with_path else found


# ── 1. Claims against actions ─────────────────────────────────────────────

_TEST_CMD = re.compile(
    r"\b(?:pytest|py\.test|unittest|vitest|jest|mocha|tox|nox|rspec|phpunit)\b"
    r"|\bnpm\s+(?:run\s+)?test\b|\bnpx\s+(?:playwright\s+test|vitest|jest)\b|\bplaywright\s+test\b"
    r"|\bgo\s+test\b|\bcargo\s+test\b|\bmake\s+(?:test|check)\b|\bnode\s+\S*tests?/\S+"
    # A test script being RUN (not read: "cat tests/foo.py" is not a run).
    r"|(?:^|[;&|]\s*|\b(?:bash|sh|python3?|node|uv\s+run)\s+)\.?/?\S*test\S*\.(?:sh|py|js|mjs)\b", re.I)
# A test runner's summary, wherever it shows up (a backgrounded run's output
# is read later through another tool).  Runner-shaped only, so a file that
# merely says "1 failed" is not a run: pytest's "=== 3 failed, 409 passed ==="
# or "... in 1.2s", jest/vitest's "Tests: ...", TAP's "# pass N", mocha's
# "N passing", and key-value runners ("passed=12108 failed=0",
# "passed 13733 failed 0").
_SUMMARY = re.compile(
    r"^=+\s[^\n]*\b\d+\s+(?:passed|failed)\b[^\n]*=+\s*$"
    r"|\b\d+\s+(?:passed|failed)\b[^\n]*\bin\s+\d+(?:\.\d+)?s\b"
    r"|^\s*Tests?:?\s+\d+\s+(?:passed|failed)\b"
    r"|^#\s*(?:pass|fail)\s+\d+|\b\d+\s+(?:passing|failing)\b"
    r"|\bpassed\s*[=:\s]\s*\d+[^\n]{0,40}\bfailed\s*[=:\s]\s*\d+", re.I | re.M)
_SOURCE_FILE = re.compile(r"\.(?:md|py|pyi|tsx?|jsx?|mjs|ya?ml|toml|cfg|ini|html|css|rs|go|java|rb)\b", re.I)
# A tool call that never ran: refused by the user or the permission layer.
_NOT_RUN = re.compile(r"doesn'?t want to proceed|tool use was rejected|permission request failed"
                      r"|permission stream closed|user denied", re.I)
# A claim about a red-first proof: the latest run may well be the deliberate red one.
_RED_GREEN = re.compile(r"\bred\b[^.;\n]{0,40}\bgreen\b|\bseen\s+red\b|\bproven\s+red\b|\b(?:goes|went|turns?|turned)\s+red\b"
                        r"|\bfail(?:s|ed)?\s+(?:on|against|with)\s+(?:the\s+)?(?:old|\S*\d)\S*[^.;\n]{0,40}\bpass", re.I)
_FAIL_KV = re.compile(r"\b(?:failed|failures|failing)\s*[=:]\s*(\d+)|\bfailed\s+(\d+)\b", re.I)
_FAIL_N = re.compile(r"\b([1-9]\d*)\s+(?:failed|failing|errors?)\b|^#\s*fail\s+([1-9]\d*)|^(FAILED)\b"
                     r"|\bTests?:\s+([1-9]\d*)\s+failed", re.I | re.M)
_BACKGROUND = re.compile(r"running in (?:the )?background|background (?:task|job|shell)|with ID:", re.I)

_BROWSER_TOOL = re.compile(r"browser|playwright|chrom|puppeteer|selenium|screenshot|preview", re.I)
_BROWSER_CMD = re.compile(r"playwright|chromium|chrome|puppeteer|selenium|screenshot|/api/preview", re.I)
_IMAGE = re.compile(r"\.(?:png|jpe?g|webp|gif)\b", re.I)
# Asking a browser its version, finding it, or installing it looks at nothing
# (2026-10-29: "chromium --version" passed for a browser check).
_BROWSER_NOT_A_CHECK = re.compile(
    r"--version\b|^\s*(?:which|where|command\s+-v|type)\s|\bpip3?\s+(?:show|install|list|download)\b"
    r"|\bplaywright\s+install\b|\bnpm\s+(?:i|install|ls|list)\b", re.I)

# "git push", "git -C <dir> push", or a push's own output wherever it shows
# up (a release script pushes for the session).
_PUSH_CMD = re.compile(r"\bgit\b[^;&|\n]{0,200}?\spush\b|\bgh\s+pr\s+merge\b|\bgit\b[^;&|\n]{0,200}?\smerge\b", re.I)
_PUSH_OUT = re.compile(r"\b[0-9a-f]{7,}\.\.\.?[0-9a-f]{7,}\s+\S+\s+->\s+\S+|\[new branch\]\s+\S+\s+->\s+\S+")

#: Tools that hand work to another agent.  Their report is the only trace of
#: what that agent did, so it counts as evidence for push and app claims.
DELEGATION_TOOLS = ("Agent", "Task")


def _failed(output: str) -> Optional[str]:
    """The failure count line in a runner's output, or None when it passed.
    A "failed=0" / "failed 0" key-value summary wins over the "N failed" form,
    because "passed 13733 failed 0" also reads as "13733 failed".

    Only the runner's LAST summary line decides: a log can say anything above
    it ("connection to localhost:55432 failed" above "5 passed in 8.35s")."""
    lines = [output[output.rfind("\n", 0, m.start()) + 1:(output.find("\n", m.end()) + 1 or len(output) + 1) - 1]
             for m in _SUMMARY.finditer(output)]
    if not lines:
        return None
    last = lines[-1]
    # TAP prints "# pass N" and "# fail N" on separate lines: read both.
    if re.match(r"\s*#\s*(?:pass|fail)\b", last):
        last = "\n".join(l for l in lines if re.match(r"\s*#\s*(?:pass|fail)\b", l))
    kv = [(m.group(0), int(m.group(1) or m.group(2))) for m in _FAIL_KV.finditer(last)]
    if kv:
        bad = [s for s, n in kv if n > 0]
        return bad[0] if bad else None
    m = _FAIL_N.search(last)
    return m.group(0) if m else None

CLAIMS = {
    "tests": re.compile(
        r"\b(?:tests?|suites?)\b[^.;\n]{0,60}?\b(?:pass(?:es|ed)?|green)\b(?![-\s]*(?:rate|error|attempt))"
        r"|\b\d[\d,]*\s+(?:(?:new|unit|backend|frontend|e2e|browser)\s+)?(?:tests?\s+)?pass(?:ed|ing)?\b"
        r"|\b(?:all|everything)\s+(?:is\s+)?green\b|\bgreen\s+(?:run|suite|gate)\b", re.I),
    "app": re.compile(
        r"\b(?:checked|tested|verified|confirmed|saw|watched|looked|clicked|opened|loaded|tried)\b[^.;\n]{0,40}?"
        r"\b(?:in|on|at)\s+(?:the\s+|a\s+)?(?:real\s+|headless\s+|live\s+)?(?:app|browser|chrom\w*|phone(?:[-\s]width)?)\b"
        r"|\b(?:real|headless)\s+(?:browser|chrom\w*)\b|\bscreenshotted\b|\btook\s+(?:a\s+)?screenshots?\b"
        r"|\bscreenshots?\s+(?:show|shows|showed|confirm|confirms|confirmed)\b|\bclicked\s+through\b", re.I),
    "pushed": re.compile(r"\bpushed\b|\bis\s+on\s+(?:develop|main|master)\b|\bmerged\s+(?:in)?to\b", re.I),
}
# "pushed" is a git claim only near a git word: "values are pushed to every
# open browser" is not.
_GIT_CONTEXT = re.compile(r"\b(?:develop|main|master|origin|branch|remote|github|repo|commit|releases)\b"
                          r"|\b[0-9a-f]{7,40}\b", re.I)
# Words before a claim that make it not a claim: negation, condition, future.
_NEGATED = re.compile(r"(?:\b(?:not|never|no|nothing|none|neither|nor|without|before|unless|until|once|if|when|"
                      r"after|will|would)\b|n't\b|\bI'll\b|\bgoing\s+to\b)[^.;\n]{0,45}$", re.I)
# A sentence about work still under way, or a condition ("only passes if"),
# is not a claim that it finished.
_ONGOING = re.compile(r"\b(?:running|in\s+progress|queued|still\s+waiting|I'll|will|going\s+to|if|unless)\b", re.I)
_EARLIER = re.compile(r"\b(?:earlier|yesterday|previously|already|last\s+(?:turn|time|night|week)|this\s+morning|"
                      r"another\s+session|the\s+other\s+session)\b", re.I)
_SENTENCE_END = re.compile(r"[.;\n]")


def _sentence(text: str, start: int, end: int) -> str:
    lo = max((m.end() for m in _SENTENCE_END.finditer(text, 0, start)), default=0)
    m = _SENTENCE_END.search(text, end)
    return text[lo:m.start() if m else len(text)]


def _is_claim(kind: str, text: str, m) -> bool:
    if _NEGATED.search(text[max(0, m.start() - 60):m.start()]):
        return False
    sentence = _sentence(text, m.start(), m.end())
    if _ONGOING.search(sentence):
        return False
    if kind == "tests" and re.search(r"\b\d[\d,]*\s+of\s+\d", sentence):
        return False   # "9,209 of 9,213 pass" reports a count, not a pass
    if kind == "pushed" and not _GIT_CONTEXT.search(text[max(0, m.start() - 60):m.end() + 60]):
        return False
    return True


def _claim_fields(card: dict):
    """The strings where the card says what it did and how it checked."""
    if isinstance(card.get("headline"), str):
        yield "headline", card["headline"]
    for i, d in enumerate(card.get("did") or []):
        if isinstance(d, str):
            yield f"did[{i}]", d
        elif isinstance(d, dict):
            for k in ("what", "why"):
                if isinstance(d.get(k), str):
                    yield f"did[{i}].{k}", d[k]
    status = card.get("status") if isinstance(card.get("status"), dict) else {}
    for i, d in enumerate(status.get("done") or []):
        t = d if isinstance(d, str) else (d.get("what") if isinstance(d, dict) else None)
        if isinstance(t, str):
            yield f"status.done[{i}]", t
    for i, v in enumerate(card.get("verified") or []):
        if isinstance(v, dict):
            for k in ("check", "result"):
                if isinstance(v.get(k), str):
                    yield f"verified[{i}].{k}", v[k]


def claims(card: dict) -> list:
    """``(kind, where, phrase)`` for each claim the card makes about this work."""
    out = []
    for where, text in _claim_fields(card):
        if _EARLIER.search(text):
            continue
        for kind, rx in CLAIMS.items():
            for m in rx.finditer(text):
                if _is_claim(kind, text, m):
                    out.append((kind, where, m.group(0).strip()))
    return out


def _red_green(card: dict) -> bool:
    return any(_RED_GREEN.search(text) for _, text in _claim_fields(card))


def test_runs(facts: TurnFacts) -> list:
    """``(passed, line)`` for each test run in memory, oldest first.  A run is
    a test command, or any tool output carrying a runner's summary."""
    runs = []
    for a in facts.actions:
        if not a.returned:
            continue   # no result yet: neither a pass nor a failure
        is_cmd = bool(_TEST_CMD.search(a.detail))
        # A summary counts from a test command or a log/output file, never
        # from reading source or notes (a doc can quote any summary).
        has_summary = bool(_SUMMARY.search(a.output)) and (is_cmd or not _SOURCE_FILE.search(a.detail))
        if not (is_cmd or has_summary) or _NOT_RUN.search(a.output[:300]):
            continue
        if is_cmd and a.ok and _BACKGROUND.search(a.output[:400]) and not has_summary:
            continue   # launched in the background; its summary shows up later
        fail_line = _failed(a.output) if has_summary else None
        failed = bool(fail_line) or (not a.ok and is_cmd)
        if fail_line:
            line = fail_line
        elif has_summary:
            line = _SUMMARY.search(a.output).group(0)
        else:
            line = a.detail[:80]
        runs.append((not failed, line))
    return runs


def _delegated_says(a: Action, kind: str) -> bool:
    if a.tool not in DELEGATION_TOOLS or not a.ok:
        return False
    return any(_is_claim(kind, a.output, m) for m in CLAIMS[kind].finditer(a.output))


def _app_checked(facts: TurnFacts) -> bool:
    for a in facts.actions:
        if a.ok and (_BROWSER_TOOL.search(a.tool) or _IMAGE.search(a.detail)
                     or (_BROWSER_CMD.search(a.detail) and not _BROWSER_NOT_A_CHECK.search(a.detail))):
            return True
        if _delegated_says(a, "app"):
            return True
    return False


def _pushed(facts: TurnFacts) -> bool:
    return any((a.ok and _PUSH_CMD.search(a.detail)) or _PUSH_OUT.search(a.output) or _delegated_says(a, "pushed")
               for a in facts.actions)


def claim_problems(card: dict, facts: TurnFacts) -> list:
    out = []
    found = claims(card)
    kinds = {k for k, _, _ in found}
    first = {}
    for k, where, phrase in found:
        first.setdefault(k, f"\"{phrase}\" ({where})")
    if "tests" in kinds:
        runs = test_runs(facts)
        if runs and _red_green(card):
            # A red-first proof ends on its deliberate red run; it still
            # needs a green one somewhere.
            if not any(ok for ok, _ in runs):
                out.append(f"The card says tests went red then green, {first['tests']}, but no passing "
                           "test run shows up in this session. Run them, or take the claim out.")
        elif runs and not runs[-1][0]:
            out.append(f"The card says tests pass, {first['tests']}, but the latest test run in this "
                       f"session failed (\"{runs[-1][1]}\"). Fix it and run them again.")
        elif not runs and facts.complete:
            out.append(f"The card says tests pass, {first['tests']}, but no test run shows up in this "
                       "session. Run them, or take the claim out.")
    if "app" in kinds and facts.complete and not _app_checked(facts):
        out.append(f"The card says it was checked in the app or a browser, {first['app']}, but no browser, "
                   "screenshot or preview step shows up in this session. Check it for real, or take the claim out.")
    if "pushed" in kinds and facts.complete and not _pushed(facts):
        out.append(f"The card says it was pushed, {first['pushed']}, but no successful push shows up in this "
                   "session. Push it, or take the claim out.")
    return out


# ── 2. The card against the requests ──────────────────────────────────────

_STOP = frozenset("""a about above after again all also am an and any are as at be because been before being
both but by can could did do does doing done dont down each even ever every few for from get got had has have
having he her here him his how i if im in into is it its ive just let lets like make me more most my need no
not now of off on once only or other our out over own please really same she should so some still such than
that thats the their them then there these they this those through to too under until up very was way we
well were what when where which while who why will with would yeah yes you your youre""".split())
_WORD = re.compile(r"[a-z0-9][a-z0-9'_-]{2,}")
# Plain "again" is not a repeat ("check that edit works again"); these are.
_REPEAT = re.compile(
    r"^\W*(?:so\s+)?again,|\bonce\s+again\b|\bask(?:ing)?\s+(?:you\s+)?again\b|\b(?:said|told|asked)\b[^.\n]{0,30}\bagain\b"
    r"|\bi\s+(?:already\s+)?(?:told|asked)\s+you\b|\bi(?:'ve|\s+have)\s+(?:said|asked|told)\b"
    r"|\b(?:\d+|two|three|ten|several|multiple|many)\s+(?:\w+\s+)?times\b|\bwhat\s+part\s+of\b"
    r"|\brepeat\s+myself\b|\bstill\s+(?:not|isn'?t|hasn'?t|doesn'?t|don'?t|broken)\b", re.I)

ASK_DONE_STATES = ("done", "answered", "replaced")
ASK_STATES = ("done", "answered", "replaced", "needs_you", "blocked", "working")


def _words(text: str) -> set:
    return {w.strip("'_-") for w in _WORD.findall((text or "").lower())} - _STOP


def _covers(ask: str, request: str) -> bool:
    rw, aw = _words(request), _words(ask)
    if not rw:
        return True
    need = min(3, max(1, (len(rw) * 3 + 9) // 10))   # 30% of the request's words, capped at 3
    if len(rw & aw) >= need:
        return True
    r, a = " ".join(request.lower().split()), " ".join(ask.lower().split())
    return any(len(chunk) >= 20 and chunk in r for chunk in (a[i:i + 20] for i in range(0, max(1, len(a) - 19), 5)))


def repeats(requests: list) -> dict:
    """``{index: times asked}`` for requests the user has had to make more than once."""
    out = {}
    ws = [_words(r) for r in requests]
    for i, r in enumerate(requests):
        n = 1 + sum(1 for j in range(i) if len(ws[i] & ws[j]) >= 3
                    and len(ws[i] & ws[j]) / max(1, len(ws[i] | ws[j])) >= 0.5)
        if n > 1 or _REPEAT.search(r):
            out[i] = max(n, 2)
    return out


def _short(text: str, n: int = 140) -> str:
    t = " ".join((text or "").split())
    return t if len(t) <= n else t[:n - 1] + "…"


def request_problems(card: dict, facts: TurnFacts) -> list:
    reqs = facts.requests
    if len(reqs) < 2:
        return []   # one request: the headline speaks for it
    asks = [a for a in (card.get("asks") or []) if isinstance(a, dict) and isinstance(a.get("ask"), str)]
    listing = "\n".join(f"  {i + 1}. \"{_short(r)}\"" for i, r in enumerate(reqs))
    if not asks:
        return ["The user made more than one request in this task. Add \"asks\": one entry per request, "
                "with a few of the user's own words in \"ask\" and its \"state\" ("
                + ", ".join(ASK_STATES) + "). The requests:\n" + listing]
    out = []
    bad_state = [a["ask"] for a in asks if a.get("state") not in ASK_STATES]
    if bad_state:
        out.append("Each ask needs a \"state\" of " + ", ".join(ASK_STATES) + ": "
                   + "; ".join(f"\"{_short(a, 60)}\"" for a in bad_state[:4]) + ".")
    matched = {}
    for i, r in enumerate(reqs):
        for a in asks:
            if _covers(a["ask"], r):
                matched[i] = a
                break
    missing = [i for i in range(len(reqs)) if i not in matched]
    if missing:
        out.append("The card leaves out what the user asked for:\n"
                   + "\n".join(f"  {i + 1}. \"{_short(reqs[i])}\"" for i in missing)
                   + "\nDo it, or add it to \"asks\" with its state, in the user's own words.")
    if card.get("type") == "done":
        open_asks = [a["ask"] for a in asks if a.get("state") not in ASK_DONE_STATES]
        if open_asks:
            out.append("A \"done\" card cannot have an ask that is not done: "
                       + "; ".join(f"\"{_short(a, 60)}\"" for a in open_asks[:4]) + ".")
    for i, n in repeats(reqs).items():
        a = matched.get(i)
        if a is not None and a.get("state") not in ASK_DONE_STATES:
            out.append(f"The user has asked for this {n} times: \"{_short(reqs[i])}\". Do it now. Stop on it "
                       "only if it truly needs them, and then say exactly what you need.")
    return out


# ── 3. Lines that must make sense on their own ────────────────────────────
#
# The headline and the ask are shown WITHOUT the rest of the card: the
# sidebar's status line, the homepage's "Needs you" list, notifications.  The
# owner works across many sessions at once; "Pick a button design for the
# card" told him nothing (2026-10-27: "idek what that is referring to ...
# fix that systemically").  So those two lines may not lean on the session's
# history: no bare "the card" / "the fix" / "this", no opening pronoun.

_GENERIC = (r"card|cards|page|pages|button|buttons|design|designs|fix|fixes|bug|bugs|issue|issues|change|changes|"
            r"feature|features|option|options|choice|choices|thing|things|branch|pr|test|tests|screen|view|panel|"
            r"flow|setting|settings|script|file|files|layout|version|plan|problem|approach|proposal|idea|work|task|"
            r"step|steps|item|items|list|form|modal|dialog|banner|link|app|site|repo|server|release|gate|build|"
            r"one|ones|part|section|piece|stuff|update|patch|draft|mockup|variant|variants")
# "the card" with nothing after it saying which one.  "the card on the Trial
# page" / "the card sessions end with" / "the card: ..." are qualified.
_VAGUE_REF = re.compile(
    r"\b(?:the|this|that|these|those)\s+(?:" + _GENERIC + r")\b"
    r"(?!\s*(?:[(:—–-]|for\b|on\b|in\b|of\b|from\b|to\b|at\b|that\b|which\b|where\b|when\b|called\b|named\b|"
    r"sessions?\b|you\b|we\b|I\b|it\b|they\b|he\b|she\b|(?-i:[A-Z0-9\"“])))", re.I)
_VAGUE_OPEN = re.compile(r"^\W*(?:it|this|that|these|those|they)\b(?!\s+(?:" + _GENERIC + r")\b)", re.I)
# Something that says WHICH: a name past the first word (VibeNode, Trial,
# GitHub), a number like a release, a quoted name or code.  A line with one
# can say "the tests" ("CustomerNode's sort fix is in and the tests pass").
_ANCHOR = re.compile(r"(?<=\s)(?!I\b)[A-Z][A-Za-z0-9]+|\b[A-Z][a-z]+[A-Z][A-Za-z]*|\b\d{2,}|[\"“`][^\"”`]{2,}[\"”`]|\b[a-z]+[A-Z][A-Za-z]*\b")
MIN_ASK_WORDS = 4
STANDALONE_FIELDS = ("headline", "needs.ask")


def standalone_problems(card: dict) -> list:
    """The headline and ask must make sense to someone who has read nothing."""
    needs = card.get("needs") if isinstance(card.get("needs"), dict) else {}
    fields = (("headline", card.get("headline")), ("needs.ask", needs.get("ask")))
    hits = []
    for where, text in fields:
        if not isinstance(text, str) or not text.strip():
            continue
        t = " ".join(text.replace("’", "'").split())
        m = _VAGUE_OPEN.match(t)
        if m:
            hits.append(f"\"{m.group(0).strip()}\" ({where})")
        if where == "needs.ask" and len(t.split()) < MIN_ASK_WORDS:
            hits.append(f"\"{t}\" ({where}, too short to say what it is about)")
        if not _ANCHOR.search(t):
            for m in _VAGUE_REF.finditer(t):
                hits.append(f"\"{m.group(0)}\" ({where})")
    if not hits:
        return []
    return ["The headline and the ask are shown on their own (the sidebar, the Needs you list, "
            "notifications), to someone working across many sessions. These lean on this session's "
            "history: " + ", ".join(dict.fromkeys(hits)) + ". Name the project and the exact thing, "
            "e.g. \"Pick a design for the buttons on VibeNode's end-of-turn summary card\", "
            "not \"Pick a button design for the card\"."]


# ── 4. Written for the reviewer ───────────────────────────────────────────
# The owner, 2026-10-28: a card titled "Card rules load live" with the big
# picture "Make VibeNode's handoff cards clean to read" answered his question
# "how did you sneak a long and small title through there".  It named the
# session's own fix, not his question; its big picture had drifted to the
# latest sub-task, so with ten sessions open it looked like another session;
# and the answer to his question was buried.  "We're still falling into AI
# centric reporting from the implementer rather than contextual to the
# reviewer."

# Spoken lead-ins before the question itself: "wait so how do we…".
_LEAD_IN = re.compile(r"^(?:\W*\b(?:wait|so|ok(?:ay)?|hmm+|um+|uh+|but|and|also|then|well|right|yeah|hey|actually|sorry|quick\s+question)\b[,.:;!]?)+\s*", re.I)
_QUESTION_START = re.compile(
    r"^\W*(?:how|why|what|when|where|who|which|whose"
    # an auxiliary only with a subject after it: "do we…", not "do the templates"
    r"|(?:is|are|was|were|does|did|do|has|have|should|isn'?t|aren'?t|didn'?t|doesn'?t|wasn'?t)\s+(?:we|you|i|it|they|this|that|there|he|she)"
    r")\b", re.I)
_POLITE_ASK = re.compile(r"^\W*(?:can|could|would|will)\s+you\b|^\W*please\b", re.I)
# A request that quotes a question ("id like to see some variants of Which
# preview should deliverable cards show?") is still a request: the owner,
# 2026-10-28, got screenshots instead of a Decide card because the quoted "?"
# made his order read as a question.  "I want to know why…" stays a question.
_REQUEST_START = re.compile(
    r"^\W*(?:i'?d\s+(?:like|love|want)|i\s+(?:would\s+like|want|need)|show\s+me|give\s+me|let\s+me\s+see"
    r"|make|build|add|create|put|mock\s*up)\b(?!\s+to\s+(?:know|understand|hear|ask))", re.I)


def is_question(text: str) -> bool:
    """The user's message asks something (not a polite "can you do X?")."""
    t = " ".join((text or "").split())
    if not t:
        return False
    for sentence in re.split(r"(?<=[.!?\n])\s+", t):
        sentence = _LEAD_IN.sub("", sentence)
        if not sentence or _POLITE_ASK.match(sentence) or _REQUEST_START.match(sentence):
            continue
        if sentence.rstrip().endswith("?") or _QUESTION_START.match(sentence):
            return True
    return False


def _stem(words: set) -> set:
    return {w[:5] for w in words if len(w) >= 4 and not w.isdigit()}


def reviewer_problems(card: dict, facts: TurnFacts) -> list:
    out = []
    zoom = card.get("zoom") if isinstance(card.get("zoom"), dict) else {}
    big = zoom.get("big") if isinstance(zoom.get("big"), str) else ""
    asked = " ".join(facts.requests + ([facts.last_user] if facts.last_user else []))

    # The big picture names the session's thread and stays put; a new line
    # only when the user moved to a new topic (his words are in it).
    if facts.prev_big and big.strip():
        old, new = _stem(_words(facts.prev_big)), _stem(_words(big))
        kept = len(old & new) / max(1, len(old | new))
        # A shorter line made only of the old line's words is the same topic
        # (the zoom cap went from 20 words to 40 characters on 2026-10-28,
        # so every running session shortens its line once).
        if kept < 0.5 and not new <= old and not (new - old) & _stem(_words(asked)):
            out.append(f"\"zoom.big\" changed from \"{_short(facts.prev_big, 90)}\" to \"{_short(big, 90)}\". "
                       "The big picture is how the user tells this session apart from his others, so it "
                       "stays the same across turns. Keep the earlier line unless the user started a new topic.")

    # A question gets its answer first, in his terms, and an answer is never
    # turned into a decision: "i just got a decide on a question?" (the
    # owner, 2026-10-28, after a card answered his question and then asked
    # him to pick whether to add a new check).
    if facts.last_user and is_question(facts.last_user):
        ans = card.get("answer")
        if not (isinstance(ans, str) and ans.strip()):
            out.append(f"The user asked a question (\"{_short(facts.last_user, 120)}\"). Answer it first, in "
                       "\"answer\": one or two plain sentences, before anything about what you changed.")
        needs = card.get("needs") if isinstance(card.get("needs"), dict) else {}
        # Except a pick between mockups he asked for: variants on screen always
        # get a reply per variant ("screens with no next cta makes zero
        # fucking sense", 2026-10-28).  The "tied to his request" check below
        # still applies to it.
        from daemon import handoff_rules  # local: handoff_rules imports this module
        picks = needs.get("kind") == "decision" and handoff_rules.shows_variants(card)
        # And a yes on work he asked for: an answer whose next step waits on
        # him carries the button for it ("once i confirm with no ctas ... that
        # is fucking useless", 2026-10-28).  It must answer first and be tied
        # to his request in "asks"; a new idea is still not an approval.
        tied = any(isinstance(a, dict) and a.get("state") == "needs_you" for a in (card.get("asks") or []))
        yes = needs.get("kind") == "approve" and isinstance(ans, str) and ans.strip() and tied
        if needs.get("kind") in ("decision", "approve") and not picks and not yes:
            out.append("The user asked a question, and this card answers it with a Decide card. An answer "
                       "is type \"explain\" (\"deliverable\" when it is long) with needs.kind \"none\". "
                       "Do not attach a new choice to an answer; anything you would offer goes in needs.next "
                       "as what happens next. If work he asked for waits on his yes, use needs.kind "
                       "\"approve\" with the answer in \"answer\" and that request in \"asks\" (state \"needs_you\").")
        # A question asked while choices are up wants the session's pick, and
        # the buttons must say which one it is (2026-10-29, the owner on "Which
        # do you prefer and why": the answer leaned one way but the choices
        # still read as an unmarked A or B).
        replies = needs.get("replies") if isinstance(needs.get("replies"), list) else []
        if picks and len(replies) > 1 and \
                sum(1 for r in replies if isinstance(r, dict) and r.get("recommended") is True) != 1:
            out.append("The user asked a question about the choices on screen, so he wants your pick. Mark "
                       "exactly one reply \"recommended\": true (the one your answer favours), so the choices "
                       "read as \"<that one>, recommended\" and the other, and say why in needs.why.")

    # A Decide card is for a choice that blocks something he asked for, not
    # an idea the session would like to offer ("why did you think that was
    # the right choice?", 2026-10-28: an answer arrived as a Decide card about
    # adding a check nobody had asked for).
    # The same for an Approve (2026-10-28: an approval about a permission gap
    # a test had found, which he had never raised, passed because its "asks"
    # entry was text the card wrote for itself).  The match is against his
    # actual messages; something noticed outside his ask goes in "finding".
    needs = card.get("needs") if isinstance(card.get("needs"), dict) else {}
    if needs.get("kind") in ("decision", "approve") and facts.history and facts.complete:
        waiting = [a.get("ask", "") for a in (card.get("asks") or []) if isinstance(a, dict)
                   and a.get("state") == "needs_you" and isinstance(a.get("ask"), str)]
        if not any(_covers(w, r) or _covers(r, w) for w in waiting for r in facts.history):
            what = "Decide" if needs.get("kind") == "decision" else "Approve"
            out.append(f"This {what} card is not tied to anything the user asked for. A decision or an "
                       "approval is only for a choice that blocks work he asked for: list that request "
                       "in \"asks\" with state \"needs_you\", in his words. An idea or improvement you "
                       "would like to offer is not a decision; name it in needs.next. Something you "
                       "noticed outside his ask that you may not act on alone goes in \"finding\" "
                       "(what, risk, recommend), drawn as an \"Also found\" band with its own button.")

    # The path's first step is where the thing lives; like the big picture it
    # stays put unless his own words moved to a new topic (owner, 2026-10-28,
    # static/scratch/card-title-crumb.html).
    path = card.get("path") if isinstance(card.get("path"), list) else []
    first = path[0].strip() if path and isinstance(path[0], str) else ""
    prev_first = getattr(facts, "prev_path", "") or ""
    if prev_first and first and first.lower() != prev_first.lower():
        old_w, new_w = _stem(_words(prev_first)), _stem(_words(first))
        if not new_w <= old_w and not (new_w - old_w) & _stem(_words(asked)):
            out.append(f"\"path\" starts with \"{first}\" but this session's last summary started it with "
                       f"\"{prev_first}\". The path's first step stays the same all session unless the user "
                       "started a new topic; keep it, or add steps after it.")

    # When he asked something, the title names his question in words he
    # would recognise, not the fix.  Only then: a terse order ("add the
    # guard") leaves too few words to judge a title by.
    title = card.get("title") if isinstance(card.get("title"), str) else ""
    mine = _stem(_words(facts.last_user))
    if title.strip() and is_question(facts.last_user) and len(mine) >= 3 and \
            not (_stem(_words(title)) | _stem(_words(" ".join(x for x in path if isinstance(x, str))))) & (mine | _stem(_words(facts.prev_big))):
        out.append(f"\"title\" (\"{title.strip()}\") and \"path\" share no word with what the user asked or "
                   "this session's big picture. Name the thing his question is about, in his words.")
    return out


# ── Entry point ───────────────────────────────────────────────────────────

# ── 5. Work left to another session, and Next lines that only hope ───────
# The owner, 2026-10-28, on a "Blocked" card that left 3 failing tests to
# "another session's in-progress edit" (that session had not touched the
# files for 11 minutes) and whose Next line read "That session's own test run
# should catch these 3": "how do we fix the code in smart termination so this
# does not happen again".  Two causes.  The rule against blaming another
# session was only words in the instructions; and VibeNode's note on other
# active sessions (app/session_awareness.py), written once when a session
# starts, said to back off and report.  So:
#   1. a card that leaves work undone because of another session is sent
#      back when no other session in the project is working now (the engine
#      knows: ``TurnFacts.others_working``), and, when that is unknown,
#      unless its "tried" shows the session was seen still working, and when;
#   2. a Next line that hopes someone else will act ("should catch",
#      "hopefully") is sent back on any card: say who does what, and when.
_OTHER_SESSION = re.compile(
    r"\b(?:another|other|a\s+different|a\s+parallel|the\s+other|someone\s+else'?s?)\s+(?:active\s+|running\s+)?"
    r"(?:sessions?|agents?)(?:'s|')?\b", re.I)
_STILL_WORKING = re.compile(r"\b(?:still|is|was|it'?s)\s+(?:working|running|editing|writing|mid-?(?:turn|change|edit))\b", re.I)
_WHEN_SEEN = re.compile(r"\b\d{1,2}:\d{2}\b|\b\d+\s*(?:s|sec|secs|seconds?|min|mins|minutes?)\b", re.I)
_HOPE = re.compile(
    r"\b(?:hopefully|presumably|probably|ought\s+to|is\s+likely\s+to|"
    r"should\s+(?:catch|notice|fix|handle|take\s+care\s+of|sort\s+(?:it|this|them)\s+out|resolve|cover|"
    r"pick\s+(?:it|this|them|these|that)\s+up))\b", re.I)
# Where a card says what is left undone.  Not why/context/answer: those
# mention other sessions for other reasons ("renewing it needs your account,
# which another session's run also uses").
_BLAME_FIELDS = ("headline", "needs.next", "status.remaining", "status.broken", "tried")


def _field_texts(card: dict, names) -> list:
    out = []
    for name in names:
        node = card
        for part in name.split("."):
            node = node.get(part) if isinstance(node, dict) else None
        for v in (node if isinstance(node, list) else [node]):
            if isinstance(v, str) and v.strip():
                out.append((name, " ".join(v.replace("’", "'").split())))
    return out


def _leaves_work(card: dict) -> bool:
    status = card.get("status") if isinstance(card.get("status"), dict) else {}
    return (card.get("type") in ("blocked", "needs_you")
            or any(isinstance(status.get(k), list) and status.get(k) for k in ("remaining", "broken")))


def blame_problems(card: dict, others_working: Optional[list]) -> list:
    """Work left undone because of another session that may have stopped."""
    if not _leaves_work(card):
        return []
    hit = next(((w, m.group(0)) for w, t in _field_texts(card, _BLAME_FIELDS)
                for m in [_OTHER_SESSION.search(t)] if m), None)
    if not hit:
        return []
    where, phrase = hit
    if others_working:                       # someone really is working there now
        return []
    if others_working is not None:           # the engine says nobody is
        return [f"The card leaves work undone because of another session (\"{phrase}\", {where}), but no "
                "other session in this project is working right now. Nothing stands in your way: "
                "finish the work yourself, check it, and end on the card for the finished work."]
    seen = [t for _, t in _field_texts(card, ("tried",)) if _STILL_WORKING.search(t) and _WHEN_SEEN.search(t)]
    if seen:
        return []
    return [f"The card leaves work undone because of another session (\"{phrase}\", {where}). First "
            "check that session is still working: the session list, or when the files it touched last "
            "changed (nothing in 5 minutes means it has stopped). If it has stopped, the work is yours: "
            "finish it and check it. If it is still working, add a \"tried\" line saying what you saw "
            "and when, e.g. \"Checked 8:11 PM: still working, last edit 8:10\"."]


def hope_problems(card: dict) -> list:
    """A Next line that only hopes someone else will act."""
    needs = card.get("needs") if isinstance(card.get("needs"), dict) else {}
    nxt = needs.get("next")
    if not isinstance(nxt, str):
        return []
    m = _HOPE.search(nxt.replace("’", "'"))
    if not m:
        return []
    return [f"needs.next only hopes (\"{m.group(0)}\"): it should say who does what next, and when. "
            "If the thing left is yours to do, do it now. If nothing will happen on its own, say so "
            "plainly. If only the user can do it, end on \"needs_you\" with the step."]


def redo_kind_problems(card: dict, facts: Optional[TurnFacts]) -> list:
    """His latest message is the "Redo as…" button on the newest card
    (last-card.js): the reply must end on the kind he picked.  Two honest
    exceptions: work he asked to have done that is still running in the
    background (a Working card), and a Done or Deliverable that something
    outside the session's control stops (a Blocked card)."""
    if facts is None or not isinstance(card, dict):
        return []
    from daemon import handoff_rules  # local: handoff_rules imports this module
    want = handoff_rules.redo_wanted(facts.last_user)
    if not want or handoff_rules.card_is_kind(card, want):
        return []
    t = card.get("type")
    if t == "working" and want in ("done", "deliverable") and facts.background_live is not False:
        return []
    if t == "blocked" and want in ("done", "deliverable", "working"):
        return []
    row = next(r for r in handoff_rules.CARD_KINDS if r[0] == want)
    shape = f"type \"{row[1]}\"" + (f" with needs.kind \"{row[2]}\"" if row[2] else "")
    return [f"The user tapped \"Redo as {row[3]}\" on your last card ({row[4].lower()}), and this "
            f"reply ends on a \"{t}\" card. End it on {shape}, doing whatever work that kind needs "
            "first: a Done means doing the work, a Decide means showing the options."]


def problems(card: dict, facts: Optional[TurnFacts]) -> list:
    if not isinstance(card, dict):
        return []
    out = (standalone_problems(card) + hope_problems(card)
           + blame_problems(card, getattr(facts, "others_working", None) if facts is not None else None))
    if facts is None:
        return out
    return (out + claim_problems(card, facts) + request_problems(card, facts) + reviewer_problems(card, facts)
            + working_problems(card, facts) + redo_kind_problems(card, facts))
