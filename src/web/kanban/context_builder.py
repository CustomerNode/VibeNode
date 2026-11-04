"""
Session context injection for task-scoped sessions.

Builds a lightweight briefing (~600 tokens) that is injected as a system
prompt prefix when a session starts on a task. This gives Claude awareness
of its position in the project, what siblings are doing, and any open issues
from failed validation.

When AI status modification is enabled, the context also includes instructions
for emitting status-change actions that update the kanban board automatically.
"""

from ..db.repository import KanbanRepository


# ---------------------------------------------------------------------------
# Template
# ---------------------------------------------------------------------------

TASK_CONTEXT_TEMPLATE = """You are working on a specific task within a larger project.

## Current Task
**{task_title}**
{task_description}

## Verification
Check your work at: {verification_url}

## Position in Project
{breadcrumb_path}

## Parent Context
{parent_title}: {parent_description}

## Sibling Tasks (same level)
{sibling_list}

## Active Sessions on Sibling Tasks
{active_sessions}

## Open Issues
{open_issues}

Focus your work on the current task. The sibling tasks and active sessions
are shown for awareness — coordinate if your changes might affect shared code.
{ai_actions_section}"""

# Instructions appended when AI can modify statuses
_AI_ACTIONS_TEMPLATE = """
## Task Status Actions

You can update task statuses on the kanban board by emitting action markers.
When you start working on a subtask, finish it, or notice something needs
attention, emit the appropriate marker. The system will automatically process
these and update the board in real time.

**Format:** `<!-- kanban:status task_id=TASK_UUID status=STATUS_VALUE -->`

**Valid statuses:** not_started, working, validating, complete, remediating

**Guidelines:**
- When you begin working on a task: emit `status=working`
- When you finish and the task is ready for human review: emit `status=validating`
{mark_complete_note}
- If you find issues during work: emit `status=remediating`
- Only change statuses for tasks listed in the sibling list above

**Available task IDs:**
{task_id_list}
"""

# Caps to keep context within ~600 token budget
_MAX_SIBLINGS = 10
_MAX_ACTIVE_SESSIONS = 8
_MAX_DESCRIPTION_CHARS = 200


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def truncate(text, max_chars):
    """Truncate text with '...' if it exceeds max_chars."""
    if not text:
        return ""
    if len(text) <= max_chars:
        return text
    return text[:max_chars - 3] + "..."


def _format_duration(started_at):
    """Format a session start time as a human-readable duration string."""
    if not started_at:
        return "unknown"
    try:
        from datetime import datetime, timezone
        start = datetime.fromisoformat(started_at.replace("Z", "+00:00"))
        now = datetime.now(timezone.utc)
        delta = now - start
        minutes = int(delta.total_seconds() / 60)
        if minutes < 60:
            return f"{minutes}m"
        hours = minutes // 60
        return f"{hours}h"
    except Exception:
        return "unknown"


def _build_ai_actions_section(siblings, task_id, ai_can_modify, ai_can_complete):
    """Build the AI actions section if preferences allow it."""
    if not ai_can_modify:
        return ""

    # Build task ID reference list from siblings
    task_id_lines = []
    for s in siblings[:_MAX_SIBLINGS]:
        if s.id == task_id:
            task_id_lines.append(f"- `{s.id}` — **{s.title}** (current task)")
        else:
            task_id_lines.append(f"- `{s.id}` — {s.title} [{s.status.value}]")

    mark_complete_note = (
        "- When you are confident a task is fully complete: emit `status=complete`"
        if ai_can_complete
        else "- You may NOT mark tasks as complete — leave them at `validating` for human review"
    )

    return _AI_ACTIONS_TEMPLATE.format(
        task_id_list="\n".join(task_id_lines) or "(no tasks available)",
        mark_complete_note=mark_complete_note,
    )


# ---------------------------------------------------------------------------
# Builder
# ---------------------------------------------------------------------------

def build_task_context(repo, task_id, daemon_client=None):
    """Build a lightweight context string for a task-scoped session.

    Target output size: ~600-900 tokens (slightly larger when AI actions enabled).

    Args:
        repo: KanbanRepository instance.
        task_id: UUID string of the task this session is scoped to.
        daemon_client: Optional DaemonClient for querying live session state.

    Returns:
        Formatted context string ready for system prompt injection.

    Raises:
        ValueError: If the task does not exist.
    """
    task = repo.get_task(task_id)
    if task is None:
        raise ValueError(f"Task not found: {task_id}")

    # Load AI autonomy preferences
    try:
        from ..config import get_kanban_config
        cfg = get_kanban_config()
    except Exception:
        cfg = {}
    ai_can_modify = cfg.get("ai_can_modify_status", True)
    ai_can_complete = cfg.get("ai_can_mark_complete", True)

    # Ancestors for breadcrumb
    ancestors = repo.get_ancestors(task_id)

    # Siblings (other children of the same parent)
    siblings = repo.get_children(task.parent_id) if task.parent_id else []

    # Parent task
    parent = repo.get_task(task.parent_id) if task.parent_id else None

    # Open issues from failed validation
    open_issues = repo.get_open_issues(task_id)

    # -- Breadcrumb --
    ancestor_titles = [a.title for a in reversed(ancestors)]
    breadcrumb = " -> ".join(ancestor_titles + [task.title])

    # -- Sibling list with status badges --
    sibling_lines = []
    for s in siblings[:_MAX_SIBLINGS]:
        if s.id == task_id:
            continue
        sibling_lines.append(f"- [{s.status.value}] {s.title}")

    # -- Active sessions on sibling tasks --
    active_session_lines = []
    if daemon_client is not None:
        for s in siblings:
            if s.id == task_id:
                continue
            linked = repo.get_task_sessions(s.id)
            for link in linked:
                sid = link.session_id if hasattr(link, 'session_id') else link
                try:
                    session_info = daemon_client.get_session_info(sid)
                    if session_info and getattr(session_info, 'status', None) in (
                        "working", "idle", "question"
                    ):
                        duration = _format_duration(getattr(session_info, 'started_at', None))
                        active_session_lines.append(
                            f"- [{session_info.status}] on \"{s.title}\" ({duration})"
                        )
                except Exception:
                    pass
                if len(active_session_lines) >= _MAX_ACTIVE_SESSIONS:
                    break
            if len(active_session_lines) >= _MAX_ACTIVE_SESSIONS:
                break
        sessions_text = "\n".join(active_session_lines) or "(no active sessions on related tasks)"
    else:
        sessions_text = "(unavailable)"

    # -- Open issues --
    issue_lines = []
    for issue in open_issues:
        issue_lines.append(f"- {issue.description}")

    # -- AI actions section (conditional on preferences) --
    ai_actions = _build_ai_actions_section(siblings, task_id, ai_can_modify, ai_can_complete)

    return TASK_CONTEXT_TEMPLATE.format(
        task_title=task.title,
        task_description=truncate(task.description, _MAX_DESCRIPTION_CHARS) or "(no description)",
        verification_url=task.verification_url or "(none set)",
        breadcrumb_path=breadcrumb,
        parent_title=parent.title if parent else "(root level)",
        parent_description=truncate(parent.description, _MAX_DESCRIPTION_CHARS) if parent else "",
        sibling_list="\n".join(sibling_lines) or "(no siblings)",
        active_sessions=sessions_text,
        open_issues="\n".join(issue_lines) or "(none)",
        ai_actions_section=ai_actions,
    )
