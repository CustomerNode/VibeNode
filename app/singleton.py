"""
Singleton enforcement via Windows named mutexes.

A named mutex is kernel-managed, race-free, and auto-released when the
owning process dies (even on crash). This makes it impossible for two
VibeNode web servers or two daemons to run simultaneously.

Port-awareness: when ``VIBENODE_TEST_PORT`` / ``VIBENODE_DAEMON_PORT``
are set (test installs, side-by-side debugging instances), the singleton
name must include the port so the test instance doesn't collide with
the user's main instance.  Without this every test instance trying to
spawn a daemon would fail the singleton check (because the production
daemon already holds the lock).  Reported via "Restart Server → Daemon
doesn't actually restart the daemon on Linux" — the same hardcoding
indirectly caused that whole class of failure.
"""

import os
import socket
import sys
import time

# Keep handles alive for the entire process lifetime.
# Do NOT close these — let Windows clean up on exit.
_held_mutexes: dict[str, int] = {}


def acquire_singleton(name: str) -> bool:
    """Try to acquire a system-wide named mutex. Returns True if acquired."""
    if sys.platform == "win32":
        return _acquire_win32(name)
    else:
        return _acquire_unix(name)


def _web_port() -> int:
    return (
        int(os.environ.get("VIBENODE_TEST_PORT", "0"))
        or int(os.environ.get("VIBENODE_WEB_PORT", "0"))
        or 5050
    )


def _daemon_port() -> int:
    return int(os.environ.get("VIBENODE_DAEMON_PORT", "0")) or 5051


def acquire_web_singleton() -> bool:
    return acquire_singleton(f"Global\\VibeNode_WebServer_{_web_port()}")


def port_has_listener(port: int, timeout: float = 0.6) -> bool:
    """True if something is accepting TCP connections on 127.0.0.1:<port>.

    A *connect* probe, deliberately -- not a bind probe.  A failing bind does
    not prove anyone is serving: a listener holding the port with SO_REUSEADDR,
    or leftover sockets in TIME_WAIT, both make a fresh bind fail with nothing
    alive behind it.  A successful connect is unambiguous on every platform,
    which is exactly what the caller needs to tell a live incumbent apart from
    an abandoned mutex.
    """
    probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    probe.settimeout(timeout)
    try:
        probe.connect(("127.0.0.1", port))
        return True
    except OSError:
        return False
    finally:
        try:
            probe.close()
        except Exception:
            pass


def wait_for_web_singleton(attempts: int = 20, delay: float = 0.25) -> bool:
    """Acquire the web singleton, tolerating a just-killed incumbent.

    The caller kills whatever holds the web port immediately before this runs.
    The kernel releases the dead process's mutex handle promptly but not
    instantaneously, so a single failed acquisition proves nothing -- retry
    across a short window before concluding that someone is genuinely alive.

    Returns True if the mutex was acquired.  A False return means the mutex is
    still held after the full window, which the caller must then disambiguate
    with ``port_has_listener`` -- a held mutex ALONE is not grounds to start a
    second web server.  Assuming it was ("stale mutex, start anyway") is what
    produced two live servers fighting over the daemon connection, surfacing to
    the user as a "VibeNode Engine Stopped" overlay that Restart never cleared.
    """
    if acquire_web_singleton():
        return True
    for _ in range(attempts):
        time.sleep(delay)
        if acquire_web_singleton():
            return True
    return False


def acquire_daemon_singleton() -> bool:
    return acquire_singleton(f"Global\\VibeNode_Daemon_{_daemon_port()}")


def _acquire_win32(name: str) -> bool:
    import ctypes
    from ctypes import wintypes

    kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]

    ERROR_ALREADY_EXISTS = 183

    handle = kernel32.CreateMutexW(None, True, name)
    if handle == 0:
        return False  # CreateMutexW failed entirely

    last_error = kernel32.GetLastError()
    if last_error == ERROR_ALREADY_EXISTS:
        # Another process holds it — close our duplicate handle and bail
        kernel32.CloseHandle(handle)
        return False

    # We own the mutex. Stash the handle so it's never GC'd.
    _held_mutexes[name] = handle
    return True


def _acquire_unix(name: str) -> bool:
    """Fallback for non-Windows: flock-based lock file."""
    import fcntl
    from pathlib import Path

    lock_dir = Path.home() / ".claude"
    lock_dir.mkdir(parents=True, exist_ok=True)
    safe_name = name.replace("\\", "_").replace("/", "_")
    lock_path = lock_dir / f"{safe_name}.lock"

    try:
        fh = open(lock_path, "w")
        fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
        # Keep the file handle alive (prevents GC from releasing the lock)
        _held_mutexes[name] = fh  # type: ignore[assignment]
        return True
    except (OSError, IOError):
        return False
