"""Code Pipeline: four places where a checker's wording could still pass for
proof are closed, each shown failing on the violation next to the valid case.

1. A NOTE is free of evidence only when it states a non-correctness class
   (``cosmetic:`` / ``enhancement:`` / ``preference:``); any other NOTE is
   held as a finding, whatever its words, and the audit must name each
   classed note so the claim is looked at.
2. The audit's coverage section reaches exactly one valid verdict: two
   verdicts, a contradiction, or a malformed one cannot pass.
3. One id names one issue: a second, different issue under an id already used
   is reported instead of silently dropped.
4. A plan names its failure scenarios or declares ``NO-SCENARIOS`` with a
   reason the audit names; silence no longer looks like having thought it
   through.

The domain is the fixtures' toy ``sub()`` helper: unrelated to any real feature.
"""

from __future__ import annotations

from pipeline import findings, harness
from tests.test_code_pipeline import (COVERAGE_OK, NO_SCENARIOS, PLAN_BLOCK, _audit, _replan, codes,  # noqa: F401
                                      make_valid_run, record_baseline, repo)
from tests.test_code_pipeline_findings import Run, audit_text

XDIST_SPLITTABLE = True


def _fmt(v):
    return harness.format_verdict(v)


# ── 1. a NOTE must state its class ──────────────────────────────────────────

def test_an_unclassed_note_with_no_alarm_words_is_still_a_finding(repo):
    # No keyword ("race", "stale", "wrong" ...) appears: the old filter let it go.
    r = Run(repo, review="# 03 Review\n\nNOTE: keep-choice the retry silently discards the previous selection\n",
            red=False, covers=None, audit=audit_text(names=("keep-choice",)))
    v = r.grade()
    assert any(f.code == "finding-unresolved" and "keep-choice" in f.message for f in v.findings), _fmt(v)
    assert v.grade == "MEDIUM"


def test_a_classed_cosmetic_note_must_be_named_by_the_audit(repo):
    r = Run(repo, review="# 03 Review\n\nNOTE: doc-wording cosmetic: the docstring could name the operands\n",
            red=False, covers=None, audit=audit_text(names=()))
    v = r.grade()
    assert any(f.code == "coverage" and "doc-wording" in f.message for f in v.findings), _fmt(v)
    assert v.grade == "MEDIUM"


def test_the_note_class_must_open_the_note():
    assert findings.note_is_free("cosmetic: rename the helper")
    assert findings.note_is_free("(enhancement) - a bulk mode would help")
    assert not findings.note_is_free("rename the helper, purely cosmetic")
    assert not findings.note_is_free("cosmetic: the total can be wrong")   # classed, but about correctness


# ── 2. exactly one valid coverage verdict ───────────────────────────────────

def _coverage(verdicts: str) -> str:
    return COVERAGE_OK.replace("Verdict: covered\n\n", verdicts)


def test_one_covered_verdict_grades_high(repo):
    run_dir, sess = make_valid_run(repo)
    _audit(run_dir, _coverage("Verdict: covered\n\n"), sess)
    assert harness.grade_run(run_dir).grade == "HIGH"


def test_a_section_that_says_covered_and_then_not_covered_is_not_high(repo):
    run_dir, sess = make_valid_run(repo)
    _audit(run_dir, _coverage("Verdict: covered\n\nOn reflection the negative case is untested.\n\n"
                              "Verdict: not covered\n\n"), sess)
    v = harness.grade_run(run_dir)
    assert "coverage" in codes(v) and v.grade == "MEDIUM", _fmt(v)


def test_two_identical_verdicts_are_still_not_exactly_one(repo):
    run_dir, sess = make_valid_run(repo)
    _audit(run_dir, _coverage("Verdict: covered\n\nVerdict: covered\n\n"), sess)
    v = harness.grade_run(run_dir)
    assert any(f.code == "coverage" and "exactly one" in f.message for f in v.findings), _fmt(v)


