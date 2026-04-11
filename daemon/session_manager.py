"""
SessionManager -- manages Claude Code SDK sessions with a dedicated asyncio event loop.

Runs in a daemon thread. External callers invoke via run_coroutine_threadsafe().
Permission callbacks use anyio.Event to wait (natively compatible with the
SDK's anyio task groups) and are resolved from the caller thread via
loop.call_soon_threadsafe().
"""

import anyio
import asyncio
import hashlib
import json
import logging
import os
import tempfile
import threading
import time
import uuid as uuid_mod
from datetime import datetime, timezone
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

            from claude_code_sdk.types import ToolPermissionContext as _TPC2
            try:
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
            except Exception as e:
                logger.exception("Permission callback error: %s", e)
                response_data = {
                    "behavior": "deny",
                    "message": str(e) or "Permission callback failed",
                }

            ctrl_response = {
                "type": "control_response",
                "response": {
                    "subtype": "success",
                    "request_id": request_id,
                    "response": response_data,
                },
            }
            try:
                await self.transport.write(_json.dumps(ctrl_response) + "\n")
            except Exception as _write_err:
                # Transport closed mid-permission (e.g. CLI subprocess died).
                # We MUST kill the CLI process AND close stdout here.  If we
                # just swallow the error, the CLI may still be alive waiting
                # for our response on stdin while the SDK waits for messages
                # on stdout — creating a deadlock that prevents the stream
                # from ending and the self-healing finally block from running.
                logger.warning(
                    "Transport write failed for permission response "
                    "(tool=%s, req=%s): %s — killing CLI + closing transport",
                    request_data.get("tool_name", "?"), request_id, _write_err,
                )
                try:
                    proc = getattr(self.transport, '_process', None)
                    if proc and proc.returncode is None:
                        proc.terminate()
                        # kill() as fallback — terminate() may not be instant
                        try:
                            proc.kill()
                        except Exception:
                            pass
                    # Mark transport as dead so no further writes are attempted
                    self.transport._ready = False
                    # Close stdout to unblock the read loop immediately.
                    # Without this, the _read_messages task may hang on Windows
                    # waiting for the pipe to close after process termination.
                    _stdout = getattr(self.transport, '_stdout_stream', None)
                    if _stdout:
                        try:
                            await _stdout.aclose()
                        except Exception:
                            pass
                except Exception:
                    pass
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
                try:
                    await self.transport.write(_json2.dumps(message) + "\n")
                except Exception as _sw_err:
                    logger.warning("stream_input: transport write failed: %s", _sw_err)
                    break
            # DON'T call end_input() — keep stdin open for queries and control
            # The original code does: await self.transport.end_input()
            # We skip this so the CLI stays alive
            logger.debug("stream_input: finished iterating, keeping stdin open")
        except Exception as e:
            logger.debug(f"Error streaming input: {e}")

    _sdk_query_mod.Query.stream_input = _patched_stream_input

except Exception as _patch_err:
    logger.warning("Could not patch SDK: %s", _patch_err)

# ---------------------------------------------------------------------------
# Monkey-patch: prevent Claude CLI subprocesses from flashing a console window.
# Patch subprocess.Popen at the lowest level so every subprocess spawned by
# the SDK (or anything else) gets CREATE_NO_WINDOW automatically.
# This is safe for concurrent use since it doesn't swap globals per-call.
#
# DO NOT change this to patch anyio.open_process or wrap the SDK's connect().
# That approach was tried and broke everything: it races when multiple sessions
# connect concurrently (they fight over the global anyio.open_process ref),
# and it breaks the SDK's control protocol initialization (60s timeout → crash).
# Patching Popen.__init__ once at import time is the only approach that works.
# ---------------------------------------------------------------------------
if os.name == "nt":
    import subprocess as _subprocess
    try:
        _original_Popen_init = _subprocess.Popen.__init__

        def _no_window_Popen_init(self, *args, **kwargs):
            # Only inject if creationflags wasn't explicitly set
            if "creationflags" not in kwargs or kwargs["creationflags"] == 0:
                kwargs["creationflags"] = kwargs.get("creationflags", 0) | _subprocess.CREATE_NO_WINDOW
            return _original_Popen_init(self, *args, **kwargs)

        _subprocess.Popen.__init__ = _no_window_Popen_init
        logger.info("Patched subprocess.Popen to suppress console windows")
    except Exception as _e:
        logger.warning("Could not patch Popen for no-window: %s", _e)


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
    session_type: str = ""  # "planner" for AI task planner sessions
    cost_usd: float = 0.0
    error: Optional[str] = None
    entries: list = field(default_factory=list)
    client: Optional[ClaudeSDKClient] = None
    task: Optional[asyncio.Task] = None
    pending_permission: Optional[tuple] = None  # (anyio.Event, result_holder_list)
    pending_tool_name: str = ""
    pending_tool_input: dict = field(default_factory=dict)
    always_allowed_tools: set = field(default_factory=set)
    almost_always_allowed_tools: set = field(default_factory=set)
    working_since: float = 0.0  # time.time() when state last became WORKING
    substatus: str = ""  # e.g. "compacting" — sub-state shown in UI while WORKING
    usage: dict = field(default_factory=dict)  # token usage from last ResultMessage
    tracked_files: set = field(default_factory=set)      # absolute paths modified by tools
    file_versions: dict = field(default_factory=dict)    # file_path -> backup version counter
    _last_hashes: dict = field(default_factory=dict)     # file_path -> last backed-up content hash
    _pre_turn_mtimes: dict = field(default_factory=dict) # file_path -> mtime before turn
    _turn_had_direct_edit: bool = False                  # True if streaming saw Edit/Write this turn
    _tracked_files_populated: bool = False               # True after first _prepopulate_tracked_files
    _last_user_uuid: str = ""                            # cached from JSONL, updated by _process_message
    _last_asst_uuid: str = ""                            # cached from JSONL, updated by _process_message
    created_ts: float = 0.0  # time.time() when session was created
    _lock: threading.Lock = field(default_factory=threading.Lock)

    def __post_init__(self):
        if not self.created_ts:
            self.created_ts = time.time()

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
            "created_ts": self.created_ts,
            "tracked_files": list(self.tracked_files)[-5:],
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
        d["entry_count"] = len(self.entries)
        return d


# ---------------------------------------------------------------------------
# Detect SDK-injected system content in UserMessage text blocks.
# These include continuation summaries, local-command output, and
# system-reminder tags that should NOT render as user chat bubbles.
# ---------------------------------------------------------------------------
_SYSTEM_CONTENT_MARKERS = (
    "This session is being continued from a previous conversation",
    "<system-reminder>",
    "<local-command-stdout>",
    "<local-command-caveat>",
    "<command-name>",
    "<command-message>",
    "<command-args>",
    "[Request interrupted by user]",
)

def _is_system_content(text: str) -> bool:
    """Return True if text looks like SDK/CLI system content, not human input."""
    for marker in _SYSTEM_CONTENT_MARKERS:
        if marker in text:
            return True
    return False

def _system_content_label(text: str) -> str:
    """Extract a short human-readable label for system content."""
    if "[Request interrupted by user]" in text:
        return "Session interrupted by user"
    if "This session is being continued from a previous conversation" in text:
        return "Session continued from previous conversation"
    if "<command-name>" in text:
        # Extract command name: <command-name>/compact</command-name>
        import re
        m = re.search(r'<command-name>(/?\w+)</command-name>', text)
        cmd = m.group(1) if m else "command"
        # Extract stdout if present
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


# ── Stream Closed on Recovery Bug ──────────────────────────────────────
# When the daemon process is killed (taskkill, crash, port recycle) while
# a session is mid-response, the .jsonl file is left with an incomplete
# assistant entry: stop_reason=null, partial content, no ResultMessage.
#
# On restart, _recover_sessions tries --resume on these .jsonl files.
# The CLI sees the dangling assistant turn, can't figure out where to
# pick up, and immediately closes the stream → "Stream closed" error.
# The self-healing logic then reconnects and retries, but every retry
# hits the same corrupt .jsonl → infinite reconnect loop, session stuck
# in "working" forever.
#
# Fix: before any --resume (recovery or mid-session reconnect), patch the
# last .jsonl entry so stop_reason="end_turn".  The CLI then sees a clean
# conversation and resumes normally.  Never skip recovery — always repair.
# ───────────────────────────────────────────────────────────────────────
def _repair_incomplete_jsonl(jsonl_path: Path) -> bool:
    """Patch a .jsonl that ends with an incomplete assistant turn.

    When the daemon is killed mid-response, the last entry in the .jsonl
    is an assistant message with stop_reason=null.  The CLI's --resume
    chokes on this and immediately closes the stream.

    This function detects that case and patches the last line so
    stop_reason="end_turn" and a text block is appended saying the
    session was interrupted.  Returns True if the file was modified.
    """
    try:
        with open(jsonl_path, "r", encoding="utf-8") as f:
            lines = f.readlines()
        if not lines:
            return False

        last_line = lines[-1].strip()
        if not last_line:
            return False

        obj = json.loads(last_line)
        if obj.get("type") != "assistant":
            return False

        msg = obj.get("message", {})
        if msg.get("stop_reason") is not None:
            return False  # already complete

        # Patch the incomplete assistant turn
        logger.info("Repairing incomplete assistant turn in %s", jsonl_path.name)
        msg["stop_reason"] = "end_turn"
        msg["stop_sequence"] = None

        # Append an interruption notice to content so the model knows
        content = msg.get("content", [])
        content.append({
            "type": "text",
            "text": "\n\n[Session interrupted — resuming from last checkpoint]",
        })
        msg["content"] = content

        lines[-1] = json.dumps(obj) + "\n"
        with open(jsonl_path, "w", encoding="utf-8") as f:
            f.writelines(lines)
        return True
    except Exception as e:
        logger.warning("_repair_incomplete_jsonl(%s) failed: %s", jsonl_path, e)
        return False


