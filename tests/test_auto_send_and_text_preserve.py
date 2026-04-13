"""
Tests for auto-send pending input and text preservation across state transitions.

Two layers of protection:

1. SOURCE GUARD TESTS (TestSourceGuard*)
   - Read the raw JS source files and verify critical functions, call sites,
     and logic patterns are present.  These catch "clanker stripped my code"
     regressions without needing a running server.

2. SELENIUM E2E TESTS (TestE2E*)
   - Drive the real browser against the running GUI and verify the actual
     behaviors work end-to-end.

Run source guards only (fast, no server needed):
    pytest tests/test_auto_send_and_text_preserve.py -k "SourceGuard"

Run E2E (requires server on test server + Chrome):
    pytest tests/test_auto_send_and_text_preserve.py -k "E2E"
"""

import re
import time
import pytest
from pathlib import Path

# ---------------------------------------------------------------------------
# Paths to the JS source files under test
# ---------------------------------------------------------------------------
_JS_DIR = Path(__file__).resolve().parent.parent / "static" / "js"
_LIVE_PANEL = _JS_DIR / "live-panel.js"
_TOOLBAR = _JS_DIR / "toolbar.js"
_APP = _JS_DIR / "app.js"


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


# ═══════════════════════════════════════════════════════════════════════════
# SOURCE GUARD TESTS — verify critical code hasn't been stripped
# ═══════════════════════════════════════════════════════════════════════════


class TestSourceGuardAutoSendFunction:
    """_autoSendPendingInput() must be DEFINED in live-panel.js."""

    def test_function_is_defined(self):
        src = _read(_LIVE_PANEL)
        assert "function _autoSendPendingInput()" in src, (
            "_autoSendPendingInput function definition is missing from live-panel.js"
        )

    def test_handles_new_session(self):
        """Must fire start_session for a new session that hasn't been submitted."""
        src = _read(_LIVE_PANEL)
        # Look for the new-session branch inside _autoSendPendingInput
        assert re.search(
            r"_autoSendPendingInput[\s\S]*?start_session", src
        ), "Auto-send must emit start_session for new sessions"

    def test_handles_idle_send_message(self):
        """Must send_message when session is idle."""
        src = _read(_LIVE_PANEL)
        assert re.search(
            r"_autoSendPendingInput[\s\S]*?send_message", src
        ), "Auto-send must emit send_message for idle sessions"

    def test_handles_permission_response(self):
        """Must emit permission_response when in question state."""
        src = _read(_LIVE_PANEL)
        assert re.search(
            r"_autoSendPendingInput[\s\S]*?permission_response", src
        ), "Auto-send must emit permission_response for question state"

    def test_handles_queue_in_working_state(self):
        """Must queue text via _addQueue when in working state."""
        src = _read(_LIVE_PANEL)
        assert re.search(
            r"_autoSendPendingInput[\s\S]*?_addQueue", src
        ), "Auto-send must call _addQueue for working-state queue textarea"

    def test_checks_both_textareas(self):
        """Must check both #live-input-ta and #live-queue-ta."""
        src = _read(_LIVE_PANEL)
        fn_match = re.search(
            r"function _autoSendPendingInput\(\)\s*\{([\s\S]*?)^\}",
            src, re.MULTILINE
        )
        assert fn_match, "Could not extract _autoSendPendingInput function body"
        body = fn_match.group(1)
        assert "live-input-ta" in body, "Must check #live-input-ta"
        assert "live-queue-ta" in body, "Must check #live-queue-ta"


