"""The red flag on the end-of-turn summary card.

Flag → tick reasons (several at once) and/or write a note → Send to fix.  The
server keeps the flag and starts a session in the VibeNode project told to
read the flagged session's transcript and patch the summary rules.

- the reasons are one list per side, held equal here;
- ``card_flags.clean`` refuses a body it cannot use and any reason it does not know;
- the fixing session's prompt names the transcript, every reason and the note;
- the route starts that session in the repo's own folder, whatever project is open;
- the popup, in a real browser: multi-select, the note alone is enough, Escape
  sends nothing, a sent flag reads "Flagged" and stays so after a reload.
"""
import json
import shutil
import subprocess
from pathlib import Path

import pytest

from tests._browser import shared_browser_scope

ROOT = Path(__file__).resolve().parents[1]
LAST_CARD = ROOT / "src" / "web" / "static" / "js" / "last-card.js"

CARD = {"type": "done", "title": "Search box", "headline": "The search box finds messages now.", "context": "x",
        "needs": {"kind": "none", "why": "All in.", "next": "Nothing runs by itself."}}

NODE = r"""
global.window = global;
global.location = { host: '127.0.0.1:5050' };
global.document = { addEventListener() {}, querySelectorAll() { return []; } };
eval(require('fs').readFileSync(process.argv[1], 'utf8'));
process.stdout.write(JSON.stringify({ reasons: VNLastCard.flagReasons, html: VNLastCard.render(JSON.parse(process.argv[2]), {}) }));
"""


@pytest.fixture(scope="module")
def js():
    if shutil.which("node") is None:
        pytest.skip("node not installed")
    r = subprocess.run(["node", "-e", NODE, str(LAST_CARD), json.dumps(CARD)],
                       capture_output=True, text=True, encoding="utf-8", timeout=30, check=True)
    return json.loads(r.stdout)


def test_the_popups_reasons_are_the_servers_word_for_word(js):
    from web import card_flags
    assert [tuple(x) for x in js["reasons"]] == list(card_flags.REASONS.items())


def test_the_flag_sits_in_the_corner_of_every_summary(js):
    h = js["html"]
    row = h[h.index('<div class="lm-pillrow">'):]
    row = row[:row.index("</div>")]
    assert 'class="lm-flag"' in row and ">Flag</button>" in row


@pytest.fixture
def flags(tmp_path, monkeypatch):
    from web import card_flags
    monkeypatch.setattr(card_flags, "PATH", tmp_path / "card_flags.jsonl")
    return card_flags


def test_bad_bodies_are_refused(flags):
    for body in (None, [], {"session": "x"}, {"session": "../../etc", "reasons": ["long"]},
                 {"session": "abcd1234", "reasons": ["made-up"]}, {"session": "abcd1234", "reasons": "long"},
                 {"session": "abcd1234", "reasons": [], "note": "  "},
                 {"session": "abcd1234", "project": "a/b", "reasons": ["long"]}):
        with pytest.raises(ValueError):
            flags.clean(body)


def test_several_reasons_or_a_note_alone(flags):
    f = flags.clean({"session": "abcd1234", "reasons": ["technical", "focus", "technical"]})
    assert f["reasons"] == ["technical", "focus"] and f["note"] == ""
    assert flags.clean({"session": "abcd1234", "note": "too chatty"})["reasons"] == []


def test_the_prompt_names_the_transcript_reasons_and_note(flags, tmp_path):
    proj = tmp_path / "projects" / "-home-someone-shop"
    proj.mkdir(parents=True)
    (proj / "abcd1234.jsonl").write_text("{}\n")
    f = flags.clean({"session": "abcd1234", "project": "-home-someone-shop", "reasons": ["technical", "focus"],
                     "note": "I only asked if it works", "card": CARD})
    t = flags.transcript_path("abcd1234", "-home-someone-shop", tmp_path / "projects")
    assert t == proj / "abcd1234.jsonl"
    # found in another project when the browser's project was wrong
    assert flags.transcript_path("abcd1234", "", tmp_path / "projects") == t
    p = flags.prompt(f, t)
    assert str(t) in p and "Unnecessary technical details" in p and "Lost focus on my task" in p
    assert "I only asked if it works" in p and "The search box finds messages now." in p
    assert "public repository" in p
    assert flags.title(f) == "Fix summary: Unnecessary technical details"


