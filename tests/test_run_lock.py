"""One parallel test run at a time (tests/_run_lock.py, conftest's
pytest_sessionstart).  Real processes holding a real OS lock on a temp file."""

import subprocess
import sys
import textwrap
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from tests import _run_lock as rl  # noqa: E402

HOLDER = textwrap.dedent("""
    import sys, time
    sys.path.insert(0, {root!r})
    from shared.test_run_lock import RunLock
    lock = RunLock({path!r})
    assert lock.try_acquire()
    import os
    print("held", os.getpid(), flush=True)
    time.sleep(float(sys.argv[1]))
    lock.release()
    print("released", flush=True)
""")


def _hold(path, seconds):
    p = subprocess.Popen([sys.executable, "-c", HOLDER.format(root=str(ROOT), path=str(path)), str(seconds)],
                         stdout=subprocess.PIPE, text=True)
    word, pid = p.stdout.readline().split()
    assert word == "held"
    p.lock_pid = int(pid)  # the interpreter's own pid (a venv launcher has another)
    return p


def test_a_second_run_waits_until_the_first_releases(tmp_path):
    path = tmp_path / "run.lock"
    holder = _hold(path, 3)
    said = []
    lock = rl.RunLock(path)
    try:
        assert not lock.try_acquire()
        start = time.monotonic()
        assert lock.acquire(max_wait=60, poll=0.2, say=said.append) is True
        waited = time.monotonic() - start
        assert waited > 1.0, "it should have waited for the holder"
        assert any(f"pid {holder.lock_pid}" in s for s in said), said
        assert any("finished" in s for s in said), said
    finally:
        lock.release()
        holder.wait(10)


def test_a_killed_run_never_leaves_the_lock_behind(tmp_path):
    path = tmp_path / "run.lock"
    holder = _hold(path, 120)
    lock = rl.RunLock(path)
    try:
        assert not lock.try_acquire()
        holder.kill()
        holder.wait(10)
        assert lock.acquire(max_wait=10, poll=0.1, say=lambda m: None) is True
    finally:
        lock.release()


def test_the_wait_is_capped_and_the_run_then_starts(tmp_path):
    path = tmp_path / "run.lock"
    holder = _hold(path, 60)
    said = []
    try:
        lock = rl.RunLock(path)
        assert lock.acquire(max_wait=0.5, poll=0.1, say=said.append) is False
        assert not lock.held
        assert any("WARNING" in s for s in said)
    finally:
        holder.kill()
        holder.wait(10)


def test_release_lets_the_next_one_in(tmp_path):
    a, b = rl.RunLock(tmp_path / "x.lock"), rl.RunLock(tmp_path / "x.lock")
    assert a.try_acquire()
    assert not b.try_acquire()
    a.release()
    assert b.try_acquire()
    b.release()


def test_switches(monkeypatch):
    monkeypatch.setenv("VIBENODE_TEST_RUN_LOCK", "0")
    assert not rl.enabled()
    monkeypatch.delenv("VIBENODE_TEST_RUN_LOCK")
    assert rl.enabled()
    monkeypatch.setenv("VIBENODE_TEST_RUN_LOCK_WAIT", "12")
    assert rl.max_wait() == 12.0


def test_a_parallel_run_holds_the_machine_lock_while_it_runs(tmp_path):
    """Through conftest: a run with -n takes the real lock, a run without
    -n never touches it."""
    pytest.importorskip("xdist")
    probe = tmp_path / "test_probe_lock.py"
    probe.write_text(textwrap.dedent(f"""
        import os, sys
        sys.path.insert(0, {str(ROOT)!r})
        from shared.test_run_lock import RunLock, DEFAULT_PATH
        def test_lock_state():
            other = RunLock(DEFAULT_PATH)
            free = other.try_acquire()
            other.release()
            assert ("free" if free else "taken") == os.environ["EXPECT_LOCK"]
    """))
    # This suite's own parallel run may hold the real lock; then the probe
    # run would wait.  Skip rather than wait on ourselves.
    mine = rl.RunLock()
    if not mine.try_acquire():
        pytest.skip("another parallel run holds the machine lock right now")
    mine.release()

    import os

    def run(expect, *extra):
        env = dict(os.environ, EXPECT_LOCK=expect)
        return subprocess.run([sys.executable, "-m", "pytest", str(probe), "-q", "-p", "no:cacheprovider", "-p", "tests.conftest",
                               "--rootdir", str(ROOT), "-c", str(ROOT / "pyproject.toml"), *extra],
                              cwd=ROOT, env=env, capture_output=True, text=True, timeout=180)

    serial = run("free")
    assert serial.returncode == 0, serial.stdout + serial.stderr
    # Under -n the test runs in a worker while the controller holds the lock.
    parallel = run("taken", "-n", "1")
    assert parallel.returncode == 0, parallel.stdout + parallel.stderr
    after = rl.RunLock()
    assert after.try_acquire(), "the lock must be free again after the run"
    after.release()
