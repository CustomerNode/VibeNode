"""The shared browser context and the page clock must not change what a browser
test proves.

Browser tests share one long-lived context per worker because its HTTP cache is
what makes a page load cheap (tests/_browser.py).  Every test must still start
as if the context were new, and PAGE_CLOCK, used because Playwright's clock
cannot leave a context again, must behave as Playwright's does.
"""

import pytest

pytest.importorskip("playwright.sync_api")

from tests._browser import shared_browser_scope  # noqa: E402
from tests._fake_app import ORIGIN, PAGE_CLOCK, FakeApp, PageClock, fake_sessions  # noqa: E402

LEAVE_EVERYTHING = """async () => {
  localStorage.setItem('left', '1');
  sessionStorage.setItem('left', '1');
  document.cookie = 'left=1; max-age=3600';
  await new Promise((ok, no) => {
    const r = indexedDB.open('left-db', 1);
    r.onupgradeneeded = () => r.result.createObjectStore('s');
    r.onsuccess = () => { r.result.close(); ok(); };
    r.onerror = no;
  });
  await (await caches.open('left-cache')).put('/left', new Response('x'));
  window.__left = 1;
}"""

WHAT_IS_LEFT = """async () => ({
  local: localStorage.getItem('left'),
  session: sessionStorage.getItem('left'),
  cookie: document.cookie,
  dbs: (await indexedDB.databases()).map(d => d.name).filter(n => n === 'left-db'),
  caches: (await caches.keys()).filter(n => n === 'left-cache'),
  win: window.__left || null,
  marker: window.__fromFirstInit || null,
  clock: !!window.__vnClock,
})"""

EMPTY = {"local": None, "session": None, "cookie": "", "dbs": [], "caches": [],
         "win": None, "marker": None, "clock": False}


@pytest.fixture(scope="module")
def browser():
    with shared_browser_scope() as b:
        yield b


def _ready(browser, **kw):
    return FakeApp(browser, fake_sessions(2), wait_for="typeof socket !== 'undefined'", **kw)


def test_a_test_starts_with_nothing_left_by_the_one_before(browser):
    from tests._fake_app import init_script
    first = _ready(browser, clock=True,
                   init=init_script() + "window.__fromFirstInit = 1;")
    first.page.context.grant_permissions(["clipboard-read"], origin=ORIGIN)
    first.eval(LEAVE_EVERYTHING)
    assert first.eval(WHAT_IS_LEFT)["local"] == "1"     # the writes did land
    ctx = first.ctx
    first.close()

    second = _ready(browser)
    assert second.ctx is ctx, "the point: the same long-lived context"
    assert second.eval(WHAT_IS_LEFT) == EMPTY
    assert second.eval("async () => (await navigator.permissions.query({name: 'clipboard-read'})).state") != "granted"
    second.close()


def test_the_next_test_is_wiped_even_if_this_one_never_closes_its_app(browser):
    _ready(browser).eval(LEAVE_EVERYTHING)


def test_after_a_test_that_never_closed_its_app(browser):
    app = _ready(browser)
    assert app.eval(WHAT_IS_LEFT) == EMPTY
    assert len(app.ctx.pages) == 1
    app.close()


def test_two_apps_at_once_do_not_share_storage(browser):
    a, b = _ready(browser), _ready(browser)
    assert a.ctx is not b.ctx
    a.eval(LEAVE_EVERYTHING)
    assert b.eval(WHAT_IS_LEFT) == EMPTY
    b.close()
    a.close()


def test_a_second_page_load_comes_from_the_cache(browser):
    # Guards the speed: a route on the page, or a no-store on the app's own
    # files, would quietly turn the cache off.
    for _ in range(4):      # Chromium writes its cache just after a load: on a busy machine, later
        app = _ready(browser)
        cached = app.eval("""() => {
            const e = performance.getEntriesByType('resource').filter(x => x.name.includes('/static/'));
            return [e.length, e.filter(x => x.transferSize === 0).length];
        }""")
        app.close()
        if cached[1] == cached[0]:
            break
    assert cached[0] > 20 and cached[1] == cached[0], cached


def test_an_outside_address_never_reaches_the_network(browser):
    app = _ready(browser)
    failed = app.eval("() => fetch('https://example.com/').then(() => false, () => true)")
    app.close()
    assert failed


