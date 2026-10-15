"""Durable Jobs: the registry of long-running work that must survive a restart.

Spec: docs/plans/durable-jobs-spec.md (Don, 2026-10-15).  A Claude session can
launch work that runs for hours.  run-detached kept it alive when the session
closed; nothing kept it alive when the computer restarted, and on 2026-10-15
three jobs stayed dead for eight hours because nothing knew they existed.

This module is the shared library behind every entry point:

* ``vibenode.py job run`` (the CLI the run-detached skill calls),
* ``POST /api/jobs/run`` and the other routes in ``app/routes/jobs_api.py``,
* ``daemon/job_supervisor.py``, the thread beside the health monitor that
  relaunches dead jobs.

**Registration IS the launch.**  ``run_job()`` launches the command detached
on the current OS and writes its record in the same step; there is no other
way to launch a long job through the skill, so nothing is ever unregistered.
It refuses only a missing done test.

**The registry** is ``data/jobs.json`` next to the code (Don: on the machine,
not in the cloud; a registry that needs the network cannot be read at boot).
One record per job; see ``new_record`` for the fields.  States: ``running``,
``finished``, ``needs_person``, ``relaunched``, ``failed``.  A file that
cannot be parsed is reported, never rewritten (``RegistryError``).

**Evaluation** is file-based and exact.  The done test, the irreversible
marker and the progress pattern are each a regex over a log file: the job's
own log by default, or the newest file matching a glob next to it (jobs that
write their own log, like the authorship read).  ``last_line`` restricts a
pattern to the file's last non-empty line (a state file whose history may
contain the words).

Nothing here touches session state.  The supervisor reads logs and process
lists and only writes this registry and launches processes.
"""

from __future__ import annotations

import glob as _glob
import json
import logging
import os
import re
import subprocess
import sys
import threading
import time
import uuid
from contextlib import contextmanager
from pathlib import Path

logger = logging.getLogger(__name__)

ROOT = Path(__file__).resolve().parents[1]
STATES = ("running", "finished", "needs_person", "relaunched", "failed")
VERSION = 1
# How many relaunches a job gets without new output between them.  After the
# third one dies with nothing new in its log, the job needs a person.
MAX_RELAUNCHES_WITHOUT_PROGRESS = 3
# A log is read from its tail only; a marker older than this is not "the end".
TAIL_BYTES = 256 * 1024

_local = threading.local()


class RegistryError(Exception):
    """The registry file exists but cannot be used.  It is never rewritten."""


class JobError(ValueError):
    """A job cannot be registered (bad or missing fields)."""


# ── Paths ────────────────────────────────────────────────────────────────

def registry_path() -> Path:
    override = os.environ.get("VIBENODE_JOBS_FILE")
    if override:
        return Path(override)
    return ROOT / "data" / "jobs.json"


def jobs_dir() -> Path:
    """Where launch logs and launcher scripts go by default."""
    return registry_path().parent / "jobs"


# ── Registry file ────────────────────────────────────────────────────────

@contextmanager
def _locked(path: Path):
    """Cross-process lock on ``<registry>.lock`` (the CLI, the web server and
    the daemon all write).  Re-entrant within a thread."""
    depth = getattr(_local, "depth", 0)
    if depth:
        _local.depth = depth + 1
        try:
            yield
        finally:
            _local.depth -= 1
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    lock_path = path.with_suffix(path.suffix + ".lock")
    fh = open(lock_path, "a+b")
    try:
        deadline = time.time() + 10.0
        while True:
            try:
                if sys.platform == "win32":
                    import msvcrt
                    fh.seek(0)
                    msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except OSError:
                if time.time() > deadline:
                    raise RegistryError(f"could not lock {lock_path} within 10s")
                time.sleep(0.05)
        _local.depth = 1
        try:
            yield
        finally:
            _local.depth = 0
            try:
                if sys.platform == "win32":
                    import msvcrt
                    fh.seek(0)
                    msvcrt.locking(fh.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
            except OSError:
                pass
    finally:
        fh.close()


def load_registry(path: Path | None = None) -> dict:
    """``{"version": 1, "jobs": {id: record}}``.  A missing file is empty.

    Raises ``RegistryError`` for a file that is not valid, with the reason;
    the file is left exactly as it was."""
    path = path or registry_path()
    try:
        raw = path.read_bytes()
    except FileNotFoundError:
        return {"version": VERSION, "jobs": {}}
    except OSError as exc:
        raise RegistryError(f"{path}: {exc}") from exc
    if not raw.strip():
        return {"version": VERSION, "jobs": {}}
    try:
        data = json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeDecodeError) as exc:
        raise RegistryError(f"{path} is not valid JSON ({exc}); left untouched")
    if not isinstance(data, dict) or not isinstance(data.get("jobs"), dict):
        raise RegistryError(f"{path} does not hold a jobs registry; left untouched")
    for jid, rec in data["jobs"].items():
        if not isinstance(rec, dict) or rec.get("state") not in STATES:
            raise RegistryError(f"{path}: job {jid!r} has no valid state; left untouched")
    return data


