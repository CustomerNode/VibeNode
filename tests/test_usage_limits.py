"""Usage-limits pill: session / weekly / Fable windows from the CLI's
rate_limit_event (added 2026-10-01).

The fixture payloads below are copied from real `claude -p --output-format
stream-json` runs (CLI 2.1.283): Haiku/Opus turns report five_hour and
seven_day; a Fable turn additionally reports seven_day_overage_included, which
the CLI's own label table calls "Fable limit".
"""

import pytest
from unittest.mock import MagicMock, patch

from daemon import usage_limits as ul

OPUS_TURN = {
    "status": "allowed", "resetsAt": 1790871000, "rateLimitType": "five_hour",
    "unifiedWindows": {
        "five_hour": {"utilization": 0.16, "resetsAt": 1790871000},
        "seven_day": {"utilization": 0.46, "resetsAt": 1790964000},
    },
}
FABLE_TURN = {
    "status": "allowed", "resetsAt": 1790871000, "rateLimitType": "five_hour",
    "unifiedWindows": {
        "five_hour": {"utilization": 0.16, "resetsAt": 1790871000},
        "seven_day": {"utilization": 0.46, "resetsAt": 1790964000},
        "seven_day_overage_included": {"utilization": 0.14, "resetsAt": 1790964000},
    },
}
NOW = 1790866000.0


class TestMerge:
    def test_first_event_is_a_change(self):
        state, changed = ul.merge({}, OPUS_TURN, now=NOW)
        assert changed is True
        assert state["windows"]["five_hour"]["utilization"] == 0.16
        assert state["windows"]["seven_day"]["resets_at"] == 1790964000
        assert state["updated_at"] == NOW

    def test_identical_event_is_not_a_change(self):
        state, _ = ul.merge({}, OPUS_TURN, now=NOW)
        _, changed = ul.merge(state, OPUS_TURN, now=NOW + 60)
        assert changed is False

    def test_fable_window_survives_non_fable_turns(self):
        """The Fable window is only reported on Fable turns; an Opus turn
        afterwards must not erase it."""
        state, _ = ul.merge({}, FABLE_TURN, now=NOW)
        state, _ = ul.merge(state, OPUS_TURN, now=NOW + 60)
        assert state["windows"]["seven_day_overage_included"]["utilization"] == 0.14

    def test_moved_utilization_or_reset_is_a_change(self):
        state, _ = ul.merge({}, OPUS_TURN, now=NOW)
        bumped = {"unifiedWindows": {"five_hour": {"utilization": 0.17, "resetsAt": 1790871000}}}
        _, changed = ul.merge(state, bumped, now=NOW)
        assert changed is True
        reset = {"unifiedWindows": {"five_hour": {"utilization": 0.16, "resetsAt": 1790889000}}}
        _, changed = ul.merge(state, reset, now=NOW)
        assert changed is True

    @pytest.mark.parametrize("bad", [None, "x", {}, {"unifiedWindows": None},
                                     {"unifiedWindows": {"five_hour": {"utilization": "0.5"}}},
                                     {"unifiedWindows": {"five_hour": {"utilization": True}}},
                                     {"unifiedWindows": {"weird_window": {"utilization": 0.5}}}])
    def test_malformed_payloads_are_ignored(self, bad):
        state, _ = ul.merge({}, OPUS_TURN, now=NOW)
        new, changed = ul.merge(state, bad, now=NOW + 1)
        assert changed is False
        assert new["windows"] == state["windows"]


class TestPublicView:
    def test_percent_label_and_expiry(self):
        state, _ = ul.merge({}, FABLE_TURN, now=NOW)
        view = ul.public_view(state, now=NOW)
        w = view["windows"]
        assert (w["five_hour"]["label"], w["five_hour"]["percent"]) == ("Session", 16)
        assert (w["seven_day"]["label"], w["seven_day"]["percent"]) == ("Week", 46)
        assert (w["seven_day_overage_included"]["label"],
                w["seven_day_overage_included"]["percent"]) == ("Fable", 14)
        assert not any(v["expired"] for v in w.values())
        later = ul.public_view(state, now=1790871000 + 1)
        assert later["windows"]["five_hour"]["expired"] is True
        assert later["windows"]["seven_day"]["expired"] is False

    def test_unseen_window_is_omitted(self):
        state, _ = ul.merge({}, OPUS_TURN, now=NOW)
        assert "seven_day_overage_included" not in ul.public_view(state, now=NOW)["windows"]


