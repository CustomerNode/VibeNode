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
import re
import subprocess
import sys
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

import pytest

from common.paths import claude_projects_dir, encode_cwd
from pipeline import attest as attest_mod
from pipeline import deliver as deliver_mod
from pipeline import depth as depth_mod
from pipeline import escalate as escalate_mod
from pipeline import evidence as evidence_mod
from pipeline import decisions as decisions_mod
from pipeline import harness, ledger, plan, run_check, start
from pipeline.evidence import evidence_line, evidence_sha, run_baseline, run_criterion, tree_fingerprint

PY = sys.executable.replace("\\", "/")

# Every test builds its own repository and session records, so a parallel run
# may split this file across workers (tests/_xdist_schedule.py).  Whole, it was
# one worker grinding for ~12 minutes at the end of the publish run (each test
# makes ~140 git and shell calls, which slow down under 8 workers).
XDIST_SPLITTABLE = True


# ── a real repository ────────────────────────────────────────────────────────

def _git(repo: Path, *args: str) -> str:
    r = subprocess.run(["git", *args], cwd=str(repo), capture_output=True, text=True)
    if r.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)} in {repo} failed ({r.returncode}): {r.stderr.strip()}")
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

# The real projects folder, read at import: conftest moves ``Path.home()`` only
# inside its per-test fixture, so this is the user's.  Every folder there is a
# project in their sidebar; on 2026-10-27 one run of this file left 97 of them
# (``…pytest-of-…-proj``).  conftest's guards were in place, so that run did
# not go through them; this check lives here so it holds even then.
_REAL_PROJECTS = (Path.home() / ".claude" / "projects").resolve()


class FakeSession:
    """Writes a root transcript and subagent transcripts the way the CLI does."""

    def __init__(self, project_cwd: Path):
        self.sid = str(uuid.uuid4())
        projects = claude_projects_dir().resolve()
        if projects == _REAL_PROJECTS:
            raise RuntimeError(
                f"FakeSession would write into the REAL Claude projects folder ({projects}). "
                "Path.home() is not sandboxed: run this file through pytest with tests/conftest.py.")
        self.dir = projects / encode_cwd(str(project_cwd))
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
                            "timestamp": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
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

    def assistant_text(self, text: str, path: Path | None = None) -> None:
        """The agent's own words (its final message with a card, say)."""
        self._append(path or self.root, {"type": "assistant", "sessionId": self.sid, "uuid": str(uuid.uuid4()),
                                         "timestamp": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
                                         "message": {"role": "assistant", "content": [{"type": "text", "text": text}]}})

    def user_text(self, text: str, path: Path | None = None) -> None:
        """A message the user sent (a card button sends its reply this way)."""
        self._append(path or self.root, {"type": "user", "sessionId": self.sid, "uuid": str(uuid.uuid4()),
                                         "timestamp": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
                                         "message": {"role": "user", "content": text}})

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

# The request is sub(); the test calls sub().  An earlier version of this
# fixture checked add() and still graded HIGH: the false HIGH the baseline
# demonstration now rules out (test_a_test_that_misses_the_change_is_not_high).
SUB_TEST = "import thing\nassert thing.sub(5, 3) == 2\nassert thing.sub(0, 4) == -4\nprint('2 passed')\n"

PLAN_BLOCK = """```verification
- id: unit
  cmd: {python} test_sub.py
  expect: \\d+ passed
  suite: true
  detects_change: true
  baseline_files: test_sub.py
  fails_with: has no attribute 'sub'
- id: lint
  cmd: {python} -c "print('clean')"
  expect: clean
  required: false
```"""

AUDIT_TEXT = """# 08 Audit

Intent matched. Blast radius: thing.py only.

## Do the checks prove the request?

The request is a sub() helper beside add(). unit calls sub() with a positive and a negative result,
and its baseline run shows it fails without sub(); lint is optional hygiene and proves nothing about the request.

Verdict: covered

## What could still be wrong that no check would catch?

Overflow on very large integers is not exercised; Python ints make it moot, but a port to a typed
language would need a bound. Nothing else outstanding.
"""


def run_authorized(run_dir: Path, cid: str, *, baseline: bool = False, repo: Path | None = None,
                   checker=None, policy: str = "auto"):
    """Authorize (under *policy*, "auto" unless told otherwise) and execute one
    criterion, the way the two run_check steps do.  With *checker*, the
    authorize step is recorded in its transcript (the execute step is the
    test's to record: that is where tests vary).  Returns (record, path)."""
    dec = run_check.authorize(run_dir, cid, baseline=baseline, repo=repo, policy=policy)
    if checker is not None:
        flag = " --baseline" if baseline else ""
        checker.bash(f"{PY} -m pipeline.run_check authorize {run_dir} {cid}{flag}",
                     stdout=decisions_mod.decision_line(dec) + "\n")
    rec, path, _dec = run_check.execute(run_dir, cid, dec["nonce"], baseline=baseline, repo=repo)
    return rec, path


def record_check(checker, run_dir: Path, cid: str, *, baseline: bool = False, repo: Path | None = None) -> dict:
    """Authorize and execute *cid* as *checker*, recording both steps."""
    rec, path = run_authorized(run_dir, cid, baseline=baseline, repo=repo, checker=checker)
    flag = " --baseline" if baseline else ""
    label = f"{cid}@baseline" if baseline else cid
    fp = rec["snapshot"] if baseline else rec["fingerprint"]
    line = evidence_line(label, evidence_sha(path), rec["passed"], str(fp), rec.get("impl"))
    checker.bash(f"{PY} -m pipeline.run_check execute {run_dir} {cid}{flag} --decision {rec['decision']}",
                 stdout=(rec.get("stdout_tail") or "") + line + "\n")
    return rec


def _attest(checker, run_dir: Path, stage: str) -> dict:
    """The checker signs its report the way a real one does: it runs
    ``pipeline.attest`` and the engine records the printed line."""
    rec, _path = attest_mod.attest_stage(run_dir, stage)
    checker.bash(f"{PY} -m pipeline.attest {run_dir} {stage}", stdout=attest_mod.attest_line(rec) + "\n")
    return rec


def _write_stage(sess, run_dir: Path, stage: str, text: str, *, attest: bool = True, brief: str = "brief"):
    """A fresh checker writes *stage* and (by default) attests it."""
    checker = sess.agent(brief)
    (run_dir / stage).write_text(text, encoding="utf-8")
    checker.write(run_dir / stage, text)
    if attest:
        _attest(checker, run_dir, stage)
    return checker


