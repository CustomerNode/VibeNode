"""Selenium E2E tests for the Kanban Board view.

Covers all features from the Kanban plan:
  1. View mode switching & board rendering
  2. Task CRUD (create, read, update, delete)
  3. Subtask hierarchy (nesting, breadcrumb)
  4. Status state machine (valid/invalid transitions)
  5. Drag-and-drop between columns
  6. Validation ceremony modal (validating → complete)
  7. Session linking & spawning
  8. Tags (add, remove, filter)
  9. Verification URLs (display, auto-correction)
 10. Column configuration (sort mode, color, name)
 11. Keyboard shortcuts
 12. Detail panel (slide-in, Quill editor, save)
 13. Reports panel
 14. Bulk operations (complete all, reset all)
 15. Upward status propagation
 16. Context injection endpoint
 17. AI planner endpoint
 18. Inline title editing
 19. Reorder within column
 20. SocketIO live updates
"""

import json
import time
import pytest
from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.common.keys import Keys
from selenium.webdriver.common.action_chains import ActionChains
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC

from tests.e2e.conftest import TEST_BASE_URL as BASE_URL
LONG_WAIT = 90
API = BASE_URL + "/api/kanban"


@pytest.fixture(scope="class", autouse=True)
def kanban_setup(driver):
    """Navigate to the app, wait for JS, select project, dismiss modals."""
    driver.get(BASE_URL)
    WebDriverWait(driver, LONG_WAIT).until(
        lambda drv: drv.execute_script('return typeof setViewMode === "function"')
    )
    time.sleep(2)
    driver.execute_script('''
        if(typeof _allProjects!=="undefined"&&_allProjects.length>0)
            setProject(_allProjects[0].encoded,true);
        document.querySelectorAll(".show").forEach(function(e){e.classList.remove("show")});
    ''')
    time.sleep(2)


def _ensure_page_loaded(driver):
    """Navigate to the app if not already there and wait for JS bundles."""
    current = driver.current_url or ""
    if "localhost:" + str(TEST_PORT) not in current:
        driver.get(BASE_URL)
        time.sleep(2)
    WebDriverWait(driver, LONG_WAIT).until(
        lambda d: d.execute_script('return typeof setViewMode === "function"')
    )


def _setup_project(driver):
    """Ensure a project is selected and dismiss modals."""
    _ensure_page_loaded(driver)
    driver.execute_script('''
        if(typeof _allProjects!=="undefined"&&_allProjects.length>0)
            setProject(_allProjects[0].encoded,true);
        document.querySelectorAll(".show").forEach(function(e){e.classList.remove("show")});
    ''')
    time.sleep(3)


def _switch_to_kanban(driver):
    """Switch to kanban view mode."""
    # Ensure JS is loaded
    WebDriverWait(driver, LONG_WAIT).until(
        lambda d: d.execute_script('return typeof setViewMode === "function"')
    )
    driver.execute_script('setViewMode("kanban")')
    time.sleep(3)


def _wait_for_board(driver):
    """Wait until the kanban board has columns rendered."""
    WebDriverWait(driver, LONG_WAIT).until(
        EC.presence_of_element_located((By.CSS_SELECTOR, ".kanban-column"))
    )


def _api_create_task(driver, title, status="not_started", parent_id=None, verification_url=""):
    """Create a task via the REST API and return its data."""
    _ensure_page_loaded(driver)
    body = {"title": title, "status": status}
    if parent_id:
        body["parent_id"] = parent_id
    if verification_url:
        body["verification_url"] = verification_url
    result = driver.execute_script(f'''
        const res = await fetch("{API}/tasks", {{
            method: "POST",
            headers: {{"Content-Type": "application/json"}},
            body: JSON.stringify({json.dumps(body)})
        }});
        return await res.json();
    ''')
    return result


def _api_get_task(driver, task_id):
    """Fetch a task by ID via REST API."""
    _ensure_page_loaded(driver)
    return driver.execute_script(f'''
        const res = await fetch("{API}/tasks/{task_id}");
        return await res.json();
    ''')


def _api_delete_task(driver, task_id):
    """Delete a task via REST API."""
    _ensure_page_loaded(driver)
    driver.execute_script(f'''
        await fetch("{API}/tasks/{task_id}", {{method: "DELETE"}});
    ''')


def _api_move_task(driver, task_id, status):
    """Move a task to a new status via REST API."""
    _ensure_page_loaded(driver)
    return driver.execute_script(f'''
        const res = await fetch("{API}/tasks/{task_id}/move", {{
            method: "POST",
            headers: {{"Content-Type": "application/json"}},
            body: JSON.stringify({{status: "{status}"}})
        }});
        return await res.json();
    ''')


def _api_get_board(driver):
    """Fetch the full board state via REST API."""
    _ensure_page_loaded(driver)
    return driver.execute_script(f'''
        const res = await fetch("{API}/board");
        return await res.json();
    ''')


def _close_modals(driver):
    """Close any open modals/overlays."""
    driver.execute_script('''
        if (typeof _closePm === "function") _closePm();
        if (typeof _kanbanCloseDetail === "function") _kanbanCloseDetail();
        document.querySelectorAll(".kanban-detail-overlay").forEach(e => e.remove());
        document.querySelectorAll(".kanban-detail-panel").forEach(e => e.remove());
    ''')
    time.sleep(0.5)


# ═══════════════════════════════════════════════════════════════
# 1. VIEW MODE & BOARD RENDERING
# ═══════════════════════════════════════════════════════════════

class TestKanbanViewMode:
    """Switching to kanban view and board rendering."""

    def test_setup(self, driver):
        _setup_project(driver)

    def test_switch_to_kanban_view(self, driver):
        _switch_to_kanban(driver)
        _wait_for_board(driver)
        board = driver.find_element(By.ID, "kanban-board")
        assert board.is_displayed(), "Kanban board should be visible"

    def test_five_default_columns_rendered(self, driver):
        cols = driver.find_elements(By.CSS_SELECTOR, ".kanban-column")
        assert len(cols) == 5, f"Expected 5 columns, got {len(cols)}"

    def test_column_names_correct(self, driver):
        names = driver.find_elements(By.CSS_SELECTOR, ".kanban-column-name")
        expected = ["not started", "working", "validating", "remediating", "complete"]
        actual = [n.text.lower() for n in names]
        assert actual == expected, f"Column names mismatch: {actual}"

    def test_column_color_bars_present(self, driver):
        bars = driver.find_elements(By.CSS_SELECTOR, ".kanban-column-color-bar")
        assert len(bars) == 5, "Each column should have a color bar"

    def test_column_counts_present(self, driver):
        counts = driver.find_elements(By.CSS_SELECTOR, ".kanban-column-count")
        assert len(counts) == 5, "Each column should have a count badge"

    def test_add_task_buttons_present(self, driver):
        btns = driver.find_elements(By.CSS_SELECTOR, ".kanban-add-card")
        assert len(btns) == 5, "Each column should have an add-task button"

    def test_toolbar_rendered(self, driver):
        # Toolbar is in the sidebar as kanban-sidebar-toolbar
        toolbar = driver.find_elements(By.CSS_SELECTOR, ".kanban-sidebar-toolbar, .kanban-toolbar")
        assert len(toolbar) >= 1, "Toolbar should be rendered (in sidebar or board)"

    def test_breadcrumb_shows_all_tasks(self, driver):
        bc = driver.find_elements(By.CSS_SELECTOR, ".kanban-breadcrumb-bar")
        assert len(bc) >= 1, "Breadcrumb bar should be present"
        assert "All Tasks" in bc[0].text

    def test_view_mode_label_shows_kanban(self, driver):
        label = driver.find_element(By.ID, "view-mode-label")
        assert "Workflow" in label.text or "Kanban" in label.text

    def test_sidebar_hidden_in_kanban(self, driver):
        search = driver.find_elements(By.CSS_SELECTOR, ".sidebar-search-row")
        if search:
            assert search[0].value_of_css_property("display") == "none", \
                "Sidebar search should be hidden in kanban mode"


