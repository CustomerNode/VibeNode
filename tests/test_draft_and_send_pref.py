"""Source guards for the two most common regressions in VibeNode:

1. DRAFT PERSISTENCE — unsent text must survive session switches and page reloads.
   Without this, typing in the input box and switching sessions loses the text,
   or worse, auto-submits it causing a cascade of unwanted "hi" sessions.

2. SEND BEHAVIOR PREFERENCE — "Enter to send" vs "Ctrl+Enter to send" must persist
   across page reloads and server restarts. AI agents keep resetting or breaking
   this preference when "simplifying" keyboard handling.

Both have been broken MULTIPLE TIMES by AI agents who didn't understand why the
code existed. These tests read the source to verify the patterns are intact.
"""

import re
from pathlib import Path

_JS = Path(__file__).resolve().parent.parent / "static" / "js"


def _read(path):
    return path.read_text(encoding="utf-8")


# ===========================================================================
# DRAFT PERSISTENCE — live-panel.js must save/restore drafts via localStorage
# ===========================================================================

class TestDraftPersistenceAPI:
    """The draft API (_saveDraft, _getDraft, _clearDraft) must exist and
    use localStorage. Without it, session switches lose typed text."""

    def test_drafts_use_localstorage(self):
        src = _read(_JS / "live-panel.js")
        assert "vibenode_drafts" in src, \
            "Draft persistence must use 'vibenode_drafts' localStorage key"
        assert "localStorage" in src, \
            "Drafts must be persisted to localStorage"

    def test_save_draft_exists(self):
        src = _read(_JS / "live-panel.js")
        assert "function _saveDraft(" in src, \
            "_saveDraft function must exist — saves typed text before session switch"

    def test_get_draft_exists(self):
        src = _read(_JS / "live-panel.js")
        assert "function _getDraft(" in src, \
            "_getDraft function must exist — restores text when switching back"

    def test_clear_draft_exists(self):
        src = _read(_JS / "live-panel.js")
        assert "function _clearDraft(" in src, \
            "_clearDraft function must exist — removes draft after successful submit"

    def test_save_draft_stores_to_localstorage(self):
        """_saveDraft must call _persistDraftsToStorage (which writes localStorage).
        Without this, drafts only survive in memory — lost on page reload."""
        src = _read(_JS / "live-panel.js")
        match = re.search(
            r"function _saveDraft\([^)]*\)\s*\{([\s\S]*?)\n\}",
            src
        )
        assert match, "Could not extract _saveDraft function body"
        body = match.group(1)
        assert "_persistDraftsToStorage" in body or "localStorage" in body, \
            "_saveDraft must persist to localStorage — in-memory only loses drafts on reload"


class TestDraftRestorationOnSessionSwitch:
    """When switching TO a session, its draft must be restored into the textarea."""

    def test_update_live_input_bar_restores_draft(self):
        """updateLiveInputBar (called during session open) must fall back to
        _getDraft when there's no _preservedText from a state transition."""
        src = _read(_JS / "live-panel.js")
        match = re.search(
            r"function updateLiveInputBar\([^)]*\)\s*\{([\s\S]*?)^function ",
            src, re.MULTILINE
        )
        assert match, "Could not find updateLiveInputBar function"
        body = match.group(1)
        assert "_getDraft" in body, \
            "updateLiveInputBar must call _getDraft to restore saved draft " \
            "when opening a session (no _preservedText from state transition)"

    def test_draft_saved_on_stop(self):
        """stopLivePanel (called when leaving a session) must save the draft."""
        src = _read(_JS / "live-panel.js")
        match = re.search(
            r"function stopLivePanel\([^)]*\)\s*\{([\s\S]*?)^function ",
            src, re.MULTILINE
        )
        assert match, "Could not find stopLivePanel function"
        body = match.group(1)
        assert "_saveDraft" in body, \
            "stopLivePanel must call _saveDraft to save typed text before switching away"

    def test_draft_cleared_on_submit(self):
        """When a message is successfully submitted, the draft must be cleared
        so it doesn't get restored next time the user opens the session."""
        src = _read(_JS / "live-panel.js")
        assert src.count("_clearDraft") >= 2, \
            "Must call _clearDraft in at least 2 places (submit paths) to " \
            "prevent sent messages from being restored as drafts"

    def test_draft_saved_on_page_unload(self):
        """Drafts must be saved on beforeunload/visibilitychange so page
        refresh doesn't lose typed text."""
        src = _read(_JS / "live-panel.js")
        assert "beforeunload" in src or "visibilitychange" in src or "_saveDraftFromDOM" in src, \
            "Must save drafts on page unload events — typed text lost on refresh otherwise"


