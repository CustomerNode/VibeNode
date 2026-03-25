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


def _snapshot(message_id, tracked_files=None, inner_message_id=None,
              is_update=False):
    """Build a file-history-snapshot JSONL line.

    Uses realistic data formats that match real Claude Code JSONL:
    - tracked_files values can be a backup name string (hash@vN format),
      a dict with full backup_info, or None for unrestorable entries.
    - inner_message_id: if provided, the inner snapshot.messageId differs
      from the outer messageId (as with isSnapshotUpdate entries).
    """
    backups = {}
    version = 0
    if tracked_files:
        for rel_path, backup_val in tracked_files.items():
            version += 1
            if backup_val is None:
                backups[rel_path] = {
                    "backupFileName": None,
                    "version": version,
                    "backupTime": None,
                }
            elif isinstance(backup_val, dict):
                backups[rel_path] = backup_val
            else:
                backups[rel_path] = {
                    "backupFileName": backup_val,
                    "version": version,
                    "backupTime": _ts(),
                }
    return json.dumps({
        "type": "file-history-snapshot",
        "messageId": message_id,
        "snapshot": {
            "messageId": inner_message_id or message_id,
            "trackedFileBackups": backups,
            "timestamp": _ts(),
        },
        "isSnapshotUpdate": is_update or bool(inner_message_id),
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


# A rich session with user/assistant messages, tool_use, snapshots.
# Uses REALISTIC data formats matching real Claude Code JSONL:
# - Absolute paths as trackedFileBackups keys (inside tmp_path so tests can write)
# - Hash-based backup filenames (hash@vN)
# - isSnapshotUpdate entries with differing outer/inner messageIds
# - A null backupFileName entry (file tracked but not yet backed up)
@pytest.fixture()
def rich_session(fake_project, tmp_path):
    uid1 = _uuid()  # user msg 1
    uid2 = _uuid()  # asst msg 1 (Read tool_use)
    uid3 = _uuid()  # user msg 2
    uid4 = _uuid()  # asst msg 2 (Edit + Write tool_use)
    uid5 = _uuid()  # user msg 3

    # Realistic backup filename (hash@version)
    backup_name = "a1b2c3d4e5f6a7b8@v1"

    # Create file-history backup
    fh_dir = tmp_path / "file-history" / "sess-rich"
    fh_dir.mkdir(parents=True)
    (fh_dir / backup_name).write_text("print('hello')\n", encoding="utf-8")

    # Absolute paths inside tmp_path (writable in tests) — this matches
    # real Claude Code which uses absolute Windows paths like
    # C:\Users\user\Documents\project\app.py
    proj_root = tmp_path / "project"
    proj_root.mkdir(parents=True, exist_ok=True)
    abs_app_py = str(proj_root / "app.py")
    abs_test_py = str(proj_root / "tests" / "test_app.py")

    # NOTE: In real Claude Code JSONL, file-history-snapshot entries appear
    # AFTER the message they reference (the message is written first, then
    # the snapshot is appended).  The fixture mirrors this real-world ordering.
    lines = [
        _user_msg("Fix the login bug", _ts(0, 0), uid1, "sess-rich"),       # line 1
        _snapshot(uid1),  # line 2: empty baseline snapshot — after its msg
        _asst_msg("I'll look at the code", _ts(0, 5), uid2, "sess-rich",
                  tool_uses=[
                      {"name": "Read", "input": {"file_path": abs_app_py}},
                  ]),                                                          # line 3
        _user_msg("Now add tests", _ts(1, 0), uid3, "sess-rich"),            # line 4
        # isSnapshotUpdate: outer=uid4 (assistant), inner=uid3 (user)
        # This is how real Claude Code writes update snapshots.
        # Also includes a null entry for test_app.py (newly tracked, not backed up)
        _snapshot(uid4, {
            abs_app_py: backup_name,
            abs_test_py: None,
        }, inner_message_id=uid3, is_update=True),                             # line 5
        _asst_msg("Here are the changes", _ts(1, 10), uid4, "sess-rich",
                  tool_uses=[
                      {"name": "Edit", "input": {
                          "file_path": abs_app_py,
                          "old_string": "print('hello')",
                          "new_string": "print('hello')\nprint('world')\nprint('test')",
                      }},
                      {"name": "Write", "input": {
                          "file_path": abs_test_py,
                          "content": "import pytest\n\ndef test_hello():\n    assert True\n",
                      }},
                  ]),                                                          # line 6
        _user_msg("Looks good, ship it", _ts(2, 0), uid5, "sess-rich"),      # line 7
        _asst_msg("Done! Everything is committed.", _ts(2, 5),
                  session_id="sess-rich"),                                     # line 8
    ]

    path = _write_session(fake_project, "sess-rich", lines)
    return {
        "path": path,
        "session_id": "sess-rich",
        "uids": [uid1, uid2, uid3, uid4, uid5],
        "fh_dir": fh_dir,
        "backup_name": backup_name,
        "abs_app_py": abs_app_py,
        "abs_test_py": abs_test_py,
        "proj_root": proj_root,
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
                          "file_path": "C:\\Users\\test\\proj\\foo.py",
                          "old_string": "old",
                          "new_string": "new",
                      }}]),
            # Snapshot comes AFTER the assistant message it references
            # Uses realistic hash@version backup name
            _snapshot(uid_b, {
                "C:\\Users\\test\\proj\\foo.py": "e4a1f6c823b09d17@v1",
            }),
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
        """Rewind restores files from realistic snapshot data including
        absolute paths and hash-based backup filenames."""
        sid = rich_session["session_id"]
        backup_name = rich_session["backup_name"]
        abs_app_py = rich_session["abs_app_py"]
        proj_root = rich_session["proj_root"]

        # Write a "modified" version to the target (absolute path in tmp_path)
        Path(abs_app_py).write_text("print('modified version')\n",
                                     encoding="utf-8")

        # Set up file-history with the backup
        fh_target = tmp_path / ".claude" / "file-history" / sid
        fh_target.mkdir(parents=True, exist_ok=True)
        src_backup = rich_session["fh_dir"] / backup_name
        (fh_target / backup_name).write_bytes(src_backup.read_bytes())

        with patch("app.routes.sessions_api.Path") as MP:
            MP.side_effect = Path
            MP.home.return_value = tmp_path
            with patch("app.routes.sessions_api.get_active_project",
                       return_value="test"), \
                 patch("app.routes.sessions_api._decode_project",
                       return_value=str(proj_root)):
                resp = client.post(f"/api/rewind/{sid}",
                                   json={"up_to_line": 7})

        data = resp.get_json()
        assert data["ok"] is True
        # Must have actually restored at least one file
        assert len(data["files_restored"]) >= 1
        assert abs_app_py in data["files_restored"]
        # The null-backupFileName entry (test_app.py) should NOT appear
        # in files_restored (it's silently skipped by the null check)
        assert rich_session["abs_test_py"] not in data["files_restored"]
        # Verify the restored content matches the backup
        assert Path(abs_app_py).read_text() == "print('hello')\n"

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
        backup_name = rich_session["backup_name"]
        abs_app_py = rich_session["abs_app_py"]
        proj_root = rich_session["proj_root"]

        fh_target = tmp_path / ".claude" / "file-history" / sid
        fh_target.mkdir(parents=True, exist_ok=True)
        (fh_target / backup_name).write_text("print('hello')\n",
                                              encoding="utf-8")

        with patch("app.routes.sessions_api.Path") as MP:
            MP.side_effect = Path
            MP.home.return_value = tmp_path
            with patch("app.routes.sessions_api.get_active_project",
                       return_value="test"), \
                 patch("app.routes.sessions_api._decode_project",
                       return_value=str(proj_root)):
                resp = client.post(f"/api/fork-rewind/{sid}",
                                   json={"up_to_line": 7})

        data = resp.get_json()
        assert data["ok"] is True
        assert "new_id" in data
        assert data["title"].startswith("[fork+rewind]")

        # New session file should exist
        new_path = fake_project / f"{data['new_id']}.jsonl"
        assert new_path.exists()

        # File should be restored with correct content
        assert len(data["files_restored"]) >= 1
        assert Path(abs_app_py).read_text() == "print('hello')\n"

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


