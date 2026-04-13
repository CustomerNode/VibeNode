"""Tests for project_api.py — project listing, switching, renaming, adding."""

import json
import pytest
from pathlib import Path
from unittest.mock import patch


@pytest.fixture
def project_app(tmp_path, monkeypatch):
    """Flask app with isolated project directories."""
    from app import create_app
    from app import config

    projects_dir = tmp_path / "projects"
    projects_dir.mkdir()

    app = create_app(testing=True)
    app.session_manager.has_session.return_value = False

    monkeypatch.setattr(config, "_CLAUDE_PROJECTS", projects_dir)
    monkeypatch.setattr("app.routes.project_api._CLAUDE_PROJECTS", projects_dir)
    monkeypatch.setattr("app.routes.project_api._decode_project",
                        lambda name: name.replace("-", "/"))
    monkeypatch.setattr("app.routes.project_api._load_project_names", lambda: {})
    monkeypatch.setattr("app.routes.project_api._save_project_names", lambda x: None)

    with app.test_client() as client:
        with app.app_context():
            yield app, client, projects_dir


class TestListProjects:

    def test_empty_projects(self, project_app):
        _, client, _ = project_app
        resp = client.get('/api/projects')
        assert resp.status_code == 200
        assert resp.get_json() == []

    def test_lists_project_dirs(self, project_app, monkeypatch):
        _, client, projects_dir = project_app
        # The route filters for projects under ~/Documents, so mock _decode_project
        # to return a path that passes the filter
        docs = str(Path.home() / "Documents").replace("\\", "/")
        monkeypatch.setattr("app.routes.project_api._decode_project",
                            lambda name: docs + "/myproj")
        proj = projects_dir / "C--Users-test-Documents-myproj"
        proj.mkdir()
        (proj / "session.jsonl").write_text("{}", encoding="utf-8")
        resp = client.get('/api/projects')
        assert resp.status_code == 200
        data = resp.get_json()
        assert len(data) >= 1
        assert data[0]["session_count"] >= 1


class TestSetProject:

    def test_set_project_success(self, project_app):
        _, client, projects_dir = project_app
        proj = projects_dir / "test-proj"
        proj.mkdir()
        resp = client.post('/api/set-project', json={"project": "test-proj"})
        assert resp.status_code == 200
        assert resp.get_json()["ok"] is True

    def test_set_project_not_found(self, project_app):
        _, client, _ = project_app
        resp = client.post('/api/set-project', json={"project": "nonexistent"})
        assert resp.status_code == 404


class TestRenameProject:

    def test_rename_project(self, project_app, monkeypatch):
        saved = {}
        monkeypatch.setattr("app.routes.project_api._save_project_names",
                            lambda x: saved.update(x))
        _, client, _ = project_app
        resp = client.post('/api/rename-project',
                           json={"encoded": "proj-1", "name": "My Project"})
        assert resp.status_code == 200
        assert resp.get_json()["ok"] is True

    def test_rename_missing_encoded(self, project_app):
        _, client, _ = project_app
        resp = client.post('/api/rename-project', json={"name": "X"})
        assert resp.status_code == 400


class TestDeleteProject:

    def test_delete_project(self, project_app):
        _, client, projects_dir = project_app
        proj = projects_dir / "delete-me"
        proj.mkdir()
        resp = client.post('/api/delete-project', json={"encoded": "delete-me"})
        assert resp.status_code == 200
        assert resp.get_json()["ok"] is True
        assert not proj.exists()

    def test_delete_nonexistent(self, project_app):
        _, client, _ = project_app
        resp = client.post('/api/delete-project', json={"encoded": "nope"})
        assert resp.status_code == 404


class TestAddProject:

    def test_add_project_by_path(self, project_app, tmp_path):
        _, client, _ = project_app
        new_proj = tmp_path / "my-project"
        new_proj.mkdir()
        resp = client.post('/api/add-project',
                           json={"mode": "path", "path": str(new_proj)})
        assert resp.status_code == 200
        assert resp.get_json()["ok"] is True

    def test_add_project_invalid_path(self, project_app):
        _, client, _ = project_app
        resp = client.post('/api/add-project',
                           json={"mode": "path", "path": "/nonexistent/path"})
        assert resp.status_code == 400

    def test_add_project_create_mode(self, project_app, monkeypatch, tmp_path):
        _, client, _ = project_app
        # Monkeypatch Path.home to tmp_path so create doesn't touch real filesystem
        monkeypatch.setattr(Path, "home", lambda: tmp_path)
        resp = client.post('/api/add-project',
                           json={"mode": "create", "name": "NewProject"})
        assert resp.status_code == 200
        assert resp.get_json()["ok"] is True

    def test_add_project_unknown_mode(self, project_app):
        _, client, _ = project_app
        resp = client.post('/api/add-project', json={"mode": "teleport"})
        assert resp.status_code == 400


class TestNewSession:

    def test_new_session(self, project_app):
        _, client, projects_dir = project_app
        proj = projects_dir / "new-sess-proj"
        proj.mkdir()
        resp = client.post('/api/new-session', json={"project": "new-sess-proj"})
        assert resp.status_code == 200
        data = resp.get_json()
        assert "session_id" in data or "ok" in data
