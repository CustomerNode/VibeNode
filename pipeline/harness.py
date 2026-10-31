"""Compute a run's confidence grade from what the Session Engine recorded.

    python -m pipeline.harness <run-dir> [--json] [--expect HIGH]

The grade is never awarded by an agent.  It is computed here from evidence
none of which an agent can produce by writing a file:

* **The start line.**  ``pipeline.start`` printed the run's route, the hash
  of the user's request, and the hashes of the repository's pinned checks and
  pinned protection; the engine recorded that line.  ``run.json`` and
  ``00-request.md`` are read, but a route, a request or a declaration that
  differs from the recorded line is tampering, graded LOW.
* **Executions.**  Each criterion in the plan's ``verification`` block has an
  evidence file that ``pipeline.run_check`` wrote, and some transcript shows a
  shell command printing the ``PIPELINE-EVIDENCE`` line with the SHA of the
  bytes on disk.  A typed "427 passed" in ``04-test.md`` counts for nothing.
* **Provenance and attestation.**  Each checker stage file (review, test,
  audit) was written by a subagent, in its own transcript, from a brief that
  does not paste the builder's output, and that subagent ran
  ``pipeline.attest`` on it: the attest line's sha is the file's sha NOW (a
  report changed after the checker signed it is not the checker's report) and
  its fingerprint is the tree NOW (a reviewer that examined an earlier tree
  has not reviewed the final code).  A stage the root session wrote or
  attested is self-grading.
* **Revision.**  Every required criterion ran against the tree being graded:
  the fingerprint in its evidence equals the working tree's content
  fingerprint now (HEAD is not part of it, so a commit of unchanged code
  changes nothing), and the plan block it read is the plan block on disk.
* **Depth.**  Computed from the diff by rule, with the protection pinned at
  the start; a checker's ``pipeline.escalate`` makes it full for good.
* **Completeness.**  Every stage the route and depth require exists, the
  audit answers the fourth question and judges coverage criterion by
  criterion, ending ``Verdict: covered``.
* **The pinned suite** runs on the final tree; **the change is detected**
  (on routes a, b, c a regression demonstration fails on the starting
  snapshot for the reason the plan named); **checkers are distinct and in
  order**.
* **The register** (``pipeline.findings``): every ``SCENARIO:`` the plan
  names and every ``FINDING:``/``BLOCKING:`` a checker raises (including one
  in an earlier attested version of a report, and a ``NOTE:`` worded as a
  correctness concern) reaches FIXED, NOT-A-DEFECT or VERIFIED with a check
  that covers it and passed on the final tree (FIXED also needs a recorded
  failure before the fix), or is disclosed UNRESOLVED.  ``RESOLVED:`` alone
  settles nothing.  An open or unresolved item caps the grade at MEDIUM.

Severities: ``block`` (the run cannot be graded), ``low`` (a failed, forged
or tampered fact: LOW), ``medium`` (missing independence or stale evidence:
MEDIUM at most), ``info``.  HIGH requires no finding above ``info``.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from pipeline import depth as depth_mod
from pipeline import findings as findings_mod
from pipeline import metrics as metrics_mod
from pipeline.attest import ATTEST_PREFIX, load_attestations
from pipeline.escalate import ESCALATE_PREFIX
from pipeline.evidence import (_is_git_repo, git, EVIDENCE_DIR, EVIDENCE_PREFIX, RUNS_SUBDIR, _file_sha, baseline_label, evidence_sha,
                               history_records, impl_identity, interpreter_site_dirs, load_run, local_excludes, run_repo,
                               site_fingerprint, tree_fingerprint)
from pipeline.decisions import (DECISION_PREFIX, DECISIONS_DIR, DecisionError, answer as decision_answer,
                                load as load_decision)
from pipeline.plan import (PINNED_FILE, PINNED_PROTECTED_FILE, PROTECTED_FILE, VERIFY_FILE, PlanError, block_sha,
                           load_plan, request_sha, request_text_of)
from pipeline.transcripts import (_SHELL_WRITE_RE, Transcript, find_root_transcript, is_pipeline_call,
                                  strip_data_heredocs,
                                  is_run_check_call,
                                  read_transcript, same_file, shell_writes_under, subagents_of, writers_of,
                                  writes_under)

GRADES = ("HIGH", "MEDIUM", "LOW", "BLOCKED")

# The stage each route starts with, and the checker stages each depth requires
# (light depth drops the Plan and Review stages, never Test or Audit).
ROUTE_ENTRY = {"a": "01-plan.md", "b": "05-debug.md", "c": "06-hotfix.md", "d": "07-refactor.md"}
CHECKER_STAGES_FULL = ("03-review.md", "04-test.md", "08-audit.md")
CHECKER_STAGES_LIGHT = ("04-test.md", "08-audit.md")
# The fourth question is a HEADING in the audit, so a mention of the phrase in
# the introduction does not count as an answered section.
FOURTH_QUESTION_RE = re.compile(r"^#+[^\n]*what\s+could\s+still\s+be\s+wrong[^\n]*$", re.IGNORECASE | re.MULTILINE)
# Passing checks show the code does what the checks test, not that the checks
# test what the user asked for: five trivial tests can all pass on an
# unfinished feature.  So the audit, an independent subagent, must also answer
# under its own heading whether the checks prove the request, name every
# required criterion while doing it, and commit to a verdict line the harness
# reads.  The judgment stays an agent's; the program enforces that a fresh
# checker made it, explicitly, and that a "no" costs the grade.
COVERAGE_RE = re.compile(r"^#+[^\n]*checks\s+prove\s+the\s+request[^\n]*$", re.IGNORECASE | re.MULTILINE)
COVERAGE_VERDICT_RE = re.compile(r"^[\s>*_-]*verdict[\s*_]*:[\s*_]*(not\s+covered|covered)[\s*_.]*$",
                                 re.IGNORECASE | re.MULTILINE)
VERDICT_LINE_RE = re.compile(r"^[ \t>*_-]*verdict[ \t*_]*:", re.IGNORECASE | re.MULTILINE)
# Routes whose request changes behavior, so a regression demonstration
# applies; (d) is cleanup with no behavior change.
BASELINE_ROUTES = ("a", "b", "c")
# An audit written this long before the evidence it relies on is stale.
STALE_TOLERANCE_S = 2.0
# A checker's brief is the request, the run directory, the spec path and the
# diff summary; a brief longer than this is carrying someone's transcript.
MAX_BRIEF_CHARS = 12_000
# A line of the builder's or planner's report this long, found verbatim in a
# checker's brief, is a paste; shorter lines (a file name, a heading) are not.
PASTE_MIN_LINE = 100


@dataclass
class Finding:
    code: str
    severity: str  # block | low | medium | info
    message: str


@dataclass
class Verdict:
    grade: str = "BLOCKED"
    findings: list[Finding] = field(default_factory=list)
    criteria: list[dict] = field(default_factory=list)
    facts: dict = field(default_factory=dict)
    # Where the run's time and tokens went (pipeline.metrics).  Kept out of
    # to_dict: it never takes part in the grade.
    metrics: dict = field(default_factory=dict)

    def add(self, code: str, severity: str, message: str) -> None:
        # One finding per fact: a checker that wrote the plan twice is one
        # finding, not two.
        if any(f.code == code and f.message == message for f in self.findings):
            return
        self.findings.append(Finding(code, severity, message))

    def finish(self) -> "Verdict":
        sev = {f.severity for f in self.findings}
        if "block" in sev:
            self.grade = "BLOCKED"
        elif "low" in sev:
            self.grade = "LOW"
        elif "medium" in sev:
            self.grade = "MEDIUM"
        else:
            self.grade = "HIGH"
        return self

    def to_dict(self) -> dict:
        return {
            "grade": self.grade,
            "findings": [asdict(f) for f in self.findings],
            "criteria": self.criteria,
            "facts": self.facts,
        }


def required_stages(route: str, depth: str) -> list[str]:
    """The stage files a run on *route* at *depth* must have, in order.  (A
    light run on route (a) has no ``01-plan.md``; its verification block
    lives in ``00-plan.md``, which ``load_plan`` finds.)"""
    stages = ["00-request.md"]
    entry = ROUTE_ENTRY.get(route, "01-plan.md")
    if not (depth == "light" and entry == "01-plan.md"):
        stages.append(entry)
    stages += ["02-build.md"]
    stages += list(CHECKER_STAGES_FULL if depth == "full" else CHECKER_STAGES_LIGHT)
    return stages


def _long_lines(text: str) -> list[str]:
    return [ln.strip() for ln in text.splitlines() if len(ln.strip()) >= PASTE_MIN_LINE]


def _check_brief(v: Verdict, stage: str, t: Transcript, builder_lines: list[str]) -> None:
    brief = t.kickoff_prompt or ""
    if not brief:
        v.add("brief-missing", "medium", f"{stage}: checker {t.agent_id} has no recorded kickoff prompt")
        return
    if len(brief) > MAX_BRIEF_CHARS:
        v.add("brief-long", "medium",
              f"{stage}: checker {t.agent_id}'s brief is {len(brief)} chars; a fresh brief is the request, "
              f"the run directory, the spec path and the diff, not a transcript")
    pasted = [ln for ln in builder_lines if ln in brief]
    if pasted:
        v.add("brief-pasted", "medium",
              f"{stage}: checker {t.agent_id}'s brief contains {len(pasted)} line(s) of the builder's or planner's "
              f"report; a checker judges from the code and the artifacts, not from the builder's account")


def grade_run(run_dir: Path, *, prices: dict | None = None) -> Verdict:
    """Compute the verdict for the run in *run_dir* from the engine's records.

    Reads ``run.json``, the stage files, the evidence folder, the working tree
    and the CLI transcripts under ``~/.claude/projects``; writes nothing.
    The run's metrics (``pipeline.metrics``) are computed from the same
    records into ``v.metrics``; *prices* (per-million-token, per model)
    turns token counts into an API-equivalent cost.
    """
    v = Verdict()
    run_dir = run_dir.resolve()
    v.facts["run_dir"] = str(run_dir)
    try:
        run = load_run(run_dir)
    except PlanError as e:
        v.add("no-run", "block", str(e))
        return v.finish()
    try:
        repo = run_repo(run_dir)
    except PlanError as e:
        v.add("no-repo", "block", str(e))
        return v.finish()
    claimed_repo = run.get("repo")
    if claimed_repo and os.path.normcase(str(Path(claimed_repo).resolve())) != os.path.normcase(str(repo)):
        v.add("repo-tampered", "low",
              f"run.json names repository {claimed_repo}, but this run lives in {repo}; the run is graded "
              f"against the repository it belongs to")
    if not repo.is_dir():
        v.add("no-repo", "block", f"repository {repo} is not a directory")
        return v.finish()
    nonce = str(run.get("nonce") or "")
    project_cwd = str(run.get("project_cwd") or repo)
    v.facts.update({"run_id": run.get("run_id"), "repo": str(repo)})

    # 1. The engine's records of this run, and the fixed point: the start line.
    root_path = find_root_transcript(project_cwd, f"PIPELINE-RUN nonce={nonce}") if nonce else None
    if root_path is None:
        v.add("no-transcript", "block",
              "no session transcript records running pipeline.start for this run "
              f"(looked for the nonce under the transcripts of {project_cwd})")
        return v.finish()
    root = read_transcript(root_path, is_subagent=False)
    subs = subagents_of(root_path)
    agent_call_ids = {c.tool_use_id for c in root.calls_named("Agent")}
    linked = {t.agent_id for t in subs if str(t.meta.get("toolUseId") or "") in agent_call_ids}
    everything: list[Transcript] = [root, *subs]
    shell_calls = [(t, c) for t in everything for c in t.calls_named("Bash", "PowerShell")]
    _attach_background_output(v, shell_calls)
    v.facts["root_transcript"] = str(root_path)
    v.facts["subagents"] = [t.agent_id for t in subs]
    fields = _start_fields(_start_line(root, nonce))
    _check_dir(v, run_dir, fields)
    route = _check_route(v, run, fields)
    v.facts["route"] = route
    _check_base(v, run, fields)
    _check_request(v, run_dir, run, fields)
    _check_pinned(v, run_dir, run, repo, fields)
    pinned_patterns = _check_protected(v, run_dir, run, repo, fields)
    # Attestations and escalations count only for THIS run: one session can
    # hold several runs, and a signed report or an escalation from an earlier
    # run must not leak into this one (found by the Final Audit of the
    # evidence-gaps run, where the earlier run's findings capped it).  An
    # attestation must name this run; an escalation is dropped only when it
    # names another run, so a spelling the filter cannot read never lowers
    # the depth.
    run_calls = [(t, c) for t, c in shell_calls if _names_run(c.command, run_dir)]
    # Anything that only ever ADDS strictness (an escalation, a BLOCKING id in
    # a signed report) is read leniently: dropped only when the command plainly
    # names another run.  Found by the Test Team, loop 3: a first sign-off
    # written with ``.`` or ``$RUN`` let its BLOCKING vanish from the history.
    lenient_calls = [(t, c) for t, c in shell_calls if not _names_other_run(c.command, run_dir)]
    escalations = _escalations(lenient_calls)
    now_fp = tree_fingerprint(repo)
    v.facts["fingerprint"] = now_fp
    # Untracked files are listed against the repository's own .gitignore only,
    # so an entry here hides nothing from the grade; it is reported because a
    # person reading the verdict should know the repository carries one.
    excludes = local_excludes(repo)
    if excludes:
        v.facts["local_excludes"] = excludes
        v.add("local-excludes", "info",
              f".git/info/exclude lists {', '.join(excludes[:5])}{'...' if len(excludes) > 5 else ''}; "
              f"those files still count toward the fingerprint and the depth")

    # 2. Depth, by rule, from the diff as it is now, with the pinned protection.
    depth, reasons, dfacts = depth_mod.required_depth(repo, str(run.get("base_rev") or ""),
                                                      pinned_patterns=pinned_patterns, escalations=escalations)
    v.facts["depth"] = depth
    v.facts["depth_reasons"] = reasons
    v.facts["changed_files"] = dfacts["files"]
    if escalations:
        v.facts["escalated"] = escalations
    claimed = run.get("depth")
    if claimed and claimed != depth:
        v.add("depth-mismatch", "medium", f"run.json says depth {claimed}; the diff requires {depth}: " + "; ".join(reasons))

    # 3. Stages.
    stages = required_stages(route, depth)
    v.facts["required_stages"] = stages
    for s in stages:
        p = run_dir / s
        if not p.is_file() or not p.read_text(encoding="utf-8", errors="replace").strip():
            v.add("stage-missing", "low", f"required stage file {s} is missing or empty")

    # 4. Provenance and attestation of the checkers.
    builder_lines: list[str] = []
    for name in ("02-build.md", "01-plan.md"):
        p = run_dir / name
        if p.is_file():
            builder_lines += _long_lines(p.read_text(encoding="utf-8", errors="replace"))
    checker_stages = CHECKER_STAGES_FULL if depth == "full" else CHECKER_STAGES_LIGHT
    for s in checker_stages:
        p = run_dir / s
        if not p.is_file():
            continue
        writers = writers_of(everything, p)
        if not writers:
            v.add("provenance-missing", "medium",
                  f"{s}: no recorded tool call wrote this file; it was written outside the run's sessions")
            continue
        root_writers = [c for t, c in writers if not t.is_subagent]
        if root_writers:
            v.add("self-graded", "medium",
                  f"{s}: written by the root session ({len(root_writers)} call(s)); a checker is a fresh subagent")
        for t, _c in writers:
            if not t.is_subagent:
                continue
            if t.agent_id not in linked:
                v.add("checker-unlinked", "medium",
                      f"{s}: checker {t.agent_id} was not launched by the root session's Agent tool")
            _check_brief(v, s, t, builder_lines)
        _check_attest(v, run_dir, s, writers, run_calls, now_fp)
    history = _attest_history(run_dir, lenient_calls)
    # Every checker stage that exists counts here, whatever the depth: a review
    # written at light depth is optional, but its BLOCKING findings are not.
    stage_last, stage_last_writer = _check_checker_sequence(v, run_dir, everything, CHECKER_STAGES_FULL, history)

    # 5. The verification plan and its executions.
    try:
        criteria, plan_sha, plan_src = load_plan(run_dir)
    except PlanError as e:
        v.add("plan-missing", "low", f"no usable verification plan: {e}")
        criteria, plan_sha, plan_src = [], "", None
    # A checker may challenge the plan; it may not rewrite it.  A subagent that
    # wrote a checker stage AND the plan file has graded its own criteria.
    if plan_src is not None:
        checker_ids = {t.agent_id for s in checker_stages for t, _c in writers_of(everything, run_dir / s) if t.is_subagent}
        for t, c in writers_of(everything, plan_src):
            if t.is_subagent and t.agent_id in checker_ids:
                v.add("plan-edited-by-checker", "medium",
                      f"{plan_src.name} was edited by checker {t.agent_id}; a checker challenges criteria in its "
                      f"report, the builder or planner changes the plan")
    v.facts["plan_file"] = plan_src.name if plan_src else None
    edir = run_dir / EVIDENCE_DIR
    # The interpreter the checks run with is outside the tree.  Its startup
    # state is pinned at the start and recorded with every piece of evidence;
    # a write by any run session into its site directories is a finding.
    site_pinned = fields.get("site")
    site_now = site_fingerprint()
    v.facts["site_sha"] = site_now
    for d in interpreter_site_dirs():
        for t, c in writes_under(everything, d):
            v.add("interpreter-written", "low",
                  f"{c.file_path} was written by {'subagent ' + t.agent_id if t.is_subagent else 'the root session'}; "
                  f"the interpreter's site directories are outside the tree and off limits to a run")
    for t, c in writes_under(everything, edir):
        v.add("evidence-forged", "low",
              f"{c.file_path} was written with the {c.name} tool by {'subagent ' + t.agent_id if t.is_subagent else 'the root session'}; "
              f"evidence is written only by the pipeline programs")
    for t, c in shell_writes_under(everything, edir):
        v.add("evidence-forged", "low",
              f"a shell command touched the evidence folder outside the pipeline programs: {c.command[:120]!r}")
    # Decisions (who allowed a check to run) are written only by the pipeline
    # programs, like evidence.
    ddir = run_dir / DECISIONS_DIR
    for t, c in writes_under(everything, ddir):
        v.add("decision-forged", "low",
              f"{c.file_path} was written with the {c.name} tool by {'subagent ' + t.agent_id if t.is_subagent else 'the root session'}; "
              f"decisions are written only by the pipeline programs")
    for t, c in shell_writes_under(everything, ddir):
        v.add("decision-forged", "low",
              f"a shell command touched the decisions folder outside the pipeline programs: {c.command[:120]!r}")
    # When each piece of evidence the grade relies on was produced.
    evidence_times: list[datetime] = []
    # What the register (pipeline.findings) may cite: criteria whose current
    # evidence passed, was recorded, and ran on this tree with this plan.
    passed_now: set[str] = set()
    stale: dict[str, str] = {}
    for crit in criteria:
        entry = {"id": crit.id, "required": crit.required, "executable": crit.executable, "status": "unknown"}
        v.criteria.append(entry)
        if crit.waiver:
            entry["status"] = "waived"
            entry["reason"] = crit.reason
            continue
        if not crit.executable:
            entry["status"] = "unexecuted"
            entry["reason"] = crit.reason
            v.add("unexecuted", "medium" if crit.required else "info",
                  f"criterion {crit.id} cannot run here: {crit.reason}")
            continue
        path = edir / f"{crit.id}.json"
        if not path.is_file():
            entry["status"] = "never-ran"
            v.add("never-ran", "low" if crit.required else "info", f"criterion {crit.id} has no evidence; it never ran")
            continue
        try:
            rec = json.loads(path.read_text(encoding="utf-8"))
        except ValueError:
            entry["status"] = "unreadable"
            v.add("evidence-unreadable", "low", f"evidence for {crit.id} is not valid JSON")
            continue
        sha = evidence_sha(path)
        needle = f"{EVIDENCE_PREFIX} id={crit.id} sha={sha}"
        # Only a run_check invocation can produce evidence; a command that
        # merely prints the line (echo after hashing a hand-written file) cannot.
        producers = [(t, c) for t, c in shell_calls if needle in c.output and is_run_check_call(c.command)]
        stamps = [ts for ts in (_parse_ts(c.timestamp) for _t, c in producers) if ts is not None]
        if stamps:
            evidence_times.append(max(stamps))
        entry["evidence_sha"] = sha
        if not producers:
            entry["status"] = "unrecorded"
            v.add("evidence-unrecorded", "low",
                  f"no recorded command produced evidence {crit.id} with this content; the file was forged or edited")
            continue
        if all(not t.is_subagent for t, _ in producers):
            v.add("evidence-not-independent", "medium",
                  f"criterion {crit.id} was run by the root session, not by an independent checker")
        if rec.get("plan_sha") != plan_sha:
            v.add("plan-changed", "medium", f"the verification plan changed after {crit.id} ran; run it again")
        if rec.get("fingerprint") != now_fp:
            v.add("stale-tree", "medium",
                  f"criterion {crit.id} ran against an earlier tree ({str(rec.get('fingerprint'))[:12]}); "
                  f"the tree now is {now_fp[:12]}; run it again on the final code")
        if not rec.get("tree_stable", True):
            v.add("tree-unstable", "medium", f"criterion {crit.id} changed the working tree while it ran")
        _check_site(v, crit.id, rec, site_pinned, site_now)
        _check_impl(v, crit.id, producers)
        _check_decision(v, run_dir, run, crit.id, rec, sha, shell_calls)
        if rec.get("passed"):
            entry["status"] = "passed"
            if rec.get("fingerprint") != now_fp:
                stale[crit.id] = f"its evidence ran on an earlier tree ({str(rec.get('fingerprint'))[:12]}), not the final one"
            elif rec.get("plan_sha") != plan_sha:
                stale[crit.id] = "the plan changed after its evidence was produced"
            elif not rec.get("tree_stable", True):
                stale[crit.id] = "it changed the working tree while it ran"
            else:
                passed_now.add(crit.id)
        else:
            entry["status"] = "failed"
            entry["exit_code"] = rec.get("exit_code")
            v.add("check-failed", "low" if crit.required else "info",
                  f"criterion {crit.id} failed (exit {rec.get('exit_code')}"
                  + (", expect did not match" if rec.get("expect_matched") is False else "")
                  + (", timed out" if rec.get("timed_out") else "") + ")")

    _check_suite(v, run, criteria)
    _check_baseline(v, run_dir, run, repo, route, criteria, plan_sha, shell_calls, evidence_times)
    for c in criteria:
        bp = edir / f"{c.id}.baseline.json"
        if c.detects_change and bp.is_file():
            try:
                brec = json.loads(bp.read_text(encoding="utf-8"))
            except ValueError:
                continue
            _check_decision(v, run_dir, run, c.id, brec, evidence_sha(bp), shell_calls, baseline=True)
    _check_commits(v, run_dir, run, repo)

    # 5b. The register: planned scenarios and suspected defects, each settled
    # by evidence or disclosed (pipeline.findings).
    view = findings_mod.EvidenceView(
        criteria={c.id: c for c in criteria}, passed_now=passed_now,
        red=_red_runs(run_dir, criteria, now_fp, shell_calls),
        baseline_ok=set(v.facts.get("baseline_demonstrated") or []), stale=stale)
    outcomes = _check_register(v, run_dir, plan_src, stage_last, stage_last_writer, history, view, everything,
                               route=route)
    v.facts["register"] = [asdict(o) for o in outcomes]
    v.facts["verification"] = {
        "criteria_passed": sum(1 for c in v.criteria if c["status"] == "passed"),
        "criteria_total": len(v.criteria),
        "on_final_tree": sorted(passed_now),
    }
    v.facts["outcome"] = {k: sorted(o.id for o in outcomes if o.state == k)
                          for k in ("verified", "fixed", "not-a-defect", "unresolved", "open", "invalid")}
    # The audit is the last judgment, so it must postdate the evidence it
    # judged.  Together with the fingerprint check (the tree has not changed
    # since that evidence) this binds the audit to the final code by content:
    # a file touched but not changed does not make it stale, a change does.
    audit_ts = stage_last.get("08-audit.md")
    if audit_ts is not None and evidence_times and \
            (max(evidence_times) - audit_ts).total_seconds() > STALE_TOLERANCE_S:
        v.add("checker-stale", "medium",
              "08-audit.md was written before the latest evidence it relies on; the audit judged an earlier "
              "version and must run again")

    # 6. The fourth question and coverage.
    audit = run_dir / "08-audit.md"
    if audit.is_file():
        text = audit.read_text(encoding="utf-8", errors="replace")
        m = FOURTH_QUESTION_RE.search(text)
        answer = text[m.end():] if m else ""
        answer = re.sub(r"^[^\n]*\n", "", answer, count=1) if m else ""
        if not m or len(re.sub(r"\s+", " ", answer).strip()) < 40:
            v.add("fourth-question", "medium",
                  "08-audit.md does not answer 'what could still be wrong that no check would catch?'")
        _check_coverage(v, text, criteria, [o.id for o in outcomes] + list(v.facts.get("audit_must_name") or []))

    v.facts["graded_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
    try:
        # Timing only: any recorded call that ran pipeline.start and printed
        # this run's line marks the start (a chained prefix does not matter
        # here, unlike for the start line's fields).
        start_call = next((c for c in root.calls_named("Bash", "PowerShell")
                           if "pipeline.start" in c.command.replace("/", ".")
                           and f"PIPELINE-RUN nonce={nonce}" in c.output), None)
        writers: dict[str, list[str]] = {}
        for t in subs:
            for c in t.calls_named("Write", "Edit", "MultiEdit"):
                m = metrics_mod.STAGE_FILE_RE.search(c.file_path.replace("\\", "/"))
                if m and same_file(Path(c.file_path).parent, run_dir):
                    writers.setdefault(t.agent_id, [])
                    if m.group(1) not in writers[t.agent_id]:
                        writers[t.agent_id].append(m.group(1))
        # The run ends at its last recorded step: every pipeline command and
        # every stage write names the run directory.  A session that went on
        # to other work afterwards does not stretch the run.
        # The ledger row is the documented last step; a later re-grade in the
        # same session is not part of the run's time.
        steps = [c.timestamp for t in everything for c in t.calls
                 if c.timestamp and (_names_run(c.command, run_dir)
                                     or (c.file_path and same_file(Path(c.file_path).parent, run_dir)))]
        ledger_steps = [c.timestamp for c in root.calls_named("Bash", "PowerShell")
                        if c.timestamp and is_pipeline_call(c.command, "ledger") and _names_run(c.command, run_dir)]
        steps = ledger_steps[:1] or steps
        v.metrics = metrics_mod.collect(root, subs, start_at=start_call.timestamp if start_call else "",
                                        end_at=max(steps, key=lambda s: _parse_ts(s) or datetime.min.replace(
                                            tzinfo=timezone.utc)) if steps else "",
                                        register=v.facts.get("register"), prices=prices, writers=writers,
                                        belongs=lambda cmd: not _names_other_run(cmd, run_dir))
    except Exception as e:  # noqa: BLE001 - instrumentation must never change or block a grade
        v.facts["metrics_error"] = f"{type(e).__name__}: {e}"
    return v.finish()


# ── the start line: the run's fixed point ────────────────────────────────────

def _start_line(root: Transcript, nonce: str) -> str:
    """The ``PIPELINE-RUN`` line the engine recorded for this run."""
    marker = f"PIPELINE-RUN nonce={nonce}"
    for c in root.calls_named("Bash", "PowerShell"):
        if not is_pipeline_call(c.command, "start"):
            continue
        for line in c.output.splitlines():
            if marker in line:
                return line
    return ""


def _check_dir(v: Verdict, run_dir: Path, fields: dict) -> None:
    """The start line belongs to this run directory.  A session can start a
    second run (another route, a snapshot that already holds the change) and
    copy its nonce and snapshot into the first run's ``run.json``; every other
    field would then agree with the engine's record.  The ``dir=`` field is
    what ties the line to one run."""
    recorded = fields.get("dir")
    if not recorded:
        return  # a run started by an older pipeline
    if not same_file(recorded, run_dir):
        v.add("run-tampered", "low",
              f"the engine recorded this run's start line for {recorded}, not for {run_dir}; a start line "
              f"belongs to the run it created")


def _check_base(v: Verdict, run: dict, fields: dict) -> None:
    """The base revision (what the diff is measured against) and the baseline
    snapshot (what the regression demonstration runs on) are the ones the
    engine recorded; ``run.json`` is overwritten with them when it differs,
    so a rewritten base cannot shrink the diff or move the baseline."""
    for key, field_name in (("base", "base_rev"), ("snapshot", "base_snapshot")):
        recorded = fields.get(key)
        if recorded is None:
            continue
        claimed = str(run.get(field_name) or "none")
        if recorded != claimed:
            v.add("base-tampered", "low",
                  f"run.json says {field_name}={claimed[:12]}, but the engine recorded {key}={recorded[:12]} at the start")
            run[field_name] = None if recorded == "none" else recorded


def _start_fields(line: str) -> dict:
    """``key=value`` pairs of the start line; ``dir`` is everything after ``dir=``."""
    out: dict = {}
    for key in ("nonce", "route", "request", "pinned", "protected", "base", "snapshot", "site"):
        m = re.search(rf"\b{key}=(\S+)", line)
        if m:
            out[key] = m.group(1)
    m = re.search(r"\bdir=(.*)$", line)
    if m:
        out["dir"] = m.group(1).strip()
    return out


def _check_route(v: Verdict, run: dict, fields: dict) -> str:
    """The route the engine recorded wins over the one in ``run.json``."""
    claimed = str(run.get("route") or "a")
    recorded = fields.get("route")
    if recorded is None:
        return claimed  # a run started by an older pipeline: no recorded route
    if recorded not in ROUTE_ENTRY:
        v.add("route-tampered", "low", f"the engine recorded route={recorded}, which is not a route")
        return claimed
    if recorded != claimed:
        v.add("route-tampered", "low",
              f"run.json says route {claimed}, but the engine recorded route={recorded} at the start; "
              f"the run is graded on route {recorded}")
    return recorded


def _check_request(v: Verdict, run_dir: Path, run: dict, fields: dict) -> None:
    """``00-request.md`` still holds the request whose hash the engine recorded."""
    recorded = fields.get("request") or run.get("request_sha")
    if not recorded:
        return  # a run started by an older pipeline
    p = run_dir / "00-request.md"
    if not p.is_file():
        return  # stage-missing reports it
    text = request_text_of(p.read_text(encoding="utf-8", errors="replace"))
    if text is None:
        v.add("request-tampered", "low", "00-request.md no longer carries the verbatim request under its marker")
        return
    if request_sha(text) != recorded:
        v.add("request-tampered", "low",
              "the request in 00-request.md is not the one the engine recorded at the start; nothing may be "
              "appended to or changed in the verbatim request (a light run's plan goes in 00-plan.md)")


def _check_pinned(v: Verdict, run_dir: Path, run: dict, repo: Path, fields: dict) -> None:
    """The pinned ``.pipeline-verify`` copy is the one the start line recorded,
    and the repository's declaration was not changed during the run."""
    recorded = fields.get("pinned")
    sha = run.get("pinned_sha")
    copy = run_dir / PINNED_FILE
    copy_sha = block_sha(copy.read_text(encoding="utf-8", errors="replace")) if copy.is_file() else None
    v.facts["pinned_sha"] = sha
    if recorded is not None and recorded != (sha or "none"):
        v.add("pinned-tampered", "low",
              f"run.json pins {sha or 'nothing'}, but the engine recorded pinned={recorded} at the start")
    if (sha or None) != copy_sha:
        v.add("pinned-tampered", "low",
              f"{PINNED_FILE} in the run is not the declaration pinned at the start; the pinned checks were altered")
    now = repo / VERIFY_FILE
    now_sha = block_sha(now.read_text(encoding="utf-8", errors="replace")) if now.is_file() else None
    if now_sha != sha:
        v.add("verify-declaration-changed", "medium",
              f"{VERIFY_FILE} changed during this run; the declaration pinned at the start was enforced, and "
              f"the change needs a person's review before a later run can rely on it")


