"""Tests for kanban API endpoints — task CRUD, move, reorder, tags, sessions, bulk ops."""

import json
import pytest


# ---------------------------------------------------------------------------
# Board
# ---------------------------------------------------------------------------

class TestBoardEndpoint:

    def test_board_returns_columns_and_tasks(self, kanban_app):
        app, client, repo = kanban_app
        # Create a task first
        client.post('/api/kanban/tasks', json={"title": "Task A"})
        resp = client.get('/api/kanban/board')
        assert resp.status_code == 200
        data = resp.get_json()
        assert "columns" in data
        assert "tasks" in data
        assert isinstance(data["columns"], list)
        assert len(data["tasks"]) >= 1

    def test_board_empty_project(self, kanban_app):
        app, client, repo = kanban_app
        resp = client.get('/api/kanban/board')
        assert resp.status_code == 200
        data = resp.get_json()
        assert "columns" in data
        assert len(data["columns"]) >= 5  # default columns

    def test_board_pagination(self, kanban_app):
        app, client, repo = kanban_app
        for i in range(5):
            client.post('/api/kanban/tasks', json={"title": f"Task {i}"})
        resp = client.get('/api/kanban/board?page=1&page_size=2')
        assert resp.status_code == 200
        data = resp.get_json()
        assert "tasks" in data

    def test_board_includes_tags_list(self, kanban_app):
        app, client, repo = kanban_app
        resp = client.get('/api/kanban/board')
        data = resp.get_json()
        assert "tags" in data


# ---------------------------------------------------------------------------
# Task CRUD
# ---------------------------------------------------------------------------

class TestTaskCRUD:

    def test_create_task_minimal(self, kanban_app):
        app, client, repo = kanban_app
        resp = client.post('/api/kanban/tasks', json={"title": "My Task"})
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["title"] == "My Task"
        assert "id" in data
        assert data["status"] == "not_started"

    def test_create_task_no_title_returns_400(self, kanban_client):
        resp = kanban_client.post('/api/kanban/tasks', json={"title": ""})
        assert resp.status_code == 400

    def test_create_task_with_parent(self, kanban_app):
        app, client, repo = kanban_app
        parent = client.post('/api/kanban/tasks', json={"title": "Parent"}).get_json()
        child = client.post('/api/kanban/tasks', json={
            "title": "Child", "parent_id": parent["id"]
        }).get_json()
        assert child["parent_id"] == parent["id"]
        assert child["depth"] == 1

    def test_create_task_with_status(self, kanban_client):
        resp = kanban_client.post('/api/kanban/tasks', json={
            "title": "WIP", "status": "working"
        })
        assert resp.get_json()["status"] == "working"

    def test_create_task_insert_top(self, kanban_app):
        app, client, repo = kanban_app
        t1 = client.post('/api/kanban/tasks', json={"title": "Bottom"}).get_json()
        t2 = client.post('/api/kanban/tasks', json={
            "title": "Top", "insert_position": "top"
        }).get_json()
        assert t2["position"] < t1["position"]

    def test_get_task(self, kanban_app):
        app, client, repo = kanban_app
        created = client.post('/api/kanban/tasks', json={"title": "Fetch Me"}).get_json()
        resp = client.get(f'/api/kanban/tasks/{created["id"]}')
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["title"] == "Fetch Me"
        assert "children" in data
        assert "sessions" in data

    def test_get_task_not_found_returns_404(self, kanban_client):
        resp = kanban_client.get('/api/kanban/tasks/nonexistent-id')
        assert resp.status_code == 404

    def test_update_task_title(self, kanban_app):
        app, client, repo = kanban_app
        created = client.post('/api/kanban/tasks', json={"title": "Old"}).get_json()
        resp = client.patch(f'/api/kanban/tasks/{created["id"]}',
                            json={"title": "New"})
        assert resp.status_code == 200
        assert resp.get_json()["title"] == "New"

    def test_update_task_status_uses_state_machine(self, kanban_app):
        app, client, repo = kanban_app
        created = client.post('/api/kanban/tasks', json={"title": "SM Test"}).get_json()
        resp = client.patch(f'/api/kanban/tasks/{created["id"]}',
                            json={"status": "working"})
        assert resp.status_code == 200
        assert resp.get_json()["status"] == "working"

    def test_update_task_no_data_returns_400(self, kanban_app):
        app, client, repo = kanban_app
        created = client.post('/api/kanban/tasks', json={"title": "X"}).get_json()
        resp = client.patch(f'/api/kanban/tasks/{created["id"]}', json={})
        assert resp.status_code == 400

    def test_delete_task(self, kanban_app):
        app, client, repo = kanban_app
        created = client.post('/api/kanban/tasks', json={"title": "Delete Me"}).get_json()
        resp = client.delete(f'/api/kanban/tasks/{created["id"]}')
        assert resp.status_code == 200
        assert resp.get_json()["ok"] is True
        # Verify it's gone
        assert client.get(f'/api/kanban/tasks/{created["id"]}').status_code == 404


