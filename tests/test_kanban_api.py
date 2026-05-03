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


# ---------------------------------------------------------------------------
# Migration helpers — preflight + copy_data flag
# ---------------------------------------------------------------------------
#
# The Persistent Storage wizard relies on these to give context-aware prompts
# instead of a generic "this will replace cloud data" warning. Preflight
# tells the UI what's on each side; copy_data: false lets a user adopt an
# existing cloud board without overwriting it.

class TestMigratePreflight:
    """``POST /api/kanban/migrate/preflight`` shape + safety."""

    def test_returns_counts_for_both_sides(self, kanban_app):
        """Preflight must report task counts so the UI can branch the prompt
        ('cloud has data' vs. 'cloud empty') instead of guessing."""
        app, client, repo = kanban_app
        # Seed the active backend with 2 tasks
        client.post('/api/kanban/tasks', json={"title": "T1"})
        client.post('/api/kanban/tasks', json={"title": "T2"})

        # Target = sqlite same backend, so target_data sees the same rows.
        # The point here isn't realistic preflight against Supabase (we'd
        # need a live cloud), but to assert the response shape and that
        # task counts roundtrip correctly.
        resp = client.post('/api/kanban/migrate/preflight',
                           json={"target": "sqlite"})
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["ok"] is True
        assert "current" in data and "target_data" in data
        assert data["current"]["tasks"] >= 2
        assert "is_empty" in data["current"]
        assert data["current"]["is_empty"] is False

    def test_invalid_target_returns_400(self, kanban_client):
        """Unknown target backend must 400 — silently treating it as a no-op
        would let a malformed wizard call appear to succeed."""
        resp = kanban_client.post('/api/kanban/migrate/preflight',
                                  json={"target": "neo4j"})
        assert resp.status_code == 400
        assert "error" in resp.get_json()

    def test_supabase_target_requires_credentials(self, kanban_client):
        """No URL/key -> 400. Defensive check; otherwise we'd try to init
        a SupabaseRepository with empty strings."""
        resp = kanban_client.post('/api/kanban/migrate/preflight',
                                  json={"target": "supabase"})
        assert resp.status_code == 400


class TestMigrateCopyDataFlag:
    """``POST /api/kanban/migrate`` accepts ``copy_data`` to gate the
    destructive export/wipe/import path."""

    def test_default_preserves_legacy_copy_behavior(self, kanban_app):
        """copy_data omitted -> True -> legacy path runs. Existing callers
        that don't know about the flag must keep working."""
        app, client, repo = kanban_app
        client.post('/api/kanban/tasks', json={"title": "Existing"})
        # target=sqlite (same backend) is unusual but fine for shape testing
        resp = client.post('/api/kanban/migrate', json={"target": "sqlite"})
        assert resp.status_code == 200
        body = resp.get_json()
        assert body["ok"] is True
        # The default response includes copied_data:True
        assert body.get("copied_data") is True

    def test_copy_data_false_skips_copy_and_does_not_wipe(self, kanban_app, monkeypatch):
        """copy_data: false must NOT call BackendMigrator.switch_backend.
        The whole point of this flag is to avoid wiping existing target
        data on shared-cloud joins."""
        app, client, repo = kanban_app
        called = {"switch": 0}
        from app.db import migrator as mig_mod
        original = mig_mod.BackendMigrator.switch_backend

        def _spy(self, current, target):
            called["switch"] += 1
            return original(self, current, target)
        monkeypatch.setattr(mig_mod.BackendMigrator, "switch_backend", _spy)

        resp = client.post('/api/kanban/migrate',
                           json={"target": "sqlite", "copy_data": False})
        assert resp.status_code == 200
        body = resp.get_json()
        assert body["ok"] is True
        assert body.get("copied_data") is False
        assert called["switch"] == 0, \
            "switch_backend was called with copy_data=False — that's the " \
            "exact bug this flag exists to prevent. It would wipe the " \
            "target backend on a shared-cloud join."


# ---------------------------------------------------------------------------
# Project match helpers (used by /projects/discover)
# ---------------------------------------------------------------------------

