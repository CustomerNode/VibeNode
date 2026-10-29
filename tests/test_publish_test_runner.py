"""Publish-gate test runner (app/routes/test_api.py), added 2026-10-01.

Covers the three changes behind "the Publish test run is slow, its errors
vanish, and there is no way to hand them to Claude":
  * parallel workers when pytest-xdist is installed (serial fallback),
  * exact pass/fail counts from the JUnit report,
  * the full output saved to logs/last_test_run.log for the "Fix errors" session.
"""

import json
import sys

import pytest
from flask import Flask

import app.routes.test_api as ta


JUNIT_2_FAIL = """<?xml version="1.0" encoding="utf-8"?>
<testsuites><testsuite name="pytest" errors="1" failures="2" skipped="3" tests="20" time="1.0"></testsuite></testsuites>
"""


@pytest.fixture
def paths(tmp_path, monkeypatch):
    monkeypatch.setattr(ta, "LAST_RUN_LOG", tmp_path / "last_test_run.log")
    monkeypatch.setattr(ta, "LAST_RUN_JUNIT", tmp_path / "last_test_run.xml")
    return tmp_path


class TestParallelArgs:
    def test_uses_xdist_when_installed(self, monkeypatch):
        monkeypatch.setitem(sys.modules, "xdist", object())
        monkeypatch.setattr("os.cpu_count", lambda: 12)
        assert ta._parallel_args() == ["-n", "8", "--dist", "loadfile"]

    def test_worker_count_bounds(self, monkeypatch):
        monkeypatch.setitem(sys.modules, "xdist", object())
        monkeypatch.setattr("os.cpu_count", lambda: 2)
        assert ta._parallel_args()[:2] == ["-n", "2"]
        monkeypatch.setattr("os.cpu_count", lambda: 64)
        assert ta._parallel_args()[:2] == ["-n", "8"]

    def test_serial_fallback_without_xdist(self, monkeypatch):
        monkeypatch.setitem(sys.modules, "xdist", None)  # import raises
        assert ta._parallel_args() == []


class TestConsolePython:
    """The web server runs under pythonw; tests must run under python.exe."""

    def test_swaps_pythonw_for_sibling_python(self, tmp_path, monkeypatch):
        (tmp_path / "pythonw.exe").write_text("")
        (tmp_path / "python.exe").write_text("")
        monkeypatch.setattr(ta.sys, "executable", str(tmp_path / "pythonw.exe"))
        assert ta._console_python() == str(tmp_path / "python.exe")

    def test_keeps_pythonw_if_no_sibling(self, tmp_path, monkeypatch):
        (tmp_path / "pythonw.exe").write_text("")
        monkeypatch.setattr(ta.sys, "executable", str(tmp_path / "pythonw.exe"))
        assert ta._console_python() == str(tmp_path / "pythonw.exe")

    def test_leaves_python_alone(self, tmp_path, monkeypatch):
        monkeypatch.setattr(ta.sys, "executable", str(tmp_path / "python.exe"))
        assert ta._console_python() == str(tmp_path / "python.exe")


class TestJunitCounts:
    def test_exact_counts(self, paths):
        ta.LAST_RUN_JUNIT.write_text(JUNIT_2_FAIL, encoding="utf-8")
        assert ta._junit_counts() == {"passed": 14, "failed": 2, "errors": 1, "skipped": 3}

    def test_missing_or_corrupt_report_is_none(self, paths):
        assert ta._junit_counts() is None
        ta.LAST_RUN_JUNIT.write_text("<not xml", encoding="utf-8")
        assert ta._junit_counts() is None


class TestSaveRunLog:
    def test_writes_header_and_every_line(self, paths):
        p = ta._save_run_log("full", ["python", "-m", "pytest"], ["a", "FAILED x"], 1)
        text = ta.LAST_RUN_LOG.read_text(encoding="utf-8")
        assert p == str(ta.LAST_RUN_LOG)
        assert "mode=full, exit=1" in text and "FAILED x" in text

    def test_lives_under_gitignored_logs(self):
        # The real path must stay inside logs/ (gitignored): run output can
        # contain local paths and must never be published.
        assert ta.LAST_RUN_LOG.parent.name == "logs"
        gitignore = (ta._REPO_ROOT / ".gitignore").read_text(encoding="utf-8")
        assert "logs/" in gitignore.splitlines()


class _FakeProc:
    def __init__(self, lines, rc, junit_path):
        self.stdout = iter(l + "\n" for l in lines)
        self.returncode = rc
        self.pid = 4242
        junit_path.write_text(JUNIT_2_FAIL, encoding="utf-8")

    def poll(self):
        return self.returncode

    def wait(self, timeout=None):
        return self.returncode

    def terminate(self):
        pass

    def kill(self):
        pass


