"""Sidebar status oriented to the person (2026-10-27).

The owner split "Idle" by what the last message needs from him: Decide,
Unblock, Review, Read, Send, Waiting (plus Approve / Working / Idle /
Sleeping from live state).  The verb comes from the final message's
```handoff card.  This file pins:

  * the card → verb mapping (``common/handoff_status.py``);
  * that the browser's copy in ``static/js/session-verb.js`` gives the same
    answer for every case (run under Node);
  * that the session summary carries ``next_step`` from the LAST message
    only: a newer user message or a newer card-less reply clears it;
  * the browser's ``display()`` rules: live state beats the card, Decide /
    Unblock survive sleep, a sleeping session is otherwise Done, a stopped
    awake session is never Idle (Review unless its card says Read / Send, and
    opening it does not clear that), a wake-up or retry countdown is Waiting.
    The workflow terminates (2026-10-27): Accept sleeps it (Done), Send back
    puts it to work (static/js/session-verdict.js).
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

from common.handoff_status import next_step, verb_from_card

ROOT = Path(__file__).resolve().parents[1]
JS = ROOT / "static" / "js" / "session-verb.js"


def _msg(card: dict, prose: str = "Some words first.") -> str:
    return prose + "\n\n```handoff\n" + json.dumps(card) + "\n```\n"


CASES = [
    ({"type": "needs_you", "needs": {"kind": "decision", "ask": "Pick one"}}, "decide"),
    ({"type": "needs_you", "needs": {"kind": "approve", "ask": "Go?"}}, "decide"),
    ({"type": "needs_you", "needs": {"kind": "action", "ask": "Log in"}}, "unblock"),
    ({"type": "blocked", "needs": {"kind": "none"}}, "unblock"),
    ({"type": "needs_you"}, "decide"),
    ({"type": "working", "needs": {"kind": "none"}}, "waiting"),
    ({"type": "deliverable", "deliverable": {"kind": "email"}}, "send"),
    ({"type": "deliverable", "deliverable": {"kind": "post"}}, "send"),
    ({"type": "deliverable", "deliverable": {"kind": "Report"}}, "read"),
    ({"type": "deliverable", "deliverable": {"kind": "audit findings"}}, "read"),
    ({"type": "deliverable"}, "send"),
    ({"type": "deliverable", "deliverable": {"kind": "presentation", "files": [{"label": "Zip", "url": "/static/scratch/a.zip"}]}}, "read"),
    ({"type": "explain"}, "read"),
    ({"type": "done", "needs": {"kind": "none"}}, "review"),
    ({"type": "done", "needs": {"kind": "decision"}}, "decide"),
    ({"type": "mystery"}, None),
    ({}, None),
]


@pytest.mark.parametrize("card,verb", CASES)
def test_card_to_verb(card, verb):
    assert verb_from_card(card) == verb
    got = next_step(_msg(card))
    assert (got or {}).get("verb") == verb


def test_last_block_wins_and_bad_json_is_no_card():
    two = _msg({"type": "done"}) + _msg({"type": "needs_you", "needs": {"kind": "action", "ask": "Log in"}})
    assert next_step(two) == {"verb": "unblock", "line": "Log in"}
    assert next_step("```handoff\n{not json\n```") is None
    assert next_step("no card here") is None
    assert next_step("```handoff\n[1, 2]\n```") is None


def test_line_is_the_ask_else_the_headline():
    assert next_step(_msg({"type": "needs_you", "headline": "H", "needs": {"kind": "decision", "ask": "A?"}}))["line"] == "A?"
    assert next_step(_msg({"type": "done", "headline": "Built it", "needs": {"kind": "none", "ask": "x"}}))["line"] == "Built it"


# ── The browser copy agrees ──────────────────────────────────────────────

NODE = shutil.which("node")

_HARNESS = r"""
const fs = require('fs'), vm = require('vm');
const ctx = { window: {}, console, JSON, String, Map, Set, RegExp, Array, Object };
ctx.window = ctx; vm.createContext(ctx);
vm.runInContext(fs.readFileSync(process.argv[2], 'utf8'), ctx);
const SV = ctx.SessionVerbCore;
// The drawn statuses follow the Smart termination switch (2026-10-28): off,
// every caller falls back to the process statuses.
const gate = [ctx.SessionVerb === undefined];
ctx.applySmartTerminationView(true); gate.push(ctx.SessionVerb === SV);
ctx.applySmartTerminationView(false); gate.push(ctx.SessionVerb === undefined);
ctx.applySmartTerminationView(true);
const input = JSON.parse(fs.readFileSync(0, 'utf8'));
const out = {};
out.cases = input.cases.map(t => { const s = SV.nextStep(t); return s ? s.verb : null; });
// display() rules, with stubbed page state.
const st = {}, unread = new Set(), rows = {};
ctx.getSessionStatus = id => st[id] || 'sleeping';
ctx._unreadSessions = unread;
ctx.SessionStore = { get: id => rows[id] || null };
ctx._sessionSubstatus = {}; ctx._sessionRetryState = {};
const d = id => SV.display(id);
const r = {};
st.a = 'question'; rows.a = { next_step: { verb: 'decide' } }; r.permission_beats_card = d('a');
st.b = 'working';  rows.b = { next_step: { verb: 'decide' } }; r.running_turn_is_working = d('b');
st.c = 'sleeping'; rows.c = { next_step: { verb: 'decide' } }; r.decide_survives_sleep = d('c');
st.c2 = 'sleeping'; rows.c2 = { next_step: { verb: 'review' } }; unread.add('c2'); r.accepted_review_is_done = d('c2');
st.z = 'sleeping'; r.sleeping_is_done = d('z');
st.e = 'idle'; rows.e = { next_step: { verb: 'review' } }; unread.add('e'); r.unread_review = d('e');
unread.delete('e'); r.opened_review_stays_review = d('e');
st.f = 'idle'; unread.add('f'); r.stopped_no_card_is_review = d('f');
st.g = 'idle'; r.seen_no_card_is_still_review = d('g');
st.m = 'idle'; rows.m = { next_step: { verb: 'send' } }; r.send_card = d('m');
st.h = 'idle'; ctx._sessionSubstatus.h = 'auto-resuming'; r.wakeup_is_waiting = d('h');
st.h2 = 'working'; ctx._sessionSubstatus.h2 = 'auto-resuming'; r.wakeup_turn_is_working = d('h2');
st.i = 'idle'; ctx._sessionRetryState.i = { retry_at: 1 }; r.retry_is_waiting = d('i');
st.j = 'idle'; rows.j = { next_step: { verb: 'waiting' } }; r.card_waiting = d('j');
st.k = 'idle'; rows.k = { next_step: { verb: 'decide' } };
SV.noteEntry('k', { kind: 'user', text: 'go with B' }); r.user_reply_clears_decide = d('k');
st.l = 'idle'; unread.add('l');
SV.noteEntry('l', { kind: 'asst', text: input.unblockMsg }); r.live_card_sets_verb = d('l');
st.n = 'idle'; rows.n = { message_count: 0 }; r.new_session_is_not_review = d('n');
r.new_session_has_no_verdict = SV.cardParts('n').accept;
SV.noteEntry('n', { kind: 'asst', text: 'hi' }); r.first_reply_makes_it_review = d('n');
r.rank_order = ['a', 'c', 'f', 'b', 'h', 'z'].map(id => SV.rank(id));
r.buckets = ['c', 'f', 'b', 'h', 'z'].map(id => SV.filterBucket(id));
r.verdict_foot = ['e', 'm', 'c', 'z'].map(id => SV.cardParts(id).accept);
out.display = r;
out.gate = gate;
process.stdout.write(JSON.stringify(out));
"""


@pytest.mark.skipif(NODE is None, reason="node not installed")
def test_browser_copy_matches_and_display_rules(tmp_path):
    harness = tmp_path / "h.js"
    harness.write_text(_HARNESS)
    texts = [_msg(c) for c, _ in CASES] + ["no card", "```handoff\n{bad\n```"]
    payload = {"cases": texts,
               "unblockMsg": _msg({"type": "needs_you", "needs": {"kind": "action", "ask": "Log in"}})}
    res = subprocess.run([NODE, str(harness), str(JS)], input=json.dumps(payload),
                         capture_output=True, text=True, timeout=30)
    assert res.returncode == 0, res.stderr
    out = json.loads(res.stdout)
    assert out["cases"] == [v for _, v in CASES] + [None, None]
    assert out["gate"] == [True, True, True]
    assert out["display"] == {
        "permission_beats_card": "approve",
        "running_turn_is_working": "working",
        "decide_survives_sleep": "decide",
        "accepted_review_is_done": "done",
        "sleeping_is_done": "done",
        "unread_review": "review",
        "opened_review_stays_review": "review",
        "stopped_no_card_is_review": "review",
        "seen_no_card_is_still_review": "review",
        "send_card": "send",
        "wakeup_is_waiting": "waiting",
        "wakeup_turn_is_working": "working",
        "retry_is_waiting": "waiting",
        "card_waiting": "waiting",
        "user_reply_clears_decide": "review",
        "live_card_sets_verb": "unblock",
        "new_session_is_not_review": "idle",
        "new_session_has_no_verdict": "",
        "first_reply_makes_it_review": "review",
        "rank_order": [0, 0, 1, 2, 3, 5],
        "buckets": ["question", "question", "working", "working", "sleeping"],
        "verdict_foot": ["", "", "", ""],   # no answer buttons on sidebar cards (owner, 2026-10-27)
    }


# ── The session summary carries the LAST message's card ──────────────────

def _line(kind: str, content, ts: str = "2026-10-27T17:00:00Z", **extra) -> str:
    obj = {"type": kind, "timestamp": ts, "message": {"role": kind, "content": content}}
    obj.update(extra)
    return json.dumps(obj)


def _summary(tmp_path, lines):
    from app.sessions import load_session_summary
    proj = tmp_path / "proj"
    proj.mkdir(exist_ok=True)
    p = proj / ("s%d.jsonl" % len(list(proj.iterdir())))
    p.write_text("\n".join(lines) + "\n")
    return load_session_summary(p)


def test_summary_next_step(tmp_path):
    ask = _msg({"type": "needs_you", "needs": {"kind": "decision", "ask": "Pick B or C"}})
    text_block = [{"type": "text", "text": ask}]
    # Final message carries a card.
    s = _summary(tmp_path, [_line("user", "do it"), _line("assistant", text_block)])
    assert s["next_step"] == {"verb": "decide", "line": "Pick B or C"}
    # A tool call after it (no text) does not hide it.
    tool = [{"type": "tool_use", "id": "t", "name": "Bash", "input": {}}]
    s = _summary(tmp_path, [_line("assistant", text_block), _line("assistant", tool),
                            _line("user", [{"type": "tool_result", "tool_use_id": "t", "content": "ok"}])])
    assert s["next_step"]["verb"] == "decide"
    # The user answered: a newer turn began.
    s = _summary(tmp_path, [_line("assistant", text_block), _line("user", "go with C")])
    assert s["next_step"] is None
    # A newer reply without a card.
    s = _summary(tmp_path, [_line("assistant", text_block), _line("assistant", [{"type": "text", "text": "ok"}])])
    assert s["next_step"] is None
    # A sidechain line never counts.
    s = _summary(tmp_path, [_line("assistant", text_block),
                            _line("assistant", [{"type": "text", "text": "sub"}], isSidechain=True)])
    assert s["next_step"]["verb"] == "decide"
    # No assistant text at all.
    assert _summary(tmp_path, [_line("user", "hi")])["next_step"] is None



# ── Free text never sets a status (2026-10-27, owner) ───────────────────
# Only the card's type does.  Text headings, however worded, set nothing.

HEADING_CASES = [
    ("**What we're working on:** x\n**Where it stands:** y\n**What's needed from you:** Decide: B or C?", None),
    ("## What\u2019s needed from you\nOpen http://localhost:5050 and check the header.", None),
    ("**What's needed from you:** Nothing right now. It ships with your next Update.", None),
    ("**What's needed from you:** Nothing right now: tests finish in about 10 minutes and I'll report.", None),
    ("Just a plain reply with no handoff.", None),
]


@pytest.mark.parametrize("text,verb", HEADING_CASES)
def test_heading_handoff_to_verb(text, verb):
    from common.handoff_status import next_step
    s = next_step(text)
    assert (s["verb"] if s else None) == verb


@pytest.mark.skipif(NODE is None, reason="node not installed")
def test_browser_reads_the_heading_handoff_the_same_way(tmp_path):
    h = tmp_path / "h.js"
    h.write_text(r"""
