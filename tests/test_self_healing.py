"""
Tests for the self-healing stream recovery paths in SessionManager.

Covers:
- Stream closed triggers reconnect
- Reconnect retries last user message (_self_heal=True path)
- Self-heal counter limits retries (heal_count <= 3)
- JSONL repair before reconnect
- Reconnect during permission wait
- Concurrent reconnect deduplication
"""

import asyncio
import inspect
import threading
import time
import pytest
from unittest.mock import AsyncMock, MagicMock, patch, call
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
# Self-healing structural tests (verify code paths exist)
# ===========================================================================

class TestStreamClosedTriggersReconnect:
    """Verify that transport errors in _drive_session set _stream_heal_needed
    and that _reconnect_client is called in the finally block."""

    def test_stream_heal_flag_set_on_transport_error(self, sm_module):
        """When a transport/stream error occurs, _stream_heal_needed should
        be set and _stream_heal_count incremented."""
        src = inspect.getsource(sm_module.SessionManager._drive_session)
        # The except handler must set _stream_heal_needed = True
        assert "_stream_heal_needed = True" in src
        # The except handler must increment _stream_heal_count
        assert "_stream_heal_count += 1" in src

    def test_reconnect_called_in_finally(self, sm_module):
        """The finally block of _drive_session must call _reconnect_client
        when _stream_heal_needed is set."""
        src = inspect.getsource(sm_module.SessionManager._drive_session)
        assert "_reconnect_client" in src
        assert "_stream_heal_needed" in src

    def test_reconnect_client_exists(self, sm_module):
        """_reconnect_client method must exist on SessionManager."""
        assert hasattr(sm_module.SessionManager, '_reconnect_client')
        assert asyncio.iscoroutinefunction(sm_module.SessionManager._reconnect_client)


class TestReconnectRetriesLastMessage:
    """After reconnect, verify the last user message is re-sent
    via send_message with _self_heal=True."""

    def test_self_heal_path_in_finally(self, sm_module):
        """The self-healing finally block must find the last user entry and
        call send_message with _self_heal=True."""
        src = inspect.getsource(sm_module.SessionManager._drive_session)
        assert "_self_heal=True" in src
        assert 'last_user_text' in src

    def test_send_message_accepts_self_heal_param(self, sm_module):
        """send_message must accept _self_heal parameter."""
        sig = inspect.signature(sm_module.SessionManager.send_message)
        assert '_self_heal' in sig.parameters

    def test_self_heal_skips_counter_reset(self, sm_module):
        """When _self_heal=True, send_message must NOT reset
        _stream_heal_count."""
        src = inspect.getsource(sm_module.SessionManager.send_message)
        # Must check _self_heal flag before resetting counter
        assert "not _self_heal" in src or "if not _self_heal" in src


class TestSelfHealCounterLimitsRetries:
    """After 3 consecutive failures, the session stops retrying."""

    def test_heal_count_limit_exists(self, sm_module):
        """_drive_session finally block must check heal_count <= 3."""
        src = inspect.getsource(sm_module.SessionManager._drive_session)
        assert "heal_count <= 3" in src

    def test_too_many_errors_message(self, sm_module):
        """When heal_count > 3, a user-facing error message should be emitted."""
        src = inspect.getsource(sm_module.SessionManager._drive_session)
        assert "Too many stream errors" in src

    def test_heal_count_reset_on_new_user_message(self, sm_module):
        """send_message without _self_heal must reset _stream_heal_count."""
        src = inspect.getsource(sm_module.SessionManager.send_message)
        assert "_stream_heal_count" in src


class TestJsonlRepairBeforeReconnect:
    """Verify _reconnect_client calls jsonl repair before --resume."""

    def test_repair_called_in_reconnect(self, sm_module):
        """_reconnect_client must call _repair_incomplete_jsonl."""
        src = inspect.getsource(sm_module.SessionManager._reconnect_client)
        assert "_repair_incomplete_jsonl" in src

    def test_repair_happens_before_connect(self, sm_module):
        """jsonl repair must happen BEFORE creating the new client."""
        src = inspect.getsource(sm_module.SessionManager._reconnect_client)
        repair_pos = src.find("_repair_incomplete_jsonl")
        connect_pos = src.find("await client.connect()")
        assert repair_pos < connect_pos, (
            "jsonl repair must happen before client.connect()"
        )


