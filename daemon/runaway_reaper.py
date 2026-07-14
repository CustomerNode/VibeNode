"""
Runaway search-process reaper.

The Claude CLI (a binary VibeNode spawns but does not control) shells out to
external code-search tools — ``ugrep`` in particular — during a turn. When a
turn is interrupted, or the CLI otherwise loses track of one, that search
process can be left running detached. On a large repository ``ugrep`` then pins
every core indefinitely (observed in the wild: 1199% CPU sustained for 11
hours). Several of those at once drives the machine's load average into the 20s
and starves every other session — WebSocket round-trips and e2e tests time out,
UI latency spikes, builds crawl.

VibeNode cannot change how the CLI spawns or reaps these children, so this is a
safety net rather than a root-cause fix: a lightweight background thread sweeps
for target search processes that have burned far more CPU time than any real
search ever could and kills them.

Why CPU-time (not wall-time) is the signal: a genuine ``ugrep`` invocation
finishes in a fraction of a CPU-second even on a huge tree; only a runaway
accumulates minutes of CPU. A process that is merely blocked/idle consumes no
CPU and is not causing load, so it is intentionally left alone. The default
limit (120 CPU-seconds) is orders of magnitude above any legitimate search, so
this can only ever reap true runaways — and it never targets the daemon,
session CLIs, or anything other than the explicit comm allowlist below.

POSIX-only: this is where the problem occurs and where ``ps`` is available. On
Windows ``start()`` is a no-op (returns False).

Tunables (env, all optional):
  VIBENODE_REAP_COMMS            comma-separated process comm names (default "ugrep")
  VIBENODE_REAP_CPU_SECONDS      CPU-seconds before a match is reaped (default 120)
  VIBENODE_REAP_INTERVAL_SECONDS sweep period in seconds (default 60)
  VIBENODE_REAP_DISABLED         set to "1" to disable the reaper entirely
"""

import logging
import os
import subprocess
import threading
import time

logger = logging.getLogger(__name__)


def _target_comms():
    raw = os.environ.get("VIBENODE_REAP_COMMS") or "ugrep"
    return frozenset(c.strip() for c in raw.split(",") if c.strip())


def _int_env(name, default):
    try:
        return int(os.environ.get(name) or default)
    except (TypeError, ValueError):
        return default


def _parse_cpu_seconds(field):
    """Parse a ps TIME field ('[[DD-]HH:]MM:SS') into total CPU seconds.

    Returns -1 if the field can't be parsed (so it is never treated as a
    runaway).
    """
    field = (field or "").strip()
    if not field:
        return -1
    days = 0
    if "-" in field:  # DD-HH:MM:SS
        day_part, field = field.split("-", 1)
        try:
            days = int(day_part)
        except ValueError:
            return -1
    try:
        nums = [int(p) for p in field.split(":")]
    except ValueError:
        return -1
    seconds = 0
    for n in nums:  # accumulate right-to-left units (…:HH:MM:SS)
        seconds = seconds * 60 + n
    return days * 86400 + seconds


def _sweep_once(target_comms, cpu_limit):
    """One pass: kill any allowlisted comm that has exceeded the CPU limit."""
    try:
        proc = subprocess.run(
            ["ps", "-eo", "pid=,comm=,time="],
            capture_output=True, text=True, timeout=15,
        )
    except Exception:  # ps missing / timed out — try again next sweep
        return
    if proc.returncode != 0:
        return
    self_pid = os.getpid()
    for line in proc.stdout.splitlines():
        parts = line.split(None, 2)
        if len(parts) != 3:
            continue
        pid_str, comm, cpu_field = parts
        # ps prints comm as a basename, but strip a path defensively.
        comm = os.path.basename(comm.strip())
        if comm not in target_comms:
            continue
        cpu = _parse_cpu_seconds(cpu_field)
        if cpu < cpu_limit:
            continue
        try:
            pid = int(pid_str)
        except ValueError:
            continue
        if pid == self_pid:
            continue
        try:
            os.kill(pid, 9)
            logger.warning(
                "runaway-reaper: killed %s pid=%s (%ss CPU >= %ss limit)",
                comm, pid, cpu, cpu_limit,
            )
        except ProcessLookupError:
            pass  # already gone
        except Exception as e:  # permission etc. — log and move on
            logger.warning("runaway-reaper: could not kill pid=%s (%s): %s", comm, pid, e)


def _loop(target_comms, cpu_limit, interval):
    while True:
        try:
            _sweep_once(target_comms, cpu_limit)
        except Exception:  # a reaper must never crash the daemon
            logger.debug("runaway-reaper: sweep error", exc_info=True)
        time.sleep(interval)


def start():
    """Start the reaper as a daemon thread. No-op (returns False) off POSIX
    or when VIBENODE_REAP_DISABLED=1. Never raises."""
    try:
        if os.name == "nt":
            return False
        if (os.environ.get("VIBENODE_REAP_DISABLED") or "").strip() == "1":
            logger.info("runaway-reaper: disabled via VIBENODE_REAP_DISABLED=1")
            return False
        target_comms = _target_comms()
        cpu_limit = _int_env("VIBENODE_REAP_CPU_SECONDS", 120)
        interval = _int_env("VIBENODE_REAP_INTERVAL_SECONDS", 60)
        threading.Thread(
            target=_loop,
            args=(target_comms, cpu_limit, interval),
            daemon=True,
            name="runaway-reaper",
        ).start()
        logger.info(
            "runaway-reaper: watching %s (reap at >=%ss CPU, sweep every %ss)",
            ",".join(sorted(target_comms)), cpu_limit, interval,
        )
        return True
    except Exception:
        logger.warning("runaway-reaper: failed to start", exc_info=True)
        return False
