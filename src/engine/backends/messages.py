"""Backend-agnostic message types for VibeNode.

Every message from any AI backend is normalized to VibeNodeMessage before
SessionManager processes it.  This eliminates all isinstance() checks
against SDK-specific types in the orchestration layer.

The normalization happens inside each AgentSDK implementation's
``receive_response()`` method.  SessionManager never sees raw SDK
messages -- only VibeNodeMessage instances.

Current Claude-specific normalization lives in session_manager.py
``_process_message`` (L2415-2790).  Phase 2 will move that logic into
``ClaudeAgentSDK._normalize_*`` methods.
"""

from dataclasses import dataclass, field
from enum import Enum
from typing import Optional, Any


class MessageKind(Enum):
    """Normalized message kinds -- one per SDK message type.

    Maps to Claude SDK types used in session_manager.py _process_message:
        ASSISTANT    -> AssistantMessage  (L2422)
        USER         -> UserMessage       (L2464)
        SYSTEM       -> SystemMessage     (L2591)
        RESULT       -> ResultMessage     (L2641)
        STREAM_EVENT -> StreamEvent       (L2732)
    """

    ASSISTANT = "assistant"
    USER = "user"
    SYSTEM = "system"
    RESULT = "result"
    STREAM_EVENT = "stream_event"


class BlockKind(Enum):
    """Content block kinds within a message.

    Maps to Claude SDK block types used in session_manager.py _process_message:
        TEXT         -> TextBlock        (L2428, L2537)
        TOOL_USE     -> ToolUseBlock     (L2435)
        TOOL_RESULT  -> ToolResultBlock  (L2480)
        THINKING     -> ThinkingBlock    (L2460)
    """

    TEXT = "text"
    TOOL_USE = "tool_use"
    TOOL_RESULT = "tool_result"
    THINKING = "thinking"


@dataclass
class VibeNodeMessage:
    """Backend-agnostic message from an AI agent.

    Every message from any backend is normalized to this format before
    SessionManager processes it.  This eliminates all isinstance() checks
    against SDK-specific types in the orchestration layer.

    Attributes:
        kind: The message category (ASSISTANT, USER, SYSTEM, RESULT,
            STREAM_EVENT).
        blocks: Content blocks for ASSISTANT and USER messages.  Each
            block is a dict with at minimum a ``"kind"`` key matching a
            ``BlockKind`` value.  Additional keys depend on block kind:

            - TEXT:        ``{"kind": "text", "text": "..."}``
            - TOOL_USE:    ``{"kind": "tool_use", "name": "...",
                            "id": "...", "input": {...}}``
            - TOOL_RESULT: ``{"kind": "tool_result", "text": "...",
                            "tool_use_id": "...", "is_error": bool}``
            - THINKING:    ``{"kind": "thinking"}``

        is_sub_agent: For USER messages -- True if from a sub-agent tool
            context (Claude: ``parent_tool_use_id`` is set at L2467).
            Skips user bubble rendering in the UI.
        subtype: For SYSTEM messages -- the system message subtype
            (e.g. ``"compact_boundary"``, ``"init"``, ``"turn_duration"``).
            See L2592-2639.
        data: For SYSTEM and STREAM_EVENT messages -- the payload dict.
            For STREAM_EVENT, contains ``{"event": "...", "data": {...}}``.
            See L2732-2790.
        cost_usd: For RESULT -- cumulative session cost from
            ``ResultMessage.total_cost_usd`` (L2646).
        is_error: For RESULT -- whether the session ended with error
            (L2669).
        session_id: For RESULT -- the SDK-assigned session ID.  May
            differ from the one we started with, triggering a remap
            (L2679-2727).
        usage: For RESULT -- token usage dict from
            ``ResultMessage.usage`` (L2651-2660).
        duration_ms: For RESULT -- turn duration in milliseconds (L2663).
        num_turns: For RESULT -- number of turns completed (L2664).
        raw: The original SDK message object.  Escape hatch for edge
            cases where the normalized form loses information.  Should
            be used sparingly and never in SessionManager core logic.
    """

    kind: MessageKind

    # Content blocks (ASSISTANT, USER messages)
    blocks: list = field(default_factory=list)

    # USER-specific
    is_sub_agent: bool = False

    # SYSTEM-specific
    subtype: str = ""

    # Shared data payload (SYSTEM, STREAM_EVENT)
    data: dict = field(default_factory=dict)

    # RESULT-specific
    cost_usd: float = 0.0
    is_error: bool = False
    session_id: Optional[str] = None
    usage: dict = field(default_factory=dict)
    duration_ms: int = 0
    num_turns: int = 0

    # Escape hatch -- original SDK message
    raw: Any = None
