"""Selenium E2E tests for auto-send and text preservation.

Split from ``tests/test_auto_send_and_text_preserve.py`` so the source-guard
checks can stay in the fast suite while the browser-driven checks run with
the rest of the e2e suite.

The ``driver`` fixture is provided by ``tests/e2e/conftest.py`` (class-scoped
shared Chrome instance). Each class is marked ``e2e`` so the marker filter
in CI picks them up.

Prerequisites:
  pip install -r requirements-test.txt
  Chrome browser installed

Run:
  pytest tests/e2e/test_auto_send_and_text_preserve_e2e.py -m e2e -v
"""

import time
import pytest

from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.common.keys import Keys

from tests.e2e.conftest import TEST_BASE_URL as BASE_URL

LONG_WAIT = 90


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _ensure_project_selected(driver):
    """Make sure a project is selected so sessions can be created."""
    driver.get(BASE_URL)
    time.sleep(2)
    label = driver.find_element(By.ID, "project-label")
    if "Select project" in label.text:
        label.click()
        WebDriverWait(driver, 5).until(
            EC.presence_of_element_located((By.CSS_SELECTOR, ".project-item"))
        )
        driver.find_element(By.CSS_SELECTOR, ".project-item").click()
        time.sleep(2)


def _click_new_session(driver):
    """Click the + button to start a new chat session. Returns the textarea."""
    driver.find_element(By.ID, "btn-add-agent").click()
    WebDriverWait(driver, 5).until(
        EC.presence_of_element_located((By.ID, "live-input-ta"))
    )
    return driver.find_element(By.ID, "live-input-ta")


# ===========================================================================
# E2E test classes
# ===========================================================================

@pytest.mark.e2e
class TestE2EAutoSendOnSessionSwitch:
    """When user has text typed and clicks away to another session,
    the text must be auto-sent to the original session."""

    def test_setup(self, driver):
        """Load page and select project."""
        _ensure_project_selected(driver)

    def test_auto_send_new_session_text_on_switch(self, driver):
        """Type text in a new-session textarea, click a different session
        in the sidebar -> the new session should start with that text."""
        ta = _click_new_session(driver)

        test_msg = "AUTO_SEND_TEST say exactly: blue whale"
        ta.send_keys(test_msg)
        time.sleep(0.5)

        assert ta.get_attribute("value") == test_msg

        sidebar_items = driver.find_elements(By.CSS_SELECTOR, ".session-item")
        if len(sidebar_items) < 2:
            pytest.skip("Need at least 2 sessions in sidebar to test switch")

        for item in sidebar_items:
            if "active" not in (item.get_attribute("class") or ""):
                item.click()
                break
        time.sleep(2)

        sidebar_html = driver.find_element(By.ID, "session-list").get_attribute("innerHTML")
        assert "AUTO_SEND_TEST" in sidebar_html or "blue whale" in sidebar_html, (
            "Auto-send did not fire: the new session should have started with "
            "the typed message, creating a title derived from it"
        )


@pytest.mark.e2e
class TestE2ETextPreservationNewChatToWorking:
    """Text typed in the new-chat textarea must not be destroyed when
    the input bar transitions to a different state."""

    def test_setup(self, driver):
        _ensure_project_selected(driver)

    def test_text_survives_in_textarea_before_submit(self, driver):
        """Type text in a new-session textarea. Even after a brief delay
        (during which updateLiveInputBar may fire), text must still be there."""
        ta = _click_new_session(driver)
        test_text = "PRESERVE_TEST this text must survive"
        ta.send_keys(test_text)
        time.sleep(0.5)

        driver.set_window_size(1200, 800)
        time.sleep(1)
        driver.set_window_size(1400, 900)
        time.sleep(1)

        ta = driver.find_element(By.ID, "live-input-ta")
        actual = ta.get_attribute("value")
        assert test_text in actual, (
            f"Text was destroyed during re-render. Expected '{test_text}', got '{actual}'"
        )


@pytest.mark.e2e
class TestE2ETextPreservationIdleToWorking:
    """Text typed in the idle textarea must be preserved when the session
    transitions to working state (e.g. via queue auto-dispatch)."""

    def test_setup(self, driver):
        _ensure_project_selected(driver)

    def test_idle_text_preserved_after_send(self, driver):
        """Start a session, wait for idle, type follow-up text, submit it,
        verify the working-state queue textarea appears. Then wait for idle
        again and type more -- text should survive any bar re-renders."""
        ta = _click_new_session(driver)
        ta.send_keys("Say exactly: red fox")
        ta.send_keys(Keys.CONTROL, Keys.ENTER)

        try:
            WebDriverWait(driver, LONG_WAIT).until(
                EC.presence_of_element_located((By.CSS_SELECTOR, "#live-log .msg.assistant"))
            )
        except Exception:
            pytest.skip("Claude did not respond in time")

        try:
            WebDriverWait(driver, LONG_WAIT).until(
                EC.presence_of_element_located((By.ID, "live-input-ta"))
            )
        except Exception:
            pytest.skip("Session did not return to idle")

        ta = driver.find_element(By.ID, "live-input-ta")
        preserve_text = "IDLE_PRESERVE do not lose this text"
        ta.send_keys(preserve_text)
        time.sleep(2)

        ta = driver.find_element(By.ID, "live-input-ta")
        actual = ta.get_attribute("value")
        assert preserve_text in actual, (
            f"Idle textarea text was destroyed. Expected '{preserve_text}', got '{actual}'"
        )


