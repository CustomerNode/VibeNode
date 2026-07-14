"""
Tests for the session-transcript search feature.

Covers ``app/search_index.py`` (FTS5 index build, incremental updates,
query-time exclusions, defensive query handling, corrupted-DB recovery)
and ``app/routes/search_api.py`` (param validation, project resolution,
error mapping).

Isolation: the autouse ``_isolate_daemon_home`` fixture in conftest.py
redirects ``Path.home()`` to a per-test tmp dir, and ``search_index``
computes its DB path lazily from ``Path.home()`` — so every test gets a
fresh, sandboxed index DB with zero risk of touching the user's real
``~/.claude/gui_search_index.db``.
"""

import json
import time

import pytest

PROJECT = "C--Users-test-searchproj"


# ---------------------------------------------------------------------------
# JSONL builders — mirror the real transcript entry shapes
# ---------------------------------------------------------------------------

def _msg(role, content, ts="2026-06-01T10:00:00Z"):
    """A user/assistant entry.  *content* may be a string or a block list."""
    return {"type": role, "timestamp": ts, "message": {"content": content}}


def _edit_block(file_path, tool="Edit"):
    """A tool_use block for an edit tool touching *file_path*."""
    return {"type": "tool_use", "name": tool, "input": {"file_path": file_path}}


def _write_session(proj_dir, sid, entries):
    """Write *entries* (dicts or raw strings) as a session JSONL."""
    lines = [e if isinstance(e, str) else json.dumps(e) for e in entries]
    p = proj_dir / f"{sid}.jsonl"
    p.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return p


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def search_env():
    """Fake project dir + Flask test client + reset module debounce state."""
    import app.config as config_mod
    import app.search_index as si
    from app import create_app

    proj_dir = config_mod._CLAUDE_PROJECTS / PROJECT
    proj_dir.mkdir(parents=True, exist_ok=True)
    si._last_ensure.clear()  # no TTL leakage across tests

    application = create_app(testing=True)
    with application.test_client() as client:
        yield proj_dir, client, si
    si._last_ensure.clear()


def _seed_two_sessions(proj_dir):
    """Two sessions with distinct content; returns their ids."""
    _write_session(proj_dir, "sess-alpha", [
        _msg("user", "How should we handle websocket retry logic?"),
        _msg("assistant", [
            {"type": "text", "text": "We decided to use exponential backoff for retries."},
            _edit_block("C:\\Users\\test\\proj\\app\\retry.py"),
        ]),
    ])
    _write_session(proj_dir, "sess-beta", [
        _msg("user", "Fix the kanban drag bug"),
        _msg("assistant", [
            {"type": "text", "text": "Patched the drop handler."},
            _edit_block("C:/Users/test/proj/static/js/kanban.js", tool="Write"),
        ]),
        _msg("user", [
            {"type": "tool_result",
             "content": "Traceback (most recent call last): ZeroDivisionError: boom"},
        ]),
    ])
    return "sess-alpha", "sess-beta"


# ---------------------------------------------------------------------------
# Content search
# ---------------------------------------------------------------------------

class TestContentSearch:
    def test_basic_match_returns_right_session_with_snippet(self, search_env):
        proj_dir, client, si = search_env
        _seed_two_sessions(proj_dir)
        resp = client.get(f"/api/search?q=exponential+backoff&project={PROJECT}")
        assert resp.status_code == 200
        data = resp.get_json()
        sids = [s["session_id"] for s in data["sessions"]]
        assert sids == ["sess-alpha"]
        snip = data["sessions"][0]["snippets"][0]["text"]
        assert "[[HIT]]" in snip and "[[/HIT]]" in snip

    def test_string_and_block_content_shapes_both_indexed(self, search_env):
        proj_dir, client, si = search_env
        _seed_two_sessions(proj_dir)
        # string-form content ("websocket") and block-form ("backoff")
        for q, expect in (("websocket", "sess-alpha"), ("backoff", "sess-alpha")):
            data = client.get(f"/api/search?q={q}&project={PROJECT}").get_json()
            assert [s["session_id"] for s in data["sessions"]] == [expect], q

    def test_tool_result_content_is_searchable(self, search_env):
        """'show me every session that hit this error' — errors live in tool output."""
        proj_dir, client, si = search_env
        _seed_two_sessions(proj_dir)
        data = client.get(f"/api/search?q=ZeroDivisionError&project={PROJECT}").get_json()
        assert [s["session_id"] for s in data["sessions"]] == ["sess-beta"]

    def test_prefix_match_on_last_token(self, search_env):
        proj_dir, client, si = search_env
        _seed_two_sessions(proj_dir)
        data = client.get(f"/api/search?q=exponen&project={PROJECT}").get_json()
        assert [s["session_id"] for s in data["sessions"]] == ["sess-alpha"]

    def test_no_matches_returns_empty_list(self, search_env):
        proj_dir, client, si = search_env
        _seed_two_sessions(proj_dir)
        data = client.get(f"/api/search?q=quetzalcoatl&project={PROJECT}").get_json()
        assert data["sessions"] == []
        assert data["stats"]["messages_indexed"] > 0

    def test_corrupt_jsonl_line_skipped(self, search_env):
        proj_dir, client, si = search_env
        _write_session(proj_dir, "sess-corrupt", [
            '{"type": "user", "message": {"content": "before the corruption"}, "timestamp": "2026-06-01T10:00:00Z"}',
            '{"type": "user", "mess',  # truncated mid-write
            _msg("assistant", "after the corruption we recovered fine"),
        ])
        data = client.get(f"/api/search?q=recovered&project={PROJECT}").get_json()
        assert [s["session_id"] for s in data["sessions"]] == ["sess-corrupt"]


