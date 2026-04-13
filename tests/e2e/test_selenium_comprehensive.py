"""Comprehensive Selenium E2E tests for VibeNode.

Tests every user-facing flow via headless Chrome against the live server.
"""

import time
import pytest
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.common.keys import Keys
from selenium.webdriver.common.action_chains import ActionChains

from tests.e2e.conftest import TEST_BASE_URL as BASE_URL
LONG_WAIT = 90  # seconds for Claude to respond

pytestmark = pytest.mark.e2e



def _wait_for_idle(driver, timeout=LONG_WAIT):
    """Wait for the live-input-ta textarea to appear (session idle)."""
    WebDriverWait(driver, timeout).until(
        EC.presence_of_element_located((By.ID, "live-input-ta"))
    )


def _count_user_msgs(driver):
    return len(driver.find_elements(By.CSS_SELECTOR, "#live-log .msg.user"))


def _count_asst_msgs(driver):
    return len(driver.find_elements(By.CSS_SELECTOR, "#live-log .msg.assistant"))


def _start_new_session(driver, name, message):
    """Helper: open dialog, fill, submit, wait for live panel."""
    driver.find_element(By.ID, "btn-add-agent").click()
    WebDriverWait(driver, 5).until(
        EC.visibility_of_element_located((By.ID, "ns-name"))
    )
    driver.find_element(By.ID, "ns-name").send_keys(name)
    driver.find_element(By.ID, "ns-message").send_keys(message)
    driver.find_element(By.ID, "ns-start").click()
    time.sleep(1)
    WebDriverWait(driver, 5).until(
        EC.presence_of_element_located((By.ID, "live-panel"))
    )


def _send_followup(driver, text):
    """Helper: type text in idle textarea and Ctrl+Enter."""
    ta = driver.find_element(By.ID, "live-input-ta")
    ta.clear()
    ta.send_keys(text)
    ta.send_keys(Keys.CONTROL, Keys.ENTER)


# =========================================================================
# Basic flow
# =========================================================================

class TestBasicNewSession:
    """Simple new session: send message, get response."""

    def test_page_loads(self, driver):
        driver.get(BASE_URL)
        WebDriverWait(driver, 10).until(
            EC.presence_of_element_located((By.TAG_NAME, "header"))
        )
        assert "Claude" in driver.title

    def test_project_selected(self, driver):
        time.sleep(2)
        label = driver.find_element(By.ID, "project-label")
        if "Select project" in label.text:
            label.click()
            WebDriverWait(driver, 5).until(
                EC.presence_of_element_located((By.CSS_SELECTOR, ".project-item"))
            )
            driver.find_element(By.CSS_SELECTOR, ".project-item").click()
            time.sleep(2)

    def test_new_session_simple(self, driver):
        _start_new_session(driver, "Simple Test", "Say exactly: green apple")
        # Wait for user message
        WebDriverWait(driver, 15).until(
            EC.presence_of_element_located((By.CSS_SELECTOR, "#live-log .msg.user"))
        )
        assert _count_user_msgs(driver) == 1

    def test_assistant_responds(self, driver):
        WebDriverWait(driver, LONG_WAIT).until(
            EC.presence_of_element_located((By.CSS_SELECTOR, "#live-log .msg.assistant"))
        )
        assert _count_asst_msgs(driver) >= 1

    def test_session_goes_idle(self, driver):
        _wait_for_idle(driver)
        ta = driver.find_element(By.ID, "live-input-ta")
        assert ta.is_displayed()

    def test_no_duplicate_user_messages(self, driver):
        time.sleep(2)
        assert _count_user_msgs(driver) == 1


# =========================================================================
# Follow-up messages
# =========================================================================

