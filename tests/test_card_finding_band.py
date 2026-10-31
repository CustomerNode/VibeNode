""""Also found" on the end-of-turn card: a home for a side finding.

A session that notices something outside the user's ask that it may not act
on alone (a permission rule, code it was not asked to touch) had nowhere to
put it: the rules read an open finding as unfinished work, so on 2026-10-29 a
note about Windows delete commands was dressed up as an Approve card, in
jargon, to get past the check.  Card field::

    "finding": {"what": "...", "risk": "...", "recommend": "...", "reply": "..."}

``static/js/last-card.js`` draws it as one quiet band titled "Also found"
right after the action band (Next / Read it / Decide ...) and before More,
with one button, "Deal with it", that sends ``finding.reply`` (or a default
naming the finding) as the user's message.  The button acts only from the
newest card, like the verdict chips.

Structure runs the real file in node; the click runs the real file in
headless Chromium through Playwright (skips without it).
"""
import json
import shutil
import subprocess
from pathlib import Path

import pytest

from tests._browser import shared_browser_scope

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

NONE = {"kind": "none", "why": "Everything asked for is in.", "next": "Nothing runs by itself."}
FINDING = {"what": "Auto mode approves the Windows ways of deleting whole folders",
           "risk": "A session could wipe a folder on this PC without asking",
           "recommend": "Refuse them the way rm -rf is refused"}
WHAT_DEFAULT = "Deal with what you found: " + FINDING["what"]


def _done(**extra):
    return dict({"type": "done", "title": "T", "headline": "H", "context": "x", "status": {"done": ["a"]},
                 "verified": [{"check": "c", "result": "r", "ok": True}], "needs": NONE}, **extra)


@pytest.fixture(scope="module")
def out():
    if shutil.which("node") is None:
        pytest.skip("node not installed")
    cards = {
        "with": _done(finding=FINDING, links=[{"label": "The test", "kind": "file", "url": "tests/x.py"}]),
        "with_reply": _done(finding=dict(FINDING, reply="Refuse the Windows delete commands under Auto mode")),
        "without": _done(links=[{"label": "The test", "kind": "file", "url": "tests/x.py"}]),
        "explain": {"type": "explain", "title": "T", "headline": "H", "context": "x", "answer": "A.",
                    "needs": NONE, "finding": FINDING},
        "decide": {"type": "needs_you", "title": "T", "headline": "H", "context": "x", "finding": FINDING,
                   "needs": {"kind": "decision", "ask": "Pick one", "why": "y",
                             "look": [{"label": "l", "kind": "link", "url": "https://example.com/"}],
                             "replies": [{"key": "A", "label": "one two three", "reply": "a"},
                                         {"key": "B", "label": "four five six", "reply": "b"}]}},
        "escaped": _done(finding=dict(FINDING, what="<script>alert(1)</script> & more")),
        "bad": _done(finding="just a string"),
    }
    r = subprocess.run(["node", "-e", SCRIPT, str(LAST_CARD), json.dumps(cards)],
                       capture_output=True, text=True, encoding="utf-8", timeout=30, check=True)
    return json.loads(r.stdout)


def test_a_finding_draws_one_also_found_band_with_its_three_lines_and_one_button(out):
    h = out["with"]
    assert h.count('class="lm-find"') == 1
    assert ">Also found<" in h
    band = h.split('class="lm-find"', 1)[1].split("</div></div></div>", 1)[0]
    assert FINDING["what"] in band
    assert "<b>Risk:</b> " + FINDING["risk"] in band
    assert "<b>Recommends:</b> " + FINDING["recommend"] in band
    assert band.count("<button") == 1 and ">Deal with it</button>" in band


def test_the_button_carries_the_reply_or_a_default_naming_the_finding(out):
    assert 'data-find="' + WHAT_DEFAULT + '"' in out["with"]
    assert 'data-find="Refuse the Windows delete commands under Auto mode"' in out["with_reply"]


def test_no_finding_no_band_and_a_bad_shape_is_ignored(out):
    assert "lm-find" not in out["without"] and "Also found" not in out["without"]
    assert "lm-find" not in out["bad"]
    # the band is the only difference between the two cards
    assert out["with"].replace(out["with"].split('<div class="lm-dl lm-quiet lm-findband">', 1)[1].split("</div></div></div>", 1)[0], "") \
        .replace('<div class="lm-dl lm-quiet lm-findband"></div></div></div>', "") == out["without"]


