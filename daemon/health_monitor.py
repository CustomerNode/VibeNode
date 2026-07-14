"""Session health monitor: stall auto-restart, sleep/wake healing, keep-awake.

This module runs ONE background daemon thread inside the session daemon
process (started from ``SessionManager.start()``).  Every tick it does
three independent jobs, all read-mostly and O(number of sessions):

1. **Stall detection + auto-restart.**  A session that sits in WORKING
   state with zero new output for ``STALL_AFTER_SECONDS`` is considered
   wedged (dead API call, hung tool, lost stream).  The monitor
   interrupts the stuck turn and resumes the conversation: if the
   session has queued messages the interrupt's IDLE emit auto-dispatches
   them (the queue IS the continuation); otherwise the monitor sends a
   short "continue where you left off" nudge.  Attempts are capped at
   ``MAX_AUTO_RESTARTS`` per stall episode — after that the monitor
   interrupts once more, leaves the session IDLE, and posts a system
   entry telling the user to take over.

   "Progress" is measured by a cheap fingerprint of the entry list
   (count, last entry's text length, last entry's timestamp) — NOT by
   ``working_since`` alone, because a healthy long turn can legitimately
   run for a long time while continuously producing output.

2. **Sleep/suspend detection.**  The tick loop compares monotonic time
   between iterations.  A gap far larger than the tick interval means
   the machine slept (or the process was suspended).  On wake the
   monitor resets every stall clock — giving in-flight turns a full
   grace window to recover on their own via the existing stream-heal
   machinery before the stall watchdog is allowed to fire.

3. **Keep-awake (Windows only).**  While at least one session is in a
   WORKING turn, the monitor pulses ``SetThreadExecutionState`` with
   ``ES_SYSTEM_REQUIRED`` each tick, resetting the OS idle timer so the
   machine does not auto-sleep mid-turn.  The display may still turn
   off (we deliberately do not pass ``ES_DISPLAY_REQUIRED``), and an
   explicit user-initiated sleep or lid close is NOT blocked — job 2
   covers recovery for those.  No-op on Linux/macOS.

Tuning knobs (environment variables, read once at import):
    VIBENODE_STALL_MINUTES        minutes of zero output before a WORKING
                                  session counts as stalled (default 10)
    VIBENODE_STALL_MAX_RESTARTS   auto-restart attempts per stall episode
                                  before giving up (default 2)
    VIBENODE_KEEP_AWAKE           set to "0" to let Windows sleep even
                                  while sessions are working (default on)

Design constraints honored here:
- No changes to any PERF-CRITICAL path.  The monitor only READS session
  state on its own thread every ``TICK_SECONDS``; recovery actions go
  through the same public ``interrupt_session``/``send_message`` calls
  the Flask/WS layer uses (both are documented thread-safe entry points).
- Sessions with a scheduled wake-up (``_wakeup_pending``) are never
  treated as stalled — they are legitimately quiet.
- Sessions in a compacting sub-state are never auto-restarted
  (interrupting mid-compact risks a truncated conversation); a wedged
  compact is logged loudly instead.
"""

import ctypes
import logging
import os
import sys
import threading
import time

logger = logging.getLogger(__name__)

# ── Tuning knobs ──────────────────────────────────────────────────────────
TICK_SECONDS = 30.0
STALL_AFTER_SECONDS = max(60.0, float(os.environ.get("VIBENODE_STALL_MINUTES", "10")) * 60.0)
MAX_AUTO_RESTARTS = int(os.environ.get("VIBENODE_STALL_MAX_RESTARTS", "2"))
KEEP_AWAKE = os.environ.get("VIBENODE_KEEP_AWAKE", "1") != "0"
# A tick gap this far beyond TICK_SECONDS means the machine slept or the
# process was suspended (timer callbacks don't run during S3/S4 sleep).
SLEEP_GAP_SECONDS = 120.0

# Windows SetThreadExecutionState flag: "the system is in use, reset the
# idle-to-sleep timer".  Pulsed (without ES_CONTINUOUS) once per tick so
# the effect ends automatically as soon as the monitor stops pulsing.
_ES_SYSTEM_REQUIRED = 0x00000001

NUDGE_TEXT = (
    "[VibeNode watchdog] Your previous turn produced no output for an "
    "extended period and was automatically interrupted. Continue where "
    "you left off. If a long-running command caused the stall, re-run it "
    "in the background or break it into smaller steps."
)

