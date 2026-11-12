"""Integration tests for KanbanRepository (SQLite backend).

Tests run against a real temporary SQLite database — no mocks.
Covers CRUD, ordering, session linking, tags, issues, and board retrieval.
"""

import os
import tempfile
import uuid
from datetime import datetime, timezone

from web.db.repository import (
    BoardColumn,
    KanbanRepository,
    Task,
    TaskStatus,
)
from web.db.sqlite_backend import SqliteRepository


PROJECT_ID = "test-project"


def _make_repo():
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    repo = SqliteRepository(db_path=path)
    repo.initialize()
    return repo, path


def _now():
    return datetime.now(timezone.utc).isoformat()


def _task(repo, title="task", status="not_started", parent_id=None):
    pos = repo.get_next_position(PROJECT_ID, status)
    t = Task(
        id=str(uuid.uuid4()),
        project_id=PROJECT_ID,
        parent_id=parent_id,
        title=title,
        description=None,
        verification_url=None,
        status=TaskStatus(status),
        position=pos,
        depth=0,
        created_at=_now(),
        updated_at=_now(),
    )
    return repo.create_task(t)


class TestTaskCRUD:

    def test_create_and_get(self):
        repo, path = _make_repo()
        try:
            t = _task(repo, "hello")
            fetched = repo.get_task(t.id)
            assert fetched is not None
            assert fetched.title == "hello"
            assert fetched.status == TaskStatus.NOT_STARTED
        finally:
            repo.close()
            os.unlink(path)

    def test_update_task_partial(self):
        repo, path = _make_repo()
        try:
            t = _task(repo, "original")
            updated = repo.update_task(t.id, title="renamed")
            assert updated.title == "renamed"
            assert updated.status == TaskStatus.NOT_STARTED  # unchanged
        finally:
            repo.close()
            os.unlink(path)

    def test_delete_task(self):
        repo, path = _make_repo()
        try:
            t = _task(repo, "doomed")
            repo.delete_task(t.id)
            assert repo.get_task(t.id) is None
        finally:
            repo.close()
            os.unlink(path)

    def test_get_nonexistent_returns_none(self):
        repo, path = _make_repo()
        try:
            assert repo.get_task("nonexistent") is None
        finally:
            repo.close()
            os.unlink(path)


class TestChildHierarchy:

    def test_get_children_returns_immediate(self):
        repo, path = _make_repo()
        try:
            parent = _task(repo, "parent")
            c1 = _task(repo, "child1", parent_id=parent.id)
            c2 = _task(repo, "child2", parent_id=parent.id)
            children = repo.get_children(parent.id)
            ids = {c.id for c in children}
            assert c1.id in ids
            assert c2.id in ids
            assert len(children) == 2
        finally:
            repo.close()
            os.unlink(path)

    def test_get_ancestors_walks_up(self):
        repo, path = _make_repo()
        try:
            root = _task(repo, "root")
            mid = _task(repo, "mid", parent_id=root.id)
            leaf = _task(repo, "leaf", parent_id=mid.id)
            ancestors = repo.get_ancestors(leaf.id)
            titles = [a.title for a in ancestors]
            assert "mid" in titles
            assert "root" in titles
        finally:
            repo.close()
            os.unlink(path)

    def test_children_ordered_by_position(self):
        repo, path = _make_repo()
        try:
            parent = _task(repo, "parent")
            c1 = _task(repo, "first", parent_id=parent.id)
            c2 = _task(repo, "second", parent_id=parent.id)
            children = repo.get_children(parent.id)
            assert children[0].position <= children[1].position
        finally:
            repo.close()
            os.unlink(path)


class TestOrdering:

    def test_next_position_increments(self):
        repo, path = _make_repo()
        try:
            p1 = repo.get_next_position(PROJECT_ID, "not_started")
            _task(repo, "a")
            p2 = repo.get_next_position(PROJECT_ID, "not_started")
            assert p2 > p1
        finally:
            repo.close()
            os.unlink(path)

    def test_get_tasks_by_status(self):
        repo, path = _make_repo()
        try:
            _task(repo, "ns1", "not_started")
            _task(repo, "w1", "working")
            ns_tasks = repo.get_tasks_by_status(PROJECT_ID, TaskStatus.NOT_STARTED)
            assert all(t.status == TaskStatus.NOT_STARTED for t in ns_tasks)
            assert any(t.title == "ns1" for t in ns_tasks)
        finally:
            repo.close()
            os.unlink(path)


