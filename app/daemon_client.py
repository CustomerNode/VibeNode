"""
DaemonClient — drop-in replacement for SessionManager in the Web UI.

Connects to the session daemon via TCP on localhost:5051 and proxies all
SessionManager method calls over JSON-line IPC.  Push events from the
daemon are re-emitted as SocketIO events to connected browsers.
"""

import json
import logging
import socket
import threading
import time
import uuid

logger = logging.getLogger(__name__)

DAEMON_PORT = 5051


class DaemonClient:
    """Proxy for the SessionManager running in the daemon process."""

    def __init__(self):
        self._sock = None
        self._socketio = None
        self._app = None
        self._connected = False
        self._reader_thread = None
        self._reconnect_thread = None
        self._pending = {}  # req_id -> (threading.Event, result_holder)
        self._pending_lock = threading.Lock()
        self._write_lock = threading.Lock()
        self._should_run = False

    def start(self, socketio, app=None) -> None:
        """Connect to daemon and start the event reader.

        Same signature as SessionManager.start() so the app factory
        doesn't need to change its calling pattern.
        """
        self._socketio = socketio
        self._app = app
        self._should_run = True
        self._connect()
        self._reader_thread = threading.Thread(
            target=self._reader_loop, daemon=True, name="daemon-reader"
        )
        self._reader_thread.start()

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
    # Connection management
    # ------------------------------------------------------------------

    def _connect(self):
        """TCP connect to daemon."""
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
                sock.connect(("127.0.0.1", DAEMON_PORT))
                self._sock = sock
                self._connected = True
                logger.info("Connected to session daemon on port %d", DAEMON_PORT)
                return
            except ConnectionRefusedError:
                time.sleep(0.1)
            except Exception as e:
                logger.debug("Connect attempt %d failed: %s", attempt, e)
                time.sleep(0.2)
        logger.warning("Could not connect to session daemon after 50 attempts")

    def _reconnect_loop(self):
        """Background reconnection loop. Restarts daemon if it crashed."""
        attempts = 0
        while self._should_run and not self._connected:
            time.sleep(2)
            if not self._should_run:
                break
            attempts += 1
            try:
                self._connect()
                if self._connected:
                    logger.info("Reconnected to daemon")
                    break
            except Exception:
                pass
            # After 5 failed reconnect rounds (~10s), the daemon is probably dead.
            # Try to restart it.
            if attempts == 5:
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
            creation_flags = 0
            if sys.platform == "win32":
                creation_flags = (
                    subprocess.CREATE_NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP
                )
            log_file = daemon_script.parent.parent / "daemon_debug.log"
            fh = open(log_file, "a")
            subprocess.Popen(
                [sys.executable, str(daemon_script)],
                cwd=str(daemon_script.parent.parent),
                creationflags=creation_flags,
                stdout=fh,
                stderr=fh,
            )
        except Exception as e:
            logger.warning("Failed to restart daemon: %s", e)

    def _start_reconnect(self):
        """Start reconnection in background if not already running."""
        if self._reconnect_thread and self._reconnect_thread.is_alive():
            return
        self._reconnect_thread = threading.Thread(
            target=self._reconnect_loop, daemon=True, name="daemon-reconnect"
        )
        self._reconnect_thread.start()

    # ------------------------------------------------------------------
    # IPC: send request, receive response
    # ------------------------------------------------------------------

    def _send_request(self, method, params=None, timeout=30):
        """Send a request to daemon and block until response."""
        if not self._connected:
            return {"ok": False, "error": "Not connected to session daemon"}

        req_id = uuid.uuid4().hex[:8]
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
            return {"ok": False, "error": f"Daemon did not respond to {method} (timeout)"}

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
            buffer = ""
            try:
                while self._should_run and self._connected:
                    try:
                        data = current_sock.recv(65536)
                    except (ConnectionResetError, ConnectionAbortedError, OSError):
                        break
                    if not data:
                        break
                    buffer += data.decode("utf-8")
                    while "\n" in buffer:
                        line, buffer = buffer.split("\n", 1)
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
        """Re-emit a daemon push event as a SocketIO event to browsers."""
        if not self._socketio:
            return
        try:
            self._socketio.emit(event_name, data)
        except Exception as e:
            logger.debug("SocketIO emit error: %s", e)

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
        return self._send_request("start_session", params)

    def send_message(self, session_id, text):
        return self._send_request("send_message", {
            "session_id": session_id, "text": text,
        })

    def resolve_permission(self, session_id, allow, always=False):
        return self._send_request("resolve_permission", {
            "session_id": session_id, "allow": allow, "always": always,
        })

    def interrupt_session(self, session_id):
        return self._send_request("interrupt_session", {
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

    def _save_registry_now(self):
        return self._send_request("save_registry_now")

    def get_all_states(self):
        result = self._send_request("get_all_states")
        if isinstance(result, list):
            return result
        return []

    def get_entries(self, session_id, since=0):
        result = self._send_request("get_entries", {
            "session_id": session_id, "since": since,
        })
        if isinstance(result, list):
            return result
        return []

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

    def set_permission_policy(self, policy, custom_rules=None):
        return self._send_request("set_permission_policy", {
            "policy": policy, "custom_rules": custom_rules or {},
        })

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
