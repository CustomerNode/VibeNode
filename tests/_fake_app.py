"""The real VibeNode page in headless Chromium with a faked network.

Shared by the sidebar/front-end browser tests
(``test_session_render_perf.py``, ``test_visible_fixes.py``,
``test_session_filters.py``).  Modeled on ``test_session_title_memory.py``:
the page is the REAL ``src/web/templates/index.html`` with every real script, served
by a request interceptor (no server runs).  Only the network is faked: ``/api``
calls get canned replies and the Socket.IO CDN script is replaced by a stub
whose listeners the test calls directly to simulate server pushes.
"""

from __future__ import annotations

import contextlib
import itertools
import json
import os
import re
import stat
import threading
from pathlib import Path

from tests._browser import PAGE_ORIGIN, _Request, register_page_app, unregister_page_app, warm_page

ROOT = Path(__file__).resolve().parents[1]
STATIC = ROOT / "src" / "web" / "static"
TEMPLATES = ROOT / "src" / "web" / "templates"

# Every request is answered by the route below; nothing reaches the network.
# It must be localhost: addNewAgent() uses crypto.randomUUID(), which only
# exists in a secure context.
ORIGIN = PAGE_ORIGIN
PROJECT = "C--test-proj"
CWD = "C:\\test\\proj"

ANIMATIONS_DONE = """() => !document.getAnimations().some(a => a.playState === 'running'
    && a.effect.getComputedTiming().iterations !== Infinity)"""

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