def save_registry(data: dict, path: Path | None = None) -> None:
    """Atomic write (temp file + replace)."""
    path = path or registry_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + f".{os.getpid()}.tmp")
    tmp.write_text(json.dumps(data, indent=1, sort_keys=True), encoding="utf-8")
    os.replace(tmp, path)


@contextmanager
def edit_registry(path: Path | None = None):
    """``with edit_registry() as reg:`` loads under the lock and saves on exit."""
    path = path or registry_path()
    with _locked(path):
        data = load_registry(path)
        yield data
        save_registry(data, path)


def get_job(job_id: str, path: Path | None = None) -> dict | None:
    return load_registry(path)["jobs"].get(job_id)


def list_jobs(path: Path | None = None) -> list[dict]:
    jobs = list(load_registry(path)["jobs"].values())
    jobs.sort(key=lambda r: r.get("registered_at", 0), reverse=True)
    return jobs


# ── Records ──────────────────────────────────────────────────────────────

_ID_JUNK = re.compile(r"[^A-Za-z0-9._-]+")


def _slug(name: str) -> str:
    return _ID_JUNK.sub("-", (name or "job").strip()).strip("-.")[:40] or "job"


def _pattern_spec(value, *, what: str, last_line: bool = False, file: str = "") -> dict | None:
    """Normalise a done/irreversible/progress spec to
    ``{"pattern": regex, "file": glob-or-empty, "last_line": bool}``."""
    if value is None or value == "" or value == {}:
        return None
    if isinstance(value, str):
        value = {"pattern": value}
    if not isinstance(value, dict):
        raise JobError(f"{what}: a pattern (regex) or an object with one is required")
    if not str(value.get("pattern") or "").strip():
        return None  # an empty spec is no spec (a missing done test is refused by the caller)
    pattern = str(value["pattern"])
    try:
        re.compile(pattern)
    except re.error as exc:
        raise JobError(f"{what}: bad pattern {pattern!r}: {exc}")
    return {
        "pattern": pattern,
        "file": str(value.get("file") or file or ""),
        "last_line": bool(value.get("last_line", last_line)),
    }


