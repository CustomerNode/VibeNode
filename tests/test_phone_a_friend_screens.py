"""Phone a Friend's screens on the real page (static/js/phone-a-friend.js).

The real index.html with every real script in headless Chromium; only the
network is faked. Covers the menu item (only in an org), the call popover, the
phone mark on the row and in the header with Revoke, the plain toast on a
call, one project row per friend who phoned, and the System → VibeNode Orgs
panel with its copyable invite.
"""

from __future__ import annotations

import json

import pytest

from tests._browser import shared_browser_scope
from tests._fake_app import FakeApp, fake_sessions

ME = {"ok": True, "member": True, "org_name": "Pair Up", "username": "Dana", "role": "owner",
      "member_id": "11111111-1111-1111-1111-111111111111", "cloud_storage": True, "publishable_key_set": True}
SAM = "22222222-2222-2222-2222-222222222222"
MEMBERS = [{"member_id": ME["member_id"], "username": "Dana", "role": "owner", "you": True},
           {"member_id": SAM, "username": "Sam", "role": "member", "you": False},
           {"member_id": "44444444-4444-4444-4444-444444444444", "username": "Lee", "role": "member", "you": False}]


@pytest.fixture(scope="module")
def browser():
    with shared_browser_scope() as b:
        yield b


def _grant(sid, guest="Sam", gid="g1", live=True, owner="Dana", owner_id=None, title="Session 0000"):
    return {"grant_id": gid, "share_id": "s-" + gid, "session_id": sid, "source": "box", "title": title,
            "level": "drive", "live": live, "expires_at": None, "guest": guest, "guest_id": SAM,
            "owner": owner, "owner_id": owner_id or ME["member_id"]}


def _routes(state, member=True):
    def j(body):
        return lambda req: (200, body() if callable(body) else body)

    def call(req):
        body = json.loads(req.post_data or "{}")
        state["calls"].append(body)
        name = {m["member_id"]: m["username"] for m in MEMBERS}[body["guest_id"]]
        g = _grant(body["session_id"], guest=name, gid="g%d" % len(state["calls"]))
        g["note"], g["note_by"] = body.get("note", ""), "Dana"
        state["out"].append(g)
        return 200, {"ok": True, "grant": g}

    def revoke(req):
        gid = req.url.rsplit("/", 1)[1]
        state["revoked"].append(gid)
        state["out"] = [g for g in state["out"] if g["grant_id"] != gid]
        return 200, {"ok": True, "revoked": True}

    def note(req):
        gid = req.url.rsplit("/", 1)[1]
        body = json.loads(req.post_data or "{}")
        state["notes"].append((gid, body["note"]))
        g = dict([x for x in state["out"] + state["in"] if x["grant_id"] == gid][0], note=body["note"], note_by="Dana")
        return 200, {"ok": True, "grant": g}

    return {
        "/api/phone/note/": note,
        # Longest prefix first: the fake app matches routes by prefix.
        "/api/orgs/members": j({"ok": True, "members": MEMBERS}),
        "/api/orgs/me": j(ME if member else {"ok": True, "member": False, "cloud_storage": True}),
        "/api/orgs/invite": j({"ok": True, "prompt": "Dana invited you\n```vibenode-orgs-invite\n{}\n```", "expires_at": "2026-01-04"}),
        "/api/phone/out": j(lambda: {"ok": True, "grants": state["out"]}),
        "/api/phone/in": j(lambda: {"ok": True, "grants": state["in"]}),
        "/api/phone/call": call,
        "/api/phone/revoke/": revoke,
    }


def _state(**kw):
    s = {"out": [], "in": [], "calls": [], "revoked": [], "notes": []}
    s.update(kw)
    return s


def _open_menu(app, sid):
    app.page.evaluate("sid => sessionContextMenu({preventDefault(){}, stopPropagation(){}, clientX: 200, clientY: 200}, sid)", sid)


def test_phone_a_friend_is_in_the_menu_only_in_an_org(browser):
    rows = fake_sessions(2)
    app = FakeApp(browser, rows, extra_routes=_routes(_state(), member=False))
    app.page.wait_for_function("window.PF && PF.state().loaded")
    _open_menu(app, rows[0]["id"])
    assert "Phone a friend" not in app.page.inner_text(".session-ctx-menu")
    app2 = FakeApp(browser, rows, extra_routes=_routes(_state()))
    app2.page.wait_for_function("window.PF && PF.isMember()")
    _open_menu(app2, rows[0]["id"])
    assert "Phone a friend" in app2.page.inner_text(".session-ctx-menu")
    assert not app.errors and not app2.errors


