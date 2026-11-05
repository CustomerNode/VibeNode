"""5-hour pacing in the usage window (real page, headless Chromium, only the
network faked): the 5-hour tile, the Linear-to-Off slider, and Reset."""

from __future__ import annotations

import json
import time

import pytest

from tests._browser import shared_browser_scope
from tests._fake_app import FakeApp, fake_sessions, init_script

STEPS = [1, 1.5, 2, 3, 5, 0]


@pytest.fixture(scope="module")
def browser():
    with shared_browser_scope() as b:
        yield b


def _app(browser, posts, state):
    def pace(req):
        if req.method == "POST":
            body = json.loads(req.post_data or "{}")
            posts.append((req.url.split("/api/pace-5h")[1], body, {k.lower(): v for k, v in dict(req.headers).items()}.get("x-vn-human")))
            if "/setting" in req.url:
                state.update(mult=body["mult"], active=False)
            if "/reset" in req.url:
                state.update(active=False, hit=False)
                return 200, dict(state, ok=True, resumed=["a1"])
        return 200, dict(state, ok=True)
    return FakeApp(browser, fake_sessions(2), init=init_script("sessions", "grid"),
                   extra_routes={"/api/pace-5h": pace})


def _open(app):
    app.eval("""() => { const t = document.createElement('div'); t.className = 'vn-status';
        document.body.appendChild(t); _toggleStatusPanel(t); }""")
    app.page.wait_for_function("() => window._pace && document.querySelector('[data-tile=five]')")


def _five(app):
    return app.eval("() => document.querySelector('[data-tile=five]').innerText")


def test_off_by_default_then_linear(browser):
    posts = []
    st = {"mult": 0, "steps": STEPS, "next_share": 3.0, "active": False, "now": time.time()}
    app = _app(browser, posts, st)
    try:
        _open(app)
        assert "Off" in _five(app) and "Pacing off" in _five(app)
        app.page.click("[data-tile=five]")
        app.page.wait_for_selector("#vsp-pace")
        assert app.eval("() => document.getElementById('vsp-pace').value") == "5"     # Off end
        assert "Linear" in app.eval("() => document.querySelector('.vsp-pace-scale').innerText")
        app.eval("""() => { const r = document.getElementById('vsp-pace'); r.value = '0';
            r.dispatchEvent(new Event('input', {bubbles: true}));
            r.dispatchEvent(new Event('change', {bubbles: true})); }""")
        assert "about 3.0% of the week per 5 hours" in app.eval("() => document.getElementById('vsp-pace-note').textContent")
        app.page.wait_for_function("() => window._pace.mult === 1")
        assert posts[-1][:2] == ("/setting", {"mult": 1})
        assert "Starts with your next message" in _five(app)
        assert not app.errors
    finally:
        app.close()


def test_hit_shows_on_the_tile_and_reset_is_sent_as_a_person(browser):
    posts = []
    st = {"mult": 1, "steps": STEPS, "next_share": 3.0, "active": True, "hit": True, "share": 3.0,
          "used": 3.2, "percent": 100, "reset_at": time.time() + 3600, "now": time.time()}
    app = _app(browser, posts, st)
    try:
        _open(app)
        assert "100%" in _five(app) and "Limit hit" in _five(app)
        assert app.eval("() => document.querySelector('[data-tile=five]').classList.contains('ulp-crit')")
        app.page.click("[data-tile=five]")
        app.page.wait_for_selector("[data-act=pace-reset]")
        assert "sessions wait, and continue at" in app.eval("() => document.getElementById('vn-status-panel').innerText")
        app.page.on("dialog", lambda d: d.accept())
        app.page.click("[data-act=pace-reset]")
        app.page.wait_for_function("() => window._pace.active === false")
        assert posts[-1][0] == "/reset" and posts[-1][2] == "reset"
    finally:
        app.close()
