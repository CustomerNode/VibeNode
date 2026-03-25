"""
Tests for Fork, Rewind, and Fork+Rewind features.

Covers:
- GET /api/session-timeline/<id>
- POST /api/fork/<id>
- POST /api/rewind/<id>
- POST /api/fork-rewind/<id>
- load_session_timeline() parser
"""

import json
import uuid as uuid_mod
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _uuid():
    return str(uuid_mod.uuid4())


def _ts(minute=0, second=0):
    return f"2026-03-10T10:{minute:02d}:{second:02d}Z"


def _user_msg(content, ts=None, uid=None, session_id="test-sess"):
    uid = uid or _uuid()
    return json.dumps({
        "type": "user",
        "message": {"role": "user", "content": content},
        "timestamp": ts or _ts(),
        "sessionId": session_id,
        "uuid": uid,
    })


def _asst_msg(content, ts=None, uid=None, session_id="test-sess",
              tool_uses=None):
    """Build an assistant message line. If tool_uses is provided, content
    becomes a list of blocks (text + tool_use entries)."""
    uid = uid or _uuid()
    if tool_uses:
        blocks = [{"type": "text", "text": content}] if content else []
        for tu in tool_uses:
            blocks.append({"type": "tool_use", **tu})
        raw_content = blocks
    else:
        raw_content = content
    return json.dumps({
        "type": "assistant",
        "message": {"role": "assistant", "content": raw_content},
        "timestamp": ts or _ts(),
        "sessionId": session_id,
        "uuid": uid,
    })


def _snapshot(message_id, tracked_files=None):
    """Build a file-history-snapshot JSONL line."""
    backups = {}
    if tracked_files:
        for rel_path, backup_name in tracked_files.items():
            backups[rel_path] = {
                "backupFileName": backup_name,
                "version": 1,
                "backupTime": _ts(),
            }
    return json.dumps({
        "type": "file-history-snapshot",
        "messageId": message_id,
        "snapshot": {
            "messageId": message_id,
            "trackedFileBackups": backups,
            "timestamp": _ts(),
        },
        "isSnapshotUpdate": bool(tracked_files),
    })


def _title(title, session_id="test-sess"):
    return json.dumps({
        "type": "custom-title",
        "customTitle": title,
        "sessionId": session_id,
    })


def _write_session(directory, session_id, lines):
    p = directory / f"{session_id}.jsonl"
    p.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return p


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture()
def fake_project(tmp_path):
    proj = tmp_path / "projects" / "C--Users-test-Documents-myproj"
    proj.mkdir(parents=True)
    return proj


@pytest.fixture()
def file_history_dir(tmp_path):
    """Create a fake ~/.claude/file-history/<session_id>/ directory."""
    d = tmp_path / "file-history" / "sess-snap"
    d.mkdir(parents=True)
    return d


@pytest.fixture()
def app(tmp_path, fake_project):
    from app import create_app

    application = create_app()
    application.config["TESTING"] = True

    _patch_sessions = patch(
        "app.config._sessions_dir", return_value=fake_project)
    _patch_projects = patch(
        "app.config._CLAUDE_PROJECTS", fake_project.parent)
    _patch_sessions_sess = patch(
        "app.sessions._sessions_dir", return_value=fake_project)
    _patch_sessions_api = patch(
        "app.routes.sessions_api._sessions_dir", return_value=fake_project)
    _patch_live_api = patch(
        "app.routes.live_api._sessions_dir", return_value=fake_project)
    _patch_analysis = patch(
        "app.routes.analysis_api._sessions_dir", return_value=fake_project)
    _patch_names_file = patch(
        "app.config._names_file",
        return_value=fake_project / "_session_names.json")
    _patch_names_sess = patch(
        "app.sessions._load_names", return_value={})
    _patch_names_cached = patch(
        "app.sessions._load_names_cached", return_value={})

    with (_patch_sessions, _patch_projects,
          _patch_sessions_sess, _patch_sessions_api,
          _patch_live_api, _patch_analysis,
          _patch_names_file, _patch_names_sess, _patch_names_cached):
        yield application


