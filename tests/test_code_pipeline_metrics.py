"""Code Pipeline: where a run's time went, checks that cannot pass refused
before they run, evidence reused only when it proves exactly the same thing,
and the depth rule no longer escalating on comments and test fixtures.

The metrics are checked against synthetic session records whose right
answers are known by construction (timestamps, usage, repeated and failed
executions).  Every guard is shown rejecting its violation next to the case
it permits.
"""

from __future__ import annotations

import json
import uuid
from pathlib import Path

import pytest

from pipeline import depth as depth_mod
from pipeline import harness, metrics, run_check
from pipeline.plan import PlanError
from pipeline.transcripts import read_transcript
from tests.test_code_pipeline import (PLAN_BLOCK, _git, auto_policy, cli_run, make_valid_run,  # noqa: F401
                                      repo)

XDIST_SPLITTABLE = True


# ── metrics against records with known answers ───────────────────────────────

def _line(path: Path, entry: dict) -> None:
    with open(path, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(entry) + "\n")


def _assistant(path: Path, ts: str, mid: str, usage: dict | None = None, tool: dict | None = None,
               model: str = "model-x") -> None:
    content = [tool] if tool else [{"type": "text", "text": "..."}]
    msg = {"role": "assistant", "id": mid, "model": model, "content": content}
    if usage is not None:
        msg["usage"] = usage
    _line(path, {"type": "assistant", "timestamp": ts, "uuid": str(uuid.uuid4()), "message": msg})


def _bash(path: Path, ts: str, mid: str, tid: str, command: str, stdout: str, usage: dict | None = None) -> None:
    _assistant(path, ts, mid, usage, {"type": "tool_use", "id": tid, "name": "Bash", "input": {"command": command}})
    _line(path, {"type": "user", "timestamp": ts, "uuid": str(uuid.uuid4()),
                 "toolUseResult": {"stdout": stdout, "stderr": ""},
                 "message": {"role": "user", "content": [{"type": "tool_result", "tool_use_id": tid,
                                                          "content": stdout, "is_error": False}]}})


def _ev(cid: str, passed: bool, fp: str) -> str:
    return f"PIPELINE-EVIDENCE id={cid} sha={'a' * 64} passed={'true' if passed else 'false'} fingerprint={fp} impl=x\n"


U = {"input_tokens": 10, "output_tokens": 5, "cache_creation_input_tokens": 100,
     "cache_creation": {"ephemeral_1h_input_tokens": 100, "ephemeral_5m_input_tokens": 0},
     "cache_read_input_tokens": 1000}


def synthetic_session(tmp_path: Path):
    root = tmp_path / "root.jsonl"
    # Before the start line: other work in the same session, not counted.
    _assistant(root, "2026-01-01T09:00:00Z", "m0", {"input_tokens": 999999, "output_tokens": 999999})
    _bash(root, "2026-01-01T10:00:00Z", "m1", "t1", "python -m pipeline.start --repo . --slug x --route a",
          "PIPELINE-RUN nonce=n1 route=a\n", U)
    # The CLI splits one response into entries that repeat its usage: one call.
    _assistant(root, "2026-01-01T10:00:00Z", "m1", U)
    _assistant(root, "2026-01-01T10:00:30Z", "m2", {"input_tokens": 1, "output_tokens": 1})
    subs = tmp_path / "subs"
    subs.mkdir()
    rev = subs / "agent-rev.jsonl"
    _line(rev, {"type": "user", "timestamp": "2026-01-01T10:01:00Z", "agentId": "rev",
                "message": {"role": "user", "content": "brief"}})
    _assistant(rev, "2026-01-01T10:01:30Z", "r1", {"input_tokens": 2, "output_tokens": 7},
               {"type": "tool_use", "id": "w1", "name": "Write", "input": {"file_path": "/run/03-review.md"}})
    _assistant(rev, "2026-01-01T10:05:00Z", "r2", {"input_tokens": 2, "output_tokens": 7})
    tst = subs / "agent-tst.jsonl"
    _line(tst, {"type": "user", "timestamp": "2026-01-01T10:03:00Z", "agentId": "tst",
                "message": {"role": "user", "content": "brief"}})
    run = "python -m pipeline.run_check execute /run unit --decision d"
    _bash(tst, "2026-01-01T10:04:00Z", "s1", "e1", run, _ev("unit", True, "aaa"), {"input_tokens": 1, "output_tokens": 1})
    _bash(tst, "2026-01-01T10:05:00Z", "s2", "e2", run, _ev("unit", True, "aaa"))   # same tree, had passed
    _bash(tst, "2026-01-01T10:06:00Z", "s3", "e3", run, _ev("unit", False, "bbb"))  # tree changed
    _bash(tst, "2026-01-01T10:07:00Z", "s4", "e4", run, _ev("unit", True, "bbb"))   # same tree, had failed
    _bash(tst, "2026-01-01T10:08:00Z", "s5", "e5", run, "run_check: not approved: no answer yet\n")
    _assistant(tst, "2026-01-01T10:09:00Z", "s6", None)
    # After the run: the session went on to other work, not counted either.
    _assistant(root, "2026-01-01T13:00:00Z", "m9", {"input_tokens": 777777, "output_tokens": 777777})
    r = read_transcript(root, is_subagent=False)
    a = read_transcript(rev, is_subagent=True)
    a.agent_id = "rev"
    b = read_transcript(tst, is_subagent=True)
    b.agent_id = "tst"
    return r, [a, b]


