"""
Regression tests for the soft-delete session trash (added 2026-05-29).

Background: session deletion used to permanently ``unlink()`` the .jsonl with
no undo — a single misclick destroyed a transcript forever.  ``api_delete``
now moves the transcript into a per-project ``_trash/`` folder, kept for 30
days, restorable in one step.  These tests pin that behavior so a future
refactor can't silently regress back to a hard delete.

Two layers:
  * ``TestTrashStore`` — unit tests of the store helpers in
    ``app.session_store`` against a monkeypatched temp sessions dir.  Fully
    isolated and deterministic (no real ~/.claude pollution).
  * ``TestTrashEndpoints`` — route-level tests through the Flask test client
    using the real active-project sessions dir, with snapshot-based cleanup
    of any ``_trash/`` artifacts (CLAUDE.md Compose fix #3 pattern — never
    name-prefix cleanup).
"""

import json
import time
import uuid
from pathlib import Path

import pytest

import app.session_store as ss


# ===========================================================================
# Layer 1 — store unit tests (isolated temp dir)
# ===========================================================================

@pytest.fixture
def sdir(tmp_path, monkeypatch):
    """Point every session_store path helper at a throwaway temp dir.

    All trash/tombstone/name helpers live in session_store and reference the
    module-global ``_sessions_dir``, so patching this one name isolates the
    whole store consistently.
    """
    d = tmp_path / "proj"
    d.mkdir()
    monkeypatch.setattr(ss, "_sessions_dir", lambda project="": d)
    return d


def _seed(sdir: Path, sid: str, text: str = "hello") -> Path:
    p = sdir / f"{sid}.jsonl"
    p.write_text(json.dumps({"type": "user", "text": text}) + "\n", encoding="utf-8")
    return p


class TestTrashStore:
    def test_move_relocates_file_into_trash(self, sdir):
        sid = str(uuid.uuid4())
        src = _seed(sdir, sid)
        assert ss.move_to_trash(sid, name="My Session") is True
        assert not src.exists(), "source .jsonl should be gone after trashing"
        assert (ss._trash_dir() / f"{sid}.jsonl").exists()

    def test_move_returns_false_when_no_file(self, sdir):
        assert ss.move_to_trash(str(uuid.uuid4()), name="ghost") is False

    def test_list_preserves_name_size_and_is_newest_first(self, sdir):
        first, second = str(uuid.uuid4()), str(uuid.uuid4())
        _seed(sdir, first, "a")
        ss.move_to_trash(first, name="First")
        # Stamp the first entry's deleted_at into the past so ordering is
        # deterministic without relying on wall-clock granularity.
        idx = ss._load_trash_index()
        idx[first]["deleted_at"] = time.time() - 100
        ss._save_trash_index(idx)
        _seed(sdir, second, "bb")
        ss.move_to_trash(second, name="Second")

        items = ss.list_trash()
        assert [it["id"] for it in items] == [second, first], "newest deleted first"
        names = {it["id"]: it["name"] for it in items}
        assert names[first] == "First" and names[second] == "Second"
        assert all(it["size"] > 0 for it in items)

    def test_list_empty_does_not_error_or_create_dir(self, sdir):
        # No session ever trashed — list must be empty and must NOT eagerly
        # create the _trash folder just because the view was opened.
        assert ss.list_trash() == []
        assert not ss._trash_dir().exists()

    def test_restore_brings_file_back_with_content_and_name(self, sdir):
        sid = str(uuid.uuid4())
        _seed(sdir, sid, "important work")
        ss.move_to_trash(sid, name="Keepme")
        name = ss.restore_from_trash(sid)
        assert name == "Keepme"
        restored = sdir / f"{sid}.jsonl"
        assert restored.exists()
        assert "important work" in restored.read_text(encoding="utf-8")
        assert ss.list_trash() == [], "restored entry should leave the trash index"
        assert not (ss._trash_dir() / f"{sid}.jsonl").exists()

    def test_restore_returns_none_when_absent(self, sdir):
        assert ss.restore_from_trash(str(uuid.uuid4())) is None

    def test_restore_refuses_to_clobber_a_live_file(self, sdir):
        sid = str(uuid.uuid4())
        _seed(sdir, sid, "trashed copy")
        ss.move_to_trash(sid, name="x")
        # A live session now occupies the same id slot.
        live = _seed(sdir, sid, "LIVE — do not overwrite")
        assert ss.restore_from_trash(sid) is None
        assert "LIVE" in live.read_text(encoding="utf-8")
        # The trashed copy stays parked (not lost) for manual handling.
        assert (ss._trash_dir() / f"{sid}.jsonl").exists()

    def test_purge_removes_file_and_index_entry(self, sdir):
        sid = str(uuid.uuid4())
        _seed(sdir, sid)
        ss.move_to_trash(sid, name="bye")
        assert ss.purge_from_trash(sid) is True
        assert not (ss._trash_dir() / f"{sid}.jsonl").exists()
        assert ss.list_trash() == []

    def test_purge_absent_returns_false(self, sdir):
        assert ss.purge_from_trash(str(uuid.uuid4())) is False

    def test_expired_entries_are_pruned(self, sdir, monkeypatch):
        sid = str(uuid.uuid4())
        _seed(sdir, sid)
        ss.move_to_trash(sid, name="old")
        # Backdate the entry well past the retention window.
        monkeypatch.setattr(ss, "_TRASH_MAX_AGE", 10)
        idx = ss._load_trash_index()
        idx[sid]["deleted_at"] = time.time() - 9999
        ss._save_trash_index(idx)
        # list_trash prunes as a side effect: entry gone, backing file removed.
        assert ss.list_trash() == []
        assert not (ss._trash_dir() / f"{sid}.jsonl").exists()

    def test_trashed_file_not_visible_to_session_glob(self, sdir):
        # all_sessions() globs "*.jsonl" non-recursively and skips "_"-prefixed
        # stems; a trashed file living under _trash/ must never match.
        sid = str(uuid.uuid4())
        _seed(sdir, sid)
        ss.move_to_trash(sid, name="hidden")
        top_level = {p.stem for p in sdir.glob("*.jsonl")}
        assert sid not in top_level

    def test_unmark_deleted_clears_tombstone(self, sdir):
        sid = str(uuid.uuid4())
        ss._mark_deleted(sid)
        assert sid in ss._get_deleted_ids()
        ss._unmark_deleted(sid)
        assert sid not in ss._get_deleted_ids()


