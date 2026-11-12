"""The "you asked" bar keeps the last prompt at the top while a session runs.

The bar (``src/web/static/js/sticky-user-msg.js``) showed the newest prompt that had
scrolled above the view, but it only looked at bubbles on the page.  During a
long run the thread drops its oldest messages (the DOM cap) and a reopened
session draws only its newest page, so the prompt that started the run was not
on the page and the bar vanished exactly while the session worked.  The last
prompt must always show at the top as one line that expands when clicked.

Runs the real script and CSS in headless Chromium (skips without Playwright).
"""
from pathlib import Path

import pytest

from tests._browser import shared_browser_scope

ROOT = Path(__file__).resolve().parents[1]
JS = ROOT / "src" / "web" / "static" / "js" / "sticky-user-msg.js"
CSS = ROOT / "src" / "web" / "static" / "style.css"

PROMPT = "a long first line for the sticky prompt bar test " + "and more words " * 30


@pytest.fixture(scope="module")
def browser():
    with shared_browser_scope() as b:
        yield b


def _page(tmp_path, body, fetch_entries=None):
    tools = "".join(f'<div class="msg assistant"><div class="msg-body">tool step {i}</div></div>' for i in range(60))
    fetch = ("window.__fetches=0; window.fetch=(u)=>{ window.__fetches++; return Promise.resolve({ok:true, json:()=>Promise.resolve("
             + ("{entries:" + fetch_entries + "}" if fetch_entries else "{entries:[]}") + ")}); };")
    html = f"""<!doctype html><html><head><meta charset="utf-8">
<link rel="stylesheet" href="{CSS.as_uri()}">
<style>#wrap{{height:600px}} #live-log{{height:600px;overflow:auto}} .msg{{min-height:40px}}</style></head>
<body><div id="wrap"><div class="conversation live-log" id="live-log">{body.replace('TOOLS', tools)}</div></div>
<script>var liveSessionId='s1'; {fetch}</script>
<script src="{JS.as_uri()}"></script>
<script>initStickyUserMessages(document.getElementById('live-log'));</script></body></html>"""
    f = tmp_path / "p.html"
    f.write_text(html, encoding="utf-8")
    return f


def _open(browser, f):
    page = browser.new_page()
    errors = []
    page.on("pageerror", lambda e: errors.append(str(e)))
    page.goto(f.as_uri(), wait_until="load")
    return page, errors


def _bar(page):
    return page.evaluate("""() => { const b = document.querySelector('.sticky-user-bar');
        return {visible: b.classList.contains('visible'), text: b.textContent.trim(),
                expanded: b.classList.contains('expanded'), h: b.getBoundingClientRect().height}; }""")


USER = f'<div class="msg user"><div class="msg-body">{PROMPT}</div></div>'


def test_shows_when_prompt_scrolled_above(browser, tmp_path):
    page, errors = _open(browser, _page(tmp_path, USER + "TOOLS"))
    page.evaluate("document.getElementById('live-log').scrollTop = 1e6")
    page.wait_for_timeout(100)
    b = _bar(page)
    assert b["visible"] and b["text"].startswith("a long first line"), b
    assert not errors, errors


def test_stays_when_running_trims_the_prompt_off_the_page(browser, tmp_path):
    page, errors = _open(browser, _page(tmp_path, USER + "TOOLS"))
    page.evaluate("document.getElementById('live-log').scrollTop = 1e6")
    page.wait_for_timeout(100)
    # The run goes on: the DOM cap removes the oldest messages, prompt included,
    # and new tool steps arrive with no scroll by the reader.
    page.evaluate("""() => { const log = document.getElementById('live-log');
        log.querySelector('.msg.user').remove();
        for (let i = 0; i < 5; i++) { const d = document.createElement('div');
          d.className = 'msg assistant'; d.innerHTML = '<div class="msg-body">new step</div>'; log.appendChild(d); } }""")
    page.wait_for_timeout(100)
    b = _bar(page)
    assert b["visible"] and b["text"].startswith("a long first line"), b
    assert page.evaluate("window.__fetches") == 0
    assert not errors, errors


def test_reopened_run_fetches_the_prompt_once_and_expands_on_click(browser, tmp_path):
    entries = '[{kind:"user",text:"older prompt"},{kind:"asst",text:"x"},{kind:"user",text:' + repr(PROMPT).replace("'", '"') + '},{kind:"tool_use",name:"Bash"}]'
    page, errors = _open(browser, _page(tmp_path, "TOOLS", fetch_entries=entries))
    page.wait_for_function("document.querySelector('.sticky-user-bar').classList.contains('visible')")
    b = _bar(page)
    assert b["text"].startswith("a long first line"), b
    one_line = b["h"]
    assert one_line <= 32, b
    page.click(".sticky-user-bar")
    b = _bar(page)
    assert b["expanded"] and b["h"] > one_line * 1.5, b
    page.evaluate("document.getElementById('live-log').scrollTop = 300")
    page.wait_for_timeout(100)
    assert page.evaluate("window.__fetches") == 1
    assert not errors, errors


def test_hidden_while_prompt_is_on_screen(browser, tmp_path):
    page, errors = _open(browser, _page(tmp_path, "TOOLS" + USER))
    page.evaluate("document.getElementById('live-log').scrollTop = 1e6")
    page.wait_for_timeout(100)
    assert not _bar(page)["visible"]
    assert not errors, errors
