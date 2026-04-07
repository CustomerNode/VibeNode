"""
Session Daemon — TCP server wrapping the SessionManager.

Listens on localhost:5051 for JSON-line IPC from the Web UI.
Push events from SessionManager are forwarded to the connected client.
"""

import json
import logging
import os
import signal
import socket
import sys
import threading
import time
from pathlib import Path

logger = logging.getLogger(__name__)

# Add parent dir to path so we can import daemon.session_manager
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from daemon.session_manager import SessionManager

DAEMON_PORT = int(os.environ.get("VIBENODE_DAEMON_PORT", 5051))
PID_FILE = Path.home() / ".claude" / (f"gui_daemon_{DAEMON_PORT}.pid" if DAEMON_PORT != 5051 else "gui_daemon.pid")


class SessionDaemon:
    """Long-lived process that manages Claude Code SDK sessions via TCP IPC."""

    def __init__(self, port=DAEMON_PORT):
        self.port = port
        self.session_manager = SessionManager()
        self._server_socket = None
        self._client_socket = None
        self._client_lock = threading.Lock()
        self._write_lock = threading.Lock()   # serialize all socket writes
        self._running = False

    def start(self):
        """Start the SessionManager and TCP server. Blocks forever."""
        # Write PID file
        PID_FILE.parent.mkdir(parents=True, exist_ok=True)
        PID_FILE.write_text(str(os.getpid()))

        # Start SessionManager with our push callback
        self.session_manager.start(push_callback=self._push_event)
        self._running = True

        # Listen for Web UI connections
        self._server_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        if sys.platform == "win32":
            # SO_EXCLUSIVEADDRUSE: hard guarantee no other process can bind this port
            self._server_socket.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        else:
            self._server_socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            self._server_socket.bind(("127.0.0.1", self.port))
        except OSError as e:
            logger.error("Port %d already in use — another daemon is running? %s", self.port, e)
            print(f"ERROR: Port {self.port} already in use. Kill the existing daemon first.", flush=True)
            sys.exit(1)
        self._server_socket.listen(1)
        self._server_socket.settimeout(1.0)  # Allow periodic check for shutdown

        logger.info("Session daemon listening on port %d", self.port)
        print(f"Session daemon listening on port {self.port}", flush=True)

        while self._running:
            try:
                client, addr = self._server_socket.accept()
                client.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
                logger.info("Web UI connected from %s", addr)
                # Handle client in a thread so we can accept new connections
                # (e.g., when Web UI restarts)
                threading.Thread(
                    target=self._handle_client, args=(client,),
                    daemon=True, name="ipc-client"
                ).start()
            except socket.timeout:
                continue
            except OSError:
                if self._running:
                    logger.exception("Accept error")
                break

    def stop(self):
        """Gracefully shut down."""
        self._running = False
        self.session_manager.stop()
        if self._server_socket:
            try:
                self._server_socket.close()
            except Exception:
                pass
        # Clean up PID file
        try:
            PID_FILE.unlink(missing_ok=True)
        except Exception:
            pass
        logger.info("Session daemon stopped")

    def _push_event(self, event_name, data):
        """Called by SessionManager to push events to the Web UI."""
        with self._client_lock:
            sock = self._client_socket
        if not sock:
            return  # No client connected, discard event
        msg = json.dumps({"event": event_name, "data": data}) + "\n"
        try:
            with self._write_lock:
                sock.sendall(msg.encode("utf-8"))
        except Exception as push_err:
            logger.warning("Push event %s failed: %s", event_name, push_err)
            with self._client_lock:
                # Only clear if no new client has connected since we grabbed the ref
                if self._client_socket is sock:
                    self._client_socket = None

    def _handle_client(self, sock):
        """Handle one Web UI TCP connection."""
        # Replace any existing client
        with self._client_lock:
            old = self._client_socket
            self._client_socket = sock
        if old:
            try:
                old.close()
            except Exception:
                pass

        # Send initial state snapshot (includes all queues)
        try:
            states = self.session_manager.get_all_states()
            with self.session_manager._queue_lock:
                queues = {k: list(v) for k, v in self.session_manager._queues.items() if v}
            self._push_event("state_snapshot", {"sessions": states, "queues": queues})
        except Exception as e:
            logger.warning("Failed to send state snapshot: %s", e)

        # Read requests
        buffer = b""
        try:
            while self._running:
                try:
                    data = sock.recv(65536)
                except (ConnectionResetError, ConnectionAbortedError) as e:
                    logger.info("Client recv error: %s", e)
                    break
                if not data:
                    logger.info("Client sent EOF (empty recv)")
                    break
                buffer += data
                last_nl = buffer.rfind(b"\n")
                if last_nl == -1:
                    continue
                decodable = buffer[:last_nl + 1]
                buffer = buffer[last_nl + 1:]
                try:
                    text = decodable.decode("utf-8")
                except UnicodeDecodeError:
                    logger.warning("UTF-8 decode error in client handler, skipping chunk")
                    continue
                for line in text.splitlines():
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        msg = json.loads(line)
                        self._dispatch(sock, msg)
                    except json.JSONDecodeError:
                        logger.warning("Invalid JSON from client: %s", line[:200])
        except Exception as e:
            logger.warning("Client connection ended with exception: %s", e, exc_info=True)
        finally:
            with self._client_lock:
                if self._client_socket is sock:
                    self._client_socket = None
            try:
                sock.close()
            except Exception:
                pass
            logger.info("Web UI disconnected")

    def _dispatch(self, sock, msg):
        """Route a JSON request to the appropriate SessionManager method."""
        req_id = msg.get("req_id", "")
        method = msg.get("method", "")
        params = msg.get("params", {})

        # For hook_pre_tool, run in a separate thread since it blocks
        if method == "hook_pre_tool":
            threading.Thread(
                target=self._dispatch_blocking,
                args=(sock, req_id, method, params),
                daemon=True
            ).start()
            return

        # Run ALL dispatches in threads so one slow/blocked handler can
        # never stall the entire request pipeline.  The _write_lock in
        # _dispatch_sync already serialises socket writes, so this is safe.
        threading.Thread(
            target=self._dispatch_sync,
            args=(sock, req_id, method, params),
            daemon=True,
        ).start()

    def _dispatch_sync(self, sock, req_id, method, params):
        """Synchronous dispatch — returns response immediately."""
        handlers = {
            "start_session": self.session_manager.start_session,
            "send_message": self.session_manager.send_message,
            "resolve_permission": self.session_manager.resolve_permission_unified,
            "interrupt_session": self.session_manager.interrupt_session,
            "close_session": self.session_manager.close_session,
            "close_session_sync": self.session_manager.close_session_sync,
            "remove_session": lambda **kw: self.session_manager.remove_session(**kw) or {"ok": True},
            "save_registry_now": lambda **kw: self.session_manager._save_registry_now() or {"ok": True},
            "get_all_states": lambda **kw: self.session_manager.get_all_states(),
            "get_entries": self.session_manager.get_entries,
            "has_session": self.session_manager.has_session,
            "get_session_state": self.session_manager.get_session_state,
            "get_permission_policy": lambda **kw: self.session_manager.get_permission_policy(),
            "set_permission_policy": self.session_manager.set_permission_policy,
            "resolve_hook_permission": self.session_manager.resolve_hook_permission,
            "queue_message": self.session_manager.queue_message,
            "get_queue": lambda **kw: self.session_manager.get_queue(**kw),
            "remove_queue_item": self.session_manager.remove_queue_item,
            "edit_queue_item": self.session_manager.edit_queue_item,
            "clear_queue": self.session_manager.clear_queue,
            "get_aliases": lambda **kw: dict(self.session_manager._id_aliases),
            "ping": lambda **kw: {"ok": True, "pid": os.getpid()},
        }

        handler = handlers.get(method)
        if not handler:
            resp = {"req_id": req_id, "error": f"Unknown method: {method}"}
        else:
            try:
                result = handler(**params) if params else handler()
                resp = {"req_id": req_id, "result": result}
            except Exception as e:
                logger.exception("Dispatch error for %s", method)
                resp = {"req_id": req_id, "error": str(e)}

        line = json.dumps(resp) + "\n"
        try:
            with self._write_lock:
                sock.sendall(line.encode("utf-8"))
        except Exception:
            pass

    def _dispatch_blocking(self, sock, req_id, method, params):
        """Blocking dispatch — for long-running calls like hook_pre_tool."""
        try:
            result = self.session_manager.hook_pre_tool(**params)
            resp = {"req_id": req_id, "result": result}
        except Exception as e:
            resp = {"req_id": req_id, "error": str(e)}

        line = json.dumps(resp) + "\n"
        try:
            with self._write_lock:
                sock.sendall(line.encode("utf-8"))
        except Exception:
            pass


def main():
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [daemon] %(levelname)s %(message)s",
        datefmt="%H:%M:%S",
    )

    # Singleton gate: only one daemon allowed system-wide
    from app.singleton import acquire_daemon_singleton
    if not acquire_daemon_singleton():
        # Double-check: is the port actually in use? If not, mutex is stale.
        try:
            _chk = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            _chk.settimeout(1)
            _chk.connect(("127.0.0.1", DAEMON_PORT))
            _chk.close()
            print("Session daemon already running (mutex held). Exiting.", flush=True)
            sys.exit(0)
        except (ConnectionRefusedError, OSError):
            print("Stale daemon mutex detected (port not listening). Starting anyway.", flush=True)

    daemon = SessionDaemon()

    def shutdown(sig, frame):
        logger.info("Received signal %s, shutting down", sig)
        daemon.stop()
        sys.exit(0)

    signal.signal(signal.SIGINT, shutdown)
    signal.signal(signal.SIGTERM, shutdown)

    daemon.start()


if __name__ == "__main__":
    main()
