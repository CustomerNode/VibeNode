"""Standby and marked Done (owner, 2026-10-28, static/scratch/standby-status.html
option A): a fourth sidebar status between Needs you and Done, set by the
person and ended by the session's next message.

Server: ``app/session_holds.py`` and ``POST /api/session-hold/<sid>``.
Browser: ``static/js/session-verb.js`` (what is drawn), run in node.
"""

import json
import shutil
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
VERB_JS = ROOT / "static" / "js" / "session-verb.js"
NODE = shutil.which("node")


def _line(kind, text, ts, sid):
    return json.dumps({"type": kind, "message": {"role": kind, "content": text}, "timestamp": ts, "sessionId": sid})


def _iso(minutes_ago: float) -> str:
    return (datetime.now(timezone.utc) - timedelta(minutes=minutes_ago)).isoformat()


@pytest.fixture()
def proj(tmp_path, monkeypatch):
    root = tmp_path / "projects"
    root.mkdir()
    import app.config as cfg
    import app.routes.sessions_api as sa
    import app.sessions as sess
    monkeypatch.setattr(cfg, "_CLAUDE_PROJECTS", root)
    monkeypatch.setattr(sa, "_CLAUDE_PROJECTS", root)
    cfg._summary_cache.clear()
    cfg._summary_cache_index.clear()
    sess._orphan_cache.clear()
    name = "C--Users-test-q"
    (root / name).mkdir()
    for sid in ("deck", "other"):
        (root / name / f"{sid}.jsonl").write_text(
            _line("user", "Make the deck", _iso(30), sid) + "\n" + _line("assistant", "Pick a style?", _iso(29), sid) + "\n",
            encoding="utf-8")
    return root / name


@pytest.fixture()
def client():
    from app import create_app
    return create_app(testing=True).test_client()


def _list(client, proj):
    return {s["id"]: s for s in client.get(f"/api/sessions?project={proj.name}").get_json()}


def test_no_holds_lists_exactly_as_before(client, proj):
    rows = _list(client, proj)
    assert "hold" not in rows["deck"] and "hold" not in rows["other"]


def test_standby_and_done_are_kept_and_listed(client, proj):
    r = client.post("/api/session-hold/deck", json={"state": "standby", "project": proj.name})
    assert r.status_code == 200 and r.get_json()["state"] == "standby"
    rows = _list(client, proj)
    assert rows["deck"]["hold"] == "standby" and "hold" not in rows["other"]
    client.post("/api/session-hold/deck", json={"state": "done", "project": proj.name})
    assert _list(client, proj)["deck"]["hold"] == "done"
    client.post("/api/session-hold/deck", json={"state": None, "project": proj.name})
    assert "hold" not in _list(client, proj)["deck"]


def test_a_new_message_ends_the_hold(client, proj):
    client.post("/api/session-hold/deck", json={"state": "standby", "project": proj.name})
    assert _list(client, proj)["deck"]["hold"] == "standby"
    with open(proj / "deck.jsonl", "a", encoding="utf-8") as f:
        f.write(_line("user", "ok, HTML slides", _iso(0), "deck") + "\n")
    assert "hold" not in _list(client, proj)["deck"]


def test_the_route_refuses_bad_input(client, proj):
    assert client.post("/api/session-hold/deck", json={"state": "later", "project": proj.name}).status_code == 400
    assert client.post("/api/session-hold/a..b", json={"state": "done", "project": proj.name}).status_code == 400


def test_state_for_pins_the_hold_to_the_newest_message():
    from app.session_holds import state_for
    assert state_for({"state": "standby", "at": 100.0}, 100.0) == "standby"
    assert state_for({"state": "done", "at": 100.0}, 100.3) == "done"       # clock slack
    assert state_for({"state": "done", "at": 100.0}, 105.0) is None
    assert state_for({"state": "nope", "at": 100.0}, 100.0) is None
    assert state_for(None, 1.0) is None


# ── The one-time backfill: the backlog is marked Done once (2026-10-28) ──

