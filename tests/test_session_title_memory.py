"""A session's auto-name must reach the sidebar without a page refresh.

Reported 2026-10-08: "session auto naming doesn't update in the left panel
card/table until I refresh the page".  The names were saved correctly on disk
(a refresh always showed them); the open page lost them.

A name reaches the client as a ONE-SHOT event: the ``/api/autonname`` reply,
or the ``session_renamed`` broadcast.  Both patched the row that existed at
that moment and nothing else, so any row created or rebuilt AFTER the event
came up with a placeholder and kept it:

* A tab or device that did not start the session gets the broadcast BEFORE it
  has the row.  The row arrives later as a ``state_snapshot`` stub, titled
  from the daemon's ``name``, which is empty for GUI sessions: "New Session".
* A session-list reload (socket reconnect, project switch...) computed before
  the title was saved lands after it and replaces the titled row with the
  server's placeholder.
* A reload during a session's first turn returns it twice: the daemon's stub
  under the launch id and the CLI transcript under the CLI's id.  The id remap
  at the end of the turn then renamed the stub onto the transcript's id,
  leaving two cards with the same id, one showing the first message.

The fix is ``_knownSessionTitles`` in ``static/js/app.js``: every title the
page learns is remembered and filled into rows that have no name of their own
(a name the server returns always wins), and the remap handler in
``static/js/socket.js`` merges the duplicate instead of keeping both.

The page under test is the REAL ``templates/index.html`` with every real
script, served by a request interceptor (no server runs).  Only the network is faked: ``/api`` calls
get canned replies, the Socket.IO CDN script is replaced by a stub whose
listeners the test calls directly to simulate server pushes, and
``/api/autonname`` replies are held until the test releases them, so each
event ordering is reproduced exactly.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
STATIC = ROOT / "static"
TEMPLATES = ROOT / "templates"

# Every request is answered by the route below; nothing reaches the network.
# It must be localhost: addNewAgent() uses crypto.randomUUID(), which only
# exists in a secure context.
ORIGIN = "http://localhost:59173"
PROJECT = "C--test-proj"
CWD = "C:\\test\\proj"
TITLE = "Sidebar naming fix"
PROMPT = "the sidebar card keeps showing my first message instead of the generated name"

_CTYPES = {
    ".js": "application/javascript", ".css": "text/css", ".svg": "image/svg+xml",
    ".png": "image/png", ".json": "application/json", ".woff2": "font/woff2",
}

OLD_ROW = {
    "id": "11111111-aaaa-4aaa-8aaa-000000000001", "display_title": "Older session",
    "custom_title": "Older session", "user_named": True,
    "last_activity": "Oct 07, 2026  10:00 AM", "last_activity_ts": 1791370000,
    "effective_ts": 1791370000, "sort_ts": 1791370000, "size": "10 KB",
    "file_bytes": 10000, "message_count": 4, "preview": "x",
}

# Stand-in for the Socket.IO client: records emits, never connects, and keeps
# the listeners so the test can deliver "server pushes" to the real handlers.
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
window.__autoname = [];
window.__holdSessions = false;
window.__heldSessions = [];
const __json = (p) => new Response(JSON.stringify(p),
  {status: 200, headers: {'Content-Type': 'application/json'}});
const __fetch = window.fetch;
window.fetch = function (url, opts) {
  if (typeof url === 'string' && url.startsWith('/api/autonname/')) {
    return new Promise(res => window.__autoname.push(res));
  }
  if (window.__holdSessions && typeof url === 'string' && url.startsWith('/api/sessions')) {
    return new Promise(res => window.__heldSessions.push(res));
  }
  return __fetch.apply(this, arguments);
};
window.__resolveAutoname = (i, p) => window.__autoname[i](__json(p));
window.__releaseSessions = (p) => {
  window.__holdSessions = false;
  for (const res of window.__heldSessions.splice(0)) res(__json(p));
};
window.__push = (ev, d) => { for (const fn of socket.listeners(ev)) fn(d); };
""" % PROJECT

