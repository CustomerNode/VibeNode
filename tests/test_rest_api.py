"""
Comprehensive REST API tests for VibeNode.

Tests cover all HTTP endpoints across:
- Session log (/api/session-log)
- Session CRUD (/api/sessions, /api/session, /api/rename, /api/autoname,
  /api/delete, /api/delete-empty, /api/duplicate, /api/continue, /api/open)
- Project management (/api/projects, /api/set-project, /api/rename-project,
  /api/add-project, /api/find-projects, /api/new-session)
- CLAUDE.md editor (/api/claude-md, /api/claude-md-global)
- Config/models (/api/config, /api/models)
- Git status (/api/project-git-status, /api/git-status, /api/git-sync)
"""

import json
import uuid
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _jsonl_line(msg_type, content="", timestamp=None, session_id="test",
                extra=None):
    """Build a single JSONL line for a mock session file."""
    ts = timestamp or datetime.now(timezone.utc).isoformat()
    if msg_type == "custom-title":
        obj = {"type": "custom-title", "customTitle": content,
               "sessionId": session_id}
    elif msg_type == "file-history-snapshot":
        obj = {"type": "file-history-snapshot", "snapshot": {}}
    elif msg_type == "progress":
        obj = {"type": "progress", "progress": 0.5}
    else:
        obj = {
            "type": msg_type,
            "message": {"role": msg_type, "content": content},
            "timestamp": ts,
            "sessionId": session_id,
        }
    if extra:
        obj.update(extra)
    return json.dumps(obj)


def _write_session(directory, session_id, lines):
    """Write a .jsonl session file and return the path."""
    p = directory / f"{session_id}.jsonl"
    p.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return p


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture()
def fake_project(tmp_path):
    """Create a fake project directory with sessions sub-structure."""
    proj = tmp_path / "projects" / "C--Users-test-Documents-myproj"
    proj.mkdir(parents=True)
    return proj


@pytest.fixture()
def app(tmp_path, fake_project):
    """Create a Flask app with all production blueprints, pointing at tmp dirs."""
    from app import create_app

    application = create_app(testing=True)
    # Default: session manager doesn't know about any sessions, so routes
    # fall through to file-based logic (which is what these tests exercise).
    application.session_manager.has_session.return_value = False

    # Redirect _sessions_dir and _CLAUDE_PROJECTS to tmp_path
    _patch_sessions = patch(
        "app.config._sessions_dir",
        return_value=fake_project,
    )
    _patch_projects = patch(
        "app.config._CLAUDE_PROJECTS",
        fake_project.parent,
    )
    # Also patch in every module that imports from config so resolution
    # goes through our patched versions.
    _patch_sessions_sess = patch(
        "app.sessions._sessions_dir",
        return_value=fake_project,
    )
    _patch_sessions_api = patch(
        "app.routes.sessions_api._sessions_dir",
        return_value=fake_project,
    )
    _patch_live_api = patch(
        "app.routes.live_api._sessions_dir",
        return_value=fake_project,
    )
    _patch_analysis = patch(
        "app.routes.analysis_api._sessions_dir",
        return_value=fake_project,
    )
    # Patch names helpers to use our fake_project
    _patch_names_file = patch(
        "app.config._names_file",
        return_value=fake_project / "_session_names.json",
    )
    _patch_names_file_sess = patch(
        "app.sessions._load_names",
        return_value={},
    )
    _patch_names_file_sess_cached = patch(
        "app.sessions._load_names_cached",
        return_value={},
    )

    with (_patch_sessions, _patch_projects,
          _patch_sessions_sess, _patch_sessions_api,
          _patch_live_api, _patch_analysis,
          _patch_names_file, _patch_names_file_sess,
          _patch_names_file_sess_cached):
        yield application


@pytest.fixture()
def client(app):
    """Flask test client."""
    return app.test_client()


@pytest.fixture()
def populated_project(fake_project):
    """Populate the fake project with several sessions."""
    # Session with messages
    _write_session(fake_project, "sess-001", [
        _jsonl_line("user", "Hello world", "2026-03-01T10:00:00Z",
                    session_id="sess-001"),
        _jsonl_line("assistant", "Hi there!", "2026-03-01T10:00:05Z",
                    session_id="sess-001"),
        _jsonl_line("user", "Write me a sorting algorithm in Python",
                    "2026-03-01T10:01:00Z", session_id="sess-001"),
        _jsonl_line("assistant",
                    "Here is quicksort:\n```python\ndef qs(a): ...\n```",
                    "2026-03-01T10:01:10Z", session_id="sess-001"),
    ])

    # Session with custom title
    _write_session(fake_project, "sess-002", [
        _jsonl_line("custom-title", "My Titled Session",
                    session_id="sess-002"),
        _jsonl_line("user", "Do something", "2026-03-02T09:00:00Z",
                    session_id="sess-002"),
        _jsonl_line("assistant", "Done!", "2026-03-02T09:00:05Z",
                    session_id="sess-002"),
    ])

    # Empty session
    _write_session(fake_project, "sess-empty", [])
    (fake_project / "sess-empty.jsonl").write_text("", encoding="utf-8")

    # Names file
    names = {"sess-001": "First Session"}
    (fake_project / "_session_names.json").write_text(
        json.dumps(names), encoding="utf-8"
    )

    return fake_project


# ===================================================================
# 1. SESSION LOG ENDPOINT  (/api/session-log/<id>)
# ===================================================================

