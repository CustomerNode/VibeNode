"""Tests verifying performance optimization mechanisms exist and are correct.

These are structural/behavioral guards — they verify the optimization MECHANISM
is in place, not timing.  If any test fails, a performance-critical pattern
documented in CLAUDE.md has been accidentally removed or broken.
"""

import inspect
import threading
from concurrent.futures import ThreadPoolExecutor


class TestDetectChangedFilesGuard:
    """CLAUDE.md #1 — is_post_turn guard on _detect_changed_files."""

    def test_detect_changed_files_skipped_on_pre_turn(self):
        from daemon.session_manager import SessionManager
        src = inspect.getsource(SessionManager._write_file_snapshot)
        # The is_post_turn guard must protect _detect_changed_files
        assert "is_post_turn" in src
        assert "_detect_changed_files" in src


class TestAsyncioGatherInSendQuery:
    """CLAUDE.md #2 — asyncio.gather runs snapshot + mtimes in parallel."""

    def test_gather_present_in_send_query(self):
        from daemon.session_manager import SessionManager
        src = inspect.getsource(SessionManager._send_query)
        assert "asyncio.gather(" in src


class TestGetEntryCount:
    """CLAUDE.md #6 — get_entry_count returns int without serialization."""

    def test_get_entry_count_returns_int(self):
        from daemon.session_manager import SessionManager
        assert hasattr(SessionManager, "get_entry_count")
        hints = SessionManager.get_entry_count.__annotations__
        assert hints.get("return") is int


class TestCleanupNotInAllSessions:
    """CLAUDE.md #13 — _cleanup_system_sessions must NOT run per-request."""

    def test_cleanup_not_in_all_sessions(self):
        from app.sessions import all_sessions
        src = inspect.getsource(all_sessions)
        assert "_cleanup_system_sessions" not in src


class TestStatesCacheExists:
    """CLAUDE.md #10 — get_all_states() cache with 2s TTL."""

    def test_states_cache_exists(self):
        from app import session_awareness
        assert hasattr(session_awareness, "_states_cache_lock")
        assert isinstance(session_awareness._states_cache_lock, type(threading.Lock()))
        assert hasattr(session_awareness, "_STATES_CACHE_TTL")
        assert session_awareness._STATES_CACHE_TTL > 0


class TestKanbanConfigCacheExists:
    """CLAUDE.md #11 — get_kanban_config() cache with 10s TTL."""

    def test_kanban_config_cache_exists(self):
        from app import config
        # _kanban_config_cache is module-level (initially None)
        assert hasattr(config, "_kanban_config_cache")
        assert hasattr(config, "_KANBAN_CONFIG_CACHE_TTL")
        assert config._KANBAN_CONFIG_CACHE_TTL >= 10


class TestGitCacheTTLMinimum:
    """CLAUDE.md #9 — _GIT_LS_FILES_CACHE_TTL must be >= 120s."""

    def test_git_cache_ttl_minimum(self):
        from daemon.session_manager import SessionManager
        assert SessionManager._GIT_LS_FILES_CACHE_TTL >= 120


class TestSaveQueuesIsDebounced:
    """CLAUDE.md #8 — _save_queues uses a timer to debounce."""

    def test_save_queues_is_debounced(self):
        from daemon.session_manager import SessionManager
        src = inspect.getsource(SessionManager._save_queues)
        # Must reference the timer mechanism, not call _save_queues_now directly
        assert "Timer" in src or "_queue_save_timer" in src


class TestSetupExecutorIsModuleLevel:
    """CLAUDE.md #12 — _setup_executor must be a module-level ThreadPoolExecutor."""

    def test_setup_executor_is_module_level(self):
        from app.routes import ws_events
        assert hasattr(ws_events, "_setup_executor")
        assert isinstance(ws_events._setup_executor, ThreadPoolExecutor)


class TestTrackedFilesNotGrownBySnapshot:
    """CLAUDE.md #7 — fs_changed must NOT be added to tracked_files."""

    def test_tracked_files_not_grown_by_snapshot(self):
        from daemon.session_manager import SessionManager
        src = inspect.getsource(SessionManager._write_file_snapshot)
        # fs_snapshot_extras / fs_changed must exist but must NOT be
        # added to tracked_files via update/|=/add/extend
        assert "fs_snapshot_extras" in src or "fs_changed" in src
        # The snowball bug was: tracked_files.update(fs_changed)
        # or tracked_files |= fs_changed.  Neither should appear.
        assert "tracked_files.update(fs_changed)" not in src
        assert "tracked_files |= fs_changed" not in src
