"""
Tests for daemon/session_registry.py -- persistent session registry for
crash recovery.

This is CRITICAL crash-recovery code.  SessionRegistry saves active
session state to disk so that if the daemon crashes, it can recover
sessions that were mid-task.  A bug here could:

- Lose all active sessions on crash (if save fails silently)
- Recover sessions that should NOT be recovered (idle, stopped, stale)
- Fail to recover sessions that SHOULD be recovered (working, waiting)
- Corrupt the registry file (non-atomic write)

Sections:
  1. load_registry
  2. save_registry_now (atomic write)
  3. schedule_registry_save (debounce)
  4. recover_sessions (the critical path)
  5. cancel_timer
"""

import json
import os
import time
import threading
import pytest
from pathlib import Path
from unittest.mock import MagicMock, patch, call

from daemon.session_registry import SessionRegistry, REGISTRY_PATH, MAX_RECOVERY_AGE


# =========================================================================
# Helpers
# =========================================================================

def _write_registry(path, sessions):
    """Write a registry JSON file with the given sessions dict."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"sessions": sessions}, indent=2),
                    encoding="utf-8")


# =========================================================================
# Section 1: load_registry
# =========================================================================


class TestLoadRegistry:
    """Test reading the session registry from disk.

    The registry must be resilient to missing, empty, and corrupted files
    since the daemon could crash at any point during a write.
    """

    def test_missing_file_returns_empty(self, tmp_path):
        """Missing registry file returns empty sessions dict.

        WHY: On first launch or after manual deletion, there are no
        sessions to recover.  Returning a non-dict or raising would
        crash the recovery logic.
        """
        reg = SessionRegistry()
        with patch.object(
            type(reg), 'load_registry',
            wraps=reg.load_registry
        ):
            # Patch REGISTRY_PATH to point to a nonexistent file
            with patch('daemon.session_registry.REGISTRY_PATH',
                       tmp_path / "nonexistent.json"):
                result = reg.load_registry()
        assert result == {"sessions": {}}

    def test_malformed_json_returns_empty(self, tmp_path):
        """Corrupted JSON falls back to empty sessions dict.

        WHY: A crash during save_registry_now could leave a partial
        JSON file.  The recovery logic must not crash on bad data.
        """
        reg = SessionRegistry()
        bad_file = tmp_path / "bad_registry.json"
        bad_file.write_text("{this is not valid json!!", encoding="utf-8")
        with patch('daemon.session_registry.REGISTRY_PATH', bad_file):
            result = reg.load_registry()
        assert result == {"sessions": {}}

    def test_valid_json_returns_correct_structure(self, tmp_path):
        """Valid registry file returns the correct dict structure.

        WHY: The most basic happy path -- the registry must be read
        correctly for recovery to work at all.
        """
        reg = SessionRegistry()
        registry_file = tmp_path / "registry.json"
        sessions = {
            "sess-1": {"name": "Test", "state": "working", "cwd": "/tmp"},
            "sess-2": {"name": "Other", "state": "idle", "cwd": "/home"},
        }
        _write_registry(registry_file, sessions)
        with patch('daemon.session_registry.REGISTRY_PATH', registry_file):
            result = reg.load_registry()
        assert "sessions" in result
        assert len(result["sessions"]) == 2
        assert result["sessions"]["sess-1"]["name"] == "Test"

    def test_json_without_sessions_key_returns_empty(self, tmp_path):
        """JSON that is valid but missing 'sessions' key returns empty.

        WHY: An old or corrupted file format must not crash recovery.
        """
        reg = SessionRegistry()
        registry_file = tmp_path / "registry.json"
        registry_file.write_text('{"version": 1}', encoding="utf-8")
        with patch('daemon.session_registry.REGISTRY_PATH', registry_file):
            result = reg.load_registry()
        assert result == {"sessions": {}}

    def test_non_dict_json_returns_empty(self, tmp_path):
        """JSON that is valid but not a dict returns empty.

        WHY: A file containing just a list or string must not crash.
        """
        reg = SessionRegistry()
        registry_file = tmp_path / "registry.json"
        registry_file.write_text('["not", "a", "dict"]', encoding="utf-8")
        with patch('daemon.session_registry.REGISTRY_PATH', registry_file):
            result = reg.load_registry()
        assert result == {"sessions": {}}


# =========================================================================
# Section 2: save_registry_now
# =========================================================================


class TestSaveRegistryNow:
    """Test atomic writing of the session registry.

    The save MUST be atomic (write to temp file, then rename) so that
    a crash mid-write never leaves a corrupt registry that prevents
    recovery of ALL sessions.
    """

    def test_basic_save_and_load_roundtrip(self, tmp_path):
        """Save followed by load returns the same data.

        WHY: The most fundamental requirement -- data must survive
        a save/load cycle or crash recovery is completely broken.
        """
        reg = SessionRegistry()
        registry_file = tmp_path / "registry.json"
        snapshot = {
            "sess-1": {"name": "Test", "state": "working", "cwd": "/tmp"},
        }
        with patch('daemon.session_registry.REGISTRY_PATH', registry_file):
            reg.save_registry_now(snapshot)
            result = reg.load_registry()
        assert result["sessions"]["sess-1"]["name"] == "Test"
        assert result["sessions"]["sess-1"]["state"] == "working"

    def test_correct_snapshot_format(self, tmp_path):
        """Saved file has the expected JSON structure.

        WHY: The format must include the top-level 'sessions' key
        that load_registry expects.
        """
        reg = SessionRegistry()
        registry_file = tmp_path / "registry.json"
        snapshot = {"s1": {"name": "A"}, "s2": {"name": "B"}}
        with patch('daemon.session_registry.REGISTRY_PATH', registry_file):
            reg.save_registry_now(snapshot)
        data = json.loads(registry_file.read_text(encoding="utf-8"))
        assert "sessions" in data
        assert len(data["sessions"]) == 2
        assert data["sessions"]["s1"]["name"] == "A"

    def test_creates_parent_directory(self, tmp_path):
        """save_registry_now creates parent directories if needed.

        WHY: On first run, ~/.claude/ may not exist yet.
        """
        reg = SessionRegistry()
        registry_file = tmp_path / "subdir" / "registry.json"
        with patch('daemon.session_registry.REGISTRY_PATH', registry_file):
            reg.save_registry_now({"s1": {"name": "Test"}})
        assert registry_file.exists()

    def test_overwrites_existing_file(self, tmp_path):
        """Saving overwrites the existing registry (via atomic replace).

        WHY: The registry must reflect current state, not accumulate
        historical data.
        """
        reg = SessionRegistry()
        registry_file = tmp_path / "registry.json"
        with patch('daemon.session_registry.REGISTRY_PATH', registry_file):
            reg.save_registry_now({"s1": {"name": "First"}})
            reg.save_registry_now({"s2": {"name": "Second"}})
            result = reg.load_registry()
        assert "s1" not in result["sessions"]
        assert result["sessions"]["s2"]["name"] == "Second"

    def test_empty_snapshot(self, tmp_path):
        """Saving an empty snapshot writes a valid but empty registry.

        WHY: When all sessions end, the registry should be cleared.
        """
        reg = SessionRegistry()
        registry_file = tmp_path / "registry.json"
        with patch('daemon.session_registry.REGISTRY_PATH', registry_file):
            reg.save_registry_now({})
            result = reg.load_registry()
        assert result["sessions"] == {}


# =========================================================================
# Section 3: schedule_registry_save (debounce)
# =========================================================================


class TestScheduleRegistrySave:
    """Test the debounced save mechanism.

    Multiple rapid state changes should batch into a single disk write.
    Without debouncing, every session state change hits disk, which can
    cause I/O contention under load.
    """

    def test_calls_save_fn_after_delay(self, tmp_path):
        """The save callback fires after the debounce delay.

        WHY: If the timer never fires, registry state is never persisted
        and crash recovery has stale data.
        """
        reg = SessionRegistry()
        save_called = threading.Event()
        call_count = [0]

        def fake_save():
            call_count[0] += 1
            save_called.set()

        reg.schedule_registry_save(fake_save)
        save_called.wait(timeout=5)
        assert call_count[0] == 1
        reg.cancel_timer()

    def test_debounce_skips_if_timer_pending(self, tmp_path):
        """Multiple rapid calls result in only one save.

        WHY: Without debounce, N state changes cause N disk writes.
        The debounce pattern ensures the pending timer captures the
        latest state when it fires.
        """
        reg = SessionRegistry()
        save_called = threading.Event()
        call_count = [0]

        def fake_save():
            call_count[0] += 1
            save_called.set()

        # Schedule multiple saves rapidly
        reg.schedule_registry_save(fake_save)
        reg.schedule_registry_save(fake_save)
        reg.schedule_registry_save(fake_save)

        save_called.wait(timeout=5)
        # Allow a bit more time for any extra calls
        time.sleep(0.5)
        # Only one save should have fired (the first timer)
        assert call_count[0] == 1
        reg.cancel_timer()


# =========================================================================
# Section 4: recover_sessions (the critical path)
# =========================================================================


class TestRecoverSessions:
    """Test crash recovery logic.

    This is the most critical code path in session_registry.  Bugs here
    can either fail to recover valid sessions (user loses work) or
    recover sessions that should stay dead (zombie sessions, resource
    waste, stale state).
    """

    def _make_mock_store(self, session_exists=True):
        """Create a mock ChatStore for recovery tests."""
        store = MagicMock()
        if session_exists:
            store.find_session_path.return_value = Path("/fake/session.jsonl")
        else:
            store.find_session_path.return_value = None
        store.repair_incomplete_turn.return_value = True
        return store

    def test_recovers_working_sessions(self, tmp_path):
        """Sessions in 'working' state are recovered.

        WHY: 'working' means the agent was mid-response when the daemon
        crashed.  Resuming picks up where it left off.
        """
        reg = SessionRegistry()
        registry_file = tmp_path / "registry.json"
        now = time.time()
        sessions = {
            "sess-1": {
                "name": "Active",
                "state": "working",
                "cwd": "/tmp",
                "model": "claude-3",
                "last_activity": now - 60,  # 1 minute ago
            },
        }
        _write_registry(registry_file, sessions)

        start_fn = MagicMock(return_value={"ok": True})
        store = self._make_mock_store()

        with patch('daemon.session_registry.REGISTRY_PATH', registry_file):
            reg.recover_sessions(start_fn, store)

        start_fn.assert_called_once()
        call_kwargs = start_fn.call_args
        assert call_kwargs.kwargs["session_id"] == "sess-1"
        assert call_kwargs.kwargs["resume"] is True
        assert call_kwargs.kwargs["cwd"] == os.path.normpath("/tmp")

    def test_recovers_waiting_sessions(self, tmp_path):
        """Sessions in 'waiting' state are recovered.

        WHY: 'waiting' means the agent is waiting for user input
        (permission prompt).  The session should be reconnected.
        """
        reg = SessionRegistry()
        registry_file = tmp_path / "registry.json"
        now = time.time()
        sessions = {
            "sess-1": {
                "name": "Waiting",
                "state": "waiting",
                "cwd": "/tmp",
                "model": "",
                "last_activity": now - 30,
            },
        }
        _write_registry(registry_file, sessions)
        start_fn = MagicMock(return_value={"ok": True})
        store = self._make_mock_store()
        with patch('daemon.session_registry.REGISTRY_PATH', registry_file):
            reg.recover_sessions(start_fn, store)
        start_fn.assert_called_once()

    def test_recovers_starting_sessions(self, tmp_path):
        """Sessions in 'starting' state are recovered.

        WHY: 'starting' means the session was being initialized when
        the daemon crashed.
        """
        reg = SessionRegistry()
        registry_file = tmp_path / "registry.json"
        now = time.time()
        sessions = {
            "sess-1": {
                "name": "Starting",
                "state": "starting",
                "cwd": "/tmp",
                "model": "",
                "last_activity": now - 30,
            },
        }
        _write_registry(registry_file, sessions)
        start_fn = MagicMock(return_value={"ok": True})
        store = self._make_mock_store()
        with patch('daemon.session_registry.REGISTRY_PATH', registry_file):
            reg.recover_sessions(start_fn, store)
        start_fn.assert_called_once()

    def test_skips_idle_sessions(self, tmp_path):
        """Sessions in 'idle' state are NOT recovered.

        WHY: Idle sessions have completed their work.  Recovering them
        wastes resources and confuses the user with unexpected reconnects.
        """
        reg = SessionRegistry()
        registry_file = tmp_path / "registry.json"
        now = time.time()
        sessions = {
            "sess-idle": {
                "name": "Idle",
                "state": "idle",
                "cwd": "/tmp",
                "model": "",
                "last_activity": now - 30,
            },
        }
        _write_registry(registry_file, sessions)
        start_fn = MagicMock(return_value={"ok": True})
        store = self._make_mock_store()
        with patch('daemon.session_registry.REGISTRY_PATH', registry_file):
            reg.recover_sessions(start_fn, store)
        start_fn.assert_not_called()

    def test_skips_stopped_sessions(self, tmp_path):
        """Sessions in 'stopped' state are NOT recovered.

        WHY: Stopped sessions were explicitly terminated by the user.
        """
        reg = SessionRegistry()
        registry_file = tmp_path / "registry.json"
        now = time.time()
        sessions = {
            "sess-stopped": {
                "name": "Stopped",
                "state": "stopped",
                "cwd": "/tmp",
                "model": "",
                "last_activity": now - 30,
            },
        }
        _write_registry(registry_file, sessions)
        start_fn = MagicMock(return_value={"ok": True})
        store = self._make_mock_store()
        with patch('daemon.session_registry.REGISTRY_PATH', registry_file):
            reg.recover_sessions(start_fn, store)
        start_fn.assert_not_called()

    def test_skips_stale_sessions(self, tmp_path):
        """Sessions older than MAX_RECOVERY_AGE are skipped.

        WHY: Very old sessions are unlikely to still be relevant.
        Recovering a 12-hour-old session would waste resources and
        possibly send stale messages to a model that has lost context.
        """
        reg = SessionRegistry()
        registry_file = tmp_path / "registry.json"
        now = time.time()
        sessions = {
            "sess-stale": {
                "name": "Stale",
                "state": "working",
                "cwd": "/tmp",
                "model": "",
                "last_activity": now - 7200,  # 2 hours ago (> 1 hour max)
            },
        }
        _write_registry(registry_file, sessions)
        start_fn = MagicMock(return_value={"ok": True})
        store = self._make_mock_store()
        with patch('daemon.session_registry.REGISTRY_PATH', registry_file):
            reg.recover_sessions(start_fn, store, max_age=3600)
        start_fn.assert_not_called()

    def test_skips_planner_sessions(self, tmp_path):
        """Planner sessions are never recovered.

        WHY: Planner sessions are transient orchestration sessions
        that should not persist across crashes.
        """
        reg = SessionRegistry()
        registry_file = tmp_path / "registry.json"
        now = time.time()
        sessions = {
            "sess-planner": {
                "name": "Planner",
                "state": "working",
                "cwd": "/tmp",
                "model": "",
                "session_type": "planner",
                "last_activity": now - 30,
            },
        }
        _write_registry(registry_file, sessions)
        start_fn = MagicMock(return_value={"ok": True})
        store = self._make_mock_store()
        with patch('daemon.session_registry.REGISTRY_PATH', registry_file):
            reg.recover_sessions(start_fn, store)
        start_fn.assert_not_called()

    def test_skips_deleted_session_files(self, tmp_path):
        """Sessions whose .jsonl file was deleted are NOT recovered.

        WHY: If the user deleted the session file, recovering would
        undo the deletion -- creating a ghost session with no history.
        """
        reg = SessionRegistry()
        registry_file = tmp_path / "registry.json"
        now = time.time()
        sessions = {
            "sess-deleted": {
                "name": "Deleted",
                "state": "working",
                "cwd": "/tmp",
                "model": "",
                "last_activity": now - 30,
            },
        }
        _write_registry(registry_file, sessions)
        start_fn = MagicMock(return_value={"ok": True})
        store = self._make_mock_store(session_exists=False)
        with patch('daemon.session_registry.REGISTRY_PATH', registry_file):
            reg.recover_sessions(start_fn, store)
        start_fn.assert_not_called()

    def test_one_failure_does_not_block_others(self, tmp_path):
        """If one session fails to recover, others still proceed.

        WHY: A single corrupted session must not prevent recovery of
        all other valid sessions.  Each recovery is independent.
        """
        reg = SessionRegistry()
        registry_file = tmp_path / "registry.json"
        now = time.time()
        sessions = {
            "sess-fail": {
                "name": "Fails",
                "state": "working",
                "cwd": "/tmp",
                "model": "",
                "last_activity": now - 30,
            },
            "sess-ok": {
                "name": "Succeeds",
                "state": "working",
                "cwd": "/tmp",
                "model": "",
                "last_activity": now - 30,
            },
        }
        _write_registry(registry_file, sessions)

        def side_effect(**kwargs):
            if kwargs["session_id"] == "sess-fail":
                return {"ok": False, "error": "boom"}
            return {"ok": True}

        start_fn = MagicMock(side_effect=side_effect)
        store = self._make_mock_store()
        with patch('daemon.session_registry.REGISTRY_PATH', registry_file):
            reg.recover_sessions(start_fn, store)
        # Both should have been attempted
        assert start_fn.call_count == 2

    def test_empty_registry_no_recovery(self, tmp_path):
        """Empty registry means nothing to recover.

        WHY: After a clean shutdown with no active sessions, recovery
        should be a no-op.
        """
        reg = SessionRegistry()
        registry_file = tmp_path / "registry.json"
        _write_registry(registry_file, {})
        start_fn = MagicMock()
        store = self._make_mock_store()
        with patch('daemon.session_registry.REGISTRY_PATH', registry_file):
            reg.recover_sessions(start_fn, store)
        start_fn.assert_not_called()

    def test_missing_registry_file_no_recovery(self, tmp_path):
        """Missing registry file means nothing to recover.

        WHY: First launch -- no crash history exists.
        """
        reg = SessionRegistry()
        start_fn = MagicMock()
        store = self._make_mock_store()
        with patch('daemon.session_registry.REGISTRY_PATH',
                   tmp_path / "nonexistent.json"):
            reg.recover_sessions(start_fn, store)
        start_fn.assert_not_called()

    def test_start_session_fn_receives_correct_args(self, tmp_path):
        """start_session_fn is called with the correct keyword arguments.

        WHY: The recovery must pass session_id, prompt="", cwd, name,
        resume=True, and model.  Wrong args could create a new session
        instead of resuming, or resume the wrong one.
        """
        reg = SessionRegistry()
        registry_file = tmp_path / "registry.json"
        now = time.time()
        sessions = {
            "sess-abc": {
                "name": "My Session",
                "state": "working",
                "cwd": "/projects/myapp",
                "model": "claude-sonnet-4-20250514",
                "last_activity": now - 60,
            },
        }
        _write_registry(registry_file, sessions)
        start_fn = MagicMock(return_value={"ok": True})
        store = self._make_mock_store()
        with patch('daemon.session_registry.REGISTRY_PATH', registry_file):
            reg.recover_sessions(start_fn, store)
        start_fn.assert_called_once_with(
            session_id="sess-abc",
            prompt="",
            cwd=os.path.normpath("/projects/myapp"),
            name="My Session",
            resume=True,
            model="claude-sonnet-4-20250514",
        )

    def test_repairs_incomplete_turn_before_recovery(self, tmp_path):
        """repair_incomplete_turn is called before starting recovery.

        WHY: If the daemon crashed mid-response, the .jsonl ends with
        an incomplete assistant turn (stop_reason=null).  The CLI's
        --resume chokes on this, so we must repair it first.
        """
        reg = SessionRegistry()
        registry_file = tmp_path / "registry.json"
        now = time.time()
        sessions = {
            "sess-1": {
                "name": "Repair Me",
                "state": "working",
                "cwd": "/tmp",
                "model": "",
                "last_activity": now - 30,
            },
        }
        _write_registry(registry_file, sessions)
        start_fn = MagicMock(return_value={"ok": True})
        store = self._make_mock_store()
        with patch('daemon.session_registry.REGISTRY_PATH', registry_file):
            reg.recover_sessions(start_fn, store)
        # cwd is normalized by os.path.normpath inside recover_sessions,
        # so on Windows "/tmp" becomes "\\tmp"
        store.repair_incomplete_turn.assert_called_once_with(
            "sess-1", cwd=os.path.normpath("/tmp")
        )


# =========================================================================
# Section 5: cancel_timer
# =========================================================================


class TestCancelTimer:
    """Test timer cancellation."""

    def test_cancel_pending_timer(self):
        """cancel_timer stops a pending save from firing.

        WHY: During shutdown, we cancel the debounce timer and do
        a final synchronous save instead.
        """
        reg = SessionRegistry()
        save_called = [False]

        def fake_save():
            save_called[0] = True

        reg.schedule_registry_save(fake_save)
        reg.cancel_timer()
        # Wait long enough for the timer to have fired if not cancelled
        time.sleep(0.5)
        assert save_called[0] is False

    def test_cancel_when_no_timer(self):
        """cancel_timer is safe to call when no timer is pending.

        WHY: Defensive -- code may cancel without checking if a timer
        was ever scheduled.
        """
        reg = SessionRegistry()
        # Should not raise
        reg.cancel_timer()
        assert reg._registry_timer is None
