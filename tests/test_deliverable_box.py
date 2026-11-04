"""The deliverable sits in its own box in Review & respond.

The summary is about the summary; the artifact sits in Review & respond: the
letter in its own box with Copy and Draft in Gmail out front and the rest under
More, and the answers as "Your verdict" chips led by Done.  An email body ends
at its sign-off: nothing such as a table of changes goes under the signature.
Runs the real last-card.js in node.
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

NONE = {"kind": "none", "why": "Ready.", "next": "Nothing else runs."}
LETTER = "Hello Sam,\n\nThe invoice is attached.\n\nBest Regards,\n\nAlex"


def _card(**dv):
    d = {"kind": "email", "title": "Invoice note", "body": LETTER,
         "meta": {"To": "sam@example.com", "Cc": "pat@example.com", "Subject": "Invoice"}}
    d.update(dv)
    return {"type": "deliverable", "title": "Invoice note", "headline": "The note is ready.",
            "answer": "Short.", "full_answer": "## Why\nBecause.", "needs": NONE, "deliverable": d}


@pytest.fixture(scope="module")
def out():
    if shutil.which("node") is None:
        pytest.skip("node not installed")
    cards = {"email": _card(),
             "memo": _card(kind="memo", meta=None),
             "long": _card(body="x" * 7000)}
    r = subprocess.run(["node", "-e", SCRIPT, str(LAST_CARD), json.dumps(cards)],
                       capture_output=True, text=True, encoding="utf-8", timeout=30, check=True)
    return json.loads(r.stdout)


def test_the_letter_is_in_review_and_respond_in_its_own_box(out):
    h = out["email"]
    summ = h[h.index(">Summary<"):h.index(">Review &amp; respond<")]
    assert "lm-dvbox" not in summ and "lm-dvopen" not in summ
    box = h[h.index('class="lm-dvbox"'):]
    assert box.index("dv-meta") < box.index("lm-dvopen") < box.index("lm-dvbar")
    assert h.index('class="lm-dvbox"') < h.index('<div class="dt">Your verdict</div>')


def test_copy_and_gmail_out_front_the_rest_under_more(out):
    h = out["email"]
    bar = h[h.index('class="lm-dvbar"'):]
    front, menu = bar.split('class="lm-dvmenu"', 1)
    assert 'data-dvact="copy"' in front and 'data-dvact="gmail"' in front
    assert "mail.google.com/mail/?view=cm" in front and "to=sam%40example.com" in front and "su=Invoice" in front
    for k in ('Copy with subject', 'Open in Mail app', 'Download for Word'):
        assert k in menu


def test_a_memo_has_no_mail_actions(out):
    h = out["memo"]
    assert 'data-dvact="gmail"' not in h and 'data-dvact="mail"' not in h
    assert 'data-dvact="word"' in h


def test_a_letter_too_long_for_an_address_opens_gmail_without_the_body(out):
    h = out["long"]
    href = h.split('data-dvact="gmail" href="', 1)[1].split('"', 1)[0]
    assert "body=" not in href and "to=" in href


def test_an_email_that_runs_past_its_signoff_is_sent_back():
    bad = _card(body=LETTER + "\n\n---\n\n## Changes\n| Where | What |\n|---|---|\n| 1 | x |")
    assert any("sign-off" in p for p in handoff_rules.email_tail_problems(bad))
    assert handoff_rules.email_tail_problems(_card()) == []
    # Only emails: a report may have tables after a "Thanks" line.
    assert handoff_rules.email_tail_problems(_card(kind="report", body=bad["deliverable"]["body"])) == []


def test_a_deliverable_needs_its_own_full():
    from engine import smart_termination as st
    c = _card()
    del c["full_answer"]
    assert any('"full_answer"' in p for p in st.depth_problems(c))
    assert "is NOT its Full" in handoff_rules.INSTRUCTIONS
