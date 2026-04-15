"""Abstract base class for AI agent backends.

This module defines the ``AgentSDK`` abstract class that encapsulates every
point where ``session_manager.py`` currently touches the Claude Code SDK.

There are currently **7 distinct SDK interaction surfaces** in
``session_manager.py``:

+---------------------+----------------------------------------------+
| Surface             | Current location                             |
+=====================+==============================================+
| Client creation     | L1234 ``ClaudeSDKClient(options=options)``    |
| Client connection   | L1252 ``await client.connect()``             |
| Query sending       | L1285 / L1721 ``await client.query(prompt)`` |
| Response streaming  | L1301 / L1727 ``async for ... receive_response()`` |
| Client interrupt    | L1997 ``await info.client.interrupt()``      |
| Client disconnect   | L2183 ``await info.client.disconnect()``     |
| Permission callback | L2206-2409 ``_make_permission_callback``     |
+---------------------+----------------------------------------------+

Additionally, two introspection surfaces exist:

- ``_extract_cli_pid`` (L2018-2031) -- subprocess PID for orphan cleanup
- Transport liveness check (L2235-2242) -- dead-transport detection

The abstract interface captures all of these so that SessionManager can
call them without knowing which backend is behind them.
"""

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import AsyncIterator, Callable, Optional, Any
from enum import Enum

from daemon.backends.messages import VibeNodeMessage


class PermissionAction(Enum):
    """Backend-agnostic permission decision.

    Used by SessionManager's permission orchestration logic
    (session_manager.py L2206-2409) to communicate tool approval/denial
    without depending on Claude-specific ``PermissionResultAllow`` /
    ``PermissionResultDeny`` types.
    """

    ALLOW = "allow"
    DENY = "deny"


@dataclass
class PermissionResult:
    """Result of a permission check.

    Attributes:
        action: ALLOW or DENY.
        updated_input: For ALLOW -- the tool input dict to echo back.
            Must be a dict, never None.  The Claude CLI 2.x crashes when
            ``updatedInput`` is null in the JSON-RPC response.  See
            ``sdk_transport_adapter.py`` docstring for details.
        message: For DENY -- human-readable denial reason shown to the
            agent.
        interrupt: For DENY -- whether to abort the entire turn rather
            than just denying the single tool use.

    The ``make_permission_result_allow`` and ``make_permission_result_deny``
    convenience methods on ``AgentSDK`` create instances with correct
    defaults.
    """

    action: PermissionAction
    updated_input: dict = field(default_factory=dict)
    message: str = ""
    interrupt: bool = False


@dataclass
class SessionOptions:
    """Backend-agnostic session configuration.

    Maps to ``ClaudeCodeOptions`` for the Claude backend (L1221-1231).
    Other backends will map to their own config formats.

    Attributes:
        cwd: Working directory for the session.  Normalized with
            ``os.path.normpath`` before use (L1222).
        resume: Session ID to resume, or None for a new session (L1223).
        model: Model identifier override, or None for default (L1225).
        system_prompt: Custom system prompt, or None (L1226).
        max_turns: Maximum agentic turns, or None for unlimited (L1227).
        allowed_tools: List of tool names the agent may use (L1228).
        permission_mode: Permission policy string (L1229).
        include_partial_messages: Whether to stream partial content
            (L1230).  Default True for live UI updates.
        extra_args: Additional backend-specific arguments (L1231).
        permission_callback: Async callback for tool permission checks.
            Set by SessionManager, not by the caller.  Signature::

                async (tool_name: str, tool_input: dict, context: Any)
                    -> PermissionResult

            The ``AgentSDK`` implementation is responsible for converting
            this abstract callback into whatever format its SDK expects
            (e.g. Claude's ``can_use_tool`` returning
            ``PermissionResultAllow``/``PermissionResultDeny``).
    """

    cwd: Optional[str] = None
    resume: Optional[str] = None
    model: Optional[str] = None
    system_prompt: Optional[str] = None
    max_turns: Optional[int] = None
    allowed_tools: list = field(default_factory=list)
    permission_mode: str = "default"
    include_partial_messages: bool = True
    extra_args: dict = field(default_factory=dict)

    # Permission callback -- set by SessionManager, not by the caller.
    permission_callback: Optional[Callable] = None


