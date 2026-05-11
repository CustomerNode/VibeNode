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


def _enable_tcp_keepalive(sock):
    """Enable SO_KEEPALIVE with aggressive Linux/macOS timing.

    Mirrors app.daemon_client._enable_tcp_keepalive — kept duplicated
    here because the daemon module must be importable without
    pulling in any Flask/app dependencies.

    Without keepalive on this side, a Linux sleep/resume cycle leaves
    the daemon-side socket blocked in recv() forever.  Even after
    the web server reconnects, the daemon is still holding the dead
    socket and refuses to give up its slot until something probes it.
    Enabling keepalive lets the daemon detect and release a dead
    peer in roughly 30 seconds.
    """
    try:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
    except OSError:
        return
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
    if keepidle is None:
        keepalive = getattr(socket, "TCP_KEEPALIVE", None)
        if keepalive is not None:
            try:
                sock.setsockopt(socket.IPPROTO_TCP, keepalive, 15)
            except OSError:
                pass


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
            # SO_REUSEADDR: let us rebind to a port whose previous owner left
            # it in TIME_WAIT.  This is the common case during ``/api/restart
            # → scope=daemon``: the old daemon was kill -9'd, the port is in
            # TIME_WAIT for ~60 seconds, and without REUSEADDR the new daemon
            # bind() fails with EADDRINUSE → daemon exits → web stays up
            # without a daemon.  That's the "restart didn't restart the
            # daemon" user-reported bug on Linux.
            self._server_socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            # SO_REUSEPORT: belt-and-suspenders.  Linux 3.9+ supports it.
            # Allows binding even when a sibling process briefly held the
            # same port (e.g. a stale daemon dying during the restart's
            # kill loop).  Wrapped because not all kernels have the
            # constant defined.
            _reuseport = getattr(socket, "SO_REUSEPORT", None)
            if _reuseport is not None:
                try:
                    self._server_socket.setsockopt(
                        socket.SOL_SOCKET, _reuseport, 1
                    )
                except OSError:
                    # Old kernel without SO_REUSEPORT support — fall back
                    # to SO_REUSEADDR alone (which is set above).
                    pass
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
                # Detect zombie peers (e.g. host sleep/resume) without
                # waiting for the kernel's 2-hour default keepalive.
                _enable_tcp_keepalive(client)
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
            "get_entry_count": lambda **kw: self.session_manager.get_entry_count(kw["session_id"]),
            "has_session": self.session_manager.has_session,
            "get_session_state": self.session_manager.get_session_state,
            "get_permission_policy": lambda **kw: self.session_manager.get_permission_policy(),
            "set_permission_policy": self.session_manager.set_permission_policy,
            "get_ui_prefs": lambda **kw: self.session_manager.get_ui_prefs(),
            "set_ui_prefs": lambda **kw: self.session_manager.set_ui_prefs(kw.get("prefs", {})),
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


def _cmdline_of(pid: int) -> str:
    """Read /proc/<pid>/cmdline.  Returns '<unknown>' if not readable."""
    try:
        with open(f"/proc/{pid}/cmdline", "rb") as fh:
            raw = fh.read().replace(b"\x00", b" ").strip()
        return raw.decode("utf-8", errors="replace") or "<empty>"
    except FileNotFoundError:
        return "<pid gone>"
    except Exception as e:
        return f"<err: {e}>"


def _proc_name_of(pid: int) -> str:
    """Read /proc/<pid>/comm.  Returns '<unknown>' if not readable."""
    try:
        with open(f"/proc/{pid}/comm") as fh:
            return fh.read().strip()
    except Exception:
        return "<unknown>"


def _ppid_of(pid: int) -> int:
    """Read PPid from /proc/<pid>/status.  Returns -1 if not readable."""
    try:
        with open(f"/proc/{pid}/status") as fh:
            for line in fh:
                if line.startswith("PPid:"):
                    return int(line.split()[1])
    except Exception:
        pass
    return -1


