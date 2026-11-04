"""Tests for project_api.py — project listing, switching, renaming, adding."""

import json
import pytest
from pathlib import Path
from unittest.mock import patch


@pytest.fixture
def project_app(tmp_path, monkeypatch):
    """Flask app with isolated project directories."""
    from web import create_app
    from web import config

    projects_dir = tmp_path / "projects"
    projects_dir.mkdir()

    app = create_app(testing=True)
    app.session_manager.has_session.return_value = False

    monkeypatch.setattr(config, "_CLAUDE_PROJECTS", projects_dir)
    monkeypatch.setattr("web.routes.project_api._CLAUDE_PROJECTS", projects_dir)
    monkeypatch.setattr("web.routes.project_api._decode_project",
                        lambda name: name.replace("-", "/"))
    monkeypatch.setattr("web.routes.project_api._load_project_names", lambda: {})
    monkeypatch.setattr("web.routes.project_api._save_project_names", lambda x: None)

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
        monkeypatch.setattr("web.routes.project_api._decode_project",
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
        monkeypatch.setattr("web.routes.project_api._save_project_names",
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

    def test_new_session_passes_the_model(self, project_app):
        app, client, _ = project_app
        app.session_manager.start_session.return_value = {"ok": True}
        resp = client.post('/api/new-session', json={"prompt": "hi", "model": "claude-sonnet-5-5"})
        assert resp.status_code == 200
        assert app.session_manager.start_session.call_args.kwargs["model"] == "claude-sonnet-5-5"
        client.post('/api/new-session', json={"prompt": "hi"})
        assert app.session_manager.start_session.call_args.kwargs["model"] is None

    def test_new_session_refuses_a_model_that_is_not_a_plain_id(self, project_app):
        app, client, _ = project_app
        app.session_manager.start_session.reset_mock()
        for bad in ("--dangerously-skip-permissions", "opus; rm -rf x", "a b"):
            resp = client.post('/api/new-session', json={"prompt": "hi", "model": bad})
            assert resp.status_code == 400
        assert not app.session_manager.start_session.called


class TestNewSessionForComparisons:
    """AI Compare starts read-only sessions; Controlled A/B Test starts each
    trial in its own folder. The reply says what was applied, so a runner can
    refuse to call a run read-only or isolated when it was not."""

    def test_cwd_starts_the_session_in_that_folder_and_its_own_project(self, project_app, tmp_path):
        app, client, projects_dir = project_app
        app.session_manager.start_session.return_value = {"ok": True}
        work = tmp_path / "trial-copy"
        work.mkdir()
        resp = client.post('/api/new-session', json={"prompt": "hi", "cwd": str(work), "name": "t1"})
        assert resp.status_code == 200
        data = resp.get_json()
        kw = app.session_manager.start_session.call_args.kwargs
        assert Path(kw["cwd"]) == work.resolve()
        assert Path(data["applied"]["cwd"]) == work.resolve()
        from web.config import _encode_cwd
        assert data["applied"]["project"] == _encode_cwd(str(work.resolve()))
        assert (projects_dir / data["applied"]["project"] / f"{data['new_id']}.jsonl").exists()

    def test_plan_mode_is_passed_and_reported(self, project_app):
        app, client, _ = project_app
        app.session_manager.start_session.return_value = {"ok": True}
        resp = client.post('/api/new-session', json={"prompt": "hi", "permission_mode": "plan"})
        assert resp.get_json()["applied"]["permission_mode"] == "plan"
        assert app.session_manager.start_session.call_args.kwargs["permission_mode"] == "plan"
        client.post('/api/new-session', json={"prompt": "hi"})
        assert "permission_mode" not in app.session_manager.start_session.call_args.kwargs

    def test_nothing_looser_than_default_is_accepted(self, project_app):
        app, client, _ = project_app
        app.session_manager.start_session.reset_mock()
        for bad in ("bypassPermissions", "acceptEdits", "auto"):
            assert client.post('/api/new-session', json={"prompt": "hi", "permission_mode": bad}).status_code == 400
        assert not app.session_manager.start_session.called

    def test_a_missing_or_relative_cwd_is_refused(self, project_app, tmp_path):
        app, client, _ = project_app
        app.session_manager.start_session.reset_mock()
        for bad in (str(tmp_path / "nope"), "relative/dir"):
            assert client.post('/api/new-session', json={"prompt": "hi", "cwd": bad}).status_code == 400
        assert client.post('/api/new-session', json={"resume_id": "x", "cwd": str(tmp_path)}).status_code == 400
        assert not app.session_manager.start_session.called

    def test_the_prompt_is_passed_exactly(self, project_app):
        app, client, _ = project_app
        app.session_manager.start_session.return_value = {"ok": True}
        exact = "  two leading spaces\n\n\nand trailing  "
        client.post('/api/new-session', json={"prompt": exact})
        assert app.session_manager.start_session.call_args.kwargs["prompt"] == exact
        client.post('/api/new-session', json={"prompt": "   \n  "})
        assert app.session_manager.start_session.call_args.kwargs["prompt"] == ""

    def test_the_stop_route_stops_and_reports_the_state_after(self, project_app):
        app, client, _ = project_app
        sm = app.session_manager
        sm.close_session.reset_mock()
        sm.close_session.return_value = {"ok": True}
        sm.get_session_state.return_value = None
        r = client.post('/api/live/close/abc-123')
        assert r.get_json() == {"ok": True, "error": "", "state": "stopped"}
        sm.close_session.assert_called_with("abc-123")
        sm.close_session.return_value = {"ok": False, "error": "Session not found"}
        assert client.post('/api/live/close/abc-123').get_json()["ok"] is True  # already not running
        sm.close_session.return_value = {"ok": False, "error": "busy"}
        sm.get_session_state.return_value = "working"
        assert client.post('/api/live/close/abc-123').get_json() == {"ok": False, "error": "busy", "state": "working"}
        assert client.post('/api/live/close/bad..id').status_code in (400, 404)
