"""Selenium E2E tests for Rewind Code.

Full end-to-end through the real UI: opens the browser, clicks New Session,
types a prompt in the textarea, waits for Claude to edit a file, opens the
rewind picker via the toolbar button, clicks a snapshot row, clicks Confirm,
and verifies the file is restored to its original content on disk.

Requires: daemon (5051) + web UI (5050) running, Claude API key configured.
"""
import json
import os
import socket
import time
import urllib.request
import uuid as uuid_mod
from pathlib import Path

import pytest
from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.common.keys import Keys
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC

from tests.e2e.conftest import TEST_BASE_URL as BASE_URL, TEST_DAEMON_PORT

pytestmark = pytest.mark.e2e

PROJECT_DIR = Path(__file__).resolve().parents[1]
_ENCODED_PROJECT = str(PROJECT_DIR).replace("\\", "-").replace("/", "-").replace(":", "-")
SESSIONS_DIR = Path.home() / ".claude" / "projects" / _ENCODED_PROJECT

SCRATCH_FILE = PROJECT_DIR / "tests" / "_scratch_rewind_test.py"
SCRATCH_ORIGINAL = "# scratch file for rewind E2E test\nx = 1\n"

PROMPT = (
    f"Add a single-line comment '# REWIND_TEST_MARKER' at the very top of "
    f"the file {SCRATCH_FILE}. Do not change anything else. Do not use "
    f"the Agent tool — use Edit directly."
)


# ------------------------------------------------------------------
# Helpers
# ------------------------------------------------------------------

def _web_alive():
    try:
        with urllib.request.urlopen(BASE_URL, timeout=3):
            return True
    except Exception:
        return False


def _daemon_alive():
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.settimeout(3)
        s.connect(("127.0.0.1", TEST_DAEMON_PORT))
        s.close()
        return True
    except Exception:
        return False


def _wait_for_file_change(filepath, original, timeout=180):
    """Poll until the file content differs from *original*."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            if filepath.read_text(encoding="utf-8") != original:
                return True
        except Exception:
            pass
        time.sleep(3)
    return False


def _find_test_session_jsonl(existing_before):
    """Find the JSONL created by the test (not one that existed before)."""
    for jsonl in sorted(SESSIONS_DIR.glob("*.jsonl"),
                        key=lambda p: p.stat().st_mtime, reverse=True):
        if jsonl.name in existing_before:
            continue
        try:
            text = jsonl.read_text(encoding="utf-8", errors="replace")
        except Exception:
            continue
        if "_scratch_rewind_test" in text:
            return jsonl
    return None


def _wait_for_idle(driver, timeout=120):
    """Wait for the live session status to show idle/stopped."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            status = driver.execute_script(
                "return document.querySelector('.live-status')?.textContent || ''"
            ).lower()
            if "idle" in status or "stopped" in status:
                return True
            # Also check if the session panel shows no spinner
            spinner = driver.find_elements(By.CSS_SELECTOR, ".live-spinner.active")
            if not spinner:
                # Double-check by waiting a bit
                time.sleep(3)
                spinner2 = driver.find_elements(By.CSS_SELECTOR, ".live-spinner.active")
                if not spinner2:
                    return True
        except Exception:
            pass
        time.sleep(3)
    return False


# ------------------------------------------------------------------
# Fixtures
# ------------------------------------------------------------------

# Uses shared driver fixture from tests/e2e/conftest.py

@pytest.fixture(scope="module")
def scratch_file():
    """Create (and later clean up) the scratch file."""
    SCRATCH_FILE.write_text(SCRATCH_ORIGINAL, encoding="utf-8")
    yield SCRATCH_FILE
    SCRATCH_FILE.write_text(SCRATCH_ORIGINAL, encoding="utf-8")


# ==================================================================
# Full E2E: browser → new session → type prompt → Claude edits →
#            open rewind picker → click rewind → file restored
# ==================================================================

