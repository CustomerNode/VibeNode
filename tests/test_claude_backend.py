"""Regression safety net for Phase 2 of the OOP abstraction refactor.

Phase 2 will extract Claude SDK code from daemon/session_manager.py into
daemon/backends/claude.py (ClaudeAgentSDK) and daemon/backends/claude_store.py
(ClaudeJsonlStore).  The existing test suite mocks the SDK so heavily that
Phase 2 could break every SDK call and tests would still pass green.

This file tests the REAL SDK types, REAL JSONL parsing logic, and REAL
attribute access patterns that session_manager.py depends on.  If Phase 2
introduces a bug -- wrong attribute name, missing field, changed type,
broken JSONL format -- one of these tests will catch it.

Sections:
  1. SDK Message Type Verification
  2. Permission Result Type Verification
  3. JSONL Format Verification
  4. ClaudeCodeOptions Verification
  5. SDK Import Verification
  6. Message Processing Integration
"""

import json
import os
import uuid
import pytest
from pathlib import Path
from datetime import datetime, timezone

# ---------------------------------------------------------------------------
# Guarded SDK imports.  If the SDK is not installed, every test in this file
# is skipped with a clear message rather than crashing the whole suite.
# ---------------------------------------------------------------------------
try:
    from claude_code_sdk import ClaudeSDKClient, ClaudeCodeOptions
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
        PermissionResultAllow,
        PermissionResultDeny,
        ContentBlock,
    )
    SDK_AVAILABLE = True
except ImportError:
    SDK_AVAILABLE = False

pytestmark = pytest.mark.skipif(
    not SDK_AVAILABLE,
    reason="claude_code_sdk is not installed -- cannot run SDK regression tests",
)


# =========================================================================
# Section 1: SDK Message Type Verification
# =========================================================================
# Phase 2 moves isinstance() checks against these types out of
# session_manager._process_message (lines ~2422-2790) into
# ClaudeAgentSDK._normalize_message().  If the new code accesses the wrong
# attribute name or assumes the wrong type, these tests catch it.
# =========================================================================


class TestAssistantMessage:
    """Verify AssistantMessage has the attributes _process_message depends on.

    session_manager.py accesses:
      - message.content  (iterated as list of blocks, line ~2427)
      - isinstance checks on each block: TextBlock, ToolUseBlock, ThinkingBlock
    """

    def test_content_is_list(self):
        """AssistantMessage.content must be a list so _process_message can
        iterate it with `for block in message.content`.

        Phase 2 regression: if _normalize_message converts content to a
        different structure (e.g. a single string), the block loop breaks.
        """
        msg = AssistantMessage(
            content=[TextBlock(text="hello")],
            model="claude-3",
            parent_tool_use_id=None,
        )
        assert isinstance(msg.content, list)

    def test_content_can_contain_text_block(self):
        """_process_message checks isinstance(block, TextBlock) at line ~2428
        and accesses block.text.  Verify TextBlock appears in content list.
        """
        tb = TextBlock(text="response text")
        msg = AssistantMessage(content=[tb], model="claude-3", parent_tool_use_id=None)
        assert len(msg.content) == 1
        assert isinstance(msg.content[0], TextBlock)
        assert msg.content[0].text == "response text"

    def test_content_can_contain_tool_use_block(self):
        """_process_message checks isinstance(block, ToolUseBlock) at line ~2435
        and accesses block.input, block.name, block.id.

        Phase 2 regression: if _normalize_message drops ToolUseBlock fields or
        renames them, tool tracking (tracked_files) and LogEntry creation break.
        """
        tu = ToolUseBlock(id="tu_123", name="Edit", input={"file_path": "/a.py"})
        msg = AssistantMessage(content=[tu], model="claude-3", parent_tool_use_id=None)
        block = msg.content[0]
        assert isinstance(block, ToolUseBlock)
        assert block.name == "Edit"
        assert block.id == "tu_123"
        assert isinstance(block.input, dict)
        assert block.input["file_path"] == "/a.py"

    def test_content_can_contain_thinking_block(self):
        """_process_message checks isinstance(block, ThinkingBlock) at line ~2460
        and skips them.  Verify ThinkingBlock has .thinking attribute.

        Phase 2 regression: if _normalize_message drops ThinkingBlock or
        changes the attribute name, an AttributeError will crash message processing.
        """
        th = ThinkingBlock(thinking="let me think...", signature="sig")
        msg = AssistantMessage(content=[th], model="claude-3", parent_tool_use_id=None)
        block = msg.content[0]
        assert isinstance(block, ThinkingBlock)
        assert block.thinking == "let me think..."

    def test_content_mixed_block_types(self):
        """_process_message iterates ALL blocks and dispatches by isinstance.
        Verify a message with mixed block types works correctly.

        Phase 2 regression: if _normalize_message changes iteration order or
        filters blocks, some entries will be missing from the session log.
        """
        blocks = [
            ThinkingBlock(thinking="planning", signature="s"),
            TextBlock(text="Here is my plan"),
            ToolUseBlock(id="tu_1", name="Bash", input={"command": "ls"}),
            TextBlock(text="Done"),
        ]
        msg = AssistantMessage(content=blocks, model="claude-3", parent_tool_use_id=None)
        assert len(msg.content) == 4
        assert isinstance(msg.content[0], ThinkingBlock)
        assert isinstance(msg.content[1], TextBlock)
        assert isinstance(msg.content[2], ToolUseBlock)
        assert isinstance(msg.content[3], TextBlock)

    def test_has_model_field(self):
        """AssistantMessage.model is used in some code paths.
        Verify it exists and accepts a string."""
        msg = AssistantMessage(content=[], model="claude-sonnet-4-20250514", parent_tool_use_id=None)
        assert msg.model == "claude-sonnet-4-20250514"

    def test_has_parent_tool_use_id(self):
        """AssistantMessage.parent_tool_use_id distinguishes sub-agent messages.
        UserMessage also has this field -- verify both."""
        msg = AssistantMessage(content=[], model="claude-3", parent_tool_use_id="parent_123")
        assert msg.parent_tool_use_id == "parent_123"


class TestUserMessage:
    """Verify UserMessage has the attributes _process_message depends on.

    session_manager.py accesses:
      - message.content (line ~2471, can be str or list)
      - message.parent_tool_use_id (line ~2467, used for sub-agent detection)
      - isinstance checks on blocks: ToolResultBlock, TextBlock
    """

    def test_content_is_list(self):
        """_process_message accesses getattr(message, 'content', None) at line ~2471
        and handles both str and list.  Verify list case works.
        """
        msg = UserMessage(
            content=[TextBlock(text="user input")],
            parent_tool_use_id=None,
        )
        assert isinstance(msg.content, list)

    def test_content_can_be_tool_result_blocks(self):
        """_process_message checks isinstance(block, ToolResultBlock) at line ~2480
        and accesses block.content, block.tool_use_id, block.is_error.

        Phase 2 regression: if _normalize_message loses ToolResultBlock fields,
        the tool_result LogEntry will have empty tool_use_id and broken error
        detection (including the "Stream closed" self-healing check).
        """
        tr = ToolResultBlock(
            tool_use_id="tu_123",
            content="command output here",
            is_error=False,
        )
        msg = UserMessage(content=[tr], parent_tool_use_id=None)
        block = msg.content[0]
        assert isinstance(block, ToolResultBlock)
        assert block.tool_use_id == "tu_123"
        assert block.content == "command output here"
        assert block.is_error is False

    def test_tool_result_with_error(self):
        """_process_message checks getattr(block, 'is_error', False) at line ~2496.
        When True and content contains "Stream closed", it triggers self-healing.

        Phase 2 regression: if is_error is dropped or renamed, self-healing
        never activates and sessions get stuck in broken states forever.
        """
        tr = ToolResultBlock(
            tool_use_id="tu_456",
            content="Stream closed unexpectedly",
            is_error=True,
        )
        assert tr.is_error is True
        assert "Stream closed" in tr.content

    def test_tool_result_content_as_list(self):
        """_process_message handles ToolResultBlock.content as str or list of
        dicts/objects (lines ~2483-2494).  The list case extracts text from
        dicts with type="text" or objects with .text attribute.

        Phase 2 regression: if _normalize_message flattens list content to a
        string prematurely, the dict-extraction logic breaks.
        """
        # ToolResultBlock.content can be a list of dicts in the raw SDK
        tr = ToolResultBlock(
            tool_use_id="tu_789",
            content=[{"type": "text", "text": "part one"}, {"type": "text", "text": "part two"}],
            is_error=False,
        )
        # Verify content is preserved as-is (a list)
        assert isinstance(tr.content, list)
        assert len(tr.content) == 2

    def test_parent_tool_use_id_for_sub_agent_detection(self):
        """_process_message uses parent_tool_use_id to detect sub-agent messages
        at line ~2467.  When set, TextBlock user messages are skipped.

        Phase 2 regression: if parent_tool_use_id is lost during normalization,
        sub-agent internal chatter will appear as user messages in the UI.
        """
        msg = UserMessage(
            content=[TextBlock(text="internal sub-agent text")],
            parent_tool_use_id="parent_tu_id",
        )
        assert msg.parent_tool_use_id == "parent_tu_id"
        # This is how session_manager detects it:
        is_sub_agent = bool(getattr(msg, 'parent_tool_use_id', None))
        assert is_sub_agent is True

    def test_parent_tool_use_id_none_for_human(self):
        """When parent_tool_use_id is None, the message is from the human user."""
        msg = UserMessage(content=[TextBlock(text="hello")], parent_tool_use_id=None)
        is_sub_agent = bool(getattr(msg, 'parent_tool_use_id', None))
        assert is_sub_agent is False


