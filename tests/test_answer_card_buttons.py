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
                       capture_output=True, text=True, encoding="utf-8", timeout=30, check=True)
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


_READ_ROWS = ("offer", "gotit", "ask", "standby")   # the rows a Read card shows


def _read_order(h):
    b = _band(h, "Read it")
    seen = re.findall(r'data-verdict="(\w+)"', b)
    return [a for a in seen if a in _READ_ROWS]


def test_offer_order_is_do_ask_gotit_standby(out):
    """Owner, 2026-10-28: every set leads with the expected answer and ends on
    Standby.  With an offer: Do what it suggested · Ask more · Got it ·
    Standby, and Got it drawn exactly once."""
    assert _read_order(out["answer_offer"]) == ["offer", "ask", "gotit", "standby"]


def test_without_an_offer_got_it_stays_first(out):
    assert _read_order(out["answer"]) == ["gotit", "ask", "standby"]


SYNC = r"""
global.window = global;
global.location = { host: '127.0.0.1:5050' };
const mk = (card) => {
  const el = { attrs: { 'data-card': JSON.stringify(card) }, getAttribute(k) { return this.attrs[k] || null; },
               closest() { return null; }, querySelector() { return null; }, querySelectorAll() { return []; } };
  const fin = { hidden: true, dataset: {}, classList: { toggle() {} }, closest: s => s === '.lm' ? el : null };
  return { el, fin };
};
const cards = JSON.parse(process.argv[2]);
const out = {};
for (const [k, c] of Object.entries(cards)) {
  const { el, fin } = mk(c);
  const log = { querySelectorAll: s => s === '.lm.skim' ? [el] : s === '.lm-finish' ? [fin] : [] };
  global.document = { addEventListener() {}, querySelectorAll() { return []; },
                      getElementById: id => id === 'live-log' ? log : null };
  global.liveSessionId = 's1';
  global.SessionVerb = { display: () => process.argv[3] };
  delete global.VNLastCard;
  eval(require('fs').readFileSync(process.argv[1], 'utf8'));
  VNLastCard.syncDone();
  out[k] = [fin.hidden, fin.dataset.state || ''];
}
process.stdout.write(JSON.stringify(out));
"""


def _sync(cards, shown):
    r = subprocess.run(["node", "-e", SYNC, str(LAST_CARD), json.dumps(cards), shown],
                       capture_output=True, text=True, encoding="utf-8", timeout=30)
    assert r.returncode == 0, r.stderr
    return json.loads(r.stdout)


def test_an_answer_always_gets_answer_buttons():
    """2026-10-28: an answer card showed Done · Something else · Problem when
    the sidebar had no card for the session and fell back to Review.  The
    newest card's own kind picks the rows."""
    if shutil.which("node") is None:
        pytest.skip("node not installed")
    cards = {"answer": {"type": "explain", "title": "T", "headline": "H", "answer": "A.", "needs": NONE},
             "email": {"type": "deliverable", "title": "T", "headline": "H", "needs": NONE,
                       "deliverable": {"kind": "email", "title": "x", "body": "y"}},
             "done": {"type": "done", "title": "T", "headline": "H", "needs": NONE}}
    out = _sync(cards, "review")
    assert out["answer"] == [False, "read"]
    assert out["email"] == [False, "send"]
    assert out["done"] == [False, "verdict"]
    # A question, Standby or a marked Done keeps its own rows.
    assert _sync({"answer": cards["answer"]}, "standby")["answer"] == [False, "standby"]
    assert _sync({"answer": cards["answer"]}, "decide")["answer"] == [False, "question"]


def test_a_text_to_send_ends_in_send_it_and_a_report_in_read_it(out):
    assert "Nothing needed" not in out["email"]
    b = _band(out["email"], "Send it")
    assert 'data-verdict="sent"' in b and 'data-verdict="change"' in b
    assert '<div class="dt">Read it</div>' in out["report"]


def test_a_finished_fix_keeps_its_next_band(out):
    # Next says what the session does, and the verdict chips sit right under
    # that line in the same band (owner's pick A, 2026-10-28,
    # static/scratch/card-verdict-in-next.html: "verdict should just fall
    # under next"); there is no separate "Your verdict" band.
    b = _band(out["done"], "Next")
    assert '<b class="lm-nn">Nothing needed.</b>' in b
    assert b.index('class="lm-nx"') < b.index('class="lm-yv lm-yv-in"')
    for act in ("done", "next", "bad", "standby"):
        assert 'data-verdict="' + act + '"' in b
    assert '<div class="dt">Your verdict</div>' not in out["done"]
    css = (ROOT / "static/css/last-card.css").read_text("utf-8")
    # the chips hide with their wrapper; "Nothing needed." goes while they show
    assert ".lm .lm-yv:not(:has(.lm-finish:not([hidden]))) { display: none; }" in css
    assert ".lm:has(.lm-yv .lm-finish:not([hidden])) .lm-nn { display: none; }" in css


def test_a_running_card_says_until_when(out):
    assert "<b>Nothing needed until 11:40.</b>" in out["working_clock"]
    assert "lm-eta" not in _band(out["working_clock"], "Next")
    span = _band(out["working_span"], "Next")
    assert '<b class="lm-nn">Nothing needed.</b>' in span and "12 min" in span
    assert "lm-yv-in" not in out["working_span"]


def test_a_late_answer_has_no_buttons_and_its_band_hides(out):
    assert 'class="lm-finish"' not in out["answer_late"]
    css = CSS.read_text(encoding="utf-8")
    assert ".lm-dl.lm-act:not(:has(.lm-finish:not([hidden])))" in css


def test_each_status_shows_only_its_own_rows():
    js = LAST_CARD.read_text(encoding="utf-8")
    assert re.search(r"read:\s*'read',\s*send:\s*'send'", js), "_syncDone must give Read and Send their own state"
    css = CSS.read_text(encoding="utf-8")
    assert '.lm .lm-finish[data-state="read"] .v-gotit' in css
    assert '.lm .lm-finish[data-state="send"] .v-sent' in css
    assert '.lm .lm-finish[data-state="read"] .v-bad' in css   # the finished-fix rows hide under an answer