def make_valid_run(repo: Path, *, route: str = "a", change: bool = True, full: bool = False):
    """A run with a real change, a real run_check execution, and transcripts
    showing every checker as a linked subagent that attested its report.
    Returns (run_dir, session).  The run starts first and the build changes
    the code after it, as in a real run, so the baseline snapshot is the code
    without sub().  ``full=True`` also touches a protected file, so the diff
    requires full depth and the Review stage is a required checker."""
    run_dir, run = start.create_run(repo, "add-sub", route, "Add a sub() helper beside add().")
    if change:
        (repo / "thing.py").write_text("def add(a, b):\n    return a + b\n\n\ndef sub(a, b):\n    return a - b\n",
                                       encoding="utf-8")
        (repo / "test_sub.py").write_text(SUB_TEST, encoding="utf-8")
    if full:
        (repo / "daemon" / "core.py").write_text("X = 2\n", encoding="utf-8")
    sess = FakeSession(repo)
    sess.bash_root(f"{PY} -m pipeline.start --repo . --slug add-sub --route {route} --request-file req.txt",
                   stdout=start.start_line(run, run_dir) + "\n")
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
    _attest(reviewer, run_dir, "03-review.md")

    tester = sess.agent(brief)
    for cid in ("unit", "lint"):
        rec = record_check(tester, run_dir, cid)
        assert rec["passed"], rec
    if change:
        record_baseline(tester, run_dir, "unit")
    (run_dir / "04-test.md").write_text("# 04 Test\n\nunit: passed (see evidence/unit.json).\n", encoding="utf-8")
    tester.write(run_dir / "04-test.md", "test")
    _attest(tester, run_dir, "04-test.md")

    auditor = sess.agent(brief)
    (run_dir / "08-audit.md").write_text(AUDIT_TEXT, encoding="utf-8")
    auditor.write(run_dir / "08-audit.md", "audit")
    _attest(auditor, run_dir, "08-audit.md")
    return run_dir, sess


def record_baseline(checker, run_dir: Path, cid: str) -> dict:
    """Run the regression demonstration for *cid* and record it as *checker*'s
    authorized run_check --baseline execution."""
    return record_check(checker, run_dir, cid, baseline=True)


def rerun_fresh(fn, run_dir: Path, cid: str, *, checker=None):
    """Run a criterion again under a NEW decision (``run_criterion`` or
    ``run_baseline`` says which) and make sure the evidence it writes is new
    content.  Every execution carries its own decision id, so the evidence
    always differs from the one already recorded; the loop stays as a guard."""
    baseline = fn is run_baseline or fn is evidence_mod.run_baseline
    edir = run_dir / evidence_mod.EVIDENCE_DIR
    before = {p.name: evidence_sha(p) for p in edir.glob("*.json")} if edir.is_dir() else {}
    for _ in range(30):
        rec, path = run_authorized(run_dir, cid, baseline=baseline, checker=checker)
        if before.get(path.name) != evidence_sha(path):
            return rec, path
        time.sleep(0.1)
    raise AssertionError(f"rerunning {cid} kept writing the same evidence")


def codes(v: harness.Verdict) -> set[str]:
    return {f.code for f in v.findings}


@pytest.fixture
def auto_policy(tmp_path: Path, monkeypatch):
    """The CLI reads the machine's permission policy; a test pins its own."""
    pol = tmp_path / "policy.json"
    pol.write_text(json.dumps({"policy": "auto"}), encoding="utf-8")
    monkeypatch.setattr(decisions_mod, "POLICY_PATH", pol)
    return pol


def cli_run(run_dir: Path, cid: str, capsys, *, baseline: bool = False) -> int:
    """The two run_check steps through the CLI, as an agent runs them.
    Returns execute's exit code (or authorize's when it fails); capsys holds
    execute's output afterwards."""
    flag = ["--baseline"] if baseline else []
    code = run_check.main(["authorize", str(run_dir), cid, *flag])
    out = capsys.readouterr().out
    if code != 0:
        return code
    nonce = out.split("PIPELINE-DECISION id=")[1].split()[0]
    return run_check.main(["execute", str(run_dir), cid, *flag, "--decision", nonce])


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
                stdout=evidence_line("unit", evidence_sha(path), False, rec["fingerprint"], rec.get("impl")))
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
    unit = PLAN_BLOCK.split("- id: lint")[0].replace("```verification\n", "")
    p.write_text("# plan\n\n```verification\n" + unit +
                 "- id: native-ui\n  executable: false\n  reason: no desktop here\n```\n", encoding="utf-8")
    tester = sess.agent("brief")
    record_check(tester, run_dir, "unit")
    record_baseline(tester, run_dir, "unit")
    # The audit accounts for both criteria, the one that cannot run included.
    _audit(run_dir, "## Do the checks prove the request?\n\nunit exercises sub(); native-ui would prove the "
                    "desktop screen but cannot run here.\n\nVerdict: covered\n\n", sess)
    v = harness.grade_run(run_dir)
    # Informational notes (the fixture repository pins no suite) do not grade.
    assert {f.code for f in v.findings if f.severity != "info"} == {"unexecuted"}, harness.format_verdict(v)
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
    line = evidence_line("unit", evidence_sha(p), True, rec["fingerprint"], rec.get("impl"))
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


FOURTH_ONLY = ("## What could still be wrong that no check would catch?\n\n"
               "Nothing material: the helper is pure and both branches are exercised by the unit criterion.\n")


def _audit(run_dir: Path, coverage: str, sess=None) -> None:
    """Rewrite the audit.  With *sess*, a fresh auditor writes and attests it
    (what a real re-audit does); without, the file is simply replaced, which
    the harness must notice."""
    text = "# 08 Audit\n\nIntent matched.\n\n" + coverage + FOURTH_ONLY
    if sess is None:
        (run_dir / "08-audit.md").write_text(text, encoding="utf-8")
    else:
        _write_stage(sess, run_dir, "08-audit.md", text)


COVERAGE_OK = "## Do the checks prove the request?\n\nunit exercises sub() directly.\n\nVerdict: covered\n\n"


