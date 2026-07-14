"""
[subsessions phase -1] Behavioral regression test for PERF #4 mtime carry-forward.

CLAUDE.md PERF #4 says: ``_record_pre_turn_mtimes`` carries forward
``_post_turn_mtimes`` from the previous turn rather than re-walking
``git ls-files`` + ``stat()`` every file each turn.  This was a 60-80ms
hot-path saving on a typical project.

Today this invariant is only guarded by the structural test in
``tests/test_performance_guards.py`` (``test_detect_changed_files_skipped_on_pre_turn``)
which checks for the source string ``is_post_turn`` — that catches
deletion of the marker but NOT a refactor that keeps the string while
breaking the carry-forward branch.

This file adds a *behavioral* test: drive two consecutive
``_record_pre_turn_mtimes`` calls on the same SessionInfo and assert
that turn 2 makes zero new ``stat()`` syscalls when ``_post_turn_mtimes``
was populated by the prior turn.

See ``docs/plans/subsessions-spec.md`` §7.1 + §13.1 test 6.
"""

import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest


# ---------------------------------------------------------------------------
# Mock SDK types so importing daemon.session_manager is cheap & safe
# ---------------------------------------------------------------------------

class _MockSDKTypes:
    ClaudeSDKClient = MagicMock
    ClaudeCodeOptions = MagicMock
    AssistantMessage = MagicMock
    UserMessage = MagicMock
    ResultMessage = MagicMock
    StreamEvent = MagicMock
    TextBlock = MagicMock
    ThinkingBlock = MagicMock
    ToolUseBlock = MagicMock
    ToolResultBlock = MagicMock
    PermissionResultAllow = MagicMock
    PermissionResultDeny = MagicMock
    ContentBlock = MagicMock
    ToolPermissionContext = MagicMock
    Message = MagicMock


@pytest.fixture
def sm_module():
    import importlib
    sdk_mod = MagicMock()
    sdk_types_mod = MagicMock()
    for name in ("ClaudeSDKClient", "ClaudeCodeOptions"):
        setattr(sdk_mod, name, getattr(_MockSDKTypes, name))
    for name in (
        "AssistantMessage", "UserMessage", "ResultMessage", "StreamEvent",
        "TextBlock", "ThinkingBlock", "ToolUseBlock", "ToolResultBlock",
        "PermissionResultAllow", "PermissionResultDeny", "ContentBlock",
        "ToolPermissionContext", "Message",
    ):
        setattr(sdk_types_mod, name, getattr(_MockSDKTypes, name))
    with patch.dict(sys.modules, {
        "claude_code_sdk": sdk_mod,
        "claude_code_sdk.types": sdk_types_mod,
    }):
        import daemon.session_manager as sm
        importlib.reload(sm)
        yield sm


# ---------------------------------------------------------------------------
# Behavioral test
# ---------------------------------------------------------------------------

