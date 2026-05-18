"""
Tests for POST /api/admin/scrub-phantoms.

Spec: docs/plans/phantom-sessions-fix-spec.md section 5.

The scrub algorithm must:
  - Build the live-session set as ``all_sessions(...) ∪ in-flight daemon states``
  - Add the protected set (``_get_utility_ids``) — utility sids must never
    be reported as phantoms.
  - Honour dry_run=true (default) — never mutate.
  - On dry_run=false, write a timestamped backup (no colons) before
    mutating, evict matching _summary_cache keys, leave the remapped
    sessions store alone.
  - Reject concurrent calls with HTTP 429.
"""

import json
import re
import threading
from unittest.mock import MagicMock, patch

import pytest


@pytest.fixture()
def admin_app(tmp_path, monkeypatch):
    """Flask app wired so admin scrub tests run against a tmp project dir.

    Patches every place ``_sessions_dir`` is resolved so the route sees
    our tmp project, and exposes the session_manager mock so tests can
    inject in-flight states.
    """
    from app import create_app

    application = create_app(testing=True)
    application.session_manager.has_session.return_value = False
    application.session_manager.get_all_states = MagicMock(return_value=[])

    proj = tmp_path / "projects" / "C--Users-test-Documents-myproj"
    proj.mkdir(parents=True)
    proj_name = proj.name

    # Patch every module that resolves _sessions_dir / _CLAUDE_PROJECTS
    # so all read+write hits land in tmp_path.
    patches = [
        patch("app.config._sessions_dir", return_value=proj),
        patch("app.config._CLAUDE_PROJECTS", proj.parent),
        patch("app.sessions._sessions_dir", return_value=proj),
        patch("app.session_store._sessions_dir", return_value=proj),
        patch("app.routes.admin_api._CLAUDE_PROJECTS", proj.parent),
    ]
    for p in patches:
        p.start()
    monkeypatch.setattr(
        "app.routes.admin_api._list_user_projects",
        lambda: [proj_name],
    )

    yield application, proj, proj_name

    for p in patches:
        p.stop()


@pytest.fixture()
def client(admin_app):
    application, _, _ = admin_app
    return application.test_client()


def _write_session(proj, sid):
    """Create a minimal .jsonl so all_sessions() reports this session as live."""
    line = json.dumps({"type": "user",
                       "message": {"role": "user", "content": "hi"},
                       "timestamp": "2026-05-15T10:00:00Z"})
    (proj / f"{sid}.jsonl").write_text(line + "\n", encoding="utf-8")


def _set_names(proj, names: dict):
    (proj / "_session_names.json").write_text(
        json.dumps(names), encoding="utf-8",
    )


def test_dry_run_reports_phantoms(client, admin_app):
    """With 5 real sessions and 5 phantom entries in the names file,
    dry-run must report 5 phantoms and not mutate the file."""
    _, proj, _ = admin_app
    real_sids = [f"real-{i}" for i in range(5)]
    phantom_sids = [f"phantom-{i}" for i in range(5)]
    for sid in real_sids:
        _write_session(proj, sid)
    names = {sid: f"Title {sid}" for sid in real_sids + phantom_sids}
    _set_names(proj, names)

    resp = client.post("/api/admin/scrub-phantoms",
                       json={"dry_run": True})
    assert resp.status_code == 200
    data = resp.get_json()
    assert data["ok"] is True
    assert data["dry_run"] is True
    assert data["total_removed"] == 5
    # Names file untouched
    after = json.loads((proj / "_session_names.json").read_text())
    assert after == names


def test_apply_removes_phantoms_with_backup(client, admin_app):
    """dry_run=false must remove phantom keys, write a backup, and
    evict matching summary-cache entries."""
    from app.config import _summary_cache

    _, proj, _ = admin_app
    real_sids = [f"real-{i}" for i in range(5)]
    phantom_sids = [f"phantom-{i}" for i in range(5)]
    for sid in real_sids:
        _write_session(proj, sid)
    names = {sid: f"T {sid}" for sid in real_sids + phantom_sids}
    _set_names(proj, names)

    # Plant some _summary_cache entries — those for phantoms must be
    # evicted; those for real sessions must survive.
    _summary_cache[(str(proj / "phantom-0.jsonl"), 0.0, 0)] = {"id": "phantom-0"}
    _summary_cache[(str(proj / "real-0.jsonl"), 0.0, 0)] = {"id": "real-0"}

    resp = client.post("/api/admin/scrub-phantoms",
                       json={"dry_run": False})
    assert resp.status_code == 200
    data = resp.get_json()
    assert data["ok"] is True
    assert data["dry_run"] is False
    assert data["total_removed"] == 5

    # Names file no longer carries the phantoms.
    after = json.loads((proj / "_session_names.json").read_text())
    assert set(after.keys()) == set(real_sids)

    # Backup exists and matches the original.
    backups = list(proj.glob("_session_names.json.backup-*"))
    assert len(backups) == 1
    backup_data = json.loads(backups[0].read_text())
    assert backup_data == names

    # Summary cache: phantom entry gone, real entry kept.
    cache_keys = list(_summary_cache.keys())
    assert not any("phantom-0.jsonl" in k[0] for k in cache_keys)
    assert any("real-0.jsonl" in k[0] for k in cache_keys)
    # Cleanup any test pollution
    for k in [k for k in _summary_cache if k[0].endswith(".jsonl")]:
        _summary_cache.pop(k, None)