def test_an_audit_that_does_not_judge_coverage_is_medium(repo):
    """Every check can pass on an unfinished feature: passing tests are not
    proof the request was met unless an independent checker says they cover it."""
    run_dir, _ = make_valid_run(repo)
    _audit(run_dir, "")
    v = harness.grade_run(run_dir)
    assert v.grade == "MEDIUM"
    assert "coverage" in codes(v)


def test_an_audit_that_judges_the_checks_insufficient_is_medium(repo):
    run_dir, _ = make_valid_run(repo)
    _audit(run_dir, "## Do the checks prove the request?\n\n"
                    "unit only checks add(); nothing calls sub(), so the request is untested.\n\n"
                    "Verdict: not covered\n\n")
    v = harness.grade_run(run_dir)
    assert v.grade == "MEDIUM"
    assert any(f.code == "coverage" and "do not prove" in f.message for f in v.findings)


def test_the_coverage_section_must_name_every_required_criterion(repo):
    run_dir, _ = make_valid_run(repo)
    _audit(run_dir, "## Do the checks prove the request?\n\nLooks thorough to me.\n\nVerdict: covered\n\n")
    v = harness.grade_run(run_dir)
    assert v.grade == "MEDIUM"
    assert any(f.code == "coverage" and "unit" in f.message for f in v.findings)
    # The optional criterion need not be named; the required one must.
    _audit(run_dir, "## Do the checks prove the request?\n\nunit exercises sub() directly.\n\nVerdict: covered\n\n")
    v = harness.grade_run(run_dir)
    assert "coverage" not in codes(v)


def test_the_coverage_verdict_must_be_explicit_and_under_the_heading(repo):
    run_dir, _ = make_valid_run(repo)
    # A verdict in prose, or under another heading, does not count.
    _audit(run_dir, "We think the checks prove the request; unit covers it. Verdict: covered\n\n")
    v = harness.grade_run(run_dir)
    assert "coverage" in codes(v)
    _audit(run_dir, "## Do the checks prove the request?\n\nunit exercises sub().\n\n"
                    "## Notes\n\nVerdict: covered\n\n")
    v = harness.grade_run(run_dir)
    assert any(f.code == "coverage" and "no 'Verdict" in f.message for f in v.findings)


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

def test_run_check_cli_prints_the_evidence_line_last_and_exits_by_result(repo, capsys, auto_policy):
    run_dir, _ = start.create_run(repo, "rc", "a", "x")
    (run_dir / "01-plan.md").write_text("```verification\n- id: ok\n  cmd: " + PY + " -c \"print('fine')\"\n"
                                        "- id: bad\n  cmd: " + PY + " -c \"import sys; sys.exit(3)\"\n```\n",
                                        encoding="utf-8")
    assert cli_run(run_dir, "ok", capsys) == 0
    out = capsys.readouterr().out.rstrip("\n").splitlines()
    assert out[1] == "fine"
    assert out[-1].startswith("PIPELINE-EVIDENCE id=ok sha=")
    sha = out[-1].split("sha=")[1].split()[0]
    assert sha == evidence_sha(run_dir / "evidence" / "ok.json")
    assert cli_run(run_dir, "bad", capsys) == 1
    assert cli_run(run_dir, "missing", capsys) == 2


def test_run_check_prints_the_evidence_line_before_the_echo_as_well(repo, capsys, auto_policy):
    """The Session Engine keeps only a ~2 KB preview of a tool result over
    ~30 KB (measured 2026-10-27: both the Bash and the PowerShell tool persist
    the rest to a side file).  A 20 KB stdout tail plus a 20 KB stderr tail
    can push a line printed only LAST out of the transcript, and the harness
    would grade genuine evidence as unrecorded.  So the same line opens the
    output too, inside the preview."""
    run_dir, _ = start.create_run(repo, "rc", "a", "x")
    (run_dir / "01-plan.md").write_text("```verification\n- id: loud\n  cmd: " + PY +
                                        " -c \"print('y' * 30000)\"\n```\n", encoding="utf-8")
    assert cli_run(run_dir, "loud", capsys) == 0
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
    assert re.search(r"-fix-thing-[0-9a-f]{4}$", run_dir.name), run_dir.name
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
    # Code changed after grading: the row is RECOMPUTED, so it no longer reads
    # as a verified HIGH, whatever verdict.json says.
    (repo / "thing.py").write_text("changed\n", encoding="utf-8")
    forged = json.loads((run_dir / "verdict.json").read_text(encoding="utf-8"))
    forged["grade"] = "HIGH"
    (run_dir / "verdict.json").write_text(json.dumps(forged), encoding="utf-8")
    text = ledger.append_row(run_dir, delivered="thing.py", obstacles="none").read_text(encoding="utf-8")
    assert "| MEDIUM |" in text and "| HIGH |" not in text
    assert json.loads((run_dir / "verdict.json").read_text(encoding="utf-8"))["grade"] == "MEDIUM"


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
                stdout=evidence_line("unit", evidence_sha(p), True, rec["fingerprint"], rec.get("impl")) + "\n")
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


# ── the change is detected: the regression demonstration ─────────────────────

def _replan(run_dir: Path, sess, block: str):
    """Replace the plan with *block* and re-run every executable criterion as a
    fresh tester, so only the plan's content differs from the valid run."""
    (run_dir / "01-plan.md").write_text("# 01 Plan\n\n" + block + "\n", encoding="utf-8")
    tester = sess.agent("brief")
    for c in plan.load_plan(run_dir)[0]:
        if c.executable:
            record_check(tester, run_dir, c.id)
    return tester


ADD_ONLY = """```verification
- id: unit
  cmd: {python} -c "import thing; assert thing.add(2, 2) == 4; print('3 passed')"
  expect: \\d+ passed
  suite: true
  detects_change: true
  baseline_files: test_sub.py
  fails_with: has no attribute 'sub'
```"""

NO_DEMO = PLAN_BLOCK.replace("  detects_change: true\n  baseline_files: test_sub.py\n"
                             "  fails_with: has no attribute 'sub'\n", "")


def test_a_test_that_misses_the_change_is_not_high(repo):
    """The false HIGH a reviewer found in this file's own fixture: the request
    is sub(), the test calls add(), every check passes, and the audit says
    covered.  On the code before the change the test passes too, so it does
    not test the change, and the grade cannot be HIGH."""
    run_dir, sess = make_valid_run(repo)
    tester = _replan(run_dir, sess, ADD_ONLY)
    rec = record_baseline(tester, run_dir, "unit")
    assert rec["failed"] is False and rec["passed"] is False
    v = harness.grade_run(run_dir)
    assert v.grade == "MEDIUM", harness.format_verdict(v)
    assert "baseline-passed" in codes(v)


