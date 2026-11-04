"""A Working summary moves while its job runs.

A Working summary must not sit static while its job runs: it shows a clock,
stages and a live feed.

- The countdown draws from needs.eta_minutes, or an estimate in words the
  card can count down ("about 40 minutes", "1h 20m", "4:30 pm").
- A Working summary rewritten with the same promise (the session is woken by
  each job event) keeps the first one's start, so it can run out.
- needs.stages / needs.stage draw a stage track; the job's events in the
  thread (automated messages) fill in the feed and move the stage.
- The stop asks for a countable estimate and checks the stages' shape; the
  stages are outside the card's 120 words and its four-item list limit.

Rules run in Python; the card runs the real ``last-card.js`` in headless
Chromium through Playwright (skips without it).
"""
import copy
import json
import time
from pathlib import Path

import pytest

from engine import handoff_rules as hr
from tests._browser import shared_browser_scope

ROOT = Path(__file__).resolve().parents[1]
LAST_CARD = ROOT / "src" / "web" / "static" / "js" / "last-card.js"

STAGES = ["Preflight", "Security scan", "Unit tests", "Site tests", "Goes live"]


def _working(**needs):
    n = {"kind": "none", "why": "The release gate runs by itself.", "next": "It goes live, then I report."}
    n.update(needs)
    return {"type": "working", "title": "Release", "headline": "The release gate is running.",
            "context": "Shipping the backup fix.", "zoom": {"big": "Ship it", "task": "Release", "now": "Gate running"},
            "status": {"done": ["Fix in"], "remaining": ["Gate passes"]}, "needs": n}


def _rule(card):
    return [p for p in hr.format_problems(card) if "eta" in p or "stages" in p]


# ── The stop ────────────────────────────────────────────────────────────

@pytest.mark.parametrize("needs", [
    {"eta_minutes": 40}, {"eta": "about 40 minutes"}, {"eta": "~12 min"}, {"eta": "1h 20m"},
    {"eta": "4:30 pm"}, {"eta": "half an hour"}, {"eta_minutes": 40, "stages": STAGES, "stage": "Unit tests"}])
def test_a_countable_estimate_passes(needs):
    assert _rule(_working(**needs)) == []


@pytest.mark.parametrize("needs", [{}, {"eta": "soon"}, {"eta": "One to two hours."}, {"eta_minutes": 0}])
def test_an_estimate_that_cannot_be_counted_down_is_sent_back(needs):
    out = _rule(_working(**needs))
    assert out and "eta_minutes" in out[0]


@pytest.mark.parametrize("stages", [["Only one"], "Preflight, tests", ["a"] * 9, ["Four words is too long"]])
def test_badly_shaped_stages_are_sent_back(stages):
    assert any("needs.stages" in p for p in _rule(_working(eta_minutes=10, stages=stages)))


def test_stages_are_outside_the_word_count_and_the_list_limit():
    base = _working(eta_minutes=10)
    staged = copy.deepcopy(base)
    staged["needs"].update(stages=["Stage " + str(i) for i in range(8)], stage="Stage 2")
    assert hr.card_words(staged) == hr.card_words(base)
    assert not any("needs.stages" in p for p in hr.length_problems(staged))


def test_the_instructions_ask_for_minutes_and_stages():
    assert "needs.eta_minutes is required" in hr.INSTRUCTIONS
    assert "needs.stages" in hr.INSTRUCTIONS


# ── The card, in a real browser ─────────────────────────────────────────

@pytest.fixture(scope="module")
def browser():
    with shared_browser_scope() as b:
        yield b


def _card_msg(card, ts):
    return ("<div class=\"msg assistant\" data-ts=\"" + str(ts) + "\"><div class=\"msg-body\"><div class=\"vn-card-ph\" data-card='"
            + json.dumps(card).replace("'", "&#39;") + "'></div></div></div>")


