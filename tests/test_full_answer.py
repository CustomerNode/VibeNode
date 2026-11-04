""""Full answer" on an answer card: one version in the Answer band.

An answer card has ONE Answer band with a Short / Full / Plain words switch:
"answer" is Short (open), the optional "full_answer" is Full, and the card's
"explained" is Plain words.  On a card with an answer there is no separate
Explain more up top and no Full answer fold.  "full_answer" is not counted
toward the card's length and needs the short "answer" beside it.  There is
no separate "In short" fold, which would say the same thing again.
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
from engine import handoff_rules  # noqa: E402

LAST_CARD = ROOT / "src" / "web" / "static" / "js" / "last-card.js"

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
        "explained": EX, "needs": {"kind": "none", "why": "Answered.", "next": "Nothing planned."},
        "what_was_done": "Compared title formats.", "checked": "No research beyond base training data."}


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
    return h[h.index(">Summary<"):]


def test_one_summary_with_short_plain_words_and_full_as_levels(out):
    """Levels, not a switch: Short open, Plain words and Full previewed with
    Show more."""
    band = _band(out["full"])
    assert ">In short<" not in out["full"] and 'class="lm-depth-b"' not in band
    rows = re.findall(r'<div class="vn-sub( shut)?"><div class="dt">([^<]+)<', band)
    # Simplest to most detailed: Short, Plain words, Full.
    assert rows == [("", "Short"), (" shut", "Plain words"), (" shut", "Full")]
    assert band.count('class="vn-more"') == 2 and band.count('class="vn-less-top"') == 2
    panes = re.findall(r'<div class="lm-depth-p" data-depth="(\w+)"', band)
    assert panes == ["short", "plain", "full"]
    # Short is the answer; Plain words is "explained"; Full is the markdown (a heading, a list, a table)
    short, plain, full = (band.index(f'class="lm-depth-p" data-depth="{k}"') for k in ("short", "plain", "full"))
    assert short < band.index("Word order isn") < plain < band.index("Plain words. ", plain) < full < band.index("<table")
    # closed levels carry a plain-text preview before their rendered text
    prevs = re.findall(r'<p class="vn-prev">([^<]*)</p>', band)
    assert len(prevs) == 2 and all(p and "#" not in p and "|" not in p for p in prevs)
    assert "<li" in band[full:]


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
    assert 'class="lm-depth"' not in band and 'class="lm-depth-b"' not in band and "Word order isn" in band


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
    assert "Short / Plain words / Full" in text and "In short" not in text


# ── The switch in a real browser: the real last-card.js and last-card.css ──

def _page_html(card):
    static = ROOT / "src" / "web" / "static"
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


def test_show_more_opens_a_level_and_copy_offers_each_level(browser):
    pg = browser.new_page(viewport={"width": 390, "height": 844})
    try:
        pg.set_content(_page_html(dict(BASE, full_answer=FULL)))
        h = "k => document.querySelector('.lm-depth-p[data-depth=\"' + k + '\"]').getBoundingClientRect().height"
        assert pg.evaluate(h, "plain") < 60 and pg.evaluate(h, "full") < 60      # previews: a couple of lines
        # a closed level previews plain sentences, never its rendered markdown
        prev = pg.inner_text('.vn-sub:has([data-depth="full"]) .vn-prev')
        assert prev and "|" not in prev and "#" not in prev
        assert not pg.locator('.lm-depth-p[data-depth="full"] table').is_visible()
        pg.click('.vn-sub:has([data-depth="full"]) .vn-more')
        assert pg.evaluate(h, "full") > 100 and pg.locator('.lm-depth-p[data-depth="full"] table').is_visible()
        assert not pg.is_visible('.vn-sub:has([data-depth="full"]) .vn-prev')
        assert pg.inner_text('.vn-sub:has([data-depth="full"]) .vn-more') == "Show less"
        pg.click('.vn-sub:has([data-depth="full"]) .vn-more')
        assert pg.evaluate(h, "full") < 60
        # an open level also folds from under its label
        assert not pg.is_visible('.vn-sub:has([data-depth="full"]) .vn-less-top')
        pg.click('.vn-sub:has([data-depth="full"]) .vn-more')
        assert pg.is_visible('.vn-sub:has([data-depth="full"]) .vn-less-top')
        pg.click('.vn-sub:has([data-depth="full"]) .vn-less-top')
        assert pg.evaluate(h, "full") < 60 and pg.inner_text('.vn-sub:has([data-depth="full"]) .vn-more') == "Show more"
        # Copy opens a menu of the levels; each copies its own text, All copies them under their names
        pg.evaluate("() => { window.__copied = []; window.vnCopyText = t => { window.__copied.push(t); return true; }; }")
        assert not pg.is_visible('.vn-cm')
        pg.click('.lm-copy-menu')
        assert pg.inner_text('.vn-cm').split() == ["Short", "Plain", "words", "Full", "All", "three"]
        pg.click('.vn-cm [data-copy="full"]')
        assert not pg.is_visible('.vn-cm')
        pg.click('.lm-copy-menu'); pg.click('.vn-cm [data-copy="all"]')
        pg.wait_for_function("() => window.__copied.length === 2")
        got = pg.evaluate("() => window.__copied")
        assert got[0] == FULL.strip()
        assert got[1].startswith("Short\n\n" + BASE["answer"]) and "\n\nFull\n\n" in got[1]
    finally:
        pg.close()


# ── Every summary reads the same ──

DV = {"type": "deliverable", "title": "Token cost table", "headline": "The build used 18M tokens, about $6.88.",
      "answer": "Main agent only; cached reads were 98% of tokens but $3.59 of the cost.",
      "explained": EX, "zoom": BASE["zoom"], "needs": {"kind": "none", "why": "Answered.", "next": "Nothing planned."},
      "what_was_done": "Added up the logs.", "checked": "Read the session logs.",
      "deliverable": {"kind": "report", "title": "Token use", "body": "| Agent | Tokens |\n|---|---|\n| Main | 18M |",
                      "files": [{"label": "The table as CSV", "url": "/static/scratch/tokens.csv", "size": "1 KB"}]}}
DONE = {"type": "done", "title": "Archive button", "headline": "Sessions can be archived from the right-click menu.",
        "answer": "Archive sits under Rename; archived sessions leave the list and come back from the filter.",
        "full_answer": "## What changed\n- the menu item\n- the filter", "explained": EX, "zoom": BASE["zoom"],
        "links": [{"label": "Try it", "kind": "link", "url": "/"}],
        "what_was_done": "Added Archive.", "checked": "Ran the archive tests.",
        "needs": {"kind": "none", "why": "Done.", "next": "Nothing planned."}}


@pytest.fixture(scope="module")
def out2():
    if shutil.which("node") is None:
        pytest.skip("node not installed")
    r = subprocess.run(["node", "-e", SCRIPT, str(LAST_CARD), json.dumps({"dv": DV, "done": DONE})],
                       capture_output=True, text=True, timeout=30, check=True)
    return json.loads(r.stdout)


def test_a_deliverables_text_sits_in_review_and_respond_not_in_full(out2):
    """The summary is about the summary; the artifact created sits in Review
    & respond."""
    h = out2["dv"]
    summ = h[h.index(">Summary<"):h.index(">Review &amp; respond<")]
    assert "lm-dvopen" not in summ and "lm-dvbox" not in summ
    rr = h[h.index(">Review &amp; respond<"):]
    dv = rr[rr.index('<div class="dt">Deliverable<'):]
    assert "lm-dvbox" in dv and "lm-dvopen" in dv and 'data-dvact="copy"' in dv
    assert "tokens.csv" in dv            # the file keeps its Download button


def test_every_kind_of_summary_has_the_same_switch(out2):
    h = out2["done"]
    band = h[h.index(">Summary<"):]
    assert re.findall(r'class="lm-depth-p" data-depth="(\w+)"', band) == ["short", "plain", "full"]
    assert re.findall(r'data-copy="(\w+)"', band) == ["short", "plain", "full", "all"]
    assert "Explain more" not in h


def test_every_summary_has_a_copy_button(out, out2):
    for h in (out["full"], out["alone"], out2["dv"], out2["done"]):
        assert h.count('class="lm-copy-menu"') == 1


def test_the_rules_ask_every_card_for_short_and_full():
    from engine import smart_termination as st
    assert st.depth_problems(dict(BASE, full_answer=FULL)) == []
    # A deliverable's body is not its Full.

    assert any('"full_answer"' in p for p in st.depth_problems(DV))
    assert st.depth_problems(dict(DV, full_answer=FULL)) == []
    assert st.depth_problems(DONE) == []
    no_short = dict(DONE)
    del no_short["answer"]
    assert any('"answer"' in p for p in st.depth_problems(no_short))
    assert any('"full_answer"' in p for p in st.depth_problems(dict(BASE)))
    files_only = dict(DV, deliverable={"files": DV["deliverable"]["files"]})
    assert any('"full_answer"' in p for p in st.depth_problems(files_only))