def test_second_turn_uses_carry_forward_no_new_stats(sm_module, tmp_path):
    """Turn 2 must NOT re-stat the working directory when turn 1 populated
    ``_post_turn_mtimes``.

    Setup:
      - Create a SessionInfo with ``cwd`` pointing at a tmp directory
        containing one source file.
      - Manually populate ``_post_turn_mtimes`` with a pre-known dict
        (simulating the side-effect of a previous turn's
        ``_detect_changed_files``).
      - Set ``_mtime_turn_count`` to a value that will NOT trigger the
        forced rescan (1 — next call increments to 2, which is not 0
        mod _MTIME_FULL_RESCAN_INTERVAL=10).

    Assertion: a stat-counting patch on ``Path.stat`` records zero calls
    on the source file during the second turn's ``_record_pre_turn_mtimes``.
    """
    SessionInfo = sm_module.SessionInfo
    manager = sm_module.SessionManager()

    # Create a real on-disk file so stat() would otherwise succeed.
    src_file = tmp_path / "hello.py"
    src_file.write_text("print('hi')\n", encoding="utf-8")

    info = SessionInfo(session_id="perf4-test", cwd=str(tmp_path))
    # Pre-populate _post_turn_mtimes the way a previous turn's
    # _detect_changed_files would have.
    info._post_turn_mtimes = {str(src_file): src_file.stat().st_mtime}
    # _mtime_turn_count is incremented inside _record_pre_turn_mtimes;
    # we set it so the *next* increment doesn't hit the forced rescan.
    info._mtime_turn_count = 1

    # Force file tracking on regardless of kanban_config.json.
    with patch.object(
        sm_module.SessionManager,
        "_is_file_tracking_enabled",
        staticmethod(lambda: True),
    ):
        # Count Path.stat calls.  We don't want to break stat — we just
        # want a tally.  Wrap rather than fully replace.
        stat_calls = {"count": 0}
        real_stat = Path.stat

        def _counting_stat(self, *args, **kwargs):
            # Count stats on files INSIDE the tmp dir — i.e. the rescan's
            # per-file stat() calls, which the carry-forward fast path must
            # skip.  Deliberately does NOT count a stat on tmp_path itself:
            # _record_pre_turn_mtimes always calls cwd_path.is_dir() (one
            # stat on the cwd dir) before the fast-path check, and that is
            # not part of the rescan.
            #
            # Compare via ``.parents`` (pure string manipulation, no
            # filesystem access).  The previous version called
            # ``Path(self).resolve()`` here, but resolve() itself calls
            # stat(), which re-enters this patched wrapper and recurses
            # infinitely on Windows (WindowsPath resolve → stat → resolve …).
            try:
                if Path(tmp_path) in Path(self).parents:
                    stat_calls["count"] += 1
            except OSError:
                pass
            return real_stat(self, *args, **kwargs)

        with patch.object(Path, "stat", _counting_stat):
            # Turn 2's _record_pre_turn_mtimes.  Because
            # _post_turn_mtimes was non-empty AND _mtime_turn_count
            # will become 2 (not 0 mod 10), this MUST hit the
            # carry-forward fast path and skip the rescan.
            manager._record_pre_turn_mtimes(info)

        assert stat_calls["count"] == 0, (
            f"PERF #4 regression: _record_pre_turn_mtimes performed "
            f"{stat_calls['count']} stat() calls on turn 2 when "
            "_post_turn_mtimes was carry-forwardable.  Expected zero "
            "(the carry-forward fast path should skip the rescan)."
        )

        # And the carry-forward semantics: pre_turn now equals the prior
        # post_turn, and post_turn was reset.
        assert info._pre_turn_mtimes == {
            str(src_file): src_file.stat().st_mtime,
        }
        assert info._post_turn_mtimes == {}


def test_first_turn_with_no_carry_forward_does_walk(sm_module, tmp_path):
    """Sanity counterpart: on the very first turn (no ``_post_turn_mtimes``
    yet) ``_record_pre_turn_mtimes`` MUST do real work — otherwise we'd
    never bootstrap the snapshot at all.

    This guards against an over-eager "always skip" optimization that
    would silently break first-turn file tracking.
    """
    SessionInfo = sm_module.SessionInfo
    manager = sm_module.SessionManager()

    src_file = tmp_path / "first.py"
    src_file.write_text("x = 1\n", encoding="utf-8")

    info = SessionInfo(session_id="perf4-first", cwd=str(tmp_path))
    # _post_turn_mtimes empty (the default).  No carry-forward source.
    assert info._post_turn_mtimes == {}

    with patch.object(
        sm_module.SessionManager,
        "_is_file_tracking_enabled",
        staticmethod(lambda: True),
    ):
        manager._record_pre_turn_mtimes(info)

    # Either git ls-files or os.walk ran; in both cases the tmp file's
    # mtime should be in the pre-turn snapshot.  We don't assert the
    # exact entry count (the tmp_path is outside git so git ls-files
    # returns nothing → falls back to os.walk → captures our .py file).
    assert str(src_file) in info._pre_turn_mtimes, (
        "First turn skipped the file walk.  This indicates the "
        "carry-forward branch is firing even when _post_turn_mtimes "
        "is empty, which would break the bootstrap snapshot."
    )