def test_an_unrelated_baseline_failure_does_not_count(repo):
    """A test that fails on the baseline for some other reason (a helper file
    the baseline does not have) is not evidence that it detects the change."""
    run_dir, sess = make_valid_run(repo)
    (repo / "helper.py").write_text("X = 1\n", encoding="utf-8")
    (repo / "test_sub.py").write_text("import helper\n" + SUB_TEST, encoding="utf-8")
    tester = _replan(run_dir, sess, PLAN_BLOCK)
    rec = record_baseline(tester, run_dir, "unit")
    assert rec["failed"] and not rec["fails_with_matched"]
    v = harness.grade_run(run_dir)
    assert "baseline-unrelated" in codes(v) and v.grade == "MEDIUM"


def test_a_feature_run_without_a_regression_demonstration_is_medium(repo):
    run_dir, sess = make_valid_run(repo)
    _replan(run_dir, sess, NO_DEMO)
    v = harness.grade_run(run_dir)
    assert "no-change-detection" in codes(v) and v.grade == "MEDIUM"


def test_a_cleanup_run_needs_no_regression_demonstration(repo):
    """Route d intends no behavior change, so there is nothing to detect."""
    run_dir, sess = make_valid_run(repo, route="d")
    (run_dir / "01-plan.md").unlink()
    (run_dir / "07-refactor.md").write_text("# 07 Refactor\n\n" + NO_DEMO + "\n", encoding="utf-8")
    _run_all(run_dir, sess.agent("brief"))
    v = harness.grade_run(run_dir)
    assert "no-change-detection" not in codes(v), harness.format_verdict(v)
    assert "baseline-missing" not in codes(v)


def test_a_waiver_holds_only_when_the_independent_audit_names_it(repo):
    run_dir, sess = make_valid_run(repo)
    block = NO_DEMO.replace("\n```", "\n- id: no-baseline\n  executable: false\n  waiver: true\n"
                                      "  reason: documentation-only change\n```")
    _replan(run_dir, sess, block)
    _audit(run_dir, COVERAGE_OK, sess)
    v = harness.grade_run(run_dir)
    assert "no-change-detection" not in codes(v)
    assert any(f.code == "coverage" and "no-baseline" in f.message for f in v.findings)
    _audit(run_dir, "## Do the checks prove the request?\n\nunit exercises sub(); no-baseline: agreed, nothing "
                    "observable changes.\n\nVerdict: covered\n\n", sess)
    v = harness.grade_run(run_dir)
    assert {f.code for f in v.findings if f.severity != "info"} == set(), harness.format_verdict(v)
    assert v.grade == "HIGH"


def test_the_baseline_never_touches_the_working_tree(repo):
    run_dir, sess = make_valid_run(repo)
    before = tree_fingerprint(repo)
    worktrees = _git(repo, "worktree", "list")
    rec, _ = run_baseline(run_dir, "unit")
    assert rec["demonstrated"]
    assert tree_fingerprint(repo) == before
    assert _git(repo, "worktree", "list") == worktrees
    assert (repo / "thing.py").read_text(encoding="utf-8").count("def sub") == 1


def test_a_dirty_start_is_the_baseline_and_is_kept(repo):
    """Uncommitted work present when the run starts is part of the baseline,
    captured without touching the working tree or the index."""
    (repo / "thing.py").write_text("def add(a, b):\n    return a + b\n\n\ndef mul(a, b):\n    return a * b\n",
                                   encoding="utf-8")
    status = _git(repo, "status", "--porcelain")
    run_dir, run = start.create_run(repo, "dirty", "a", "Add sub().")
    assert _git(repo, "status", "--porcelain") == status
    assert run["base_snapshot"] != run["base_rev"]
    assert _git(repo, "rev-parse", f"refs/pipeline/{run['run_id']}").strip() == run["base_snapshot"]
    shown = _git(repo, "show", f"{run['base_snapshot']}:thing.py")
    assert "def mul" in shown and "def sub" not in shown


def test_baseline_evidence_edited_by_hand_is_forged(repo):
    run_dir, sess = make_valid_run(repo)
    p = run_dir / "evidence" / "unit.baseline.json"
    rec = json.loads(p.read_text(encoding="utf-8"))
    rec["stdout_tail"] += " "
    p.write_text(json.dumps(rec), encoding="utf-8")
    v = harness.grade_run(run_dir)
    assert "evidence-unrecorded" in codes(v) and v.grade == "LOW"


def test_a_baseline_goes_stale_when_its_tests_change(repo):
    run_dir, sess = make_valid_run(repo)
    (repo / "test_sub.py").write_text(SUB_TEST + "assert thing.sub(1, 1) == 0\n", encoding="utf-8")
    _run_all(run_dir, sess.agent("brief"))
    v = harness.grade_run(run_dir)
    assert "baseline-stale" in codes(v)


def test_run_check_baseline_cli_exits_by_the_demonstration(repo, capsys, auto_policy):
    run_dir, _ = make_valid_run(repo)
    assert cli_run(run_dir, "unit", capsys, baseline=True) == 0
    assert "PIPELINE-EVIDENCE id=unit@baseline" in capsys.readouterr().out
    (run_dir / "01-plan.md").write_text("# plan\n\n" + ADD_ONLY + "\n", encoding="utf-8")
    assert cli_run(run_dir, "unit", capsys, baseline=True) == 1
    assert "does not detect it" in capsys.readouterr().err


def test_a_detects_change_criterion_must_say_what_it_detects():
    with pytest.raises(plan.PlanError, match="fails_with and baseline_files"):
        plan.parse_criteria("- id: t\n  cmd: x\n  detects_change: true\n")


# ── the project's suite is pinned for the run ────────────────────────────────

SUITE_DECL = "- id: suite\n  cmd: {python} -c \"print('9 passed')\"\n  expect: \\d+ passed\n"


def _pinned_repo(repo: Path) -> Path:
    (repo / ".pipeline-verify").write_text(SUITE_DECL, encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "declare suite")
    return repo


def _run_all(run_dir: Path, checker) -> None:
    for c in plan.load_plan(run_dir)[0]:
        if c.executable:
            record_check(checker, run_dir, c.id)