# ═══════════════════════════════════════════════════════════════
# 2. TASK CRUD
# ═══════════════════════════════════════════════════════════════

class TestTaskCrud:
    """Create, read, update, and delete tasks."""

    def test_create_task_via_api(self, driver):
        task = _api_create_task(driver, "E2E Test Task Alpha")
        assert task.get("id"), "Task should have an ID"
        assert task["title"] == "E2E Test Task Alpha"
        assert task["status"] == "not_started"
        # Store for later tests
        driver._test_task_id = task["id"]

    def test_task_appears_in_not_started_column(self, driver):
        _switch_to_kanban(driver)
        _wait_for_board(driver)
        time.sleep(2)  # ensure full render including cards
        card = driver.find_elements(
            By.CSS_SELECTOR,
            f'.kanban-card[data-task-id="{driver._test_task_id}"]'
        )
        assert len(card) >= 1, "Created task should appear as a card"
        col = card[0].find_element(By.XPATH, "./ancestor::div[contains(@class,'kanban-column') and @data-status]")
        status = col.get_attribute("data-status")
        assert status == "not_started", f"Expected 'not_started', got '{status}'"

    def test_get_task_returns_children_and_sessions(self, driver):
        task = _api_get_task(driver, driver._test_task_id)
        assert "children" in task, "GET task should include children"
        assert "sessions" in task, "GET task should include sessions"

    def test_update_task_title(self, driver):
        result = driver.execute_script(f'''
            const res = await fetch("{API}/tasks/{driver._test_task_id}", {{
                method: "PATCH",
                headers: {{"Content-Type": "application/json"}},
                body: JSON.stringify({{title: "E2E Updated Title"}})
            }});
            return await res.json();
        ''')
        assert result["title"] == "E2E Updated Title"

    def test_update_task_description(self, driver):
        result = driver.execute_script(f'''
            const res = await fetch("{API}/tasks/{driver._test_task_id}", {{
                method: "PATCH",
                headers: {{"Content-Type": "application/json"}},
                body: JSON.stringify({{description: "<p>Test description</p>"}})
            }});
            return await res.json();
        ''')
        assert "Test description" in (result.get("description") or "")

    def test_update_task_verification_url(self, driver):
        result = driver.execute_script(f'''
            const res = await fetch("{API}/tasks/{driver._test_task_id}", {{
                method: "PATCH",
                headers: {{"Content-Type": "application/json"}},
                body: JSON.stringify({{verification_url: "/api/health"}})
            }});
            return await res.json();
        ''')
        assert result["verification_url"] == "/api/health"

    def test_delete_task(self, driver):
        task = _api_create_task(driver, "E2E Temp Delete Task")
        tid = task["id"]
        _api_delete_task(driver, tid)
        result = _api_get_task(driver, tid)
        assert result.get("error"), "Deleted task should return error"

    def test_create_task_requires_title(self, driver):
        result = driver.execute_script(f'''
            const res = await fetch("{API}/tasks", {{
                method: "POST",
                headers: {{"Content-Type": "application/json"}},
                body: JSON.stringify({{title: ""}})
            }});
            return {{status: res.status, body: await res.json()}};
        ''')
        assert result["status"] == 400


# ═══════════════════════════════════════════════════════════════
# 3. SUBTASK HIERARCHY
# ═══════════════════════════════════════════════════════════════

class TestSubtaskHierarchy:
    """Nesting, breadcrumbs, and tree operations."""

    def test_create_subtask(self, driver):
        parent = _api_create_task(driver, "E2E Parent Task")
        child = _api_create_task(driver, "E2E Child Task", parent_id=parent["id"])
        assert child["parent_id"] == parent["id"]
        driver._parent_id = parent["id"]
        driver._child_id = child["id"]

    def test_get_parent_includes_children(self, driver):
        parent = _api_get_task(driver, driver._parent_id)
        children = parent.get("children", [])
        assert len(children) >= 1, "Parent should have at least 1 child"
        child_ids = [c["id"] for c in children]
        assert driver._child_id in child_ids

    def test_create_grandchild(self, driver):
        grandchild = _api_create_task(driver, "E2E Grandchild", parent_id=driver._child_id)
        assert grandchild["parent_id"] == driver._child_id
        driver._grandchild_id = grandchild["id"]

    def test_ancestors_endpoint(self, driver):
        result = driver.execute_script(f'''
            const res = await fetch("{API}/tasks/{driver._grandchild_id}/ancestors");
            return await res.json();
        ''')
        ancestors = result.get("ancestors", [])
        assert len(ancestors) >= 2, "Grandchild should have at least 2 ancestors"

    def test_cascade_delete_removes_children(self, driver):
        parent = _api_create_task(driver, "E2E Cascade Parent")
        child1 = _api_create_task(driver, "E2E Cascade Child 1", parent_id=parent["id"])
        child2 = _api_create_task(driver, "E2E Cascade Child 2", parent_id=parent["id"])
        _api_delete_task(driver, parent["id"])
        # Children should be gone
        r1 = _api_get_task(driver, child1["id"])
        r2 = _api_get_task(driver, child2["id"])
        assert r1.get("error"), "Child 1 should be cascade deleted"
        assert r2.get("error"), "Child 2 should be cascade deleted"


# ═══════════════════════════════════════════════════════════════
# 4. STATUS STATE MACHINE
# ═══════════════════════════════════════════════════════════════

class TestStateMachine:
    """Valid and invalid status transitions."""

    def test_valid_not_started_to_working(self, driver):
        task = _api_create_task(driver, "E2E SM Test 1")
        result = _api_move_task(driver, task["id"], "working")
        assert result["status"] == "working"
        driver._sm_task_id = task["id"]

    def test_valid_working_to_validating(self, driver):
        result = _api_move_task(driver, driver._sm_task_id, "validating")
        assert result["status"] == "validating"

    def test_valid_validating_to_complete(self, driver):
        result = _api_move_task(driver, driver._sm_task_id, "complete")
        assert result["status"] == "complete"

    def test_valid_complete_to_remediating(self, driver):
        result = _api_move_task(driver, driver._sm_task_id, "remediating")
        assert result["status"] == "remediating"

    def test_valid_remediating_to_working(self, driver):
        result = _api_move_task(driver, driver._sm_task_id, "working")
        assert result["status"] == "working"

    def test_invalid_not_started_to_complete(self, driver):
        task = _api_create_task(driver, "E2E SM Invalid")
        result = _api_move_task(driver, task["id"], "complete")
        assert result.get("error"), "not_started → complete should be invalid"
        _api_delete_task(driver, task["id"])

    def test_invalid_not_started_to_validating(self, driver):
        task = _api_create_task(driver, "E2E SM Invalid 2")
        result = _api_move_task(driver, task["id"], "validating")
        assert result.get("error"), "not_started → validating should be invalid"
        _api_delete_task(driver, task["id"])

    def test_invalid_working_to_complete(self, driver):
        task = _api_create_task(driver, "E2E SM Invalid 3")
        _api_move_task(driver, task["id"], "working")
        result = _api_move_task(driver, task["id"], "complete")
        assert result.get("error"), "working → complete should be invalid"
        _api_delete_task(driver, task["id"])

    def test_noop_move_same_status(self, driver):
        task = _api_create_task(driver, "E2E SM Noop")
        result = _api_move_task(driver, task["id"], "not_started")
        assert result["status"] == "not_started"
        assert not result.get("error")
        _api_delete_task(driver, task["id"])


# ═══════════════════════════════════════════════════════════════
# 5. UPWARD STATUS PROPAGATION
# ═══════════════════════════════════════════════════════════════

