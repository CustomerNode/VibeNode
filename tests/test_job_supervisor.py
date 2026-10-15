"""Durable Jobs step 3: the supervisor (daemon/job_supervisor.py).

Acceptance tests 1-5 from docs/plans/durable-jobs-spec.md, run against the
real registry and the real decision code, with the process list and the
launch faked (a "daemon restart" is a fresh JobSupervisor over the same file,
whose first tick is the reconcile):

1. Register a job, kill its process, restart the daemon: relaunched once,
   record shows ``relaunched``.
2. Same, with the done marker present: not relaunched, ``finished``.
3. Same, with ``irreversible_after`` matched: not relaunched,
   ``needs_person``, a push arrives.
4. Dies three times in a row with no new output: stops at ``needs_person``;
   dies after progress: relaunched again.
5. A corrupt ``jobs.json``: logged and reported, file untouched, nothing
   launched.

Plus the invisible normal path: the launching session gets one system entry
and nothing is pushed; a dry run changes nothing; a process list that cannot
be read relaunches nothing.
"""

from __future__ import annotations

import logging
import threading
import time
from pathlib import Path

import pytest

from daemon import durable_jobs as dj
from daemon import job_supervisor as js


class FakeInfo:
    def __init__(self):
        self.entries = []
        self._lock = threading.Lock()


class FakeManager:
    def __init__(self):
        self.pushes = []
        self.emitted = []
        self._lock = threading.Lock()
        self._sessions = {"sess-1": FakeInfo()}
        self._push_callback = lambda ev, data: self.pushes.append((ev, data))

    def _emit_entry(self, sid, entry, index):
        self.emitted.append((sid, entry.text, index))


class World:
    """The registry, a fake process table and a fake launcher."""

    def __init__(self, tmp_path):
        self.reg = tmp_path / "jobs.json"
        self.dir = tmp_path
        self.procs = []            # command lines "running"
        self.launches = []         # (command, log_path)
        self.now = 1_000_000.0
        self.sm = FakeManager()

    def launch(self, command, cwd, log_path, env):
        self.launches.append((command, log_path))
        Path(log_path).write_text("")   # a fresh launch log
        return {"ok": True, "pid": 42, "launcher": "", "error": ""}

    def supervisor(self):
        """A 'daemon start': a fresh supervisor whose first tick reconciles."""
        return js.JobSupervisor(self.sm, proc_lister=lambda: self.procs,
                                launch=self.launch, path=self.reg, clock=lambda: self.now)

    def register(self, **over):
        fields = dict(name="authorship read", command="python -u authorship.py --workers 30",
                      cwd=str(self.dir), done_test="AUTHORSHIP_DONE", session_id="sess-1",
                      running_match="*authorship.py*--workers*",
                      job_id="auth", log_path=str(self.dir / "auth-{ts}.log"),
                      progress=r"cells (\d+)/(\d+)", irreversible_after="rebuild launched on donvm")
        fields.update(over)
        return dj.run_job(launch=self.launch, path=self.reg, **fields)

    def job(self):
        return dj.get_job("auth", self.reg)

    def log(self):
        return Path(self.job()["last_log"])

    def write(self, text):
        with self.log().open("a") as fh:
            fh.write(text)


@pytest.fixture
def w(tmp_path):
    return World(tmp_path)


