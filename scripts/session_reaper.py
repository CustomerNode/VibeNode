#!/usr/bin/env python3
"""Reap processes left behind by sessions that are already dead.

THE PROBLEM
-----------
A session spawns a command; the command backgrounds a process; the command
exits without reaping it. The child reparents to init and runs forever. One
such batch -- 64 CPU burners from a load-test script -- sat on a dev box for 20
hours (615 CPU-hours) at load 67 before anyone noticed. It kept recurring.

The leak is NOT the session's fault and cannot be fixed in whatever repo the
script lives in: any script anywhere can do this, and a `trap` cannot stop it
(bash defers trap handlers while blocked in a foreground command and skips them
entirely under SIGKILL, and a tight-loop child ignores SIGTERM outright).

WHY SESSION ID IS THE RIGHT HANDLE
----------------------------------
``sdk_patches.py`` Patch 4 forces ``start_new_session=True`` on every POSIX
subprocess, so each spawned CLI becomes a session leader. That is what makes
orphans survive -- but it is also what makes them findable:

    orphaning changes a process's PARENT (to init). It does NOT change its
    SESSION ID.

So every descendant of a session -- however deeply nested, however orphaned,
even after `setsid` on an intermediate process -- still carries the session id
of the leader it came from, and a process cannot forge another session's id.
That gives an ownership claim that is exact rather than heuristic:

    the session leader is GONE, but processes carrying its session id are
    still running and burning CPU  ->  those are leaked, definitionally.

That precision is what makes reaping safe here, where pattern-matching would
not be. This box hosts live AI sessions, and killing the wrong thing aborts
every one of them (it has happened). We never kill on "looks like junk".

SAFETY RAILS (all must hold before anything is signalled)
---------------------------------------------------------
1. The session LEADER must be dead. A live leader means a live session -- we
   never touch it, no matter how much CPU it uses. A test suite is *supposed*
   to saturate the box.
2. The process must be burning CPU over a live sampling window. A quiet orphan
   is a daemon, not a leak.
3. It must be older than a grace period, so a mid-burst background job is safe.
4. It must not match the allowlist in ``leak_detector.py`` -- shared so the two
   tools can never disagree about what is untouchable.
5. Session id 0/1 and our own session are never candidates.

DRY RUN BY DEFAULT. Pass ``--reap`` to actually signal.

USAGE
    python3 scripts/session_reaper.py               # report only (default)
    python3 scripts/session_reaper.py --reap        # actually reap
    python3 scripts/session_reaper.py --reap --min-cpu 50 --min-age-min 60

EXIT CODES
    0  nothing to do (the scan RAN and found nothing)
    1  leaked processes found (and reaped, if --reap)
    2  the scan could not run -- never conflated with 0

PLATFORM: POSIX (session ids). On Windows this exits 2 rather than pretending;
the equivalent there is a Job Object with JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
assigned at spawn.
"""
from __future__ import annotations

import argparse
import json
import os
import signal
import sys
import time
from dataclasses import asdict, dataclass
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

DEFAULT_MIN_CPU = 20.0
DEFAULT_MIN_AGE_MIN = 30
SAMPLE_SECONDS = 1.0


class ScanUnavailable(Exception):
    """Raised so an un-runnable scan can never be reported as 'nothing to do'."""


@dataclass
class Leaked:
    pid: int
    sid: int
    name: str
    cmdline: str
    cpu_percent: float
    age_minutes: float
    cpu_hours_burned: float
    reaped: bool = False


def _allow_re():
    """Share leak_detector's allowlist so the two tools cannot disagree."""
    try:
        from leak_detector import _ALLOW_RE  # type: ignore
        return _ALLOW_RE
    except Exception as exc:  # pragma: no cover - defensive
        raise ScanUnavailable(
            f"cannot load the shared allowlist from leak_detector.py: {exc}. "
            "Refusing to reap without it -- an unfiltered sweep could kill the "
            "session host."
        ) from exc


