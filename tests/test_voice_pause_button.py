"""The voice recorder's pause button: stop recording, keep the words in the box, don't send.

The recorder shows a pause icon in the middle and a send icon in place of the red mic,
all three red in the standard recorder. Drives the real voice.js and style.css in headless Chromium
with a fake microphone: browser speech (the standard recorder) and SpeechNode.
"""
from pathlib import Path

import pytest

sync_api = pytest.importorskip("playwright.sync_api")

ROOT = Path(__file__).resolve().parents[1]

FAKES = r"""
window._MOD = 'Ctrl';
window.showToast = function () {};
window.submits = [];
delete window.AudioContext; delete window.webkitAudioContext;
// Browser speech (the standard recorder)
class FakeRecognition {
  constructor() { window._rec = this; }
  start() { this._started = true; this.onstart && this.onstart(); }
  stop() { setTimeout(() => this.onend && this.onend(), 0); }
  say(text) { this.onresult({ resultIndex: 0, results: [Object.assign([{ transcript: text }], { isFinal: true })] }); }
}
window.SpeechRecognition = FakeRecognition;
// SpeechNode
window.useSpeechNode = false;
window.SpeechNode = {
  isReady: () => window.useSpeechNode,
  transcribeBlob: () => Promise.resolve({ ok: true, text: 'hello from speechnode' }),
};
if (!navigator.mediaDevices) Object.defineProperty(navigator, 'mediaDevices', { value: {} });
navigator.mediaDevices.getUserMedia =() => Promise.resolve({ getTracks: () => [{ stop() {} }] });
window.MediaRecorder = class {
  constructor() { this.state = 'inactive'; }
  start() { this.state = 'recording'; }
  stop() {
    this.state = 'inactive';
    this.ondataavailable && this.ondataavailable({ data: new Blob(['x']) });
    setTimeout(() => this.onstop && this.onstop(), 0);
  }
};
"""

PAGE = """<!doctype html><html data-theme="dark"><head><style>{css}</style></head><body>
<div class="live-bar-row" style="display:flex;align-items:center">
  <textarea id="live-input-ta"></textarea>
  <button id="live-voice-btn" class="live-send-btn"></button>
</div>
<script>{fakes}</script>
<script>{js}</script>
<script>
  setupVoiceButton(document.getElementById('live-input-ta'), document.getElementById('live-voice-btn'),
                   () => window.submits.push(document.getElementById('live-input-ta').value));
</script></body></html>"""


@pytest.fixture(scope="module")
def browser():
    with sync_api.sync_playwright() as p:
        try:
            b = p.chromium.launch()
        except Exception as e:  # no Chromium installed
            pytest.skip(f"Chromium unavailable: {e}")
        yield b
        b.close()


@pytest.fixture
def page(browser):
    pg = browser.new_page()
    errors = []
    pg.on("pageerror", lambda e: errors.append(str(e)))
    pg.set_content(PAGE.format(
        css=(ROOT / "src" / "web" / "static" / "style.css").read_text(encoding="utf-8"),
        fakes=FAKES,
        js=(ROOT / "src" / "web" / "static" / "js" / "voice.js").read_text(encoding="utf-8"),
    ))
    yield pg
    assert not errors, errors
    pg.close()


def _visible(pg, sel):
    return pg.locator(sel).first.is_visible()


def test_recording_shows_cancel_pause_send_and_listening(page):
    page.click("#live-voice-btn")
    assert _visible(page, ".voice-cancel-btn")
    assert _visible(page, ".voice-insert-btn")
    assert _visible(page, ".voice-listening")
    assert page.get_attribute("#live-voice-btn", "title") == "Stop & send"
    # Order left to right: Listening, X, pause, send
    order = page.evaluate("""() => [...document.querySelector('.live-bar-row').children]
        .map(e => e.className.split(' ')[0] || e.tagName)""")
    i = order.index
    assert i("voice-listening") < i("voice-cancel-btn") < i("voice-insert-btn") < i("live-send-btn")
    # The pause button is red like the X beside it
    colors = page.evaluate("""() => ['.voice-cancel-btn', '.voice-insert-btn']
        .map(s => getComputedStyle(document.querySelector(s)).color)""")
    assert colors[0] == colors[1]


def test_pause_keeps_the_words_and_sends_nothing(page):
    page.click("#live-voice-btn")
    page.evaluate("window._rec.say('fix the header please')")
    page.click(".voice-insert-btn")
    page.wait_for_function("!document.querySelector('#live-voice-btn').classList.contains('recording')")
    page.wait_for_timeout(50)
    assert page.input_value("#live-input-ta") == "fix the header please"
    assert page.evaluate("window.submits") == []
    assert page.evaluate("document.activeElement.id") == "live-input-ta"
    assert not _visible(page, ".voice-insert-btn")
    assert not _visible(page, ".voice-listening")


def test_send_still_stops_and_sends(page):
    page.click("#live-voice-btn")
    page.evaluate("window._rec.say('ship it')")
    page.click("#live-voice-btn")
    page.wait_for_function("window.submits.length === 1")
    assert page.evaluate("window.submits") == ["ship it"]


def test_cancel_still_discards(page):
    page.click("#live-voice-btn")
    page.evaluate("window._rec.say('never mind')")
    page.click(".voice-cancel-btn")
    page.wait_for_timeout(50)
    assert page.input_value("#live-input-ta") == ""
    assert page.evaluate("window.submits") == []


def test_speechnode_panel_pause_keeps_the_words_and_sends_nothing(page):
    page.evaluate("window.useSpeechNode = true")
    page.click("#live-voice-btn")
    page.wait_for_selector("#sn-live-caption.show .sn-cap-pause")
    assert _visible(page, "#sn-live-caption .sn-cap-listen")
    page.click("#sn-live-caption .sn-cap-pause")
    page.wait_for_function("document.querySelector('#live-input-ta').value === 'hello from speechnode'")
    page.wait_for_timeout(100)
    assert page.evaluate("window.submits") == []
    assert not page.evaluate("document.getElementById('sn-live-caption').classList.contains('show')")


def test_speechnode_panel_send_still_sends(page):
    page.evaluate("window.useSpeechNode = true")
    page.click("#live-voice-btn")
    page.wait_for_selector("#sn-live-caption.show .sn-cap-stop")
    page.click("#sn-live-caption .sn-cap-stop")
    page.wait_for_function("window.submits.length === 1")
    assert page.evaluate("window.submits") == ["hello from speechnode"]

