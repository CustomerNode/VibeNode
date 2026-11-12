"""
SQLite implementation of KanbanRepository.

Uses the stdlib ``sqlite3`` module (sync).  One database file at
``~/.claude/kanban.db`` holds data for every project, differentiated by
``project_id`` foreign keys.

Thread safety: each thread gets its own ``sqlite3.Connection`` via a
``threading.local()`` store.  WAL journal mode allows concurrent readers
while a single writer holds the lock.
"""

import sqlite3
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from .repository import (
    BoardColumn,
    KanbanRepository,
    Task,
    TaskIssue,
    TaskSession,
    TaskStatus,
    TaskTag,
)

# Gap size for position numbering.  New items get multiples of this;
# reorder computes a midpoint and renumbers the whole column on collision.
_POSITION_GAP = 1000

# Default columns created for every new project.
_DEFAULT_COLUMNS = [
    ("Not Started",  "not_started",  0, "#8b949e"),
    ("Working",      "working",      1, "#58a6ff"),
    ("Validating",   "validating",   2, "#d29922"),
    ("Remediating",  "remediating",  3, "#f85149"),
    ("Complete",     "complete",     4, "#3fb950"),
]


class SqliteRepository(KanbanRepository):
    """SQLite-backed Kanban repository."""

    def __init__(self, db_path=None):
        if db_path is None:
            db_path = Path.home() / ".claude" / "gui_kanban.db"
        self._db_path = Path(db_path)
        self._local = threading.local()
        self._schema_applied = False

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def initialize(self):
        """Ensure the database directory exists and apply schema."""
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        self._ensure_schema()

    def close(self):
        """Close the thread-local connection if open."""
        conn = getattr(self._local, "conn", None)
        if conn is not None:
            conn.close()
            self._local.conn = None

    def clear_all_data(self):
        """Delete all rows from every kanban table. Used before migration."""
        conn = self._get_conn()
        for table in [
            "task_status_history", "task_issues", "task_tags",
            "task_sessions", "tasks", "board_columns", "preferences",
        ]:
            conn.execute(f"DELETE FROM {table}")
        conn.commit()

    # ------------------------------------------------------------------
    # Connection helpers
    # ------------------------------------------------------------------

    def _get_conn(self):
        """Return a per-thread ``sqlite3.Connection``."""
        conn = getattr(self._local, "conn", None)
        if conn is None:
            conn = sqlite3.connect(str(self._db_path), timeout=10)
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA foreign_keys=ON")
            self._local.conn = conn
        return conn

    def _ensure_schema(self):
        """Run the initial migration if the schema_version table is absent
        or has no rows."""
        if self._schema_applied:
            return
        conn = self._get_conn()
        # Check whether schema_version exists
        cur = conn.execute(
            "SELECT name FROM sqlite_master "
            "WHERE type='table' AND name='schema_version'"
        )
        has_table = cur.fetchone() is not None

        if has_table:
            cur = conn.execute("SELECT MAX(version) FROM schema_version")
            row = cur.fetchone()
            if row and row[0] is not None:
                # Apply any newer migrations that may not have run yet
                self._apply_migration_002(conn)
                self._apply_migration_005(conn)
                self._apply_migration_006(conn)
                self._schema_applied = True
                return

        # Apply migration 001
        migration = Path(__file__).parent / "migrations" / "001_initial.sql"
        sql = migration.read_text(encoding="utf-8")
        conn.executescript(sql)
        # Record version
        now = datetime.now(timezone.utc).isoformat()
        conn.execute(
            "INSERT OR IGNORE INTO schema_version (version, applied_at) VALUES (?, ?)",
            (1, now),
        )
        conn.commit()

        # Apply migration 002 — task_tags (idempotent)
        self._apply_migration_002(conn)
        # Apply migration 005 — session_type column
        self._apply_migration_005(conn)
        # Apply migration 006 — session_id in status history
        self._apply_migration_006(conn)

        self._schema_applied = True

    def _apply_migration_002(self, conn):
        """Ensure task_tags has id + created_at columns.

        Migration 001 created task_tags with only (task_id, tag).
        This migration adds the missing columns if needed, or creates
        the full table if it doesn't exist yet.
        """
        cur = conn.execute(
            "SELECT name FROM sqlite_master "
            "WHERE type='table' AND name='task_tags'"
        )
        if cur.fetchone() is not None:
            # Table exists — check if it has the 'id' column
            info = conn.execute("PRAGMA table_info(task_tags)").fetchall()
            col_names = [row[1] if isinstance(row, tuple) else row["name"] for row in info]
            if "id" not in col_names:
                # Recreate with full schema (SQLite doesn't support ADD PRIMARY KEY)
                conn.executescript("""
                    ALTER TABLE task_tags RENAME TO _task_tags_old;
                    CREATE TABLE task_tags (
                        id TEXT PRIMARY KEY,
                        task_id TEXT NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
                        tag TEXT NOT NULL,
                        created_at TEXT NOT NULL,
                        UNIQUE(task_id, tag)
                    );
                    INSERT INTO task_tags (id, task_id, tag, created_at)
                        SELECT hex(randomblob(16)), task_id, tag, datetime('now')
                        FROM _task_tags_old;
                    DROP TABLE _task_tags_old;
                    CREATE INDEX IF NOT EXISTS idx_tags_task ON task_tags(task_id);
                    CREATE INDEX IF NOT EXISTS idx_tags_tag ON task_tags(tag);
                """)
            now = datetime.now(timezone.utc).isoformat()
            conn.execute(
                "INSERT OR IGNORE INTO schema_version (version, applied_at) VALUES (?, ?)",
                (2, now),
            )
            conn.commit()
            return
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS task_tags (
                id TEXT PRIMARY KEY,
                task_id TEXT NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
                tag TEXT NOT NULL,
                created_at TEXT NOT NULL,
                UNIQUE(task_id, tag)
            );
            CREATE INDEX IF NOT EXISTS idx_tags_task ON task_tags(task_id);
            CREATE INDEX IF NOT EXISTS idx_tags_tag ON task_tags(tag);
        """)
        now = datetime.now(timezone.utc).isoformat()
        conn.execute(
            "INSERT OR IGNORE INTO schema_version (version, applied_at) VALUES (?, ?)",
            (2, now),
        )
        conn.commit()

    def _apply_migration_005(self, conn):
        """Add session_type column to task_sessions (idempotent)."""
        info = conn.execute("PRAGMA table_info(task_sessions)").fetchall()
        col_names = [row[1] if isinstance(row, tuple) else row["name"] for row in info]
        if "session_type" not in col_names:
            conn.execute(
                "ALTER TABLE task_sessions ADD COLUMN session_type TEXT NOT NULL DEFAULT 'session'"
            )
            now = datetime.now(timezone.utc).isoformat()
            conn.execute(
                "INSERT OR IGNORE INTO schema_version (version, applied_at) VALUES (?, ?)",
                (5, now),
            )
            conn.commit()

    def _apply_migration_006(self, conn):
        """Add optional session_id column to task_status_history (idempotent)."""
        info = conn.execute("PRAGMA table_info(task_status_history)").fetchall()
        col_names = [row[1] if isinstance(row, tuple) else row["name"] for row in info]
        if "session_id" not in col_names:
            conn.execute(
                "ALTER TABLE task_status_history ADD COLUMN session_id TEXT"
            )
            now = datetime.now(timezone.utc).isoformat()
            conn.execute(
                "INSERT OR IGNORE INTO schema_version (version, applied_at) VALUES (?, ?)",
                (6, now),
            )
            conn.commit()

    # ------------------------------------------------------------------
    # Row ↔ dataclass helpers
    # ------------------------------------------------------------------

    def _row_to_task(self, row, depth=None):
        """Convert a sqlite3.Row to a Task dataclass, computing depth."""
        if depth is None:
            depth = 0
            parent_id = row["parent_id"]
            if parent_id:
                conn = self._get_conn()
                cur_pid = parent_id
                while cur_pid:
                    depth += 1
                    r = conn.execute(
                        "SELECT parent_id FROM tasks WHERE id = ?", (cur_pid,)
                    ).fetchone()
                    cur_pid = r["parent_id"] if r else None
        return Task(
            id=row["id"],
            project_id=row["project_id"],
            parent_id=row["parent_id"],
            title=row["title"],
            description=row["description"],
            verification_url=row["verification_url"],
            status=TaskStatus(row["status"]),
            position=row["position"],
            depth=depth,
            created_at=row["created_at"],
            updated_at=row["updated_at"],
            owner=row["owner"] if "owner" in row.keys() else None,
        )

    @staticmethod
    def _row_to_column(row):
        """Convert a sqlite3.Row to a BoardColumn dataclass."""
        return BoardColumn(
            id=row["id"],
            project_id=row["project_id"],
            name=row["name"],
            status_key=row["status_key"],
            position=row["position"],
            color=row["color"],
            sort_mode=row["sort_mode"],
            sort_direction=row["sort_direction"],
            is_terminal=bool(row["is_terminal"]) if "is_terminal" in row.keys() else False,
            is_regression=bool(row["is_regression"]) if "is_regression" in row.keys() else False,
        )

    @staticmethod
    def _row_to_issue(row):
        """Convert a sqlite3.Row to a TaskIssue dataclass."""
        return TaskIssue(
            id=row["id"],
            task_id=row["task_id"],
            description=row["description"],
            session_id=row["session_id"],
            resolved_at=row["resolved_at"],
            created_at=row["created_at"],
        )

    @staticmethod
    def _row_to_session(row):
        """Convert a sqlite3.Row to a TaskSession dataclass."""
        return TaskSession(
            task_id=row["task_id"],
            session_id=row["session_id"],
            created_at=row["created_at"],
        )

    # ------------------------------------------------------------------
    # Tasks
    # ------------------------------------------------------------------

    def create_task(self, task):
        """Insert a new task and return the persisted Task."""
        conn = self._get_conn()
        now = datetime.now(timezone.utc).isoformat()
        task_id = task.id or str(uuid.uuid4())
        position = task.position if task.position is not None else self.get_next_position(
            task.project_id, task.status.value
        )
        conn.execute(
            "INSERT INTO tasks "
            "(id, project_id, parent_id, title, description, verification_url, "
            " status, position, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                task_id,
                task.project_id,
                task.parent_id,
                task.title,
                task.description,
                task.verification_url,
                task.status.value,
                position,
                now,
                now,
            ),
        )
        # Record initial status in history
        conn.execute(
            "INSERT INTO task_status_history (id, task_id, old_status, new_status, changed_by, changed_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (str(uuid.uuid4()), task_id, None, task.status.value, None, now),
        )
        conn.commit()
        return self.get_task(task_id)

    def get_task(self, task_id):
        """Return a Task by id, or None if not found."""
        conn = self._get_conn()
        cur = conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,))
        row = cur.fetchone()
        if row is None:
            return None
        return self._row_to_task(row)

    def update_task(self, task_id, **fields):
        """Partial update of a task.  Returns the updated Task."""
        if not fields:
            return self.get_task(task_id)

        conn = self._get_conn()
        now = datetime.now(timezone.utc).isoformat()

        # If status changed, record in history and assign end-of-column position
        if "status" in fields:
            old_task = self.get_task(task_id)
            new_status = fields["status"]
            if isinstance(new_status, TaskStatus):
                new_status = new_status.value
            fields["status"] = new_status
            if old_task and old_task.status.value != new_status:
                # Assign a new position at the end of the target column
                # so it doesn't collide with existing tasks there.
                if "position" not in fields:
                    fields["position"] = self.get_next_position(
                        old_task.project_id, new_status
                    )
                conn.execute(
                    "INSERT INTO task_status_history "
                    "(id, task_id, old_status, new_status, changed_by, changed_at) "
                    "VALUES (?, ?, ?, ?, ?, ?)",
                    (str(uuid.uuid4()), task_id, old_task.status.value, new_status, None, now),
                )

        fields["updated_at"] = now
        set_clause = ", ".join(f"{k} = ?" for k in fields)
        values = list(fields.values()) + [task_id]
        conn.execute(f"UPDATE tasks SET {set_clause} WHERE id = ?", values)
        conn.commit()
        return self.get_task(task_id)

    def delete_task(self, task_id):
        """Delete a task and all descendants recursively."""
        conn = self._get_conn()
        # Recursively delete all descendants first (no FK cascade on parent_id)
        conn.execute("""
            WITH RECURSIVE desc AS (
                SELECT id FROM tasks WHERE parent_id = ?
                UNION ALL
                SELECT t.id FROM tasks t JOIN desc d ON t.parent_id = d.id
            )
            DELETE FROM tasks WHERE id IN (SELECT id FROM desc)
        """, (task_id,))
        conn.execute("DELETE FROM tasks WHERE id = ?", (task_id,))
        conn.commit()

    def get_children(self, parent_id):
        """Return immediate children ordered by position ASC."""
        conn = self._get_conn()
        cur = conn.execute(
            "SELECT * FROM tasks WHERE parent_id = ? ORDER BY position ASC",
            (parent_id,),
        )
        return [self._row_to_task(row) for row in cur.fetchall()]

    def get_children_counts_batch(self, task_ids):
        """Return {task_id: (child_count, complete_count)} for a list of task IDs."""
        if not task_ids:
            return {}
        conn = self._get_conn()
        placeholders = ','.join('?' * len(task_ids))
        result = {}
        for row in conn.execute(
            f"SELECT parent_id, COUNT(*) as cnt, "
            f"SUM(CASE WHEN status='complete' THEN 1 ELSE 0 END) as done "
            f"FROM tasks WHERE parent_id IN ({placeholders}) GROUP BY parent_id",
            task_ids,
        ):
            result[row['parent_id']] = (row['cnt'], row['done'])
        return result

    def get_session_counts_batch(self, task_ids):
        """Return {task_id: session_count} for a list of task IDs."""
        if not task_ids:
            return {}
        conn = self._get_conn()
        placeholders = ','.join('?' * len(task_ids))
        result = {}
        for row in conn.execute(
            f"SELECT task_id, COUNT(*) as cnt FROM task_sessions "
            f"WHERE task_id IN ({placeholders}) GROUP BY task_id",
            task_ids,
        ):
            result[row['task_id']] = row['cnt']
        return result

    def get_ancestors(self, task_id):
        """Walk up the parent chain using a recursive CTE.

        Returns list[Task] from immediate parent up to root.
        """
        conn = self._get_conn()
        sql = """
            WITH RECURSIVE ancestors AS (
                SELECT t.* FROM tasks t
                WHERE t.id = (SELECT parent_id FROM tasks WHERE id = ?)
              UNION ALL
                SELECT t.* FROM tasks t
                JOIN ancestors a ON t.id = a.parent_id
            )
            SELECT * FROM ancestors
        """
        cur = conn.execute(sql, (task_id,))
        return [self._row_to_task(row) for row in cur.fetchall()]

    def get_tasks_by_status(self, project_id, status):
        """Return all tasks in a project with the given status, ordered by
        position ASC."""
        conn = self._get_conn()
        status_val = status.value if isinstance(status, TaskStatus) else status
        cur = conn.execute(
            "SELECT * FROM tasks WHERE project_id = ? AND status = ? "
            "ORDER BY position ASC",
            (project_id, status_val),
        )
        return [self._row_to_task(row) for row in cur.fetchall()]

    # ------------------------------------------------------------------
    # Ordering
    # ------------------------------------------------------------------

    def reorder_task(self, task_id, after_id, before_id):
        """Place *task_id* between *after_id* and *before_id*.

        Uses gap-numbered integers.  If the midpoint collides (gap of 0),
        the entire column is renumbered with fresh gaps.
        """
        conn = self._get_conn()
        task = self.get_task(task_id)
        if task is None:
            return

        after_pos = 0
        before_pos = None

        if after_id:
            after_task = self.get_task(after_id)
            if after_task:
                after_pos = after_task.position

        if before_id:
            before_task = self.get_task(before_id)
            if before_task:
                before_pos = before_task.position

        if before_pos is None:
            # Append after the last item
            new_pos = after_pos + _POSITION_GAP
        else:
            new_pos = (after_pos + before_pos) // 2

        # Collision check: if new_pos equals either neighbour, renumber
        if new_pos == after_pos or (before_pos is not None and new_pos == before_pos):
            self._renumber_column(task.project_id, task.status.value)
            # Recalculate after renumber
            return self.reorder_task(task_id, after_id, before_id)

        now = datetime.now(timezone.utc).isoformat()
        conn.execute(
            "UPDATE tasks SET position = ?, updated_at = ? WHERE id = ?",
            (new_pos, now, task_id),
        )
        conn.commit()

    def _renumber_column(self, project_id, status_val):
        """Reassign positions for all tasks in a column with fresh gaps."""
        conn = self._get_conn()
        cur = conn.execute(
            "SELECT id FROM tasks WHERE project_id = ? AND status = ? "
            "ORDER BY position ASC",
            (project_id, status_val),
        )
        now = datetime.now(timezone.utc).isoformat()
        for idx, row in enumerate(cur.fetchall()):
            conn.execute(
                "UPDATE tasks SET position = ?, updated_at = ? WHERE id = ?",
                ((idx + 1) * _POSITION_GAP, now, row["id"]),
            )
        conn.commit()

    def get_next_position(self, project_id, status):
        """Return the next available position at the end of a column."""
        conn = self._get_conn()
        status_val = status.value if isinstance(status, TaskStatus) else status
        cur = conn.execute(
            "SELECT MAX(position) as max_pos FROM tasks "
            "WHERE project_id = ? AND status = ?",
            (project_id, status_val),
        )
        row = cur.fetchone()
        max_pos = row["max_pos"] if row and row["max_pos"] is not None else 0
        return max_pos + _POSITION_GAP

    def get_min_position(self, project_id, status):
        """Return the smallest position in a column (for top-insert)."""
        conn = self._get_conn()
        status_val = status.value if isinstance(status, TaskStatus) else status
        cur = conn.execute(
            "SELECT MIN(position) as min_pos FROM tasks "
            "WHERE project_id = ? AND status = ?",
            (project_id, status_val),
        )
        row = cur.fetchone()
        return row["min_pos"] if row and row["min_pos"] is not None else _POSITION_GAP

    # ------------------------------------------------------------------
    # Task ↔ Session links
    # ------------------------------------------------------------------

    def link_session(self, task_id, session_id, session_type='session'):
        """Associate a Claude session with a task.

        Args:
            task_id: The task to link to.
            session_id: The session UUID.
            session_type: 'session' (work session) or 'planner'.
        """
        conn = self._get_conn()
        now = datetime.now(timezone.utc).isoformat()
        conn.execute(
            "INSERT OR IGNORE INTO task_sessions (task_id, session_id, created_at, session_type) "
            "VALUES (?, ?, ?, ?)",
            (task_id, session_id, now, session_type),
        )
        conn.commit()
        return TaskSession(task_id=task_id, session_id=session_id, created_at=now, session_type=session_type)

    def unlink_session(self, task_id, session_id):
        """Remove the link between a session and a task."""
        conn = self._get_conn()
        conn.execute(
            "DELETE FROM task_sessions WHERE task_id = ? AND session_id = ?",
            (task_id, session_id),
        )
        conn.commit()

    def get_task_sessions(self, task_id, session_type=None):
        """Return list of TaskSession objects linked to a task.

        Args:
            task_id: The task to query.
            session_type: Optional filter — 'session', 'planner', or None for all.
        """
        conn = self._get_conn()
        if session_type:
            cur = conn.execute(
                "SELECT task_id, session_id, created_at, session_type "
                "FROM task_sessions WHERE task_id = ? AND session_type = ? "
                "ORDER BY created_at ASC",
                (task_id, session_type),
            )
        else:
            cur = conn.execute(
                "SELECT task_id, session_id, created_at, session_type "
                "FROM task_sessions WHERE task_id = ? "
                "ORDER BY created_at ASC",
                (task_id,),
            )
        rows = cur.fetchall()
        return [
            TaskSession(
                task_id=r["task_id"],
                session_id=r["session_id"],
                created_at=r["created_at"],
                session_type=r["session_type"] if "session_type" in r.keys() else "session",
            )
            for r in rows
        ]

    def get_session_task(self, session_id):
        """Return the task_id linked to a session, or None."""
        conn = self._get_conn()
        cur = conn.execute(
            "SELECT task_id FROM task_sessions WHERE session_id = ?",
            (session_id,),
        )
        row = cur.fetchone()
        return row["task_id"] if row else None

    def remap_session(self, old_id, new_id):
        """Update all task_sessions rows from old_id to new_id."""
        conn = self._get_conn()
        conn.execute(
            "UPDATE task_sessions SET session_id = ? WHERE session_id = ?",
            (new_id, old_id),
        )
        conn.commit()

    # ------------------------------------------------------------------
    # Validation Issues
    # ------------------------------------------------------------------

    def create_issue(self, task_id, description, session_id=None):
        """Log a new validation issue against a task."""
        conn = self._get_conn()
        issue_id = str(uuid.uuid4())
        now = datetime.now(timezone.utc).isoformat()
        conn.execute(
            "INSERT INTO task_issues (id, task_id, description, session_id, "
            "resolved_at, created_at) VALUES (?, ?, ?, ?, ?, ?)",
            (issue_id, task_id, description, session_id, None, now),
        )
        conn.commit()
        return TaskIssue(
            id=issue_id,
            task_id=task_id,
            description=description,
            session_id=session_id,
            resolved_at=None,
            created_at=now,
        )

    def resolve_issue(self, issue_id):
        """Mark an issue as resolved."""
        conn = self._get_conn()
        now = datetime.now(timezone.utc).isoformat()
        conn.execute(
            "UPDATE task_issues SET resolved_at = ? WHERE id = ?",
            (now, issue_id),
        )
        conn.commit()

    def get_open_issues(self, task_id):
        """Return unresolved issues for a task."""
        conn = self._get_conn()
        cur = conn.execute(
            "SELECT * FROM task_issues WHERE task_id = ? AND resolved_at IS NULL "
            "ORDER BY created_at ASC",
            (task_id,),
        )
        return [self._row_to_issue(row) for row in cur.fetchall()]

    def get_all_issues(self, task_id):
        """Return every issue (open and resolved) for a task."""
        conn = self._get_conn()
        cur = conn.execute(
            "SELECT * FROM task_issues WHERE task_id = ? ORDER BY created_at ASC",
            (task_id,),
        )
        return [self._row_to_issue(row) for row in cur.fetchall()]

    # ------------------------------------------------------------------
    # Tags
    # ------------------------------------------------------------------

    @staticmethod
    def _row_to_tag(row):
        """Convert a sqlite3.Row to a TaskTag dataclass."""
        return TaskTag(
            id=row["id"],
            task_id=row["task_id"],
            tag=row["tag"],
            created_at=row["created_at"],
        )

    def add_tag(self, task_id, tag):
        """Add a tag to a task.  Returns TaskTag."""
        conn = self._get_conn()
        tag_id = str(uuid.uuid4())
        now = datetime.now(timezone.utc).isoformat()
        conn.execute(
            "INSERT OR IGNORE INTO task_tags (id, task_id, tag, created_at) "
            "VALUES (?, ?, ?, ?)",
            (tag_id, task_id, tag, now),
        )
        conn.commit()
        # Return the existing or newly created tag
        cur = conn.execute(
            "SELECT * FROM task_tags WHERE task_id = ? AND tag = ?",
            (task_id, tag),
        )
        return self._row_to_tag(cur.fetchone())

    def remove_tag(self, task_id, tag):
        """Remove a tag from a task."""
        conn = self._get_conn()
        conn.execute(
            "DELETE FROM task_tags WHERE task_id = ? AND tag = ?",
            (task_id, tag),
        )
        conn.commit()

    def get_task_tags(self, task_id):
        """Return list of TaskTag records for a task."""
        conn = self._get_conn()
        cur = conn.execute(
            "SELECT * FROM task_tags WHERE task_id = ? ORDER BY tag ASC",
            (task_id,),
        )
        return [self._row_to_tag(row) for row in cur.fetchall()]

    def get_tasks_by_tag(self, project_id, tag):
        """Return all tasks in a project that carry a given tag."""
        conn = self._get_conn()
        cur = conn.execute(
            "SELECT t.* FROM tasks t "
            "JOIN task_tags tt ON t.id = tt.task_id "
            "WHERE t.project_id = ? AND tt.tag = ? "
            "ORDER BY t.position ASC",
            (project_id, tag),
        )
        return [self._row_to_task(row) for row in cur.fetchall()]

    def get_all_tags(self, project_id):
        """Return all distinct tag strings used in a project."""
        conn = self._get_conn()
        cur = conn.execute(
            "SELECT DISTINCT tt.tag FROM task_tags tt "
            "JOIN tasks t ON tt.task_id = t.id "
            "WHERE t.project_id = ? "
            "ORDER BY tt.tag ASC",
            (project_id,),
        )
        return [row["tag"] for row in cur.fetchall()]

    # ------------------------------------------------------------------
    # Raw SQL (for reports)
    # ------------------------------------------------------------------

    def execute_sql(self, sql, params=()):
        """Execute an arbitrary read-only SQL query and return rows as
        list[dict]."""
        conn = self._get_conn()
        cur = conn.execute(sql, params)
        columns = [desc[0] for desc in cur.description] if cur.description else []
        return [dict(zip(columns, row)) for row in cur.fetchall()]

    # ------------------------------------------------------------------
    # Columns / Board Config
    # ------------------------------------------------------------------

    def get_columns(self, project_id):
        """Return BoardColumn list for a project, ordered by position."""
        conn = self._get_conn()
        cur = conn.execute(
            "SELECT * FROM board_columns WHERE project_id = ? ORDER BY position ASC",
            (project_id,),
        )
        rows = cur.fetchall()
        if not rows:
            self._create_default_columns(project_id)
            cur = conn.execute(
                "SELECT * FROM board_columns WHERE project_id = ? ORDER BY position ASC",
                (project_id,),
            )
            rows = cur.fetchall()
        return [self._row_to_column(row) for row in rows]

    def create_column(self, project_id, name, status_key, position, color,
                      sort_mode='manual', sort_direction='desc'):
        """Insert a single column for a project."""
        conn = self._get_conn()
        col_id = str(uuid.uuid4())
        conn.execute(
            "INSERT INTO board_columns "
            "(id, project_id, name, status_key, position, color, "
            " sort_mode, sort_direction) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (col_id, project_id, name, status_key, position, color,
             sort_mode, sort_direction),
        )
        conn.commit()
        return self._row_to_column(conn.execute(
            "SELECT * FROM board_columns WHERE id = ?", (col_id,)
        ).fetchone())

    def upsert_columns(self, project_id, columns):
        """Replace the column configuration for a project."""
        conn = self._get_conn()
        conn.execute(
            "DELETE FROM board_columns WHERE project_id = ?", (project_id,)
        )
        for col in columns:
            conn.execute(
                "INSERT INTO board_columns "
                "(id, project_id, name, status_key, position, color, "
                " sort_mode, sort_direction) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    col.id or str(uuid.uuid4()),
                    project_id,
                    col.name,
                    col.status_key,
                    col.position,
                    col.color,
                    col.sort_mode,
                    col.sort_direction,
                ),
            )
        conn.commit()

    def update_columns(self, project_id, columns_data):
        """Update columns from a list of dicts (API-facing alias)."""
        conn = self._get_conn()
        conn.execute(
            "DELETE FROM board_columns WHERE project_id = ?", (project_id,)
        )
        for col in columns_data:
            conn.execute(
                "INSERT INTO board_columns "
                "(id, project_id, name, status_key, position, color, "
                " sort_mode, sort_direction) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    col.get('id', str(uuid.uuid4())),
                    project_id,
                    col['name'],
                    col['status_key'],
                    col.get('position', 0),
                    col.get('color', '#8b949e'),
                    col.get('sort_mode', 'manual'),
                    col.get('sort_direction', 'desc'),
                ),
            )
        conn.commit()
        return self.get_columns(project_id)

    def add_status_history(self, task_id, old_status, new_status, changed_by=None,
                           changed_at=None, session_id=None):
        """Record a status transition in the history table.

        Args:
            session_id: Optional session ID that triggered this transition.
        """
        conn = self._get_conn()
        if changed_at is None:
            changed_at = datetime.now(timezone.utc).isoformat()
        conn.execute(
            "INSERT INTO task_status_history "
            "(id, task_id, old_status, new_status, changed_by, changed_at, session_id) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (str(uuid.uuid4()), task_id, old_status, new_status, changed_by,
             changed_at, session_id),
        )
        conn.commit()

    def get_status_history(self, task_id):
        """Return status history for a task, newest first."""
        conn = self._get_conn()
        cur = conn.execute(
            "SELECT id, task_id, old_status, new_status, changed_by, changed_at, session_id "
            "FROM task_status_history WHERE task_id = ? ORDER BY changed_at DESC",
            (task_id,),
        )
        return [dict(row) for row in cur.fetchall()]

    def get_all_status_history(self):
        """Return all status history rows (for migration export)."""
        conn = self._get_conn()
        cur = conn.execute(
            "SELECT id, task_id, old_status, new_status, changed_by, changed_at, session_id "
            "FROM task_status_history ORDER BY changed_at"
        )
        return [dict(row) for row in cur.fetchall()]

    # ------------------------------------------------------------------
    # Preferences
    # ------------------------------------------------------------------

    def get_preference(self, key):
        """Return the value for a preference key, or None."""
        conn = self._get_conn()
        cur = conn.execute("SELECT value FROM preferences WHERE key = ?", (key,))
        row = cur.fetchone()
        return row["value"] if row else None

    def set_preference(self, key, value):
        """Set a preference key to a value."""
        conn = self._get_conn()
        now = datetime.now(timezone.utc).isoformat()
        conn.execute(
            "INSERT OR REPLACE INTO preferences (key, value, updated_at) VALUES (?, ?, ?)",
            (key, str(value), now),
        )
        conn.commit()

    def get_all_preferences(self):
        """Return all preferences as a list of dicts."""
        conn = self._get_conn()
        cur = conn.execute("SELECT key, value, updated_at FROM preferences ORDER BY key")
        return [dict(row) for row in cur.fetchall()]

    def _create_default_columns(self, project_id):
        """Insert the five default columns for a new project."""
        conn = self._get_conn()
        for name, status_key, position, color in _DEFAULT_COLUMNS:
            conn.execute(
                "INSERT INTO board_columns "
                "(id, project_id, name, status_key, position, color, "
                " sort_mode, sort_direction) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    str(uuid.uuid4()),
                    project_id,
                    name,
                    status_key,
                    position,
                    color,
                    "manual",
                    "desc",
                ),
            )
        conn.commit()

    # ------------------------------------------------------------------
    # Full Board
    # ------------------------------------------------------------------

    def get_board(self, project_id):
        """Return the complete board state as a dict.

        Returns::

            {
                "columns": [BoardColumn, ...],
                "tasks":   { status_key: [Task, ...], ... }
            }

        Columns are auto-created for new projects.
        """
        columns = self.get_columns(project_id)

        conn = self._get_conn()
        cur = conn.execute(
            "SELECT * FROM tasks WHERE project_id = ? ORDER BY position ASC",
            (project_id,),
        )
        all_tasks = [self._row_to_task(row) for row in cur.fetchall()]

        # Group tasks by status key
        tasks_by_status = {}
        for col in columns:
            tasks_by_status[col.status_key] = []
        for task in all_tasks:
            key = task.status.value
            if key not in tasks_by_status:
                tasks_by_status[key] = []
            tasks_by_status[key].append(task)

        return {
            "columns": columns,
            "tasks": tasks_by_status,
        }

    # ------------------------------------------------------------------
    # Migrator helpers (used by BackendMigrator.export_all / import_all)
    # ------------------------------------------------------------------

    def get_all_columns_all_projects(self):
        """Return every board_columns row across all projects (for migration)."""
        conn = self._get_conn()
        cur = conn.execute(
            "SELECT id, project_id, name, status_key, position, color, "
            "sort_mode, sort_direction FROM board_columns ORDER BY project_id, position"
        )
        return [dict(row) for row in cur.fetchall()]

    def get_all_tasks_ordered(self):
        """Return every task row, parents before children (for migration).

        Uses a recursive CTE to get topological order so FK constraints
        resolve correctly during import.
        """
        conn = self._get_conn()
        sql = """
            WITH RECURSIVE ordered_tasks AS (
                SELECT *, 0 AS _depth FROM tasks WHERE parent_id IS NULL
              UNION ALL
                SELECT t.*, ot._depth + 1 FROM tasks t
                JOIN ordered_tasks ot ON t.parent_id = ot.id
            )
            SELECT * FROM ordered_tasks ORDER BY _depth ASC, position ASC
        """
        cur = conn.execute(sql)
        rows = cur.fetchall()
        result = []
        for row in rows:
            task = self._row_to_task(row, depth=row["_depth"])
            result.append(task.to_dict())
        return result

    def get_all_task_sessions(self):
        """Return every task_sessions row (for migration)."""
        conn = self._get_conn()
        cur = conn.execute(
            "SELECT task_id, session_id, created_at, session_type FROM task_sessions"
        )
        return [dict(row) for row in cur.fetchall()]

    def get_all_task_tags(self):
        """Return every task_tags row (for migration)."""
        conn = self._get_conn()
        cur = conn.execute(
            "SELECT id, task_id, tag, created_at FROM task_tags"
        )
        return [dict(row) for row in cur.fetchall()]

    def create_task_from_dict(self, d):
        """Insert a task from a migration dict (preserving original id).

        Note: depth is computed dynamically by _row_to_task, not stored in DB.
        """
        conn = self._get_conn()
        status = d.get("status", "not_started")
        if isinstance(status, TaskStatus):
            status = status.value
        conn.execute(
            "INSERT OR IGNORE INTO tasks "
            "(id, project_id, parent_id, title, description, verification_url, "
            " status, position, owner, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                d["id"], d.get("project_id", ""), d.get("parent_id"),
                d.get("title", ""), d.get("description"),
                d.get("verification_url"),
                status, d.get("position", 0),
                d.get("owner"), d.get("created_at", ""),
                d.get("updated_at", ""),
            ),
        )
        conn.commit()

    def create_column(self, col):
        """Insert a board column from a migration dict."""
        conn = self._get_conn()
        conn.execute(
            "INSERT OR IGNORE INTO board_columns "
            "(id, project_id, name, status_key, position, color, "
            " sort_mode, sort_direction) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                col.get("id", str(uuid.uuid4())),
                col.get("project_id", ""),
                col.get("name", ""),
                col.get("status_key", ""),
                col.get("position", 0),
                col.get("color", "#888"),
                col.get("sort_mode", "manual"),
                col.get("sort_direction", "asc"),
            ),
        )
        conn.commit()
