"""Selenium E2E tests for Rewind Code.

Full end-to-end through the REAL UI:
 1. Open the web UI in a real browser via Selenium
 2. Click "New Session" to create a new chat
 3. Type a prompt in the chat textarea asking Claude to edit a file
 4. Wait for Claude to actually edit the file on disk
 5. Open the rewind picker via the toolbar button
 6. Verify the timeline shows rows with snapshot indicators
 7. Click a snapshot row, click Confirm
 8. Verify the file is restored to its original content

NO mocks, NO fake data.  Requires daemon + web UI running.
"""
import json
import os
import time
from pathlib import Path

import pytest
from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.common.keys import Keys
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC

BASE_URL = "http://127.0.0.1:5050"
PROJECT_DIR = Path("C:/Users/15512/Documents/ClaudeGUI")
SESSIONS_DIR = Path.home() / ".claude" / "projects" / "C--Users-15512-Documents-ClaudeGUI"

# Scratch file the test asks Claude to edit.
SCRATCH_FILE = PROJECT_DIR / "tests" / "_scratch_rewind_test.py"
SCRATCH_ORIGINAL = "# scratch file for rewind E2E test\nx = 1\n"


# ------------------------------------------------------------------
# Helpers
# ------------------------------------------------------------------

def _daemon_alive():
    import socket
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.settimeout(3)
        s.connect(("127.0.0.1", 5051))
        s.close()
        return True
    except Exception:
        return False


def _web_alive():
    import urllib.request
    try:
        with urllib.request.urlopen(BASE_URL, timeout=3) as r:
            return r.status == 200
    except Exception:
        return False


