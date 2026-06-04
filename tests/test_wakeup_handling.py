"""Regression tests for session wake-up / auto-resume handling.

Context
-------

The Claude SDK keeps a session alive past the assistant turn's RESULT when
the agent schedules a deferred wake-up (ScheduleWakeup tool, Bash with
run_in_background=True, etc.).  When the wake-up fires, the SDK injects a
synthetic turn (init -> content -> RESULT) into the same buffer.

VibeNode's post-turn listener is what consumes that auto-resume cycle.  A
prior version of the listener handled task_notification-led wake-ups but
missed the case where the SDK delivers a bare ``init`` first.  In that
case _process_message's init handler emitted IDLE while state was still
IDLE, _try_dispatch_queue fired on the emit, and a queued user message
got sent into the middle of the wake-up cycle — the session ended up
reading wake-up content as the response to the queued message.

These tests guard the fix:

1. ``_tool_creates_wakeup`` recognises every flavor of wake-up tool we
   know about (and rejects unrelated tools).
2. ``_emit_state`` suppresses queue auto-dispatch when a wake-up is
   pending and the post-turn listener is active.
3. Non-wake-up sessions are unaffected by the gate (queue still
   dispatches normally).
4. ``_enter_auto_resume`` resets ``_wakeup_pending`` so a single wake-up
   cycle doesn't keep the gate latched forever.
5. The post-turn listener's pre-detect logic flips to WORKING BEFORE
   _process_message runs on an auto-resume init / task_notification /
   non-system message — so the init handler's emit cannot fire on stale
   IDLE state.
6. ``_send_query`` and ``_drive_session`` clear ``_wakeup_pending`` at
   user-driven turn start.
"""

import asyncio
import inspect
import time
import pytest

from daemon.session_manager import (
    SessionManager,
    SessionInfo,
    SessionState,
)
from daemon.backends.messages import (
    VibeNodeMessage,
    MessageKind,
)


# ── 1. Wake-up tool classification ──────────────────────────────────────


class TestWakeupToolDetection:
    """`_tool_creates_wakeup` must catch every flavor of wake-up tool."""

    def test_schedule_wakeup_exact_name(self):
        assert SessionManager._tool_creates_wakeup("ScheduleWakeup", {})

    def test_bash_run_in_background_true(self):
        assert SessionManager._tool_creates_wakeup(
            "Bash", {"command": "sleep 60 &", "run_in_background": True}
        )

    def test_bash_run_in_background_false_not_detected(self):
        assert not SessionManager._tool_creates_wakeup(
            "Bash", {"command": "ls", "run_in_background": False}
        )

    def test_bash_no_run_in_background_kwarg(self):
        assert not SessionManager._tool_creates_wakeup(
            "Bash", {"command": "ls"}
        )

    def test_substring_variants_caught(self):
        # Renames or vendor variants must not silently regress the fix.
        for name in ("schedule_wakeup", "WakeUp", "wakeup",
                     "ScheduleWakeUp", "BackgroundTask",
                     "background_task_runner"):
            assert SessionManager._tool_creates_wakeup(name, {}), (
                f"variant {name!r} should be flagged as a wake-up tool"
            )

    def test_unrelated_tools_not_detected(self):
        for name in ("Edit", "Write", "Read", "Grep", "Task", "Glob"):
            assert not SessionManager._tool_creates_wakeup(name, {}), (
                f"{name!r} should not be flagged as a wake-up tool"
            )

    def test_empty_tool_name(self):
        assert not SessionManager._tool_creates_wakeup("", {})

    def test_non_dict_input_handled(self):
        # Defensive: if input arrives as something other than a dict
        # (shouldn't happen but the normalization layer can't always
        # guarantee), we don't blow up.
        assert SessionManager._tool_creates_wakeup("ScheduleWakeup", None)
        assert not SessionManager._tool_creates_wakeup("Bash", None)


# ── 2/3. Queue dispatch gating in _emit_state ───────────────────────────


class TestEmitStateQueueDispatchGate:
    """`_emit_state` must suppress queue dispatch ONLY when a wake-up is
    pending AND the post-turn listener owns the buffer.  Sessions without
    a pending wake-up keep dispatching normally."""

    def _make_idle_info(self):
        info = SessionInfo(session_id="test-sid")
        info.state = SessionState.IDLE
        return info

    def test_dispatch_fires_on_normal_idle(self):
        sm = SessionManager()
        info = self._make_idle_info()
        with sm._lock:
            sm._sessions[info.session_id] = info
        calls = []
        sm._try_dispatch_queue = lambda sid: calls.append(sid)
        sm._emit_state(info)
        assert calls == [info.session_id]

    def test_dispatch_suppressed_when_post_turn_AND_wakeup_pending(self):
        sm = SessionManager()
        info = self._make_idle_info()
        info._in_post_turn = True
        info._wakeup_pending = True
        with sm._lock:
            sm._sessions[info.session_id] = info
        calls = []
        sm._try_dispatch_queue = lambda sid: calls.append(sid)
        sm._emit_state(info)
        assert calls == [], (
            "queue dispatch must be suppressed during the post-turn window "
            "when a wake-up is pending — otherwise the queued message races "
            "the wake-up content into the SDK buffer"
        )

    def test_dispatch_fires_when_post_turn_but_no_wakeup_pending(self):
        sm = SessionManager()
        info = self._make_idle_info()
        info._in_post_turn = True
        info._wakeup_pending = False
        with sm._lock:
            sm._sessions[info.session_id] = info
        calls = []
        sm._try_dispatch_queue = lambda sid: calls.append(sid)
        sm._emit_state(info)
        assert calls == [info.session_id], (
            "non-wake-up sessions must keep dispatching normally even with "
            "the listener active — only wake-up sessions need the gate"
        )

    def test_dispatch_fires_when_wakeup_pending_but_no_listener(self):
        # If a wake-up was scheduled but the post-turn listener isn't yet
        # active (e.g. mid-turn state still WORKING), we shouldn't gate.
        # (In practice _emit_state only dispatches on IDLE anyway, but
        # belt-and-suspenders.)
        sm = SessionManager()
        info = self._make_idle_info()
        info._in_post_turn = False
        info._wakeup_pending = True
        with sm._lock:
            sm._sessions[info.session_id] = info
        calls = []
        sm._try_dispatch_queue = lambda sid: calls.append(sid)
        sm._emit_state(info)
        assert calls == [info.session_id]


# ── 4. _enter_auto_resume side effects ──────────────────────────────────


