#!/usr/bin/env python3
"""Arm the provenance watch for a Code Pipeline comparison run, in one step.

    python scripts/pipeline_experiment.py --worktree <contestant worktree> [--hours 6] [--dry-run]

Run it BEFORE the contestant session starts.  It records the approved
pipeline (the ``pipeline/`` beside this script), its identity and the
identity of its guides, and the run folders that already exist in the
worktree, then starts ``scripts/pipeline_provenance.py --watch`` on the
worktree as a Durable Job (``vibenode.py job run``), so the watch survives a
closed terminal or a reboot and needs nobody to remember it.  The watch
ignores every run folder that existed at arming (no time window), takes the
first new run as THE run and fails on a second one, checks it as its
``run.json`` appears, on every change to its delivery record, and once more
when that record says delivered; a delivery waiting for the person's
acceptance keeps it watching.  Results go to
``data/pipeline_provenance/<worktree>.json``; a failure chimes every
VibeNode tab.

It also prints the launch line the contestant must use: the approved
pipeline run from the main checkout with ``--repo`` pointing at the
worktree, never ``python -m pipeline...`` from inside the worktree (that
imports the worktree's own copy).  That line is an instruction; the watch is
the guard.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import pipeline_provenance as prov  # noqa: E402

RESULTS = ROOT / "data" / "pipeline_provenance"


def arm(worktree: Path) -> Path:
    """Record the run folders that exist now; the watch ignores them."""
    RESULTS.mkdir(parents=True, exist_ok=True)
    armed = RESULTS / f"{worktree.resolve().name}.armed.json"
    armed.write_text(json.dumps(sorted(prov.existing_runs(worktree))), encoding="utf-8")
    return armed


def job_command(worktree: Path, armed: Path, hours: float) -> tuple[list[str], Path]:
    """The ``vibenode.py job run`` argv for the watch, and its result file."""
    name = worktree.resolve().name
    out = RESULTS / f"{name}.json"
    watch = (f'"{sys.executable}" "{ROOT / "scripts" / "pipeline_provenance.py"}" --repo "{worktree.resolve()}" '
             f'--watch --exclude-file "{armed}" --timeout {int(hours * 3600)} --out "{out}"')
    argv = [sys.executable, str(ROOT / "vibenode.py"), "job", "run", "--name", f"Pipeline provenance: {name}",
            "--command", watch, "--cwd", str(ROOT), "--done", r"WATCH DONE", "--json"]
    return argv, out


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python scripts/pipeline_experiment.py", description=__doc__.split("\n\n")[0])
    ap.add_argument("--worktree", required=True, help="the contestant's worktree")
    ap.add_argument("--hours", type=float, default=6, help="how long the watch waits for a delivered run")
    ap.add_argument("--dry-run", action="store_true", help="print what would be started, start nothing")
    a = ap.parse_args(argv)
    wt = Path(a.worktree)
    if not (wt / ".git").exists():
        print(f"{wt} is not a git worktree", file=sys.stderr)
        return 2
    armed = arm(wt)
    cmd, out = job_command(wt, armed, a.hours)
    approved = prov.DEFAULT_APPROVED
    print(f"approved pipeline: {approved}")
    print(f"approved identity: {prov.pipeline_identity(approved)[:12]}")
    print(f"approved guides: {prov.workflow_identity(approved.parent / 'workforce')[:12]}")
    print(f"ignoring {len(json.loads(armed.read_text(encoding='utf-8')))} run folder(s) already in "
          f"{wt.resolve() / prov.RUNS_SUBDIR}; the next new run is the one watched")
    print(f"results: {out}")
    print("contestant launch line (run from the main checkout, not the worktree):")
    print(f'  cd "{ROOT}" && python -m pipeline.start --repo "{wt.resolve()}" --project-cwd "{wt.resolve()}" '
          f"--slug <slug> --route <a|b|c|d> --request-file <file>")
    if a.dry_run:
        print("would start: " + json.dumps(cmd))
        return 0
    r = subprocess.run(cmd, cwd=str(ROOT), capture_output=True, text=True, timeout=60)
    print(r.stdout.strip() or r.stderr.strip())
    return r.returncode


if __name__ == "__main__":
    sys.exit(main())