@pytest.fixture()
def client(app):
    return app.test_client()


# A rich session with user/assistant messages, tool_use, snapshots
@pytest.fixture()
def rich_session(fake_project, tmp_path):
    uid1 = _uuid()
    uid2 = _uuid()
    uid3 = _uuid()
    uid4 = _uuid()
    uid5 = _uuid()

    # Create file-history backup
    fh_dir = tmp_path / "file-history" / "sess-rich"
    fh_dir.mkdir(parents=True)
    (fh_dir / "backup_app_py_v1").write_text("print('hello')\n",
                                              encoding="utf-8")

    # NOTE: In real Claude Code JSONL, file-history-snapshot entries appear
    # AFTER the message they reference (the message is written first, then
    # the snapshot is appended).  The fixture mirrors this real-world ordering.
    lines = [
        _user_msg("Fix the login bug", _ts(0, 0), uid1, "sess-rich"),  # line 1
        _snapshot(uid1),  # line 2: empty snapshot (baseline) — after its message
        _asst_msg("I'll look at the code", _ts(0, 5), uid2, "sess-rich",
                  tool_uses=[
                      {"name": "Read", "input": {"file_path": "/app.py"}},
                  ]),  # line 3
        _user_msg("Now add tests", _ts(1, 0), uid3, "sess-rich"),  # line 4
        _snapshot(uid3, {"app.py": "backup_app_py_v1"}),  # line 5: snapshot with backup — after its message
        _asst_msg("Here are the changes", _ts(1, 10), uid4, "sess-rich",
                  tool_uses=[
                      {"name": "Edit", "input": {
                          "file_path": "/app.py",
                          "old_string": "print('hello')",
                          "new_string": "print('hello')\nprint('world')\nprint('test')",
                      }},
                      {"name": "Write", "input": {
                          "file_path": "/tests/test_app.py",
                          "content": "import pytest\n\ndef test_hello():\n    assert True\n",
                      }},
                  ]),  # line 6
        _user_msg("Looks good, ship it", _ts(2, 0), uid5, "sess-rich"),  # line 7
        _asst_msg("Done! Everything is committed.", _ts(2, 5), session_id="sess-rich"),  # line 8
    ]

    path = _write_session(fake_project, "sess-rich", lines)
    return {
        "path": path,
        "session_id": "sess-rich",
        "uids": [uid1, uid2, uid3, uid4, uid5],
        "fh_dir": fh_dir,
    }


@pytest.fixture()
def simple_session(fake_project):
    """A simple session with no tool_use or snapshots."""
    lines = [
        _user_msg("Hello", _ts(0, 0), session_id="sess-simple"),
        _asst_msg("Hi there!", _ts(0, 5), session_id="sess-simple"),
        _user_msg("What is Python?", _ts(1, 0), session_id="sess-simple"),
        _asst_msg("Python is a programming language.", _ts(1, 5),
                  session_id="sess-simple"),
    ]
    _write_session(fake_project, "sess-simple", lines)
    return "sess-simple"


# ===================================================================
# 1. load_session_timeline UNIT TESTS
# ===================================================================

