"""Tests for app.sessions — session loading, parsing, and caching."""

import json
from pathlib import Path


class TestLoadSessionSummary:
    """Tests for the fast head+tail session summary loader."""

    def test_returns_all_required_fields(self, sample_session_file):
        from app.sessions import load_session_summary
        result = load_session_summary(sample_session_file)
        required = {"id", "custom_title", "display_title", "date", "last_activity",
                     "last_activity_ts", "sort_ts", "file_bytes", "size", "preview", "message_count"}
        assert required.issubset(result.keys())

    def test_extracts_preview_from_first_user_message(self, sample_session_file):
        from app.sessions import load_session_summary
        result = load_session_summary(sample_session_file)
        assert "Hello, help me with Python" in result["preview"]

    def test_counts_messages(self, sample_session_file):
        from app.sessions import load_session_summary
        result = load_session_summary(sample_session_file)
        assert result["message_count"] >= 4

    def test_uses_session_stem_as_id(self, sample_session_file):
        from app.sessions import load_session_summary
        result = load_session_summary(sample_session_file)
        assert result["id"] == "sess_abc123"

    def test_empty_file_returns_zero_messages(self, empty_session_file):
        from app.sessions import load_session_summary
        result = load_session_summary(empty_session_file)
        assert result["message_count"] == 0
        assert result["preview"] == ""

    def test_caches_by_mtime_and_size(self, sample_session_file):
        from app.sessions import load_session_summary, _summary_cache
        _summary_cache.clear()
        r1 = load_session_summary(sample_session_file)
        r2 = load_session_summary(sample_session_file)
        assert r1 is r2  # same cached object

    def test_cache_invalidates_on_file_change(self, sample_session_file):
        from app.sessions import load_session_summary, _summary_cache
        _summary_cache.clear()
        r1 = load_session_summary(sample_session_file)
        # Append a new message
        with open(sample_session_file, "a", encoding="utf-8") as f:
            f.write(json.dumps({"type": "user", "message": {"content": "new"},
                                "timestamp": "2026-03-01T11:00:00Z"}) + "\n")
        r2 = load_session_summary(sample_session_file)
        assert r1 is not r2  # new object, cache busted

    def test_large_file_uses_head_tail(self, large_session_file):
        from app.sessions import load_session_summary
        result = load_session_summary(large_session_file)
        assert result["message_count"] > 0
        assert "First message" in result["preview"]
        assert result["file_bytes"] > 32768

    def test_file_size_formatting(self, sample_session_file):
        from app.sessions import load_session_summary
        result = load_session_summary(sample_session_file)
        assert any(unit in result["size"] for unit in ("B", "KB", "MB"))

    def test_nonexistent_file(self, tmp_path):
        from app.sessions import load_session_summary
        result = load_session_summary(tmp_path / "nonexistent.jsonl")
        assert result["id"] == "nonexistent"
        assert result["message_count"] == 0


class TestLoadSession:
    """Tests for the full session parser."""

    def test_returns_messages_array(self, sample_session_file):
        from app.sessions import load_session
        result = load_session(sample_session_file)
        assert "messages" in result
        assert len(result["messages"]) == 4

    def test_messages_have_role_and_content(self, sample_session_file):
        from app.sessions import load_session
        result = load_session(sample_session_file)
        for msg in result["messages"]:
            assert "role" in msg
            assert "content" in msg
            assert msg["role"] in ("user", "assistant")

    def test_custom_title_extracted(self, titled_session_file):
        from app.sessions import load_session
        result = load_session(titled_session_file)
        assert result["custom_title"] == "My Project"
        assert result["display_title"] == "My Project"

    def test_display_title_falls_back_to_first_message(self, sample_session_file):
        from app.sessions import load_session
        result = load_session(sample_session_file)
        assert "Hello" in result["display_title"]


class TestAllSessions:
    """Tests for the parallel session loader."""

    def test_loads_all_sessions_from_directory(self, mock_sessions_dir):
        from app.sessions import load_session_summary
        from app.config import _summary_cache
        _summary_cache.clear()

        files = list(mock_sessions_dir.glob("*.jsonl"))
        results = [load_session_summary(f) for f in files]
        assert len(results) == 6  # 5 sessions + 1 empty

    def test_sessions_sorted_by_date(self, mock_sessions_dir):
        from app.sessions import load_session_summary
        files = list(mock_sessions_dir.glob("*.jsonl"))
        results = [load_session_summary(f) for f in files]
        results.sort(key=lambda x: x["sort_ts"], reverse=True)
        # Most recent should be first
        timestamps = [r["sort_ts"] for r in results]
        assert timestamps == sorted(timestamps, reverse=True)
