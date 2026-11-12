"""The mic button transcribes on the device itself when the PC has no transcriber.

iPhone dictation can cut off mid-sentence (Safari ends its recognizer at a
breath), and the PC-side transcriber (SpeechNode) is not on every computer.
``src/web/static/js/voice-local.js`` runs
Whisper inside the browser; ``voice.js`` picks it when SpeechNode is not ready:
on a phone always, on a computer only when the browser has no recognizer.

These run the real ``src/web/static/js/voice.js`` in Node with a fake microphone,
recorder, level meter and a fake on-device engine, and drive a dictation
through the real capture code: speech, a pause, the transcription, the send.
The real model is covered by ``test_voice_local_real.py`` (opt-in).
"""
import json
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
VOICE = ROOT / "src" / "web" / "static" / "js" / "voice.js"
LOCAL = ROOT / "src" / "web" / "static" / "js" / "voice-local.js"

NODE = shutil.which("node")
pytestmark = pytest.mark.skipif(NODE is None, reason="node is not installed")

HARNESS = r"""
const fs = require('fs');
const vm = require('vm');
const src = fs.readFileSync(process.argv[2], 'utf8');
const localSrc = fs.readFileSync(process.argv[5], 'utf8');
const scenario = process.argv[3];
const ua = process.argv[4] || 'Mozilla/5.0 (iPhone; CPU iPhone OS 18_7 like Mac OS X) Safari/604.1';

const made = [];
function el(tag) {
  const e = {
    tagName: tag, style: {}, value: '', _html: '', title: '', id: '', className: '', dataset: {}, children: [],
    get innerHTML() { return this._html; }, set innerHTML(v) { this._html = v; this.children = []; },
    classList: { _s: new Set(), add(...c) { c.forEach((x) => this._s.add(x)); }, remove(...c) { c.forEach((x) => this._s.delete(x)); },
      contains(c) { return this._s.has(c); },
      toggle(c, on) { if (on === undefined ? !this._s.has(c) : on) this._s.add(c); else this._s.delete(c); } },
    _l: {}, setAttribute() {}, focus() {}, setSelectionRange() {}, appendChild(c) { this.children.push(c); return c; },
    removeChild() {}, querySelector(sel) { return sel === '.sn-cap-text' ? this : null; },
    getBoundingClientRect() { return { left: 0, width: 300, height: 40, bottom: 500 }; },
    addEventListener(t, f) { (this._l[t] = this._l[t] || []).push(f); },
    dispatchEvent(ev) { (this._l[ev.type] || []).forEach((f) => f(ev)); },
    nextElementSibling: null, previousElementSibling: null, parentElement: { querySelector() { return null; } },
  };
  made.push(e);
  return e;
}
const parent = { insertBefore(n, ref) {
  if (ref) { n.nextElementSibling = ref; n.previousElementSibling = ref.previousElementSibling;
    if (ref.previousElementSibling) ref.previousElementSibling.nextElementSibling = n;
    ref.previousElementSibling = n; }
} };

// Microphone level: loud for `speakMs`, then silence.
let speakUntil = 0;
const analyser = { fftSize: 1024, getByteTimeDomainData(buf) {
  const loud = Date.now() < speakUntil;
  for (let i = 0; i < buf.length; i++) buf[i] = loud ? 128 + (i % 2 ? 40 : -40) : 128;
} };
class FakeAC { constructor() { this.state = 'running'; } resume() { return Promise.resolve(); } close() {}
  createMediaStreamSource() { return { connect() {}, disconnect() {} }; } createAnalyser() { return analyser; } }
let recorder = null;
class FakeRecorder {
  constructor(stream) { this.state = 'inactive'; this.mimeType = 'audio/mp4'; recorder = this; }
  start(ms) { this.state = 'recording'; this._t = setInterval(() => this.ondataavailable && this.ondataavailable({ data: { size: 1600, type: 'audio/mp4' } }), ms); }
  stop() { this.state = 'inactive'; clearInterval(this._t); setTimeout(() => this.onstop && this.onstop(), 0); }
}
const calls = { partial: 0, final: 0, loads: 0 };
// The real on-device engine decides canRun; its model work is replaced by a fake.
const fakeEngine = { isReady: () => true,
  load() { calls.loads++; return Promise.resolve(); },
  transcribeBlob(blob, opts) { if (opts && opts.fast) calls.partial++; else calls.final++;
    return Promise.resolve({ ok: true, text: 'hello from the device', gap: 0 }); } };

const toasts = [];
const docEl = el('div');
const ctx = {
  window: { AudioContext: FakeAC, innerHeight: 800,
    matchMedia: () => ({ matches: false }), performance: { now: () => Date.now() } },
  navigator: { userAgent: ua, maxTouchPoints: 0, mediaDevices: { getUserMedia: () => Promise.resolve({ getTracks: () => [{ stop() {} }] }) } },
  MediaRecorder: FakeRecorder, Blob, WebAssembly, performance: { now: () => Date.now() },
  document: { getElementById(id) { return id === 'sn-live-caption' ? docEl : null; }, createElement: el,
    body: { classList: { add() {}, remove() {} }, appendChild() {} } },
  Event: function (t) { this.type = t; }, requestAnimationFrame: (f) => setTimeout(f, 0),
  showToast: (m) => toasts.push(m), localStorage: { getItem() { return null; }, setItem() {} },
  fetch: () => Promise.resolve({}), setTimeout, clearTimeout, setInterval, clearInterval, console, _MOD: 'Ctrl',
};
docEl.classList.add('show');
ctx.window.window = ctx.window; ctx.window.navigator = ctx.navigator; ctx.window.localStorage = ctx.localStorage;
if (scenario === 'desktop_webspeech' || scenario === 'desktop_no_recognizer' || scenario === 'speechnode_first') {
  if (scenario !== 'desktop_no_recognizer') ctx.window.webkitSpeechRecognition = function () {};
  if (scenario === 'speechnode_first') ctx.window.SpeechNode = { isReady: () => true };
}
if (scenario === 'opted_out') { ctx.localStorage.getItem = (k) => (k === 'vnVoiceLocal' ? 'off' : null); ctx.window.webkitSpeechRecognition = function () {}; }
// A Surface used as a tablet: Windows, a coarse primary pointer, and the browser's own recognizer present.
if (scenario === 'desktop_tablet_posture') { ctx.window.matchMedia = (q) => ({ matches: q === '(pointer: coarse)' }); ctx.navigator.maxTouchPoints = 10; ctx.window.webkitSpeechRecognition = function () {}; }
ctx.window.MediaRecorder = FakeRecorder; ctx.window.WebAssembly = WebAssembly;
vm.createContext(ctx);
vm.runInContext(localSrc, ctx);                       // the real engine: canRun, noPartials
Object.assign(ctx.window.VoiceLocal, fakeEngine);     // but no download or model in a test
vm.runInContext(src, ctx);
vm.runInContext('this.__mode = _voiceMode; this.__setup = setupVoiceButton;', ctx);

if (scenario.startsWith('desktop') || scenario === 'speechnode_first' || scenario === 'opted_out' || scenario === 'phone_mode') {
  console.log(JSON.stringify({ mode: ctx.__mode() }));
  process.exit(0);
}

const ta = el('textarea'); ta.id = 'live-input-ta'; const btn = el('button'); btn.parentNode = parent;
let sent = null;
ctx.__setup(ta, btn, () => { sent = ta.value; });
const tick = (ms) => new Promise((r) => setTimeout(r, ms));
(async () => {
  speakUntil = Date.now() + 700;         // talk for 0.7 s
  btn.onclick();
  await tick(50);
  if (scenario === 'pause_keeps') {
    await tick(900);
    const pause = made.find((e) => e.className === 'voice-insert-btn');
    pause.onclick({ preventDefault() {}, stopPropagation() {} });
  }
  // The capture ends itself after SILENCE_SHORT (5 s) of quiet; wait for the send.
  for (let i = 0; i < 160 && sent === null && !(scenario === 'pause_keeps' && ta.value); i++) await tick(50);
  await tick(100);
  console.log(JSON.stringify({ mode: ctx.__mode(), sent, box: ta.value, calls, toasts }));
})();
"""


