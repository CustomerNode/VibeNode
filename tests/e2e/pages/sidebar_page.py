"""Page Object for the session sidebar and project selector."""

from selenium.webdriver.common.by import By
from .base_page import BasePage


class SidebarPage(BasePage):
    """Page object for the sidebar, project selector, and session list."""

    # --- Locators ---
    SIDEBAR = (By.ID, "sidebar")
    PROJECT_SELECTOR = (By.ID, "project-selector")
    PROJECT_ITEMS = (By.CSS_SELECTOR, ".project-item")
    SESSION_LIST = (By.CSS_SELECTOR, ".session-list")
    SESSION_ITEMS = (By.CSS_SELECTOR, ".session-item")
    ACTIVE_SESSION = (By.CSS_SELECTOR, ".session-item.active")
    SIDEBAR_TOGGLE = (By.ID, "sidebar-toggle")

    # --- Actions ---

    def select_project(self, index=0):
        """Click a project in the project selector by index."""
        items = self.driver.find_elements(*self.PROJECT_ITEMS)
        if items and index < len(items):
            items[index].click()

    def select_session(self, index=0):
        """Click a session in the sidebar by index."""
        items = self.driver.find_elements(*self.SESSION_ITEMS)
        if items and index < len(items):
            items[index].click()

    def toggle_sidebar(self):
        """Toggle sidebar visibility."""
        self.wait_clickable(*self.SIDEBAR_TOGGLE).click()

    # --- Queries ---

    def session_count(self):
        """Return number of sessions in the sidebar list."""
        return len(self.driver.find_elements(*self.SESSION_ITEMS))

    def get_active_session_text(self):
        """Return the text of the active (selected) session."""
        els = self.driver.find_elements(*self.ACTIVE_SESSION)
        return els[0].text if els else ""

    def is_sidebar_visible(self):
        """Check if the sidebar is visible."""
        els = self.driver.find_elements(*self.SIDEBAR)
        return els[0].is_displayed() if els else False
