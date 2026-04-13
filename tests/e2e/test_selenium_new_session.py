"""Selenium end-to-end test: New Session flow.

Opens the real GUI in Chrome, clicks New Session, fills the form,
submits, and verifies Claude responds in the chat panel.
"""

import time
import pytest
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.common.keys import Keys

from tests.e2e.conftest import TEST_BASE_URL as BASE_URL


class TestNewSessionFlow:
    """The #1 user flow: click New Session, type a message, get a response."""

    def test_page_loads(self, driver):
        """GUI loads and shows the app title."""
        driver.get(BASE_URL)
        WebDriverWait(driver, 10).until(
            EC.presence_of_element_located((By.TAG_NAME, "header"))
        )
        assert "Claude" in driver.title or "Claude" in driver.page_source

    def test_select_project(self, driver):
        """If no project is selected, select one."""
        driver.get(BASE_URL)
        time.sleep(2)
        # Check if sessions loaded (project selected) or we need to pick one
        project_label = driver.find_element(By.ID, "project-label")
        if "Select project" in project_label.text:
            # Open project overlay and pick the first project
            project_label.click()
            WebDriverWait(driver, 5).until(
                EC.presence_of_element_located((By.CSS_SELECTOR, ".project-item"))
            )
            driver.find_element(By.CSS_SELECTOR, ".project-item").click()
            time.sleep(2)

        # Project should now be selected
        assert "Select project" not in project_label.text

    def test_click_new_session(self, driver):
        """Click the + button to open the New Session dialog."""
        # Find and click the new session button
        add_btn = driver.find_element(By.ID, "btn-add-agent")
        add_btn.click()

        # Wait for the dialog to appear
        WebDriverWait(driver, 5).until(
            EC.visibility_of_element_located((By.ID, "ns-name"))
        )

        # Dialog should be visible with name and message fields
        name_input = driver.find_element(By.ID, "ns-name")
        msg_input = driver.find_element(By.ID, "ns-message")
        assert name_input.is_displayed()
        assert msg_input.is_displayed()

    def test_fill_and_submit_new_session(self, driver):
        """Fill in name and message, click Start Session."""
        name_input = driver.find_element(By.ID, "ns-name")
        msg_input = driver.find_element(By.ID, "ns-message")

        name_input.clear()
        name_input.send_keys("Selenium Test Session")

        msg_input.clear()
        msg_input.send_keys("Say exactly: selenium works")

        # Click Start Session
        start_btn = driver.find_element(By.ID, "ns-start")
        start_btn.click()

        # Dialog should close
        time.sleep(1)
        overlay = driver.find_element(By.ID, "pm-overlay")
        assert "show" not in overlay.get_attribute("class")

    def test_live_panel_appears(self, driver):
        """After starting, the live panel should appear with the chat log."""
        WebDriverWait(driver, 5).until(
            EC.presence_of_element_located((By.ID, "live-panel"))
        )
        live_log = driver.find_element(By.ID, "live-log")
        assert live_log.is_displayed()

    def test_user_message_appears(self, driver):
        """The user's prompt should appear in the chat log."""
        WebDriverWait(driver, 10).until(
            EC.presence_of_element_located((By.CSS_SELECTOR, "#live-log .msg.user"))
        )
        user_msgs = driver.find_elements(By.CSS_SELECTOR, "#live-log .msg.user")
        assert len(user_msgs) >= 1
        # Check user message text
        user_text = user_msgs[-1].text
        assert "selenium works" in user_text.lower()

    def test_assistant_response_appears(self, driver):
        """Claude should respond. Wait up to 60 seconds."""
        WebDriverWait(driver, 60).until(
            EC.presence_of_element_located((By.CSS_SELECTOR, "#live-log .msg.assistant"))
        )
        asst_msgs = driver.find_elements(By.CSS_SELECTOR, "#live-log .msg.assistant")
        assert len(asst_msgs) >= 1
        # Claude was asked to say "selenium works"
        asst_text = asst_msgs[-1].text.lower()
        assert "selenium" in asst_text or "works" in asst_text

    def test_session_goes_idle(self, driver):
        """After Claude responds, session should show as idle (input bar visible)."""
        WebDriverWait(driver, 30).until(
            EC.presence_of_element_located((By.ID, "live-input-ta"))
        )
        ta = driver.find_element(By.ID, "live-input-ta")
        assert ta.is_displayed()
        # The textarea should have a "next command" placeholder
        placeholder = ta.get_attribute("placeholder")
        assert "next" in placeholder.lower() or "command" in placeholder.lower() or "message" in placeholder.lower()

    def test_send_followup_message(self, driver):
        """Type a follow-up message and send it."""
        ta = driver.find_element(By.ID, "live-input-ta")
        ta.clear()
        ta.send_keys("Say exactly: followup received")
        # Ctrl+Enter to send
        ta.send_keys(Keys.CONTROL, Keys.ENTER)

        # Wait for the follow-up user message to appear
        time.sleep(2)
        user_msgs = driver.find_elements(By.CSS_SELECTOR, "#live-log .msg.user")
        assert len(user_msgs) >= 2

    def test_followup_response_appears(self, driver):
        """Claude should respond to the follow-up."""
        # Wait for a new assistant message (there should be at least 2 now)
        WebDriverWait(driver, 60).until(
            lambda d: len(d.find_elements(By.CSS_SELECTOR, "#live-log .msg.assistant")) >= 2
        )
        asst_msgs = driver.find_elements(By.CSS_SELECTOR, "#live-log .msg.assistant")
        assert len(asst_msgs) >= 2
        last_text = asst_msgs[-1].text.lower()
        assert "followup" in last_text or "received" in last_text

    def test_no_duplicate_user_messages(self, driver):
        """After full conversation, each user message should appear exactly once."""
        time.sleep(2)  # Let any late events settle
        user_msgs = driver.find_elements(By.CSS_SELECTOR, "#live-log .msg.user")
        # Should be exactly 2: initial prompt + follow-up
        assert len(user_msgs) == 2, f"Expected 2 user messages, got {len(user_msgs)}"
        # Verify they're different messages (not duplicates)
        texts = [m.find_element(By.CSS_SELECTOR, ".msg-body").text.strip().lower() for m in user_msgs]
        assert len(set(texts)) == 2, f"User messages are duplicates: {texts}"


class TestExistingSessionView:
    """Verify that clicking an existing session loads its chat history."""

    def test_click_existing_session(self, driver):
        """Click a session in the sidebar and verify messages load."""
        driver.get(BASE_URL)
        time.sleep(3)

        # Find session items in the sidebar
        items = driver.find_elements(By.CSS_SELECTOR, ".session-item[data-sid]")
        if not items:
            pytest.skip("No sessions in sidebar")

        # Click the first one with content (non-empty)
        items[0].click()
        time.sleep(2)

        # Should show conversation or live panel
        main_body = driver.find_element(By.ID, "main-body")
        html = main_body.get_attribute("innerHTML")
        assert len(html) > 100  # Not empty

    def test_double_click_opens_live_panel(self, driver):
        """Double-click a session to open the live panel."""
        items = driver.find_elements(By.CSS_SELECTOR, ".session-item[data-sid]")
        if not items:
            pytest.skip("No sessions in sidebar")

        # Double-click
        from selenium.webdriver.common.action_chains import ActionChains
        ActionChains(driver).double_click(items[0]).perform()

        WebDriverWait(driver, 10).until(
            EC.presence_of_element_located((By.ID, "live-log"))
        )
        live_log = driver.find_element(By.ID, "live-log")
        assert live_log.is_displayed()