def _row(sid, ts, verb=None):
    return {"id": sid, "last_activity_ts": ts, "next_step": {"verb": verb} if verb else None}


def test_backfill_marks_old_sessions_done_once(tmp_path):
    from app import session_holds as sh
    old = sh.BACKFILL_CUTOFF_TS - 86400
    rows = [_row("old_review", old, "review"), _row("old_plain", old),
            _row("old_decide", old, "decide"), _row("old_unblock", old, "unblock"),
            _row("recent", sh.BACKFILL_CUTOFF_TS + 60, "review"), _row("never", 0)]
    (tmp_path / sh.FILENAME).write_text(json.dumps({"old_plain": {"state": "standby", "at": old, "set": 1}}), encoding="utf-8")
    assert sh.backfill_reviewed(rows, tmp_path) == 1
    holds = json.loads((tmp_path / sh.FILENAME).read_text(encoding="utf-8"))
    assert holds["old_review"]["state"] == "done" and holds["old_review"]["at"] == old
    assert holds["old_plain"]["state"] == "standby"           # his own choice is kept
    for sid in ("old_decide", "old_unblock", "recent", "never"):   # still his, or too new
        assert sid not in holds
    # Once per project: a later run marks nothing, even a session that aged.
    assert (tmp_path / sh.BACKFILL_MARKER).exists()
    assert sh.backfill_reviewed([_row("late", old, "review")], tmp_path) == 0
    assert "late" not in json.loads((tmp_path / sh.FILENAME).read_text(encoding="utf-8"))


def test_a_backfilled_done_ends_on_the_next_message(tmp_path):
    from app import session_holds as sh
    old = sh.BACKFILL_CUTOFF_TS - 86400
    sh.backfill_reviewed([_row("s", old, "review")], tmp_path)
    entry = sh.load(tmp_path)["s"]
    assert sh.state_for(entry, old) == "done"
    assert sh.state_for(entry, old + 60) is None


# ── What the sidebar draws ──

HARNESS = r"""
const fs = require('fs'), vm = require('vm');
const ctx = { window: {}, console, JSON, String, Map, Set, RegExp, Array, Object, Promise };
ctx.window = ctx; vm.createContext(ctx);
vm.runInContext(fs.readFileSync(process.argv[2], 'utf8'), ctx);
const SV = ctx.SessionVerbCore;
const st = {}, rows = {};
ctx.getSessionStatus = id => st[id] || 'sleeping';
ctx.SessionStore = { get: id => rows[id] || null };
ctx._sessionSubstatus = {}; ctx._sessionRetryState = {};
const set = (id, s, verb, hold) => { st[id] = s; rows[id] = { message_count: 3, next_step: verb ? { verb } : null }; if (hold) rows[id].hold = hold; };
set('dec', 'sleeping', 'decide', 'standby'); set('rev', 'idle', 'review', 'done'); set('w', 'working', 'review', 'standby');
set('plain', 'idle', 'review'); set('q', 'sleeping', 'decide');
const out = {};
const look = id => { const p = SV.cardParts(id); return [p.label, p.cls, SV.filterBucket(id), SV.rank(id)]; };
for (const id of Object.keys(st)) out[id] = look(id);
SV.noteEntry('dec', { kind: 'asst', text: 'Here are the slides.' });   // a new message ends Standby
out.after_msg = look('dec');
SV.applyHold('plain', 'standby'); out.applied = look('plain');
SV.applyHold('plain', null); out.cleared = look('plain');
process.stdout.write(JSON.stringify(out));
"""


