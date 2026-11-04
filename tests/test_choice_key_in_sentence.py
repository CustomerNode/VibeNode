"""A choice picked by its key inside a sentence counts as picked.

A user can answer a three-way wording choice with a sentence that named the
choice's letter after "do" and goes on with another request.  The
end-of-turn check used to count only a key that was the whole message, so it
treated the choice as unmade and told the session to ask again.  The
end-of-turn check (Python) and the card's own "answered" mark (last-card.js)
must agree.  Messages below are made up.
"""

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from engine.handoff_evidence import answers_choice

ROOT = Path(__file__).resolve().parents[1]
LAST_CARD = ROOT / "src" / "web" / "static" / "js" / "last-card.js"

CARD = {"needs": {"kind": "decision", "replies": [
    {"key": "P", "label": "Plain label", "reply": "Use the plain label"},
    {"key": "Q", "label": "Question label", "reply": "Use the question"},
    {"key": "R", "label": "Long label", "reply": "Use the long one"}]}}

CASES = [
    ("lets do q and also tidy the footer", True),
    ("i agree with r but the words feel long", True),
    ("go with p", True),
    ("p", True),
    ("lets do a quick check first", False),   # "a" runs on: not a pick
    ("what is q?", False),
    ("do p or q?", False),                     # a question naming two
    ("use the q one later maybe", False),
]

SCRIPT = r"""
global.window = global;
global.location = { host: '127.0.0.1:5050' };
global.document = { addEventListener() {}, querySelectorAll() { return []; } };
eval(require('fs').readFileSync(process.argv[1], 'utf8'));
const c = JSON.parse(process.argv[2]), texts = JSON.parse(process.argv[3]);
process.stdout.write(JSON.stringify(texts.map(t => VNLastCard._answersForTest(c, t))));
"""


@pytest.mark.parametrize("text,picked", CASES)
def test_the_end_of_turn_check(text, picked):
    assert answers_choice(CARD, text) is picked


def test_the_card_agrees():
    if shutil.which("node") is None:
        pytest.skip("node not installed")
    r = subprocess.run(["node", "-e", SCRIPT, str(LAST_CARD), json.dumps(CARD), json.dumps([t for t, _ in CASES])],
                       capture_output=True, text=True, encoding="utf-8", timeout=30)
    assert r.returncode == 0, r.stderr
    assert json.loads(r.stdout) == [p for _, p in CASES]