class TestSourceGuardSessionSwitchSavesDraft:
    """Session switches must call _savePendingInputAsDraft() (NOT _autoSendPendingInput).

    _autoSendPendingInput SENDS the text, which causes the "cascade of hi sessions"
    bug: user types text, switches sessions, text gets auto-submitted.
    _savePendingInputAsDraft SAVES the text as a draft for when the user returns.
    """

    def test_select_session_saves_draft(self):
        src = _read(_TOOLBAR)
        assert re.search(
            r"_savePendingInputAsDraft\(\);\s*\n?\s*stopLivePanel\(\)",
            src
        ), "selectSession must call _savePendingInputAsDraft() before stopLivePanel()"

    def test_select_session_does_NOT_auto_send(self):
        """selectSession must NOT call _autoSendPendingInput — that submits the text."""
        src = _read(_TOOLBAR)
        fn_match = re.search(
            r"function selectSession\([^)]*\)\s*\{([\s\S]*?)^function ",
            src, re.MULTILINE
        )
        assert fn_match, "Could not find selectSession function"
        body = fn_match.group(1)
        assert "_autoSendPendingInput()" not in body, (
            "REGRESSION: selectSession calls _autoSendPendingInput() which SENDS text. "
            "Must use _savePendingInputAsDraft() to SAVE text as a draft instead."
        )

    def test_open_in_gui_saves_draft(self):
        src = _read(_LIVE_PANEL)
        assert re.search(
            r"_savePendingInputAsDraft\(\);\s*stopLivePanel\(\)",
            src
        ), "openInGUI must call _savePendingInputAsDraft() before stopLivePanel()"

    def test_deselect_session_saves_draft(self):
        src = _read(_TOOLBAR)
        fn_match = re.search(
            r"function deselectSession\(\)\s*\{([\s\S]*?)^function ",
            src, re.MULTILINE
        )
        assert fn_match, "Could not find deselectSession function"
        body = fn_match.group(1)
        assert "_savePendingInputAsDraft()" in body, (
            "deselectSession must call _savePendingInputAsDraft()"
        )
        assert "_autoSendPendingInput()" not in body, (
            "REGRESSION: deselectSession calls _autoSendPendingInput() which SENDS text"
        )

    def test_save_pending_input_as_draft_exists(self):
        """The _savePendingInputAsDraft function must exist in live-panel.js."""
        src = _read(_LIVE_PANEL)
        assert "function _savePendingInputAsDraft()" in src, (
            "_savePendingInputAsDraft must be defined — it saves typed text as a draft "
            "instead of auto-sending it when switching sessions"
        )

    def test_save_pending_input_calls_save_draft(self):
        """_savePendingInputAsDraft must call _saveDraft, not emit socket events."""
        src = _read(_LIVE_PANEL)
        fn_match = re.search(
            r"function _savePendingInputAsDraft\(\)\s*\{([\s\S]*?)\n\}",
            src
        )
        assert fn_match, "Could not find _savePendingInputAsDraft function"
        body = fn_match.group(1)
        assert "_saveDraft" in body, (
            "_savePendingInputAsDraft must call _saveDraft to persist text"
        )
        assert "socket.emit" not in body, (
            "REGRESSION: _savePendingInputAsDraft must NOT emit socket events — "
            "it should only save the draft, not send the text"
        )


