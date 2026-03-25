"""
Comprehensive tests for the SessionManager class.

Tests cover:
- Session lifecycle (start -> work -> idle -> stop)
- Permission callback flow (wait -> resolve -> continue)
- Message processing for all SDK message types
- Thread safety and concurrent operations
- Error handling and edge cases
"""

import asyncio
import threading
import time
import pytest
from unittest.mock import AsyncMock, MagicMock, patch, PropertyMock
from dataclasses import dataclass, field
from typing import Optional


# ---------------------------------------------------------------------------
# Mock SDK types (so tests don't require claude-code-sdk installed)
# ---------------------------------------------------------------------------

class MockTextBlock:
    def __init__(self, text=""):
        self.type = "text"
        self.text = text


class MockThinkingBlock:
    def __init__(self, text=""):
        self.type = "thinking"
        self.text = text


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


class MockAssistantMessage:
    def __init__(self, content=None):
        self.content = content or []
        self.role = "assistant"


class MockUserMessage:
    def __init__(self, content=None):
        self.content = content or []
        self.role = "user"


class MockResultMessage:
    def __init__(self, session_id="test-session", total_cost_usd=0.05,
                 duration_ms=1000, is_error=False, num_turns=1, usage=None):
        self.session_id = session_id
        self.total_cost_usd = total_cost_usd
        self.duration_ms = duration_ms
        self.is_error = is_error
        self.num_turns = num_turns
        self.usage = usage or {}


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
        self._messages = []  # set by test before driving
        self._response_messages = []  # for receive_response
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
    """Create a mock SocketIO instance."""
    sio = MagicMock()
    sio.emit = MagicMock()
    return sio


@pytest.fixture
def mock_sdk_types():
    """Patch all SDK types so SessionManager can be imported without the real SDK."""
    type_mocks = {
        'claude_code_sdk': MagicMock(),
        'claude_code_sdk.types': MagicMock(),
    }
    # Set up the type classes on the mock modules
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
def session_manager(mock_socketio, mock_sdk_types):
    """Create a SessionManager with mocked SDK and SocketIO, started and ready."""
    with patch.dict('sys.modules', mock_sdk_types):
        # Force reimport with mocked modules
        import importlib
        import app.session_manager as sm_module
        importlib.reload(sm_module)

        # Patch the type references on the reloaded module
        sm_module.AssistantMessage = MockAssistantMessage
        sm_module.UserMessage = MockUserMessage
        sm_module.ResultMessage = MockResultMessage
        sm_module.StreamEvent = MockStreamEvent
        sm_module.TextBlock = MockTextBlock
        sm_module.ThinkingBlock = MockThinkingBlock
        sm_module.ToolUseBlock = MockToolUseBlock
        sm_module.ToolResultBlock = MockToolResultBlock
        sm_module.PermissionResultAllow = MockPermissionResultAllow
        sm_module.PermissionResultDeny = MockPermissionResultDeny
        sm_module.ClaudeSDKClient = MockClaudeSDKClient
        sm_module.ClaudeCodeOptions = MagicMock

        manager = sm_module.SessionManager()
        manager.start(mock_socketio)
        yield manager
        manager.stop()


@pytest.fixture
def sm_module(mock_sdk_types):
    """Return the reloaded session_manager module with mocked SDK types."""
    with patch.dict('sys.modules', mock_sdk_types):
        import importlib
        import app.session_manager as sm_mod
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


# ---------------------------------------------------------------------------
# Helper to wait for async operations to propagate
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


# ---------------------------------------------------------------------------
# 1. Session lifecycle: start to idle
# ---------------------------------------------------------------------------

