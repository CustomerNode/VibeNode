"""
Tests for ClaudeAgentSDK normalization methods in daemon/backends/claude.py.

This is CRITICAL moved-code testing.  The normalization methods were
extracted from session_manager.py's _process_message (L2422-2790) into
ClaudeAgentSDK during the Phase 2 OOP refactor.  These tests verify that
the extraction preserved behavior exactly -- any regression here means
messages from the Claude SDK are silently dropped, mistyped, or corrupted,
which breaks the entire UI display pipeline.

Sections:
  1. _normalize_message dispatch
  2. _normalize_assistant (TextBlock, ToolUseBlock, ThinkingBlock)
  3. _normalize_user (TextBlock, ToolResultBlock, string content)
  4. _normalize_result (cost, usage, duration, session_id)
  5. _normalize_system (subtype, data)
  6. _normalize_stream_event
  7. Integration: ClaudeAgentSDK instance with real SDK objects
"""

import pytest

# ---------------------------------------------------------------------------
# Guarded SDK imports
# ---------------------------------------------------------------------------
try:
    from claude_code_sdk.types import (
        AssistantMessage,
        UserMessage,
        ResultMessage,
        SystemMessage,
        StreamEvent,
        TextBlock,
        ThinkingBlock,
        ToolUseBlock,
        ToolResultBlock,
    )
    SDK_AVAILABLE = True
except ImportError:
    SDK_AVAILABLE = False

pytestmark = pytest.mark.skipif(
    not SDK_AVAILABLE,
    reason="claude_code_sdk is not installed -- cannot run normalization tests",
)

from daemon.backends.claude import ClaudeAgentSDK
from daemon.backends.messages import VibeNodeMessage, MessageKind, BlockKind


# =========================================================================
# Helpers
# =========================================================================

def _sdk():
    """Create a ClaudeAgentSDK instance for testing."""
    return ClaudeAgentSDK()


# =========================================================================
# Section 1: _normalize_message dispatch
# =========================================================================


class TestNormalizeMessageDispatch:
    """Test that _normalize_message dispatches to the correct handler
    based on message type.

    WHY: If the isinstance() dispatch is wrong, messages silently map to
    the wrong kind (e.g. UserMessage treated as AssistantMessage), which
    corrupts the entire chat display.
    """

    def test_assistant_message_maps_to_assistant(self):
        """AssistantMessage must produce MessageKind.ASSISTANT."""
        sdk = _sdk()
        msg = AssistantMessage(content=[], model="claude-3", parent_tool_use_id=None)
        result = sdk._normalize_message(msg)
        assert result is not None
        assert result.kind == MessageKind.ASSISTANT

    def test_user_message_maps_to_user(self):
        """UserMessage must produce MessageKind.USER."""
        sdk = _sdk()
        msg = UserMessage(content=[], parent_tool_use_id=None)
        result = sdk._normalize_message(msg)
        assert result is not None
        assert result.kind == MessageKind.USER

    def test_result_message_maps_to_result(self):
        """ResultMessage must produce MessageKind.RESULT."""
        sdk = _sdk()
        msg = ResultMessage(
            subtype="result",
            is_error=False,
            total_cost_usd=0.0,
            session_id="s1",
            num_turns=1,
            duration_ms=100,
            duration_api_ms=80,
            usage={},
        )
        result = sdk._normalize_message(msg)
        assert result is not None
        assert result.kind == MessageKind.RESULT

    def test_system_message_maps_to_system(self):
        """SystemMessage must produce MessageKind.SYSTEM."""
        sdk = _sdk()
        msg = SystemMessage(subtype="init", data={"session_id": "s1"})
        result = sdk._normalize_message(msg)
        assert result is not None
        assert result.kind == MessageKind.SYSTEM

    def test_stream_event_maps_to_stream_event(self):
        """StreamEvent must produce MessageKind.STREAM_EVENT."""
        sdk = _sdk()
        msg = StreamEvent(uuid="u1", session_id="s1", event={"type": "progress", "pct": 50})
        result = sdk._normalize_message(msg)
        assert result is not None
        assert result.kind == MessageKind.STREAM_EVENT

    def test_none_input_returns_none(self):
        """None input must return None (not crash).

        WHY: The safe_parse_message patch can yield None for unknown
        message types.  _normalize_message must handle this gracefully.
        """
        sdk = _sdk()
        result = sdk._normalize_message(None)
        assert result is None

    def test_unknown_type_returns_none(self):
        """Unknown message type must return None.

        WHY: Future SDK versions may add new message types.  Unknown
        types must be silently dropped, not crash the session.
        """
        sdk = _sdk()
        result = sdk._normalize_message("not a real message")
        assert result is None

    def test_unknown_object_returns_none(self):
        """An arbitrary object must return None."""
        sdk = _sdk()
        result = sdk._normalize_message(object())
        assert result is None