def test_calling_marks_the_row_and_revoke_clears_it(browser):
    rows = fake_sessions(2)
    sid = rows[0]["id"]
    st = _state()
    app = FakeApp(browser, rows, extra_routes=_routes(st))
    app.page.wait_for_function("window.PF && PF.isMember()")
    _open_menu(app, sid)
    app.page.click(".session-ctx-menu >> text=Phone a friend")
    app.page.click(".pf-pop .pf-opt >> text=Sam")
    app.page.click(".pf-pop .pf-go")
    app.page.wait_for_function(f"document.querySelector('[data-sid=\"{sid}\"] .pf-mark')")
    assert st["calls"][0]["guest_id"] == SAM and st["calls"][0]["level"] == "drive"
    # Open the session: the header shows the mark; it lists Sam with Revoke.
    app.page.evaluate("sid => { activeId = sid; }", sid)
    app.page.wait_for_selector("#pf-head-mark", state="attached", timeout=5000)
    app.page.evaluate("() => document.getElementById('pf-head-mark').click()")
    assert "Sam" in app.page.inner_text(".pf-pop")
    app.page.click(".pf-pop [data-g]")
    app.page.wait_for_function(f"!document.querySelector('[data-sid=\"{sid}\"] .pf-mark')")
    assert st["revoked"] == ["g1"]
    assert not app.errors


def test_a_call_to_you_shows_a_toast_and_a_project_row(browser):
    rows = fake_sessions(1)
    st = _state(**{"in": [dict(_grant("x-1", owner="Priya", owner_id="33333333-3333-3333-3333-333333333333", title="Flaky login test"), project="Acme")]})
    app = FakeApp(browser, rows, extra_routes=_routes(st))
    app.page.wait_for_function("window.PF && PF.isMember() && PF.state().incoming.length")
    app.push("pf_incoming", st["in"][0])
    toast = app.page.wait_for_selector(".pf-toast")
    assert "Priya phoned you" in toast.inner_text() and "Acme · Flaky login test" in toast.inner_text()
    app.page.evaluate("() => PF.openFriend('33333333-3333-3333-3333-333333333333')")
    assert "Acme" in app.page.wait_for_selector(".pf-vrow .pf-vproj").inner_text()
    assert "in Acme on box" in app.page.inner_text(".pf-banner")
    app.page.evaluate("() => document.querySelector('.pf-view').remove()")
    html = app.page.evaluate("() => PF.projectRows()")
    assert "Priya phoned you" in html and html.count("pf-friend-row") == 1
    assert not app.errors


def test_no_friend_row_when_nobody_phoned(browser):
    app = FakeApp(browser, fake_sessions(1), extra_routes=_routes(_state()))
    app.page.wait_for_function("window.PF && PF.isMember()")
    assert app.page.evaluate("() => PF.projectRows()") == ""


def test_the_orgs_panel_copies_an_invite(browser):
    app = FakeApp(browser, fake_sessions(1), extra_routes=_routes(_state()))
    app.page.wait_for_function("window.PF && PF.isMember()")
    app.page.evaluate("() => openVibeNodeOrgs()")
    app.page.wait_for_selector(".pf-mrow >> text=Sam")
    app.page.click(".pf-mkinv")
    pre = app.page.wait_for_selector(".pf-prompt")
    assert "vibenode-orgs-invite" in pre.inner_text()
    assert not app.errors


def test_the_note_is_written_shown_and_edited_on_both_sides(browser):
    rows = fake_sessions(2)
    sid = rows[0]["id"]
    st = _state(**{"in": [dict(_grant("x-1", gid="gin", owner="Priya", owner_id="33333333-3333-3333-3333-333333333333",
                                      title="Flaky login test"), note="Login flakes on CI", note_by="Priya")]})
    app = FakeApp(browser, rows, extra_routes=_routes(st))
    app.page.wait_for_function("window.PF && PF.isMember() && PF.state().incoming.length")
    # Phoning with a note.
    _open_menu(app, sid)
    app.page.click(".session-ctx-menu >> text=Phone a friend")
    app.page.click(".pf-pop .pf-opt >> text=Sam")
    app.page.fill(".pf-pop .pf-callnote-in", "CI fails on Node 20")
    app.page.click(".pf-pop .pf-go")
    app.page.wait_for_function(f"document.querySelector('[data-sid=\"{sid}\"] .pf-mark')")
    assert st["calls"][0]["note"] == "CI fails on Node 20"
    # The caller sees it in the list and edits it later.
    app.page.evaluate("sid => { activeId = sid; }", sid)
    app.page.wait_for_selector("#pf-head-mark", state="attached", timeout=5000)
    app.page.evaluate("() => document.getElementById('pf-head-mark').click()")
    assert "CI fails on Node 20" in app.page.inner_text(".pf-pop .pf-callnote")
    app.page.click(".pf-pop .pf-nedit")
    app.page.fill(".pf-pop .pf-nin", "Lockfile, fixed")
    app.page.click(".pf-pop .pf-nsave")
    app.page.wait_for_function("document.querySelector('.pf-pop .pf-callnote') && document.querySelector('.pf-pop .pf-callnote').innerText.includes('Lockfile, fixed')")
    assert st["notes"] == [("g1", "Lockfile, fixed")]
    # The friend's side: the toast, the project row and the friend view show the note.
    app.page.keyboard.press("Escape")
    app.push("pf_incoming", st["in"][0])
    assert "Login flakes on CI" in app.page.wait_for_selector(".pf-toast").inner_text()
    assert "Login flakes on CI" in app.page.evaluate("() => PF.projectRows()")
    app.page.evaluate("() => PF.openFriend('33333333-3333-3333-3333-333333333333')")
    assert "Login flakes on CI" in app.page.wait_for_selector(".pf-vnotebox .pf-callnote").inner_text()
    # A change from the other side repaints every copy.
    app.push("pf_note", dict(st["in"][0], note="Pinned the clock", note_by="Priya"))
    app.page.wait_for_function("document.querySelector('.pf-vnotebox').innerText.includes('Pinned the clock')")
    assert not app.errors