# A test's page timers, on a clock that belongs to the PAGE.  Playwright's own
# clock belongs to the context and cannot be taken off again, so it would end the
# one shared context that makes page loads cheap.  Same behaviour: time flows
# normally, and runFor() fires every timer due in the window, in order, each in
# its own task, at the time it was due.
PAGE_CLOCK = r"""
(() => {
  if (window.__vnClock) return;
  const R = {setTimeout: window.setTimeout.bind(window), clearTimeout: window.clearTimeout.bind(window),
             Date: window.Date, now: window.Date.now.bind(window.Date),
             perf: performance.now.bind(performance)};
  const timers = new Map();
  const base = R.now() - R.perf();
  let seq = 1, jump = 0, cursor = null, running = false, paused = false;
  const vnow = () => cursor !== null ? cursor : R.perf() + jump;
  const arm = (t) => { if (!running && !paused) t.real = R.setTimeout(() => fire(t), Math.max(0, t.due - vnow())); };
  function fire(t) {
    if (!timers.has(t.id)) return;
    if (t.every === null) timers.delete(t.id);
    else if (t.due <= vnow()) { t.due = Math.max(t.due + t.every, vnow()); arm(t); }
    if (typeof t.fn === 'function') t.fn(...t.args); else (0, eval)(String(t.fn));
  }
  const add = (fn, ms, args, every) => {
    const d = Math.max(0, Number(ms) || 0);
    const t = {id: seq++, fn, args, due: vnow() + d, every: every ? Math.max(1, d) : null, order: seq};
    timers.set(t.id, t); arm(t); return t.id;
  };
  window.setTimeout = (fn, ms, ...args) => add(fn, ms, args, false);
  window.setInterval = (fn, ms, ...args) => add(fn, ms, args, true);
  window.clearTimeout = window.clearInterval = (id) => {
    const t = timers.get(id);
    if (t) { R.clearTimeout(t.real); timers.delete(id); }
  };
  class VDate extends R.Date {
    constructor(...a) { if (a.length) super(...a); else super(VDate.now()); }
    static now() { return Math.round(base + vnow()); }
  }
  window.Date = VDate;
  performance.now = () => vnow();
  const turn = new MessageChannel();
  const yieldTask = () => new Promise((r) => { turn.port1.onmessage = r; turn.port2.postMessage(0); });
  window.__vnClock = {
    // Two real frames (observers and layout have run), never hanging on a page
    // that draws none.
    frames() {
      return new Promise((done) => {
        requestAnimationFrame(() => requestAnimationFrame(done));
        R.setTimeout(done, 150);
      });
    },
    async runFor(ms) {
      const end = vnow() + ms;
      running = true;
      for (const t of timers.values()) R.clearTimeout(t.real);
      cursor = vnow();
      try {
        for (;;) {
          let next = null;
          for (const t of timers.values())
            if (t.due <= end && (!next || t.due < next.due || (t.due === next.due && t.order < next.order))) next = t;
          if (!next) break;
          cursor = Math.max(cursor, next.due);
          try { fire(next); } catch (e) { R.setTimeout(() => { throw e; }, 0); }
          await yieldTask();
        }
        cursor = end;
      } finally {
        running = false;
        if (paused) cursor = end;
        else { jump = end - R.perf(); cursor = null; for (const t of timers.values()) arm(t); }
      }
    },
    // Jump to ``epoch`` and stay there until runFor(): every timer due by then
    // fires at most once, as Playwright's clock.pauseAt does.
    async pauseAt(epoch) {
      const end = vnow() + (epoch - VDate.now());
      for (const t of timers.values()) R.clearTimeout(t.real);
      running = true; paused = true; cursor = vnow();
      for (const t of [...timers.values()].filter((x) => x.due <= end).sort((a, b) => a.due - b.due || a.order - b.order)) {
        if (t.every !== null) t.due = end + t.every;
        cursor = Math.min(end, Math.max(cursor, t.due));
        if (timers.has(t.id)) { try { fire(t); } catch (e) { R.setTimeout(() => { throw e; }, 0); } await yieldTask(); }
      }
      cursor = end; running = false;
    },
  };
})();
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


_index_cache: tuple = (None, "")
_static_cache: dict = {}


def index_html() -> str:
    # Every browser test loads this page; rendered once per version of the
    # templates, in each worker.
    global _index_cache
    from jinja2 import Environment, FileSystemLoader

    stamp = tuple(sorted((e.name, e.stat().st_mtime_ns) for e in os.scandir(TEMPLATES)))
    if _index_cache[0] != stamp:
        env = Environment(loader=FileSystemLoader(str(TEMPLATES)))
        _index_cache = (stamp, env.get_template("index.html").render(
            versioned_static=lambda f: f"/static/{f}", a2hs_title="VibeNode"))
    return _index_cache[1]


def static_bytes(f: Path):
    """A static file's bytes, or None if there is none.  Read again whenever the
    file changes on disk, so a test (or an edit during a run) is always served
    what the disk holds."""
    try:
        st = f.stat()
    except OSError:
        return None
    if not stat.S_ISREG(st.st_mode):
        return None
    stamp = (st.st_mtime_ns, st.st_size)
    hit = _static_cache.get(f)
    if hit is None or hit[0] != stamp:
        hit = _static_cache[f] = (stamp, f.read_bytes())
    return hit[1]


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


_FAKE_IO_PATH = "/__fake_socket_io.js"
_app_ids = itertools.count(1)
_open_apps: list = []       # (test that opened it, app), newest last
_served_html: tuple = (None, b"")


def _strip_external(html: str) -> str:
    out = re.sub(r"https://cdn\.socket\.io/[^\"']+", _FAKE_IO_PATH, html)
    return re.sub(r"<link[^>]*fonts\.g[^>]*>", "", out)


def _served_index() -> bytes:
    """index.html with the Socket.IO CDN script pointing at the stand-in and the
    web fonts dropped: nothing leaves this machine."""
    global _served_html
    html = index_html()
    if _served_html[0] != html:
        _served_html = (html, _strip_external(html).encode())
    return _served_html[1]


def _current_test() -> str:
    return os.environ.get("PYTEST_CURRENT_TEST", "").split(" ")[0]


def _open_page(browser, app, size: dict, options: dict):
    """An empty context's new page: the app's own private context when another
    app of this test is open, else the worker's long-lived page, reset."""
    test = _current_test()
    for entry in list(_open_apps):
        if entry[0] != test or entry[1].page.is_closed():
            entry[1].close()
    got = None if _open_apps else warm_page(browser, options, size)
    if got is None:
        ctx = app._own_ctx = getattr(browser, "_real", browser).new_context(viewport=size, **options)
        got = ctx, ctx.new_page()
    _open_apps.append((test, app))
    return got


