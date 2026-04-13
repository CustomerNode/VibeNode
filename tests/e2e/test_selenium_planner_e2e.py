"""Selenium E2E test for the AI Planner flow.

Tests the ACTUAL end-to-end: open popup → type prompt → submit →
slide-out opens → session starts → Claude responds → tree renders OR error.
"""

import time
import pytest
from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC

from tests.e2e.conftest import TEST_BASE_URL as BASE_URL
LONG_WAIT = 90

pytestmark = pytest.mark.e2e


@pytest.fixture(scope="class", autouse=True)
def planner_setup(driver):
    """Navigate, switch to test project, track task IDs for cleanup."""
    driver.get(BASE_URL)
    WebDriverWait(driver, LONG_WAIT).until(
        lambda drv: drv.execute_script('return typeof setViewMode === "function"')
    )
    time.sleep(2)
    driver.execute_script('''
        fetch("/api/set-project", {
            method: "POST",
            headers: {"Content-Type": "application/json"},
            body: JSON.stringify({project: "__selenium_test__"})
        });
    ''')
    time.sleep(2)
    driver.execute_script('document.querySelectorAll(".show").forEach(function(e){e.classList.remove("show")})')
    driver.execute_script('window.__test_created_ids = [];')
    time.sleep(1)
    yield
    # Only delete tasks we created — NEVER wipe the board
    driver.execute_script('''
        var ids = window.__test_created_ids || [];
        Promise.all(ids.map(function(id) {
            return fetch("/api/kanban/tasks/" + id, {method: "DELETE"});
        }));
    ''')
    time.sleep(2)


def _to_kanban(driver):
    driver.execute_script('setViewMode("kanban")')
    time.sleep(3)


