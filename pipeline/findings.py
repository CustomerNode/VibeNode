"""The register: every suspected correctness defect and every planned failure
scenario stays accountable until it reaches a disposition backed by evidence.

A checker's prose is not a disposition.  Before this register, a reviewer
could call a correctness concern "non-blocking" (it then needed nothing), and
a later checker could write ``RESOLVED: <id>`` with nothing behind it (the
grader accepted the word).  Both are closed here, as structured lines the
harness reads:

Raised (each id is persistent; one namespace per run):

* ``SCENARIO: <id> <what must hold>`` in the plan file: a consequential
  failure scenario the plan names before building.  It is a verification
  obligation, not a defect.
* ``FINDING: <id> <what is wrong>`` (``BLOCKING:`` is the same line) in a
  checker report: a suspected correctness defect.
* ``NOTE: <id> cosmetic: <text>`` (or ``enhancement:`` / ``preference:``):
  an observation that is NOT about correctness.  It needs no evidence, but
  the class is an explicit claim: the audit must name the id and may
  challenge it.  A NOTE without one of those classes is held accountable
  like a finding, whatever its words: the cheap path is a stated claim, not
  a missing keyword.  A classed note whose text still uses correctness
  vocabulary (a race, lost or stale data, a crash, wrong output ...) is held
  accountable too.
* ``NO-SCENARIOS: <why none apply>`` in the plan file, when the plan names
  no SCENARIO: an affirmative decision, not a silence.  The audit must name
  it and judge the reason.

One id, one issue.  An id raised twice for two different things (two
FINDING lines, a FINDING and a SCENARIO, a NOTE and a FINDING with different
text) is a collision: the second issue would otherwise vanish, so the harness
reports it and the grade cannot be HIGH until the ids are made distinct.

Dispositions (in a checker report; for a finding, a LATER checker stage than
the one that raised it, as before):

* ``VERIFIED: <id> by=<criterion>[,<criterion>]``: a scenario holds.
* ``FIXED: <id> by=<criterion>``: a defect was real, was corrected, and the
  criterion shows it: it FAILED on an earlier tree (a recorded run_check
  execution, or its baseline demonstration) and passes on the final tree.
* ``NOT-A-DEFECT: <id> by=<criterion>``: the suspected failure was exercised
  by an executable check that passes on the final tree.  An explanation
  alone, or an invariant asserted in prose, is not a disposition: if the
  claim cannot be tested, the honest disposition is UNRESOLVED.
* ``UNRESOLVED: <id> <what is unknown and what it could cost>``: disclosed,
  and the grade cannot be HIGH.
* ``RESOLVED: <id>`` is the old word.  It closes nothing.

What the harness checks mechanically: the id, the permitted disposition,
that every criterion named in ``by=`` exists in the plan, is required and
executable, is not a pinned project check, declares ``covers: <id>`` in the
verification block, and passed on the final tree with recorded evidence; for
FIXED, a recorded failure of that criterion on an earlier tree.  What it
cannot check is whether the criterion really exercises the concern: the
plan's ``covers`` is a claim, and the audit must name every id in its
coverage section and challenge it there.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

ID_PATTERN = r"[A-Za-z0-9][A-Za-z0-9_.-]*"
_LEAD = r"^[ \t>*_-]*"
_SEP = r"[ \t*_]*:[ \t*_]*"

SCENARIO_RE = re.compile(_LEAD + r"SCENARIO" + _SEP + rf"({ID_PATTERN})[ \t]*(.*)$", re.MULTILINE)
FINDING_RE = re.compile(_LEAD + r"(?:BLOCKING|FINDING)" + _SEP + rf"({ID_PATTERN})[ \t]*(.*)$", re.MULTILINE)
NOTE_RE = re.compile(_LEAD + r"NOTE" + _SEP + rf"({ID_PATTERN})[ \t]*(.*)$", re.MULTILINE)
DISPOSITION_RE = re.compile(
    _LEAD + r"(FIXED|NOT[- ]A[- ]DEFECT|VERIFIED|UNRESOLVED|RESOLVED)" + _SEP + rf"({ID_PATTERN})[ \t]*(.*)$",
    re.MULTILINE)
BY_RE = re.compile(r"\bby[ \t]*=[ \t]*([A-Za-z0-9][A-Za-z0-9_.,-]*)")

# Words that make a NOTE a correctness concern.  Deliberately about outcomes
# (what a user or the data would suffer), not about style.
CORRECTNESS_RE = re.compile(
    r"\b(?:race|racy|out[- ]of[- ]order|reorder\w*|concurren\w*|data[- ]loss|los[est]|lost|corrupt\w*|"
    r"inconsisten\w*|crash\w*|incorrect\w*|wrong|stale|overwrit\w*|deadlock\w*|leak\w*|vulnerab\w*|"
    r"inject\w*|bypass\w*|bug|bugs|defect\w*|regress\w*|broken|desync\w*|out of sync|unsafe)\b",
    re.IGNORECASE)

# The classes that make a NOTE free of evidence: it must open with one of them.
NOTE_CLASS_RE = re.compile(r"^[ \t*_(\[]*(cosmetic|enhancement|preference)[ \t*_)\]]*[:\-–—][ \t]*",
                           re.IGNORECASE)
NO_SCENARIOS_RE = re.compile(_LEAD + r"NO[- ]SCENARIOS" + _SEP + r"(.*)$", re.MULTILINE)
NO_SCENARIOS_ID = "NO-SCENARIOS"
NO_SCENARIOS_MIN_WORDS = 4

# A checker line that labels an observation "non-blocking" without one of the
# structured forms above is how a concern escaped the gate.  "Non-blocking:
# none" says there is nothing, and passes.
UNSTRUCTURED_RE = re.compile(
    r"^[ \t>*_#-]*(?:\d+[.)][ \t]*)?[*_(\[ \t]*non[- ]?blocking\b(?![^\n]*\bnone\b)[^\n]*$",
    re.IGNORECASE | re.MULTILINE)

DISPOSITIONS = ("verified", "fixed", "not-a-defect", "unresolved", "resolved")
CLOSED = ("verified", "fixed", "not-a-defect")


@dataclass
class Entry:
    """One accountable item: a planned scenario or a suspected defect."""

    id: str
    kind: str          # "scenario" | "finding"
    stage: str         # the file that raised it
    text: str = ""
    promoted: bool = False   # a NOTE held accountable for its wording
    dropped: bool = False    # raised in an earlier signed version, absent now


@dataclass
class Disposition:
    id: str
    kind: str          # one of DISPOSITIONS
    stage: str
    text: str = ""
    by: list[str] = field(default_factory=list)
    pos: int = 0       # position in the stage file (later wins within a stage)


@dataclass
class Outcome:
    """Where one entry stands, and the finding the harness reports for it."""

    id: str
    kind: str
    state: str                 # verified | fixed | not-a-defect | unresolved | open | invalid
    code: str = ""             # the harness finding code when not closed
    message: str = ""
    by: list[str] = field(default_factory=list)
    text: str = ""
    stage: str = ""


def _norm_kind(word: str) -> str:
    w = word.lower().replace(" ", "-")
    return "not-a-defect" if w.startswith("not") else w


def scenarios_in(text: str, stage: str) -> list[Entry]:
    out: dict[str, Entry] = {}
    for m in SCENARIO_RE.finditer(text or ""):
        out.setdefault(m.group(1), Entry(m.group(1), "scenario", stage, m.group(2).strip()))
    return list(out.values())


def note_is_free(text: str) -> bool:
    """A NOTE needs no evidence only when it states a non-correctness class
    and its words are not about correctness."""
    m = NOTE_CLASS_RE.match(text or "")
    return bool(m) and not CORRECTNESS_RE.search(text[m.end():])


def findings_in(text: str, stage: str) -> list[Entry]:
    """Accountable findings in a checker report: FINDING/BLOCKING lines, and
    every NOTE that does not state a non-correctness class (or does, but is
    worded as a correctness concern)."""
    out: dict[str, Entry] = {}
    for m in FINDING_RE.finditer(text or ""):
        out.setdefault(m.group(1), Entry(m.group(1), "finding", stage, m.group(2).strip()))
    for m in NOTE_RE.finditer(text or ""):
        if m.group(1) not in out and not note_is_free(m.group(2)):
            out[m.group(1)] = Entry(m.group(1), "finding", stage, m.group(2).strip(), promoted=True)
    return list(out.values())


def free_note_ids(text: str) -> list[str]:
    """The NOTE ids that claim a non-correctness class (the audit must name
    each one, so the claim is looked at, not taken)."""
    return sorted({m.group(1) for m in NOTE_RE.finditer(text or "") if note_is_free(m.group(2))})


def _norm(text: str) -> str:
    return re.sub(r"\s+", " ", text or "").strip().rstrip(".;:").lower()


def raises_in(text: str, stage: str) -> list[tuple[str, str, str, str]]:
    """Every raising line, undeduplicated: (id, kind, stage, text), kind one
    of scenario / finding / note."""
    out = []
    for rx, kind in ((SCENARIO_RE, "scenario"), (FINDING_RE, "finding"), (NOTE_RE, "note")):
        for m in rx.finditer(text or ""):
            out.append((m.group(1), kind, stage, m.group(2).strip()))
    return out


def collisions(raises) -> list[str]:
    """Messages for ids raised for more than one issue.  Repeating a line
    word for word (the same issue named again) is not a collision; a second
    text, or a second kind of item, under the same id is."""
    by_id: dict[str, list] = {}
    for rid, kind, stage, text in raises:
        by_id.setdefault(rid, []).append((kind, stage, text))
    out = []
    for rid, items in sorted(by_id.items()):
        distinct = {(k, _norm(t)) for k, _s, t in items}
        if len(distinct) > 1:
            where = "; ".join(f"{s} {k.upper()}: {t[:60]!r}" for k, s, t in items)
            out.append(f"id {rid} is used for more than one issue ({where}); give each issue its own id so none "
                       f"drops out of the register")
    return out


def no_scenarios_reason(plan_text: str) -> str | None:
    """The plan's NO-SCENARIOS justification, or None when it declares none."""
    m = NO_SCENARIOS_RE.search(plan_text or "")
    return m.group(1).strip() if m else None


