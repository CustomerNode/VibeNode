"""The session list keeps its order while the pointer is on it.

A redraw that re-sorts the list or card grid ("Active first") used to move
cards under the mouse, so a click aimed at one session opened another.
``filterSessions`` (``src/web/static/js/app.js``) now holds redraws that do not
come from the person while the mouse is over the list, or a finger is on it or
scrolled it in the last moment, and runs one when the hold ends.

The page under test is the REAL ``index.html`` with every real script in
headless Chromium; only the network is faked (``tests/_fake_app.py``).
"""

from __future__ import annotations

import pytest

from tests._browser import shared_browser_scope
from tests._fake_app import CWD, FakeApp, fake_sessions, init_script

N = 20
ORDER = "() => [...document.querySelectorAll('#workforce-grid .wf-card')].map(c => c.dataset.sid)"


@pytest.fixture(scope="module")
def browser():
    with shared_browser_scope() as b:
        yield b


def _app(browser):
    sessions = fake_sessions(N)
    for s in sessions:
        s["hold"] = "done"
    app = FakeApp(browser, sessions, clock=True,
                  init=init_script("sessions", "grid", "localStorage.setItem('wfSort', 'status');"))
    return app, sessions


def _work(app, sessions):
    """The last half starts working: under Active first they move to the front."""
    app.push_many("session_state", [{"session_id": s["id"], "state": "working", "cwd": CWD}
                                    for s in sessions[N // 2:]])


def _hover_first_card(app):
    box = app.page.locator("#workforce-grid .wf-card").first.bounding_box()
    app.page.mouse.move(box["x"] + box["width"] / 2, box["y"] + box["height"] / 2)


@pytest.mark.slow
def test_order_holds_while_the_mouse_is_on_the_list_and_catches_up_after(browser):
    app, sessions = _app(browser)
    try:
        before = app.eval(ORDER)
        _hover_first_card(app)
        _work(app, sessions)
        app.advance(2000)
        assert app.eval(ORDER) == before, "cards moved under the mouse"
        # The rows themselves still show the new state in place.
        assert app.eval("() => document.querySelectorAll('#workforce-grid .wf-card.wf-working').length") == N // 2
        app.page.mouse.move(2, 2)            # off the list
        app.advance(600)
        working = {s["id"] for s in sessions[N // 2:]}
        assert set(app.eval(ORDER)[: N // 2]) == working
        assert not app.errors, app.errors
    finally:
        app.close()


@pytest.mark.slow
def test_a_finger_on_the_list_holds_it_until_shortly_after_lifting(browser):
    app, sessions = _app(browser)
    try:
        before = app.eval(ORDER)
        touch = """(type) => document.querySelector('#workforce-grid .wf-card').dispatchEvent(
            new PointerEvent(type, {pointerType: 'touch', bubbles: true}))"""
        app.eval(touch, "pointerdown")
        _work(app, sessions)
        app.advance(3000)
        assert app.eval(ORDER) == before
        app.eval(touch, "pointercancel")      # a scroll ends the touch this way
        app.advance(1000)
        assert app.eval(ORDER) == before, "reordered right after the finger lifted"
        app.advance(1000)
        assert set(app.eval(ORDER)[: N // 2]) == {s["id"] for s in sessions[N // 2:]}
        assert not app.errors, app.errors
    finally:
        app.close()


@pytest.mark.slow
def test_a_redraw_the_person_asked_for_is_never_held(browser):
    app, sessions = _app(browser)
    try:
        _hover_first_card(app)
        _work(app, sessions)
        app.advance(400)
        # A key press (a sort change, a filter) makes the next redraw theirs.
        app.page.keyboard.press("Shift")
        app.eval("() => filterSessions()")
        assert set(app.eval(ORDER)[: N // 2]) == {s["id"] for s in sessions[N // 2:]}
        assert not app.errors, app.errors
    finally:
        app.close()
