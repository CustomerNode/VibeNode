"""_kill_process_tree must never signal anything it cannot prove is one CLI.

2026-10-06: a test's MagicMock backend returned a MagicMock from
``extract_process_pid``.  The old guard was ``pid <= 1`` inside a
``try/except Exception: pass`` that fell through, so the TypeError from
comparing a mock skipped the guard.  ``os.getpgid(mock)`` then coerced the
mock to 1 (MagicMock implements ``__index__``), returned process group 1, and
the function called ``os.killpg(1, SIGTERM)`` and ``SIGKILL``.  killpg(1) is
kill(-1): every process the user owns.  Running the suite logged the desktop
out and killed every agent session.

Every test here replaces os.kill / os.killpg with recorders, so nothing in
this file can send a real signal whatever the code under test does.
"""

import os
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

pytestmark = pytest.mark.skipif(os.name == "nt", reason="POSIX kill path")


@pytest.fixture
def kill_tree():
    from daemon.session_manager import SessionManager
    return SessionManager._kill_process_tree


@pytest.fixture
def signals():
    """Record every signal call instead of sending it."""
    sent = []
    with patch("os.kill", side_effect=lambda *a: sent.append(("kill",) + a)), \
         patch("os.killpg", side_effect=lambda *a: sent.append(("killpg",) + a)), \
         patch("time.sleep"):
        yield sent


@pytest.mark.parametrize("bad_pid", [
    MagicMock(),        # the 2026-10-06 incident
    None, 0, 1, -1, -5,
    True,               # bool is an int subclass: True == 1
    "1234", 12.0, object(),
])
def test_refuses_anything_that_is_not_a_real_pid(kill_tree, signals, bad_pid):
    with patch("os.getpgid", return_value=4242) as getpgid:
        kill_tree(bad_pid)
    assert signals == []
    getpgid.assert_not_called()


def test_refuses_own_pid(kill_tree, signals):
    kill_tree(os.getpid())
    assert signals == []


@pytest.mark.parametrize("bad_pgid", [1, 0, -1, MagicMock()])
def test_refuses_a_process_group_that_means_everything(kill_tree, signals, bad_pgid):
    """Even with a plausible pid, a resolved group of 1/0 is never signalled."""
    def _getpgid(p):
        return bad_pgid if p == 54321 else 777
    with patch("os.getpgid", side_effect=_getpgid):
        kill_tree(54321)
    assert signals == []


def test_still_kills_a_real_isolated_group(kill_tree, signals):
    """The guard must not break the normal path."""
    def _getpgid(p):
        return 54321 if p == 54321 else 777
    with patch("os.getpgid", side_effect=_getpgid):
        kill_tree(54321)
    assert [s[:2] for s in signals] == [("killpg", 54321), ("killpg", 54321)]


def test_conftest_safety_net_blocks_broadcast_signals():
    """tests/conftest.py wraps os.kill / os.killpg for the whole test run."""
    assert getattr(os, "_vn_signal_net", False) is True
    for call, arg in ((os.killpg, 1), (os.killpg, 0), (os.killpg, MagicMock()),
                      (os.kill, -1), (os.kill, 0), (os.kill, MagicMock())):
        with pytest.raises(RuntimeError):
            call(arg, 0)
    with pytest.raises(RuntimeError):
        os.kill(1, 15)


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="PID namespaces are Linux-only")
def test_the_suite_is_running_inside_its_own_pid_namespace():
    """tests/conftest.py re-execs every run under `unshare`.

    Inside the namespace a broadcast signal cannot reach the desktop session.
    If this fails, a plain `pytest` is running on the bare machine again.
    """
    import subprocess
    if os.environ.get("VIBENODE_TEST_NO_SANDBOX"):
        pytest.skip("sandbox explicitly disabled")
    if not os.environ.get("VIBENODE_TEST_SANDBOXED"):
        try:
            can = subprocess.run(
                ["unshare", "-U", "--map-current-user", "-p", "-f", "--mount-proc", "true"],
                capture_output=True, timeout=10).returncode == 0
        except Exception:
            can = False
        if not can:
            pytest.skip("unshare is not available or not permitted here")
        pytest.fail("the suite is NOT in a PID namespace although one is available")
    # pid 1 in here is the sandbox's bash init, not the machine's init.
    init = Path("/proc/1/cmdline").read_bytes().split(b"\0")[0]
    assert os.path.basename(init) == b"bash", f"pid 1 is {init!r}, not the sandbox init"
    # ...and the namespace holds only this run: a handful of processes.
    pids = [p for p in os.listdir("/proc") if p.isdigit()]
    assert len(pids) < 200, f"{len(pids)} processes visible: this is not an isolated namespace"
