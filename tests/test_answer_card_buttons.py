"""An answer and a text to send get their own end buttons (owner's pick A, 2026-10-28).

On "Nothing needed" followed by the finished-fix buttons under an answer:
"obviously i have to make a decision here", then "the ctas for a done state
were already fine dont touch them. its the other states".  An answer ends in
"Read it": Got it / Do what it suggested (only with the card's ``offer``) /
Ask more / Standby.  A text to send ends in "Send it": Sent it / Change it /
Standby.  A finished fix keeps its Next band.  A still-running card with a
clock time says "Nothing needed until <time>".  Runs the real
``static/js/last-card.js`` in node with the browser globals stubbed.
"""

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
LAST_CARD = ROOT / "static" / "js" / "last-card.js"
CSS = ROOT / "static" / "css" / "last-card.css"

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

NONE = {"kind": "none", "why": "This answers your question.", "next": "A single click still opens a session, unchanged."}


@pytest.fixture(scope="module")
def out():
    if shutil.which("node") is None:
        pytest.skip("node not installed")
    cards = {
        "answer": {"type": "explain", "title": "T", "headline": "H", "answer": "A.", "needs": NONE},
        "answer_offer": {"type": "explain", "title": "T", "headline": "H", "answer": "A.", "needs": NONE,
                         "offer": "Give answer cards their own buttons"},
        "answer_late": {"type": "explain", "title": "T", "headline": "H", "answer": "A.", "needs": NONE, "late": True},
        "email": {"type": "deliverable", "title": "T", "headline": "H", "needs": NONE,
                  "deliverable": {"kind": "email", "title": "Lease", "body": "Hi"}},
        "report": {"type": "deliverable", "title": "T", "headline": "H", "needs": NONE,
                   "deliverable": {"kind": "report", "title": "Q3", "body": "Up 12%"}},
        "done": {"type": "done", "title": "T", "headline": "H", "status": {"done": ["a"]}, "needs": NONE},
        "working_clock": {"type": "working", "title": "T", "headline": "H",
                          "needs": {"kind": "none", "why": "w", "next": "I report then.", "eta": "~11:40"}},
        "working_span": {"type": "working", "title": "T", "headline": "H",
                         "needs": {"kind": "none", "why": "w", "next": "I report then.", "eta": "~12 min"}},
    }
    r = subprocess.run(["node", "-e", SCRIPT, str(LAST_CARD), json.dumps(cards)],
                       capture_output=True, text=True, timeout=30, check=True)
    return json.loads(r.stdout)


def _band(h, label):
    return h.split('<div class="dt">' + label + '</div>', 1)[1].split('<div class="lm-dl', 1)[0]


def test_an_answer_ends_in_read_it_with_its_own_buttons(out):
    h = out["answer"]
    assert "Nothing needed" not in h and '<div class="dt">Next</div>' not in h
    b = _band(h, "Read it")
    for act in ("gotit", "ask", "standby"):
        assert 'data-verdict="' + act + '"' in b
    assert 'data-verdict="offer"' not in b          # nothing suggested, no button
    assert "single click" not in h                   # the session's filler is not drawn


def test_an_offer_becomes_do_what_it_suggested(out):
    b = _band(out["answer_offer"], "Read it")
    assert 'data-verdict="offer" data-offer="Give answer cards their own buttons"' in b
    assert "Do what it suggested" in b


def test_a_text_to_send_ends_in_send_it_and_a_report_in_read_it(out):
    assert "Nothing needed" not in out["email"]
    b = _band(out["email"], "Send it")
    assert 'data-verdict="sent"' in b and 'data-verdict="change"' in b
    assert '<div class="dt">Read it</div>' in out["report"]


def test_a_finished_fix_keeps_its_next_band(out):
    b = _band(out["done"], "Next")
    assert "<b>Nothing needed.</b>" in b
    for act in ("done", "next", "bad", "standby"):
        assert 'data-verdict="' + act + '"' in b


def test_a_running_card_says_until_when(out):
    assert "<b>Nothing needed until 11:40.</b>" in out["working_clock"]
    assert "lm-eta" not in _band(out["working_clock"], "Next")
    span = _band(out["working_span"], "Next")
    assert "<b>Nothing needed.</b>" in span and "12 min" in span


def test_a_late_answer_has_no_buttons_and_its_band_hides(out):
    assert 'class="lm-finish"' not in out["answer_late"]
    css = CSS.read_text()
    assert ".lm-dl.lm-act:not(:has(.lm-finish:not([hidden])))" in css


def test_each_status_shows_only_its_own_rows():
    js = LAST_CARD.read_text()
    assert re.search(r"read:\s*'read',\s*send:\s*'send'", js), "_syncDone must give Read and Send their own state"
    css = CSS.read_text()
    assert '.lm .lm-finish[data-state="read"] .v-gotit' in css
    assert '.lm .lm-finish[data-state="send"] .v-sent' in css
    assert '.lm .lm-finish[data-state="read"] .v-bad' in css   # the finished-fix rows hide under an answer
