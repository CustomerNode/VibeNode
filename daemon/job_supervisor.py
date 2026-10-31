"""Durable Jobs supervisor: brings registered long jobs back after a restart.

One daemon thread beside the health monitor (``daemon/health_monitor.py``),
same tick, started from ``SessionManager.start()``.  Spec:
docs/plans/durable-jobs-spec.md.  The registry and every file-based judgement
live in ``daemon/durable_jobs.py``; this module only decides and acts.

What a tick does, for every job in ``running`` or ``relaunched``:

* **Alive** (a process carries its command line): refresh
  ``last_seen_alive`` and the progress reading.  If the done marker appears,
  the job is ``finished``.
* **Done** (marker present, process or not): ``finished``.  Never relaunched.
* **Absent and not done:**
  - past its irreversible marker → ``needs_person``.  A launch is never
    repeated past an irreversible step; this wins over everything.
  - marked non-resumable → ``failed``.
  - dead three times in a row with no progress between the relaunches →
    ``needs_person``.  "Progress" is the progress pattern advancing when the
    job has one; otherwise the log growing AND the job having lived past
    ``YOUNG_DEATH_SECONDS`` (a job that crashes at once with a traceback has
    written output, but has not progressed).  A job that dies after real
    progress is not broken: its count resets and it keeps its relaunches.
  - otherwise → relaunched, and the session that launched it gets ONE
    system entry in its own transcript.  Nothing else is shown: the normal
    path is invisible.

**Reconcile on start** is the first tick, run ``FIRST_TICK_SECONDS`` after
the daemon starts (login, or boot via boot_access), with the entry worded
"after restart".

**What surfaces.**  Only ``needs_person`` and ``failed``: a ``job_needs_person``
push to every open tab (and the phone, which is a tab), with the reason and
two buttons, Relaunch and Leave it (static/js/socket.js).  Each is pushed once
per decision; the Leave-it button acknowledges, Relaunch resets.

**Safety.**  The supervisor changes nothing on the hot path: it reads logs
and process lists, and only writes the registry and launches processes.  A
registry it cannot parse is reported (log + one ``job_registry_error`` push
per file version) and never rewritten.  When the process list cannot be
read, nothing is relaunched: a job that might be alive is left alone.

Kill switch: ``VIBENODE_DURABLE_JOBS=0``.  Tests: tests/test_job_supervisor.py.
"""

from __future__ import annotations

import logging
import os
import threading
import time

from daemon import durable_jobs as dj

logger = logging.getLogger(__name__)

ENABLED = os.environ.get("VIBENODE_DURABLE_JOBS", "1") != "0"
TICK_SECONDS = 30.0
FIRST_TICK_SECONDS = 15.0
# A job that dies this soon after a launch has not run: its output (a
# traceback, a usage line) is not progress.
YOUNG_DEATH_SECONDS = 120.0
PUSH_EVENT = "job_needs_person"
REGISTRY_EVENT = "job_registry_error"