def _check_protected(v: Verdict, run_dir: Path, run: dict, repo: Path, fields: dict) -> list[str]:
    """The pinned ``.pipeline-protected`` copy is the one the start line
    recorded; returns its patterns (enforced whatever the file says now)."""
    recorded = fields.get("protected")
    sha = run.get("protected_sha")
    copy = run_dir / PINNED_PROTECTED_FILE
    copy_text = copy.read_text(encoding="utf-8", errors="replace") if copy.is_file() else None
    copy_sha = block_sha(copy_text) if copy_text is not None else None
    v.facts["protected_sha"] = sha
    if recorded is not None and recorded != (sha or "none"):
        v.add("protected-tampered", "low",
              f"run.json pins protection {sha or 'nothing'}, but the engine recorded protected={recorded} at the start")
        # Grade on what the engine recorded: a missing or altered copy is worthless.
    if (sha or None) != copy_sha:
        v.add("protected-tampered", "low",
              f"{PINNED_PROTECTED_FILE} in the run is not the protection pinned at the start")
        copy_text = None
    now = repo / PROTECTED_FILE
    now_sha = block_sha(now.read_text(encoding="utf-8", errors="replace")) if now.is_file() else None
    if now_sha != sha:
        v.add("protected-declaration-changed", "medium",
              f"{PROTECTED_FILE} changed during this run; the protection pinned at the start was enforced, and "
              f"the change needs a person's review")
    return depth_mod.parse_patterns(copy_text) if copy_text else []


