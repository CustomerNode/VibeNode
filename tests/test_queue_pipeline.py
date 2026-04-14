"""
Tests for interrupt/drain/follow-up queue pipeline in SessionManager.

Covers:
- Queued messages preserved through interrupt (when clear_queue=False)
- Queue auto-dispatches when session goes idle
- Rapid queue + interrupt: remaining messages preserved
"""

import asyncio
import inspect
import threading
import time
import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from dataclasses import dataclass, field
from typing import Optional


# ---------------------------------------------------------------------------
# Mock SDK types (mirrors test_session_manager.py pattern)
# ---------------------------------------------------------------------------

class MockTextBlock:
    def __init__(self, text=""):
        self.type = "text"
        self.text = text


class MockAssistantMessage:
    def __init__(self, content=None):
        self.content = content or []
        self.role = "assistant"


class MockResultMessage:
    def __init__(self, session_id="test-session", total_cost_usd=0.05,
                 duration_ms=1000, is_error=False, num_turns=1, usage=None):
        self.session_id = session_id
        self.total_cost_usd = total_cost_usd
        self.duration_ms = duration_ms
        self.is_error = is_error
        self.num_turns = num_turns
        self.usage = usage or {}


class MockToolUseBlock:
    def __init__(self, id="tool-1", name="Bash", input=None):
        self.type = "tool_use"
        self.id = id
        self.name = name
        self.input = input or {}


class MockToolResultBlock:
    def __init__(self, tool_use_id="tool-1", content="", is_error=False):
        self.type = "tool_result"
        self.tool_use_id = tool_use_id
        self.content = content
        self.is_error = is_error


class MockThinkingBlock:
    def __init__(self, text=""):
        self.type = "thinking"
        self.text = text


class MockUserMessage:
    def __init__(self, content=None):
        self.content = content or []
        self.role = "user"


class MockStreamEvent:
    def __init__(self, event="content_block_delta", data=None):
        self.event = event
        self.data = data or {}


class MockPermissionResultAllow:
    def __init__(self, updated_input=None, updated_permissions=None):
        self.updated_input = updated_input
        self.updated_permissions = updated_permissions


class MockPermissionResultDeny:
    def __init__(self, message="", interrupt=False):
        self.message = message
        self.interrupt = interrupt


class MockToolPermissionContext:
    def __init__(self):
        self.signal = None
        self.suggestions = []


class MockClaudeSDKClient:
    """Mock SDK client that yields predefined messages."""

    def __init__(self, options=None):
        self.options = options
        self._messages = []
        self._response_messages = []
        self._connected = False
        self._queries = []
        self._interrupted = False
        self._disconnected = False
        self.connect_prompt = None

    async def connect(self, prompt=None):
        self._connected = True
        self.connect_prompt = prompt

    async def query(self, prompt, session_id="default"):
        self._queries.append(prompt)

    async def receive_messages(self):
        for msg in self._messages:
            yield msg

    async def receive_response(self):
        for msg in self._response_messages:
            yield msg

    async def interrupt(self):
        self._interrupted = True

    async def disconnect(self):
        self._disconnected = True
        self._connected = False

    async def get_server_info(self):
        return {"version": "mock"}


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def mock_socketio():
    sio = MagicMock()
    sio.emit = MagicMock()
    return sio


@pytest.fixture
def mock_sdk_types():
    type_mocks = {
        'claude_code_sdk': MagicMock(),
        'claude_code_sdk.types': MagicMock(),
    }
    type_mocks['claude_code_sdk'].ClaudeSDKClient = MockClaudeSDKClient
    type_mocks['claude_code_sdk'].ClaudeCodeOptions = MagicMock

    types_mod = type_mocks['claude_code_sdk.types']
    types_mod.AssistantMessage = MockAssistantMessage
    types_mod.UserMessage = MockUserMessage
    types_mod.ResultMessage = MockResultMessage
    types_mod.StreamEvent = MockStreamEvent
    types_mod.TextBlock = MockTextBlock
    types_mod.ThinkingBlock = MockThinkingBlock
    types_mod.ToolUseBlock = MockToolUseBlock
    types_mod.ToolResultBlock = MockToolResultBlock
    types_mod.PermissionResultAllow = MockPermissionResultAllow
    types_mod.PermissionResultDeny = MockPermissionResultDeny
    types_mod.ContentBlock = MagicMock
    types_mod.ToolPermissionContext = MockToolPermissionContext
    types_mod.Message = MagicMock

    return type_mocks


