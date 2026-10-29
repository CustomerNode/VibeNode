""""Redo as…" on the end-of-turn card: send the reply back as another kind.

The owner, 2026-10-29: a button on the card to say it is the wrong kind, a
popup to pick the kind he wanted, the session sent back to redo its reply,
and every send-back saved "from the user previous message to the response
data so we can later go improve that".  He picked an outlined "Redo as…"
button where the time was (the time is gone) with a drawing of each kind
(static/scratch/card-wrong-kind-b.html, option C), then: "you are missing
card types, lets make sure thats impossible".

So the kinds live in ONE list per side, and these tests hold them together:

- ``CARD_KINDS`` (daemon/handoff_rules.py) has a kind for every card type and
  every needs.kind the rules accept;
- ``REDO_KINDS`` (static/js/last-card.js) is the same list word for word, and
  every kind has its own drawing;
- the message the button sends reads back as the kind he picked
  (``redo_wanted``), and the end-of-turn check sends a reply of any other
  kind back (``handoff_evidence.redo_kind_problems``);
- ``app/card_feedback.py`` keeps each send-back and joins the redone card to it.

Structure runs the real file in node; the clicks run the real file in
headless Chromium through Playwright (skips without it).
"""
import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

from daemon import handoff_evidence, handoff_rules
from tests._browser import shared_browser_scope

ROOT = Path(__file__).resolve().parents[1]
LAST_CARD = ROOT / "static" / "js" / "last-card.js"

NODE = r"""
global.window = global;
global.location = { host: '127.0.0.1:5050' };
global.document = { addEventListener() {}, querySelectorAll() { return []; } };
eval(require('fs').readFileSync(process.argv[1], 'utf8'));
const req = JSON.parse(process.argv[2]);
const out = { kinds: VNLastCard.redoKinds(), messages: {}, html: VNLastCard.render(req.card, { when: '2:31 PM' }) };
for (const [name, c] of Object.entries(req.cards))
  for (const k of out.kinds.map(x => x.k)) {
    out.messages[name + '>' + k] = VNLastCard.redoMessage(c, k, '');
    out.messages[name + '>' + k + '+note'] = VNLastCard.redoMessage(c, k, 'I asked you to fix it, not explain it');
  }
process.stdout.write(JSON.stringify(out));
"""

NONE = {"kind": "none", "why": "Everything asked for is in.", "next": "Nothing runs by itself."}


def _card(t, kind="none", **extra):
    needs = dict(NONE, kind=kind) if kind == "none" else {"kind": kind, "ask": "Pick one", "why": "y",
                                                           "replies": [{"key": "A", "label": "one", "reply": "a"},
                                                                       {"key": "B", "label": "two", "reply": "b"}]}
    return dict({"type": t, "title": "T", "headline": "H", "context": "x", "needs": needs}, **extra)


ONE_OF_EACH = {row[0]: _card(row[1], row[2] or "none") for row in handoff_rules.CARD_KINDS}


@pytest.fixture(scope="module")
def js():
    if shutil.which("node") is None:
        pytest.skip("node not installed")
    req = {"card": _card("done"), "cards": ONE_OF_EACH}
    r = subprocess.run(["node", "-e", NODE, str(LAST_CARD), json.dumps(req)],
                       capture_output=True, text=True, encoding="utf-8", timeout=30, check=True)
    return json.loads(r.stdout)


# ── No kind can go missing ────────────────────────────────────────────────

def test_every_card_type_and_every_needs_kind_has_a_redo_kind():
    have = {(t, n) for _k, t, n, _nm, _d in handoff_rules.CARD_KINDS}
    for t in handoff_rules.TYPES:
        if t == "needs_you":
            for n in handoff_rules.NEEDS_KINDS:
                if n != "none":
                    assert (t, n) in have, f"no Redo kind for a needs_you card with needs.kind {n!r}"
        else:
            assert (t, None) in have, f"no Redo kind for a {t!r} card"
    # and nothing that is not a real card
    for _k, t, n, _nm, _d in handoff_rules.CARD_KINDS:
        assert t in handoff_rules.TYPES and (n is None or n in handoff_rules.NEEDS_KINDS)