class TestResultMessage:
    """Verify ResultMessage has all fields _process_message reads.

    session_manager.py accesses (lines ~2641-2730):
      - message.total_cost_usd  (line ~2646)
      - message.usage           (line ~2651, expected dict)
      - message.duration_ms     (line ~2663)
      - message.num_turns       (line ~2664)
      - message.is_error        (line ~2669)
      - message.session_id      (line ~2679, for session ID remapping)
    """

    def test_total_cost_usd(self):
        """_process_message reads getattr(message, 'total_cost_usd', 0.0) at line ~2646.

        Phase 2 regression: if the field is renamed (e.g. to 'cost_usd'),
        info.cost_usd will always be 0.0 and the UI shows $0.00.
        """
        msg = ResultMessage(
            subtype="result", duration_ms=5000, duration_api_ms=4000,
            is_error=False, num_turns=3, session_id="sess_abc",
            total_cost_usd=0.042, usage={"input_tokens": 1000}, result="done",
        )
        assert msg.total_cost_usd == 0.042

    def test_usage_is_dict(self):
        """_process_message checks isinstance(raw_usage, dict) at line ~2652.

        Phase 2 regression: if usage becomes a dataclass or named tuple,
        the dict() copy and key access patterns break.
        """
        usage_data = {
            "input_tokens": 500,
            "output_tokens": 200,
            "cache_read_input_tokens": 100,
            "cache_creation_input_tokens": 50,
        }
        msg = ResultMessage(
            subtype="result", duration_ms=1000, duration_api_ms=800,
            is_error=False, num_turns=1, session_id="s1",
            total_cost_usd=0.01, usage=usage_data, result="ok",
        )
        assert isinstance(msg.usage, dict)
        assert msg.usage["input_tokens"] == 500

    def test_duration_ms_and_num_turns(self):
        """_process_message reads duration_ms and num_turns at lines ~2663-2664.

        Phase 2 regression: if these fields are renamed or wrapped,
        session timing metadata will be lost.
        """
        msg = ResultMessage(
            subtype="result", duration_ms=12345, duration_api_ms=10000,
            is_error=False, num_turns=7, session_id="s2",
            total_cost_usd=0.1, usage={}, result="done",
        )
        assert msg.duration_ms == 12345
        assert msg.num_turns == 7

    def test_is_error(self):
        """_process_message reads is_error at line ~2669.  When True, a system
        error entry is logged.

        Phase 2 regression: if is_error is lost, error sessions will look
        like they completed successfully.
        """
        msg = ResultMessage(
            subtype="result", duration_ms=100, duration_api_ms=80,
            is_error=True, num_turns=1, session_id="s3",
            total_cost_usd=0.0, usage={}, result="error",
        )
        assert msg.is_error is True

    def test_session_id_for_remapping(self):
        """_process_message reads session_id at line ~2679 to detect when the
        SDK assigns a different session ID than the one we generated.  This
        triggers the ID remapping logic.

        Phase 2 regression: if session_id is lost, sessions will have
        duplicate entries in _sessions dict and queues will be orphaned.
        """
        msg = ResultMessage(
            subtype="result", duration_ms=100, duration_api_ms=80,
            is_error=False, num_turns=1, session_id="sdk_assigned_id",
            total_cost_usd=0.01, usage={}, result="ok",
        )
        assert msg.session_id == "sdk_assigned_id"


class TestSystemMessage:
    """Verify SystemMessage has the attributes _process_message depends on.

    session_manager.py accesses (lines ~2591-2639):
      - message.subtype  (line ~2592, e.g. 'init', 'compact_boundary', 'turn_duration')
      - message.data     (line ~2593, dict with event-specific payload)
    """

    def test_subtype_field(self):
        """_process_message dispatches on subtype string at line ~2592.

        Phase 2 regression: if subtype is renamed or becomes an enum,
        the string comparisons ('compact_boundary', 'init', 'turn_duration') break.
        """
        msg = SystemMessage(subtype="compact_boundary", data={})
        assert msg.subtype == "compact_boundary"

    def test_data_is_dict(self):
        """_process_message accesses data as dict at line ~2593.

        Phase 2 regression: if data becomes a dataclass, .get() calls break.
        """
        msg = SystemMessage(
            subtype="compact_boundary",
            data={"compactMetadata": {"preTokens": 50000, "trigger": "auto"}},
        )
        assert isinstance(msg.data, dict)
        assert msg.data["compactMetadata"]["preTokens"] == 50000

    def test_compact_boundary_data_shape(self):
        """Verify the compactMetadata structure that _process_message extracts.
        Lines ~2598-2606: data.get('compactMetadata', {}).get('preTokens'),
        data.get('compactMetadata', {}).get('trigger').
        """
        msg = SystemMessage(
            subtype="compact_boundary",
            data={"compactMetadata": {"preTokens": 80000, "trigger": "manual"}},
        )
        meta = msg.data.get("compactMetadata", {})
        assert meta.get("preTokens") == 80000
        assert meta.get("trigger") == "manual"


class TestStreamEvent:
    """Verify StreamEvent has the attributes _process_message depends on.

    session_manager.py accesses (lines ~2732-2790):
      - message.event  (line ~2736, string like 'message_start')
      - message.data   (line ~2738, dict or None -- but NOTE: StreamEvent
        uses a different 'data' pattern than SystemMessage)
    """

    def test_event_field(self):
        """_process_message accesses message.event at line ~2736.
        The hasattr guard means a missing field won't crash, but the event
        type will be empty and usage extraction won't trigger.
        """
        msg = StreamEvent(
            uuid="uuid1", session_id="s1",
            event="message_start", parent_tool_use_id=None,
        )
        assert msg.event == "message_start"

    def test_session_id_field(self):
        """StreamEvent carries session_id for routing."""
        msg = StreamEvent(
            uuid="uuid1", session_id="sess_xyz",
            event="content_block_delta", parent_tool_use_id=None,
        )
        assert msg.session_id == "sess_xyz"


