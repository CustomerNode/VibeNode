"""5-hour pacing: the window opens with the first usage, its share is the
weekly usage left spread evenly over the 5-hour blocks, a hit lets running
turns finish and stops any that start after it, refuses messages until the
reset, the reset continues them, and only a person may press Reset."""

import json
import time

import pytest

from web import pace_5h as pace

H = 3600
NOW = 1_800_000_000.0
WEEK_RESETS = int(NOW + 90 * H)     # 18 five-hour blocks left


def _go(st, pct, now, mult=1.0):
    return pace.step(st, pct=pct, week_resets=WEEK_RESETS, mult=mult, now=now)


def test_off_by_default():
    assert pace.mult_of({}) == 0.0 and pace.STEPS[-1] == 0.0 and pace.STEPS[0] == 1.0


def test_linear_share_is_the_week_left_over_the_blocks_left():
    # 46% used, 54% left over 18 blocks = 3% per 5 hours; 2x = 6%.
    assert pace.share_for(46, WEEK_RESETS, NOW, 1) == pytest.approx(3.0)
    assert pace.share_for(46, WEEK_RESETS, NOW, 2) == pytest.approx(6.0)
    assert pace.share_for(99.9, WEEK_RESETS, NOW, 1) == pace.MIN_SHARE


def test_a_window_opens_with_the_first_usage_and_resets_five_hours_later():
    st, ev = _go({}, 46.0, NOW)
    assert st.get("window") is None and ev == []          # first reading: nothing used yet
    st, ev = _go(st, 46.0, NOW + 600)
    assert st.get("window") is None                        # still nothing used
    st, ev = _go(st, 47.0, NOW + 1200)
    w = st["window"]
    assert w["reset_at"] == NOW + 1200 + 5 * H and w["start_pct"] == 46.0 and w["used"] == 1.0
    assert ev == []


def test_hitting_the_share_then_the_reset():
    st, _ = _go({}, 46.0, NOW)
    st, _ = _go(st, 47.0, NOW + 60)
    st, ev = _go(st, 49.1, NOW + 120)
    assert ev == ["hit"] and st["window"]["hit"]
    st, ev = _go(st, 49.5, NOW + 180)
    assert ev == []                                        # hit once
    st, ev = _go(st, 49.5, NOW + 60 + 5 * H)
    assert ev == ["reset"] and st["window"] is None


def test_turning_it_off_ends_a_hit_window():
    st, _ = _go({}, 46.0, NOW)
    st, _ = _go(st, 50.0, NOW + 60)
    assert st["window"]["hit"]
    st, ev = _go(st, 50.0, NOW + 120, mult=0)
    assert ev == ["reset"] and st["window"] is None


class Engine:
    def __init__(self, states):
        self.states = states
        self.interrupted, self.sent = [], []

    def get_all_states(self):
        return [{"session_id": k, "state": v} for k, v in self.states.items()]

    def interrupt_session(self, sid, clear_queue=True):
        assert clear_queue is False           # the person's queued messages are kept
        self.interrupted.append(sid)
        self.states[sid] = "idle"
        return {"ok": True}

    def send_message(self, sid, text):
        self.sent.append((sid, text))
        return {"ok": True}


