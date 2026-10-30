"""Code Pipeline: the external review of 2026-10-27 named nine false-HIGH
paths and a handful of gaps.  Each is closed here with a test that fails on
the engine as it was and passes on the engine as it is.

The findings, by number in the review:

1. a report changed after the checker signed it still graded HIGH;
2. editing the route in ``run.json`` dropped a required check;
3. a commit of unchanged code read as a stale verification;
4. a protected file changed after Review, nothing demanded Review again;
5. a repository with no commit, or no git, graded light;
7. the ledger trusted an editable ``verdict.json``;
8. the request could be redefined after the start;
9. deleting ``.pipeline-protected`` turned a protected change light;
11. a BLOCKING finding vanished with a rewrite of the report;
plus: one-way checker escalation, pure renames not counted, a short list of
risk signals in the diff, and the delivery gate.

The fixtures (a real repository, transcripts in the CLI's shape, a checker
that attests its report) live in ``test_code_pipeline``.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from pipeline import attest as attest_mod
from pipeline import deliver as deliver_mod
from pipeline import depth as depth_mod
from pipeline import escalate as escalate_mod
from pipeline import harness, ledger, plan, start
from pipeline.evidence import tree_fingerprint
from tests.test_code_pipeline import (ADD_ONLY, AUDIT_TEXT, COVERAGE_OK, PLAN_BLOCK, PY, FakeAgent, _attest, _audit,
                                      _git, _replan, _run_all, _write_stage, codes, make_valid_run, record_baseline,
                                      repo)  # noqa: F401  (repo is the fixture)

# Every test builds its own repository and session records, so a parallel run
# may split this file across workers (tests/_xdist_schedule.py).
XDIST_SPLITTABLE = True


def _as(sess, agent):
    """A handle that writes and runs as an existing subagent."""
    again = FakeAgent.__new__(FakeAgent)
    again.session, again.agent_id, again.path = sess, agent.agent_id, agent.path
    return again


# ── 1. the report is bound to its content ────────────────────────────────────

def test_an_audit_changed_after_the_auditor_signed_it_is_not_the_audit(repo):
    """Provenance proved a subagent issued a Write, not that the file still
    holds what it wrote.  The attest line's sha is the file's sha now."""
    run_dir, _ = make_valid_run(repo)
    p = run_dir / "08-audit.md"
    p.write_text(p.read_text(encoding="utf-8").replace("Overflow on very large integers is not exercised",
                                                       "Nothing could be wrong at all"), encoding="utf-8")
    v = harness.grade_run(run_dir)
    assert "stage-tampered" in codes(v) and v.grade == "MEDIUM", harness.format_verdict(v)


def test_a_report_that_was_never_attested_is_not_a_checker_report(repo):
    """A fresh auditor writes a new report and never attests it: the only
    attestation on record is the earlier auditor's, for other content."""
    run_dir, sess = make_valid_run(repo)
    _write_stage(sess, run_dir, "08-audit.md", AUDIT_TEXT + "\nNo further notes.\n", attest=False)
    v = harness.grade_run(run_dir)
    assert "stage-tampered" in codes(v) and v.grade == "MEDIUM"
    # And a stage with no attestation at all, ever.
    for p in sess.subdir.glob("agent-*.jsonl"):
        text = p.read_text(encoding="utf-8")
        if "PIPELINE-ATTEST stage=08-audit.md" in text:
            p.write_text(text.replace("PIPELINE-ATTEST stage=08-audit.md", "PIPELINE-ATTEST stage=08-draft.md"),
                         encoding="utf-8")
    assert "stage-unattested" in codes(harness.grade_run(run_dir))


def test_a_report_attested_by_the_root_session_is_self_graded(repo):
    run_dir, sess = make_valid_run(repo)
    tester = sess.agent("brief")
    (run_dir / "04-test.md").write_text("# 04 Test\n\nall green\n", encoding="utf-8")
    tester.write(run_dir / "04-test.md", "test")
    rec, _ = attest_mod.attest_stage(run_dir, "04-test.md")
    sess.bash_root(f"{PY} -m pipeline.attest {run_dir} 04-test.md", stdout=attest_mod.attest_line(rec) + "\n")
    v = harness.grade_run(run_dir)
    assert any(f.code == "self-graded" and "attested" in f.message for f in v.findings)


def test_a_report_attested_by_a_subagent_that_did_not_write_it_is_flagged(repo):
    run_dir, sess = make_valid_run(repo)
    tester = sess.agent("brief")
    (run_dir / "04-test.md").write_text("# 04 Test\n\nall green\n", encoding="utf-8")
    tester.write(run_dir / "04-test.md", "test")
    _attest(sess.agent("someone else"), run_dir, "04-test.md")
    v = harness.grade_run(run_dir)
    assert "stage-attested-by-other" in codes(v)