def _check_impl(v: Verdict, cid: str, producers) -> None:
    """The ``run_check`` that produced the evidence is this pipeline: the
    ``impl=`` it printed (recorded by the engine) is the identity of the
    package grading it now.  A same-named project package standing in for it,
    or an older copy, prints another identity or none at all (external review,
    finding 7).  *cid* is the label in the line (``<id>`` or ``<id>@baseline``);
    *producers* are the (transcript, call) pairs that printed it."""
    mine = impl_identity()[0][:12]
    seen = set()
    unnamed = False
    for _t, c in producers:
        for line in c.output.splitlines():
            if line.startswith(EVIDENCE_PREFIX) and f" id={cid} " in line:
                m = re.search(r"\bimpl=(\S+)", line)
                if m:
                    seen.add(m.group(1))
                else:
                    unnamed = True
    if unnamed and not seen:
        # Every run_check of this pipeline prints impl=; a line without it
        # came from something else, so the identity check cannot be skipped.
        v.add("pipeline-impl-unknown", "medium",
              f"criterion {cid} was produced by a run_check that names no pipeline implementation; run it again "
              f"with this pipeline")
    elif seen and mine not in seen:
        v.add("pipeline-impl-mismatch", "medium",
              f"criterion {cid} was produced by pipeline implementation {', '.join(sorted(seen))}, not {mine} (the "
              f"one grading it); run it again with this pipeline")


