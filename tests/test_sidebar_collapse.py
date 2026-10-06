"""A hidden sessions panel must always be easy to bring back (desktop).

Since 2026-10-02 the desktop layout has no collapse arrow or expand button. The
divider between the panel and the main area does both jobs: drag it to resize,
drag it past the minimum to hide the panel, and when the panel is hidden the
divider becomes a strip at the left edge that reopens it.

On 2026-10-06 a hidden panel was reported as "the sessions panel is gone": the
strip was a faint 4px pill at 60% opacity, close to invisible in light theme. It
is now a 16px tinted strip with a chevron and a "Show sessions panel" tooltip.
The other tests pin every way in and out of the hidden state (click, double-
click, drag, reload) so a redesign of the divider cannot strand the panel.

The page is built from the real ``style.css`` plus the real divider block from
``toolbar.js`` and the real ``toggleSidebar()`` from ``app.js``, cut out of those
files by marker, so a regression in any of them fails here.
"""

from __future__ import annotations

from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
STATIC = ROOT / "static"

DIVIDER_MARKER = "/* ---- Sidebar resize / hide (desktop) ----"


def _braced_block(src: str, start: int) -> str:
    """Text from ``start`` through the brace that closes the first ``{`` after it."""
    depth, i = 0, src.index("{", start)
    while i < len(src):
        if src[i] == "{":
            depth += 1
        elif src[i] == "}":
            depth -= 1
            if depth == 0:
                return src[start : i + 1]
        i += 1
    raise AssertionError("unbalanced braces")


def _divider_js() -> str:
    """The divider IIFE from toolbar.js: marker comment through its ``})();``."""
    src = (STATIC / "js" / "toolbar.js").read_text(encoding="utf-8")
    assert DIVIDER_MARKER in src, "divider block marker is gone from toolbar.js"
    start = src.index(DIVIDER_MARKER)
    end = src.index("})();", start) + len("})();")
    return src[start:end]


def _toggle_sidebar_js() -> str:
    src = (STATIC / "js" / "app.js").read_text(encoding="utf-8")
    assert "function toggleSidebar()" in src, "toggleSidebar() is gone from app.js"
    return _braced_block(src, src.index("function toggleSidebar()"))


def _page_html(collapsed: bool) -> str:
    style = (STATIC / "style.css").read_text(encoding="utf-8")
    return f"""<!doctype html>
<html data-theme="light"><head><meta charset="utf-8"><style>{style}</style></head>
<body>
<button class="sidebar-expand-btn" id="btn-sidebar-expand">&gt;</button>
<div class="layout" style="height:100vh">
  <div class="sidebar{' collapsed' if collapsed else ''}"><div class="sidebar-header">Sessions</div></div>
  <div class="resize-handle" id="resize-handle"></div>
  <div class="main" id="main-panel"></div>
</div>
<script>
localStorage.setItem('sidebarCollapsed', {'"1"' if collapsed else '""'});
{_toggle_sidebar_js()}
{_divider_js()}
function state() {{
  const s = document.querySelector('.sidebar'), h = document.getElementById('resize-handle');
  return {{collapsed: s.classList.contains('collapsed'),
           width: s.getBoundingClientRect().width,
           handleWidth: h.getBoundingClientRect().width,
           title: h.title}};
}}
</script></body></html>"""


@pytest.fixture(scope="module")
def browser():
    pw = pytest.importorskip("playwright.sync_api", reason="playwright not installed")
    with pw.sync_playwright() as p:
        try:
            b = p.chromium.launch()
        except Exception as exc:  # browser binary not downloaded on this machine
            pytest.skip(f"chromium unavailable: {str(exc).splitlines()[0][:80]}")
        yield b
        b.close()


@pytest.fixture
def page(browser):
    def _open(collapsed: bool):
        # Served from a fake origin, not set_content(): toggleSidebar() writes
        # localStorage, which Chromium denies on about:blank.
        pg = browser.new_page(viewport={"width": 1440, "height": 900})
        html = _page_html(collapsed)
        pg.route("http://vibenode.test/", lambda r: r.fulfill(body=html, content_type="text/html"))
        pg.goto("http://vibenode.test/")
        return pg

    pages = []

    def factory(collapsed: bool):
        pg = _open(collapsed)
        pages.append(pg)
        return pg

    yield factory
    for pg in pages:
        pg.close()


@pytest.mark.slow
def test_hidden_strip_is_visible_and_says_what_it_does(page):
    """The hidden panel's strip must be a real, labelled target, not a sliver."""
    pg = page(collapsed=True)
    st = pg.evaluate("state()")
    assert st["collapsed"] and st["width"] == 0
    assert st["handleWidth"] >= 14, f"strip is {st['handleWidth']}px wide, too thin to find"
    assert "Show sessions panel" in st["title"], f"strip tooltip is {st['title']!r}"
    chevron = pg.evaluate(
        """() => { const cs = getComputedStyle(document.getElementById('resize-handle'), '::before');
                   return {opacity: parseFloat(cs.opacity), mask: cs.maskImage || cs.webkitMaskImage}; }"""
    )
    assert chevron["opacity"] == 1, "chevron on the hidden strip is faded"
    assert chevron["mask"] and chevron["mask"] != "none", "hidden strip lost its chevron"


@pytest.mark.slow
def test_single_click_on_hidden_strip_opens_panel(page):
    pg = page(collapsed=True)
    pg.click("#resize-handle")
    st = pg.evaluate("state()")
    assert not st["collapsed"] and st["width"] > 100
    assert "double-click to hide" in st["title"]


@pytest.mark.slow
def test_double_click_on_hidden_strip_leaves_panel_open(page):
    """Users double-click a divider out of habit; that must not leave it hidden."""
    pg = page(collapsed=True)
    pg.dblclick("#resize-handle")
    pg.wait_for_timeout(100)
    st = pg.evaluate("state()")
    assert not st["collapsed"], "double-clicking the hidden strip re-hid the panel"
    assert st["width"] > 100


@pytest.mark.slow
def test_double_click_on_open_divider_hides_panel(page):
    pg = page(collapsed=False)
    pg.dblclick("#resize-handle")
    st = pg.evaluate("state()")
    assert st["collapsed"], "double-click on the open divider no longer hides the panel"
    assert "Show sessions panel" in st["title"]


@pytest.mark.slow
def test_drag_shut_then_drag_back_open(page):
    pg = page(collapsed=False)
    box = pg.locator("#resize-handle").bounding_box()
    y = box["y"] + 200
    pg.mouse.move(box["x"] + box["width"] / 2, y)
    pg.mouse.down()
    pg.mouse.move(40, y, steps=10)
    pg.mouse.up()
    assert pg.evaluate("state()")["collapsed"], "dragging past the minimum no longer hides"

    pg.mouse.move(8, y)
    pg.mouse.down()
    pg.mouse.move(300, y, steps=10)
    pg.mouse.up()
    st = pg.evaluate("state()")
    assert not st["collapsed"] and st["width"] > 100, "dragging the strip out no longer reopens"


@pytest.mark.slow
def test_hidden_state_survives_reload_and_strip_still_reopens(page):
    """Collapse persists via localStorage; the strip must still work after a reload."""
    pg = page(collapsed=False)
    pg.dblclick("#resize-handle")
    assert pg.evaluate("localStorage.getItem('sidebarCollapsed')") == "1"
    pg.close()
    pg2 = page(collapsed=True)  # what app.js's restore-on-load produces
    pg2.click("#resize-handle")
    assert not pg2.evaluate("state()")["collapsed"]
