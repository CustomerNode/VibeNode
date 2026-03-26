"""
Tests for the output shelf feature endpoints:
  - GET  /api/file-info         (file name, size, existence)
  - POST /api/open-file         (open file with system default app)
  - POST /api/download-to-downloads  (copy file to Downloads with dedup)

Also tests security boundary: paths outside home are rejected.
"""

import json
import sys
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
# GET /api/file-info
# ---------------------------------------------------------------------------

class TestFileInfoEndpoint:
    """Test GET /api/file-info."""

    def test_no_path_returns_400(self, client):
        resp = client.get("/api/file-info")
        assert resp.status_code == 400
        assert "No path" in resp.get_json()["error"]

    def test_existing_file_returns_info(self, client):
        """An existing file within home returns name, size, exists=True."""
        target_dir = Path.home() / "VibeNode_test_fileinfo"
        target_dir.mkdir(exist_ok=True)
        test_file = target_dir / "report.xlsx"
        test_file.write_bytes(b"x" * 2048)  # 2 KB
        try:
            resp = client.get(f"/api/file-info?path={test_file}")
            rj = resp.get_json()
            assert resp.status_code == 200
            assert rj["name"] == "report.xlsx"
            assert rj["exists"] is True
            assert "KB" in rj["size"]
        finally:
            test_file.unlink(missing_ok=True)
            target_dir.rmdir()

    def test_nonexistent_file_returns_exists_false(self, client):
        """A non-existent path within home returns exists=False."""
        fake = Path.home() / "VibeNode_test_fileinfo_nonexistent.xlsx"
        resp = client.get(f"/api/file-info?path={fake}")
        rj = resp.get_json()
        assert resp.status_code == 200
        assert rj["exists"] is False
        assert rj["name"] == "VibeNode_test_fileinfo_nonexistent.xlsx"

    def test_path_outside_home_returns_403(self, client):
        """A path outside the user's home is rejected with 403."""
        if sys.platform == "win32":
            outside = "C:\\Windows\\System32\\notepad.exe"
        else:
            outside = "/etc/hosts"
        resp = client.get(f"/api/file-info?path={outside}")
        assert resp.status_code == 403
        assert "home" in resp.get_json()["error"].lower()

    def test_small_file_shows_bytes(self, client):
        """A file under 1KB shows size in bytes."""
        target_dir = Path.home() / "VibeNode_test_fileinfo_small"
        target_dir.mkdir(exist_ok=True)
        test_file = target_dir / "tiny.txt"
        test_file.write_bytes(b"hi")  # 2 bytes
        try:
            resp = client.get(f"/api/file-info?path={test_file}")
            rj = resp.get_json()
            assert resp.status_code == 200
            assert "B" in rj["size"]
        finally:
            test_file.unlink(missing_ok=True)
            target_dir.rmdir()


# ---------------------------------------------------------------------------
# POST /api/open-file
# ---------------------------------------------------------------------------

class TestOpenFileEndpoint:
    """Test POST /api/open-file."""

    def test_no_path_returns_400(self, client):
        resp = client.post("/api/open-file",
                           data=json.dumps({}),
                           content_type="application/json")
        assert resp.status_code == 400
        assert "No path" in resp.get_json()["error"]

    def test_nonexistent_file_returns_404(self, client):
        fake = Path.home() / "VibeNode_test_open_nonexistent.xlsx"
        resp = client.post("/api/open-file",
                           data=json.dumps({"path": str(fake)}),
                           content_type="application/json")
        assert resp.status_code == 404
        assert "does not exist" in resp.get_json()["error"]

    def test_path_outside_home_returns_403(self, client):
        if sys.platform == "win32":
            outside = "C:\\Windows\\System32\\notepad.exe"
        else:
            outside = "/etc/hosts"
        resp = client.post("/api/open-file",
                           data=json.dumps({"path": outside}),
                           content_type="application/json")
        assert resp.status_code == 403
        assert "home" in resp.get_json()["error"].lower()

    @patch("app.routes.live_api.os.startfile", create=True)
    def test_valid_file_calls_startfile(self, mock_startfile, client):
        """A valid file within home calls os.startfile and returns ok."""
        target_dir = Path.home() / "VibeNode_test_open"
        target_dir.mkdir(exist_ok=True)
        test_file = target_dir / "test_open.xlsx"
        test_file.write_bytes(b"fake excel")
        try:
            resp = client.post("/api/open-file",
                               data=json.dumps({"path": str(test_file)}),
                               content_type="application/json")
            rj = resp.get_json()
            assert resp.status_code == 200
            assert rj["ok"] is True
            mock_startfile.assert_called_once()
        finally:
            test_file.unlink(missing_ok=True)
            target_dir.rmdir()