def find_leaked(min_cpu: float, min_age_min: float) -> list[Leaked]:
    if os.name == "nt":
        raise ScanUnavailable(
            "POSIX session ids are unavailable on Windows; this scan did not run."
        )
    try:
        import psutil
    except ImportError as exc:
        raise ScanUnavailable(
            f"psutil unavailable ({sys.executable}); this scan did not run."
        ) from exc

    allow_re = _allow_re()
    try:
        own_sid = os.getsid(0)
    except OSError:
        own_sid = -1

    procs = []
    live_pids: set[int] = set()
    for p in psutil.process_iter(["pid", "ppid", "name", "cmdline", "create_time"]):
        try:
            live_pids.add(p.info["pid"])
            procs.append(p)
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue

    now = time.time()
    candidates = []
    for p in procs:
        try:
            pid = p.info["pid"]
            sid = os.getsid(pid)
            # Rail 5: never our own session, never init/kernel sessions.
            if sid in (0, 1) or sid == own_sid:
                continue
            # Rail 1: a LIVE leader means a live session -- hands off.
            if sid in live_pids:
                continue
            # Rail 3: grace period.
            age_min = (now - (p.info.get("create_time") or now)) / 60.0
            if age_min < min_age_min:
                continue
            cmdline = " ".join(p.info.get("cmdline") or []) or (p.info.get("name") or "")
            if not cmdline:
                continue
            # Rail 4: shared allowlist.
            if allow_re.search(cmdline) or allow_re.search(p.info.get("name") or ""):
                continue
            p.cpu_percent(None)  # prime
            candidates.append((p, sid, age_min, cmdline))
        except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess, OSError):
            continue

    if not candidates:
        return []

    time.sleep(SAMPLE_SECONDS)  # Rail 2 measures live CPU, not cumulative

    leaked: list[Leaked] = []
    for p, sid, age_min, cmdline in candidates:
        try:
            cpu = p.cpu_percent(None)
            if cpu < min_cpu:
                continue
            t = p.cpu_times()
            leaked.append(
                Leaked(
                    pid=p.pid,
                    sid=sid,
                    name=p.name(),
                    cmdline=cmdline[:160],
                    cpu_percent=round(cpu, 1),
                    age_minutes=round(age_min, 1),
                    cpu_hours_burned=round((t.user + t.system) / 3600.0, 2),
                )
            )
        except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
            continue
    return sorted(leaked, key=lambda x: x.cpu_percent, reverse=True)


def reap(items: list[Leaked]) -> None:
    """SIGTERM, then SIGKILL what ignores it.

    The burners that caused the original incident ignore SIGTERM outright, so
    escalation is required -- a polite kill alone is what let them survive.
    """
    for it in items:
        try:
            os.kill(it.pid, signal.SIGTERM)
        except OSError:
            pass
    time.sleep(2)
    for it in items:
        try:
            os.kill(it.pid, 0)          # still alive?
            os.kill(it.pid, signal.SIGKILL)
        except OSError:
            pass

    # Confirm, with a grace period. Checking immediately after SIGKILL reports
    # false "SURVIVED": the kernel has not finished tearing the process down and
    # `kill(pid, 0)` still succeeds. Reporting a successful reap as a failure is
    # its own bug -- it teaches the operator to distrust a tool that worked.
    deadline = time.time() + 3.0
    pending = list(items)
    while pending and time.time() < deadline:
        still = []
        for it in pending:
            try:
                os.kill(it.pid, 0)
                still.append(it)
            except OSError:
                it.reaped = True
        pending = still
        if pending:
            time.sleep(0.1)
    for it in pending:
        it.reaped = False               # genuinely survived SIGKILL


def _notify(items: list[Leaked], did_reap: bool) -> None:
    verb = "Reaped" if did_reap else "Detected"
    total = sum(i.cpu_percent for i in items)
    body = (f"{verb} {len(items)} orphaned process(es) from dead sessions, "
            f"{total:.0f}% CPU.\nTop: {items[0].cmdline[:70]}")
    try:
        import shutil
        import subprocess
        if shutil.which("notify-send"):
            subprocess.run(
                ["notify-send", "-i", "dialog-warning",
                 f"VibeNode: {verb.lower()} leaked processes", body],
                check=False, timeout=10,
            )
    except Exception:
        pass


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--reap", action="store_true",
                    help="actually kill (default is a dry run)")
    ap.add_argument("--min-cpu", type=float, default=DEFAULT_MIN_CPU)
    ap.add_argument("--min-age-min", type=float, default=DEFAULT_MIN_AGE_MIN)
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--notify", action="store_true")
    args = ap.parse_args()

    try:
        items = find_leaked(args.min_cpu, args.min_age_min)
    except ScanUnavailable as exc:
        print(f"session_reaper: SCAN DID NOT RUN -- {exc}", file=sys.stderr)
        return 2

    if items and args.reap:
        reap(items)

    if args.json:
        print(json.dumps([asdict(i) for i in items], indent=2))
        return 1 if items else 0

    if not items:
        print("session_reaper: nothing to do -- no CPU-burning processes from "
              f"dead sessions (>{args.min_cpu}% CPU, >{args.min_age_min} min).")
        return 0

    if args.notify:
        _notify(items, args.reap)

    action = "REAPED" if args.reap else "FOUND (dry run -- pass --reap to kill)"
    burned = sum(i.cpu_hours_burned for i in items)
    print(f"session_reaper: {action} {len(items)} leaked process(es), "
          f"{burned:.1f} CPU-hours burned\n")
    by_sid: dict[int, list[Leaked]] = {}
    for i in items:
        by_sid.setdefault(i.sid, []).append(i)
    for sid, group in by_sid.items():
        print(f"  dead session {sid} -- {len(group)} process(es):")
        for i in group[:5]:
            status = ("reaped" if i.reaped else "SURVIVED") if args.reap else "would reap"
            print(f"    pid {i.pid:>7}  {i.cpu_percent:>5.1f}% cpu  "
                  f"{i.age_minutes/60:>5.1f}h  [{status}]  {i.cmdline[:60]}")
        if len(group) > 5:
            print(f"    ... and {len(group) - 5} more")
    return 1


if __name__ == "__main__":
    sys.exit(main())
