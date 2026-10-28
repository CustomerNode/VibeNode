"""Code Pipeline: the external review of 2026-10-28 (tree 0bbfce0b087b).

Each test here is the failing case of one finding, and each fails on the
engine as it was when this run started (the regression demonstration runs
this file against that snapshot).  Module-level imports use only names the
old engine has, so the old engine fails each test for its own reason, not at
import.

1. a timed-out baseline demonstration counted;
2. a pinned full-suite check could be waived;
3. the repository checked was read from editable run.json;
6. an ignored conftest.py steered the tests outside the fingerprint;
7. the pipeline implementation that produced evidence was not identified;
8. file modes were not in the fingerprint;
9. the plan parser accepted unknown and duplicate keys;
10. same-minute, same-slug runs collided.
"""

from __future__ import annotations

import json
import subprocess
from datetime import datetime, timezone
from pathlib import Path

import pytest

from pipeline import evidence as ev
from pipeline import harness, plan, start
from pipeline.evidence import evidence_sha, run_criterion, tree_fingerprint
from tests.test_code_pipeline import (COVERAGE_OK, PY, _audit, _git, codes, make_valid_run, record_baseline,
                                      repo)  # noqa: F401  (repo is the fixture)


# ── 1. a baseline that times out is not a demonstration ─────────────────────

def test_a_baseline_that_times_out_after_printing_the_error_is_not_a_demonstration(repo, monkeypatch):
    run_dir, sess = make_valid_run(repo)
    real = ev._execute

    def hangs_after_printing(*a, **k):
        r = list(real(*a, **k))
        r[0], r[3] = -1, True  # exit -1, timed out; the expected error is still in the output
        return tuple(r)

    monkeypatch.setattr(ev, "_execute", hangs_after_printing)
    rec = record_baseline(sess.agent("brief"), run_dir, "unit")
    assert rec["timed_out"] and rec["fails_with_matched"] and not rec["passed"]
    # Restore this one attribute only: monkeypatch.undo() would also undo the
    # conftest's home sandbox, which shares the fixture.
    monkeypatch.setattr(ev, "_execute", real)
    # A fresh audit after the new evidence, so the only thing wrong with the
    # run is the baseline that hung.
    _audit(run_dir, COVERAGE_OK, sess)
    v = harness.grade_run(run_dir)
    graded = {f.code for f in v.findings if f.severity != "info"}
    assert graded and all(c.startswith("baseline-") for c in graded), harness.format_verdict(v)
    assert v.grade == "MEDIUM", harness.format_verdict(v)
    assert "unit" not in v.facts.get("baseline_demonstrated", [])


# ── 2. a pinned check always runs ────────────────────────────────────────────

WAIVED_SUITE = "- id: suite\n  executable: false\n  waiver: true\n  reason: tests unavailable\n"


def test_a_pinned_suite_cannot_be_waived_at_the_start(repo):
    (repo / ".pipeline-verify").write_text(WAIVED_SUITE, encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "waive the suite")
    with pytest.raises(ValueError):
        start.create_run(repo, "waived", "a", "do a thing")


def test_a_pinned_suite_cannot_be_waived_in_the_run(repo):
    run_dir, _ = start.create_run(repo, "waived", "a", "do a thing")
    (run_dir / "pinned-verify.txt").write_text(WAIVED_SUITE, encoding="utf-8")
    with pytest.raises(plan.PlanError):
        plan.load_pinned(run_dir)


# ── 3. the repository is the one the run lives in ────────────────────────────

def _clone(repo: Path, tmp_path: Path) -> Path:
    other = tmp_path / "other-clone"
    subprocess.run(["git", "clone", "-q", str(repo), str(other)], check=True, capture_output=True)
    return other


