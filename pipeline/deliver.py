"""The delivery gate: recompute the grade and refuse to deliver below HIGH.

    python -m pipeline.deliver <run-dir> [--accept MEDIUM]

Run immediately before any delivery action (commit, merge, push, package,
deploy).  It recomputes the verdict on the tree as it is now (never trusts
``verdict.json``), writes ``delivered.json`` into the run, and prints::

    PIPELINE-DELIVERED grade=<grade> fingerprint=<12> accepted=<true|false>

Exit 0 only when the grade is HIGH, or when the user explicitly accepted
the computed lower grade with ``--accept <grade>`` (that is the user's
decision, stated in the report, never the agent's).  Anything else exits 1
and the work is not delivered as verified.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

from pipeline.harness import GRADES, grade_run, write_verdict

DELIVERED_PREFIX = "PIPELINE-DELIVERED"


def deliver(run_dir: Path, *, accept: str | None = None) -> tuple[dict, bool]:
    run_dir = run_dir.resolve()
    v = grade_run(run_dir)
    if run_dir.is_dir():
        write_verdict(run_dir, v)
    ok = v.grade == "HIGH" or (accept is not None and accept == v.grade)
    rec = {
        "grade": v.grade,
        "fingerprint": v.facts.get("fingerprint"),
        "accepted_grade": accept,
        "delivered": ok,
        "at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "findings": [f.code for f in v.findings if f.severity != "info"],
    }
    if run_dir.is_dir():
        (run_dir / "delivered.json").write_text(json.dumps(rec, indent=2, sort_keys=True), encoding="utf-8")
    return rec, ok


def delivered_line(rec: dict) -> str:
    return (f"{DELIVERED_PREFIX} grade={rec['grade']} fingerprint={str(rec.get('fingerprint') or '')[:12]} "
            f"accepted={'true' if rec['delivered'] else 'false'}")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m pipeline.deliver")
    ap.add_argument("run_dir")
    # BLOCKED is an execution status, not a grade of the work, so there is
    # nothing in it a user could accept.
    ap.add_argument("--accept", choices=[g for g in GRADES if g not in ("HIGH", "BLOCKED")], default=None,
                    help="the user's explicit acceptance of this lower grade")
    a = ap.parse_args(argv)
    rec, ok = deliver(Path(a.run_dir), accept=a.accept)
    print(delivered_line(rec))
    if not ok:
        print(f"deliver: grade is {rec['grade']}, not HIGH; findings: {', '.join(rec['findings']) or 'none'}. "
              f"Not delivered as verified.", file=sys.stderr)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
