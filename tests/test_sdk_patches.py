"""Tests for SDK monkey-patches.

Validates that:
1. Structural assertions pass for the installed SDK version
2. Safe parse_message returns None for unknown message types
3. Transport adapter correctly reformats permission responses
4. Transport adapter suppresses end_input when keep_stdin_open=True
5. Transport adapter delegates all other methods unchanged
"""

import json
import pytest
from unittest.mock import AsyncMock, MagicMock, patch

# Apply patches before any tests run (mirrors what session_manager does at import)
from daemon.sdk_patches import apply_patches as _apply_patches
_apply_patches()


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

    @pytest.mark.asyncio
    async def test_reformat_allow_response(self, adapter, mock_transport):
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
        await adapter.write(json.dumps(sdk_response) + "\n")

        assert len(mock_transport.written) == 1
        written = json.loads(mock_transport.written[0])
        inner = written["response"]["response"]
        assert inner == {"behavior": "allow", "updatedInput": {"command": "ls"}}

    @pytest.mark.asyncio
    async def test_reformat_allow_no_input(self, adapter, mock_transport):
        """Allow without input should default to empty updatedInput."""
        sdk_response = {
            "type": "control_response",
            "response": {
                "subtype": "success",
                "request_id": "req-2",
                "response": {"allow": True},
            },
        }
        await adapter.write(json.dumps(sdk_response) + "\n")

        written = json.loads(mock_transport.written[0])
        inner = written["response"]["response"]
        assert inner == {"behavior": "allow", "updatedInput": {}}

    @pytest.mark.asyncio
    async def test_reformat_deny_response(self, adapter, mock_transport):
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
        await adapter.write(json.dumps(sdk_response) + "\n")

        written = json.loads(mock_transport.written[0])
        inner = written["response"]["response"]
        assert inner == {"behavior": "deny", "message": "User denied"}

    @pytest.mark.asyncio
    async def test_deny_default_message(self, adapter, mock_transport):
        """Deny without reason should default to 'Denied'."""
        sdk_response = {
            "type": "control_response",
            "response": {
                "subtype": "success",
                "request_id": "req-4",
                "response": {"allow": False},
            },
        }
        await adapter.write(json.dumps(sdk_response) + "\n")

        written = json.loads(mock_transport.written[0])
        inner = written["response"]["response"]
        assert inner == {"behavior": "deny", "message": "Denied"}

    @pytest.mark.asyncio
    async def test_non_permission_passthrough(self, adapter, mock_transport):
        """Non-permission messages should pass through unchanged."""
        msg = {"type": "user", "message": {"role": "user", "content": "hello"}}
        data = json.dumps(msg) + "\n"
        await adapter.write(data)

        assert mock_transport.written[0] == data

    @pytest.mark.asyncio
    async def test_error_response_passthrough(self, adapter, mock_transport):
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
        await adapter.write(data)

        assert mock_transport.written[0] == data

    @pytest.mark.asyncio
    async def test_malformed_json_passthrough(self, adapter, mock_transport):
        """Malformed JSON should pass through unchanged."""
        data = "not json at all\n"
        await adapter.write(data)
        assert mock_transport.written[0] == data

    # -- end_input behavior --

    @pytest.mark.asyncio
    async def test_end_input_passes_through_by_default(self, adapter, mock_transport):
        """Without keep_stdin_open, end_input should delegate."""
        await adapter.end_input()
        assert mock_transport.input_ended is True

    @pytest.mark.asyncio
    async def test_end_input_suppressed_when_keep_open(
        self, adapter_keep_open, mock_transport
    ):
        """With keep_stdin_open=True, end_input should be suppressed."""
        await adapter_keep_open.end_input()
        assert mock_transport.input_ended is False

    # -- delegation --

    @pytest.mark.asyncio
    async def test_connect_delegates(self, adapter, mock_transport):
        await adapter.connect()
        assert mock_transport.connected is True

    @pytest.mark.asyncio
    async def test_close_delegates(self, adapter, mock_transport):
        await adapter.close()
        assert mock_transport.closed is True

    def test_is_ready_delegates(self, adapter, mock_transport):
        assert adapter.is_ready() is True
        mock_transport._ready = False
        assert adapter.is_ready() is False

    def test_inner_property(self, adapter, mock_transport):
        """inner property should expose the underlying transport."""
        assert adapter.inner is mock_transport


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