const fs = require('fs'), vm = require('vm');
const ctx = { window: {}, console, JSON, String, Map, Set, RegExp, Array, Object };
ctx.window = ctx; vm.createContext(ctx);
vm.runInContext(fs.readFileSync(process.argv[2], 'utf8'), ctx);
const texts = JSON.parse(fs.readFileSync(0, 'utf8'));
process.stdout.write(JSON.stringify(texts.map(t => { const s = ctx.SessionVerbCore.nextStep(t); return s ? s.verb : null; })));
""")
    res = subprocess.run([NODE, str(h), str(JS)], input=json.dumps([t for t, _ in HEADING_CASES]),
                         capture_output=True, text=True, timeout=30)
    assert res.returncode == 0, res.stderr
    assert json.loads(res.stdout) == [v for _, v in HEADING_CASES]



@pytest.mark.skipif(NODE is None, reason="node not installed")
def test_sidebar_draws_three_statuses(tmp_path):
    """Owner's pick B, 2026-10-28: Needs you, Working, Done; one picture each."""
    h = tmp_path / "h3.js"
    h.write_text(r"""
const fs = require('fs'), vm = require('vm');
const ctx = { window: {}, console, JSON, String, Map, Set, RegExp, Array, Object };
ctx.window = ctx; vm.createContext(ctx);
vm.runInContext(fs.readFileSync(process.argv[2], 'utf8'), ctx);
const SV = ctx.SessionVerbCore;
const st = {}, rows = {};
ctx.getSessionStatus = id => st[id] || 'sleeping';
ctx.SessionStore = { get: id => rows[id] || null };
ctx._sessionSubstatus = {}; ctx._sessionRetryState = {};
const set = (id, s, verb) => { st[id] = s; rows[id] = { message_count: 3, next_step: verb ? { verb } : null }; };
set('dec', 'idle', 'decide'); set('rev', 'idle', 'review'); set('snd', 'idle', 'send'); set('q', 'question');
set('w', 'working'); set('wt', 'idle', 'waiting'); set('dn', 'sleeping');
const out = {};
for (const id of Object.keys(st)) { const p = SV.cardParts(id); out[id] = [p.label, p.cls, SV.filterBucket(id)]; }
process.stdout.write(JSON.stringify(out));
""")
    res = subprocess.run([NODE, str(h), str(JS)], capture_output=True, text=True, timeout=30)
    assert res.returncode == 0, res.stderr
    out = json.loads(res.stdout)
    needs = ["Needs you", "vs-needs", "question"]
    assert out["dec"] == needs and out["rev"] == needs and out["snd"] == needs and out["q"] == needs
    assert out["w"][:2] == ["Working", "vs-working"] and out["wt"][:2] == ["Working", "vs-working"]
    assert out["dn"] == ["Done", "vs-done", "sleeping"]
