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
           {"member_id": SAM, "username": "Sam", "role": "member", "you": False}]


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
        g = _grant(body["session_id"], gid="g%d" % len(state["calls"]))
        state["out"].append(g)
        return 200, {"ok": True, "grant": g}

    def revoke(req):
        gid = req.url.rsplit("/", 1)[1]
        state["revoked"].append(gid)
        state["out"] = [g for g in state["out"] if g["grant_id"] != gid]
        return 200, {"ok": True, "revoked": True}

    return {
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
    s = {"out": [], "in": [], "calls": [], "revoked": []}
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
    st = _state(**{"in": [_grant("x-1", owner="Priya", owner_id="33333333-3333-3333-3333-333333333333", title="Flaky login test")]})
    app = FakeApp(browser, rows, extra_routes=_routes(st))
    app.page.wait_for_function("window.PF && PF.isMember() && PF.state().incoming.length")
    app.push("pf_incoming", st["in"][0])
    toast = app.page.wait_for_selector(".pf-toast")
    assert "Priya phoned you" in toast.inner_text() and "Flaky login test" in toast.inner_text()
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
