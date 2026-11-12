"""Do what it suggested gets an info button.

A card with an ``offer`` carries ``offer_info`` {answer, explained,
full_answer}; ``src/web/static/js/last-card.js`` draws an info button beside the
"Do what it suggested" row (and beside its chip on a Done card) that opens
them as Short / Plain words / Full, the summary's own switch and Copy.
``src/engine/handoff_rules.offer_info_problems`` sends back an offer without it.

Rules run in Python; the drawing runs the real file in node; the clicks run
the real file and CSS in headless Chromium (skips without Playwright).
"""
import copy
import json
import shutil
import subprocess
from pathlib import Path

import pytest

from engine import handoff_rules as hr
from tests._browser import shared_browser_scope

ROOT = Path(__file__).resolve().parents[1]
LAST_CARD = ROOT / "src" / "web" / "static" / "js" / "last-card.js"
CSS = ROOT / "src" / "web" / "static" / "css" / "last-card.css"

INFO = {
    "answer": "I would add a check sessions run on their summary before they stop.",
    "explained": "## What it is\n\nRight now a session **guesses** how long its summary is and the "
                 "rules count it afterwards, so a wrong guess means a rewrite.\n\n## What changes "
                 "for you\n\nMost rewrites never happen, so answers reach you sooner.",
    "full_answer": "## Plan\n\nEverything behind the self-check.",
}
CARD = {
    "type": "explain", "path": ["Summaries"], "title": "Invalid summary responses",
    "headline": "Most send-backs are summaries that are too long.",
    "answer": "Most were too long.", "explained": " ".join(["word"] * 45), "full_answer": "More.",
    "what_was_done": "Scanned transcripts", "checked": "Counted send-backs",
    "needs": {"kind": "none", "why": "Answered.", "next": "Nothing runs by itself."},
    "offer": "Build a summary self-check sessions run first", "offer_info": INFO,
}


# ── The rule ──────────────────────────────────────────────────────────────

def test_an_offer_with_its_explanation_passes():
    assert hr.offer_info_problems(CARD) == []


def test_an_offer_without_its_explanation_is_sent_back():
    card = copy.deepcopy(CARD)
    del card["offer_info"]
    assert any("offer_info" in p for p in hr.offer_info_problems(card))
    for k in ("answer", "explained", "full_answer"):
        card = copy.deepcopy(CARD)
        del card["offer_info"][k]
        assert k in " ".join(hr.offer_info_problems(card)), k


def test_thin_plain_words_is_sent_back():
    card = copy.deepcopy(CARD)
    card["offer_info"]["explained"] = "Short and thin."
    assert any("offer_info.explained" in p for p in hr.offer_info_problems(card))


def test_no_offer_needs_no_explanation_and_a_stray_one_is_flagged():
    card = copy.deepcopy(CARD)
    del card["offer"], card["offer_info"]
    assert hr.offer_info_problems(card) == []
    card["offer_info"] = INFO
    assert hr.offer_info_problems(card)


def test_the_explanation_is_outside_the_cards_length_and_its_plan_is_not_unfinished_work():
    card = copy.deepcopy(CARD)
    base = hr.length_problems(card)
    card["offer_info"]["explained"] += " " + " ".join(["word"] * 400)
    card["offer_info"]["full_answer"] = "Next steps: you would then review it. " + " ".join(["word"] * 300)
    assert hr.length_problems(card) == base
    hits = hr._checks(card, "")
    assert not any(where.startswith("offer_info") for _, _, where, _ in hits), hits


def test_the_instructions_name_it():
    assert '"offer_info"' in hr.INSTRUCTIONS


# ── The drawing ──────────────────────────────────────────────────────────

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


@pytest.fixture(scope="module")
def out():
    if shutil.which("node") is None:
        pytest.skip("node not installed")
    plain = dict(CARD)
    plain.pop("offer_info")
    cards = {"with": CARD, "without": plain}
    r = subprocess.run(["node", "-e", SCRIPT, str(LAST_CARD), json.dumps(cards)],
                       capture_output=True, text=True, encoding="utf-8", timeout=30, check=True)
    return json.loads(r.stdout)