class PageClock:
    """``page.clock``'s install / run_for / pause_at, on PAGE_CLOCK."""

    def __init__(self, page):
        self.page = page

    def install(self):
        self.page.add_init_script(PAGE_CLOCK)

    def run_for(self, ms: int):
        self.page.evaluate("(ms) => window.__vnClock.runFor(ms)", ms)

    def pause_at(self, when: str):
        self.page.evaluate("(w) => window.__vnClock.pauseAt(new Date(w).getTime())", when)


class _Route:
    """What a test's ``route(route, request)`` handler calls; it may answer
    later, from the test's thread, as with a real route."""

    def __init__(self):
        self.answered = threading.Event()
        self.reply = None

    def fulfill(self, status=200, body="", content_type="text/plain", json_=None, **_):
        data = body.encode() if isinstance(body, str) else (body or b"")
        self.reply = (status, content_type, data, "no-store")
        self.answered.set()

    def abort(self, *_):
        self.reply = None
        self.answered.set()


class ServedPage:
    """A test's own route handler in front of a real page on the page server,
    in a wiped long-lived context: same answers, cheaper load (tests/_browser.py).
    The handler runs on the server's thread, so it must not touch the page."""

    def __init__(self, browser, route, *, init: str = "", viewport=None, context: dict | None = None):
        self._route_fn = route
        self._id = f"app-{next(_app_ids)}"
        self.ctx, self.page = _open_page(browser, self, viewport or {"width": 1400, "height": 900}, context or {})
        self.clock = PageClock(self.page)
        if init:
            self.page.add_init_script(init)
        register_page_app(self._id, self._respond)
        self.page.set_extra_http_headers({"x-vn-app": self._id})

    def _respond(self, request):
        path = request.url[len(ORIGIN):].split("?")[0]
        asked = request
        if path == _FAKE_IO_PATH:
            asked = _Request(request.method, "https://cdn.socket.io/stand-in.js", request.headers, b"")
        route = _Route()
        self._route_fn(route, asked)
        route.answered.wait(60)
        if route.reply is None:
            return None
        status, ctype, data, _ = route.reply
        cache = "no-store"
        if path == "/" and ctype == "text/html":
            data = _strip_external(data.decode()).encode()
        elif path.startswith("/static/") and status == 200 and data == static_bytes(STATIC / path[len("/static/"):]):
            cache = "max-age=3600"      # the app's own file, unaltered
        return status, ctype, data, cache

    def close(self):
        unregister_page_app(self._id)
        _open_apps[:] = [e for e in _open_apps if e[1] is not self]
        with contextlib.suppress(Exception):
            if getattr(self, "_own_ctx", None) is not None:
                self._own_ctx.close()
            else:
                self.page.close()