@pytest.mark.e2e
class TestE2EAutoSendFunctionExists:
    """Verify the _autoSendPendingInput function is callable in the browser."""

    def test_setup(self, driver):
        _ensure_project_selected(driver)

    def test_function_exists_in_browser(self, driver):
        """The function must be defined and callable in the browser's JS context."""
        result = driver.execute_script(
            "return typeof _autoSendPendingInput === 'function'"
        )
        assert result is True, (
            "_autoSendPendingInput is not defined as a function in the browser"
        )

    def test_function_does_not_throw_when_no_session(self, driver):
        """Calling _autoSendPendingInput with no active session must not throw."""
        error = driver.execute_script("""
            try {
                var oldId = liveSessionId;
                liveSessionId = null;
                _autoSendPendingInput();
                liveSessionId = oldId;
                return null;
            } catch(e) {
                return e.message;
            }
        """)
        assert error is None, f"_autoSendPendingInput threw: {error}"


@pytest.mark.e2e
class TestE2ETextPreservationViaJS:
    """Use execute_script to directly test the text preservation logic
    without needing to trigger real server state changes."""

    def test_setup(self, driver):
        _ensure_project_selected(driver)

    def test_text_preserved_idle_to_working(self, driver):
        """Simulate idle->working transition via JS and verify text preservation."""
        ta = _click_new_session(driver)
        ta.send_keys("Say exactly: test fish")
        ta.send_keys(Keys.CONTROL, Keys.ENTER)

        time.sleep(3)

        result = driver.execute_script("""
            if (!liveSessionId) return 'no_session';

            var sid = liveSessionId;

            sessionKinds[sid] = 'idle';
            runningIds.add(sid);
            liveBarState = null;
            updateLiveInputBar();

            var ta = document.getElementById('live-input-ta');
            if (!ta) return 'no_idle_textarea';
            ta.value = 'PRESERVE_ME_123';

            sessionKinds[sid] = 'working';
            liveBarState = null;
            updateLiveInputBar();

            var qta = document.getElementById('live-queue-ta');
            if (!qta) return 'no_queue_textarea';
            return qta.value;
        """)

        if result == 'no_session':
            pytest.skip("No live session available")

        assert result == "PRESERVE_ME_123", (
            f"Text was NOT preserved during idle->working transition. "
            f"Queue textarea value: '{result}'"
        )

    def test_text_preserved_working_to_idle(self, driver):
        """Simulate working->idle transition via JS and verify text preservation."""
        result = driver.execute_script("""
            if (!liveSessionId) return 'no_session';

            var sid = liveSessionId;

            sessionKinds[sid] = 'working';
            runningIds.add(sid);
            liveBarState = null;
            updateLiveInputBar();

            var qta = document.getElementById('live-queue-ta');
            if (!qta) return 'no_queue_textarea';
            qta.value = 'QUEUE_TEXT_456';

            sessionKinds[sid] = 'idle';
            liveBarState = null;
            updateLiveInputBar();

            var ta = document.getElementById('live-input-ta');
            if (!ta) return 'no_idle_textarea';
            return ta.value;
        """)

        if result == 'no_session':
            pytest.skip("No live session available")

        assert result == "QUEUE_TEXT_456", (
            f"Text was NOT preserved during working->idle transition. "
            f"Idle textarea value: '{result}'"
        )

    def test_text_preserved_idle_to_question(self, driver):
        """Simulate idle->question transition via JS and verify text preservation."""
        result = driver.execute_script("""
            if (!liveSessionId) return 'no_session';

            var sid = liveSessionId;

            sessionKinds[sid] = 'idle';
            runningIds.add(sid);
            liveBarState = null;
            updateLiveInputBar();

            var ta = document.getElementById('live-input-ta');
            if (!ta) return 'no_idle_textarea';
            ta.value = 'QUESTION_TEXT_789';

            sessionKinds[sid] = 'question';
            waitingData[sid] = {question: 'Allow Bash?', options: ['y','n'], kind: 'tool'};
            liveBarState = null;
            updateLiveInputBar();

            var qta = document.getElementById('live-input-ta');
            if (!qta) return 'no_question_textarea';
            return qta.value;
        """)

        if result == 'no_session':
            pytest.skip("No live session available")

        assert result == "QUESTION_TEXT_789", (
            f"Text was NOT preserved during idle->question transition. "
            f"Question textarea value: '{result}'"
        )

    def test_text_preserved_question_to_working(self, driver):
        """Simulate question->working transition via JS and verify text preservation."""
        result = driver.execute_script("""
            if (!liveSessionId) return 'no_session';

            var sid = liveSessionId;

            sessionKinds[sid] = 'question';
            runningIds.add(sid);
            waitingData[sid] = {question: 'Allow?', options: ['y','n'], kind: 'tool'};
            liveBarState = null;
            updateLiveInputBar();

            var ta = document.getElementById('live-input-ta');
            if (!ta) return 'no_question_textarea';
            ta.value = 'WORKING_TEXT_012';

            delete waitingData[sid];
            sessionKinds[sid] = 'working';
            liveBarState = null;
            updateLiveInputBar();

            var qta = document.getElementById('live-queue-ta');
            if (!qta) return 'no_queue_textarea';
            return qta.value;
        """)

        if result == 'no_session':
            pytest.skip("No live session available")

        assert result == "WORKING_TEXT_012", (
            f"Text was NOT preserved during question->working transition. "
            f"Queue textarea value: '{result}'"
        )
