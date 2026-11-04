"""One browser per test worker, shared by consecutive browser test files.

Starting a Playwright driver and Chromium per browser test file and shutting
both down at the end costs ~0.3s idle, but in the parallel run a shutdown can
take 4-30s, and with files split into chunks across workers there are ~20 per
run.

So a worker starts one driver + browser on first use and keeps it while it
runs browser test files back to back.  ``tests/conftest.py`` closes it as
soon as the next test is not a browser test (and at session end), so no
Playwright event loop is alive while other tests run.  Isolation is
unchanged: each file's fixtures still open their own contexts and pages, and
``shared_browser_scope`` closes every context a file opened when the file is
done, exactly what closing the browser per file used to do.

Keep Playwright's bundled x64 build on Windows ARM64, not the installed
native ARM64 Chrome: in the full run the native build takes the CPU the
Python workers need, is no faster (80-90s vs 69-75s) and makes 5s waits in
daemon tests time out.
"""

from __future__ import annotations

import collections
import contextlib
import http.server
import json as _json
import threading

_shared = {"pw": None, "browser": None, "warm": {}, "pages": {}}

# ── the local page server ──────────────────────────────────────────────────
# Real HTTP lets Chromium cache the app's files and the compiled code of its
# scripts, which is most of a page load's CPU (measured per load:
# Chromium 256ms -> 105ms and Python 96ms -> 10ms).  Interception cannot do
# that: Playwright turns the HTTP cache off on any page with a route.  One
# server per worker, on a free port; pages keep the fixed address (PAGE_ORIGIN)
# because the browser maps it to this port, so an address never differs between
# workers and "localhost" stays a secure context.
PAGE_ORIGIN = "http://localhost:59174"
RESET_PATH = "/__vn_reset"
_server = {"srv": None, "apps": {}, "latest": None}


class _Request:
    """What a test's route handler reads from a request."""

    def __init__(self, method, url, headers, body):
        self.method, self.url, self.headers, self._body = method, url, headers, body

    @property
    def post_data(self):
        return self._body.decode("utf-8", "replace") if self._body else None

    @property
    def post_data_buffer(self):
        return self._body or None

    @property
    def post_data_json(self):
        return _json.loads(self._body) if self._body else None


