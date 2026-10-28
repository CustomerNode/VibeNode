"""A checker demands full depth.  One way: it cannot be undone.

    python -m pipeline.escalate <run-dir> "<why>"

A checker may demand more review, never less.  When a Test Team or an audit
on a light run finds the change is larger than its diff looks (a shared
helper, a subtle contract, a behavior the rule set did not name), it runs
this.  The harness then requires every full-depth stage, whatever the diff
says and whatever happens to the file this writes: the recorded
``PIPELINE-ESCALATE`` line in the checker's own transcript is the fact.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

from pipeline.evidence import EVIDENCE_DIR, load_run
from pipeline.plan import PlanError

ESCALATE_PREFIX = "PIPELINE-ESCALATE"
ESCALATION_FILE = "escalation.json"


def escalate(run_dir: Path, reason: str) -> tuple[dict, Path]:
    """Append an escalation record to ``evidence/escalation.json``; returns
    (record, path).  The record is for the CLI's report; the harness reads the
    printed line from the checker's transcript."""
    run_dir = run_dir.resolve()
    run = load_run(run_dir)
    reason = " ".join(reason.split())
    if not reason:
        raise PlanError("an escalation needs a reason")
    rec = {"depth": "full", "reason": reason, "at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
           "run_id": run.get("run_id"), "recorder": "pipeline.escalate"}
    path = run_dir / EVIDENCE_DIR / ESCALATION_FILE
    path.parent.mkdir(parents=True, exist_ok=True)
    history: list = []
    if path.is_file():
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            history = data if isinstance(data, list) else []
        except ValueError:
            history = []
    history.append(rec)
    path.write_text(json.dumps(history, indent=2, sort_keys=True), encoding="utf-8")
    return rec, path


def escalate_line(rec: dict) -> str:
    return f"{ESCALATE_PREFIX} depth=full reason={rec['reason']}"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m pipeline.escalate")
    ap.add_argument("run_dir")
    ap.add_argument("reason", help="why this change needs full depth")
    a = ap.parse_args(argv)
    try:
        rec, _ = escalate(Path(a.run_dir), a.reason)
    except (PlanError, RuntimeError, OSError) as e:
        print(f"escalate: {e}", file=sys.stderr)
        return 2
    print(escalate_line(rec))
    return 0


if __name__ == "__main__":
    sys.exit(main())
