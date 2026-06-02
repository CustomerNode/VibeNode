"""
Comprehensive tests for the permission system end-to-end.

Covers:
- Permission lifecycle (callback -> WAITING -> resolve -> WORKING)
- Double-click / race-condition protection
- Concurrent permissions across sessions
- Permission data flow (tool_name, tool_input passthrough)
- State consistency during permission awaits
- WebSocket event verification
"""

import asyncio
import threading
import time
import pytest
from unittest.mock import AsyncMock, MagicMock, patch, call

from dataclasses import dataclass, field
from typing import Optional


# ---------------------------------------------------------------------------
# Mock SDK types  (mirrors test_session_manager.py)
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


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def mock_socketio():
    """Create a mock SocketIO instance that records emitted events."""
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
    """Return the reloaded session_manager module with mocked SDK types."""
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
    """Create a SessionManager with mocked SDK and SocketIO, started and ready."""
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


def create_future_on_loop(loop):
    """Create an asyncio.Future on the given event loop from a non-loop thread."""
    created = [None]
    event = threading.Event()

    def _create():
        created[0] = loop.create_future()
        event.set()

    loop.call_soon_threadsafe(_create)
    event.wait(timeout=5)
    return created[0]


def make_permission_client(session_id, sm_module, session_manager,
                           tool_name="Bash", tool_input=None,
                           post_permission_messages=None):
    """Create a mock client whose message stream triggers the permission callback.

    Returns (mock_client, permission_triggered_event, permission_result_holder).
    The client's receive_messages will:
      1. Yield an assistant message
      2. Call can_use_tool (the permission callback) and block
      3. After permission resolves, yield remaining messages and ResultMessage

    The caller must resolve the permission via session_manager.resolve_permission()
    or interrupt to unblock the stream.
    """
    if tool_input is None:
        tool_input = {"command": "rm -rf /"}
    if post_permission_messages is None:
        post_permission_messages = []

    permission_triggered = threading.Event()
    permission_result_holder = [None]

    # Capture the actual permission callback at construction time.
    # _make_permission_callback returns a standalone coroutine function
    # that doesn't depend on the options object.
    captured_callback = session_manager._make_permission_callback(session_id)

    mock_client = MockClaudeSDKClient()

    async def patched_receive_messages():
        # Yield an initial assistant text message
        yield MockAssistantMessage([MockTextBlock("I need to run a tool.")])

        # Invoke the captured permission callback directly
        permission_triggered.set()
        result = await captured_callback(tool_name, tool_input, MockToolPermissionContext())
        permission_result_holder[0] = result

        # Yield post-permission messages
        for msg in post_permission_messages:
            yield msg

        # End with a result
        yield MockResultMessage(session_id=session_id, total_cost_usd=0.03)

    mock_client.receive_messages = patched_receive_messages

    return mock_client, permission_triggered, permission_result_holder