def _run(scenario, tmp_path, ua=None):
    h = tmp_path / "harness.js"
    h.write_text(HARNESS, encoding="utf-8")
    cmd = [NODE, str(h), str(VOICE), scenario, ua or "", str(LOCAL)]
    out = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
    assert out.returncode == 0, out.stderr
    return json.loads(out.stdout.strip().splitlines()[-1])


def test_a_phone_without_speechnode_transcribes_on_the_device(tmp_path):
    assert _run("phone_mode", tmp_path)["mode"] == "local"


def test_the_pc_transcriber_still_comes_first(tmp_path):
    assert _run("speechnode_first", tmp_path)["mode"] == "speechnode"


def test_a_computer_keeps_its_own_recognizer_unless_it_has_none(tmp_path):
    pc = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/145.0"
    assert _run("desktop_webspeech", tmp_path, pc)["mode"] == "webspeech"
    assert _run("desktop_no_recognizer", tmp_path, pc)["mode"] == "local"


def test_a_touch_screen_pc_used_as_a_tablet_is_not_a_phone(tmp_path):
    # A touch-screen laptop reports a coarse pointer when its keyboard is folded
    # back. That used to route it to the on-device transcriber, which takes long to
    # warm up on such hardware and then fails with "Session already started". A
    # Windows machine keeps the browser's own recognizer whatever its pointer says.
    pc = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/156.0"
    assert _run("desktop_tablet_posture", tmp_path, pc)["mode"] == "webspeech"


def test_the_person_can_opt_out(tmp_path):
    assert _run("opted_out", tmp_path)["mode"] == "webspeech"


def test_speak_pause_and_it_sends_the_on_device_transcript(tmp_path):
    r = _run("silence_sends", tmp_path)
    assert r["mode"] == "local"
    assert r["sent"] == "hello from the device", r
    assert r["calls"]["final"] == 1 and r["calls"]["partial"] == 0, "no live-caption passes on the device"
    assert r["calls"]["loads"] >= 1, "the model starts loading in the tap"


def test_the_pause_button_keeps_the_words_unsent(tmp_path):
    r = _run("pause_keeps", tmp_path)
    assert r["sent"] is None
    assert r["box"] == "hello from the device", r


def test_the_on_device_engine_is_loaded_before_the_mic_code():
    html = (ROOT / "src" / "web" / "templates" / "index.html").read_text(encoding="utf-8")
    assert html.index("js/voice-local.js") < html.index("js/voice.js")
    assert "js/voice-local.js" in (ROOT / "src" / "web" / "static" / "js" / "live-assets.js").read_text(encoding="utf-8")


def test_the_engine_pins_its_library_and_model():
    src = LOCAL.read_text(encoding="utf-8")
    assert "@huggingface/transformers@3." in src, "an unpinned CDN tag would change what the phone runs"
    assert "noPartials: true" in src