def _event(text, ts):
    return ("<div class=\"live-entry vn-auto-msg\" data-ts=\"" + str(ts) + "\" data-event=\"" + text + "\">"
            "<span class=\"vn-auto-time\">3:2" + str(ts % 10) + " PM</span><div class=\"vn-auto-body\">Monitor event</div></div>")


def _open(browser, tmp_path, parts):
    f = tmp_path / "live.html"
    f.write_text(f"""<!doctype html><html><head><meta charset="utf-8"></head><body><div id="live-log">{''.join(parts)}</div>
<script>var liveSessionId='s1', sessionKinds={{s1:'idle'}}; window.fetch=()=>Promise.reject(new Error('offline'));</script>
<script src="{LAST_CARD.as_uri()}"></script>
<script>VNLastCard.renderIn(document.getElementById('live-log'));</script></body></html>""", encoding="utf-8")
    page = browser.new_page()
    errors = []
    page.on("pageerror", lambda e: errors.append(str(e)))
    page.goto(f.as_uri(), wait_until="load")
    assert not errors, errors
    page.wait_for_function("document.querySelectorAll('.lm.skim').length >= 1")
    page.wait_for_timeout(50)
    return page


def test_an_estimate_in_words_draws_the_ticking_countdown(browser, tmp_path):
    now = int(time.time())
    page = _open(browser, tmp_path, [_card_msg(_working(eta="about 40 minutes"), now - 120)])
    try:
        assert page.locator(".lm-timing").count() == 1
        page.wait_for_function("document.querySelector('[data-f=since]').textContent.startsWith('2:')")
        assert page.locator(".lm-eta").count() == 0
    finally:
        page.close()


def test_the_same_promise_rewritten_keeps_one_countdown_and_the_feed_moves_the_stages(browser, tmp_path):
    now = int(time.time())
    card = _working(eta_minutes=10, stages=STAGES, stage="Preflight")
    parts = [_card_msg(card, now - 900), _event("== 15:20:01 [+60s] preflight ok", now - 800),
             _card_msg(card, now - 790), _event("Security scan clean", now - 700),
             _event("Unit tests: 1,200 of 4,100", now - 30)]
    page = _open(browser, tmp_path, parts)
    try:
        # 15 minutes since the first card against a 10-minute promise: overdue.
        page.wait_for_function("document.querySelectorAll('.tm.is-over').length >= 1")
        newest = page.locator(".lm.skim").last
        assert newest.locator("[data-f=dlabel]").inner_text() == "overdue"
        # Only the newest summary is live.
        assert page.locator(".lm.skim").first.locator(".lm-live").is_hidden()
        feed = newest.locator(".lm-live .feed li span")
        assert feed.all_inner_texts() == ["Unit tests: 1,200 of 4,100", "Security scan clean", "preflight ok"]
        cls = newest.locator(".st-track .s").evaluate_all("els => els.map(e => e.className)")
        assert cls == ["s done", "s done", "s now", "s", "s"]
        assert newest.locator(".feed li .ago").first.inner_text().endswith("ago")
    finally:
        page.close()


def test_a_new_event_lands_in_the_feed_without_a_redraw(browser, tmp_path):
    now = int(time.time())
    page = _open(browser, tmp_path, [_card_msg(_working(eta_minutes=30, stages=STAGES), now - 60)])
    try:
        assert page.locator(".lm-live .feed li.empty").count() == 1
        page.evaluate("ts => document.getElementById('live-log').insertAdjacentHTML('beforeend', "
                      "'<div class=\"live-entry vn-auto-msg\" data-ts=\"' + ts + '\" data-event=\"Site tests passed\">"
                      "<div class=\"vn-auto-body\">x</div></div>')", now)
        page.wait_for_function("document.querySelector('.lm-live .feed li span').textContent === 'Site tests passed'")
        cls = page.locator(".st-track .s").evaluate_all("els => els.map(e => e.className)")
        assert cls[:3] == ["s done"] * 3 and cls[3] == "s done" and cls[4] == "s now"
    finally:
        page.close()