# ---------------------------------------------------------------------------
# Touched-file lookup
# ---------------------------------------------------------------------------

class TestFileFilter:
    def test_file_lookup_finds_editing_session(self, search_env):
        proj_dir, client, si = search_env
        _seed_two_sessions(proj_dir)
        data = client.get(f"/api/search?file=retry.py&project={PROJECT}").get_json()
        assert [s["session_id"] for s in data["sessions"]] == ["sess-alpha"]
        assert any("retry.py" in f for f in data["sessions"][0]["files"])

    def test_file_lookup_is_case_and_slash_insensitive(self, search_env):
        proj_dir, client, si = search_env
        _seed_two_sessions(proj_dir)
        # stored with backslashes + lowercase; query with fwd slash + mixed case
        data = client.get(
            f"/api/search?file=app/Retry.PY&project={PROJECT}").get_json()
        assert [s["session_id"] for s in data["sessions"]] == ["sess-alpha"]

    def test_combined_q_and_file_intersects(self, search_env):
        proj_dir, client, si = search_env
        _seed_two_sessions(proj_dir)
        # "the" appears in both sessions; file narrows to kanban.js's session
        data = client.get(
            f"/api/search?q=the&file=kanban.js&project={PROJECT}").get_json()
        assert [s["session_id"] for s in data["sessions"]] == ["sess-beta"]

    def test_like_wildcards_in_file_filter_are_literal(self, search_env):
        proj_dir, client, si = search_env
        _seed_two_sessions(proj_dir)
        data = client.get(f"/api/search?file=%25&project={PROJECT}").get_json()
        assert data["sessions"] == []  # '%' must not act as a wildcard


# ---------------------------------------------------------------------------
# Incremental indexing
# ---------------------------------------------------------------------------

class TestIncrementalIndex:
    def test_changed_file_is_reindexed(self, search_env):
        proj_dir, client, si = search_env
        p = _write_session(proj_dir, "sess-grow", [_msg("user", "original content here")])
        client.get(f"/api/search?q=original&project={PROJECT}")
        # Append a new message (size changes → signature changes)
        with open(p, "a", encoding="utf-8") as f:
            f.write(json.dumps(_msg("assistant", "freshly appended zanzibar")) + "\n")
        si.ensure_index(PROJECT, force=True)  # bypass the 20s TTL
        data = client.get(f"/api/search?q=zanzibar&project={PROJECT}").get_json()
        assert [s["session_id"] for s in data["sessions"]] == ["sess-grow"]

    def test_deleted_file_rows_removed(self, search_env):
        proj_dir, client, si = search_env
        p = _write_session(proj_dir, "sess-gone", [_msg("user", "ephemeral xylophone")])
        client.get(f"/api/search?q=xylophone&project={PROJECT}")
        p.unlink()
        si.ensure_index(PROJECT, force=True)
        data = client.get(f"/api/search?q=xylophone&project={PROJECT}").get_json()
        assert data["sessions"] == []
        assert data["stats"]["sessions_indexed"] == 0

    def test_ttl_debounce_short_circuits(self, search_env):
        proj_dir, client, si = search_env
        _write_session(proj_dir, "sess-ttl", [_msg("user", "debounce test")])
        calls = []
        real = si._ensure_index_locked
        si._ensure_index_locked = lambda proj: calls.append(proj) or real(proj)
        try:
            si.ensure_index(PROJECT)
            si.ensure_index(PROJECT)          # within TTL → skipped
            assert len(calls) == 1
            si.ensure_index(PROJECT, force=True)  # force bypasses TTL
            assert len(calls) == 2
        finally:
            si._ensure_index_locked = real

    def test_underscore_metadata_files_not_indexed(self, search_env):
        proj_dir, client, si = search_env
        (proj_dir / "_session_names.json").write_text("{}", encoding="utf-8")
        _write_session(proj_dir, "_not_a_session", [_msg("user", "metadata sentinel")])
        _write_session(proj_dir, "sess-real", [_msg("user", "real sentinel")])
        data = client.get(f"/api/search?q=sentinel&project={PROJECT}").get_json()
        assert [s["session_id"] for s in data["sessions"]] == ["sess-real"]


