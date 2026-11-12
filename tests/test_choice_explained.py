"""Each Decide choice explains itself.

A Decide popup must not show its choices as a short label, a line of commit
hashes and one shared reason.  Each choice says what it is, what advantage it
has, and why the recommended one wins.  Every reply of a decision now carries
``what``, ``gain`` and ``verdict``; ``choice_explain_problems`` sends back a
card without them, and the popup draws them (``_explainHTML``).

The cards below are made up; they only reproduce the shape of a card that lacked explanations.
"""

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from engine import handoff_rules as hr

LAST_CARD = Path(__file__).resolve().parents[1] / "src" / "web" / "static" / "js" / "last-card.js"


def _card(replies, **needs):
    n = {"kind": "decision", "ask": "Which starting point should both trial runs use?",
         "why": "It keeps the app the same as the first trial.", "replies": replies}
    n.update(needs)
    return {"type": "needs_you", "title": "Trial starting point", "headline": "Pick the starting point",
            "needs": n}


# The bad shape: labels, hashes in the detail, no explanation per choice.
BARE = [
    {"key": "A", "label": "Last week's app, today's tools", "detail": "New commit: a1b2c3d app plus tools",
     "reply": "A", "recommended": True},
    {"key": "B", "label": "Today's code for both", "detail": "Baseline 9f8e7d6 for both", "reply": "B"},
]

GOOD = [
    dict(BARE[0],
         what="Both runs start from the app exactly as the first trial had it, with today's tools added.",
         gain="Any difference between the two trials comes from the tools, not from the app changing.",
         verdict="Beats today's code for both because that one changes the app too, so a gap could not be explained."),
    dict(BARE[1],
         what="Both runs start from today's code, app and tools together, as it is right now.",
         gain="Nothing to build first, and the trial tests the code you would actually ship today.",
         verdict="Here the app changed too, so a different result could come from either change."),
]


def test_the_bare_choices_he_saw_are_sent_back():
    out = " ".join(hr.choice_explain_problems(_card(BARE)))
    assert "Missing" in out and "A.what" in out and "B.verdict" in out
    # the full review of a final message sends it back too
    msg = "```handoff\n" + json.dumps(_card(BARE)) + "\n```"
    assert "explains itself" in (hr.review(msg) or "")


def test_explained_choices_pass():
    assert hr.choice_explain_problems(_card(GOOD)) == []


def test_a_hash_in_the_explanation_is_sent_back():
    bad = [dict(GOOD[0], what="Both runs start from commit a1b2c3d with today's tools added on top."), GOOD[1]]
    out = " ".join(hr.choice_explain_problems(_card(bad)))
    assert '"a1b2c3d" (A.what)' in out


def test_one_verdict_shared_by_every_choice_is_sent_back():
    same = "It keeps the app the same as the first trial run did."
    out = " ".join(hr.choice_explain_problems(_card([dict(GOOD[0], verdict=same), dict(GOOD[1], verdict=same)])))
    assert "share one verdict" in out


def test_a_verdict_that_repeats_the_headings_name_is_sent_back():
    # A verdict must not repeat the name the popup heading already shows.
    echo = dict(GOOD[1], verdict=GOOD[0]["label"] + " is recommended instead: here the app changed too.")
    out = " ".join(hr.choice_explain_problems(_card([GOOD[0], echo])))
    assert "opens on the recommended choice's name" in out and "of B" in out


def test_a_one_word_line_is_too_short():
    out = " ".join(hr.choice_explain_problems(_card([dict(GOOD[0], gain="Cleaner."), GOOD[1]])))
    assert "Too short: A.gain" in out


def test_a_pick_between_mockups_and_an_approval_are_exempt():
    shots = [dict(r, screens=[{"label": "x", "url": "/static/scratch/x.html#" + r["key"]}]) for r in BARE]
    assert hr.choice_explain_problems(_card(shots)) == []
    assert hr.choice_explain_problems(_card(BARE, kind="approve")) == []


def test_the_explanations_do_not_count_toward_the_card_length():
    long = [dict(r, what=r["what"] + " " + "word " * 12) for r in GOOD]
    assert not any("needs.replies" in p and "total" in p for p in hr.length_problems(_card(long)))
    assert hr.length_problems(_card(GOOD)) == hr.length_problems(_card(BARE))


def test_sessions_are_told_to_explain_each_choice():
    for w in ('"what"', '"gain"', '"verdict"'):
        assert w in hr.INSTRUCTIONS


SCRIPT = r"""
global.window = global;
global.location = { host: '127.0.0.1:5050' };
global.document = { addEventListener() {}, querySelectorAll() { return []; } };
eval(require('fs').readFileSync(process.argv[1], 'utf8'));
const rs = JSON.parse(process.argv[2]);
process.stdout.write(JSON.stringify(rs.map(r => VNLastCard.explainHTML(r, rs)).concat([VNLastCard.explainHTML({other: true}, rs)])));
"""


def test_the_popup_draws_the_three_lines_and_names_the_winner():
    if shutil.which("node") is None:
        pytest.skip("node not installed")
    r = subprocess.run(["node", "-e", SCRIPT, str(LAST_CARD), json.dumps(GOOD)],
                       capture_output=True, text=True, encoding="utf-8", timeout=30, check=True)
    a, b, other = json.loads(r.stdout)
    for h in ("What it is", "What it gets you"):
        assert h in a and h in b
    assert "Why it’s recommended" in a and 'class="xp-s rec"' in a
    assert "Last week&#39;s app, today&#39;s tools is recommended instead" in b.replace("'", "&#39;") or \
        "Last week's app, today's tools is recommended instead" in b
    assert 'class="xp-s alt"' in b
    assert other == ""


ECHO_SCRIPT = SCRIPT.replace("rs.map(r => VNLastCard.explainHTML(r, rs)).concat([VNLastCard.explainHTML({other: true}, rs)])",
                             "[VNLastCard.explainHTML(rs[1], rs)]")


def test_the_popup_drops_the_name_the_heading_already_says():
    if shutil.which("node") is None:
        pytest.skip("node not installed")
    echo = [GOOD[0], dict(GOOD[1], verdict=GOOD[0]["label"] + " is recommended instead: here the app changed too.")]
    r = subprocess.run(["node", "-e", ECHO_SCRIPT, str(LAST_CARD), json.dumps(echo)],
                       capture_output=True, text=True, encoding="utf-8", timeout=30, check=True)
    (b,) = json.loads(r.stdout)
    body = b.split('class="xp-s alt"', 1)[1].split('class="xp-b">', 1)[1]
    assert body.startswith("Here the app changed too.")
