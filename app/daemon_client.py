"""
DaemonClient — drop-in replacement for SessionManager in the Web UI.

Connects to the session daemon via TCP on localhost:5051 and proxies all
SessionManager method calls over JSON-line IPC.  Push events from the
daemon are re-emitted as SocketIO events to connected browsers.
"""

import json
import logging
import queue
import socket
import threading
import time

from .config import _mark_utility, _get_utility_ids
from .platform_utils import NO_WINDOW as _NO_WINDOW
import uuid

logger = logging.getLogger(__name__)

import os as _os
DAEMON_PORT = int(_os.environ.get("VIBENODE_DAEMON_PORT", 5051))

# Conditional profiling flag — matches daemon's _PROFILE_PIPELINE pattern.
# Set to False to disable IPC round-trip timing logs.
_PROFILE_IPC = True

# ----------------------------------------------------------------------
# Sleep/resume resilience tuning
# ----------------------------------------------------------------------
# When a Linux host suspends (laptop lid close, systemd-suspend, etc.)
# the loopback TCP connection between the web server and the daemon
# enters a zombie state.  Without proactive probing, the kernel never
# discovers the connection is dead and recv() blocks forever — the
# reactive "reconnect on send/recv failure" logic never fires, so the
# whole UI hangs until the user kills and restarts the server.
#
# We defend against this with two mechanisms:
#   1. TCP keepalive (kernel-level probes) — see _enable_tcp_keepalive()
#   2. Application-level heartbeat thread — see _heartbeat_loop()
#
# Both must be present.  Keepalive alone has too long a worst-case
# detection window on some kernels; the heartbeat alone won't catch
# sendall() blocking inside a zombie socket buffer.
HEARTBEAT_INTERVAL = 20   # seconds between ping probes
HEARTBEAT_TIMEOUT = 8     # seconds to wait for a ping response

# ----------------------------------------------------------------------
# IPC protocol (see PROTOCOL_VERSION in daemon/daemon_server.py)
# ----------------------------------------------------------------------
# The engine outlives web restarts and app updates, so this proxy routinely
# talks to an engine older than itself.  On connect it reads the engine's
# ``ping`` (protocol version + method list).  A method the engine does not
# list is answered locally with an "unavailable" result instead of being sent.
# An engine that predates the handshake (its ping has no version: version 0,
# methods unknown) is still sent everything, and its "Unknown method" reply is
# translated into the same result.
EXPECTED_PROTOCOL_VERSION = 1

# Every RPC this proxy sends.  Kept equal to the ``_send_request("...")``
# literals in this file by tests/test_daemon_protocol.py, so skew logging
# compares against what the web really uses.
CLIENT_RPCS = frozenset({
    "start_session", "send_message", "resolve_permission", "interrupt_session",
    "set_session_model", "set_session_effort", "cancel_auto_retry", "retry_now",
    "close_session", "close_session_sync", "remove_session", "save_registry_now",
    "get_all_states", "get_entries", "get_entry_count", "get_entry_trim_status",
    "has_session", "get_session_state", "get_dormant_states",
    "get_permission_policy", "set_permission_policy", "get_ui_prefs",
    "set_ui_prefs", "hook_pre_tool", "queue_message",
    "get_queue", "remove_queue_item", "edit_queue_item", "clear_queue",
    "get_aliases", "get_subsession_meta", "mark_inbox_dirty",
    "orphan_children_of", "detect_rewind_orphans", "reanchor_subsession",
    "detach_subsession", "set_auto_report_on_idle", "ping",
})

_UNKNOWN_METHOD_PREFIX = "Unknown method"


def _unavailable(method, detail=""):
    """The result for a method the running engine does not have.

    ``error`` keeps the "Unknown method" wording so any caller that matched
    on it before still matches; ``live_unavailable`` is what callers should
    test.
    """
    return {
        "ok": False,
        "live_unavailable": True,
        "unavailable_method": method,
        "error": detail or (f"{_UNKNOWN_METHOD_PREFIX}: {method} (the running "
                            "Session Engine predates it)"),
    }


def _note_queue_event(event_name, data):
    try:
        from app import queue_history
        if event_name == "queue_dispatched":
            queue_history.note_dispatched(data.get("session_id", ""), data.get("text", ""))
        else:
            queue_history.note_remap(data.get("old_id", ""), data.get("new_id", ""))
    except Exception as e:
        logger.debug("queue history: %s", e)


def _remap_kanban_links(old_id, new_id):
    """Point kanban task<->session links at a session's adopted CLI id.
    Idempotent (an UPDATE), so several connected web servers may all run it.
    Never raises."""
    try:
        from .db import create_repository
        create_repository().remap_session(old_id, new_id)
    except Exception as e:
        logger.debug("Kanban link remap %s -> %s failed: %s", old_id, new_id, e)


def _as_unavailable(method, result):
    """Translate an engine's raw "Unknown method" error into ``_unavailable``.
    Idempotent; any other result passes through unchanged."""
    if (isinstance(result, dict) and result.get("ok") is False
            and not result.get("live_unavailable")
            and str(result.get("error", "")).startswith(_UNKNOWN_METHOD_PREFIX)):
        return _unavailable(method, str(result.get("error")))
    return result