class TestEnterAutoResumeSideEffects:
    """`_enter_auto_resume` must flip IDLE→WORKING, CLEAR substatus (the
    wake-up has fired so we're no longer "awaiting"), AND reset
    _wakeup_pending so the dispatch gate re-opens at the next RESULT if
    no new wake-up was scheduled."""

    def test_enter_auto_resume_from_idle(self):
        sm = SessionManager()
        info = SessionInfo(session_id="test-sid")
        info.state = SessionState.IDLE
        info._wakeup_pending = True
        # Simulate the sleep window: substatus was set to "auto-resuming"
        # by the prior RESULT branch.
        info.substatus = "auto-resuming"
        sm._push_callback = lambda *a, **kw: None
        sm._enter_auto_resume(info)
        assert info.state == SessionState.WORKING
        assert info.substatus == "", (
            "_enter_auto_resume must clear the 'auto-resuming' substatus "
            "when the wake-up actually fires — otherwise the working bar "
            "keeps saying 'Awaiting wake-up…' and kanban keeps showing "
            "the sleeping dot while the session is actively processing"
        )
        assert info._wakeup_pending is False, (
            "the wake-up that triggered this resume is consumed — gate "
            "should re-open unless the auto-resume turn schedules a new one"
        )

    def test_substatus_preserved_through_sleep_window(self):
        """While the wake-up is pending (state=IDLE, _wakeup_pending=True)
        the ``auto-resuming`` substatus must survive _emit_state's auto-clear.
        Otherwise the UI's "Awaiting wake-up…" indicator disappears the
        moment the post-turn listener emits IDLE, and the user sees a
        plain-idle bar throughout the sleep — the UX half of the bug."""
        sm = SessionManager()
        info = SessionInfo(session_id="test-sid")
        info.state = SessionState.IDLE
        info._wakeup_pending = True
        info.substatus = "auto-resuming"
        with sm._lock:
            sm._sessions[info.session_id] = info
        sm._push_callback = lambda *a, **kw: None
        sm._emit_state(info)
        assert info.substatus == "auto-resuming", (
            "auto-clear stripped the sleeping substatus while a wake-up "
            "was pending — UX would revert to plain idle during the sleep"
        )

    def test_substatus_cleared_after_wakeup_completes(self):
        """When the wake-up cycle completes (_wakeup_pending=False), the
        normal IDLE auto-clear should sweep the substatus.  Without this
        the session would show "Awaiting wake-up…" forever after the
        wake-up actually fires."""
        sm = SessionManager()
        info = SessionInfo(session_id="test-sid")
        info.state = SessionState.IDLE
        info._wakeup_pending = False  # cleared by _enter_auto_resume earlier
        info.substatus = "auto-resuming"
        with sm._lock:
            sm._sessions[info.session_id] = info
        sm._push_callback = lambda *a, **kw: None
        sm._emit_state(info)
        assert info.substatus == "", (
            "auto-clear failed to sweep the substatus after the wake-up "
            "cycle ended — UI would stay in 'Awaiting wake-up…' forever"
        )

    def test_tool_use_flags_wakeup_pending_without_setting_substatus(self):
        """When ``_process_message`` sees a wake-up tool use mid-turn, it
        sets ``_wakeup_pending=True`` but does NOT set the substatus.
        The substatus is applied at RESULT so the working bar doesn't say
        "Awaiting wake-up…" while the agent is still actively running
        the rest of the turn after the schedule call (the user-reported
        bug: "shows awaiting wake-up while still working")."""
        import inspect
        src = inspect.getsource(SessionManager._process_message)
        assert 'info._wakeup_pending = True' in src, (
            "_process_message no longer flags _wakeup_pending in tool_use"
        )
        # Locate the tool_use handler's wakeup branch
        tool_use_idx = src.find('info._wakeup_pending = True')
        # Find the next 200 chars of the wakeup branch
        branch = src[tool_use_idx:tool_use_idx + 600]
        # Substatus must NOT be set INSIDE this branch
        assert 'info.substatus = "auto-resuming"' not in branch, (
            "_process_message sets substatus during the turn — that "
            "prematurely shows 'Awaiting wake-up…' while the agent is "
            "still working.  Move the substatus set to the RESULT branch."
        )

    def test_result_branch_applies_sleeping_substatus_when_wakeup_pending(self):
        """The RESULT branch of ``_process_message`` is where the sleeping
        substatus is finally applied — only when ``_wakeup_pending`` was
        flipped by an earlier tool_use in the same turn."""
        import inspect
        src = inspect.getsource(SessionManager._process_message)
        # Locate the RESULT branch
        result_idx = src.find('elif message.kind == MessageKind.RESULT:')
        assert result_idx > 0, "RESULT branch not found"
        result_branch = src[result_idx:result_idx + 4000]
        # The branch must contain BOTH the pending check AND the substatus set
        assert "_wakeup_pending" in result_branch
        assert 'info.substatus = "auto-resuming"' in result_branch, (
            "RESULT branch no longer applies the sleeping substatus — "
            "the UI will revert to plain idle even with a pending wake-up"
        )

    def test_interrupt_clears_sleeping_substatus(self):
        """interrupt_session must clear both ``_wakeup_pending`` and the
        sleeping substatus — otherwise a session the user just stopped
        keeps showing 'Awaiting wake-up…' forever.  We verify by source
        inspection because the full interrupt flow requires the daemon
        event loop, which we don't spin up here."""
        import inspect
        src = inspect.getsource(SessionManager.interrupt_session)
        # interrupt_session must clear BOTH the pending flag and the substatus
        assert 'info._wakeup_pending = False' in src, (
            "interrupt_session no longer clears _wakeup_pending — the gate "
            "remains latched and the substatus may stick"
        )
        assert 'info.substatus = ""' in src, (
            "interrupt_session no longer clears substatus — UI will keep "
            "showing 'Awaiting wake-up…' on a stopped session"
        )

    def test_close_session_clears_sleeping_substatus(self):
        """close_session must also clear sleeping state for the same
        reason — a STOPPED session is not awaiting a wake-up."""
        import inspect
        src = inspect.getsource(SessionManager.close_session)
        assert 'info._wakeup_pending = False' in src
        assert 'info.substatus = ""' in src, (
            "close_session no longer clears substatus — STOPPED session "
            "would still show 'Awaiting wake-up…' in the UI"
        )

    def test_enter_auto_resume_idempotent(self):
        # Calling twice (e.g. chained resume signals) shouldn't double-emit
        # or break state.
        sm = SessionManager()
        info = SessionInfo(session_id="test-sid")
        info.state = SessionState.IDLE
        emits = []
        sm._push_callback = lambda evt, data: emits.append((evt, data))
        sm._enter_auto_resume(info)
        emits_after_first = list(emits)
        sm._enter_auto_resume(info)  # state is WORKING now — should no-op
        assert emits == emits_after_first


# ── 5. Listener pre-detect logic ────────────────────────────────────────


