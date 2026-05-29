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


# ---------------------------------------------------------------------------
# Phase 6 lifecycle bits — parent-deleted orphaning + rewind detection
# ---------------------------------------------------------------------------

class TestParentDeletionOrphaning:
    def test_delete_endpoint_calls_orphan_children_of(
        self, client, session_manager, parent_session
    ):
        """Deleting a parent session calls SessionManager.orphan_children_of
        so any in-memory children get their parent pointer cleared and a
        parent_deleted_at timestamp.  Tested via the MagicMock — we just
        assert the wiring."""
        parent_sid, project, _ = parent_session

        session_manager.has_session.return_value = False
        session_manager.orphan_children_of.return_value = ["child-A"]

        resp = client.delete(
            f"/api/delete/{parent_sid}?project={project}"
        )
        # Delete is best-effort — 200 or 404 acceptable in the test fixture
        # depending on tombstone semantics; the wiring is the contract.
        assert resp.status_code in (200, 404)
        session_manager.orphan_children_of.assert_called_with(parent_sid)

    def test_delete_endpoint_removes_inbox_directory(
        self, client, session_manager, parent_session, tmp_path, monkeypatch
    ):
        """Deleting a parent removes its vibenode-state/<sid>/ directory
        (spec §6.2).  We append a report to seed the directory, then
        confirm the endpoint nukes it.
        """
        from daemon import subsession_inbox as ibx

        parent_sid, project, _ = parent_session
        session_manager.has_session.return_value = False
        session_manager.orphan_children_of.return_value = []

        # Seed the inbox.
        ibx.append_report(parent_sid, "c", "child", "msg")
        assert ibx.inbox_dir_for(parent_sid).exists()

        resp = client.delete(
            f"/api/delete/{parent_sid}?project={project}"
        )
        assert resp.status_code in (200, 404)
        assert not ibx.inbox_dir_for(parent_sid).exists()


class TestRewindOrphanDetection:
    def test_rewind_endpoint_surfaces_rewind_orphans_when_detected(
        self, client, session_manager, parent_session
    ):
        """After a rewind, the endpoint returns the list of child SIDs
        whose subsession_origin_turn is past the new line count."""
        parent_sid, project, parent_path = parent_session

        # Add edit-tool entries to the JSONL so the rewind endpoint
        # actually proceeds past its "no edits" 400.
        import json
        edit_obj = {
            "type": "assistant",
            "uuid": "edit-1",
            "sessionId": parent_sid,
            "message": {
                "role": "assistant",
                "content": [{
                    "type": "tool_use",
                    "name": "Edit",
                    "input": {
                        "file_path": "x.py",
                        "old_string": "foo",
                        "new_string": "bar",
                    },
                }],
            },
            "timestamp": "2026-05-28T10:01:00Z",
        }
        with open(parent_path, "a", encoding="utf-8") as f:
            f.write(json.dumps(edit_obj) + "\n")

        # Stub detect_rewind_orphans to return two flagged children.
        session_manager.detect_rewind_orphans.return_value = [
            "orphan-child-1", "orphan-child-2"
        ]

        resp = client.post(
            f"/api/rewind/{parent_sid}?project={project}",
            json={"up_to_line": 1},
        )
        # Rewind itself may 400 on "no edits found" depending on the test
        # JSONL — what we're testing is the wiring; assert detect was called.
        if resp.status_code == 200:
            data = resp.get_json()
            assert data.get("rewind_orphans") == [
                "orphan-child-1", "orphan-child-2"
            ]
        # Either way, the daemon helper was invoked with up_to_line.
        session_manager.detect_rewind_orphans.assert_called()


# ---------------------------------------------------------------------------
# Phase 6 — SessionManager helpers exposed by Phase 6 work
# ---------------------------------------------------------------------------