class TestEndpoint:
    def test_failed_run_reports_exact_counts_and_log(self, paths, monkeypatch):
        captured = {}
        real_popen = ta.subprocess.Popen

        def fake_popen(cmd, *args, **kw):
            # ta.subprocess IS the global subprocess module, so this patch is
            # process-wide.  Background threads left by other test files in
            # the same xdist worker (e.g. app.git_ops's git refresh) can call
            # Popen mid-request; intercept only the pytest launch under test.
            if not (isinstance(cmd, list) and "pytest" in cmd):
                return real_popen(cmd, *args, **kw)
            captured["cmd"] = cmd
            captured["kw"] = kw
            return _FakeProc(["..F.", "FAILED tests/test_x.py::test_y - boom"], 1, ta.LAST_RUN_JUNIT)

        monkeypatch.setattr(ta.subprocess, "Popen", fake_popen)
        app = Flask(__name__)
        app.register_blueprint(ta.bp)
        resp = app.test_client().post("/api/run-tests", json={"mode": "full"})
        events = [json.loads(chunk[len("data: "):])
                  for chunk in resp.get_data(as_text=True).split("\n\n")
                  if chunk.startswith("data: ")]
        done = events[-1]
        assert done["type"] == "done" and done["ok"] is False
        # Exact counts from the JUnit report, not the dot count.
        assert (done["passed"], done["failed"], done["errors"], done["skipped"]) == (14, 2, 1, 3)
        assert done["log_path"] == str(ta.LAST_RUN_LOG)
        assert "FAILED tests/test_x.py::test_y - boom" in ta.LAST_RUN_LOG.read_text(encoding="utf-8")
        assert "--junitxml" in captured["cmd"]
        # pythonw has no valid stdin; inheriting it breaks every subprocess a
        # test starts ("[WinError 6] The handle is invalid", 9 failures).
        assert captured["kw"].get("stdin") is ta.subprocess.DEVNULL


# ---------------------------------------------------------------------------
# The run belongs to the server, not to the stream (2026-10-18)
#
# A phone that locked its screen 41s into a Publish dropped the stream; the
# server took that as "client gone", killed the run, and the dialog (no
# "done" event) showed "running" for 20 minutes.  Now a dropped stream only
# detaches: the run continues and a stream can reattach from where it was.
# ---------------------------------------------------------------------------
import threading as _threading


class _GatedProc:
    """A pytest stand-in whose output arrives only as the test releases it."""

    def __init__(self, junit_path, rc=0):
        self._q = []
        self._cv = _threading.Condition()
        self._closed = False
        self.returncode = None
        self._rc = rc
        self.pid = 4343
        self.terminated = False
        self._junit = junit_path

    def emit(self, line):
        with self._cv:
            self._q.append(line + "\n")
            self._cv.notify_all()

    def finish(self):
        with self._cv:
            self._junit.write_text(JUNIT_2_FAIL, encoding="utf-8")
            self._closed = True
            self._cv.notify_all()

    @property
    def stdout(self):
        def gen():
            while True:
                with self._cv:
                    while not self._q and not self._closed:
                        self._cv.wait(5)
                    if self._q:
                        item = self._q.pop(0)
                    else:
                        self.returncode = self._rc
                        return
                yield item
        return gen()

    def poll(self):
        return self.returncode

    def wait(self, timeout=None):
        return self.returncode

    def terminate(self):
        self.terminated = True
        self._rc = 1          # a terminated pytest exits non-zero
        self.finish()

    def kill(self):
        self.terminate()


@pytest.fixture
def gated(paths, monkeypatch):
    procs = []
    real_popen = ta.subprocess.Popen

    def fake_popen(cmd, *args, **kw):
        if not (isinstance(cmd, list) and "pytest" in cmd):
            return real_popen(cmd, *args, **kw)
        p = _GatedProc(ta.LAST_RUN_JUNIT)
        procs.append(p)
        return p

    monkeypatch.setattr(ta.subprocess, "Popen", fake_popen)
    monkeypatch.setattr(ta, "_test_proc", None)
    monkeypatch.setattr(ta, "_run", None)
    monkeypatch.setattr(ta, "KEEPALIVE_S", 0.2)
    app = Flask(__name__)
    app.register_blueprint(ta.bp)
    yield app.test_client(), procs
    for p in procs:
        p.finish()
    # Wait for the run's pump thread to write its result BEFORE monkeypatch
    # restores LAST_RUN_LOG: a late write landed in the real
    # logs/last_test_run.log (seen 2026-10-18).
    run = ta._run
    if run is not None:
        with ta._run_cond:
            ta._run_cond.wait_for(lambda: run["done"] is not None, timeout=10)
        assert run["done"] is not None, "test run pump never finished"


