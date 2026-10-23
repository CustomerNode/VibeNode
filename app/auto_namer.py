"""
Server-side naming of untitled sessions.

Auto-naming used to fire only from GUI paths (first send in ``app.js``,
``session_started`` in ``socket.js``, the kanban launch), all through
``POST /api/autonname/<sid>``.  A session a script or the SDK spawned never
went through a browser, so it never got a title: one project had 1,009
sessions and not one ``custom_title``.  Two pieces close that gap, both built
on the same helpers the route uses (``collect_title_messages`` for the input,
``smart_title`` for the title, ``_append_custom_title_if_changed`` +
``_save_name`` + ``session_renamed`` for the persist step).

1. **The background namer** (``AutoNamer``, one daemon thread started from
   ``create_app`` strictly inside ``if not testing``).  Every 60 s it looks at
   each project for transcripts modified since a per-project watermark
   (``data/auto_namer.json``, gitignored) that have no title and at least one
   assistant reply, and titles at most 3 per tick.  The watermark starts at
   "now" on first sight of a project, so the existing backlog is NOT named
   automatically: naming costs an LLM call each, and a thousand of them is a
   decision for the user (piece 2), not a side effect of a restart.

2. **The bulk namer** (``start_bulk``), behind ``POST /api/sessions/name-untitled``:
   every untitled session in one project, oldest first, the free heuristic
   first (``cheap_title``) and the LLM only when that is loose.  Progress goes
   out as ``naming_progress`` every 5 sessions and at the end.  One run per
   project at a time.

Both skip anything the user named (``_session_names.json``) and anything that
already carries a ``custom-title`` line, and never save the heuristic's
"Untitled Session" placeholder (the phantom rule from the autoname route).
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
from pathlib import Path

from . import config as _cfg
from .config import _encode_cwd, _SYSTEM_UTILITY_CWD, _VIBENODE_DIR, _sessions_dir
from .session_store import (
    _load_names,
    _save_name,
    _get_deleted_ids,
    _get_utility_ids,
    _get_remapped_ids,
)
from .sessions import all_sessions, load_session_summary
from .titling import cheap_title, collect_title_messages, smart_title

log = logging.getLogger(__name__)

STATE_PATH = _VIBENODE_DIR / "data" / "auto_namer.json"
TICK_SECONDS = 60
MAX_PER_TICK = 3
# A transcript must have been quiet this long before the namer touches it,
# so the GUI's own autoname (fired at first send) wins the race on sessions
# that do have a browser.
MIN_AGE_SECONDS = 30
PROGRESS_EVERY = 5


# ---------------------------------------------------------------------------
# Shared pieces
# ---------------------------------------------------------------------------

def _has_assistant_reply(path: Path) -> bool:
    """True when the transcript holds at least one ``assistant`` entry."""
    try:
        with open(path, "rb") as f:
            for raw in f:
                if b'"assistant"' not in raw:
                    continue
                try:
                    obj = json.loads(raw)
                except Exception:
                    continue
                if obj.get("type") == "assistant":
                    return True
    except Exception:
        return False
    return False


def untitled_sessions(project: str) -> list:
    """Nameable untitled sessions of *project*, oldest first.

    Untitled: no ``custom_title`` (neither a user/auto name in
    ``_session_names.json`` nor a ``custom-title`` line).  Nameable: it has at
    least one message; a transcript with no turns would only ever get the
    "Empty Session (n)" placeholder and is left alone.
    """
    rows = all_sessions(summary_only=True, project=project)
    rows = [s for s in rows
            if not s.get("custom_title") and (s.get("message_count") or 0) > 0]
    rows.sort(key=lambda s: (s.get("sort_ts") or 0, s.get("effective_ts") or 0))
    return rows


def count_untitled(project: str) -> dict:
    rows = all_sessions(summary_only=True, project=project)
    untitled = [s for s in rows if not s.get("custom_title")]
    nameable = [s for s in untitled if (s.get("message_count") or 0) > 0]
    return {"total": len(rows), "untitled": len(untitled), "nameable": len(nameable)}


def name_transcript(project: str, session_id: str, *, cheap_first: bool = False,
                    titler=None, used_titles: set | None = None) -> dict:
    """Title one on-disk session exactly the way ``POST /api/autonname`` does.

    Returns ``{"ok": True, "title": ..., "cheap": bool}`` when a title was
    persisted and broadcast, or ``{"ok": False, "skipped": True, "reason": ...}``.
    *titler* replaces ``smart_title`` (tests).  Must run inside an app
    context when the daemon title path is wanted; without one ``smart_title``
    falls through to the API / CLI / heuristic on its own.
    """
    # Lazy: sessions_api imports this module for the bulk routes.
    from .routes.sessions_api import (
        _append_custom_title_if_changed,
        _emit_session_renamed,
        _latest_custom_title_in_jsonl,
    )

    path = _sessions_dir(project) / f"{session_id}.jsonl"
    if not path.exists():
        return {"ok": False, "skipped": True, "reason": "not found"}
    if _load_names(project).get(session_id):
        return {"ok": False, "skipped": True, "reason": "User-set name preserved"}
    if _latest_custom_title_in_jsonl(path):
        return {"ok": False, "skipped": True, "reason": "already titled"}

    messages = collect_title_messages(path)
    if not messages:
        return {"ok": False, "skipped": True, "reason": "no user text"}

    title = None
    cheap = False
    if cheap_first:
        title = cheap_title(messages)
        # Scripted sessions share an opening line, so the heuristic hands
        # dozens of them the same title (measured: 40 sessions of one project all
        # "You are the <role> agent").  A duplicate title is no
        # better than none: send the repeat to the LLM, which reads further.
        if title and used_titles is not None and title.lower() in used_titles:
            title = None
        cheap = bool(title)
    if not title:
        title = (titler or smart_title)(messages)
    if not title or title == "Untitled Session":
        return {"ok": False, "skipped": True, "reason": "no usable title"}

    _append_custom_title_if_changed(path, title, session_id)
    _save_name(session_id, title, project)
    _emit_session_renamed(session_id, title, project)
    if used_titles is not None:
        used_titles.add(title.lower())
    return {"ok": True, "title": title, "cheap": cheap}


def _project_dirs(root: Path) -> list:
    """Encoded project directory names under *root*, minus the subagents
    trees and the system utility project (title-generation transcripts)."""
    system = _encode_cwd(_SYSTEM_UTILITY_CWD)
    out = []
    try:
        with os.scandir(root) as it:
            for de in it:
                n = de.name
                if not de.is_dir() or n.startswith("subagents") or n.startswith("_") or n == system:
                    continue
                out.append(n)
    except OSError:
        return []
    out.sort()
    return out


# ---------------------------------------------------------------------------
# 1. Background namer
# ---------------------------------------------------------------------------

class AutoNamer:
    """Names untitled sessions that change after the per-project watermark.

    Watermark semantics: ``watermarks[project]`` is the mtime of the newest
    transcript this namer has looked at.  A project seen for the first time
    gets ``now`` and nothing older is ever considered (the backlog rule).  A
    tick examines candidates with ``watermark < mtime <= now - MIN_AGE`` in
    mtime order, stops BEFORE examining a candidate once ``max_per_tick``
    naming attempts were made, and moves the watermark to the mtime of the
    last candidate examined.  So a session the cap deferred is picked up
    next tick, and a session skipped because it had no assistant reply yet
    comes back on its own: the reply bumps its mtime past the watermark.
    """

    def __init__(self, app=None, *, state_path: Path | None = None,
                 projects_root=None, tick_seconds: float = TICK_SECONDS,
                 max_per_tick: int = MAX_PER_TICK,
                 min_age_seconds: float = MIN_AGE_SECONDS,
                 titler=None, clock=time.time):
        self.app = app
        self.state_path = Path(state_path) if state_path else STATE_PATH
        # Read at call time so a monkeypatched config._CLAUDE_PROJECTS applies.
        self._projects_root = projects_root
        self.tick_seconds = tick_seconds
        self.max_per_tick = max_per_tick
        self.min_age_seconds = min_age_seconds
        self.titler = titler
        self.clock = clock
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self.watermarks: dict = {}
        self._load_state()

    # -- state ---------------------------------------------------------------

    def _root(self) -> Path:
        r = self._projects_root
        if callable(r):
            r = r()
        return Path(r) if r else _cfg._CLAUDE_PROJECTS

    def _load_state(self) -> None:
        try:
            data = json.loads(self.state_path.read_text(encoding="utf-8"))
            wm = data.get("watermarks") if isinstance(data, dict) else None
            if isinstance(wm, dict):
                self.watermarks = {k: float(v) for k, v in wm.items()
                                   if isinstance(v, (int, float))}
        except Exception:
            self.watermarks = {}

    def _save_state(self) -> None:
        try:
            self.state_path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.state_path.with_suffix(".json.tmp")
            tmp.write_text(json.dumps({"version": 1, "watermarks": self.watermarks},
                                      indent=2), encoding="utf-8")
            os.replace(tmp, self.state_path)
        except Exception as e:
            log.warning("auto_namer: could not save state to %s: %s", self.state_path, e)

    # -- one tick ------------------------------------------------------------

    def tick(self) -> list:
        """Run one pass over every project.  Returns the naming results."""
        now = self.clock()
        results = []
        changed = False
        for project in _project_dirs(self._root()):
            wm = self.watermarks.get(project)
            if wm is None:
                # First sight of this project: the backlog is not ours.
                self.watermarks[project] = now
                changed = True
                continue
            new_wm, project_results = self._tick_project(project, wm, now)
            results.extend(project_results)
            if new_wm != wm:
                self.watermarks[project] = new_wm
                changed = True
        if changed:
            self._save_state()
        return results

    def _candidates(self, project: str, wm: float, now: float) -> list:
        sdir = self._root() / project
        cutoff = now - self.min_age_seconds
        hidden = _get_deleted_ids(project) | _get_utility_ids(project) | _get_remapped_ids(project)
        out = []
        try:
            with os.scandir(sdir) as it:
                for de in it:
                    n = de.name
                    if not n.endswith(".jsonl") or n.startswith("_"):
                        continue
                    if n[:-6] in hidden:
                        continue
                    try:
                        if not de.is_file():
                            continue
                        st = de.stat()
                    except OSError:
                        continue
                    if wm < st.st_mtime <= cutoff:
                        out.append((st.st_mtime, sdir / n, st))
        except OSError:
            return []
        out.sort(key=lambda t: t[0])
        return out

    def _tick_project(self, project: str, wm: float, now: float):
        attempts = 0
        results = []
        for mtime, path, st in self._candidates(project, wm, now):
            if attempts >= self.max_per_tick:
                break  # the watermark stays below this one; next tick
            wm = mtime
            summary = load_session_summary(path, st)
            if summary.get("custom_title"):
                continue
            if not _has_assistant_reply(path):
                continue
            attempts += 1
            r = self._name(project, path.stem)
            r.update({"project": project, "session_id": path.stem})
            results.append(r)
            if r.get("ok"):
                log.info("auto_namer: %s/%s -> %r", project, path.stem, r.get("title"))
        return wm, results

    def _name(self, project: str, session_id: str) -> dict:
        try:
            if self.app is not None:
                with self.app.app_context():
                    return name_transcript(project, session_id, titler=self.titler)
            return name_transcript(project, session_id, titler=self.titler)
        except Exception as e:
            log.warning("auto_namer: naming %s/%s failed: %s", project, session_id, e)
            return {"ok": False, "skipped": True, "reason": f"error: {e}"}

    # -- thread --------------------------------------------------------------

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                self.tick()
            except Exception:
                log.exception("auto_namer: tick failed")
            self._stop.wait(self.tick_seconds)

    def start(self) -> threading.Thread:
        if self._thread and self._thread.is_alive():
            return self._thread
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="auto-namer", daemon=True)
        self._thread.start()
        return self._thread

    def stop(self) -> None:
        self._stop.set()


_namer: AutoNamer | None = None


def start_auto_namer(app) -> AutoNamer | None:
    """Create and start the process-wide namer.  ``VIBENODE_AUTO_NAMER=0``
    disables it.  Called from ``create_app`` inside ``if not testing``."""
    global _namer
    if os.environ.get("VIBENODE_AUTO_NAMER", "1") == "0":
        log.info("auto_namer: disabled by VIBENODE_AUTO_NAMER=0")
        return None
    if _namer is None:
        _namer = AutoNamer(app)
    _namer.start()
    return _namer


# ---------------------------------------------------------------------------
# 2. Bulk namer
# ---------------------------------------------------------------------------

class BulkJob:
    def __init__(self, project: str, limit: int | None):
        self.project = project
        self.limit = limit
        self.total = 0
        self.done = 0
        self.named = 0
        self.skipped = 0
        self.failed = 0
        self.cheap = 0
        self.current_title = ""
        self.running = True
        self.started_at = time.time()
        self.finished_at: float | None = None
        self.error = ""

    def to_dict(self) -> dict:
        return {
            "project": self.project,
            "running": self.running,
            "limit": self.limit,
            "total": self.total,
            "done": self.done,
            "named": self.named,
            "skipped": self.skipped,
            "failed": self.failed,
            "cheap": self.cheap,
            "current_title": self.current_title,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "error": self.error,
        }

    def progress_payload(self, finished: bool = False) -> dict:
        return {
            "project": self.project,
            "done": self.done,
            "total": self.total,
            "current_title": self.current_title,
            "named": self.named,
            "skipped": self.skipped,
            "failed": self.failed,
            "finished": finished,
        }


_bulk_jobs: dict = {}
_bulk_lock = threading.Lock()


def _default_emit(payload: dict) -> None:
    try:
        from . import socketio
        socketio.emit("naming_progress", payload)
    except Exception as e:  # SocketIO not wired (unit tests): non-fatal
        log.debug("naming_progress emit failed: %s", e)


def bulk_status(project: str) -> dict:
    with _bulk_lock:
        job = _bulk_jobs.get(project)
    if job is None:
        return {"project": project, "running": False, "total": 0, "done": 0,
                "named": 0, "skipped": 0, "failed": 0, "cheap": 0,
                "current_title": "", "started_at": None, "finished_at": None,
                "limit": None, "error": ""}
    return job.to_dict()


def start_bulk(app, project: str, limit: int | None = None, *, titler=None,
               emit=None, threaded: bool = True):
    """Start naming every untitled session of *project*.

    Returns ``(status_dict, started)``.  ``started`` is False when a run for
    this project is still in progress; the status describes that run.
    """
    with _bulk_lock:
        existing = _bulk_jobs.get(project)
        if existing is not None and existing.running:
            return existing.to_dict(), False
        job = BulkJob(project, limit)
        _bulk_jobs[project] = job
    if threaded:
        t = threading.Thread(target=_run_bulk, args=(app, job, titler, emit),
                             name=f"bulk-namer-{project[:24]}", daemon=True)
        t.start()
    else:
        _run_bulk(app, job, titler, emit)
    return job.to_dict(), True


def _run_bulk(app, job: BulkJob, titler, emit) -> None:
    emit = emit or _default_emit
    try:
        ctx = app.app_context() if app is not None else None
        if ctx is not None:
            ctx.push()
        try:
            rows = untitled_sessions(job.project)
            if job.limit:
                rows = rows[:job.limit]
            job.total = len(rows)
            emit(job.progress_payload())
            # Seed with the project's existing titles so the cheap path never
            # duplicates one either.
            used = {str(t).lower() for t in _load_names(job.project).values() if t}
            for row in rows:
                sid = row["id"]
                try:
                    r = name_transcript(job.project, sid, cheap_first=True,
                                        titler=titler, used_titles=used)
                except Exception as e:
                    log.warning("bulk namer: %s/%s failed: %s", job.project, sid, e)
                    r = {"ok": False, "error": str(e)}
                job.done += 1
                if r.get("ok"):
                    job.named += 1
                    job.current_title = r.get("title", "")
                    if r.get("cheap"):
                        job.cheap += 1
                elif r.get("skipped"):
                    job.skipped += 1
                else:
                    job.failed += 1
                if job.done % PROGRESS_EVERY == 0 and job.done != job.total:
                    emit(job.progress_payload())
        finally:
            if ctx is not None:
                ctx.pop()
    except Exception as e:
        job.error = str(e)
        log.exception("bulk namer: run for %s aborted", job.project)
    finally:
        job.running = False
        job.finished_at = time.time()
        emit(job.progress_payload(finished=True))
        log.info("bulk namer: %s done=%d named=%d (cheap %d) skipped=%d failed=%d",
                 job.project, job.done, job.named, job.cheap, job.skipped, job.failed)
