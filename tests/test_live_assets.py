"""live-assets.js: an open tab picks up a changed card without a reload.

The owner, 2026-10-28: a session renamed the card's "Your move" heading, and
its own card, drawn by the old code in his open tab, still said "Your move"
and told him to reload ("do you see how little sense that makes").

The page here starts on an OLD copy of last-card.js (the "Your move"
heading), served as the page would have loaded it before the change.  One
check by live-assets.js must load the real file from disk, hand over the
document listener (a "More" click toggles once, not twice), swap the CSS and
redraw both cards with the new headings.

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

PAGE = """<!doctype html><meta charset=utf-8>
<link rel="stylesheet" href="/static/css/last-card.css?v=1">
<body>
<div class="msg"><div class="vn-card-ph" data-card='{"type":"needs_you","headline":"Pick a heading","context":"x","needs":{"kind":"decision","ask":"Which?","why":"y","replies":[{"key":"A","label":"First choice here","reply":"a"},{"key":"B","label":"Second choice here","reply":"b"}]}}'></div></div>
<div class="msg"><div class="vn-card-ph" data-card='{"type":"done","headline":"Renamed","context":"x","verified":[{"check":"c","result":"r","ok":true}],"needs":{"kind":"none","why":"y","next":"z"}}'></div></div>
<script src="/static/old/last-card.js?v=1&as=js/last-card.js"></script>
<script>VNLastCard.renderIn(document.body);</script>
<script src="/static/js/live-assets.js"></script>
"""


def _old_copy(src: str) -> str:
    """The card script as it was before the rename: both headings 'Your move'."""
    old = src.replace("band(({ decision: 'Decide', approve: 'Approve', action: 'To do' })[n.kind] || 'Needed from you'",
                      "band('Your move'")
    old = old.replace("h += band('Next', (clock ? ", "h += band('Your move', (clock ? ")
    assert old.count("'Your move'") == 2, "the card's heading code moved; update this test's old copy"
    return old


@pytest.fixture
def site(tmp_path):
    root = tmp_path / "site"
    for rel in ("static/js/last-card.js", "static/js/live-assets.js", "static/css/last-card.css"):
        (root / rel).parent.mkdir(parents=True, exist_ok=True)
        shutil.copy(REPO / rel, root / rel)
    (root / "static/old").mkdir()
    (root / "static/old/last-card.js").write_text(_old_copy((REPO / "static/js/last-card.js").read_text("utf-8")), "utf-8")
    (root / "page.html").write_text(PAGE, "utf-8")
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

            heads = "Array.from(document.querySelectorAll('.lm-dl .dt')).map(e => e.textContent).filter(t => t !== 'More')"
            await call("Page.navigate", url=url)
            for _ in range(50):
                if await ev("!!window.VNLiveAssets && document.querySelectorAll('.lm').length === 2"):
                    break
                await asyncio.sleep(0.1)
            out = {"before": await ev(heads)}
            await ev("window.__old = window.VNLastCard, 1")
            await ev("VNLiveAssets.check()")
            out["after"] = await ev(heads)
            out["took_over"] = await ev("window.VNLastCard !== window.__old")
            out["css"] = await ev("document.querySelector('link[href*=\"last-card.css\"]').getAttribute('href')")
            out["more_opens"] = await ev("(() => { const b = document.querySelector('.lm-more'); b.click(); return b.classList.contains('open'); })()")
            return out
    finally:
        proc.terminate()
        proc.wait(timeout=10)
        shutil.rmtree(prof, ignore_errors=True)


def test_an_open_tab_redraws_cards_with_the_new_card_code(site):
    r = asyncio.run(_drive(site))
    assert r["before"] == ["Your move", "Your move"]
    assert r["after"] == ["Decide", "Next"]
    assert r["took_over"] is True
    assert r["css"] != "/static/css/last-card.css?v=1"
    assert r["more_opens"] is True   # the old copy's click listener stood down


def test_the_page_loads_live_assets_after_the_card():
    html = (REPO / "templates/index.html").read_text("utf-8")
    assert html.index("js/last-card.js") < html.index("css/last-card.css") < html.index("js/live-assets.js")