def new_record(*, name: str, command: str, cwd: str, done_test, job_id: str = "",
               project: str = "", session_id: str = "", env_file: str = "",
               running_match: str = "", progress=None, irreversible_after=None,
               log_path: str = "", resumable: bool = True) -> dict:
    """Build and validate a registry record.  Nothing is launched here.

    Refuses a missing done test (spec rule 5: no launch of a job whose done
    test is missing) and a missing command or folder."""
    command = str(command or "").strip()
    if not command:
        raise JobError("a command is required")
    cwd = str(cwd or "").strip()
    if not cwd or not Path(cwd).is_dir():
        raise JobError(f"cwd must be an existing folder (got {cwd!r})")
    done = _pattern_spec(done_test, what="done test")
    if done is None:
        raise JobError("a done test is required: how will anyone know this job finished?")
    env_file = str(env_file or "")
    if env_file and not Path(env_file).is_file():
        raise JobError(f"env_file not found: {env_file}")
    job_id = job_id or f"{_slug(name)}-{int(time.time())}-{uuid.uuid4().hex[:4]}"
    if _ID_JUNK.search(job_id):
        raise JobError(f"bad job id {job_id!r}")
    return {
        "id": job_id,
        "name": str(name or job_id),
        "project": str(project or ""),
        "session_id": str(session_id or ""),
        "command": command,
        "cwd": cwd,
        "env_file": env_file,
        "running_match": str(running_match or "").strip() or command,
        "done_test": done,
        "progress": _pattern_spec(progress, what="progress"),
        "irreversible_after": _pattern_spec(irreversible_after, what="irreversible_after"),
        "resumable": bool(resumable),
        # ``{ts}`` is replaced at each launch, so a relaunch gets its own file
        # and a glob like ``name-[0-9]*.log`` finds the newest.
        "log_path": str(log_path or str(jobs_dir() / (job_id + "-{ts}.log"))),
        "last_log": "",
        "registered_at": time.time(),
        "last_seen_alive": 0.0,
        "last_launch_at": 0.0,
        "relaunch_count": 0,
        "relaunches_without_progress": 0,
        # Size of the log when the last relaunch was made: "progress" means
        # the log grew past this before the job died again.
        "log_size_at_relaunch": -1,
        "state": "running",
        "reason": "",
        "progress_value": None,
        "acknowledged": False,
        "history": [],
    }


def note(rec: dict, text: str) -> None:
    """One line of history per decision (kept short; newest last)."""
    hist = rec.setdefault("history", [])
    hist.append({"ts": time.time(), "text": text})
    del hist[:-30]


# ── Launching ────────────────────────────────────────────────────────────

def _read_env_file(path: str) -> dict:
    env = {}
    if not path:
        return env
    for line in Path(path).read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        env[k.strip()] = v.strip().strip('"').strip("'")
    return env


def expand_log_path(rec: dict, ts: int | None = None) -> str:
    ts = int(ts if ts is not None else time.time())
    return os.path.expandvars(str(rec["log_path"]).replace("{ts}", str(ts)))


def launch_detached(command: str, cwd: str, log_path: str, env: dict | None = None,
                    launcher_dir: Path | None = None) -> dict:
    """Start ``command`` in ``cwd`` detached from this process on the current
    OS, with stdout+stderr appended to ``log_path``.  Returns
    ``{"ok", "pid", "launcher", "error"}``.  ``pid`` is the detached wrapper's
    pid (the real job is its child); the job is recognised later by its
    command line, not this pid.

    Windows: the proven hidden-console pattern from run-detached and
    ``reviver.py``.  ``wscript.exe`` runs a one-line .vbs that starts
    ``cmd /c <command>`` with window style 0 (SW_HIDE).  Task Scheduler is not
    used: it refused launches on 2026-10-13 (0x80070420), and a detached
    wscript survives the session that launched it just the same.

    macOS/Linux: ``nohup sh -c '...' &`` in a new session, as launch.sh and
    the restart path in app/routes/main.py do."""
    full_env = dict(os.environ)
    full_env.update(env or {})
    Path(log_path).parent.mkdir(parents=True, exist_ok=True)
    try:
        if sys.platform == "win32":
            launcher_dir = launcher_dir or Path(log_path).parent
            launcher_dir.mkdir(parents=True, exist_ok=True)
            vbs = launcher_dir / (Path(log_path).stem + ".vbs")
            inner = f'cmd /c cd /d "{cwd}" && {command} >> "{log_path}" 2>&1'
            body = 'CreateObject("WScript.Shell").Run "' + inner.replace('"', '""') + '", 0, False'
            vbs.write_text(body + "\r\n", encoding="ascii", errors="replace")
            flags = (subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP
                     | getattr(subprocess, "CREATE_NO_WINDOW", 0))
            proc = subprocess.Popen(["wscript.exe", str(vbs)], cwd=cwd, env=full_env,
                                    creationflags=flags, close_fds=True,
                                    stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                    stderr=subprocess.DEVNULL)
            return {"ok": True, "pid": proc.pid, "launcher": str(vbs), "error": ""}
        q = lambda s: "'" + str(s).replace("'", "'\\''") + "'"  # noqa: E731
        script = f"cd {q(cwd)} && {command} >> {q(log_path)} 2>&1"
        proc = subprocess.Popen(["nohup", "sh", "-c", script], cwd=cwd, env=full_env,
                                start_new_session=True, close_fds=True,
                                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                stderr=subprocess.DEVNULL)
        return {"ok": True, "pid": proc.pid, "launcher": "", "error": ""}
    except Exception as exc:
        return {"ok": False, "pid": 0, "launcher": "", "error": str(exc)}


