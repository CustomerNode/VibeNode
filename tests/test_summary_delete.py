"""Delete a session from its newest end-of-turn summary (owner's pick,
2026-10-29: "confirm on the button").

A quiet trash sits at the bottom right of the NEWEST summary only. Tapping it
turns that spot into "Delete session?" Delete / Cancel, with Cancel where the
trash was so a double-tap can never delete. Delete removes the session with
no second popup; Cancel puts the trash back.

The real ``index.html`` in headless Chromium at phone size; only the network
is faked.
"""

from __future__ import annotations

import pytest

from tests._browser import shared_browser_scope
from tests.test_card_lands_on_top import _tall_card


@pytest.fixture(scope="module")
def browser():
    with shared_browser_scope() as b:
        yield b


def _open(browser, deletes):
    from tests._fake_app import CWD, FakeApp, fake_sessions, init_script
    s = fake_sessions(1)
    sid = s[0]["id"]

    def _delete(r):
        deletes.append(r)
        return (200, {"ok": True})

    app = FakeApp(browser, s, init=init_script("sessions", "grid"),
                  viewport={"width": 390, "height": 760},
                  extra_routes={"/api/session/": lambda r: (200, {"id": sid, "display_title": "T", "messages": []}),
                                "/api/delete/": _delete})
    app.push("session_state", {"session_id": sid, "state": "idle", "cwd": CWD})
    app.eval(f"() => openInGUI('{sid}')")
    app.page.wait_for_function(f"typeof liveSessionId !== 'undefined' && liveSessionId === '{sid}'", timeout=10000)
    app.page.wait_for_timeout(400)
    app.push("session_log", {"session_id": sid, "total": 3, "offset": 0, "has_more": False, "entries": [
        {"kind": "asst", "text": _tall_card()},
        {"kind": "user", "text": "next"},
        {"kind": "asst", "text": _tall_card()}]})
    app.page.wait_for_function("document.querySelectorAll('#live-log .lm').length >= 2", timeout=5000)
    app.push("session_state", {"session_id": sid, "state": "idle", "cwd": CWD})
    app.page.wait_for_timeout(500)
    return app, sid


TRASH_PER_CARD = """() => Array.from(document.querySelectorAll('#live-log .lm.skim'))
  .map(c => !!c.querySelector(':scope > .lm-del'))"""


@pytest.mark.slow
def test_only_the_newest_summary_has_the_trash(browser):
    app, _sid = _open(browser, [])
    try:
        assert app.eval(TRASH_PER_CARD) == [False, True]
        assert not app.errors, app.errors
    finally:
        app.close()


@pytest.mark.slow
def test_trash_asks_in_place_and_cancel_sits_where_the_trash_was(browser):
    deletes = []
    app, _sid = _open(browser, deletes)
    try:
        # The fake app keeps its splash over the page, so tap through script
        # and compare positions: Cancel must cover the spot the trash was in.
        trash = app.eval("""() => { const b = document.querySelector('#live-log .lm-hasdel > .lm-del');
          const r = b.getBoundingClientRect(); b.click();
          return {x: r.left + r.width / 2, y: r.top + r.height / 2}; }""")
        app.page.wait_for_timeout(200)
        assert app.eval("() => !!document.querySelector('#live-log .lm-delask')")
        # A second tap on the same spot lands on Cancel, never Delete.
        hit = app.eval("""(p) => { const r = document.querySelector('#live-log .lm-delask .no').getBoundingClientRect();
          return r.left <= p.x && p.x <= r.right && r.top <= p.y && p.y <= r.bottom; }""", trash)
        assert hit, "Cancel does not sit where the trash was"
        app.eval("() => document.querySelector('#live-log .lm-delask .no').click()")
        app.page.wait_for_timeout(200)
        assert not app.eval("() => !!document.querySelector('#live-log .lm-delask')")
        assert app.eval("() => !document.querySelector('#live-log .lm-hasdel > .lm-del').hidden")
        assert deletes == []
        assert not app.errors, app.errors
    finally:
        app.close()


@pytest.mark.slow
def test_delete_removes_the_session_with_no_second_popup(browser):
    deletes = []
    app, sid = _open(browser, deletes)
    try:
        app.eval("() => document.querySelector('#live-log .lm-hasdel > .lm-del').click()")
        app.page.wait_for_timeout(150)
        app.eval("() => document.querySelector('#live-log .lm-delask .yes').click()")
        app.page.wait_for_timeout(800)
        assert len(deletes) == 1
        assert not app.eval("() => { const o = document.getElementById('pm-overlay'); return !!(o && o.classList.contains('show')); }")
        assert sid in deletes[0].url
        assert app.eval("() => (document.getElementById('toast') || {}).textContent") == "Session deleted"
        assert not app.errors, app.errors
    finally:
        app.close()
