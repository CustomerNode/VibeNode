"""Honest publish gate: the commit says what changed and whether tests ran, the
Update installs the dependencies it pulls, and the modes are labelled for what
they do (2026-10-23).

74 of the 100 commits before this were "Update VibeNode <timestamp>"; the gate
could be skipped with nothing recording it; "Full" claimed to include the e2e
suite that pytest.ini's addopts ignores in both modes.
"""

import subprocess
from unittest.mock import MagicMock

import pytest

from app import git_ops


# ── Commit message ────────────────────────────────────────────────────────

class TestCommitMessage:
    def test_user_summary_then_gate_line(self):
        msg = git_ops.build_commit_message(
            "Fix the Sleep button", {"mode": "fast", "passed": 412, "skipped": 3,
                                     "failed": 0, "seconds": 87}, ["a.py"])
        assert msg == "Fix the Sleep button\n\nPublish gate: fast (412 passed, 3 skipped, 0 failed, 87s)\n"

    def test_empty_summary_is_generated_from_the_staged_areas(self):
        paths = ["app/routes/git_api.py", "app/git_ops.py", "static/js/git-sync.js",
                 "tests/test_a.py", "tests/test_b.py", "run.py"]
        msg = git_ops.build_commit_message("", {"mode": "full", "passed": 10,
                                                "skipped": 0, "failed": 0, "seconds": 5}, paths)
        first, blank, gate, _ = msg.split("\n")
        assert first == "Update app/routes, app, static/js, tests, run.py (6 files)"
        assert blank == ""
        assert gate == "Publish gate: full (10 passed, 0 skipped, 0 failed, 5s)"

    def test_skipped_gate_is_recorded_as_such(self):
        assert git_ops.format_publish_gate({"mode": "skipped"}) == "Publish gate: skipped (no tests run)"
        assert git_ops.format_publish_gate(None) == "Publish gate: skipped (no tests run)"
        assert git_ops.format_publish_gate({"mode": "bogus"}) == "Publish gate: skipped (no tests run)"

    def test_publish_anyway_after_failures_says_so(self):
        line = git_ops.format_publish_gate({"mode": "fast", "passed": 400, "skipped": 1,
                                            "failed": 2, "seconds": 90, "anyway": True})
        assert line == "Publish gate: fast (400 passed, 1 skipped, 2 failed, 90s), published anyway"

    def test_bad_counts_do_not_break_the_message(self):
        line = git_ops.format_publish_gate({"mode": "full", "passed": "x", "seconds": None})
        assert line == "Publish gate: full (0 passed, 0 skipped, 0 failed, 0s)"

    def test_summary_is_one_line_and_capped(self):
        msg = git_ops.build_commit_message("a\nb   c" + "x" * 200, {"mode": "skipped"}, [])
        first = msg.split("\n")[0]
        assert "\n" not in first and first.startswith("a b cxxx") and len(first) <= 120

    def test_area_rules(self):
        assert git_ops.summarize_staged([]) == "Update VibeNode"
        assert git_ops.summarize_staged(["README.md"]) == "Update README.md (1 file)"
        assert git_ops.summarize_staged(["daemon/a.py", "daemon/b.py"]) == "Update daemon (2 files)"
        many = ["d%d/x/y.py" % i for i in range(8)]
        assert git_ops.summarize_staged(many) == "Update d0/x, d1/x, d2/x, d3/x, d4/x, d5/x, … (8 files)"

    def test_the_push_commits_with_the_built_message(self, tmp_path, monkeypatch):
        (tmp_path / ".git").mkdir()
        monkeypatch.setattr(git_ops, "_VIBENODE_DIR", tmp_path)
        monkeypatch.setattr(git_ops, "_current_branch", lambda *a: "main")
        monkeypatch.setattr(git_ops, "_default_branch", lambda *a: "main")
        monkeypatch.setattr("app.git_scanner.scan_staged_files",
                            lambda *a, **kw: {"ok": True, "summary": "clean", "files_scanned": 1})
        monkeypatch.setattr(git_ops, "_staged_paths", lambda proj: ["app/git_ops.py", "tests/t.py"])

        commits = []

        def run(cmd, **kw):
            if "commit" in cmd:
                commits.append(cmd[cmd.index("-m") + 1])
            if "status" in cmd:
                return MagicMock(stdout=" M app/git_ops.py", returncode=0, stderr="")
            return MagicMock(stdout="", returncode=0, stderr="")
        monkeypatch.setattr(subprocess, "run", run)

        res = git_ops.do_git_sync("push", summary="", gate={"mode": "fast", "passed": 5,
                                                          "skipped": 0, "failed": 0, "seconds": 3})
        assert res["ok"] is True
        assert commits == ["Update app, tests (2 files)\n\nPublish gate: fast (5 passed, 0 skipped, 0 failed, 3s)\n"]

    def test_the_route_passes_summary_and_gate_through(self):
        from flask import Flask
        from app.routes import git_api
        app = Flask(__name__)
        app.register_blueprint(git_api.bp)
        seen = {}

        def fake_sync(action, confirm_branch=False, summary="", gate=None):
            seen.update(action=action, summary=summary, gate=gate)
            return {"ok": True, "messages": []}
        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(git_api, "do_git_sync", fake_sync)
            r = app.test_client().post("/api/git-sync", json={
                "action": "both", "summary": "Tidy", "gate": {"mode": "skipped"}})
        assert r.status_code == 200
        assert seen == {"action": "both", "summary": "Tidy", "gate": {"mode": "skipped"}}


