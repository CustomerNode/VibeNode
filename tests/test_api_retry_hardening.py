"""
Hardening regression tests for the API-error auto-retry exponential backoff.

These cover the failure paths that made the retry "sometimes work, sometimes
fail".  Every one of them previously ended with the session STRANDED: the
countdown fields left non-zero with no live timer behind them.  That end state
is worse than a plain error, because ``retry_at > 0`` is the centralized queue
gate in ``_try_dispatch_queue`` — a stranded session silently suppresses every
queued message forever, and the browser renders a countdown stuck on "now…".

The invariant asserted throughout: **when no timer is alive, retry_at must be
0.**  Either the chain re-arms (a new timer exists) or the countdown is cleared
and a manual Retry is surfaced.  Never neither.

Fixtures are reused from tests/test_api_error_retry.py so the SDK mocking and
manager setup stay in one place.
"""

import asyncio
import time
import pytest
from unittest.mock import patch

from tests.test_api_error_retry import (  # noqa: F401 — imported for pytest fixtures
    mock_socketio,
    mock_sdk_types,
    sm_module,
    session_manager,
    _make_idle_session,
)


# ===========================================================================
# _fire_api_retry: the resend can fail, and the chain must survive it
# ===========================================================================

class TestFireRetrySendFailure:
    """``_fire_api_retry`` used to call ``send_message`` unguarded, then clear
    the countdown on the next four lines.  A raise skipped the clear entirely;
    an ``{"ok": False}`` return was discarded without re-arming."""

    def test_raising_send_does_not_strand_the_session(self, session_manager,
                                                      sm_module, monkeypatch):
        info = _make_idle_session(sm_module, session_manager, "strand-1")
        armed = []
        monkeypatch.setattr(session_manager, "_arm_api_retry",
                            lambda sid, i: armed.append(sid))

        with patch.object(session_manager, "send_message",
                          side_effect=RuntimeError("transport is dead")):
            session_manager._fire_api_retry("strand-1", info)

        # The countdown must be cleared even though the send blew up.
        assert info.retry_at == 0.0, \
            "a raising send left retry_at > 0 — queue gate closed forever"
        assert info.retry_attempt == 0
        assert info.retry_reason == ""
        # ...and the chain must continue instead of dying silently.
        assert armed == ["strand-1"], "a failed resend must re-arm the backoff"
        assert info._retry_needs_reconnect is True, \
            "a failed send means a dead transport — next attempt must reconnect"

    def test_rejected_send_rearms(self, session_manager, sm_module, monkeypatch):
        """send_message returning ok=False (e.g. 'Session is stopped') is a
        failure too — it used to end the chain with no error and no retry."""
        info = _make_idle_session(sm_module, session_manager, "strand-2")
        armed = []
        monkeypatch.setattr(session_manager, "_arm_api_retry",
                            lambda sid, i: armed.append(sid))

        with patch.object(session_manager, "send_message",
                          return_value={"ok": False, "error": "Session is stopped"}):
            session_manager._fire_api_retry("strand-2", info)

        assert info.retry_at == 0.0
        assert armed == ["strand-2"]

    def test_successful_send_does_not_rearm(self, session_manager, sm_module,
                                            monkeypatch):
        """Guard against over-correction: the happy path must NOT re-arm."""
        info = _make_idle_session(sm_module, session_manager, "ok-1")
        armed = []
        monkeypatch.setattr(session_manager, "_arm_api_retry",
                            lambda sid, i: armed.append(sid))

        with patch.object(session_manager, "send_message",
                          return_value={"ok": True}):
            session_manager._fire_api_retry("ok-1", info)

        assert armed == []
        assert info.retry_at == 0.0
        assert info._api_retry_count == 1, "the attempt must still be consumed"

    def test_send_failure_with_no_budget_surfaces_manual_retry(
            self, session_manager, sm_module):
        """Last attempt fails → clear the countdown and surface manual Retry."""
        info = _make_idle_session(sm_module, session_manager, "strand-3")
        info._api_retry_count = session_manager._API_RETRY_MAX - 1

        with patch.object(session_manager, "send_message",
                          side_effect=RuntimeError("boom")):
            session_manager._fire_api_retry("strand-3", info)

        assert info.retry_at == 0.0
        assert info.state == sm_module.SessionState.IDLE
        assert "Retry" in (info.error or ""), \
            "exhausted chain must leave an actionable error, not a blank idle"

    def test_send_failure_ignored_when_a_newer_turn_owns_the_session(
            self, session_manager, sm_module, monkeypatch):
        """If the session is already WORKING, something else took over — do not
        re-arm a competing retry on top of it."""
        info = _make_idle_session(sm_module, session_manager, "strand-4")
        armed = []
        monkeypatch.setattr(session_manager, "_arm_api_retry",
                            lambda sid, i: armed.append(sid))

        def _fail_then_work(*a, **k):
            info.state = sm_module.SessionState.WORKING
            return {"ok": False, "error": "already busy"}

        with patch.object(session_manager, "send_message",
                          side_effect=_fail_then_work):
            session_manager._fire_api_retry("strand-4", info)

        assert armed == []
        assert info.retry_at == 0.0