def test_the_offer_row_has_an_info_button_and_a_three_tab_panel(out):
    h = out["with"]
    assert h.count('class="v-offer-info"') == 1
    wrap = h.split('class="v-offer-wrap">', 1)[1]
    assert wrap.index('data-verdict="offer"') < wrap.index('class="v-offer-info"')
    xp = h.split('class="v-offer-xp lm-depth-scope" hidden>', 1)[1]
    tabs = [s.split(">", 1)[1].split("<", 1)[0] for s in xp.split('class="lm-depth-b"')[1:4]]
    assert tabs == ["Short", "Plain words", "Full"]
    assert "lm-depth-copy" in xp and "<b>guesses</b>" in xp


def test_an_offer_without_an_explanation_keeps_the_plain_row(out):
    assert "v-offer-info" not in out["without"] and "v-offer-xp" not in out["without"]
    assert 'data-verdict="offer"' in out["without"]


def test_the_row_styles_never_reach_the_panels_buttons():
    css = CSS.read_text(encoding="utf-8")
    assert ".lm .lm-verdict button {" not in css and ".lm .lm-yv .lm-verdict button {" not in css
    assert ".lm .lm-verdict button:where([data-verdict]) {" in css


# ── The clicks, in a real browser ─────────────────────────────────────────

@pytest.fixture(scope="module")
def browser():
    with shared_browser_scope() as b:
        yield b


def _open(browser, card, tmp_path, state):
    f = tmp_path / "card.html"
    f.write_text(f"""<!doctype html><html><head><meta charset="utf-8">
<link rel="stylesheet" href="{CSS.as_uri()}"></head><body><div id="live-log">
<div class="msg asst"><div class="msg-body"><div class="vn-card-ph" data-card='{json.dumps(card).replace("'", "&#39;")}'></div></div></div></div>
<script>var liveSessionId='s1', sessionKinds={{s1:'idle'}}; window.__sent=[];
window._liveSubmitDirect=(sid,text)=>{{ window.__sent.push([sid,text]); }};
window.fetch=()=>Promise.reject(new Error('offline'));</script>
<script src="{LAST_CARD.as_uri()}"></script>
<script>VNLastCard.renderIn(document.getElementById('live-log'));</script></body></html>""", encoding="utf-8")
    page = browser.new_page()
    errors = []
    page.on("pageerror", lambda e: errors.append(str(e)))
    page.goto(f.as_uri(), wait_until="load")
    assert not errors, errors
    page.wait_for_function("document.querySelectorAll('.lm.skim').length === 1")
    page.evaluate("s => document.querySelectorAll('.lm-finish').forEach(f => { f.hidden = false; f.dataset.state = s; })", state)
    return page


@pytest.mark.parametrize("ctype,state", [("explain", "read"), ("done", "verdict")])
def test_the_info_button_opens_the_explanation_and_sends_nothing(browser, tmp_path, ctype, state):
    card = dict(CARD, type=ctype)
    if ctype == "done":
        card["links"] = [{"label": "x", "kind": "link", "url": "/"}]
    page = _open(browser, card, tmp_path, state)
    try:
        info = page.locator(".v-offer-info:visible")
        assert info.count() == 1
        xp = page.locator(".v-offer-xp:visible")
        assert xp.count() == 0
        info.click()
        xp = page.locator(".v-offer-xp:visible")
        assert xp.count() == 1 and "add a check sessions run" in xp.inner_text()
        assert info.get_attribute("aria-expanded") == "true"
        xp.locator(".lm-depth-b[data-depth=plain]").click()
        assert "guesses" in xp.inner_text() and "add a check" not in xp.inner_text()
        # the panel's tab buttons are tabs, not verdict rows
        h = page.evaluate("document.querySelector('.v-offer-xp:not([hidden]) .lm-depth-b').getBoundingClientRect().height")
        assert h < 40
        assert page.evaluate("window.__sent") == []
        info.click()
        assert page.locator(".v-offer-xp:visible").count() == 0
        # the row itself still says yes to the suggestion
        page.locator("[data-verdict=offer]:visible").click()
        assert page.evaluate("window.__sent") == [["s1", "Yes, do that: " + CARD["offer"]]]
    finally:
        page.close()
