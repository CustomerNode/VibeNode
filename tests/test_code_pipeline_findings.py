"""Code Pipeline: a suspected defect or a planned scenario is settled by
evidence, never by a checker's word.

The register (``pipeline/findings.py``) closes two gaps in the old
``BLOCKING`` / ``RESOLVED`` gate:

* a checker decided whether the gate applied at all: a correctness concern
  written as "non-blocking" (or as a cosmetic note) needed nothing;
* a later checker's ``RESOLVED:`` closed a finding with nothing behind it.

Every guard here is shown failing on the violation it exists for, next to the
valid case that passes.  The domain is the fixture's toy ``sub()`` helper and
its sign handling: deliberately unrelated to any real feature.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from pipeline import deliver as deliver_mod
from pipeline import findings, harness, plan, start
from tests.test_code_pipeline import (AUDIT_TEXT, PLAN_BLOCK, PY, SUB_TEST, FakeSession, _attest, _write_stage,
                                      codes, record_baseline, record_check, repo)  # noqa: F401 (repo is a fixture)

XDIST_SPLITTABLE = True

GOOD = "def add(a, b):\n    return a + b\n\n\ndef sub(a, b):\n    return a - b\n"
# The planted defect: the sign is lost when the result is negative.
BAD = "def add(a, b):\n    return a + b\n\n\ndef sub(a, b):\n    return abs(a - b)\n"
NEG_TEST = "import thing\nassert thing.sub(1, 2) == -1, 'wrong sign for a negative result'\nprint('neg ok')\n"


def plan_block(covers: str | None = "neg-sign") -> str:
    """The fixture's plan plus a ``neg`` check that exercises the sign."""
    item = "- id: neg\n  cmd: {python} test_neg.py\n  expect: neg ok\n"
    if covers:
        item += f"  covers: {covers}\n"
    return PLAN_BLOCK[:-3] + item + "```"


def audit_text(names=("neg-sign",), extra: str = "") -> str:
    """The fixture's audit, its coverage section naming every register id."""
    named = "neg asserts the sign of a negative result.\n"
    named += "".join(f"{n}: the neg check calls sub() with a negative result and asserts the sign.\n" for n in names)
    return AUDIT_TEXT.replace("Verdict: covered", named + "\nVerdict: covered") + extra


class Run:
    """One run in the CLI's shape: plan, review, (red run, fix), test, audit."""

    def __init__(self, repo: Path, *, review: str = "# 03 Review\n\nNo findings.\n", test: str = "",
                 audit: str | None = None, covers: str | None = "neg-sign", scenarios: str = "", red: bool = True):
        self.repo = repo
        self.run_dir, run = start.create_run(repo, "sub-sign", "a", "Add a sub() helper beside add().")
        (repo / "thing.py").write_text(BAD if red else GOOD, encoding="utf-8")
        (repo / "test_sub.py").write_text(SUB_TEST, encoding="utf-8")
        (repo / "test_neg.py").write_text(NEG_TEST, encoding="utf-8")
        (repo / "daemon" / "core.py").write_text("X = 2\n", encoding="utf-8")  # full depth: Review is required
        self.sess = sess = FakeSession(repo)
        sess.bash_root(f"{PY} -m pipeline.start --repo . --slug sub-sign --route a --request-file req.txt",
                       stdout=start.start_line(run, self.run_dir) + "\n")
        self.plan_text = "# 01 Plan\n\nAdd sub().\n\n" + scenarios + "\n" + plan_block(covers) + "\n"
        self.write_plan(self.plan_text)
        build = "# 02 Build\n\nImplemented sub(a, b) in thing.py.\n"
        (self.run_dir / "02-build.md").write_text(build, encoding="utf-8")
        sess.write_root(self.run_dir / "02-build.md", build)
        self.brief = f"Original request: Add a sub() helper beside add().\nRun directory: {self.run_dir}\n"
        self.reviewer = sess.agent(self.brief)
        self.write(self.reviewer, "03-review.md", review)
        if red:
            # The defect is shown failing on the defective tree, then fixed.
            first = sess.agent(self.brief)
            assert not record_check(first, self.run_dir, "neg")["passed"]
            (repo / "thing.py").write_text(GOOD, encoding="utf-8")
            _attest(self.reviewer, self.run_dir, "03-review.md")  # the reviewer looks again at the final tree
        self.tester = sess.agent(self.brief)
        self.check_all(self.tester)
        self.write(self.tester, "04-test.md", "# 04 Test\n\nunit, lint and neg ran through run_check.\n" + test)
        self.auditor = sess.agent(self.brief)
        self.write(self.auditor, "08-audit.md", audit if audit is not None else audit_text())

    def write_plan(self, text: str) -> None:
        (self.run_dir / "01-plan.md").write_text(text, encoding="utf-8")
        self.sess.write_root(self.run_dir / "01-plan.md", text)

    def write(self, agent, stage: str, text: str) -> None:
        (self.run_dir / stage).write_text(text, encoding="utf-8")
        agent.write(self.run_dir / stage, text)
        _attest(agent, self.run_dir, stage)

    def check_all(self, tester) -> None:
        for c in plan.load_plan(self.run_dir)[0]:
            if c.executable:
                record_check(tester, self.run_dir, c.id)
        record_baseline(tester, self.run_dir, "unit")

    def grade(self) -> harness.Verdict:
        return harness.grade_run(self.run_dir)