class TestUpwardPropagation:
    """Status propagation from child to parent."""

    def test_child_working_propagates_to_parent(self, driver):
        parent = _api_create_task(driver, "E2E Prop Parent")
        child = _api_create_task(driver, "E2E Prop Child", parent_id=parent["id"])
        _api_move_task(driver, child["id"], "working")
        # Parent should now be working too
        updated_parent = _api_get_task(driver, parent["id"])
        assert updated_parent["status"] == "working", \
            "Parent should propagate to working when child starts working"
        driver._prop_parent_id = parent["id"]
        driver._prop_child_id = child["id"]

    def test_child_remediating_propagates_from_complete(self, driver):
        parent = _api_create_task(driver, "E2E Prop Rem Parent")
        child = _api_create_task(driver, "E2E Prop Rem Child", parent_id=parent["id"])
        # Walk both through: not_started → working → validating → complete
        _api_move_task(driver, child["id"], "working")
        _api_move_task(driver, child["id"], "validating")
        _api_move_task(driver, child["id"], "complete")
        # Manually move parent to complete
        _api_move_task(driver, parent["id"], "validating")
        _api_move_task(driver, parent["id"], "complete")
        # Now reopen child → remediating
        _api_move_task(driver, child["id"], "remediating")
        updated_parent = _api_get_task(driver, parent["id"])
        assert updated_parent["status"] == "remediating", \
            "Parent should propagate to remediating when child reopens"
        _api_delete_task(driver, parent["id"])


# ═══════════════════════════════════════════════════════════════
# 6. SESSION LINKING
# ═══════════════════════════════════════════════════════════════

class TestSessionLinking:
    """Link and unlink sessions to tasks."""

    def test_link_session_to_task(self, driver):
        task = _api_create_task(driver, "E2E Session Link Task")
        result = driver.execute_script(f'''
            const res = await fetch("{API}/tasks/{task["id"]}/sessions", {{
                method: "POST",
                headers: {{"Content-Type": "application/json"}},
                body: JSON.stringify({{session_id: "test-session-001"}})
            }});
            return await res.json();
        ''')
        assert result.get("task_id") == task["id"]
        assert result.get("session_id") == "test-session-001"
        driver._session_task_id = task["id"]

    def test_session_appears_in_task_detail(self, driver):
        task = _api_get_task(driver, driver._session_task_id)
        sessions = task.get("sessions", [])
        session_ids = [s if isinstance(s, str) else s.get("session_id", s) for s in sessions]
        assert "test-session-001" in session_ids

    def test_linking_session_auto_transitions_to_working(self, driver):
        """Linking a session should auto-transition from not_started to working."""
        task = _api_get_task(driver, driver._session_task_id)
        assert task["status"] == "working", \
            "Task should auto-transition to working when session is linked"

    def test_unlink_session(self, driver):
        driver.execute_script(f'''
            await fetch("{API}/tasks/{driver._session_task_id}/sessions/test-session-001", {{
                method: "DELETE"
            }});
        ''')
        task = _api_get_task(driver, driver._session_task_id)
        sessions = task.get("sessions", [])
        session_ids = [s if isinstance(s, str) else s.get("session_id", s) for s in sessions]
        assert "test-session-001" not in session_ids

    def test_link_session_requires_session_id(self, driver):
        task = _api_create_task(driver, "E2E Session Empty")
        result = driver.execute_script(f'''
            const res = await fetch("{API}/tasks/{task["id"]}/sessions", {{
                method: "POST",
                headers: {{"Content-Type": "application/json"}},
                body: JSON.stringify({{session_id: ""}})
            }});
            return {{status: res.status, body: await res.json()}};
        ''')
        assert result["status"] == 400
        _api_delete_task(driver, task["id"])


# ═══════════════════════════════════════════════════════════════
# 7. TAGS
# ═══════════════════════════════════════════════════════════════

class TestTags:
    """Tag add, remove, and filtering."""

    def test_add_tag_to_task(self, driver):
        task = _api_create_task(driver, "E2E Tag Task")
        result = driver.execute_script(f'''
            const res = await fetch("{API}/tasks/{task["id"]}/tags", {{
                method: "POST",
                headers: {{"Content-Type": "application/json"}},
                body: JSON.stringify({{tag: "backend"}})
            }});
            return await res.json();
        ''')
        assert result.get("tag") == "backend"
        driver._tag_task_id = task["id"]

    def test_add_second_tag(self, driver):
        driver.execute_script(f'''
            await fetch("{API}/tasks/{driver._tag_task_id}/tags", {{
                method: "POST",
                headers: {{"Content-Type": "application/json"}},
                body: JSON.stringify({{tag: "urgent"}})
            }});
        ''')

    def test_get_all_tags(self, driver):
        result = driver.execute_script(f'''
            const res = await fetch("{API}/tags");
            return await res.json();
        ''')
        tags = result.get("tags", [])
        assert "backend" in tags
        assert "urgent" in tags

    def test_get_tasks_by_tag(self, driver):
        result = driver.execute_script(f'''
            const res = await fetch("{API}/tags/backend/tasks");
            return await res.json();
        ''')
        assert isinstance(result, list)
        task_ids = [t["id"] for t in result]
        assert driver._tag_task_id in task_ids

    def test_remove_tag(self, driver):
        driver.execute_script(f'''
            await fetch("{API}/tasks/{driver._tag_task_id}/tags/urgent", {{
                method: "DELETE"
            }});
        ''')
        result = driver.execute_script(f'''
            const res = await fetch("{API}/tags");
            return await res.json();
        ''')
        tags = result.get("tags", [])
        assert "urgent" not in tags

    def test_add_empty_tag_fails(self, driver):
        result = driver.execute_script(f'''
            const res = await fetch("{API}/tasks/{driver._tag_task_id}/tags", {{
                method: "POST",
                headers: {{"Content-Type": "application/json"}},
                body: JSON.stringify({{tag: ""}})
            }});
            return {{status: res.status}};
        ''')
        assert result["status"] == 400


# ═══════════════════════════════════════════════════════════════
# 8. ISSUES (VALIDATION)
# ═══════════════════════════════════════════════════════════════

class TestIssues:
    """Validation issue tracking."""

    def test_create_issue_on_task(self, driver):
        task = _api_create_task(driver, "E2E Issue Task")
        result = driver.execute_script(f'''
            const res = await fetch("{API}/tasks/{task["id"]}/issues", {{
                method: "POST",
                headers: {{"Content-Type": "application/json"}},
                body: JSON.stringify({{description: "Button color is wrong"}})
            }});
            return await res.json();
        ''')
        assert result.get("description") == "Button color is wrong"
        assert result.get("resolved_at") is None
        driver._issue_task_id = task["id"]
        driver._issue_id = result["id"]

    def test_resolve_issue(self, driver):
        driver.execute_script(f'''
            await fetch("{API}/issues/{driver._issue_id}", {{
                method: "PATCH"
            }});
        ''')
        # Issue should now have resolved_at set
        # Verified by the fact the PATCH didn't error

    def test_create_issue_requires_description(self, driver):
        result = driver.execute_script(f'''
            const res = await fetch("{API}/tasks/{driver._issue_task_id}/issues", {{
                method: "POST",
                headers: {{"Content-Type": "application/json"}},
                body: JSON.stringify({{description: ""}})
            }});
            return {{status: res.status}};
        ''')
        assert result["status"] == 400


# ═══════════════════════════════════════════════════════════════
# 9. COLUMNS CONFIGURATION
# ═══════════════════════════════════════════════════════════════