def test_the_pinned_suite_is_required_even_when_the_plan_leaves_it_out(repo):
    run_dir, sess = make_valid_run(_pinned_repo(repo))
    assert (run_dir / "pinned-verify.txt").read_text(encoding="utf-8") == SUITE_DECL
    assert [c.id for c in plan.load_plan(run_dir)[0]][0] == "suite"
    v = harness.grade_run(run_dir)
    assert any(f.code == "never-ran" and "suite" in f.message for f in v.findings) and v.grade == "LOW"
    _run_all(run_dir, sess.agent("brief"))
    record_baseline(sess.agent("brief"), run_dir, "unit")
    # New evidence means a fresh audit of it.
    _audit(run_dir, "## Do the checks prove the request?\n\nsuite is the project's own suite; unit exercises "
                    "sub().\n\nVerdict: covered\n\n", sess)
    v = harness.grade_run(run_dir)
    assert v.grade == "HIGH", harness.format_verdict(v)
    assert "suite-unpinned" not in codes(v)


def test_weakening_the_declaration_during_the_run_is_not_enforced_and_not_high(repo):
    run_dir, sess = make_valid_run(_pinned_repo(repo))
    (repo / ".pipeline-verify").write_text("- id: suite\n  cmd: {python} -c \"print('ok')\"\n", encoding="utf-8")
    rec, _ = run_criterion(run_dir, "suite")
    assert "9 passed" in rec["stdout_tail"]  # the pinned command ran, not the weakened one
    v = harness.grade_run(run_dir)
    assert "verify-declaration-changed" in codes(v) and v.grade != "HIGH"


def test_editing_the_pinned_copy_is_tampering(repo):
    run_dir, _ = make_valid_run(_pinned_repo(repo))
    (run_dir / "pinned-verify.txt").write_text("- id: suite\n  cmd: echo 1 passed\n", encoding="utf-8")
    v = harness.grade_run(run_dir)
    assert "pinned-tampered" in codes(v) and v.grade == "LOW"


def test_a_plan_cannot_redefine_a_pinned_criterion(repo):
    run_dir, _ = make_valid_run(_pinned_repo(repo))
    (run_dir / "01-plan.md").write_text("# plan\n\n```verification\n- id: suite\n  cmd: echo 1 passed\n```\n",
                                        encoding="utf-8")
    with pytest.raises(plan.PlanError, match="redefines pinned criterion suite"):
        plan.load_plan(run_dir)


def test_no_declaration_and_no_suite_in_the_plan_is_medium(repo):
    run_dir, sess = make_valid_run(repo)
    tester = _replan(run_dir, sess, PLAN_BLOCK.replace("  suite: true\n", ""))
    record_baseline(tester, run_dir, "unit")
    v = harness.grade_run(run_dir)
    assert "no-suite" in codes(v) and v.grade == "MEDIUM"


def test_a_new_project_may_supply_its_suite_in_the_plan(repo):
    """No declaration yet: the plan's suite: true criterion counts, with a note."""
    run_dir, _ = make_valid_run(repo)
    v = harness.grade_run(run_dir)
    assert v.grade == "HIGH" and "suite-unpinned" in codes(v)


def test_the_declaration_is_a_protected_file():
    assert depth_mod._matches(".pipeline-verify", depth_mod.DEFAULT_PROTECTED)


def test_a_plan_command_names_no_machine_interpreter(repo):
    run_dir, _ = make_valid_run(repo)
    rec, _ = run_criterion(run_dir, "lint")
    assert rec["cmd"].startswith("{python}") and rec["passed"]


# ── distinct, current checkers ───────────────────────────────────────────────

def test_one_checker_writing_two_stages_is_medium(repo):
    run_dir, sess = make_valid_run(repo)
    reviewer = harness.writers_of(harness.subagents_of(sess.root), run_dir / "03-review.md")[0][0]
    again = FakeAgent.__new__(FakeAgent)
    again.session, again.agent_id, again.path = sess, reviewer.agent_id, reviewer.path
    again.write(run_dir / "08-audit.md", "audit")
    v = harness.grade_run(run_dir)
    assert "checker-reused" in codes(v) and v.grade == "MEDIUM"


def test_an_audit_older_than_the_evidence_it_relies_on_is_stale(repo, monkeypatch):
    """The code changed after the audit and the tests ran again on it, so the
    evidence is current but the audit judged the earlier version."""
    monkeypatch.setattr(harness, "STALE_TOLERANCE_S", 0.0)
    run_dir, sess = make_valid_run(repo)
    (repo / "thing.py").write_text("def add(a, b):\n    return a + b\n\n\ndef sub(a, b):\n    return -(b - a)\n",
                                   encoding="utf-8")
    _run_all(run_dir, sess.agent("brief"))
    record_baseline(sess.agent("brief"), run_dir, "unit")
    v = harness.grade_run(run_dir)
    assert "stale-tree" not in codes(v)
    assert "checker-stale" in codes(v) and v.grade == "MEDIUM"


def test_touching_a_file_without_changing_it_does_not_stale_the_audit(repo):
    """Found by the qualification run: an mtime-based check kept MEDIUM after
    the code was restored byte for byte.  Freshness is judged by content."""
    run_dir, _ = make_valid_run(repo)
    future = datetime.now(timezone.utc).timestamp() + 30
    os.utime(repo / "thing.py", (future, future))
    v = harness.grade_run(run_dir)
    assert v.grade == "HIGH", harness.format_verdict(v)


def test_checkers_run_in_order(repo):
    run_dir, sess = make_valid_run(repo)
    sess.agent("brief").write(run_dir / "04-test.md", "test again")
    v = harness.grade_run(run_dir)
    assert "stage-order" in codes(v)