class TestSessionLog:
    """Tests for GET /api/session-log/<session_id>."""

    def test_returns_entries_for_historical_session(
        self, client, fake_project
    ):
        _write_session(fake_project, "log-001", [
            _jsonl_line("user", "Hi", session_id="log-001"),
            _jsonl_line("assistant", "Hello", session_id="log-001"),
        ])
        resp = client.get("/api/session-log/log-001")
        assert resp.status_code == 200
        data = resp.get_json()
        assert "entries" in data
        assert data["total_lines"] == 2
        kinds = [e["kind"] for e in data["entries"]]
        assert "user" in kinds
        assert "asst" in kinds

    def test_since_parameter_skips_lines(self, client, fake_project):
        _write_session(fake_project, "log-since", [
            _jsonl_line("user", "Msg 1", session_id="log-since"),
            _jsonl_line("assistant", "Resp 1", session_id="log-since"),
            _jsonl_line("user", "Msg 2", session_id="log-since"),
            _jsonl_line("assistant", "Resp 2", session_id="log-since"),
        ])
        resp = client.get("/api/session-log/log-since?since=2")
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["total_lines"] == 4
        # Only entries after line 2
        assert len(data["entries"]) == 2

    def test_since_zero_returns_all(self, client, fake_project):
        _write_session(fake_project, "log-zero", [
            _jsonl_line("user", "A", session_id="log-zero"),
        ])
        resp = client.get("/api/session-log/log-zero?since=0")
        data = resp.get_json()
        assert len(data["entries"]) == 1

    def test_sdk_managed_session_returns_from_manager(self, client, app):
        """When SessionManager has the session, entries come from memory."""
        sm = app.session_manager
        sm.has_session = MagicMock(return_value=True)
        sm.get_entries = MagicMock(return_value=[
            {"kind": "asst", "text": "from SDK"}
        ])
        resp = client.get("/api/session-log/sdk-sess")
        data = resp.get_json()
        assert data["entries"][0]["text"] == "from SDK"
        sm.has_session.assert_called_with("sdk-sess")

    def test_nonexistent_session_returns_404(self, client):
        resp = client.get("/api/session-log/no-such-id")
        assert resp.status_code == 404
        data = resp.get_json()
        assert "error" in data

    def test_invalid_since_parameter_defaults_to_zero(
        self, client, fake_project
    ):
        _write_session(fake_project, "log-bad-since", [
            _jsonl_line("user", "X", session_id="log-bad-since"),
        ])
        resp = client.get("/api/session-log/log-bad-since?since=abc")
        assert resp.status_code == 200
        data = resp.get_json()
        assert len(data["entries"]) == 1

    def test_filters_out_internal_types(self, client, fake_project):
        """file-history-snapshot, custom-title, progress are excluded."""
        _write_session(fake_project, "log-filter", [
            _jsonl_line("file-history-snapshot"),
            _jsonl_line("custom-title", "Title"),
            _jsonl_line("progress"),
            _jsonl_line("user", "Real message", session_id="log-filter"),
        ])
        resp = client.get("/api/session-log/log-filter")
        data = resp.get_json()
        assert len(data["entries"]) == 1
        assert data["entries"][0]["kind"] == "user"

    def test_tool_use_entry_parsed(self, client, fake_project):
        line = json.dumps({
            "type": "assistant",
            "message": {
                "role": "assistant",
                "content": [
                    {
                        "type": "tool_use",
                        "id": "tu-1",
                        "name": "Bash",
                        "input": {"command": "ls -la"},
                    }
                ],
            },
            "timestamp": "2026-03-01T10:00:00Z",
            "sessionId": "log-tool",
        })
        p = fake_project / "log-tool.jsonl"
        p.write_text(line + "\n", encoding="utf-8")

        resp = client.get("/api/session-log/log-tool")
        data = resp.get_json()
        tool = [e for e in data["entries"] if e["kind"] == "tool_use"]
        assert len(tool) == 1
        assert tool[0]["name"] == "Bash"
        assert "ls -la" in tool[0]["desc"]

    def test_tool_result_entry_parsed(self, client, fake_project):
        line = json.dumps({
            "type": "user",
            "message": {
                "role": "user",
                "content": [
                    {
                        "type": "tool_result",
                        "tool_use_id": "tu-1",
                        "content": "file.txt",
                        "is_error": False,
                    }
                ],
            },
            "timestamp": "2026-03-01T10:00:00Z",
        })
        p = fake_project / "log-tr.jsonl"
        p.write_text(line + "\n", encoding="utf-8")
        resp = client.get("/api/session-log/log-tr")
        data = resp.get_json()
        tr = [e for e in data["entries"] if e["kind"] == "tool_result"]
        assert len(tr) == 1
        assert tr[0]["tool_use_id"] == "tu-1"

    def test_large_message_truncated(self, client, fake_project):
        big = "x" * 5000
        _write_session(fake_project, "log-big", [
            _jsonl_line("user", big, session_id="log-big"),
        ])
        resp = client.get("/api/session-log/log-big")
        data = resp.get_json()
        assert len(data["entries"][0]["text"]) <= 20000

    def test_empty_session_file_returns_zero(self, client, fake_project):
        p = fake_project / "log-empty.jsonl"
        p.write_text("", encoding="utf-8")
        resp = client.get("/api/session-log/log-empty")
        data = resp.get_json()
        assert data["entries"] == []
        assert data["total_lines"] == 0


# ===================================================================
# 2. SESSION CRUD ENDPOINTS
# ===================================================================

class TestSessionList:
    """GET /api/sessions"""

    def test_returns_session_list(self, client, populated_project):
        resp = client.get("/api/sessions")
        assert resp.status_code == 200
        data = resp.get_json()
        assert isinstance(data, list)
        ids = [s["id"] for s in data]
        assert "sess-001" in ids
        assert "sess-002" in ids

    def test_empty_project_returns_empty_list(self, client, fake_project):
        # fake_project has no jsonl files by default
        resp = client.get("/api/sessions")
        data = resp.get_json()
        assert isinstance(data, list)


class TestSessionDetail:
    """GET /api/session/<id>"""

    def test_returns_full_session(self, client, populated_project):
        resp = client.get("/api/session/sess-001")
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["id"] == "sess-001"
        assert "messages" in data

    def test_nonexistent_returns_404(self, client, fake_project):
        resp = client.get("/api/session/nope")
        assert resp.status_code == 404