def test_the_route_keeps_the_flag_and_starts_the_fixer_in_the_repo(flags, tmp_path, monkeypatch):
    from web import create_app, config
    monkeypatch.setattr(config, "_CLAUDE_PROJECTS", tmp_path / "projects")
    names = []
    monkeypatch.setattr(config, "_save_name", lambda sid, name, project="": names.append((sid, name, project)))
    app = create_app(testing=True)
    app.session_manager.start_session.return_value = {"ok": True}
    c = app.test_client()
    r = c.post("/api/card-flag", json={"session": "abcd1234", "project": "-x", "reasons": ["long"], "card": CARD})
    d = r.get_json()
    assert r.status_code == 200 and d["ok"] and d["session"]
    kw = app.session_manager.start_session.call_args.kwargs
    assert kw["cwd"] == str(config._VIBENODE_DIR) and kw["resume"] is False and kw["session_id"] == d["session"]
    assert "Too long or wordy" in kw["prompt"]
    assert names == [(d["session"], "Fix summary: Too long or wordy", config._encode_cwd(str(config._VIBENODE_DIR)))]
    assert (tmp_path / "projects" / d["project"] / (d["session"] + ".jsonl")).is_file()
    got = c.get("/api/card-flag").get_json()
    assert len(got) == 1 and got[0]["reasons"] == ["long"] and got[0]["fixer"] == d["session"]
    assert c.post("/api/card-flag", json={"session": "abcd1234", "reasons": ["nope"]}).status_code == 400
    # a fixer that fails to start still keeps the flag, and says so
    app.session_manager.start_session.return_value = {"ok": False, "error": "engine down"}
    r = c.post("/api/card-flag", json={"session": "abcd1234", "note": "x"})
    assert r.status_code == 502 and r.get_json()["error"] == "engine down" and len(flags.records()) == 2


def test_the_file_is_never_published():
    assert subprocess.run(["git", "check-ignore", "-q", "data/card_flags.jsonl"], cwd=ROOT).returncode == 0


# ── The popup, in a real browser ──────────────────────────────────────────

@pytest.fixture(scope="module")
def browser():
    with shared_browser_scope() as b:
        yield b


def _open(browser, tmp_path, ok=True, missing=False):
    page_html = f"""<!doctype html><html><head><meta charset="utf-8"></head><body><div id="live-log">
<div class="msg user"><div class="msg-body">Does search work now?</div></div>
<div class="msg asst"><div class="msg-body"><div class="vn-card-ph" data-card='{json.dumps(CARD)}'></div></div></div></div>
<script>try {{ localStorage.clear(); }} catch (e) {{}}
var liveSessionId='s1abc', sessionKinds={{s1abc:'idle'}}; window.__posts=[]; window.__toasts=[]; window.__sent=[];
window._liveSubmitDirect=(sid,text)=>{{ window.__sent.push([sid,text]); }};
window.showToast=(m,e)=>window.__toasts.push([m,!!e]);
window.fetch=(u,o)=>{{ window.__posts.push([u, JSON.parse(o.body)]);
  if ({'true' if missing else 'false'} && u === '/api/card-flag') return Promise.resolve({{ok:false, status:404, json:()=>Promise.reject(new Error('html'))}});
  return Promise.resolve({{ok:{'true' if ok else 'false'}, status:{200 if ok else 500}, json:()=>Promise.resolve({json.dumps({"ok": True, "id": "fl-1", "session": "new1", "name": "Fix summary: Too long or wordy"}) if ok else 'null'})}}); }};</script>
<script src="{LAST_CARD.as_uri()}"></script>
<script>VNLastCard.renderIn(document.getElementById('live-log'));</script></body></html>"""
    f = tmp_path / "flag.html"
    f.write_text(page_html, encoding="utf-8")
    page = browser.new_page()
    errors = []
    page.on("pageerror", lambda e: errors.append(str(e)))
    page.goto(f.as_uri(), wait_until="load")
    page.wait_for_function("document.querySelectorAll('.lm.skim').length === 1")
    page.wait_for_timeout(60)
    assert not errors, errors
    return page