RAISED = "# 03 Review\n\nFINDING: neg-sign sub() can return the wrong sign when the result is negative\n"


def assert_high(v: harness.Verdict) -> None:
    assert v.grade == "HIGH", harness.format_verdict(v)


# ── the valid cases: settled by evidence ─────────────────────────────────────

def test_a_finding_fixed_with_a_failing_then_passing_check_grades_high(repo):
    r = Run(repo, review=RAISED, test="FIXED: neg-sign by=neg\n")
    v = r.grade()
    assert_high(v)
    assert v.facts["outcome"]["fixed"] == ["neg-sign"]
    assert "register: 1 item(s): 1 fixed" in harness.format_verdict(v)


def test_a_finding_shown_not_to_be_a_defect_by_a_passing_check_grades_high(repo):
    r = Run(repo, review=RAISED, test="NOT-A-DEFECT: neg-sign by=neg\n", red=False)
    v = r.grade()
    assert_high(v)
    assert v.facts["outcome"]["not-a-defect"] == ["neg-sign"]


def test_a_planned_scenario_with_a_passing_covering_check_is_verified(repo):
    r = Run(repo, scenarios="SCENARIO: neg-sign sub() keeps the sign of a negative result\n", red=False)
    v = r.grade()
    assert_high(v)
    assert v.facts["outcome"]["verified"] == ["neg-sign"]


def test_a_cosmetic_note_needs_no_evidence(repo):
    r = Run(repo, review="# 03 Review\n\nNOTE: doc-wording cosmetic: the docstring could name the operands\n",
            covers=None, red=False, audit=audit_text(names=("doc-wording",)))
    assert_high(r.grade())


# ── 1. a resolution with no evidence ─────────────────────────────────────────

def test_resolved_alone_settles_nothing(repo):
    r = Run(repo, review=RAISED, test="RESOLVED: neg-sign the sign is right now\n")
    v = r.grade()
    assert "resolution-unproven" in codes(v) and v.grade == "MEDIUM", harness.format_verdict(v)


def test_fixed_without_evidence_settles_nothing(repo):
    r = Run(repo, review=RAISED, test="FIXED: neg-sign changed abs() to a plain subtraction\n")
    v = r.grade()
    assert "resolution-unproven" in codes(v) and v.grade == "MEDIUM", harness.format_verdict(v)


# ── 2. a fabricated or missing evidence reference ────────────────────────────

def test_a_disposition_citing_a_check_that_does_not_exist_is_rejected(repo):
    r = Run(repo, review=RAISED, test="FIXED: neg-sign by=sign-proof\n")
    v = r.grade()
    assert "evidence-missing" in codes(v) and v.grade == "MEDIUM", harness.format_verdict(v)


# ── 3. evidence from an obsolete code version ────────────────────────────────

def test_evidence_from_before_the_last_code_change_does_not_settle(repo):
    r = Run(repo, review=RAISED, test="FIXED: neg-sign by=neg\n")
    assert_high(r.grade())
    # The code changes after neg ran; every OTHER check is run again, so the
    # only thing left resting on the old tree is the evidence cited for the fix.
    (repo / "thing.py").write_text(GOOD + "\n\ndef mul(a, b):\n    return a * b\n", encoding="utf-8")
    tester = r.sess.agent(r.brief)
    for cid in ("unit", "lint"):
        record_check(tester, r.run_dir, cid)
    v = r.grade()
    assert any(f.code == "evidence-stale" and "neg-sign" in f.message for f in v.findings), harness.format_verdict(v)
    assert v.grade != "HIGH"


# ── 4. an unrelated passing check offered as resolution ─────────────────────

def test_an_unrelated_passing_check_does_not_settle_a_finding(repo):
    """unit is required and passes on the final tree, but its plan entry does
    not declare that it covers the finding."""
    r = Run(repo, review=RAISED, test="NOT-A-DEFECT: neg-sign by=unit\n", red=False)
    v = r.grade()
    assert any(f.code == "evidence-unrelated" and "covers: neg-sign" in f.message for f in v.findings), \
        harness.format_verdict(v)
    assert v.grade == "MEDIUM"