# ── the long-lived page is a new tab again ───────────────────────────────────
# Each test gets the worker's one page per device kind, because a new page is a
# new renderer process.  Everything below would survive a navigation.

SEES = """async () => ({
  history: history.length,
  name: window.name,
  width: innerWidth,
  height: innerHeight,
  reduced: matchMedia('(prefers-reduced-motion: reduce)').matches,
  dark: matchMedia('(prefers-color-scheme: dark)').matches,
  print: matchMedia('print').matches,
  online: navigator.onLine,
  hovering: document.querySelectorAll(':hover').length,
  session: sessionStorage.length,
  local: localStorage.length,
  cookie: document.cookie,
  init: window.__leftInit || null,
  bound: typeof window.__leftBinding,
  referrer: document.referrer,
  opener: !!window.opener,
})"""


def _plain(browser, viewport):
    """The same page in a new context and a new tab: what every test must see."""
    ctx = browser._real.new_context(viewport=viewport)
    page = ctx.new_page()
    return ctx, page


def _sees_after(browser, dirty, url=ORIGIN + "/__vn_reset", reused=True):
    """What a test sees on the long-lived page after ``dirty`` used it, next to
    what it would see in a new tab.  ``reused`` False: what ``dirty`` did
    cannot be taken back, so the page must be replaced, never handed on."""
    page = browser.new_page(viewport={"width": 700, "height": 500})
    first = page
    try:
        dirty(page)
    finally:
        page.close()
    page = browser.new_page(viewport={"width": 640, "height": 480})
    try:
        if reused() if callable(reused) else reused:
            assert page is first, "the point: the same page, so the same renderer"
        else:
            assert page is not first, "a page that cannot be made new is replaced"
        page.goto(url)
        got = page.evaluate(SEES)
    finally:
        page.close()
    ctx, fresh = _plain(browser, {"width": 640, "height": 480})
    fresh.goto(url)
    expected = fresh.evaluate(SEES)
    ctx.close()
    return got, expected


def _at_origin(page):
    page.goto(ORIGIN + "/__vn_reset")


def test_a_clean_use_already_looks_like_a_new_tab(browser):
    got, expected = _sees_after(browser, _at_origin)
    assert got == expected


def test_this_tabs_session_storage_and_name_are_gone(browser):
    def dirty(page):
        _at_origin(page)
        page.evaluate("() => { sessionStorage.setItem('left', '1'); window.name = 'left'; }")
    got, expected = _sees_after(browser, dirty)
    assert got == expected


def test_back_has_nowhere_to_go(browser):
    def dirty(page):
        for i in range(4):
            page.goto(f"{ORIGIN}/__vn_reset?{i}")
    got, expected = _sees_after(browser, dirty)
    assert got == expected


def test_held_keys_and_buttons_and_the_pointer_are_let_go(browser):
    def dirty(page):
        page.set_content("<div style='position:fixed;inset:0'>x</div>")
        page.mouse.move(100, 100)
        page.mouse.down()
        page.keyboard.down("Shift")
    page = browser.new_page()
    dirty(page)
    page.close()
    page = browser.new_page()
    page.set_content("""<div id=d style='position:fixed;inset:0'>x</div><script>
      window.__e = []; addEventListener('mousemove', e => __e.push(['m', e.buttons]));
      addEventListener('keydown', e => __e.push(['k', e.shiftKey]));</script>""")
    assert page.evaluate("() => document.querySelectorAll(':hover').length") == 0
    page.mouse.move(5, 5)
    page.keyboard.press("a")
    assert page.evaluate("() => __e") == [["m", 0], ["k", False]]
    page.close()


def test_emulated_media_and_offline_are_undone(browser):
    def dirty(page):
        _at_origin(page)
        page.emulate_media(media="print", color_scheme="dark", reduced_motion="reduce")
        page.context.set_offline(True)
    got, expected = _sees_after(browser, dirty)
    assert got == expected


def test_init_scripts_and_headers_are_taken_back(browser):
    # Playwright 1.59+ returns a handle that takes an init script back; with an
    # older one (1.58 on the Windows venv) the page is retired instead.
    handles = []

    def dirty(page):
        handles.append(page.add_init_script("window.__leftInit = 1;"))
        page.set_extra_http_headers({"x-left": "1"})
        _at_origin(page)
        assert page.evaluate("() => window.__leftInit") == 1
    got, expected = _sees_after(browser, dirty, reused=lambda: hasattr(handles[0], "dispose"))
    assert got == expected


