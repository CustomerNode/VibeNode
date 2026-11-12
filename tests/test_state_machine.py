"""Tests for app.kanban.state_machine — status transition validation and propagation."""

import tempfile
import os
from web.db.repository import TaskStatus
from web.db.sqlite_backend import SqliteRepository
from web.kanban.state_machine import (
    VALID_TRANSITIONS,
    transition_task,
    propagate_up,
    handle_session_start,
)


def _make_repo():
    """Create a temporary SQLite repo for testing."""
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    repo = SqliteRepository(db_path=path)
    repo.initialize()
    return repo, path


def _create_task(repo, title="test", status="not_started", parent_id=None):
    """Helper: insert a task and return it."""
    from web.db.repository import Task
    import uuid
    from datetime import datetime, timezone

    now = datetime.now(timezone.utc).isoformat()
    project_id = "test-project"
    pos = repo.get_next_position(project_id, status)
    task = Task(
        id=str(uuid.uuid4()),
        project_id=project_id,
        parent_id=parent_id,
        title=title,
        description=None,
        verification_url=None,
        status=TaskStatus(status),
        position=pos,
        depth=0,
        created_at=now,
        updated_at=now,
    )
    return repo.create_task(task)


class TestValidTransitions:
    """Verify the transition map matches the plan's state diagram."""

    def test_not_started_can_go_to_working(self):
        assert TaskStatus.WORKING in VALID_TRANSITIONS[TaskStatus.NOT_STARTED]

    def test_working_can_go_to_validating(self):
        assert TaskStatus.VALIDATING in VALID_TRANSITIONS[TaskStatus.WORKING]

    def test_validating_can_go_to_complete(self):
        assert TaskStatus.COMPLETE in VALID_TRANSITIONS[TaskStatus.VALIDATING]

    def test_validating_can_go_to_remediating(self):
        assert TaskStatus.REMEDIATING in VALID_TRANSITIONS[TaskStatus.VALIDATING]

    def test_complete_can_go_to_remediating(self):
        assert TaskStatus.REMEDIATING in VALID_TRANSITIONS[TaskStatus.COMPLETE]

    def test_remediating_can_go_to_working(self):
        assert TaskStatus.WORKING in VALID_TRANSITIONS[TaskStatus.REMEDIATING]

    def test_not_started_cannot_go_to_complete(self):
        assert TaskStatus.COMPLETE not in VALID_TRANSITIONS[TaskStatus.NOT_STARTED]

    def test_working_cannot_go_to_not_started(self):
        assert TaskStatus.NOT_STARTED not in VALID_TRANSITIONS[TaskStatus.WORKING]

    def test_complete_cannot_go_to_working(self):
        assert TaskStatus.WORKING not in VALID_TRANSITIONS[TaskStatus.COMPLETE]


class TestTransitionTask:
    """Test transition_task() validation and execution."""

    def test_valid_transition_updates_status(self):
        repo, path = _make_repo()
        try:
            task = _create_task(repo, "t1", "not_started")
            updated = transition_task(repo, task.id, TaskStatus.WORKING)
            assert updated.status == TaskStatus.WORKING
        finally:
            repo.close()
            os.unlink(path)

    def test_invalid_transition_raises(self):
        repo, path = _make_repo()
        try:
            task = _create_task(repo, "t2", "not_started")
            try:
                transition_task(repo, task.id, TaskStatus.COMPLETE)
                assert False, "Should have raised ValueError"
            except ValueError as e:
                assert "Invalid transition" in str(e)
        finally:
            repo.close()
            os.unlink(path)

    def test_same_status_is_noop(self):
        repo, path = _make_repo()
        try:
            task = _create_task(repo, "t3", "not_started")
            result = transition_task(repo, task.id, TaskStatus.NOT_STARTED)
            assert result.status == TaskStatus.NOT_STARTED
        finally:
            repo.close()
            os.unlink(path)

    def test_force_skips_validation(self):
        repo, path = _make_repo()
        try:
            task = _create_task(repo, "t4", "not_started")
            updated = transition_task(repo, task.id, TaskStatus.COMPLETE, force=True)
            assert updated.status == TaskStatus.COMPLETE
        finally:
            repo.close()
            os.unlink(path)

    def test_string_status_accepted(self):
        repo, path = _make_repo()
        try:
            task = _create_task(repo, "t5", "not_started")
            updated = transition_task(repo, task.id, "working")
            assert updated.status == TaskStatus.WORKING
        finally:
            repo.close()
            os.unlink(path)

    def test_invalid_string_status_raises(self):
        repo, path = _make_repo()
        try:
            task = _create_task(repo, "t6", "not_started")
            try:
                transition_task(repo, task.id, "bogus")
                assert False, "Should have raised ValueError"
            except ValueError as e:
                assert "Invalid status" in str(e)
        finally:
            repo.close()
            os.unlink(path)

    def test_nonexistent_task_raises(self):
        repo, path = _make_repo()
        try:
            try:
                transition_task(repo, "no-such-id", TaskStatus.WORKING)
                assert False, "Should have raised ValueError"
            except ValueError as e:
                assert "not found" in str(e)
        finally:
            repo.close()
            os.unlink(path)

    def test_transition_records_history(self):
        repo, path = _make_repo()
        try:
            task = _create_task(repo, "t7", "not_started")
            transition_task(repo, task.id, TaskStatus.WORKING)
            rows = repo.get_status_history(task.id)
            assert len(rows) >= 1
            assert rows[0]["new_status"] == "working"
        finally:
            repo.close()
            os.unlink(path)


