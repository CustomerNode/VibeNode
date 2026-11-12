"""A pick between options is a Decide card, never an Approve.

A session rebuilt a page's design as four live options, each with its own
screens.  The user's message ended in "?", so the stop read it as a question
and refused a Decide card; the session re-sent the choice as an Approve, which
draws its replies as plain buttons and never shows their screens.  The user got
four letters and nothing to look at.  Three fixes:

- an Approve whose replies carry screens, or whose question asks "which", is
  sent back (``approve_pick_problem``);
- a question may still end on a Decide when at least two replies carry
  their own screens, not only when a scratch page is linked;
- the card draws an older Approve with screens as the Decide popup.
"""
import json
import shutil
import subprocess
from pathlib import Path

import pytest

from engine import handoff_evidence, handoff_rules

ROOT = Path(__file__).resolve().parents[1]
LAST_CARD = ROOT / "src" / "web" / "static" / "js" / "last-card.js"


def _reply(key, label, screens=True):
    r = {"key": key, "label": label, "reply": f"{key}: {label}",
         "what": "A different layout for the bakery's landing page.",
         "gain": "Shows the bakery's daily bread first.", "verdict": "Pick by looking at the picture."}
    if screens:
        r["screens"] = [{"label": "Landing", "url": f"http://127.0.0.1:8123/landing?look={key.lower()}",
                         "device": "web", "image": f"http://127.0.0.1:5050/static/scratch/bakery_{key}.png"}]
    return r


def _card(kind="approve", screens=True, ask="Which landing page should the bakery show?"):
    return {"type": "needs_you", "title": "Bakery landing page",
            "headline": "Pick a landing page for the bakery's site from three looks.",
            "answer": "Three looks are live to pick from.",
            "asks": [{"ask": "the landing page looks awful", "state": "needs_you"}],
            "needs": {"kind": kind, "ask": ask, "why": "A matter of taste.",
                      "replies": [_reply("A", "Today's plain list", screens),
                                  _reply("B", "Big photo of bread", screens),
                                  _reply("C", "Menu as cards", screens)]}}


def test_an_approve_with_screens_is_sent_back():
    p = handoff_rules.approve_pick_problem(_card())
    assert p and "decision" in p and "screens" in p
    assert any("choice between options" in x for x in handoff_rules.format_problems(_card()))


def test_an_approve_asking_which_is_sent_back():
    assert handoff_rules.approve_pick_problem(_card(screens=False)) is not None


def test_a_plain_yes_or_no_approve_passes():
    c = _card(screens=False, ask="Publish the new landing page now?")
    c["needs"]["replies"] = [{"key": "A", "label": "Publish it now", "reply": "Publish"},
                             {"key": "B", "label": "Not yet please", "reply": "Not yet"}]
    assert handoff_rules.approve_pick_problem(c) is None


def _yes_no_with_screens():
    c = _card(ask="Publish the new landing page now?")
    c["needs"]["replies"] = c["needs"]["replies"][:2]
    return c


def test_a_yes_or_no_approve_whose_only_pictures_are_reply_screens_is_sent_back():
    p = handoff_rules.approve_pick_problem(_yes_no_with_screens())
    assert p and "needs.look" in p


def test_a_yes_or_no_approve_with_its_page_in_look_passes():
    c = _yes_no_with_screens()
    c["needs"]["look"] = [{"label": "Landing", "kind": "image", "url": "/static/scratch/bakery_A.png"}]
    assert handoff_rules.approve_pick_problem(c) is None
    assert 'class="lm-decide"' not in _html(c)


def test_the_same_pick_as_a_decision_passes_the_pick_check():
    assert handoff_rules.approve_pick_problem(_card(kind="decision")) is None


def _facts(last):
    f = handoff_evidence.TurnFacts()
    f.last_user = last
    f.requests = [last]
    f.history = [last]
    return f


def test_a_decision_with_screens_may_answer_a_question():
    facts = _facts("why does the landing page look so awful??")
    assert handoff_evidence.is_question(facts.last_user)
    probs = handoff_evidence.reviewer_problems(_card(kind="decision"), facts)
    assert not any("answers it with a Decide card" in p for p in probs)


def test_a_decision_without_anything_to_see_still_may_not_answer_a_question():
    facts = _facts("why does the landing page look so awful??")
    probs = handoff_evidence.reviewer_problems(_card(kind="decision", screens=False), facts)
    assert any("answers it with a Decide card" in p for p in probs)


NODE = r"""
global.window = global;
global.location = { host: '127.0.0.1:5050' };
global.document = { addEventListener() {}, querySelectorAll() { return []; } };
eval(require('fs').readFileSync(process.argv[1], 'utf8'));
process.stdout.write(VNLastCard.render(JSON.parse(process.argv[2]), {}));
"""


def _html(card):
    if shutil.which("node") is None:
        pytest.skip("node not installed")
    return subprocess.run(["node", "-e", NODE, str(LAST_CARD), json.dumps(card)], capture_output=True,
                          text=True, encoding="utf-8", timeout=30, check=True).stdout


def test_an_older_approve_with_screens_opens_the_choices():
    h = _html(_card())
    assert 'class="lm-decide"' in h and "bakery_A.png" in h


def test_a_plain_approve_keeps_its_buttons():
    c = _card(screens=False, ask="Publish the new landing page now?")
    assert 'class="lm-decide"' not in _html(c)
