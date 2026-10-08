"""Focusing the composer on a phone must not shove the app up the screen.

When the composer takes focus, iOS Safari slides the whole page up by about
the keyboard's height to "reveal" the input.  The composer is position:fixed
and ``updateKeyboardOffset()`` (static/js/mobile.js) already lifts it above the
keyboard, so the pan only pushed the header off the top and everything above
the composer with it (reported 2026-10-08).  The function now undoes the pan
while the composer has focus and lifts the bar by the full keyboard height.

A real on-screen keyboard cannot be opened in a test, so the real function is
lifted out of mobile.js and driven in Node against a fake ``visualViewport``
under each browser model:

* pan undone by ``scrollTo`` (the fix working),
* pan NOT undone by ``scrollTo`` (must end exactly where the old code did),
* the browser panning straight back every time (must not loop).
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
MOBILE_JS = (ROOT / "static" / "js" / "mobile.js").read_text(encoding="utf-8")


def _function_source(name: str) -> str:
    start = MOBILE_JS.index(f"function {name}(")
    i = MOBILE_JS.index("{", start)
    depth = 0
    while True:
        depth += {"{": 1, "}": -1}.get(MOBILE_JS[i], 0)
        if depth == 0:
            return MOBILE_JS[start:i + 1]
        i += 1


_HARNESS = r"""
const KB = 300, INNER = 800;
function run(model, focus) {
  const props = {}; const listeners = {}; let rafQ = []; let scrollCalls = 0; let pins = 0;
  const vv = { height: INNER - KB, offsetTop: KB, addEventListener() {} };
  const composerEl = { closest: sel => sel === '#live-input-bar' ? {} : null };
  const otherEl = { closest: () => null };
  const document = {
    documentElement: { style: { setProperty: (k, v) => { props[k] = v; } }, classList: { toggle: (c, on) => { props[c] = on; } } },
    activeElement: focus === 'composer' ? composerEl : otherEl,
    getElementById: () => null,
    addEventListener: (n, f) => { listeners[n] = f; },
    body: {},
  };
  const window = {
    visualViewport: vv, innerHeight: INNER, pageYOffset: 0,
    addEventListener() {},
    requestAnimationFrame: f => { rafQ.push(f); return 1; },
    scrollTo: () => {
      scrollCalls++;
      if (model === 'undone') vv.offsetTop = 0;          // scrollTo undoes the pan
      // 'ignored': nothing changes.  'fights': undone, then panned back below.
      if (model === 'fights') vv.offsetTop = 0;
    },
    ThreadScroll: { atBottom: () => true, scrollToBottom: () => { pins++; } },
    _updateFloatOffset() {},
  };
  const MutationObserver = function () { this.observe = () => {}; };
  const setTimeout = (f) => { rafQ.push(f); return 1; };
  %s
  initComposerAnchoring();
  // Let queued frames run; in the 'fights' model the browser re-pans after
  // every frame and fires another viewport event, as a hostile browser would.
  let guard = 0;
  while (rafQ.length && guard++ < 200) {
    const f = rafQ.shift(); f();
    if (model === 'fights' && guard < 150) { vv.offsetTop = KB; listeners.focusin(); }
  }
  return { offset: props['--vn-kb-offset'], open: props['vn-kb-open'], scrollCalls, guard, pins };
}
const out = {};
for (const m of ['undone', 'ignored', 'fights']) out[m] = run(m, 'composer');
out.other = run('undone', 'other');
process.stdout.write(JSON.stringify(out));
"""


@pytest.fixture(scope="module")
def results():
    node = shutil.which("node")
    if not node:
        pytest.skip("node not installed")
    script = _HARNESS % _function_source("initComposerAnchoring")
    proc = subprocess.run([node, "-e", script], capture_output=True, text=True, timeout=30)
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout)


def test_pan_is_undone_and_the_bar_lifts_by_the_full_keyboard(results):
    r = results["undone"]
    assert r["scrollCalls"] >= 1, "the pan was never undone"
    assert r["offset"] == "300px", "the composer must end the full keyboard height up"
    assert r["open"] is True, "html.vn-kb-open must be set so CSS can compact the greeting"
    assert r["pins"] >= 1, "a reader at the bottom of the thread must be kept there"


def test_a_browser_that_ignores_scrollto_ends_where_the_old_code_did(results):
    """innerHeight - vv.height - vv.offsetTop = 0: the bar is already in view."""
    assert results["ignored"]["offset"] == "0px"


def test_a_browser_that_pans_straight_back_is_not_fought_in_a_loop(results):
    r = results["fights"]
    assert r["scrollCalls"] <= 4, f"{r['scrollCalls']} pan-undo attempts: the rate limit is gone"
    assert r["guard"] < 200, "the re-measure never settled"


def test_other_fields_keep_the_browsers_pan(results):
    """A rename box or a dialog field needs the pan to stay visible."""
    r = results["other"]
    assert r["scrollCalls"] == 0
    assert r["offset"] == "0px"


def test_the_thread_gives_up_the_space_the_bar_rises_into():
    css = (ROOT / "static" / "css" / "mobile.css").read_text(encoding="utf-8")
    assert re.search(
        r"\.live-panel\s*\{[^}]*padding-bottom:\s*calc\(var\(--vn-composer-height[^)]*\)\s*\+\s*var\(--vn-kb-offset",
        css, re.S,
    ), ".live-panel must reserve the keyboard lift, or content sits behind the raised composer"


def test_android_resizes_the_page_for_the_keyboard():
    html = (ROOT / "templates" / "index.html").read_text(encoding="utf-8")
    assert "interactive-widget=resizes-content" in html