# =========================================================================
# Section 2: _normalize_assistant
# =========================================================================


class TestNormalizeAssistant:
    """Test AssistantMessage normalization.

    These tests verify that TextBlock, ToolUseBlock, and ThinkingBlock
    content blocks are correctly translated to the VibeNodeMessage format.
    """

    def test_text_block(self):
        """TextBlock produces a block with kind='text' and correct text.

        WHY: Text blocks are the primary output content.  If they are
        dropped or truncated wrong, the user sees no agent response.
        """
        sdk = _sdk()
        msg = AssistantMessage(
            content=[TextBlock(text="Hello world")],
            model="claude-3",
            parent_tool_use_id=None,
        )
        result = sdk._normalize_assistant(msg)
        assert len(result.blocks) == 1
        assert result.blocks[0]["kind"] == BlockKind.TEXT.value
        assert result.blocks[0]["text"] == "Hello world"

    def test_text_block_truncation_at_50k(self):
        """Text blocks are truncated to 50,000 characters.

        WHY: Prevents memory bloat from extremely long responses.
        The 50KB limit protects both the server and WebSocket transport.
        """
        sdk = _sdk()
        long_text = "x" * 60000
        msg = AssistantMessage(
            content=[TextBlock(text=long_text)],
            model="claude-3",
            parent_tool_use_id=None,
        )
        result = sdk._normalize_assistant(msg)
        assert len(result.blocks[0]["text"]) == 50000

    def test_tool_use_block(self):
        """ToolUseBlock produces a block with name, input, and id.

        WHY: Tool use blocks drive the entire tool execution pipeline.
        Missing 'name' breaks tool dispatch, missing 'id' breaks
        tool result correlation.
        """
        sdk = _sdk()
        msg = AssistantMessage(
            content=[ToolUseBlock(id="tu_1", name="Bash", input={"command": "ls"})],
            model="claude-3",
            parent_tool_use_id=None,
        )
        result = sdk._normalize_assistant(msg)
        assert len(result.blocks) == 1
        block = result.blocks[0]
        assert block["kind"] == BlockKind.TOOL_USE.value
        assert block["name"] == "Bash"
        assert block["id"] == "tu_1"
        assert block["input"] == {"command": "ls"}

    def test_tool_use_block_non_dict_input(self):
        """ToolUseBlock with non-dict input defaults to empty dict.

        WHY: Defensive handling for malformed SDK responses.  Non-dict
        input would cause TypeError in downstream code that calls
        input.get().
        """
        sdk = _sdk()
        tu = ToolUseBlock(id="tu_1", name="Bash", input="not a dict")
        msg = AssistantMessage(
            content=[tu], model="claude-3", parent_tool_use_id=None,
        )
        result = sdk._normalize_assistant(msg)
        assert result.blocks[0]["input"] == {}

    def test_thinking_block(self):
        """ThinkingBlock produces a block with kind='thinking'.

        WHY: Thinking blocks are included in the normalized output so
        the UI can show/hide them based on user preference.  Dropping
        them entirely would lose debug information.
        """
        sdk = _sdk()
        msg = AssistantMessage(
            content=[ThinkingBlock(thinking="reasoning...", signature="sig")],
            model="claude-3",
            parent_tool_use_id=None,
        )
        result = sdk._normalize_assistant(msg)
        assert len(result.blocks) == 1
        assert result.blocks[0]["kind"] == BlockKind.THINKING.value

    def test_empty_content_list(self):
        """Empty content list produces empty blocks.

        WHY: Some assistant messages have no content (e.g. partial
        streaming messages).  Must not crash.
        """
        sdk = _sdk()
        msg = AssistantMessage(content=[], model="claude-3", parent_tool_use_id=None)
        result = sdk._normalize_assistant(msg)
        assert result.blocks == []

    def test_mixed_blocks_preserve_order(self):
        """Mixed block types appear in the correct order.

        WHY: Block order matters for UI rendering.  Thinking comes
        before text, tool use appears where the agent placed it.
        """
        sdk = _sdk()
        msg = AssistantMessage(
            content=[
                ThinkingBlock(thinking="let me think", signature="s"),
                TextBlock(text="Here is my plan"),
                ToolUseBlock(id="tu_1", name="Bash", input={"command": "ls"}),
                TextBlock(text="Done"),
            ],
            model="claude-3",
            parent_tool_use_id=None,
        )
        result = sdk._normalize_assistant(msg)
        assert len(result.blocks) == 4
        assert result.blocks[0]["kind"] == BlockKind.THINKING.value
        assert result.blocks[1]["kind"] == BlockKind.TEXT.value
        assert result.blocks[1]["text"] == "Here is my plan"
        assert result.blocks[2]["kind"] == BlockKind.TOOL_USE.value
        assert result.blocks[3]["kind"] == BlockKind.TEXT.value
        assert result.blocks[3]["text"] == "Done"

    def test_raw_preserved(self):
        """The original SDK message is preserved in .raw.

        WHY: Escape hatch for edge cases where normalized form loses
        information.
        """
        sdk = _sdk()
        msg = AssistantMessage(content=[], model="claude-3", parent_tool_use_id=None)
        result = sdk._normalize_assistant(msg)
        assert result.raw is msg