class TestDoubleClickProtection:

    def test_resolve_twice_rapidly_second_returns_error(self, session_manager, sm_module):
        """Calling resolve_permission twice rapidly -- second call gets error, no crash."""
        sid = "double-click-01"
        info = sm_module.SessionInfo(session_id=sid, state=sm_module.SessionState.WAITING)
        with session_manager._lock:
            session_manager._sessions[sid] = info

        future = create_future_on_loop(session_manager._loop)
        info.pending_permission = future

        # First resolve -- succeeds
        r1 = session_manager.resolve_permission(sid, allow=True)
        assert r1["ok"] is True

        # After first resolve, pending_permission is cleared and state is no longer WAITING
        # The second call should fail gracefully
        r2 = session_manager.resolve_permission(sid, allow=True)
        assert r2["ok"] is False

    def test_resolve_after_future_already_done(self, session_manager, sm_module):
        """Resolving after the future is already done should fail gracefully."""
        sid = "double-click-02"
        info = sm_module.SessionInfo(session_id=sid, state=sm_module.SessionState.WAITING)
        with session_manager._lock:
            session_manager._sessions[sid] = info

        future = create_future_on_loop(session_manager._loop)
        info.pending_permission = future

        # Resolve once
        r1 = session_manager.resolve_permission(sid, allow=True)
        assert r1["ok"] is True

        # pending_permission is now None, state changed
        # Even if we force state back, pending_permission is None
        info.state = sm_module.SessionState.WAITING
        r2 = session_manager.resolve_permission(sid, allow=True)
        assert r2["ok"] is False
        assert "no pending" in r2["error"].lower()

    def test_resolve_for_wrong_session(self, session_manager, sm_module):
        """Resolving permission for a nonexistent session should error."""
        result = session_manager.resolve_permission("wrong-session-id", allow=True)
        assert result["ok"] is False
        assert "not found" in result["error"].lower()

    def test_resolve_for_session_with_no_pending(self, session_manager, sm_module):
        """Resolving when session exists but has no pending permission should error."""
        sid = "double-click-04"
        info = sm_module.SessionInfo(session_id=sid, state=sm_module.SessionState.WAITING)
        info.pending_permission = None
        with session_manager._lock:
            session_manager._sessions[sid] = info

        result = session_manager.resolve_permission(sid, allow=True)
        assert result["ok"] is False
        assert "no pending" in result["error"].lower()

    def test_resolve_for_stopped_session(self, session_manager, sm_module):
        """Resolving permission for a stopped session should error."""
        sid = "double-click-05"
        info = sm_module.SessionInfo(session_id=sid, state=sm_module.SessionState.STOPPED)
        with session_manager._lock:
            session_manager._sessions[sid] = info

        result = session_manager.resolve_permission(sid, allow=True)
        assert result["ok"] is False
        assert "stopped" in result["error"].lower() or "not waiting" in result["error"].lower()

    def test_resolve_for_working_session(self, session_manager, sm_module):
        """Resolving permission for a WORKING (not WAITING) session should error."""
        sid = "double-click-06"
        info = sm_module.SessionInfo(session_id=sid, state=sm_module.SessionState.WORKING)
        with session_manager._lock:
            session_manager._sessions[sid] = info

        result = session_manager.resolve_permission(sid, allow=True)
        assert result["ok"] is False
        assert "not waiting" in result["error"].lower()

    def test_resolve_for_idle_session(self, session_manager, sm_module):
        """Resolving permission for an IDLE session should error."""
        sid = "double-click-07"
        info = sm_module.SessionInfo(session_id=sid, state=sm_module.SessionState.IDLE)
        with session_manager._lock:
            session_manager._sessions[sid] = info

        result = session_manager.resolve_permission(sid, allow=True)
        assert result["ok"] is False
