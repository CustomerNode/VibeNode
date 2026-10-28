"""The sidebar shows what the PERSON has to do (2026-10-27), on the real page.

The REAL ``templates/index.html`` with every real script in headless
Chromium; only the network is faked (``tests/_fake_app.py``).  Sessions come
from ``/api/sessions`` with ``next_step`` (the last message's handoff card,
``common/handoff_status.py``); live updates arrive as ``session_entry`` and
``session_state`` pushes.  Checked: the card labels and colour classes, the
"Active first" order, live changes repainting one card, the funnel's "Needs
you" filter, the list-mode icons, and the terminating workflow: a stopped
session is never Idle, Accept sleeps it and it shows Done.
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
                f"localStorage.setItem('vn_unread_sessions', '{unread}'); {sort_js}"))
    a.push_many("session_state", [{"session_id": i, "state": "idle", "cwd": CWD} for i in ids[:6]])
    a.page.wait_for_timeout(600)
    return a, ids


def _card(sid):
    return f"#workforce-grid .wf-card[data-sid=\"{sid}\"]"


@pytest.mark.slow
def test_cards_say_what_you_have_to_do(browser):
    app, ids = _app(browser, "grid", "localStorage.setItem('wfSort', 'status');")
    try:
        # A stopped session is never "Idle": no card means Review.  Opening it
        # (the unread flag) changes nothing.  Asleep with a decision: Decide.
        assert app.eval(LABELS) == {
            ids[0]: "Decide", ids[1]: "Unblock", ids[2]: "Review", ids[3]: "Send",
            ids[4]: "Review", ids[5]: "Review", ids[6]: "Decide"}
        assert app.eval(f"() => document.querySelector('{_card(ids[1])}').classList.contains('vs-unblock')")
        assert app.eval(f"() => !!document.querySelector('{_card(ids[0])} .wf-avatar svg.vs-ic.vs-decide')")
        # Active first: can't-go-on, then your verdict, newest first within.
        assert app.eval(ORDER) == [ids[0], ids[1], ids[6], ids[2], ids[3], ids[4], ids[5]]
        # Review / Send cards carry the verdict in place of the date; Decide does not.
        assert app.eval(f"() => document.querySelector('{_card(ids[3])} .vs-verdict .vs-ok').textContent") == "✓ Sent it"
        assert app.eval(f"() => !document.querySelector('{_card(ids[0])} .vs-verdict')")

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
        assert got[ids[0]] == "Working"
        assert got[ids[4]] == "Unblock"
        assert got[ids[3]] == "Waiting"
        assert got[ids[2]] == "Review"
        # Session 3's buttons went with its Review: the date line is back.
        assert app.eval(f"() => !document.querySelector('{_card(ids[3])} .vs-verdict') && !!document.querySelector('{_card(ids[3])} .wf-meta')")

        # Accept on session 5's card: it sleeps (close_session) and shows Done.
        app.eval(f"() => document.querySelector('{_card(ids[5])} .vs-verdict .vs-ok').click()")
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
        assert app.eval(f"() => !!document.querySelector('{row(ids[0])} svg.vs-ic.vs-decide')")
        assert app.eval(f"() => !!document.querySelector('{row(ids[5])} svg.vs-ic.vs-review')")
        assert app.eval(f"() => document.querySelector('{row(ids[0])}').classList.contains('vs-row-yours')")
        assert app.eval("() => document.querySelectorAll('#session-list .vs-row-idle').length") == 0
        # The ask is the icon's tooltip.
        assert app.eval(f"() => document.querySelector('{row(ids[1])} svg.vs-ic title').textContent") == \
            "Unblock: Log the GitHub CLI in"
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
    """The verdict at the end of the open thread: Send back points the composer
    at what's wrong, the strip leaves while it works, Accept sleeps it."""
    s = fake_sessions(2)
    ids = [x["id"] for x in s]
    app = FakeApp(browser, s, init=init_script("sessions", "grid"),
                  extra_routes={"/api/session/": lambda r: (200, {"id": ids[0], "display_title": "T", "messages": []})})
    try:
        app.push_many("session_state", [{"session_id": i, "state": "idle", "cwd": CWD} for i in ids])
        app.eval(f"() => openInGUI('{ids[0]}')")
        app.page.wait_for_function(f"typeof liveSessionId !== 'undefined' && liveSessionId === '{ids[0]}'", timeout=10000)
        app.push("session_log", {"session_id": ids[0], "total": 2, "offset": 0, "has_more": False, "entries": [
            {"kind": "user", "text": "build it"}, {"kind": "asst", "text": "Built it. Tests pass."}]})
        app.page.wait_for_function("!!document.querySelector('#live-log > .vs-verdict-strip')", timeout=5000)
        # After the last message, before the thread's own jump button / sentinel; and it stays put.
        assert app.eval("""() => { const s = document.querySelector('#live-log > .vs-verdict-strip');
            const p = s.previousElementSibling; return !!p && p.classList.contains('msg'); }""")
        app.eval("() => { window.__muts = 0; new MutationObserver(m => window.__muts += m.length).observe(document.getElementById('live-log'), {childList: true}); }")
        app.page.wait_for_timeout(600)
        assert app.eval("() => window.__muts") == 0

        app.eval("() => document.querySelector('#live-log > .vs-verdict-strip .vs-back').click()")
        app.page.wait_for_timeout(300)
        assert app.eval("() => { const t = document.getElementById('live-input-ta'); return [t.placeholder, document.activeElement === t]; }") == \
            ["What’s wrong, and what do you want fixed?", True]

        app.push("session_state", {"session_id": ids[0], "state": "working", "cwd": CWD})
        app.page.wait_for_timeout(300)
        assert app.eval("() => !document.querySelector('#live-log > .vs-verdict-strip')")

        app.push("session_state", {"session_id": ids[0], "state": "idle", "cwd": CWD})
        app.page.wait_for_function("!!document.querySelector('#live-log > .vs-verdict-strip')", timeout=5000)
        app.eval("() => document.querySelector('#live-log > .vs-verdict-strip .vs-accept').click()")
        app.page.wait_for_timeout(400)
        assert ["close_session", {"session_id": ids[0]}] in app.eval("() => window.__emits")
        assert app.eval("() => !document.querySelector('#live-log > .vs-verdict-strip')")
        assert not app.errors, app.errors
    finally:
        app.close()


@pytest.mark.slow
def test_new_session_has_no_verdict_and_done_is_grey(browser):
    """A new session has nothing to judge: no "Is this right?" strip and no
    Accept on its card until it replies.  Done looks like Sleeping always did:
    grey label, greyed-out picture, the card not faded as a whole."""
    s = fake_sessions(2)
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
