"""Six fixed sections on every summary, and two true lines (owner, 2026-10-31).

Two sessions asked "who is the president" drew different sections: one had
searched the web (a Status band, a folded More line), the other had answered
from memory (neither).  Every summary now draws Header · Summary · What was
done · Checked · History · Review & respond, whatever the session wrote.  What was
done and Checked each open on a line the session writes ("Answered directly
from training data; no web search"), and that line is held to the tools the
session actually ran this turn.
"""
import json
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from daemon import handoff_evidence as ev  # noqa: E402
from daemon import handoff_rules  # noqa: E402
from daemon import smart_termination as st  # noqa: E402

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

EX = "## What this is about\nA question about the officeholder.\n\n## What happened\n" + "Plain words. " * 30
ZOOM = {"big": "Test questions", "task": "Who is the president", "now": "Answered"}
NONE = {"kind": "none", "why": "Answered.", "next": "Nothing planned."}

# A question answered from memory, and the same question answered after a web search.
FROM_MEMORY = {"type": "explain", "path": ["Quick question"], "title": "US president",
               "headline": "A made-up answer for the test.", "answer": "Short version.",
               "explained": EX, "full_answer": "## Detail\nMore.", "zoom": ZOOM, "needs": NONE,
               "offer": "Look up another country",
               "what_was_done": "Answered directly from training data; no web search, no tools used.",
               "checked": "No research beyond base training data."}
SEARCHED = {"type": "explain", "path": ["General question"], "title": "Current US president",
            "headline": "A made-up answer for the test.", "answer": "Short version.",
            "explained": EX, "full_answer": "## Detail\nMore.", "zoom": ZOOM, "needs": NONE,
            "status": {"done": ["Checked current sources"], "remaining": [], "broken": []},
            "verified": [{"check": "Web search for the officeholder", "result": "current reports agree", "ok": True}],
            "links": [{"label": "A news source", "kind": "link", "url": "https://example.com/news"}],
            "what_was_done": "Searched the web for the current officeholder.",
            "checked": "One web search; one news source below."}
DONE = {"type": "done", "path": ["Sessions list"], "title": "Archive sessions",
        "headline": "Sessions can be archived from the menu.", "answer": "Archive sits under Rename.",
        "explained": EX, "full_answer": "## Detail\nMore.", "zoom": ZOOM, "needs": NONE,
        "status": {"done": ["Archive in the menu"]},
        "links": [{"label": "The menu", "kind": "image", "url": "/static/scratch/x.png"}],
        "validate": {"how": "Right-click a session", "url": "/static/scratch/x.html"},
        "what_was_done": "Added Archive to the session menu.", "checked": "Ran the archive tests."}
SHOTS_ONLY = dict(FROM_MEMORY, links=[{"label": "A picture", "kind": "image", "url": "/static/scratch/p.png"}])
STUCK = {"type": "needs_you", "path": ["Bridge"], "title": "Sign-in", "headline": "It needs you to sign in.",
         "answer": "Sign in once.", "explained": EX, "full_answer": "## Why\nA person must.", "zoom": ZOOM,
         "what_was_done": "Tried the saved session and a fresh sign-in.", "checked": "Both stopped at the human check.",
         "needs": {"kind": "action", "ask": "Sign in", "why": "Only a person can.", "next": "It carries on.",
                   "tried": [{"what": "Saved session", "result": "expired"}, {"what": "Fresh sign-in", "result": "human check"}],
                   "only_you": "A person must pass the check.",
                   "replies": [{"key": "A", "label": "I did it", "reply": "I signed in"}]},
         "finding": {"what": "Old windows are open", "risk": "memory", "recommend": "close them"}}
OLD = {"type": "explain", "title": "An old summary", "headline": "Written before the two lines existed.",
       "answer": "Short.", "needs": NONE}


@pytest.fixture(scope="module")
def out():
    if shutil.which("node") is None:
        pytest.skip("node not installed")
    cards = {"memory": FROM_MEMORY, "searched": SEARCHED, "done": DONE, "shots": SHOTS_ONLY, "stuck": STUCK, "old": OLD}
    r = subprocess.run(["node", "-e", SCRIPT, str(LAST_CARD), json.dumps(cards)],
                       capture_output=True, text=True, timeout=30, check=True)
    return json.loads(r.stdout)


def _sections(h):
    """The top-level section names, in order (nested parts of Review & respond excluded)."""
    names, depth, i = [], 0, 0
    for m in re.finditer(r'<div class="lm-dl[^"]*"><div class="dt">([^<]*)</div><div class="dd">|<div class="lm-dl|</div>', h):
        pass
    # Parse properly: walk div tags and track nesting of .lm-dl sections.
    stack = []
    for m in re.finditer(r'<div([^>]*)>|</div>', h):
        if m.group(0) == '</div>':
            if stack:
                stack.pop()
            continue
        attrs = m.group(1) or ''
        is_sec = re.search(r'class="lm-dl\b', attrs) is not None
        if is_sec and not any(stack):
            dt = re.match(r'<div class="dt">([^<]*)</div>', h[m.end():])
            names.append(dt.group(1) if dt else '?')
        stack.append(is_sec)
    return names


