"""
Tests for moving a session between projects.

Covers the store-level ``move_session`` helper and the
``POST /api/move-session/<id>`` route: the happy path (file + display name
relocate, source tombstoned), and the guard rails (missing target, same
project, id collision in target, path traversal).
"""

import json
from pathlib import Path
from unittest.mock import patch

import pytest


SRC = "C--Users-test-ProjA"
DST = "C--Users-test-ProjB"


@pytest.fixture()
def projects_root(tmp_path):
    root = tmp_path / "projects"
    (root / SRC).mkdir(parents=True)
    (root / DST).mkdir(parents=True)
    return root


def _sessions_dir_for(projects_root):
    """A _sessions_dir replacement that maps an encoded project to its dir."""
    def _fn(project: str = "") -> Path:
        return projects_root / project if project else projects_root / SRC
    return _fn


@pytest.fixture()
def app(projects_root):
    from app import create_app

    application = create_app(testing=True)
    application.session_manager.has_session.return_value = False

    sd = _sessions_dir_for(projects_root)
    with (
        patch("app.config._sessions_dir", side_effect=sd),
        patch("app.session_store._sessions_dir", side_effect=sd),
        patch("app.routes.sessions_api._sessions_dir", side_effect=sd),
        patch("app.config._CLAUDE_PROJECTS", projects_root),
        patch("app.routes.sessions_api._CLAUDE_PROJECTS", projects_root),
    ):
        yield application


@pytest.fixture()
def client(app):
    return app.test_client()


def _write_session(projects_root, project, sid, name=None, custom_title=None):
    p = projects_root / project / f"{sid}.jsonl"
    lines = []
    if custom_title:
        lines.append(json.dumps({"type": "custom-title",
                                 "customTitle": custom_title, "sessionId": sid}))
    lines.append(json.dumps({"sessionId": sid, "type": "user"}))
    p.write_text("\n".join(lines) + "\n", encoding="utf-8")
    if name:
        nf = projects_root / project / "_session_names.json"
        nf.write_text(json.dumps({sid: name}), encoding="utf-8")
    return p


def _set_name(projects_root, project, sid, name):
    """Seed a _session_names.json entry without touching the .jsonl (used to
    simulate a stale name left by a prior occupant of an id)."""
    nf = projects_root / project / "_session_names.json"
    try:
        cur = json.loads(nf.read_text())
    except Exception:
        cur = {}
    cur[sid] = name
    nf.write_text(json.dumps(cur), encoding="utf-8")


# ---------------------------------------------------------------------------
# Store-level move_session
# ---------------------------------------------------------------------------

class TestMoveSessionStore:
    def test_moves_file_and_carries_name(self, projects_root):
        from app import session_store as ss
        with patch("app.session_store._sessions_dir",
                   side_effect=_sessions_dir_for(projects_root)):
            sid = "aaaa1111-bbbb-2222-cccc-333344445555"
            _write_session(projects_root, SRC, sid, name="My Work")
            r = ss.move_session(sid, SRC, DST)
            assert r["ok"] is True
            assert r["name"] == "My Work"
            assert not (projects_root / SRC / f"{sid}.jsonl").exists()
            assert (projects_root / DST / f"{sid}.jsonl").exists()
            assert ss._load_names(DST).get(sid) == "My Work"
            assert sid not in ss._load_names(SRC)

    def test_missing_source_rejected(self, projects_root):
        from app import session_store as ss
        with patch("app.session_store._sessions_dir",
                   side_effect=_sessions_dir_for(projects_root)):
            r = ss.move_session("does-not-exist", SRC, DST)
            assert r["ok"] is False

    def test_stale_target_name_is_overwritten(self, projects_root):
        # Regression: an id that previously lived in the target left a stale
        # name entry there. A move must overwrite it with the source's name,
        # not let the moved session show the old, unrelated title.
        from app import session_store as ss
        with patch("app.session_store._sessions_dir",
                   side_effect=_sessions_dir_for(projects_root)):
            sid = "beef1111-2222-3333-4444-555566667777"
            _write_session(projects_root, SRC, sid, name="Correct Title")
            _set_name(projects_root, DST, sid, "Stale Old Title")  # prior occupant
            r = ss.move_session(sid, SRC, DST)
            assert r["ok"] is True
            assert ss._load_names(DST).get(sid) == "Correct Title"

    def test_stale_target_name_cleared_when_source_unnamed(self, projects_root):
        # When the source has no resolvable display name, any stale target
        # entry must be removed so the moved session falls back to its own
        # .jsonl title rather than an unrelated old name.
        from app import session_store as ss
        with patch("app.session_store._sessions_dir",
                   side_effect=_sessions_dir_for(projects_root)):
            sid = "cafe1111-2222-3333-4444-555566667777"
            _write_session(projects_root, SRC, sid)  # no names entry
            _set_name(projects_root, DST, sid, "Stale Old Title")
            r = ss.move_session(sid, SRC, DST, source_display="")
            assert r["ok"] is True
            assert sid not in ss._load_names(DST)

    def test_target_collision_preserves_source(self, projects_root):
        from app import session_store as ss
        with patch("app.session_store._sessions_dir",
                   side_effect=_sessions_dir_for(projects_root)):
            sid = "dddd1111-eeee-2222-ffff-333344445555"
            _write_session(projects_root, SRC, sid)
            _write_session(projects_root, DST, sid)  # collision
            r = ss.move_session(sid, SRC, DST)
            assert r["ok"] is False
            # Source must survive a refused move.
            assert (projects_root / SRC / f"{sid}.jsonl").exists()


