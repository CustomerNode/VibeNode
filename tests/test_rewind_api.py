"""Tests for the rewind API endpoint — specifically the Write tool_use path.

Verifies that Write tool_use entries without a file-history snapshot appear
in files_skipped (not silently dropped).
"""

import json
import uuid
import pytest
from pathlib import Path


@pytest.fixture
def rewind_app(tmp_path, monkeypatch):
    """Flask app wired to a temp sessions directory for rewind API tests."""
    import app.db as db_mod
    from app.db import reset_repository
    from app.db.sqlite_backend import SqliteRepository
    from app import create_app

    reset_repository()

    application = create_app(testing=True)
    application.session_manager.has_session.return_value = False

    repo = SqliteRepository(str(tmp_path / "test_rewind.db"))
    repo.initialize()
    db_mod._repo = repo

    # Point _sessions_dir to our temp directory
    sessions_dir = tmp_path / "sessions"
    sessions_dir.mkdir()
    monkeypatch.setattr(
        "app.routes.sessions_api._sessions_dir",
        lambda project="": sessions_dir,
    )
    # Fix active project so path resolution works
    monkeypatch.setattr(
        "app.routes.sessions_api.get_active_project",
        lambda: "test-project",
    )
    monkeypatch.setattr(
        "app.routes.sessions_api._decode_project",
        lambda p: str(tmp_path),
    )

    with application.test_client() as client:
        with application.app_context():
            yield application, client, sessions_dir

    repo.close()
    db_mod._repo = None


def _make_jsonl_with_write(sessions_dir, target_file_path):
    """Create a JSONL with a user message then an assistant Write tool_use.

    Returns the session ID and the path to the JSONL file.
    """
    sid = str(uuid.uuid4())
    jsonl_path = sessions_dir / f"{sid}.jsonl"

    lines = [
        # Line 1: user message (this is our rewind target)
        json.dumps({
            "type": "user",
            "uuid": str(uuid.uuid4()),
            "message": {"content": "Write a file"},
            "timestamp": "2026-04-01T10:00:00Z",
        }),
        # Line 2: assistant message with a Write tool_use
        json.dumps({
            "type": "assistant",
            "uuid": str(uuid.uuid4()),
            "message": {
                "content": [
                    {
                        "type": "tool_use",
                        "name": "Write",
                        "input": {
                            "file_path": target_file_path,
                            "content": "new content from Claude",
                        },
                    }
                ]
            },
            "timestamp": "2026-04-01T10:00:05Z",
        }),
    ]
    jsonl_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return sid, jsonl_path


class TestRewindWriteToolUse:
    """Verify that Write tool_use entries are reported in files_skipped."""

    def test_write_without_snapshot_appears_in_skipped(self, rewind_app, tmp_path):
        """A Write tool_use with no snapshot backup must appear in files_skipped."""
        app, client, sessions_dir = rewind_app

        # Create a target file that the Write "overwrote"
        target = tmp_path / "written_file.py"
        target.write_text("overwritten content", encoding="utf-8")

        sid, _ = _make_jsonl_with_write(sessions_dir, str(target))

        # Rewind to line 1 (the user message) — the Write on line 2
        # should be collected but not reversible without a snapshot.
        resp = client.post(
            f"/api/rewind/{sid}",
            json={"up_to_line": 1},
        )
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["ok"] is True
        assert str(target) in data["files_skipped"], (
            f"Write file should appear in files_skipped but got: {data}"
        )
        assert str(target) not in data["files_restored"], (
            f"Write file should NOT appear in files_restored: {data}"
        )

    def test_write_only_session_returns_skipped_not_error(self, rewind_app, tmp_path):
        """A session with ONLY a Write (no Edit) should still return 200, not 400."""
        app, client, sessions_dir = rewind_app

        target = tmp_path / "only_write.py"
        target.write_text("content", encoding="utf-8")

        sid, _ = _make_jsonl_with_write(sessions_dir, str(target))

        resp = client.post(
            f"/api/rewind/{sid}",
            json={"up_to_line": 1},
        )
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["ok"] is True

    def test_edit_reversed_but_write_skipped(self, rewind_app, tmp_path):
        """Mixed Edit + Write: Edit is reversed, Write lands in skipped."""
        app, client, sessions_dir = rewind_app

        # Create target files
        edit_file = tmp_path / "edited.py"
        edit_file.write_text("hello world", encoding="utf-8")
        write_file = tmp_path / "written.py"
        write_file.write_text("overwritten", encoding="utf-8")

        sid = str(uuid.uuid4())
        jsonl_path = sessions_dir / f"{sid}.jsonl"
        lines = [
            json.dumps({
                "type": "user",
                "uuid": str(uuid.uuid4()),
                "message": {"content": "Edit and write"},
                "timestamp": "2026-04-01T10:00:00Z",
            }),
            json.dumps({
                "type": "assistant",
                "uuid": str(uuid.uuid4()),
                "message": {
                    "content": [
                        {
                            "type": "tool_use",
                            "name": "Edit",
                            "input": {
                                "file_path": str(edit_file),
                                "old_string": "hello",
                                "new_string": "goodbye",
                            },
                        },
                        {
                            "type": "tool_use",
                            "name": "Write",
                            "input": {
                                "file_path": str(write_file),
                                "content": "overwritten",
                            },
                        },
                    ]
                },
                "timestamp": "2026-04-01T10:00:05Z",
            }),
        ]
        jsonl_path.write_text("\n".join(lines) + "\n", encoding="utf-8")

        # First, simulate what Claude did — apply the Edit
        edit_file.write_text("goodbye world", encoding="utf-8")

        resp = client.post(
            f"/api/rewind/{sid}",
            json={"up_to_line": 1},
        )
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["ok"] is True

        # Edit should be reversed
        assert str(edit_file) in data["files_restored"]
        assert edit_file.read_text(encoding="utf-8") == "hello world"

        # Write should be skipped (no snapshot)
        assert str(write_file) in data["files_skipped"]
