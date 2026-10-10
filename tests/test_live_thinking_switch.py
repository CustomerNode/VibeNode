"""Changing thinking or model on a busy session must never be refused.

Reported 2026-10-08: picking a thinking level (or model + thinking) while a
session was working showed "Session is busy. Change thinking once the current
turn finishes."  The level is the CLI's effort, which used to be launch-only,
so the browser restarted the CLI to change it and refused while a turn ran.

Now (``_applyThinkingChange`` in ``static/js/invoke-workforce.js``):

* the change is made IN PLACE through ``set_session_effort`` (the daemon sends
  the CLI's ``apply_flag_settings``), mid-turn included, with no restart;
* when the Session Engine or CLI predates that, the CLI is relaunched with
  ``--effort``, but only when that cuts nothing short: a running turn, a
  question, a pending wake-up, an auto-retry countdown (which also continues a
  usage-limited turn) or a queued message.  Otherwise the relaunch is QUEUED
  until the session is free, and a session the user puts to sleep is never
  woken by it;
* the same queue holds the model switch's restart for a CLI too old for the
  chosen model, which used to sleep a working session mid-turn.

The page under test is the REAL ``templates/index.html`` with every real
script.  Only the network is faked: ``/api`` calls get canned replies and the
Socket.IO client is a stub that records emits; the test delivers "server
pushes" to the real handlers.
"""

from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
STATIC = ROOT / "static"
TEMPLATES = ROOT / "templates"

ORIGIN = "http://localhost:59174"
PROJECT = "C--test-proj"
CWD = "C:\\test\\proj"
SID = "aaaaaaaa-1111-4111-8111-000000000001"

_CTYPES = {
    ".js": "application/javascript", ".css": "text/css", ".svg": "image/svg+xml",
    ".png": "image/png", ".json": "application/json", ".woff2": "font/woff2",
}

ROW = {
    "id": SID, "display_title": "Busy session", "custom_title": "Busy session",
    "user_named": True, "last_activity": "Oct 08, 2026  10:00 AM",
    "last_activity_ts": 1791456000, "effective_ts": 1791456000, "sort_ts": 1791456000,
    "size": "10 KB", "file_bytes": 10000, "message_count": 4, "preview": "x",
}

MODELS = [
    {"id": "claude-opus-5-5", "name": "Opus 5.5", "desc": "x"},
    {"id": "claude-opus-4-7", "name": "Opus 4.7", "desc": "x"},
]

# Stand-in for the Socket.IO client: records emits, never connects, and keeps
# the listeners so the test can deliver server pushes to the real handlers.
FAKE_IO = r"""
window.io = function () {
  const handlers = {};
  const s = {
    connected: false, active: false,
    io: {opts: {}, _readyState: 'closed', on() {}, off() {}},
    on(ev, fn) { (handlers[ev] = handlers[ev] || []).push(fn); return s; },
    once(ev, fn) { return s.on(ev, fn); },
    off(ev, fn) { if (handlers[ev]) handlers[ev] = fn ? handlers[ev].filter(f => f !== fn) : []; return s; },
    listeners(ev) { return (handlers[ev] || []).slice(); },
    emit(ev, d) { window.__emits.push([ev, d]); return s; },
    connect() { return s; },
    disconnect() { return s; },
  };
  return s;
};
"""

INIT = """
localStorage.setItem('activeProject', '%s');
localStorage.setItem('viewMode', 'sessions');
localStorage.setItem('sessionDisplayMode', 'grid');
window.__emits = [];
window.__push = (ev, d) => { for (const fn of socket.listeners(ev)) fn(d); };
""" % PROJECT

SETTLE_MS = 1500 + 400   # _TURN_END_SETTLE_MS plus margin


def _index_html() -> str:
    from jinja2 import Environment, FileSystemLoader

    env = Environment(loader=FileSystemLoader(str(TEMPLATES)))
    return env.get_template("index.html").render(
        versioned_static=lambda f: f"/static/{f}", a2hs_title="VibeNode")


@pytest.fixture(scope="module")
def browser():
    pw = pytest.importorskip("playwright.sync_api", reason="playwright not installed")
    with pw.sync_playwright() as p:
        try:
            b = p.chromium.launch()
        except Exception as exc:  # browser binary not downloaded on this machine
            pytest.skip(f"chromium unavailable: {str(exc).splitlines()[0][:80]}")
        yield b
        b.close()