def test_an_attest_line_from_a_chained_command_is_not_an_attestation(repo):
    from pipeline.transcripts import is_pipeline_call
    assert is_pipeline_call(f"{PY} -m pipeline.attest C:/run 08-audit.md", "attest")
    assert not is_pipeline_call(f"{PY} -m pipeline.attest C:/run 08-audit.md; python forge.py", "attest")
    assert not is_pipeline_call(f"{PY} -m pipeline.attest C:/run 08-audit.md && echo done", "attest")
    run_dir, sess = make_valid_run(repo)
    p = run_dir / "08-audit.md"
    p.write_text(p.read_text(encoding="utf-8") + "\nAll fine.\n", encoding="utf-8")
    rec, _ = attest_mod.attest_stage(run_dir, "08-audit.md")
    auditor = harness.writers_of(harness.subagents_of(sess.root), p)[0][0]
    _as(sess, auditor).bash(f"{PY} -m pipeline.attest {run_dir} 08-audit.md; python forge.py",
                            stdout=attest_mod.attest_line(rec) + "\n")
    assert "stage-tampered" in codes(harness.grade_run(run_dir))


def test_attest_cli(repo, capsys):
    run_dir, _ = make_valid_run(repo)
    assert attest_mod.main([str(run_dir), "08-audit.md"]) == 0
    out = capsys.readouterr().out
    assert out.startswith("PIPELINE-ATTEST stage=08-audit.md sha=")
    assert attest_mod.main([str(run_dir), "99-missing.md"]) == 2
    assert attest_mod.main([str(run_dir), "../etc/passwd"]) == 2
    assert len(attest_mod.load_attestations(run_dir, "08-audit.md")) == 2


# ── 2. the route is the one the engine recorded ──────────────────────────────

def test_changing_the_route_in_run_json_cannot_drop_a_required_check(repo):
    """Route (d) needs no regression demonstration.  Removing the baseline
    evidence from a feature run and relabelling it cleanup graded HIGH."""
    run_dir, _ = make_valid_run(repo)
    (run_dir / "evidence" / "unit.baseline.json").unlink()
    v = harness.grade_run(run_dir)
    assert "baseline-missing" in codes(v) and v.grade == "MEDIUM"
    run = json.loads((run_dir / "run.json").read_text(encoding="utf-8"))
    run["route"] = "d"
    (run_dir / "run.json").write_text(json.dumps(run), encoding="utf-8")
    (run_dir / "07-refactor.md").write_text("# 07 Refactor\n\n" + PLAN_BLOCK + "\n", encoding="utf-8")
    v = harness.grade_run(run_dir)
    assert v.facts["route"] == "a"
    assert "route-tampered" in codes(v) and v.grade == "LOW", harness.format_verdict(v)
    assert "baseline-missing" in codes(v)


def test_rewriting_the_base_revision_or_snapshot_in_run_json_is_tampering(repo):
    """The review's "initial run metadata": a base moved forward shrinks the
    diff (light depth); a snapshot moved forward makes a test that does not
    detect the change pass its baseline.  Both are pinned by the start line."""
    run_dir, _ = make_valid_run(repo)
    run = json.loads((run_dir / "run.json").read_text(encoding="utf-8"))
    run["base_rev"] = "0" * 40
    (run_dir / "run.json").write_text(json.dumps(run), encoding="utf-8")
    v = harness.grade_run(run_dir)
    assert "base-tampered" in codes(v) and v.grade == "LOW"
    assert v.facts["depth"] == "light"  # graded on the recorded base, not the forged one


# ── 3. the fingerprint is content, not commit identity ───────────────────────

def test_a_commit_of_unchanged_code_does_not_stale_the_verification(repo):
    run_dir, _ = make_valid_run(repo)
    before = tree_fingerprint(repo)
    # The sanctioned order: the delivery gate passes, then the commit.  A
    # commit before it is graded MEDIUM (committed-before-delivery).
    _rec, delivered = deliver_mod.deliver(run_dir)
    assert delivered
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "ship")
    assert tree_fingerprint(repo) == before
    v = harness.grade_run(run_dir)
    assert v.grade == "HIGH", harness.format_verdict(v)
    text = ledger.append_row(run_dir, delivered="committed", obstacles="none").read_text(encoding="utf-8")
    assert "| HIGH |" in text


def test_staging_a_file_does_not_move_the_fingerprint_either(repo):
    run_dir, _ = make_valid_run(repo)
    before = tree_fingerprint(repo)
    _git(repo, "add", "thing.py")
    assert tree_fingerprint(repo) == before
    (repo / "thing.py").write_text("def add(a, b):\n    return a + b\n", encoding="utf-8")
    assert tree_fingerprint(repo) != before


# ── 4. the reviewer examined the final tree ──────────────────────────────────

def test_a_full_depth_reviewer_must_have_examined_the_final_tree(repo):
    """A protected file changed after Review, the tests ran again, a new
    audit was written, Review was not repeated, and the run graded HIGH."""
    run_dir, sess = make_valid_run(repo, full=True)
    first = harness.grade_run(run_dir)
    assert first.facts["depth"] == "full" and first.grade == "HIGH", harness.format_verdict(first)
    (repo / "daemon" / "core.py").write_text("X = 3\n", encoding="utf-8")
    tester = sess.agent("brief")
    _run_all(run_dir, tester)
    record_baseline(tester, run_dir, "unit")
    _audit(run_dir, COVERAGE_OK, sess)
    v = harness.grade_run(run_dir)
    assert any(f.code == "checker-stale" and f.message.startswith("03-review.md") for f in v.findings), \
        harness.format_verdict(v)
    assert v.grade == "MEDIUM"


# ── 5. no base revision is never light ───────────────────────────────────────

