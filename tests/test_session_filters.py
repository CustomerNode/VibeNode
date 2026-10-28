"""Finding one session among many: the funnel filters, windowing, naming.

Added 2026-10-23.  Against the real page (``tests/_fake_app.py``) with 300
fake sessions:
  * the sidebar has ONE search control, the "Search…" button: no second
    title-filter box under it (removed 2026-07-13, re-added 2026-10-23,
    removed again 2026-10-26 at the owner's request);
  * the status/date filters live behind the funnel button beside the •••
    menu (the owner's pick, 2026-10-26): state filters (OR'd) and Unread /
    Last 7 days (AND'd) narrow the cards, a click leaves the funnel menu open,
    the funnel lights up with a dot while any is on, "Show all sessions"
    clears them, the ••• menu no longer holds them, the funnel shows only
    where the filters apply, and they persist per project in localStorage
    'vnSessionFilters';
  * the grid and list mount the first 150 rows, and scrolling the sentinel
    into view mounts the next 150;
  * "Name N untitled sessions" appears at >= 10 untitled, POSTs the run,
    shows progress from `naming_progress` in the shared float stack, and hides
    itself when the count endpoint is missing (404).
"""

from __future__ import annotations

import json
import time

import pytest

from tests._browser import shared_browser_scope
from tests._fake_app import CWD, PROJECT, FakeApp, fake_sessions, init_script

XDIST_SPLITTABLE = True

N = 300
CARDS = "() => document.querySelectorAll('#workforce-grid .wf-card').length"
ROWS = "() => document.querySelectorAll('#session-list .session-item').length"


@pytest.fixture(scope="module")
def browser():
    with shared_browser_scope() as b:
        yield b


def _sessions():
    now = int(time.time())
    rows = fake_sessions(N, base_ts=now)          # 1 minute apart: all within 7 days
    for i, s in enumerate(rows):
        if i >= 250:                                # 50 old ones
            s["effective_ts"] = s["last_activity_ts"] = s["sort_ts"] = now - 30 * 86400
        if i % 50 == 7:
            s["preview"] = "please fix the zebra stripes"
    rows[42]["display_title"] = rows[42]["custom_title"] = "Quarterly Report Draft"
    return rows


def _click(app, selector):
    """DOM click: runs the real onclick handler.  The boot splash (#vn-splash)
    never lifts in this harness and would block a pointer click."""
    app.eval("(s) => document.querySelector(s).click()", selector)


def _count_api(status, body):
    return {"/api/sessions/untitled-count": lambda req: (status, body)}


@pytest.mark.slow
def test_windowing_mounts_150_then_more_on_scroll(browser):
    app = FakeApp(browser, _sessions())
    try:
        assert app.eval(CARDS) == 150
        assert app.eval("() => !!document.querySelector('#workforce-grid .session-window-sentinel')")
        app.eval("() => { const g = document.getElementById('workforce-grid'); g.scrollTop = g.scrollHeight; }")
        app.page.wait_for_function(f"({CARDS})() === 300", timeout=5000)
        assert not app.eval("() => !!document.querySelector('#workforce-grid .session-window-sentinel')")
        assert not app.errors, app.errors
    finally:
        app.close()


def _opt(name):
    return f".sidebar-filter-opt[data-filter='{name}']"


HAS_DOT = "() => document.getElementById('sidebar-filter-btn').classList.contains('has-filter')"
FILTER_OPEN = "() => document.getElementById('sidebar-filter-dropdown').classList.contains('open')"
MENU_OPEN = "() => document.getElementById('sidebar-menu-dropdown').classList.contains('open')"
FUNNEL_SHOWN = "() => !document.getElementById('sidebar-filter-wrap').hidden"


@pytest.mark.slow
def test_one_search_control_in_the_sidebar(browser):
    """The sidebar's only search is the 'Search…' button.  A second box under
    it was added twice and removed twice; this keeps it out."""
    app = FakeApp(browser, _sessions())
    try:
        assert app.eval("() => document.querySelector('.sidebar-search-btn span').textContent") == "Search…"
        assert app.eval("() => document.getElementById('search').type") == "hidden"
        assert app.eval("() => !document.getElementById('session-filter-bar')")
        typable = app.eval("""() => [...document.querySelectorAll(
            '.sidebar input:not([type=hidden]):not([type=checkbox]):not([type=radio])')]
            .filter(el => el.getClientRects().length).map(el => el.id || el.className)""")
        assert typable == [], typable
        assert not app.errors, app.errors
    finally:
        app.close()


