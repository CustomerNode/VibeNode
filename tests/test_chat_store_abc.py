"""Tests for ChatStore abstract base class.

Validates the abstract interface defined in daemon/backends/chat_store.py.
"""

import pytest
from pathlib import Path
from typing import Optional

from daemon.backends.chat_store import ChatStore


# ---------------------------------------------------------------------------
# Minimal concrete implementation for testing
# ---------------------------------------------------------------------------

class StubChatStore(ChatStore):
    """Minimal concrete implementation that satisfies all abstract methods."""

    def find_session_path(
        self, session_id: str, cwd: str = ""
    ) -> Optional[Path]:
        return None

    def read_tracked_files(
        self, session_id: str, cwd: str = ""
    ) -> tuple:
        return (set(), {}, "", "")

    def read_tail_uuids(
        self, session_id: str, cwd: str = ""
    ) -> tuple:
        return ("", "")

    def write_snapshot(
        self, session_id: str, snapshot: dict, cwd: str = ""
    ) -> None:
        pass

    def repair_incomplete_turn(
        self, session_id: str, cwd: str = ""
    ) -> bool:
        return False

    def load_summary(self, session_id: str, cwd: str = "") -> dict:
        return {}

    def read_entries(
        self, session_id: str, since: int = 0, cwd: str = ""
    ) -> list:
        return []


# ---------------------------------------------------------------------------
# ChatStore cannot be instantiated directly
# ---------------------------------------------------------------------------

class TestChatStoreAbstract:
    """Verify ChatStore enforces its abstract contract."""

    def test_cannot_instantiate_directly(self):
        with pytest.raises(TypeError, match="abstract method"):
            ChatStore()

    def test_missing_single_method_raises(self):
        """Omitting even one abstract method prevents instantiation."""

        class Incomplete(ChatStore):
            def find_session_path(self, session_id, cwd=""):
                return None

            def read_tracked_files(self, session_id, cwd=""):
                return (set(), {}, "", "")

            def read_tail_uuids(self, session_id, cwd=""):
                return ("", "")

            def write_snapshot(self, session_id, snapshot, cwd=""):
                pass

            def repair_incomplete_turn(self, session_id, cwd=""):
                return False

            def load_summary(self, session_id, cwd=""):
                return {}

            # Missing: read_entries

        with pytest.raises(TypeError, match="abstract method"):
            Incomplete()

    def test_all_abstract_methods_must_be_implemented(self):
        """Verify the set of abstract methods matches expectations."""
        abstract_methods = ChatStore.__abstractmethods__
        expected = {
            "find_session_path",
            "read_tracked_files",
            "read_tail_uuids",
            "write_snapshot",
            "repair_incomplete_turn",
            "load_summary",
            "read_entries",
        }
        assert abstract_methods == expected


# ---------------------------------------------------------------------------
# Minimal concrete implementation works
# ---------------------------------------------------------------------------

class TestStubChatStore:
    """Verify a minimal concrete implementation can be instantiated and used."""

    def test_instantiation(self):
        store = StubChatStore()
        assert store is not None

    def test_find_session_path_returns_none(self):
        store = StubChatStore()
        result = store.find_session_path("nonexistent-id")
        assert result is None

    def test_find_session_path_accepts_cwd(self):
        store = StubChatStore()
        result = store.find_session_path("session-id", cwd="/home/user/project")
        assert result is None

    def test_read_tracked_files_returns_tuple(self):
        store = StubChatStore()
        result = store.read_tracked_files("session-id")
        assert isinstance(result, tuple)
        assert len(result) == 4
        tracked_files, file_versions, last_user_uuid, last_asst_uuid = result
        assert isinstance(tracked_files, set)
        assert isinstance(file_versions, dict)
        assert isinstance(last_user_uuid, str)
        assert isinstance(last_asst_uuid, str)

    def test_read_tail_uuids_returns_tuple(self):
        store = StubChatStore()
        result = store.read_tail_uuids("session-id")
        assert isinstance(result, tuple)
        assert len(result) == 2
        user_uuid, asst_uuid = result
        assert isinstance(user_uuid, str)
        assert isinstance(asst_uuid, str)

    def test_write_snapshot_accepts_dict(self):
        store = StubChatStore()
        snapshot = {
            "type": "file-history-snapshot",
            "messageId": "uuid-123",
            "snapshot": {"trackedFileBackups": {}},
        }
        # Should not raise
        store.write_snapshot("session-id", snapshot)

    def test_repair_incomplete_turn_returns_bool(self):
        store = StubChatStore()
        result = store.repair_incomplete_turn("session-id")
        assert isinstance(result, bool)
        assert result is False

    def test_load_summary_returns_dict(self):
        store = StubChatStore()
        result = store.load_summary("session-id")
        assert isinstance(result, dict)

    def test_read_entries_returns_list(self):
        store = StubChatStore()
        result = store.read_entries("session-id")
        assert isinstance(result, list)

    def test_read_entries_accepts_since_parameter(self):
        store = StubChatStore()
        result = store.read_entries("session-id", since=5)
        assert isinstance(result, list)

    def test_read_entries_accepts_cwd_parameter(self):
        store = StubChatStore()
        result = store.read_entries("session-id", cwd="/some/path")
        assert isinstance(result, list)
