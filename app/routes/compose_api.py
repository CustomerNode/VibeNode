"""
Compose API — project CRUD, section management, context, conflict resolution.

Follows the same patterns as kanban_api.py: Flask blueprint, JSON responses,
SocketIO event emission for real-time updates.
"""

import logging
import uuid

from flask import Blueprint, current_app, jsonify, request

from ..compose.models import (
    ComposeProject, ComposeSection, ComposeConflict, ComposeDirective,
    SectionStatus, ConflictStatus,
    scaffold_project, scaffold_section,
    delete_project_folder, delete_section_folder,
    list_projects, get_project, save_project,
    get_sections, get_section,
    project_dir,
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
        parent = request.args.get('project', '').strip()

        # Fetch project list once — reused for both project lookup and siblings
        all_projects = None
        if project_id:
            project = get_project(project_id)
        else:
            all_projects = list_projects()
            projects = all_projects
            # Filter by parent VibeNode project if specified
            if parent:
                projects = [p for p in projects if p.parent_project == parent]
            project = projects[-1] if projects else None

        if not project:
            return jsonify(None)

        sections = get_sections(project.id)
        section_dicts = [s.to_dict() for s in sections]

        # Compute status summary
        total = len(sections)
        complete = sum(1 for s in sections if s.status == SectionStatus.COMPLETE)
        reviewing = sum(1 for s in sections if s.status == SectionStatus.REVIEWING)
        drafting = sum(1 for s in sections if s.status == SectionStatus.DRAFTING)

        # Load conflicts from context
        from ..compose.context_manager import read_context
        try:
            ctx = read_context(project.id)
            conflicts = ctx.get('conflicts', [])
        except Exception:
            conflicts = []

        # Include sibling compositions + pinned compositions for the sidebar
        # Each entry includes a status summary for sidebar indicators
        def _project_with_status(proj):
            d = proj.to_dict()
            # Read compose-context.json once for both sections and conflicts
            try:
                ctx = read_context(proj.id)
            except Exception:
                ctx = {}
            try:
                secs = [ComposeSection.from_dict(s) for s in ctx.get('sections', [])]
                t = len(secs)
                c = sum(1 for s in secs if s.status == SectionStatus.COMPLETE)
                r = sum(1 for s in secs if s.status == SectionStatus.REVIEWING)
                dr = sum(1 for s in secs if s.status == SectionStatus.DRAFTING)
                d['status'] = {
                    'total_sections': t, 'complete': c,
                    'in_progress': dr + r, 'drafting': dr, 'reviewing': r,
                }
            except Exception:
                d['status'] = {'total_sections': 0, 'complete': 0, 'in_progress': 0, 'drafting': 0, 'reviewing': 0}
            d['has_conflicts'] = any(
                cf.get('status') == 'pending' for cf in ctx.get('conflicts', [])
            )
            return d

        sibling_projects = []
        effective_parent = parent or (project.parent_project if project else None)
        if all_projects is None:
            all_projects = list_projects()
        seen_ids = set()
        if effective_parent:
            for p in all_projects:
                if p.parent_project == effective_parent:
                    sibling_projects.append(_project_with_status(p))
                    seen_ids.add(p.id)
        else:
            sibling_projects.append(_project_with_status(project))
            seen_ids.add(project.id)
        # Append pinned compositions from other projects
        for p in all_projects:
            if p.pinned and p.id not in seen_ids:
                sibling_projects.append(_project_with_status(p))
                seen_ids.add(p.id)

        return jsonify({
            'project': project.to_dict(),
            'sections': section_dicts,
            'status': {
                'total_sections': total,
                'complete': complete,
                'in_progress': drafting + reviewing,
                'drafting': drafting,
                'reviewing': reviewing,
            },
            'conflicts': conflicts,
            'sibling_projects': sibling_projects,
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

    from ..config import get_active_project
    parent_project = data.get('parent_project') or get_active_project() or None
    project = ComposeProject.create(name, parent_project=parent_project)
    pdir = scaffold_project(project)

    logger.info("Created compose project %s at %s", project.id, pdir)

    # --- NB-7: Auto-create root session ---
    try:
        from ..compose.prompt_builder import build_compose_prompt, link_session

        compose_task_id = f"root:{project.id}"
        session_id = uuid.uuid4().hex

        prompt_result = build_compose_prompt(compose_task_id)
        root_system_prompt = None
        if prompt_result.get('ok'):
            root_system_prompt = prompt_result.get('system_prompt')

        sm = current_app.session_manager
        result = sm.start_session(
            session_id=session_id,
            prompt="",
            cwd=str(pdir),
            name=name + " (root)",
            system_prompt=root_system_prompt,
        )

        if result and not (isinstance(result, dict) and result.get('error')):
            link_session(compose_task_id, session_id)
            logger.info(
                "Auto-created root session %s for project %s",
                session_id, project.id,
            )
        else:
            logger.warning(
                "Root session start returned error for project %s: %s",
                project.id, result,
            )
    except Exception:
        logger.warning(
            "Failed to auto-create root session for project %s (non-blocking)",
            project.id, exc_info=True,
        )
    # --- End NB-7 ---

    # Re-read project to include root_session_id if link_session updated it
    updated_project = get_project(project.id) or project

    _emit('compose_board_refresh', {'project_id': project.id})

    return jsonify({'ok': True, 'project': updated_project.to_dict()}), 201


@bp.route('/projects', methods=['GET'])
def list_all_projects():
    """List all composition projects.

    Optional query param ``?project=`` filters by parent_project.
    """
    projects = list_projects()
    parent = request.args.get('project', '').strip()
    if parent:
        projects = [p for p in projects if p.parent_project == parent or p.pinned]
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
    if 'position' in data:
        project.position = int(data['position'])
    if 'pinned' in data:
        project.pinned = bool(data['pinned'])

    save_project(project)
    _emit('compose_board_refresh', {'project_id': project_id})

    return jsonify({'ok': True, 'project': project.to_dict()})


@bp.route('/projects/reorder', methods=['POST'])
def reorder_projects():
    """Batch-update composition positions.

    JSON body: { "order": ["id1", "id2", ...] }
    Assigns position = index * 1000 using gap numbering.
    """
    data = request.get_json(force=True, silent=True) or {}
    order = data.get('order', [])
    if not isinstance(order, list):
        return jsonify({'ok': False, 'error': 'order must be a list'}), 400

    for i, pid in enumerate(order):
        project = get_project(pid)
        if project:
            project.position = i * 1000
            save_project(project)

    return jsonify({'ok': True})


@bp.route('/projects/<project_id>/clone', methods=['POST'])
def clone_project_endpoint(project_id):
    """Clone a composition project with new IDs.

    JSON body: { "name": "Clone Name" }  (optional — defaults to "Copy of ...")
    """
    from ..compose.models import clone_project as do_clone

    source = get_project(project_id)
    if not source:
        return jsonify({'ok': False, 'error': 'Project not found'}), 404

    data = request.get_json(force=True, silent=True) or {}
    name = (data.get('name') or '').strip() or ('Copy of ' + source.name)

    new_project = do_clone(project_id, name)
    if not new_project:
        return jsonify({'ok': False, 'error': 'Clone failed'}), 500

    logger.info("Cloned compose project %s -> %s", project_id, new_project.id)
    _emit('compose_board_refresh', {'project_id': new_project.id})

    return jsonify({'ok': True, 'project': new_project.to_dict()}), 201


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

    # Determine order
    existing = get_sections(project_id)
    insert_pos = (data.get('insert_position') or 'bottom').lower()
    if insert_pos == 'top' and existing:
        order = min((s.order for s in existing), default=0) - 1
    else:
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
    old_status = section.status.value if section.status else None

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

    section_dict = section.to_dict()
    _emit('compose_task_updated', {
        'project_id': project_id,
        'section': section_dict,
    })

    # Emit a separate move event when status changed (e.g. drag between columns)
    new_status = section.status.value if section.status else None
    if old_status and new_status and old_status != new_status:
        _emit('compose_task_moved', {
            'project_id': project_id,
            'section_id': section_id,
            'old_status': old_status,
            'new_status': new_status,
        })

    return jsonify({'ok': True, 'section': section_dict})


@bp.route('/projects/<project_id>/sections/<section_id>', methods=['DELETE'])
def delete_section(project_id, section_id):
    """Delete a section and its folder. With ?cascade=true, also deletes all descendants."""
    section = get_section(project_id, section_id)
    if not section:
        return jsonify({'ok': False, 'error': 'Section not found'}), 404

    cascade = request.args.get('cascade', 'false').lower() == 'true'
    from ..compose.context_manager import remove_section_from_context, read_context

    deleted_names = [section.name]

    if cascade:
        # Find all descendants recursively
        try:
            ctx = read_context(project_id)
            all_sections = ctx.get("sections", [])

            def _find_descendants(parent_id):
                children = [s for s in all_sections if s.get("parent_id") == parent_id]
                result = []
                for child in children:
                    result.append(child)
                    result.extend(_find_descendants(child["id"]))
                return result

            descendants = _find_descendants(section_id)
            # Delete descendants (deepest first to avoid orphan issues)
            for desc in reversed(descendants):
                try:
                    remove_section_from_context(project_id, desc["id"])
                    delete_section_folder(project_id, desc.get("name", ""))
                    deleted_names.append(desc.get("name", ""))
                except Exception:
                    logger.exception("Failed to delete descendant %s", desc.get("id"))
        except Exception:
            logger.exception("Failed to find descendants for cascade delete")

    # Delete the section itself
    try:
        remove_section_from_context(project_id, section_id)
    except Exception:
        logger.exception("Failed to remove section from context")

    delete_section_folder(project_id, section.name)

    _emit('compose_board_refresh', {'project_id': project_id})

    return jsonify({'ok': True, 'deleted_count': len(deleted_names), 'deleted_sections': deleted_names})


@bp.route('/projects/<project_id>/sections/<section_id>/launch', methods=['POST'])
def launch_section_session(project_id, section_id):
    """Create and link a new session for a section.

    Used by "Launch All" to programmatically start section agents
    without opening the session spawner UI.
    """
    project = get_project(project_id)
    if not project:
        return jsonify({'ok': False, 'error': 'Project not found'}), 404

    section = get_section(project_id, section_id)
    if not section:
        return jsonify({'ok': False, 'error': 'Section not found'}), 404

    if section.session_id:
        return jsonify({'ok': False, 'error': 'Section already has a session', 'session_id': section.session_id}), 409

    try:
        from ..compose.prompt_builder import build_compose_prompt, link_session
        from ..compose.context_manager import update_section_in_context

        compose_task_id = f"section:{project_id}:{section_id}"
        session_id = uuid.uuid4().hex

        prompt_result = build_compose_prompt(compose_task_id)
        system_prompt = prompt_result.get('system_prompt') if prompt_result.get('ok') else None

        sm = current_app.session_manager
        result = sm.start_session(
            session_id=session_id,
            prompt="",
            cwd=str(project_dir(project_id)),
            name=f"{section.name} ({project.name})",
            system_prompt=system_prompt,
        )

        if result and not (isinstance(result, dict) and result.get('error')):
            link_session(compose_task_id, session_id)
            section.session_id = session_id
            update_section_in_context(project_id, section)
            _emit('compose_task_updated', {
                'project_id': project_id,
                'section': section.to_dict(),
            })
            return jsonify({'ok': True, 'session_id': session_id, 'section': section.to_dict()})
        else:
            return jsonify({'ok': False, 'error': 'Session daemon did not respond', 'detail': str(result)}), 503
    except Exception:
        logger.exception("Failed to launch session for section %s", section_id)
        return jsonify({'ok': False, 'error': 'Internal error'}), 500


@bp.route('/projects/<project_id>/sections/add-and-link', methods=['POST'])
def add_and_link_section(project_id):
    """Atomic create-section + link-session.

    JSON body: {
        "name": "Section Name",
        "session_id": "sess-abc",
        "parent_id": null,          // optional
        "order": null,              // optional — defaults to end of siblings
        "artifact_type": null       // optional
    }

    Creates the section and sets session_id in one operation.
    If anything fails, neither change persists.
    """
    project = get_project(project_id)
    if not project:
        return jsonify({'ok': False, 'error': 'Project not found'}), 404

    data = request.get_json(force=True, silent=True) or {}
    name = (data.get('name') or '').strip()
    session_id = (data.get('session_id') or '').strip()
    if not name:
        return jsonify({'ok': False, 'error': 'name is required'}), 400
    if not session_id:
        return jsonify({'ok': False, 'error': 'session_id is required'}), 400

    parent_id = data.get('parent_id')

    # Determine order among siblings
    existing = get_sections(project_id)
    siblings = [s for s in existing if s.parent_id == parent_id]
    if data.get('order') is not None:
        order = data['order']
    else:
        order = max((s.order for s in siblings), default=-1) + 1

    # Guard: check if session is already linked to a section in this project
    for s in existing:
        if s.session_id == session_id:
            return jsonify({
                'ok': False,
                'error': 'Session already linked',
                'linked_section': s.name,
                'linked_section_id': s.id,
            }), 409

    section = ComposeSection.create(
        project_id=project_id,
        name=name,
        parent_id=parent_id,
        order=order,
        artifact_type=data.get('artifact_type'),
    )
    section.session_id = session_id

    # Scaffold folder, then write to context. If context write fails,
    # clean up the folder so we don't leak orphan directories.
    scaffold_section(project_id, section)

    from ..compose.context_manager import add_section_to_context
    try:
        add_section_to_context(project_id, section)
    except Exception:
        logger.exception("add-and-link: failed to add section to context")
        delete_section_folder(project_id, section.name)
        return jsonify({'ok': False, 'error': 'Failed to save section'}), 500

    _emit('compose_task_created', {
        'project_id': project_id,
        'section': section.to_dict(),
    })

    return jsonify({'ok': True, 'section': section.to_dict()}), 201


@bp.route('/projects/<project_id>/sections/<section_id>/preview', methods=['GET'])
def preview_section(project_id, section_id):
    """Return the source file contents for a section's output preview."""
    section = get_section(project_id, section_id)
    if not section:
        return jsonify({'ok': False, 'error': 'Section not found'}), 404

    from pathlib import Path
    section_dir = project_dir(project_id) / "sections" / section.name / "content"
    files = []
    if section_dir.is_dir():
        for fp in sorted(section_dir.iterdir()):
            if fp.is_file() and fp.suffix in ('.md', '.csv', '.yaml', '.yml', '.json', '.html', '.mmd', '.puml', '.txt'):
                try:
                    content = fp.read_text(encoding='utf-8')
                    files.append({
                        'name': fp.name,
                        'type': fp.suffix.lstrip('.'),
                        'content': content[:50000],  # Cap at 50KB per file
                    })
                except Exception:
                    files.append({'name': fp.name, 'type': fp.suffix.lstrip('.'), 'content': '', 'error': 'Could not read'})

    return jsonify({'ok': True, 'files': files})


@bp.route('/projects/<project_id>/sections/<section_id>/children', methods=['GET'])
def list_section_children(project_id, section_id):
    """List all descendant sections for cascade delete confirmation."""
    from ..compose.context_manager import read_context

    try:
        ctx = read_context(project_id)
        all_sections = ctx.get("sections", [])

        def _find_descendants(parent_id):
            children = [s for s in all_sections if s.get("parent_id") == parent_id]
            result = []
            for child in children:
                result.append({'id': child['id'], 'name': child.get('name', ''), 'status': child.get('status', '')})
                result.extend(_find_descendants(child['id']))
            return result

        descendants = _find_descendants(section_id)
        return jsonify({'ok': True, 'children': descendants, 'count': len(descendants)})
    except Exception:
        return jsonify({'ok': True, 'children': [], 'count': 0})


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

    JSON body: { "status": "drafting", "summary": "...", "changing": false, "change_note": "..." }
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
# Directives
# ---------------------------------------------------------------------------

@bp.route('/projects/<project_id>/directives', methods=['POST'])
def create_directive(project_id):
    """Add a new directive and run conflict detection.

    JSON body: { "content": "...", "scope": "global"|section_id, "source": "user" }

    Emits compose_directive_logged with the directive and any conflicts found.
    """
    from ..compose.context_manager import add_directive
    from ..compose.conflict_detector import detect_conflicts

    project = get_project(project_id)
    if not project:
        return jsonify({'ok': False, 'error': 'Project not found'}), 404

    data = request.get_json(force=True, silent=True) or {}
    content = (data.get('content') or '').strip()
    if not content:
        return jsonify({'ok': False, 'error': 'content is required'}), 400

    scope = data.get('scope', 'global')
    source = data.get('source', 'user')

    directive = ComposeDirective.create(scope=scope, content=content, source=source)
    directive = add_directive(project_id, directive)

    # Run conflict detection against existing directives
    conflicts = detect_conflicts(project_id, directive)

    # Build conflict payload for the frontend
    conflict_payloads = []
    for c in conflicts:
        conflict_payloads.append({
            'id': c.id,
            'classification': 'ambiguous',
            'existing_id': c.directive_a_id,
            'existing_text': c.directive_a_content,
            'recommendation': c.recommendation,
        })

    # Emit socket event so the frontend can show conflict cards
    _emit('compose_directive_logged', {
        'project_id': project_id,
        'directive': directive.to_dict(),
        'conflicts': conflict_payloads,
    })

    return jsonify({
        'ok': True,
        'directive': directive.to_dict(),
        'conflicts': [c.to_dict() for c in conflicts],
    }), 201


# ---------------------------------------------------------------------------
# Conflict resolution
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
# Planner accept — create sections from AI-proposed plan
# ---------------------------------------------------------------------------

@bp.route('/projects/<project_id>/planner/accept', methods=['POST'])
def accept_compose_plan(project_id):
    """Accept an AI-proposed content plan and create sections.

    JSON body: {
        "sections": [
            {
                "name": "Section Name",
                "artifact_type": "text",
                "subsections": [
                    {"name": "Sub Name", "artifact_type": "data"}
                ]
            }
        ],
        "parent_id": null  // optional — scope plan under a parent section
    }
    """
    project = get_project(project_id)
    if not project:
        return jsonify({'ok': False, 'error': 'Project not found'}), 404

    data = request.get_json(force=True, silent=True) or {}
    plan_sections = data.get('sections', [])
    scope_parent_id = data.get('parent_id') or None

    if not plan_sections:
        return jsonify({'ok': False, 'error': 'No sections in plan'}), 400

    from ..compose.context_manager import add_section_to_context

    created = []
    order_base = len(get_sections(project_id))

    def _create_recursive(items, parent_id, order_start):
        nonlocal created
        for i, item in enumerate(items):
            name = (item.get('name') or '').strip()
            if not name:
                continue
            artifact = item.get('artifact_type', 'text')
            section = ComposeSection.create(
                project_id=project_id,
                name=name,
                parent_id=parent_id,
                order=order_start + i,
                artifact_type=artifact,
            )
            # Set brief as summary if provided by planner
            brief = (item.get('brief') or '').strip()
            if brief:
                section.summary = brief

            scaffold_section(project_id, section)

            # Write brief to section folder if provided
            if brief:
                try:
                    brief_path = project_dir(project_id) / "sections" / section.name / "brief.md"
                    brief_path.write_text(brief, encoding='utf-8')
                except Exception:
                    logger.warning("Failed to write brief for section %s", name)

            try:
                add_section_to_context(project_id, section)
                created.append(section)
            except Exception:
                logger.exception("Failed to add planned section %s", name)

            # Recurse into subsections
            subs = item.get('subsections', [])
            if subs:
                _create_recursive(subs, section.id, 0)

    _create_recursive(plan_sections, scope_parent_id, order_base)

    _emit('compose_board_refresh', {'project_id': project_id})

    return jsonify({
        'ok': True,
        'created_count': len(created),
        'sections': [s.to_dict() for s in created],
    })


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
