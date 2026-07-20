#!/usr/bin/env python3
"""Report-only detector for leaked, orphaned, CPU-burning processes.

WHY THIS EXISTS
---------------
On 2026-07-19 a repro script leaked 64 ``python3 -c 'while True: pass'`` burners.
They reparented to PID 1 and span for 20 hours -- 615 CPU-hours -- pinning a
32-core dev box at load 67. Nobody noticed until the box was unusable. That had
been happening every couple of days.

The structural fixes live elsewhere (lifetime binding at spawn, enforced by
``core/testing/backend/test_background_process_lint.py``). This tool closes the
*detection* gap: it turns "I came in and my box was melting" into "something
leaked 4 minutes ago", which is the difference between a 30-second fix and an
afternoon.

IT NEVER KILLS ANYTHING. THAT IS A DESIGN DECISION, NOT AN OMISSION.
-------------------------------------------------------------------
An auto-killer is permanently racing the next novel leak pattern, and the cost
of one wrong match on this box is severe: it hosts live AI sessions, and killing
the session host aborts every running session (this has happened -- see
``tasks/lessons/``). Detection is high-value and safe; killing is low-marginal-
value and unbounded-risk. So this prints a report and the exact command a human
can run. Judgement stays with the operator.

WHAT COUNTS AS A LEAK
---------------------
All three must hold, because any one alone is far too noisy:

1. **Orphaned** -- the parent is gone (reparented to init), or the recorded
   parent PID has been recycled by a newer process. Long-lived daemons are
   orphaned too, hence the other two conditions.
2. **Burning CPU** -- sustained CPU over a live sampling window, not cumulative
   time. A quiet orphan is a daemon; a spinning orphan is a leak.
3. **Old enough** -- survived the grace period, so a legitimate short-lived
   background job mid-burst is not reported.

Known daemons are additionally allowlisted by name/cmdline (see ``ALLOWLIST``),
which is what keeps containerd/Xorg/VibeNode out of the report.

USAGE
    python3 infra/scripts/leak_detector.py                 # human report
    python3 infra/scripts/leak_detector.py --json          # machine-readable
    python3 infra/scripts/leak_detector.py --min-cpu 50 --min-age-min 60

EXIT CODES
    0  clean -- the scan RAN and found nothing
    1  suspected leaks reported
    2  the scan could not run (psutil missing). Never conflated with 0.

Cross-platform via psutil, matching ``core/testing/run.py``'s approach. If psutil
is missing this exits 2 and says the scan did not run -- it must NEVER report a
clean bill of health it did not actually establish. Note ``cn_venv`` has no
psutil; the systemd unit deliberately invokes ``/usr/bin/python3``.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import time
from dataclasses import asdict, dataclass

# Processes that are legitimately orphaned, long-lived, and sometimes busy.
# Matched case-insensitively against the process name AND full cmdline.
ALLOWLIST = (
    # --- AI session host: killing these aborts every running session ---------
    # NOTE: these patterns are deliberately ANCHORED to real binary/script
    # paths. A loose pattern here is not a cosmetic issue -- an earlier version
    # used `[/\\]code\b`, which matched any checkout living under a `code/`
    # directory (a very common layout) and silently allowlisted EVERY process
    # launched from there, i.e. exactly the leaks this tool exists to find.
    # Before adding an entry, check it against a repo path and a /tmp path.
    # Anchored to a PATH-COMPONENT boundary via `(?:^|\s)\S*[/\\]name`, not to
    # `^` alone: a script usually appears as argv[1] behind its interpreter
    # (`/usr/bin/python3 .../reviver.py`), so a `^`-only anchor matched nothing.
    # The boundary still excludes mid-path directory matches -- the repo has a
    # real `.claude/hooks/` whose scripts can background processes, and a bare
    # `\.claude[/\\]` would have made leaks from there invisible.
    r"vibenode",
    r"VibeNode",
    r"(?:^|\s)\S*[/\\]reviver\.py\b",
    r"(?:^|\s)\S*[/\\]prelogin\.py\b",
    r"(?:^|\s)\S*[/\\]\.local[/\\]bin[/\\]claude(\s|$)",
    # Intentionally NOT argv[0]-anchored: the agent's own wrappers look like
    # `/bin/bash -c source ~/.claude/shell-snapshots/...`, so the marker is an
    # argument, not the binary. Kept narrow to `shell-snapshots/` specifically
    # -- a bare `.claude/` would also exempt `.claude/hooks/` scripts, which can
    # background processes and must stay visible.
    r"[/\\]\.claude[/\\]shell-snapshots[/\\]",
    # --- container / virtualisation ------------------------------------------
    r"\bdockerd\b",
    r"\bcontainerd",
    r"\bpodman\b",
    r"\bqemu",
    # --- desktop / display ---------------------------------------------------
    r"\bXorg\b",
    r"\bcinnamon\b",
    r"\bgnome-shell\b",
    r"\bkwin",
    r"\bplasmashell\b",
    r"\blightdm\b",
    r"\bpipewire",
    r"\bwireplumber\b",
    r"\bpulseaudio\b",
    r"\bibus-",
    r"\bat-spi2",
    # --- editors / browsers (user-facing, user closes them) ------------------
    r"[/\\]usr[/\\]share[/\\]code[/\\]",       # VS Code install dir, NOT ~/code
    r"[/\\]\.config[/\\]Code\b",
    r"[/\\]\.vscode(-server)?[/\\]",
    r"[/\\]google[/\\]chrome[/\\]",
    r"\bchromium\b",
    r"\bfirefox\b",
    r"\bnode\b.*\bvite\b",
    # --- remote access / networking ------------------------------------------
    r"\brustdesk\b",
    r"\btailscaled?\b",
    r"\bsshd\b",
    r"\bNetworkManager\b",
    r"\bwpa_supplicant\b",
    r"\bavahi",
    r"\bshairport-sync\b",
    # --- init / system services ----------------------------------------------
    r"\bsystemd",
    r"\bdbus",
    r"\bpolkitd\b",
    r"\budisksd\b",
    r"\bupowerd\b",
    r"\bcolord\b",
    r"\bcron\b",
    r"\brsyslogd\b",
    r"\bfail2ban",
    r"\birqbalance\b",
    r"\bModemManager\b",
    r"\bbluetoothd\b",
    r"\bgvfs",
    r"\bkerneloops\b",
    r"\bsnapd?\b",
    r"\bunattended-upgrade",
    # --- project infra that is meant to persist ------------------------------
    r"(?:^|\s)\S*[/\\]redis-server\b",
    r"^postgres:",                              # postgres' own worker titles
    r"(?:^|\s)\S*[/\\]bin[/\\]postgres\b",
    r"(?:^|\s)\S*[/\\]gunicorn\b",
    r"(?:^|\s)\S*[/\\]daphne\b",
    r"(?:^|\s)\S*[/\\]watchdog\.py\b",
)
_ALLOW_RE = re.compile("|".join(ALLOWLIST), re.IGNORECASE)

DEFAULT_MIN_CPU = 20.0     # percent of one core, sustained over the sample
DEFAULT_MIN_AGE_MIN = 30   # minutes
SAMPLE_SECONDS = 1.0


@dataclass
class Suspect:
    pid: int
    ppid: int
    name: str
    cmdline: str
    cpu_percent: float
    age_minutes: float
    cpu_hours_burned: float


def _is_orphan(proc, psutil) -> bool:
    """Parent gone, or the parent slot was recycled by a newer process.

    The PID-reuse check matters: a leaked child can appear to have a live
    parent that is actually an unrelated, newer process holding a recycled PID.
    A parent created *after* its supposed child is the tell.
    """
    try:
        ppid = proc.ppid()
    except (psutil.NoSuchProcess, psutil.AccessDenied):
        return False
    if ppid in (0, 1):
        return True
    try:
        parent = psutil.Process(ppid)
        return parent.create_time() > proc.create_time()
    except psutil.NoSuchProcess:
        return True
    except psutil.AccessDenied:
        return False


class PsutilMissing(Exception):
    """Raised so a missing psutil can never be reported as 'clean'."""


def find_suspects(min_cpu: float, min_age_min: float) -> list[Suspect]:
    try:
        import psutil
    except ImportError as exc:
        # MUST NOT degrade to "clean" -- `cn_venv` has no psutil, so running
        # this with the repo's own interpreter would otherwise print an
        # all-clear and exit 0 while seeing nothing at all.
        raise PsutilMissing(
            f"psutil unavailable ({sys.executable}). This is NOT a clean bill of "
            "health -- the scan did not run. Use /usr/bin/python3 (which has "
            "psutil), or pip install psutil into this interpreter."
        ) from exc

    now = time.time()
    candidates = []
    for proc in psutil.process_iter(["pid", "ppid", "name", "cmdline", "create_time"]):
        try:
            info = proc.info
            age_min = (now - (info.get("create_time") or now)) / 60.0
            if age_min < min_age_min:
                continue
            cmdline = " ".join(info.get("cmdline") or []) or (info.get("name") or "")
            if _ALLOW_RE.search(cmdline) or _ALLOW_RE.search(info.get("name") or ""):
                continue
            if not _is_orphan(proc, psutil):
                continue
            proc.cpu_percent(None)  # prime the sampler
            candidates.append((proc, age_min, cmdline))
        except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
            continue

    if not candidates:
        return []

    time.sleep(SAMPLE_SECONDS)  # measure live CPU, not cumulative

    suspects: list[Suspect] = []
    for proc, age_min, cmdline in candidates:
        try:
            cpu = proc.cpu_percent(None)
            if cpu < min_cpu:
                continue
            times = proc.cpu_times()
            burned = (times.user + times.system) / 3600.0
            suspects.append(
                Suspect(
                    pid=proc.pid,
                    ppid=proc.ppid(),
                    name=proc.name(),
                    cmdline=cmdline[:160],
                    cpu_percent=round(cpu, 1),
                    age_minutes=round(age_min, 1),
                    cpu_hours_burned=round(burned, 2),
                )
            )
        except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
            continue

    return sorted(suspects, key=lambda s: s.cpu_percent, reverse=True)


def _notify(suspects: list[Suspect]) -> None:
    """Best-effort desktop notification. Never let this break the report."""
    total = sum(s.cpu_percent for s in suspects)
    body = (f"{len(suspects)} orphaned process(es) burning {total:.0f}% CPU.\n"
            f"Top: {suspects[0].cmdline[:70]}\n"
            f"Run: python3 infra/scripts/leak_detector.py")
    try:
        import shutil
        import subprocess
        if shutil.which("notify-send"):
            subprocess.run(
                ["notify-send", "-u", "critical", "-i", "dialog-warning",
                 "Leaked processes detected", body],
                check=False, timeout=10,
            )
    except Exception:
        pass


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--min-cpu", type=float, default=DEFAULT_MIN_CPU,
                    help=f"sustained CPU%% to flag (default {DEFAULT_MIN_CPU})")
    ap.add_argument("--min-age-min", type=float, default=DEFAULT_MIN_AGE_MIN,
                    help=f"minimum age in minutes (default {DEFAULT_MIN_AGE_MIN})")
    ap.add_argument("--json", action="store_true", help="machine-readable output")
    ap.add_argument("--notify", action="store_true",
                    help="raise a desktop notification when leaks are found")
    args = ap.parse_args()

    try:
        suspects = find_suspects(args.min_cpu, args.min_age_min)
    except PsutilMissing as exc:
        print(f"leak_detector: SCAN DID NOT RUN -- {exc}", file=sys.stderr)
        return 2  # distinct from 0 (clean) and 1 (leaks found)

    if suspects and args.notify:
        _notify(suspects)

    if args.json:
        print(json.dumps([asdict(s) for s in suspects], indent=2))
        return 1 if suspects else 0

    if not suspects:
        print(
            f"leak_detector: clean -- no orphaned processes over {args.min_cpu}% CPU "
            f"older than {args.min_age_min} min."
        )
        return 0

    total_cpu = sum(s.cpu_percent for s in suspects)
    total_burned = sum(s.cpu_hours_burned for s in suspects)
    print(f"leak_detector: {len(suspects)} SUSPECTED LEAK(S) "
          f"-- {total_cpu:.0f}% CPU, {total_burned:.1f} CPU-hours burned\n")
    for s in suspects:
        print(f"  pid {s.pid:>7}  {s.cpu_percent:>5.1f}% cpu  "
              f"{s.age_minutes/60:>6.1f}h old  {s.cpu_hours_burned:>7.2f} cpu-h")
        print(f"      {s.cmdline}")
    print("\nNothing was killed -- this tool only reports. Verify these are yours "
          "and that no DO-NOT-KILL marker is present, then:")
    print("  kill -9 " + " ".join(str(s.pid) for s in suspects))
    return 1


if __name__ == "__main__":
    sys.exit(main())
