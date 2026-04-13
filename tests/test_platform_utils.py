"""Tests for app.platform_utils — cross-platform helpers."""

import sys
import pytest
from pathlib import Path
from unittest.mock import patch, MagicMock


# ---------------------------------------------------------------------------
# default_project_roots()
# ---------------------------------------------------------------------------

class TestDefaultProjectRoots:

    def test_always_includes_documents_if_exists(self, tmp_path, monkeypatch):
        from app import platform_utils
        docs = tmp_path / "Documents"
        docs.mkdir()
        monkeypatch.setattr(Path, "home", lambda: tmp_path)
        roots = platform_utils.default_project_roots()
        assert docs in roots

    def test_always_includes_desktop_if_exists(self, tmp_path, monkeypatch):
        from app import platform_utils
        desktop = tmp_path / "Desktop"
        desktop.mkdir()
        monkeypatch.setattr(Path, "home", lambda: tmp_path)
        roots = platform_utils.default_project_roots()
        assert desktop in roots

    def test_excludes_nonexistent_dirs(self, tmp_path, monkeypatch):
        from app import platform_utils
        # Don't create any directories — all candidates should be filtered out
        monkeypatch.setattr(Path, "home", lambda: tmp_path)
        roots = platform_utils.default_project_roots()
        assert roots == []

    def test_windows_includes_source_repos(self, tmp_path, monkeypatch):
        from app import platform_utils
        (tmp_path / "source" / "repos").mkdir(parents=True)
        monkeypatch.setattr(Path, "home", lambda: tmp_path)
        monkeypatch.setattr(platform_utils, "sys", MagicMock(platform="win32"))
        roots = platform_utils.default_project_roots()
        assert tmp_path / "source" / "repos" in roots

    def test_mac_includes_developer(self, tmp_path, monkeypatch):
        from app import platform_utils
        (tmp_path / "Developer").mkdir()
        monkeypatch.setattr(Path, "home", lambda: tmp_path)
        monkeypatch.setattr(platform_utils, "sys", MagicMock(platform="darwin"))
        roots = platform_utils.default_project_roots()
        assert tmp_path / "Developer" in roots

    def test_linux_includes_common_dev_dirs(self, tmp_path, monkeypatch):
        from app import platform_utils
        for name in ("projects", "src", "code"):
            (tmp_path / name).mkdir()
        monkeypatch.setattr(Path, "home", lambda: tmp_path)
        monkeypatch.setattr(platform_utils, "sys", MagicMock(platform="linux"))
        roots = platform_utils.default_project_roots()
        for name in ("projects", "src", "code"):
            assert tmp_path / name in roots

    def test_linux_omits_missing_dev_dirs(self, tmp_path, monkeypatch):
        from app import platform_utils
        # Only create 'src', not 'projects', 'code', etc.
        (tmp_path / "src").mkdir()
        monkeypatch.setattr(Path, "home", lambda: tmp_path)
        monkeypatch.setattr(platform_utils, "sys", MagicMock(platform="linux"))
        roots = platform_utils.default_project_roots()
        assert tmp_path / "src" in roots
        assert tmp_path / "projects" not in roots


# ---------------------------------------------------------------------------
# native_folder_picker()
# ---------------------------------------------------------------------------

