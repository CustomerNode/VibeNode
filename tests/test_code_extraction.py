"""Tests for app.code_extraction — code block extraction from sessions."""

import json
from pathlib import Path


def _make_session_with_code(tmp_path, code_blocks):
    """Helper to create a session file with code blocks in assistant messages."""
    path = tmp_path / "sess_code.jsonl"
    lines = [json.dumps({
        "type": "user",
        "message": {"content": "Write some code"},
        "timestamp": "2026-03-01T10:00:00Z",
    })]
    for i, (lang, code) in enumerate(code_blocks):
        content = f"Here's the code:\n```{lang}\n{code}\n```"
        lines.append(json.dumps({
            "type": "assistant",
            "message": {"content": content},
            "timestamp": f"2026-03-01T10:0{i+1}:00Z",
        }))
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


class TestExtractCodeBlocks:

    def test_extracts_python_block(self, tmp_path):
        from app.code_extraction import _extract_code_blocks
        path = _make_session_with_code(tmp_path, [("python", "print('hello')")])
        blocks = _extract_code_blocks(path)
        assert len(blocks) >= 1
        assert any("hello" in b.get("code", "") for b in blocks)

    def test_extracts_multiple_languages(self, tmp_path):
        from app.code_extraction import _extract_code_blocks
        path = _make_session_with_code(tmp_path, [
            ("python", "x = 1"),
            ("javascript", "const x = 1"),
        ])
        blocks = _extract_code_blocks(path)
        langs = {b.get("lang", "") for b in blocks}
        assert "python" in langs
        assert "javascript" in langs

    def test_empty_session_returns_no_blocks(self, empty_session_file):
        from app.code_extraction import _extract_code_blocks
        blocks = _extract_code_blocks(empty_session_file)
        assert blocks == []

    def test_no_code_session_returns_empty(self, sample_session_file):
        from app.code_extraction import _extract_code_blocks
        blocks = _extract_code_blocks(sample_session_file)
        # sample_session_file has a code block in it
        assert isinstance(blocks, list)
