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


class TestPerfCriticalProximityGuard:
    """Automated check that detects modifications near PERF-CRITICAL markers.

    This converts the "don't touch PERF-CRITICAL code" doctrine from
    CLAUDE.md into an automated guard. If any staged or committed changes
    are within 5 lines of a PERF-CRITICAL comment, the test outputs a
    warning with the specific location.

    The test itself always passes — it's a detection/warning mechanism,
    not a blocker. The warning surfaces in test output so developers and
    AI agents are aware they're touching performance-sensitive code.
    """

    PROXIMITY_LINES = 5  # Lines of context around PERF-CRITICAL markers

    @staticmethod
    def _find_perf_critical_lines(filepath):
        """Return line numbers containing PERF-CRITICAL in a file."""
        import pathlib
        path = pathlib.Path(filepath)
        if not path.exists():
            return []
        try:
            lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        except Exception:
            return []
        return [i + 1 for i, line in enumerate(lines) if "PERF-CRITICAL" in line]

    @staticmethod
    def _get_changed_lines_from_diff(diff_output):
        """Parse unified diff output to extract (filepath, line_number) tuples
        for changed lines (added or modified)."""
        import re
        results = []
        current_file = None
        for line in diff_output.splitlines():
            # Match diff header: +++ b/path/to/file
            if line.startswith("+++ b/"):
                current_file = line[6:]
            # Match hunk header: @@ -old,count +new,count @@
            elif line.startswith("@@") and current_file:
                match = re.search(r"\+(\d+)", line)
                if match:
                    hunk_start = int(match.group(1))
                    line_offset = 0
            # Match added/changed lines (starting with +, not +++)
            elif line.startswith("+") and not line.startswith("+++") and current_file:
                try:
                    results.append((current_file, hunk_start + line_offset))
                except NameError:
                    pass
                line_offset += 1
            elif not line.startswith("-") and current_file and not line.startswith("\\"):
                try:
                    line_offset += 1
                except NameError:
                    pass
        return results

    def test_perf_critical_markers_exist(self):
        """Verify that PERF-CRITICAL markers exist in the codebase.
        If they don't, the guard has nothing to protect."""
        import pathlib
        project_root = pathlib.Path(__file__).resolve().parents[1]
        sm_path = project_root / "daemon" / "session_manager.py"
        markers = self._find_perf_critical_lines(str(sm_path))
        assert len(markers) > 0, (
            "No PERF-CRITICAL markers found in session_manager.py — "
            "the performance guard has nothing to protect"
        )

    def test_proximity_detection_logic(self):
        """Verify the proximity detection works correctly with
        synthetic data."""
        import tempfile, pathlib
        # Create a temporary file with a PERF-CRITICAL marker
        content = "\n".join([
            "# line 1",
            "# line 2",
            "# PERF-CRITICAL: Do not change this",
            "critical_code = True",
            "# line 5",
            "# line 6",
            "# line 7",
            "normal_code = True",
            "# line 9",
            "# line 10",
        ])
        with tempfile.NamedTemporaryFile(mode="w", suffix=".py",
                                         delete=False, encoding="utf-8") as f:
            f.write(content)
            tmppath = f.name

        try:
            markers = self._find_perf_critical_lines(tmppath)
            assert 3 in markers  # Line 3 has the marker

            # Simulate a change on line 4 (within 5 lines of marker at line 3)
            changed = [(tmppath, 4)]
            warnings = []
            for filepath, line_no in changed:
                for marker_line in markers:
                    if abs(line_no - marker_line) <= self.PROXIMITY_LINES:
                        warnings.append(
                            f"Change at {filepath}:{line_no} is within "
                            f"{self.PROXIMITY_LINES} lines of PERF-CRITICAL "
                            f"marker at line {marker_line}"
                        )
            assert len(warnings) == 1

            # Simulate a change on line 10 (beyond 5 lines)
            changed_far = [(tmppath, 10)]
            warnings_far = []
            for filepath, line_no in changed_far:
                for marker_line in markers:
                    if abs(line_no - marker_line) <= self.PROXIMITY_LINES:
                        warnings_far.append("too close")
            assert len(warnings_far) == 0
        finally:
            pathlib.Path(tmppath).unlink(missing_ok=True)

    def test_detect_changes_near_perf_critical(self):
        """Scan the current git diff (staged + unstaged) for changes near
        PERF-CRITICAL markers. Outputs warnings but does not fail.

        This test surfaces proximity warnings in CI output or local test
        runs, making developers aware they are touching perf-sensitive code.
        """
        import subprocess, pathlib
        project_root = pathlib.Path(__file__).resolve().parents[1]

        # Get staged + unstaged diff
        try:
            result = subprocess.run(
                ["git", "diff", "HEAD"],
                capture_output=True, cwd=str(project_root),
                timeout=10,
            )
            diff_output = result.stdout.decode("utf-8", errors="replace")
        except (subprocess.SubprocessError, FileNotFoundError):
            # Git not available or no commits — skip silently
            return

        if not diff_output.strip():
            return  # No changes to check

        changed_lines = self._get_changed_lines_from_diff(diff_output)
        if not changed_lines:
            return

        # Build a map of PERF-CRITICAL marker locations per file
        perf_files = {}
        for filepath, _ in changed_lines:
            if filepath not in perf_files:
                abs_path = project_root / filepath
                perf_files[filepath] = self._find_perf_critical_lines(str(abs_path))

        # Check proximity
        warnings = []
        for filepath, line_no in changed_lines:
            markers = perf_files.get(filepath, [])
            for marker_line in markers:
                if abs(line_no - marker_line) <= self.PROXIMITY_LINES:
                    warnings.append(
                        f"  WARNING: {filepath}:{line_no} is within "
                        f"{self.PROXIMITY_LINES} lines of PERF-CRITICAL "
                        f"marker at line {marker_line}"
                    )

        if warnings:
            import sys
            msg = (
                "\n\n=== PERF-CRITICAL PROXIMITY WARNING ===\n"
                "The following changes are near PERF-CRITICAL code markers.\n"
                "Review CLAUDE.md for the performance reason before proceeding.\n\n"
                + "\n".join(warnings)
                + "\n\n========================================\n"
            )
            # Print to stderr so it's visible in test output
            print(msg, file=sys.stderr)
