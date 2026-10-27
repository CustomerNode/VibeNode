"""Append a finished run to ``LEDGER.md``: the pipeline's operational memory.

    python -m pipeline.ledger <run-dir> [--delivered "..."] [--obstacles "..."]

One row per run.  Step 0 of every run reads the ledger; the last step appends
to it.  The three measures the spec asks for come from these rows: escaped
defects after HIGH (a later row's ``obstacles`` or a repair run naming an
earlier run), verification completeness (``criteria``), and time to verified
delivery (``duration``).  The ledger remembers; the regression tests a run
leaves in the repository are what converge.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

from pipeline.evidence import RUNS_SUBDIR, load_run

HEADER = (
    "# Code Pipeline ledger\n\n"
    "One row per run, appended by `python -m pipeline.ledger`. Read it before starting a run.\n\n"
    "| run | date | route | depth | duration | grade | criteria | delivered | obstacles |\n"
    "|---|---|---|---|---|---|---|---|---|\n"
)


def _cell(s: str) -> str:
    return str(s or "").replace("|", "\\|").replace("\n", " ").strip() or "-"


def _duration(created_at: str, graded_at: str) -> str:
    try:
        a = datetime.fromisoformat(created_at)
        b = datetime.fromisoformat(graded_at)
        if a.tzinfo is None:
            a = a.replace(tzinfo=timezone.utc)
        if b.tzinfo is None:
            b = b.replace(tzinfo=timezone.utc)
        mins = int((b - a).total_seconds() // 60)
        return f"{mins // 60}h{mins % 60:02d}m" if mins >= 60 else f"{mins}m"
    except (ValueError, TypeError):
        return "-"


def ledger_path(repo: Path) -> Path:
    """``docs/plans/runs/LEDGER.md`` in *repo*."""
    return repo / RUNS_SUBDIR / "LEDGER.md"


def append_row(run_dir: Path, *, delivered: str = "", obstacles: str = "") -> Path:
    """Append the run's row (from ``run.json`` and ``verdict.json``) to the
    ledger, replacing an earlier row for the same run id.  Returns the path."""
    run_dir = run_dir.resolve()
    run = load_run(run_dir)
    repo = Path(run["repo"])
    verdict_path = run_dir / "verdict.json"
    verdict = json.loads(verdict_path.read_text(encoding="utf-8")) if verdict_path.is_file() else {}
    facts = verdict.get("facts", {})
    crit = verdict.get("criteria", [])
    passed = sum(1 for c in crit if c.get("status") == "passed")
    row = "| " + " | ".join([
        _cell(run.get("run_id")),
        _cell(str(run.get("created_at", ""))[:10]),
        _cell(run.get("route")),
        _cell(facts.get("depth") or run.get("depth")),
        _duration(str(run.get("created_at", "")), str(facts.get("graded_at", ""))),
        _cell(verdict.get("grade", "UNGRADED")),
        f"{passed}/{len(crit)}" if crit else "-",
        _cell(delivered),
        _cell(obstacles),
    ]) + " |\n"
    path = ledger_path(repo)
    path.parent.mkdir(parents=True, exist_ok=True)
    existing = path.read_text(encoding="utf-8") if path.is_file() else HEADER
    if not existing.startswith("# Code Pipeline ledger"):
        existing = HEADER + existing
    key = f"| {_cell(run.get('run_id'))} |"
    lines = [ln for ln in existing.splitlines(keepends=True) if not ln.startswith(key)]
    if lines and not lines[-1].endswith("\n"):
        lines[-1] += "\n"
    path.write_text("".join(lines) + row, encoding="utf-8")
    return path


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m pipeline.ledger")
    ap.add_argument("run_dir")
    ap.add_argument("--delivered", default="", help="what shipped: files, commit, or 'not committed'")
    ap.add_argument("--obstacles", default="", help="one line the next run should know")
    a = ap.parse_args(argv)
    try:
        p = append_row(Path(a.run_dir), delivered=a.delivered, obstacles=a.obstacles)
    except Exception as e:  # noqa: BLE001 - a CLI reports and exits
        print(f"ledger: {e}", file=sys.stderr)
        return 2
    print(f"PIPELINE-LEDGER {p}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
