"""The watchdog swaps in new code from disk without an engine restart.

Why (2026-10-20): the Session Engine had run since Oct 6.  Every recovery job
(stranded workers, silent waits, usage-limit resume) was committed after that,
so none of them ever ran, and a session sat stranded 83 minutes in the exact
state job 4 handles.  ``HealthMonitor._maybe_hot_reload`` now reloads the
watchdog modules while the engine keeps running.

The real reload runs in a subprocess against a COPY of the watchdog package,
so reloading never replaces modules inside this test process (other tests
hold references to the real classes).
"""

import shutil
import subprocess
import sys
import textwrap
import threading
from pathlib import Path

import pytest

from daemon import health_monitor as hm

REPO = Path(__file__).resolve().parents[1]
WATCHDOG_FILES = ("__init__.py", "health_monitor.py", "orphaned_workers.py",
                  "silent_wait.py", "limit_watch.py", "engine_code.py")


class _FakeSM:
    def __init__(self):
        self._lock = threading.Lock()
        self._sessions = {}
        self._health_monitor = None


# ── State hand-over ───────────────────────────────────────────────────────

def test_new_monitor_adopts_act_once_state_but_not_caches_or_threads():
    sm = _FakeSM()
    old = hm.HealthMonitor(sm)
    old._orphan_nudged = {("s1", "n1", 1.0)}
    old._silent_escalated = {"s1": 5.0}
    old._restarts = {"s2": 1}
    old._orphan_caches = {"s1": object()}
    old._limit_stop_cache = {"p": object()}
    old._thread = object()
    new = hm.HealthMonitor(sm)
    new._adopt_state(old)
    # Act-once bookkeeping survives, so nothing is nudged twice.
    assert new._orphan_nudged == {("s1", "n1", 1.0)}
    assert new._silent_escalated == {"s1": 5.0}
    assert new._restarts == {"s2": 1}
    # Caches of the old modules' classes and thread plumbing do not.
    assert new._orphan_caches == {}
    assert new._limit_stop_cache == {}
    assert new._thread is None
    assert new._stop_event is not old._stop_event


def test_adopt_skips_a_field_whose_type_changed():
    sm = _FakeSM()
    old = hm.HealthMonitor(sm)
    old._silent_nudged = ["an", "older", "shape"]   # was a list in "v1"
    new = hm.HealthMonitor(sm)
    new._adopt_state(old)
    assert new._silent_nudged == {}


def test_one_failing_job_does_not_silence_the_others(monkeypatch):
    # Hot reload can pair new watchdog code with an older SessionManager; a
    # job that breaks on that must not stop the other jobs or other sessions.
    from types import SimpleNamespace
    sm = _FakeSM()
    sm._sessions = {s: SimpleNamespace(session_id=s, state="idle") for s in ("a", "b")}
    m = hm.HealthMonitor(sm)
    ran = []

    def broken(info, now):
        raise AttributeError("SessionManager has no attribute _new_thing")
    monkeypatch.setattr(m, "_check_stranded_workers", broken)
    monkeypatch.setattr(m, "_check_silent_wait", lambda info, now: ran.append(info.session_id))
    monkeypatch.setattr(m, "_limit_backstop", lambda now, sessions: ran.append("job6"))
    m.tick()
    assert ran == ["a", "b", "job6"]


# ── Gates (no reload happens) ─────────────────────────────────────────────

def test_unchanged_code_does_not_reload(monkeypatch):
    m = hm.HealthMonitor(_FakeSM())
    called = []
    monkeypatch.setattr(hm, "_trial_import", lambda: called.append(1) or (True, ""))
    assert m._maybe_hot_reload() is False
    assert called == []


def test_code_still_being_edited_is_not_loaded(monkeypatch):
    m = hm.HealthMonitor(_FakeSM())
    now_ns = int(hm.time.time() * 1e9)
    monkeypatch.setattr(hm, "watchdog_code_mtimes",
                        lambda: {k: now_ns for k in m._code_mtimes})
    called = []
    monkeypatch.setattr(hm, "_trial_import", lambda: called.append(1) or (True, ""))
    assert m._maybe_hot_reload() is False
    assert called == []


