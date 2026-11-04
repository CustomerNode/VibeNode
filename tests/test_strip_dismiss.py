"""Left waiting gets a Details fold, and Left waiting and Also found each get
a cross that hides them for the session (an answered strip can be
dismissed).  The real last-card.js in headless Chromium."""

from pathlib import Path

import pytest

sync_api = pytest.importorskip("playwright.sync_api")
ROOT = Path(__file__).resolve().parents[1]

CARD = {"type": "done", "title": "Night theme", "headline": "The night theme is built.",
        "status": {"done": ["Built"]}, "links": [{"label": "Open", "kind": "link", "url": "/"}],
        "needs": {"kind": "none", "why": "Nothing runs.", "next": "Nothing else."},
        "back_to": {"label": "Toolbar layout", "status": "Answered by your own design",
                    "reply": "Show me the toolbar choices again", "detail": "It waited on the toolbar layout."},
        "finding": {"what": "A test leaves a file behind", "risk": "Clutter", "recommend": "Clean it up"},
        "zoom": {"big": "Night theme", "task": "Add it", "now": "Built"}}


@pytest.fixture()
def page():
    with sync_api.sync_playwright() as p:
        try:
            b = p.chromium.launch()
        except Exception as e:  # no browser installed
            pytest.skip(f"no Chromium: {e}")
        pg = b.new_page()
        # A real origin: the hidden strips are remembered in localStorage.
        html = ('<html><head><style>' + (ROOT / "src/web/static/css/last-card.css").read_text(encoding="utf-8") +
                '</style></head><body><div id="live-log"><div class="msg assistant"><div id="h"></div></div></div></body></html>')
        pg.route("http://vn.test/", lambda r: r.fulfill(status=200, content_type="text/html", body=html))
        pg.goto("http://vn.test/")
        pg.evaluate("() => { window.liveSessionId = 's1'; window._liveSubmitDirect = () => true; }")
        pg.add_script_tag(content=(ROOT / "src/web/static/js/last-card.js").read_text(encoding="utf-8"))

        def draw():
            pg.evaluate("c => { document.getElementById('h').innerHTML = VNLastCard.render(c, {}); VNLastCard.syncDone(); }", CARD)
        pg.draw = draw
        draw()
        yield pg
        b.close()


def test_details_opens_the_left_waiting_detail(page):
    body = page.locator(".lm-backto .lm-strip-body")
    assert body.is_hidden()
    page.click(".lm-strip-det")
    assert body.is_visible() and "It waited on the toolbar layout." in body.inner_text()


def test_the_cross_hides_each_strip_for_the_session_and_undo_brings_it_back(page):
    page.click(".lm-backto .lm-strip-x")
    page.click(".lm-findband .lm-strip-x")
    assert page.locator(".lm-backto").is_hidden() and page.locator(".lm-findband").is_hidden()
    # A later card of the same session carrying the same strips stays clean.
    page.draw()
    assert page.locator(".lm-backto").is_hidden() and page.locator(".lm-findband").is_hidden()
    # Nothing was sent to the session; Undo restores it.
    page.draw()
    page.evaluate("() => { const b = document.querySelector('.lm-backto'); b.hidden = false; }")
    page.click(".lm-backto .lm-strip-x")
    page.click(".lm-strip-hid .u")
    assert page.locator(".lm-backto").is_visible()


def test_another_session_is_not_affected(page):
    page.click(".lm-backto .lm-strip-x")
    page.evaluate("() => { window.liveSessionId = 's2'; }")
    page.draw()
    assert page.locator(".lm-backto").is_visible() and page.locator(".lm-findband").is_visible()