class TestPersistence:
    def test_roundtrip_and_missing_file(self, tmp_path):
        p = tmp_path / "limits.json"
        assert ul.load(p) == {}
        state, _ = ul.merge({}, FABLE_TURN, now=NOW)
        ul.save(state, p)
        assert ul.load(p) == state

    def test_corrupt_file_reads_empty(self, tmp_path):
        p = tmp_path / "limits.json"
        p.write_text("{not json", encoding="utf-8")
        assert ul.load(p) == {}


class TestSdkCapture:
    """The SDK has no type for rate_limit_event; the safe-parse wrapper must
    hand it to the sink and still drop it from the message stream."""

    def test_rate_limit_event_reaches_sink_and_is_dropped(self):
        from daemon import sdk_patches as sp
        sp.apply_patches()
        from claude_code_sdk._internal.message_parser import parse_message
        got = []
        sp.set_rate_limit_sink(got.append)
        try:
            result = parse_message({"type": "rate_limit_event",
                                    "rate_limit_info": FABLE_TURN,
                                    "uuid": "u", "session_id": "s"})
        finally:
            sp.set_rate_limit_sink(None)
        assert result is None
        assert got == [FABLE_TURN]

    def test_sink_failure_never_breaks_the_stream(self):
        from daemon import sdk_patches as sp
        sp.apply_patches()
        from claude_code_sdk._internal.message_parser import parse_message

        def boom(_):
            raise RuntimeError("sink broke")
        sp.set_rate_limit_sink(boom)
        try:
            assert parse_message({"type": "rate_limit_event",
                                  "rate_limit_info": OPUS_TURN}) is None
        finally:
            sp.set_rate_limit_sink(None)

    def test_other_unknown_types_do_not_reach_sink(self):
        from daemon import sdk_patches as sp
        sp.apply_patches()
        from claude_code_sdk._internal.message_parser import parse_message
        got = []
        sp.set_rate_limit_sink(got.append)
        try:
            parse_message({"type": "some_future_event", "data": {}})
        finally:
            sp.set_rate_limit_sink(None)
        assert got == []


class TestSessionManagerSink:
    """SessionManager._on_rate_limit_info: persist + broadcast on real change only."""

    def _sm(self, tmp_path):
        import threading
        from daemon.session_manager import SessionManager
        sm = SessionManager.__new__(SessionManager)
        sm._usage_lock = threading.Lock()
        sm._usage_state = {}
        sm._push_callback = MagicMock()
        return sm

    def test_broadcasts_once_per_change(self, tmp_path):
        sm = self._sm(tmp_path)
        with patch.object(ul, "USAGE_LIMITS_PATH", tmp_path / "l.json"), \
             patch.object(ul, "save") as save:
            sm._on_rate_limit_info(FABLE_TURN)
            sm._on_rate_limit_info(FABLE_TURN)  # identical: no second push
        assert sm._push_callback.call_count == 1
        event, payload = sm._push_callback.call_args.args
        assert event == "usage_limits"
        assert payload["windows"]["seven_day_overage_included"]["percent"] == 14
        assert save.call_count == 1

    def test_without_init_is_a_noop(self):
        from daemon.session_manager import SessionManager
        sm = SessionManager.__new__(SessionManager)
        sm._on_rate_limit_info(OPUS_TURN)  # no _usage_lock yet: must not raise


class TestEndpoint:
    def test_reads_persisted_snapshot(self, tmp_path):
        from flask import Flask
        from app.routes.main import bp
        state, _ = ul.merge({}, OPUS_TURN, now=NOW)
        p = tmp_path / "l.json"
        ul.save(state, p)
        app = Flask(__name__)
        app.register_blueprint(bp)
        real_load = ul.load
        with patch.object(ul, "load", lambda path=p: real_load(p)):
            r = app.test_client().get("/api/usage-limits")
        body = r.get_json()
        assert r.status_code == 200 and body["ok"] is True
        assert body["windows"]["five_hour"]["percent"] == 16
        assert body["windows"]["seven_day"]["label"] == "Week"

    def test_empty_when_nothing_seen(self, tmp_path):
        from flask import Flask
        from app.routes.main import bp
        app = Flask(__name__)
        app.register_blueprint(bp)
        with patch.object(ul, "load", lambda *a, **k: {}):
            body = app.test_client().get("/api/usage-limits").get_json()
        assert body["ok"] is True and body["windows"] == {}
