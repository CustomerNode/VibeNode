"""The homepage answers "what needs me" (added 2026-10-23).

Against the real page (``tests/_fake_app.py``: the real ``index.html``, every
real script and the real CSS in headless Chromium; only the network is faked):

  * the Sessions card lists "Needs you" (sessions waiting on a question or
    permission first, then finished sessions with unread output) and
    "Running" (working sessions), each from SessionStore and the live state
    maps, and re-renders on the same state pushes as the counts;
  * with nothing waiting or running it says "Nothing needs you. N sleeping.";
  * a row opens its session;
  * the strip under the project name shows Durable Jobs needing a person, an
    older-code Session Engine, and "Name N untitled sessions" at >= 10.
"""

from __future__ import annotations

import time

import pytest

from tests._browser import shared_browser_scope
from tests._fake_app import CWD, PROJECT, FakeApp, fake_sessions, init_script

XDIST_SPLITTABLE = True

HOME_READY = "document.querySelector('#homepage-container .homepage') !== null"


@pytest.fixture(scope="module")
def browser():
    with shared_browser_scope() as b:
        yield b


def _home(browser, sessions, *, extra="", routes=None):
    return FakeApp(browser, sessions, init=init_script(view_mode="homepage", extra=extra),
                   wait_for=HOME_READY, extra_routes=routes or {})


def _titles(app, list_id):
    return app.eval("(id) => Array.from(document.querySelectorAll('#' + id + ' .hpn-row .t')).map(e => e.textContent)",
                    list_id)


def _sessions(n=12):
    return fake_sessions(n, base_ts=int(time.time()))


@pytest.mark.slow
def test_needs_you_and_running_lists(browser):
    rows = _sessions()
    # Session 0003 has unread output from an earlier turn.
    extra = "localStorage.setItem('vn_unread_sessions', JSON.stringify(['%s']));" % rows[3]["id"]
    app = _home(browser, rows, extra=extra)
    try:
        app.push_many("session_state", [
            {"session_id": rows[0]["id"], "state": "working", "cwd": CWD},
            {"session_id": rows[1]["id"], "state": "waiting", "cwd": CWD},
            {"session_id": rows[2]["id"], "state": "working", "cwd": CWD},
            {"session_id": rows[3]["id"], "state": "idle", "cwd": CWD},
            {"session_id": rows[5]["id"], "state": "waiting", "cwd": CWD},
        ])
        app.page.wait_for_function("document.getElementById('hp-running-list') !== null", timeout=5000)
        needs = _titles(app, "hp-needs-list")
        running = _titles(app, "hp-running-list")
        # Questions first (newest first), then the unread finished one.
        assert needs == ["Session 0001", "Session 0005", "Session 0003"], needs
        assert sorted(running) == ["Session 0000", "Session 0002"], running
        assert app.eval("() => document.querySelector('#hp-needs-list .hpx-ico').classList.contains('question')")
        assert app.eval("() => !document.getElementById('hp-attention-empty')")
        # The lists follow state pushes, like the counts: a working session going idle leaves "Running".
        app.push("session_state", {"session_id": rows[2]["id"], "state": "idle", "cwd": CWD})
        app.page.wait_for_function(
            "Array.from(document.querySelectorAll('#hp-running-list .hpn-row .t')).map(e => e.textContent).join() === 'Session 0000'",
            timeout=5000)
        # The old five-item "Recent" list is gone.
        assert app.eval("() => !Array.from(document.querySelectorAll('.hpx-hd')).some(e => e.textContent === 'Recent')")
        assert not app.errors, app.errors
    finally:
        app.close()


@pytest.mark.slow
def test_lists_cap_at_five(browser):
    rows = _sessions(15)
    app = _home(browser, rows)
    try:
        app.push_many("session_state", [{"session_id": s["id"], "state": "working", "cwd": CWD} for s in rows[:8]]
                      + [{"session_id": s["id"], "state": "waiting", "cwd": CWD} for s in rows[8:15]])
        app.page.wait_for_function("document.getElementById('hp-running-list') !== null", timeout=5000)
        assert len(_titles(app, "hp-needs-list")) == 5
        assert len(_titles(app, "hp-running-list")) == 5
    finally:
        app.close()


@pytest.mark.slow
def test_empty_state_says_nothing_needs_you(browser):
    rows = _sessions(7)
    app = _home(browser, rows)
    try:
        app.page.wait_for_function("document.getElementById('hp-attention-empty') !== null", timeout=5000)
        assert app.eval("() => document.getElementById('hp-attention-empty').textContent") == \
            "Nothing needs you. 7 sleeping."
        # The Open Sessions button is still there.
        assert app.eval("() => document.querySelector('.hp-sessions .hp-cta').textContent.trim()") == "Open Sessions"
        assert app.eval("() => !document.getElementById('hp-needs-list') && !document.getElementById('hp-running-list')")
    finally:
        app.close()


@pytest.mark.slow
def test_clicking_a_row_opens_the_session(browser):
    rows = _sessions(6)
    app = _home(browser, rows)
    try:
        app.push("session_state", {"session_id": rows[4]["id"], "state": "waiting", "cwd": CWD})
        app.page.wait_for_function("document.querySelector('#hp-needs-list .hpn-row') !== null", timeout=5000)
        app.eval("() => document.querySelector('#hp-needs-list .hpn-row').click()")
        app.page.wait_for_function("(id) => window.viewMode === 'sessions' || viewMode === 'sessions'", arg=rows[4]["id"],
                                   timeout=5000)
        app.page.wait_for_function("(id) => typeof liveSessionId !== 'undefined' && liveSessionId === id",
                                   arg=rows[4]["id"], timeout=5000)
        assert not app.errors, app.errors
    finally:
        app.close()


