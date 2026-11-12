"""Tests for app.db.migrator — lossless backend migration round-trips.

Uses two independent SQLite repos (source and target) to verify that
export_all + import_all + switch_backend preserve every record.
"""

import os
import tempfile
import uuid
from datetime import datetime, timezone

from web.db.repository import Task, TaskStatus
from web.db.sqlite_backend import SqliteRepository
from web.db.migrator import BackendMigrator, MigrationError


PROJECT_ID = "migrate-test-project"


def _make_repo():
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    repo = SqliteRepository(db_path=path)
    repo.initialize()
    return repo, path


def _now():
    return datetime.now(timezone.utc).isoformat()


def _create_task(repo, title, status="not_started", parent_id=None):
    pos = repo.get_next_position(PROJECT_ID, status)
    t = Task(
        id=str(uuid.uuid4()),
        project_id=PROJECT_ID,
        parent_id=parent_id,
        title=title,
        description="desc",
        verification_url=None,
        status=TaskStatus(status),
        position=pos,
        depth=0,
        created_at=_now(),
        updated_at=_now(),
    )
    return repo.create_task(t)


def _seed_source(repo):
    """Populate a repo with a variety of data for migration testing."""
    # Auto-create columns
    repo.get_board(PROJECT_ID)

    # Tasks with hierarchy
    root = _create_task(repo, "Root Task")
    child1 = _create_task(repo, "Child 1", parent_id=root.id)
    child2 = _create_task(repo, "Child 2", "working", parent_id=root.id)

    # Tags
    repo.add_tag(root.id, "backend")
    repo.add_tag(child1.id, "frontend")

    # Sessions
    repo.link_session(root.id, "session-001")
    repo.link_session(child2.id, "session-002")

    # Status history
    repo.add_status_history(child2.id, "not_started", "working", _now())

    # Preferences
    repo.set_preference("kanban_auto_advance", "false")

    return root, child1, child2


class TestExportAll:

    def test_export_returns_all_tables(self):
        repo, path = _make_repo()
        try:
            _seed_source(repo)
            migrator = BackendMigrator()
            data = migrator.export_all(repo)
            assert "preferences" in data
            assert "board_columns" in data
            assert "tasks" in data
            assert "task_sessions" in data
            assert "task_tags" in data
            assert "status_history" in data
        finally:
            repo.close()
            os.unlink(path)

    def test_export_captures_task_count(self):
        repo, path = _make_repo()
        try:
            _seed_source(repo)
            migrator = BackendMigrator()
            data = migrator.export_all(repo)
            assert len(data["tasks"]) == 3
        finally:
            repo.close()
            os.unlink(path)

    def test_export_captures_tags(self):
        repo, path = _make_repo()
        try:
            _seed_source(repo)
            migrator = BackendMigrator()
            data = migrator.export_all(repo)
            assert len(data["task_tags"]) == 2
        finally:
            repo.close()
            os.unlink(path)

    def test_export_captures_sessions(self):
        repo, path = _make_repo()
        try:
            _seed_source(repo)
            migrator = BackendMigrator()
            data = migrator.export_all(repo)
            assert len(data["task_sessions"]) == 2
        finally:
            repo.close()
            os.unlink(path)


class TestImportAll:

    def test_import_into_fresh_repo(self):
        source, s_path = _make_repo()
        target, t_path = _make_repo()
        try:
            _seed_source(source)
            migrator = BackendMigrator()
            data = migrator.export_all(source)
            migrator.import_all(target, data)

            # Verify tasks exist in target
            for task_dict in data["tasks"]:
                t = target.get_task(task_dict["id"])
                assert t is not None
                assert t.title == task_dict["title"]
        finally:
            source.close()
            target.close()
            os.unlink(s_path)
            os.unlink(t_path)

    def test_import_preserves_hierarchy(self):
        source, s_path = _make_repo()
        target, t_path = _make_repo()
        try:
            _seed_source(source)
            migrator = BackendMigrator()
            data = migrator.export_all(source)
            migrator.import_all(target, data)

            # Find tasks with parent_id set
            children = [t for t in data["tasks"] if t.get("parent_id")]
            for child_dict in children:
                t = target.get_task(child_dict["id"])
                assert t.parent_id == child_dict["parent_id"]
        finally:
            source.close()
            target.close()
            os.unlink(s_path)
            os.unlink(t_path)


class TestSwitchBackend:

    def test_successful_migration_returns_true(self):
        source, s_path = _make_repo()
        target, t_path = _make_repo()
        try:
            _seed_source(source)
            migrator = BackendMigrator()
            result = migrator.switch_backend(source, target)
            assert result is True
        finally:
            source.close()
            target.close()
            os.unlink(s_path)
            os.unlink(t_path)

    def test_migration_preserves_record_count(self):
        source, s_path = _make_repo()
        target, t_path = _make_repo()
        try:
            _seed_source(source)
            migrator = BackendMigrator()
            source_data = migrator.export_all(source)
            source_count = sum(
                len(v) for v in source_data.values() if isinstance(v, list)
            )

            migrator.switch_backend(source, target)

            target_data = migrator.export_all(target)
            target_count = sum(
                len(v) for v in target_data.values() if isinstance(v, list)
            )
            assert target_count == source_count
        finally:
            source.close()
            target.close()
            os.unlink(s_path)
            os.unlink(t_path)

    def test_source_data_preserved_after_migration(self):
        """Old backend data is NEVER deleted — plan Section 16."""
        source, s_path = _make_repo()
        target, t_path = _make_repo()
        try:
            root, _, _ = _seed_source(source)
            migrator = BackendMigrator()
            migrator.switch_backend(source, target)
            # Source still has data
            assert source.get_task(root.id) is not None
        finally:
            source.close()
            target.close()
            os.unlink(s_path)
            os.unlink(t_path)

    def test_empty_source_migrates_cleanly(self):
        source, s_path = _make_repo()
        target, t_path = _make_repo()
        try:
            migrator = BackendMigrator()
            result = migrator.switch_backend(source, target)
            assert result is True
        finally:
            source.close()
            target.close()
            os.unlink(s_path)
            os.unlink(t_path)


class TestRoundTrip:

    def test_double_migration_preserves_data(self):
        """Migrate A -> B -> C and verify C matches A."""
        repo_a, a_path = _make_repo()
        repo_b, b_path = _make_repo()
        repo_c, c_path = _make_repo()
        try:
            _seed_source(repo_a)
            migrator = BackendMigrator()

            # A -> B
            migrator.switch_backend(repo_a, repo_b)
            # B -> C
            migrator.switch_backend(repo_b, repo_c)

            # Compare A and C
            data_a = migrator.export_all(repo_a)
            data_c = migrator.export_all(repo_c)

            count_a = sum(len(v) for v in data_a.values() if isinstance(v, list))
            count_c = sum(len(v) for v in data_c.values() if isinstance(v, list))
            assert count_c == count_a

            # Verify individual tasks match
            tasks_a = {t["id"]: t["title"] for t in data_a["tasks"]}
            tasks_c = {t["id"]: t["title"] for t in data_c["tasks"]}
            assert tasks_a == tasks_c
        finally:
            repo_a.close()
            repo_b.close()
            repo_c.close()
            os.unlink(a_path)
            os.unlink(b_path)
            os.unlink(c_path)
