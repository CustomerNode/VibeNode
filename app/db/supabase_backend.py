"""
Supabase (PostgREST) implementation of KanbanRepository.

Uses the ``supabase-py`` SDK to communicate with a hosted Supabase project
over HTTPS.  The exact same table layout as the SQLite backend is expected
to exist in the remote Postgres database — create it via the Supabase
dashboard or a migration script before first use.

All methods are synchronous (the supabase-py ``create_client`` helper
returns a sync ``Client``), matching the threading model used by the
Flask app.

Performance notes
-----------------
Every method call is an HTTPS round-trip (~50-150ms).  Avoid calling
repo methods in loops.  Key optimizations:

- **_row_to_task** does NOT compute depth.  Depth was previously
  calculated by walking the parent chain with one query per ancestor —
  for 30 tasks that meant 60-80 extra HTTP calls.  Now depth is computed
  in Python via _compute_depths() from a flat list of rows.  get_board()
  uses this automatically.  Callers that need depth on single tasks
  (get_task) get depth=0; the API layer recomputes from the full list.

- **get_session_counts_batch** and **get_children_counts_batch** exist
  so the API layer can fetch counts for all tasks in one query instead
  of calling get_task_sessions / get_children per task.

Never add per-row Supabase queries back into _row_to_task.
"""

import uuid
from datetime import datetime, timezone
from typing import Optional

try:
    from supabase import create_client, Client
except ImportError:
    raise ImportError(
        "The 'supabase' package is required for the Supabase backend. "
        "Install it with:  pip install supabase"
    )

from .repository import (
    BoardColumn,
    KanbanRepository,
    Task,
    TaskIssue,
    TaskSession,
    TaskStatus,
    TaskTag,
)

# Gap size for position numbering — must match the SQLite backend.
_POSITION_GAP = 1000

# Default columns created for every new project.
_DEFAULT_COLUMNS = [
    ("Not Started",  "not_started",  0, "#8b949e"),
    ("Working",      "working",      1, "#58a6ff"),
    ("Validating",   "validating",   2, "#d29922"),
    ("Remediating",  "remediating",  3, "#f85149"),
    ("Complete",     "complete",     4, "#3fb950"),
]


