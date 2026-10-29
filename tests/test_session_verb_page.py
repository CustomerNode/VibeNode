"""The sidebar shows what the PERSON has to do (2026-10-27), on the real page.

The REAL ``templates/index.html`` with every real script in headless
Chromium; only the network is faked (``tests/_fake_app.py``).  Sessions come
from ``/api/sessions`` with ``next_step`` (the last message's handoff card,
``common/handoff_status.py``); live updates arrive as ``session_entry`` and
``session_state`` pushes.  Checked: the card labels and colour classes, the
"Active first" order, live changes repainting one card, the funnel's "Needs
you" filter, the list-mode icons, and the terminating workflow: a stopped
session is never Idle, Accept sleeps it and it shows Done.

Drawn statuses are the three groups (owner's pick B, 2026-10-28,
static/scratch/status-merge.html): Needs you / Working / Done (plus Standby).
The fine verb (Decide, Review, ...) is kept in ``SessionVerb.display()``, the
"Active first" rank and the hover title.  Sidebar cards carry no answer
buttons (owner, 2026-10-27) and the "Is this right?" strip under the reply is
never mounted (owner, 2026-10-27: "NO SECOND PANEL").
"""

from __future__ import annotations

import json

import pytest

from tests._browser import shared_browser_scope
from tests._fake_app import CWD, FakeApp, fake_sessions, init_script

XDIST_SPLITTABLE = True

LABELS = """() => Object.fromEntries([...document.querySelectorAll('#workforce-grid .wf-card')]
  .map(c => [c.dataset.sid, c.querySelector('.wf-status-label').textContent]))"""
ORDER = "() => [...document.querySelectorAll('#workforce-grid .wf-card')].map(c => c.dataset.sid)"
VERBS = "(ids) => Object.fromEntries(ids.map(i => [i, SessionVerb.display(i)]))"
# The hold a verdict saves (app/session_holds.py); the page checks the reply.
HOLD_OK = {"/api/session-hold/": lambda r: (200, {"ok": True})}


@pytest.fixture(scope="module")
def browser():
    with shared_browser_scope() as b:
        yield b


def _sessions():
    s = fake_sessions(7)
    s[0]["next_step"] = {"verb": "decide", "line": "Pick B or C"}
    s[1]["next_step"] = {"verb": "unblock", "line": "Log the GitHub CLI in"}
    s[2]["next_step"] = {"verb": "review", "line": "Built it"}
    s[3]["next_step"] = {"verb": "send", "line": "Reply to Dana"}
    s[6]["next_step"] = {"verb": "decide", "line": "Which one?"}   # stays asleep
    return s


def _app(browser, mode, sort_js):
    s = _sessions()
    ids = [x["id"] for x in s]
    unread = json.dumps([ids[2], ids[3], ids[5]])
    a = FakeApp(browser, s, init=init_script("sessions", mode,
                f"localStorage.setItem('vn_unread_sessions', '{unread}'); {sort_js}"),
                extra_routes=HOLD_OK)
    a.push_many("session_state", [{"session_id": i, "state": "idle", "cwd": CWD} for i in ids[:6]])
    a.page.wait_for_timeout(600)
    return a, ids


def _card(sid):
    return f"#workforce-grid .wf-card[data-sid=\"{sid}\"]"


