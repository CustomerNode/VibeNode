"""
Earliest-possible boot hardening — imported FIRST by every VibeNode Python
entry point (run.py, daemon/daemon_server.py, app/boot_splash.py), before any
third-party import.

Deliberately a TOP-LEVEL module, NOT inside the ``app`` package: importing
``app`` runs ``app/__init__.py``, which imports flask_socketio → ... → aiohttp.
That is the exact chain we need to neutralize *before* it runs, so this module
must be importable without touching ``app`` at all.  Keep it stdlib-only and
import-cheap.

Two responsibilities, both about surviving a wedged environment:

1. WMI-hang immunity.  On Windows + Python 3.12, ``platform.uname()`` /
   ``platform.system()`` issue a WMI query.  aiohttp calls ``platform.system()``
   at import time.  If the Windows WMI service (Winmgmt) is wedged, that query
   never returns and boot freezes forever — historically showing the misleading
   "clearing caches" splash step (the last status written before the freeze).
   We pre-seed ``platform``'s uname cache so the call is a pure cache read that
   never touches WMI.

2. Hang autopsy.  ``arm_hang_dump(seconds)`` schedules faulthandler to dump
   every thread's stack to stderr after N seconds (entry points redirect stderr
   to their log file).  If boot ever wedges again — WMI, disk, network, a lock,
   anything — the log gets the exact stack for free, instead of a human having
   to reproduce it by hand.  Call ``disarm_hang_dump()`` once the process is
   healthy.  ``dump_stacks_now()`` dumps immediately (used by run.py's
   per-step boot watchdog when a step overruns its budget).
"""

import sys


def _preseed_platform_uname():
    """Make platform.uname()/system() WMI-free on Windows.

    No-op off Windows, or if something already populated the cache.  Never
    raises — hardening must not be able to break boot.
    """
    if sys.platform != "win32":
        return
    import os
    import platform
    if getattr(platform, "_uname_cache", None) is not None:
        return
    try:
        import socket
        # NOTE: do NOT call platform.machine() / platform.uname() here — those
        # are the very calls that would hit the wedged WMI.  Read the arch from
        # the environment instead.  release/version are left blank: they are
        # cosmetic for our callers (aiohttp only reads ``.system``).
        platform._uname_cache = platform.uname_result(
            "Windows",
            socket.gethostname(),
            "",
            "",
            os.environ.get("PROCESSOR_ARCHITECTURE", "AMD64"),
        )
    except Exception:
        # Worst case we fall back to the original (possibly slow) behavior.
        pass


def arm_hang_dump(seconds, label="boot"):
    """Dump all thread stacks to stderr if not disarmed within ``seconds``.

    Fires once (``repeat=False``).  Cheap, stdlib-only, and safe to call when
    faulthandler is unavailable (no-op).
    """
    try:
        import faulthandler
        # Print a marker so a later dump in the log is easy to attribute.
        print("[_early_boot] hang-dump armed (%ds, %s)" % (seconds, label),
              file=sys.stderr, flush=True)
        faulthandler.dump_traceback_later(seconds, repeat=False)
    except Exception:
        pass


def disarm_hang_dump():
    """Cancel a pending arm_hang_dump()."""
    try:
        import faulthandler
        faulthandler.cancel_dump_traceback_later()
    except Exception:
        pass


def dump_stacks_now():
    """Dump every thread's stack to stderr immediately. Best-effort."""
    try:
        import faulthandler
        faulthandler.dump_traceback()
    except Exception:
        pass


# Apply the WMI immunity at import time — this is the whole point of importing
# this module first.
_preseed_platform_uname()