class TestListenerPreDetectAutoResume:
    """The post-turn listener must enter auto-resume BEFORE
    _process_message runs on init / task_notification / non-system
    content.  Otherwise _process_message's emits could fire on stale
    IDLE state and dispatch the queue mid-wake-up.

    These tests exercise the listener with a mock SDK that injects a
    sequence of post-RESULT messages and verifies state transitions
    and dispatch behavior.
    """

    def _make_session(self, sm: SessionManager, *,
                       wakeup_pending: bool = True) -> SessionInfo:
        info = SessionInfo(session_id="test-sid")
        info.state = SessionState.IDLE
        info._wakeup_pending = wakeup_pending
        with sm._lock:
            sm._sessions[info.session_id] = info
        return info

    def _patch_sdk_with_messages(self, sm: SessionManager,
                                 message_batches: list[list[VibeNodeMessage]]):
        """Wire ``sm._sdk.receive_response`` to yield each batch in turn.

        Each call to receive_response() returns one batch's async iterator
        and terminates when that batch is exhausted.  Mimics the real SDK's
        per-turn termination behavior.
        """
        batches = iter(message_batches)

        async def fake_receive_response(_client):
            try:
                batch = next(batches)
            except StopIteration:
                # No more batches — block forever (matches real SDK which
                # would wait for more wake-ups).  In tests we cancel the
                # task to break out.
                evt = asyncio.Event()
                await evt.wait()
                return
            for msg in batch:
                yield msg

        sm._sdk.receive_response = fake_receive_response  # type: ignore[assignment]

    def _run_listener_to_completion(self, sm: SessionManager,
                                    info: SessionInfo,
                                    batches: list[list[VibeNodeMessage]]):
        """Run _extended_post_turn_listener until all batches are drained,
        then cancel.  Returns the list of state transitions observed and
        the list of queue-dispatch attempts."""
        loop = asyncio.new_event_loop()
        sm._loop = loop  # listener occasionally calls sm._loop in helpers
        try:
            self._patch_sdk_with_messages(sm, batches)

            dispatch_calls: list[str] = []
            sm._try_dispatch_queue = lambda sid: dispatch_calls.append(sid)

            states: list[tuple[str, str]] = []
            def _push(evt, data):
                if evt == 'session_state':
                    states.append((data.get('state'), data.get('substatus', '')))
            sm._push_callback = _push

            async def run():
                # The listener checks info.task is current_task — bind it.
                task = asyncio.current_task()
                info.task = task
                try:
                    # Run with a timeout — the listener never returns
                    # normally; we expect it to exit on CancelledError
                    # when the test cancels.
                    await asyncio.wait_for(
                        sm._extended_post_turn_listener(info.session_id, info),
                        timeout=2.0,
                    )
                except (asyncio.TimeoutError, asyncio.CancelledError):
                    pass

            loop.run_until_complete(run())
            return states, dispatch_calls
        finally:
            loop.close()

    def test_init_after_result_enters_working_before_process_message(self):
        """Bare init delivered after RESULT (ScheduleWakeup wake-up shape)
        must transition state to WORKING (with substatus cleared, since
        the wake-up has fired and we're no longer "awaiting") BEFORE
        _process_message processes the init.  This proves the pre-detect
        block fired."""
        sm = SessionManager()
        info = self._make_session(sm, wakeup_pending=True)

        # One batch: just an init, then a RESULT (very short auto-resume
        # cycle).  In a real flow there'd be assistant content between
        # init and RESULT, but this is the minimal repro of the pre-detect
        # path firing on init.
        init_msg = VibeNodeMessage(
            kind=MessageKind.SYSTEM, subtype='init', data={'model': 'x'}
        )
        result_msg = VibeNodeMessage(
            kind=MessageKind.RESULT,
            session_id=info.session_id,
            duration_ms=10, num_turns=2, cost_usd=0.0,
        )
        states, dispatch_calls = self._run_listener_to_completion(
            sm, info, batches=[[init_msg, result_msg]]
        )

        # Auto-resume must have been entered: somewhere in the trace we
        # must see state='working' (with substatus cleared — the wake-up
        # has fired so we're no longer "awaiting").  The exact order may
        # vary depending on emit timing but the WORKING transition MUST
        # appear, and it MUST NOT carry the stale 'auto-resuming'
        # substatus (that would re-introduce the "stale label" UX bug).
        assert any(s[0] == 'working' for s in states), (
            f"listener did not flip to WORKING on the init — "
            f"the pre-detect path is not firing.  states={states}"
        )
        assert not any(s == ('working', 'auto-resuming') for s in states), (
            f"listener emitted WORKING+auto-resuming — the substatus must "
            f"be cleared by _enter_auto_resume so the UI stops showing "
            f"'Awaiting wake-up…' when the session is actively working. "
            f"states={states}"
        )

    def test_queued_message_not_dispatched_during_wakeup_cycle(self):
        """While a wake-up is pending, the listener's IDLE emits must
        NOT trigger queue auto-dispatch.  This is the user-visible
        symptom of the bug: queued messages used to race the wake-up
        content."""
        sm = SessionManager()
        info = self._make_session(sm, wakeup_pending=True)

        init_msg = VibeNodeMessage(
            kind=MessageKind.SYSTEM, subtype='init', data={'model': 'x'}
        )
        result_msg = VibeNodeMessage(
            kind=MessageKind.RESULT,
            session_id=info.session_id,
            duration_ms=10, num_turns=2, cost_usd=0.0,
        )
        # Two batches: first is the wake-up cycle; second will never
        # arrive (we cancel via timeout) — represents the listener
        # sitting waiting for more wake-ups.
        states, dispatch_calls = self._run_listener_to_completion(
            sm, info, batches=[[init_msg, result_msg]]
        )

        # After the wake-up cycle ends, _enter_auto_resume cleared
        # _wakeup_pending=False, so the RESULT's IDLE emit MAY dispatch
        # normally (no new wake-up was scheduled in this minimal cycle).
        # The thing we're guarding against is the INITIAL listener-entry
        # IDLE emit dispatching while _wakeup_pending was still True.
        # With wakeup_pending=True at listener start, the gate must hold
        # through the init's emit — i.e. at most one dispatch (the
        # post-RESULT one), not two.
        assert len(dispatch_calls) <= 1, (
            "queue was dispatched more than once during the wake-up cycle "
            "— the gate is leaking emits.  Dispatch sites: "
            f"{dispatch_calls}"
        )

    def test_non_wakeup_session_dispatches_normally(self):
        """A session with no pending wake-up must still dispatch its queue
        at the listener's initial IDLE emit.  The gate should not strand
        queued messages on sessions that aren't sleeping."""
        sm = SessionManager()
        info = self._make_session(sm, wakeup_pending=False)

        # No actual SDK content — just cancel the listener after a moment.
        # The initial IDLE emit fires before the listener blocks on
        # receive_response().
        states, dispatch_calls = self._run_listener_to_completion(
            sm, info, batches=[[]]  # empty batch = receive_response returns immediately
        )

        # With _wakeup_pending=False, the gate is open; the initial IDLE
        # emit (and any subsequent IDLE emits) must dispatch.
        assert len(dispatch_calls) >= 1, (
            "non-wake-up session had no dispatch — the gate is over-firing "
            "and stranding queued messages on sessions that aren't sleeping"
        )


# ── 5b. queue_message has its OWN dispatch path that must also gate ─────


