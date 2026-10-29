"""End-of-turn summary cards are read from their TOP (2026-10-29, the owner
on a phone: "instead of scrolling to bottom of card it should scroll to top
of card").

"Jump to latest" from partway down the final card went to the card's last
line: ``catchUp()`` in ``thread-scroll.js`` treated any position past the
card's top as a second tap. It now goes back up to the card's title, and only
a tap from the title goes on to the bottom. Opening a session that ends on a
tall card already lands on its top; that is pinned here too.

The real ``index.html`` in headless Chromium at phone size; only the network
is faked.
"""

from __future__ import annotations

import json

import pytest

from tests._browser import shared_browser_scope


@pytest.fixture(scope="module")
def browser():
    with shared_browser_scope() as b:
        yield b


def _tall_card() -> str:
    card = {
        "type": "done", "path": ["Summaries"], "title": "Summary checker ceiling",
        "headline": "Broken summaries now go back up to five times before they reach you.",
        "context": "Stopping unusable end-of-turn cards.",
        "status": {"done": [f"Fix number {i} is in and checked" for i in range(1, 5)]},
        "verified": [{"check": f"Check {i}", "result": "passed", "ok": True} for i in range(1, 5)],
        "links": [{"label": "The change", "kind": "file", "url": "/static/style.css"}],
        "validate": {"how": "Open a session and look", "url": "/static/style.css"},
        "zoom": {"big": "Smart termination", "task": "Stop broken summaries", "now": "Built all three fixes"},
        "explained": " ".join(["This explains the card in more words for someone who is lost."] * 6),
        "needs": {"kind": "none", "why": "It is finished.", "next": "They go out with your next Update."},
    }
    return "```handoff\n" + json.dumps(card) + "\n```"


# Where the card's top sits relative to the top of the thread, in px.
CARD_TOP = """() => { const log = document.getElementById('live-log');
  const msgs = log.querySelectorAll('.msg'); const m = msgs[msgs.length - 1];
  return Math.round(m.getBoundingClientRect().top - log.getBoundingClientRect().top); }"""


@pytest.mark.slow
def test_opening_a_session_lands_on_the_top_of_its_tall_card(browser):
    from tests._fake_app import CWD, FakeApp, fake_sessions, init_script
    s = fake_sessions(1)
    sid = s[0]["id"]
    app = FakeApp(browser, s, init=init_script("sessions", "grid"),
                  viewport={"width": 390, "height": 760},
                  extra_routes={"/api/session/": lambda r: (200, {"id": sid, "display_title": "T", "messages": []})})
    try:
        app.push("session_state", {"session_id": sid, "state": "idle", "cwd": CWD})
        app.eval(f"() => openInGUI('{sid}')")
        app.page.wait_for_function(f"typeof liveSessionId !== 'undefined' && liveSessionId === '{sid}'", timeout=10000)
        # The thread arrives over the network a moment after the panel opens;
        # by then the scroll code's observers have settled on the empty log.
        app.page.wait_for_timeout(400)
        app.push("session_log", {"session_id": sid, "total": 3, "offset": 0, "has_more": False, "entries": [
            {"kind": "user", "text": "build all three fixes"},
            {"kind": "asst", "text": "Earlier reply. " * 40},
            {"kind": "asst", "text": _tall_card()}]})
        app.page.wait_for_function("!!document.querySelector('#live-log .lm')", timeout=5000)
        # The card is taller than the thread, so it must be read from its top.
        tall = app.eval("""() => { const log = document.getElementById('live-log');
          const msgs = log.querySelectorAll('.msg');
          return msgs[msgs.length - 1].offsetHeight > log.clientHeight; }""")
        assert tall
        # The text box redraws on the next state push (turn ended / session opened).
        app.push("session_state", {"session_id": sid, "state": "idle", "cwd": CWD})
        app.page.wait_for_timeout(600)
        top = app.eval(CARD_TOP)
        assert -2 <= top <= 40, f"card top sits {top}px from the top of the thread (expected near 0)"
        assert not app.errors, app.errors
    finally:
        app.close()


