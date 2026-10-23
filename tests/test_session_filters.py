"""Finding one session among many: chips, title filter, windowing, naming.

Added 2026-10-23.  Against the real page (``tests/_fake_app.py``) with 300
fake sessions:
  * the title filter (#search) narrows the cards after its 100ms debounce,
    matching title, custom title and first message;
  * state chips (OR'd) and Unread / Last 7 days (AND'd) narrow them, and the
    chips persist per project in localStorage 'vnSessionFilters';
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


@pytest.mark.slow
def test_title_filter_and_chips(browser):
    app = FakeApp(browser, _sessions())
    try:
        assert app.eval("() => !document.getElementById('session-filter-bar').hidden")
        assert app.eval("() => document.getElementById('search').type") == "search"
        # Title filter (debounced): custom title, and first message.
        app.page.fill("#search", "quarterly")
        app.page.wait_for_function(f"({CARDS})() === 1", timeout=3000)
        app.page.fill("#search", "zebra")
        app.page.wait_for_function(f"({CARDS})() === 6", timeout=3000)
        app.page.fill("#search", "")
        app.page.wait_for_function(f"({CARDS})() === 150", timeout=3000)

        # State: 10 working, 5 waiting; everything else sleeping.
        sessions = app.sessions
        app.push_many("session_state",
                      [{"session_id": s["id"], "state": "working", "cwd": CWD} for s in sessions[:10]]
                      + [{"session_id": s["id"], "state": "waiting", "cwd": CWD} for s in sessions[10:15]])
        app.page.wait_for_timeout(300)
        _click(app, ".session-chip[data-filter='working']")
        assert app.eval(CARDS) == 10
        assert app.eval("() => document.querySelector(\".session-chip[data-filter='working']\").getAttribute('aria-pressed')") == "true"
        _click(app, ".session-chip[data-filter='needs']")          # OR
        assert app.eval(CARDS) == 15
        # A working session going idle leaves the filtered list (debounced rebuild).
        app.push("session_state", {"session_id": sessions[0]["id"], "state": "idle", "cwd": CWD})
        app.page.wait_for_function(f"({CARDS})() === 14", timeout=3000)
        _click(app, ".session-chip[data-filter='working']")
        _click(app, ".session-chip[data-filter='needs']")
        assert app.eval(CARDS) == 150

        # Last 7 days (AND with nothing else): 250 recent -> windowed to 150; with Sleeping too.
        _click(app, ".session-chip[data-filter='recent']")
        _click(app, ".session-chip[data-filter='sleeping']")
        stored = json.loads(app.eval("() => localStorage.getItem('vnSessionFilters')"))
        assert sorted(stored[PROJECT]) == ["recent", "sleeping"]
        app.eval("() => { const g = document.getElementById('workforce-grid'); g.scrollTop = g.scrollHeight; }")
        app.page.wait_for_timeout(300)
        # 250 recent, minus 14 working/waiting/idle (sessions 0..14, 0 now idle) = 235.
        assert app.eval(CARDS) == 235

        # Persisted per project: a reload restores the chips.
        app.page.reload(wait_until="domcontentloaded")
        app.page.wait_for_function(f"({CARDS})() > 0", timeout=20000)
        assert app.eval("() => [...document.querySelectorAll('.session-chip.active')].map(b => b.dataset.filter).sort()") == ["recent", "sleeping"]
        assert not app.errors, app.errors
    finally:
        app.close()


@pytest.mark.slow
def test_list_mode_filters_too(browser):
    app = FakeApp(browser, _sessions(), init=init_script("sessions", "list"))
    try:
        assert app.eval(ROWS) == 150
        app.page.fill("#search", "quarterly")
        app.page.wait_for_function(f"({ROWS})() === 1", timeout=3000)
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