class TestLoadSessionTimeline:

    def test_parses_user_and_assistant_messages(self, fake_project,
                                                 simple_session):
        from app.sessions import load_session_timeline
        path = fake_project / f"{simple_session}.jsonl"
        result = load_session_timeline(path)

        assert len(result["messages"]) == 4
        roles = [m["role"] for m in result["messages"]]
        assert roles == ["user", "assistant", "user", "assistant"]

    def test_preview_text_truncated(self, fake_project):
        from app.sessions import load_session_timeline
        long_text = "A" * 300
        lines = [_user_msg(long_text, session_id="sess-long")]
        _write_session(fake_project, "sess-long", lines)

        result = load_session_timeline(fake_project / "sess-long.jsonl")
        assert len(result["messages"]) == 1
        assert len(result["messages"][0]["preview"]) == 140

    def test_tracks_edit_line_changes(self, fake_project, rich_session):
        from app.sessions import load_session_timeline
        result = load_session_timeline(rich_session["path"])

        # Find the assistant message with Edit/Write tool_use (line 6)
        edit_msgs = [m for m in result["messages"]
                     if m["changes"]["added"] > 0 or m["changes"]["removed"] > 0]
        assert len(edit_msgs) >= 1
        m = edit_msgs[0]
        assert m["changes"]["added"] > 0
        assert "app.py" in m["changes"]["files"]
        assert "test_app.py" in m["changes"]["files"]

    def test_detects_snapshots(self, fake_project, rich_session):
        from app.sessions import load_session_timeline
        result = load_session_timeline(rich_session["path"])
        assert result["has_snapshots"] is True

        # The message at uid3 should have has_snapshot=True
        snap_msgs = [m for m in result["messages"] if m["has_snapshot"]]
        assert len(snap_msgs) >= 1

    def test_snapshot_after_message_detected(self, fake_project):
        """Snapshots always appear AFTER the message they reference in real
        Claude Code JSONL.  Verify the post-pass correctly links them."""
        from app.sessions import load_session_timeline

        uid_a = _uuid()
        uid_b = _uuid()
        lines = [
            _user_msg("Hello", _ts(0, 0), uid_a, "sess-snap-order"),
            _asst_msg("I edited the file", _ts(0, 5), uid_b, "sess-snap-order",
                      tool_uses=[{"name": "Edit", "input": {
                          "file_path": "/foo.py",
                          "old_string": "old",
                          "new_string": "new",
                      }}]),
            # Snapshot comes AFTER the assistant message it references
            _snapshot(uid_b, {"foo.py": "backup_foo_py_v1"}),
        ]
        _write_session(fake_project, "sess-snap-order", lines)
        result = load_session_timeline(fake_project / "sess-snap-order.jsonl")

        assert result["has_snapshots"] is True

        # The assistant message (uid_b) should have has_snapshot=True
        asst_msgs = [m for m in result["messages"] if m["role"] == "assistant"]
        assert len(asst_msgs) == 1
        assert asst_msgs[0]["has_snapshot"] is True, \
            "Snapshot appearing after its message should still be linked"

    def test_no_snapshots_flag(self, fake_project, simple_session):
        from app.sessions import load_session_timeline
        path = fake_project / f"{simple_session}.jsonl"
        result = load_session_timeline(path)
        assert result["has_snapshots"] is False

    def test_skips_system_injected_messages(self, fake_project):
        from app.sessions import load_session_timeline
        lines = [
            _user_msg("This session is being continued from a previous one.",
                      session_id="sess-sys"),
            _user_msg("**What we were working on:** building auth",
                      session_id="sess-sys"),
            _user_msg("Real user message here", session_id="sess-sys"),
            _asst_msg("Got it!", session_id="sess-sys"),
        ]
        _write_session(fake_project, "sess-sys", lines)
        result = load_session_timeline(fake_project / "sess-sys.jsonl")
        # Only the real user message and the assistant response
        assert len(result["messages"]) == 2
        assert result["messages"][0]["preview"].startswith("Real user")

    def test_includes_timestamps(self, fake_project, simple_session):
        from app.sessions import load_session_timeline
        path = fake_project / f"{simple_session}.jsonl"
        result = load_session_timeline(path)
        for m in result["messages"]:
            assert m["ts"], f"Message {m['index']} has no timestamp"

    def test_includes_line_numbers(self, fake_project, simple_session):
        from app.sessions import load_session_timeline
        path = fake_project / f"{simple_session}.jsonl"
        result = load_session_timeline(path)
        line_nums = [m["line_number"] for m in result["messages"]]
        assert line_nums == sorted(line_nums), "Line numbers should be ascending"
        assert all(ln >= 1 for ln in line_nums)

    def test_custom_title_returned(self, fake_project):
        from app.sessions import load_session_timeline
        lines = [
            _title("My Cool Project", "sess-titled"),
            _user_msg("Hello", session_id="sess-titled"),
        ]
        _write_session(fake_project, "sess-titled", lines)
        result = load_session_timeline(fake_project / "sess-titled.jsonl")
        assert result["title"] == "My Cool Project"

    def test_empty_file_returns_empty(self, fake_project):
        from app.sessions import load_session_timeline
        (fake_project / "sess-empty.jsonl").write_text("", encoding="utf-8")
        result = load_session_timeline(fake_project / "sess-empty.jsonl")
        assert result["messages"] == []

    def test_corrupt_lines_skipped(self, fake_project):
        from app.sessions import load_session_timeline
        lines = [
            "NOT VALID JSON AT ALL",
            _user_msg("Valid message", session_id="sess-corrupt"),
            "{broken json",
            _asst_msg("Also valid", session_id="sess-corrupt"),
        ]
        _write_session(fake_project, "sess-corrupt", lines)
        result = load_session_timeline(fake_project / "sess-corrupt.jsonl")
        assert len(result["messages"]) == 2


