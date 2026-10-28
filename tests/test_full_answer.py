""""Full answer" on an answer card (owner's pick A of
static/scratch/answer-detail.html, 2026-10-28: "shouldn't there be a more
detailed answer too, perhaps like expandable markdown").

The card's optional "full_answer" is the detailed answer in markdown, drawn
folded under "In short".  It is not counted toward the card's length, it needs
the short "answer" beside it, and "Explain more" stays where it was.
"""
import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from daemon import handoff_rules  # noqa: E402

LAST_CARD = ROOT / "static" / "js" / "last-card.js"

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

FULL = ("## Why word order doesn't help\nThese titles follow it:\n\n- **Build the cold reader**\n\n"
        "| Vague | Clear |\n|---|---|\n| Fix stale rules | Sidebar: filter by Done |\n\n" + "More words here. " * 80)
EX = "## What this is about\nYou asked about titles.\n\n## What happened\n" + "Plain words. " * 20
BASE = {"type": "explain", "title": "A fixed title format", "headline": "Word order won't fix vague titles.",
        "context": "Titles", "answer": "Word order isn't the problem; a first slot naming a place is.",
        "zoom": {"big": "Clearer titles", "task": "Enforce a title format", "now": "Tested formats"},
        "explained": EX, "needs": {"kind": "none", "why": "Answered.", "next": "Nothing planned."}}


@pytest.fixture(scope="module")
def out():
    if shutil.which("node") is None:
        pytest.skip("node not installed")
    cards = {"full": dict(BASE, full_answer=FULL), "short": dict(BASE)}
    r = subprocess.run(["node", "-e", SCRIPT, str(LAST_CARD), json.dumps(cards)],
                       capture_output=True, text=True, timeout=30, check=True)
    return json.loads(r.stdout)


def test_full_answer_is_folded_under_in_short(out):
    h = out["full"]
    band = h[h.index(">In short<"):]
    assert band.index("Full answer") < band.index("Why word order")
    assert 'class="lm-xpl lm-fullans"' in band and 'aria-expanded="false"' in band
    # drawn as markdown: a heading, a list, a table
    assert "<table" in band and "<li" in band


def test_explain_more_stays_up_top(out):
    h = out["full"]
    assert h.index("Explain more") < h.index(">In short<")


def test_no_full_answer_no_toggle(out):
    assert "Full answer" not in out["short"]


def test_full_answer_does_not_count_toward_the_length():
    assert handoff_rules.length_problems(dict(BASE, full_answer=FULL)) == []


def test_full_answer_needs_the_short_answer():
    card = dict(BASE, full_answer=FULL)
    del card["answer"]
    card["compare"] = {"before": ["a"], "after": ["b"]}
    assert any("full_answer" in p for p in handoff_rules.format_problems(card))
    assert any("full_answer" in p for p in handoff_rules.format_problems(dict(BASE, full_answer="  ")))
    assert not any("full_answer" in p for p in handoff_rules.format_problems(dict(BASE, full_answer=FULL)))


def test_the_instructions_describe_it():
    assert '"full_answer"' in handoff_rules.INSTRUCTIONS