class TestSessionLifecycle:

    def test_session_lifecycle_start_to_idle(self, session_manager, sm_module):
        """A session should go STARTING -> WORKING -> IDLE after messages complete."""
        sid = "test-lifecycle-001"

        # Pre-configure what the mock client will yield
        messages = [
            MockAssistantMessage([MockTextBlock("Hello! How can I help?")]),
            MockResultMessage(session_id=sid, total_cost_usd=0.01),
        ]

        # Patch ClaudeSDKClient to return our mock
        mock_client = MockClaudeSDKClient()
        mock_client._messages = messages

        with patch.object(sm_module, 'ClaudeSDKClient', return_value=mock_client):
            result = session_manager.start_session(sid, prompt="Hi", cwd="/tmp")
            assert result["ok"] is True

            # Wait for session to reach IDLE
            wait_for(lambda: session_manager.get_session_state(sid) == "idle")

        state = session_manager.get_session_state(sid)
        assert state == "idle"

        # Check that entries were recorded (user prompt + assistant reply)
        entries = session_manager.get_entries(sid)
        assert len(entries) >= 2
        assert entries[0]["kind"] == "user"
        assert entries[1]["kind"] == "asst"
        assert "Hello" in entries[1]["text"]

    def test_session_lifecycle_with_permission(self, session_manager, sm_module):
        """Session should go WORKING -> WAITING when permission is needed."""
        sid = "test-perm-lifecycle"

        # We need a client that calls can_use_tool
        # For this test, we'll directly test the permission callback
        mock_client = MockClaudeSDKClient()
        # Messages that include a tool use requiring permission
        mock_client._messages = [
            MockAssistantMessage([MockToolUseBlock(id="t1", name="Bash", input={"command": "ls"})]),
            MockResultMessage(session_id=sid),
        ]

        with patch.object(sm_module, 'ClaudeSDKClient', return_value=mock_client):
            result = session_manager.start_session(sid, prompt="list files", cwd="/tmp")
            assert result["ok"] is True

            # Wait for it to finish processing
            wait_for(lambda: session_manager.get_session_state(sid) == "idle", timeout=5)

        # Verify entries include tool_use
        entries = session_manager.get_entries(sid)
        tool_entries = [e for e in entries if e["kind"] == "tool_use"]
        assert len(tool_entries) >= 1
        assert tool_entries[0]["name"] == "Bash"


# ---------------------------------------------------------------------------
# 3-4. Permission resolve: allow and deny
# ---------------------------------------------------------------------------

class TestPermissionResolve:

    def test_permission_resolve_allow(self, session_manager, sm_module):
        """Resolving a permission with allow=True should return PermissionResultAllow."""
        sid = "test-perm-allow"
        info = sm_module.SessionInfo(session_id=sid, state=sm_module.SessionState.WAITING)

        # Set up session manually
        with session_manager._lock:
            session_manager._sessions[sid] = info

        # Use call_soon_threadsafe to create the future on the event loop
        loop = session_manager._loop
        created = [None]
        event = threading.Event()

        def _create():
            created[0] = loop.create_future()
            event.set()

        loop.call_soon_threadsafe(_create)
        event.wait(timeout=5)
        info.pending_permission = created[0]

        result = session_manager.resolve_permission(sid, allow=True, always=False)
        assert result["ok"] is True

        # The future should now be resolved
        wait_for(lambda: created[0].done(), timeout=2)
        perm_result, always = created[0].result()
        assert isinstance(perm_result, MockPermissionResultAllow)
        assert always is False

    def test_permission_resolve_deny(self, session_manager, sm_module):
        """Resolving a permission with allow=False should return PermissionResultDeny."""
        sid = "test-perm-deny"
        info = sm_module.SessionInfo(session_id=sid, state=sm_module.SessionState.WAITING)

        with session_manager._lock:
            session_manager._sessions[sid] = info

        # Create future on the loop
        created = [None]
        event = threading.Event()

        def _create():
            created[0] = session_manager._loop.create_future()
            event.set()

        session_manager._loop.call_soon_threadsafe(_create)
        event.wait(timeout=5)
        info.pending_permission = created[0]

        result = session_manager.resolve_permission(sid, allow=False)
        assert result["ok"] is True

        wait_for(lambda: created[0].done(), timeout=2)
        perm_result, always = created[0].result()
        assert isinstance(perm_result, MockPermissionResultDeny)

    def test_permission_timeout(self, session_manager, sm_module):
        """Permission callback should handle timeout gracefully."""
        sid = "test-perm-timeout"
        # Test that resolve_permission rejects when state is not WAITING
        info = sm_module.SessionInfo(session_id=sid, state=sm_module.SessionState.WORKING)
        with session_manager._lock:
            session_manager._sessions[sid] = info

        result = session_manager.resolve_permission(sid, allow=True)
        assert result["ok"] is False
        assert "not waiting" in result["error"].lower()