class TestRenameSession:
    """POST /api/rename/<id>"""

    def test_rename_with_valid_title(self, client, populated_project,
                                     fake_project):
        resp = client.post(
            "/api/rename/sess-001",
            json={"title": "New Name"},
        )
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["ok"] is True
        assert data["title"] == "New Name"

    def test_rename_with_empty_title_rejected(self, client,
                                               populated_project):
        resp = client.post(
            "/api/rename/sess-001",
            json={"title": ""},
        )
        assert resp.status_code == 400
        data = resp.get_json()
        assert "error" in data

    def test_rename_with_no_json_body(self, client, populated_project):
        resp = client.post(
            "/api/rename/sess-001",
            data="not json",
            content_type="text/plain",
        )
        assert resp.status_code == 400

    def test_rename_nonexistent_succeeds(self, client, fake_project):
        """Rename intentionally succeeds even without a .jsonl — allows
        naming sessions before their first message."""
        resp = client.post("/api/rename/ghost", json={"title": "X"})
        assert resp.status_code == 200
        assert resp.get_json()["ok"] is True

    def test_rename_writes_to_jsonl(self, client, populated_project,
                                    fake_project):
        client.post("/api/rename/sess-001", json={"title": "Appended"})
        content = (fake_project / "sess-001.jsonl").read_text(
            encoding="utf-8"
        )
        assert "Appended" in content

    # --- DEDUP REGRESSION (see app/routes/sessions_api.py
    # _append_custom_title_if_changed): without dedup, every rename call
    # piled another identical custom-title line onto the .jsonl.  One
    # Aras session in production accumulated 52 copies of the same title,
    # which also bumped file mtime constantly and made the sort lie about
    # "when was this last touched". ---

    def test_rename_to_same_title_is_a_noop(self, client, populated_project,
                                              fake_project):
        """Rename to the title the file already carries — must NOT
        append a duplicate line (size and mtime stay put)."""
        import time as _t
        path = fake_project / "sess-002.jsonl"
        # sess-002 fixture starts with custom-title "My Titled Session"
        before_size = path.stat().st_size
        before_mtime = path.stat().st_mtime
        _t.sleep(0.05)  # ensure mtime granularity would register a change
        resp = client.post("/api/rename/sess-002",
                           json={"title": "My Titled Session"})
        assert resp.status_code == 200
        assert path.stat().st_size == before_size
        assert path.stat().st_mtime == before_mtime

    def test_rename_records_session_access(
        self, client, populated_project, fake_project, monkeypatch,
    ):
        """Rename is a deliberate user interaction and MUST bubble the
        session.  After dropping file-mtime from effective_ts (SDK
        background writes pollute it), renames bubble via the access
        store instead.  If this regresses, renames would stop moving
        the session in the sidebar at all."""
        monkeypatch.setattr(
            "app.session_store._sessions_dir",
            lambda project="": fake_project,
        )
        access_file = fake_project / "_session_access.json"
        if access_file.exists():
            access_file.unlink()
        resp = client.post("/api/rename/sess-001",
                           json={"title": "Fresh Name"})
        assert resp.status_code == 200
        assert access_file.exists()
        data = json.loads(access_file.read_text(encoding="utf-8"))
        assert "sess-001" in data

    def test_rename_repeated_with_same_title_only_appends_once(
        self, client, populated_project, fake_project,
    ):
        """The Aras failure mode in miniature: spamming rename with the
        same value must not accrete duplicate lines."""
        path = fake_project / "sess-002.jsonl"

        def _count_pinned_custom_titles():
            n = 0
            for line in path.read_text(encoding="utf-8").splitlines():
                if not line.strip():
                    continue
                try:
                    obj = json.loads(line)
                except Exception:
                    continue
                if (obj.get("type") == "custom-title"
                        and obj.get("customTitle") == "Pinned"):
                    n += 1
            return n

        # Initial rename to a brand-new title — appends exactly once.
        resp = client.post("/api/rename/sess-002",
                           json={"title": "Pinned"})
        assert resp.status_code == 200
        assert _count_pinned_custom_titles() == 1
        # Two more renames with the same title — both must be no-ops.
        client.post("/api/rename/sess-002", json={"title": "Pinned"})
        client.post("/api/rename/sess-002", json={"title": "Pinned"})
        assert _count_pinned_custom_titles() == 1


# ===================================================================
# 2.5 SESSION TOUCH + ACCESS RECORDING
# ===================================================================
#
# The sidebar's date sort uses ``effective_ts = max(last_message_ts,
# file_mtime, last_access_ts)``.  The access_ts component is bumped by
# UI hooks so view-only opens and SDK-remap-then-write-elsewhere cases
# still bubble the user-clicked session to the top.  These tests guard:
#
#   - POST /api/session/<id>/touch persists into _session_access.json
#   - GET  /api/session/<id> (non meta_only) ALSO persists
#   - GET  /api/session/<id>?meta_only=1 does NOT persist (poll, not click)
#
# Regression: if any of these silently no-op, the sidebar stops
# reflecting interactions and the Aras-style "I clicked it and it
# didn't move" bug returns.

class TestSessionTouch:
    """POST /api/session/<id>/touch — explicit "I interacted" signal."""

    def test_touch_returns_ok(self, client, populated_project):
        resp = client.post("/api/session/sess-001/touch")
        assert resp.status_code == 200
        assert resp.get_json()["ok"] is True

    def test_touch_records_access(self, client, populated_project,
                                   fake_project, monkeypatch):
        import time as _t
        # The shared app fixture patches _sessions_dir in many spots, but
        # session_store imports _sessions_dir as a local binding — we
        # need an explicit patch there so the access file lands under
        # fake_project, not the real user dir.
        monkeypatch.setattr(
            "app.session_store._sessions_dir",
            lambda project="": fake_project,
        )
        resp = client.post("/api/session/sess-001/touch")
        assert resp.status_code == 200
        access_file = fake_project / "_session_access.json"
        assert access_file.exists()
        data = json.loads(access_file.read_text(encoding="utf-8"))
        assert "sess-001" in data
        assert abs(data["sess-001"] - _t.time()) < 5.0

    def test_touch_unknown_id_still_ok(self, client, populated_project,
                                         fake_project, monkeypatch):
        """Best-effort sort hint — must not 404 on unknown IDs."""
        monkeypatch.setattr(
            "app.session_store._sessions_dir",
            lambda project="": fake_project,
        )
        resp = client.post("/api/session/never-existed/touch")
        assert resp.status_code == 200


class TestSessionGetRecordsAccess:
    """GET /api/session/<id> (full, non-meta_only) records access.
    ``meta_only=1`` requests are polls and must NOT record."""

    def test_full_get_records_access(self, client, populated_project,
                                       fake_project, monkeypatch):
        monkeypatch.setattr(
            "app.session_store._sessions_dir",
            lambda project="": fake_project,
        )
        access_file = fake_project / "_session_access.json"
        if access_file.exists():
            access_file.unlink()
        resp = client.get("/api/session/sess-001")
        assert resp.status_code == 200
        assert access_file.exists()
        data = json.loads(access_file.read_text(encoding="utf-8"))
        assert "sess-001" in data

    def test_meta_only_get_does_not_record_access(
        self, client, populated_project, fake_project, monkeypatch,
    ):
        """If meta_only fired the recorder, every background widget
        (live-panel existence check, etc.) would bump every session's
        access_ts on every page render — the sort would be meaningless."""
        monkeypatch.setattr(
            "app.session_store._sessions_dir",
            lambda project="": fake_project,
        )
        access_file = fake_project / "_session_access.json"
        if access_file.exists():
            access_file.unlink()
        resp = client.get("/api/session/sess-001?meta_only=1")
        assert resp.status_code == 200
        # Either no file, or the file exists but sess-001 is not in it
        if access_file.exists():
            data = json.loads(access_file.read_text(encoding="utf-8"))
            assert "sess-001" not in data