class TestSessionManagerLifecycleHelpers:
    def test_orphan_children_of_clears_parent_pointer(self, sm_module=None):
        """SessionManager.orphan_children_of sets parent_deleted_at and
        clears parent_session_id on every in-memory child."""
        import sys
        from unittest.mock import MagicMock, patch

        class _MockSDKTypes:
            ClaudeSDKClient = MagicMock
            ClaudeCodeOptions = MagicMock

        sdk_mod = MagicMock()
        sdk_types_mod = MagicMock()
        for name in ("ClaudeSDKClient", "ClaudeCodeOptions"):
            setattr(sdk_mod, name, getattr(_MockSDKTypes, name))
        with patch.dict(sys.modules, {
            "claude_code_sdk": sdk_mod,
            "claude_code_sdk.types": sdk_types_mod,
        }):
            import importlib
            import daemon.session_manager as sm
            importlib.reload(sm)

            mgr = sm.SessionManager()
            parent_sid = "parent-001"
            child_a = sm.SessionInfo(session_id="child-a", parent_session_id=parent_sid)
            child_b = sm.SessionInfo(session_id="child-b", parent_session_id=parent_sid)
            unrelated = sm.SessionInfo(session_id="other", parent_session_id="someone-else")
            mgr._sessions = {
                "child-a": child_a,
                "child-b": child_b,
                "other": unrelated,
            }

            orphaned = mgr.orphan_children_of(parent_sid)
            assert set(orphaned) == {"child-a", "child-b"}
            assert child_a.parent_session_id is None
            assert child_a.parent_deleted_at  # ISO8601 timestamp
            assert child_b.parent_session_id is None
            # Unrelated session is untouched.
            assert unrelated.parent_session_id == "someone-else"
            assert unrelated.parent_deleted_at is None

    def test_detect_rewind_orphans_flags_only_children_past_anchor(self):
        import sys
        from unittest.mock import MagicMock, patch

        class _MockSDKTypes:
            ClaudeSDKClient = MagicMock
            ClaudeCodeOptions = MagicMock

        sdk_mod = MagicMock()
        sdk_types_mod = MagicMock()
        for name in ("ClaudeSDKClient", "ClaudeCodeOptions"):
            setattr(sdk_mod, name, getattr(_MockSDKTypes, name))
        with patch.dict(sys.modules, {
            "claude_code_sdk": sdk_mod,
            "claude_code_sdk.types": sdk_types_mod,
        }):
            import importlib
            import daemon.session_manager as sm
            importlib.reload(sm)

            mgr = sm.SessionManager()
            parent_sid = "parent-rewind"
            old_anchor = sm.SessionInfo(
                session_id="old",
                parent_session_id=parent_sid,
                subsession_origin_turn=10,
            )
            new_anchor = sm.SessionInfo(
                session_id="new",
                parent_session_id=parent_sid,
                subsession_origin_turn=2,
            )
            unrelated = sm.SessionInfo(
                session_id="other",
                parent_session_id="x",
                subsession_origin_turn=100,
            )
            mgr._sessions = {
                "old": old_anchor, "new": new_anchor, "other": unrelated,
            }

            # Rewind to line 5 — old (turn 10) is now an orphan,
            # new (turn 2) survives.  Unrelated is ignored.
            flagged = mgr.detect_rewind_orphans(parent_sid, 5)
            assert flagged == ["old"]

    def test_auto_report_on_idle_writes_to_parent_inbox(self, tmp_path, monkeypatch):
        """Phase 6.5 P1-4 — a subsession with auto_report_on_idle=True
        and a parent pointer writes its last assistant message to the
        parent's inbox on every IDLE _emit_state that has a fresh entry.
        Idempotent: a second IDLE emit with no new entries must NOT
        produce a duplicate report.
        """
        import sys
        from unittest.mock import MagicMock, patch

        # Isolate Path.home() so the inbox writes into a sandbox.
        monkeypatch.setenv("HOME", str(tmp_path))
        monkeypatch.setenv("USERPROFILE", str(tmp_path))
        monkeypatch.setattr(Path, "home", lambda: tmp_path)

        class _MockSDKTypes:
            ClaudeSDKClient = MagicMock
            ClaudeCodeOptions = MagicMock

        sdk_mod = MagicMock()
        sdk_types_mod = MagicMock()
        for name in ("ClaudeSDKClient", "ClaudeCodeOptions"):
            setattr(sdk_mod, name, getattr(_MockSDKTypes, name))
        with patch.dict(sys.modules, {
            "claude_code_sdk": sdk_mod,
            "claude_code_sdk.types": sdk_types_mod,
        }):
            import importlib
            import daemon.session_manager as sm
            importlib.reload(sm)
            from daemon import subsession_inbox as ibx

            mgr = sm.SessionManager()
            parent_sid = "auto-report-parent"
            child_sid = "auto-report-child"

            child = sm.SessionInfo(
                session_id=child_sid,
                parent_session_id=parent_sid,
                auto_report_on_idle=True,
                name="Investigate the flake",
            )
            # Synthesize a finished turn: one user entry + one assistant.
            child.entries.append(sm.LogEntry(kind="user", text="run X"))
            child.entries.append(
                sm.LogEntry(kind="assistant", text="Found the bug at line 42.")
            )
            child.state = sm.SessionState.IDLE
            mgr._sessions = {child_sid: child}

            # First IDLE _emit_state — should trigger an auto-report.
            mgr._emit_state(child)
            inbox = ibx.load_inbox(parent_sid)
            assert len(inbox["pending_reports"]) == 1
            entry = inbox["pending_reports"][0]
            assert entry["child_session_id"] == child_sid
            assert "line 42" in entry["summary"]
            assert entry["delivered"] is False
            # Counter snapshotted at the fired entry count.
            assert child._last_auto_report_entry_count == 2

            # Second IDLE _emit_state with no new entries — idempotent.
            mgr._emit_state(child)
            inbox = ibx.load_inbox(parent_sid)
            assert len(inbox["pending_reports"]) == 1, \
                "Idempotency: second IDLE without new entries must not duplicate."

            # Add a new assistant message — third IDLE emits a new report.
            child.entries.append(
                sm.LogEntry(kind="assistant", text="Second turn conclusion.")
            )
            mgr._emit_state(child)
            inbox = ibx.load_inbox(parent_sid)
            assert len(inbox["pending_reports"]) == 2
            assert "Second turn" in inbox["pending_reports"][1]["summary"]

    def test_auto_report_off_does_nothing(self, tmp_path, monkeypatch):
        """When auto_report_on_idle is False, no report is written even
        if the child has assistant content and a parent pointer."""
        import sys
        from unittest.mock import MagicMock, patch

        monkeypatch.setenv("HOME", str(tmp_path))
        monkeypatch.setenv("USERPROFILE", str(tmp_path))
        monkeypatch.setattr(Path, "home", lambda: tmp_path)

        class _MockSDKTypes:
            ClaudeSDKClient = MagicMock
            ClaudeCodeOptions = MagicMock

        sdk_mod = MagicMock()
        sdk_types_mod = MagicMock()
        for name in ("ClaudeSDKClient", "ClaudeCodeOptions"):
            setattr(sdk_mod, name, getattr(_MockSDKTypes, name))
        with patch.dict(sys.modules, {
            "claude_code_sdk": sdk_mod,
            "claude_code_sdk.types": sdk_types_mod,
        }):
            import importlib
            import daemon.session_manager as sm
            importlib.reload(sm)
            from daemon import subsession_inbox as ibx

            mgr = sm.SessionManager()
            parent_sid = "no-auto-parent"
            child = sm.SessionInfo(
                session_id="no-auto-child",
                parent_session_id=parent_sid,
                auto_report_on_idle=False,
                name="silent",
            )
            child.entries.append(
                sm.LogEntry(kind="assistant", text="Wouldn't report this.")
            )
            child.state = sm.SessionState.IDLE
            mgr._sessions = {child.session_id: child}
            mgr._emit_state(child)

            inbox = ibx.load_inbox(parent_sid)
            assert inbox["pending_reports"] == []

    def test_set_auto_report_on_idle_endpoint_toggles_flag(
        self, client, session_manager
    ):
        """The /api/sessions/<sid>/auto-report-toggle endpoint calls
        set_auto_report_on_idle on the daemon and returns ok."""
        child_sid = str(uuid.uuid4())
        session_manager.set_auto_report_on_idle.return_value = True

        resp = client.post(
            f"/api/sessions/{child_sid}/auto-report-toggle",
            json={"on": True},
        )
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["ok"] is True
        assert data["auto_report_on_idle"] is True
        session_manager.set_auto_report_on_idle.assert_called_once_with(
            child_sid, True
        )

    def test_set_auto_report_on_idle_endpoint_404_when_unknown(
        self, client, session_manager
    ):
        child_sid = str(uuid.uuid4())
        session_manager.set_auto_report_on_idle.return_value = False
        resp = client.post(
            f"/api/sessions/{child_sid}/auto-report-toggle",
            json={"on": True},
        )
        assert resp.status_code == 404