# =========================================================================
# Section 3: _normalize_user
# =========================================================================


class TestNormalizeUser:
    """Test UserMessage normalization.

    User messages contain tool results (ToolResultBlock) which drive
    the tool execution feedback loop.
    """

    def test_text_block_in_content(self):
        """TextBlock in content produces a text block.

        WHY: User text is the primary input mechanism.
        """
        sdk = _sdk()
        msg = UserMessage(
            content=[TextBlock(text="Fix the bug")],
            parent_tool_use_id=None,
        )
        result = sdk._normalize_user(msg)
        assert len(result.blocks) == 1
        assert result.blocks[0]["kind"] == BlockKind.TEXT.value
        assert result.blocks[0]["text"] == "Fix the bug"

    def test_tool_result_with_string_content(self):
        """ToolResultBlock with string content produces tool_result block.

        WHY: Simple tool results are plain strings (e.g. bash stdout).
        """
        sdk = _sdk()
        msg = UserMessage(
            content=[ToolResultBlock(
                tool_use_id="tu_1",
                content="file contents here",
                is_error=False,
            )],
            parent_tool_use_id=None,
        )
        result = sdk._normalize_user(msg)
        assert len(result.blocks) == 1
        block = result.blocks[0]
        assert block["kind"] == BlockKind.TOOL_RESULT.value
        assert block["text"] == "file contents here"
        assert block["tool_use_id"] == "tu_1"
        assert block["is_error"] is False

    def test_tool_result_with_is_error(self):
        """ToolResultBlock with is_error=True sets is_error flag.

        WHY: Error results are rendered differently in the UI (red
        styling, error icon).
        """
        sdk = _sdk()
        msg = UserMessage(
            content=[ToolResultBlock(
                tool_use_id="tu_1",
                content="command failed",
                is_error=True,
            )],
            parent_tool_use_id=None,
        )
        result = sdk._normalize_user(msg)
        assert result.blocks[0]["is_error"] is True

    def test_tool_result_with_list_content_dicts(self):
        """ToolResultBlock with list content extracts text from dicts.

        WHY: Complex tool results contain a list of typed content
        blocks (e.g. [{"type": "text", "text": "..."}]).  The
        normalizer must extract text from these.
        """
        sdk = _sdk()
        msg = UserMessage(
            content=[ToolResultBlock(
                tool_use_id="tu_1",
                content=[
                    {"type": "text", "text": "part 1"},
                    {"type": "text", "text": "part 2"},
                ],
                is_error=False,
            )],
            parent_tool_use_id=None,
        )
        result = sdk._normalize_user(msg)
        assert "part 1" in result.blocks[0]["text"]
        assert "part 2" in result.blocks[0]["text"]

    def test_content_as_string(self):
        """Content as a plain string (not list) is handled.

        WHY: Some SDK versions send content as a string rather than
        a list of blocks.  The normalizer must handle both formats.
        """
        sdk = _sdk()
        msg = UserMessage(content="plain text content", parent_tool_use_id=None)
        result = sdk._normalize_user(msg)
        assert len(result.blocks) == 1
        assert result.blocks[0]["kind"] == BlockKind.TEXT.value
        assert result.blocks[0]["text"] == "plain text content"

    def test_content_as_empty_string(self):
        """Empty string content produces no blocks.

        WHY: Whitespace-only content should not create empty text bubbles.
        """
        sdk = _sdk()
        msg = UserMessage(content="   ", parent_tool_use_id=None)
        result = sdk._normalize_user(msg)
        assert result.blocks == []

    def test_content_is_none(self):
        """None content produces empty blocks.

        WHY: Defensive handling for malformed SDK messages.
        """
        sdk = _sdk()
        msg = UserMessage(content=None, parent_tool_use_id=None)
        result = sdk._normalize_user(msg)
        assert result.blocks == []

    def test_sub_agent_detection(self):
        """parent_tool_use_id set marks the message as sub-agent.

        WHY: Sub-agent messages are rendered differently (no user
        bubble) to avoid confusing the user with messages they didn't
        send.
        """
        sdk = _sdk()
        msg = UserMessage(
            content=[TextBlock(text="sub-agent result")],
            parent_tool_use_id="tu_parent_1",
        )
        result = sdk._normalize_user(msg)
        assert result.is_sub_agent is True

    def test_no_parent_tool_use_id_is_not_sub_agent(self):
        """Missing parent_tool_use_id means this is a normal user message."""
        sdk = _sdk()
        msg = UserMessage(content=[], parent_tool_use_id=None)
        result = sdk._normalize_user(msg)
        assert result.is_sub_agent is False