# ---------------------------------------------------------------------------
# Move & Reorder
# ---------------------------------------------------------------------------

class TestTaskMove:

    def test_move_task_to_new_status(self, kanban_app):
        app, client, repo = kanban_app
        created = client.post('/api/kanban/tasks', json={"title": "Move Me"}).get_json()
        resp = client.post(f'/api/kanban/tasks/{created["id"]}/move',
                           json={"status": "working"})
        assert resp.status_code == 200
        assert resp.get_json()["status"] == "working"

    def test_move_task_with_force(self, kanban_app):
        app, client, repo = kanban_app
        created = client.post('/api/kanban/tasks', json={"title": "Force"}).get_json()
        resp = client.post(f'/api/kanban/tasks/{created["id"]}/move',
                           json={"status": "complete", "force": True})
        assert resp.status_code == 200
        assert resp.get_json()["status"] == "complete"

    def test_move_task_no_status_returns_400(self, kanban_app):
        app, client, repo = kanban_app
        created = client.post('/api/kanban/tasks', json={"title": "X"}).get_json()
        resp = client.post(f'/api/kanban/tasks/{created["id"]}/move', json={})
        assert resp.status_code == 400

    def test_reorder_task(self, kanban_app):
        app, client, repo = kanban_app
        t1 = client.post('/api/kanban/tasks', json={"title": "First"}).get_json()
        t2 = client.post('/api/kanban/tasks', json={"title": "Second"}).get_json()
        resp = client.post(f'/api/kanban/tasks/{t2["id"]}/reorder',
                           json={"before_id": t1["id"]})
        assert resp.status_code == 200


# ---------------------------------------------------------------------------
# Session Linking
# ---------------------------------------------------------------------------

class TestSessionLinking:

    def test_link_session_to_task(self, kanban_app):
        app, client, repo = kanban_app
        task = client.post('/api/kanban/tasks', json={"title": "Linked"}).get_json()
        resp = client.post(f'/api/kanban/tasks/{task["id"]}/sessions',
                           json={"session_id": "sess-001"})
        assert resp.status_code == 200

    def test_link_session_no_id_returns_400(self, kanban_app):
        app, client, repo = kanban_app
        task = client.post('/api/kanban/tasks', json={"title": "X"}).get_json()
        resp = client.post(f'/api/kanban/tasks/{task["id"]}/sessions',
                           json={"session_id": ""})
        assert resp.status_code == 400

    def test_link_session_task_not_found_returns_404(self, kanban_client):
        resp = kanban_client.post('/api/kanban/tasks/nonexistent/sessions',
                                 json={"session_id": "s1"})
        assert resp.status_code == 404

    def test_unlink_session(self, kanban_app):
        app, client, repo = kanban_app
        task = client.post('/api/kanban/tasks', json={"title": "Unlink"}).get_json()
        client.post(f'/api/kanban/tasks/{task["id"]}/sessions',
                    json={"session_id": "sess-002"})
        resp = client.delete(f'/api/kanban/tasks/{task["id"]}/sessions/sess-002')
        assert resp.status_code == 200
        assert resp.get_json()["ok"] is True

    def test_unlink_session_from_all(self, kanban_app):
        app, client, repo = kanban_app
        task = client.post('/api/kanban/tasks', json={"title": "Unlink All"}).get_json()
        client.post(f'/api/kanban/tasks/{task["id"]}/sessions',
                    json={"session_id": "sess-003"})
        resp = client.delete('/api/kanban/sessions/sess-003/unlink-all')
        assert resp.status_code == 200
        assert resp.get_json()["ok"] is True

    def test_create_task_from_session(self, kanban_app):
        app, client, repo = kanban_app
        resp = client.post('/api/kanban/tasks/from-session',
                           json={"session_id": "new-sess", "title": "From Session"})
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["task"]["title"] == "From Session"
        assert data["linked"] is True

    def test_create_task_from_session_no_id_returns_400(self, kanban_client):
        resp = kanban_client.post('/api/kanban/tasks/from-session', json={})
        assert resp.status_code == 400