# ===========================================================================
# Layer 2 — route wiring through the Flask test client
# ===========================================================================

from app import create_app  # noqa: E402
from app.config import _CLAUDE_PROJECTS, _sessions_dir  # noqa: E402


@pytest.fixture
def app():
    return create_app(testing=True)


@pytest.fixture
def client(app):
    with app.test_client() as c:
        yield c


@pytest.fixture(autouse=True)
def cleanup_sessions_dir():
    """Snapshot _CLAUDE_PROJECTS before each test and remove anything new
    afterwards — including any _trash/ subdirectory created by the test.
    Snapshot-diff cleanup per CLAUDE.md Compose fix #3 (never name-prefix)."""
    import shutil

    snapshot = {}
    if _CLAUDE_PROJECTS.is_dir():
        for d in _CLAUDE_PROJECTS.iterdir():
            if d.is_dir():
                snapshot[d.name] = {
                    "files": {f.name for f in d.iterdir() if f.is_file()},
                    "dirs": {f.name for f in d.iterdir() if f.is_dir()},
                }
    yield
    if not _CLAUDE_PROJECTS.is_dir():
        return
    for d in _CLAUDE_PROJECTS.iterdir():
        if not d.is_dir():
            continue
        before = snapshot.get(d.name)
        if before is None:
            shutil.rmtree(d, ignore_errors=True)
            continue
        for f in d.iterdir():
            if f.is_file() and f.name not in before["files"]:
                try:
                    f.unlink()
                except OSError:
                    pass
            elif f.is_dir() and f.name not in before["dirs"]:
                shutil.rmtree(f, ignore_errors=True)


class TestTrashEndpoints:
    def test_delete_routes_to_trash_then_restore_round_trips(self, client):
        proj_dir = _sessions_dir("")
        proj_dir.mkdir(parents=True, exist_ok=True)
        proj = proj_dir.name
        sid = str(uuid.uuid4())
        jsonl = proj_dir / f"{sid}.jsonl"
        jsonl.write_text(
            json.dumps({"type": "user", "sessionId": sid, "text": "route test"}) + "\n",
            encoding="utf-8",
        )
        # Give it a title so restore can prove the name survives.
        client.post(f"/api/rename/{sid}?project={proj}", json={"title": "Route Trash Test"})

        # Delete → soft delete (original gone, not in normal listing).
        r = client.delete(f"/api/delete/{sid}?project={proj}")
        assert r.status_code == 200 and r.get_json().get("ok") is True
        assert not jsonl.exists(), "delete must move the .jsonl out of the sessions dir"

        # It shows up in the trash listing with its title.
        r = client.get(f"/api/trash?project={proj}")
        body = r.get_json()
        assert body.get("ok") is True
        entry = next((e for e in body["trash"] if e["id"] == sid), None)
        assert entry is not None, "deleted session should appear in trash"
        assert entry["name"] == "Route Trash Test"

        # Restore → file comes back, tombstone cleared, removed from trash.
        r = client.post(f"/api/trash/{sid}/restore", json={"project": proj})
        assert r.status_code == 200 and r.get_json().get("ok") is True
        assert jsonl.exists(), "restore must return the .jsonl to the sessions dir"
        r = client.get(f"/api/trash?project={proj}")
        assert all(e["id"] != sid for e in r.get_json()["trash"])

    def test_restore_unknown_id_returns_404(self, client):
        proj = _sessions_dir("").name
        r = client.post(f"/api/trash/{uuid.uuid4()}/restore", json={"project": proj})
        assert r.status_code == 404

    def test_purge_endpoint_permanently_removes(self, client):
        proj_dir = _sessions_dir("")
        proj_dir.mkdir(parents=True, exist_ok=True)
        proj = proj_dir.name
        sid = str(uuid.uuid4())
        (proj_dir / f"{sid}.jsonl").write_text(
            json.dumps({"type": "user", "sessionId": sid, "text": "purge me"}) + "\n",
            encoding="utf-8",
        )
        client.delete(f"/api/delete/{sid}?project={proj}")
        r = client.delete(f"/api/trash/{sid}?project={proj}")
        assert r.status_code == 200 and r.get_json().get("ok") is True
        # Gone from trash and from disk entirely.
        r = client.get(f"/api/trash?project={proj}")
        assert all(e["id"] != sid for e in r.get_json()["trash"])
