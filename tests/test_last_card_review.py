"""A decision card's page opens full screen over the chat (owner, 2026-10-27).

"i have to click to open another tab and then come back here?  just make a
review card that goes fullscreen overlay with the options and a selector".
A card whose needs carry replies and a look link to a page this app can frame
gets ONE "Review the options" button; the overlay shows the page with the
card's own choices in a picker.  Runs the real ``static/js/last-card.js`` in
node with the browser globals stubbed.
"""

import html
import json
import shutil
import subprocess
from pathlib import Path

import pytest

LAST_CARD = Path(__file__).resolve().parents[1] / "static" / "js" / "last-card.js"

SCRIPT = r"""
global.window = global;
global.location = { host: '127.0.0.1:5050' };
global.document = { addEventListener() {}, querySelectorAll() { return []; } };
eval(require('fs').readFileSync(process.argv[1], 'utf8'));
const cards = JSON.parse(process.argv[2]);
const out = {};
for (const [k, c] of Object.entries(cards)) out[k] = VNLastCard.render(c, {});
out._src = ['/static/scratch/x.html', 'http://127.0.0.1:5050/static/scratch/x.html#b', 'http://127.0.0.1:8000/app/review?t=1',
            'http://localhost:3000/', 'https://example.com/page', 'javascript:alert(1)', '//evil.example/x', '']
  .map(u => VNLastCard.reviewSrc(u));
process.stdout.write(JSON.stringify(out));
"""

LOOK = [{"label": "Both designs side by side", "kind": "link", "url": "/static/scratch/card-buttons.html"},
        {"label": "The spec", "kind": "link", "url": "https://example.com/spec"}]
REPLIES = [{"key": "A", "label": "Buttons inside the card", "reply": "a"},
           {"key": "B", "label": "Bar along the bottom", "reply": "b"}]


def _card(**needs):
    return {"type": "needs_you", "headline": "Pick one", "context": "x",
            "needs": dict({"kind": "decision", "ask": "Which?", "why": "y"}, **needs)}


@pytest.fixture(scope="module")
def out():
    if shutil.which("node") is None:
        pytest.skip("node not installed")
    cards = {
        "decision": _card(look=LOOK, replies=REPLIES),
        "no_replies": _card(kind="action", look=LOOK, steps=[{"text": "do it"}]),
        "external": _card(look=[LOOK[1]], replies=REPLIES),
        "done": {"type": "done", "headline": "Shipped", "context": "x",
                 "status": {"done": ["a"]}, "needs": {"kind": "none", "why": "y", "next": "z"}},
        "val_page": {"type": "done", "headline": "S", "context": "x", "needs": {"kind": "none", "why": "y"},
                     "validate": {"how": "Open a panel and press Back", "url": "/static/scratch/x.html",
                                  "later": "For real: the next time you open a panel"}},
        "val_web": {"type": "done", "headline": "S", "context": "x", "needs": {"kind": "none", "why": "y"},
                    "validate": {"how": "Open the site", "url": "https://example.com/"}},
        "val_none": {"type": "done", "headline": "S", "context": "x", "needs": {"kind": "none", "why": "y"},
                     "validate": {"how": "Send a message in any session"}},
        "val_shots": {"type": "done", "headline": "S", "context": "x", "needs": {"kind": "none", "why": "y"},
                      "validate": {"how": "Tap each preview", "url": "/static/scratch/x.html"},
                      "links": [{"label": "Desktop shot", "kind": "image", "url": "/static/scratch/d.png"},
                                {"label": "The page", "kind": "link", "url": "/static/scratch/x.html"}]},
        "val_shots2": {"type": "done", "headline": "S", "context": "x", "needs": {"kind": "none", "why": "y"},
                       "validate": {"how": "Tap each preview", "url": "/static/scratch/x.html"},
                       "links": [{"label": "Desktop shot", "kind": "image", "url": "/static/scratch/d.png"},
                                 {"label": "Phone shot", "kind": "image", "url": "/static/scratch/p.png"}]},
        "val_shots_noaddr": {"type": "done", "headline": "S", "context": "x", "needs": {"kind": "none", "why": "y"},
                             "validate": {"how": "Send a message in any session"},
                             "links": [{"label": "Desktop shot", "kind": "image", "url": "/static/scratch/d.png"}]},
        "ordered": {"type": "done", "title": "T", "headline": "S", "context": "x", "status": {"done": ["a"]},
                    "verified": [{"check": "c", "result": "r", "ok": True}],
                    "zoom": {"big": "b", "task": "t", "now": "n"}, "needs": {"kind": "none", "why": "y", "next": "z"}},
        "ordered_ask": {"type": "needs_you", "title": "T", "headline": "S", "context": "x",
                        "zoom": {"big": "b", "task": "t", "now": "n"},
                        "needs": {"kind": "action", "ask": "Log in", "why": "y", "steps": [{"text": "do it"}]}},
        "shots_only": {"type": "explain", "headline": "S", "context": "x", "answer": "a",
                       "needs": {"kind": "none", "why": "y"},
                       "links": [{"label": "Desktop shot", "kind": "image", "url": "/static/scratch/d.png"}]},
    }
    r = subprocess.run(["node", "-e", SCRIPT, str(LAST_CARD), json.dumps(cards)],
                       capture_output=True, text=True, timeout=30, check=True)
    return json.loads(r.stdout)