class _Handler(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    disable_nagle_algorithm = True      # a delayed ACK per response costs 40ms
    wbufsize = -1

    def log_message(self, *a):
        pass

    def _serve(self):
        n = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(n) if n else b""
        if self.path == RESET_PATH:     # see _reset_page
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.send_header("Content-Length", "0")
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.flush()
            return
        app = _server["apps"].get(self.headers.get("x-vn-app")) or _server["latest"]
        req = _Request(self.command, PAGE_ORIGIN + self.path, dict(self.headers.items()), body)
        try:
            reply = app(req) if app else (404, "text/plain", b"", "no-store")
            if reply is None:           # route.abort(): the connection just fails
                self.close_connection = True
                return
            status, ctype, data, cache = reply
        except Exception as exc:  # a broken handler shows as a server error, as a route's would
            status, ctype, data, cache = 500, "text/plain", repr(exc).encode(), "no-store"
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", cache)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(data)
        self.wfile.flush()

    do_GET = do_POST = do_PUT = do_DELETE = do_PATCH = do_HEAD = _serve


def _server_port() -> int:
    if _server["srv"] is None:
        srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        srv.daemon_threads = True
        threading.Thread(target=srv.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True).start()
        _server["srv"] = srv
    return _server["srv"].server_address[1]


def register_page_app(app_id: str, handler) -> None:
    """``handler(request) -> (status, content type, bytes, cache-control)``."""
    _server["apps"][app_id] = handler
    _server["latest"] = handler


def unregister_page_app(app_id: str) -> None:
    handler = _server["apps"].pop(app_id, None)
    if _server["latest"] is handler:
        _server["latest"] = None


def _launch_args() -> list:
    host = PAGE_ORIGIN.split("//")[1]
    # PAGE_ORIGIN reaches this worker's server.  Nothing else resolves: no test
    # may need the network (web fonts and the like just fail, at once).
    # No --disable-frame-rate-limit / --disable-gpu-vsync: with them every
    # open page draws frames as fast as it can.  Three idle pages cost 4.6 CPU
    # cores (0.37 without); 8 workers saturate the machine, page loads stall
    # past the 120 s test timeout and workers are killed in clusters
    # ("node down: Not properly terminated").
    return [f"--host-resolver-rules=MAP {host} 127.0.0.1:{_server_port()},"
            "MAP *.* ~NOTFOUND, EXCLUDE 127.0.0.1",
            # Most of a load's renderer CPU is painting pixels on several threads
            # at once, and these tests read the DOM.  One thread: 145 -> 114ms
            # of CPU per load (measured).
            "--num-raster-threads=1"]


def shared_browser():
    """This worker's browser, started on first use; skips if unavailable."""
    if _shared["browser"] is None:
        import pytest
        pw = pytest.importorskip("playwright.sync_api", reason="playwright not installed")
        mgr = pw.sync_playwright().start()
        try:
            browser = mgr.chromium.launch(args=_launch_args())
        except Exception as exc:  # browser binary not downloaded on this machine
            mgr.stop()
            pytest.skip(f"chromium unavailable: {str(exc).splitlines()[0][:80]}")
        _shared.update(pw=mgr, browser=browser, warm={}, pages={})
    return _shared["browser"]


# ── one long-lived page per kind of device ─────────────────────────────────
# A new page is a new renderer process: 290ms of CPU per test against 110ms for
# navigating an existing one (measured), and that is nearly all of
# the suite's Chromium time.  So a worker keeps one page per set of context
# options and hands it to one test at a time.  Everything a new tab would not
# have is removed first (_reset_page); tests/test_fake_app_isolation.py pins each
# item.  A page that cannot be made new is closed instead, and the context with
# it if the test talked to another web server (its HTTP cache would carry over).
# Only one user at a time: anything asked for while the page is out gets its own
# context, as every page did before.
class _WarmState:
    __slots__ = ("key", "ctx", "in_use", "disposables", "events", "origins", "retire", "cdp")

    def __init__(self, key, ctx):
        self.key, self.ctx = key, ctx
        self.in_use = False
        self.disposables: list = []
        self.events = None
        self.origins: set = set()
        self.retire = False
        self.cdp = None


def _warm_class(page_class):
    """The page class plus bookkeeping: what a test adds is recorded so it can
    be removed, and ``close()`` hands the page back."""
    class WarmPage(page_class):
        def __tracked(self, name, *a, **k):
            result = getattr(page_class, name)(self, *a, **k)
            if hasattr(result, "dispose"):
                self._vn.disposables.append(result)
            else:       # an older Playwright: this cannot be taken back
                self._vn.retire = True
            return result

        def add_init_script(self, *a, **k):
            return self.__tracked("add_init_script", *a, **k)

        def expose_function(self, *a, **k):     # its name stays taken on this page
            self._vn.retire = True
            return page_class.expose_function(self, *a, **k)

        def expose_binding(self, *a, **k):
            self._vn.retire = True
            return page_class.expose_binding(self, *a, **k)

        # route: _reset_page's unroute_all takes those off.

        def route_from_har(self, *a, **k):
            self._vn.retire = True
            return page_class.route_from_har(self, *a, **k)

        @property
        def clock(self):        # Playwright's clock cannot be taken off a page
            self._vn.retire = True
            return page_class.clock.fget(self)

        def close(self, *a, **k):
            state = self._vn
            if not state.in_use:
                return None
            state.in_use = False
            with contextlib.suppress(Exception):
                page_class.goto(self, "about:blank")    # its timers stop here, not at the next test
                return None
            state.retire = True
            return None

        def is_closed(self):
            return not self._vn.in_use or page_class.is_closed(self)
    return WarmPage


_warm_classes: dict = {}


def _foreign(url: str) -> str:
    """The origin of ``url`` if it is a real one other than the page server's.
    An address with a dot in its host cannot resolve (see _launch_args)."""
    if url.startswith("file:"):
        return "file://"
    if not url.startswith(("http:", "https:")) or url.startswith(PAGE_ORIGIN + "/"):
        return ""
    origin = "/".join(url.split("/")[:3])
    host = origin.split("//")[1].rsplit(":", 1)[0]
    return origin if host == "127.0.0.1" or "." not in host else ""


def _new_warm_page(key, ctx):
    page = ctx.new_page()
    cls = type(page)
    page.__class__ = _warm_classes.setdefault(cls, _warm_class(cls))
    state = page._vn = _WarmState(key, ctx)
    state.cdp = ctx.new_cdp_session(page)
    page.on("request", lambda r: _note_origin(state, r.url))
    state.events = {k: dict(v) for k, v in page._impl_obj._events.items()}
    return page


def _note_origin(state, url):
    origin = _foreign(url)
    if origin:
        state.origins.add(origin)


def _drop_warm_page(page) -> None:
    state = page._vn
    _shared["pages"].pop(state.key, None)
    with contextlib.suppress(Exception):
        type(page).__mro__[1].close(page)


def _reset_page(page) -> None:
    """Back to what a new tab in an empty context is."""
    state = page._vn
    real_goto = type(page).__mro__[1].goto
    ctx = state.ctx
    # What the test added.
    impl = page._impl_obj
    impl._events.clear()
    for name, handlers in state.events.items():
        impl._events[name] = _handlers(handlers)
    for item in state.disposables:
        with contextlib.suppress(Exception):
            item.dispose()
    state.disposables.clear()
    page.unroute_all(behavior="ignoreErrors")
    for other in list(ctx.pages):
        if other is not page:
            other.close()
    ctx.set_offline(False)
    # The document, and with it every script, timer and socket.
    real_goto(page, "about:blank")
    page.set_extra_http_headers({})
    page.emulate_media(media="null", color_scheme="null", reduced_motion="null", forced_colors="null")
    page.set_default_timeout(30000)
    page.set_default_navigation_timeout(30000)
    # Pointer and keys: nothing held, the pointer where a new tab's is.
    page.mouse.up()
    page.mouse.move(0, 0)
    for key in ("Shift", "Control", "Alt", "Meta"):
        page.keyboard.up(key)
    # This tab's sessionStorage and name: only a page of the origin can reach them.
    real_goto(page, PAGE_ORIGIN + RESET_PATH)
    page.evaluate("() => { sessionStorage.clear(); window.name = ''; }")
    real_goto(page, "about:blank")
    # What a test leaves in the context.
    ctx.clear_cookies()
    ctx.clear_permissions()
    state.cdp.send("Storage.clearDataForOrigin", {"origin": PAGE_ORIGIN, "storageTypes": CLEARED})
    state.cdp.send("Page.resetNavigationHistory")
    state.origins.clear()


# What a test can leave in a context besides its pages.
CLEARED = "cookies,file_systems,indexeddb,local_storage,websql,service_workers,cache_storage"


def _handlers(saved):
    return collections.OrderedDict(saved)


def warm_page(browser, options: dict, size: dict):
    """(context, page) for one test: long-lived and reset, or None when the
    worker's page for these options is out with someone else."""
    real = getattr(browser, "_real", browser)
    if real is not _shared["browser"]:
        return None
    key = _json.dumps(options, sort_keys=True)
    page = _shared["pages"].get(key)
    if page is not None and page._vn.in_use and not type(page).__mro__[1].is_closed(page):
        return None
    if page is not None:
        state = page._vn
        http_foreign = any(o != "file://" for o in state.origins)
        real_closed = type(page).__mro__[1].is_closed(page)
        if real_closed or state.retire or state.origins:
            if "file://" in state.origins and not real_closed:
                with contextlib.suppress(Exception):
                    state.cdp.send("Storage.clearDataForOrigin", {"origin": "file://", "storageTypes": CLEARED})
            _drop_warm_page(page)
            page = None
            if http_foreign or real_closed:
                # Its HTTP cache holds another server's answers: as a new context would not.
                with contextlib.suppress(Exception):
                    state.ctx.close()
                _shared["warm"].pop(key, None)
        else:
            try:
                _reset_page(page)
            except Exception:
                _drop_warm_page(page)
                page = None
    ctx = _shared["warm"].get(key)
    if ctx is None:
        ctx = _shared["warm"][key] = real.new_context(viewport=size, **options)
    if page is None:
        for other in list(ctx.pages):
            other.close()
        ctx.clear_cookies()
        ctx.clear_permissions()
        ctx.set_offline(False)
        page = _new_warm_page(key, ctx)
        page._vn.cdp.send("Storage.clearDataForOrigin", {"origin": PAGE_ORIGIN, "storageTypes": CLEARED})
        _shared["pages"][key] = page
    page._vn.in_use = True
    page.set_viewport_size(size)
    return ctx, page


class _WarmBrowser:
    """The worker's browser, with ``new_page`` served by the long-lived page."""

    def __init__(self, real):
        self._real = real

    def __getattr__(self, name):
        return getattr(self._real, name)

    def new_page(self, *, viewport=None, **options):
        got = None if options.get("storage_state") else warm_page(
            self, options, viewport or {"width": 1280, "height": 720})
        if got is None:
            kw = dict(options)
            if viewport is not None:
                kw["viewport"] = viewport
            return self._real.new_page(**kw)
        return got[1]


@contextlib.contextmanager
def shared_browser_scope():
    """Use the shared browser for one test file; close what the file opened."""
    browser = shared_browser()
    before = set(browser.contexts)
    try:
        yield _WarmBrowser(browser)
    finally:
        warm = set(_shared["warm"].values())
        for ctx in list(browser.contexts):
            with contextlib.suppress(Exception):
                if ctx not in warm and ctx not in before:
                    ctx.close()
        for page in list(_shared["pages"].values()):    # no page stays out past its file
            with contextlib.suppress(Exception):
                page.close()


def close_shared_browser():
    browser, mgr = _shared["browser"], _shared["pw"]
    _shared.update(pw=None, browser=None, warm={}, pages={})
    if browser is not None:
        with contextlib.suppress(Exception):
            browser.close()
    if mgr is not None:
        with contextlib.suppress(Exception):
            mgr.stop()


def uses_shared_browser(module) -> bool:
    return getattr(module, "shared_browser_scope", None) is shared_browser_scope


def stop_throwaway_chrome(proc) -> None:
    """End a Chrome a test launched itself with a throwaway profile.

    On Windows a killed ``chrome.exe`` can take 10-20 s to exit while its
    renderer and GPU children live on (2 of 8 still alive after 20 s in one
    measurement), so ``kill()`` + ``wait(10)`` can fail passing tests in their
    cleanup.  Kill the whole tree; a Chrome that is still exiting after that is
    not the test's result, so it is never raised."""
    import os
    import subprocess
    if os.name == "nt":
        subprocess.run(["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                       capture_output=True, timeout=30,
                       creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    else:
        proc.kill()
    try:
        proc.wait(timeout=30)
    except subprocess.TimeoutExpired:
        pass
