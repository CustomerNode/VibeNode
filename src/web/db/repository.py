"""
Abstract repository interface for Kanban board data access.

Defines the data model (dataclasses) and the backend-agnostic ABC that
all storage implementations must satisfy.  The rest of the codebase
imports these types and programs against KanbanRepository — never
touching SQL or API calls directly.
"""

from abc import ABC, abstractmethod
from dataclasses import dataclass, asdict, field
from enum import Enum
from typing import Optional


# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------

class TaskStatus(Enum):
    """Lifecycle states a task can occupy on the board."""
    NOT_STARTED  = "not_started"
    WORKING      = "working"
    VALIDATING   = "validating"
    REMEDIATING  = "remediating"
    COMPLETE     = "complete"


# ---------------------------------------------------------------------------
# Dataclasses
# ---------------------------------------------------------------------------

@dataclass
class Task:
    """A single work item on the Kanban board."""
    id: str
    project_id: str
    parent_id: Optional[str]
    title: str
    description: Optional[str]
    verification_url: Optional[str]
    status: TaskStatus
    position: int
    depth: int
    created_at: str
    updated_at: str
    owner: Optional[str] = None

    def to_dict(self):
        """Serialize to a JSON-safe dict (enum values become strings)."""
        d = asdict(self)
        d['status'] = self.status.value
        return d


@dataclass
class TaskSession:
    """Link between a task and a Claude session."""
    task_id: str
    session_id: str
    created_at: str
    session_type: str = 'session'  # 'session' (work) or 'planner'

    def to_dict(self):
        return asdict(self)


@dataclass
class TaskIssue:
    """A validation issue logged against a task."""
    id: str
    task_id: str
    description: str
    session_id: Optional[str]
    resolved_at: Optional[str]
    created_at: str

    def to_dict(self):
        return asdict(self)


@dataclass
class TaskTag:
    """A tag attached to a task."""
    id: str
    task_id: str
    tag: str
    created_at: str

    def to_dict(self):
        return asdict(self)


@dataclass
class BoardColumn:
    """A column in the Kanban board UI."""
    id: str
    project_id: str
    name: str
    status_key: str
    position: int
    color: str
    sort_mode: str
    sort_direction: str
    is_terminal: bool = False
    is_regression: bool = False

    def to_dict(self):
        return asdict(self)


# ---------------------------------------------------------------------------
# Abstract repository
# ---------------------------------------------------------------------------

class KanbanRepository(ABC):
    """Backend-agnostic interface for all Kanban data operations.

    Every method is synchronous — the Flask app runs with
    ``async_mode='threading'`` so there is no event loop to await on.
    """

    # ── Lifecycle ──────────────────────────────────────────────────────

    @abstractmethod
    def initialize(self):
        """Run migrations / connect to database.  Called once at startup."""
        ...

    @abstractmethod
    def close(self):
        """Release resources (connections, file handles)."""
        ...

    # ── Tasks ──────────────────────────────────────────────────────────

    @abstractmethod
    def create_task(self, task):
        """Insert a new task and return the persisted Task."""
        ...

    @abstractmethod
    def get_task(self, task_id):
        """Return a Task by id, or None if not found."""
        ...

    @abstractmethod
    def update_task(self, task_id, **fields):
        """Partial update — only the supplied keyword fields are changed.
        Returns the updated Task."""
        ...

    @abstractmethod
    def delete_task(self, task_id):
        """Delete a task.  Children are cascade-deleted by the DB."""
        ...

    @abstractmethod
    def get_children(self, parent_id):
        """Return immediate children of *parent_id*, ordered by position."""
        ...

    @abstractmethod
    def get_ancestors(self, task_id):
        """Walk up the parent chain via recursive CTE.
        Returns list[Task] from immediate parent to root."""
        ...

    @abstractmethod
    def get_tasks_by_status(self, project_id, status):
        """Return all tasks in *project_id* with the given TaskStatus,
        ordered by position."""
        ...

    # ── Ordering ───────────────────────────────────────────────────────

    @abstractmethod
    def reorder_task(self, task_id, after_id, before_id):
        """Place *task_id* between *after_id* and *before_id* using
        gap-numbered positions.  Pass None for either end."""
        ...

    @abstractmethod
    def get_next_position(self, project_id, status):
        """Return the position value to use for a new task appended
        to the end of the column identified by *status*."""
        ...

    @abstractmethod
    def get_min_position(self, project_id, status):
        """Return the smallest position in a column (for top-insert)."""
        ...

    # ── Task ↔ Session links ──────────────────────────────────────────

    @abstractmethod
    def link_session(self, task_id, session_id, session_type='session'):
        """Associate a Claude session with a task.  Returns TaskSession."""
        ...

    @abstractmethod
    def unlink_session(self, task_id, session_id):
        """Remove the association between a session and a task."""
        ...

    @abstractmethod
    def get_task_sessions(self, task_id, session_type=None):
        """Return list of TaskSession objects linked to *task_id*.

        Args:
            session_type: Optional filter — 'session', 'planner', or None for all.
        """
        ...

    @abstractmethod
    def get_session_task(self, session_id):
        """Return the task_id linked to *session_id*, or None."""
        ...

    @abstractmethod
    def remap_session(self, old_id, new_id):
        """Update all task_sessions rows from old_id to new_id."""
        ...

    # ── Validation Issues ─────────────────────────────────────────────

    @abstractmethod
    def create_issue(self, task_id, description, session_id=None):
        """Log a new issue against a task.  Returns TaskIssue."""
        ...

    @abstractmethod
    def resolve_issue(self, issue_id):
        """Mark an issue as resolved (sets resolved_at)."""
        ...

    @abstractmethod
    def get_open_issues(self, task_id):
        """Return unresolved TaskIssue records for *task_id*."""
        ...

    @abstractmethod
    def get_all_issues(self, task_id):
        """Return every TaskIssue (open and resolved) for *task_id*."""
        ...

    # ── Tags ──────────────────────────────────────────────────────────

    @abstractmethod
    def add_tag(self, task_id, tag):
        """Add a tag to a task.  Returns TaskTag."""
        ...

    @abstractmethod
    def remove_tag(self, task_id, tag):
        """Remove a tag from a task."""
        ...

    @abstractmethod
    def get_task_tags(self, task_id):
        """Return list of TaskTag records for *task_id*."""
        ...

    @abstractmethod
    def get_tasks_by_tag(self, project_id, tag):
        """Return all tasks in *project_id* that carry *tag*."""
        ...

    @abstractmethod
    def get_all_tags(self, project_id):
        """Return all distinct tag strings used in *project_id*."""
        ...

    # ── Raw SQL (for reports) ─────────────────────────────────────────

    @abstractmethod
    def execute_sql(self, sql, params=()):
        """Execute an arbitrary read-only SQL query and return rows as
        list[dict].  Used by the reports layer."""
        ...

    # ── Columns / Board Config ────────────────────────────────────────

    @abstractmethod
    def get_columns(self, project_id):
        """Return BoardColumn list for *project_id*, ordered by position."""
        ...

    @abstractmethod
    def upsert_columns(self, project_id, columns):
        """Replace the column configuration for *project_id*."""
        ...

    # ── Full Board ────────────────────────────────────────────────────

    @abstractmethod
    def get_board(self, project_id):
        """Return the complete board state as a dict::

            {
                "columns": [BoardColumn, ...],
                "tasks":   { status_key: [Task, ...], ... }
            }

        Columns are auto-created for new projects.
        """
        ...
