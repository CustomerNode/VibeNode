""""Full answer" on an answer card, now one version in the Answer band's
switch (owner's pick C of static/scratch/answer-depth.html, 2026-10-28:
"this part doesnt make sense because explain more is in short answer").

An answer card has ONE Answer band with a Short / Full / Plain words switch:
"answer" is Short (open), the optional "full_answer" is Full, and the card's
"explained" is Plain words.  On a card with an answer there is no separate
Explain more up top and no Full answer fold.  "full_answer" is not counted
toward the card's length and needs the short "answer" beside it.  It
replaced the owner's pick A of answer-detail.html (a Full answer fold under
"In short", with Explain more up top), which said the same thing up to four
times.
"""
import json
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from daemon import handoff_rules  # noqa: E402

LAST_CARD = ROOT / "static" / "js" / "last-card.js"

SCRIPT = r"""
global.window = global;
global.location = { host: '127.0.0.1:5050' };
global.document = { addEventListener() {}, querySelectorAll() { return []; } };
eval(require('fs').readFileSync(process.argv[1], 'utf8'));
const cards = JSON.parse(process.argv[2]);
const out = {};
for (const [k, c] of Object.entries(cards)) out[k] = VNLastCard.render(c, {});
process.stdout.write(JSON.stringify(out));
"""

FULL = ("## Why word order doesn't help\nThese titles follow it:\n\n- **Build the cold reader**\n\n"
        "| Vague | Clear |\n|---|---|\n| Fix stale rules | Sidebar: filter by Done |\n\n" + "More words here. " * 80)
EX = "## What this is about\nYou asked about titles.\n\n## What happened\n" + "Plain words. " * 20
BASE = {"type": "explain", "title": "A fixed title format", "headline": "Word order won't fix vague titles.",
        "context": "Titles", "answer": "Word order isn't the problem; a first slot naming a place is.",
        "zoom": {"big": "Clearer titles", "task": "Enforce a title format", "now": "Tested formats"},
        "explained": EX, "needs": {"kind": "none", "why": "Answered.", "next": "Nothing planned."}}


@pytest.fixture(scope="module")
def out():
    if shutil.which("node") is None:
        pytest.skip("node not installed")
    alone = dict(BASE)
    del alone["explained"]
    no_answer = dict(BASE, compare={"before": ["a"], "after": ["b"]})
    del no_answer["answer"]
    cards = {"full": dict(BASE, full_answer=FULL), "short": dict(BASE), "alone": alone, "no_answer": no_answer}
    r = subprocess.run(["node", "-e", SCRIPT, str(LAST_CARD), json.dumps(cards)],
                       capture_output=True, text=True, timeout=30, check=True)
    return json.loads(r.stdout)


def _band(h):
    return h[h.index(">Answer<"):]


def test_one_answer_band_with_a_short_full_plain_words_switch(out):
    band = _band(out["full"])
    assert ">In short<" not in out["full"]
    btns = re.findall(r'<button type="button" class="lm-depth-b" data-depth="(\w+)" aria-pressed="(\w+)">([^<]+)</button>', band)
    assert btns == [("short", "true", "Short"), ("full", "false", "Full"), ("plain", "false", "Plain words")]
    panes = re.findall(r'<div class="lm-depth-p" data-depth="(\w+)"( hidden)?>', band)
    assert panes == [("short", ""), ("full", " hidden"), ("plain", " hidden")]
    # Short is the answer; Full is the markdown (a heading, a list, a table); Plain words is "explained"
    short, full, plain = (band.index(f'class="lm-depth-p" data-depth="{k}"') for k in ("short", "full", "plain"))
    assert short < band.index("Word order isn") < full < band.index("<table") < plain < band.index("Plain words. ")
    assert "<li" in band[full:plain]


def test_explain_more_moves_into_the_switch(out):
    for k in ("full", "short"):
        assert 'class="lm-xpl-tg"' not in out[k] and "Explain more" not in out[k]
    assert "lm-fullans" not in out["full"]


def test_no_full_answer_no_full_tab(out):
    band = _band(out["short"])
    assert 'data-depth="full"' not in band
    assert 'data-depth="short"' in band and 'data-depth="plain"' in band


def test_an_answer_alone_has_no_switch(out):
    band = _band(out["alone"])
    assert "lm-depth" not in band and "Word order isn" in band


def test_a_card_without_an_answer_keeps_explain_more_up_top(out):
    h = out["no_answer"]
    assert "lm-depth" not in h and h.index('class="lm-xpl-tg"') < h.index('class="lm-dl')


def test_full_answer_does_not_count_toward_the_length():
    assert handoff_rules.length_problems(dict(BASE, full_answer=FULL)) == []


def test_full_answer_needs_the_short_answer():
    card = dict(BASE, full_answer=FULL)
    del card["answer"]
    card["compare"] = {"before": ["a"], "after": ["b"]}
    assert any("full_answer" in p for p in handoff_rules.format_problems(card))
    assert any("full_answer" in p for p in handoff_rules.format_problems(dict(BASE, full_answer="  ")))
    assert not any("full_answer" in p for p in handoff_rules.format_problems(dict(BASE, full_answer=FULL)))


def test_the_instructions_describe_it():
    assert '"full_answer"' in handoff_rules.INSTRUCTIONS


def test_the_instructions_describe_the_switch():
    text = handoff_rules.INSTRUCTIONS
    assert "Short / Full / Plain words" in text and "In short" not in text


# ── The switch in a real browser: the real last-card.js and last-card.css ──

def _page_html(card):
    static = ROOT / "static"
    css = (static / "css" / "last-card.css").read_text(encoding="utf-8")
    js = LAST_CARD.read_text(encoding="utf-8")
    return f"""<!doctype html><html data-theme="dark"><head><meta charset="utf-8"><style>{css}</style></head>
<body><div class="msg assistant"><div class="msg-body" id="slot"></div></div>
<script>{js}</script>
<script>document.getElementById('slot').innerHTML = VNLastCard.render({json.dumps(card)}, {{}});</script></body></html>"""


@pytest.fixture(scope="module")
def browser():
    from tests._browser import shared_browser_scope
    with shared_browser_scope() as b:
        yield b


def test_tapping_a_version_shows_only_that_version(browser):
    pg = browser.new_page(viewport={"width": 390, "height": 844})
    try:
        pg.set_content(_page_html(dict(BASE, full_answer=FULL)))
        shown = "() => [...document.querySelectorAll('.lm-depth-p')].filter(p => p.offsetParent !== null).map(p => p.dataset.depth)"
        pressed = "() => [...document.querySelectorAll('.lm-depth-b')].filter(b => b.getAttribute('aria-pressed') === 'true').map(b => b.dataset.depth)"
        assert pg.evaluate(shown) == ["short"] and pg.evaluate(pressed) == ["short"]
        pg.click('.lm-depth-b[data-depth="full"]')
        assert pg.evaluate(shown) == ["full"] and pg.evaluate(pressed) == ["full"]
        assert pg.locator('.lm-depth-p[data-depth="full"] table').is_visible()
        pg.click('.lm-depth-b[data-depth="plain"]')
        assert pg.evaluate(shown) == ["plain"] and pg.evaluate(pressed) == ["plain"]
        pg.click('.lm-depth-b[data-depth="short"]')
        assert pg.evaluate(shown) == ["short"]
        # the three tabs sit on one line on a phone
        tops = pg.evaluate("() => [...document.querySelectorAll('.lm-depth-b')].map(b => Math.round(b.getBoundingClientRect().top))")
        assert len(set(tops)) == 1
    finally:
        pg.close()