class TestQueueMessageGate:
    """``queue_message`` has its own ``_try_dispatch_queue`` call that
    bypasses the ``_emit_state`` gate.  Without gating it too, queueing a
    message during a wake-up sleep window would dispatch immediately and
    re-introduce the race.  This was caught only in the LIVE repro — the
    earlier unit tests passed but the real SDK run exposed the gap."""

    def _make_idle_info(self, *, wakeup_pending: bool, in_post_turn: bool):
        info = SessionInfo(session_id="test-sid")
        info.state = SessionState.IDLE
        info._wakeup_pending = wakeup_pending
        info._in_post_turn = in_post_turn
        return info

    def test_queue_message_suppresses_when_wakeup_pending(self):
        sm = SessionManager()
        info = self._make_idle_info(wakeup_pending=True, in_post_turn=True)
        with sm._lock:
            sm._sessions[info.session_id] = info
        calls = []
        sm._try_dispatch_queue = lambda sid: calls.append(sid)
        sm.queue_message(info.session_id, "follow-up while sleeping")
        assert calls == [], (
            "queue_message dispatched while a wake-up was pending — the "
            "queued message would race the wake-up content"
        )

    def test_queue_message_dispatches_normally_without_wakeup(self):
        sm = SessionManager()
        info = self._make_idle_info(wakeup_pending=False, in_post_turn=True)
        with sm._lock:
            sm._sessions[info.session_id] = info
        calls = []
        sm._try_dispatch_queue = lambda sid: calls.append(sid)
        sm.queue_message(info.session_id, "ordinary follow-up")
        assert calls == [info.session_id], (
            "queue_message should still dispatch immediately for non-wake-up "
            "sessions even with the listener active"
        )

    def test_queue_message_dispatches_when_listener_not_active(self):
        sm = SessionManager()
        # Even if wakeup_pending leaked from a prior turn, the gate is
        # tied to _in_post_turn — and we should dispatch when no listener
        # is active.
        info = self._make_idle_info(wakeup_pending=True, in_post_turn=False)
        with sm._lock:
            sm._sessions[info.session_id] = info
        calls = []
        sm._try_dispatch_queue = lambda sid: calls.append(sid)
        sm.queue_message(info.session_id, "after listener exited")
        assert calls == [info.session_id]


# ── 5c. _process_message resolves stale session IDs (post-remap) ─────────


class TestProcessMessageResolvesSessionId:
    """``_process_message`` must resolve aliased session IDs.  The
    post-turn listener is started with the OLD (pre-remap) session_id —
    after the first turn's RESULT remaps to the SDK-assigned UUID, every
    subsequent ``_process_message`` call from the listener would look up
    the stale ID, find ``None``, and return silently.  The wake-up's
    RESULT then never sets ``state=IDLE`` and the queued message stays
    stranded forever.

    This bug was only caught in the LIVE repro — the original tests
    passed because they exercised _emit_state / queue gating in
    isolation, not the full listener→_process_message chain across a
    remap.
    """

    def test_process_message_resolves_aliased_id(self):
        from daemon.backends.messages import VibeNodeMessage, MessageKind
        sm = SessionManager()
        new_id = "real-sdk-uuid"
        old_id = "temp-id"
        info = SessionInfo(session_id=new_id)
        info.state = SessionState.WORKING
        with sm._lock:
            sm._sessions[new_id] = info
            sm._id_aliases[old_id] = new_id

        # Call _process_message with the OLD id — like the listener would
        # after remap.  Without _resolve_id, info lookup fails and state
        # never transitions; with it, state correctly moves to IDLE.
        msg = VibeNodeMessage(
            kind=MessageKind.RESULT,
            session_id=new_id,
            duration_ms=10, num_turns=1, cost_usd=0.0,
        )
        import asyncio
        loop = asyncio.new_event_loop()
        try:
            loop.run_until_complete(sm._process_message(old_id, msg))
        finally:
            loop.close()
        assert info.state == SessionState.IDLE, (
            "RESULT with stale session_id was silently dropped — the "
            "listener will get stuck on the next wake-up cycle.  See "
            "_process_message's _resolve_id call."
        )


# ── 5d. PreCompact hook surfaces "Compacting…" during the actual work ──


class TestPreCompactHook:
    """The SDK's ``compact_boundary`` message arrives at the END of a
    compaction cycle.  Without an early signal the UI shows "Working…"
    for the entire ~15-second compaction window, then briefly flashes
    "Compacting…" right at the end.  The PreCompact SDK hook fires
    BEFORE compaction starts; SessionManager uses it to flip substatus
    early.  These tests guard the hook plumbing."""

    def test_make_pre_compact_callback_returns_async(self):
        sm = SessionManager()
        cb = sm._make_pre_compact_callback("test-sid")
        assert cb is not None
        import asyncio
        assert asyncio.iscoroutinefunction(cb)

    def test_pre_compact_callback_flips_substatus(self):
        import asyncio
        sm = SessionManager()
        info = SessionInfo(session_id="test-sid")
        info.state = SessionState.WORKING
        info.substatus = ""  # SDK is mid-turn, no signal yet
        with sm._lock:
            sm._sessions[info.session_id] = info
        emits = []
        sm._push_callback = lambda evt, data: emits.append((evt, data))

        cb = sm._make_pre_compact_callback("test-sid")
        loop = asyncio.new_event_loop()
        try:
            loop.run_until_complete(cb({}, None, None))
        finally:
            loop.close()

        assert info.substatus == "compacting", (
            "PreCompact callback failed to flip substatus — the UI would "
            "keep showing 'Working…' through the compaction window instead "
            "of 'Compacting…'"
        )
        assert info.state == SessionState.WORKING
        # Must have emitted state to clients
        state_emits = [d for evt, d in emits if evt == 'session_state']
        assert state_emits, "no session_state emit — UI won't see the change"

    def test_pre_compact_callback_resolves_aliased_id(self):
        """The hook callback captures the session_id at registration
        time.  After the first turn's RESULT remaps the id, the hook
        must still find the session via the alias map."""
        import asyncio
        sm = SessionManager()
        new_id = "real-sdk-uuid"
        old_id = "temp-sid"
        info = SessionInfo(session_id=new_id)
        info.state = SessionState.WORKING
        with sm._lock:
            sm._sessions[new_id] = info
            sm._id_aliases[old_id] = new_id
        sm._push_callback = lambda *a, **kw: None

        cb = sm._make_pre_compact_callback(old_id)
        loop = asyncio.new_event_loop()
        try:
            loop.run_until_complete(cb({}, None, None))
        finally:
            loop.close()

        assert info.substatus == "compacting", (
            "PreCompact callback didn't resolve the stale session_id — "
            "the substatus flip silently dropped on the floor"
        )

    def test_pre_compact_callback_noop_if_already_compacting(self):
        """If the user explicitly hit /compact (optimistic substatus
        already set), the hook firing later shouldn't re-emit a no-op
        state event that confuses the UI."""
        import asyncio
        sm = SessionManager()
        info = SessionInfo(session_id="test-sid")
        info.state = SessionState.WORKING
        info.substatus = "compacting"  # already set by /compact path
        with sm._lock:
            sm._sessions[info.session_id] = info
        emits = []
        sm._push_callback = lambda evt, data: emits.append((evt, data))

        cb = sm._make_pre_compact_callback("test-sid")
        loop = asyncio.new_event_loop()
        try:
            loop.run_until_complete(cb({}, None, None))
        finally:
            loop.close()

        state_emits = [d for evt, d in emits if evt == 'session_state']
        assert not state_emits, (
            "duplicate state emit when substatus was already compacting — "
            "noisy redundant push to clients"
        )

    def test_pre_compact_callback_handles_missing_session(self):
        """If the session was closed between hook registration and
        invocation, the callback must not crash."""
        import asyncio
        sm = SessionManager()
        cb = sm._make_pre_compact_callback("never-existed")
        loop = asyncio.new_event_loop()
        try:
            # Should not raise
            loop.run_until_complete(cb({}, None, None))
        finally:
            loop.close()

    def test_session_options_carries_callback_to_sdk(self):
        """``_drive_session`` must thread the pre_compact_callback
        through to ``SessionOptions`` so the SDK actually registers
        the hook."""
        import inspect
        src = inspect.getsource(SessionManager._drive_session)
        assert 'pre_compact_callback=' in src, (
            "_drive_session no longer wires pre_compact_callback into "
            "SessionOptions — the PreCompact hook will never register, "
            "and 'Compacting…' regression returns for auto-compact"
        )