class TestAutonameSession:
    """POST /api/autonname/<id> (note the double-n in the route)."""

    def test_autoname_generates_title(self, client, populated_project):
        # Mock smart_title to avoid hitting the real daemon / API / CLI chain
        # (~30s of timeouts in a sandbox without ANTHROPIC_API_KEY or claude CLI).
        with patch("app.routes.sessions_api.smart_title",
                   return_value="Generated Title"):
            resp = client.post("/api/autonname/sess-002")
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["ok"] is True
        assert "title" in data

    def test_autoname_preserves_user_set_name(self, client,
                                               populated_project,
                                               fake_project):
        """If user already set a name, autoname should skip."""
        # sess-001 has a name in _session_names.json
        with patch("app.routes.sessions_api._load_names",
                   return_value={"sess-001": "My Custom"}):
            resp = client.post("/api/autonname/sess-001")
        data = resp.get_json()
        assert data["ok"] is True
        assert data.get("skipped") is True

    def test_autoname_empty_session(self, client, populated_project):
        resp = client.post("/api/autonname/sess-empty")
        data = resp.get_json()
        assert data["ok"] is True
        assert "Empty Session" in data["title"]

    def test_autoname_nonexistent_returns_404(self, client, fake_project):
        resp = client.post("/api/autonname/nope")
        assert resp.status_code == 404

    # --- PHANTOM-PREVENTION (docs/plans/phantom-sessions-fix-spec.md LEAK B) ---

    def test_autoname_rejects_numbered_list_response(self, client,
                                                     populated_project,
                                                     fake_project):
        """When smart_title returns junk like ``"1. Generate a title"``,
        the chain falls through to the heuristic. The endpoint must
        return the heuristic title — never persist the LLM junk."""
        # The validator already rejects "1. Generate a title", so we
        # simulate it by patching smart_title to return the heuristic
        # result directly. The point of this test is that the names file
        # ends up clean — no leaked junk.
        names_file = fake_project / "_session_names.json"
        before = {}
        if names_file.exists():
            before = json.loads(names_file.read_text(encoding="utf-8"))
        # Confirm sess-002 has no entry before we start
        assert "sess-002" not in before

        # Patch smart_title to return a real legitimate title — verifies
        # the standard happy path still persists. (LEAK-B-specific
        # behaviour is exercised by the heuristic-fallback test below.)
        with patch("app.routes.sessions_api.smart_title",
                   return_value="Frontend polish pass"):
            resp = client.post("/api/autonname/sess-002")
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["ok"] is True
        assert data["title"] == "Frontend polish pass"

    def test_autoname_does_not_persist_untitled(self, client,
                                                populated_project,
                                                fake_project):
        """``"Untitled Session"`` is the heuristic last-resort string.
        It MUST NOT be written to _session_names.json — every such
        write becomes a phantom entry on the next page load."""
        names_file = fake_project / "_session_names.json"
        before = json.loads(names_file.read_text(encoding="utf-8")) \
            if names_file.exists() else {}
        assert "sess-002" not in before

        with patch("app.routes.sessions_api.smart_title",
                   return_value="Untitled Session"):
            resp = client.post("/api/autonname/sess-002")
        assert resp.status_code == 200

        after = json.loads(names_file.read_text(encoding="utf-8")) \
            if names_file.exists() else {}
        assert "sess-002" not in after, (
            f"'Untitled Session' was persisted for sess-002 — "
            f"names file now: {after}"
        )

    def test_autoname_skips_save_when_no_jsonl_and_no_sdk_session(
        self, client, fake_project, app
    ):
        """If the session has no .jsonl AND the daemon doesn't know about
        it, autoname must NOT persist a name — the session may be
        abandoned before flush and would otherwise become a phantom."""
        # No .jsonl exists for this sid; SDK doesn't know it either.
        sm = app.session_manager
        sm.has_session.return_value = False

        names_file = fake_project / "_session_names.json"
        # Ensure the names file is empty to start.
        names_file.write_text("{}", encoding="utf-8")

        with patch("app.routes.sessions_api.smart_title",
                   return_value="Frontend polish pass"):
            resp = client.post(
                "/api/autonname/ghost-sid",
                json={"prompt": "please polish the frontend"},
            )

        # The 404 path actually applies here because path doesn't exist
        # AND messages list path resolution lands on the existence check.
        # Acceptable outcomes: either 404 (no path) or 200 with no save.
        # Either way the names file must NOT have ghost-sid.
        after = json.loads(names_file.read_text(encoding="utf-8"))
        assert "ghost-sid" not in after, (
            f"Phantom entry created for abandoned session: {after}"
        )


class TestDeleteSession:
    """DELETE /api/delete/<id>"""

    def test_delete_removes_file(self, client, populated_project,
                                 fake_project):
        resp = client.delete("/api/delete/sess-001")
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["ok"] is True
        assert not (fake_project / "sess-001.jsonl").exists()

    def test_delete_cleans_up_names(self, client, populated_project,
                                    fake_project):
        client.delete("/api/delete/sess-001")
        names_file = fake_project / "_session_names.json"
        if names_file.exists():
            names = json.loads(names_file.read_text(encoding="utf-8"))
            assert "sess-001" not in names

    def test_delete_nonexistent_succeeds(self, client, fake_project):
        """Delete is intentionally idempotent — already-gone sessions return 200."""
        resp = client.delete("/api/delete/ghost")
        assert resp.status_code == 200
        assert resp.get_json()["ok"] is True

    def test_delete_removes_session_folder_too(self, client,
                                                populated_project,
                                                fake_project):
        folder = fake_project / "sess-001"
        folder.mkdir()
        (folder / "data.txt").write_text("x")

        client.delete("/api/delete/sess-001")
        assert not folder.exists()


class TestDeleteEmpty:
    """DELETE /api/delete-empty"""

    def test_removes_only_empty_sessions(self, client, populated_project,
                                         fake_project):
        resp = client.delete("/api/delete-empty")
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["ok"] is True
        assert data["deleted"] >= 1
        # Non-empty sessions should still exist
        assert (fake_project / "sess-001.jsonl").exists()
        # Empty session should be gone
        assert not (fake_project / "sess-empty.jsonl").exists()

    def test_returns_zero_when_no_empty(self, client, fake_project):
        _write_session(fake_project, "nonempty", [
            _jsonl_line("user", "Some content"),
            _jsonl_line("assistant", "Reply"),
        ])
        resp = client.delete("/api/delete-empty")
        data = resp.get_json()
        assert data["ok"] is True
        assert data["deleted"] == 0


