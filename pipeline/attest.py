"""A checker attests its report: this content, judged on this tree.

    python -m pipeline.attest <run-dir> <stage-file>

Run by the checker, from inside its own session, as the last thing it does
after writing its stage file (``03-review.md``, ``04-test.md``,
``08-audit.md``), and again whenever it changes the report.  It records the
SHA-256 of the file's bytes and the fingerprint of the working tree the checker
examined, appends that to ``evidence/<stage>.attest.json``, and prints::

    PIPELINE-ATTEST stage=<file> sha=<sha256> fingerprint=<full fingerprint> [blocking=<id,id>]

The Session Engine records that line as the command's output.  The harness
then requires, for every checker stage: an attest line in the transcript of
the subagent that wrote the stage, whose ``sha`` is the sha of the file as it
is NOW (a report changed after the checker signed it is not the checker's
report), and whose ``fingerprint`` is the tree as it is NOW (a Review that
examined an earlier tree has not reviewed the final code).

The line also carries every ``BLOCKING:`` id the report held when it was
signed (the attest record file keeps them too, but the printed line is the
record an agent cannot delete), so a finding cannot disappear by rewriting
the report: it is either still open or ``RESOLVED:`` by a later checker.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

from pipeline.evidence import EVIDENCE_DIR, _file_sha, load_run, tree_fingerprint
from pipeline.plan import PlanError

ATTEST_PREFIX = "PIPELINE-ATTEST"
STAGE_NAME_RE = re.compile(r"^\d{2}-[a-z][a-z0-9-]*\.md$")
BLOCKING_RE = re.compile(r"^[\s>*_-]*BLOCKING[\s*_]*:[\s*_]*([A-Za-z0-9][A-Za-z0-9_.-]*)", re.MULTILINE)
RESOLVED_RE = re.compile(r"^[\s>*_-]*RESOLVED[\s*_]*:[\s*_]*([A-Za-z0-9][A-Za-z0-9_.-]*)", re.MULTILINE)


def attest_line(rec: dict) -> str:
    """The one line ``attest`` prints; the harness parses exactly this.  The
    ``blocking=`` field appears only when the signed report carries findings
    (ids never contain commas: see ``BLOCKING_RE``)."""
    line = f"{ATTEST_PREFIX} stage={rec['stage']} sha={rec['sha']} fingerprint={rec['fingerprint']}"
    if rec.get("blocking"):
        line += " blocking=" + ",".join(rec["blocking"])
    return line


def attest_path(run_dir: Path, stage: str) -> Path:
    """``evidence/<stage>.attest.json``: the stage's attest history, oldest first."""
    return run_dir / EVIDENCE_DIR / f"{stage}.attest.json"


def load_attestations(run_dir: Path, stage: str) -> list[dict]:
    """Every record attested for *stage*, oldest first; [] when none or unreadable."""
    p = attest_path(run_dir, stage)
    if not p.is_file():
        return []
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except ValueError:
        return []
    return [r for r in data if isinstance(r, dict)] if isinstance(data, list) else []


def attest_stage(run_dir: Path, stage: str, *, repo: Path | None = None) -> tuple[dict, Path]:
    """Record the attestation for *stage*; returns (record, attest file path)."""
    run_dir = run_dir.resolve()
    if not STAGE_NAME_RE.match(stage):
        raise PlanError(f"{stage!r} is not a stage file name (NN-name.md)")
    run = load_run(run_dir)
    repo = (repo or Path(run["repo"])).resolve()
    target = run_dir / stage
    if not target.is_file():
        raise PlanError(f"{target} does not exist; write the report first, then attest it")
    text = target.read_text(encoding="utf-8", errors="replace")
    rec = {
        "stage": stage,
        "sha": _file_sha(target),
        "fingerprint": tree_fingerprint(repo),
        "at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "blocking": sorted(set(BLOCKING_RE.findall(text))),
        "resolved": sorted(set(RESOLVED_RE.findall(text))),
        "run_id": run.get("run_id"),
        "recorder": "pipeline.attest",
    }
    path = attest_path(run_dir, stage)
    path.parent.mkdir(parents=True, exist_ok=True)
    history = load_attestations(run_dir, stage)
    history.append(rec)
    path.write_text(json.dumps(history, indent=2, sort_keys=True), encoding="utf-8")
    return rec, path


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m pipeline.attest")
    ap.add_argument("run_dir")
    ap.add_argument("stage", help="the stage file you wrote, e.g. 08-audit.md")
    a = ap.parse_args(argv)
    try:
        rec, _path = attest_stage(Path(a.run_dir), a.stage)
    except (PlanError, RuntimeError, OSError) as e:
        print(f"attest: {e}", file=sys.stderr)
        return 2
    print(attest_line(rec))
    if rec["blocking"]:
        print(f"attest: {a.stage} carries BLOCKING: {', '.join(rec['blocking'])}; a later checker must RESOLVE each")
    return 0


if __name__ == "__main__":
    sys.exit(main())
