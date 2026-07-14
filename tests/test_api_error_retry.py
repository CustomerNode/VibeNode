"""
Tests for API-error auto-retry with exponential backoff in SessionManager.

Background: when a turn ends with a ResultMessage(is_error=True) caused by a
transient upstream failure (API overload / 429 / 529 / 5xx / network), the
session used to log "Session ended with error" and sit idle forever.  These
tests cover the auto-retry that resends the last user message on an exponential
backoff, the countdown state surfaced to the UI, and the Cancel / Retry-now
controls.

Mirrors the mock/fixture pattern of tests/test_self_healing.py.  Timing-
sensitive behavior is exercised with a tiny backoff base (monkeypatched) and a
recorder in place of the real send path; pure functions are tested directly.
"""

import asyncio
import inspect
import time
import pytest
from unittest.mock import AsyncMock, MagicMock, patch


# ---------------------------------------------------------------------------
# Mock SDK types (mirrors test_self_healing.py; MockResultMessage extended with
# subtype + result so we can exercise the error classifier through the backend)
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
                 duration_ms=1000, is_error=False, num_turns=1, usage=None,
                 subtype="", result=""):
        self.session_id = session_id
        self.total_cost_usd = total_cost_usd
        self.duration_ms = duration_ms
        self.is_error = is_error
        self.num_turns = num_turns
        self.usage = usage or {}
        self.subtype = subtype
        self.result = result


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


class _RaisingClient:
    """A connected SDK client whose receive_response() raises a configured
    exception on the first message — simulates the API/connection failing
    mid-turn (a 500 result, a dropped connection, etc.)."""

    def __init__(self, err_str):
        self._err = err_str
        self._connected = True

    async def connect(self, prompt=None):
        self._connected = True

    async def query(self, prompt, session_id="default"):
        pass

    async def receive_response(self):
        raise RuntimeError(self._err)
        yield  # unreachable — makes this an async generator function

    async def interrupt(self):
        pass

    async def disconnect(self):
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
    manager._queue_path = tmp_path / "test_queues.json"
    manager._queues = {}
    manager.start(mock_socketio)
    yield manager
    manager.stop()