# ---------------------------------------------------------------------------
# POST /api/download-to-downloads
# ---------------------------------------------------------------------------

class TestDownloadToDownloadsEndpoint:
    """Test POST /api/download-to-downloads."""

    def test_no_path_returns_400(self, client):
        resp = client.post("/api/download-to-downloads",
                           data=json.dumps({}),
                           content_type="application/json")
        assert resp.status_code == 400
        assert "No path" in resp.get_json()["error"]

    def test_nonexistent_file_returns_404(self, client):
        fake = Path.home() / "VibeNode_test_dl_nonexistent.xlsx"
        resp = client.post("/api/download-to-downloads",
                           data=json.dumps({"path": str(fake)}),
                           content_type="application/json")
        assert resp.status_code == 404
        assert "does not exist" in resp.get_json()["error"]

    def test_path_outside_home_returns_403(self, client):
        if sys.platform == "win32":
            outside = "C:\\Windows\\System32\\notepad.exe"
        else:
            outside = "/etc/hosts"
        resp = client.post("/api/download-to-downloads",
                           data=json.dumps({"path": outside}),
                           content_type="application/json")
        assert resp.status_code == 403
        assert "home" in resp.get_json()["error"].lower()

    def test_successful_copy_to_downloads(self, client):
        """File is copied to Downloads with correct filename."""
        source_dir = Path.home() / "VibeNode_test_dl_src"
        source_dir.mkdir(exist_ok=True)
        test_file = source_dir / "output_report.xlsx"
        test_file.write_bytes(b"excel content here")
        downloads = Path.home() / "Downloads"
        dest = downloads / "output_report.xlsx"
        try:
            resp = client.post("/api/download-to-downloads",
                               data=json.dumps({"path": str(test_file)}),
                               content_type="application/json")
            rj = resp.get_json()
            assert resp.status_code == 200
            assert rj["ok"] is True
            assert "output_report" in rj["filename"]
            # Verify the file actually landed in Downloads
            actual_dest = Path(rj["dest"])
            assert actual_dest.exists()
            assert actual_dest.read_bytes() == b"excel content here"
        finally:
            # Cleanup
            test_file.unlink(missing_ok=True)
            source_dir.rmdir()
            actual = downloads / rj.get("filename", "output_report.xlsx")
            if actual.exists():
                actual.unlink()

    def test_dedup_suffix_when_name_exists(self, client):
        """If a file with the same name exists in Downloads, a dedup suffix is added."""
        source_dir = Path.home() / "VibeNode_test_dl_dedup"
        source_dir.mkdir(exist_ok=True)
        test_file = source_dir / "dedup_test.xlsx"
        test_file.write_bytes(b"new version")
        downloads = Path.home() / "Downloads"
        existing = downloads / "dedup_test.xlsx"
        existing.write_bytes(b"original version")
        try:
            resp = client.post("/api/download-to-downloads",
                               data=json.dumps({"path": str(test_file)}),
                               content_type="application/json")
            rj = resp.get_json()
            assert resp.status_code == 200
            assert rj["ok"] is True
            # The filename must differ from the original (dedup suffix)
            assert rj["filename"] != "dedup_test.xlsx"
            assert "(1)" in rj["filename"]
            # Original untouched
            assert existing.read_bytes() == b"original version"
            # New file has new content
            actual_dest = Path(rj["dest"])
            assert actual_dest.read_bytes() == b"new version"
        finally:
            test_file.unlink(missing_ok=True)
            source_dir.rmdir()
            existing.unlink(missing_ok=True)
            deduped = downloads / rj.get("filename", "")
            if deduped.exists():
                deduped.unlink()