# ── 6. User-driven turn start resets _wakeup_pending ────────────────────


class TestUserDrivenTurnResetsWakeup:
    """`_send_query` and `_drive_session` must clear ``_wakeup_pending``
    at the start of a user-driven turn so a previous turn's scheduled
    wake-up doesn't keep the dispatch gate latched after the user
    explicitly takes over with a new query."""

    def test_send_query_clears_wakeup_pending(self):
        src = inspect.getsource(SessionManager._send_query)
        assert "info._wakeup_pending = False" in src, (
            "_send_query no longer resets _wakeup_pending — a session "
            "that had a pending wake-up will keep gating queue dispatch "
            "after the user sends a new message that supersedes the "
            "wake-up"
        )

    def test_drive_session_clears_wakeup_pending(self):
        src = inspect.getsource(SessionManager._drive_session)
        assert "info._wakeup_pending = False" in src, (
            "_drive_session no longer resets _wakeup_pending — crash "
            "recovery / first-turn flows could inherit a stale flag from "
            "a previous incarnation of the same session_id"
        )


# ── 6b. Compacting substatus cleared at init ────────────────────────────


class TestCompactingSubstatusClearedAtInit:
    """The "Compacting…" UI label must clear when ``init`` arrives (the
    SDK's signal that compaction has FINISHED and a new context is
    ready), NOT after the agent's first post-compact ``ASSISTANT`` block.

    Earlier design preserved the substatus through init to span the
    "perceived compaction window" (the seconds during which the agent
    rebuilds context).  In practice this produced the user-reported
    bug: "it comes out of compacting and it still shows compacting" —
    the session is technically out of compacting but the UI still says
    "Compacting…".  Same shape as the wake-up sleeping UX bug fixed in
    `_enter_auto_resume`: once the state machine has moved past the
    event the substatus was describing, the label stops being honest.
    """

    def _mk(self):
        sm = SessionManager()
        info = SessionInfo(session_id="test-sid")
        info.state = SessionState.WORKING
        with sm._lock:
            sm._sessions[info.session_id] = info
        sm._push_callback = lambda *a, **kw: None
        return sm, info

    def test_init_clears_compacting_substatus(self):
        """``init`` arriving while substatus='compacting' must CLEAR the
        substatus (compaction has finished — SDK has emitted the new
        context).  Holding "Compacting…" through the agent's
        context-rebuild period after init was the stale-label bug."""
        from daemon.backends.messages import VibeNodeMessage, MessageKind
        sm, info = self._mk()
        info.substatus = "compacting"
        info._post_compact_init_seen = False
        msg = VibeNodeMessage(kind=MessageKind.SYSTEM, subtype="init",
                              data={"model": "claude-haiku-4-5"})
        import asyncio
        asyncio.new_event_loop().run_until_complete(
            sm._process_message(info.session_id, msg)
        )
        assert info.substatus == "", (
            "init failed to clear 'compacting' — UI will keep showing "
            "'Compacting…' for several seconds while the agent is "
            "rebuilding context, lying about what's actually happening"
        )
        assert info._post_compact_init_seen is False, (
            "_post_compact_init_seen must stay False after init clears "
            "substatus directly — leaving it True risks the dead "
            "ASSISTANT-handler branch acting on stale state"
        )

    def test_assistant_clears_compacting_after_init_only(self):
        """First ASSISTANT block after post-compact init clears
        'compacting'.  This is the perceptual signal that compaction
        is truly done — the agent is producing new content."""
        from daemon.backends.messages import VibeNodeMessage, MessageKind
        sm, info = self._mk()
        info.substatus = "compacting"
        info._post_compact_init_seen = True
        msg = VibeNodeMessage(
            kind=MessageKind.ASSISTANT,
            blocks=[{"kind": "text", "text": "Resuming work..."}],
        )
        import asyncio
        asyncio.new_event_loop().run_until_complete(
            sm._process_message(info.session_id, msg)
        )
        assert info.substatus == "", (
            "ASSISTANT block after post-compact init did NOT clear "
            "'compacting' — UI will stay stuck on 'Compacting…' forever"
        )
        assert info._post_compact_init_seen is False, (
            "_post_compact_init_seen not reset after clearing substatus"
        )

    def test_assistant_does_not_clear_compacting_before_init(self):
        """An ASSISTANT block arriving BETWEEN compact_boundary and init
        (late-streaming pre-compact content) must NOT clear the substatus.
        Otherwise the UI flashes back to "Working…" mid-compaction, which
        is the bug the existing comment guarded against."""
        from daemon.backends.messages import VibeNodeMessage, MessageKind
        sm, info = self._mk()
        info.substatus = "compacting"
        info._post_compact_init_seen = False  # init has NOT been seen yet
        msg = VibeNodeMessage(
            kind=MessageKind.ASSISTANT,
            blocks=[{"kind": "text", "text": "Late pre-compact chunk"}],
        )
        import asyncio
        asyncio.new_event_loop().run_until_complete(
            sm._process_message(info.session_id, msg)
        )
        assert info.substatus == "compacting", (
            "ASSISTANT arriving BEFORE post-compact init incorrectly "
            "cleared 'compacting' — the guard against pre-compact "
            "streaming has regressed"
        )

    def test_post_turn_compact_does_not_carry_init_seen_flag(self):
        """Post-turn auto-compact path: substatus is cleared via
        _emit_state's IDLE auto-clear, not via an ASSISTANT block.
        The flag must be reset so a later spurious ASSISTANT (self-heal
        retry, etc.) doesn't try to act on stale state."""
        from daemon.backends.messages import VibeNodeMessage, MessageKind
        sm, info = self._mk()
        info.substatus = "compacting"
        info._post_compact_init_seen = False
        info._awaiting_compact_drain = True  # post-turn marker
        msg = VibeNodeMessage(kind=MessageKind.SYSTEM, subtype="init",
                              data={"model": "claude-haiku-4-5"})
        import asyncio
        asyncio.new_event_loop().run_until_complete(
            sm._process_message(info.session_id, msg)
        )
        # Post-turn path: state went IDLE inside init handler, then
        # _emit_state's auto-clear stripped the substatus.
        assert info.state == SessionState.IDLE
        assert info.substatus == "", (
            "Post-turn compact: substatus should have been cleared by "
            "the IDLE auto-clear in _emit_state"
        )
        assert info._post_compact_init_seen is False, (
            "Post-turn compact left _post_compact_init_seen=True — a "
            "later ASSISTANT (self-heal) would try to clear a substatus "
            "that's already cleared, masking real bugs"
        )