def test_keys_and_names_are_unique():
    keys = [r[0] for r in handoff_rules.CARD_KINDS]
    names = [r[3].lower() for r in handoff_rules.CARD_KINDS]
    assert len(set(keys)) == len(keys) and len(set(names)) == len(names)


def test_the_popups_list_is_the_rules_list_word_for_word(js):
    got = [(x["k"], x["type"], x["needs"], x["name"], x["ds"]) for x in js["kinds"]]
    assert got == [tuple(r) for r in handoff_rules.CARD_KINDS]


def test_every_kind_has_its_own_drawing():
    src = LAST_CARD.read_text(encoding="utf-8")
    block = src[src.index("const REDO_SKETCH = {"):]
    block = block[:block.index("\n  };")]
    drawn = set(re.findall(r"^\s{4}(\w+): _SKF", block, re.M))
    assert drawn == {r[0] for r in handoff_rules.CARD_KINDS}


def test_a_card_type_the_list_lacks_still_gets_a_tile():
    src = LAST_CARD.read_text(encoding="utf-8")
    fn = src[src.index("function _redoKinds()"):]
    fn = fn[:fn.index("\n  }\n")]
    assert "Object.keys(TYPES)" in fn


# ── The message, both ways ────────────────────────────────────────────────

def test_the_message_reads_back_as_the_kind_he_picked(js):
    assert len(js["messages"]) == 2 * len(ONE_OF_EACH) * len(handoff_rules.CARD_KINDS)
    for key, msg in js["messages"].items():
        want = key.split(">")[1].replace("+note", "")
        assert msg.startswith(handoff_rules.REDO_PREFIX), msg
        assert handoff_rules.redo_wanted(msg) == want, (key, msg)


def test_the_message_names_the_card_he_got_and_carries_his_note(js):
    m = js["messages"]["explain>done+note"]
    assert m == ("Wrong kind of card: you ended on an Answer card. I wanted a Done card: do the work, then show "
                 "me what changed. Redo your last reply that way.\nWhat was off: I asked you to fix it, not explain it")
    assert "a To do card" in js["messages"]["done>action"]


def test_other_messages_are_not_a_redo():
    for t in ("", None, "I wanted a Decide card", "wrong kind of card: I wanted a Decide card",
              "Wrong kind of card: I wanted a Banana card. Redo your last reply that way.",
              "Please fix the jump button"):
        assert handoff_rules.redo_wanted(t) is None


def test_card_is_kind():
    assert handoff_rules.card_is_kind(_card("needs_you", "approve"), "approve")
    assert not handoff_rules.card_is_kind(_card("needs_you", "decision"), "approve")
    assert handoff_rules.card_is_kind(_card("blocked", "action"), "blocked")
    assert not handoff_rules.card_is_kind(_card("done"), "nope")


# ── The end-of-turn check holds the redo to the kind ──────────────────────

def _facts(msg, **kw):
    return handoff_evidence.TurnFacts(last_user=msg, **kw)


def _redo_msg(want, js):
    return js["messages"]["done>" + want]


def test_a_reply_of_another_kind_is_sent_back(js):
    p = handoff_evidence.redo_kind_problems(_card("done"), _facts(_redo_msg("decision", js)))
    assert len(p) == 1 and "Redo as Decide" in p[0] and 'type "needs_you" with needs.kind "decision"' in p[0]
    # and it is part of the full check
    assert any("Redo as Decide" in x for x in handoff_evidence.problems(_card("done"), _facts(_redo_msg("decision", js))))


def test_the_kind_he_picked_passes(js):
    for row in handoff_rules.CARD_KINDS:
        assert handoff_evidence.redo_kind_problems(ONE_OF_EACH[row[0]], _facts(_redo_msg(row[0], js))) == []


def test_a_decide_is_not_an_approve(js):
    assert handoff_evidence.redo_kind_problems(_card("needs_you", "decision"), _facts(_redo_msg("approve", js)))


