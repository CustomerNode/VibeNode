"""The smart-termination rules as they are on disk now, for the Session Engine.

The engine runs for days and used to import ``daemon/smart_termination.py``
once.  A rule added after it started was never enforced for the sessions it
runs, and the settings-file copy of the hook (``smart_termination_hook.py``)
stands down for those sessions by design.  On 2026-10-28 the card "title"
rule was added at 09:53, three minutes after the engine started at 09:50, and
cards without a title went through all morning ("how did you sneak a long
and small title through there").

``module()`` returns ``daemon.smart_termination`` and reloads the rule files
first when any of them changed on disk.  A new version must import cleanly
in a separate interpreter before it is loaded here; one that does not is
skipped (the running rules stay) and is not retried until the files change
again.  A version is only loaded once it has been quiet for ``QUIET_S``, so a
file being written is never picked up half done.
"""

from __future__ import annotations

import importlib
import logging
import os
import subprocess
import sys
import threading
import time
from pathlib import Path

logger = logging.getLogger(__name__)

_DIR = Path(__file__).resolve().parent
# Reload order: what a module imports comes before it.
FILES = ("handoff_evidence.py", "handoff_rules.py", "smart_termination.py")
QUIET_S = 3.0
CHECK_TIMEOUT_S = 30

_lock = threading.Lock()
_loaded = None      # the signature of the files the running rules came from
_bad = None         # a signature that failed its import check


def _signature(root: Path = _DIR) -> tuple:
    out = []
    for name in FILES:
        try:
            st = os.stat(root / name)
            out.append((name, st.st_mtime_ns, st.st_size))
        except OSError:
            out.append((name, None, None))
    return tuple(out)


def _newest(sig: tuple) -> float:
    return max((m or 0) for _, m, _ in sig) / 1e9


def _imports_cleanly() -> bool:
    """Import the rules in a separate interpreter: a broken file must never
    replace working rules inside the engine."""
    try:
        r = subprocess.run([sys.executable, "-c", "import daemon.smart_termination"],
                           cwd=str(_DIR.parent), capture_output=True, text=True,
                           timeout=CHECK_TIMEOUT_S)
    except Exception as e:   # noqa: BLE001 - any failure means "do not load"
        logger.warning("Smart termination rules: import check could not run: %s", e)
        return False
    if r.returncode != 0:
        logger.error("Smart termination rules on disk do not import; keeping the running ones: %s",
                     (r.stderr or "").strip().splitlines()[-1:] or "")
        return False
    return True


def module():
    """``daemon.smart_termination``, reloaded first if its rules changed."""
    global _loaded, _bad
    from daemon import smart_termination as current
    sig = _signature()
    if sig == _loaded or sig == _bad:
        return sys.modules.get("daemon.smart_termination", current)
    if time.time() - _newest(sig) < QUIET_S:
        return current      # still being written; the next turn end picks it up
    with _lock:
        if sig == _loaded or sig == _bad:
            return sys.modules.get("daemon.smart_termination", current)
        if not _imports_cleanly():
            _bad = sig
            return current
        try:
            for name in FILES:
                mod = sys.modules.get("daemon." + name[:-3])
                if mod is not None:
                    importlib.reload(mod)
                else:
                    importlib.import_module("daemon." + name[:-3])
        except Exception:   # noqa: BLE001 - keep whatever loaded; never trap a turn
            logger.exception("Smart termination rules: reload failed")
            _bad = sig
            return sys.modules.get("daemon.smart_termination", current)
        first = _loaded is None
        _loaded = sig
        if not first:
            logger.info("Smart termination rules reloaded from disk")
        return sys.modules["daemon.smart_termination"]
