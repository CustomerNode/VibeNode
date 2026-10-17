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


def test_browser_work_is_capped_and_a_worker_is_never_left_idle():
    """8 workers on Chromium at once starved the machine.  Past the cap a
    worker gets non-browser work; with only browser work left it still gets
    some, or the run would hang."""
    sources = {f"tests/test_b{i}.py": BROWSER_SRC for i in range(HEAVY_CAP + 1)}
    sources["tests/test_plain.py"] = "def test_x(): pass\n"
    s = _sched(sources=sources)
    busy = [object() for _ in range(HEAVY_CAP)]
    for i, n in enumerate(busy):
        s.assigned_work[n] = {f"tests/test_b{i}.py": {f"tests/test_b{i}.py::a": False}}
    node = object()
    s.assigned_work[node] = {}
    last = f"tests/test_b{HEAVY_CAP}.py"
    s.workqueue[last] = {last + "::a": False}
    s.workqueue["tests/test_plain.py"] = {"tests/test_plain.py::x": False}
    assert s.pick_unit(node) == "tests/test_plain.py"
    del s.workqueue["tests/test_plain.py"]
    assert s.pick_unit(node) == last
    # Below the cap, browser work is taken in queue order.
    s.assigned_work[busy[0]] = {}
    s.workqueue["tests/test_plain.py"] = {"tests/test_plain.py::x": False}
    assert s.pick_unit(node) == last


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
