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
        # After Phase 3 extraction, debounce logic lives in MessageQueue.
        # Check the actual implementation, not the thin wrapper.
        from daemon.message_queue import MessageQueue
        src = inspect.getsource(MessageQueue.save_queues)
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


class TestTrackedFilesNotGrownOnResume:
    """CLAUDE.md #7 — second snowball source: ``read_tracked_files`` must
    NOT re-feed file-history-snapshot ``trackedFileBackups`` entries
    into the in-memory ``tracked_files`` set.

    The original CLAUDE.md #7 fix kept fs_changed out of tracked_files
    in memory, but the same fs_snapshot_extras still landed in the JSONL
    via ``trackedFileBackups`` on every post-turn snapshot.  When a
    fresh ``SessionInfo`` is created (recovery, daemon restart, or
    resume), ``_prepopulate_tracked_files`` calls ``read_tracked_files``
    which scans the JSONL.  Source 1 (Edit/Write tool_use blocks) is
    canonical; source 2 (file-history-snapshot) was re-introducing the
    fs_snapshot_extras and snowballing the next pre-turn snapshot to
    130-380 s on Windows.

    These tests pin the invariant on both ends:
      - The source-2-doesn't-feed-found contract in claude_store.py.
      - End-to-end: a JSONL with one tool_use'd file but ten
        snapshot-only files yields exactly one tracked file.
    """

    def test_read_tracked_files_ignores_snapshot_only_files(self, tmp_path):
        """Source 2 (file-history-snapshot trackedFileBackups) entries
        without a corresponding tool_use must NOT inflate ``found``.
        Only the version counter (``max_version``) may be derived from
        them — the file path itself stays out of the returned set."""
        import json
        import uuid
        from app.config import _encode_cwd
        from daemon.backends.claude_store import ClaudeJsonlStore

        cwd = str(tmp_path / "proj")
        (tmp_path / "proj").mkdir()
        encoded = _encode_cwd(cwd)
        proj_dir = tmp_path / ".claude" / "projects" / encoded
        proj_dir.mkdir(parents=True)

        session_id = "snowball-test"
        edit_target = str(tmp_path / "proj" / "edited.py")
        snapshot_only_paths = [
            str(tmp_path / "proj" / f"bash_touched_{i}.py")
            for i in range(50)
        ]

        # Build a JSONL with:
        #  - one Edit tool_use → counts as a tracked file (source 1)
        #  - one file-history-snapshot whose trackedFileBackups dict
        #    holds the edited file PLUS 50 snapshot-only fs_extras
        lines = [
            json.dumps({
                "type": "user",
                "message": {"role": "user", "content": "edit it"},
                "uuid": str(uuid.uuid4()),
                "sessionId": session_id,
            }),
            json.dumps({
                "type": "assistant",
                "message": {
                    "role": "assistant",
                    "content": [{
                        "type": "tool_use",
                        "name": "Edit",
                        "id": str(uuid.uuid4()),
                        "input": {"file_path": edit_target},
                    }],
                },
                "uuid": str(uuid.uuid4()),
                "sessionId": session_id,
            }),
            json.dumps({
                "type": "file-history-snapshot",
                "messageId": str(uuid.uuid4()),
                "snapshot": {
                    "messageId": str(uuid.uuid4()),
                    "trackedFileBackups": {
                        edit_target: {
                            "backupFileName": "abc@v1",
                            "version": 7,
                            "backupTime": "2026-05-03T00:00:00Z",
                        },
                        **{
                            sp: {
                                "backupFileName": f"hash{i}@v3",
                                "version": 3,
                                "backupTime": "2026-05-03T00:00:00Z",
                            }
                            for i, sp in enumerate(snapshot_only_paths)
                        },
                    },
                    "timestamp": "2026-05-03T00:00:00Z",
                },
                "isSnapshotUpdate": True,
            }),
        ]
        (proj_dir / f"{session_id}.jsonl").write_text(
            "\n".join(lines) + "\n", encoding="utf-8"
        )

        store = ClaudeJsonlStore()
        # Patch Path.home() inside the store module so it points at tmp_path
        from unittest.mock import patch
        from pathlib import Path as _Path
        with patch("daemon.backends.claude_store.Path") as SP:
            SP.side_effect = _Path
            SP.home.return_value = tmp_path
            found, max_version, _u, _a = store.read_tracked_files(
                session_id, cwd=cwd
            )

        # Only the directly-edited file should be in `found`.  The 50
        # snapshot-only paths must NOT appear — that's the regression.
        assert found == {edit_target}, (
            f"snowball regression: read_tracked_files returned "
            f"{len(found)} files; expected exactly 1 (the Edit target). "
            f"Snapshot-only files must not be re-fed into tracked_files."
        )
        # Version counters can still come from the snapshot — that's
        # how new backup names avoid collisions on resume.
        assert max_version.get(edit_target) == 7

    def test_prepopulate_tracked_files_does_not_snowball(self, tmp_path):
        """End-to-end: SessionInfo with no in-memory tracked_files,
        scanning a JSONL whose snapshots reference 100 files but whose
        tool_use blocks reference only 2, must end with exactly 2
        files in ``info.tracked_files``."""
        import json
        import uuid
        from unittest.mock import patch
        from pathlib import Path as _Path

        from app.config import _encode_cwd
        from daemon.session_manager import (
            SessionManager, SessionInfo, SessionState,
        )

        cwd = str(tmp_path / "proj")
        (tmp_path / "proj").mkdir()
        encoded = _encode_cwd(cwd)
        proj_dir = tmp_path / ".claude" / "projects" / encoded
        proj_dir.mkdir(parents=True)

        session_id = "snowball-resume"
        directly_edited = [
            str(tmp_path / "proj" / "real_edit_a.py"),
            str(tmp_path / "proj" / "real_edit_b.py"),
        ]
        snapshot_only = [
            str(tmp_path / "proj" / f"fs_extra_{i}.py")
            for i in range(100)
        ]

        lines = [
            # Two real Edit tool_uses
            json.dumps({
                "type": "assistant",
                "message": {
                    "role": "assistant",
                    "content": [{
                        "type": "tool_use",
                        "name": "Edit",
                        "id": str(uuid.uuid4()),
                        "input": {"file_path": fp},
                    }],
                },
                "uuid": str(uuid.uuid4()),
                "sessionId": session_id,
            })
            for fp in directly_edited
        ] + [
            # One snapshot listing all 102 files (2 real + 100 fs-extras)
            json.dumps({
                "type": "file-history-snapshot",
                "messageId": str(uuid.uuid4()),
                "snapshot": {
                    "messageId": str(uuid.uuid4()),
                    "trackedFileBackups": {
                        fp: {
                            "backupFileName": f"hash@v1",
                            "version": 1,
                            "backupTime": "2026-05-03T00:00:00Z",
                        }
                        for fp in (directly_edited + snapshot_only)
                    },
                    "timestamp": "2026-05-03T00:00:00Z",
                },
                "isSnapshotUpdate": True,
            }),
        ]
        (proj_dir / f"{session_id}.jsonl").write_text(
            "\n".join(lines) + "\n", encoding="utf-8"
        )

        info = SessionInfo(
            session_id=session_id,
            cwd=cwd,
            state=SessionState.IDLE,
        )
        mgr = SessionManager()
        with mgr._lock:
            mgr._sessions[session_id] = info

        with patch("daemon.backends.claude_store.Path") as SP:
            SP.side_effect = _Path
            SP.home.return_value = tmp_path
            mgr._prepopulate_tracked_files(info)

        # tracked_files MUST contain only the 2 directly-edited files.
        # 100 snapshot-only entries must stay out — that's the snowball.
        assert info.tracked_files == set(directly_edited), (
            f"snowball-on-resume regression: prepopulate left "
            f"{len(info.tracked_files)} entries in tracked_files; "
            f"expected exactly 2 (the Edit/Write tool_use targets)."
        )