def accountable_ids(text: str) -> list[str]:
    """The ids a signed report holds accountable (what ``pipeline.attest``
    records, so a rewrite of the report cannot make one disappear)."""
    return sorted({e.id for e in findings_in(text, "")})


def notes_in(text: str) -> list[tuple[str, str]]:
    return [(m.group(1), m.group(2).strip()) for m in NOTE_RE.finditer(text or "")]


def dispositions_in(text: str, stage: str) -> list[Disposition]:
    out = []
    for m in DISPOSITION_RE.finditer(text or ""):
        rest = m.group(3).strip()
        by = []
        b = BY_RE.search(rest)
        if b:
            by = [x for x in b.group(1).split(",") if x]
        out.append(Disposition(m.group(2), _norm_kind(m.group(1)), stage, rest, by, m.start()))
    return out


def unstructured_observations(text: str) -> list[str]:
    """Lines that call something non-blocking outside the structured forms."""
    out = []
    for m in UNSTRUCTURED_RE.finditer(text or ""):
        line = m.group(0).strip()
        if NOTE_RE.match(line) or FINDING_RE.match(line):
            continue
        out.append(line)
    return out


@dataclass
class EvidenceView:
    """What the harness already established about the criteria, in the shape
    the register needs.  ``passed_now`` holds the ids whose current evidence
    passed, was recorded by a run_check call, and ran on the final tree with
    the current plan; ``red`` maps an id to its recorded failing executions
    on an earlier tree; ``baseline_ok`` holds the ids whose baseline
    demonstration held (they failed on the code before the run)."""

    criteria: dict            # id -> pipeline.plan.Criterion
    passed_now: set
    red: dict
    baseline_ok: set
    stale: dict = field(default_factory=dict)   # id -> why the current evidence does not count


