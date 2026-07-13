"""Tests for SDK monkey-patches.

Validates that:
1. Structural assertions pass for the installed SDK version
2. Safe parse_message returns None for unknown message types
3. Transport adapter correctly reformats permission responses
4. Transport adapter suppresses end_input when keep_stdin_open=True
5. Transport adapter delegates all other methods unchanged
6. POSIX subprocesses are isolated into their own session (Patch 4 — guards
   against daemon-killing killpg in _kill_process_tree)
"""

import asyncio
import json
import os
import signal
import subprocess
import sys
import pytest
from unittest.mock import AsyncMock, MagicMock, patch

# Apply patches before any tests run (mirrors what session_manager does at import)
from daemon.sdk_patches import apply_patches as _apply_patches
_apply_patches()


def _run(coro):
    """Helper to run async code in sync tests."""
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        loop = None
    if loop and loop.is_running():
        import concurrent.futures
        with concurrent.futures.ThreadPoolExecutor() as pool:
            return pool.submit(asyncio.run, coro).result()
    return asyncio.run(coro)


# ── Structural Assertion Tests ──────────────────────────���────────────────


class TestStructuralAssertions:
    """Verify SDK structures that patches depend on haven't changed."""

    def test_parse_message_preconditions(self):
        """parse_message exists and MessageParseError has .data attribute."""
        from daemon.sdk_patches import _assert_patch_parse_message_preconditions
        _assert_patch_parse_message_preconditions()

    def test_transport_adapter_preconditions(self):
        """Query.__init__ accepts transport and can_use_tool; handler uses 'allow' format."""
        from daemon.sdk_patches import _assert_patch_transport_adapter_preconditions
        _assert_patch_transport_adapter_preconditions()

    def test_json_buffer_limit_preconditions(self):
        """subprocess_cli._MAX_BUFFER_SIZE exists and the read loop references it."""
        from daemon.sdk_patches import (
            _assert_patch_raise_json_buffer_limit_preconditions,
        )
        # Returns the resolved target size in bytes; must be a positive int.
        target = _assert_patch_raise_json_buffer_limit_preconditions()
        assert isinstance(target, int) and target > 0


# ── JSON Buffer Limit Tests (Patch 5) ────────────────────────────────────


class TestRaiseJsonBufferLimit:
    """Patch 5 lifts the SDK's hardcoded 1MB per-message JSON decode ceiling.

    Regression guard for the "stream fails, reconnects, fails again" loop:
    a single tool result / message over 1MB raised SDKJSONDecodeError, which
    session_manager classified as a non-transport error and hard-reconnected
    instead of self-healing — and the next turn busted the buffer again.
    """

    _SDK_DEFAULT = 1024 * 1024  # the value the SDK ships with

    def test_buffer_limit_raised_above_sdk_default(self):
        """After patching (done at module load), the ceiling must exceed 1MB."""
        from claude_code_sdk._internal.transport import subprocess_cli as sc
        assert sc._MAX_BUFFER_SIZE > self._SDK_DEFAULT, (
            f"_MAX_BUFFER_SIZE {sc._MAX_BUFFER_SIZE} not raised above the SDK's "
            f"1MB default — oversized messages will kill the stream again"
        )

    def test_buffer_limit_matches_default_target(self):
        """With no env override, the ceiling should be the 64MB default."""
        from daemon.sdk_patches import _DEFAULT_MAX_BUFFER_MB
        from claude_code_sdk._internal.transport import subprocess_cli as sc
        # This holds as long as VIBENODE_SDK_MAX_BUFFER_MB wasn't set when
        # patches were applied at import time.
        if not os.environ.get("VIBENODE_SDK_MAX_BUFFER_MB"):
            assert sc._MAX_BUFFER_SIZE == _DEFAULT_MAX_BUFFER_MB * 1024 * 1024

    def test_env_override_is_respected(self, monkeypatch):
        """VIBENODE_SDK_MAX_BUFFER_MB overrides the default target size."""
        from daemon.sdk_patches import (
            _assert_patch_raise_json_buffer_limit_preconditions,
        )
        monkeypatch.setenv("VIBENODE_SDK_MAX_BUFFER_MB", "128")
        target = _assert_patch_raise_json_buffer_limit_preconditions()
        assert target == 128 * 1024 * 1024

    def test_env_override_invalid_falls_back_to_default(self, monkeypatch):
        """A non-integer override is ignored, not fatal."""
        from daemon.sdk_patches import (
            _assert_patch_raise_json_buffer_limit_preconditions,
            _DEFAULT_MAX_BUFFER_MB,
        )
        monkeypatch.setenv("VIBENODE_SDK_MAX_BUFFER_MB", "not-a-number")
        target = _assert_patch_raise_json_buffer_limit_preconditions()
        assert target == _DEFAULT_MAX_BUFFER_MB * 1024 * 1024

    def test_env_override_nonpositive_falls_back_to_default(self, monkeypatch):
        """Zero/negative overrides are ignored (would disable the ceiling)."""
        from daemon.sdk_patches import (
            _assert_patch_raise_json_buffer_limit_preconditions,
            _DEFAULT_MAX_BUFFER_MB,
        )
        monkeypatch.setenv("VIBENODE_SDK_MAX_BUFFER_MB", "0")
        target = _assert_patch_raise_json_buffer_limit_preconditions()
        assert target == _DEFAULT_MAX_BUFFER_MB * 1024 * 1024


# ── Safe Parse Message Tests ─────────────────────────────────────────────


