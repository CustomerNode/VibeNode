"""Regression tests: the browser must be allowed to keep versioned assets.

index.html pulls 43 versioned JS/CSS files -- 2172 KB measured -- through
``versioned_static()``, which stamps each URL with the file's mtime. That is a
complete cache-busting scheme by itself: change a byte, the mtime moves, the
URL moves, the browser fetches the new file.

The response headers then threw all of it away. Every ``/static/`` response
carried ``no-store``, which forbids the browser from keeping *any* copy, so all
2172 KB were re-downloaded in full on every single page load. The versioning
and the headers were fighting and ``no-store`` won.

These tests pin the resolution: a ``?v=`` URL is immutable by construction and
must be cacheable; an un-versioned one must still revalidate, but with
``no-cache`` (304, empty body) rather than ``no-store`` (re-ship the file).

The safety interlock is the HTML branch: the SPA shell must NEVER be immutable,
because it is what tells the browser the new ``?v=``. If the shell were cached,
a stale shell would keep pointing at old asset URLs and a fix would never reach
the device -- which is exactly the failure the shell's own comment describes.
``test_html_shell_is_never_immutable`` is the guard on that, and it is the test
that matters most here.
"""

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture()
def client():
    from app import create_app
    application = create_app(testing=True)
    application.config["TESTING"] = True
    with application.test_client() as c:
        yield c


def _cc(resp) -> str:
    return resp.headers.get("Cache-Control", "")


# ---------------------------------------------------------------------------
# The regression
# ---------------------------------------------------------------------------

def test_versioned_asset_is_cacheable(client):
    """THE regression: ?v= assets were 'no-store' and re-downloaded every load."""
    resp = client.get("/static/js/app.js?v=1787856211")
    assert resp.status_code == 200
    cc = _cc(resp)
    assert "no-store" not in cc, (
        "no-store forbids the browser from keeping the file at all, so all "
        "2172 KB of versioned JS is re-fetched on every page load"
    )
    assert "immutable" in cc and "max-age=" in cc


def test_versioned_asset_drops_the_legacy_no_cache_pragmas(client):
    """A stale Pragma/Expires would override Cache-Control on older clients."""
    resp = client.get("/static/js/app.js?v=1787856211")
    assert "Pragma" not in resp.headers
    assert "Expires" not in resp.headers


def test_unversioned_asset_still_revalidates(client):
    """Vendored files carry no ?v=, so they must not be pinned."""
    resp = client.get("/static/js/app.js")
    cc = _cc(resp)
    assert "no-cache" in cc
    assert "immutable" not in cc and "max-age=31536000" not in cc


def test_unversioned_asset_does_not_use_no_store(client):
    """no-cache revalidates and takes a 304; no-store re-ships the bytes."""
    assert "no-store" not in _cc(client.get("/static/js/app.js"))


def test_html_shell_is_never_immutable(client):
    """The interlock that makes `immutable` safe for everything else.

    The shell is what carries the new ?v= URLs. Cache the shell and a fix can
    never reach the device, no matter how correct the asset versioning is.
    """
    resp = client.get("/")
    cc = _cc(resp)
    assert "immutable" not in cc
    assert "no-cache" in cc


def test_a_versioned_miss_is_not_cached_for_a_year(client):
    """A 404 wearing a ?v= must not be pinned into the cache."""
    resp = client.get("/static/js/does-not-exist.js?v=123")
    assert resp.status_code == 404
    assert "immutable" not in _cc(resp)


# ---------------------------------------------------------------------------
# Source guards -- the invariants the headers depend on
# ---------------------------------------------------------------------------

def test_versioned_static_still_stamps_mtime():
    """`immutable` is only safe while the URL is content-addressed.

    If versioned_static ever stops varying with the file's bytes, a year-long
    cache becomes a year-long stale asset.
    """
    src = (ROOT / "app" / "__init__.py").read_text(encoding="utf-8")
    body = src[src.index("def versioned_static("):src.index("return dict(versioned_static")]
    assert "getmtime" in body and "?v=" in body


def test_launcher_does_not_open_localhost():
    """`localhost` resolves to ::1 first, where nothing listens and the SYN is
    dropped rather than refused -- ~209ms of dead wait per connection, and
    werkzeug sends Connection: close so that is per asset, not per page.
    Measured 11340ms vs 2343ms for the 43-asset load.
    """
    src = (ROOT / "run.py").read_text(encoding="utf-8")
    m = re.search(r'url = f"http://([^:]+):\{_WEB_PORT\}"', src)
    assert m, "could not find the launcher URL in run.py"
    assert m.group(1) == "127.0.0.1", (
        "open_browser must use the IPv4 literal so there is no ::1 to stall on"
    )


def test_project_switch_is_not_padded_with_seconds_of_animation():
    """The overlay floor is a de-flicker guard, not a pacing device."""
    src = (ROOT / "static" / "js" / "app.js").read_text(encoding="utf-8")
    m = re.search(r"const _minDisplay = new Promise\(r => setTimeout\(r, (\d+)\)\)", src)
    assert m, "could not find the project-switch minimum-display timer"
    assert int(m.group(1)) <= 400, (
        "a long floor makes every switch wait on the animation after the data "
        "is already there"
    )
