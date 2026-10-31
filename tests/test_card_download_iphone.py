"""A summary card's Download button saves the file in the iPhone home-screen app.

In the home-screen app a plain download replaces the app with iOS's file
viewer, and the separate browser view that replaced it did not save files
either (2026-10-31: "still doesn't work").  The share sheet's "Save to Files"
works there, but iOS opens it only inside the tap, never after a fetch.  So
the first tap fetches the file and the button reads "Tap again to save"; the
second opens the sheet with the file in hand.

Runs the real ``last-card.js`` in headless Chromium with the home-screen flag
and the share sheet stubbed; everything is served by a fake origin.
"""
import json
from pathlib import Path

import pytest

from tests._browser import shared_browser_scope

ROOT = Path(__file__).resolve().parents[1]
LAST_CARD = (ROOT / "static" / "js" / "last-card.js").read_text(encoding="utf-8")
ZIP = b"PK\x03\x04" + b"fictional archive bytes" * 50
ORIGIN = "http://vn.test"

CARD = {"type": "deliverable", "title": "Widget export", "headline": "The widget export is ready.",
        "context": "x", "answer": "a", "full_answer": "f", "what_was_done": "w", "checked": "c",
        "deliverable": {"kind": "download", "title": "Widget export",
                        "files": [{"label": "The export", "url": "/static/scratch/widget-export.zip", "size": "1 KB"}]},
        "needs": {"kind": "none", "why": "y", "next": "n"}}

PAGE = """<!doctype html><html><body><div id="log"></div>
<script>
window.__shared = [];
Object.defineProperty(navigator, 'standalone', {value: %(standalone)s, configurable: true});
navigator.canShare = %(can_share)s;
navigator.share = (d) => { window.__shared.push({name: d.files[0].name, size: d.files[0].size, type: d.files[0].type});
                          return Promise.resolve(); };
window.open = (u) => { (window.__opened = window.__opened || []).push(u); return null; };
</script>
<script>%(js)s</script>
<script>document.getElementById('log').innerHTML = VNLastCard.render(%(card)s, {});</script>
</body></html>"""


@pytest.fixture(scope="module")
def browser():
    with shared_browser_scope() as b:
        yield b


def _open(browser, *, standalone=True, can_share="(d) => true"):
    page = browser.new_context().new_page()
    html = PAGE % {"standalone": "true" if standalone else "false", "can_share": can_share,
                   "js": LAST_CARD.replace("</script>", "<\\/script>"), "card": json.dumps(CARD)}

    def handle(route):
        if route.request.url.endswith(".zip"):
            route.fulfill(status=200, body=ZIP, headers={"Content-Type": "application/zip"})
        else:
            route.fulfill(status=200, body=html, headers={"Content-Type": "text/html"})
    page.route(f"{ORIGIN}/**", handle)
    page.goto(f"{ORIGIN}/", wait_until="load")
    return page


def _label(page):
    return page.locator("a.lm-dlfile .vb").inner_text().strip()


def test_first_tap_gets_the_file_ready_and_the_second_opens_the_share_sheet(browser):
    page = _open(browser)
    btn = page.locator("a.lm-dlfile")
    btn.click()
    page.wait_for_function("document.querySelector('a.lm-dlfile').dataset.dlState === 'ready'")
    assert _label(page) == "Tap again to save"
    assert page.evaluate("window.__shared.length") == 0      # nothing yet: iOS needs the tap itself
    btn.click()
    page.wait_for_function("window.__shared.length === 1")
    shared = page.evaluate("window.__shared[0]")
    assert shared == {"name": "widget-export.zip", "size": len(ZIP), "type": "application/zip"}
    assert page.evaluate("window.__opened || []") == []      # the app was never replaced
    page.wait_for_function("document.querySelector('a.lm-dlfile .vb').innerText.trim() === 'Saved'")


def test_without_the_share_sheet_it_opens_a_separate_view(browser):
    page = _open(browser, can_share="undefined")
    page.locator("a.lm-dlfile").click()
    assert page.evaluate("window.__opened") == [f"{ORIGIN}/static/scratch/widget-export.zip"]


def test_a_file_the_share_sheet_refuses_falls_back_to_a_separate_view(browser):
    page = _open(browser, can_share="(d) => !(d && d.files)")
    page.locator("a.lm-dlfile").click()
    page.wait_for_function("(window.__opened || []).length === 1")
    assert page.evaluate("window.__shared.length") == 0 and _label(page) == "Download"


def test_outside_the_home_screen_app_it_stays_a_plain_download(browser):
    page = _open(browser, standalone=False)
    with page.expect_download() as dl:
        page.locator("a.lm-dlfile").click()
    assert dl.value.suggested_filename == "widget-export.zip"
    assert page.evaluate("window.__shared.length") == 0