def test_pointing_run_json_at_another_clone_is_tampering(repo, tmp_path):
    run_dir, _ = make_valid_run(repo)
    other = _clone(repo, tmp_path)
    run = json.loads((run_dir / "run.json").read_text(encoding="utf-8"))
    run["repo"] = str(other)
    (run_dir / "run.json").write_text(json.dumps(run), encoding="utf-8")
    v = harness.grade_run(run_dir)
    assert "repo-tampered" in codes(v) and v.grade == "LOW", harness.format_verdict(v)


def test_run_check_refuses_to_check_another_repository(repo, tmp_path):
    run_dir, _ = make_valid_run(repo)
    other = _clone(repo, tmp_path)
    with pytest.raises(plan.PlanError):
        run_criterion(run_dir, "lint", repo=other)


# ── 6. an ignored file that steers the tests still counts ────────────────────

def test_an_ignored_conftest_changes_the_fingerprint(repo):
    (repo / ".gitignore").write_text("docs/plans/\n__pycache__/\nconftest.py\n.venv/\n", encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "ignore conftest")
    before = tree_fingerprint(repo)
    (repo / "conftest.py").write_text("def pytest_collection_modifyitems(items):\n    items.clear()\n",
                                      encoding="utf-8")
    assert _git(repo, "status", "--porcelain").strip() == ""
    assert tree_fingerprint(repo) != before
    # A dependency directory's copy is not the project's.
    (repo / "conftest.py").unlink()
    (repo / ".venv").mkdir()
    (repo / ".venv" / "conftest.py").write_text("x = 1\n", encoding="utf-8")
    assert tree_fingerprint(repo) == before


# ── 7. the evidence names the pipeline that produced it ──────────────────────

def test_evidence_from_another_pipeline_implementation_is_flagged(repo):
    run_dir, sess = make_valid_run(repo)
    tester = sess.agent("brief")
    for cid in ("unit", "lint"):
        rec, path = run_criterion(run_dir, cid)
        line = (f"PIPELINE-EVIDENCE id={cid} sha={evidence_sha(path)} passed=true "
                f"fingerprint={rec['fingerprint'][:12]} impl=000000000000")
        tester.bash(f"{PY} -m pipeline.run_check {run_dir} {cid}", stdout=line + "\n")
    v = harness.grade_run(run_dir)
    assert "pipeline-impl-mismatch" in codes(v), harness.format_verdict(v)


# ── 8. a file's mode is part of its content ──────────────────────────────────

def test_making_a_file_executable_changes_the_fingerprint(repo):
    """Same bytes, new mode.  On Windows git keeps the mode only in the index
    (the file system has no executable bit); on Linux and macOS the file's
    own mode is what git sees, and the index is updated by ``git add``.  The
    test changes both, so it holds on every platform the suite runs on (CI is
    Ubuntu; found by the Test Team of the evidence-gaps run)."""
    import os
    import stat
    before = tree_fingerprint(repo)
    p = repo / "thing.py"
    os.chmod(p, os.stat(p).st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    _git(repo, "update-index", "--chmod=+x", "thing.py")
    assert tree_fingerprint(repo) != before


# ── 9. the plan parser refuses what it does not understand ───────────────────

def test_an_unknown_key_in_the_plan_is_an_error():
    with pytest.raises(plan.PlanError):
        plan.parse_criteria("- id: a\n  cmd: x\n  expcet: passed\n")


def test_a_key_given_twice_in_one_item_is_an_error():
    with pytest.raises(plan.PlanError):
        plan.parse_criteria("- id: a\n  cmd: x\n  expect: passed\n  expect: .*\n")


# ── 10. two runs started together do not collide ─────────────────────────────

def test_two_runs_with_the_same_slug_in_the_same_minute_both_start(repo):
    now = datetime(2026, 10, 28, 12, 0, tzinfo=timezone.utc)
    a, _ = start.create_run(repo, "same", "a", "one", now=now)
    b, _ = start.create_run(repo, "same", "a", "two", now=now)
    assert a != b and a.is_dir() and b.is_dir()