class TestSafeParseMessage:
    """Test that the safe parse_message wrapper handles unknown types."""

    def test_known_types_pass_through(self):
        """Known message types should parse normally."""
        from claude_code_sdk._internal.message_parser import parse_message
        from claude_code_sdk.types import SystemMessage

        result = parse_message({
            "type": "system",
            "subtype": "init",
            "data": {},
        })
        assert isinstance(result, SystemMessage)

    def test_unknown_type_returns_none(self):
        """Unknown but structurally valid messages should return None."""
        from claude_code_sdk._internal.message_parser import parse_message

        result = parse_message({
            "type": "rate_limit_event",
            "data": {"retry_after": 5},
        })
        assert result is None

    def test_unknown_type_with_extra_fields_returns_none(self):
        """Unknown types with extra fields should also return None."""
        from claude_code_sdk._internal.message_parser import parse_message

        result = parse_message({
            "type": "some_future_sdk_type",
            "version": 2,
            "payload": {"key": "value"},
        })
        assert result is None

    def test_malformed_message_still_raises(self):
        """Messages without a type field should still raise."""
        from claude_code_sdk._internal.message_parser import parse_message
        from claude_code_sdk._errors import MessageParseError

        with pytest.raises(MessageParseError):
            parse_message({"no_type_field": True})

    def test_non_dict_still_raises(self):
        """Non-dict input should still raise."""
        from claude_code_sdk._internal.message_parser import parse_message
        from claude_code_sdk._errors import MessageParseError

        with pytest.raises(MessageParseError):
            parse_message("not a dict")

    def test_client_module_also_patched(self):
        """The client module's parse_message should also be patched."""
        from claude_code_sdk.client import parse_message

        result = parse_message({
            "type": "rate_limit_event",
            "data": {},
        })
        assert result is None


# ── Transport Adapter Tests ────────────────────────────────��─────────────


class MockTransport:
    """Mock transport for testing the adapter."""

    def __init__(self):
        self.written: list[str] = []
        self.connected = False
        self.closed = False
        self.input_ended = False
        self._ready = True

    async def connect(self):
        self.connected = True

    async def write(self, data: str):
        self.written.append(data)

    async def read_messages(self):
        yield {"type": "test"}

    async def close(self):
        self.closed = True

    def is_ready(self):
        return self._ready

    async def end_input(self):
        self.input_ended = True


class TestTransportAdapter:
    """Test the VibeNodeTransportAdapter."""

    @pytest.fixture
    def mock_transport(self):
        return MockTransport()

    @pytest.fixture
    def adapter(self, mock_transport):
        from daemon.sdk_transport_adapter import VibeNodeTransportAdapter
        return VibeNodeTransportAdapter(mock_transport, keep_stdin_open=False)

    @pytest.fixture
    def adapter_keep_open(self, mock_transport):
        from daemon.sdk_transport_adapter import VibeNodeTransportAdapter
        return VibeNodeTransportAdapter(mock_transport, keep_stdin_open=True)

    # -- Permission response reformatting --

    def test_reformat_allow_response(self, adapter, mock_transport):
        """SDK allow format should be converted to CLI 2.x format."""
        sdk_response = {
            "type": "control_response",
            "response": {
                "subtype": "success",
                "request_id": "req-1",
                "response": {
                    "allow": True,
                    "input": {"command": "ls"},
                },
            },
        }
        _run(adapter.write(json.dumps(sdk_response) + "\n"))

        assert len(mock_transport.written) == 1
        written = json.loads(mock_transport.written[0])
        inner = written["response"]["response"]
        assert inner == {"behavior": "allow", "updatedInput": {"command": "ls"}}

    def test_reformat_allow_no_input(self, adapter, mock_transport):
        """Allow without input should default to empty updatedInput."""
        sdk_response = {
            "type": "control_response",
            "response": {
                "subtype": "success",
                "request_id": "req-2",
                "response": {"allow": True},
            },
        }
        _run(adapter.write(json.dumps(sdk_response) + "\n"))

        written = json.loads(mock_transport.written[0])
        inner = written["response"]["response"]
        assert inner == {"behavior": "allow", "updatedInput": {}}

    def test_reformat_deny_response(self, adapter, mock_transport):
        """SDK deny format should be converted to CLI 2.x format."""
        sdk_response = {
            "type": "control_response",
            "response": {
                "subtype": "success",
                "request_id": "req-3",
                "response": {
                    "allow": False,
                    "reason": "User denied",
                },
            },
        }
        _run(adapter.write(json.dumps(sdk_response) + "\n"))

        written = json.loads(mock_transport.written[0])
        inner = written["response"]["response"]
        assert inner == {"behavior": "deny", "message": "User denied"}

    def test_deny_default_message(self, adapter, mock_transport):
        """Deny without reason should default to 'Denied'."""
        sdk_response = {
            "type": "control_response",
            "response": {
                "subtype": "success",
                "request_id": "req-4",
                "response": {"allow": False},
            },
        }
        _run(adapter.write(json.dumps(sdk_response) + "\n"))

        written = json.loads(mock_transport.written[0])
        inner = written["response"]["response"]
        assert inner == {"behavior": "deny", "message": "Denied"}

    def test_non_permission_passthrough(self, adapter, mock_transport):
        """Non-permission messages should pass through unchanged."""
        msg = {"type": "user", "message": {"role": "user", "content": "hello"}}
        data = json.dumps(msg) + "\n"
        _run(adapter.write(data))

        assert mock_transport.written[0] == data

    def test_error_response_passthrough(self, adapter, mock_transport):
        """Error control responses should pass through (no 'allow' key)."""
        error_response = {
            "type": "control_response",
            "response": {
                "subtype": "error",
                "request_id": "req-5",
                "error": "something failed",
            },
        }
        data = json.dumps(error_response) + "\n"
        _run(adapter.write(data))

        assert mock_transport.written[0] == data

    def test_malformed_json_passthrough(self, adapter, mock_transport):
        """Malformed JSON should pass through unchanged."""
        data = "not json at all\n"
        _run(adapter.write(data))
        assert mock_transport.written[0] == data

    def test_non_dict_response_field_passthrough(self, adapter, mock_transport):
        """Messages where 'response' is not a dict should pass through."""
        msg = {"type": "control_response", "response": "not a dict"}
        data = json.dumps(msg) + "\n"
        _run(adapter.write(data))
        assert mock_transport.written[0] == data

    def test_nested_non_dict_response_passthrough(self, adapter, mock_transport):
        """Messages where nested 'response' is not a dict should pass through."""
        msg = {
            "type": "control_response",
            "response": {"subtype": "success", "request_id": "r1", "response": 42},
        }
        data = json.dumps(msg) + "\n"
        _run(adapter.write(data))
        assert mock_transport.written[0] == data

    # -- end_input behavior --

    def test_end_input_passes_through_by_default(self, adapter, mock_transport):
        """Without keep_stdin_open, end_input should delegate."""
        _run(adapter.end_input())
        assert mock_transport.input_ended is True

    def test_end_input_suppressed_when_keep_open(
        self, adapter_keep_open, mock_transport
    ):
        """With keep_stdin_open=True, end_input should be suppressed."""
        _run(adapter_keep_open.end_input())
        assert mock_transport.input_ended is False

    # -- delegation --

    def test_connect_delegates(self, adapter, mock_transport):
        _run(adapter.connect())
        assert mock_transport.connected is True

    def test_close_delegates(self, adapter, mock_transport):
        _run(adapter.close())
        assert mock_transport.closed is True

    def test_is_ready_delegates(self, adapter, mock_transport):
        assert adapter.is_ready() is True
        mock_transport._ready = False
        assert adapter.is_ready() is False

    def test_inner_property(self, adapter, mock_transport):
        """inner property should expose the underlying transport."""
        assert adapter.inner is mock_transport

    # -- error propagation --

    def test_write_error_propagates(self, mock_transport):
        """If inner transport write() raises, adapter must propagate it."""
        from daemon.sdk_transport_adapter import VibeNodeTransportAdapter

        async def failing_write(data):
            raise ConnectionError("pipe broken")

        mock_transport.write = failing_write
        adapter = VibeNodeTransportAdapter(mock_transport, keep_stdin_open=False)

        with pytest.raises(ConnectionError, match="pipe broken"):
            _run(adapter.write('{"type": "user"}\n'))

    def test_write_error_propagates_during_reformat(self, mock_transport):
        """If inner write() fails AFTER reformatting, error still propagates."""
        from daemon.sdk_transport_adapter import VibeNodeTransportAdapter

        async def failing_write(data):
            raise ConnectionError("transport dead")

        mock_transport.write = failing_write
        adapter = VibeNodeTransportAdapter(mock_transport, keep_stdin_open=False)

        sdk_response = {
            "type": "control_response",
            "response": {
                "subtype": "success",
                "request_id": "req-err",
                "response": {"allow": True},
            },
        }
        with pytest.raises(ConnectionError, match="transport dead"):
            _run(adapter.write(json.dumps(sdk_response) + "\n"))

    # -- non-permission success responses --

    def test_hook_callback_response_passthrough(self, adapter, mock_transport):
        """Hook callback success responses should NOT be reformatted."""
        hook_response = {
            "type": "control_response",
            "response": {
                "subtype": "success",
                "request_id": "req-hook",
                "response": {"some_hook_data": True},
            },
        }
        data = json.dumps(hook_response) + "\n"
        _run(adapter.write(data))

        # Should pass through unchanged — no "allow" key in inner response
        assert mock_transport.written[0] == data

    def test_mcp_response_passthrough(self, adapter, mock_transport):
        """MCP success responses should NOT be reformatted."""
        mcp_response = {
            "type": "control_response",
            "response": {
                "subtype": "success",
                "request_id": "req-mcp",
                "response": {"mcp_response": {"result": "ok"}},
            },
        }
        data = json.dumps(mcp_response) + "\n"
        _run(adapter.write(data))

        assert mock_transport.written[0] == data

    # -- outer field preservation --

    def test_reformat_preserves_outer_fields(self, adapter, mock_transport):
        """Reformatting should preserve request_id, subtype, and other outer fields."""
        sdk_response = {
            "type": "control_response",
            "response": {
                "subtype": "success",
                "request_id": "req-preserve",
                "response": {"allow": True, "input": {"file": "test.py"}},
            },
        }
        _run(adapter.write(json.dumps(sdk_response) + "\n"))

        written = json.loads(mock_transport.written[0])
        assert written["type"] == "control_response"
        assert written["response"]["subtype"] == "success"
        assert written["response"]["request_id"] == "req-preserve"
        # Inner response should be reformatted
        assert written["response"]["response"]["behavior"] == "allow"