def test_1_a_killed_job_is_relaunched_once_after_a_daemon_restart(w):
    w.register()
    w.procs = ["C:\\py\\python.exe -u authorship.py --tp x --workers 30"]
    w.write("cells 100/200\n")
    assert w.supervisor().tick()[0]["action"] == "alive"
    assert w.job()["state"] == "running" and w.job()["progress_value"]["pct"] == 50.0

    w.procs = []                         # the machine restarted: the process is gone
    sup = w.supervisor()                 # ...and so is the daemon; this is the new one
    w.now += 600
    d = sup.tick()
    assert [x["action"] for x in d] == ["relaunch"]
    assert len(w.launches) == 2, "exactly one relaunch"
    rec = w.job()
    assert rec["state"] == "relaunched" and rec["relaunch_count"] == 1
    assert rec["relaunches_without_progress"] == 1
    # Invisible: no push.  The launching session got ONE system entry.
    assert w.sm.pushes == []
    assert len(w.sm.emitted) == 1
    sid, text, _ = w.sm.emitted[0]
    assert sid == "sess-1" and "relaunched after restart" in text and "cells 100/200" in text
    assert w.sm._sessions["sess-1"].entries[0].kind == "system"
    # It comes back alive; the next tick does not launch again.
    w.procs = ["python -u authorship.py --workers 30"]
    assert sup.tick()[0]["action"] == "alive" and len(w.launches) == 2


def test_2_a_job_with_the_done_marker_is_finished_not_relaunched(w):
    w.register()
    w.write("cells 200/200\nAUTHORSHIP_DONE\n")
    w.procs = []
    assert w.supervisor().tick()[0]["action"] == "finish"
    assert w.job()["state"] == "finished"
    assert len(w.launches) == 1 and w.sm.pushes == [] and w.sm.emitted == []
    # Finished stays finished on later ticks (no evaluation at all).
    assert w.supervisor().tick() == []


def test_3_past_the_irreversible_marker_needs_a_person_and_pushes(w):
    w.register()
    w.write("gates ok\nZ rebuild launched on donvm\n")
    w.procs = []
    d = w.supervisor().tick()
    assert d[0]["action"] == "needs_person" and "irreversible" in d[0]["reason"]
    rec = w.job()
    assert rec["state"] == "needs_person" and len(w.launches) == 1
    assert len(w.sm.pushes) == 1
    ev, data = w.sm.pushes[0]
    assert ev == "job_needs_person"
    assert data["job_id"] == "auth" and data["state"] == "needs_person"
    assert "rebuild launched on donvm" in data["reason"] and data["session_id"] == "sess-1"
    # Asked once: a later tick does not push again or relaunch.
    assert w.supervisor().tick() == [] and len(w.sm.pushes) == 1
    # The irreversible marker wins even over a done-looking run: Relaunch by a
    # person is the only way on, and it launches.
    rec, res = dj.relaunch_job("auth", by="user", launch=w.launch, path=w.reg)
    assert res["ok"] and rec["state"] == "relaunched" and len(w.launches) == 2


def test_4_three_deaths_without_progress_stop_but_progress_keeps_relaunches(w):
    w.register(progress=None)
    w.procs = []
    sup = w.supervisor()
    # Death 1: the original run counted as progress → relaunch #1.
    assert sup.tick()[0]["action"] == "relaunch"
    for expected in (2, 3):
        w.now += 10            # dies at once, writes nothing new
        d = sup.tick()
        assert d[0]["action"] == "relaunch", d
        assert w.job()["relaunches_without_progress"] == expected
    assert w.job()["relaunch_count"] == 3
    w.now += 10
    d = sup.tick()
    assert d[0]["action"] == "needs_person" and "3 times" in d[0]["reason"]
    assert w.job()["state"] == "needs_person" and len(w.launches) == 4
    assert [ev for ev, _ in w.sm.pushes] == ["job_needs_person"]

    # A job that dies after real progress is not broken.
    w2 = World(w.dir / "two")
    w2.dir.mkdir()
    w2.register(job_id="auth", log_path=str(w2.dir / "auth-{ts}.log"))
    w2.procs = []
    sup2 = w2.supervisor()
    for i in range(6):
        w2.now += 600                       # lived 10 min...
        w2.write("cells %d/700\n" % (100 * (i + 1)))   # ...and advanced
        d = sup2.tick()
        assert d[0]["action"] == "relaunch", (i, d)
        assert w2.job()["relaunches_without_progress"] == 1
    assert w2.job()["state"] == "relaunched" and w2.job()["relaunch_count"] == 6
    assert w2.sm.pushes == []