class JobSupervisor:
    def __init__(self, manager=None, *, proc_lister=None, launch=None, path=None,
                 clock=None) -> None:
        self._sm = manager
        self._procs = proc_lister or dj.list_command_lines
        self._launch = launch
        self._path = path
        self._now = clock or time.time
        self._stop_event = threading.Event()
        self._thread = None
        self._ticks = 0
        # (path, mtime_ns, size) of the last registry version reported corrupt.
        self._reported_corrupt = None

    # ── Lifecycle ─────────────────────────────────────────────────────

    def start(self) -> None:
        if self._thread is not None:
            return
        self._thread = threading.Thread(target=self._run, daemon=True, name="job-supervisor")
        self._thread.start()
        logger.info("JobSupervisor started (registry %s)", self._path or dj.registry_path())

    def stop(self) -> None:
        self._stop_event.set()
        if self._thread is not None:
            self._thread.join(timeout=2)
            self._thread = None

    def _run(self) -> None:
        if self._stop_event.wait(FIRST_TICK_SECONDS):
            return
        while True:
            try:
                self.tick()
            except Exception:
                logger.exception("JobSupervisor tick failed")
            if self._stop_event.wait(TICK_SECONDS):
                return

    # ── One tick (public for tests) ───────────────────────────────────

    def tick(self, dry_run: bool = False) -> list[dict]:
        """One pass.  Returns the decisions, ``[{id, action, reason}]``.
        ``dry_run`` decides without writing, launching or pushing."""
        path = self._path or dj.registry_path()
        after_restart = self._ticks == 0
        if not dry_run:
            self._ticks += 1
        try:
            if dry_run:
                reg = dj.load_registry(path)
                return self._decide_all(reg, self._now())
            with dj.edit_registry(path) as reg:
                decisions = self._decide_all(reg, self._now())
                self._apply_all(reg, decisions, after_restart)
                return decisions
        except dj.RegistryError as exc:
            self._report_corrupt(path, exc)
            return []

    def _decide_all(self, reg: dict, now: float) -> list[dict]:
        active = [r for r in reg["jobs"].values() if r.get("state") in ("running", "relaunched")]
        if not active:
            return []
        lines = self._procs()
        if lines is None:
            logger.warning("JobSupervisor: process list unavailable; no job is relaunched this tick")
        return [self.decide(r, lines, now) for r in active]

    @staticmethod
    def decide(rec: dict, lines, now: float) -> dict:
        """The decision for one job, from files and the process list only."""
        jid = rec["id"]
        progress = dj.parse_progress(rec)
        if dj.is_done(rec):
            return {"id": jid, "action": "finish", "reason": "done marker present", "progress": progress}
        alive = lines is not None and dj.is_running(rec, lines)
        if alive:
            return {"id": jid, "action": "alive", "reason": "process present", "progress": progress}
        if lines is None:
            return {"id": jid, "action": "unknown", "reason": "process list unavailable", "progress": progress}
        if rec.get("acknowledged"):
            # Leave it was pressed while the job still ran (Relaunch by a
            # person clears the flag).  It stopped after the person decided:
            # no notice, no relaunch.
            return {"id": jid, "action": "left", "progress": progress,
                    "reason": "stopped after it was left alone"}
        if dj.is_past_irreversible(rec):
            return {"id": jid, "action": "needs_person", "progress": progress,
                    "reason": "stopped past an irreversible step (%r matched); not relaunched"
                              % rec["irreversible_after"]["pattern"]}
        if not rec.get("resumable", True):
            return {"id": jid, "action": "fail", "progress": progress,
                    "reason": "stopped and marked non-resumable"}
        # Progress since the previous relaunch?
        if rec.get("relaunch_count", 0) == 0:
            progressed = True  # the first run counts as progress
        elif rec.get("progress") and progress and rec.get("progress_at_relaunch") is not None:
            progressed = (progress.get("n") or 0) > (rec.get("progress_at_relaunch") or 0)
        else:
            lived = now - float(rec.get("last_launch_at") or 0) > YOUNG_DEATH_SECONDS
            progressed = lived and dj.log_size(rec) > int(rec.get("log_size_at_relaunch", -1))
        streak = 0 if progressed else int(rec.get("relaunches_without_progress", 0))
        if streak >= dj.MAX_RELAUNCHES_WITHOUT_PROGRESS:
            return {"id": jid, "action": "needs_person", "progress": progress,
                    "reason": "died %d times in a row with no new output; not relaunched again" % streak}
        return {"id": jid, "action": "relaunch", "progress": progress, "streak": streak + 1,
                "reason": "process absent and not done" + ("" if progressed else " (no progress since the last relaunch)")}

    def _apply_all(self, reg: dict, decisions: list[dict], after_restart: bool) -> None:
        now = self._now()
        for d in decisions:
            rec = reg["jobs"][d["id"]]
            if d.get("progress") is not None:
                rec["progress_value"] = d["progress"]
            action = d["action"]
            if action == "alive":
                rec["last_seen_alive"] = now
            elif action == "finish":
                rec["state"] = "finished"
                rec["reason"] = ""
                dj.note(rec, "finished")
                logger.info("job %s finished", rec["id"])
            elif action == "needs_person":
                rec["state"] = "needs_person"
                rec["reason"] = d["reason"]
                rec["acknowledged"] = False
                dj.note(rec, d["reason"])
                logger.warning("job %s needs a person: %s", rec["id"], d["reason"])
                self._push(rec)
            elif action == "fail":
                rec["state"] = "failed"
                rec["reason"] = d["reason"]
                rec["acknowledged"] = False
                dj.note(rec, d["reason"])
                logger.error("job %s failed: %s", rec["id"], d["reason"])
                self._push(rec)
            elif action == "left":
                rec["state"] = "failed"
                rec["reason"] = d["reason"]
                dj.note(rec, d["reason"])
                logger.info("job %s %s", rec["id"], d["reason"])
            elif action == "relaunch":
                result = dj.apply_relaunch(rec, by="supervisor", launch=self._launch, streak=d["streak"])
                if result.get("ok"):
                    logger.warning("job %s relaunched (%s)", rec["id"], d["reason"])
                    self._announce(rec, after_restart)
                else:
                    logger.error("job %s relaunch failed: %s", rec["id"], result.get("error"))
                    self._push(rec)

    # ── Surfacing ─────────────────────────────────────────────────────

    def _push(self, rec: dict) -> None:
        push = getattr(self._sm, "_push_callback", None)
        if not push:
            return
        try:
            push(PUSH_EVENT, {
                "job_id": rec["id"], "name": rec.get("name", ""), "state": rec["state"],
                "reason": rec.get("reason", ""), "progress": rec.get("progress_value"),
                "session_id": rec.get("session_id", ""), "project": rec.get("project", ""),
            })
            rec["notified_at"] = self._now()
        except Exception:
            logger.exception("job push failed for %s", rec["id"])

    def _announce(self, rec: dict, after_restart: bool) -> None:
        """ONE system entry in the transcript of the session that launched the
        job, so it can tell the user in its own words if it matters."""
        sid = rec.get("session_id") or ""
        sm = self._sm
        if not sid or sm is None:
            return
        p = rec.get("progress_value") or {}
        where = p.get("text") or ""
        if not where:
            tail = dj.read_tail(dj.resolve_log(rec, rec.get("done_test")) or "", 2000)
            lines = [ln for ln in tail.splitlines() if ln.strip()]
            where = lines[-1][:160] if lines else "the start"
        text = "Durable job %r relaunched %s; resumed from %s." % (
            rec.get("name", rec["id"]), "after restart" if after_restart else "after it stopped", where)
        try:
            with sm._lock:
                info = sm._sessions.get(sid)
            if info is None:
                logger.info("job %s: session %s not live; no entry", rec["id"], sid)
                return
            from daemon.session_manager import LogEntry
            entry = LogEntry(kind="system", text=text)
            with info._lock:
                info.entries.append(entry)
                index = len(info.entries) - 1
            sm._emit_entry(sid, entry, index)
        except Exception:
            logger.exception("job announce failed for %s", rec["id"])

    def _report_corrupt(self, path, exc) -> None:
        try:
            st = os.stat(path)
            key = (str(path), st.st_mtime_ns, st.st_size)
        except OSError:
            key = (str(path), 0, 0)
        if key == self._reported_corrupt:
            return
        self._reported_corrupt = key
        logger.error("Durable Jobs registry unusable: %s (nothing relaunched; file left as is)", exc)
        push = getattr(self._sm, "_push_callback", None)
        if push:
            try:
                push(REGISTRY_EVENT, {"path": str(path), "error": str(exc)})
            except Exception:
                logger.exception("registry error push failed")
