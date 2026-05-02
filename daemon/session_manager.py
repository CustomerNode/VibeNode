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
import signal
import subprocess as _subprocess
import tempfile
import threading
import time
import uuid as uuid_mod
from datetime import datetime, timezone
from enum import Enum
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from daemon.backends.base import (
    AgentSDK,
    SessionOptions,
    PermissionResult,
    PermissionAction,
)
from daemon.backends.messages import VibeNodeMessage, MessageKind, BlockKind
from daemon.backends.chat_store import ChatStore
from daemon.message_queue import MessageQueue
from daemon.permission_manager import PermissionManager
from daemon.session_registry import SessionRegistry

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Ensure Claude Code CLI is discoverable.
# When the daemon is spawned with CREATE_NO_WINDOW the PATH may be stripped.
# Add the standard install location so shutil.which("claude") always works.
# ---------------------------------------------------------------------------
_extra_path_dirs = [
    str(Path.home() / ".local" / "bin"),      # Linux: pip --user, npm --prefix ~/.local
    str(Path.home() / ".npm-global" / "bin"), # Linux: npm config set prefix ~/.npm-global
    str(Path.home() / ".npm" / "bin"),        # Linux: some npm versions
    str(Path.home() / ".volta" / "bin"),      # Volta node version manager
    "/opt/homebrew/bin",                       # macOS Apple Silicon (Homebrew)
    "/usr/local/bin",                          # macOS Intel (Homebrew) / Linux common
    "/usr/bin",                                # Linux fallback for system-installed claude
]

# nvm (Node Version Manager) installs node into versioned directories that
# can't be known statically.  When launched from a .desktop file or other
# non-interactive shell, ~/.bashrc is not sourced so nvm's shims are absent
# from PATH.  Resolve the active version from NVM_BIN (set when nvm is live)
# or from ~/.nvm/alias/default (the version that would activate on login).
_nvm_dir = Path.home() / ".nvm"
if _nvm_dir.is_dir():
    _nvm_bin = os.environ.get("NVM_BIN", "")
    if _nvm_bin and os.path.isdir(_nvm_bin):
        _extra_path_dirs.append(_nvm_bin)
    else:
        try:
            _alias_file = _nvm_dir / "alias" / "default"
            if _alias_file.exists():
                _version = _alias_file.read_text(encoding="utf-8").strip().lstrip("v")
                # Resolve lts/* aliases (e.g. "lts/iron" → ~/.nvm/alias/lts/iron)
                if "/" in _version:
                    _lts_file = _nvm_dir / "alias" / _version
                    if _lts_file.exists():
                        _version = _lts_file.read_text(encoding="utf-8").strip().lstrip("v")
                _nvm_node_bin = _nvm_dir / "versions" / "node" / ("v" + _version) / "bin"
                if _nvm_node_bin.is_dir():
                    _extra_path_dirs.append(str(_nvm_node_bin))
        except Exception:
            pass  # Best-effort — never block startup

_current_path = os.environ.get("PATH", "")
_dirs_to_add = [d for d in _extra_path_dirs if d not in _current_path]
if _dirs_to_add:
    os.environ["PATH"] = os.pathsep.join(_dirs_to_add) + os.pathsep + _current_path

# SDK Patches — now applied by AgentSDK.apply_patches() in SessionManager.__init__().
# See daemon/backends/claude.py for the Claude implementation.


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
    client: Optional[object] = None  # Opaque handle from AgentSDK.create_session()
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
    _post_turn_mtimes: dict = field(default_factory=dict) # carried forward from _detect_changed_files
    _turn_had_direct_edit: bool = False                  # True if streaming saw Edit/Write this turn
    _turn_content_started: bool = False                  # True once first ASSISTANT message arrives this turn
    _awaiting_compact_drain: bool = False                # True when RESULT seen but IDLE emit deferred for post-turn compact check
    _tracked_files_populated: bool = False               # True after first _prepopulate_tracked_files
    _cached_git_files: list = field(default_factory=list) # cached git ls-files result
    _cached_git_files_ts: float = 0.0                     # time.time() when _cached_git_files was set
    _mtime_turn_count: int = 0                            # how many turns have used mtime carry-forward
    _last_user_uuid: str = ""                            # cached from JSONL, updated by _process_message
    _last_asst_uuid: str = ""                            # cached from JSONL, updated by _process_message
    created_ts: float = 0.0  # time.time() when session was created
    _cli_pid: int = 0  # PID of the CLI subprocess, for orphan cleanup
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
        # Always include substatus so the frontend can distinguish
        # "cleared" (empty string) from "not provided" (key absent).
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
# Registry file for crash recovery — moved to daemon/session_registry.py
# ---------------------------------------------------------------------------


# ── Stream Closed on Recovery Bug ──────────────────────────────────────
# JSONL repair logic has been moved to ClaudeJsonlStore.repair_incomplete_turn()
# in daemon/backends/claude_store.py.  See that class for the implementation.


