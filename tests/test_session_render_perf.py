"""A ``session_state`` event must not rebuild the whole sidebar.

Before 2026-10-23 the ``session_state`` handler in ``static/js/socket.js``
ended with an unconditional ``filterSessions()``: every state event of every
session rebuilt the entire list or card grid (a string per session,
``innerHTML``, then ``initColResize()`` and ``attachTooltipListeners()`` which
re-attached three listeners to every row).  With 200 sessions a burst of 200
events was 200 full rebuilds, 40,000 row renders.

Now the handler updates the ONE row/card in place (``_updateRowState``: state
classes, the state icon or avatar, the status label, the unread dot) and only
schedules a trailing-debounced full ``filterSessions()`` when a surgical update
cannot express the change: the sort order depends on state ("Active first")
and the session's rank changed, the session has no row on screen, or the view
is not the list/grid (Mission Control, Workforce, Subsessions).

The page under test is the REAL ``templates/index.html`` with every real
script in headless Chromium; only the network is faked (``tests/_fake_app.py``).
"""

from __future__ import annotations

import pytest

from tests._browser import shared_browser_scope
from tests._fake_app import CWD, FakeApp, fake_sessions, init_script

XDIST_SPLITTABLE = True

N = 200
# The grid and list mount the first 150 rows (windowed rendering, app.js);
# events for the other 50 must not trigger a rebuild either.
WINDOW = 150

# Wrap the two renderers so the test can count full rebuilds.  Both are
# top-level function declarations, so the global binding is reassignable and
# ``filterSessions()`` (which calls them by name) picks up the wrapper.
COUNT_RENDERS = """() => {
  window.__renders = {list: 0, grid: 0};
  const _rl = renderList, _rw = renderWorkforce;
  renderList = function () { window.__renders.list++; return _rl.apply(this, arguments); };
  renderWorkforce = function () { window.__renders.grid++; return _rw.apply(this, arguments); };
}"""


@pytest.fixture(scope="module")
def browser():
    with shared_browser_scope() as b:
        yield b


def _events(sessions, state, **extra):
    return [{"session_id": s["id"], "state": state, "cwd": CWD, **extra} for s in sessions]


def _app(browser, display_mode, sort_extra=""):
    sessions = fake_sessions(N)
    a = FakeApp(browser, sessions, init=init_script("sessions", display_mode, sort_extra))
    a.eval(COUNT_RENDERS)
    return a, sessions


def _renders(app):
    return app.eval("() => window.__renders.list + window.__renders.grid")


@pytest.mark.slow
def test_grid_state_burst_updates_cards_in_place(browser):
    app, sessions = _app(browser, "grid")
    try:
        assert app.eval("() => document.querySelectorAll('#workforce-grid .wf-card').length") == WINDOW
        app.push_many("session_state", _events(sessions, "working"))
        app.page.wait_for_timeout(400)          # longer than the 150ms debounce
        full = _renders(app)
        # Date sort: a state change does not move a card, so no rebuild at all.
        assert full == 0, f"{full} full grid rebuilds for {N} state events"
        working = app.eval(
            "() => document.querySelectorAll('#workforce-grid .wf-card.wf-working').length")
        assert working == WINDOW
        labels = app.eval(
            "() => [...new Set([...document.querySelectorAll('#workforce-grid .wf-status-label')].map(e => e.textContent))]")
        assert labels == ["Working"]

        # Compacting is a substatus on a working session: the avatar changes.
        app.push("session_state", {"session_id": sessions[3]["id"], "state": "working",
                                   "cwd": CWD, "substatus": "compacting"})
        card3 = f"#workforce-grid .wf-card[data-sid=\"{sessions[3]['id']}\"]"
        assert app.eval(f"() => document.querySelector('{card3} .wf-status-label').textContent") == "Compacting"
        assert app.eval(f"() => !!document.querySelector('{card3} .wf-avatar .compacting-icon')")

        app.push_many("session_state", _events(sessions, "idle"))
        app.page.wait_for_timeout(400)
        assert _renders(app) == 0
        idle = app.eval("() => document.querySelectorAll('#workforce-grid .wf-card.wf-idle').length")
        assert idle == WINDOW
        # A stopped, awake session waits for your verdict: Review, never "Idle"
        # (session-verb.js, 2026-10-27), drawn as "Needs you" (status-merge B,
        # 2026-10-28).
        assert app.eval(f"() => SessionVerb.display('{sessions[3]['id']}')") == "review"
        assert app.eval(f"() => document.querySelector('{card3} .wf-status-label').textContent") == "Needs you"
        assert not app.errors, app.errors
    finally:
        app.close()


@pytest.mark.slow
def test_list_state_burst_updates_rows_in_place(browser):
    app, sessions = _app(browser, "list")
    try:
        assert app.eval("() => document.querySelectorAll('#session-list .session-item').length") == WINDOW
        app.push_many("session_state", _events(sessions, "working"))
        app.page.wait_for_timeout(400)
        assert _renders(app) == 0
        assert app.eval(
            "() => document.querySelectorAll('#session-list .session-item.si-working.running').length") == WINDOW
        # The state icon is rendered surgically too (the pickaxe for working).
        assert app.eval(
            "() => document.querySelectorAll('#session-list .session-item img.state-icon[src*=pickaxe]').length") == WINDOW
        app.push_many("session_state", _events(sessions, "waiting"))
        app.page.wait_for_timeout(400)
        assert _renders(app) == 0
        assert app.eval(
            "() => document.querySelectorAll('#session-list .session-item.si-question.waiting').length") == WINDOW
        assert not app.errors, app.errors
    finally:
        app.close()


@pytest.mark.slow
def test_active_first_sort_rebuilds_once_per_burst(browser):
    """"Active first" orders by state, so a state change CAN move a card.

    The burst must coalesce into a handful of debounced rebuilds, never one per
    event, and the final order must put the working cards first.
    """
    app, sessions = _app(browser, "grid", "localStorage.setItem('wfSort', 'status');")
    try:
        # Only the second half starts working: their cards must move to the front.
        app.push_many("session_state", _events(sessions[N // 2:], "working"))
        app.page.wait_for_timeout(500)
        full = _renders(app)
        assert 1 <= full <= 3, f"{full} full rebuilds for {N // 2} state events under Active first"
        order = app.eval("() => [...document.querySelectorAll('#workforce-grid .wf-card')].map(c => c.dataset.sid)")
        working_ids = {s["id"] for s in sessions[N // 2:]}
        assert set(order[: N // 2]) == working_ids
        # A second burst that changes no rank (working -> working) rebuilds nothing.
        before = _renders(app)
        app.push_many("session_state", _events(sessions[N // 2:], "working"))
        app.page.wait_for_timeout(400)
        assert _renders(app) == before
        assert not app.errors, app.errors
    finally:
        app.close()
