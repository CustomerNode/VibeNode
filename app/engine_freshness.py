"""Is the Session Engine running the code on disk?  And restart it when idle.

Why this exists (2026-10-20)
----------------------------
The Session Engine (daemon, port 5051) keeps the code it started with until it
restarts, and restarting it ends running sessions, so it is rarely restarted.
On 2026-10-20 it had run since Oct 6.  Eleven commits to ``daemon/``, including
every session-recovery job, sat on disk without running, and nothing told the
user.  Fixes were "made" over and over while the failures went on.

``status()`` compares the engine's code record (``daemon/engine_code.py``,
``data/engine_code.json``) with the files on disk.  An engine too old to write
the record is judged by its process start time against ``daemon/*.py`` mtimes.
``static/js/engine-freshness.js`` shows a banner while code is outdated.

``arm_restart_when_idle()`` is the banner's button.  It waits until no session
is busy, then restarts the engine through the same ``/api/restart`` path as
System > Developer Tools > Restart Server > Session Engine.  "Busy" means any
of:

* a session that is starting, working, or waiting on a question/permission;
* a background command running under any session's CLI;
* anything written in a session's footprint (transcript, agents, task output)
  in the last ``QUIET_SECONDS``.

The user arms it with a click.  AI agents must never call it (CLAUDE.md:
no agent restarts the daemon).
"""

import glob
import json
import logging
import os
import threading
import time
import urllib.request
from pathlib import Path

log = logging.getLogger(__name__)

ROOT = Path(__file__).resolve().parents[1]
RECORD_PATH = ROOT / "data" / "engine_code.json"

# Watchdog files hot-reload themselves (daemon/health_monitor.py) about a
# minute after they settle.  Within this window a change to one of them is
# "being applied", not outdated.
HOT_RELOAD_FILES = frozenset({
    "daemon/health_monitor.py", "daemon/orphaned_workers.py",
    "daemon/silent_wait.py", "daemon/limit_watch.py",
})
HOT_RELOAD_GRACE_SECONDS = 300.0

# Restart-when-idle: how long everything must be quiet, and how often to look.
QUIET_SECONDS = 300.0
POLL_SECONDS = 15.0
# Second look before restarting: a session can start a turn between polls.
RECHECK_PAUSE_SECONDS = 3.0
_BUSY_STATES = frozenset({"starting", "working", "waiting"})


def _daemon_port() -> int:
    return int(os.environ.get("VIBENODE_DAEMON_PORT", "0")) or 5051


def _web_port() -> int:
    return (int(os.environ.get("VIBENODE_TEST_PORT", "0"))
            or int(os.environ.get("VIBENODE_WEB_PORT", "0")) or 5050)


def engine_process():
    """The psutil Process listening on the engine port, or None."""
    try:
        import psutil
    except ImportError:
        return None
    port = _daemon_port()
    try:
        for c in psutil.net_connections(kind="tcp"):
            if (c.status == psutil.CONN_LISTEN and c.laddr
                    and c.laddr.port == port and c.pid):
                return psutil.Process(c.pid)
    except Exception as exc:
        log.debug("engine_process lookup failed: %s", exc)
    return None


def _read_record(pid: int):
    """The engine's code record, if it was written by THIS engine process."""
    try:
        with open(RECORD_PATH, encoding="utf-8") as fh:
            record = json.load(fh)
    except (OSError, ValueError):
        return None
    if not isinstance(record, dict) or record.get("pid") != pid:
        return None
    loaded = record.get("loaded")
    return loaded if isinstance(loaded, dict) else None


def _engine_source_files() -> list:
    """Repo-relative paths of the engine's own package (``daemon/**.py``)."""
    out = []
    for path in (ROOT / "daemon").rglob("*.py"):
        if "__pycache__" in path.parts:
            continue
        out.append(path.relative_to(ROOT).as_posix())
    return out


def outdated_files(started_at: float, loaded, now: float = None) -> list:
    """Source files whose on-disk version is not the one the engine runs.

    ``loaded`` is the engine's record (``{path: mtime_ns}``) or None for an
    engine too old to write one; then any ``daemon/`` file modified after the
    engine started counts.
    """
    now = time.time() if now is None else now
    paths = set(_engine_source_files())
    if loaded:
        paths.update(loaded)
    out = []
    for rel in sorted(paths):
        try:
            mtime_ns = os.stat(ROOT / rel).st_mtime_ns
        except OSError:
            continue
        if loaded and rel in loaded:
            stale = mtime_ns != loaded[rel]
        else:
            stale = mtime_ns / 1e9 > started_at + 1.0
        if not stale:
            continue
        # A running engine new enough to write the record hot-reloads its
        # watchdog: give that a chance before calling it outdated.
        if (loaded and rel in HOT_RELOAD_FILES
                and now - mtime_ns / 1e9 < HOT_RELOAD_GRACE_SECONDS):
            continue
        out.append(rel)
    return out


