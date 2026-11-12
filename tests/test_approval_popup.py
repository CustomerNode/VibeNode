"""A release approval opens in a popup over the chat, not in a new tab.

A script's approval page is on a port of this machine; a session
writes its card for the phone, so the link is the preview proxy under the
Mobile Command address.  Opened on the computer that host differs from the
page's, so ``_reviewSrc`` found nothing it could frame and the row opened a new
tab.  ``_reviewSrc`` now reads what such a link points at.

The real ``src/web/static/js/last-card.js`` in headless Chromium, against a real local
server shaped like an approval page: links that answer it, nothing else.
"""
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import quote

import pytest

from tests._browser import shared_browser_scope

ROOT = Path(__file__).resolve().parents[1]
LAST_CARD = ROOT / "src" / "web" / "static" / "js" / "last-card.js"
CSS = ROOT / "src" / "web" / "static" / "css" / "last-card.css"
TS = "https://my-pc.tail0000.ts.net/api/preview/proxy?u="


class _Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_GET(self):
        s = self.server
        if self.path == "/chat":
            body = s.chat
        elif self.path == "/last-card.js":
            body = LAST_CARD.read_text(encoding="utf-8")
        elif self.path == "/last-card.css":
            body = CSS.read_text(encoding="utf-8")
        elif self.path.startswith("/answer"):
            s.answers.append(self.path)
            body = "<!doctype html><body id=done>Release authorized</body>"
        else:
            body = '<!doctype html><title>Approve</title><a id=go href="/answer?v=yes&t=n">Approve release</a>'
        kind = {"js": "text/javascript", "ss": "text/css"}.get(self.path[-2:], "text/html")
        self.send_response(200)
        self.send_header("Content-Type", kind)
        self.end_headers()
        self.wfile.write(body.encode("utf-8"))


@pytest.fixture()
def serve():
    servers = []

    def start():
        srv = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        srv.daemon_threads = True
        srv.answers, srv.chat = [], ""
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        servers.append(srv)
        return srv
    yield start
    for srv in servers:
        srv.shutdown()
        srv.server_close()


@pytest.fixture()
def browser():
    with shared_browser_scope() as b:
        yield b


def _chat(link):
    card = {"type": "needs_you", "title": "Release approval", "headline": "Approve the release.",
            "answer": "a", "explained": "b", "full_answer": "c",
            "needs": {"kind": "approve", "ask": "Approve the release.", "why": "w",
                      "look": [{"label": "Open the approval", "kind": "link", "url": link}]},
            "zoom": {"big": "b", "task": "t", "now": "n"}}
    return ("<!doctype html><html><head><link rel=stylesheet href=/last-card.css></head><body>"
            "<div id=live-log><div class='msg assistant'><div class=msg-body id=b></div></div></div>"
            "<script>var liveSessionId='s', sessionKinds={s:'idle'};</script><script src=/last-card.js></script>"
            f"<script>b.innerHTML=VNLastCard.render({json.dumps(card)},{{}});</script></body></html>")


def _tap_approval(browser, serve, link_for):
    """Open the chat with a card linking the page, tap the row, return the pieces."""
    app, approval = serve(), serve()
    target = f"http://localhost:{approval.server_address[1]}/"
    app.chat = _chat(link_for(target))
    ctx = browser.new_context()
    page = ctx.new_page()
    page.goto(f"http://127.0.0.1:{app.server_address[1]}/chat")
    page.locator(".lm-needs .lm-row").first.click()
    return ctx, page, approval


@pytest.mark.parametrize("link_for", [
    lambda t: TS + quote(t, safe=""),        # how the release card wrote it
    lambda t: t,                             # the page itself
], ids=["mobile-address", "plain-local"])
def test_the_approval_opens_over_the_chat_and_answers_there(browser, serve, link_for):
    ctx, page, approval = _tap_approval(browser, serve, link_for)
    page.wait_for_selector(".lm-ov.lm-view iframe")
    assert len(ctx.pages) == 1, "a new tab opened"
    frame = page.frame_locator(".lm-ov.lm-view iframe")
    frame.locator("#go").click()
    frame.locator("#done").wait_for()
    assert approval.answers == ["/answer?v=yes&t=n"]
    # still the chat underneath, and Close takes the popup away
    assert page.url.endswith("/chat") or "/chat?" in page.url
    page.locator(".lm-ov.lm-view .x").click()
    assert page.locator(".lm-ov").count() == 0


def test_a_link_to_the_outside_web_still_opens_a_tab(browser, serve):
    app = serve()
    app.chat = _chat("https://example.com/approve")
    ctx = browser.new_context()
    page = ctx.new_page()
    page.goto(f"http://127.0.0.1:{app.server_address[1]}/chat")
    ctx.route("https://example.com/**", lambda r: r.fulfill(status=200, body="x"))
    with ctx.expect_page():
        page.locator(".lm-needs .lm-row").first.click()
    assert page.locator(".lm-ov").count() == 0


def test_a_proxy_link_to_the_outside_web_is_not_framed(browser, serve):
    app = serve()
    app.chat = _chat(TS + quote("https://example.com/approve", safe=""))
    ctx = browser.new_context()
    page = ctx.new_page()
    page.goto(f"http://127.0.0.1:{app.server_address[1]}/chat")
    ctx.route("https://my-pc.tail0000.ts.net/**", lambda r: r.fulfill(status=200, body="x"))
    with ctx.expect_page():
        page.locator(".lm-needs .lm-row").first.click()
    assert page.locator(".lm-ov").count() == 0
