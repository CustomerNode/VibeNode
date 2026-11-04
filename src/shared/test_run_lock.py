"""One parallel test run on this machine at a time.

Measured: the full publish run took 394s on a quiet machine and
557s, 1,447s and ~12 min when other sessions ran their own parallel suites at
the same time.  Each run starts 8 workers and up to 4 browsers on 12 cores, so
two runs slow each other several times over (one browser test file: 22s alone,
825s inside an overlapping run).  Waiting for the other run is faster for both.

``tests/conftest.py`` takes this lock (through ``tests/_run_lock.py``) in the xdist controller of every
parallel run (``-n``) and holds it until the run ends.  A run with no ``-n``
(one file, a few tests) never waits.  The lock is an OS lock on an open file
(``msvcrt.locking`` / ``fcntl.flock``), so the OS frees it when the holding
process exits for any reason: a killed or crashed run never leaves a stale
lock behind.  Who holds it is written to a side file, for the wait message.

``VIBENODE_TEST_RUN_LOCK=0`` skips it; ``VIBENODE_TEST_RUN_LOCK_WAIT`` caps
the wait in seconds (default 3600), after which the run starts anyway and
says so.
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import time
from pathlib import Path

DEFAULT_PATH = Path(tempfile.gettempdir()) / "vibenode-parallel-test-run.lock"
_LOCK_OFFSET = 0  # one byte, locked; the holder's details live in the .json


class RunLock:
    def __init__(self, path: Path | None = None):
        # DEFAULT_PATH is read at call time, so a test can point it elsewhere.
        self.path = Path(path or DEFAULT_PATH)
        self.info_path = self.path.with_suffix(".json")
        self._fh = None

    @property
    def held(self) -> bool:
        return self._fh is not None

    def try_acquire(self) -> bool:
        """Take the lock if it is free; never blocks."""
        if self._fh is not None:
            return True
        fh = open(self.path, "a+b")
        try:
            if sys.platform == "win32":
                import msvcrt
                fh.seek(_LOCK_OFFSET)
                msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            fh.close()
            return False
        self._fh = fh
        try:
            self.info_path.write_text(json.dumps({
                "pid": os.getpid(), "started": time.time(),
                "args": " ".join(sys.argv[1:])[:200],
            }), encoding="utf-8")
        except OSError:
            pass
        return True

    def holder(self) -> dict:
        try:
            return json.loads(self.info_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}

    def release(self) -> None:
        fh, self._fh = self._fh, None
        if fh is None:
            return
        try:
            if sys.platform == "win32":
                import msvcrt
                fh.seek(_LOCK_OFFSET)
                msvcrt.locking(fh.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
        except OSError:
            pass
        finally:
            fh.close()

    def acquire(self, max_wait: float, poll: float = 2.0, say=None,
                report_every: float = 60.0) -> bool:
        """Wait up to ``max_wait`` seconds for the lock.  True once held;
        False when the wait ran out (the caller runs anyway)."""
        if self.try_acquire():
            return True
        say = say or (lambda msg: (sys.stderr.write(msg + "\n"), sys.stderr.flush()))
        start = time.monotonic()
        last_report = None
        while True:
            now = time.monotonic()
            if last_report is None or now - last_report >= report_every:
                h = self.holder()
                who = f"pid {h.get('pid', '?')}"
                if h.get("started"):
                    who += ", started " + time.strftime("%H:%M:%S", time.localtime(h["started"]))
                say(f"[tests] Another parallel test run is using this machine ({who}). "
                    f"Waiting for it to finish, waited {int(now - start)}s so far "
                    "(VIBENODE_TEST_RUN_LOCK=0 skips the wait).")
                last_report = now
            if now - start >= max_wait:
                say(f"[tests] WARNING: still not free after {int(max_wait)}s; "
                    "running alongside the other run.")
                return False
            time.sleep(poll)
            if self.try_acquire():
                say(f"[tests] The other run finished; starting after {int(time.monotonic() - start)}s.")
                return True


def enabled() -> bool:
    return os.environ.get("VIBENODE_TEST_RUN_LOCK", "1") != "0"


def max_wait() -> float:
    try:
        return float(os.environ.get("VIBENODE_TEST_RUN_LOCK_WAIT", "3600"))
    except ValueError:
        return 3600.0