# =========================================================================
# Section 4: _normalize_result
# =========================================================================


class TestNormalizeResult:
    """Test ResultMessage normalization.

    Result messages carry session cost, usage metrics, and the SDK session
    ID.  Errors here cause incorrect billing display, missing metrics, or
    broken session ID remapping.
    """

    def test_cost_and_session_id(self):
        """cost_usd and session_id are mapped correctly.

        WHY: Cost is shown in the session header.  Session ID is used
        for SDK session remapping (the SDK may assign a different ID
        than the one we started with).
        """
        sdk = _sdk()
        msg = ResultMessage(
            subtype="result",
            is_error=False,
            total_cost_usd=0.0542,
            session_id="sdk-session-123",
            num_turns=3,
            duration_ms=4500,
            duration_api_ms=4000,
            usage={"input_tokens": 100, "output_tokens": 50},
        )
        result = sdk._normalize_result(msg)
        assert result.cost_usd == pytest.approx(0.0542)
        assert result.session_id == "sdk-session-123"

    def test_duration_and_num_turns(self):
        """duration_ms and num_turns are included in the result.

        WHY: These metrics are displayed in the session footer and
        used for performance monitoring.
        """
        sdk = _sdk()
        msg = ResultMessage(
            subtype="result",
            is_error=False,
            total_cost_usd=0.0,
            session_id="s1",
            num_turns=5,
            duration_ms=12345,
            duration_api_ms=10000,
            usage={},
        )
        result = sdk._normalize_result(msg)
        assert result.duration_ms == 12345
        assert result.num_turns == 5

    def test_is_error_flag(self):
        """is_error flag is propagated from the SDK message.

        WHY: Error results trigger different UI behavior (error styling,
        automatic retry logic).
        """
        sdk = _sdk()
        msg = ResultMessage(
            subtype="result",
            is_error=True,
            total_cost_usd=0.0,
            session_id="s1",
            num_turns=0,
            duration_ms=0,
            duration_api_ms=0,
            usage={},
        )
        result = sdk._normalize_result(msg)
        assert result.is_error is True

    def test_usage_dict_mapped(self):
        """Usage dict is correctly mapped from the SDK message.

        WHY: Token counts are used for cost tracking and displayed
        in the session info panel.
        """
        sdk = _sdk()
        usage = {"input_tokens": 1000, "output_tokens": 500}
        msg = ResultMessage(
            subtype="result",
            is_error=False,
            total_cost_usd=0.01,
            session_id="s1",
            num_turns=1,
            duration_ms=100,
            duration_api_ms=80,
            usage=usage,
        )
        result = sdk._normalize_result(msg)
        assert result.usage == usage


# =========================================================================
# Section 5: _normalize_system
# =========================================================================


class TestNormalizeSystem:
    """Test SystemMessage normalization.

    System messages carry initialization data and compact boundaries.
    """

    def test_subtype_extracted(self):
        """Subtype is extracted from the SystemMessage.

        WHY: The subtype determines how SessionManager processes the
        message (e.g. 'init' triggers session setup, 'compact_boundary'
        triggers summary extraction).
        """
        sdk = _sdk()
        msg = SystemMessage(subtype="init", data={"session_id": "s1"})
        result = sdk._normalize_system(msg)
        assert result.subtype == "init"

    def test_data_extracted(self):
        """Data dict is extracted from the SystemMessage.

        WHY: The data payload contains session initialization info,
        model metadata, etc.
        """
        sdk = _sdk()
        msg = SystemMessage(subtype="init", data={"session_id": "s1", "model": "claude-3"})
        result = sdk._normalize_system(msg)
        assert result.data["session_id"] == "s1"
        assert result.data["model"] == "claude-3"

    def test_empty_subtype(self):
        """Missing subtype defaults to empty string."""
        sdk = _sdk()
        msg = SystemMessage(subtype="", data={})
        result = sdk._normalize_system(msg)
        assert result.subtype == ""