def test_honest_exceptions_work_still_running_and_blocked(js):
    done = _facts(_redo_msg("done", js), background_live=True)
    assert handoff_evidence.redo_kind_problems(_card("working"), done) == []
    # nothing running: a Working card is not the Done he asked for
    assert handoff_evidence.redo_kind_problems(_card("working"), _facts(_redo_msg("done", js), background_live=False))
    assert handoff_evidence.redo_kind_problems(_card("blocked"), _facts(_redo_msg("done", js))) == []
    # Blocked is no way out of a Decide
    assert handoff_evidence.redo_kind_problems(_card("blocked"), _facts(_redo_msg("decision", js)))


def test_no_redo_message_no_check():
    assert handoff_evidence.redo_kind_problems(_card("done"), _facts("Let me filter the sidebar by status")) == []
    assert handoff_evidence.redo_kind_problems(_card("done"), None) == []


def test_the_instructions_tell_the_session_what_the_message_means():
    assert '"Wrong kind of card:"' in handoff_rules.INSTRUCTIONS
    assert handoff_rules.REDO_PREFIX == "Wrong kind of card:"


# ── The card: no time, and the button where it was ───────────────────────

def test_the_time_is_gone_and_the_button_sits_in_its_place(js):
    h = js["html"]
    assert "lm-when" not in h and "2:31 PM" not in h
    row = h[h.index('<div class="lm-pillrow">'):]
    row = row[:row.index("</div>")]
    assert 'class="lm-redo" hidden' in row and "Redo as…" in row


# ── Saved send-backs ──────────────────────────────────────────────────────

@pytest.fixture
def store(tmp_path, monkeypatch):
    from app import card_feedback
    monkeypatch.setattr(card_feedback, "PATH", tmp_path / "card_feedback.jsonl")
    return card_feedback


def test_a_send_back_and_its_redo_become_one_record(store):
    sent = {"id": "rd-abc123", "session": "s1", "project": "p", "your_message": "Let me filter the sidebar",
            "card": _card("done"), "card_kind": "done", "wanted": "decision", "note": "show options first",
            "message": "Wrong kind of card: ..."}
    store.record_sent(sent)
    store.record_redo("rd-abc123", {"redo_card": _card("needs_you", "decision"), "redo_kind": "decision"})
    recs = store.records()
    assert len(recs) == 1
    r = recs[0]
    assert r["your_message"] == "Let me filter the sidebar" and r["card"]["type"] == "done"
    assert r["wanted"] == "decision" and r["note"] == "show options first"
    assert r["redo_card"]["needs"]["kind"] == "decision" and r["redo_kind"] == "decision"


def test_a_send_back_without_its_redo_yet_is_kept(store):
    store.record_sent({"id": "rd-1111", "wanted": "done", "card": _card("explain")})
    assert store.records()[0]["redo_card"] is None


def test_bad_bodies_are_refused(store):
    for body in (None, [], {"wanted": "done"}, {"id": "../etc", "wanted": "done"}, {"id": "rd-1111"}):
        with pytest.raises(ValueError):
            store.record_sent(body)
    with pytest.raises(ValueError):
        store.record_redo("rd-1111", {"redo_card": "not a card"})
    assert store.records() == []


def test_a_huge_card_is_trimmed_not_lost(store):
    big = _card("deliverable", deliverable={"title": "x", "body": "y" * 600_000})
    store.record_sent({"id": "rd-2222", "wanted": "done", "card": big})
    r = store.records()[0]
    assert r["card"]["deliverable"] == "(trimmed: too long to keep)" and r["card"]["type"] == "deliverable"


def test_the_summary_a_session_starts_from_when_asked_to_improve(store):
    store.record_sent({"id": "rd-a001", "wanted": "decision", "card_kind": "done", "note": "show options first",
                       "your_message": "Let me filter the sidebar"})
    store.record_sent({"id": "rd-a002", "wanted": "decision", "card_kind": "done"})
    store.record_sent({"id": "rd-a003", "wanted": "done", "card_kind": "explain"})
    store.record_redo("rd-a001", {"redo_card": _card("needs_you", "decision"), "redo_kind": "decision"})
    store.record_redo("rd-a003", {"redo_card": _card("explain"), "redo_kind": "explain"})
    s = store.summary()
    assert s["send_backs"] == 3
    assert list(s["by_kind"].items()) == [("done -> decision", 2), ("explain -> done", 1)]
    assert s["redone"] == 2 and s["redone_as_asked"] == 1
    assert [n["note"] for n in s["notes"]] == ["show options first"]