def test_a_blocking_finding_needs_a_resolution_from_a_later_checker(repo):
    run_dir, sess = make_valid_run(repo)
    (run_dir / "03-review.md").write_text("# 03 Review\n\nBLOCKING: neg-input sub() ignores negatives\n",
                                          encoding="utf-8")
    v = harness.grade_run(run_dir)
    assert any(f.code == "finding-unresolved" and "neg-input" in f.message for f in v.findings)
    assert v.grade == "MEDIUM"
    # The stage that raised it cannot clear it itself.
    (run_dir / "03-review.md").write_text("# 03 Review\n\nBLOCKING: neg-input\nRESOLVED: neg-input\n",
                                          encoding="utf-8")
    assert "finding-unresolved" in codes(harness.grade_run(run_dir))
    # A later checker stage can: a fresh tester records the resolution and a
    # fresh auditor follows, in order.
    (run_dir / "03-review.md").write_text("# 03 Review\n\nBLOCKING: neg-input\n", encoding="utf-8")
    _write_stage(sess, run_dir, "04-test.md", "# 04 Test\n\nRESOLVED: neg-input (test_sub covers 0 - 4)\n")
    _audit(run_dir, COVERAGE_OK, sess)
    v = harness.grade_run(run_dir)
    assert "finding-unresolved" not in codes(v) and v.grade == "HIGH", harness.format_verdict(v)


# ── the gate ─────────────────────────────────────────────────────────────────

def test_expect_high_is_the_completion_gate_and_lower_grades_still_report(repo, capsys):
    run_dir, sess = make_valid_run(repo)
    assert harness.main([str(run_dir), "--expect", "HIGH"]) == 0
    capsys.readouterr()
    _replan(run_dir, sess, ADD_ONLY)
    # The plain report succeeds and says what is wrong; the gate refuses.
    assert harness.main([str(run_dir)]) == 0
    out = capsys.readouterr().out
    assert out.startswith("PIPELINE-VERDICT MEDIUM") and "baseline" in out
    assert harness.main([str(run_dir), "--expect", "HIGH"]) == 1


# ── found by the qualification run ───────────────────────────────────────────

def test_non_ascii_output_is_recorded_and_echoed_without_crashing(repo, capsys, auto_policy):
    """The first real qualification run: pytest printed an ellipsis, the
    evidence recorded U+FFFD, and echoing it to a cp1252 console crashed
    run_check after the evidence was written."""
    run_dir, _ = make_valid_run(repo)
    (run_dir / "01-plan.md").write_text(
        "# plan\n\n```verification\n- id: dots\n"
        "  cmd: {python} -c \"print(chr(8230) + ' 1 passed')\"\n"
        "  expect: passed\n```\n", encoding="utf-8")
    rec, _ = run_criterion(run_dir, "dots")
    assert "…" in rec["stdout_tail"] and "�" not in rec["stdout_tail"], rec["stdout_tail"]
    assert cli_run(run_dir, "dots", capsys) == 0
    assert "PIPELINE-EVIDENCE id=dots" in capsys.readouterr().out


# ── review of the evidence-gaps run (2026-10-28) ─────────────────────────────

def test_evidence_that_names_no_pipeline_implementation_is_flagged(repo):
    """A stand-in (or an older copy) that prints no impl= is not this pipeline."""
    run_dir, sess = make_valid_run(repo)
    tester = sess.agent("brief")
    for cid in ("unit", "lint"):
        rec, path = rerun_fresh(run_criterion, run_dir, cid)
        line = (f"PIPELINE-EVIDENCE id={cid} sha={evidence_sha(path)} passed=true "
                f"fingerprint={rec['fingerprint'][:12]}")
        tester.bash(f"{PY} -m pipeline.run_check {run_dir} {cid}", stdout=line + "\n")
    v = harness.grade_run(run_dir)
    assert "pipeline-impl-unknown" in codes(v), harness.format_verdict(v)


def test_baseline_evidence_from_another_pipeline_implementation_is_flagged(repo):
    run_dir, sess = make_valid_run(repo, change=True)
    rec, path = rerun_fresh(evidence_mod.run_baseline, run_dir, "unit")
    line = (f"PIPELINE-EVIDENCE id=unit@baseline sha={evidence_sha(path)} passed=true "
            f"fingerprint={rec['snapshot'][:12]} impl=000000000000")
    sess.agent("brief").bash(f"{PY} -m pipeline.run_check {run_dir} unit --baseline", stdout=line + "\n")
    v = harness.grade_run(run_dir)
    assert "pipeline-impl-mismatch" in codes(v), harness.format_verdict(v)


def test_a_run_directory_outside_a_runs_tree_is_a_plan_error(tmp_path):
    with pytest.raises(plan.PlanError):
        evidence_mod.run_repo(Path(tmp_path.anchor) / "shallow")


def test_a_baseline_that_exits_zero_is_not_recorded_as_demonstrated(repo, monkeypatch):
    """run_check and the harness agree: only a non-zero exit demonstrates."""
    run_dir, _ = make_valid_run(repo)
    real = evidence_mod._execute

    def exits_zero(*a, **k):
        r = list(real(*a, **k))
        r[0] = 0
        return tuple(r)

    monkeypatch.setattr(evidence_mod, "_execute", exits_zero)
    rec, _ = evidence_mod.run_baseline(run_dir, "unit")
    monkeypatch.setattr(evidence_mod, "_execute", real)
    assert rec["fails_with_matched"] and not rec["passed"], rec


def test_a_key_with_no_value_is_an_error():
    """``expect:`` with nothing after it dropped the assertion silently
    (Review Team, evidence-gaps run)."""
    with pytest.raises(plan.PlanError, match="has no value"):
        plan.parse_criteria("- id: a\n  cmd: x\n  expect:\n")


def test_an_earlier_run_in_the_same_session_does_not_leak_into_this_one(repo):
    """Final Audit, evidence-gaps run: one session held two runs, and the
    earlier run's signed BLOCKING findings capped the later run at MEDIUM
    because the harness read every attest line in the session.  Attestations
    and escalations count only when the command names this run."""
    from pipeline import attest as attest_mod2
    first, sess = make_valid_run(repo)
    reviewer = sess.agent("brief")
    (first / "03-review.md").write_text("# 03 Review\n\nBLOCKING: old-issue in the first run\n", encoding="utf-8")
    reviewer.write(first / "03-review.md", "review")
    rec, _ = attest_mod2.attest_stage(first, "03-review.md")
    reviewer.bash(f"{PY} -m pipeline.attest {first} 03-review.md", stdout=attest_mod2.attest_line(rec) + "\n")
    # A second run in the SAME session: start, build, checkers, all clean.
    second, run = start.create_run(repo, "second", "a", "Add a sub() helper beside add().")
    sess.bash_root(f"{PY} -m pipeline.start --repo . --slug second --route a --request-file r.txt",
                   stdout=start.start_line(run, second) + "\n")
    (second / "01-plan.md").write_text("# 01 Plan\n\n" + PLAN_BLOCK + "\n", encoding="utf-8")
    (second / "02-build.md").write_text("# 02 Build\n\nNothing new.\n", encoding="utf-8")
    tester = sess.agent("brief")
    for cid in ("unit", "lint"):
        r, path = run_criterion(second, cid)
        tester.bash(f"{PY} -m pipeline.run_check {second} {cid}",
                    stdout=evidence_line(cid, evidence_sha(path), r["passed"], r["fingerprint"]) + "\n")
    record_baseline(tester, second, "unit")
    for stage, text in (("04-test.md", "# 04 Test\n\nall passed\n"), ("08-audit.md", AUDIT_TEXT)):
        checker = tester if stage == "04-test.md" else sess.agent("brief")
        (second / stage).write_text(text, encoding="utf-8")
        checker.write(second / stage, text)
        r, _ = attest_mod2.attest_stage(second, stage)
        checker.bash(f"{PY} -m pipeline.attest {second} {stage}", stdout=attest_mod2.attest_line(r) + "\n")
    v = harness.grade_run(second)
    assert not ({"finding-dropped", "finding-unresolved"} & codes(v)), harness.format_verdict(v)