def _check_site(v: Verdict, cid: str, rec: dict, pinned: str | None, now: str) -> None:
    """The evidence was produced by the interpreter in the state pinned at the
    start, and that state is unchanged now."""
    recorded = rec.get("site_sha")
    if recorded is None:
        return  # evidence from an older run_check
    if pinned is not None and pinned != "none" and recorded != pinned:
        v.add("interpreter-changed", "medium",
              f"criterion {cid} ran with an interpreter whose startup files (.pth, sitecustomize) or PYTHONPATH "
              f"differ from what was pinned at the start; something outside the tree steered the checks")
    elif recorded != now:
        v.add("interpreter-changed", "medium",
              f"the interpreter's startup files changed after {cid} ran; run it again")


# What the shell tool reports when a command outruns its foreground limit.
# Anchored at the start of the result: a run_check prints its evidence line
# first, so text the checked command printed can never start the result, and
# a criterion that prints this sentence cannot point the harness at a file it
# wrote (Review, loop 5).  The file must be the engine's ``tasks/<ID>.output``.
_BACKGROUND_RE = re.compile(r"\A\s*Command did not complete within its \d+s timeout and was moved to the "
                            r"background \(ID: (\w+)\)\.\s*Output is being written to: (\S+?\.output)")


def _attach_background_output(v: Verdict, shell_calls) -> None:
    """A ``run_check`` that outran the shell tool's foreground limit (the
    full suite on a busy machine takes longer than 10 minutes) finishes in the
    background, and the engine writes its output to a file it names in the
    recorded result.  That file is the command's output, written by the
    engine, so its text is attached to the call.  Any recorded tool call that
    wrote to it (a file tool, or a shell command naming it with a write) makes
    it worthless: evidence-forged.  Found by the Test Team of the
    evidence-gaps run, whose suite evidence was unrecorded twice."""
    for t, c in shell_calls:
        if not is_pipeline_call(c.command, "run_check"):
            continue
        m = _BACKGROUND_RE.match(c.result_text or "")
        if not m:
            continue
        out_path = Path(m.group(2).replace("\\", "/"))
        if out_path.name != f"{m.group(1)}.output" or out_path.parent.name != "tasks" or EVIDENCE_PREFIX in c.output:
            continue  # not the engine's file for this task, or the call already finished in the foreground
        transcripts = list({id(t2): t2 for t2, _ in shell_calls}.values())
        tampered = [x for x in writes_under(transcripts, out_path.parent)
                    if same_file(x[1].file_path, out_path)]
        tail = out_path.name.lower()
        for _t2, c2 in shell_calls:
            cmd = strip_data_heredocs(c2.command).replace("\\", "/").lower()
            if c2 is not c and tail in cmd and _SHELL_WRITE_RE.search(cmd):
                tampered.append((_t2, c2))
        if tampered:
            v.add("evidence-forged", "low",
                  f"the background output of a run_check ({out_path.name}) was written by a recorded tool call; "
                  f"it is not the command's output")
            continue
        try:
            text = out_path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue  # gone (temp cleaned): the evidence stays unrecorded
        c.stdout = (c.stdout + "\n" if c.stdout else "") + text