def _covering(view: EvidenceView, eid: str) -> list[str]:
    return [c.id for c in view.criteria.values()
            if not c.pinned and c.required and c.executable and eid in c.covers_ids()]


def _check_by(entry: Entry, d: Disposition, view: EvidenceView) -> Outcome | None:
    """The mechanical half of a closing disposition; None when it holds."""
    def bad(code: str, msg: str) -> Outcome:
        return Outcome(entry.id, entry.kind, "invalid", code, msg, d.by, entry.text, entry.stage)

    label = d.kind.upper()
    if not d.by:
        return bad("resolution-unproven",
                   f"{d.stage}: {label}: {entry.id} names no evidence; a disposition needs by=<criterion> "
                   f"(an executable check in the plan that covers {entry.id})")
    for cid in d.by:
        c = view.criteria.get(cid)
        if c is None:
            return bad("evidence-missing",
                       f"{d.stage}: {label}: {entry.id} cites criterion {cid}, which is not in the verification plan")
        if c.pinned:
            return bad("evidence-unrelated",
                       f"{d.stage}: {label}: {entry.id} cites the project's pinned check {cid}; a whole suite "
                       f"passing does not show this concern was exercised")
        if not (c.executable and c.required):
            return bad("evidence-unrelated",
                       f"{d.stage}: {label}: {entry.id} cites {cid}, which is not a required executable check")
        if entry.id not in c.covers_ids():
            return bad("evidence-unrelated",
                       f"{d.stage}: {label}: {entry.id} cites {cid}, whose plan entry does not declare "
                       f"'covers: {entry.id}'; an unrelated passing check is not evidence")
        if cid not in view.passed_now:
            why = view.stale.get(cid) or "it has not passed on the final tree"
            return bad("evidence-stale", f"{d.stage}: {label}: {entry.id} cites {cid}, but {why}")
    if d.kind == "fixed" and not any(view.red.get(cid) or cid in view.baseline_ok for cid in d.by):
        return bad("fix-unproven",
                   f"{d.stage}: FIXED: {entry.id} cites {', '.join(d.by)}, but no recorded execution shows it "
                   f"failing before the fix (run it on the defective tree, or its baseline, through run_check)")
    return None