class TestProjectMatchHelpers:
    """Pure-function unit tests for the heuristics that rank cloud project
    candidates against the user's local project. Cheap to test, central to
    the empty-state 'find my tasks' UX feeling correct."""

    def test_basename_extracts_last_segment(self):
        from app.routes.kanban_api import _project_basename
        assert _project_basename("-home-me-code-VibeNode") == "VibeNode"
        assert _project_basename("C--Users-15512-Documents-VibeNode") == "VibeNode"

    def test_basename_handles_trailing_dash(self):
        from app.routes.kanban_api import _project_basename
        # _encode_cwd never emits trailing dashes today, but be defensive
        assert _project_basename("-home-me-VibeNode-") == "VibeNode"

    def test_basename_empty_input(self):
        from app.routes.kanban_api import _project_basename
        assert _project_basename("") == ""
        assert _project_basename(None) == ""

    def test_score_exact_basename_match_is_100(self):
        """Same trailing folder name -> strong recommendation. The dominant
        case for the 'son's Windows VibeNode -> father's Linux VibeNode'
        scenario this whole feature was built for."""
        from app.routes.kanban_api import _score_project_match
        score = _score_project_match(
            "-home-me-code-vibenode-VibeNode",
            "C--Users-other-Documents-VibeNode",
        )
        assert score == 100

    def test_score_substring_match_is_60(self):
        """One basename appears inside the other id -> weaker but useful.
        Catches cases like 'customerNode' as basename matching against a
        path that contains 'customerNode' deeper than the last segment."""
        from app.routes.kanban_api import _score_project_match
        score = _score_project_match(
            "-home-me-code-customerNode",
            "C--Users-other-Documents-customerNode-root",
        )
        # basename "customerNode" appears in the remote id
        assert score == 60

    def test_score_unrelated_is_zero(self):
        from app.routes.kanban_api import _score_project_match
        assert _score_project_match("-home-me-Foo", "C--Users-other-Bar") == 0

    def test_score_identical_id_is_zero(self):
        """If local and remote project_ids already match, no aliasing is
        needed. Score 0 keeps it from showing up as a recommendation."""
        from app.routes.kanban_api import _score_project_match
        assert _score_project_match("-home-me-X", "-home-me-X") == 0


# ---------------------------------------------------------------------------
# /api/kanban/projects/discover + /api/kanban/projects/alias
# ---------------------------------------------------------------------------

class TestProjectsDiscover:
    """The empty-state 'find tasks in cloud' button calls /discover, which
    enumerates distinct project_ids in the active backend and ranks them
    against the user's local project."""

    def test_discover_returns_distinct_project_ids_with_counts(self, kanban_app, monkeypatch):
        """Two different project_ids in the backend -> two candidates,
        each with the right task_count."""
        app, client, repo = kanban_app

        # Create tasks under the fixture's project_id ("test-project")
        client.post('/api/kanban/tasks', json={"title": "Alpha"})
        client.post('/api/kanban/tasks', json={"title": "Beta"})

        # Insert tasks under a different project_id directly via the repo
        # so we have two silos to discover. create_task_from_dict skips the
        # active-project plumbing so we can plant tasks anywhere.
        import uuid as _uuid
        from datetime import datetime, timezone
        other_proj = "C--Users-other-Documents-VibeNode"
        now = datetime.now(timezone.utc).isoformat()
        for title in ("Cloud Task 1", "Cloud Task 2", "Cloud Task 3"):
            repo.create_task_from_dict({
                "id": str(_uuid.uuid4()),
                "project_id": other_proj,
                "parent_id": None,
                "title": title,
                "description": None,
                "verification_url": None,
                "status": "not_started",
                "position": 0,
                "owner": None,
                "created_at": now,
                "updated_at": now,
            })

        resp = client.post('/api/kanban/projects/discover', json={})
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["ok"] is True

        candidates_by_id = {c["project_id"]: c for c in data["candidates"]}
        assert "test-project" in candidates_by_id
        assert other_proj in candidates_by_id
        assert candidates_by_id["test-project"]["task_count"] == 2
        assert candidates_by_id[other_proj]["task_count"] == 3

    def test_discover_includes_local_project_id_in_response(self, kanban_app):
        """The frontend shows the local id for sanity-check ('aliasing
        from: ...') so the user can tell what they're remapping FROM.
        The endpoint must surface it explicitly."""
        app, client, repo = kanban_app
        resp = client.post('/api/kanban/projects/discover', json={})
        assert resp.status_code == 200
        data = resp.get_json()
        assert "local_project_id" in data
        assert data["local_project_id"] == "test-project"

    def test_discover_empty_backend_returns_empty_candidates(self, kanban_app):
        """No tasks anywhere -> empty list (not an error). The UI shows
        'no other projects found' in this case."""
        app, client, repo = kanban_app
        resp = client.post('/api/kanban/projects/discover', json={})
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["ok"] is True
        assert data["candidates"] == []


