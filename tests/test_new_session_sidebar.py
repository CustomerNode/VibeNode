"""A new session in the sidebar: labelled New, first and highlighted at once,
and still there when you click away and come back (owner, 2026-10-29).

The owner: "when i do a new session it goes to needs you in the left menu (i
think it should be branded as new). that its super delayed before the left
card gets focused (should be immediate) and it gets fucked if i click away
from it and try to go back".  What was wrong, each pinned below:

- The never-sent session's status (``idle`` in session-verb.js) was drawn in
  the Needs-you group.  It is its own group now, "New", ranked first in
  "Active first".
- ``addNewAgent()`` drew the sidebar BEFORE making the new session the open
  one, so its card was not highlighted until some later redraw; and the
  placeholder row had no timestamp, so it sorted to the bottom of the list.
- Opening another session asks the engine for a state snapshot, and the
  snapshot handler dropped every GUI-open session that was not running: the
  unsent new one included.  Clicking back then showed "Session not found".
  Text typed before clicking away was not restored either.
- The id remap of a new session (seconds into its first turn) wrote its
  title over the toolbar even when another session was open, and a slow
  first send pulled the screen back to the new session.

The REAL ``templates/index.html`` with every real script in headless
Chromium; only the network is faked (``tests/_fake_app.py``).
"""

from __future__ import annotations

import pytest

from tests._browser import shared_browser_scope
from tests._fake_app import CWD, FakeApp, fake_sessions, init_script

XDIST_SPLITTABLE = True

CARDS = "() => [...document.querySelectorAll('#workforce-grid .wf-card')].map(c => c.dataset.sid)"
SELECTED = "() => [...document.querySelectorAll('#workforce-grid .wf-card.wf-selected')].map(c => c.dataset.sid)"
TITLE = "() => document.getElementById('main-title').textContent"


@pytest.fixture(scope="module")
def browser():
    with shared_browser_scope() as b:
        yield b


def _app(browser, sort="recent", extra=""):
    """30 sleeping sessions; /api/session/<id> answers 404 for an id the
    server has never seen, as the real route does (sessions_api.api_session)."""
    s = fake_sessions(30)
    known = {x["id"] for x in s}
    started: set = set()

    def session_route(req):
        sid = req.url.split("/api/session/")[1].split("?")[0]
        if sid in known or sid in started:
            return 200, {"id": sid, "display_title": "Session " + sid[:4], "custom_title": ""}
        return 404, {"error": "Not found"}

    app = FakeApp(browser, s, init=init_script("sessions", "grid",
                  f"localStorage.setItem('wfSort', '{sort}'); {extra}"),
                  extra_routes={"/api/session/": session_route})
    app.started = started
    return app, s


def _open(app, sid, wait=400):
    app.eval("(id) => openInGUI(id)", sid)
    app.page.wait_for_timeout(wait)


@pytest.mark.parametrize("sort", ["recent", "status"])
def test_a_new_session_is_labelled_new_first_and_highlighted_at_once(browser, sort):
    app, s = _app(browser, sort)
    try:
        _open(app, s[5]["id"])
        # Read in the same task as the click: no redraw may be needed.
        got = app.eval("""() => { addNewAgent(); const id = activeId;
            const cards = [...document.querySelectorAll('#workforce-grid .wf-card')];
            const c = cards.find(x => x.dataset.sid === id);
            return {id, first: cards[0] && cards[0].dataset.sid === id,
                    selected: [...document.querySelectorAll('#workforce-grid .wf-card.wf-selected')].map(x => x.dataset.sid),
                    label: c && c.querySelector('.wf-status-label').textContent,
                    cls: c && c.className}; }""")
        assert got["first"], got
        assert got["selected"] == [got["id"]], got
        assert got["label"] == "New", got
        assert "vs-new" in got["cls"] and "vs-needs" not in got["cls"], got
        assert not app.errors, app.errors
    finally:
        app.close()


def test_clicking_away_and_back_keeps_the_new_session_and_its_text(browser):
    app, s = _app(browser)
    other = s[5]["id"]
    try:
        _open(app, other)
        new_id = app.eval("() => { addNewAgent(); return activeId; }")
        app.page.fill("#live-input-ta", "half typed idea")
        _open(app, other)
        # Opening a sleeping session asks the engine for a snapshot; the
        # engine knows nothing about a session that was never sent.
        app.push("state_snapshot", {"sessions": []})
        app.page.wait_for_timeout(200)
        assert app.eval(CARDS)[0] == new_id

        _open(app, new_id, 600)
        got = app.eval("""() => { const ta = document.getElementById('live-input-ta');
            return {body: document.getElementById('main-body').innerText,
                    value: ta && ta.value,
                    newComposer: !!(ta && (ta.getAttribute('onkeydown') || '').includes('_newSessionSubmit'))}; }""")
        assert "Session not found" not in got["body"], got
        assert got["newComposer"], got
        assert got["value"] == "half typed idea", got
        assert app.eval(SELECTED) == [new_id]
        assert app.eval(f"() => SessionVerb.display('{new_id}')") == "idle"

        # A reload of the list (a reconnect) keeps it too.
        _open(app, other)
        app.eval("() => loadSessions()")
        app.page.wait_for_timeout(600)
        assert new_id in app.eval(CARDS)
        assert not app.errors, app.errors
    finally:
        app.close()


