"""Compute a run's confidence grade from what the Session Engine recorded.

    python -m pipeline.harness <run-dir> [--json] [--expect HIGH]

The grade is never awarded by an agent.  It is computed here from four kinds
of evidence, none of which an agent can produce by writing a stage file:

* **Executions.**  Each criterion in the plan's ``verification`` block has an
  evidence file that ``pipeline.run_check`` wrote, and some transcript shows a
  shell command printing the ``PIPELINE-EVIDENCE`` line with the SHA of the
  bytes on disk.  A typed "427 passed" in ``04-test.md`` counts for nothing.
* **Provenance.**  Each checker stage file (review, test, audit) was written
  by a subagent, in its own transcript, from a brief that does not paste the
  builder's output.  A stage file the root session wrote is self-grading.
* **Revision.**  Every required criterion ran against the tree being graded:
  the fingerprint in its evidence equals the working tree's fingerprint now,
  and the plan block it read is the plan block on disk.
* **Completeness.**  Every stage the route and depth require exists, and the
  audit answers the fourth question.

Severities: ``block`` (the run cannot be graded), ``low`` (a failed or forged
check: LOW), ``medium`` (missing independence or stale evidence: MEDIUM at
most), ``info``.  HIGH requires no finding above ``info``.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from pipeline import depth as depth_mod
from pipeline.evidence import EVIDENCE_DIR, EVIDENCE_PREFIX, evidence_sha, load_run, tree_fingerprint
from pipeline.plan import PlanError, load_plan
from pipeline.transcripts import (Transcript, find_root_transcript, is_run_check_call, read_transcript,
                                  shell_writes_under, subagents_of, writers_of, writes_under)

GRADES = ("HIGH", "MEDIUM", "LOW", "BLOCKED")

# The stage each route starts with, and the checker stages each depth requires
# (light depth drops the Plan and Review stages, never Test or Audit).
ROUTE_ENTRY = {"a": "01-plan.md", "b": "05-debug.md", "c": "06-hotfix.md", "d": "07-refactor.md"}
CHECKER_STAGES_FULL = ("03-review.md", "04-test.md", "08-audit.md")
CHECKER_STAGES_LIGHT = ("04-test.md", "08-audit.md")
# The fourth question is a HEADING in the audit, so a mention of the phrase in
# the introduction does not count as an answered section.
FOURTH_QUESTION_RE = re.compile(r"^#+[^\n]*what\s+could\s+still\s+be\s+wrong[^\n]*$", re.IGNORECASE | re.MULTILINE)
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
    """The stage files a run on *route* at *depth* must have, in order."""
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


def grade_run(run_dir: Path) -> Verdict:
    """Compute the verdict for the run in *run_dir* from the engine's records.

    Reads ``run.json``, the stage files, the evidence folder, the working tree
    and the CLI transcripts under ``~/.claude/projects``; writes nothing.
    """
    v = Verdict()
    run_dir = run_dir.resolve()
    v.facts["run_dir"] = str(run_dir)
    try:
        run = load_run(run_dir)
    except PlanError as e:
        v.add("no-run", "block", str(e))
        return v.finish()
    repo = Path(run.get("repo") or "")
    if not repo.is_dir():
        v.add("no-repo", "block", f"repository {repo} is not a directory")
        return v.finish()
    route = str(run.get("route") or "a")
    nonce = str(run.get("nonce") or "")
    project_cwd = str(run.get("project_cwd") or repo)
    v.facts.update({"run_id": run.get("run_id"), "route": route, "repo": str(repo)})

    # 1. The engine's records of this run.
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
    v.facts["root_transcript"] = str(root_path)
    v.facts["subagents"] = [t.agent_id for t in subs]

    # 2. Depth, by rule, from the diff as it is now.
    depth, reasons, dfacts = depth_mod.required_depth(repo, str(run.get("base_rev") or ""))
    v.facts["depth"] = depth
    v.facts["depth_reasons"] = reasons
    v.facts["changed_files"] = dfacts["files"]
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

    # 4. Provenance of the checkers.
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
    now_fp = tree_fingerprint(repo)
    v.facts["fingerprint"] = now_fp
    edir = run_dir / EVIDENCE_DIR
    for t, c in writes_under(everything, edir):
        v.add("evidence-forged", "low",
              f"{c.file_path} was written with the {c.name} tool by {'subagent ' + t.agent_id if t.is_subagent else 'the root session'}; "
              f"evidence is written only by pipeline.run_check")
    for t, c in shell_writes_under(everything, edir):
        v.add("evidence-forged", "low",
              f"a shell command touched the evidence folder outside pipeline.run_check: {c.command[:120]!r}")
    shell_calls = [(t, c) for t in everything for c in t.calls_named("Bash", "PowerShell")]
    for crit in criteria:
        entry = {"id": crit.id, "required": crit.required, "executable": crit.executable, "status": "unknown"}
        v.criteria.append(entry)
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
        if rec.get("passed"):
            entry["status"] = "passed"
        else:
            entry["status"] = "failed"
            entry["exit_code"] = rec.get("exit_code")
            v.add("check-failed", "low" if crit.required else "info",
                  f"criterion {crit.id} failed (exit {rec.get('exit_code')}"
                  + (", expect did not match" if rec.get("expect_matched") is False else "")
                  + (", timed out" if rec.get("timed_out") else "") + ")")

    # 6. The fourth question.
    audit = run_dir / "08-audit.md"
    if audit.is_file():
        text = audit.read_text(encoding="utf-8", errors="replace")
        m = FOURTH_QUESTION_RE.search(text)
        answer = text[m.end():] if m else ""
        answer = re.sub(r"^[^\n]*\n", "", answer, count=1) if m else ""
        if not m or len(re.sub(r"\s+", " ", answer).strip()) < 40:
            v.add("fourth-question", "medium",
                  "08-audit.md does not answer 'what could still be wrong that no check would catch?'")

    v.facts["graded_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
    return v.finish()


def write_verdict(run_dir: Path, v: Verdict) -> Path:
    """Write ``verdict.json`` into the run directory (the ledger reads it)."""
    p = run_dir / "verdict.json"
    p.write_text(json.dumps(v.to_dict(), indent=2, sort_keys=True), encoding="utf-8")
    return p


def format_verdict(v: Verdict) -> str:
    """The human-readable verdict; its first line is ``PIPELINE-VERDICT <grade>``."""
    lines = [f"PIPELINE-VERDICT {v.grade}"]
    if v.facts.get("depth"):
        lines.append(f"depth: {v.facts['depth']}  tree: {str(v.facts.get('fingerprint', ''))[:12]}")
    passed = sum(1 for c in v.criteria if c["status"] == "passed")
    if v.criteria:
        lines.append(f"criteria: {passed}/{len(v.criteria)} passed on the final tree")
        for c in v.criteria:
            lines.append(f"  - {c['id']}: {c['status']}")
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
    v = grade_run(run_dir)
    if run_dir.is_dir():
        write_verdict(run_dir, v)
    print(json.dumps(v.to_dict(), indent=2) if a.json else format_verdict(v))
    if a.expect and v.grade != a.expect:
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