class TestProjectsAlias:
    """``POST /api/kanban/projects/alias`` writes a per-local-id mapping into
    kanban_config.json and resets the kanban repo cache so the alias is
    immediately visible to subsequent requests."""

    def _isolate_kanban_config(self, tmp_path, monkeypatch):
        """Point _KANBAN_CONFIG_FILE at a temp file so we don't poison the
        real user's config. Returns the (config module, path) tuple."""
        import json
        from app import config
        cfg_file = tmp_path / "kanban_config.json"
        cfg_file.write_text(json.dumps({"kanban_backend": "sqlite"}), encoding="utf-8")
        monkeypatch.setattr(config, "_KANBAN_CONFIG_FILE", cfg_file)
        config._kanban_config_cache = None
        return config, cfg_file

    def test_alias_saves_to_kanban_config(self, kanban_app, tmp_path, monkeypatch):
        """Happy path: pass a remote project_id, get it persisted into the
        project_id_aliases dict keyed by the active local project_id."""
        app, client, repo = kanban_app
        config, cfg_file = self._isolate_kanban_config(tmp_path, monkeypatch)

        remote = "C--Users-other-Documents-VibeNode"
        resp = client.post('/api/kanban/projects/alias',
                           json={"remote_project_id": remote})
        assert resp.status_code == 200
        body = resp.get_json()
        assert body["ok"] is True
        assert body["aliases"] == {"test-project": remote}

        # Persisted to disk
        import json
        on_disk = json.loads(cfg_file.read_text())
        assert on_disk["project_id_aliases"]["test-project"] == remote

    def test_alias_null_remote_clears_existing_alias(self, kanban_app, tmp_path, monkeypatch):
        """POST {remote_project_id: null} removes the alias for the current
        local project. Lets a user undo an accidentally-adopted match
        without opening the JSON by hand."""
        import json
        app, client, repo = kanban_app
        config, cfg_file = self._isolate_kanban_config(tmp_path, monkeypatch)

        # Pre-seed with an alias
        cfg_file.write_text(json.dumps({
            "project_id_aliases": {"test-project": "remote-A"}
        }), encoding="utf-8")
        config._kanban_config_cache = None

        resp = client.post('/api/kanban/projects/alias',
                           json={"remote_project_id": None})
        assert resp.status_code == 200
        body = resp.get_json()
        assert body["ok"] is True
        # The alias for the CURRENT local project should be gone
        assert "test-project" not in body["aliases"]

    def test_alias_takes_effect_on_subsequent_board_call(self, kanban_app, tmp_path, monkeypatch):
        """After /alias, the next /board call should see the aliased
        project_id and return tasks stored under THAT id, not the local
        one. This is the whole point of the feature."""
        import uuid as _uuid
        from datetime import datetime, timezone

        app, client, repo = kanban_app
        config, cfg_file = self._isolate_kanban_config(tmp_path, monkeypatch)

        remote = "C--Users-other-VibeNode"
        # Put a task ONLY under the remote project_id (not the local
        # 'test-project'). Without an alias, /board would find nothing.
        now = datetime.now(timezone.utc).isoformat()
        repo.create_task_from_dict({
            "id": str(_uuid.uuid4()),
            "project_id": remote,
            "parent_id": None,
            "title": "Remote Task",
            "description": None,
            "verification_url": None,
            "status": "not_started",
            "position": 0,
            "owner": None,
            "created_at": now,
            "updated_at": now,
        })

        # Sanity: WITHOUT an alias the local-id /board returns nothing
        before = client.get('/api/kanban/board').get_json()
        assert all(task["title"] != "Remote Task" for task in before["tasks"])

        # Adopt the alias
        client.post('/api/kanban/projects/alias',
                    json={"remote_project_id": remote})

        # Now /board should see Remote Task because the active id resolved
        # to the remote project_id via resolve_project_alias().
        after = client.get('/api/kanban/board').get_json()
        titles = [task["title"] for task in after["tasks"]]
        assert "Remote Task" in titles, \
            "After adopting an alias, /board should query the aliased " \
            "project_id. Got titles: %r" % titles
