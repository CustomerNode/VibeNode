"""
Kanban board REST API -- task CRUD, status transitions, session linking,
column configuration, and issue tracking.

Performance notes (Supabase)
----------------------------
Every Supabase call is an HTTPS round-trip (~50-150ms).  The board and
drill-down endpoints are optimized to minimize these:

1. **ensure_project_columns** is cached in-memory after the first call
   (_ensured_projects set in defaults.py).  Cost: 0ms after first request.

2. **get_board** fetches columns + all tasks in 2 queries.  Depth is
   computed in Python from the flat task list (see _compute_depths in
   supabase_backend.py) — the old code walked the parent chain per-row
   which caused 60-80 extra queries for ~30 tasks.

3. **_build_recursive_counts** computes children counts and session
   counts from the already-fetched task list + one batch session query.
   The old code did recursive per-task get_children + get_task_sessions
   calls (N+1 pattern).

4. **Tags are merged into the board response** so the frontend doesn't
   need a second fetch to /api/kanban/tags.

5. **Task detail (drill-down)** reuses get_board to fetch all tasks in
   one shot, then filters/enriches in Python.  The old code did per-child
   get_children + recursive _count_descendant_sessions calls.

6. **Task move** emits only kanban_task_moved (not kanban_task_updated
   too), avoiding a double board refresh on the frontend.

Rule of thumb: never call repo.get_children / repo.get_task_sessions /
repo.get_ancestors in a loop.  Fetch all tasks once, compute in Python.
"""

import os
import subprocess
import uuid as uuid_mod
from datetime import datetime, timezone

from flask import Blueprint, jsonify, request

from ..config import get_active_project
from ..db import create_repository, reset_repository
from ..db.repository import Task, TaskStatus
from ..kanban.state_machine import transition_task, handle_session_start, handle_session_complete
from ..kanban.defaults import ensure_project_columns
from ..kanban.context_builder import build_task_context
from ..kanban.ai_planner import plan_subtasks, apply_plan, run_planner

bp = Blueprint('kanban_api', __name__)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _get_repo():
    """Create and return a KanbanRepository for the current request."""
    return create_repository()


def _count_descendant_sessions(repo, task_id):
    """Count all sessions linked to a task and all its descendants."""
    count = len(repo.get_task_sessions(task_id))
    for child in repo.get_children(task_id):
        count += _count_descendant_sessions(repo, child.id)
    return count


def _build_recursive_counts(all_tasks, repo):
    """Build children counts and session counts from already-fetched task list.

    Returns (child_counts, session_counts) dicts keyed by task_id.
    child_counts[id] = (total_children, completed_children) — recursive
    session_counts[id] = total_sessions — recursive (direct only for now)

    Does all computation in Python from the flat task list + one batch
    session query instead of N+1 recursive DB calls.
    """
    # Build parent -> children index
    children_by_parent = {}
    for t in all_tasks:
        pid = t.parent_id
        if pid:
            children_by_parent.setdefault(pid, []).append(t)

    # Batch-fetch session counts for ALL tasks in one query
    all_ids = [t.id for t in all_tasks]
    if hasattr(repo, 'get_session_counts_batch') and all_ids:
        direct_sessions = repo.get_session_counts_batch(all_ids)
    else:
        direct_sessions = {}

    # Recursive count caches
    _child_cache = {}
    _session_cache = {}

    def _recurse_children(tid):
        if tid in _child_cache:
            return _child_cache[tid]
        kids = children_by_parent.get(tid, [])
        total = len(kids)
        done = sum(1 for c in kids if c.status.value == 'complete')
        for c in kids:
            sub_total, sub_done = _recurse_children(c.id)
            total += sub_total
            done += sub_done
        _child_cache[tid] = (total, done)
        return (total, done)

    def _recurse_sessions(tid):
        if tid in _session_cache:
            return _session_cache[tid]
        count = direct_sessions.get(tid, 0)
        for c in children_by_parent.get(tid, []):
            count += _recurse_sessions(c.id)
        _session_cache[tid] = count
        return count

    child_counts = {}
    session_counts = {}
    for t in all_tasks:
        child_counts[t.id] = _recurse_children(t.id)
        session_counts[t.id] = _recurse_sessions(t.id)

    return child_counts, session_counts


def _get_project_id():
    """Return the active project identifier."""
    return get_active_project()


def _emit(event, data):
    """Emit a SocketIO event if socketio is available."""
    try:
        from .. import socketio
        # Serialize dataclass objects to dicts for SocketIO
        if hasattr(data, 'to_dict'):
            data = data.to_dict()
        socketio.emit(event, data)
    except Exception:
        pass  # SocketIO not available -- skip


def _task_response(task):
    """Convert a Task dataclass to a JSON-safe dict."""
    if task is None:
        return None
    if hasattr(task, 'to_dict'):
        return task.to_dict()
    return task


# ---------------------------------------------------------------------------
# Board
# ---------------------------------------------------------------------------