def _install_signal_forensics_watcher(daemon):
    """Install a thread that captures the actual sender PID of every shutdown signal.

    Why this exists: Python's standard `signal.signal()` handler runs in the
    main thread without access to `siginfo_t.si_pid`, so by the time the
    handler fires we have no way to know *who* killed us.  That's why every
    previous round of debugging the "Stop Session crashed the daemon on
    Linux" regression ended with a fix that "could not be reproduced" —
    the logs only ever said "Received signal 15", with zero information
    about the source.

    The trick: block SIGTERM / SIGINT / SIGHUP at the thread level, then
    have a dedicated thread loop on `signal.sigwaitinfo()`.  `sigwaitinfo()`
    DOES preserve `si_pid`, so when the signal fires we get a definitive
    "PID N (cmdline ...) killed us" record before exiting.

    POSIX-only.  On Windows this is a no-op — Windows uses TerminateProcess,
    not POSIX signals, and the historic regression is Linux/macOS-specific
    anyway.

    Returns True if installed, False on non-POSIX or hostile environments.
    """
    if os.name == "nt":
        return False

    # Signals we want forensic detail for.  SIGPIPE is intentionally
    # excluded — it's a normal consequence of a dead socket peer.
    watched = {signal.SIGTERM, signal.SIGINT}
    if hasattr(signal, "SIGHUP"):
        watched.add(signal.SIGHUP)

    # Block at the process level so the kernel queues the signal for
    # sigwaitinfo() to pick up, instead of running the default disposition
    # (= terminate the process before we can log anything).  Threads created
    # after this inherit the mask.
    try:
        signal.pthread_sigmask(signal.SIG_BLOCK, watched)
    except Exception as e:
        logger.warning("Could not block signals for forensics watcher: %s", e)
        return False

    def _watcher():
        while True:
            try:
                si = signal.sigwaitinfo(watched)
            except InterruptedError:
                continue
            except Exception as e:
                # If sigwaitinfo dies for some reason, fall back to the
                # default disposition so the process doesn't become
                # unkillable.  Best-effort log.
                logger.error("sigwaitinfo failed (%s) — restoring default disposition", e)
                signal.pthread_sigmask(signal.SIG_UNBLOCK, watched)
                return

            sender = getattr(si, "si_pid", -1) or -1
            sig = si.si_signo
            try:
                sig_name = signal.Signals(sig).name
            except (ValueError, AttributeError):
                sig_name = f"signal{sig}"

            logger.warning(
                "=" * 78
            )
            logger.warning(
                "DAEMON KILLED BY EXTERNAL SIGNAL — full forensic record below."
            )
            logger.warning(
                "  signal:  %s (%d)   si_code=%s   si_uid=%s",
                sig_name, sig, getattr(si, "si_code", "?"),
                getattr(si, "si_uid", "?"),
            )
            logger.warning(
                "  sender:  pid=%s  ppid=%s  name=%s",
                sender, _ppid_of(sender), _proc_name_of(sender),
            )
            logger.warning(
                "  sender cmdline: %s", _cmdline_of(sender),
            )
            logger.warning(
                "  self:    pid=%s  pgid=%s  sid=%s  ppid=%s",
                os.getpid(),
                os.getpgrp() if hasattr(os, "getpgrp") else "?",
                os.getsid(0) if hasattr(os, "getsid") else "?",
                os.getppid(),
            )

            # Interpret the sender for the user.  The most important case
            # is "killed by myself" — that means a buggy os.kill() /
            # os.killpg() call inside this very process targeted us.
            self_pid = os.getpid()
            if sender == self_pid:
                logger.warning(
                    "  DIAGNOSIS: sender == self.  The daemon killed ITSELF "
                    "via os.kill()/os.killpg() — a bug inside the daemon's "
                    "own session-cleanup code.  Inspect _kill_process_tree "
                    "and _close_session for an os.killpg() / os.kill() "
                    "call whose target pgid/pid resolved to ours."
                )
            elif sender > 0 and _proc_name_of(sender) in ("python", "python3", "python3.12"):
                logger.warning(
                    "  DIAGNOSIS: sender is a Python process.  Likely the "
                    "VibeNode web server (run.py) or a daemon-launcher "
                    "running a restart endpoint.  Check app/routes/main.py "
                    "/api/restart and /api/shutdown for whether scope="
                    "'daemon' or scope='both' was triggered."
                )
            else:
                logger.warning(
                    "  DIAGNOSIS: sender is NOT this daemon and NOT a known "
                    "VibeNode component.  External source — could be "
                    "systemd, OOM killer, the user's terminal, or a "
                    "wrapper script.  Inspect ps/journalctl for context."
                )
            logger.warning("=" * 78)

            # Hand off to the normal shutdown path.  Unblock just this
            # signal, then re-raise it to ourselves so the existing
            # signal.signal() handler (registered below) runs daemon.stop()
            # + sys.exit(0) on the main thread.
            try:
                signal.pthread_sigmask(signal.SIG_UNBLOCK, {sig})
                os.kill(self_pid, sig)
            except Exception as e:
                logger.error("Failed to forward signal to main thread: %s", e)
                # Last resort — exit directly so the daemon doesn't hang.
                daemon.stop()
                os._exit(0)
            # Re-block for any future signal.
            try:
                signal.pthread_sigmask(signal.SIG_BLOCK, {sig})
            except Exception:
                pass

    t = threading.Thread(target=_watcher, daemon=True, name="signal-forensics")
    t.start()
    return True


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
        # The forensics watcher (_install_signal_forensics_watcher) already
        # logged the full sender PID / cmdline / diagnosis before forwarding
        # the signal here.  This handler just performs the graceful stop.
        # We still emit one short line so an ungraceful exit (forensics
        # watcher misfired) is still visible in the log.
        try:
            logger.info(
                "Daemon shutdown handler running (signal=%s pid=%s).",
                sig, os.getpid(),
            )
        except Exception:
            pass
        daemon.stop()
        sys.exit(0)

    # Install the forensics watcher BEFORE registering the regular handlers.
    # The watcher blocks the signals at the process level and re-delivers
    # them to the main thread after logging — the signal.signal() handlers
    # below catch the re-delivered copy.
    _install_signal_forensics_watcher(daemon)

    signal.signal(signal.SIGINT, shutdown)
    signal.signal(signal.SIGTERM, shutdown)

    daemon.start()


if __name__ == "__main__":
    main()
