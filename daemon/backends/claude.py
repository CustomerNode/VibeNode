"""Claude Code SDK implementation of AgentSDK.

Wraps ``claude_code_sdk.ClaudeSDKClient`` and normalizes all Claude-specific
message types into ``VibeNodeMessage`` instances.  This is where ALL
``claude_code_sdk`` imports live -- ``session_manager.py`` never imports
the SDK directly.

Phase 2 of the OOP abstraction refactor moved the following code here:

- All ``claude_code_sdk`` imports (session_manager.py L28-42)
- ``_extract_cli_pid`` static method (L2018-2031)
- Transport liveness check (L2235-2242)
- Message normalization (isinstance() logic from ``_process_message`` L2422-2790)
- Permission callback wrapper (PermissionResultAllow/Deny construction)
- SDK patch application (L66-69)
"""

import logging
import os
from typing import Any, AsyncIterator, Optional

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

from daemon.backends.base import (
    AgentSDK,
    SessionOptions,
    PermissionResult,
    PermissionAction,
)
from daemon.backends.messages import VibeNodeMessage, MessageKind, BlockKind

logger = logging.getLogger(__name__)


class ClaudeAgentSDK(AgentSDK):
    """Claude Code SDK implementation of AgentSDK.

    Wraps claude_code_sdk.ClaudeSDKClient and normalizes all Claude-specific
    message types into VibeNodeMessage instances.

    Thread safety: this class holds no mutable state.  A single instance
    can safely manage multiple concurrent sessions.
    """

    async def create_session(self, options: SessionOptions) -> ClaudeSDKClient:
        """Create a ClaudeSDKClient with mapped options.

        Moved from session_manager.py L1221-1234 and L1174-1182.
        """
        # Convert the abstract permission callback to Claude's format
        can_use_tool = None
        if options.permission_callback:
            can_use_tool = self._wrap_permission_callback(options.permission_callback)

        claude_options = ClaudeCodeOptions(
            cwd=os.path.normpath(options.cwd) if options.cwd else None,
            resume=options.resume,
            can_use_tool=can_use_tool,
            model=options.model,
            system_prompt=options.system_prompt,
            max_turns=options.max_turns,
            allowed_tools=options.allowed_tools,
            permission_mode=options.permission_mode,
            include_partial_messages=options.include_partial_messages,
            extra_args=options.extra_args,
        )
        return ClaudeSDKClient(options=claude_options)

    async def connect(self, client: ClaudeSDKClient) -> None:
        """Connect the Claude client (spawns CLI subprocess).

        Moved from session_manager.py L1252.

        PERF-CRITICAL #5: This must remain a thin wrapper -- the caller
        overlaps mtime recording with this call.  Adding blocking work
        here would break the overlap timing.
        """
        await client.connect()

    async def send_query(self, client: ClaudeSDKClient, prompt: str) -> None:
        """Send a query to the connected Claude session.

        Moved from session_manager.py L1285, L1721.
        """
        await client.query(prompt)

    async def receive_response(
        self, client: ClaudeSDKClient
    ) -> AsyncIterator[VibeNodeMessage]:
        """Stream response messages, normalized to VibeNodeMessage.

        Moved from session_manager.py L1301 and L1727.
        Uses receive_response() (NOT receive_messages()) so the
        iterator terminates after ResultMessage.  See comment at
        session_manager.py L1290.
        """
        async for message in client.receive_response():
            if message is None:
                continue  # Unknown message types (safe_parse_message patch)
            normalized = self._normalize_message(message)
            if normalized is not None:
                yield normalized

    async def interrupt(self, client: ClaudeSDKClient) -> None:
        """Interrupt the current Claude turn.

        Moved from session_manager.py L1997.
        """
        await client.interrupt()

    async def disconnect(self, client: ClaudeSDKClient) -> None:
        """Disconnect the Claude client.

        Moved from session_manager.py L2183.
        """
        await client.disconnect()

    def extract_process_pid(self, client: Any) -> int:
        """Extract CLI subprocess PID from the Claude client.

        Moved from session_manager.py L2018-2031 (_extract_cli_pid).
        Navigates through the transport adapter wrapper.
        """
        if not client:
            return 0
        transport = getattr(client, '_transport', None)
        if transport is None:
            query = getattr(client, '_query', None)
            transport = getattr(query, 'transport', None) if query else None
        inner = getattr(transport, 'inner', transport) if transport else None
        proc = getattr(inner, '_process', None) if inner else None
        if proc and proc.returncode is None:
            return proc.pid or 0
        return 0

    def is_transport_alive(self, client: Any) -> bool:
        """Check if the Claude transport is still connected.

        Moved from session_manager.py L2235-2242.
        """
        try:
            return (
                client is not None
                and client._query is not None
                and client._query.transport is not None
                and client._query.transport.is_ready()
            )
        except Exception:
            return False

    def apply_patches(self) -> list[str]:
        """Apply Claude SDK patches (safe_parse, transport adapter, etc.).

        Moved from session_manager.py L66-69.
        Delegates to daemon/sdk_patches.py.
        """
        from daemon.sdk_patches import apply_patches
        return apply_patches()

    # ── Internal: Message Normalization ──────────────────────────────

    def _normalize_message(self, message: Any) -> Optional[VibeNodeMessage]:
        """Convert a Claude SDK message to a VibeNodeMessage.

        This is the core mapping function.  Every isinstance() check
        in session_manager.py _process_message (L2422-2790) becomes
        a branch here instead.
        """
        if isinstance(message, AssistantMessage):
            return self._normalize_assistant(message)
        elif isinstance(message, UserMessage):
            return self._normalize_user(message)
        elif isinstance(message, SystemMessage):
            return self._normalize_system(message)
        elif isinstance(message, ResultMessage):
            return self._normalize_result(message)
        elif isinstance(message, StreamEvent):
            return self._normalize_stream_event(message)
        return None

    def _normalize_assistant(self, msg: AssistantMessage) -> VibeNodeMessage:
        """Convert AssistantMessage to VibeNodeMessage.

        Moved from session_manager.py L2422-2462.
        """
        blocks = []
        for block in (msg.content if hasattr(msg, 'content') else []):
            if isinstance(block, TextBlock):
                blocks.append({
                    "kind": BlockKind.TEXT.value,
                    "text": (block.text or "")[:50000],
                })
            elif isinstance(block, ToolUseBlock):
                inp = (block.input if hasattr(block, 'input')
                       and isinstance(block.input, dict) else {})
                blocks.append({
                    "kind": BlockKind.TOOL_USE.value,
                    "name": getattr(block, 'name', '') or '',
                    "id": getattr(block, 'id', '') or '',
                    "input": inp,
                })
            elif isinstance(block, ThinkingBlock):
                blocks.append({
                    "kind": BlockKind.THINKING.value,
                })
        return VibeNodeMessage(kind=MessageKind.ASSISTANT, blocks=blocks, raw=msg)

    def _normalize_user(self, msg: UserMessage) -> VibeNodeMessage:
        """Convert UserMessage to VibeNodeMessage.

        Moved from session_manager.py L2464-2589.
        """
        is_sub_agent = bool(getattr(msg, 'parent_tool_use_id', None))
        raw_content = getattr(msg, 'content', None) or []

        blocks = []
        if isinstance(raw_content, str):
            if raw_content.strip():
                blocks.append({
                    "kind": BlockKind.TEXT.value,
                    "text": raw_content[:20000],
                })
        elif isinstance(raw_content, list):
            for block in raw_content:
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
                    blocks.append({
                        "kind": BlockKind.TOOL_RESULT.value,
                        "text": rt[:20000],
                        "tool_use_id": getattr(block, 'tool_use_id', '') or '',
                        "is_error": bool(getattr(block, 'is_error', False)),
                    })
                elif isinstance(block, TextBlock):
                    blocks.append({
                        "kind": BlockKind.TEXT.value,
                        "text": (block.text or "")[:20000],
                    })

        return VibeNodeMessage(
            kind=MessageKind.USER,
            blocks=blocks,
            is_sub_agent=is_sub_agent,
            raw=msg,
        )

    def _normalize_system(self, msg: SystemMessage) -> VibeNodeMessage:
        """Convert SystemMessage to VibeNodeMessage.

        Moved from session_manager.py L2591-2639.
        """
        subtype = getattr(msg, 'subtype', '') or ''
        data = getattr(msg, 'data', {}) or {}
        return VibeNodeMessage(
            kind=MessageKind.SYSTEM,
            subtype=subtype,
            data=data,
            raw=msg,
        )

    def _normalize_result(self, msg: ResultMessage) -> VibeNodeMessage:
        """Convert ResultMessage to VibeNodeMessage.

        Moved from session_manager.py L2641-2730.
        """
        raw_usage = getattr(msg, 'usage', None)
        return VibeNodeMessage(
            kind=MessageKind.RESULT,
            cost_usd=getattr(msg, 'total_cost_usd', 0.0) or 0.0,
            is_error=getattr(msg, 'is_error', False),
            session_id=getattr(msg, 'session_id', None),
            usage=dict(raw_usage) if raw_usage and isinstance(raw_usage, dict) else {},
            duration_ms=getattr(msg, 'duration_ms', 0) or 0,
            num_turns=getattr(msg, 'num_turns', 0) or 0,
            raw=msg,
        )

    def _normalize_stream_event(self, msg: StreamEvent) -> VibeNodeMessage:
        """Convert StreamEvent to VibeNodeMessage.

        Moved from session_manager.py L2732-2790.
        """
        event_data = {}
        if hasattr(msg, 'event'):
            event_data['event'] = msg.event
        if hasattr(msg, 'data'):
            event_data['data'] = msg.data
        return VibeNodeMessage(
            kind=MessageKind.STREAM_EVENT,
            data=event_data,
            raw=msg,
        )

    # ── Internal: Permission callback wrapper ────────────────────────

    def _wrap_permission_callback(self, callback: Any) -> Any:
        """Wrap the abstract permission callback into Claude SDK format.

        The Claude SDK expects can_use_tool to return PermissionResultAllow
        or PermissionResultDeny.  This wrapper converts our abstract
        PermissionResult into the SDK-specific types.

        See phase1-oop-abstraction-plan.md section 7 for full details.
        """
        async def claude_can_use_tool(tool_name, tool_input, context):
            result = await callback(tool_name, tool_input, context)
            if result.action == PermissionAction.ALLOW:
                return PermissionResultAllow(
                    updated_input=result.updated_input,
                    updated_permissions=None,
                )
            else:
                return PermissionResultDeny(
                    message=result.message,
                    interrupt=result.interrupt,
                )
        return claude_can_use_tool
