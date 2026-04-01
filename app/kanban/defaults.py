"""
Default Kanban board column configuration.

Provides the standard five-column layout and ensures columns exist for a
given project before the board is first rendered.
"""

from ..db.repository import KanbanRepository


DEFAULT_COLUMNS = [
    {
        'name': 'Not Started',
        'status_key': 'not_started',
        'position': 0,
        'color': '#8b949e',
        'sort_mode': 'manual',
        'sort_direction': 'asc',
    },
    {
        'name': 'Working',
        'status_key': 'working',
        'position': 1,
        'color': '#58a6ff',
        'sort_mode': 'manual',
        'sort_direction': 'asc',
    },
    {
        'name': 'Validating',
        'status_key': 'validating',
        'position': 2,
        'color': '#d29922',
        'sort_mode': 'last_updated',
        'sort_direction': 'desc',
    },
    {
        'name': 'Remediating',
        'status_key': 'remediating',
        'position': 3,
        'color': '#f85149',
        'sort_mode': 'last_updated',
        'sort_direction': 'desc',
    },
    {
        'name': 'Complete',
        'status_key': 'complete',
        'position': 4,
        'color': '#3fb950',
        'sort_mode': 'last_updated',
        'sort_direction': 'desc',
    },
]


_ensured_projects = set()  # in-memory cache: skip get_columns after first check


def ensure_project_columns(repo, project_id):
    """Create default columns for a project if none exist yet.

    This is called before rendering the board to guarantee the standard
    five-column layout is present. If the project already has columns
    configured, this is a no-op.  After the first successful check the
    result is cached in-memory so subsequent calls are free.

    Args:
        repo: KanbanRepository instance.
        project_id: The encoded project path string.

    Returns:
        List of column dicts for the project.
    """
    if project_id in _ensured_projects:
        return None  # columns already confirmed, skip the query
    existing = repo.get_columns(project_id)
    if existing:
        # Migrate: auto-sort columns that are still on manual
        _AUTO_SORT = {
            'complete': ('last_updated', 'desc'),
            'validating': ('last_updated', 'desc'),
            'remediating': ('last_updated', 'desc'),
        }
        needs_update = False
        patched = []
        for col in existing:
            d = col.to_dict() if hasattr(col, 'to_dict') else dict(col)
            key = d.get('status_key')
            mode = d.get('sort_mode', 'manual')
            if key in _AUTO_SORT and mode in ('manual', 'date_entered'):
                target_mode, target_dir = _AUTO_SORT[key]
                d['sort_mode'] = target_mode
                d['sort_direction'] = target_dir
                needs_update = True
            patched.append(d)
        if needs_update:
            try:
                repo.update_columns(project_id, patched)
                _ensured_projects.add(project_id)
                return repo.get_columns(project_id)
            except Exception:
                pass
        _ensured_projects.add(project_id)
        return existing

    for col_def in DEFAULT_COLUMNS:
        repo.create_column(
            project_id=project_id,
            name=col_def['name'],
            status_key=col_def['status_key'],
            position=col_def['position'],
            color=col_def['color'],
            sort_mode=col_def['sort_mode'],
            sort_direction=col_def['sort_direction'],
        )

    _ensured_projects.add(project_id)
    return repo.get_columns(project_id)
