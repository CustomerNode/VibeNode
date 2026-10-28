"""Double-tap Sleep sleeps a session in any state (owner, 2026-10-27).

"i am trying to sleep you but it wont let me sleep you ... make it so if i
sleep a session, i am in fucking charge."  The row/card double-tap refused a
working session ("Session is working — use Stop from the menu") and one
waiting on a question.  Now anything not already sleeping is slept, and Undo
on a session slept mid-turn wakes it.

Runs the real ``_dblclickSleepIfIdle`` / ``_dblclickWakeSleeping`` from
``static/js/sessions.js`` in node, with the page's globals stubbed.
"""

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

SESSIONS_JS = Path(__file__).resolve().parents[1] / "static" / "js" / "sessions.js"


def _function_source(src: str, name: str) -> str:
    start = src.index("function " + name + "(")
    depth, i = 0, src.index("{", start)
    while True:
        c = src[i]
        depth += c == "{"
        depth -= c == "}"
        i += 1
        if depth == 0:
            return src[start:i]


SCRIPT = r"""
const src = JSON.parse(process.argv[1]);
const status = process.argv[2];
const log = [];
global.window = global;
global.getSessionStatus = () => status;
global.markUserStopped = id => log.push('markUserStopped');
global.clearUserStopped = id => log.push('clearUserStopped');
global.socket = { emit: (ev, data) => log.push('emit:' + ev + (data.resume ? ':resume' : '')) };
global.guiOpenDelete = () => {}; global.guiOpenAdd = () => {};
global.runningIds = new Set(['s1']); global.sessionKinds = { s1: status };
global.allSessions = [{ id: 's1', display_title: 'Test' }];
global.activeId = 'other'; global.filterSessions = () => {};
global.showToast = t => log.push('toast:' + t);
let undo = null;
global._showUndoToast = (msg, label, fn) => { log.push('undoToast:' + msg); undo = fn; };
global.SessionModel = undefined; global._currentProjectDir = () => '/p';
eval(src);
_dblclickSleepIfIdle('s1', { preventDefault() {}, stopPropagation() {} });
const afterSleep = log.splice(0);
if (undo) undo();
process.stdout.write(JSON.stringify({ afterSleep, afterUndo: log }));
"""


def _run(status):
    src = SESSIONS_JS.read_text("utf-8")
    # The double-click routes through _sleepOrWake (Standby, 2026-10-28).
    code = "\n".join(_function_source(src, n) for n in
                      ("_dblclickSleepIfIdle", "_sleepOrWake", "_holdUndoToast", "_dblclickWakeSleeping"))
    out = subprocess.run(["node", "-e", SCRIPT, json.dumps(code), status],
                         capture_output=True, text=True, timeout=30, check=True)
    return json.loads(out.stdout)


needs_node = pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")


@needs_node
@pytest.mark.parametrize("status", ["idle", "working", "question"])
def test_double_tap_sleeps_any_awake_session(status):
    r = _run(status)
    assert "markUserStopped" in r["afterSleep"]
    assert "emit:close_session" in r["afterSleep"]
    assert not any(e.startswith("toast:") for e in r["afterSleep"]), r["afterSleep"]


@needs_node
@pytest.mark.parametrize("status", ["working", "question"])
def test_undo_after_sleeping_a_busy_session_wakes_it(status):
    r = _run(status)
    assert "clearUserStopped" in r["afterUndo"]
    assert "emit:start_session:resume" in r["afterUndo"]


@needs_node
def test_undo_after_sleeping_an_idle_session_restores_it_locally():
    r = _run("idle")
    assert "toast:Sleep undone" in r["afterUndo"]
    assert not any(e.startswith("emit:") for e in r["afterUndo"])


@needs_node
def test_double_tap_on_a_sleeping_session_still_wakes_it():
    r = _run("sleeping")
    assert "emit:start_session:resume" in r["afterSleep"]


def test_no_refusal_text_is_left():
    src = SESSIONS_JS.read_text("utf-8")
    body = _function_source(src, "_dblclickSleepIfIdle")
    assert not re.search(r"showToast\([^)]*use Stop", body)
