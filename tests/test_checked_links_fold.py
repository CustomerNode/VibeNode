"""Links under Checked fold behind one quiet line.

As full rows with a tile and a View button they read as the reader's next
step, as loud as Try it.  The real last-card.js and last-card.css in headless Chromium.
"""
import pathlib

import pytest

sync_api = pytest.importorskip("playwright.sync_api")
ROOT = pathlib.Path(__file__).resolve().parents[1]

CARD = {"type": "done", "title": "Dark mode switch", "path": ["Settings"],
        "headline": "The dark mode switch remembers your choice", "context": "x",
        "checked": "Reloaded; it stayed dark",
        "verified": [{"check": "Reload", "result": "Stayed dark", "ok": True}],
        "links": [{"label": "Test results", "kind": "link", "url": "/static/a.html"},
                  {"label": "Shot", "kind": "image", "url": "/static/a.png"}],
        "validate": {"how": "Reload the page", "url": "/static/b.html"},
        "needs": {"kind": "none"}}


@pytest.fixture(scope="module")
def page():
    with sync_api.sync_playwright() as p:
        try:
            b = p.chromium.launch()
        except Exception as e:  # no browser installed
            pytest.skip(f"no Chromium: {e}")
        pg = b.new_page()
        pg.set_content("<html><head><style>" + (ROOT / "src/web/static/css/last-card.css").read_text(encoding="utf-8")
                       + "</style></head><body><div id=h></div></body></html>")
        pg.add_script_tag(content=(ROOT / "src/web/static/js/last-card.js").read_text(encoding="utf-8"))
        pg.evaluate("c => { document.getElementById('h').innerHTML = VNLastCard.render(c); }", CARD)
        yield pg
        b.close()


def test_checked_links_start_folded_behind_one_line(page):
    assert page.inner_text(".lm-checked .lm-ckfold").strip().endswith("1 thing it looked at")
    assert not page.is_visible(".lm-checked .lm-ckrows")
    assert page.locator(".lm-checked .lm-row").count() == 1          # the picture is not in Checked


def test_the_line_opens_and_closes_the_list(page):
    page.click(".lm-checked .lm-ckfold")
    assert page.is_visible(".lm-checked .lm-ckrows .lm-row")
    assert page.get_attribute(".lm-checked .lm-ckfold", "aria-expanded") == "true"
    assert not page.is_visible(".lm-checked .lm-ckrows .lm-row .vb")  # no View button look
    page.click(".lm-checked .lm-ckfold")
    assert not page.is_visible(".lm-checked .lm-ckrows")


def test_try_it_keeps_its_full_row(page):
    assert page.locator(".lm-ckfold").count() == 1
    assert "Reload the page" in page.inner_text(".lm")
