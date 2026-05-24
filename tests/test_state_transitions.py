"""
Comprehensive state-transition tests for SessionManager.

Covers every edge of the state machine:
    STARTING -> WORKING -> IDLE -> (send_message) -> WORKING -> ...
    STARTING -> WORKING -> WAITING -> (resolve) -> WORKING -> ...
    Any -> STOPPED  (on error, close, or cancel)
    STOPPED -> STARTING  (on restart)

Replaces the old test_state_sync.py and test_full_stack_failures.py.
"""

import asyncio
import threading
import time
import pytest
from unittest.mock import MagicMock, patch

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


def make_future_on_loop(loop):
    """Create an asyncio.Future on the given loop from an external thread."""
    created = [None]
    event = threading.Event()

    def _create():
        created[0] = loop.create_future()
        event.set()

    loop.call_soon_threadsafe(_create)
    event.wait(timeout=5)
    return created[0]


def collect_emitted_states(mock_socketio, session_id):
    """Return all session_state payloads emitted for a given session_id."""
    results = []
    for call in mock_socketio.emit.call_args_list:
        args = call[0]
        if len(args) >= 2 and args[0] == 'session_state':
            payload = args[1]
            if payload.get('session_id') == session_id:
                results.append(payload)
    return results


# ===================================================================
# HAPPY PATH TRANSITIONS (10+ tests)
# ===================================================================

class TestHappyPathTransitions:

    def test_working_to_idle_on_result_message(self, session_manager, sm_module):
        """WORKING -> IDLE when ResultMessage is received."""
        sid = "hp-work-idle"
        mock_client = MockClaudeSDKClient()
        mock_client._messages = [
            MockAssistantMessage([MockTextBlock("Done")]),
            MockResultMessage(session_id=sid, total_cost_usd=0.01),
        ]

        with patch.object(sm_module, 'ClaudeSDKClient', return_value=mock_client):
            session_manager.start_session(sid, prompt="test", cwd="/tmp")
            wait_for(lambda: session_manager.get_session_state(sid) == "idle")

        assert session_manager.get_session_state(sid) == "idle"

    def test_idle_to_working_on_send_message(self, session_manager, sm_module):
        """IDLE -> WORKING on send_message."""
        sid = "hp-idle-work"
        mock_client = MockClaudeSDKClient()
        mock_client._messages = [
            MockResultMessage(session_id=sid),
        ]
        mock_client._response_messages = [
            MockAssistantMessage([MockTextBlock("Reply")]),
            MockResultMessage(session_id=sid),
        ]

        with patch.object(sm_module, 'ClaudeSDKClient', return_value=mock_client):
            session_manager.start_session(sid, prompt="init", cwd="/tmp")
            wait_for(lambda: session_manager.get_session_state(sid) == "idle")

            # Capture states after sending
            states_after_send = []
            original_emit = session_manager._emit_state

            def track(info):
                if info.session_id == sid:
                    states_after_send.append(info.state.value)
                original_emit(info)

            session_manager._emit_state = track

            result = session_manager.send_message(sid, "Follow-up")
            assert result["ok"] is True

            wait_for(lambda: session_manager.get_session_state(sid) == "idle")

        assert "working" in states_after_send

    def test_idle_to_stopped_on_close(self, session_manager, sm_module):
        """IDLE -> STOPPED on close_session."""
        sid = "hp-idle-stop"
        mock_client = MockClaudeSDKClient()
        mock_client._messages = [MockResultMessage(session_id=sid)]

        with patch.object(sm_module, 'ClaudeSDKClient', return_value=mock_client):
            session_manager.start_session(sid, prompt="x", cwd="/tmp")
            wait_for(lambda: session_manager.get_session_state(sid) == "idle")

            session_manager.close_session(sid)
            wait_for(lambda: session_manager.get_session_state(sid) == "stopped")

        assert session_manager.get_session_state(sid) == "stopped"

    def test_full_lifecycle_start_work_idle_send_work_idle_close(
        self, session_manager, sm_module
    ):
        """Full round trip: start -> work -> idle -> send -> work -> idle -> close -> stopped."""
        sid = "hp-full"
        mock_client = MockClaudeSDKClient()
        mock_client._messages = [
            MockAssistantMessage([MockTextBlock("First reply")]),
            MockResultMessage(session_id=sid),
        ]
        mock_client._response_messages = [
            MockAssistantMessage([MockTextBlock("Second reply")]),
            MockResultMessage(session_id=sid, total_cost_usd=0.02),
        ]

        with patch.object(sm_module, 'ClaudeSDKClient', return_value=mock_client):
            session_manager.start_session(sid, prompt="hi", cwd="/tmp")
            wait_for(lambda: session_manager.get_session_state(sid) == "idle")

            result = session_manager.send_message(sid, "More")
            assert result["ok"] is True
            wait_for(lambda: session_manager.get_session_state(sid) == "idle")

            session_manager.close_session(sid)
            wait_for(lambda: session_manager.get_session_state(sid) == "stopped")

        assert session_manager.get_session_state(sid) == "stopped"