def test_code_that_does_not_import_is_skipped_once_and_not_retried(monkeypatch, caplog):
    m = hm.HealthMonitor(_FakeSM())
    old_ns = int((hm.time.time() - 600) * 1e9)
    changed = {k: old_ns for k in m._code_mtimes}
    monkeypatch.setattr(hm, "watchdog_code_mtimes", lambda: changed)
    calls = []
    monkeypatch.setattr(hm, "_trial_import",
                        lambda: calls.append(1) or (False, "SyntaxError: boom"))
    assert m._maybe_hot_reload() is False
    assert "does not import cleanly" in caplog.text
    assert m._maybe_hot_reload() is False
    assert calls == [1]                       # the broken version is not retried


def test_trial_import_of_the_real_code_succeeds():
    ok, detail = hm._trial_import()
    assert ok, detail


# ── The real reload, end to end, in a copy of the package ────────────────

_DRIVER = textwrap.dedent(r'''
    import os, sys, threading, time
    sys.path.insert(0, os.getcwd())
    # The copies keep the repo's mtimes; a file edited moments ago would
    # (correctly) be "still being edited".  Start from settled files.
    for name in os.listdir("daemon"):
        t = time.time() - 3600
        os.utime(os.path.join("daemon", name), (t, t))
    from daemon import health_monitor as hm
    from daemon import silent_wait

    class SM:
        def __init__(self):
            self._lock = threading.Lock()
            self._sessions = {}
            self._health_monitor = None

    sm = SM()
    old = hm.HealthMonitor(sm)
    sm._health_monitor = old
    old._orphan_nudged.add(("sid", "nested", 1.0))
    old_cls = hm.HealthMonitor

    def age(path, seconds):
        t = time.time() - seconds
        os.utime(path, (t, t))

    sw = os.path.join("daemon", "silent_wait.py")
    mode = sys.argv[1]
    if mode == "broken":
        with open(sw, "a") as fh:
            fh.write("\nthis is not python\n")
    else:
        with open(sw, "a") as fh:
            fh.write("\nHOT_RELOAD_MARK = 42\n")
    age(sw, 300)

    took_over = old._maybe_hot_reload()
    new = sm._health_monitor
    print("took_over", took_over)
    print("swapped", new is not old)
    print("new_class", type(new) is not old_cls)
    print("mark", getattr(sys.modules["daemon.silent_wait"], "HOT_RELOAD_MARK", None))
    print("adopted", ("sid", "nested", 1.0) in new._orphan_nudged)
    print("thread", bool(new._thread and new._thread.is_alive()))
    if new is not old:
        new.stop()
''')


def _run_driver(tmp_path, mode):
    pkg = tmp_path / "daemon"
    pkg.mkdir()
    for name in WATCHDOG_FILES:
        shutil.copy2(REPO / "daemon" / name, pkg / name)
    driver = tmp_path / "driver.py"
    driver.write_text(_DRIVER, encoding="utf-8")
    proc = subprocess.run([sys.executable, str(driver), mode], cwd=str(tmp_path),
                          capture_output=True, text=True, timeout=120)
    assert proc.returncode == 0, proc.stderr
    return dict(line.split(" ", 1) for line in proc.stdout.strip().splitlines())


def test_new_code_on_disk_goes_live_without_a_restart(tmp_path):
    out = _run_driver(tmp_path, "good")
    assert out["took_over"] == "True"
    assert out["swapped"] == "True"          # the engine now points at a new monitor
    assert out["new_class"] == "True"        # built from the reloaded module
    assert out["mark"] == "42"               # the edited detector is live
    assert out["adopted"] == "True"          # nothing already nudged is nudged again
    assert out["thread"] == "True"           # and it is running


def test_broken_code_on_disk_leaves_the_running_watchdog_in_place(tmp_path):
    out = _run_driver(tmp_path, "broken")
    assert out["took_over"] == "False"
    assert out["swapped"] == "False"
    assert out["mark"] == "None"