def _names_run(command: str, run_dir: Path) -> bool:
    """True when a recorded command names this run's directory: its name as a
    whole path segment, in any case, with either slash, quoted or not.  The
    name must end at a separator, so ``...-gaps`` is not found inside
    ``...-gaps-2/`` (a run whose name extends this one's)."""
    cmd = command.replace("\\", "/").lower()
    name = re.escape(run_dir.name.lower())
    return re.search(rf"(?:^|[/\s\"'=]){name}(?=$|[/\s\"';&|)])", cmd) is not None


# A run directory's name as pipeline.start makes it (date, time, slug).
_RUN_NAME_RE = re.compile(r"(?<![\w.-])\d{4}-\d{2}-\d{2}-\d{4}-[\w.-]+")


def _names_other_run(command: str, run_dir: Path) -> bool:
    """True when a recorded command names some run directory and not this one.

    Escalations are filtered with this, not ``_names_run``: an escalation only
    ever demands more review, so one whose target cannot be read from the
    command (``escalate . "why"`` after a ``cd``, a shell variable) still
    counts.  Only an escalation that plainly names another run is left out."""
    if _names_run(command, run_dir):
        return False
    cmd = command.replace("\\", "/").lower()
    return bool(_RUN_NAME_RE.search(cmd) or re.search(rf"{re.escape(RUNS_SUBDIR.as_posix())}/[^/\s\"']", cmd))


def _escalations(shell_calls) -> list[str]:
    """Reasons from every ``pipeline.escalate`` call recorded in the run."""
    out: list[str] = []
    for _t, c in shell_calls:
        if not is_pipeline_call(c.command, "escalate"):
            continue
        for line in c.output.splitlines():
            m = re.search(rf"{ESCALATE_PREFIX} depth=full reason=(.*)$", line)
            if m and m.group(1).strip() not in out:
                out.append(m.group(1).strip())
    return out


# ── attestation ──────────────────────────────────────────────────────────────

def _attest_calls(shell_calls, stage: str):
    """Recorded ``pipeline.attest`` calls whose output attests *stage*."""
    return [(t, c) for t, c in shell_calls
            if f"{ATTEST_PREFIX} stage={stage} " in c.output and is_pipeline_call(c.command, "attest")]


