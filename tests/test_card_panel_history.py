"""A handoff card's full-screen panels have a URL (owner, 2026-10-28).

"anytime i click a panel and it goes fullscreen i want the url to work so i
can use browser forward back buttons, and refresh work".  Opening a picture,
a Review or a Decide panel adds ``?panel=...`` as a history entry: Back closes
it, Forward and a refresh open it again, closing it steps back over the entry,
and the app's own popstate handlers (chat, folder, Workflow) never see a move
that only opens or closes a panel.  A crafted link opens nothing a card in the
thread does not show.

Drives real headless Chrome over the DevTools protocol; skipped when Chrome
or the `websockets` package is missing.
"""

import asyncio
import functools
import http.server
import json
import shutil
import subprocess
import tempfile
import threading
import time
import urllib.request
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
CHROME = shutil.which("google-chrome") or shutil.which("chromium") or shutil.which("chromium-browser")
websockets = pytest.importorskip("websockets")
pytestmark = pytest.mark.skipif(not CHROME, reason="needs Chrome or Chromium")

CARDS = [
    {"type": "done", "headline": "Shot", "context": "x", "status": {"done": ["a"]},
     "links": [{"label": "The screen", "kind": "image", "url": "/static/shot.png"}],
     "needs": {"kind": "none", "why": "y", "next": "z"}},
    {"type": "needs_you", "headline": "Approve it", "context": "x",
     "needs": {"kind": "approve", "ask": "OK?", "why": "y",
               "look": [{"label": "The page", "kind": "link", "url": "/other.html"}],
               "replies": [{"key": "A", "label": "Yes ship it now", "reply": "a"},
                           {"key": "B", "label": "No hold it back", "reply": "b"}]}},
    {"type": "needs_you", "headline": "Pick one", "context": "x",
     "needs": {"kind": "decision", "ask": "Which?", "why": "y",
               "look": [{"label": "Options", "kind": "link", "url": "/other.html"}],
               "replies": [{"key": "A", "label": "First choice here", "reply": "a"},
                           {"key": "B", "label": "Second choice here", "reply": "b"}]}},
]


def _page():
    ph = "".join("<div class=\"msg\"><div class=\"vn-card-ph\" data-card='" + json.dumps(c).replace("'", "&#39;") + "'></div></div>"
                 for c in CARDS)
    return ("<!doctype html><meta charset=utf-8><body><div id=\"live-log\">" + ph + "</div>"
            "<script src=\"/static/js/last-card.js\"></script>"
            # what app.js / workspace.js do on load: rewrite the entry's state
            "<script>history.replaceState({folder: null, chat: 's1'}, '', location.href);"
            "window.__appPops = 0; window.addEventListener('popstate', () => { window.__appPops++; });"
            # the thread renders a moment after the page, as it does in the app
            "setTimeout(() => VNLastCard.renderIn(document.getElementById('live-log')), 400);</script>")


PNG = (b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01\x08\x06\x00\x00\x00\x1f\x15\xc4\x89"
       b"\x00\x00\x00\rIDATx\x9cc\xf8\xff\xff?\x00\x05\xfe\x02\xfe\xa7V\xbd\xfa\x00\x00\x00\x00IEND\xaeB`\x82")


@pytest.fixture
def site(tmp_path):
    root = tmp_path / "site"
    (root / "static/js").mkdir(parents=True)
    shutil.copy(REPO / "static/js/last-card.js", root / "static/js/last-card.js")
    (root / "page.html").write_text(_page(), "utf-8")
    (root / "other.html").write_text("<!doctype html><p>other</p>", "utf-8")
    (root / "static/shot.png").write_bytes(PNG)
    handler = functools.partial(http.server.SimpleHTTPRequestHandler, directory=str(root))
    handler.log_message = lambda *a, **k: None
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{srv.server_address[1]}/page.html"
    srv.shutdown()