class TestNativeFolderPicker:

    def test_windows_returns_chosen_path(self, monkeypatch):
        from app import platform_utils
        monkeypatch.setattr(platform_utils, "sys", MagicMock(platform="win32"))
        mock_result = MagicMock(stdout="C:\\Users\\test\\project\n", returncode=0)
        with patch.object(platform_utils.subprocess, "run", return_value=mock_result):
            path, err = platform_utils.native_folder_picker()
        assert path == "C:\\Users\\test\\project"
        assert err is None

    def test_windows_cancelled(self, monkeypatch):
        from app import platform_utils
        monkeypatch.setattr(platform_utils, "sys", MagicMock(platform="win32"))
        mock_result = MagicMock(stdout="::CANCELLED::\n", returncode=0)
        with patch.object(platform_utils.subprocess, "run", return_value=mock_result):
            path, err = platform_utils.native_folder_picker()
        assert path is None
        assert err == "cancelled"

    def test_windows_empty_output(self, monkeypatch):
        from app import platform_utils
        monkeypatch.setattr(platform_utils, "sys", MagicMock(platform="win32"))
        mock_result = MagicMock(stdout="\n", returncode=0)
        with patch.object(platform_utils.subprocess, "run", return_value=mock_result):
            path, err = platform_utils.native_folder_picker()
        assert path is None
        assert err == "cancelled"

    def test_windows_exception(self, monkeypatch):
        from app import platform_utils
        monkeypatch.setattr(platform_utils, "sys", MagicMock(platform="win32"))
        with patch.object(platform_utils.subprocess, "run", side_effect=TimeoutError("timed out")):
            path, err = platform_utils.native_folder_picker()
        assert path is None
        assert "timed out" in err

    def test_mac_returns_chosen_path(self, monkeypatch):
        from app import platform_utils
        monkeypatch.setattr(platform_utils, "sys", MagicMock(platform="darwin"))
        mock_result = MagicMock(stdout="/Users/test/project/\n", returncode=0)
        with patch.object(platform_utils.subprocess, "run", return_value=mock_result):
            path, err = platform_utils.native_folder_picker()
        assert path == "/Users/test/project"  # trailing slash stripped
        assert err is None

    def test_mac_cancelled(self, monkeypatch):
        from app import platform_utils
        monkeypatch.setattr(platform_utils, "sys", MagicMock(platform="darwin"))
        mock_result = MagicMock(stdout="", returncode=1)
        with patch.object(platform_utils.subprocess, "run", return_value=mock_result):
            path, err = platform_utils.native_folder_picker()
        assert path is None
        assert err == "cancelled"

    def test_mac_exception(self, monkeypatch):
        from app import platform_utils
        monkeypatch.setattr(platform_utils, "sys", MagicMock(platform="darwin"))
        with patch.object(platform_utils.subprocess, "run", side_effect=OSError("osascript failed")):
            path, err = platform_utils.native_folder_picker()
        assert path is None
        assert "osascript failed" in err

    def test_linux_zenity_success(self, monkeypatch):
        from app import platform_utils
        monkeypatch.setattr(platform_utils, "sys", MagicMock(platform="linux"))
        mock_result = MagicMock(stdout="/home/test/project\n", returncode=0)
        with patch.object(platform_utils.subprocess, "run", return_value=mock_result):
            path, err = platform_utils.native_folder_picker()
        assert path == "/home/test/project"
        assert err is None

    def test_linux_zenity_cancelled(self, monkeypatch):
        from app import platform_utils
        monkeypatch.setattr(platform_utils, "sys", MagicMock(platform="linux"))
        mock_result = MagicMock(stdout="", returncode=1)
        with patch.object(platform_utils.subprocess, "run", return_value=mock_result):
            path, err = platform_utils.native_folder_picker()
        assert path is None
        assert err == "cancelled"

    def test_linux_falls_back_to_kdialog(self, monkeypatch):
        from app import platform_utils
        monkeypatch.setattr(platform_utils, "sys", MagicMock(platform="linux"))

        call_count = [0]
        def mock_run(cmd, **kwargs):
            call_count[0] += 1
            if cmd[0] == "zenity":
                raise FileNotFoundError("zenity not found")
            return MagicMock(stdout="/home/test/project\n", returncode=0)

        with patch.object(platform_utils.subprocess, "run", side_effect=mock_run):
            path, err = platform_utils.native_folder_picker()
        assert path == "/home/test/project"
        assert err is None
        assert call_count[0] == 2  # zenity failed, kdialog succeeded

    def test_linux_no_picker_available(self, monkeypatch):
        from app import platform_utils
        monkeypatch.setattr(platform_utils, "sys", MagicMock(platform="linux"))
        with patch.object(platform_utils.subprocess, "run", side_effect=FileNotFoundError("not found")):
            path, err = platform_utils.native_folder_picker()
        assert path is None
        assert "No folder picker" in err
