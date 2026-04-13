"""Page Object for the Kanban board view."""

import json
from selenium.webdriver.common.by import By
from selenium.webdriver.common.keys import Keys
from selenium.webdriver.common.action_chains import ActionChains
from .base_page import BasePage


class KanbanPage(BasePage):
    """Page object for the Kanban board, task cards, and drill-down."""

    # --- Locators ---
    COLUMNS_WRAPPER = (By.CSS_SELECTOR, ".kanban-columns-wrapper")
    EMPTY_STATE = (By.CSS_SELECTOR, ".kanban-empty-state")
    TASK_CARDS = (By.CSS_SELECTOR, ".task-card")
    COLUMN_HEADERS = (By.CSS_SELECTOR, ".kanban-col-header")
    DRILL_TITLEBAR = (By.CSS_SELECTOR, ".kanban-drill-titlebar")
    DRILL_TITLE = (By.CSS_SELECTOR, ".kanban-drill-title")
    DRILL_SUBTASK_ROW = (By.CSS_SELECTOR, ".kanban-drill-subtask-row")
    DRILL_BACK_BTN = (By.CSS_SELECTOR, ".kanban-drill-back")
    BREADCRUMB = (By.CSS_SELECTOR, ".kanban-breadcrumb")
    DETAIL_PANEL = (By.CSS_SELECTOR, ".kanban-detail-panel")
    CONTEXT_MENU = (By.CSS_SELECTOR, ".kanban-context-menu")
    NEW_TASK_POPUP = (By.CSS_SELECTOR, ".kanban-new-task-popup")
    SETTINGS_OVERLAY = (By.ID, "pm-overlay")

    # --- Actions ---

    def switch_to_kanban(self):
        """Switch to kanban view mode and wait for board to render."""
        self.wait_js('typeof setViewMode === "function"')
        self.js('setViewMode("kanban")')
        self.wait_for_board()

    def wait_for_board(self, timeout=None):
        """Wait for kanban board columns or empty state to render."""
        timeout = timeout or self.DEFAULT_TIMEOUT
        from selenium.webdriver.support.ui import WebDriverWait
        WebDriverWait(self.driver, timeout).until(
            lambda d: d.execute_script(
                'return document.querySelector(".kanban-columns-wrapper") !== null '
                '|| document.querySelector(".kanban-empty-state") !== null'
            )
        )

    def create_task_via_api(self, title, status="not_started", parent_id=None):
        """Create a task via fetch API, track it for cleanup, return the task ID."""
        body = {"title": title, "status": status}
        if parent_id:
            body["parent_id"] = parent_id
        result = self.js('''
            return fetch("/api/kanban/tasks", {
                method: "POST",
                headers: {"Content-Type": "application/json"},
                body: arguments[0]
            }).then(r => r.json()).then(d => {
                (window.__test_created_ids = window.__test_created_ids || []).push(d.id);
                return d.id;
            });
        ''', json.dumps(body))
        return result

    def delete_task_via_api(self, task_id):
        """Delete a task via fetch API."""
        self.js(f'fetch("/api/kanban/tasks/{task_id}", {{method: "DELETE"}})')

    def navigate_to_task(self, task_id):
        """Drill down into a task by ID."""
        self.js(f'navigateToTask("{task_id}")')
        self.wait_for(*self.DRILL_TITLEBAR)

    def click_drill_back(self):
        """Click the back button in drill-down view."""
        self.wait_clickable(*self.DRILL_BACK_BTN).click()

    def refresh_board(self):
        """Re-initialize the kanban board."""
        self.js('initKanban(true)')
        self.wait_for_board()

    def open_new_task_popup(self):
        """Open the new task popup."""
        self.js('openNewTaskPopup()')

    def cleanup_test_tasks(self):
        """Delete all tasks tracked during the test."""
        self.js('''
            var ids = window.__test_created_ids || [];
            Promise.all(ids.map(function(id) {
                return fetch("/api/kanban/tasks/" + id, {method: "DELETE"});
            })).then(function() { window.__test_created_ids = []; });
        ''')

    def init_task_tracking(self):
        """Initialize the test task ID tracking array."""
        self.js('window.__test_created_ids = [];')

    # --- Queries ---

    def is_board_visible(self):
        """Check if the kanban board columns are visible."""
        return bool(self.driver.find_elements(*self.COLUMNS_WRAPPER))

    def is_empty_state(self):
        """Check if the kanban empty state is shown."""
        return bool(self.driver.find_elements(*self.EMPTY_STATE))

    def is_drill_view_visible(self):
        """Check if the drill-down view is visible."""
        return bool(self.driver.find_elements(*self.DRILL_TITLEBAR))

    def get_drill_title_text(self):
        """Return the text of the drill-down title."""
        els = self.driver.find_elements(*self.DRILL_TITLE)
        return els[0].text if els else ""

    def get_task_card_count(self):
        """Return the number of visible task cards."""
        return len(self.driver.find_elements(*self.TASK_CARDS))

    def get_breadcrumb_text(self):
        """Return the breadcrumb text."""
        els = self.driver.find_elements(*self.BREADCRUMB)
        return els[0].text if els else ""
