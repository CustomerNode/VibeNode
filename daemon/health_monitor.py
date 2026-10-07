"""Session health monitor: stall auto-restart, sleep/wake healing, keep-awake,
stranded-worker recovery.

This module runs ONE background daemon thread inside the session daemon
process (started from ``SessionManager.start()``).  Every tick it does
four independent jobs, all read-mostly and O(number of sessions):

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

4. **Stranded-worker recovery.**  Jobs 1-3 only watch sessions that are
   WORKING.  This one covers a session that ended its turn normally (IDLE)
   while it waits on background workers that can never report.  Claude Code
   delivers a nested agent's completion notice (an agent launched BY a
   background worker) to the top-level session, never to the worker that
   launched it.  A worker that stopped to wait for its own reviewer
   therefore waits forever, and so does the session waiting on that worker.
   This cost a whole day on 2026-10-06: two workers sat 343 and 317 minutes
   after their reviewers had finished, and the IDLE session was invisible
   to job 1.  Once a session has been IDLE for
   ``ORPHAN_IDLE_GRACE_SECONDS``, the monitor checks the CLI's on-disk
   transcripts (``daemon/orphaned_workers.py``).  For any stranded worker it
   sends the session ONE message naming the worker and the result to
   forward.  Each stranded result is nudged at most once.  Sessions the user
   stopped, and sessions with queued messages, are left alone.

Tuning knobs (environment variables, read once at import):
    VIBENODE_STALL_MINUTES        minutes of zero output before a WORKING
                                  session counts as stalled (default 10)
    VIBENODE_STALL_MAX_RESTARTS   auto-restart attempts per stall episode
                                  before giving up (default 2)
    VIBENODE_KEEP_AWAKE           set to "0" to let Windows sleep even
                                  while sessions are working (default on)
    VIBENODE_ORPHAN_RECOVERY      set to "0" to disable job 4 (default on)
    VIBENODE_ORPHAN_GRACE_SECONDS seconds a session must sit IDLE before
                                  job 4 looks for stranded workers
                                  (default 90, minimum 30)

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
from pathlib import Path

from daemon.orphaned_workers import ScanCache, build_nudge, find_orphaned_workers

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

# ── Stranded-worker recovery (job 4, see daemon/orphaned_workers.py) ──────
ORPHAN_RECOVERY = os.environ.get("VIBENODE_ORPHAN_RECOVERY", "1") != "0"
# How long a session must sit IDLE before job 4 looks for stranded workers.
# This lets the post-turn machinery settle (chained auto-resumes arrive within
# seconds of RESULT).  It also keeps the nudge from racing a user who is still
# reading the final message.  The tick interval adds up to 30s on top, so the
# nudge lands 90-120s after the session goes idle, against the hours lost
# without it.
ORPHAN_IDLE_GRACE_SECONDS = max(
    30.0, float(os.environ.get("VIBENODE_ORPHAN_GRACE_SECONDS", "90"))
)
# While a session stays IDLE, re-check at most this often.  The check itself
# is gated on the root transcript growing (ScanCache.evaluated_size), so an
# unchanged long-idle session costs one or two stats per re-check.
ORPHAN_RESCAN_SECONDS = 300.0

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
        # ── Job 4 bookkeeping (stranded-worker recovery) ──
        # session_id -> wall time this monitor first saw the current IDLE
        # episode.  Cleared whenever the session is seen in any other state.
        self._idle_since: dict[str, float] = {}
        # session_id -> wall time of the last stranded-worker check in the
        # current IDLE episode.
        self._orphan_checked_at: dict[str, float] = {}
        # (session_id, nested_agent_id, delivered_at) already nudged.  Never
        # nudge the same stranded result twice.  A resumed nested agent that
        # stops again has a new delivered_at, so it is evaluated afresh.
        self._orphan_nudged: set[tuple] = set()
        # session_id -> incremental transcript scan state.
        self._orphan_caches: dict[str, ScanCache] = {}
        # session_id -> resolved root transcript path.
        self._transcript_paths: dict[str, Path] = {}

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
                    self._check_stranded_workers(info, now)
                else:
                    self._end_idle_episode(sid)
                continue

            # A turn is running: any IDLE episode is over, and the next one
            # gets a fresh grace window.
            self._end_idle_episode(sid)
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
        for book in (self._idle_since, self._orphan_checked_at,
                     self._orphan_caches, self._transcript_paths):
            for sid in list(book):
                if sid not in live_ids:
                    book.pop(sid, None)
        self._orphan_nudged = {k for k in self._orphan_nudged if k[0] in live_ids}

        if KEEP_AWAKE and any_working:
            self._pulse_keep_awake()

    # ── Stall recovery ────────────────────────────────────────────────

    def _recover_stalled(self, info, stalled_for: float) -> None:
        """Interrupt a wedged turn and resume the conversation."""
        sm = self._sm
        sid = info.session_id

        # ── PHANTOM-TURN GUARD (2026-09-29) ─────────────────────────────
        # The post-turn listeners' resume pre-detect can flip an idle
        # session to WORKING on post-sleep transport chatter with NO turn
        # behind it. No RESULT ever comes, entries stay frozen, and this
        # watchdog then "recovered" a healthy-idle session — interrupting
        # it and injecting the nudge as a user-visible message. Observed
        # as mass simultaneous stalls ~10 min after every laptop wake,
        # and users being spammed with watchdog nudges on idle sessions.
        #
        # Discriminator: every REAL turn appends at least one entry at or
        # after the WORKING flip (the user/nudge message, the wake-up's
        # task_notification, streamed content). If nothing was appended
        # since ``working_since``, there is no turn to restart — quietly
        # correct the state to IDLE (via the same thread-safe interrupt
        # path) and send NO nudge: nudging a phantom burns a real model
        # turn and spams the transcript of a session that was never stuck.
        last_entry_ts = 0.0
        try:
            if info.entries:
                last_entry_ts = float(info.entries[-1].timestamp or 0.0)
        except Exception:
            last_entry_ts = 0.0
        if info.working_since and last_entry_ts < info.working_since:
            logger.warning(
                "Session %s: WORKING for %.0f min with no turn content since "
                "the flip — phantom auto-resume; correcting to IDLE without "
                "a nudge", sid, stalled_for / 60.0,
            )
            sm.interrupt_session(sid, clear_queue=False)
            self._progress.pop(sid, None)
            self._restarts.pop(sid, None)
            return

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

    # ── Stranded-worker recovery (job 4) ──────────────────────────────

    def _end_idle_episode(self, sid: str) -> None:
        """Forget the current IDLE episode's clocks (session left IDLE)."""
        self._idle_since.pop(sid, None)
        self._orphan_checked_at.pop(sid, None)

    def _check_stranded_workers(self, info, now: float) -> None:
        """Nudge an IDLE session whose background workers can never report.

        See the module docstring (job 4) and ``daemon/orphaned_workers.py``
        for the CLI routing bug this works around.  The decision itself is
        file-based and exact.  This method only adds the conditions under
        which acting is appropriate:

        * the session has been IDLE for the grace window (checked again at
          most every ``ORPHAN_RESCAN_SECONDS`` while it stays IDLE);
        * the user did not stop it (``_interrupted``: Stop, or the stall
          watchdog giving up).  An explicit stop must stick;
        * nothing is queued (a queued message is already the continuation;
          the next IDLE episode re-checks);
        * the stranded result reached the session after the daemon took it
          on (``created_ts``), so history from before a daemon restart can
          never trigger a nudge.
        """
        if not ORPHAN_RECOVERY:
            return
        sid = info.session_id
        idle_since = self._idle_since.setdefault(sid, now)
        if now - idle_since < ORPHAN_IDLE_GRACE_SECONDS:
            return
        last = self._orphan_checked_at.get(sid)
        if last is not None and now - last < ORPHAN_RESCAN_SECONDS:
            return
        self._orphan_checked_at[sid] = now

        if getattr(info, "_interrupted", False):
            return
        sm = self._sm
        try:
            if sm._mq.get_queue_data(sid):
                return
        except Exception:
            return
        path = self._transcript_path(info)
        if path is None:
            return
        cache = self._orphan_caches.setdefault(sid, ScanCache())
        since = float(getattr(info, "created_ts", 0.0) or 0.0)
        try:
            stranded = find_orphaned_workers(path, cache, since=since)
        except Exception:
            # The detector is written to be exception-free; this is the
            # belt-and-braces guard that keeps the monitor ticking.
            logger.exception("Stranded-worker check failed for %s", sid)
            return
        fresh = [o for o in stranded
                 if (sid, o.nested_id, o.delivered_at) not in self._orphan_nudged]
        if not fresh:
            return
        # Mark BEFORE sending: whatever happens next, this result is never
        # nudged twice (a failed send is logged for manual follow-up).
        for o in fresh:
            self._orphan_nudged.add((sid, o.nested_id, o.delivered_at))
        logger.warning(
            "Session %s: %d background worker(s) stranded waiting on nested "
            "agents whose results were delivered to the session instead "
            "(%s). Nudging the session to forward them.",
            sid, len(fresh),
            ", ".join(f"{o.worker_id}<-{o.nested_id} [{o.reason}]" for o in fresh),
        )
        # The announce is what the user sees live.  send_message only pushes
        # the user bubble to clients after an interrupt (the frontend normally
        # renders its own sends optimistically), and this nudge goes to an
        # IDLE session with no interrupt, so the nudge text itself shows up
        # only on the next log load.
        names = ", ".join(
            sorted({o.worker_desc or o.worker_id for o in fresh})
        )
        self._announce(
            info,
            "Watchdog: %d background worker(s) stuck waiting on results that "
            "were delivered to this session instead (%s). Asked the session "
            "to forward them." % (len(fresh), names),
        )
        result = sm.send_message(sid, build_nudge(fresh))
        if not result.get("ok"):
            logger.error(
                "Stranded-worker nudge for %s failed: %s",
                sid, result.get("error"),
            )

    def _transcript_path(self, info):
        """The session's root ``.jsonl`` path (cached), or None."""
        sid = info.session_id
        cached = self._transcript_paths.get(sid)
        if cached is not None and cached.exists():
            return cached
        store = getattr(self._sm, "_store", None)
        if store is None:
            return None
        try:
            found = store.find_session_path(sid, getattr(info, "cwd", "") or "")
        except Exception:
            return None
        if not found:
            return None
        path = Path(found)
        self._transcript_paths[sid] = path
        return path

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