def _enable_tcp_keepalive(sock):
    """Enable SO_KEEPALIVE with aggressive Linux/macOS timing.

    The Linux defaults (2-hour idle, 75-second probe window) are far
    too slow to recover the UI after the host wakes from sleep, so we
    tighten the timers to detect a dead peer in roughly 30 seconds.

    Options that don't exist on the current platform are skipped
    silently — Windows and macOS expose different (or fewer) knobs.
    """
    try:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
    except OSError:
        # Some socket types reject SO_KEEPALIVE; nothing more we can do.
        return
    # Linux: TCP_KEEPIDLE / TCP_KEEPINTVL / TCP_KEEPCNT
    keepidle = getattr(socket, "TCP_KEEPIDLE", None)
    keepintvl = getattr(socket, "TCP_KEEPINTVL", None)
    keepcnt = getattr(socket, "TCP_KEEPCNT", None)
    try:
        if keepidle is not None:
            sock.setsockopt(socket.IPPROTO_TCP, keepidle, 15)
        if keepintvl is not None:
            sock.setsockopt(socket.IPPROTO_TCP, keepintvl, 5)
        if keepcnt is not None:
            sock.setsockopt(socket.IPPROTO_TCP, keepcnt, 3)
    except OSError:
        pass
    # macOS uses TCP_KEEPALIVE for idle time (no per-probe interval/count).
    if keepidle is None:
        keepalive = getattr(socket, "TCP_KEEPALIVE", None)
        if keepalive is not None:
            try:
                sock.setsockopt(socket.IPPROTO_TCP, keepalive, 15)
            except OSError:
                pass


