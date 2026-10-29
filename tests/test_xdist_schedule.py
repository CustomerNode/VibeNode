"""The parallel-run scheduler (tests/_xdist_schedule.py): slowest work first,
at most HEAVY_CAP workers on browser work at once, opted-in browser files
split into contiguous chunks, everything else whole per file."""

from collections import OrderedDict
from pathlib import Path

import pytest

pytest.importorskip("xdist")

from tests._xdist_schedule import HEAVY_CAP, DurationOrderedLoadFileScheduling  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
# Built in pieces so this file is not itself detected as a browser file.
BROWSER_SRC = "from playwright.sync_api import sync_" + "playwright\n"


def _sched(durations=None, sources=None):
    s = DurationOrderedLoadFileScheduling.__new__(DurationOrderedLoadFileScheduling)
    s._rootdir = ROOT
    s._durations = durations or {}
    s._sources = dict(sources or {})
    s._chunk_of = {}
    s.assigned_work = {}
    s.workqueue = OrderedDict()
    return s


def test_slowest_unit_goes_first_even_with_few_tests():
    """The stall: a 2-test, 60s browser file was queued behind 100-test
    files because xdist ordered by test count."""
    units = {
        "tests/test_many.py": {f"tests/test_many.py::t{i}": False for i in range(100)},
        "tests/test_browser.py": {"tests/test_browser.py::a": False,
                                  "tests/test_browser.py::b": False},
    }
    durations = {n: 0.01 for n in units["tests/test_many.py"]}
    durations.update({"tests/test_browser.py::a": 30.0, "tests/test_browser.py::b": 30.0})
    assert _sched(durations).order_units(units)[0] == "tests/test_browser.py"


def test_unit_without_history_is_queued_as_the_slowest():
    """A new file has no measured time; it must not land at the tail."""
    units = {
        "tests/test_old.py": {"tests/test_old.py::a": False},
        "tests/test_new.py": {"tests/test_new.py::a": False},
        "tests/test_fast.py": {"tests/test_fast.py::a": False},
    }
    durations = {"tests/test_old.py::a": 5.0, "tests/test_fast.py::a": 0.1}
    order = _sched(durations).order_units(units)
    assert order.index("tests/test_new.py") < order.index("tests/test_fast.py")


def test_no_history_falls_back_to_test_count():
    units = {
        "tests/test_small.py": {"tests/test_small.py::a": False},
        "tests/test_big.py": {f"tests/test_big.py::t{i}": False for i in range(5)},
    }
    assert _sched().order_units(units) == ["tests/test_big.py", "tests/test_small.py"]


class _Node:
    shutting_down = False


def test_browser_work_is_capped_and_surplus_workers_wait():
    """Throughput of the real page peaks at 4 parallel browsers and halves at
    8.  Past the cap a worker takes non-browser work; with only browser work
    left it waits instead of joining the pile-up."""
    sources = {f"tests/test_b{i}.py": BROWSER_SRC for i in range(HEAVY_CAP + 1)}
    sources["tests/test_plain.py"] = "def test_x(): pass\n"
    s = _sched(sources=sources)
    busy = [_Node() for _ in range(HEAVY_CAP)]
    for i, n in enumerate(busy):
        s.assigned_work[n] = {f"tests/test_b{i}.py": {f"tests/test_b{i}.py::a": False}}
    node = _Node()
    s.assigned_work[node] = {}
    last = f"tests/test_b{HEAVY_CAP}.py"
    s.workqueue[last] = {last + "::a": False}
    s.workqueue["tests/test_plain.py"] = {"tests/test_plain.py::x": False}
    assert s.pick_unit(node) == "tests/test_plain.py"
    del s.workqueue["tests/test_plain.py"]
    assert s.pick_unit(node) is None
    # A slot frees up: the browser unit is offered.
    s.assigned_work[busy[0]][next(iter(s.assigned_work[busy[0]]))]["tests/test_b0.py::a"] = True
    assert s.pick_unit(node) == last