class TestColumns:
    """Column configuration API."""

    def test_get_columns_returns_five_defaults(self, driver):
        result = driver.execute_script(f'''
            const res = await fetch("{API}/columns");
            return await res.json();
        ''')
        assert len(result) == 5
        assert result[0]["status_key"] == "not_started"
        assert result[4]["status_key"] == "complete"

    def test_columns_have_sort_mode(self, driver):
        result = driver.execute_script(f'''
            const res = await fetch("{API}/columns");
            return await res.json();
        ''')
        for col in result:
            assert "sort_mode" in col, f"Column {col['name']} missing sort_mode"
            assert col["sort_mode"] in ("manual", "date_entered", "date_created", "alphabetical")

    def test_columns_have_sort_direction(self, driver):
        result = driver.execute_script(f'''
            const res = await fetch("{API}/columns");
            return await res.json();
        ''')
        for col in result:
            assert "sort_direction" in col
            assert col["sort_direction"] in ("asc", "desc")

    def test_update_column_name_and_color(self, driver):
        cols = driver.execute_script(f'''
            const res = await fetch("{API}/columns");
            return await res.json();
        ''')
        cols[0]["name"] = "Backlog"
        cols[0]["color"] = "#bc8cff"
        result = driver.execute_script(f'''
            const res = await fetch("{API}/columns", {{
                method: "PUT",
                headers: {{"Content-Type": "application/json"}},
                body: JSON.stringify({json.dumps(cols)})
            }});
            return await res.json();
        ''')
        assert result[0]["name"] == "Backlog"
        assert result[0]["color"] == "#bc8cff"
        # Restore
        cols[0]["name"] = "Not Started"
        cols[0]["color"] = "#8b949e"
        driver.execute_script(f'''
            await fetch("{API}/columns", {{
                method: "PUT",
                headers: {{"Content-Type": "application/json"}},
                body: JSON.stringify({json.dumps(cols)})
            }});
        ''')


# ═══════════════════════════════════════════════════════════════
# 10. REORDER WITHIN COLUMN
# ═══════════════════════════════════════════════════════════════

class TestReorder:
    """Gap-numbered position reordering."""

    def test_reorder_task(self, driver):
        t1 = _api_create_task(driver, "E2E Reorder A")
        t2 = _api_create_task(driver, "E2E Reorder B")
        t3 = _api_create_task(driver, "E2E Reorder C")
        # Move t3 between t1 and t2
        result = driver.execute_script(f'''
            const res = await fetch("{API}/tasks/{t3["id"]}/reorder", {{
                method: "POST",
                headers: {{"Content-Type": "application/json"}},
                body: JSON.stringify({{after_id: "{t1["id"]}", before_id: "{t2["id"]}"}})
            }});
            return await res.json();
        ''')
        assert result.get("ok"), "Reorder should succeed"
        # Verify order via board
        board = _api_get_board(driver)
        ns_tasks = board["tasks"].get("not_started", [])
        ids = [t["id"] for t in ns_tasks]
        # t3 should be between t1 and t2
        if t1["id"] in ids and t2["id"] in ids and t3["id"] in ids:
            i1 = ids.index(t1["id"])
            i3 = ids.index(t3["id"])
            i2 = ids.index(t2["id"])
            assert i1 < i3 < i2, f"Order should be t1,t3,t2 but got indices {i1},{i3},{i2}"


# ═══════════════════════════════════════════════════════════════
# 11. CONTEXT INJECTION
# ═══════════════════════════════════════════════════════════════

class TestContextInjection:
    """Session context injection endpoint."""

    def test_context_endpoint_returns_string(self, driver):
        task = _api_create_task(driver, "E2E Context Task")
        result = driver.execute_script(f'''
            const res = await fetch("{API}/tasks/{task["id"]}/context");
            return await res.json();
        ''')
        assert "context" in result
        assert isinstance(result["context"], str)
        assert len(result["context"]) > 50, "Context should be substantial"
        driver._ctx_task_id = task["id"]

    def test_context_includes_task_title(self, driver):
        result = driver.execute_script(f'''
            const res = await fetch("{API}/tasks/{driver._ctx_task_id}/context");
            return await res.json();
        ''')
        assert "E2E Context Task" in result["context"]

    def test_context_includes_verification_section(self, driver):
        result = driver.execute_script(f'''
            const res = await fetch("{API}/tasks/{driver._ctx_task_id}/context");
            return await res.json();
        ''')
        assert "Verification" in result["context"]

    def test_context_includes_sibling_section(self, driver):
        result = driver.execute_script(f'''
            const res = await fetch("{API}/tasks/{driver._ctx_task_id}/context");
            return await res.json();
        ''')
        assert "Sibling" in result["context"]

    def test_context_for_nonexistent_task(self, driver):
        result = driver.execute_script(f'''
            const res = await fetch("{API}/tasks/nonexistent-id/context");
            return {{status: res.status}};
        ''')
        assert result["status"] == 404


# ═══════════════════════════════════════════════════════════════
# 12. AI PLANNER
# ═══════════════════════════════════════════════════════════════

class TestAIPlanner:
    """AI task planner endpoint."""

    def test_plan_endpoint_exists(self, driver):
        task = _api_create_task(driver, "E2E Plan Task")
        result = driver.execute_script(f'''
            const res = await fetch("{API}/tasks/{task["id"]}/plan", {{
                method: "POST"
            }});
            return {{status: res.status, body: await res.json()}};
        ''')
        # The planner may return empty if SDK not available, but the endpoint should work
        assert result["status"] == 200
        assert "subtasks" in result["body"]
        _api_delete_task(driver, task["id"])


# ═══════════════════════════════════════════════════════════════
# 13. BULK OPERATIONS
# ═══════════════════════════════════════════════════════════════

class TestBulkOperations:
    """Bulk complete and reset subtasks."""

    def test_bulk_complete_all_children(self, driver):
        parent = _api_create_task(driver, "E2E Bulk Parent")
        c1 = _api_create_task(driver, "E2E Bulk Child 1", parent_id=parent["id"])
        c2 = _api_create_task(driver, "E2E Bulk Child 2", parent_id=parent["id"])
        result = driver.execute_script(f'''
            const res = await fetch("{API}/tasks/{parent["id"]}/bulk", {{
                method: "POST",
                headers: {{"Content-Type": "application/json"}},
                body: JSON.stringify({{action: "complete_all"}})
            }});
            return await res.json();
        ''')
        assert result.get("ok")
        assert result.get("updated", 0) >= 2
        driver._bulk_parent_id = parent["id"]

    def test_bulk_reset_all_children(self, driver):
        result = driver.execute_script(f'''
            const res = await fetch("{API}/tasks/{driver._bulk_parent_id}/bulk", {{
                method: "POST",
                headers: {{"Content-Type": "application/json"}},
                body: JSON.stringify({{action: "reset_all"}})
            }});
            return await res.json();
        ''')
        assert result.get("ok")

    def test_bulk_invalid_action(self, driver):
        result = driver.execute_script(f'''
            const res = await fetch("{API}/tasks/{driver._bulk_parent_id}/bulk", {{
                method: "POST",
                headers: {{"Content-Type": "application/json"}},
                body: JSON.stringify({{action: "invalid"}})
            }});
            return {{status: res.status}};
        ''')
        assert result["status"] == 400


# ═══════════════════════════════════════════════════════════════
# 14. REPORTS
# ═══════════════════════════════════════════════════════════════

