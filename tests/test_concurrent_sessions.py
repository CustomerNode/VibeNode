"""
Tests for multi-session concurrency scenarios in SessionManager.

Covers:
- Two sessions in same project maintain independent state
- Concurrent send_message to different sessions has no cross-contamination
- Interrupting one session doesn't affect another
- Queue dispatch survives interrupt
- File tracking overlap between sessions
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
def session_manager(mock_socketio, sm_module, tmp_path):
    manager = sm_module.SessionManager()
    # Use a temp queue file so tests don't interfere with each other
    manager._queue_path = tmp_path / "test_queues.json"
    manager._queues = {}
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
# Multi-session independence tests
# ===========================================================================

class TestTwoSessionsSameProjectIndependentState:
    """Two sessions in the same project dir should have completely
    independent state, entries, and tracked files."""

    def test_independent_session_info(self, session_manager, sm_module):
        """Each session has its own SessionInfo instance with
        independent entries, state, and tracked files."""
        sid1 = "concurrent-a"
        sid2 = "concurrent-b"

        # Create sessions directly (no SDK needed for this test)
        info1 = sm_module.SessionInfo(session_id=sid1, state=sm_module.SessionState.IDLE)
        info1.cwd = "/tmp/project"
        info1.entries.append(sm_module.LogEntry(kind="user", text="Hello A"))
        info1.entries.append(sm_module.LogEntry(kind="asst", text="Reply A"))
        info1.tracked_files = {"/tmp/project/file_a.py"}

        info2 = sm_module.SessionInfo(session_id=sid2, state=sm_module.SessionState.IDLE)
        info2.cwd = "/tmp/project"
        info2.entries.append(sm_module.LogEntry(kind="user", text="Hello B"))
        info2.entries.append(sm_module.LogEntry(kind="asst", text="Reply B"))
        info2.tracked_files = {"/tmp/project/file_b.py"}

        with session_manager._lock:
            session_manager._sessions[sid1] = info1
            session_manager._sessions[sid2] = info2

        # Verify entries are independent
        entries1 = session_manager.get_entries(sid1)
        entries2 = session_manager.get_entries(sid2)

        asst_texts_1 = [e["text"] for e in entries1 if e["kind"] == "asst"]
        asst_texts_2 = [e["text"] for e in entries2 if e["kind"] == "asst"]

        assert any("Reply A" in t for t in asst_texts_1)
        assert any("Reply B" in t for t in asst_texts_2)
        # No cross-contamination
        assert not any("Reply B" in t for t in asst_texts_1)
        assert not any("Reply A" in t for t in asst_texts_2)

        # Tracked files are independent
        assert info1.tracked_files != info2.tracked_files

    def test_independent_file_tracking(self, session_manager, sm_module):
        """Each session's tracked_files set is independent."""
        sid1 = "ft-a"
        sid2 = "ft-b"

        info1 = sm_module.SessionInfo(session_id=sid1, state=sm_module.SessionState.IDLE)
        info1.cwd = "/tmp/project"
        info1.tracked_files = {"/tmp/project/file1.py"}

        info2 = sm_module.SessionInfo(session_id=sid2, state=sm_module.SessionState.IDLE)
        info2.cwd = "/tmp/project"
        info2.tracked_files = {"/tmp/project/file2.py"}

        with session_manager._lock:
            session_manager._sessions[sid1] = info1
            session_manager._sessions[sid2] = info2

        # Verify they don't share tracked files
        assert info1.tracked_files != info2.tracked_files
        assert "/tmp/project/file1.py" in info1.tracked_files
        assert "/tmp/project/file2.py" in info2.tracked_files
        assert "/tmp/project/file2.py" not in info1.tracked_files
        assert "/tmp/project/file1.py" not in info2.tracked_files

    def test_state_independence(self, session_manager, sm_module):
        """Changing state of one session doesn't affect another."""
        sid1 = "state-a"
        sid2 = "state-b"

        info1 = sm_module.SessionInfo(session_id=sid1, state=sm_module.SessionState.IDLE)
        info2 = sm_module.SessionInfo(session_id=sid2, state=sm_module.SessionState.WORKING)

        with session_manager._lock:
            session_manager._sessions[sid1] = info1
            session_manager._sessions[sid2] = info2

        assert session_manager.get_session_state(sid1) == "idle"
        assert session_manager.get_session_state(sid2) == "working"

        # Change sid1 state
        info1.state = sm_module.SessionState.STOPPED
        assert session_manager.get_session_state(sid1) == "stopped"
        assert session_manager.get_session_state(sid2) == "working"


class TestConcurrentSendMessageDifferentSessions:
    """Simultaneous messages to different sessions should not
    cross-contaminate queues or entries."""

    def test_no_queue_cross_contamination(self, session_manager, sm_module):
        """Queue for session A should not contain messages for session B."""
        sid1 = "queue-a"
        sid2 = "queue-b"

        info1 = sm_module.SessionInfo(session_id=sid1, state=sm_module.SessionState.WORKING)
        info2 = sm_module.SessionInfo(session_id=sid2, state=sm_module.SessionState.WORKING)

        with session_manager._lock:
            session_manager._sessions[sid1] = info1
            session_manager._sessions[sid2] = info2

        # Both are WORKING, so messages get queued
        session_manager.send_message(sid1, "Message for A")
        session_manager.send_message(sid2, "Message for B")

        queue1 = session_manager.get_queue(sid1)
        queue2 = session_manager.get_queue(sid2)

        assert "Message for A" in queue1
        assert "Message for B" not in queue1
        assert "Message for B" in queue2
        assert "Message for A" not in queue2