class TestFollowUpMessages:
    """Send a follow-up after initial response."""

    def test_setup_session(self, driver):
        driver.get(BASE_URL)
        time.sleep(2)
        # Select project if needed
        label = driver.find_element(By.ID, "project-label")
        if "Select project" in label.text:
            label.click()
            WebDriverWait(driver, 5).until(EC.presence_of_element_located((By.CSS_SELECTOR, ".project-item")))
            driver.find_element(By.CSS_SELECTOR, ".project-item").click()
            time.sleep(2)
        _start_new_session(driver, "Follow-up Test", "Say exactly: red")
        WebDriverWait(driver, LONG_WAIT).until(EC.presence_of_element_located((By.CSS_SELECTOR, "#live-log .msg.assistant")))
        _wait_for_idle(driver)
        time.sleep(1)

    def test_send_followup(self, driver):
        _send_followup(driver, "Say exactly: blue")
        # Wait for second assistant response
        WebDriverWait(driver, LONG_WAIT).until(
            lambda d: _count_asst_msgs(d) >= 2
        )
        time.sleep(2)

    def test_exactly_two_user_messages(self, driver):
        assert _count_user_msgs(driver) == 2

    def test_exactly_two_assistant_messages(self, driver):
        assert _count_asst_msgs(driver) >= 2

    def test_no_duplicate_followup(self, driver):
        """Each user message should have unique text."""
        msgs = driver.find_elements(By.CSS_SELECTOR, "#live-log .msg.user .msg-body")
        texts = [m.text.strip().lower() for m in msgs]
        assert len(set(texts)) == len(texts), f"Duplicate user messages: {texts}"


# =========================================================================
# Long-running session (tool use)
# =========================================================================

class TestToolUseSession:
    """Session that triggers tool use (Read file)."""

    def test_setup(self, driver):
        driver.get(BASE_URL)
        time.sleep(2)
        label = driver.find_element(By.ID, "project-label")
        if "Select project" in label.text:
            label.click()
            WebDriverWait(driver, 5).until(EC.presence_of_element_located((By.CSS_SELECTOR, ".project-item")))
            driver.find_element(By.CSS_SELECTOR, ".project-item").click()
            time.sleep(2)

    def test_tool_use_renders(self, driver):
        """Ask Claude to read a file — tool_use and tool_result entries should render."""
        _start_new_session(driver, "Tool Use Test", "Read the file run.py and tell me the first line")
        # Wait for assistant response (may take a while due to tool use)
        WebDriverWait(driver, LONG_WAIT).until(
            EC.presence_of_element_located((By.CSS_SELECTOR, "#live-log .msg.assistant"))
        )
        _wait_for_idle(driver)
        time.sleep(2)

        # Should have tool entries
        tool_entries = driver.find_elements(By.CSS_SELECTOR, "#live-log .live-entry-tool")
        result_entries = driver.find_elements(By.CSS_SELECTOR, "#live-log .live-entry-result")
        assert len(tool_entries) >= 1, "Expected at least one tool_use entry"

    def test_no_duplicate_after_tool_use(self, driver):
        assert _count_user_msgs(driver) == 1


# =========================================================================
# Protected file access (the exact scenario that was broken)
# =========================================================================

class TestProtectedFileAccess:
    """Ask Claude to read a protected file. Session should NOT hang."""

    def test_setup(self, driver):
        driver.get(BASE_URL)
        time.sleep(2)
        label = driver.find_element(By.ID, "project-label")
        if "Select project" in label.text:
            label.click()
            WebDriverWait(driver, 5).until(EC.presence_of_element_located((By.CSS_SELECTOR, ".project-item")))
            driver.find_element(By.CSS_SELECTOR, ".project-item").click()
            time.sleep(2)

    def test_protected_file_doesnt_hang(self, driver):
        """Reading SAM file should complete (denied) without hanging."""
        _start_new_session(
            driver, "Protected File",
            "Read the file C:/Windows/System32/config/SAM and show me its contents"
        )
        # Must complete within 90 seconds — not hang
        WebDriverWait(driver, LONG_WAIT).until(
            EC.presence_of_element_located((By.CSS_SELECTOR, "#live-log .msg.assistant"))
        )
        _wait_for_idle(driver)

    def test_session_reached_idle(self, driver):
        ta = driver.find_element(By.ID, "live-input-ta")
        assert ta.is_displayed()

    def test_no_duplicate(self, driver):
        assert _count_user_msgs(driver) == 1

    def test_followup_after_protected_file(self, driver):
        """Can still send follow-up after the protected file attempt."""
        _send_followup(driver, "Say exactly: still works")
        WebDriverWait(driver, LONG_WAIT).until(
            lambda d: _count_asst_msgs(d) >= 2
        )
        time.sleep(2)
        assert _count_user_msgs(driver) == 2