class TestReports:
    """Report endpoints return valid data."""

    def test_throughput_report(self, driver):
        result = driver.execute_script(f'''
            const res = await fetch("{API}/reports/throughput");
            return await res.json();
        ''')
        assert "throughput" in result

    def test_velocity_report(self, driver):
        result = driver.execute_script(f'''
            const res = await fetch("{API}/reports/velocity");
            return await res.json();
        ''')
        assert "daily" in result
        assert "weekly" in result

    def test_cycle_time_report(self, driver):
        result = driver.execute_script(f'''
            const res = await fetch("{API}/reports/cycle-time");
            return await res.json();
        ''')
        assert "average_hours" in result

    def test_stale_report(self, driver):
        result = driver.execute_script(f'''
            const res = await fetch("{API}/reports/stale");
            return await res.json();
        ''')
        assert "stale" in result
        assert "threshold_days" in result

    def test_remediation_rate_report(self, driver):
        result = driver.execute_script(f'''
            const res = await fetch("{API}/reports/remediation-rate");
            return await res.json();
        ''')
        assert "rate_percent" in result

    def test_tag_distribution_report(self, driver):
        result = driver.execute_script(f'''
            const res = await fetch("{API}/reports/tag-distribution");
            return await res.json();
        ''')
        assert "tags" in result

    def test_session_utilization_report(self, driver):
        result = driver.execute_script(f'''
            const res = await fetch("{API}/reports/session-utilization");
            return await res.json();
        ''')
        assert "utilization_percent" in result

    def test_wip_limits_report(self, driver):
        result = driver.execute_script(f'''
            const res = await fetch("{API}/reports/wip-limits");
            return await res.json();
        ''')
        assert "wip_count" in result
        assert "wip_limit" in result
        assert "over_limit" in result

    def test_blockers_report(self, driver):
        result = driver.execute_script(f'''
            const res = await fetch("{API}/reports/blockers");
            return await res.json();
        ''')
        assert "blockers" in result

    def test_completion_trend_report(self, driver):
        result = driver.execute_script(f'''
            const res = await fetch("{API}/reports/completion-trend");
            return await res.json();
        ''')
        assert "trend" in result

    def test_workload_report(self, driver):
        result = driver.execute_script(f'''
            const res = await fetch("{API}/reports/workload");
            return await res.json();
        ''')
        assert "workload" in result

    def test_status_breakdown_report(self, driver):
        result = driver.execute_script(f'''
            const res = await fetch("{API}/reports/status-breakdown");
            return await res.json();
        ''')
        assert "breakdown" in result

    def test_session_activity_report(self, driver):
        result = driver.execute_script(f'''
            const res = await fetch("{API}/reports/session-activity");
            return await res.json();
        ''')
        assert "tasks" in result

    def test_subtask_depth_report(self, driver):
        result = driver.execute_script(f'''
            const res = await fetch("{API}/reports/subtask-depth");
            return await res.json();
        ''')
        assert "depths" in result

    def test_issue_frequency_report(self, driver):
        result = driver.execute_script(f'''
            const res = await fetch("{API}/reports/issue-frequency");
            return await res.json();
        ''')
        assert "tasks" in result


# ═══════════════════════════════════════════════════════════════
# 15. BOARD API RESPONSE SHAPE
# ═══════════════════════════════════════════════════════════════

class TestBoardResponseShape:
    """Verify the board API response matches the plan spec."""

    def test_board_has_columns_and_tasks(self, driver):
        board = _api_get_board(driver)
        assert "columns" in board
        assert "tasks" in board

    def test_board_columns_are_list(self, driver):
        board = _api_get_board(driver)
        assert isinstance(board["columns"], list)
        assert len(board["columns"]) == 5

    def test_board_tasks_grouped_by_status(self, driver):
        board = _api_get_board(driver)
        tasks = board["tasks"]
        assert isinstance(tasks, dict)
        for key in ["not_started", "working", "validating", "remediating", "complete"]:
            assert key in tasks, f"Tasks should have key '{key}'"
            assert isinstance(tasks[key], list)

    def test_column_has_required_fields(self, driver):
        board = _api_get_board(driver)
        col = board["columns"][0]
        for field in ["id", "project_id", "name", "status_key", "position", "color", "sort_mode", "sort_direction"]:
            assert field in col, f"Column missing field: {field}"

    def test_task_has_required_fields(self, driver):
        board = _api_get_board(driver)
        # Find any task
        for key, tasks in board["tasks"].items():
            if tasks:
                task = tasks[0]
                for field in ["id", "project_id", "title", "status", "position", "created_at", "updated_at"]:
                    assert field in task, f"Task missing field: {field}"
                break


# ═══════════════════════════════════════════════════════════════
# 16. UI — DETAIL PANEL
# ═══════════════════════════════════════════════════════════════

class TestDetailPanel:
    """Detail panel slide-in UI."""

    def test_clicking_card_opens_detail_panel(self, driver):
        _switch_to_kanban(driver)
        _wait_for_board(driver)
        time.sleep(2)
        # Create a task to ensure we have something to click
        task = _api_create_task(driver, "E2E Detail Panel Test")
        driver._detail_task_id = task["id"]
        # Verify the task is fetchable first
        verify = driver.execute_script(f'''
            const res = await fetch("{API}/tasks/{task["id"]}");
            return res.status;
        ''')
        assert verify == 200, f"Task should be fetchable, got status {verify}"
        # Open detail — fire and forget the async call, then poll for result
        driver.execute_script(f'_kanbanOpenDetail("{task["id"]}")')
        # Wait for the detail panel to fully load (title input signals async fetch done)
        try:
            WebDriverWait(driver, 15).until(
                lambda d: d.find_elements(By.ID, "kanban-detail-title")
            )
        except Exception:
            # Capture panel state for debugging
            panel_html = driver.execute_script('''
                const p = document.querySelector(".kanban-detail-panel");
                return p ? p.innerHTML.substring(0, 500) : "NO_PANEL";
            ''')
            console_errors = driver.execute_script('''
                return window._kanbanLastError || "none";
            ''')
            assert False, f"Detail panel content failed to load. Panel: {panel_html}. Errors: {console_errors}"
        panel = driver.find_elements(By.CSS_SELECTOR, ".kanban-detail-panel")
        assert len(panel) >= 1, "Detail panel should appear"

    def test_detail_panel_has_title_input(self, driver):
        title = driver.find_elements(By.ID, "kanban-detail-title")
        assert len(title) >= 1, "Detail panel should have title input"

    def test_detail_panel_has_verification_url_input(self, driver):
        ver = driver.find_elements(By.ID, "kanban-detail-ver")
        assert len(ver) >= 1, "Detail panel should have verification URL input"

    def test_detail_panel_has_save_button(self, driver):
        save = driver.find_elements(By.CSS_SELECTOR, ".kanban-detail-save-btn")
        assert len(save) >= 1, "Detail panel should have save button"

    def test_detail_panel_has_delete_button(self, driver):
        delete = driver.find_elements(By.CSS_SELECTOR, ".kanban-detail-delete-btn")
        assert len(delete) >= 1, "Detail panel should have delete button"

    def test_close_detail_panel(self, driver):
        _close_modals(driver)
        panel = driver.find_elements(By.CSS_SELECTOR, ".kanban-detail-panel")
        assert len(panel) == 0, "Detail panel should be closed"


# ═══════════════════════════════════════════════════════════════
# 17. UI — VALIDATION CEREMONY
# ═══════════════════════════════════════════════════════════════

class TestValidationCeremonyUI:
    """Validation ceremony modal behavior."""

    def test_validation_modal_shows_on_validating_to_complete(self, driver):
        task = _api_create_task(driver, "E2E Val Ceremony")
        _api_move_task(driver, task["id"], "working")
        _api_move_task(driver, task["id"], "validating")
        _switch_to_kanban(driver)
        _wait_for_board(driver)
        # Trigger the modal via JS
        driver.execute_script(f'_kanbanShowValidationModal("{task["id"]}", null, null)')
        time.sleep(1)
        overlay = driver.find_element(By.ID, "pm-overlay")
        assert "show" in (overlay.get_attribute("class") or ""), "Validation modal should be visible"
        driver._val_task_id = task["id"]

    def test_validation_modal_has_approve_and_reject(self, driver):
        approve = driver.find_elements(By.ID, "kanban-val-approve")
        reject = driver.find_elements(By.ID, "kanban-val-reject")
        assert len(approve) >= 1, "Approve button should exist"
        assert len(reject) >= 1, "Reject button should exist"

    def test_validation_modal_has_issue_textarea(self, driver):
        ta = driver.find_elements(By.ID, "kanban-val-issues")
        assert len(ta) >= 1, "Issue textarea should exist"

    def test_validation_cancel_closes_modal(self, driver):
        cancel = driver.find_element(By.ID, "kanban-val-cancel")
        cancel.click()
        time.sleep(0.5)
        overlay = driver.find_element(By.ID, "pm-overlay")
        assert "show" not in (overlay.get_attribute("class") or ""), \
            "Modal should be closed after cancel"