def test_an_optional_check_does_not_settle_a_finding(repo):
    """lint is optional: a failure of it would be noted, never fatal, so it
    cannot carry a finding's settlement."""
    r = Run(repo, review=RAISED, test="NOT-A-DEFECT: neg-sign by=lint\n", red=False)
    v = r.grade()
    assert any(f.code == "evidence-unrelated" and "not a required" in f.message for f in v.findings), \
        harness.format_verdict(v)


def test_a_check_that_never_failed_does_not_prove_a_fix(repo):
    """FIXED needs the before half: the covering check failed on an earlier
    tree.  One that only ever passed proves the code is right now, not that
    the defect existed and was corrected."""
    r = Run(repo, review=RAISED, test="FIXED: neg-sign by=neg\n", red=False)
    v = r.grade()
    assert "fix-unproven" in codes(v) and v.grade == "MEDIUM", harness.format_verdict(v)


def test_a_hand_written_failure_record_is_not_a_before_run(repo):
    r = Run(repo, review=RAISED, test="FIXED: neg-sign by=neg\n", red=False)
    hist = r.run_dir / "evidence" / "history"
    fake = json.loads((r.run_dir / "evidence" / "neg.json").read_text(encoding="utf-8"))
    fake.update({"passed": False, "exit_code": 1, "fingerprint": "0" * 64})
    (hist / "neg.0123456789abcdef.json").write_text(json.dumps(fake), encoding="utf-8")
    assert "fix-unproven" in codes(r.grade())


def test_a_pinned_project_check_cannot_declare_covers():
    try:
        plan.check_pinnable(plan.parse_criteria("- id: suite\n  cmd: x\n  covers: neg-sign\n"))
    except plan.PlanError as e:
        assert "covers" in str(e)
    else:
        raise AssertionError("a pinned check declared covers")


# ── 5. reclassifying a correctness concern to escape the gate ───────────────

def test_a_correctness_concern_labelled_as_a_note_is_still_accountable(repo):
    r = Run(repo, review="# 03 Review\n\nNOTE: neg-sign minor: sub() can return the wrong sign\n",
            red=False)
    v = r.grade()
    assert any(f.code == "finding-unresolved" and "neg-sign" in f.message for f in v.findings), \
        harness.format_verdict(v)
    assert v.grade == "MEDIUM"


def test_an_unstructured_non_blocking_observation_is_flagged(repo):
    r = Run(repo, review="# 03 Review\n\n- Non-blocking: sub() can return the wrong sign\n", red=False,
            audit=audit_text(names=()), covers=None)
    v = r.grade()
    assert "observation-unstructured" in codes(v) and v.grade == "MEDIUM", harness.format_verdict(v)
    # "Non-blocking: none" says there is nothing to track.
    assert not findings.unstructured_observations("Non-blocking: none\n")


def test_rewriting_a_finding_as_a_cosmetic_note_does_not_drop_it(repo):
    r = Run(repo, review=RAISED, red=False)
    r.write(r.reviewer, "03-review.md", "# 03 Review\n\nNOTE: neg-sign style only\n")
    v = r.grade()
    assert any(f.code in ("finding-dropped", "finding-unresolved") and "neg-sign" in f.message
               for f in v.findings), harness.format_verdict(v)
    assert v.grade != "HIGH"


def test_verified_is_not_a_disposition_for_a_finding(repo):
    r = Run(repo, review=RAISED, test="VERIFIED: neg-sign by=neg\n", red=False)
    assert "disposition-invalid" in codes(r.grade())


# ── 6. no unqualified HIGH with an unresolved concern ───────────────────────

def test_an_unresolved_finding_caps_the_grade_and_travels_with_the_delivery(repo):
    r = Run(repo, review=RAISED, test="UNRESOLVED: neg-sign could not reproduce; a negative result may lose its sign\n",
            red=False)
    v = r.grade()
    assert "finding-unresolved" in codes(v) and v.grade == "MEDIUM", harness.format_verdict(v)
    assert v.facts["outcome"]["unresolved"] == ["neg-sign"]
    rec, delivered = deliver_mod.deliver(r.run_dir)
    assert not delivered
    assert rec["unsettled"] == [{"id": "neg-sign", "kind": "finding", "state": "unresolved",
                                 "text": "sub() can return the wrong sign when the result is negative"}]


def test_a_finding_nobody_settles_caps_the_grade(repo):
    r = Run(repo, review=RAISED, red=False)
    assert "finding-unresolved" in codes(r.grade())


# ── invariants need executable evidence ─────────────────────────────────────

