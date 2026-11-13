"""A parallel run that stops making progress ends itself and says why.

After a worker crash, pytest-xdist can be left with every worker idle and the
controller waiting forever for a result nothing will send.  The run printed
``[ 98%]`` and then nothing: the Publish dialog's ring sat at 98% until
someone cancelled it, and the commit never happened.

No test may run longer than the per-test timeout (pytest-timeout ends it and
xdist reports the crash), so a run in which NO test has finished for twice
that timeout plus a minute is stuck, not slow.  This guard watches for that on
the xdist controller and then:

* prints what it knows: how long nothing finished, which tests crashed their
  worker, and which test files were still unfinished;
* stops this run's own worker processes;
* exits with pytest's internal-error status (3), so whoever started the run
  (the Publish gate, a terminal) gets a result instead of a ring that never
  moves.

It arms only after the first test has reported, so worker start-up and
collection are never timed.  It acts only on an xdist controller; the Linux
fork runner has its own silent-worker handling (tests/_fork_runner.py).

Kill switch: ``VIBENODE_TEST_STALL_GUARD=0``.  ``VIBENODE_TEST_STALL_SECONDS``
sets the limit directly.
"""

from __future__ import annotations

import os
import sys
import threading
import time

EXIT_STALLED = 3          # pytest's ExitCode.INTERNAL_ERROR
DEFAULT_TIMEOUT_S = 120   # the Publish gate's longest per-test timeout
POLL_S = 5.0


def limit_for(test_timeout) -> float:
    """Seconds without a finished test after which the run counts as stuck."""
    env = os.environ.get("VIBENODE_TEST_STALL_SECONDS")
    if env:
        try:
            return max(1.0, float(env))
        except ValueError:
            pass
    try:
        t = float(test_timeout or 0)
    except (TypeError, ValueError):
        t = 0.0
    if t <= 0:
        t = DEFAULT_TIMEOUT_S
    return 2 * t + 60


def enabled() -> bool:
    return os.environ.get("VIBENODE_TEST_STALL_GUARD", "1") != "0"


def unfinished_files(sched) -> list[str]:
    """Test files the xdist scheduler still has queued or running."""
    files: set[str] = set()
    try:
        for scope in list(getattr(sched, "workqueue", {}) or {}):
            files.add(str(scope).split("::", 1)[0])
        for work in list((getattr(sched, "assigned_work", {}) or {}).values()):
            for scope, done in work.items():
                if not all(done.values()):
                    files.add(str(scope).split("::", 1)[0])
        # --dist load: indexes into the collection, queued and per worker.
        collection = getattr(sched, "collection", None) or []
        indexes = list(getattr(sched, "pending", None) or [])
        for pend in (getattr(sched, "node2pending", None) or {}).values():
            indexes.extend(pend)
        for i in indexes:
            if isinstance(i, int) and 0 <= i < len(collection):
                files.add(str(collection[i]).split("::", 1)[0])
    except Exception:
        pass
    return sorted(files)


class StallGuard:
    def __init__(self, config, limit: float, *, poll: float = POLL_S,
                 clock=time.monotonic, on_stall=None):
        self.config = config
        self.limit = limit
        self.poll = poll
        self.clock = clock
        self.crashes: list[str] = []
        self._last: float | None = None
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._on_stall = on_stall or self._give_up

    # ── what the hooks tell it ──────────────────────────────────────────
    def note_report(self, report) -> None:
        self._last = self.clock()
        if getattr(report, "when", None) == "???":      # xdist's crash report
            self.crashes.append(report.nodeid)

    # ── the watch ───────────────────────────────────────────────────────
    def start(self) -> None:
        self._thread = threading.Thread(target=self._watch, name="vn-stall-guard", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def check(self) -> bool:
        """True (and the stall handled) when the run is stuck right now."""
        last = self._last
        if last is None or self._dsession() is None:
            return False
        idle = self.clock() - last
        if idle < self.limit:
            return False
        self._on_stall(self.report(idle))
        return True

    def _watch(self) -> None:
        while not self._stop.wait(self.poll):
            if self.check():
                return

    def _dsession(self):
        try:
            return self.config.pluginmanager.getplugin("dsession")
        except Exception:
            return None

    def report(self, idle: float) -> str:
        sched = getattr(self._dsession(), "sched", None)
        files = unfinished_files(sched)
        lines = [
            "",
            "=" * 70,
            f"STALLED: no test finished for {int(idle)}s (limit {int(self.limit)}s, "
            "longer than any test may run).",
            "The parallel runner stopped handing out work, so this run is ended "
            "instead of waiting forever.",
        ]
        if self.crashes:
            lines.append("Tests that crashed their worker: " + ", ".join(self.crashes))
        if files:
            lines.append("Test files never finished: " + ", ".join(files))
        lines.append("=" * 70)
        return "\n".join(lines) + "\n"

    def _give_up(self, message: str) -> None:
        try:
            sys.stdout.write(message)
            sys.stdout.flush()
        except Exception:
            pass
        _stop_own_workers()
        os._exit(EXIT_STALLED)


def _stop_own_workers() -> None:
    """End this process's own child processes (the xdist workers)."""
    try:
        import psutil
        kids = psutil.Process(os.getpid()).children(recursive=True)
    except Exception:
        return
    for p in kids:
        try:
            p.kill()
        except Exception:
            pass