# ── Idempotency Tests ───────────────────────────────────────────────────


class TestIdempotency:
    """Test that apply_patches() is safe to call multiple times."""

    def test_apply_patches_idempotent(self):
        """Second call to apply_patches() should return empty list (already applied)."""
        from daemon.sdk_patches import apply_patches
        # Patches were already applied at module load. Calling again should no-op.
        result = apply_patches()
        assert result == []

    def test_parse_message_not_double_wrapped(self):
        """parse_message should be wrapped exactly once, not nested."""
        from claude_code_sdk._internal.message_parser import parse_message

        # Call with an unknown type — should return None (one wrap)
        result = parse_message({"type": "unknown_test_type_xyz"})
        assert result is None

        # Call with a known type — should parse normally (not broken by stacking)
        from claude_code_sdk.types import SystemMessage
        result = parse_message({"type": "system", "subtype": "init", "data": {}})
        assert isinstance(result, SystemMessage)


# ── Patch Application Tests ──────────────────────────────────────────────


class TestPatchApplication:
    """Test that patches are correctly applied to the SDK."""

    def test_transport_adapter_injected_into_query(self):
        """Query.__init__ should wrap transport in VibeNodeTransportAdapter."""
        from claude_code_sdk._internal.query import Query
        from daemon.sdk_transport_adapter import VibeNodeTransportAdapter

        # Create a mock transport
        mock = MagicMock()
        mock.connect = AsyncMock()
        mock.write = AsyncMock()
        mock.close = AsyncMock()
        mock.end_input = AsyncMock()
        mock.is_ready = MagicMock(return_value=True)
        mock.read_messages = MagicMock()

        # Create a Query — the patch should wrap the transport
        q = Query(
            transport=mock,
            is_streaming_mode=True,
            can_use_tool=None,
        )

        # Transport should be wrapped in our adapter
        assert isinstance(q.transport, VibeNodeTransportAdapter)
        assert q.transport.inner is mock

    def test_adapter_keep_stdin_when_can_use_tool_set(self):
        """When can_use_tool is provided, adapter should keep stdin open."""
        from claude_code_sdk._internal.query import Query
        from daemon.sdk_transport_adapter import VibeNodeTransportAdapter

        mock = MagicMock()
        mock.connect = AsyncMock()
        mock.write = AsyncMock()
        mock.close = AsyncMock()
        mock.end_input = AsyncMock()
        mock.is_ready = MagicMock(return_value=True)
        mock.read_messages = MagicMock()

        async def dummy_callback(tool, inp, ctx):
            pass

        q = Query(
            transport=mock,
            is_streaming_mode=True,
            can_use_tool=dummy_callback,
        )

        assert isinstance(q.transport, VibeNodeTransportAdapter)
        assert q.transport._keep_stdin_open is True

    def test_adapter_no_keep_stdin_without_can_use_tool(self):
        """Without can_use_tool, adapter should not keep stdin open."""
        from claude_code_sdk._internal.query import Query
        from daemon.sdk_transport_adapter import VibeNodeTransportAdapter

        mock = MagicMock()
        mock.connect = AsyncMock()
        mock.write = AsyncMock()
        mock.close = AsyncMock()
        mock.end_input = AsyncMock()
        mock.is_ready = MagicMock(return_value=True)
        mock.read_messages = MagicMock()

        q = Query(
            transport=mock,
            is_streaming_mode=True,
            can_use_tool=None,
        )

        assert isinstance(q.transport, VibeNodeTransportAdapter)
        assert q.transport._keep_stdin_open is False

    def test_no_double_wrapping(self):
        """If transport is already a VibeNodeTransportAdapter, don't wrap again."""
        from claude_code_sdk._internal.query import Query
        from daemon.sdk_transport_adapter import VibeNodeTransportAdapter

        mock = MagicMock()
        mock.connect = AsyncMock()
        mock.write = AsyncMock()
        mock.close = AsyncMock()
        mock.end_input = AsyncMock()
        mock.is_ready = MagicMock(return_value=True)
        mock.read_messages = MagicMock()

        # Pre-wrap in adapter
        pre_wrapped = VibeNodeTransportAdapter(mock, keep_stdin_open=True)

        q = Query(
            transport=pre_wrapped,
            is_streaming_mode=True,
            can_use_tool=None,
        )

        # Should still be the same adapter, not double-wrapped
        assert q.transport is pre_wrapped
        assert q.transport.inner is mock


