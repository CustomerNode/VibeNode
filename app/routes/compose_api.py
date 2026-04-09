"""
Compose API — project CRUD, section management, context, conflict resolution.

Follows the same patterns as kanban_api.py: Flask blueprint, JSON responses,
SocketIO event emission for real-time updates.
"""

import logging

from flask import Blueprint, jsonify, request

from ..compose.models import (
    ComposeProject, ComposeSection, ComposeConflict, ComposeDirective,
    SectionStatus, ConflictStatus,
    scaffold_project, scaffold_section,
    delete_project_folder, delete_section_folder,
    list_projects, get_project, save_project,
    get_sections, get_section,
)

logger = logging.getLogger(__name__)

bp = Blueprint('compose_api', __name__, url_prefix='/api/compose')


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _emit(event, data):
    """Emit a SocketIO event if socketio is available."""
    try:
        from .. import socketio
        if hasattr(data, 'to_dict'):
            data = data.to_dict()
        socketio.emit(event, data)
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Board (what toolbar.js / app.js expects)
# ---------------------------------------------------------------------------

@bp.route('/board')
def get_board():
    """Return board data for the active composition.

    If a project_id query param is provided, use that project.
    Otherwise return the most recently created project's board.
    """
    try:
        project_id = request.args.get('project_id', '').strip()

        if project_id:
            project = get_project(project_id)
        else:
            projects = list_projects()
            project = projects[-1] if projects else None

        if not project:
            return jsonify(None)

        sections = get_sections(project.id)
        section_dicts = [s.to_dict() for s in sections]

        # Compute status summary
        total = len(sections)
        complete = sum(1 for s in sections if s.status == SectionStatus.COMPLETE)
        working = sum(1 for s in sections if s.status == SectionStatus.WORKING)
        not_started = total - complete - working

        # Load conflicts from context
        from ..compose.context_manager import read_context
        try:
            ctx = read_context(project.id)
            conflicts = ctx.get('conflicts', [])
        except Exception:
            conflicts = []

        return jsonify({
            'project': project.to_dict(),
            'sections': section_dicts,
            'status': {
                'total_sections': total,
                'complete': complete,
                'in_progress': working,
                'not_started': not_started,
            },
            'conflicts': conflicts,
        })
    except Exception:
        logger.exception("Error loading compose board")
        return jsonify(None)


# ---------------------------------------------------------------------------
# Project CRUD
# ---------------------------------------------------------------------------

@bp.route('/projects', methods=['POST'])
def create_project():
    """Create a new composition project.

    JSON body: { "name": "Project Name" }
    """
    data = request.get_json(force=True, silent=True) or {}
    name = (data.get('name') or '').strip()
    if not name:
        return jsonify({'ok': False, 'error': 'name is required'}), 400

    project = ComposeProject.create(name)
    pdir = scaffold_project(project)

    logger.info("Created compose project %s at %s", project.id, pdir)
    _emit('compose_board_refresh', {'project_id': project.id})

    return jsonify({'ok': True, 'project': project.to_dict()}), 201


@bp.route('/projects', methods=['GET'])
def list_all_projects():
    """List all composition projects."""
    projects = list_projects()
    return jsonify({'ok': True, 'projects': [p.to_dict() for p in projects]})


@bp.route('/projects/<project_id>', methods=['GET'])
def get_project_detail(project_id):
    """Get a project with its sections and context."""
    project = get_project(project_id)
    if not project:
        return jsonify({'ok': False, 'error': 'Project not found'}), 404

    sections = get_sections(project_id)

    # Load context
    from ..compose.context_manager import read_context
    try:
        ctx = read_context(project_id)
    except Exception:
        ctx = {}

    return jsonify({
        'ok': True,
        'project': project.to_dict(),
        'sections': [s.to_dict() for s in sections],
        'context': ctx,
    })


@bp.route('/projects/<project_id>', methods=['PUT'])
def update_project(project_id):
    """Update project settings (name, shared_prompts_enabled)."""
    project = get_project(project_id)
    if not project:
        return jsonify({'ok': False, 'error': 'Project not found'}), 404

    data = request.get_json(force=True, silent=True) or {}

    if 'name' in data:
        project.name = data['name'].strip()
    if 'shared_prompts_enabled' in data:
        project.shared_prompts_enabled = bool(data['shared_prompts_enabled'])
    if 'root_session_id' in data:
        project.root_session_id = data['root_session_id']

    save_project(project)
    _emit('compose_board_refresh', {'project_id': project_id})

    return jsonify({'ok': True, 'project': project.to_dict()})


@bp.route('/projects/<project_id>', methods=['DELETE'])
def delete_project(project_id):
    """Delete a project and its folder."""
    project = get_project(project_id)
    if not project:
        return jsonify({'ok': False, 'error': 'Project not found'}), 404

    deleted = delete_project_folder(project_id)
    _emit('compose_board_refresh', {'project_id': project_id, 'deleted': True})

    return jsonify({'ok': True, 'deleted': deleted})