# =========================================================================
# Section 6: _normalize_stream_event
# =========================================================================


class TestNormalizeStreamEvent:
    """Test StreamEvent normalization."""

    def test_event_data_captured(self):
        """Event name and data are captured in the result.

        WHY: Stream events drive partial UI updates (progress bars,
        streaming text).
        """
        sdk = _sdk()
        msg = StreamEvent(uuid="u1", session_id="s1", event={"type": "progress", "pct": 75})
        result = sdk._normalize_stream_event(msg)
        assert result.kind == MessageKind.STREAM_EVENT
        assert result.data["event"] == {"type": "progress", "pct": 75}


# =========================================================================
# Section 7: Integration -- ClaudeAgentSDK with real SDK objects
# =========================================================================


class TestClaudeAgentSDKIntegration:
    """Test the ACTUAL ClaudeAgentSDK instance with real SDK objects.

    These integration tests create a ClaudeAgentSDK, feed it real SDK
    message objects, and verify the full normalization pipeline.

    WHY: Unit tests on helper methods in isolation could pass while
    the top-level _normalize_message dispatch is broken.  These tests
    exercise the complete path.
    """

    def test_full_assistant_pipeline(self):
        """Full pipeline: AssistantMessage with mixed blocks.

        WHY: End-to-end test that exercises dispatch + normalization
        together.
        """
        sdk = ClaudeAgentSDK()
        msg = AssistantMessage(
            content=[
                ThinkingBlock(thinking="planning", signature="s"),
                TextBlock(text="I will edit the file"),
                ToolUseBlock(id="tu_1", name="Edit", input={"file_path": "/a.py"}),
            ],
            model="claude-3",
            parent_tool_use_id=None,
        )
        result = sdk._normalize_message(msg)
        assert result.kind == MessageKind.ASSISTANT
        assert len(result.blocks) == 3
        assert result.blocks[0]["kind"] == "thinking"
        assert result.blocks[1]["kind"] == "text"
        assert result.blocks[2]["kind"] == "tool_use"
        assert result.blocks[2]["name"] == "Edit"

    def test_full_user_pipeline_with_tool_result(self):
        """Full pipeline: UserMessage with ToolResultBlock.

        WHY: The tool result -> user message path is the most complex
        normalization path.
        """
        sdk = ClaudeAgentSDK()
        msg = UserMessage(
            content=[ToolResultBlock(
                tool_use_id="tu_1",
                content="File edited successfully",
                is_error=False,
            )],
            parent_tool_use_id=None,
        )
        result = sdk._normalize_message(msg)
        assert result.kind == MessageKind.USER
        assert len(result.blocks) == 1
        assert result.blocks[0]["kind"] == "tool_result"
        assert result.blocks[0]["text"] == "File edited successfully"
        assert result.blocks[0]["is_error"] is False

    def test_full_result_pipeline(self):
        """Full pipeline: ResultMessage with all fields.

        WHY: ResultMessage triggers session completion logic including
        cost tracking and session ID remapping.
        """
        sdk = ClaudeAgentSDK()
        msg = ResultMessage(
            subtype="result",
            is_error=False,
            total_cost_usd=0.25,
            session_id="sdk-123",
            num_turns=10,
            duration_ms=30000,
            duration_api_ms=25000,
            usage={"input_tokens": 5000, "output_tokens": 2000},
        )
        result = sdk._normalize_message(msg)
        assert result.kind == MessageKind.RESULT
        assert result.cost_usd == pytest.approx(0.25)
        assert result.session_id == "sdk-123"
        assert result.num_turns == 10
        assert result.duration_ms == 30000
        assert result.is_error is False

    def test_full_system_pipeline(self):
        """Full pipeline: SystemMessage dispatches correctly."""
        sdk = ClaudeAgentSDK()
        msg = SystemMessage(subtype="init", data={"session_id": "s1"})
        result = sdk._normalize_message(msg)
        assert result.kind == MessageKind.SYSTEM
        assert result.subtype == "init"

    def test_full_stream_event_pipeline(self):
        """Full pipeline: StreamEvent dispatches correctly."""
        sdk = ClaudeAgentSDK()
        msg = StreamEvent(uuid="u2", session_id="s1", event={"type": "heartbeat"})
        result = sdk._normalize_message(msg)
        assert result.kind == MessageKind.STREAM_EVENT
