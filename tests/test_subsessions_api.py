"""
Tests for the Subsessions API endpoints (spec §4.2 / §4.3 / §6).

This file is grown across Phases 2, 3, and 6 of the Subsessions build.
Phase 2 adds the spawn endpoint coverage; Phase 3 adds report-to-parent;
Phase 6 adds parent-deleted orphaning and rewind-past-spawn detection.

Snapshot-based cleanup pattern (per CLAUDE.md Compose fix #3):
the fixture below snapshots the test's session directory before each test
and removes anything new afterwards — never name-prefix cleanup.
"""

import json
import os
import uuid
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from app import create_app
from app.config import _CLAUDE_PROJECTS, _sessions_dir


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def app():
    """Flask app in testing mode (no daemon)."""
    return create_app(testing=True)


@pytest.fixture
def client(app):
    with app.test_client() as c:
        yield c


@pytest.fixture
def session_manager(app):
    """The MagicMock session_manager stub installed by create_app(testing=True)."""
    return app.session_manager


@pytest.fixture(autouse=True)
def cleanup_sessions_dir():
    """Snapshot _CLAUDE_PROJECTS before each test and remove anything new
    afterwards.  Mirrors the test_compose_api.py snapshot-cleanup pattern
    (CLAUDE.md Compose fix #3) — do NOT regress to name-prefix cleanup."""
    import shutil

    snapshot_dirs = {}
    if _CLAUDE_PROJECTS.is_dir():
        for d in _CLAUDE_PROJECTS.iterdir():
            if d.is_dir():
                snapshot_dirs[d.name] = {
                    f.name for f in d.iterdir() if f.is_file()
                }

    yield

    if _CLAUDE_PROJECTS.is_dir():
        for d in _CLAUDE_PROJECTS.iterdir():
            if not d.is_dir():
                continue
            before_files = snapshot_dirs.get(d.name, None)
            if before_files is None:
                # Entire project directory is new — remove it
                shutil.rmtree(d, ignore_errors=True)
                continue
            for f in d.iterdir():
                if f.is_file() and f.name not in before_files:
                    try:
                        f.unlink()
                    except OSError:
                        pass


@pytest.fixture
def parent_session(tmp_path, app):
    """Create a real .jsonl file under the active project's sessions dir
    seeded with a couple of turns so the spawn endpoint has something to
    slice.

    Returns ``(parent_sid, project_dir_name, parent_jsonl_path)``.
    """
    parent_sid = str(uuid.uuid4())
    project_dir = _sessions_dir("")  # active project
    project_dir.mkdir(parents=True, exist_ok=True)
    jsonl_path = project_dir / f"{parent_sid}.jsonl"

    lines = [
        json.dumps({
            "type": "user",
            "uuid": "u-1",
            "sessionId": parent_sid,
            "message": {"role": "user", "content": "Hello"},
            "timestamp": "2026-05-28T10:00:00Z",
        }),
        json.dumps({
            "type": "assistant",
            "uuid": "a-1",
            "sessionId": parent_sid,
            "message": {
                "role": "assistant",
                "content": [{"type": "text", "text": "Hi there"}],
            },
            "timestamp": "2026-05-28T10:00:01Z",
        }),
        json.dumps({
            "type": "custom-title",
            "customTitle": "Parent topic",
            "sessionId": parent_sid,
        }),
    ]
    jsonl_path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    return parent_sid, project_dir.name, jsonl_path


# ---------------------------------------------------------------------------
# Spawn endpoint — happy path
# ---------------------------------------------------------------------------

