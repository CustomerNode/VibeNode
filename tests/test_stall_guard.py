"""A parallel run that stops making progress ends itself (tests/_stall_guard.py).

The Publish gate's run once sat at 98% for over twenty minutes: two workers
crashed, xdist replaced them, and then every worker sat idle while the
controller waited for a result that never came.
"""

import os
import subprocess
import sys
import textwrap
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from tests import _stall_guard as sg

REPO = Path(__file__).resolve().parents[1]


class _Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


class _PM:
    def __init__(self, dsession):
        self.dsession = dsession

    def getplugin(self, name):
        return self.dsession if name == "dsession" else None


def _guard(dsession, limit=300):
    clock = _Clock()
    seen = []
    config = SimpleNamespace(pluginmanager=_PM(dsession))
    g = sg.StallGuard(config, limit, clock=clock, on_stall=seen.append)
    return g, clock, seen


def _rep(nodeid, when="call"):
    return SimpleNamespace(nodeid=nodeid, when=when)


def _sched():
    return SimpleNamespace(
        workqueue={"tests/test_queued.py": {"tests/test_queued.py::t": False}},
        assigned_work={
            "gw0": {"tests/test_running.py": {"tests/test_running.py::a": True,
                                               "tests/test_running.py::b": False}},
            "gw1": {"tests/test_done.py": {"tests/test_done.py::a": True}},
        })


def test_the_limit_is_twice_the_test_timeout_plus_a_minute(monkeypatch):
    monkeypatch.delenv("VIBENODE_TEST_STALL_SECONDS", raising=False)
    assert sg.limit_for(120) == 300
    assert sg.limit_for(60) == 180
    assert sg.limit_for(None) == 300
    monkeypatch.setenv("VIBENODE_TEST_STALL_SECONDS", "7")
    assert sg.limit_for(120) == 7


def test_it_never_acts_before_the_first_test_reports():
    g, clock, seen = _guard(SimpleNamespace(sched=_sched()))
    clock.t += 10_000
    assert not g.check() and not seen


def test_it_never_acts_outside_an_xdist_controller():
    g, clock, seen = _guard(None)
    g.note_report(_rep("tests/test_a.py::t"))
    clock.t += 10_000
    assert not g.check() and not seen


def test_a_finished_test_restarts_the_count():
    g, clock, seen = _guard(SimpleNamespace(sched=_sched()), limit=300)
    g.note_report(_rep("tests/test_a.py::t"))
    clock.t += 299
    assert not g.check()
    g.note_report(_rep("tests/test_a.py::u"))
    clock.t += 299
    assert not g.check() and not seen


def test_a_stall_names_the_crashes_and_the_unfinished_files():
    g, clock, seen = _guard(SimpleNamespace(sched=_sched()), limit=300)
    g.note_report(_rep("tests/test_crashy.py::boom", when="???"))
    clock.t += 301
    assert g.check()
    msg = seen[0]
    assert "STALLED: no test finished for 301s" in msg
    assert "tests/test_crashy.py::boom" in msg
    assert "tests/test_queued.py" in msg and "tests/test_running.py" in msg
    assert "tests/test_done.py" not in msg


def test_a_real_parallel_run_that_hangs_ends_itself(tmp_path):
    pytest.importorskip("xdist")
    (tmp_path / "conftest.py").write_text(textwrap.dedent("""
        from tests import _stall_guard as sg
        _g = None

        def pytest_sessionstart(session):
            global _g
            if hasattr(session.config, "workerinput"):
                return
            _g = sg.StallGuard(session.config, sg.limit_for(None), poll=0.2)
            _g.start()

        def pytest_runtest_logreport(report):
            if _g is not None:
                _g.note_report(report)
    """), encoding="utf-8")
    (tmp_path / "test_fast.py").write_text("def test_fast():\n    pass\n", encoding="utf-8")
    (tmp_path / "test_stuck.py").write_text(
        "import time\n\ndef test_stuck():\n"
        "    time.sleep(120)  # slow-ok: the stuck test the guard must end\n", encoding="utf-8")
    env = dict(os.environ, VIBENODE_TEST_STALL_SECONDS="3",
               PYTHONPATH=os.pathsep.join([str(REPO), str(REPO / "src"),
                                           os.environ.get("PYTHONPATH", "")]))
    (tmp_path / "pytest.ini").write_text("[pytest]\n", encoding="utf-8")
    t0 = time.monotonic()
    proc = subprocess.run(
        [sys.executable, "-m", "pytest", "-n", "2", "-p", "no:cacheprovider", "-q",
         "--rootdir", str(tmp_path), "test_fast.py", "test_stuck.py"],
        cwd=str(tmp_path), env=env, capture_output=True, text=True, timeout=90)
    out = proc.stdout + proc.stderr
    assert proc.returncode == sg.EXIT_STALLED, out
    assert "STALLED: no test finished" in out, out
    assert "test_stuck.py" in out, out
    assert time.monotonic() - t0 < 60, "the guard should end the run within seconds of its limit"