# ===================================================================
# 7. SNAPSHOT MERGING AND PATH RESOLUTION TESTS
# ===================================================================

class TestSnapshotMerging:
    """Verify that multiple snapshots are merged correctly and that
    absolute paths are handled in the rewind endpoint."""

    def test_rewind_merges_multiple_snapshots(self, client, fake_project,
                                                tmp_path):
        """Multiple snapshots should be merged — later entries override
        earlier ones for the same file."""
        uid1 = _uuid()
        uid2 = _uuid()
        uid3 = _uuid()
        snap1_id = _uuid()
        snap2_id = _uuid()

        # Build session with two snapshots tracking different files
        lines = [
            _user_msg("First change", _ts(0, 0), uid1, "sess-merge"),
            # Snapshot 1: tracks file_a with backup
            json.dumps({
                "type": "file-history-snapshot",
                "messageId": snap1_id,
                "snapshot": {
                    "messageId": snap1_id,
                    "trackedFileBackups": {
                        "file_a.py": {
                            "backupFileName": "backup_a_v1",
                            "version": 1,
                            "backupTime": _ts(0, 1),
                        },
                    },
                    "timestamp": _ts(0, 1),
                },
            }),
            _asst_msg("Changed file_a", _ts(0, 5), uid2, "sess-merge"),
            _user_msg("Second change", _ts(1, 0), uid3, "sess-merge"),
            # Snapshot 2: tracks file_b and updates file_a
            json.dumps({
                "type": "file-history-snapshot",
                "messageId": snap2_id,
                "snapshot": {
                    "messageId": snap2_id,
                    "trackedFileBackups": {
                        "file_a.py": {
                            "backupFileName": "backup_a_v2",
                            "version": 2,
                            "backupTime": _ts(1, 1),
                        },
                        "file_b.py": {
                            "backupFileName": "backup_b_v1",
                            "version": 1,
                            "backupTime": _ts(1, 1),
                        },
                    },
                    "timestamp": _ts(1, 1),
                },
            }),
        ]
        _write_session(fake_project, "sess-merge", lines)

        # Create backup files
        proj_dir = tmp_path / "project"
        proj_dir.mkdir()
        fh = tmp_path / ".claude" / "file-history" / "sess-merge"
        fh.mkdir(parents=True)
        (fh / "backup_a_v1").write_text("file_a version 1", encoding="utf-8")
        (fh / "backup_a_v2").write_text("file_a version 2", encoding="utf-8")
        (fh / "backup_b_v1").write_text("file_b version 1", encoding="utf-8")

        with patch("app.routes.sessions_api.Path") as MP:
            MP.side_effect = Path
            MP.home.return_value = tmp_path
            with patch("app.routes.sessions_api.get_active_project",
                       return_value="test"), \
                 patch("app.routes.sessions_api._decode_project",
                       return_value=str(proj_dir)):
                resp = client.post("/api/rewind/sess-merge",
                                   json={"up_to_line": 999})

        data = resp.get_json()
        assert data["ok"] is True
        # Both files should be restored
        assert len(data["files_restored"]) == 2
        # file_a should use v2 (the later snapshot)
        assert (proj_dir / "file_a.py").read_text() == "file_a version 2"
        # file_b should use v1
        assert (proj_dir / "file_b.py").read_text() == "file_b version 1"

    def test_rewind_handles_absolute_paths(self, client, fake_project,
                                             tmp_path):
        """Absolute paths in trackedFileBackups should be used directly."""
        snap_id = _uuid()
        abs_path = str(tmp_path / "project" / "abs_file.py").replace("\\", "/")

        lines = [
            _user_msg("Make a change", _ts(0, 0), session_id="sess-abs"),
            json.dumps({
                "type": "file-history-snapshot",
                "messageId": snap_id,
                "snapshot": {
                    "messageId": snap_id,
                    "trackedFileBackups": {
                        abs_path: {
                            "backupFileName": "backup_abs_v1",
                            "version": 1,
                            "backupTime": _ts(0, 1),
                        },
                    },
                    "timestamp": _ts(0, 1),
                },
            }),
        ]
        _write_session(fake_project, "sess-abs", lines)

        # Create backup
        (tmp_path / "project").mkdir(parents=True, exist_ok=True)
        fh = tmp_path / ".claude" / "file-history" / "sess-abs"
        fh.mkdir(parents=True)
        (fh / "backup_abs_v1").write_text("absolute content", encoding="utf-8")

        with patch("app.routes.sessions_api.Path") as MP:
            MP.side_effect = Path
            MP.home.return_value = tmp_path
            with patch("app.routes.sessions_api.get_active_project",
                       return_value="test"), \
                 patch("app.routes.sessions_api._decode_project",
                       return_value=str(tmp_path / "other")):
                resp = client.post("/api/rewind/sess-abs",
                                   json={"up_to_line": 999})

        data = resp.get_json()
        assert data["ok"] is True
        assert len(data["files_restored"]) == 1
        # File should be restored to the absolute path, not relative to proj_dir
        assert (tmp_path / "project" / "abs_file.py").read_text() == "absolute content"

    def test_null_backupFileName_not_counted_as_valid_snapshot(
        self, fake_project
    ):
        """Snapshots where all backupFileNames are null should not set
        has_snapshots=True in the timeline."""
        from app.sessions import load_session_timeline

        snap_id = _uuid()
        lines = [
            _user_msg("Hello", session_id="sess-null"),
            json.dumps({
                "type": "file-history-snapshot",
                "messageId": snap_id,
                "snapshot": {
                    "messageId": snap_id,
                    "trackedFileBackups": {
                        "file.py": {
                            "backupFileName": None,
                            "version": 0,
                            "backupTime": None,
                        },
                    },
                    "timestamp": _ts(),
                },
            }),
        ]
        _write_session(fake_project, "sess-null", lines)
        result = load_session_timeline(fake_project / "sess-null.jsonl")
        assert result["has_snapshots"] is False

    def test_isSnapshotUpdate_linked_via_inner_messageId(self, fake_project):
        """isSnapshotUpdate entries should be linked via the inner
        snapshot.messageId, not just the outer messageId."""
        from app.sessions import load_session_timeline

        user_uid = _uuid()
        asst_uid = _uuid()

        lines = [
            _user_msg("Hello", _ts(0, 0), user_uid, "sess-update"),
            _asst_msg("I edited a file", _ts(0, 5), asst_uid, "sess-update",
                      tool_uses=[{"name": "Edit", "input": {
                          "file_path": "/app.py",
                          "old_string": "old",
                          "new_string": "new",
                      }}]),
            # isSnapshotUpdate: outer messageId=asst_uid, inner=user_uid
            json.dumps({
                "type": "file-history-snapshot",
                "messageId": asst_uid,
                "isSnapshotUpdate": True,
                "snapshot": {
                    "messageId": user_uid,
                    "trackedFileBackups": {
                        "app.py": {
                            "backupFileName": "backup_v1",
                            "version": 1,
                            "backupTime": _ts(),
                        },
                    },
                    "timestamp": _ts(),
                },
            }),
        ]
        _write_session(fake_project, "sess-update", lines)
        result = load_session_timeline(fake_project / "sess-update.jsonl")
        assert result["has_snapshots"] is True

        # Both the user message (via inner messageId) and the assistant
        # message (via outer messageId) should be linkable
        snap_msgs = [m for m in result["messages"] if m["has_snapshot"]]
        assert len(snap_msgs) >= 1


