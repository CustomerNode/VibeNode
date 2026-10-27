"""Code Pipeline: the grade is computed from the engine's records, and the
harness has been seen to reject every forgery it was built against.

A rule that has never failed on wrong data has not been seen to work.  So each
test here starts from one genuinely valid run (a real git repository, a real
``run_check`` execution, transcripts in the exact shape the CLI writes) and
then breaks exactly one thing:

* a typed "427 passed" with no execution behind it;
* a checker stage written by the root session (self-grading);
* an evidence file written by hand, and one edited after the fact;
* a verification run on a tree that was changed afterwards;
* a plan edited after its checks ran;
* a checker briefed with the builder's own report pasted in;
* a run with no engine record at all.

The valid run grades HIGH.  Every forgery grades below it, for the reason the
harness names.  The depth rule, the plan parser, the ledger and the CLIs are
covered too.  Nothing here touches the real ``~/.claude``: ``Path.home()`` is
sandboxed by ``conftest``.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import uuid
from pathlib import Path

import pytest

from common.paths import claude_projects_dir, encode_cwd
from pipeline import depth as depth_mod
from pipeline import harness, ledger, plan, run_check, start
from pipeline.evidence import evidence_line, evidence_sha, run_criterion, tree_fingerprint

PY = sys.executable.replace("\\", "/")


# ── a real repository ────────────────────────────────────────────────────────

def _git(repo: Path, *args: str) -> str:
    r = subprocess.run(["git", *args], cwd=str(repo), capture_output=True, text=True, check=True)
    return r.stdout


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    r = tmp_path / "proj"
    r.mkdir()
    _git(r, "init", "-q")
    _git(r, "config", "user.email", "pipeline@test.local")
    _git(r, "config", "user.name", "pipeline test")
    _git(r, "config", "commit.gpgsign", "false")
    (r / "thing.py").write_text("def add(a, b):\n    return a + b\n", encoding="utf-8")
    (r / "daemon").mkdir()
    (r / "daemon" / "core.py").write_text("X = 1\n", encoding="utf-8")
    (r / ".gitignore").write_text("docs/plans/\n__pycache__/\n", encoding="utf-8")
    _git(r, "add", "-A")
    _git(r, "commit", "-q", "-m", "base")
    return r


# ── transcripts in the CLI's shape ───────────────────────────────────────────

class FakeSession:
    """Writes a root transcript and subagent transcripts the way the CLI does."""

    def __init__(self, project_cwd: Path):
        self.sid = str(uuid.uuid4())
        self.dir = claude_projects_dir() / encode_cwd(str(project_cwd))
        self.dir.mkdir(parents=True, exist_ok=True)
        self.root = self.dir / f"{self.sid}.jsonl"
        self.root.write_text("", encoding="utf-8")
        self.subdir = self.dir / self.sid / "subagents"
        self.subdir.mkdir(parents=True, exist_ok=True)
        self._n = 0

    def _id(self) -> str:
        self._n += 1
        return f"toolu_{self._n:04d}"

    @staticmethod
    def _append(path: Path, entry: dict) -> None:
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(entry) + "\n")

    def tool(self, path: Path, name: str, inp: dict, *, stdout: str = "", stderr: str = "",
             is_error: bool = False, result: str | None = None) -> str:
        tid = self._id()
        self._append(path, {"type": "assistant", "sessionId": self.sid, "uuid": str(uuid.uuid4()),
                            "message": {"role": "assistant", "content": [
                                {"type": "tool_use", "id": tid, "name": name, "input": inp}]}})
        entry = {"type": "user", "sessionId": self.sid, "uuid": str(uuid.uuid4()),
                 "message": {"role": "user", "content": [
                     {"type": "tool_result", "tool_use_id": tid, "is_error": is_error,
                      "content": result if result is not None else (stdout if not is_error else f"Error: Exit code 1\n{stdout}")}]}}
        if name in ("Bash", "PowerShell"):
            entry["toolUseResult"] = {"stdout": stdout, "stderr": stderr, "interrupted": False, "isImage": False}
        self._append(path, entry)
        return tid

    def bash_root(self, command: str, stdout: str = "", **kw) -> str:
        return self.tool(self.root, "Bash", {"command": command}, stdout=stdout, **kw)

    def write_root(self, file_path: Path, content: str) -> str:
        return self.tool(self.root, "Write", {"file_path": str(file_path), "content": content}, result="ok")

    def agent(self, prompt: str, *, linked: bool = True, agent_type: str = "general-purpose") -> "FakeAgent":
        tid = self.tool(self.root, "Agent", {"prompt": prompt, "description": "checker", "subagent_type": agent_type},
                        result="done") if linked else "toolu_unlinked"
        return FakeAgent(self, tid, prompt, agent_type)


class FakeAgent:
    def __init__(self, session: FakeSession, tool_use_id: str, prompt: str, agent_type: str):
        self.session = session
        self.agent_id = "a" + uuid.uuid4().hex[:15]
        self.path = session.subdir / f"agent-{self.agent_id}.jsonl"
        (session.subdir / f"agent-{self.agent_id}.meta.json").write_text(json.dumps(
            {"agentType": agent_type, "description": "checker", "toolUseId": tool_use_id, "spawnDepth": 1}),
            encoding="utf-8")
        FakeSession._append(self.path, {"type": "user", "agentId": self.agent_id, "sessionId": session.sid,
                                        "uuid": str(uuid.uuid4()),
                                        "message": {"role": "user", "content": prompt}})

    def bash(self, command: str, stdout: str = "", **kw) -> str:
        return self.session.tool(self.path, "Bash", {"command": command}, stdout=stdout, **kw)

    def write(self, file_path: Path, content: str) -> str:
        return self.session.tool(self.path, "Write", {"file_path": str(file_path), "content": content}, result="ok")


# ── one valid run ────────────────────────────────────────────────────────────

PLAN_BLOCK = f"""```verification
- id: unit
  cmd: {PY} -c "import thing; assert thing.add(2, 2) == 4; print('3 passed')"
  expect: \\d+ passed
- id: lint
  cmd: {PY} -c "print('clean')"
  expect: clean
  required: false
```"""

AUDIT_TEXT = """# 08 Audit