CARDS_JS = """() => [...document.querySelectorAll('#workforce-grid .wf-card')]
  .map(c => [c.dataset.sid, c.querySelector('.wf-name').textContent.trim()])"""


def _index_html() -> str:
    from jinja2 import Environment, FileSystemLoader

    env = Environment(loader=FileSystemLoader(str(TEMPLATES)))
    return env.get_template("index.html").render(
        versioned_static=lambda f: f"/static/{f}", a2hs_title="VibeNode")


def _stub_row(sid: str, title: str = "New Session", custom: str = "") -> dict:
    return {"id": sid, "display_title": title, "custom_title": custom, "user_named": bool(custom),
            "last_activity": "", "last_activity_ts": 0, "effective_ts": 1791400000, "sort_ts": 0,
            "size": "", "file_bytes": 0, "message_count": 0, "preview": ""}


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
    """One loaded VibeNode page plus the knobs to drive it."""

    def __init__(self, browser):
        self.sessions = [OLD_ROW]
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
            return route.abort()  # web fonts: cosmetic, and the test must not need the network
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
                [{"encoded": PROJECT, "display": CWD, "session_count": 3}]))
        if path.startswith("/api/sessions"):
            return route.fulfill(content_type="application/json", body=json.dumps(self.sessions))
        if path.startswith("/api/"):
            return route.fulfill(content_type="application/json", body="{}")
        return route.fulfill(status=404, body="")

    def push(self, event: str, data: dict) -> None:
        self.page.evaluate("([e, d]) => window.__push(e, d)", [event, data])

    def cards(self) -> list[list[str]]:
        return self.page.evaluate(CARDS_JS)

    def card(self, sid: str):
        names = [name for s, name in self.cards() if s == sid]
        return names[0] if len(names) == 1 else names

    def start_new_session(self) -> str:
        """The real New Session flow, up to the auto-name request in flight."""
        self.page.evaluate("addNewAgent()")
        sid = self.page.evaluate("activeId")
        self.page.evaluate("(t) => { document.getElementById('live-input-ta').value = t; }", PROMPT)
        self.page.evaluate("(sid) => { _newSessionSubmit(sid); }", sid)
        self.page.wait_for_function("window.__autoname.length > 0", timeout=10000)
        self.push("session_started", {"session_id": sid})
        return sid

    def name_arrives(self, sid: str) -> None:
        """The server finished auto-naming: HTTP reply plus the broadcast."""
        self.page.evaluate("(t) => window.__resolveAutoname(0, {ok: true, title: t})", TITLE)
        self.page.wait_for_timeout(50)
        self.push("session_renamed", {"session_id": sid, "title": TITLE, "project": PROJECT})

    def snapshot(self, sid: str, aliases: dict | None = None) -> None:
        self.push("state_snapshot", {
            "sessions": [{"session_id": sid, "state": "working", "cwd": CWD, "name": ""}],
            "queues": {}, "aliases": aliases or {}})

    def close(self):
        self.ctx.close()


@pytest.fixture
def app(browser):
    a = App(browser)
    yield a
    assert not a.errors, f"page errors: {a.errors}"
    a.close()


@pytest.mark.slow
def test_starting_tab_card_gets_the_auto_name(app):
    """Baseline: the tab that started the session shows the name live."""
    sid = app.start_new_session()
    assert app.card(sid) != TITLE
    app.name_arrives(sid)
    assert app.card(sid) == TITLE


@pytest.mark.slow
def test_other_tab_gets_the_name_when_the_broadcast_beats_the_row(app):
    """A tab that didn't start the session hears the name before it has a row.

    The row shows up afterwards as a state_snapshot stub; it must carry the
    name, not "New Session" until a refresh.
    """
    sid = "44444444-dddd-4ddd-8ddd-000000000004"
    app.push("session_renamed", {"session_id": sid, "title": TITLE, "project": PROJECT})
    assert app.card(sid) == []          # no phantom row is synthesized
    app.snapshot(sid)
    assert app.card(sid) == TITLE