class TestSourceGuardTextPreservation:
    """updateLiveInputBar() must capture text from the old textarea and
    restore it into the new textarea during state transitions."""

    def test_captures_text_before_rebuild(self):
        """Must read value from existing textarea before innerHTML replacement."""
        src = _read(_LIVE_PANEL)
        fn_match = re.search(
            r"function updateLiveInputBar\(\)\s*\{([\s\S]*?)^function ",
            src, re.MULTILINE
        )
        assert fn_match, "Could not find updateLiveInputBar function"
        body = fn_match.group(1)

        # Must grab text from whichever textarea exists
        assert re.search(r"live-input-ta.*live-queue-ta|live-queue-ta.*live-input-ta", body), (
            "Must check both textarea IDs when capturing text"
        )
        # Must store the value
        assert re.search(r"\.value", body), (
            "Must read .value from existing textarea"
        )

    def test_restores_text_after_rebuild(self):
        """After bar.innerHTML is replaced, preserved text must be restored."""
        src = _read(_LIVE_PANEL)
        fn_match = re.search(
            r"function updateLiveInputBar\(\)\s*\{([\s\S]*?)^function ",
            src, re.MULTILINE
        )
        assert fn_match, "Could not find updateLiveInputBar function"
        body = fn_match.group(1)

        # Must have restoration logic that sets .value on the new textarea
        assert re.search(r"preserv", body, re.IGNORECASE), (
            "Must have text preservation logic (variable with 'preserv' in name)"
        )
        # Must restore into the new textarea
        assert re.search(r"\.value\s*=\s*_preservedText", body), (
            "Must assign _preservedText to new textarea's .value"
        )

    def test_does_not_early_return_on_existing_text(self):
        """The old pattern 'if (existingTa && existingTa.value.trim()) return'
        must NOT be present — it blocked state transitions entirely."""
        src = _read(_LIVE_PANEL)
        fn_match = re.search(
            r"function updateLiveInputBar\(\)\s*\{([\s\S]*?)^function ",
            src, re.MULTILINE
        )
        assert fn_match, "Could not find updateLiveInputBar function"
        body = fn_match.group(1)

        # This pattern is the old broken behavior — should NOT exist
        assert not re.search(
            r"existingTa\s*&&\s*existingTa\.value\.trim\(\)\)\s*return",
            body
        ), (
            "REGRESSION: The old early-return pattern that blocks state transitions "
            "has been reintroduced. This prevents the UI from updating when the user "
            "has text typed. Remove it and use text preservation instead."
        )

    def test_calls_auto_resize_after_restore(self):
        """After restoring text, must call _autoResizeTextarea so the textarea
        isn't the wrong height."""
        src = _read(_LIVE_PANEL)
        fn_match = re.search(
            r"function updateLiveInputBar\(\)\s*\{([\s\S]*?)^function ",
            src, re.MULTILINE
        )
        assert fn_match, "Could not find updateLiveInputBar function"
        body = fn_match.group(1)

        assert re.search(r"_autoResizeTextarea", body), (
            "Must call _autoResizeTextarea after restoring preserved text"
        )


class TestSourceGuardSubmitClearsBeforePreserve:
    """Every submit function must clear ta.value BEFORE calling any function
    that triggers updateLiveInputBar (like _liveSubmitDirect), otherwise
    the preservation logic will recapture the sent text and stuff it back
    into the new textarea."""

    def test_liveSubmitIdle_clears_before_direct(self):
        """liveSubmitIdle must clear textarea BEFORE calling _liveSubmitDirect."""
        src = _read(_LIVE_PANEL)
        fn_match = re.search(
            r"function liveSubmitIdle\(\)\s*\{([\s\S]*?)^function ",
            src, re.MULTILINE
        )
        assert fn_match, "Could not find liveSubmitIdle function"
        body = fn_match.group(1)

        clear_pos = body.find("ta.value = ''")
        direct_pos = body.find("_liveSubmitDirect")
        assert clear_pos >= 0, "liveSubmitIdle must clear textarea (ta.value = '')"
        assert direct_pos >= 0, "liveSubmitIdle must call _liveSubmitDirect"
        assert clear_pos < direct_pos, (
            "REGRESSION: liveSubmitIdle clears the textarea AFTER _liveSubmitDirect. "
            "This causes the preservation logic to recapture the sent text and "
            "restore it into the queue textarea. The clear must come BEFORE."
        )

    def test_liveSubmitContinue_clears_before_updateBar(self):
        """liveSubmitContinue must clear textarea before updateLiveInputBar."""
        src = _read(_LIVE_PANEL)
        fn_match = re.search(
            r"function liveSubmitContinue\([^)]*\)\s*\{([\s\S]*?)^function ",
            src, re.MULTILINE
        )
        assert fn_match, "Could not find liveSubmitContinue function"
        body = fn_match.group(1)

        clear_pos = body.find("ta.value = ''")
        update_pos = body.find("updateLiveInputBar")
        assert clear_pos >= 0, "liveSubmitContinue must clear textarea"
        assert update_pos >= 0, "liveSubmitContinue must call updateLiveInputBar"
        assert clear_pos < update_pos, (
            "REGRESSION: liveSubmitContinue clears textarea AFTER updateLiveInputBar"
        )

    def test_liveSubmitWaiting_clears_before_updateBar(self):
        """liveSubmitWaiting must clear textarea before updateLiveInputBar."""
        src = _read(_LIVE_PANEL)
        fn_match = re.search(
            r"function liveSubmitWaiting\(\)\s*\{([\s\S]*?)^function ",
            src, re.MULTILINE
        )
        assert fn_match, "Could not find liveSubmitWaiting function"
        body = fn_match.group(1)

        clear_pos = body.find("ta.value = ''")
        # Could call updateLiveInputBar directly or via _liveSubmitDirect
        update_pos = body.find("updateLiveInputBar")
        direct_pos = body.find("_liveSubmitDirect")
        barrier = min(
            update_pos if update_pos >= 0 else 99999,
            direct_pos if direct_pos >= 0 else 99999,
        )
        assert clear_pos >= 0, "liveSubmitWaiting must clear textarea"
        assert barrier < 99999, "liveSubmitWaiting must call updateLiveInputBar or _liveSubmitDirect"
        assert clear_pos < barrier, (
            "REGRESSION: liveSubmitWaiting clears textarea AFTER the bar update"
        )


