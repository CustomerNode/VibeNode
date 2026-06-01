"""Regression tests for the sidebar date sort fix.

The sidebar's date column sorts by ``effective_ts`` =
``max(last_user_assistant_msg_ts, last_access_ts)`` so user interactions
bubble a session to the top — without being polluted by SDK background
writes that bump file mtime without representing real activity.

Two clean signals feed effective_ts:

  * **last_activity_ts**: timestamp of the last user/assistant message
    inside the .jsonl.  Real conversation moves this forward.

  * **access_ts**: server-recorded UI interactions (click, send,
    rename, /touch).  Overlaid on every load_session_summary call.

Failure modes these tests guard against:

  1. ``effective_ts`` field missing from the session summary — frontend
     sort silently falls back to ``last_activity_ts`` and rename
     interactions stop bubbling.

  2. ``effective_ts`` reflecting file mtime — the Claude SDK appends
     untimestamped state entries (ai-title, mode, last-prompt,
     file-history-snapshot) in the background, bumping mtime without
     real activity.  One CustomerNode incident clustered 7 sessions at
     the same mtime minute and pushed an actually-recent conversation
     9 positions down.  effective_ts MUST NOT max with mtime.

  3. Access timestamp not overlaid on cache hits — the body-content
     cache key is ``(path, mtime, size)`` so a click that doesn't change
     the file would never reflect in the cached summary.

  4. ``custom-title`` append dedup missing — every rename/autoname call
     piles another identical line onto the .jsonl (one production Aras
     session had 52 duplicates of the same title).

  5. ``/api/session/<id>/touch`` endpoint missing or not recording — the
     belt-and-suspenders signal from JS sidebar clicks goes nowhere.

  6. GET ``/api/session/<id>`` recording access on ``meta_only=1``
     requests — would mean every background polling widget bumps every
     session's access_ts on every page render.
"""

import json
import time
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

import pytest


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _msg_line(role, content, ts, session_id="test"):
    return json.dumps({
        "type": role,
        "message": {"role": role, "content": content},
        "timestamp": ts,
        "sessionId": session_id,
    })


def _custom_title_line(title, session_id="test"):
    return json.dumps({
        "type": "custom-title",
        "customTitle": title,
        "sessionId": session_id,
    })


def _write_session(path, lines):
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


# ---------------------------------------------------------------------------
# Section 1 — effective_ts field in session summaries
# ---------------------------------------------------------------------------

class TestEffectiveTsField:
    """``effective_ts = max(last_msg_ts, file_mtime)`` in load_session_summary.

    The sidebar sort keys on this field; without it the frontend silently
    falls back to ``last_activity_ts`` and renames/autoname appends never
    bubble the session up.
    """

    def test_effective_ts_present_in_summary(self, tmp_path):
        from app.sessions import load_session_summary, _summary_cache
        _summary_cache.clear()
        path = tmp_path / "sess.jsonl"
        _write_session(path, [
            _msg_line("user", "hi", "2026-03-01T10:00:00Z"),
            _msg_line("assistant", "yo", "2026-03-01T10:00:05Z"),
        ])
        result = load_session_summary(path)
        assert "effective_ts" in result
        assert isinstance(result["effective_ts"], (int, float))

    def test_effective_ts_present_in_full_load(self, tmp_path):
        from app.sessions import load_session, _summary_cache
        _summary_cache.clear()
        path = tmp_path / "sess.jsonl"
        _write_session(path, [
            _msg_line("user", "hi", "2026-03-01T10:00:00Z"),
            _msg_line("assistant", "yo", "2026-03-01T10:00:05Z"),
        ])
        result = load_session(path)
        assert "effective_ts" in result

    def test_effective_ts_equals_last_activity_ts_without_access(
        self, tmp_path, monkeypatch,
    ):
        """No access entry → effective_ts is exactly last_activity_ts."""
        monkeypatch.setattr(
            "app.sessions._load_session_access_cached", lambda project: {},
        )
        from app.sessions import load_session_summary, _summary_cache
        _summary_cache.clear()
        path = tmp_path / "sess.jsonl"
        _write_session(path, [
            _msg_line("user", "hi", "2026-03-01T10:00:00Z"),
            _msg_line("assistant", "yo", "2026-03-01T10:00:05Z"),
        ])
        result = load_session_summary(path)
        assert result["effective_ts"] == result["last_activity_ts"]

    def test_effective_ts_ignores_file_mtime(self, tmp_path, monkeypatch):
        """Regression for the CustomerNode 06-01 incident: the Claude SDK
        appends untimestamped entries (ai-title, mode, last-prompt) to
        session files in the background, bumping mtime without any user
        interaction.  effective_ts MUST NOT max with mtime, or those
        background writes pollute the sort and push real-activity
        sessions down."""
        monkeypatch.setattr(
            "app.sessions._load_session_access_cached", lambda project: {},
        )
        from app.sessions import load_session_summary, _summary_cache
        _summary_cache.clear()
        path = tmp_path / "sess.jsonl"
        # Last real message ts is far in the past
        _write_session(path, [
            _msg_line("user", "old", "2026-03-01T10:00:00Z"),
            _msg_line("assistant", "old", "2026-03-01T10:00:05Z"),
        ])
        # Simulate SDK background write — append a state entry that bumps
        # mtime to ~now without adding any user/assistant message
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps({"type": "ai-title", "aiTitle": "auto"}) + "\n")
        # mtime is "now"; last user/asst message is still 2026-03-01
        assert path.stat().st_mtime - time.time() < 5
        result = load_session_summary(path)
        # effective_ts must reflect ONLY the message timestamp, not mtime
        assert result["effective_ts"] == result["last_activity_ts"]
        assert path.stat().st_mtime - result["effective_ts"] > 86400  # >1 day gap

    def test_last_activity_ts_still_means_last_message(self, tmp_path):
        """Renaming must not change last_activity_ts — display code uses
        it for the human-readable 'last conversation' date column."""
        from app.sessions import load_session_summary, _summary_cache
        _summary_cache.clear()
        path = tmp_path / "sess.jsonl"
        # Write messages with a known timestamp, then touch mtime
        _write_session(path, [
            _msg_line("user", "hi", "2026-03-01T10:00:00Z"),
            _msg_line("assistant", "yo", "2026-03-01T10:00:05Z"),
        ])
        result = load_session_summary(path)
        expected_last_msg = datetime.fromisoformat(
            "2026-03-01T10:00:05+00:00"
        ).timestamp()
        assert abs(result["last_activity_ts"] - expected_last_msg) < 1.0

    def test_error_path_returns_effective_ts_zero(self, tmp_path):
        """When the file is missing the err dict must still include the
        field so the frontend sort comparator doesn't see undefined."""
        from app.sessions import load_session_summary
        result = load_session_summary(tmp_path / "nope.jsonl")
        assert result.get("effective_ts", "MISSING") == 0