def _check_attest(v: Verdict, run_dir: Path, stage: str, writers, shell_calls, now_fp: str) -> None:
    """The checker that wrote *stage* attested its current content on the
    current tree."""
    sha = _file_sha(run_dir / stage)
    needle = f"{ATTEST_PREFIX} stage={stage} sha={sha}"
    all_calls = _attest_calls(shell_calls, stage)
    producers = [(t, c) for t, c in all_calls if needle in c.output]
    if not producers:
        if all_calls:
            v.add("stage-tampered", "medium",
                  f"{stage} changed after its checker attested it; the report on disk is not the report the "
                  f"checker signed (the checker attests again after every change it makes)")
        else:
            v.add("stage-unattested", "medium",
                  f"{stage} was never attested; the checker runs 'python -m pipeline.attest <run-dir> {stage}' "
                  f"after writing its report")
        return
    if all(not t.is_subagent for t, _c in producers):
        v.add("self-graded", "medium", f"{stage}: attested by the root session; a checker attests its own report")
        return
    writer_ids = {t.agent_id for t, _c in writers if t.is_subagent}
    attesters = {t.agent_id for t, _c in producers if t.is_subagent}
    if not (writer_ids & attesters):
        v.add("stage-attested-by-other", "medium",
              f"{stage} was attested by a subagent that did not write it; the checker that wrote the report attests it")
    fps = set()
    for _t, c in producers:
        for line in c.output.splitlines():
            m = re.search(rf"{re.escape(needle)} fingerprint=(\S+)", line)
            if m:
                fps.add(m.group(1))
    if now_fp not in fps:
        seen = ", ".join(sorted(f[:12] for f in fps)) or "none"
        v.add("checker-stale", "medium",
              f"{stage} was attested on tree {seen}; the tree now is {now_fp[:12]}; the checker must examine the "
              f"final code and attest again")


ATTEST_LINE_RE = re.compile(rf"{ATTEST_PREFIX} stage=(\S+) sha=(\S+) fingerprint=(\S+)(?: blocking=(\S+))?")


def _attest_history(run_dir: Path, shell_calls) -> dict[str, list[dict]]:
    """Every attestation the engine recorded, per stage, oldest first.

    The records come from the printed lines in the checkers' transcripts, so
    deleting or editing ``evidence/<stage>.attest.json`` loses nothing; a
    file record is merged in only when a recorded line carries its sha."""
    out: dict[str, list[dict]] = {}
    seen: dict[str, set[str]] = {}
    on_disk: dict[str, set] = {}
    for t, c in shell_calls:
        if not is_pipeline_call(c.command, "attest"):
            continue
        named = _names_run(c.command, run_dir)
        for m in ATTEST_LINE_RE.finditer(c.output):
            stage, sha, fp, ids = m.group(1), m.group(2), m.group(3), m.group(4) or ""
            if not named:
                # A command that does not spell out its run (``.``, ``$RUN``)
                # counts only if pipeline.attest wrote that signature into THIS
                # run's attest file: another run's sign-off went to that run's
                # file (Review, loop 4).  Deleting the file to hide one is
                # itself an evidence-forged finding.
                if stage not in on_disk:
                    on_disk[stage] = {r.get("sha") for r in load_attestations(run_dir, stage)}
                if sha not in on_disk[stage]:
                    continue
            out.setdefault(stage, []).append(
                {"stage": stage, "sha": sha, "fingerprint": fp, "blocking": [i for i in ids.split(",") if i],
                 "agent": t.agent_id if t.is_subagent else "root"})
            seen.setdefault(stage, set()).add(sha)
    for stage, shas in seen.items():
        out[stage] += [r for r in load_attestations(run_dir, stage) if r.get("sha") in shas]
    return out


# ── the rest of the checks ───────────────────────────────────────────────────

def _red_runs(run_dir: Path, criteria, now_fp: str, shell_calls) -> dict[str, list[dict]]:
    """Recorded FAILING executions of each criterion on an earlier tree: the
    "before" half of a FIXED disposition.  A history file counts only when a
    recorded run_check call printed its sha (so a hand-written failure is not
    one), it ran the command the plan has now, exited non-zero without timing
    out, and ran on a tree other than the final one."""
    out: dict[str, list[dict]] = {}
    for c in criteria:
        if not c.covers_ids():
            continue
        for path, rec in history_records(run_dir, c.id):
            needle = f"{EVIDENCE_PREFIX} id={c.id} sha={evidence_sha(path)}"
            if not any(needle in call.output and is_run_check_call(call.command) for _t, call in shell_calls):
                continue
            if rec.get("passed") or rec.get("timed_out") or rec.get("exit_code") in (0, None):
                continue
            if rec.get("cmd") != c.cmd or rec.get("fingerprint") == now_fp:
                continue
            out.setdefault(c.id, []).append({"fingerprint": rec.get("fingerprint"), "started": rec.get("started"),
                                             "exit_code": rec.get("exit_code")})
    return out


def _check_coverage(v: Verdict, audit_text: str, criteria, register_ids=()) -> None:
    """Require the audit to judge whether the checks prove the request.

    The section is the text under a heading containing "checks prove the
    request", up to the next heading.  It must name every required criterion
    by id (the audit went criterion by criterion, not one blanket sentence),
    every id in the register (each planned scenario and each finding, so the
    audit has to say whether the check cited for it really exercises it), and
    end in ``Verdict: covered`` or ``Verdict: not covered``.  Anything short of
    an explicit "covered" caps the grade at MEDIUM."""
    m = COVERAGE_RE.search(audit_text)
    if not m:
        v.add("coverage", "medium",
              "08-audit.md does not judge whether the checks prove the request "
              "(a heading 'Do the checks prove the request?' with a Verdict line)")
        return
    rest = audit_text[m.end():]
    nxt = re.search(r"^#", rest, re.MULTILINE)
    section = rest[:nxt.start()] if nxt else rest
    missing = [c.id for c in criteria if c.required
               and not re.search(rf"(?<![\w-]){re.escape(c.id)}(?![\w-])", section)]
    if missing:
        v.add("coverage", "medium",
              f"the coverage section does not account for criterion {', '.join(missing)}")
    unnamed = [i for i in register_ids if not re.search(rf"(?<![\w-]){re.escape(i)}(?![\w-])", section)]
    if unnamed:
        v.add("coverage", "medium",
              f"the coverage section does not account for scenario or finding {', '.join(sorted(unnamed))}; the "
              f"audit judges whether the evidence cited for each really exercises it")
    # Exactly one verdict, and a valid one: the first line no longer decides
    # when a later one in the same section says the opposite.
    lines = VERDICT_LINE_RE.findall(section)
    valid = [m.group(1) for m in COVERAGE_VERDICT_RE.finditer(section)]
    if len(lines) > len(valid):
        v.add("coverage", "medium",
              "the coverage section has a Verdict line that is neither 'covered' nor 'not covered'")
    if len(valid) > 1:
        v.add("coverage", "medium",
              f"the coverage section has {len(valid)} Verdict lines; it must reach exactly one")
    verdict = COVERAGE_VERDICT_RE.search(section)
    if not verdict:
        if len(lines) == len(valid):
            v.add("coverage", "medium",
                  "the coverage section has no 'Verdict: covered' or 'Verdict: not covered' line")
    elif any(not re.fullmatch(r"covered", x, re.IGNORECASE) for x in valid):
        v.add("coverage", "medium",
              "the audit judged that the checks do not prove the request was met")


def _check_decision(v: Verdict, run_dir: Path, run: dict, cid: str, rec: dict, sha: str, shell_calls,
                    baseline: bool = False) -> None:
    """Evidence counts only when a decision allowed exactly this execution:
    the decision exists, was issued by a recorded ``run_check authorize`` in
    this run's sessions, binds this criterion (and whether it is the baseline
    run), the plan and the fingerprint the evidence records, is allowed (by
    the policy, or by the user's answer on the card), and was used for this
    evidence file.  Anything less is ``unauthorized``: a check that ran
    without an approval is graded LOW, however it went."""
    label = f"{cid}@baseline" if baseline else cid
    nonce = str(rec.get("decision") or "")
    if not nonce:
        v.add("unauthorized", "low", f"criterion {label} ran without a decision (the old one-step run_check)")
        return
    try:
        d = load_decision(run_dir, nonce)
    except DecisionError as e:
        v.add("unauthorized", "low", f"criterion {label}: {e}")
        return
    # Only the authorize step issues a decision; execute prints the line too,
    # but a decision an agent wrote by hand and then executed is not issued.
    issued = f"{DECISION_PREFIX} id={nonce} "
    if not any(issued in c.output and is_pipeline_call(c.command, "run_check")
               and re.search(r"run_check\s+authorize\b", c.command) for _t, c in shell_calls):
        v.add("unauthorized", "low", f"criterion {label}: no recorded run_check authorize issued decision {nonce}")
        return
    op = d.get("operation") or {}
    if d.get("kind") != "check" or op.get("criterion") != cid or bool(op.get("baseline")) != baseline:
        v.add("unauthorized", "low", f"criterion {label}: decision {nonce} is for a different operation")
        return
    if op.get("plan_sha") != rec.get("plan_sha"):
        v.add("unauthorized", "low", f"criterion {label}: decision {nonce} approved a different plan")
        return
    fp = rec.get("snapshot") if baseline else rec.get("fingerprint")
    if op.get("fingerprint") != fp:
        v.add("unauthorized", "low", f"criterion {label}: decision {nonce} approved a different tree")
        return
    if d.get("verdict") != "allow":
        ans, detail = decision_answer(run_dir, d, run=run)
        if ans != "allow":
            v.add("unauthorized", "low", f"criterion {label}: decision {nonce} was not allowed ({detail})")
            return
    if not d.get("used") or d.get("evidence_sha") != sha:
        v.add("unauthorized", "low", f"criterion {label}: decision {nonce} was not used for this evidence")