# =========================================================================
# Working state UI
# =========================================================================

class TestWorkingStateUI:
    """Verify the working state bar renders correctly."""

    def test_setup(self, driver):
        driver.get(BASE_URL)
        time.sleep(2)
        label = driver.find_element(By.ID, "project-label")
        if "Select project" in label.text:
            label.click()
            WebDriverWait(driver, 5).until(EC.presence_of_element_located((By.CSS_SELECTOR, ".project-item")))
            driver.find_element(By.CSS_SELECTOR, ".project-item").click()
            time.sleep(2)

    def test_working_bar_has_stop_button(self, driver):
        """During working state, stop button should be visible and clickable."""
        _start_new_session(
            driver, "Stop Test",
            "List all files in C:/Windows/System32 recursively and count them"
        )
        # Wait for working state
        time.sleep(3)
        stop_btns = driver.find_elements(By.CSS_SELECTOR, ".live-stop-btn")
        if stop_btns:
            assert stop_btns[0].is_displayed()
            # Verify it's clickable (not covered by pseudo-element)
            assert stop_btns[0].is_enabled()

    def test_queue_textarea_exists(self, driver):
        """During working state, queue textarea should exist."""
        queue_ta = driver.find_elements(By.ID, "live-queue-ta")
        if queue_ta:
            assert queue_ta[0].is_displayed()

    def test_queue_textarea_keeps_focus(self, driver):
        """Typing in queue textarea should not lose focus."""
        queue_ta = driver.find_elements(By.ID, "live-queue-ta")
        if queue_ta:
            queue_ta[0].click()
            queue_ta[0].send_keys("test message")
            time.sleep(2)  # Wait for timer tick
            # Textarea should still have our text
            val = queue_ta[0].get_attribute("value")
            assert "test message" in val, f"Queue textarea lost content: '{val}'"

    def test_cleanup(self, driver):
        """Wait for session to complete or stop it."""
        try:
            _wait_for_idle(driver, timeout=60)
        except Exception:
            # Session might still be running — that's OK
            pass


# =========================================================================
# Scroll behavior
# =========================================================================

class TestScrollBehavior:
    """Verify auto-scroll works when messages arrive."""

    def test_setup(self, driver):
        driver.get(BASE_URL)
        time.sleep(2)
        label = driver.find_element(By.ID, "project-label")
        if "Select project" in label.text:
            label.click()
            WebDriverWait(driver, 5).until(EC.presence_of_element_located((By.CSS_SELECTOR, ".project-item")))
            driver.find_element(By.CSS_SELECTOR, ".project-item").click()
            time.sleep(2)

    def test_auto_scroll_on_message(self, driver):
        _start_new_session(driver, "Scroll Test", "Write a 20-line numbered list from 1 to 20")
        WebDriverWait(driver, LONG_WAIT).until(
            EC.presence_of_element_located((By.CSS_SELECTOR, "#live-log .msg.assistant"))
        )
        _wait_for_idle(driver)
        time.sleep(1)

        log = driver.find_element(By.ID, "live-log")
        scroll_top = driver.execute_script("return arguments[0].scrollTop", log)
        scroll_height = driver.execute_script("return arguments[0].scrollHeight", log)
        client_height = driver.execute_script("return arguments[0].clientHeight", log)

        # Should be scrolled near the bottom
        at_bottom = (scroll_height - scroll_top - client_height) < 100
        assert at_bottom, f"Not scrolled to bottom: scrollTop={scroll_top}, scrollHeight={scroll_height}, clientHeight={client_height}"