def test_a_decision_is_one_decide_button_carrying_its_page_and_choices(out):
    """Owner, 2026-10-28: "a decide button that expands to a full screen
    decision environment" (static/scratch/decide-button.html, variant A)."""
    h = out["decision"]
    assert h.count('class="lm-decide"') == 1
    # the band already says "Decide": the button must not say it again
    assert ">See the 2 choices<span class=\"go\">" in h
    assert ">Decide<" not in h.split('class="lm-decide"', 1)[1].split("</button>", 1)[0]
    # the choices and the page ride on the button, not as rows in the card
    assert "lm-row reply" not in h and "lm-review" not in h
    data = h.split('data-decide="', 1)[1].split('"', 1)[0]
    data = json.loads(html.unescape(data))
    assert data["page"] == "/static/scratch/card-buttons.html"
    assert [r["key"] for r in data["replies"]] == ["A", "B"]
    assert data["replies"][1]["reply"] == "b"
    # the reviewed page is not listed again; the other link still is
    assert h.count("card-buttons.html") == 1
    assert "example.com/spec" in h


def test_no_decide_or_review_button_without_choices(out):
    assert "lm-review" not in out["no_replies"]
    assert "lm-decide" not in out["no_replies"]


def test_a_decision_without_a_frameable_page_still_decides_full_screen(out):
    h = out["external"]
    assert "lm-review" not in h and h.count('class="lm-decide"') == 1
    data = json.loads(html.unescape(h.split('data-decide="', 1)[1].split('"', 1)[0]))
    assert data["page"] == ""
    assert "example.com/spec" in h


def test_review_src_frames_only_this_machine(out):
    assert out["_src"] == [
        "/static/scratch/x.html",
        "/static/scratch/x.html#b",
        "/api/preview/proxy?u=" + "http%3A%2F%2F127.0.0.1%3A8000%2Fapp%2Freview%3Ft%3D1",
        "/api/preview/proxy?u=" + "http%3A%2F%2Flocalhost%3A3000%2F",
        "", "", "", "",
    ]


# ── Approve / action replies stay buttons inside the card (design A, 2026-10-28) ──

def test_replies_are_buttons_in_the_card_with_the_recommended_one_filled():
    if shutil.which("node") is None:
        pytest.skip("node not installed")
    replies = [{"key": "A", "label": "Buttons inside the card", "reply": "a"},
               {"key": "B", "label": "Bar along the bottom", "reply": "b", "recommended": True}]
    cards = {"rec": _card(kind="approve", replies=replies), "none": _card(kind="approve", replies=[{"label": "Go", "reply": "go"}, {"label": "Not yet", "reply": "no"}])}
    r = subprocess.run(["node", "-e", SCRIPT, str(LAST_CARD), json.dumps(cards)],
                       capture_output=True, text=True, timeout=30, check=True)
    out = json.loads(r.stdout)
    rec, none = out["rec"], out["none"]
    assert 'class="lm-rows lm-ctas"' in rec
    # the recommended choice is the filled one; with none, the first is
    assert 'class="lm-row reply cta primary" data-reply="b"' in rec
    assert 'class="lm-row reply cta" data-reply="a"' in rec
    assert 'class="lm-row reply cta primary" data-reply="go"' in none
    # no "REPLY" verb label any more; the key and label stay for the Review overlay
    assert '>Reply<' not in rec
    assert '<span class="tile">A</span>' in rec and 'Buttons inside the card' in rec