GIVE_UP_TEXT = (
    "Watchdog: session still stalled after {attempts} automatic "
    "restart(s). Leaving it idle for manual review — send a message to "
    "continue."
)


class HealthMonitor:
    """Background watchdog owning stall recovery, wake healing, keep-awake.

    Holds only weak coupling to SessionManager: it reads ``_sessions``
    under the manager's lock and calls the manager's public thread-safe
    API (``interrupt_session``, ``send_message``, ``_emit_entry``).
    """

    def __init__(self, manager) -> None:
        self._sm = manager
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None
        # session_id -> (fingerprint, last_change_wall_ts).  Tracks when a
        # WORKING session last produced observable output.
        self._progress: dict[str, tuple[tuple, float]] = {}
        # session_id -> auto-restart attempts in the current stall episode.
        # Reset when the session is observed IDLE at a tick (i.e. a turn
        # completed and stayed completed — our own interrupt+nudge flips
        # back to WORKING within the same tick, so it is never observed).
        self._restarts: dict[str, int] = {}

    # ── Lifecycle ─────────────────────────────────────────────────────

    def start(self) -> None:
        if self._thread is not None:
            return
        self._thread = threading.Thread(
            target=self._run, daemon=True, name="session-health-monitor"
        )
        self._thread.start()
        logger.info(
            "HealthMonitor started (stall_after=%.0fs, max_restarts=%d, "
            "keep_awake=%s)",
            STALL_AFTER_SECONDS, MAX_AUTO_RESTARTS, KEEP_AWAKE,
        )

    def stop(self) -> None:
        self._stop_event.set()
        if self._thread is not None:
            self._thread.join(timeout=2)
            self._thread = None

    # ── Main loop ─────────────────────────────────────────────────────

    def _run(self) -> None:
        last_mono = time.monotonic()
        while not self._stop_event.wait(TICK_SECONDS):
            now_mono = time.monotonic()
            gap = now_mono - last_mono
            last_mono = now_mono
            # Timer waits do not elapse during system sleep, so a gap far
            # beyond the tick interval means we just woke from sleep (or
            # the whole process was suspended, which needs the same grace).
            woke_from_sleep = gap > (TICK_SECONDS + SLEEP_GAP_SECONDS)
            try:
                self.tick(woke_from_sleep=woke_from_sleep, sleep_gap=gap)
            except Exception:
                # The monitor must never die to an unexpected error — it
                # is the safety net, so it logs and keeps ticking.
                logger.exception("HealthMonitor tick failed")

    # ── One tick (public for tests) ───────────────────────────────────

    def tick(self, woke_from_sleep: bool = False, sleep_gap: float = 0.0) -> None:
        """Run one monitoring pass over all sessions."""
        sm = self._sm
        now = time.time()
        with sm._lock:
            sessions = list(sm._sessions.values())
        live_ids = {info.session_id for info in sessions}

        if woke_from_sleep:
            logger.warning(
                "System sleep/suspend detected (tick gap %.0fs) — resetting "
                "stall clocks to give in-flight turns a recovery grace window",
                sleep_gap,
            )
            # Full grace window after wake: the existing stream-heal
            # machinery gets first shot at recovering interrupted turns;
            # the stall watchdog fires only if nothing moves afterwards.
            self._progress = {
                sid: (fp, now) for sid, (fp, _ts) in self._progress.items()
            }

        any_working = False
        for info in sessions:
            sid = info.session_id
            state = getattr(info.state, "value", str(info.state))
            if state != "working":
                # Not in a turn: drop the stall clock; a completed turn
                # (observed IDLE) also closes the stall episode.
                self._progress.pop(sid, None)
                if state == "idle":
                    self._restarts.pop(sid, None)
                continue

            any_working = True
            # Sessions awaiting a scheduled wake-up are legitimately quiet.
            if getattr(info, "_wakeup_pending", False):
                self._progress.pop(sid, None)
                continue

            fp = self._fingerprint(info)
            record = self._progress.get(sid)
            if record is None or record[0] != fp:
                self._progress[sid] = (fp, now)
                continue

            stalled_for = now - record[1]
            if stalled_for < STALL_AFTER_SECONDS:
                continue
            # Belt and suspenders: never fire inside a turn younger than
            # the stall window (the turn-start user entry refreshes the
            # fingerprint anyway, but working_since is authoritative).
            if info.working_since and (now - info.working_since) < STALL_AFTER_SECONDS:
                continue

            if getattr(info, "substatus", "") == "compacting":
                # Interrupting mid-compact risks a truncated conversation.
                logger.warning(
                    "Session %s appears stalled while compacting "
                    "(%.0f min, no auto-restart) — manual review needed",
                    sid, stalled_for / 60.0,
                )
                continue

            self._recover_stalled(info, stalled_for)

        # Prune bookkeeping for sessions that were removed entirely.
        for sid in list(self._progress):
            if sid not in live_ids:
                self._progress.pop(sid, None)
        for sid in list(self._restarts):
            if sid not in live_ids:
                self._restarts.pop(sid, None)

        if KEEP_AWAKE and any_working:
            self._pulse_keep_awake()

    # ── Stall recovery ────────────────────────────────────────────────

    def _recover_stalled(self, info, stalled_for: float) -> None:
        """Interrupt a wedged turn and resume the conversation."""
        sm = self._sm
        sid = info.session_id
        attempts = self._restarts.get(sid, 0)

        if attempts >= MAX_AUTO_RESTARTS:
            logger.error(
                "Session %s stalled again after %d auto-restart(s) — "
                "giving up, leaving IDLE for manual review", sid, attempts,
            )
            self._announce(info, GIVE_UP_TEXT.format(attempts=attempts))
            sm.interrupt_session(sid, clear_queue=False)
            self._progress.pop(sid, None)
            return

        self._restarts[sid] = attempts + 1
        logger.warning(
            "Session %s stalled (%.0f min with no output) — auto-restarting "
            "turn (attempt %d/%d)",
            sid, stalled_for / 60.0, attempts + 1, MAX_AUTO_RESTARTS,
        )
        self._announce(
            info,
            "Watchdog: no output for %d min — restarting turn (attempt %d/%d)."
            % (int(stalled_for // 60), attempts + 1, MAX_AUTO_RESTARTS),
        )

        # Snapshot the queue BEFORE the interrupt: interrupt_session's
        # IDLE emit auto-dispatches queued messages, and when that happens
        # the queue itself is the continuation — a nudge would just be
        # queued behind it as noise.
        had_queue = bool(sm._mq.get_queue_data(sid))
        result = sm.interrupt_session(sid, clear_queue=False)
        if not result.get("ok"):
            logger.error(
                "Watchdog interrupt of stalled session %s failed: %s",
                sid, result.get("error"),
            )
            return
        self._progress.pop(sid, None)
        if not had_queue:
            send_result = sm.send_message(sid, NUDGE_TEXT)
            if not send_result.get("ok"):
                logger.error(
                    "Watchdog resume nudge for %s failed: %s",
                    sid, send_result.get("error"),
                )

    def _announce(self, info, text: str) -> None:
        """Append a visible system entry to the session timeline."""
        sm = self._sm
        try:
            # Local import: session_manager imports this module lazily in
            # start(), so importing back at call time is cycle-safe.
            from daemon.session_manager import LogEntry
            entry = LogEntry(kind="system", text=text)
            with info._lock:
                info.entries.append(entry)
                index = len(info.entries) - 1
            sm._emit_entry(info.session_id, entry, index)
        except Exception:
            logger.exception("HealthMonitor announce failed for %s", info.session_id)

    # ── Helpers ───────────────────────────────────────────────────────

    @staticmethod
    def _fingerprint(info) -> tuple:
        """Cheap O(1) progress fingerprint of a session's entry list.

        Captures entry count plus the last entry's text length and
        timestamp, so both new entries AND in-place streaming growth of
        the trailing entry register as progress.
        """
        entries = info.entries
        n = len(entries)
        if not n:
            return (0, 0, 0.0)
        try:
            last = entries[n - 1]
            return (n, len(last.text or ""), last.timestamp)
        except IndexError:  # raced a concurrent truncation — treat as change
            return (n, -1, 0.0)

    @staticmethod
    def _pulse_keep_awake() -> None:
        """Reset the Windows idle-to-sleep timer (no-op elsewhere)."""
        if sys.platform != "win32":
            return
        try:
            ctypes.windll.kernel32.SetThreadExecutionState(_ES_SYSTEM_REQUIRED)
        except Exception as e:  # never let keep-awake break the monitor
            logger.debug("SetThreadExecutionState failed: %s", e)
