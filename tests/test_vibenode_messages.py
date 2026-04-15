"""Tests for VibeNodeMessage, MessageKind, and BlockKind.

Validates the backend-agnostic message abstraction layer defined in
daemon/backends/messages.py.
"""

import pytest
from daemon.backends.messages import VibeNodeMessage, MessageKind, BlockKind


# ---------------------------------------------------------------------------
# MessageKind enum
# ---------------------------------------------------------------------------

class TestMessageKind:
    """Verify MessageKind enum has all expected values."""

    def test_all_kinds_present(self):
        expected = {"ASSISTANT", "USER", "SYSTEM", "RESULT", "STREAM_EVENT"}
        actual = {k.name for k in MessageKind}
        assert actual == expected

    def test_values_are_strings(self):
        for kind in MessageKind:
            assert isinstance(kind.value, str)

    def test_assistant_value(self):
        assert MessageKind.ASSISTANT.value == "assistant"

    def test_user_value(self):
        assert MessageKind.USER.value == "user"

    def test_system_value(self):
        assert MessageKind.SYSTEM.value == "system"

    def test_result_value(self):
        assert MessageKind.RESULT.value == "result"

    def test_stream_event_value(self):
        assert MessageKind.STREAM_EVENT.value == "stream_event"


# ---------------------------------------------------------------------------
# BlockKind enum
# ---------------------------------------------------------------------------

class TestBlockKind:
    """Verify BlockKind enum has all expected values."""

    def test_all_kinds_present(self):
        expected = {"TEXT", "TOOL_USE", "TOOL_RESULT", "THINKING"}
        actual = {k.name for k in BlockKind}
        assert actual == expected

    def test_values_are_strings(self):
        for kind in BlockKind:
            assert isinstance(kind.value, str)

    def test_text_value(self):
        assert BlockKind.TEXT.value == "text"

    def test_tool_use_value(self):
        assert BlockKind.TOOL_USE.value == "tool_use"

    def test_tool_result_value(self):
        assert BlockKind.TOOL_RESULT.value == "tool_result"

    def test_thinking_value(self):
        assert BlockKind.THINKING.value == "thinking"


# ---------------------------------------------------------------------------
# VibeNodeMessage creation and defaults
# ---------------------------------------------------------------------------

class TestVibeNodeMessageDefaults:
    """Verify VibeNodeMessage defaults are correct."""

    def test_minimal_creation(self):
        msg = VibeNodeMessage(kind=MessageKind.ASSISTANT)
        assert msg.kind == MessageKind.ASSISTANT
        assert msg.blocks == []
        assert msg.is_sub_agent is False
        assert msg.subtype == ""
        assert msg.data == {}
        assert msg.cost_usd == 0.0
        assert msg.is_error is False
        assert msg.session_id is None
        assert msg.usage == {}
        assert msg.duration_ms == 0
        assert msg.num_turns == 0
        assert msg.raw is None

    def test_blocks_default_is_independent(self):
        """Each instance gets its own list, not a shared mutable default."""
        msg1 = VibeNodeMessage(kind=MessageKind.ASSISTANT)
        msg2 = VibeNodeMessage(kind=MessageKind.ASSISTANT)
        msg1.blocks.append({"kind": "text", "text": "hello"})
        assert msg2.blocks == []

    def test_data_default_is_independent(self):
        """Each instance gets its own dict, not a shared mutable default."""
        msg1 = VibeNodeMessage(kind=MessageKind.SYSTEM)
        msg2 = VibeNodeMessage(kind=MessageKind.SYSTEM)
        msg1.data["key"] = "value"
        assert msg2.data == {}

    def test_usage_default_is_independent(self):
        """Each instance gets its own dict, not a shared mutable default."""
        msg1 = VibeNodeMessage(kind=MessageKind.RESULT)
        msg2 = VibeNodeMessage(kind=MessageKind.RESULT)
        msg1.usage["input_tokens"] = 100
        assert msg2.usage == {}


# ---------------------------------------------------------------------------
# VibeNodeMessage with all message kinds
# ---------------------------------------------------------------------------

