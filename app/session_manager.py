"""
SessionManager -- manages Claude Code SDK sessions with a dedicated asyncio event loop.

Runs in a daemon thread. Flask routes call in via run_coroutine_threadsafe().
Permission callbacks use anyio.Event to wait (natively compatible with the
SDK's anyio task groups) and are resolved from the Flask thread via
loop.call_soon_threadsafe().
"""

import anyio
import asyncio
import json
import logging
import os
import tempfile
import threading
import time
from enum import Enum
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

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

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Ensure Claude Code CLI is discoverable.
# When the daemon is spawned with CREATE_NO_WINDOW the PATH may be stripped.
# Add the standard install location so shutil.which("claude") always works.
# ---------------------------------------------------------------------------
_local_bin = str(Path.home() / ".local" / "bin")
if _local_bin not in os.environ.get("PATH", ""):
    os.environ["PATH"] = _local_bin + os.pathsep + os.environ.get("PATH", "")

# ---------------------------------------------------------------------------
# Monkey-patch: make the SDK tolerant of unknown message types.
# The SDK raises MessageParseError for types like "rate_limit_event" which
# kills the entire receive_messages() generator. Patch parse_message to
# return None for unknown types so the generator survives.
# ---------------------------------------------------------------------------
try:
    import claude_code_sdk.client as _sdk_client_mod
    import claude_code_sdk._internal.message_parser as _sdk_parser_mod
    import claude_code_sdk._internal.query as _sdk_query_mod

    # Patch 1: Skip unknown message types (e.g. rate_limit_event)
    _original_parse_message = _sdk_parser_mod.parse_message

    def _safe_parse_message(data):
        try:
            return _original_parse_message(data)
        except Exception as e:
            if "Unknown message type" in str(e):
                logger.debug("Skipping unknown SDK message type: %s", e)
                return None
            raise

    _sdk_parser_mod.parse_message = _safe_parse_message
    _sdk_client_mod.parse_message = _safe_parse_message

    # Patch 2: Fix permission response format for CLI 2.x
    # The SDK sends {"allow": true} but CLI 2.x expects
    # {"behavior": "allow", "updatedInput": {}} (TypeScript PermissionResult)
    _original_handle_control = _sdk_query_mod.Query._handle_control_request

    async def _patched_handle_control(self, request):
        """Intercept permission responses to use CLI 2.x format."""
        request_data = request.get("request", {})
        subtype = request_data.get("subtype")

        await _original_handle_control(self, request)

    _sdk_query_mod.Query._handle_control_request = _patched_handle_control

    # Patch 2b: Fix permission response format in the original handler.
    # The SDK sends {"allow": true} but CLI 2.x expects {"behavior": "allow", "updatedInput": {}}.
    # Monkey-patch the PermissionResultAllow class to produce the right format
    # when the original _handle_control_request converts it.
    # We do this by patching the response_data construction inside the handler.
    # Since we can't easily patch just that part, we patch the entire handler
    # to fix the format but use the same flow.
    _real_original_handle = _original_handle_control

    async def _format_fixing_handle(self, request):
        """Wrap the original handler to fix the permission response format."""
        request_data = request.get("request", {})
        subtype = request_data.get("subtype")

        if subtype == "can_use_tool" and self.can_use_tool:
            # request_id is on the OUTER object, not inside request_data
            request_id = request.get("request_id")
            import json as _json
            try:
                from claude_code_sdk.types import ToolPermissionContext as _TPC2
                context = _TPC2(
                    signal=None,
                    suggestions=request_data.get("permission_suggestions", []) or [],
                )
                response = await self.can_use_tool(
                    request_data["tool_name"],
                    request_data["input"],
                    context,
                )
                # Use CLI 2.x format
                if isinstance(response, PermissionResultAllow):
                    response_data = {
                        "behavior": "allow",
                        "updatedInput": response.updated_input if response.updated_input is not None else request_data.get("input", {}),
                    }
                elif isinstance(response, PermissionResultDeny):
                    response_data = {
                        "behavior": "deny",
                        "message": response.message or "Denied",
                    }
                else:
                    raise TypeError(f"Unexpected: {type(response)}")

                success_response = {
                    "type": "control_response",
                    "response": {
                        "subtype": "success",
                        "request_id": request_id,
                        "response": response_data,
                    },
                }
                await self.transport.write(_json.dumps(success_response) + "\n")
            except Exception as e:
                logger.exception("Permission error: %s", e)
                err = {
                    "type": "control_response",
                    "response": {"subtype": "error", "request_id": request_id, "error": str(e)},
                }
                await self.transport.write(_json.dumps(err) + "\n")
        else:
            await _real_original_handle(self, request)

    _sdk_query_mod.Query._handle_control_request = _format_fixing_handle

    # Patch 3: Don't close stdin after empty stream when can_use_tool is set.
    # The SDK calls end_input() after iterating the prompt stream, which closes
    # stdin and makes the CLI exit. We need stdin to stay open for the control
    # protocol (permission prompts) and for query() to send follow-up messages.
    _original_stream_input = _sdk_query_mod.Query.stream_input

    async def _patched_stream_input(self, stream):
        import json as _json2
        try:
            async for message in stream:
                if self._closed:
                    break
                await self.transport.write(_json2.dumps(message) + "\n")
            # DON'T call end_input() — keep stdin open for queries and control
            # The original code does: await self.transport.end_input()
            # We skip this so the CLI stays alive
            logger.debug("stream_input: finished iterating, keeping stdin open")
        except Exception as e:
            logger.debug(f"Error streaming input: {e}")

    _sdk_query_mod.Query.stream_input = _patched_stream_input

except Exception as _patch_err:
    logger.warning("Could not patch SDK: %s", _patch_err)


class SessionState(str, Enum):
    STARTING = "starting"
    WORKING = "working"
    WAITING = "waiting"
    IDLE = "idle"
    STOPPED = "stopped"


@dataclass
class LogEntry:
    """A single log entry for the session timeline."""
    kind: str          # 'user', 'asst', 'tool_use', 'tool_result', 'system', 'stream'
    text: str = ""
    name: str = ""     # tool name (for tool_use)
    desc: str = ""     # tool description/summary (for tool_use)
    id: str = ""       # tool_use id
    tool_use_id: str = ""  # for tool_result, references the tool_use
    is_error: bool = False
    timestamp: float = field(default_factory=time.time)

    def to_dict(self) -> dict:
        d = {"kind": self.kind}
        if self.text:
            d["text"] = self.text
        if self.name:
            d["name"] = self.name
        if self.desc:
            d["desc"] = self.desc
        if self.id:
            d["id"] = self.id
        if self.tool_use_id:
            d["tool_use_id"] = self.tool_use_id
        if self.is_error:
            d["is_error"] = True
        d["timestamp"] = self.timestamp
        return d


