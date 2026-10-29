"""A summary card that cannot be drawn never hides the rest of the thread
(2026-10-29, the owner on two sessions, Monitor issue and Fix terminal
popups: "why is this session giving a response that is not in a card").

Both threads stopped drawing right after the second "Invalid termination
response" box. The card that passed on the third try had "verified" written
as a sentence instead of a list (the checks stop polishing after two
send-backs, so it was let through), ``render()`` in ``last-card.js`` called
``.map`` on the string, and the exception escaped into the thread render:
the accepted card and every message after it, his own included, vanished.

The real ``index.html`` in headless Chromium; only the network is faked.
"""

from __future__ import annotations

import json

import pytest

from tests._browser import shared_browser_scope


@pytest.fixture(scope="module")
def browser():
    with shared_browser_scope() as b:
        yield b


def _card(**over) -> str:
    card = {
        "type": "done", "path": ["Surface", "Displays"], "title": "External monitors",
        "headline": "The monitor problem is fixed, and all three screens are working.",
        "context": "Two external monitors went dark.",
        # The shape that broke the thread: a sentence, not a list of checks.
        "verified": "Just now, Windows listed three screens, all three on.",
        "compare": {"before": "One screen lit", "after": "All three lit"},
        "validate": {"how": "Look at your desk.", "url": "ms-settings:display"},
        "needs": {"kind": "none", "why": "The restart cleared it.", "next": "Nothing."},
        "asks": [{"ask": "Where are we", "state": "answered"}],
        "zoom": {"big": "External monitors working", "task": "Exit Xbox mode", "now": "Checked all three"},
        "links": "not a list either",
        "explained": " ".join(["This explains the card in more words for someone who is lost."] * 6),
    }
    card.update(over)
    return "```handoff\n" + json.dumps(card) + "\n```"


@pytest.mark.slow
def test_a_card_with_a_sentence_for_a_list_draws_and_the_thread_goes_on(browser):
    from tests._fake_app import CWD, FakeApp, fake_sessions, init_script
    s = fake_sessions(1)
    sid = s[0]["id"]
    app = FakeApp(browser, s, init=init_script("sessions", "grid"),
                  viewport={"width": 1200, "height": 900},
                  extra_routes={"/api/session/": lambda r: (200, {"id": sid, "display_title": "T", "messages": []})})
    try:
        app.push("session_state", {"session_id": sid, "state": "idle", "cwd": CWD})
        app.eval(f"() => openInGUI('{sid}')")
        app.page.wait_for_function(f"typeof liveSessionId !== 'undefined' && liveSessionId === '{sid}'", timeout=10000)
        app.page.wait_for_timeout(300)
        app.push("session_log", {"session_id": sid, "total": 6, "offset": 0, "has_more": False, "entries": [
            {"kind": "user", "text": "Where are we"},
            {"kind": "asst", "text": "Plain text the check sent back."},
            {"kind": "sent_back", "name": "Incomplete response", "desc": "Missing parts.", "text": "Format: ..."},
            {"kind": "asst", "text": _card()},
            {"kind": "user", "text": "Why does the reply not follow a card"},
            {"kind": "asst", "text": _card(title="Later answer", headline="The later answer shows too.",
                                           verified=[{"check": "Looked", "result": "fine", "ok": True}],
                                           links=[])}]})
        app.page.wait_for_function("document.querySelectorAll('#live-log .lm').length >= 2", timeout=5000)
        got = app.eval("""() => {
          const log = document.getElementById('live-log');
          return { cards: [...log.querySelectorAll('.lm .lm-title')].map(h => h.textContent.trim()),
                   text: log.innerText, all: log.textContent,
                   code: !!log.querySelector('.vn-card-ph'), plain: log.querySelectorAll('.lm-plain').length };
        }""")
        assert got["cards"] == ["External monitors", "Later answer"], got["cards"]
        assert "Why does the reply not follow a card" in got["text"]
        assert "Just now, Windows listed three screens" in got["all"], got["all"][:3000]  # in the folded More band
        assert not got["code"]
        assert got["plain"] == 0, "drawn as the bare fallback, not the full card"
    finally:
        app.close()
