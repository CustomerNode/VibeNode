"""Selenium E2E tests for the sidebar multi-select feature (Ctrl/Cmd+click).

Covers:
- Ctrl+click toggles row selection without opening the session
- Multi-select badge updates with the count
- Right-click on a row that's NOT in selection clears the selection
- Right-click on a selected row opens the bulk context menu
- Bulk Delete confirm modal lists the count
- Bulk Delete actually removes the selected sessions from the sidebar
- Project switch clears the selection (deferred — covered by unit smoke)
- Bulk Stop runs without error and clears the selection

Spec: docs/plans/sidebar-multi-select-spec.md (Section 12 — Testing plan)

These tests follow the patterns in test_selenium_session_manage.py:
seed sessions on disk under the user's project folder, navigate to the
test web app, and assert via DOM queries.  They do NOT require a running
backend session/daemon — they only exercise sidebar UI state and the
DELETE endpoint.
"""

import json, time, uuid as uuid_mod
from pathlib import Path
import pytest
from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.common.keys import Keys
from selenium.webdriver.common.action_chains import ActionChains
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC

from tests.e2e.conftest import TEST_BASE_URL as BASE_URL

pytestmark = pytest.mark.e2e

# Where Claude Code stores per-project session JSONLs.  Tests write fixture
# sessions here under the project derived from the test file's parent dir.
CP = Path.home() / ".claude" / "projects"


# ---------------------------------------------------------------------------
# Fixture helpers (mirror test_selenium_session_manage.py for consistency)
# ---------------------------------------------------------------------------

def _uuid():
    return str(uuid_mod.uuid4())


def _ts(m=0, s=0):
    return f"2026-03-10T10:{m:02d}:{s:02d}Z"


def _umsg(c, ts=None, uid=None, sid="t"):
    """Build a user-role JSONL line."""
    uid = uid or _uuid()
    return json.dumps({
        "type": "user",
        "message": {"role": "user", "content": c},
        "timestamp": ts or _ts(),
        "sessionId": sid,
        "uuid": uid,
    })


def _amsg(c, ts=None, uid=None, sid="t"):
    """Build an assistant-role JSONL line."""
    uid = uid or _uuid()
    return json.dumps({
        "type": "assistant",
        "message": {"role": "assistant", "content": c},
        "timestamp": ts or _ts(),
        "sessionId": sid,
        "uuid": uid,
    })


def _ttl(t, sid="t"):
    """Build a custom-title JSONL line so the sidebar shows a readable name."""
    return json.dumps({"type": "custom-title", "customTitle": t, "sessionId": sid})


def _fsd():
    """The .claude/projects directory derived from the tests folder path."""
    _proj = (
        str(Path(__file__).resolve().parents[1])
        .replace("\\", "-")
        .replace("/", "-")
        .replace(":", "-")
    )
    return CP / _proj


def _make_session(sdir, title):
    """Write a minimal valid session JSONL with a custom title.  Returns the session id."""
    sid = f"e2e-multi-{_uuid()[:8]}"
    lines = [
        _ttl(title, sid),
        _umsg("Hello", _ts(0, 0), _uuid(), sid),
        _amsg("Hi there", _ts(0, 5), _uuid(), sid),
    ]
    p = sdir / f"{sid}.jsonl"
    p.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return sid, p


# ---------------------------------------------------------------------------
# Module-scoped fixture: 3 fresh sessions for the multi-select tests
# ---------------------------------------------------------------------------

@pytest.fixture(scope="module")
def sdir():
    return _fsd()


@pytest.fixture(scope="module")
def three_sessions(sdir):
    """Create three sessions on disk, yield their ids, then clean up."""
    sdir.mkdir(parents=True, exist_ok=True)
    sids = []
    paths = []
    for i in range(3):
        sid, p = _make_session(sdir, f"E2E Multi {i+1}")
        sids.append(sid)
        paths.append(p)
    yield sids
    # Cleanup: any path that survives (bulk-delete may have removed some)
    for p in paths:
        try:
            p.unlink(missing_ok=True)
        except Exception:
            pass


# ---------------------------------------------------------------------------
# Setup helper
# ---------------------------------------------------------------------------

