"""A sent-back message folds into the "Invalid termination response" box.

2026-10-28, the owner: the rejected card "still renders it ... it shows the
interjection nice but its confusing".  Live, socket.js appends the send-back
after thread-scroll's jump button and sentinel (and the verdict strip), so the
box's previous sibling was never the message it replaced; the box gave up and
the rejected card stayed drawn above it.  Runs the real _renderSentBackCard
from static/js/live-panel.js in headless Chrome (--dump-dom).
"""

import re
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
CHROME = shutil.which("google-chrome") or shutil.which("chromium") or shutil.which("chromium-browser")


def _render_fn() -> str:
    src = (ROOT / "static/js/live-panel.js").read_text("utf-8")
    start = src.index("const _SENT_BACK_OLD_NAMES")
    end = src.index("\nfunction renderLiveEntry")
    return src[start:end]


CASES = {
    # Live: socket.js appends after the thread's trailing chrome.
    "live": '<div class="msg assistant"><div class="msg-body">REJECTED-CARD</div></div>'
            '<div class="vs-verdict-strip"></div><div class="vn-jump-wrap"></div><div class="vn-thread-sentinel"></div>',
    # Reload: the full render appends entries back to back.
    "reload": '<div class="msg assistant"><div class="msg-body">REJECTED-CARD</div></div>',
    # Never eat a message that is not the session's own.
    "user": '<div class="msg user"><div class="msg-body">MY-WORDS</div></div><div class="vn-thread-sentinel"></div>',
}


@pytest.fixture(scope="module")
def doms(tmp_path_factory):
    if not CHROME:
        pytest.skip("no Chrome")
    out = {}
    for name, html in CASES.items():
        page = tmp_path_factory.mktemp("sb") / f"{name}.html"
        page.write_text(f"""<!doctype html><html><head><meta charset="utf-8"></head><body>
<div id="live-log">{html}</div>
<script>function escHtml(s){{return String(s).replace(/[&<>"]/g,c=>({{'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}})[c]);}}
{_render_fn()}
const d = document.createElement('div');
_renderSentBackCard(d, {{kind:'sent_back', name:'Incomplete response', desc:'Its response was missing required parts.', text:'Format: x'}});
document.getElementById('live-log').appendChild(d);</script></body></html>""", "utf-8")
        r = subprocess.run([CHROME, "--headless=new", "--no-sandbox", "--disable-gpu",
                            "--allow-file-access-from-files", "--virtual-time-budget=2000",
                            "--dump-dom", page.as_uri()], capture_output=True, text=True, timeout=90)
        out[name] = r.stdout
    return out


def _log(dom: str) -> str:
    return re.search(r'<div id="live-log">(.*?)</div>\s*<script>', dom, re.S).group(1)


@pytest.mark.parametrize("name", ["live", "reload"])
def test_the_rejected_card_moves_into_the_box(doms, name):
    log = _log(doms[name])
    assert 'class="msg assistant"' not in log, "the rejected card is still drawn in the thread"
    tried = re.search(r'<div class="vn-sb-tried">(.*?)</div>\s*<div class="vn-sb-h">It was told', log, re.S)
    assert tried and "REJECTED-CARD" in tried.group(1)


def test_a_user_message_is_never_folded(doms):
    log = _log(doms["user"])
    assert 'class="msg user"' in log and "MY-WORDS" in log
    assert '<div class="vn-sb-tried" hidden="">' in log
