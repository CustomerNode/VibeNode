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


def reclaim_port(port: int, timeout: float = 25.0) -> bool:
    """Ensure nothing else holds the web port at the instant we bind it.

    The reviver (reviver.py) deliberately parks a "Start VibeNode" page on the
    web port whenever the real server is down, so a phone can bring VibeNode
    back with one tap.  It releases the port on request, synchronously, via a
    POST to its loopback control port -- without the reviver process dying.

    session_manager.py already sends that yield.  It is not sufficient on its
    own: it fires at LAUNCH, and a cold boot spends the better part of a minute
    in imports and dependency checks before reaching the bind below.  The
    reviver's own loop sees the port free during that window and re-takes it,
    so by the time we bind, the yield we asked for is long stale.

    That race used to be invisible: werkzeug sets SO_REUSEADDR, and on Windows
    that lets a second live process bind a port already in use, so the web
    server simply co-bound alongside the reviver and appeared to work.  Now
    that the bind is exclusive (which is what stops two web servers existing at
    all), losing this race is fatal -- the server exits and the user is left
    tapping Start and landing back on the Start page.

    So ask again here, at the only moment that matters, and wait for the port
    to actually come free.  Returns True if the port is clear to bind.
    """
    if not port_has_listener(port):
        return True

    control_port = int(os.environ.get("VIBENODE_REVIVER_PORT", 0) or 5052)
    deadline = time.monotonic() + timeout
    asked = 0
    while time.monotonic() < deadline:
        try:
            import urllib.request
            req = urllib.request.Request(
                "http://127.0.0.1:%d/yield" % control_port, data=b"",
                method="POST")
            urllib.request.urlopen(req, timeout=2)  # noqa: S310 (loopback only)
            asked += 1
        except Exception:
            # No reviver on the control port.  Whatever holds the web port is
            # not something we can negotiate with -- keep polling in case it is
            # a dying process whose socket has not closed yet.
            pass
        time.sleep(0.5)
        if not port_has_listener(port):
            if asked:
                print("  Reviver yielded port %d" % port, flush=True)
            return True
    return False


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
