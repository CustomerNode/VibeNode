"""Sleep must stick: a user's sleep beats ghost recovery.

``_liveSubmitDirect`` (``static/js/live-panel.js``) arms "ghost recovery" on
every send: if no ``session_state`` arrives within 3s it emits
``close_session`` and, 1.5s later, ``start_session`` with ``resume: true``,
re-sending the prompt.  Its cancel handlers deliberately ignore
``state === 'stopped'`` (a dead session also reports stopped), so the one
event proving the user slept the session could not cancel recovery, and a
Sleep within ~3s of a send was silently undone (fixed 2026-07-20).

The fix is the user-stop intent registry (``markUserStopped`` /
``isUserStopped``), checked when the 3s timer fires, AGAIN inside the 1.5s
close-to-start gap, and in the ``send_message`` error fallback.  Until this
file existed, those checks were guarded only by source greps.

The page under test is the REAL ``templates/index.html`` with every real
script; only the network is faked (canned ``/api`` replies, a Socket.IO
stub that records emits).  The page's clock is Playwright's and is PAUSED
after load, so the timers fire only when a test advances it: a real-time
clock let a slow Playwright round-trip fire the 3s timer before the test
had recorded the sleep (the same race fixed in
``tests/test_live_thinking_switch.py`` on 2026-10-20).
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from tests._browser import shared_browser_scope

# Every test builds its own page, so a parallel run may split this file
# across workers (see tests/_xdist_schedule.py).
XDIST_SPLITTABLE = True

ROOT = Path(__file__).resolve().parents[1]
STATIC = ROOT / "static"
TEMPLATES = ROOT / "templates"

ORIGIN = "http://localhost:59175"
PROJECT = "C--test-proj"
CWD = "C:\\test\\proj"
SID = "bbbbbbbb-2222-4222-8222-000000000002"

_CTYPES = {
    ".js": "application/javascript", ".css": "text/css", ".svg": "image/svg+xml",
    ".png": "image/png", ".json": "application/json", ".woff2": "font/woff2",
}

ROW = {
    "id": SID, "display_title": "Ghost session", "custom_title": "Ghost session",
    "user_named": True, "last_activity": "Oct 28, 2026  10:00 AM",
    "last_activity_ts": 1793188800, "effective_ts": 1793188800, "sort_ts": 1793188800,
    "size": "10 KB", "file_bytes": 10000, "message_count": 4, "preview": "x",
}

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

GHOST_MS = 3000       # _ghostTimer / _ghostTimer2
GAP_MS = 1500         # close_session -> start_session
TTL_MS = 60000        # _USER_STOP_INTENT_TTL_MS


def _index_html() -> str:
    from jinja2 import Environment, FileSystemLoader

    env = Environment(loader=FileSystemLoader(str(TEMPLATES)))
    return env.get_template("index.html").render(
        versioned_static=lambda f: f"/static/{f}", a2hs_title="VibeNode")


@pytest.fixture(scope="module")
def browser():
    with shared_browser_scope() as b:
        yield b


class App:
    """One loaded VibeNode page with SID open as the live session."""

    def __init__(self, browser):
        self.errors: list[str] = []
        self._index = _index_html()
        self.ctx = browser.new_context(viewport={"width": 1400, "height": 900})
        self.ctx.add_init_script(INIT)
        self.page = self.ctx.new_page()
        self.page.on("pageerror", lambda e: self.errors.append(str(e)))
        self.page.route("**/*", self._route)
        self.page.clock.install()
        self.page.goto(ORIGIN + "/", wait_until="domcontentloaded")
        self.page.wait_for_function(
            "document.querySelector('#workforce-grid .wf-card') !== null", timeout=20000)
        # Freeze the clock (a far-future instant, which Playwright always
        # accepts); from here only wait() moves it.
        self.page.clock.pause_at("2100-01-01T00:00:00Z")

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
        if path.startswith("/api/"):
            return route.fulfill(content_type="application/json", body="{}")
        return route.fulfill(status=404, body="")

    # ── driving the page ────────────────────────────────────────────────
    def js(self, expr, arg=None):
        return self.page.evaluate(expr, arg)

    def open(self, running=True):
        """SID is the live session, awake (in runningIds) or not, socket up.
        The wrong-session gate is off: it is not under test and its confirm
        dialog would turn the send into a dialog."""
        self.js("""([sid, running]) => {
            socket.connected = true;
            liveSessionId = sid;
            window._wrongSessionDetectionEnabled = false;
            if (running) { runningIds.add(sid); sessionKinds[sid] = 'idle'; }
            else { runningIds.delete(sid); delete sessionKinds[sid]; }
            window.__emits.length = 0;
        }""", [SID, running])

    def submit(self, text="hello"):
        self.js("([sid, t]) => _liveSubmitDirect(sid, t)", [SID, text])

    def sleep(self):
        """What closeSession() does before its close_session reaches the server."""
        self.js("(sid) => { markUserStopped(sid); runningIds.delete(sid); delete sessionKinds[sid]; }", SID)

    def push(self, event, data):
        self.js("([e, d]) => window.__push(e, d)", [event, data])

    def wait(self, ms):
        self.page.clock.run_for(ms)

    def emits(self, name):
        return [d for ev, d in self.js("window.__emits") if ev == name]

    def user_stopped(self):
        return self.js("(sid) => isUserStopped(sid)", SID)

    def close(self):
        self.ctx.close()


@pytest.fixture
def app(browser):
    a = App(browser)
    yield a
    assert not a.errors, f"page errors: {a.errors}"
    a.close()


# ── 1. Recovery itself still works for a genuinely dead session ──────────

@pytest.mark.slow
def test_dead_session_is_closed_then_restarted(app):
    app.open(running=True)
    app.submit("hello")
    assert len(app.emits("send_message")) == 1
    app.wait(GHOST_MS - 100)
    assert app.emits("close_session") == []
    app.wait(100)
    assert app.emits("close_session") == [{"session_id": SID}]
    assert app.emits("start_session") == []
    app.wait(GAP_MS - 100)
    assert app.emits("start_session") == []
    app.wait(100)
    starts = app.emits("start_session")
    assert len(starts) == 1
    assert starts[0]["session_id"] == SID and starts[0]["resume"] is True
    assert starts[0]["prompt"] == "hello"


# ── 2. The reported bug: a Sleep inside the 3s window must stick ─────────

@pytest.mark.slow
def test_sleep_before_the_ghost_timer_is_never_undone(app):
    app.open(running=True)
    app.submit("hello")
    app.wait(GHOST_MS - 100)
    app.sleep()
    app.wait(TTL_MS)                      # well past both timers
    assert app.emits("close_session") == []
    assert app.emits("start_session") == []


# ── 3. A Sleep inside the close -> start gap: the second check ───────────

@pytest.mark.slow
def test_sleep_inside_the_gap_aborts_the_restart(app):
    app.open(running=True)
    app.submit("hello")
    app.wait(GHOST_MS + 200)
    assert app.emits("close_session") == [{"session_id": SID}]   # recovery began
    app.sleep()                                                   # Sleep mid-recovery
    app.wait(GAP_MS + 1000)
    assert app.emits("start_session") == []


# ── 4. What cancels the timer, and what deliberately does not ────────────

@pytest.mark.slow
def test_a_working_push_cancels_recovery(app):
    app.open(running=True)
    app.submit("hello")
    app.wait(1000)
    app.push("session_state", {"session_id": SID, "state": "working"})
    app.wait(GHOST_MS + GAP_MS + 1000)
    assert app.emits("close_session") == [] and app.emits("start_session") == []


@pytest.mark.slow
def test_a_stopped_push_alone_does_not_cancel_recovery(app):
    """A genuinely dead session also reports stopped; the cause (did the
    user ask?) is the discriminator, not the state."""
    app.open(running=True)
    app.submit("hello")
    app.wait(1000)
    app.push("session_state", {"session_id": SID, "state": "stopped"})
    app.wait(GHOST_MS + GAP_MS + 1000)
    assert app.emits("close_session") == [{"session_id": SID}]
    assert len(app.emits("start_session")) == 1


@pytest.mark.slow
def test_another_sessions_push_does_not_cancel_recovery(app):
    app.open(running=True)
    app.submit("hello")
    app.wait(1000)
    app.push("session_state", {"session_id": "cccccccc-3333-4333-8333-000000000003", "state": "working"})
    app.wait(GHOST_MS + GAP_MS + 1000)
    assert app.emits("close_session") == [{"session_id": SID}]


# ── 5. The not-running path: send_message first, resume on "is stopped" ──

@pytest.mark.slow
def test_not_running_send_resumes_on_a_stopped_error(app):
    app.open(running=False)
    app.submit("hello")
    assert app.emits("send_message") == [{"session_id": SID, "text": "hello"}]
    app.push("error", {"session_id": SID, "message": "Session is stopped"})
    starts = app.emits("start_session")
    assert len(starts) == 1 and starts[0]["resume"] is True and starts[0]["prompt"] == "hello"
    # The fallback fired, so the ghost timer must not ALSO close and restart.
    app.wait(GHOST_MS + GAP_MS + 1000)
    assert app.emits("close_session") == []
    assert len(app.emits("start_session")) == 1


@pytest.mark.slow
def test_not_running_send_honours_a_sleep_on_the_stopped_error(app):
    """"is stopped" is exactly what the daemon reports for a session the
    user just slept."""
    app.open(running=False)
    app.submit("hello")
    app.sleep()
    app.push("error", {"session_id": SID, "message": "Session is stopped"})
    assert app.emits("start_session") == []
    app.wait(GHOST_MS + GAP_MS + 1000)
    assert app.emits("close_session") == [] and app.emits("start_session") == []


@pytest.mark.slow
def test_not_running_path_has_its_own_ghost_recovery_and_sleep_check(app):
    app.open(running=False)
    app.submit("hello")
    app.wait(GHOST_MS + 200)
    assert app.emits("close_session") == [{"session_id": SID}]
    app.sleep()
    app.wait(GAP_MS + 1000)
    assert app.emits("start_session") == []


# ── 6/7. The intent flag itself ──────────────────────────────────────────

@pytest.mark.slow
def test_an_explicit_send_clears_the_sleep_intent(app):
    app.open(running=True)
    app.sleep()
    assert app.user_stopped() is True
    app.open(running=True)                # the user wakes it and sends again
    app.submit("hello")
    assert app.user_stopped() is False
    # ...so recovery is armed normally for THIS send.
    app.wait(GHOST_MS + GAP_MS + 1000)
    assert len(app.emits("start_session")) == 1


@pytest.mark.slow
def test_the_sleep_intent_expires_after_sixty_seconds(app):
    """A stale flag can never permanently disable ghost recovery."""
    app.open(running=True)
    app.sleep()
    app.wait(TTL_MS - 1000)
    assert app.user_stopped() is True
    app.wait(2000)
    assert app.user_stopped() is False


# ── Source guard: the second check inside each 1.5s gap must stay ────────

def test_both_gaps_recheck_the_sleep_intent():
    """Both ghost timers check isUserStopped() TWICE: when the 3s timer fires
    and again inside the 1.5s close-to-start gap (the user can hit Sleep
    mid-recovery).  Plus the send_message error fallback: five checks."""
    src = (STATIC / "js" / "live-panel.js").read_text(encoding="utf-8")
    m = re.search(r"^async function _liveSubmitDirect\(.*?^}\n", src, re.S | re.M)
    assert m, "_liveSubmitDirect not found"
    body = m.group(0)
    assert body.count("isUserStopped(sid)") >= 5, body.count("isUserStopped(sid)")
    # Each inner setTimeout (the gap) carries its own check.
    gaps = re.findall(r"setTimeout\(\(\) => \{(.*?)\}, 1500\)", body, re.S)
    assert len(gaps) == 2, len(gaps)
    for gap in gaps:
        assert "isUserStopped(sid)" in gap
    # The cancel handlers must keep ignoring 'stopped' (a dead session also
    # reports stopped; the cause is the discriminator, not the state).
    assert body.count("data.state !== 'stopped'") == 2