@pytest.mark.slow
def test_filters_live_behind_the_funnel(browser):
    app = FakeApp(browser, _sessions())
    try:
        assert app.eval(FUNNEL_SHOWN)
        assert not app.eval(HAS_DOT)
        # The ••• menu no longer holds them.
        assert app.eval("() => document.querySelectorAll('#sidebar-menu-dropdown .sidebar-filter-opt').length") == 0

        # State: 10 working, 5 waiting; everything else sleeping.
        sessions = app.sessions
        app.push_many("session_state",
                      [{"session_id": s["id"], "state": "working", "cwd": CWD} for s in sessions[:10]]
                      + [{"session_id": s["id"], "state": "waiting", "cwd": CWD} for s in sessions[10:15]])
        app.page.wait_for_timeout(300)
        _click(app, "#sidebar-filter-btn")
        assert app.eval(FILTER_OPEN)
        assert app.eval("() => document.getElementById('sidebar-filter-clear').hidden")
        _click(app, _opt("working"))
        assert app.eval(CARDS) == 10
        assert app.eval(FILTER_OPEN), "ticking a filter must leave the menu open"
        assert app.eval(HAS_DOT)
        assert not app.eval("() => document.getElementById('sidebar-menu-btn').classList.contains('has-filter')")
        assert "Working" in app.eval("() => document.getElementById('sidebar-filter-btn').title")
        assert app.eval(f"() => document.querySelector(\"{_opt('working')}\").getAttribute('aria-pressed')") == "true"
        _click(app, _opt("needs"))          # OR
        assert app.eval(CARDS) == 15
        # A working session going to sleep (Done) leaves the filtered list (debounced
        # rebuild).  Going idle would not: a stopped session is Review, which is
        # "Needs you" (session-verb.js, 2026-10-27).
        app.push("session_state", {"session_id": sessions[0]["id"], "state": "stopped", "cwd": CWD})
        app.page.wait_for_function(f"({CARDS})() === 14", timeout=3000)
        # "Show all sessions" clears every filter and closes the menu.
        assert not app.eval("() => document.getElementById('sidebar-filter-clear').hidden")
        _click(app, "#sidebar-filter-clear button")
        assert app.eval(CARDS) == 150
        assert not app.eval(HAS_DOT)
        assert not app.eval(FILTER_OPEN)
        assert app.eval("() => document.querySelectorAll('.sidebar-filter-opt.active').length") == 0

        # One menu open at a time.
        _click(app, "#sidebar-filter-btn")
        _click(app, "#sidebar-menu-btn")
        assert app.eval(MENU_OPEN) and not app.eval(FILTER_OPEN)
        _click(app, "#sidebar-filter-btn")
        assert app.eval(FILTER_OPEN) and not app.eval(MENU_OPEN)

        # A sort pick re-ticks the active filters (pickSort clears .active on every option).
        _click(app, _opt("sleeping"))
        _click(app, ".sidebar-sort-opt[data-sort='name'][data-asc='true']")
        assert app.eval(f"() => document.querySelector(\"{_opt('sleeping')}\").classList.contains('active')")
        _click(app, _opt("sleeping"))

        # Last 7 days (AND with nothing else): 250 recent -> windowed to 150; with Sleeping too.
        _click(app, _opt("recent"))
        _click(app, _opt("sleeping"))
        stored = json.loads(app.eval("() => localStorage.getItem('vnSessionFilters')"))
        assert sorted(stored[PROJECT]) == ["recent", "sleeping"]
        app.eval("() => { const g = document.getElementById('workforce-grid'); g.scrollTop = g.scrollHeight; }")
        app.page.wait_for_timeout(300)
        # 250 recent, minus 14 working/waiting (sessions 1..14; 0 is asleep now) = 236.
        assert app.eval(CARDS) == 236

        # Persisted per project: a reload restores the chips.
        app.page.reload(wait_until="domcontentloaded")
        app.page.wait_for_function(f"({CARDS})() > 0", timeout=20000)
        assert app.eval("() => [...document.querySelectorAll('.sidebar-filter-opt.active')].map(b => b.dataset.filter).sort()") == ["recent", "sleeping"]
        assert app.eval(HAS_DOT)
        assert not app.errors, app.errors
    finally:
        app.close()


