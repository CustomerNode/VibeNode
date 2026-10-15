"""Code blocks in the chat wrap at the box edge; they never scroll sideways.

A long prose prompt inside a fenced block rendered as one line per paragraph
behind a horizontal scrollbar (2026-10-15), unreadable on a phone.  The fix is
one rule in ``static/style.css`` (``pre.vn-code, .msg-body pre, ...``) plus
the ``vn-code`` class ``mdParse`` puts on every fenced block.  These tests
drive the real stylesheets, the real ``markdown.js`` and the real
``smart-copy.js`` in headless Chromium, at desktop and phone width, for every
place the chat draws a code block:

* an assistant message (``.msg.assistant .msg-body``, the ``.conversation`` skin);
* a user bubble (plain ``<pre>``, as live-panel.js draws it);
* the queue banner, the compose file preview, and a mission-control preview;

and assert: no horizontal overflow, ``white-space: pre-wrap``, indentation and
blank lines preserved, height not capped, and the copy button still copying
the original text.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
STATIC = ROOT / "static"

# The kind of prompt that broke: long prose paragraphs, a blank line between
# them, one indented line, and an unbroken path longer than any phone.
PROMPT = (
    "Build Durable Jobs from docs/plans/durable-jobs-spec.md. Read the spec first, then "
    "daemon/health_monitor.py (the supervisor pattern to copy), launch.bat, launch.sh and the "
    "Start-Process path in app/routes/main.py (the three detached launch paths).\n"
    "\n"
    "Don's decisions are in the spec and are not open: registry under Persistent Storage when "
    "enabled, else data/; registration is the launch path itself.\n"
    "\n"
    "    indented: C:\\Users\\someone\\AppData\\Local\\claude\\run-detached\\"
    "a-very-long-unbroken-file-name-that-is-wider-than-any-phone-screen.json\n"
    "\n"
    "All seven acceptance tests in the spec must pass; add them to the test suite."
)

VIEWPORTS = {"desktop": (1280, 900), "phone": (390, 844)}


def _page_html() -> str:
    style = (STATIC / "style.css").read_text(encoding="utf-8")
    mobile = (STATIC / "css" / "mobile.css").read_text(encoding="utf-8")
    markdown = (STATIC / "js" / "markdown.js").read_text(encoding="utf-8")
    smart = (STATIC / "js" / "smart-copy.js").read_text(encoding="utf-8")
    return f"""<!doctype html>
<html data-theme="dark"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<style>{style}</style>
<style>{mobile}</style>
</head><body>
<div class="layout"><div class="main"><div class="live-panel" id="live-panel">
  <div class="conversation live-log" id="live-log">
    <div class="msg assistant" id="asst"><div class="msg-body msg-content"></div></div>
    <div class="msg user" id="user"><div class="msg-body msg-content"></div></div>
  </div>
  <div class="live-queue-banner" id="queue"><div class="live-queue-banner-text"></div></div>
  <div class="compose-preview-file" id="compose"><pre></pre></div>
  <div class="mc-preview" id="mc"><div class="msg-body"></div></div>
</div></div></div>
<script>{markdown}</script>
<script>{smart}</script>
<script>
  const PROMPT = {json.dumps(PROMPT)};
  const fence = 'Paste this into a VibeNode session:\\n\\n```\\n' + PROMPT + '\\n```';
  const asst = document.querySelector('#asst .msg-body');
  asst.innerHTML = mdParse(fence);
  addSmartCopyButtons(asst, fence);
  // live-panel.js draws a user bubble as a plain pre-wrap <pre>.
  const u = document.createElement('pre');
  u.style.cssText = 'white-space:pre-wrap;margin:0;display:inline;';
  u.textContent = PROMPT;
  document.querySelector('#user .msg-body').appendChild(u);
  document.querySelector('#queue .live-queue-banner-text').innerHTML = mdParse(fence);
  document.querySelector('#compose pre').textContent = PROMPT;
  document.querySelector('#mc .msg-body').innerHTML = mdParse(fence);
