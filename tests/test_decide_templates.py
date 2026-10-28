"""The Decide popup's templates (owner's pick A, 2026-10-28,
static/scratch/decide-motion.html).

A decision reply may carry a design-choice read-out (strengths, weaknesses,
opportunities, risks, effort, undo, risk, fit) and screens to show in a Web
or Phone frame.  The card puts them on the Decide button for the popup, and
the handoff length check does not count them: they are drawn only inside the
popup, one choice at a time.  Runs the real ``static/js/last-card.js`` in node.
"""

import html
import json
import shutil
import subprocess
from pathlib import Path

import pytest

from daemon import smart_termination as st
from daemon.handoff_rules import length_problems

LAST_CARD = Path(__file__).resolve().parents[1] / "static" / "js" / "last-card.js"

SCRIPT = r"""
global.window = global;
global.location = { host: '127.0.0.1:5050' };
global.document = { addEventListener() {}, querySelectorAll() { return []; } };
eval(require('fs').readFileSync(process.argv[1], 'utf8'));
process.stdout.write(VNLastCard.render(JSON.parse(process.argv[2]), {}));
"""

REPLIES = [
    {"key": "A", "label": "Red stripe on the card", "detail": "Keeps its column.", "reply": "Go with A",
     "strengths": ["Nothing moves", "Seen at a glance", "", 7] + ["more"] * 6,
     "weaknesses": ["Colour alone"], "opportunities": ["Same stripe for review"], "risks": ["Competes with errors"],
     "effort": "small", "undo": "easy", "risk": "low", "fit": "you want the board to look the same",
     "screens": [{"label": "Board", "url": "/static/scratch/x.html#a", "device": "phone"},
                 {"label": "Elsewhere", "url": "https://example.com/x"},
                 {"label": "Script", "url": "javascript:alert(1)"}]},
    {"key": "B", "label": "Its own Blocked column", "reply": "Go with B"},
]


def _card(replies):
    return {"type": "needs_you", "headline": "Pick how the board marks a blocked task", "context": "x",
            "needs": {"kind": "decision", "ask": "Which marking?", "why": "y", "replies": replies}}


@pytest.fixture(scope="module")
def data():
    if shutil.which("node") is None:
        pytest.skip("node not installed")
    r = subprocess.run(["node", "-e", SCRIPT, str(LAST_CARD), json.dumps(_card(REPLIES))],
                       capture_output=True, text=True, timeout=30, check=True)
    h = r.stdout
    assert h.count('class="lm-decide"') == 1
    return json.loads(html.unescape(h.split('data-decide="', 1)[1].split('"', 1)[0]))


def test_the_read_out_rides_on_the_decide_button(data):
    a, b = data["replies"]
    assert a["weaknesses"] == ["Colour alone"] and a["risks"] == ["Competes with errors"]
    assert (a["effort"], a["undo"], a["risk"]) == ("small", "easy", "low")
    assert a["fit"] == "you want the board to look the same"
    # blank and non-string lines dropped, at most six kept
    assert a["strengths"][:2] == ["Nothing moves", "Seen at a glance"] and len(a["strengths"]) == 6
    assert all(isinstance(x, str) and x for x in a["strengths"])
    # a reply without template fields stays a plain slide
    for k in ("strengths", "screens", "fit", "effort"):
        assert k not in b


def test_only_frameable_screens_are_kept(data):
    a = data["replies"][0]
    assert a["screens"] == [{"label": "Board", "url": "/static/scratch/x.html#a", "device": "phone"}]


def test_template_fields_do_not_count_toward_the_card_length():
    big = [dict(r, strengths=["a strength line with a good number of words in it"] * 6,
                weaknesses=["a weakness line with a good number of words in it"] * 6) for r in REPLIES]
    big = [{k: v for k, v in r.items() if k != "screens"} for r in big]
    big[0]["strengths"] = big[0]["strengths"][:6]
    assert length_problems(_card(big)) == []


def test_a_template_line_still_has_the_line_cap():
    r = dict(REPLIES[1], risks=["word " * 30])
    out = length_problems(_card([REPLIES[0], r]))
    assert any("needs.replies[1].risks[0] is 30 words" in x for x in out)


def test_sessions_are_told_about_the_templates():
    for field in ('"strengths"', '"weaknesses"', '"opportunities"', '"risks"', '"fit"', '"screens"', "web|phone|both"):
        assert field in st.INSTRUCTIONS


# ── Each screen shows its own option (owner, 2026-10-28: "these are all
# showing as the same, and that keeps happening its a systemic failure") ──
SCREENS_SCRIPT = r"""
global.window = global;
global.location = { host: '127.0.0.1:5050' };
global.document = { addEventListener() {}, querySelectorAll() { return []; },
                    documentElement: { getAttribute() { return 'dark'; } } };
eval(require('fs').readFileSync(process.argv[1], 'utf8'));
// A page with four tagged sections and ids v-now, v-a, v-b, v-c.
const els = ['now', 'A', 'B', 'C'].map(k => ({ id: 'v-' + k.toLowerCase(), opt: k }));
const d = {
  getElementById: id => els.find(e => e.id === id) || null,
  querySelector: sel => { const m = /data-option="([^"]*)" i/.exec(sel); return m ? els.find(e => e.opt.toLowerCase() === m[1].toLowerCase()) || null : null; },
  querySelectorAll: () => [],
};
const pick = (frag, key) => { const t = VNLastCard.screenOption(d, frag, key); return t ? t.opt : null; };
process.stdout.write(JSON.stringify({
  src: ['/static/scratch/p.html#v-a', '/static/scratch/p.html?x=1#b', '/static/scratch/p.html', '/static/scratch/p.html?theme=light#a',
        'http://localhost:3000/#a'].map(VNLastCard.themedSrc),
  opt: [pick('v-b', 'A'), pick('c', 'A'), pick('', 'B'), pick('nothing-here', 'C'), pick('', 'Z')],
}));
"""


@pytest.fixture(scope="module")
def screens():
    if shutil.which("node") is None:
        pytest.skip("node not installed")
    r = subprocess.run(["node", "-e", SCREENS_SCRIPT, str(LAST_CARD)],
                       capture_output=True, text=True, timeout=30, check=True)
    return json.loads(r.stdout)


def test_the_theme_goes_before_the_fragment(screens):
    # Appended after it, "#v-a" became "#v-a?theme=dark" and every screen
    # opened at the top of the same page.
    assert screens["src"] == ["/static/scratch/p.html?theme=dark#v-a",
                              "/static/scratch/p.html?x=1&theme=dark#b",
                              "/static/scratch/p.html?theme=dark",
                              "/static/scratch/p.html?theme=light#a",
                              "http://localhost:3000/#a"]


def test_each_screen_finds_its_own_option(screens):
    # By the url's fragment first, then the reply's key; none: whole page.
    assert screens["opt"] == ["B", "C", "B", "C", None]