Intent matched. Blast radius: thing.py only.

## What could still be wrong that no check would catch?

Overflow on very large integers is not exercised; Python ints make it moot, but a port to a typed
language would need a bound. Nothing else outstanding.
"""


def make_valid_run(repo: Path, *, route: str = "a", change: bool = True):
    """A run with a real change, a real run_check execution, and transcripts
    showing every checker as a linked subagent.  Returns (run_dir, session)."""
    if change:
        (repo / "thing.py").write_text("def add(a, b):\n    return a + b\n\n\ndef sub(a, b):\n    return a - b\n",
                                       encoding="utf-8")
    run_dir, run = start.create_run(repo, "add-sub", route, "Add a sub() helper beside add().")
    sess = FakeSession(repo)
    sess.bash_root(f"{PY} -m pipeline.start --repo . --slug add-sub --route {route} --request-file req.txt",
                   stdout=f"PIPELINE-RUN nonce={run['nonce']} dir={run_dir}\n")
    (run_dir / "01-plan.md").write_text("# 01 Plan\n\nAdd sub().\n\n" + PLAN_BLOCK + "\n", encoding="utf-8")
    sess.write_root(run_dir / "01-plan.md", "plan")
    build_text = ("# 02 Build\n\n" + "Implemented sub(a, b) returning a - b in thing.py, with a one-line docstring, "
                  "and verified by calling it from a scratch interpreter before handing off to review.\n")
    (run_dir / "02-build.md").write_text(build_text, encoding="utf-8")
    sess.write_root(run_dir / "02-build.md", build_text)
    brief = f"Original request: Add a sub() helper beside add().\nRun directory: {run_dir}\nChanged files: thing.py\n"

    reviewer = sess.agent(brief)
    (run_dir / "03-review.md").write_text("# 03 Review\n\nNo findings.\n", encoding="utf-8")
    reviewer.write(run_dir / "03-review.md", "review")

    tester = sess.agent(brief)
    for cid in ("unit", "lint"):
        rec, path = run_criterion(run_dir, cid)
        assert rec["passed"], rec
        tester.bash(f"{PY} -m pipeline.run_check {run_dir} {cid}",
                    stdout=rec["stdout_tail"] + evidence_line(cid, evidence_sha(path), True, rec["fingerprint"]) + "\n")
    (run_dir / "04-test.md").write_text("# 04 Test\n\nunit: passed (see evidence/unit.json).\n", encoding="utf-8")
    tester.write(run_dir / "04-test.md", "test")

    auditor = sess.agent(brief)
    (run_dir / "08-audit.md").write_text(AUDIT_TEXT, encoding="utf-8")
    auditor.write(run_dir / "08-audit.md", "audit")
    return run_dir, sess


def codes(v: harness.Verdict) -> set[str]:
    return {f.code for f in v.findings}


# ── the valid run ────────────────────────────────────────────────────────────

def test_a_genuinely_valid_run_grades_high(repo):
    run_dir, _ = make_valid_run(repo)
    v = harness.grade_run(run_dir)
    assert v.grade == "HIGH", harness.format_verdict(v)
    assert {c["id"]: c["status"] for c in v.criteria} == {"unit": "passed", "lint": "passed"}
    assert v.facts["depth"] == "light"
    assert (run_dir / "evidence" / "unit.json").is_file()


def test_the_verdict_file_and_cli_report(repo, capsys):
    run_dir, _ = make_valid_run(repo)
    assert harness.main([str(run_dir)]) == 0
    out = capsys.readouterr().out
    assert out.startswith("PIPELINE-VERDICT HIGH")
    assert "criteria: 2/2 passed on the final tree" in out
    verdict = json.loads((run_dir / "verdict.json").read_text(encoding="utf-8"))
    assert verdict["grade"] == "HIGH"
    assert harness.main([str(run_dir), "--expect", "LOW"]) == 1


# ── forgeries: each one breaks exactly one thing ─────────────────────────────

def test_a_typed_passing_line_with_no_execution_is_low(repo):
    run_dir, sess = make_valid_run(repo)
    for cid in ("unit", "lint"):
        (run_dir / "evidence" / f"{cid}.json").unlink()
    (run_dir / "04-test.md").write_text("# 04 Test\n\n427 passed, 0 failed. Full suite green.\n", encoding="utf-8")
    v = harness.grade_run(run_dir)
    assert v.grade == "LOW"
    assert "never-ran" in codes(v)


def test_a_checker_stage_written_by_the_root_session_is_self_graded(repo):
    run_dir, sess = make_valid_run(repo)
    sess.write_root(run_dir / "04-test.md", "all green, HIGH")
    v = harness.grade_run(run_dir)
    assert v.grade == "MEDIUM"
    assert "self-graded" in codes(v)


def test_review_provenance_is_checked_only_at_full_depth(repo):
    """At light depth there is no Review stage, so a 03-review.md the root
    wrote is just a note.  At full depth it is a checker stage."""
    run_dir, sess = make_valid_run(repo)
    sess.write_root(run_dir / "03-review.md", "looks great")
    assert harness.grade_run(run_dir).grade == "HIGH"
    assert "03-review.md" not in harness.required_stages("a", "light")
    assert "03-review.md" in harness.required_stages("a", "full")


def test_a_stage_written_outside_the_run_has_no_provenance(repo):
    run_dir, sess = make_valid_run(repo)
    other = FakeSession(repo)  # a different session, not this run's
    other.write_root(run_dir / "08-audit.md", AUDIT_TEXT)
    # remove the real auditor's write by rebuilding the audit under a new name
    for p in sess.subdir.glob("agent-*.jsonl"):
        text = p.read_text(encoding="utf-8")
        if "08-audit.md" in text:
            p.write_text(text.replace("08-audit.md", "08-audit-draft.md"), encoding="utf-8")
    v = harness.grade_run(run_dir)
    assert v.grade == "MEDIUM"
    assert "provenance-missing" in codes(v)


def test_an_evidence_file_written_by_hand_is_forged(repo):
    run_dir, sess = make_valid_run(repo)
    forged = run_dir / "evidence" / "unit.json"
    rec = json.loads(forged.read_text(encoding="utf-8"))
    rec["passed"] = True
    tester = sess.agent("brief")
    tester.write(forged, json.dumps(rec))
    forged.write_text(json.dumps(rec), encoding="utf-8")
    v = harness.grade_run(run_dir)
    assert v.grade == "LOW"
    assert {"evidence-forged", "evidence-unrecorded"} & codes(v)


def test_an_evidence_file_edited_after_the_run_no_longer_matches_its_line(repo):
    run_dir, _ = make_valid_run(repo)
    p = run_dir / "evidence" / "unit.json"
    rec = json.loads(p.read_text(encoding="utf-8"))
    rec["exit_code"] = 0
    rec["passed"] = True
    rec["stdout_tail"] = "everything passed, honestly"
    p.write_text(json.dumps(rec, indent=2, sort_keys=True), encoding="utf-8")
    v = harness.grade_run(run_dir)
    assert v.grade == "LOW"
    assert "evidence-unrecorded" in codes(v)


def test_a_shell_redirect_into_the_evidence_folder_is_forged(repo):
    run_dir, sess = make_valid_run(repo)
    tester = sess.agent("brief")
    tester.bash(f"echo '{{}}' > {run_dir}/evidence/unit.json")
    v = harness.grade_run(run_dir)
    assert v.grade == "LOW"
    assert "evidence-forged" in codes(v)


def test_reading_the_evidence_is_not_a_forgery(repo):
    run_dir, sess = make_valid_run(repo)
    auditor = sess.agent("brief")
    auditor.bash(f"cat {run_dir}/evidence/unit.json", stdout="{...}")
    v = harness.grade_run(run_dir)
    assert v.grade == "HIGH", harness.format_verdict(v)


def test_a_check_that_failed_is_low(repo):
    run_dir, sess = make_valid_run(repo)
    (run_dir / "01-plan.md").write_text("# plan\n\n```verification\n- id: unit\n  cmd: " + PY +
                                        " -c \"import sys; print('1 failed'); sys.exit(1)\"\n```\n", encoding="utf-8")
    rec, path = run_criterion(run_dir, "unit")
    assert not rec["passed"]
    tester = sess.agent("brief")
    tester.bash(f"{PY} -m pipeline.run_check {run_dir} unit", is_error=True,
                stdout=evidence_line("unit", evidence_sha(path), False, rec["fingerprint"]))
    v = harness.grade_run(run_dir)
    assert v.grade == "LOW"
    assert "check-failed" in codes(v)


def test_a_tree_changed_after_verification_is_stale(repo):
    run_dir, _ = make_valid_run(repo)
    (repo / "thing.py").write_text("def add(a, b):\n    return a - b\n", encoding="utf-8")  # the fix after the green run
    v = harness.grade_run(run_dir)
    assert v.grade == "MEDIUM"
    assert "stale-tree" in codes(v)
    assert any("run it again on the final code" in f.message for f in v.findings)


def test_a_plan_edited_after_its_checks_ran_invalidates_them(repo):
    run_dir, _ = make_valid_run(repo)
    p = run_dir / "01-plan.md"
    p.write_text(p.read_text(encoding="utf-8").replace("expect: clean", "expect: clean|anything"), encoding="utf-8")
    v = harness.grade_run(run_dir)
    assert v.grade == "MEDIUM"
    assert "plan-changed" in codes(v)


def test_a_checker_briefed_with_the_builders_report_is_not_independent(repo):
    run_dir, sess = make_valid_run(repo)
    build = (run_dir / "02-build.md").read_text(encoding="utf-8")
    auditor = sess.agent("Please audit. Here is what the builder did:\n" + build)
    auditor.write(run_dir / "08-audit.md", AUDIT_TEXT)
    v = harness.grade_run(run_dir)
    assert v.grade == "MEDIUM"
    assert "brief-pasted" in codes(v)


def test_a_checker_the_root_did_not_launch_is_flagged(repo):
    run_dir, sess = make_valid_run(repo)
    stranger = sess.agent("brief", linked=False)
    stranger.write(run_dir / "04-test.md", "fine")
    v = harness.grade_run(run_dir)
    assert "checker-unlinked" in codes(v)
    assert v.grade == "MEDIUM"


def test_an_unexecutable_criterion_caps_the_grade_at_medium(repo):
    """Everything else about the run is valid: the one unexecutable criterion
    is the only finding, and it caps the grade at MEDIUM, not lower."""
    run_dir, sess = make_valid_run(repo)
    p = run_dir / "01-plan.md"
    p.write_text("# plan\n\n```verification\n- id: unit\n  cmd: " + PY + " -c \"print('3 passed')\"\n"
                 "  expect: passed\n- id: native-ui\n  executable: false\n  reason: no desktop here\n```\n",
                 encoding="utf-8")
    rec, path = run_criterion(run_dir, "unit")
    tester = sess.agent("brief")
    tester.bash(f"{PY} -m pipeline.run_check {run_dir} unit",
                stdout=evidence_line("unit", evidence_sha(path), True, rec["fingerprint"]))
    v = harness.grade_run(run_dir)
    assert codes(v) == {"unexecuted"}, harness.format_verdict(v)
    assert v.grade == "MEDIUM"
    assert {c["id"]: c["status"] for c in v.criteria} == {"unit": "passed", "native-ui": "unexecuted"}


def test_an_echoed_evidence_line_is_not_a_producer(repo):
    """A forged file plus an ``echo`` of its sha puts the exact needle in the
    transcript; only a run_check invocation counts as producing evidence."""
    run_dir, sess = make_valid_run(repo)
    p = run_dir / "evidence" / "unit.json"
    rec = json.loads(p.read_text(encoding="utf-8"))
    rec["stdout_tail"] = "forged"
    p.write_text(json.dumps(rec, indent=2, sort_keys=True), encoding="utf-8")
    tester = sess.agent("brief")
    line = evidence_line("unit", evidence_sha(p), True, rec["fingerprint"])
    tester.bash(f"echo '{line}'", stdout=line + "\n")
    v = harness.grade_run(run_dir)
    assert v.grade == "LOW"
    assert "evidence-unrecorded" in codes(v)


def test_an_audit_without_the_fourth_question_is_medium(repo):
    run_dir, _ = make_valid_run(repo)
    (run_dir / "08-audit.md").write_text("# 08 Audit\n\nAll good.\n", encoding="utf-8")
    v = harness.grade_run(run_dir)
    assert v.grade == "MEDIUM"
    assert "fourth-question" in codes(v)


def test_a_run_with_no_engine_record_is_blocked(repo):
    run_dir, run = start.create_run(repo, "ghost", "a", "do a thing")
    v = harness.grade_run(run_dir)
    assert v.grade == "BLOCKED"
    assert "no-transcript" in codes(v)
    v2 = harness.grade_run(repo / "docs" / "plans" / "runs" / "does-not-exist")
    assert v2.grade == "BLOCKED"
    assert "no-run" in codes(v2)


def test_the_two_historical_execute_pipeline_runs_cannot_be_graded():
    """They predate the engine: no run.json, no evidence, no provenance."""
    runs = Path(__file__).resolve().parents[1] / "docs" / "plans" / "runs"
    found = [d for d in (runs / "2026-05-30-retention-policy", runs / "2026-07-01-2300-session-search") if d.is_dir()]
    if not found:
        pytest.skip("historical runs are not on this machine")
    for d in found:
        assert harness.grade_run(d).grade == "BLOCKED"


# ── depth is a rule ──────────────────────────────────────────────────────────

def test_depth_is_light_for_a_small_unprotected_change(repo):
    base = _git(repo, "rev-parse", "HEAD").strip()
    (repo / "thing.py").write_text("def add(a, b):\n    return a + b\n\n\ndef sub(a, b):\n    return a - b\n",
                                   encoding="utf-8")
    depth, reasons, facts = depth_mod.required_depth(repo, base)
    assert depth == "light"
    assert facts["files"] == ["thing.py"]


def test_depth_is_full_when_a_protected_file_changes(repo):
    base = _git(repo, "rev-parse", "HEAD").strip()
    (repo / "daemon" / "core.py").write_text("X = 2\n", encoding="utf-8")
    depth, reasons, _ = depth_mod.required_depth(repo, base)
    assert depth == "full"
    assert any("daemon/core.py is protected" in r for r in reasons)


def test_depth_is_full_for_a_large_change_and_for_a_perf_marker(repo):
    base = _git(repo, "rev-parse", "HEAD").strip()
    (repo / "big.py").write_text("\n".join(f"x{i} = {i}" for i in range(300)) + "\n", encoding="utf-8")
    depth, reasons, _ = depth_mod.required_depth(repo, base)
    assert depth == "full"
    assert any("lines changed" in r for r in reasons)
    (repo / "big.py").unlink()
    (repo / "thing.py").write_text("# PERF-CRITICAL: do not touch\ndef add(a, b):\n    return a + b\n", encoding="utf-8")
    depth, reasons, _ = depth_mod.required_depth(repo, base)
    assert depth == "full"
    assert any("PERF-CRITICAL" in r for r in reasons)


def test_a_repository_can_add_its_own_protected_patterns(repo):
    base = _git(repo, "rev-parse", "HEAD").strip()
    (repo / ".pipeline-protected").write_text("# ours\nthing.py\n", encoding="utf-8")
    (repo / "thing.py").write_text("def add(a, b):\n    return a + b  # touched\n", encoding="utf-8")
    depth, reasons, _ = depth_mod.required_depth(repo, base)
    assert depth == "full"
    assert any("thing.py is protected" in r for r in reasons)


def test_an_unknown_base_revision_fails_closed_to_full_depth(repo):
    """``git diff <rev>`` on a rebased or pruned base fails, and with
    ``check=False`` that read as "no changes yet": a protected change graded
    light.  Found by the Test Team on the engine's first run."""
    (repo / "daemon" / "core.py").write_text("X = 2\n", encoding="utf-8")
    depth, reasons, facts = depth_mod.required_depth(repo, "deadbeef" * 5)
    assert depth == "full"
    assert any("not in this repository" in r for r in reasons)
    assert facts["base_rev"] == "deadbeef" * 5


