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

    # Pre-compact callback -- set by SessionManager, not by the caller.
    # Fires BEFORE the SDK starts a compaction cycle (both manual /compact
    # and automatic context-fill compaction).  SessionManager uses this
    # to mark substatus="compacting" early so the UI shows "Compacting…"
    # during the actual work, not just briefly after the work is done
    # (compact_boundary from the SDK arrives at the END of compaction).
    # Signature::
    #     async (input_data: dict, tool_use_id: str | None, context: Any)
    #         -> dict
    #
    # The implementation MUST return an empty dict ``{}`` (the SDK's
    # ``HookJSONOutput`` shape) so the SDK proceeds with compaction
    # unaltered — this is a notification-only hook.
    pre_compact_callback: Optional[Callable] = None

    # Session Host (daemon/session_host.py).  ``hosted``: run the CLI inside
    # its own host process so it outlives a Session Engine restart;
    # ``host_sid``: the id the host's record is filed under; ``attach``: a
    # host record a previous engine left running, to reattach to instead of
    # launching anything.  Backends that cannot host ignore all three.
    hosted: bool = False
    host_sid: Optional[str] = None
    attach: Optional[dict] = None


class BackendKnowledge:
    """What the orchestration layer needs to KNOW about a backend's vocabulary,
    as opposed to what it needs to DO with it (``AgentSDK``).

    Model naming, tool names, stream markers and on-disk paths used to be
    hardcoded across ``session_manager.py``.  They live behind this object so
    the manager reads them by meaning ("the compaction marker", "the model
    family of this id") and never by the Claude CLI's literal spelling
    (``tests/test_backend_seam.py`` enforces that).  The defaults here are a
    backend that knows nothing: every lookup misses, nothing switches, nothing
    is parsed.  ``daemon/backends/claude_layout.py`` is the Claude one.

    Every member is pure and cheap: no I/O, no SDK import.
    """

    # Tools whose call schedules a deferred auto-resume of the session.
    wakeup_tool_names: frozenset = frozenset()
    # System-message subtype that marks a finished context compaction.
    compaction_marker: Optional[str] = None
    # Stream-JSON event carrying account usage windows.
    rate_limit_event_name: Optional[str] = None
    # Usage-limit auto-switch: model family -> family to continue on.
    limit_switch_families: dict = {}

    @property
    def system_markers(self) -> tuple:
        """Markers of backend-injected content inside user-role messages."""
        return ()

    # ── Model naming ──
    def strip_markers(self, model_id: str) -> str:
        return str(model_id or "").strip()

    def model_family(self, model_id: str) -> str:
        return ""

    def model_version(self, model_id: str) -> tuple:
        return ()

    def model_label(self, model_id: str) -> str:
        return self.strip_markers(model_id) or "the model"

    def newest_in_family(self, model_ids, family: str) -> str:
        return ""

    def cli_model_id(self, model: Optional[str]) -> str:
        """A recorded model id made safe to hand back to the backend."""
        return (model or "").strip()

    def newest_model_of(self, family: str, listed: list, known_ids: Callable,
                        limited_model: str = "") -> str:
        """The newest model of ``family`` to continue on, or ''."""
        return ""

    # ── Stream / disk ──
    def stream_usage(self, event_data: dict) -> dict:
        """Read a raw stream event: ``{"type", "payload", "usage_start",
        "usage"}`` where ``usage`` is the per-call usage dict or None."""
        return {"type": "", "payload": {}, "usage_start": False, "usage": None}

    def file_history_dir(self, home) -> Optional[Any]:
        """Where per-session file backups live, under ``home``; None if the
        backend has no such store."""
        return None