@pytest.mark.slow
def test_cards_say_what_you_have_to_do(browser):
    app, ids = _app(browser, "grid", "localStorage.setItem('wfSort', 'status');")
    try:
        # Every move that is yours is drawn as one status, "Needs you".  A
        # stopped session is never "Idle": no card means Review.  Opening it
        # (the unread flag) changes nothing.  Asleep with a decision: Decide.
        assert app.eval(LABELS) == {i: "Needs you" for i in ids}
        assert app.eval(VERBS, ids) == {
            ids[0]: "decide", ids[1]: "unblock", ids[2]: "review", ids[3]: "send",
            ids[4]: "review", ids[5]: "review", ids[6]: "decide"}
        assert app.eval(f"() => document.querySelector('{_card(ids[1])}').classList.contains('vs-needs')")
        assert app.eval(f"() => !!document.querySelector('{_card(ids[0])} .wf-avatar svg.vs-ic.vs-needs')")
        # Active first: can't-go-on, then your verdict, newest first within.
        assert app.eval(ORDER) == [ids[0], ids[1], ids[6], ids[2], ids[3], ids[4], ids[5]]
        # No answer buttons on a sidebar card; every card keeps its date line.
        assert app.eval("() => document.querySelectorAll('#workforce-grid .vs-verdict').length") == 0
        assert app.eval(f"() => !!document.querySelector('{_card(ids[3])} .wf-meta')")

        # You answered session 0: it works again.
        app.push("session_entry", {"session_id": ids[0], "index": 9, "entry": {"kind": "user", "text": "go with C"}})
        app.push("session_state", {"session_id": ids[0], "state": "working", "cwd": CWD})
        # Session 4 ends a turn on a card that needs an action from you.
        card = {"type": "needs_you", "headline": "Need a token", "needs": {"kind": "action", "ask": "Log in"}}
        app.push("session_entry", {"session_id": ids[4], "index": 9,
                                   "entry": {"kind": "asst", "text": "Done so far.\n\n```handoff\n" + json.dumps(card) + "\n```"}})
        app.push("session_state", {"session_id": ids[4], "state": "idle", "cwd": CWD})   # the turn ends
        # Session 3 scheduled a wake-up.
        app.push("session_state", {"session_id": ids[3], "state": "idle", "cwd": CWD, "substatus": "auto-resuming"})
        # You opened session 2: still waiting for your verdict.
        app.eval(f"() => clearSessionUnread('{ids[2]}')")
        app.page.wait_for_timeout(600)
        got = app.eval(LABELS)
        verbs = app.eval(VERBS, ids)
        assert (got[ids[0]], verbs[ids[0]]) == ("Working", "working")
        assert (got[ids[4]], verbs[ids[4]]) == ("Needs you", "unblock")
        assert (got[ids[3]], verbs[ids[3]]) == ("Working", "waiting")    # its move: it continues by itself
        assert (got[ids[2]], verbs[ids[2]]) == ("Needs you", "review")

        # Accept on session 5: it sleeps (close_session) and shows Done.
        app.eval(f"() => SessionVerdict.accept('{ids[5]}')")
        app.page.wait_for_timeout(400)
        assert app.eval(LABELS)[ids[5]] == "Done"
        assert ["close_session", {"session_id": ids[5]}] in app.eval("() => window.__emits")
        assert app.eval(ORDER)[-1] == ids[5]

        # The funnel's "Needs you" = every move that is yours; "Done" = asleep.
        app.eval("() => toggleSessionFilter('needs')")
        app.page.wait_for_timeout(300)
        assert set(app.eval(ORDER)) == {ids[1], ids[2], ids[4], ids[6]}
        app.eval("() => clearSessionFilters()")
        app.eval("() => toggleSessionFilter('sleeping')")
        app.page.wait_for_timeout(300)
        assert app.eval(ORDER) == [ids[5]]
        app.eval("() => clearSessionFilters()")
        assert not app.errors, app.errors
    finally:
        app.close()


@pytest.mark.slow
def test_list_rows_carry_the_icon_and_no_unread_dot(browser):
    app, ids = _app(browser, "list", "")
    try:
        row = lambda sid: f"#session-list .session-item[data-sid=\"{sid}\"]"
        assert app.eval(f"() => !!document.querySelector('{row(ids[0])} svg.vs-ic.vs-needs')")
        assert app.eval(f"() => !!document.querySelector('{row(ids[5])} svg.vs-ic.vs-needs')")
        assert app.eval(f"() => document.querySelector('{row(ids[0])}').classList.contains('vs-row-yours')")
        assert app.eval("() => document.querySelectorAll('#session-list .vs-row-idle').length") == 0
        # The ask is the icon's tooltip.
        assert app.eval(f"() => document.querySelector('{row(ids[1])} svg.vs-ic title').textContent") == \
            "Needs you: Log the GitHub CLI in"
        # Review / Read / Send say "unread" themselves: the dot is not drawn beside them.
        dots = app.eval("""() => [...document.querySelectorAll('#session-list .unread-dot')]
            .filter(d => getComputedStyle(d).display !== 'none').length""")
        assert dots == 0
        # The funnel offers Done, not Idle.
        assert app.eval("() => !document.querySelector('.sidebar-filter-opt[data-filter=idle]')")
        assert app.eval("() => document.querySelector('.sidebar-filter-opt[data-filter=sleeping]').textContent") == "Done"
        assert not app.errors, app.errors
    finally:
        app.close()