def test_metrics_report_time_calls_tokens_and_overlap_from_the_records(tmp_path):
    root, subs = synthetic_session(tmp_path)
    m = metrics.collect(root, subs, start_at="2026-01-01T10:00:00Z", end_at="2026-01-01T10:09:00Z",
                        writers={"rev": ["03-review.md"], "tst": ["04-test.md"]})
    assert m["wall_clock_s"] == 540.0                  # 10:00:00 -> 10:09:00
    assert m["subagent_overlap_s"] == 120.0            # review 10:01-10:05, test 10:03-10:09
    assert m["stage_time_s"] == 30.0 + 240.0 + 360.0   # each execution's own span, overlap counted twice
    assert m["model_calls"] == 2 + 2 + 1               # m1 once (split entries), m2; r1, r2; s1 (the rest carry no usage)
    assert m["tokens"]["output"] == 5 + 1 + 14 + 1     # neither the work before the start nor after the end
    assert m["tokens"]["cache_write_1h"] == 100 and m["tokens"]["cache_write"] == 0
    assert [s["stage"] for s in m["stages"]] == ["orchestration", "03-review.md#1", "04-test.md#1"]
    assert m["stages"][2]["checks_run"] == 4
    causes = [r["cause"].split(":")[0] for r in m["repeats"]]
    assert causes == ["avoidable", "revalidation", "retry"]
    assert m["orchestration_errors"][0]["error"] == "run_check: not approved: no answer yet"
    assert m["cost_usd"] is None and not m["cost_measured"]
    out = metrics.format_metrics(m)
    assert out.startswith("PIPELINE-METRICS wall=9m00s") and "cost=not measured" in out
    assert "repeated unit x3: 1 avoidable, 1 retry, 1 revalidation" in out
    assert "error (tst): run_check: not approved: no answer yet" in out


def test_a_baseline_rerun_is_never_called_avoidable():
    """A baseline always runs on the same snapshot; whether its copied tests
    changed is not in the record, so the cause is stated as conditional."""
    runs = [{"id": "unit@baseline", "passed": True, "fingerprint": "snap", "agent": "t", "at": "1"},
            {"id": "unit@baseline", "passed": True, "fingerprint": "snap", "agent": "t", "at": "2"}]
    assert metrics.classify_repeats(runs)[0]["cause"].startswith("baseline again")


