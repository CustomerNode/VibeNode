"""Pinned sessions (owner, 2026-10-29, pick B of static/scratch/session-pin.html):
a pinned session stays first in the sidebar whatever the sort.

Server: ``app/session_pins.py`` and ``POST /api/session-pin/<sid>`` (kept per
project, listed as ``pinned: true``, broadcast as ``session_pin``).
Browser: ``static/js/sessions.js`` (``_pinnedFirst``, ``_pinButtonHtml``,
``toggleSessionPin``, the right-click item), ``workforce.js`` (the card),
``socket.js`` (the broadcast) and the ``.vn-pin`` rules in ``style.css``,
against the real page in headless Chromium (``tests/_fake_app.py``).
"""

from __future__ import annotations

import json
import time
from datetime import datetime, timedelta, timezone

import pytest

from tests._browser import shared_browser_scope
from tests._fake_app import PROJECT, FakeApp, fake_sessions, init_script

XDIST_SPLITTABLE = True


# ── Server ─────────────────────────────────────────────────────────────────

def _line(kind, text, ts, sid):
    return json.dumps({"type": kind, "message": {"role": kind, "content": text}, "timestamp": ts, "sessionId": sid})


def _iso(minutes_ago: float) -> str:
    return (datetime.now(timezone.utc) - timedelta(minutes=minutes_ago)).isoformat()


@pytest.fixture()
def proj(tmp_path, monkeypatch):
    root = tmp_path / "projects"
    root.mkdir()
    import app.config as cfg
    import app.routes.sessions_api as sa
    import app.sessions as sess
    monkeypatch.setattr(cfg, "_CLAUDE_PROJECTS", root)
    monkeypatch.setattr(sa, "_CLAUDE_PROJECTS", root)
    cfg._summary_cache.clear()
    cfg._summary_cache_index.clear()
    sess._orphan_cache.clear()
    name = "C--Users-test-pin"
    (root / name).mkdir()
    for sid in ("deck", "other"):
        (root / name / f"{sid}.jsonl").write_text(
            _line("user", "Make the deck", _iso(30), sid) + "\n" + _line("assistant", "Done.", _iso(29), sid) + "\n",
            encoding="utf-8")
    return root / name


@pytest.fixture()
def client():
    from app import create_app
    return create_app(testing=True).test_client()


def _list(client, proj):
    return {s["id"]: s for s in client.get(f"/api/sessions?project={proj.name}").get_json()}


def test_no_pins_lists_exactly_as_before(client, proj):
    rows = _list(client, proj)
    assert "pinned" not in rows["deck"] and "pinned" not in rows["other"]
    assert not (proj / "_session_pins.json").exists()


def test_a_pin_is_kept_listed_and_cleared(client, proj):
    r = client.post("/api/session-pin/deck", json={"pinned": True, "project": proj.name})
    assert r.status_code == 200 and r.get_json() == {"ok": True, "pinned": True, "rank": 0}
    rows = _list(client, proj)
    assert rows["deck"]["pinned"] is True and rows["deck"]["pin_rank"] == 0
    assert "pinned" not in rows["other"] and "pin_rank" not in rows["other"]
    client.post("/api/session-pin/deck", json={"pinned": False, "project": proj.name})
    assert "pinned" not in _list(client, proj)["deck"]


def test_a_pin_outlives_new_messages(client, proj):
    """Unlike Standby, a pin stays until it is unpinned."""
    client.post("/api/session-pin/deck", json={"pinned": True, "project": proj.name})
    with open(proj / "deck.jsonl", "a", encoding="utf-8") as f:
        f.write(_line("user", "one more thing", _iso(0), "deck") + "\n")
    assert _list(client, proj)["deck"]["pinned"] is True


def test_the_route_refuses_bad_input(client, proj):
    assert client.post("/api/session-pin/deck", json={"pinned": "yes", "project": proj.name}).status_code == 400
    assert client.post("/api/session-pin/deck", json={"project": proj.name}).status_code == 400
    assert client.post("/api/session-pin/a..b", json={"pinned": True, "project": proj.name}).status_code == 400
    assert client.post("/api/session-pin/deck", json={"pinned": True, "project": "../x"}).status_code == 400