# ── POSIX Subprocess Isolation Tests (Patch 4) ──────────────────────────


@pytest.mark.skipif(os.name == "nt", reason="POSIX-only patch")
class TestPosixSubprocessIsolation:
    """Patch 4 must place every Popen child in its own session/pgrp.

    Regression guard for the bug where stopping one session killed the
    whole daemon on Linux. _kill_process_tree() calls
    os.killpg(os.getpgid(cli_pid), SIGTERM); without start_new_session=True
    the CLI inherited the daemon's pgid and killpg blasted the daemon.
    """

    def test_default_spawn_is_session_isolated(self):
        """A normal Popen must land in a session distinct from this process."""
        proc = subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(5)"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        try:
            child_sid = os.getsid(proc.pid)
            self_sid = os.getsid(os.getpid())
            # If the patch is missing, child inherits parent's session.
            # If the patch is applied, child is its own session leader.
            assert child_sid != self_sid, (
                f"Child SID {child_sid} == parent SID {self_sid}: "
                "Patch 4 (start_new_session injection) is not active. "
                "killpg-based session stop will kill the daemon."
            )
            # Child should be its own session leader (sid == pid).
            assert child_sid == proc.pid, (
                f"Child SID {child_sid} != child PID {proc.pid}: "
                "child is not the session leader as expected."
            )
        finally:
            proc.kill()
            proc.wait(timeout=5)

    def test_explicit_start_new_session_false_is_overridden(self):
        """Patch 4 OVERRIDES explicit start_new_session=False on POSIX.

        Background:  the SDK uses ``anyio.open_process``, which has
        ``start_new_session: bool = False`` as a default and forwards
        that explicit False to ``subprocess.Popen``.  An earlier
        "respect explicit False" rule made the patch silently no-op
        for every SDK-spawned CLI — the daemon-blast bug returned
        because the CLI inherited the daemon's process group.

        The current rule: on POSIX we always force isolation unless
        the caller supplied ``preexec_fn``.  This is a deliberate
        deviation from honoring caller intent because the safety
        guarantee (killpg cannot kill the daemon) requires it for
        every spawn path, including third-party libraries that pass
        their own default of False through.
        """
        proc = subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(5)"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=False,
        )
        try:
            child_sid = os.getsid(proc.pid)
            self_sid = os.getsid(os.getpid())
            # The override must produce isolation, not honor False.
            assert child_sid != self_sid, (
                "Patch 4 honored explicit start_new_session=False — "
                "this re-introduces the SDK daemon-blast bug. The "
                "current policy is to force isolation on POSIX even "
                "when False was passed explicitly."
            )
            assert child_sid == proc.pid, (
                "Child should be its own session leader."
            )
        finally:
            proc.kill()
            proc.wait(timeout=5)

    def test_preexec_fn_skips_injection(self):
        """If caller supplied preexec_fn, the patch must NOT add start_new_session.

        Combining the two would silently change child setup behavior. The
        patch detects preexec_fn and bows out, leaving the caller's setup
        untouched.
        """
        # preexec_fn=lambda: None means "do nothing extra"; without our
        # injection, the child stays in the parent's session.
        proc = subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(5)"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            preexec_fn=lambda: None,
        )
        try:
            child_sid = os.getsid(proc.pid)
            self_sid = os.getsid(os.getpid())
            assert child_sid == self_sid, (
                "Patch 4 stepped on a caller-supplied preexec_fn by also "
                "setting start_new_session=True."
            )
        finally:
            proc.kill()
            proc.wait(timeout=5)

    def test_killpg_on_isolated_child_does_not_signal_parent(self):
        """End-to-end guard: the exact bug scenario must not reproduce.

        Spawn a child, kill its process group with SIGTERM, and confirm
        THIS test process (the stand-in for the daemon) is unaffected.
        If Patch 4 is missing, this test would deliver SIGTERM to itself.
        """
        import signal
        proc = subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(10)"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        try:
            child_pgid = os.getpgid(proc.pid)
            self_pgid = os.getpgid(os.getpid())
            assert child_pgid != self_pgid, (
                "Child shares parent's pgid — killpg below would kill "
                "the test process. Patch 4 is not active."
            )
            # Safe to killpg now; the child is in its own group.
            os.killpg(child_pgid, signal.SIGTERM)
            proc.wait(timeout=5)
            # Child should be terminated by SIGTERM (negative returncode = signal).
            assert proc.returncode is not None
        finally:
            if proc.poll() is None:
                proc.kill()
                proc.wait(timeout=5)