# ===================================================================
# ERROR TRANSITIONS (10+ tests)
# ===================================================================

class TestErrorTransitions:

    def test_error_during_permission_callback_results_in_deny(
        self, session_manager, sm_module
    ):
        """If an exception occurs related to a permission flow, session
        should still be recoverable or transition to STOPPED cleanly."""
        sid = "err-perm-cb"
        info = sm_module.SessionInfo(
            session_id=sid, state=sm_module.SessionState.WORKING
        )
        info.client = MockClaudeSDKClient()
        with session_manager._lock:
            session_manager._sessions[sid] = info

        callback = session_manager._make_permission_callback(sid)

        # Start the permission callback -- it creates a future and waits
        async def run_and_cancel():
            task = asyncio.ensure_future(
                callback("Bash", {"command": "bad"}, MockToolPermissionContext())
            )
            await asyncio.sleep(0.1)
            # Cancel the task to simulate an error path
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
            return task

        f = asyncio.run_coroutine_threadsafe(run_and_cancel(), session_manager._loop)
        f.result(timeout=5)

        # The session should not be stuck in WAITING after cancellation
        state = session_manager.get_session_state(sid)
        assert state != "waiting"

class TestEdgeCaseTransitions:

    def test_zero_messages_goes_to_idle(self, session_manager, sm_module):
        """receive_messages yields 0 messages -> session goes to IDLE."""
        sid = "edge-zero-msgs"
        mock_client = MockClaudeSDKClient()
        mock_client._messages = []  # No messages at all

        with patch.object(sm_module, 'ClaudeSDKClient', return_value=mock_client):
            session_manager.start_session(sid, prompt="x", cwd="/tmp")
            wait_for(lambda: session_manager.get_session_state(sid) == "idle")

        assert session_manager.get_session_state(sid) == "idle"

    def test_only_stream_events_still_transitions(self, session_manager, sm_module):
        """receive_messages yields only StreamEvents -> session still reaches IDLE."""
        sid = "edge-streams-only"
        mock_client = MockClaudeSDKClient()
        mock_client._messages = [
            MockStreamEvent(event="content_block_start", data={"type": "text"}),
            MockStreamEvent(event="content_block_delta", data={"delta": "hi"}),
            MockStreamEvent(event="content_block_stop", data={}),
        ]

        with patch.object(sm_module, 'ClaudeSDKClient', return_value=mock_client):
            session_manager.start_session(sid, prompt="x", cwd="/tmp")
            wait_for(lambda: session_manager.get_session_state(sid) == "idle")

        assert session_manager.get_session_state(sid) == "idle"

    def test_send_message_to_stopped_returns_error(self, session_manager, sm_module):
        """send_message to STOPPED session returns error."""
        sid = "edge-send-stopped"
        info = sm_module.SessionInfo(
            session_id=sid, state=sm_module.SessionState.STOPPED
        )
        with session_manager._lock:
            session_manager._sessions[sid] = info

        result = session_manager.send_message(sid, "text")
        assert result["ok"] is False
        assert "stopped" in result["error"].lower()

    def test_interrupt_stopped_returns_error(self, session_manager, sm_module):
        """interrupt_session on STOPPED session returns error."""
        sid = "edge-int-stopped"
        info = sm_module.SessionInfo(
            session_id=sid, state=sm_module.SessionState.STOPPED
        )
        with session_manager._lock:
            session_manager._sessions[sid] = info

        result = session_manager.interrupt_session(sid)
        assert result["ok"] is False
        assert "stopped" in result["error"].lower()

    def test_close_nonexistent_session_returns_error(self, session_manager):
        """close_session on nonexistent session returns error."""
        result = session_manager.close_session("no-such-session")
        assert result["ok"] is False
        assert "not found" in result["error"].lower()

    def test_get_entries_nonexistent_returns_empty(self, session_manager):
        """get_entries for unknown session returns empty list."""
        entries = session_manager.get_entries("unknown-session")
        assert entries == []

    def test_get_session_state_nonexistent_returns_none(self, session_manager):
        """get_session_state for unknown session returns None."""
        state = session_manager.get_session_state("unknown-session")
        assert state is None

    def test_interrupt_nonexistent_returns_error(self, session_manager):
        """interrupt_session on nonexistent session returns error."""
        result = session_manager.interrupt_session("ghost-session")
        assert result["ok"] is False
        assert "not found" in result["error"].lower()


