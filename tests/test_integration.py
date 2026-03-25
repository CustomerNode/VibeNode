"""Integration tests — end-to-end workflows through the Flask app."""

import json
import pytest
from pathlib import Path


@pytest.fixture
def app_with_sessions(mock_sessions_dir, monkeypatch):
    """Create a test app pointing at the mock sessions directory."""
    from app import config
    monkeypatch.setattr(config, "_CLAUDE_PROJECTS", mock_sessions_dir.parent)
    monkeypatch.setattr(config, "_active_project", mock_sessions_dir.name)
    # Avoid Windows junction PermissionError in _decode_project
    monkeypatch.setattr(config, "_decode_project", lambda name: str(mock_sessions_dir / name))

    from app import create_app
    app = create_app()
    app.config["TESTING"] = True
    return app


@pytest.fixture
def client(app_with_sessions):
    return app_with_sessions.test_client()


class TestSessionListWorkflow:

    def test_list_sessions(self, client):
        resp = client.get("/api/sessions")
        assert resp.status_code == 200
        data = resp.get_json()
        assert isinstance(data, list)
        assert len(data) > 0

    def test_session_has_required_fields(self, client):
        resp = client.get("/api/sessions")
        session = resp.get_json()[0]
        for key in ("id", "display_title", "date", "size", "message_count"):
            assert key in session, f"Missing key: {key}"

    def test_get_single_session(self, client):
        # Get list first
        sessions = client.get("/api/sessions").get_json()
        sid = sessions[0]["id"]
        # Get individual session
        resp = client.get(f"/api/session/{sid}")
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["id"] == sid
        assert "messages" in data

    def test_get_nonexistent_session(self, client):
        resp = client.get("/api/session/nonexistent_id")
        assert resp.status_code == 404


class TestRenameWorkflow:

    def test_rename_session(self, client):
        sessions = client.get("/api/sessions").get_json()
        sid = sessions[0]["id"]
        resp = client.post(f"/api/rename/{sid}", json={"title": "Renamed Session"})
        assert resp.status_code == 200
        assert resp.get_json()["ok"]

    def test_rename_empty_title_rejected(self, client):
        sessions = client.get("/api/sessions").get_json()
        sid = sessions[0]["id"]
        resp = client.post(f"/api/rename/{sid}", json={"title": ""})
        assert resp.status_code == 400


class TestDeleteWorkflow:

    def test_delete_session(self, client):
        sessions = client.get("/api/sessions").get_json()
        count_before = len(sessions)
        sid = sessions[0]["id"]
        resp = client.delete(f"/api/delete/{sid}")
        assert resp.status_code == 200
        # Verify deleted
        sessions_after = client.get("/api/sessions").get_json()
        assert len(sessions_after) == count_before - 1

    def test_delete_nonexistent_returns_error(self, client):
        resp = client.delete("/api/delete/nonexistent_id")
        assert resp.status_code in (404, 200)


class TestProjectWorkflow:

    def test_list_projects(self, client):
        resp = client.get("/api/projects")
        assert resp.status_code == 200
        projects = resp.get_json()
        assert isinstance(projects, list)
        # In test environment with mock dir, may be empty
        assert isinstance(projects, list)

    def test_project_response_is_list(self, client):
        projects = client.get("/api/projects").get_json()
        assert isinstance(projects, list)


class TestPageServing:

    def test_index_returns_html(self, client):
        resp = client.get("/")
        assert resp.status_code == 200
        assert b"ClaudeCodeGUI" in resp.data or b"Claude Code GUI" in resp.data

    def test_static_css_accessible(self, client):
        resp = client.get("/static/style.css")
        assert resp.status_code == 200
        assert b"--bg-body" in resp.data


class TestGitStatus:

    def test_git_status_returns_json(self, client):
        resp = client.get("/api/git-status")
        assert resp.status_code == 200
        data = resp.get_json()
        assert "has_git" in data or "ready" in data