class DaemonClient:
    """Proxy for the SessionManager running in the daemon process."""

    def __init__(self):
        self._sock = None
        self._socketio = None
        self._app = None
        self._connected = False
        self._reader_thread = None
        self._emitter_thread = None
        self._reconnect_thread = None
        self._heartbeat_thread = None
        self._pending = {}  # req_id -> (threading.Event, result_holder)
        self._pending_lock = threading.Lock()
        self._write_lock = threading.Lock()
        self._should_run = False
        self._cached_policy = None       # last policy set by browser
        self._cached_custom_rules = {}
        # Session IDs tagged as "planner" — hidden from all UI broadcasts.
        # Tracked here so the daemon doesn't need to know about session_type.
        # Seed from persistent file so utility sessions stay hidden after restart.
        self._planner_ids = _get_utility_ids()
        # Track old→new session ID remaps so /api/sessions can resolve
        # aliased JSONL files to their canonical (SDK-assigned) IDs.
        self._id_aliases: dict[str, str] = {}
        # Queue for SocketIO emits — decouples the IPC reader from the
        # potentially-blocking socketio.emit() call so that IPC response
        # processing is never delayed by WebSocket write latency.
        self._emit_queue = queue.Queue()
        # What the connected engine speaks, from its ``ping`` (see
        # protocol_info).  None until the first successful ping.
        self._protocol = None
        self._protocol_logged = None

    def start(self, socketio, app=None) -> None:
        """Connect to daemon and start the event reader.

        Same signature as SessionManager.start() so the app factory
        doesn't need to change its calling pattern.
        """
        self._socketio = socketio
        self._app = app
        self._should_run = True
        # Connect WITHOUT resync — the reader thread must be running first
        # so that _send_request() responses can be processed.  Without this,
        # _resync_aliases() blocks for 30 seconds waiting for a response
        # that the (not-yet-started) reader thread never delivers.
        self._connect(resync=False)
        self._reader_thread = threading.Thread(
            target=self._reader_loop, daemon=True, name="daemon-reader"
        )
        self._reader_thread.start()
        # Dedicated thread for SocketIO emits so the reader loop is never
        # blocked by WebSocket write latency
        self._emitter_thread = threading.Thread(
            target=self._emitter_loop, daemon=True, name="socketio-emitter"
        )
        self._emitter_thread.start()
        # Heartbeat thread: actively probes the daemon so a zombie
        # connection (e.g. after Linux sleep/resume) is detected
        # promptly and a reconnect is triggered.  Without this, the
        # reader thread can sit blocked in recv() forever.
        self._heartbeat_thread = threading.Thread(
            target=self._heartbeat_loop, daemon=True, name="daemon-heartbeat"
        )
        self._heartbeat_thread.start()
        # Now that the reader thread is running, resync aliases/policy.
        # Do this in a background thread so start() returns immediately
        # and doesn't block Flask app creation.
        if self._connected:
            threading.Thread(
                target=self._deferred_resync, daemon=True,
                name="daemon-resync"
            ).start()

    def stop(self) -> None:
        """Disconnect from daemon."""
        self._should_run = False
        self._connected = False
        if self._sock:
            try:
                self._sock.close()
            except Exception:
                pass
            self._sock = None

    @property
    def is_connected(self):
        return self._connected

    # ------------------------------------------------------------------
    # Protocol handshake
    # ------------------------------------------------------------------

    def protocol_info(self) -> dict:
        """What the connected engine speaks.

        ``{"known": bool, "protocol_version": int, "methods": [..] | None,
        "pid": int | None, "engine_started_at": float | None}``.  ``known`` is
        False before the first ping.  An engine that predates the handshake
        reports version 0 and ``methods`` None (unknown, not empty).
        """
        p = getattr(self, "_protocol", None)
        if not p:
            return {"known": False, "protocol_version": 0, "methods": None,
                    "pid": None, "engine_started_at": None}
        methods = p.get("methods")
        return {
            "known": True,
            "protocol_version": p.get("protocol_version", 0),
            "methods": sorted(methods) if methods is not None else None,
            "pid": p.get("pid"),
            "engine_started_at": p.get("engine_started_at"),
        }

    def supports(self, method: str) -> bool:
        """True unless the engine is known not to have ``method``.

        Optimistic when the method list is unknown (no ping yet, or an engine
        that predates the handshake): the call is sent, and an "Unknown
        method" reply is translated by ``_send_request``.
        """
        p = getattr(self, "_protocol", None)
        methods = p.get("methods") if p else None
        if methods is None:
            return True
        return method in methods

    def _note_protocol(self, ping_result) -> None:
        """Record an engine ``ping`` reply; log one line when it shows skew."""
        if not (isinstance(ping_result, dict) and ping_result.get("ok") is True):
            return
        raw = ping_result.get("methods")
        methods = frozenset(m for m in raw if isinstance(m, str)) if isinstance(raw, list) else None
        try:
            version = int(ping_result.get("protocol_version", 0) or 0)
        except (TypeError, ValueError):
            version = 0
        self._protocol = {
            "protocol_version": version,
            "methods": methods,
            "pid": ping_result.get("pid"),
            "engine_started_at": ping_result.get("engine_started_at"),
        }
        # One line per distinct engine shape, not one per heartbeat.
        signature = (version, methods, ping_result.get("pid"))
        if signature == getattr(self, "_protocol_logged", None):
            return
        self._protocol_logged = signature
        missing = sorted(CLIENT_RPCS - methods) if methods is not None else None
        if version != EXPECTED_PROTOCOL_VERSION or missing:
            logger.warning(
                "Session Engine protocol skew: engine v%s (pid %s), web expects v%s; "
                "methods the web uses that the engine lacks: %s",
                version, ping_result.get("pid"), EXPECTED_PROTOCOL_VERSION,
                ", ".join(missing) if missing else
                ("unknown (engine predates the handshake)" if missing is None else "none"),
            )
        else:
            logger.info("Session Engine protocol v%s (pid %s), %d methods",
                        version, ping_result.get("pid"), len(methods or ()))

    def _refresh_protocol(self) -> None:
        """Ping the engine and record what it speaks.  Never raises."""
        try:
            self._note_protocol(self._send_request("ping", timeout=HEARTBEAT_TIMEOUT))
        except Exception as e:
            logger.debug("Protocol handshake failed: %s", e)

    # ------------------------------------------------------------------
    # Connection management
    # ------------------------------------------------------------------

    def _connect(self, resync=True):
        """TCP connect to daemon.

        Args:
            resync: If True, re-send cached aliases/policy after connecting.
                    Set to False during initial start() so the reader thread
                    can be started first (resync requires the reader thread
                    to process daemon responses).
        """
        # Close old socket to avoid CLOSE_WAIT leaks
        old = self._sock
        if old:
            try:
                old.close()
            except Exception:
                pass
            self._sock = None

        for attempt in range(50):
            try:
                sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
                # Defends against Linux sleep/resume zombie sockets — see
                # comment block at top of file.
                _enable_tcp_keepalive(sock)
                sock.connect(("127.0.0.1", DAEMON_PORT))
                self._sock = sock
                self._connected = True
                logger.info("Connected to session daemon on port %d", DAEMON_PORT)
                # A (re)connect may reach a different engine: forget what the
                # last one spoke until this one answers a ping.
                self._protocol = None
                if resync:
                    self._refresh_protocol()
                    self._resync_aliases()
                    self._resync_policy()
                return
            except ConnectionRefusedError:
                time.sleep(0.1)
            except Exception as e:
                logger.debug("Connect attempt %d failed: %s", attempt, e)
                time.sleep(0.2)
        logger.warning("Could not connect to session daemon after 50 attempts")

    def _deferred_resync(self):
        """Resync aliases and policy after reader thread is running.

        Called from a background thread during start() to avoid blocking
        Flask app creation while still ensuring the reader thread is
        available to process daemon responses.
        """
        # Brief pause to let the reader thread enter its recv() loop
        time.sleep(0.05)
        self._refresh_protocol()
        self._resync_aliases()
        self._resync_policy()

    def _resync_policy(self):
        """Fetch permission policy from daemon after connect/reconnect."""
        if self._connected:
            try:
                result = self._send_request("get_permission_policy", {})
                if isinstance(result, dict) and result.get("policy"):
                    self._cached_policy = result["policy"]
                    self._cached_custom_rules = result.get("custom_rules", {})
                    logger.info("Loaded permission policy from daemon: %s", self._cached_policy)
            except Exception as e:
                logger.warning("Failed to fetch policy from daemon: %s", e)

    def _resync_aliases(self):
        """Fetch accumulated ID aliases from daemon on connect/reconnect."""
        try:
            result = self._send_request("get_aliases")
            if isinstance(result, dict):
                self._id_aliases.update(result)
        except Exception:
            pass

    def _reconnect_loop(self):
        """Background reconnection loop. Restarts daemon if it crashed."""
        attempts = 0
        max_display = 10
        while self._should_run and not self._connected:
            time.sleep(2)
            if not self._should_run:
                break
            attempts += 1
            # Push reconnect progress to frontend
            if self._socketio and attempts <= max_display:
                try:
                    self._socketio.emit('daemon_reconnect', {
                        'status': 'connecting',
                        'attempt': attempts,
                        'message': f'Reconnecting to daemon (attempt {attempts})...',
                    })
                except Exception:
                    pass
            try:
                self._connect()
                if self._connected:
                    logger.info("Reconnected to daemon")
                    if self._socketio:
                        try:
                            self._socketio.emit('daemon_reconnect', {
                                'status': 'connected',
                                'attempt': attempts,
                                'message': 'Reconnected to daemon',
                            })
                        except Exception:
                            pass
                    break
            except Exception:
                pass
            # After 5 failed reconnect rounds (~10s), the daemon is probably dead.
            # Try to restart it.
            if attempts == 5:
                if self._socketio:
                    try:
                        self._socketio.emit('daemon_reconnect', {
                            'status': 'restarting',
                            'attempt': attempts,
                            'message': 'Daemon unresponsive — restarting it...',
                        })
                    except Exception:
                        pass
                self._restart_daemon()

    @staticmethod
    def _restart_daemon():
        """Attempt to restart the session daemon if it crashed."""
        import subprocess, sys
        from pathlib import Path
        daemon_script = Path(__file__).resolve().parent.parent / "daemon" / "daemon_server.py"
        if not daemon_script.exists():
            logger.warning("Cannot restart daemon: %s not found", daemon_script)
            return
        logger.info("Daemon appears dead — restarting it")
        try:
            # Console output only; the engine writes its own capped
            # daemon_debug.log (common/logfiles.py, same rule as run.py).
            log_file = daemon_script.parent.parent / "logs" / "daemon_console.log"
            log_file.parent.mkdir(exist_ok=True)
            fh = open(log_file, "a")
            popen_kwargs = {
                "cwd": str(daemon_script.parent.parent),
                "stdout": fh,
                "stderr": fh,
            }
            if sys.platform == "win32":
                popen_kwargs["creationflags"] = (
                    _NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP
                )
            else:
                # start_new_session=True calls setsid() so the restarted daemon
                # is immune to SIGHUP if the web server's terminal is closed.
                # Mirrors the same fix in run.py ensure_daemon().
                popen_kwargs["start_new_session"] = True
            fh_ref = fh
            try:
                subprocess.Popen([sys.executable, str(daemon_script)], **popen_kwargs)
            finally:
                fh_ref.close()  # child inherits its own fd copy
        except Exception as e:
            logger.warning("Failed to restart daemon: %s", e)

    def _start_reconnect(self):
        """Start reconnection in background if not already running."""
        if self._reconnect_thread and self._reconnect_thread.is_alive():
            return
        # Immediately tell frontend we lost the connection
        if self._socketio:
            try:
                self._socketio.emit('daemon_reconnect', {
                    'status': 'disconnected',
                    'attempt': 0,
                    'message': 'Lost connection to daemon — reconnecting...',
                })
            except Exception:
                pass
        self._reconnect_thread = threading.Thread(
            target=self._reconnect_loop, daemon=True, name="daemon-reconnect"
        )
        self._reconnect_thread.start()

    def _heartbeat_loop(self):
        """Periodically ping the daemon to detect zombie connections.

        Why this exists:
            On Linux, when the host suspends and resumes, the loopback
            TCP connection enters a half-open zombie state.  recv() in
            the reader thread blocks indefinitely because no data
            arrives, and the OS's keepalive timers may take longer
            than the user is willing to wait.  Without an active
            probe, the existing "reconnect on send/recv failure"
            logic never fires — the user sees a frozen UI and has to
            kill and restart the server (the exact symptom this fix
            addresses).

        How it works:
            Every HEARTBEAT_INTERVAL seconds, we send a ping to the
            daemon with a short HEARTBEAT_TIMEOUT.  If the ping fails
            or times out, we forcibly close the socket — that wakes
            up the reader thread's recv() with an OSError, which in
            turn sets _connected=False and triggers reconnect.
        """
        while self._should_run:
            time.sleep(HEARTBEAT_INTERVAL)
            if not self._should_run:
                break
            if not self._connected:
                # Reconnect logic owns this state; nothing to probe.
                continue
            try:
                result = self._send_request("ping", timeout=HEARTBEAT_TIMEOUT)
                ok = isinstance(result, dict) and result.get("ok") is True
                if ok:
                    self._note_protocol(result)
            except Exception as e:
                logger.debug("Heartbeat ping raised: %s", e)
                ok = False
            if not ok and self._connected and self._should_run:
                logger.warning(
                    "Daemon heartbeat failed — connection appears dead, "
                    "forcing reconnect"
                )
                self._force_disconnect()

    def _force_disconnect(self):
        """Tear down the current socket so the reader thread unblocks.

        Calling close() alone is NOT enough on Linux: a recv() that
        is already mid-call may not return until the next packet
        arrives.  shutdown(SHUT_RDWR) reliably wakes any pending
        recv() with EBADF/OSError, which is what we need for the
        reader loop's exception handler to fire and trigger
        reconnect.
        """
        self._connected = False
        sock = self._sock
        self._sock = None
        if sock is not None:
            try:
                sock.shutdown(socket.SHUT_RDWR)
            except Exception:
                # Already closed / never connected — nothing to do.
                pass
            try:
                sock.close()
            except Exception:
                pass
        if self._should_run:
            self._start_reconnect()

    # ------------------------------------------------------------------
    # IPC: send request, receive response
    # ------------------------------------------------------------------

    def _send_request(self, method, params=None, timeout=30):
        """Send a request to daemon and block until response."""
        if not self._connected:
            return {"ok": False, "error": "Not connected to session daemon",
                    "disconnected": True}
        if method != "ping" and not self.supports(method):
            return _unavailable(method)
        return _as_unavailable(method, self._send_request_raw(method, params, timeout))

    def _send_request_raw(self, method, params=None, timeout=30):
        """One request/response round trip, no protocol handling."""
        req_id = uuid.uuid4().hex[:8]
        if _PROFILE_IPC:
            _t0 = time.perf_counter()
        event = threading.Event()
        result_holder = [None]

        with self._pending_lock:
            self._pending[req_id] = (event, result_holder)

        msg = json.dumps({"req_id": req_id, "method": method, "params": params or {}}) + "\n"
        try:
            with self._write_lock:
                self._sock.sendall(msg.encode("utf-8"))
        except Exception as e:
            with self._pending_lock:
                self._pending.pop(req_id, None)
            self._connected = False
            self._start_reconnect()
            return {"ok": False, "error": f"Daemon communication error: {e}"}

        event.wait(timeout=timeout)

        with self._pending_lock:
            self._pending.pop(req_id, None)

        if result_holder[0] is None:
            if _PROFILE_IPC:
                logger.info("PROFILE ipc [%s] %s: %.3fs (TIMEOUT)", req_id, method, time.perf_counter() - _t0)
            return {"ok": False, "error": f"Daemon did not respond to {method} (timeout)"}

        if _PROFILE_IPC:
            logger.info("PROFILE ipc [%s] %s: %.3fs", req_id, method, time.perf_counter() - _t0)

        return result_holder[0]

    def _reader_loop(self):
        """Background thread: reads JSON lines from daemon socket."""
        while self._should_run:
            if not self._connected or not self._sock:
                time.sleep(0.5)
                continue

            # Snapshot the current socket so we can detect if reconnection
            # happened while we were reading (prevents stale disconnect from
            # overwriting a successful reconnect).
            current_sock = self._sock
            buffer = b""  # raw bytes buffer to avoid UTF-8 boundary splits
            try:
                while self._should_run and self._connected:
                    try:
                        data = current_sock.recv(65536)
                    except (ConnectionResetError, ConnectionAbortedError, OSError):
                        break
                    if not data:
                        break
                    buffer += data
                    # Decode only up to the last newline (complete lines).
                    # This avoids UnicodeDecodeError when a multi-byte char
                    # is split across recv() calls.
                    last_nl = buffer.rfind(b"\n")
                    if last_nl == -1:
                        continue  # no complete line yet
                    decodable = buffer[:last_nl + 1]
                    buffer = buffer[last_nl + 1:]
                    try:
                        text = decodable.decode("utf-8")
                    except UnicodeDecodeError:
                        # Corrupted data — skip this chunk
                        logger.warning("UTF-8 decode error in IPC reader, skipping chunk")
                        continue
                    for line in text.splitlines():
                        line = line.strip()
                        if not line:
                            continue
                        try:
                            msg = json.loads(line)
                        except json.JSONDecodeError:
                            continue

                        if "req_id" in msg and "event" not in msg:
                            # Response to a request
                            req_id = msg["req_id"]
                            with self._pending_lock:
                                pending = self._pending.get(req_id)
                            if pending:
                                ev, holder = pending
                                if "error" in msg:
                                    holder[0] = {"ok": False, "error": msg["error"]}
                                else:
                                    holder[0] = msg.get("result")
                                ev.set()
                        elif "event" in msg:
                            # Push event — re-emit as SocketIO
                            self._emit_socketio(msg["event"], msg.get("data", {}))
            except Exception:
                logger.debug("Reader loop error", exc_info=True)

            # Only handle disconnect if our socket is still the current one.
            # If self._sock changed, a reconnect already happened — don't
            # overwrite _connected=True with False.
            if self._sock is current_sock:
                self._connected = False
                logger.info("Lost connection to daemon")
                if self._should_run:
                    self._start_reconnect()

    def _emit_socketio(self, event_name, data):
        """Queue a daemon push event for SocketIO emission.

        Called from the reader loop — must be non-blocking so IPC response
        processing is never delayed by WebSocket write latency.
        """
        if not self._socketio:
            return
        # Track remapped utility session IDs (planner, title) so
        # get_all_states filtering works after daemon assigns a new ID.
        if isinstance(data, dict) and event_name == "session_id_remapped":
            old_id = data.get("old_id", "")
            new_id = data.get("new_id", "")
            if old_id and new_id:
                self._id_aliases[old_id] = new_id
                # Kanban task<->session links live in the web's database, so
                # the web remaps them (the engine used to open the DB itself).
                # Off the reader thread: a Supabase backend is a network call.
                threading.Thread(target=_remap_kanban_links, args=(old_id, new_id),
                                 daemon=True, name="kanban-remap").start()
            if old_id in self._planner_ids:
                self._planner_ids.add(new_id)
                _mark_utility(new_id)
        # When a utility session finishes, delete its JSONL so it never
        # shows up on page refresh.  This catches both planner and title
        # sessions regardless of ID remapping.
        if isinstance(data, dict) and event_name == "session_state":
            sid = data.get("session_id", "")
            state = data.get("state", "")
            if state in ("idle", "stopped") and sid in self._planner_ids:
                self._cleanup_utility_jsonl(sid)
        # A queued message the engine sent: remembered for the card's "Also
        # handled" line (app/queue_history.py).  Off the reader thread: it
        # writes a file.
        if isinstance(data, dict) and event_name in ("queue_dispatched", "session_id_remapped"):
            threading.Thread(target=_note_queue_event, args=(event_name, dict(data)),
                             daemon=True, name="queue-history").start()
        # Utility session events (planner, title) are allowed through to the
        # browser — the frontend's _isHiddenSession() prevents them from
        # affecting the main UI, but the planner's dedicated listeners need
        # session_entry and session_state events to function.
        self._emit_queue.put((event_name, data))

    def _emitter_loop(self):
        """Dedicated thread: drains the emit queue and sends to SocketIO.

        This decouples IPC reading from WebSocket writing so that a slow
        emit (e.g. WebSocket backpressure) never blocks the reader loop
        and delays IPC request/response processing.
        """
        while self._should_run:
            try:
                event_name, data = self._emit_queue.get(timeout=1.0)
            except queue.Empty:
                continue
            if not self._socketio:
                continue
            try:
                self._socketio.emit(event_name, data)
            except Exception as e:
                logger.warning("SocketIO emit FAILED for %s: %s", event_name, e)

    # ------------------------------------------------------------------
    # Public API — same signatures as SessionManager
    # ------------------------------------------------------------------

    def start_session(self, session_id, prompt="", cwd="", name="",
                      resume=False, model=None, system_prompt=None,
                      max_turns=None, allowed_tools=None,
                      permission_mode=None, **kwargs):
        params = {
            "session_id": session_id, "prompt": prompt, "cwd": cwd,
            "name": name, "resume": resume,
        }
        if model:
            params["model"] = model
        if system_prompt:
            params["system_prompt"] = system_prompt
        if max_turns is not None:
            params["max_turns"] = max_turns
        if allowed_tools:
            params["allowed_tools"] = allowed_tools
        if permission_mode:
            params["permission_mode"] = permission_mode
        # Pass extra CLI args (e.g. effort="low" for title generation)
        if kwargs.get("extra_args"):
            params["extra_args"] = kwargs["extra_args"]
        # Track utility sessions at proxy layer — don't pass to daemon
        # (the running daemon may not support the session_type param yet)
        if kwargs.get("session_type") in ("planner", "title"):
            self._planner_ids.add(session_id)
            _mark_utility(session_id)
            params["session_type"] = kwargs["session_type"]
        # Subsessions (spec §4.2): forward the type + parent linkage so the
        # daemon persists them on the child's SessionInfo.  Subsessions are
        # NOT utility sessions — they must stay visible in the sidebar, so we
        # do NOT add them to _planner_ids / _mark_utility.
        if kwargs.get("session_type") == "subsession":
            params["session_type"] = "subsession"
        if kwargs.get("parent_session_id"):
            params["parent_session_id"] = kwargs["parent_session_id"]
        if kwargs.get("subsession_origin_turn") is not None:
            params["subsession_origin_turn"] = kwargs["subsession_origin_turn"]
        # Explicit "model default" effort on a resume.  Sent ONLY when true: a
        # daemon started before the param existed rejects unknown keys
        # (``handler(**params)``).  That older daemon never re-pins a
        # remembered effort anyway, so dropping the key there is equivalent.
        if kwargs.get("effort_reset"):
            params["effort_reset"] = True
        result = self._send_request("start_session", params)
        if params.get("effort_reset") and isinstance(result, dict) \
                and not result.get("ok") \
                and "effort_reset" in str(result.get("error", "")):
            params.pop("effort_reset", None)
            result = self._send_request("start_session", params)
        return result

    def send_message(self, session_id, text, voice=False, _ttft_t0_ns=0):
        # TTFT: pass-through of the server-captured wall-clock T0 timestamp,
        # only when the caller supplied one (VIBENODE_TIMING_TTFT=1). The
        # param is only added to the IPC payload when nonzero, so a
        # legacy-signature daemon (no _ttft_t0_ns kwarg) is only ever asked
        # to accept it when the caller has already opted into instrumentation
        # — in which case the daemon has been restarted with the matching
        # code. See docs/plans/runs/2026-09-10-1625-ttft-instrumentation/.
        params = {"session_id": session_id, "text": text}
        if _ttft_t0_ns:
            params["_ttft_t0_ns"] = int(_ttft_t0_ns)
        return self._send_request("send_message", params)

    def resolve_permission(self, session_id, allow, always=False, almost_always=False):
        return self._send_request("resolve_permission", {
            "session_id": session_id, "allow": allow, "always": always,
            "almost_always": almost_always,
        })

    def interrupt_session(self, session_id):
        return self._send_request("interrupt_session", {
            "session_id": session_id,
        })

    def set_session_model(self, session_id, model, resume_turn=False):
        """Switch a running session's model mid-session (next turn onward).
        Returns the daemon's honest result — {"ok": True, "model": ...} only
        if the CLI confirmed the switch, else {"ok": False, "error": ...}.

        ``resume_turn`` (usage-limit CTA only) additionally re-runs the turn the
        error interrupted.  It is sent ONLY when true, for the same reason
        ``start_session`` withholds ``session_type``: a daemon started before
        the param existed rejects any unknown key outright
        (``daemon_server`` dispatches with ``handler(**params)``), and the
        plain model badge — which never asks to resume a turn — must keep
        working against a long-running daemon.  If an older daemon does reject
        the key, retry without it: switching the model is the part the user
        asked for, and the client reads ``turn_resumed`` to say honestly
        whether the turn came back with it.
        """
        params = {"session_id": session_id, "model": model}
        if resume_turn:
            params["resume_turn"] = True
        result = self._send_request("set_session_model", params)
        if resume_turn and isinstance(result, dict) and not result.get("ok") \
                and "resume_turn" in str(result.get("error", "")):
            logger.warning(
                "Daemon rejected resume_turn for set_session_model — retrying "
                "without it (model will switch, interrupted turn will not "
                "auto-resume)"
            )
            result = self._send_request("set_session_model", {
                "session_id": session_id, "model": model,
            })
        return result

    def set_session_effort(self, session_id, effort=""):
        """Change a live session's thinking level in place, mid-turn included
        (see ``SessionManager.set_session_effort`` for the result shapes).

        The Session Engine keeps running across web-server restarts and app
        updates, so this proxy often talks to a daemon started before the
        method existed.  ``_send_request`` reports any method such an engine
        lacks as ``live_unavailable`` (generically, for every method), so the
        browser relaunches the session's CLI with ``--effort`` once no turn
        is running, exactly as it did before the live change existed.
        """
        result = _as_unavailable("set_session_effort", self._send_request(
            "set_session_effort", {"session_id": session_id, "effort": effort}))
        if isinstance(result, dict) and result.get("live_unavailable"):
            return dict(result, error="The Session Engine predates live thinking changes")
        return result

    def cancel_auto_retry(self, session_id):
        """Cancel a pending API-error auto-retry countdown (Cancel button)."""
        return self._send_request("cancel_auto_retry", {
            "session_id": session_id,
        })

    def retry_now(self, session_id):
        """Fire the auto-retry / manual retry immediately (Retry-now button)."""
        return self._send_request("retry_now", {
            "session_id": session_id,
        })

    def close_session(self, session_id):
        return self._send_request("close_session", {
            "session_id": session_id,
        })

    def close_session_sync(self, session_id, timeout=5.0):
        return self._send_request("close_session_sync", {
            "session_id": session_id, "timeout": timeout,
        })

    def remove_session(self, session_id):
        return self._send_request("remove_session", {
            "session_id": session_id,
        })

    # ── Subsessions (spec §4.2/§4.3/§6) ────────────────────────────────
    # These proxy the daemon-side SessionManager subsession API across the
    # IPC boundary.  Without them the web tier (which only ever holds a
    # DaemonClient, never the real SessionManager) raises AttributeError on
    # every subsession endpoint — the create_app(testing=True) MagicMock
    # auto-fabricates them, which is exactly why the unit tests stayed green
    # while production 500'd.  tests/test_daemon_client_parity.py pins this.
    def get_subsession_meta(self, session_id):
        return self._send_request("get_subsession_meta", {
            "session_id": session_id,
        })

    def mark_inbox_dirty(self, session_id):
        return self._send_request("mark_inbox_dirty", {
            "session_id": session_id,
        })

    def orphan_children_of(self, parent_sid):
        return self._send_request("orphan_children_of", {
            "parent_sid": parent_sid,
        })

    def detect_rewind_orphans(self, parent_sid, new_line_count):
        return self._send_request("detect_rewind_orphans", {
            "parent_sid": parent_sid, "new_line_count": new_line_count,
        })

    def reanchor_subsession(self, child_sid, new_origin_turn):
        return self._send_request("reanchor_subsession", {
            "child_sid": child_sid, "new_origin_turn": new_origin_turn,
        })

    def detach_subsession(self, child_sid):
        return self._send_request("detach_subsession", {
            "child_sid": child_sid,
        })

    def set_auto_report_on_idle(self, session_id, on):
        return self._send_request("set_auto_report_on_idle", {
            "session_id": session_id, "on": on,
        })

    def _resolve_id(self, session_id):
        """Resolve a possibly-aliased session ID to its canonical form."""
        return self._id_aliases.get(session_id, session_id)

    def _cleanup_utility_jsonl(self, sid: str):
        """Delete the JSONL file for a finished utility session."""
        try:
            from .config import _sessions_dir
            sd = _sessions_dir()
            jsonl = sd / f"{sid}.jsonl"
            if jsonl.exists():
                jsonl.unlink()
                logger.debug("Cleaned up utility JSONL: %s", sid)
        except Exception as e:
            logger.debug("_cleanup_utility_jsonl(%s): %s", sid, e)

    def _save_registry_now(self):
        return self._send_request("save_registry_now")

    def get_all_states(self):
        result = self._send_request("get_all_states")
        if isinstance(result, list):
            # Convention: underscore-prefixed IDs are system/utility sessions
            return [s for s in result
                    if s.get("session_id", "") not in self._planner_ids
                    and s.get("session_type", "") not in ("planner", "title")
                    and not s.get("session_id", "").startswith("_")]
        return []

    def get_entries(self, session_id, since=0):
        result = self._send_request("get_entries", {
            "session_id": session_id, "since": since,
        })
        if isinstance(result, list):
            return result
        return []

    def get_entry_count(self, session_id):
        """Return the number of log entries without fetching them all."""
        result = self._send_request("get_entry_count", {"session_id": session_id})
        if isinstance(result, int):
            return result
        if isinstance(result, dict) and result.get("ok") is False:
            return 0
        return result if isinstance(result, int) else 0

    def get_entry_trim_status(self, session_id):
        """Return (count, trimmed_ever) for a managed session.

        Companion to get_entry_count -- one IPC roundtrip that carries both
        the O(1) entry count AND the sticky "was ever trimmed" flag.  The
        ws_events fast path uses ``trimmed`` to decide whether the daemon's
        count is an authoritative total for the on-disk JSONL.
        See docs/plans/runs/2026-09-11-1546-load-older-fix.

        Returns (0, False) on IPC failure or unmanaged session.  Defensive:
        if a rolling-upgrade window exposes an old daemon without this
        method the request will error and we return (0, False), which the
        fast path treats as "fall through to legacy" -- correct behavior.
        """
        try:
            result = self._send_request("get_entry_trim_status", {"session_id": session_id})
        except Exception:
            return (0, False)
        if isinstance(result, dict):
            if result.get("ok") is False:
                return (0, False)
            count = result.get("count", 0)
            trimmed = result.get("trimmed", False)
            if isinstance(count, int) and isinstance(trimmed, bool):
                return (count, trimmed)
        return (0, False)

    def has_session(self, session_id):
        result = self._send_request("has_session", {
            "session_id": session_id,
        })
        if isinstance(result, bool):
            return result
        return False

    def get_session_state(self, session_id):
        return self._send_request("get_session_state", {
            "session_id": session_id,
        })

    def get_dormant_states(self):
        """Fetch restart-memory states for previously-active dormant sessions.

        Returns {session_id: {"last_state", ...}}.  Returns {} against an older
        daemon that predates the method (the response is an error/ok=False dict)
        so the web layer degrades gracefully in the window between a web update
        and the next daemon (re)start — the sidebar simply shows "sleeping"
        until the daemon is restarted, exactly as before.
        """
        result = self._send_request("get_dormant_states")
        if (isinstance(result, dict)
                and "error" not in result
                and result.get("ok") is not False):
            return result
        return {}

    def get_permission_policy(self):
        """Fetch current permission policy from the daemon."""
        result = self._send_request("get_permission_policy", {})
        if isinstance(result, dict) and result.get("policy"):
            self._cached_policy = result["policy"]
            self._cached_custom_rules = result.get("custom_rules", {})
        return result

    def set_permission_policy(self, policy, custom_rules=None):
        self._cached_policy = policy
        self._cached_custom_rules = custom_rules or {}
        return self._send_request("set_permission_policy", {
            "policy": policy, "custom_rules": self._cached_custom_rules,
        })

    def get_ui_prefs(self):
        """Fetch persisted UI preferences from daemon."""
        result = self._send_request("get_ui_prefs", {})
        return result if isinstance(result, dict) else {}

    def set_ui_prefs(self, prefs):
        """Persist UI preferences via daemon."""
        return self._send_request("set_ui_prefs", {"prefs": prefs})

    def hook_pre_tool(self, tool_name, tool_input, session_id):
        """Proxy for hook_pre_tool — blocks up to 1 hour."""
        return self._send_request("hook_pre_tool", {
            "tool_name": tool_name, "tool_input": tool_input,
            "session_id": session_id,
        }, timeout=3600)

    # ------------------------------------------------------------------
    # Server-side message queue
    # ------------------------------------------------------------------

    def queue_message(self, session_id, text):
        return self._send_request("queue_message", {
            "session_id": session_id, "text": text,
        })

    def get_queue(self, session_id):
        result = self._send_request("get_queue", {"session_id": session_id})
        if isinstance(result, list):
            return result
        return []

    def remove_queue_item(self, session_id, index):
        return self._send_request("remove_queue_item", {
            "session_id": session_id, "index": index,
        })

    def edit_queue_item(self, session_id, index, text):
        return self._send_request("edit_queue_item", {
            "session_id": session_id, "index": index, "text": text,
        })

    def clear_queue(self, session_id):
        return self._send_request("clear_queue", {
            "session_id": session_id,
        })