def test_a_pin_is_broadcast(client, proj, monkeypatch):
    import app as appmod
    sent = []
    monkeypatch.setattr(appmod.socketio, "emit", lambda ev, data=None, **kw: sent.append((ev, data)))
    client.post("/api/session-pin/deck", json={"pinned": True, "project": proj.name})
    assert sent == [("session_pin", {"session_id": "deck", "pinned": True, "rank": 0, "project": proj.name})]
    client.post("/api/session-pin-order", json={"order": ["deck"], "project": proj.name})
    assert sent[-1] == ("session_pin_order", {"order": ["deck"], "project": proj.name})


def test_a_new_pin_goes_to_the_top_and_a_drag_order_is_kept(client, proj):
    for sid in ("deck", "other"):
        client.post(f"/api/session-pin/{sid}", json={"pinned": True, "project": proj.name})
    rows = _list(client, proj)
    assert rows["other"]["pin_rank"] < rows["deck"]["pin_rank"]      # newest pin on top
    r = client.post("/api/session-pin-order", json={"order": ["deck", "other", "nope"], "project": proj.name})
    assert r.status_code == 200 and r.get_json()["order"] == ["deck", "other"]   # unpinned ids skipped
    rows = _list(client, proj)
    assert (rows["deck"]["pin_rank"], rows["other"]["pin_rank"]) == (0, 1)
    # Re-pinning a pinned session keeps its place.
    client.post("/api/session-pin/other", json={"pinned": True, "project": proj.name})
    assert _list(client, proj)["other"]["pin_rank"] == 1


def test_the_order_route_refuses_bad_input(client, proj):
    bad = [{"order": "deck"}, {"order": []}, {"order": [1]}, {"order": ["a/b"]}, {"order": ["a..b"]}]
    for body in bad:
        assert client.post("/api/session-pin-order", json={**body, "project": proj.name}).status_code == 400, body
    assert client.post("/api/session-pin-order", json={"order": ["deck"], "project": "..\\x"}).status_code == 400


def test_a_drag_order_finds_a_pin_under_its_launch_id(proj):
    from app import session_pins
    session_pins.set_pin(proj, "launch", True)
    session_pins.set_pin(proj, "deck", True)
    assert session_pins.set_order(proj, ["shown", "deck"], {"launch": "shown"}) == ["shown", "deck"]
    pins = session_pins.load(proj)
    assert (pins["launch"]["rank"], pins["deck"]["rank"]) == (0, 1)


def test_a_pin_follows_a_renamed_id_and_unpins_under_it(client, proj, monkeypatch):
    """Pinned under the launch id during the first turn, listed under the
    CLI's id later; unpinning under the new id must not leave the old pin
    to be found through the alias."""
    import app.routes.sessions_api as sa
    client.post("/api/session-pin/deck", json={"pinned": True, "project": proj.name})
    monkeypatch.setattr(sa, "_load_remaps_cached", lambda project="": {"deck": {"new_id": "deck-new"}})
    rows = _list(client, proj)
    assert rows["deck-new"]["pinned"] is True
    client.post("/api/session-pin/deck-new", json={"pinned": False, "project": proj.name})
    assert "pinned" not in _list(client, proj)["deck-new"]
    assert json.loads((proj / "_session_pins.json").read_text(encoding="utf-8")) == {}


def test_old_pins_of_gone_sessions_are_forgotten(proj):
    from app import session_pins
    (proj / "_session_pins.json").write_text(json.dumps({
        "gone-old": {"set": time.time() - 100 * 86400},
        "gone-new": {"set": time.time()},
        "deck": {"set": time.time() - 100 * 86400},
    }), encoding="utf-8")
    session_pins.set_pin(proj, "other", True)
    assert set(session_pins.load(proj)) == {"gone-new", "deck", "other"}