class App:
    """One loaded VibeNode page with SID in the session list."""

    def __init__(self, browser):
        self.errors: list[str] = []
        self._index = _index_html()
        self.ctx = browser.new_context(viewport={"width": 1400, "height": 900})
        self.ctx.add_init_script(INIT)
        self.page = self.ctx.new_page()
        self.page.on("pageerror", lambda e: self.errors.append(str(e)))
        self.page.route("**/*", self._route)
        self.page.goto(ORIGIN + "/", wait_until="domcontentloaded")
        self.page.wait_for_function(
            "document.querySelector('#workforce-grid .wf-card') !== null", timeout=20000)

    def _route(self, route, request):
        url = request.url
        if url.startswith("https://cdn.socket.io/"):
            return route.fulfill(body=FAKE_IO, content_type="application/javascript")
        if not url.startswith(ORIGIN):
            return route.abort()
        path = url[len(ORIGIN):].split("?")[0]
        if path == "/":
            return route.fulfill(body=self._index, content_type="text/html")
        if path.startswith("/static/"):
            f = STATIC / path[len("/static/"):]
            if f.is_file():
                return route.fulfill(body=f.read_bytes(),
                                     content_type=_CTYPES.get(f.suffix, "application/octet-stream"))
            return route.fulfill(status=404, body="")
        if path.startswith("/api/projects"):
            return route.fulfill(content_type="application/json", body=json.dumps(
                [{"encoded": PROJECT, "display": CWD, "session_count": 1}]))
        if path.startswith("/api/sessions"):
            return route.fulfill(content_type="application/json", body=json.dumps([ROW]))
        if path == "/api/models":
            return route.fulfill(content_type="application/json", body=json.dumps(MODELS))
        if path.startswith("/api/"):
            return route.fulfill(content_type="application/json", body="{}")
        return route.fulfill(status=404, body="")

    # ── driving the page ────────────────────────────────────────────────
    def js(self, expr, arg=None):
        return self.page.evaluate(expr, arg)

    def session(self, state="working", effort="medium", live=False, model=None):
        """Put SID in `state` as far as the page knows, at `effort`, with the
        socket connected."""
        self.js("""([sid, state, effort, live, model]) => {
            socket.connected = true;
            if (live) liveSessionId = sid;
            if (state === 'stopped') { runningIds.delete(sid); delete sessionKinds[sid]; }
            else { runningIds.add(sid); sessionKinds[sid] = state; }
            SessionModel.ingestConfirmedThinking(sid, effort);
            if (model) SessionModel.ingestConfirmed(sid, model);
            window.__emits.length = 0;
        }""", [SID, state, effort, live, model])

    def push(self, event, data):
        self.js("([e, d]) => window.__push(e, d)", [event, data])

    def state(self, state, **extra):
        self.push("session_state", {"session_id": SID, "state": state, **extra})

    def old_engine(self, level):
        """The reply a Session Engine from before the live change produces
        (DaemonClient maps its "Unknown method"); like every reply from
        ws_events it names the level it answers."""
        self.push("session_effort_result", {"ok": False, "session_id": SID, "effort": level,
                                            "live_unavailable": True, "error": "old engine"})

    def ok(self, level, applied=None):
        self.push("session_effort_result", {"ok": True, "session_id": SID, "effort": level,
                                            "applied": applied if applied is not None else level})

    def wait_settle(self):
        self.page.wait_for_timeout(SETTLE_MS)

    def emits(self, name):
        return [d for ev, d in self.js("window.__emits") if ev == name]

    def clear_emits(self):
        self.js("() => { window.__emits.length = 0; }")

    def toast(self):
        return self.js("document.getElementById('toast').textContent")

    def queued(self):
        return self.js("(sid) => _relaunchQueue[sid] ? Object.assign({}, _relaunchQueue[sid], "
                       "{settle: null, onState: null, onSnap: null}) : null", SID)

    def queue_while_busy(self, level):
        """Ask for `level` on the busy session against an old engine."""
        self.js("([sid, lvl]) => _applyThinkingChange(sid, {level: lvl})", [SID, level])
        self.old_engine(level)
        assert self.queued() is not None
        self.clear_emits()

    def close(self):
        self.ctx.close()


@pytest.fixture
def app(browser):
    a = App(browser)
    yield a
    assert not a.errors, f"page errors: {a.errors}"
    a.close()