class TranscriptLayout:
    """Where a backend keeps a session's side files, and how its transcript
    marks an API-error stop.  Read by the watchdog jobs
    (``orphaned_workers``, ``silent_wait``, ``limit_watch``).  They obtain it
    as ``getattr(sm, "backend_layout", None) or ClaudeTranscriptLayout()``:
    the watchdog hot-reloads, so it must work against a SessionManager that
    predates this attribute.
    """

    # Glob (within ``subagents_dir``) of per-agent metadata files.
    subagent_meta_glob: str = ""

    def subagents_dir(self, root_transcript):
        """Directory of per-agent transcripts and metadata, or None."""
        return None

    def subagent_meta_id(self, filename: str) -> Optional[str]:
        """The agent id a metadata file (matching ``subagent_meta_glob``)
        belongs to, or None for any other file."""
        return None

    def subagent_transcript(self, sub_dir, agent_id: str):
        """Path of agent ``agent_id``'s own transcript inside ``sub_dir``."""
        return None

    def is_api_error_entry(self, entry) -> Optional[str]:
        """The error kind (possibly '') if ``entry`` is an API-error stop the
        backend wrote into the transcript, else None."""
        return None

    def task_output_dir(self, root_transcript):
        """Directory of background-task output files, or None."""
        return None


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

    async def set_model(self, client: Any, model: str) -> None:
        """Switch the model used for subsequent turns of a connected session.

        Non-abstract on purpose: live model switching is an optional
        capability.  Backends that support it override this method;
        the default raises so callers surface an honest "not supported"
        error instead of silently pretending the switch happened.

        Args:
            client: Handle from ``create_session()``.
            model:  Backend-specific model identifier.

        Raises:
            NotImplementedError: backend cannot switch models mid-session.
            Exception: the backend rejected the switch (callers MUST NOT
                update any recorded model state in that case).
        """
        raise NotImplementedError(
            "This backend does not support mid-session model switching"
        )

    async def set_effort(self, client: Any,
                         effort: Optional[str]) -> Optional[str]:
        """Change a connected session's thinking level (effort) in place.

        Non-abstract for the same reason as ``set_model``: a live change is
        an optional capability.  The default raises, so the caller falls back
        to relaunching the session with the level instead of pretending the
        change happened.

        Args:
            client: Handle from ``create_session()``.
            effort: A level the backend accepts, or ``None`` for the model's
                own default.

        Returns:
            The level the backend now applies to the session's current model.
            It may differ from ``effort`` (a model can clamp a level it lacks)
            or be ``None`` (a model without thinking levels).

        Raises:
            NotImplementedError: backend cannot change effort live.
            Exception: the backend rejected the change or could not confirm
                it (callers MUST NOT record the new level in that case).
        """
        raise NotImplementedError(
            "This backend does not support live thinking-level changes"
        )

    def supported_models(self, client: Any) -> list:
        """Models the connected backend process can run, newest aliases first.

        Each entry is a dict with at least ``value`` (an id or a family alias
        such as ``"opus"``) and ``resolvedModel`` (the concrete id the value
        runs).  The default reports nothing; callers then fall back to other
        sources.  Never raises.
        """
        return []

    async def applied_model(self, client: Any) -> Optional[str]:
        """The concrete model id the connected process runs now, or None when
        the backend cannot say.  Used after switching to a family alias."""
        return None

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

    @property
    def knowledge(self) -> BackendKnowledge:
        """This backend's vocabulary (see ``BackendKnowledge``)."""
        return BackendKnowledge()

    def transcript_layout(self) -> TranscriptLayout:
        """This backend's on-disk session layout (see ``TranscriptLayout``)."""
        return TranscriptLayout()

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

    # ── Session Host (daemon/session_host.py); defaults: no hosting ───

    #: True when ``create_session`` honours ``SessionOptions.hosted``.
    supports_hosting: bool = False

    def host_record(self, client: Any) -> Optional[dict]:
        """The host record of a hosted client, or None (not hosted)."""
        return None

    def host_hello(self, client: Any) -> dict:
        """What the host reported on attach (``hello_ok``), or {}."""
        return {}

    def host_release_replay(self, client: Any, skip_ids=()) -> None:
        """Let a reattached client's replayed messages through, skipping
        those whose transcript id is in ``skip_ids``."""
        return None

    async def host_detach(self, client: Any) -> None:
        """Disconnect WITHOUT ending the CLI (the engine is stopping)."""
        await self.disconnect(client)

    async def host_rename(self, client: Any, new_id: str) -> None:
        """The session adopted a new id; the host refiles its record."""
        return None

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