def test_claude_md_says_where_the_data_is_and_how_to_use_it():
    text = (ROOT / "CLAUDE.md").read_text(encoding="utf-8")
    assert "data/card_feedback.jsonl" in text and "python -m app.card_feedback" in text


def test_the_routes(store):
    from app import create_app
    client = create_app(testing=True).test_client()
    assert client.post("/api/card-feedback", json={"id": "rd-3333", "wanted": "approve", "card": _card("done")}).status_code == 200
    assert client.post("/api/card-feedback/rd-3333/redo", json={"redo_card": _card("needs_you", "approve")}).status_code == 200
    assert client.post("/api/card-feedback", json={"nope": 1}).status_code == 400
    got = client.get("/api/card-feedback").get_json()
    assert [r["id"] for r in got] == ["rd-3333"] and got[0]["redo_card"]["needs"]["kind"] == "approve"


def test_the_file_is_never_published():
    r = subprocess.run(["git", "check-ignore", "-q", "data/card_feedback.jsonl"], cwd=ROOT)
    assert r.returncode == 0


# ── The clicks, in a real browser ─────────────────────────────────────────

@pytest.fixture(scope="module")
def browser():
    with shared_browser_scope() as b:
        yield b


def _page_html(items, fetch_ok=True):
    parts = []
    for it in items:
        if isinstance(it, str):
            parts.append('<div class="msg user"><div class="msg-body">' + it + '</div></div>')
        else:
            parts.append("<div class=\"msg asst\"><div class=\"msg-body\"><div class=\"vn-card-ph\" data-card='"
                         + json.dumps(it).replace("'", "&#39;") + "'></div></div></div>")
    fetch = ("window.fetch=(u,o)=>{ window.__posts.push([u, JSON.parse(o.body)]); return Promise.resolve({ok:true,status:200}); };"
             if fetch_ok else "window.fetch=(u,o)=>{ window.__posts.push([u, JSON.parse(o.body)]); return Promise.resolve({ok:false,status:404}); };")
    return f"""<!doctype html><html><head><meta charset="utf-8"></head><body><div id="live-log">{''.join(parts)}</div>
<script>try {{ localStorage.clear(); }} catch (e) {{}}
var liveSessionId='s1', sessionKinds={{s1:'idle'}}; window.__sent=[]; window.__posts=[];
window._liveSubmitDirect=(sid,text)=>{{ window.__sent.push([sid,text]); }};
{fetch}</script>
<script src="{LAST_CARD.as_uri()}"></script>
<script>VNLastCard.renderIn(document.getElementById('live-log'));</script></body></html>"""


def _open(browser, items, tmp_path, **kw):
    f = tmp_path / "card.html"
    f.write_text(_page_html(items, **kw), encoding="utf-8")
    page = browser.new_page()
    errors = []
    page.on("pageerror", lambda e: errors.append(str(e)))
    page.goto(f.as_uri(), wait_until="load")
    assert not errors, errors
    n = sum(1 for it in items if not isinstance(it, str))
    page.wait_for_function("document.querySelectorAll('.lm.skim').length === " + str(n))
    page.wait_for_timeout(60)   # _syncDone runs on the next animation frame
    return page


def test_only_the_newest_card_offers_it(browser, tmp_path):
    page = _open(browser, ["first", _card("done"), "second", _card("explain", answer="A.")], tmp_path)
    try:
        assert page.evaluate("[...document.querySelectorAll('.lm-redo')].map(b => b.hidden)") == [True, False]
    finally:
        page.close()


