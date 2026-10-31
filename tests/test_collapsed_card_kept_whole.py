"""An older, collapsed AI message never shows its handoff card as raw JSON
(fixed 2026-10-28: "for a smart termination deeper in chat history that gets
collapsed it shows the raw json instead of the polish").

Older assistant messages are cut to 600 characters behind "show more"
(`_fillMsgBody` in live-panel.js).  The cut sliced the ```handoff fence in
half, so mdParse saw no closing fence and drew the card's JSON as text.  Now
only the prose counts toward the limit and every card is kept whole.

Runs the real `_truncateKeepingCards` from live-panel.js and the real
`mdParse` from markdown.js under node.
"""
import json
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
LIVE = ROOT / "static" / "js" / "live-panel.js"
MD = ROOT / "static" / "js" / "markdown.js"

SCRIPT = r"""
global.window = global;
const fs = require('fs');
// A Windows checkout (core.autocrlf) has CRLF line endings; the end marker
// below is written with "\n", so normalise or indexOf misses it.
const live = fs.readFileSync(process.argv[1], 'utf8').replace(/\r\n/g, '\n');
const start = live.indexOf('function _truncateAtWord(');
const end = live.indexOf('/**\n * Populate (or re-populate)');
eval(fs.readFileSync(process.argv[2], 'utf8') + '\n' + live.slice(start, end) +
     '\nglobal._t = _truncateKeepingCards; global._md = mdParse;');
const cases = JSON.parse(process.argv[3]);
const out = {};
for (const [k, text] of Object.entries(cases)) {
  const r = _t(text, 600);
  const html = _md(r.text);
  const m = html.match(/data-card="([^"]*)"/g) || [];
  out[k] = { truncated: r.truncated, len: r.text.length, html,
             cards: m.map(s => s.slice(11, -1).replace(/&quot;/g, '"').replace(/&lt;/g, '<')
                                .replace(/&gt;/g, '>').replace(/&amp;/g, '&')) };
}
process.stdout.write(JSON.stringify(out));
"""

CARD = {"type": "done", "title": "Sidebar filter works", "headline": "x " * 60,
        "context": "y " * 80, "zoom": {"big": "a", "task": "b", "now": "c"},
        "explained": "z " * 100, "needs": {"kind": "none", "why": "w", "next": "n"}}
FENCE = "```handoff\n" + json.dumps(CARD, indent=1) + "\n```"


@pytest.fixture(scope="module")
def out():
    if shutil.which("node") is None:
        pytest.skip("node not installed")
    cases = {
        "card_only": FENCE,
        "short_prose": "Done.\n\n" + FENCE,
        "long_prose": ("Lots of words here. " * 60) + "\n\n" + FENCE,
        "plain": "Plain words. " * 80,
    }
    r = subprocess.run(["node", "-e", SCRIPT, str(LIVE), str(MD), json.dumps(cases)],
                       capture_output=True, text=True, timeout=30, check=True)
    return json.loads(r.stdout)


def test_a_long_card_alone_is_never_cut(out):
    assert len(FENCE) > 600
    assert out["card_only"]["truncated"] is False
    assert json.loads(out["card_only"]["cards"][0]) == CARD


def test_short_prose_plus_card_is_not_cut(out):
    assert out["short_prose"]["truncated"] is False
    assert json.loads(out["short_prose"]["cards"][0]) == CARD


def test_long_prose_is_cut_but_the_card_stays_whole(out):
    o = out["long_prose"]
    assert o["truncated"] is True
    assert len(o["cards"]) == 1 and json.loads(o["cards"][0]) == CARD
    assert '"headline"' not in o["html"].split('data-card=')[0]   # no raw JSON before the card


def test_plain_text_still_truncates(out):
    assert out["plain"]["truncated"] is True and out["plain"]["len"] <= 600