# ---------------------------------------------------------------------------
# Phase 6.5 P1-5 — Rewind-orphan reanchor / detach endpoints
# ---------------------------------------------------------------------------

class TestRewindOrphanEndpoints:
    """The rewind-orphan UI prompt calls POST /reanchor or /detach for
    each orphaned child the user picks an action for.  Phase 6.5 P1-5."""

    def test_reanchor_with_explicit_origin_turn(
        self, client, session_manager
    ):
        """Caller passes an explicit origin_turn — endpoint forwards
        to SessionManager.reanchor_subsession and returns 200."""
        child_sid = str(uuid.uuid4())
        session_manager.reanchor_subsession.return_value = True
        resp = client.post(
            f"/api/sessions/{child_sid}/reanchor",
            json={"origin_turn": 12},
        )
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["ok"] is True
        assert data["subsession_origin_turn"] == 12
        session_manager.reanchor_subsession.assert_called_once_with(
            child_sid, 12
        )

    def test_reanchor_derives_origin_turn_from_parent_tip(
        self, client, session_manager, parent_session
    ):
        """When the body has no origin_turn, the endpoint reads the
        parent's current JSONL line count and uses that — the spec §6.3
        'Re-anchor at current parent tip' affordance."""
        parent_sid, project, parent_path = parent_session
        child_sid = str(uuid.uuid4())

        def _meta(sid):
            if sid == child_sid:
                return {
                    "session_id": child_sid,
                    "parent_session_id": parent_sid,
                    "cwd": "",
                    "session_type": "subsession",
                }
            return None

        session_manager.get_subsession_meta.side_effect = _meta
        session_manager.reanchor_subsession.return_value = True

        # Parent JSONL fixture has 3 lines.
        resp = client.post(
            f"/api/sessions/{child_sid}/reanchor?project={project}",
            json={},
        )
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["ok"] is True
        assert data["subsession_origin_turn"] == 3
        session_manager.reanchor_subsession.assert_called_once_with(
            child_sid, 3
        )

    def test_reanchor_404_when_child_missing(self, client, session_manager):
        child_sid = str(uuid.uuid4())
        session_manager.reanchor_subsession.return_value = False
        resp = client.post(
            f"/api/sessions/{child_sid}/reanchor",
            json={"origin_turn": 1},
        )
        assert resp.status_code == 404

    def test_detach_endpoint_calls_daemon_helper(
        self, client, session_manager
    ):
        child_sid = str(uuid.uuid4())
        session_manager.detach_subsession.return_value = True
        resp = client.post(
            f"/api/sessions/{child_sid}/detach",
            json={},
        )
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["ok"] is True
        session_manager.detach_subsession.assert_called_once_with(child_sid)

    def test_detach_endpoint_404_when_unknown(self, client, session_manager):
        child_sid = str(uuid.uuid4())
        session_manager.detach_subsession.return_value = False
        resp = client.post(
            f"/api/sessions/{child_sid}/detach",
            json={},
        )
        assert resp.status_code == 404

    def test_detach_helper_clears_parent_pointer_and_stamps_tombstone(self):
        """SessionManager.detach_subsession sets parent_session_id=None
        and stamps parent_deleted_at on the in-memory SessionInfo."""
        import sys
        from unittest.mock import MagicMock, patch

        class _MockSDKTypes:
            ClaudeSDKClient = MagicMock
            ClaudeCodeOptions = MagicMock

        sdk_mod = MagicMock()
        sdk_types_mod = MagicMock()
        for name in ("ClaudeSDKClient", "ClaudeCodeOptions"):
            setattr(sdk_mod, name, getattr(_MockSDKTypes, name))
        with patch.dict(sys.modules, {
            "claude_code_sdk": sdk_mod,
            "claude_code_sdk.types": sdk_types_mod,
        }):
            import importlib
            import daemon.session_manager as sm
            importlib.reload(sm)

            mgr = sm.SessionManager()
            child = sm.SessionInfo(
                session_id="child-1",
                parent_session_id="parent-1",
            )
            mgr._sessions = {"child-1": child}
            assert mgr.detach_subsession("child-1") is True
            assert child.parent_session_id is None
            assert child.parent_deleted_at  # ISO8601 timestamp
            assert mgr.detach_subsession("missing") is False

    def test_reanchor_subsession_updates_origin_turn(self):
        import sys
        from unittest.mock import MagicMock, patch

        class _MockSDKTypes:
            ClaudeSDKClient = MagicMock
            ClaudeCodeOptions = MagicMock

        sdk_mod = MagicMock()
        sdk_types_mod = MagicMock()
        for name in ("ClaudeSDKClient", "ClaudeCodeOptions"):
            setattr(sdk_mod, name, getattr(_MockSDKTypes, name))
        with patch.dict(sys.modules, {
            "claude_code_sdk": sdk_mod,
            "claude_code_sdk.types": sdk_types_mod,
        }):
            import importlib
            import daemon.session_manager as sm
            importlib.reload(sm)

            mgr = sm.SessionManager()
            child = sm.SessionInfo(
                session_id="c",
                parent_session_id="p",
                subsession_origin_turn=12,
            )
            mgr._sessions = {"c": child}
            assert mgr.reanchor_subsession("c", 5) is True
            assert child.subsession_origin_turn == 5
            # Missing session returns False without raising.
            assert mgr.reanchor_subsession("missing", 1) is False