def test_a_waiting_worker_is_reoffered_work_when_any_test_finishes():
    """The wait must never become a hang: every finished test re-offers work
    to every worker that has none."""
    sources = {f"tests/test_b{i}.py": BROWSER_SRC for i in range(HEAVY_CAP + 1)}
    s = _sched(sources=sources)
    s.registered_collections = {}
    busy = [_Node() for _ in range(HEAVY_CAP)]
    for i, n in enumerate(busy):
        nid = f"tests/test_b{i}.py::a"
        s.assigned_work[n] = {f"tests/test_b{i}.py": {nid: False}}
        s.registered_collections[n] = [nid]
    waiting = _Node()
    s.assigned_work[waiting] = {}
    last = f"tests/test_b{HEAVY_CAP}.py"
    s.registered_collections[waiting] = [last + "::a"]  # waits only after collecting
    s.workqueue[last] = {last + "::a": False}
    s._queue_ordered = True
    offered = []
    s._reschedule = lambda n: offered.append(n)
    s.mark_test_complete(busy[0], 0)
    assert busy[0] in offered and waiting in offered


class _Worker(_Node):
    def __init__(self):
        self.sent = []

    def send_runtest_some(self, indexes):
        self.sent.append(list(indexes))

    def shutdown(self):
        self.shutting_down = True


def test_a_replacement_worker_is_not_given_work_before_it_has_collected():
    """2026-10-23: a worker crashed, xdist started a replacement, and the next
    finished test re-offered work to it before its collection arrived.  The
    real xdist path raised KeyError on registered_collections and aborted the
    whole run with an INTERNALERROR instead of reporting one crashed test."""
    s = _sched(sources={"tests/test_a.py": "", "tests/test_b.py": "", "tests/test_c.py": ""})
    s.log = lambda *a: None
    s.registered_collections = {}
    busy = _Worker()
    s.assigned_work[busy] = {"tests/test_a.py": {"tests/test_a.py::x": False}}
    s.registered_collections[busy] = ["tests/test_a.py::x", "tests/test_b.py::y",
                                      "tests/test_c.py::z"]
    replacement = _Worker()
    s.assigned_work[replacement] = {}  # added, not collected yet
    s.workqueue["tests/test_b.py"] = {"tests/test_b.py::y": False}
    s.workqueue["tests/test_c.py"] = {"tests/test_c.py::z": False}
    s._queue_ordered = True
    s.mark_test_complete(busy, 0)  # must not raise
    assert replacement.sent == [] and not replacement.shutting_down
    # The busy worker, which has collected, still gets the next unit, and the
    # other stays queued for the replacement.
    assert busy.sent == [[1]]
    assert list(s.workqueue) == ["tests/test_c.py"]


def test_splittable_files_split_into_contiguous_chunks_and_others_stay_whole():
    sources = {"tests/test_split.py": BROWSER_SRC + "XDIST_SPLITTABLE = True\n",
               "tests/test_whole.py": BROWSER_SRC}
    s = _sched(sources=sources)
    split = [f"tests/test_split.py::t{i}" for i in range(7)]
    whole = [f"tests/test_whole.py::C::t{i}" for i in range(4)]
    s.plan_chunks(split + whole)
    scopes = [s._split_scope(n) for n in split]
    assert len(set(scopes)) == HEAVY_CAP
    assert scopes == sorted(scopes)  # contiguous: never back to an earlier chunk
    assert {s._split_scope(n) for n in whole} == {"tests/test_whole.py"}


def test_the_real_flags_and_browser_detection():
    s = _sched()
    assert s._splittable("tests/test_live_thinking_switch.py") is True
    assert s._splittable("tests/test_notify_flash.py") is False  # shares one page per file
    assert s._splittable("tests/test_session_manager.py") is False
    assert s._heavy("tests/test_notify_flash.py") is True
    assert s._heavy("tests/test_session_manager.py") is False