# ---------------------------------------------------------------------------
# HTTP route
# ---------------------------------------------------------------------------

class TestMoveSessionRoute:
    def test_happy_path(self, client, projects_root):
        sid = "1111aaaa-2222-bbbb-3333-cccc4444dddd"
        _write_session(projects_root, SRC, sid, name="Ported")
        resp = client.post(
            f"/api/move-session/{sid}?project={SRC}",
            json={"from_project": SRC, "to_project": DST},
        )
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["ok"] is True
        assert data["to_project"] == DST
        assert not (projects_root / SRC / f"{sid}.jsonl").exists()
        assert (projects_root / DST / f"{sid}.jsonl").exists()
        # Source project tombstones the moved id so it can't reappear there.
        tomb = projects_root / SRC / "_deleted_sessions.json"
        assert tomb.exists()
        assert sid in json.loads(tomb.read_text())

    def test_move_overwrites_stale_target_name(self, client, projects_root):
        # Full-stack version of the production bug: the same id had a stale
        # "OpenAI"-style name in the target from a previous life. After moving
        # a source session named "AI industry report", the target must show
        # the source name, not the stale one.
        from app import session_store as ss
        sid = "9999aaaa-1111-bbbb-2222-cccc3333dddd"
        _write_session(projects_root, SRC, sid, name="AI industry report")
        _set_name(projects_root, DST, sid, "OpenAI")  # stale prior occupant
        resp = client.post(
            f"/api/move-session/{sid}",
            json={"from_project": SRC, "to_project": DST},
        )
        assert resp.status_code == 200
        assert ss._load_names(DST).get(sid) == "AI industry report"
        assert sid not in ss._load_names(SRC)

    def test_move_carries_jsonl_title_when_unnamed(self, client, projects_root):
        # Source has no names-file entry but does carry a custom-title inside
        # the .jsonl — that title must travel and replace a stale target name.
        from app import session_store as ss
        sid = "8888aaaa-1111-bbbb-2222-cccc3333dddd"
        _write_session(projects_root, SRC, sid, custom_title="Quarterly Plan")
        _set_name(projects_root, DST, sid, "Stale Name")
        resp = client.post(
            f"/api/move-session/{sid}",
            json={"from_project": SRC, "to_project": DST},
        )
        assert resp.status_code == 200
        assert ss._load_names(DST).get(sid) == "Quarterly Plan"

    def test_requires_to_project(self, client, projects_root):
        sid = "2222aaaa-3333-bbbb-4444-cccc5555dddd"
        _write_session(projects_root, SRC, sid)
        resp = client.post(f"/api/move-session/{sid}?project={SRC}", json={})
        assert resp.status_code == 400

    def test_same_project_rejected(self, client, projects_root):
        sid = "3333aaaa-4444-bbbb-5555-cccc6666dddd"
        _write_session(projects_root, SRC, sid)
        resp = client.post(
            f"/api/move-session/{sid}",
            json={"from_project": SRC, "to_project": SRC},
        )
        assert resp.status_code == 400

    def test_unknown_target_404(self, client, projects_root):
        sid = "4444aaaa-5555-bbbb-6666-cccc7777dddd"
        _write_session(projects_root, SRC, sid)
        resp = client.post(
            f"/api/move-session/{sid}",
            json={"from_project": SRC, "to_project": "C--Nope-Missing"},
        )
        assert resp.status_code == 404

    def test_traversal_rejected(self, client, projects_root):
        sid = "5555aaaa-6666-bbbb-7777-cccc8888dddd"
        _write_session(projects_root, SRC, sid)
        resp = client.post(
            f"/api/move-session/{sid}",
            json={"from_project": SRC, "to_project": "../evil"},
        )
        assert resp.status_code == 400

    def test_target_collision_409(self, client, projects_root):
        sid = "6666aaaa-7777-bbbb-8888-cccc9999dddd"
        _write_session(projects_root, SRC, sid)
        _write_session(projects_root, DST, sid)
        resp = client.post(
            f"/api/move-session/{sid}",
            json={"from_project": SRC, "to_project": DST},
        )
        assert resp.status_code == 409
        assert (projects_root / SRC / f"{sid}.jsonl").exists()