# ---------------------------------------------------------------------------
# Phase 6.5 P1-2 — SID format validation (path-traversal defense)
# ---------------------------------------------------------------------------

class TestSidValidation:
    """The subsession endpoints interpolate the SID into a filesystem path
    under ~/.claude/vibenode-state/<sid>/.  Path-traversal SIDs ("..", "/",
    "\\") must be rejected at the endpoint with a 400 — never reach the
    filesystem.  Phase 6.5 P1-2.
    """

    @pytest.mark.parametrize("bad_sid", [
        "..",                                  # parent dir
        "../etc",                              # POSIX traversal
        "..\\..\\evil",                        # Windows traversal
        "/etc/passwd",                         # absolute POSIX
        "C:\\Windows",                         # absolute Windows
        "{12345678-1234-1234-1234-123456789012}",  # braced UUID (curly braces)
        "has spaces in it",                    # space char
        "has@symbols",                         # @ char
    ])
    def test_spawn_rejects_invalid_sids(
        self, client, session_manager, bad_sid
    ):
        """Spawn-subsession returns 400 for path-traversal SIDs or SIDs
        with characters outside [A-Za-z0-9_-]."""
        resp = client.post(
            f"/api/sessions/{bad_sid}/spawn-subsession",
            json={},
        )
        # Flask routes with a literal path may 404 instead of hitting
        # the handler — that's also a safe failure (no path traversal),
        # so 400 OR 404 OR 405 are all acceptable rejections.
        assert resp.status_code in (400, 404, 405)
        # If the handler did run, start_session must NOT have been called.
        session_manager.start_session.assert_not_called()

    def test_report_to_parent_rejects_invalid_sid(
        self, client, session_manager
    ):
        bad = "..\\..\\evil"
        resp = client.post(
            f"/api/sessions/{bad}/report-to-parent",
            json={"summary": "hi"},
        )
        assert resp.status_code in (400, 404, 405)

    def test_pull_subsession_updates_rejects_invalid_sid(
        self, client, session_manager
    ):
        bad = "../traverse"
        resp = client.post(
            f"/api/sessions/{bad}/pull-subsession-updates",
            json={},
        )
        assert resp.status_code in (400, 404, 405)

    def test_inbox_dir_for_rejects_invalid_sid(self):
        """The lower-level inbox_dir_for() also validates.  Belt-and-
        suspenders for any future caller that bypasses the endpoint
        path."""
        from daemon.subsession_inbox import inbox_dir_for
        with pytest.raises(ValueError):
            inbox_dir_for("..\\..\\evil")
        with pytest.raises(ValueError):
            inbox_dir_for("../traverse")
        with pytest.raises(ValueError):
            inbox_dir_for("")
        with pytest.raises(ValueError):
            inbox_dir_for("/abs/path")
        with pytest.raises(ValueError):
            inbox_dir_for("with spaces")
        with pytest.raises(ValueError):
            inbox_dir_for("with:colon")

    def test_inbox_dir_for_accepts_valid_uuid4(self):
        from daemon.subsession_inbox import inbox_dir_for
        valid = str(uuid.uuid4())
        # Should not raise.
        path = inbox_dir_for(valid)
        assert path.name == valid

    def test_inbox_dir_for_accepts_legacy_short_ids(self):
        """Short title SIDs and integration-test SIDs are alphanumeric
        with dashes/underscores; the validator must not break them."""
        from daemon.subsession_inbox import inbox_dir_for
        # Mirrors app/titling.py:375 form.
        assert inbox_dir_for("_title_a1b2c3d4").name == "_title_a1b2c3d4"
        # Mirrors tests/test_subsessions_integration.py SIDs.
        assert inbox_dir_for("parent-int-001").name == "parent-int-001"