def test_light_depth_requires_no_plan_or_review_but_keeps_test_and_audit():
    assert harness.required_stages("a", "light") == ["00-request.md", "02-build.md", "04-test.md", "08-audit.md"]
    assert harness.required_stages("a", "full") == ["00-request.md", "01-plan.md", "02-build.md",
                                                    "03-review.md", "04-test.md", "08-audit.md"]
    assert harness.required_stages("c", "light") == ["00-request.md", "06-hotfix.md", "02-build.md",
                                                     "04-test.md", "08-audit.md"]


def test_the_depth_cli_records_the_rule_in_run_json(repo, capsys):
    run_dir, _ = start.create_run(repo, "d", "a", "x")
    (repo / "daemon" / "core.py").write_text("X = 3\n", encoding="utf-8")
    assert depth_mod.main([str(run_dir)]) == 0
    assert capsys.readouterr().out.startswith("PIPELINE-DEPTH full")
    run = json.loads((run_dir / "run.json").read_text(encoding="utf-8"))
    assert run["depth"] == "full" and run["depth_reasons"]


def test_the_harness_grades_by_the_diff_not_by_the_claimed_depth(repo):
    run_dir, sess = make_valid_run(repo)
    run = json.loads((run_dir / "run.json").read_text(encoding="utf-8"))
    run["depth"] = "full"
    (run_dir / "run.json").write_text(json.dumps(run), encoding="utf-8")
    v = harness.grade_run(run_dir)
    assert v.facts["depth"] == "light"
    assert "depth-mismatch" in codes(v)


