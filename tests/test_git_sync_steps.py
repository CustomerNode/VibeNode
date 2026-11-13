"""An Update / Publish reports each step as it starts, so the dialog fills its
ring step by step instead of spinning (src/web/git_ops.py ``sync_steps`` and
``progress``, src/web/routes/git_api.py /api/git-sync-stream)."""

import json
import subprocess

import pytest
from flask import Flask

from web import git_ops


def test_the_steps_follow_the_action():
    keys = lambda a: [k for k, _ in git_ops.sync_steps(a)]
    assert keys("pull") == ["pull", "restore", "finish"]
    assert keys("push") == ["scan", "save", "upload"]
    assert keys("both") == keys("pull") + keys("push")
    assert all(label for _, label in git_ops.sync_steps("both"))


def _fake_repo(tmp_path, monkeypatch):
    """A repo where every git call succeeds, recording nothing."""
    (tmp_path / ".git").mkdir()
    monkeypatch.setattr(git_ops, "_VIBENODE_DIR", tmp_path)
    monkeypatch.setattr(git_ops, "_current_branch", lambda *a: "main")
    monkeypatch.setattr(git_ops, "_default_branch", lambda *a: "main")
    monkeypatch.setattr(git_ops, "_unmerged_paths", lambda *a: [])
    monkeypatch.setattr(git_ops, "_remote_history_is_unrelated", lambda *a: False)
    monkeypatch.setattr(git_ops, "_requirements_blob", lambda *a: "same")
    monkeypatch.setattr(git_ops, "_run_post_update", lambda *a: True)
    from web import git_scanner
    monkeypatch.setattr(git_scanner, "scan_staged_files", lambda *a, **k: {"ok": True, "summary": ""})
    monkeypatch.setattr(git_scanner, "scan_outgoing_commits", lambda *a, **k: [])

    def run(cmd, *a, **k):
        out = "Already up to date." if "pull" in cmd else ""
        return subprocess.CompletedProcess(cmd, 0, stdout=out, stderr="")
    monkeypatch.setattr(git_ops.subprocess, "run", run)


def test_a_sync_reports_every_step_in_order(tmp_path, monkeypatch):
    _fake_repo(tmp_path, monkeypatch)
    seen = []
    r = git_ops.do_git_sync("both", progress=seen.append)
    assert r["ok"], r
    assert seen == ["pull", "restore", "finish", "scan", "save", "upload"]


def test_a_broken_progress_reader_never_stops_the_sync(tmp_path, monkeypatch):
    _fake_repo(tmp_path, monkeypatch)

    def boom(key):
        raise RuntimeError("reader went away")
    assert git_ops.do_git_sync("pull", progress=boom)["ok"]


def test_the_stream_names_the_steps_then_reports_them_then_the_result():
    from web.routes import git_api
    app = Flask(__name__)
    app.register_blueprint(git_api.bp)
    seen = {}

    def fake_sync(action, confirm_branch=False, summary="", gate=None, progress=None):
        seen.update(action=action, summary=summary, gate=gate)
        for key in ("pull", "restore", "finish"):
            progress(key)
        return {"ok": True, "messages": ["Pulled."]}
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(git_api, "do_git_sync", fake_sync)
        mp.setattr(git_api, "get_git_cache", lambda: {"ahead": 0, "behind": 0})
        r = app.test_client().post("/api/git-sync-stream", json={"action": "pull", "summary": "x"})
        lines = [json.loads(l) for l in r.get_data(as_text=True).splitlines() if l.strip()]
    assert r.status_code == 200 and "ndjson" in r.headers["Content-Type"]
    assert lines[0] == {"type": "steps", "steps": [{"key": k, "label": l} for k, l in git_ops.sync_steps("pull")]}
    assert [e["key"] for e in lines[1:-1]] == ["pull", "restore", "finish"]
    assert lines[-1] == {"type": "done", "result": {"ok": True, "messages": ["Pulled."],
                                                    "git_status": {"ahead": 0, "behind": 0}}}
    assert seen == {"action": "pull", "summary": "x", "gate": None}


def test_a_crash_in_the_sync_still_ends_the_stream_with_an_answer():
    from web.routes import git_api
    app = Flask(__name__)
    app.register_blueprint(git_api.bp)

    def fake_sync(*a, **k):
        raise RuntimeError("disk gone")
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(git_api, "do_git_sync", fake_sync)
        mp.setattr(git_api, "get_git_cache", lambda: {})
        r = app.test_client().post("/api/git-sync-stream", json={"action": "both"})
        last = json.loads(r.get_data(as_text=True).strip().splitlines()[-1])
    assert last["type"] == "done" and last["result"]["ok"] is False
    assert "disk gone" in last["result"]["messages"][0]