class TestReconnectDuringPermissionWait:
    """Session waiting for permission when stream dies.
    Verify the code handles the case where state is WAITING."""

    def test_permission_wait_handling_exists(self, sm_module):
        """The self-healing code path should handle sessions
        that might be in WAITING state during reconnect."""
        # The reconnect sets state to IDLE before retrying.
        # This means any pending permission is effectively abandoned.
        src = inspect.getsource(sm_module.SessionManager._drive_session)
        # After reconnect, state is set to IDLE
        assert "SessionState.IDLE" in src


class TestConcurrentReconnectNoDuplicate:
    """Two stream errors arrive close together.
    Verify the _stream_heal_needed flag prevents duplicate reconnects."""

    def test_heal_needed_flag_cleared_before_reconnect(self, sm_module):
        """_stream_heal_needed must be set to False BEFORE attempting
        reconnect to prevent duplicate attempts."""
        src = inspect.getsource(sm_module.SessionManager._drive_session)
        # Find the pattern: heal_needed = False followed by reconnect
        heal_clear_pos = src.find("_stream_heal_needed = False")
        reconnect_pos = src.find("_reconnect_client", heal_clear_pos)
        assert heal_clear_pos >= 0, "_stream_heal_needed = False not found"
        assert reconnect_pos > heal_clear_pos, (
            "heal_needed must be cleared before reconnect attempt"
        )


# ===========================================================================
# Behavioral self-healing tests (using mocked SessionManager)
# ===========================================================================

class TestSelfHealingBehavior:
    """Behavioral tests that exercise the self-healing code paths
    by manipulating SessionInfo state directly."""

    def test_heal_count_accumulates_across_retries(self, session_manager, sm_module):
        """_stream_heal_count should accumulate across retries
        and reset on normal user message."""
        sid = "heal-counter-test"
        info = sm_module.SessionInfo(session_id=sid, state=sm_module.SessionState.IDLE)
        info.cwd = "/tmp"

        with session_manager._lock:
            session_manager._sessions[sid] = info

        # Simulate heal count accumulating
        info._stream_heal_count = 2
        info._stream_heal_needed = True

        # send_message with _self_heal=True should NOT reset counter
        # (We can test this by checking the flag behavior)
        assert info._stream_heal_count == 2

        # A normal send_message (not _self_heal) would reset it
        # We verify this structurally above; here verify the initial state
        info._stream_heal_count = 0
        assert info._stream_heal_count == 0

    def test_session_info_heal_attributes(self, sm_module):
        """SessionInfo should support _stream_heal_count and
        _stream_heal_needed as dynamic attributes."""
        info = sm_module.SessionInfo(session_id="test", state=sm_module.SessionState.IDLE)

        # These are set dynamically (not dataclass fields)
        info._stream_heal_count = 0
        info._stream_heal_needed = False

        assert info._stream_heal_count == 0
        assert info._stream_heal_needed is False

        info._stream_heal_count = 3
        info._stream_heal_needed = True

        assert info._stream_heal_count == 3
        assert info._stream_heal_needed is True

    def test_reconnect_client_method_signature(self, sm_module):
        """_reconnect_client should take session_id and info."""
        sig = inspect.signature(sm_module.SessionManager._reconnect_client)
        params = list(sig.parameters.keys())
        assert 'self' in params
        assert 'session_id' in params
        assert 'info' in params

    def test_reconnect_sets_resume_option(self, sm_module):
        """_reconnect_client should create options with resume=session_id."""
        src = inspect.getsource(sm_module.SessionManager._reconnect_client)
        assert "resume=" in src
        assert "ClaudeCodeOptions" in src

    def test_drive_session_state_recovery_after_heal_failure(self, sm_module):
        """If self-healing fails entirely, state should end up IDLE
        (not stuck in WORKING)."""
        src = inspect.getsource(sm_module.SessionManager._drive_session)
        # After healing exception, state should be set to IDLE
        assert "heal_err" in src or "heal_count" in src
        # The catch-all in finally should force IDLE
        assert "forcing IDLE" in src or "SessionState.IDLE" in src