def _launch_record(rec: dict, launch=None) -> dict:
    """Launch ``rec``'s command and update its bookkeeping (no registry I/O).
    ``launch`` defaults to ``launch_detached`` at call time (tests patch it)."""
    launch = launch or launch_detached
    now = time.time()
    log_path = expand_log_path(rec, now)
    try:
        env = _read_env_file(rec.get("env_file") or "")
    except OSError as exc:
        return {"ok": False, "error": f"env_file: {exc}"}
    result = launch(rec["command"], rec["cwd"], log_path, env)
    rec["last_launch_at"] = now
    if result.get("ok"):
        rec["last_log"] = log_path
        rec["last_seen_alive"] = now
    return result


def run_job(*, launch=None, path: Path | None = None, **fields) -> dict:
    """Register AND launch in one step (the only way a long job is launched).

    Returns the record.  Raises ``JobError`` for a bad request.  A launch that
    fails is still registered, as ``failed``, so it is seen."""
    rec = new_record(**fields)
    with edit_registry(path) as reg:
        if rec["id"] in reg["jobs"]:
            raise JobError(f"job id {rec['id']!r} already registered")
        result = _launch_record(rec, launch)
        if result.get("ok"):
            rec["state"] = "running"
            note(rec, "launched")
        else:
            rec["state"] = "failed"
            rec["reason"] = f"launch failed: {result.get('error')}"
            note(rec, rec["reason"])
        reg["jobs"][rec["id"]] = rec
    return rec


def register_existing(*, path: Path | None = None, **fields) -> dict:
    """Register a job that is ALREADY running (migration of the laptop patch;
    spec: register without restarting).  Validated like any other record."""
    rec = new_record(**fields)
    now = time.time()
    rec["last_seen_alive"] = now
    rec["last_launch_at"] = now
    note(rec, "registered while already running (migrated)")
    with edit_registry(path) as reg:
        reg["jobs"][rec["id"]] = rec
    return rec


def relaunch_job(job_id: str, *, by: str = "supervisor", launch=None,
                 path: Path | None = None) -> dict:
    """Launch a registered job again.  ``by="user"`` (the Relaunch button)
    resets the no-progress count and the acknowledgement."""
    with edit_registry(path) as reg:
        rec = reg["jobs"].get(job_id)
        if rec is None:
            raise JobError(f"no job {job_id!r}")
        if by == "user":
            rec["relaunches_without_progress"] = 0
            rec["acknowledged"] = False
        result = apply_relaunch(rec, by=by, launch=launch)
        return dict(rec), result


def apply_relaunch(rec: dict, *, by: str, launch=None, streak: int | None = None) -> dict:
    """Relaunch ``rec`` in place (no registry I/O; the caller holds the
    record).  ``streak`` is the new count of relaunches without progress
    (the supervisor's bookkeeping); the launch marks where the log and the
    progress stood, so the next death can tell whether anything moved."""
    rec["log_size_at_relaunch"] = log_size(rec)
    p = parse_progress(rec)
    rec["progress_at_relaunch"] = p["n"] if p else None
    result = _launch_record(rec, launch)
    rec["relaunch_count"] = int(rec.get("relaunch_count", 0)) + 1
    if streak is not None:
        rec["relaunches_without_progress"] = streak
    if result.get("ok"):
        rec["state"] = "relaunched"
        rec["reason"] = ""
        note(rec, f"relaunched by {by} (#{rec['relaunch_count']})")
    else:
        rec["state"] = "failed"
        rec["reason"] = f"relaunch failed: {result.get('error')}"
        note(rec, rec["reason"])
    return result


def leave_job(job_id: str, path: Path | None = None) -> dict:
    """The Leave-it button: the person has decided; stop asking."""
    with edit_registry(path) as reg:
        rec = reg["jobs"].get(job_id)
        if rec is None:
            raise JobError(f"no job {job_id!r}")
        rec["acknowledged"] = True
        note(rec, "left alone by the user")
        return dict(rec)