def test_metrics_cost_needs_a_price_for_every_model(tmp_path):
    root, subs = synthetic_session(tmp_path)
    prices = {"model-x": {"input": 1.0, "output": 2.0, "cache_write": 3.0, "cache_write_1h": 4.0, "cache_read": 0.5}}
    m = metrics.collect(root, subs, start_at="2026-01-01T10:00:00Z", end_at="2026-01-01T10:09:00Z", prices=prices)
    tok = m["tokens"]
    expect = (tok["input"] * 1 + tok["output"] * 2 + tok["cache_write_1h"] * 4 + tok["cache_read"] * 0.5) / 1e6
    assert m["cost_usd"] == round(expect, 4) and m["cost_measured"]
    # A model with no price: the cost is not measured, never a partial sum.
    assert metrics.collect(root, subs, start_at="2026-01-01T10:00:00Z", end_at="2026-01-01T10:09:00Z",
                           prices={"other": prices["model-x"]})["cost_usd"] is None


def test_the_harness_writes_metrics_and_they_never_change_the_grade(repo, capsys):
    run_dir, _ = make_valid_run(repo)
    assert harness.main([str(run_dir)]) == 0
    out = capsys.readouterr().out
    assert out.startswith("PIPELINE-VERDICT HIGH") and "PIPELINE-METRICS wall=" in out
    m = json.loads((run_dir / "metrics.json").read_text(encoding="utf-8"))
    assert m["subagents"] == 3 and m["executions"] >= 3
    assert {s["stage"].split("#")[0] for s in m["stages"]} >= {"03-review.md", "04-test.md", "08-audit.md"}
    assert "metrics" not in json.loads((run_dir / "verdict.json").read_text(encoding="utf-8"))


# ── preflight: a check that cannot pass is refused before it runs ────────────

def _plan(run_dir: Path, block: str) -> None:
    """Rewrite the plan without running anything (preflight reads the plan)."""
    (run_dir / "01-plan.md").write_text("# 01 Plan\n\n" + block + "\n", encoding="utf-8")


def test_preflight_refuses_a_command_naming_a_missing_test_file(repo):
    run_dir, _ = make_valid_run(repo)
    _plan(run_dir, PLAN_BLOCK.replace("cmd: {python} -c \"print('clean')\"",
                                              "cmd: {python} tests/test_missing.py"))
    with pytest.raises(PlanError, match="preflight.*test_missing.py.*does not exist"):
        run_check.authorize(run_dir, "lint", policy="auto")
    assert run_check.preflight(run_dir, "unit") == []


def test_preflight_refuses_a_module_the_interpreter_does_not_have(repo):
    run_dir, _ = make_valid_run(repo)
    _plan(run_dir, PLAN_BLOCK.replace("cmd: {python} -c \"print('clean')\"",
                                              "cmd: {python} -m no_such_runner_xyz"))
    problems = run_check.preflight(run_dir, "lint")
    assert problems and "no_such_runner_xyz" in problems[0]


def test_preflight_refuses_a_stacked_quiet_flag_when_expect_needs_the_summary(repo):
    run_dir, _ = make_valid_run(repo)
    (repo / "pytest.ini").write_text("[pytest]\naddopts = -q\n", encoding="utf-8")
    block = PLAN_BLOCK.replace("cmd: {python} -c \"print('clean')\"\n  expect: clean",
                               "cmd: {python} -m pytest -q test_sub.py\n  expect: \\d+ passed")
    _plan(run_dir, block)
    assert any("already passes -q" in p for p in run_check.preflight(run_dir, "lint"))
    # The same command with an expect that does not need the summary line is fine.
    _plan(run_dir, block.replace("expect: \\d+ passed\n  required: false", "expect: 100%\n  required: false"))
    assert not any("already passes -q" in p for p in run_check.preflight(run_dir, "lint"))


def test_preflight_refuses_baseline_files_that_match_nothing(repo):
    run_dir, _ = make_valid_run(repo)
    _plan(run_dir, PLAN_BLOCK.replace("baseline_files: test_sub.py", "baseline_files: test_gone.py"))
    assert any("match no file" in p for p in run_check.preflight(run_dir, "unit", baseline=True))


# ── reuse: identical evidence is not produced twice ─────────────────────────