# ── Browser: the real page ─────────────────────────────────────────────────

@pytest.fixture(scope="module")
def browser():
    with shared_browser_scope() as b:
        yield b


def _sessions(pinned_index=None):
    rows = fake_sessions(6, base_ts=int(time.time()))
    for s in rows:
        s["hold"] = "done"
    if pinned_index is not None:
        rows[pinned_index]["pinned"] = True
    return rows


def _ids(rows):
    return [r["id"] for r in rows]


CARD_ORDER = "() => [...document.querySelectorAll('#workforce-grid .wf-card')].map(c => c.dataset.sid)"
ROW_ORDER = "() => [...document.querySelectorAll('#session-list .session-item')].map(c => c.dataset.sid)"
# The boot splash never lifts in this harness, and the health checks' blocker
# shows (no server answers its probe); both would take a real pointer click.
NO_SPLASH = "() => { for (const id of ['vn-splash', 'health-blocker']) { const s = document.getElementById(id); if (s) s.remove(); } }"


def _pin_route(log, status=200):
    def handler(req):
        log.append(json.loads(req.post_data or "{}"))
        return status, ({"ok": True} if status == 200 else {"error": "nope"})
    return {"/api/session-pin/": handler}


@pytest.mark.slow
def test_a_pinned_session_comes_first_in_cards_and_list_under_every_sort(browser):
    rows = _sessions(pinned_index=4)
    ids = _ids(rows)
    for sort in ("recent", "status", "name"):
        extra = "localStorage.setItem('wfSort', %r); localStorage.setItem('sortMode', %r);" % (
            sort, "date" if sort == "recent" else sort)
        app = FakeApp(browser, rows, init=init_script(extra=extra))
        try:
            order = app.eval(CARD_ORDER)
            assert order[0] == ids[4], (sort, order)
            assert app.eval("(id) => !!document.querySelector(`.wf-card[data-sid='${id}'].is-pinned .vn-pin.on`)", ids[4])
            assert app.eval("() => document.querySelectorAll('.wf-card .vn-pin.on').length") == 1
            assert not app.errors, app.errors
        finally:
            app.close()
    app = FakeApp(browser, rows, init=init_script(display_mode="list"))
    try:
        order = app.eval(ROW_ORDER)
        assert order[0] == ids[4] and order[1:] == [i for i in ids if i != ids[4]], order
        assert app.eval("(id) => !!document.querySelector(`.session-item[data-sid='${id}'].is-pinned .vn-pin.on`)", ids[4])
    finally:
        app.close()


@pytest.mark.slow
def test_the_pin_on_a_card_pins_and_unpins_without_opening_it(browser):
    rows = _sessions()
    ids = _ids(rows)
    posts = []
    app = FakeApp(browser, rows, extra_routes=_pin_route(posts))
    try:
        app.eval(NO_SPLASH)
        card = f".wf-card[data-sid='{ids[3]}']"
        pin = card + " .vn-pin"
        rest = f".wf-card[data-sid='{ids[5]}'] .vn-pin"
        op = "(s) => +getComputedStyle(document.querySelector(s)).opacity"
        assert app.eval(op, pin) == 0                      # nothing at rest
        app.page.hover(card)
        app.page.wait_for_function(f"({op})({json.dumps(pin)}) > 0.3", timeout=2000)   # faint grey on hover
        assert app.eval(op, rest) == 0
        app.page.click(pin)
        app.page.wait_for_function(f"({CARD_ORDER})()[0] === {json.dumps(ids[3])}", timeout=3000)
        assert posts == [{"pinned": True, "project": PROJECT}]
        app.page.mouse.move(2, 2)
        pinned = app.eval("(s) => { const c = getComputedStyle(document.querySelector(s));"
                          " return [c.opacity, c.color, getComputedStyle(document.documentElement).getPropertyValue('--accent')]; }", pin)
        assert float(pinned[0]) == 1
        time.sleep(0.5)   # past singleOrDouble's double-click window: the session was not opened
        assert app.eval("() => typeof activeId === 'undefined' ? null : activeId") in (None, "")
        assert "chat=" not in app.page.url
        app.page.hover(card)
        app.page.click(pin)
        app.page.wait_for_function(f"({CARD_ORDER})()[0] === {json.dumps(ids[0])}", timeout=3000)
        assert app.eval(CARD_ORDER) == ids
        assert posts[-1] == {"pinned": False, "project": PROJECT}
        assert not app.errors, app.errors
    finally:
        app.close()