# ── the plan block ───────────────────────────────────────────────────────────

def test_plan_parser_reads_the_block_and_rejects_bad_ones():
    crit = plan.parse_criteria("- id: a\n  cmd: echo hi\n  expect: hi\n- id: b\n  executable: false\n  reason: no\n")
    assert [c.id for c in crit] == ["a", "b"]
    assert crit[0].required and crit[0].executable and crit[0].expect == "hi"
    assert not crit[1].executable and crit[1].reason == "no"
    with pytest.raises(plan.PlanError):
        plan.parse_criteria("- id: a\n")  # executable with no cmd
    with pytest.raises(plan.PlanError):
        plan.parse_criteria("- id: a\n  executable: false\n")  # no reason
    with pytest.raises(plan.PlanError):
        plan.parse_criteria("- id: a\n  cmd: x\n- id: a\n  cmd: y\n")  # repeated id
    with pytest.raises(plan.PlanError):
        plan.parse_criteria("- id: a\n  cmd: x\n  expect: (\n")  # bad regex
    with pytest.raises(plan.PlanError):
        plan.parse_criteria("")


def test_plan_block_sha_ignores_line_endings_and_trailing_space():
    assert plan.block_sha("- id: a\n  cmd: x\n") == plan.block_sha("- id: a\r\n  cmd: x\r\n\r\n")


