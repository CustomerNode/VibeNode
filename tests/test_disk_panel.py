"""The usage panel's Disk section in the real page (headless Chromium, only
the network faked): the fourth column, the biggest sessions with Delete, the
cap slider, Delete old sessions and Empty Trash."""

from __future__ import annotations

import json
import time

import pytest

from tests._browser import shared_browser_scope
from tests._fake_app import FakeApp, fake_sessions, init_script

GB = 1 << 30
DISK = {"known": True, "total": 1800 * GB, "used": 737 * GB, "free": 1063 * GB, "percent": 41}
SHARE = {"sessions": int(1.4 * GB), "trash": 9 << 20, "other": 280 << 20}
BIG = [
    {"id": "aaaa1111", "project": "-home-x-alpha", "name": "Release fix", "bytes": 300 << 20,
     "last_active": time.time() - 12 * 86400},
    {"id": "bbbb2222", "project": "-home-x-beta", "name": "", "bytes": 120 << 20,
     "last_active": time.time() - 3600},
]


@pytest.fixture(scope="module")
def browser():
    with shared_browser_scope() as b:
        yield b


def _app(browser, posts):
    def disk(req):
        if req.method == "POST":
            body = json.loads(req.post_data or "{}")
            posts.append((req.url.split("/api/disk")[1], body))
            if "/delete" in req.url:
                if body.get("dry_run"):
                    return 200, {"ok": True, "dry_run": True, "count": 7, "bytes": 90 << 20}
                return 200, {"ok": True, "deleted": len(body.get("ids") or []) or 7,
                             "trashed": len(body.get("ids") or []) or 7, "removed": 0, "freed": 0}
            if "/cap" in req.url:
                return 200, {"ok": True, "cap": body.get("bytes") or 0}
            if "/empty-trash" in req.url:
                return 200, {"ok": True, "freed": 9 << 20}
        return 200, {"ok": True, "disk": DISK, "cap": 0, "share": SHARE, "biggest": BIG}
    return FakeApp(browser, fake_sessions(2), init=init_script("sessions", "grid"),
                   extra_routes={"/api/disk": disk})


def _open(app):
    # The panel anchors to the bar's status control; stand one in at the bottom.
    app.eval("""() => { const t = document.createElement('div'); t.className = 'vn-status';
        t.style.cssText = 'position:fixed;left:20px;bottom:20px;width:80px;height:20px';
        document.body.appendChild(t); _toggleStatusPanel(t); }""")
    app.page.wait_for_selector("#vn-status-panel [data-tile=disk]")
    app.page.click("#vn-status-panel [data-tile=disk]")
    app.page.wait_for_selector("#vn-status-panel .vsp-srow")


def _text(app):
    return app.eval("() => document.getElementById('vn-status-panel').innerText")


def test_disk_column_and_section(browser):
    app = _app(browser, [])
    try:
        app.page.wait_for_function("() => window._disk && window._disk.disk")
        assert app.eval("() => _usageLimitsPillHtml()").count("ulp-disk") == 1
        _open(app)
        t = _text(app)
        # First bar: what VibeNode uses of its allotment (no cap: all it could have).
        assert "VibeNode uses" in t and "1.4 GB of 1.0 TB allotted" in t and "Trash 9 MB" in t
        # Second bar: the allotment of what is available (free + what it holds).
        assert "Allotted to VibeNode" in t and "All available" in t
        assert "1.0 TB available · disk 41% full" in t
        assert "Empty Trash (9 MB)" in t
        assert "Release fix" in t and "300 MB" in t and "Last active 12 d ago" in t
        assert "Session bbbb2222" in t                    # no name: its id
        assert not app.errors
    finally:
        app.close()


def test_first_bar_is_use_of_the_allotment(browser):
    posts = []
    app = _app(browser, posts)
    try:
        _open(app)
        app.eval("() => { window._disk.cap = 2 * 1073741824; _renderStatusPanel(); }")
        t = _text(app)
        assert "1.4 GB of 2.0 GB allotted" in t and "70%" in t
        assert "2.0 GB · 0% of available" not in t and "2.0 GB · 1% of available" in t
        assert app.eval("() => _diskColumn().tip") == "Disk: VibeNode uses 1.4 GB of 2.0 GB allotted"
    finally:
        app.close()


def test_delete_one(browser):
    posts = []
    app = _app(browser, posts)
    try:
        _open(app)
        app.page.click("[data-act=disk-one][data-id=aaaa1111]")
        app.page.wait_for_selector(".vsp-srow.gone")
        assert posts[-1] == ("/delete", {"ids": [{"id": "aaaa1111", "project": "-home-x-alpha"}]})
        assert "1 moved to the Trash" in _text(app)
    finally:
        app.close()