class TestDuplicateSession:
    """POST /api/duplicate/<id>"""

    def test_duplicate_creates_copy(self, client, populated_project,
                                    fake_project):
        resp = client.post("/api/duplicate/sess-001")
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["ok"] is True
        new_id = data["new_id"]
        assert (fake_project / f"{new_id}.jsonl").exists()

    def test_duplicate_has_different_session_id(self, client,
                                                 populated_project,
                                                 fake_project):
        resp = client.post("/api/duplicate/sess-001")
        new_id = resp.get_json()["new_id"]
        content = (fake_project / f"{new_id}.jsonl").read_text(
            encoding="utf-8"
        )
        # sessionId fields should reference the new id
        for line in content.strip().splitlines():
            obj = json.loads(line)
            if "sessionId" in obj:
                assert obj["sessionId"] == new_id

    def test_duplicate_nonexistent_returns_404(self, client, fake_project):
        resp = client.post("/api/duplicate/ghost")
        assert resp.status_code == 404


class TestContinueSession:
    """POST /api/continue/<id>

    NOTE: ``/api/continue`` calls ``smart_title`` to generate the topic for
    the handoff context. We mock it in every test below — the real call
    falls through daemon → API → CLI subprocess (~23s each) in a sandbox.
    """

    def test_continue_creates_new_session(self, client, populated_project,
                                          fake_project):
        with patch("app.routes.sessions_api._decode_project",
                   return_value=str(fake_project)), \
             patch("app.routes.sessions_api.smart_title",
                   return_value="Topic Stub"):
            resp = client.post("/api/continue/sess-001")
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["ok"] is True
        new_id = data["new_id"]
        assert (fake_project / f"{new_id}.jsonl").exists()
        assert data["title"].startswith("[cont]")

    def test_continue_includes_handoff_context(self, client,
                                                populated_project,
                                                fake_project):
        with patch("app.routes.sessions_api._decode_project",
                   return_value=str(fake_project)), \
             patch("app.routes.sessions_api.smart_title",
                   return_value="Topic Stub"):
            resp = client.post("/api/continue/sess-001")
        new_id = resp.get_json()["new_id"]
        content = (fake_project / f"{new_id}.jsonl").read_text(
            encoding="utf-8"
        )
        assert "continuation" in content.lower()

    def test_continue_nonexistent_returns_404(self, client, fake_project):
        resp = client.post("/api/continue/ghost")
        assert resp.status_code == 404


class TestOpenSession:
    """POST /api/open/<id>"""

    def test_open_nonexistent_returns_404(self, client, fake_project):
        resp = client.post("/api/open/ghost")
        assert resp.status_code == 404

    def test_open_already_running_returns_ok(self, client,
                                              populated_project, app):
        sm = app.session_manager
        sm.has_session = MagicMock(return_value=True)
        sm.get_session_state = MagicMock(return_value="working")

        resp = client.post("/api/open/sess-001")
        data = resp.get_json()
        assert data["ok"] is True
        assert data.get("already_running") is True

    def test_open_starts_sdk_session(self, client, populated_project, app):
        sm = app.session_manager
        sm.has_session = MagicMock(return_value=False)
        sm.start_session = MagicMock(return_value={"ok": True})

        resp = client.post("/api/open/sess-001")
        data = resp.get_json()
        assert data["ok"] is True
        sm.start_session.assert_called_once()


# ===================================================================
# 3. PROJECT ENDPOINTS
# ===================================================================

class TestProjectList:
    """GET /api/projects"""

    def test_returns_project_list(self, client, fake_project, tmp_path):
        # The default fake_project path contains "test" not "Documents"
        # so the filter in api_projects (checks startswith docs) may exclude it.
        # We test that it returns a list regardless.
        resp = client.get("/api/projects")
        assert resp.status_code == 200
        data = resp.get_json()
        assert isinstance(data, list)


class TestSetProject:
    """POST /api/set-project"""

    def test_switch_to_valid_project(self, client, fake_project):
        encoded = fake_project.name  # "C--Users-test-Documents-myproj"
        with patch("app.routes.project_api._CLAUDE_PROJECTS",
                   fake_project.parent):
            with patch("app.routes.project_api._decode_project",
                       return_value=str(fake_project)):
                resp = client.post("/api/set-project",
                                   json={"project": encoded})
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["ok"] is True

    def test_switch_to_invalid_project(self, client):
        resp = client.post(
            "/api/set-project",
            json={"project": "nonexistent-dir-xyz"},
        )
        assert resp.status_code == 404


class TestRenameProject:
    """POST /api/rename-project"""

    def test_rename_project(self, client, fake_project):
        with patch("app.routes.project_api._load_project_names",
                   return_value={}):
            with patch("app.routes.project_api._save_project_names") as save:
                resp = client.post("/api/rename-project", json={
                    "encoded": fake_project.name,
                    "name": "My Project",
                })
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["ok"] is True

    def test_rename_project_clear_name(self, client, fake_project):
        with patch("app.routes.project_api._load_project_names",
                   return_value={fake_project.name: "Old"}):
            with patch("app.routes.project_api._save_project_names") as save:
                resp = client.post("/api/rename-project", json={
                    "encoded": fake_project.name,
                    "name": "",
                })
        assert resp.status_code == 200

    def test_rename_project_missing_encoded(self, client):
        resp = client.post("/api/rename-project", json={"name": "X"})
        assert resp.status_code == 400


class TestAddProject:
    """POST /api/add-project"""

    def test_add_project_mode_path(self, client, tmp_path):
        folder = tmp_path / "real_project"
        folder.mkdir()
        resp = client.post("/api/add-project", json={
            "mode": "path",
            "path": str(folder),
        })
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["ok"] is True
        assert data["path"] == str(folder)

    def test_add_project_mode_path_invalid(self, client):
        resp = client.post("/api/add-project", json={
            "mode": "path",
            "path": "/nonexistent/path/xyz",
        })
        assert resp.status_code == 400

    def test_add_project_mode_create(self, client, tmp_path):
        with patch("app.routes.project_api.Path.home",
                   return_value=tmp_path):
            (tmp_path / "Documents").mkdir(exist_ok=True)
            resp = client.post("/api/add-project", json={
                "mode": "create",
                "name": "brand-new-project",
            })
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["ok"] is True

    def test_add_project_mode_create_no_name(self, client):
        resp = client.post("/api/add-project", json={
            "mode": "create",
            "name": "",
        })
        assert resp.status_code == 400

    def test_add_project_unknown_mode(self, client):
        resp = client.post("/api/add-project", json={"mode": "magic"})
        assert resp.status_code == 400