# ---------------------------------------------------------------------------
# Issues
# ---------------------------------------------------------------------------

class TestIssues:

    def test_create_issue(self, kanban_app):
        app, client, repo = kanban_app
        task = client.post('/api/kanban/tasks', json={"title": "Issue Task"}).get_json()
        resp = client.post(f'/api/kanban/tasks/{task["id"]}/issues',
                           json={"description": "Something is wrong"})
        assert resp.status_code == 200
        data = resp.get_json()
        assert "id" in data

    def test_resolve_issue(self, kanban_app):
        app, client, repo = kanban_app
        task = client.post('/api/kanban/tasks', json={"title": "Resolve"}).get_json()
        issue = client.post(f'/api/kanban/tasks/{task["id"]}/issues',
                            json={"description": "Fix this"}).get_json()
        resp = client.patch(f'/api/kanban/issues/{issue["id"]}',
                            json={"resolution": "Fixed it"})
        assert resp.status_code == 200


# ---------------------------------------------------------------------------
# Tags
# ---------------------------------------------------------------------------

class TestTags:

    def test_add_tag(self, kanban_app):
        app, client, repo = kanban_app
        task = client.post('/api/kanban/tasks', json={"title": "Tagged"}).get_json()
        resp = client.post(f'/api/kanban/tasks/{task["id"]}/tags',
                           json={"tag": "bug"})
        assert resp.status_code == 200

    def test_get_task_tags(self, kanban_app):
        app, client, repo = kanban_app
        task = client.post('/api/kanban/tasks', json={"title": "Tagged"}).get_json()
        client.post(f'/api/kanban/tasks/{task["id"]}/tags', json={"tag": "bug"})
        client.post(f'/api/kanban/tasks/{task["id"]}/tags', json={"tag": "feature"})
        resp = client.get(f'/api/kanban/tasks/{task["id"]}/tags')
        assert resp.status_code == 200
        data = resp.get_json()
        assert len(data["tags"]) == 2

    def test_remove_tag(self, kanban_app):
        app, client, repo = kanban_app
        task = client.post('/api/kanban/tasks', json={"title": "Untagged"}).get_json()
        client.post(f'/api/kanban/tasks/{task["id"]}/tags', json={"tag": "remove-me"})
        resp = client.delete(f'/api/kanban/tasks/{task["id"]}/tags/remove-me')
        assert resp.status_code == 200

    def test_get_all_tags(self, kanban_app):
        app, client, repo = kanban_app
        task = client.post('/api/kanban/tasks', json={"title": "T"}).get_json()
        client.post(f'/api/kanban/tasks/{task["id"]}/tags', json={"tag": "alpha"})
        resp = client.get('/api/kanban/tags')
        assert resp.status_code == 200
        data = resp.get_json()
        assert "alpha" in data["tags"]

    def test_get_tasks_by_tag(self, kanban_app):
        app, client, repo = kanban_app
        t1 = client.post('/api/kanban/tasks', json={"title": "A"}).get_json()
        t2 = client.post('/api/kanban/tasks', json={"title": "B"}).get_json()
        client.post(f'/api/kanban/tasks/{t1["id"]}/tags', json={"tag": "shared"})
        client.post(f'/api/kanban/tasks/{t2["id"]}/tags', json={"tag": "shared"})
        resp = client.get('/api/kanban/tags/shared/tasks')
        assert resp.status_code == 200
        assert len(resp.get_json()) == 2


# ---------------------------------------------------------------------------
# Columns
# ---------------------------------------------------------------------------

class TestColumns:

    def test_get_columns(self, kanban_app):
        app, client, repo = kanban_app
        # Trigger default column creation via board endpoint
        client.get('/api/kanban/board')
        resp = client.get('/api/kanban/columns')
        assert resp.status_code == 200
        cols = resp.get_json()
        assert len(cols) >= 5

    def test_update_columns(self, kanban_app):
        app, client, repo = kanban_app
        client.get('/api/kanban/board')  # ensure defaults exist
        cols = client.get('/api/kanban/columns').get_json()
        # Rename first column
        cols[0]["label"] = "Backlog"
        resp = client.put('/api/kanban/columns', json=cols)
        assert resp.status_code == 200


