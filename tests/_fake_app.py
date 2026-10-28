"""The real VibeNode page in headless Chromium with a faked network.

Shared by the sidebar/front-end browser tests added 2026-10-23
(``test_session_render_perf.py``, ``test_visible_fixes.py``,
``test_session_filters.py``).  Modeled on ``test_session_title_memory.py``:
the page is the REAL ``templates/index.html`` with every real script, served
by a request interceptor (no server runs).  Only the network is faked: ``/api``
calls get canned replies and the Socket.IO CDN script is replaced by a stub
whose listeners the test calls directly to simulate server pushes.
"""

from __future__ import annotations

import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
STATIC = ROOT / "static"
TEMPLATES = ROOT / "templates"

# Every request is answered by the route below; nothing reaches the network.
# It must be localhost: addNewAgent() uses crypto.randomUUID(), which only
# exists in a secure context.
ORIGIN = "http://localhost:59174"
PROJECT = "C--test-proj"
CWD = "C:\\test\\proj"

_CTYPES = {
    ".js": "application/javascript", ".css": "text/css", ".svg": "image/svg+xml",
    ".png": "image/png", ".json": "application/json", ".woff2": "font/woff2",
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


def init_script(view_mode: str = "sessions", display_mode: str = "grid", extra: str = "") -> str:
    """localStorage + fetch hooks installed before any page script runs."""
    return """
localStorage.setItem('activeProject', '%s');
localStorage.setItem('viewMode', '%s');
localStorage.setItem('sessionDisplayMode', '%s');
// The sidebar's person statuses follow Smart termination (session-verb.js);
// the fake config is {} so this cached value decides.  A test that wants the
// process statuses sets it to 'off' in `extra`.
localStorage.setItem('vnSmartTermination', 'on');
window.__emits = [];
window.__push = (ev, d) => { for (const fn of socket.listeners(ev)) fn(d); };
%s
""" % (PROJECT, view_mode, display_mode, extra)


def index_html() -> str:
    from jinja2 import Environment, FileSystemLoader

    env = Environment(loader=FileSystemLoader(str(TEMPLATES)))
    return env.get_template("index.html").render(
        versioned_static=lambda f: f"/static/{f}", a2hs_title="VibeNode")


def session_row(sid: str, title: str, ts: int, *, custom: str = "", preview: str = "",
                message_count: int = 4, file_bytes: int = 10000) -> dict:
    return {
        "id": sid, "display_title": title, "custom_title": custom, "user_named": bool(custom),
        "last_activity": "Oct 07, 2026  10:00 AM", "last_activity_ts": ts,
        "effective_ts": ts, "sort_ts": ts, "size": "10 KB",
        "file_bytes": file_bytes, "message_count": message_count, "preview": preview,
    }


def fake_sessions(n: int, *, base_ts: int = 1791370000) -> list[dict]:
    """``n`` distinct sessions, newest first."""
    return [session_row(f"{i:08d}-aaaa-4aaa-8aaa-{i:012d}", f"Session {i:04d}", base_ts - i * 60)
            for i in range(n)]


class FakeApp:
    """One loaded VibeNode page plus the knobs to drive it."""

    def __init__(self, browser, sessions: list[dict], *, init: str | None = None,
                 path: str = "/", viewport=None, wait_for: str | None = None,
                 extra_routes: dict | None = None):
        self.sessions = sessions
        self.errors: list[str] = []
        self.requests: list[str] = []
        self.extra_routes = extra_routes or {}
        self._index = index_html()
        self.ctx = browser.new_context(viewport=viewport or {"width": 1400, "height": 900})
        self.ctx.add_init_script(init if init is not None else init_script())
        self.page = self.ctx.new_page()
        self.page.on("pageerror", lambda e: self.errors.append(str(e)))
        self.page.route("**/*", self._route)
        self.page.goto(ORIGIN + path, wait_until="domcontentloaded")
        if wait_for is None:
            wait_for = "document.querySelector('#workforce-grid .wf-card, #session-list .session-item') !== null"
        if wait_for:
            self.page.wait_for_function(wait_for, timeout=20000)

    def _route(self, route, request):
        url = request.url
        if url.startswith("https://cdn.socket.io/"):
            return route.fulfill(body=FAKE_IO, content_type="application/javascript")
        if not url.startswith(ORIGIN):
            return route.abort()  # web fonts: cosmetic, and the test must not need the network
        path = url[len(ORIGIN):].split("?")[0]
        if path.startswith("/api/"):
            self.requests.append(url[len(ORIGIN):])
        if path == "/":
            return route.fulfill(body=self._index, content_type="text/html")
        if path.startswith("/static/"):
            f = STATIC / path[len("/static/"):]
            if f.is_file():
                return route.fulfill(body=f.read_bytes(),
                                     content_type=_CTYPES.get(f.suffix, "application/octet-stream"))
            return route.fulfill(status=404, body="")
        for prefix, handler in self.extra_routes.items():
            if path.startswith(prefix):
                status, body = handler(request)
                return route.fulfill(status=status, content_type="application/json", body=json.dumps(body))
        if path.startswith("/api/projects"):
            return route.fulfill(content_type="application/json", body=json.dumps(
                [{"encoded": PROJECT, "display": CWD, "session_count": len(self.sessions)}]))
        if path.startswith("/api/sessions"):
            return route.fulfill(content_type="application/json", body=json.dumps(self.sessions))
        if path.startswith("/api/"):
            return route.fulfill(content_type="application/json", body="{}")
        return route.fulfill(status=404, body="")

    def push(self, event: str, data: dict) -> None:
        self.page.evaluate("([e, d]) => window.__push(e, d)", [event, data])

    def push_many(self, event: str, items: list[dict]) -> None:
        """Deliver many pushes inside ONE JS task, like a burst on the wire."""
        self.page.evaluate("([e, items]) => { for (const d of items) window.__push(e, d); }", [event, items])

    def eval(self, js: str, arg=None):
        return self.page.evaluate(js, arg) if arg is not None else self.page.evaluate(js)

    def close(self):
        self.ctx.close()