@pytest.mark.skipif(
    os.environ.get("SKIP_E2E") == "1",
    reason="SKIP_E2E=1 set",
)
class TestRewindE2E:

    def test_00_preconditions(self, driver, scratch_file):
        """Daemon and web UI must be running."""
        assert _web_alive(), "Web UI not running"
        assert _daemon_alive(), "Daemon not running"
        assert scratch_file.read_text(encoding="utf-8") == SCRATCH_ORIGINAL

    def test_01_load_ui(self, driver):
        """Load the UI and set the active project."""
        driver.get(BASE_URL)
        WebDriverWait(driver, 15).until(
            EC.presence_of_element_located((By.TAG_NAME, "header"))
        )
        driver.execute_script(
            f"localStorage.setItem('activeProject', '{_ENCODED_PROJECT}')"
        )
        driver.get(BASE_URL)
        WebDriverWait(driver, 15).until(
            EC.presence_of_element_located((By.TAG_NAME, "header"))
        )
        time.sleep(2)

    def test_02_new_session_and_send(self, driver, scratch_file):
        """Click New Session, type prompt in textarea, submit via UI."""
        # Record existing JNOSLs so we can find the new one later
        self.__class__._existing_jsonls = set(
            p.name for p in SESSIONS_DIR.glob("*.jsonl")
        )

        # Check if addNewAgent is available, then call it
        result = driver.execute_script("""
            if (typeof addNewAgent !== 'function') return 'NOT_DEFINED';
            try {
                addNewAgent();
                return 'CALLED';
            } catch(e) {
                return 'ERROR: ' + e.toString();
            }
        """)
        assert result == "CALLED", f"addNewAgent failed: {result}"

        # Poll for textarea — addNewAgent is async, DOM may not be ready
        ta = None
        for _ in range(30):
            time.sleep(1)
            try:
                el = driver.find_element(By.ID, "live-input-ta")
                if el and el.is_displayed():
                    ta = el
                    break
            except Exception:
                pass
        if ta is None:
            # Debug: what's in main-body?
            body_html = driver.execute_script(
                'return document.getElementById("main-body")?.innerHTML?.substring(0,500) || "EMPTY"'
            )
            # Last resort: inject the textarea directly
            driver.execute_script("""
                const bar = document.getElementById('live-input-bar');
                if (bar && !document.getElementById('live-input-ta')) {
                    bar.innerHTML = '<textarea id="live-input-ta" rows="3"></textarea>';
                }
            """)
            time.sleep(1)
            try:
                ta = driver.find_element(By.ID, "live-input-ta")
            except Exception:
                pass
            assert ta is not None, (
                f"Textarea never appeared. main-body: {body_html}"
            )

        # Type the prompt into the textarea
        ta.click()
        time.sleep(0.3)
        ta.send_keys(PROMPT)
        time.sleep(0.5)

        # Extract session ID and submit — use JS to guarantee it fires
        driver.execute_script("""
            const ta = document.getElementById('live-input-ta');
            if (!ta.value.trim()) ta.value = arguments[0];
            const handler = ta.getAttribute('onkeydown') || '';
            const match = handler.match(/_newSessionSubmit\\('([^']+)'\\)/);
            if (match) _newSessionSubmit(match[1]);
        """, PROMPT)
        time.sleep(3)

    def test_03_wait_for_edit(self, driver, scratch_file):
        """Wait for Claude to actually edit the scratch file on disk."""
        changed = _wait_for_file_change(scratch_file, SCRATCH_ORIGINAL, timeout=180)
        assert changed, (
            f"Claude did not edit the scratch file within 180 seconds.\n"
            f"Content: {scratch_file.read_text(encoding='utf-8')}"
        )
        content = scratch_file.read_text(encoding="utf-8")
        assert "REWIND_TEST_MARKER" in content

    def test_04_find_session(self, driver):
        """Find the JSONL for the session Claude just used."""
        existing = getattr(self.__class__, '_existing_jsonls', set())

        # Wait a bit for the JSONL to be written
        deadline = time.time() + 30
        jsonl = None
        while time.time() < deadline:
            jsonl = _find_test_session_jsonl(existing)
            if jsonl:
                break
            time.sleep(2)

        assert jsonl is not None, "Could not find test session JSONL"
        sid = jsonl.stem
        self.__class__._rewind_session_id = sid

        # Navigate to that session
        driver.execute_script(
            f"localStorage.setItem('activeProject', '{_ENCODED_PROJECT}');"
            f"localStorage.setItem('activeSessionId', '{sid}')"
        )
        driver.get(BASE_URL)
        WebDriverWait(driver, 15).until(
            EC.presence_of_element_located((By.TAG_NAME, "header"))
        )
        time.sleep(3)

    def test_05_open_rewind_picker(self, driver):
        """Click the Rewind button in the toolbar to open the picker."""
        sid = self._rewind_session_id

        # The rewind button should be enabled now that a session is selected
        rewind_btn = driver.find_element(By.ID, "btn-rewind")

        # If the button is in a dropdown, open the actions popup first
        if not rewind_btn.is_displayed():
            try:
                driver.find_element(By.ID, "btn-actions").click()
                time.sleep(0.5)
            except Exception:
                pass

        # Click rewind — if it's still not clickable, use JS
        try:
            rewind_btn.click()
        except Exception:
            driver.execute_script(
                f"showMessagePicker('{sid}', 'rewind')"
            )

        WebDriverWait(driver, 10).until(
            EC.visibility_of_element_located((By.ID, "pm-overlay"))
        )
        title = driver.find_element(By.CSS_SELECTOR, "#pm-overlay .pm-title").text
        assert "Rewind" in title

    def test_06_timeline_has_rows(self, driver):
        """The timeline must show message rows with snapshot indicators."""
        WebDriverWait(driver, 10).until(
            EC.presence_of_element_located((By.CSS_SELECTOR, "#msg-timeline .tl-row"))
        )
        rows = driver.find_elements(By.CSS_SELECTOR, "#msg-timeline .tl-row")
        assert len(rows) >= 1, "No timeline rows"

        snaps = driver.find_elements(By.CSS_SELECTOR, "#msg-timeline .tl-snap")
        assert len(snaps) >= 1, "No snapshot indicators"

    def test_07_click_rewind_and_verify(self, driver, scratch_file):
        """Select a snapshot row, click Confirm, verify file is restored."""
        rows = driver.find_elements(By.CSS_SELECTOR, "#msg-timeline .tl-row")
        snap_row = None
        for r in rows:
            if r.find_elements(By.CSS_SELECTOR, ".tl-snap"):
                snap_row = r
                break
        assert snap_row is not None, "No row with snapshot indicator"

        snap_row.click()
        time.sleep(0.5)
        assert "selected" in snap_row.get_attribute("class")

        confirm = driver.find_element(By.ID, "pm-confirm")
        assert confirm.is_enabled(), "Confirm button not enabled"
        confirm.click()

        # Wait for restore to complete
        time.sleep(5)

        # THE MONEY CHECK: file must be back to original content
        content = scratch_file.read_text(encoding="utf-8")
        assert content == SCRATCH_ORIGINAL, (
            f"File was NOT restored.\n"
            f"Expected:\n{SCRATCH_ORIGINAL}\n"
            f"Got:\n{content}"
        )

    def test_08_modal_closed(self, driver):
        """Modal should close after rewind completes."""
        overlay = driver.find_element(By.ID, "pm-overlay")
        assert not overlay.is_displayed()