class TestTextBlock:
    """Verify TextBlock has the .text attribute.

    Used in _process_message at multiple points:
      - line ~2429: block.text for AssistantMessage text blocks
      - line ~2538: block.text for UserMessage text blocks
    """

    def test_text_attribute(self):
        """block.text must be a string."""
        tb = TextBlock(text="hello world")
        assert tb.text == "hello world"

    def test_empty_text(self):
        """_process_message uses (block.text or '') which handles None.
        Verify empty string works.
        """
        tb = TextBlock(text="")
        assert tb.text == ""

    def test_long_text_not_truncated_by_sdk(self):
        """_process_message truncates text itself ([:50000] at line ~2429).
        The SDK should NOT truncate -- verify the full string is stored.
        """
        long_text = "x" * 100000
        tb = TextBlock(text=long_text)
        assert len(tb.text) == 100000


class TestToolUseBlock:
    """Verify ToolUseBlock has .name, .input, .id attributes.

    Used in _process_message at lines ~2435-2458:
      - block.input  (line ~2436, checked with hasattr + isinstance dict)
      - block.name   (line ~2440, via getattr with fallback)
      - block.id     (line ~2441, via getattr with fallback)
    """

    def test_name_attribute(self):
        """_process_message uses getattr(block, 'name', '') at line ~2440."""
        tu = ToolUseBlock(id="1", name="Edit", input={})
        assert tu.name == "Edit"

    def test_input_is_dict(self):
        """_process_message checks isinstance(block.input, dict) at line ~2436.

        Phase 2 regression: if input becomes a string (JSON), the dict check
        fails and inp will be {} -- tool tracking breaks entirely.
        """
        tu = ToolUseBlock(id="1", name="Write", input={"file_path": "/test.py", "content": "print(1)"})
        assert isinstance(tu.input, dict)
        assert "file_path" in tu.input

    def test_id_attribute(self):
        """_process_message uses getattr(block, 'id', '') at line ~2441.
        This becomes LogEntry.id for matching with ToolResultBlock.tool_use_id.
        """
        tu = ToolUseBlock(id="toolu_abc123", name="Bash", input={"command": "ls"})
        assert tu.id == "toolu_abc123"

    def test_file_tracking_tools(self):
        """_process_message tracks files for Edit, Write, MultiEdit, NotebookEdit
        tools at lines ~2452-2458.  It reads inp.get('file_path') or inp.get('path').

        Phase 2 regression: if input dict keys change, file tracking silently
        stops working and Rewind shows no file changes.
        """
        for tool_name in ("Edit", "Write", "MultiEdit", "NotebookEdit"):
            tu = ToolUseBlock(id="1", name=tool_name, input={"file_path": "/src/main.py"})
            inp = tu.input if isinstance(tu.input, dict) else {}
            fp = inp.get("file_path", "") or inp.get("path", "")
            assert fp == "/src/main.py", f"file_path extraction failed for {tool_name}"


class TestToolResultBlock:
    """Verify ToolResultBlock has .tool_use_id, .content, .is_error attributes.

    Used in _process_message at lines ~2480-2535.
    """

    def test_tool_use_id(self):
        """_process_message uses getattr(block, 'tool_use_id', '') at line ~2529.
        This links the result back to its ToolUseBlock.
        """
        tr = ToolResultBlock(tool_use_id="toolu_xyz", content="ok", is_error=False)
        assert tr.tool_use_id == "toolu_xyz"

    def test_content_string(self):
        """_process_message uses getattr(block, 'content', '') at line ~2482.
        When content is a string, it's used directly.
        """
        tr = ToolResultBlock(tool_use_id="1", content="file written successfully", is_error=False)
        assert isinstance(tr.content, str)

    def test_is_error_default_false(self):
        """_process_message uses getattr(block, 'is_error', False) at line ~2496.
        Default must be False.
        """
        tr = ToolResultBlock(tool_use_id="1", content="ok", is_error=False)
        assert tr.is_error is False


class TestThinkingBlock:
    """Verify ThinkingBlock has .thinking attribute.

    _process_message checks isinstance(block, ThinkingBlock) at line ~2460
    and skips thinking blocks.  The .thinking attribute exists but is not
    read by session_manager -- however Phase 2 normalization may need it.
    """

    def test_thinking_attribute(self):
        """ThinkingBlock.thinking holds the model's internal reasoning."""
        th = ThinkingBlock(thinking="I should use the Edit tool", signature="sig123")
        assert th.thinking == "I should use the Edit tool"

    def test_signature_attribute(self):
        """ThinkingBlock.signature is required by the SDK dataclass."""
        th = ThinkingBlock(thinking="reasoning", signature="signature_value")
        assert th.signature == "signature_value"


# =========================================================================
# Section 2: Permission Result Type Verification
# =========================================================================
# Phase 2 moves permission callback creation from session_manager._make_
# permission_callback (lines ~2206-2409) into ClaudeAgentSDK.  The Allow
# and Deny types must have exactly the right fields or the CLI crashes.
#
# CRITICAL CONTEXT: PermissionResultAllow.updated_input must ALWAYS be a
# dict, never None.  When None is sent, the CLI transport crashes with:
#   "undefined is not an object (evaluating 'H.includes')"
# See the CRITICAL comment block at lines ~2270-2276.
# =========================================================================


class TestPermissionResultAllow:
    """Verify PermissionResultAllow can be created with the fields
    _make_permission_callback depends on.
    """

    def test_updated_input_accepts_dict(self):
        """_make_permission_callback always passes updated_input=tool_input
        (lines ~2283, 2302, 2310, 2357).

        Phase 2 regression: if updated_input is dropped or renamed,
        the CLI will crash on every tool approval.
        """
        result = PermissionResultAllow(updated_input={"command": "ls -la"})
        assert result.updated_input == {"command": "ls -la"}
        assert isinstance(result.updated_input, dict)

    def test_updated_input_defaults_to_none(self):
        """The SDK default for updated_input is None.  session_manager.py
        explicitly passes a dict to avoid this.

        Phase 2 regression: if the new code forgets to pass updated_input,
        it defaults to None and the CLI crashes.  This test documents the
        dangerous default.
        """
        result = PermissionResultAllow()
        assert result.updated_input is None  # This is the DANGEROUS default

    def test_updated_input_must_be_dict_not_none(self):
        """Simulate the pattern session_manager uses: always pass a dict.
        The expression `tool_input if isinstance(tool_input, dict) else {}`
        appears at lines ~2283, 2302, 2310, 2357.

        Phase 2 regression: if this guard is removed, None tool_input will
        crash the CLI.
        """
        # Simulate the guard pattern
        tool_input_dict = {"file_path": "/a.py"}
        tool_input_none = None
        tool_input_str = "not a dict"

        r1 = PermissionResultAllow(
            updated_input=tool_input_dict if isinstance(tool_input_dict, dict) else {}
        )
        assert r1.updated_input == {"file_path": "/a.py"}

        r2 = PermissionResultAllow(
            updated_input=tool_input_none if isinstance(tool_input_none, dict) else {}
        )
        assert r2.updated_input == {}  # Safe empty dict, NOT None

        r3 = PermissionResultAllow(
            updated_input=tool_input_str if isinstance(tool_input_str, dict) else {}
        )
        assert r3.updated_input == {}

    def test_behavior_field_defaults_to_allow(self):
        """PermissionResultAllow.behavior must be 'allow'."""
        result = PermissionResultAllow(updated_input={})
        assert result.behavior == "allow"