@pytest.mark.slow
def test_pinned_is_purple(browser):
    rows = _sessions(pinned_index=2)
    app = FakeApp(browser, rows)
    try:
        color, accent = app.eval("""(id) => {
            const p = document.querySelector(`.wf-card[data-sid='${id}'] .vn-pin.on`);
            const probe = document.createElement('span'); probe.style.color = 'var(--accent)'; document.body.appendChild(probe);
            const a = getComputedStyle(probe).color; probe.remove();
            return [getComputedStyle(p).color, a]; }""", rows[2]["id"])
        assert color == accent, (color, accent)
    finally:
        app.close()


@pytest.mark.slow
def test_right_click_menu_pins_and_unpins(browser):
    rows = _sessions()
    ids = _ids(rows)
    posts = []
    app = FakeApp(browser, rows, extra_routes=_pin_route(posts))
    try:
        app.eval(NO_SPLASH)
        card = f".wf-card[data-sid='{ids[2]}']"
        items = "() => [...document.querySelectorAll('.session-ctx-menu .ws-ctx-item')].map(e => e.textContent.trim())"
        app.page.click(card, button="right")
        labels = app.eval(items)
        assert "Pin to top" in labels and labels.index("Pin to top") == labels.index("Rename") + 1, labels
        app.page.click(".session-ctx-menu .ws-ctx-item:has-text('Pin to top')")
        app.page.wait_for_function(f"({CARD_ORDER})()[0] === {json.dumps(ids[2])}", timeout=3000)
        assert posts == [{"pinned": True, "project": PROJECT}]
        app.page.click(card, button="right")
        assert "Unpin" in app.eval(items) and "Pin to top" not in app.eval(items)
        app.page.click(".session-ctx-menu .ws-ctx-item:has-text('Unpin')")
        app.page.wait_for_function(f"({CARD_ORDER})()[0] === {json.dumps(ids[0])}", timeout=3000)
        assert posts[-1] == {"pinned": False, "project": PROJECT}
        assert not app.errors, app.errors
    finally:
        app.close()


@pytest.mark.slow
def test_a_pin_from_another_device_resorts_this_sidebar(browser):
    rows = _sessions()
    ids = _ids(rows)
    app = FakeApp(browser, rows)
    try:
        app.push("session_pin", {"session_id": ids[5], "pinned": True, "project": PROJECT})
        app.page.wait_for_function(f"({CARD_ORDER})()[0] === {json.dumps(ids[5])}", timeout=3000)
        app.push("session_pin", {"session_id": ids[5], "pinned": False, "project": PROJECT})
        app.page.wait_for_function(f"({CARD_ORDER})()[5] === {json.dumps(ids[5])}", timeout=3000)
        assert not app.errors, app.errors
    finally:
        app.close()


@pytest.mark.slow
def test_a_pin_the_server_did_not_keep_is_taken_back(browser):
    rows = _sessions()
    ids = _ids(rows)
    posts = []
    app = FakeApp(browser, rows, extra_routes=_pin_route(posts, status=500))
    try:
        app.eval("(id) => { toggleSessionPin(id); }", ids[4])   # not awaited: the save is still out
        assert app.eval(CARD_ORDER)[0] == ids[4]          # at once
        app.page.wait_for_function(f"({CARD_ORDER})()[4] === {json.dumps(ids[4])}", timeout=5000)
        assert len(posts) == 2                              # one retry
        assert "pin" in app.eval("() => document.getElementById('toast').textContent").lower()
    finally:
        app.close()