# ===================================================================
# 2. GET /api/session-timeline/<id> ENDPOINT TESTS
# ===================================================================

class TestSessionTimelineEndpoint:

    def test_returns_timeline_for_existing_session(self, client,
                                                    fake_project,
                                                    simple_session):
        resp = client.get(f"/api/session-timeline/{simple_session}")
        assert resp.status_code == 200
        data = resp.get_json()
        assert "messages" in data
        assert "has_snapshots" in data
        assert len(data["messages"]) == 4

    def test_returns_404_for_nonexistent(self, client):
        resp = client.get("/api/session-timeline/nonexistent-id")
        assert resp.status_code == 404
        data = resp.get_json()
        assert "error" in data

    def test_sdk_session_returns_helpful_message(self, client, app):
        sm = app.session_manager
        sm.has_session = MagicMock(return_value=True)
        resp = client.get("/api/session-timeline/sdk-only-sess")
        assert resp.status_code == 200
        data = resp.get_json()
        assert "in-memory" in data.get("error", "")

    def test_rich_session_has_changes(self, client, rich_session):
        resp = client.get(f"/api/session-timeline/{rich_session['session_id']}")
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["has_snapshots"] is True
        # At least one message should have change counts
        has_changes = any(
            m["changes"]["added"] > 0 or m["changes"]["removed"] > 0
            for m in data["messages"]
        )
        assert has_changes


# ===================================================================
# 3. POST /api/fork/<id> ENDPOINT TESTS
# ===================================================================

class TestForkEndpoint:

    def test_fork_creates_new_session(self, client, fake_project,
                                      rich_session):
        sid = rich_session["session_id"]
        resp = client.post(f"/api/fork/{sid}", json={"up_to_line": 5})
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["ok"] is True
        assert "new_id" in data
        assert data["title"].startswith("[fork]")

        # Verify new file exists
        new_path = fake_project / f"{data['new_id']}.jsonl"
        assert new_path.exists()

    def test_forked_session_has_fewer_lines(self, client, fake_project,
                                             rich_session):
        sid = rich_session["session_id"]
        # Fork at line 5 (should include lines 1-5, exclude 6-8)
        resp = client.post(f"/api/fork/{sid}", json={"up_to_line": 5})
        data = resp.get_json()
        new_path = fake_project / f"{data['new_id']}.jsonl"

        # Read both files
        orig_lines = [l for l in rich_session["path"].read_text().splitlines()
                      if l.strip()]
        fork_lines = [l for l in new_path.read_text().splitlines()
                      if l.strip()]

        # Forked file should have at most 5 original lines + 1 title line
        assert len(fork_lines) <= 6
        assert len(fork_lines) < len(orig_lines)

    def test_forked_session_has_new_session_id(self, client, fake_project,
                                                rich_session):
        sid = rich_session["session_id"]
        resp = client.post(f"/api/fork/{sid}", json={"up_to_line": 5})
        data = resp.get_json()
        new_path = fake_project / f"{data['new_id']}.jsonl"

        # Every line with sessionId should have the new ID
        for line in new_path.read_text().splitlines():
            if not line.strip():
                continue
            obj = json.loads(line)
            if "sessionId" in obj:
                assert obj["sessionId"] == data["new_id"]

    def test_fork_nonexistent_returns_404(self, client):
        resp = client.post("/api/fork/no-such-id", json={"up_to_line": 1})
        assert resp.status_code == 404

    def test_fork_missing_line_number_returns_400(self, client, fake_project,
                                                   simple_session):
        resp = client.post(f"/api/fork/{simple_session}", json={})
        assert resp.status_code == 400

    def test_fork_at_last_line_includes_everything(self, client, fake_project,
                                                    simple_session):
        resp = client.post(f"/api/fork/{simple_session}",
                           json={"up_to_line": 9999})
        data = resp.get_json()
        assert data["ok"] is True

    def test_forked_session_is_loadable(self, client, fake_project,
                                        rich_session):
        """Forked session can be loaded by /api/session/<id>."""
        sid = rich_session["session_id"]
        resp = client.post(f"/api/fork/{sid}", json={"up_to_line": 5})
        data = resp.get_json()
        # Load the forked session
        resp2 = client.get(f"/api/session/{data['new_id']}")
        assert resp2.status_code == 200
        sess = resp2.get_json()
        assert sess["id"] == data["new_id"]
        assert len(sess["messages"]) > 0