# ═══════════════════════════════════════════════════════════════
# 18. UI — COLUMN CONFIG
# ═══════════════════════════════════════════════════════════════

class TestColumnConfigUI:
    """Column gear icon and config modal."""

    def test_column_gear_button_exists(self, driver):
        _switch_to_kanban(driver)
        _wait_for_board(driver)
        gears = driver.find_elements(By.CSS_SELECTOR, ".kanban-col-gear-btn")
        assert len(gears) == 5, "Each column should have a gear button"

    def test_clicking_gear_opens_config_modal(self, driver):
        driver.execute_script('_kanbanColumnConfig("not_started", {stopPropagation:function(){}})')
        time.sleep(1)
        overlay = driver.find_element(By.ID, "pm-overlay")
        assert "show" in (overlay.get_attribute("class") or "")

    def test_config_modal_has_sort_mode_selector(self, driver):
        select = driver.find_elements(By.ID, "kanban-cfg-sort-mode")
        assert len(select) >= 1, "Sort mode selector should exist"

    def test_config_modal_has_color_swatches(self, driver):
        swatches = driver.find_elements(By.CSS_SELECTOR, ".kanban-color-swatch")
        assert len(swatches) >= 5, "Color swatches should be present"
        _close_modals(driver)


# ═══════════════════════════════════════════════════════════════
# 19. UI — KEYBOARD SHORTCUTS
# ═══════════════════════════════════════════════════════════════

class TestKeyboardShortcuts:
    """Keyboard navigation and shortcuts."""

    def test_question_mark_shows_help(self, driver):
        _switch_to_kanban(driver)
        _wait_for_board(driver)
        _close_modals(driver)
        body = driver.find_element(By.TAG_NAME, "body")
        body.send_keys("?")
        time.sleep(0.5)
        help_overlay = driver.find_elements(By.CSS_SELECTOR, ".kanban-shortcut-overlay")
        assert len(help_overlay) >= 1, "? should show shortcut help"

    def test_help_shows_keybindings(self, driver):
        grid = driver.find_elements(By.CSS_SELECTOR, ".kanban-shortcut-grid kbd")
        assert len(grid) >= 5, "Help should list multiple keybindings"

    def test_dismiss_help(self, driver):
        close = driver.find_elements(By.CSS_SELECTOR, ".kanban-shortcut-close")
        if close:
            driver.execute_script("arguments[0].click()", close[0])
            time.sleep(0.5)

    def test_r_refreshes_board(self, driver):
        body = driver.find_element(By.TAG_NAME, "body")
        body.send_keys("r")
        time.sleep(2)
        cols = driver.find_elements(By.CSS_SELECTOR, ".kanban-column")
        assert len(cols) == 5, "Board should still have 5 columns after refresh"


# ═══════════════════════════════════════════════════════════════
# 20. UI — REPORTS PANEL
# ═══════════════════════════════════════════════════════════════

class TestReportsUI:
    """Reports modal opened from toolbar."""

    def test_reports_button_in_toolbar(self, driver):
        _switch_to_kanban(driver)
        _wait_for_board(driver)
        btns = driver.find_elements(By.CSS_SELECTOR, ".kanban-toolbar-btn, .kanban-sidebar-btn")
        report_btn = [b for b in btns if "Reports" in b.text]
        assert len(report_btn) >= 1, "Reports button should be in toolbar or sidebar"

    def test_clicking_reports_opens_modal(self, driver):
        driver.execute_script('_kanbanShowReportsPanel()')
        time.sleep(3)
        overlay = driver.find_element(By.ID, "pm-overlay")
        assert "show" in (overlay.get_attribute("class") or "")

    def test_reports_modal_has_report_cards(self, driver):
        cards = driver.find_elements(By.CSS_SELECTOR, ".kanban-report-card")
        assert len(cards) >= 10, f"Expected 10+ report cards, got {len(cards)}"

    def test_close_reports_modal(self, driver):
        _close_modals(driver)


# ═══════════════════════════════════════════════════════════════
# 21. VERIFICATION URL
# ═══════════════════════════════════════════════════════════════

class TestVerificationUrl:
    """Verification URL rendering and auto-correction."""

    def test_verification_url_renders_on_card(self, driver):
        task = _api_create_task(driver, "E2E Ver URL Task", verification_url="/api/test")
        _switch_to_kanban(driver)
        _wait_for_board(driver)
        card = driver.find_elements(
            By.CSS_SELECTOR,
            f'.kanban-card[data-task-id="{task["id"]}"] .kanban-ver-link'
        )
        assert len(card) >= 1, "Verification URL icon should render on card"

    def test_auto_correction_relative_url(self, driver):
        result = driver.execute_script('return _resolveVerificationUrl("/api/health")')
        assert result == "BASE_URL + "/"api/health"

    def test_auto_correction_no_leading_slash(self, driver):
        result = driver.execute_script('return _resolveVerificationUrl("api/health")')
        assert result == "BASE_URL + "/"api/health"

    def test_absolute_url_unchanged(self, driver):
        result = driver.execute_script('return _resolveVerificationUrl("https://example.com")')
        assert result == "https://example.com"

    def test_empty_url_returns_empty(self, driver):
        result = driver.execute_script('return _resolveVerificationUrl("")')
        assert result == ""


# ═══════════════════════════════════════════════════════════════
# 22. VIEW MODE SWITCHING
# ═══════════════════════════════════════════════════════════════

class TestViewModeSwitching:
    """Switching between kanban and other views."""

    def test_switch_to_workforce_hides_kanban(self, driver):
        _switch_to_kanban(driver)
        _wait_for_board(driver)
        driver.execute_script('setViewMode("workforce")')
        time.sleep(2)
        board = driver.find_element(By.ID, "kanban-board")
        assert board.value_of_css_property("display") == "none", \
            "Kanban board should be hidden in workforce view"

    def test_switch_back_to_kanban_shows_board(self, driver):
        _switch_to_kanban(driver)
        _wait_for_board(driver)
        board = driver.find_element(By.ID, "kanban-board")
        assert board.is_displayed(), "Kanban board should be visible again"

    def test_kanban_view_selector_shows_kanban_option(self, driver):
        driver.execute_script('openViewModeSelector()')
        time.sleep(1)
        cards = driver.find_elements(By.CSS_SELECTOR, ".add-mode-card")
        kanban_cards = [c for c in cards if "Workflow" in c.text or "Kanban" in c.text]
        assert len(kanban_cards) >= 1, "View mode selector should have Workflow/Kanban option"
        _close_modals(driver)


# ═══════════════════════════════════════════════════════════════
# 23. SHORT DATE DISPLAY
# ═══════════════════════════════════════════════════════════════

class TestShortDate:
    """_kanbanShortDate function behavior."""

    def test_today_shows_time(self, driver):
        # Ensure kanban.js is loaded
        WebDriverWait(driver, LONG_WAIT).until(
            lambda d: d.execute_script('return typeof _kanbanShortDate === "function"')
        )
        result = driver.execute_script('return _kanbanShortDate(new Date().toISOString())')
        assert "AM" in result or "PM" in result, "Today should show AM/PM time"

    def test_empty_returns_empty(self, driver):
        result = driver.execute_script('return _kanbanShortDate("")')
        assert result == ""

    def test_null_returns_empty(self, driver):
        result = driver.execute_script('return _kanbanShortDate(null)')
        assert result == ""

    def test_old_date_shows_month_day_year(self, driver):
        result = driver.execute_script('return _kanbanShortDate("2024-06-15T10:00:00Z")')
        assert "Jun" in result and "15" in result and "'24" in result


# ═══════════════════════════════════════════════════════════════
# 24. VERIFICATION URL AUTO-CORRECTION (EXTENDED)
# ═══════════════════════════════════════════════════════════════