class SessionManager:
    """Manages all Claude Code SDK sessions on a dedicated asyncio event loop."""

    def __init__(self, sdk: AgentSDK = None, store: ChatStore = None):
        # ── Backend abstraction (Phase 2 OOP refactor) ──
        # Dependency injection: defaults to Claude backend if not specified.
        # This is backward-compatible — no callers need to change.
        if sdk is None:
            from daemon.backends.claude import ClaudeAgentSDK
            sdk = ClaudeAgentSDK()
        if store is None:
            from daemon.backends.claude_store import ClaudeJsonlStore
            store = ClaudeJsonlStore()
        self._sdk: AgentSDK = sdk
        self._store: ChatStore = store

        # Apply SDK-specific patches (moved from module-level L66-69)
        try:
            self._applied_patches = self._sdk.apply_patches()
        except Exception as _patch_err:
            logger.warning("Could not apply SDK patches: %s", _patch_err)
            self._applied_patches = []

        self._sessions: dict[str, SessionInfo] = {}
        self._id_aliases: dict[str, str] = {}  # old_id -> new_id for SDK remaps
        self._lock = threading.Lock()
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._thread: Optional[threading.Thread] = None
        self._push_callback = None
        self._started = False
        # Session registry (crash recovery) — delegated to SessionRegistry
        self._reg = SessionRegistry()
        # Permission policy & UI prefs — delegated to PermissionManager
        self._pm = PermissionManager(emit_entry_fn=self._emit_entry)
        # Hook-based permission storage
        self._hook_pending = {}  # {req_id: {"event": threading.Event, "result": str}}
        self._hook_lock = threading.Lock()
        # Reconnect throttle: limit concurrent CLI process spawns to prevent
        # thundering-herd failures when multiple sessions die simultaneously.
        # Created lazily on the event loop (asyncio.Semaphore is loop-bound).
        self._reconnect_semaphore: Optional[asyncio.Semaphore] = None
        # Server-side message queue (per-session, FIFO) — delegated to MessageQueue
        self._mq = MessageQueue()

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def start(self, push_callback=None) -> None:
        """Start the background event loop thread. Called once at app startup."""
        if self._started:
            return
        self._push_callback = push_callback
        self._mq.set_push_callback(push_callback)
        self._loop = asyncio.new_event_loop()
        self._thread = threading.Thread(
            target=self._run_loop, daemon=True, name="session-manager-loop"
        )
        self._thread.start()
        self._started = True
        logger.info("SessionManager started")

        # Recover sessions from a previous crash (non-blocking background task)
        threading.Thread(
            target=lambda: self._reg.recover_sessions(self.start_session, self._store),
            daemon=True,
            name="session-recovery"
        ).start()

        # Periodic orphan process cleanup (Windows-critical: child processes
        # survive when parent claude.exe crashes).  Runs every 60s.
        self._orphan_sweep_timer: Optional[threading.Timer] = None
        self._schedule_orphan_sweep()

    def _run_loop(self) -> None:
        """Entry point for the background thread."""
        asyncio.set_event_loop(self._loop)
        self._loop.run_forever()

    def stop(self) -> None:
        """Stop the event loop and all sessions. Called on shutdown."""
        if not self._started:
            return
        # Cancel any pending registry save timer
        self._reg.cancel_timer()
        # Cancel orphan sweep timer
        if getattr(self, '_orphan_sweep_timer', None):
            self._orphan_sweep_timer.cancel()
            self._orphan_sweep_timer = None
        # Flush queue to disk
        self._mq.flush()
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


        # Normalize cwd to OS-native path separators (cross-platform)
        if cwd:
            cwd = os.path.normpath(cwd)
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
            # Reset any stale substatus from the previous turn, then re-set
            # if this turn is a /compact command.
            info.substatus = ""
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
            # Echo back the original tool input — CLI expects updatedInput
            # to be the full input dict, not null (causes H.includes crash).
            # See _make_permission_callback() and sdk_transport_adapter.py
            # for the full explanation of this requirement.
            original_input = info.pending_tool_input if isinstance(info.pending_tool_input, dict) else {}
            result = self._sdk.make_permission_result_allow(original_input)
        else:
            result = self._sdk.make_permission_result_deny(
                message="User denied permission", interrupt=False
            )

        # Resolve the permission by setting the anyio Event.
        perm_tuple = info.pending_permission  # (anyio.Event, result_holder)
        info.pending_permission = None

        if isinstance(perm_tuple, tuple) and len(perm_tuple) == 2:
            perm_event, result_holder = perm_tuple
            result_holder[0] = (result, always, almost_always)
            perm_event.set()  # threading.Event.set() is fully thread-safe

        return {"ok": True}

    # ------------------------------------------------------------------
    # Permission methods — thin wrappers delegating to PermissionManager
    # ------------------------------------------------------------------

    def get_permission_policy(self) -> dict:
        """Return the current permission policy and custom rules."""
        return self._pm.get_permission_policy()

    def set_permission_policy(self, policy: str, custom_rules: dict = None) -> None:
        """Update the permission policy (synced from browser)."""
        self._pm.set_permission_policy(policy, custom_rules)

    # ------------------------------------------------------------------
    # UI Preferences — thin wrappers delegating to PermissionManager
    # ------------------------------------------------------------------

    def get_ui_prefs(self) -> dict:
        """Return all persisted UI preferences."""
        return self._pm.get_ui_prefs()

    def set_ui_prefs(self, prefs: dict) -> None:
        """Merge new preferences into saved UI prefs and persist."""
        self._pm.set_ui_prefs(prefs)

    def _should_auto_approve(self, tool_name: str, tool_input: dict) -> bool:
        """Check if a tool use should be auto-approved — delegates to PermissionManager."""
        return self._pm.should_auto_approve(tool_name, tool_input)

    @classmethod
    def _is_dangerous(cls, tool_name: str, tool_input) -> bool:
        """Return True if tool_input looks destructive — delegates to PermissionManager."""
        return PermissionManager.is_dangerous(tool_name, tool_input)

    # ------------------------------------------------------------------
    # Server-side message queue
    # ------------------------------------------------------------------

    # ------------------------------------------------------------------
    # Queue methods — thin wrappers delegating to MessageQueue
    # PERF-CRITICAL #8: Debounced saves preserved in daemon/message_queue.py
    # ------------------------------------------------------------------

    def _save_queues(self) -> None:
        """Debounced queue save — delegates to MessageQueue. See CLAUDE.md #8."""
        self._mq.save_queues()

    def _emit_queue_update(self, session_id: str) -> None:
        self._mq.emit_queue_update(session_id)

    def queue_message(self, session_id: str, text: str) -> dict:
        """Add a message to a session's queue."""
        session_id = self._resolve_id(session_id)
        result = self._mq.queue_message(session_id, text)

        # If the session is already idle, dispatch immediately
        info = self._sessions.get(session_id)
        if info and info.state == SessionState.IDLE:
            self._try_dispatch_queue(session_id)

        return result

    def get_queue(self, session_id: str) -> list:
        return self._mq.get_queue(self._resolve_id(session_id))

    def remove_queue_item(self, session_id: str, index: int) -> dict:
        return self._mq.remove_queue_item(self._resolve_id(session_id), index)

    def edit_queue_item(self, session_id: str, index: int, text: str) -> dict:
        return self._mq.edit_queue_item(self._resolve_id(session_id), index, text)

    def clear_queue(self, session_id: str) -> dict:
        return self._mq.clear_queue(self._resolve_id(session_id))

    def _try_dispatch_queue(self, session_id: str) -> None:
        """Dispatch next queued message — delegates to MessageQueue with send_fn callback."""
        self._mq.try_dispatch_queue(session_id, self.send_message)

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
            self._mq.pop_queue(session_id)

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
        # Merge queue data into state dicts
        for s in states:
            sid = s.get("session_id", "")
            q = self._mq.get_queue_data(sid)
            if q:
                s["queue"] = q
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

    # PERF-CRITICAL: Returns len(entries) without serialization (0-1ms vs 25-32ms). See CLAUDE.md #6.
    #
    # LESSON LEARNED (2026-04-12): The watchdog endpoint (/api/live/state)
    # polled every 10 seconds and used len(get_entries(session_id)) to count
    # entries.  get_entries() serializes EVERY LogEntry to a dict, JSON-encodes
    # the list, sends it over TCP IPC, deserializes — then Python counts len().
    # For a 300-entry session: 25-32ms to return the integer 300.  This method
    # returns len(info.entries) directly — O(1), no serialization, 0-1ms.
    def get_entry_count(self, session_id: str) -> int:
        """Return the number of log entries without serializing them."""
        session_id = self._resolve_id(session_id)
        with self._lock:
            info = self._sessions.get(session_id)
        if not info:
            return 0
        return len(info.entries)

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

        Uses a semaphore to limit concurrent reconnects — when multiple
        sessions die simultaneously (e.g. network blip), spawning too many
        CLI processes at once overwhelms the system and they all timeout on
        the "initialize" control request.
        """
        # Lazy-init the semaphore on the event loop (can't create in __init__
        # because the loop doesn't exist yet).
        if self._reconnect_semaphore is None:
            self._reconnect_semaphore = asyncio.Semaphore(2)

        resolved = self._resolve_id(session_id)
        logger.info("Reconnecting SDK client for %s (resolved: %s) — "
                     "waiting for reconnect slot", session_id, resolved)

        try:
            async with self._reconnect_semaphore:
                logger.info("Reconnecting SDK client for %s — got slot", session_id)

                # Tear down the dead client
                if info.client:
                    try:
                        await self._sdk.disconnect(info.client)
                    except Exception:
                        pass
                    info.client = None

                # Repair incomplete .jsonl before reconnecting — if the stream
                # died mid-response, the last entry has stop_reason=null and
                # --resume will choke on it immediately.
                try:
                    self._store.repair_incomplete_turn(resolved, cwd=info.cwd or "")
                except Exception as _rep_err:
                    logger.warning("_reconnect_client: jsonl repair failed: %s", _rep_err)

                # Small stagger to avoid slamming the system when multiple
                # sessions are queued behind the semaphore.
                await asyncio.sleep(0.5)

                try:
                    options = SessionOptions(
                        cwd=info.cwd or None,
                        resume=resolved,
                        permission_callback=self._make_permission_callback(session_id),
                        model=info.model or None,
                        permission_mode="default",
                        include_partial_messages=True,
                    )
                    client = await self._sdk.create_session(options)
                    await self._sdk.connect(client)
                    info.client = client
                    # Update tracked PID so orphan sweep doesn't kill the new process
                    info._cli_pid = self._sdk.extract_process_pid(info.client)
                    logger.info("Reconnected SDK client for %s (new PID %d)",
                                session_id, info._cli_pid)
                    return True
                except Exception as e:
                    logger.exception("Failed to reconnect SDK client for %s: %s", session_id, e)
                    return False
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.exception("_reconnect_client semaphore error for %s: %s", session_id, e)
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
            options = SessionOptions(
                cwd=cwd or None,
                resume=session_id if resume else None,
                permission_callback=self._make_permission_callback(session_id),
                model=model or None,
                system_prompt=system_prompt or None,
                max_turns=max_turns or None,
                allowed_tools=allowed_tools or [],
                permission_mode=permission_mode or "default",
                include_partial_messages=True,
                extra_args=extra_args or {},
            )
            _profile_log("options_built")
            client = await self._sdk.create_session(options)
            info.client = client

            # Connect with no prompt. The SDK auto-sets permission_prompt_tool_name="stdio"
            # when can_use_tool is set. Prompt=None becomes _empty_stream() which is an
            # AsyncIterator, so the streaming mode check passes.
            # PERF-CRITICAL: Mtime scan overlapped with client.connect() — moving after adds 70-90ms. See CLAUDE.md #5.
            #
            # LESSON LEARNED (2026-04-12): client.connect() takes 700-1000ms
            # (Claude CLI subprocess spawn).  _record_pre_turn_mtimes takes
            # 70-90ms (git ls-files + stat).  They're independent — mtime scan
            # only reads info.cwd, doesn't need the SDK connection.  Starting
            # the scan before await connect() hides it completely behind the
            # 900ms startup.  Measured result: pre_turn_mtimes_done equals
            # client_connected timestamp (0ms additional wait).
            loop = asyncio.get_event_loop()
            mtime_task = loop.run_in_executor(None, self._record_pre_turn_mtimes, info)

            await self._sdk.connect(client)
            _profile_log("client_connected")

            # Capture the CLI subprocess PID for orphan cleanup.
            # If _drive_session exits (crash, cancel, normal end) without
            # going through _close_session, the finally block uses this to
            # kill the process tree so child node.exe workers don't linger.
            info._cli_pid = self._sdk.extract_process_pid(info.client)

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
            await mtime_task
            _profile_log("pre_turn_mtimes_done")

            # Add user's message to the log and send
            if prompt:
                entry = LogEntry(kind="user", text=prompt[:20000])
                with info._lock:
                    info.entries.append(entry)
                    entry_index = len(info.entries) - 1
                self._emit_entry(session_id, entry, entry_index)
                await self._sdk.send_query(client, prompt)
                _profile_log("query_sent")

            # Process messages — the SDK backend normalizes all messages to
            # VibeNodeMessage before yielding them.  None/unknown types are
            # filtered out by the backend's receive_response().
            #
            # IMPORTANT: receive_response() terminates after the equivalent
            # of a ResultMessage (turn complete).  See comment in
            # ClaudeAgentSDK.receive_response() for details.
            info._stream_evt_logged = False
            _first_msg_logged = False
            if prompt:
                async for message in self._sdk.receive_response(client):
                    # If a newer task has replaced us, stop processing —
                    # our stream is stale and we must not touch state.
                    if info.task is not asyncio.current_task():
                        break
                    if not _first_msg_logged:
                        _profile_log("first_stream_message (%s)" % message.kind.value)
                        _first_msg_logged = True
                    if message.kind == MessageKind.RESULT:
                        got_result = True
                        _profile_log("result_message")
                    try:
                        await self._process_message(session_id, message)
                        if message.kind == MessageKind.RESULT:
                            result_handled = True
                    except Exception as pm_err:
                        # Don't let one bad message kill the entire stream.
                        logger.exception(
                            "_process_message error for %s (msg kind %s): %s",
                            session_id, message.kind.value, pm_err
                        )
            else:
                # No prompt (empty session or bare resume) — nothing to receive.
                # Go straight to IDLE so send_message() can dispatch follow-ups.
                got_result = True
                result_handled = True
                info.state = SessionState.IDLE
                self._emit_state(info)

            # Post-turn compact drain (same as _send_query — see comment there).
            if result_handled and info._awaiting_compact_drain \
                    and info.task is asyncio.current_task():
                await self._post_turn_compact_drain(session_id, info)
                info._awaiting_compact_drain = False
                if info.task is asyncio.current_task() \
                        and info.state == SessionState.IDLE:
                    self._emit_state(info)

            # Safety net: if the stream ended without a ResultMessage,
            # force IDLE so the session isn't stuck.  Skip if we already
            # got a ResultMessage (drain above handled IDLE + queue dispatch).
            # Also skip if superseded — the new task owns state.
            if not got_result and info.state == SessionState.WORKING \
                    and info.task is asyncio.current_task():
                logger.warning("_drive_session for %s: stream ended without "
                               "ResultMessage, forcing IDLE", session_id)
                info._awaiting_compact_drain = False
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
                        # Exponential backoff: 2s, 4s, 8s between retries
                        backoff = 2 ** heal_count
                        logger.info(
                            "Self-healing (drive): reconnecting %s after %d stream errors "
                            "(backoff %ds)",
                            session_id, heal_count, backoff,
                        )
                        entry = LogEntry(kind="system", text="Reconnecting session...")
                        with info._lock:
                            info.entries.append(entry)
                            entry_index = len(info.entries) - 1
                        self._emit_entry(session_id, entry, entry_index)
                        await asyncio.sleep(backoff)

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
            # Post-turn snapshot
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

            # Orphan cleanup: if the CLI process died (crash, OOM, etc.),
            # kill its child process tree.  On Windows, child node.exe
            # workers survive when the parent dies — they become orphans
            # eating CPU/RAM until the machine is rebooted.
            if info and info._cli_pid:
                try:
                    # Check if the CLI process is actually dead
                    proc_alive = True
                    try:
                        os.kill(info._cli_pid, 0)  # signal 0 = existence check
                    except (OSError, ProcessLookupError):
                        proc_alive = False
                    if not proc_alive:
                        logger.info("CLI process %d for %s is dead — "
                                    "cleaning up orphaned child processes",
                                    info._cli_pid, session_id)
                        self._kill_process_tree(info._cli_pid)
                        info._cli_pid = 0
                except Exception as cleanup_err:
                    logger.debug("Orphan cleanup for %s: %s", session_id, cleanup_err)

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

        # Pre-flight liveness check: the client object exists but the
        # underlying CLI process may have exited between turns.  Silently
        # reconnect before sending so the user never sees "Stream lost".
        if info.client:
            cli_pid = self._sdk.extract_process_pid(info.client)
            if cli_pid == 0:
                # Process already exited — silent reconnect
                logger.info("_send_query: %s CLI process dead before send — "
                            "silent reconnect", session_id)
                if await self._reconnect_client(session_id, info):
                    logger.info("_send_query: silent reconnect succeeded for %s", session_id)
                else:
                    if info.state == SessionState.WORKING:
                        info.state = SessionState.IDLE
                        info.error = "Session disconnected — reconnect failed"
                        entry = LogEntry(kind="system",
                                         text="Could not reconnect — please try again",
                                         is_error=True)
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
        # ── Parallel pre-turn file operations ──
        #
        # PERF-CRITICAL: _turn_had_direct_edit reset BEFORE gather — moving it creates a race. See CLAUDE.md #3.
        # PERF-CRITICAL: asyncio.gather runs snapshot+mtimes in parallel — sequential adds 60-70ms. See CLAUDE.md #2.
        #
        # LESSON LEARNED (2026-04-12 performance overhaul):
        #
        # These two operations were originally sequential await calls, adding
        # 66-139ms to every follow-up message before the query reached Claude.
        # Profiling proved they access disjoint SessionInfo fields:
        #   _write_file_snapshot: tracked_files, _turn_had_direct_edit, _last_hashes, file_versions
        #   _record_pre_turn_mtimes: _pre_turn_mtimes, _post_turn_mtimes, _mtime_turn_count, _cached_git_files
        # Running them via asyncio.gather() cut the measured pre-work from
        # 66-139ms to 10ms (with carry-forward hitting on most turns).
        #
        # The _turn_had_direct_edit reset MUST happen before BOTH functions
        # start.  It was originally between the two sequential awaits.  When
        # we parallelized, we moved it above the gather.  If it were moved
        # back between or after, _write_file_snapshot could read the stale
        # True value from the previous turn and skip the snapshot.
        info._turn_had_direct_edit = False
        info._turn_content_started = False
        info._awaiting_compact_drain = False
        await asyncio.gather(
            loop.run_in_executor(None, self._write_file_snapshot, session_id, False),
            loop.run_in_executor(None, self._record_pre_turn_mtimes, info),
        )
        _profile_log("write_snapshot+record_mtimes")

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
                    async for _msg in self._sdk.receive_response(info.client):
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
            await self._sdk.send_query(info.client, text)
            _profile_log("query_sent")

            # Process response messages — normalized to VibeNodeMessage by backend
            info.usage.pop('_per_call', None)  # clear stale per-call marker from previous turn
            info._stream_evt_logged = False  # re-enable diagnostic log for this turn
            async for message in self._sdk.receive_response(info.client):
                # If a newer task has replaced us, stop processing —
                # our stream is stale and we must not touch state.
                if info.task is not asyncio.current_task():
                    break
                if not _first_msg_logged:
                    _profile_log("first_stream_message (%s)" % message.kind.value)
                    _first_msg_logged = True
                if message.kind == MessageKind.RESULT:
                    got_result = True
                    _profile_log("result_message")
                try:
                    await self._process_message(session_id, message)
                    if message.kind == MessageKind.RESULT:
                        result_handled = True
                except Exception as pm_err:
                    # Don't let one bad message kill the entire stream.
                    # Log the error and continue processing remaining
                    # messages so ResultMessage can still arrive and set IDLE.
                    logger.exception(
                        "_process_message error for %s (msg kind %s): %s",
                        session_id, message.kind.value, pm_err
                    )

            # Post-turn compact drain: pick up compact_boundary/init buffered
            # after ResultMessage so auto-compaction shows "Compacting…" status
            # rather than silently hanging IDLE for 1-2 minutes.
            if result_handled and info._awaiting_compact_drain \
                    and info.task is asyncio.current_task():
                await self._post_turn_compact_drain(session_id, info)
                info._awaiting_compact_drain = False
                # Emit IDLE if drain didn't already transition state (no compaction
                # found, or compaction already completed inside the drain).
                if info.task is asyncio.current_task() \
                        and info.state == SessionState.IDLE:
                    self._emit_state(info)

            # Safety net: if the stream ended without a ResultMessage,
            # force IDLE so the session isn't stuck forever.  Skip if we
            # got a ResultMessage — the drain above handled IDLE transition.
            # Also skip if superseded.
            if not got_result and info.state == SessionState.WORKING \
                    and info.task is asyncio.current_task():
                logger.warning("_send_query for %s: stream ended without "
                               "ResultMessage, forcing IDLE", session_id)
                info._awaiting_compact_drain = False
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
                        # Exponential backoff: 2s, 4s, 8s between retries
                        backoff = 2 ** heal_count
                        logger.info(
                            "Self-healing (query): reconnecting %s after %d stream errors "
                            "(backoff %ds)",
                            session_id, heal_count, backoff,
                        )
                        entry = LogEntry(kind="system", text="Reconnecting session...")
                        with info._lock:
                            info.entries.append(entry)
                            entry_index = len(info.entries) - 1
                        self._emit_entry(session_id, entry, entry_index)
                        await asyncio.sleep(backoff)

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
                    deny = self._sdk.make_permission_result_deny(
                        message="Interrupted by user", interrupt=True
                    )
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
                    await self._sdk.interrupt(info.client)
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

    @staticmethod
    def _kill_process_tree(pid: int) -> None:
        """Kill a process and all its children.  Cross-platform.

        On Windows, ``taskkill /T`` only works while the parent is alive.
        If the parent already died, we fall back to WMI to find orphaned
        children by ParentProcessId and kill them individually.
        """
        try:
            if os.name == "nt":
                # Try taskkill /T first (works when parent is alive)
                result = _subprocess.run(
                    ["taskkill", "/F", "/T", "/PID", str(pid)],
                    stdout=_subprocess.PIPE,
                    stderr=_subprocess.PIPE,
                    timeout=10,
                    creationflags=_subprocess.CREATE_NO_WINDOW,
                )
                # If taskkill failed (parent already dead), find orphans
                # by ParentProcessId via WMIC and kill them with /T so
                # their own children are also cleaned up recursively.
                if result.returncode != 0:
                    try:
                        wmic = _subprocess.run(
                            ["wmic", "process", "where",
                             f"ParentProcessId={pid}", "get",
                             "ProcessId", "/value"],
                            capture_output=True, text=True, timeout=10,
                            creationflags=_subprocess.CREATE_NO_WINDOW,
                        )
                        for line in wmic.stdout.splitlines():
                            line = line.strip()
                            if line.startswith("ProcessId="):
                                child_pid = line.split("=", 1)[1].strip()
                                if child_pid.isdigit():
                                    # Use /T to kill the child AND its
                                    # descendants (e.g. node → workers)
                                    _subprocess.run(
                                        ["taskkill", "/F", "/T", "/PID",
                                         child_pid],
                                        stdout=_subprocess.DEVNULL,
                                        stderr=_subprocess.DEVNULL,
                                        timeout=5,
                                        creationflags=_subprocess.CREATE_NO_WINDOW,
                                    )
                    except Exception as wmic_err:
                        logger.debug("WMIC orphan cleanup for PID %d: %s",
                                     pid, wmic_err)
            else:
                # Send SIGTERM to the process group, then SIGKILL
                try:
                    os.killpg(os.getpgid(pid), signal.SIGTERM)
                except (ProcessLookupError, PermissionError):
                    pass
                time.sleep(0.3)
                try:
                    os.killpg(os.getpgid(pid), signal.SIGKILL)
                except (ProcessLookupError, PermissionError):
                    pass
        except Exception as e:
            logger.debug("_kill_process_tree(%d) best-effort: %s", pid, e)

    def _schedule_orphan_sweep(self) -> None:
        """Schedule the next orphan process sweep (every 60s)."""
        self._orphan_sweep_timer = threading.Timer(60.0, self._orphan_sweep)
        self._orphan_sweep_timer.daemon = True
        self._orphan_sweep_timer.start()

    def _orphan_sweep(self) -> None:
        """Check all tracked sessions for dead CLI processes with live children.

        On Windows, when claude.exe crashes or is killed externally, its child
        node.exe workers survive as orphans eating CPU and RAM.  This sweep
        detects that and kills the orphaned tree.
        """
        try:
            with self._lock:
                sessions = list(self._sessions.values())
            cleaned = 0
            for info in sessions:
                pid = info._cli_pid
                if not pid:
                    continue
                # Skip sessions that are actively working — their CLI
                # process may have been replaced by a reconnect and
                # _cli_pid may be stale.  Only clean up IDLE/STOPPED.
                if info.state in (SessionState.WORKING, SessionState.WAITING):
                    continue
                # Verify _cli_pid matches the current client's process.
                # After reconnect, _cli_pid is updated, but belt-and-
                # suspenders: if the client has a live process with a
                # DIFFERENT PID, our recorded PID is stale — skip it.
                current_pid = self._sdk.extract_process_pid(info.client)
                if current_pid and current_pid != pid:
                    # Stale PID — update and skip
                    info._cli_pid = current_pid
                    continue
                # Check if the CLI process is still alive
                try:
                    os.kill(pid, 0)  # signal 0 = existence check
                    continue  # still alive, nothing to do
                except (OSError, ProcessLookupError):
                    pass
                # CLI is dead but we still have a PID recorded — kill orphans
                logger.warning(
                    "Orphan sweep: CLI PID %d for session %s is dead — "
                    "killing child process tree",
                    pid, info.session_id,
                )
                self._kill_process_tree(pid)
                info._cli_pid = 0
                cleaned += 1
            if cleaned:
                logger.info("Orphan sweep cleaned up %d dead session(s)", cleaned)
        except Exception as e:
            logger.debug("Orphan sweep error: %s", e)
        finally:
            # Reschedule regardless of success/failure
            if getattr(self, '_started', False):
                self._schedule_orphan_sweep()

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
                    deny = self._sdk.make_permission_result_deny(
                        message="Session closed", interrupt=True
                    )
                    result_holder[0] = (deny, False, False)
                    perm_event.set()

            # Cancel the driving task if running
            if info.task and not info.task.done():
                info.task.cancel()

            # Grab the CLI subprocess PID *before* disconnect() cleans it up.
            cli_pid = self._sdk.extract_process_pid(info.client) or info._cli_pid

            # Disconnect the client (terminates the direct CLI process)
            if info.client:
                try:
                    await self._sdk.disconnect(info.client)
                except Exception:
                    pass

            # Kill the full process tree so child processes (test runners,
            # build tools, etc.) don't linger as orphans eating CPU/RAM.
            if cli_pid:
                self._kill_process_tree(cli_pid)
            info._cli_pid = 0

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
                return manager._sdk.make_permission_result_deny(
                    message="Session not found", interrupt=True
                )

            # ── Early abort: if the CLI subprocess transport is dead,
            # every tool use will fail with "Stream closed".  Instead of
            # letting the agent retry dozens of times, deny with
            # interrupt=True to end the turn immediately.  The finally
            # block in _drive_session / _send_query will reconnect and
            # retry the whole message on a fresh CLI process.
            _transport_alive = manager._sdk.is_transport_alive(info.client)
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
                return manager._sdk.make_permission_result_deny(
                    message="Transport disconnected — reconnecting",
                    interrupt=True,
                )

            # ── CRITICAL: make_permission_result_allow always includes tool_input ──
            # The CLI 2.x expects updatedInput to be the full tool input dict.
            # Sending None/null crashes the CLI sandbox validator with:
            #   "undefined is not an object (evaluating 'H.includes')"
            # See sdk_transport_adapter.py module docstring for full explanation.
            # ─────────────────────────────────────────────────────────────────

            # Auto-approve if user previously clicked "Always" for this tool
            if tool_name in info.always_allowed_tools:
                manager._log_auto_approved(
                    resolved_id, info, tool_name, tool_input, "always-allow"
                )
                return manager._sdk.make_permission_result_allow(
                    tool_input if isinstance(tool_input, dict) else {}
                )

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
                    return manager._sdk.make_permission_result_allow(
                        tool_input if isinstance(tool_input, dict) else {}
                    )

            # Server-side policy check -- resolve without browser round-trip
            if manager._should_auto_approve(tool_name, tool_input if isinstance(tool_input, dict) else {}):
                logger.debug("Auto-approved %s via server policy", tool_name)
                manager._log_auto_approved(
                    resolved_id, info, tool_name, tool_input, "server-policy"
                )
                return manager._sdk.make_permission_result_allow(
                    tool_input if isinstance(tool_input, dict) else {}
                )

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
                        return manager._sdk.make_permission_result_allow(
                            tool_input if isinstance(tool_input, dict) else {}
                        )
                    try:
                        await anyio.sleep(0.1)
                    except Exception:
                        await asyncio.sleep(0.1)

                result_tuple = perm_result_holder[0]
                if result_tuple is None:
                    result_tuple = (manager._sdk.make_permission_result_deny(message="No result"), False, False)
                # Support both 2-tuple (legacy) and 3-tuple
                if len(result_tuple) == 2:
                    permission_result, always = result_tuple
                    almost_always = False
                else:
                    permission_result, always, almost_always = result_tuple

                # Remember "Always Allow" for this tool for the rest of the session
                if isinstance(permission_result, PermissionResult) and permission_result.action == PermissionAction.ALLOW:
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
                    isinstance(permission_result, PermissionResult)
                    and permission_result.action == PermissionAction.DENY
                    and permission_result.interrupt
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
                return manager._sdk.make_permission_result_deny(
                    message="Permission request cancelled", interrupt=True
                )

        return can_use_tool

    # ------------------------------------------------------------------
    # Message processing
    # ------------------------------------------------------------------

    async def _process_message(self, session_id: str, message: VibeNodeMessage) -> None:
        """Convert a VibeNodeMessage into log entries and emit them.

        All SDK-specific isinstance() checks have been replaced with
        message.kind and block dict lookups.  The normalization from raw
        SDK types to VibeNodeMessage happens in AgentSDK.receive_response().
        """
        with self._lock:
            info = self._sessions.get(session_id)
        if not info:
            return

        if message.kind == MessageKind.ASSISTANT:
            # Don't clear compacting substatus here — only RESULT or
            # init SYSTEM should clear it. ASSISTANT can arrive
            # mid-compact (e.g. partial streaming) and clearing here causes
            # the UI to flash back to "Working" during compaction.
            info._turn_content_started = True
            for block in message.blocks:
                bk = block.get("kind", "")

                if bk == BlockKind.TEXT.value:
                    entry = LogEntry(kind="asst", text=(block.get("text", "") or "")[:50000])
                    with info._lock:
                        info.entries.append(entry)
                        entry_index = len(info.entries) - 1
                    self._emit_entry(session_id, entry, entry_index)

                elif bk == BlockKind.TOOL_USE.value:
                    inp = block.get("input", {})
                    if not isinstance(inp, dict):
                        inp = {}
                    desc = self._extract_tool_desc(inp)
                    entry = LogEntry(
                        kind="tool_use",
                        name=block.get("name", "") or "",
                        desc=desc,
                        id=block.get("id", "") or "",
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

                elif bk == BlockKind.THINKING.value:
                    # Skip thinking blocks -- they're internal reasoning
                    pass

        elif message.kind == MessageKind.USER:
            # Sub-agent detection: VibeNodeMessage.is_sub_agent is set by
            # the normalization layer based on parent_tool_use_id.
            is_sub_agent = message.is_sub_agent

            # Blocks are already normalized to dicts by receive_response().
            for block in message.blocks:
                bk = block.get("kind", "")

                if bk == BlockKind.TOOL_RESULT.value:
                    rt = (block.get("text", "") or "")[:20000]
                    is_err = bool(block.get("is_error", False))

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
                            text="Stream error detected — will auto-reconnect and retry",
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
                        text=rt,
                        tool_use_id=block.get("tool_use_id", "") or "",
                        is_error=is_err,
                    )
                    with info._lock:
                        info.entries.append(entry)
                        entry_index = len(info.entries) - 1
                    self._emit_entry(session_id, entry, entry_index)

                elif bk == BlockKind.TEXT.value:
                    user_text = (block.get("text", "") or "")[:20000]

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

        elif message.kind == MessageKind.SYSTEM:
            subtype = message.subtype or ""
            data = message.data or {}
            logger.info("SystemMessage subtype=%s keys=%s", subtype, list(data.keys())[:10])

            # Detect compaction events — CLI sends "compact_boundary" subtype
            if subtype == 'compact_boundary':
                # Guard against stale SDK-buffered compact_boundary messages.
                # The SDK delivers compact_boundary AFTER ResultMessage, so it
                # stays in the internal MemoryObjectStream buffer and gets
                # picked up at the START of the next turn before any content.
                # A legitimate compact_boundary always arrives either:
                #   (a) after some assistant content (auto-compact mid-task), or
                #   (b) at the start of a /compact turn (substatus already set).
                # If neither is true, this is a stale leftover — discard it.
                if not info._turn_content_started and info.substatus != 'compacting':
                    logger.info("Discarding stale compact_boundary for %s (buffered from prior turn)", session_id)
                    return
                compact_meta = data.get('compactMetadata', {})
                pre_tokens = compact_meta.get('preTokens', 0)
                trigger = compact_meta.get('trigger', 'auto')
                logger.info("Compact boundary: trigger=%s preTokens=%d", trigger, pre_tokens)

                # Set WORKING so _emit_state won't auto-clear the substatus
                # (the substatus-clear guard fires on IDLE/STOPPED only).
                # This covers both mid-turn and post-turn (deferred IDLE) cases.
                info.state = SessionState.WORKING
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

            elif subtype == 'init':
                # Record the resolved model ID so /api/models always knows
                # which models this installation has actually used.
                resolved_model = data.get("model", "")
                if resolved_model and resolved_model.startswith("claude-"):
                    if info.model != resolved_model:
                        info.model = resolved_model
                    try:
                        from app.routes.live_api import record_confirmed_model
                        record_confirmed_model(resolved_model)
                    except Exception:
                        pass

                # End of compaction (or session re-init) — always clear substatus.
                # Only add the "Context compacted" log entry if we were actually
                # compacting; avoids spurious entries on a plain session restart.
                was_compacting = info.substatus == 'compacting'
                info.substatus = ""
                # Restore IDLE only if we're in post-turn context
                # (_awaiting_compact_drain=True means RESULT already came and
                # we're draining the buffer — safe to go IDLE now).
                # Mid-turn compaction must NOT set IDLE here — the session is
                # still generating a response and will reach RESULT normally.
                if was_compacting and info._awaiting_compact_drain:
                    info.state = SessionState.IDLE
                self._emit_state(info)
                if was_compacting:
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

        elif message.kind == MessageKind.RESULT:
            # Clear substatus on result (compaction is done if it was in progress)
            if info.substatus:
                info.substatus = ""

            info.cost_usd = message.cost_usd or 0.0

            # Extract token usage from ResultMessage.
            # ResultMessage.usage is cumulative across the session — if we have
            # per-call data from a message_start StreamEvent, keep it.
            raw_usage = message.usage
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
            duration_ms = message.duration_ms or 0
            num_turns = message.num_turns or 0
            if duration_ms or num_turns:
                info.usage['duration_ms'] = duration_ms
                info.usage['num_turns'] = num_turns

            is_error = message.is_error
            if is_error:
                info.error = "Session ended with error"
                entry = LogEntry(kind="system", text="Session ended with error", is_error=True)
                with info._lock:
                    info.entries.append(entry)
                    entry_index = len(info.entries) - 1
                self._emit_entry(session_id, entry, entry_index)

            # Remap session ID if the SDK assigned a different one
            result_session_id = message.session_id
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
                self._mq.remap_session_id(session_id, result_session_id)

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
            # Defer the IDLE emit: compact_boundary may be buffered immediately
            # after this ResultMessage (auto-compaction post-turn notification).
            # _post_turn_compact_drain() in _send_query/_drive_session will pick
            # it up and emit WORKING+compacting instead; only if no compact_boundary
            # is found does it fall through to emit IDLE normally.
            info._awaiting_compact_drain = True

        elif message.kind == MessageKind.STREAM_EVENT:
            # Forward raw streaming events for partial message display
            event_data = message.data or {}

            # Extract per-call context usage from message_start events.
            # This is the AUTHORITATIVE context window size — unlike
            # ResultMessage.usage which is cumulative across the session.
            # NOTE: event is a STRING like "message_start", while
            # data is the dict payload with the actual content.
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
        if "description" in inp and "prompt" in inp:
            # Agent tool — use the short description field
            return str(inp["description"])[:200]
        elif "command" in inp:
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
    _PROFILE_PIPELINE = True

    # PERF-CRITICAL: git ls-files cache TTL — do NOT reduce below 120s. See CLAUDE.md #9.
    # TTL for cached git ls-files results (seconds).  File additions during
    # a turn are tracked via Edit/Write tool events, so this cache only needs
    # refreshing to catch external changes.  180s keeps the subprocess from
    # re-running on every follow-up message in a typical conversation.
    _GIT_LS_FILES_CACHE_TTL = 180

    # How many turns before forcing a full mtime rescan (0 = never force)
    _MTIME_FULL_RESCAN_INTERVAL = 10

    @staticmethod
    def _is_file_tracking_enabled() -> bool:
        """Check kanban_config.json for the file_tracking_enabled preference.

        Defaults to True if the key is missing or the config file can't be read.
        """
        try:
            cfg_path = Path(__file__).resolve().parents[1] / "kanban_config.json"
            if cfg_path.exists():
                cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
                return cfg.get("file_tracking_enabled", True)
        except Exception:
            pass
        return True

    def _git_ls_files(self, cwd_path: Path, info: "SessionInfo | None" = None) -> list:
        """Use `git ls-files` to get tracked files, respecting .gitignore.

        When *info* is provided, caches the result on the SessionInfo so
        subsequent calls within the TTL window skip the subprocess entirely.

        Returns a list of absolute Path objects, or None if git is unavailable
        or the directory is not a git repo.
        """
        # ── Check per-session cache ──
        if info is not None:
            now = time.time()
            if (info._cached_git_files
                    and now - info._cached_git_files_ts < self._GIT_LS_FILES_CACHE_TTL):
                return info._cached_git_files

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

            # ── Store in per-session cache ──
            if info is not None:
                info._cached_git_files = paths
                info._cached_git_files_ts = time.time()

            return paths
        except Exception:
            return None

    def _record_pre_turn_mtimes(self, info: SessionInfo) -> None:
        """Snapshot mtimes of source files in the working directory.

        Only used as a fallback when the streaming message handler doesn't
        see direct Edit/Write tool uses (e.g. Agent sub-agent edits).
        Uses `git ls-files` when available (fast, respects .gitignore),
        falls back to os.walk with directory pruning.

        **Optimisation:** On follow-up turns, if ``_post_turn_mtimes`` was
        populated by the previous turn's ``_detect_changed_files``, we carry
        it forward directly instead of re-walking + re-stat-ing every file.
        A full rescan is forced every ``_MTIME_FULL_RESCAN_INTERVAL`` turns
        so newly-added files are eventually picked up.
        """
        if not self._is_file_tracking_enabled():
            info._pre_turn_mtimes = {}
            return

        cwd = info.cwd
        if not cwd:
            return
        cwd_path = Path(cwd)
        if not cwd_path.is_dir():
            return

        # PERF-CRITICAL: Mtime carry-forward avoids full git ls-files + stat every turn. See CLAUDE.md #4.
        #
        # LESSON LEARNED (2026-04-12): Every follow-up turn used to do a full
        # git ls-files + stat() on every source file in the project.  With a
        # typical project of 200+ files, this took 60-80ms per turn.  The
        # carry-forward pattern reuses the mtime snapshot from the previous
        # turn's post-turn _detect_changed_files (which already scanned
        # everything).  The chain: post-turn populates _post_turn_mtimes →
        # next pre-turn carries it to _pre_turn_mtimes → next post-turn
        # compares against it.  A full rescan is forced every
        # _MTIME_FULL_RESCAN_INTERVAL turns to pick up newly added files.
        #
        # ── Fast path: carry forward from previous turn ──
        info._mtime_turn_count += 1
        force_rescan = (self._MTIME_FULL_RESCAN_INTERVAL > 0
                        and info._mtime_turn_count % self._MTIME_FULL_RESCAN_INTERVAL == 0)

        if info._post_turn_mtimes and not force_rescan:
            info._pre_turn_mtimes = info._post_turn_mtimes
            info._post_turn_mtimes = {}
            logger.debug("_record_pre_turn_mtimes: carried forward %d files (turn %d)",
                         len(info._pre_turn_mtimes), info._mtime_turn_count)
            return

        # ── Full rescan ──
        mtimes = {}
        try:
            # Fast path: use git ls-files (respects .gitignore)
            git_files = self._git_ls_files(cwd_path, info)
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
        info._post_turn_mtimes = {}
        logger.debug("_record_pre_turn_mtimes: full rescan %d files in %s (turn %d)",
                     len(mtimes), cwd, info._mtime_turn_count)

    def _detect_changed_files(self, info: SessionInfo) -> set:
        """Compare current file mtimes against the pre-turn snapshot.

        Returns absolute paths of files that were created or modified
        since _record_pre_turn_mtimes was called.
        Uses `git ls-files` when available, falls back to os.walk with pruning.

        Side-effect: populates ``info._post_turn_mtimes`` with the fresh
        mtime dict so the next turn can carry it forward without re-scanning.
        """
        if not self._is_file_tracking_enabled():
            return set()

        cwd = info.cwd
        if not cwd:
            return set()
        cwd_path = Path(cwd)
        if not cwd_path.is_dir():
            return set()

        pre = info._pre_turn_mtimes
        changed = set()
        post_mtimes = {}
        try:
            # Fast path: use git ls-files (cached per-session)
            git_files = self._git_ls_files(cwd_path, info)
            if git_files is not None:
                for f in git_files:
                    if f.suffix.lower() not in self._SOURCE_EXTS:
                        continue
                    fpath = str(f)
                    try:
                        current_mtime = f.stat().st_mtime
                    except OSError:
                        continue
                    post_mtimes[fpath] = current_mtime
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
                        post_mtimes[fpath] = current_mtime
                        if fpath not in pre or pre[fpath] != current_mtime:
                            changed.add(fpath)
        except Exception as e:
            logger.warning("_detect_changed_files failed: %s", e)

        # Save for carry-forward on next turn
        info._post_turn_mtimes = post_mtimes
        return changed

    def _prepopulate_tracked_files(self, info: SessionInfo) -> None:
        """Scan the session storage for tracked files from two sources:

        1. Past Edit/Write/MultiEdit/NotebookEdit tool_use blocks
        2. Existing file-history-snapshot entries (catches files tracked
           by previous daemon runs or the CLI itself)

        This ensures snapshots work after daemon restart or for resumed
        sessions.

        Only runs ONCE per session — after the initial scan, real-time
        tracking in _process_message keeps tracked_files up to date.
        Re-parsing a 38MB JSONL on every follow-up message was blocking
        the asyncio event loop and starving all other sessions.

        Delegates the actual JSONL scanning to self._store.read_tracked_files().
        """
        if info._tracked_files_populated:
            return
        try:
            found, max_version, last_user_uuid, last_asst_uuid = \
                self._store.read_tracked_files(info.session_id, cwd=info.cwd or "")

            # Cache user/assistant UUIDs so _write_file_snapshot
            # doesn't have to re-parse the entire JSONL every turn.
            if last_user_uuid:
                info._last_user_uuid = last_user_uuid
            if last_asst_uuid:
                info._last_asst_uuid = last_asst_uuid

            if found:
                info.tracked_files.update(found)
                # Restore version counters so new backups don't collide
                for fp, v in max_version.items():
                    if v > info.file_versions.get(fp, 0):
                        info.file_versions[fp] = v
                logger.info(
                    "_prepopulate_tracked_files(%s): found %d files from store",
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
        if not self._is_file_tracking_enabled():
            return
        session_id = self._resolve_id(session_id)
        with self._lock:
            info = self._sessions.get(session_id)
        if not info:
            logger.info("_write_file_snapshot(%s): skipped (no session info)", session_id)
            return

        # ── Filesystem fallback for change detection ──
        #
        # PERF-CRITICAL: tracked_files snowball prevention — fs_changed are snapshot extras only. See CLAUDE.md #7.
        # PERF-CRITICAL: is_post_turn guard — skips _detect_changed_files on pre-turn. See CLAUDE.md #1.
        #
        # LESSON LEARNED (2026-04-12 performance overhaul):
        #
        # Problem 1 — tracked_files snowball:
        #   We used to add fs_changed files to info.tracked_files permanently.
        #   When test suites or Agent sub-agents touched many project files,
        #   tracked_files grew to 1,400+ entries.  Every subsequent turn then
        #   read, hashed, and backed up all 1,400 files — turns took 20-55s.
        #   Fix: fs_changed is used for the CURRENT snapshot only (fs_snapshot_extras),
        #   never added to tracked_files.  tracked_files only grows via direct
        #   Edit/Write tool uses seen in the streaming message handler.
        #
        # Problem 2 — pre-turn filesystem scan was waste:
        #   _detect_changed_files runs git ls-files + stat() + file read + MD5
        #   on ~200 files to find what changed since last turn.  On pre-turn,
        #   _turn_had_direct_edit is ALWAYS False (the turn hasn't started yet),
        #   so the fallback triggered every time — scanning 199 files for 2-138ms.
        #   But the pre-turn snapshot only needs files THIS session edited
        #   (tracked_files), not changes from other sessions.  Other sessions'
        #   edits are noise — this session didn't cause them, Rewind doesn't
        #   need to undo them.  Fix: guard with is_post_turn.  The filesystem
        #   fallback now only runs post-turn, where it catches Agent/Bash edits
        #   that bypassed the Edit/Write tool tracking.
        fs_snapshot_extras = set()
        if is_post_turn and not info._turn_had_direct_edit:
            fs_changed = self._detect_changed_files(info)
            if fs_changed:
                fs_snapshot_extras = fs_changed
                logger.info("_write_file_snapshot(%s): filesystem fallback detected %d changed files (snapshot-only)",
                            session_id, len(fs_changed))

        # Combine direct-edit tracked files with filesystem-detected extras
        all_snapshot_files = info.tracked_files | fs_snapshot_extras
        if not all_snapshot_files:
            logger.info("_write_file_snapshot(%s): skipped (no tracked files)", session_id)
            return

        try:
            sid = info.session_id
            history_dir = Path.home() / ".claude" / "file-history" / sid
            history_dir.mkdir(parents=True, exist_ok=True)

            tracked_backups = {}
            for fpath in list(all_snapshot_files):
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

            # Read UUIDs from the tail of the session storage.
            # Delegated to ChatStore — reads only the last 64KB for performance.
            last_user_uuid, last_asst_uuid = self._store.read_tail_uuids(
                info.session_id, cwd=info.cwd or ""
            )

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

            # Append snapshot to session storage via ChatStore
            self._store.write_snapshot(
                info.session_id, snapshot_entry, cwd=info.cwd or ""
            )

            logger.info(
                "Wrote file-history-snapshot for %s (update=%s, %d files, outer=%s inner=%s)",
                sid, is_update,
                sum(1 for v in tracked_backups.values()
                    if isinstance(v, dict) and v.get("backupFileName")),
                outer_mid[:12], inner_mid[:12],
            )
        except Exception as e:
            logger.warning("Failed to write file snapshot for %s: %s", session_id, e)

    # ------------------------------------------------------------------
    # Registry methods — thin wrappers delegating to SessionRegistry
    # ------------------------------------------------------------------

    def _save_registry_now(self) -> None:
        """Prepare a snapshot of active sessions and save via SessionRegistry."""
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
        self._reg.save_registry_now(sessions_data)

    def _schedule_registry_save(self) -> None:
        """Debounced save — delegates to SessionRegistry."""
        self._reg.schedule_registry_save(self._save_registry_now)

    # ------------------------------------------------------------------
    # WebSocket emission helpers
    # ------------------------------------------------------------------

    async def _post_turn_compact_drain(self, session_id: str, info: SessionInfo) -> None:
        """Drain the SDK buffer after RESULT to detect auto-compaction.

        The SDK sends compact_boundary AFTER ResultMessage as a post-turn
        notification.  The main receive_response() loop terminates at RESULT,
        so compact_boundary (and init) are left in the SDK's MemoryObjectStream
        buffer.  This method peeks at the buffer for up to 100 ms — fast enough
        to be imperceptible on normal turns, but enough to catch a buffered
        compact_boundary.  If one is found, we wait up to 5 minutes for init
        so the session shows "Compacting…" for the true compaction duration.
        """
        compact_seen = False

        # Phase 1: short peek for compact_boundary (already buffered if present)
        try:
            async def _peek():
                nonlocal compact_seen
                async for msg in self._sdk.receive_response(info.client):
                    if info.task is not asyncio.current_task():
                        return
                    await self._process_message(session_id, msg)
                    if msg.kind == MessageKind.SYSTEM:
                        sub = msg.subtype or ''
                        if sub == 'compact_boundary':
                            compact_seen = True
                            return  # Confirmed — move to Phase 2
                        elif sub == 'init':
                            return  # Already done (fast compaction)
                        # Other system messages (e.g. turn_duration) — keep draining
                    else:
                        # Non-system in post-turn buffer — unexpected, stop
                        logger.warning("Unexpected post-turn msg kind=%s for %s",
                                       msg.kind.value, session_id)
                        return
            await asyncio.wait_for(_peek(), timeout=0.1)
        except asyncio.TimeoutError:
            pass  # Normal — no buffered messages means no compaction
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.warning("_post_turn_compact_drain peek error for %s: %s", session_id, e)

        if not compact_seen or info.task is not asyncio.current_task():
            return

        # Phase 2: compact_boundary confirmed — wait for init (actual compaction)
        logger.info("Auto-compaction detected for %s — waiting for init", session_id)
        try:
            async def _wait_init():
                async for msg in self._sdk.receive_response(info.client):
                    if info.task is not asyncio.current_task():
                        return
                    await self._process_message(session_id, msg)
                    if msg.kind == MessageKind.SYSTEM and (msg.subtype or '') == 'init':
                        return  # Compaction complete
                    elif msg.kind == MessageKind.RESULT:
                        return  # Unexpected second RESULT — stop
                    elif msg.kind != MessageKind.SYSTEM:
                        logger.warning("Unexpected post-compact msg kind=%s for %s",
                                       msg.kind.value, session_id)
                        return
            await asyncio.wait_for(_wait_init(), timeout=300.0)
        except asyncio.TimeoutError:
            logger.warning("Auto-compact timed out waiting for init on %s (5 min)", session_id)
            if info.task is asyncio.current_task() and info.substatus:
                info.substatus = ""
                self._emit_state(info)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.warning("_post_turn_compact_drain wait-init error for %s: %s", session_id, e)

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
        # Auto-clear stale substatus when session is no longer actively working.
        # This ensures safety-net IDLE transitions don't carry "compacting" forward
        # into the next turn's WORKING state emission.
        if info.state in (SessionState.IDLE, SessionState.STOPPED) and info.substatus:
            info.substatus = ""
        if self._push_callback:
            data = info.to_state_dict()
            # Include queue data from server-side store
            q = self._mq.get_queue_data(info.session_id)
            if q:
                data["queue"] = q
            try:
                self._push_callback('session_state', data)
            except Exception as cb_err:
                logger.error("_emit_state push_callback failed for %s (state=%s): %s",
                             info.session_id, info.state, cb_err)
        # Keep the persistent registry up to date
        self._schedule_registry_save()

        # ── Memory management: trim in-memory entries for idle sessions ──
        # The JSONL file is the source of truth; the web frontend reads it
        # directly (with caching).  The daemon's in-memory entries list is
        # only needed during active streaming for real-time updates.  Once
        # idle, keep only the last N entries so long-running sessions don't
        # consume hundreds of MB of RAM.
        _ENTRY_TRIM_THRESHOLD = 500   # start trimming above this
        _ENTRY_KEEP_AFTER_TRIM = 200  # keep this many after trimming
        if info.state in (SessionState.IDLE, SessionState.STOPPED):
            with info._lock:
                if len(info.entries) > _ENTRY_TRIM_THRESHOLD:
                    trimmed = len(info.entries) - _ENTRY_KEEP_AFTER_TRIM
                    info.entries = info.entries[-_ENTRY_KEEP_AFTER_TRIM:]
                    logger.info("Trimmed %d in-memory entries for %s (kept last %d)",
                                trimmed, info.session_id[:12], _ENTRY_KEEP_AFTER_TRIM)
            # Also release large per-turn data structures.  These get
            # repopulated on the next turn start; no need to hold 1400+
            # file paths in RAM while the session is idle.
            if info._pre_turn_mtimes and len(info._pre_turn_mtimes) > 50:
                info._pre_turn_mtimes = {}
            if info._post_turn_mtimes and len(info._post_turn_mtimes) > 50:
                info._post_turn_mtimes = {}
            if info._cached_git_files and len(info._cached_git_files) > 100:
                info._cached_git_files = []
                info._cached_git_files_ts = 0.0

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
                        q = self._mq.get_queue_data(sid)
                        if q:
                            data["queue"] = q
                        try:
                            self._push_callback('session_state', data)
                        except Exception:
                            pass
            t = threading.Timer(3.0, _deferred_idle_reemit)
            t.daemon = True
            t.start()

    def _log_auto_approved(self, session_id: str, info, tool_name: str,
                           tool_input, policy: str) -> None:
        """Log auto-approved permission — delegates to PermissionManager."""
        self._pm.log_auto_approved(session_id, info, tool_name, tool_input, policy)

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