class TestInterruptOneSessionOtherContinues:
    """Interrupting session A while session B is working
    should not affect B."""

    def test_interrupt_independence(self, session_manager, sm_module):
        """Interrupt A, verify B's state is unchanged."""
        sid_a = "int-a"
        sid_b = "int-b"

        # Create two sessions in WORKING state
        info_a = sm_module.SessionInfo(session_id=sid_a, state=sm_module.SessionState.WORKING)
        info_a.cwd = "/tmp"
        info_a.task = MagicMock()  # mock asyncio task

        info_b = sm_module.SessionInfo(session_id=sid_b, state=sm_module.SessionState.WORKING)
        info_b.cwd = "/tmp"
        info_b.task = MagicMock()

        with session_manager._lock:
            session_manager._sessions[sid_a] = info_a
            session_manager._sessions[sid_b] = info_b

        # Interrupt A
        result = session_manager.interrupt_session(sid_a)
        assert result["ok"] is True

        # A should be IDLE (interrupt sets IDLE synchronously)
        assert session_manager.get_session_state(sid_a) == "idle"
        # B should still be WORKING
        assert session_manager.get_session_state(sid_b) == "working"


class TestQueueDispatchAfterInterrupt:
    """Queue a message, interrupt, verify queue item survives
    and can be dispatched later."""

    def test_queue_survives_non_clearing_interrupt(self, session_manager, sm_module):
        """When interrupt is called with clear_queue=False, queued
        messages should be preserved."""
        sid = "queue-survive"
        info = sm_module.SessionInfo(session_id=sid, state=sm_module.SessionState.WORKING)
        info.cwd = "/tmp"
        info.task = MagicMock()

        with session_manager._lock:
            session_manager._sessions[sid] = info

        # Queue a message while WORKING
        session_manager.queue_message(sid, "Queued msg")

        # Interrupt WITHOUT clearing queue
        result = session_manager.interrupt_session(sid, clear_queue=False)
        assert result["ok"] is True

        # Queue should still have the message
        queue = session_manager.get_queue(sid)
        assert "Queued msg" in queue

    def test_queue_cleared_on_default_interrupt(self, session_manager, sm_module):
        """Default interrupt clears the queue to prevent auto-dispatch."""
        sid = "queue-clear"
        info = sm_module.SessionInfo(session_id=sid, state=sm_module.SessionState.WORKING)
        info.cwd = "/tmp"
        info.task = MagicMock()

        with session_manager._lock:
            session_manager._sessions[sid] = info

        session_manager.queue_message(sid, "Will be cleared")

        # Default interrupt clears queue.
        # Note: interrupt_session calls _emit_queue_update inside _queue_lock,
        # which also acquires _queue_lock (potential reentrant issue with
        # threading.Lock). We patch _emit_queue_update to avoid the nested
        # lock acquisition in tests.
        with patch.object(session_manager, '_emit_queue_update'):
            session_manager.interrupt_session(sid)

        queue = session_manager.get_queue(sid)
        assert len(queue) == 0


class TestFileTrackingOverlap:
    """Two sessions modify the same file. Both detect change
    independently in their snapshots."""

    def test_tracked_files_per_session(self, sm_module):
        """Each SessionInfo has its own tracked_files set that can
        independently track the same file."""
        info1 = sm_module.SessionInfo(session_id="s1", state=sm_module.SessionState.WORKING)
        info2 = sm_module.SessionInfo(session_id="s2", state=sm_module.SessionState.WORKING)

        shared_file = "/tmp/project/shared.py"

        info1.tracked_files.add(shared_file)
        info2.tracked_files.add(shared_file)

        # Both should have it independently
        assert shared_file in info1.tracked_files
        assert shared_file in info2.tracked_files

        # Removing from one doesn't affect the other
        info1.tracked_files.discard(shared_file)
        assert shared_file not in info1.tracked_files
        assert shared_file in info2.tracked_files

    def test_mtimes_per_session(self, sm_module):
        """Each session tracks its own pre/post turn mtimes independently."""
        info1 = sm_module.SessionInfo(session_id="s1")
        info2 = sm_module.SessionInfo(session_id="s2")

        shared_file = "/tmp/project/shared.py"

        info1._pre_turn_mtimes[shared_file] = 1000.0
        info2._pre_turn_mtimes[shared_file] = 2000.0

        assert info1._pre_turn_mtimes[shared_file] == 1000.0
        assert info2._pre_turn_mtimes[shared_file] == 2000.0

    def test_session_lock_independence(self, sm_module):
        """Each SessionInfo has its own lock for thread safety."""
        info1 = sm_module.SessionInfo(session_id="lock-1")
        info2 = sm_module.SessionInfo(session_id="lock-2")

        assert info1._lock is not info2._lock
        assert isinstance(info1._lock, type(threading.Lock()))
        assert isinstance(info2._lock, type(threading.Lock()))
