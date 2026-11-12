"""Compatibility wrapper: A/B Test is now AI Compare.

Every command line the old A/B Test runner accepted still works and runs AI
Compare (``scripts/ai_compare.py``), with two deliberate differences: the
prompt is sent exactly as written (the old runner appended a Downloads date
rule and renamed files inside it), and variants run read-only. A comparison
that has to change files belongs in Controlled A/B Test
(``scripts/controlled_ab_test.py``), which gives each variant its own copy.

The helpers older code imported from here are re-exported from the shared
module ``features.experiments.sessions``.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from features.experiments.compare import assign_models, build_variants, variant_name  # noqa: E402,F401
from features.experiments.sessions import (  # noqa: E402,F401
    read_outcome, resolve_id, resolve_model, same_model, short_answer,
)


def main(argv=None) -> int:
    print("A/B Test is now AI Compare; running scripts/ai_compare.py", file=sys.stderr)
    import importlib.util
    spec = importlib.util.spec_from_file_location("ai_compare", Path(__file__).with_name("ai_compare.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod.main(argv, prog="ab_test.py")


if __name__ == "__main__":
    raise SystemExit(main())