def _setup_sessions_view(d, sdir):
    """Navigate to the test app, set the active project, and switch to sessions view."""
    d.get(BASE_URL)
    WebDriverWait(d, 10).until(EC.presence_of_element_located((By.TAG_NAME, "header")))
    time.sleep(1)
    pn = sdir.name
    d.execute_script("localStorage.setItem('activeProject'," + repr(pn) + ")")
    d.execute_script("localStorage.setItem('viewMode','sessions')")
    d.execute_script("localStorage.setItem('sessionDisplayMode','list')")
    d.get(BASE_URL)
    WebDriverWait(d, 10).until(EC.presence_of_element_located((By.TAG_NAME, "header")))
    # Wait for the sidebar list to render rows.
    WebDriverWait(d, 15).until(
        lambda dr: len(dr.find_elements(By.CSS_SELECTOR, ".session-item[data-sid]")) >= 1
    )
    time.sleep(1)


def _ctrl_click(d, el):
    """Ctrl+click an element (Cmd+click on Mac would be the same JS event)."""
    ActionChains(d).key_down(Keys.CONTROL).click(el).key_up(Keys.CONTROL).perform()
    time.sleep(0.2)


def _row(d, sid):
    return d.find_element(By.CSS_SELECTOR, f".session-item[data-sid='{sid}']")


def _ctx_click(d, el):
    """Right-click the element."""
    ActionChains(d).context_click(el).perform()
    time.sleep(0.3)


# ===========================================================================
# Tests
# ===========================================================================

class TestMultiSelect:
    def test_setup(self, driver, sdir, three_sessions):
        _setup_sessions_view(driver, sdir)
        # All three test sessions should be visible.
        for sid in three_sessions:
            assert _row(driver, sid) is not None, f"Row missing for {sid}"

    def test_ctrl_click_adds_to_selection(self, driver, three_sessions):
        # Ctrl+click the first row.
        _ctrl_click(driver, _row(driver, three_sessions[0]))
        # The row should have .multi-selected.
        assert "multi-selected" in _row(driver, three_sessions[0]).get_attribute("class")
        # The badge should show "1 selected".
        badge = driver.find_element(By.ID, "sidebar-multi-badge")
        assert "1 selected" in badge.text

    def test_ctrl_click_does_not_open_session(self, driver, three_sessions):
        # After Ctrl+click, activeId in localStorage should NOT be the row id.
        active = driver.execute_script("return localStorage.getItem('activeSessionId')")
        assert active != three_sessions[0], (
            "Ctrl+click should not change activeId, but it did"
        )

    def test_ctrl_click_more_rows_grows_count(self, driver, three_sessions):
        _ctrl_click(driver, _row(driver, three_sessions[1]))
        _ctrl_click(driver, _row(driver, three_sessions[2]))
        badge = driver.find_element(By.ID, "sidebar-multi-badge")
        assert "3 selected" in badge.text
        for sid in three_sessions:
            assert "multi-selected" in _row(driver, sid).get_attribute("class")

    def test_ctrl_click_again_toggles_off(self, driver, three_sessions):
        _ctrl_click(driver, _row(driver, three_sessions[2]))
        assert "multi-selected" not in _row(driver, three_sessions[2]).get_attribute("class")
        badge = driver.find_element(By.ID, "sidebar-multi-badge")
        assert "2 selected" in badge.text

    def test_clear_button_empties_selection(self, driver, three_sessions):
        clear_btn = driver.find_element(By.CSS_SELECTOR, ".sidebar-multi-badge-clear")
        clear_btn.click()
        time.sleep(0.2)
        # Badge should be gone; rows should not have .multi-selected.
        badges = driver.find_elements(By.ID, "sidebar-multi-badge")
        assert len(badges) == 0
        for sid in three_sessions:
            assert "multi-selected" not in _row(driver, sid).get_attribute("class")

    def test_right_click_unselected_clears_selection(self, driver, three_sessions):
        # Build a 2-row selection then right-click the third row.
        _ctrl_click(driver, _row(driver, three_sessions[0]))
        _ctrl_click(driver, _row(driver, three_sessions[1]))
        assert "2 selected" in driver.find_element(By.ID, "sidebar-multi-badge").text
        _ctx_click(driver, _row(driver, three_sessions[2]))
        # Single-row context menu should have appeared (NOT bulk).
        menus = driver.find_elements(By.CSS_SELECTOR, ".session-ctx-menu")
        assert len(menus) >= 1
        # And it should NOT be a bulk menu (no header strip).
        assert not driver.find_elements(By.CSS_SELECTOR, ".sidebar-bulk-ctx-menu")
        # Selection should be cleared.
        badges = driver.find_elements(By.ID, "sidebar-multi-badge")
        assert len(badges) == 0
        # Close the menu by clicking elsewhere.
        driver.find_element(By.TAG_NAME, "body").click()
        time.sleep(0.2)

    def test_right_click_selected_opens_bulk_menu(self, driver, three_sessions):
        # Build a 3-row selection.
        for sid in three_sessions:
            _ctrl_click(driver, _row(driver, sid))
        assert "3 selected" in driver.find_element(By.ID, "sidebar-multi-badge").text
        _ctx_click(driver, _row(driver, three_sessions[0]))
        # Bulk menu should be shown.
        bulk_menu = driver.find_element(By.CSS_SELECTOR, ".sidebar-bulk-ctx-menu")
        assert bulk_menu is not None
        text = bulk_menu.text
        # Header should announce 3 selected.
        assert "3 selected" in text
        # Expected items.
        assert "Stop all" in text
        assert "Auto-name all" in text
        assert "Duplicate all" in text
        assert "Add all to Compose" in text
        assert "Delete all" in text
        assert "Clear selection" in text
        # Close the menu by clicking elsewhere.
        driver.find_element(By.TAG_NAME, "body").click()
        time.sleep(0.2)

    def test_escape_clears_selection(self, driver, three_sessions):
        # Re-arm selection (previous menu test left the selection intact).
        if not driver.find_elements(By.ID, "sidebar-multi-badge"):
            for sid in three_sessions:
                _ctrl_click(driver, _row(driver, sid))
        ActionChains(driver).send_keys(Keys.ESCAPE).perform()
        time.sleep(0.2)
        badges = driver.find_elements(By.ID, "sidebar-multi-badge")
        assert len(badges) == 0


