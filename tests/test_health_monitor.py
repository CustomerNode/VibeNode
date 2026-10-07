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


def test_phantom_working_state_corrected_without_nudge():
    """A session flipped WORKING by post-sleep transport chatter — no entry
    appended since the flip — is corrected to IDLE quietly: interrupt to fix
    the state, but NO nudge message and NO visible announce (2026-09-29)."""
    sm = FakeManager()
    info = FakeInfo("s1")
    sm._sessions["s1"] = info
    mon = HealthMonitor(sm)

    # Phantom discriminator: the WORKING flip happened AFTER the last entry
    # was written (no turn content since the flip). Age the entry BEFORE the
    # first tick — the fingerprint includes its timestamp, so mutating it
    # between ticks would read as progress and reset the stall clock.
    info.working_since = time.time() - STALL_AFTER_SECONDS - 5
    info.entries[-1].timestamp = info.working_since - 3600
    mon.tick()
    fp, _ts = mon._progress["s1"]
    mon._progress["s1"] = (fp, time.time() - STALL_AFTER_SECONDS - 5)
    mon.tick()

    assert sm.interrupts == [("s1", False)]  # state corrected, queue kept
    assert sm.sent == []                     # the whole point: no nudge
    assert sm.emitted == []                  # no visible watchdog announce
    assert "s1" not in mon._restarts         # no restart budget consumed


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


# ---------------------------------------------------------------------------
# Job 4: stranded-worker recovery (daemon/orphaned_workers.py)
#
# An IDLE session whose background worker stopped to wait for its own nested
# agent, whose result Claude Code delivered to the session instead.  Nothing
# will ever wake the worker (incident 2026-10-06, 343 + 317 minutes lost).
# The file-level detection is covered in tests/test_orphaned_workers.py; these
# tests pin WHEN the monitor acts on it.
# ---------------------------------------------------------------------------

from daemon import health_monitor as hm  # noqa: E402
from daemon.health_monitor import (  # noqa: E402
    ORPHAN_IDLE_GRACE_SECONDS,
    ORPHAN_RESCAN_SECONDS,
)
from tests.test_orphaned_workers import SID, SessionFiles, _incident  # noqa: E402


class FakeStore:
    def __init__(self, path):
        self.path = path

    def find_session_path(self, sid, cwd=""):
        return self.path if sid == SID else None


def _stranded_setup(tmp_path, incident=True):
    files = SessionFiles(tmp_path)
    if incident:
        _incident(files)
    sm = FakeManager()
    sm._store = FakeStore(files.root)
    info = FakeInfo(SID, state=SessionState.IDLE)
    info.cwd = "C:/proj"
    info.created_ts = 0.0          # daemon has owned the session all along
    info._interrupted = False
    sm._sessions[SID] = info
    return files, sm, info, HealthMonitor(sm)


def _age_idle(mon, sid=SID):
    mon._idle_since[sid] = time.time() - ORPHAN_IDLE_GRACE_SECONDS - 1


def test_stranded_worker_nudged_once_after_grace(tmp_path):
    files, sm, info, mon = _stranded_setup(tmp_path)

    mon.tick()                                   # IDLE, but inside the grace window
    assert sm.sent == []

    _age_idle(mon)
    mon.tick()
    assert len(sm.sent) == 1
    sid, text = sm.sent[0]
    assert sid == SID
    assert text.startswith("[VibeNode watchdog]")
    assert "w1" in text and "r1" in text and "SendMessage" in text
    # The visible announce names the stuck worker
    assert any("Fix webhook + SSO findings" in e.text for _, e, _ in sm.emitted)

    # The nudged turn runs (WORKING), ends IDLE again, the transcript grows,
    # but the worker is still stranded on the SAME result: never nudge twice.
    mon.tick()                                   # observed WORKING: episode reset
    info.state = SessionState.IDLE
    files.root_note(time.time())
    mon.tick()
    _age_idle(mon)
    mon.tick()
    assert len(sm.sent) == 1


def test_user_stopped_session_is_left_alone(tmp_path):
    _files, sm, info, mon = _stranded_setup(tmp_path)
    info._interrupted = True                     # user pressed Stop
    mon.tick()
    _age_idle(mon)
    mon.tick()
    assert sm.sent == []


def test_session_with_queued_message_is_left_alone(tmp_path):
    _files, sm, _info, mon = _stranded_setup(tmp_path)
    sm._mq.queues[SID] = {"messages": ["queued work"]}
    mon.tick()
    _age_idle(mon)
    mon.tick()
    assert sm.sent == []


def test_working_turn_restarts_idle_grace(tmp_path):
    _files, sm, info, mon = _stranded_setup(tmp_path)
    mon.tick()
    _age_idle(mon)
    info.state = SessionState.WORKING            # a turn started before the check
    mon.tick()
    assert SID not in mon._idle_since
    info.state = SessionState.IDLE
    mon.tick()                                   # fresh episode: grace starts over
    assert sm.sent == []


def test_result_from_before_daemon_took_session_is_ignored(tmp_path):
    _files, sm, info, mon = _stranded_setup(tmp_path)
    info.created_ts = time.time()                # session (re)loaded after the incident
    mon.tick()
    _age_idle(mon)
    mon.tick()
    assert sm.sent == []


def test_stranded_recovery_can_be_disabled(tmp_path, monkeypatch):
    monkeypatch.setattr(hm, "ORPHAN_RECOVERY", False)
    _files, sm, _info, mon = _stranded_setup(tmp_path)
    mon.tick()
    _age_idle(mon)
    mon.tick()
    assert sm.sent == []


def test_stranded_check_throttled_while_idle(tmp_path, monkeypatch):
    calls = []
    real = hm.find_orphaned_workers

    def counting(*a, **k):
        calls.append(1)
        return real(*a, **k)

    monkeypatch.setattr(hm, "find_orphaned_workers", counting)
    _files, sm, _info, mon = _stranded_setup(tmp_path, incident=False)
    mon.tick()
    _age_idle(mon)
    mon.tick()
    mon.tick()
    assert len(calls) == 1                       # second tick inside the rescan window
    mon._orphan_checked_at[SID] -= ORPHAN_RESCAN_SECONDS + 1
    mon.tick()
    assert len(calls) == 2
    assert sm.sent == []


def test_missing_store_is_harmless():
    sm = FakeManager()                           # no _store attribute at all
    sm._sessions["s1"] = FakeInfo("s1", state=SessionState.IDLE)
    mon = HealthMonitor(sm)
    mon.tick()
    _age_idle(mon, "s1")
    mon.tick()
    assert sm.sent == []


def test_stranded_bookkeeping_pruned_for_removed_sessions(tmp_path):
    _files, sm, _info, mon = _stranded_setup(tmp_path)
    mon.tick()
    _age_idle(mon)
    mon.tick()
    assert mon._orphan_nudged and SID in mon._orphan_caches

    del sm._sessions[SID]
    mon.tick()
    assert SID not in mon._idle_since
    assert SID not in mon._orphan_checked_at
    assert SID not in mon._orphan_caches
    assert SID not in mon._transcript_paths
    assert not mon._orphan_nudged
