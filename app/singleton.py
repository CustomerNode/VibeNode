"""
Singleton enforcement via Windows named mutexes.

A named mutex is kernel-managed, race-free, and auto-released when the
owning process dies (even on crash). This makes it impossible for two
VibeNode web servers or two daemons to run simultaneously.
"""

import sys

# Keep handles alive for the entire process lifetime.
# Do NOT close these — let Windows clean up on exit.
_held_mutexes: dict[str, int] = {}


def acquire_singleton(name: str) -> bool:
    """Try to acquire a system-wide named mutex. Returns True if acquired."""
    if sys.platform == "win32":
        return _acquire_win32(name)
    else:
        return _acquire_unix(name)


def acquire_web_singleton() -> bool:
    return acquire_singleton("Global\\VibeNode_WebServer_5050")


def acquire_daemon_singleton() -> bool:
    return acquire_singleton("Global\\VibeNode_Daemon_5051")


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
