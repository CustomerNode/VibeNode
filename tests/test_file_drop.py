"""
Tests for the file-drop feature endpoints:
  - POST /api/file-drop  (upload file to target directory)
  - GET  /api/browse-dir  (list subdirectories)
  - GET  /api/project-path (return active project path)

Also tests helper functions _dedup_filename and _is_within_home.
"""

import io
import json
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture()
def fake_project(tmp_path):
    """Create a fake project directory."""
    proj = tmp_path / "projects" / "C--Users-test-Documents-myproj"
    proj.mkdir(parents=True)
    return proj


@pytest.fixture()
def app(tmp_path, fake_project):
    """Create a Flask app with production blueprints, pointing at tmp dirs."""
    from app import create_app

    application = create_app()
    application.config["TESTING"] = True

    _patch_sessions = patch(
        "app.config._sessions_dir",
        return_value=fake_project,
    )
    _patch_projects = patch(
        "app.config._CLAUDE_PROJECTS",
        fake_project.parent,
    )
    _patch_sessions_api = patch(
        "app.routes.sessions_api._sessions_dir",
        return_value=fake_project,
    )
    _patch_live_api = patch(
        "app.routes.live_api._sessions_dir",
        return_value=fake_project,
    )
    _patch_names_file = patch(
        "app.config._names_file",
        return_value=fake_project / "_session_names.json",
    )
    _patch_names_file_sess = patch(
        "app.sessions._load_names",
        return_value={},
    )
    _patch_names_file_sess_cached = patch(
        "app.sessions._load_names_cached",
        return_value={},
    )

    with (_patch_sessions, _patch_projects,
          _patch_sessions_api, _patch_live_api,
          _patch_names_file, _patch_names_file_sess,
          _patch_names_file_sess_cached):
        yield application


@pytest.fixture()
def client(app):
    """Flask test client."""
    return app.test_client()


# ---------------------------------------------------------------------------
# Helper function unit tests
# ---------------------------------------------------------------------------

class TestDedupFilename:
    """Test _dedup_filename helper."""

    def test_no_conflict(self, tmp_path):
        from app.routes.live_api import _dedup_filename
        result = _dedup_filename(tmp_path, "report.pdf")
        assert result == "report.pdf"

    def test_first_conflict(self, tmp_path):
        from app.routes.live_api import _dedup_filename
        (tmp_path / "report.pdf").write_text("existing")
        result = _dedup_filename(tmp_path, "report.pdf")
        assert result == "report (1).pdf"

    def test_multiple_conflicts(self, tmp_path):
        from app.routes.live_api import _dedup_filename
        (tmp_path / "report.pdf").write_text("v1")
        (tmp_path / "report (1).pdf").write_text("v2")
        result = _dedup_filename(tmp_path, "report.pdf")
        assert result == "report (2).pdf"

    def test_no_extension(self, tmp_path):
        from app.routes.live_api import _dedup_filename
        (tmp_path / "README").write_text("existing")
        result = _dedup_filename(tmp_path, "README")
        assert result == "README (1)"


class TestIsWithinHome:
    """Test _is_within_home helper."""

    def test_home_subpath(self):
        from app.routes.live_api import _is_within_home
        p = Path.home() / "Documents" / "test"
        assert _is_within_home(p) is True

    def test_root_path(self):
        from app.routes.live_api import _is_within_home
        # Root or system path should not be within home
        import sys
        if sys.platform == "win32":
            p = Path("C:\\Windows\\System32")
        else:
            p = Path("/etc")
        assert _is_within_home(p) is False


# ---------------------------------------------------------------------------
# /api/file-drop endpoint tests
# ---------------------------------------------------------------------------