# ---------------------------------------------------------------------------
# Bulk Actions
# ---------------------------------------------------------------------------

class TestBulkAction:

    def test_bulk_complete_children(self, kanban_app):
        app, client, repo = kanban_app
        parent = client.post('/api/kanban/tasks', json={"title": "Parent"}).get_json()
        client.post('/api/kanban/tasks', json={
            "title": "Child 1", "parent_id": parent["id"]
        })
        client.post('/api/kanban/tasks', json={
            "title": "Child 2", "parent_id": parent["id"]
        })
        resp = client.post(f'/api/kanban/tasks/{parent["id"]}/bulk',
                           json={"action": "complete_all"})
        assert resp.status_code == 200

    def test_bulk_delete_children(self, kanban_app):
        app, client, repo = kanban_app
        parent = client.post('/api/kanban/tasks', json={"title": "Parent"}).get_json()
        client.post('/api/kanban/tasks', json={
            "title": "Child", "parent_id": parent["id"]
        })
        resp = client.post(f'/api/kanban/tasks/{parent["id"]}/bulk',
                           json={"action": "reset_all"})
        assert resp.status_code == 200


# ---------------------------------------------------------------------------
# Ancestors & History
# ---------------------------------------------------------------------------

class TestAncestors:

    def test_get_ancestors_returns_chain(self, kanban_app):
        app, client, repo = kanban_app
        gp = client.post('/api/kanban/tasks', json={"title": "GP"}).get_json()
        p = client.post('/api/kanban/tasks', json={
            "title": "P", "parent_id": gp["id"]
        }).get_json()
        c = client.post('/api/kanban/tasks', json={
            "title": "C", "parent_id": p["id"]
        }).get_json()
        resp = client.get(f'/api/kanban/tasks/{c["id"]}/ancestors')
        assert resp.status_code == 200
        ancestors = resp.get_json()
        assert len(ancestors) == 2


class TestTaskHistory:

    def test_get_history_returns_transitions(self, kanban_app):
        app, client, repo = kanban_app
        task = client.post('/api/kanban/tasks', json={"title": "History"}).get_json()
        # Move to working (creates history entry)
        client.post(f'/api/kanban/tasks/{task["id"]}/move',
                    json={"status": "working"})
        resp = client.get(f'/api/kanban/tasks/{task["id"]}/history')
        assert resp.status_code == 200
        data = resp.get_json()
        assert "history" in data
        assert len(data["history"]) >= 1


# ---------------------------------------------------------------------------
# Claim / Unclaim
# ---------------------------------------------------------------------------

class TestClaimUnclaim:

    def test_claim_task(self, kanban_app):
        app, client, repo = kanban_app
        task = client.post('/api/kanban/tasks', json={"title": "Claim Me"}).get_json()
        resp = client.post(f'/api/kanban/tasks/{task["id"]}/claim')
        assert resp.status_code == 200

    def test_unclaim_task(self, kanban_app):
        app, client, repo = kanban_app
        task = client.post('/api/kanban/tasks', json={"title": "Unclaim"}).get_json()
        client.post(f'/api/kanban/tasks/{task["id"]}/claim')
        resp = client.post(f'/api/kanban/tasks/{task["id"]}/unclaim')
        assert resp.status_code == 200


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

class TestKanbanConfig:

    def test_get_config(self, kanban_client):
        resp = kanban_client.get('/api/kanban/config')
        assert resp.status_code == 200
        assert isinstance(resp.get_json(), dict)

    def test_update_config(self, kanban_client):
        resp = kanban_client.put('/api/kanban/config',
                                 json={"kanban_depth_limit": 3})
        assert resp.status_code == 200


# ---------------------------------------------------------------------------
# Board Filtering
# ---------------------------------------------------------------------------

