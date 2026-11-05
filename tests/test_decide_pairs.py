"""The Decide popup's pairs: things that should be identical, side by side.

A decision may carry ``needs.pairs`` when a change should leave something
looking the same.  The card draws one "Review the N pairs" button; the popup
uses the form's left panel to list the pairs with their mark, shows the open
pair side by side (picture, page, html with no scripts, or text with the first
difference marked) with Same / Not the same and a note, and sends every mark as
ONE message starting with the pairs prefix.  ``split_problems``
(src/engine/handoff_rules.py) checks its shape; it is outside the card's length.

Rules run in Python; the card in node; the popup in headless Chromium.
"""
import copy
import html
import json
import shutil
import subprocess
from pathlib import Path

import pytest

from engine import handoff_evidence as ev
from engine import handoff_rules as hr
from engine import smart_termination as st
from tests._browser import shared_browser_scope

ROOT = Path(__file__).resolve().parents[1]
LAST_CARD = ROOT / "src" / "web" / "static" / "js" / "last-card.js"
CSS = ROOT / "src" / "web" / "static" / "css" / "last-card.css"

PAGE_A = "<h1>Sample sign in</h1><button>Sign in</button>"
PAGE_B = "<h1>Sample sign in</h1><button>Log in</button><script>parent.__ran = 1</script>"
PAIRS = [
    {"id": "page", "title": "Sample sign-in page", "a": {"kind": "html", "html": PAGE_A, "label": "Old"},
     "b": {"kind": "html", "html": PAGE_B, "label": "New"}},
    {"id": "msg", "title": "Sample error message", "a": {"kind": "text", "text": "Could not save. Try again."},
     "b": {"kind": "text", "text": "Could not save. Retry."}},
    {"id": "pic", "title": "Sample card picture", "a": {"kind": "image", "url": "/static/a.png"},
     "b": {"kind": "image", "url": "/static/a.png"}},
]


def _card(pairs=PAIRS, **needs):
    return {"type": "needs_you", "title": "Sample pairs", "headline": "Three sample pairs to check",
            "context": "x", "needs": dict({"kind": "decision", "ask": "Do these sample pairs match?",
                                            "why": "The sample change should change nothing", "pairs": pairs}, **needs)}


# ── The rules ────────────────────────────────────────────────────────────

def test_complete_pairs_have_no_pair_problems_and_need_no_replies_or_look():
    probs = hr.format_problems(_card())
    assert not [p for p in probs if "pair" in p.lower() or "reply" in p.lower() or "look at" in p]
    assert st.unusable_problems(_card()) == []


def test_pairs_are_outside_the_cards_length_and_their_content_has_no_line_cap():
    long = copy.deepcopy(PAIRS) * 2
    for i, p in enumerate(long):
        p["id"] = f"p{i}"
        p["a"] = {"kind": "text", "text": " ".join(["word"] * 200)}
    assert not [p for p in hr.length_problems(_card(long)) if "needs.pairs" in p]
    assert hr.card_words(_card(long)) == hr.card_words(_card(PAIRS[:1]))


@pytest.mark.parametrize("break_it, says", [
    (lambda p: [], "1 to 12 pairs"),
    (lambda p: [dict(p[0], title="x")] + p[1:], "title"),
    (lambda p: [dict(p[0], a={"kind": "video", "url": "/x"})] + p[1:], "kind"),
    (lambda p: [dict(p[0], a={"kind": "text"})] + p[1:], "needs \"text\""),
    (lambda p: [dict(p[2], a={"kind": "image", "url": "C:/pics/a.png"})], "not a file path"),
    (lambda p: [p[0], dict(p[1], id="page")], "repeats the id"),
])
def test_incomplete_pairs_are_sent_back(break_it, says):
    probs = hr.split_problems(_card(break_it(copy.deepcopy(PAIRS))))
    assert probs and says in " ".join(probs)


def test_pairs_with_replies_beside_them_are_sent_back():
    probs = hr.split_problems(_card(replies=[{"key": "A", "label": "one two three", "reply": "a"}]))
    assert any("leave needs.replies" in p for p in probs)


def test_a_pair_picture_is_link_checked():
    assert ("needs.pairs[2].a.url", "/static/a.png") in list(hr._card_urls(_card()))


def test_the_review_message_closes_the_choice():
    assert ev.answers_choice(_card(), hr.PAIRS_ANSWER_PREFIX + " 3 pairs:\n1. x: same")
    assert not ev.answers_choice(_card(), "why is the second one different?")


def test_sessions_are_told_about_pairs():
    assert "needs.pairs" in st.INSTRUCTIONS and hr.PAIRS_ANSWER_PREFIX in st.INSTRUCTIONS


# ── The card ─────────────────────────────────────────────────────────────

