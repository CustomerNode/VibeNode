"""Weekly usage pace: fast-day warnings and spare-capacity suggestions."""

from web import usage_pace as up

DAY = up.DAY
R = 1_800_000_000  # a weekly reset instant


def _run(readings, start=1_000_000.0):
    """Feed (hours_from_start, util, resets_at) readings; collect events."""
    h, out = {}, []
    for hours, util, resets in readings:
        h, ev = up.evaluate(h, util, resets, start + hours * 3600)
        out.append(ev)
    return h, out


def test_one_day_of_usage_in_a_day_fires_once():
    _, out = _run([(0, 0.10, R), (6, 0.20, R), (10, 0.25, R), (12, 0.26, R)])
    kinds = [[e["days"] for e in ev if e["kind"] == "fast_day"] for ev in out]
    assert kinds == [[], [], [1], []]


def test_each_further_day_fires_again():
    _, out = _run([(0, 0.0, R), (2, 0.15, R), (4, 0.30, R), (6, 0.44, R)])
    days = [e["days"] for ev in out for e in ev if e["kind"] == "fast_day"]
    assert days == [1, 2, 3]


def test_slow_steady_use_never_warns():
    # 10% a day for a week: under one day of usage in any 24 hours.
    _, out = _run([(i * 12, 0.05 * i, R) for i in range(14)])
    assert not [e for ev in out for e in ev if e["kind"] == "fast_day"]


def test_the_level_rolls_off_and_can_fire_again_later():
    _, out = _run([(0, 0.0, R), (1, 0.15, R), (30, 0.16, R), (31, 0.32, R)])
    days = [e["days"] for ev in out for e in ev if e["kind"] == "fast_day"]
    assert days == [1, 1]


def test_usage_across_a_weekly_reset_still_counts():
    samples = [{"t": 0, "u": 0.90, "r": R}, {"t": 3600, "u": 0.98, "r": R},
               {"t": 7200, "u": 0.10, "r": R + 7 * DAY}]
    assert abs(up.used_in_last_day(samples, 7200) - 0.18) < 1e-9


def test_spare_needs_double_the_normal_pace_left():
    now = 0.0
    # 2 days to reset; normal pace would spend 2/7. Left 4 days -> spare.
    assert up.spare(1 - 4 / 7, int(now + 2 * DAY), now)
    # left 3 days with 2 to go -> not spare.
    assert up.spare(1 - 3 / 7, int(now + 2 * DAY), now) is None
    # nothing in the last 6 hours before the reset.
    assert up.spare(0.0, int(now + 3 * 3600), now) is None


def test_spare_fires_once_per_week():
    resets = int(1_000_000 + 2 * DAY)
    _, out = _run([(0, 0.40, resets), (1, 0.40, resets), (5, 0.41, resets)])
    spares = [e for ev in out for e in ev if e["kind"] == "spare"]
    assert len(spares) == 1 and spares[0]["days_left"] > 4


def test_tick_reads_the_tracker_and_keeps_history(tmp_path):
    path = tmp_path / "h.json"
    state = {"windows": {"seven_day": {"utilization": 0.0, "resets_at": R}}}
    first = up.tick(lambda: state, path, now=R - 3 * DAY)
    assert [e["kind"] for e in first] == ["spare"]
    state["windows"]["seven_day"]["utilization"] = 0.2
    ev = up.tick(lambda: state, path, now=R - 3 * DAY + 3600)
    assert [e["days"] for e in ev if e["kind"] == "fast_day"] == [1]
    assert len(up.load(path)["samples"]) == 2


def test_the_warning_carries_the_days_curve():
    _, out = _run([(0, 0.0, R), (3, 0.08, R), (6, 0.16, R)])
    ev = [e for e in out[-1] if e["kind"] == "fast_day"][0]
    assert ev["curve"][0] == [24.0, 0.0] and ev["curve"][-1] == [0.0, 0.16]


def test_both_pop_ups_are_on_unless_turned_off():
    assert up.enabled("fast_day", {}) and up.enabled("spare", {})
    assert not up.enabled("spare", {"usage_spare_suggest": False})


def test_pending_keeps_the_newest_per_kind_until_answered(tmp_path):
    p = tmp_path / "h.json"
    up.add_pending([{"kind": "fast_day", "days": 1}], p, now=100)
    up.add_pending([{"kind": "fast_day", "days": 2}, {"kind": "spare"}], p, now=200)
    assert sorted((e["kind"], e.get("days")) for e in up.pending(p, now=300)) == [("fast_day", 2), ("spare", None)]
    up.ack("fast_day", p)
    assert [e["kind"] for e in up.pending(p, now=300)] == ["spare"]
    assert up.pending(p, now=200 + up.PENDING_MAX_AGE + 1) == []


def test_busiest_project_ranks_by_sessions_this_month(tmp_path):
    import json, os
    busy, quiet = tmp_path / "w" / "busy", tmp_path / "w" / "quiet"
    for d in (busy, quiet):
        d.mkdir(parents=True)
    projects = tmp_path / "projects"
    now = 1_000_000_000.0

    def proj(name, cwd, ages):
        d = projects / name
        d.mkdir(parents=True)
        for i, age in enumerate(ages):
            f = d / f"{i}.jsonl"
            f.write_text(json.dumps({"type": "user", "cwd": str(cwd)}) + "\n")
            os.utime(f, (now - age * DAY, now - age * DAY))

    proj("-quiet", quiet, [1, 2, 40, 41, 42])     # 2 this month, newest
    proj("-busy", busy, [3, 5, 9])                 # 3 this month
    proj("-gone", tmp_path / "nope", [1, 1, 1, 1])  # folder deleted
    ranked = up.busiest_projects(projects, now=now)
    assert [p["cwd"] for p in ranked] == [str(busy), str(quiet)]
    assert ranked[0]["recent"] == 3


def test_the_job_prompt_keeps_the_task_and_asks_for_a_long_autonomous_run():
    t = up.task_prompt("  Make the suite faster  ")
    assert t.startswith("Make the suite faster\n") and "autonomous" in t


# ---------------------------------------------------------------- routes

import pytest


@pytest.fixture
def client(tmp_path, monkeypatch):
    from web import create_app
    monkeypatch.setattr(up, "HISTORY_PATH", tmp_path / "h.json")
    app = create_app(testing=True)
    return app.test_client()


def test_pending_route_returns_unanswered_pop_ups_and_ack_clears_them(client, tmp_path):
    up.add_pending([{"kind": "spare", "days_left": 4}])
    assert [e["kind"] for e in client.get("/api/usage-pace").get_json()["pending"]] == ["spare"]
    assert client.post("/api/usage-pace/ack", json={"kind": "spare"}).get_json()["ok"]
    assert client.get("/api/usage-pace").get_json()["pending"] == []
    assert client.post("/api/usage-pace/ack", json={"kind": "x"}).status_code == 400


def test_start_refuses_a_folder_that_is_not_a_project_with_sessions(client):
    r = client.post("/api/usage-pace/start", json={"cwd": "/etc", "task": "x"})
    assert r.status_code == 400
    assert client.post("/api/usage-pace/start", json={"cwd": "/etc"}).status_code == 400
