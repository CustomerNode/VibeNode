"""Tests for cross-platform behavior in project_api.py routes."""

import sys
import json
import pytest
from pathlib import Path
from unittest.mock import patch, MagicMock


@pytest.fixture
def xplat_app(tmp_path, monkeypatch):
    """Flask app configured for cross-platform API tests."""
    from app import create_app
    from app import config

    projects_dir = tmp_path / "projects"
    projects_dir.mkdir()

    app = create_app(testing=True)
    app.session_manager.has_session.return_value = False

    monkeypatch.setattr(config, "_CLAUDE_PROJECTS", projects_dir)
    monkeypatch.setattr("app.routes.project_api._CLAUDE_PROJECTS", projects_dir)
    monkeypatch.setattr("app.routes.project_api._load_project_names", lambda: {})
    monkeypatch.setattr("app.routes.project_api._save_project_names", lambda x: None)

    with app.test_client() as client:
        with app.app_context():
            yield app, client, projects_dir


# ---------------------------------------------------------------------------
# Project list filtering — platform-aware
# ---------------------------------------------------------------------------

class TestProjectListFiltering:

    def test_windows_filters_to_documents(self, xplat_app, monkeypatch):
        """On Windows, only projects under ~/Documents should appear."""
        _, client, projects_dir = xplat_app
        docs_path = str(Path.home() / "Documents" / "myproj").replace("\\", "/")
        monkeypatch.setattr("app.routes.project_api._decode_project",
                            lambda name: docs_path)
        monkeypatch.setattr("app.routes.project_api.sys",
                            MagicMock(platform="win32"))
        proj = projects_dir / "encoded-proj"
        proj.mkdir()
        (proj / "session.jsonl").write_text("{}", encoding="utf-8")
        resp = client.get('/api/projects')
        assert resp.status_code == 200
        data = resp.get_json()
        assert len(data) >= 1

    def test_linux_filters_to_home(self, xplat_app, monkeypatch):
        """On Linux, projects under ~/ should appear (not just ~/Documents)."""
        _, client, projects_dir = xplat_app
        home_path = str(Path.home()).replace("\\", "/")
        monkeypatch.setattr("app.routes.project_api._decode_project",
                            lambda name: home_path + "/src/myproj")
        monkeypatch.setattr("app.routes.project_api.sys",
                            MagicMock(platform="linux"))
        proj = projects_dir / "encoded-proj"
        proj.mkdir()
        (proj / "session.jsonl").write_text("{}", encoding="utf-8")
        resp = client.get('/api/projects')
        assert resp.status_code == 200
        data = resp.get_json()
        assert len(data) >= 1


# ---------------------------------------------------------------------------
# New session — path validation without backslash conversion
# ---------------------------------------------------------------------------

class TestNewSessionPathValidation:

    def test_unix_path_not_corrupted(self, xplat_app, monkeypatch):
        """Verify forward-slash paths are NOT converted to backslashes."""
        _, client, projects_dir = xplat_app
        proj = projects_dir / "test-proj"
        proj.mkdir()

        # Mock _decode_project to return a path with forward slashes
        test_dir = str(proj).replace("\\", "/")
        monkeypatch.setattr("app.routes.project_api._decode_project",
                            lambda name: test_dir)

        resp = client.post('/api/new-session', json={"project": "test-proj"})
        # Should not fail with "Project directory not found"
        assert resp.status_code == 200

    def test_windows_path_still_works(self, xplat_app, monkeypatch):
        """Windows-style paths with backslashes should still work."""
        _, client, projects_dir = xplat_app
        proj = projects_dir / "test-proj"
        proj.mkdir()

        # Path with native separators
        monkeypatch.setattr("app.routes.project_api._decode_project",
                            lambda name: str(proj))

        resp = client.post('/api/new-session', json={"project": "test-proj"})
        assert resp.status_code == 200


# ---------------------------------------------------------------------------
# Add project — folder picker cross-platform
# ---------------------------------------------------------------------------