SIX = ["Summary", "What was done", "Checked", "History", "Review &amp; respond"]


def test_the_same_question_draws_the_same_sections(out):
    assert _sections(out["memory"]) == SIX
    assert _sections(out["searched"]) == SIX


def test_every_kind_of_summary_draws_the_same_sections(out):
    for k in ("done", "shots", "stuck"):
        assert _sections(out[k]) == SIX, k


def test_the_two_lines_open_their_sections(out):
    h = out["memory"]
    done = h[h.index('<div class="dt">What was done</div>'):h.index('<div class="dt">Checked</div>')]
    assert "Answered directly from training data; no web search, no tools used." in done
    chk = h[h.index('<div class="dt">Checked</div>'):h.index('<div class="dt">History</div>')]
    assert "No research beyond base training data." in chk


def test_checks_and_links_sit_in_checked_and_work_in_what_was_done(out):
    h = out["searched"]
    done = h[h.index('<div class="dt">What was done</div>'):h.index('<div class="dt">Checked</div>')]
    chk = h[h.index('<div class="dt">Checked</div>'):h.index('<div class="dt">History</div>')]
    assert "Checked current sources" in done
    assert "Web search for the officeholder" in chk and "example.com/news" in chk
    assert ">More<" not in h and ">Status<" not in h


def test_your_move_stacks_its_parts_in_order(out):
    h = out["stuck"]
    move = h[h.index('<div class="dt">Review &amp; respond</div>'):]
    order = [move.index(f'<div class="dt">{n}</div>') for n in ("Tried", "To do", "Also found")]
    assert order == sorted(order)
    assert "lm-whyask" in move and "Only a person can." in move


def test_screenshots_always_live_in_validate(out):
    for k in ("done", "shots"):
        move = out[k][out[k].index('<div class="dt">Review &amp; respond</div>'):]
        assert '<div class="dt">Validate</div>' in move, k
        assert ">Screens<" not in out[k]


def test_an_old_summary_still_draws_the_fixed_sections(out):
    names = _sections(out["old"])
    assert names[-3:] == ["What was done", "Checked", "Review &amp; respond"] or names[-2:] == ["Checked", "Review &amp; respond"]
    assert "Nothing listed." in out["old"]


# ── The rules ──

def test_every_card_needs_both_lines():
    card = dict(FROM_MEMORY)
    del card["what_was_done"]
    del card["checked"]
    probs = st.depth_problems(card)
    assert any('"what_was_done"' in p for p in probs) and any('"checked"' in p for p in probs)
    assert st.depth_problems(FROM_MEMORY) == []


def test_the_lines_do_not_count_toward_the_length_but_are_capped():
    long = dict(FROM_MEMORY, checked=" ".join(["word"] * 30))
    assert any("checked is 30 words" in p for p in handoff_rules.length_problems(long))
    assert not any(p.startswith("Too long") for p in handoff_rules.length_problems(FROM_MEMORY))


def test_the_instructions_describe_the_sections():
    text = handoff_rules.INSTRUCTIONS
    assert '"what_was_done"' in text and '"checked"' in text and "Review & respond" in text


def _facts(*tools):
    f = ev.TurnFacts()
    f.turn_actions = [ev.Action(tool=t, detail=d, ok=True) for t, d in tools]
    return f


def test_a_line_that_denies_a_search_that_happened_is_sent_back():
    probs = ev.research_problems(FROM_MEMORY, _facts(("WebSearch", "who is the president")))
    assert probs and "WebSearch" in probs[0]


def test_a_line_that_claims_a_search_that_never_ran_is_sent_back():
    probs = ev.research_problems(SEARCHED, _facts())
    assert probs and "no web search or page fetch ran" in probs[0]


def test_a_line_that_denies_tools_it_used_is_sent_back():
    probs = ev.research_problems(FROM_MEMORY, _facts(("Read", "/notes.md")))
    assert probs and "Read" in probs[0]


def test_true_lines_pass():
    assert ev.research_problems(FROM_MEMORY, _facts()) == []
    assert ev.research_problems(FROM_MEMORY, _facts(("ToolSearch", "load tools"))) == []
    assert ev.research_problems(SEARCHED, _facts(("WebSearch", "q"))) == []
    assert ev.research_problems(SEARCHED, _facts(("Agent", "fetch https://example.com/news"))) == []


def test_unknown_turn_is_not_judged():
    f = ev.TurnFacts()
    assert ev.research_problems(SEARCHED, f) == []
    f2 = _facts()
    f2.complete = False
    assert ev.research_problems(SEARCHED, f2) == []


def test_the_turn_is_counted_from_his_latest_message():
    class E:
        def __init__(self, **kw):
            self.__dict__.update(kw)
    entries = [E(kind="user", text="first question"), E(kind="tool_use", name="WebSearch", desc="old", id="t1"),
               E(kind="tool_result", tool_use_id="t1", text="ok", is_error=False),
               E(kind="asst", text="answer"), E(kind="user", text="who is the president")]
    f = ev.facts_from_entries(entries)
    assert f.turn_actions == []
    assert ev.research_problems(FROM_MEMORY, f) == []