# ── run_check ────────────────────────────────────────────────────────────────

def test_run_check_cli_prints_the_evidence_line_last_and_exits_by_result(repo, capsys):
    run_dir, _ = start.create_run(repo, "rc", "a", "x")
    (run_dir / "01-plan.md").write_text("```verification\n- id: ok\n  cmd: " + PY + " -c \"print('fine')\"\n"
                                        "- id: bad\n  cmd: " + PY + " -c \"import sys; sys.exit(3)\"\n```\n",
                                        encoding="utf-8")
    assert run_check.main([str(run_dir), "ok"]) == 0
    out = capsys.readouterr().out.rstrip("\n").splitlines()
    assert out[1] == "fine"
    assert out[-1].startswith("PIPELINE-EVIDENCE id=ok sha=")
    sha = out[-1].split("sha=")[1].split()[0]
    assert sha == evidence_sha(run_dir / "evidence" / "ok.json")
    assert run_check.main([str(run_dir), "bad"]) == 1
    assert run_check.main([str(run_dir), "missing"]) == 2


def test_run_check_prints_the_evidence_line_before_the_echo_as_well(repo, capsys):
    """The Session Engine keeps only a ~2 KB preview of a tool result over
    ~30 KB (measured 2026-10-27: both the Bash and the PowerShell tool persist
    the rest to a side file).  A 20 KB stdout tail plus a 20 KB stderr tail
    can push a line printed only LAST out of the transcript, and the harness
    would grade genuine evidence as unrecorded.  So the same line opens the
    output too, inside the preview."""
    run_dir, _ = start.create_run(repo, "rc", "a", "x")
    (run_dir / "01-plan.md").write_text("```verification\n- id: loud\n  cmd: " + PY +
                                        " -c \"print('y' * 30000)\"\n```\n", encoding="utf-8")
    assert run_check.main([str(run_dir), "loud"]) == 0
    out = capsys.readouterr().out
    first, last = out.split("\n", 1)[0], out.rstrip("\n").rsplit("\n", 1)[-1]
    assert first == last and first.startswith("PIPELINE-EVIDENCE id=loud sha=")
    assert first.split("sha=")[1].split()[0] == evidence_sha(run_dir / "evidence" / "loud.json")
    assert len(out.split("\n", 1)[0]) < 2000  # the line sits inside the preview the engine keeps