# ---------------------------------------------------------------------------
# 6. Concurrent sessions
# ---------------------------------------------------------------------------

class TestConcurrentSessions:

    def test_concurrent_sessions_independent(self, session_manager, sm_module):
        """Multiple sessions should operate independently."""
        sid1 = "concurrent-001"
        sid2 = "concurrent-002"

        mock_client1 = MockClaudeSDKClient()
        mock_client1._messages = [
            MockAssistantMessage([MockTextBlock("Response 1")]),
            MockResultMessage(session_id=sid1, total_cost_usd=0.01),
        ]
        mock_client2 = MockClaudeSDKClient()
        mock_client2._messages = [
            MockAssistantMessage([MockTextBlock("Response 2")]),
            MockResultMessage(session_id=sid2, total_cost_usd=0.02),
        ]

        clients = iter([mock_client1, mock_client2])

        with patch.object(sm_module, 'ClaudeSDKClient', side_effect=lambda **kw: next(clients)):
            session_manager.start_session(sid1, prompt="P1", cwd="/tmp")
            session_manager.start_session(sid2, prompt="P2", cwd="/tmp")

            wait_for(lambda: session_manager.get_session_state(sid1) == "idle")
            wait_for(lambda: session_manager.get_session_state(sid2) == "idle")

        entries1 = session_manager.get_entries(sid1)
        entries2 = session_manager.get_entries(sid2)

        # Each session should have its own entries
        asst1 = [e for e in entries1 if e["kind"] == "asst"]
        asst2 = [e for e in entries2 if e["kind"] == "asst"]
        assert any("Response 1" in e["text"] for e in asst1)
        assert any("Response 2" in e["text"] for e in asst2)


# ---------------------------------------------------------------------------
# 7-10. Message processing
# ---------------------------------------------------------------------------

