"""
Kanban task status state machine.

Enforces valid status transitions, handles automatic transitions triggered by
session activity, and propagates status changes up the task hierarchy.
"""

from datetime import datetime, timezone

from ..db.repository import KanbanRepository, TaskStatus


# ---------------------------------------------------------------------------
# Valid transitions: {current_status: [allowed_new_statuses]}
# ---------------------------------------------------------------------------

VALID_TRANSITIONS = {
    TaskStatus.NOT_STARTED: [
        TaskStatus.WORKING,           # session starts or user drags
    ],
    TaskStatus.WORKING: [
        TaskStatus.VALIDATING,        # all children done + manual/auto-advance
    ],
    TaskStatus.VALIDATING: [
        TaskStatus.COMPLETE,          # human approves (validation ceremony)
        TaskStatus.REMEDIATING,       # issues found during validation
    ],
    TaskStatus.COMPLETE: [
        TaskStatus.REMEDIATING,       # reopened — new issue found
    ],
    TaskStatus.REMEDIATING: [
        TaskStatus.WORKING,           # work resumes on remediation
    ],
}


def transition_task(repo, task_id, new_status, force=False, session_id=None):
    """Validate and execute a status transition for a task.

    Args:
        repo: KanbanRepository instance.
        task_id: UUID string of the task to transition.
        new_status: Target TaskStatus (or string that maps to one).
        force: If True, skip transition validation (for admin overrides).
        session_id: Optional session ID that triggered this transition.

    Returns:
        The updated Task object.

    Raises:
        ValueError: If the transition is not allowed and force is False.
        ValueError: If the task does not exist.
    """
    # Accept string status values and convert to enum
    if isinstance(new_status, str):
        try:
            new_status = TaskStatus(new_status)
        except ValueError:
            raise ValueError(f"Invalid status: {new_status}")

    task = repo.get_task(task_id)
    if task is None:
        raise ValueError(f"Task not found: {task_id}")

    current_status = task.status

    # No-op if already at the target status
    if current_status == new_status:
        return task

    # Validate transition
    if not force:
        allowed = VALID_TRANSITIONS.get(current_status, [])
        if new_status not in allowed:
            raise ValueError(
                f"Invalid transition: {current_status.value} -> {new_status.value}. "
                f"Allowed: {[s.value for s in allowed]}"
            )

    # Execute the transition
    now = datetime.now(timezone.utc).isoformat()
    updated = repo.update_task(task_id, status=new_status.value, updated_at=now)

    # Record in status history
    repo.add_status_history(task_id, current_status.value, new_status.value, now,
                            session_id=session_id)

    # Propagate status change up the hierarchy
    propagate_up(repo, task_id)

    return updated


def _get_pref(key, default=False):
    """Read a single behavior preference from kanban config."""
    try:
        from ..config import get_kanban_config
        return get_kanban_config().get(key, default)
    except Exception:
        return default


def propagate_up(repo, task_id):
    """After a task changes status, check if its parent needs updating.

    Both rules are controlled by behavior preferences:
      - auto_parent_working: child Working → parent Not Started → Working
      - auto_parent_reopen:  child Remediating → parent Complete → Remediating
    """
    task = repo.get_task(task_id)
    if task is None:
        return

    parent_id = task.parent_id
    if not parent_id:
        return

    parent = repo.get_task(parent_id)
    if parent is None:
        return

    parent_status = parent.status
    child_status = task.status
    transitioned = False

    if (child_status == TaskStatus.WORKING
            and parent_status == TaskStatus.NOT_STARTED
            and _get_pref("auto_parent_working", True)):
        now = datetime.now(timezone.utc).isoformat()
        repo.update_task(parent_id, status=TaskStatus.WORKING.value, updated_at=now)
        repo.add_status_history(
            parent_id, parent_status.value, TaskStatus.WORKING.value, now
        )
        transitioned = True

    if (child_status == TaskStatus.REMEDIATING
            and parent_status == TaskStatus.COMPLETE
            and _get_pref("auto_parent_reopen", True)):
        now = datetime.now(timezone.utc).isoformat()
        repo.update_task(parent_id, status=TaskStatus.REMEDIATING.value, updated_at=now)
        repo.add_status_history(
            parent_id, parent_status.value, TaskStatus.REMEDIATING.value, now
        )
        transitioned = True

    if transitioned:
        propagate_up(repo, parent_id)


def handle_session_start(repo, task_id, session_id=None):
    """Handle a session starting on a task.

    Controlled by auto_start_on_session preference.
    Transitions NOT_STARTED or REMEDIATING -> WORKING.

    Args:
        session_id: Optional session ID that triggered this transition.
    """
    if not _get_pref("auto_start_on_session", True):
        task = repo.get_task(task_id)
        if task is None:
            raise ValueError(f"Task not found: {task_id}")
        return task

    task = repo.get_task(task_id)
    if task is None:
        raise ValueError(f"Task not found: {task_id}")

    if task.status == TaskStatus.NOT_STARTED:
        return transition_task(repo, task_id, TaskStatus.WORKING, session_id=session_id)

    if task.status == TaskStatus.REMEDIATING:
        return transition_task(repo, task_id, TaskStatus.WORKING, session_id=session_id)

    return task


def handle_session_complete(repo, task_id, session_id=None):
    """Handle a session completing / being unlinked from a task.

    Checks if all linked sessions are done and all subtasks are
    validating/complete. If so, and if auto_advance is enabled in config,
    auto-pushes the task to 'validating'.
    NEVER auto-pushes to 'complete' -- that requires manual validation.

    Args:
        session_id: Optional session ID that triggered this transition.

    Returns:
        The updated Task object, or the unchanged task if no transition needed.
    """
    task = repo.get_task(task_id)
    if task is None:
        raise ValueError(f"Task not found: {task_id}")

    # Only auto-advance from working
    if task.status != TaskStatus.WORKING:
        return task

    if not _get_pref("auto_advance_to_validating", False):
        return task

    # Check if there are still active sessions linked to this task
    linked_sessions = repo.get_task_sessions(task_id)
    if linked_sessions:
        return task

    # Check if all children are validating or complete
    children = repo.get_children(task_id)
    if children:
        for child in children:
            if child.status not in (TaskStatus.VALIDATING, TaskStatus.COMPLETE):
                return task

    # All sessions done, all children done (or no children) -> validating
    return transition_task(repo, task_id, TaskStatus.VALIDATING, session_id=session_id)