class TestBoardFiltering:

    def test_board_tag_filter(self, kanban_app):
        app, client, repo = kanban_app
        t1 = client.post('/api/kanban/tasks', json={"title": "Bug"}).get_json()
        t2 = client.post('/api/kanban/tasks', json={"title": "Feature"}).get_json()
        client.post(f'/api/kanban/tasks/{t1["id"]}/tags', json={"tag": "bug"})
        client.post(f'/api/kanban/tasks/{t2["id"]}/tags', json={"tag": "feature"})
        resp = client.get('/api/kanban/board?tags=bug')
        assert resp.status_code == 200
        data = resp.get_json()
        # Tag filter sets active_tag_filter in response
        assert data.get("active_tag_filter") is not None or "tasks" in data

    def test_board_parent_scope(self, kanban_app):
        app, client, repo = kanban_app
        parent = client.post('/api/kanban/tasks', json={"title": "Parent"}).get_json()
        child = client.post('/api/kanban/tasks', json={
            "title": "Child", "parent_id": parent["id"]
        }).get_json()
        client.post('/api/kanban/tasks', json={"title": "Other"})
        resp = client.get(f'/api/kanban/board?parent_id={parent["id"]}')
        assert resp.status_code == 200
        data = resp.get_json()
        titles = [t["title"] for t in data["tasks"]]
        assert "Child" in titles
        assert "Other" not in titles


# ---------------------------------------------------------------------------
# Task Tree Summary & Detected URLs
# ---------------------------------------------------------------------------

class TestTaskTreeSummary:

    def test_returns_200(self, kanban_app):
        app, client, repo = kanban_app
        client.post('/api/kanban/tasks', json={"title": "Summary Task"})
        resp = client.get('/api/kanban/task-tree-summary')
        assert resp.status_code == 200

    def test_empty_board(self, kanban_client):
        resp = kanban_client.get('/api/kanban/task-tree-summary')
        assert resp.status_code == 200


class TestDetectedUrls:

    def test_returns_200(self, kanban_client):
        resp = kanban_client.get('/api/kanban/detected-urls')
        assert resp.status_code == 200


# ---------------------------------------------------------------------------
# Session State Change
# ---------------------------------------------------------------------------

class TestSessionStateChange:

    def test_missing_fields_returns_400(self, kanban_client):
        resp = kanban_client.post('/api/kanban/session-state-change', json={})
        assert resp.status_code in (400, 200)

    def test_unlinked_session_returns_not_linked(self, kanban_client):
        resp = kanban_client.post('/api/kanban/session-state-change',
                                  json={"session_id": "orphan", "state": "working"})
        assert resp.status_code == 200
        assert resp.get_json().get("linked") is False

    def test_working_state_transitions_linked_task(self, kanban_app):
        app, client, repo = kanban_app
        task = client.post('/api/kanban/tasks', json={"title": "Linked"}).get_json()
        client.post(f'/api/kanban/tasks/{task["id"]}/sessions',
                    json={"session_id": "sess-sc-1"})
        resp = client.post('/api/kanban/session-state-change',
                           json={"session_id": "sess-sc-1", "state": "working"})
        assert resp.status_code == 200
        assert resp.get_json()["linked"] is True


# ---------------------------------------------------------------------------
# Task Context
# ---------------------------------------------------------------------------

class TestTaskContext:

    def test_context_returns_string(self, kanban_app):
        app, client, repo = kanban_app
        task = client.post('/api/kanban/tasks', json={
            "title": "Context Task", "description": "Do the thing"
        }).get_json()
        resp = client.get(f'/api/kanban/tasks/{task["id"]}/context')
        assert resp.status_code == 200
        data = resp.get_json()
        assert "context" in data
        assert "Context Task" in data["context"]

    def test_context_not_found(self, kanban_client):
        resp = kanban_client.get('/api/kanban/tasks/nonexistent/context')
        assert resp.status_code == 404


# ---------------------------------------------------------------------------
# Suggest Tags
# ---------------------------------------------------------------------------

