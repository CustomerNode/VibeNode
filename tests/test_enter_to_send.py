"""Enter to send follows the device's keyboard, not the window width.

Reported 2026-10-18: with Preferences -> "Enter to send" on, Enter added a new
line and only the send arrow sent.  ``_shouldSend`` (static/js/utils.js)
treated any window 768px or narrower as a phone and ignored the preference,
and a half-screen browser on a scaled laptop is that narrow.  The phone rule
now keys on a touch-first device (``pointer: coarse``), which also stops a
phone in landscape (wider than 768px) from sending on Enter.

Also: the Preferences switch saved the setting only to localStorage, and the
server copy restored on every reconnect (``ui_prefs_loaded``) undid it.  Both
switches now go through ``_setSendBehavior``, which saves to the server too.

The page is the REAL templates/index.html with every real script; only the
network is faked (the harness of test_live_thinking_switch.py).
"""

from __future__ import annotations

import json

import pytest

from tests._browser import shared_browser_scope
from tests.test_live_thinking_switch import (
    _CTYPES, FAKE_IO, INIT, MODELS, ORIGIN, PROJECT, ROW, SID, STATIC, CWD, _index_html,
)


@pytest.fixture(scope="module")
def browser():
    with shared_browser_scope() as b:
        yield b


_IPHONE_UA = ("Mozilla/5.0 (iPhone; CPU iPhone OS 18_0 like Mac OS X) AppleWebKit/605.1.15 "
              "(KHTML, like Gecko) Version/18.0 Mobile/15E148 Safari/604.1")
_ANDROID_UA = ("Mozilla/5.0 (Linux; Android 15; Pixel 9) AppleWebKit/537.36 "
               "(KHTML, like Gecko) Chrome/140.0.0.0 Mobile Safari/537.36")
_WINDOWS_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
               "(KHTML, like Gecko) Chrome/140.0.0.0 Safari/537.36")


def _open(browser, width, height=900, touch=False, behavior="enter", extra_init="",
          user_agent=None):
    index = _index_html()

    def route(route, request):
        url = request.url
        if url.startswith("https://cdn.socket.io/"):
            return route.fulfill(body=FAKE_IO, content_type="application/javascript")
        if not url.startswith(ORIGIN):
            return route.abort()
        path = url[len(ORIGIN):].split("?")[0]
        if path == "/":
            return route.fulfill(body=index, content_type="text/html")
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

    kw = {"user_agent": user_agent} if user_agent else {}
    ctx = browser.new_context(viewport={"width": width, "height": height}, has_touch=touch,
                              is_mobile=touch, **kw)
    ctx.add_init_script(extra_init + INIT
                        + "localStorage.setItem('sendBehavior', %s);" % json.dumps(behavior))
    page = ctx.new_page()
    page.route("**/*", route)
    page.goto(ORIGIN + "/", wait_until="domcontentloaded")
    page.wait_for_function("typeof updateLiveInputBar === 'function' && typeof socket !== 'undefined'",
                           timeout=20000)
    # An idle session with its composer rendered by the real code; record sends.
    page.evaluate("""(sid) => {
        socket.connected = true; liveSessionId = sid; runningIds.add(sid); sessionKinds[sid] = 'idle';
        if (!document.getElementById('live-input-bar')) {
            const d = document.createElement('div'); d.id = 'live-input-bar'; document.body.appendChild(d);
        }
        window.__sent = 0; window.liveSubmitIdle = function () { window.__sent++; };
        updateLiveInputBar();
    }""", SID)
    return page


def _type_and_press(page, key):
    page.focus("#live-input-ta")
    page.keyboard.type("hello")
    page.keyboard.press(key)
    return page.evaluate("window.__sent"), page.input_value("#live-input-ta")


@pytest.mark.slow
@pytest.mark.parametrize("width", [1400, 760, 600])
def test_enter_sends_on_a_keyboard_device_at_any_width(browser, width):
    """The reported case is the narrow window (760, 600)."""
    page = _open(browser, width)
    sent, text = _type_and_press(page, "Enter")
    assert sent == 1 and text == "hello"


@pytest.mark.slow
@pytest.mark.parametrize("ua", [_IPHONE_UA, _ANDROID_UA], ids=["iphone", "android"])
@pytest.mark.parametrize("width,height", [(390, 844), (844, 390)])
def test_enter_is_a_new_line_on_a_touch_screen_portrait_and_landscape(browser, width, height, ua):
    """Landscape (844 wide) used to count as a desktop and send on Enter."""
    page = _open(browser, width, height, touch=True, user_agent=ua)
    sent, text = _type_and_press(page, "Enter")
    assert sent == 0 and text == "hello\n"


# Chrome on a Surface Pro: the touchscreen is the primary pointer and, with the
# Type Cover folded or its touchpad not counted, no fine pointer is reported at
# all.  Reported twice on 2026-10-19: Enter added a new line despite "Enter to
# send", and Preferences showed the touch note.
_TOUCHSCREEN_LAPTOP = """(() => {
  const real = window.matchMedia.bind(window);
  const fake = { '(pointer: coarse)': true, '(any-pointer: fine)': false,
                 '(any-pointer: coarse)': true, '(hover: hover)': false };
  window.matchMedia = (q) => (q in fake)
    ? { matches: fake[q], media: q, addEventListener() {}, removeEventListener() {},
        addListener() {}, removeListener() {} }
    : real(q);
})();
"""


@pytest.mark.slow
def test_enter_sends_on_a_windows_touchscreen_with_no_fine_pointer(browser):
    page = _open(browser, 1400, touch=True, extra_init=_TOUCHSCREEN_LAPTOP,
                 user_agent=_WINDOWS_UA)
    assert page.evaluate("_isTouchKeyboard()") is False
    sent, text = _type_and_press(page, "Enter")
    assert sent == 1 and text == "hello"


@pytest.mark.slow
def test_ctrl_enter_mode_still_works_in_a_narrow_window(browser):
    page = _open(browser, 700, behavior="ctrl-enter")
    sent, text = _type_and_press(page, "Enter")
    assert sent == 0 and text == "hello\n"
    page.keyboard.press("Control+Enter")
    assert page.evaluate("window.__sent") == 1


@pytest.mark.slow
def test_the_preferences_switch_saves_to_the_server(browser):
    """Saved only to localStorage, the next reconnect's ui_prefs_loaded
    restored the server's old value."""
    page = _open(browser, 1400, behavior="ctrl-enter")
    page.evaluate("() => { window.__emits.length = 0; openPreferences(); }")
    page.evaluate("document.querySelector(\"#pm-overlay [data-toggle='send']\").click()")
    emits = page.evaluate("window.__emits.filter(e => e[0] === 'set_ui_prefs').map(e => e[1])")
    assert emits == [{"sendBehavior": "enter"}]
    assert page.evaluate("localStorage.getItem('sendBehavior')") == "enter"
    sent, _ = _type_and_press(page, "Enter")
    assert sent == 1