def test_apply_ignores_utility_ids(client, admin_app):
    """A sid that lives in _utility_sessions.json but has no .jsonl
    must NOT be reported as a phantom — it's a tracked utility, not
    junk."""
    _, proj, _ = admin_app
    util_sid = "_title_aabbccdd"
    # _utility_sessions.json schema: {sid: timestamp}
    import time as _t
    (proj / "_utility_sessions.json").write_text(
        json.dumps({util_sid: _t.time()}), encoding="utf-8",
    )
    _set_names(proj, {util_sid: "Some title that shouldn't matter"})

    resp = client.post("/api/admin/scrub-phantoms",
                       json={"dry_run": True})
    assert resp.status_code == 200
    data = resp.get_json()
    assert data["total_removed"] == 0


def test_apply_ignores_in_flight_sdk_session(client, admin_app):
    """A sid present in ``sm.get_all_states()`` but with no .jsonl yet
    must NOT be reported as a phantom — its file hasn't flushed."""
    application, proj, proj_name = admin_app
    in_flight_sid = "freshly-spawned-sid"
    _set_names(proj, {in_flight_sid: "In Flight"})

    # The route only counts states whose cwd belongs to *this* project.
    # We patch cwd_matches_active_project to True for our case.
    application.session_manager.get_all_states = MagicMock(return_value=[
        {"session_id": in_flight_sid, "cwd": "/whatever",
         "state": "idle"}
    ])
    with patch("app.routes.admin_api.cwd_matches_active_project",
               return_value=True):
        resp = client.post("/api/admin/scrub-phantoms",
                           json={"dry_run": True})

    assert resp.status_code == 200
    data = resp.get_json()
    assert data["total_removed"] == 0


def test_scrub_backup_filename_format(client, admin_app):
    """Backup filename must match
    ``_session_names.json.backup-\\d{8}-\\d{6}`` and contain no colons
    (Windows refuses colons in filenames)."""
    _, proj, _ = admin_app
    _set_names(proj, {"phantom-x": "Junk"})

    resp = client.post("/api/admin/scrub-phantoms",
                       json={"dry_run": False})
    assert resp.status_code == 200
    backups = list(proj.glob("_session_names.json.backup-*"))
    assert len(backups) == 1
    name = backups[0].name
    assert ":" not in name
    assert re.match(r"^_session_names\.json\.backup-\d{8}-\d{6}$", name), \
        f"Unexpected backup filename: {name!r}"


def test_concurrent_scrub_returns_429(client, admin_app):
    """A second concurrent call must return HTTP 429."""
    from app.routes.admin_api import _scrub_lock
    _, proj, _ = admin_app
    _set_names(proj, {"phantom-y": "junk"})

    # Acquire the lock manually to simulate an in-flight scrub.
    assert _scrub_lock.acquire(blocking=False)
    try:
        resp = client.post("/api/admin/scrub-phantoms",
                           json={"dry_run": True})
        assert resp.status_code == 429
        assert resp.get_json()["ok"] is False
    finally:
        _scrub_lock.release()


def test_unknown_project_returns_400(client, admin_app):
    resp = client.post("/api/admin/scrub-phantoms",
                       json={"project": "does-not-exist"})
    assert resp.status_code == 400
    data = resp.get_json()
    assert data["ok"] is False
    assert "Unknown project" in data["error"]


def test_empty_names_file_returns_zero(client, admin_app):
    """When _session_names.json is missing, the scrub must succeed
    with total_removed=0."""
    resp = client.post("/api/admin/scrub-phantoms",
                       json={"dry_run": True})
    assert resp.status_code == 200
    assert resp.get_json()["total_removed"] == 0


def test_no_remap_file_mutation(client, admin_app):
    """The scrub must not mutate ``_remapped_sessions.json`` — its TTL
    handles itself."""
    _, proj, _ = admin_app
    _write_session(proj, "real-1")
    _set_names(proj, {"real-1": "T", "phantom-1": "Junk"})
    # Plant a remap file and snapshot it.
    remap_data = {"old-sid": {"new_id": "new-sid", "ts": 100.0}}
    remap_path = proj / "_remapped_sessions.json"
    remap_path.write_text(json.dumps(remap_data), encoding="utf-8")
    before = remap_path.read_text(encoding="utf-8")

    resp = client.post("/api/admin/scrub-phantoms",
                       json={"dry_run": False})
    assert resp.status_code == 200
    after = remap_path.read_text(encoding="utf-8")
    assert before == after, "Scrub mutated _remapped_sessions.json"
