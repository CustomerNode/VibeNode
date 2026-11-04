"""Browser voice input: a stop the browser makes on its own never sends.

On an iPhone, dictation could stop 10 to 20 seconds in, mid-sentence, and
send. iPhone Safari ends speech recognition by itself; the
restart in ``onend`` can be refused, and the code then fell through to the
normal finish and sent the half message. Words Safari had shown but never
finalized were dropped too.

These run the real ``src/web/static/js/voice.js`` in Node with a fake recognizer and a
fake page, and drive what Safari does.
"""
import json
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
VOICE = ROOT / "src" / "web" / "static" / "js" / "voice.js"

NODE = shutil.which("node")
pytestmark = pytest.mark.skipif(NODE is None, reason="node is not installed")

HARNESS = r"""
const fs = require('fs');
const vm = require('vm');
const src = fs.readFileSync(process.argv[2], 'utf8');
const scenario = process.argv[3];

function el(tag) {
  const e = {
    tagName: tag, style: {}, value: '', innerHTML: '', title: '', id: '',
    classList: {
      _s: new Set(),
      add(c) { this._s.add(c); }, remove(c) { this._s.delete(c); },
      contains(c) { return this._s.has(c); },
      toggle(c, on) { if (on === undefined ? !this._s.has(c) : on) this._s.add(c); else this._s.delete(c); },
    },
    _l: {}, setAttribute() {}, focus() {}, setSelectionRange() {},
    addEventListener(t, f) { (this._l[t] = this._l[t] || []).push(f); },
    dispatchEvent(ev) { (this._l[ev.type] || []).forEach((f) => f(ev)); },
    nextElementSibling: null, previousElementSibling: null,
  };
  return e;
}
const parent = { insertBefore(n, ref) {
  // keep sibling links so setupVoiceButton finds what it made
  if (ref) { n.nextElementSibling = ref; n.previousElementSibling = ref.previousElementSibling;
    if (ref.previousElementSibling) ref.previousElementSibling.nextElementSibling = n;
    ref.previousElementSibling = n; }
} };

let current = null;
let refuseRestart = false;
class FakeSR {
  constructor() { current = this; this.starts = 0; }
  start() {
    if (this.starts > 0 && refuseRestart) throw new Error('InvalidStateError');
    this.starts++; setTimeout(() => this.onstart && this.onstart(), 0);
  }
  stop() { setTimeout(() => this.onend && this.onend(), 0); }
  say(words, isFinal) {
    const r = [{ transcript: words }]; r.isFinal = isFinal;
    this.onresult({ resultIndex: 0, results: [r] });
  }
}

const toasts = [];
const logs = [];
const ctx = {
  window: { webkitSpeechRecognition: FakeSR },
  document: { getElementById() { return null; }, createElement: el },
  Event: function (t) { this.type = t; },
  showToast: (m) => toasts.push(m),
  fetch: (url, o) => { logs.push(JSON.parse(o.body)); return Promise.resolve({}); },
  setTimeout, clearTimeout, setInterval, clearInterval, console, navigator: { userAgent: 'iPhone test' }, _MOD: 'Ctrl',
  _voiceTranscriptProcessors: [], _lastSubmitWasVoice: false,
  localStorage: { getItem() { return null; }, setItem() {} },
};
ctx.window.window = ctx.window;
vm.createContext(ctx);
vm.runInContext(src, ctx);

const ta = el('textarea'); const btn = el('button'); btn.parentNode = parent;
let sent = null;
vm.runInContext('this.__setup = setupVoiceButton;', ctx);
ctx.__setup(ta, btn, () => { sent = ta.value; });

const tick = (ms) => new Promise((r) => setTimeout(r, ms || 5));
(async () => {
  btn.onclick();
  await tick();
  const sr = current;
  if (scenario === 'safari_ends_mid_sentence') {
    refuseRestart = true;
    sr.say('the header looks wrong.', true);
    sr.say('fix the login page so that', false);   // shown, never finalized
    await tick();
    sr.onend();                                      // Safari ends by itself
  } else if (scenario === 'safari_restarts') {
    sr.say('first part', true);
    sr.say('second', false);
    await tick();
    sr.onend();                                      // restart allowed
    await tick();
    sr.say('third part', true);
    await tick();
    btn.onclick();                                   // the person taps send
  } else if (scenario === 'person_taps_send') {
    sr.say('please run the tests', true);
    await tick();
    btn.onclick();
  } else if (scenario === 'no_speech_error') {
    sr.say('one finished line.', true);
    sr.say('half a thought', false);
    await tick();
    sr.onerror({ error: 'no-speech' });
    sr.onend();
  }
  await tick(20);
  console.log(JSON.stringify({ sent, box: ta.value, toasts, logs }));
})();
"""


def _run(scenario, tmp_path):
    h = tmp_path / "harness.js"
    h.write_text(HARNESS, encoding="utf-8")
    out = subprocess.run([NODE, str(h), str(VOICE), scenario],
                         capture_output=True, text=True, timeout=30)
    assert out.returncode == 0, out.stderr
    return json.loads(out.stdout.strip().splitlines()[-1])


def test_a_stop_safari_makes_mid_sentence_never_sends(tmp_path):
    r = _run("safari_ends_mid_sentence", tmp_path)
    assert r["sent"] is None, "the half message was sent"
    assert "fix the login page so that" in r["box"], "the unfinalized words were dropped"
    assert any("stopped by itself" in t for t in r["toasts"])


def test_a_restart_keeps_every_word(tmp_path):
    r = _run("safari_restarts", tmp_path)
    assert r["sent"] is not None
    for w in ("first part", "second", "third part"):
        assert w in r["sent"]


def test_the_persons_own_tap_still_sends(tmp_path):
    r = _run("person_taps_send", tmp_path)
    assert r["sent"] and "please run the tests" in r["sent"]


def test_a_no_speech_error_keeps_the_words_unsent(tmp_path):
    r = _run("no_speech_error", tmp_path)
    assert r["sent"] is None
    assert "half a thought" in r["box"]


def test_every_dictation_logs_why_it_ended(tmp_path):
    r = _run("safari_ends_mid_sentence", tmp_path)
    trail = " ".join(l["msg"] for l in r["logs"] if l["tag"] == "voice")
    assert "restart refused" in trail and "outcome: kept in box" in trail
    assert "fix the login" not in trail, "the trail must count words, never log them"
    r = _run("person_taps_send", tmp_path)
    trail = " ".join(l["msg"] for l in r["logs"] if l["tag"] == "voice")
    assert "stopped: your tap" in trail and "outcome: sent (4 words)" in trail
