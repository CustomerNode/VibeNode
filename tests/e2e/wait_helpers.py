"""Reusable explicit-wait conditions for VibeNode E2E tests.

Replaces time.sleep() calls with deterministic waits that are faster
and more reliable.  Import these in any E2E test file:

    from tests.e2e.wait_helpers import wait_for_element, wait_for_idle, ...
"""

from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC

DEFAULT_TIMEOUT = 10
LONG_TIMEOUT = 90  # Claude API responses


# ------------------------------------------------------------------
# Generic element waits
# ------------------------------------------------------------------

def wait_for_element(driver, by, value, timeout=DEFAULT_TIMEOUT):
    """Wait for element to be present in the DOM and return it."""
    return WebDriverWait(driver, timeout).until(
        EC.presence_of_element_located((by, value))
    )


def wait_for_visible(driver, by, value, timeout=DEFAULT_TIMEOUT):
    """Wait for element to be visible and return it."""
    return WebDriverWait(driver, timeout).until(
        EC.visibility_of_element_located((by, value))
    )


def wait_for_clickable(driver, by, value, timeout=DEFAULT_TIMEOUT):
    """Wait for element to be clickable and return it."""
    return WebDriverWait(driver, timeout).until(
        EC.element_to_be_clickable((by, value))
    )


def wait_for_invisible(driver, by, value, timeout=DEFAULT_TIMEOUT):
    """Wait for element to become invisible or be removed from DOM."""
    return WebDriverWait(driver, timeout).until(
        EC.invisibility_of_element_located((by, value))
    )


def wait_for_text_in(driver, by, value, text, timeout=DEFAULT_TIMEOUT):
    """Wait until element contains the expected text."""
    return WebDriverWait(driver, timeout).until(
        EC.text_to_be_present_in_element((by, value), text)
    )


def wait_for_element_count(driver, css_selector, min_count, timeout=DEFAULT_TIMEOUT):
    """Wait until at least min_count elements matching selector exist."""
    return WebDriverWait(driver, timeout).until(
        lambda d: len(d.find_elements(By.CSS_SELECTOR, css_selector)) >= min_count
    )


# ------------------------------------------------------------------
# JavaScript waits
# ------------------------------------------------------------------

def wait_for_js_truthy(driver, script, timeout=DEFAULT_TIMEOUT):
    """Wait until a JS expression returns truthy."""
    return WebDriverWait(driver, timeout).until(
        lambda d: d.execute_script(f"return !!({script})")
    )


def wait_for_js_ready(driver, timeout=DEFAULT_TIMEOUT):
    """Wait until core JS bundles are loaded (setViewMode defined)."""
    return wait_for_js_truthy(driver, 'typeof setViewMode === "function"', timeout)


# ------------------------------------------------------------------
# VibeNode-specific waits
# ------------------------------------------------------------------

def wait_for_idle(driver, timeout=LONG_TIMEOUT):
    """Wait for session to return to idle state (textarea visible)."""
    return wait_for_element(driver, By.ID, "live-input-ta", timeout)


def wait_for_board_rendered(driver, timeout=DEFAULT_TIMEOUT):
    """Wait for kanban board columns or empty state to render."""
    return WebDriverWait(driver, timeout).until(
        lambda d: d.execute_script(
            'return document.querySelector(".kanban-columns-wrapper") !== null '
            '|| document.querySelector(".kanban-empty-state") !== null'
        )
    )


def wait_for_msg_count(driver, css_selector, min_count, timeout=LONG_TIMEOUT):
    """Wait until at least min_count messages matching selector exist."""
    return wait_for_element_count(driver, css_selector, min_count, timeout)


def wait_for_overlay_visible(driver, timeout=DEFAULT_TIMEOUT):
    """Wait for the pm-overlay to become visible."""
    return wait_for_visible(driver, By.ID, "pm-overlay", timeout)


def wait_for_overlay_hidden(driver, timeout=DEFAULT_TIMEOUT):
    """Wait for the pm-overlay to disappear."""
    return wait_for_invisible(driver, By.ID, "pm-overlay", timeout)


def wait_for_toast(driver, timeout=DEFAULT_TIMEOUT):
    """Wait for a toast notification to appear."""
    return wait_for_visible(driver, By.CSS_SELECTOR, ".toast-notification", timeout)


def wait_for_status(driver, status_text, timeout=LONG_TIMEOUT):
    """Wait for the live session status to contain the given text."""
    return WebDriverWait(driver, timeout).until(
        lambda d: status_text.lower() in (
            d.execute_script(
                "return document.querySelector('.live-status')?.textContent || ''"
            ).lower()
        )
    )


def wait_for_fetch_complete(driver, timeout=DEFAULT_TIMEOUT):
    """Wait for any pending fetch calls to settle (200ms quiet period)."""
    driver.execute_script("""
        window.__fetchSettled = false;
        setTimeout(function() { window.__fetchSettled = true; }, 200);
    """)
    return WebDriverWait(driver, timeout).until(
        lambda d: d.execute_script("return window.__fetchSettled === true")
    )
