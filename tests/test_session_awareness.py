"""Behavior tests for app.session_awareness — cross-session context builder.

This module is pure, deterministic logic: given a list of session-state
dicts it filters and formats a system-prompt block.  The only external
dependency is a ``daemon_client`` with a ``get_all_states()`` method, which
is trivially faked here.  No daemon, no IPC, no I/O.

Covers the hardening goal "cross-session awareness must be testable":
every filter branch (self, utility sessions, inactive, cross-project), the
12-session cap and "(+N more)" truncation, the duration formatter, basename
dedup, and the 2-second states cache.
"""

import time

import pytest

from app import session_awareness as sa
from app.config import _encode_cwd


# ---------------------------------------------------------------------------
# Fakes / helpers
# ---------------------------------------------------------------------------

class FakeClient:
    """Stand-in for the daemon client / SessionManager.

    Counts calls so cache behavior is observable.  ``raises=True`` makes
    ``get_all_states()`` blow up to exercise the defensive try/except.
    """

    def __init__(self, states, raises=False):
        self._states = states
        self.calls = 0
        self.raises = raises

    def get_all_states(self):
        self.calls += 1
        if self.raises:
            raise RuntimeError("daemon down")
        return self._states


def _state(session_id, **over):
    """Build a session-state dict with sane active defaults."""
    base = {
        "session_id": session_id,
        "session_type": "",
        "state": "working",
        "cwd": "",          # empty cwd → cross-project filter is skipped
        "name": f"name-{session_id}",
        "created_ts": 0,
        "tracked_files": [],
    }
    base.update(over)
    return base


@pytest.fixture(autouse=True)
def _reset_states_cache():
    """Each test starts with an empty states cache.

    The cache is module-global; without this reset, ordering between tests
    would leak stale state and make the cache tests non-deterministic.
    """
    sa._states_cache = None
    sa._states_cache_time = 0.0
    yield
    sa._states_cache = None
    sa._states_cache_time = 0.0


# ---------------------------------------------------------------------------
# _format_duration
# ---------------------------------------------------------------------------

class TestFormatDuration:

    def test_zero_is_unknown(self):
        assert sa._format_duration(0) == "unknown"

    def test_none_is_unknown(self):
        assert sa._format_duration(None) == "unknown"

    def test_under_one_minute(self):
        assert sa._format_duration(time.time() - 30) == "<1m"

    def test_minutes(self):
        assert sa._format_duration(time.time() - 5 * 60) == "5m"

    def test_whole_hours(self):
        assert sa._format_duration(time.time() - 2 * 3600) == "2h"

    def test_hours_and_minutes(self):
        assert sa._format_duration(time.time() - (3 * 3600 + 15 * 60)) == "3h15m"

    def test_future_timestamp_does_not_crash(self):
        # A clock-skewed future created_ts yields a negative delta → <1m,
        # never an exception.
        assert sa._format_duration(time.time() + 1000) == "<1m"


# ---------------------------------------------------------------------------
# _basenames
# ---------------------------------------------------------------------------

class TestBasenames:

    def test_empty_list(self):
        assert sa._basenames([]) == "(no file edits yet)"

    def test_strips_directories(self):
        out = sa._basenames(["/a/b/foo.py"])
        assert out == "foo.py"

    def test_most_recent_last_appears_first(self):
        # The list is ordered oldest→newest; the builder shows newest first.
        out = sa._basenames(["/x/old.py", "/x/new.py"])
        assert out == "new.py, old.py"

    def test_dedupes_basenames(self):
        out = sa._basenames(["/a/dup.py", "/b/dup.py", "/c/uniq.py"])
        # Only one "dup.py", newest-first ordering.
        assert out == "uniq.py, dup.py"

    def test_respects_limit(self):
        paths = [f"/p/f{i}.py" for i in range(10)]
        out = sa._basenames(paths, limit=3)
        assert len(out.split(", ")) == 3
        # Newest three, newest first.
        assert out == "f9.py, f8.py, f7.py"


# ---------------------------------------------------------------------------
# _get_all_states_cached
# ---------------------------------------------------------------------------

class TestStatesCache:

    def test_caches_within_ttl(self):
        client = FakeClient([_state("a")])
        first = sa._get_all_states_cached(client)
        second = sa._get_all_states_cached(client)
        assert first == second
        assert client.calls == 1  # second served from cache

    def test_refetches_after_ttl(self, monkeypatch):
        client = FakeClient([_state("a")])
        clock = [1000.0]
        monkeypatch.setattr(sa.time, "monotonic", lambda: clock[0])
        sa._get_all_states_cached(client)
        clock[0] += sa._STATES_CACHE_TTL + 0.1  # advance past TTL
        sa._get_all_states_cached(client)
        assert client.calls == 2

    def test_exception_propagates_to_caller(self):
        client = FakeClient(None, raises=True)
        with pytest.raises(RuntimeError):
            sa._get_all_states_cached(client)


