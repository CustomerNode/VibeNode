"""A summary's buttons answer the session that wrote it, never another.

Every button on a summary (Deal with it, Approve, Decide's Choose, Back to
it, Do what it suggested, the reply rows) goes through ``_send`` in
``src/web/static/js/last-card.js``.  Sending to whatever session the tab has
open (``liveSessionId``) would let a tap land in a different session.

Each summary is stamped with the id of the session whose thread it was
drawn in (``data-sid``, from its ``.msg``; ``live-panel.js`` stamps the
message), and ``_send`` refuses unless that id is the open session (or the
same session under a renamed id).  A summary drawn anywhere else (the full
history view, the project finder's chat) has no session and sends nothing.

The real file runs in headless Chromium through Playwright (skips without it).
"""
import json
from pathlib import Path

import pytest

from tests._browser import shared_browser_scope

ROOT = Path(__file__).resolve().parents[1]
LAST_CARD = ROOT / "src" / "web" / "static" / "js" / "last-card.js"

NONE = {"kind": "none", "why": "Everything asked for is in.", "next": "Nothing runs by itself."}
FINDING = {"what": "Auto mode approves the Windows ways of deleting whole folders",
           "risk": "A session could wipe a folder", "recommend": "Refuse them"}
DEAL = "Deal with what you found: " + FINDING["what"]


def _done(**extra):
    return dict({"type": "done", "title": "T", "headline": "H", "context": "x", "status": {"done": ["a"]},
                 "verified": [{"check": "c", "result": "r", "ok": True}], "needs": NONE}, **extra)


APPROVE = {"type": "needs_you", "title": "T", "headline": "H", "context": "x",
          "needs": {"kind": "approve", "ask": "Pick one", "why": "y",
                    "replies": [{"key": "A", "label": "one two three", "reply": "pick one"},
                                {"key": "B", "label": "four five six", "reply": "pick two"}]}}


@pytest.fixture(scope="module")
def browser():
    with shared_browser_scope() as b:
        yield b


def _msg(card, sid=None):
    attr = f' data-sid="{sid}"' if sid else ""
    return ("<div class=\"msg assistant\"" + attr + "><div class=\"msg-body\"><div class=\"vn-card-ph\" data-card='"
            + json.dumps(card).replace("'", "&#39;") + "'></div></div></div>")


def _open(browser, tmp_path, body, live="s1"):
    html = f"""<!doctype html><html><head><meta charset="utf-8"></head><body>{body}
<script>var liveSessionId={json.dumps(live)}, sessionKinds={{s1:'idle', s2:'idle', s1b:'idle'}};
window.__sent=[]; window.__toasts=[];
window._liveSubmitDirect=(sid,text)=>{{ window.__sent.push([sid,text]); }};
window.showToast=(t)=>{{ window.__toasts.push(t); }};
window.fetch=()=>Promise.reject(new Error('offline'));</script>
<script src="{LAST_CARD.as_uri()}"></script>
<script>document.querySelectorAll('.log').forEach(l => VNLastCard.renderIn(l));</script></body></html>"""
    f = tmp_path / "card.html"
    f.write_text(html, encoding="utf-8")
    page = browser.new_page()
    errors = []
    page.on("pageerror", lambda e: errors.append(str(e)))
    page.goto(f.as_uri(), wait_until="load")
    assert not errors, errors
    page.wait_for_function("document.querySelectorAll('.lm.skim').length > 0")
    page.wait_for_timeout(50)   # _syncDone runs on the next animation frame
    return page


def _live(*msgs):
    return '<div id="live-log" class="log">' + "".join(msgs) + "</div>"


def test_deal_with_it_goes_to_the_session_that_wrote_the_summary(browser, tmp_path):
    page = _open(browser, tmp_path, _live(_msg(_done(finding=FINDING), "s1")))
    try:
        assert page.evaluate("document.querySelector('.lm').dataset.sid") == "s1"
        page.locator(".lm-find .go").click()
        assert page.evaluate("window.__sent") == [["s1", DEAL]]
    finally:
        page.close()