# ── Observation ──────────────────────────────────────────────────────────

def list_command_lines() -> list[str] | None:
    """Command lines of every process, or None when they cannot be listed."""
    try:
        if sys.platform == "win32":
            out = subprocess.run(
                ["powershell", "-NoProfile", "-NonInteractive", "-Command",
                 "Get-CimInstance Win32_Process | ForEach-Object { $_.CommandLine }"],
                capture_output=True, text=True, timeout=40,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            ).stdout
        else:
            out = subprocess.run(["ps", "-eo", "args"], capture_output=True,
                                 text=True, timeout=20).stdout
    except Exception as exc:
        logger.warning("could not list processes: %s", exc)
        return None
    return [ln for ln in out.splitlines() if ln.strip()]


def _norm(s: str) -> str:
    return " ".join(str(s).split()).lower()


def is_running(rec: dict, command_lines: list[str]) -> bool:
    """A process whose command line contains ``running_match`` (case- and
    whitespace-insensitive substring; ``*`` is a wildcard).  The job's own
    launcher wrapper (``cmd /c ...``) matches too, which is fine: it lives
    exactly as long as the job.  A process that is only LOOKING at the job
    (this scan, a ``vibenode job`` command) does not carry the command."""
    pat = _norm(rec.get("running_match") or rec.get("command") or "")
    if not pat:
        return False
    if "*" in pat:
        rx = re.compile(".*".join(re.escape(p) for p in pat.split("*")))
        test = lambda s: rx.search(s) is not None  # noqa: E731
    else:
        test = lambda s: pat in s  # noqa: E731
    for line in command_lines:
        n = _norm(line)
        if "vibenode.py job" in n or "jobs_api" in n or "durable_jobs" in n:
            continue
        if test(n):
            return True
    return False


def resolve_log(rec: dict, spec: dict | None) -> str:
    """The file a pattern reads: the newest match of ``spec.file`` (a glob,
    relative to the job's log folder unless absolute), else the last launch
    log, else the expanded log_path with no timestamp."""
    base = Path(os.path.expandvars(str(rec.get("last_log") or expand_log_path(rec, 0)))).parent
    pattern = (spec or {}).get("file") or ""
    if pattern:
        pattern = os.path.expandvars(pattern)
        if not os.path.isabs(pattern):
            pattern = str(base / pattern)
        if any(ch in pattern for ch in "*?["):
            matches = _glob.glob(pattern)
            if not matches:
                return ""
            return max(matches, key=lambda p: os.path.getmtime(p))
        return pattern
    if rec.get("last_log"):
        return os.path.expandvars(rec["last_log"])
    tmpl = str(rec.get("log_path") or "")
    if "{ts}" in tmpl:
        matches = _glob.glob(os.path.expandvars(tmpl.replace("{ts}", "*")))
        return max(matches, key=lambda p: os.path.getmtime(p)) if matches else ""
    return os.path.expandvars(tmpl)


def read_tail(path: str, limit: int = TAIL_BYTES) -> str:
    try:
        size = os.path.getsize(path)
        with open(path, "rb") as fh:
            if size > limit:
                fh.seek(size - limit)
            return fh.read().decode("utf-8", "replace")
    except OSError:
        return ""


def match_pattern(rec: dict, spec: dict | None):
    """The regex match for ``spec`` in its file (tail), or None."""
    if not spec:
        return None
    path = resolve_log(rec, spec)
    if not path:
        return None
    # Windows text files end lines with CRLF; a ``$`` anchor must still match.
    text = read_tail(path).replace("\r\n", "\n").replace("\r", "\n")
    if not text:
        return None
    rx = re.compile(spec["pattern"], re.MULTILINE)
    if spec.get("last_line"):
        lines = [ln for ln in text.splitlines() if ln.strip()]
        return rx.search(lines[-1]) if lines else None
    last = None
    for m in rx.finditer(text):
        last = m
    return last


def is_done(rec: dict) -> bool:
    return match_pattern(rec, rec.get("done_test")) is not None


