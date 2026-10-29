"""A Needs-you card goes green once it is answered (owner, 2026-10-28).

"this should go green after i decide": a Decide card he had answered still
showed amber "Needs you" with "Sent: ..." on its button.  Now the card turns
green with "Decided" (or "Answered" for any other ask) when he replies from
the card, and also when any message of his follows the card in the thread,
so a redraw or reload keeps it green and an answer typed in the chat box
counts.  A card nobody answered stays amber.

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


def _card(title, kind="decision", **needs):
    return json.dumps({"type": "needs_you", "title": title, "headline": title + " for the test page",
                       "needs": dict({"kind": kind, "ask": "Which?", "why": "y",
                                      "replies": [{"key": "A", "label": "Always a link", "reply": "Go with A"},
                                                  {"key": "B", "label": "Always a demo", "reply": "Validate fix B: always a demo"}]},
                                     **needs)}).replace("'", "&#39;")


PAGE = f"""<!doctype html><meta charset=utf-8>
<link rel="stylesheet" href="/static/css/last-card.css">
<body><div id="live-log">
<div class="msg assistant" id="m1"><div class="msg-body"><div class="vn-card-ph" data-card='{_card("Answered in the chat")}'></div></div></div>
<div class="msg user"><div class="msg-body">Validate fix B: always a demo</div></div>
<div class="msg assistant" id="m2"><div class="msg-body"><div class="vn-card-ph" data-card='{_card("Answered from the card", kind="approve", replies=[{"key": "A", "label": "Yes", "reply": "yes"}, {"key": "B", "label": "No", "reply": "no"}], ask="Ok?")}'></div></div></div>
<div class="msg user vn-event-msg"><div class="msg-body">watchdog nudge, not his answer</div></div>
<div class="msg assistant" id="m3"><div class="msg-body"><div class="vn-card-ph" data-card='{_card("Not answered yet")}'></div></div></div>
<div class="msg assistant" id="m4"><div class="msg-body"><div class="vn-card-ph" data-card='{_card("Asked back")}'></div></div></div>
<div class="msg user"><div class="msg-body">wait, what does a demo mean here?</div></div>
<div class="msg assistant" id="m5"><div class="msg-body"><div class="vn-card-ph" data-card='{_card("Moved on")}'></div></div></div>
<div class="msg user"><div class="msg-body">different thing: the sidebar is slow today</div></div>
<div class="msg assistant" id="m6"><div class="msg-body"><div class="vn-card-ph" data-card='{_card("Answered by its key")}'></div></div></div>
<div class="msg user"><div class="msg-body">B</div></div>
</div>
<script>window.liveSessionId = 's1'; window.sessionKinds = {{}}; window.__sent = [];
window._liveSubmitDirect = (sid, t) => window.__sent.push(t);</script>
<script src="/static/js/last-card.js"></script>
<script>VNLastCard.renderIn(document.getElementById('live-log'));</script>
"""

STATE = """(() => Array.from(document.querySelectorAll('.msg.assistant')).map(m => {
  const c = m.querySelector('.lm');
  return { id: m.id, answered: c.classList.contains('lm-answered'),
           tone: getComputedStyle(c).getPropertyValue('--tone').trim(),
           pill: c.querySelector('.lm-pill').textContent.trim(),
           decide: (c.querySelector('.lm-decide') || {}).textContent || '',
           stick: c.querySelector('.lm-stick .jump') ? 'jump' : (c.querySelector('.lm-stick .st') || {}).textContent || '' };
}))()"""


@pytest.fixture
def site(tmp_path):
    root = tmp_path / "site"
    for rel in ("static/js/last-card.js", "static/css/last-card.css"):
        (root / rel).parent.mkdir(parents=True, exist_ok=True)
        shutil.copy(REPO / rel, root / rel)
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
        for _ in range(100):
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

            await call("Page.navigate", url=url)
            for _ in range(50):
                if await ev("document.querySelectorAll('.lm').length === 6"):
                    break
                await asyncio.sleep(0.1)
            await asyncio.sleep(0.2)   # the rAF that marks answered cards
            out = {"loaded": await ev(STATE)}
            await ev("document.querySelector('#m2 .lm-row.reply[data-reply=\"no\"]').click(), 1")
            out["clicked"] = await ev(STATE)
            out["sent"] = await ev("window.__sent")
            await ev("VNLastCard.syncAnswered(), 1")
            out["resynced"] = await ev(STATE)
            return out
    finally:
        proc.terminate()
        proc.wait(timeout=10)
        shutil.rmtree(prof, ignore_errors=True)


GREEN, AMBER = "52, 199, 120", "240, 166, 40"


def test_a_card_answered_in_the_chat_turns_green_and_the_rest_stay_amber(site):
    r = asyncio.run(_drive(site))
    m1, m2, m3, *_ = r["loaded"]
    assert (m1["answered"], m1["tone"], m1["pill"]) == (True, GREEN, "Decided")
    assert m1["decide"] == "Sent: “Validate fix B: always a demo”"
    assert m1["stick"] == "Decided"
    # a hook message is not his answer
    assert (m2["answered"], m2["tone"], m2["pill"]) == (False, AMBER, "Needs you")
    assert (m3["answered"], m3["tone"], m3["pill"], m3["stick"]) == (False, AMBER, "Needs you", "jump")

    # answering from the card's own buttons turns it green at once
    c1, c2, c3, *_ = r["clicked"]
    assert r["sent"] == ["no"]
    assert (c2["answered"], c2["tone"], c2["pill"]) == (True, GREEN, "Answered")
    assert c3["answered"] is False
    # a later sync does not touch the unanswered card
    assert r["resynced"][2]["answered"] is False


def test_a_question_back_or_a_new_topic_leaves_the_card_waiting(site):
    # 2026-10-29: any later message of his used to turn a Needs-you card
    # green.  A question back, or a change of subject, is not an answer.
    r = asyncio.run(_drive(site))
    m4, m5, m6 = r["loaded"][3:]
    assert (m4["answered"], m4["pill"]) == (False, "Needs you")
    assert (m5["answered"], m5["pill"]) == (False, "Needs you")
    # the choice's key typed alone in the chat box still counts
    assert (m6["answered"], m6["pill"]) == (True, "Decided")
    assert r["resynced"][3]["answered"] is False and r["resynced"][4]["answered"] is False
