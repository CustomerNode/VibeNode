"""Opening a session that ends on a tall end-of-turn card lands on the card's
TOP, not its bottom (2026-10-29, the owner on a phone: "instead of scrolling
to bottom of card it should scroll to top of card").

The thread top-aligned the card, but ThreadScroll's "at the bottom" flag is
only updated by its sentinel observer a frame later. The text box redraw that
follows a session open or a turn ending calls ``ThreadScroll.holdBottom()``,
which read the stale flag and snapped the view to the bottom of the card.
``_syncAtBottom()`` in ``thread-scroll.js`` re-reads it from geometry right
after each scroll the thread makes.

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
