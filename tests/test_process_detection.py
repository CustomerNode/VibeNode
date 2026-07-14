"""Tests for app.process_detection — tail read, waiting state, session kind.

The waiting/kind parsers key off file mtime (a recently-written file means
Claude is still active, so it is NOT "waiting").  Tests therefore age the
file with ``os.utime`` to put it past the relevant idle thresholds; without
that, every parse returns the live/"too recent" short-circuit and the real
logic is never exercised — which is exactly why the original waiting-state
test could only assert ``result is not None or result is None``.
"""

import json
import os
import time

import pytest


def _write_jsonl(path, objs):
    """Write a list of dict entries as one JSONL file."""
    path.write_text(
        "\n".join(json.dumps(o) for o in objs) + "\n", encoding="utf-8"
    )


def _age_file(path, seconds):
    """Backdate a file's mtime so idle-threshold checks see it as stale."""
    past = time.time() - seconds
    os.utime(path, (past, past))


# ---------------------------------------------------------------------------
# _tail_read_lines
# ---------------------------------------------------------------------------

class TestTailReadLines:

    def test_small_file_reads_all(self, tmp_path):
        from app.process_detection import _tail_read_lines
        f = tmp_path / "small.txt"
        f.write_text("line1\nline2\nline3\n", encoding="utf-8")
        assert _tail_read_lines(f) == ["line1", "line2", "line3"]

    def test_large_file_reads_tail(self, tmp_path):
        from app.process_detection import _tail_read_lines
        f = tmp_path / "large.txt"
        content = "\n".join(f"line-{i}" for i in range(2000))
        f.write_text(content, encoding="utf-8")
        lines = _tail_read_lines(f, tail_bytes=1024)
        assert 0 < len(lines) < 2000  # tail only, not the whole file
        assert lines[-1] == "line-1999"

    def test_large_file_skips_partial_first_line(self, tmp_path):
        from app.process_detection import _tail_read_lines
        f = tmp_path / "partial.txt"
        # Fixed-width lines so we can reason about the cut point.
        f.write_text("\n".join("X" * 50 for _ in range(500)) + "\n",
                     encoding="utf-8")
        lines = _tail_read_lines(f, tail_bytes=200)
        # Every returned line must be intact (the sliced-through first line
        # is dropped by the readline() skip), so all are full width.
        assert all(len(ln) == 50 for ln in lines)

    def test_blank_lines_are_stripped(self, tmp_path):
        from app.process_detection import _tail_read_lines
        f = tmp_path / "blanks.txt"
        f.write_text("a\n\n  \nb\n", encoding="utf-8")
        assert _tail_read_lines(f) == ["a", "b"]

    def test_nonexistent_file_returns_empty(self, tmp_path):
        from app.process_detection import _tail_read_lines
        assert _tail_read_lines(tmp_path / "nope.txt") == []

    def test_empty_file_returns_empty(self, tmp_path):
        from app.process_detection import _tail_read_lines
        f = tmp_path / "empty.txt"
        f.write_text("", encoding="utf-8")
        assert _tail_read_lines(f) == []


# ---------------------------------------------------------------------------
# _parse_waiting_state
# ---------------------------------------------------------------------------

class TestParseWaitingState:

    def test_tool_permission_detected(self, tmp_path):
        from app.process_detection import _parse_waiting_state
        f = tmp_path / "session.jsonl"
        _write_jsonl(f, [
            {"type": "user", "message": {"content": "write the file"}},
            {"type": "assistant", "message": {
                "content": [{"type": "tool_use", "name": "Write",
                             "input": {"file_path": "/tmp/x"}}],
                "stop_reason": "tool_use"}},
        ])
        _age_file(f, 10)  # past the 3s tool-idle threshold
        result = _parse_waiting_state(f)
        assert result is not None
        assert result["kind"] == "tool"
        assert result["options"] == ["y", "n", "a"]
        assert "Write" in result["question"]

    def test_text_question_detected(self, tmp_path):
        from app.process_detection import _parse_waiting_state
        f = tmp_path / "session.jsonl"
        _write_jsonl(f, [
            {"type": "assistant",
             "message": {"content": "Should I proceed with the refactor?"}},
        ])
        _age_file(f, 10)  # past the 6s text-idle threshold
        result = _parse_waiting_state(f)
        assert result is not None
        assert result["kind"] == "text"

    def test_completion_message_is_not_waiting(self, tmp_path):
        from app.process_detection import _parse_waiting_state
        f = tmp_path / "session.jsonl"
        _write_jsonl(f, [
            {"type": "assistant",
             "message": {"content": "Done. I've saved the file."}},
        ])
        _age_file(f, 10)
        # No "?" and no option list → a completion, not a question.
        assert _parse_waiting_state(f) is None

    def test_last_message_from_user_is_processing(self, tmp_path):
        from app.process_detection import _parse_waiting_state
        f = tmp_path / "session.jsonl"
        _write_jsonl(f, [
            {"type": "assistant", "message": {"content": "Working on it?"}},
            {"type": "user", "message": {"content": "go ahead"}},
        ])
        _age_file(f, 10)
        # Last meaningful entry is the user → Claude is processing, not waiting.
        assert _parse_waiting_state(f) is None

    def test_tool_with_later_activity_is_not_pending(self, tmp_path):
        from app.process_detection import _parse_waiting_state
        f = tmp_path / "session.jsonl"
        _write_jsonl(f, [
            {"type": "assistant", "message": {
                "content": [{"type": "tool_use", "name": "Bash",
                             "input": {"command": "ls"}}],
                "stop_reason": "tool_use"}},
            {"type": "progress", "message": {"content": "running"}},
        ])
        _age_file(f, 10)
        # A progress entry after the tool_use means it already started.
        assert _parse_waiting_state(f) is None

    def test_recent_file_is_not_waiting(self, tmp_path):
        from app.process_detection import _parse_waiting_state
        f = tmp_path / "session.jsonl"
        _write_jsonl(f, [
            {"type": "assistant", "message": {
                "content": [{"type": "tool_use", "name": "Write",
                             "input": {"file_path": "/tmp/x"}}],
                "stop_reason": "tool_use"}},
        ])
        # Freshly written (mtime ~now) → under the 3s threshold → not waiting.
        assert _parse_waiting_state(f) is None

    def test_tool_result_last_is_processing(self, tmp_path):
        from app.process_detection import _parse_waiting_state
        f = tmp_path / "session.jsonl"
        _write_jsonl(f, [
            {"type": "assistant", "message": {
                "content": [{"type": "tool_result", "content": "output"}]}},
        ])
        _age_file(f, 10)
        # tool_result means Claude just received output → processing, not waiting.
        assert _parse_waiting_state(f) is None

    def test_empty_file_returns_none(self, tmp_path):
        from app.process_detection import _parse_waiting_state
        f = tmp_path / "empty.jsonl"
        f.write_text("", encoding="utf-8")
        _age_file(f, 10)
        assert _parse_waiting_state(f, has_live_pid=False) is None

    def test_corrupted_lines_return_none(self, tmp_path):
        from app.process_detection import _parse_waiting_state
        f = tmp_path / "corrupt.jsonl"
        f.write_text("{not valid json\n}}}also bad\n", encoding="utf-8")
        _age_file(f, 10)
        # Unparseable lines must not raise — they yield "not waiting".
        assert _parse_waiting_state(f) is None