def test_tick_stops_working_sessions_and_continues_them_at_the_reset(tmp_path):
    path = tmp_path / "pace.json"
    eng = Engine({"a": "working", "b": "idle", "c": "working"})
    usage = {"windows": {"seven_day": {"utilization": 0.46, "resets_at": WEEK_RESETS}}}
    cfg = {pace.SETTING_NAME: 1}
    pace.tick(eng, lambda: usage, cfg, path, now=NOW)
    usage["windows"]["seven_day"]["utilization"] = 0.50
    out = pace.tick(eng, lambda: usage, cfg, path, now=NOW + 60)
    # The turns running at the hit finish: nothing is interrupted.
    assert out["events"] == ["hit"] and out["stopped"] == [] and eng.interrupted == []
    assert pace.is_hit(path, now=NOW + 120) == pytest.approx(NOW + 60 + 5 * H)
    # A turn that starts after the hit is interrupted.
    eng.states["b"] = "working"
    assert pace.tick(eng, lambda: usage, cfg, path, now=NOW + 200)["stopped"] == ["b"]
    # "a" finishes its turn; its next one (a queued message) is interrupted.
    eng.states["a"] = "idle"
    assert pace.tick(eng, lambda: usage, cfg, path, now=NOW + 215)["stopped"] == []
    eng.states["a"] = "working"
    assert pace.tick(eng, lambda: usage, cfg, path, now=NOW + 230)["stopped"] == ["a"]
    # "c" is still on the turn it had at the hit; then it is slept.
    eng.states["c"] = "stopped"
    out = pace.tick(eng, lambda: usage, cfg, path, now=NOW + 60 + 5 * H)
    assert out["events"] == ["reset"] and sorted(out["resumed"]) == ["a", "b"]
    assert all(t == pace.CONTINUE_TEXT for _, t in eng.sent)
    assert pace.is_hit(path, now=NOW + 60 + 5 * H) is None


def test_manual_reset_ends_the_window_and_continues(tmp_path):
    path = tmp_path / "pace.json"
    eng = Engine({"a": "working"})
    usage = {"windows": {"seven_day": {"utilization": 0.46, "resets_at": WEEK_RESETS}}}
    cfg = {pace.SETTING_NAME: 1}
    pace.tick(eng, lambda: usage, cfg, path, now=NOW)
    usage["windows"]["seven_day"]["utilization"] = 0.50
    pace.tick(eng, lambda: usage, cfg, path, now=NOW + 60)
    eng.states["a"] = "idle"
    pace.tick(eng, lambda: usage, cfg, path, now=NOW + 75)
    eng.states["a"] = "working"                      # a later turn: interrupted
    pace.tick(eng, lambda: usage, cfg, path, now=NOW + 90)
    assert pace.reset(eng, path) == {"resumed": ["a"]}
    assert pace.load(path)["window"] is None and pace.is_hit(path) is None


def test_the_continue_message_reads_as_automated():
    from shared.transcripts import automated_source
    assert automated_source(pace.CONTINUE_TEXT, False)


# ── Reset is a person's button ────────────────────────────────────────────

@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(pace, "STATE_PATH", tmp_path / "pace.json")
    monkeypatch.setattr(pace.reset, "__defaults__", (tmp_path / "pace.json",))
    from web import create_app
    app = create_app(testing=True)

    class SM:
        def get_all_states(self):
            return []
    app.session_manager = SM()
    return app.test_client()


def test_reset_is_refused_without_the_page(client):
    r = client.post("/api/pace-5h/reset", json={})
    assert r.status_code == 403
    r = client.post("/api/pace-5h/reset", json={}, headers={"X-VN-Human": "reset"})
    assert r.status_code == 403                      # no Origin: not a browser page
    r = client.post("/api/pace-5h/reset", json={}, headers={"X-VN-Human": "reset", "Origin": "http://evil.example"})
    assert r.status_code == 403


def test_reset_from_the_page_works(client):
    r = client.post("/api/pace-5h/reset", json={},
                    headers={"X-VN-Human": "reset", "Origin": "http://localhost"})
    assert r.status_code == 200 and r.get_json()["ok"]


def test_a_session_may_not_press_reset(tmp_path):
    from engine import tool_guards
    none = tmp_path / "no-guards.json"
    cmd = "curl -s -X POST http://localhost:5050/api/pace-5h/reset -H 'X-VN-Human: reset'"
    assert "human only" in (tool_guards.decide("Bash", {"command": cmd}, "", config=none) or "")
    assert "human only" in (tool_guards.decide("Write", {"file_path": "/x/data/pace_5h.json", "content": "{}"}, "", config=none) or "")
    # Reading or editing the code that serves the button is fine.
    assert tool_guards.decide("Read", {"file_path": "/x/data/pace_5h.json"}, "", config=none) is None
    assert tool_guards.decide("Edit", {"file_path": "/x/a.py", "old_string": "a", "new_string": "pace-5h/reset"}, "", config=none) is None