def test_a_first_worker_crash_of_a_test_is_marked_rerun_not_failed():
    """2026-10-28: test_code_pipeline_review.py::test_attest_cli crashed with a
    native Windows access violation inside subprocess._wait on Python 3.14
    ARM64, under 8-worker loadfile parallelism.  LoadScopeScheduling.remove_node
    already re-queues the workload (including the crashed test), so the test
    runs again on another worker; the hook changes the first crash's spurious
    ``failed`` report to ``rerun`` so pytest does not count it as a failure.
    The retry's own report is what stands."""
    from tests import conftest

    class _Report:
        outcome = "failed"
        when = "???"
        failed = True

    # First crash: outcome is flipped to "rerun", so report.failed goes False.
    conftest._crash_retry_count.clear()
    r1 = _Report()
    conftest.pytest_handlecrashitem("tests/test_x.py::t_one", r1, None)
    assert r1.outcome == "rerun"
    assert conftest._crash_retry_count["tests/test_x.py::t_one"] == 1
    # Second crash of the SAME test: leave it as a real failure (no infinite retry).
    r2 = _Report()
    conftest.pytest_handlecrashitem("tests/test_x.py::t_one", r2, None)
    assert r2.outcome == "failed"
    assert conftest._crash_retry_count["tests/test_x.py::t_one"] == 2
    # A DIFFERENT test's first crash is still a rerun.
    r3 = _Report()
    conftest.pytest_handlecrashitem("tests/test_x.py::t_two", r3, None)
    assert r3.outcome == "rerun"


def test_the_crash_retry_can_be_turned_off(monkeypatch):
    """Kill switch for the retry (``VIBENODE_NO_CRASH_RETRY=1``): the hook
    leaves the report as failed so a developer debugging a genuinely-crashing
    test sees it fail on the first run."""
    from tests import conftest

    monkeypatch.setenv("VIBENODE_NO_CRASH_RETRY", "1")

    class _Report:
        outcome = "failed"
        when = "???"
        failed = True

    conftest._crash_retry_count.clear()
    r = _Report()
    conftest.pytest_handlecrashitem("tests/test_x.py::t_off", r, None)
    assert r.outcome == "failed"
    assert "tests/test_x.py::t_off" not in conftest._crash_retry_count


def test_an_xdist_worker_crash_is_actually_retried_end_to_end(tmp_path):
    """Drive the real xdist controller + a worker that crashes on its first
    run of a specific test, and confirm the retry runs on another worker and
    the overall exit code is 0.

    The crashing test uses a file flag so the first run of it hits
    ``os._exit(134)`` (SIGABRT-equivalent on Windows) and the retry finds the
    flag and passes.  Without the ``pytest_handlecrashitem`` hook in
    conftest.py the controller would record the first run's crash report as a
    failure and exit 1 even though the retry passes."""
    import subprocess
    import sys

    (tmp_path / "conftest.py").write_text(
        "import sys\n"
        "sys.path.insert(0, r'" + str(ROOT) + "')\n"
        "from tests.conftest import pytest_handlecrashitem  # noqa: F401\n",
        encoding="utf-8")
    (tmp_path / "test_sub.py").write_text(
        "import os\n"
        "from pathlib import Path\n"
        "FLAG = Path(__file__).parent / 'crashed_once.flag'\n"
        "def test_passes():\n"
        "    assert True\n"
        "def test_crashes_once_then_passes():\n"
        "    if not FLAG.exists():\n"
        "        FLAG.write_text('x')\n"
        "        os._exit(134)  # SIGABRT-equivalent: worker crash, no traceback\n"
        "    assert True\n",
        encoding="utf-8")
    r = subprocess.run(
        [sys.executable, "-m", "pytest", "-p", "xdist", "--dist", "loadfile",
         "-n", "2", "-q", "--no-header", str(tmp_path)],
        cwd=str(tmp_path), capture_output=True, text=True, timeout=120)
    # The retry must have run and the suite must exit clean (0): without the
    # hook, the first crash report would set testsfailed and exit 1.
    assert r.returncode == 0, f"stdout:\n{r.stdout}\nstderr:\n{r.stderr}"
