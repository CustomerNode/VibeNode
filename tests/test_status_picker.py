"""The status picker in the text box (owner's pick A, 2026-10-31,
static/scratch/status-picker.html and status-picker-a-flow.html).

A stopped session's status line above the text box shows the person's status
(Needs you, Standby, Done) and opens a menu to change it.  The menu runs the
same actions as the sidebar's right-click, so a pick is a hold that the next
message ends, and a running turn still shows Working.

Browser: the real ``index.html`` in headless Chromium (``live-panel.js``
``_pickCardOpen`` / ``_togglePickMenu``); only the network is faked.
"""

from __future__ import annotations

import json

import pytest

# At module level: conftest keeps the shared browser open only for files that
# import this name (``uses_shared_browser``).
from tests._browser import shared_browser_scope


@pytest.fixture(scope="module")
def browser():
    with shared_browser_scope() as b:
        yield b


STRIP = """() => { const c = document.querySelector('#live-input-bar .vn-card');
  const t = document.getElementById('live-input-ta');
  return c ? [c.dataset.st || '', c.querySelector('.vn-st-lbl').textContent,
              (c.querySelector('.vn-st-hint') || {}).textContent || '', t ? t.placeholder : '',
              !!c.querySelector('.vn-pick')] : null; }"""

MENU = """() => { const m = document.querySelector('#live-input-bar .vn-pick-menu');
  return m ? [...m.querySelectorAll('.vn-pick-mi')].map(b => b.querySelector('b').textContent + (b.classList.contains('cur') ? '*' : '')) : null; }"""


def _open(browser, state="stopped", extra=""):
    from tests._fake_app import CWD, FakeApp, fake_sessions, init_script
    s = fake_sessions(1)
    sid = s[0]["id"]
    holds: list[dict] = []

    def hold(req):
        holds.append(json.loads(req.post_data or "{}"))
        return 200, {"ok": True}

    app = FakeApp(browser, s, init=init_script("sessions", "grid", extra),
                  extra_routes={"/api/session/": lambda r: (200, {"id": sid, "display_title": "T", "messages": []}),
                                "/api/session-hold/": hold})
    app.push("session_state", {"session_id": sid, "state": state, "cwd": CWD})
    app.eval(f"() => openInGUI('{sid}')")
    app.page.wait_for_function(f"typeof liveSessionId !== 'undefined' && liveSessionId === '{sid}'", timeout=10000)
    return app, sid, holds


# Clicks go through the DOM: the page's splash and health overlays sit over
# everything in this fake app, so a pointer click never lands.
def _click_pick(app):
    app.eval("() => document.querySelector('#live-input-bar .vn-pick').click()")


def _pick(app, label):
    _click_pick(app)
    app.page.wait_for_selector("#live-input-bar .vn-pick-menu", timeout=3000)
    app.eval("(l) => [...document.querySelectorAll('#live-input-bar .vn-pick-mi')]"
             ".find(b => b.querySelector('b').textContent === l).click()", label)
    app.page.wait_for_timeout(250)


@pytest.mark.slow
def test_a_sleeping_session_changes_status_from_the_text_box(browser):
    app, sid, holds = _open(browser)
    try:
        app.page.wait_for_function("!!document.querySelector('#live-input-bar .vn-pick')", timeout=5000)
        assert app.eval(STRIP) == ["needs", "Needs you", "Its reply is waiting for you", "Type a message to wake it", True]

        # The menu lists the three choices with the current one checked.
        _click_pick(app)
        assert app.eval(MENU) == ["Needs you*", "Standby", "Done"]
        # Escape closes it without changing anything.
        app.eval("() => document.body.dispatchEvent(new KeyboardEvent('keydown', {key: 'Escape', bubbles: true}))")
        assert app.eval(MENU) is None and holds == []

        _pick(app, "Standby")
        assert holds[-1]["state"] == "standby"
        assert app.eval(STRIP) == ["standby", "Standby", "Set aside just now", "Type to pick it back up", True]
        # The sidebar agrees.
        assert app.eval(f"() => SessionVerb.shown(SessionVerb.display('{sid}'))") == "standby"

        _pick(app, "Done")
        assert holds[-1]["state"] == "done"
        assert app.eval(STRIP) == ["done", "Done", "Marked done just now", "Type to reopen it", True]

        _pick(app, "Needs you")
        assert holds[-1]["state"] is None
        assert app.eval(STRIP)[:2] == ["needs", "Needs you"]

        # Clicking outside closes an open menu.
        _click_pick(app)
        assert app.eval(MENU) is not None
        app.eval("() => document.body.click()")
        assert app.eval(MENU) is None
        assert app.errors == []
    finally:
        app.close()


@pytest.mark.slow
def test_a_new_message_ends_the_pick(browser):
    app, sid, _holds = _open(browser)
    try:
        app.page.wait_for_function("!!document.querySelector('#live-input-bar .vn-pick')", timeout=5000)
        _pick(app, "Standby")
        assert app.eval(STRIP)[1] == "Standby"
        # Automatic status takes back over at the next message.
        app.eval(f"() => SessionVerb.noteEntry('{sid}', {{kind: 'user', text: 'more'}})")
        app.page.wait_for_timeout(150)
        assert app.eval(STRIP)[:2] == ["needs", "Needs you"]
    finally:
        app.close()


@pytest.mark.slow
def test_an_awake_idle_session_shows_the_picker_too(browser):
    app, sid, holds = _open(browser, state="idle")
    try:
        app.page.wait_for_function("!!document.querySelector('#live-input-bar .vn-pick')", timeout=5000)
        assert app.eval(STRIP)[:2] == ["needs", "Needs you"]
        _pick(app, "Standby")
        assert holds and holds[-1]["state"] == "standby"
    finally:
        app.close()


@pytest.mark.slow
def test_a_working_session_shows_working_with_no_picker(browser):
    app, sid, _holds = _open(browser, state="working")
    try:
        app.page.wait_for_function("!!document.querySelector('#live-input-bar .vn-st-lbl')", timeout=5000)
        assert app.eval("() => document.querySelector('#live-input-bar .vn-st-lbl').textContent") == "Working"
        assert app.eval("() => !!document.querySelector('#live-input-bar .vn-pick')") is False
    finally:
        app.close()


@pytest.mark.slow
def test_with_person_statuses_off_there_is_no_picker(browser):
    app, sid, _holds = _open(browser, extra="localStorage.setItem('vnSmartTermination', 'off');")
    try:
        app.page.wait_for_function("!!document.querySelector('#live-input-bar .vn-st-lbl')", timeout=5000)
        assert app.eval(STRIP) == ["", "Sleeping", "Wakes when you send a message", "Type a message to wake it", False]
    finally:
        app.close()
