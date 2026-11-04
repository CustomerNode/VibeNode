"""The four-dot More button never lands inside the voice recording buttons.

The More button places itself before the X, which used to be the mic's
neighbour; with pause between them it could land X, pause, More, send. The page
is the REAL src/web/templates/index.html with every real script; only the network and the
microphone are faked (the harness of test_enter_to_send.py).
"""
import pytest

from tests._browser import shared_browser_scope
from tests.test_enter_to_send import _open


@pytest.fixture(scope="module")
def browser():
    with shared_browser_scope() as b:
        yield b


REAL_FAKE_MIC = r"""
window.SpeechRecognition = class {
  constructor() { window._rec = this; }
  start() { this._started = true; this.onstart && this.onstart(); }
  stop() { setTimeout(() => this.onend && this.onend(), 0); }
  say(text) { this.onresult({ resultIndex: 0, results: [Object.assign([{ transcript: text }], { isFinal: true })] }); }
};
window.webkitSpeechRecognition = window.SpeechRecognition;
"""


def _bar_order(pg):
    return pg.evaluate("""() => [...document.getElementById('live-voice-btn').parentNode.children]
        .filter(e => getComputedStyle(e).display !== 'none')
        .map(e => e.id || e.className.split(' ')[0])""")


@pytest.mark.parametrize("width", [1400, 390])
def test_more_button_never_lands_inside_the_recording_buttons(browser, width):
    """The four-dot More button must not land between X, pause and send (it
    places itself before the X, which used to be the mic's neighbour)."""
    if True:
        pg = _open(browser, width, extra_init=REAL_FAKE_MIC)
        # The splash and the server-reachable overlay sit over the page; no test here is about them.
        pg.evaluate("() => { for (const id of ['vn-splash', 'health-blocker']) document.getElementById(id)?.remove(); }")
        pg.wait_for_selector("#live-image-btn", state="attached")
        # A phone-width page with no session open leaves the sidebar over the composer,
        # so press the buttons directly; order and spacing are checked below.
        pg.evaluate("document.getElementById('live-voice-btn').click()")
        pg.wait_for_selector(".voice-insert-btn", state="visible")
        pg.wait_for_timeout(100)   # let the More button's observer run after the re-render
        order = _bar_order(pg)
        i = order.index
        assert i("live-image-btn") < i("voice-listening") < i("voice-cancel-btn") \
            < i("voice-insert-btn") < i("live-voice-btn"), order
        # The three recording buttons touch: one capsule, nothing between them
        boxes = pg.evaluate("""() => ['.voice-cancel-btn', '.voice-insert-btn', '#live-voice-btn']
            .map(s => document.querySelector(s).getBoundingClientRect()).map(r => [r.left, r.right])""")
        assert abs(boxes[0][1] - boxes[1][0]) <= 2 and abs(boxes[1][1] - boxes[2][0]) <= 2, boxes
        pg.evaluate("window._rec.say('fix the header')")
        pg.evaluate("document.querySelector('.voice-insert-btn').click()")
        pg.wait_for_function("document.getElementById('live-input-ta').value === 'fix the header'")
        assert pg.evaluate("window.__sent") == 0
