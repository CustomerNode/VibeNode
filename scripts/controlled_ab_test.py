"""Controlled A/B Test: isolated, repeated, independently verified experiments.

    python scripts/controlled_ab_test.py spec-help                 # the spec format
    python scripts/controlled_ab_test.py validate <spec.json>      # is it a valid experiment?
    python scripts/controlled_ab_test.py run <spec.json> [--trials N]
    python scripts/controlled_ab_test.py report <experiment folder>
    python scripts/controlled_ab_test.py score <experiment folder> <scores.json>   # rubric experiments

Records go to ``data/experiments/<experiment run id>/`` (never reused); each
trial's copy of the baseline goes to ``~/.vibenode-experiments/<run id>/``.
A real experiment takes longer than a turn: launch ``run`` as a durable job
(``python vibenode.py job run ...``). Cancel by creating a file named CANCEL in
the experiment folder.

Exit status: validate 0 valid / 1 invalid; run 0 when the experiment is valid
and every trial was verified, 1 otherwise, 2 for a spec that cannot run.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from features.experiments import controlled as X  # noqa: E402


def main(argv=None, client=None) -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv or argv[0] in ("-h", "--help"):
        print(__doc__)
        return 0
    cmd, rest = argv[0], argv[1:]
    if cmd == "spec-help":
        print(X.SPEC_HELP)
        return 0
    if cmd in ("validate", "run"):
        spec = json.loads(Path(rest[0]).read_text(encoding="utf-8"))
        if "--trials" in rest:
            spec["trials"] = int(rest[rest.index("--trials") + 1])
        problems = X.validate(spec)
        if cmd == "validate" or problems:
            if problems:
                print("INVALID experiment:")
                for p in problems:
                    print(f"  - {p}")
                return 1 if cmd == "validate" else 2
            print("valid: the experiment can run")
            return 0
        rec = X.run_experiment(spec, client=client)
        print()
        print(Path(rec["report"]).read_text(encoding="utf-8"))
        print(f"Report: {rec['report']}")
        ok = rec["summary"]["verdict"] != "invalid" and all(t.get("verdict") == X.VERIFIED for t in rec["trials"])
        return 0 if ok else 1
    if cmd == "report":
        exp = Path(rest[0])
        print((exp / "report.md").read_text(encoding="utf-8"))
        return 0
    if cmd == "score":
        exp = Path(rest[0])
        mapped = X.import_scores(exp, json.loads(Path(rest[1]).read_text(encoding="utf-8")))
        print(json.dumps(mapped, indent=2))
        print("Recorded as subjective, blinded scores; they do not make any trial verified.")
        return 0
    print(__doc__)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
