"""An update installs what it pulls (app/requirements_sync.py, 2026-10-23).

The Update button pulled code and never ran pip; the launcher installed four
hard-coded packages.  Any other dependency a commit added reached one-click
users only by hand.  Now:

* ``do_git_sync`` runs pip when the pull changed the requirements.txt blob,
  and only then (tests/test_publish_gate_commit.py covers the sync path);
* run.py runs pip at launch when requirements.txt's sha256 differs from
  ``data/requirements.stamp``, and writes the stamp only after success;
* the daily SDK upgrade passes the requirement line (with its ceiling) to pip
  instead of a bare ``--upgrade claude-code-sdk``.
"""

import subprocess
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from app import requirements_sync as rs


def _fake_run(returncode=0, stdout="", stderr=""):
    calls = []

    def run(cmd, **kw):
        calls.append(list(cmd))
        return MagicMock(returncode=returncode, stdout=stdout, stderr=stderr)
    run.calls = calls
    return run


# ── sha256 stamp ──────────────────────────────────────────────────────────

def test_sha256_of_a_missing_file_is_empty(tmp_path):
    assert rs.requirements_sha256(tmp_path / "nope.txt") == ""


def test_first_launch_installs_and_writes_the_stamp(tmp_path):
    req = tmp_path / "requirements.txt"
    req.write_text("flask>=3.1\n", encoding="utf-8")
    stamp = tmp_path / "data" / "requirements.stamp"
    run = _fake_run(stdout="Successfully installed flask-3.1.3 tzdata-2026.1")

    res = rs.install_if_changed(req, stamp, python="py.exe", runner=run)

    assert res["pip_ran"] is True and res["pip_ok"] is True
    assert res["pip_installed"] == 2
    assert run.calls == [["py.exe", "-m", "pip", "install", "--disable-pip-version-check",
                          "--only-binary=:all:", "-r", str(req)]]
    assert stamp.read_text().strip() == rs.requirements_sha256(req)


def test_unchanged_requirements_do_not_run_pip(tmp_path):
    req = tmp_path / "requirements.txt"
    req.write_text("flask>=3.1\n", encoding="utf-8")
    stamp = tmp_path / "requirements.stamp"
    rs.write_stamp(rs.requirements_sha256(req), stamp)
    run = _fake_run()

    res = rs.install_if_changed(req, stamp, python="py.exe", runner=run)

    assert res["pip_ran"] is False and res["reason"] == "unchanged"
    assert run.calls == []


def test_changed_requirements_run_pip_again(tmp_path):
    req = tmp_path / "requirements.txt"
    req.write_text("flask>=3.1\n", encoding="utf-8")
    stamp = tmp_path / "requirements.stamp"
    rs.write_stamp(rs.requirements_sha256(req), stamp)
    req.write_text("flask>=3.1\ntzdata\n", encoding="utf-8")
    run = _fake_run(stdout="Successfully installed tzdata-2026.1")

    res = rs.install_if_changed(req, stamp, python="py.exe", runner=run)

    assert res["pip_ran"] is True and len(run.calls) == 1
    assert stamp.read_text().strip() == rs.requirements_sha256(req)


def test_a_failed_install_leaves_no_stamp_so_it_is_retried(tmp_path):
    req = tmp_path / "requirements.txt"
    req.write_text("nonexistent-package-xyz\n", encoding="utf-8")
    stamp = tmp_path / "requirements.stamp"
    run = _fake_run(returncode=1, stderr="ERROR: No matching distribution found for nonexistent-package-xyz")

    res = rs.install_if_changed(req, stamp, python="py.exe", runner=run)

    assert res["pip_ran"] is True and res["pip_ok"] is False
    assert "No matching distribution" in res["pip_output_tail"]
    assert not stamp.exists()
    # ...and the next launch tries again.
    res2 = rs.install_if_changed(req, stamp, python="py.exe", runner=run)
    assert res2["pip_ran"] is True and len(run.calls) == 2


def test_pip_timeout_and_crash_are_reported_not_raised(tmp_path):
    req = tmp_path / "requirements.txt"
    req.write_text("flask\n", encoding="utf-8")

    def timeout(cmd, **kw):
        raise subprocess.TimeoutExpired(cmd, kw.get("timeout", 1))
    res = rs.install_requirements("py.exe", req, runner=timeout)
    assert res["pip_ok"] is False and "timed out" in res["pip_output_tail"]

    def boom(cmd, **kw):
        raise OSError("no such interpreter")
    res = rs.install_requirements("py.exe", req, runner=boom)
    assert res["pip_ok"] is False and "no such interpreter" in res["pip_output_tail"]


# ── PEP 668: a system Python on Ubuntu/Debian (2026-10-29) ────────────────

_PEP668 = ("error: externally-managed-environment\n"
           "× This environment is externally managed")


def _pep668_run():
    """Refuses a plain install the way Debian's pip does; accepts --user."""
    calls = []

    def run(cmd, **kw):
        calls.append(list(cmd))
        if "--break-system-packages" in cmd:
            return MagicMock(returncode=0, stdout="Successfully installed psutil-7.0.0", stderr="")
        return MagicMock(returncode=1, stdout="", stderr=_PEP668)
    run.calls = calls
    return run