# ---------------------------------------------------------------------------
# Section 2 — access_ts overlay
# ---------------------------------------------------------------------------

class TestAccessOverlay:
    """``_overlay_access_ts`` bumps ``effective_ts`` when the per-project
    access map has a newer timestamp for this session.  Must apply on
    BOTH cache hits and cache misses — the file-body cache is keyed on
    ``(path, mtime, size)`` so access changes don't invalidate it."""

    def test_access_ts_bumps_effective_ts_on_cache_miss(self, tmp_path,
                                                        monkeypatch):
        from app.sessions import load_session_summary, _summary_cache
        _summary_cache.clear()
        # Create a session file in a project-like directory
        proj = tmp_path / "projects" / "test-proj"
        proj.mkdir(parents=True)
        sid = "sess-a"
        path = proj / f"{sid}.jsonl"
        _write_session(path, [
            _msg_line("user", "old", "2026-03-01T10:00:00Z"),
            _msg_line("assistant", "old", "2026-03-01T10:00:05Z"),
        ])
        future_ts = time.time() + 86400  # one day from now
        # Make the access lookup return a far-future access timestamp
        monkeypatch.setattr(
            "app.sessions._load_session_access_cached",
            lambda project: {sid: future_ts},
        )
        result = load_session_summary(path)
        assert result["effective_ts"] == future_ts

    def test_access_ts_bumps_effective_ts_on_cache_hit(self, tmp_path,
                                                       monkeypatch):
        """The body cache returns a stale dict; the access overlay must
        run AFTER the cache lookup so subsequent clicks bubble immediately."""
        from app.sessions import load_session_summary, _summary_cache
        _summary_cache.clear()
        proj = tmp_path / "projects" / "test-proj"
        proj.mkdir(parents=True)
        sid = "sess-b"
        path = proj / f"{sid}.jsonl"
        _write_session(path, [
            _msg_line("user", "old", "2026-03-01T10:00:00Z"),
            _msg_line("assistant", "old", "2026-03-01T10:00:05Z"),
        ])
        # First call with no access entry — populates the body cache
        monkeypatch.setattr(
            "app.sessions._load_session_access_cached",
            lambda project: {},
        )
        first = load_session_summary(path)
        first_eff = first["effective_ts"]

        # Now simulate a click: access map has a fresh ts.  Cache key
        # (path, mtime, size) is UNCHANGED — body cache will hit, but
        # the overlay must still run.
        future_ts = time.time() + 86400
        monkeypatch.setattr(
            "app.sessions._load_session_access_cached",
            lambda project: {sid: future_ts},
        )
        second = load_session_summary(path)
        assert second["effective_ts"] == future_ts
        assert second["effective_ts"] > first_eff

    def test_overlay_returns_copy_not_mutated_cache(self, tmp_path, monkeypatch):
        """The cache stores the body-content dict; overlay must NOT mutate
        it in place or a stale access_ts would leak into future hits."""
        from app.sessions import load_session_summary, _summary_cache
        _summary_cache.clear()
        proj = tmp_path / "projects" / "test-proj"
        proj.mkdir(parents=True)
        sid = "sess-c"
        path = proj / f"{sid}.jsonl"
        _write_session(path, [
            _msg_line("user", "old", "2026-03-01T10:00:00Z"),
            _msg_line("assistant", "old", "2026-03-01T10:00:05Z"),
        ])
        # First load — no access
        monkeypatch.setattr(
            "app.sessions._load_session_access_cached", lambda project: {})
        result1 = load_session_summary(path)
        baseline = result1["effective_ts"]

        # Second load with high access_ts
        monkeypatch.setattr(
            "app.sessions._load_session_access_cached",
            lambda project: {sid: time.time() + 86400},
        )
        load_session_summary(path)

        # Third load — drop the access entry again.  Should return the
        # baseline, not the second call's bumped value.
        monkeypatch.setattr(
            "app.sessions._load_session_access_cached", lambda project: {})
        result3 = load_session_summary(path)
        assert result3["effective_ts"] == baseline

    def test_older_access_does_not_decrease_effective_ts(self, tmp_path,
                                                          monkeypatch):
        from app.sessions import load_session_summary, _summary_cache
        _summary_cache.clear()
        proj = tmp_path / "projects" / "test-proj"
        proj.mkdir(parents=True)
        sid = "sess-d"
        path = proj / f"{sid}.jsonl"
        # Recent file & messages
        _write_session(path, [
            _msg_line("user", "hi",
                      datetime.now(timezone.utc).isoformat()),
        ])
        # Access ts from a year ago
        monkeypatch.setattr(
            "app.sessions._load_session_access_cached",
            lambda project: {sid: time.time() - 365 * 86400},
        )
        result = load_session_summary(path)
        # Must reflect the (newer) mtime, not the ancient access ts
        assert result["effective_ts"] > time.time() - 365 * 86400


