"""Publish-gate test runner (app/routes/test_api.py), added 2026-10-01.

Covers the three changes behind "the Publish test run is slow, its errors
vanish, and there is no way to hand them to Claude":
  * parallel workers when pytest-xdist is installed (serial fallback),
  * exact pass/fail counts from the JUnit report,
  * the full output saved to logs/last_test_run.log for the "Fix errors" session.
"""

import json
import sys

import pytest
from flask import Flask

import app.routes.test_api as ta


JUNIT_2_FAIL = """<?xml version="1.0" encoding="utf-8"?>
<testsuites><testsuite name="pytest" errors="1" failures="2" skipped="3" tests="20" time="1.0"></testsuite></testsuites>
"""


@pytest.fixture
def paths(tmp_path, monkeypatch):
    monkeypatch.setattr(ta, "LAST_RUN_LOG", tmp_path / "last_test_run.log")
    monkeypatch.setattr(ta, "LAST_RUN_JUNIT", tmp_path / "last_test_run.xml")
    return tmp_path


class TestParallelArgs:
    def test_uses_xdist_when_installed(self, monkeypatch):
        monkeypatch.setitem(sys.modules, "xdist", object())
        monkeypatch.setattr("os.cpu_count", lambda: 12)
        assert ta._parallel_args() == ["-n", "8", "--dist", "loadfile"]

    def test_worker_count_bounds(self, monkeypatch):
        monkeypatch.setitem(sys.modules, "xdist", object())
        monkeypatch.setattr("os.cpu_count", lambda: 2)
        assert ta._parallel_args()[:2] == ["-n", "2"]
        monkeypatch.setattr("os.cpu_count", lambda: 64)
        assert ta._parallel_args()[:2] == ["-n", "8"]

    def test_serial_fallback_without_xdist(self, monkeypatch):
        monkeypatch.setitem(sys.modules, "xdist", None)  # import raises
        assert ta._parallel_args() == []


class TestConsolePython:
    """The web server runs under pythonw; tests must run under python.exe."""

    def test_swaps_pythonw_for_sibling_python(self, tmp_path, monkeypatch):
        (tmp_path / "pythonw.exe").write_text("")
        (tmp_path / "python.exe").write_text("")
        monkeypatch.setattr(ta.sys, "executable", str(tmp_path / "pythonw.exe"))
        assert ta._console_python() == str(tmp_path / "python.exe")

    def test_keeps_pythonw_if_no_sibling(self, tmp_path, monkeypatch):
        (tmp_path / "pythonw.exe").write_text("")
        monkeypatch.setattr(ta.sys, "executable", str(tmp_path / "pythonw.exe"))
        assert ta._console_python() == str(tmp_path / "pythonw.exe")

    def test_leaves_python_alone(self, tmp_path, monkeypatch):
        monkeypatch.setattr(ta.sys, "executable", str(tmp_path / "python.exe"))
        assert ta._console_python() == str(tmp_path / "python.exe")


class TestJunitCounts:
    def test_exact_counts(self, paths):
        ta.LAST_RUN_JUNIT.write_text(JUNIT_2_FAIL, encoding="utf-8")
        assert ta._junit_counts() == {"passed": 14, "failed": 2, "errors": 1, "skipped": 3}

    def test_missing_or_corrupt_report_is_none(self, paths):
        assert ta._junit_counts() is None
        ta.LAST_RUN_JUNIT.write_text("<not xml", encoding="utf-8")
        assert ta._junit_counts() is None


class TestSaveRunLog:
    def test_writes_header_and_every_line(self, paths):
        p = ta._save_run_log("full", ["python", "-m", "pytest"], ["a", "FAILED x"], 1)
        text = ta.LAST_RUN_LOG.read_text(encoding="utf-8")
        assert p == str(ta.LAST_RUN_LOG)
        assert "mode=full, exit=1" in text and "FAILED x" in text

    def test_lives_under_gitignored_logs(self):
        # The real path must stay inside logs/ (gitignored): run output can
        # contain local paths and must never be published.
        assert ta.LAST_RUN_LOG.parent.name == "logs"
        gitignore = (ta._REPO_ROOT / ".gitignore").read_text(encoding="utf-8")
        assert "logs/" in gitignore.splitlines()


class _FakeProc:
    def __init__(self, lines, rc, junit_path):
        self.stdout = iter(l + "\n" for l in lines)
        self.returncode = rc
        self.pid = 4242
        junit_path.write_text(JUNIT_2_FAIL, encoding="utf-8")

    def poll(self):
        return self.returncode

    def wait(self, timeout=None):
        return self.returncode

    def terminate(self):
        pass

    def kill(self):
        pass


class TestEndpoint:
    def test_failed_run_reports_exact_counts_and_log(self, paths, monkeypatch):
        captured = {}
        real_popen = ta.subprocess.Popen

        def fake_popen(cmd, *args, **kw):
            # ta.subprocess IS the global subprocess module, so this patch is
            # process-wide.  Background threads left by other test files in
            # the same xdist worker (e.g. app.git_ops's git refresh) can call
            # Popen mid-request; intercept only the pytest launch under test.
            if not (isinstance(cmd, list) and "pytest" in cmd):
                return real_popen(cmd, *args, **kw)
            captured["cmd"] = cmd
            captured["kw"] = kw
            return _FakeProc(["..F.", "FAILED tests/test_x.py::test_y - boom"], 1, ta.LAST_RUN_JUNIT)

        monkeypatch.setattr(ta.subprocess, "Popen", fake_popen)
        app = Flask(__name__)
        app.register_blueprint(ta.bp)
        resp = app.test_client().post("/api/run-tests", json={"mode": "full"})
        events = [json.loads(chunk[len("data: "):])
                  for chunk in resp.get_data(as_text=True).split("\n\n")
                  if chunk.startswith("data: ")]
        done = events[-1]
        assert done["type"] == "done" and done["ok"] is False
        # Exact counts from the JUnit report, not the dot count.
        assert (done["passed"], done["failed"], done["errors"], done["skipped"]) == (14, 2, 1, 3)
        assert done["log_path"] == str(ta.LAST_RUN_LOG)
        assert "FAILED tests/test_x.py::test_y - boom" in ta.LAST_RUN_LOG.read_text(encoding="utf-8")
        assert "--junitxml" in captured["cmd"]
        # pythonw has no valid stdin; inheriting it breaks every subprocess a
        # test starts ("[WinError 6] The handle is invalid", 9 failures).
        assert captured["kw"].get("stdin") is ta.subprocess.DEVNULL
