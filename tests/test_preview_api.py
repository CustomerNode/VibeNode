"""Tests for the Mobile Visual Channel capture/serve backend (app/routes/preview_api.py).

Chrome rendering itself is not exercised here (it needs a real browser + is slow);
we cover the pure logic (validation, id-scoping, proxy URL rewriting) and the
asset-serving contract with a rendered PNG faked on disk.
"""

import uuid

import pytest

from app.routes import preview_api as P


# ---- pure helpers ---------------------------------------------------------

def test_clamp_bounds_and_defaults():
    assert P._clamp("999999", 200, 2000, 1024) == 2000      # over max
    assert P._clamp("1", 200, 2000, 1024) == 200            # under min
    assert P._clamp(None, 200, 2000, 1024) == 1024          # missing -> default
    assert P._clamp("bad", 200, 4000, 1400) == 1400         # non-numeric -> default
    assert P._clamp("512", 200, 2000, 1024) == 512          # in range


def test_proxy_rewrite_routes_localhost_and_relative_but_not_data():
    html = (
        '<img src="/static/logo.png">'
        '<a href="http://localhost:5173/page">x</a>'
        '<img src="data:image/png;base64,AAAA">'
        '<script src="app.js"></script>'
        '<a href="#anchor">y</a>'
    )
    out = P._rewrite_html(html, "http://localhost:5173/index.html")
    # relative + absolute resources are proxied
    assert "/api/preview/proxy?u=" in out
    assert out.count("/api/preview/proxy") >= 3
    # data: and #fragment are left alone
    assert 'src="data:image/png;base64,AAAA"' in out
    assert 'href="#anchor"' in out


# ---- render input validation ---------------------------------------------

@pytest.fixture
def client():
    from app import create_app
    app = create_app()
    app.config["TESTING"] = True
    return app.test_client()


def test_render_rejects_non_http_url(client):
    r = client.post("/api/preview/render", json={"url": "ftp://nope"})
    assert r.status_code == 400
    assert r.get_json()["ok"] is False


def test_render_requires_url(client):
    r = client.post("/api/preview/render", json={"name": "x"})
    assert r.status_code == 400


# ---- asset serving contract ----------------------------------------------

def test_asset_rejects_bad_id(client):
    # path traversal / non-hex ids never resolve to a file
    assert client.get("/api/preview/asset/..%2f..%2fsecret").status_code == 404
    assert client.get("/api/preview/asset/not-hex").status_code == 404


def test_asset_serves_rendered_png(client):
    P._PREVIEW_DIR.mkdir(parents=True, exist_ok=True)
    pid = uuid.uuid4().hex
    f = P._PREVIEW_DIR / (pid + ".png")
    # minimal valid PNG header bytes are enough for the serve path
    f.write_bytes(b"\x89PNG\r\n\x1a\n" + b"\x00" * 16)
    try:
        r = client.get("/api/preview/asset/" + pid)
        assert r.status_code == 200
        assert r.mimetype == "image/png"
        r.close()  # release the send_file handle (Windows locks the file otherwise)
    finally:
        try:
            f.unlink()
        except OSError:
            pass  # best-effort; a lingering handle is a test artifact, not a bug


def test_proxy_rejects_non_http(client):
    assert client.get("/api/preview/proxy?u=file:///etc/passwd").status_code == 400