def test_refusal_message_is_gone():
    """The exact refusal the user hit must not come back (the comment that
    documents it may quote it; no code may show it)."""
    src = (STATIC / "js" / "invoke-workforce.js").read_text(encoding="utf-8")
    code = [ln for ln in src.splitlines() if not ln.strip().startswith("//")]
    assert not [ln for ln in code if "Session is busy" in ln]


# ── 1. The live path: the reported case ─────────────────────────────────

@pytest.mark.slow
def test_busy_session_changes_thinking_in_place(app):
    """The reported case, through the status panel's real Apply path."""
    app.session("working", effort="medium", live=True)
    app.js("() => _openSessionModelSelector(true, undefined, {thinking: 'high', thinkingTouched: true})")
    assert app.emits("set_session_effort") == [{"session_id": SID, "effort": "high"}]
    assert app.emits("close_session") == []          # nothing restarted, the turn keeps going
    assert "busy" not in app.toast().lower()
    app.ok("high")
    assert app.toast() == "Thinking set to High"
    assert app.js("(sid) => SessionModel.getConfirmedThinking(sid)", SID) == "high"
    assert app.js("(sid) => SessionModel.resumeThinking(sid)", SID) == "high"


@pytest.mark.slow
def test_broadcast_confirms_when_the_reply_is_lost(app):
    """A mobile reconnect can drop the reply; the broadcast must confirm."""
    app.session("working")
    app.js("(sid) => _applyThinkingChange(sid, {level: 'low'})", SID)
    app.push("session_effort_changed", {"session_id": SID, "effort": "low", "applied": "low"})
    assert app.toast() == "Thinking set to Low"
    assert app.queued() is None and app.emits("close_session") == []


@pytest.mark.slow
def test_clamped_level_says_what_runs(app):
    app.session("working")
    app.js("(sid) => _applyThinkingChange(sid, {level: 'xhigh'})", SID)
    app.ok("xhigh", applied="high")
    assert app.toast() == "Thinking set to xHigh (this model runs it as High)"


@pytest.mark.slow
def test_reply_to_an_earlier_pick_does_not_settle_a_later_one(app):
    """Two quick picks on one session: each reply settles only its own."""
    app.session("working")
    app.js("(sid) => _applyThinkingChange(sid, {level: 'high'})", SID)
    app.js("(sid) => _applyThinkingChange(sid, {level: 'low'})", SID)
    app.ok("high")
    assert app.toast() == "Thinking set to High"
    app.ok("low")
    assert app.toast() == "Thinking set to Low"
    assert app.js("(sid) => SessionModel.getConfirmedThinking(sid)", SID) == "low"


@pytest.mark.slow
def test_combined_change_runs_its_own_model_switch(app):
    """Model + thinking from the status panel: the model switch chained after
    the thinking reply belongs to THIS Apply, even when another picker was
    opened meanwhile, and that picker is left open."""
    app.session("working", live=True, model="claude-opus-4-7")
    app.js("() => _openSessionModelSelector(true, undefined, "
           "{model: 'claude-opus-5-5', thinking: 'high', thinkingTouched: true})")
    assert app.emits("set_session_effort") == [{"session_id": SID, "effort": "high"}]
    # The user opens the full picker before the reply arrives.
    app.js("() => _openSessionModelSelector(true)")
    app.page.wait_for_function("document.querySelector('#pm-overlay #sm-model-list .msel-row') !== null")
    app.ok("high")
    assert app.emits("set_session_model") == [{"session_id": SID, "model": "claude-opus-5-5"}]
    app.push("session_model_result", {"ok": True, "session_id": SID, "model": "claude-opus-5-5"})
    assert app.toast() == ("Thinking set to High · Model switched to Opus 5.5"
                           " — applies from the next message")
    app.page.wait_for_timeout(300)                   # past _closePm's 150ms exit
    assert app.js("document.querySelector('#pm-overlay .pm-card') !== null")


@pytest.mark.slow
def test_a_late_reply_never_closes_an_unrelated_dialog(app):
    app.session("working", live=True)
    app.js("() => _openSessionModelSelector(true)")
    app.page.wait_for_function("document.querySelector('#sm-thinking-list .msel-row') !== null")
    app.js("() => { _smSelectThinking(document.querySelector('#sm-thinking-list [data-level=\"high\"]'));"
           " _applyLiveSessionChoice(); }")
    assert app.emits("set_session_effort") == [{"session_id": SID, "effort": "high"}]
    app.js("() => { window.__confirm = showConfirm('Something else', 'Unrelated'); }")
    app.ok("high")
    app.page.wait_for_timeout(300)
    assert app.js("document.querySelector('#pm-overlay .pm-title').textContent") == "Something else"


