"""Tests for app.git_ops — git cache and sync operations."""

import subprocess
from unittest.mock import MagicMock
import pytest


class TestGitCache:

    def test_get_git_cache_returns_dict(self):
        from app.git_ops import get_git_cache
        cache = get_git_cache()
        assert isinstance(cache, dict)
        assert "ahead" in cache
        assert "behind" in cache
        assert "uncommitted" in cache

    def test_get_git_cache_has_branch_fields(self):
        from app.git_ops import get_git_cache
        cache = get_git_cache()
        assert "branch" in cache
        assert "default_branch" in cache
        assert "on_default" in cache


class TestDoGitSync:

    def test_sync_no_git_repo(self, tmp_path, monkeypatch):
        from app import git_ops
        monkeypatch.setattr(git_ops, "_VIBENODE_DIR", tmp_path)
        result = git_ops.do_git_sync("pull")
        assert result["ok"] is False
        assert "no git repo" in result["messages"][0].lower()

    def test_sync_pull_success(self, tmp_path, monkeypatch):
        from app import git_ops
        # Create fake .git dir
        (tmp_path / ".git").mkdir()
        monkeypatch.setattr(git_ops, "_VIBENODE_DIR", tmp_path)
        # On the default branch → branch guard is a no-op.
        monkeypatch.setattr(git_ops, "_current_branch", lambda *a: "main")
        monkeypatch.setattr(git_ops, "_default_branch", lambda *a: "main")

        mock_run = MagicMock()
        # stash returns "No local changes"
        stash_result = MagicMock(stdout="No local changes to save", returncode=0)
        # pull returns success
        pull_result = MagicMock(stdout="Already up to date.", returncode=0, stderr="")
        # rev-list for cache update
        revlist_result = MagicMock(stdout="0\t0", returncode=0)
        status_result = MagicMock(stdout="", returncode=0)

        mock_run.side_effect = [stash_result, pull_result, revlist_result, status_result]
        monkeypatch.setattr(subprocess, "run", mock_run)

        result = git_ops.do_git_sync("pull")
        assert result["ok"] is True
        assert any("up to date" in m.lower() for m in result["messages"])

    def test_sync_push_with_scan_pass(self, tmp_path, monkeypatch):
        from app import git_ops
        (tmp_path / ".git").mkdir()
        monkeypatch.setattr(git_ops, "_VIBENODE_DIR", tmp_path)
        monkeypatch.setattr(git_ops, "_current_branch", lambda *a: "main")
        monkeypatch.setattr(git_ops, "_default_branch", lambda *a: "main")

        # Mock the security scanner to pass (imported inside function)
        monkeypatch.setattr("app.git_scanner.scan_staged_files",
                            lambda *a, **kw: {"ok": True, "summary": "clean", "files_scanned": 1})

        mock_run = MagicMock()
        # status --porcelain (no dirty files)
        status_result = MagicMock(stdout="", returncode=0)
        # push success
        push_result = MagicMock(stdout="", returncode=0, stderr="")

        mock_run.side_effect = [status_result, push_result]
        monkeypatch.setattr(subprocess, "run", mock_run)

        result = git_ops.do_git_sync("push")
        assert result["ok"] is True

    def test_sync_push_blocked_by_scan(self, tmp_path, monkeypatch):
        from app import git_ops
        (tmp_path / ".git").mkdir()
        monkeypatch.setattr(git_ops, "_VIBENODE_DIR", tmp_path)
        monkeypatch.setattr(git_ops, "_current_branch", lambda *a: "main")
        monkeypatch.setattr(git_ops, "_default_branch", lambda *a: "main")

        monkeypatch.setattr("app.git_scanner.scan_staged_files",
                            lambda *a, **kw: {"ok": False, "summary": "secret found",
                                        "files_scanned": 5, "findings": []})

        result = git_ops.do_git_sync("push")
        assert result["ok"] is False
        assert "scan" in str(result).lower()


class TestBranchGuard:
    """The sync button must not silently push to a non-default branch."""

    def test_blocked_on_non_default_branch(self, tmp_path, monkeypatch):
        from app import git_ops
        (tmp_path / ".git").mkdir()
        monkeypatch.setattr(git_ops, "_VIBENODE_DIR", tmp_path)
        monkeypatch.setattr(git_ops, "_current_branch", lambda *a: "feature-x")
        monkeypatch.setattr(git_ops, "_default_branch", lambda *a: "main")
        # No git command may run — the guard short-circuits first.
        no_git = MagicMock(side_effect=AssertionError("git ran despite branch guard"))
        monkeypatch.setattr(subprocess, "run", no_git)

        result = git_ops.do_git_sync("both")
        assert result["ok"] is False
        assert result["needs_branch_confirm"] is True
        assert result["branch"] == "feature-x"
        assert result["default_branch"] == "main"
        assert any("feature-x" in m for m in result["messages"])
        no_git.assert_not_called()

    def test_confirm_branch_bypasses_guard(self, tmp_path, monkeypatch):
        from app import git_ops
        (tmp_path / ".git").mkdir()
        monkeypatch.setattr(git_ops, "_VIBENODE_DIR", tmp_path)
        monkeypatch.setattr(git_ops, "_current_branch", lambda *a: "feature-x")
        monkeypatch.setattr(git_ops, "_default_branch", lambda *a: "main")
        monkeypatch.setattr("app.git_scanner.scan_staged_files",
                            lambda *a, **kw: {"ok": True, "summary": "clean", "files_scanned": 1})
        status_result = MagicMock(stdout="", returncode=0)   # no dirty files
        push_result = MagicMock(stdout="", returncode=0, stderr="")
        monkeypatch.setattr(subprocess, "run",
                            MagicMock(side_effect=[status_result, push_result]))

        result = git_ops.do_git_sync("push", confirm_branch=True)
        assert result["ok"] is True
        assert not result.get("needs_branch_confirm")

    def test_detached_head_is_gated(self, tmp_path, monkeypatch):
        from app import git_ops
        (tmp_path / ".git").mkdir()
        monkeypatch.setattr(git_ops, "_VIBENODE_DIR", tmp_path)
        monkeypatch.setattr(git_ops, "_current_branch", lambda *a: "")  # detached
        monkeypatch.setattr(git_ops, "_default_branch", lambda *a: "main")

        result = git_ops.do_git_sync("both")
        assert result.get("needs_branch_confirm") is True
        assert "detached" in result["messages"][0].lower()

    def test_not_gated_when_default_unknown(self, tmp_path, monkeypatch):
        # Fail open: if origin/HEAD isn't set we can't tell, so never block.
        from app import git_ops
        (tmp_path / ".git").mkdir()
        monkeypatch.setattr(git_ops, "_VIBENODE_DIR", tmp_path)
        monkeypatch.setattr(git_ops, "_current_branch", lambda *a: "whatever")
        monkeypatch.setattr(git_ops, "_default_branch", lambda *a: "")  # unknown
        monkeypatch.setattr("app.git_scanner.scan_staged_files",
                            lambda *a, **kw: {"ok": True, "summary": "clean", "files_scanned": 1})
        status_result = MagicMock(stdout="", returncode=0)
        push_result = MagicMock(stdout="", returncode=0, stderr="")
        monkeypatch.setattr(subprocess, "run",
                            MagicMock(side_effect=[status_result, push_result]))

        result = git_ops.do_git_sync("push")
        assert result["ok"] is True
        assert not result.get("needs_branch_confirm")