def test_run_check_records_the_tree_and_the_plan_it_ran_against(repo):
    run_dir, _ = start.create_run(repo, "rc", "a", "x")
    (run_dir / "01-plan.md").write_text("```verification\n- id: ok\n  cmd: " + PY + " -c \"print(1)\"\n```\n",
                                        encoding="utf-8")
    rec, path = run_criterion(run_dir, "ok")
    assert rec["fingerprint"] == tree_fingerprint(repo)
    assert rec["plan_sha"] == plan.load_plan(run_dir)[1]
    assert rec["tree_stable"] and rec["passed"] and rec["exit_code"] == 0


def test_a_relative_command_with_forward_slashes_runs_under_the_platform_shell(repo):
    """Plans name the interpreter as ``.venv/Scripts/python.exe``; cmd.exe
    splits that first token on ``/`` and fails.  The first real run hit it."""
    run_dir, _ = start.create_run(repo, "rc", "a", "x")
    tool_dir = repo / "tools" / "sub"
    tool_dir.mkdir(parents=True)
    if os.name == "nt":
        (tool_dir / "hello.cmd").write_text("@echo fine\r\n", encoding="utf-8")
        cmd = "tools/sub/hello.cmd"
    else:
        script = tool_dir / "hello.sh"
        script.write_text("#!/bin/sh\necho fine\n", encoding="utf-8")
        script.chmod(0o755)
        cmd = "tools/sub/hello.sh"
    (run_dir / "01-plan.md").write_text(f"```verification\n- id: ok\n  cmd: {cmd} --flag\n  expect: fine\n```\n",
                                        encoding="utf-8")
    rec, _ = run_criterion(run_dir, "ok")
    assert rec["passed"], rec["stderr_tail"]
    assert rec["cmd"] == f"{cmd} --flag"  # the evidence keeps the plan's spelling