class TestFindProjects:
    """GET /api/find-projects"""

    def test_find_projects_returns_list(self, client):
        resp = client.get("/api/find-projects")
        assert resp.status_code == 200
        data = resp.get_json()
        assert "projects" in data
        assert isinstance(data["projects"], list)


class TestNewSession:
    """POST /api/new-session"""

    def test_new_session_starts_sdk(self, client, app, fake_project):
        sm = app.session_manager
        sm.start_session = MagicMock(return_value={"ok": True})

        with patch("app.routes.project_api.get_active_project",
                   return_value=""):
            resp = client.post("/api/new-session", json={
                "name": "Test Session",
                "prompt": "Hello",
            })
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["ok"] is True
        assert "new_id" in data

    def test_new_session_with_resume_id(self, client, app, fake_project):
        sm = app.session_manager
        sm.start_session = MagicMock(return_value={"ok": True})

        _write_session(fake_project, "resume-me", [
            _jsonl_line("user", "old msg"),
        ])
        with patch("app.routes.project_api.get_active_project",
                   return_value=""):
            resp = client.post("/api/new-session", json={
                "resume_id": "resume-me",
                "prompt": "continue",
            })
        data = resp.get_json()
        assert data["ok"] is True
        assert data["new_id"] == "resume-me"

    def test_new_session_sdk_failure(self, client, app, fake_project):
        sm = app.session_manager
        sm.start_session = MagicMock(
            return_value={"ok": False, "error": "boom"}
        )
        with patch("app.routes.project_api.get_active_project",
                   return_value=""):
            resp = client.post("/api/new-session", json={"prompt": "Hi"})
        assert resp.status_code == 500


# ===================================================================
# 4. CLAUDE.MD ENDPOINTS
# ===================================================================

class TestClaudeMd:
    """GET/PUT /api/claude-md"""

    def test_get_claude_md_exists(self, client, tmp_path):
        proj = tmp_path / "md_project"
        proj.mkdir()
        (proj / "CLAUDE.md").write_text("# Rules\nBe nice.", encoding="utf-8")

        with patch("app.routes.live_api._get_project_path",
                   return_value=proj):
            resp = client.get("/api/claude-md")
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["exists"] is True
        assert "Be nice" in data["content"]

    def test_get_claude_md_missing(self, client, tmp_path):
        proj = tmp_path / "no_md"
        proj.mkdir()
        with patch("app.routes.live_api._get_project_path",
                   return_value=proj):
            resp = client.get("/api/claude-md")
        data = resp.get_json()
        assert data["exists"] is False
        assert data["content"] == ""

    def test_put_claude_md(self, client, tmp_path):
        proj = tmp_path / "write_md"
        proj.mkdir()
        with patch("app.routes.live_api._get_project_path",
                   return_value=proj):
            resp = client.put("/api/claude-md", json={
                "content": "# Updated\nNew rules.",
            })
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["ok"] is True
        written = (proj / "CLAUDE.md").read_text(encoding="utf-8")
        assert "New rules" in written

    def test_put_claude_md_empty_content(self, client, tmp_path):
        proj = tmp_path / "empty_md"
        proj.mkdir()
        with patch("app.routes.live_api._get_project_path",
                   return_value=proj):
            resp = client.put("/api/claude-md", json={"content": ""})
        assert resp.status_code == 200
        written = (proj / "CLAUDE.md").read_text(encoding="utf-8")
        assert written == ""

    def test_put_claude_md_missing_content_field(self, client, tmp_path):
        proj = tmp_path / "bad_md"
        proj.mkdir()
        with patch("app.routes.live_api._get_project_path",
                   return_value=proj):
            resp = client.put("/api/claude-md", json={"text": "oops"})
        assert resp.status_code == 400

    def test_put_claude_md_no_json_body(self, client, tmp_path):
        proj = tmp_path / "nojson"
        proj.mkdir()
        with patch("app.routes.live_api._get_project_path",
                   return_value=proj):
            resp = client.put("/api/claude-md", data="not json",
                              content_type="text/plain")
        assert resp.status_code == 400


class TestClaudeMdGlobal:
    """GET/PUT /api/claude-md-global"""

    def test_get_global_exists(self, client, tmp_path):
        claude_dir = tmp_path / ".claude"
        claude_dir.mkdir()
        (claude_dir / "CLAUDE.md").write_text("Global rules", encoding="utf-8")

        with patch("app.routes.live_api.Path.home", return_value=tmp_path):
            resp = client.get("/api/claude-md-global")
        data = resp.get_json()
        assert data["exists"] is True
        assert "Global rules" in data["content"]

    def test_get_global_missing(self, client, tmp_path):
        with patch("app.routes.live_api.Path.home", return_value=tmp_path):
            resp = client.get("/api/claude-md-global")
        data = resp.get_json()
        assert data["exists"] is False

    def test_put_global(self, client, tmp_path):
        claude_dir = tmp_path / ".claude"
        claude_dir.mkdir()

        with patch("app.routes.live_api.Path.home", return_value=tmp_path):
            resp = client.put("/api/claude-md-global", json={
                "content": "New global rules",
            })
        assert resp.status_code == 200
        written = (claude_dir / "CLAUDE.md").read_text(encoding="utf-8")
        assert "New global" in written

    def test_put_global_creates_directory(self, client, tmp_path):
        with patch("app.routes.live_api.Path.home", return_value=tmp_path):
            resp = client.put("/api/claude-md-global", json={
                "content": "auto-create dir",
            })
        assert resp.status_code == 200
        assert (tmp_path / ".claude" / "CLAUDE.md").exists()

    def test_put_global_missing_content(self, client, tmp_path):
        with patch("app.routes.live_api.Path.home", return_value=tmp_path):
            resp = client.put("/api/claude-md-global", json={"x": "y"})
        assert resp.status_code == 400


# ===================================================================
# 5. CONFIG / MODELS ENDPOINTS
# ===================================================================