def test_a_run_is_named_by_whole_path_segment_in_any_spelling():
    """Loop 3 review: the filter must not find ``...-gaps`` inside ``...-gaps-2``."""
    run = Path("/r/docs/plans/runs/2026-10-28-0006-gaps")
    for cmd in ("py -m pipeline.attest docs/plans/runs/2026-10-28-0006-gaps 03-review.md",
                r'py -m pipeline.attest "C:\R\DOCS\PLANS\RUNS\2026-10-28-0006-GAPS\" 03-review.md',
                "py -m pipeline.attest './2026-10-28-0006-gaps' 03-review.md",
                "py -m pipeline.attest 2026-10-28-0006-gaps/ 03-review.md"):
        assert harness._names_run(cmd, run), cmd
    other = "py -m pipeline.attest docs/plans/runs/2026-10-28-0006-gaps-2 03-review.md"
    assert not harness._names_run(other, run)
    assert harness._names_other_run(other, run)


def test_an_escalation_whose_target_is_not_spelled_out_still_counts(repo):
    """Loop 3 review: ``escalate . "why"`` (after a cd) must not lower the depth;
    only an escalation that names another run is left out."""
    run_dir, sess = make_valid_run(repo)
    rec, _ = escalate_mod.escalate(run_dir, "sub() is called from the billing path")
    sess.agent("brief").bash(f'{PY} -m pipeline.escalate . "sub() is called from the billing path"',
                             stdout=escalate_mod.escalate_line(rec) + "\n")
    assert harness.grade_run(run_dir).facts["depth"] == "full"


def test_an_escalation_of_another_run_does_not_count(repo):
    run_dir, sess = make_valid_run(repo)
    rec, _ = escalate_mod.escalate(run_dir, "about the other run")
    sess.agent("brief").bash(f'{PY} -m pipeline.escalate docs/plans/runs/2026-01-01-0000-other "about the other run"',
                             stdout=escalate_mod.escalate_line(rec) + "\n")
    assert harness.grade_run(run_dir).facts["depth"] == "light"


def test_a_signed_finding_cannot_be_dropped_by_spelling_the_run_differently(repo):
    """Test Team, evidence-gaps loop 3: a reviewer signed a report carrying a
    BLOCKING with ``attest $RUN 03-review.md`` (the path hidden in a shell
    variable), then rewrote it
    without the finding and signed with the full path; the history read only
    commands naming the run, so the finding vanished.  Findings are read
    leniently: only a command that names ANOTHER run is left out."""
    from pipeline import attest as attest_mod2
    run_dir, sess = make_valid_run(repo, full=True)
    reviewer = sess.agent("brief")
    (run_dir / "03-review.md").write_text("# 03 Review\n\nBLOCKING: hidden-issue\n", encoding="utf-8")
    reviewer.write(run_dir / "03-review.md", "review")
    rec, _ = attest_mod2.attest_stage(run_dir, "03-review.md")
    reviewer.bash(f"{PY} -m pipeline.attest $RUN 03-review.md", stdout=attest_mod2.attest_line(rec) + "\n")
    (run_dir / "03-review.md").write_text("# 03 Review\n\nNo findings.\n", encoding="utf-8")
    reviewer.write(run_dir / "03-review.md", "review")
    rec, _ = attest_mod2.attest_stage(run_dir, "03-review.md")
    reviewer.bash(f"{PY} -m pipeline.attest {run_dir} 03-review.md", stdout=attest_mod2.attest_line(rec) + "\n")
    v = harness.grade_run(run_dir)
    assert any(f.code == "finding-dropped" and "hidden-issue" in f.message for f in v.findings), harness.format_verdict(v)


def test_an_earlier_runs_unspelled_sign_off_does_not_leak_into_this_one(repo):
    """Review, loop 4: the lenient history must not take in another run's
    finding signed with ``$RUN``; that signature lives in the other run's
    attest file, not this one's."""
    first, sess = make_valid_run(repo)
    reviewer = sess.agent("brief")
    (first / "03-review.md").write_text("# 03 Review\n\nBLOCKING: old-issue in the first run\n", encoding="utf-8")
    reviewer.write(first / "03-review.md", "review")
    rec, _ = attest_mod.attest_stage(first, "03-review.md")
    reviewer.bash(f"{PY} -m pipeline.attest $RUN 03-review.md", stdout=attest_mod.attest_line(rec) + "\n")
    second, run = start.create_run(repo, "second", "a", "Add a sub() helper beside add().")
    sess.bash_root(f"{PY} -m pipeline.start --repo . --slug second --route a --request-file r.txt",
                   stdout=start.start_line(run, second) + "\n")
    (second / "03-review.md").write_text("# 03 Review\n\nNo findings.\n", encoding="utf-8")
    checker = sess.agent("brief")
    checker.write(second / "03-review.md", "review")
    r, _ = attest_mod.attest_stage(second, "03-review.md")
    checker.bash(f"{PY} -m pipeline.attest {second} 03-review.md", stdout=attest_mod.attest_line(r) + "\n")
    v = harness.grade_run(second)
    assert not any("old-issue" in f.message for f in v.findings), harness.format_verdict(v)