class TestFileDropEndpoint:
    """Test POST /api/file-drop."""

    def test_no_file_returns_400(self, client):
        resp = client.post("/api/file-drop", data={"target_dir": "/tmp"})
        assert resp.status_code == 400
        assert "No file" in resp.get_json()["error"]

    def test_empty_filename_returns_400(self, client, tmp_path):
        data = {
            "file": (io.BytesIO(b"data"), ""),
            "target_dir": str(tmp_path),
        }
        resp = client.post(
            "/api/file-drop",
            data=data,
            content_type="multipart/form-data",
        )
        assert resp.status_code == 400

    def test_no_target_dir_returns_400(self, client):
        data = {
            "file": (io.BytesIO(b"content"), "test.txt"),
        }
        resp = client.post(
            "/api/file-drop",
            data=data,
            content_type="multipart/form-data",
        )
        assert resp.status_code == 400
        assert "target_dir" in resp.get_json()["error"]

    def test_target_outside_home_returns_403(self, client):
        import sys
        if sys.platform == "win32":
            outside = "C:\\Windows\\Temp"
        else:
            outside = "/tmp"
        data = {
            "file": (io.BytesIO(b"content"), "test.txt"),
            "target_dir": outside,
        }
        resp = client.post(
            "/api/file-drop",
            data=data,
            content_type="multipart/form-data",
        )
        # Should be 403 if outside home, or 400 if doesn't exist
        assert resp.status_code in (403, 400)

    def test_successful_upload(self, client, tmp_path):
        # Create target inside home
        target = Path.home() / "VibeNode_test_drop"
        target.mkdir(exist_ok=True)
        try:
            data = {
                "file": (io.BytesIO(b"hello world"), "test_drop.txt"),
                "target_dir": str(target),
            }
            resp = client.post(
                "/api/file-drop",
                data=data,
                content_type="multipart/form-data",
            )
            rj = resp.get_json()
            assert resp.status_code == 200
            assert rj["ok"] is True
            assert "test_drop" in rj["filename"]
            # Verify file on disk
            saved = Path(rj["path"])
            assert saved.exists()
            assert saved.read_bytes() == b"hello world"
        finally:
            # Cleanup
            for f in target.iterdir():
                f.unlink()
            target.rmdir()

    def test_dedup_on_conflict(self, client):
        """Dropping file with same name creates dedup'd name."""
        target = Path.home() / "VibeNode_test_dedup"
        target.mkdir(exist_ok=True)
        try:
            # Create existing file
            (target / "test_dedup.txt").write_text("original")

            data = {
                "file": (io.BytesIO(b"new content"), "test_dedup.txt"),
                "target_dir": str(target),
            }
            resp = client.post(
                "/api/file-drop",
                data=data,
                content_type="multipart/form-data",
            )
            rj = resp.get_json()
            assert resp.status_code == 200
            assert rj["ok"] is True
            # Should have a dedup suffix
            assert "(1)" in rj["filename"] or rj["filename"] != "test_dedup.txt"
            # Original untouched
            assert (target / "test_dedup.txt").read_text() == "original"
            # New file has new content
            assert Path(rj["path"]).read_bytes() == b"new content"
        finally:
            for f in target.iterdir():
                f.unlink()
            target.rmdir()

    def test_nonexistent_target_returns_400(self, client):
        target = Path.home() / "VibeNode_nonexistent_dir_xyz"
        data = {
            "file": (io.BytesIO(b"content"), "test.txt"),
            "target_dir": str(target),
        }
        resp = client.post(
            "/api/file-drop",
            data=data,
            content_type="multipart/form-data",
        )
        assert resp.status_code == 400
        assert "does not exist" in resp.get_json()["error"]


# ---------------------------------------------------------------------------
# /api/browse-dir endpoint tests
# ---------------------------------------------------------------------------

class TestBrowseDirEndpoint:
    """Test GET /api/browse-dir."""

    def test_no_path_returns_400(self, client):
        resp = client.get("/api/browse-dir")
        assert resp.status_code == 400

    def test_nonexistent_dir_returns_400(self, client):
        resp = client.get("/api/browse-dir?path=" + str(Path.home() / "nonexistent_xyz"))
        assert resp.status_code == 400

    def test_lists_subdirectories(self, client):
        target = Path.home() / "VibeNode_test_browse"
        target.mkdir(exist_ok=True)
        (target / "subA").mkdir(exist_ok=True)
        (target / "subB").mkdir(exist_ok=True)
        # File should not appear in dirs list
        (target / "file.txt").write_text("hi")
        try:
            resp = client.get("/api/browse-dir?path=" + str(target))
            rj = resp.get_json()
            assert resp.status_code == 200
            assert "subA" in rj["dirs"]
            assert "subB" in rj["dirs"]
            assert "file.txt" not in rj["dirs"]
        finally:
            (target / "file.txt").unlink()
            (target / "subA").rmdir()
            (target / "subB").rmdir()
            target.rmdir()

    def test_hidden_dirs_excluded(self, client):
        target = Path.home() / "VibeNode_test_hidden"
        target.mkdir(exist_ok=True)
        (target / ".hidden").mkdir(exist_ok=True)
        (target / "visible").mkdir(exist_ok=True)
        try:
            resp = client.get("/api/browse-dir?path=" + str(target))
            rj = resp.get_json()
            assert ".hidden" not in rj["dirs"]
            assert "visible" in rj["dirs"]
        finally:
            (target / ".hidden").rmdir()
            (target / "visible").rmdir()
            target.rmdir()

    def test_sorted_output(self, client):
        target = Path.home() / "VibeNode_test_sorted"
        target.mkdir(exist_ok=True)
        (target / "zebra").mkdir(exist_ok=True)
        (target / "alpha").mkdir(exist_ok=True)
        try:
            resp = client.get("/api/browse-dir?path=" + str(target))
            rj = resp.get_json()
            assert rj["dirs"] == sorted(rj["dirs"])
        finally:
            (target / "zebra").rmdir()
            (target / "alpha").rmdir()
            target.rmdir()


# ---------------------------------------------------------------------------
# /api/project-path endpoint tests
# ---------------------------------------------------------------------------

class TestProjectPathEndpoint:
    """Test GET /api/project-path."""

    def test_returns_path_key(self, client):
        resp = client.get("/api/project-path")
        rj = resp.get_json()
        assert resp.status_code == 200
        assert "path" in rj
        # Should be a string path
        assert isinstance(rj["path"], str)
        assert len(rj["path"]) > 0