def _open_with_reply(browser):
    from tests._fake_app import CWD, FakeApp, fake_sessions, init_script
    s = fake_sessions(1)
    sid = s[0]["id"]
    app = FakeApp(browser, s, init=init_script("sessions", "grid"),
                  viewport={"width": 390, "height": 760},
                  extra_routes={"/api/session/": lambda r: (200, {"id": sid, "display_title": "T", "messages": []})})
    app.push("session_state", {"session_id": sid, "state": "idle", "cwd": CWD})
    app.eval(f"() => openInGUI('{sid}')")
    app.page.wait_for_function(f"typeof liveSessionId !== 'undefined' && liveSessionId === '{sid}'", timeout=10000)
    app.page.wait_for_timeout(400)
    # Enough thread to scroll at phone size.
    earlier = [{"kind": "user", "text": "build all three fixes"}] + [
        {"kind": "asst", "text": "\n\n".join(["Earlier reply, step %d of the work." % i] * 6)} for i in range(8)]
    app.push("session_log", {"session_id": sid, "total": len(earlier), "offset": 0, "has_more": False,
                             "entries": earlier})
    app.page.wait_for_function("document.querySelectorAll('#live-log .msg').length >= 9", timeout=5000)
    assert app.eval("() => { const l = document.getElementById('live-log'); return l.scrollHeight > l.clientHeight * 1.5; }")
    app.page.wait_for_timeout(300)
    app.push("session_state", {"session_id": sid, "state": "working", "cwd": CWD})
    app.page.wait_for_timeout(200)
    return app, sid, CWD


@pytest.mark.slow
def test_a_card_arriving_live_lands_with_its_top_at_the_top(browser):
    """The owner (2026-10-29): "When the answer card comes the scroll should be
    so the top of the card is at the top of the screen." Auto scroll is off
    (the default); the card arrives live while he watches the bottom."""
    app, sid, cwd = _open_with_reply(browser)
    try:
        assert app.eval("() => ThreadScroll.isLiveScroll()") is False
        app.push("session_entry", {"session_id": sid, "index": 9,
                                   "entry": {"kind": "asst", "text": _tall_card()}})
        app.page.wait_for_function("!!document.querySelector('#live-log .lm')", timeout=5000)
        app.page.wait_for_timeout(400)
        top = app.eval(CARD_TOP)
        assert 0 <= top <= 60, f"card top sits {top}px from the top of the thread"
        # The turn ends and the text box redraws: the card stays put.
        app.push("session_state", {"session_id": sid, "state": "idle", "cwd": cwd})
        app.page.wait_for_timeout(600)
        top = app.eval(CARD_TOP)
        assert 0 <= top <= 60, f"after the turn ended the card top sits {top}px from the top"
        assert not app.errors, app.errors
    finally:
        app.close()


@pytest.mark.slow
def test_a_card_arriving_live_lands_on_top_after_he_scrolled_along(browser):
    """The owner (2026-10-29): "the scroll so it just has the top of the card
    to the top of screen is broken". On a phone he scrolls down with the work
    during a turn; any touch scroll off the very bottom marked him READING, and
    the card then never moved. Following along near the end is not reading
    history: the card must still land with its top at the top."""
    app, sid, _cwd = _open_with_reply(browser)
    try:
        app.eval("""() => { const l = document.getElementById('live-log');
          l.dispatchEvent(new TouchEvent('touchstart')); l.dispatchEvent(new TouchEvent('touchmove'));
          l.scrollTop = l.scrollHeight - l.clientHeight - 150; }""")
        app.page.wait_for_timeout(400)
        assert app.eval("() => ThreadScroll.state()") == "READING"
        app.push("session_entry", {"session_id": sid, "index": 9,
                                   "entry": {"kind": "asst", "text": _tall_card()}})
        app.page.wait_for_function("!!document.querySelector('#live-log .lm')", timeout=5000)
        app.page.wait_for_timeout(400)
        top = app.eval(CARD_TOP)
        assert 0 <= top <= 60, f"card top sits {top}px from the top of the thread"
        assert not app.errors, app.errors
    finally:
        app.close()