def test_sending_clears_new_and_coming_back_mid_turn_shows_the_session(browser):
    app, s = _app(browser)
    other = s[5]["id"]
    try:
        new_id = app.eval("() => { addNewAgent(); return activeId; }")
        app.page.fill("#live-input-ta", "build me a thing")
        app.eval("(id) => _newSessionSubmit(id)", new_id)
        app.page.wait_for_timeout(400)
        assert app.eval(f"() => !!SessionStore.get('{new_id}').unsent") is False
        assert app.eval("() => window.__emits.some(e => e[0] === 'start_session')")
        label = app.eval(f"() => document.querySelector('.wf-card[data-sid=\"{new_id}\"] .wf-status-label').textContent")
        assert label == "Working"

        # Back before the engine has registered it: its panel, not "not found".
        _open(app, other)
        _open(app, new_id, 600)
        assert "Session not found" not in app.eval("() => document.getElementById('main-body').innerText")
        assert app.eval("() => liveSessionId") == new_id
        assert not app.errors, app.errors
    finally:
        app.close()


def test_a_remap_while_you_read_another_session_leaves_its_toolbar_alone(browser):
    app, s = _app(browser)
    other = s[5]["id"]
    real = "11111111-2222-4333-8444-555555555555"
    try:
        new_id = app.eval("() => { addNewAgent(); return activeId; }")
        app.page.fill("#live-input-ta", "build me a thing")
        app.eval("(id) => _newSessionSubmit(id)", new_id)
        app.page.wait_for_timeout(300)
        app.started.add(new_id)
        _open(app, other)
        before = app.eval(TITLE)
        app.push("session_id_remapped", {"old_id": new_id, "new_id": real})
        app.page.wait_for_timeout(200)
        assert app.eval(TITLE) == before
        assert app.eval("() => activeId") == other

        # Its card now carries the CLI's id and opens it.
        app.started.add(real)
        app.eval("(r) => document.querySelector('.wf-card[data-sid=\"' + r + '\"]').click()", real)
        app.page.wait_for_timeout(600)
        assert app.eval("() => liveSessionId") == real
        assert app.eval(SELECTED) == [real]
        assert not app.errors, app.errors
    finally:
        app.close()


def test_a_slow_first_send_does_not_pull_you_back(browser):
    # The first send of a page load waits for the agent catalog file; make
    # that wait long enough to click away during it.
    app, s = _app(browser)
    other = s[5]["id"]
    try:
        app.eval("() => { window._ensureAgentCatalog = () => new Promise(r => setTimeout(r, 500)); }")
        new_id = app.eval("() => { addNewAgent(); return activeId; }")
        app.page.fill("#live-input-ta", "build me a thing")
        app.eval("(id) => { _newSessionSubmit(id); }", new_id)
        _open(app, other, 900)
        assert app.eval("() => window.__emits.some(e => e[0] === 'start_session')")
        assert app.eval("() => [activeId, liveSessionId]") == [other, other]
        assert app.eval(SELECTED) == [other]
        assert "build me a thing" not in app.eval(TITLE)
        assert not app.errors, app.errors
    finally:
        app.close()


def test_a_slow_reply_for_an_earlier_click_does_not_draw_over_the_later_one(browser):
    s0 = fake_sessions(30)
    slow = s0[3]["id"]
    extra = ("const _f = window.fetch; window.fetch = (u, o) => String(u).includes('/api/session/%s')"
             " ? new Promise(r => setTimeout(r, 500)).then(() => _f(u, o)) : _f(u, o);" % slow)
    app, s = _app(browser, extra=extra)
    fast = s[7]["id"]
    try:
        app.eval("(id) => { openInGUI(id); }", slow)
        _open(app, fast, 900)
        assert app.eval("() => [activeId, liveSessionId]") == [fast, fast]
        assert app.eval(SELECTED) == [fast]
        assert not app.errors, app.errors
    finally:
        app.close()
