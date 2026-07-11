"""Tests for daemon/health_monitor.py — stall detection and auto-restart.

The monitor thread itself is trivial (wait/tick loop); these tests drive
``HealthMonitor.tick()`` directly against a fake SessionManager so every
decision branch is exercised deterministically without real sleeps.
"""

import threading
import time

import pytest

from daemon.health_monitor import (
    HealthMonitor,
    MAX_AUTO_RESTARTS,
    NUDGE_TEXT,
    STALL_AFTER_SECONDS,
)
from daemon.session_manager import SessionState


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------

class FakeEntry:
    def __init__(self, text=""):
        self.text = text
        self.timestamp = time.time()


class FakeInfo:
    def __init__(self, session_id, state=SessionState.WORKING):
        self.session_id = session_id
        self.state = state
        self.entries = [FakeEntry("hello")]
        self.working_since = time.time()
        self.substatus = ""
        self._wakeup_pending = False
        self._lock = threading.Lock()


class FakeQueue:
    def __init__(self):
        self.queues = {}

    def get_queue_data(self, sid):
        return self.queues.get(sid)


class FakeManager:
    def __init__(self):
        self._lock = threading.Lock()
        self._sessions = {}
        self._mq = FakeQueue()
        self.interrupts = []
        self.sent = []
        self.emitted = []

    def interrupt_session(self, sid, clear_queue=True):
        self.interrupts.append((sid, clear_queue))
        info = self._sessions.get(sid)
        if info:
            info.state = SessionState.IDLE
        return {"ok": True}

    def send_message(self, sid, text):
        self.sent.append((sid, text))
        info = self._sessions.get(sid)
        if info:
            info.state = SessionState.WORKING
            info.working_since = time.time()
        return {"ok": True}

    def _emit_entry(self, sid, entry, index):
        self.emitted.append((sid, entry, index))


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def make_stalled(monitor, sm, sid):
    """Run one tick to record the fingerprint, then age it past the
    stall threshold so the next tick sees a stall."""
    monitor.tick()
    fp, _ts = monitor._progress[sid]
    monitor._progress[sid] = (fp, time.time() - STALL_AFTER_SECONDS - 5)
    sm._sessions[sid].working_since = time.time() - STALL_AFTER_SECONDS - 5


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

def test_healthy_working_session_not_restarted():
    sm = FakeManager()
    sm._sessions["s1"] = FakeInfo("s1")
    mon = HealthMonitor(sm)

    mon.tick()
    # Entry list keeps growing between ticks = healthy progress
    sm._sessions["s1"].entries.append(FakeEntry("more"))
    mon.tick()

    assert sm.interrupts == []
    assert sm.sent == []


def test_stalled_session_interrupted_and_nudged():
    sm = FakeManager()
    sm._sessions["s1"] = FakeInfo("s1")
    mon = HealthMonitor(sm)

    make_stalled(mon, sm, "s1")
    mon.tick()

    assert sm.interrupts == [("s1", False)]  # queue preserved
    assert sm.sent == [("s1", NUDGE_TEXT)]
    assert mon._restarts["s1"] == 1
    # A visible system entry was appended and pushed to clients
    assert any(e.kind == "system" for _, e, _ in sm.emitted)


def test_stalled_session_with_queue_gets_no_nudge():
    sm = FakeManager()
    sm._sessions["s1"] = FakeInfo("s1")
    sm._mq.queues["s1"] = {"messages": ["queued work"]}
    mon = HealthMonitor(sm)

    make_stalled(mon, sm, "s1")
    mon.tick()

    # Interrupt happened, but the queued message IS the continuation
    assert sm.interrupts == [("s1", False)]
    assert sm.sent == []


