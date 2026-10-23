"""
GET /api/sessions at scale: query parameters, the summary-cache leak fix,
the orphan-verdict cache, and the cached daemon-state lookup.

Project directories live under tmp_path; ``app.config._CLAUDE_PROJECTS`` is
patched there (the autouse conftest fixture already sandboxes Path.home()).
"""

import json
import os
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest


def _line(kind, text, ts, sid="s"):
    if kind == "custom-title":
        return json.dumps({"type": "custom-title", "customTitle": text, "sessionId": sid})
    return json.dumps({"type": kind, "message": {"role": kind, "content": text},
                       "timestamp": ts, "sessionId": sid})


def _iso(days_ago: float) -> str:
    return (datetime.now(timezone.utc) - timedelta(days=days_ago)).isoformat()


def _write(proj: Path, sid: str, lines) -> Path:
    p = proj / f"{sid}.jsonl"
    p.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return p


@pytest.fixture()
def projects_root(tmp_path, monkeypatch):
    root = tmp_path / "projects"
    root.mkdir()
    import app.config as cfg
    import app.routes.sessions_api as sa
    import app.sessions as sess
    monkeypatch.setattr(cfg, "_CLAUDE_PROJECTS", root)
    monkeypatch.setattr(sa, "_CLAUDE_PROJECTS", root)
    cfg._summary_cache.clear()
    cfg._summary_cache_index.clear()
    sess._orphan_cache.clear()
    return root


@pytest.fixture()
def project(projects_root):
    name = "C--Users-test-Documents-q"
    (projects_root / name).mkdir()
    return name


@pytest.fixture()
def app():
    from app import create_app
    return create_app(testing=True)


@pytest.fixture()
def seeded(projects_root, project):
    proj = projects_root / project
    _write(proj, "alpha", [_line("user", "Fix the login redirect bug", _iso(1), "alpha"),
                           _line("assistant", "ok", _iso(1), "alpha")])
    _write(proj, "beta", [_line("custom-title", "Billing export", "", "beta"),
                          _line("user", "Export invoices to CSV", _iso(10), "beta"),
                          _line("assistant", "ok", _iso(10), "beta")])
    _write(proj, "gamma", [_line("user", "Write release notes", _iso(40), "gamma"),
                           _line("assistant", "ok", _iso(40), "gamma")])
    _write(proj, "delta", [_line("user", "Refactor LOGIN form validation", _iso(3), "delta"),
                           _line("assistant", "ok", _iso(3), "delta")])
    return proj


def _get(app, project, **params):
    qs = "&".join(f"{k}={v}" for k, v in params.items())
    url = f"/api/sessions?project={project}" + (f"&{qs}" if qs else "")
    return app.test_client().get(url)


def _ids(resp):
    return [s["id"] for s in resp.get_json()]


# ---------------------------------------------------------------------------
# Query parameters
# ---------------------------------------------------------------------------