# ===================================================================
# 4. POST /api/rewind/<id> ENDPOINT TESTS
# ===================================================================

class TestRewindEndpoint:

    def test_rewind_restores_files(self, client, fake_project,
                                    rich_session, tmp_path):
        # Set up: create the project dir and a current version of app.py
        proj_dir = tmp_path / "project"
        proj_dir.mkdir()
        (proj_dir / "app.py").write_text("print('modified')\n",
                                          encoding="utf-8")

        sid = rich_session["session_id"]
        # Patch _decode_project and home dir for file-history lookup
        with patch("app.routes.sessions_api.get_active_project",
                   return_value="test-proj"), \
             patch("app.routes.sessions_api._decode_project",
                   return_value=str(proj_dir)), \
             patch("app.routes.sessions_api.Path") as MockPath:
            # Make Path.home() return our tmp_path (for file-history lookup)
            MockPath.home.return_value = tmp_path
            # But Path(proj_dir) / norm_rel should still work
            MockPath.side_effect = Path
            MockPath.home.return_value = tmp_path

            # Actually, let's just move the file-history to the right place
            # The endpoint does: Path.home() / ".claude" / "file-history" / session_id
            fh_target = tmp_path / ".claude" / "file-history" / sid
            fh_target.mkdir(parents=True, exist_ok=True)
            # Copy backup from rich_session fixture
            src_backup = rich_session["fh_dir"] / "backup_app_py_v1"
            (fh_target / "backup_app_py_v1").write_bytes(
                src_backup.read_bytes())

            # Now just patch home() and project resolution
            with patch("app.routes.sessions_api.Path") as MP:
                # We need Path to work normally but Path.home() to return tmp_path
                MP.side_effect = Path
                MP.home.return_value = tmp_path

                with patch("app.routes.sessions_api.get_active_project",
                           return_value="test"), \
                     patch("app.routes.sessions_api._decode_project",
                           return_value=str(proj_dir)):
                    resp = client.post(f"/api/rewind/{sid}",
                                       json={"up_to_line": 7})

        data = resp.get_json()
        assert data["ok"] is True
        assert "files_restored" in data

    def test_rewind_no_snapshot_returns_error(self, client, fake_project,
                                              simple_session):
        resp = client.post(f"/api/rewind/{simple_session}",
                           json={"up_to_line": 2})
        assert resp.status_code == 400
        data = resp.get_json()
        assert "No file snapshot" in data["error"]

    def test_rewind_nonexistent_returns_404(self, client):
        resp = client.post("/api/rewind/no-such-id", json={"up_to_line": 1})
        assert resp.status_code == 404

    def test_rewind_missing_line_returns_400(self, client, fake_project,
                                              simple_session):
        resp = client.post(f"/api/rewind/{simple_session}", json={})
        assert resp.status_code == 400