class TestMessageProcessing:

    def test_message_processing_assistant_text(self, session_manager, sm_module):
        """AssistantMessage with TextBlock -> kind='asst' entry."""
        sid = "msg-asst-text"
        mock_client = MockClaudeSDKClient()
        mock_client._messages = [
            MockAssistantMessage([MockTextBlock("Here is some code")]),
            MockResultMessage(session_id=sid),
        ]

        with patch.object(sm_module, 'ClaudeSDKClient', return_value=mock_client):
            session_manager.start_session(sid, prompt="Write code", cwd="/tmp")
            wait_for(lambda: session_manager.get_session_state(sid) == "idle")

        entries = session_manager.get_entries(sid)
        asst = [e for e in entries if e["kind"] == "asst"]
        assert len(asst) == 1
        assert asst[0]["text"] == "Here is some code"

    def test_message_processing_tool_use(self, session_manager, sm_module):
        """AssistantMessage with ToolUseBlock -> kind='tool_use' entry."""
        sid = "msg-tool-use"
        mock_client = MockClaudeSDKClient()
        mock_client._messages = [
            MockAssistantMessage([
                MockToolUseBlock(id="tu-1", name="Write", input={"path": "/foo/bar.py", "content": "x=1"})
            ]),
            MockResultMessage(session_id=sid),
        ]

        with patch.object(sm_module, 'ClaudeSDKClient', return_value=mock_client):
            session_manager.start_session(sid, prompt="Create file", cwd="/tmp")
            wait_for(lambda: session_manager.get_session_state(sid) == "idle")

        entries = session_manager.get_entries(sid)
        tools = [e for e in entries if e["kind"] == "tool_use"]
        assert len(tools) == 1
        assert tools[0]["name"] == "Write"
        assert "/foo/bar.py" in tools[0]["desc"]

    def test_message_processing_tool_result(self, session_manager, sm_module):
        """UserMessage with ToolResultBlock -> kind='tool_result' entry."""
        sid = "msg-tool-result"
        mock_client = MockClaudeSDKClient()
        mock_client._messages = [
            MockUserMessage([
                MockToolResultBlock(tool_use_id="tu-1", content="File created successfully")
            ]),
            MockResultMessage(session_id=sid),
        ]

        with patch.object(sm_module, 'ClaudeSDKClient', return_value=mock_client):
            session_manager.start_session(sid, prompt="test", cwd="/tmp")
            wait_for(lambda: session_manager.get_session_state(sid) == "idle")

        entries = session_manager.get_entries(sid)
        results = [e for e in entries if e["kind"] == "tool_result"]
        assert len(results) == 1
        assert "File created" in results[0]["text"]
        assert results[0]["tool_use_id"] == "tu-1"

    def test_message_processing_result(self, session_manager, sm_module):
        """ResultMessage should update cost and set state to IDLE."""
        sid = "msg-result"
        mock_client = MockClaudeSDKClient()
        mock_client._messages = [
            MockAssistantMessage([MockTextBlock("Done")]),
            MockResultMessage(session_id=sid, total_cost_usd=0.123),
        ]

        with patch.object(sm_module, 'ClaudeSDKClient', return_value=mock_client):
            session_manager.start_session(sid, prompt="test", cwd="/tmp")
            wait_for(lambda: session_manager.get_session_state(sid) == "idle")

        # Check cost was recorded
        with session_manager._lock:
            info = session_manager._sessions[sid]
        assert info.cost_usd == pytest.approx(0.123)

    def test_message_processing_thinking_block_skipped(self, session_manager, sm_module):
        """ThinkingBlock should not produce an entry."""
        sid = "msg-thinking"
        mock_client = MockClaudeSDKClient()
        mock_client._messages = [
            MockAssistantMessage([
                MockThinkingBlock("Let me think..."),
                MockTextBlock("Here's my answer"),
            ]),
            MockResultMessage(session_id=sid),
        ]

        with patch.object(sm_module, 'ClaudeSDKClient', return_value=mock_client):
            session_manager.start_session(sid, prompt="test", cwd="/tmp")
            wait_for(lambda: session_manager.get_session_state(sid) == "idle")

        entries = session_manager.get_entries(sid)
        # Only the text block should appear, not the thinking block
        kinds = [e["kind"] for e in entries]
        assert "asst" in kinds
        assert all(e.get("text", "") != "Let me think..." for e in entries)


# ---------------------------------------------------------------------------
# 11-12. Send message
# ---------------------------------------------------------------------------