def _said(text, ts):
    return ("<div class=\"msg user\" data-ts=\"" + str(ts) + "\"><div class=\"msg-body\">" + text + "</div></div>")


def _script_event(text, ts):
    return ("<div class=\"msg user vn-event-msg\" data-ts=\"" + str(ts) + "\"><div class=\"msg-role\">Release page "
            "<span class=\"msg-time\">3:30 PM</span></div><div class=\"msg-body msg-content\">" + text + "</div></div>")


def test_a_revised_estimate_keeps_the_jobs_updates_in_the_feed(browser, tmp_path):
    # Each event wakes the session, which writes a new Working summary right
    # after it, often with a revised estimate.  The newest summary must still
    # show the updates since the user's last message, never "Waiting for the
    # first update".
    now = int(time.time())
    parts = [_said("ship it", now - 1000), _event("Older run: build ok", now - 1200),
             _card_msg(_working(eta_minutes=20, stages=STAGES), now - 900),
             _event("== 15:20:01 [+60s] preflight ok", now - 800),
             _card_msg(_working(eta_minutes=15, stages=STAGES), now - 790),
             _script_event("app v853: release started", now - 300),
             _card_msg(_working(eta_minutes=10, stages=STAGES), now - 290)]
    parts.insert(1, parts.pop(0))   # the older event sits before his message
    page = _open(browser, tmp_path, parts)
    try:
        newest = page.locator(".lm.skim").last
        page.wait_for_function("document.querySelectorAll('.lm-live .feed li[data-at]').length >= 1")
        assert newest.locator(".lm-live .feed li.empty").count() == 0
        assert newest.locator(".lm-live .feed li span").all_inner_texts() == ["app v853: release started", "preflight ok"]
    finally:
        page.close()


# ── The estimate watch (watchdog job 7, src/engine/eta_watch.py) ────────────

from types import SimpleNamespace

from engine import eta_watch
from engine.health_monitor import HealthMonitor
from engine.session_manager import SessionState
from tests.test_health_monitor import FakeManager


def _asst(card, ts):
    return SimpleNamespace(kind="asst", text="```handoff\n" + json.dumps(card) + "\n```", timestamp=ts, auto="")


def _idle(sid, entries):
    return SimpleNamespace(session_id=sid, state=SessionState.IDLE, entries=entries, name="Release",
                           _interrupted=False, created_ts=0.0, _task_started_at=0.0, _bg_work_at=0.0)


def _mon():
    sm = FakeManager()
    sm.pushes = []
    sm._push_callback = lambda ev, data: sm.pushes.append((ev, data))
    mon = HealthMonitor(sm)
    mon._announce = lambda info, text, is_error=False: None
    return sm, mon


def test_words_and_numbers_both_make_a_promise():
    assert eta_watch.promise(_working(eta="about 40 minutes"))[1] == 40
    assert eta_watch.promise(_working(eta="1h 20m"))[1] == 80
    assert eta_watch.promise(_working(eta_minutes=12))[1] == 12
    late = dict(_working(eta_minutes=12), late={"apology": "a", "why": "w", "new_eta_minutes": 5})
    assert eta_watch.promise(late)[1] == 5
    assert eta_watch.promise(_working(eta="4:30 pm")) is None      # the card counts that one down
    assert eta_watch.promise(dict(_working(eta_minutes=5), type="done")) is None


def test_a_promise_kept_on_time_is_left_alone():
    sm, mon = _mon()
    now = time.time()
    sm._sessions["s1"] = _idle("s1", [_asst(_working(eta_minutes=10), now - 300)])
    mon._check_eta(sm._sessions["s1"], now)
    assert sm.sent == [] and sm.pushes == []