def _ranked(ranks: dict):
    """Six Done sessions; ``ranks`` maps an index to a saved pin place."""
    rows = _sessions()
    for i, r in ranks.items():
        rows[i]["pinned"] = True
        rows[i]["pin_rank"] = r
    return rows


def _order_route(log, status=200):
    def handler(req):
        log.append(json.loads(req.post_data or "{}"))
        return status, ({"ok": True} if status == 200 else {"error": "nope"})
    return {"/api/session-pin-order": handler}


def _center(page, sel):
    b = page.locator(sel).bounding_box()
    return b["x"] + b["width"] / 2, b["y"] + b["height"] / 2


def _lift(page, sel):
    """Press on `sel` and move a little: the drag has started, nothing moved."""
    x, y = _center(page, sel)
    page.mouse.move(x, y)
    page.mouse.down()
    page.mouse.move(x + 8, y + 4, steps=3)


def _over(page, sel, steps=14):
    """Move the held mouse to the middle of `sel`, then let the slide finish."""
    x, y = _center(page, sel)
    page.mouse.move(x, y, steps=steps)
    page.wait_for_timeout(260)
    return x, y


SETTLED = "() => !document.querySelector('.vn-pin-lifted, .vn-pin-settling') && !document.body.classList.contains('vn-pin-dragging')"


@pytest.mark.slow
def test_pinned_sessions_follow_their_saved_order(browser):
    rows = _ranked({1: 1, 4: 0})
    ids = _ids(rows)
    app = FakeApp(browser, rows)
    try:
        assert app.eval(CARD_ORDER)[:3] == [ids[4], ids[1], ids[0]]
        drag = app.eval("() => [...document.querySelectorAll('#workforce-grid .wf-card')].map(c => c.hasAttribute('data-pin-drag'))")
        assert drag[:2] == [True, True] and not any(drag[2:]), drag   # only pinned ones move
        # No browser drag: the card itself follows the mouse.
        assert app.eval("() => document.querySelectorAll('[draggable=true]').length") == 0
    finally:
        app.close()


@pytest.mark.slow
def test_pinned_cards_reorder_live_while_dragging_and_save_on_release(browser):
    """The owner: 'literally reorder as you move it like ios'."""
    rows = _ranked({1: 0, 3: 1, 5: 2})
    ids = _ids(rows)
    posts = []
    app = FakeApp(browser, rows, extra_routes=_order_route(posts))
    try:
        app.eval(NO_SPLASH)
        assert app.eval(CARD_ORDER)[:3] == [ids[1], ids[3], ids[5]]
        _lift(app.page, f".wf-card[data-sid='{ids[5]}']")
        assert app.eval("(id) => document.querySelector(`.wf-card[data-sid='${id}']`).classList.contains('vn-pin-lifted')", ids[5])
        mouse = _over(app.page, f".wf-card[data-sid='{ids[1]}']")
        # Mouse still down: the others already moved out of its way.
        assert app.eval(CARD_ORDER)[:4] == [ids[5], ids[1], ids[3], ids[0]]
        assert posts == []
        # The lifted card stays under the mouse (it was picked up by its middle).
        lifted = app.eval("(id) => { const r = document.querySelector(`.wf-card[data-sid='${id}']`).getBoundingClientRect(); return [r.x + r.width / 2, r.y + r.height / 2]; }", ids[5])
        assert abs(lifted[0] - mouse[0]) < 3 and abs(lifted[1] - mouse[1]) < 3, (lifted, mouse)
        assert not app.eval("() => !!document.querySelector('.session-ctx-menu')")   # no press-and-hold menu
        app.page.mouse.up()
        app.page.wait_for_function(SETTLED, timeout=3000)
        assert posts == [{"order": [ids[5], ids[1], ids[3]], "project": PROJECT}]
        assert app.eval(CARD_ORDER)[:4] == [ids[5], ids[1], ids[3], ids[0]]
        time.sleep(0.4)
        assert app.eval("() => typeof activeId === 'undefined' ? null : activeId") in (None, "")   # not opened
        # Back to the end, passing over the middle one on the way.
        _lift(app.page, f".wf-card[data-sid='{ids[5]}']")
        _over(app.page, f".wf-card[data-sid='{ids[3]}']", steps=20)
        assert app.eval(CARD_ORDER)[:3] == [ids[1], ids[3], ids[5]]
        app.page.mouse.up()
        app.page.wait_for_function(SETTLED, timeout=3000)
        assert posts[-1]["order"] == [ids[1], ids[3], ids[5]]
        assert not app.errors, app.errors
    finally:
        app.close()