def test_the_popup_lists_every_kind_and_sends_his_pick(browser, tmp_path):
    page = _open(browser, ["Let me filter the sidebar by status", _card("done")], tmp_path)
    try:
        page.locator(".lm-redo").click()
        tiles = page.locator(".lm-rd .rk")
        assert tiles.count() == len(handoff_rules.CARD_KINDS)
        names = page.evaluate("[...document.querySelectorAll('.lm-rd .rk .nm')].map(x => x.textContent)")
        assert names == [r[3] for r in handoff_rules.CARD_KINDS]
        assert page.evaluate("[...document.querySelectorAll('.lm-rd .rk svg')].length") == len(handoff_rules.CARD_KINDS)
        # the card's own kind can't be picked
        assert page.locator('.lm-rd .rk[data-kind="done"]').get_attribute("aria-disabled") == "true"
        assert page.locator(".lm-rd .go").is_disabled()
        page.locator('.lm-rd .rk[data-kind="decision"]').click()
        assert page.locator(".lm-rd .go").inner_text() == "Redo as Decide"
        page.locator(".lm-rd-note").fill("show me options before you build")
        page.locator(".lm-rd .go").click()
        sent = page.evaluate("window.__sent")
        assert len(sent) == 1 and sent[0][0] == "s1"
        assert handoff_rules.redo_wanted(sent[0][1]) == "decision"
        assert sent[0][1].endswith("\nWhat was off: show me options before you build")
        page.wait_for_function("window.__posts.length >= 1")
        url, body = page.evaluate("window.__posts[0]")
        assert url == "/api/card-feedback"
        assert body["your_message"] == "Let me filter the sidebar by status"
        assert body["card"]["type"] == "done" and body["card_kind"] == "done" and body["wanted"] == "decision"
        assert body["note"] == "show me options before you build" and body["session"] == "s1"
        # the corner now says what he asked for, and the popup is gone
        assert page.locator(".lm-redo").is_hidden()
        assert page.locator(".lm-pillrow .lm-redone").inner_text().strip() == "Redoing as Decide"
        page.wait_for_timeout(250)
        assert page.locator(".lm-rd").count() == 0
    finally:
        page.close()


def test_escape_and_cancel_send_nothing(browser, tmp_path):
    page = _open(browser, ["x y z", _card("explain", answer="A.")], tmp_path)
    try:
        page.locator(".lm-redo").click()
        page.keyboard.press("Escape")
        page.wait_for_timeout(250)
        assert page.locator(".lm-rd").count() == 0
        page.locator(".lm-redo").click()
        page.locator(".lm-rd .cx").click()
        page.wait_for_timeout(250)
        assert page.locator(".lm-rd").count() == 0 and page.evaluate("window.__sent") == []
    finally:
        page.close()


def test_after_a_reload_the_corner_still_says_redoing(browser, tmp_path):
    msg = "Wrong kind of card: you ended on a Done card. I wanted an Approve card: ask me yes or no before you go on. Redo your last reply that way."
    page = _open(browser, ["Do it", _card("done"), msg], tmp_path)
    try:
        assert page.locator(".lm-redo").is_hidden()
        assert page.locator(".lm-pillrow .lm-redone").inner_text().strip() == "Redoing as Approve"
    finally:
        page.close()


def test_any_reply_of_his_after_the_card_hides_it(browser, tmp_path):
    page = _open(browser, ["Do it", _card("done"), "thanks, now do the next one"], tmp_path)
    try:
        assert page.locator(".lm-redo").is_hidden() and page.locator(".lm-redone").count() == 0
    finally:
        page.close()


def test_a_record_the_server_cant_take_yet_waits_in_the_browser(browser, tmp_path):
    page = _open(browser, ["Do it", _card("done")], tmp_path, fetch_ok=False)
    try:
        page.locator(".lm-redo").click()
        page.locator('.lm-rd .rk[data-kind="explain"]').click()
        page.locator(".lm-rd .go").click()
        page.wait_for_function("window.__posts.length >= 1")
        page.wait_for_timeout(50)
        box = page.evaluate("JSON.parse(localStorage.getItem('vnCardFeedbackOutbox') || '[]')")
        assert len(box) == 1 and box[0]["url"] == "/api/card-feedback" and box[0]["body"]["wanted"] == "explain"
    finally:
        page.close()
