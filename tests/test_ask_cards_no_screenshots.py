"""A Decide or Approve card shows only the button that matters.

A Validate band with a screenshot above the Approve band was confusing, so
Decide and Approve cards draw no Validate and no screenshots; a done card
keeps both.
"""
import json
import shutil
import subprocess
from pathlib import Path

import pytest

from engine import handoff_rules

ROOT = Path(__file__).resolve().parents[1]
LAST_CARD = ROOT / "src" / "web" / "static" / "js" / "last-card.js"
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

SHOT = {"label": "The release card", "kind": "image", "url": "/static/scratch/x.png"}
VAL = {"how": "Open the release page", "url": "/static/scratch/x.html"}
BASE = {"path": ["Releases"], "title": "Release 1", "headline": "Approve a made-up release.",
        "answer": "Short.", "explained": "Plain words " * 20, "full_answer": "Full.",
        "zoom": {"big": "b", "task": "t", "now": "n"}, "links": [SHOT], "validate": VAL}
APPROVE = dict(BASE, type="needs_you", needs={"kind": "approve", "ask": "Approve release 1?",
               "look": [{"label": "Open the approval page", "kind": "link", "url": "http://127.0.0.1:7000/"}, SHOT]})
DECIDE = dict(BASE, type="needs_you", needs={"kind": "decision", "ask": "Which way?",
              "replies": [{"key": "A", "label": "One way", "reply": "A", "recommended": True},
                          {"key": "B", "label": "Other way", "reply": "B"}]})
DONE = dict(BASE, type="done", needs={"kind": "none", "why": "w", "next": "n"})
TODO = dict(BASE, type="needs_you", needs={"kind": "action", "ask": "Reload the tab", "replies": [{"label": "Done", "reply": "done"}]})
DELIV = dict(BASE, type="deliverable", needs={"kind": "none", "why": "w", "next": "n"},
             deliverable={"kind": "notes", "title": "Notes", "body": "Text."})
ANSWER = dict(BASE, type="explain", validate=None, needs={"kind": "none", "why": "w", "next": "n"},
              links=[SHOT, {"label": "Try the page", "kind": "link", "url": "/static/scratch/y.html"}])
PICS = dict(BASE, type="explain", validate=None, needs={"kind": "none", "why": "w", "next": "n"})


def _render(cards):
    node = shutil.which("node")
    if not node:
        pytest.skip("node not installed")
    r = subprocess.run([node, "-e", SCRIPT, str(LAST_CARD), json.dumps(cards)],
                       capture_output=True, text=True, timeout=60)
    assert r.returncode == 0, r.stderr
    return json.loads(r.stdout)


def test_decide_and_approve_cards_draw_no_validate_or_screenshot():
    out = _render({"approve": APPROVE, "decide": DECIDE, "done": DONE, "todo": TODO, "deliv": DELIV})
    for k in ("approve", "decide", "todo", "deliv"):
        assert ">Validate<" not in out[k], k
        assert "x.png" not in out[k], k
    assert "Open the approval page" in out["approve"]
    assert ">Validate<" in out["done"] and "x.png" in out["done"]


def test_an_approve_card_is_not_asked_for_a_screenshot():
    card = dict(APPROVE, links=[], needs={"kind": "approve", "ask": "Approve?",
                "look": [{"label": "Page", "kind": "link", "url": "/static/scratch/x.html"}]})
    assert handoff_rules.mockup_picture_problem(card) is None


def test_screenshots_are_always_the_just_look_tile():
    out = _render({"answer": ANSWER, "pics": PICS})
    # A page beside the screenshots becomes "Try it yourself / OR just look".
    assert "Try it yourself" in out["answer"] and "just look" in out["answer"]
    assert out["answer"].count("y.html") >= 1 and "lm-rows" not in out["answer"].split("Validate", 1)[1].split("Your verdict", 1)[0]
    # Pictures alone are one "Just look" tile, never a list of rows.
    assert "lm-vone" in out["pics"] and "Just look" in out["pics"]


def test_todo_and_deliverable_cards_are_not_asked_for_a_screenshot():
    page = [{"label": "Page", "kind": "link", "url": "/static/scratch/x.html"}]
    todo = dict(TODO, links=page)
    deliv = dict(DELIV, links=page)
    assert handoff_rules.mockup_picture_problem(todo) is None
    assert handoff_rules.mockup_picture_problem(deliv) is None
