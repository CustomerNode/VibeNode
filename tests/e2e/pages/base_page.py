"""Base Page Object — common waits, navigation, and JS helpers.

All page objects inherit from this class.
"""

import time
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC


class BasePage:
    """Base class for all page objects."""

    DEFAULT_TIMEOUT = 10
    LONG_TIMEOUT = 90

    def __init__(self, driver, base_url):
        self.driver = driver
        self.base_url = base_url

    # ------------------------------------------------------------------
    # Navigation
    # ------------------------------------------------------------------

    def navigate(self, path=""):
        """Navigate to a path under the base URL."""
        self.driver.get(f"{self.base_url}{path}")

    def refresh(self):
        """Refresh the current page."""
        self.driver.refresh()

    # ------------------------------------------------------------------
    # Wait helpers
    # ------------------------------------------------------------------

    def wait_for(self, by, value, timeout=None):
        """Wait for element to be present and return it."""
        timeout = timeout or self.DEFAULT_TIMEOUT
        return WebDriverWait(self.driver, timeout).until(
            EC.presence_of_element_located((by, value))
        )

    def wait_visible(self, by, value, timeout=None):
        """Wait for element to be visible and return it."""
        timeout = timeout or self.DEFAULT_TIMEOUT
        return WebDriverWait(self.driver, timeout).until(
            EC.visibility_of_element_located((by, value))
        )

    def wait_clickable(self, by, value, timeout=None):
        """Wait for element to be clickable and return it."""
        timeout = timeout or self.DEFAULT_TIMEOUT
        return WebDriverWait(self.driver, timeout).until(
            EC.element_to_be_clickable((by, value))
        )

    def wait_invisible(self, by, value, timeout=None):
        """Wait for element to become invisible or removed from DOM."""
        timeout = timeout or self.DEFAULT_TIMEOUT
        return WebDriverWait(self.driver, timeout).until(
            EC.invisibility_of_element_located((by, value))
        )

    def wait_text(self, by, value, text, timeout=None):
        """Wait until element contains the expected text."""
        timeout = timeout or self.DEFAULT_TIMEOUT
        return WebDriverWait(self.driver, timeout).until(
            EC.text_to_be_present_in_element((by, value), text)
        )

    def wait_count(self, css_selector, min_count, timeout=None):
        """Wait until at least min_count matching elements exist."""
        timeout = timeout or self.DEFAULT_TIMEOUT
        return WebDriverWait(self.driver, timeout).until(
            lambda d: len(d.find_elements(By.CSS_SELECTOR, css_selector)) >= min_count
        )

    # ------------------------------------------------------------------
    # JavaScript helpers
    # ------------------------------------------------------------------

    def js(self, script, *args):
        """Execute JavaScript and return result."""
        return self.driver.execute_script(script, *args)

    def wait_js(self, expression, timeout=None):
        """Wait until JS expression is truthy."""
        timeout = timeout or self.DEFAULT_TIMEOUT
        return WebDriverWait(self.driver, timeout).until(
            lambda d: d.execute_script(f"return !!({expression})")
        )

    # ------------------------------------------------------------------
    # VibeNode common helpers
    # ------------------------------------------------------------------

    def wait_for_js_ready(self, timeout=None):
        """Wait until core JS bundles are loaded."""
        timeout = timeout or self.LONG_TIMEOUT
        self.wait_js('typeof setViewMode === "function"', timeout)

    def ensure_project_selected(self):
        """Select the first project if none is selected, dismiss modals."""
        self.js('''
            if(typeof _allProjects!=="undefined" && _allProjects.length>0)
                setProject(_allProjects[0].encoded, true);
            document.querySelectorAll(".show").forEach(function(e){
                e.classList.remove("show")
            });
        ''')

    def set_test_project(self, project_id="__selenium_test__"):
        """Switch to an isolated test project via API."""
        self.js('''
            fetch("/api/set-project", {
                method: "POST",
                headers: {"Content-Type": "application/json"},
                body: JSON.stringify({project: arguments[0]})
            });
        ''', project_id)

    def dismiss_modals(self):
        """Close any open modals/dropdowns."""
        self.js('document.querySelectorAll(".show").forEach(function(e){e.classList.remove("show")})')

    def settle(self, seconds=0.3):
        """Brief pause to let a DOM mutation/animation settle.

        Use sparingly — prefer explicit waits. Max 0.5s.
        """
        time.sleep(min(seconds, 0.5))