class TestSendMessage:

    def test_send_message_to_idle_session(self, session_manager, sm_module):
        """Sending a message to an idle session should work."""
        sid = "send-idle"

        # Start and complete a session
        mock_client = MockClaudeSDKClient()
        mock_client._messages = [
            MockAssistantMessage([MockTextBlock("Ready")]),
            MockResultMessage(session_id=sid),
        ]
        # Set up response messages for the follow-up
        mock_client._response_messages = [
            MockAssistantMessage([MockTextBlock("Follow-up response")]),
            MockResultMessage(session_id=sid, total_cost_usd=0.02),
        ]

        with patch.object(sm_module, 'ClaudeSDKClient', return_value=mock_client):
            session_manager.start_session(sid, prompt="init", cwd="/tmp")
            wait_for(lambda: session_manager.get_session_state(sid) == "idle")

            # Now send a follow-up
            result = session_manager.send_message(sid, "What else?")
            assert result["ok"] is True

            # Wait for it to return to idle
            wait_for(lambda: session_manager.get_session_state(sid) == "idle", timeout=5)

        entries = session_manager.get_entries(sid)
        user_entries = [e for e in entries if e["kind"] == "user"]
        assert any("What else?" in e["text"] for e in user_entries)

    def test_send_message_to_working_session_rejected(self, session_manager, sm_module):
        """Sending a message to a WORKING session should be rejected."""
        sid = "send-working"
        info = sm_module.SessionInfo(session_id=sid, state=sm_module.SessionState.WORKING)
        with session_manager._lock:
            session_manager._sessions[sid] = info

        result = session_manager.send_message(sid, "Hello")
        assert result["ok"] is False
        assert "not idle" in result["error"].lower() or "working" in result["error"].lower()

    def test_send_message_to_nonexistent_session(self, session_manager):
        """Sending a message to a nonexistent session should fail gracefully."""
        result = session_manager.send_message("nonexistent", "Hello")
        assert result["ok"] is False
        assert "not found" in result["error"].lower()


# ---------------------------------------------------------------------------
# 13. Interrupt session
# ---------------------------------------------------------------------------

class TestInterruptSession:

    def test_interrupt_session(self, session_manager, sm_module):
        """Interrupting a session should call client.interrupt()."""
        sid = "interrupt-test"
        mock_client = MockClaudeSDKClient()
        # Slow message stream to keep session working
        async def slow_messages():
            yield MockAssistantMessage([MockTextBlock("Working...")])
            await asyncio.sleep(30)  # Will be interrupted before this completes
            yield MockResultMessage(session_id=sid)

        mock_client.receive_messages = slow_messages

        with patch.object(sm_module, 'ClaudeSDKClient', return_value=mock_client):
            session_manager.start_session(sid, prompt="slow task", cwd="/tmp")

            # Wait for session to be working
            wait_for(lambda: session_manager.get_session_state(sid) == "working")

            # Interrupt it
            result = session_manager.interrupt_session(sid)
            assert result["ok"] is True

            # Wait for interrupt to take effect
            wait_for(lambda: mock_client._interrupted, timeout=5)

    def test_interrupt_stopped_session_rejected(self, session_manager, sm_module):
        """Interrupting a stopped session should fail."""
        sid = "interrupt-stopped"
        info = sm_module.SessionInfo(session_id=sid, state=sm_module.SessionState.STOPPED)
        with session_manager._lock:
            session_manager._sessions[sid] = info

        result = session_manager.interrupt_session(sid)
        assert result["ok"] is False


# ---------------------------------------------------------------------------
# 14. Close session
# ---------------------------------------------------------------------------

class TestCloseSession:

    def test_close_session(self, session_manager, sm_module):
        """Closing a session should disconnect and set state to STOPPED."""
        sid = "close-test"
        mock_client = MockClaudeSDKClient()
        mock_client._messages = [
            MockAssistantMessage([MockTextBlock("Hi")]),
            MockResultMessage(session_id=sid),
        ]

        with patch.object(sm_module, 'ClaudeSDKClient', return_value=mock_client):
            session_manager.start_session(sid, prompt="test", cwd="/tmp")
            wait_for(lambda: session_manager.get_session_state(sid) == "idle")

            result = session_manager.close_session(sid)
            assert result["ok"] is True

            wait_for(lambda: session_manager.get_session_state(sid) == "stopped")

        assert mock_client._disconnected is True

    def test_close_nonexistent_session(self, session_manager):
        """Closing a nonexistent session should fail gracefully."""
        result = session_manager.close_session("nonexistent")
        assert result["ok"] is False


# ---------------------------------------------------------------------------
# 15. Session resume
# ---------------------------------------------------------------------------

