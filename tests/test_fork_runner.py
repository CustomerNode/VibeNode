"""The fork runner must report exactly what happened, like xdist and serial.

Each test runs a real nested pytest over a small made-up suite, with the real
conftest and runner, and reads the real terminal output, exit status and JUnit
file.  Plain subprocesses: the nested run is itself in the same PID namespace.
"""

import os
import subprocess
import sys
import textwrap
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest

from tests._fork_runner import plan_units

ROOT = Path(__file__).resolve().parents[1]

# The fork runner only exists on Linux (tests/conftest.py enables it there;
# os.fork has no Windows or macOS path), so these tests are Linux-only too.
# On Windows the nested run ends with pytest's internal-error exit code 3.
pytestmark = pytest.mark.skipif(not sys.platform.startswith("linux"),
                                reason="the fork runner is Linux-only (tests/conftest.py)")

# The real runner without the suite's conftest and its imports: a nested run
# only has to start the runner.
ENABLE = """
import pytest
@pytest.hookimpl(tryfirst=True)
def pytest_runtestloop(session):
    if not session.items or session.testsfailed:
        return None
    from tests._fork_runner import run_session
    return True if run_session(session, {workers}) else None
"""


def _suite(tmp_path, files: dict, workers=2, args=()):
    for name, src in files.items():
        (tmp_path / name).write_text(textwrap.dedent(src), encoding="utf-8")
    (tmp_path / "vn_enable_fork.py").write_text(ENABLE.format(workers=workers), encoding="utf-8")
    xml = tmp_path / "junit.xml"
    env = dict(os.environ, PYTHONPATH=f"{tmp_path}{os.pathsep}{ROOT}", VIBENODE_TEST_SANDBOXED="1")
    env.pop("VIBENODE_NO_CRASH_RETRY", None)
    proc = subprocess.run(
        [sys.executable, "-m", "pytest", "-p", "vn_enable_fork", "-p", "no:cacheprovider",
         "--rootdir", str(tmp_path), "--no-header", "--tb=short", "--timeout=30", f"--junitxml={xml}", str(tmp_path), *args],
        capture_output=True, text=True, env=env, cwd=str(ROOT), timeout=120)
    results = {}
    if xml.exists():
        for case in ET.parse(xml).getroot().iter("testcase"):
            kind = next((c.tag for c in case if c.tag in ("failure", "error", "skipped")), "passed")
            results[case.get("name")] = kind
    return proc, results


A = """
import os, pytest
SEEN = []
@pytest.fixture(scope="module")
def once():
    SEEN.append("up")
    return SEEN
def test_ok(once): assert once == ["up"]
def test_ok_too(once): assert once == ["up"]          # a module fixture is not rebuilt
def test_fails():
    print("output of the failing test")
    assert 1 == 2
@pytest.mark.skip(reason="not today")
def test_skipped(): pass
@pytest.mark.xfail
def test_xfails(): assert False
@pytest.mark.parametrize("n", [1, 2, 3])
def test_param(n): assert n
"""
B = """
import pytest
@pytest.fixture
def bad_teardown():
    yield
    raise RuntimeError("teardown broke")
def test_teardown_error(bad_teardown): pass
def test_prints_alone(capsys):
    print("from b")
    assert capsys.readouterr().out == "from b\\n"
"""


def test_every_outcome_reaches_the_report_and_the_exit_status(tmp_path):
    proc, results = _suite(tmp_path, {"test_a.py": A, "test_b.py": B})
    assert proc.returncode == 1, proc.stdout + proc.stderr
    assert results["test_ok"] == "passed" and results["test_ok_too"] == "passed"
    assert results["test_fails"] == "failure"
    assert results["test_skipped"] == "skipped" and results["test_xfails"] == "skipped"
    assert results["test_param[3]"] == "passed"
    assert results["test_teardown_error"] == "error"
    assert results["test_prints_alone"] == "passed"
    assert len(results) == 10
    assert "output of the failing test" in proc.stdout     # capture reaches the failure section
    assert "teardown broke" in proc.stdout
    assert "INTERNALERROR" not in proc.stdout + proc.stderr
    assert "1 failed, 7 passed, 1 skipped, 1 xfailed, 1 error" in proc.stdout


def test_all_green_exits_zero(tmp_path):
    proc, results = _suite(tmp_path, {"test_a.py": "def test_x(): pass\n", "test_b.py": "def test_y(): pass\n"})
    assert proc.returncode == 0, proc.stdout
    assert results == {"test_x": "passed", "test_y": "passed"}