def test_4b_a_crash_traceback_is_output_but_not_progress(w):
    """Without a progress pattern, output from a job that died within two
    minutes of its launch does not count as progress."""
    w.register(progress=None)
    w.procs = []
    sup = w.supervisor()
    sup.tick()
    for _ in range(2):
        w.now += 5
        w.write("Traceback (most recent call last):\n  boom\n")
        assert sup.tick()[0]["action"] == "relaunch"
    w.now += 5
    w.write("Traceback again\n")
    assert sup.tick()[0]["action"] == "needs_person"


def test_5_a_corrupt_registry_is_reported_and_left_untouched(w, caplog):
    w.reg.write_bytes(b'{"jobs": {"x": {"state": "nope"}}}')
    before = w.reg.read_bytes()
    with caplog.at_level(logging.ERROR, logger="daemon.job_supervisor"):
        sup = w.supervisor()
        assert sup.tick() == []
        assert sup.tick() == []
    assert w.reg.read_bytes() == before
    assert w.launches == []
    assert "registry unusable" in caplog.text and "left as is" in caplog.text
    # Reported once per file version, to every open tab.
    assert [ev for ev, _ in w.sm.pushes] == ["job_registry_error"]
    assert w.sm.pushes[0][1]["path"] == str(w.reg)
    w.reg.write_bytes(b"{still broken")
    sup.tick()
    assert len(w.sm.pushes) == 2


def test_non_resumable_jobs_fail_instead_of_relaunching(w):
    w.register(resumable=False, progress=None)
    w.procs = []
    d = w.supervisor().tick()
    assert d[0]["action"] == "fail"
    assert w.job()["state"] == "failed" and len(w.launches) == 1
    assert [ev for ev, _ in w.sm.pushes] == ["job_needs_person"]
    assert w.sm.pushes[0][1]["state"] == "failed"


def test_dry_run_decides_without_changing_anything(w):
    w.register()
    w.procs = []
    before = w.reg.read_bytes()
    d = w.supervisor().tick(dry_run=True)
    assert d[0]["action"] == "relaunch"
    assert w.reg.read_bytes() == before and len(w.launches) == 1
    assert w.sm.pushes == [] and w.sm.emitted == []


def test_an_unreadable_process_list_relaunches_nothing(w):
    w.register()
    sup = js.JobSupervisor(w.sm, proc_lister=lambda: None, launch=w.launch,
                           path=w.reg, clock=lambda: w.now)
    d = sup.tick()
    assert d[0]["action"] == "unknown"
    assert w.job()["state"] == "running" and len(w.launches) == 1
    # ...but a done marker is still honoured.
    w.write("AUTHORSHIP_DONE\n")
    assert sup.tick()[0]["action"] == "finish"


def test_the_thread_runs_the_first_tick_after_start(w, monkeypatch):
    monkeypatch.setattr(js, "FIRST_TICK_SECONDS", 0.05)
    monkeypatch.setattr(js, "TICK_SECONDS", 0.05)
    w.register()
    w.procs = []
    sup = w.supervisor()
    sup.start()
    try:
        deadline = time.time() + 5
        while time.time() < deadline and len(w.launches) < 2:
            time.sleep(0.02)
    finally:
        sup.stop()
    assert len(w.launches) >= 2 and w.job()["state"] == "relaunched"


def test_the_daemon_starts_the_supervisor_beside_the_health_monitor():
    """The wiring in SessionManager.start()/stop() (source check: starting
    a real SessionManager needs the SDK)."""
    src = (Path(__file__).resolve().parents[1] / "daemon" / "session_manager.py").read_text(encoding="utf-8")
    assert "from daemon.job_supervisor import JobSupervisor" in src
    assert "self._job_supervisor.start()" in src
    assert "self._job_supervisor.stop()" in src
