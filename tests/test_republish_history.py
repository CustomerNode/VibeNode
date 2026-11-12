"""Re-scrubbing history is one command, and users adopt it on their own.

``scripts/republish_history.py`` replaces the published history with a clean
orphan root (after refusing if the scanner would flag anything), and the
already-shipped Update auto-adopt (``web.git_ops._adopt_republished_history``)
moves an existing clone onto it with no manual step. These tests drive real git
repositories (no network) through the whole loop.
"""
from __future__ import annotations

import importlib.util
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
_spec = importlib.util.spec_from_file_location("republish_history", ROOT / "scripts" / "republish_history.py")
rh = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(rh)

from web import git_ops


def _git(d, *a, check=True):
    return subprocess.run(["git", "-C", str(d), *a], capture_output=True, text=True, check=check)


def _commit(d, name, body="x\n"):
    (d / name).write_text(body)
    _git(d, "add", "-A")
    _git(d, "-c", "user.email=t@example.org", "-c", "user.name=t", "commit", "-q", "-m", name)


def _maintainer_and_origin(tmp_path):
    origin = tmp_path / "origin.git"
    subprocess.run(["git", "init", "--bare", "-q", "-b", "main", str(origin)], check=True)
    work = tmp_path / "maint"
    work.mkdir()
    _git(work, "init", "-q", "-b", "main")
    _commit(work, "README.md", "hello\n")
    _commit(work, "code.py", "print('ok')\n")        # a second commit: real history
    _git(work, "remote", "add", "origin", str(origin))
    _git(work, "push", "-q", "origin", "main")
    _git(work, "remote", "set-head", "origin", "main")
    return origin, work


def test_republish_builds_a_clean_orphan_root_and_pushes(tmp_path):
    origin, work = _maintainer_and_origin(tmp_path)
    before = _git(work, "rev-list", "--count", "HEAD").stdout.strip()
    assert before == "2"

    res = rh.republish(work, message="VibeNode", push=True)
    assert res["ok"] and res["pushed"]
    # The new main is an orphan: exactly one commit, no parents.
    assert _git(work, "rev-list", "--count", "HEAD").stdout.strip() == "1"
    assert _git(work, "rev-list", "--parents", "-1", "HEAD").stdout.split() == [res["new"]]
    # The tree is preserved.
    assert (work / "README.md").read_text() == "hello\n"
    assert (work / "code.py").read_text() == "print('ok')\n"
    # The old tip is on a backup branch, and origin matches the new root.
    assert res["backup"] in _git(work, "branch").stdout
    _git(work, "fetch", "-q", "origin")
    assert _git(work, "rev-parse", "origin/main").stdout.strip() == res["new"]


def test_republish_refuses_when_the_scanner_would_flag_something(tmp_path):
    origin, work = _maintainer_and_origin(tmp_path)
    # A real-looking email is caught by the scanner -> republish must refuse.
    (work / "leak.py").write_text("contact = 'jdoe@gmail" + ".com'\n")
    _commit(work, "leak.py", (work / "leak.py").read_text())
    tip = _git(work, "rev-parse", "HEAD").stdout.strip()

    res = rh.republish(work, message="VibeNode", push=True)
    assert res["ok"] is False
    assert any(f["type"] == "Email Address" for f in res["findings"])
    # Nothing was rewritten or pushed.
    assert _git(work, "rev-parse", "HEAD").stdout.strip() == tip
    assert _git(work, "rev-list", "--count", "HEAD").stdout.strip() == "3"


def test_a_user_clone_adopts_the_republished_history_on_update(tmp_path, monkeypatch):
    origin, work = _maintainer_and_origin(tmp_path)
    # A user cloned the OLD history and has a local edit.
    user = tmp_path / "user"
    subprocess.run(["git", "clone", "-q", str(origin), str(user)], check=True)
    (user / "README.md").write_text("hello\nlocal note\n")

    # We re-scrub and republish (one command).
    assert rh.republish(work, message="VibeNode", push=True)["ok"]

    # The user clicks Update: do_git_sync detects the unrelated history and adopts it.
    monkeypatch.setattr(git_ops, "_run_post_update", lambda proj: True)
    monkeypatch.setattr(git_ops, "_install_pulled_requirements", lambda proj: {"pip_ok": True, "pip_installed": 0})
    monkeypatch.setattr(git_ops, "_default_branch", lambda proj: "main")

    assert git_ops._remote_history_is_unrelated(user, "main") is True
    res = git_ops._adopt_republished_history(user, "main")
    assert res["ok"]
    # The user is now ON the republished root, with a backup of their old copy.
    assert _git(user, "rev-parse", "HEAD").stdout.strip() == _git(user, "rev-parse", "origin/main").stdout.strip()
    assert _git(user, "rev-list", "--count", "HEAD").stdout.strip() == "1"
    assert "before-republish-" in _git(user, "branch").stdout