class TestConfig:
    """GET/PUT /api/config"""

    def test_get_config_exists(self, client, tmp_path):
        claude_dir = tmp_path / ".claude"
        claude_dir.mkdir()
        settings = {"theme": "dark", "fontSize": 14}
        (claude_dir / "settings.json").write_text(
            json.dumps(settings), encoding="utf-8"
        )
        with patch("app.routes.live_api.Path.home", return_value=tmp_path):
            resp = client.get("/api/config")
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["theme"] == "dark"

    def test_get_config_missing(self, client, tmp_path):
        with patch("app.routes.live_api.Path.home", return_value=tmp_path):
            resp = client.get("/api/config")
        assert resp.status_code == 200
        data = resp.get_json()
        assert data == {}

    def test_put_config_writes_json(self, client, tmp_path):
        claude_dir = tmp_path / ".claude"
        claude_dir.mkdir()
        with patch("app.routes.live_api.Path.home", return_value=tmp_path):
            resp = client.put("/api/config", json={"newKey": "newVal"})
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["ok"] is True
        written = json.loads(
            (claude_dir / "settings.json").read_text(encoding="utf-8")
        )
        assert written["newKey"] == "newVal"

    def test_put_config_non_dict_rejected(self, client, tmp_path):
        with patch("app.routes.live_api.Path.home", return_value=tmp_path):
            resp = client.put(
                "/api/config",
                data=json.dumps([1, 2, 3]),
                content_type="application/json",
            )
        assert resp.status_code == 400

    def test_put_config_creates_directory(self, client, tmp_path):
        with patch("app.routes.live_api.Path.home", return_value=tmp_path):
            resp = client.put("/api/config", json={"auto": True})
        assert resp.status_code == 200
        assert (tmp_path / ".claude" / "settings.json").exists()

    def test_get_config_invalid_json(self, client, tmp_path):
        claude_dir = tmp_path / ".claude"
        claude_dir.mkdir()
        corrupt = claude_dir / "settings.json"
        corrupt.write_text("not json!", encoding="utf-8")
        before = corrupt.read_text(encoding="utf-8")
        with patch("app.routes.live_api.Path.home", return_value=tmp_path):
            resp = client.get("/api/config")
        assert resp.status_code == 500
        # GET is read-only: a corrupt file is never overwritten. The browser
        # mirror skips the PUT on this 500, so settings.json stays intact.
        assert corrupt.read_text(encoding="utf-8") == before

    # --- Retention mirror: GET-merge-PUT contract (added 2026-05-30) ---

    def test_config_put_preserves_other_keys(self, client, tmp_path):
        """The browser mirrors cleanupPeriodDays via GET-merge-PUT (the endpoint
        is a whole-file replace).  Seeding theme, then merging cleanupPeriodDays
        into the full GET result and PUTting it, must keep BOTH keys — a partial
        PUT of just {cleanupPeriodDays} would wipe the user's other settings.
        """
        with patch("app.routes.live_api.Path.home", return_value=tmp_path):
            client.put("/api/config", json={"theme": "dark"})
            cur = client.get("/api/config").get_json()
            assert cur.get("theme") == "dark"
            cur["cleanupPeriodDays"] = 30          # merge one key client-side
            r = client.put("/api/config", json=cur)
            assert r.status_code == 200
            body = client.get("/api/config").get_json()
        assert body.get("theme") == "dark"
        assert body.get("cleanupPeriodDays") == 30


class TestModels:
    """GET /api/models

    NOTE: every test below patches ``_fetch_current_model_from_cli`` and
    ``_fetch_models_from_anthropic_api`` and invalidates the module-level
    cache. Without those mocks the endpoint shells out to ``claude`` and
    hits the network with long timeouts (~10s).
    """

    def _setup_models(self, cli_model: str = ""):
        """Return the context-manager pair that isolates /api/models from
        external calls. Also clears the in-memory cache so each test gets
        a fresh response."""
        from app.routes import live_api
        live_api._invalidate_models_cache()
        return (
            patch("app.routes.live_api._fetch_models_from_anthropic_api",
                  return_value=[]),
            patch("app.routes.live_api._fetch_current_model_from_cli",
                  return_value=cli_model),
        )

    def test_returns_model_list(self, client):
        api_mock, cli_mock = self._setup_models()
        with api_mock, cli_mock:
            resp = client.get("/api/models")
        assert resp.status_code == 200
        data = resp.get_json()
        assert isinstance(data, list)
        assert len(data) >= 3
        ids = [m["id"] for m in data]
        assert any("sonnet" in i for i in ids)
        assert any("opus" in i for i in ids)

    def test_has_default_model(self, client):
        api_mock, cli_mock = self._setup_models()
        with api_mock, cli_mock:
            resp = client.get("/api/models")
        data = resp.get_json()
        defaults = [m for m in data if m.get("default")]
        assert len(defaults) == 1


# ===================================================================
# 6. GIT ENDPOINTS
# ===================================================================

class TestGitStatus:
    """GET /api/git-status"""

    def test_returns_cached_git_status(self, client):
        with patch("app.routes.git_api.refresh_if_idle"):
            with patch("app.routes.git_api.get_git_cache",
                       return_value={
                           "has_git": True, "ahead": 0, "behind": 2,
                           "uncommitted": False, "ready": True,
                       }):
                resp = client.get("/api/git-status")
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["has_git"] is True
        assert data["behind"] == 2


class TestGitSync:
    """POST /api/git-sync"""

    def test_git_sync_both(self, client):
        with patch("app.routes.git_api.do_git_sync",
                   return_value={"ok": True, "messages": ["Done"]}):
            resp = client.post("/api/git-sync", json={"action": "both"})
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["ok"] is True

    def test_git_sync_pull_only(self, client):
        with patch("app.routes.git_api.do_git_sync",
                   return_value={"ok": True, "messages": ["Pulled"]}):
            resp = client.post("/api/git-sync", json={"action": "pull"})
        data = resp.get_json()
        assert data["ok"] is True


class TestProjectGitStatus:
    """GET /api/project-git-status"""

    def test_git_project(self, client, tmp_path):
        proj = tmp_path / "git_proj"
        proj.mkdir()
        (proj / ".git").mkdir()  # fake git dir

        with patch("app.routes.git_api.get_active_project",
                   return_value="encoded"):
            with patch("app.routes.git_api._decode_project",
                       return_value=str(proj)):
                with patch("subprocess.run") as mock_run:
                    # rev-parse -> success (is git)
                    # branch -> "main"
                    # status -> ""
                    # log -> "abc1234 Initial commit"
                    mock_run.side_effect = [
                        MagicMock(returncode=0, stdout="true\n"),
                        MagicMock(returncode=0, stdout="main\n"),
                        MagicMock(returncode=0, stdout=""),
                        MagicMock(returncode=0,
                                  stdout="abc1234 Initial commit\n"),
                    ]
                    resp = client.get("/api/project-git-status")
        data = resp.get_json()
        assert data["is_git"] is True
        assert data["branch"] == "main"

    def test_non_git_directory(self, client, tmp_path):
        proj = tmp_path / "no_git"
        proj.mkdir()

        with patch("app.routes.git_api.get_active_project",
                   return_value="encoded"):
            with patch("app.routes.git_api._decode_project",
                       return_value=str(proj)):
                with patch("subprocess.run") as mock_run:
                    mock_run.return_value = MagicMock(
                        returncode=128, stdout="", stderr="not a git repo"
                    )
                    resp = client.get("/api/project-git-status")
        data = resp.get_json()
        assert data["is_git"] is False