class SessionManager:
    """Manages all Claude Code SDK sessions on a dedicated asyncio event loop."""

    def __init__(self):
        self._sessions: dict[str, SessionInfo] = {}
        self._id_aliases: dict[str, str] = {}  # old_id -> new_id for SDK remaps
        self._lock = threading.Lock()
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._thread: Optional[threading.Thread] = None
        self._push_callback = None
        self._started = False
        self._registry_timer: Optional[threading.Timer] = None
        self._registry_dirty = False
        # Permission policy (synced from browser) — persisted to disk
        self._policy_path = Path.home() / ".claude" / "gui_permission_policy.json"
        self._permission_policy, self._custom_rules = self._load_policy()
        # UI preferences (send behavior, etc.) — persisted to disk
        self._ui_prefs_path = Path.home() / ".claude" / "gui_ui_prefs.json"
        self._ui_prefs = self._load_ui_prefs()
        # Hook-based permission storage
        self._hook_pending = {}  # {req_id: {"event": threading.Event, "result": str}}
        self._hook_lock = threading.Lock()
        # Server-side message queue (per-session, FIFO)
        self._queues: dict[str, list[str]] = {}
        self._queue_lock = threading.Lock()
        self._queue_path = Path.home() / ".claude" / "gui_message_queues.json"
        self._load_queues()

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def start(self, push_callback=None) -> None:
        """Start the background event loop thread. Called once at app startup."""
        if self._started:
            return
        self._push_callback = push_callback
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

    async def _tracked_coro(self, info: SessionInfo, coro) -> None:
        """Run *coro* while storing the asyncio.Task on *info* so interrupt can cancel it."""
        info.task = asyncio.current_task()
        try:
            await coro
        finally:
            # Clear only if we're still the tracked task (a new query may have replaced us)
            if info.task is asyncio.current_task():
                info.task = None

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
        extra_args: Optional[dict] = None,
    ) -> dict:
        """Start or resume an SDK session. Returns immediately."""
        _forward_to_send = False
        with self._lock:
            if session_id in self._sessions:
                existing = self._sessions[session_id]
                # Detect zombie: task coroutine finished but state never
                # transitioned to STOPPED/IDLE.  Force cleanup so the
                # session can be restarted instead of stuck forever.
                _is_zombie = (
                    existing.state in (SessionState.WORKING, SessionState.STARTING)
                    and existing.task is not None
                    and existing.task.done()
                )
                if _is_zombie:
                    logger.warning("Zombie session %s detected (state=%s, task done) — forcing cleanup",
                                   session_id, existing.state.value)
                    existing.state = SessionState.STOPPED
                    del self._sessions[session_id]
                elif existing.state not in (SessionState.STOPPED,):
                    # Session is alive (IDLE/WORKING/WAITING/STARTING).
                    # If a prompt was provided, deliver it via send_message()
                    # instead of rejecting — this covers the common case where
                    # the frontend thought the session was sleeping (stale
                    # runningIds) and fell back to start_session.
                    if prompt:
                        _forward_to_send = True
                    else:
                        return {"ok": False, "error": "Session already running"}
                else:
                    # Allow restart of a stopped session
                    del self._sessions[session_id]

        # Forward OUTSIDE the lock — send_message() also acquires self._lock,
        # and since it's a threading.Lock (non-reentrant), calling it while
        # holding the lock would deadlock the daemon thread.
        if _forward_to_send:
            return self.send_message(session_id, prompt)

        info = SessionInfo(
            session_id=session_id,
            name=name,
            cwd=cwd,
            model=model or "",
            state=SessionState.STARTING,
            session_type=session_type or "",
        )
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

        # Launch the async session driver (wrapped so info.task is set for interrupt)
        asyncio.run_coroutine_threadsafe(
            self._tracked_coro(info, self._drive_session(
                session_id, prompt, cwd, resume,
                model=model, system_prompt=system_prompt,
                max_turns=max_turns, allowed_tools=allowed_tools,
                permission_mode=permission_mode,
                extra_args=extra_args,
            )),
            self._loop,
        )
        return {"ok": True}

    def send_message(self, session_id: str, text: str, _self_heal: bool = False) -> dict:
        """Send a follow-up message to an idle session.

        If the session is busy (WORKING/WAITING/STARTING), the message is
        automatically queued and will be dispatched when the session next
        becomes IDLE.  This eliminates race conditions where the frontend
        thinks the session is idle but it has already transitioned.

        _self_heal: internal flag -- when True, the call is a self-healing
        retry and the heal counter is NOT reset (so the <=3 limit works).
        """
        session_id = self._resolve_id(session_id)
        with self._lock:
            info = self._sessions.get(session_id)
        if not info:
            return {"ok": False, "error": "Session not found"}

        # Atomic state check + set under per-session lock to prevent two
        # concurrent send_message calls from both seeing IDLE and both
        # launching _send_query coroutines in parallel.
        with info._lock:
            if info.state == SessionState.STOPPED:
                return {"ok": False, "error": "Session is stopped"}
            if info.state != SessionState.IDLE:
                # Auto-queue instead of returning an error
                return self.queue_message(session_id, text)
            # Reset self-healing counters for new user messages.
            # Skip reset on self-healing retries so heal_count <= 3 works.
            if not _self_heal:
                if hasattr(info, '_stream_heal_count'):
                    info._stream_heal_count = 0
                info._stream_heal_needed = False
            # Read (but do NOT clear) the interrupted flag. Clearing it
            # here races: the old task's CancelledError/finally handler
            # runs on the event loop and checks _interrupted — if we clear
            # it from this Flask thread first, the old task thinks it's a
            # non-interrupt cancel and sets state back to IDLE.
            # _send_query clears it on the event loop AFTER the old task exits.
            _was_interrupted = getattr(info, '_interrupted', False)
            # Flag for _send_query: drain stale messages from the
            # interrupted turn's SDK buffer before sending the new query.
            # See the "Drain stale messages" block in _send_query for
            # the full explanation of the SDK shared-buffer bug.
            if _was_interrupted:
                info._drain_stale = True
            # Add user entry to history. Normally the frontend shows it
            # optimistically so we skip the emit. But after an interrupt the
            # optimistic bubble was cleared, so emit to avoid a gap.
            _stripped = text.strip()
            if not (_stripped.startswith('/') and ' ' not in _stripped):
                entry = LogEntry(kind="user", text=text)
                info.entries.append(entry)
                if _was_interrupted:
                    entry_index = len(info.entries) - 1
                    self._emit_entry(info.session_id, entry, entry_index)
            # Set state to WORKING before submitting query
            info.state = SessionState.WORKING
            # Set compacting substatus immediately so the state event carries it
            if _stripped == '/compact':
                info.substatus = "compacting"

        self._emit_state(info)

        asyncio.run_coroutine_threadsafe(
            self._tracked_coro(info, self._send_query(session_id, text)), self._loop
        )
        return {"ok": True}

    def resolve_permission(self, session_id: str, allow: bool, always: bool = False,
                           almost_always: bool = False) -> dict:
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
            result_holder[0] = (result, always, almost_always)
            perm_event.set()  # threading.Event.set() is fully thread-safe

        return {"ok": True}

    def _load_policy(self):
        """Load persisted permission policy from disk."""
        try:
            if self._policy_path.exists():
                data = json.loads(self._policy_path.read_text())
                policy = data.get("policy", "manual")
                if policy in ("manual", "auto", "almost_always", "custom"):
                    logger.info("Loaded persisted permission policy: %s", policy)
                    return policy, data.get("custom_rules", {})
        except Exception as e:
            logger.warning("Failed to load permission policy: %s", e)
        return "manual", {}

    def _save_policy(self):
        """Persist permission policy to disk."""
        try:
            self._policy_path.parent.mkdir(parents=True, exist_ok=True)
            self._policy_path.write_text(json.dumps({
                "policy": self._permission_policy,
                "custom_rules": self._custom_rules,
            }))
        except Exception as e:
            logger.warning("Failed to save permission policy: %s", e)

    def get_permission_policy(self) -> dict:
        """Return the current permission policy and custom rules."""
        return {
            "policy": self._permission_policy,
            "custom_rules": self._custom_rules,
        }

    def set_permission_policy(self, policy: str, custom_rules: dict = None) -> None:
        """Update the permission policy (synced from browser)."""
        if policy not in ("manual", "auto", "almost_always", "custom"):
            return
        self._permission_policy = policy
        self._custom_rules = custom_rules or {}
        self._save_policy()
        logger.info("Permission policy updated and saved: %s", policy)

    # ------------------------------------------------------------------
    # UI Preferences persistence
    # ------------------------------------------------------------------

    def _load_ui_prefs(self) -> dict:
        """Load persisted UI preferences from disk."""
        try:
            if self._ui_prefs_path.exists():
                data = json.loads(self._ui_prefs_path.read_text())
                if isinstance(data, dict):
                    logger.info("Loaded persisted UI prefs: %s", list(data.keys()))
                    return data
        except Exception as e:
            logger.warning("Failed to load UI prefs: %s", e)
        return {}

    def _save_ui_prefs(self):
        """Persist UI preferences to disk."""
        try:
            self._ui_prefs_path.parent.mkdir(parents=True, exist_ok=True)
            self._ui_prefs_path.write_text(json.dumps(self._ui_prefs))
        except Exception as e:
            logger.warning("Failed to save UI prefs: %s", e)

    def get_ui_prefs(self) -> dict:
        """Return all persisted UI preferences."""
        return dict(self._ui_prefs)

    def set_ui_prefs(self, prefs: dict) -> None:
        """Merge new preferences into saved UI prefs and persist."""
        if not isinstance(prefs, dict):
            return
        self._ui_prefs.update(prefs)
        self._save_ui_prefs()
        logger.info("UI prefs updated and saved: %s", list(prefs.keys()))

    def _should_auto_approve(self, tool_name: str, tool_input: dict) -> bool:
        """Check if a tool use should be auto-approved based on the current policy."""
        policy = self._permission_policy

        if policy == "manual":
            return False
        if policy == "auto":
            return True
        if policy == "almost_always":
            # Auto-approve everything EXCEPT dangerous commands
            if self._is_dangerous(tool_name, tool_input):
                return False
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

    # ------------------------------------------------------------------
    # Dangerous-command detection (for "Almost Always")
    # ------------------------------------------------------------------

    # Only block IRREVERSIBLE or HARD-TO-REPAIR actions.
    # Recoverable operations (process kills, chmod, mv /tmp, docker rm,
    # git branch -D with reflog) are left alone to keep prompts minimal.
    _DANGEROUS_PATTERNS = [
        # ── Permanent file/data destruction ──
        r'\brm\s+.*-[rRf]',              # rm -r, rm -rf, rm -f
        r'\brm\s+.*\*',                   # rm with wildcards
        r'\bfind\b.*\s-delete\b',         # find ... -delete
        r'\bfind\b.*-exec\s+rm\b',       # find ... -exec rm
        r'\bshutil\.rmtree\b',           # Python rmtree in inline scripts
        r'>\s*/dev/',                      # redirect to devices
        r'^\s*>\s*[\'"]?/',               # bare redirect truncating a file
        r'\btruncate\s',                  # truncate command
        r'\bmkfs\b',                      # format filesystem
        r'\bdd\s+if=',                    # dd disk overwrite
        r'\bmv\s+.*\s+/dev/null\b',       # mv to /dev/null (data gone)

        # ── Git operations that rewrite shared history ──
        r'\bgit\s+push\s+.*--force',      # force push (overwrites remote)
        r'\bgit\s+push\s+-f\b',          # force push short flag
        r'\bgit\s+reset\s+--hard',        # hard reset (uncommitted work gone)
        r'\bgit\s+clean\s+-[fdxe]',       # git clean (untracked files gone forever)
        r'\bgit\s+stash\s+clear\b',       # clear ALL stashes

        # ── SQL irreversible operations ──
        r'\bDROP\s+(TABLE|DATABASE|SCHEMA|VIEW)',
        r'\bTRUNCATE\b',

        # ── Public/irreversible deployment ──
        r'\bnpm\s+publish\b',            # publishes to the world, can't unpublish

        # ── Remote code execution (unknown impact) ──
        r'\bcurl\b.*\|\s*(ba)?sh',        # pipe curl to shell
        r'\bwget\b.*\|\s*(ba)?sh',        # pipe wget to shell
        r'\bpython[3]?\s+-c\s+.*\brmtree\b',  # python -c with rmtree
    ]
    _DANGEROUS_RE = None  # lazily compiled

    @classmethod
    def _is_dangerous(cls, tool_name: str, tool_input) -> bool:
        """Return True if tool_input looks destructive (used by Almost Always)."""
        if (tool_name or "").lower() != "bash":
            return False
        command = ""
        if isinstance(tool_input, dict):
            command = tool_input.get("command", "")
        if not command:
            return False
        if cls._DANGEROUS_RE is None:
            import re as _re
            cls._DANGEROUS_RE = _re.compile(
                "|".join(cls._DANGEROUS_PATTERNS), _re.IGNORECASE | _re.MULTILINE
            )
        return bool(cls._DANGEROUS_RE.search(command))

    # ------------------------------------------------------------------
    # Server-side message queue
    # ------------------------------------------------------------------

    def _load_queues(self) -> None:
        """Load persisted queues from disk."""
        try:
            if self._queue_path.exists():
                raw = json.loads(self._queue_path.read_text(encoding="utf-8"))
                if isinstance(raw, dict):
                    for k, v in raw.items():
                        if isinstance(v, list) and all(isinstance(x, str) for x in v):
                            self._queues[k] = v
        except Exception as e:
            logger.warning("Failed to load queues: %s", e)

    def _save_queues(self) -> None:
        """Persist queues to disk."""
        try:
            self._queue_path.parent.mkdir(parents=True, exist_ok=True)
            with self._queue_lock:
                data = {k: v for k, v in self._queues.items() if v}
            self._queue_path.write_text(json.dumps(data), encoding="utf-8")
        except Exception as e:
            logger.debug("Failed to save queues: %s", e)

    def _emit_queue_update(self, session_id: str) -> None:
        """Push queue state to connected clients."""
        with self._queue_lock:
            items = list(self._queues.get(session_id, []))
        if self._push_callback:
            self._push_callback('queue_updated', {
                'session_id': session_id,
                'queue': items,
            })

    def queue_message(self, session_id: str, text: str) -> dict:
        """Add a message to a session's queue."""
        session_id = self._resolve_id(session_id)
        with self._queue_lock:
            if session_id not in self._queues:
                self._queues[session_id] = []
            self._queues[session_id].append(text)
        self._save_queues()
        self._emit_queue_update(session_id)
        logger.info("Queued message for %s (%d in queue)", session_id,
                     len(self._queues.get(session_id, [])))

        # If the session is already idle (e.g. voice recording finished after
        # the session went idle), dispatch immediately instead of waiting for
        # the next idle transition which will never come.
        info = self._sessions.get(session_id)
        if info and info.state == SessionState.IDLE:
            self._try_dispatch_queue(session_id)

        return {"ok": True, "queued": True}

    def get_queue(self, session_id: str) -> list:
        """Return the queue for a session."""
        session_id = self._resolve_id(session_id)
        with self._queue_lock:
            return list(self._queues.get(session_id, []))

    def remove_queue_item(self, session_id: str, index: int) -> dict:
        """Remove one item from a session's queue by index."""
        session_id = self._resolve_id(session_id)
        with self._queue_lock:
            q = self._queues.get(session_id, [])
            if 0 <= index < len(q):
                q.pop(index)
                if not q:
                    self._queues.pop(session_id, None)
            else:
                return {"ok": False, "error": "Index out of range"}
        self._save_queues()
        self._emit_queue_update(session_id)
        return {"ok": True}

    def edit_queue_item(self, session_id: str, index: int, text: str) -> dict:
        """Edit one item in a session's queue by index."""
        session_id = self._resolve_id(session_id)
        with self._queue_lock:
            q = self._queues.get(session_id, [])
            if 0 <= index < len(q):
                q[index] = text
            else:
                return {"ok": False, "error": "Index out of range"}
        self._save_queues()
        self._emit_queue_update(session_id)
        return {"ok": True}

    def clear_queue(self, session_id: str) -> dict:
        """Clear all queued messages for a session."""
        session_id = self._resolve_id(session_id)
        with self._queue_lock:
            self._queues.pop(session_id, None)
        self._save_queues()
        self._emit_queue_update(session_id)
        return {"ok": True}

    def _try_dispatch_queue(self, session_id: str) -> None:
        """If session is IDLE and queue has items, dispatch the first one.

        Called from _emit_state on IDLE transitions. Runs send_message
        which sets state to WORKING and submits the query asynchronously.
        """
        with self._queue_lock:
            q = self._queues.get(session_id, [])
            if not q:
                return
            text = q.pop(0)
            remaining = len(q)
            if not q:
                self._queues.pop(session_id, None)

        self._save_queues()
        self._emit_queue_update(session_id)

        logger.info("Auto-dispatching queued message for %s (%d remaining)",
                     session_id, remaining)

        # Notify frontend that a queued message is being sent
        if self._push_callback:
            self._push_callback('queue_dispatched', {
                'session_id': session_id,
                'text': text,
                'remaining': remaining,
            })

        # send_message checks state==IDLE and sets WORKING atomically
        result = self.send_message(session_id, text)
        if not result.get("ok") and not result.get("queued"):
            # Send failed (race condition) — re-queue at front
            logger.warning("Queue dispatch failed for %s: %s — re-queuing",
                          session_id, result.get("error"))
            with self._queue_lock:
                if session_id not in self._queues:
                    self._queues[session_id] = []
                self._queues[session_id].insert(0, text)
            self._save_queues()
            self._emit_queue_update(session_id)

    def interrupt_session(self, session_id: str, clear_queue: bool = True) -> dict:
        """Interrupt a running session.

        When clear_queue is True (default), also clears any queued messages
        to prevent auto-dispatch when the session goes idle after interrupt.
        """
        session_id = self._resolve_id(session_id)
        with self._lock:
            info = self._sessions.get(session_id)
        if not info:
            return {"ok": False, "error": "Session not found"}
        if info.state == SessionState.STOPPED:
            return {"ok": False, "error": "Session already stopped"}

        # Clear queue atomically BEFORE the interrupt so _emit_state(IDLE)
        # won't auto-dispatch a queued message after the interrupt completes.
        if clear_queue:
            with self._queue_lock:
                if session_id in self._queues:
                    self._queues.pop(session_id)
                    self._save_queues()
                    self._emit_queue_update(session_id)

        # Set IDLE synchronously so send_message sees it immediately.
        # Without this, there's a race: the async _interrupt_session hasn't
        # run yet, state is still WORKING, and send_message queues instead
        # of sending.
        info._interrupted = True
        info.state = SessionState.IDLE
        # Capture the task NOW so _interrupt_session cancels the right one.
        # Without this, if the user sends a new message before
        # _interrupt_session runs, info.task gets replaced by the new
        # query's task, and the interrupt kills the wrong coroutine.
        task_to_cancel = info.task
        self._emit_state(info)

        asyncio.run_coroutine_threadsafe(
            self._interrupt_session(session_id, task_to_cancel), self._loop
        )
        return {"ok": True}

    def close_session(self, session_id: str) -> dict:
        """Close and disconnect an SDK session.

        Immediately forces the session to STOPPED so subsequent start_session
        calls don't get rejected with 'already running'.  The async cleanup
        (disconnect, cancel task) still runs in the background.
        """
        session_id = self._resolve_id(session_id)
        with self._lock:
            info = self._sessions.get(session_id)
        if not info:
            return {"ok": False, "error": "Session not found"}

        # Force STOPPED immediately so the session is unblocked for restart
        info.state = SessionState.STOPPED
        self._emit_state(info)

        asyncio.run_coroutine_threadsafe(
            self._close_session(session_id), self._loop
        )
        return {"ok": True}

    def close_session_sync(self, session_id: str, timeout: float = 5.0) -> dict:
        """Close an SDK session and block until the disconnect finishes."""
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
        """Remove a session from the in-memory dict entirely."""
        with self._lock:
            self._sessions.pop(session_id, None)

    def get_all_states(self) -> list:
        """Return snapshot of all session states for initial WebSocket connect.

        Includes queue data from the server-side queue store so reconnecting
        clients can immediately display queued items.
        """
        with self._lock:
            states = [info.to_state_dict() for info in self._sessions.values()
                      if info.session_type not in ("planner", "title")]
        # Merge queue data from _queues into state dicts
        with self._queue_lock:
            for s in states:
                sid = s.get("session_id", "")
                q = self._queues.get(sid, [])
                if q:
                    s["queue"] = list(q)
        return states

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

    async def _reconnect_client(self, session_id: str, info) -> bool:
        """Reconnect a session whose SDK stream died.

        Creates a fresh ClaudeSDKClient with resume=session_id, connects it,
        and replaces info.client.  Returns True on success.
        """
        resolved = self._resolve_id(session_id)
        logger.info("Reconnecting SDK client for %s (resolved: %s)", session_id, resolved)

        # Tear down the dead client
        if info.client:
            try:
                await info.client.disconnect()
            except Exception:
                pass
            info.client = None

        # Repair incomplete .jsonl before reconnecting — if the stream
        # died mid-response, the last entry has stop_reason=null and
        # --resume will choke on it immediately.
        try:
            projects_dir = Path.home() / ".claude" / "projects"
            if info.cwd:
                encoded = info.cwd.replace("\\", "/").replace(":", "-").replace("/", "-").replace("_", "-")
                jsonl_candidate = projects_dir / encoded / f"{resolved}.jsonl"
                if jsonl_candidate.exists():
                    _repair_incomplete_jsonl(jsonl_candidate)
        except Exception as _rep_err:
            logger.warning("_reconnect_client: jsonl repair failed: %s", _rep_err)

        try:
            options = ClaudeCodeOptions(
                cwd=info.cwd or None,
                resume=resolved,
                can_use_tool=self._make_permission_callback(session_id),
                model=info.model or None,
                permission_mode="default",
                include_partial_messages=True,
            )
            client = ClaudeSDKClient(options=options)
            await client.connect()
            info.client = client
            logger.info("Reconnected SDK client for %s", session_id)
            return True
        except Exception as e:
            logger.exception("Failed to reconnect SDK client for %s: %s", session_id, e)
            return False

    async def _drive_session(
        self, session_id: str, prompt: str, cwd: str, resume: bool,
        model: Optional[str] = None, system_prompt: Optional[str] = None,
        max_turns: Optional[int] = None, allowed_tools: Optional[list] = None,
        permission_mode: Optional[str] = None,
        extra_args: Optional[dict] = None,
    ) -> None:
        """Main driver coroutine for one SDK session."""
        with self._lock:
            info = self._sessions.get(session_id)
        if not info:
            return

        _t0 = time.perf_counter()
        _profile_log = (lambda label: logger.info(
            "PROFILE _drive_session [%s] %s: %.3fs elapsed",
            session_id[:12], label, time.perf_counter() - _t0)
        ) if self._PROFILE_PIPELINE else lambda _: None

        got_result = False
        result_handled = False
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
                extra_args=extra_args or {},
            )
            _profile_log("options_built")
            client = ClaudeSDKClient(options=options)
            info.client = client

            # Connect with no prompt. The SDK auto-sets permission_prompt_tool_name="stdio"
            # when can_use_tool is set. Prompt=None becomes _empty_stream() which is an
            # AsyncIterator, so the streaming mode check passes.
            await client.connect()
            _profile_log("client_connected")

            info.state = SessionState.WORKING
            self._emit_state(info)

            # Reset per-turn state and record mtimes for change detection.
            # Mtimes are only used as fallback when no direct Edit/Write is
            # seen in the stream (e.g. Agent sub-agent did the editing).
            #
            # NOTE: We do NOT write a pre-turn snapshot here.  On the first
            # turn the SDK hasn't sent a ResultMessage yet, so the session_id
            # hasn't been remapped to the CLI's real UUID.  Writing a snapshot
            # now would target the wrong JSONL file.  Pre-populate and pre-turn
            # snapshots are deferred to _send_query (follow-up turns), where
            # the remap has already occurred.
            info._turn_had_direct_edit = False
            loop = asyncio.get_event_loop()
            await loop.run_in_executor(None, self._record_pre_turn_mtimes, info)
            _profile_log("pre_turn_mtimes_done")

            # Add user's message to the log and send
            if prompt:
                entry = LogEntry(kind="user", text=prompt[:20000])
                with info._lock:
                    info.entries.append(entry)
                    entry_index = len(info.entries) - 1
                self._emit_entry(session_id, entry, entry_index)
                await client.query(prompt)
                _profile_log("query_sent")

            # Process messages (None = unknown types, skipped via monkey-patch)
            #
            # IMPORTANT: use receive_response() (not receive_messages()) so the
            # iterator terminates after ResultMessage.  receive_messages() keeps
            # the generator alive for the entire subprocess lifetime, which
            # means _drive_session would never exit after the first turn.  When
            # _send_query later calls receive_response() on the same client,
            # BOTH generators compete for messages from the same underlying
            # stream — causing ResultMessages to be consumed by the wrong
            # handler and leaving sessions stuck in WORKING forever.
            info._stream_evt_logged = False
            _first_msg_logged = False
            if prompt:
                async for message in client.receive_response():
                    # If a newer task has replaced us, stop processing —
                    # our stream is stale and we must not touch state.
                    if info.task is not asyncio.current_task():
                        break
                    if message is not None:
                        if not _first_msg_logged:
                            _profile_log("first_stream_message (%s)" % type(message).__name__)
                            _first_msg_logged = True
                        if isinstance(message, ResultMessage):
                            got_result = True
                            _profile_log("result_message")
                        try:
                            await self._process_message(session_id, message)
                            if isinstance(message, ResultMessage):
                                result_handled = True
                        except Exception as pm_err:
                            # Don't let one bad message kill the entire stream.
                            logger.exception(
                                "_process_message error for %s (msg type %s): %s",
                                session_id, type(message).__name__, pm_err
                            )
            else:
                # No prompt (empty session or bare resume) — nothing to receive.
                # Go straight to IDLE so send_message() can dispatch follow-ups.
                got_result = True
                result_handled = True
                info.state = SessionState.IDLE
                self._emit_state(info)

            # Safety net: if the stream ended without a ResultMessage,
            # force IDLE so the session isn't stuck.  Skip if we already
            # got a ResultMessage (which handles IDLE + queue dispatch).
            # Also skip if superseded — the new task owns state.
            if not got_result and info.state == SessionState.WORKING \
                    and info.task is asyncio.current_task():
                logger.warning("_drive_session for %s: stream ended without "
                               "ResultMessage, forcing IDLE", session_id)
                info.state = SessionState.IDLE
                self._emit_state(info)

        except asyncio.CancelledError:
            # Defense-in-depth for the stop→follow-up race (see "Drain
            # stale messages" comment in _send_query).
            _superseded = info.task is not asyncio.current_task()
            if _superseded or getattr(info, '_interrupted', False):
                if not _superseded:
                    logger.info("Session %s interrupted (task cancelled)", session_id)
            else:
                # Close/shutdown — go to STOPPED
                logger.info("Session %s cancelled", session_id)
                info.state = SessionState.STOPPED
                self._emit_state(info)
        except Exception as e:
            err_str = str(e)
            logger.exception("Session %s stream error: %s", session_id, e)

            # If _stream_heal_needed was already set (from "Stream closed" in
            # a tool result), skip the reconnect here — the finally block will
            # do reconnect + retry.  This avoids a wasteful double-reconnect.
            if getattr(info, '_stream_heal_needed', False):
                logger.info("_drive_session %s: stream_heal already flagged, "
                            "deferring to finally block", session_id)
                entry = LogEntry(kind="system", text="Stream lost — will reconnect and retry...", is_error=True)
                with info._lock:
                    info.entries.append(entry)
                    entry_index = len(info.entries) - 1
                self._emit_entry(session_id, entry, entry_index)
            else:
                # Flag for self-healing if this looks like a transport error
                # (not an API/logic error).  This covers cases where the
                # stream dies with an exception BEFORE a "Stream closed"
                # tool result was processed.
                _etype = type(e).__name__
                _is_transport = (
                    "Stream closed" in err_str
                    or "exit code" in err_str
                    or "closed" in err_str.lower()
                    or "CLIConnection" in _etype
                    or "Process" in _etype
                    or "ClosedResource" in _etype
                )
                if _is_transport:
                    if not hasattr(info, '_stream_heal_count'):
                        info._stream_heal_count = 0
                    info._stream_heal_count += 1
                    info._stream_heal_needed = True
                    logger.info("_drive_session %s: flagged stream_heal from "
                                "exception (%s), deferring to finally", session_id, type(e).__name__)
                    entry = LogEntry(kind="system", text="Stream lost — will reconnect and retry...", is_error=True)
                    with info._lock:
                        info.entries.append(entry)
                        entry_index = len(info.entries) - 1
                    self._emit_entry(session_id, entry, entry_index)
                else:
                    # Non-transport error: reconnect but don't auto-retry
                    entry = LogEntry(kind="system", text=f"Stream lost — reconnecting...", is_error=True)
                    with info._lock:
                        info.entries.append(entry)
                        entry_index = len(info.entries) - 1
                    self._emit_entry(session_id, entry, entry_index)

                    if await self._reconnect_client(session_id, info):
                        info.state = SessionState.IDLE
                        info.error = ""
                        entry = LogEntry(kind="system", text="Reconnected successfully")
                        with info._lock:
                            info.entries.append(entry)
                            entry_index = len(info.entries) - 1
                        self._emit_entry(session_id, entry, entry_index)
                        self._emit_state(info)
                    else:
                        info.error = err_str
                        info.state = SessionState.STOPPED
                        entry = LogEntry(kind="system", text=f"Reconnect failed: {e}", is_error=True)
                        with info._lock:
                            info.entries.append(entry)
                            entry_index = len(info.entries) - 1
                        self._emit_entry(session_id, entry, entry_index)
                        self._emit_state(info)
        finally:
            # Defense-in-depth for the stop→follow-up race (see "Drain
            # stale messages" comment in _send_query).  Superseded or
            # interrupted tasks must not touch state.
            _superseded = info and info.task is not asyncio.current_task()
            if _superseded or (info and getattr(info, '_interrupted', False)):
                return

            # Catch-all: force IDLE if stuck in WORKING. Skip if
            # ResultMessage was processed -- _try_dispatch_queue may
            # have legitimately set WORKING for the next queued msg.
            if info and info.state == SessionState.WORKING and not result_handled:
                logger.error("_drive_session finally: %s still WORKING after all "
                             "handlers — forcing IDLE as catch-all", session_id)
                info.state = SessionState.IDLE
                self._emit_state(info)

            # ── Self-healing: reconnect + retry if "Stream closed" errors
            # were detected during the turn (either from tool results or
            # from the except handler flagging a transport exception).
            try:
                if info and getattr(info, '_stream_heal_needed', False):
                    info._stream_heal_needed = False
                    heal_count = getattr(info, '_stream_heal_count', 0)
                    if heal_count <= 3:
                        logger.info(
                            "Self-healing (drive): reconnecting %s after %d stream errors",
                            session_id, heal_count,
                        )
                        entry = LogEntry(kind="system", text="Reconnecting session...")
                        with info._lock:
                            info.entries.append(entry)
                            entry_index = len(info.entries) - 1
                        self._emit_entry(session_id, entry, entry_index)

                        if await self._reconnect_client(session_id, info):
                            # Do NOT reset _stream_heal_count here.  Let it
                            # accumulate across retries so the heal_count<=3
                            # limit actually stops infinite loops.  The counter
                            # is reset in send_message() when the user sends a
                            # genuinely new message (not a self-healing retry).
                            last_user_text = None
                            with info._lock:
                                for e in reversed(info.entries):
                                    if e.kind == "user":
                                        last_user_text = e.text
                                        break
                            if last_user_text:
                                entry = LogEntry(
                                    kind="system",
                                    text="Reconnected — retrying last message automatically",
                                )
                                with info._lock:
                                    info.entries.append(entry)
                                    entry_index = len(info.entries) - 1
                                self._emit_entry(session_id, entry, entry_index)
                                info.state = SessionState.IDLE
                                self._emit_state(info)
                                self.send_message(session_id, last_user_text, _self_heal=True)
                            else:
                                entry = LogEntry(kind="system", text="Reconnected successfully")
                                with info._lock:
                                    info.entries.append(entry)
                                    entry_index = len(info.entries) - 1
                                self._emit_entry(session_id, entry, entry_index)
                                info.state = SessionState.IDLE
                                self._emit_state(info)
                        else:
                            info._stream_heal_count = 0
                            entry = LogEntry(
                                kind="system",
                                text="Reconnect failed — please resend your message",
                                is_error=True,
                            )
                            with info._lock:
                                info.entries.append(entry)
                                entry_index = len(info.entries) - 1
                            self._emit_entry(session_id, entry, entry_index)
                            info.state = SessionState.IDLE
                            self._emit_state(info)
                    else:
                        info._stream_heal_count = 0
                        entry = LogEntry(
                            kind="system",
                            text="Too many stream errors \u2014 please resend your message",
                            is_error=True,
                        )
                        with info._lock:
                            info.entries.append(entry)
                            entry_index = len(info.entries) - 1
                        self._emit_entry(session_id, entry, entry_index)
                        info.state = SessionState.IDLE
                        self._emit_state(info)
            except Exception as heal_err:
                logger.exception("Self-healing failed for %s: %s", session_id, heal_err)
                if info:
                    info._stream_heal_count = 0
                    info.state = SessionState.IDLE
                    self._emit_state(info)

            # Post-turn snapshot: captures file state AFTER Claude's edits.
            # By now the SDK remap has occurred (ResultMessage was processed
            # in the message loop), so _resolve_id will find the correct
            # session JSONL.
            try:
                resolved = self._resolve_id(session_id)
                with self._lock:
                    finfo = self._sessions.get(resolved)
                loop = asyncio.get_event_loop()
                if finfo:
                    await loop.run_in_executor(
                        None, self._prepopulate_tracked_files, finfo)
                await loop.run_in_executor(
                    None, self._write_file_snapshot, session_id, True)
            except Exception as snap_err:
                logger.warning("Snapshot in finally for %s failed: %s", session_id, snap_err)

    async def _send_query(self, session_id: str, text: str) -> None:
        """Send a follow-up query to an already-connected session."""
        # Yield once so any pending CancelledError from the old task is
        # delivered BEFORE we clear _interrupted.  Without this, the old
        # task's handler may see _interrupted=False and clobber state.
        await asyncio.sleep(0)
        # Now safe to clear the flag — the old task has processed its cancel.
        with self._lock:
            info = self._sessions.get(session_id)
        if info:
            info._interrupted = False

        _t0 = time.perf_counter()
        _profile_log = (lambda label: logger.info(
            "PROFILE _send_query [%s] %s: %.3fs elapsed",
            session_id[:12], label, time.perf_counter() - _t0)
        ) if self._PROFILE_PIPELINE else lambda _: None
        if not info:
            return
        if not info.client:
            # Client is gone — attempt reconnection before giving up.
            # This covers zombie sessions left IDLE with no client after
            # a failed reconnect or a daemon registry restore.
            logger.warning("_send_query: %s has no client — attempting reconnect", session_id)
            if await self._reconnect_client(session_id, info):
                logger.info("_send_query: reconnected %s successfully", session_id)
            else:
                # Reconnect failed — force back to IDLE so the session isn't stuck
                if info.state == SessionState.WORKING:
                    logger.error("_send_query: %s reconnect failed — forcing IDLE", session_id)
                    info.state = SessionState.IDLE
                    info.error = "Session disconnected — reconnect failed"
                    entry = LogEntry(kind="system", text="Could not reconnect SDK client — please try again", is_error=True)
                    with info._lock:
                        info.entries.append(entry)
                    self._emit_state(info)
                return

        # Pre-populate tracked_files on follow-up turns (covers daemon
        # restart scenarios where tracked_files was lost).
        # Runs in a thread so large JSONL parses don't block the event loop.
        loop = asyncio.get_event_loop()
        await loop.run_in_executor(None, self._prepopulate_tracked_files, info)
        _profile_log("prepopulate_tracked_files")

        # Pre-turn snapshot (isSnapshotUpdate=false, linked to user UUID)
        # Safe here because the SDK remap has already happened by the time
        # _send_query is called (remap occurs on first turn's ResultMessage).
        # Runs in a thread — reads JSONL tail + file contents + writes backups.
        await loop.run_in_executor(None, self._write_file_snapshot, session_id, False)
        _profile_log("write_file_snapshot")

        # Reset per-turn state and record mtimes for fallback detection
        # rglob over the project directory runs in a thread to avoid blocking.
        info._turn_had_direct_edit = False
        await loop.run_in_executor(None, self._record_pre_turn_mtimes, info)
        _profile_log("record_pre_turn_mtimes")

        # ── Drain stale messages from interrupted turn ──────────────
        #
        # BUG: "Stop then follow-up shows IDLE immediately"
        #
        # The Claude SDK (claude-code-sdk) uses a single shared anyio
        # MemoryObjectStream (100-msg capacity) as its message buffer.
        # This buffer persists for the entire ClaudeSDKClient lifetime
        # and is NOT flushed by interrupt().  Here's the race:
        #
        #   1. User clicks Stop → interrupt_session() sets IDLE, cancels
        #      the old task, schedules _interrupt_session().
        #   2. _interrupt_session() calls client.interrupt() → the CLI
        #      acknowledges and emits a ResultMessage for the old turn.
        #   3. The old task was reading receive_response() but got
        #      CancelledError — it never consumed that ResultMessage.
        #      The stale ResultMessage sits in the SDK's shared buffer.
        #   4. User sends a follow-up → send_message() sets WORKING,
        #      schedules _send_query().
        #   5. _send_query() calls receive_response() → immediately
        #      gets the STALE ResultMessage from step 2.
        #   6. _process_message(ResultMessage) sets state to IDLE.
        #   7. UI flashes WORKING for a split second then goes IDLE.
        #      The new query was sent but its response loop terminated
        #      early because of the stale message.
        #
        # Fix: when send_message() detects it's sending after an
        # interrupt (_was_interrupted), it sets info._drain_stale=True.
        # Here we consume one full receive_response() cycle to eat the
        # stale ResultMessage before sending the new query.  This
        # naturally synchronises with _interrupt_session() because
        # receive_response() blocks until the stale ResultMessage
        # arrives (which happens after interrupt() completes).
        #
        # Defense in depth: the CancelledError handlers and finally
        # blocks in both _send_query and _drive_session also check
        # info.task identity (superseded check) so that even if a
        # stale task somehow touches state, it bails out when it
        # detects a newer task has replaced it.
        if getattr(info, '_drain_stale', False):
            info._drain_stale = False
            try:
                async def _drain():
                    async for _msg in info.client.receive_response():
                        logger.debug("Drained stale msg after interrupt for %s: %s",
                                     session_id, type(_msg).__name__)
                await asyncio.wait_for(_drain(), timeout=5.0)
                _profile_log("drained_stale_messages")
            except asyncio.TimeoutError:
                logger.warning("_send_query %s: stale drain timed out (5s)", session_id)
            except asyncio.CancelledError:
                raise
            except Exception as drain_err:
                logger.warning("_send_query %s: stale drain error: %s", session_id, drain_err)

        got_result = False
        result_handled = False
        _first_msg_logged = False
        try:
            await info.client.query(text)
            _profile_log("query_sent")

            # Process response messages (None = unknown types, skipped)
            info.usage.pop('_per_call', None)  # clear stale per-call marker from previous turn
            info._stream_evt_logged = False  # re-enable diagnostic log for this turn
            async for message in info.client.receive_response():
                # If a newer task has replaced us, stop processing —
                # our stream is stale and we must not touch state.
                if info.task is not asyncio.current_task():
                    break
                if message is not None:
                    if not _first_msg_logged:
                        _profile_log("first_stream_message (%s)" % type(message).__name__)
                        _first_msg_logged = True
                    if isinstance(message, ResultMessage):
                        got_result = True
                        _profile_log("result_message")
                    try:
                        await self._process_message(session_id, message)
                        if isinstance(message, ResultMessage):
                            result_handled = True
                    except Exception as pm_err:
                        # Don't let one bad message kill the entire stream.
                        # Log the error and continue processing remaining
                        # messages so ResultMessage can still arrive and set IDLE.
                        logger.exception(
                            "_process_message error for %s (msg type %s): %s",
                            session_id, type(message).__name__, pm_err
                        )

            # Safety net: if the stream ended without a ResultMessage,
            # force IDLE so the session isn't stuck forever.  Skip if we
            # got a ResultMessage — _process_message already handled the
            # IDLE transition (and may have dispatched a queued message
            # which set state back to WORKING).  Also skip if superseded.
            if not got_result and info.state == SessionState.WORKING \
                    and info.task is asyncio.current_task():
                logger.warning("_send_query for %s: stream ended without "
                               "ResultMessage, forcing IDLE", session_id)
                info.state = SessionState.IDLE
                self._emit_state(info)

        except asyncio.CancelledError:
            # Defense-in-depth for the stop→follow-up race (see "Drain
            # stale messages" comment above).  If a newer task replaced
            # us, bail without touching state.
            _superseded = info.task is not asyncio.current_task()
            if _superseded or getattr(info, '_interrupted', False):
                if not _superseded:
                    logger.info("_send_query %s: interrupted (CancelledError)", session_id)
                return
            # Non-interrupt, non-superseded cancel — set IDLE so session isn't stuck
            if info.state == SessionState.WORKING:
                info.state = SessionState.IDLE
                self._emit_state(info)
        except Exception as e:
            err_str = str(e)
            logger.exception("Send query stream error for %s: %s", session_id, e)

            # If _stream_heal_needed was already set (from "Stream closed" in
            # a tool result), skip reconnect — finally block will do it + retry.
            if getattr(info, '_stream_heal_needed', False):
                logger.info("_send_query %s: stream_heal already flagged, "
                            "deferring to finally block", session_id)
                entry = LogEntry(kind="system", text="Stream lost — will reconnect and retry...", is_error=True)
                with info._lock:
                    info.entries.append(entry)
                    entry_index = len(info.entries) - 1
                self._emit_entry(session_id, entry, entry_index)
            else:
                _etype = type(e).__name__
                _is_transport = (
                    "Stream closed" in err_str
                    or "exit code" in err_str
                    or "closed" in err_str.lower()
                    or "CLIConnection" in _etype
                    or "Process" in _etype
                    or "ClosedResource" in _etype
                )
                if _is_transport:
                    if not hasattr(info, '_stream_heal_count'):
                        info._stream_heal_count = 0
                    info._stream_heal_count += 1
                    info._stream_heal_needed = True
                    logger.info("_send_query %s: flagged stream_heal from "
                                "exception (%s), deferring to finally", session_id, type(e).__name__)
                    entry = LogEntry(kind="system", text="Stream lost — will reconnect and retry...", is_error=True)
                    with info._lock:
                        info.entries.append(entry)
                        entry_index = len(info.entries) - 1
                    self._emit_entry(session_id, entry, entry_index)
                else:
                    entry = LogEntry(kind="system", text="Stream lost — reconnecting...")
                    with info._lock:
                        info.entries.append(entry)
                        entry_index = len(info.entries) - 1
                    self._emit_entry(session_id, entry, entry_index)

                    if await self._reconnect_client(session_id, info):
                        info.state = SessionState.IDLE
                        info.error = ""
                        entry = LogEntry(kind="system", text="Reconnected successfully")
                        with info._lock:
                            info.entries.append(entry)
                            entry_index = len(info.entries) - 1
                        self._emit_entry(session_id, entry, entry_index)
                        self._emit_state(info)
                    else:
                        info.error = err_str
                        entry = LogEntry(kind="system", text=f"Reconnect failed: {e}", is_error=True)
                        with info._lock:
                            info.entries.append(entry)
                            entry_index = len(info.entries) - 1
                        self._emit_entry(session_id, entry, entry_index)
                        info.state = SessionState.IDLE
                        self._emit_state(info)
        finally:
            # Defense-in-depth for the stop→follow-up race (see "Drain
            # stale messages" comment in _send_query).  Superseded or
            # interrupted tasks must not touch state.
            _superseded = info and info.task is not asyncio.current_task()
            if _superseded or (info and getattr(info, '_interrupted', False)):
                return

            # Catch-all: force IDLE if stuck in WORKING. Skip if
            # ResultMessage was processed -- _try_dispatch_queue may
            # have legitimately set WORKING for the next queued msg.
            if info and info.state == SessionState.WORKING and not result_handled:
                logger.error("_send_query finally: %s still WORKING after all "
                             "handlers — forcing IDLE as catch-all", session_id)
                info.state = SessionState.IDLE
                self._emit_state(info)

            # ── Self-healing: reconnect + retry if "Stream closed" errors
            # were detected during this turn (either from tool results or
            # from the except handler flagging a transport exception).
            try:
                if info and getattr(info, '_stream_heal_needed', False):
                    info._stream_heal_needed = False
                    heal_count = getattr(info, '_stream_heal_count', 0)
                    if heal_count <= 3:
                        logger.info(
                            "Self-healing (query): reconnecting %s after %d stream errors",
                            session_id, heal_count,
                        )
                        entry = LogEntry(kind="system", text="Reconnecting session...")
                        with info._lock:
                            info.entries.append(entry)
                            entry_index = len(info.entries) - 1
                        self._emit_entry(session_id, entry, entry_index)

                        if await self._reconnect_client(session_id, info):
                            # Do NOT reset _stream_heal_count here.  Let it
                            # accumulate across retries so the heal_count<=3
                            # limit actually stops infinite loops.  The counter
                            # is reset in send_message() when the user sends a
                            # genuinely new message (not a self-healing retry).
                            last_user_text = None
                            with info._lock:
                                for e in reversed(info.entries):
                                    if e.kind == "user":
                                        last_user_text = e.text
                                        break
                            if last_user_text:
                                entry = LogEntry(
                                    kind="system",
                                    text="Reconnected — retrying last message automatically",
                                )
                                with info._lock:
                                    info.entries.append(entry)
                                    entry_index = len(info.entries) - 1
                                self._emit_entry(session_id, entry, entry_index)
                                logger.info("Self-heal: resending last user message for %s", session_id)
                                info.state = SessionState.IDLE
                                self._emit_state(info)
                                self.send_message(session_id, last_user_text, _self_heal=True)
                            else:
                                entry = LogEntry(kind="system", text="Reconnected successfully")
                                with info._lock:
                                    info.entries.append(entry)
                                    entry_index = len(info.entries) - 1
                                self._emit_entry(session_id, entry, entry_index)
                                info.state = SessionState.IDLE
                                self._emit_state(info)
                        else:
                            info._stream_heal_count = 0
                            entry = LogEntry(
                                kind="system",
                                text="Reconnect failed — please resend your message",
                                is_error=True,
                            )
                            with info._lock:
                                info.entries.append(entry)
                                entry_index = len(info.entries) - 1
                            self._emit_entry(session_id, entry, entry_index)
                            info.state = SessionState.IDLE
                            self._emit_state(info)
                    else:
                        info._stream_heal_count = 0
                        entry = LogEntry(
                            kind="system",
                            text="Too many stream errors — please resend your message",
                            is_error=True,
                        )
                        with info._lock:
                            info.entries.append(entry)
                            entry_index = len(info.entries) - 1
                        self._emit_entry(session_id, entry, entry_index)
                        info.state = SessionState.IDLE
                        self._emit_state(info)
            except Exception as heal_err:
                logger.exception("Self-healing failed for %s: %s", session_id, heal_err)
                if info:
                    info._stream_heal_count = 0
                    info.state = SessionState.IDLE
                    self._emit_state(info)

            # Post-turn snapshot (isSnapshotUpdate=true, linked to assistant UUID)
            try:
                loop = asyncio.get_event_loop()
                await loop.run_in_executor(
                    None, self._write_file_snapshot, session_id, True)
            except Exception as snap_err:
                logger.warning("Snapshot in finally for %s failed: %s", session_id, snap_err)

    async def _interrupt_session(self, session_id: str,
                                task_to_cancel: "asyncio.Task | None" = None) -> None:
        """Interrupt a running session.

        task_to_cancel: the asyncio.Task captured at interrupt time so we
        cancel the correct coroutine even if a new query replaced info.task
        before this coroutine ran.
        """
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
                    result_holder[0] = (deny, False, False)
                    perm_event.set()

            # _interrupted flag and IDLE state already set by the sync
            # interrupt_session() before this coroutine was dispatched.

            # Cancel the driving task — this is what actually stops the
            # coroutine. client.interrupt() alone just signals the CLI
            # but the coroutine keeps running and can set WORKING again.
            # Use the captured task reference, NOT info.task, because a
            # new send_message may have replaced info.task already.
            _task = task_to_cancel or info.task
            if _task and not _task.done():
                _task.cancel()

            # Only send the SDK interrupt signal if no new query has started.
            # If info.task has been replaced by a new _tracked_coro, calling
            # interrupt() would kill the NEW query instead of the old one.
            _new_task_started = (
                task_to_cancel is not None
                and info.task is not None
                and info.task is not task_to_cancel
            )
            if not _new_task_started:
                try:
                    await info.client.interrupt()
                except Exception:
                    pass

            # Add interrupt marker unless the CLI stream already delivered one
            # (the SDK sends "[Request interrupted by user]" which gets
            # converted to "Session interrupted by user" by _process_message).
            with info._lock:
                already = any(
                    getattr(e, 'text', '') == "Session interrupted by user"
                    for e in info.entries[-3:]  # only check tail
                ) if info.entries else False
            if not already:
                entry = LogEntry(kind="system", text="Session interrupted by user")
                with info._lock:
                    info.entries.append(entry)
                    entry_index = len(info.entries) - 1
                self._emit_entry(session_id, entry, entry_index)
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
                    result_holder[0] = (deny, False, False)
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

        resolve_permission() (called from a caller thread) sets the event
        via loop.call_soon_threadsafe() so the waiting coroutine wakes up
        immediately.
        """
        manager = self

        async def can_use_tool(tool_name, tool_input, context):
            # Resolve through aliases — the SDK remaps session IDs on
            # ResultMessage, so the closed-over session_id may be stale.
            resolved_id = manager._resolve_id(session_id)
            with manager._lock:
                info = manager._sessions.get(resolved_id)
            if not info:
                return PermissionResultDeny(message="Session not found", interrupt=True)

            # ── Early abort: if the CLI subprocess transport is dead,
            # every tool use will fail with "Stream closed".  Instead of
            # letting the agent retry dozens of times, deny with
            # interrupt=True to end the turn immediately.  The finally
            # block in _drive_session / _send_query will reconnect and
            # retry the whole message on a fresh CLI process.
            try:
                _transport_alive = (
                    info.client
                    and info.client._query
                    and info.client._query.transport
                    and info.client._query.transport.is_ready()
                )
            except Exception:
                _transport_alive = False
            if not _transport_alive:
                logger.warning(
                    "Transport dead for %s — aborting turn for tool %s "
                    "(will reconnect in finally block)",
                    resolved_id, tool_name,
                )
                # Flag for self-healing so the finally block reconnects
                info._stream_heal_needed = True
                if not hasattr(info, '_stream_heal_count'):
                    info._stream_heal_count = 0
                info._stream_heal_count += 1
                # Log a single user-visible message (only on first detection)
                if info._stream_heal_count == 1:
                    _heal_entry = LogEntry(
                        kind="system",
                        text="Connection lost \u2014 aborting turn, will reconnect and retry automatically",
                        is_error=True,
                    )
                    with info._lock:
                        info.entries.append(_heal_entry)
                        _heal_idx = len(info.entries) - 1
                    manager._emit_entry(resolved_id, _heal_entry, _heal_idx)
                return PermissionResultDeny(
                    message="Transport disconnected — reconnecting",
                    interrupt=True,
                )

            # Auto-approve if user previously clicked "Always" for this tool
            if tool_name in info.always_allowed_tools:
                manager._log_auto_approved(
                    resolved_id, info, tool_name, tool_input, "always-allow"
                )
                return PermissionResultAllow()

            # "Almost Always" — auto-approve unless the command looks dangerous
            if tool_name in info.almost_always_allowed_tools:
                if manager._is_dangerous(tool_name, tool_input):
                    logger.warning(
                        "Almost-always BLOCKED dangerous %s: %s",
                        tool_name,
                        (tool_input.get("command", "") if isinstance(tool_input, dict) else ""),
                    )
                    manager._log_auto_approved(
                        resolved_id, info, tool_name, tool_input,
                        "almost-always-blocked"
                    )
                    # Fall through to manual prompt below
                else:
                    manager._log_auto_approved(
                        resolved_id, info, tool_name, tool_input, "almost-always"
                    )
                    return PermissionResultAllow()

            # Server-side policy check -- resolve without browser round-trip
            if manager._should_auto_approve(tool_name, tool_input if isinstance(tool_input, dict) else {}):
                logger.debug("Auto-approved %s via server policy", tool_name)
                manager._log_auto_approved(
                    resolved_id, info, tool_name, tool_input, "server-policy"
                )
                return PermissionResultAllow()

            # Use threading.Event (fully thread-safe) with anyio.sleep polling.
            # anyio.Event + call_soon_threadsafe doesn't reliably wake the waiter.
            perm_event = threading.Event()
            perm_result_holder = [None]  # [0] = (PermissionResult, always, almost_always)
            info.pending_permission = (perm_event, perm_result_holder)
            info.pending_tool_name = tool_name
            info.pending_tool_input = tool_input if isinstance(tool_input, dict) else {}

            # Set state to WAITING
            prev_state = info.state
            info.state = SessionState.WAITING

            # Emit permission via push_callback.
            # Use resolved_id so the frontend matches the current session key.
            perm_data = {
                'session_id': resolved_id,
                'tool_name': tool_name,
                'tool_input': info.pending_tool_input,
            }
            state_data = info.to_state_dict()
            _push = manager._push_callback
            if _push:
                _push('session_state', state_data)
                _push('session_permission', perm_data)

            try:
                # Poll threading.Event. Use anyio.sleep to yield to scheduler.
                # If that doesn't work, fall back to asyncio.sleep.
                for _poll_i in range(36000):  # 1 hour max
                    if perm_event.is_set():
                        break
                    # Early bail: if client disconnected while we're waiting
                    # for permission, auto-allow so the tool runs instead of
                    # failing with "Stream closed".
                    if not info.client:
                        logger.warning(
                            "Permission poll: client gone for %s — "
                            "auto-allowing %s to avoid Stream closed",
                            resolved_id, tool_name,
                        )
                        info.pending_permission = None
                        info.pending_tool_name = ""
                        info.pending_tool_input = {}
                        info.state = SessionState.WORKING
                        manager._emit_state(info)
                        return PermissionResultAllow()
                    try:
                        await anyio.sleep(0.1)
                    except Exception:
                        await asyncio.sleep(0.1)

                result_tuple = perm_result_holder[0]
                if result_tuple is None:
                    result_tuple = (PermissionResultDeny(message="No result"), False, False)
                # Support both 2-tuple (legacy) and 3-tuple
                if len(result_tuple) == 2:
                    permission_result, always = result_tuple
                    almost_always = False
                else:
                    permission_result, always, almost_always = result_tuple

                # Remember "Always Allow" for this tool for the rest of the session
                if isinstance(permission_result, PermissionResultAllow):
                    if always:
                        info.always_allowed_tools.add(tool_name)
                    elif almost_always:
                        info.almost_always_allowed_tools.add(tool_name)

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
            # mid-compact (e.g. partial streaming) and clearing here causes
            # the UI to flash back to "Working" during compaction.
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

                    # Track file modifications for rewind/snapshot support
                    tool_name = entry.name
                    logger.info("Tool use: %s (input keys: %s)", tool_name, list(inp.keys())[:5])
                    if tool_name in ('Edit', 'Write', 'MultiEdit', 'NotebookEdit'):
                        fp = inp.get('file_path', '') or inp.get('path', '')
                        logger.info("  File tracking: tool=%s fp=%s", tool_name, fp[:80] if fp else "(empty)")
                        if fp:
                            info.tracked_files.add(fp)
                            info._turn_had_direct_edit = True
                            logger.info("  tracked_files now has %d entries", len(info.tracked_files))

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

                    is_err = bool(getattr(block, 'is_error', False))

                    # Detect "Stream closed" permission failures and flag session
                    # for auto-retry after the current turn ends.  The turn will
                    # finish (ResultMessage), then _send_query / _drive_session
                    # will reconnect the client and resend the last user message
                    # so the failed tools actually execute.
                    if is_err and "Stream closed" in rt:
                        if not hasattr(info, '_stream_heal_count'):
                            info._stream_heal_count = 0
                        info._stream_heal_count += 1
                        info._stream_heal_needed = True
                        logger.warning(
                            "Stream closed error #%d for session %s — "
                            "will auto-reconnect and retry after turn ends",
                            info._stream_heal_count, session_id,
                        )
                        # Show the user what's happening (not an error, a status)
                        entry = LogEntry(
                            kind="system",
                            text=f"Stream error detected — will auto-reconnect and retry",
                        )
                        with info._lock:
                            info.entries.append(entry)
                            entry_index = len(info.entries) - 1
                        self._emit_entry(session_id, entry, entry_index)
                        # Still emit the original error for transparency but
                        # continue processing — the retry will happen after the
                        # turn ends.

                    entry = LogEntry(
                        kind="tool_result",
                        text=rt[:20000],
                        tool_use_id=getattr(block, 'tool_use_id', '') or '',
                        is_error=is_err,
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
                        # Extract a short human-readable label
                        label = _system_content_label(stripped)
                        # Deduplicate: don't emit if an identical system entry
                        # already exists in the tail (e.g. _interrupt_session
                        # already added "Session interrupted by user").
                        with info._lock:
                            already = any(
                                getattr(e, 'text', '') == label
                                for e in info.entries[-3:]
                            ) if info.entries else False
                        if already:
                            continue
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
                # Store pre-compaction token count for UI display
                if pre_tokens:
                    info.usage['pre_compact_tokens'] = pre_tokens
                self._emit_state(info)

                # Human-readable log entry
                tk_str = f"{pre_tokens // 1000}k" if pre_tokens >= 1000 else str(pre_tokens)
                label = f"Compacting context ({tk_str} tokens, {trigger})…"
                entry = LogEntry(kind="system", text=label)
                with info._lock:
                    info.entries.append(entry)
                    entry_index = len(info.entries) - 1
                self._emit_entry(session_id, entry, entry_index)

            elif subtype == 'turn_duration':
                # Per-turn timing info — log but don't emit to UI
                logger.info("Turn duration for %s: %s", session_id,
                            {k: v for k, v in data.items() if k != 'type'})

            elif subtype == 'init' and info.substatus == 'compacting':
                # End of compaction — session re-initialized
                info.substatus = ""
                self._emit_state(info)
                entry = LogEntry(kind="system", text="Context compacted")
                with info._lock:
                    info.entries.append(entry)
                    entry_index = len(info.entries) - 1
                self._emit_entry(session_id, entry, entry_index)
            else:
                # Forward any other system message as a push event for debugging
                if self._push_callback:
                    self._push_callback('system_message', {
                        'session_id': session_id,
                        'subtype': subtype,
                        'data': data,
                    })

        elif isinstance(message, ResultMessage):
            # Clear substatus on result (compaction is done if it was in progress)
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

                # Remap queue to new session ID
                with self._queue_lock:
                    if session_id in self._queues:
                        self._queues[result_session_id] = self._queues.pop(session_id)
                self._save_queues()

                # Remap user-set name to the new ID (server-side, no race)
                try:
                    from app.config import _remap_name
                    _remap_name(session_id, result_session_id)
                except Exception:
                    pass

                # Persist the old temp ID so all_sessions() filters it out
                # even if in-memory aliases haven't synced yet on refresh.
                try:
                    from app.config import _mark_remapped
                    _mark_remapped(session_id, result_session_id)
                except Exception:
                    pass

                # Remap kanban task↔session links so they point to the new ID
                try:
                    from app.db import create_repository
                    repo = create_repository()
                    repo.remap_session(session_id, result_session_id)
                except Exception:
                    pass

                # Notify frontend to update its references (URL, activeId, etc.)
                if self._push_callback:
                    self._push_callback(
                        'session_id_remapped',
                        {'old_id': session_id, 'new_id': result_session_id}
                    )

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
            # This is the AUTHORITATIVE context window size — unlike
            # ResultMessage.usage which is cumulative across the session.
            # NOTE: message.event is a STRING like "message_start", while
            # message.data is the dict payload with the actual content.
            evt_type = event_data.get('event', '')
            evt_data = event_data.get('data') or {}
            is_message_start = (
                evt_type == 'message_start'
                or (isinstance(evt_data, dict) and evt_data.get('type') == 'message_start')
            )
            # Log first StreamEvent per session for diagnostics (shape of event/data)
            if not getattr(info, '_stream_evt_logged', False):
                logger.info("StreamEvent for %s: event_type=%r data_type=%s data_keys=%s",
                            session_id, evt_type,
                            type(evt_data).__name__,
                            list(evt_data.keys())[:8] if isinstance(evt_data, dict) else '(not dict)')
                info._stream_evt_logged = True
            if is_message_start:
                logger.info("message_start detected for %s — evt_data keys: %s",
                            session_id, list(evt_data.keys()) if isinstance(evt_data, dict) else '(not dict)')
            if is_message_start and isinstance(evt_data, dict):
                msg = evt_data.get('message', {})
                logger.info("message_start msg keys: %s, has usage: %s",
                            list(msg.keys()) if isinstance(msg, dict) else '(not dict)',
                            'usage' in msg if isinstance(msg, dict) else False)
                if isinstance(msg, dict) and 'usage' in msg:
                    call_usage = msg['usage']
                    if isinstance(call_usage, dict):
                        # Preserve pre_compact_tokens if set
                        prev_pct = info.usage.get('pre_compact_tokens')
                        info.usage = dict(call_usage)
                        info.usage['_per_call'] = True
                        if prev_pct:
                            info.usage['pre_compact_tokens'] = prev_pct
                        logger.info("Per-call usage for %s: input=%s cache_read=%s cache_create=%s",
                                    session_id, call_usage.get('input_tokens'),
                                    call_usage.get('cache_read_input_tokens'),
                                    call_usage.get('cache_creation_input_tokens'))
                        # Push lightweight usage update to frontend
                        if self._push_callback:
                            self._push_callback('session_usage', {
                                'session_id': session_id,
                                'usage': info.usage,
                            })

            if self._push_callback:
                self._push_callback('stream_event', {
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
    # File-history snapshot support (rewind feature)
    # ------------------------------------------------------------------

    # File extensions worth tracking for change detection
    _SOURCE_EXTS = {
        '.py', '.js', '.ts', '.tsx', '.jsx', '.css', '.html', '.json',
        '.yaml', '.yml', '.toml', '.cfg', '.ini', '.sh', '.bat', '.md',
        '.txt', '.xml', '.sql', '.rb', '.go', '.rs', '.java', '.c',
        '.cpp', '.h', '.hpp', '.cs', '.vue', '.svelte', '.astro',
    }
    _SKIP_DIRS = {'.git', 'node_modules', '__pycache__', '.venv', 'venv',
                  '.tox', '.mypy_cache', '.pytest_cache', 'dist', 'build',
                  '.next', '.nuxt', '.claude'}

    # Set True to log per-step timing in _drive_session / _send_query
    _PROFILE_PIPELINE = False

    def _git_ls_files(self, cwd_path: Path) -> list:
        """Use `git ls-files` to get tracked files, respecting .gitignore.

        Returns a list of absolute Path objects, or None if git is unavailable
        or the directory is not a git repo.
        """
        import subprocess as _sp
        try:
            result = _sp.run(
                ["git", "ls-files", "-z"],
                cwd=str(cwd_path),
                capture_output=True,
                timeout=10,
            )
            if result.returncode != 0:
                return None
            raw = result.stdout
            if not raw:
                return []
            paths = []
            for rel in raw.split(b'\x00'):
                if rel:
                    paths.append(cwd_path / rel.decode('utf-8', errors='replace'))
            return paths
        except Exception:
            return None

    def _record_pre_turn_mtimes(self, info: SessionInfo) -> None:
        """Snapshot mtimes of source files in the working directory.

        Only used as a fallback when the streaming message handler doesn't
        see direct Edit/Write tool uses (e.g. Agent sub-agent edits).
        Uses `git ls-files` when available (fast, respects .gitignore),
        falls back to os.walk with directory pruning.
        """
        cwd = info.cwd
        if not cwd:
            return
        cwd_path = Path(cwd)
        if not cwd_path.is_dir():
            return

        mtimes = {}
        try:
            # Fast path: use git ls-files (respects .gitignore)
            git_files = self._git_ls_files(cwd_path)
            if git_files is not None:
                for f in git_files:
                    if f.suffix.lower() not in self._SOURCE_EXTS:
                        continue
                    try:
                        mtimes[str(f)] = f.stat().st_mtime
                    except OSError:
                        pass
            else:
                # Fallback: os.walk with directory pruning
                for dirpath, dirnames, filenames in os.walk(cwd_path):
                    dirnames[:] = [d for d in dirnames if d not in self._SKIP_DIRS]
                    for fname in filenames:
                        f = Path(dirpath) / fname
                        if f.suffix.lower() not in self._SOURCE_EXTS:
                            continue
                        try:
                            mtimes[str(f)] = f.stat().st_mtime
                        except OSError:
                            pass
        except Exception as e:
            logger.warning("_record_pre_turn_mtimes failed: %s", e)
        info._pre_turn_mtimes = mtimes
        logger.debug("_record_pre_turn_mtimes: recorded %d files in %s", len(mtimes), cwd)

    def _detect_changed_files(self, info: SessionInfo) -> set:
        """Compare current file mtimes against the pre-turn snapshot.

        Returns absolute paths of files that were created or modified
        since _record_pre_turn_mtimes was called.
        Uses `git ls-files` when available, falls back to os.walk with pruning.
        """
        cwd = info.cwd
        if not cwd:
            return set()
        cwd_path = Path(cwd)
        if not cwd_path.is_dir():
            return set()

        pre = info._pre_turn_mtimes
        changed = set()
        try:
            # Fast path: use git ls-files
            git_files = self._git_ls_files(cwd_path)
            if git_files is not None:
                for f in git_files:
                    if f.suffix.lower() not in self._SOURCE_EXTS:
                        continue
                    fpath = str(f)
                    try:
                        current_mtime = f.stat().st_mtime
                    except OSError:
                        continue
                    if fpath not in pre or pre[fpath] != current_mtime:
                        changed.add(fpath)
            else:
                # Fallback: os.walk with directory pruning
                for dirpath, dirnames, filenames in os.walk(cwd_path):
                    dirnames[:] = [d for d in dirnames if d not in self._SKIP_DIRS]
                    for fname in filenames:
                        f = Path(dirpath) / fname
                        if f.suffix.lower() not in self._SOURCE_EXTS:
                            continue
                        fpath = str(f)
                        try:
                            current_mtime = f.stat().st_mtime
                        except OSError:
                            continue
                        if fpath not in pre or pre[fpath] != current_mtime:
                            changed.add(fpath)
        except Exception as e:
            logger.warning("_detect_changed_files failed: %s", e)
        return changed

    def _prepopulate_tracked_files(self, info: SessionInfo) -> None:
        """Scan the session JSONL for tracked files from two sources:

        1. Past Edit/Write/MultiEdit/NotebookEdit tool_use blocks
        2. Existing file-history-snapshot entries (catches files tracked
           by previous daemon runs or the CLI itself)

        This ensures snapshots work after daemon restart or for resumed
        sessions.

        Only runs ONCE per session — after the initial scan, real-time
        tracking in _process_message keeps tracked_files up to date.
        Re-parsing a 38MB JSONL on every follow-up message was blocking
        the asyncio event loop and starving all other sessions.
        """
        if info._tracked_files_populated:
            return
        try:
            jsonl_path = self._find_session_jsonl(info)
            if not jsonl_path or not jsonl_path.exists():
                logger.info("_prepopulate_tracked_files: no JSONL for %s", info.session_id)
                return

            edit_tools = {'Edit', 'Write', 'MultiEdit', 'NotebookEdit'}
            found = set()
            max_version = {}  # track highest version per file for file_versions

            with open(jsonl_path, "r", encoding="utf-8", errors="replace") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        obj = json.loads(line)
                    except Exception:
                        continue

                    t = obj.get("type", "")

                    # Cache user/assistant UUIDs so _write_file_snapshot
                    # doesn't have to re-parse the entire JSONL every turn.
                    uid = obj.get("uuid", "")
                    if uid:
                        if t == "user":
                            info._last_user_uuid = uid
                        elif t == "assistant":
                            info._last_asst_uuid = uid

                    # Source 1: tool_use blocks in assistant messages
                    if t == "assistant":
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

                    # Source 2: existing file-history-snapshot entries
                    elif t == "file-history-snapshot":
                        snap = obj.get("snapshot", {})
                        for fp, binfo in snap.get("trackedFileBackups", {}).items():
                            if fp:
                                found.add(fp)
                            if isinstance(binfo, dict):
                                v = binfo.get("version", 0)
                                if v > max_version.get(fp, 0):
                                    max_version[fp] = v

            if found:
                info.tracked_files.update(found)
                # Restore version counters so new backups don't collide
                for fp, v in max_version.items():
                    if v > info.file_versions.get(fp, 0):
                        info.file_versions[fp] = v
                logger.info(
                    "_prepopulate_tracked_files(%s): found %d files from JSONL",
                    info.session_id, len(found),
                )
            info._tracked_files_populated = True
        except Exception as e:
            logger.warning("_prepopulate_tracked_files failed for %s: %s", info.session_id, e)
            # Mark as populated even on failure — don't retry on every message.
            # Real-time tracking in _process_message will catch new edits.
            info._tracked_files_populated = True

    def _write_file_snapshot(self, session_id: str, is_post_turn: bool = False) -> None:
        """Create file backups and append a file-history-snapshot to the JSONL.

        Replicates the native Claude Code CLI behavior:
        - Pre-turn  (is_post_turn=False): ``isSnapshotUpdate: false``,
          linked to the **user** message UUID.  Captures state before edits.
        - Post-turn (is_post_turn=True):  ``isSnapshotUpdate: true``,
          outer ``messageId`` = latest **assistant** UUID, inner
          ``messageId`` = the user UUID from the pre-turn snapshot.
          Captures state after edits.

        File change detection uses filesystem mtime comparison so it
        catches edits from Agent sub-agents, Bash, or anything else.
        """
        session_id = self._resolve_id(session_id)
        with self._lock:
            info = self._sessions.get(session_id)
        if not info:
            logger.info("_write_file_snapshot(%s): skipped (no session info)", session_id)
            return

        # Only fall back to filesystem mtime scanning when the streaming
        # message handler didn't see any direct Edit/Write tool uses.
        # This avoids scanning the entire project directory on every turn —
        # only needed when something opaque (Agent, Bash) may have edited files.
        if not info._turn_had_direct_edit:
            fs_changed = self._detect_changed_files(info)
            if fs_changed:
                info.tracked_files.update(fs_changed)
                logger.info("_write_file_snapshot(%s): filesystem fallback detected %d changed files",
                            session_id, len(fs_changed))

        if not info.tracked_files:
            logger.info("_write_file_snapshot(%s): skipped (no tracked files)", session_id)
            return

        try:
            sid = info.session_id
            history_dir = Path.home() / ".claude" / "file-history" / sid
            history_dir.mkdir(parents=True, exist_ok=True)

            tracked_backups = {}
            for fpath in list(info.tracked_files):
                p = Path(fpath)
                if not p.exists():
                    # Only record missing-file entry if we previously had a backup
                    if fpath in info._last_hashes:
                        tracked_backups[fpath] = {
                            "backupFileName": None,
                            "version": 0,
                            "backupTime": None,
                        }
                    continue
                try:
                    content = p.read_bytes()
                except Exception:
                    continue

                content_hash = hashlib.md5(content).hexdigest()[:16]

                # Skip if content hasn't changed since last backup
                if info._last_hashes.get(fpath) == content_hash:
                    continue

                version = info.file_versions.get(fpath, 0) + 1
                info.file_versions[fpath] = version
                info._last_hashes[fpath] = content_hash

                backup_name = f"{content_hash}@v{version}"

                backup_path = history_dir / backup_name
                if not backup_path.exists():
                    backup_path.write_bytes(content)

                tracked_backups[fpath] = {
                    "backupFileName": backup_name,
                    "version": version,
                    "backupTime": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
                }

            has_valid = any(
                isinstance(v, dict) and v.get("backupFileName")
                for v in tracked_backups.values()
            )
            if not has_valid:
                logger.info("_write_file_snapshot: no valid backups, skipping")
                return

            jsonl_path = self._find_session_jsonl(info)
            if not jsonl_path or not jsonl_path.exists():
                logger.warning("_write_file_snapshot: JSONL not found (cwd=%s, sid=%s)",
                               info.cwd, info.session_id)
                return

            # Read UUIDs from the TAIL of the JSONL only (last 64KB).
            # Previous approach re-read the entire 38MB file on every turn.
            last_user_uuid = ""
            last_asst_uuid = ""
            try:
                file_size = jsonl_path.stat().st_size
                tail_size = min(file_size, 65536)
                with open(jsonl_path, "rb") as rf:
                    rf.seek(max(0, file_size - tail_size))
                    tail = rf.read().decode("utf-8", errors="replace")
                for raw_line in tail.splitlines():
                    raw_line = raw_line.strip()
                    if not raw_line:
                        continue
                    try:
                        obj = json.loads(raw_line)
                    except Exception:
                        continue
                    t = obj.get("type", "")
                    uid = obj.get("uuid", "")
                    if t == "user" and uid:
                        last_user_uuid = uid
                    elif t == "assistant" and uid:
                        last_asst_uuid = uid
            except Exception:
                pass

            # CLI pattern:
            #   pre-turn:  outer=user_uuid, inner=user_uuid, isSnapshotUpdate=false
            #   post-turn: outer=asst_uuid, inner=user_uuid, isSnapshotUpdate=true
            fallback = str(uuid_mod.uuid4())
            if is_post_turn:
                outer_mid = last_asst_uuid or fallback
                inner_mid = last_user_uuid or outer_mid
                is_update = True
            else:
                outer_mid = last_user_uuid or fallback
                inner_mid = outer_mid
                is_update = False

            now_iso = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
            snapshot_entry = {
                "type": "file-history-snapshot",
                "messageId": outer_mid,
                "snapshot": {
                    "messageId": inner_mid,
                    "trackedFileBackups": tracked_backups,
                    "timestamp": now_iso,
                },
                "isSnapshotUpdate": is_update,
            }

            with open(jsonl_path, "a", encoding="utf-8") as f:
                f.write(json.dumps(snapshot_entry) + "\n")

            logger.info(
                "Wrote file-history-snapshot for %s (update=%s, %d files, outer=%s inner=%s)",
                sid, is_update,
                sum(1 for v in tracked_backups.values()
                    if isinstance(v, dict) and v.get("backupFileName")),
                outer_mid[:12], inner_mid[:12],
            )
        except Exception as e:
            logger.warning("Failed to write file snapshot for %s: %s", session_id, e)

    @staticmethod
    def _find_session_jsonl(info: SessionInfo) -> Optional[Path]:
        """Locate the .jsonl file for a session on disk."""
        projects_dir = Path.home() / ".claude" / "projects"
        sid = info.session_id

        # Try the encoded cwd first (fastest path)
        cwd = info.cwd or ""
        if cwd:
            encoded = cwd.replace("\\", "/").replace(":", "-").replace("/", "-").replace("_", "-")
            candidate = projects_dir / encoded / f"{sid}.jsonl"
            if candidate.exists():
                return candidate

        # Fallback: scan project directories
        if projects_dir.is_dir():
            for d in projects_dir.iterdir():
                if d.is_dir() and not d.name.startswith("subagents"):
                    candidate = d / f"{sid}.jsonl"
                    if candidate.exists():
                        return candidate
        return None

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

                # Never recover planner sessions
                if meta.get("session_type") == "planner":
                    continue

                last_activity = meta.get("last_activity", 0)
                age = now - last_activity
                if age > _MAX_RECOVERY_AGE:
                    logger.info(
                        "Skipping stale session %s (%.0f min old)", sid, age / 60
                    )
                    continue

                name = meta.get("name", "")
                cwd = meta.get("cwd", "")
                model = meta.get("model", "")

                # Guard: if the .jsonl file was deleted (user chose to delete
                # the session), do NOT recover it — that would undo the delete.
                jsonl_path = None
                projects_dir = Path.home() / ".claude" / "projects"
                if cwd:
                    encoded = cwd.replace("\\", "/").replace(":", "-").replace("/", "-").replace("_", "-")
                    candidate = projects_dir / encoded / f"{sid}.jsonl"
                    if candidate.exists():
                        jsonl_path = candidate
                if not jsonl_path and projects_dir.is_dir():
                    for d in projects_dir.iterdir():
                        if d.is_dir() and not d.name.startswith("subagents"):
                            candidate = d / f"{sid}.jsonl"
                            if candidate.exists():
                                jsonl_path = candidate
                                break
                if not jsonl_path:
                    logger.info(
                        "Skipping recovery of %s — .jsonl file was deleted", sid
                    )
                    continue

                # Repair incomplete assistant turns so --resume doesn't choke.
                # If the daemon was killed mid-response, the last .jsonl entry
                # is an assistant message with stop_reason=null — the CLI can't
                # resume from that state and the stream dies immediately.
                _repair_incomplete_jsonl(jsonl_path)

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

        Uses push_callback to ensure cross-context compatibility.
        Also schedules a registry save so crash recovery data stays fresh.
        When a session transitions to IDLE, auto-dispatches queued messages.
        """
        # Track when session entered WORKING state for elapsed timer
        if info.state == SessionState.WORKING and info.working_since == 0.0:
            info.working_since = time.time()
        elif info.state != SessionState.WORKING:
            info.working_since = 0.0
        if self._push_callback:
            data = info.to_state_dict()
            # Include queue data from server-side store
            with self._queue_lock:
                q = self._queues.get(info.session_id, [])
                if q:
                    data["queue"] = list(q)
            try:
                self._push_callback('session_state', data)
            except Exception as cb_err:
                logger.error("_emit_state push_callback failed for %s (state=%s): %s",
                             info.session_id, info.state, cb_err)
        # Keep the persistent registry up to date
        self._schedule_registry_save()
        # Auto-dispatch queued messages when session goes IDLE —
        # but NOT if the user just interrupted (flag is cleared on next
        # send_message so the session resumes normal dispatch after).
        if info.state == SessionState.IDLE and not getattr(info, '_interrupted', False):
            self._try_dispatch_queue(info.session_id)
            # Safety net: re-emit IDLE state after 3 seconds in case the first
            # push was silently lost (SocketIO transport hiccup, tab sleeping,
            # etc.).  If the session is no longer IDLE (queue dispatched a
            # follow-up or user sent a new message), the re-emit is skipped.
            sid = info.session_id
            def _deferred_idle_reemit():
                with self._lock:
                    recheck = self._sessions.get(sid)
                if recheck and recheck.state == SessionState.IDLE:
                    # Safety net for race condition: if a message was queued
                    # right as the session went idle, dispatch it now.
                    self._try_dispatch_queue(sid)
                    # Re-check state — dispatch may have moved it to WORKING
                    if recheck.state != SessionState.IDLE:
                        return
                    logger.debug("Deferred IDLE re-emit for %s", sid)
                    if self._push_callback:
                        data = recheck.to_state_dict()
                        with self._queue_lock:
                            q = self._queues.get(sid, [])
                            if q:
                                data["queue"] = list(q)
                        try:
                            self._push_callback('session_state', data)
                        except Exception:
                            pass
            t = threading.Timer(3.0, _deferred_idle_reemit)
            t.daemon = True
            t.start()

    def _log_auto_approved(self, session_id: str, info, tool_name: str,
                           tool_input, policy: str) -> None:
        """Log an audit entry when a tool is auto-approved (or blocked).

        This must never raise — a logging failure should not break the
        permission callback that auto-approved the tool.
        """
        try:
            desc = ""
            if isinstance(tool_input, dict):
                desc = (tool_input.get("command", "")
                        or tool_input.get("file_path", "")
                        or tool_input.get("path", "")
                        or tool_input.get("pattern", ""))
            if policy == "almost-always-blocked":
                text = f"Dangerous command blocked by Almost Always — prompting for manual approval\n{tool_name}: {desc}"
                is_error = True
            else:
                text = f"Auto-approved ({policy})\n{tool_name}: {desc}"
                is_error = False
            entry = LogEntry(kind="permission", text=text, name=tool_name, is_error=is_error)
            with info._lock:
                info.entries.append(entry)
                idx = len(info.entries) - 1
            self._emit_entry(session_id, entry, idx)
        except Exception as e:
            logger.warning("Failed to log auto-approved permission: %s", e)

    def _emit_entry(self, session_id: str, entry: LogEntry, index: int) -> None:
        """Push a new log entry to all connected WebSocket clients."""
        if self._push_callback:
            data = {
                'session_id': session_id,
                'entry': entry.to_dict(),
                'index': index,
            }
            try:
                self._push_callback('session_entry', data)
            except Exception as cb_err:
                logger.error("_emit_entry push_callback failed for %s (entry %d): %s",
                             session_id, index, cb_err)

    def _emit_permission(self, session_id: str, tool_name: str, tool_input: dict) -> None:
        """Push a permission request to all connected WebSocket clients.

        This is called from inside an anyio task group (the SDK's control
        handler), so we use push_callback to ensure it reaches the clients.
        """
        # Never broadcast permission requests for hidden utility sessions
        with self._lock:
            info = self._sessions.get(session_id)
        if info and info.session_type in ("planner", "title"):
            return
        if self._push_callback:
            data = {
                'session_id': session_id,
                'tool_name': tool_name,
                'tool_input': tool_input,
            }
            self._push_callback('session_permission', data)

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

    # ------------------------------------------------------------------
    # Hook-based permission methods
    # ------------------------------------------------------------------

    def resolve_hook_permission(self, req_id: str, action: str) -> bool:
        """Resolve a pending hook-based permission request."""
        with self._hook_lock:
            entry = self._hook_pending.get(req_id)
        if not entry:
            return False
        entry["result"] = action
        entry["event"].set()
        return True

    def hook_pre_tool(self, tool_name: str, tool_input: dict, session_id: str) -> dict:
        """Handle a PreToolUse hook. Blocks until user responds."""
        import uuid
        req_id = str(uuid.uuid4())[:8]
        event = threading.Event()
        with self._hook_lock:
            self._hook_pending[req_id] = {"event": event, "result": "allow"}

        self._hook_permission_start(session_id, req_id, tool_name, tool_input)

        # Block until user responds (up to 1 hour)
        event.wait(timeout=3600)

        with self._hook_lock:
            entry = self._hook_pending.pop(req_id, {})
        result = entry.get("result", "allow")

        self._hook_permission_end(session_id)

        return {"action": result}

    # ------------------------------------------------------------------
    # Unified permission resolution
    # ------------------------------------------------------------------

    def resolve_permission_unified(self, session_id: str, allow: bool, always: bool = False,
                                   almost_always: bool = False) -> dict:
        """Resolve permission — auto-detects hook vs SDK callback."""
        session_id = self._resolve_id(session_id)
        with self._lock:
            info = self._sessions.get(session_id)
        if not info:
            return {"ok": False, "error": "Session not found"}

        hook_req_id = getattr(info, '_hook_req_id', None)
        if hook_req_id:
            action = "allow" if allow else "deny"
            resolved = self.resolve_hook_permission(hook_req_id, action)
            if resolved:
                return {"ok": True}
            return {"ok": False, "error": "Hook permission request not found"}
        else:
            return self.resolve_permission(session_id, allow=allow, always=always,
                                           almost_always=almost_always)