def evaluate(entry: Entry, eligible: list[Disposition], view: EvidenceView) -> Outcome:
    """Where *entry* stands, given the dispositions that may settle it (in
    order; the last one is the current judgment) and the evidence."""
    base = dict(by=[], text=entry.text, stage=entry.stage)
    what = "scenario" if entry.kind == "scenario" else "finding"
    if not eligible:
        if entry.kind == "scenario":
            cover = [cid for cid in _covering(view, entry.id) if cid in view.passed_now]
            if cover:
                return Outcome(entry.id, entry.kind, "verified", by=cover, text=entry.text, stage=entry.stage)
            return Outcome(entry.id, entry.kind, "open", "obligation-open",
                           f"scenario {entry.id} ({entry.stage}) has no passing check that covers it and no "
                           f"disposition; it is an open verification obligation", **base)
        if entry.dropped:
            return Outcome(entry.id, entry.kind, "open", "finding-dropped",
                           f"{entry.stage} raised {entry.id} in an earlier attested version; it is neither open "
                           f"now nor settled by a later checker stage", **base)
        return Outcome(entry.id, entry.kind, "open", "finding-unresolved",
                       f"{entry.stage} raised {'NOTE (held as a finding for its wording)' if entry.promoted else 'FINDING'}: "
                       f"{entry.id}, and no later checker stage settles it (FIXED or NOT-A-DEFECT with by=<criterion>, "
                       f"or UNRESOLVED)", **base)
    d = eligible[-1]
    if d.kind == "unresolved":
        return Outcome(entry.id, entry.kind, "unresolved",
                       "obligation-unresolved" if entry.kind == "scenario" else "finding-unresolved",
                       f"{what} {entry.id} is UNRESOLVED ({d.stage}): {d.text or 'no reason given'}",
                       by=[], text=entry.text, stage=entry.stage)
    if d.kind == "resolved":
        return Outcome(entry.id, entry.kind, "invalid", "resolution-unproven",
                       f"{d.stage}: RESOLVED: {entry.id} is a statement, not a disposition; record FIXED or "
                       f"NOT-A-DEFECT with by=<criterion> (VERIFIED for a scenario), or UNRESOLVED", **base)
    if d.kind == "verified" and entry.kind == "finding":
        return Outcome(entry.id, entry.kind, "invalid", "disposition-invalid",
                       f"{d.stage}: VERIFIED applies to planned scenarios; finding {entry.id} is FIXED, NOT-A-DEFECT "
                       f"or UNRESOLVED", **base)
    problem = _check_by(entry, d, view)
    if problem is not None:
        return problem
    return Outcome(entry.id, entry.kind, d.kind, by=list(d.by), text=entry.text, stage=entry.stage)