# ── The Update installs what it pulls ─────────────────────────────────────

class TestPullInstallsRequirements:
    def _repo(self, tmp_path, monkeypatch, blobs):
        (tmp_path / ".git").mkdir()
        monkeypatch.setattr(git_ops, "_VIBENODE_DIR", tmp_path)
        monkeypatch.setattr(git_ops, "_current_branch", lambda *a: "main")
        monkeypatch.setattr(git_ops, "_default_branch", lambda *a: "main")
        it = iter(blobs)
        monkeypatch.setattr(git_ops, "_requirements_blob", lambda proj: next(it))
        # The stash-pop clash check is its own git call; no clash here.
        monkeypatch.setattr(git_ops, "_unmerged_paths", lambda *a: [])
        monkeypatch.setattr(subprocess, "run",
                            lambda cmd, **kw: MagicMock(stdout="Updating abc..def", returncode=0, stderr=""))

    def test_pip_runs_when_the_pull_changed_requirements(self, tmp_path, monkeypatch):
        self._repo(tmp_path, monkeypatch, ["blob-before", "blob-after"])
        pips = []
        monkeypatch.setattr(git_ops, "_install_pulled_requirements", lambda proj: (
            pips.append(proj) or {"pip_ran": True, "pip_ok": True, "pip_installed": 2,
                                  "pip_output_tail": "Successfully installed a-1 b-2"}))

        res = git_ops.do_git_sync("pull")

        assert pips == [tmp_path]
        assert res["ok"] is True and res["pip_ran"] is True and res["pip_ok"] is True
        assert res["pip_installed"] == 2
        assert any("Installed 2 new or updated dependencies" in m for m in res["messages"])

    def test_pip_does_not_run_when_requirements_are_unchanged(self, tmp_path, monkeypatch):
        self._repo(tmp_path, monkeypatch, ["same", "same"])
        monkeypatch.setattr(git_ops, "_install_pulled_requirements",
                            lambda proj: pytest.fail("pip ran for an unchanged requirements.txt"))

        res = git_ops.do_git_sync("pull")

        assert res["ok"] is True and "pip_ran" not in res

    def test_a_pip_failure_never_undoes_the_pull(self, tmp_path, monkeypatch):
        self._repo(tmp_path, monkeypatch, ["before", "after"])
        monkeypatch.setattr(git_ops, "_install_pulled_requirements", lambda proj: {
            "pip_ran": True, "pip_ok": False, "pip_installed": 0,
            "pip_output_tail": "ERROR: No matching distribution found for nope"})

        res = git_ops.do_git_sync("pull")

        assert res["ok"] is True, "the code update must land even when pip fails"
        assert res["pip_ran"] is True and res["pip_ok"] is False
        assert "No matching distribution" in res["pip_output_tail"]
        assert any("Dependency install failed" in m for m in res["messages"])

    def test_the_real_installer_uses_the_servers_interpreter(self, tmp_path, monkeypatch):
        import sys
        from app import requirements_sync
        seen = []
        monkeypatch.setattr(requirements_sync, "install_requirements",
                            lambda python, req, **kw: seen.append((python, req)) or {"pip_ran": True, "pip_ok": True})
        git_ops._install_pulled_requirements(tmp_path)
        assert seen == [(sys.executable, tmp_path / "requirements.txt")]