# ===========================================================================
# _api_retry_timer: nothing may escape the task
# ===========================================================================

class TestTimerExceptionContainment:
    """An exception escaping the timer coroutine became an un-retrieved asyncio
    task exception — invisible, and leaving the session pinned at retry_at > 0
    with no timer alive to ever clear it."""

    def test_timer_crash_clears_the_countdown(self, session_manager, sm_module):
        info = _make_idle_session(sm_module, session_manager, "crash-1")
        info.retry_at = time.time() + 999
        info.retry_attempt = 3
        info.retry_max = 30
        info.retry_reason = "API overloaded"

        with patch.object(session_manager, "_api_retry_timer_fire",
                          side_effect=RuntimeError("kaboom")):
            asyncio.run(session_manager._api_retry_timer("crash-1", 0))

        assert info.retry_at == 0.0, \
            "a crashed timer must not leave the queue gate permanently closed"
        assert info.retry_attempt == 0
        assert info.retry_reason == ""
        assert "Retry" in (info.error or "")

    def test_cancellation_is_still_silent(self, session_manager, sm_module):
        """Cancel (user pressed Cancel / sent a new message) must NOT be treated
        as a crash — no error banner, and the countdown is left to the canceller."""
        info = _make_idle_session(sm_module, session_manager, "crash-2")
        info.retry_at = time.time() + 999

        async def _run():
            task = asyncio.ensure_future(
                session_manager._api_retry_timer("crash-2", 60))
            await asyncio.sleep(0.05)
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

        asyncio.run(_run())
        assert not info.error, "a cancelled retry must not surface an error"


# ===========================================================================
# _arm_api_retry: ineligible states must be logged, never silently dropped
# ===========================================================================

class TestArmEligibility:
    @pytest.mark.parametrize("state_name",
                             ["WORKING", "WAITING", "STARTING", "STOPPED"])
    def test_arm_skipped_for_ineligible_state(self, session_manager, sm_module,
                                              state_name, caplog):
        info = _make_idle_session(sm_module, session_manager, "arm-" + state_name)
        info.state = getattr(sm_module.SessionState, state_name)
        info._api_retry_needed = True
        info.retry_reason = "API overloaded"

        with caplog.at_level("INFO"):
            session_manager._arm_api_retry(info.session_id, info)

        assert info.retry_at == 0.0
        assert info._api_retry_task is None
        assert info._api_retry_needed is False
        # The drop must be traceable — it used to return with no log at all,
        # which is indistinguishable from "the retry never happened".
        assert any("Auto-retry NOT armed" in r.getMessage()
                   for r in caplog.records), \
            "an ineligible-state drop must be logged"

    def test_arm_schedules_a_timer_when_idle(self, session_manager, sm_module,
                                             monkeypatch):
        monkeypatch.setattr(sm_module.SessionManager, "_API_RETRY_BASE", 30.0)
        info = _make_idle_session(sm_module, session_manager, "arm-ok")
        info._api_retry_needed = True

        # _arm_api_retry is called here from a NON-loop thread (the test thread),
        # exercising the run_coroutine_threadsafe path.
        session_manager._arm_api_retry(info.session_id, info)
        try:
            assert info.retry_at > time.time(), "countdown must be set"
            assert info._api_retry_task is not None, "a timer must exist"
            assert info.retry_attempt == 1
        finally:
            session_manager._clear_api_retry(info, reset_count=True)