def test_a_missed_estimate_asks_the_session_once_then_alerts_the_owner_on_the_second():
    sm, mon = _mon()
    now = time.time()
    card = _working(eta_minutes=10)
    # Rewritten on each job event with the same promise: the clock is the first one's.
    sm._sessions["s1"] = info = _idle("s1", [_asst(card, now - 900)])
    mon._check_eta(info, now - 890)          # first sight: remembers the first card's time
    info.entries.append(_asst(card, now - 60))   # rewritten after a job event, same promise
    mon._check_eta(info, now)
    assert len(sm.sent) == 1 and "about 10 minutes" in sm.sent[0][1] and "`late` block" in sm.sent[0][1]
    assert sm.sent[0][1].startswith(eta_watch.PREFIX) and sm.pushes == []
    info.state = SessionState.IDLE
    mon._check_eta(info, now + 60)           # same promise: never twice
    assert len(sm.sent) == 1
    # The late card gives a new time, and misses it too: firmer, and the owner hears.
    late = dict(card, late={"apology": "Sorry", "why": "Slow tests", "new_eta_minutes": 5, "confidence": "fair"})
    info.entries = [_asst(late, now + 100)]
    mon._check_eta(info, now + 100 + 5 * 60 + eta_watch.GRACE_SECONDS + 1)
    assert len(sm.sent) == 2 and "second time" in sm.sent[1][1]
    assert sm.pushes and sm.pushes[0][0] == "session_stalled"


def test_a_stopped_session_or_his_own_message_is_left_alone():
    sm, mon = _mon()
    now = time.time()
    info = _idle("s1", [_asst(_working(eta_minutes=1), now - 3600)])
    info._interrupted = True
    mon._check_eta(info, now)
    info._interrupted = False
    info.entries.append(SimpleNamespace(kind="user", text="hold on", timestamp=now - 10, auto=""))
    mon._check_eta(info, now)
    assert sm.sent == []


# ── What the running job is printing (GET /api/session-live/<sid>) ──────

def _transcript(tmp_path, out_file, done=False, desc="Run the release"):
    lines = [
        {"type": "assistant", "message": {"content": [{"type": "tool_use", "id": "tu1", "name": "Bash",
            "input": {"command": "./release.sh", "description": desc, "run_in_background": True}}]}},
        {"type": "user", "message": {"content": [{"type": "tool_result", "tool_use_id": "tu1",
            "content": f"Command running in background with ID: bxyz123. Output is being written to: {out_file}. You will be notified."}]}},
    ]
    if done:
        lines.append({"type": "user", "message": {"content": "<task-notification>\n<task-id>bxyz123</task-id>\n</task-notification>"}})
    p = tmp_path / "s1.jsonl"
    p.write_text("\n".join(json.dumps(x) for x in lines) + "\n", encoding="utf-8")
    return p


def test_the_route_reads_the_last_lines_of_running_work_only(tmp_path, monkeypatch):
    from web.routes import live_api
    root = tmp_path / "claude-tmp"
    (root / "tasks").mkdir(parents=True)
    out = root / "tasks" / "bxyz123.output"
    out.write_text("step 1\n\x1b[32mUnit tests: 1,200 of 4,100\x1b[0m\n\nSite tests started\n", encoding="utf-8")
    monkeypatch.setattr(live_api, "_live_roots", lambda: [root.resolve()])
    got = live_api._read_session_live(_transcript(tmp_path, out))
    assert got and got[0]["what"] == "Run the release"
    assert got[0]["lines"] == ["step 1", "Unit tests: 1,200 of 4,100", "Site tests started"]
    # finished: by its notice, or by its last line
    assert live_api._read_session_live(_transcript(tmp_path, out, done=True)) == []
    out.write_text("ok\n[exited with code 0]\n", encoding="utf-8")
    assert live_api._read_session_live(_transcript(tmp_path, out)) == []


def _write(tmp_path, lines):
    p = tmp_path / "s2.jsonl"
    p.write_text("\n".join(json.dumps(x) for x in lines) + "\n", encoding="utf-8")
    return p