@pytest.mark.slow
def test_a_card_lands_on_top_even_when_he_was_left_screens_behind(browser):
    """The owner (2026-10-29, after a version that held back for a reader
    "away from the end": "it doesn't work"). With Auto scroll off the thread
    never follows the work, so when the card lands he is screens behind it.
    It must land with its top at the top anyway."""
    app, sid, _cwd = _open_with_reply(browser)
    try:
        app.eval("() => { const l = document.getElementById('live-log'); l.dispatchEvent(new WheelEvent('wheel')); l.scrollTop = 0; }")
        app.page.wait_for_timeout(300)
        assert app.eval("() => ThreadScroll.state()") == "READING"
        app.push("session_entry", {"session_id": sid, "index": 9,
                                   "entry": {"kind": "asst", "text": _tall_card()}})
        app.page.wait_for_function("!!document.querySelector('#live-log .lm')", timeout=5000)
        app.page.wait_for_timeout(400)
        top = app.eval(CARD_TOP)
        assert 0 <= top <= 60, f"card top sits {top}px from the top of the thread"
        assert not app.errors, app.errors
    finally:
        app.close()


@pytest.mark.slow
def test_a_card_waits_for_a_swipe_in_progress_then_lands_on_top(browser):
    app, sid, _cwd = _open_with_reply(browser)
    try:
        # A finger is moving when the card lands: keep it "driving" for 600ms.
        app.eval("""() => { const l = document.getElementById('live-log'); l.scrollTop = 0;
          l.dispatchEvent(new TouchEvent('touchmove'));
          let n = 0; const t = setInterval(() => { l.dispatchEvent(new TouchEvent('touchmove')); if (++n > 6) clearInterval(t); }, 100); }""")
        app.push("session_entry", {"session_id": sid, "index": 9,
                                   "entry": {"kind": "asst", "text": _tall_card()}})
        app.page.wait_for_timeout(250)
        assert app.eval("() => document.getElementById('live-log').scrollTop") == 0, "jumped mid-swipe"
        app.page.wait_for_timeout(1200)
        top = app.eval(CARD_TOP)
        assert 0 <= top <= 60, f"after the swipe the card top sits {top}px from the top"
        assert not app.errors, app.errors
    finally:
        app.close()


@pytest.mark.slow
def test_jump_to_latest_from_inside_a_card_goes_up_to_its_top(browser):
    """Reading partway down the final card, "Jump to latest" used to treat the
    tap as a second tap and go to the card's last line. It now goes back up to
    the card's title; only a tap from the title goes on to the bottom."""
    from tests._fake_app import CWD, FakeApp, fake_sessions, init_script
    s = fake_sessions(1)
    sid = s[0]["id"]
    app = FakeApp(browser, s, init=init_script("sessions", "grid"),
                  viewport={"width": 390, "height": 760},
                  extra_routes={"/api/session/": lambda r: (200, {"id": sid, "display_title": "T", "messages": []})})
    try:
        app.push("session_state", {"session_id": sid, "state": "idle", "cwd": CWD})
        app.eval(f"() => openInGUI('{sid}')")
        app.page.wait_for_function(f"typeof liveSessionId !== 'undefined' && liveSessionId === '{sid}'", timeout=10000)
        app.page.wait_for_timeout(400)
        app.push("session_log", {"session_id": sid, "total": 3, "offset": 0, "has_more": False, "entries": [
            {"kind": "user", "text": "build all three fixes"},
            {"kind": "asst", "text": "Earlier reply. " * 40},
            {"kind": "asst", "text": _tall_card()}]})
        app.page.wait_for_function("!!document.querySelector('#live-log .lm')", timeout=5000)
        app.page.wait_for_timeout(300)
        # Read partway down the card.
        app.eval("() => { const l = document.getElementById('live-log'); l.dispatchEvent(new WheelEvent('wheel')); l.scrollTop += 200; }")
        app.page.wait_for_timeout(200)
        assert app.eval(CARD_TOP) < -150
        app.eval("() => ThreadScroll.catchUp()")
        app.page.wait_for_timeout(200)
        top = app.eval(CARD_TOP)
        # Just under the pinned "you asked" pill (40px), so its Done line shows.
        assert 0 <= top <= 60, f"jump left the card top {top}px from the top of the thread"
        # From the title, the next jump goes on to the bottom (never stuck).
        app.eval("() => ThreadScroll.catchUp()")
        app.page.wait_for_timeout(200)
        assert app.eval("() => { const l = document.getElementById('live-log'); return l.scrollHeight - l.clientHeight - l.scrollTop; }") <= 2
        assert not app.errors, app.errors
    finally:
        app.close()