class TestSourceGuardInterruptPreservation:
    """liveSubmitInterrupt must also preserve queue textarea text when
    switching from working to idle (this existed before and must not regress)."""

    def test_interrupt_captures_queue_text(self):
        src = _read(_LIVE_PANEL)
        fn_match = re.search(
            r"function liveSubmitInterrupt\(\)\s*\{([\s\S]*?)^function ",
            src, re.MULTILINE
        )
        assert fn_match, "Could not find liveSubmitInterrupt function"
        body = fn_match.group(1)

        assert "live-queue-ta" in body, (
            "liveSubmitInterrupt must read from #live-queue-ta"
        )
        assert re.search(r"preserv", body, re.IGNORECASE), (
            "liveSubmitInterrupt must preserve queue text"
        )
        assert "live-input-ta" in body, (
            "liveSubmitInterrupt must restore text into #live-input-ta"
        )


# ═══════════════════════════════════════════════════════════════════════════
# SELENIUM E2E TESTS — verify behavior in the real browser
# ═══════════════════════════════════════════════════════════════════════════

try:
    from selenium import webdriver
    from selenium.webdriver.common.by import By
    from selenium.webdriver.support.ui import WebDriverWait
    from selenium.webdriver.support import expected_conditions as EC
    from selenium.webdriver.common.keys import Keys
    from selenium.webdriver.common.action_chains import ActionChains
    HAS_SELENIUM = True
except ImportError:
    HAS_SELENIUM = False

try:
    from tests.e2e.conftest import TEST_BASE_URL as BASE_URL
except ImportError:
    BASE_URL = "http://localhost:5099"
LONG_WAIT = 90


@pytest.fixture(scope="module")
def driver():
    if not HAS_SELENIUM:
        pytest.skip("selenium not installed")
    options = webdriver.ChromeOptions()
    options.add_argument("--headless=new")
    options.add_argument("--no-sandbox")
    options.add_argument("--disable-gpu")
    options.add_argument("--window-size=1400,900")
    try:
        d = webdriver.Chrome(options=options)
    except Exception:
        pytest.skip("Chrome/ChromeDriver not available")
    yield d
    d.quit()


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


