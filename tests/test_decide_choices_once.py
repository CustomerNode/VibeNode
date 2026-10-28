"""A Decide card shows its choices once (owner, 2026-10-28: "screens and
decide, both with the same choices, confused the fuck out of me. thats a
smart termination failure").  Pictures of the choices go inside Decide with
their choice; the rules no longer push them into "links", and a card that
does it anyway is sent back and still drawn right."""

import html
import json
import shutil
import subprocess
from pathlib import Path

import pytest

from daemon import handoff_rules as rules

LAST_CARD = Path(__file__).resolve().parents[1] / "static" / "js" / "last-card.js"
SCRIPT = r"""
global.window = global;
global.location = { host: '127.0.0.1:5050' };
global.document = { addEventListener() {}, querySelectorAll() { return []; } };
eval(require('fs').readFileSync(process.argv[1], 'utf8'));
process.stdout.write(VNLastCard.render(JSON.parse(process.argv[2]), {}));
"""

CARD = {"type": "needs_you", "title": "Earlier tasks in summaries", "headline": "Pick how VibeNode summaries show earlier tasks",
        "context": "x",
        "links": [{"label": "A: Earlier line, opened", "kind": "image", "url": "/static/scratch/earlier-a.png"},
                  {"label": "Option B", "kind": "image", "url": "/static/scratch/earlier-b.png"},
                  {"label": "Today's sidebar", "kind": "image", "url": "/static/scratch/today.png"}],
        "needs": {"kind": "decision", "ask": "How should VibeNode summaries show earlier tasks?",
                  "look": [{"label": "Designs", "kind": "link", "url": "/static/scratch/card-earlier.html"}],
                  "replies": [{"key": "A", "label": "An Earlier line that opens", "reply": "A"},
                              {"key": "B", "label": "Last two tasks always shown", "reply": "B"}]}}


def _render(card):
    if shutil.which("node") is None:
        pytest.skip("node not installed")
    return subprocess.run(["node", "-e", SCRIPT, str(LAST_CARD), json.dumps(card)],
                          capture_output=True, text=True, timeout=30, check=True).stdout


def test_choice_pictures_go_inside_decide_not_a_screens_band():
    h = _render(CARD)
    data = json.loads(html.unescape(h.split('data-decide="', 1)[1].split('"', 1)[0]))
    a, b = data["replies"]
    assert [s["url"] for s in a["screens"]] == ["/static/scratch/earlier-a.png"]
    assert [s["url"] for s in b["screens"]] == ["/static/scratch/earlier-b.png"]
    band = h.split('data-decide="', 1)[0]
    assert "earlier-a.png" not in band and "earlier-b.png" not in band
    assert "today.png" in h            # a picture that is not a choice stays where it was


def test_the_rules_send_back_a_card_that_shows_choices_twice():
    p = rules.duplicate_choice_problem(CARD)
    assert p and "twice" in p and "screens" in p


def test_the_rules_point_a_decide_card_at_screens_not_links():
    bare = dict(CARD, links=[])
    p = rules.mockup_picture_problem(bare)
    assert p and '"screens"' in p and "Do not add them as" in p
    fixed = dict(bare, needs=dict(bare["needs"], replies=[dict(r, screens=[{"label": r["key"], "url": "/static/scratch/x.png"}])
                                                          for r in bare["needs"]["replies"]]))
    assert rules.mockup_picture_problem(fixed) is None and rules.duplicate_choice_problem(fixed) is None
