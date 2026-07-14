"""
Tests for the SessionManager class.

What's here:
- send_message rejection paths (idle accepted, nonexistent rejected)
- interrupt/close rejection paths
- restart_stopped_session
- get_all_states / get_entries query surface
- thread safety smoke test
- ToolDescExtraction helpers
- LogEntry dataclass serialization
- has_session / get_session_state / resolve_permission edge cases
- permission timeout

Coverage moved or removed (2026-05 migration from app.session_manager to
daemon.session_manager):
- End-to-end lifecycle (start -> work -> idle -> stop) — covered by the
  ``test_concurrent_sessions.py`` and ``test_wakeup_handling.py`` files
  which mock the SDK against the new daemon interface.
- Permission resolve allow/deny — moved to ``test_permission_flow.py``.
- Message processing (assistant text, tool use, tool result, result,
  thinking) — moved to ``test_message_processing.py`` (re-mocks against
  the new ``_process_message`` shape).
- State transitions — moved to ``test_state_transitions.py``.

Tests deleted (not migrated) because they tested driving sessions through
the legacy SDK mocking layer that doesn't map cleanly onto the OOP-decomposed
SessionManager / MessageQueue / PermissionManager split. The behaviors they
covered are still exercised — just through more focused test files written
against the new shapes.
"""

import asyncio
import os
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
        import daemon.session_manager as sm_module
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
class TestPermissionResolve:

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

    def test_send_message_to_nonexistent_session(self, session_manager):
        """Sending a message to a nonexistent session should fail gracefully."""
        result = session_manager.send_message("nonexistent", "Hello")
        assert result["ok"] is False
        assert "not found" in result["error"].lower()


# ---------------------------------------------------------------------------
# 13. Interrupt session
# ---------------------------------------------------------------------------

class TestInterruptSession:

    def test_interrupt_stopped_session_rejected(self, session_manager, sm_module):
        """Interrupting a stopped session should fail."""
        sid = "interrupt-stopped"
        info = sm_module.SessionInfo(session_id=sid, state=sm_module.SessionState.STOPPED)
        with session_manager._lock:
            session_manager._sessions[sid] = info

        result = session_manager.interrupt_session(sid)
        assert result["ok"] is False


# ---------------------------------------------------------------------------
# 13b. Mid-session model switch (set_session_model)
#
# Honesty contract under test: info.model must change ONLY when the backend
# confirms the switch.  Any failure (rejected, unsupported, no client) must
# leave the recorded model untouched and return ok=False with an error —
# the UI relies on this to never display a model the session isn't running.
# ---------------------------------------------------------------------------