def test_a_command_that_changes_the_tree_does_not_pass(repo):
    run_dir, _ = start.create_run(repo, "rc", "a", "x")
    (run_dir / "01-plan.md").write_text("```verification\n- id: dirty\n  cmd: " + PY +
                                        " -c \"open('new.txt','w').write('x')\"\n```\n", encoding="utf-8")
    rec, _ = run_criterion(run_dir, "dirty")
    assert not rec["tree_stable"] and not rec["passed"]


def test_the_fingerprint_ignores_the_run_directory_and_caches(repo):
    before = tree_fingerprint(repo)
    run_dir, _ = start.create_run(repo, "fp", "a", "x")
    (run_dir / "02-build.md").write_text("notes", encoding="utf-8")
    (repo / "__pycache__").mkdir()
    (repo / "__pycache__" / "x.pyc").write_bytes(b"\x00")
    assert tree_fingerprint(repo) == before
    (repo / "thing.py").write_text("changed", encoding="utf-8")
    assert tree_fingerprint(repo) != before


# ── start and ledger ─────────────────────────────────────────────────────────

def test_start_writes_the_request_verbatim_and_prints_the_nonce(repo, tmp_path, capsys):
    req = tmp_path / "req.txt"
    req.write_text("Fix the   thing.\n\nExactly   like this.\n", encoding="utf-8")
    assert start.main(["--repo", str(repo), "--slug", "Fix Thing!", "--route", "b", "--request-file", str(req)]) == 0
    out = capsys.readouterr().out
    assert out.startswith("PIPELINE-RUN nonce=")
    run_dir = Path(out.split("dir=")[1].strip())
    assert run_dir.name.endswith("-fix-thing")
    run = json.loads((run_dir / "run.json").read_text(encoding="utf-8"))
    assert run["route"] == "b" and run["nonce"] in out and run["base_rev"] == _git(repo, "rev-parse", "HEAD").strip()
    assert "Fix the   thing.\n\nExactly   like this." in (run_dir / "00-request.md").read_text(encoding="utf-8")
    with pytest.raises(SystemExit):  # argparse rejects an unknown route
        start.main(["--repo", str(repo), "--slug", "x", "--route", "z", "--request-file", str(req)])
    empty = tmp_path / "empty.txt"
    empty.write_text("", encoding="utf-8")
    assert start.main(["--repo", str(repo), "--slug", "x", "--route", "a", "--request-file", str(empty)]) == 2


def test_ledger_appends_one_row_per_run_and_replaces_a_repeat(repo):
    run_dir, _ = make_valid_run(repo)
    v = harness.grade_run(run_dir)
    harness.write_verdict(run_dir, v)
    p = ledger.append_row(run_dir, delivered="thing.py", obstacles="none")
    text = p.read_text(encoding="utf-8")
    assert text.startswith("# Code Pipeline ledger")
    assert text.count(run_dir.name) == 1
    assert "| HIGH | 2/2 | thing.py | none |" in text
    ledger.append_row(run_dir, delivered="thing.py (committed)", obstacles="none")
    text = p.read_text(encoding="utf-8")
    assert text.count(run_dir.name) == 1 and "(committed)" in text


# ── review findings from the first real run, each closed with a check ────────

def test_a_checker_that_edits_the_plan_has_graded_its_own_criteria(repo):
    """Review finding 2.3: the Test Team of the first real run rewrote the
    plan's command and nothing flagged it.  A checker challenges criteria in
    its report; it does not edit the plan."""
    run_dir, sess = make_valid_run(repo)
    tester = sess.agent("brief")
    tester.write(run_dir / "04-test.md", "ran it")
    tester.write(run_dir / "01-plan.md", "expect: .*")
    v = harness.grade_run(run_dir)
    assert "plan-edited-by-checker" in codes(v)
    assert v.grade == "MEDIUM"


def test_a_planner_subagent_may_write_the_plan(repo):
    run_dir, sess = make_valid_run(repo)
    planner = sess.agent("plan this")
    planner.write(run_dir / "01-plan.md", "the plan")
    v = harness.grade_run(run_dir)
    assert "plan-edited-by-checker" not in codes(v)
    assert v.grade == "HIGH", harness.format_verdict(v)