def _events(chunks):
    out = []
    for chunk in "".join(chunks).split("\n\n"):
        if chunk.startswith("data: "):
            out.append(json.loads(chunk[len("data: "):]))
    return out


def _read_until(it, pred, limit=200):
    got = []
    for _ in range(limit):
        got.append(next(it).decode() if isinstance(got, list) else None)
        if pred(_events(got)):
            return got
    raise AssertionError("expected event never arrived")


class TestRunSurvivesDisconnect:
    def test_a_dropped_stream_does_not_stop_the_run_and_a_reattach_gets_the_rest(self, gated):
        client, procs = gated
        resp = client.post("/api/run-tests", json={"mode": "full"}, buffered=False)
        it = resp.response
        got = _read_until(it, lambda ev: any(e["type"] == "run" for e in ev))
        run_id = next(e for e in _events(got) if e["type"] == "run")["run_id"]
        procs[0].emit("..F.")
        got = _read_until(it, lambda ev: any(e.get("line") == "..F." for e in ev))
        resp.close()                       # the phone locks its screen
        assert not procs[0].terminated, "a dropped connection must not stop the run"

        procs[0].emit("FAILED tests/test_x.py::test_y - boom")
        procs[0].finish()                  # the run ends while nobody watches

        again = client.post("/api/run-tests", json={"mode": "full", "run_id": run_id, "since": 1})
        ev = _events([again.get_data(as_text=True)])
        assert ev[0] == {"type": "run", "run_id": run_id, "mode": "full",
                         "mode_label": ta.MODES["full"]["label"]}
        assert [e["line"] for e in ev if e["type"] == "line"] == ["FAILED tests/test_x.py::test_y - boom"]
        done = ev[-1]
        assert done["type"] == "done" and done["run_id"] == run_id
        assert (done["passed"], done["failed"]) == (14, 2)

    def test_reattaching_to_an_unknown_run_says_it_is_lost(self, gated):
        client, _ = gated
        ev = _events([client.post("/api/run-tests", json={"mode": "full", "run_id": "nope"}).get_data(as_text=True)])
        assert ev == [{"type": "error", "line": "That test run is no longer available.", "lost": True}]

    def test_a_second_publish_attaches_to_the_run_in_progress(self, gated):
        client, procs = gated
        first = client.post("/api/run-tests", json={"mode": "full"}, buffered=False)
        got = _read_until(first.response, lambda ev: any(e["type"] == "run" for e in ev))
        run_id = _events(got)[0]["run_id"]
        second = client.post("/api/run-tests", json={"mode": "full"}, buffered=False)
        got2 = _read_until(second.response, lambda ev: any(e["type"] == "run" for e in ev))
        assert _events(got2)[0]["run_id"] == run_id and len(procs) == 1
        # A Sync asking for fast tests joins the full run (full covers fast)
        # instead of being refused with nothing on screen (2026-10-28).
        third = client.post("/api/run-tests", json={"mode": "fast"}, buffered=False)
        got3 = _read_until(third.response, lambda ev: any(e["type"] == "run" for e in ev))
        ev3 = _events(got3)[0]
        assert ev3["run_id"] == run_id and ev3["mode"] == "full" and len(procs) == 1
        first.close(); second.close(); third.close()

    def test_a_full_request_during_a_fast_run_is_refused_as_busy(self, gated):
        client, procs = gated
        first = client.post("/api/run-tests", json={"mode": "fast"}, buffered=False)
        _read_until(first.response, lambda ev: any(e["type"] == "run" for e in ev))
        other = client.post("/api/run-tests", json={"mode": "full"})
        ev = _events([other.get_data(as_text=True)])[0]
        assert ev["type"] == "error" and ev["busy"] is True and "fast" in ev["line"]
        assert len(procs) == 1
        first.close()

    def test_cancel_still_stops_the_run_and_says_so(self, gated):
        client, procs = gated
        resp = client.post("/api/run-tests", json={"mode": "full"}, buffered=False)
        got = _read_until(resp.response, lambda ev: any(e["type"] == "run" for e in ev))
        run_id = _events(got)[0]["run_id"]
        resp.close()
        assert client.post("/api/cancel-tests").get_json()["ok"] is True
        assert procs[0].terminated
        ev = _events([client.post("/api/run-tests", json={"mode": "full", "run_id": run_id}).get_data(as_text=True)])
        assert ev[-1]["type"] == "done" and ev[-1]["cancelled"] is True and ev[-1]["ok"] is False