class TestPropagateUp:
    """Test upward status propagation rules."""

    def test_child_working_moves_parent_from_not_started(self):
        repo, path = _make_repo()
        try:
            parent = _create_task(repo, "parent", "not_started")
            child = _create_task(repo, "child", "not_started", parent_id=parent.id)
            # Move child to working
            transition_task(repo, child.id, TaskStatus.WORKING)
            # Parent should now be working
            p = repo.get_task(parent.id)
            assert p.status == TaskStatus.WORKING
        finally:
            repo.close()
            os.unlink(path)

    def test_child_remediating_moves_parent_from_complete(self):
        repo, path = _make_repo()
        try:
            parent = _create_task(repo, "parent", "not_started")
            child = _create_task(repo, "child", "not_started", parent_id=parent.id)
            # Force parent to complete, child to validating then complete then remediating
            transition_task(repo, child.id, TaskStatus.WORKING)
            transition_task(repo, child.id, TaskStatus.VALIDATING)
            transition_task(repo, child.id, TaskStatus.COMPLETE)
            transition_task(repo, parent.id, TaskStatus.VALIDATING, force=True)
            transition_task(repo, parent.id, TaskStatus.COMPLETE, force=True)
            # Now remediate the child
            transition_task(repo, child.id, TaskStatus.REMEDIATING)
            p = repo.get_task(parent.id)
            assert p.status == TaskStatus.REMEDIATING
        finally:
            repo.close()
            os.unlink(path)

    def test_no_propagation_when_no_parent(self):
        repo, path = _make_repo()
        try:
            task = _create_task(repo, "root", "not_started")
            transition_task(repo, task.id, TaskStatus.WORKING)
            # No error, task updated
            t = repo.get_task(task.id)
            assert t.status == TaskStatus.WORKING
        finally:
            repo.close()
            os.unlink(path)


class TestHandleSessionStart:
    """Test auto-transition on session start."""

    def test_not_started_moves_to_working(self):
        repo, path = _make_repo()
        try:
            task = _create_task(repo, "t1", "not_started")
            result = handle_session_start(repo, task.id)
            assert result.status == TaskStatus.WORKING
        finally:
            repo.close()
            os.unlink(path)

    def test_remediating_moves_to_working(self):
        repo, path = _make_repo()
        try:
            task = _create_task(repo, "t2", "not_started")
            # Walk to remediating
            transition_task(repo, task.id, TaskStatus.WORKING)
            transition_task(repo, task.id, TaskStatus.VALIDATING)
            transition_task(repo, task.id, TaskStatus.REMEDIATING)
            result = handle_session_start(repo, task.id)
            assert result.status == TaskStatus.WORKING
        finally:
            repo.close()
            os.unlink(path)

    def test_already_working_is_noop(self):
        repo, path = _make_repo()
        try:
            task = _create_task(repo, "t3", "not_started")
            transition_task(repo, task.id, TaskStatus.WORKING)
            result = handle_session_start(repo, task.id)
            assert result.status == TaskStatus.WORKING
        finally:
            repo.close()
            os.unlink(path)

    def test_nonexistent_task_raises(self):
        repo, path = _make_repo()
        try:
            try:
                handle_session_start(repo, "no-such-id")
                assert False, "Should have raised ValueError"
            except ValueError:
                pass
        finally:
            repo.close()
            os.unlink(path)
