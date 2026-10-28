"""Only one of a handoff card's full-screen panels is ever open (owner, 2026-10-28).

"sometimes like multiple get opened ... there should on the opener make sure
only one at a time".  Every opener (Review, Decide, the picture/page viewer)
closes whatever panel is up first, including a Decide popup still folding
shut and a panel drawn by an older copy of last-card.js that live-assets.js
left beside a newer one.

Drives real headless Chrome over the DevTools protocol, with the page and
server of tests/test_card_panel_history.py.
"""

import asyncio
import json
import shutil
import subprocess
import tempfile
import time
import urllib.request
from pathlib import Path

import pytest

from tests.test_card_panel_history import CHROME, site  # noqa: F401  (fixture)

pytest.importorskip("websockets")
import websockets  # noqa: E402

pytestmark = pytest.mark.skipif(not CHROME, reason="needs Chrome or Chromium")

COUNT = "document.querySelectorAll('.lm-ov, .lm-dz, .lm-dz-scrim').length"
PANELS = "document.querySelectorAll('.lm-ov, .lm-dz').length"


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

            async def until(expr, tries=60):
                for _ in range(tries):
                    if await ev(expr):
                        return True
                    await asyncio.sleep(0.1)
                return False

            out = {}
            await call("Page.navigate", url=url + "?chat=s1")
            await until("document.querySelectorAll('.lm').length === 3")

            # Review open, then Decide, then the picture: one at a time.
            await ev("document.querySelector('.lm-review').click()")
            await ev("document.querySelector('.lm-decide').click()")
            out["review_then_decide"] = await ev(PANELS)
            await ev("document.querySelector('.lm-row.shot').click()")
            out["decide_then_shot"] = await ev(COUNT)
            await ev("document.querySelector('.lm-ov .x').click()")
            await until("!new URL(location.href).searchParams.get('panel')")

            # Decide closing with its fold, and Review opened before the fold ends.
            await ev("document.querySelector('.lm-decide').click()")
            await asyncio.sleep(0.7)
            await ev("document.dispatchEvent(new KeyboardEvent('keydown', {key: 'Escape', bubbles: true}))")
            await ev("document.querySelector('.lm-review').click()")
            out["fold_then_review"] = await ev(COUNT)
            await asyncio.sleep(0.8)   # the fold's own cleanup has run by now
            out["fold_then_review_later"] = {"n": await ev(COUNT),
                                             "locked": await ev("document.documentElement.classList.contains('lm-ov-open')")}
            await ev("document.querySelector('.lm-ov .x').click()")
            await until("!new URL(location.href).searchParams.get('panel')")

            # A newer copy of the file loaded beside the old one (live-assets.js):
            # the old copy has a panel up, the new copy opens another.
            await ev("window.__old = VNLastCard; document.querySelector('.lm-review').click(); true")
            await ev("new Promise(r => { const s = document.createElement('script');"
                     " s.src = '/static/js/last-card.js?v=2'; s.onload = r; document.body.appendChild(s); })")
            out["swapped"] = await ev("window.VNLastCard !== window.__old")
            await ev("document.querySelector('.lm-row.shot').click()")
            out["swap_open"] = {"n": await ev(COUNT), "view": await ev("!!document.querySelector('.lm-ov.lm-view')")}
            await ev("document.dispatchEvent(new KeyboardEvent('keydown', {key: 'Escape', bubbles: true}))")
            await until("!document.querySelector('.lm-ov')")
            out["swap_closed"] = await ev(COUNT)
            return out
    finally:
        proc.terminate()
        proc.wait(timeout=10)
        shutil.rmtree(prof, ignore_errors=True)


def test_only_one_full_screen_panel_at_a_time(site):  # noqa: F811
    r = asyncio.run(_drive(site))
    assert r["review_then_decide"] == 1
    assert r["decide_then_shot"] == 1
    assert r["fold_then_review"] == 1, "a Decide popup still folding shut stayed beside the new panel"
    assert r["fold_then_review_later"] == {"n": 1, "locked": True}
    assert r["swapped"]
    assert r["swap_open"] == {"n": 1, "view": True}, "the old copy's panel stayed open under the new one"
    assert r["swap_closed"] == 0
