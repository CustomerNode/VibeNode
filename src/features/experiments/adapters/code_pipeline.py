"""Provenance adapter for Controlled A/B Test: did this trial run Code Pipeline?

    python src/features/experiments/adapters/code_pipeline.py <trial workdir> <trial start epoch> [--approved <pipeline dir>]

Prints one JSON line for the controller:
- ``{"impl": "none", "absent": [<runs folder>]}`` when the trial's copy holds no
  Code Pipeline run (normal coding); the controller re-checks that folder itself;
- ``{"impl": "code-pipeline:<identity>", "record": "<run.json>"}`` when exactly one
  run started after the trial did and ``scripts/pipeline_provenance.py`` proves
  it came from the approved pipeline;
- exit 1 with ``reason`` otherwise (a run from before the trial, two runs, or a
  run the checker cannot prove).

The checker runs from the VibeNode repository, never from the trial's copy, so
an older pipeline inside the copy cannot vouch for itself.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[4]
RUNS = Path("docs") / "plans" / "runs"


def main(argv=None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    approved = []
    if "--approved" in argv:
        i = argv.index("--approved")
        approved = ["--approved", argv[i + 1]]
        del argv[i:i + 2]
    workdir, started = Path(argv[0]), float(argv[1])
    runs = sorted(p for p in (workdir / RUNS).glob("*") if (p / "run.json").is_file())
    if not runs:
        # Evidence of absence the controller re-checks itself, not a bare label.
        print(json.dumps({"impl": "none", "absent": [str(workdir / RUNS)]}))
        return 0
    fresh = [p for p in runs if (p / "run.json").stat().st_mtime >= started]
    if len(fresh) != len(runs):
        print(json.dumps({"impl": "unknown", "reason": "a pipeline run in the copy predates this trial"}))
        return 1
    if len(fresh) > 1:
        print(json.dumps({"impl": "unknown", "reason": f"{len(fresh)} pipeline runs in one trial"}))
        return 1
    run = fresh[0]
    r = subprocess.run([sys.executable, str(REPO / "scripts" / "pipeline_provenance.py"), str(run), *approved],
                       capture_output=True, text=True, cwd=REPO)
    try:
        impl = json.loads((run / "run.json").read_text(encoding="utf-8")).get("impl") or "?"
    except (OSError, ValueError):
        impl = "?"
    if r.returncode != 0:
        print(json.dumps({"impl": f"code-pipeline:{impl}", "record": str(run / "run.json"),
                          "reason": "the run is not provably from the approved pipeline: "
                                    + (r.stdout + r.stderr).strip().replace("\n", " | ")[:400]}))
        return 1
    print(json.dumps({"impl": f"code-pipeline:{impl}", "record": str(run / "run.json")}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