class TestSessionLinks:

    def test_link_and_get(self):
        repo, path = _make_repo()
        try:
            t = _task(repo, "linked")
            sess_id = "session-abc"
            repo.link_session(t.id, sess_id)
            sessions = repo.get_task_sessions(t.id)
            session_ids = [s.session_id if hasattr(s, 'session_id') else s for s in sessions]
            assert sess_id in session_ids
        finally:
            repo.close()
            os.unlink(path)

    def test_unlink_removes(self):
        repo, path = _make_repo()
        try:
            t = _task(repo, "linked2")
            sess_id = "session-xyz"
            repo.link_session(t.id, sess_id)
            repo.unlink_session(t.id, sess_id)
            sessions = repo.get_task_sessions(t.id)
            session_ids = [s.session_id if hasattr(s, 'session_id') else s for s in sessions]
            assert sess_id not in session_ids
        finally:
            repo.close()
            os.unlink(path)

    def test_get_session_task_returns_task_id(self):
        repo, path = _make_repo()
        try:
            t = _task(repo, "owner")
            sess_id = "session-lookup"
            repo.link_session(t.id, sess_id)
            result = repo.get_session_task(sess_id)
            assert result == t.id
        finally:
            repo.close()
            os.unlink(path)


class TestTags:

    def test_add_and_get_tags(self):
        repo, path = _make_repo()
        try:
            t = _task(repo, "tagged")
            repo.add_tag(t.id, "backend")
            repo.add_tag(t.id, "urgent")
            tags = repo.get_task_tags(t.id)
            tag_names = [tt.tag for tt in tags]
            assert "backend" in tag_names
            assert "urgent" in tag_names
        finally:
            repo.close()
            os.unlink(path)

    def test_remove_tag(self):
        repo, path = _make_repo()
        try:
            t = _task(repo, "tagged2")
            repo.add_tag(t.id, "remove-me")
            repo.remove_tag(t.id, "remove-me")
            tags = repo.get_task_tags(t.id)
            assert all(tt.tag != "remove-me" for tt in tags)
        finally:
            repo.close()
            os.unlink(path)

    def test_get_tasks_by_tag(self):
        repo, path = _make_repo()
        try:
            t1 = _task(repo, "has-tag")
            t2 = _task(repo, "no-tag")
            repo.add_tag(t1.id, "special")
            result = repo.get_tasks_by_tag(PROJECT_ID, "special")
            ids = [r.id for r in result]
            assert t1.id in ids
            assert t2.id not in ids
        finally:
            repo.close()
            os.unlink(path)

    def test_get_all_tags(self):
        repo, path = _make_repo()
        try:
            t = _task(repo, "multi-tag")
            repo.add_tag(t.id, "alpha")
            repo.add_tag(t.id, "beta")
            all_tags = repo.get_all_tags(PROJECT_ID)
            assert "alpha" in all_tags
            assert "beta" in all_tags
        finally:
            repo.close()
            os.unlink(path)


class TestIssues:

    def test_create_and_get_issues(self):
        repo, path = _make_repo()
        try:
            t = _task(repo, "issue-task")
            issue = repo.create_issue(t.id, "Something broken")
            open_issues = repo.get_open_issues(t.id)
            assert len(open_issues) >= 1
            assert any(i.description == "Something broken" for i in open_issues)
        finally:
            repo.close()
            os.unlink(path)

    def test_resolve_issue(self):
        repo, path = _make_repo()
        try:
            t = _task(repo, "resolve-task")
            issue = repo.create_issue(t.id, "Fix this")
            repo.resolve_issue(issue.id)
            open_issues = repo.get_open_issues(t.id)
            assert len(open_issues) == 0
            all_issues = repo.get_all_issues(t.id)
            assert len(all_issues) >= 1
            assert all_issues[0].resolved_at is not None
        finally:
            repo.close()
            os.unlink(path)


class TestBoardAndColumns:

    def test_get_board_auto_creates_columns(self):
        repo, path = _make_repo()
        try:
            board = repo.get_board(PROJECT_ID)
            assert "columns" in board
            assert len(board["columns"]) == 5
            status_keys = [c.status_key for c in board["columns"]]
            assert "not_started" in status_keys
            assert "complete" in status_keys
        finally:
            repo.close()
            os.unlink(path)

    def test_get_columns(self):
        repo, path = _make_repo()
        try:
            # Trigger auto-creation
            repo.get_board(PROJECT_ID)
            cols = repo.get_columns(PROJECT_ID)
            assert len(cols) == 5
            assert all(isinstance(c, BoardColumn) for c in cols)
        finally:
            repo.close()
            os.unlink(path)

    def test_execute_sql(self):
        repo, path = _make_repo()
        try:
            _task(repo, "sql-test")
            rows = repo.execute_sql(
                "SELECT title FROM tasks WHERE project_id = ?",
                (PROJECT_ID,),
            )
            assert any(r["title"] == "sql-test" for r in rows)
        finally:
            repo.close()
            os.unlink(path)