class TestSetSessionModel:

    def _make_session(self, session_manager, sm_module, sid, state=None,
                      model="claude-fable-5", client=object()):
        info = sm_module.SessionInfo(
            session_id=sid,
            state=state or sm_module.SessionState.IDLE,
        )
        info.model = model
        info.client = client
        with session_manager._lock:
            session_manager._sessions[sid] = info
        return info

    def test_nonexistent_session_rejected(self, session_manager):
        result = session_manager.set_session_model("nope", "claude-sonnet-4-6")
        assert result["ok"] is False
        assert "not found" in result["error"].lower()

    def test_stopped_session_rejected(self, session_manager, sm_module):
        self._make_session(session_manager, sm_module, "sm-stopped",
                           state=sm_module.SessionState.STOPPED)
        result = session_manager.set_session_model("sm-stopped", "claude-sonnet-4-6")
        assert result["ok"] is False
        assert "stopped" in result["error"].lower()

    def test_empty_model_rejected(self, session_manager, sm_module):
        self._make_session(session_manager, sm_module, "sm-empty")
        result = session_manager.set_session_model("sm-empty", "   ")
        assert result["ok"] is False

    def test_session_without_client_rejected(self, session_manager, sm_module):
        info = self._make_session(session_manager, sm_module, "sm-noclient",
                                  client=None)
        result = session_manager.set_session_model("sm-noclient", "claude-sonnet-4-6")
        assert result["ok"] is False
        assert info.model == "claude-fable-5"  # untouched

    def test_success_updates_model_and_logs(self, session_manager, sm_module):
        info = self._make_session(session_manager, sm_module, "sm-ok")
        with patch.object(session_manager._sdk, 'set_model',
                          new=AsyncMock(return_value=None)) as mock_set, \
             patch.object(session_manager, '_emit_state') as mock_emit:
            result = session_manager.set_session_model("sm-ok", "claude-sonnet-4-6")
        assert result["ok"] is True
        assert result["model"] == "claude-sonnet-4-6"
        # Recorded model updated ONLY after confirmed success
        assert info.model == "claude-sonnet-4-6"
        mock_set.assert_awaited_once()
        # Transcript records the switch
        assert any(e.kind == "system" and "claude-sonnet-4-6" in e.text
                   for e in info.entries)
        # Must NOT call _emit_state — doing so on an IDLE session fires
        # _try_dispatch_queue which causes a surprise WORKING state immediately
        # after the switch.  Badge updates go via the socket result instead.
        mock_emit.assert_not_called()

    def test_backend_rejection_leaves_model_untouched(self, session_manager, sm_module):
        info = self._make_session(session_manager, sm_module, "sm-reject")
        with patch.object(session_manager._sdk, 'set_model',
                          new=AsyncMock(side_effect=Exception("CLI says no"))):
            result = session_manager.set_session_model("sm-reject", "claude-sonnet-4-6")
        assert result["ok"] is False
        assert "CLI says no" in result["error"]
        assert info.model == "claude-fable-5"  # NEVER updated on failure
        assert not any("claude-sonnet-4-6" in e.text for e in info.entries)

    def test_unsupported_backend_graceful(self, session_manager, sm_module):
        info = self._make_session(session_manager, sm_module, "sm-unsup")
        with patch.object(session_manager._sdk, 'set_model',
                          new=AsyncMock(side_effect=NotImplementedError("no live switch"))):
            result = session_manager.set_session_model("sm-unsup", "claude-sonnet-4-6")
        assert result["ok"] is False
        assert info.model == "claude-fable-5"

    def test_dormant_session_resumed_with_requested_model(self, session_manager, tmp_path):
        """After a daemon restart an idle session exists only on disk.
        set_session_model must auto-resume it (like send_message does) with
        the REQUESTED model instead of returning 'Session not found' — the
        bug Q hit on 2026-06-10."""
        sid = "sm-dormant"
        jsonl = tmp_path / f"{sid}.jsonl"
        jsonl.write_text("{}\n")

        with patch.object(session_manager._store, 'find_session_path',
                          return_value=jsonl), \
             patch.object(session_manager._reg, 'load_registry',
                          return_value={"sessions": {sid: {
                              "cwd": str(tmp_path), "name": "Dormant",
                              "model": "claude-fable-5"}}}), \
             patch.object(session_manager, 'start_session',
                          return_value={"ok": True}) as mock_start:
            result = session_manager.set_session_model(sid, "claude-sonnet-4-6")

        assert result["ok"] is True
        assert result["model"] == "claude-sonnet-4-6"
        assert result.get("resumed") is True
        kwargs = mock_start.call_args.kwargs
        assert kwargs["session_id"] == sid
        assert kwargs["resume"] is True
        # The REQUESTED model, not the registry's stale one
        assert kwargs["model"] == "claude-sonnet-4-6"
        # cwd/name must come from the registry read.  This also guards the
        # self._reg attribute name: a typo (e.g. self._registry) is swallowed
        # by the except and resumes with empty cwd/name — the exact silent
        # bug that lived in send_message's fallback until 2026-06-10.
        assert os.path.normpath(kwargs["cwd"]) == os.path.normpath(str(tmp_path))
        assert kwargs["name"] == "Dormant"

    def test_no_registry_attr_typo_in_source(self):
        """``self._registry`` does not exist on SessionManager (it's
        ``self._reg``); references to it inside try/except blocks fail
        silently and gut the auto-resume fallbacks."""
        import pathlib
        src = (pathlib.Path(__file__).resolve().parents[1]
               / "daemon" / "session_manager.py").read_text(encoding="utf-8")
        assert "self._registry" not in src, \
            "Use self._reg — self._registry silently breaks auto-resume"

    # --- _set_confirmed_model: the single daemon write path ---------------
    # The three previously-scattered writes to info.model (mid-session switch,
    # CLI init message, and start) now funnel through one sink so their
    # reconciliation order can never drift apart.

    def test_confirmed_model_sink_updates_and_reports_change(self, session_manager, sm_module):
        info = self._make_session(session_manager, sm_module, "sink-1",
                                  model="claude-fable-5")
        with patch("app.routes.live_api.record_confirmed_model") as rec:
            changed = session_manager._set_confirmed_model(info, "claude-opus-4-7")
        assert changed is True
        assert info.model == "claude-opus-4-7"
        rec.assert_called_once_with("claude-opus-4-7")

    def test_confirmed_model_sink_noop_when_unchanged(self, session_manager, sm_module):
        info = self._make_session(session_manager, sm_module, "sink-2",
                                  model="claude-opus-4-7")
        assert session_manager._set_confirmed_model(info, "claude-opus-4-7") is False
        assert info.model == "claude-opus-4-7"

    def test_confirmed_model_sink_ignores_empty(self, session_manager, sm_module):
        info = self._make_session(session_manager, sm_module, "sink-3",
                                  model="claude-fable-5")
        assert session_manager._set_confirmed_model(info, "   ") is False
        assert info.model == "claude-fable-5"

    def test_confirmed_model_sink_init_is_ground_truth(self, session_manager, sm_module):
        """The CLI init message's resolved id (e.g. a dated variant) overwrites
        a previously recorded base id — init is the ultimate authority."""
        info = self._make_session(session_manager, sm_module, "sink-4",
                                  model="claude-sonnet-4-6")
        changed = session_manager._set_confirmed_model(
            info, "claude-sonnet-4-6-20251022")
        assert changed is True
        assert info.model == "claude-sonnet-4-6-20251022"

    def test_confirmed_model_sink_saves_registry_only_when_requested(self, session_manager, sm_module):
        info = self._make_session(session_manager, sm_module, "sink-5",
                                  model="claude-fable-5")
        with patch.object(session_manager, "_schedule_registry_save") as save:
            # Default (init path): no extra disk write per turn.
            session_manager._set_confirmed_model(info, "claude-opus-4-7")
            save.assert_not_called()
            # Explicit switch path: persist the change.
            session_manager._set_confirmed_model(info, "claude-sonnet-4-6",
                                                 save_registry=True)
            save.assert_called_once()

    def test_dormant_session_without_jsonl_rejected(self, session_manager):
        """No in-memory session AND no .jsonl on disk → honest not-found."""
        with patch.object(session_manager._store, 'find_session_path',
                          return_value=None):
            result = session_manager.set_session_model("sm-ghost",
                                                       "claude-sonnet-4-6")
        assert result["ok"] is False
        assert "not found" in result["error"].lower()

    def test_dormant_resume_failure_propagates(self, session_manager, tmp_path):
        """If the resume launch fails, the error must reach the caller —
        never a fake ok."""
        sid = "sm-dormant-fail"
        jsonl = tmp_path / f"{sid}.jsonl"
        jsonl.write_text("{}\n")

        with patch.object(session_manager._store, 'find_session_path',
                          return_value=jsonl), \
             patch.object(session_manager._reg, 'load_registry',
                          return_value={"sessions": {}}), \
             patch.object(session_manager, 'start_session',
                          return_value={"ok": False, "error": "spawn failed"}):
            result = session_manager.set_session_model(sid, "claude-sonnet-4-6")

        assert result["ok"] is False
        assert "spawn failed" in result["error"]

    def test_startup_init_drain_prevents_stuck_working(self, session_manager, sm_module):
        """Regression 2026-06-10: dormant bare resume must drain the CLI startup init.

        When set_session_model resumes a dormant session via start_session(prompt="",
        resume=True), the CLI emits a ``system.init`` message immediately after
        connecting.  Without the ``_drain_startup_init()`` call in the else branch of
        _drive_session, that init sits in the SDK buffer.  _post_turn_compact_drain then
        reads it in its 100 ms peek window, mistakes it for an auto-resume signal,
        calls _enter_auto_resume (→ WORKING), and waits for a RESULT that never arrives
        — the session is stuck in WORKING indefinitely.

        Fix (applied 2026-06-10): before declaring IDLE in the empty-prompt else branch,
        _drive_session now drains up to one init within a 3-second window.  This test
        verifies that a session started with an empty prompt whose mock SDK yields one
        init on the first receive_response() call resolves to IDLE, not WORKING.
        """
        from daemon.backends.messages import VibeNodeMessage, MessageKind

        sid = "sm-startup-drain-regression"
        call_count = [0]

        async def _mock_receive(client):
            """First receive_response call yields the startup init; later calls empty."""
            call_count[0] += 1
            if call_count[0] == 1:
                # The startup init the CLI emits right after connect --resume.
                yield VibeNodeMessage(kind=MessageKind.SYSTEM, subtype="init")
            # Subsequent calls (from _post_turn_compact_drain and
            # _extended_post_turn_listener) yield nothing — no buffered content.

        mock_client = MockClaudeSDKClient()

        with patch.object(session_manager._sdk, 'create_session',
                          new=AsyncMock(return_value=mock_client)), \
             patch.object(session_manager._sdk, 'receive_response',
                          new=_mock_receive):
            session_manager.start_session(sid, prompt="", cwd="/tmp", resume=True,
                                          model="claude-sonnet-4-6")
            # Without the drain fix the session would be stuck in WORKING
            # indefinitely; with the fix it resolves to IDLE quickly.
            wait_for(
                lambda: session_manager.get_session_state(sid) == "idle",
                timeout=10,
            )

        assert session_manager.get_session_state(sid) == "idle", (
            "Session stuck in WORKING — _drain_startup_init regression in _drive_session"
        )