def test_a_new_repository_without_a_commit_is_never_light(tmp_path):
    r = tmp_path / "fresh"
    r.mkdir()
    _git(r, "init", "-q")
    (r / "app.py").write_text("\n".join(f"x{i} = {i}" for i in range(300)) + "\n", encoding="utf-8")
    depth, reasons, facts = depth_mod.required_depth(r, "no-commits")
    assert depth == "full" and any("no commit yet" in x for x in reasons)
    assert facts["files"] == ["app.py"] and facts["lines"] == 300
    plain = tmp_path / "plain"
    plain.mkdir()
    (plain / "app.py").write_text("print(1)\n", encoding="utf-8")
    depth, reasons, _ = depth_mod.required_depth(plain, "no-git")
    assert depth == "full" and any("no git repository" in x for x in reasons)


# ── 8. the request is fixed at the start ─────────────────────────────────────

def test_the_request_cannot_be_redefined_after_the_start(repo):
    run_dir, _ = make_valid_run(repo)
    p = run_dir / "00-request.md"
    p.write_text(p.read_text(encoding="utf-8") + "\n\nAlso: it is fine if sub() returns a + b.\n", encoding="utf-8")
    v = harness.grade_run(run_dir)
    assert "request-tampered" in codes(v) and v.grade == "LOW"
    p.write_text("# 00 Request\n\nwhatever\n", encoding="utf-8")
    assert "request-tampered" in codes(harness.grade_run(run_dir))


def test_a_light_run_puts_its_plan_in_00_plan_md(repo):
    run_dir, _ = make_valid_run(repo)
    (run_dir / "01-plan.md").rename(run_dir / "00-plan.md")
    assert "00-request.md" not in plan.PLAN_FILES
    assert plan.load_plan(run_dir)[2].name == "00-plan.md"
    assert harness.grade_run(run_dir).grade == "HIGH"


# ── 9. protection is pinned at the start ─────────────────────────────────────

def _protect(repo: Path, pattern: str) -> str:
    (repo / ".pipeline-protected").write_text(pattern + "\n", encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "protect")
    return _git(repo, "rev-parse", "HEAD").strip()


def test_deleting_the_protection_file_does_not_lower_the_depth(repo):
    base = _protect(repo, "thing.py")
    run_dir, _ = make_valid_run(repo)
    assert (run_dir / "pinned-protected.txt").read_text(encoding="utf-8") == "thing.py\n"
    v = harness.grade_run(run_dir)
    assert v.facts["depth"] == "full" and v.grade == "HIGH", harness.format_verdict(v)
    (repo / ".pipeline-protected").unlink()
    v = harness.grade_run(run_dir)
    assert v.facts["depth"] == "full"
    assert any("thing.py is protected" in r for r in v.facts["depth_reasons"])
    assert "protected-declaration-changed" in codes(v) and v.grade == "MEDIUM"
    assert depth_mod.required_depth(repo, base, pinned_patterns=["thing.py"])[0] == "full"
    assert depth_mod._matches(".pipeline-protected", depth_mod.DEFAULT_PROTECTED)


def test_editing_the_pinned_protection_copy_is_tampering(repo):
    _protect(repo, "thing.py")
    run_dir, _ = make_valid_run(repo)
    (run_dir / "pinned-protected.txt").write_text("# nothing\n", encoding="utf-8")
    v = harness.grade_run(run_dir)
    assert "protected-tampered" in codes(v) and v.grade == "LOW"


# ── 11. findings survive a rewrite ───────────────────────────────────────────

def test_blocking_findings_survive_a_rewrite_of_the_report(repo):
    run_dir, sess = make_valid_run(repo, full=True)
    reviewer = _as(sess, harness.writers_of(harness.subagents_of(sess.root), run_dir / "03-review.md")[0][0])
    (run_dir / "03-review.md").write_text("# 03 Review\n\nBLOCKING: neg-input sub() ignores negatives\n",
                                          encoding="utf-8")
    reviewer.write(run_dir / "03-review.md", "review")
    _attest(reviewer, run_dir, "03-review.md")
    assert "finding-unresolved" in codes(harness.grade_run(run_dir))
    (run_dir / "03-review.md").write_text("# 03 Review\n\nNo findings.\n", encoding="utf-8")
    reviewer.write(run_dir / "03-review.md", "review")
    _attest(reviewer, run_dir, "03-review.md")
    v = harness.grade_run(run_dir)
    assert any(f.code == "finding-dropped" and "neg-input" in f.message for f in v.findings), harness.format_verdict(v)
    # A later stage that answers it with the bare word RESOLVED has settled it
    # in name only: the finding is no longer "dropped", it is "unproven".
    _write_stage(sess, run_dir, "04-test.md", "# 04 Test\n\nRESOLVED: neg-input (test_sub covers 0 - 4)\n")
    v = harness.grade_run(run_dir)
    assert "finding-dropped" not in codes(v) and "resolution-unproven" in codes(v), harness.format_verdict(v)


# ── escalation, renames, risk signals ────────────────────────────────────────