# ── 2. The relaunch fallback (an engine or CLI from before the live change) ──

@pytest.mark.slow
def test_old_engine_queues_the_relaunch_until_the_session_is_free(app):
    """The relaunch waits for the turn, then for any queued message, ignores an
    idle flicker, and retries in place before relaunching."""
    app.session("working")
    app.js("(sid) => _applyThinkingChange(sid, {level: 'high', cwd: 'C:\\\\test\\\\proj'})", SID)
    app.old_engine("high")
    assert app.emits("close_session") == []
    assert app.toast() == "Thinking switches to High when this turn finishes"
    assert app.queued()["level"] == "high"

    # The turn ends with a message queued: it runs first.
    app.state("idle", queue=[{"text": "next"}])
    app.wait_settle()
    assert app.emits("set_session_effort")[1:] == [] and app.emits("close_session") == []
    app.state("working")
    # A quick idle flicker (queue dispatch, post-turn wake-up) must not trigger it.
    app.state("idle", queue=[])
    app.state("working")
    app.wait_settle()
    assert app.emits("close_session") == []

    # Truly free: it tries in place once more, then relaunches.
    app.clear_emits()
    app.state("idle", queue=[])
    app.wait_settle()
    assert app.emits("set_session_effort") == [{"session_id": SID, "effort": "high"}]
    app.old_engine("high")
    assert app.emits("close_session") == [{"session_id": SID}]
    app.state("stopped")
    starts = app.emits("start_session")
    assert len(starts) == 1
    assert starts[0]["resume"] is True and starts[0]["thinking_level"] == "high"
    assert starts[0]["session_id"] == SID
    app.state("idle", effort="high")
    assert app.toast() == "Thinking set to High. Applies from the next message."
    assert app.queued() is None


@pytest.mark.slow
def test_engine_restarted_meanwhile_applies_in_place_without_a_relaunch(app):
    app.session("working")
    app.queue_while_busy("max")
    app.state("idle", queue=[])
    app.wait_settle()
    assert app.emits("set_session_effort") == [{"session_id": SID, "effort": "max"}]
    app.ok("max")
    assert app.toast() == "Thinking set to Max"
    assert app.emits("close_session") == [] and app.queued() is None


@pytest.mark.slow
@pytest.mark.parametrize("busy", [
    {"retry_at": "future"},                  # auto-retry / usage-limit auto-continue
    {"substatus": "auto-resuming"},          # background work a wake-up waits on
])
def test_queued_relaunch_never_cancels_pending_work(app, busy):
    """close_session cancels an auto-retry countdown and kills background work,
    so an idle session holding either is not free."""
    if busy.get("retry_at") == "future":
        busy = {"retry_at": time.time() + 600, "retry_attempt": 1, "retry_max": 5}
    app.session("working")
    app.queue_while_busy("high")
    app.state("idle", queue=[], **busy)
    app.wait_settle()
    assert app.emits("set_session_effort") == [] and app.emits("close_session") == []
    app.state("idle", queue=[], retry_at=0, substatus="")
    app.wait_settle()
    assert app.emits("set_session_effort") == [{"session_id": SID, "effort": "high"}]


@pytest.mark.slow
def test_idle_session_with_a_retry_pending_is_not_relaunched_at_once(app):
    app.session("idle")
    app.state("idle", queue=[], retry_at=time.time() + 600, retry_attempt=1, retry_max=5)
    app.clear_emits()
    app.js("(sid) => _applyThinkingChange(sid, {level: 'low'})", SID)
    app.old_engine("low")
    assert app.emits("close_session") == []
    assert app.queued() is not None


@pytest.mark.slow
def test_idle_session_on_old_engine_relaunches_at_once(app):
    app.session("idle")
    app.js("(sid) => _applyThinkingChange(sid, {level: 'low'})", SID)
    app.old_engine("low")
    assert app.emits("close_session") == [{"session_id": SID}]
    assert app.queued() is None