@pytest.mark.slow
def test_strip_jobs_engine_and_naming(browser):
    rows = _sessions(4)
    jobs = {"ok": True, "jobs": [
        {"id": "j1", "state": "needs_person", "name": "transcribe"},
        {"id": "j2", "state": "failed", "name": "encode"},
        {"id": "j3", "state": "running", "name": "fine"},
    ]}
    engine = {"ok": True, "running": True, "stale": True, "outdated": ["daemon/a.py", "daemon/b.py", "daemon/c.py"],
              "started_at": 1, "restart_armed": False}
    routes = {
        "/api/sessions/untitled-count": lambda req: (200, {"project": PROJECT, "total": 40, "untitled": 37, "nameable": 35}),
        "/api/jobs": lambda req: (200, jobs),
        "/api/engine/status": lambda req: (200, engine),
    }
    # Snooze the engine banner itself: the strip must not depend on the banner.
    extra = "localStorage.setItem('engineFreshnessSnoozedUntil', String(Date.now() + 3600e3));"
    app = _home(browser, rows, extra=extra, routes=routes)
    try:
        app.page.wait_for_function("document.getElementById('hp-jobs-link') !== null", timeout=5000)
        assert app.eval("() => document.getElementById('hp-jobs-link').textContent") == "2 durable jobs need you"
        assert app.eval("() => document.getElementById('hp-name-untitled').textContent") == "Name 35 untitled sessions"
        app.eval("() => EngineFreshness.check()")
        app.page.wait_for_function("document.getElementById('hp-engine-stale') !== null", timeout=5000)
        assert app.eval("() => document.getElementById('hp-engine-stale').textContent") == \
            "Session Engine running older code: 3 files changed"
        # The usage gauge is in the strip but hidden until a reading exists.
        assert app.eval("() => getComputedStyle(document.querySelector('#hp-strip .hps-usage')).display") == "none"
        app.eval("() => _ingestUsageLimits({windows: {five_hour: {percent: 42, resets_at: Date.now() / 1000 + 3600}}})")
        assert app.eval("() => getComputedStyle(document.querySelector('#hp-strip .hps-usage')).display") != "none"
        # The jobs link opens System -> Durable Jobs.
        app.eval("() => document.getElementById('hp-jobs-link').click()")
        app.page.wait_for_function("document.querySelector('#pm-overlay .pm-title') && "
                                   "document.querySelector('#pm-overlay .pm-title').textContent === 'Durable Jobs'",
                                   timeout=5000)
        assert not app.errors, app.errors
    finally:
        app.close()


@pytest.mark.slow
def test_strip_hides_naming_below_threshold(browser):
    rows = _sessions(4)
    routes = {"/api/sessions/untitled-count": lambda req: (200, {"project": PROJECT, "total": 9, "untitled": 9, "nameable": 9}),
              "/api/jobs": lambda req: (200, {"ok": True, "jobs": []})}
    app = _home(browser, rows, routes=routes)
    try:
        app.page.wait_for_timeout(800)
        assert app.eval("() => !document.getElementById('hp-name-untitled')")
        assert app.eval("() => !document.getElementById('hp-jobs-link')")
        assert app.eval("() => !document.getElementById('hp-engine-stale')")
    finally:
        app.close()


@pytest.mark.slow
def test_workforce_counts_have_one_source(browser):
    """The homepage Workforce card and the Workforce view header both read
    workforceCounts() (folders.js), so they cannot disagree."""
    tree = {"version": 1, "rootChildren": ["eng", "ops"], "folders": {
        "eng": {"id": "eng", "name": "Engineering", "children": ["be"], "sessions": [],
                "skill": {"label": "Eng lead", "systemPrompt": "x"}},
        "be": {"id": "be", "name": "Backend", "children": [], "sessions": [],
               "skill": {"label": "Backend", "systemPrompt": "y"}},
        "ops": {"id": "ops", "name": "Ops", "children": [], "sessions": []},
    }}
    app = _home(browser, _sessions(3), routes={"/api/folder-tree": lambda req: (200, tree)})
    try:
        app.page.wait_for_function(
            "document.getElementById('hp-workforce-stat').textContent.includes('department')", timeout=5000)
        assert app.eval("() => document.getElementById('hp-workforce-stat').textContent") == "2 agents · 2 departments"
        assert app.eval("() => JSON.stringify(workforceCounts())") == \
            '{"departments":2,"subDepartments":1,"agents":2}'
        app.eval("() => setViewMode('workplace')")
        app.page.wait_for_function("!!document.querySelector('.wf-cc-subtitle')", timeout=5000)
        sub = app.eval("() => document.querySelector('.wf-cc-subtitle').textContent")
        assert sub.startswith("2 departments · 1 sub-department"), sub
        assert app.eval("() => Array.from(document.querySelectorAll('.wf-mode-btn')).map(b => b.textContent)") == \
            ["Workspace", "Configure"]
        assert not app.errors, app.errors
    finally:
        app.close()