class TestE2EAutoSendOnSessionSwitch:
    """When user has text typed and clicks away to another session,
    the text must be auto-sent to the original session."""

    def test_setup(self, driver):
        """Load page and select project."""
        _ensure_project_selected(driver)

    def test_auto_send_new_session_text_on_switch(self, driver):
        """Type text in a new-session textarea, click a different session
        in the sidebar → the new session should start with that text."""
        # Create a new session
        ta = _click_new_session(driver)

        # Type a distinctive message but DON'T submit
        test_msg = "AUTO_SEND_TEST say exactly: blue whale"
        ta.send_keys(test_msg)
        time.sleep(0.5)

        # Verify the text is in the textarea
        assert ta.get_attribute("value") == test_msg

        # Now click on a different session in the sidebar (if one exists)
        sidebar_items = driver.find_elements(By.CSS_SELECTOR, ".session-item")
        if len(sidebar_items) < 2:
            pytest.skip("Need at least 2 sessions in sidebar to test switch")

        # Find a session item that isn't the currently active one
        for item in sidebar_items:
            if "active" not in (item.get_attribute("class") or ""):
                item.click()
                break
        time.sleep(2)

        # The auto-send should have fired — verify by going back to the
        # session list and checking the session is now running
        # (The session title should have changed from "New Session" to
        # something derived from the message text)
        sidebar_html = driver.find_element(By.ID, "session-list").get_attribute("innerHTML")
        # The auto-send creates a placeholder title from the message text
        assert "AUTO_SEND_TEST" in sidebar_html or "blue whale" in sidebar_html, (
            "Auto-send did not fire: the new session should have started with "
            "the typed message, creating a title derived from it"
        )


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

        # Force a potential updateLiveInputBar by resizing the window
        # (which can trigger various re-render paths)
        driver.set_window_size(1200, 800)
        time.sleep(1)
        driver.set_window_size(1400, 900)
        time.sleep(1)

        # Text must still be in the textarea
        ta = driver.find_element(By.ID, "live-input-ta")
        actual = ta.get_attribute("value")
        assert test_text in actual, (
            f"Text was destroyed during re-render. Expected '{test_text}', got '{actual}'"
        )


class TestE2ETextPreservationIdleToWorking:
    """Text typed in the idle textarea must be preserved when the session
    transitions to working state (e.g. via queue auto-dispatch)."""

    def test_setup(self, driver):
        _ensure_project_selected(driver)

    def test_idle_text_preserved_after_send(self, driver):
        """Start a session, wait for idle, type follow-up text, submit it,
        verify the working-state queue textarea appears. Then wait for idle
        again and type more — text should survive any bar re-renders."""
        # Create and submit a new session
        ta = _click_new_session(driver)
        ta.send_keys("Say exactly: red fox")
        ta.send_keys(Keys.CONTROL, Keys.ENTER)

        # Wait for Claude to respond and session to go idle
        try:
            WebDriverWait(driver, LONG_WAIT).until(
                EC.presence_of_element_located((By.CSS_SELECTOR, "#live-log .msg.assistant"))
            )
        except Exception:
            pytest.skip("Claude did not respond in time")

        # Wait for idle state (input textarea reappears)
        try:
            WebDriverWait(driver, LONG_WAIT).until(
                EC.presence_of_element_located((By.ID, "live-input-ta"))
            )
        except Exception:
            pytest.skip("Session did not return to idle")

        # Type text in the idle textarea but don't submit
        ta = driver.find_element(By.ID, "live-input-ta")
        preserve_text = "IDLE_PRESERVE do not lose this text"
        ta.send_keys(preserve_text)
        time.sleep(2)

        # Text must still be there after brief delay (updateLiveInputBar
        # fires on timers and server events)
        ta = driver.find_element(By.ID, "live-input-ta")
        actual = ta.get_attribute("value")
        assert preserve_text in actual, (
            f"Idle textarea text was destroyed. Expected '{preserve_text}', got '{actual}'"
        )


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