@pytest.mark.slow
def test_holding_a_lifted_card_still_never_opens_the_menu(browser):
    """The drag starts 5px in, inside the press-and-hold menu's 20px slop:
    without the `vn:press-cancel` hook (mobile.js) the menu opens at 500ms."""
    rows = _ranked({1: 0, 3: 1})
    ids = _ids(rows)
    app = FakeApp(browser, rows)
    try:
        app.eval(NO_SPLASH)
        _lift(app.page, f".wf-card[data-sid='{ids[3]}']")
        app.page.wait_for_timeout(800)
        assert not app.eval("() => !!document.querySelector('.session-ctx-menu')")
        # Nor the cards' hover tooltip, carried across the others.
        _over(app.page, f".wf-card[data-sid='{ids[1]}']")
        app.page.wait_for_timeout(900)
        assert not app.eval("() => !!document.querySelector('.vn-tip.visible')")
        app.page.mouse.up()
    finally:
        app.close()


@pytest.mark.slow
def test_a_plain_click_on_a_pinned_card_still_opens_it(browser):
    rows = _ranked({1: 0})
    ids = _ids(rows)
    app = FakeApp(browser, rows)
    try:
        app.eval(NO_SPLASH)
        app.eval("() => { window.__opened = []; const o = window.openInGUI; window.openInGUI = (id, ...a) => { window.__opened.push(id); return Promise.resolve(); }; }")
        app.page.click(f".wf-card[data-sid='{ids[1]}']")
        app.page.wait_for_function(f"window.__opened.includes({json.dumps(ids[1])}) || (typeof activeId !== 'undefined' && activeId === {json.dumps(ids[1])})", timeout=3000)
    finally:
        app.close()


@pytest.mark.slow
def test_pinned_rows_reorder_live_in_the_list(browser):
    rows = _ranked({0: 0, 2: 1, 4: 2})
    ids = _ids(rows)
    posts = []
    app = FakeApp(browser, rows, init=init_script(display_mode="list"), extra_routes=_order_route(posts))
    try:
        app.eval(NO_SPLASH)
        _lift(app.page, f".session-item[data-sid='{ids[0]}'] .session-col-date")
        _over(app.page, f".session-item[data-sid='{ids[2]}']")
        assert app.eval(ROW_ORDER)[:3] == [ids[2], ids[0], ids[4]]
        _over(app.page, f".session-item[data-sid='{ids[4]}']")
        assert app.eval(ROW_ORDER)[:3] == [ids[2], ids[4], ids[0]]
        app.page.mouse.up()
        app.page.wait_for_function(SETTLED, timeout=3000)
        assert posts == [{"order": [ids[2], ids[4], ids[0]], "project": PROJECT}]
        assert not app.errors, app.errors
    finally:
        app.close()