def test_cap_slider_is_to_scale_saves_and_asks_when_it_would_delete(browser):
    posts = []
    app = _app(browser, posts)
    try:
        _open(app)
        dialogs = []
        app.page.on("dialog", lambda d: (dialogs.append(d.message), d.accept()))
        # Drawn to scale: the middle of the track is half of what is available.
        half = app.eval("() => _diskPosToBytes(500)")
        assert abs(half - (DISK["free"] + SHARE["sessions"] + SHARE["trash"]) / 2) < 2 * GB
        app.eval("""() => { const r = document.getElementById('vsp-cap');
            r.value = String(_diskBytesToPos(1073741824));
            r.dispatchEvent(new Event('input', {bubbles: true}));
            r.dispatchEvent(new Event('change', {bubbles: true})); }""")
        # One step on a 1 TB scale is about 1 GB.
        assert app.eval("() => document.getElementById('vsp-cap-val').textContent") in (
            "1.0 GB · 1% of available", "1.1 GB · 1% of available")
        assert "deleted for good until they fit" in app.eval("() => document.getElementById('vsp-cap-note').textContent")
        app.page.wait_for_function("() => window._disk.cap > 0")
        assert dialogs and "deleted for good" in dialogs[0]
        assert posts[-1][0] == "/cap" and GB <= posts[-1][1]["bytes"] < 1.2 * GB
    finally:
        app.close()


def test_delete_old_previews_then_moves_to_trash(browser):
    posts = []
    app = _app(browser, posts)
    try:
        _open(app)
        app.page.click("[data-act=disk-delopen]")
        app.page.wait_for_function("() => document.getElementById('vsp-del-go').textContent === 'Delete 7 sessions (90 MB)'")
        assert ("/delete", {"created_days": None, "inactive_days": 14, "dry_run": True}) in posts
        app.page.on("dialog", lambda d: d.accept())
        app.page.click("#vsp-del-go")
        app.page.wait_for_function("() => document.getElementById('vn-status-panel').innerText.includes('7 moved to the Trash')")
        assert ("/delete", {"created_days": None, "inactive_days": 14}) in posts
    finally:
        app.close()


def test_empty_trash_asks_first(browser):
    posts = []
    app = _app(browser, posts)
    try:
        _open(app)
        app.page.on("dialog", lambda d: d.accept())
        app.page.click("[data-act=disk-trash]")
        app.page.wait_for_function("() => window._disk.msg.indexOf('Trash emptied') === 0")
        assert ("/empty-trash", {}) in posts
    finally:
        app.close()


def test_the_panel_is_a_modal_with_tiles(browser):
    app = _app(browser, [])
    try:
        app.eval("""() => { const t = document.createElement('div'); t.className = 'vn-status';
            document.body.appendChild(t); _toggleStatusPanel(t); }""")
        app.page.wait_for_selector("#vn-status-panel.vn-status-modal .vsm-tiles")
        assert app.eval("() => !!document.getElementById('vn-status-scrim')")
        tiles = app.eval("() => [...document.querySelectorAll('.vsm-tile')].map(t => t.dataset.tile)")
        assert tiles == ["model", "weekly", "five", "disk"]
        # Opens on the model tile: chips shown, disk not.
        assert app.eval("() => !!document.querySelector('#vn-status-panel .vsp-think')")
        assert not app.eval("() => !!document.querySelector('#vn-status-panel .vsp-cap')")
        # Disk tile: its numbers on the tile, its details below.
        assert "1.4 GB of 1.0 TB" in app.eval("() => document.querySelector('[data-tile=disk]').innerText")
        app.page.click("[data-tile=disk]")
        app.page.wait_for_selector("#vn-status-panel .vsp-cap")
        assert app.eval("() => document.querySelector('[data-tile=disk]').classList.contains('on')")
        # Centered on a computer.
        box = app.page.locator("#vn-status-panel").bounding_box()
        vw = app.eval("() => innerWidth")
        assert abs(box["x"] + box["width"] / 2 - vw / 2) < 4
        # Close button, then the backdrop, then Escape all close it.
        app.page.click(".vsm-x")
        assert not app.eval("() => !!document.getElementById('vn-status-panel') || !!document.getElementById('vn-status-scrim')")
        app.eval("() => _toggleStatusPanel(document.querySelector('.vn-status'))")
        app.page.mouse.click(5, 5)
        assert not app.eval("() => !!document.getElementById('vn-status-panel')")
        app.eval("() => _toggleStatusPanel(document.querySelector('.vn-status'))")
        app.page.keyboard.press("Escape")
        assert not app.eval("() => !!document.getElementById('vn-status-panel')")
        assert not app.errors
    finally:
        app.close()


def test_on_a_phone_it_is_a_bottom_sheet(browser):
    app = FakeApp(browser, fake_sessions(2), init=init_script("sessions", "grid"),
                  viewport={"width": 390, "height": 844},
                  extra_routes={"/api/disk": lambda r: (200, {"ok": True, "disk": DISK, "cap": 0,
                                                              "share": SHARE, "biggest": BIG})})
    try:
        app.eval("""() => { const t = document.createElement('div'); t.className = 'vn-status';
            document.body.appendChild(t); _toggleStatusPanel(t); }""")
        app.page.wait_for_selector("#vn-status-panel .vsm-tiles")
        app.page.wait_for_timeout(300)  # its slide-up animation
        box = app.page.locator("#vn-status-panel").bounding_box()
        assert box["x"] == 0 and abs(box["y"] + box["height"] - 844) < 2 and box["width"] == 390
        assert app.eval("() => getComputedStyle(document.querySelector('.vsm-tiles')).gridTemplateColumns.split(' ').length") == 2
    finally:
        app.close()
