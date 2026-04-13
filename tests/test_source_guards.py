"""Source guards for critical patterns that must not be "simplified" away.

Each test reads production source code and verifies that specific patterns
are present. These patterns exist to solve real, hard-to-debug problems
(Windows focus issues, daemon latency, race conditions, IPC reliability).
Removing them causes silent failures.

See CLAUDE.md "Performance-critical patterns" section for full documentation.
"""

import re
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
_JS = _ROOT / "static" / "js"


def _read(path):
    return path.read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# Python: Daemon startup patterns (run.py)
# ---------------------------------------------------------------------------

class TestDaemonStartupGuards:

    def test_daemon_uses_create_no_window(self):
        """Daemon subprocess must use CREATE_NO_WINDOW on Windows to prevent
        console flash when launched from launch.bat."""
        src = _read(_ROOT / "run.py")
        assert "CREATE_NO_WINDOW" in src, \
            "Daemon subprocess must use CREATE_NO_WINDOW flag on Windows"

    def test_daemon_uses_create_new_process_group(self):
        """Daemon must use CREATE_NEW_PROCESS_GROUP so it survives web server
        restarts. Without this, killing the web server kills the daemon too."""
        src = _read(_ROOT / "run.py")
        assert "CREATE_NEW_PROCESS_GROUP" in src, \
            "Daemon must use CREATE_NEW_PROCESS_GROUP to survive web server restarts"


# ---------------------------------------------------------------------------
# Python: IPC patterns (daemon_client.py)
# ---------------------------------------------------------------------------

class TestDaemonClientGuards:

    def test_separate_reader_and_emitter_threads(self):
        """DaemonClient must have separate reader and emitter threads.
        If SocketIO emit blocks (network backpressure), the reader thread
        would stall and miss IPC responses, causing daemon timeouts."""
        src = _read(_ROOT / "app" / "daemon_client.py")
        assert "daemon-reader" in src, "Must have dedicated reader thread"
        assert "socketio-emitter" in src, "Must have dedicated emitter thread"

    def test_deferred_resync_after_reader_start(self):
        """Resync must happen AFTER reader thread starts. If resync sends IPC
        requests before the reader is listening, it blocks for 30 seconds."""
        src = _read(_ROOT / "app" / "daemon_client.py")
        assert "daemon-resync" in src, "Must have deferred resync thread"


# ---------------------------------------------------------------------------
# Python: Performance-critical daemon patterns
# ---------------------------------------------------------------------------

class TestDaemonPerfGuards:

    def _daemon_src(self):
        return _read(_ROOT / "daemon" / "session_manager.py")

    def test_asyncio_gather_for_parallel_ops(self):
        """PERF-CRITICAL: _write_file_snapshot and _record_pre_turn_mtimes must
        run in parallel via asyncio.gather. Sequential awaits add 60-70ms."""
        src = self._daemon_src()
        assert "asyncio.gather" in src, \
            "Must use asyncio.gather for parallel file operations (60-70ms savings)"

    def test_git_ls_files_cache_ttl_above_120s(self):
        """PERF-CRITICAL: Git ls-files cache TTL must be >= 120s. Below that
        causes excessive git subprocess calls in rapid-turn sessions."""
        src = self._daemon_src()
        match = re.search(r'_GIT_LS_FILES_CACHE_TTL\s*=\s*(\d+)', src)
        assert match, "Must have _GIT_LS_FILES_CACHE_TTL constant"
        ttl = int(match.group(1))
        assert ttl >= 120, \
            f"Git ls-files cache TTL is {ttl}s, must be >= 120s to prevent subprocess spam"

    def test_get_entry_count_no_serialization(self):
        """PERF-CRITICAL: get_entry_count must return len(entries) directly,
        NOT serialize via get_entries. Direct: 0-1ms. Serialized: 25-32ms."""
        src = self._daemon_src()
        assert re.search(r'def get_entry_count.*?len\(', src, re.DOTALL), \
            "get_entry_count must use len() directly, not serialize entries"

    def test_is_post_turn_guard_on_detect_changed(self):
        """PERF-CRITICAL: _detect_changed_files must only run post-turn.
        Pre-turn calls cause a 199-file scan per message (+2-138ms)."""
        src = self._daemon_src()
        assert "is_post_turn" in src, \
            "Must have is_post_turn guard on _detect_changed_files"

    def test_debounced_save_queues(self):
        """PERF-CRITICAL: Queue saves must be debounced (1-second timer).
        Direct _save_queues_now() on every operation adds disk write latency."""
        src = self._daemon_src()
        assert "_save_queues" in src and "timer" in src.lower(), \
            "Queue saves must be debounced, not synchronous"


