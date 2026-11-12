"""The Decide popup's form: several questions answered at once.

A decision may carry ``needs.form`` instead of ``needs.replies`` when several
independent choices block one task.  The card draws one "Answer the N
questions" button; the popup opens a split view (questions down the left with
what was picked, the open one on the right with its options as cards and a
note box) and sends every answer back as ONE message starting with the form
prefix.  The form is drawn only in the popup, so it is outside the card's
length; ``split_problems`` (src/engine/handoff_rules.py) checks its shape.

Rules run in Python; the card runs the real ``last-card.js`` in node; the
popup runs it in headless Chromium (skips without Playwright).
"""
import copy
import html
import json
import re
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


def _opt(key, label, rec=False):
    return {"key": key, "label": label, "recommended": rec,
            "gain": f"What option {key} gets the user",
            "verdict": f"Why option {key} {'wins' if rec else 'loses'} here"}


FORM = [
    {"id": "store", "question": "Where should the sample state live?",
     "context": "Each sample writes its own file today, so a listing reads many files.",
     "options": [_opt("A", "One shared file", True), _opt("B", "Keep many files"), _opt("C", "A small server")]},
    {"id": "sync", "question": "How should the sample sync?",
     "context": "The sample loses its link while asleep and misses changes meanwhile.",
     "options": [_opt("A", "Replay what was missed", True), _opt("B", "Reload everything")]},
    {"id": "check", "question": "Where should the sample checks run?",
     "context": "The sample checks take most of the run time on every publish.",
     "options": [_opt("A", "Keep them on publish", True), _opt("B", "Run them nightly")]},
]


def _card(form=FORM, **needs):
    return {"type": "needs_you", "title": "Sample choices", "headline": "Three sample questions",
            "context": "x", "needs": dict({"kind": "decision", "ask": "Answer the sample questions",
                                            "why": "They block the sample task", "form": form}, **needs)}


# ── The rules ────────────────────────────────────────────────────────────

def test_a_complete_form_has_no_form_problems_and_needs_no_replies_or_look():
    probs = hr.format_problems(_card())
    assert not [p for p in probs if "form" in p.lower() or "reply" in p.lower() or "look at" in p]
    assert st.unusable_problems(_card()) == []


def test_the_form_is_outside_the_cards_length_and_list_limit():
    big = [copy.deepcopy(FORM[i % 3]) | {"id": f"q{i}"} for i in range(7)]
    for q in big:
        q["context"] = " ".join(["word"] * 28)   # over a line (25), under the form's 30
    rows = [r for r in hr.word_table(_card(big)) if r[0].startswith("needs.form")]
    assert rows and not any(counted for _, _, counted, _ in rows)
    assert not [p for p in hr.length_problems(_card(big)) if "needs.form" in p]
    assert hr.card_words(_card(big)) == hr.card_words(_card(FORM))


@pytest.mark.parametrize("break_it, says", [
    (lambda f: f[:1], "2 to 8 questions"),
    (lambda f: [dict(f[0], context="too short"), f[1]], "context"),
    (lambda f: [dict(f[0], options=f[0]["options"][:1]), f[1]], "options"),
    (lambda f: [dict(f[0], options=[dict(o, recommended=False) for o in f[0]["options"]]), f[1]], "exactly one"),
    (lambda f: [dict(f[0], options=[dict(o, verdict="the same verdict for every one") for o in f[0]["options"]]), f[1]], "share one verdict"),
    (lambda f: [f[0], dict(f[1], id="store")], "repeats the id"),
    (lambda f: [dict(f[0], options=[dict(f[0]["options"][0], gain="short")] + f[0]["options"][1:]), f[1]], "gain"),
])
def test_an_incomplete_form_is_sent_back(break_it, says):
    probs = hr.split_problems(_card(break_it(copy.deepcopy(FORM))))
    assert probs and says in " ".join(probs)


def test_a_form_with_replies_beside_it_is_sent_back():
    probs = hr.split_problems(_card(replies=[{"key": "A", "label": "one two three", "reply": "a"}]))
    assert any("leave needs.replies" in p for p in probs)


def test_an_empty_form_is_unusable():
    assert st.unusable_problems(_card(form=[]))


def test_the_answer_message_closes_the_form_and_other_words_do_not():
    assert ev.answers_choice(_card(), hr.FORM_ANSWER_PREFIX + " 3 questions:\n1. x")
    assert not ev.answers_choice(_card(), "what does the second question mean?")