@pytest.mark.slow
def test_open_session_asks_is_this_right(browser):
    """No second panel under the reply (owner, 2026-10-27): the "Is this
    right?" strip is never mounted.  Accept (the end-of-turn card's Done)
    still sleeps the open session."""
    s = fake_sessions(2)
    ids = [x["id"] for x in s]
    routes = {"/api/session/": lambda r: (200, {"id": ids[0], "display_title": "T", "messages": []}), **HOLD_OK}
    app = FakeApp(browser, s, init=init_script("sessions", "grid"), extra_routes=routes)
    try:
        app.push_many("session_state", [{"session_id": i, "state": "idle", "cwd": CWD} for i in ids])
        app.eval(f"() => openInGUI('{ids[0]}')")
        app.page.wait_for_function(f"typeof liveSessionId !== 'undefined' && liveSessionId === '{ids[0]}'", timeout=10000)
        app.push("session_log", {"session_id": ids[0], "total": 2, "offset": 0, "has_more": False, "entries": [
            {"kind": "user", "text": "build it"}, {"kind": "asst", "text": "Built it. Tests pass."}]})
        app.page.wait_for_function("!!document.querySelector('#live-log .msg')", timeout=5000)
        app.page.wait_for_timeout(600)
        assert app.eval(f"() => SessionVerb.display('{ids[0]}')") == "review"
        assert app.eval("() => !document.querySelector('#live-log .vs-verdict-strip')")

        app.eval(f"() => SessionVerdict.accept('{ids[0]}')")
        app.page.wait_for_timeout(400)
        assert ["close_session", {"session_id": ids[0]}] in app.eval("() => window.__emits")
        assert app.eval("() => !document.querySelector('#live-log .vs-verdict-strip')")
        assert not app.errors, app.errors
    finally:
        app.close()


@pytest.mark.slow
def test_new_session_has_no_verdict_and_done_is_grey(browser):
    """A new session has nothing to judge (its verb is ``idle``, drawn as
    New since 2026-10-29): no "Is this right?" strip and no Accept until it replies.  Done looks like Sleeping always did:
    grey label, greyed-out picture, the card not faded as a whole."""
    s = fake_sessions(2)
    s[1]["hold"] = "done"         # Done is the person's verdict, not sleep (2026-10-28)
    ids = [x["id"] for x in s]
    app = FakeApp(browser, s, init=init_script("sessions", "grid"))
    try:
        app.push_many("session_state", [{"session_id": ids[0], "state": "idle", "cwd": CWD}])
        app.page.wait_for_timeout(400)
        new_id = app.eval("async () => { await addNewAgent(); return liveSessionId || activeId; }")
        app.page.wait_for_timeout(800)
        assert app.eval(f"() => SessionVerb.display('{new_id}')") == "idle"
        assert app.eval("() => !document.querySelector('#live-log > .vs-verdict-strip')")
        card = _card(new_id)
        assert app.eval(f"() => document.querySelector('{card} .wf-status-label').textContent") == "New"
        assert app.eval(f"() => !document.querySelector('{card} .vs-verdict')")

        # Its first reply makes it Review, Accept and all.
        app.push("session_entry", {"session_id": new_id, "entry": {"kind": "asst", "text": "Hello."}})
        app.push("session_state", {"session_id": new_id, "state": "idle", "cwd": CWD})   # the turn ends
        app.page.wait_for_timeout(400)
        assert app.eval(f"() => SessionVerb.display('{new_id}')") == "review"

        # Done (the sleeping session ids[1]) is grey.
        done = _card(ids[1])
        got = app.eval(f"""() => {{ const c = document.querySelector('{done}');
            const l = c.querySelector('.wf-status-label');
            const faint = getComputedStyle(document.documentElement).getPropertyValue('--text-faint').trim();
            const probe = document.createElement('span'); probe.style.color = faint; document.body.appendChild(probe);
            const faintRgb = getComputedStyle(probe).color; probe.remove();
            return [l.textContent, getComputedStyle(l).color === faintRgb,
                    getComputedStyle(c.querySelector('.wf-avatar')).filter, getComputedStyle(c).opacity]; }}""")
        assert got[0] == "Done"
        assert got[1], got
        assert "grayscale(1)" in got[2], got
        assert got[3] == "1", got
        assert not app.errors, app.errors
    finally:
        app.close()
