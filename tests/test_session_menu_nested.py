"""The session right-click menu opens short, with two lists one level down.

The real index.html with every real script in headless Chromium; only the
network is faked. On a computer the lists open beside their row on hover; on a
phone the bottom sheet opens a list in place, with Back. Every action the menu
offered before is still reachable.
"""

from __future__ import annotations

import pytest

from tests._browser import shared_browser_scope
from tests._fake_app import FakeApp, fake_sessions, init_script

EVERY_ACTION = {"rename", "pin", "duplicate", "link-workflow", "create-workflow", "add-compose",
                "spawn-subsession", "autoname", "continue", "move-project", "save-template",
                "terminal", "delete"}


@pytest.fixture(scope="module")
def browser():
    with shared_browser_scope() as b:
        yield b


def _open(app, sid):
    app.page.evaluate("sid => sessionContextMenu({preventDefault(){}, stopPropagation(){},"
                      " clientX: 200, clientY: 200}, sid)", sid)


def test_the_menu_opens_short_and_lists_open_beside_their_row(browser):
    rows = fake_sessions(2)
    app = FakeApp(browser, rows)
    _open(app, rows[0]["id"])
    menu = app.page.locator(".session-ctx-menu")
    top = menu.locator(":scope > .ws-ctx-item")
    assert top.count() <= 11
    shown = menu.inner_text()
    assert "Workflow & composition" in shown and "More" in shown
    assert "Save as Template" not in shown          # one level down until opened
    menu.locator(".vn-ctx-has-sub", has_text="More").hover()
    assert "Save as Template" in menu.inner_text()
    fly = menu.locator(".vn-ctx-has-sub.open > .vn-ctx-flyout")
    box, outer = fly.bounding_box(), menu.bounding_box()
    assert box["x"] >= outer["x"] + outer["width"] - 6   # beside the menu, not over it
    menu.locator(".vn-ctx-has-sub", has_text="Workflow").hover()
    assert "Save as Template" not in menu.inner_text()   # one list at a time
    assert "Spawn Subsession" in menu.inner_text()
    html = menu.inner_html()
    for action in EVERY_ACTION:
        assert "'" + action + "'" in html, action
    assert not app.errors


def test_icons_line_up_and_the_status_list_ticks_the_current_one(browser):
    rows = fake_sessions(2)
    app = FakeApp(browser, rows, init=init_script() + ";localStorage.setItem('vnSmartTermination', 'on');")
    app.page.wait_for_function("!!window.SessionVerb")
    app.page.evaluate("sid => SessionVerb.setHold && SessionVerb.applyHold"
                      " ? SessionVerb.applyHold(sid, 'standby') : null", rows[0]["id"])
    _open(app, rows[0]["id"])
    menu = app.page.locator(".session-ctx-menu")
    assert menu.locator(".vn-ctx-head").inner_text() == "Status"
    sts = menu.locator(".vn-ctx-st")
    assert sts.all_inner_texts() == ["Needs you", "Standby", "Done"]
    # Each status row draws in its own colour.
    colours = menu.evaluate("m => [...m.querySelectorAll('.vn-ctx-st > svg')].map(s => getComputedStyle(s).color)")
    assert len(set(colours)) == 3
    # Exactly one is ticked, and ticking it does nothing.
    assert menu.locator(".vn-ctx-st.cur").all_inner_texts() == ["Standby"]
    # Every icon's centre sits on its label's centre.
    off = menu.evaluate("""m => Math.max(...[...m.querySelectorAll(':scope > .ws-ctx-item')].map(r => {
        const i = r.querySelector('svg').getBoundingClientRect(), l = r.querySelector('.vn-ctx-lbl').getBoundingClientRect();
        return Math.abs((i.top + i.height / 2) - (l.top + l.height / 2)); }))""")
    assert off <= 1
    assert not app.errors


def test_the_phone_sheet_opens_a_list_in_place_with_back(browser):
    rows = fake_sessions(2)
    app = FakeApp(browser, rows, viewport={"width": 390, "height": 844})
    _open(app, rows[0]["id"])
    sheet = app.page.locator("#mobile-sheet")
    sheet.locator(".sheet-item", has_text="More").click()
    assert "Save as Template" in sheet.inner_text()
    assert "Rename" not in sheet.inner_text()
    sheet.locator(".sheet-item", has_text="Back").click()
    assert "Rename" in sheet.inner_text()
    assert "Save as Template" not in sheet.inner_text()
    assert not app.errors