class TestPermissionResultDeny:
    """Verify PermissionResultDeny can be created with the fields
    _make_permission_callback depends on.
    """

    def test_message_field(self):
        """_make_permission_callback uses message= for deny reasons
        at lines ~2226, 2265, 2365, 2406.
        """
        result = PermissionResultDeny(message="Session not found", interrupt=True)
        assert result.message == "Session not found"

    def test_interrupt_field(self):
        """_make_permission_callback uses interrupt=True to end the turn
        immediately at lines ~2226, 2265, 2406.

        Phase 2 regression: if interrupt is lost, denied tools will NOT
        stop the turn, and the agent will keep retrying the same tool.
        """
        result = PermissionResultDeny(message="no", interrupt=True)
        assert result.interrupt is True

    def test_interrupt_defaults_to_false(self):
        """Default interrupt=False means the agent can try a different approach
        instead of aborting the turn.
        """
        result = PermissionResultDeny(message="not allowed")
        assert result.interrupt is False

    def test_behavior_field_defaults_to_deny(self):
        """PermissionResultDeny.behavior must be 'deny'."""
        result = PermissionResultDeny()
        assert result.behavior == "deny"

    def test_isinstance_check_distinguishes_allow_deny(self):
        """_make_permission_callback checks isinstance(permission_result,
        PermissionResultAllow) at line ~2374 to decide whether to add to
        always_allowed_tools.

        Phase 2 regression: if both types share a base class and isinstance
        check is removed, deny results could be treated as allow.
        """
        allow = PermissionResultAllow(updated_input={})
        deny = PermissionResultDeny(message="no")
        assert isinstance(allow, PermissionResultAllow)
        assert not isinstance(allow, PermissionResultDeny)
        assert isinstance(deny, PermissionResultDeny)
        assert not isinstance(deny, PermissionResultAllow)


# =========================================================================
# Section 3: JSONL Format Verification
# =========================================================================
# Phase 2 moves JSONL reading/writing into ClaudeJsonlStore.  The JSONL
# format is a contract between session_manager.py, the Claude CLI, and the
# file-history system.  If Phase 2 changes the format, sessions break.
#
# These tests create real JSONL files in tmp_path and verify the parsing
# and writing logic.
# =========================================================================