@pytest.mark.slow
def test_sleep_in_the_settle_window_is_never_undone(app):
    """Sleep must stick: the user sleeps the session just as the turn ends."""
    app.session("working")
    app.queue_while_busy("high")
    app.state("idle", queue=[])
    # What closeSession() does before its close_session reaches the server.
    app.js("(sid) => { markUserStopped(sid); runningIds.delete(sid); }", SID)
    app.wait_settle()
    assert app.emits("set_session_effort") == []
    assert app.emits("close_session") == [] and app.emits("start_session") == []


@pytest.mark.slow
def test_a_send_in_the_settle_window_is_not_cut_short(app):
    app.session("working")
    app.queue_while_busy("high")
    app.state("idle", queue=[])
    app.js("(sid) => { sessionKinds[sid] = 'working'; }", SID)   # this tab just sent
    app.wait_settle()
    assert app.emits("set_session_effort") == [] and app.emits("close_session") == []


@pytest.mark.slow
def test_a_queued_change_waits_for_the_wake_after_a_sleep(app):
    """A session put to sleep mid-turn is never woken by the queue; the change
    applies after the user wakes it."""
    app.session("working")
    app.queue_while_busy("max")
    app.js("(sid) => { markUserStopped(sid); runningIds.delete(sid); }", SID)
    app.state("stopped")
    app.wait_settle()
    assert app.emits("close_session") == [] and app.emits("start_session") == []
    assert app.queued()["stopped"] is True
    # The user wakes it and its turn ends.
    app.js("(sid) => clearUserStopped(sid)", SID)
    app.state("working")
    app.state("idle", queue=[])
    app.wait_settle()
    assert app.emits("set_session_effort") == [{"session_id": SID, "effort": "max"}]


@pytest.mark.slow
def test_explicit_change_on_a_sleeping_session_replaces_a_queued_one(app):
    """Waking it for a newer pick must not leave the older queued pick behind
    to replay after the wake."""
    app.session("working")
    app.queue_while_busy("max")
    app.js("(sid) => { runningIds.delete(sid); }", SID)
    app.state("stopped")
    app.js("(sid) => _applyThinkingChange(sid, {level: 'low'})", SID)
    starts = app.emits("start_session")
    assert len(starts) == 1 and starts[0]["thinking_level"] == "low"
    assert app.queued() is None


@pytest.mark.slow
def test_late_live_success_cancels_the_queued_relaunch(app):
    app.session("working")
    app.queue_while_busy("high")
    app.push("session_effort_changed", {"session_id": SID, "effort": "high", "applied": "high"})
    assert app.queued() is None
    app.state("idle", queue=[])
    app.wait_settle()
    assert app.emits("set_session_effort") == [] and app.emits("close_session") == []


@pytest.mark.slow
def test_cancelling_a_queued_change(app):
    """The status panel's Cancel on a queued change (data-act="unqueue")."""
    app.session("working")
    app.queue_while_busy("high")
    assert app.js("(sid) => _cancelQueuedRelaunch(sid)", SID) is True
    app.state("idle", queue=[])
    app.wait_settle()
    assert app.emits("set_session_effort") == [] and app.emits("close_session") == []


@pytest.mark.slow
def test_relaunch_reports_failure_when_the_start_does_not_take(app):
    """This tab thought the session was asleep; the daemon still had it, so the
    start did not take and the push is the session as it was."""
    app.session("stopped", effort="medium")
    app.js("(sid) => _applyThinkingChange(sid, {level: 'low'})", SID)
    assert len(app.emits("start_session")) == 1
    app.state("idle", effort="medium")
    assert app.toast() == "Thinking NOT changed: the session is still at Medium"
    assert app.js("(sid) => SessionModel.getConfirmedThinking(sid)", SID) == "medium"


@pytest.mark.slow
def test_sleeping_session_resumes_with_the_level(app):
    app.session("stopped")
    app.js("(sid) => _applyThinkingChange(sid, {level: 'low'})", SID)
    assert app.emits("set_session_effort") == []
    starts = app.emits("start_session")
    assert len(starts) == 1 and starts[0]["thinking_level"] == "low" and starts[0]["resume"] is True


@pytest.mark.slow
def test_no_reply_falls_back_without_refusing(app):
    """A web server from before the live change has no handler and never
    replies; after the reply timeout the change is queued, not refused."""
    app.session("working")
    app.js("(sid) => _applyThinkingChange(sid, {level: 'medium'})", SID)
    app.page.wait_for_timeout(6600)                  # _EFFORT_LIVE_REPLY_MS + margin
    assert app.emits("close_session") == []
    assert app.toast() == "Thinking switches to Medium when this turn finishes"


