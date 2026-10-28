"""Earlier tasks on the newest summary: the ladder grows upward (owner's pick
B, 2026-10-28, static/scratch/card-earlier.html).  Runs the real
static/js/last-card.js in headless Chrome (--dump-dom) on a chat with five
earlier summaries."""

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
CHROME = shutil.which("google-chrome") or shutil.which("chromium") or shutil.which("chromium-browser")


def _card(type_, task, needs=None):
    return {"type": type_, "title": task, "headline": "VibeNode " + task.lower(), "context": "x",
            "zoom": {"big": "Supervising ten VibeNode sessions", "task": task, "now": "Step"},
            "needs": needs or {"kind": "none", "why": "x", "next": "y"}}


DECIDE = {"kind": "decision", "ask": "Pick one", "replies": [{"key": "A", "label": "one two three", "reply": "Go with A"},
                                                             {"key": "B", "label": "four five six", "reply": "Go with B"}]}
SEQ = [("u", "make the popup premium"), ("c", _card("done", "Premium motion")),
       ("u", "default templates"), ("c", _card("needs_you", "Default templates", DECIDE)),
       ("u", "fix the notes card"), ("c", _card("done", "Fix the notes summary")),
       ("u", "cards from my seat"), ("c", _card("needs_you", "Summaries for you", DECIDE)),
       ("u", "Go with A"), ("c", _card("blocked", "Summaries for you")),
       ("u", "queue line please"), ("c", _card("done", "Queue line"))]


@pytest.fixture(scope="module")
def dom(tmp_path_factory):
    if not CHROME:
        pytest.skip("no Chrome")
    msgs = "".join(
        f'<div class="msg user">{v}</div>' if k == "u" else
        "<div class=\"msg asst\"><div class=\"msg-body\"><div class=\"vn-card-ph\" data-card='"
        + json.dumps(v).replace("'", "&#39;") + "'></div></div></div>" for k, v in SEQ)
    page = tmp_path_factory.mktemp("ladder") / "page.html"
    page.write_text(f"""<!doctype html><html><head><meta charset="utf-8"></head><body><div id="live-log">{msgs}</div>
<script>var liveSessionId='s1',sessionKinds={{s1:'idle'}};window.fetch=()=>Promise.reject(new Error('offline'));
// renderIn draws the ladder on the next animation frame.  --dump-dom does not
// always produce a frame before it dumps (the ladder was missing in about one
// dump in three), but it always runs timers inside the virtual-time budget.
window.requestAnimationFrame=cb=>setTimeout(()=>cb(performance.now()),0);</script>
<script src="{(ROOT / 'static/js/last-card.js').as_uri()}"></script>
<script>VNLastCard.renderIn(document.getElementById('live-log'));</script></body></html>""", "utf-8")
    r = subprocess.run([CHROME, "--headless=new", "--no-sandbox", "--disable-gpu", "--allow-file-access-from-files",
                        "--virtual-time-budget=3000", "--dump-dom", page.as_uri()],
                       capture_output=True, text=True, timeout=90)
    return r.stdout


def test_only_the_newest_summary_gets_the_ladder(dom):
    assert dom.count('class="lm-earlier"') == 1
    last = dom.rsplit('class="lm skim', 1)[1]
    assert 'class="lm-earlier"' in last


def test_the_last_two_tasks_show_and_the_rest_fold(dom):
    ladder = dom.split('class="lm-earlier"', 1)[1].split('class="r z1"', 1)[0]
    assert "+ 2 earlier" in ladder
    rows = re.findall(r'lm-emark (\w+)".*?<span class="t">([^<]+)</span>', ladder)
    assert rows == [("done", "Premium motion"), ("you", "Default templates"),
                    ("done", "Fix the notes summary"), ("blocked", "Summaries for you")]


def test_moving_on_is_not_answering_but_a_reply_is(dom):
    # "Default templates" was never answered (he moved on): still needs him.
    # "Summaries for you" was answered ("Go with A") and then blocked: its
    # latest summary decides, so it reads Blocked, once.
    ladder = dom.split('class="lm-earlier"', 1)[1].split('class="r z1"', 1)[0]
    assert ladder.count('<span class="t">Summaries for you</span>') == 1
    assert 'lm-emark you" title="Needs you"' in ladder
