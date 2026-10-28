"""The Session Engine checks cards with the rules on disk now, not the ones it
started with (2026-10-28: the card "title" rule was added three minutes after
the engine started and was never enforced).  Runs against a copy of the rule
files in a separate interpreter, so the real files are never touched."""

import shutil
import subprocess
import sys
import textwrap
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
FILES = ("handoff_evidence.py", "handoff_rules.py", "smart_termination.py", "smart_termination_live.py")

SCRIPT = textwrap.dedent(r"""
    import sys, time, os
    sys.path.insert(0, sys.argv[1])
    from daemon import smart_termination_live as live
    live.QUIET_S = 0
    rules = os.path.join(sys.argv[1], "daemon", "handoff_rules.py")

    def bump(text):
        with open(rules, "a") as f:
            f.write(text)

    st = live.module()
    assert not hasattr(st._rules, "PROBE"), "fresh copy"
    bump("\nPROBE = 1\n")
    st = live.module()
    assert st._rules.PROBE == 1, "a rule added on disk is used without a restart"
    review_before = st.review
    bump("\nthis is not python\n")
    st = live.module()
    assert st._rules.PROBE == 1 and st.review is review_before, "a broken file keeps the running rules"
    assert live.module() is st, "a broken version is not retried"
    print("ok")
""")


def test_the_engine_picks_up_rule_changes_and_ignores_broken_ones(tmp_path):
    pkg = tmp_path / "daemon"
    pkg.mkdir()
    (pkg / "__init__.py").write_text("")
    for name in FILES:
        shutil.copy(ROOT / "daemon" / name, pkg / name)
    r = subprocess.run([sys.executable, "-c", SCRIPT, str(tmp_path)], capture_output=True, text=True,
                       timeout=120, cwd=str(tmp_path))
    assert r.returncode == 0, r.stderr
    assert r.stdout.strip().endswith("ok")


def test_the_engine_reads_the_rules_through_the_live_loader():
    src = (ROOT / "daemon" / "session_manager.py").read_text()
    assert "from daemon import smart_termination as _st" not in src
    assert src.count("smart_termination_live import module") == 2