class TestVibeNodeMessageKinds:
    """Test creating VibeNodeMessage with each MessageKind."""

    def test_assistant_message(self):
        msg = VibeNodeMessage(
            kind=MessageKind.ASSISTANT,
            blocks=[
                {"kind": BlockKind.TEXT.value, "text": "Hello, world!"},
            ],
        )
        assert msg.kind == MessageKind.ASSISTANT
        assert len(msg.blocks) == 1
        assert msg.blocks[0]["kind"] == "text"
        assert msg.blocks[0]["text"] == "Hello, world!"

    def test_user_message(self):
        msg = VibeNodeMessage(
            kind=MessageKind.USER,
            blocks=[
                {"kind": BlockKind.TEXT.value, "text": "Fix the bug"},
            ],
            is_sub_agent=False,
        )
        assert msg.kind == MessageKind.USER
        assert msg.is_sub_agent is False

    def test_user_message_sub_agent(self):
        msg = VibeNodeMessage(
            kind=MessageKind.USER,
            is_sub_agent=True,
        )
        assert msg.is_sub_agent is True

    def test_system_message(self):
        msg = VibeNodeMessage(
            kind=MessageKind.SYSTEM,
            subtype="compact_boundary",
            data={"compactMetadata": {"preTokens": 50000}},
        )
        assert msg.kind == MessageKind.SYSTEM
        assert msg.subtype == "compact_boundary"
        assert msg.data["compactMetadata"]["preTokens"] == 50000

    def test_result_message(self):
        msg = VibeNodeMessage(
            kind=MessageKind.RESULT,
            cost_usd=0.0342,
            is_error=False,
            session_id="abc-123",
            usage={"input_tokens": 1000, "output_tokens": 500},
            duration_ms=4200,
            num_turns=3,
        )
        assert msg.kind == MessageKind.RESULT
        assert msg.cost_usd == pytest.approx(0.0342)
        assert msg.is_error is False
        assert msg.session_id == "abc-123"
        assert msg.usage["input_tokens"] == 1000
        assert msg.duration_ms == 4200
        assert msg.num_turns == 3

    def test_result_message_with_error(self):
        msg = VibeNodeMessage(
            kind=MessageKind.RESULT,
            is_error=True,
        )
        assert msg.is_error is True

    def test_stream_event_message(self):
        msg = VibeNodeMessage(
            kind=MessageKind.STREAM_EVENT,
            data={"event": "message_start", "data": {"type": "message_start"}},
        )
        assert msg.kind == MessageKind.STREAM_EVENT
        assert msg.data["event"] == "message_start"


# ---------------------------------------------------------------------------
# VibeNodeMessage with blocks containing all block kinds
# ---------------------------------------------------------------------------

class TestVibeNodeMessageBlocks:
    """Test VibeNodeMessage with all block kinds."""

    def test_text_block(self):
        block = {"kind": BlockKind.TEXT.value, "text": "Hello"}
        msg = VibeNodeMessage(kind=MessageKind.ASSISTANT, blocks=[block])
        assert msg.blocks[0]["kind"] == "text"
        assert msg.blocks[0]["text"] == "Hello"

    def test_tool_use_block(self):
        block = {
            "kind": BlockKind.TOOL_USE.value,
            "name": "Edit",
            "id": "tu_123",
            "input": {"file_path": "/foo/bar.py", "old_string": "x", "new_string": "y"},
        }
        msg = VibeNodeMessage(kind=MessageKind.ASSISTANT, blocks=[block])
        assert msg.blocks[0]["kind"] == "tool_use"
        assert msg.blocks[0]["name"] == "Edit"
        assert msg.blocks[0]["id"] == "tu_123"
        assert msg.blocks[0]["input"]["file_path"] == "/foo/bar.py"

    def test_tool_result_block(self):
        block = {
            "kind": BlockKind.TOOL_RESULT.value,
            "text": "File edited successfully",
            "tool_use_id": "tu_123",
            "is_error": False,
        }
        msg = VibeNodeMessage(kind=MessageKind.USER, blocks=[block])
        assert msg.blocks[0]["kind"] == "tool_result"
        assert msg.blocks[0]["text"] == "File edited successfully"
        assert msg.blocks[0]["tool_use_id"] == "tu_123"
        assert msg.blocks[0]["is_error"] is False

    def test_tool_result_block_with_error(self):
        block = {
            "kind": BlockKind.TOOL_RESULT.value,
            "text": "Stream closed",
            "tool_use_id": "tu_456",
            "is_error": True,
        }
        msg = VibeNodeMessage(kind=MessageKind.USER, blocks=[block])
        assert msg.blocks[0]["is_error"] is True

    def test_thinking_block(self):
        block = {"kind": BlockKind.THINKING.value}
        msg = VibeNodeMessage(kind=MessageKind.ASSISTANT, blocks=[block])
        assert msg.blocks[0]["kind"] == "thinking"

    def test_mixed_blocks(self):
        """An assistant message can have text, tool_use, and thinking blocks."""
        blocks = [
            {"kind": BlockKind.THINKING.value},
            {"kind": BlockKind.TEXT.value, "text": "I will edit the file."},
            {"kind": BlockKind.TOOL_USE.value, "name": "Edit", "id": "tu_1", "input": {}},
        ]
        msg = VibeNodeMessage(kind=MessageKind.ASSISTANT, blocks=blocks)
        assert len(msg.blocks) == 3
        assert msg.blocks[0]["kind"] == "thinking"
        assert msg.blocks[1]["kind"] == "text"
        assert msg.blocks[2]["kind"] == "tool_use"

    def test_raw_escape_hatch(self):
        """The raw field preserves the original SDK message object."""
        sentinel = object()
        msg = VibeNodeMessage(kind=MessageKind.ASSISTANT, raw=sentinel)
        assert msg.raw is sentinel