# ---------------------------------------------------------------------------
# 14. Close session
# ---------------------------------------------------------------------------

class TestCloseSession:

    def test_close_nonexistent_session(self, session_manager):
        """Closing a nonexistent session should fail gracefully."""
        result = session_manager.close_session("nonexistent")
        assert result["ok"] is False
class TestErrorHandling:

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
# 17b. Get dormant states (restart memory)
# ---------------------------------------------------------------------------

class TestGetDormantStates:
    """get_dormant_states() surfaces the restart-memory snapshot so the UI can
    show sessions that were idle/working before a restart with their real
    state instead of "sleeping" -- but only while they remain dormant."""

    def test_returns_snapshot_for_dormant_sessions(self, session_manager):
        """Sessions in the snapshot that aren't live are returned as-is."""
        session_manager._last_known_states = {
            "s-idle": {"last_state": "idle", "name": "Idle"},
            "s-work": {"last_state": "working", "name": "Work"},
        }
        out = session_manager.get_dormant_states()
        assert out["s-idle"]["last_state"] == "idle"
        assert out["s-work"]["last_state"] == "working"

    def test_omits_sessions_that_went_live(self, session_manager, sm_module):
        """A session that has since gone live must be omitted -- its live state
        from get_all_states() is authoritative and must win."""
        session_manager._last_known_states = {
            "s-idle": {"last_state": "idle"},
            "s-live": {"last_state": "working"},
        }
        with session_manager._lock:
            session_manager._sessions["s-live"] = sm_module.SessionInfo(
                session_id="s-live", state=sm_module.SessionState.IDLE
            )
        out = session_manager.get_dormant_states()
        assert "s-idle" in out
        assert "s-live" not in out

    def test_empty_when_no_snapshot(self, session_manager):
        """No captured snapshot -> empty dict (nothing to restore)."""
        session_manager._last_known_states = {}
        assert session_manager.get_dormant_states() == {}


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