class TestSpawnSubsessionHappyPath:
    def test_spawn_creates_child_jsonl_with_rewritten_session_id(
        self, client, session_manager, parent_session
    ):
        """The new subsession gets its own .jsonl whose sessionId fields are
        rewritten to the new UUID, and the response carries the child SID,
        parent SID, and origin-turn count."""
        parent_sid, project, parent_path = parent_session

        # Daemon-side: parent is a normal session in the active project.
        session_manager.get_subsession_meta.return_value = {
            "session_id": parent_sid,
            "name": "Parent topic",
            "cwd": "",  # blank cwd skips the cross-project gate
            "session_type": "",  # normal session
            "parent_session_id": None,
            "subsession_origin_turn": 0,
        }
        session_manager.start_session.return_value = {"ok": True}

        resp = client.post(
            f"/api/sessions/{parent_sid}/spawn-subsession?project={project}",
            json={},
        )
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["ok"] is True
        assert data["parent_id"] == parent_sid
        assert data["new_id"] and data["new_id"] != parent_sid
        # Parent JSONL has 3 lines so the slice captures all 3.
        assert data["subsession_origin_turn"] == 3
        assert data["title"].startswith("[sub] ")

        # Child JSONL exists with the parent slice + a [sub] title entry.
        child_path = parent_path.parent / f"{data['new_id']}.jsonl"
        assert child_path.exists(), "Child JSONL was not written"

        child_lines = [
            json.loads(l)
            for l in child_path.read_text(encoding="utf-8").splitlines()
            if l.strip()
        ]
        # Every sessionId in the child must be the new ID, never the parent.
        for entry in child_lines:
            if "sessionId" in entry:
                assert entry["sessionId"] == data["new_id"]
        # Last line is the [sub] custom-title.
        last = child_lines[-1]
        assert last["type"] == "custom-title"
        assert last["customTitle"].startswith("[sub] ")

        # start_session was invoked with the parent pointer + session_type.
        session_manager.start_session.assert_called_once()
        kwargs = session_manager.start_session.call_args.kwargs
        assert kwargs["session_id"] == data["new_id"]
        assert kwargs["parent_session_id"] == parent_sid
        assert kwargs["subsession_origin_turn"] == 3
        assert kwargs["session_type"] == "subsession"
        assert kwargs["resume"] is True


# ---------------------------------------------------------------------------
# Spawn endpoint — guard rails
# ---------------------------------------------------------------------------