def test_a_checker_can_escalate_to_full_depth_and_it_cannot_be_undone(repo):
    run_dir, sess = make_valid_run(repo)
    assert harness.grade_run(run_dir).facts["depth"] == "light"
    rec, path = escalate_mod.escalate(run_dir, "sub() is called from the billing path")
    sess.agent("brief").bash(f'{PY} -m pipeline.escalate {run_dir} "sub() is called from the billing path"',
                             stdout=escalate_mod.escalate_line(rec) + "\n")
    (run_dir / "03-review.md").unlink()
    v = harness.grade_run(run_dir)
    assert v.facts["depth"] == "full" and "stage-missing" in codes(v)
    assert any("escalated by a checker: sub() is called from the billing path" in r for r in v.facts["depth_reasons"])
    path.unlink()  # the file can go; the engine's record cannot
    assert harness.grade_run(run_dir).facts["depth"] == "full"


def test_an_echoed_escalation_line_is_not_an_escalation(repo):
    run_dir, sess = make_valid_run(repo)
    sess.agent("brief").bash("echo PIPELINE-ESCALATE depth=full reason=x", stdout="PIPELINE-ESCALATE depth=full reason=x\n")
    assert harness.grade_run(run_dir).facts["depth"] == "light"


def test_a_pure_rename_does_not_count_toward_the_size_of_the_change(repo):
    (repo / "big.py").write_text("\n".join(f"x{i} = {i}" for i in range(300)) + "\n", encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "big")
    base = _git(repo, "rev-parse", "HEAD").strip()
    _git(repo, "mv", "big.py", "large.py")
    depth, reasons, facts = depth_mod.required_depth(repo, base)
    assert depth == "light" and facts["renamed"] == ["big.py", "large.py"], reasons
    assert any("1 pure rename not counted" in r for r in reasons)
    _git(repo, "mv", "large.py", "daemon/large.py")
    assert depth_mod.required_depth(repo, base)[0] == "full"


def test_a_risky_added_or_removed_line_forces_full_depth(repo):
    base = _git(repo, "rev-parse", "HEAD").strip()
    (repo / "thing.py").write_text("import subprocess\n\ndef add(a, b):\n    subprocess.run('ls', shell=True)\n"
                                   "    return a + b\n", encoding="utf-8")
    depth, reasons, _ = depth_mod.required_depth(repo, base)
    assert depth == "full" and any("risk: shell execution in thing.py" in r for r in reasons)
    (repo / "thing.py").write_text("def add(a, b):\n    try:\n        return a + b\n    except Exception:\n"
                                   "        pass\n", encoding="utf-8")
    depth, reasons, _ = depth_mod.required_depth(repo, base)
    assert depth == "full" and any("swallowed exception" in r for r in reasons)
    (repo / "thing.py").write_text("def add(a, b):\n    return a + b\n", encoding="utf-8")
    (repo / "test_add.py").write_text("import thing\nassert thing.add(1, 1) == 2\n", encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "test")
    base = _git(repo, "rev-parse", "HEAD").strip()
    (repo / "test_add.py").write_text("import thing\nthing.add(1, 1)\n", encoding="utf-8")
    depth, reasons, _ = depth_mod.required_depth(repo, base)
    assert depth == "full" and any("removed assertion" in r for r in reasons)
    (repo / "test_add.py").write_text("import thing\nassert thing.add(1, 1) == 2\n", encoding="utf-8")
    (repo / "NOTES.md").write_text("Never use shell=True here.\n", encoding="utf-8")
    assert depth_mod.required_depth(repo, base)[0] == "light"


# ── the delivery gate ────────────────────────────────────────────────────────

def test_the_delivery_gate_recomputes_and_refuses_below_high(repo, capsys):
    run_dir, sess = make_valid_run(repo)
    assert deliver_mod.main([str(run_dir)]) == 0
    out = capsys.readouterr().out
    assert out.startswith("PIPELINE-DELIVERED grade=HIGH") and "accepted=true" in out
    assert json.loads((run_dir / "delivered.json").read_text(encoding="utf-8"))["delivered"] is True
    _replan(run_dir, sess, ADD_ONLY)
    assert deliver_mod.main([str(run_dir)]) == 1
    captured = capsys.readouterr()
    assert "grade=MEDIUM" in captured.out and "accepted=false" in captured.out and "Not delivered" in captured.err
    # There is no flag an agent can type to accept a lower grade: the user
    # answers a card (tests/test_code_pipeline_cards.py).
    with pytest.raises(SystemExit):
        deliver_mod.main([str(run_dir), "--accept", "MEDIUM"])


# ── found by the Review Team of the evidence-chain run (2026-10-27) ───────────

def test_a_bare_ampersand_chains_a_command_after_the_program(repo):
    """``run_check … & perl -e 'sleep 30; forge'`` is one recorded command
    whose second half rewrites the evidence after run_check wrote it (bash
    runs both at once; cmd.exe runs them in turn).  Only ``&&`` was a chain
    before.  Redirects that contain ``&`` are not chains."""
    from pipeline.transcripts import is_pipeline_call
    assert not is_pipeline_call(f"{PY} -m pipeline.run_check C:/run unit & perl -e 'sleep 30'", "run_check")
    assert not is_pipeline_call(f"{PY} -m pipeline.attest C:/run 08-audit.md & cmd /c forge.bat", "attest")
    assert is_pipeline_call(f"{PY} -m pipeline.run_check C:/run unit 2>&1", "run_check")
    assert is_pipeline_call(f"{PY} -m pipeline.run_check C:/run unit &> out.txt", "run_check")
    assert is_pipeline_call(f"cd C:/repo && {PY} -m pipeline.run_check C:/run unit", "run_check")


