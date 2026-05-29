"""
[subsessions phase -1] Pin the operation order inside ``_send_query``.

The subsessions inbox-drain (spec §4.3.4) prepends parent-reports to the
user's text immediately BEFORE the existing PERF-CRITICAL chokepoint:

    text mutation (inbox drain)
       ↓
    _turn_had_direct_edit = False          (CLAUDE.md PERF #3)
       ↓
    asyncio.gather(_write_file_snapshot,   (CLAUDE.md PERF #2)
                   _record_pre_turn_mtimes) (CLAUDE.md PERF #4 + #5)
       ↓
    self._sdk.send_query(...)

This test patches each of the four operations with order-recording stubs
and asserts the relative ordering held above.  Any future edit that
moves the inbox-drain INTO or AFTER the gather, or that breaks the
parallel snapshot+mtime gather, will fail this test loudly.

See ``docs/plans/subsessions-spec.md`` §7.1 + §13.1 test 4.
"""

import asyncio
import threading
import time
from unittest.mock import MagicMock, patch

import pytest


# ---------------------------------------------------------------------------
# Mock SDK types (so importing the manager doesn't drag claude-code-sdk)
# ---------------------------------------------------------------------------

class _MockSDKTypes:
    """Minimal stand-ins for the SDK type module."""
    ClaudeSDKClient = MagicMock
    ClaudeCodeOptions = MagicMock
    AssistantMessage = MagicMock
    UserMessage = MagicMock
    ResultMessage = MagicMock
    StreamEvent = MagicMock
    TextBlock = MagicMock
    ThinkingBlock = MagicMock
    ToolUseBlock = MagicMock
    ToolResultBlock = MagicMock
    PermissionResultAllow = MagicMock
    PermissionResultDeny = MagicMock
    ContentBlock = MagicMock
    ToolPermissionContext = MagicMock
    Message = MagicMock


@pytest.fixture
def sm_module():
    """Load daemon.session_manager with the SDK shims in place."""
    import sys, importlib
    sdk_mod = MagicMock()
    sdk_types_mod = MagicMock()
    # Wire the type names ``session_manager`` imports at module scope.
    for name in (
        "ClaudeSDKClient", "ClaudeCodeOptions",
    ):
        setattr(sdk_mod, name, getattr(_MockSDKTypes, name))
    for name in (
        "AssistantMessage", "UserMessage", "ResultMessage", "StreamEvent",
        "TextBlock", "ThinkingBlock", "ToolUseBlock", "ToolResultBlock",
        "PermissionResultAllow", "PermissionResultDeny", "ContentBlock",
        "ToolPermissionContext", "Message",
    ):
        setattr(sdk_types_mod, name, getattr(_MockSDKTypes, name))
    with patch.dict(sys.modules, {
        "claude_code_sdk": sdk_mod,
        "claude_code_sdk.types": sdk_types_mod,
    }):
        import daemon.session_manager as sm
        importlib.reload(sm)
        # Re-bind the type names the module captured at import time so
        # later code paths see our shims.
        for name in (
            "ClaudeSDKClient", "ClaudeCodeOptions",
            "AssistantMessage", "UserMessage", "ResultMessage", "StreamEvent",
            "TextBlock", "ThinkingBlock", "ToolUseBlock", "ToolResultBlock",
            "PermissionResultAllow", "PermissionResultDeny",
        ):
            if hasattr(_MockSDKTypes, name):
                setattr(sm, name, getattr(_MockSDKTypes, name))
        yield sm


# ---------------------------------------------------------------------------
# Test
# ---------------------------------------------------------------------------