# ---------------------------------------------------------------------------
# build_cross_session_context — defensive returns
# ---------------------------------------------------------------------------

class TestBuilderDefensive:

    def test_daemon_failure_returns_none(self):
        client = FakeClient(None, raises=True)
        assert sa.build_cross_session_context(client, "proj", "self") is None

    def test_empty_states_returns_none(self):
        client = FakeClient([])
        assert sa.build_cross_session_context(client, "proj", "self") is None

    def test_only_self_returns_none(self):
        client = FakeClient([_state("self")])
        assert sa.build_cross_session_context(client, "proj", "self") is None


# ---------------------------------------------------------------------------
# build_cross_session_context — filtering
# ---------------------------------------------------------------------------

class TestBuilderFiltering:

    def test_excludes_self(self):
        client = FakeClient([_state("self"), _state("other", name="Other")])
        out = sa.build_cross_session_context(client, "proj", "self")
        assert out is not None
        assert "Other" in out
        assert "name-self" not in out

    @pytest.mark.parametrize("stype", ["planner", "title"])
    def test_excludes_utility_sessions(self, stype):
        client = FakeClient([
            _state("util", session_type=stype, name="Utility"),
            _state("real", name="RealWork"),
        ])
        out = sa.build_cross_session_context(client, "proj", "self")
        assert "Utility" not in out
        assert "RealWork" in out

    @pytest.mark.parametrize("bad_state", ["dead", "starting", "error", ""])
    def test_excludes_non_active_states(self, bad_state):
        client = FakeClient([_state("x", state=bad_state, name="Ghost")])
        out = sa.build_cross_session_context(client, "proj", "self")
        assert out is None

    @pytest.mark.parametrize("good_state", ["working", "idle", "waiting"])
    def test_includes_active_states(self, good_state):
        client = FakeClient([_state("x", state=good_state, name="Live")])
        out = sa.build_cross_session_context(client, "proj", "self")
        assert out is not None
        assert "Live" in out
        assert good_state in out

    def test_excludes_other_projects(self):
        proj_a = _encode_cwd("C:\\projA")
        client = FakeClient([
            _state("in", cwd="C:\\projA", name="InProject"),
            _state("out", cwd="C:\\projB", name="OtherProject"),
        ])
        out = sa.build_cross_session_context(client, proj_a, "self")
        assert "InProject" in out
        assert "OtherProject" not in out


# ---------------------------------------------------------------------------
# build_cross_session_context — formatting
# ---------------------------------------------------------------------------

class TestBuilderFormatting:

    def test_unnamed_session_renders_placeholder(self):
        client = FakeClient([_state("x", name="")])
        out = sa.build_cross_session_context(client, "proj", "self")
        assert "(unnamed)" in out

    def test_named_session_is_quoted(self):
        client = FakeClient([_state("x", name="My Feature")])
        out = sa.build_cross_session_context(client, "proj", "self")
        assert '"My Feature"' in out

    def test_tracked_files_rendered_as_basenames(self):
        client = FakeClient([
            _state("x", name="W", tracked_files=["/repo/app/foo.py"]),
        ])
        out = sa.build_cross_session_context(client, "proj", "self")
        assert "foo.py" in out
        assert "/repo/app" not in out

    def test_no_files_placeholder(self):
        client = FakeClient([_state("x", name="W", tracked_files=[])])
        out = sa.build_cross_session_context(client, "proj", "self")
        assert "(no file edits yet)" in out

    def test_includes_conflict_guidance_template(self):
        client = FakeClient([_state("x", name="W")])
        out = sa.build_cross_session_context(client, "proj", "self")
        assert "Multi-Session Conflict Guidance" in out


# ---------------------------------------------------------------------------
# build_cross_session_context — cap and truncation
# ---------------------------------------------------------------------------

class TestBuilderCap:

    def test_caps_at_max_sessions(self):
        states = [_state(f"s{i}", name=f"S{i}") for i in range(20)]
        client = FakeClient(states)
        out = sa.build_cross_session_context(client, "proj", "self")
        # Count rendered session bullet lines.
        bullet_lines = [ln for ln in out.splitlines() if ln.startswith("- ")]
        assert len(bullet_lines) == sa._MAX_SESSIONS

    def test_truncation_footer(self):
        states = [_state(f"s{i}", name=f"S{i}") for i in range(15)]
        client = FakeClient(states)
        out = sa.build_cross_session_context(client, "proj", "self")
        # 15 active others − 12 cap = 3 truncated.
        assert "(+3 more)" in out

    def test_no_footer_when_under_cap(self):
        states = [_state(f"s{i}", name=f"S{i}") for i in range(3)]
        client = FakeClient(states)
        out = sa.build_cross_session_context(client, "proj", "self")
        assert "more)" not in out