@pytest.mark.skipif(NODE is None, reason="node not installed")
def test_the_sidebar_draws_standby_between_needs_you_and_done(tmp_path):
    h = tmp_path / "h.js"
    h.write_text(HARNESS)
    res = subprocess.run([NODE, str(h), str(VERB_JS)], capture_output=True, text=True, timeout=30)
    assert res.returncode == 0, res.stderr
    out = json.loads(res.stdout)
    assert out["dec"][:3] == ["Standby", "vs-standby", "standby"]
    assert out["rev"][:3] == ["Done", "vs-done", "sleeping"]          # a marked Done question or review
    assert out["w"][:2] == ["Working", "vs-working"]                   # live work beats the hold
    assert out["q"][:3] == ["Needs you", "vs-needs", "question"]       # a question still needs you, asleep
    # A new message ends Standby; asleep with no verdict it waits for him
    # again (Done is his verdict, never sleep alone: owner, 2026-10-28).
    assert out["after_msg"][:2] == ["Needs you", "vs-needs"]
    assert out["applied"][:2] == ["Standby", "vs-standby"] and out["cleared"][:2] == ["Needs you", "vs-needs"]
    # "Active first": Needs you, Working, Standby, Done
    assert out["plain"][3] < out["w"][3] < out["dec"][3] < out["rev"][3]


# ── The double-click (static/js/sessions.js), with a stubbed SessionVerb ──

DBL = r"""
const src = require('fs').readFileSync(process.argv[1], 'utf8');
const fn = name => { const st = src.indexOf('function ' + name + '('); let d = 0, i = src.indexOf('{', st);
  for (;;) { const c = src[i++]; if (c === '{') d++; else if (c === '}' && --d === 0) return src.slice(st, i); } };
global.window = global;
const log = [], holds = { s1: process.argv[3] === 'none' ? null : process.argv[3] };
const groupOf = { needs: 'needs', standby: 'standby', done: 'done' };
global.SessionVerb = { setHold: (id, s) => { holds[id] = s; log.push('hold:' + s); }, holdOf: id => holds[id] || null,
  shown: d => d, display: id => holds[id] || process.argv[4] };
global.getSessionStatus = () => process.argv[2];
global.markUserStopped = global.clearUserStopped = () => {};
global.socket = { emit: (ev, d) => log.push('emit:' + ev) };
global.guiOpenDelete = global.guiOpenAdd = () => {};
global.runningIds = new Set(['s1']); global.sessionKinds = {}; global.allSessions = [{ id: 's1', display_title: 'T' }];
global.activeId = 'x'; global.filterSessions = () => {};
let undo = null;
global._showUndoToast = (m, l, f) => { log.push('toast:' + m); undo = f; };
eval(['_dblclickSleepIfIdle', '_holdUndoToast', '_sleepOrWake', '_dblclickWakeSleeping'].map(fn).join('\n'));
_dblclickSleepIfIdle('s1', { preventDefault() {}, stopPropagation() {} });
const after = log.splice(0); if (undo) undo();
process.stdout.write(JSON.stringify({ after, undo: log }));
"""

SESSIONS_JS = ROOT / "static" / "js" / "sessions.js"


def _dbl(status, hold, shown):
    res = subprocess.run([NODE, "-e", DBL, str(SESSIONS_JS), status, hold, shown],
                         capture_output=True, text=True, timeout=30)
    assert res.returncode == 0, res.stderr
    return json.loads(res.stdout)


@pytest.mark.skipif(NODE is None, reason="node not installed")
def test_double_click_on_done_goes_to_standby_without_waking():
    """Owner, 2026-10-28: "when i click a done session i think it should go right to standby"."""
    for hold in ("done", "none"):          # marked Done, or a plain sleeping Done
        r = _dbl("sleeping", hold, "done")
        assert r["after"][0] == "hold:standby"
        assert not any(e.startswith("emit:") for e in r["after"]), r["after"]   # no wake
        assert any("Standby" in e for e in r["after"] if e.startswith("toast:"))
        assert r["undo"] == ["hold:" + ("done" if hold == "done" else "null")]


@pytest.mark.skipif(NODE is None, reason="node not installed")
def test_double_click_still_steps_needs_you_to_standby_to_done():
    r = _dbl("idle", "none", "needs")
    assert r["after"][0] == "hold:standby" and "emit:close_session" in r["after"]
    r = _dbl("sleeping", "standby", "standby")
    assert r["after"][0] == "hold:done" and not any(e.startswith("emit:") for e in r["after"])