def test_a_crash_is_retried_once_on_a_new_worker(tmp_path):
    marker = tmp_path / "crashed-once"
    proc, results = _suite(tmp_path, {"test_c.py": f"""
        import os
        from pathlib import Path
        def test_first(): pass
        def test_crashes_once():
            m = Path({str(marker)!r})
            if not m.exists():
                m.write_text("x")
                os._exit(3)
        def test_after(): pass
    """})
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert results == {"test_first": "passed", "test_crashes_once": "passed", "test_after": "passed"}


def test_a_test_that_always_crashes_fails_and_the_rest_still_run(tmp_path):
    proc, results = _suite(tmp_path, {"test_c.py": """
        import os
        def test_first(): pass
        def test_always_crashes(): os._exit(3)
        def test_after(): pass
    """})
    assert proc.returncode == 1, proc.stdout
    assert results["test_always_crashes"] == "failure"
    assert results["test_after"] == "passed"
    assert "crashed while running this test" in proc.stdout


def test_stop_on_first_failure_is_honoured(tmp_path):
    many = "\n".join(f"def test_{i}(): pass" for i in range(200))
    proc, results = _suite(tmp_path, {"test_a.py": "def test_bad(): assert 0\n" + many}, workers=1, args=("-x",))
    assert proc.returncode == 1
    assert results["test_bad"] == "failure"
    assert "test_199" not in results


def test_a_collection_error_is_reported_and_nothing_runs(tmp_path):
    proc, results = _suite(tmp_path, {"test_a.py": "def test_x(): pass\n", "test_b.py": "import no_such_module_at_all\n"})
    assert proc.returncode != 0
    assert "no_such_module_at_all" in proc.stdout
    assert "test_x" not in results


def test_workers_share_no_memory_and_no_temp_folder(tmp_path):
    src = """
        import os, pytest
        STATE = []
        def test_state(tmp_path):
            STATE.append(1)
            assert STATE == [1]
            assert not list(tmp_path.iterdir())
            print(f"\\nWORKER {os.environ['PYTEST_XDIST_WORKER']} {tmp_path}")
    """
    proc, results = _suite(tmp_path, {f"test_{i}.py": src for i in range(4)}, workers=4, args=("-s",))
    assert proc.returncode == 0, proc.stdout
    assert set(results.values()) == {"passed"}


class _Item:
    def __init__(self, nodeid):
        self.nodeid = nodeid


def test_plan_keeps_files_whole_and_puts_the_slowest_first(tmp_path):
    (tmp_path / "test_slow.py").write_text("x = 1\n", encoding="utf-8")
    (tmp_path / "test_fast.py").write_text("x = 1\n", encoding="utf-8")
    (tmp_path / "test_new.py").write_text("x = 1\n", encoding="utf-8")
    items = [_Item(f"test_fast.py::t{i}") for i in range(3)] + [_Item(f"test_slow.py::t{i}") for i in range(2)] \
        + [_Item("test_new.py::t0")]
    durations = {f"test_fast.py::t{i}": 0.1 for i in range(3)} | {f"test_slow.py::t{i}": 5.0 for i in range(2)}
    units = plan_units(items, durations, tmp_path)
    assert [sorted(u.indexes) for u in units] in ([[3, 4], [5], [0, 1, 2]], [[5], [3, 4], [0, 1, 2]])
    assert sorted(i for u in units for i in u.indexes) == list(range(len(items)))     # each test exactly once


def test_plan_splits_only_files_that_opted_in_and_marks_browser_work(tmp_path):
    (tmp_path / "test_split.py").write_text("XDIST_SPLITTABLE = True\nsync_playwright = 1\n", encoding="utf-8")
    (tmp_path / "test_whole.py").write_text("shared_browser_scope = 1\n", encoding="utf-8")
    items = [_Item(f"test_split.py::t{i}") for i in range(8)] + [_Item(f"test_whole.py::t{i}") for i in range(8)]
    units = plan_units(items, {}, tmp_path, chunks=4)
    assert all(u.heavy for u in units)
    split = [u for u in units if u.indexes[0] < 8]
    whole = [u for u in units if u.indexes[0] >= 8]
    assert len(split) == 4 and len(whole) == 1
    assert [sorted(u.indexes) for u in sorted(split, key=lambda u: u.indexes[0])] == [[0, 1], [2, 3], [4, 5], [6, 7]]


def test_a_native_thread_in_the_parent_is_gone_before_the_fork(tmp_path):
    # Importing the engine arms this native thread.  A worker that cancels it
    # would otherwise hang forever where no test timeout can reach it.
    proc, results = _suite(tmp_path, {"conftest.py": """
        import faulthandler
        faulthandler.dump_traceback_later(600)
    """, "test_a.py": """
        import faulthandler
        def test_cancels_in_a_worker():
            faulthandler.cancel_dump_traceback_later()
    """})
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert results == {"test_cancels_in_a_worker": "passed"}
    assert "multi-threaded" not in proc.stdout + proc.stderr
