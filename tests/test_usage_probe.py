"""Fable limit probe (app/usage_probe.py): when to run, and that it never
blocks, double-runs, or fires for accounts it cannot help."""

from unittest.mock import MagicMock, patch

import pytest

from app import usage_probe as up

NOW = 1790960000.0
FIVE = {"utilization": 0.1, "resets_at": int(NOW) + 3600, "seen_at": NOW}
WEEK = {"utilization": 0.6, "resets_at": int(NOW) + 86400, "seen_at": NOW}


def _state(**extra):
    return {"windows": {"five_hour": FIVE, "seven_day": WEEK, **extra}, "updated_at": NOW}


@pytest.fixture(autouse=True)
def _reset():
    up._last_attempt = 0.0
    up._backoff_until = 0.0
    up._running = False
    yield
    up._running = False


class TestNeedsProbe:
    def test_missing_fable_needs_probe(self):
        assert up.needs_probe(_state(), NOW) is True

    def test_fresh_fable_does_not(self):
        fable = {"utilization": 0.93, "resets_at": int(NOW) + 86400, "seen_at": NOW}
        assert up.needs_probe(_state(**{up.FABLE_WINDOW: fable}), NOW) is False

    def test_expired_fable_needs_probe(self):
        fable = {"utilization": 0.93, "resets_at": int(NOW) - 10, "seen_at": NOW - 99999}
        assert up.needs_probe(_state(**{up.FABLE_WINDOW: fable}), NOW) is True

    def test_no_unified_windows_never_probes(self):
        # API-key account / nothing seen yet: a probe would cost money for nothing.
        assert up.needs_probe({}, NOW) is False
        assert up.needs_probe({"windows": {}}, NOW) is False
        assert up.needs_probe(None, NOW) is False


class TestMaybeStart:
    def _sm(self, connected=True):
        sm = MagicMock()
        sm.is_connected = connected
        return sm

    def test_starts_once_then_throttles(self):
        with patch.object(up.threading, "Thread") as T, patch.object(up, "_enabled", return_value=True):
            assert up.maybe_start(self._sm(), _state(), now=NOW) is True
            assert T.return_value.start.call_count == 1
            up._running = False          # pretend the probe finished
            assert up.maybe_start(self._sm(), _state(), now=NOW + 60) is False
            assert up.maybe_start(self._sm(), _state(), now=NOW + up.RETRY_S + 1) is True

    def test_never_two_at_once(self):
        with patch.object(up.threading, "Thread"), patch.object(up, "_enabled", return_value=True):
            assert up.maybe_start(self._sm(), _state(), now=NOW) is True
            assert up.maybe_start(self._sm(), _state(), manual=True, now=NOW + 9999) is False

    def test_disabled_or_disconnected_or_unneeded(self):
        with patch.object(up.threading, "Thread") as T:
            with patch.object(up, "_enabled", return_value=False):
                assert up.maybe_start(self._sm(), _state(), now=NOW) is False
            with patch.object(up, "_enabled", return_value=True):
                assert up.maybe_start(self._sm(connected=False), _state(), now=NOW) is False
                assert up.maybe_start(None, _state(), now=NOW) is False
                assert up.maybe_start(self._sm(), {}, now=NOW) is False
            assert T.return_value.start.call_count == 0

    def test_backoff_blocks_automatic_but_not_manual(self):
        up._backoff_until = NOW + 5000
        with patch.object(up.threading, "Thread"), patch.object(up, "_enabled", return_value=True):
            assert up.maybe_start(self._sm(), _state(), now=NOW) is False
            assert up.maybe_start(self._sm(), _state(), manual=True, now=NOW) is True

    def test_manual_runs_even_when_reading_is_fresh(self):
        fable = {"utilization": 0.93, "resets_at": int(NOW) + 86400, "seen_at": NOW}
        with patch.object(up.threading, "Thread"), patch.object(up, "_enabled", return_value=True):
            assert up.maybe_start(self._sm(), _state(**{up.FABLE_WINDOW: fable}), manual=True, now=NOW) is True


class TestRun:
    def test_probe_is_hidden_one_turn_and_always_cleaned_up(self):
        sm = MagicMock()
        sm.start_session.return_value = {"ok": True}
        sm.get_session_state.return_value = "idle"
        with patch("daemon.usage_limits.load", return_value=_state()), \
             patch("app.titling._dispose_title_session") as dispose, \
             patch("app.titling._cleanup_title_jsonl") as cleanup, \
             patch.object(up.time, "sleep"):
            up._running = True
            up._run(sm)
        kw = sm.start_session.call_args.kwargs
        assert kw["session_type"] == "title" and kw["max_turns"] == 1
        assert kw["allowed_tools"] == [] and "fable" in kw["model"]
        assert kw["session_id"].startswith("_usage_")
        dispose.assert_called_once()
        cleanup.assert_called_once()
        assert up._running is False
        assert up._backoff_until > 0      # no Fable window came back -> back off