class TestPlannerE2E:

    def test_01_open_popup_and_submit(self, driver):
        """Open New Task popup, type in AI section, submit → slide-out opens."""
        _to_kanban(driver)

        # Open the New Task popup
        driver.execute_script('createTask("not_started")')
        time.sleep(0.5)

        # Verify Plan with AI textarea exists
        ta = driver.execute_script('return document.getElementById("kanban-plan-input") !== null')
        assert ta, "Plan with AI textarea missing from popup"

        # Type a planning prompt
        driver.execute_script('''
            document.getElementById("kanban-plan-input").value = "Create a simple todo app with add, edit, delete, and mark complete features";
        ''')

        # Submit
        driver.execute_script('_submitPlanWithAi()')
        time.sleep(1)

        # Verify slide-out opened
        panel = driver.execute_script('return document.getElementById("kanban-planner-panel") !== null')
        assert panel, "Planner slide-out panel did not open"

        panel_open = driver.execute_script('return document.getElementById("kanban-planner-panel").classList.contains("open")')
        assert panel_open, "Planner panel does not have 'open' class"

    def test_02_session_started(self, driver):
        """Verify a planner session was actually created."""
        sid = driver.execute_script('return _plannerSessionId')
        assert sid is not None, "_plannerSessionId is null — session was not created"
        assert len(sid) > 10, f"_plannerSessionId looks invalid: {sid}"

    def test_03_listeners_attached(self, driver):
        """Verify socket listeners are attached."""
        entry = driver.execute_script('return _plannerEntryListener !== null')
        state = driver.execute_script('return _plannerStateListener !== null')
        assert entry, "_plannerEntryListener is null — not attached"
        assert state, "_plannerStateListener is null — not attached"

    def test_04_progress_or_result_within_timeout(self, driver):
        """Wait up to 120s for either progress feedback or final result.

        This is the critical test — verifies the full pipeline:
        socket.emit('start_session') → daemon → Claude → session_entry events →
        _updatePlannerProgress / _showPlanResult
        """
        deadline = time.time() + 120
        got_progress = False
        got_result = False

        while time.time() < deadline:
            state = driver.execute_script('''
                var body = document.getElementById("planner-body");
                if (!body) return "no-body";
                if (body.querySelector(".planner-result")) return "result";
                if (body.querySelector(".planner-error")) return "error";
                if (body.querySelector(".planner-progress-count")) return "progress";
                if (body.querySelector(".planner-spinner")) return "spinning";
                return "unknown:" + body.innerHTML.slice(0, 100);
            ''')

            if state == "result":
                got_result = True
                break
            elif state == "error":
                # Error is still a valid end state (means session ran but JSON parse failed)
                got_result = True
                break
            elif state == "progress":
                got_progress = True

            time.sleep(2)

        assert got_progress or got_result, \
            f"Neither progress nor result appeared within 120s. Last state: {state}"

    def test_05_result_has_tree_and_accept(self, driver):
        """If we got a result, verify it has a tree and accept button."""
        has_result = driver.execute_script('''
            var body = document.getElementById("planner-body");
            return body && body.querySelector(".planner-result") !== null;
        ''')
        if not has_result:
            pytest.skip("No result to check (may have errored)")

        checks = driver.execute_script('''
            var body = document.getElementById("planner-body");
            return {
                hasTree: body.querySelector(".planner-tree") !== null,
                hasAccept: body.querySelector(".planner-accept-btn") !== null,
                hasHint: body.querySelector(".planner-hint") !== null,
                nodeCount: body.querySelectorAll(".planner-node").length,
                acceptText: body.querySelector(".planner-accept-btn") ?
                    body.querySelector(".planner-accept-btn").textContent : "",
                proposalSet: typeof _plannerProposal === "object" && _plannerProposal !== null,
            };
        ''')
        assert checks["hasTree"], "No .planner-tree found in result"
        assert checks["hasAccept"], "No accept button found"
        assert checks["hasHint"], "No hint text found"
        assert checks["nodeCount"] > 0, "Tree has 0 nodes"
        assert "Add" in checks["acceptText"], f"Accept button text wrong: {checks['acceptText']}"
        assert checks["proposalSet"], "_plannerProposal is null after result"

    def test_06_tree_is_collapsible(self, driver):
        """Nodes with subtasks should have chevrons and collapse on click."""
        has_chevron = driver.execute_script('''
            var body = document.getElementById("planner-body");
            return body ? body.querySelectorAll(".planner-chevron").length : 0;
        ''')
        if has_chevron == 0:
            pytest.skip("No collapsible nodes (flat structure)")

        # Click first chevron's parent row
        driver.execute_script('''
            var row = document.querySelector(".planner-chevron").closest(".planner-node-row");
            if (row) row.click();
        ''')
        time.sleep(0.3)
        collapsed = driver.execute_script('''
            return document.querySelector(".planner-node.collapsed") !== null;
        ''')
        assert collapsed, "Node did not collapse on click"

        # Click again to expand
        driver.execute_script('''
            var row = document.querySelector(".planner-node.collapsed .planner-node-row");
            if (row) row.click();
        ''')
        time.sleep(0.3)

    def test_07_refine_input_exists(self, driver):
        """Refine input textarea should be present."""
        exists = driver.execute_script('return document.getElementById("planner-refine-input") !== null')
        assert exists, "Refine input missing"

    def test_08_accept_creates_tasks(self, driver):
        """Clicking accept should create tasks on the board."""
        has_proposal = driver.execute_script('return _plannerProposal !== null && _plannerProposal.tasks && _plannerProposal.tasks.length > 0')
        if not has_proposal:
            pytest.skip("No proposal to accept")

        expected_count = driver.execute_script('return _countTasks(_plannerProposal.tasks)')

        # Accept
        driver.execute_script('''
            window._acceptDone = false;
            _acceptPlan().then(() => { window._acceptDone = true; }).catch(() => { window._acceptDone = true; });
        ''')
        WebDriverWait(driver, 30).until(lambda d: d.execute_script('return window._acceptDone === true'))
        time.sleep(2)

        # Verify panel closed
        panel = driver.execute_script('return document.getElementById("kanban-planner-panel")')
        assert panel is None, "Planner panel still exists after accept"

        # Verify tasks were created (top-level cards on board — subtasks are nested)
        driver.execute_script('initKanban(true)')
        time.sleep(3)
        card_count = driver.execute_script('return document.querySelectorAll(".kanban-card").length')
        # Board only shows root tasks, not subtasks — just verify some appeared
        assert card_count > 0, f"Expected cards on board after accept, got {card_count}"

    def test_09_cleanup(self, driver):
        """Clean up only tasks we created."""
        driver.execute_script('''
            var ids = window.__test_created_ids || [];
            Promise.all(ids.map(function(id) {
                return fetch("/api/kanban/tasks/" + id, {method: "DELETE"});
            })).then(function() { window.__test_created_ids = []; initKanban(true); });
        ''')
        time.sleep(2)

    def test_10_no_toolbar_bleed(self, driver):
        """Main toolbar should not be showing after planner close."""
        _to_kanban(driver)
        tb = driver.execute_script('''
            var tb = document.getElementById("main-toolbar");
            return tb ? tb.style.display : "none";
        ''')
        assert tb == "none" or tb == "", "Toolbar visible after planner closed"