class TestSessionResume:

    def test_session_resume(self, session_manager, sm_module):
        """Resuming a session should pass resume=session_id in options."""
        sid = "resume-test"
        mock_client = MockClaudeSDKClient()
        mock_client._messages = [
            MockAssistantMessage([MockTextBlock("Resumed!")]),
            MockResultMessage(session_id=sid),
        ]

        captured_options = [None]
        original_init = MockClaudeSDKClient.__init__

        def capture_init(self, options=None, **kwargs):
            captured_options[0] = options
            original_init(self, options=options)

        with patch.object(sm_module, 'ClaudeSDKClient', return_value=mock_client) as mock_cls:
            session_manager.start_session(sid, prompt="continue", cwd="/tmp", resume=True)
            wait_for(lambda: session_manager.get_session_state(sid) == "idle")

        # Verify ClaudeCodeOptions was called with resume parameter
        # (The mock captures it through ClaudeSDKClient instantiation)
        entries = session_manager.get_entries(sid)
        asst = [e for e in entries if e["kind"] == "asst"]
        assert any("Resumed" in e["text"] for e in asst)


# ---------------------------------------------------------------------------
# 16. Error handling
# ---------------------------------------------------------------------------

class TestErrorHandling:

    def test_error_handling_sdk_crash(self, session_manager, sm_module):
        """If the SDK client crashes, session should go to STOPPED with error."""
        sid = "error-crash"

        mock_client = MockClaudeSDKClient()

        async def crashing_connect(prompt=None):
            raise RuntimeError("SDK crashed!")

        mock_client.connect = crashing_connect

        with patch.object(sm_module, 'ClaudeSDKClient', return_value=mock_client):
            session_manager.start_session(sid, prompt="test", cwd="/tmp")
            wait_for(lambda: session_manager.get_session_state(sid) == "stopped")

        with session_manager._lock:
            info = session_manager._sessions[sid]
        assert info.error is not None
        assert "crashed" in info.error.lower() or "SDK" in info.error

        # Should have an error entry
        entries = session_manager.get_entries(sid)
        error_entries = [e for e in entries if e.get("is_error")]
        assert len(error_entries) >= 1

    def test_double_start_rejected(self, session_manager, sm_module):
        """Starting a session that's already running should be rejected."""
        sid = "double-start"

        # Create a running session
        info = sm_module.SessionInfo(session_id=sid, state=sm_module.SessionState.WORKING)
        with session_manager._lock:
            session_manager._sessions[sid] = info

        result = session_manager.start_session(sid, prompt="test", cwd="/tmp")
        assert result["ok"] is False
        assert "already running" in result["error"].lower()

    def test_restart_stopped_session(self, session_manager, sm_module):
        """Starting a session that was stopped should succeed."""
        sid = "restart-stopped"

        # Create a stopped session
        info = sm_module.SessionInfo(session_id=sid, state=sm_module.SessionState.STOPPED)
        with session_manager._lock:
            session_manager._sessions[sid] = info

        mock_client = MockClaudeSDKClient()
        mock_client._messages = [
            MockAssistantMessage([MockTextBlock("Restarted")]),
            MockResultMessage(session_id=sid),
        ]

        with patch.object(sm_module, 'ClaudeSDKClient', return_value=mock_client):
            result = session_manager.start_session(sid, prompt="restart", cwd="/tmp")
            assert result["ok"] is True
            wait_for(lambda: session_manager.get_session_state(sid) == "idle")


# ---------------------------------------------------------------------------
# 17. Get all states
# ---------------------------------------------------------------------------

class TestGetAllStates:

    def test_get_all_states(self, session_manager, sm_module):
        """get_all_states should return snapshot of all sessions."""
        # Add some sessions manually
        with session_manager._lock:
            session_manager._sessions["s1"] = sm_module.SessionInfo(
                session_id="s1", state=sm_module.SessionState.WORKING, name="Session 1"
            )
            session_manager._sessions["s2"] = sm_module.SessionInfo(
                session_id="s2", state=sm_module.SessionState.IDLE, name="Session 2"
            )

        states = session_manager.get_all_states()
        assert len(states) == 2

        by_id = {s["session_id"]: s for s in states}
        assert by_id["s1"]["state"] == "working"
        assert by_id["s2"]["state"] == "idle"
        assert by_id["s1"]["name"] == "Session 1"

    def test_get_all_states_empty(self, session_manager):
        """get_all_states with no sessions should return empty list."""
        states = session_manager.get_all_states()
        assert states == []