def test_sessions_are_told_about_the_form():
    assert "needs.form instead of needs.replies" in st.INSTRUCTIONS
    assert hr.FORM_ANSWER_PREFIX in st.INSTRUCTIONS


# ── The card ─────────────────────────────────────────────────────────────

SCRIPT = r"""
global.window = global;
global.location = { host: '127.0.0.1:5050' };
global.document = { addEventListener() {}, querySelectorAll() { return []; } };
eval(require('fs').readFileSync(process.argv[1], 'utf8'));
process.stdout.write(VNLastCard.render(JSON.parse(process.argv[2]), {}));
"""


def test_the_card_draws_one_button_and_keeps_the_questions_in_the_popup():
    if shutil.which("node") is None:
        pytest.skip("node not installed")
    c = _card()
    h = subprocess.run(["node", "-e", SCRIPT, str(LAST_CARD), json.dumps(c)],
                       capture_output=True, text=True, encoding="utf-8", timeout=30, check=True).stdout
    assert h.count('class="lm-decide"') == 1 and "Answer the 3 questions" in h
    # The questions travel only inside the button's data: the card face lists none.
    face = re.sub(r'data-decide="[^"]*"', "", h)
    assert "lm-fmlist" not in face
    for q in c["needs"]["form"]:
        assert html.escape(q["question"]) not in face and q["question"] not in face
    data = json.loads(html.unescape(h.split('data-decide="', 1)[1].split('"', 1)[0]))
    assert data["tpl"] == "form" and [q["id"] for q in data["items"]] == ["store", "sync", "check"]
    assert "replies" not in data


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
    page.wait_for_selector(".lm-dz.lm-fm")
    page.wait_for_timeout(650)   # the open animation
    return page, errors


def test_the_split_view_answers_every_question_and_sends_one_message(browser, tmp_path):
    page, errors = _open(browser, tmp_path)
    try:
        z = page.locator(".lm-dz.lm-fm")
        assert z.locator(".lm-fm-ri").count() == 3
        assert z.locator(".lm-fm-pane .qt").inner_text() == FORM[0]["question"]
        assert z.locator(".lm-fm-cd").count() == 3
        assert z.locator(".go").is_disabled() and z.locator(".cnt").inner_text() == "0 of 3"
        z.locator(".lm-fm-cd[data-pick=B]").click()
        assert "Keep many files" in z.locator(".lm-fm-ri").nth(0).inner_text()
        z.locator("textarea").fill("a sample note")
        z.locator(".lm-fm-next").click()
        assert z.locator(".lm-fm-pane .qt").inner_text() == FORM[1]["question"]
        z.locator(".lm-fm-cd[data-pick=A]").click()
        z.locator(".lm-fm-ri").nth(2).click()
        z.locator(".lm-fm-cd[data-pick=A]").click()
        # back to the first: the pick and the note are kept
        z.locator(".lm-fm-ri").nth(0).click()
        assert z.locator(".lm-fm-cd.on").get_attribute("data-pick") == "B"
        assert z.locator("textarea").input_value() == "a sample note"
        assert z.locator(".cnt").inner_text() == "3 of 3" and not z.locator(".go").is_disabled()
        z.locator(".go").click()
        sent = page.evaluate("window.__sent")
        assert len(sent) == 1 and sent[0][0] == "s1"
        text = sent[0][1]
        assert text.startswith(hr.FORM_ANSWER_PREFIX + " 3 questions:")
        assert "1. " + FORM[0]["question"] + "\n   Keep many files\n   Note: a sample note" in text
        assert "Replay what was missed (recommended)" in text and "Keep them on publish (recommended)" in text
        page.wait_for_selector(".lm-dz", state="detached", timeout=3000)
        assert "Sent your answers" in page.locator(".lm-decide").inner_text()
        assert not errors, errors
    finally:
        page.close()


def test_escape_closes_without_sending_and_the_phone_shows_a_strip(browser, tmp_path):
    page, errors = _open(browser, tmp_path, width=390)
    try:
        rail = page.locator(".lm-fm-rail").bounding_box()
        pane = page.locator(".lm-fm-pane").bounding_box()
        assert rail["y"] + rail["height"] <= pane["y"] + 1   # the list sits above the question
        page.keyboard.press("Escape")
        page.wait_for_selector(".lm-dz", state="detached", timeout=3000)
        assert page.evaluate("window.__sent") == []
        assert not errors, errors
    finally:
        page.close()