def test_a_start_line_transplanted_from_a_second_run_is_tampering(repo):
    """Every field the harness checks against the start line (route, request,
    pinned, protected, base, snapshot) can be made to agree by starting a
    SECOND run with the wanted route and copying its nonce and snapshot into
    the first run's ``run.json``: route (d) then needs no regression
    demonstration.  The ``dir=`` field ties the line to one run."""
    run_dir, sess = make_valid_run(repo)
    (run_dir / "evidence" / "unit.baseline.json").unlink()
    assert "baseline-missing" in codes(harness.grade_run(run_dir))
    second_dir, second = start.create_run(repo, "second-look", "d", "Add a sub() helper beside add().")
    sess.bash_root(f"{PY} -m pipeline.start --repo . --slug second-look --route d --request-file req.txt",
                   stdout=start.start_line(second, second_dir) + "\n")
    run = json.loads((run_dir / "run.json").read_text(encoding="utf-8"))
    for key in ("nonce", "route", "base_rev", "base_snapshot"):
        run[key] = second[key]
    (run_dir / "run.json").write_text(json.dumps(run), encoding="utf-8")
    (run_dir / "07-refactor.md").write_text("# 07 Refactor\n\n" + PLAN_BLOCK + "\n", encoding="utf-8")
    v = harness.grade_run(run_dir)
    assert "run-tampered" in codes(v) and v.grade == "LOW", harness.format_verdict(v)
    assert "route-tampered" not in codes(v) and "base-tampered" not in codes(v)  # those all agree: dir is the tell


def test_blocking_ids_survive_deletion_of_the_attest_record_file(repo):
    """The ids a signed report carried must come from the engine's record,
    not from ``evidence/<stage>.attest.json``, which any script can delete."""
    run_dir, sess = make_valid_run(repo, full=True)
    reviewer = _as(sess, harness.writers_of(harness.subagents_of(sess.root), run_dir / "03-review.md")[0][0])
    (run_dir / "03-review.md").write_text("# 03 Review\n\nBLOCKING: neg-input sub() ignores negatives\n",
                                          encoding="utf-8")
    reviewer.write(run_dir / "03-review.md", "review")
    rec = _attest(reviewer, run_dir, "03-review.md")
    assert attest_mod.attest_line(rec).endswith(" blocking=neg-input")
    (run_dir / "03-review.md").write_text("# 03 Review\n\nNo findings.\n", encoding="utf-8")
    reviewer.write(run_dir / "03-review.md", "review")
    _attest(reviewer, run_dir, "03-review.md")
    attest_mod.attest_path(run_dir, "03-review.md").unlink()
    v = harness.grade_run(run_dir)
    assert any(f.code == "finding-dropped" and "neg-input" in f.message for f in v.findings), harness.format_verdict(v)


def test_an_assume_unchanged_file_cannot_hide_a_change_from_the_fingerprint(repo):
    """``git update-index --assume-unchanged`` (or ``--skip-worktree``) makes
    ``git diff`` report nothing for a file however much it changes, so the
    index blob stood in for content that was no longer there: a change after
    verification left the fingerprint, and HIGH, untouched."""
    before = tree_fingerprint(repo)
    _git(repo, "update-index", "--assume-unchanged", "thing.py")
    (repo / "thing.py").write_text("def add(a, b):\n    return a - b\n", encoding="utf-8")
    assert _git(repo, "diff", "--name-only").strip() == ""  # git itself does not see it
    assert tree_fingerprint(repo) != before
    _git(repo, "update-index", "--no-assume-unchanged", "thing.py")
    _git(repo, "update-index", "--skip-worktree", "thing.py")
    assert tree_fingerprint(repo) != before
    (repo / "thing.py").write_text("def add(a, b):\n    return a + b\n", encoding="utf-8")
    assert tree_fingerprint(repo) == before


def test_a_removed_risky_line_in_a_deleted_file_names_that_file(repo):
    (repo / "test_add.py").write_text("import thing\nassert thing.add(1, 1) == 2\n", encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "test")
    base = _git(repo, "rev-parse", "HEAD").strip()
    (repo / "test_add.py").unlink()
    depth, reasons, _ = depth_mod.required_depth(repo, base)
    assert depth == "full" and any("removed assertion in test_add.py" in r for r in reasons), reasons
    assert not any("/dev/null" in r for r in reasons)


def test_a_request_with_leading_blank_lines_is_not_read_as_tampered(repo):
    """``00-request.md`` puts a blank line after its marker heading, so the
    request's own leading blank lines cannot be told apart from it; the hash
    ignores them on both sides."""
    assert plan.request_sha("\n\nx\n") == plan.request_sha("x")
    run_dir, run = start.create_run(repo, "blank", "a", "\n\nLeading blank lines.\n")
    text = plan.request_text_of((run_dir / "00-request.md").read_text(encoding="utf-8"))
    assert plan.request_sha(text) == run["request_sha"]


# ── found by the Test Team of the evidence-chain run (2026-10-27) ─────────────