@pytest.mark.slow
def test_funnel_shows_only_where_filters_apply(browser):
    app = FakeApp(browser, _sessions())
    try:
        assert app.eval(FUNNEL_SHOWN)
        _click(app, "#sidebar-filter-btn")
        app.eval("() => setViewMode('workplace')")       # the Workforce view: no filters there
        app.page.wait_for_function("document.getElementById('sidebar-filter-wrap').hidden", timeout=5000)
        assert not app.eval(FILTER_OPEN), "a hidden funnel must not leave its menu open"
        assert not app.errors, app.errors
    finally:
        app.close()


@pytest.mark.slow
def test_list_mode_filters_too(browser):
    app = FakeApp(browser, _sessions(), init=init_script("sessions", "list"))
    try:
        assert app.eval(ROWS) == 150
        app.push_many("session_state",
                      [{"session_id": s["id"], "state": "working", "cwd": CWD} for s in app.sessions[:3]])
        app.page.wait_for_timeout(300)
        _click(app, _opt("working"))
        app.page.wait_for_function(f"({ROWS})() === 3", timeout=3000)
        assert not app.errors, app.errors
    finally:
        app.close()


@pytest.mark.slow
def test_name_untitled_button_and_progress(browser):
    posts = []

    def name_untitled(req):
        posts.append(req.url)
        return 202, {"ok": True, "started": True}

    routes = {**_count_api(200, {"project": PROJECT, "total": N, "untitled": 40, "nameable": 37}),
              "/api/sessions/name-untitled": name_untitled}
    app = FakeApp(browser, _sessions(), extra_routes=routes)
    try:
        app.page.wait_for_function("!document.getElementById('btn-name-untitled').hidden", timeout=5000)
        assert app.eval("() => document.getElementById('btn-name-untitled').textContent") == "Name 37 untitled sessions"
        _click(app, "#btn-name-untitled")
        app.page.wait_for_function("!!document.getElementById('vn-naming-toast')", timeout=3000)
        assert posts and "project=" + PROJECT in posts[0]
        app.push("naming_progress", {"project": PROJECT, "done": 5, "total": 37,
                                     "current_title": "Fix login", "named": 5, "finished": False})
        txt = app.eval("() => document.getElementById('vn-naming-toast').textContent")
        assert "5/37" in txt and "Fix login" in txt
        # Another project's progress is ignored.
        app.push("naming_progress", {"project": "C--other", "done": 9, "total": 9, "finished": True})
        assert "5/37" in app.eval("() => document.getElementById('vn-naming-toast').textContent")
        # It sits in the shared float stack: bottom written by _layoutFloats().
        app.page.wait_for_timeout(100)
        assert app.eval("() => document.getElementById('vn-naming-toast').style.bottom") != ""
        app.push("naming_progress", {"project": PROJECT, "done": 37, "total": 37, "named": 35,
                                     "failed": 2, "finished": True})
        assert app.eval("() => document.getElementById('vn-naming-toast').textContent").startswith("Named 35 sessions")
        assert not app.errors, app.errors
    finally:
        app.close()


@pytest.mark.slow
@pytest.mark.parametrize("status,body", [(404, {"error": "not found"}),
                                         (200, {"project": PROJECT, "total": N, "untitled": 9, "nameable": 9})])
def test_name_untitled_button_hidden(browser, status, body):
    app = FakeApp(browser, _sessions(), extra_routes=_count_api(status, body))
    try:
        app.page.wait_for_function(
            "() => window.__countSeen || performance.getEntriesByType('resource').some(e => e.name.includes('untitled-count'))",
            timeout=5000)
        app.page.wait_for_timeout(200)
        assert app.eval("() => document.getElementById('btn-name-untitled').hidden")
        assert not app.errors, app.errors
    finally:
        app.close()