class TestWriteFileSnapshotShortCircuit:
    """Per-turn cost guard: ``_write_file_snapshot`` against many tracked
    files should be cheap on follow-up turns when nothing changed,
    AND must not grow ``info.tracked_files`` while doing so.

    Bounding the loop is what saved Windows users from 130-380 s
    pre-turn waits — Defender real-time scan + OneDrive synchronisation
    in the user's Documents/ folder made every read_bytes() multi-second.
    The (mtime, size) short-circuit means the 1000-file warm path
    stat()s every file but reads ~zero of them.
    """

    def test_unchanged_files_short_circuit_under_500ms(self, tmp_path):
        import time
        from unittest.mock import patch
        from pathlib import Path as _Path
        from app.config import _encode_cwd
        from daemon.session_manager import (
            SessionManager, SessionInfo, SessionState,
        )

        N = 1000
        cwd = str(tmp_path / "proj")
        (tmp_path / "proj").mkdir()
        encoded = _encode_cwd(cwd)
        proj_dir = tmp_path / ".claude" / "projects" / encoded
        proj_dir.mkdir(parents=True)
        session_id = "snap-perf"
        # Empty JSONL is fine — read_tail_uuids tolerates empty files
        (proj_dir / f"{session_id}.jsonl").write_text("", encoding="utf-8")

        # Materialize N small source files
        proj = tmp_path / "proj"
        files = []
        for i in range(N):
            fp = proj / f"file_{i:04d}.py"
            fp.write_text(f"# file {i}\n", encoding="utf-8")
            files.append(str(fp))

        info = SessionInfo(
            session_id=session_id,
            cwd=cwd,
            state=SessionState.IDLE,
        )
        info.tracked_files.update(files)

        mgr = SessionManager()
        with mgr._lock:
            mgr._sessions[session_id] = info

        # First call warms the (mtime, size) cache and writes backups.
        # We don't assert timing on this one — cold cache is allowed
        # to be slow.  We just need a successful warm-up.
        with patch("daemon.session_manager.Path") as MP, \
             patch("daemon.backends.claude_store.Path") as SP:
            MP.side_effect = _Path
            MP.home.return_value = tmp_path
            SP.side_effect = _Path
            SP.home.return_value = tmp_path
            mgr._write_file_snapshot(session_id, is_post_turn=False)

            tracked_before = len(info.tracked_files)

            # Second call: every file is unchanged → must short-circuit.
            t0 = time.perf_counter()
            mgr._write_file_snapshot(session_id, is_post_turn=False)
            elapsed = time.perf_counter() - t0

        # Snowball guard: warm path must NOT have grown tracked_files.
        assert len(info.tracked_files) == tracked_before, (
            f"tracked_files grew during a no-op snapshot: "
            f"{tracked_before} → {len(info.tracked_files)}"
        )

        # Performance guard: 1000 unchanged files in <500ms.
        # The short-circuit reduces this to N stat() calls + bookkeeping.
        # Real-world Windows numbers should be well under this; the
        # threshold is generous to absorb CI jitter on slow runners.
        assert elapsed < 0.5, (
            f"_write_file_snapshot took {elapsed:.3f}s for {N} unchanged "
            f"tracked files — short-circuit not engaging"
        )

    def test_post_turn_fs_extras_do_not_grow_tracked_files(self, tmp_path):
        """Pin CLAUDE.md #7 invariant: a post-turn snapshot whose
        ``_detect_changed_files`` returns 200 fs-modified paths must
        write them all into the JSONL backup dict yet leave
        ``info.tracked_files`` untouched."""
        from unittest.mock import patch
        from pathlib import Path as _Path
        from app.config import _encode_cwd
        from daemon.session_manager import (
            SessionManager, SessionInfo, SessionState,
        )

        cwd = str(tmp_path / "proj")
        (tmp_path / "proj").mkdir()
        encoded = _encode_cwd(cwd)
        proj_dir = tmp_path / ".claude" / "projects" / encoded
        proj_dir.mkdir(parents=True)
        session_id = "perf-extras"
        (proj_dir / f"{session_id}.jsonl").write_text("", encoding="utf-8")

        proj = tmp_path / "proj"
        # 1 directly-edited file + 200 fs_changed extras (e.g. Bash output)
        edited = proj / "edited.py"
        edited.write_text("v1\n", encoding="utf-8")
        extras = []
        for i in range(200):
            fp = proj / f"bash_out_{i:03d}.py"
            fp.write_text(f"out {i}\n", encoding="utf-8")
            extras.append(str(fp))

        info = SessionInfo(
            session_id=session_id,
            cwd=cwd,
            state=SessionState.IDLE,
        )
        info.tracked_files.add(str(edited))
        info._turn_had_direct_edit = False  # so fs fallback engages

        mgr = SessionManager()
        with mgr._lock:
            mgr._sessions[session_id] = info

        before = set(info.tracked_files)

        with patch("daemon.session_manager.Path") as MP, \
             patch("daemon.backends.claude_store.Path") as SP, \
             patch.object(SessionManager, "_detect_changed_files",
                          lambda self, info: set(extras)):
            MP.side_effect = _Path
            MP.home.return_value = tmp_path
            SP.side_effect = _Path
            SP.home.return_value = tmp_path
            mgr._write_file_snapshot(session_id, is_post_turn=True)

        # The 200 fs_extras land in the JSONL snapshot but MUST NOT
        # be added to in-memory tracked_files (CLAUDE.md #7).
        assert info.tracked_files == before, (
            f"CLAUDE.md #7 violation: post-turn fs_changed grew "
            f"tracked_files from {len(before)} to {len(info.tracked_files)}"
        )


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
