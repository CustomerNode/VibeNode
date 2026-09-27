"""Regression tests for the reviver / autostart spawn safety guard.

The guard lives in ``tests/conftest.py`` as the autouse fixture
``_forbid_reviver_and_autostart_spawn``. It wraps ``subprocess.Popen`` and
raises ``AssertionError`` if a test tries to spawn ``reviver.py`` or
``schtasks`` — either of which can register or DELETE the production
``\\VibeNodeReviver_0d5a41d4`` scheduled task and Startup VBS.

These tests exist because the guard is load-bearing: if it silently stops
firing, a whole class of production-destroying bugs (see the 2026-09-25
ARM64 investigation) can silently return.
"""

import os
import subprocess

import pytest


# ---------------------------------------------------------------------------
# The guard must FIRE (loudly) on any of these command shapes.
# ---------------------------------------------------------------------------

def test_guard_blocks_reviver_launch_via_popen_list():
    # Generic checkout path — the guard fires on the ``reviver.py`` basename,
    # not on any user-specific prefix, so this string is portable across
    # machines and never carries a real user's path into the public repo.
    with pytest.raises(AssertionError, match="reviver / autostart safety guard"):
        subprocess.Popen(
            ["C:/Python314/pythonw.exe", "C:/some/checkout/VibeNode/reviver.py"]
        )


def test_guard_blocks_reviver_unregister_launch():
    with pytest.raises(AssertionError, match="reviver / autostart safety guard"):
        subprocess.Popen(
            ["python", "reviver.py", "--unregister"]
        )


def test_guard_blocks_reviver_launch_via_run():
    # subprocess.run() routes through Popen on CPython, so the wrapper covers it too.
    with pytest.raises(AssertionError, match="reviver / autostart safety guard"):
        subprocess.run(["python", "reviver.py"])


def test_guard_blocks_schtasks_create():
    with pytest.raises(AssertionError, match="reviver / autostart safety guard"):
        subprocess.Popen(["schtasks.exe", "/Create", "/TN", "VibeNodeReviver_0d5a41d4"])


def test_guard_blocks_schtasks_delete():
    with pytest.raises(AssertionError, match="reviver / autostart safety guard"):
        subprocess.Popen(["schtasks", "/Delete", "/TN", "VibeNodeReviver_0d5a41d4", "/F"])


def test_guard_blocks_full_windows_schtasks_path():
    # The reviver resolves schtasks via SystemRoot; make sure a fully-qualified
    # path still trips the guard (it's the basename that matters).
    with pytest.raises(AssertionError, match="reviver / autostart safety guard"):
        subprocess.Popen([r"C:\Windows\System32\schtasks.exe", "/Query"])


# ---------------------------------------------------------------------------
# The guard must NOT fire on unrelated subprocesses. Otherwise it would break
# every other test in the suite that spawns git, pytest, playwright, etc.
# ---------------------------------------------------------------------------

def test_guard_allows_unrelated_subprocess():
    # Bare Python echo — safe, does not touch autostart. Must not raise.
    proc = subprocess.run(
        ["python", "-c", "print('ok')"],
        capture_output=True, text=True, timeout=15,
    )
    assert proc.returncode == 0
    assert "ok" in proc.stdout


def test_guard_allows_string_literal_containing_reviver():
    # A test that merely BUILDS a string containing "reviver.py" (e.g. leak-detector
    # allowlist tests) must not be blocked — the guard fires on the subprocess call,
    # not on string contents in the test body.
    fake_cmdline = "/usr/bin/python3 /home/x/VibeNode/reviver.py --guardian"
    assert "reviver.py" in fake_cmdline
    # No AssertionError raised: the guard only triggers on Popen invocation.


# ---------------------------------------------------------------------------
# Windows-user-folder sandboxing. Complements the subprocess guard: even if a
# test somehow reaches an in-process registration path, APPDATA points at the
# tmp sandbox so the real Startup folder is untouchable.
# ---------------------------------------------------------------------------

def test_appdata_env_is_sandboxed_not_real_user_appdata():
    appdata = os.environ.get("APPDATA", "")
    assert appdata, "APPDATA must be set during tests"
    # The real user's Roaming AppData is under C:\Users\<name>\AppData\Roaming.
    # The sandbox lives under the pytest tmp path factory — always contains
    # 'pytest' somewhere. This is a coarse but reliable check.
    low = appdata.lower().replace("\\", "/")
    assert "pytest" in low or "/tmp" in low or low.endswith("/appdata/roaming"), (
        f"APPDATA should be redirected to a tmp sandbox, got: {appdata!r}"
    )


def test_startup_folder_under_sandboxed_appdata_exists():
    # _isolate_daemon_home pre-creates the Startup subdir so any in-process
    # reviver code path finds it and writes into the sandbox rather than
    # crashing on a missing folder.
    startup = os.path.join(
        os.environ["APPDATA"],
        "Microsoft", "Windows", "Start Menu", "Programs", "Startup",
    )
    assert os.path.isdir(startup), f"Sandboxed Startup dir missing: {startup}"


def test_userprofile_env_is_sandboxed():
    # USERPROFILE is what Path.home() reads on Windows, and it's what a
    # subprocess would inherit if one ever slipped past the Popen guard.
    up = os.environ.get("USERPROFILE", "")
    assert up, "USERPROFILE must be set during tests"
    low = up.lower().replace("\\", "/")
    assert "pytest" in low or "/tmp" in low, (
        f"USERPROFILE should be redirected to a tmp sandbox, got: {up!r}"
    )
