"""
End-to-end integration test for the Subsessions feature (spec §8).

Flow exercised:
  1. Build a SessionManager with a fake parent SessionInfo in IDLE.
  2. Write a subsession report into the parent's inbox via the
     daemon.subsession_inbox module directly (simulating what the
     report-to-parent endpoint does).
  3. Mark the parent's in-memory inbox_dirty flag.
  4. Call SessionManager.send_message on the parent.
  5. Assert the SDK saw the prepended block before "[Your message]"
     and that the on-disk inbox entries were marked delivered.

This test sits across the Phase-3 inbox storage and the Phase-4
send_message drain.  It does NOT spin up a real SDK client — the
session is rigged with a stub client so the dispatch path runs
through to the point where _send_query reads the text, and we
capture that text via a stub on self._sdk.send_query.
"""

import asyncio
import sys
import threading
from unittest.mock import MagicMock, patch

import pytest


# ---------------------------------------------------------------------------
# SDK shim — keep imports lightweight
# ---------------------------------------------------------------------------

class _MockSDKTypes:
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
    import importlib

    sdk_mod = MagicMock()
    sdk_types_mod = MagicMock()
    for name in ("ClaudeSDKClient", "ClaudeCodeOptions"):
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
        for name in (
            "ClaudeSDKClient", "ClaudeCodeOptions",
            "AssistantMessage", "UserMessage", "ResultMessage", "StreamEvent",
            "TextBlock", "ThinkingBlock", "ToolUseBlock", "ToolResultBlock",
            "PermissionResultAllow", "PermissionResultDeny",
        ):
            if hasattr(_MockSDKTypes, name):
                setattr(sm, name, getattr(_MockSDKTypes, name))
        yield sm


@pytest.fixture
def parent_with_inbox(sm_module):
    """Return (manager, parent_sid, captured_send_text) where the manager
    has a fake parent SessionInfo in IDLE state with a stub client and a
    stub _send_query that records the text it received."""
    SessionInfo = sm_module.SessionInfo
    SessionState = sm_module.SessionState

    manager = sm_module.SessionManager()
    parent_sid = "parent-int-001"

    info = SessionInfo(
        session_id=parent_sid,
        state=SessionState.IDLE,
        name="Parent",
        cwd="",
    )
    info.client = object()
    info.task = None
    manager._sessions[parent_sid] = info

    # Provide a real event loop on a background thread so the dispatch
    # path can submit and we can observe the text that lands at
    # self._sdk.send_query.
    captured = {"text": None}

    loop = asyncio.new_event_loop()

    def _runner():
        asyncio.set_event_loop(loop)
        loop.run_forever()

    t = threading.Thread(target=_runner, daemon=True)
    t.start()
    manager._loop = loop

    # Stub the entire _send_query coroutine to just capture text.
    async def _stub_send_query(session_id, text):
        captured["text"] = text

    manager._send_query = _stub_send_query

    # Tracked-coro wrapper — emulate the production path by setting
    # info.task and awaiting the coroutine.
    async def _tracked(info_arg, coro):
        info_arg.task = asyncio.current_task()
        try:
            await coro
        finally:
            info_arg.task = None
            info_arg.state = SessionState.IDLE

    manager._tracked_coro = _tracked

    manager._emit_state = lambda info_arg: None

    yield manager, parent_sid, captured

    loop.call_soon_threadsafe(loop.stop)
    t.join(timeout=2)
    loop.close()


# ---------------------------------------------------------------------------
# Test
# ---------------------------------------------------------------------------

class TestSubsessionsIntegration:
    def test_parent_send_message_prepends_subsession_report(
        self, sm_module, parent_with_inbox
    ):
        """Phase 3 (inbox write) + Phase 4 (drain in send_message)
        compose: a child reports → parent's next send_message text
        carries the report block.
        """
        from daemon import subsession_inbox as ibx
        import time as _time

        manager, parent_sid, captured = parent_with_inbox

        # 1. Simulate the child reporting (what /api/report-to-parent does).
        ibx.append_report(
            parent_sid=parent_sid,
            child_sid="child-int-A",
            child_name="Investigate flake",
            summary="Fix is a one-liner at line 882",
        )
        manager._sessions[parent_sid].inbox_dirty = True

        # 2. Send a follow-up user message.
        result = manager.send_message(parent_sid, "What did you find?")
        assert result["ok"] is True

        # 3. Wait for the dispatched coroutine to land at the stub.
        for _ in range(40):
            if captured["text"] is not None:
                break
            _time.sleep(0.05)
        assert captured["text"] is not None, "send_query stub never fired"

        text = captured["text"]
        # The drain block prefix is present.
        assert text.startswith(
            "[Subsession reports — surfaced before your next message]"
        )
        assert "Investigate flake" in text
        assert "Fix is a one-liner at line 882" in text
        # The original user text is present, after the separator.
        assert "[Your message]" in text
        assert "What did you find?" in text
        # Block comes before the user's text.
        assert text.index("[Subsession reports") < text.index(
            "[Your message]"
        )

        # 4. On disk, the entries are now marked delivered.
        on_disk = ibx.load_inbox(parent_sid)
        assert all(r["delivered"] for r in on_disk["pending_reports"])

        # 5. inbox_dirty is cleared.
        assert manager._sessions[parent_sid].inbox_dirty is False

    def test_pull_updates_empty_text_sends_block_only(
        self, sm_module, parent_with_inbox
    ):
        """Spec §4.3.5: when text == "" and inbox_dirty == True, the
        SDK sees ONLY the block (no "[Your message]" suffix)."""
        from daemon import subsession_inbox as ibx
        import time as _time

        manager, parent_sid, captured = parent_with_inbox

        ibx.append_report(
            parent_sid=parent_sid,
            child_sid="child-pull-1",
            child_name="Background scan",
            summary="No critical issues found",
        )
        manager._sessions[parent_sid].inbox_dirty = True

        result = manager.send_message(parent_sid, "")
        assert result["ok"] is True

        for _ in range(40):
            if captured["text"] is not None:
                break
            _time.sleep(0.05)
        assert captured["text"] is not None

        text = captured["text"]
        assert text.startswith(
            "[Subsession reports — surfaced before your next message]"
        )
        # No "[Your message]" separator in the Pull-updates path.
        assert "[Your message]" not in text
        assert "Background scan" in text

    def test_fast_path_when_inbox_dirty_is_false(
        self, sm_module, parent_with_inbox
    ):
        """Hot-path: inbox_dirty=False bypasses any disk I/O and the
        text is sent unchanged."""
        import time as _time

        manager, parent_sid, captured = parent_with_inbox
        assert manager._sessions[parent_sid].inbox_dirty is False

        manager.send_message(parent_sid, "plain message")

        for _ in range(40):
            if captured["text"] is not None:
                break
            _time.sleep(0.05)
        assert captured["text"] == "plain message"