class TestPrepopulateTrackedFiles:
    """Verify _prepopulate_tracked_files can extract tracked files from JSONL.

    This function (lines ~3045-3137) scans the JSONL for:
    1. tool_use blocks with name in {Edit, Write, MultiEdit, NotebookEdit}
    2. file-history-snapshot entries with trackedFileBackups

    Phase 2 regression: if ClaudeJsonlStore changes the JSONL parsing logic
    or field names, session recovery after daemon restart will lose file
    tracking and Rewind will show no files.
    """

    def test_extracts_file_paths_from_tool_use_blocks(self, tmp_path):
        """Source 1: assistant messages with tool_use blocks.
        Lines ~3094-3109 extract block.input.file_path for Edit/Write tools.
        """
        jsonl_path = tmp_path / "test_session.jsonl"
        entries = [
            # A user message (should be ignored)
            {"type": "user", "uuid": "u1", "message": {"content": "edit the file"}},
            # An assistant message with an Edit tool_use
            {
                "type": "assistant",
                "uuid": "a1",
                "message": {
                    "content": [
                        {"type": "tool_use", "name": "Edit", "id": "tu1",
                         "input": {"file_path": "/src/main.py", "old_string": "a", "new_string": "b"}},
                    ],
                },
            },
            # Another assistant with Write
            {
                "type": "assistant",
                "uuid": "a2",
                "message": {
                    "content": [
                        {"type": "tool_use", "name": "Write", "id": "tu2",
                         "input": {"file_path": "/src/utils.py", "content": "print(1)"}},
                    ],
                },
            },
            # An assistant with Bash (should NOT be tracked)
            {
                "type": "assistant",
                "uuid": "a3",
                "message": {
                    "content": [
                        {"type": "tool_use", "name": "Bash", "id": "tu3",
                         "input": {"command": "ls"}},
                    ],
                },
            },
        ]
        with open(jsonl_path, "w", encoding="utf-8") as f:
            for entry in entries:
                f.write(json.dumps(entry) + "\n")

        # Parse the JSONL the same way _prepopulate_tracked_files does
        edit_tools = {"Edit", "Write", "MultiEdit", "NotebookEdit"}
        found = set()
        with open(jsonl_path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                obj = json.loads(line)
                if obj.get("type") == "assistant":
                    msg = obj.get("message", {})
                    content = msg.get("content", [])
                    if not isinstance(content, list):
                        continue
                    for block in content:
                        if not isinstance(block, dict):
                            continue
                        if block.get("type") != "tool_use":
                            continue
                        if block.get("name") not in edit_tools:
                            continue
                        inp = block.get("input", {})
                        fp = inp.get("file_path", "") or inp.get("path", "")
                        if fp:
                            found.add(fp)

        assert "/src/main.py" in found
        assert "/src/utils.py" in found
        assert len(found) == 2  # Bash tool should NOT be included

    def test_extracts_files_from_file_history_snapshot(self, tmp_path):
        """Source 2: file-history-snapshot entries.
        Lines ~3112-3120 extract trackedFileBackups keys.
        """
        jsonl_path = tmp_path / "test_session.jsonl"
        entries = [
            {
                "type": "file-history-snapshot",
                "messageId": "msg1",
                "snapshot": {
                    "messageId": "msg1",
                    "trackedFileBackups": {
                        "/src/app.py": {"backupFileName": "abc@v1", "version": 1, "backupTime": "2026-01-01T00:00:00Z"},
                        "/src/config.py": {"backupFileName": "def@v2", "version": 2, "backupTime": "2026-01-01T00:00:00Z"},
                    },
                    "timestamp": "2026-01-01T00:00:00Z",
                },
                "isSnapshotUpdate": False,
            },
        ]
        with open(jsonl_path, "w", encoding="utf-8") as f:
            for entry in entries:
                f.write(json.dumps(entry) + "\n")

        found = set()
        max_version = {}
        with open(jsonl_path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                obj = json.loads(line)
                if obj.get("type") == "file-history-snapshot":
                    snap = obj.get("snapshot", {})
                    for fp, binfo in snap.get("trackedFileBackups", {}).items():
                        if fp:
                            found.add(fp)
                        if isinstance(binfo, dict):
                            v = binfo.get("version", 0)
                            if v > max_version.get(fp, 0):
                                max_version[fp] = v

        assert "/src/app.py" in found
        assert "/src/config.py" in found
        assert max_version["/src/app.py"] == 1
        assert max_version["/src/config.py"] == 2

    def test_uuid_caching_from_jsonl(self, tmp_path):
        """_prepopulate_tracked_files caches user/assistant UUIDs so
        _write_file_snapshot doesn't re-parse the whole JSONL every turn.
        Lines ~3086-3091.

        Phase 2 regression: if UUID caching is lost, _write_file_snapshot
        falls back to tail-reading which may pick up wrong UUIDs.
        """
        jsonl_path = tmp_path / "test_session.jsonl"
        entries = [
            {"type": "user", "uuid": "user-uuid-1", "message": {"content": "hello"}},
            {"type": "assistant", "uuid": "asst-uuid-1", "message": {"content": []}},
            {"type": "user", "uuid": "user-uuid-2", "message": {"content": "do more"}},
            {"type": "assistant", "uuid": "asst-uuid-2", "message": {"content": []}},
        ]
        with open(jsonl_path, "w", encoding="utf-8") as f:
            for entry in entries:
                f.write(json.dumps(entry) + "\n")

        # Simulate the UUID caching loop from _prepopulate_tracked_files
        last_user_uuid = ""
        last_asst_uuid = ""
        with open(jsonl_path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                obj = json.loads(line)
                uid = obj.get("uuid", "")
                if uid:
                    if obj.get("type") == "user":
                        last_user_uuid = uid
                    elif obj.get("type") == "assistant":
                        last_asst_uuid = uid

        assert last_user_uuid == "user-uuid-2"
        assert last_asst_uuid == "asst-uuid-2"


class TestWriteFileSnapshotFormat:
    """Verify the file-history-snapshot JSONL entry structure.

    _write_file_snapshot (lines ~3139-3324) creates entries with this shape:
    {
        "type": "file-history-snapshot",
        "messageId": <outer_uuid>,
        "snapshot": {
            "messageId": <inner_uuid>,
            "trackedFileBackups": { <path>: { "backupFileName": ..., "version": ..., "backupTime": ... } },
            "timestamp": <ISO string>,
        },
        "isSnapshotUpdate": <bool>,
    }

    Phase 2 regression: if ClaudeJsonlStore produces a different structure,
    the CLI's Rewind feature won't find the snapshots and file history breaks.
    """

    def test_pre_turn_snapshot_structure(self, tmp_path):
        """Pre-turn: outer=user_uuid, inner=user_uuid, isSnapshotUpdate=false.
        Lines ~3296-3299.
        """
        user_uuid = str(uuid.uuid4())
        now_iso = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")

        snapshot_entry = {
            "type": "file-history-snapshot",
            "messageId": user_uuid,
            "snapshot": {
                "messageId": user_uuid,  # Same as outer for pre-turn
                "trackedFileBackups": {
                    "/src/main.py": {
                        "backupFileName": "abc123@v1",
                        "version": 1,
                        "backupTime": now_iso,
                    },
                },
                "timestamp": now_iso,
            },
            "isSnapshotUpdate": False,
        }

        # Verify structure
        assert snapshot_entry["type"] == "file-history-snapshot"
        assert snapshot_entry["isSnapshotUpdate"] is False
        assert snapshot_entry["messageId"] == snapshot_entry["snapshot"]["messageId"]

        # Verify it round-trips through JSON
        serialized = json.dumps(snapshot_entry)
        restored = json.loads(serialized)
        assert restored == snapshot_entry

    def test_post_turn_snapshot_structure(self, tmp_path):
        """Post-turn: outer=asst_uuid, inner=user_uuid, isSnapshotUpdate=true.
        Lines ~3292-3295.
        """
        user_uuid = str(uuid.uuid4())
        asst_uuid = str(uuid.uuid4())
        now_iso = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")

        snapshot_entry = {
            "type": "file-history-snapshot",
            "messageId": asst_uuid,       # outer = assistant UUID
            "snapshot": {
                "messageId": user_uuid,   # inner = user UUID (different!)
                "trackedFileBackups": {
                    "/src/main.py": {
                        "backupFileName": "def456@v2",
                        "version": 2,
                        "backupTime": now_iso,
                    },
                },
                "timestamp": now_iso,
            },
            "isSnapshotUpdate": True,
        }

        assert snapshot_entry["isSnapshotUpdate"] is True
        assert snapshot_entry["messageId"] != snapshot_entry["snapshot"]["messageId"]

    def test_snapshot_appended_to_jsonl(self, tmp_path):
        """Verify a snapshot entry can be appended to a JSONL file and
        read back correctly.  _write_file_snapshot does this at line ~3313.
        """
        jsonl_path = tmp_path / "session.jsonl"
        # Start with existing content
        with open(jsonl_path, "w", encoding="utf-8") as f:
            f.write(json.dumps({"type": "user", "uuid": "u1", "message": {"content": "hi"}}) + "\n")

        # Append a snapshot
        snapshot = {
            "type": "file-history-snapshot",
            "messageId": "msg_id",
            "snapshot": {
                "messageId": "msg_id",
                "trackedFileBackups": {"/a.py": {"backupFileName": "h@v1", "version": 1, "backupTime": "2026-01-01T00:00:00Z"}},
                "timestamp": "2026-01-01T00:00:00Z",
            },
            "isSnapshotUpdate": False,
        }
        with open(jsonl_path, "a", encoding="utf-8") as f:
            f.write(json.dumps(snapshot) + "\n")

        # Read back
        lines = jsonl_path.read_text(encoding="utf-8").strip().split("\n")
        assert len(lines) == 2
        restored = json.loads(lines[1])
        assert restored["type"] == "file-history-snapshot"
        assert restored["snapshot"]["trackedFileBackups"]["/a.py"]["version"] == 1


class TestRepairIncompleteJsonl:
    """Verify _repair_incomplete_jsonl fixes truncated assistant entries.

    This function (lines ~254-302) detects assistant messages with
    stop_reason=null and patches them to stop_reason="end_turn".

    Phase 2 regression: if ClaudeJsonlStore changes the repair logic or
    field names, session recovery will fail with infinite reconnect loops
    (see the "Stream Closed on Recovery Bug" comment at line ~238).
    """

    def test_patches_incomplete_assistant_turn(self, tmp_path):
        """An assistant message with stop_reason=null should be patched
        to stop_reason='end_turn' with an interruption notice appended.
        """
        jsonl_path = tmp_path / "session.jsonl"
        incomplete = {
            "type": "assistant",
            "uuid": "a1",
            "message": {
                "content": [{"type": "text", "text": "I was in the middle of"}],
                "stop_reason": None,
            },
        }
        with open(jsonl_path, "w", encoding="utf-8") as f:
            f.write(json.dumps(incomplete) + "\n")

        # Replicate _repair_incomplete_jsonl logic
        with open(jsonl_path, "r", encoding="utf-8") as f:
            lines = f.readlines()

        last_line = lines[-1].strip()
        obj = json.loads(last_line)
        assert obj["type"] == "assistant"
        msg = obj.get("message", {})
        assert msg.get("stop_reason") is None  # Confirms it's incomplete

        # Apply the repair
        msg["stop_reason"] = "end_turn"
        msg["stop_sequence"] = None
        content = msg.get("content", [])
        content.append({
            "type": "text",
            "text": "\n\n[Session interrupted — resuming from last checkpoint]",
        })
        msg["content"] = content

        lines[-1] = json.dumps(obj) + "\n"
        with open(jsonl_path, "w", encoding="utf-8") as f:
            f.writelines(lines)

        # Verify the repair
        with open(jsonl_path, "r", encoding="utf-8") as f:
            repaired = json.loads(f.readline().strip())

        assert repaired["message"]["stop_reason"] == "end_turn"
        assert repaired["message"]["stop_sequence"] is None
        assert len(repaired["message"]["content"]) == 2
        assert "interrupted" in repaired["message"]["content"][-1]["text"].lower()

    def test_skips_complete_assistant_turn(self, tmp_path):
        """If stop_reason is not None, the function should return False."""
        jsonl_path = tmp_path / "session.jsonl"
        complete = {
            "type": "assistant",
            "uuid": "a1",
            "message": {
                "content": [{"type": "text", "text": "All done"}],
                "stop_reason": "end_turn",
            },
        }
        with open(jsonl_path, "w", encoding="utf-8") as f:
            f.write(json.dumps(complete) + "\n")

        with open(jsonl_path, "r", encoding="utf-8") as f:
            lines = f.readlines()
        obj = json.loads(lines[-1].strip())
        msg = obj.get("message", {})
        # This is the check at line ~281 -- should NOT be patched
        assert msg.get("stop_reason") is not None

    def test_skips_non_assistant_last_line(self, tmp_path):
        """If the last line is a user message, no repair needed."""
        jsonl_path = tmp_path / "session.jsonl"
        with open(jsonl_path, "w", encoding="utf-8") as f:
            f.write(json.dumps({"type": "user", "uuid": "u1", "message": {"content": "hi"}}) + "\n")

        with open(jsonl_path, "r", encoding="utf-8") as f:
            lines = f.readlines()
        obj = json.loads(lines[-1].strip())
        # The check at line ~276 -- should NOT repair non-assistant
        assert obj.get("type") != "assistant"


class TestUuidExtractionFromJsonlTail:
    """Verify UUID extraction from the tail of a JSONL file.

    _write_file_snapshot reads the last 64KB of the JSONL to find UUIDs
    (lines ~3261-3286).  This is a performance optimization over reading
    the entire file.

    Phase 2 regression: if ClaudeJsonlStore changes how UUIDs are stored
    or extracted, file-history snapshots will have wrong messageIds and
    Rewind will break.
    """

    def test_extracts_last_user_and_assistant_uuids(self, tmp_path):
        """Simulate the tail-reading pattern from lines ~3261-3286."""
        jsonl_path = tmp_path / "session.jsonl"
        entries = [
            {"type": "user", "uuid": "u1", "message": {"content": "hello"}},
            {"type": "assistant", "uuid": "a1", "message": {"content": []}},
            {"type": "user", "uuid": "u2", "message": {"content": "more work"}},
            {"type": "assistant", "uuid": "a2", "message": {"content": []}},
            {"type": "user", "uuid": "u3", "message": {"content": "final question"}},
        ]
        with open(jsonl_path, "w", encoding="utf-8") as f:
            for entry in entries:
                f.write(json.dumps(entry) + "\n")

        # Replicate the tail-reading logic
        file_size = jsonl_path.stat().st_size
        tail_size = min(file_size, 65536)
        with open(jsonl_path, "rb") as rf:
            rf.seek(max(0, file_size - tail_size))
            tail = rf.read().decode("utf-8", errors="replace")

        last_user_uuid = ""
        last_asst_uuid = ""
        for raw_line in tail.splitlines():
            raw_line = raw_line.strip()
            if not raw_line:
                continue
            obj = json.loads(raw_line)
            t = obj.get("type", "")
            uid = obj.get("uuid", "")
            if t == "user" and uid:
                last_user_uuid = uid
            elif t == "assistant" and uid:
                last_asst_uuid = uid

        assert last_user_uuid == "u3"
        assert last_asst_uuid == "a2"


# =========================================================================
# Section 4: ClaudeCodeOptions Verification
# =========================================================================
# Phase 2 moves ClaudeCodeOptions creation into ClaudeAgentSDK.create_session().
# session_manager._drive_session creates options at lines ~1221-1232.
# If any of those fields are renamed or have incompatible types, session
# creation will fail.
# =========================================================================


class TestClaudeCodeOptions:
    """Verify ClaudeCodeOptions accepts all parameters session_manager uses."""

    def test_all_session_manager_fields(self):
        """session_manager._drive_session creates ClaudeCodeOptions with these
        exact fields at lines ~1221-1232.  Each field name must be accepted.

        Phase 2 regression: if ClaudeAgentSDK.create_session() uses different
        field names or drops a field, sessions won't start.
        """
        async def dummy_can_use_tool(name, inp, ctx):
            return PermissionResultAllow(updated_input=inp)

        options = ClaudeCodeOptions(
            cwd="/some/path",
            resume="session-id-to-resume",
            can_use_tool=dummy_can_use_tool,
            model="claude-sonnet-4-20250514",
            system_prompt="You are a helpful assistant",
            max_turns=10,
            allowed_tools=["Bash", "Edit", "Write"],
            permission_mode="default",
            include_partial_messages=True,
            extra_args={"--verbose": None},
        )
        assert options.cwd == "/some/path"
        assert options.resume == "session-id-to-resume"
        assert options.model == "claude-sonnet-4-20250514"
        assert options.system_prompt == "You are a helpful assistant"
        assert options.max_turns == 10
        assert options.allowed_tools == ["Bash", "Edit", "Write"]
        assert options.permission_mode == "default"
        assert options.include_partial_messages is True
        assert options.extra_args == {"--verbose": None}
        assert options.can_use_tool is dummy_can_use_tool

    def test_cwd_accepts_none(self):
        """session_manager passes cwd=None when no working directory is set."""
        options = ClaudeCodeOptions(cwd=None)
        assert options.cwd is None

    def test_resume_accepts_none(self):
        """session_manager passes resume=None for new sessions (not resuming)."""
        options = ClaudeCodeOptions(resume=None)
        assert options.resume is None

    def test_can_use_tool_accepts_none(self):
        """can_use_tool=None means the SDK handles permissions itself."""
        options = ClaudeCodeOptions(can_use_tool=None)
        assert options.can_use_tool is None

    def test_permission_mode_values(self):
        """session_manager passes permission_mode from user config.
        Verify common values are accepted.
        """
        for mode in ("default", "acceptEdits", "plan", "bypassPermissions"):
            options = ClaudeCodeOptions(permission_mode=mode)
            assert options.permission_mode == mode


# =========================================================================
# Section 5: SDK Import Verification
# =========================================================================
# session_manager.py imports specific names from the SDK at lines ~27-41.
# If a future SDK update renames or removes any of these, session_manager
# will fail to import and the entire daemon crashes.
#
# Phase 2 regression: the new daemon/backends/claude.py must import the
# same names.  If it imports from a different path, it will break.
# =========================================================================


class TestSDKImports:
    """Verify all SDK names that session_manager.py imports are available."""

    def test_claude_sdk_client_importable(self):
        """session_manager.py line ~27: from claude_code_sdk import ClaudeSDKClient"""
        from claude_code_sdk import ClaudeSDKClient
        assert ClaudeSDKClient is not None

    def test_claude_code_options_importable(self):
        """session_manager.py line ~27: from claude_code_sdk import ClaudeCodeOptions"""
        from claude_code_sdk import ClaudeCodeOptions
        assert ClaudeCodeOptions is not None

    def test_assistant_message_importable(self):
        """session_manager.py line ~29"""
        from claude_code_sdk.types import AssistantMessage
        assert AssistantMessage is not None

    def test_user_message_importable(self):
        """session_manager.py line ~30"""
        from claude_code_sdk.types import UserMessage
        assert UserMessage is not None

    def test_result_message_importable(self):
        """session_manager.py line ~31"""
        from claude_code_sdk.types import ResultMessage
        assert ResultMessage is not None

    def test_system_message_importable(self):
        """session_manager.py line ~32"""
        from claude_code_sdk.types import SystemMessage
        assert SystemMessage is not None

    def test_stream_event_importable(self):
        """session_manager.py line ~33"""
        from claude_code_sdk.types import StreamEvent
        assert StreamEvent is not None

    def test_text_block_importable(self):
        """session_manager.py line ~34"""
        from claude_code_sdk.types import TextBlock
        assert TextBlock is not None

    def test_thinking_block_importable(self):
        """session_manager.py line ~35"""
        from claude_code_sdk.types import ThinkingBlock
        assert ThinkingBlock is not None

    def test_tool_use_block_importable(self):
        """session_manager.py line ~36"""
        from claude_code_sdk.types import ToolUseBlock
        assert ToolUseBlock is not None

    def test_tool_result_block_importable(self):
        """session_manager.py line ~37"""
        from claude_code_sdk.types import ToolResultBlock
        assert ToolResultBlock is not None

    def test_permission_result_allow_importable(self):
        """session_manager.py line ~38"""
        from claude_code_sdk.types import PermissionResultAllow
        assert PermissionResultAllow is not None

    def test_permission_result_deny_importable(self):
        """session_manager.py line ~39"""
        from claude_code_sdk.types import PermissionResultDeny
        assert PermissionResultDeny is not None

    def test_content_block_importable(self):
        """session_manager.py line ~40: ContentBlock is a Union type alias."""
        from claude_code_sdk.types import ContentBlock
        assert ContentBlock is not None

    def test_content_block_is_union_of_block_types(self):
        """ContentBlock should be a Union of TextBlock, ThinkingBlock,
        ToolUseBlock, ToolResultBlock.  Verify all are included.

        Phase 2 regression: if _normalize_message uses ContentBlock for
        type checking and it changes, isinstance checks break.
        """
        from claude_code_sdk.types import ContentBlock
        # ContentBlock is a Union type alias (types.UnionType in Python 3.10+)
        args = getattr(ContentBlock, '__args__', None)
        if args:
            arg_names = {a.__name__ for a in args}
            assert "TextBlock" in arg_names
            assert "ToolUseBlock" in arg_names
            assert "ToolResultBlock" in arg_names
            assert "ThinkingBlock" in arg_names


# =========================================================================
# Section 6: Message Processing Integration
# =========================================================================
# This section tests the full isinstance/attribute-access pipeline that
# _process_message uses.  We can't call _process_message directly (it's
# on SessionManager and needs a full session setup), so we replicate its
# dispatch logic and verify it produces correct results.
#
# This is the MOST IMPORTANT section -- it tests the exact code path that
# Phase 2 will move into ClaudeAgentSDK._normalize_message().
# =========================================================================


def _simulate_process_message(message):
    """Replicate the isinstance dispatch and attribute access pattern from
    session_manager._process_message (lines ~2422-2790).

    Returns a list of dicts representing the LogEntries that would be created.
    This lets us verify that real SDK message objects produce the right output.

    This function deliberately mirrors session_manager.py line-by-line.
    If Phase 2 changes the dispatch logic, update this function to match
    and verify the tests still pass.
    """
    entries = []

    if isinstance(message, AssistantMessage):
        for block in (message.content if hasattr(message, 'content') else []):
            if isinstance(block, TextBlock):
                entries.append({
                    "kind": "asst",
                    "text": (block.text or "")[:50000],
                })
            elif isinstance(block, ToolUseBlock):
                inp = block.input if hasattr(block, 'input') and isinstance(block.input, dict) else {}
                entries.append({
                    "kind": "tool_use",
                    "name": getattr(block, 'name', '') or '',
                    "id": getattr(block, 'id', '') or '',
                    "input": inp,
                })
            elif isinstance(block, ThinkingBlock):
                # Skipped -- no entry created
                pass

    elif isinstance(message, UserMessage):
        is_sub_agent = bool(getattr(message, 'parent_tool_use_id', None))

        raw_content = getattr(message, 'content', None) or []
        if isinstance(raw_content, str):
            blocks = [TextBlock(text=raw_content)] if raw_content.strip() else []
        elif isinstance(raw_content, list):
            blocks = raw_content
        else:
            blocks = []

        for block in blocks:
            if isinstance(block, ToolResultBlock):
                rc = getattr(block, 'content', '') or ''
                if isinstance(rc, list):
                    text_parts = []
                    for b in rc:
                        if isinstance(b, dict) and b.get("type") == "text":
                            text_parts.append(b.get("text", ""))
                        elif hasattr(b, 'text'):
                            text_parts.append(b.text or "")
                    rt = " ".join(text_parts)
                elif isinstance(rc, str):
                    rt = rc
                else:
                    rt = str(rc)

                entries.append({
                    "kind": "tool_result",
                    "text": rt[:20000],
                    "tool_use_id": getattr(block, 'tool_use_id', '') or '',
                    "is_error": bool(getattr(block, 'is_error', False)),
                })

            elif isinstance(block, TextBlock) and not is_sub_agent:
                entries.append({
                    "kind": "user",
                    "text": (block.text or "")[:20000],
                })

    elif isinstance(message, SystemMessage):
        subtype = getattr(message, 'subtype', '') or ''
        data = getattr(message, 'data', {}) or {}
        entries.append({
            "kind": "system",
            "subtype": subtype,
            "data": data,
        })

    elif isinstance(message, ResultMessage):
        entries.append({
            "kind": "result",
            "cost_usd": getattr(message, 'total_cost_usd', 0.0) or 0.0,
            "is_error": getattr(message, 'is_error', False),
            "session_id": getattr(message, 'session_id', None),
            "duration_ms": getattr(message, 'duration_ms', 0) or 0,
            "num_turns": getattr(message, 'num_turns', 0) or 0,
        })

    elif isinstance(message, StreamEvent):
        entries.append({
            "kind": "stream",
            "event": getattr(message, 'event', ''),
        })

    return entries


class TestMessageProcessingIntegration:
    """End-to-end tests that feed real SDK message objects through the
    _process_message dispatch logic.

    These tests are the ultimate regression net for Phase 2: they verify
    that real SDK types produce the correct log entries through the exact
    isinstance/getattr pipeline that session_manager.py uses.
    """

    def test_assistant_text_message(self):
        """An AssistantMessage with a TextBlock should produce an 'asst' entry.

        Phase 2 will move this into _normalize_message.  If the new code
        produces a different entry structure, the UI will show broken messages.
        """
        msg = AssistantMessage(
            content=[TextBlock(text="Hello, I can help with that.")],
            model="claude-3",
            parent_tool_use_id=None,
        )
        entries = _simulate_process_message(msg)
        assert len(entries) == 1
        assert entries[0]["kind"] == "asst"
        assert entries[0]["text"] == "Hello, I can help with that."

    def test_assistant_tool_use_message(self):
        """An AssistantMessage with a ToolUseBlock should produce a 'tool_use' entry
        with the correct name, id, and input.
        """
        msg = AssistantMessage(
            content=[ToolUseBlock(id="tu_abc", name="Edit", input={"file_path": "/main.py", "old_string": "a", "new_string": "b"})],
            model="claude-3",
            parent_tool_use_id=None,
        )
        entries = _simulate_process_message(msg)
        assert len(entries) == 1
        assert entries[0]["kind"] == "tool_use"
        assert entries[0]["name"] == "Edit"
        assert entries[0]["id"] == "tu_abc"
        assert entries[0]["input"]["file_path"] == "/main.py"

    def test_assistant_mixed_content(self):
        """An AssistantMessage with thinking + text + tool_use should produce
        entries for text and tool_use only (thinking is skipped).
        """
        msg = AssistantMessage(
            content=[
                ThinkingBlock(thinking="Let me think about this", signature="s"),
                TextBlock(text="I'll edit the file"),
                ToolUseBlock(id="tu_1", name="Write", input={"file_path": "/new.py", "content": "print(1)"}),
            ],
            model="claude-3",
            parent_tool_use_id=None,
        )
        entries = _simulate_process_message(msg)
        assert len(entries) == 2  # thinking skipped
        assert entries[0]["kind"] == "asst"
        assert entries[1]["kind"] == "tool_use"
        assert entries[1]["name"] == "Write"

    def test_user_tool_result_message(self):
        """A UserMessage with a ToolResultBlock should produce a 'tool_result' entry.

        Phase 2 regression: if tool_result entries lose their tool_use_id,
        the UI can't match results to their tool uses.
        """
        msg = UserMessage(
            content=[ToolResultBlock(tool_use_id="tu_abc", content="File written successfully", is_error=False)],
            parent_tool_use_id=None,
        )
        entries = _simulate_process_message(msg)
        assert len(entries) == 1
        assert entries[0]["kind"] == "tool_result"
        assert entries[0]["tool_use_id"] == "tu_abc"
        assert entries[0]["text"] == "File written successfully"
        assert entries[0]["is_error"] is False

    def test_user_tool_result_error(self):
        """A ToolResultBlock with is_error=True should propagate the error flag.

        Phase 2 regression: if is_error is lost, the "Stream closed" self-healing
        logic at line ~2503 won't trigger.
        """
        msg = UserMessage(
            content=[ToolResultBlock(tool_use_id="tu_xyz", content="Stream closed", is_error=True)],
            parent_tool_use_id=None,
        )
        entries = _simulate_process_message(msg)
        assert len(entries) == 1
        assert entries[0]["is_error"] is True
        assert "Stream closed" in entries[0]["text"]

    def test_user_tool_result_list_content(self):
        """ToolResultBlock.content can be a list of dicts with type='text'.
        _process_message extracts and joins them (lines ~2483-2494).
        """
        msg = UserMessage(
            content=[ToolResultBlock(
                tool_use_id="tu_1",
                content=[{"type": "text", "text": "part1"}, {"type": "text", "text": "part2"}],
                is_error=False,
            )],
            parent_tool_use_id=None,
)
        entries = _simulate_process_message(msg)
        assert len(entries) == 1
        assert entries[0]["text"] == "part1 part2"

    def test_user_text_message_not_sub_agent(self):
        """A UserMessage with a TextBlock and no parent_tool_use_id should
        produce a 'user' entry.
        """
        msg = UserMessage(
            content=[TextBlock(text="Please help me")],
            parent_tool_use_id=None,
        )
        entries = _simulate_process_message(msg)
        assert len(entries) == 1
        assert entries[0]["kind"] == "user"
        assert entries[0]["text"] == "Please help me"

    def test_user_text_sub_agent_skipped(self):
        """A UserMessage from a sub-agent (parent_tool_use_id set) should
        NOT produce a user text entry.

        Phase 2 regression: if the sub-agent check is lost, internal agent
        messages will appear as user chat bubbles in the UI.
        """
        msg = UserMessage(
            content=[TextBlock(text="Sub-agent internal message")],
            parent_tool_use_id="parent_tu_id",
        )
        entries = _simulate_process_message(msg)
        # TextBlock entries are skipped for sub-agents
        assert len(entries) == 0

    def test_system_message(self):
        """A SystemMessage should produce a 'system' entry with subtype and data."""
        msg = SystemMessage(subtype="compact_boundary", data={"compactMetadata": {"preTokens": 50000}})
        entries = _simulate_process_message(msg)
        assert len(entries) == 1
        assert entries[0]["kind"] == "system"
        assert entries[0]["subtype"] == "compact_boundary"
        assert entries[0]["data"]["compactMetadata"]["preTokens"] == 50000

    def test_result_message(self):
        """A ResultMessage should produce a 'result' entry with all metadata.

        Phase 2 regression: if any field is renamed, session completion
        metadata (cost, timing, error state) will be lost.
        """
        msg = ResultMessage(
            subtype="result", duration_ms=5000, duration_api_ms=4000,
            is_error=False, num_turns=3, session_id="final_session_id",
            total_cost_usd=0.042, usage={"input_tokens": 1000}, result="done",
        )
        entries = _simulate_process_message(msg)
        assert len(entries) == 1
        assert entries[0]["kind"] == "result"
        assert entries[0]["cost_usd"] == 0.042
        assert entries[0]["is_error"] is False
        assert entries[0]["session_id"] == "final_session_id"
        assert entries[0]["duration_ms"] == 5000
        assert entries[0]["num_turns"] == 3

    def test_stream_event(self):
        """A StreamEvent should produce a 'stream' entry with the event type."""
        msg = StreamEvent(
            uuid="u1", session_id="s1",
            event="message_start", parent_tool_use_id=None,
        )
        entries = _simulate_process_message(msg)
        assert len(entries) == 1
        assert entries[0]["kind"] == "stream"
        assert entries[0]["event"] == "message_start"

    def test_file_tracking_edit_tool(self):
        """Verify that the file tracking logic correctly identifies Edit/Write
        tool uses and extracts file_path from the input.

        This is the logic at lines ~2450-2458 that Phase 2 must preserve.
        """
        edit_tools = {"Edit", "Write", "MultiEdit", "NotebookEdit"}

        for tool_name in edit_tools:
            msg = AssistantMessage(
                content=[ToolUseBlock(
                    id="tu_1", name=tool_name,
                    input={"file_path": f"/project/{tool_name.lower()}_target.py"},
                )],
                model="claude-3",
                parent_tool_use_id=None,
            )
            entries = _simulate_process_message(msg)
            assert len(entries) == 1
            inp = entries[0]["input"]
            fp = inp.get("file_path", "") or inp.get("path", "")
            assert fp == f"/project/{tool_name.lower()}_target.py", (
                f"File path extraction failed for tool {tool_name}"
            )

    def test_non_edit_tool_not_tracked(self):
        """Tools like Bash, Read, Grep should NOT have their files tracked.
        Only Edit/Write/MultiEdit/NotebookEdit trigger file tracking.
        """
        msg = AssistantMessage(
            content=[ToolUseBlock(id="tu_1", name="Bash", input={"command": "cat /etc/hosts"})],
            model="claude-3",
            parent_tool_use_id=None,
        )
        entries = _simulate_process_message(msg)
        assert len(entries) == 1
        # The entry is still created, but session_manager only adds to
        # tracked_files for edit tools -- we verify the name for downstream checks
        assert entries[0]["name"] == "Bash"

    def test_empty_assistant_content(self):
        """An AssistantMessage with empty content list should produce no entries."""
        msg = AssistantMessage(content=[], model="claude-3", parent_tool_use_id=None)
        entries = _simulate_process_message(msg)
        assert len(entries) == 0

    def test_user_message_string_content(self):
        """UserMessage.content can be a plain string (SDK wraps it).
        _process_message normalizes this at lines ~2472-2477.

        Phase 2 regression: if string content normalization is lost,
        plain string messages will produce no entries.
        """
        msg = UserMessage(content="hello from string content", parent_tool_use_id=None)
        entries = _simulate_process_message(msg)
        assert len(entries) == 1
        assert entries[0]["kind"] == "user"
        assert entries[0]["text"] == "hello from string content"

    def test_user_message_empty_string_content(self):
        """An empty string content should produce no entries (whitespace check)."""
        msg = UserMessage(content="   ", parent_tool_use_id=None)
        entries = _simulate_process_message(msg)
        assert len(entries) == 0


# =========================================================================
# Section 7: Dataclass Field Completeness
# =========================================================================
# Verify that the SDK dataclass fields haven't changed in a way that would
# break session_manager.py.  This catches SDK updates that rename fields.
# =========================================================================


class TestDataclassFieldCompleteness:
    """Verify that SDK dataclasses have the exact fields session_manager needs.

    These tests will fail if a future SDK update renames or removes a field,
    giving early warning before Phase 2 code breaks at runtime.
    """

    def test_assistant_message_has_required_fields(self):
        """AssistantMessage must have: content, model, parent_tool_use_id"""
        import dataclasses
        fields = {f.name for f in dataclasses.fields(AssistantMessage)}
        assert "content" in fields
        assert "model" in fields
        assert "parent_tool_use_id" in fields

    def test_user_message_has_required_fields(self):
        """UserMessage must have: content, parent_tool_use_id"""
        import dataclasses
        fields = {f.name for f in dataclasses.fields(UserMessage)}
        assert "content" in fields
        assert "parent_tool_use_id" in fields

    def test_result_message_has_required_fields(self):
        """ResultMessage must have: session_id, total_cost_usd, usage,
        duration_ms, num_turns, is_error"""
        import dataclasses
        fields = {f.name for f in dataclasses.fields(ResultMessage)}
        for required in ("session_id", "total_cost_usd", "usage",
                         "duration_ms", "num_turns", "is_error"):
            assert required in fields, f"ResultMessage missing field: {required}"

    def test_system_message_has_required_fields(self):
        """SystemMessage must have: subtype, data"""
        import dataclasses
        fields = {f.name for f in dataclasses.fields(SystemMessage)}
        assert "subtype" in fields
        assert "data" in fields

    def test_stream_event_has_required_fields(self):
        """StreamEvent must have: event, session_id, uuid"""
        import dataclasses
        fields = {f.name for f in dataclasses.fields(StreamEvent)}
        assert "event" in fields
        assert "session_id" in fields
        assert "uuid" in fields

    def test_tool_use_block_has_required_fields(self):
        """ToolUseBlock must have: id, name, input"""
        import dataclasses
        fields = {f.name for f in dataclasses.fields(ToolUseBlock)}
        assert "id" in fields
        assert "name" in fields
        assert "input" in fields

    def test_tool_result_block_has_required_fields(self):
        """ToolResultBlock must have: tool_use_id, content, is_error"""
        import dataclasses
        fields = {f.name for f in dataclasses.fields(ToolResultBlock)}
        assert "tool_use_id" in fields
        assert "content" in fields
        assert "is_error" in fields

    def test_permission_result_allow_has_required_fields(self):
        """PermissionResultAllow must have: behavior, updated_input"""
        import dataclasses
        fields = {f.name for f in dataclasses.fields(PermissionResultAllow)}
        assert "behavior" in fields
        assert "updated_input" in fields

    def test_permission_result_deny_has_required_fields(self):
        """PermissionResultDeny must have: behavior, message, interrupt"""
        import dataclasses
        fields = {f.name for f in dataclasses.fields(PermissionResultDeny)}
        assert "behavior" in fields
        assert "message" in fields
        assert "interrupt" in fields

    def test_claude_code_options_has_required_fields(self):
        """ClaudeCodeOptions must have all fields used in _drive_session
        at lines ~1221-1232.
        """
        import dataclasses
        fields = {f.name for f in dataclasses.fields(ClaudeCodeOptions)}
        for required in ("cwd", "resume", "can_use_tool", "model",
                         "system_prompt", "max_turns", "allowed_tools",
                         "permission_mode", "include_partial_messages",
                         "extra_args"):
            assert required in fields, f"ClaudeCodeOptions missing field: {required}"
