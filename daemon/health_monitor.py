"""Session health monitor: stall auto-restart, sleep/wake healing, keep-awake,
stranded-worker recovery, silent-wait backstop, usage-limit resume backstop.

This module runs ONE background daemon thread inside the session daemon
process (started from ``SessionManager.start()``).  Every tick it does
six independent jobs, all read-mostly and O(number of sessions):

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

5. **Silent-wait backstop (any cause).**  Jobs 1 and 4 each recognise
   one known cause, and every new cause slipped past them until someone
   diagnosed it.  Job 5 watches the symptom instead
   (``daemon/silent_wait.py``).  The session is IDLE, its final message says
   it is waiting on background work it launched in this task, and nothing
   is alive: no write anywhere in its footprint, no background command
   running under its CLI, no scheduled wake-up.  It gets ONE nudge.  If it
   idles "waiting" again with nothing alive, it is escalated to the user
   (an error entry, plus a ``session_stalled`` push that pings every open
   VibeNode tab) instead of being nudged again.

6. **Usage-limit resume backstop.**  The user's rule: a session stopped by
   a usage limit must ALWAYS continue once the limit resets.
   session_manager's timed resume (Layer 1) is in-memory, so a Session
   Engine restart loses it, and it depends on recognising the message.
   Every 2 minutes this job reads the transcripts of the IDLE and dormant
   sessions the daemon knows (``daemon/limit_watch.py``).  If one still ends
   on the CLI's structural ``rate_limit`` stop (or a ``server_error`` stop)
   after its reset time plus grace, it sends the continue prompt.  Sessions
   the user stopped, slept or deleted are never touched: they are neither
   live-IDLE nor in the restart memory.

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
    VIBENODE_SILENT_WAIT_RECOVERY set to "0" to disable job 5 (default on)
    VIBENODE_SILENT_WAIT_MINUTES  minutes of total silence before job 5
                                  acts when nothing is running (default
                                  10, minimum 2)
    VIBENODE_LIMIT_BACKSTOP       set to "0" to disable job 6 (default on)

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

from daemon.limit_watch import due_at as limit_due_at
from daemon.limit_watch import stopped_on_error
from daemon.orphaned_workers import ScanCache, build_nudge, find_orphaned_workers
from daemon.silent_wait import (
    build_nudge as build_silent_wait_nudge,
    footprint_last_write,
    live_commands,
    says_waiting_on_background,
)

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

# ── Silent-wait backstop (job 5, see daemon/silent_wait.py) ───────────────
SILENT_WAIT_RECOVERY = os.environ.get("VIBENODE_SILENT_WAIT_RECOVERY", "1") != "0"
# Total silence before acting when NOTHING is running.  Nothing alive means
# nothing can ever report back, so there is no reason to wait long.  The
# longest legitimate gap is a running agent's single API call, which still
# streams task_progress events (see _bg_work_at).
SILENT_WAIT_SECONDS = max(
    120.0, float(os.environ.get("VIBENODE_SILENT_WAIT_MINUTES", "10")) * 60.0
)
# Background commands ARE running but nothing has been written anywhere for
# this long.  Catches a hung command, or a forgotten dev server masking a dead
# wait.  Long, because quiet-but-healthy runs exist (a suite piped through
# ``tail`` prints nothing until it ends).
SILENT_WAIT_LIVE_SECONDS = max(SILENT_WAIT_SECONDS, 45 * 60.0)
# psutil unavailable: a quiet running command cannot be told from a dead one.
SILENT_WAIT_UNKNOWN_SECONDS = max(SILENT_WAIT_SECONDS, 30 * 60.0)
# The footprint stat walk and the process check run at most this often per
# IDLE session.
SILENT_WAIT_RECHECK_SECONDS = 60.0

# ── Usage-limit resume backstop (job 6, see daemon/limit_watch.py) ────────
LIMIT_BACKSTOP = os.environ.get("VIBENODE_LIMIT_BACKSTOP", "1") != "0"
# How often the transcripts of IDLE + dormant sessions are checked.  Reads are
# cached by (size, mtime), so an unchanged transcript costs one stat.
LIMIT_SWEEP_SECONDS = 120.0
# Skip the first sweeps after startup: crash recovery is still relaunching
# sessions, and Layer 1 state is being rebuilt.
LIMIT_STARTUP_GRACE_SECONDS = 120.0
# After resuming a session for a given stop, do not try again for that same
# stop sooner than this (the resumed turn normally rewrites the transcript
# within seconds, so this only matters when a resume did not take).
LIMIT_REATTEMPT_SECONDS = 1800.0
# A Layer-1 countdown this far past its fire time has a dead timer (e.g. the
# loop stalled); the backstop takes over.
LIMIT_STALE_COUNTDOWN_SECONDS = 600.0

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
        # ── Job 5 bookkeeping (silent-wait backstop) ──
        # session_id -> wall time of the last silent-wait evaluation in the
        # current IDLE episode.
        self._silent_checked_at: dict[str, float] = {}
        # session_id -> task-start value of the task our nudge started.  An
        # IDLE episode of that same task (nothing new from the user since)
        # escalates to the user instead of nudging again.
        self._silent_nudged: dict[str, float] = {}
        # session_id -> task-start value already escalated (tell the user
        # once per task).
        self._silent_escalated: dict[str, float] = {}
        # ── Job 6 bookkeeping (usage-limit resume backstop) ──
        self._started_at = time.time()
        self._limit_swept_at = 0.0
        # session_id -> (stop timestamp, when we last resumed it for that stop)
        self._limit_attempts: dict[str, tuple] = {}
        # transcript path -> ((size, mtime_ns), stopped_on_error result)
        self._limit_stop_cache: dict = {}

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
            "keep_awake=%s, stranded_workers=%s, silent_wait=%s/%.0fs, "
            "limit_backstop=%s)",
            STALL_AFTER_SECONDS, MAX_AUTO_RESTARTS, KEEP_AWAKE,
            ORPHAN_RECOVERY, SILENT_WAIT_RECOVERY, SILENT_WAIT_SECONDS,
            LIMIT_BACKSTOP,
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
                    # One IDLE clock shared by jobs 4 and 5 (set here so it
                    # runs even when either job is switched off).
                    self._idle_since.setdefault(sid, now)
                    self._check_stranded_workers(info, now)
                    self._check_silent_wait(info, now)
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
                     self._orphan_caches, self._transcript_paths,
                     self._silent_checked_at, self._silent_nudged,
                     self._silent_escalated):
            for sid in list(book):
                if sid not in live_ids:
                    book.pop(sid, None)
        self._orphan_nudged = {k for k in self._orphan_nudged if k[0] in live_ids}

        # Job 6 runs over live AND dormant sessions, so it is not part of the
        # per-live-session loop above.
        try:
            self._limit_backstop(now, sessions)
        except Exception:
            logger.exception("Usage-limit resume backstop failed")

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

    def _announce(self, info, text: str, is_error: bool = False) -> None:
        """Append a visible system entry to the session timeline."""
        sm = self._sm
        try:
            # Local import: session_manager imports this module lazily in
            # start(), so importing back at call time is cycle-safe.
            from daemon.session_manager import LogEntry
            entry = LogEntry(kind="system", text=text, is_error=is_error)
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
        self._silent_checked_at.pop(sid, None)

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

    # ── Silent-wait backstop (job 5) ──────────────────────────────────

    @staticmethod
    def _task_start(info) -> float:
        """When the session's current task began.

        That is the last genuine send, or the moment the daemon took the
        session on, whichever is later.
        """
        return max(float(getattr(info, "_task_started_at", 0.0) or 0.0),
                   float(getattr(info, "created_ts", 0.0) or 0.0))

    @staticmethod
    def _last_assistant_text(info) -> str:
        """Text of the session's most recent assistant entry ('' if none)."""
        try:
            entries = info.entries
            for i in range(len(entries) - 1, max(-1, len(entries) - 60), -1):
                entry = entries[i]
                if getattr(entry, "kind", "") == "asst":
                    return entry.text or ""
        except (IndexError, AttributeError):
            pass
        return ""

    def _check_silent_wait(self, info, now: float) -> None:
        """Act on an IDLE session waiting on background work that is dead.

        Cause-agnostic backstop: see the module docstring (job 5) and
        ``daemon/silent_wait.py``.  Cheap in-memory gates run first, and the
        footprint stat walk plus the process check at most once a minute per
        IDLE session.  Acts at most once per task: a nudge first, then (same
        task, still dead) an escalation to the user.
        """
        if not SILENT_WAIT_RECOVERY:
            return
        if getattr(info.state, "value", str(info.state)) != "idle":
            return  # e.g. job 4 just started a turn on this tick
        if getattr(info, "_interrupted", False):
            return  # the user stopped it: an explicit stop must stick
        sid = info.session_id
        task_start = self._task_start(info)
        if self._silent_escalated.get(sid) == task_start:
            return  # the user has already been told about this task
        after_nudge = self._silent_nudged.get(sid) == task_start
        bg_at = float(getattr(info, "_bg_work_at", 0.0) or 0.0)
        if not after_nudge and (bg_at <= 0.0 or bg_at < task_start):
            # No background work in this task.  An idle session "waiting" is
            # waiting on the user, which is the normal end of a turn.
            return
        if getattr(info, "_wakeup_is_scheduled", False) and \
                float(getattr(info, "_wakeup_deadline", 0.0) or 0.0) > now:
            return  # a real timer will wake it; the deadline watchdog owns it
        last = self._silent_checked_at.get(sid)
        if last is not None and now - last < SILENT_WAIT_RECHECK_SECONDS:
            return
        self._silent_checked_at[sid] = now
        if not says_waiting_on_background(self._last_assistant_text(info)):
            return
        try:
            if self._sm._mq.get_queue_data(sid):
                return  # a queued message is already the continuation
        except Exception:
            return

        idle_since = self._idle_since.get(sid, now)
        path = self._transcript_path(info)
        last_write = footprint_last_write(path) if path is not None else 0.0
        quiet_for = now - max(last_write, bg_at, idle_since)
        live = live_commands(getattr(info, "_cli_pid", 0))
        if live is None:
            threshold = SILENT_WAIT_UNKNOWN_SECONDS
        elif live:
            threshold = SILENT_WAIT_LIVE_SECONDS
        else:
            threshold = SILENT_WAIT_SECONDS
        if quiet_for < threshold:
            return
        minutes = int(quiet_for // 60)
        if after_nudge:
            self._escalate_silent_wait(info, minutes, task_start)
        else:
            self._nudge_silent_wait(info, minutes, live, task_start)

    def _nudge_silent_wait(self, info, minutes: int, live, task_start: float) -> None:
        """First response: tell the session its wait is dead (once per task)."""
        sid = info.session_id
        logger.warning(
            "Session %s: IDLE %d min waiting on background work with nothing "
            "alive (live commands: %s). Nudging it to check its work.",
            sid, minutes, live,
        )
        self._announce(
            info,
            "Watchdog: this session has been waiting %d min on background "
            "work with nothing making progress. Asked it to check its work "
            "and continue." % minutes,
        )
        result = self._sm.send_message(sid, build_silent_wait_nudge(minutes, live))
        if result.get("ok"):
            # send_message stamped a new task start.  Remember it, so the next
            # IDLE episode of this same task escalates instead of nudging
            # again.
            self._silent_nudged[sid] = self._task_start(info)
        else:
            logger.error("Silent-wait nudge for %s failed: %s", sid, result.get("error"))
            self._escalate_silent_wait(info, minutes, task_start)

    def _escalate_silent_wait(self, info, minutes: int, task_start: float) -> None:
        """Second response: the nudge did not help, so tell the user.

        This adds an error entry in the session, and pushes
        ``session_stalled`` to every open tab, which chimes and notifies
        through static/js/notify.js.
        """
        sid = info.session_id
        self._silent_escalated[sid] = task_start
        text = (
            "Watchdog: this session is still waiting on background work that "
            "is not making progress (%d min with no activity, after a nudge). "
            "It needs you." % minutes
        )
        logger.error("Session %s: %s", sid, text)
        self._announce(info, text, is_error=True)
        push = getattr(self._sm, "_push_callback", None)
        if push:
            try:
                push("session_stalled", {
                    "session_id": sid,
                    "name": getattr(info, "name", "") or "",
                    "text": text,
                })
            except Exception:
                logger.exception("session_stalled push failed for %s", sid)

    # ── Usage-limit resume backstop (job 6) ───────────────────────────

    def _limit_backstop(self, now: float, sessions: list) -> None:
        """Resume any session still stopped on a limit after its reset.

        See the module docstring (job 6) and ``daemon/limit_watch.py``.
        Candidates are only sessions the user has not stopped:

        * live sessions that are IDLE, not interrupted (Stop), and not already
          counting down to a resume of their own (Layer 1) unless that
          countdown is long overdue;
        * dormant sessions in the daemon's restart memory (an explicit stop,
          sleep or delete removes a session from it).
        """
        if not LIMIT_BACKSTOP:
            return
        if now - self._started_at < LIMIT_STARTUP_GRACE_SECONDS:
            return
        if now - self._limit_swept_at < LIMIT_SWEEP_SECONDS:
            return
        self._limit_swept_at = now
        sm = self._sm
        parse = getattr(sm, "_parse_usage_limit", None)
        if getattr(sm, "_store", None) is None or parse is None:
            return
        cont = getattr(sm, "_API_RETRY_CONTINUE_PROMPT", "") or \
            "Continue from where you left off."

        candidates = []   # (session_id, live SessionInfo or None, cwd)
        seen = set()
        for info in sessions:
            sid = info.session_id
            seen.add(sid)
            if getattr(info.state, "value", str(info.state)) != "idle":
                continue
            if getattr(info, "_interrupted", False):
                continue
            if (getattr(info, "session_type", "") or "") not in ("", "normal"):
                continue
            retry_at = float(getattr(info, "retry_at", 0.0) or 0.0)
            if retry_at > 0 and now < retry_at + LIMIT_STALE_COUNTDOWN_SECONDS:
                continue   # Layer 1 owns this resume
            candidates.append((sid, info, getattr(info, "cwd", "") or ""))
        try:
            dormant = sm.get_dormant_states() or {}
        except Exception:
            dormant = {}
        for sid, meta in dormant.items():
            meta = meta or {}
            if sid in seen or (meta.get("session_type", "") or "") not in ("", "normal"):
                continue
            candidates.append((sid, None, meta.get("cwd", "") or ""))

        live_or_dormant = {c[0] for c in candidates}
        for sid in list(self._limit_attempts):
            if sid not in live_or_dormant and sid not in seen:
                self._limit_attempts.pop(sid, None)

        for sid, info, cwd in candidates:
            path = self._limit_path(sid, cwd)
            if path is None:
                continue
            stop = self._cached_stop(path, cont)
            if stop is None or now < limit_due_at(stop, parse):
                continue
            last = self._limit_attempts.get(sid)
            if last and last[0] == stop["ts"] and now - last[1] < LIMIT_REATTEMPT_SECONDS:
                continue
            self._limit_attempts[sid] = (stop["ts"], now)
            self._resume_after_stop(sid, info, stop, cont)

    def _cached_stop(self, path, cont: str):
        """``stopped_on_error`` for ``path``, re-read only when the file changed."""
        try:
            st = os.stat(path)
        except OSError:
            self._limit_stop_cache.pop(path, None)
            return None
        key = (st.st_size, st.st_mtime_ns)
        hit = self._limit_stop_cache.get(path)
        if hit is not None and hit[0] == key:
            return hit[1]
        stop = stopped_on_error(path, cont)
        self._limit_stop_cache[path] = (key, stop)
        return stop

    def _limit_path(self, sid: str, cwd: str):
        """Root transcript path for a live or dormant session (cached)."""
        cached = self._transcript_paths.get(sid)
        if cached is not None and cached.exists():
            return cached
        store = getattr(self._sm, "_store", None)
        if store is None:
            return None
        try:
            found = store.find_session_path(sid, cwd)
        except Exception:
            return None
        if not found:
            return None
        path = Path(found)
        self._transcript_paths[sid] = path
        return path

    def _resume_after_stop(self, sid: str, info, stop: dict, cont: str) -> None:
        """Send the continue prompt to a session nothing else resumed."""
        what = "usage limit" if stop["error"] == "rate_limit" else "server error"
        logger.warning(
            "Session %s: stopped on a %s at %s and nothing resumed it "
            "(%d consecutive). Continuing it now (limit backstop).",
            sid, what, time.strftime("%m-%d %H:%M", time.localtime(stop["ts"])),
            stop["failures"],
        )
        if info is not None:
            self._announce(
                info,
                "Watchdog: the %s that stopped this session has passed. "
                "Continuing automatically." % what,
            )
        # send_message also wakes a dormant session from its transcript.
        result = self._sm.send_message(sid, cont)
        if not result.get("ok"):
            logger.error("Limit backstop could not resume %s: %s",
                         sid, result.get("error"))

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
