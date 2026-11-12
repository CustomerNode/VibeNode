"""An Approve card shows what it asks the user to approve.

A card asked "Ship the Home card and the Directions dashboard?" while the page
and its screenshot sat folded under Checked and dropped, so the Approve
section had nothing to open.  Two fixes:

- the rules send back an approval of something visible with nothing in
  needs.look (``approve_look_problem``), and a why/next that names a choice
  by its letter;
- the card draws an asking card's links and pictures in its own band when
  needs.look is empty, so older cards show them too.
"""
import json
import shutil
import subprocess
from pathlib import Path

import pytest

from engine import handoff_rules

ROOT = Path(__file__).resolve().parents[1]
LAST_CARD = ROOT / "src" / "web" / "static" / "js" / "last-card.js"


def _approve(**extra):
    c = {"type": "needs_you", "title": "Shop front page", "headline": "The shop front page has a new order dashboard; ship it?",
         "context": "x", "answer": "Built.", "needs": {"kind": "approve", "ask": "Ship the new order dashboard?",
                                                        "why": "It changes what every buyer sees.",
                                                        "replies": [{"key": "A", "label": "Ship it", "reply": "Ship it", "recommended": True},
                                                                    {"key": "B", "label": "Not yet", "reply": "Not yet"}]}}
    c.update(extra)
    return c


def test_an_approval_of_something_visible_needs_it_in_look():
    assert handoff_rules.approve_look_problem(_approve()) is not None
    links = [{"label": "Real page", "kind": "link", "url": "/static/scratch/shop.html"}]
    assert handoff_rules.approve_look_problem(_approve(links=links)) is not None   # Checked is not the place
    c = _approve()
    c["needs"]["look"] = [{"label": "The dashboard", "kind": "image", "url": "/static/scratch/shop.png"}]
    assert handoff_rules.approve_look_problem(c) is None


def test_an_approval_with_nothing_to_see_passes():
    c = _approve(title="Web server restart", headline="Restart the web server so the change goes live?")
    c["needs"]["ask"] = "Restart the web server now?"
    assert handoff_rules.approve_look_problem(c) is None


def test_why_and_next_may_not_name_a_choice_by_its_letter():
    c = _approve()
    c["needs"]["why"] = "It is your D, built from existing parts."
    assert any("(needs.why)" in p for p in handoff_rules.variant_name_problems(c))
    c["needs"]["why"] = "It is the direction you picked, built from existing parts."
    assert handoff_rules.variant_name_problems(c) == []


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


def _band(h, name):
    i = h.index(">" + name + "<")
    return h[i:]


def test_an_older_approve_card_draws_its_links_and_pictures_in_the_approve_band():
    h = _html(_approve(links=[{"label": "Real page", "kind": "link", "url": "https://shop.example/front"},
                              {"label": "Both render", "kind": "image", "url": "/static/scratch/shop.png"}]))
    ap = _band(h, "Approve")
    assert "Real page" in ap and "Both render" in ap and 'class="thumb"' in ap
    assert "it looked at" not in h        # not folded under Checked


def test_a_done_card_still_folds_its_links_under_checked():
    c = {"type": "done", "title": "Shop", "headline": "Done.", "context": "x",
         "links": [{"label": "Log", "kind": "link", "url": "https://shop.example/log"}],
         "needs": {"kind": "none", "why": "y", "next": "n"}}
    assert "1 thing it looked at" in _html(c)
