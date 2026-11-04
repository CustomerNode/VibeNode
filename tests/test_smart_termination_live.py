"""The Session Engine checks cards with the rules on disk now, not the ones it
started with (a rule added after the engine started would otherwise never be
enforced).  Runs against a copy of the rule
files in a separate interpreter, so the real files are never touched."""

import shutil
import subprocess
import sys
import textwrap
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
FILES = ("stop_plugins/__init__.py", "handoff_evidence.py", "handoff_rules.py", "smart_termination.py",
         "smart_termination_live.py")

SCRIPT = textwrap.dedent(r"""
    import sys, time, os
    sys.path.insert(0, sys.argv[1])
    from engine import smart_termination_live as live
    live.QUIET_S = 0
    rules = os.path.join(sys.argv[1], "engine", "handoff_rules.py")

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
    pkg = tmp_path / "engine"
    pkg.mkdir()
    (pkg / "__init__.py").write_text("")
    shutil.copytree(ROOT / "src" / "shared", tmp_path / "shared",
                    ignore=shutil.ignore_patterns("__pycache__"))
    for name in FILES:
        (pkg / name).parent.mkdir(parents=True, exist_ok=True)
        shutil.copy(ROOT / "src" / "engine" / name, pkg / name)
    r = subprocess.run([sys.executable, "-c", SCRIPT, str(tmp_path)], capture_output=True, text=True,
                       timeout=120, cwd=str(tmp_path))
    assert r.returncode == 0, r.stderr
    assert r.stdout.strip().endswith("ok")


def test_the_engine_reads_the_rules_through_the_live_loader():
    src = (ROOT / "src" / "engine" / "session_manager.py").read_text()
    assert "from engine import smart_termination as _st" not in src
    assert src.count("smart_termination_live import module") == 2


def test_the_add_on_loader_reloads_with_the_rules():
    """Otherwise a fix to the add-on loader's scoping waits for an engine restart,
    and unrelated sessions stay held to another project's checks."""
    from engine import smart_termination_live as live
    assert "stop_plugins/__init__.py" in live.FILES
    assert live._module_name("stop_plugins/__init__.py") == "engine.stop_plugins"
    assert live.FILES.index("stop_plugins/__init__.py") < live.FILES.index("handoff_rules.py")