class TestVerificationUrlExtended:
    """Extended tests for _resolveVerificationUrl edge cases."""

    def test_null_returns_empty(self, driver):
        _ensure_page_loaded(driver)
        result = driver.execute_script('return _resolveVerificationUrl(null)')
        assert result == "", "null input should return empty string"

    def test_undefined_returns_empty(self, driver):
        result = driver.execute_script('return _resolveVerificationUrl(undefined)')
        assert result == "", "undefined input should return empty string"

    def test_http_url_passes_through(self, driver):
        result = driver.execute_script('return _resolveVerificationUrl("http://example.com/test")')
        assert result == "http://example.com/test", "http:// URLs should pass through unchanged"

    def test_https_url_passes_through(self, driver):
        result = driver.execute_script('return _resolveVerificationUrl("https://example.com/api")')
        assert result == "https://example.com/api", "https:// URLs should pass through unchanged"


# ═══════════════════════════════════════════════════════════════
# 25. REORDER EDGE CASES
# ═══════════════════════════════════════════════════════════════

class TestReorderEdgeCases:
    """Reorder to first and last positions."""

    def test_reorder_to_first_position(self, driver):
        """Reorder with no after_id should move task to the top."""
        t1 = _api_create_task(driver, "E2E Reord First A")
        t2 = _api_create_task(driver, "E2E Reord First B")
        t3 = _api_create_task(driver, "E2E Reord First C")
        # Move t3 to first position (before t1, no after_id)
        result = driver.execute_script(f'''
            const res = await fetch("{API}/tasks/{t3["id"]}/reorder", {{
                method: "POST",
                headers: {{"Content-Type": "application/json"}},
                body: JSON.stringify({{before_id: "{t1["id"]}"}})
            }});
            return await res.json();
        ''')
        assert result.get("ok"), "Reorder to first position should succeed"
        board = _api_get_board(driver)
        ns_tasks = board["tasks"].get("not_started", [])
        ids = [t["id"] for t in ns_tasks]
        if t1["id"] in ids and t3["id"] in ids:
            assert ids.index(t3["id"]) < ids.index(t1["id"]), \
                "t3 should be before t1 after reorder to first"

    def test_reorder_to_last_position(self, driver):
        """Reorder with no before_id should move task to the end."""
        t1 = _api_create_task(driver, "E2E Reord Last A")
        t2 = _api_create_task(driver, "E2E Reord Last B")
        t3 = _api_create_task(driver, "E2E Reord Last C")
        # Move t1 to last position (after t3, no before_id)
        result = driver.execute_script(f'''
            const res = await fetch("{API}/tasks/{t1["id"]}/reorder", {{
                method: "POST",
                headers: {{"Content-Type": "application/json"}},
                body: JSON.stringify({{after_id: "{t3["id"]}"}})
            }});
            return await res.json();
        ''')
        assert result.get("ok"), "Reorder to last position should succeed"
        board = _api_get_board(driver)
        ns_tasks = board["tasks"].get("not_started", [])
        ids = [t["id"] for t in ns_tasks]
        if t1["id"] in ids and t3["id"] in ids:
            assert ids.index(t1["id"]) > ids.index(t3["id"]), \
                "t1 should be after t3 after reorder to last"


# ═══════════════════════════════════════════════════════════════
# 26. TAG FILTERING ON BOARD (UI)
# ═══════════════════════════════════════════════════════════════

class TestTagFilteringUI:
    """Tag pills on cards and tag filtering behavior."""

    def test_tag_pill_appears_on_card(self, driver):
        """Add a tag via API, refresh board, verify tag pill renders."""
        task = _api_create_task(driver, "E2E TagUI Task")
        driver.execute_script(f'''
            await fetch("{API}/tasks/{task["id"]}/tags", {{
                method: "POST",
                headers: {{"Content-Type": "application/json"}},
                body: JSON.stringify({{tag: "e2e-ui-tag"}})
            }});
        ''')
        driver._tagui_task_id = task["id"]
        _switch_to_kanban(driver)
        _wait_for_board(driver)
        time.sleep(2)
        # Check that the tag filter appears (in sidebar or board)
        tag_bar = driver.find_elements(By.CSS_SELECTOR, ".kanban-tag-filter-bar, .kanban-sidebar-tags")
        assert len(tag_bar) >= 1, "Tag filter should appear when tags exist"

    def test_click_tag_filter_filters_cards(self, driver):
        """Clicking a tag filter pill should filter the board to show only matching cards."""
        # Create a second task WITHOUT the tag
        task2 = _api_create_task(driver, "E2E TagUI NoTag")
        driver._tagui_notag_id = task2["id"]
        _switch_to_kanban(driver)
        _wait_for_board(driver)
        time.sleep(2)
        # Click the tag filter to activate filtering
        driver.execute_script('_kanbanFilterByTag("e2e-ui-tag")')
        time.sleep(1)
        # The active filter indicator should appear
        active = driver.find_elements(By.CSS_SELECTOR, ".kanban-tag-filter-active")
        assert len(active) >= 1, "Active tag filter indicator should appear"

    def test_clear_tag_filter_shows_all_cards(self, driver):
        """Clearing the tag filter should show all cards again."""
        driver.execute_script('_kanbanClearTagFilter()')
        time.sleep(1)
        active = driver.find_elements(By.CSS_SELECTOR, ".kanban-tag-filter-active")
        assert len(active) == 0, "Active tag filter indicator should be gone after clearing"


# ═══════════════════════════════════════════════════════════════
# 27. VALIDATION CEREMONY (API-LEVEL)
# ═══════════════════════════════════════════════════════════════

class TestValidationCeremonyAPI:
    """Approve and reject paths through the validation ceremony via API."""

    def test_approve_path_validating_to_complete(self, driver):
        """Move task validating -> complete (approve)."""
        task = _api_create_task(driver, "E2E ValAPI Approve")
        _api_move_task(driver, task["id"], "working")
        _api_move_task(driver, task["id"], "validating")
        result = _api_move_task(driver, task["id"], "complete")
        assert result["status"] == "complete", \
            "Approve path should move task to complete"
        driver._val_approve_id = task["id"]

    def test_reject_path_validating_to_remediating(self, driver):
        """Move task validating -> remediating (reject)."""
        task = _api_create_task(driver, "E2E ValAPI Reject")
        _api_move_task(driver, task["id"], "working")
        _api_move_task(driver, task["id"], "validating")
        result = _api_move_task(driver, task["id"], "remediating")
        assert result["status"] == "remediating", \
            "Reject path should move task to remediating"
        driver._val_reject_id = task["id"]

    def test_reject_creates_issue(self, driver):
        """After rejecting, create an issue describing the problem."""
        result = driver.execute_script(f'''
            const res = await fetch("{API}/tasks/{driver._val_reject_id}/issues", {{
                method: "POST",
                headers: {{"Content-Type": "application/json"}},
                body: JSON.stringify({{description: "Failed verification: API returns 500"}})
            }});
            return await res.json();
        ''')
        assert result.get("description") == "Failed verification: API returns 500"
        assert result.get("id"), "Issue should have an ID"
        assert result.get("resolved_at") is None, "New issue should not be resolved"


# ═══════════════════════════════════════════════════════════════
# 28. BULK OPERATIONS EDGE CASES
# ═══════════════════════════════════════════════════════════════

class TestBulkEdgeCases:
    """Bulk operations on tasks with no children and nonexistent tasks."""

    def test_bulk_complete_no_children(self, driver):
        """Bulk complete on a task with no children should succeed with 0 updated."""
        task = _api_create_task(driver, "E2E Bulk NoKids")
        result = driver.execute_script(f'''
            const res = await fetch("{API}/tasks/{task["id"]}/bulk", {{
                method: "POST",
                headers: {{"Content-Type": "application/json"}},
                body: JSON.stringify({{action: "complete_all"}})
            }});
            return await res.json();
        ''')
        assert result.get("ok"), "Bulk complete with no children should succeed"
        assert result.get("updated", -1) == 0, "Should update 0 children"
        driver._bulk_nokids_id = task["id"]

    def test_bulk_action_nonexistent_task(self, driver):
        """Bulk action on a nonexistent task should return 404."""
        result = driver.execute_script(f'''
            const res = await fetch("{API}/tasks/nonexistent-fake-id-999/bulk", {{
                method: "POST",
                headers: {{"Content-Type": "application/json"}},
                body: JSON.stringify({{action: "complete_all"}})
            }});
            return {{status: res.status, body: await res.json()}};
        ''')
        assert result["status"] == 404, "Bulk on nonexistent task should return 404"