def _background_run_check(sess, checker, run_dir, cid, tmp_path, *, tamper=False):
    """Run a criterion and record it the way the shell tool does when the
    command outruns its foreground limit: the result names an output file the
    engine writes, and the evidence line is in that file."""
    rec, path = run_authorized(run_dir, cid, checker=checker)
    out = tmp_path / "tasks" / f"bg{cid}.output"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(evidence_line(cid, evidence_sha(path), rec["passed"], rec["fingerprint"], rec.get("impl"))
                   + "\n...\n", encoding="utf-8")
    msg = (f"Command did not complete within its 600s timeout and was moved to the background (ID: bg{cid}). "
           f"Output is being written to: {out}. You will be notified when it completes.")
    sess.tool(checker.path, "Bash",
              {"command": f"{PY} -m pipeline.run_check execute {run_dir} {cid} --decision {rec['decision']}"}, result=msg)
    if tamper:
        checker.write(out, "forged")
    return rec


def test_a_run_check_that_outran_the_shell_limit_still_counts(repo, tmp_path):
    """Test Team, evidence-gaps loop 4: the full suite outran the shell tool's
    10-minute foreground limit twice, finished in the background, and its
    evidence was unrecorded.  The engine names the output file it writes; the
    evidence line in it counts."""
    run_dir, sess = make_valid_run(repo)
    tester = sess.agent("brief")
    for cid in ("unit", "lint"):
        _background_run_check(sess, tester, run_dir, cid, tmp_path)
    (run_dir / "04-test.md").write_text("# 04 Test\n\nran in the background\n", encoding="utf-8")
    tester.write(run_dir / "04-test.md", "test")
    _attest(tester, run_dir, "04-test.md")
    _audit(run_dir, COVERAGE_OK, sess)
    v = harness.grade_run(run_dir)
    assert {c["id"]: c["status"] for c in v.criteria}["unit"] == "passed", harness.format_verdict(v)
    assert "evidence-unrecorded" not in codes(v) and v.grade == "HIGH", harness.format_verdict(v)


def test_a_background_output_file_written_by_a_tool_call_is_forged(repo, tmp_path):
    run_dir, sess = make_valid_run(repo)
    tester = sess.agent("brief")
    _background_run_check(sess, tester, run_dir, "unit", tmp_path, tamper=True)
    v = harness.grade_run(run_dir)
    assert "evidence-forged" in codes(v) and v.grade == "LOW"


def test_a_criterion_that_prints_the_background_sentence_cannot_attach_a_file(repo, tmp_path):
    """Review, loop 5: the background sentence counts only as the tool's own
    result.  A foreground run_check whose checked command prints it, naming a
    file the command wrote with another criterion's evidence line, must not
    make that evidence recorded."""
    run_dir, sess = make_valid_run(repo)
    checker = sess.agent("brief")
    lint, lint_path = rerun_fresh(run_criterion, run_dir, "lint")  # new content, no recorded producer yet
    planted = tmp_path / "tasks" / "bx.output"
    planted.parent.mkdir(parents=True, exist_ok=True)
    planted.write_text(evidence_line("lint", evidence_sha(lint_path), True, lint["fingerprint"], lint.get("impl"))
                       + "\n", encoding="utf-8")
    unit, unit_path = run_criterion(run_dir, "unit")
    result = (evidence_line("unit", evidence_sha(unit_path), True, unit["fingerprint"], unit.get("impl")) + "\n"
              + "Command did not complete within its 600s timeout and was moved to the background (ID: bx). "
              + f"Output is being written to: {planted}. You will be notified when it completes.\n")
    sess.tool(checker.path, "Bash", {"command": f"{PY} -m pipeline.run_check {run_dir} unit"}, result=result)
    v = harness.grade_run(run_dir)
    assert {c["id"]: c["status"] for c in v.criteria}["lint"] == "unrecorded", harness.format_verdict(v)


def test_a_report_that_names_an_evidence_file_is_not_a_forgery(repo, tmp_path):
    """Found on the evidence-gaps run: a reviewer appended its report with
    ``cat >> 03-review.md <<'EOF'``, the report named a background output
    file, and the write check read a forgery.  Text fed to cat or tee is data;
    the same text fed to an interpreter is code and still counts."""
    from pipeline.transcripts import strip_data_heredocs
    run_dir, sess = make_valid_run(repo)
    tester = sess.agent("brief")
    _background_run_check(sess, tester, run_dir, "unit", tmp_path)
    reviewer = sess.agent("brief")
    reviewer.bash(f"cat >> {run_dir}/03-review.md <<'EOF'\nThe output went to {tmp_path}/tasks/bgunit.output\n"
                  f"and evidence lives in {run_dir}/evidence/unit.json\nEOF")
    v = harness.grade_run(run_dir)
    assert "evidence-forged" not in codes(v), harness.format_verdict(v)
    # The same text fed to python is code: a write to the evidence folder.
    code = f"python - <<'EOF'\nopen(r'{run_dir}/evidence/unit.json', 'w').write('x')\nEOF"
    assert "evidence/unit.json" in strip_data_heredocs(code)
    sess.agent("brief").bash(code + " > /dev/null")
    assert "evidence-forged" in codes(harness.grade_run(run_dir))


def test_a_heredoc_that_is_code_or_misread_hides_nothing():
    """Review, loop 6: only a terminated heredoc owned by cat/tee, on a header
    line where nothing but cat/tee follows, is stripped.  A heredoc piped from
    an interpreter, a delimiter bash reads differently, and an unterminated
    heredoc all keep every line visible to the write checks."""
    from pipeline.transcripts import strip_data_heredocs
    ev_path = "docs/plans/runs/R/evidence/unit.json"
    visible = [
        "python - <<'EOF' | cat\nopen('%s', 'w').write('x')\nEOF" % ev_path,
        "cat <<'EOF' | sh\necho x > %s\nEOF" % ev_path,
        'cat <<E"O"F >/dev/null\nx\nEOF\ncp f.json %s\nE' % ev_path,
        "cat <<'EOF' >/dev/null\nx\ncp f.json %s" % ev_path,
        "cat <<'EOF' > %s\n{}\nEOF" % ev_path,
    ]
    for cmd in visible:
        assert "evidence/" in strip_data_heredocs(cmd), cmd
    report = "cat >> 03-review.md <<'EOF'\nnames evidence/unit.json > x\nEOF"
    assert "evidence/" not in strip_data_heredocs(report)