def test_pick_several_reasons_and_send(browser, tmp_path):
    page = _open(browser, tmp_path)
    try:
        page.locator(".lm-flag").click()
        assert page.locator(".lm-fl .fr").count() == 8
        assert page.locator(".lm-fl .go").is_disabled()
        page.locator('.lm-fl .fr[data-reason="technical"]').click()
        page.locator('.lm-fl .fr[data-reason="long"]').click()
        page.locator('.lm-fl .fr[data-reason="focus"]').click()
        page.locator('.lm-fl .fr[data-reason="focus"]').click()   # untick
        assert page.locator(".lm-fl .go").inner_text() == "Redo and fix (2)"
        page.locator(".lm-fl .lm-rd-note").fill("too much about ports")
        page.locator(".lm-fl .go").click()
        # the flag, then the redo record (which may be retried)
        page.wait_for_function("window.__posts.filter(p => p[0] === '/api/card-feedback').length >= 1")
        flags_sent = page.evaluate("window.__posts.filter(p => p[0] === '/api/card-flag')")
        assert len(flags_sent) == 1 and page.evaluate("window.__posts[0][0]") == "/api/card-flag"
        url, body = flags_sent[0]
        assert body["session"] == "s1abc" and body["reasons"] == ["technical", "long"]
        assert body["note"] == "too much about ports" and body["card"]["title"] == "Search box"
        assert body["your_message"] == "Does search work now?"
        page.wait_for_function("document.querySelector('.lm-flag').disabled")
        assert page.locator(".lm-flag").inner_text().strip() == "Flagged"
        assert "Fix summary" in page.evaluate("window.__toasts[0][0]")
        sent = page.evaluate("window.__sent")
        assert len(sent) == 1 and sent[0][0] == "s1abc" and sent[0][1].startswith("Flagged summary: your last Done card was off")
        assert sent[0][1].endswith("\nWhat was off: too much about ports")
        page.wait_for_function("!document.querySelector('.lm-fl')")
        # a reload keeps it flagged
        f = tmp_path / "flag.html"
        f.write_text(f.read_text(encoding="utf-8").replace("try { localStorage.clear(); } catch (e) {}", ""), encoding="utf-8")
        page.reload(wait_until="load")
        page.wait_for_function("document.querySelectorAll('.lm.skim').length === 1")
        page.wait_for_timeout(60)
        assert page.locator(".lm-flag").inner_text().strip() == "Flagged"
    finally:
        page.close()


def test_a_note_alone_is_enough_and_escape_sends_nothing(browser, tmp_path):
    page = _open(browser, tmp_path)
    try:
        page.locator(".lm-flag").click()
        page.keyboard.press("Escape")
        page.wait_for_timeout(250)
        assert page.locator(".lm-fl").count() == 0 and page.evaluate("window.__posts") == []
        page.locator(".lm-flag").click()
        page.locator(".lm-fl .lm-rd-note").fill("wrong tone")
        assert page.locator(".lm-fl .go").inner_text() == "Redo and fix"
    finally:
        page.close()


def test_a_failed_send_keeps_the_popup_to_try_again(browser, tmp_path):
    page = _open(browser, tmp_path, ok=False)
    try:
        page.locator(".lm-flag").click()
        page.locator('.lm-fl .fr[data-reason="kind"]').click()
        page.locator(".lm-fl .go").click()
        page.wait_for_function("document.querySelector('.lm-fl .go').textContent === 'Try again'")
        assert not page.locator(".lm-flag").is_disabled()
        assert page.evaluate("window.__toasts[0][1]") is True
    finally:
        page.close()


def test_a_server_without_the_flag_route_still_sends_the_redo(browser, tmp_path):
    """A web server started before the flag route answers 404: Send must not
    fail.  The redo goes to the session now; the flag waits in the outbox."""
    page = _open(browser, tmp_path, missing=True)
    try:
        page.locator(".lm-flag").click()
        page.locator('.lm-fl .fr[data-reason="long"]').click()
        page.locator(".lm-fl .go").click()
        page.wait_for_function("document.querySelector('.lm-flag').disabled")
        sent = page.evaluate("window.__sent")
        assert len(sent) == 1 and sent[0][1].startswith("Flagged summary:")
        assert not any(t[1] for t in page.evaluate("window.__toasts"))
        page.wait_for_function("window.__posts.some(p => p[0] === '/api/card-feedback')")
        box = page.evaluate("JSON.parse(localStorage.getItem('vnCardFeedbackOutbox') || '[]')")
        assert any(x["url"] == "/api/card-flag" for x in box), box
    finally:
        page.close()
