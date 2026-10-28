"""Execute one verification criterion and record the evidence.

    python -m pipeline.run_check <run-dir> <criterion-id> [--repo PATH] [--baseline]

``--baseline`` runs a ``detects_change`` criterion against the code as it was
when the run started (an isolated worktree, never the working directory) with
the criterion's test files copied in.  It exits 0 when the demonstration holds:
the command failed there, for the reason ``fails_with`` names.

Exit status is 0 when the criterion passed, 1 when it failed, 2 on a plan or
run error.  The last line of stdout is the ``PIPELINE-EVIDENCE`` line the
harness matches against the file on disk; the command's own output is echoed
first, in full, so a failure's traceback is in the transcript as well as in the
evidence file.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from pipeline.evidence import baseline_label, evidence_line, evidence_sha, run_baseline, run_criterion
from pipeline.plan import PlanError


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m pipeline.run_check")
    ap.add_argument("run_dir")
    ap.add_argument("criterion_id")
    ap.add_argument("--repo", default=None, help="repository to run in (default: run.json's repo)")
    ap.add_argument("--quiet", action="store_true", help="do not echo the command's output")
    ap.add_argument("--baseline", action="store_true",
                    help="regression demonstration: run against the starting snapshot; passes when it FAILS there")
    a = ap.parse_args(argv)
    # A Windows console is cp1252: echoing a test's "…" crashed the first
    # qualification run's check after the evidence was written.  Print what
    # cannot be encoded as a replacement instead of dying mid-report.
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(errors="replace")
        except (AttributeError, ValueError):
            pass
    runner = run_baseline if a.baseline else run_criterion
    try:
        record, path = runner(Path(a.run_dir), a.criterion_id,
                              repo=Path(a.repo) if a.repo else None)
    except PlanError as e:
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