# ── 7. Source-level guards (cheap, catch regressions) ───────────────────


class TestListenerSourceGuards:
    """Cheap source-level assertions that catch regressions where the
    pre-detect block or the _in_post_turn flag handling get accidentally
    removed by a future 'cleanup' PR.

    These are belt-and-suspenders to the behavioral tests above — they
    add no coverage but make refactoring intent loud."""

    def test_pre_detect_block_present_in_listener(self):
        src = inspect.getsource(SessionManager._extended_post_turn_listener)
        assert "Pre-detect auto-resume BEFORE _process_message" in src, (
            "the pre-detect marker comment was removed from "
            "_extended_post_turn_listener — the wake-up race fix is at "
            "risk of being silently reverted"
        )
        # Order check: _enter_auto_resume must be called BEFORE the first
        # _process_message call in the inner loop.
        async_for_idx = src.find("async for msg in self._sdk.receive_response")
        enter_idx = src.find("self._enter_auto_resume(info)", async_for_idx)
        process_idx = src.find("await self._process_message", async_for_idx)
        assert async_for_idx >= 0 and enter_idx >= 0 and process_idx >= 0
        assert enter_idx < process_idx, (
            "_enter_auto_resume is called AFTER _process_message inside "
            "the listener loop — the pre-detect fix has regressed"
        )

    def test_in_post_turn_set_in_listener(self):
        src = inspect.getsource(SessionManager._extended_post_turn_listener)
        assert "info._in_post_turn = True" in src
        assert "info._in_post_turn = False" in src, (
            "_in_post_turn is set True but never cleared — wake-up "
            "gating will leak across turns"
        )

    def test_emit_state_gate_uses_both_flags(self):
        src = inspect.getsource(SessionManager._emit_state)
        # Both flags must appear in the gate.  If only one is checked,
        # we either over-suppress (strand queues on every session) or
        # under-suppress (re-introduce the wake-up race).
        assert "_in_post_turn" in src
        assert "_wakeup_pending" in src


# ── 8. Starvation watchdog — queue can NEVER be stranded by a wake-up loop ──