# ── _close_session exception-safety contract ────────────────────────────


class TestCloseSessionExceptionSafety:
    """``_close_session`` must never let an exception escape.

    It runs on the daemon's asyncio loop via ``run_coroutine_threadsafe``;
    an unhandled exception lands on the resulting Future, where
    ``close_session_sync`` reads it via ``future.result()`` and could
    mislead callers into thinking the close failed when it actually
    succeeded.  More importantly, an exception in the cleanup-side
    ``except`` block (which itself emits state + schedules a save)
    could fire on top of the original and break the
    ``info.state = STOPPED`` invariant other code relies on.

    These tests make sure each cleanup step is independently fault-isolated.
    """

    def _make_session_manager(self):
        """Build a minimal SessionManager + SessionInfo for direct testing
        of `_close_session` without spinning up the IPC plumbing.
        """
        from daemon.session_manager import SessionManager, SessionInfo, SessionState
        sm = SessionManager()
        info = SessionInfo(session_id="test-sid")
        info.state = SessionState.IDLE
        # Don't try to extract from a real client; pretend extract returns 0.
        info.client = None
        info._cli_pid = 0
        with sm._lock:
            sm._sessions[info.session_id] = info
        return sm, info

    def _run(self, coro):
        """Run a coroutine synchronously."""
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            loop = None
        if loop and loop.is_running():
            import concurrent.futures
            with concurrent.futures.ThreadPoolExecutor() as pool:
                return pool.submit(asyncio.run, coro).result()
        return asyncio.run(coro)

    def test_close_session_swallows_emit_state_failure(self):
        """If the push callback raises, _close_session must still
        complete and leave ``info.state = STOPPED``.
        """
        from daemon.session_manager import SessionState
        sm, info = self._make_session_manager()
        # Force _emit_state to blow up by sticking in a callback that raises.
        sm._push_callback = lambda *a, **kw: (_ for _ in ()).throw(
            RuntimeError("simulated push failure"))

        # Must not raise.
        self._run(sm._close_session(info.session_id))

        # Post-condition: STOPPED was reached even though emit threw.
        assert info.state == SessionState.STOPPED, (
            "_close_session let an _emit_state failure prevent "
            "STOPPED — exception isolation is broken"
        )

    def test_close_session_swallows_kill_process_tree_failure(self):
        """If _kill_process_tree raises, _close_session must still
        complete and leave ``info.state = STOPPED``.
        """
        from daemon.session_manager import SessionState
        sm, info = self._make_session_manager()
        info._cli_pid = 1  # any nonzero so kill is attempted

        # Replace _kill_process_tree with a raiser.
        original_kpt = sm._kill_process_tree
        sm._kill_process_tree = (lambda pid: (_ for _ in ()).throw(  # type: ignore[assignment]
            RuntimeError("simulated kill failure")))
        try:
            self._run(sm._close_session(info.session_id))
        finally:
            sm._kill_process_tree = original_kpt  # type: ignore[assignment]

        assert info.state == SessionState.STOPPED, (
            "_close_session let a _kill_process_tree failure prevent "
            "STOPPED"
        )

    def test_close_session_swallows_disconnect_failure(self):
        """If SDK disconnect raises, _close_session must still
        complete and leave ``info.state = STOPPED``.
        """
        from daemon.session_manager import SessionState
        sm, info = self._make_session_manager()
        # Set a sentinel client so the disconnect branch fires.
        info.client = object()

        # Make _sdk.disconnect raise.
        async def _raise(_):
            raise RuntimeError("simulated disconnect failure")
        original = sm._sdk.disconnect
        sm._sdk.disconnect = _raise  # type: ignore[assignment]
        try:
            self._run(sm._close_session(info.session_id))
        finally:
            sm._sdk.disconnect = original  # type: ignore[assignment]

        assert info.state == SessionState.STOPPED


@pytest.mark.skipif(os.name != "nt", reason="Windows-only check")
class TestWindowsPatchSkipsPosixIsolation:
    """On Windows, Patch 4 must be a no-op (Windows uses taskkill, not killpg)."""

    def test_isolation_patch_returns_false_on_windows(self):
        from daemon.sdk_patches import _apply_patch_isolate_posix_subprocesses
        # Returns False because the patch doesn't apply on Windows.
        assert _apply_patch_isolate_posix_subprocesses() is False


# ── End-to-End: Patch 4 catches the actual SDK spawn path ───────────────


