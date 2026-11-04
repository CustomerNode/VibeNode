"""Shift+click range selection in the session sidebar.

Ctrl/Cmd+click already toggled one session; Shift+click now selects every
session between the anchor (the last session plain- or Ctrl-clicked) and the
clicked one, in on-screen order, and Ctrl/Cmd+Shift adds the range to the
selection.  Modifier double-clicks never sleep a session.  Real page, real
scripts, headless Chromium (``tests/_fake_app.py``).
"""

from __future__ import annotations

import json
import time

import pytest

from tests._browser import shared_browser_scope
from tests._fake_app import FakeApp, fake_sessions, init_script

XDIST_SPLITTABLE = True

NO_SPLASH = "() => { for (const id of ['vn-splash', 'health-blocker']) { const s = document.getElementById(id); if (s) s.remove(); } }"
SELECTED = "() => [...multiSelectedIds]"
BADGE = "() => (document.getElementById('sidebar-multi-badge') || {}).textContent || ''"


@pytest.fixture(scope="module")
def browser():
    with shared_browser_scope() as b:
        yield b


def _app(browser, display_mode):
    rows = fake_sessions(8, base_ts=int(time.time()))
    app = FakeApp(browser, rows, init=init_script(display_mode=display_mode), clock=True)
    app.eval(NO_SPLASH)
    # Opening a session is not under test; record it instead.
    app.eval("() => { window.__opened = []; window.openInGUI = (id) => { window.__opened.push(id); return Promise.resolve(); }; }")
    return app, [r["id"] for r in rows]


@pytest.mark.parametrize("display_mode,sel", [("list", ".session-item"), ("grid", ".wf-card")])
def test_shift_click_selects_the_range_from_the_anchor(browser, display_mode, sel):
    app, ids = _app(browser, display_mode)
    try:
        row = lambda i: f"{sel}[data-sid='{ids[i]}']"
        app.page.click(row(1), modifiers=["Control"])          # anchor
        app.page.click(row(4), modifiers=["Shift"])
        assert sorted(app.eval(SELECTED)) == sorted(ids[1:5])
        assert "4 selected" in app.eval(BADGE)
        # The anchor stays: a Shift+click upward re-spans from it.
        app.page.click(row(0), modifiers=["Shift"])
        assert sorted(app.eval(SELECTED)) == sorted(ids[0:2])
        # Ctrl+Shift adds a second range to the selection.
        app.page.click(row(6), modifiers=["Control"])
        app.page.click(row(7), modifiers=["Control", "Shift"])
        assert sorted(app.eval(SELECTED)) == sorted(ids[0:2] + ids[6:8])
        # The highlight follows, and none of these clicks opened a session.
        hl = app.eval(f"() => [...document.querySelectorAll('{sel}.multi-selected')].map(r => r.dataset.sid)")
        assert sorted(hl) == sorted(ids[0:2] + ids[6:8])
        assert app.eval("() => window.__opened") == []
        assert not app.errors, app.errors
    finally:
        app.close()


def test_a_plain_click_sets_the_anchor(browser):
    app, ids = _app(browser, "list")
    try:
        app.page.click(f".session-item[data-sid='{ids[2]}'] .session-col-date")
        app.page.click(f".session-item[data-sid='{ids[5]}']", modifiers=["Shift"])
        assert sorted(app.eval(SELECTED)) == sorted(ids[2:6])
    finally:
        app.close()


def _hold_route(log):
    def handler(request):
        sid = request.url.split("/api/session-hold/")[1].split("?")[0]
        log.append((sid, json.loads(request.post_data or "{}").get("state")))
        return 200, {"ok": True}
    return {"/api/session-hold/": handler}


@pytest.mark.parametrize("item,state", [("Mark all done", "done"), ("Standby all", "standby")])
def test_bulk_menu_marks_every_selected_session_with_one_undo(browser, item, state):
    rows = fake_sessions(8, base_ts=int(time.time()))
    ids = [r["id"] for r in rows]
    posts: list = []
    app = FakeApp(browser, rows, init=init_script(display_mode="list"), extra_routes=_hold_route(posts), clock=True)
    try:
        app.eval(NO_SPLASH)
        app.eval("() => { window.openInGUI = () => Promise.resolve(); }")
        # One session awake (idle), the rest sleeping: both kinds are covered.
        app.eval("(id) => { guiOpenAdd(id); filterSessions(); }", ids[3])
        row = lambda i: f".session-item[data-sid='{ids[i]}']"
        app.page.click(row(2), modifiers=["Control"])
        app.page.click(row(4), modifiers=["Shift"])
        app.page.click(row(3), button="right")
        labels = app.eval("() => [...document.querySelectorAll('.sidebar-bulk-ctx-menu .ws-ctx-item')].map(e => e.textContent.trim())")
        assert "Mark all done" in labels and "Standby all" in labels, labels
        app.page.click(f".sidebar-bulk-ctx-menu .ws-ctx-item:has-text('{item}')")
        app.advance(300)
        assert sorted(posts) == sorted((i, state) for i in ids[2:5]), posts
        assert [app.eval("(id) => SessionVerb.holdOf(id)", i) for i in ids[2:5]] == [state] * 3
        # The awake one was slept the sidebar's way; the sleeping ones were not touched.
        closes = app.eval("() => window.__emits.filter(e => e[0] === 'close_session').map(e => e[1].session_id)")
        assert closes == [ids[3]], closes
        # Exactly one Undo toast for the batch, and it puts every verdict back.
        toasts = app.eval("() => [...document.querySelectorAll('.vn-undo-toast')].map(t => t.textContent)")
        assert len(toasts) == 1 and "3 sessions" in toasts[0], toasts
        assert app.eval(SELECTED) == []
        posts.clear()
        app.page.click(".vn-undo-toast button")
        app.advance(300)
        assert sorted(posts) == sorted((i, None) for i in ids[2:5]), posts
        assert [app.eval("(id) => SessionVerb.holdOf(id)", i) for i in ids[2:5]] == [None] * 3
        assert not app.errors, app.errors
    finally:
        app.close()


def test_modifier_double_click_never_sleeps_a_session(browser):
    app, ids = _app(browser, "list")
    try:
        app.eval("() => { window.__slept = 0; const SV = window.SessionVerb; if (SV) SV.setHold = () => { window.__slept++; }; window.liveSleep = () => { window.__slept++; }; }")
        target = f".session-item[data-sid='{ids[3]}']"
        app.page.dblclick(target, modifiers=["Shift"])
        app.page.dblclick(target, modifiers=["Control"])
        assert app.eval("() => window.__slept") == 0
        # Control: the probe does see a plain double-click.
        app.page.dblclick(f".session-item[data-sid='{ids[4]}']")
        assert app.eval("() => window.__slept") > 0
        assert not app.errors, app.errors
    finally:
        app.close()
