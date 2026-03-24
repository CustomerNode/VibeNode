"""Shared test fixtures for VibeNode."""

import json
import pytest
from pathlib import Path
from datetime import datetime, timezone


def _make_session_line(msg_type, content="", timestamp=None):
    """Build a single JSONL line for a mock session file."""
    ts = timestamp or datetime.now(timezone.utc).isoformat()
    if msg_type == "custom-title":
        return json.dumps({"type": "custom-title", "customTitle": content})
    return json.dumps({
        "type": msg_type,
        "message": {"content": content},
        "timestamp": ts,
    })


@pytest.fixture
def sample_session_file(tmp_path):
    """Create a single .jsonl session file with a few messages."""
    path = tmp_path / "sess_abc123.jsonl"
    lines = [
        _make_session_line("user", "Hello, help me with Python", "2026-03-01T10:00:00Z"),
        _make_session_line("assistant", "Sure! What do you need?", "2026-03-01T10:00:05Z"),
        _make_session_line("user", "Write a fibonacci function", "2026-03-01T10:01:00Z"),
        _make_session_line("assistant", "Here's a fibonacci function:\n```python\ndef fib(n):\n    if n <= 1: return n\n    return fib(n-1) + fib(n-2)\n```", "2026-03-01T10:01:10Z"),
    ]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


@pytest.fixture
def empty_session_file(tmp_path):
    """Create an empty .jsonl session file."""
    path = tmp_path / "sess_empty.jsonl"
    path.write_text("", encoding="utf-8")
    return path


@pytest.fixture
def titled_session_file(tmp_path):
    """Create a session with a custom title."""
    path = tmp_path / "sess_titled.jsonl"
    lines = [
        _make_session_line("custom-title", "My Project"),
        _make_session_line("user", "Let's build something", "2026-03-01T12:00:00Z"),
        _make_session_line("assistant", "Sounds good!", "2026-03-01T12:00:05Z"),
    ]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


@pytest.fixture
def mock_sessions_dir(tmp_path):
    """Create a directory with multiple session files, mimicking ~/.claude/projects/xxx/."""
    project_dir = tmp_path / "projects" / "C--Users-test-project"
    project_dir.mkdir(parents=True)

    for i in range(5):
        path = project_dir / f"session_{i:03d}.jsonl"
        lines = [
            _make_session_line("user", f"Question {i}", f"2026-03-0{i+1}T10:00:00Z"),
            _make_session_line("assistant", f"Answer {i}", f"2026-03-0{i+1}T10:00:05Z"),
        ]
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    # Add an empty session
    empty = project_dir / "session_empty.jsonl"
    empty.write_text("", encoding="utf-8")

    # Add names file
    names = {"session_000": "First Session", "session_001": "Second Session"}
    (project_dir / "_session_names.json").write_text(json.dumps(names), encoding="utf-8")

    return project_dir


@pytest.fixture
def large_session_file(tmp_path):
    """Create a large session file (>32KB) to test head+tail reading."""
    path = tmp_path / "sess_large.jsonl"
    lines = [_make_session_line("user", "First message", "2026-01-01T00:00:00Z")]
    # Add many assistant messages to push file over 32KB
    for i in range(200):
        lines.append(_make_session_line(
            "assistant",
            f"Response {i}: " + "x" * 150,
            f"2026-01-01T00:{i // 60:02d}:{i % 60:02d}Z"
        ))
    lines.append(_make_session_line("user", "Last message", "2026-01-01T12:00:00Z"))
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path