</script>
</body></html>"""


MEASURE = """() => Array.from(document.querySelectorAll('pre')).map(pre => {
  const cs = getComputedStyle(pre);
  const code = pre.querySelector('code');
  const holder = pre.closest('[id]');
  return {
    where: holder ? holder.id : '?',
    whiteSpace: cs.whiteSpace,
    overflowX: cs.overflowX,
    maxHeight: cs.maxHeight,
    scrollW: pre.scrollWidth, clientW: pre.clientWidth,
    scrollH: pre.scrollHeight, clientH: pre.clientHeight,
    text: (code || pre).textContent,
    vw: document.documentElement.clientWidth,
    right: pre.getBoundingClientRect().right,
  };
})"""


@pytest.fixture(scope="module")
def browser():
    pw = pytest.importorskip("playwright.sync_api", reason="playwright not installed")
    with pw.sync_playwright() as p:
        try:
            b = p.chromium.launch()
        except Exception as exc:  # pragma: no cover - environment dependent
            pytest.skip(f"chromium unavailable: {str(exc).splitlines()[0][:80]}")
        yield b
        b.close()


def _measure(browser, size):
    pg = browser.new_page(viewport={"width": size[0], "height": size[1]})
    try:
        pg.set_content(_page_html())
        return pg.evaluate(MEASURE), pg
    except Exception:
        pg.close()
        raise


@pytest.mark.parametrize("name", list(VIEWPORTS))
def test_every_chat_code_block_wraps_without_sideways_scroll(browser, name):
    blocks, pg = _measure(browser, VIEWPORTS[name])
    try:
        where = {b["where"] for b in blocks}
        assert {"asst", "user", "queue", "compose", "mc"} <= where, where
        for b in blocks:
            label = f"{name}/{b['where']}"
            assert b["whiteSpace"] == "pre-wrap", (label, b["whiteSpace"])
            assert b["overflowX"] not in ("auto", "scroll"), (label, b["overflowX"])
            # No hidden overflow either: every line fits inside the box.
            assert b["scrollW"] <= b["clientW"] + 1, (label, b["scrollW"], b["clientW"])
            assert b["right"] <= b["vw"] + 1, (label, "pre runs off the page")
            # Height grows with the text: no cap, no vertical scroll box.
            assert b["maxHeight"] == "none", (label, b["maxHeight"])
            assert b["scrollH"] <= b["clientH"] + 1, (label, "vertical scroll box")
    finally:
        pg.close()


@pytest.mark.parametrize("name", list(VIEWPORTS))
def test_wrapping_keeps_indentation_blank_lines_and_monospace(browser, name):
    blocks, pg = _measure(browser, VIEWPORTS[name])
    try:
        fenced = [b for b in blocks if b["where"] in ("asst", "queue", "mc")]
        assert fenced
        for b in fenced:
            # mdParse keeps the fence body byte for byte (one trailing newline).
            assert b["text"].rstrip("\n") == PROMPT, b["where"]
            assert "\n\n    indented:" in b["text"]
        font = pg.evaluate(
            "() => getComputedStyle(document.querySelector('#asst pre code')).fontFamily")
        assert "mono" in font.lower() or "consol" in font.lower() or "courier" in font.lower(), font
        # The block really wraps: it is several visual lines tall per paragraph.
        lines = pg.evaluate("""() => {
            const c = document.querySelector('#asst pre code');
            const lh = parseFloat(getComputedStyle(c).lineHeight) || 16;
            return c.getBoundingClientRect().height / lh; }""")
        assert lines > PROMPT.count("\n") + 1, lines
    finally:
        pg.close()


def test_copy_button_copies_the_original_unwrapped_text(browser):
    pg = browser.new_page(viewport={"width": 390, "height": 844})
    try:
        pg.set_content(_page_html())
        copied = pg.evaluate("""async () => {
            let got = null;
            window.vnCopyText = (t) => { got = t; return Promise.resolve(); };
            Object.defineProperty(navigator, 'clipboard', {
              configurable: true, value: { writeText: (t) => { got = t; return Promise.resolve(); } } });
            const btn = document.querySelector('#asst pre .smart-copy-btn');
            if (!btn) return '__no button__';
            btn.click();
            await new Promise(r => setTimeout(r, 50));
            return got;
        }""")
        assert copied is not None and copied != "__no button__", copied
        assert copied.rstrip("\n") == PROMPT
    finally:
        pg.close()


def test_tables_and_extract_views_keep_their_own_behaviour():
    """The wrap rule is scoped to chat code blocks, not every <pre>."""
    css = (STATIC / "style.css").read_text(encoding="utf-8")
    start = css.index("pre.vn-code, .msg-body pre")
    rule = css[start:css.index("}", start)]
    assert ".code-block-pre" not in rule and "table" not in rule
    # The extract/diff view keeps its own sideways scroll.
    assert ".code-block-pre { margin:0; padding:var(--sp-5); overflow-x:auto;" in css


NESTED = ("Paste this:\n\n````\nFix the renderer so that\n\n```\ninner block\n```\n\n"
          "stays inside the box.\n````\n\nAfter the box.")


def test_a_longer_fence_can_carry_a_shorter_fence_inside(browser):
    """A ```` fence closes only on ````, so a ``` block inside it is content.

    The first attempt at the wrap-fix prompt put three backticks inside a
    three-backtick fence and the box ended early.  With a four-backtick outer
    fence that is one block, inner fence lines included, and nothing after the
    outer fence is swallowed."""
    pg = browser.new_page()
    try:
        pg.set_content(_page_html())
        got = pg.evaluate("""(md) => {
            const d = document.createElement('div');
            d.innerHTML = mdParse(md);
            return { pres: d.querySelectorAll('pre').length,
                     code: d.querySelector('pre code').textContent,
                     text: d.textContent }; }""", NESTED)
        assert got["pres"] == 1, got
        assert got["code"].rstrip("\n") == "Fix the renderer so that\n\n```\ninner block\n```\n\nstays inside the box."
        assert got["text"].rstrip().endswith("After the box.")
        assert "````" not in got["text"]
        # The common case is untouched: a plain ``` fence still closes on ```.
        plain = pg.evaluate("""(md) => { const d = document.createElement('div');
            d.innerHTML = mdParse(md);
            return [d.querySelectorAll('pre').length, d.querySelector('pre code').className,
                    d.querySelector('pre code').textContent]; }""", "a\n```py\nx = 1\n```\nb")
        assert plain[0] == 1 and plain[1] == "lang-py" and plain[2].rstrip("\n") == "x = 1", plain
    finally:
        pg.close()