def test_a_job_that_writes_its_own_log_shows_that_log(tmp_path, monkeypatch):
    # The usual shape: `./gate.sh > /tmp/gate.log 2>&1` leaves the CLI's
    # output file empty for the whole run, which is why the feed showed
    # nothing.  The feed follows the log the command writes instead.
    from web.routes import live_api
    root = tmp_path / "claude-tmp"
    (root / "tasks").mkdir(parents=True)
    out = root / "tasks" / "bq1.output"
    out.write_text("", encoding="utf-8")
    log = tmp_path / "gate.log"
    log.write_text("== bootstrap\nBOOTSTRAP OK\n== build\n", encoding="utf-8")
    monkeypatch.setattr(live_api, "_live_roots", lambda: [root.resolve()])
    monkeypatch.setattr(live_api, "_log_roots", lambda: [tmp_path.resolve()])
    t = _write(tmp_path, [
        {"type": "assistant", "message": {"content": [{"type": "tool_use", "id": "tu9", "name": "Bash",
            "input": {"command": f"cd {tmp_path} && ./gate.sh > gate.log 2>&1; echo done",
                      "description": "Run the gate", "run_in_background": True}}]}},
        {"type": "user", "message": {"content": [{"type": "tool_result", "tool_use_id": "tu9",
            "content": f"Command running in background with ID: bq1. Output is being written to: {out}. ok"}]}},
    ])
    got = live_api._read_session_live(t)
    assert got and got[0]["what"] == "Run the gate"
    assert got[0]["lines"] == ["== bootstrap", "BOOTSTRAP OK", "== build"]


def test_a_log_outside_temp_and_home_is_never_read(tmp_path, monkeypatch):
    from web.routes import live_api
    assert live_api._redirect_logs("x > /dev/null 2>&1") == []
    monkeypatch.setattr(live_api, "_log_roots", lambda: [(tmp_path / "nowhere").resolve()])
    log = tmp_path / "secret.log"
    log.write_text("secret\n", encoding="utf-8")
    assert live_api._freshest([log], 0, live_api._log_roots()) is None


def test_a_long_foreground_command_is_running_work(tmp_path, monkeypatch):
    # Without this the summary said "Nothing running in the background" while
    # the session sat in a 20-minute command.
    from web.routes import live_api
    monkeypatch.setattr(live_api, "_log_roots", lambda: [tmp_path.resolve()])
    log = tmp_path / "suite.log"
    log.write_text("Unit tests: 900 of 4,100\n", encoding="utf-8")
    use = {"type": "assistant", "message": {"content": [{"type": "tool_use", "id": "tf1", "name": "Bash",
        "input": {"command": f"./suite.sh > {log} 2>&1", "description": "Run the suite"}}]}}
    got = live_api._read_session_live(_write(tmp_path, [use]))
    assert got and got[0]["what"] == "Run the suite" and got[0]["foreground"] is True and got[0]["no_log"] is False
    assert got[0]["lines"] == ["Unit tests: 900 of 4,100"]
    res = {"type": "user", "message": {"content": [{"type": "tool_result", "tool_use_id": "tf1", "content": "ok"}]}}
    assert live_api._read_session_live(_write(tmp_path, [use, res])) == []


def test_the_route_never_reads_a_file_outside_the_cli_temp_folder(tmp_path, monkeypatch):
    from web.routes import live_api
    secret = tmp_path / "elsewhere" / "x.output"
    secret.parent.mkdir()
    secret.write_text("secret\n", encoding="utf-8")
    monkeypatch.setattr(live_api, "_live_roots", lambda: [(tmp_path / "claude-tmp").resolve()])
    assert live_api._read_session_live(_transcript(tmp_path, secret)) == []