# ===================================================================
# 5. POST /api/fork-rewind/<id> ENDPOINT TESTS
# ===================================================================

class TestForkRewindEndpoint:

    def test_fork_rewind_creates_session_and_reports_files(
        self, client, fake_project, rich_session, tmp_path
    ):
        sid = rich_session["session_id"]
        proj_dir = tmp_path / "project"
        proj_dir.mkdir()

        fh_target = tmp_path / ".claude" / "file-history" / sid
        fh_target.mkdir(parents=True, exist_ok=True)
        (fh_target / "backup_app_py_v1").write_text("print('hello')\n",
                                                      encoding="utf-8")

        with patch("app.routes.sessions_api.Path") as MP:
            MP.side_effect = Path
            MP.home.return_value = tmp_path
            with patch("app.routes.sessions_api.get_active_project",
                       return_value="test"), \
                 patch("app.routes.sessions_api._decode_project",
                       return_value=str(proj_dir)):
                resp = client.post(f"/api/fork-rewind/{sid}",
                                   json={"up_to_line": 7})

        data = resp.get_json()
        assert data["ok"] is True
        assert "new_id" in data
        assert data["title"].startswith("[fork+rewind]")

        # New session file should exist
        new_path = fake_project / f"{data['new_id']}.jsonl"
        assert new_path.exists()

    def test_fork_rewind_nonexistent_returns_404(self, client):
        resp = client.post("/api/fork-rewind/no-such-id",
                           json={"up_to_line": 1})
        assert resp.status_code == 404

    def test_fork_rewind_missing_line_returns_400(self, client, fake_project,
                                                   simple_session):
        resp = client.post(f"/api/fork-rewind/{simple_session}", json={})
        assert resp.status_code == 400

    def test_fork_rewind_no_snapshot_still_forks(self, client, fake_project,
                                                  simple_session):
        """Even without snapshots, fork part should succeed."""
        resp = client.post(f"/api/fork-rewind/{simple_session}",
                           json={"up_to_line": 3})
        data = resp.get_json()
        assert data["ok"] is True
        assert "new_id" in data
        assert data["files_restored"] == []


# ===================================================================
# 6. EDGE CASES & INTEGRATION
# ===================================================================

class TestEdgeCases:

    def test_fork_then_view_forked_timeline(self, client, fake_project,
                                             rich_session):
        """Fork a session, then load its timeline — full round trip."""
        sid = rich_session["session_id"]

        # Fork at line 5
        resp = client.post(f"/api/fork/{sid}", json={"up_to_line": 5})
        new_id = resp.get_json()["new_id"]

        # Load timeline of forked session
        resp2 = client.get(f"/api/session-timeline/{new_id}")
        assert resp2.status_code == 200
        data = resp2.get_json()
        assert len(data["messages"]) > 0
        # Should have fewer messages than original
        orig = client.get(f"/api/session-timeline/{sid}").get_json()
        assert len(data["messages"]) <= len(orig["messages"])

    def test_timeline_message_indices_are_sequential(self, client,
                                                      fake_project,
                                                      rich_session):
        resp = client.get(
            f"/api/session-timeline/{rich_session['session_id']}")
        data = resp.get_json()
        indices = [m["index"] for m in data["messages"]]
        assert indices == list(range(len(indices)))

    def test_multiple_forks_create_independent_sessions(self, client,
                                                         fake_project,
                                                         rich_session):
        sid = rich_session["session_id"]
        r1 = client.post(f"/api/fork/{sid}", json={"up_to_line": 2})
        r2 = client.post(f"/api/fork/{sid}", json={"up_to_line": 5})

        id1 = r1.get_json()["new_id"]
        id2 = r2.get_json()["new_id"]
        assert id1 != id2

        # Both should exist
        assert (fake_project / f"{id1}.jsonl").exists()
        assert (fake_project / f"{id2}.jsonl").exists()