@pytest.mark.skipif(os.name == "nt", reason="POSIX-only path")
class TestPatch4CatchesSdkSpawnPath:
    """The SDK uses ``anyio.open_process`` →
    ``asyncio.create_subprocess_exec`` → ``_UnixSubprocessTransport`` →
    ``subprocess.Popen``.  Patching ``subprocess.Popen.__init__``
    transitively isolates every CLI subprocess spawned via the SDK.

    Earlier tests directly call ``subprocess.Popen`` to verify the patch
    on its own seam.  This test exercises the EXACT chain the SDK uses
    end-to-end so a future Python / anyio change that bypasses
    ``subprocess.Popen`` (e.g. switching to a direct ``os.posix_spawn``
    path) is caught immediately rather than at runtime when "Stop
    Session" silently kills the daemon again.
    """

    def test_anyio_open_process_child_is_session_isolated(self):
        """Run anyio.open_process with no explicit isolation kwargs and
        confirm Patch 4 still injects start_new_session via the
        Popen-level patch.  This is the same pattern
        ``claude_code_sdk._internal.transport.subprocess_cli.py`` uses
        to spawn the Claude CLI.
        """
        import anyio

        async def _run() -> tuple[int, int, int]:
            proc = await anyio.open_process(
                [sys.executable, "-c", "import time; time.sleep(10)"],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            try:
                child_pid = proc.pid
                child_sid = os.getsid(child_pid)
                self_sid = os.getsid(os.getpid())
                return child_pid, child_sid, self_sid
            finally:
                proc.kill()
                # Drain wait so the test process doesn't leave a zombie.
                await proc.wait()

        child_pid, child_sid, self_sid = anyio.run(_run)

        # The exact regression check: child must be in its own session.
        # If this assertion ever fails, Patch 4 stopped catching the
        # SDK's spawn path and "Stop Session" will start blasting the
        # daemon again on Linux.
        assert child_sid != self_sid, (
            f"anyio.open_process child SID {child_sid} == parent SID "
            f"{self_sid}: Patch 4 is no longer being invoked through the "
            "SDK's actual spawn chain.  Either the patch isn't applied, "
            "or asyncio/anyio bypasses subprocess.Popen on this runtime."
        )
        assert child_sid == child_pid, (
            f"Child SID {child_sid} != child PID {child_pid}: child is "
            "not the session leader."
        )

    def test_full_stop_session_simulation_does_not_kill_daemon(self):
        """END-TO-END SMOKING GUN: reproduce the exact "Stop Session
        blew up the daemon connection on Linux" bug and prove the
        composite fix prevents it.

        Sequence (mirrors what `_close_session` does on a real stop):
          1. Spawn a CLI subprocess via ``anyio.open_process`` exactly
             how the SDK does it.
          2. Call ``SessionManager._kill_process_tree(cli_pid)`` —
             which goes through the killpg path.
          3. Confirm: the test process (daemon stand-in) is still
             alive AND the CLI subprocess is dead.

        Before the fix, step 2 would deliver SIGTERM to the test
        process via killpg.  The test process would die before
        reaching the post-condition assertion, so the test runner
        would report a fatal signal — which is exactly how the bug
        manifested in production.
        """
        import anyio
        from daemon.session_manager import SessionManager

        async def _spawn() -> int:
            proc = await anyio.open_process(
                [sys.executable, "-c", "import time; time.sleep(30)"],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            return proc.pid

        cli_pid = anyio.run(_spawn)

        # Safety net so the child can't outlive the test if a future
        # change breaks the kill path.
        try:
            # Pre-condition: the simulated CLI is in its OWN session,
            # not ours (proves Patch 4 caught the SDK spawn path).
            cli_pgid = os.getpgid(cli_pid)
            self_pgid = os.getpgid(os.getpid())
            assert cli_pgid != self_pgid, (
                f"CLI subprocess landed in the daemon's process group "
                f"(both pgid={cli_pgid}).  Patch 4 did not isolate the "
                "anyio spawn — _kill_process_tree below would killpg "
                "the test process.  The fix has regressed."
            )

            our_pid_before = os.getpid()

            # The exact call that used to blast the daemon.
            SessionManager._kill_process_tree(cli_pid)

            # Post-condition 1: the daemon stand-in (us) survives.
            # If killpg leaked, the test process would already be dead
            # from SIGTERM and we'd never reach this line.
            assert os.getpid() == our_pid_before, (
                "Test process survived but somehow has a different PID "
                "— this should be impossible."
            )

            # Post-condition 2: the CLI is actually dead.
            #
            # When anyio.run() returns, its child watcher has already
            # exited so the killed CLI lingers as a zombie until reaped.
            # ``os.kill(pid, 0)`` returns success for zombies (the PID
            # still exists in the process table), so we can't use that
            # for the "is it dead" check.  Read /proc/<pid>/status
            # directly to distinguish "running" from "zombie/dead".
            #
            # Falls back to waitpid()-with-WNOHANG on platforms without
            # /proc (macOS).
            import time as _t

            def _is_dead(pid: int) -> bool:
                # Linux: /proc/<pid>/status — State: Z means zombie (dead).
                try:
                    with open(f"/proc/{pid}/status") as fh:
                        for line in fh:
                            if line.startswith("State:"):
                                state = line.split()[1] if len(line.split()) > 1 else ""
                                # Z = zombie, X = dead.  R/S/D = alive.
                                return state in ("Z", "X")
                except FileNotFoundError:
                    # /proc entry gone → fully reaped → dead.
                    return True
                except OSError:
                    pass
                # Cross-platform fallback: try to reap.  If the process
                # was never our direct child, waitpid raises
                # ChildProcessError — fall back to /bin/kill -0 by way
                # of os.kill(pid, 0).
                try:
                    rpid, _ = os.waitpid(pid, os.WNOHANG)
                    return rpid != 0
                except ChildProcessError:
                    try:
                        os.kill(pid, 0)
                        return False
                    except ProcessLookupError:
                        return True

            for _ in range(50):
                if _is_dead(cli_pid):
                    break
                _t.sleep(0.1)
            else:
                pytest.fail(
                    "_kill_process_tree did not kill the CLI subprocess. "
                    "Either Patch 4's isolation broke the kill path, "
                    "or the safe-path killpg is no longer firing."
                )

            # Reap the zombie so the test process doesn't leave it
            # hanging around for the rest of the suite.
            try:
                os.waitpid(cli_pid, os.WNOHANG)
            except (ChildProcessError, OSError):
                pass
        finally:
            # Last-resort cleanup so a failed test doesn't leak.
            try:
                os.kill(cli_pid, signal.SIGKILL)
            except (ProcessLookupError, NameError):
                pass


# ── _kill_process_tree Defensive-pgid Check ─────────────────────────────


@pytest.mark.skipif(os.name == "nt", reason="POSIX-only defense")
class TestKillProcessTreeDefensivePgidCheck:
    """Belt-and-suspenders defense in `_kill_process_tree`.

    Patch 4 is supposed to ensure every CLI subprocess lives in its own
    session/pgrp.  If it ever fails to apply (race, third-party spawn
    path that bypasses subprocess.Popen, PID reuse), the unconditional
    `os.killpg(os.getpgid(pid), SIGTERM)` would blast the daemon and
    kill every running session.  This regression already shipped to
    Linux users.

    `_kill_process_tree` now refuses to call killpg when the target's
    pgid equals its own, falling back to per-PID kill.  These tests
    exercise that defense end-to-end.
    """

    def test_kill_process_tree_refuses_killpg_on_own_pgroup(self):
        """The exact bug scenario: child shares our pgid → killpg would
        kill us.  The defensive check must short-circuit to per-PID kill
        and the test process (stand-in for the daemon) must survive.
        """
        import signal as _signal
        import time as _time

        from daemon.session_manager import SessionManager

        # Spawn a child in OUR process group.  Patch 4 only opts out
        # when preexec_fn is supplied — using a no-op preexec_fn lets us
        # force the child into our session/pgrp so the defensive check
        # has a real failure mode to catch.
        proc = subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(10)"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            preexec_fn=lambda: None,
        )
        try:
            child_pgid = os.getpgid(proc.pid)
            self_pgid = os.getpgid(os.getpid())
            assert child_pgid == self_pgid, (
                "Test setup failed — child should be in our pgroup so "
                "the defensive check has something to defend against."
            )

            # Pre-condition: we (the test process, daemon stand-in) are alive.
            assert os.getpid() > 0
            our_pid_before = os.getpid()

            # Call the kill path.  Without the defensive check this
            # would killpg our own group and SIGTERM us.  With the
            # defensive check it falls back to per-PID kill — the child
            # dies, we survive.
            SessionManager._kill_process_tree(proc.pid)

            # Give the per-PID SIGTERM/SIGKILL a moment to land.
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                pytest.fail(
                    "Defensive fallback did not kill the child via per-PID."
                )

            # Post-condition: we (the daemon) are still alive.  If the
            # defensive check failed, we wouldn't reach this assertion.
            assert os.getpid() == our_pid_before
            # Child terminated by signal — returncode is negative.
            assert proc.returncode is not None
        finally:
            if proc.poll() is None:
                proc.kill()
                proc.wait(timeout=5)

    def test_kill_process_tree_uses_killpg_when_target_isolated(self):
        """When Patch 4 isolated the child correctly (different pgid),
        `_kill_process_tree` must still take the killpg path so
        descendants are reaped, not orphaned.  Verify by spawning a
        child with start_new_session=True (its own pgrp), giving it a
        grandchild, then confirming the grandchild dies too.
        """
        from daemon.session_manager import SessionManager

        # Parent shells out to a sleeping grandchild it doesn't reap.
        # When _kill_process_tree() killpgs the parent's group, the
        # grandchild (same group) must die too.
        script = (
            "import os, subprocess, time;"
            "p = subprocess.Popen(['python3', '-c', 'import time; time.sleep(60)']);"
            "open('/tmp/_kill_pt_grandchild.pid', 'w').write(str(p.pid));"
            "time.sleep(60)"
        )
        # Clean up any stale pid file from a previous run.
        try:
            os.unlink("/tmp/_kill_pt_grandchild.pid")
        except FileNotFoundError:
            pass

        proc = subprocess.Popen(
            [sys.executable, "-c", script],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,  # explicit isolation, like Patch 4
        )
        try:
            # Wait until the parent has spawned its grandchild.
            for _ in range(50):
                try:
                    grand_pid = int(
                        open("/tmp/_kill_pt_grandchild.pid").read().strip()
                    )
                    break
                except (FileNotFoundError, ValueError):
                    import time as _t
                    _t.sleep(0.1)
            else:
                pytest.fail("Grandchild never started")

            # Sanity: parent and grandchild share a pgrp distinct from ours.
            parent_pgid = os.getpgid(proc.pid)
            grand_pgid = os.getpgid(grand_pid)
            self_pgid = os.getpgid(os.getpid())
            assert parent_pgid != self_pgid, "Parent should be isolated"
            assert grand_pgid == parent_pgid, (
                "Grandchild should share parent's pgrp so killpg reaps it"
            )

            SessionManager._kill_process_tree(proc.pid)

            # Both parent and grandchild must be dead.
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                pytest.fail("Parent was not killed by killpg")
            # Grandchild — poll via os.kill(0) until it disappears.
            import time as _t
            for _ in range(50):
                try:
                    os.kill(grand_pid, 0)
                    _t.sleep(0.1)
                except ProcessLookupError:
                    break
            else:
                pytest.fail(
                    "Grandchild survived killpg — descendants are leaking"
                )
        finally:
            if proc.poll() is None:
                proc.kill()
                proc.wait(timeout=5)
            try:
                os.unlink("/tmp/_kill_pt_grandchild.pid")
            except FileNotFoundError:
                pass

    # ── Suicide-PID guard (added 2026-05-10) ──────────────────────────
    # The historic Linux "Stop Session crashes the daemon" regression was
    # supposed to be fixed by the pgid check above, but the user reported
    # it kept happening across 10+ "fix" attempts.  Root cause inspection
    # found a SECOND path the pgid check did not cover: if pid==os.getpid(),
    # pid==0, or pid<=1, the per-PID fallback INSIDE the pgid check would
    # still execute os.kill(pid, SIGTERM) and kill the daemon (or, for
    # pid==0, broadcast SIGTERM to the entire process group).  The new
    # SUICIDE GUARD #0 short-circuits before any signal flies.  These
    # tests prove the daemon stand-in survives every flavour of bad pid.
    @pytest.mark.parametrize("bad_pid_factory,label", [
        (lambda: os.getpid(), "own_pid"),
        (lambda: 0, "zero"),
        (lambda: 1, "init"),
        (lambda: -1, "minus_one"),
    ])
    def test_kill_process_tree_refuses_suicidal_pids(self, bad_pid_factory, label):
        """The daemon (stand-in: this test process) must survive when
        `_kill_process_tree` is called with a pid that would otherwise
        deliver a SIGTERM/SIGKILL to itself."""
        from daemon.session_manager import SessionManager

        bad_pid = bad_pid_factory()
        our_pid_before = os.getpid()

        # If the guard regresses, this call sends SIGTERM (then SIGKILL)
        # to our own pid (or broadcasts via pid=0).  The pytest process
        # would die mid-call and the assertion below would never run —
        # pytest would report the test as KILLED rather than FAILED, so
        # the absence of a green check is itself a regression signal.
        SessionManager._kill_process_tree(bad_pid)

        # We reached this line → we weren't signalled.  Sanity check.
        assert os.getpid() == our_pid_before, (
            f"Daemon stand-in survived suicide pid {bad_pid!r} ({label}) "
            "but PID changed — should be impossible."
        )

    def test_kill_process_tree_none_pid_does_not_crash(self):
        """`info._cli_pid` can be 0 by default and a buggy caller might
        pass through ``None`` directly.  The guard must handle both."""
        from daemon.session_manager import SessionManager

        # None must not raise (the function's try/except wraps everything
        # but the guard itself reads pid <= 1 which would raise TypeError
        # on None without the explicit None check).
        SessionManager._kill_process_tree(None)  # type: ignore[arg-type]
        # If we got here without raising, the guard handled None correctly.
        assert True


# ── Signal-forensics watcher (added 2026-05-10) ─────────────────────────


@pytest.mark.skipif(os.name == "nt", reason="POSIX-only sigwaitinfo")
class TestSignalForensicsWatcher:
    """`daemon/daemon_server.py` installs a thread that uses
    ``signal.sigwaitinfo()`` to capture the sender PID of every shutdown
    signal — Python's normal ``signal.signal()`` handler can't see si_pid.

    Without this, every previous round of "Stop Session crashes the
    daemon" debugging hit a dead end: the log only said
    ``Received signal 15`` with no way to identify the killer.  These
    tests prove the function exists and is wired in.
    """

    def test_forensics_helpers_present(self):
        """`daemon_server` must export the helpers we depend on for
        producing the forensic log line (sender cmdline / proc name /
        ppid).  These are pure-Python /proc readers — easy to test."""
        from daemon import daemon_server
        assert callable(getattr(daemon_server, "_cmdline_of", None))
        assert callable(getattr(daemon_server, "_proc_name_of", None))
        assert callable(getattr(daemon_server, "_ppid_of", None))

    def test_proc_helpers_return_safely_for_dead_pid(self):
        """The helpers must not raise for a nonexistent pid — they run
        from inside a signal-handling thread and any exception would
        prevent the forensic log line from being emitted."""
        from daemon import daemon_server
        # A pid value that's guaranteed not to exist (32-bit max).
        dead = 4_294_967_294
        assert isinstance(daemon_server._cmdline_of(dead), str)
        assert isinstance(daemon_server._proc_name_of(dead), str)
        assert isinstance(daemon_server._ppid_of(dead), int)

    def test_install_returns_true_on_posix(self):
        """The installer should report success on POSIX.  We do NOT
        actually call it here (it would block SIGTERM at the test
        process level and leak across the suite), but we can verify
        it's importable and callable."""
        from daemon import daemon_server
        installer = getattr(daemon_server, "_install_signal_forensics_watcher", None)
        assert installer is not None
        assert callable(installer)


# ── /api/restart POSIX detachment ───────────────────────────────────────


@pytest.mark.skipif(os.name == "nt", reason="POSIX-only restart path")
class TestApiRestartPosixDetachment:
    """The /api/restart Linux/macOS path was previously broken because:

    1. The outer ``subprocess.Popen(restart_cmd, shell=True)`` did not
       pass ``start_new_session=True``, so the bash subprocess shared
       the web server's process group.  When the new python instance
       killed port 5050 (the old web), the kill propagated to bash
       through the shared group — bash died before it could spawn the
       replacement python, so the daemon never came back.

    2. Output was redirected to /dev/null, making failures impossible
       to debug.

    These tests guard the fix at the source code level (string match)
    so a future "cleanup" PR can't silently revert the bug back into
    the codebase.
    """

    def _read_main_routes(self) -> str:
        from pathlib import Path
        path = Path(__file__).resolve().parents[1] / "app" / "routes" / "main.py"
        return path.read_text()

    def test_restart_posix_uses_start_new_session(self):
        """Outer Popen on POSIX must pass start_new_session=True so the
        bash subprocess survives the kill of the old web server."""
        src = self._read_main_routes()
        # Find the POSIX restart Popen call.
        assert 'sys.platform in ("darwin", "linux")' in src, (
            "POSIX branch in restart_server() not found — has the file moved?"
        )
        # Locate the Popen invocation following the POSIX branch and
        # confirm it includes start_new_session=True.
        posix_section_start = src.index('sys.platform in ("darwin", "linux")')
        posix_section = src[posix_section_start:posix_section_start + 4000]
        assert "subprocess.Popen(" in posix_section, (
            "Popen call removed from POSIX restart branch"
        )
        assert "start_new_session=True" in posix_section, (
            "POSIX restart Popen no longer detaches with "
            "start_new_session=True — the daemon-restart-fails-on-Linux "
            "regression has returned. Re-add it before merging."
        )

    def test_restart_posix_logs_to_file_not_devnull(self):
        """The bash redirect must point at a log file we can read after
        a failure, not /dev/null.  Without a log we can't debug silent
        breakage in production."""
        src = self._read_main_routes()
        posix_section_start = src.index('sys.platform in ("darwin", "linux")')
        posix_section = src[posix_section_start:posix_section_start + 4000]
        # The bash command's stdout/stderr redirect must mention
        # restart.log (the agreed log filename) and must NOT redirect
        # everything to /dev/null in the launch line.
        assert "restart.log" in posix_section, (
            "POSIX restart no longer logs to logs/restart.log — failures "
            "will be silent again"
        )
        # Specifically: the launched python's redirect must not be
        # >/dev/null 2>&1 (the old broken pattern).  stdin can still be
        # </dev/null — that's just closing the input.
        # The launch line in the bash heredoc starts with `nohup` and
        # ends with `&'`.  Check that line for the bad pattern.
        for line in posix_section.splitlines():
            stripped = line.strip()
            if stripped.startswith('f"nohup') or stripped.startswith('"nohup'):
                # This is the launch line concatenation — the redirect
                # is on a following line in the f-string concat.
                pass
            if ">> \"" in stripped or '>>"' in stripped:
                # Append-redirect to a log file — good.
                break
        else:
            # Fall back to a generic check: must not redirect stdout to
            # /dev/null in the launch payload.
            assert "> /dev/null 2>&1 &" not in posix_section, (
                "POSIX restart still discards stdout/stderr to /dev/null"
            )