class TestQueryParams:
    def test_no_params_is_the_full_unfiltered_list(self, app, project, seeded):
        r = _get(app, project)
        assert r.status_code == 200
        assert isinstance(r.get_json(), list)
        assert set(_ids(r)) == {"alpha", "beta", "gamma", "delta"}
        assert "X-Total-Count" not in r.headers
        ts = [s["sort_ts"] for s in r.get_json()]
        assert ts == sorted(ts, reverse=True)

    def test_q_matches_title_custom_title_and_preview_case_insensitively(self, app, project, seeded):
        r = _get(app, project, q="login")
        assert set(_ids(r)) == {"alpha", "delta"}
        assert r.headers["X-Total-Count"] == "2"
        r = _get(app, project, q="BILLING")
        assert _ids(r) == ["beta"]
        r = _get(app, project, q="nothing-here")
        assert _ids(r) == [] and r.headers["X-Total-Count"] == "0"

    def test_since_days(self, app, project, seeded):
        assert set(_ids(_get(app, project, since=5))) == {"alpha", "delta"}
        assert set(_ids(_get(app, project, since=20))) == {"alpha", "delta", "beta"}
        assert set(_ids(_get(app, project, since=365))) == {"alpha", "beta", "gamma", "delta"}

    def test_limit_and_offset_page_the_filtered_list(self, app, project, seeded):
        full = _ids(_get(app, project))
        r = _get(app, project, limit=2)
        assert _ids(r) == full[:2]
        assert r.headers["X-Total-Count"] == "4"
        r = _get(app, project, limit=2, offset=2)
        assert _ids(r) == full[2:4]
        r = _get(app, project, offset=3)
        assert _ids(r) == full[3:]
        r = _get(app, project, q="login", limit=1)
        assert len(_ids(r)) == 1 and r.headers["X-Total-Count"] == "2"

    def test_state_filter_uses_live_daemon_state(self, app, project, seeded):
        app.session_manager.get_all_states.return_value = [
            {"session_id": "alpha", "state": "working", "cwd": ""},
            {"session_id": "beta", "state": "waiting", "cwd": ""},
            {"session_id": "gamma", "state": "idle", "cwd": ""},
            {"session_id": "delta", "state": "stopped", "cwd": ""},
            # A live session with no transcript yet is merged in as working.
            {"session_id": "live-only", "state": "starting", "cwd": ""},
        ]
        assert set(_ids(_get(app, project, state="working"))) == {"alpha", "live-only"}
        assert _ids(_get(app, project, state="waiting")) == ["beta"]
        assert _ids(_get(app, project, state="idle")) == ["gamma"]
        assert _ids(_get(app, project, state="sleeping")) == ["delta"]
        r = _get(app, project, state="working,waiting")
        assert set(_ids(r)) == {"alpha", "beta", "live-only"}
        assert r.headers["X-Total-Count"] == "3"

    def test_sessions_without_a_daemon_record_are_sleeping(self, app, project, seeded):
        assert set(_ids(_get(app, project, state="sleeping"))) == {"alpha", "beta", "gamma", "delta"}
        assert _ids(_get(app, project, state="working")) == []

    @pytest.mark.parametrize("params", [
        {"state": "napping"},
        {"state": "working,bogus"},
        {"since": "yesterday"},
        {"since": "-1"},
        {"limit": "ten"},
        {"limit": "-5"},
        {"offset": "x"},
    ])
    def test_invalid_values_are_400(self, app, project, seeded, params):
        r = _get(app, project, **params)
        assert r.status_code == 400
        assert "error" in r.get_json()


# ---------------------------------------------------------------------------
# Listing cost
# ---------------------------------------------------------------------------

class TestSummaryCache:
    def test_a_growing_transcript_keeps_one_cache_entry(self, projects_root, project):
        """Before the fix every turn left the previous (path, mtime, size) key
        behind for the life of the process."""
        import app.config as cfg
        from app.sessions import load_session_summary
        proj = projects_root / project
        p = _write(proj, "grow", [_line("user", "first", _iso(1), "grow")])
        load_session_summary(p)
        assert sum(1 for k in cfg._summary_cache if k[0] == str(p)) == 1
        for i in range(5):
            with open(p, "a", encoding="utf-8") as f:
                f.write(_line("assistant", f"reply {i} " + "x" * 50, _iso(1), "grow") + "\n")
            # Force a distinct mtime even on coarse filesystems.
            st = p.stat()
            os.utime(p, (st.st_atime, st.st_mtime + 1 + i))
            s = load_session_summary(p)
            assert s["message_count"] == i + 2
            assert sum(1 for k in cfg._summary_cache if k[0] == str(p)) == 1
        assert cfg._summary_cache_index[str(p)] in cfg._summary_cache

    def test_cache_clear_by_tests_does_not_break_the_index(self, projects_root, project):
        import app.config as cfg
        from app.sessions import load_session_summary
        proj = projects_root / project
        p = _write(proj, "x", [_line("user", "hello there", _iso(1), "x")])
        load_session_summary(p)
        cfg._summary_cache.clear()  # what existing tests do
        s = load_session_summary(p)
        assert s["id"] == "x"
        assert len([k for k in cfg._summary_cache if k[0] == str(p)]) == 1

    def test_warm_listing_reads_no_transcript(self, projects_root, project, monkeypatch):
        """Second listing of unchanged files: every summary is a cache hit and
        the orphan check is not re-run."""
        import app.sessions as sess
        from app.sessions import all_sessions
        proj = projects_root / project
        for i in range(12):
            _write(proj, f"s{i}", [_line("user", f"message {i} here", _iso(1), f"s{i}"),
                                   _line("assistant", "ok", _iso(1), f"s{i}")])
        _write(proj, "stub", [json.dumps({"type": "ai-title", "title": "x"})])
        first = all_sessions(summary_only=True, project=project)
        assert {s["id"] for s in first} == {f"s{i}" for i in range(12)}  # stub hidden
        orphan_calls = []
        monkeypatch.setattr(sess, "_is_aititle_only_orphan",
                            lambda p: orphan_calls.append(p) or False)
        opened = []
        real_open = open

        def spy_open(file, *a, **k):
            if str(file).endswith(".jsonl"):
                opened.append(str(file))
            return real_open(file, *a, **k)
        monkeypatch.setattr("builtins.open", spy_open)
        second = all_sessions(summary_only=True, project=project)
        assert [s["id"] for s in second] == [s["id"] for s in first]
        assert orphan_calls == []
        assert opened == []

    def test_a_changed_file_is_re_read_and_a_new_stub_is_hidden(self, projects_root, project):
        from app.sessions import all_sessions
        proj = projects_root / project
        p = _write(proj, "a", [_line("user", "one message", _iso(1), "a")])
        assert all_sessions(summary_only=True, project=project)[0]["message_count"] == 1
        with open(p, "a", encoding="utf-8") as f:
            f.write(_line("assistant", "two", _iso(1), "a") + "\n")
        st = p.stat()
        os.utime(p, (st.st_atime, st.st_mtime + 2))
        rows = all_sessions(summary_only=True, project=project)
        assert rows[0]["message_count"] == 2
        _write(proj, "stub", [json.dumps({"type": "ai-title", "title": "x"})])
        assert [s["id"] for s in all_sessions(summary_only=True, project=project)] == ["a"]

    def test_summaries_handed_out_are_copies(self, projects_root, project):
        from app.sessions import all_sessions
        proj = projects_root / project
        _write(proj, "a", [_line("user", "one message", _iso(1), "a")])
        rows = all_sessions(summary_only=True, project=project)
        rows[0]["last_state"] = "idle"
        again = all_sessions(summary_only=True, project=project)
        assert "last_state" not in again[0]