# ---------------------------------------------------------------------------
# Query-time exclusions
# ---------------------------------------------------------------------------

class TestExclusions:
    def test_tombstoned_session_hidden_immediately(self, search_env):
        """Tombstones change WITHOUT touching the JSONL — exclusion must be
        query-time, not index-time."""
        proj_dir, client, si = search_env
        _seed_two_sessions(proj_dir)
        client.get(f"/api/search?q=backoff&project={PROJECT}")  # index both
        (proj_dir / "_deleted_sessions.json").write_text(
            json.dumps({"sess-alpha": time.time()}), encoding="utf-8")
        data = client.get(f"/api/search?q=backoff&project={PROJECT}").get_json()
        assert data["sessions"] == []
        # ...and the file-filter path applies the same exclusion
        data = client.get(f"/api/search?file=retry.py&project={PROJECT}").get_json()
        assert data["sessions"] == []


# ---------------------------------------------------------------------------
# Param validation & error handling
# ---------------------------------------------------------------------------

class TestValidationAndErrors:
    def test_missing_q_and_file_is_400(self, search_env):
        _, client, _ = search_env
        resp = client.get(f"/api/search?project={PROJECT}")
        assert resp.status_code == 400
        assert "error" in resp.get_json()

    def test_one_char_query_is_400(self, search_env):
        _, client, _ = search_env
        resp = client.get(f"/api/search?q=x&project={PROJECT}")
        assert resp.status_code == 400

    def test_punctuation_only_query_is_400_not_500(self, search_env):
        proj_dir, client, si = search_env
        _seed_two_sessions(proj_dir)
        resp = client.get(f"/api/search?q=%22%22%22&project={PROJECT}")  # q='"""'
        assert resp.status_code == 400
        assert "error" in resp.get_json()

    def test_fts_syntax_chars_are_neutralized(self, search_env):
        proj_dir, client, si = search_env
        _seed_two_sessions(proj_dir)
        # NEAR/AND/parens/quotes must be treated as literals, never syntax
        for q in ('backoff AND retry', 'NEAR(a b)', '(backoff)', 'back"off'):
            resp = client.get(f"/api/search?q={q}&project={PROJECT}")
            assert resp.status_code in (200, 400), q  # never 500
            assert "error" not in (resp.get_json() or {}) or resp.status_code == 400

    def test_nonexistent_project_returns_empty_200(self, search_env):
        _, client, _ = search_env
        resp = client.get("/api/search?q=anything&project=C--No-Such-Project")
        assert resp.status_code == 200
        assert resp.get_json()["sessions"] == []

    def test_invalid_limit_falls_back_to_default(self, search_env):
        proj_dir, client, si = search_env
        _seed_two_sessions(proj_dir)
        resp = client.get(f"/api/search?q=backoff&limit=banana&project={PROJECT}")
        assert resp.status_code == 200


# ---------------------------------------------------------------------------
# Resilience
# ---------------------------------------------------------------------------

class TestResilience:
    def test_corrupted_index_db_auto_rebuilds(self, search_env):
        proj_dir, client, si = search_env
        _seed_two_sessions(proj_dir)
        db = si._index_db_path()
        db.parent.mkdir(parents=True, exist_ok=True)
        db.write_bytes(b"this is definitely not a sqlite database")
        resp = client.get(f"/api/search?q=backoff&project={PROJECT}")
        assert resp.status_code == 200
        assert [s["session_id"] for s in resp.get_json()["sessions"]] == ["sess-alpha"]

    def test_long_tool_result_capped_but_head_searchable(self, search_env):
        proj_dir, client, si = search_env
        big = "ERROR: catastrophic flurble failure " + ("x" * 20000) + " TAILMARKER"
        _write_session(proj_dir, "sess-big", [
            _msg("user", [{"type": "tool_result", "content": big}]),
        ])
        data = client.get(f"/api/search?q=flurble&project={PROJECT}").get_json()
        assert [s["session_id"] for s in data["sessions"]] == ["sess-big"]
        # beyond the 4000-char cap is not indexed
        data = client.get(f"/api/search?q=TAILMARKER&project={PROJECT}").get_json()
        assert data["sessions"] == []