@pytest.mark.slow
def test_no_reply_while_offline_never_relaunches(app):
    """Offline, a fallback's close + start would be buffered and land after the
    reconnect, possibly on a turn started meanwhile from another device."""
    app.session("idle")
    app.js("(sid) => { _applyThinkingChange(sid, {level: 'medium'}); socket.connected = false; }", SID)
    app.page.wait_for_timeout(6600)
    assert app.emits("close_session") == [] and app.queued() is None
    assert app.toast() == "Connection lost: thinking change not confirmed"


@pytest.mark.slow
def test_model_restart_for_an_old_cli_waits_for_the_turn(app):
    """A CLI spawned before a model existed rejects a live switch to it, and the
    fallback used to sleep the session at once, cutting a running turn short."""
    app.session("working", live=True, model="claude-opus-4-7")
    app.js("() => _openSessionModelSelector(true, undefined, {model: 'claude-opus-5-5'})")
    assert app.emits("set_session_model") == [{"session_id": SID, "model": "claude-opus-5-5"}]
    app.push("session_model_result", {"ok": False, "session_id": SID,
                                      "error": "Model claude-opus-5-5 is not supported"})
    assert app.emits("close_session") == []
    q = app.queued()
    assert q["modelChanged"] is True and q["model"] == "claude-opus-5-5"
    assert app.toast().endswith("The session switches to Opus 5.5 when this turn finishes")
    # The queue is keyed by session, not by what the panel shows; detach the
    # live panel (not rendered in this harness) before pushing its states.
    app.js("() => { liveSessionId = null; }")
    # Free: a model change relaunches directly (no in-place retry for a model).
    app.state("idle", queue=[])
    app.wait_settle()
    assert app.emits("close_session") == [{"session_id": SID}]
    app.state("stopped")
    start = app.emits("start_session")[0]
    assert start["model"] == "claude-opus-5-5" and start["thinking_level"] == "medium"


# ── 3. The usage-limit banner (added 2026-10-09) ────────────────────────
# A Fable session that hits its limit switches to the newest Opus by itself
# (SessionManager._limit_switch_plan); the banner says so during the
# countdown, and its "Switch to" chips are the newest model of each family.

@pytest.mark.slow
def test_limit_chips_offer_the_newest_model_of_each_family(app):
    app.js("""() => { window._statusModels = [
        {id: 'claude-fable-5-1'}, {id: 'claude-opus-5-5'}, {id: 'claude-opus-6'},
        {id: 'claude-sonnet-5'}, {id: 'claude-sonnet-5-5'}, {id: 'claude-haiku-4-5-20251001'}]; }""")
    assert app.js("() => _limitAlternatives('claude-fable-5-1')") == ["claude-opus-6", "claude-sonnet-5-5"]
    assert app.js("() => _limitAlternatives('claude-opus-6')") == [
        "claude-sonnet-5-5", "claude-haiku-4-5-20251001"]


@pytest.mark.slow
def test_limit_chips_fall_back_before_the_model_list_loads(app):
    app.js("() => { window._statusModels = undefined; window._statusModelsLoading = true; }")
    assert app.js("() => _limitAlternatives('claude-fable-5-1')") == ["claude-opus-5-5", "claude-sonnet-5"]


@pytest.mark.slow
@pytest.mark.parametrize("switch_to, expected", [
    ("claude-opus-5-5[1m]", 'switching to <strong style="color:var(--text);">Opus 5.5</strong> in'),
    # A bare alias: the family, not the legacy alias label ("Opus 4.6").
    ("opus[1m]", 'switching to <strong style="color:var(--text);">Opus</strong> in'),
    ("", "continues automatically in"),
])
def test_limit_banner_says_what_happens_next(app, switch_to, expected):
    html = app.js("""([sid, sw]) => {
        window._sessionRetryState = window._sessionRetryState || {};
        window._sessionRetryState[sid] = {retry_at: Date.now() / 1000 + 5};
        return _buildUsageLimitBanner(sid, {limited_model: 'claude-fable-5-1',
            limit_reset_at: Date.now() / 1000 + 3600, limit_switch_to: sw});
    }""", [SID, switch_to])
    assert expected in html
    assert "[1m]" not in html