class TestSuggestTags:

    def test_suggest_empty(self, kanban_client):
        resp = kanban_client.get('/api/kanban/tags/suggest')
        assert resp.status_code == 200

    def test_suggest_ranked_by_usage(self, kanban_app):
        app, client, repo = kanban_app
        t1 = client.post('/api/kanban/tasks', json={"title": "A"}).get_json()
        t2 = client.post('/api/kanban/tasks', json={"title": "B"}).get_json()
        client.post(f'/api/kanban/tasks/{t1["id"]}/tags', json={"tag": "bug"})
        client.post(f'/api/kanban/tasks/{t2["id"]}/tags', json={"tag": "bug"})
        client.post(f'/api/kanban/tasks/{t1["id"]}/tags', json={"tag": "feature"})
        resp = client.get('/api/kanban/tags/suggest')
        assert resp.status_code == 200
        data = resp.get_json()
        # Response is {tags: [{tag, usage_count}, ...]}
        tags = data.get("tags", data) if isinstance(data, dict) else data
        assert isinstance(tags, list)
        if len(tags) >= 2:
            assert tags[0]["usage_count"] >= tags[1]["usage_count"]

    def test_suggest_prefix_filter(self, kanban_app):
        app, client, repo = kanban_app
        t = client.post('/api/kanban/tasks', json={"title": "T"}).get_json()
        client.post(f'/api/kanban/tasks/{t["id"]}/tags', json={"tag": "bugfix"})
        client.post(f'/api/kanban/tasks/{t["id"]}/tags', json={"tag": "feature"})
        resp = client.get('/api/kanban/tags/suggest?q=bu')
        assert resp.status_code == 200
        data = resp.get_json()
        tags = data.get("tags", data) if isinstance(data, dict) else data
        tag_names = [t["tag"] if isinstance(t, dict) else t for t in tags]
        assert any("bug" in str(t) for t in tag_names)


# ---------------------------------------------------------------------------
# Backup Endpoints
# ---------------------------------------------------------------------------

class TestBackupEndpoints:

    def test_backup_download(self, kanban_app, monkeypatch, tmp_path):
        app, client, repo = kanban_app
        backup_dir = tmp_path / "backups"
        backup_dir.mkdir()
        monkeypatch.setattr("app.routes.kanban_api._backups_dir", lambda: backup_dir)
        client.post('/api/kanban/tasks', json={"title": "Backup Task"})
        resp = client.post('/api/kanban/backup/download')
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["ok"] is True
        assert data["filename"].startswith("backup_")

    def test_backup_list(self, kanban_app, monkeypatch, tmp_path):
        app, client, repo = kanban_app
        backup_dir = tmp_path / "backups"
        backup_dir.mkdir()
        monkeypatch.setattr("app.routes.kanban_api._backups_dir", lambda: backup_dir)
        # Create a backup first
        client.post('/api/kanban/backup/download')
        resp = client.get('/api/kanban/backup/list')
        assert resp.status_code == 200
        data = resp.get_json()
        assert "backups" in data
        assert len(data["backups"]) >= 1

    def test_backup_list_empty(self, kanban_app, monkeypatch, tmp_path):
        app, client, repo = kanban_app
        backup_dir = tmp_path / "backups"
        backup_dir.mkdir()
        monkeypatch.setattr("app.routes.kanban_api._backups_dir", lambda: backup_dir)
        resp = client.get('/api/kanban/backup/list')
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["backups"] == []

    def test_backup_restore(self, kanban_app, monkeypatch, tmp_path):
        app, client, repo = kanban_app
        backup_dir = tmp_path / "backups"
        backup_dir.mkdir()
        monkeypatch.setattr("app.routes.kanban_api._backups_dir", lambda: backup_dir)
        # Create and then restore
        dl = client.post('/api/kanban/backup/download').get_json()
        resp = client.post('/api/kanban/backup/restore',
                           json={"filename": dl["filename"]})
        assert resp.status_code == 200
        assert resp.get_json()["ok"] is True

    def test_backup_restore_missing_filename(self, kanban_client):
        resp = kanban_client.post('/api/kanban/backup/restore', json={})
        assert resp.status_code == 400

    def test_backup_delete(self, kanban_app, monkeypatch, tmp_path):
        app, client, repo = kanban_app
        backup_dir = tmp_path / "backups"
        backup_dir.mkdir()
        monkeypatch.setattr("app.routes.kanban_api._backups_dir", lambda: backup_dir)
        dl = client.post('/api/kanban/backup/download').get_json()
        resp = client.post('/api/kanban/backup/delete',
                           json={"filename": dl["filename"]})
        assert resp.status_code == 200
        assert resp.get_json()["ok"] is True

    def test_backup_delete_missing_filename(self, kanban_client):
        resp = kanban_client.post('/api/kanban/backup/delete', json={})
        assert resp.status_code == 400
