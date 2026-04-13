"""Page Object Model for VibeNode E2E tests.

Each page object encapsulates the locators and actions for one major
UI area, decoupling tests from CSS selectors and DOM structure.
"""

from .base_page import BasePage
from .session_page import SessionPage
from .kanban_page import KanbanPage
from .workforce_page import WorkforcePage
from .planner_page import PlannerPage
from .session_manage_page import SessionManagePage
from .sidebar_page import SidebarPage

__all__ = [
    "BasePage",
    "SessionPage",
    "KanbanPage",
    "WorkforcePage",
    "PlannerPage",
    "SessionManagePage",
    "SidebarPage",
]