# ---------------------------------------------------------------------------
# _parse_session_kind
# ---------------------------------------------------------------------------

class TestParseSessionKind:

    def test_empty_file_is_idle(self, tmp_path):
        from app.process_detection import _parse_session_kind
        f = tmp_path / "empty.jsonl"
        f.write_text("", encoding="utf-8")
        assert _parse_session_kind(f) == "idle"

    def test_recent_file_is_working(self, tmp_path):
        from app.process_detection import _parse_session_kind
        f = tmp_path / "recent.jsonl"
        _write_jsonl(f, [
            {"type": "assistant",
             "message": {"content": "hi", "stop_reason": "end_turn"}},
        ])
        # mtime ~now → file_age < 10 → always working.
        assert _parse_session_kind(f) == "working"

    def test_tool_use_stop_reason_is_working(self, tmp_path):
        from app.process_detection import _parse_session_kind
        f = tmp_path / "tool.jsonl"
        _write_jsonl(f, [
            {"type": "assistant", "message": {
                "content": [{"type": "tool_use", "name": "Bash",
                             "input": {"command": "make"}}],
                "stop_reason": "tool_use"}},
        ])
        _age_file(f, 30)  # stale, but tool_use → still working
        assert _parse_session_kind(f) == "working"

    def test_end_turn_after_user_text_is_idle(self, tmp_path):
        from app.process_detection import _parse_session_kind
        f = tmp_path / "done.jsonl"
        _write_jsonl(f, [
            {"type": "user", "message": {"content": "what is 2+2"}},
            {"type": "assistant",
             "message": {"content": "4", "stop_reason": "end_turn"}},
        ])
        _age_file(f, 30)
        # User asked → Claude answered with end_turn → stale → idle.
        assert _parse_session_kind(f) == "idle"

    def test_tool_result_last_with_live_pid_is_working(self, tmp_path):
        from app.process_detection import _parse_session_kind
        f = tmp_path / "result.jsonl"
        _write_jsonl(f, [
            {"type": "assistant", "message": {
                "content": [{"type": "tool_use", "name": "Bash",
                             "input": {"command": "ls"}}],
                "stop_reason": "tool_use"}},
            {"type": "user", "message": {
                "content": [{"type": "tool_result", "content": "files"}]}},
        ])
        _age_file(f, 30)
        # A confirmed live PID overrides the stale-file heuristic.
        assert _parse_session_kind(f, has_live_pid=True) == "working"

    def test_corrupted_only_lines_default_to_working(self, tmp_path):
        from app.process_detection import _parse_session_kind
        f = tmp_path / "corrupt.jsonl"
        f.write_text("{{{garbage\nmore !!! garbage\n", encoding="utf-8")
        _age_file(f, 30)
        # Lines exist but none parse → safe default is "working".
        assert _parse_session_kind(f) == "working"


# ---------------------------------------------------------------------------
# _enumerate_claude_processes — failure isolation
# ---------------------------------------------------------------------------

class TestEnumerateProcesses:

    def test_subprocess_timeout_returns_empty(self, monkeypatch):
        import subprocess
        from app.process_detection import _enumerate_claude_processes
        monkeypatch.setattr(
            subprocess, "run",
            lambda *a, **k: (_ for _ in ()).throw(
                subprocess.TimeoutExpired("cmd", 10)))
        assert _enumerate_claude_processes() == []

    def test_empty_result_returns_list(self, monkeypatch):
        import subprocess
        from unittest.mock import MagicMock
        from app.process_detection import _enumerate_claude_processes
        mock_result = MagicMock()
        mock_result.stdout = "[]"
        mock_result.returncode = 0
        monkeypatch.setattr(subprocess, "run",
                            MagicMock(return_value=mock_result))
        assert isinstance(_enumerate_claude_processes(), list)