class TestE2ETextPreservationViaJS:
    """Use execute_script to directly test the text preservation logic
    without needing to trigger real server state changes."""

    def test_setup(self, driver):
        _ensure_project_selected(driver)

    def test_text_preserved_idle_to_working(self, driver):
        """Simulate idle→working transition via JS and verify text preservation."""
        # Create a new session so we have a live panel
        ta = _click_new_session(driver)
        ta.send_keys("Say exactly: test fish")
        ta.send_keys(Keys.CONTROL, Keys.ENTER)

        # Wait for the session to exist
        time.sleep(3)

        result = driver.execute_script("""
            // Only run if we have a live session
            if (!liveSessionId) return 'no_session';

            var sid = liveSessionId;

            // Simulate being in idle state with text typed
            sessionKinds[sid] = 'idle';
            runningIds.add(sid);
            liveBarState = null;
            updateLiveInputBar();

            // Type text into the idle textarea
            var ta = document.getElementById('live-input-ta');
            if (!ta) return 'no_idle_textarea';
            ta.value = 'PRESERVE_ME_123';

            // Now transition to working state
            sessionKinds[sid] = 'working';
            liveBarState = null;
            updateLiveInputBar();

            // Check if text was preserved in the queue textarea
            var qta = document.getElementById('live-queue-ta');
            if (!qta) return 'no_queue_textarea';
            return qta.value;
        """)

        if result == 'no_session':
            pytest.skip("No live session available")

        assert result == "PRESERVE_ME_123", (
            f"Text was NOT preserved during idle→working transition. "
            f"Queue textarea value: '{result}'"
        )

    def test_text_preserved_working_to_idle(self, driver):
        """Simulate working→idle transition via JS and verify text preservation."""
        result = driver.execute_script("""
            if (!liveSessionId) return 'no_session';

            var sid = liveSessionId;

            // Simulate being in working state with text in queue textarea
            sessionKinds[sid] = 'working';
            runningIds.add(sid);
            liveBarState = null;
            updateLiveInputBar();

            var qta = document.getElementById('live-queue-ta');
            if (!qta) return 'no_queue_textarea';
            qta.value = 'QUEUE_TEXT_456';

            // Now transition to idle state
            sessionKinds[sid] = 'idle';
            liveBarState = null;
            updateLiveInputBar();

            // Check if text was preserved in the idle textarea
            var ta = document.getElementById('live-input-ta');
            if (!ta) return 'no_idle_textarea';
            return ta.value;
        """)

        if result == 'no_session':
            pytest.skip("No live session available")

        assert result == "QUEUE_TEXT_456", (
            f"Text was NOT preserved during working→idle transition. "
            f"Idle textarea value: '{result}'"
        )

    def test_text_preserved_idle_to_question(self, driver):
        """Simulate idle→question transition via JS and verify text preservation."""
        result = driver.execute_script("""
            if (!liveSessionId) return 'no_session';

            var sid = liveSessionId;

            // Set up idle state with text
            sessionKinds[sid] = 'idle';
            runningIds.add(sid);
            liveBarState = null;
            updateLiveInputBar();

            var ta = document.getElementById('live-input-ta');
            if (!ta) return 'no_idle_textarea';
            ta.value = 'QUESTION_TEXT_789';

            // Transition to question state
            sessionKinds[sid] = 'question';
            waitingData[sid] = {question: 'Allow Bash?', options: ['y','n'], kind: 'tool'};
            liveBarState = null;
            updateLiveInputBar();

            // Check if text was preserved in the question textarea
            var qta = document.getElementById('live-input-ta');
            if (!qta) return 'no_question_textarea';
            return qta.value;
        """)

        if result == 'no_session':
            pytest.skip("No live session available")

        assert result == "QUESTION_TEXT_789", (
            f"Text was NOT preserved during idle→question transition. "
            f"Question textarea value: '{result}'"
        )

    def test_text_preserved_question_to_working(self, driver):
        """Simulate question→working transition via JS and verify text preservation."""
        result = driver.execute_script("""
            if (!liveSessionId) return 'no_session';

            var sid = liveSessionId;

            // Set up question state with text
            sessionKinds[sid] = 'question';
            runningIds.add(sid);
            waitingData[sid] = {question: 'Allow?', options: ['y','n'], kind: 'tool'};
            liveBarState = null;
            updateLiveInputBar();

            var ta = document.getElementById('live-input-ta');
            if (!ta) return 'no_question_textarea';
            ta.value = 'WORKING_TEXT_012';

            // Transition to working state
            delete waitingData[sid];
            sessionKinds[sid] = 'working';
            liveBarState = null;
            updateLiveInputBar();

            // Check if text was preserved in the queue textarea
            var qta = document.getElementById('live-queue-ta');
            if (!qta) return 'no_queue_textarea';
            return qta.value;
        """)

        if result == 'no_session':
            pytest.skip("No live session available")

        assert result == "WORKING_TEXT_012", (
            f"Text was NOT preserved during question→working transition. "
            f"Queue textarea value: '{result}'"
        )
