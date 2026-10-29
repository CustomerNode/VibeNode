"""Standby is only shown once it is saved (2026-10-28).

The owner: "i had put a session in standby, refreshed the page, and it showed
as done".  ``SessionVerb.setHold`` (``static/js/session-verb.js``) drew the
choice at once and sent the save without looking at the answer.  When the save
failed (a web server still running code without ``/api/session-hold``, a
dropped connection, a phone between networks) the sidebar said Standby while
nothing was stored, and the next reload drew the sleeping session as Done.

Now a failed save is retried once; if that fails too, the screen goes back to
what is saved and a toast says the choice was not kept.  Run in node with the
real file and a scripted ``fetch``.
"""

import json
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
VERB_JS = ROOT / "static" / "js" / "session-verb.js"
NODE = shutil.which("node")

HARNESS = r"""
const fs = require('fs'), vm = require('vm');
const answers = JSON.parse(process.argv[2]);   // per call: true = 200, false = 404, 'throw' = network error
const calls = [], toasts = [];
const ctx = { console, JSON, String, Map, Set, RegExp, Array, Object, Promise, Date, Math, Number,
  setTimeout: (fn) => setImmediate(fn) };
ctx.window = ctx; vm.createContext(ctx);
ctx.localStorage = { getItem: k => k === 'activeProject' ? 'proj' : null, setItem() {} };
ctx.fetch = (url, opts) => {
  const a = answers.length ? answers.shift() : true;
  calls.push([url, JSON.parse(opts.body)]);
  if (a === 'throw') return Promise.reject(new Error('offline'));
  return Promise.resolve({ ok: a === true, status: a === true ? 200 : 404 });
};
ctx.showToast = m => toasts.push(m);
vm.runInContext(fs.readFileSync(process.argv[1], 'utf8'), ctx);
const SV = ctx.SessionVerbCore || ctx.SessionVerb;
const rows = { s1: { message_count: 3, next_step: { verb: 'review' } } };
ctx.getSessionStatus = () => 'sleeping';
ctx.SessionStore = { get: id => rows[id] || null };
ctx._sessionSubstatus = {}; ctx._sessionRetryState = {};
(async () => {
  const first = process.argv[3];
  const p = SV.setHold('s1', first);
  const shownAtOnce = SV.holdOf('s1');
  let newer = null;
  if (process.argv[4]) { newer = SV.setHold('s1', process.argv[4]); }
  const ok = await p;
  if (newer) await newer;
  process.stdout.write(JSON.stringify({ ok, shownAtOnce, after: SV.holdOf('s1'),
    display: SV.display('s1'), calls, toasts }));
})();
"""


def _run(answers, state="standby", newer=""):
    res = subprocess.run([NODE, "-e", HARNESS, str(VERB_JS), json.dumps(answers), state, newer],
                         capture_output=True, text=True, encoding="utf-8", timeout=30)
    assert res.returncode == 0, res.stderr
    return json.loads(res.stdout)


pytestmark = pytest.mark.skipif(NODE is None, reason="node not installed")


def test_a_saved_standby_stays_standby():
    r = _run([True])
    assert r["ok"] is True and r["shownAtOnce"] == "standby" and r["after"] == "standby"
    assert r["calls"] == [["/api/session-hold/s1", {"state": "standby", "project": "proj"}]]
    assert r["toasts"] == []


def test_one_failed_save_is_retried_and_kept():
    r = _run([False, True])
    assert r["ok"] is True and r["after"] == "standby" and len(r["calls"]) == 2
    assert r["toasts"] == []


@pytest.mark.parametrize("fail", [False, "throw"])
def test_a_standby_the_server_never_kept_is_not_shown(fail):
    """The reported failure: the screen said Standby, a reload said Done."""
    r = _run([fail, fail])
    assert r["shownAtOnce"] == "standby"          # drawn at once, as before
    assert r["ok"] is False and r["after"] is None
    assert r["display"] == "review"               # what a reload shows, shown now (no verdict: Needs you)
    assert r["toasts"] == ["Couldn’t save Standby. Try again."]


def test_a_failed_save_never_undoes_a_newer_choice():
    r = _run([False, False, True], state="standby", newer="done")
    assert r["after"] == "done" and r["toasts"] == []