SCRIPT = r"""
global.window = global;
global.location = { host: '127.0.0.1:5050' };
global.document = { addEventListener() {}, querySelectorAll() { return []; } };
eval(require('fs').readFileSync(process.argv[1], 'utf8'));
process.stdout.write(VNLastCard.render(JSON.parse(process.argv[2]), {}));
"""


def test_the_card_draws_one_button_and_lists_the_pairs():
    if shutil.which("node") is None:
        pytest.skip("node not installed")
    h = subprocess.run(["node", "-e", SCRIPT, str(LAST_CARD), json.dumps(_card())],
                       capture_output=True, text=True, encoding="utf-8", timeout=30, check=True).stdout
    assert h.count('class="lm-decide"') == 1 and "Review the 3 pairs" in h
    data = json.loads(html.unescape(h.split('data-decide="', 1)[1].split('"', 1)[0]))
    assert data["tpl"] == "pairs" and [p["id"] for p in data["items"]] == ["page", "msg", "pic"]


# ── The popup, in a real browser ─────────────────────────────────────────

@pytest.fixture(scope="module")
def browser():
    with shared_browser_scope() as b:
        yield b


def _open(browser, tmp_path, width=1200):
    c = _card()
    f = tmp_path / "card.html"
    f.write_text(f"""<!doctype html><html><head><meta charset="utf-8"><link rel="stylesheet" href="{CSS.as_uri()}"></head>
<body><div id="live-log"><div class="msg asst"><div class="msg-body"><div class="vn-card-ph" data-card='{json.dumps(c).replace("'", "&#39;")}'></div></div></div></div>
<script>var liveSessionId='s1', sessionKinds={{s1:'idle'}}; window.__sent=[];
window._liveSubmitDirect=(sid,text)=>{{ window.__sent.push([sid,text]); }};
window.fetch=()=>Promise.reject(new Error('offline'));</script>
<script src="{LAST_CARD.as_uri()}"></script>
<script>VNLastCard.renderIn(document.getElementById('live-log'));</script></body></html>""", encoding="utf-8")
    page = browser.new_page(viewport={"width": width, "height": 800})
    errors = []
    page.on("pageerror", lambda e: errors.append(str(e)))
    page.goto(f.as_uri(), wait_until="load")
    page.wait_for_function("document.querySelectorAll('.lm.skim').length === 1")
    page.wait_for_timeout(50)
    page.locator(".lm-decide").click()
    page.wait_for_selector(".lm-dz.lm-pr")
    page.wait_for_timeout(650)
    return page, errors


def test_marking_every_pair_sends_one_review_message(browser, tmp_path):
    page, errors = _open(browser, tmp_path)
    try:
        z = page.locator(".lm-dz.lm-pr")
        assert z.locator(".lm-fm-ri").count() == 3 and z.locator(".lm-pr-side").count() == 2
        assert z.locator(".go").is_disabled()
        # an html side never runs its script
        assert z.locator(".lm-pr-side iframe").nth(1).get_attribute("sandbox") == ""
        assert page.evaluate("window.__ran") is None
        z.locator(".lm-pr-no").click()
        assert z.locator(".lm-pr-why").is_visible()
        z.locator(".lm-pr-why textarea").fill("button wording changed")
        assert "Not the same" in z.locator(".lm-fm-ri").nth(0).inner_text()
        z.locator(".lm-fm-ri").nth(1).click()
        # the text pair marks where it starts to differ
        assert z.locator(".lm-pr-side mark").first.inner_text() == "Try again."
        z.locator(".lm-pr-ok").click()
        page.wait_for_timeout(450)   # Same moves on to the next pair not reviewed
        assert z.locator(".lm-fm-pane .qt").inner_text() == "Sample card picture"
        z.locator(".lm-pr-ok").click()
        assert z.locator(".cnt").inner_text() == "3 of 3" and not z.locator(".go").is_disabled()
        z.locator(".go").click()
        sent = page.evaluate("window.__sent")
        assert len(sent) == 1
        assert sent[0][1] == (hr.PAIRS_ANSWER_PREFIX + " 3 pairs:\n"
                              "1. Sample sign-in page: NOT the same (button wording changed)\n"
                              "2. Sample error message: same\n3. Sample card picture: same")
        page.wait_for_selector(".lm-dz", state="detached", timeout=3000)
        assert "Sent your review" in page.locator(".lm-decide").inner_text()
        assert not errors, errors
    finally:
        page.close()


def test_on_a_phone_the_pair_stacks_and_escape_sends_nothing(browser, tmp_path):
    page, errors = _open(browser, tmp_path, width=390)
    try:
        a, b = (page.locator(".lm-pr-side").nth(i).bounding_box() for i in (0, 1))
        assert a["y"] + a["height"] <= b["y"] + 1
        page.keyboard.press("Escape")
        page.wait_for_selector(".lm-dz", state="detached", timeout=3000)
        assert page.evaluate("window.__sent") == []
        assert not errors, errors
    finally:
        page.close()