class TestSideFileCaches:
    def test_tombstone_utility_and_remap_files_are_reread_only_when_changed(self, projects_root, project, monkeypatch):
        from app import session_store as ss
        from app.session_store import (_get_deleted_ids, _get_utility_ids, _get_remapped_ids,
                                       _mark_deleted, _mark_utility, _mark_remapped)
        _mark_deleted("d1", project)
        _mark_utility("u1", project)
        _mark_remapped("old1", "new1", project)
        assert _get_deleted_ids(project) == {"d1"}
        assert _get_utility_ids(project) == {"u1"}
        assert _get_remapped_ids(project) == {"old1"}
        loads = []
        for name in ("_load_tombstones", "_load_utility", "_load_remaps"):
            real = getattr(ss, name)

            def wrapped(p="", _real=real, _n=name):
                loads.append(_n)
                return _real(p)
            monkeypatch.setattr(ss, name, wrapped)
        for _ in range(3):
            _get_deleted_ids(project)
            _get_utility_ids(project)
            _get_remapped_ids(project)
        assert loads == []
        # A write invalidates: the next read parses the file again and sees it.
        _mark_deleted("d2", project)  # the writer reads uncached under its lock
        loads.clear()
        assert _get_deleted_ids(project) == {"d1", "d2"}
        assert loads == ["_load_tombstones"]
        assert ss._resolve_remapped_id("old1", project) == "new1"

    def test_missing_side_files_yield_empty_sets(self, projects_root, project):
        from app.session_store import _get_deleted_ids, _get_utility_ids, _get_remapped_ids
        assert _get_deleted_ids(project) == set()
        assert _get_utility_ids(project) == set()
        assert _get_remapped_ids(project) == set()


class TestCachedDaemonStates:
    def test_production_path_uses_the_two_second_cache(self, app, project, seeded):
        """Outside testing mode the route goes through
        session_awareness._get_all_states_cached (PERF-CRITICAL #10)."""
        import app.session_awareness as aw
        aw._states_cache = None
        aw._states_cache_time = 0.0
        sm = app.session_manager
        sm.get_all_states.reset_mock()
        sm.get_all_states.return_value = []
        app.testing = False
        try:
            c = app.test_client()
            assert c.get(f"/api/sessions?project={project}").status_code == 200
            assert c.get(f"/api/sessions?project={project}").status_code == 200
            assert sm.get_all_states.call_count == 1
        finally:
            app.testing = True
            aw._states_cache = None
            aw._states_cache_time = 0.0

    def test_testing_path_bypasses_the_cache(self, app, project, seeded):
        sm = app.session_manager
        sm.get_all_states.reset_mock()
        c = app.test_client()
        c.get(f"/api/sessions?project={project}")
        c.get(f"/api/sessions?project={project}")
        assert sm.get_all_states.call_count == 2