class AgentSDK(ABC):
    """Abstract base for AI agent backends.

    Each implementation wraps a specific SDK (Claude, Codex, Gemini, etc.)
    and provides a uniform interface for:

    - Creating and connecting sessions
    - Sending queries and receiving responses as ``VibeNodeMessage``
    - Interrupting and disconnecting sessions
    - Creating permission results compatible with the backend's model
    - Applying any SDK-specific monkey-patches

    Implementations MUST:

    - Convert all backend-specific message types to ``VibeNodeMessage``
      inside ``receive_response()``.
    - Handle their own transport/protocol quirks internally.
    - Provide process PID extraction for orphan cleanup.
    - Be safe for concurrent use across sessions (one ``AgentSDK``
      instance may manage multiple sessions simultaneously).

    The ``client`` parameter in most methods is an opaque handle returned
    by ``create_session()``.  SessionManager stores it on
    ``SessionInfo.client`` but never inspects it -- only passes it back
    to other ``AgentSDK`` methods.
    """

    @abstractmethod
    async def create_session(self, options: SessionOptions) -> Any:
        """Create a new backend session/client object.

        Args:
            options: Backend-agnostic session configuration.

        Returns:
            An opaque client handle.  SessionManager stores this on
            ``SessionInfo.client`` but never inspects it -- only passes
            it back to other ``AgentSDK`` methods.

        Claude implementation (session_manager.py L1221-1234):
            Builds ``ClaudeCodeOptions`` from ``SessionOptions``,
            including ``can_use_tool`` from
            ``options.permission_callback``.
            Returns ``ClaudeSDKClient(options=claude_options)``.
        """
        ...

    @abstractmethod
    async def connect(self, client: Any) -> None:
        """Connect the client (spawn subprocess, establish transport).

        Args:
            client: Handle from ``create_session()``.

        Claude implementation (session_manager.py L1252):
            ``await client.connect()``
            Spawns the Claude CLI subprocess (700-1000ms).

        PERF-CRITICAL #5: The caller (``SessionManager._drive_session``)
        overlaps mtime recording with this call via
        ``run_in_executor``.  The abstract interface must NOT add
        blocking work before returning -- the overlap timing is
        load-bearing.  See CLAUDE.md performance rule #5.
        """
        ...

    @abstractmethod
    async def send_query(self, client: Any, prompt: str) -> None:
        """Send a prompt/query to the connected session.

        Args:
            client: Handle from ``create_session()``.
            prompt: The user's message text.

        Claude implementation (session_manager.py L1285, L1721):
            ``await client.query(prompt)``
        """
        ...

    @abstractmethod
    def receive_response(self, client: Any) -> AsyncIterator[VibeNodeMessage]:
        """Iterate response messages as normalized ``VibeNodeMessage``.

        The iterator MUST terminate after the equivalent of a
        ``ResultMessage`` (turn complete).  It must NOT keep the
        generator alive for the subprocess lifetime.

        Args:
            client: Handle from ``create_session()``.

        Yields:
            ``VibeNodeMessage`` instances.  ``None`` / unknown message
            types are filtered out internally -- the caller never
            sees ``None``.

        Claude implementation (session_manager.py L1301, L1727):
            ``async for message in client.receive_response()``
            with ``None`` filtering (from ``safe_parse_message`` patch)
            and normalization to ``VibeNodeMessage``.
        """
        ...

    @abstractmethod
    async def interrupt(self, client: Any) -> None:
        """Signal the backend to stop the current turn.

        Args:
            client: Handle from ``create_session()``.

        Claude implementation (session_manager.py L1997):
            ``await info.client.interrupt()``
        """
        ...

    @abstractmethod
    async def disconnect(self, client: Any) -> None:
        """Disconnect and clean up the client.

        Args:
            client: Handle from ``create_session()``.

        Claude implementation (session_manager.py L2183):
            ``await info.client.disconnect()``
        """
        ...

    @abstractmethod
    def extract_process_pid(self, client: Any) -> int:
        """Extract the backend subprocess PID for orphan cleanup.

        Returns 0 if there is no subprocess or it has already exited.

        Args:
            client: Handle from ``create_session()``.

        Claude implementation (session_manager.py L2018-2031):
            Navigates ``client._transport`` or
            ``client._query.transport``, unwraps
            ``VibeNodeTransportAdapter.inner``, reads ``_process.pid``.
        """
        ...

    @abstractmethod
    def is_transport_alive(self, client: Any) -> bool:
        """Check if the underlying transport/connection is still alive.

        Used by the permission callback (session_manager.py L2235-2242)
        to detect dead transports and abort the turn early rather than
        letting the agent retry failed tools dozens of times.

        Args:
            client: Handle from ``create_session()``.

        Claude implementation (session_manager.py L2235-2242)::

            return (client and client._query
                    and client._query.transport
                    and client._query.transport.is_ready())
        """
        ...

    # ── Non-abstract methods with sensible defaults ──────────────────

    def apply_patches(self) -> list[str]:
        """Apply any SDK-specific patches at startup.

        Called once during ``SessionManager`` initialization.
        Returns a list of applied patch names for logging.

        Default: no-op (returns empty list).

        Override for backends that need monkey-patching.  The Claude
        implementation delegates to ``daemon/sdk_patches.py`` which
        applies ``safe_parse_message``, ``transport_adapter``, and
        ``suppress_console_windows`` patches.
        """
        return []

    def make_permission_result_allow(
        self, tool_input: dict
    ) -> PermissionResult:
        """Create an ALLOW permission result.

        Default implementation returns a standard ``PermissionResult``.
        Backends can override for SDK-specific requirements.

        Claude implementation:
            Must ensure ``updated_input`` is always a dict, never
            ``None``.  See ``sdk_transport_adapter.py`` docstring for
            the CLI 2.x crash when ``updatedInput`` is null.
        """
        return PermissionResult(
            action=PermissionAction.ALLOW,
            updated_input=tool_input if isinstance(tool_input, dict) else {},
        )

    def make_permission_result_deny(
        self, message: str = "Denied", interrupt: bool = False
    ) -> PermissionResult:
        """Create a DENY permission result.

        Default implementation returns a standard ``PermissionResult``.
        """
        return PermissionResult(
            action=PermissionAction.DENY,
            message=message,
            interrupt=interrupt,
        )