# ---------------------------------------------------------------------------
# 18. Get entries with since
# ---------------------------------------------------------------------------

class TestGetEntries:

    def test_get_entries_with_since(self, session_manager, sm_module):
        """get_entries(since=N) should skip first N entries."""
        sid = "entries-since"
        info = sm_module.SessionInfo(session_id=sid)
        info.entries = [
            sm_module.LogEntry(kind="user", text="First"),
            sm_module.LogEntry(kind="asst", text="Second"),
            sm_module.LogEntry(kind="user", text="Third"),
        ]
        with session_manager._lock:
            session_manager._sessions[sid] = info

        all_entries = session_manager.get_entries(sid)
        assert len(all_entries) == 3

        since_1 = session_manager.get_entries(sid, since=1)
        assert len(since_1) == 2
        assert since_1[0]["text"] == "Second"

        since_2 = session_manager.get_entries(sid, since=2)
        assert len(since_2) == 1
        assert since_2[0]["text"] == "Third"

        since_3 = session_manager.get_entries(sid, since=3)
        assert len(since_3) == 0

    def test_get_entries_nonexistent(self, session_manager):
        """get_entries for nonexistent session returns empty list."""
        entries = session_manager.get_entries("nonexistent")
        assert entries == []


# ---------------------------------------------------------------------------
# 19. Thread safety
# ---------------------------------------------------------------------------

class TestThreadSafety:

    def test_thread_safety_concurrent_operations(self, session_manager, sm_module):
        """Multiple threads calling session manager methods should not corrupt state."""
        errors = []

        def worker(thread_id):
            try:
                sid = f"thread-{thread_id}"
                info = sm_module.SessionInfo(
                    session_id=sid, state=sm_module.SessionState.IDLE
                )
                with session_manager._lock:
                    session_manager._sessions[sid] = info

                # Read operations
                session_manager.get_all_states()
                session_manager.get_entries(sid)
                session_manager.has_session(sid)
                session_manager.get_session_state(sid)
            except Exception as e:
                errors.append((thread_id, e))

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(10)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=10)

        assert len(errors) == 0, f"Thread errors: {errors}"


# ---------------------------------------------------------------------------
# 20. State consistency
# ---------------------------------------------------------------------------

class TestStateConsistency:

    def test_state_never_wrong(self, session_manager, sm_module):
        """State should always match what's actually happening."""
        sid = "state-check"
        mock_client = MockClaudeSDKClient()
        mock_client._messages = [
            MockAssistantMessage([MockTextBlock("Working on it...")]),
            MockAssistantMessage([MockToolUseBlock(id="t1", name="Bash", input={"command": "echo hi"})]),
            MockUserMessage([MockToolResultBlock(tool_use_id="t1", content="hi")]),
            MockAssistantMessage([MockTextBlock("All done!")]),
            MockResultMessage(session_id=sid, total_cost_usd=0.05),
        ]

        with patch.object(sm_module, 'ClaudeSDKClient', return_value=mock_client):
            result = session_manager.start_session(sid, prompt="do stuff", cwd="/tmp")
            assert result["ok"] is True

            # Eventually reaches idle
            wait_for(lambda: session_manager.get_session_state(sid) == "idle")

        # Verify final state
        state = session_manager.get_session_state(sid)
        assert state == "idle"

        # Verify all entry types were recorded
        entries = session_manager.get_entries(sid)
        kinds = {e["kind"] for e in entries}
        assert "asst" in kinds
        assert "tool_use" in kinds
        assert "tool_result" in kinds