class TestWakeupQueueStarvationWatchdog:
    """Regression guard for the 2026-06-04 incident: an agent that
    re-scheduled a background Bash on every turn kept ``_wakeup_pending``
    armed at every RESULT, so the dispatch gate never reopened and 4 queued
    user messages were starved across an entire night.

    ``_wakeup_queue_watchdog`` bounds that wait: once a queued message has
    been blocked behind the gate longer than
    ``_WAKEUP_QUEUE_STARVATION_TIMEOUT`` it force-dispatches via the same
    supersede path a manual user send uses.
    """

    def _make_session(self, sm, *, in_post_turn, wakeup_pending,
                      state=SessionState.IDLE):
        info = SessionInfo(session_id="starve-sid")
        info.state = state
        info._in_post_turn = in_post_turn
        info._wakeup_pending = wakeup_pending
        with sm._lock:
            sm._sessions[info.session_id] = info
        return info

    def _stub(self, sm, info, queue):
        """Stub queue access + dispatch so the watchdog runs in isolation
        (no event loop / no disk)."""
        sm._mq.get_queue_data = lambda sid: list(queue) if sid == info.session_id else []
        calls = []
        sm._try_dispatch_queue = lambda sid: calls.append(sid)
        return calls

    def test_arms_then_force_dispatches_after_timeout(self):
        sm = SessionManager()
        info = self._make_session(sm, in_post_turn=True, wakeup_pending=True)
        calls = self._stub(sm, info, ["queued msg"])

        # First sweep: arms the timer, does NOT dispatch yet.
        sm._wakeup_queue_watchdog()
        assert info._wakeup_queue_blocked_since > 0, "watchdog failed to arm"
        assert calls == [], "watchdog dispatched before the timeout elapsed"
        assert info._force_queue_dispatch is False

        # Simulate the starvation window having elapsed.
        info._wakeup_queue_blocked_since = (
            time.time() - sm._WAKEUP_QUEUE_STARVATION_TIMEOUT - 1
        )
        sm._wakeup_queue_watchdog()
        assert info._force_queue_dispatch is True, (
            "watchdog did not open the gate after the starvation timeout — "
            "the queue would stay stranded behind the wake-up loop"
        )
        assert calls == [info.session_id], (
            "watchdog did not force-dispatch the starved queue while idle"
        )

    def test_rescues_phantom_wakeup_with_dead_listener(self):
        """THE bug 'those wake-ups never fire': an IDLE session with a
        backlog and NO live listener / NO wake-up flags set must still be
        rescued.  The watchdog must not depend on _in_post_turn or
        _wakeup_pending being set — otherwise a wake-up that never fires
        (after the listener died) strands the queue forever."""
        sm = SessionManager()
        info = self._make_session(sm, in_post_turn=False, wakeup_pending=False)
        calls = self._stub(sm, info, ["queued msg"])
        # First sweep arms purely on the presence of an undrained backlog.
        sm._wakeup_queue_watchdog()
        assert info._wakeup_queue_blocked_since > 0
        assert calls == []
        # Past the timeout it dispatches even with both wake-up flags False.
        info._wakeup_queue_blocked_since = (
            time.time() - sm._WAKEUP_QUEUE_STARVATION_TIMEOUT - 1
        )
        sm._wakeup_queue_watchdog()
        assert calls == [info.session_id], (
            "watchdog failed to rescue an idle backlog when no wake-up was "
            "pending and no listener was alive — phantom wake-ups would "
            "strand the queue forever"
        )

    def test_arms_on_any_undrained_backlog(self):
        """Arming is driven purely by an undrained backlog on an alive
        session, independent of the wake-up flags."""
        sm = SessionManager()
        info = self._make_session(sm, in_post_turn=False, wakeup_pending=False)
        self._stub(sm, info, ["queued msg"])
        sm._wakeup_queue_watchdog()
        assert info._wakeup_queue_blocked_since > 0

    def test_stopped_session_not_dispatched(self):
        """A STOPPED session must never be force-dispatched — it has no live
        client to run anything."""
        sm = SessionManager()
        info = self._make_session(sm, in_post_turn=True, wakeup_pending=True,
                                  state=SessionState.STOPPED)
        info._wakeup_queue_blocked_since = (
            time.time() - sm._WAKEUP_QUEUE_STARVATION_TIMEOUT - 1
        )
        calls = self._stub(sm, info, ["queued msg"])
        sm._wakeup_queue_watchdog()
        assert calls == []

    def test_interrupted_session_not_dispatched(self):
        """A freshly-interrupted session is being torn down — the watchdog
        must not resurrect its queue."""
        sm = SessionManager()
        info = self._make_session(sm, in_post_turn=True, wakeup_pending=True)
        info._interrupted = True
        info._wakeup_queue_blocked_since = (
            time.time() - sm._WAKEUP_QUEUE_STARVATION_TIMEOUT - 1
        )
        calls = self._stub(sm, info, ["queued msg"])
        sm._wakeup_queue_watchdog()
        assert calls == []

    def test_disarms_when_queue_drains(self):
        sm = SessionManager()
        info = self._make_session(sm, in_post_turn=True, wakeup_pending=True)
        info._wakeup_queue_blocked_since = time.time() - 5
        # Queue is now empty → watchdog must disarm and never dispatch.
        calls = self._stub(sm, info, [])
        sm._wakeup_queue_watchdog()
        assert info._wakeup_queue_blocked_since == 0.0
        assert calls == []

    def test_keeps_armed_across_sweeps_until_drained(self):
        """The timer must NOT reset between sweeps while the loop persists —
        otherwise a tight wake-up loop would re-arm from zero every sweep
        and never reach the timeout (the original starvation)."""
        sm = SessionManager()
        info = self._make_session(sm, in_post_turn=True, wakeup_pending=True)
        self._stub(sm, info, ["a", "b"])
        sm._wakeup_queue_watchdog()
        armed_at = info._wakeup_queue_blocked_since
        assert armed_at > 0
        # A second sweep while still blocked must preserve the arm time.
        sm._wakeup_queue_watchdog()
        assert info._wakeup_queue_blocked_since == armed_at, (
            "watchdog reset its own timer mid-loop — it would never fire"
        )

    def test_sets_flag_but_defers_dispatch_when_working(self):
        """If the session is mid wake-up turn (WORKING) the watchdog sets
        the override flag but must NOT call dispatch directly — the next
        post-RESULT IDLE emit delivers it."""
        sm = SessionManager()
        info = self._make_session(sm, in_post_turn=True, wakeup_pending=True,
                                  state=SessionState.WORKING)
        info._wakeup_queue_blocked_since = (
            time.time() - sm._WAKEUP_QUEUE_STARVATION_TIMEOUT - 1
        )
        calls = self._stub(sm, info, ["queued msg"])
        sm._wakeup_queue_watchdog()
        assert info._force_queue_dispatch is True
        assert calls == [], "must not dispatch directly while WORKING"

    def test_emit_state_force_flag_opens_gate(self):
        """With _force_queue_dispatch set, _emit_state must dispatch even
        though _in_post_turn + _wakeup_pending are both True."""
        sm = SessionManager()
        info = SessionInfo(session_id="force-sid")
        info.state = SessionState.IDLE
        info._in_post_turn = True
        info._wakeup_pending = True
        info._force_queue_dispatch = True
        with sm._lock:
            sm._sessions[info.session_id] = info
        calls = []
        sm._try_dispatch_queue = lambda sid: calls.append(sid)
        sm._emit_state(info)
        assert calls == [info.session_id], (
            "force-dispatch override did not open the _emit_state gate"
        )

    def test_queue_message_force_flag_opens_gate(self):
        sm = SessionManager()
        info = SessionInfo(session_id="force-sid")
        info.state = SessionState.IDLE
        info._in_post_turn = True
        info._wakeup_pending = True
        info._force_queue_dispatch = True
        with sm._lock:
            sm._sessions[info.session_id] = info
        calls = []
        sm._try_dispatch_queue = lambda sid: calls.append(sid)
        sm.queue_message(info.session_id, "msg")
        assert calls == [info.session_id], (
            "force-dispatch override did not open the queue_message gate"
        )

    def test_send_query_clears_force_dispatch(self):
        src = inspect.getsource(SessionManager._send_query)
        assert "info._force_queue_dispatch = False" in src, (
            "_send_query no longer consumes the starvation override — the "
            "flag would latch and dispatch could double-fire"
        )

    def test_emit_state_gate_includes_force_override(self):
        src = inspect.getsource(SessionManager._emit_state)
        assert "_force_queue_dispatch" in src, (
            "the _emit_state gate dropped the starvation override — a "
            "wake-up loop could once again strand the queue forever"
        )


# ── 9. Phantom wake-up — 'Awaiting wake-up…' can NEVER hang forever ─────────