@pytest.mark.slow
def test_other_tab_gets_the_name_across_an_id_remap(app):
    sid, real = "66666666-ffff-4fff-8fff-000000000006", "77777777-aaaa-4aaa-8aaa-000000000007"
    app.push("session_renamed", {"session_id": sid, "title": TITLE, "project": PROJECT})
    app.push("session_id_remapped", {"old_id": sid, "new_id": real})
    app.snapshot(real, aliases={sid: real})
    assert app.card(real) == TITLE


@pytest.mark.slow
def test_stale_session_list_reload_does_not_wipe_the_name(app):
    """A reload computed before the name was saved lands after it."""
    sid = app.start_new_session()
    app.page.evaluate("() => { window.__holdSessions = true; window.__reload = loadSessions(); }")
    app.page.wait_for_function("window.__heldSessions.length > 0", timeout=10000)
    app.name_arrives(sid)
    assert app.card(sid) == TITLE
    app.page.evaluate("(p) => window.__releaseSessions(p)", [_stub_row(sid), OLD_ROW])
    app.page.evaluate("() => window.__reload")          # the reload has fully landed
    assert app.page.evaluate("(sid) => allSessions.find(s => s.id === sid).display_title", sid) == TITLE
    assert app.card(sid) == TITLE


@pytest.mark.slow
def test_first_turn_reload_then_remap_leaves_one_titled_card(app):
    """Mid-first-turn reload lists the session twice; the remap must merge them."""
    sid = app.start_new_session()
    transcript = "33333333-cccc-4ccc-8ccc-000000000003"
    app.sessions = [
        _stub_row(sid),
        {**_stub_row(transcript, PROMPT[:60] + "\u2026"), "custom_title": None,
         "message_count": 2, "size": "5 KB", "file_bytes": 5000, "preview": PROMPT},
        OLD_ROW,
    ]
    app.page.evaluate("loadSessions()")
    app.name_arrives(sid)
    assert app.card(sid) == TITLE
    app.push("session_id_remapped", {"old_id": sid, "new_id": transcript})
    assert app.card(transcript) == TITLE, app.cards()      # exactly one card, titled
    assert app.page.evaluate("activeId") == transcript
    # PERF-CRITICAL #15: the id Set must stay in sync after the merge.
    assert app.page.evaluate(
        "(t) => allSessions.length === allSessionIds.size && allSessionIds.has(t)", transcript)


@pytest.mark.slow
def test_a_name_from_the_server_wins_over_the_memory(app):
    """Memory only fills rows with no name; it never overrides the server's."""
    sid = "88888888-bbbb-4bbb-8bbb-000000000008"
    app.push("session_renamed", {"session_id": sid, "title": TITLE, "project": PROJECT})
    app.sessions = [_stub_row(sid, "Renamed elsewhere", "Renamed elsewhere"), OLD_ROW]
    app.page.evaluate("loadSessions()")
    assert app.card(sid) == "Renamed elsewhere"


@pytest.mark.slow
def test_sessions_refresh_forgets_names_the_server_removed(app):
    """An admin scrub removes junk names and broadcasts sessions_refresh.

    The reload must show what the server now has, not repaint the scrubbed
    name from memory.
    """
    sid = "99999999-cccc-4ccc-8ccc-000000000009"
    app.push("session_renamed", {"session_id": sid, "title": TITLE, "project": PROJECT})
    app.sessions = [_stub_row(sid), OLD_ROW]
    app.push("sessions_refresh", {})
    app.page.wait_for_function(
        "(sid) => !!document.querySelector('#workforce-grid .wf-card[data-sid=\"' + sid + '\"]')",
        arg=sid, timeout=10000)
    assert app.card(sid) == "New Session"