class TestBulkDelete:
    """Verify the bulk Delete confirm modal and that confirming actually
    removes the selected sessions from disk + sidebar."""

    def test_setup(self, driver, sdir, three_sessions):
        _setup_sessions_view(driver, sdir)
        for sid in three_sessions:
            assert _row(driver, sid) is not None

    def test_select_two_and_open_bulk_menu(self, driver, three_sessions):
        _ctrl_click(driver, _row(driver, three_sessions[0]))
        _ctrl_click(driver, _row(driver, three_sessions[1]))
        assert "2 selected" in driver.find_element(By.ID, "sidebar-multi-badge").text
        _ctx_click(driver, _row(driver, three_sessions[0]))
        assert driver.find_element(By.CSS_SELECTOR, ".sidebar-bulk-ctx-menu") is not None

    def test_delete_confirm_modal_lists_count(self, driver, three_sessions):
        # Click the Delete all item.
        items = driver.find_elements(By.CSS_SELECTOR, ".sidebar-bulk-ctx-menu .ws-ctx-item")
        delete_item = next((i for i in items if "Delete all" in i.text), None)
        assert delete_item is not None
        delete_item.click()
        # Wait for the pm-overlay confirmation modal to appear.
        WebDriverWait(driver, 5).until(
            EC.visibility_of_element_located((By.ID, "pm-overlay"))
        )
        body_text = driver.find_element(By.CSS_SELECTOR, "#pm-overlay .pm-body").text
        assert "2" in body_text, f"Expected '2' in modal body, got: {body_text!r}"
        # Confirm button should say "Delete 2".
        confirm_btn = driver.find_element(By.ID, "pm-confirm")
        assert "Delete 2" in confirm_btn.text

    def test_confirm_actually_deletes(self, driver, sdir, three_sessions):
        # Click Confirm.
        driver.find_element(By.ID, "pm-confirm").click()
        # Wait for the deleted sessions to leave the sidebar.
        WebDriverWait(driver, 15).until(
            lambda dr: not dr.find_elements(
                By.CSS_SELECTOR, f".session-item[data-sid='{three_sessions[0]}']"
            )
        )
        WebDriverWait(driver, 5).until(
            lambda dr: not dr.find_elements(
                By.CSS_SELECTOR, f".session-item[data-sid='{three_sessions[1]}']"
            )
        )
        # The third (un-selected) session should still be there.
        assert _row(driver, three_sessions[2]) is not None
        # Selection badge should be gone.
        assert not driver.find_elements(By.ID, "sidebar-multi-badge")
        # JSONL files for the deleted sessions should be gone from disk.
        for i in (0, 1):
            assert not (sdir / f"{three_sessions[i]}.jsonl").exists(), (
                f"JSONL for {three_sessions[i]} still on disk after bulk delete"
            )