def test_a_session_that_merely_quotes_the_nonce_line_is_not_the_root(repo):
    """Review finding 2.5: the newest transcript containing the nonce bytes
    used to win.  A later session that cats 00-request.md is not the root."""
    run_dir, sess = make_valid_run(repo)
    run = json.loads((run_dir / "run.json").read_text(encoding="utf-8"))
    quoter = FakeSession(repo)  # newer file: would have been picked before
    quoter.bash_root(f"cat {run_dir}/00-request.md",
                     stdout=f"PIPELINE-RUN nonce={run['nonce']} dir={run_dir}\n")
    os.utime(quoter.root, None)
    v = harness.grade_run(run_dir)
    assert v.facts["root_transcript"] == str(sess.root)
    assert v.grade == "HIGH", harness.format_verdict(v)


def test_the_nonce_must_come_from_a_pipeline_start_call(repo):
    run_dir, run = start.create_run(repo, "ghost", "a", "do a thing")
    s = FakeSession(repo)
    s.bash_root("echo hi", stdout=f"PIPELINE-RUN nonce={run['nonce']} dir={run_dir}\n")
    v = harness.grade_run(run_dir)
    assert v.grade == "BLOCKED" and "no-transcript" in codes(v)


def test_the_fourth_question_must_be_a_heading_with_an_answer(repo):
    """Review finding 2.6: a mention of the phrase in the introduction used to
    count, with everything after it as the 'answer'."""
    run_dir, _ = make_valid_run(repo)
    (run_dir / "08-audit.md").write_text(
        "# 08 Audit\n\nWe asked what could still be wrong that no check would catch and moved on.\n\n"
        "## Findings\n\nNone. The implementation matches the request and the blast radius is one file.\n",
        encoding="utf-8")
    v = harness.grade_run(run_dir)
    assert "fourth-question" in codes(v)
    (run_dir / "08-audit.md").write_text(
        "# 08 Audit\n\n## What could still be wrong that no check would catch?\n\n"
        "Nothing material: the helper is pure and both branches are exercised by the unit criterion.\n",
        encoding="utf-8")
    v = harness.grade_run(run_dir)
    assert "fourth-question" not in codes(v)


def test_a_compound_command_after_run_check_cannot_manufacture_evidence(repo):
    """Audit finding, first real run: ``run_check …; python forge.py`` is one
    recorded command that invokes run_check, rewrites the evidence file and
    prints a matching line.  Nothing may run after run_check in a producer."""
    from pipeline.transcripts import is_run_check_call
    assert is_run_check_call(f"{PY} -m pipeline.run_check C:/run unit")
    assert is_run_check_call(f"cd C:/repo && {PY} -m pipeline.run_check C:/run unit")
    assert not is_run_check_call(f"{PY} -m pipeline.run_check C:/run unit; python forge.py")
    assert not is_run_check_call(f"{PY} -m pipeline.run_check C:/run unit && python forge.py")
    assert not is_run_check_call(f"{PY} -m pipeline.run_check C:/run unit | tee out.txt")
    assert not is_run_check_call(f"{PY} -m pipeline.run_check C:/run unit\npython forge.py")
    run_dir, sess = make_valid_run(repo)
    p = run_dir / "evidence" / "unit.json"
    rec = json.loads(p.read_text(encoding="utf-8"))
    rec["stdout_tail"] = "forged"
    p.write_text(json.dumps(rec, indent=2, sort_keys=True), encoding="utf-8")
    forger = sess.agent("brief")
    forger.bash(f"{PY} -m pipeline.run_check {run_dir} unit; python forge.py",
                stdout=evidence_line("unit", evidence_sha(p), True, rec["fingerprint"]) + "\n")
    v = harness.grade_run(run_dir)
    assert v.grade == "LOW"
    assert "evidence-unrecorded" in codes(v)


def test_one_fact_is_one_finding(repo):
    run_dir, sess = make_valid_run(repo)
    tester = sess.agent("brief")
    tester.write(run_dir / "04-test.md", "ran it")
    tester.write(run_dir / "01-plan.md", "expect: .*")
    tester.write(run_dir / "01-plan.md", "expect: .*")
    v = harness.grade_run(run_dir)
    assert sum(1 for f in v.findings if f.code == "plan-edited-by-checker") == 1


def test_the_package_imports_nothing_from_app_or_daemon():
    root = Path(__file__).resolve().parents[1] / "pipeline"
    for py in root.glob("*.py"):
        text = py.read_text(encoding="utf-8")
        for bad in ("from app", "import app", "from daemon", "import daemon"):
            assert not any(line.strip().startswith(bad) for line in text.splitlines()), (py.name, bad)
