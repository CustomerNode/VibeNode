"""Execute one verification criterion and record the evidence.

    python -m pipeline.run_check <run-dir> <criterion-id> [--repo PATH]

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

from pipeline.evidence import evidence_line, evidence_sha, run_criterion
from pipeline.plan import PlanError


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m pipeline.run_check")
    ap.add_argument("run_dir")
    ap.add_argument("criterion_id")
    ap.add_argument("--repo", default=None, help="repository to run in (default: run.json's repo)")
    ap.add_argument("--quiet", action="store_true", help="do not echo the command's output")
    a = ap.parse_args(argv)
    try:
        record, path = run_criterion(Path(a.run_dir), a.criterion_id,
                                     repo=Path(a.repo) if a.repo else None)
    except PlanError as e:
        print(f"run_check: {e}", file=sys.stderr)
        return 2
    except (RuntimeError, OSError) as e:
        print(f"run_check: {e}", file=sys.stderr)
        return 2
    sha = evidence_sha(path)
    line = evidence_line(record["id"], sha, record["passed"], record["fingerprint"])
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
    if not record["tree_stable"]:
        print(f"run_check: {record['id']} changed the working tree while running; evidence is not stable", file=sys.stderr)
    return 0 if record["passed"] else 1


if __name__ == "__main__":
    sys.exit(main())