# ===========================================================================
# SEND BEHAVIOR PREFERENCE — Enter vs Ctrl+Enter must persist
# ===========================================================================

class TestSendBehaviorPreference:
    """The 'Enter to send' vs 'Ctrl+Enter to send' preference must be stored
    in localStorage AND synced to the server. AI agents keep breaking this."""

    def test_send_behavior_variable_exists(self):
        src = _read(_JS / "utils.js")
        assert re.search(r"let sendBehavior\s*=", src), \
            "sendBehavior variable must exist in utils.js"

    def test_reads_from_localstorage_on_load(self):
        """Must read preference from localStorage on page load — not hardcoded."""
        src = _read(_JS / "utils.js")
        assert re.search(r"localStorage\.getItem\(['\"]sendBehavior['\"]\)", src), \
            "Must read sendBehavior from localStorage on page load"

    def test_saves_to_localstorage_on_change(self):
        """Must save to localStorage when the user changes the preference."""
        src = _read(_JS / "utils.js")
        assert re.search(r"localStorage\.setItem\(['\"]sendBehavior['\"]", src), \
            "Must save sendBehavior to localStorage when changed"

    def test_syncs_to_server(self):
        """Must sync to server via set_ui_prefs so preference survives
        localStorage clears and works across devices."""
        src = _read(_JS / "utils.js")
        assert "set_ui_prefs" in src, \
            "Must sync sendBehavior to server via set_ui_prefs socket event"

    def test_should_send_function_exists(self):
        """_shouldSend(e) must exist and check the sendBehavior variable."""
        src = _read(_JS / "utils.js")
        assert "function _shouldSend(" in src, \
            "_shouldSend function must exist — keyboard handler for Enter/Ctrl+Enter"
        match = re.search(
            r"function _shouldSend\([^)]*\)\s*\{([\s\S]*?)\n\}",
            src
        )
        assert match, "Could not extract _shouldSend body"
        body = match.group(1)
        assert "sendBehavior" in body, \
            "_shouldSend must check the sendBehavior variable — not hardcoded to one mode"

    def test_enter_mode_sends_on_plain_enter(self):
        """When sendBehavior === 'enter', plain Enter (no modifiers) must send."""
        src = _read(_JS / "utils.js")
        match = re.search(
            r"function _shouldSend\([^)]*\)\s*\{([\s\S]*?)\n\}",
            src
        )
        body = match.group(1)
        # Must check for 'enter' mode and return true for unmodified Enter
        assert re.search(r"sendBehavior\s*===\s*['\"]enter['\"]", body), \
            "_shouldSend must have a branch for sendBehavior === 'enter'"

    def test_preference_restored_from_server_on_connect(self):
        """On socket connect, server prefs must be loaded and applied.
        This ensures preference survives localStorage clears."""
        src = _read(_JS / "socket.js")
        assert "ui_prefs_loaded" in src, \
            "Must listen for ui_prefs_loaded event to restore server-side preferences"
        assert "sendBehavior" in src, \
            "Must restore sendBehavior from server preferences on connect"

    def test_toggle_function_exists(self):
        """Must have a toggle function accessible from the UI."""
        src = _read(_JS / "utils.js")
        assert re.search(r"function _toggleSendBehavior", src), \
            "_toggleSendBehavior must exist for the preference toggle UI"

    def test_refresh_hints_after_toggle(self):
        """After toggling, all visible send hints must be updated."""
        src = _read(_JS / "utils.js")
        match = re.search(
            r"function _toggleSendBehavior\([^)]*\)\s*\{([\s\S]*?)\n\}",
            src
        )
        assert match, "Could not find _toggleSendBehavior"
        body = match.group(1)
        assert "_refreshSendHints" in body, \
            "Must call _refreshSendHints after toggle — UI hints go stale otherwise"