def test_an_environment_prefix_before_the_program_is_not_a_producer():
    """BLOCKING env-prefix: ``PYTEST_ADDOPTS='-k one' python -m
    pipeline.run_check …`` recorded a genuine passing run of one test as the
    whole suite.  Only a directory change and the interpreter may precede the
    program."""
    from pipeline.transcripts import is_pipeline_call, is_run_check_call
    good = [
        f"{PY} -m pipeline.run_check C:/run unit",
        f"cd C:/repo && {PY} -m pipeline.run_check C:/run unit",
        f'cd "C:/my repo" ; {PY} -m pipeline.run_check C:/run unit',
        f'& "C:/x y/python.exe" -m pipeline.run_check C:/run unit',
        f"Set-Location C:/repo; {PY} -m pipeline.attest C:/run 04-test.md",
        "python3 -m pipeline.run_check C:/run unit",
        "C:/x/python.exe -m pipeline.escalate C:/run \"why\"",
    ]
    bad = [
        f"PYTEST_ADDOPTS='-k test_one' {PY} -m pipeline.run_check C:/run unit",
        f"$env:PYTEST_ADDOPTS='-k test_one'; {PY} -m pipeline.run_check C:/run unit",
        f"env PATH=/tmp/fake:$PATH {PY} -m pipeline.run_check C:/run unit",
        f"export PATH=/tmp/fakegit:$PATH && {PY} -m pipeline.attest C:/run 04-test.md",
        f"set X=1 && {PY} -m pipeline.run_check C:/run unit",
        f"echo hi && {PY} -m pipeline.run_check C:/run unit",
        f"true; {PY} -m pipeline.run_check C:/run unit",
    ]
    for cmd in good:
        assert is_pipeline_call(cmd, cmd.split("pipeline.")[1].split()[0]), cmd
    for cmd in bad:
        assert not is_pipeline_call(cmd, cmd.split("pipeline.")[1].split()[0]), cmd
    assert is_run_check_call(good[0]) and not is_run_check_call(bad[0])


def test_run_check_strips_test_steering_variables_and_records_it(repo, monkeypatch):
    """The second half of env-prefix: even when the variable is set some other
    way, the verified command never sees it, and the evidence says so."""
    from pipeline.evidence import run_criterion
    run_dir, _ = start.create_run(repo, "env", "a", "x")
    (repo / "test_two.py").write_text("def test_a():\n    assert True\n\n\ndef test_b():\n    assert True\n",
                                      encoding="utf-8")
    (run_dir / "01-plan.md").write_text("```verification\n- id: two\n  cmd: {python} -m pytest -p no:cacheprovider "
                                        "--no-header test_two.py\n  expect: 2 passed\n```\n", encoding="utf-8")
    monkeypatch.setenv("PYTEST_ADDOPTS", "-k test_a")
    rec, _ = run_criterion(run_dir, "two")
    assert rec["passed"], rec["stdout_tail"] + rec["stderr_tail"]
    assert "2 passed" in rec["stdout_tail"] and "PYTEST_ADDOPTS" in rec["env_stripped"]


def test_a_file_hidden_by_git_info_exclude_still_counts(repo):
    """BLOCKING exclude-hidden-conftest: a root ``conftest.py`` listed in
    ``.git/info/exclude`` was invisible to ``--exclude-standard`` and so to the
    fingerprint and the depth scan, while pytest loaded it.  Untracked files
    are listed against the repository's own ``.gitignore`` only."""
    from pipeline.evidence import local_excludes, untracked_files
    base = _git(repo, "rev-parse", "HEAD").strip()
    before = tree_fingerprint(repo)
    (repo / ".git" / "info").mkdir(exist_ok=True)
    (repo / ".git" / "info" / "exclude").write_text("conftest.py\n", encoding="utf-8")
    (repo / "conftest.py").write_text("def pytest_collection_modifyitems(items):\n    items.clear()\n",
                                      encoding="utf-8")
    assert _git(repo, "status", "--porcelain").strip() == ""  # git itself hides it
    assert "conftest.py" in untracked_files(repo)
    assert tree_fingerprint(repo) != before
    depth, reasons, facts = depth_mod.required_depth(repo, base)
    assert "conftest.py" in facts["files"]
    assert local_excludes(repo) == ["conftest.py"]
    # The repository's own .gitignore is still honoured.
    (repo / "scratch.log").write_text("x\n", encoding="utf-8")
    (repo / ".gitignore").write_text("docs/plans/\n__pycache__/\n*.log\n", encoding="utf-8")
    assert "scratch.log" not in untracked_files(repo)
    # ...and adding that ignore rule is itself a risk signal (full depth).
    depth, reasons, _ = depth_mod.required_depth(repo, base)
    assert depth == "full" and any("new ignore rule" in r for r in reasons), reasons


def test_the_cd_argument_cannot_carry_an_environment_assignment():
    """Loop 1, Review: the prefix rule read ``cd foo;$env:X='y';python`` as a
    directory named ``foo;$env:X='y'`` followed by ``;``, and PowerShell
    evaluates a ``$(…)`` inside quotes in the caller's scope."""
    from pipeline.transcripts import is_pipeline_call
    py = "C:/x/python.exe"
    for cmd in (
        f"cd foo;$env:PYTEST_ADDOPTS='-k_one';{py} -m pipeline.run_check R unit",
        f"cd \"$($env:PYTEST_ADDOPTS='-k_one';'.')\"; {py} -m pipeline.run_check R unit",
        f"& \"$($env:PYTEST_ADDOPTS='-k_one';'python')\" -m pipeline.run_check R unit",
        f"cd .;X=1;{py} -m pipeline.run_check R unit",
        f"cd `pwd`; {py} -m pipeline.attest R 04-test.md",
    ):
        assert not is_pipeline_call(cmd, cmd.split("pipeline.")[1].split()[0]), cmd
    for cmd in (f"cd C:/repo && {py} -m pipeline.run_check R unit",
                f'cd "C:/my repo"; {py} -m pipeline.run_check R unit',
                f"& '{py}' -m pipeline.attest R 04-test.md"):
        assert is_pipeline_call(cmd, cmd.split("pipeline.")[1].split()[0]), cmd


