"""Holding on a chat message selects text; it never opens the session menu.

Chat messages and end-of-turn summaries carry ``data-sid`` so their buttons
answer the right session, and the press-and-hold menu in ``mobile.js``
matched any ``[data-sid]``: holding on a reply to select its text opened the
sidebar's session menu. The real ``mobile.js`` in headless Chromium.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from tests._browser import shared_browser_scope

ROOT = Path(__file__).resolve().parents[1]
MOBILE_JS = (ROOT / "src/web/static/js/mobile.js").read_text(encoding="utf-8")

PAGE = """<html><body>
<div class="session-item" data-sid="row-1" id="row">A session row</div>
<div class="conversation live-log" id="live-log">
  <div class="msg assistant" data-sid="s-1" id="msg"><div class="msg-body">Some reply text</div></div>
  <div class="msg assistant" data-sid="s-1"><div class="lm skim" data-sid="s-1"><p id="card">A summary</p></div></div>
</div>
<script>window.menus = []; function sessionContextMenu(e, sid) { window.menus.push(sid); }</script>
</body></html>"""


@pytest.fixture(scope="module")
def page():
    with shared_browser_scope() as b:
        pg = b.new_page()
        pg.set_content(PAGE)
        pg.add_script_tag(content=MOBILE_JS)
        yield pg


def _hold(pg, el_id, kind="touch"):
    pg.evaluate("window.menus = []")
    pg.evaluate("""([id, kind]) => {
        const el = document.getElementById(id); const r = el.getBoundingClientRect();
        const at = {bubbles: true, clientX: r.left + 2, clientY: r.top + 2, button: 0};
        el.dispatchEvent(new PointerEvent('pointerdown', {...at, pointerType: kind, pointerId: 7}));
        if (kind === 'mouse') el.dispatchEvent(new MouseEvent('mousedown', at));
    }""", [el_id, kind])
    pg.wait_for_timeout(650)
    pg.evaluate("document.dispatchEvent(new PointerEvent('pointerup', {bubbles: true, pointerId: 7}))")
    return pg.evaluate("window.menus")


@pytest.mark.parametrize("el_id", ["msg", "card"])
def test_a_mouse_hold_on_a_reply_or_summary_on_the_computer_opens_no_menu(page, el_id):
    assert _hold(page, el_id, kind="mouse") == []


def test_a_mouse_hold_on_a_session_row_opens_its_menu(page):
    assert _hold(page, "row", kind="mouse") == ["row-1"]


def test_holding_a_session_row_opens_its_menu(page):
    assert _hold(page, "row") == ["row-1"]


def test_holding_a_reply_opens_no_menu(page):
    assert _hold(page, "msg") == []


def test_holding_a_summary_opens_no_menu(page):
    assert _hold(page, "card") == []