class FakeApp:
    """One loaded VibeNode page plus the knobs to drive it."""

    def __init__(self, browser, sessions: list[dict], *, init: str | None = None,
                 path: str = "/", viewport=None, wait_for: str | None = None,
                 extra_routes: dict | None = None, context: dict | None = None,
                 clock: bool = False):
        self.sessions = sessions
        self.errors: list[str] = []
        self.requests: list[str] = []
        self.extra_routes = extra_routes or {}
        self._index = _served_index()
        self._id = f"app-{next(_app_ids)}"
        size = viewport or {"width": 1400, "height": 900}
        # One long-lived context per kind of device (`context`: more options,
        # e.g. a touch phone), so the HTTP cache carries over.  Each app starts
        # from an empty one (_open_page).  An app beside another open app of the
        # same test (two tabs of two devices) gets a private context, as before.
        self.ctx, self.page = _open_page(browser, self, size, context or {})
        # `clock`: the page's timers run on PAGE_CLOCK, so advance() can fire
        # every production timer in a window at once instead of sitting through
        # it.  Time still flows normally between advances.
        if clock:
            self.page.add_init_script(PAGE_CLOCK)
        self.page.add_init_script(init if init is not None else init_script())
        self.page.on("pageerror", lambda e: self.errors.append(str(e)))
        register_page_app(self._id, self._respond)
        self.page.set_extra_http_headers({"x-vn-app": self._id})
        self.page.goto(ORIGIN + path, wait_until="domcontentloaded")
        if wait_for is None:
            wait_for = "document.querySelector('#workforce-grid .wf-card, #session-list .session-item') !== null"
        if wait_for:
            self.page.wait_for_function(wait_for, timeout=20000)
        # The splash fades over the page for about a second: a real click would
        # wait it out.  No test here is about the splash.
        self.page.evaluate("() => { for (const id of ['vn-splash', 'health-blocker']) { const s = document.getElementById(id); if (s) s.remove(); } }")

    def _respond(self, request):
        """Everything the page asks for: the same answers a route gave, with the
        app's own files cacheable."""
        url = request.url
        path = url[len(ORIGIN):].split("?")[0]
        if path.startswith("/api/"):
            self.requests.append(url[len(ORIGIN):])
        if path == "/":
            return 200, "text/html", self._index, "no-store"
        if path == _FAKE_IO_PATH:
            return 200, "application/javascript", FAKE_IO.encode(), "no-store"
        if path.startswith("/static/"):
            f = STATIC / path[len("/static/"):]
            body = static_bytes(f)
            if body is not None:
                return 200, _CTYPES.get(f.suffix, "application/octet-stream"), body, "max-age=3600"
            return 404, "text/plain", b"", "no-store"
        for prefix, handler in self.extra_routes.items():
            if path.startswith(prefix):
                status, body = handler(request)
                return status, "application/json", json.dumps(body).encode(), "no-store"
        if path.startswith("/api/projects"):
            return 200, "application/json", json.dumps(
                [{"encoded": PROJECT, "display": CWD, "session_count": len(self.sessions)}]).encode(), "no-store"
        if path.startswith("/api/sessions"):
            return 200, "application/json", json.dumps(self.sessions).encode(), "no-store"
        if path.startswith("/api/"):
            return 200, "application/json", b"{}", "no-store"
        return 404, "text/plain", b"", "no-store"

    def push(self, event: str, data: dict) -> None:
        self.page.evaluate("([e, d]) => window.__push(e, d)", [event, data])

    def push_many(self, event: str, items: list[dict]) -> None:
        """Deliver many pushes inside ONE JS task, like a burst on the wire."""
        self.page.evaluate("([e, items]) => { for (const d of items) window.__push(e, d); }", [event, items])

    def advance(self, ms: int) -> None:
        """Fire every page timer due in the next ``ms`` (needs ``clock=True``),
        with a real frame before and after: observers and layout run on the
        browser's real frames, not on the page's timers, and in a real wait
        they get their turn before a timer that is hundreds of ms away."""
        self.page.evaluate("() => window.__vnClock.frames().then(() => 0)")
        PageClock(self.page).run_for(ms)
        self.page.evaluate("() => window.__vnClock.frames().then(() => 0)")

    def animations_done(self, timeout: int = 3000) -> None:
        """Every finite CSS animation or transition has ended (real time: the
        clock does not drive those)."""
        self.page.wait_for_function(ANIMATIONS_DONE, timeout=timeout)

    def eval(self, js: str, arg=None):
        return self.page.evaluate(js, arg) if arg is not None else self.page.evaluate(js)

    def close(self):
        unregister_page_app(self._id)
        _open_apps[:] = [e for e in _open_apps if e[1] is not self]
        with contextlib.suppress(Exception):
            if getattr(self, "_own_ctx", None) is not None:
                self._own_ctx.close()
            else:
                self.page.close()