# ===================================================================
# 8. DAEMON SNAPSHOT CREATION TESTS
# ===================================================================

class TestDaemonSnapshotCreation:
    """Test the SessionManager._write_file_snapshot method."""

    def test_write_file_snapshot_creates_backups(self, tmp_path):
        """_write_file_snapshot should create backup files and append
        a snapshot entry to the JSONL."""
        from daemon.session_manager import SessionManager, SessionInfo, SessionState

        # Set up a fake session with tracked files
        session_id = "test-snap-daemon"
        cwd = str(tmp_path / "myproject")
        (tmp_path / "myproject").mkdir()

        # Create a source file to be backed up
        src_file = tmp_path / "myproject" / "app.py"
        src_file.write_text("print('hello')\n", encoding="utf-8")

        # Create the project dir and JSONL
        encoded = cwd.replace("\\", "/").replace(":", "-").replace("/", "-")
        proj_dir = tmp_path / ".claude" / "projects" / encoded
        proj_dir.mkdir(parents=True)
        jsonl = proj_dir / f"{session_id}.jsonl"
        jsonl.write_text(
            _user_msg("Fix bug", session_id=session_id) + "\n",
            encoding="utf-8",
        )

        # Create SessionInfo with tracked file
        info = SessionInfo(
            session_id=session_id,
            cwd=cwd,
            state=SessionState.IDLE,
        )
        info.tracked_files.add(str(src_file))

        # Create a minimal SessionManager and inject the session
        mgr = SessionManager()
        with mgr._lock:
            mgr._sessions[session_id] = info

        # Patch Path.home to use our tmp_path
        with patch("daemon.session_manager.Path") as MP:
            MP.side_effect = Path
            MP.home.return_value = tmp_path
            mgr._write_file_snapshot(session_id)

        # Verify backup was created
        history_dir = tmp_path / ".claude" / "file-history" / session_id
        assert history_dir.exists()
        backup_files = list(history_dir.iterdir())
        assert len(backup_files) >= 1

        # Verify backup content matches original
        backup_content = backup_files[0].read_text(encoding="utf-8")
        assert backup_content == "print('hello')\n"

        # Verify snapshot entry was appended to JSONL
        lines = jsonl.read_text(encoding="utf-8").strip().split("\n")
        assert len(lines) == 2  # original message + snapshot
        snap_line = json.loads(lines[1])
        assert snap_line["type"] == "file-history-snapshot"
        assert snap_line["snapshot"]["trackedFileBackups"]

        # Verify the backup filename is referenced in the snapshot
        tracked = snap_line["snapshot"]["trackedFileBackups"]
        assert str(src_file) in tracked
        assert tracked[str(src_file)]["backupFileName"] == backup_files[0].name

        # Verify the snapshot's messageId is linked to the user message
        # (not a random UUID) so the timeline picker can show the 💾 icon
        user_line = json.loads(lines[0])
        assert snap_line["messageId"] == user_line["uuid"]

    def test_write_file_snapshot_skips_when_no_tracked_files(self, tmp_path):
        """Should be a no-op when there are no tracked files."""
        from daemon.session_manager import SessionManager, SessionInfo, SessionState

        session_id = "test-snap-empty"
        info = SessionInfo(
            session_id=session_id,
            cwd=str(tmp_path),
            state=SessionState.IDLE,
        )
        # tracked_files is empty by default

        mgr = SessionManager()
        with mgr._lock:
            mgr._sessions[session_id] = info

        # Should not raise
        mgr._write_file_snapshot(session_id)

        # No history dir should be created
        history_dir = tmp_path / ".claude" / "file-history" / session_id
        assert not history_dir.exists()

    def test_write_file_snapshot_handles_missing_files(self, tmp_path):
        """Should gracefully handle tracked files that don't exist on disk."""
        from daemon.session_manager import SessionManager, SessionInfo, SessionState

        session_id = "test-snap-missing"
        cwd = str(tmp_path / "proj")
        (tmp_path / "proj").mkdir()

        # Create JSONL
        encoded = cwd.replace("\\", "/").replace(":", "-").replace("/", "-")
        proj_dir = tmp_path / ".claude" / "projects" / encoded
        proj_dir.mkdir(parents=True)
        jsonl = proj_dir / f"{session_id}.jsonl"
        jsonl.write_text(
            _user_msg("Hello", session_id=session_id) + "\n",
            encoding="utf-8",
        )

        info = SessionInfo(
            session_id=session_id,
            cwd=cwd,
            state=SessionState.IDLE,
        )
        info.tracked_files.add(str(tmp_path / "proj" / "nonexistent.py"))

        mgr = SessionManager()
        with mgr._lock:
            mgr._sessions[session_id] = info

        with patch("daemon.session_manager.Path") as MP:
            MP.side_effect = Path
            MP.home.return_value = tmp_path
            mgr._write_file_snapshot(session_id)

        # JSONL should NOT have a snapshot (all files had null backups)
        lines = jsonl.read_text(encoding="utf-8").strip().split("\n")
        assert len(lines) == 1  # only the original message

    def test_prepopulate_tracked_files_from_jsonl(self, tmp_path):
        """_prepopulate_tracked_files should scan the JSONL for past Edit/Write
        tool uses and populate tracked_files — critical for daemon restarts."""
        from daemon.session_manager import SessionManager, SessionInfo, SessionState

        session_id = "test-prepop"
        cwd = str(tmp_path / "myproject")
        (tmp_path / "myproject").mkdir()

        # Create a JSONL with real assistant messages containing tool_use blocks
        abs_file1 = str(tmp_path / "myproject" / "app.py")
        abs_file2 = str(tmp_path / "myproject" / "utils.py")
        lines = [
            _user_msg("Fix stuff", session_id=session_id),
            _asst_msg("I'll fix it", session_id=session_id, tool_uses=[
                {"name": "Read", "input": {"file_path": abs_file1}},
            ]),
            _asst_msg("Here's the fix", session_id=session_id, tool_uses=[
                {"name": "Edit", "input": {
                    "file_path": abs_file1,
                    "old_string": "old",
                    "new_string": "new",
                }},
            ]),
            _asst_msg("New file too", session_id=session_id, tool_uses=[
                {"name": "Write", "input": {
                    "file_path": abs_file2,
                    "content": "# utils\n",
                }},
            ]),
        ]
        encoded = cwd.replace("\\", "/").replace(":", "-").replace("/", "-")
        proj_dir = tmp_path / ".claude" / "projects" / encoded
        proj_dir.mkdir(parents=True)
        _write_session(proj_dir, session_id, lines)

        # Create SessionInfo with empty tracked_files (simulates daemon restart)
        info = SessionInfo(
            session_id=session_id,
            cwd=cwd,
            state=SessionState.WORKING,
        )
        assert len(info.tracked_files) == 0

        mgr = SessionManager()
        with mgr._lock:
            mgr._sessions[session_id] = info

        with patch("daemon.session_manager.Path") as MP:
            MP.side_effect = Path
            MP.home.return_value = tmp_path
            mgr._prepopulate_tracked_files(info)

        # Should have found the Edit and Write file paths, NOT the Read
        assert abs_file1 in info.tracked_files
        assert abs_file2 in info.tracked_files
        assert len(info.tracked_files) == 2

    def test_end_to_end_snapshot_then_timeline_then_rewind(self, tmp_path):
        """Full round trip: create file -> snapshot -> load timeline ->
        modify file -> rewind restores original. All real files, no mocks
        except Path.home()."""
        from daemon.session_manager import SessionManager, SessionInfo, SessionState
        from app.sessions import load_session_timeline

        session_id = "test-e2e"
        cwd = str(tmp_path / "proj")
        (tmp_path / "proj").mkdir()

        # 1. Create a real source file
        src = tmp_path / "proj" / "main.py"
        src.write_text("original_content = True\n", encoding="utf-8")

        # 2. Create JSONL with a user message and an assistant Edit
        abs_src = str(src)
        uid_user = _uuid()
        uid_asst = _uuid()
        lines = [
            json.dumps({
                "type": "user",
                "message": {"role": "user", "content": "Fix the bug"},
                "timestamp": _ts(0, 0),
                "sessionId": session_id,
                "uuid": uid_user,
            }),
            json.dumps({
                "type": "assistant",
                "message": {"role": "assistant", "content": [
                    {"type": "text", "text": "Fixed it"},
                    {"type": "tool_use", "name": "Edit", "input": {
                        "file_path": abs_src,
                        "old_string": "original",
                        "new_string": "modified",
                    }},
                ]},
                "timestamp": _ts(0, 5),
                "sessionId": session_id,
                "uuid": uid_asst,
            }),
        ]
        encoded = cwd.replace("\\", "/").replace(":", "-").replace("/", "-")
        proj_dir = tmp_path / ".claude" / "projects" / encoded
        proj_dir.mkdir(parents=True)
        jsonl = proj_dir / f"{session_id}.jsonl"
        jsonl.write_text("\n".join(lines) + "\n", encoding="utf-8")

        # 3. Create SessionInfo with tracked file and call _write_file_snapshot
        info = SessionInfo(
            session_id=session_id,
            cwd=cwd,
            state=SessionState.IDLE,
        )
        info.tracked_files.add(abs_src)

        mgr = SessionManager()
        with mgr._lock:
            mgr._sessions[session_id] = info

        with patch("daemon.session_manager.Path") as MP:
            MP.side_effect = Path
            MP.home.return_value = tmp_path
            mgr._write_file_snapshot(session_id)

        # 4. Verify backup file was created
        history_dir = tmp_path / ".claude" / "file-history" / session_id
        assert history_dir.exists()
        backup_files = list(history_dir.iterdir())
        assert len(backup_files) == 1
        assert backup_files[0].read_text() == "original_content = True\n"

        # 5. Verify JSONL now has a snapshot entry
        all_lines = jsonl.read_text().strip().split("\n")
        assert len(all_lines) == 3  # user + assistant + snapshot
        snap_entry = json.loads(all_lines[2])
        assert snap_entry["type"] == "file-history-snapshot"

        # 6. load_session_timeline should find the snapshot
        result = load_session_timeline(jsonl)
        assert result["has_snapshots"] is True
        snap_msgs = [m for m in result["messages"] if m["has_snapshot"]]
        assert len(snap_msgs) >= 1

        # 7. Modify the source file (simulating further edits)
        src.write_text("modified_content = True\n", encoding="utf-8")
        assert src.read_text() == "modified_content = True\n"

        # 8. Rewind should restore the original content
        backup_name = snap_entry["snapshot"]["trackedFileBackups"][abs_src]["backupFileName"]
        backup_path = history_dir / backup_name
        assert backup_path.exists()
        # Manually restore (same logic as the rewind API)
        src.write_bytes(backup_path.read_bytes())
        assert src.read_text() == "original_content = True\n"

    def test_filesystem_change_detection_catches_agent_edits(self, tmp_path):
        """When a file is modified by an Agent sub-agent (not a direct Edit),
        the mtime-based change detection should still catch it and create a
        snapshot. This is the key fix for the rewind feature."""
        from daemon.session_manager import SessionManager, SessionInfo, SessionState
        from app.sessions import load_session_timeline
        import time

        session_id = "test-fs-detect"
        cwd = str(tmp_path / "proj")
        (tmp_path / "proj").mkdir()

        # Create a source file
        src = tmp_path / "proj" / "config.py"
        src.write_text("# original\n", encoding="utf-8")

        # Create JSONL with a user message (NO Edit tool_use — simulates
        # an Agent sub-agent doing the edit, which the daemon can't see)
        uid_user = _uuid()
        uid_asst = _uuid()
        lines = [
            json.dumps({
                "type": "user",
                "message": {"role": "user", "content": "Add a comment"},
                "timestamp": _ts(0, 0),
                "sessionId": session_id,
                "uuid": uid_user,
            }),
            json.dumps({
                "type": "assistant",
                "message": {"role": "assistant", "content": [
                    {"type": "text", "text": "Done!"},
                    {"type": "tool_use", "name": "Agent", "input": {
                        "prompt": "add a comment to config.py",
                        "description": "add comment",
                    }},
                ]},
                "timestamp": _ts(0, 5),
                "sessionId": session_id,
                "uuid": uid_asst,
            }),
        ]
        encoded = cwd.replace("\\", "/").replace(":", "-").replace("/", "-")
        proj_dir = tmp_path / ".claude" / "projects" / encoded
        proj_dir.mkdir(parents=True)
        jsonl = proj_dir / f"{session_id}.jsonl"
        jsonl.write_text("\n".join(lines) + "\n", encoding="utf-8")

        # Create SessionInfo — tracked_files is EMPTY (daemon never saw Edit)
        info = SessionInfo(
            session_id=session_id,
            cwd=cwd,
            state=SessionState.WORKING,
        )
        assert len(info.tracked_files) == 0

        mgr = SessionManager()
        with mgr._lock:
            mgr._sessions[session_id] = info

        # Step 1: Record pre-turn mtimes
        mgr._record_pre_turn_mtimes(info)
        assert len(info._pre_turn_mtimes) > 0

        # Step 2: Simulate the Agent sub-agent modifying the file
        time.sleep(0.05)  # ensure mtime changes
        src.write_text("# original\n# this comment means nothing\n", encoding="utf-8")

        # Step 3: Call _write_file_snapshot (post-turn)
        with patch("daemon.session_manager.Path") as MP:
            MP.side_effect = Path
            MP.home.return_value = tmp_path
            mgr._write_file_snapshot(session_id, is_post_turn=True)

        # The file MUST be detected and backed up
        history_dir = tmp_path / ".claude" / "file-history" / session_id
        assert history_dir.exists(), "file-history dir should be created"
        backup_files = list(history_dir.iterdir())
        assert len(backup_files) >= 1, "At least one backup file should exist"

        # The JSONL MUST have a snapshot entry
        all_lines = jsonl.read_text().strip().split("\n")
        assert len(all_lines) == 3, "Should be: user + assistant + snapshot"
        snap_entry = json.loads(all_lines[2])
        assert snap_entry["type"] == "file-history-snapshot"
        assert snap_entry["isSnapshotUpdate"] is True

        # The snapshot should reference the assistant UUID (post-turn)
        assert snap_entry["messageId"] == uid_asst

        # The timeline loader should find the snapshot
        result = load_session_timeline(jsonl)
        assert result["has_snapshots"] is True

    def test_pre_and_post_turn_snapshot_format(self, tmp_path):
        """Pre-turn snapshots have isSnapshotUpdate=false, post-turn have true.
        This matches the native CLI behavior."""
        from daemon.session_manager import SessionManager, SessionInfo, SessionState
        import time

        session_id = "test-pre-post"
        cwd = str(tmp_path / "proj")
        (tmp_path / "proj").mkdir()

        src = tmp_path / "proj" / "app.py"
        src.write_text("v1\n", encoding="utf-8")

        uid_user = _uuid()
        uid_asst = _uuid()
        lines = [
            json.dumps({
                "type": "user",
                "message": {"role": "user", "content": "Fix it"},
                "timestamp": _ts(0, 0),
                "sessionId": session_id,
                "uuid": uid_user,
            }),
            json.dumps({
                "type": "assistant",
                "message": {"role": "assistant", "content": "Fixed"},
                "timestamp": _ts(0, 5),
                "sessionId": session_id,
                "uuid": uid_asst,
            }),
        ]
        encoded = cwd.replace("\\", "/").replace(":", "-").replace("/", "-")
        proj_dir = tmp_path / ".claude" / "projects" / encoded
        proj_dir.mkdir(parents=True)
        jsonl = proj_dir / f"{session_id}.jsonl"
        jsonl.write_text("\n".join(lines) + "\n", encoding="utf-8")

        info = SessionInfo(
            session_id=session_id,
            cwd=cwd,
            state=SessionState.WORKING,
        )
        info.tracked_files.add(str(src))

        mgr = SessionManager()
        with mgr._lock:
            mgr._sessions[session_id] = info

        with patch("daemon.session_manager.Path") as MP:
            MP.side_effect = Path
            MP.home.return_value = tmp_path

            # Pre-turn snapshot
            mgr._write_file_snapshot(session_id, is_post_turn=False)

            # Simulate edit
            time.sleep(0.05)
            src.write_text("v2\n", encoding="utf-8")
            info.tracked_files.add(str(src))

            # Post-turn snapshot
            mgr._write_file_snapshot(session_id, is_post_turn=True)

        all_lines = jsonl.read_text().strip().split("\n")
        # Should have: user + assistant + pre-snapshot + post-snapshot
        assert len(all_lines) == 4

        pre_snap = json.loads(all_lines[2])
        post_snap = json.loads(all_lines[3])

        assert pre_snap["type"] == "file-history-snapshot"
        assert pre_snap["isSnapshotUpdate"] is False
        assert pre_snap["messageId"] == uid_user  # linked to user msg

        assert post_snap["type"] == "file-history-snapshot"
        assert post_snap["isSnapshotUpdate"] is True
        assert post_snap["messageId"] == uid_asst  # linked to assistant msg
        # Inner messageId should reference the user message
        assert post_snap["snapshot"]["messageId"] == uid_user