def _check_commits(v: Verdict, run_dir: Path, run: dict, repo: Path) -> None:
    """A commit made in the run's repository before a passed delivery is
    reported, not prevented: Code Pipeline does not sit in VibeNode's
    permission path.  The ledger carries the same flag."""
    base = str(run.get("base_rev") or "")
    if base in ("", "no-git", "no-commits") or not _is_git_repo(repo):
        return
    out = [t for t in (_parse_ts(x) for x in git(repo, "log", "--format=%cI", f"{base}..HEAD", check=False).split())
           if t is not None]
    if not out:
        return
    first_commit = min(out)
    dp = run_dir / "delivered.json"
    delivered_at = None
    if dp.is_file():
        try:
            drec = json.loads(dp.read_text(encoding="utf-8"))
            if drec.get("delivered"):
                delivered_at = _parse_ts(str(drec.get("at") or ""))
        except ValueError:
            pass
    if delivered_at is None or first_commit < delivered_at:
        v.add("committed-before-delivery", "medium",
              f"{len(out)} commit(s) were made in this repository since the run started, the first at "
              f"{first_commit.isoformat(timespec='seconds')}, before any passed delivery; the delivery gate runs "
              f"before a commit")


def _parse_ts(ts: str) -> datetime | None:
    try:
        d = datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        return None
    return d if d.tzinfo else d.replace(tzinfo=timezone.utc)


def _check_suite(v: Verdict, run: dict, criteria) -> None:
    """The project's full suite is part of the evidence: pinned from the
    repository, or, for a project with no declaration yet, a plan criterion
    marked ``suite: true``."""
    if any(c.pinned for c in criteria):
        return
    planned = [c for c in criteria if c.suite and c.required and c.executable]
    if not planned:
        v.add("no-suite", "medium",
              f"no full test suite in the evidence: the repository has no {VERIFY_FILE} and the plan marks no "
              f"required criterion 'suite: true'")
    else:
        v.add("suite-unpinned", "info",
              f"the repository declares no {VERIFY_FILE}; this run's suite ({planned[0].id}) came from the plan. "
              f"Add the declaration so later runs cannot change it")


def _check_baseline(v: Verdict, run_dir: Path, run: dict, repo: Path, route: str, criteria, plan_sha: str,
                    shell_calls, evidence_times: list) -> None:
    """At least one required criterion demonstrates that the tests detect the
    change, unless the route is cleanup or the plan states a waiver."""
    if route not in BASELINE_ROUTES:
        return
    demos = [c for c in criteria if c.detects_change and c.required]
    waivers = [c for c in criteria if c.waiver]
    held = []
    for c in demos:
        path = run_dir / EVIDENCE_DIR / f"{c.id}.baseline.json"
        if not path.is_file():
            v.add("baseline-missing", "medium",
                  f"criterion {c.id} is a regression demonstration but never ran on the baseline "
                  f"(python -m pipeline.run_check <run-dir> {c.id} --baseline)")
            continue
        try:
            rec = json.loads(path.read_text(encoding="utf-8"))
        except ValueError:
            v.add("evidence-unreadable", "low", f"baseline evidence for {c.id} is not valid JSON")
            continue
        needle = f"{EVIDENCE_PREFIX} id={baseline_label(c.id)} sha={evidence_sha(path)}"
        made = [call for _t, call in shell_calls if needle in call.output and is_run_check_call(call.command)]
        if not made:
            v.add("evidence-unrecorded", "low",
                  f"no recorded run_check --baseline produced baseline evidence {c.id} with this content")
            continue
        stamps = [ts for ts in (_parse_ts(call.timestamp) for call in made) if ts is not None]
        if stamps:
            evidence_times.append(max(stamps))
        # A stand-in pipeline could write a "demonstrated" record without
        # running anything, so a baseline is held to the same identity check.
        _check_impl(v, baseline_label(c.id), [(None, call) for call in made])
        if rec.get("plan_sha") != plan_sha:
            v.add("plan-changed", "medium", f"the verification plan changed after the baseline of {c.id} ran")
            continue
        if rec.get("snapshot") != run.get("base_snapshot"):
            v.add("baseline-wrong-snapshot", "medium",
                  f"the baseline of {c.id} ran against {str(rec.get('snapshot'))[:12]}, not the run's starting snapshot")
            continue
        changed = [rel for rel, sha in (rec.get("overlay") or {}).items() if _file_sha(repo / rel) != sha]
        if changed:
            v.add("baseline-stale", "medium",
                  f"the tests copied into the baseline of {c.id} changed since it ran ({', '.join(changed)}); "
                  f"run the baseline again")
            continue
        if rec.get("timed_out"):
            # A baseline that prints the expected error and then hangs did not
            # demonstrate anything: run_check recorded it as not demonstrated
            # (found by the external review of 2026-10-27, finding 1).
            v.add("baseline-timed-out", "medium",
                  f"the baseline of {c.id} timed out; a hang is not a demonstration that the tests detect the change")
            continue
        if not rec.get("failed") or rec.get("exit_code") in (0, None):
            # A genuine non-zero exit, not merely a missing success line.
            v.add("baseline-passed", "medium",
                  f"criterion {c.id} passes on the code as it was before the change (exit "
                  f"{rec.get('exit_code')}): it does not test the change")
            continue
        if not rec.get("fails_with_matched"):
            v.add("baseline-unrelated", "medium",
                  f"criterion {c.id} fails on the baseline, but not with {rec.get('fails_with')!r}: the failure is "
                  f"unrelated to the intended behavior")
            continue
        if not rec.get("passed"):
            v.add("baseline-not-demonstrated", "medium",
                  f"run_check recorded the baseline of {c.id} as not demonstrated")
            continue
        held.append(c.id)
    v.facts["baseline_demonstrated"] = held
    if not held and not waivers and not demos:
        v.add("no-change-detection", "medium",
              "no required criterion is a regression demonstration (detects_change), and the plan states no "
              "waiver; nothing shows the tests would notice if the change were missing")


def _stage_writes(everything, path: Path) -> list[tuple[Transcript, datetime | None]]:
    return [(t, _parse_ts(c.timestamp)) for t, c in writers_of(everything, path) if t.is_subagent]


def _check_checker_sequence(v: Verdict, run_dir: Path, everything, checker_stages,
                            history: dict[str, list[dict]] | None = None) -> dict:
    """Review, Test and Audit are distinct subagents, in that order, and no
    BLOCKING finding is left without a RESOLVED from a later checker stage,
    including findings an earlier attested version of a report carried.
    Returns each stage's last write time, for the audit-freshness check."""
    owners: dict[str, set[str]] = {}
    last: dict[str, datetime | None] = {}
    last_writer: dict[str, str] = {}
    for s in checker_stages:
        writes = _stage_writes(everything, run_dir / s)
        for t, _ts in writes:
            owners.setdefault(t.agent_id, set()).add(s)
        stamped = [(ts, t.agent_id) for t, ts in writes if ts is not None]
        last[s] = max(stamped)[0] if stamped else None
        if stamped:
            last_writer[s] = max(stamped)[1]
    for agent, stages in owners.items():
        if len(stages) > 1:
            v.add("checker-reused", "medium",
                  f"checker {agent} wrote {', '.join(sorted(stages))}; each checker stage needs its own fresh subagent")
    present = [s for s in checker_stages if last.get(s) is not None]
    for a, b in zip(present, present[1:]):
        if last[a] > last[b]:
            v.add("stage-order", "medium", f"{a} was last written after {b}; the checkers run in order")
    return last, last_writer