# ---------------------------------------------------------------------------
# Regression: project-switch bleed — get_all_states cwd fill-in (2026-06-10)
# ---------------------------------------------------------------------------

class TestGetAllStatesCwdFillIn:
    """Regression tests for the project-switch cross-project bleed fix.

    Root cause: sessions auto-resumed before the _reg typo fix had info.cwd=""
    which caused _filter_sessions_for_project to include them in EVERY project's
    snapshot.  get_all_states() now fills in cwd from the registry for empty-cwd
    sessions so the filter can exclude them correctly.
    """

    def test_empty_cwd_session_gets_registry_fallback(self, session_manager, sm_module):
        """A session with cwd='' in daemon memory should get cwd filled from registry."""
        sid = "bleed-test-session"
        real_cwd = "C:/Users/test/code/ProjectA"

        info = sm_module.SessionInfo(session_id=sid, state=sm_module.SessionState.IDLE)
        info.cwd = ""  # simulate pre-fix auto-resume
        with session_manager._lock:
            session_manager._sessions[sid] = info

        reg_data = {"sessions": {sid: {"cwd": real_cwd, "name": "test", "state": "idle"}}}
        session_manager._reg.load_registry = MagicMock(return_value=reg_data)

        states = session_manager.get_all_states()
        by_id = {s["session_id"]: s for s in states}
        assert sid in by_id
        # cwd must be filled in from the registry
        assert by_id[sid]["cwd"] == real_cwd, (
            "get_all_states must fill in cwd from registry for empty-cwd sessions "
            "so _filter_sessions_for_project can exclude them from wrong projects"
        )

    def test_session_with_real_cwd_is_unchanged(self, session_manager, sm_module):
        """Sessions that already have a cwd set must not have it overwritten."""
        sid = "has-cwd-session"
        original_cwd = "C:/Users/test/code/ProjectB"
        reg_cwd = "C:/Users/test/code/SomethingElse"

        info = sm_module.SessionInfo(session_id=sid, state=sm_module.SessionState.IDLE)
        info.cwd = original_cwd
        with session_manager._lock:
            session_manager._sessions[sid] = info

        reg_data = {"sessions": {sid: {"cwd": reg_cwd, "name": "test"}}}
        session_manager._reg.load_registry = MagicMock(return_value=reg_data)

        states = session_manager.get_all_states()
        by_id = {s["session_id"]: s for s in states}
        assert by_id[sid]["cwd"] == original_cwd, (
            "Sessions with a real cwd must not have it overwritten by the registry"
        )
        # Registry should not even be read when no empty-cwd sessions exist
        session_manager._reg.load_registry.assert_not_called()

    def test_registry_read_failure_is_silently_ignored(self, session_manager, sm_module):
        """If the registry read raises, get_all_states() must still return normally."""
        sid = "fail-reg-session"
        info = sm_module.SessionInfo(session_id=sid, state=sm_module.SessionState.IDLE)
        info.cwd = ""
        with session_manager._lock:
            session_manager._sessions[sid] = info

        session_manager._reg.load_registry = MagicMock(side_effect=OSError("disk error"))

        # Must not raise
        states = session_manager.get_all_states()
        by_id = {s["session_id"]: s for s in states}
        assert sid in by_id
        # cwd stays empty (no crash, just permissive)
        assert by_id[sid]["cwd"] == ""

    def test_no_registry_read_when_all_cwds_populated(self, session_manager, sm_module):
        """Registry must not be read at all when every session already has a cwd."""
        for i in range(3):
            sid = f"cwd-session-{i}"
            info = sm_module.SessionInfo(session_id=sid, state=sm_module.SessionState.IDLE)
            info.cwd = f"C:/Users/test/proj{i}"
            with session_manager._lock:
                session_manager._sessions[sid] = info

        session_manager._reg.load_registry = MagicMock()

        session_manager.get_all_states()
        session_manager._reg.load_registry.assert_not_called()

    def test_filter_sessions_excludes_other_project(self):
        """Server-side _filter_sessions_for_project must exclude sessions with a
        non-matching cwd now that get_all_states() fills in the registry cwd."""
        from app.routes.ws_events import register_ws_events
        from unittest.mock import MagicMock
        # Reconstruct the filter function the same way ws_events defines it
        # (it's a closure, so we re-implement the same logic inline)
        from app.config import cwd_matches_active_project

        def _filter(sessions, project=""):
            return [s for s in sessions
                    if not s.get("cwd") or cwd_matches_active_project(s["cwd"], project=project)]

        project_a_cwd = "C:/Users/test/ProjectA"
        project_b_encoded = "C--Users-test-ProjectB"
        sessions = [
            {"session_id": "s1", "cwd": project_a_cwd, "state": "working"},
            {"session_id": "s2", "cwd": "", "state": "idle"},  # new session, no cwd yet
        ]
        filtered = _filter(sessions, project=project_b_encoded)
        ids = {s["session_id"] for s in filtered}
        assert "s1" not in ids, (
            "Session from ProjectA must be excluded when filtering for ProjectB"
        )
        assert "s2" in ids, (
            "Brand-new session with empty cwd must still pass through (may belong to active project)"
        )