@bp.route("/api/kanban/board")
def get_board():
    """Get full board state: columns + paginated tasks for current project.

    Query params:
        page      – 1-based page number (default 1)
        page_size – tasks per column per page (default 50)
        tags      – comma-separated tag filter
        parent_id – scope to a parent task's children
    """
    try:
        import time as _t, logging as _lg
        _bt = [_t.perf_counter()]
        repo = _get_repo()
        project_id = _get_project_id()
        ensure_project_columns(repo, project_id)
        _bt.append(_t.perf_counter())  # [1] after ensure_cols

        # Pagination params
        page = max(1, int(request.args.get("page", 1)))
        page_size = max(1, int(request.args.get("page_size", 50)))

        # Tag filter
        tags_param = request.args.get("tags", "").strip()
        active_tag_filter = [t.strip() for t in tags_param.split(",") if t.strip()] if tags_param else []

        # Support scoped view: ?parent_id=X shows only that parent's children
        scope_parent_id = request.args.get("parent_id", "").strip() or None

        board = repo.get_board(project_id)
        _bt.append(_t.perf_counter())  # [2] after get_board
        columns = board.get("columns", [])
        tasks = board.get("tasks", {})

        # Collect ALL tasks (flat) for recursive count computation
        all_tasks_flat = []
        for v in (tasks.values() if isinstance(tasks, dict) else []):
            all_tasks_flat.extend(v)

        # Build recursive counts in Python from the flat list + one batch
        # session query — replaces the old N+1 _count_descendant_sessions.
        child_counts, session_counts = _build_recursive_counts(all_tasks_flat, repo)
        _bt.append(_t.perf_counter())  # [3] after counts

        # Filter to the requested level — no extra DB queries needed,
        # all_tasks_flat already has every task in the project.
        if scope_parent_id:
            # Drill-down: show direct children of the scoped parent
            filtered = {}
            for k, v in (tasks.items() if isinstance(tasks, dict) else []):
                filtered[k] = [t for t in v if t.parent_id == scope_parent_id]
            tasks = filtered
        else:
            # Default: show only root tasks (no parent)
            filtered = {}
            for k, v in (tasks.items() if isinstance(tasks, dict) else []):
                filtered[k] = [t for t in v if not t.parent_id]
            tasks = filtered

        # Apply tag filter — keep only tasks that have ALL requested tags
        if active_tag_filter:
            filtered = {}
            for k, v in (tasks.items() if isinstance(tasks, dict) else []):
                filtered[k] = [
                    t for t in v
                    if all(
                        tag in (getattr(t, 'tags', None) or [])
                        for tag in active_tag_filter
                    )
                ]
            tasks = filtered

        # Enrich tasks with computed fields + apply per-column pagination
        enriched_flat = []
        column_dicts = []
        for col in columns:
            col_dict = col.to_dict() if hasattr(col, 'to_dict') else dict(col)
            col_tasks = tasks.get(col.status_key, []) if isinstance(tasks, dict) else []
            total_count = len(col_tasks)

            # Paginate per column
            start = (page - 1) * page_size
            end = start + page_size
            page_tasks = col_tasks[start:end]

            col_dict['total_count'] = total_count
            col_dict['page'] = page
            col_dict['has_more'] = end < total_count
            column_dicts.append(col_dict)

            for t in page_tasks:
                td = t.to_dict() if hasattr(t, 'to_dict') else t
                cc = child_counts.get(td['id'], (0, 0))
                td['children_count'] = cc[0]
                td['children_complete'] = cc[1]
                td['session_count'] = session_counts.get(td['id'], 0)
                td['active_sessions'] = 0
                enriched_flat.append(td)

        _bt.append(_t.perf_counter())  # [4] after enrichment
        _lg.getLogger("kanban_api").warning(
            "BOARD ensure=%.0fms board=%.0fms counts=%.0fms enrich=%.0fms TOTAL=%.0fms tasks=%d",
            (_bt[1]-_bt[0])*1000, (_bt[2]-_bt[1])*1000, (_bt[3]-_bt[2])*1000,
            (_bt[4]-_bt[3])*1000, (_bt[4]-_bt[0])*1000, len(all_tasks_flat))
        # Include all tags so the frontend doesn't need a second fetch
        try:
            all_tags = repo.get_all_tags(project_id)
        except Exception:
            all_tags = []

        _timing = {"ensure": int((_bt[1]-_bt[0])*1000), "board": int((_bt[2]-_bt[1])*1000),
                   "counts": int((_bt[3]-_bt[2])*1000), "enrich": int((_bt[4]-_bt[3])*1000),
                   "total": int((_bt[4]-_bt[0])*1000), "tasks": len(all_tasks_flat)}
        return jsonify({
            "columns": column_dicts,
            "tasks": enriched_flat,
            "tags": all_tags,
            "active_tag_filter": active_tag_filter,
            "_timing": _timing,
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# ---------------------------------------------------------------------------
# Task CRUD
# ---------------------------------------------------------------------------

@bp.route("/api/kanban/tasks", methods=["POST"])
def create_task():
    """Create a new task.

    Body: {title, parent_id?, description?, verification_url?, status?}
    """
    try:
        data = request.get_json(silent=True) or {}
        title = data.get("title", "").strip()
        if not title:
            return jsonify({"error": "Title is required"}), 400

        repo = _get_repo()
        project_id = _get_project_id()

        task_id = str(uuid_mod.uuid4())
        now = datetime.now(timezone.utc).isoformat()
        parent_id = data.get("parent_id") or None

        # Compute depth from ancestors (no depth limit — plan Section 3)
        depth = 0
        if parent_id:
            ancestors = repo.get_ancestors(parent_id)
            depth = len(ancestors) + 1

        status_str = data.get("status", "not_started")
        status = TaskStatus(status_str)
        insert_position = data.get("insert_position", "bottom")
        if insert_position == "top":
            # Position 0 — the board sorts by position ASC so this goes first
            # Existing tasks have position >= 1000 (gap numbering)
            position = 0
        else:
            position = repo.get_next_position(project_id, status_str)

        task_obj = Task(
            id=task_id,
            project_id=project_id,
            parent_id=parent_id,
            title=title,
            description=data.get("description", ""),
            verification_url=data.get("verification_url", ""),
            status=status,
            position=position,
            depth=depth,
            created_at=now,
            updated_at=now,
        )
        task = repo.create_task(task_obj)

        _emit("kanban_task_created", task)
        return jsonify(_task_response(task))
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@bp.route("/api/kanban/tasks/<task_id>")
def get_task(task_id):
    """Get a single task with its children and linked sessions."""
    try:
        repo = _get_repo()
        task = repo.get_task(task_id)
        if task is None:
            return jsonify({"error": "Task not found"}), 404

        # Fetch all project tasks in ONE query, compute counts in Python.
        # This replaces N+1 get_children + recursive _count_descendant_sessions
        # calls that were causing 8s+ response times on Supabase.
        project_id = _get_project_id()
        board = repo.get_board(project_id)
        all_tasks_flat = []
        for v in board.get("tasks", {}).values():
            all_tasks_flat.extend(v)
        child_counts, session_counts = _build_recursive_counts(all_tasks_flat, repo)

        # Build parent->children index from flat list
        children_by_parent = {}
        for t in all_tasks_flat:
            if t.parent_id:
                children_by_parent.setdefault(t.parent_id, []).append(t)

        children = children_by_parent.get(task_id, [])
        sessions = repo.get_task_sessions(task_id)
        issues = repo.get_open_issues(task_id)
        result = _task_response(task)

        # Enrich children with precomputed counts (no extra queries)
        enriched_children = []
        for c in children:
            cd = _task_response(c)
            cc = child_counts.get(c.id, (0, 0))
            cd['children_count'] = cc[0]
            cd['children_complete'] = cc[1]
            cd['session_count'] = session_counts.get(c.id, 0)
            cd['active_sessions'] = 0
            enriched_children.append(cd)

        # Enrich parent task with precomputed counts
        parent_cc = child_counts.get(task_id, (0, 0))
        result['children_count'] = parent_cc[0]
        result['children_complete'] = parent_cc[1]
        result['session_count'] = session_counts.get(task_id, 0)
        result['active_sessions'] = 0

        result["children"] = enriched_children
        # Enrich sessions with live status from daemon.
        # Display names are resolved client-side from allSessions (same as
        # grid/list/workforce views) so naming is consistent across the app.
        enriched_sessions = []
        active_count = 0
        try:
            from flask import current_app
            sm = getattr(current_app, 'session_manager', None)
            for sid in sessions:
                sess_id = sid.session_id if hasattr(sid, 'session_id') else sid
                sess_info = {'session_id': sess_id, 'status': 'sleeping'}
                if sm:
                    try:
                        state = sm.get_session_state(sess_id)
                        if state:
                            sess_info['status'] = state
                            if state in ('working', 'idle'):
                                active_count += 1
                    except Exception:
                        pass
                enriched_sessions.append(sess_info)
        except Exception:
            enriched_sessions = [{'session_id': (s.session_id if hasattr(s, 'session_id') else s), 'status': 'sleeping'} for s in sessions]

        result['active_sessions'] = active_count
        result["sessions"] = enriched_sessions
        result["issues"] = [i.to_dict() if hasattr(i, 'to_dict') else i for i in issues]
        # Include tags
        tags = repo.get_task_tags(task_id)
        result["tags"] = [t.tag if hasattr(t, 'tag') else t for t in tags]
        return jsonify(result)
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@bp.route("/api/kanban/tasks/<task_id>", methods=["PATCH"])
def update_task(task_id):
    """Update task fields from request body.

    If 'status' is included in the update, the state machine is used
    to validate and execute the transition.
    """
    try:
        data = request.get_json(silent=True) or {}
        if not data:
            return jsonify({"error": "No update data provided"}), 400

        repo = _get_repo()
        task = repo.get_task(task_id)
        if task is None:
            return jsonify({"error": "Task not found"}), 404

        # If status is changing, use the state machine (force=True for user moves)
        new_status = data.pop("status", None)
        if new_status and new_status != task.status.value:
            task = transition_task(repo, task_id, new_status, force=True)

        # Update remaining fields (title, description, verification_url, etc.)
        if data:
            data["updated_at"] = datetime.now(timezone.utc).isoformat()
            task = repo.update_task(task_id, **data)

        _emit("kanban_task_updated", task)
        return jsonify(_task_response(task))
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@bp.route("/api/kanban/tasks/<task_id>", methods=["DELETE"])
def delete_task(task_id):
    """Delete a task and all its children (cascade)."""
    try:
        repo = _get_repo()
        repo.delete_task(task_id)
        _emit("kanban_board_refresh", {})
        return jsonify({"ok": True})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# ---------------------------------------------------------------------------
# Status transitions
# ---------------------------------------------------------------------------

@bp.route("/api/kanban/tasks/<task_id>/move", methods=["POST"])
def move_task(task_id):
    """Move a task to a new status column.

    Body: {status}
    Uses the state machine to validate the transition.
    """
    try:
        data = request.get_json(silent=True) or {}
        new_status = data.get("status", "").strip()
        if not new_status:
            return jsonify({"error": "Status is required"}), 400

        force = data.get("force", False)
        old_status = data.get("old_status", "")
        repo = _get_repo()
        updated = transition_task(repo, task_id, new_status, force=force)

        # Only emit moved (not updated too — both trigger full board refresh)
        _emit("kanban_task_moved", {
            "task_id": task_id,
            "old_status": old_status,
            "new_status": new_status,
            "position": getattr(updated, 'position', None),
        })
        return jsonify(_task_response(updated))
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# ---------------------------------------------------------------------------
# Reorder
# ---------------------------------------------------------------------------

@bp.route("/api/kanban/tasks/<task_id>/reorder", methods=["POST"])
def reorder_task(task_id):
    """Reorder a task within its column.

    Body: {after_id?, before_id?}
    """
    try:
        data = request.get_json(silent=True) or {}
        repo = _get_repo()

        task = repo.get_task(task_id)
        if task is None:
            return jsonify({"error": "Task not found"}), 404

        repo.reorder_task(
            task_id=task_id,
            after_id=data.get("after_id"),
            before_id=data.get("before_id"),
        )

        # No emit — reorder is done optimistically on the client
        return jsonify({"ok": True})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# ---------------------------------------------------------------------------
# Session linking
# ---------------------------------------------------------------------------

@bp.route("/api/kanban/tasks/<task_id>/sessions", methods=["POST"])
def link_session(task_id):
    """Link a session to a task.

    Body: {session_id}
    Triggers handle_session_start to auto-transition the task if needed.
    """
    try:
        data = request.get_json(silent=True) or {}
        session_id = data.get("session_id", "").strip()
        if not session_id:
            return jsonify({"error": "session_id is required"}), 400

        repo = _get_repo()
        task = repo.get_task(task_id)
        if task is None:
            return jsonify({"error": "Task not found"}), 404

        link = repo.link_session(task_id, session_id)
        updated = handle_session_start(repo, task_id)

        _emit("kanban_task_updated", updated)
        return jsonify(link.to_dict() if hasattr(link, 'to_dict') else link)
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@bp.route("/api/kanban/sessions/<session_id>/unlink-all", methods=["DELETE"])
def unlink_session_from_all(session_id):
    """Unlink a session from ALL tasks it's linked to.

    Called when a session is deleted — cleans up orphaned links.
    """
    try:
        repo = _get_repo()
        task_id = repo.get_session_task(session_id)
        if task_id:
            repo.unlink_session(task_id, session_id)
        return jsonify({"ok": True})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@bp.route("/api/kanban/tasks/<task_id>/sessions/<session_id>", methods=["DELETE"])
def unlink_session(task_id, session_id):
    """Unlink a session from a task.

    Triggers handle_session_complete to check if auto-advance is needed.
    """
    try:
        repo = _get_repo()
        repo.unlink_session(task_id, session_id)
        updated = handle_session_complete(repo, task_id)

        _emit("kanban_task_updated", updated)
        return jsonify({"ok": True})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# ---------------------------------------------------------------------------
# Issues
# ---------------------------------------------------------------------------

@bp.route("/api/kanban/tasks/<task_id>/issues", methods=["POST"])
def create_issue(task_id):
    """Create an issue on a task (from validation rejection).

    Body: {description}
    """
    try:
        data = request.get_json(silent=True) or {}
        description = data.get("description", "").strip()
        if not description:
            return jsonify({"error": "Description is required"}), 400

        repo = _get_repo()
        task = repo.get_task(task_id)
        if task is None:
            return jsonify({"error": "Task not found"}), 404

        issue = repo.create_issue(task_id=task_id, description=description)

        _emit("kanban_task_updated", repo.get_task(task_id))
        return jsonify(issue.to_dict() if hasattr(issue, 'to_dict') else issue)
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@bp.route("/api/kanban/issues/<issue_id>", methods=["PATCH"])
def resolve_issue(issue_id):
    """Mark an issue as resolved."""
    try:
        repo = _get_repo()
        repo.resolve_issue(issue_id)
        return jsonify({"ok": True})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# ---------------------------------------------------------------------------
# Columns
# ---------------------------------------------------------------------------

@bp.route("/api/kanban/columns")
def get_columns():
    """Get column configuration for the current project."""
    try:
        repo = _get_repo()
        project_id = _get_project_id()
        columns = ensure_project_columns(repo, project_id)
        return jsonify([c.to_dict() if hasattr(c, 'to_dict') else c for c in columns])
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@bp.route("/api/kanban/columns", methods=["PUT"])
def update_columns():
    """Replace column configuration for the current project.

    Body: list of column objects [{name, status_key, position, color,
          sort_mode, sort_direction}, ...]
    """
    try:
        data = request.get_json(silent=True)
        if not isinstance(data, list):
            return jsonify({"error": "Expected a list of column objects"}), 400

        repo = _get_repo()
        project_id = _get_project_id()
        columns = repo.update_columns(project_id, data)

        _emit("kanban_board_refresh", {"reason": "columns_updated"})
        return jsonify([c.to_dict() if hasattr(c, 'to_dict') else c for c in columns])
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# ---------------------------------------------------------------------------
# Context (for session injection)
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# Tags
# ---------------------------------------------------------------------------

@bp.route("/api/kanban/tasks/<task_id>/tags", methods=["GET"])
def get_task_tags(task_id):
    """Get all tags for a task."""
    try:
        repo = _get_repo()
        task = repo.get_task(task_id)
        if task is None:
            return jsonify({"error": "Task not found"}), 404
        tags = repo.get_task_tags(task_id)
        tags = [t.tag if hasattr(t, 'tag') else t for t in tags]
        return jsonify({"tags": tags})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@bp.route("/api/kanban/tasks/<task_id>/tags", methods=["POST"])
def add_tag(task_id):
    """Add a tag to a task.

    Body: {tag}
    """
    try:
        data = request.get_json(silent=True) or {}
        tag = data.get("tag", "").strip()
        if not tag:
            return jsonify({"error": "Tag is required"}), 400

        repo = _get_repo()
        task = repo.get_task(task_id)
        if task is None:
            return jsonify({"error": "Task not found"}), 404

        task_tag = repo.add_tag(task_id, tag)
        _emit("kanban_task_updated", repo.get_task(task_id))
        return jsonify(task_tag.to_dict())
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@bp.route("/api/kanban/tasks/<task_id>/tags/<tag>", methods=["DELETE"])
def remove_tag(task_id, tag):
    """Remove a tag from a task."""
    try:
        repo = _get_repo()
        task = repo.get_task(task_id)
        if task is None:
            return jsonify({"error": "Task not found"}), 404

        repo.remove_tag(task_id, tag)
        _emit("kanban_task_updated", repo.get_task(task_id))
        return jsonify({"ok": True})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@bp.route("/api/kanban/tags")
def get_all_tags():
    """List all distinct tags for the current project."""
    try:
        repo = _get_repo()
        project_id = _get_project_id()
        tags = repo.get_all_tags(project_id)
        return jsonify({"tags": tags})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@bp.route("/api/kanban/tags/<tag>/tasks")
def get_tasks_by_tag(tag):
    """Get all tasks with a specific tag."""
    try:
        repo = _get_repo()
        project_id = _get_project_id()
        tasks = repo.get_tasks_by_tag(project_id, tag)
        return jsonify([_task_response(t) for t in tasks])
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# ---------------------------------------------------------------------------
# Context (for session injection)
# ---------------------------------------------------------------------------

@bp.route("/api/kanban/tasks/<task_id>/ancestors")
def get_ancestors(task_id):
    """Get the ancestor chain for breadcrumb navigation."""
    try:
        repo = _get_repo()
        task = repo.get_task(task_id)
        if task is None:
            return jsonify({"error": "Task not found"}), 404

        ancestors = repo.get_ancestors(task_id)
        return jsonify({
            "ancestors": [_task_response(a) for a in ancestors],
            "task": _task_response(task),
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@bp.route("/api/kanban/tasks/<task_id>/bulk", methods=["POST"])
def bulk_action(task_id):
    """Bulk operations on a task's children.

    Body: {action: "complete_all" | "reset_all"}
    """
    try:
        data = request.get_json(silent=True) or {}
        action = data.get("action", "").strip()
        if action not in ("complete_all", "reset_all"):
            return jsonify({"error": "Invalid action. Use 'complete_all' or 'reset_all'"}), 400

        repo = _get_repo()
        task = repo.get_task(task_id)
        if task is None:
            return jsonify({"error": "Task not found"}), 404

        children = repo.get_children(task_id)
        target_status = "complete" if action == "complete_all" else "not_started"
        updated = []
        for child in children:
            if child.status.value != target_status:
                try:
                    t = transition_task(repo, child.id, target_status, force=True)
                    updated.append(_task_response(t))
                except ValueError:
                    pass  # skip invalid transitions

        _emit("kanban_board_refresh", {"reason": "bulk_action"})
        return jsonify({"ok": True, "updated": len(updated)})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@bp.route("/api/kanban/tasks/<task_id>/plan", methods=["POST"])
def plan_task(task_id):
    """Use AI to generate subtask suggestions for a task.

    Returns a list of proposed subtasks (not yet created).
    The frontend shows these in a review modal before accepting.
    """
    try:
        import asyncio
        repo = _get_repo()
        task = repo.get_task(task_id)
        if task is None:
            return jsonify({"error": "Task not found"}), 404

        # Run the async planner
        try:
            loop = asyncio.get_event_loop()
            if loop.is_running():
                # Inside an already-running loop (e.g. Flask-SocketIO async mode)
                import concurrent.futures
                with concurrent.futures.ThreadPoolExecutor() as pool:
                    subtasks = pool.submit(
                        lambda: asyncio.run(run_planner(repo, task_id))
                    ).result(timeout=90)
            else:
                subtasks = loop.run_until_complete(run_planner(repo, task_id))
        except RuntimeError:
            subtasks = asyncio.run(run_planner(repo, task_id))

        return jsonify({"subtasks": subtasks})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@bp.route("/api/kanban/tasks/<task_id>/plan/apply", methods=["POST"])
def apply_task_plan(task_id):
    """Accept AI-generated subtasks and create them under the parent task.

    Body: {subtasks: [{title, description, verification_url}, ...]}
    """
    try:
        data = request.get_json(silent=True) or {}
        subtasks = data.get("subtasks", [])
        if not subtasks:
            return jsonify({"error": "No subtasks provided"}), 400

        repo = _get_repo()
        task = repo.get_task(task_id)
        if task is None:
            return jsonify({"error": "Task not found"}), 404

        created = apply_plan(repo, task_id, subtasks, task.project_id)
        _emit("kanban_board_refresh", {"reason": "plan_applied"})
        return jsonify({
            "ok": True,
            "created": [_task_response(t) for t in created],
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# ---------------------------------------------------------------------------
# AI Planner — chat-based endpoints (plan Section 5b, lines 1466-1467)
# ---------------------------------------------------------------------------

PLANNER_CHAT_SYSTEM = """You are a task planning assistant. The user will describe
what they want to accomplish. Break it down into a hierarchical task tree.

Respond with a JSON structure wrapped in ```json code fences:
```json
{
  "response": "Your conversational response explaining the breakdown",
  "tasks": [
    {
      "title": "...",
      "description": "...",
      "subtasks": [
        { "title": "...", "description": "", "subtasks": [] }
      ]
    }
  ]
}
```

Guidelines:
- Each task should be a concrete, actionable unit of work
- Aim for tasks completable in 1-3 sessions
- Use 2-4 levels of nesting max unless the user asks for more
- Include descriptions only when the title isn't self-explanatory
- The "response" field should be a friendly conversational explanation
"""


@bp.route("/api/kanban/planner/chat", methods=["POST"])
def planner_chat():
    """Chat with AI planner — returns proposed task tree.

    Uses the Anthropic API directly (no CLI, no terminal windows).
    Body: {message, history: [{role, content}, ...]}
    Returns: {response, proposal: {tasks: [...]}}
    """
    import json as json_mod
    import os
    import re as re_mod

    try:
        data = request.get_json(silent=True) or {}
        message = data.get("message", "").strip()
        history = data.get("history", [])
        if not message:
            return jsonify({"error": "Message is required"}), 400

        # Build messages — include history for refinement
        messages = []
        for h in history:
            if h.get("role") in ("user", "assistant"):
                messages.append({"role": h["role"], "content": h["content"]})
        messages.append({"role": "user", "content": message})

        result_text = None

        # Strategy 1: Direct Anthropic API (fast, needs ANTHROPIC_API_KEY)
        if os.environ.get("ANTHROPIC_API_KEY"):
            try:
                import anthropic
                client = anthropic.Anthropic()
                resp = client.messages.create(
                    model="claude-sonnet-4-20250514",
                    max_tokens=4096,
                    system=PLANNER_CHAT_SYSTEM,
                    messages=messages,
                )
                result_text = resp.content[0].text if resp.content else ""
            except Exception as api_err:
                import logging
                logging.getLogger(__name__).warning("Planner API failed, trying CLI: %s", api_err)

        # Strategy 2: Claude CLI fallback (uses CLI auth / OAuth — no key needed)
        if not result_text:
            import subprocess
            import sys
            prompt = PLANNER_CHAT_SYSTEM + "\n\n"
            for m in messages:
                prompt += "[" + m.get("role", "user").upper() + "]\n" + m["content"] + "\n\n"
            creationflags = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0
            r = subprocess.run(
                ["claude", "-p", prompt, "--output-format", "text",
                 "--max-turns", "1", "--model", "sonnet"],
                capture_output=True, text=True, timeout=120,
                creationflags=creationflags,
            )
            if r.returncode != 0:
                return jsonify({"error": "AI planning failed: " + r.stderr[:300]}), 500
            result_text = r.stdout.strip()

        if not result_text:
            return jsonify({"error": "No response from AI planner"}), 500

        # Parse JSON from response — try ```json fences, then brace-balanced extraction
        response_text = result_text
        proposal = None
        json_match = re_mod.search(r'```json\s*([\s\S]*?)```', result_text)
        if json_match:
            raw = json_match.group(1)
        else:
            # Brace-balanced extraction for first complete top-level {...}
            raw = None
            start = result_text.find('{')
            if start >= 0:
                depth, end = 0, -1
                for i in range(start, len(result_text)):
                    if result_text[i] == '{': depth += 1
                    elif result_text[i] == '}':
                        depth -= 1
                        if depth == 0: end = i; break
                if end > start:
                    raw = result_text[start:end+1]
        if raw:
            try:
                parsed = json_mod.loads(raw)
                if "tasks" in parsed:
                    proposal = {"tasks": parsed["tasks"]}
                if "response" in parsed:
                    response_text = parsed["response"]
            except (json_mod.JSONDecodeError, KeyError):
                pass

        return jsonify({
            "response": response_text,
            "proposal": proposal,
        })

    except Exception as e:
        return jsonify({"error": str(e)}), 500


@bp.route("/api/kanban/planner/accept", methods=["POST"])
def planner_accept():
    """Accept AI planner proposal — batch-inserts all tasks recursively.

    Body: {proposal: {tasks: [{title, description, subtasks: [...]}]}}
    Returns: {created_count}
    """
    import uuid as uuid_mod
    from datetime import datetime, timezone

    try:
        data = request.get_json(silent=True) or {}
        proposal = data.get("proposal", {})
        tasks = proposal.get("tasks", [])
        if not tasks:
            return jsonify({"error": "No tasks in proposal"}), 400

        repo = _get_repo()
        project_id = _get_project_id()
        now = datetime.now(timezone.utc).isoformat()
        insert_position = data.get("insert_position", "bottom")
        scope_parent_id = data.get("parent_id")  # scoped plan: insert under this parent
        count = [0]
        root_ids = []  # track top-level created task IDs for highlight

        from ..db.repository import Task, TaskStatus

        # If scoped to a parent, delete existing subtree first
        if scope_parent_id:
            existing_children = repo.get_children(scope_parent_id)
            for child in existing_children:
                try:
                    repo.delete_task(child.id)
                except Exception:
                    pass

        # Flatten the tree into creates and updates
        flat_tasks = []
        update_items = []  # (existing_id, {fields}) for edits

        def _flatten(items, parent_id=None):
            for i, item in enumerate(items):
                existing_id = item.get("id")
                # If AI returned an id AND that task exists, update it
                if existing_id and repo.get_task(existing_id):
                    updates = {}
                    if "title" in item:
                        updates["title"] = item["title"]
                    if "description" in item:
                        updates["description"] = item["description"]
                    if updates:
                        updates["updated_at"] = now
                        update_items.append((existing_id, updates))
                    if parent_id is None:
                        root_ids.append(existing_id)
                    count[0] += 1
                    subtasks = item.get("subtasks", [])
                    if subtasks:
                        _flatten(subtasks, existing_id)
                else:
                    task_id = str(uuid_mod.uuid4())
                    if parent_id is None:
                        root_ids.append(task_id)
                    if insert_position == "top" and parent_id is None:
                        position = -(len(items) - i) * 1000
                    else:
                        position = (i + 1) * 1000
                    flat_tasks.append(Task(
                        id=task_id,
                        project_id=project_id,
                        parent_id=parent_id,
                        title=item.get("title", "Untitled"),
                        description=item.get("description", ""),
                        verification_url=item.get("verification_url"),
                        status=TaskStatus.NOT_STARTED,
                        position=position,
                        depth=0,
                        created_at=now,
                        updated_at=now,
                    ))
                    count[0] += 1
                    subtasks = item.get("subtasks", [])
                    if subtasks:
                        _flatten(subtasks, task_id)

        _flatten(tasks, parent_id=scope_parent_id)

        # Apply updates to existing tasks
        for tid, fields in update_items:
            repo.update_task(tid, **fields)

        # Batch insert new tasks
        if flat_tasks:
            if hasattr(repo, '_get_conn'):
                conn = repo._get_conn()
                for t in flat_tasks:
                    conn.execute(
                        "INSERT INTO tasks "
                        "(id, project_id, parent_id, title, description, verification_url, "
                        " status, position, created_at, updated_at) "
                        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                        (t.id, t.project_id, t.parent_id, t.title, t.description,
                         t.verification_url, t.status.value, t.position, now, now),
                    )
                    conn.execute(
                        "INSERT INTO task_status_history (id, task_id, old_status, new_status, changed_by, changed_at) "
                        "VALUES (?, ?, ?, ?, ?, ?)",
                        (str(uuid_mod.uuid4()), t.id, None, t.status.value, None, now),
                    )
                conn.commit()
            elif hasattr(repo, 'client'):
                task_rows = [{
                    "id": t.id, "project_id": t.project_id,
                    "parent_id": t.parent_id, "title": t.title,
                    "description": t.description,
                    "verification_url": t.verification_url,
                    "status": t.status.value, "position": t.position,
                    "created_at": now, "updated_at": now,
                } for t in flat_tasks]
                history_rows = [{
                    "id": str(uuid_mod.uuid4()), "task_id": t.id,
                    "old_status": None, "new_status": t.status.value,
                    "changed_at": now,
                } for t in flat_tasks]
                repo.client.table("tasks").insert(task_rows).execute()
                repo.client.table("task_status_history").insert(history_rows).execute()
            else:
                for t in flat_tasks:
                    repo.create_task(t)
        _emit("kanban_board_refresh", {"reason": "plan_accepted"})
        return jsonify({"created_count": count[0], "created_ids": root_ids})

    except Exception as e:
        return jsonify({"error": str(e)}), 500


@bp.route("/api/kanban/config")
def get_kanban_config():
    """Get current kanban configuration."""
    from ..config import get_kanban_config as _get_cfg
    cfg = _get_cfg()
    return jsonify({
        "backend": cfg.get("kanban_backend", "sqlite"),
        "kanban_backend": cfg.get("kanban_backend", "sqlite"),
        "supabase_url": cfg.get("supabase_url", ""),
        "supabase_publishable_key": cfg.get("supabase_publishable_key", ""),
        "supabase_secret_key": cfg.get("supabase_secret_key", ""),
        "supabase_connected": bool(cfg.get("supabase_url")),
        "depth_limit": cfg.get("kanban_depth_limit", 5),
        "auto_advance": cfg.get("kanban_auto_advance", False),
        "kanban_auto_advance": cfg.get("kanban_auto_advance", False),
        "kanban_page_size": cfg.get("kanban_page_size", 50),
    })


@bp.route("/api/kanban/config", methods=["PUT"])
def update_kanban_config():
    """Update kanban configuration."""
    from ..config import get_kanban_config as _get_cfg, save_kanban_config as _save_cfg
    data = request.get_json(silent=True) or {}
    cfg = _get_cfg()

    old_backend = cfg.get("kanban_backend", "sqlite")

    if "backend" in data:
        cfg["kanban_backend"] = data["backend"]
    if "supabase_url" in data:
        cfg["supabase_url"] = data["supabase_url"]
    if "supabase_secret_key" in data:
        cfg["supabase_secret_key"] = data["supabase_secret_key"]
    if "supabase_publishable_key" in data:
        cfg["supabase_publishable_key"] = data["supabase_publishable_key"]
    if "depth_limit" in data:
        cfg["kanban_depth_limit"] = int(data["depth_limit"])
    # Behavior preferences
    for pref_key in ("auto_start_on_session", "auto_parent_working",
                     "auto_parent_reopen", "auto_advance_to_validating"):
        if pref_key in data:
            cfg[pref_key] = bool(data[pref_key])
    # Legacy key migration
    if "auto_advance" in data and "auto_advance_to_validating" not in data:
        cfg["auto_advance_to_validating"] = bool(data["auto_advance"])
    if "kanban_page_size" in data:
        cfg["kanban_page_size"] = int(data["kanban_page_size"])

    _save_cfg(cfg)

    # If backend changed, reset the cached repository singleton
    if cfg.get("kanban_backend", "sqlite") != old_backend:
        reset_repository()

    return jsonify({"ok": True})


@bp.route("/api/kanban/session-state-change", methods=["POST"])
def session_state_change():
    """Bridge: when a session changes state, update linked kanban task.

    Body: {session_id, state}
    Called by the frontend socket.js when session_state events fire.
    """
    try:
        data = request.get_json(silent=True) or {}
        session_id = data.get("session_id", "").strip()
        state = data.get("state", "").strip()
        if not session_id or not state:
            return jsonify({"ok": False}), 400

        repo = _get_repo()
        task_id = repo.get_session_task(session_id)
        if not task_id:
            return jsonify({"ok": True, "linked": False})

        task = repo.get_task(task_id)
        if not task:
            return jsonify({"ok": True, "linked": False})

        if state in ("working",):
            # Session became active -> task should be Working
            updated = handle_session_start(repo, task_id)
            _emit("kanban_task_updated", updated)
        elif state in ("idle", "stopped"):
            # Session went idle/stopped -> check if auto-advance needed
            updated = handle_session_complete(repo, task_id)
            _emit("kanban_task_updated", updated)

        return jsonify({"ok": True, "linked": True, "task_id": task_id})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


@bp.route("/api/kanban/tasks/<task_id>/context")
def get_task_context(task_id):
    """Get the context injection string for a task-scoped session."""
    try:
        from flask import current_app
        repo = _get_repo()
        daemon_client = getattr(current_app, 'session_manager', None)
        context = build_task_context(repo, task_id, daemon_client=daemon_client)
        return jsonify({"context": context})
    except ValueError as e:
        return jsonify({"error": str(e)}), 404
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# ---------------------------------------------------------------------------
# User identity helper
# ---------------------------------------------------------------------------

def _get_user_identity():
    """Detect user identity: preference > git email > git name > OS username."""
    try:
        repo = _get_repo()
        pref = repo.get_preference("kanban_user_identity")
        if pref:
            return pref
    except Exception:
        pass
    try:
        result = subprocess.run(
            ["git", "config", "user.email"],
            capture_output=True, text=True, timeout=5
        )
        if result.returncode == 0 and result.stdout.strip():
            return result.stdout.strip()
    except Exception:
        pass
    try:
        return os.getlogin()
    except Exception:
        return "unknown"


# ---------------------------------------------------------------------------
# Task claiming
# ---------------------------------------------------------------------------

@bp.route("/api/kanban/tasks/<task_id>/claim", methods=["POST"])
def claim_task(task_id):
    """Claim task — set owner to current user identity."""
    try:
        repo = _get_repo()
        task = repo.get_task(task_id)
        if task is None:
            return jsonify({"error": "Task not found"}), 404
        identity = _get_user_identity()
        updated = repo.update_task(task_id, owner=identity)
        _emit("kanban_task_updated", {"task": _task_response(updated)})
        return jsonify(_task_response(updated))
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@bp.route("/api/kanban/tasks/<task_id>/unclaim", methods=["POST"])
def unclaim_task(task_id):
    """Unclaim task — set owner to None."""
    try:
        repo = _get_repo()
        task = repo.get_task(task_id)
        if task is None:
            return jsonify({"error": "Task not found"}), 404
        updated = repo.update_task(task_id, owner=None)
        _emit("kanban_task_updated", {"task": _task_response(updated)})
        return jsonify(_task_response(updated))
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# ---------------------------------------------------------------------------
# Status history
# ---------------------------------------------------------------------------

@bp.route("/api/kanban/tasks/<task_id>/history")
def get_task_history(task_id):
    """Paginated status history for a task."""
    try:
        page = request.args.get("page", 1, type=int)
        page_size = request.args.get("page_size", 20, type=int)
        repo = _get_repo()
        task = repo.get_task(task_id)
        if task is None:
            return jsonify({"error": "Task not found"}), 404
        history = repo.get_status_history(task_id)
        start = (page - 1) * page_size
        end = start + page_size
        return jsonify({
            "history": history[start:end],
            "total": len(history),
            "page": page,
            "has_more": end < len(history),
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# ---------------------------------------------------------------------------
# Tag suggestions
# ---------------------------------------------------------------------------

@bp.route("/api/kanban/tags/suggest")
def suggest_tags():
    """Autocomplete tag suggestions ranked by usage_count (most-used first)."""
    try:
        q = request.args.get("q", "").strip().lower()
        project_id = _get_project_id()
        repo = _get_repo()

        # Query tag usage counts directly from the database
        rows = repo.execute_sql(
            """
            SELECT tt.tag, COUNT(*) as usage_count
            FROM task_tags tt
            JOIN tasks t ON tt.task_id = t.id
            WHERE t.project_id = ?
            GROUP BY tt.tag
            ORDER BY usage_count DESC
            """,
            (project_id,),
        )

        # Filter by query prefix
        if q:
            rows = [r for r in rows if q in r['tag'].lower()]

        # Return ranked tag objects with usage_count
        tags = [{"tag": r["tag"], "usage_count": r["usage_count"]} for r in rows[:10]]
        return jsonify({"tags": tags})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# ---------------------------------------------------------------------------
# Migration
# ---------------------------------------------------------------------------

@bp.route("/api/kanban/migrate", methods=["POST"])
def migrate_backend():
    """Switch database backend (SQLite <-> Supabase)."""
    try:
        data = request.get_json(silent=True) or {}
        target_backend = data.get("target", "").strip()
        if target_backend not in ("sqlite", "supabase"):
            return jsonify({"error": "Invalid target. Use 'sqlite' or 'supabase'"}), 400

        from ..db.migrator import BackendMigrator, MigrationError
        current_repo = _get_repo()
        migrator = BackendMigrator()

        if target_backend == "supabase":
            from ..db.supabase_backend import SupabaseRepository
            url = data.get("supabase_url", "")
            key = data.get("supabase_secret_key", "")
            if not url or not key:
                return jsonify({"error": "supabase_url and supabase_secret_key required"}), 400
            target_repo = SupabaseRepository(url=url, key=key)
        else:
            from ..db.sqlite_backend import SqliteRepository
            target_repo = SqliteRepository()

        target_repo.initialize()
        migrator.switch_backend(current_repo, target_repo)

        from ..config import save_kanban_config, get_kanban_config
        cfg = get_kanban_config()
        cfg["kanban_backend"] = target_backend
        if target_backend == "supabase":
            cfg["supabase_url"] = url
            cfg["supabase_secret_key"] = key
        save_kanban_config(cfg)

        # Clear cached singleton so subsequent requests use the new backend
        reset_repository()

        return jsonify({"ok": True, "message": f"Migrated to {target_backend}"})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@bp.route("/api/kanban/migrate/test", methods=["POST"])
def migrate_test():
    """Test connection to target backend without migrating."""
    try:
        data = request.get_json(silent=True) or {}
        target = data.get("target", "").strip()
        if target == "supabase":
            url = data.get("supabase_url", "")
            key = data.get("supabase_secret_key", "")
            if not url or not key:
                return jsonify({"ok": False, "error": "URL and key required"}), 400
            from ..db.supabase_backend import SupabaseRepository, SchemaNotReady
            repo = SupabaseRepository(url=url, key=key)
            try:
                repo.initialize()
                return jsonify({"ok": True, "message": "Connection successful"})
            except SchemaNotReady:
                setup_sql = SupabaseRepository.get_setup_sql()
                return jsonify({
                    "ok": False,
                    "needs_schema": True,
                    "setup_sql": setup_sql,
                    "error": "Connected! But the database tables don't exist yet. "
                             "Click 'Copy Setup SQL' and run it in your Supabase SQL Editor.",
                })
        elif target == "sqlite":
            from ..db.sqlite_backend import SqliteRepository
            repo = SqliteRepository()
            repo.initialize()
            return jsonify({"ok": True, "message": "SQLite OK"})
        else:
            return jsonify({"ok": False, "error": "Invalid target"}), 400
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


@bp.route("/api/kanban/setup-schema", methods=["POST"])
def setup_schema():
    """Auto-create the kanban tables in Supabase using the Management API."""
    try:
        data = request.get_json(silent=True) or {}
        project_url = data.get("supabase_url", "").strip()
        access_token = data.get("access_token", "").strip()
        if not project_url or not access_token:
            return jsonify({"ok": False, "error": "Project URL and access token required"}), 400

        from ..db.supabase_backend import SupabaseRepository
        SupabaseRepository.provision_schema(project_url, access_token)
        return jsonify({"ok": True, "message": "Schema created successfully"})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


@bp.route("/api/kanban/migrate/status")
def migrate_status():
    """Return current backend, record counts, last migration timestamp."""
    try:
        from ..config import get_kanban_config
        cfg = get_kanban_config()
        repo = _get_repo()
        project_id = _get_project_id()

        tasks = repo.get_tasks_by_status(project_id, None)
        task_count = len(tasks) if tasks else 0
        tags = repo.get_all_tags(project_id) if hasattr(repo, 'get_all_tags') else []

        return jsonify({
            "backend": cfg.get("kanban_backend", "sqlite"),
            "task_count": task_count,
            "tag_count": len(tags),
            "last_migration": cfg.get("last_migration_at"),
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# ---------------------------------------------------------------------------
# Cloud Backups
# ---------------------------------------------------------------------------

def _backups_dir():
    """Return the backups directory, creating it if needed."""
    from pathlib import Path
    d = Path(__file__).resolve().parent.parent.parent / "backups"
    d.mkdir(exist_ok=True)
    return d


@bp.route("/api/kanban/backup/download", methods=["POST"])
def backup_download():
    """Export current backend data to a timestamped JSON file in backups/."""
    import json
    try:
        from ..db.migrator import BackendMigrator
        repo = _get_repo()
        migrator = BackendMigrator()
        data = migrator.export_all(repo)

        # Build a timestamped filename
        ts = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
        filename = f"backup_{ts}.json"
        filepath = _backups_dir() / filename

        # Add metadata
        from ..config import get_kanban_config
        cfg = get_kanban_config()
        record_count = sum(len(v) for v in data.values() if isinstance(v, list))
        payload = {
            "meta": {
                "created_at": datetime.now(timezone.utc).isoformat(),
                "backend": cfg.get("kanban_backend", "sqlite"),
                "record_count": record_count,
            },
            "data": data,
        }

        filepath.write_text(
            json.dumps(payload, indent=2, ensure_ascii=False, default=str),
            encoding="utf-8",
        )

        return jsonify({
            "ok": True,
            "filename": filename,
            "record_count": record_count,
            "path": str(filepath),
        })
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


@bp.route("/api/kanban/backup/list")
def backup_list():
    """List all existing backup files."""
    import json
    try:
        d = _backups_dir()
        backups = []
        for f in sorted(d.glob("backup_*.json"), reverse=True):
            entry = {
                "filename": f.name,
                "size": f.stat().st_size,
                "modified": datetime.fromtimestamp(
                    f.stat().st_mtime, tz=timezone.utc
                ).isoformat(),
            }
            # Try to read metadata from the file
            try:
                content = json.loads(f.read_text(encoding="utf-8"))
                meta = content.get("meta", {})
                entry["record_count"] = meta.get("record_count", "?")
                entry["backend"] = meta.get("backend", "?")
            except Exception:
                entry["record_count"] = "?"
                entry["backend"] = "?"
            backups.append(entry)
        return jsonify({"ok": True, "backups": backups})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


@bp.route("/api/kanban/backup/restore", methods=["POST"])
def backup_restore():
    """Restore data from a backup file, replacing current backend data."""
    import json
    try:
        req = request.get_json(silent=True) or {}
        filename = req.get("filename", "").strip()
        if not filename or ".." in filename or "/" in filename or "\\" in filename:
            return jsonify({"ok": False, "error": "Invalid filename"}), 400

        filepath = _backups_dir() / filename
        if not filepath.is_file():
            return jsonify({"ok": False, "error": "Backup file not found"}), 404

        content = json.loads(filepath.read_text(encoding="utf-8"))
        data = content.get("data", content)  # support with or without meta wrapper

        from ..db.migrator import BackendMigrator
        repo = _get_repo()
        migrator = BackendMigrator()

        # Clear current data, then import the backup
        repo.clear_all_data()
        migrator.import_all(repo, data)

        # Verify
        verify = migrator.export_all(repo)
        restored_count = sum(len(v) for v in verify.values() if isinstance(v, list))
        expected_count = sum(len(v) for v in data.values() if isinstance(v, list))

        if restored_count != expected_count:
            return jsonify({
                "ok": False,
                "error": f"Verification mismatch: expected {expected_count} records, got {restored_count}",
            }), 500

        return jsonify({
            "ok": True,
            "message": f"Restored {restored_count} records from {filename}",
            "record_count": restored_count,
        })
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


@bp.route("/api/kanban/backup/delete", methods=["POST"])
def backup_delete():
    """Delete a backup file."""
    try:
        req = request.get_json(silent=True) or {}
        filename = req.get("filename", "").strip()
        if not filename or ".." in filename or "/" in filename or "\\" in filename:
            return jsonify({"ok": False, "error": "Invalid filename"}), 400

        filepath = _backups_dir() / filename
        if not filepath.is_file():
            return jsonify({"ok": False, "error": "Backup file not found"}), 404

        filepath.unlink()
        return jsonify({"ok": True, "message": f"Deleted {filename}"})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500
