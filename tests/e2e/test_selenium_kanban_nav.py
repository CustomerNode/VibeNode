"""Selenium E2E test for kanban drill-down browser back/forward."""

import time
import pytest
from selenium import webdriver
from selenium.webdriver.support.ui import WebDriverWait

from tests.e2e.conftest import TEST_BASE_URL as BASE_URL
LONG_WAIT = 90

pytestmark = pytest.mark.e2e


TEST_PROJECT = "__selenium_test__"


@pytest.fixture(scope="class", autouse=True)
def kanban_nav_setup(driver):
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
            body: JSON.stringify({project: arguments[0]})
        });
    ''', TEST_PROJECT)
    time.sleep(2)
    driver.execute_script('document.querySelectorAll(".show").forEach(function(e){e.classList.remove("show")})')
    driver.execute_script('window.__test_created_ids = [];')
    time.sleep(1)
    yield
    # Only delete tasks we created
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


def _create_task_with_child(driver):
    """Create a parent + child task, track IDs, return parent ID."""
    parent_id = driver.execute_script('''
        return fetch("/api/kanban/tasks", {
            method: "POST",
            headers: {"Content-Type": "application/json"},
            body: JSON.stringify({title: "Nav Parent", status: "not_started"})
        }).then(r => r.json()).then(d => {
            (window.__test_created_ids = window.__test_created_ids || []).push(d.id);
            return d.id;
        });
    ''')
    time.sleep(1)
    driver.execute_script('''
        fetch("/api/kanban/tasks", {
            method: "POST",
            headers: {"Content-Type": "application/json"},
            body: JSON.stringify({title: "Nav Child", status: "not_started", parent_id: arguments[0]})
        }).then(r => r.json()).then(d => {
            (window.__test_created_ids = window.__test_created_ids || []).push(d.id);
        });
    ''', parent_id)
    time.sleep(1)
    driver.execute_script('initKanban(true)')
    time.sleep(2)
    return parent_id


class TestKanbanNavigation:

    def test_01_setup(self, driver):
        _to_kanban(driver)
        self.__class__.parent_id = _create_task_with_child(driver)

    def test_02_board_shows_columns(self, driver):
        # initKanban may still be running — wait and force refresh
        driver.execute_script('initKanban(true)')
        time.sleep(3)
        WebDriverWait(driver, 15).until(
            lambda d: d.execute_script('return document.querySelector(".kanban-columns-wrapper") !== null || document.querySelector(".kanban-empty-state") !== null')
        )
        has_board = driver.execute_script('return document.querySelector(".kanban-columns-wrapper") !== null || document.querySelector(".kanban-empty-state") !== null')
        assert has_board, "Board not visible"

    def test_03_drill_into_task(self, driver):
        pid = self.__class__.parent_id
        driver.execute_script(f'navigateToTask("{pid}")')
        time.sleep(2)
        has_detail = driver.execute_script('return document.querySelector(".kanban-drill-titlebar") !== null')
        assert has_detail, "Drill-down view not shown"
        assert '#kanban/task/' in driver.current_url

    def test_04_browser_back_returns_to_board(self, driver):
        driver.back()
        time.sleep(2)
        has_cols = driver.execute_script('return document.querySelector(".kanban-columns-wrapper") !== null')
        has_detail = driver.execute_script('return document.querySelector(".kanban-drill-titlebar") !== null')
        assert has_cols, "Board columns not restored after back"
        assert not has_detail, "Drill-down view still showing after back"

    def test_05_browser_forward_returns_to_task(self, driver):
        driver.forward()
        time.sleep(2)
        has_detail = driver.execute_script('return document.querySelector(".kanban-drill-titlebar") !== null')
        assert has_detail, "Drill-down view not restored after forward"

    def test_06_back_from_child_to_parent(self, driver):
        """Drill into child from parent detail, then back should go to parent detail."""
        # Get child ID
        child_id = driver.execute_script('''
            var row = document.querySelector(".kanban-drill-subtask-row");
            return row ? row.getAttribute("onclick").match(/'([^']+)'/)?.[1] : null;
        ''')
        if not child_id:
            pytest.skip("No child task row found")

        driver.execute_script(f'navigateToTask("{child_id}")')
        time.sleep(2)

        # Should show child detail
        title = driver.execute_script('''
            var t = document.querySelector(".kanban-drill-title");
            return t ? t.textContent : "";
        ''')
        assert "Nav Child" in title

        # Back should go to parent detail
        driver.back()
        time.sleep(2)
        title = driver.execute_script('''
            var t = document.querySelector(".kanban-drill-title");
            return t ? t.textContent : "";
        ''')
        assert "Nav Parent" in title

    def test_07_back_to_board_from_parent(self, driver):
        driver.back()
        time.sleep(2)
        has_cols = driver.execute_script('return document.querySelector(".kanban-columns-wrapper") !== null')
        assert has_cols, "Board not restored after second back"

    def test_08_cleanup(self, driver):
        driver.execute_script('''
            var ids = window.__test_created_ids || [];
            Promise.all(ids.map(function(id) {
                return fetch("/api/kanban/tasks/" + id, {method: "DELETE"});
            })).then(function() { window.__test_created_ids = []; initKanban(true); });
        ''')
        time.sleep(2)