def test_an_invariant_asserted_in_prose_does_not_settle_a_finding(repo):
    """'Callers never pass a larger second operand' is an argument, not a
    check: no criterion would fail if the invariant were violated."""
    r = Run(repo, review=RAISED,
            test="NOT-A-DEFECT: neg-sign callers never pass a larger second operand, so it cannot happen\n",
            red=False)
    v = r.grade()
    assert "resolution-unproven" in codes(v) and v.grade == "MEDIUM", harness.format_verdict(v)


# ── planned scenarios cannot silently disappear ─────────────────────────────

def test_a_planned_scenario_with_no_covering_check_is_an_open_obligation(repo):
    r = Run(repo, scenarios="SCENARIO: neg-sign sub() keeps the sign of a negative result\n", covers=None,
            red=False)
    v = r.grade()
    assert "obligation-open" in codes(v) and v.grade == "MEDIUM", harness.format_verdict(v)


def test_removing_a_planned_scenario_from_the_plan_is_flagged(repo):
    r = Run(repo, scenarios="SCENARIO: neg-sign sub() keeps the sign of a negative result\n", red=False,
            audit=audit_text(names=()))
    r.write_plan(r.plan_text.replace("SCENARIO: neg-sign sub() keeps the sign of a negative result\n", ""))
    v = r.grade()
    assert "obligation-dropped" in codes(v), harness.format_verdict(v)


def test_an_unresolved_scenario_caps_the_grade(repo):
    r = Run(repo, scenarios="SCENARIO: neg-sign sub() keeps the sign of a negative result\n", covers=None,
            test="UNRESOLVED: neg-sign no check exercises negative results yet\n", red=False)
    v = r.grade()
    assert "obligation-unresolved" in codes(v) and v.grade == "MEDIUM", harness.format_verdict(v)


# ── the audit must account for every item ────────────────────────────────────

def test_the_audit_must_name_every_register_item(repo):
    r = Run(repo, review=RAISED, test="FIXED: neg-sign by=neg\n", audit=audit_text(names=()))
    v = r.grade()
    assert any(f.code == "coverage" and "neg-sign" in f.message for f in v.findings), harness.format_verdict(v)


REGISTER_CODES = ("resolution-unproven", "evidence-missing", "evidence-unrelated", "evidence-stale", "fix-unproven",
                  "disposition-invalid", "obligation-open", "obligation-unresolved", "obligation-dropped",
                  "observation-unstructured", "finding-unresolved", "finding-id-collision", "scenarios-undeclared")


@pytest.mark.parametrize("code", REGISTER_CODES)
def test_every_register_finding_has_its_own_words_on_a_card_that_passes_the_validators(code):
    """The delivery card names the worst finding in plain words; a register
    finding must not read as the generic 'Not every check passed'."""
    from daemon import handoff_rules, smart_termination
    from pipeline import cards
    assert code in cards.WORDS and code in cards.PRIORITY
    verdict = {"grade": "MEDIUM", "criteria": [], "findings": [{
        "code": code, "severity": "medium",
        "message": "04-test.md: FIXED: neg-sign cites neg, but a disposition needs by=<criterion> and covers: neg-sign"}]}
    card, _ = cards.delivery_card(verdict, "commit", "ab12cd34", "Add a sub() helper")
    msg = cards.fenced(card)
    assert handoff_rules.review(msg) is None, handoff_rules.review(msg)
    assert smart_termination.review(msg, continued=False, sendbacks=0) is None
    assert card["title"] == cards.WORDS[code][0]


def test_the_parser_reads_the_structured_lines():
    text = ("FINDING: a1 wrong total\n**BLOCKING:** b2 crash on empty input\nNOTE: c3 cosmetic: rename the helper\n"
            "NOTE: d4 cosmetic: the cache can go stale\nNOTE: e5 the retry keeps the old choice\n"
            "FIXED: a1 by=t1,t2\nNOT A DEFECT: b2 by=t3\nUNRESOLVED: x9 unknown\n")
    # c3 states a class and reads as cosmetic; d4 states one but is worded as
    # a correctness concern; e5 states none, so it is held whatever its words.
    assert findings.accountable_ids(text) == ["a1", "b2", "d4", "e5"]
    assert findings.free_note_ids(text) == ["c3"]
    ds = {d.id: (d.kind, d.by) for d in findings.dispositions_in(text, "04-test.md")}
    assert ds == {"a1": ("fixed", ["t1", "t2"]), "b2": ("not-a-defect", ["t3"]), "x9": ("unresolved", [])}
    assert plan.parse_criteria("- id: t\n  cmd: x\n  covers: a1, b2\n")[0].covers_ids() == ["a1", "b2"]