class TestReviewHardening:
    """Cases added from the Review Team pass (utility/remap exclusion,
    caps, project fallback, traversal guard, query-time corruption)."""

    def test_utility_and_remapped_sessions_excluded(self, search_env):
        proj_dir, client, si = search_env
        _write_session(proj_dir, "sess-util", [_msg("user", "sharedword utility")])
        _write_session(proj_dir, "sess-remap", [_msg("user", "sharedword remapped")])
        _write_session(proj_dir, "sess-keep", [_msg("user", "sharedword keeper")])
        (proj_dir / "_utility_sessions.json").write_text(
            json.dumps({"sess-util": time.time()}), encoding="utf-8")
        (proj_dir / "_remapped_sessions.json").write_text(
            json.dumps({"sess-remap": {"new_id": "sess-keep", "ts": time.time()}}),
            encoding="utf-8")
        data = client.get(f"/api/search?q=sharedword&project={PROJECT}").get_json()
        assert [s["session_id"] for s in data["sessions"]] == ["sess-keep"]

    def test_snippets_capped_at_three_per_session(self, search_env):
        proj_dir, client, si = search_env
        _write_session(proj_dir, "sess-many", [
            _msg("user", f"pangolin sighting number {i}") for i in range(6)
        ])
        data = client.get(f"/api/search?q=pangolin&project={PROJECT}").get_json()
        assert len(data["sessions"]) == 1
        assert len(data["sessions"][0]["snippets"]) == 3

    def test_limit_caps_session_count(self, search_env):
        proj_dir, client, si = search_env
        for i in range(3):
            _write_session(proj_dir, f"sess-lim-{i}", [_msg("user", f"wombat {i}")])
        data = client.get(f"/api/search?q=wombat&limit=1&project={PROJECT}").get_json()
        assert len(data["sessions"]) == 1

    def test_combined_query_includes_matched_files(self, search_env):
        proj_dir, client, si = search_env
        _seed_two_sessions(proj_dir)
        data = client.get(
            f"/api/search?q=drop+handler&file=kanban.js&project={PROJECT}").get_json()
        assert len(data["sessions"]) == 1
        assert any("kanban.js" in f for f in data["sessions"][0]["files"])

    def test_project_defaults_to_active_project(self, search_env, monkeypatch):
        proj_dir, client, si = search_env
        _write_session(proj_dir, "sess-active", [_msg("user", "capybara default")])
        monkeypatch.setattr(
            "app.routes.search_api.get_active_project", lambda: PROJECT)
        data = client.get("/api/search?q=capybara").get_json()
        assert [s["session_id"] for s in data["sessions"]] == ["sess-active"]

    def test_path_traversal_project_rejected(self, search_env):
        _, client, _ = search_env
        for bad in ("..%2F..%2Foutside", ".." + chr(92) + "evil", "a/b", ".."):
            resp = client.get(f"/api/search?q=anything&project={bad}")
            assert resp.status_code == 400, bad
            assert resp.get_json()["error"] == "invalid project name"

    def test_corruption_at_query_time_recovers(self, search_env):
        """ensure_index() on an unchanged project never touches FTS pages,
        so search() itself must recover when they are corrupt."""
        proj_dir, client, si = search_env
        _seed_two_sessions(proj_dir)
        client.get(f"/api/search?q=backoff&project={PROJECT}")  # build index
        # Corrupt the DB while its (mtime,size) bookkeeping stays "unchanged"
        # from ensure_index's point of view: overwrite with garbage.
        si._index_db_path().write_bytes(b"garbage " * 64)
        # TTL still fresh -> ensure_index() short-circuits entirely; only the
        # search() recovery wrapper can save this request.
        resp = client.get(f"/api/search?q=backoff&project={PROJECT}")
        assert resp.status_code == 200
        assert [s["session_id"] for s in resp.get_json()["sessions"]] == ["sess-alpha"]