class TestSpawnSubsessionGuards:
    def test_parent_not_found(self, client, session_manager):
        """Spawning from a non-existent parent SID returns 404."""
        session_manager.get_subsession_meta.return_value = None
        unknown = str(uuid.uuid4())
        resp = client.post(f"/api/sessions/{unknown}/spawn-subsession", json={})
        assert resp.status_code == 404
        assert "not found" in resp.get_json()["error"].lower()
        # start_session was NOT called.
        session_manager.start_session.assert_not_called()

    def test_planner_parent_rejected(
        self, client, session_manager, parent_session
    ):
        """Planner sessions cannot legally be parents (spec §4.2)."""
        parent_sid, project, _ = parent_session
        session_manager.get_subsession_meta.return_value = {
            "session_id": parent_sid,
            "name": "Planner",
            "cwd": "",
            "session_type": "planner",
            "parent_session_id": None,
            "subsession_origin_turn": 0,
        }
        resp = client.post(
            f"/api/sessions/{parent_sid}/spawn-subsession?project={project}",
            json={},
        )
        assert resp.status_code == 400
        assert "planner" in resp.get_json()["error"].lower()
        session_manager.start_session.assert_not_called()

    def test_cross_project_parent_rejected(
        self, client, session_manager, parent_session
    ):
        """A parent whose cwd belongs to a different project is rejected
        (spec §6.5).  We force the daemon-reported cwd to a path that does
        NOT encode to the current active project name."""
        parent_sid, project, _ = parent_session
        # Set a clearly-foreign cwd: encode_cwd("/totally/other/proj") is
        # "-totally-other-proj" which won't match the active project dir.
        session_manager.get_subsession_meta.return_value = {
            "session_id": parent_sid,
            "name": "Parent",
            "cwd": "/totally/other/proj",
            "session_type": "",
            "parent_session_id": None,
            "subsession_origin_turn": 0,
        }
        resp = client.post(
            f"/api/sessions/{parent_sid}/spawn-subsession?project={project}",
            json={},
        )
        assert resp.status_code == 400
        assert "cross-project" in resp.get_json()["error"].lower() or \
               "different project" in resp.get_json()["error"].lower()
        session_manager.start_session.assert_not_called()

    def test_report_to_parent_happy_path(
        self, client, session_manager, parent_session
    ):
        """A child can report up to a known parent; the parent's inbox
        receives the entry and the response carries the
        undelivered_count."""
        from daemon import subsession_inbox as ibx

        parent_sid, project, _ = parent_session
        child_sid = str(uuid.uuid4())

        def _meta(sid):
            if sid == child_sid:
                return {
                    "session_id": child_sid,
                    "name": "Investigate flake",
                    "cwd": "",
                    "session_type": "subsession",
                    "parent_session_id": parent_sid,
                    "subsession_origin_turn": 3,
                }
            if sid == parent_sid:
                return {
                    "session_id": parent_sid,
                    "name": "Parent topic",
                    "cwd": "",
                    "session_type": "",
                    "parent_session_id": None,
                    "subsession_origin_turn": 0,
                }
            return None

        session_manager.get_subsession_meta.side_effect = _meta
        session_manager.mark_inbox_dirty.return_value = True

        resp = client.post(
            f"/api/sessions/{child_sid}/report-to-parent?project={project}",
            json={"summary": "Found a one-liner fix"},
        )
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["ok"] is True
        assert data["parent_session_id"] == parent_sid
        assert data["undelivered_count"] == 1
        assert data["report_id"]

        # Sanity: the entry actually landed on disk.
        inbox = ibx.load_inbox(parent_sid)
        assert len(inbox["pending_reports"]) == 1
        assert inbox["pending_reports"][0]["summary"] == "Found a one-liner fix"

        # mark_inbox_dirty was called on the parent.
        session_manager.mark_inbox_dirty.assert_called_with(parent_sid)

    def test_report_to_parent_missing_summary(self, client, session_manager):
        child_sid = str(uuid.uuid4())
        session_manager.get_subsession_meta.return_value = {
            "session_id": child_sid,
            "parent_session_id": "p",
            "cwd": "",
            "session_type": "subsession",
        }
        resp = client.post(
            f"/api/sessions/{child_sid}/report-to-parent",
            json={"summary": ""},
        )
        assert resp.status_code == 400
        assert "summary" in resp.get_json()["error"].lower()

    def test_report_to_parent_no_parent(self, client, session_manager):
        """A subsession whose parent_session_id is None returns 404."""
        child_sid = str(uuid.uuid4())
        session_manager.get_subsession_meta.return_value = {
            "session_id": child_sid,
            "name": "orphan",
            "cwd": "",
            "session_type": "subsession",
            "parent_session_id": None,
        }
        resp = client.post(
            f"/api/sessions/{child_sid}/report-to-parent",
            json={"summary": "Hello"},
        )
        assert resp.status_code == 404
        assert "parent" in resp.get_json()["error"].lower()

    def test_report_to_parent_missing_parent(
        self, client, session_manager, parent_session
    ):
        """A child whose parent_session_id points at a SID that exists in
        neither the daemon nor on disk returns 404."""
        parent_sid, project, parent_path = parent_session
        # Delete the parent JSONL to simulate a parent that's been
        # removed both from the daemon and from disk.
        parent_path.unlink()
        child_sid = str(uuid.uuid4())

        def _meta(sid):
            if sid == child_sid:
                return {
                    "session_id": child_sid,
                    "name": "child",
                    "cwd": "",
                    "session_type": "subsession",
                    "parent_session_id": parent_sid,
                }
            return None  # parent is gone from daemon too

        session_manager.get_subsession_meta.side_effect = _meta
        resp = client.post(
            f"/api/sessions/{child_sid}/report-to-parent?project={project}",
            json={"summary": "Hello"},
        )
        assert resp.status_code == 404

    def test_cycle_in_parent_chain_rejected(
        self, client, session_manager, parent_session
    ):
        """If the spawn endpoint's parent-chain walk discovers a cycle (a
        sentinel for a corrupted in-memory graph), the spawn aborts with
        409.  We simulate by having get_subsession_meta return a parent
        whose parent_session_id points back at itself."""
        parent_sid, project, _ = parent_session

        def _meta(sid):
            # Self-referential parent chain — instant cycle on hop 1.
            return {
                "session_id": sid,
                "name": "Cyclic",
                "cwd": "",
                "session_type": "",
                "parent_session_id": sid,
                "subsession_origin_turn": 0,
            }

        session_manager.get_subsession_meta.side_effect = _meta
        resp = client.post(
            f"/api/sessions/{parent_sid}/spawn-subsession?project={project}",
            json={},
        )
        assert resp.status_code == 409
        assert "cycle" in resp.get_json()["error"].lower()
        session_manager.start_session.assert_not_called()
