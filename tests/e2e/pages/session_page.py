"""Page Object for session creation and live chat panel."""

from selenium.webdriver.common.by import By
from selenium.webdriver.common.keys import Keys
from .base_page import BasePage


class SessionPage(BasePage):
    """Page object for the New Session dialog and live chat panel."""

    # --- Locators ---
    ADD_SESSION_BTN = (By.ID, "btn-add-agent")
    SESSION_NAME_INPUT = (By.ID, "ns-name")
    SESSION_MSG_INPUT = (By.ID, "ns-message")
    START_SESSION_BTN = (By.ID, "ns-start")
    LIVE_PANEL = (By.ID, "live-panel")
    LIVE_LOG = (By.ID, "live-log")
    IDLE_TEXTAREA = (By.ID, "live-input-ta")
    USER_MESSAGES = (By.CSS_SELECTOR, "#live-log .msg.user")
    ASSISTANT_MESSAGES = (By.CSS_SELECTOR, "#live-log .msg.assistant")
    TOOL_ENTRIES = (By.CSS_SELECTOR, "#live-log .live-entry-tool")
    STOP_BUTTON = (By.CSS_SELECTOR, ".live-stop-btn")
    QUEUE_TEXTAREA = (By.ID, "live-queue-ta")
    LIVE_STATUS = (By.CSS_SELECTOR, ".live-status")
    LIVE_SPINNER = (By.CSS_SELECTOR, ".live-spinner.active")

    # --- Actions ---

    def start_new_session(self, name, message):
        """Open New Session dialog, fill name+message, submit, wait for live panel."""
        self.wait_clickable(*self.ADD_SESSION_BTN).click()
        self.wait_visible(*self.SESSION_NAME_INPUT).send_keys(name)
        self.driver.find_element(*self.SESSION_MSG_INPUT).send_keys(message)
        self.driver.find_element(*self.START_SESSION_BTN).click()
        self.wait_for(*self.LIVE_PANEL)

    def click_new_session_button(self):
        """Click the Add Session button."""
        self.wait_clickable(*self.ADD_SESSION_BTN).click()

    def send_followup(self, text):
        """Type text in idle textarea and Ctrl+Enter to submit."""
        ta = self.wait_for(*self.IDLE_TEXTAREA)
        ta.clear()
        ta.send_keys(text)
        ta.send_keys(Keys.CONTROL, Keys.ENTER)

    def wait_for_idle(self, timeout=None):
        """Wait for session to return to idle state (textarea visible)."""
        timeout = timeout or self.LONG_TIMEOUT
        return self.wait_for(*self.IDLE_TEXTAREA, timeout=timeout)

    def wait_for_assistant_response(self, min_count=1, timeout=None):
        """Wait for at least N assistant messages to appear."""
        timeout = timeout or self.LONG_TIMEOUT
        self.wait_count(self.ASSISTANT_MESSAGES[1], min_count, timeout)

    def wait_for_working(self, timeout=None):
        """Wait for session to enter working state (spinner active)."""
        timeout = timeout or self.DEFAULT_TIMEOUT
        self.wait_for(*self.LIVE_SPINNER, timeout=timeout)

    # --- Queries ---

    def user_message_count(self):
        """Return number of user messages visible in the log."""
        return len(self.driver.find_elements(*self.USER_MESSAGES))

    def assistant_message_count(self):
        """Return number of assistant messages visible in the log."""
        return len(self.driver.find_elements(*self.ASSISTANT_MESSAGES))

    def tool_entry_count(self):
        """Return number of tool-use entries visible in the log."""
        return len(self.driver.find_elements(*self.TOOL_ENTRIES))

    def get_status_text(self):
        """Return the live session status text."""
        els = self.driver.find_elements(*self.LIVE_STATUS)
        return els[0].text.lower() if els else ""

    def is_idle(self):
        """Check if the session is currently idle."""
        status = self.get_status_text()
        return "idle" in status or "stopped" in status