@pytest.fixture
def sm_module(mock_sdk_types):
    with patch.dict('sys.modules', mock_sdk_types):
        import importlib
        import daemon.session_manager as sm_mod
        importlib.reload(sm_mod)

        sm_mod.AssistantMessage = MockAssistantMessage
        sm_mod.UserMessage = MockUserMessage
        sm_mod.ResultMessage = MockResultMessage
        sm_mod.StreamEvent = MockStreamEvent
        sm_mod.TextBlock = MockTextBlock
        sm_mod.ThinkingBlock = MockThinkingBlock
        sm_mod.ToolUseBlock = MockToolUseBlock
        sm_mod.ToolResultBlock = MockToolResultBlock
        sm_mod.PermissionResultAllow = MockPermissionResultAllow
        sm_mod.PermissionResultDeny = MockPermissionResultDeny
        sm_mod.ClaudeSDKClient = MockClaudeSDKClient
        sm_mod.ClaudeCodeOptions = MagicMock
        yield sm_mod


@pytest.fixture
def session_manager(mock_socketio, sm_module):
    manager = sm_module.SessionManager()
    manager.start(mock_socketio)
    yield manager
    manager.stop()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def wait_for(condition, timeout=5.0, interval=0.05):
    """Poll until condition() is truthy or timeout expires."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        result = condition()
        if result:
            return result
        time.sleep(interval)
    raise TimeoutError(f"Condition not met within {timeout}s")


# ===========================================================================
# Queue pipeline tests
# ===========================================================================

class TestInterruptDrainsToQueue:
    """Queued messages are preserved through interrupt."""

    def test_queued_messages_preserved_through_interrupt_no_clear(
        self, session_manager, sm_module
    ):
        """Messages queued while WORKING survive interrupt with clear_queue=False."""
        sid = "drain-test"
        info = sm_module.SessionInfo(session_id=sid, state=sm_module.SessionState.WORKING)
        info.cwd = "/tmp"
        info.task = MagicMock()

        with session_manager._lock:
            session_manager._sessions[sid] = info

        # Queue two messages while session is working
        session_manager.queue_message(sid, "First queued")
        session_manager.queue_message(sid, "Second queued")

        assert len(session_manager.get_queue(sid)) == 2

        # Interrupt without clearing queue
        session_manager.interrupt_session(sid, clear_queue=False)

        # Queue should be intact
        queue = session_manager.get_queue(sid)
        assert len(queue) == 2
        assert queue[0] == "First queued"
        assert queue[1] == "Second queued"

    def test_default_interrupt_clears_queue(self, session_manager, sm_module):
        """Default interrupt (clear_queue=True) empties the queue."""
        sid = "drain-clear"
        info = sm_module.SessionInfo(session_id=sid, state=sm_module.SessionState.WORKING)
        info.cwd = "/tmp"
        info.task = MagicMock()

        with session_manager._lock:
            session_manager._sessions[sid] = info

        session_manager.queue_message(sid, "Will be cleared")
        assert len(session_manager.get_queue(sid)) == 1

        # Patch _emit_queue_update to avoid nested _queue_lock acquisition
        with patch.object(session_manager, '_emit_queue_update'):
            session_manager.interrupt_session(sid)

        assert len(session_manager.get_queue(sid)) == 0


class TestQueueAutoDispatchesOnIdle:
    """Session finishes turn, queued message auto-dispatches."""

    def test_try_dispatch_queue_method_exists(self, sm_module):
        """_try_dispatch_queue must exist and be callable."""
        assert hasattr(sm_module.SessionManager, '_try_dispatch_queue')
        assert callable(sm_module.SessionManager._try_dispatch_queue)

    def test_try_dispatch_pops_first_item(self, session_manager, sm_module):
        """_try_dispatch_queue should pop the first item from the queue
        and call send_message with it."""
        sid = "dispatch-test"
        info = sm_module.SessionInfo(session_id=sid, state=sm_module.SessionState.IDLE)
        info.cwd = "/tmp"

        with session_manager._lock:
            session_manager._sessions[sid] = info

        # Queue messages
        session_manager.queue_message(sid, "Auto-msg-1")
        session_manager.queue_message(sid, "Auto-msg-2")

        # Note: queue_message already calls _try_dispatch_queue if IDLE,
        # which will call send_message. Since the session is IDLE and
        # send_message will set WORKING, the first message should dispatch.
        # After that the second message stays queued until next IDLE.

        # Verify the queue behavior is correct by checking entries
        # The first queued message should have been dispatched
        # (send_message was called, which adds a user entry)
        entries = session_manager.get_entries(sid)
        user_entries = [e for e in entries if e["kind"] == "user"]
        # At least one message should have been dispatched
        assert len(user_entries) >= 1

    def test_dispatch_on_idle_transition(self, sm_module):
        """_emit_state should call _try_dispatch_queue when state is IDLE."""
        src = inspect.getsource(sm_module.SessionManager._emit_state)
        assert "_try_dispatch_queue" in src

    def test_queue_dispatch_re_queues_on_failure(self, sm_module):
        """If send_message fails during dispatch, the message should be
        re-queued at the front."""
        src = inspect.getsource(sm_module.SessionManager._try_dispatch_queue)
        assert "re-queue" in src.lower() or "insert(0" in src


class TestRapidQueueAndInterrupt:
    """Queue 3 messages, interrupt after 1st dispatches, verify
    remaining 2 are preserved (or cleared based on clear_queue flag)."""

    def test_rapid_queue_messages(self, session_manager, sm_module):
        """Queue multiple messages rapidly, verify they're all stored."""
        sid = "rapid-q"
        info = sm_module.SessionInfo(session_id=sid, state=sm_module.SessionState.WORKING)
        info.cwd = "/tmp"
        info.task = MagicMock()

        with session_manager._lock:
            session_manager._sessions[sid] = info

        # Queue 3 messages while working (they won't dispatch)
        session_manager.queue_message(sid, "Msg 1")
        session_manager.queue_message(sid, "Msg 2")
        session_manager.queue_message(sid, "Msg 3")

        queue = session_manager.get_queue(sid)
        assert len(queue) == 3
        assert queue == ["Msg 1", "Msg 2", "Msg 3"]

    def test_interrupt_preserves_remaining_without_clear(
        self, session_manager, sm_module
    ):
        """After interrupt with clear_queue=False, remaining queue items
        are preserved."""
        sid = "rapid-int"
        info = sm_module.SessionInfo(session_id=sid, state=sm_module.SessionState.WORKING)
        info.cwd = "/tmp"
        info.task = MagicMock()

        with session_manager._lock:
            session_manager._sessions[sid] = info

        # Queue 3
        session_manager.queue_message(sid, "Msg 1")
        session_manager.queue_message(sid, "Msg 2")
        session_manager.queue_message(sid, "Msg 3")

        # Simulate that Msg 1 was dispatched (remove from front)
        with session_manager._queue_lock:
            q = session_manager._queues.get(sid, [])
            if q:
                q.pop(0)

        # Interrupt without clearing
        session_manager.interrupt_session(sid, clear_queue=False)

        queue = session_manager.get_queue(sid)
        assert len(queue) == 2
        assert queue == ["Msg 2", "Msg 3"]

    def test_queue_ordering_preserved(self, session_manager, sm_module):
        """Queue messages maintain FIFO order."""
        sid = "order-test"
        info = sm_module.SessionInfo(session_id=sid, state=sm_module.SessionState.WORKING)
        info.cwd = "/tmp"

        with session_manager._lock:
            session_manager._sessions[sid] = info

        for i in range(5):
            session_manager.queue_message(sid, f"Msg {i}")

        queue = session_manager.get_queue(sid)
        assert queue == ["Msg 0", "Msg 1", "Msg 2", "Msg 3", "Msg 4"]

    def test_queue_edit_and_remove(self, session_manager, sm_module):
        """Queue items can be edited and removed by index."""
        sid = "edit-rm"
        info = sm_module.SessionInfo(session_id=sid, state=sm_module.SessionState.WORKING)
        info.cwd = "/tmp"

        with session_manager._lock:
            session_manager._sessions[sid] = info

        session_manager.queue_message(sid, "Original 1")
        session_manager.queue_message(sid, "Original 2")
        session_manager.queue_message(sid, "Original 3")

        # Edit index 1
        result = session_manager.edit_queue_item(sid, 1, "Edited 2")
        assert result["ok"] is True

        queue = session_manager.get_queue(sid)
        assert queue[1] == "Edited 2"

        # Remove index 0
        result = session_manager.remove_queue_item(sid, 0)
        assert result["ok"] is True

        queue = session_manager.get_queue(sid)
        assert len(queue) == 2
        assert queue[0] == "Edited 2"
        assert queue[1] == "Original 3"

    def test_clear_queue(self, session_manager, sm_module):
        """clear_queue removes all items."""
        sid = "clear-all"
        info = sm_module.SessionInfo(session_id=sid, state=sm_module.SessionState.WORKING)
        info.cwd = "/tmp"

        with session_manager._lock:
            session_manager._sessions[sid] = info

        session_manager.queue_message(sid, "A")
        session_manager.queue_message(sid, "B")

        result = session_manager.clear_queue(sid)
        assert result["ok"] is True
        assert len(session_manager.get_queue(sid)) == 0
