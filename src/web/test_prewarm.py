"""Start a full test run by itself once editing has been quiet, so the
publish gate usually finds a passing run on identical files to reuse
(src/web/gate_reuse.py) instead of making the person wait five minutes.

One daemon thread, started from ``create_app`` (never in a test app).  Every
``TICK_S`` it fingerprints the working tree.  When the fingerprint has not
changed for ``QUIET_S`` seconds, no passing record covers it, no test run is
in progress and no other parallel run holds the machine lock
(src/shared/test_run_lock.py), it starts a full run through the same path the
Publish button uses (``test_api.start_run``, source "prewarm"), so a Publish
pressed meanwhile joins that run instead of starting another.  A fingerprint
is tried once: a run that fails or is cancelled is not repeated until a file
changes.

``VIBENODE_TEST_PREWARM=0`` turns it off; ``VIBENODE_TEST_PREWARM_QUIET``
is the quiet window in seconds (default 300).
"""

from __future__ import annotations

import logging
import os
import threading
import time

log = logging.getLogger(__name__)

TICK_S = 60


def quiet_seconds() -> float:
    try:
        return float(os.environ.get("VIBENODE_TEST_PREWARM_QUIET", "300"))
    except ValueError:
        return 300.0


def enabled() -> bool:
    return os.environ.get("VIBENODE_TEST_PREWARM", "1") != "0"


class Prewarm:
    def __init__(self):
        self.last_fp = None
        self.changed_at = time.time()
        self.attempted = None
        self.last_reason = ""

    def tick(self, now: float | None = None) -> str:
        """One decision; returns why it did or did not start a run."""
        now = now if now is not None else time.time()
        from . import gate_reuse
        from .routes import test_api
        if not enabled() or not gate_reuse.enabled():
            return self._say("off")
        if test_api.run_in_progress():
            return self._say("a test run is in progress")
        fp = gate_reuse.fingerprint()
        if fp["all"] != self.last_fp:
            self.last_fp = fp["all"]
            self.changed_at = now
            return self._say("files changed; waiting for quiet")
        if now - self.changed_at < quiet_seconds():
            return self._say("settling")
        if gate_reuse.plan(fp=fp, now=now).get("whole"):
            return self._say("a passing run already covers these files")
        if self.attempted == fp["all"]:
            return self._say("already tried these files")
        from shared.test_run_lock import RunLock
        lock = RunLock()
        if not lock.try_acquire():
            return self._say("another parallel test run holds the machine")
        lock.release()
        self.attempted = fp["all"]
        res = test_api.start_run("full", source="prewarm")
        if "run" in res:
            log.info("Pre-warm test run started: %s", res["run"]["id"])
            return self._say("started")
        return self._say("not started: " + str(res.get("error") or "busy"))

    def _say(self, reason: str) -> str:
        self.last_reason = reason
        return reason


_prewarm = None


def start_test_prewarm(app=None):
    """Start the thread; returns the Prewarm, or None when switched off."""
    global _prewarm
    if not enabled():
        return None
    _prewarm = Prewarm()

    def loop():
        while True:
            try:
                _prewarm.tick()
            except Exception:
                log.exception("Pre-warm tick failed")
            time.sleep(TICK_S)

    threading.Thread(target=loop, name="test-prewarm", daemon=True).start()
    return _prewarm
