"""AI Compare: run two or more variants in fresh read-only VibeNode sessions.

Each variant becomes a real session in the active VibeNode project (it shows in
the sidebar under its name). Prompts are sent exactly as written. Results land
in ``data/ai_compare/<run id>/``: ``report.md`` (side by side), ``run.json``
and one folder per variant with its reply and transcript copy.

Usage (any number of variants from two up, same prompt or different):
    python scripts/ai_compare.py --prompt "who is the president" --count 3
    python scripts/ai_compare.py --each "say it in one line" "say it in a paragraph"
    python scripts/ai_compare.py --prompt "who is the president" --names "test a" "test b"
    python scripts/ai_compare.py "short=answer in one line" "long=explain in detail"
    python scripts/ai_compare.py --prompt "who is the president" --models opus sonnet haiku
    python scripts/ai_compare.py ... --timeout 900 --json results.json

Cancel a running comparison with Ctrl-C, or by creating a file named CANCEL in
its run folder; unfinished variants are reported as cancelled, never dropped.
Exit status: 0 when every variant completed, 1 otherwise.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from features.experiments import compare as C  # noqa: E402
from features.experiments import sessions as S  # noqa: E402


def parser(prog: str | None = None) -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog=prog, description=__doc__.split("\n")[0])
    ap.add_argument("variants", nargs="*", help='"name=prompt" pairs')
    ap.add_argument("--prompt", help="one prompt for every variant (--names or --count)")
    ap.add_argument("--names", nargs="*", default=[])
    ap.add_argument("--count", type=int, default=0, help="with --prompt: run it this many times")
    ap.add_argument("--each", nargs="*", default=[], help="a different prompt per variant")
    ap.add_argument("--models", nargs="*", default=[],
                    help='a model for all variants, or one per variant ("opus", "sonnet 5", "haiku"); '
                         'with --prompt alone, one variant per model')
    ap.add_argument("--timeout", type=float, default=600, help="seconds to wait for every variant")
    ap.add_argument("--settle", type=float, default=8)
    ap.add_argument("--json", help="also write the run record here")
    return ap


def main(argv=None, prog=None, client=None) -> int:
    try:  # a Windows console would print "·" and "→" as "?"
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass
    ap = parser(prog)
    a = ap.parse_args(argv)
    if a.prompt and a.models and not (a.names or a.count or a.each or a.variants):
        a.count = len(a.models)  # one prompt, one variant per model
    client = client or S.VibeNodeClient()
    try:
        variants = C.build_variants(a.variants, a.prompt, a.names, a.count, a.each)
        variants = C.assign_models(variants, a.models, client.models() if a.models else [])
    except ValueError as e:
        ap.error(str(e))
    if not (a.names or a.variants):
        variants = [((f"{n} · {m['name']}" if m else n), p, m) for n, p, m in variants]
    rec = C.run(variants, client=client, timeout=a.timeout, settle=a.settle)
    print()
    print(Path(rec["report"]).read_text(encoding="utf-8"))
    print(f"Report: {rec['report']}")
    if a.json:
        Path(a.json).write_text(json.dumps(rec, indent=2, default=str), encoding="utf-8")
    return 0 if rec["all_completed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