def status() -> dict:
    """Snapshot for ``GET /api/engine/status``."""
    proc = engine_process()
    if proc is None:
        return {"ok": True, "running": False, "stale": False, "outdated": [],
                "restart_armed": _waiter.armed()}
    try:
        pid, started_at = proc.pid, proc.create_time()
    except Exception:
        return {"ok": True, "running": False, "stale": False, "outdated": [],
                "restart_armed": _waiter.armed()}
    loaded = _read_record(pid)
    outdated = outdated_files(started_at, loaded)
    return {
        "ok": True,
        "running": True,
        "pid": pid,
        "started_at": started_at,
        "has_record": loaded is not None,
        "stale": bool(outdated),
        "outdated": outdated,
        "restart_armed": _waiter.armed(),
    }


# ── Restart when idle ─────────────────────────────────────────────────────

def _transcript_for(session_id: str):
    hits = glob.glob(os.path.join(os.path.expanduser("~/.claude/projects"), "*",
                                  glob.escape(session_id) + ".jsonl"))
    return hits[0] if hits else None


def busy_reasons(session_manager, now: float = None) -> list:
    """Why restarting the engine now would cut work short ([] = safe)."""
    from daemon.silent_wait import footprint_last_write, live_commands
    now = time.time() if now is None else now
    reasons = []
    try:
        states = session_manager.get_all_states() or []
    except Exception as exc:
        return ["cannot read session states (%s)" % exc]
    for s in states:
        sid = str(s.get("session_id") or "")
        state = str(s.get("state") or "")
        label = (s.get("name") or sid[:8])
        if state in _BUSY_STATES:
            reasons.append("%s is %s" % (label, state))
            continue
        path = _transcript_for(sid) if sid else None
        if path:
            quiet = now - footprint_last_write(path)
            if quiet < QUIET_SECONDS:
                reasons.append("%s wrote output %ds ago" % (label, int(quiet)))
    proc = engine_process()
    if proc is not None:
        try:
            children = proc.children()
        except Exception:
            children = []
        for cli in children:
            live = live_commands(cli.pid)
            if live:
                reasons.append("background command running (%s)" % ", ".join(live[:3]))
    return reasons


def _request_engine_restart() -> None:
    """Restart via the same endpoint the Developer Tools button uses."""
    req = urllib.request.Request(
        "http://127.0.0.1:%d/api/restart" % _web_port(),
        data=json.dumps({"scope": "daemon"}).encode(),
        headers={"Content-Type": "application/json"}, method="POST",
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        resp.read()


class _RestartWaiter:
    """One background thread that restarts the engine once all is quiet."""

    def __init__(self):
        self._lock = threading.Lock()
        self._thread = None
        self._cancel = threading.Event()
        self.last_reasons: list = []

    def armed(self) -> bool:
        with self._lock:
            return self._thread is not None and self._thread.is_alive()

    def arm(self, session_manager, restart=_request_engine_restart) -> bool:
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return False
            self._cancel = threading.Event()
            self._thread = threading.Thread(
                target=self._run, args=(session_manager, restart, self._cancel),
                daemon=True, name="engine-restart-when-idle")
            self._thread.start()
            return True

    def disarm(self) -> None:
        with self._lock:
            self._cancel.set()

    def _run(self, session_manager, restart, cancel) -> None:
        log.warning("Engine restart armed: waiting until no session is busy")
        while not cancel.wait(POLL_SECONDS):
            try:
                reasons = busy_reasons(session_manager)
            except Exception as exc:
                reasons = ["busy check failed (%s)" % exc]
            self.last_reasons = reasons
            if reasons:
                continue
            # Look once more after a short pause: a session can start a turn
            # between two polls.
            if cancel.wait(RECHECK_PAUSE_SECONDS) or busy_reasons(session_manager):
                continue
            log.warning("Engine restart: nothing is busy, restarting the Session Engine now")
            try:
                restart()
            except Exception:
                log.exception("Engine restart request failed")
            return
        log.warning("Engine restart cancelled by the user")


_waiter = _RestartWaiter()


def arm_restart_when_idle(session_manager) -> bool:
    return _waiter.arm(session_manager)


def disarm_restart_when_idle() -> None:
    _waiter.disarm()


def waiting_on() -> list:
    return list(_waiter.last_reasons)