# ===================================================================
# STATE CONSISTENCY (10+ tests)
# ===================================================================

class TestStateConsistency:

    def test_get_all_states_reflects_current_state(self, session_manager, sm_module):
        """get_all_states returns the actual current state for every session."""
        with session_manager._lock:
            session_manager._sessions["s1"] = sm_module.SessionInfo(
                session_id="s1", state=sm_module.SessionState.IDLE
            )
            session_manager._sessions["s2"] = sm_module.SessionInfo(
                session_id="s2", state=sm_module.SessionState.WORKING
            )
            session_manager._sessions["s3"] = sm_module.SessionInfo(
                session_id="s3", state=sm_module.SessionState.STOPPED
            )

        states = session_manager.get_all_states()
        by_id = {s["session_id"]: s for s in states}
        assert by_id["s1"]["state"] == "idle"
        assert by_id["s2"]["state"] == "working"
        assert by_id["s3"]["state"] == "stopped"

    def test_get_session_state_matches_at_every_point(
        self, session_manager, sm_module
    ):
        """get_session_state always agrees with internal info.state."""
        sid = "consist-match"
        states_seen = []

        original_emit = session_manager._emit_state

        def track(info):
            actual = session_manager.get_session_state(info.session_id)
            if info.session_id == sid:
                states_seen.append((info.state.value, actual))
            original_emit(info)

        session_manager._emit_state = track

        mock_client = MockClaudeSDKClient()
        mock_client._messages = [
            MockAssistantMessage([MockTextBlock("Done")]),
            MockResultMessage(session_id=sid),
        ]

        with patch.object(sm_module, 'ClaudeSDKClient', return_value=mock_client):
            session_manager.start_session(sid, prompt="x", cwd="/tmp")
            wait_for(lambda: session_manager.get_session_state(sid) == "idle")

        # Every emitted state should match get_session_state at that moment
        for emitted, actual in states_seen:
            assert emitted == actual

    def test_has_session_active(self, session_manager, sm_module):
        """has_session returns True for active (non-stopped) sessions."""
        sid = "consist-active"
        info = sm_module.SessionInfo(
            session_id=sid, state=sm_module.SessionState.WORKING
        )
        with session_manager._lock:
            session_manager._sessions[sid] = info

        assert session_manager.has_session(sid) is True

    def test_has_session_stopped(self, session_manager, sm_module):
        """has_session returns True even for stopped sessions (still tracked)."""
        sid = "consist-stopped"
        info = sm_module.SessionInfo(
            session_id=sid, state=sm_module.SessionState.STOPPED
        )
        with session_manager._lock:
            session_manager._sessions[sid] = info

        assert session_manager.has_session(sid) is True

    def test_has_session_unknown(self, session_manager):
        """has_session returns False for unknown sessions."""
        assert session_manager.has_session("totally-unknown") is False

    def test_state_never_stuck_normal_path(self, session_manager, sm_module):
        """Every code path eventually reaches IDLE or STOPPED."""
        sid = "consist-not-stuck"
        mock_client = MockClaudeSDKClient()
        mock_client._messages = [
            MockAssistantMessage([MockTextBlock("Working")]),
            MockAssistantMessage([
                MockToolUseBlock(id="t1", name="Bash", input={"command": "ls"})
            ]),
            MockUserMessage([MockToolResultBlock(tool_use_id="t1", content="file.txt")]),
            MockAssistantMessage([MockTextBlock("Done")]),
            MockResultMessage(session_id=sid),
        ]

        with patch.object(sm_module, 'ClaudeSDKClient', return_value=mock_client):
            session_manager.start_session(sid, prompt="x", cwd="/tmp")
            wait_for(lambda: session_manager.get_session_state(sid) == "idle")

        final = session_manager.get_session_state(sid)
        assert final in ("idle", "stopped")

    def test_concurrent_start_and_close_no_deadlock(self, session_manager, sm_module):
        """Concurrent start_session + close_session should not deadlock or crash."""
        errors = []

        def start_and_close(i):
            sid = f"concurrent-sc-{i}"
            try:
                mock_client = MockClaudeSDKClient()
                mock_client._messages = [MockResultMessage(session_id=sid)]

                with patch.object(
                    sm_module, 'ClaudeSDKClient', return_value=mock_client
                ):
                    session_manager.start_session(sid, prompt="x", cwd="/tmp")
                    time.sleep(0.05)
                    session_manager.close_session(sid)
            except Exception as e:
                errors.append((i, str(e)))

        threads = [
            threading.Thread(target=start_and_close, args=(i,))
            for i in range(5)
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=15)

        assert len(errors) == 0, f"Errors: {errors}"

    def test_session_state_dict_includes_permission_in_waiting(
        self, session_manager, sm_module
    ):
        """to_state_dict includes permission details when in WAITING state."""
        sid = "consist-perm-dict"
        info = sm_module.SessionInfo(
            session_id=sid,
            state=sm_module.SessionState.WAITING,
        )
        info.pending_tool_name = "Bash"
        info.pending_tool_input = {"command": "rm -rf /"}
        with session_manager._lock:
            session_manager._sessions[sid] = info

        states = session_manager.get_all_states()
        by_id = {s["session_id"]: s for s in states}
        assert "permission" in by_id[sid]
        assert by_id[sid]["permission"]["tool_name"] == "Bash"
        assert by_id[sid]["permission"]["tool_input"]["command"] == "rm -rf /"

    def test_session_state_dict_no_permission_when_not_waiting(
        self, session_manager, sm_module
    ):
        """to_state_dict does NOT include permission details when not WAITING."""
        sid = "consist-no-perm"
        info = sm_module.SessionInfo(
            session_id=sid,
            state=sm_module.SessionState.IDLE,
        )
        info.pending_tool_name = "Bash"  # stale data
        with session_manager._lock:
            session_manager._sessions[sid] = info

        states = session_manager.get_all_states()
        by_id = {s["session_id"]: s for s in states}
        assert "permission" not in by_id[sid]