def test_one_call_can_reach_several_people(browser):
    rows = fake_sessions(2)
    sid = rows[0]["id"]
    st = _state()
    app = FakeApp(browser, rows, extra_routes=_routes(st))
    app.page.wait_for_function("window.PF && PF.isMember()")
    _open_menu(app, sid)
    app.page.click(".session-ctx-menu >> text=Phone a friend")
    go = app.page.locator(".pf-pop .pf-go")
    app.page.click(".pf-pop .pf-opt >> text=Sam")
    assert go.inner_text() == "Call Sam"
    app.page.click(".pf-pop .pf-opt >> text=Lee")
    assert go.inner_text() == "Call 2 people"
    app.page.click(".pf-pop .pf-opt >> text=Sam")          # tap again to drop someone
    assert go.inner_text() == "Call Lee"
    app.page.click(".pf-pop .pf-opt >> text=Sam")
    app.page.fill(".pf-pop .pf-callnote-in", "Both of you, please")
    go.click()
    app.page.wait_for_function("!document.querySelector('.pf-pop')")
    assert sorted(c["guest_id"] for c in st["calls"]) == sorted([SAM, "44444444-4444-4444-4444-444444444444"])
    assert {c["note"] for c in st["calls"]} == {"Both of you, please"}
    app.page.evaluate("sid => { activeId = sid; }", sid)
    app.page.wait_for_selector("#pf-head-mark", state="attached", timeout=5000)
    app.page.evaluate("() => document.getElementById('pf-head-mark').click()")
    text = app.page.inner_text(".pf-pop")
    assert "Shared with 2" in text and "Sam" in text and "Lee" in text
    assert not app.errors


def test_phone_a_friend_is_in_the_composer_menu_beside_send(browser):
    """The four-squares More button beside Send lists Phone a friend while this
    VibeNode is in an org, and opens the call popover from there."""
    from tests.test_enter_to_send import ROW, _open
    pg = _open(browser, 1400)
    pg.evaluate("() => { for (const id of ['vn-splash', 'health-blocker']) document.getElementById(id)?.remove(); }")
    pg.wait_for_selector("#live-image-btn", state="attached")
    pg.wait_for_function("window.PF && PF.state().loaded")
    pg.evaluate("document.getElementById('live-image-btn').click()")
    assert "Phone a friend" not in pg.inner_text(".vn-more-menu")      # not in an org
    pg.evaluate("document.getElementById('live-image-btn').click()")    # close
    pg.evaluate("sid => { PF.state().me = {ok: true, member: true, username: 'Dana'}; activeId = sid; }", ROW["id"])
    pg.evaluate("document.getElementById('live-image-btn').click()")
    assert "Phone a friend" in pg.inner_text(".vn-more-menu")
    pg.evaluate("() => [...document.querySelectorAll('.vn-more-menu .vn-more-row')].find(r => r.innerText.includes('Phone a friend')).click()")
    pg.wait_for_selector(".pf-pop")
    assert not pg.query_selector(".vn-more-menu")
    assert "Phone a friend" in pg.inner_text(".pf-pop h4")


def test_the_friend_view_shows_summaries_as_words_and_can_hang_up(browser):
    rows = fake_sessions(1)
    owner = "33333333-3333-3333-3333-333333333333"
    st = _state(**{"in": [_grant("x-1", gid="gin", owner="Priya", owner_id=owner, title="Flaky login test")]})
    routes = _routes(st)
    card = json.dumps({"type": "done", "title": "Login test fixed", "headline": "The clock is pinned now.", "answer": "Short answer here."})
    posts = [{"seq": 1, "kind": "entry", "body": {"kind": "user", "text": "why does it flake"}},
             {"seq": 2, "kind": "entry", "body": {"kind": "asst", "text": "Done.\n\n```handoff\n" + card + "\n```"}}]
    routes = dict({"/api/phone/read/": lambda req: (200, {"ok": True, "posts": posts, "status": "idle"})}, **routes)
    app = FakeApp(browser, rows, extra_routes=routes)
    app.page.wait_for_function("window.PF && PF.isMember() && PF.state().incoming.length")
    app.page.evaluate(f"() => PF.openFriend('{owner}')")
    app.page.wait_for_selector(".pf-thread .pf-card")
    text = app.page.inner_text(".pf-thread")
    assert "Login test fixed" in text and "The clock is pinned now." in text and '"type"' not in text
    app.page.click(".pf-banner .pf-hang")
    app.page.wait_for_function("document.querySelector('.pf-toast, #toast.show') || true")
    assert st["revoked"] == ["gin"]
    assert not app.errors
