"""Selenium E2E tests for Rewind Code — the most critical E2E test we have.

WHAT THIS TESTS
===============
The full rewind workflow, end-to-end through a real browser:

  1. Opens the VibeNode UI in headless Chrome
  2. Creates a new Claude session
  3. Sends a prompt asking Claude to edit a scratch file
  4. Waits for Claude to actually edit the file on disk
  5. Opens the Rewind Code picker from the toolbar
  6. Selects a point in the conversation timeline BEFORE the edit
  7. Clicks "Rewind Code" to confirm
  8. Verifies the scratch file is restored to its original content

This proves the entire rewind pipeline works: UI → toolbar → timeline API →
rewind API → JSONL edit-reversal → file write.  If this test passes, rewind
works.  If it fails, something in that chain is broken.

WHY EACH TEST EXISTS
====================
Tests are numbered 00-08 and run in order (pytest sorts by name within a
class).  Each step depends on the previous one:

  00 - Preconditions: daemon + web server + scratch file all healthy
  01 - Load UI: navigate to VibeNode and set the active project
  02 - New Session: create a session and send the "edit this file" prompt
  03 - Wait for Edit: poll the scratch file until Claude has edited it
  04 - Find Session: locate the JSONL file so we can open the rewind picker
  05 - Open Rewind: click the Rewind toolbar button, verify picker opens
  06 - Timeline: verify the picker loaded message rows with snapshot indicators
  07 - Rewind & Verify: select the right row, confirm, verify file is restored
  08 - Modal Closed: picker overlay should dismiss after rewind completes

HOW TO RUN
==========
  pytest tests/e2e/test_selenium_rewind.py -m e2e -v --timeout=300

  Set SKIP_E2E=1 to skip this test entirely.

ISOLATION
=========
The conftest starts a SEPARATE test server (port 5099) and daemon (port 5098).
The user's running instance on :5050/:5051 is NEVER touched.  The scratch file
lives at tests/_scratch_rewind_test.py and is cleaned up after each run.

Requires: test daemon (5098) + test web UI (5099) running, Claude API key configured.
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
from tests.e2e.wait_helpers import wait_for_js_ready

pytestmark = pytest.mark.e2e

# ---------------------------------------------------------------------------
# Path constants — derived dynamically, never hardcoded
# ---------------------------------------------------------------------------

REPO_ROOT = Path(__file__).resolve().parents[2]     # VibeNode/
PROJECT_DIR = REPO_ROOT                              # Claude project directory
# Claude Code encodes project paths into directory-safe names by replacing
# all path separators and colons with hyphens.  This must match the naming
# convention used in ~/.claude/projects/ so we can find the session JSONL.
_ENCODED_PROJECT = (
    str(PROJECT_DIR)
    .replace("\\", "-")
    .replace("/", "-")
    .replace(":", "-")
)
# Where Claude stores session JSONL files for this project
SESSIONS_DIR = Path.home() / ".claude" / "projects" / _ENCODED_PROJECT

# The file Claude will edit (and rewind will restore)
SCRATCH_FILE = REPO_ROOT / "tests" / "_scratch_rewind_test.py"
SCRATCH_ORIGINAL = "# scratch file for rewind E2E test\nx = 1\n"

# The prompt is very specific: exactly one edit, no Agent tool, no ambiguity.
# This makes the rewind reversal deterministic — there's exactly one Edit to
# undo, so we know exactly what the file should look like after rewind.
#
# "Do not use the Agent tool" is CRITICAL: the Agent tool wraps edits in a
# subprocess and may use Write instead of Edit.  The rewind API can reverse
# Edit tool_use blocks (old_string → new_string swap) but cannot reverse
# Write tool_use without a daemon file-history snapshot on disk.  Forcing
# Edit keeps the test deterministic.
PROMPT = (
    f"Add a single-line comment '# REWIND_TEST_MARKER' at the very top of "
    f"the file {SCRATCH_FILE}. Do not change anything else. Do not use "
    f"the Agent tool — use Edit directly."
)


# ===========================================================================
# Helpers — small pure functions, easy to understand and debug
# ===========================================================================

def _web_alive():
    """Check if the test web server (port 5099) is responding."""
    try:
        with urllib.request.urlopen(BASE_URL, timeout=3):
            return True
    except Exception:
        return False


def _daemon_alive():
    """Check if the test daemon (port 5098) is accepting TCP connections."""
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.settimeout(3)
        s.connect(("127.0.0.1", TEST_DAEMON_PORT))
        s.close()
        return True
    except Exception:
        return False


def _wait_for_file_change(filepath, original, timeout=180):
    """Poll until the file content differs from *original*.

    Returns True if the file changed, False if we timed out.
    Polls every 3 seconds — a shorter interval would waste CPU on stat()
    calls while Claude is still thinking, and a longer one adds unnecessary
    latency to the test.  Claude typically edits within 10-30s.
    """
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            if filepath.read_text(encoding="utf-8") != original:
                return True
        except Exception:
            pass  # File might be mid-write; just retry
        time.sleep(3)
    return False


def _find_test_session_jsonl(existing_before):
    """Find the JSONL created by THIS test run (not a pre-existing one).

    We compare against `existing_before` (a set of filenames recorded before
    we created the session) to avoid picking up old sessions.  We also check
    that the JSONL mentions our scratch file to be doubly sure.
    """
    if not SESSIONS_DIR.is_dir():
        return None
    for jsonl in sorted(
        SESSIONS_DIR.glob("*.jsonl"),
        key=lambda p: p.stat().st_mtime,
        reverse=True,  # newest first — our session is the most recent
    ):
        if jsonl.name in existing_before:
            continue
        try:
            text = jsonl.read_text(encoding="utf-8", errors="replace")
        except Exception:
            continue
        if "_scratch_rewind_test" in text:
            return jsonl
    return None


# ===========================================================================
# Fixtures
# ===========================================================================

# The driver fixture comes from tests/e2e/conftest.py — DO NOT duplicate here.

@pytest.fixture(scope="class")
def scratch_file():
    """Create the scratch file before tests, restore it after.

    scope="class" matches the driver scope so they share a lifecycle.
    The file is ALWAYS restored to original after tests — even if a test
    fails — so rewind test runs are idempotent.
    """
    SCRATCH_FILE.parent.mkdir(parents=True, exist_ok=True)
    SCRATCH_FILE.write_text(SCRATCH_ORIGINAL, encoding="utf-8")
    yield SCRATCH_FILE
    # Always restore — don't leave a modified file in the repo
    try:
        SCRATCH_FILE.write_text(SCRATCH_ORIGINAL, encoding="utf-8")
    except Exception:
        pass


# ===========================================================================
# The Test Class — ordered steps that exercise the full rewind pipeline
# ===========================================================================

@pytest.mark.skipif(
    os.environ.get("SKIP_E2E") == "1",
    reason="SKIP_E2E=1 set — skipping E2E tests",
)
class TestRewindE2E:
    """Full end-to-end test: create session → Claude edits → rewind → verify.

    Tests are numbered for execution order.  Each step stores state on the
    class (e.g. _rewind_session_id) so later steps can use it.  If an early
    step fails, later steps will also fail — that's intentional, because the
    pipeline is sequential.
    """

    # --- Step 0: Make sure the test environment is healthy ---

    def test_00_preconditions(self, driver, scratch_file):
        """Verify daemon, web server, and scratch file are all ready.

        If this fails, nothing else can work.  The error message tells you
        exactly which component is down.
        """
        # Reset class state from any prior run (prevents bleed-over if
        # pytest reuses the class object across parameterised invocations)
        self.__class__._existing_jsonls = set()
        self.__class__._rewind_session_id = None
        assert _web_alive(), (
            f"Web UI not running on {BASE_URL}. "
            "The E2E conftest should auto-start it — check logs."
        )
        assert _daemon_alive(), (
            f"Daemon not running on port {TEST_DAEMON_PORT}. "
            "The E2E conftest should auto-start it — check logs."
        )
        content = scratch_file.read_text(encoding="utf-8")
        assert content == SCRATCH_ORIGINAL, (
            f"Scratch file has unexpected content: {content!r}"
        )

    # --- Step 1: Load the VibeNode UI ---

    def test_01_load_ui(self, driver):
        """Navigate to the VibeNode UI and set the active project.

        We set the project in localStorage BEFORE loading the page so the UI
        knows which project we're working in.  The second get() applies it.
        """
        driver.get(BASE_URL)
        WebDriverWait(driver, 15).until(
            EC.presence_of_element_located((By.TAG_NAME, "header"))
        )
        # Tell VibeNode which project to use
        driver.execute_script(
            f"localStorage.setItem('activeProject', '{_ENCODED_PROJECT}')"
        )
        # Reload to apply the project selection
        driver.get(BASE_URL)
        WebDriverWait(driver, 15).until(
            EC.presence_of_element_located((By.TAG_NAME, "header"))
        )
        # Wait for the JS app to fully initialise (WebSocket, session list)
        # rather than a blind sleep.
        wait_for_js_ready(driver, timeout=10)

    # --- Step 2: Create a new session and send the edit prompt ---

    def test_02_new_session_and_send(self, driver, scratch_file):
        """Click New Session, type prompt in textarea, submit.

        We record existing JSONLs before creating the session so test_04
        can find the NEW one by exclusion.
        """
        # Snapshot existing JSONLs (so we can find the new one later)
        if SESSIONS_DIR.is_dir():
            self.__class__._existing_jsonls = set(
                p.name for p in SESSIONS_DIR.glob("*.jsonl")
            )
        else:
            self.__class__._existing_jsonls = set()

        # Call addNewAgent() — VibeNode's JS function to create a session
        result = driver.execute_script("""
            if (typeof addNewAgent !== 'function') return 'NOT_DEFINED';
            try {
                addNewAgent();
                return 'CALLED';
            } catch(e) {
                return 'ERROR: ' + e.toString();
            }
        """)
        assert result == "CALLED", f"addNewAgent() failed: {result}"

        # Wait for the textarea to appear (async DOM update after addNewAgent)
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
            # Debug info: what's in the main panel?
            body_html = driver.execute_script(
                'return document.getElementById("main-body")'
                '?.innerHTML?.substring(0,500) || "EMPTY"'
            )
            # Do NOT inject a synthetic textarea — that masks real failures.
            # If the textarea doesn't appear, the session creation is broken.
            assert ta is not None, (
                f"Textarea never appeared after 30s. main-body: {body_html}"
            )

        # Type the prompt
        ta.click()
        time.sleep(0.3)
        ta.send_keys(PROMPT)
        time.sleep(0.5)

        # Submit via JS to guarantee the handler fires
        driver.execute_script("""
            const ta = document.getElementById('live-input-ta');
            if (!ta.value.trim()) ta.value = arguments[0];
            const handler = ta.getAttribute('onkeydown') || '';
            const match = handler.match(/_newSessionSubmit\\('([^']+)'\\)/);
            if (match) _newSessionSubmit(match[1]);
        """, PROMPT)
        time.sleep(3)  # Give the session a moment to initialize on the daemon

        # Verify submission took effect — the textarea should clear or a
        # spinner should appear.
        submitted = driver.execute_script("""
            const ta = document.getElementById('live-input-ta');
            // If submit worked, the textarea is either cleared, disabled, or replaced
            return !ta || !ta.value.trim() || ta.disabled ||
                   !!document.querySelector('.live-spinner');
        """)
        # Note: not asserting here because some UI paths don't immediately
        # clear the textarea.  The real proof is test_03 — if Claude edits
        # the file, the submit worked.

    # --- Step 3: Wait for Claude to edit the scratch file ---

    def test_03_wait_for_edit(self, driver, scratch_file):
        """Poll the scratch file until Claude has added the REWIND_TEST_MARKER.

        This is the slowest step — Claude needs to receive the prompt, plan
        the edit, and write to disk.  We allow up to 180 seconds.
        """
        changed = _wait_for_file_change(
            scratch_file, SCRATCH_ORIGINAL, timeout=180
        )
        assert changed, (
            f"Claude did not edit the scratch file within 180 seconds.\n"
            f"Content is still: {scratch_file.read_text(encoding='utf-8')!r}"
        )
        content = scratch_file.read_text(encoding="utf-8")
        assert "REWIND_TEST_MARKER" in content, (
            f"File changed but doesn't contain REWIND_TEST_MARKER: {content!r}"
        )

    # --- Step 4: Find the session JSONL so we can open the rewind picker ---

    def test_04_find_session(self, driver):
        """Locate the JSONL file for the session Claude just used.

        The rewind picker needs a session ID.  We find it by looking for a
        JSONL that (a) didn't exist before our test and (b) mentions our
        scratch file.
        """
        existing = getattr(self.__class__, '_existing_jsonls', set())

        # Poll — the JSONL may take a few seconds to appear
        deadline = time.time() + 30
        jsonl = None
        while time.time() < deadline:
            jsonl = _find_test_session_jsonl(existing)
            if jsonl:
                break
            time.sleep(2)

        assert jsonl is not None, (
            "Could not find test session JSONL in "
            f"{SESSIONS_DIR}. Existing files: {existing}"
        )
        sid = jsonl.stem
        self.__class__._rewind_session_id = sid

        # Navigate to that session in the UI
        driver.execute_script(
            f"localStorage.setItem('activeProject', '{_ENCODED_PROJECT}');"
            f"localStorage.setItem('activeSessionId', '{sid}')"
        )
        driver.get(BASE_URL)
        WebDriverWait(driver, 15).until(
            EC.presence_of_element_located((By.TAG_NAME, "header"))
        )
        # Wait for the session panel to render instead of a blind sleep
        wait_for_js_ready(driver, timeout=15)

    # --- Step 5: Open the Rewind Code picker ---

    def test_05_open_rewind_picker(self, driver):
        """Click the Rewind button in the toolbar to open the picker.

        The picker is a modal overlay (#pm-overlay) with a timeline of
        messages.  We try the toolbar button first; if it's hidden behind
        an actions dropdown, we open that first.  As a fallback, we call
        showMessagePicker() directly via JS.
        """
        sid = self._rewind_session_id

        # Find the rewind button
        rewind_btn = driver.find_element(By.ID, "btn-rewind")

        # It might be hidden in an actions dropdown on narrow viewports
        if not rewind_btn.is_displayed():
            try:
                driver.find_element(By.ID, "btn-actions").click()
                time.sleep(0.5)
            except Exception:
                pass

        # Click — fall back to JS if the button isn't interactable
        try:
            rewind_btn.click()
        except Exception:
            driver.execute_script(
                f"showMessagePicker('{sid}', 'rewind')"
            )

        # Wait for the overlay to appear
        WebDriverWait(driver, 10).until(
            EC.visibility_of_element_located((By.ID, "pm-overlay"))
        )

        # Wait for the title text to render (it's set via innerHTML, so
        # there can be a frame where the element exists but text is empty)
        def _title_has_text(d):
            try:
                el = d.find_element(By.CSS_SELECTOR, "#pm-overlay .pm-title")
                return el if el.text.strip() else False
            except Exception:
                return False

        title_el = WebDriverWait(driver, 10).until(_title_has_text)
        assert "Rewind" in title_el.text, (
            f"Expected 'Rewind' in picker title, got: {title_el.text!r}"
        )

    # --- Step 6: Verify the timeline loaded with snapshot indicators ---

    def test_06_timeline_has_rows(self, driver):
        """The timeline must show message rows, at least one with a snapshot.

        A snapshot indicator (💾 icon, class .tl-snap) means the daemon
        recorded file state at that message — which is what rewind uses.
        """
        WebDriverWait(driver, 15).until(
            EC.presence_of_element_located(
                (By.CSS_SELECTOR, "#msg-timeline .tl-row")
            )
        )
        rows = driver.find_elements(By.CSS_SELECTOR, "#msg-timeline .tl-row")
        assert len(rows) >= 1, "No timeline rows — is the session empty?"

        snaps = driver.find_elements(By.CSS_SELECTOR, "#msg-timeline .tl-snap")
        assert len(snaps) >= 1, (
            "No snapshot indicators (💾) in the timeline. "
            "The daemon may not have recorded file snapshots."
        )

    # --- Step 7: THE MONEY TEST — rewind and verify file is restored ---

    def test_07_click_rewind_and_verify(self, driver, scratch_file):
        """Select a point BEFORE the edit, click Confirm, verify file restore.

        HOW REWIND WORKS:
        The rewind API reverses all edits AFTER the selected timeline row.
        So to undo Claude's edit, we need to select a message BEFORE it.

        STRATEGY:
        1. Find the first row with a snapshot indicator — that's the assistant
           message where Claude made the edit.
        2. Select the row BEFORE it — that's the user prompt.
        3. Click Confirm.  The API reverses the Edit tool_use, restoring the
           file to its pre-edit state.

        EDGE CASE: If the very first row has a snapshot (e.g. only one
        message pair), we select that row itself.  The up_to_line will be
        the start of the assistant message, and the Edit tool_use blocks
        inside it are AFTER that line, so they still get reversed.
        """
        # Ensure timeline rows are loaded
        WebDriverWait(driver, 15).until(
            EC.presence_of_element_located(
                (By.CSS_SELECTOR, "#msg-timeline .tl-row")
            )
        )

        rows = driver.find_elements(By.CSS_SELECTOR, "#msg-timeline .tl-row")
        assert len(rows) >= 1, "No timeline rows"

        # Find the first row with a snapshot (the Edit turn)
        snap_idx = None
        for i, r in enumerate(rows):
            if r.find_elements(By.CSS_SELECTOR, ".tl-snap"):
                snap_idx = i
                break

        # Select the row BEFORE the snapshot (the user prompt)
        if snap_idx is not None and snap_idx > 0:
            target_row = rows[snap_idx - 1]
        elif snap_idx is not None:
            # First row has the snapshot — select it directly
            target_row = rows[snap_idx]
        else:
            # No snapshot found — select the first row as fallback
            target_row = rows[0]

        # Click to select the row
        target_row.click()
        time.sleep(1)

        # Verify the row is visually selected
        row_class = target_row.get_attribute("class") or ""
        assert "selected" in row_class, (
            f"Row was not selected after click. Classes: {row_class!r}"
        )

        # Click the Confirm button (wait for it to be enabled — the JS
        # handler needs a moment to set _pickerSelectedLine)
        confirm = driver.find_element(By.ID, "pm-confirm")
        WebDriverWait(driver, 5).until(lambda d: confirm.is_enabled())
        confirm.click()

        # Poll for the file to be restored (up to 30 seconds).
        # The rewind API call is async from the browser's perspective —
        # the server reverses the edit and writes the file, then the
        # modal closes.  We poll the actual file on disk.
        deadline = time.time() + 30
        while time.time() < deadline:
            content = scratch_file.read_text(encoding="utf-8")
            if content == SCRATCH_ORIGINAL:
                break
            time.sleep(0.5)

        # THE MONEY CHECK: file must be back to its original content
        content = scratch_file.read_text(encoding="utf-8")
        assert content == SCRATCH_ORIGINAL, (
            f"File was NOT restored after 30 seconds.\n"
            f"Expected:\n{SCRATCH_ORIGINAL}\n"
            f"Got:\n{content}\n"
            f"This means the rewind API did not reverse Claude's edit."
        )

    # --- Step 8: Verify the modal dismissed ---

    def test_08_modal_closed(self, driver):
        """The rewind picker modal should close after rewind completes.

        The JS calls _closePm() after the API returns.  If the modal is
        still visible, something went wrong with the close animation or
        the API errored silently.
        """
        overlay = driver.find_element(By.ID, "pm-overlay")
        # Wait up to 10s for the overlay to hide (animation + API round-trip)
        WebDriverWait(driver, 10).until(
            lambda d: not overlay.is_displayed()
        )
