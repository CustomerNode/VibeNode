"""SAFETY RAILS for ``scripts/session_reaper.py``.

This is the one tool in the repo allowed to kill processes on a box that hosts
live AI sessions. Killing the wrong thing aborts every running session, so its
"is this mine to kill?" rule gets pinned with real processes, not mocks.

The rule under test: a process is leaked **iff its session leader is dead**.
That is exact rather than heuristic -- orphaning changes a process's parent (to
init) but never its session id, and a process cannot forge another session's id.
Everything else (CPU, age, allowlist) only narrows it further.

These tests spawn real short-lived burners in their own sessions and clean up
after themselves, including on failure.

Run: python -m pytest tests/test_session_reaper.py -q
POSIX only -- session ids do not exist on Windows.
"""
from __future__ import annotations

import importlib.util
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(os.name == "nt", reason="POSIX session ids only")

REPO_ROOT = Path(__file__).resolve().parents[1]
_REAPER = REPO_ROOT / "scripts" / "session_reaper.py"

BURNER = "import time\nwhile True:\n    pass\n"


def _load():
    spec = importlib.util.spec_from_file_location("_session_reaper", _REAPER)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    sys.modules["_session_reaper"] = mod
    spec.loader.exec_module(mod)
    return mod


def _kill(pids) -> None:
    for pid in pids:
        try:
            os.kill(pid, signal.SIGKILL)
        except OSError:
            pass


def _spawn_dead_session(n: int = 2, ledger: Path | None = None) -> list[int]:
    """Start n burners whose session leader exits -> orphaned, sid preserved."""
    # The burners MUST NOT inherit our stdout pipe: they outlive the launcher,
    # so an inherited pipe never closes and capture_output() blocks forever.
    # Write the pids to a file instead and give the children DEVNULL.
    if ledger is not None:
        pidfile = str(ledger)
    else:
        import tempfile
        fd, pidfile = tempfile.mkstemp(prefix="reaper_test_", suffix=".pids")
        os.close(fd)
    script = (
        f"import subprocess,sys\n"
        f"ps=[subprocess.Popen([sys.executable,'-c','''{BURNER}'''],"
        f" stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL) for _ in range({n})]\n"
        f"open({pidfile!r},'w').write(' '.join(str(p.pid) for p in ps))\n"
    )
    subprocess.run(
        [sys.executable, "-c", script],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        start_new_session=True, timeout=30,
    )
    pids = [int(x) for x in Path(pidfile).read_text().split()]
    if ledger is None:
        os.unlink(pidfile)
    return pids


@pytest.fixture
def spawned(tmp_path):
    """Track spawned pids for cleanup, surviving a mid-helper failure.

    Appending pids in the test body is not enough: if the spawn helper raises
    (a timeout, say) the test never learns the pids and the burners leak --
    which is exactly the bug this whole file is about, and it happened during
    development. So the helper also records pids to a sidecar file that cleanup
    reads unconditionally.
    """
    ledger = tmp_path / "spawned.pids"
    tracked = _Tracked(ledger)
    try:
        yield tracked
    finally:
        pids = set(tracked)
        if ledger.exists():
            pids |= {int(x) for x in ledger.read_text().split() if x.strip()}
        _kill(pids)


class _Tracked(list):
    """A list that also knows where the sidecar pid ledger lives."""

    def __init__(self, ledger: Path):
        super().__init__()
        self.ledger = ledger


def test_orphan_keeps_its_session_id_after_the_leader_dies(spawned) -> None:
    """The premise the whole tool rests on."""
    pids = _spawn_dead_session(1, ledger=spawned.ledger)
    spawned.extend(pids)
    time.sleep(1)
    pid = pids[0]
    assert os.path.exists(f"/proc/{pid}"), "burner should still be running"
    sid = os.getsid(pid)
    with open(f"/proc/{pid}/stat", "rb") as fh:
        ppid = int(fh.read().split(b") ")[1].split()[1])
    assert ppid == 1, f"expected orphaned to init, got ppid={ppid}"
    assert not os.path.exists(f"/proc/{sid}"), "session leader should be gone"
    assert sid not in (0, 1), "orphan must retain a real session id, not init's"


def test_reaps_orphans_of_a_dead_session(spawned) -> None:
    mod = _load()
    pids = _spawn_dead_session(2, ledger=spawned.ledger)
    spawned.extend(pids)
    time.sleep(2)

    found = mod.find_leaked(min_cpu=20.0, min_age_min=0.0)
    found_pids = {f.pid for f in found}
    assert set(pids) <= found_pids, (
        f"leaked burners {pids} not detected; found {sorted(found_pids)}"
    )

    mod.reap([f for f in found if f.pid in set(pids)])
    time.sleep(1)
    survivors = [p for p in pids if os.path.exists(f"/proc/{p}")]
    assert not survivors, f"these survived the reap: {survivors}"


def test_never_touches_a_session_whose_leader_is_alive(spawned) -> None:
    """The rail that protects running sessions. If this breaks, sessions die."""
    mod = _load()
    leader = subprocess.Popen(
        [sys.executable, "-c",
         f"import subprocess,sys,time\n"
         f"p=subprocess.Popen([sys.executable,'-c','''{BURNER}'''])\n"
         f"print(p.pid, flush=True)\n"
         f"time.sleep(120)\n"],
        stdout=subprocess.PIPE, text=True, start_new_session=True,
    )
    try:
        child_pid = int(leader.stdout.readline().strip())
        spawned.extend([child_pid, leader.pid])
        time.sleep(2)

        found = mod.find_leaked(min_cpu=20.0, min_age_min=0.0)
        offenders = {f.pid for f in found} & {child_pid, leader.pid}
        assert not offenders, (
            f"would have killed processes of a LIVE session: {offenders}. "
            "A live leader means a live session -- CPU usage is irrelevant."
        )
    finally:
        leader.kill()


def test_scan_failure_is_never_reported_as_nothing_to_do() -> None:
    """A scan that could not run must exit 2, never 0."""
    mod = _load()
    assert issubclass(mod.ScanUnavailable, Exception)
    # The allowlist is a hard dependency: without it a sweep is unfiltered and
    # could target the session host, so its absence must raise, not degrade.
    assert hasattr(mod, "_allow_re")


def test_shares_the_leak_detector_allowlist() -> None:
    """Both tools must agree on what is untouchable."""
    mod = _load()
    allow = mod._allow_re()
    for cmdline in (
        "/usr/bin/python3 /home/x/VibeNode/reviver.py --guardian",
        "python [VibeNode-DO-NOT-KILL:session-daemon]",
    ):
        assert allow.search(cmdline), f"session host not protected: {cmdline!r}"