class TestWebSocketEventVerification:

    def test_frontend_invalid_action_emits_error(self, mock_socketio):
        """Frontend permission_response with invalid action should emit error."""
        sdk_mocks = {
            'claude_code_sdk': MagicMock(),
            'claude_code_sdk.types': MagicMock(),
        }
        with patch.dict('sys.modules', sdk_mocks):
            from flask import Flask
            from flask_socketio import SocketIO
            from app.routes.ws_events import register_ws_events

            app = Flask(__name__)
            app.config['TESTING'] = True
            socketio = SocketIO(app, async_mode='threading')

            mock_sm = MagicMock()
            mock_sm.get_all_states.return_value = []
            mock_sm.resolve_permission.return_value = {"ok": True}
            app.session_manager = mock_sm

            register_ws_events(socketio, app)
            client = socketio.test_client(app)
            client.get_received()  # clear connect events

            # Send invalid action
            client.emit('permission_response', {
                'session_id': 's1',
                'action': 'invalid_action',
            })

            received = client.get_received()
            errors = [msg for msg in received if msg['name'] == 'error']
            assert len(errors) >= 1
            assert 'action' in errors[0]['args'][0]['message'].lower()

            client.disconnect()

    def test_frontend_missing_session_id_emits_error(self, mock_socketio):
        """Frontend permission_response with no session_id should emit error."""
        sdk_mocks = {
            'claude_code_sdk': MagicMock(),
            'claude_code_sdk.types': MagicMock(),
        }
        with patch.dict('sys.modules', sdk_mocks):
            from flask import Flask
            from flask_socketio import SocketIO
            from app.routes.ws_events import register_ws_events

            app = Flask(__name__)
            app.config['TESTING'] = True
            socketio = SocketIO(app, async_mode='threading')

            mock_sm = MagicMock()
            mock_sm.get_all_states.return_value = []
            app.session_manager = mock_sm

            register_ws_events(socketio, app)
            client = socketio.test_client(app)
            client.get_received()

            # Send without session_id
            client.emit('permission_response', {
                'action': 'y',
            })

            received = client.get_received()
            errors = [msg for msg in received if msg['name'] == 'error']
            assert len(errors) >= 1
            assert 'session_id' in errors[0]['args'][0]['message'].lower()

            client.disconnect()

    def test_frontend_resolve_failure_emits_error(self, mock_socketio):
        """Frontend permission_response where resolve_permission fails should emit error."""
        sdk_mocks = {
            'claude_code_sdk': MagicMock(),
            'claude_code_sdk.types': MagicMock(),
        }
        with patch.dict('sys.modules', sdk_mocks):
            from flask import Flask
            from flask_socketio import SocketIO
            from app.routes.ws_events import register_ws_events

            app = Flask(__name__)
            app.config['TESTING'] = True
            socketio = SocketIO(app, async_mode='threading')

            mock_sm = MagicMock()
            mock_sm.get_all_states.return_value = []
            mock_sm.resolve_permission.return_value = {
                "ok": False,
                "error": "No pending permission"
            }
            app.session_manager = mock_sm

            register_ws_events(socketio, app)
            client = socketio.test_client(app)
            client.get_received()

            client.emit('permission_response', {
                'session_id': 's1',
                'action': 'y',
            })

            received = client.get_received()
            errors = [msg for msg in received if msg['name'] == 'error']
            assert len(errors) >= 1
            assert 'no pending' in errors[0]['args'][0]['message'].lower()

            client.disconnect()

    def test_non_dict_data_to_permission_response(self, mock_socketio):
        """Sending non-dict data to permission_response handler should emit error."""
        sdk_mocks = {
            'claude_code_sdk': MagicMock(),
            'claude_code_sdk.types': MagicMock(),
        }
        with patch.dict('sys.modules', sdk_mocks):
            from flask import Flask
            from flask_socketio import SocketIO
            from app.routes.ws_events import register_ws_events

            app = Flask(__name__)
            app.config['TESTING'] = True
            socketio = SocketIO(app, async_mode='threading')

            mock_sm = MagicMock()
            mock_sm.get_all_states.return_value = []
            app.session_manager = mock_sm

            register_ws_events(socketio, app)
            client = socketio.test_client(app)
            client.get_received()

            client.emit('permission_response', "not a dict")

            received = client.get_received()
            errors = [msg for msg in received if msg['name'] == 'error']
            assert len(errors) >= 1

            client.disconnect()


# ---------------------------------------------------------------------------
# AskUserQuestion interception
# ---------------------------------------------------------------------------
# Regression coverage for the "empty answer kills the turn" bug: the Claude SDK
# AskUserQuestion tool has no interactive UI in VibeNode, so it must be denied
# with a redirect message (never auto-approved/executed, never prompted).
# See the ASK_USER_QUESTION_* constants and can_use_tool in session_manager.py.

def _run_async(coro):
    """Run a coroutine to completion on a throwaway event loop.

    The AskUserQuestion / auto-approve branches of can_use_tool resolve
    synchronously (no anyio polling), so a fresh loop per call is fine and
    keeps these tests off the manager's background loop.
    """
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


