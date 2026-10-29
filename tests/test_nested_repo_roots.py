"""A wrapper folder's repos are counted once, worktrees included.

The CustomerNode project folder holds one repo and 14 linked worktrees
of it.  The pre-turn mtime scan counted each worktree as its own repo,
listed 143,442 files instead of ~10,800, and hit its 2s budget on every
turn (2026-10-28).  These tests build the layout with real git.
"""
import shutil
import subprocess
from pathlib import Path

import pytest

from daemon.session_manager import SessionManager

pytestmark = pytest.mark.skipif(shutil.which("git") is None,
                                reason="git not installed")


def _git(cwd: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=str(cwd), check=True,
                   capture_output=True)


def _repo(path: Path, files: dict) -> Path:
    path.mkdir(parents=True)
    _git(path, "init", "-q")
    _git(path, "config", "user.email", "t@example.invalid")
    _git(path, "config", "user.name", "t")
    for name, text in files.items():
        (path / name).write_text(text, encoding="utf-8")
    _git(path, "add", "-A")
    _git(path, "commit", "-q", "-m", "init")
    return path


def test_worktrees_of_a_listed_repo_are_skipped(tmp_path):
    wrapper = tmp_path / "wrapper"
    main = _repo(wrapper / "main_repo", {"a.py": "1", "b.py": "2"})
    for name in ("wt_one", "wt_two", ".preflight_copy"):
        _git(main, "worktree", "add", "-q", "--detach", str(wrapper / name))
    other = _repo(wrapper / "other_repo", {"c.py": "3"})

    roots = SessionManager._nested_repo_roots(wrapper)
    assert sorted(r.name for r in roots) == ["main_repo", "other_repo"]

    sm = SessionManager.__new__(SessionManager)
    files = sm._git_ls_files(wrapper)
    names = sorted(str(f.relative_to(wrapper)).replace("\\", "/") for f in files)
    assert names == ["main_repo/a.py", "main_repo/b.py", "other_repo/c.py"]
    assert other in roots


def test_a_worktree_of_a_repo_outside_the_folder_is_kept(tmp_path):
    outside = _repo(tmp_path / "elsewhere", {"x.py": "1"})
    wrapper = tmp_path / "wrapper"
    wrapper.mkdir()
    _git(outside, "worktree", "add", "-q", "--detach", str(wrapper / "wt_outside"))

    roots = SessionManager._nested_repo_roots(wrapper)
    assert [r.name for r in roots] == ["wt_outside"]


def test_a_folder_without_repos_lists_nothing(tmp_path):
    (tmp_path / "plain").mkdir()
    (tmp_path / "file.txt").write_text("x", encoding="utf-8")
    assert SessionManager._nested_repo_roots(tmp_path) == []
    assert SessionManager._nested_repo_roots(tmp_path / "missing") == []