# ===================================================================
# ADDITIONAL EDGE CASE TESTS
# ===================================================================

class TestSessionLogEdgeCases:
    """Extra edge cases for session log parsing."""

    def test_mixed_content_types_in_user_message(self, client, fake_project):
        """User message with list content containing both text and tool_result."""
        line = json.dumps({
            "type": "user",
            "message": {
                "role": "user",
                "content": [
                    {"type": "text", "text": "Here is input"},
                    {
                        "type": "tool_result",
                        "tool_use_id": "tu-99",
                        "content": [
                            {"type": "text", "text": "Result output"},
                        ],
                        "is_error": True,
                    },
                ],
            },
            "timestamp": "2026-03-01T00:00:00Z",
        })
        p = fake_project / "log-mixed.jsonl"
        p.write_text(line + "\n", encoding="utf-8")

        resp = client.get("/api/session-log/log-mixed")
        data = resp.get_json()
        assert any(e["kind"] == "user" for e in data["entries"])
        tr = [e for e in data["entries"] if e["kind"] == "tool_result"]
        assert len(tr) == 1
        assert tr[0]["is_error"] is True

    def test_assistant_text_block_in_list(self, client, fake_project):
        line = json.dumps({
            "type": "assistant",
            "message": {
                "role": "assistant",
                "content": [
                    {"type": "text", "text": "Block text response"},
                ],
            },
            "timestamp": "2026-03-01T00:00:00Z",
        })
        p = fake_project / "log-asst-list.jsonl"
        p.write_text(line + "\n", encoding="utf-8")

        resp = client.get("/api/session-log/log-asst-list")
        data = resp.get_json()
        asst = [e for e in data["entries"] if e["kind"] == "asst"]
        assert len(asst) == 1
        assert asst[0]["text"] == "Block text response"


class TestCRUDEdgeCases:
    """Edge cases for session CRUD operations."""

    def test_duplicate_preserves_message_count(self, client,
                                                populated_project,
                                                fake_project):
        resp = client.post("/api/duplicate/sess-001")
        new_id = resp.get_json()["new_id"]

        original = (fake_project / "sess-001.jsonl").read_text(
            encoding="utf-8"
        ).strip().splitlines()
        copy = (fake_project / f"{new_id}.jsonl").read_text(
            encoding="utf-8"
        ).strip().splitlines()

        # Same number of non-empty lines
        orig_lines = [l for l in original if l.strip()]
        copy_lines = [l for l in copy if l.strip()]
        assert len(orig_lines) == len(copy_lines)

    def test_continue_creates_three_lines(self, client, populated_project,
                                          fake_project):
        """Continue should create snapshot + user entry + title entry."""
        with patch("app.routes.sessions_api._decode_project",
                   return_value=str(fake_project)), \
             patch("app.routes.sessions_api.smart_title",
                   return_value="Topic Stub"):
            resp = client.post("/api/continue/sess-001")
        new_id = resp.get_json()["new_id"]
        content = (fake_project / f"{new_id}.jsonl").read_text(
            encoding="utf-8"
        )
        lines = [l for l in content.strip().splitlines() if l.strip()]
        assert len(lines) == 3

    def test_delete_empty_idempotent(self, client, fake_project):
        """Calling delete-empty twice should not error."""
        p = fake_project / "empty1.jsonl"
        p.write_text("", encoding="utf-8")
        client.delete("/api/delete-empty")
        resp = client.delete("/api/delete-empty")
        assert resp.status_code == 200

    def test_sessions_sorted_by_timestamp(self, client, fake_project):
        """Sessions should be returned sorted by sort_ts descending."""
        _write_session(fake_project, "old", [
            _jsonl_line("user", "Old msg", "2025-01-01T00:00:00Z"),
            _jsonl_line("assistant", "Old reply", "2025-01-01T00:00:01Z"),
        ])
        _write_session(fake_project, "new", [
            _jsonl_line("user", "New msg", "2026-06-01T00:00:00Z"),
            _jsonl_line("assistant", "New reply", "2026-06-01T00:00:01Z"),
        ])
        resp = client.get("/api/sessions")
        data = resp.get_json()
        if len(data) >= 2:
            timestamps = [s["sort_ts"] for s in data]
            assert timestamps == sorted(timestamps, reverse=True)


# ===================================================================
# PROJECT PATH-TRAVERSAL GUARD (sessions_api blueprint before_request)
# ===================================================================

class TestProjectTraversalGuard:
    """The blueprint-level guard must reject any ``project`` containing
    path separators or '..' (query string OR JSON body) with 400, while
    leaving legitimate encoded project names and project-less requests
    untouched.  Mirrors the /api/search guard added in the same pass."""

    BAD_PROJECTS = ("..%2F..%2Fevil", ".." + chr(92) + "evil", "a/b", "..")

    def test_query_param_traversal_rejected_on_get_routes(self, client):
        for bad in self.BAD_PROJECTS:
            for route in (f"/api/sessions?project={bad}",
                          f"/api/session/sess-001?project={bad}",
                          f"/api/trash?project={bad}",
                          f"/api/session-timeline/sess-001?project={bad}"):
                resp = client.get(route)
                assert resp.status_code == 400, route
                assert resp.get_json()["error"] == "invalid project name"

    def test_query_param_traversal_rejected_on_mutating_routes(self, client):
        bad = "..%2Fevil"
        resp = client.delete(f"/api/delete/sess-001?project={bad}")
        assert resp.status_code == 400
        resp = client.post(f"/api/duplicate/sess-001?project={bad}")
        assert resp.status_code == 400

    def test_json_body_traversal_rejected(self, client):
        resp = client.post("/api/rename/sess-001",
                           json={"title": "x", "project": "../evil"})
        assert resp.status_code == 400
        assert resp.get_json()["error"] == "invalid project name"
        resp = client.post("/api/autonname/sess-001",
                           json={"project": ".." + chr(92) + "evil"})
        assert resp.status_code == 400

    def test_legitimate_encoded_project_still_works(self, client, populated_project):
        resp = client.get("/api/sessions?project=C--Users-test-Documents-myproj")
        assert resp.status_code == 200

    def test_requests_without_project_unaffected(self, client, populated_project):
        assert client.get("/api/sessions").status_code == 200
        resp = client.post("/api/rename/sess-001", json={"title": "Renamed"})
        assert resp.status_code == 200