def test_an_untracked_self_ignoring_gitignore_hides_nothing(repo):
    """Loop 1, Review: ``sub/.gitignore`` holding ``.gitignore`` and
    ``conftest.py`` is honoured like a tracked ignore file and hid both from
    ``--exclude-per-directory=.gitignore`` (``git status`` empty).  A hidden
    ignore file whose rule is not in a tracked file counts; one inside a
    directory a TRACKED rule ignores does not."""
    from pipeline.evidence import untracked_files
    (repo / ".gitignore").write_text("docs/plans/\n__pycache__/\nbuild/\n", encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "ignore build")
    base = _git(repo, "rev-parse", "HEAD").strip()
    before = tree_fingerprint(repo)
    (repo / "sub").mkdir()
    (repo / "sub" / ".gitignore").write_text(".gitignore\nconftest.py\n", encoding="utf-8")
    (repo / "sub" / "conftest.py").write_text("def pytest_collection_modifyitems(items):\n    items.clear()\n",
                                              encoding="utf-8")
    assert _git(repo, "status", "--porcelain").strip() == ""
    assert "sub/.gitignore" in untracked_files(repo)
    assert tree_fingerprint(repo) != before
    depth, reasons, _ = depth_mod.required_depth(repo, base)
    assert depth == "full" and any("new ignore rule in sub/.gitignore" in r for r in reasons), reasons
    (repo / "build").mkdir()
    (repo / "build" / ".gitignore").write_text("*\n", encoding="utf-8")
    assert "build/.gitignore" not in untracked_files(repo)


def test_the_verdict_reports_a_local_exclude_file(repo):
    run_dir, _ = make_valid_run(repo)
    (repo / ".git" / "info").mkdir(exist_ok=True)
    (repo / ".git" / "info" / "exclude").write_text("# comment\nnotes.txt\n", encoding="utf-8")
    v = harness.grade_run(run_dir)
    assert v.grade == "HIGH" and "local-excludes" in codes(v)
    assert v.facts["local_excludes"] == ["notes.txt"]


# ── found by the Review Team's loop-1 re-review (2026-10-27) ──────────────────

def test_the_interpreters_startup_files_are_pinned_and_checked(repo, tmp_path, monkeypatch):
    """BLOCKING interpreter-outside-tree: a ``.pth`` or ``sitecustomize.py``
    written into the gitignored virtualenv steers every ``{python}`` run with
    nothing in the tree to show for it.  The interpreter's startup state is
    pinned at the start, recorded with every piece of evidence, and a write
    into its site directories by a run session is a finding."""
    import sys
    from pipeline import evidence as ev
    site = tmp_path / "site-packages"
    site.mkdir()
    before = ev.site_fingerprint([site])
    assert before == ev.site_fingerprint([site])
    (site / "steer.pth").write_text("import steer\n", encoding="utf-8")
    assert ev.site_fingerprint([site]) != before
    (site / "steer.pth").unlink()
    assert ev.site_fingerprint([site]) == before
    (site / "sitecustomize.py").write_text("print('hi')\n", encoding="utf-8")
    assert ev.site_fingerprint([site]) != before
    (site / "sitecustomize.py").unlink()
    monkeypatch.setenv("PYTHONPATH", str(tmp_path / "elsewhere"))
    assert ev.site_fingerprint([site]) != before

    # A valid run pins the state; evidence produced in a different state, or
    # a state that changed since, costs the grade.
    run_dir, _ = make_valid_run(repo)
    run = json.loads((run_dir / "run.json").read_text(encoding="utf-8"))
    assert run["site_sha"] == ev.site_fingerprint()
    v = harness.grade_run(run_dir)
    assert v.grade == "HIGH" and "interpreter-changed" not in codes(v), harness.format_verdict(v)
    monkeypatch.setattr(ev, "site_fingerprint", lambda dirs=None: "0" * 64)
    monkeypatch.setattr(harness, "site_fingerprint", lambda dirs=None: "0" * 64)
    v = harness.grade_run(run_dir)
    assert any(f.code == "interpreter-changed" and "changed after" in f.message for f in v.findings)
    assert v.grade == "MEDIUM"


def test_an_installed_pytest_plugin_moves_the_interpreter_fingerprint(tmp_path):
    """Loop 2, Review: pytest auto-loads a plugin declared under ``pytest11``
    in any installed distribution's entry points, so ``pip install`` of a
    steering plugin changed nothing the fingerprint read."""
    from pipeline import evidence as ev
    site = tmp_path / "site-packages"
    site.mkdir()
    before = ev.site_fingerprint([site])
    dist = site / "steer-1.0.dist-info"
    dist.mkdir()
    assert ev.site_fingerprint([site]) != before
    added = ev.site_fingerprint([site])
    (dist / "entry_points.txt").write_text("[pytest11]\nsteer = steer\n", encoding="utf-8")
    assert ev.site_fingerprint([site]) != added