async def _drive(url):
    prof = tempfile.mkdtemp()
    proc = subprocess.Popen([CHROME, "--headless=new", "--no-sandbox", "--disable-gpu", "--remote-debugging-port=0",
                             "--user-data-dir=" + prof, "about:blank"],
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        port_file = Path(prof) / "DevToolsActivePort"
        for _ in range(600):   # 60 s: a cold Chrome on a GitHub runner took over 10
            if port_file.exists() and port_file.read_text().strip():
                break
            time.sleep(0.1)
        port = port_file.read_text().split()[0]
        tabs = json.load(urllib.request.urlopen(f"http://127.0.0.1:{port}/json"))
        ws_url = [t for t in tabs if t["type"] == "page"][0]["webSocketDebuggerUrl"]
        async with websockets.connect(ws_url, max_size=2 ** 24) as ws:
            seq = 0

            async def call(method, **params):
                nonlocal seq
                seq += 1
                me = seq
                await ws.send(json.dumps({"id": me, "method": method, "params": params}))
                while True:
                    r = json.loads(await ws.recv())
                    if r.get("id") == me:
                        return r.get("result", {})

            async def ev(expr):
                r = await call("Runtime.evaluate", expression=expr, awaitPromise=True, returnByValue=True)
                return r.get("result", {}).get("value")

            async def until(expr, tries=60):
                for _ in range(tries):
                    if await ev(expr):
                        return True
                    await asyncio.sleep(0.1)
                return False

            state = ("({panel: new URL(location.href).searchParams.get('panel'), chat: new URL(location.href).searchParams.get('chat'),"
                     " view: !!document.querySelector('.lm-ov.lm-view'), review: !!document.querySelector('.lm-ov:not(.lm-view)'),"
                     " decide: !!document.querySelector('.lm-dz'), pops: window.__appPops})")
            ready = "document.querySelectorAll('.lm').length === 3"
            out = {}

            await call("Page.navigate", url=url + "?chat=s1")
            await until(ready)
            # picture: open, Back, Forward, refresh, close
            await ev("document.querySelector('.lm-row.shot').click()")
            out["img_open"] = await ev(state)
            await ev("history.back()")
            await until("!document.querySelector('.lm-ov')")
            out["img_back"] = await ev(state)
            await ev("history.forward()")
            await until("!!document.querySelector('.lm-ov.lm-view')")
            out["img_forward"] = await ev(state)
            await call("Page.reload")
            await until(ready)
            await until("!!document.querySelector('.lm-ov.lm-view')")
            out["img_reload"] = await ev(state)
            await ev("document.querySelector('.lm-ov .x').click()")
            await until("!new URL(location.href).searchParams.get('panel')")
            out["img_closed"] = await ev(state)
            out["img_back_after_close"] = await ev("history.length")
            # review: open, Escape closes and steps back
            await ev("document.querySelector('.lm-review').click()")
            out["review_open"] = await ev(state)
            await ev("document.dispatchEvent(new KeyboardEvent('keydown', {key: 'Escape', bubbles: true}))")
            await until("!new URL(location.href).searchParams.get('panel')")
            out["review_escape"] = await ev(state)
            # decide: open, refresh reopens it
            await ev("document.querySelector('.lm-decide').click()")
            out["decide_open"] = await ev(state)
            await call("Page.reload")
            await until(ready)
            await until("!!document.querySelector('.lm-dz')")
            out["decide_reload"] = await ev(state)
            await ev("history.back()")
            await until("!document.querySelector('.lm-dz')")
            out["decide_back"] = await ev(state)
            # a crafted link frames nothing the thread does not show
            await call("Page.navigate", url=url + "?chat=s1&panel=" + urllib.parse.quote("page:https://example.com/"))
            await until(ready)
            await asyncio.sleep(1.0)
            out["crafted"] = await ev(state)
            return out
    finally:
        proc.terminate()
        proc.wait(timeout=10)
        shutil.rmtree(prof, ignore_errors=True)


import urllib.parse  # noqa: E402


def test_full_screen_panels_follow_back_forward_and_refresh(site):
    r = asyncio.run(_drive(site))
    assert r["img_open"]["panel"] == "img:/static/shot.png" and r["img_open"]["view"]
    assert r["img_back"]["panel"] is None and not r["img_back"]["view"]
    assert r["img_forward"]["panel"] == "img:/static/shot.png" and r["img_forward"]["view"]
    assert r["img_reload"]["panel"] == "img:/static/shot.png" and r["img_reload"]["view"]
    assert r["img_closed"]["panel"] is None and not r["img_closed"]["view"]
    assert r["review_open"]["panel"] == "review:/other.html" and r["review_open"]["review"]
    assert r["review_escape"]["panel"] is None and not r["review_escape"]["review"]
    assert r["decide_open"]["panel"] == "decide:1" and r["decide_open"]["decide"]
    assert r["decide_reload"]["decide"]
    assert r["decide_back"]["panel"] is None and not r["decide_back"]["decide"]
    # the chat stays put, and the app's own handlers never saw a panel move
    for k in ("img_back", "img_forward", "img_closed", "review_escape", "decide_back"):
        assert r[k]["chat"] == "s1", k
        assert r[k]["pops"] == 0, k
    assert not (r["crafted"]["view"] or r["crafted"]["review"] or r["crafted"]["decide"])