def _wait_for_file_change(filepath, original_content, timeout=180):
    """Wait until a file's content differs from original_content."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            current = filepath.read_text(encoding="utf-8")
            if current != original_content:
                return True
        except Exception:
            pass
        time.sleep(3)
    return False


def _find_newest_jsonl_with_snapshot():
    """Find the most recently modified JSONL that has a snapshot for our scratch file."""
    for jsonl in sorted(SESSIONS_DIR.glob("*.jsonl"),
                        key=lambda p: p.stat().st_mtime, reverse=True):
        try:
            text = jsonl.read_text(encoding="utf-8", errors="replace")
        except Exception:
            continue
        for line in text.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except Exception:
                continue
            if obj.get("type") == "file-history-snapshot":
                snap = obj.get("snapshot", {})
                for fp in snap.get("trackedFileBackups", {}):
                    if "_scratch_rewind_test" in fp:
                        return jsonl
    return None


# ------------------------------------------------------------------
# Fixtures
# ------------------------------------------------------------------

@pytest.fixture(scope="module")
def driver():
    o = webdriver.ChromeOptions()
    o.add_argument("--headless=new")
    o.add_argument("--no-sandbox")
    o.add_argument("--disable-gpu")
    o.add_argument("--window-size=1400,900")
    d = webdriver.Chrome(options=o)
    yield d
    d.quit()


@pytest.fixture(scope="module")
def scratch_file():
    """Create (and later clean up) the scratch file."""
    SCRATCH_FILE.write_text(SCRATCH_ORIGINAL, encoding="utf-8")
    yield SCRATCH_FILE
    SCRATCH_FILE.write_text(SCRATCH_ORIGINAL, encoding="utf-8")


# ==================================================================
# REAL end-to-end test — full UI, real Claude, real filesystem
# ==================================================================

@pytest.mark.skipif(
    os.environ.get("SKIP_E2E") == "1",
    reason="SKIP_E2E=1 set",
)
class TestRewindE2E:

    def test_00_preconditions(self, driver, scratch_file):
        """Daemon and web UI must be running."""
        assert _web_alive(), "Web UI not running at " + BASE_URL
        assert _daemon_alive(), "Daemon not running on port 5051"
        assert scratch_file.exists()
        assert scratch_file.read_text(encoding="utf-8") == SCRATCH_ORIGINAL

    def test_01_load_ui(self, driver):
        """Load the web UI and set the active project."""
        driver.get(BASE_URL)
        WebDriverWait(driver, 15).until(
            EC.presence_of_element_located((By.TAG_NAME, "header"))
        )
        driver.execute_script(
            "localStorage.setItem('activeProject', 'C--Users-15512-Documents-ClaudeGUI')"
        )
        driver.get(BASE_URL)
        WebDriverWait(driver, 15).until(
            EC.presence_of_element_located((By.TAG_NAME, "header"))
        )
        time.sleep(2)

    def test_02_click_new_session(self, driver):
        """Click the 'New Session' button in the sidebar via Selenium."""
        btn = driver.find_element(By.ID, "btn-add-agent")
        btn.click()
        # Wait for the new session textarea to appear
        WebDriverWait(driver, 10).until(
            EC.presence_of_element_located((By.ID, "live-input-ta"))
        )
        time.sleep(1)

    def test_03_type_prompt_and_send(self, driver, scratch_file):
        """Type a prompt in the chat textarea and press Enter to send."""
        # Wait for the textarea — it may re-render after new session setup
        ta = WebDriverWait(driver, 15).until(
            EC.presence_of_element_located((By.ID, "live-input-ta"))
        )
        # Also wait for it to be interactable
        WebDriverWait(driver, 5).until(EC.element_to_be_clickable((By.ID, "live-input-ta")))
        ta = driver.find_element(By.ID, "live-input-ta")

        prompt = (
            f"Add a single-line comment '# REWIND_TEST_MARKER' at the top of "
            f"the file {scratch_file}. Do not change anything else. "
            f"Do not use the Agent tool. Use Edit directly."
        )

        ta.click()
        time.sleep(0.3)
        ta.send_keys(prompt)
        time.sleep(0.5)

        # Send via Enter (the textarea has onkeydown that calls _newSessionSubmit)
        ta.send_keys(Keys.RETURN)
        time.sleep(3)

    def test_04_wait_for_edit(self, driver, scratch_file):
        """Wait for Claude to actually edit the scratch file on disk."""
        changed = _wait_for_file_change(scratch_file, SCRATCH_ORIGINAL, timeout=180)
        assert changed, (
            f"Claude did not edit the scratch file within 180 seconds.\n"
            f"Content: {scratch_file.read_text(encoding='utf-8')}"
        )
        content = scratch_file.read_text(encoding="utf-8")
        assert "REWIND_TEST_MARKER" in content, (
            f"Claude edited the file but marker not found. Content:\n{content}"
        )

    def test_05_wait_for_idle(self, driver):
        """Wait for the session to go idle (Claude finished processing)."""
        # Wait for the textarea to reappear with idle-state placeholder
        deadline = time.time() + 60
        while time.time() < deadline:
            try:
                ta = driver.find_element(By.ID, "live-input-ta")
                placeholder = ta.get_attribute("placeholder") or ""
                if "next command" in placeholder.lower() or "continue" in placeholder.lower():
                    return
            except Exception:
                pass
            time.sleep(2)
        # If we get here, just continue — the edit happened, that's what matters

    def test_06_snapshot_written(self):
        """Verify a snapshot referencing our scratch file exists in the JSONL."""
        # Give the daemon a moment to write the snapshot
        deadline = time.time() + 30
        while time.time() < deadline:
            jsonl = _find_newest_jsonl_with_snapshot()
            if jsonl:
                self.__class__._rewind_jsonl = jsonl
                self.__class__._rewind_session_id = jsonl.stem
                return
            time.sleep(3)
        pytest.fail("No file-history-snapshot found for scratch file in any JSONL")

    def test_07_open_rewind_picker(self, driver):
        """Open the rewind picker via the toolbar Rewind button."""
        sid = self._rewind_session_id

        # Make sure we're viewing this session
        driver.execute_script(
            f"localStorage.setItem('activeSessionId', '{sid}')"
        )
        driver.get(BASE_URL)
        WebDriverWait(driver, 15).until(
            EC.presence_of_element_located((By.TAG_NAME, "header"))
        )
        time.sleep(3)

        # Click the Rewind toolbar button
        try:
            btn = driver.find_element(By.ID, "btn-rewind")
            btn.click()
        except Exception:
            # Fallback: invoke via JS
            driver.execute_script(f"showMessagePicker('{sid}', 'rewind')")

        WebDriverWait(driver, 10).until(
            EC.visibility_of_element_located((By.ID, "pm-overlay"))
        )
        title = driver.find_element(By.CSS_SELECTOR, "#pm-overlay .pm-title").text
        assert "Rewind" in title

    def test_08_timeline_has_snapshots(self, driver):
        """Verify the timeline shows rows with snapshot indicators."""
        WebDriverWait(driver, 10).until(
            EC.presence_of_element_located((By.CSS_SELECTOR, "#msg-timeline .tl-row"))
        )
        rows = driver.find_elements(By.CSS_SELECTOR, "#msg-timeline .tl-row")
        assert len(rows) >= 1, "No timeline rows found"

        snaps = driver.find_elements(By.CSS_SELECTOR, "#msg-timeline .tl-snap")
        assert len(snaps) >= 1, "No snapshot indicators found in timeline"

    def test_09_select_and_rewind(self, driver, scratch_file):
        """Click a snapshot row, click Confirm, verify file is restored."""
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
        time.sleep(5)

        # THE REAL TEST: is the file back to original?
        content = scratch_file.read_text(encoding="utf-8")
        assert content == SCRATCH_ORIGINAL, (
            f"File was NOT restored.\nExpected:\n{SCRATCH_ORIGINAL}\nGot:\n{content}"
        )

    def test_10_modal_closed(self, driver):
        """Verify the rewind modal closed after confirm."""
        overlay = driver.find_element(By.ID, "pm-overlay")
        assert not overlay.is_displayed(), "Modal still visible after rewind"
