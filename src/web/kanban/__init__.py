"""
Kanban board business logic — state machine, defaults, and context injection.
"""

from .state_machine import transition_task, handle_session_start, handle_session_complete
from .defaults import ensure_project_columns, DEFAULT_COLUMNS
from .context_builder import build_task_context
from .ai_planner import run_planner, apply_plan, detect_verification_urls