class TestAskUserQuestionInterception:

    def _register_live_session(self, session_manager, sm_module, sid):
        """Create a registered session whose transport reports as alive.

        can_use_tool checks transport liveness before anything else and would
        otherwise short-circuit to a 'Transport disconnected' deny against the
        mock client.  We register a real SessionInfo with a client and force
        is_transport_alive True so execution reaches the AskUserQuestion guard.
        """
        info = sm_module.SessionInfo(
            session_id=sid, state=sm_module.SessionState.WORKING
        )
        info.client = MockClaudeSDKClient()
        with session_manager._lock:
            session_manager._sessions[sid] = info
        # Force the transport to look alive so the dead-transport early-abort
        # branch doesn't fire ahead of the AskUserQuestion guard.
        session_manager._sdk.is_transport_alive = lambda client: True
        return info

    def test_ask_user_question_is_redirected_not_executed(self, session_manager, sm_module):
        """AskUserQuestion is denied with a redirect message instead of running.

        This is policy-independent: even in the default ('manual') policy the
        tool is intercepted BEFORE the prompt path, so the user never sees an
        unanswerable allow/deny dialog and the model never gets an empty answer.
        """
        sid = "auq-redirect-01"
        info = self._register_live_session(session_manager, sm_module, sid)
        callback = session_manager._make_permission_callback(sid)

        question_input = {"questions": [{
            "question": "Which fix should I apply?",
            "header": "Scope",
            "multiSelect": False,
            "options": [{"label": "A", "description": "do A"},
                        {"label": "B", "description": "do B"}],
        }]}

        result = _run_async(callback(
            "AskUserQuestion", question_input, MockToolPermissionContext()
        ))

        # Denied, but WITHOUT interrupt — a non-interrupting deny feeds the
        # message back to the model as the tool result so the turn continues.
        assert isinstance(result, sm_module.PermissionResult)
        assert result.action == sm_module.PermissionAction.DENY
        assert result.interrupt is False
        # The message must steer the model to proceed and defer the question:
        # it earns no right to ask until work has been produced.
        assert result.message == sm_module.ASK_USER_QUESTION_REDIRECT_MESSAGE
        assert "produced work" in result.message

        # AskUserQuestion must never be remembered as an allowed tool.
        assert "AskUserQuestion" not in info.always_allowed_tools
        assert "AskUserQuestion" not in info.almost_always_allowed_tools

        # The intercept is surfaced in the session timeline for the user.
        assert any(
            e.kind == "permission" and "AskUserQuestion intercepted" in e.text
            for e in info.entries
        )

    def test_redirect_beats_auto_policy_but_other_tools_still_approve(
        self, session_manager, sm_module
    ):
        """In 'auto' policy AskUserQuestion is still redirected; normal tools approve.

        Proves the guard runs ahead of the auto-approval path (so the empty
        answer can't happen) without breaking ordinary auto-approval.
        """
        sid = "auq-redirect-02"
        self._register_live_session(session_manager, sm_module, sid)
        # In-memory policy flip only — do NOT call set_permission_policy(), which
        # would persist to ~/.claude and clobber the real user's policy on disk.
        session_manager._pm._permission_policy = "auto"
        callback = session_manager._make_permission_callback(sid)

        auq = _run_async(callback(
            "AskUserQuestion", {"questions": []}, MockToolPermissionContext()
        ))
        assert auq.action == sm_module.PermissionAction.DENY
        assert auq.interrupt is False
        assert auq.message == sm_module.ASK_USER_QUESTION_REDIRECT_MESSAGE

        # Control: a normal tool is still auto-approved under 'auto' policy.
        bash = _run_async(callback(
            "Bash", {"command": "ls"}, MockToolPermissionContext()
        ))
        assert bash.action == sm_module.PermissionAction.ALLOW