# ---------------------------------------------------------------------------
# Section 3 — access store (session_store helpers)
# ---------------------------------------------------------------------------

class TestSessionAccessStore:
    """The persistent access map under ``_session_access.json``."""

    def test_record_creates_file(self, tmp_path, monkeypatch):
        from app.session_store import (_record_session_access,
                                       _load_session_access, _access_file)
        proj = tmp_path / "projects" / "test-proj"
        proj.mkdir(parents=True)
        monkeypatch.setattr("app.session_store._sessions_dir",
                            lambda project="": proj)
        _record_session_access("sess-x", "test-proj")
        assert _access_file("test-proj").exists()
        data = _load_session_access("test-proj")
        assert "sess-x" in data
        assert abs(data["sess-x"] - time.time()) < 5.0

    def test_record_updates_existing(self, tmp_path, monkeypatch):
        from app.session_store import (_record_session_access,
                                       _load_session_access)
        proj = tmp_path / "projects" / "test-proj"
        proj.mkdir(parents=True)
        monkeypatch.setattr("app.session_store._sessions_dir",
                            lambda project="": proj)
        _record_session_access("sess-y", "test-proj")
        first = _load_session_access("test-proj")["sess-y"]
        time.sleep(0.01)
        _record_session_access("sess-y", "test-proj")
        second = _load_session_access("test-proj")["sess-y"]
        assert second > first

    def test_record_with_empty_id_is_noop(self, tmp_path, monkeypatch):
        from app.session_store import _record_session_access, _access_file
        proj = tmp_path / "projects" / "test-proj"
        proj.mkdir(parents=True)
        monkeypatch.setattr("app.session_store._sessions_dir",
                            lambda project="": proj)
        _record_session_access("", "test-proj")
        # No file should have been created
        assert not _access_file("test-proj").exists()

    def test_cached_loader_returns_same_dict_until_mtime_changes(
        self, tmp_path, monkeypatch
    ):
        from app.session_store import (_record_session_access,
                                       _load_session_access_cached,
                                       _access_cache)
        proj = tmp_path / "projects" / "test-proj"
        proj.mkdir(parents=True)
        monkeypatch.setattr("app.session_store._sessions_dir",
                            lambda project="": proj)
        _access_cache.clear()
        _record_session_access("sess-z", "test-proj")
        d1 = _load_session_access_cached("test-proj")
        d2 = _load_session_access_cached("test-proj")
        # Same cached object — mtime hasn't changed
        assert d1 is d2

    def test_cached_loader_busts_when_access_file_changes(
        self, tmp_path, monkeypatch
    ):
        from app.session_store import (_record_session_access,
                                       _load_session_access_cached,
                                       _access_cache)
        proj = tmp_path / "projects" / "test-proj"
        proj.mkdir(parents=True)
        monkeypatch.setattr("app.session_store._sessions_dir",
                            lambda project="": proj)
        _access_cache.clear()
        _record_session_access("sess-1", "test-proj")
        d1 = dict(_load_session_access_cached("test-proj"))
        time.sleep(0.05)  # ensure mtime granularity
        _record_session_access("sess-2", "test-proj")
        d2 = _load_session_access_cached("test-proj")
        assert "sess-2" in d2
        assert d2 != d1

    def test_load_session_access_missing_file_returns_empty(self, tmp_path):
        from app.session_store import _load_session_access
        # No file ever created
        result = _load_session_access(str(tmp_path / "nope"))
        assert result == {}