def _check_register(v: Verdict, run_dir: Path, plan_src: Path | None, last: dict, last_writer: dict,
                    history: dict[str, list[dict]], view: findings_mod.EvidenceView,
                    everything, route: str = "a") -> list[findings_mod.Outcome]:
    """Every planned scenario and every suspected defect reaches a disposition
    the evidence supports (``pipeline.findings``).

    A finding is settled only by a LATER checker stage than the one that
    raised it (or, for the last stage, by a fresh checker that signed the
    current version and never signed one carrying the finding).  Findings an
    earlier signed version carried do not vanish with a rewrite.  A scenario
    may be settled by any checker stage, or by a passing check that covers it.
    Anything open, unresolved or settled without valid evidence caps the
    grade at MEDIUM, with the reason named."""
    stages = CHECKER_STAGES_FULL
    order = {s: i for i, s in enumerate(stages)}
    texts = {s: (run_dir / s).read_text(encoding="utf-8", errors="replace")
             for s in stages if (run_dir / s).is_file()}
    disps = {s: findings_mod.dispositions_in(t, s) for s, t in texts.items()}
    entries: dict[str, findings_mod.Entry] = {}
    raises: list = []
    must_name: list[str] = []
    if plan_src is not None and plan_src.is_file():
        plan_text = plan_src.read_text(encoding="utf-8", errors="replace")
        raises += findings_mod.raises_in(plan_text, plan_src.name)
        for e in findings_mod.scenarios_in(plan_text, plan_src.name):
            entries[e.id] = e
        # A plan that names no scenario says so, with a reason the audit
        # judges; silence would look the same as having thought it through.
        reason = findings_mod.no_scenarios_reason(plan_text)
        if route in BASELINE_ROUTES and not entries:
            if reason is None:
                v.add("scenarios-undeclared", "medium",
                      f"{plan_src.name} names no SCENARIO and does not declare 'NO-SCENARIOS: <why none apply>'; "
                      f"a plan that considered none must say so")
            elif len(reason.split()) < findings_mod.NO_SCENARIOS_MIN_WORDS:
                v.add("scenarios-undeclared", "medium",
                      f"{plan_src.name} declares NO-SCENARIOS without a reason (at least "
                      f"{findings_mod.NO_SCENARIOS_MIN_WORDS} words saying why no failure scenario applies)")
            else:
                must_name.append(findings_mod.NO_SCENARIOS_ID)
        # A scenario the plan once named cannot silently leave it: every
        # SCENARIO id a recorded file tool ever wrote into the plan counts.
        for _t, c in writers_of(everything, plan_src):
            written = "\n".join(str(x) for x in (c.input.get("content"), c.input.get("new_string"),
                                                  *[(ed or {}).get("new_string") for ed in (c.input.get("edits") or [])
                                                    if isinstance(ed, dict)]) if x)
            for e in findings_mod.scenarios_in(written, plan_src.name):
                if e.id not in entries:
                    v.add("obligation-dropped", "medium",
                          f"scenario {e.id} was written into {plan_src.name} during the run and is gone now; a "
                          f"planned scenario is settled by a disposition, not removed")
    for s in stages:
        raises += findings_mod.raises_in(texts.get(s, ""), s)
        must_name += findings_mod.free_note_ids(texts.get(s, ""))
        for e in findings_mod.findings_in(texts.get(s, ""), s):
            if e.id not in entries:
                entries[e.id] = e
        for line in findings_mod.unstructured_observations(texts.get(s, "")):
            v.add("observation-unstructured", "medium",
                  f"{s}: {line[:120]!r} labels an observation non-blocking outside the structured forms; record it "
                  f"as FINDING: <id> (a correctness concern) or NOTE: <id> cosmetic: (or enhancement: / "
                  f"preference:) <text>")
    for msg in findings_mod.collisions(raises):
        v.add("finding-id-collision", "medium", msg)
    v.facts["audit_must_name"] = sorted(set(must_name) - set(entries))
    raisers: dict[str, set] = {}
    for s, recs in (history or {}).items():
        if s not in order:
            continue
        for r in recs:
            for fid in r.get("blocking") or []:
                raisers.setdefault(fid, set()).add(r.get("agent"))
                if fid not in entries:
                    entries[fid] = findings_mod.Entry(fid, "finding", s, "(raised in an earlier signed version)",
                                                      dropped=True)

    def eligible(e: findings_mod.Entry) -> list:
        out = []
        for s2 in stages:
            for d in disps.get(s2, []):
                if d.id != e.id:
                    continue
                if e.kind == "scenario":
                    out.append(d)
                elif order[s2] > order.get(e.stage, -1):
                    if last.get(s2) and last.get(e.stage) and not last[s2] > last[e.stage]:
                        continue  # written before the stage that raised it
                    out.append(d)
                elif s2 == e.stage and e.dropped:
                    # The last stage (the audit) has no later stage.  A FRESH
                    # checker rewriting the same stage may settle it: one that
                    # signed the current version, when no raiser did (else the
                    # raising auditor launders its own verdict through a
                    # stooge that re-Writes the text; Review, loop 3).
                    recs = (history or {}).get(s2, [])
                    writer = last_writer.get(s2)
                    current = _file_sha(run_dir / s2)
                    signers_now = {r.get("agent") for r in recs if r.get("sha") == current and r.get("agent")}
                    who = raisers.get(e.id, set())
                    if writer and writer != "root" and writer not in who and writer in signers_now \
                            and not (signers_now & who):
                        out.append(d)
        return out

    outcomes = [findings_mod.evaluate(e, eligible(e), view) for e in entries.values()]
    for o in outcomes:
        if o.code:
            v.add(o.code, "medium", o.message)
    return outcomes


def write_verdict(run_dir: Path, v: Verdict) -> Path:
    """Write ``verdict.json`` into the run directory."""
    p = run_dir / "verdict.json"
    p.write_text(json.dumps(v.to_dict(), indent=2, sort_keys=True), encoding="utf-8")
    if v.metrics:
        (run_dir / "metrics.json").write_text(json.dumps(v.metrics, indent=2, sort_keys=True), encoding="utf-8")
    return p


def format_verdict(v: Verdict) -> str:
    """The human-readable verdict; its first line is ``PIPELINE-VERDICT <grade>``."""
    lines = [f"PIPELINE-VERDICT {v.grade}"]
    if v.facts.get("depth"):
        lines.append(f"depth: {v.facts['depth']}  tree: {str(v.facts.get('fingerprint', ''))[:12]}")
    passed = sum(1 for c in v.criteria if c["status"] == "passed")
    if v.criteria:
        # Verification status: did the prescribed checks run, with valid evidence?
        lines.append(f"criteria: {passed}/{len(v.criteria)} passed on the final tree")
        for c in v.criteria:
            lines.append(f"  - {c['id']}: {c['status']}")
    reg = v.facts.get("register") or []
    if reg:
        # Outcome confidence: did the evidence settle every planned scenario and
        # every suspected defect?  Anything not settled is named here.
        counts = {k: len(ids) for k, ids in (v.facts.get("outcome") or {}).items() if ids}
        lines.append(f"register: {len(reg)} item(s): " + ", ".join(f"{n} {k}" for k, n in counts.items()))
        for o in reg:
            by = f" by {','.join(o['by'])}" if o.get("by") else ""
            lines.append(f"  - {o['id']} ({o['kind']}): {o['state']}{by}")
    if v.findings:
        lines.append("findings:")
        for f in v.findings:
            lines.append(f"  [{f.severity}] {f.code}: {f.message}")
    else:
        lines.append("findings: none")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m pipeline.harness")
    ap.add_argument("run_dir")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--expect", choices=GRADES, default=None, help="exit 1 unless the grade is this")
    a = ap.parse_args(argv)
    run_dir = Path(a.run_dir)
    v = grade_run(run_dir, prices=metrics_mod.load_prices(None))
    if run_dir.is_dir():
        write_verdict(run_dir, v)
    print(json.dumps(v.to_dict(), indent=2) if a.json else format_verdict(v))
    if v.metrics and not a.json:
        print(metrics_mod.format_metrics(v.metrics))
    if a.expect and v.grade != a.expect:
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