class TestPhantomWakeupDeadline:
    """Hard guarantee: a scheduled wake-up that never fires must never leave
    the session stuck on 'Awaiting wake-up…'.  At RESULT a deadline is armed;
    past it the watchdog clears the awaiting state back to plain Idle — even
    with NO queued messages (the pure stuck-indicator case the user raged
    about: 'a waiting wake up that never wakes up')."""

    def _make_awaiting(self, sm, *, deadline_offset, queue=None,
                       state=SessionState.IDLE):
        info = SessionInfo(session_id="phantom-sid")
        info.state = state
        info._wakeup_pending = True
        info.substatus = "auto-resuming"
        info._wakeup_deadline = time.time() + deadline_offset
        with sm._lock:
            sm._sessions[info.session_id] = info
        q = list(queue or [])
        sm._mq.get_queue_data = lambda sid: list(q) if sid == info.session_id else []
        sm._push_callback = None
        return info

    def test_phantom_cleared_after_deadline_no_queue(self):
        sm = SessionManager()
        info = self._make_awaiting(sm, deadline_offset=-10, queue=[])
        sm._try_dispatch_queue = lambda sid: None
        sm._wakeup_queue_watchdog()
        assert info.substatus == "", (
            "phantom wake-up past its deadline did NOT clear the "
            "'Awaiting wake-up…' substatus — the indicator would hang forever"
        )
        assert info._wakeup_pending is False
        assert info._wakeup_deadline == 0.0

    def test_awaiting_preserved_before_deadline(self):
        """A real wake-up still pending (deadline in the future) must NOT be
        cleared — we only kill it once it's overdue."""
        sm = SessionManager()
        info = self._make_awaiting(sm, deadline_offset=+1000, queue=[])
        sm._try_dispatch_queue = lambda sid: None
        sm._wakeup_queue_watchdog()
        assert info.substatus == "auto-resuming"
        assert info._wakeup_pending is True

    def test_phantom_clear_also_dispatches_queue(self):
        sm = SessionManager()
        info = self._make_awaiting(sm, deadline_offset=-10, queue=["m1"])
        calls = []
        sm._try_dispatch_queue = lambda sid: calls.append(sid)
        sm._wakeup_queue_watchdog()
        assert info.substatus == ""
        assert info._wakeup_pending is False
        assert info._force_queue_dispatch is True
        assert calls == [info.session_id], (
            "phantom clear with a backlog must also drain the queue"
        )

    def test_phantom_not_cleared_when_working(self):
        """If the session is actively WORKING (the wake-up DID fire and is
        streaming), the deadline check must not fire."""
        sm = SessionManager()
        info = self._make_awaiting(sm, deadline_offset=-10, queue=[],
                                   state=SessionState.WORKING)
        sm._try_dispatch_queue = lambda sid: None
        sm._wakeup_queue_watchdog()
        # WORKING means the wake-up fired; branch A only acts on IDLE.
        assert info.state == SessionState.WORKING

    def test_expected_delay_schedule_wakeup_uses_delay(self):
        d = SessionManager._wakeup_expected_delay(
            "ScheduleWakeup", {"delaySeconds": 300}
        )
        assert d == 300.0

    def test_expected_delay_schedule_wakeup_clamped(self):
        # Mirrors the SDK [60, 3600] clamp.
        assert SessionManager._wakeup_expected_delay(
            "ScheduleWakeup", {"delaySeconds": 999999}) == 3600.0
        assert SessionManager._wakeup_expected_delay(
            "ScheduleWakeup", {"delaySeconds": 5}) == 60.0

    def test_expected_delay_background_bash_uses_ceiling(self):
        d = SessionManager._wakeup_expected_delay(
            "Bash", {"command": "server", "run_in_background": True}
        )
        assert d == SessionManager._WAKEUP_UNKNOWN_DELAY_WAIT

    def test_result_branch_arms_deadline_when_wakeup_pending(self):
        """The RESULT branch must arm _wakeup_deadline from the per-turn
        expected delay so the watchdog has something to enforce."""
        from daemon.backends.messages import VibeNodeMessage, MessageKind
        sm = SessionManager()
        info = SessionInfo(session_id="arm-sid")
        info.state = SessionState.WORKING
        info._wakeup_pending = True          # a wake-up tool fired this turn
        info._wakeup_max_delay = 300.0       # accumulated in tool_use handler
        with sm._lock:
            sm._sessions[info.session_id] = info
        sm._push_callback = lambda *a, **k: None
        msg = VibeNodeMessage(
            kind=MessageKind.RESULT, session_id=info.session_id,
            duration_ms=10, num_turns=1, cost_usd=0.0,
        )
        loop = asyncio.new_event_loop()
        try:
            loop.run_until_complete(sm._process_message(info.session_id, msg))
        finally:
            loop.close()
        expected = 300.0 + SessionManager._WAKEUP_DEADLINE_GRACE
        assert info._wakeup_deadline > time.time() + expected - 30, (
            "RESULT branch did not arm a phantom-wake-up deadline"
        )
        assert info._wakeup_max_delay == 0.0, (
            "per-turn delay accumulator was not reset at RESULT"
        )

    def test_background_bash_does_not_show_awaiting_wakeup(self):
        """THE root mislabel: a background-Bash-only turn must NOT display
        'Awaiting wake-up…' (substatus stays empty / plain Idle), because a
        background process is not a scheduled wake-up.  The queue is still
        gated for race-safety (_wakeup_pending True) but the UI is honest."""
        from daemon.backends.messages import VibeNodeMessage, MessageKind
        sm = SessionManager()
        info = SessionInfo(session_id="bg-sid")
        info.state = SessionState.WORKING
        info._wakeup_pending = True          # set by the bg-Bash tool_use
        info._wakeup_is_scheduled = False    # ...but it was NOT a ScheduleWakeup
        with sm._lock:
            sm._sessions[info.session_id] = info
        sm._push_callback = lambda *a, **k: None
        msg = VibeNodeMessage(
            kind=MessageKind.RESULT, session_id=info.session_id,
            duration_ms=10, num_turns=1, cost_usd=0.0,
        )
        loop = asyncio.new_event_loop()
        try:
            loop.run_until_complete(sm._process_message(info.session_id, msg))
        finally:
            loop.close()
        assert info.substatus == "", (
            "background Bash showed 'Awaiting wake-up…' — it is not a "
            "scheduled wake-up and must render as plain Idle"
        )
        # Gate still active (race-safety) and deadline armed (bounded by the
        # watchdog), but no misleading label.
        assert info._wakeup_pending is True
        assert info._wakeup_deadline > 0

    def test_schedule_wakeup_does_show_awaiting_wakeup(self):
        """A real ScheduleWakeup DOES show 'Awaiting wake-up…' — there is a
        genuine timer the CLI will fire."""
        from daemon.backends.messages import VibeNodeMessage, MessageKind
        sm = SessionManager()
        info = SessionInfo(session_id="sched-sid")
        info.state = SessionState.WORKING
        info._wakeup_pending = True
        info._wakeup_is_scheduled = True
        info._wakeup_max_delay = 300.0
        with sm._lock:
            sm._sessions[info.session_id] = info
        sm._push_callback = lambda *a, **k: None
        msg = VibeNodeMessage(
            kind=MessageKind.RESULT, session_id=info.session_id,
            duration_ms=10, num_turns=1, cost_usd=0.0,
        )
        loop = asyncio.new_event_loop()
        try:
            loop.run_until_complete(sm._process_message(info.session_id, msg))
        finally:
            loop.close()
        assert info.substatus == "auto-resuming", (
            "real ScheduleWakeup should still show 'Awaiting wake-up…'"
        )

    def test_is_scheduled_wakeup_classifier(self):
        assert SessionManager._is_scheduled_wakeup("ScheduleWakeup")
        assert SessionManager._is_scheduled_wakeup("schedule_wakeup")
        assert SessionManager._is_scheduled_wakeup("ScheduleWakeUp")
        # Background Bash and unrelated tools are NOT scheduled wake-ups.
        assert not SessionManager._is_scheduled_wakeup("Bash")
        assert not SessionManager._is_scheduled_wakeup("Edit")

    def test_result_branch_clears_deadline_when_no_wakeup(self):
        from daemon.backends.messages import VibeNodeMessage, MessageKind
        sm = SessionManager()
        info = SessionInfo(session_id="noarm-sid")
        info.state = SessionState.WORKING
        info._wakeup_pending = False
        info._wakeup_deadline = time.time() + 999  # stale from a prior turn
        with sm._lock:
            sm._sessions[info.session_id] = info
        sm._push_callback = lambda *a, **k: None
        msg = VibeNodeMessage(
            kind=MessageKind.RESULT, session_id=info.session_id,
            duration_ms=10, num_turns=1, cost_usd=0.0,
        )
        loop = asyncio.new_event_loop()
        try:
            loop.run_until_complete(sm._process_message(info.session_id, msg))
        finally:
            loop.close()
        assert info._wakeup_deadline == 0.0, (
            "RESULT with no pending wake-up must clear any stale deadline"
        )