# ---------------------------------------------------------------------------
# Section 4 — custom-title append dedup
# ---------------------------------------------------------------------------

class TestCustomTitleDedup:
    """The Aras file accumulated 52 identical ``custom-title`` entries
    because every UI rename/autoname call appended unconditionally.  The
    dedup helper compares against the latest in-file title and skips
    when it matches.
    """

    def test_latest_returns_most_recent_title(self, tmp_path):
        from app.routes.sessions_api import _latest_custom_title_in_jsonl
        path = tmp_path / "s.jsonl"
        _write_session(path, [
            _custom_title_line("first"),
            _msg_line("user", "x", "2026-03-01T10:00:00Z"),
            _custom_title_line("second"),
            _custom_title_line("third"),
        ])
        assert _latest_custom_title_in_jsonl(path) == "third"

    def test_latest_returns_none_when_no_custom_title(self, tmp_path):
        from app.routes.sessions_api import _latest_custom_title_in_jsonl
        path = tmp_path / "s.jsonl"
        _write_session(path, [
            _msg_line("user", "x", "2026-03-01T10:00:00Z"),
            _msg_line("assistant", "y", "2026-03-01T10:00:05Z"),
        ])
        assert _latest_custom_title_in_jsonl(path) is None

    def test_latest_returns_none_for_missing_file(self, tmp_path):
        from app.routes.sessions_api import _latest_custom_title_in_jsonl
        assert _latest_custom_title_in_jsonl(tmp_path / "nope.jsonl") is None

    def test_latest_works_on_large_file(self, tmp_path):
        """Backward chunked read — must scan more than one 8 KiB chunk
        without buffer corruption."""
        from app.routes.sessions_api import _latest_custom_title_in_jsonl
        path = tmp_path / "big.jsonl"
        lines = []
        for i in range(500):
            lines.append(_msg_line("user", "x" * 200,
                                   "2026-03-01T10:00:00Z"))
        lines.append(_custom_title_line("final-name"))
        _write_session(path, lines)
        assert path.stat().st_size > 16384  # spans multiple chunks
        assert _latest_custom_title_in_jsonl(path) == "final-name"

    def test_append_skips_when_title_unchanged(self, tmp_path):
        from app.routes.sessions_api import _append_custom_title_if_changed
        path = tmp_path / "s.jsonl"
        _write_session(path, [_custom_title_line("Aras", "sid")])
        before_size = path.stat().st_size
        before_mtime = path.stat().st_mtime
        appended = _append_custom_title_if_changed(path, "Aras", "sid")
        assert appended is False
        assert path.stat().st_size == before_size
        assert path.stat().st_mtime == before_mtime

    def test_append_writes_when_title_changes(self, tmp_path):
        from app.routes.sessions_api import (
            _append_custom_title_if_changed, _latest_custom_title_in_jsonl,
        )
        path = tmp_path / "s.jsonl"
        _write_session(path, [_custom_title_line("Old name", "sid")])
        appended = _append_custom_title_if_changed(path, "New name", "sid")
        assert appended is True
        assert _latest_custom_title_in_jsonl(path) == "New name"

    def test_append_noop_when_file_missing(self, tmp_path):
        from app.routes.sessions_api import _append_custom_title_if_changed
        # Should not raise; should not create the file
        ret = _append_custom_title_if_changed(
            tmp_path / "nope.jsonl", "Whatever", "sid",
        )
        assert ret is False
        assert not (tmp_path / "nope.jsonl").exists()


# NOTE: API-level tests for /api/session/<id>/touch, the access-recording
# side-effect of GET /api/session/<id>, and the rename-dedup wiring live in
# tests/test_rest_api.py — those tests need the ``client`` / ``populated_project``
# / ``fake_project`` fixtures that are scoped to that file's app setup.
# Keep this file pure-Python so it stays fast and import-free of Flask.
