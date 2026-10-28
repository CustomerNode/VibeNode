"""A tab running out-of-date sidebar code reloads itself while out of view
(2026-10-28: the owner double-clicked a Done session and it woke, because his
open tab still ran the double-click from before the change).  Runs the real
``static/js/live-assets.js`` in node with a fake page."""

import json
import shutil
import subprocess
from pathlib import Path

import pytest

JS = Path(__file__).resolve().parents[1] / "static" / "js" / "live-assets.js"

HARNESS = r"""
const fs = require('fs');
const listeners = {}, wl = {};
let vis = 'visible', reloads = 0, draft = process.argv[3] || '';
const scripts = { 'js/sessions.js': 100, 'js/last-card.js': 100 };
global.window = global;
global.document = {
  get visibilityState() { return vis; },
  addEventListener: (e, f) => { (listeners[e] = listeners[e] || []).push(f); },
  querySelector: sel => { const m = /src\*="([^"]+)"/.exec(sel); return m && scripts[m[1]] ? { getAttribute: () => '/static/' + m[1] + '?v=' + scripts[m[1]] } : null; },
  querySelectorAll: () => [], createElement: () => ({}), head: { appendChild: el => setTimeout(el.onload, 0) },
  getElementById: id => id === 'live-input-ta' ? { value: draft } : null,
};
global.addEventListener = (e, f) => { (wl[e] = wl[e] || []).push(f); };
global.setInterval = () => 0;
console.info = () => {};
global.location = { reload: () => { reloads++; } };
const disk = JSON.parse(process.argv[2]);
global.fetch = async url => { const p = url.replace('/static/', ''); return { ok: true, headers: { get: () => new Date((disk[p] || 0) * 1000).toUTCString() } }; };
eval(fs.readFileSync(process.argv[1], 'utf8'));
(async () => {
  const fire = () => (listeners.visibilitychange || []).forEach(f => f());
  vis = 'hidden'; fire(); const hiddenBefore = reloads;        // nothing known yet
  vis = 'visible'; await VNLiveAssets.check();
  const stale = VNLiveAssets.stale(), whileVisible = reloads;
  vis = 'hidden'; fire();
  process.stdout.write(JSON.stringify({ hiddenBefore, stale, whileVisible, after: reloads }));
})();
"""


def _run(disk, draft=""):
    if shutil.which("node") is None:
        pytest.skip("node not installed")
    r = subprocess.run(["node", "-e", HARNESS, str(JS), json.dumps(disk), draft],
                       capture_output=True, text=True, timeout=30)
    assert r.returncode == 0, r.stderr
    return json.loads(r.stdout)


def test_a_changed_sessions_js_reloads_the_tab_once_it_is_out_of_view():
    out = _run({"js/sessions.js": 200, "js/last-card.js": 100})
    assert out == {"hiddenBefore": 0, "stale": True, "whileVisible": 0, "after": 1}


def test_no_reload_when_nothing_changed():
    out = _run({"js/sessions.js": 100, "js/last-card.js": 100})
    assert out["stale"] is False and out["after"] == 0


def test_never_reloads_over_unsent_text():
    out = _run({"js/sessions.js": 200}, draft="half a message")
    assert out["stale"] is True and out["after"] == 0
