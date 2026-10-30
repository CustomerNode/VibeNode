"""Authorize, then execute, one verification criterion; record the evidence.

    python -m pipeline.run_check authorize <run-dir> <criterion-id> [--baseline] [--rerun] [--repo PATH]
    python -m pipeline.run_check execute   <run-dir> <criterion-id> --decision <id> [--baseline] [--repo PATH]

``authorize`` binds the exact operation (the expanded command, the working
directory, the repository, the plan, the source fingerprint, the environment
names it will pass) to a decision (``pipeline.decisions``).  Under the user's
``auto`` permission policy the decision is allowed at once and the line
``PIPELINE-DECISION id=<id> ... verdict=allow`` is printed.  Under any other
policy it is ``pending`` and the card to end the turn with is printed after
it: the user's tap on that card is the answer, read by ``execute`` from the
session transcript.  Nothing waits, so neither a slow answer nor a long
suite meets the shell tool's time limit.

``execute`` rebuilds the operation from the state NOW and refuses when it
differs from the one approved (code, plan, command, environment); otherwise
it runs the command with ``pipeline.evidence.run_criterion`` (or
``run_baseline`` with ``--baseline``: the regression demonstration against
the starting snapshot in an isolated worktree) and writes the evidence with
the decision id in it.  A decision is used once.  A criterion whose timeout
is above nine minutes is run as a background shell command; the harness
reads the engine's output file for it.

Before any of that, ``authorize`` runs two cheap, deterministic steps:

* **preflight** refuses a check that cannot succeed as written (a missing
  ``{python} -m`` module, a test file named in the command that does not
  exist, a missing working directory, a stacked pytest ``-q`` that hides the
  line ``expect`` waits for, baseline files that match nothing): exit 2,
  nothing run, no approval asked;
* **reuse**: when the evidence on disk already passed for exactly this
  operation (same command, plan, tree, interpreter state and pipeline, under
  a used decision), it prints ``PIPELINE-REUSE`` and stops: running it again
  would prove nothing new.  ``--rerun`` runs it anyway.  The harness still
  grades the evidence on disk by its own rules, so reuse cannot make stale or
  dependent evidence count.

Exit status: 0 passed, 1 failed, 2 plan or run error, 3 not authorized.
The ``PIPELINE-EVIDENCE`` line is printed first and last; the command's own
output is echoed between them, so a failure's traceback is in the transcript
as well as in the evidence file.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shlex
import sys
from pathlib import Path

from pipeline import cards, decisions
from pipeline.evidence import (EVIDENCE_DIR, _file_sha, baseline_label, checked_repo, child_env, evidence_line,
                               evidence_sha, expand_cmd, impl_identity, load_run, overlay_files, run_baseline,
                               run_criterion, site_fingerprint, tree_fingerprint)
from pipeline.plan import PlanError, load_plan, request_text_of

LONG_CHECK_S = 9 * 60


def operation_for(run_dir: Path, cid: str, *, baseline: bool = False, repo: Path | None = None) -> dict:
    """The exact operation a decision binds, computed from the state now."""
    run_dir = run_dir.resolve()
    run = load_run(run_dir)
    repo = checked_repo(run_dir, repo)
    criteria, plan_sha, plan_src = load_plan(run_dir)
    by_id = {c.id: c for c in criteria}
    if cid not in by_id:
        raise PlanError(f"no criterion {cid!r} in {plan_src.name}; have: {', '.join(by_id)}")
    c = by_id[cid]
    if not c.executable:
        raise PlanError(f"criterion {cid!r} is marked executable: false ({c.reason}); nothing to run")
    if baseline and not c.detects_change:
        raise PlanError(f"criterion {cid!r} is not marked detects_change; it has no baseline demonstration")
    cwd = (repo / c.cwd).resolve() if c.cwd else repo
    _env, dropped = child_env()
    return {
        "run_id": run.get("run_id"),
        "criterion": cid,
        "baseline": bool(baseline),
        "cmd": expand_cmd(c.cmd),
        "cwd": str(cwd),
        "repo": str(repo),
        "plan_sha": plan_sha,
        "fingerprint": str(run.get("base_snapshot") or "") if baseline else tree_fingerprint(repo),
        "env_dropped": dropped,
        "timeout": c.timeout,
        "impl": impl_identity()[0],
    }


# ── preflight: refuse a check that cannot succeed, before anyone approves or
# waits for it.  Each rule is a failure an earlier run paid for in full: a
# suite that ran for minutes and then failed for a reason readable from the
# plan alone.

_PY_MODULE_RE = re.compile(r"^\s*\{python\}\s+-m\s+([A-Za-z_][\w.]*)")
_PATH_SUFFIXES = (".py", ".js", ".mjs", ".cjs", ".ts", ".tsx", ".jsx")
# Options whose value is not a path that must exist.
_OPTION_TAKES_VALUE = {"-k", "-m", "-p", "-c", "-o", "--deselect", "--ignore", "--ignore-glob", "--rootdir",
                       "--confcutdir", "--basetemp", "--junitxml", "--log-file", "--timeout", "-n", "--dist"}
_QUIET_ADDOPTS_RE = re.compile(r"^\s*addopts\s*=.*(?:^|\s)(?:-q|--quiet)\b", re.MULTILINE)


def _pytest_ini_quiet(repo: Path) -> str | None:
    """The configuration file whose pytest ``addopts`` already passes -q."""
    for name in ("pytest.ini", "tox.ini", "setup.cfg", "pyproject.toml"):
        p = repo / name
        if p.is_file():
            text = p.read_text(encoding="utf-8", errors="replace")
            if name != "pytest.ini" and "pytest" not in text:
                continue
            if _QUIET_ADDOPTS_RE.search(text):
                return name
    return None


def preflight(run_dir: Path, cid: str, *, baseline: bool = False, repo: Path | None = None) -> list[str]:
    """Problems that make *cid* certain to fail or impossible to run, found
    without running it: a ``{python} -m`` module this interpreter does not
    have, a test file or node id named in the command that does not exist, a
    working directory that does not exist, a pytest command whose ``-q``
    stacks on a configured ``-q`` while ``expect`` waits for the "N passed"
    line that stacking hides, and (``baseline``) baseline files that match
    nothing.  An empty list means nothing was found, not that it will pass."""
    import importlib.util
    run_dir = run_dir.resolve()
    repo = checked_repo(run_dir, repo)
    criteria, _sha, _src = load_plan(run_dir)
    c = {x.id: x for x in criteria}.get(cid)
    if c is None or not c.executable:
        return []
    problems: list[str] = []
    cwd = (repo / c.cwd).resolve() if c.cwd else repo
    if not cwd.is_dir():
        problems.append(f"working directory {c.cwd!r} does not exist in the repository")
        return problems
    m = _PY_MODULE_RE.match(c.cmd)
    if m and importlib.util.find_spec(m.group(1).split(".")[0]) is None:
        problems.append(f"module {m.group(1)!r} is not installed for {sys.executable}; the check would fail "
                        f"before running anything")
    try:
        tokens = shlex.split(c.cmd, posix=os.name != "nt")
    except ValueError:
        tokens = c.cmd.split()
    prev = ""
    for tok in tokens:
        raw = tok.strip("\"'")
        if not raw.startswith("-") and prev not in _OPTION_TAKES_VALUE and "{" not in raw and "*" not in raw:
            path_part = raw.split("::", 1)[0]
            if ("::" in raw or path_part.endswith(_PATH_SUFFIXES)) and not (cwd / path_part).exists():
                problems.append(f"{path_part!r} named in the command does not exist under {cwd.name or cwd}")
        prev = raw if raw.startswith("-") and "=" not in raw else ""
    if re.search(r"-m\s+pytest\b", c.cmd) and re.search(r"(?:^|\s)(?:-q|--quiet)\b", c.cmd) \
            and c.expect and "passed" in c.expect:
        cfg = _pytest_ini_quiet(repo)
        if cfg:
            problems.append(f"{cfg} already passes -q and the command adds another; pytest then prints no "
                            f"'N passed' line, so expect {c.expect!r} can never match")
    if baseline and c.detects_change and not overlay_files(repo, c.baseline_globs()):
        problems.append(f"baseline_files {c.baseline_files!r} match no file")
    return problems


def reusable_evidence(run_dir: Path, cid: str, op: dict, *, baseline: bool = False) -> str | None:
    """The sha of evidence that already proves exactly this operation: it
    passed (for a baseline: demonstrated), under a used decision, with the
    same command, plan, tree (or snapshot and copied tests), interpreter
    state and pipeline implementation.  Running it again would add nothing.
    None when anything differs or the record cannot be trusted."""
    path = run_dir / EVIDENCE_DIR / (f"{cid}.baseline.json" if baseline else f"{cid}.json")
    if not path.is_file():
        return None
    try:
        rec = json.loads(path.read_text(encoding="utf-8"))
    except ValueError:
        return None
    sha = evidence_sha(path)
    same = (rec.get("passed") is True and rec.get("plan_sha") == op["plan_sha"]
            and expand_cmd(str(rec.get("cmd") or "")) == op["cmd"] and rec.get("impl") == op["impl"]
            and rec.get("site_sha") == site_fingerprint())
    if baseline:
        repo = Path(op["repo"])
        same = same and rec.get("snapshot") == op["fingerprint"] and all(
            _file_sha(repo / rel) == s for rel, s in (rec.get("overlay") or {}).items())
    else:
        same = same and rec.get("fingerprint") == op["fingerprint"] and rec.get("tree_stable", True)
    if not same or not rec.get("decision"):
        return None
    try:
        d = decisions.load(run_dir, str(rec["decision"]))
    except decisions.DecisionError:
        return None
    return sha if d.get("used") and d.get("evidence_sha") == sha else None


def reuse_line(cid: str, sha: str, op: dict) -> str:
    return (f"PIPELINE-REUSE id={cid} evidence={sha[:12]} fingerprint={str(op['fingerprint'])[:12]}: already "
            f"passed on this tree with this plan; nothing to run")


def _task(run_dir: Path) -> str:
    p = run_dir / "00-request.md"
    text = request_text_of(p.read_text(encoding="utf-8", errors="replace")) if p.is_file() else ""
    return cards.task_name(text or "this task")


def authorize(run_dir: Path, cid: str, *, baseline: bool = False, repo: Path | None = None,
              policy: str | None = None) -> dict:
    run_dir = run_dir.resolve()
    op = operation_for(run_dir, cid, baseline=baseline, repo=repo)
    problems = preflight(run_dir, cid, baseline=baseline, repo=repo)
    if problems:
        raise PlanError(f"preflight: criterion {cid!r} cannot pass as written: " + "; ".join(problems)
                        + ". Fix the plan or the tree; nothing was run and no approval was asked")
    task = _task(run_dir)
    return decisions.issue(run_dir, "check", op, policy=policy,
                           build=lambda nonce: cards.permission_card(op, nonce, task))


class NotAuthorized(Exception):
    pass


def execute(run_dir: Path, cid: str, decision: str, *, baseline: bool = False,
            repo: Path | None = None) -> tuple[dict, Path, dict]:
    """Run the criterion under *decision*.  Returns (record, path, decision)."""
    run_dir = run_dir.resolve()
    rec = decisions.load(run_dir, decision)
    if rec.get("kind") != "check":
        raise NotAuthorized(f"decision {decision} is not a check decision")
    if rec.get("used"):
        raise NotAuthorized(f"decision {decision} was already used; authorize again")
    op = operation_for(run_dir, cid, baseline=baseline, repo=repo)
    if decisions.operation_sha(op) != rec.get("operation_sha"):
        changed = [k for k in op if op[k] != (rec.get("operation") or {}).get(k)]
        raise NotAuthorized(f"the operation is not the one approved ({', '.join(changed) or 'differs'}); "
                            f"state changed since the approval, authorize again")
    ans, detail = decisions.answer(run_dir, rec)
    if ans == "deny":
        decisions.update(run_dir, decision, verdict="deny", used=True, used_at=decisions._now(), answer=detail)
        raise NotAuthorized(f"the user refused: {detail}")
    if ans != "allow":
        raise NotAuthorized(f"not approved: {detail}")
    decisions.update(run_dir, decision, started_at=decisions._now(), answer=detail)
    extra = {"decision": decision, "authorized": True, "decision_how": rec.get("how")}
    runner = run_baseline if baseline else run_criterion
    record, path = runner(run_dir, cid, repo=repo, extra=extra)
    rec = decisions.update(run_dir, decision, used=True, used_at=decisions._now(), finished_at=decisions._now(),
                           evidence_sha=evidence_sha(path), passed=bool(record.get("passed")))
    return record, path, rec


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m pipeline.run_check")
    sub = ap.add_subparsers(dest="step", required=True)
    for name in ("authorize", "execute"):
        sp = sub.add_parser(name)
        sp.add_argument("run_dir")
        sp.add_argument("criterion_id")
        sp.add_argument("--repo", default=None, help="repository to run in (default: the run's)")
        sp.add_argument("--baseline", action="store_true",
                        help="regression demonstration: run against the starting snapshot; passes when it FAILS there")
        if name == "execute":
            sp.add_argument("--decision", required=True, help="the decision id authorize printed")
            sp.add_argument("--quiet", action="store_true", help="do not echo the command's output")
        else:
            sp.add_argument("--rerun", action="store_true",
                            help="run it again even when evidence for this exact tree and plan already passed")
    a = ap.parse_args(argv)
    # A Windows console is cp1252: echoing a test's "…" crashed the first
    # qualification run's check after the evidence was written.  Print what
    # cannot be encoded as a replacement instead of dying mid-report.
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(errors="replace")
        except (AttributeError, ValueError):
            pass
    run_dir = Path(a.run_dir)
    repo = Path(a.repo) if a.repo else None
    try:
        if a.step == "authorize":
            if not a.rerun:
                op = operation_for(run_dir, a.criterion_id, baseline=a.baseline, repo=repo)
                sha = reusable_evidence(run_dir.resolve(), a.criterion_id, op, baseline=a.baseline)
                if sha:
                    print(reuse_line(baseline_label(a.criterion_id) if a.baseline else a.criterion_id, sha, op))
                    print("run_check: the evidence on disk already proves this exact check on this tree; it stands. "
                          "Pass --rerun to run it anyway (for example, when the harness reports that the evidence "
                          "was not produced by an independent checker).", file=sys.stderr)
                    return 0
            rec = authorize(run_dir, a.criterion_id, baseline=a.baseline, repo=repo)
            print(decisions.decision_line(rec))
            if rec["verdict"] == "pending":
                print()
                print(cards.fenced(rec["card"]))
                print("run_check: the user must allow this command. End your turn with the card above, alone; "
                      "when the user answers it, run the execute step with this decision id.", file=sys.stderr)
            elif rec["operation"]["timeout"] > LONG_CHECK_S:
                print(f"run_check: this check may run up to {rec['operation']['timeout']}s; run the execute step "
                      f"as a background shell command.", file=sys.stderr)
            return 0
        record, path, rec = execute(run_dir, a.criterion_id, a.decision, baseline=a.baseline, repo=repo)
    except NotAuthorized as e:
        print(f"run_check: {e}", file=sys.stderr)
        return 3
    except (PlanError, decisions.DecisionError) as e:
        print(f"run_check: {e}", file=sys.stderr)
        return 2
    except (RuntimeError, OSError) as e:
        print(f"run_check: {e}", file=sys.stderr)
        return 2
    sha = evidence_sha(path)
    if a.baseline:
        line = evidence_line(baseline_label(record["id"]), sha, record["passed"], str(record["snapshot"]),
                             record.get("impl"))
    else:
        line = evidence_line(record["id"], sha, record["passed"], record["fingerprint"], record.get("impl"))
    # The line is printed BEFORE the echoed output as well as after it.  The
    # Session Engine keeps only a short preview of a tool result over ~30 KB
    # (the rest goes to a side file the harness does not read), and a failing
    # suite's echo can be 40 KB: a line printed only last would be lost from the
    # transcript and genuine evidence would grade as unrecorded.
    print(line)
    if not a.quiet:
        if record["stdout_tail"]:
            print(record["stdout_tail"], end="" if record["stdout_tail"].endswith("\n") else "\n")
        if record["stderr_tail"]:
            print(record["stderr_tail"], end="" if record["stderr_tail"].endswith("\n") else "\n", file=sys.stderr)
    print(decisions.decision_line(rec))
    print(line)
    if record["timed_out"]:
        print(f"run_check: {record['id']} timed out after {record['elapsed_s']}s", file=sys.stderr)
    if a.baseline and not record["passed"]:
        why = ("it passed without the change: the test does not detect it" if not record["failed"]
               else f"it failed, but not with {record['fails_with']!r}: the failure is unrelated to the change")
        print(f"run_check: baseline for {record['id']} did not demonstrate the change; {why}", file=sys.stderr)
    if not a.baseline and not record["tree_stable"]:
        print(f"run_check: {record['id']} changed the working tree while running; evidence is not stable", file=sys.stderr)
    return 0 if record["passed"] else 1


if __name__ == "__main__":
    sys.exit(main())