def test_extra_headers_do_not_reach_the_next_tests_requests(browser):
    from tests._browser import register_page_app, unregister_page_app
    page = browser.new_page()
    page.set_extra_http_headers({"x-left": "1"})
    page.close()
    page = browser.new_page()
    seen = []

    def answer(request):
        seen.append(request.headers)
        return 200, "text/html", b"", "no-store"
    register_page_app("headers", answer)
    page.goto(ORIGIN + "/seen")
    unregister_page_app("headers")
    page.close()
    assert seen and not any("x-left" in {k.lower() for k in h} for h in seen)


def test_a_page_with_an_exposed_function_is_not_used_again(browser):
    page = browser.new_page()
    page.expose_function("__leftBinding", lambda: 1)
    page.close()
    again = browser.new_page()
    assert again is not page
    again.goto(ORIGIN + "/__vn_reset")
    assert again.evaluate("() => typeof window.__leftBinding") == "undefined"
    again.close()


def test_a_tests_listeners_and_routes_never_hear_the_next_test(browser):
    heard = []
    page = browser.new_page()
    page.on("console", lambda m: heard.append(m.text))
    page.on("request", lambda r: heard.append(r.url))
    page.route("**/__vn_reset*", lambda route: (heard.append("routed"), route.continue_()))
    _at_origin(page)
    assert "routed" in heard
    page.close()
    heard.clear()
    page = browser.new_page()
    page.goto(ORIGIN + "/__vn_reset?next")
    page.evaluate("() => console.log('from the next test')")
    assert heard == []
    page.close()


def test_a_default_timeout_does_not_carry_over(browser):
    page = browser.new_page()
    page.set_default_timeout(1)
    page.close()
    page = browser.new_page()
    page.goto(ORIGIN + "/__vn_reset")
    page.evaluate("() => setTimeout(() => { document.title = 'late'; }, 60)")
    page.wait_for_function("document.title === 'late'")     # 1 ms would have failed this
    page.close()


def test_popups_go_with_the_test(browser):
    page = browser.new_page()
    _at_origin(page)
    with page.expect_popup():
        page.evaluate("() => { window.open('about:blank'); }")
    ctx = page.context
    page.close()
    page = browser.new_page()
    assert ctx.pages == [page]
    page.close()


def test_a_page_still_timers_stop_when_its_test_hands_it_back(browser):
    page = browser.new_page()
    _at_origin(page)
    page.evaluate("() => { setInterval(() => console.log('still running'), 1); }")
    page.close()
    assert page.url == "about:blank"


def test_closing_looks_closed_and_a_page_in_use_is_never_shared(browser):
    a = browser.new_page()
    b = browser.new_page()
    assert a is not b and a.context is not b.context    # the second gets its own tab, as before
    b.evaluate("() => 1")
    b.close()
    assert not a.is_closed()
    a.close()
    assert a.is_closed()
    assert b.is_closed()


def test_playwrights_clock_retires_the_page(browser):
    page = browser.new_page()
    page.clock.install()
    page.close()
    again = browser.new_page()
    assert again is not page
    again.close()


def test_another_web_servers_cache_goes_with_the_context(browser, tmp_path):
    import functools
    import http.server
    import threading
    (tmp_path / "x.html").write_text("first", encoding="utf-8")

    class H(http.server.SimpleHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def end_headers(self):
            self.send_header("Cache-Control", "max-age=3600")
            super().end_headers()
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), functools.partial(H, directory=str(tmp_path)))
    threading.Thread(target=srv.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True).start()
    url = f"http://127.0.0.1:{srv.server_address[1]}/x.html"
    try:
        page = browser.new_page()
        page.goto(url)
        assert "first" in page.content()
        ctx = page.context
        page.close()
        (tmp_path / "x.html").write_text("second", encoding="utf-8")
        page = browser.new_page()
        assert page.context is not ctx
        page.goto(url)
        assert "second" in page.content()
        page.close()
    finally:
        srv.shutdown()