def test_deal_with_it_never_lands_in_another_session(browser, tmp_path):
    # The tab moved to session s2 while s1's summary was still on screen
    # (a popup over the chat, a jump the app made by itself).
    page = _open(browser, tmp_path, _live(_msg(_done(finding=FINDING), "s1")))
    try:
        page.evaluate("liveSessionId = 's2'")
        page.locator(".lm-find .go").click()
        assert page.evaluate("window.__sent") == []
        assert page.evaluate("document.querySelector('.lm-find .go').disabled") is False
        assert any("another session" in t for t in page.evaluate("window.__toasts"))
    finally:
        page.close()


def test_every_summary_button_refuses_another_session(browser, tmp_path):
    card = _done(finding=FINDING, offer="Do the thing",
                 back_to={"label": "the old work", "status": "half done", "reply": "Back to the old work"})
    page = _open(browser, tmp_path, _live(_msg(card, "s1")))
    try:
        page.evaluate("liveSessionId = 's2'")
        clicked = page.evaluate("""() => { let n = 0;
            document.querySelectorAll('.lm-backto .go, [data-verdict=offer]').forEach(b => { b.click(); n++; });
            return n; }""")
        assert clicked >= 1
        assert page.evaluate("window.__sent") == []
    finally:
        page.close()


def test_a_reply_row_refuses_another_session(browser, tmp_path):
    page = _open(browser, tmp_path, _live(_msg(APPROVE, "s1")))
    try:
        page.evaluate("liveSessionId = 's2'")
        n = page.evaluate("() => { const r = document.querySelector('.lm-row.reply'); if (r) r.click(); return !!r; }")
        assert n, "the decision draws reply rows"
        assert page.evaluate("window.__sent") == []
    finally:
        page.close()


def test_a_summary_outside_the_live_chat_sends_nothing(browser, tmp_path):
    # The full-history view draws summaries too; one there belongs to no open chat.
    body = '<div id="convo" class="log">' + _msg(_done(finding=FINDING)) + "</div>" + _live()
    page = _open(browser, tmp_path, body, live="s2")
    try:
        assert page.evaluate("() => { const b = document.querySelector('#convo .lm-find .go'); if (b) b.click(); return !!b; }")
        assert page.evaluate("window.__sent") == []
    finally:
        page.close()


def test_a_renamed_session_is_still_the_same_session(browser, tmp_path):
    page = _open(browser, tmp_path, _live(_msg(_done(finding=FINDING), "s1")))
    try:
        page.evaluate("window._idRemaps = {s1: 's1b'}; liveSessionId = 's1b'")
        page.locator(".lm-find .go").click()
        assert page.evaluate("window.__sent") == [["s1b", DEAL]]
    finally:
        page.close()


def test_a_message_from_an_old_thread_without_a_stamp_takes_the_open_session(browser, tmp_path):
    # A tab still running the old chat code draws messages without data-sid;
    # a summary in the live chat is the open session's at the moment it is drawn.
    page = _open(browser, tmp_path, _live(_msg(_done(finding=FINDING))))
    try:
        assert page.evaluate("document.querySelector('.lm').dataset.sid") == "s1"
        page.evaluate("liveSessionId = 's2'")
        page.locator(".lm-find .go").click()
        assert page.evaluate("window.__sent") == []
    finally:
        page.close()


def test_the_chat_stamps_each_message_with_its_session():
    js = (ROOT / "src" / "web" / "static" / "js" / "live-panel.js").read_text(encoding="utf-8")
    seg = js[js.index("div.className = 'msg ' + role;"):][:400]
    assert "div.dataset.sid" in seg


def test_a_verdict_chip_never_marks_another_session(browser, tmp_path):
    page = _open(browser, tmp_path, _live(_msg(_done(), "s1")))
    try:
        page.evaluate("window.__acc=[]; window.SessionVerdict={accept:(s)=>window.__acc.push(s), standby:(s)=>window.__acc.push('sb:'+s)}")
        page.evaluate("liveSessionId = 's2'")
        n = page.evaluate("""() => { const b = [...document.querySelectorAll('[data-verdict=done], [data-verdict=standby]')];
            b.forEach(x => x.click()); return b.length; }""")
        assert n >= 1
        assert page.evaluate("window.__acc") == []
        page.evaluate("liveSessionId = 's1'; document.querySelector('[data-verdict=done], [data-verdict=standby]').click()")
        assert page.evaluate("window.__acc")[0] in ("s1", "sb:s1")
    finally:
        page.close()