def test_a_system_python_installs_into_the_user_site(tmp_path):
    req = tmp_path / "requirements.txt"
    req.write_text("psutil>=5.9\n", encoding="utf-8")
    py = tmp_path / "usr" / "bin" / "python3"   # no pyvenv.cfg near it
    run = _pep668_run()

    res = rs.install_requirements(str(py), req, runner=run)

    assert res["pip_ok"] is True and res["pip_installed"] == 1
    assert len(run.calls) == 2
    assert run.calls[1] == [str(py), "-m", "pip", "install", "--user", "--break-system-packages",
                            "--disable-pip-version-check", "--only-binary=:all:", "-r", str(req)]


def test_a_venv_python_is_never_given_the_override(tmp_path):
    req = tmp_path / "requirements.txt"
    req.write_text("psutil>=5.9\n", encoding="utf-8")
    venv = tmp_path / ".venv"
    (venv / "bin").mkdir(parents=True)
    (venv / "pyvenv.cfg").write_text("home = /usr/bin\n", encoding="utf-8")
    run = _pep668_run()

    res = rs.install_requirements(str(venv / "bin" / "python"), req, runner=run)

    assert res["pip_ok"] is False and len(run.calls) == 1
    assert "--break-system-packages" not in run.calls[0]


def test_other_failures_are_not_retried_with_the_override(tmp_path):
    req = tmp_path / "requirements.txt"
    req.write_text("nope-xyz\n", encoding="utf-8")
    run = _fake_run(returncode=1, stderr="ERROR: No matching distribution found for nope-xyz")

    res = rs.install_requirements(str(tmp_path / "python3"), req, runner=run)

    assert res["pip_ok"] is False and len(run.calls) == 1


def test_launch_pip_timeout_stays_under_the_boot_watchdog_budget():
    # run.py's "deps" boot step has a 600s watchdog budget; a longer pip wait
    # would be declared a hang instead of reported.
    assert rs.LAUNCH_PIP_TIMEOUT < 600


def test_installed_count_reads_pips_summary_line():
    assert rs.installed_count("Requirement already satisfied: flask\n") == 0
    assert rs.installed_count("...\nSuccessfully installed a-1.0 b-2.0 c-3.0\n") == 3


# ── the SDK requirement line and its ceiling ───────────────────────────────

def test_sdk_requirement_is_read_from_requirements_txt(tmp_path):
    req = tmp_path / "requirements.txt"
    req.write_text("flask>=3.1\n# a comment\nclaude-code-sdk>=0.0.25,<0.1  # pinned\nanthropic\n",
                   encoding="utf-8")
    assert rs.sdk_requirement(req) == "claude-code-sdk>=0.0.25,<0.1"


def test_sdk_requirement_falls_back_to_the_bare_name(tmp_path):
    req = tmp_path / "requirements.txt"
    req.write_text("flask\n", encoding="utf-8")
    assert rs.sdk_requirement(req) == "claude-code-sdk"
    assert rs.sdk_requirement(tmp_path / "missing.txt") == "claude-code-sdk"


def test_the_real_requirements_file_has_a_ceiling_matching_the_tested_range():
    spec = rs.sdk_requirement()
    assert spec.startswith("claude-code-sdk>=")
    assert "<" in spec, "requirements.txt lost the claude-code-sdk ceiling"
    # The ceiling and daemon/sdk_patches.TESTED_SDK_RANGE move together.
    from daemon.sdk_patches import TESTED_SDK_RANGE
    high = ".".join(str(p) for p in TESTED_SDK_RANGE[1])
    assert spec.endswith("<" + high), (spec, TESTED_SDK_RANGE)


def test_upgrade_sdk_safely_hands_pip_the_full_requirement():
    """The daily upgrade must not be a bare ``--upgrade claude-code-sdk``."""
    from app import update_safety

    seen = []

    def runner(cmd, timeout):
        seen.append(list(cmd))
        if "show" in cmd:
            return 0, "Name: claude-code-sdk\nVersion: 0.0.25\n"
        if "--dry-run" in cmd:
            return 0, "Would install claude-code-sdk-0.0.26\n"
        if "check" in cmd:
            return 0, "No broken requirements found."
        return 0, "Successfully installed claude-code-sdk-0.0.26"

    result = update_safety.upgrade_sdk_safely(
        interpreter="py.exe", runner=runner, env={"VIBENODE_UPDATE_ISOLATED": "1"},
        requirement="claude-code-sdk>=0.0.25,<0.1")
    installs = [c for c in seen if "install" in c]
    assert installs, "pip install never ran"
    for cmd in installs:
        assert cmd[-1] == "claude-code-sdk>=0.0.25,<0.1"
        assert "claude-code-sdk" not in cmd[:-1]
    assert result["status"] in (update_safety.STATUS_UPGRADED, update_safety.STATUS_NO_CHANGE)


def test_upgrade_sdk_safely_without_a_requirement_keeps_the_old_target():
    from app import update_safety

    seen = []

    def runner(cmd, timeout):
        seen.append(list(cmd))
        if "show" in cmd:
            return 0, "Version: 0.0.25\n"
        return 0, ""

    update_safety.upgrade_sdk_safely(interpreter="py.exe", runner=runner,
                                     env={"VIBENODE_UPDATE_ISOLATED": "1"})
    assert any(c[-1] == "claude-code-sdk" for c in seen if "--dry-run" in c)


# ── the stamp file is gitignored ──────────────────────────────────────────

def test_stamp_lives_in_data_and_is_gitignored():
    assert rs.STAMP == rs.ROOT / "data" / "requirements.stamp"
    gitignore = (rs.ROOT / ".gitignore").read_text(encoding="utf-8").splitlines()
    assert "data/requirements.stamp" in gitignore