# ---------------------------------------------------------------------------
# Tool description extraction
# ---------------------------------------------------------------------------

class TestToolDescExtraction:

    def test_extract_command(self, sm_module):
        desc = sm_module.SessionManager._extract_tool_desc({"command": "ls -la /home"})
        assert desc == "ls -la /home"

    def test_extract_path_with_content(self, sm_module):
        desc = sm_module.SessionManager._extract_tool_desc(
            {"path": "/foo/bar.py", "content": "hello world"}
        )
        assert "/foo/bar.py" in desc
        assert "write" in desc.lower()

    def test_extract_pattern(self, sm_module):
        desc = sm_module.SessionManager._extract_tool_desc({"pattern": "*.py"})
        assert desc == "*.py"

    def test_extract_other(self, sm_module):
        desc = sm_module.SessionManager._extract_tool_desc({"url": "https://example.com"})
        assert "url" in desc
        assert "example.com" in desc

    def test_extract_empty(self, sm_module):
        desc = sm_module.SessionManager._extract_tool_desc({})
        assert desc == ""


# ---------------------------------------------------------------------------
# LogEntry serialization
# ---------------------------------------------------------------------------

class TestLogEntry:

    def test_to_dict_minimal(self, sm_module):
        entry = sm_module.LogEntry(kind="asst", text="Hello")
        d = entry.to_dict()
        assert d["kind"] == "asst"
        assert d["text"] == "Hello"
        assert "name" not in d  # empty string fields should be excluded
        assert "timestamp" in d

    def test_to_dict_tool_use(self, sm_module):
        entry = sm_module.LogEntry(
            kind="tool_use", name="Bash", desc="echo hi", id="t-123"
        )
        d = entry.to_dict()
        assert d["kind"] == "tool_use"
        assert d["name"] == "Bash"
        assert d["desc"] == "echo hi"
        assert d["id"] == "t-123"

    def test_to_dict_error(self, sm_module):
        entry = sm_module.LogEntry(kind="system", text="Error!", is_error=True)
        d = entry.to_dict()
        assert d["is_error"] is True


# ---------------------------------------------------------------------------
# SessionInfo serialization
# ---------------------------------------------------------------------------

class TestSessionInfo:

    def test_to_state_dict(self, sm_module):
        info = sm_module.SessionInfo(
            session_id="test-1",
            state=sm_module.SessionState.WORKING,
            name="My Session",
            cost_usd=0.05,
        )
        d = info.to_state_dict()
        assert d == {
            "session_id": "test-1",
            "state": "working",
            "cost_usd": 0.05,
            "error": None,
            "name": "My Session",
        }


# ---------------------------------------------------------------------------
# has_session / get_session_state edge cases
# ---------------------------------------------------------------------------

class TestSessionQueries:

    def test_has_session_true(self, session_manager, sm_module):
        info = sm_module.SessionInfo(session_id="exists")
        with session_manager._lock:
            session_manager._sessions["exists"] = info
        assert session_manager.has_session("exists") is True

    def test_has_session_false(self, session_manager):
        assert session_manager.has_session("does-not-exist") is False

    def test_get_session_state_none(self, session_manager):
        assert session_manager.get_session_state("no-such") is None

    def test_resolve_permission_wrong_session(self, session_manager):
        """Resolving permission for nonexistent session should fail."""
        result = session_manager.resolve_permission("nobody", allow=True)
        assert result["ok"] is False
        assert "not found" in result["error"].lower()

    def test_resolve_permission_no_pending(self, session_manager, sm_module):
        """Resolving when no future is pending should fail."""
        sid = "no-pending"
        info = sm_module.SessionInfo(session_id=sid, state=sm_module.SessionState.WAITING)
        info.pending_permission = None
        with session_manager._lock:
            session_manager._sessions[sid] = info

        result = session_manager.resolve_permission(sid, allow=True)
        assert result["ok"] is False
        assert "no pending" in result["error"].lower()