def test_evidence_made_by_a_steered_interpreter_is_flagged(repo):
    run_dir, _ = make_valid_run(repo)
    p = run_dir / "evidence" / "lint.json"
    rec = json.loads(p.read_text(encoding="utf-8"))
    rec["site_sha"] = "f" * 64  # as run_check would record under a steered interpreter
    p.write_text(json.dumps(rec, indent=2, sort_keys=True), encoding="utf-8")
    v = harness.grade_run(run_dir)
    # The edited file no longer matches its recorded line (unrecorded), and
    # had it been produced that way, the pin would have caught it:
    from pipeline.harness import Verdict, _check_site
    w = Verdict()
    _check_site(w, "lint", rec, "a" * 64, "a" * 64)
    assert any(f.code == "interpreter-changed" and "pinned at the start" in f.message for f in w.findings)
    assert v.grade == "LOW"


def test_a_write_into_the_interpreters_site_directory_is_a_finding(repo):
    from pipeline.evidence import interpreter_site_dirs
    run_dir, sess = make_valid_run(repo)
    target = interpreter_site_dirs()[0] / "zz_steer.pth"
    sess.agent("brief").write(target, "import steer\n")  # recorded, never performed
    v = harness.grade_run(run_dir)
    assert "interpreter-written" in codes(v) and v.grade == "LOW"


# ── found by the Final Audit of the evidence-chain run (2026-10-27) ──────────

def test_moving_protected_code_out_of_its_protected_path_is_full_depth(repo):
    """BLOCKING rename-out-of-protection: with git's rename detection on, a
    rename was listed by its new path only, so ``git mv daemon/core.py
    core.py`` (with or without an edit) graded light.  Both paths count."""
    base = _git(repo, "rev-parse", "HEAD").strip()
    _git(repo, "mv", "daemon/core.py", "core.py")
    depth, reasons, facts = depth_mod.required_depth(repo, base)
    assert "daemon/core.py" in facts["files"] and "core.py" in facts["files"]
    assert depth == "full" and any("daemon/core.py is protected" in r for r in reasons), reasons
    (repo / "core.py").write_text("X = 99\n", encoding="utf-8")
    depth, reasons, _ = depth_mod.required_depth(repo, base)
    assert depth == "full" and any("daemon/core.py is protected" in r for r in reasons), reasons


def test_an_audit_blocking_finding_is_cleared_only_by_a_fresh_auditor(repo):
    """The audit is the last stage, so a BLOCKING it raises had no later stage
    to clear it and stopped HIGH forever.  A fresh auditor (a different
    subagent) rewriting the audit after the fix may record RESOLVED; the
    auditor that raised it may not."""
    run_dir, sess = make_valid_run(repo)
    first = _write_stage(sess, run_dir, "08-audit.md",
                         AUDIT_TEXT + "\nBLOCKING: rename-gap protected code can move out of protection\n")
    assert "finding-unresolved" in codes(harness.grade_run(run_dir))
    # The same auditor clears its own finding: not accepted.
    (run_dir / "08-audit.md").write_text(AUDIT_TEXT + "\nRESOLVED: rename-gap\n", encoding="utf-8")
    first.write(run_dir / "08-audit.md", "audit")
    _attest(first, run_dir, "08-audit.md")
    assert "finding-dropped" in codes(harness.grade_run(run_dir))
    # A fresh auditor settles it, with a check that covers it and passed on
    # the final tree (the word RESOLVED alone no longer settles anything).
    tester = _replan(run_dir, sess, PLAN_BLOCK.replace("  suite: true\n", "  suite: true\n  covers: rename-gap\n"))
    record_baseline(tester, run_dir, "unit")
    _write_stage(sess, run_dir, "08-audit.md",
                 AUDIT_TEXT.replace("Verdict: covered", "rename-gap: unit exercises it.\n\nVerdict: covered")
                 + "\nNOT-A-DEFECT: rename-gap by=unit\n")
    v = harness.grade_run(run_dir)
    assert "finding-dropped" not in codes(v) and v.grade == "HIGH", harness.format_verdict(v)


def test_the_raising_auditor_cannot_launder_its_resolution_through_a_stooge(repo):
    """Loop 3, Review: the raising auditor writes RESOLVED itself, any other
    subagent re-Writes the same text (becoming the last writer), and the
    raising auditor attests it.  The clearing checker must be the one that
    signed the current version, and no raiser may sign it."""
    run_dir, sess = make_valid_run(repo)
    first = _write_stage(sess, run_dir, "08-audit.md",
                         AUDIT_TEXT + "\nBLOCKING: rename-gap protected code can move out of protection\n")
    text = AUDIT_TEXT + "\nRESOLVED: rename-gap\n"
    (run_dir / "08-audit.md").write_text(text, encoding="utf-8")
    first.write(run_dir / "08-audit.md", text)
    sess.agent("copy this file").write(run_dir / "08-audit.md", text)
    _attest(first, run_dir, "08-audit.md")
    v = harness.grade_run(run_dir)
    assert "finding-dropped" in codes(v) and v.grade != "HIGH", harness.format_verdict(v)