class TestAddProjectBrowse:

    def test_browse_calls_native_picker(self, xplat_app, monkeypatch, tmp_path):
        """Browse mode should use native_folder_picker() not inline PowerShell."""
        _, client, _ = xplat_app
        chosen_dir = tmp_path / "chosen"
        chosen_dir.mkdir()

        monkeypatch.setattr(
            "app.routes.project_api.native_folder_picker",
            lambda: (str(chosen_dir), None)
        )

        resp = client.post('/api/add-project', json={"mode": "browse"})
        assert resp.status_code == 200
        assert resp.get_json()["ok"] is True

    def test_browse_cancelled(self, xplat_app, monkeypatch):
        """Cancelled folder picker should return appropriate response."""
        _, client, _ = xplat_app

        monkeypatch.setattr(
            "app.routes.project_api.native_folder_picker",
            lambda: (None, "cancelled")
        )

        resp = client.post('/api/add-project', json={"mode": "browse"})
        data = resp.get_json()
        # Should indicate cancellation, not a 500 error
        assert resp.status_code != 500

    def test_browse_no_picker_available(self, xplat_app, monkeypatch):
        """When no picker is available (headless Linux), return helpful error."""
        _, client, _ = xplat_app

        monkeypatch.setattr(
            "app.routes.project_api.native_folder_picker",
            lambda: (None, "No folder picker available — install zenity or kdialog")
        )

        resp = client.post('/api/add-project', json={"mode": "browse"})
        assert resp.status_code != 500


# ---------------------------------------------------------------------------
# Create project — platform-aware default directory
# ---------------------------------------------------------------------------

class TestCreateProjectDefaults:

    def test_linux_defaults_to_home(self, xplat_app, monkeypatch, tmp_path):
        """On Linux, new projects should default to ~/ not ~/Documents."""
        _, client, _ = xplat_app
        monkeypatch.setattr(Path, "home", lambda: tmp_path)
        monkeypatch.setattr("app.routes.project_api.sys",
                            MagicMock(platform="linux"))

        resp = client.post('/api/add-project',
                           json={"mode": "create", "name": "TestProj"})
        assert resp.status_code == 200
        # The project should be created under tmp_path (home), not tmp_path/Documents
        assert (tmp_path / "TestProj").is_dir()

    def test_windows_defaults_to_documents(self, xplat_app, monkeypatch, tmp_path):
        """On Windows, new projects should default to ~/Documents/."""
        _, client, _ = xplat_app
        (tmp_path / "Documents").mkdir()
        monkeypatch.setattr(Path, "home", lambda: tmp_path)
        monkeypatch.setattr("app.routes.project_api.sys",
                            MagicMock(platform="win32"))

        resp = client.post('/api/add-project',
                           json={"mode": "create", "name": "TestProj"})
        assert resp.status_code == 200
        assert (tmp_path / "Documents" / "TestProj").is_dir()


# ---------------------------------------------------------------------------
# Session handoff — cwd encoding
# ---------------------------------------------------------------------------

class TestSessionHandoffCwd:

    def test_cwd_not_backslash_converted_on_unix(self, monkeypatch):
        """On Unix, session handoff cwd should not have backslash conversion."""
        # This is a structural check — the code should use platform guard
        from app.routes import sessions_api
        import inspect
        source = inspect.getsource(sessions_api)
        # The backslash conversion should be guarded by a platform check
        # It should NOT have a bare .replace("/", "\\") without a sys.platform guard
        lines = source.split("\n")
        for i, line in enumerate(lines):
            if '.replace("/", "\\\\")' in line or ".replace('/', '\\\\')" in line:
                # Check that this line or nearby lines have a platform guard
                context = "\n".join(lines[max(0, i-5):i+1])
                assert "win32" in context or "platform" in context, \
                    f"Unguarded backslash conversion at line {i+1}: {line.strip()}"
