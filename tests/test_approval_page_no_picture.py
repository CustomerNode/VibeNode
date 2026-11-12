"""An approval answered on its own page shows the page and nothing else.

A release approval is answered on a page the release script serves; the
Approve band draws that page as its one button.  A card that also put a
screenshot of that page in needs.look drew the picture above the button:
noise between the reader and the one thing to tap.  The rules send such a
card back (``approval_page_picture_problem``), the guidance says so, and the
card draws the page before any picture an older card still carries.
"""
import json
import shutil
import subprocess
from pathlib import Path

import pytest

from engine import handoff_rules, smart_termination

ROOT = Path(__file__).resolve().parents[1]
LAST_CARD = ROOT / "src" / "web" / "static" / "js" / "last-card.js"

PAGE = {"label": "Approve the release", "kind": "link", "url": "http://localhost:41000/"}
SHOT = {"label": "The approval page", "kind": "image", "url": "/api/preview/asset/0123abcd"}


def _card(look, title="Bakery stock alerts", ask="Approve the bakery release that turns on the low-flour alerts?",
          replies=None):
    needs = {"kind": "approve", "ask": ask, "why": "Releases go live only on your tap.",
             "next": "After the tap it goes live.", "look": look}
    if replies is not None:
        needs["replies"] = replies
    return {"type": "needs_you", "path": ["Bakery"], "title": title,
            "headline": "Tap approve on the bakery release page to turn on low-flour alerts.",
            "context": "x", "answer": "Only the alerts change.", "needs": needs}


def test_a_screenshot_beside_the_approval_page_is_sent_back():
    p = handoff_rules.approval_page_picture_problem(_card([PAGE, SHOT]))
    assert p and "The approval page" in p
    assert any("its own page" in x for x in handoff_rules.format_problems(_card([PAGE, SHOT])))


def test_the_page_alone_passes():
    assert handoff_rules.approval_page_picture_problem(_card([PAGE])) is None


def test_a_picture_of_a_new_look_may_stay():
    c = _card([PAGE, {"label": "New menu board", "kind": "image", "url": "/static/scratch/board.png"}],
              title="Menu board redesign", ask="Approve the release that ships the new menu board design?")
    assert handoff_rules.approval_page_picture_problem(c) is None


def test_an_approval_with_buttons_is_not_judged_here():
    replies = [{"key": "A", "label": "Ship it now", "reply": "Ship it", "recommended": True}]
    assert handoff_rules.approval_page_picture_problem(_card([PAGE, SHOT], replies=replies)) is None


def test_pictures_only_is_left_to_the_other_rules():
    assert handoff_rules.approval_page_picture_problem(_card([SHOT])) is None


def test_the_guidance_says_only_the_page_and_a_short_answer():
    text = smart_termination.INSTRUCTIONS
    assert "The page is the only thing in needs.look" in text
    assert "what goes live and what the user will notice" in text


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


def test_an_older_card_draws_the_page_before_its_picture():
    h = _html(_card([SHOT, PAGE]))
    ap = h[h.index(">Approve<"):]
    assert ap.index("Approve the release") < ap.index("The approval page")


def test_an_approval_with_buttons_keeps_its_pictures_first():
    replies = [{"key": "A", "label": "Ship it now", "reply": "Ship it", "recommended": True}]
    h = _html(_card([{"label": "Shop picture", "kind": "image", "url": "/static/scratch/a.png"},
                     {"label": "Notes page", "kind": "link", "url": "https://shop.example/notes"}], replies=replies))
    ap = h[h.index(">Approve<"):]
    assert ap.index("Shop picture") < ap.index("Notes page")
