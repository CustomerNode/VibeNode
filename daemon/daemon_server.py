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

DAEMON_PORT = 5051
PID_FILE = Path.home() / ".claude" / "gui_daemon.pid"


class SessionDaemon:
    """Long-lived process that manages Claude Code SDK sessions via TCP IPC."""

    def __init__(self, port=DAEMON_PORT):
        self.port = port
        self.session_manager = SessionManager()
        self._server_socket = None
        self._client_socket = None
        self._client_lock = threading.Lock()
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
        self._server_socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._server_socket.bind(("127.0.0.1", self.port))
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
            sock.sendall(msg.encode("utf-8"))
        except Exception:
            with self._client_lock:
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

        # Send initial state snapshot
        try:
            states = self.session_manager.get_all_states()
            self._push_event("state_snapshot", {"sessions": states})
        except Exception as e:
            logger.warning("Failed to send state snapshot: %s", e)

        # Read requests
        buffer = ""
        try:
            while self._running:
                try:
                    data = sock.recv(65536)
                except (ConnectionResetError, ConnectionAbortedError):
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
                        self._dispatch(sock, msg)
                    except json.JSONDecodeError:
                        logger.warning("Invalid JSON from client: %s", line[:200])
        except Exception:
            logger.debug("Client connection ended", exc_info=True)
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

        self._dispatch_sync(sock, req_id, method, params)

    def _dispatch_sync(self, sock, req_id, method, params):
        """Synchronous dispatch — returns response immediately."""
        handlers = {
            "start_session": self.session_manager.start_session,
            "send_message": self.session_manager.send_message,
            "resolve_permission": self.session_manager.resolve_permission_unified,
            "interrupt_session": self.session_manager.interrupt_session,
            "close_session": self.session_manager.close_session,
            "get_all_states": lambda **kw: self.session_manager.get_all_states(),
            "get_entries": self.session_manager.get_entries,
            "has_session": self.session_manager.has_session,
            "get_session_state": self.session_manager.get_session_state,
            "set_permission_policy": self.session_manager.set_permission_policy,
            "resolve_hook_permission": self.session_manager.resolve_hook_permission,
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
            sock.sendall(line.encode("utf-8"))
        except Exception:
            pass


def main():
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [daemon] %(levelname)s %(message)s",
        datefmt="%H:%M:%S",
    )

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
