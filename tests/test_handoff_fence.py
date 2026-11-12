"""A ``` inside a handoff card's text never ends the card.

The checker (src/engine/handoff_rules.py) only closes a fence at the start of a
line.  If the chat renderer (markdown.js), the sidebar status (session-verb.js)
or the session list (src/shared/handoff_status.py) closed it at a mid-line
```, the rest of the card's JSON would show raw.  All four
readers must find the same card.  Runs the real JS in node.
"""

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from shared import handoff_status
from engine import handoff_rules

ROOT = Path(__file__).resolve().parents[1]
NODE = shutil.which("node")

CARD = {"type": "done", "title": "Tables now draw in cards",
        "headline": "Cards now draw tables.",
        "status": {"done": ["Tables, --- dividers and ``` code blocks now draw", "x"]},
        "needs": {"kind": "none", "why": "y", "next": "z"}}
# The card as a session writes it: pretty JSON, so the ``` sits mid-line.
MSG = "```handoff\n" + json.dumps(CARD, indent=1, ensure_ascii=False) + "\n```"

PLAIN = ("Run this:\n```bash\necho `date`\n```\nThen:\n  ```\n  indented\n  ```\n"
         "~~~py\nx = 1\n~~~\nInline ``` mid-line stays text.")

SCRIPT = r"""
global.window = global; global.document = { createElement: () => ({}) };
const fs = require('fs');
eval(fs.readFileSync(process.argv[1], 'utf8'));
eval(fs.readFileSync(process.argv[2], 'utf8'));
const [msg, plain] = JSON.parse(fs.readFileSync(0, 'utf8'));
const html = mdParse(msg);
const m = /data-card="([^"]*)"/.exec(html);
const card = m ? m[1].replace(/&quot;/g, '"').replace(/&lt;/g, '<').replace(/&gt;/g, '>').replace(/&amp;/g, '&') : null;
process.stdout.write(JSON.stringify({ card, html, verbCard: SessionVerbCore.lastCard(msg), plain: mdParse(plain) }));
"""


@pytest.fixture(scope="module")
def js():
    if NODE is None:
        pytest.skip("node not installed")
    r = subprocess.run([NODE, "-e", SCRIPT, str(ROOT / "src/web/static/js/markdown.js"), str(ROOT / "src/web/static/js/session-verb.js")],
                       input=json.dumps([MSG, PLAIN]), capture_output=True, text=True, timeout=30, check=True)
    return json.loads(r.stdout)


def test_the_checker_finds_the_card():
    card, _prose, _err = handoff_rules.extract(MSG)
    assert card == CARD


def test_the_session_list_finds_the_card():
    assert handoff_status.last_card(MSG) == CARD


def test_the_chat_draws_the_whole_card(js):
    assert js["card"] is not None and json.loads(js["card"]) == CARD
    # nothing of the card leaks out of the placeholder as raw text
    assert js["html"].count("vn-card-ph") == 1
    assert '"needs"' not in js["html"].split("</div>")[-1]


def test_the_sidebar_status_finds_the_card(js):
    assert js["verbCard"] == CARD


def test_ordinary_code_blocks_still_draw(js):
    h = js["plain"]
    assert h.count('<pre class="vn-code">') == 3
    assert "lang-bash" in h and "lang-py" in h
    assert "indented" in h and "```" in h  # the mid-line one stays as text
