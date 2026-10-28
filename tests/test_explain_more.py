""""Explain more" on the end-of-turn card (owner's pick B of
static/scratch/card-lost.html, 2026-10-28: "im thinking a im lost or confused
on what this is telling me button with a more detailed version with less
conciseness constraints would be nice").

The card's "explained" text is drawn behind an "Explain more" button under the
This task / Just now lines, in the card's quiet grey whatever its status (the
owner: "idk that green or any status based color makes sense"), with no icon.
"""
import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

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

EX = "## What this is about\nThe **deliverable** card lost its preview.\n\n## What you do\nNothing."
ZOOM = {"big": "Making cards easy to read", "task": "Drop the inline notes", "now": "Done"}


@pytest.fixture(scope="module")
def out():
    if shutil.which("node") is None:
        pytest.skip("node not installed")
    base = {"type": "done", "title": "T", "headline": "H", "context": "x",
            "needs": {"kind": "none", "why": "y", "next": "z"}}
    cards = {
        "zoomed": dict(base, zoom=ZOOM, explained=EX),
        "bare": dict(base, explained=EX),
        "none": dict(base, zoom=ZOOM),
        "needs": dict(base, type="needs_you", zoom=ZOOM, explained=EX,
                      needs={"kind": "action", "ask": "a", "why": "w", "steps": [{"text": "s"}],
                             "replies": [{"key": "D", "label": "I did it now", "reply": "done"}]}),
    }
    r = subprocess.run(["node", "-e", SCRIPT, str(LAST_CARD), json.dumps(cards)],
                       capture_output=True, text=True, timeout=30, check=True)
    return json.loads(r.stdout)


def test_explain_more_sits_under_the_sentence_and_history_at_the_bottom(out):
    # The This task / Just now lines moved into a Session history section at
    # the bottom of the card (owner, 2026-10-28, card-title-crumb.html);
    # Explain more stays at the top, right under the sentence.
    h = out["zoomed"]
    assert h.index('class="lm-subtitle"') < h.index('class="lm-xpl-tg"') < h.index(">History<") < h.index("Just now")
    assert ">Explain more <span class=\"tg\">" in h
    # the long text is markdown, closed until tapped
    assert "<h4>What this is about</h4>" in h and "<b>deliverable</b>" in h
    assert 'aria-expanded="false"' in h and 'class="lm-xpl open"' not in h


def test_a_card_without_zoom_lines_still_gets_it(out):
    assert '<div class="lm-xpl-wrap"><div class="lm-xpl">' in out["bare"]


def test_no_explained_text_no_button(out):
    assert "lm-xpl" not in out["none"]


def test_no_icon_and_no_status_colour():
    css = CSS.read_text()
    block = css[css.index('/* "Explain more"'):]
    rules = [r for r in re.findall(r"[^{}]*\{[^}]*\}", block) if "lm-xpl" in r]
    assert rules
    assert not any("--tone" in r for r in rules), "Explain more must not take the card's status colour"
    js = LAST_CARD.read_text()
    tg = js[js.index('class="lm-xpl-tg"'):js.index('class="lm-xpl-tg"') + 200]
    assert "<svg" not in tg.split("</button>")[0]


def test_it_does_not_reuse_the_more_bands_class():
    """The card's MORE band already uses .lm-more (and a click handler on it)."""
    js = LAST_CARD.read_text()
    assert 'class="lm-more"><button type="button" class="lm-xpl-tg"' not in js