def test_the_summary_shows_what_the_job_is_printing(browser, tmp_path):
    now = int(time.time())
    f = tmp_path / "run.html"
    card = _working(eta_minutes=20)
    f.write_text(f"""<!doctype html><html><head><meta charset="utf-8"></head><body><div id="live-log">{_card_msg(card, now - 60)}</div>
<script>var liveSessionId='s1', sessionKinds={{s1:'working'}}; window.__asked=[];
window.fetch=(u)=>{{ window.__asked.push(u); return Promise.resolve({{ok:true, json:()=>Promise.resolve({{running:[{{id:'b1', what:'Run the release', lines:['Unit tests: 1,200 of 4,100'], quiet_s:3}}]}})}}); }};</script>
<script src="{LAST_CARD.as_uri()}"></script>
<script>VNLastCard.renderIn(document.getElementById('live-log'));</script></body></html>""", encoding="utf-8")
    page = browser.new_page()
    try:
        page.goto(f.as_uri(), wait_until="load")
        page.wait_for_function("document.querySelector('.lm-run .r b') && document.querySelector('.lm-run .r b').textContent === 'Run the release'")
        assert page.locator(".lm-run code").inner_text() == "Unit tests: 1,200 of 4,100"
        assert page.evaluate("window.__asked[0]") == "/api/session-live/s1"
        assert page.locator(".lm-live .feed li.empty").is_hidden()
        assert page.locator(".lm-run .ago").inner_text() in ("printing now", "last output 5s ago", "last output 6s ago")
    finally:
        page.close()


def test_what_the_job_prints_moves_the_stage_track(browser, tmp_path):
    now = int(time.time())
    f = tmp_path / "stages.html"
    card = _working(eta_minutes=20, stages=["Bootstrap", "Build", "Tests", "Release"], stage="Bootstrap")
    f.write_text(f"""<!doctype html><html><head><meta charset="utf-8"></head><body><div id="live-log">{_card_msg(card, now - 60)}</div>
<script>var liveSessionId='s1', sessionKinds={{s1:'working'}};
window.fetch=(u)=>Promise.resolve({{ok:true, json:()=>Promise.resolve({{running:[{{id:'b1', what:'Run the gate', lines:['== build', 'compiling 40 of 90'], quiet_s:1}}]}})}});</script>
<script src="{LAST_CARD.as_uri()}"></script>
<script>VNLastCard.renderIn(document.getElementById('live-log'));</script></body></html>""", encoding="utf-8")
    page = browser.new_page()
    try:
        page.goto(f.as_uri(), wait_until="load")
        page.wait_for_function("document.querySelector('.lm-live .st-track .s.now span') && document.querySelector('.lm-live .st-track .s.now span').textContent === 'Build'")
    finally:
        page.close()


def test_a_command_with_no_log_says_how_long_it_has_run(browser, tmp_path):
    # Its output reaches nobody until it ends, so "last output 16s ago" was false.
    now = int(time.time())
    f = tmp_path / "nolog.html"
    card = _working(eta_minutes=20)
    f.write_text(f"""<!doctype html><html><head><meta charset="utf-8"></head><body><div id="live-log">{_card_msg(card, now - 60)}</div>
<script>var liveSessionId='s1', sessionKinds={{s1:'working'}};
window.fetch=(u)=>Promise.resolve({{ok:true, json:()=>Promise.resolve({{running:[{{id:'t1', foreground:true, no_log:true, what:'Run the suite', lines:[], quiet_s:125}}]}})}});</script>
<script src="{LAST_CARD.as_uri()}"></script>
<script>VNLastCard.renderIn(document.getElementById('live-log'));</script></body></html>""", encoding="utf-8")
    page = browser.new_page()
    try:
        page.goto(f.as_uri(), wait_until="load")
        page.wait_for_function("document.querySelector('.lm-run .ago') && document.querySelector('.lm-run .ago').textContent.startsWith('running 2 min')")
        assert "running for" in page.locator(".tm-figs").inner_text().lower()
    finally:
        page.close()