# ── Honest mode labels ────────────────────────────────────────────────────

class TestModeLabels:
    def test_both_modes_run_the_same_files_and_the_labels_say_so(self):
        import app.routes.test_api as ta
        assert set(ta.MODES) == {"fast", "full"}
        # pytest.ini already ignores tests/e2e, so "full" must not claim e2e.
        ini = (ta._REPO_ROOT / "pytest.ini").read_text(encoding="utf-8")
        assert "--ignore=tests/e2e" in ini
        assert "e2e" not in ta.MODES["fast"]["label"].lower().replace("e2e suite runs in ci", "")
        assert "CI" in ta.MODES["full"]["label"]
        assert "60s" in ta.MODES["fast"]["label"] and "120s" in ta.MODES["full"]["label"]
        cp = "--ignore-glob=tests/test_code_pipeline*.py"
        assert ta.MODES["fast"]["pytest_args"] == ["--ignore=tests/e2e", cp, "--timeout=60"]
        assert ta.MODES["full"]["pytest_args"] == [cp, "--timeout=120"]

    def test_code_pipeline_tests_never_gate_a_publish_but_ci_runs_them(self):
        """Code Pipeline is a separate tool; its tests do not gate a VibeNode
        Update (owner, 2026-10-28).  CI keeps running the whole suite."""
        import app.routes.test_api as ta
        for mode in ta.MODES.values():
            assert ta.CODE_PIPELINE_TESTS in mode["pytest_args"]
            assert "Code Pipeline tests run in CI" in mode["label"]
        ci = (ta._REPO_ROOT / ".github" / "workflows" / "unit-tests.yml").read_text(encoding="utf-8")
        assert "test_code_pipeline" not in ci, "CI must keep running the Code Pipeline tests"

    def test_the_docstring_no_longer_claims_full_includes_e2e(self):
        import app.routes.test_api as ta
        assert "everything including e2e" not in (ta.__doc__ or "")

    def test_test_modes_endpoint(self):
        from flask import Flask
        import app.routes.test_api as ta
        app = Flask(__name__)
        app.register_blueprint(ta.bp)
        body = app.test_client().get("/api/test-modes").get_json()
        assert body["modes"]["full"]["label"] == ta.MODES["full"]["label"]
        assert body["modes"]["fast"]["pytest_args"] == ta.MODES["fast"]["pytest_args"]

    def test_the_dialog_fallback_matches_the_server(self):
        import re
        import app.routes.test_api as ta
        js = (ta._REPO_ROOT / "static" / "js" / "git-sync.js").read_text(encoding="utf-8")
        for mode in ("fast", "full"):
            m = re.search(mode + r": \{label: '([^']+)'\}", js)
            assert m, "git-sync.js lost the %s fallback label" % mode
            assert m.group(1) == ta.MODES[mode]["label"]


# ── Workflow ──────────────────────────────────────────────────────────────

def test_the_unit_suite_has_a_ci_workflow():
    import app.routes.test_api as ta
    wf = ta._REPO_ROOT / ".github" / "workflows" / "unit-tests.yml"
    text = wf.read_text(encoding="utf-8")
    assert "pull_request" in text and "branches: [main]" in text
    assert "'3.11'" in text and "'3.12'" in text
    assert "python -m pytest -q -p no:cacheprovider --timeout=120" in text
    assert "node tests/test_thread_scroll.js" in text
    assert "node tests/test_thread_scroll_classic.js" in text
    assert "playwright install --with-deps chromium" in text
    assert "upload-artifact" in text and "junit" in text