@pytest.mark.slow
def test_escape_puts_it_back_and_saves_nothing(browser):
    rows = _ranked({0: 0, 1: 1})
    ids = _ids(rows)
    posts = []
    app = FakeApp(browser, rows, extra_routes=_order_route(posts))
    try:
        app.eval(NO_SPLASH)
        app.eval("() => { window.__opened = []; window.openInGUI = (id) => { window.__opened.push(id); return Promise.resolve(); }; }")
        _lift(app.page, f".wf-card[data-sid='{ids[1]}']")
        _over(app.page, f".wf-card[data-sid='{ids[0]}']")
        assert app.eval(CARD_ORDER)[:2] == [ids[1], ids[0]]
        app.page.keyboard.press("Escape")
        app.page.wait_for_function(SETTLED, timeout=3000)
        app.page.wait_for_function(f"({CARD_ORDER})()[0] === {json.dumps(ids[0])}", timeout=3000)
        app.page.mouse.up()
        time.sleep(0.4)
        assert posts == [] and app.eval("() => window.__opened") == []
    finally:
        app.close()


@pytest.mark.slow
def test_an_unpinned_session_does_not_move(browser):
    rows = _ranked({0: 0, 1: 1})
    ids = _ids(rows)
    posts = []
    app = FakeApp(browser, rows, extra_routes=_order_route(posts))
    try:
        app.eval(NO_SPLASH)
        _lift(app.page, f".wf-card[data-sid='{ids[0]}']")
        _over(app.page, f".wf-card[data-sid='{ids[4]}']")
        assert app.eval(CARD_ORDER) == ids
        app.page.mouse.up()
        app.page.wait_for_function(SETTLED, timeout=3000)
        assert app.eval(CARD_ORDER) == ids and posts == []
        # An unpinned card is not picked up at all.
        _lift(app.page, f".wf-card[data-sid='{ids[3]}']")
        assert app.eval("() => !document.querySelector('.vn-pin-lifted')")
        app.page.mouse.up()
    finally:
        app.close()


@pytest.mark.slow
def test_an_order_the_server_did_not_keep_is_taken_back(browser):
    rows = _ranked({0: 0, 1: 1})
    ids = _ids(rows)
    posts = []
    app = FakeApp(browser, rows, extra_routes=_order_route(posts, status=500))
    try:
        app.eval("([a, b]) => { movePinnedSession(a, b, false); }", [ids[1], ids[0]])
        assert app.eval(CARD_ORDER)[:2] == [ids[1], ids[0]]      # at once
        app.page.wait_for_function(f"({CARD_ORDER})()[0] === {json.dumps(ids[0])}", timeout=5000)
        assert len(posts) == 2
        assert "order" in app.eval("() => document.getElementById('toast').textContent").lower()
    finally:
        app.close()


@pytest.mark.slow
def test_an_order_from_another_device_resorts_this_sidebar(browser):
    rows = _ranked({0: 0, 1: 1, 2: 2})
    ids = _ids(rows)
    app = FakeApp(browser, rows)
    try:
        app.push("session_pin_order", {"order": [ids[2], ids[0], ids[1]], "project": PROJECT})
        app.page.wait_for_function(f"({CARD_ORDER})()[0] === {json.dumps(ids[2])}", timeout=3000)
        assert app.eval(CARD_ORDER)[:3] == [ids[2], ids[0], ids[1]]
        assert not app.errors, app.errors
    finally:
        app.close()


@pytest.mark.slow
def test_a_touch_screen_shows_only_pinned_pins(browser):
    rows = _sessions(pinned_index=1)
    app = FakeApp(browser, rows, viewport={"width": 390, "height": 844},
                  context={"is_mobile": True, "has_touch": True})
    try:
        if not app.eval("() => matchMedia('(hover: none)').matches"):
            pytest.skip("this Chromium does not emulate a touch-only screen")
        shown = app.eval("""() => [...document.querySelectorAll('#workforce-grid .wf-card')].map(c =>
            [c.dataset.sid, getComputedStyle(c.querySelector('.vn-pin')).display !== 'none'])""")
        assert shown[0] == [rows[1]["id"], True]
        assert all(not vis for _sid, vis in shown[1:]), shown
        # Press-and-hold is the menu there: nothing is draggable.
        assert app.eval("() => document.querySelectorAll('[data-pin-drag]').length") == 0
    finally:
        app.close()