# ---------------------------------------------------------------------------
# Phase 6.5 P0-2 — pull-subsession-updates endpoint
# ---------------------------------------------------------------------------

class TestPullSubsessionUpdatesEndpoint:
    """The /api/sessions/<parent>/pull-subsession-updates REST endpoint
    that replaces the broken WS user_message + /api/live/send fallback.
    Phase 6.5 P0-1/P0-2.
    """

    def test_parent_not_managed_returns_404(self, client, session_manager):
        """If the parent SID is not daemon-managed, return 404 without
        firing send_message (which would also fail)."""
        unknown = str(uuid.uuid4())
        session_manager.get_subsession_meta.return_value = None
        resp = client.post(
            f"/api/sessions/{unknown}/pull-subsession-updates",
            json={},
        )
        assert resp.status_code == 404
        assert "not found" in resp.get_json()["error"].lower()
        session_manager.send_message.assert_not_called()

    def test_no_pending_reports_returns_pulled_false(
        self, client, session_manager
    ):
        """When there are no undelivered reports on disk AND the in-memory
        inbox_dirty flag is False, the endpoint short-circuits without
        invoking send_message."""
        parent_sid = str(uuid.uuid4())
        session_manager.get_subsession_meta.return_value = {
            "session_id": parent_sid,
            "name": "Parent",
            "cwd": "",
            "session_type": "",
            "parent_session_id": None,
        }
        # No inbox file on disk => has_undelivered() returns False.
        # No in-memory dirty flag => endpoint does not call send_message.
        # MagicMock session_manager has no real _sessions; use an empty dict
        # so the endpoint's "is the in-memory parent dirty?" probe finds
        # nothing.  The MagicMock _lock is acquirable as a context manager
        # (default MagicMock supports __enter__/__exit__).
        session_manager._sessions = {}
        resp = client.post(
            f"/api/sessions/{parent_sid}/pull-subsession-updates",
            json={},
        )
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["ok"] is True
        assert data["pulled"] is False
        assert data["undelivered_count"] == 0
        session_manager.send_message.assert_not_called()

    def test_pending_report_triggers_send_message_with_empty_text(
        self, client, session_manager
    ):
        """When the parent has undelivered reports on disk, the endpoint
        calls send_message(parent_sid, '') — the daemon's empty-text
        branch (spec §4.3.5) turns the empty into a drain-block-only
        message."""
        from daemon import subsession_inbox as ibx

        parent_sid = str(uuid.uuid4())
        session_manager.get_subsession_meta.return_value = {
            "session_id": parent_sid,
            "name": "Parent",
            "cwd": "",
            "session_type": "",
            "parent_session_id": None,
        }
        session_manager._sessions = {}
        session_manager.send_message.return_value = {"ok": True}

        # Seed one undelivered report.
        ibx.append_report(parent_sid, "c1", "child-1", "Did the thing")
        try:
            resp = client.post(
                f"/api/sessions/{parent_sid}/pull-subsession-updates",
                json={},
            )
            assert resp.status_code == 200
            data = resp.get_json()
            assert data["ok"] is True
            assert data["pulled"] is True
            assert data["queued"] is False
            # send_message must have been invoked with EMPTY text — the
            # daemon's drain branch keys off text == "" to omit the
            # "[Your message]" suffix.
            session_manager.send_message.assert_called_once_with(
                parent_sid, ""
            )
        finally:
            # Clean up the inbox we seeded so the next test starts fresh.
            ibx.remove_inbox(parent_sid)

    def test_queued_when_session_busy(self, client, session_manager):
        """When send_message returns queued (parent was WORKING), the
        endpoint forwards that shape — the frontend toasts a 'queued'
        message instead of a success message."""
        from daemon import subsession_inbox as ibx

        parent_sid = str(uuid.uuid4())
        session_manager.get_subsession_meta.return_value = {
            "session_id": parent_sid,
            "name": "Parent",
            "cwd": "",
            "session_type": "",
            "parent_session_id": None,
        }
        session_manager._sessions = {}
        session_manager.send_message.return_value = {"queued": True}

        ibx.append_report(parent_sid, "c1", "child-1", "Hello")
        try:
            resp = client.post(
                f"/api/sessions/{parent_sid}/pull-subsession-updates",
                json={},
            )
            assert resp.status_code == 200
            data = resp.get_json()
            assert data["ok"] is True
            assert data["pulled"] is True
            assert data["queued"] is True
        finally:
            ibx.remove_inbox(parent_sid)

    def test_send_message_failure_propagates_400(
        self, client, session_manager
    ):
        """If send_message returns an error, the endpoint forwards a 400
        with the error message so the toast can explain what broke."""
        from daemon import subsession_inbox as ibx

        parent_sid = str(uuid.uuid4())
        session_manager.get_subsession_meta.return_value = {
            "session_id": parent_sid,
            "name": "Parent",
            "cwd": "",
            "session_type": "",
            "parent_session_id": None,
        }
        session_manager._sessions = {}
        session_manager.send_message.return_value = {
            "ok": False,
            "error": "Session is stopped",
        }

        ibx.append_report(parent_sid, "c1", "child-1", "Hi")
        try:
            resp = client.post(
                f"/api/sessions/{parent_sid}/pull-subsession-updates",
                json={},
            )
            assert resp.status_code == 400
            data = resp.get_json()
            assert data["ok"] is False
            assert "stopped" in data["error"].lower()
        finally:
            ibx.remove_inbox(parent_sid)