def test_the_band_sits_after_the_action_band_and_before_more(out):
    # Last part of Review & respond, after the action (six fixed sections, 2026-10-31;
    # there is no More section any more).
    h = out["with"]
    assert h.index(">Review &amp; respond<") < h.index(">Next<") < h.index(">Also found<")
    # an answer: Read it, then Also found
    assert out["explain"].index(">Read it<") < out["explain"].index(">Also found<")
    # a decision: Decide (with why I'm asking), then Also found
    d = out["decide"]
    assert d.index(">Decide<") < d.index("lm-whyask") < d.index(">Also found<")
    assert ">More<" not in h and ">More<" not in d


def test_the_text_is_escaped(out):
    h = out["escaped"]
    assert "<script>" not in h
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in h


def test_quiet_grey_no_status_colour_and_its_own_class():
    css = CSS.read_text(encoding="utf-8")
    rules = [r for r in css.split("}") if ".lm-find" in r]
    assert rules
    assert not any("--tone" in r for r in rules), "Also found must not take the card's status colour"
    js = LAST_CARD.read_text(encoding="utf-8")
    seg = js[js.index("function findingBand"):js.index("function render(")]
    assert "lm-more" not in seg and "lm-xpl" not in seg


# ── The click, in a real browser ─────────────────────────────────────────

@pytest.fixture(scope="module")
def browser():
    with shared_browser_scope() as b:
        yield b


def _page_html(cards):
    msgs = "".join(
        "<div class=\"msg asst\"><div class=\"msg-body\"><div class=\"vn-card-ph\" data-card='"
        + json.dumps(c).replace("'", "&#39;") + "'></div></div></div>" for c in cards)
    return f"""<!doctype html><html><head><meta charset="utf-8"></head><body><div id="live-log">{msgs}</div>
<script>var liveSessionId='s1', sessionKinds={{s1:'idle'}}; window.__sent=[];
window._liveSubmitDirect=(sid,text)=>{{ window.__sent.push([sid,text]); }};
window.fetch=()=>Promise.reject(new Error('offline'));</script>
<script src="{LAST_CARD.as_uri()}"></script>
<script>VNLastCard.renderIn(document.getElementById('live-log'));</script></body></html>"""


def _open(browser, cards, tmp_path):
    # A file: page, so the file: script it loads is same-origin.
    f = tmp_path / "card.html"
    f.write_text(_page_html(cards), encoding="utf-8")
    page = browser.new_page()
    errors = []
    page.on("pageerror", lambda e: errors.append(str(e)))
    page.goto(f.as_uri(), wait_until="load")
    assert not errors, errors
    page.wait_for_function("document.querySelectorAll('.lm.skim').length === " + str(len(cards)))
    page.wait_for_timeout(50)   # _syncDone runs on the next animation frame
    return page


def test_deal_with_it_sends_the_default_text_as_the_users_message(browser, tmp_path):
    page = _open(browser, [_done(finding=FINDING)], tmp_path)
    try:
        btn = page.locator(".lm-find .go")
        assert btn.count() == 1 and btn.inner_text() == "Deal with it"
        btn.click()
        assert page.evaluate("window.__sent") == [["s1", WHAT_DEFAULT]]
        assert page.evaluate("document.querySelector('.lm-find .go').disabled") is True
        assert page.locator(".lm-find.sent").count() == 1
        # a second click sends nothing more
        page.locator(".lm-find .go").click(force=True)
        assert page.evaluate("window.__sent.length") == 1
    finally:
        page.close()


def test_deal_with_it_sends_the_cards_own_reply_when_it_has_one(browser, tmp_path):
    page = _open(browser, [_done(finding=dict(FINDING, reply="Refuse them under Auto mode too"))], tmp_path)
    try:
        page.locator(".lm-find .go").click()
        assert page.evaluate("window.__sent") == [["s1", "Refuse them under Auto mode too"]]
    finally:
        page.close()


def test_only_the_newest_card_offers_the_button_the_text_stays_everywhere(browser, tmp_path):
    older = _done(finding=dict(FINDING, what="An older finding"))
    page = _open(browser, [older, _done(finding=FINDING)], tmp_path)
    try:
        assert page.locator(".lm-find").count() == 2
        hidden = page.evaluate("[...document.querySelectorAll('.lm-find .go')].map(b => b.hidden)")
        assert hidden == [True, False]
        assert page.locator(".lm-find .tt").first.inner_text() == "An older finding"
    finally:
        page.close()
