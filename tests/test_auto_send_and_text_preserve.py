"""
Source-guard tests for auto-send pending input and text preservation.

These read the raw JS source files and verify critical functions, call sites,
and logic patterns are present. They catch "clanker stripped my code"
regressions without needing a running server.

The Selenium browser-driven counterparts live in
``tests/e2e/test_auto_send_and_text_preserve_e2e.py`` (run with the e2e
suite, not the fast suite).

Run:
    pytest tests/test_auto_send_and_text_preserve.py
"""

import re
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