def test_a_malformed_verdict_is_not_covered(repo):
    run_dir, sess = make_valid_run(repo)
    _audit(run_dir, _coverage("Verdict: mostly covered\n\n"), sess)
    v = harness.grade_run(run_dir)
    assert any(f.code == "coverage" and "neither" in f.message for f in v.findings), _fmt(v)
    _audit(run_dir, _coverage("Verdict: covered, except the negative case\n\n"), sess)
    assert "coverage" in codes(harness.grade_run(run_dir))


# ── 3. one id, one issue ────────────────────────────────────────────────────

def test_two_different_findings_under_one_id_are_reported(repo):
    r = Run(repo, review="# 03 Review\n\nFINDING: neg-sign sub() can return the wrong sign\n"
                         "FINDING: neg-sign sub() crashes on None\n", test="FIXED: neg-sign by=neg\n")
    v = r.grade()
    assert "finding-id-collision" in codes(v) and v.grade == "MEDIUM", _fmt(v)


def test_a_finding_reusing_a_scenario_id_keeps_the_scenario_and_is_reported(repo):
    r = Run(repo, scenarios="SCENARIO: neg-sign sub() keeps the sign of a negative result\n",
            review="# 03 Review\n\nFINDING: neg-sign sub() overflows on huge inputs\n", red=False)
    v = r.grade()
    assert "finding-id-collision" in codes(v), _fmt(v)
    kinds = {o["id"]: o["kind"] for o in v.facts["register"]}
    assert kinds["neg-sign"] == "scenario"


def test_a_note_and_a_finding_sharing_an_id_are_reported():
    raises = findings.raises_in("FINDING: x1 wrong total\nNOTE: x1 cosmetic: rename it\n", "03-review.md")
    assert findings.collisions(raises)


def test_naming_the_same_issue_twice_is_not_a_collision(repo):
    line = "FINDING: neg-sign sub() can return the wrong sign\n"
    r = Run(repo, review="# 03 Review\n\n" + line + "\nRecap:\n" + line, test="FIXED: neg-sign by=neg\n")
    v = r.grade()
    assert "finding-id-collision" not in codes(v) and v.grade == "HIGH", _fmt(v)


# ── 4. scenarios named, or none declared with a reason ──────────────────────

WITHOUT_DECLARATION = PLAN_BLOCK[len(NO_SCENARIOS) + 1:]


def _replanned(repo, block: str, coverage: str = COVERAGE_OK, route: str = "a"):
    run_dir, sess = make_valid_run(repo, route=route)
    tester = _replan(run_dir, sess, block)
    if route != "d":
        record_baseline(tester, run_dir, "unit")
    _audit(run_dir, coverage, sess)
    return harness.grade_run(run_dir)


def test_a_declared_no_scenarios_plan_named_by_the_audit_grades_high(repo):
    v = _replanned(repo, PLAN_BLOCK)
    assert v.grade == "HIGH", _fmt(v)


def test_a_plan_with_neither_scenarios_nor_a_declaration_is_not_high(repo):
    v = _replanned(repo, WITHOUT_DECLARATION)
    assert "scenarios-undeclared" in codes(v) and v.grade == "MEDIUM", _fmt(v)


def test_a_declaration_without_a_reason_is_not_enough(repo):
    v = _replanned(repo, "NO-SCENARIOS: none\n\n" + WITHOUT_DECLARATION)
    assert any(f.code == "scenarios-undeclared" and "reason" in f.message for f in v.findings), _fmt(v)


def test_the_audit_must_name_the_declaration(repo):
    v = _replanned(repo, PLAN_BLOCK, coverage=COVERAGE_OK.replace(" NO-SCENARIOS holds for a pure helper.", ""))
    assert any(f.code == "coverage" and "NO-SCENARIOS" in f.message for f in v.findings), _fmt(v)


def test_a_cleanup_route_needs_no_scenarios(repo):
    v = _replanned(repo, WITHOUT_DECLARATION, route="d")
    assert "scenarios-undeclared" not in codes(v), _fmt(v)