@dataclass
class SessionInfo:
    """Tracks the state and data of one SDK session."""
    session_id: str
    state: SessionState = SessionState.STARTING
    name: str = ""
    cwd: str = ""
    model: str = ""
    session_type: str = ""  # "planner" for AI task planner sessions (hidden from UI)
    cost_usd: float = 0.0
    error: Optional[str] = None
    entries: list = field(default_factory=list)
    client: Optional[ClaudeSDKClient] = None
    task: Optional[asyncio.Task] = None
    pending_permission: Optional[tuple] = None  # (anyio.Event, result_holder_list)
    pending_tool_name: str = ""
    pending_tool_input: dict = field(default_factory=dict)
    always_allowed_tools: set = field(default_factory=set)
    working_since: float = 0.0  # time.time() when state last became WORKING
    substatus: str = ""  # e.g. "compacting" — sub-state shown in UI while WORKING
    usage: dict = field(default_factory=dict)  # token usage from last ResultMessage
    _lock: threading.Lock = field(default_factory=threading.Lock)

    def to_state_dict(self) -> dict:
        d = {
            "session_id": self.session_id,
            "state": self.state.value,
            "cost_usd": self.cost_usd,
            "error": self.error,
            "name": self.name,
            "cwd": self.cwd,
            "model": self.model,
            "session_type": self.session_type,
            "working_since": self.working_since if self.state == SessionState.WORKING else 0,
            "entry_count": len(self.entries),
        }
        if self.substatus:
            d["substatus"] = self.substatus
        if self.usage:
            d["usage"] = self.usage
        # Include permission details for WAITING sessions so reconnecting
        # clients can display the permission prompt
        if self.state == SessionState.WAITING and self.pending_tool_name:
            d["permission"] = {
                "tool_name": self.pending_tool_name,
                "tool_input": self.pending_tool_input,
            }
        return d


# ---------------------------------------------------------------------------
# Detect SDK-injected system content in UserMessage text blocks.
# ---------------------------------------------------------------------------
_SYSTEM_CONTENT_MARKERS = (
    "This session is being continued from a previous conversation",
    "<system-reminder>",
    "<local-command-stdout>",
    "<local-command-caveat>",
    "<command-name>",
    "<command-message>",
    "<command-args>",
)

def _is_system_content(text: str) -> bool:
    """Return True if text looks like SDK/CLI system content, not human input."""
    for marker in _SYSTEM_CONTENT_MARKERS:
        if marker in text:
            return True
    return False

def _system_content_label(text: str) -> str:
    """Extract a short human-readable label for system content."""
    if "This session is being continued from a previous conversation" in text:
        return "Session continued from previous conversation"
    if "<command-name>" in text:
        import re
        m = re.search(r'<command-name>(/?\w+)</command-name>', text)
        cmd = m.group(1) if m else "command"
        m2 = re.search(r'<local-command-stdout>(.*?)</local-command-stdout>', text, re.DOTALL)
        stdout = m2.group(1).strip() if m2 else ""
        if stdout:
            return f"{cmd}: {stdout[:100]}"
        return f"Local command: {cmd}"
    if "<local-command-stdout>" in text:
        import re
        m = re.search(r'<local-command-stdout>(.*?)</local-command-stdout>', text, re.DOTALL)
        return f"Command output: {(m.group(1).strip()[:100]) if m else '...'}"
    return "System message"


# ---------------------------------------------------------------------------
# Registry file for crash recovery
# ---------------------------------------------------------------------------
_REGISTRY_PATH = Path.home() / ".claude" / "gui_active_sessions.json"

# Maximum age (seconds) for a session to be eligible for recovery
_MAX_RECOVERY_AGE = 3600  # 1 hour