def is_past_irreversible(rec: dict) -> bool:
    return match_pattern(rec, rec.get("irreversible_after")) is not None


def parse_progress(rec: dict) -> dict | None:
    """``{"n", "m", "pct", "text"}`` from the newest progress match, or None.
    A pattern with two groups gives n of m; one group gives a bare number."""
    m = match_pattern(rec, rec.get("progress"))
    if m is None:
        return None
    groups = [g for g in m.groups() if g is not None]
    out = {"text": m.group(0).strip(), "n": None, "m": None, "pct": None}
    try:
        if len(groups) >= 2:
            n, total = float(groups[0]), float(groups[1])
            out.update(n=n, m=total, pct=round(100.0 * n / total, 1) if total > 0 else None)
        elif len(groups) == 1:
            out["n"] = float(groups[0])
    except ValueError:
        pass
    return out


def log_size(rec: dict) -> int:
    path = resolve_log(rec, rec.get("done_test"))
    try:
        return os.path.getsize(path) if path else 0
    except OSError:
        return 0


# ── Migration of the laptop patch (resume_jobs.json) ─────────────────────

def migrate_resume_jobs(src: Path, rd_dir: Path, collect_dir: Path,
                        waiter_command: str) -> list[dict]:
    """Translate ``run-detached\\resume_jobs.json`` entries (the per-laptop
    patch) into registry records WITHOUT launching anything.

    The patch's three shapes map onto the registry like this:

    * ``doneLogGlob`` + ``doneText``  → done test on the newest matching log;
    * ``stopIfLastLineMatches`` on ``lastLineFile`` → the file's last line:
      ``done$`` is the done test, the rest is the irreversible marker;
    * ``stopIfFileMatches`` on ``matchFile`` → ``Z DONE$`` is the done test,
      the other alternatives are the irreversible marker.

    Patterns in the patch were ``-like`` globs (``*x*``); the registry's
    running_match is a substring with ``*`` wildcards, so they carry over."""
    cfg = json.loads(Path(src).read_text(encoding="utf-8"))
    out = []
    for j in cfg.get("jobs", []):
        name = j.get("name", "job")
        launch = j.get("launch") or {}
        fields = dict(name=name, running_match=j.get("running", ""), project="Altium")
        if launch.get("via") == "collect":
            fields.update(command=launch["command"], cwd=str(collect_dir),
                          log_path=str(rd_dir / (launch["name"] + "-{ts}.log")),
                          job_id=_slug(launch["name"]))
        elif launch.get("via") == "task":
            fields.update(command=waiter_command, cwd=str(rd_dir),
                          log_path=str(rd_dir / "final-rebuild-waiter.out"),
                          job_id=_slug(launch["task"].replace("ClaudeRunDetached_", "")))
        else:
            raise JobError(f"{name}: unknown launch kind {launch.get('via')!r}")
        if j.get("doneLogGlob"):
            fields["done_test"] = {"pattern": re.escape(j["doneText"]), "file": j["doneLogGlob"]}
        elif j.get("lastLineFile"):
            alts = [a for a in str(j["stopIfLastLineMatches"]).split("|")]
            done = [a for a in alts if a.rstrip("$").strip().lower() == "done"]
            rest = [a for a in alts if a not in done]
            fields["done_test"] = {"pattern": "|".join(done) or "done$",
                                   "file": j["lastLineFile"], "last_line": True}
            if rest:
                fields["irreversible_after"] = {"pattern": "|".join(rest),
                                                "file": j["lastLineFile"], "last_line": True}
        elif j.get("matchFile"):
            alts = [a for a in str(j["stopIfFileMatches"]).split("|")]
            done = [a for a in alts if "DONE" in a]
            rest = [a for a in alts if a not in done]
            fields["done_test"] = {"pattern": "|".join(done), "file": j["matchFile"]}
            if rest:
                fields["irreversible_after"] = {"pattern": "|".join(rest), "file": j["matchFile"]}
        if "authorship" in str(fields.get("command", "")) and "--workers" in str(fields.get("command", "")):
            fields["progress"] = {"pattern": r"cells (\d+)/(\d+)", "file": j.get("doneLogGlob", "")}
        out.append(fields)
    return out