# ---------------------------------------------------------------------------
# Section CRUD (Step 3 will expand this)
# ---------------------------------------------------------------------------

@bp.route('/projects/<project_id>/sections', methods=['POST'])
def create_section(project_id):
    """Create a new section in a project.

    JSON body: { "name": "Section Name", "parent_id": null, "artifact_type": "text" }
    """
    project = get_project(project_id)
    if not project:
        return jsonify({'ok': False, 'error': 'Project not found'}), 404

    data = request.get_json(force=True, silent=True) or {}
    name = (data.get('name') or '').strip()
    if not name:
        return jsonify({'ok': False, 'error': 'name is required'}), 400

    # Determine order (append to end)
    existing = get_sections(project_id)
    order = max((s.order for s in existing), default=-1) + 1

    section = ComposeSection.create(
        project_id=project_id,
        name=name,
        parent_id=data.get('parent_id'),
        order=order,
        artifact_type=data.get('artifact_type'),
    )

    # Scaffold folder
    scaffold_section(project_id, section)

    # Add to compose-context.json
    from ..compose.context_manager import add_section_to_context
    try:
        add_section_to_context(project_id, section)
    except Exception:
        logger.exception("Failed to add section to context")

    _emit('compose_task_created', {
        'project_id': project_id,
        'section': section.to_dict(),
    })

    return jsonify({'ok': True, 'section': section.to_dict()}), 201


@bp.route('/projects/<project_id>/sections/<section_id>', methods=['PUT'])
def update_section(project_id, section_id):
    """Update a section's properties."""
    from ..compose.context_manager import update_section_in_context
    project = get_project(project_id)
    if not project:
        return jsonify({'ok': False, 'error': 'Project not found'}), 404

    section = get_section(project_id, section_id)
    if not section:
        return jsonify({'ok': False, 'error': 'Section not found'}), 404

    data = request.get_json(force=True, silent=True) or {}

    if 'name' in data:
        section.name = data['name'].strip()
    if 'status' in data:
        section.status = SectionStatus(data['status'])
    if 'artifact_type' in data:
        section.artifact_type = data['artifact_type']
    if 'session_id' in data:
        section.session_id = data['session_id']
    if 'parent_id' in data:
        section.parent_id = data['parent_id']
    if 'summary' in data:
        section.summary = data['summary']

    try:
        update_section_in_context(project_id, section)
    except Exception:
        logger.exception("Failed to update section in context")

    _emit('compose_task_updated', {
        'project_id': project_id,
        'section': section.to_dict(),
    })

    return jsonify({'ok': True, 'section': section.to_dict()})


@bp.route('/projects/<project_id>/sections/<section_id>', methods=['DELETE'])
def delete_section(project_id, section_id):
    """Delete a section and its folder."""
    section = get_section(project_id, section_id)
    if not section:
        return jsonify({'ok': False, 'error': 'Section not found'}), 404

    # Remove from context
    from ..compose.context_manager import remove_section_from_context
    try:
        remove_section_from_context(project_id, section_id)
    except Exception:
        logger.exception("Failed to remove section from context")

    delete_section_folder(project_id, section.name)

    _emit('compose_board_refresh', {'project_id': project_id})

    return jsonify({'ok': True})


@bp.route('/projects/<project_id>/sections/reorder', methods=['POST'])
def reorder_sections(project_id):
    """Reorder sections.

    JSON body: { "order": ["section-id-1", "section-id-2", ...] }
    """
    from ..compose.context_manager import reorder_sections_in_context

    data = request.get_json(force=True, silent=True) or {}
    order = data.get('order', [])
    if not order:
        return jsonify({'ok': False, 'error': 'order is required'}), 400

    try:
        reorder_sections_in_context(project_id, order)
    except Exception:
        logger.exception("Failed to reorder sections")
        return jsonify({'ok': False, 'error': 'Failed to reorder'}), 500

    _emit('compose_board_refresh', {'project_id': project_id})

    return jsonify({'ok': True})


# ---------------------------------------------------------------------------
# Context endpoints (Step 4 will expand)
# ---------------------------------------------------------------------------

@bp.route('/projects/<project_id>/context', methods=['GET'])
def get_context(project_id):
    """Read the compose-context.json for a project."""
    from ..compose.context_manager import read_context
    try:
        ctx = read_context(project_id)
        return jsonify({'ok': True, 'context': ctx})
    except Exception:
        return jsonify({'ok': False, 'error': 'Failed to read context'}), 500