class SessionManager:
    """Manages all Claude Code SDK sessions on a dedicated asyncio event loop."""

    def __init__(self):
        self._sessions: dict[str, SessionInfo] = {}
        self._id_aliases: dict[str, str] = {}  # old_id -> new_id for SDK remaps
        self._lock = threading.Lock()
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._thread: Optional[threading.Thread] = None
        self._socketio = None
        self._started = False
        self._registry_timer: Optional[threading.Timer] = None
        self._registry_dirty = False
        # Permission policy (synced from browser)
        self._permission_policy = "manual"     # 'manual' | 'auto' | 'custom'
        self._custom_rules = {}                # dict of custom auto-approve rules

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def start(self, socketio, app=None) -> None:
        """Start the background event loop thread. Called once at app startup."""
        if self._started:
            return
        self._socketio = socketio
        self._app = app
        self._loop = asyncio.new_event_loop()
        self._thread = threading.Thread(
            target=self._run_loop, daemon=True, name="session-manager-loop"
        )
        self._thread.start()
        self._started = True
        logger.info("SessionManager started")

        # Recover sessions from a previous crash (non-blocking background task)
        threading.Thread(
            target=self._recover_sessions, daemon=True,
            name="session-recovery"
        ).start()

    def _run_loop(self) -> None:
        """Entry point for the background thread."""
        asyncio.set_event_loop(self._loop)
        self._loop.run_forever()

    def stop(self) -> None:
        """Stop the event loop and all sessions. Called on shutdown."""
        if not self._started:
            return
        # Cancel any pending registry save timer
        if self._registry_timer:
            self._registry_timer.cancel()
            self._registry_timer = None
        # Close all sessions
        with self._lock:
            session_ids = list(self._sessions.keys())
        for sid in session_ids:
            try:
                self._run_sync(self._close_session(sid))
            except Exception:
                pass
        # Clear the registry since all sessions are intentionally stopped
        self._save_registry_now()
        if self._loop and self._loop.is_running():
            self._loop.call_soon_threadsafe(self._loop.stop)
        if self._thread:
            self._thread.join(timeout=5)
        self._started = False

    # ------------------------------------------------------------------
    # Thread-safe bridge: Flask (sync) -> asyncio loop
    # ------------------------------------------------------------------

    def _run_sync(self, coro, timeout=30):
        """Submit a coroutine to the event loop and wait for the result."""
        if not self._loop or not self._loop.is_running():
            raise RuntimeError("SessionManager event loop is not running")
        future = asyncio.run_coroutine_threadsafe(coro, self._loop)
        return future.result(timeout=timeout)

    # ------------------------------------------------------------------
    # Public API (called from Flask routes / WS handlers)
    # ------------------------------------------------------------------

    def _resolve_id(self, session_id: str) -> str:
        """Resolve a session ID through aliases (old_id -> new_id)."""
        return self._id_aliases.get(session_id, session_id)

    def start_session(
        self, session_id: str, prompt: str = "", cwd: str = "",
        name: str = "", resume: bool = False,
        model: Optional[str] = None, system_prompt: Optional[str] = None,
        max_turns: Optional[int] = None, allowed_tools: Optional[list] = None,
        permission_mode: Optional[str] = None,
        session_type: str = "",
    ) -> dict:
        """Start or resume an SDK session. Returns immediately."""
        with self._lock:
            if session_id in self._sessions:
                existing = self._sessions[session_id]
                if existing.state not in (SessionState.STOPPED,):
                    return {"ok": False, "error": "Session already running"}
                # Allow restart of a stopped session
                del self._sessions[session_id]

        info = SessionInfo(
            session_id=session_id,
            name=name,
            cwd=cwd,
            model=model or "",
            state=SessionState.STARTING,
            session_type=session_type or "",
        )
        # Store the user prompt immediately so get_entries() returns it
        # before _drive_session starts. After a page refresh there's no
        # optimistic bubble, so the log fetch must find this entry.
        if prompt and not resume:
            with info._lock:
                info.entries.append(LogEntry(kind="user", text=prompt[:20000]))
        with self._lock:
            self._sessions[session_id] = info

        self._emit_state(info)
        self._schedule_registry_save()

        # Verify the event loop is alive before submitting
        if not self._loop or not self._loop.is_running():
            info.state = SessionState.STOPPED
            info.error = "Session manager event loop is not running"
            self._emit_state(info)
            return {"ok": False, "error": "Session manager event loop is not running"}

        # Launch the async session driver
        asyncio.run_coroutine_threadsafe(
            self._drive_session(
                session_id, prompt, cwd, resume,
                model=model, system_prompt=system_prompt,
                max_turns=max_turns, allowed_tools=allowed_tools,
                permission_mode=permission_mode,
            ),
            self._loop,
        )
        return {"ok": True}

    def send_message(self, session_id: str, text: str) -> dict:
        """Send a follow-up message to an idle session."""
        session_id = self._resolve_id(session_id)
        with self._lock:
            info = self._sessions.get(session_id)
        if not info:
            return {"ok": False, "error": "Session not found"}

        # Atomic state check + set under per-session lock
        with info._lock:
            if info.state != SessionState.IDLE:
                return {"ok": False, "error": f"Session is {info.state.value}, not idle"}
            # Skip slash commands — they're internal CLI directives, not user chat
            _stripped = text.strip()
            if not (_stripped.startswith('/') and ' ' not in _stripped):
                info.entries.append(LogEntry(kind="user", text=text))
            info.state = SessionState.WORKING
            # Set compacting substatus immediately so the state event carries it
            if _stripped == '/compact':
                info.substatus = "compacting"

        self._emit_state(info)

        asyncio.run_coroutine_threadsafe(
            self._send_query(session_id, text), self._loop
        )
        return {"ok": True}

    def resolve_permission(self, session_id: str, allow: bool, always: bool = False) -> dict:
        """Resolve a pending permission request.

        Called from a Flask-SocketIO handler thread. Uses
        loop.call_soon_threadsafe to set the anyio.Event on the correct
        event loop so the waiting callback resumes immediately.
        """
        session_id = self._resolve_id(session_id)
        with self._lock:
            info = self._sessions.get(session_id)
        if not info:
            return {"ok": False, "error": "Session not found"}
        if info.state != SessionState.WAITING:
            return {"ok": False, "error": f"Session is {info.state.value}, not waiting"}
        if not info.pending_permission:
            return {"ok": False, "error": "No pending permission"}

        if allow:
            result = PermissionResultAllow(updated_input=None, updated_permissions=None)
        else:
            result = PermissionResultDeny(message="User denied permission", interrupt=False)

        # Resolve the permission by setting the anyio Event.
        perm_tuple = info.pending_permission  # (anyio.Event, result_holder)
        info.pending_permission = None

        if isinstance(perm_tuple, tuple) and len(perm_tuple) == 2:
            perm_event, result_holder = perm_tuple
            result_holder[0] = (result, always)
            perm_event.set()  # threading.Event.set() is fully thread-safe

        return {"ok": True}

    def set_permission_policy(self, policy: str, custom_rules: dict = None) -> None:
        """Update the permission policy (synced from browser)."""
        if policy not in ("manual", "auto", "custom"):
            return
        self._permission_policy = policy
        self._custom_rules = custom_rules or {}
        logger.info("Permission policy updated: %s", policy)

    def _should_auto_approve(self, tool_name: str, tool_input: dict) -> bool:
        """Check if a tool use should be auto-approved based on the current policy."""
        policy = self._permission_policy

        if policy == "manual":
            return False
        if policy == "auto":
            return True
        if policy == "custom":
            rules = self._custom_rules
            tool_lower = (tool_name or "").lower()

            if rules.get("approveAllReads") and tool_lower == "read":
                return True
            if rules.get("approveProjectReads") and tool_lower == "read":
                return True
            if rules.get("approveAllBash") and tool_lower == "bash":
                return True
            if rules.get("approveProjectWrites") and tool_lower in ("write", "edit"):
                return True
            if rules.get("approveGlob") and tool_lower == "glob":
                return True
            if rules.get("approveGrep") and tool_lower == "grep":
                return True

            # Custom regex pattern
            custom_pattern = rules.get("customPattern", "")
            if custom_pattern:
                import re
                try:
                    # Build a question string similar to the frontend
                    desc = ""
                    if isinstance(tool_input, dict):
                        desc = tool_input.get("command", "") or tool_input.get("file_path", "") or tool_input.get("path", "") or tool_input.get("pattern", "")
                    question = f"Claude wants to use {tool_name}:\n\n{desc}"
                    if re.search(custom_pattern, question, re.IGNORECASE):
                        return True
                except re.error:
                    pass

        return False

    def interrupt_session(self, session_id: str) -> dict:
        """Interrupt a running session."""
        session_id = self._resolve_id(session_id)
        with self._lock:
            info = self._sessions.get(session_id)
        if not info:
            return {"ok": False, "error": "Session not found"}
        if info.state == SessionState.STOPPED:
            return {"ok": False, "error": "Session already stopped"}

        asyncio.run_coroutine_threadsafe(
            self._interrupt_session(session_id), self._loop
        )
        return {"ok": True}

    def close_session(self, session_id: str) -> dict:
        """Close and disconnect an SDK session."""
        session_id = self._resolve_id(session_id)
        with self._lock:
            info = self._sessions.get(session_id)
        if not info:
            return {"ok": False, "error": "Session not found"}

        asyncio.run_coroutine_threadsafe(
            self._close_session(session_id), self._loop
        )
        return {"ok": True}

    def close_session_sync(self, session_id: str, timeout: float = 5.0) -> dict:
        """Close an SDK session and block until the disconnect finishes.

        Used by delete endpoints so the CLI subprocess is fully dead before
        we remove the .jsonl file (prevents the subprocess from recreating it).
        """
        session_id = self._resolve_id(session_id)
        with self._lock:
            info = self._sessions.get(session_id)
        if not info:
            return {"ok": False, "error": "Session not found"}

        future = asyncio.run_coroutine_threadsafe(
            self._close_session(session_id), self._loop
        )
        try:
            future.result(timeout=timeout)
        except Exception as e:
            logger.warning("Timed-out or failed waiting for close of %s: %s", session_id, e)
        return {"ok": True}

    def remove_session(self, session_id: str) -> None:
        """Remove a session from the in-memory dict entirely.

        Called after deletion so recovery can never resurrect it.
        """
        with self._lock:
            self._sessions.pop(session_id, None)

    def get_all_states(self) -> list:
        """Return snapshot of all session states for initial WebSocket connect."""
        with self._lock:
            return [info.to_state_dict() for info in self._sessions.values()
                    if info.state != SessionState.STOPPED
                    and info.session_type not in ("planner", "title")]

    def get_entries(self, session_id: str, since: int = 0) -> list:
        """Return log entries for a session, optionally from an index."""
        session_id = self._resolve_id(session_id)
        with self._lock:
            info = self._sessions.get(session_id)
        if not info:
            return []
        with info._lock:
            return [e.to_dict() for e in info.entries[since:]]

    def has_session(self, session_id: str) -> bool:
        """Check if a session is managed by the SDK."""
        session_id = self._resolve_id(session_id)
        with self._lock:
            return session_id in self._sessions

    def get_session_state(self, session_id: str) -> Optional[str]:
        """Return the state string for a session, or None if not managed."""
        session_id = self._resolve_id(session_id)
        with self._lock:
            info = self._sessions.get(session_id)
        if info:
            return info.state.value
        return None

    # ------------------------------------------------------------------
    # Async internals (run on the event loop thread)
    # ------------------------------------------------------------------

    async def _drive_session(
        self, session_id: str, prompt: str, cwd: str, resume: bool,
        model: Optional[str] = None, system_prompt: Optional[str] = None,
        max_turns: Optional[int] = None, allowed_tools: Optional[list] = None,
        permission_mode: Optional[str] = None,
    ) -> None:
        """Main driver coroutine for one SDK session."""
        with self._lock:
            info = self._sessions.get(session_id)
        if not info:
            return

        try:
            options = ClaudeCodeOptions(
                cwd=cwd or None,
                resume=session_id if resume else None,
                can_use_tool=self._make_permission_callback(session_id),
                model=model or None,
                system_prompt=system_prompt or None,
                max_turns=max_turns or None,
                allowed_tools=allowed_tools or [],
                permission_mode=permission_mode or "default",
                include_partial_messages=True,
            )
            client = ClaudeSDKClient(options=options)
            info.client = client

            # Connect with no prompt. The SDK auto-sets permission_prompt_tool_name="stdio"
            # when can_use_tool is set. Prompt=None becomes _empty_stream() which is an
            # AsyncIterator, so the streaming mode check passes.
            await client.connect()

            info.state = SessionState.WORKING
            self._emit_state(info)

            # Send user's prompt to the CLI.
            # The user entry was already added in start_session() (synchronously)
            # so get_entries() returns it immediately — even before this async
            # method starts. Don't re-append it here.
            if prompt:
                await client.query(prompt)

            # Process messages (None = unknown types, skipped via monkey-patch)
            got_result = False
            async for message in client.receive_messages():
                if message is not None:
                    if isinstance(message, ResultMessage):
                        got_result = True
                    await self._process_message(session_id, message)

            # Safety net: force IDLE only if no ResultMessage was received.
            # If we got one, _process_message already handled the transition.
            if not got_result and info.state == SessionState.WORKING:
                info.state = SessionState.IDLE
                self._emit_state(info)

        except asyncio.CancelledError:
            logger.info("Session %s cancelled", session_id)
            info.state = SessionState.STOPPED
            self._emit_state(info)
        except Exception as e:
            logger.exception("Session %s error: %s", session_id, e)
            info.error = str(e)
            info.state = SessionState.STOPPED
            entry = LogEntry(kind="system", text=f"Error: {e}", is_error=True)
            with info._lock:
                info.entries.append(entry)
                entry_index = len(info.entries) - 1
            self._emit_entry(session_id, entry, entry_index)
            self._emit_state(info)

    async def _send_query(self, session_id: str, text: str) -> None:
        """Send a follow-up query to an already-connected session."""
        with self._lock:
            info = self._sessions.get(session_id)
        if not info or not info.client:
            # send_message already set state to WORKING before scheduling us.
            # If client is gone, force back to IDLE so the session isn't stuck.
            if info and info.state == SessionState.WORKING:
                logger.error("_send_query: %s has no client — forcing IDLE", session_id)
                info.state = SessionState.IDLE
                info.error = "Session disconnected"
                entry = LogEntry(kind="system", text="Session disconnected — no SDK client", is_error=True)
                with info._lock:
                    info.entries.append(entry)
                self._emit_state(info)
            return

        try:
            await info.client.query(text)

            # Process response messages (None = unknown types, skipped)
            got_result = False
            info.usage.pop('_per_call', None)  # clear stale per-call marker from previous turn
            async for message in info.client.receive_response():
                if message is not None:
                    if isinstance(message, ResultMessage):
                        got_result = True
                    await self._process_message(session_id, message)

            # Safety net: force IDLE if no ResultMessage was received.
            # Skip if ResultMessage already handled the transition (and
            # may have dispatched a queued message setting WORKING).
            if not got_result and info.state not in (SessionState.STOPPED, SessionState.IDLE):
                info.state = SessionState.IDLE
                self._emit_state(info)

        except asyncio.CancelledError:
            # Don't leave session stuck in WORKING on cancel
            if info.state == SessionState.WORKING:
                info.state = SessionState.IDLE
                self._emit_state(info)
        except Exception as e:
            logger.exception("Send query error for %s: %s", session_id, e)
            info.error = str(e)
            entry = LogEntry(kind="system", text=f"Error: {e}", is_error=True)
            with info._lock:
                info.entries.append(entry)
                entry_index = len(info.entries) - 1
            self._emit_entry(session_id, entry, entry_index)
            # Transition to IDLE (not STOPPED) so the session stays usable.
            info.state = SessionState.IDLE
            self._emit_state(info)

    async def _interrupt_session(self, session_id: str) -> None:
        """Interrupt a running session."""
        with self._lock:
            info = self._sessions.get(session_id)
        if not info or not info.client:
            return

        try:
            # If waiting for permission, resolve with deny
            if info.pending_permission:
                perm_tuple = info.pending_permission
                info.pending_permission = None
                if isinstance(perm_tuple, tuple) and len(perm_tuple) == 2:
                    perm_event, result_holder = perm_tuple
                    deny = PermissionResultDeny(message="Interrupted by user", interrupt=True)
                    result_holder[0] = (deny, False)
                    perm_event.set()

            try:
                await info.client.interrupt()
            except Exception as int_err:
                logger.warning("interrupt() failed for %s: %s, forcing disconnect", session_id, int_err)
                try:
                    await info.client.disconnect()
                except Exception:
                    pass

            info.state = SessionState.IDLE
            entry = LogEntry(kind="system", text="Session interrupted by user")
            with info._lock:
                info.entries.append(entry)
                entry_index = len(info.entries) - 1
            self._emit_entry(session_id, entry, entry_index)
            self._emit_state(info)
        except Exception as e:
            logger.exception("Interrupt error for %s: %s", session_id, e)

    async def _close_session(self, session_id: str) -> None:
        """Disconnect and clean up a session."""
        with self._lock:
            info = self._sessions.get(session_id)
        if not info:
            return

        try:
            # Cancel pending permission if any
            if info.pending_permission:
                perm_tuple = info.pending_permission
                info.pending_permission = None
                if isinstance(perm_tuple, tuple) and len(perm_tuple) == 2:
                    perm_event, result_holder = perm_tuple
                    deny = PermissionResultDeny(message="Session closed", interrupt=True)
                    result_holder[0] = (deny, False)
                    perm_event.set()

            # Cancel the driving task if running
            if info.task and not info.task.done():
                info.task.cancel()

            # Disconnect the client
            if info.client:
                try:
                    await info.client.disconnect()
                except Exception:
                    pass

            info.state = SessionState.STOPPED
            info.client = None
            self._emit_state(info)
            self._schedule_registry_save()
        except Exception as e:
            logger.exception("Close error for %s: %s", session_id, e)
            info.state = SessionState.STOPPED
            self._emit_state(info)
            self._schedule_registry_save()

    # ------------------------------------------------------------------
    # Permission callback
    # ------------------------------------------------------------------

    def _make_permission_callback(self, session_id: str):
        """Create the can_use_tool callback for a specific session.

        The callback runs inside an anyio task group (the SDK's control
        handler). We use anyio.Event for waiting, which properly yields to
        the anyio scheduler instead of blocking the thread.

        resolve_permission() (called from a Flask thread) sets the event
        via loop.call_soon_threadsafe() so the waiting coroutine wakes up
        immediately.
        """
        manager = self

        async def can_use_tool(tool_name, tool_input, context):
            with manager._lock:
                info = manager._sessions.get(session_id)
            if not info:
                return PermissionResultDeny(message="Session not found", interrupt=True)

            # Auto-approve if user previously clicked "Always" for this tool
            if tool_name in info.always_allowed_tools:
                return PermissionResultAllow()

            # Server-side policy check -- resolve without browser round-trip
            if manager._should_auto_approve(tool_name, tool_input if isinstance(tool_input, dict) else {}):
                logger.debug("Auto-approved %s via server policy", tool_name)
                return PermissionResultAllow()

            # Use threading.Event (fully thread-safe) with anyio.sleep polling.
            # anyio.Event + call_soon_threadsafe doesn't reliably wake the waiter.
            perm_event = threading.Event()
            perm_result_holder = [None]  # [0] = (PermissionResult, always)
            info.pending_permission = (perm_event, perm_result_holder)
            info.pending_tool_name = tool_name
            info.pending_tool_input = tool_input if isinstance(tool_input, dict) else {}

            # Set state to WAITING
            prev_state = info.state
            info.state = SessionState.WAITING

            # Emit permission from a thread with Flask app context.
            # socketio.emit doesn't work from anyio task group context.
            perm_data = {
                'session_id': session_id,
                'tool_name': tool_name,
                'tool_input': info.pending_tool_input,
            }
            state_data = info.to_state_dict()
            _sio = manager._socketio
            _app = manager._app  # Flask app reference

            def _bg_emit():
                try:
                    with _app.app_context():
                        _sio.emit('session_state', state_data)
                        _sio.emit('session_permission', perm_data)
                except Exception as e:
                    logger.error("Permission emit error: %s", e)

            t = threading.Thread(target=_bg_emit, daemon=True)
            t.start()
            t.join(timeout=5)  # Wait for emit to complete

            try:
                # Poll threading.Event. Use anyio.sleep to yield to scheduler.
                # If that doesn't work, fall back to asyncio.sleep.
                for _poll_i in range(36000):  # 1 hour max
                    if perm_event.is_set():
                        break
                    try:
                        await anyio.sleep(0.1)
                    except Exception:
                        await asyncio.sleep(0.1)

                result_tuple = perm_result_holder[0]
                if result_tuple is None:
                    result_tuple = (PermissionResultDeny(message="No result"), False)
                permission_result, always = result_tuple

                # Remember "Always Allow" for this tool for the rest of the session
                if always and isinstance(permission_result, PermissionResultAllow):
                    info.always_allowed_tools.add(tool_name)

                # Clean up permission state
                info.pending_permission = None
                info.pending_tool_name = ""
                info.pending_tool_input = {}

                # Back to working — but NOT if this was an interrupt.
                # The interrupt handler already set state to IDLE; emitting
                # WORKING here would race with it and flip the UI back.
                is_interrupt = (
                    isinstance(permission_result, PermissionResultDeny)
                    and getattr(permission_result, 'interrupt', False)
                )
                if not is_interrupt:
                    info.state = SessionState.WORKING
                    manager._emit_state(info)

                return permission_result

            except BaseException:
                # Handles cancellation (anyio uses BaseException subclasses)
                info.pending_permission = None
                info.pending_tool_name = ""
                info.pending_tool_input = {}
                info.state = prev_state
                manager._emit_state(info)
                return PermissionResultDeny(
                    message="Permission request cancelled", interrupt=True
                )

        return can_use_tool

    # ------------------------------------------------------------------
    # Message processing
    # ------------------------------------------------------------------

    async def _process_message(self, session_id: str, message) -> None:
        """Convert an SDK Message into log entries and emit them."""
        with self._lock:
            info = self._sessions.get(session_id)
        if not info:
            return

        if isinstance(message, AssistantMessage):
            # Don't clear compacting substatus here — only ResultMessage or
            # init SystemMessage should clear it. AssistantMessage can arrive
            # mid-compact and clearing here causes the UI to flash "Working".
            for block in (message.content if hasattr(message, 'content') else []):
                if isinstance(block, TextBlock):
                    entry = LogEntry(kind="asst", text=(block.text or "")[:50000])
                    with info._lock:
                        info.entries.append(entry)
                        entry_index = len(info.entries) - 1
                    self._emit_entry(session_id, entry, entry_index)

                elif isinstance(block, ToolUseBlock):
                    inp = block.input if hasattr(block, 'input') and isinstance(block.input, dict) else {}
                    desc = self._extract_tool_desc(inp)
                    entry = LogEntry(
                        kind="tool_use",
                        name=getattr(block, 'name', '') or '',
                        desc=desc,
                        id=getattr(block, 'id', '') or '',
                    )
                    with info._lock:
                        info.entries.append(entry)
                        entry_index = len(info.entries) - 1
                    self._emit_entry(session_id, entry, entry_index)

                elif isinstance(block, ThinkingBlock):
                    # Skip thinking blocks -- they're internal reasoning
                    pass

        elif isinstance(message, UserMessage):
            # Messages with parent_tool_use_id are sub-agent / nested tool
            # context — not from the human user. Skip text blocks for those.
            is_sub_agent = bool(getattr(message, 'parent_tool_use_id', None))

            # Normalize content: SDK can send str | list[ContentBlock].
            # Wrap plain strings in a TextBlock so the block loop works.
            raw_content = getattr(message, 'content', None) or []
            if isinstance(raw_content, str):
                blocks = [TextBlock(text=raw_content)] if raw_content.strip() else []
            elif isinstance(raw_content, list):
                blocks = raw_content
            else:
                blocks = []

            for block in blocks:
                if isinstance(block, ToolResultBlock):
                    # Extract text content from tool result
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

                    entry = LogEntry(
                        kind="tool_result",
                        text=rt[:20000],
                        tool_use_id=getattr(block, 'tool_use_id', '') or '',
                        is_error=bool(getattr(block, 'is_error', False)),
                    )
                    with info._lock:
                        info.entries.append(entry)
                        entry_index = len(info.entries) - 1
                    self._emit_entry(session_id, entry, entry_index)

                elif isinstance(block, TextBlock):
                    user_text = (block.text or "")[:20000]

                    # Skip internal/sub-agent user messages — they're not
                    # from the human and shouldn't show as user bubbles
                    if is_sub_agent:
                        logger.debug("Skipping sub-agent user text (parent_tool_use_id set)")
                        continue

                    # Skip slash commands echoed back by CLI (e.g. /compact)
                    stripped = user_text.strip()
                    if stripped.startswith('/') and ' ' not in stripped:
                        logger.debug("Skipping CLI slash command echo: %s", stripped[:50])
                        continue

                    # Detect SDK-injected system content (continuation
                    # summaries, local-command output, system-reminders).
                    # Render as collapsed system entry, not user bubble.
                    if _is_system_content(stripped):
                        logger.debug("Rendering SDK system content as system entry (len=%d)", len(stripped))
                        label = _system_content_label(stripped)
                        entry = LogEntry(kind="system", text=label)
                        with info._lock:
                            info.entries.append(entry)
                            entry_index = len(info.entries) - 1
                        self._emit_entry(session_id, entry, entry_index)
                        continue

                    # Never emit user text entries — they're already shown:
                    # - Initial prompt: added by _drive_session
                    # - Follow-ups: added by send_message
                    # - Frontend: shows optimistic bubble immediately
                    # Just add to history for get_session_log, don't emit
                    with info._lock:
                        # Only add if not a duplicate of the last user entry
                        last_user = None
                        for e in reversed(info.entries):
                            if e.kind == "user":
                                last_user = e
                                break
                        if not last_user or last_user.text.strip() != stripped:
                            info.entries.append(LogEntry(kind="user", text=user_text))

        elif isinstance(message, SystemMessage):
            subtype = getattr(message, 'subtype', '') or ''
            data = getattr(message, 'data', {}) or {}
            logger.info("SystemMessage subtype=%s keys=%s", subtype, list(data.keys())[:10])

            # Detect compaction events — CLI sends "compact_boundary" subtype
            if subtype == 'compact_boundary':
                compact_meta = data.get('compactMetadata', {})
                pre_tokens = compact_meta.get('preTokens', 0)
                trigger = compact_meta.get('trigger', 'auto')
                logger.info("Compact boundary: trigger=%s preTokens=%d", trigger, pre_tokens)

                info.substatus = "compacting"
                if pre_tokens:
                    info.usage['pre_compact_tokens'] = pre_tokens
                self._emit_state(info)

                tk_str = f"{pre_tokens // 1000}k" if pre_tokens >= 1000 else str(pre_tokens)
                label = f"Compacting context ({tk_str} tokens, {trigger})…"
                entry = LogEntry(kind="system", text=label)
                with info._lock:
                    info.entries.append(entry)
                    entry_index = len(info.entries) - 1
                self._emit_entry(session_id, entry, entry_index)

            elif subtype == 'turn_duration':
                logger.info("Turn duration for %s: %s", session_id,
                            {k: v for k, v in data.items() if k != 'type'})

            elif subtype == 'init' and info.substatus == 'compacting':
                info.substatus = ""
                self._emit_state(info)
                entry = LogEntry(kind="system", text="Context compacted")
                with info._lock:
                    info.entries.append(entry)
                    entry_index = len(info.entries) - 1
                self._emit_entry(session_id, entry, entry_index)

        elif isinstance(message, ResultMessage):
            # Clear substatus on result
            if info.substatus:
                info.substatus = ""

            info.cost_usd = getattr(message, 'total_cost_usd', 0.0) or 0.0

            # Extract token usage from ResultMessage.
            # ResultMessage.usage is cumulative across the session — if we have
            # per-call data from a message_start StreamEvent, keep it.
            raw_usage = getattr(message, 'usage', None)
            if raw_usage and isinstance(raw_usage, dict):
                prev_pct = info.usage.get('pre_compact_tokens')
                if not info.usage.get('_per_call'):
                    info.usage = dict(raw_usage)
                if prev_pct and 'pre_compact_tokens' not in info.usage:
                    info.usage['pre_compact_tokens'] = prev_pct
                logger.info("Usage for %s: %s (per_call=%s)", session_id,
                            {k: v for k, v in raw_usage.items() if isinstance(v, (int, float))},
                            bool(info.usage.get('_per_call')))

            # Extract session timing metadata
            duration_ms = getattr(message, 'duration_ms', 0) or 0
            num_turns = getattr(message, 'num_turns', 0) or 0
            if duration_ms or num_turns:
                info.usage['duration_ms'] = duration_ms
                info.usage['num_turns'] = num_turns

            is_error = getattr(message, 'is_error', False)
            if is_error:
                info.error = "Session ended with error"
                entry = LogEntry(kind="system", text="Session ended with error", is_error=True)
                with info._lock:
                    info.entries.append(entry)
                    entry_index = len(info.entries) - 1
                self._emit_entry(session_id, entry, entry_index)

            # Remap session ID if the SDK assigned a different one
            result_session_id = getattr(message, 'session_id', None)
            if result_session_id and result_session_id != session_id:
                logger.info(
                    "SDK assigned session_id %s (we used %s) — remapping",
                    result_session_id, session_id
                )
                # Update the session info and remap in _sessions dict
                info.session_id = result_session_id
                with self._lock:
                    self._sessions[result_session_id] = info
                    if session_id in self._sessions:
                        del self._sessions[session_id]
                    self._id_aliases[session_id] = result_session_id

                # Remap kanban task-session links so the board
                # still references the correct session after the ID change
                try:
                    from .db import create_repository
                    repo = create_repository()
                    repo.remap_session(session_id, result_session_id)
                except Exception:
                    pass  # kanban DB not available or no linked task

                # Notify frontend to update its references (URL, activeId, etc.)
                if self._socketio:
                    try:
                        self._socketio.emit(
                            'session_id_remapped',
                            {'old_id': session_id, 'new_id': result_session_id}
                        )
                    except Exception as emit_err:
                        logger.error("session_id_remapped emit failed: %s", emit_err)

            info.state = SessionState.IDLE
            self._emit_state(info)

        elif isinstance(message, StreamEvent):
            # Forward raw streaming events for partial message display
            event_data = {}
            if hasattr(message, 'event'):
                event_data['event'] = message.event
            if hasattr(message, 'data'):
                event_data['data'] = message.data

            # Extract per-call context usage from message_start events.
            # NOTE: message.event is a STRING like "message_start", while
            # message.data is the dict payload with the actual content.
            # This is the AUTHORITATIVE context window size.
            evt_type = event_data.get('event', '')
            evt_data = event_data.get('data') or {}
            is_message_start = (
                evt_type == 'message_start'
                or (isinstance(evt_data, dict) and evt_data.get('type') == 'message_start')
            )
            if is_message_start and isinstance(evt_data, dict):
                msg = evt_data.get('message', {})
                if isinstance(msg, dict) and 'usage' in msg:
                    call_usage = msg['usage']
                    if isinstance(call_usage, dict):
                        prev_pct = info.usage.get('pre_compact_tokens')
                        info.usage = dict(call_usage)
                        info.usage['_per_call'] = True
                        if prev_pct:
                            info.usage['pre_compact_tokens'] = prev_pct
                        logger.debug("Per-call usage for %s: input=%s cache_read=%s cache_create=%s",
                                     session_id, call_usage.get('input_tokens'),
                                     call_usage.get('cache_read_input_tokens'),
                                     call_usage.get('cache_creation_input_tokens'))
                        if self._socketio:
                            self._socketio.emit('session_usage', {
                                'session_id': session_id,
                                'usage': info.usage,
                            })

            if self._socketio:
                self._socketio.emit('stream_event', {
                    'session_id': session_id,
                    'event': event_data,
                })

    @staticmethod
    def _extract_tool_desc(inp: dict) -> str:
        """Extract a human-readable description from tool input."""
        if "command" in inp:
            return str(inp["command"])[:300]
        elif "file_path" in inp:
            desc = str(inp["file_path"])
            if "content" in inp:
                desc += f" (write {len(str(inp.get('content', '')))} chars)"
            return desc
        elif "path" in inp:
            desc = str(inp["path"])
            if "content" in inp:
                desc += f" (write {len(str(inp.get('content', '')))} chars)"
            return desc
        elif "pattern" in inp:
            return str(inp["pattern"])[:200]
        elif inp:
            first_key = next(iter(inp))
            return f"{first_key}: {str(inp[first_key])[:200]}"
        return ""

    # ------------------------------------------------------------------
    # Persistent session registry (crash recovery)
    # ------------------------------------------------------------------

    def _load_registry(self) -> dict:
        """Read the session registry from disk. Returns empty dict on error."""
        try:
            if _REGISTRY_PATH.exists():
                data = json.loads(_REGISTRY_PATH.read_text(encoding="utf-8"))
                if isinstance(data, dict) and "sessions" in data:
                    return data
        except Exception as e:
            logger.warning("Failed to load session registry: %s", e)
        return {"sessions": {}}

    def _save_registry_now(self) -> None:
        """Write the current session state to the registry file atomically.

        Only includes non-STOPPED sessions so that on recovery we know
        which sessions were still alive when the server went down.
        """
        try:
            _REGISTRY_PATH.parent.mkdir(parents=True, exist_ok=True)
            sessions_data = {}
            with self._lock:
                for sid, info in self._sessions.items():
                    if info.state == SessionState.STOPPED:
                        continue
                    sessions_data[sid] = {
                        "name": info.name,
                        "cwd": info.cwd,
                        "model": info.model,
                        "session_type": info.session_type,
                        "state": info.state.value,
                        "started_at": (
                            info.entries[0].timestamp if info.entries else time.time()
                        ),
                        "last_activity": time.time(),
                    }
            registry = {"sessions": sessions_data}
            payload = json.dumps(registry, indent=2, ensure_ascii=False)

            # Atomic write: write to a temp file then rename
            tmp_fd, tmp_path = tempfile.mkstemp(
                dir=str(_REGISTRY_PATH.parent), suffix=".tmp"
            )
            try:
                os.write(tmp_fd, payload.encode("utf-8"))
                os.close(tmp_fd)
                # On Windows, os.rename fails if destination exists; use os.replace
                os.replace(tmp_path, str(_REGISTRY_PATH))
            except Exception:
                try:
                    os.close(tmp_fd)
                except OSError:
                    pass
                try:
                    os.unlink(tmp_path)
                except OSError:
                    pass
                raise
        except Exception as e:
            logger.warning("Failed to save session registry: %s", e)

    def _schedule_registry_save(self) -> None:
        """Debounced save -- batches writes so we don't hit disk on every event.

        If a timer is already pending, skip (the pending save will capture
        the latest state).  Otherwise set a 3-second timer.
        """
        if self._registry_timer and self._registry_timer.is_alive():
            # A save is already scheduled; it will pick up the newest state
            return
        self._registry_timer = threading.Timer(3.0, self._save_registry_now)
        self._registry_timer.daemon = True
        self._registry_timer.start()

    def _recover_sessions(self) -> None:
        """Recover sessions that were active before a crash.

        Called once at startup in a background thread. Reads the registry,
        filters out stale or stopped entries, and resumes each one via the
        SDK's --resume flag.
        """
        try:
            registry = self._load_registry()
            sessions = registry.get("sessions", {})
            if not sessions:
                logger.debug("No sessions to recover from registry")
                return

            now = time.time()
            recovered = 0
            for sid, meta in sessions.items():
                state = meta.get("state", "stopped")
                # Only recover sessions that were mid-task (working/waiting).
                # Idle sessions were done — no need to resume them.
                if state not in ("working", "waiting", "starting"):
                    continue

                last_activity = meta.get("last_activity", 0)
                age = now - last_activity
                if age > _MAX_RECOVERY_AGE:
                    logger.info(
                        "Skipping stale session %s (%.0f min old)", sid, age / 60
                    )
                    continue

                # Never recover planner sessions — they're disposable utility sessions
                if meta.get("session_type") == "planner":
                    continue

                name = meta.get("name", "")
                cwd = meta.get("cwd", "")
                model = meta.get("model", "")

                # Guard: if the .jsonl file was deleted (user chose to delete
                # the session), do NOT recover it — that would undo the delete.
                from .config import _sessions_dir
                jsonl_path = _sessions_dir() / f"{sid}.jsonl"
                if not jsonl_path.exists():
                    logger.info(
                        "Skipping recovery of %s — .jsonl file was deleted", sid
                    )
                    continue

                logger.info(
                    "Recovering session %s (%s) from registry", sid, name or "unnamed"
                )

                # Use start_session with resume=True to reconnect via SDK --resume
                result = self.start_session(
                    session_id=sid,
                    prompt="",       # no new prompt; just reconnect
                    cwd=cwd,
                    name=name,
                    resume=True,
                    model=model if model else None,
                )
                if result.get("ok"):
                    recovered += 1
                else:
                    logger.warning(
                        "Failed to recover session %s: %s",
                        sid, result.get("error", "unknown")
                    )

            if recovered:
                logger.info("Recovered %d session(s) from crash registry", recovered)

            # Clear the registry now that recovery is done; ongoing state
            # changes will re-populate it via _schedule_registry_save()
            # (Don't clear -- let the normal emit_state cycle keep it updated)

        except Exception as e:
            logger.exception("Session recovery failed: %s", e)

    # ------------------------------------------------------------------
    # WebSocket emission helpers
    # ------------------------------------------------------------------

    def _emit_state(self, info: SessionInfo) -> None:
        """Push session state change to all connected WebSocket clients.

        Emits directly (synchronous, thread-safe) to guarantee ordering.
        Also schedules a registry save so crash recovery data stays fresh.
        """
        # Track when session entered WORKING state for elapsed timer
        if info.state == SessionState.WORKING and info.working_since == 0.0:
            info.working_since = time.time()
        elif info.state != SessionState.WORKING:
            info.working_since = 0.0
        if self._socketio:
            data = info.to_state_dict()
            try:
                self._socketio.emit('session_state', data)
            except Exception as emit_err:
                logger.error("_emit_state socketio.emit failed for %s (state=%s): %s",
                             info.session_id, info.state, emit_err)
        # Keep the persistent registry up to date
        self._schedule_registry_save()

    def _emit_entry(self, session_id: str, entry: LogEntry, index: int) -> None:
        """Push a new log entry to all connected WebSocket clients.

        Emits directly (synchronous, thread-safe) to guarantee ordering
        and prevent entries from being dropped by client-side index dedup.
        """
        if self._socketio:
            data = {
                'session_id': session_id,
                'entry': entry.to_dict(),
                'index': index,
            }
            try:
                self._socketio.emit('session_entry', data)
            except Exception as emit_err:
                logger.error("_emit_entry socketio.emit failed for %s index=%d: %s",
                             session_id, index, emit_err)

    def _emit_permission(self, session_id: str, tool_name: str, tool_input: dict) -> None:
        """Push a permission request to all connected WebSocket clients.

        This is called from inside an anyio task group (the SDK's control
        handler), so we schedule the emit on a background thread to ensure
        it reaches the SocketIO clients.
        """
        if self._socketio:
            import threading
            data = {
                'session_id': session_id,
                'tool_name': tool_name,
                'tool_input': tool_input,
            }
            def _do_emit():
                self._socketio.emit('session_permission', data)
            threading.Thread(target=_do_emit, daemon=True).start()

    # ------------------------------------------------------------------
    # Hook-based permission helpers (for CLI 2.x which doesn't support
    # the SDK's can_use_tool callback)
    # ------------------------------------------------------------------

    def _hook_permission_start(self, session_id: str, req_id: str,
                                tool_name: str, tool_input: dict) -> None:
        """Called when a PreToolUse hook fires. Sets state to WAITING and
        pushes the permission prompt to the frontend."""
        with self._lock:
            info = self._sessions.get(session_id)
        if not info:
            # Session might not be tracked (hook fires for any Claude session)
            # Try to find by matching — for now, use the most recent WORKING session
            with self._lock:
                for sid, si in self._sessions.items():
                    if si.state == SessionState.WORKING:
                        info = si
                        session_id = sid
                        break
        if not info:
            return

        info.pending_tool_name = tool_name
        info.pending_tool_input = tool_input if isinstance(tool_input, dict) else {}
        info.state = SessionState.WAITING
        # Store the hook request ID so the WebSocket handler can resolve it
        info._hook_req_id = req_id
        self._emit_state(info)
        self._emit_permission(session_id, tool_name, info.pending_tool_input)

    def _hook_permission_end(self, session_id: str) -> None:
        """Called when a hook permission is resolved. Restores WORKING state."""
        with self._lock:
            info = self._sessions.get(session_id)
        if not info:
            with self._lock:
                for sid, si in self._sessions.items():
                    if si.state == SessionState.WAITING:
                        info = si
                        session_id = sid
                        break
        if not info:
            return

        info.pending_tool_name = ""
        info.pending_tool_input = {}
        info._hook_req_id = None
        info.state = SessionState.WORKING
        self._emit_state(info)
