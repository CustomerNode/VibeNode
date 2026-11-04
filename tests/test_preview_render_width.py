"""
The preview renderer must lay a page out at the width it was asked for.

Headless Chrome's ``--window-size`` has a 500px floor, so a 390px "phone"
request used to come back as a 500px desktop layout cropped to 390: every phone
screenshot lied. The route now renders through Playwright device emulation and
only falls back to the Chrome CLI (clamped, and labelled in ``note``) when
Playwright or its browser is missing.
"""

import functools
import struct
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from web import create_app  # noqa: E402
from web.routes import preview_api  # noqa: E402


def _png_size(path: Path):
    with open(path, "rb") as f:
        head = f.read(24)
    assert head[:8] == b"\x89PNG\r\n\x1a\n", "not a PNG"
    w, h = struct.unpack(">II", head[16:24])
    return w, h


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(preview_api, "_PREVIEW_DIR", tmp_path)
    app = create_app(testing=True)
    return app.test_client()


# A page that reports its own layout width; the PNG dimensions are the proof.
_PAGE = ("data:text/html,<html><body style='margin:0;background:%23fff'>"
         "<div id=w style='font:40px monospace'></div>"
         "<script>document.getElementById('w').textContent=innerWidth</script>"
         "</body></html>")


@functools.lru_cache(maxsize=1)
def _playwright_chromium_available():
    try:
        from playwright.sync_api import sync_playwright
    except Exception:
        return False
    try:
        with sync_playwright() as p:
            p.chromium.launch(headless=True).close()
        return True
    except Exception:
        return False


@pytest.fixture
def chromium():
    # Asked when a test needs it, not at import: a driver and a browser
    # launched during collection cost every worker of every run, whatever
    # that run selects.
    if not _playwright_chromium_available():
        pytest.skip("Playwright Chromium not installed")


def test_phone_width_renders_at_phone_width(client, chromium):
    r = client.post("/api/preview/render",
                    json={"url": _PAGE, "width": 390, "height": 300, "wait_ms": 0})
    body = r.get_json()
    assert body["ok"], body
    assert body["renderer"] == "playwright"
    assert body["mobile"] is True
    assert body["viewport"] == {"width": 390, "height": 300}
    assert "note" not in body
    dsf = body["device_scale_factor"]
    w, h = _png_size(Path(body["file"]))
    # Width is the claim; the page laid out at 390 CSS px. Height can be off by
    # a pixel from device-emulation rounding.
    assert w == 390 * dsf
    assert abs(h - 300 * dsf) <= 2


def test_desktop_width_is_not_emulated_as_a_phone(client, chromium):
    r = client.post("/api/preview/render",
                    json={"url": _PAGE, "width": 1280, "height": 400, "wait_ms": 0})
    body = r.get_json()
    assert body["ok"], body
    assert body["mobile"] is False
    assert _png_size(Path(body["file"])) == (1280, 400)


def test_chrome_fallback_clamps_and_says_so(client, monkeypatch, tmp_path):
    """Without Playwright the CLI path runs, cannot go under 500px, and must
    say so rather than silently returning a cropped desktop layout."""
    monkeypatch.setattr(preview_api, "_render_with_playwright",
                        lambda *a, **k: None)
    monkeypatch.setattr(preview_api, "_find_chrome", lambda: "chrome-stub")
    seen = {}

    def fake_run(cmd, **kw):
        seen["cmd"] = cmd
        out = next(a for a in cmd if a.startswith("--screenshot="))[len("--screenshot="):]
        Path(out).write_bytes(b"\x89PNG\r\n\x1a\n" + b"\0" * 16)

        class R:
            returncode = 0
            stderr = ""
        return R()

    monkeypatch.setattr(preview_api.subprocess, "run", fake_run)
    r = client.post("/api/preview/render",
                    json={"url": "http://localhost:1/", "width": 390, "height": 844})
    body = r.get_json()
    assert body["ok"], body
    assert body["renderer"] == "chrome"
    assert "--window-size=500,844" in seen["cmd"]
    assert body["viewport"]["width"] == 500
    assert "clamped" in body["note"]


def test_chrome_fallback_leaves_desktop_widths_alone(client, monkeypatch):
    monkeypatch.setattr(preview_api, "_render_with_playwright",
                        lambda *a, **k: None)
    monkeypatch.setattr(preview_api, "_find_chrome", lambda: "chrome-stub")

    def fake_run(cmd, **kw):
        out = next(a for a in cmd if a.startswith("--screenshot="))[len("--screenshot="):]
        Path(out).write_bytes(b"\x89PNG\r\n\x1a\n" + b"\0" * 16)

        class R:
            returncode = 0
            stderr = ""
        return R()

    monkeypatch.setattr(preview_api.subprocess, "run", fake_run)
    r = client.post("/api/preview/render",
                    json={"url": "http://localhost:1/", "width": 1024, "height": 768})
    body = r.get_json()
    assert body["ok"] and "note" not in body
    assert body["viewport"] == {"width": 1024, "height": 768}


def test_playwright_page_failure_is_a_clear_500(client, monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("net::ERR_CONNECTION_REFUSED at http://localhost:1/")
    monkeypatch.setattr(preview_api, "_render_with_playwright", boom)
    r = client.post("/api/preview/render", json={"url": "http://localhost:1/"})
    assert r.status_code == 500
    assert "ERR_CONNECTION_REFUSED" in r.get_json()["error"]


def test_a_render_whose_browser_was_killed_from_outside_is_tried_once_more(monkeypatch, tmp_path):
    calls = []

    def once(*a):
        calls.append(a)
        if len(calls) == 1:
            raise RuntimeError("BrowserContext.new_page: Target page, context or browser has been closed")
        return {"renderer": "playwright"}

    monkeypatch.setattr(preview_api, "_render_once", once)
    assert preview_api._render_with_playwright("http://x/", tmp_path / "a.png", 800, 600, 0) == \
        {"renderer": "playwright"}
    assert len(calls) == 2


def test_a_page_that_will_not_load_is_not_retried(monkeypatch, tmp_path):
    calls = []

    def once(*a):
        calls.append(a)
        raise RuntimeError("Page.goto: net::ERR_CONNECTION_REFUSED")

    monkeypatch.setattr(preview_api, "_render_once", once)
    with pytest.raises(RuntimeError, match="REFUSED"):
        preview_api._render_with_playwright("http://x/", tmp_path / "a.png", 800, 600, 0)
    assert len(calls) == 1