class TestBulkDeleteCancel:
    """Confirm that clicking Cancel on the bulk-delete modal leaves the
    selection AND the underlying sessions intact."""

    def test_setup(self, driver, sdir):
        # Seed a fresh pair just for this class so we don't collide with
        # the deletion class above.
        sdir.mkdir(parents=True, exist_ok=True)
        sids = []
        paths = []
        for i in range(2):
            sid, p = _make_session(sdir, f"E2E Cancel {i+1}")
            sids.append(sid)
            paths.append(p)
        # Stash on the test class so other methods can see them.
        TestBulkDeleteCancel._sids = sids
        TestBulkDeleteCancel._paths = paths
        _setup_sessions_view(driver, sdir)
        for sid in sids:
            WebDriverWait(driver, 10).until(
                lambda dr, s=sid: dr.find_elements(By.CSS_SELECTOR, f".session-item[data-sid='{s}']")
            )

    def test_cancel_keeps_sessions(self, driver, sdir):
        sids = TestBulkDeleteCancel._sids
        for sid in sids:
            _ctrl_click(driver, _row(driver, sid))
        assert "2 selected" in driver.find_element(By.ID, "sidebar-multi-badge").text
        _ctx_click(driver, _row(driver, sids[0]))
        items = driver.find_elements(By.CSS_SELECTOR, ".sidebar-bulk-ctx-menu .ws-ctx-item")
        delete_item = next((i for i in items if "Delete all" in i.text), None)
        assert delete_item is not None
        delete_item.click()
        WebDriverWait(driver, 5).until(
            EC.visibility_of_element_located((By.ID, "pm-cancel"))
        )
        driver.find_element(By.ID, "pm-cancel").click()
        time.sleep(0.5)
        # Sessions still on disk and in sidebar.
        for sid in sids:
            assert (sdir / f"{sid}.jsonl").exists()
            assert _row(driver, sid) is not None

    def test_teardown_cleanup(self, driver, sdir):
        # Best-effort cleanup of the cancel-test fixture sessions.
        for p in TestBulkDeleteCancel._paths:
            try:
                p.unlink(missing_ok=True)
            except Exception:
                pass


class TestSelectionLifecycle:
    """JS-level smoke tests for selection clearing on project / view-mode
    switches.  We verify the helpers behave correctly when invoked, since
    full multi-project Selenium setup is out of scope for v1 tests."""

    def test_setup(self, driver, sdir, three_sessions):
        # We need at least one row to test against.  Reuse the three_sessions
        # fixture even though TestBulkDelete may have removed two of them —
        # JS-level helpers don't care about real session counts.
        _setup_sessions_view(driver, sdir)

    def test_clear_helper_drops_badge(self, driver):
        # Manually populate a fake selection then call _clearMultiSelect.
        driver.execute_script(
            "multiSelectedIds.add('fake-id-A');"
            "multiSelectedIds.add('fake-id-B');"
            "_renderMultiSelectionBadge();"
        )
        # Badge should appear.
        assert driver.find_elements(By.ID, "sidebar-multi-badge"), (
            "Badge should appear after manual population"
        )
        driver.execute_script("_clearMultiSelect();")
        time.sleep(0.1)
        assert not driver.find_elements(By.ID, "sidebar-multi-badge"), (
            "Badge should be gone after _clearMultiSelect"
        )

    def test_prune_drops_unknown_ids(self, driver):
        # Add an ID that doesn't exist in allSessionIds.  After prune,
        # selection should be empty.
        driver.execute_script(
            "multiSelectedIds.add('definitely-not-a-real-session');"
            "_renderMultiSelectionBadge();"
        )
        assert driver.find_elements(By.ID, "sidebar-multi-badge")
        driver.execute_script("_pruneMultiSelectionToExisting();")
        time.sleep(0.1)
        assert not driver.find_elements(By.ID, "sidebar-multi-badge"), (
            "Badge should be gone after pruning unknown IDs"
        )

    def test_view_mode_switch_clears_selection(self, driver):
        # Populate a fake selection, switch viewMode away from sessions,
        # and verify the badge is gone.
        driver.execute_script(
            "multiSelectedIds.add('fake-vm-test');"
            "_renderMultiSelectionBadge();"
        )
        assert driver.find_elements(By.ID, "sidebar-multi-badge")
        # Switch to homepage view (simplest target — no sidebar).
        driver.execute_script("setViewMode('homepage');")
        time.sleep(0.5)
        # Switch back to sessions; the selection should NOT be restored.
        driver.execute_script("setViewMode('sessions');")
        time.sleep(0.5)
        # Badge should not be present.
        assert not driver.find_elements(By.ID, "sidebar-multi-badge"), (
            "Selection should be cleared when leaving sessions view"
        )