# ===========================================================================
# Wall-clock reconciliation (suspend / resume)
# ===========================================================================

class TestWallClockDeadline:
    """asyncio.sleep runs on the MONOTONIC clock, which does not advance across
    a machine suspend; retry_at is WALL clock.  A lid closed mid-backoff used to
    resume with an expired countdown and a timer that still had its full sleep
    left.  The deadline is now re-derived from time.time() on every slice."""

    def test_past_deadline_returns_immediately(self, sm_module):
        mgr = sm_module.SessionManager()
        started = time.time()
        asyncio.run(mgr._await_retry_deadline(time.time() - 3600))
        assert time.time() - started < 1.0

    def test_sleeps_in_bounded_slices(self, sm_module, monkeypatch):
        """The slice cap is what bounds how late a suspend can push a retry."""
        mgr = sm_module.SessionManager()
        monkeypatch.setattr(sm_module.SessionManager, "_API_RETRY_TICK", 1.0)

        real_sleep = asyncio.sleep
        slices = []

        async def recording_sleep(d):
            slices.append(d)
            return await real_sleep(d)

        monkeypatch.setattr(asyncio, "sleep", recording_sleep)
        asyncio.run(mgr._await_retry_deadline(time.time() + 3.0))
        monkeypatch.undo()

        assert len(slices) >= 3, "a 3s wait with a 1s tick must slice, not one-shot"
        assert max(slices) <= 1.0 + 1e-6, \
            "no slice may exceed the tick — that is the suspend overshoot bound"

    def test_deadline_is_still_honored(self, sm_module, monkeypatch):
        """Slicing must not make the timer fire early."""
        mgr = sm_module.SessionManager()
        monkeypatch.setattr(sm_module.SessionManager, "_API_RETRY_TICK", 0.2)
        started = time.time()
        asyncio.run(mgr._await_retry_deadline(time.time() + 0.8))
        assert time.time() - started >= 0.7


# ===========================================================================
# The core invariant, stated once
# ===========================================================================

class TestNoStrandedCountdownInvariant:
    def test_fail_retry_open_reopens_the_queue_gate(self, session_manager,
                                                    sm_module):
        info = _make_idle_session(sm_module, session_manager, "gate-1")
        info.retry_at = time.time() + 600
        info.retry_attempt = 5
        info.retry_max = 30
        info.retry_reason = "Rate limited"
        info._api_retry_count = 5

        session_manager._fail_retry_open("gate-1", "Auto-retry failed — use Retry.")

        # retry_at == 0 is precisely what re-opens _try_dispatch_queue.
        assert info.retry_at == 0.0
        assert info.retry_attempt == 0
        assert info.retry_max == 0
        assert info.retry_reason == ""
        assert info._api_retry_count == 0
        assert info.error == "Auto-retry failed — use Retry."
        assert info.state == sm_module.SessionState.IDLE

    def test_fail_retry_open_never_raises_on_unknown_session(self,
                                                            session_manager):
        # Called from except handlers — must be bulletproof.
        session_manager._fail_retry_open("no-such-session", "whatever")