# ---------------------------------------------------------------------------
# JavaScript: Performance-critical patterns
# ---------------------------------------------------------------------------

class TestJsPerfGuards:

    def test_all_session_ids_set_exists(self):
        """PERF-CRITICAL: allSessionIds must be a Set for O(1) lookups.
        Without it, streaming handler runs allSessions.find() on every token —
        O(n) × 100 tokens/turn × 50 sessions = 5,000 linear scans per response."""
        src = _read(_JS / "app.js")
        assert "allSessionIds" in src, "Must have allSessionIds Set"
        assert re.search(r'new Set\b', src), \
            "allSessionIds must be initialized as a Set"
        assert ".has(" in src, "Must use .has() for O(1) lookups"

    def test_watchdog_dedup_globals(self):
        """PERF-CRITICAL: Watchdog dedup requires window._watchdogSid and
        window._watchdogTimer. Without them, per-submit and background
        watchdogs both monitor the same session, doubling IPC load."""
        src = _read(_JS / "live-panel.js")
        assert "window._watchdogSid" in src or "_watchdogSid" in src, \
            "Must have watchdog session ID for cross-script dedup"
        assert "window._watchdogTimer" in src or "_watchdogTimer" in src, \
            "Must have watchdog timer for cross-script dedup"

    def test_performance_marks_exist(self):
        """PERF-CRITICAL: performance.mark() instrumentation must exist for
        submit timing and session switch timing. Only client-side timing data."""
        src = _read(_JS / "socket.js")
        assert "performance.mark" in src, \
            "Must have performance.mark() instrumentation in socket.js"


# ---------------------------------------------------------------------------
# JavaScript: Slash command interception
# ---------------------------------------------------------------------------

class TestSlashCommandGuards:

    def test_slash_commands_intercepted_client_side(self):
        """Slash commands must be intercepted BEFORE being sent to the SDK.
        The SDK silently eats them with no response, leaving the session stuck."""
        # Check for the interception function in any JS file
        found = False
        for js_file in _JS.glob("*.js"):
            src = js_file.read_text(encoding="utf-8")
            if "_interceptSlashCommand" in src or "_slashCommandMap" in src:
                found = True
                break
        assert found, \
            "Must have client-side slash command interception — " \
            "SDK silently eats slash commands with no response"


# ---------------------------------------------------------------------------
# Subprocess: CREATE_NO_WINDOW consistency
# ---------------------------------------------------------------------------

class TestSubprocessFlagGuards:

    def test_git_ops_uses_no_window(self):
        """All git subprocess calls must use CREATE_NO_WINDOW on Windows."""
        src = _read(_ROOT / "app" / "git_ops.py")
        assert "NO_WINDOW" in src or "CREATE_NO_WINDOW" in src, \
            "git_ops.py must use NO_WINDOW flag for subprocess calls"

    def test_process_detection_uses_no_window(self):
        """Process detection subprocess calls must use CREATE_NO_WINDOW."""
        src = _read(_ROOT / "app" / "process_detection.py")
        assert "NO_WINDOW" in src or "CREATE_NO_WINDOW" in src, \
            "process_detection.py must use NO_WINDOW flag"