def test_restart_attempts_capped_then_gives_up():
    sm = FakeManager()
    info = FakeInfo("s1")
    sm._sessions["s1"] = info
    mon = HealthMonitor(sm)

    for attempt in range(MAX_AUTO_RESTARTS):
        info.state = SessionState.WORKING
        make_stalled(mon, sm, "s1")
        mon.tick()
        assert mon._restarts["s1"] == attempt + 1

    # One more stall: give up — interrupt but do NOT nudge again
    info.state = SessionState.WORKING
    nudges_before = len(sm.sent)
    make_stalled(mon, sm, "s1")
    mon.tick()

    assert len(sm.sent) == nudges_before          # no new nudge
    assert len(sm.interrupts) == MAX_AUTO_RESTARTS + 1
    assert any("manual review" in e.text for _, e, _ in sm.emitted)


def test_idle_observation_resets_restart_count():
    sm = FakeManager()
    info = FakeInfo("s1")
    sm._sessions["s1"] = info
    mon = HealthMonitor(sm)
    mon._restarts["s1"] = MAX_AUTO_RESTARTS

    info.state = SessionState.IDLE
    mon.tick()

    assert "s1" not in mon._restarts


def test_wakeup_pending_session_never_stalls():
    sm = FakeManager()
    info = FakeInfo("s1")
    info._wakeup_pending = True
    sm._sessions["s1"] = info
    mon = HealthMonitor(sm)

    # Inject an aged stall record directly — wakeup-pending sessions never
    # even accumulate one, so make_stalled() can't be used here.
    mon._progress["s1"] = (mon._fingerprint(info), time.time() - STALL_AFTER_SECONDS - 5)
    info.working_since = time.time() - STALL_AFTER_SECONDS - 5
    mon.tick()

    assert sm.interrupts == []
    assert sm.sent == []
    assert "s1" not in mon._progress  # stall clock dropped, not aged


def test_compacting_session_not_auto_restarted():
    sm = FakeManager()
    info = FakeInfo("s1")
    info.substatus = "compacting"
    sm._sessions["s1"] = info
    mon = HealthMonitor(sm)

    make_stalled(mon, sm, "s1")
    mon.tick()

    assert sm.interrupts == []
    assert sm.sent == []


def test_wake_from_sleep_resets_stall_clocks():
    sm = FakeManager()
    sm._sessions["s1"] = FakeInfo("s1")
    mon = HealthMonitor(sm)

    make_stalled(mon, sm, "s1")
    # Wake tick: clocks reset, so no restart fires even though the
    # fingerprint has been frozen past the threshold
    mon.tick(woke_from_sleep=True, sleep_gap=3600.0)

    assert sm.interrupts == []
    assert sm.sent == []
    # But if nothing moves for another full window, the stall fires
    fp, _ts = mon._progress["s1"]
    mon._progress["s1"] = (fp, time.time() - STALL_AFTER_SECONDS - 5)
    mon.tick()
    assert sm.interrupts == [("s1", False)]


def test_in_place_stream_growth_counts_as_progress():
    sm = FakeManager()
    info = FakeInfo("s1")
    sm._sessions["s1"] = info
    mon = HealthMonitor(sm)

    mon.tick()
    # Same entry count, but the trailing entry's text grew (streaming)
    info.entries[-1].text += " streamed more text"
    fp, _ts = mon._progress["s1"]
    mon._progress["s1"] = (fp, time.time() - STALL_AFTER_SECONDS - 5)
    info.working_since = time.time() - STALL_AFTER_SECONDS - 5
    mon.tick()

    # Fingerprint changed => clock refreshed, no restart
    assert sm.interrupts == []


def test_removed_sessions_pruned_from_bookkeeping():
    sm = FakeManager()
    sm._sessions["s1"] = FakeInfo("s1")
    mon = HealthMonitor(sm)
    mon.tick()
    assert "s1" in mon._progress

    del sm._sessions["s1"]
    mon._restarts["s1"] = 1
    mon.tick()

    assert "s1" not in mon._progress
    assert "s1" not in mon._restarts