def test_a_review_card_ends_with_the_verdict_bar(out):
    """Owner, 2026-10-28: a row at the bottom of a Review card with three
    options (pick C of static/scratch/card-verdict-row.html)."""
    h = out["done"]
    assert h.count('class="lm-finish"') == 1 and "Is this right?" not in h
    for act, label, meaning in (("done", "Done", "Put the session to sleep"),
                                ("next", "Something else", "Done, but keep chatting"),
                                ("bad", "Problem", "Not done, needs remediation")):
        assert 'data-verdict="' + act + '"' in h
        assert '<span class="tt">' + label + '</span><span class="ts">' + meaning + '</span>' in h
    assert 'data-verdict="undo"' in h
    # right under the Next line, in the Next band (owner's pick A, 2026-10-28,
    # static/scratch/card-verdict-in-next.html)
    nx = h.split('<div class="dt">Next</div>', 1)[1].split('<div class="lm-dl', 1)[0]
    assert 'class="lm-finish"' in nx and '<div class="dt">Your verdict</div>' not in h
    # A question or a blocker carries the same block, for "Not answering
    # now?" Standby / Done (Standby, owner 2026-10-28); _syncDone shows only
    # the rows that fit the session's status.
    for k in ("decision", "no_replies", "external"):
        assert out[k].count('class="lm-finish"') == 1
        assert 'data-verdict="standby"' in out[k] and "Not answering now?" in out[k]


def test_validate_is_a_row_the_owner_taps(out):
    """Owner, 2026-10-28: "we need to force a validate row so i have a way to check"."""
    h = out["val_page"]
    assert '<div class="dt">Validate</div>' in h
    assert 'class="lm-row lm-validate"' in h and 'data-view="/static/scratch/x.html"' in h and "Try it" in h
    # The real moment, when it is later, is a quiet line under the demo row.
    assert '<div class="lm-vlater">' in h and "For real: the next time you open a panel" in h
    assert "lm-vlater" not in out["val_web"]
    h = out["val_web"]
    assert 'data-href="https://example.com/"' in h and "data-view" not in h.split("Validate", 1)[1].split("</div></div>", 1)[0]
    h = out["val_none"]
    assert 'class="lm-row lm-validate static"' in h and "Send a message in any session" in h
    assert "Validate" not in out["done"]


def test_screenshots_sit_inside_validate_never_beside_it(out):
    """Owner's pick A, 2026-10-28 (static/scratch/validate-screens.html): "the
    screenshots are ok for validate i just dont want it presented as screen +
    validate"."""
    h = out["val_shots"]
    assert '<div class="dt">Screens</div>' not in h
    band = h.split('<div class="dt">Validate</div>', 1)[1].split('<div class="lm-dl', 1)[0]
    # the Try it row first, then the screenshot row, in the one band
    assert band.index('lm-row lm-validate') < band.index('lm-row shot')
    assert 'data-view="/static/scratch/d.png"' in band and "Desktop shot" in band
    # a card with no Validate row keeps its Screens band
    assert '<div class="dt">Screens</div>' in out["shots_only"]


def _validate_band(h):
    return h.split('<div class="dt">Validate</div>', 1)[1].split('<div class="lm-dl', 1)[0]


def test_try_it_or_see_it_reads_as_a_choice(out):
    """Owner's pick B, 2026-10-28 (static/scratch/validate-or.html): "its not
    obvious to me that that is a or ... i need that clearer".  A page to try
    and a screenshot are two tiles with OR between, not one list."""
    band = _validate_band(out["val_shots"])
    assert 'class="lm-vpair"' in band and 'class="lm-rows"' not in band
    # the OR is in the tile names, no mark between them (owner's pick B,
    # static/scratch/validate-or-word.html: "more pronounced without a circle")
    assert 'vt-or"' not in band
    assert band.index('Try it yourself') < band.index('<span class="vt-orw">OR</span> just look')
    # the try tile opens the page; the whole see tile opens the one screenshot
    try_tile = band.split('vt-try"', 1)[1].split('</button>', 1)[0]
    assert 'data-view="/static/scratch/x.html"' in try_tile
    see_tile = band.split('vt-see"', 1)[1].split('</button>', 1)[0]
    assert 'data-view="/static/scratch/d.png"' in see_tile and 'data-img="1"' in see_tile


def test_several_screenshots_each_open_from_the_see_tile(out):
    band = _validate_band(out["val_shots2"])
    assert '<span class="vt-orw">OR</span> just look' in band
    assert 'data-view="/static/scratch/d.png"' in band and 'data-view="/static/scratch/p.png"' in band
    assert band.count('class="lm-row shot vt-') == 2


def test_nothing_to_open_keeps_the_plain_rows(out):
    """With no page to try there is no choice to draw: the screenshots stay rows."""
    band = _validate_band(out["val_shots_noaddr"])
    assert 'lm-vpair' not in band and 'class="lm-row lm-validate static"' in band and 'lm-row shot' in band



def test_history_sits_above_next_which_sits_above_more(out):
    """Owner, 2026-10-28: "history should go before next which goes before more"."""
    h = out["ordered"]
    assert h.index('<div class="dt">History</div>') < h.index('<div class="dt">Next</div>') < h.index('<div class="dt">More</div>')
    h = out["ordered_ask"]
    assert h.index('<div class="dt">History</div>') < h.index('<div class="dt">To do</div>') < h.index('<div class="dt">More</div>')