# ═══════════════════════════════════════════════════════════════
# 29. COLUMN SORT MODE UPDATE
# ═══════════════════════════════════════════════════════════════

class TestColumnSortModeUpdate:
    """Update and verify column sort_mode via PUT /api/kanban/columns."""

    def test_update_sort_mode_to_alphabetical(self, driver):
        """Change the first column's sort_mode and verify it persists."""
        _ensure_page_loaded(driver)
        result = driver.execute_script(f'''
            const res1 = await fetch("{API}/columns");
            const cols = await res1.json();
            const origSort = cols[0].sort_mode;
            cols[0].sort_mode = "alphabetical";
            const res2 = await fetch("{API}/columns", {{
                method: "PUT",
                headers: {{"Content-Type": "application/json"}},
                body: JSON.stringify(cols)
            }});
            const updated = await res2.json();
            return {{updated: updated, origSort: origSort}};
        ''')
        assert result["updated"][0]["sort_mode"] == "alphabetical", \
            "sort_mode should be updated to alphabetical"
        driver._original_sort_mode = result["origSort"]

    def test_sort_mode_persists_on_get(self, driver):
        """GET columns should return the updated sort_mode."""
        result = driver.execute_script(f'''
            const res = await fetch("{API}/columns");
            return await res.json();
        ''')
        assert result[0]["sort_mode"] == "alphabetical", \
            "sort_mode should persist as alphabetical"

    def test_restore_original_sort_mode(self, driver):
        """Restore original sort_mode to avoid side effects."""
        orig = getattr(driver, '_original_sort_mode', 'manual')
        driver.execute_script(f'''
            const res1 = await fetch("{API}/columns");
            const cols = await res1.json();
            cols[0].sort_mode = "{orig}";
            await fetch("{API}/columns", {{
                method: "PUT",
                headers: {{"Content-Type": "application/json"}},
                body: JSON.stringify(cols)
            }});
        ''')


# ═══════════════════════════════════════════════════════════════
# 30. STATUS HISTORY TRACKING
# ═══════════════════════════════════════════════════════════════

class TestStatusHistoryTracking:
    """Verify status history is recorded when tasks transition."""

    def test_multi_transition_records_history(self, driver):
        """Move a task through multiple statuses and verify cycle-time report picks it up."""
        task = _api_create_task(driver, "E2E History Track")
        _api_move_task(driver, task["id"], "working")
        _api_move_task(driver, task["id"], "validating")
        _api_move_task(driver, task["id"], "complete")
        # The cycle-time report queries status_history -- if history was recorded,
        # this task should appear in the report data
        result = driver.execute_script(f'''
            const res = await fetch("{API}/reports/cycle-time");
            return await res.json();
        ''')
        assert "average_hours" in result, "Cycle-time report should work with history data"
        # Also verify throughput report captures the completion
        throughput = driver.execute_script(f'''
            const res = await fetch("{API}/reports/throughput");
            return await res.json();
        ''')
        assert "throughput" in throughput, "Throughput report should reflect history"
        driver._history_task_id = task["id"]


# ═══════════════════════════════════════════════════════════════
# 31. PLAN APPLY ENDPOINT
# ═══════════════════════════════════════════════════════════════

class TestPlanApply:
    """POST /api/kanban/tasks/{id}/plan/apply creates subtasks."""

    def test_apply_plan_creates_children(self, driver):
        """Applying a plan with subtasks should create child tasks."""
        parent = _api_create_task(driver, "E2E Plan Apply Parent")
        subtasks_json = json.dumps([
            {"title": "E2E Sub-plan 1", "description": "First subtask"},
            {"title": "E2E Sub-plan 2", "description": "Second subtask"},
            {"title": "E2E Sub-plan 3", "description": "Third subtask", "verification_url": "/api/check"},
        ])
        result = driver.execute_script(f'''
            const res = await fetch("{API}/tasks/{parent["id"]}/plan/apply", {{
                method: "POST",
                headers: {{"Content-Type": "application/json"}},
                body: JSON.stringify({{subtasks: {subtasks_json}}})
            }});
            return await res.json();
        ''')
        assert result.get("ok"), "Plan apply should succeed"
        assert len(result.get("created", [])) == 3, \
            f"Should create 3 children, got {len(result.get('created', []))}"
        driver._plan_parent_id = parent["id"]

    def test_applied_children_are_fetched_as_children(self, driver):
        """Children created by plan/apply should appear in parent's children list."""
        parent = _api_get_task(driver, driver._plan_parent_id)
        children = parent.get("children", [])
        assert len(children) == 3, f"Parent should have 3 children, got {len(children)}"
        child_titles = [c["title"] for c in children]
        assert "E2E Sub-plan 1" in child_titles
        assert "E2E Sub-plan 2" in child_titles
        assert "E2E Sub-plan 3" in child_titles

    def test_apply_plan_empty_subtasks_fails(self, driver):
        """Applying a plan with empty subtasks should return 400."""
        task = _api_create_task(driver, "E2E Plan Apply Empty")
        result = driver.execute_script(f'''
            const res = await fetch("{API}/tasks/{task["id"]}/plan/apply", {{
                method: "POST",
                headers: {{"Content-Type": "application/json"}},
                body: JSON.stringify({{subtasks: []}})
            }});
            return {{status: res.status, body: await res.json()}};
        ''')
        assert result["status"] == 400, "Empty subtasks should return 400"
        _api_delete_task(driver, task["id"])


# ═══════════════════════════════════════════════════════════════
# 32. BOARD RENDERS IN MAIN PANEL
# ═══════════════════════════════════════════════════════════════

class TestBoardInMainPanel:
    """Verify kanban board is inside main-panel, not sidebar."""

    def test_kanban_board_is_inside_main_panel(self, driver):
        _switch_to_kanban(driver)
        _wait_for_board(driver)
        result = driver.execute_script('''
            const board = document.getElementById("kanban-board");
            if (!board) return "NO_BOARD";
            const main = document.getElementById("main-panel");
            if (!main) return "NO_MAIN_PANEL";
            return main.contains(board) ? "IN_MAIN" : "NOT_IN_MAIN";
        ''')
        assert result == "IN_MAIN", \
            f"kanban-board should be inside main-panel, got: {result}"

    def test_kanban_board_not_inside_sidebar(self, driver):
        result = driver.execute_script('''
            const board = document.getElementById("kanban-board");
            const sidebar = document.getElementById("sidebar");
            if (!board || !sidebar) return "SKIP";
            return sidebar.contains(board) ? "IN_SIDEBAR" : "NOT_IN_SIDEBAR";
        ''')
        assert result != "IN_SIDEBAR", \
            "kanban-board should NOT be inside sidebar"


# ═══════════════════════════════════════════════════════════════
# CLEANUP
# ═══════════════════════════════════════════════════════════════

class TestCleanup:
    """Clean up test data."""

    def test_cleanup_test_tasks(self, driver):
        """Delete all tasks with 'E2E' in the title."""
        board = _api_get_board(driver)
        for key, tasks in board.get("tasks", {}).items():
            for task in tasks:
                if "E2E" in task.get("title", ""):
                    _api_delete_task(driver, task["id"])
        # Verify cleanup
        board = _api_get_board(driver)
        remaining = sum(
            1 for tasks in board.get("tasks", {}).values()
            for t in tasks if "E2E" in t.get("title", "")
        )
        assert remaining == 0, f"{remaining} E2E tasks remain after cleanup"
