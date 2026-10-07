"""Tests for live_api.py — permission emit, hook, live state, folder tree, workforce."""

import json
import pytest
from pathlib import Path
from unittest.mock import patch, MagicMock


@pytest.fixture
def live_app(tmp_path, monkeypatch):
    """Flask app with isolated paths for live_api tests."""
    from app import create_app
    app = create_app(testing=True)
    app.session_manager.has_session.return_value = False

    # Mock folder tree path
    monkeypatch.setattr("app.routes.live_api._folder_tree_path",
                        lambda: tmp_path / "folder_tree.json")

    with app.test_client() as client:
        with app.app_context():
            yield app, client, tmp_path


@pytest.fixture
def live_client(live_app):
    _, client, _ = live_app
    return client


# ---------------------------------------------------------------------------
# Emit Permission (internal endpoint)
# ---------------------------------------------------------------------------

class TestEmitPermission:

    def test_emit_permission_returns_ok(self, live_client):
        resp = live_client.post('/api/_emit-permission',
                                json={"session_id": "s1", "tool_name": "Write"})
        assert resp.status_code == 200
        assert resp.get_json()["ok"] is True


# ---------------------------------------------------------------------------
# Hook Pre-Tool
# ---------------------------------------------------------------------------

class TestHookPreTool:

    def test_hook_pre_tool_allow(self, live_app):
        app, client, _ = live_app
        app.session_manager.hook_pre_tool.return_value = {"action": "allow"}
        resp = client.post('/api/hook/pre-tool',
                           json={"tool_name": "Write", "session_id": "s1"})
        assert resp.status_code == 200
        assert resp.get_json()["action"] == "allow"

    def test_hook_pre_tool_deny(self, live_app):
        app, client, _ = live_app
        app.session_manager.hook_pre_tool.return_value = {"action": "deny"}
        resp = client.post('/api/hook/pre-tool',
                           json={"tool_name": "Bash", "session_id": "s1"})
        assert resp.status_code == 200
        assert resp.get_json()["action"] == "deny"

    def test_hook_pre_tool_default_allow_on_error(self, live_app, monkeypatch):
        import time
        app, client, _ = live_app
        app.session_manager.hook_pre_tool.side_effect = Exception("IPC failed")
        # The route waits 1s between its 3 IPC attempts; record the waits
        # instead of sleeping through them.
        waits = []
        monkeypatch.setattr(time, "sleep", lambda s: waits.append(s))
        resp = client.post('/api/hook/pre-tool',
                           json={"tool_name": "Write", "session_id": "s1"})
        assert resp.status_code == 200
        assert resp.get_json()["action"] == "allow"
        assert app.session_manager.hook_pre_tool.call_count == 3
        assert waits == [1, 1]


# ---------------------------------------------------------------------------
# Live State
# ---------------------------------------------------------------------------

class TestLiveState:

    def test_live_state_returns_session_state(self, live_app):
        app, client, _ = live_app
        app.session_manager.get_session_state.return_value = "idle"
        app.session_manager.get_entry_count.return_value = 1
        app.session_manager.get_entries.return_value = [{"kind": "user"}]
        resp = client.get('/api/live/state/test-session')
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["state"] == "idle"

    def test_live_state_unknown_session(self, live_app):
        app, client, _ = live_app
        app.session_manager.get_session_state.return_value = None
        resp = client.get('/api/live/state/nonexistent')
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["state"] == "stopped"
        assert data["managed"] is False


# ---------------------------------------------------------------------------
# Folder Tree
# ---------------------------------------------------------------------------

class TestFolderTree:

    def test_get_folder_tree_empty(self, live_client):
        resp = live_client.get('/api/folder-tree')
        assert resp.status_code == 200
        assert resp.get_json() == {}

    def test_put_and_get_folder_tree(self, live_app):
        _, client, tmp_path = live_app
        tree = {"children": [{"name": "src", "type": "dir"}]}
        resp = client.put('/api/folder-tree', json=tree)
        assert resp.status_code == 200
        assert resp.get_json()["ok"] is True

        resp = client.get('/api/folder-tree')
        assert resp.status_code == 200
        assert resp.get_json()["children"][0]["name"] == "src"

    def test_put_folder_tree_no_body_returns_400(self, live_client):
        resp = live_client.put('/api/folder-tree',
                               data="not json",
                               content_type='application/json')
        assert resp.status_code == 400


# ---------------------------------------------------------------------------
# Config endpoints
# ---------------------------------------------------------------------------

class TestConfigEndpoints:

    def test_get_config(self, live_client):
        resp = live_client.get('/api/config')
        assert resp.status_code == 200
        assert isinstance(resp.get_json(), dict)