@bp.route('/projects/<project_id>/context/facts', methods=['PUT'])
def update_facts(project_id):
    """Update facts in compose-context.json.

    JSON body: { "facts": { "key": "value", ... } }
    """
    from ..compose.context_manager import update_facts as _update_facts
    data = request.get_json(force=True, silent=True) or {}
    facts = data.get('facts', {})

    try:
        _update_facts(project_id, facts)
        return jsonify({'ok': True})
    except Exception:
        logger.exception("Failed to update facts")
        return jsonify({'ok': False, 'error': 'Failed to update facts'}), 500


@bp.route('/projects/<project_id>/sections/<section_id>/status', methods=['PUT'])
def update_section_status(project_id, section_id):
    """Update a section's status and summary.

    JSON body: { "status": "working", "summary": "...", "changing": false, "change_note": "..." }
    """
    from ..compose.context_manager import update_section_status as _update_status
    data = request.get_json(force=True, silent=True) or {}

    try:
        _update_status(
            project_id,
            section_id,
            status=data.get('status'),
            summary=data.get('summary'),
            changing=data.get('changing'),
            change_note=data.get('change_note'),
        )
        return jsonify({'ok': True})
    except Exception:
        logger.exception("Failed to update section status")
        return jsonify({'ok': False, 'error': 'Failed to update status'}), 500


# ---------------------------------------------------------------------------
# Conflict resolution (Step 6 will implement fully)
# ---------------------------------------------------------------------------

@bp.route('/projects/<project_id>/directives/resolve', methods=['POST'])
def resolve_directives(project_id):
    """Resolve a directive conflict.

    JSON body: { "conflict_id": "...", "action": "supersede|scope|keep_both" }
    """
    from ..compose.conflict_detector import resolve_conflict
    data = request.get_json(force=True, silent=True) or {}
    conflict_id = data.get('conflict_id', '').strip()
    action = data.get('action', '').strip()

    if not conflict_id or not action:
        return jsonify({'ok': False, 'error': 'conflict_id and action are required'}), 400

    if action not in ('supersede', 'scope', 'keep_both'):
        return jsonify({'ok': False, 'error': 'action must be supersede, scope, or keep_both'}), 400

    try:
        result = resolve_conflict(project_id, conflict_id, action)
        _emit('compose_directive_conflict_resolved', {
            'project_id': project_id,
            'conflict_id': conflict_id,
            'action': action,
        })
        return jsonify({'ok': True, 'result': result})
    except Exception:
        logger.exception("Failed to resolve conflict")
        return jsonify({'ok': False, 'error': 'Failed to resolve conflict'}), 500


# ---------------------------------------------------------------------------
# Changing flag (Step 7 will implement fully)
# ---------------------------------------------------------------------------

@bp.route('/projects/<project_id>/sections/<section_id>/changing', methods=['PUT'])
def update_changing(project_id, section_id):
    """Set or clear the changing flag on a section.

    JSON body: { "changing": true, "change_note": "...", "set_by": "root"|"section" }
    or: { "changing": false, "cleared_by": "section-id" }
    """
    from ..compose.context_manager import set_changing, clear_changing
    data = request.get_json(force=True, silent=True) or {}

    try:
        if data.get('changing', False):
            set_changing(
                project_id, section_id,
                change_note=data.get('change_note', ''),
                set_by=data.get('set_by', 'root'),
            )
        else:
            clear_changing(
                project_id, section_id,
                cleared_by=data.get('cleared_by', section_id),
            )

        _emit('compose_changing', {
            'project_id': project_id,
            'section_id': section_id,
            'changing': data.get('changing', False),
        })

        return jsonify({'ok': True})
    except ValueError as e:
        return jsonify({'ok': False, 'error': str(e)}), 403
    except Exception:
        logger.exception("Failed to update changing flag")
        return jsonify({'ok': False, 'error': 'Failed to update changing flag'}), 500


# ---------------------------------------------------------------------------
# Functions imported by ws_events.py
# ---------------------------------------------------------------------------

def resolve_compose_system_prompt(compose_task_id):
    """Resolve the system prompt for a compose session.

    Called by ws_events.py when a session starts with compose_task_id.
    Returns {'ok': True, 'system_prompt': '...', 'agent_role': '...'} or
    {'ok': False, 'error': '...'}.
    """
    try:
        from ..compose.prompt_builder import build_compose_prompt
        return build_compose_prompt(compose_task_id)
    except Exception as e:
        logger.exception("Failed to resolve compose prompt for %s", compose_task_id)
        return {'ok': False, 'error': str(e)}


def link_session_to_compose_task(compose_task_id, session_id):
    """Link a session to a compose task (section or root).

    Called by ws_events.py after session creation.
    """
    try:
        from ..compose.prompt_builder import link_session
        link_session(compose_task_id, session_id)
    except Exception:
        logger.exception(
            "Failed to link session %s to compose task %s",
            session_id, compose_task_id,
        )
