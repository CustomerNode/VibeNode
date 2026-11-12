"""/api/session-steps: plain words and real changes read from a transcript."""
from __future__ import annotations

import json

from web.routes.live_api import _read_session_steps


def _line(obj):
    return json.dumps(obj) + "\n"


def test_reads_a_commands_words_and_a_changes_real_lines(tmp_path):
    p = tmp_path / "s.jsonl"
    p.write_text(
        _line({"type": "assistant", "message": {"content": [
            {"type": "tool_use", "id": "b1", "name": "Bash", "input": {"command": "pytest", "description": "Run the tests"}},
            {"type": "tool_use", "id": "e1", "name": "Edit", "input": {"file_path": "/a/b.py", "old_string": "x", "new_string": "y"}},
            {"type": "tool_use", "id": "other", "name": "Bash", "input": {"command": "secret", "description": "not asked for"}},
        ]}})
        + "this line is not json e1\n"
        + _line({"type": "user", "toolUseResult": {"structuredPatch": [{"newStart": 7, "lines": ["-x", "+y"]}]},
                 "message": {"content": [{"type": "tool_result", "tool_use_id": "e1", "content": "ok"}]}})
    )
    got = _read_session_steps(p, {"b1", "e1"})
    assert got["b1"]["label"] == "Run the tests"
    assert got["e1"]["file"] == "/a/b.py"
    assert got["e1"]["patch"] == [{"newStart": 7, "lines": ["-x", "+y"]}]
    assert "other" not in got