def test_a_file_pages_storage_is_gone(browser, tmp_path):
    f = tmp_path / "x.html"
    f.write_text("<p>x</p>", encoding="utf-8")
    page = browser.new_page()
    page.goto(f.as_uri())
    page.evaluate("() => { localStorage.setItem('left', '1'); sessionStorage.setItem('left', '1'); }")
    page.close()
    page = browser.new_page()
    page.goto(f.as_uri())
    assert page.evaluate("() => [localStorage.getItem('left'), sessionStorage.getItem('left')]") == [None, None]
    page.close()


def test_a_crashed_or_closed_page_is_replaced(browser):
    page = browser.new_page()
    real_close = type(page).__mro__[1].close
    real_close(page)
    again = browser.new_page()
    assert again is not page
    again.goto(ORIGIN + "/__vn_reset")
    assert again.evaluate("() => 1 + 1") == 2
    again.close()


# ── PAGE_CLOCK does what Playwright's clock does ─────────────────────────────

SCENARIO = """() => {
  window.__log = [];
  const log = (m) => window.__log.push(m);
  setTimeout(() => { log('t100'); setTimeout(() => log('t100+70'), 70); }, 100);
  const cancelled = setTimeout(() => log('cancelled'), 120); clearTimeout(cancelled);
  setTimeout((a, b) => log('args ' + a + b), 150, 1, 2);
  const every = setInterval(() => { log('i200'); if (window.__log.filter(x => x === 'i200').length === 3) clearInterval(every); }, 200);
  setTimeout(() => { Promise.resolve().then(() => log('micro')); log('t300'); }, 300);
  setTimeout(() => log('t5000'), 5000);
  window.__d0 = Date.now(); window.__p0 = performance.now();
}"""
READ = "() => [window.__log, Math.round((Date.now() - window.__d0) / 100), Math.round((performance.now() - window.__p0) / 100)]"


def _run_scenario(page, clock):
    # Both clocks flow in real time between advances, so on a loaded machine
    # the wall time spent between these calls leaked into the measured window
    # (13 vs 12 tenths) and could fire timers before run_for. Pausing first
    # makes the window exactly what run_for advanced, on both clocks.
    clock.pause_at("2030-01-01T00:00:00Z")
    page.evaluate(SCENARIO)
    clock.run_for(40)
    early = page.evaluate(READ)
    clock.run_for(1160)
    later = page.evaluate(READ)
    return early, later


def test_page_clock_fires_the_same_timers_in_the_same_order_as_playwrights(browser):
    ctx = browser.new_context()
    theirs = ctx.new_page()
    theirs.clock.install()
    theirs.goto("about:blank")
    expected = _run_scenario(theirs, theirs.clock)
    ctx.close()

    mine = browser.new_page()
    mine.add_init_script(PAGE_CLOCK)
    mine.goto("about:blank")
    got = _run_scenario(mine, PageClock(mine))
    mine.close()
    assert got[0][0] == expected[0][0] == []            # nothing fires early
    assert got[1][0] == expected[1][0]
    assert 't5000' not in got[1][0]
    assert got[1][1] == expected[1][1] == got[1][2]     # both clocks moved by the window


def test_page_clock_flows_in_real_time_between_advances(browser):
    page = browser.new_page()
    page.add_init_script(PAGE_CLOCK)
    page.goto("about:blank")
    page.evaluate("() => { window.__fired = false; setTimeout(() => { window.__fired = true; }, 30); }")
    page.wait_for_function("window.__fired === true", timeout=2000)
    page.close()


def test_page_clock_pause_freezes_until_run_for(browser):
    page = browser.new_page()
    page.add_init_script(PAGE_CLOCK)
    page.goto("about:blank")
    clock = PageClock(page)
    page.evaluate("() => { window.__n = 0; setTimeout(() => window.__n++, 10000); setInterval(() => window.__n += 10, 86400000); }")
    clock.pause_at("2100-01-01T00:00:00Z")
    assert page.evaluate("() => new Date().getUTCFullYear()") == 2100
    assert page.evaluate("() => window.__n") == 11      # each due timer fired once
    t = page.evaluate("() => Date.now()")
    page.evaluate("() => { setTimeout(() => { window.__late = true; }, 20); }")
    page.wait_for_timeout(60)
    assert not page.evaluate("() => window.__late === true")    # frozen
    assert page.evaluate("() => Date.now()") == t
    clock.run_for(20)
    assert page.evaluate("() => window.__late === true")
    assert page.evaluate("() => Date.now()") == t + 20
    page.close()
