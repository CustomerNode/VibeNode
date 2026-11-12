"""Git itself runs the Publish scan on commits and pushes (src/web/git_hook.py).

Real commits and pushes in throwaway repositories, through the tracked hooks.
Every word here is made up.
"""
from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
HOOKS = ROOT / "scripts" / "git-hooks"
TYPED = "the copper teapot on the third shelf keeps tipping over"


def _git(cwd, *args, env=None):
    return subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, env=env)


@pytest.fixture
def repo(tmp_path):
    projects = tmp_path / "projects" / "proj-a"
    projects.mkdir(parents=True)
    (projects / "s1.jsonl").write_text(
        json.dumps({"type": "user", "message": {"role": "user", "content": TYPED}}) + "\n", encoding="utf-8")
    env = dict(os.environ, VIBENODE_USER_WORDS_GUARD="1",
               VIBENODE_USER_WORDS_FILE=str(tmp_path / "index.json"),
               VIBENODE_TRANSCRIPTS_DIR=str(tmp_path / "projects"))
    w = tmp_path / "w"
    w.mkdir()
    for args in (["init", "-q"], ["config", "user.email", "t@example.com"], ["config", "user.name", "T"],
                 ["config", "core.hooksPath", HOOKS.as_posix()]):
        _git(w, *args)
    return w, env


def _commit(w, env, text, msg="a plain message", *extra):
    (w / "a.py").write_text(text, encoding="utf-8")
    _git(w, "add", "a.py", env=env)
    return _git(w, "commit", "-q", "-m", msg, *extra, env=env)


def test_a_commit_holding_typed_words_is_refused(repo):
    w, env = repo
    r = _commit(w, env, "# note: copper teapot on the third shelf keeps tipping\n")
    assert r.returncode != 0 and "Your Own Words" in r.stderr


def test_a_neutral_commit_goes_through(repo):
    w, env = repo
    assert _commit(w, env, "# a neutral note about the shelf layout\n").returncode == 0


def test_a_commit_message_holding_typed_words_is_refused(repo):
    w, env = repo
    r = _commit(w, env, "x = 1\n", "the copper teapot on the third shelf keeps tipping")
    assert r.returncode != 0 and "commit message" in r.stderr


def test_a_push_of_a_commit_that_skipped_the_check_is_refused(repo, tmp_path):
    w, env = repo
    _git(tmp_path, "init", "-q", "--bare", "remote.git")
    # The message was typed (and indexed by an earlier refused commit) before
    # someone committed it while skipping the commit check.
    assert _commit(w, env, f"# {TYPED}\n").returncode != 0
    assert _commit(w, env, f"# {TYPED}\n", "init", "--no-verify").returncode == 0
    _git(w, "remote", "add", "origin", "../remote.git")
    r = _git(w, "push", "origin", "HEAD:main", env=env)
    assert r.returncode != 0 and "pre-push" in r.stderr


def test_the_hooks_keep_unix_line_endings_and_are_executable():
    attrs = (ROOT / ".gitattributes").read_text(encoding="utf-8")
    assert "scripts/git-hooks/* text eol=lf" in attrs
    modes = _git(ROOT, "ls-files", "-s", "scripts/git-hooks").stdout.split("\n")
    assert len([m for m in modes if m.startswith("100755")]) == 3


def test_install_leaves_a_developers_own_hooks_alone(tmp_path):
    import sys
    sys.path.insert(0, str(ROOT / "src"))
    from web.git_hook import ensure_installed, HOOKS_DIR
    r = tmp_path / "r"
    r.mkdir()
    _git(r, "init", "-q")
    (r / HOOKS_DIR).mkdir(parents=True)
    assert ensure_installed(r) == "installed now"
    assert _git(r, "config", "--get", "core.hooksPath").stdout.strip() == HOOKS_DIR
    other = tmp_path / "o"
    other.mkdir()
    _git(other, "init", "-q")
    _git(other, "config", "core.hooksPath", "my-hooks")
    assert ensure_installed(other).startswith("left alone")


def test_a_push_never_scans_the_scanners_own_examples(repo, tmp_path):
    w, env = repo
    _git(tmp_path, "init", "-q", "--bare", "remote2.git")
    assert _commit(w, env, "# " + TYPED + "\n").returncode != 0      # indexes the words
    (w / "a.py").write_text("x = 1\n", encoding="utf-8")
    (w / "src" / "web").mkdir(parents=True)
    (w / "src" / "web" / "git_scanner.py").write_text('EXAMPLE = "' + TYPED + '"\n', encoding="utf-8")
    _git(w, "add", "-A", env=env)
    assert _git(w, "commit", "-q", "--no-verify", "-m", "start", env=env).returncode == 0
    _git(w, "remote", "add", "origin2", "../remote2.git")
    r = _git(w, "push", "origin2", "HEAD:main", env=env)
    assert r.returncode == 0, r.stderr