def test_authorize_reuses_evidence_that_already_proves_the_same_operation(repo, capsys, auto_policy):
    run_dir, _ = make_valid_run(repo)
    capsys.readouterr()
    assert run_check.main(["authorize", str(run_dir), "lint"]) == 0
    out = capsys.readouterr().out
    assert out.startswith("PIPELINE-REUSE id=lint ") and "PIPELINE-DECISION" not in out
    # --rerun asks again.
    assert run_check.main(["authorize", str(run_dir), "lint", "--rerun"]) == 0
    assert "PIPELINE-DECISION" in capsys.readouterr().out


def test_reuse_stops_the_moment_the_tree_changes(repo, capsys, auto_policy):
    run_dir, _ = make_valid_run(repo)
    (repo / "thing.py").write_text((repo / "thing.py").read_text(encoding="utf-8") + "\nZ = 1\n", encoding="utf-8")
    capsys.readouterr()
    assert run_check.main(["authorize", str(run_dir), "lint"]) == 0
    out = capsys.readouterr().out
    assert "PIPELINE-REUSE" not in out and "PIPELINE-DECISION" in out


def test_reuse_refuses_evidence_whose_decision_does_not_match(repo):
    run_dir, _ = make_valid_run(repo)
    op = run_check.operation_for(run_dir, "lint")
    assert run_check.reusable_evidence(run_dir, "lint", op)
    p = run_dir / "evidence" / "lint.json"
    rec = json.loads(p.read_text(encoding="utf-8"))
    rec["stdout_tail"] = "edited by hand\n"
    p.write_text(json.dumps(rec), encoding="utf-8")
    assert run_check.reusable_evidence(run_dir, "lint", op) is None


# ── depth: comments and test fixtures do not escalate; real risk still does ──

def test_a_risky_word_in_a_comment_or_a_test_fixture_does_not_force_full_depth(repo):
    base = _git(repo, "rev-parse", "HEAD").strip()
    (repo / "thing.py").write_text("def add(a, b):\n    # never call this with shell=True or subprocess.run(\n"
                                   "    return a + b\n", encoding="utf-8")
    (repo / "tests").mkdir()
    (repo / "tests" / "test_thing.py").write_text(
        "import subprocess, threading\n\ndef test_add():\n    subprocess.run(['echo'], check=True)\n"
        "    threading.Thread(target=print).start()\n    html = '<b>' ; el_innerHTML = None\n", encoding="utf-8")
    depth, reasons, _ = depth_mod.required_depth(repo, base)
    assert depth == "light", reasons


def test_risk_that_weakens_tests_or_ships_in_code_still_forces_full_depth(repo):
    base = _git(repo, "rev-parse", "HEAD").strip()
    (repo / "tests").mkdir()
    (repo / "tests" / "test_thing.py").write_text("import pytest\n\n@pytest.mark.skip\ndef test_add():\n    pass\n",
                                                  encoding="utf-8")
    depth, reasons, _ = depth_mod.required_depth(repo, base)
    assert depth == "full" and any("skipped test" in r for r in reasons)
    (repo / "tests" / "test_thing.py").unlink()
    (repo / "thing.py").write_text("import subprocess\ndef add(a, b):\n    subprocess.run(['ls'])\n    return a + b\n",
                                   encoding="utf-8")
    depth, reasons, _ = depth_mod.required_depth(repo, base)
    assert depth == "full" and any("process execution in thing.py" in r for r in reasons)
    # A suppressed check is a comment, and still counts.
    (repo / "thing.py").write_text("def add(a, b):\n    return a + b  # noqa\n", encoding="utf-8")
    assert depth_mod.required_depth(repo, base)[0] == "full"
    # conftest.py steers every test: scanned like production code.
    (repo / "thing.py").write_text("def add(a, b):\n    return a + b\n", encoding="utf-8")
    (repo / "tests" / "conftest.py").write_text("import subprocess\nsubprocess.run(['ls'])\n", encoding="utf-8")
    assert depth_mod.required_depth(repo, base)[0] == "full"
    assert not depth_mod.is_test_file("tests/conftest.py") and depth_mod.is_test_file("tests/helpers.py")