class SupabaseRepository(KanbanRepository):
    """Supabase-backed Kanban repository.

    Parameters
    ----------
    url : str
        The Supabase project URL (e.g. ``https://xyz.supabase.co``).
    key : str
        The **service-role / secret** key (``sb_secret_...``).  This key
        bypasses Row Level Security so the server can access all rows.
    """

    def __init__(self, url: str, key: str):
        self._url = url
        self._key = key
        self.client: Optional[Client] = None

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    # Individual SQL statements to bootstrap the kanban schema.
    # Kept as a list so each can be executed independently (no `;` splitting).
    _BOOTSTRAP_STATEMENTS = [
        """CREATE TABLE IF NOT EXISTS schema_version (
            version INTEGER PRIMARY KEY,
            applied_at TIMESTAMPTZ NOT NULL DEFAULT now()
        )""",
        """CREATE TABLE IF NOT EXISTS preferences (
            key        TEXT PRIMARY KEY,
            value      TEXT NOT NULL,
            updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
        )""",
        """INSERT INTO preferences (key, value) VALUES
            ('kanban_backend',      'supabase'),
            ('kanban_auto_advance', 'false'),
            ('kanban_page_size',    '50')
        ON CONFLICT (key) DO NOTHING""",
        """CREATE TABLE IF NOT EXISTS board_columns (
            id TEXT PRIMARY KEY,
            project_id TEXT NOT NULL,
            name TEXT NOT NULL,
            status_key TEXT NOT NULL,
            position INTEGER NOT NULL DEFAULT 0,
            color TEXT DEFAULT '#8b949e',
            is_terminal BOOLEAN DEFAULT FALSE,
            is_regression BOOLEAN DEFAULT FALSE,
            sort_mode TEXT DEFAULT 'manual',
            sort_direction TEXT DEFAULT 'desc',
            UNIQUE(project_id, status_key)
        )""",
        """CREATE TABLE IF NOT EXISTS tasks (
            id TEXT PRIMARY KEY,
            project_id TEXT NOT NULL,
            parent_id TEXT REFERENCES tasks(id) ON DELETE CASCADE,
            position INTEGER NOT NULL DEFAULT 0,
            title TEXT NOT NULL,
            description TEXT,
            verification_url TEXT,
            status TEXT NOT NULL DEFAULT 'not_started',
            owner TEXT,
            created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
        )""",
        "CREATE INDEX IF NOT EXISTS idx_tasks_project ON tasks(project_id)",
        "CREATE INDEX IF NOT EXISTS idx_tasks_parent ON tasks(parent_id)",
        "CREATE INDEX IF NOT EXISTS idx_tasks_status ON tasks(project_id, status)",
        "CREATE INDEX IF NOT EXISTS idx_tasks_position ON tasks(project_id, status, position)",
        """CREATE TABLE IF NOT EXISTS task_sessions (
            task_id TEXT NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
            session_id TEXT NOT NULL,
            created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            PRIMARY KEY (task_id, session_id)
        )""",
        """CREATE TABLE IF NOT EXISTS task_issues (
            id TEXT PRIMARY KEY,
            task_id TEXT NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
            description TEXT NOT NULL,
            session_id TEXT,
            resolved_at TIMESTAMPTZ,
            created_at TIMESTAMPTZ NOT NULL DEFAULT now()
        )""",
        "CREATE INDEX IF NOT EXISTS idx_issues_task ON task_issues(task_id)",
        """CREATE TABLE IF NOT EXISTS task_status_history (
            id TEXT PRIMARY KEY,
            task_id TEXT NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
            old_status TEXT,
            new_status TEXT NOT NULL,
            changed_by TEXT,
            changed_at TIMESTAMPTZ NOT NULL DEFAULT now()
        )""",
        "CREATE INDEX IF NOT EXISTS idx_status_hist_task ON task_status_history(task_id)",
        """CREATE TABLE IF NOT EXISTS task_tags (
            id TEXT PRIMARY KEY DEFAULT gen_random_uuid()::text,
            task_id TEXT NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
            tag TEXT NOT NULL,
            created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            UNIQUE(task_id, tag)
        )""",
        "CREATE INDEX IF NOT EXISTS idx_task_tags_tag ON task_tags(tag)",
        "INSERT INTO schema_version (version) VALUES (1) ON CONFLICT (version) DO NOTHING",
        # Enable Row Level Security on all tables.
        # The server uses the service-role key which bypasses RLS, so this
        # only blocks unauthorized access via the public/anon API.
        "ALTER TABLE schema_version      ENABLE ROW LEVEL SECURITY",
        "ALTER TABLE preferences          ENABLE ROW LEVEL SECURITY",
        "ALTER TABLE board_columns        ENABLE ROW LEVEL SECURITY",
        "ALTER TABLE tasks                ENABLE ROW LEVEL SECURITY",
        "ALTER TABLE task_sessions        ENABLE ROW LEVEL SECURITY",
        "ALTER TABLE task_issues          ENABLE ROW LEVEL SECURITY",
        "ALTER TABLE task_status_history  ENABLE ROW LEVEL SECURITY",
        "ALTER TABLE task_tags            ENABLE ROW LEVEL SECURITY",
        # RPC: recursive ancestor lookup (contains $$ so must be a single statement)
        """CREATE OR REPLACE FUNCTION get_ancestors(task_id_param TEXT)
        RETURNS SETOF tasks AS $$
          WITH RECURSIVE ancestors AS (
            SELECT t.*
            FROM tasks t
            WHERE t.id = (SELECT parent_id FROM tasks WHERE id = task_id_param)
            UNION ALL
            SELECT t.*
            FROM tasks t
            INNER JOIN ancestors a ON t.id = a.parent_id
          )
          SELECT * FROM ancestors;
        $$ LANGUAGE SQL STABLE""",
    ]

    @classmethod
    def get_setup_sql(cls):
        """Return the full SQL needed to set up the kanban schema."""
        return ";\n\n".join(cls._BOOTSTRAP_STATEMENTS) + ";"

    @classmethod
    def provision_schema(cls, project_url: str, access_token: str):
        """Create all kanban tables via the Supabase Management API.

        Parameters
        ----------
        project_url : str
            The Supabase project URL (e.g. ``https://xyz.supabase.co``).
        access_token : str
            A personal access token from
            https://supabase.com/dashboard/account/tokens
        """
        import re
        import httpx

        m = re.search(r"https://([^.]+)\.supabase\.co", project_url)
        if not m:
            raise ConnectionError(f"Could not parse project ref from: {project_url}")
        project_ref = m.group(1)

        sql = cls.get_setup_sql()
        resp = httpx.post(
            f"https://api.supabase.com/v1/projects/{project_ref}/database/query",
            json={"query": sql},
            headers={
                "Authorization": f"Bearer {access_token}",
            },
            timeout=30.0,
        )
        if resp.status_code == 401:
            raise ConnectionError(
                "Invalid access token. Generate one at "
                "supabase.com/dashboard/account/tokens"
            )
        if resp.status_code >= 400:
            raise ConnectionError(
                f"Schema setup failed ({resp.status_code}): {resp.text[:300]}"
            )

    def initialize(self):
        """Create the Supabase client and verify the schema exists.

        Raises ``SchemaNotReady`` (a ``ConnectionError`` subclass) if the
        tables haven't been created yet — the caller should surface the
        setup SQL to the user.
        """
        self.client = create_client(self._url, self._key)

        # Smoke-test: try to read from the schema_version table.
        try:
            self.client.table("schema_version").select("version").limit(1).execute()
        except Exception:
            raise SchemaNotReady(self._url)

    def close(self):
        """Release the client reference.  The HTTP session is not
        persistent so there is nothing to truly close."""
        self.client = None

    def clear_all_data(self):
        """Delete all rows from every kanban table. Used before migration
        import to ensure a clean slate."""
        # Order matters — delete children before parents (FK constraints)
        for table in [
            "task_status_history", "task_issues", "task_tags",
            "task_sessions", "tasks", "board_columns", "preferences",
        ]:
            try:
                # PostgREST requires a filter for DELETE — use neq on a
                # column that always exists to match all rows
                self.client.table(table).delete().neq("id" if table != "preferences" else "key", "___impossible___").execute()
            except Exception:
                pass

    # ------------------------------------------------------------------
    # Row → dataclass helpers
    # ------------------------------------------------------------------

    def _row_to_task(self, row: dict, depth: int = 0) -> Task:
        return Task(
            id=row["id"],
            project_id=row["project_id"],
            parent_id=row.get("parent_id"),
            title=row["title"],
            description=row.get("description"),
            verification_url=row.get("verification_url"),
            status=TaskStatus(row["status"]),
            position=row["position"],
            depth=depth,
            created_at=row["created_at"],
            updated_at=row["updated_at"],
            owner=row.get("owner"),
        )

    @staticmethod
    def _compute_depths(rows: list) -> dict:
        """Compute depth for each task from a flat list of rows in Python.

        Replaces the old per-row recursive Supabase queries.
        """
        parent_map = {r["id"]: r.get("parent_id") for r in rows}
        cache = {}

        def _depth(tid):
            if tid in cache:
                return cache[tid]
            pid = parent_map.get(tid)
            d = 0 if not pid else _depth(pid) + 1
            cache[tid] = d
            return d

        return {r["id"]: _depth(r["id"]) for r in rows}

    @staticmethod
    def _row_to_column(row: dict) -> BoardColumn:
        return BoardColumn(
            id=row["id"],
            project_id=row["project_id"],
            name=row["name"],
            status_key=row["status_key"],
            position=row["position"],
            color=row.get("color", "#8b949e"),
            sort_mode=row.get("sort_mode", "manual"),
            sort_direction=row.get("sort_direction", "desc"),
        )

    @staticmethod
    def _row_to_issue(row: dict) -> TaskIssue:
        return TaskIssue(
            id=row["id"],
            task_id=row["task_id"],
            description=row["description"],
            session_id=row.get("session_id"),
            resolved_at=row.get("resolved_at"),
            created_at=row["created_at"],
        )

    @staticmethod
    def _row_to_session(row: dict) -> TaskSession:
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
        now = datetime.now(timezone.utc).isoformat()
        task_id = task.id or str(uuid.uuid4())
        position = task.position if task.position is not None else self.get_next_position(
            task.project_id, task.status.value
        )

        row = {
            "id": task_id,
            "project_id": task.project_id,
            "parent_id": task.parent_id,
            "title": task.title,
            "description": task.description,
            "verification_url": task.verification_url,
            "status": task.status.value,
            "position": position,
            "created_at": now,
            "updated_at": now,
        }
        self.client.table("tasks").insert(row).execute()

        # Record initial status in history
        self.client.table("task_status_history").insert({
            "id": str(uuid.uuid4()),
            "task_id": task_id,
            "old_status": None,
            "new_status": task.status.value,
            "changed_at": now,
        }).execute()

        return self.get_task(task_id)

    def get_task(self, task_id):
        """Return a Task by id, or None if not found."""
        result = (
            self.client.table("tasks")
            .select("*")
            .eq("id", task_id)
            .execute()
        )
        if not result.data:
            return None
        return self._row_to_task(result.data[0])

    def update_task(self, task_id, **fields):
        """Partial update — only the supplied keyword fields are changed.
        Returns the updated Task."""
        if not fields:
            return self.get_task(task_id)

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
                self.client.table("task_status_history").insert({
                    "id": str(uuid.uuid4()),
                    "task_id": task_id,
                    "old_status": old_task.status.value,
                    "new_status": new_status,
                    "changed_at": now,
                }).execute()

        fields["updated_at"] = now
        self.client.table("tasks").update(fields).eq("id", task_id).execute()
        return self.get_task(task_id)

    def delete_task(self, task_id):
        """Delete a task and all its descendants recursively."""
        # Collect all descendant IDs first
        all_ids = []
        queue = [task_id]
        while queue:
            current = queue.pop(0)
            all_ids.append(current)
            children = (
                self.client.table("tasks")
                .select("id")
                .eq("parent_id", current)
                .execute()
            )
            for child in children.data:
                queue.append(child["id"])

        # Delete related records for all tasks
        for tid in all_ids:
            try:
                self.client.table("task_sessions").delete().eq("task_id", tid).execute()
            except Exception:
                pass
            try:
                self.client.table("task_status_history").delete().eq("task_id", tid).execute()
            except Exception:
                pass
            try:
                self.client.table("task_tags").delete().eq("task_id", tid).execute()
            except Exception:
                pass

        # Delete tasks bottom-up (children first to avoid FK issues)
        for tid in reversed(all_ids):
            self.client.table("tasks").delete().eq("id", tid).execute()

    def get_children(self, parent_id):
        """Return immediate children ordered by position ASC."""
        result = (
            self.client.table("tasks")
            .select("*")
            .eq("parent_id", parent_id)
            .order("position")
            .execute()
        )
        return [self._row_to_task(row) for row in result.data]

    def get_children_counts_batch(self, task_ids):
        """Return {task_id: (child_count, complete_count)} for a list of task IDs."""
        if not task_ids:
            return {}
        result = (
            self.client.table("tasks")
            .select("parent_id, status")
            .in_("parent_id", task_ids)
            .execute()
        )
        counts = {}
        for row in result.data:
            pid = row['parent_id']
            if pid not in counts:
                counts[pid] = [0, 0]
            counts[pid][0] += 1
            if row['status'] == 'complete':
                counts[pid][1] += 1
        return {k: tuple(v) for k, v in counts.items()}

    def get_session_counts_batch(self, task_ids):
        """Return {task_id: session_count} for a list of task IDs."""
        if not task_ids:
            return {}
        result = (
            self.client.table("task_sessions")
            .select("task_id")
            .in_("task_id", task_ids)
            .execute()
        )
        counts = {}
        for row in result.data:
            tid = row['task_id']
            counts[tid] = counts.get(tid, 0) + 1
        return counts

    def get_ancestors(self, task_id):
        """Walk up the parent chain using a Postgres RPC function.

        Requires a ``get_ancestors`` function in the Supabase database::

            CREATE OR REPLACE FUNCTION get_ancestors(task_id_param TEXT)
            RETURNS SETOF tasks AS $$
              WITH RECURSIVE ancestors AS (
                  SELECT t.* FROM tasks t
                  WHERE t.id = (SELECT parent_id FROM tasks WHERE id = task_id_param)
                UNION ALL
                  SELECT t.* FROM tasks t
                  JOIN ancestors a ON t.id = a.parent_id
              )
              SELECT * FROM ancestors;
            $$ LANGUAGE sql STABLE;

        Returns list[Task] from immediate parent up to root.
        """
        result = self.client.rpc(
            "get_ancestors", {"task_id_param": task_id}
        ).execute()
        return [self._row_to_task(row) for row in result.data]

    def get_tasks_by_status(self, project_id, status):
        """Return all tasks in a project with the given status, ordered by
        position ASC."""
        status_val = status.value if isinstance(status, TaskStatus) else status
        result = (
            self.client.table("tasks")
            .select("*")
            .eq("project_id", project_id)
            .eq("status", status_val)
            .order("position")
            .execute()
        )
        return [self._row_to_task(row) for row in result.data]

    # ------------------------------------------------------------------
    # Ordering
    # ------------------------------------------------------------------

    def reorder_task(self, task_id, after_id, before_id):
        """Place *task_id* between *after_id* and *before_id*.

        Uses gap-numbered integers.  If the midpoint collides (gap of 0),
        the entire column is renumbered with fresh gaps.
        """
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
            new_pos = after_pos + _POSITION_GAP
        else:
            new_pos = (after_pos + before_pos) // 2

        # Collision check
        if new_pos == after_pos or (before_pos is not None and new_pos == before_pos):
            self._renumber_column(task.project_id, task.status.value)
            return self.reorder_task(task_id, after_id, before_id)

        now = datetime.now(timezone.utc).isoformat()
        self.client.table("tasks").update({
            "position": new_pos,
            "updated_at": now,
        }).eq("id", task_id).execute()

    def _renumber_column(self, project_id, status_val):
        """Reassign positions for all tasks in a column with fresh gaps."""
        result = (
            self.client.table("tasks")
            .select("id")
            .eq("project_id", project_id)
            .eq("status", status_val)
            .order("position")
            .execute()
        )
        now = datetime.now(timezone.utc).isoformat()
        for idx, row in enumerate(result.data):
            self.client.table("tasks").update({
                "position": (idx + 1) * _POSITION_GAP,
                "updated_at": now,
            }).eq("id", row["id"]).execute()

    def get_next_position(self, project_id, status):
        """Return the next available position at the end of a column."""
        status_val = status.value if isinstance(status, TaskStatus) else status
        result = (
            self.client.table("tasks")
            .select("position")
            .eq("project_id", project_id)
            .eq("status", status_val)
            .order("position", desc=True)
            .limit(1)
            .execute()
        )
        if result.data:
            return result.data[0]["position"] + _POSITION_GAP
        return _POSITION_GAP

    def get_min_position(self, project_id, status):
        """Return the smallest position in a column (for top-insert)."""
        status_val = status.value if isinstance(status, TaskStatus) else status
        result = (
            self.client.table("tasks")
            .select("position")
            .eq("project_id", project_id)
            .eq("status", status_val)
            .order("position", desc=False)
            .limit(1)
            .execute()
        )
        if result.data:
            return result.data[0]["position"]
        return _POSITION_GAP

    # ------------------------------------------------------------------
    # Task ↔ Session links
    # ------------------------------------------------------------------

    def link_session(self, task_id, session_id):
        """Associate a Claude session with a task."""
        now = datetime.now(timezone.utc).isoformat()
        self.client.table("task_sessions").upsert({
            "task_id": task_id,
            "session_id": session_id,
            "created_at": now,
        }).execute()
        return TaskSession(task_id=task_id, session_id=session_id, created_at=now)

    def unlink_session(self, task_id, session_id):
        """Remove the link between a session and a task."""
        (
            self.client.table("task_sessions")
            .delete()
            .eq("task_id", task_id)
            .eq("session_id", session_id)
            .execute()
        )

    def get_task_sessions(self, task_id):
        """Return list of session_id strings linked to a task."""
        result = (
            self.client.table("task_sessions")
            .select("session_id")
            .eq("task_id", task_id)
            .order("created_at")
            .execute()
        )
        return [row["session_id"] for row in result.data]

    def get_session_task(self, session_id):
        """Return the task_id linked to a session, or None."""
        result = (
            self.client.table("task_sessions")
            .select("task_id")
            .eq("session_id", session_id)
            .execute()
        )
        if not result.data:
            return None
        return result.data[0]["task_id"]

    def remap_session(self, old_id, new_id):
        """Update all task_sessions rows from old_id to new_id."""
        self.client.table("task_sessions").update(
            {"session_id": new_id}
        ).eq("session_id", old_id).execute()

    # ------------------------------------------------------------------
    # Validation Issues
    # ------------------------------------------------------------------

    def create_issue(self, task_id, description, session_id=None):
        """Log a new validation issue against a task."""
        issue_id = str(uuid.uuid4())
        now = datetime.now(timezone.utc).isoformat()
        self.client.table("task_issues").insert({
            "id": issue_id,
            "task_id": task_id,
            "description": description,
            "session_id": session_id,
            "resolved_at": None,
            "created_at": now,
        }).execute()
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
        now = datetime.now(timezone.utc).isoformat()
        (
            self.client.table("task_issues")
            .update({"resolved_at": now})
            .eq("id", issue_id)
            .execute()
        )

    def get_open_issues(self, task_id):
        """Return unresolved issues for a task."""
        result = (
            self.client.table("task_issues")
            .select("*")
            .eq("task_id", task_id)
            .is_("resolved_at", "null")
            .order("created_at")
            .execute()
        )
        return [self._row_to_issue(row) for row in result.data]

    def get_all_issues(self, task_id):
        """Return every issue (open and resolved) for a task."""
        result = (
            self.client.table("task_issues")
            .select("*")
            .eq("task_id", task_id)
            .order("created_at")
            .execute()
        )
        return [self._row_to_issue(row) for row in result.data]

    # ------------------------------------------------------------------
    # Columns / Board Config
    # ------------------------------------------------------------------

    def get_columns(self, project_id):
        """Return BoardColumn list for a project, ordered by position."""
        result = (
            self.client.table("board_columns")
            .select("*")
            .eq("project_id", project_id)
            .order("position")
            .execute()
        )
        if not result.data:
            self._create_default_columns(project_id)
            result = (
                self.client.table("board_columns")
                .select("*")
                .eq("project_id", project_id)
                .order("position")
                .execute()
            )
        return [self._row_to_column(row) for row in result.data]

    def create_column(self, project_id_or_dict, name=None, status_key=None,
                      position=None, color=None, sort_mode='manual',
                      sort_direction='desc'):
        """Insert a single column. Accepts positional args or a migration dict."""
        if isinstance(project_id_or_dict, dict):
            d = project_id_or_dict
            col_id = d.get("id", str(uuid.uuid4()))
            # Remove any existing column with same project+status_key to avoid unique constraint
            pid = d.get("project_id", "")
            sk = d.get("status_key", "")
            if pid and sk:
                try:
                    self.client.table("board_columns").delete().eq("project_id", pid).eq("status_key", sk).execute()
                except Exception:
                    pass
            row = {
                "id": col_id,
                "project_id": pid,
                "name": d.get("name", ""),
                "status_key": sk,
                "position": d.get("position", 0),
                "color": d.get("color", "#8b949e"),
                "sort_mode": d.get("sort_mode", "manual"),
                "sort_direction": d.get("sort_direction", "desc"),
            }
        else:
            col_id = str(uuid.uuid4())
            row = {
                "id": col_id,
                "project_id": project_id_or_dict,
                "name": name,
                "status_key": status_key,
                "position": position,
                "color": color,
                "sort_mode": sort_mode,
                "sort_direction": sort_direction,
            }
        self.client.table("board_columns").insert(row).execute()
        result = (
            self.client.table("board_columns")
            .select("*")
            .eq("id", col_id)
            .execute()
        )
        return self._row_to_column(result.data[0])

    def upsert_columns(self, project_id, columns):
        """Replace the column configuration for a project."""
        # Delete existing columns for the project
        (
            self.client.table("board_columns")
            .delete()
            .eq("project_id", project_id)
            .execute()
        )
        # Insert new columns
        for col in columns:
            self.client.table("board_columns").insert({
                "id": col.id or str(uuid.uuid4()),
                "project_id": project_id,
                "name": col.name,
                "status_key": col.status_key,
                "position": col.position,
                "color": col.color,
                "sort_mode": col.sort_mode,
                "sort_direction": col.sort_direction,
            }).execute()

    def update_columns(self, project_id, columns_data):
        """Update columns from a list of dicts (API-facing alias)."""
        (
            self.client.table("board_columns")
            .delete()
            .eq("project_id", project_id)
            .execute()
        )
        for col in columns_data:
            self.client.table("board_columns").insert({
                "id": col.get("id", str(uuid.uuid4())),
                "project_id": project_id,
                "name": col["name"],
                "status_key": col["status_key"],
                "position": col.get("position", 0),
                "color": col.get("color", "#8b949e"),
                "sort_mode": col.get("sort_mode", "manual"),
                "sort_direction": col.get("sort_direction", "desc"),
            }).execute()
        return self.get_columns(project_id)

    def add_status_history(self, task_id, old_status, new_status, changed_by=None, changed_at=None):
        """Record a status transition in the history table."""
        self.client.table("task_status_history").insert({
            "id": str(uuid.uuid4()),
            "task_id": task_id,
            "old_status": old_status,
            "new_status": new_status,
            "changed_by": changed_by,
            "changed_at": changed_at or datetime.now(timezone.utc).isoformat(),
        }).execute()

    def _create_default_columns(self, project_id):
        """Insert the five default columns for a new project."""
        for name, status_key, position, color in _DEFAULT_COLUMNS:
            self.client.table("board_columns").insert({
                "id": str(uuid.uuid4()),
                "project_id": project_id,
                "name": name,
                "status_key": status_key,
                "position": position,
                "color": color,
                "sort_mode": "manual",
                "sort_direction": "desc",
            }).execute()

    # ------------------------------------------------------------------
    # Tags
    # ------------------------------------------------------------------

    @staticmethod
    def _row_to_tag(row: dict) -> TaskTag:
        return TaskTag(
            id=row["id"],
            task_id=row["task_id"],
            tag=row["tag"],
            created_at=row["created_at"],
        )

    def add_tag(self, task_id, tag):
        """Add a tag to a task.  Returns TaskTag."""
        tag_id = str(uuid.uuid4())
        now = datetime.now(timezone.utc).isoformat()
        self.client.table("task_tags").upsert({
            "id": tag_id,
            "task_id": task_id,
            "tag": tag,
            "created_at": now,
        }).execute()
        result = (
            self.client.table("task_tags")
            .select("*")
            .eq("task_id", task_id)
            .eq("tag", tag)
            .execute()
        )
        return self._row_to_tag(result.data[0])

    def remove_tag(self, task_id, tag):
        """Remove a tag from a task."""
        (
            self.client.table("task_tags")
            .delete()
            .eq("task_id", task_id)
            .eq("tag", tag)
            .execute()
        )

    def get_task_tags(self, task_id):
        """Return list of TaskTag records for a task."""
        result = (
            self.client.table("task_tags")
            .select("*")
            .eq("task_id", task_id)
            .order("tag")
            .execute()
        )
        return [self._row_to_tag(row) for row in result.data]

    def get_tasks_by_tag(self, project_id, tag):
        """Return all tasks in a project that carry a given tag.

        Uses a Supabase inner join via PostgREST foreign-key embedding.
        Falls back to a two-step query if the join syntax is unavailable.
        """
        # Two-step: get task_ids, then fetch tasks
        tag_result = (
            self.client.table("task_tags")
            .select("task_id")
            .eq("tag", tag)
            .execute()
        )
        task_ids = [r["task_id"] for r in tag_result.data]
        if not task_ids:
            return []
        result = (
            self.client.table("tasks")
            .select("*")
            .eq("project_id", project_id)
            .in_("id", task_ids)
            .order("position")
            .execute()
        )
        return [self._row_to_task(row) for row in result.data]

    def get_all_tags(self, project_id):
        """Return all distinct tag strings used in a project.

        Two-step: get all task_ids for project, then distinct tags.
        """
        tasks_result = (
            self.client.table("tasks")
            .select("id")
            .eq("project_id", project_id)
            .execute()
        )
        task_ids = [r["id"] for r in tasks_result.data]
        if not task_ids:
            return []
        tags_result = (
            self.client.table("task_tags")
            .select("tag")
            .in_("task_id", task_ids)
            .execute()
        )
        return sorted(set(r["tag"] for r in tags_result.data))

    # ------------------------------------------------------------------
    # Raw SQL (for reports)
    # ------------------------------------------------------------------

    def execute_sql(self, sql, params=()):
        """Execute an arbitrary read-only SQL query via Supabase RPC.

        Requires a Postgres function in the Supabase database::

            CREATE OR REPLACE FUNCTION execute_readonly_sql(query TEXT, params JSONB DEFAULT '[]')
            RETURNS JSONB AS $$
            DECLARE
                result JSONB;
            BEGIN
                EXECUTE query INTO result USING params;
                RETURN result;
            END;
            $$ LANGUAGE plpgsql SECURITY DEFINER;

        If the RPC function is not available, this raises NotImplementedError
        and the reports layer will show an error to the user.
        """
        try:
            result = self.client.rpc(
                "execute_readonly_sql",
                {"query": sql, "params": list(params)},
            ).execute()
            return result.data if isinstance(result.data, list) else []
        except Exception:
            raise NotImplementedError(
                "The execute_sql method requires a 'execute_readonly_sql' "
                "RPC function in your Supabase database. See the Kanban "
                "plan document for the CREATE FUNCTION statement."
            )

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

        result = (
            self.client.table("tasks")
            .select("*")
            .eq("project_id", project_id)
            .order("position")
            .execute()
        )
        # Compute depths in Python from the flat list — no extra queries
        depths = self._compute_depths(result.data)
        all_tasks = [self._row_to_task(row, depth=depths.get(row["id"], 0))
                     for row in result.data]

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
    # Migration helpers (used by BackendMigrator for data portability)
    # ------------------------------------------------------------------

    def get_all_preferences(self):
        result = self.client.table("preferences").select("*").execute()
        return [dict(r) for r in result.data]

    def set_preference(self, key, value):
        now = datetime.now(timezone.utc).isoformat()
        self.client.table("preferences").upsert({
            "key": key, "value": str(value), "updated_at": now,
        }).execute()

    def get_all_columns_all_projects(self):
        result = (
            self.client.table("board_columns")
            .select("*")
            .order("project_id")
            .order("position")
            .execute()
        )
        return [dict(r) for r in result.data]

    def get_all_tasks_ordered(self):
        """Return all tasks, parents before children."""
        result = (
            self.client.table("tasks")
            .select("*")
            .order("created_at")
            .execute()
        )
        rows = [dict(r) for r in result.data]
        # Sort so parents come before children
        by_id = {r["id"]: r for r in rows}
        ordered, seen = [], set()

        def _visit(row):
            if row["id"] in seen:
                return
            pid = row.get("parent_id")
            if pid and pid in by_id and pid not in seen:
                _visit(by_id[pid])
            seen.add(row["id"])
            ordered.append(row)

        for r in rows:
            _visit(r)
        return ordered

    def get_all_task_sessions(self):
        result = self.client.table("task_sessions").select("*").execute()
        return [dict(r) for r in result.data]

    def get_all_task_tags(self):
        result = self.client.table("task_tags").select("*").execute()
        return [dict(r) for r in result.data]

    def get_all_status_history(self):
        result = (
            self.client.table("task_status_history")
            .select("*")
            .order("changed_at")
            .execute()
        )
        return [dict(r) for r in result.data]

    def create_task_from_dict(self, d):
        """Insert a task from a migration dict (preserving original id)."""
        status = d.get("status", "not_started")
        if isinstance(status, TaskStatus):
            status = status.value
        self.client.table("tasks").upsert({
            "id": d["id"],
            "project_id": d.get("project_id", ""),
            "parent_id": d.get("parent_id"),
            "title": d.get("title", ""),
            "description": d.get("description"),
            "verification_url": d.get("verification_url"),
            "status": status,
            "position": d.get("position", 0),
            "owner": d.get("owner"),
            "created_at": d.get("created_at", ""),
            "updated_at": d.get("updated_at", ""),
        }).execute()


class SchemaNotReady(ConnectionError):
    """Raised when the Supabase database exists but the kanban tables
    haven't been created yet."""

    def __init__(self, url: str):
        self.url = url
        super().__init__(
            "SCHEMA_NOT_READY: Connected to Supabase but the kanban "
            "tables don't exist yet. Run the setup SQL in your Supabase "
            "SQL Editor."
        )
