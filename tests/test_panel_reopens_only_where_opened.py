"""A full-screen popup reopens only where the user opened it.

A popup must not reopen by itself on a session switch or a reload.
``?panel=decide:1`` means "the newest Decide button of whatever session is
open", and the app carries the query into every address it builds, so it could
open another session's card on a reload, on Back, or from a pasted link.  The
popup reopens only on the history entry last-card.js wrote when it was opened,
in the same chat.

Real Chromium (Playwright) on the real ``src/web/static/js/last-card.js``, served from
a temporary folder.
"""

import functools
import http.server
import json
import shutil
import threading
from pathlib import Path

import pytest

from tests._browser import shared_browser_scope

pytest.importorskip("playwright.sync_api")

ROOT = Path(__file__).resolve().parents[1]
CARD = {"type": "needs_you", "title": "Pick a look", "headline": "Pick a look", "context": "x",
        "needs": {"kind": "decision", "ask": "Which look?", "why": "y",
                  "replies": [{"key": "A", "label": "Keep it as it is", "reply": "a"},
                              {"key": "B", "label": "The new look here", "reply": "b"}]}}
PAGE = """<!doctype html><html data-theme="dark"><head><meta charset="utf-8">
<link rel="stylesheet" href="last-card.css"></head><body>
<div id="live-log" class="conversation"><div class="msg assistant"><div class="msg-body" id="c"></div></div></div>
<script src="last-card.js"></script>
<script>document.getElementById('c').innerHTML = VNLastCard.render(%s, {});</script></body></html>""" % json.dumps(CARD)


@pytest.fixture(scope="module")
def base(tmp_path_factory):
    d = tmp_path_factory.mktemp("panel")
    shutil.copy(ROOT / "src" / "web" / "static" / "js" / "last-card.js", d / "last-card.js")
    shutil.copy(ROOT / "src" / "web" / "static" / "css" / "last-card.css", d / "last-card.css")
    (d / "page.html").write_text(PAGE, encoding="utf-8")
    handler = functools.partial(http.server.SimpleHTTPRequestHandler, directory=str(d))
    handler.log_message = lambda *a: None
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=srv.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True).start()
    yield f"http://127.0.0.1:{srv.server_address[1]}/page.html"
    srv.shutdown()


@pytest.fixture(scope="module")
def browser():
    with shared_browser_scope() as b:
        yield b


@pytest.fixture()
def page(base, browser):
    ctx = browser.new_context()   # no history or storage carried from another test
    ctx.clock.install()           # the panel's restore retries run on _popup_after
    yield ctx.new_page()
    ctx.close()


def _open_popup(page):
    page.click(".lm-decide")
    page.wait_for_selector(".lm-dz", timeout=5000)


def _popup_after(page, ms=1500):
    page.wait_for_timeout(40)   # a real frame: popstate and reload work lands first
    page.clock.run_for(ms)      # every restore retry (300ms apart) in the window fires
    page.wait_for_timeout(40)
    return page.locator(".lm-dz").count() > 0


def test_a_reload_of_the_popup_he_opened_reopens_it(page, base):
    page.goto(base + "?chat=s1")
    _open_popup(page)
    assert "panel=decide" in page.url
    page.reload()
    assert _popup_after(page)


def test_another_session_carrying_the_address_opens_nothing(page, base):
    page.goto(base + "?chat=s1")
    _open_popup(page)
    # the app opens another session from the current address (?panel rides along)
    page.evaluate("""() => { const u = new URL(location.href); u.searchParams.set('chat', 's2');
                             history.pushState({ folder: null, chat: 's2' }, '', u); }""")
    page.reload()
    assert not _popup_after(page)
    assert "panel=" not in page.url


def test_a_pasted_link_or_fresh_load_opens_nothing(page, base):
    page.goto(base + "?chat=s1&panel=decide%3A1")
    assert not _popup_after(page)
    assert "panel=" not in page.url


def test_back_and_forward_still_close_and_reopen_it(page, base):
    page.goto(base + "?chat=s1")
    _open_popup(page)
    page.go_back()
    assert not _popup_after(page, 800)
    page.go_forward()
    assert _popup_after(page)