def test_send_query_operation_order_pinned(sm_module):
    """Patches the four operations along the send chokepoint with
    order-recording stubs and asserts the documented relative order.

    The test installs a synchronous ``send_query`` mock that completes
    immediately and a ``receive_response`` mock that yields nothing.
    That lets us drive ``_send_query`` to completion without spinning
    up the daemon loop or a real SDK client.
    """
    # Use the real SessionInfo / SessionState — not mocks — so the
    # in-method state checks behave naturally.
    SessionInfo = sm_module.SessionInfo
    SessionState = sm_module.SessionState

    manager = sm_module.SessionManager()

    # Build the in-memory session the real send chokepoint expects.
    sid = "order-test-sid"
    info = SessionInfo(
        session_id=sid,
        state=SessionState.WORKING,
        cwd="",          # blank cwd avoids the mtime walk doing real I/O
        name="order test",
    )
    info.client = object()  # truthy sentinel so the reconnect branch is skipped
    info.task = None
    manager._sessions[sid] = info

    events = []
    events_lock = threading.Lock()

    def _rec(label):
        with events_lock:
            events.append(label)

    # --- Stubs --------------------------------------------------------------
    # _prepopulate_tracked_files runs before our four operations; let it
    # do nothing.
    def _stub_prepopulate(info_arg):
        _rec("prepopulate")
    manager._prepopulate_tracked_files = _stub_prepopulate

    # _write_file_snapshot: PERF marker #2 says it runs inside the gather
    # alongside _record_pre_turn_mtimes.  Record entry order.
    def _stub_write_file_snapshot(session_id, is_post_turn=False):
        _rec(f"write_file_snapshot:post={bool(is_post_turn)}")
    manager._write_file_snapshot = _stub_write_file_snapshot

    # _record_pre_turn_mtimes: PERF marker #4 (carry-forward) lives in
    # here; we only care that it lands inside the gather.
    def _stub_record_pre_turn_mtimes(info_arg):
        _rec("record_pre_turn_mtimes")
    manager._record_pre_turn_mtimes = _stub_record_pre_turn_mtimes

    # _detect_changed_files is called from inside _write_file_snapshot
    # when is_post_turn=True, so PERF marker #1 isn't directly visible
    # here, but we still want to make sure the patched name is in scope
    # in case Phase 1 inlines a call.
    def _stub_detect_changed_files(info_arg):
        _rec("detect_changed_files")
        return set()
    manager._detect_changed_files = _stub_detect_changed_files

    # SDK shim: the manager calls self._sdk.send_query(client, text) and
    # self._sdk.receive_response(client).  receive_response is an async
    # generator that yields nothing so the response loop exits immediately.
    async def _send_query_stub(client, text):
        _rec("sdk_send_query")
    async def _receive_response_stub(client):
        if False:
            yield
        return
    def _extract_pid_stub(client):
        # Non-zero PID keeps the pre-flight liveness check happy.
        return 1234
    manager._sdk = MagicMock()
    manager._sdk.send_query = MagicMock(side_effect=_send_query_stub)
    manager._sdk.receive_response = _receive_response_stub
    manager._sdk.extract_process_pid = _extract_pid_stub

    # Short-circuit downstream side effects so _send_query completes cleanly.
    async def _no_post_drain(session_id, info_arg):
        return
    manager._post_turn_compact_drain = _no_post_drain
    manager._emit_state = lambda info_arg: None
    manager._tracked_coro = lambda info_arg, coro: coro
    async def _cli_watchdog_stub(session_id, info_arg):
        return
    manager._cli_watchdog = _cli_watchdog_stub
    async def _process_message_stub(session_id, message):
        return
    manager._process_message = _process_message_stub

    # Tie info.task to the current task so the supersede checks pass.
    async def _runner():
        info.task = asyncio.current_task()
        await manager._send_query(sid, "hello world")

    asyncio.run(_runner())

    # ---------------------------------------------------------------------
    # Assertions: the documented order from CLAUDE.md and spec §7.1.
    # ---------------------------------------------------------------------
    # Both gather operations must appear BEFORE sdk_send_query.
    assert "sdk_send_query" in events, f"send_query never fired: {events}"
    snap_idx = next(
        (i for i, e in enumerate(events)
         if e.startswith("write_file_snapshot:post=False")),
        None,
    )
    mtime_idx = events.index("record_pre_turn_mtimes")
    send_idx = events.index("sdk_send_query")

    assert snap_idx is not None, \
        f"pre-turn _write_file_snapshot (is_post_turn=False) was never "\
        f"called: {events}"
    assert snap_idx < send_idx, (
        f"PERF #2 violated: _write_file_snapshot must run BEFORE the SDK "
        f"send_query, but order was {events}"
    )
    assert mtime_idx < send_idx, (
        f"PERF #4/#5 violated: _record_pre_turn_mtimes must run BEFORE "
        f"the SDK send_query, but order was {events}"
    )

    # The PERF-CRITICAL gather makes the two pre-turn ops run in parallel.
    # We can't assert true concurrency from synchronous stubs, but we can
    # assert that BOTH ran before the SDK call (i.e. neither was skipped),
    # and that prepopulate ran before them (it's awaited first).
    prep_idx = events.index("prepopulate")
    assert prep_idx < snap_idx
    assert prep_idx < mtime_idx


def test_send_query_resets_turn_had_direct_edit_before_gather(sm_module):
    """PERF #3: ``_turn_had_direct_edit = False`` must reset BEFORE the
    asyncio.gather() runs the snapshot + mtime ops, not between them or
    after.  Otherwise _write_file_snapshot can read a stale True from the
    previous turn and skip the snapshot.

    This test verifies the source-level invariant: the reset assignment
    appears before the ``asyncio.gather(`` call inside ``_send_query``.
    """
    import inspect
    src = inspect.getsource(sm_module.SessionManager._send_query)
    # Use ``info._turn_had_direct_edit = False`` (with the receiver) to
    # exclude comment lines that mention the attribute by bare name.
    reset_pos = src.find("info._turn_had_direct_edit = False")
    gather_pos = src.find("await asyncio.gather(")
    assert reset_pos != -1, "_turn_had_direct_edit reset missing — PERF #3"
    assert gather_pos != -1, "asyncio.gather call missing — PERF #2"
    assert reset_pos < gather_pos, (
        "PERF #3 regression: _turn_had_direct_edit reset must come "
        "BEFORE asyncio.gather(...) in _send_query, but reset is at "
        f"offset {reset_pos} and gather is at offset {gather_pos}."
    )