def wait_for(condition, timeout=5.0, interval=0.02):
    """Poll until condition() is truthy or timeout expires."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        result = condition()
        if result:
            return result
        time.sleep(interval)
    raise TimeoutError(f"Condition not met within {timeout}s")


def _make_idle_session(sm_module, manager, sid, user_text="do the thing"):
    """Register an IDLE SessionInfo with one user entry and return it."""
    info = sm_module.SessionInfo(session_id=sid, state=sm_module.SessionState.IDLE)
    info.cwd = "/tmp"
    info.entries.append(sm_module.LogEntry(kind="user", text=user_text))
    with manager._lock:
        manager._sessions[sid] = info
    return info


# ===========================================================================
# Pure-function unit tests — classifier, backoff schedule, reason text
# ===========================================================================

class TestClassifier:
    @pytest.mark.parametrize("text", [
        "API Error: 529 Overloaded",
        "overloaded_error",
        "429 Too Many Requests",
        "rate limit exceeded",
        "Internal Server Error (500)",
        "502 Bad Gateway",
        "503 service unavailable",
        "connection reset by peer",
        "request timed out",
        "network error, please try again",
    ])
    def test_transient_errors_classified_transient(self, sm_module, text):
        assert sm_module.SessionManager._classify_result_error("error_during_execution", text) == "transient"

    def test_max_turns_is_permanent(self, sm_module):
        assert sm_module.SessionManager._classify_result_error("error_max_turns", "") == "permanent"
        assert sm_module.SessionManager._classify_result_error("error_max_turns", "anything") == "permanent"

    @pytest.mark.parametrize("text", [
        "invalid request: messages must be non-empty",
        "401 unauthorized",
        "authentication failed",
        "403 forbidden",
    ])
    def test_known_permanent_errors(self, sm_module, text):
        assert sm_module.SessionManager._classify_result_error("error_during_execution", text) == "permanent"

    def test_unknown_error_defaults_transient(self, sm_module):
        # An unrecognised is_error result is retried (bounded by the cap).
        assert sm_module.SessionManager._classify_result_error("", "some weird message") == "transient"
        assert sm_module.SessionManager._classify_result_error("", "") == "transient"


class TestBackoffSchedule:
    def test_exponential_schedule(self, sm_module):
        SM = sm_module.SessionManager
        # Defaults: base=10, factor=2, cap=1800 ->
        # 10, 20, 40, 80, 160, 320, 640, 1280, min(2560,1800)=1800, 1800
        expected = [10, 20, 40, 80, 160, 320, 640, 1280, 1800, 1800]
        got = [SM._api_retry_delay(n) for n in range(10)]
        assert got == expected

    def test_negative_attempt_clamped(self, sm_module):
        SM = sm_module.SessionManager
        assert SM._api_retry_delay(-5) == SM._API_RETRY_BASE

    def test_cap_respected(self, sm_module):
        # Very large attempt index never exceeds the cap.
        assert sm_module.SessionManager._api_retry_delay(100) == sm_module.SessionManager._API_RETRY_CAP

    def test_window_covers_overnight(self, sm_module):
        # The total retry window (sum of all attempt delays) must comfortably
        # exceed a 2-hour outage so an overnight task can recover.
        SM = sm_module.SessionManager
        total = sum(SM._api_retry_delay(n) for n in range(SM._API_RETRY_MAX))
        assert total >= 2 * 3600, f"retry window only {total}s (< 2h)"

    def test_jitter_within_bounds(self, sm_module):
        SM = sm_module.SessionManager
        base = 100.0
        for _ in range(300):
            v = SM._apply_jitter(base)
            assert 80.0 <= v <= 120.0, f"jitter {v} outside +/-20% of {base}"

    def test_jitter_zero_returns_base(self, sm_module, monkeypatch):
        monkeypatch.setattr(sm_module.SessionManager, '_API_RETRY_JITTER', 0)
        assert sm_module.SessionManager._apply_jitter(123.0) == 123.0

    @pytest.mark.parametrize("secs,expected", [
        (5, "5s"), (59, "59s"), (60, "1m"), (90, "1m 30s"),
        (600, "10m"), (1800, "30m"), (3600, "1h"), (3725, "1h 2m"),
    ])
    def test_fmt_duration(self, sm_module, secs, expected):
        assert sm_module.SessionManager._fmt_duration(secs) == expected


class TestReasonText:
    @pytest.mark.parametrize("text,expected", [
        ("529 overloaded", "API overloaded"),
        ("429 rate limit", "Rate limited"),
        ("500 internal server error", "Server error"),
        ("connection timed out", "Network error"),
        ("something else entirely", "Temporary error"),
    ])
    def test_reason_text(self, sm_module, text, expected):
        assert sm_module.SessionManager._retry_reason_text(text) == expected


# ===========================================================================
# Detection channel (b): non-transport stream EXCEPTIONS (5xx / overload / …)
# These are the API errors raised mid-stream that previously hit
# "reconnect, no retry" and dead-ended.
# ===========================================================================

class TestExceptionChannelDetection:
    @pytest.mark.parametrize("err_str", [
        "Error code: 500 - {'type': 'error', 'error': {'type': 'api_error'}}",
        "anthropic.InternalServerError: 500",
        "503 Service Unavailable",
        "502 Bad Gateway",
        "API Error: 529 Overloaded",
        "overloaded_error",
        "429 rate limit exceeded",
        "Connection reset by peer",
        "read operation timed out",
        # Unknown error strings default to transient → still retried.
        "some unexpected failure nobody anticipated",
    ])
    def test_transient_exceptions_flagged(self, session_manager, sm_module, err_str):
        info = _make_idle_session(sm_module, session_manager, "exc-" + str(abs(hash(err_str)) % 9999))
        flagged = session_manager._flag_api_retry_if_transient(info, err_str)
        assert flagged is True
        assert info._api_retry_needed is True
        assert info.retry_reason  # a human reason was set

    @pytest.mark.parametrize("err_str", [
        "Error code: 400 - invalid request: messages must be non-empty",
        "401 Unauthorized",
        "403 Forbidden",
    ])
    def test_permanent_exceptions_not_flagged(self, session_manager, sm_module, err_str):
        info = _make_idle_session(sm_module, session_manager, "excp-" + str(abs(hash(err_str)) % 9999))
        flagged = session_manager._flag_api_retry_if_transient(info, err_str)
        assert flagged is False
        assert info._api_retry_needed is False

    def test_not_flagged_without_user_message(self, session_manager, sm_module):
        # Empty transcript — nothing to continue, so never arm.
        info = sm_module.SessionInfo(session_id="exc-empty", state=sm_module.SessionState.IDLE)
        with session_manager._lock:
            session_manager._sessions["exc-empty"] = info
        assert session_manager._flag_api_retry_if_transient(info, "503 Service Unavailable") is False

    def test_not_flagged_when_budget_exhausted(self, session_manager, sm_module):
        info = _make_idle_session(sm_module, session_manager, "exc-exhausted")
        info._api_retry_count = sm_module.SessionManager._API_RETRY_MAX
        assert session_manager._flag_api_retry_if_transient(info, "500 internal server error") is False

    def test_drive_loops_detect_exception_channel(self, sm_module):
        # Both stream drivers must classify the non-transport exception and
        # route transient ones into the backoff (not the old no-retry path).
        for meth in (sm_module.SessionManager._drive_session,
                     sm_module.SessionManager._send_query):
            src = inspect.getsource(meth)
            assert "_flag_api_retry_if_transient" in src, \
                "non-transport exception branch does not flag API retry"


# ===========================================================================
# Detection channel (c): connectivity loss / dead transport.
# An internet outage that kills the CLI surfaces as a transport error; the
# short 3-try stream-heal escalates into the SAME long backoff, reconnecting
# each attempt until the network returns.
# ===========================================================================

class TestConnectivityEscalation:
    @pytest.mark.parametrize("err_str", [
        "Could not resolve host: api.anthropic.com",
        "getaddrinfo failed: Name or service not known",
        "Connection refused",
        "Connection reset by peer",
        "[Errno 101] Network is unreachable",
        "No route to host",
        "SSL handshake failed",
        "Temporary failure in name resolution",
        "HTTPSConnectionPool: Max retries exceeded",
    ])
    def test_connectivity_strings_classified_transient(self, sm_module, err_str):
        assert sm_module.SessionManager._classify_result_error("", err_str) == "transient"

    def test_escalate_flags_reconnect_retry(self, session_manager, sm_module):
        info = _make_idle_session(sm_module, session_manager, "esc-1")
        info._ever_got_result = True   # session is resumable (produced a RESULT)
        flagged = session_manager._escalate_heal_to_backoff(info, "Connection lost")
        assert flagged is True
        assert info._api_retry_needed is True
        assert info._retry_needs_reconnect is True
        assert info.retry_reason == "Connection lost"

    def test_escalate_not_before_first_result(self, session_manager, sm_module):
        # A crash before the first RESULT can't --resume (no real UUID yet) —
        # escalation must bail so it doesn't spin a doomed reconnect.
        info = _make_idle_session(sm_module, session_manager, "esc-firstturn")
        info._ever_got_result = False
        assert session_manager._escalate_heal_to_backoff(info, "Connection lost") is False

    def test_escalate_not_without_user_message(self, session_manager, sm_module):
        info = sm_module.SessionInfo(session_id="esc-empty", state=sm_module.SessionState.IDLE)
        with session_manager._lock:
            session_manager._sessions["esc-empty"] = info
        assert session_manager._escalate_heal_to_backoff(info, "Connection lost") is False

    def test_escalate_not_when_budget_exhausted(self, session_manager, sm_module):
        info = _make_idle_session(sm_module, session_manager, "esc-exhausted")
        info._api_retry_count = sm_module.SessionManager._API_RETRY_MAX
        assert session_manager._escalate_heal_to_backoff(info, "Connection lost") is False

    def test_clear_resets_reconnect_flag(self, session_manager, sm_module):
        info = _make_idle_session(sm_module, session_manager, "esc-clear")
        info._retry_needs_reconnect = True
        session_manager._clear_api_retry(info, reset_count=True)
        assert info._retry_needs_reconnect is False

    def test_give_up_branches_escalate(self, sm_module):
        # Both drive loops must escalate a give-up into the long backoff
        # rather than only printing "please resend".
        for meth in (sm_module.SessionManager._drive_session,
                     sm_module.SessionManager._send_query):
            src = inspect.getsource(meth)
            assert "_escalate_heal_to_backoff" in src

    def test_timer_reconnects_then_continues(self, session_manager, sm_module, monkeypatch):
        # When the retry needs a reconnect, the timer reconnects first, then
        # resumes with the continue prompt.
        monkeypatch.setattr(sm_module.SessionManager, '_API_RETRY_BASE', 0.05)
        monkeypatch.setattr(sm_module.SessionManager, '_API_RETRY_JITTER', 0)
        info = _make_idle_session(sm_module, session_manager, "esc-timer")
        info._retry_needs_reconnect = True

        reconnect_calls = []
        async def _fake_reconnect(sid, inf):
            reconnect_calls.append(sid)
            return True
        monkeypatch.setattr(session_manager, '_reconnect_client', _fake_reconnect)

        recorder = []
        monkeypatch.setattr(session_manager, 'send_message',
                            lambda sid, text, **kw: recorder.append((sid, text, kw)) or {"ok": True})

        async def _arm():
            session_manager._arm_api_retry("esc-timer", info)
        asyncio.run_coroutine_threadsafe(_arm(), session_manager._loop).result(timeout=2)

        wait_for(lambda: len(recorder) >= 1, timeout=3)
        assert reconnect_calls, "reconnect was not attempted before firing"
        sid, text, kw = recorder[0]
        assert text  # a retry was resent (original, since no output produced yet)
        assert info._retry_needs_reconnect is False  # cleared after success

    def test_timer_rearms_when_reconnect_fails(self, session_manager, sm_module, monkeypatch):
        # Reconnect fails (outage ongoing) → consume the attempt and re-arm the
        # next backoff instead of giving up.
        monkeypatch.setattr(sm_module.SessionManager, '_API_RETRY_BASE', 0.05)
        monkeypatch.setattr(sm_module.SessionManager, '_API_RETRY_JITTER', 0)
        info = _make_idle_session(sm_module, session_manager, "esc-fail")
        info._retry_needs_reconnect = True

        async def _fake_reconnect(sid, inf):
            return False
        monkeypatch.setattr(session_manager, '_reconnect_client', _fake_reconnect)

        recorder = []
        monkeypatch.setattr(session_manager, 'send_message',
                            lambda sid, text, **kw: recorder.append((sid, text, kw)) or {"ok": True})

        async def _arm():
            session_manager._arm_api_retry("esc-fail", info)
        asyncio.run_coroutine_threadsafe(_arm(), session_manager._loop).result(timeout=2)

        # After the first failed reconnect, the attempt count advances and a new
        # countdown is armed (it keeps trying), and no resend happened yet.
        wait_for(lambda: info._api_retry_count >= 1, timeout=3)
        assert recorder == [], "should not resend while still disconnected"
        assert info._retry_needs_reconnect is True  # still needs reconnect


# ===========================================================================
# END-TO-END: drive a real turn (_send_query) through a failure and confirm
# the exponential backoff actually arms + fires.  Covers the two cases the
# feature exists for: 500-class API errors and network connection loss.
# ===========================================================================

class TestEndToEndFailureArmsBackoff:
    @pytest.mark.parametrize("err_str,kind", [
        # 500-class API errors
        ("Error code: 500 - {'type':'error','error':{'type':'api_error'}}", "500"),
        ("503 Service Unavailable", "500"),
        ("anthropic.InternalServerError: Internal server error", "500"),
        ("API Error: 529 Overloaded", "500"),
        # Network connection interruption (raised as a NON-transport exception
        # — a "...closed" string would instead be a transport error handled by
        # the stream-heal→escalate path, covered in TestConnectivityEscalation).
        ("Could not resolve host: api.anthropic.com", "network"),
        ("[Errno 111] Connection refused", "network"),
        ("HTTPSConnectionPool(host='api.anthropic.com'): Max retries exceeded", "network"),
        ("[Errno 101] Network is unreachable", "network"),
    ])
    def test_turn_failure_arms_and_fires_backoff(
        self, session_manager, sm_module, monkeypatch, err_str, kind
    ):
        # Fast, deterministic backoff so the timer fires within the test.
        monkeypatch.setattr(sm_module.SessionManager, '_API_RETRY_BASE', 0.05)
        monkeypatch.setattr(sm_module.SessionManager, '_API_RETRY_JITTER', 0)

        sid = "e2e-" + kind + "-" + str(abs(hash(err_str)) % 99999)
        info = _make_idle_session(sm_module, session_manager, sid, user_text="do the long task")
        # A connected client whose stream raises the failure mid-turn.
        info.client = _RaisingClient(err_str)
        info.state = sm_module.SessionState.WORKING  # as send_message would set it

        # Isolate from the real reconnect machinery (jsonl repair, CLI spawn).
        async def _fake_reconnect(s, i):
            return True
        monkeypatch.setattr(session_manager, '_reconnect_client', _fake_reconnect)

        # Capture the auto-retry resend instead of driving a second turn.
        recorder = []
        monkeypatch.setattr(session_manager, 'send_message',
                            lambda s, t, **kw: recorder.append((s, t, kw)) or {"ok": True})

        # Drive the turn end-to-end on the manager loop (via _tracked_coro so the
        # finally's superseded-guard sees us as the owning task).
        async def _drive():
            await session_manager._tracked_coro(
                info, session_manager._send_query(sid, "do the long task"))
        asyncio.run_coroutine_threadsafe(_drive(), session_manager._loop).result(timeout=8)

        # The turn failed → a transient error was detected → the exponential
        # backoff was armed → the timer fired → it re-sent under the _auto_retry
        # flag.  No assistant output was produced before the error, so the retry
        # RE-SENDS THE ORIGINAL request (not the "continue" prompt).
        wait_for(lambda: len(recorder) >= 1, timeout=4)
        s, text, kw = recorder[0]
        assert s == sid
        assert text == "do the long task"  # original re-sent (no output before error)
        assert kw.get("_auto_retry") is True
        assert info._api_retry_count == 1
        # A connection error raised mid-stream is a NON-transport exception, so
        # the except branch already reconnected — no second reconnect needed.
        assert info._retry_needs_reconnect is False

    def test_permanent_turn_failure_does_not_arm(self, session_manager, sm_module, monkeypatch):
        # A genuinely permanent error (400 invalid request) must NOT auto-retry.
        monkeypatch.setattr(sm_module.SessionManager, '_API_RETRY_BASE', 0.05)
        monkeypatch.setattr(sm_module.SessionManager, '_API_RETRY_JITTER', 0)
        sid = "e2e-permanent"
        info = _make_idle_session(sm_module, session_manager, sid)
        info.client = _RaisingClient("Error code: 400 - invalid request: bad content")
        info.state = sm_module.SessionState.WORKING

        async def _fake_reconnect(s, i):
            return True
        monkeypatch.setattr(session_manager, '_reconnect_client', _fake_reconnect)
        recorder = []
        monkeypatch.setattr(session_manager, 'send_message',
                            lambda s, t, **kw: recorder.append((s, t, kw)) or {"ok": True})

        async def _drive():
            await session_manager._tracked_coro(
                info, session_manager._send_query(sid, "x"))
        asyncio.run_coroutine_threadsafe(_drive(), session_manager._loop).result(timeout=8)

        # Give any (erroneously) armed timer a moment — then confirm nothing fired.
        time.sleep(0.4)
        assert recorder == [], "permanent error must not auto-retry"
        assert info.retry_at == 0.0


# ===========================================================================
# Serialization — retry fields exposed to the UI
# ===========================================================================

class TestSerialization:
    def test_to_state_dict_includes_retry_fields(self, sm_module):
        info = sm_module.SessionInfo(session_id="s1", state=sm_module.SessionState.IDLE)
        d = info.to_state_dict()
        for key in ("retry_at", "retry_attempt", "retry_max", "retry_reason"):
            assert key in d, f"{key} missing from to_state_dict()"
        # Defaults (no retry pending)
        assert d["retry_at"] == 0.0
        assert d["retry_attempt"] == 0
        assert d["retry_max"] == 0
        assert d["retry_reason"] == ""

    def test_to_state_dict_reflects_armed_retry(self, sm_module):
        info = sm_module.SessionInfo(session_id="s2", state=sm_module.SessionState.IDLE)
        info.retry_at = 1234567890.0
        info.retry_attempt = 2
        info.retry_max = 5
        info.retry_reason = "API overloaded"
        d = info.to_state_dict()
        assert d["retry_at"] == 1234567890.0
        assert d["retry_attempt"] == 2
        assert d["retry_max"] == 5
        assert d["retry_reason"] == "API overloaded"


# ===========================================================================
# Clearing / counter semantics
# ===========================================================================

class TestClearAndCounter:
    def test_clear_resets_all_fields(self, session_manager, sm_module):
        info = _make_idle_session(sm_module, session_manager, "clr-1")
        info.retry_at = time.time() + 10
        info.retry_attempt = 3
        info.retry_max = 5
        info.retry_reason = "Rate limited"
        info._api_retry_needed = True
        info._api_retry_count = 3
        session_manager._clear_api_retry(info, reset_count=True)
        assert info.retry_at == 0.0
        assert info.retry_attempt == 0
        assert info.retry_max == 0
        assert info.retry_reason == ""
        assert info._api_retry_needed is False
        assert info._api_retry_count == 0

    def test_clear_can_preserve_count(self, session_manager, sm_module):
        info = _make_idle_session(sm_module, session_manager, "clr-2")
        info._api_retry_count = 2
        session_manager._clear_api_retry(info, reset_count=False)
        assert info._api_retry_count == 2

    def test_new_user_message_resets_count(self, session_manager, sm_module):
        info = _make_idle_session(sm_module, session_manager, "reset-1")
        info._api_retry_count = 3
        # Simulate a session that had cancelled/errored (error banner showing).
        info.error = "Auto-retry cancelled — use Retry or type a new message"
        # Stub out the turn dispatch so send_message runs its counter logic
        # without actually launching a coroutine.
        with patch.object(session_manager, '_send_query', MagicMock()), \
             patch.object(session_manager, '_tracked_coro', MagicMock()), \
             patch('asyncio.run_coroutine_threadsafe', MagicMock()):
            session_manager.send_message("reset-1", "a brand new message")
        # Taking over resets the budget AND clears the stale error banner, so a
        # fresh failure starts a brand-new backoff chain.
        assert info._api_retry_count == 0
        assert info.error == ""

    def test_auto_retry_send_preserves_count(self, session_manager, sm_module):
        info = _make_idle_session(sm_module, session_manager, "reset-2")
        info._api_retry_count = 3
        with patch.object(session_manager, '_send_query', MagicMock()), \
             patch.object(session_manager, '_tracked_coro', MagicMock()), \
             patch('asyncio.run_coroutine_threadsafe', MagicMock()):
            session_manager.send_message("reset-2", "retry text", _auto_retry=True)
        assert info._api_retry_count == 3


# ===========================================================================
# Cancel / Retry-now actions
# ===========================================================================

class TestCancelAndRetryNow:
    def test_cancel_auto_retry_clears_and_idles(self, session_manager, sm_module):
        info = _make_idle_session(sm_module, session_manager, "cancel-1")
        info.retry_at = time.time() + 30
        info.retry_attempt = 1
        info.retry_max = 5
        info._api_retry_count = 1
        result = session_manager.cancel_auto_retry("cancel-1")
        assert result["ok"] is True
        assert info.retry_at == 0.0
        assert info._api_retry_count == 0
        assert info.state == sm_module.SessionState.IDLE
        assert "cancel" in info.error.lower()

    def test_retry_now_during_countdown_fires_and_preserves_count(self, session_manager, sm_module):
        info = _make_idle_session(sm_module, session_manager, "rn-1")
        info.retry_at = time.time() + 30      # countdown active
        info.retry_attempt = 2
        info._api_retry_count = 2
        recorder = []
        with patch.object(session_manager, 'send_message',
                          side_effect=lambda sid, text, **kw: recorder.append((sid, text, kw)) or {"ok": True}):
            result = session_manager.retry_now("rn-1")
        assert result["ok"] is True
        assert recorder, "send_message was not called"
        sid, text, kw = recorder[0]
        assert kw.get("_auto_retry") is True
        assert text  # a retry was resent (original here — no assistant output in this session)
        assert info.retry_at == 0.0           # countdown cleared
        # Mid-countdown retry consumes the attempt — counter keeps accumulating.
        assert info._api_retry_count == 3

    def test_retry_now_from_error_resets_budget(self, session_manager, sm_module):
        info = _make_idle_session(sm_module, session_manager, "rn-2")
        info.retry_at = 0.0                   # no countdown — settled error
        info.error = "Auto-retry gave up after 5 attempts — use Retry to try again"
        info._api_retry_count = 5
        recorder = []
        with patch.object(session_manager, 'send_message',
                          side_effect=lambda sid, text, **kw: recorder.append((sid, text, kw)) or {"ok": True}):
            result = session_manager.retry_now("rn-2")
        assert result["ok"] is True
        assert recorder, "send_message was not called"
        # Manual retry from a settled error is a fresh start: budget reset to 0,
        # then this resend consumes one -> 1.
        assert info._api_retry_count == 1

    def test_retry_now_rejects_non_idle(self, session_manager, sm_module):
        info = _make_idle_session(sm_module, session_manager, "rn-3")
        info.state = sm_module.SessionState.WORKING
        result = session_manager.retry_now("rn-3")
        assert result["ok"] is False

    def test_actions_unknown_session(self, session_manager):
        assert session_manager.cancel_auto_retry("nope")["ok"] is False
        assert session_manager.retry_now("nope")["ok"] is False


# ===========================================================================
# End-to-end timer behavior — arm -> backoff -> fire resend
# ===========================================================================

class TestArmAndFire:
    def test_arm_then_timer_fires_continue(self, session_manager, sm_module, monkeypatch):
        # Tiny backoff + no jitter so the timer fires quickly and deterministically.
        monkeypatch.setattr(sm_module.SessionManager, '_API_RETRY_BASE', 0.05)
        monkeypatch.setattr(sm_module.SessionManager, '_API_RETRY_JITTER', 0)
        info = _make_idle_session(sm_module, session_manager, "arm-1", user_text="do the original thing")
        # The failed turn already ran a TOOL (side-effectful work), so the retry
        # must CONTINUE (not replay, which would duplicate it).
        info.entries.append(sm_module.LogEntry(kind="tool_use", text=""))

        recorder = []
        monkeypatch.setattr(session_manager, 'send_message',
                            lambda sid, text, **kw: recorder.append((sid, text, kw)) or {"ok": True})

        # Arm must run on the manager loop (it uses asyncio.create_task).
        async def _arm():
            session_manager._arm_api_retry("arm-1", info)
        fut = asyncio.run_coroutine_threadsafe(_arm(), session_manager._loop)
        fut.result(timeout=2)

        # Countdown is now visible to the UI.
        assert info.retry_at > 0
        assert info.retry_attempt == 1
        assert info.retry_max == sm_module.SessionManager._API_RETRY_MAX

        # Timer fires — and (because output existed) sends the CONTINUE prompt,
        # NOT a replay of the original instruction.
        wait_for(lambda: len(recorder) >= 1, timeout=3)
        sid, text, kw = recorder[0]
        assert sid == "arm-1"
        assert text == sm_module.SessionManager._API_RETRY_CONTINUE_PROMPT
        assert text != "do the original thing"
        assert kw.get("_auto_retry") is True
        assert info._api_retry_count == 1
        assert info.retry_at == 0.0  # cleared on fire

    def test_cancel_prevents_timer_fire(self, session_manager, sm_module, monkeypatch):
        monkeypatch.setattr(sm_module.SessionManager, '_API_RETRY_BASE', 0.3)
        monkeypatch.setattr(sm_module.SessionManager, '_API_RETRY_JITTER', 0)
        info = _make_idle_session(sm_module, session_manager, "arm-2")

        recorder = []
        monkeypatch.setattr(session_manager, 'send_message',
                            lambda sid, text, **kw: recorder.append((sid, text, kw)) or {"ok": True})

        async def _arm():
            session_manager._arm_api_retry("arm-2", info)
        asyncio.run_coroutine_threadsafe(_arm(), session_manager._loop).result(timeout=2)
        assert info.retry_at > 0

        # Cancel before the (0.3s) timer fires.
        session_manager.cancel_auto_retry("arm-2")
        time.sleep(0.6)
        assert recorder == [], "Cancelled retry should not have resent the message"
        assert info.retry_at == 0.0


# ===========================================================================
# Structural tests — verify the wiring is in place (no timing dependence)
# ===========================================================================

class TestStructuralWiring:
    def test_process_message_flags_transient_retry(self, sm_module):
        src = inspect.getsource(sm_module.SessionManager._process_message)
        assert "_api_retry_needed" in src
        assert "_classify_result_error" in src

    def test_drive_loops_arm_retry(self, sm_module):
        for meth in (sm_module.SessionManager._drive_session,
                     sm_module.SessionManager._send_query):
            src = inspect.getsource(meth)
            assert "_arm_api_retry" in src, "finally block does not arm API retry"
            # Must not double-fire when transport self-heal already owns a retry.
            assert "_stream_heal_needed" in src

    def test_arm_uses_backoff_and_schedules_task(self, sm_module):
        src = inspect.getsource(sm_module.SessionManager._arm_api_retry)
        assert "_api_retry_delay" in src
        assert "create_task" in src

    def test_send_message_has_auto_retry_param(self, sm_module):
        sig = inspect.signature(sm_module.SessionManager.send_message)
        assert "_auto_retry" in sig.parameters

    def test_constants_exist(self, sm_module):
        SM = sm_module.SessionManager
        assert isinstance(SM._API_RETRY_MAX, int) and SM._API_RETRY_MAX >= 1
        assert SM._API_RETRY_CAP >= SM._API_RETRY_BASE



class TestDrainSkipAndResendOriginal:
    """Regression for two bugs found via live-CLI fault testing:
    (1) the post-turn listener blocked the arm on the channel-a (is_error
        RESULT) path — the drive loops must arm directly + skip the drain;
    (2) re-send the ORIGINAL request when the failed turn produced no output,
        else the 'continue' prompt makes the model ramble."""

    def test_drive_loops_arm_instead_of_blocking_drain(self, sm_module):
        # Both drive loops must choose _arm_api_retry over _post_turn_compact_drain
        # when a retry is flagged, so the listener can't block the arm.
        for meth in (sm_module.SessionManager._drive_session,
                     sm_module.SessionManager._send_query):
            src = inspect.getsource(meth)
            assert "_api_retry_needed" in src and "_arm_api_retry" in src
            assert "_post_turn_compact_drain" in src
            # the arm and the drain must be in the same result-handled block
            i_arm = src.find("_arm_api_retry(info.session_id")
            i_drain = src.find("await self._post_turn_compact_drain")
            assert i_arm != -1 and i_drain != -1 and i_arm < i_drain, \
                "arm must be chosen before/instead of the blocking drain"

    def test_fire_resends_original_when_no_output(self, session_manager, sm_module):
        # Transcript has the user request but NO assistant text after it.
        info = _make_idle_session(sm_module, session_manager, "resend-1",
                                  user_text="Reply with token ABC123")
        rec = []
        with patch.object(session_manager, 'send_message',
                          side_effect=lambda s, t, **k: rec.append((t, k)) or {"ok": True}):
            session_manager._fire_api_retry("resend-1", info)
        assert rec and rec[0][0] == "Reply with token ABC123", \
            "should re-send the ORIGINAL request when no output was produced"
        assert rec[0][1].get("_auto_retry") is True

    def test_fire_continues_when_had_output(self, session_manager, sm_module):
        # Transcript shows the turn already ran a TOOL (side-effectful work) —
        # so the retry must CONTINUE, not re-send (which would duplicate it).
        info = _make_idle_session(sm_module, session_manager, "resend-2",
                                  user_text="Do the big task")
        info.entries.append(sm_module.LogEntry(kind="tool_use", text=""))
        info.entries.append(sm_module.LogEntry(kind="tool_result", text="done"))
        rec = []
        with patch.object(session_manager, 'send_message',
                          side_effect=lambda s, t, **k: rec.append((t, k)) or {"ok": True}):
            session_manager._fire_api_retry("resend-2", info)
        assert rec and rec[0][0] == sm_module.SessionManager._API_RETRY_CONTINUE_PROMPT, \
            "should send the continue prompt when the turn had produced output"


class TestQueueDuringRetry:
    """A queued follow-up must wait behind an active auto-retry countdown —
    the retry is the continuation of the failed turn and must run first."""

    def test_queue_held_while_retry_pending(self, session_manager, sm_module):
        info = _make_idle_session(sm_module, session_manager, "q-1")
        info.retry_at = time.time() + 30   # countdown active
        dispatched = []
        with patch.object(session_manager, '_try_dispatch_queue',
                          side_effect=lambda sid: dispatched.append(sid)):
            r = session_manager.queue_message("q-1", "a follow-up")
        assert r.get("ok") is not False
        assert dispatched == [], "queued message must NOT dispatch during a retry countdown"

    def test_queue_dispatches_once_retry_cleared(self, session_manager, sm_module):
        info = _make_idle_session(sm_module, session_manager, "q-2")
        info.retry_at = 0.0   # no countdown -> normal dispatch
        dispatched = []
        with patch.object(session_manager, '_try_dispatch_queue',
                          side_effect=lambda sid: dispatched.append(sid)):
            session_manager.queue_message("q-2", "a follow-up")
        assert dispatched, "queued message should dispatch normally when no retry is pending"

    def test_fire_does_not_emit_idle_before_send(self, sm_module):
        # _fire_api_retry must NOT emit an IDLE state (open queue gate) before
        # send_message flips to WORKING — otherwise a queued msg races ahead.
        src = inspect.getsource(sm_module.SessionManager._fire_api_retry)
        # the send happens; and there is no _emit_state immediately before it
        assert "send_message(info.session_id, retry_text" in src
        tail = src[src.rfind("info._api_retry_count += 1"):]
        assert "_emit_state(info)" not in tail, \
            "_fire_api_retry must not emit IDLE state before send_message (queue race)"
