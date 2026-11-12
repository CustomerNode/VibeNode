"""Automated session messages: text the person did not type is never "me".

A session's own scheduled check-ins, background-job notices and watchdog
nudges must not be drawn as the person's bubble, or Smart termination reads
them as requests.  Anything sent to a session that is not from the person is
drawn as a full-width "Automated session message" banner.

Covered here: the shared rule (``shared.transcripts.automated_source``), the
engine's entry mark, both web transcript readers, the checker skipping these
messages, and the banner itself in headless Chromium against the real
``live-panel.js`` functions and ``style.css``.
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from tests._browser import launch_chromium

from shared.transcripts import automated_source

ROOT = Path(__file__).resolve().parents[1]
LIVE_PANEL_JS = ROOT / "src" / "web" / "static" / "js" / "live-panel.js"

NOTICE = ("<task-notification>\n<task-id>b1</task-id>\n<status>completed</status>\n"
          "<summary>Background command \"Wait for the build\" completed (exit code 0)</summary>\n"
          "</task-notification>")
CHECK_IN = "Check the deploy watcher output; if my build is green, report done; else retry."

SAMPLES = [
    (NOTICE, False, "Background job finished"),
    ("[VibeNode watchdog] You ended your turn waiting on background work.", False, "VibeNode watchdog"),
    ("Continue from where you left off.", False, "Automatic continue"),
    (CHECK_IN, True, "Scheduled check-in"),
    (CHECK_IN, False, ""),
    ("please fix the header alignment", False, ""),
    ("can you continue from where you left off and also fix the footer", False, ""),
    ("Your response above was cut off mid-stream. Resume directly from where it stops.", True, "Automatic continue"),
    ("Your response above was stopped by a safety classifier - this is not a tool error.", True, "Safety notice"),
    ("Another Claude session sent a message:\n<cross-session-message from=\"x\">hi</cross-session-message>", True,
     "Message from another session"),
    ("Base directory for this skill: /home/x/.claude/skills/demo", True, "Skill loaded"),
    # The CLI flags its note on a picture the session looked at; it stays a pill.
    ("[Image: original 900x2504, displayed at 719x2000. Multiply coordinates by 1.25.]", True, ""),
    ("<system-reminder>\nnote\n</system-reminder>", True, ""),
]


@pytest.mark.parametrize("text,is_meta,want", SAMPLES)
def test_the_shared_rule_names_who_sent_it(text, is_meta, want):
    assert automated_source(text, is_meta) == want


def test_an_engine_entry_carries_the_mark_both_ways():
    from engine.session_manager import LogEntry
    e = LogEntry(kind="user", text=NOTICE, auto="Background job finished")
    d = e.to_dict()
    assert d["auto"] == "Background job finished"
    assert "auto" not in LogEntry(kind="user", text="hi there").to_dict()
    back = LogEntry.from_transcript([d])[0]
    assert back.auto == "Background job finished"


def _line(text, **extra):
    obj = {"type": "user", "message": {"role": "user", "content": text}}
    obj.update(extra)
    return json.dumps(obj)


def test_the_web_reader_marks_a_scheduled_check_in_and_leaves_his_words():
    from web.routes.ws_events import _parse_raw_line
    mine = _parse_raw_line(_line(CHECK_IN, isMeta=True))
    assert mine == [{"kind": "user", "text": CHECK_IN, "auto": "Scheduled check-in"}]
    his = _parse_raw_line(_line("please fix the header alignment"))
    assert his == [{"kind": "user", "text": "please fix the header alignment"}]


def test_the_web_readers_share_one_row_rule():
    from web.routes import live_api, ws_events
    obj = {"isMeta": True}
    assert live_api._user_row(CHECK_IN, obj) == ws_events._user_row(CHECK_IN, obj)


def _entries(*pairs):
    out = []
    for kind, text, *auto in pairs:
        out.append(SimpleNamespace(kind=kind, text=text, auto=(auto[0] if auto else ""),
                                   name="", id="", tool_use_id="", is_error=False, desc=""))
    return out


def test_the_checker_never_takes_an_automated_message_for_his_request():
    from engine.handoff_evidence import facts_from_entries
    entries = _entries(
        ("user", "make the preview titles line up with the table headers"),
        ("asst", "Lining them up now."),
        ("user", CHECK_IN, "Scheduled check-in"),
        ("user", NOTICE),
        ("user", "[VibeNode watchdog] You ended your turn waiting on background work."),
    )
    facts = facts_from_entries(entries)
    assert facts.last_user == "make the preview titles line up with the table headers"
    assert facts.requests == ["make the preview titles line up with the table headers"]
    assert all("watcher" not in r for r in facts.history)


# ── The banner, in a real browser ───────────────────────────────────────────

def _fn_source(src: str, name: str) -> str:
    start = src.index("\nfunction " + name + "(")
    end = src.index("\n}\n", start) + 3
    return src[start:end]


@pytest.fixture(scope="module")
def page():
    pw = pytest.importorskip("playwright.sync_api")
    src = LIVE_PANEL_JS.read_text(encoding="utf-8")
    js = "\n".join(_fn_source(src, n) for n in ("_automatedSource", "_automatedSummary", "_renderAutomatedMsg"))
    css = (ROOT / "src" / "web" / "static" / "style.css").read_text(encoding="utf-8")
    css += (ROOT / "src" / "web" / "static" / "css" / "work-trail.css").read_text(encoding="utf-8")
    html = ("<!doctype html><html data-theme='dark'><head><style>" + css + "</style></head>"
            "<body><div id='log' class='conversation' style='display:flex;flex-direction:column'></div><script>"
            "function _formatMsgTime(){return '7:24 PM'}\nfunction _noteDetailToggle(){}\n" + js +
            "</script></body></html>")
    with pw.sync_playwright() as p:
        try:
            browser = launch_chromium(p.chromium)
        except Exception as exc:  # pragma: no cover - no browser installed
            pytest.skip(f"no Chromium: {exc}")
        pg = browser.new_page()
        pg.set_content(html)
        yield pg
        browser.close()


# Every case the text alone decides (a scheduled check-in needs the server's mark).
@pytest.mark.parametrize("text,is_meta,want", [s for s in SAMPLES if s[2] != "Scheduled check-in"])
def test_the_browser_rule_matches_the_server_rule(page, text, is_meta, want):
    assert page.evaluate("t => _automatedSource({}, t)", text) == want


def test_the_server_mark_wins_in_the_browser(page):
    assert page.evaluate("t => _automatedSource({auto: 'Scheduled check-in'}, t)", CHECK_IN) == "Scheduled check-in"


def test_the_banner_reads_automated_never_me_and_opens_on_tap(page):
    got = page.evaluate("""t => {
        const d = document.createElement('div');
        _renderAutomatedMsg(d, 'Background job finished', t, 1700000000);
        document.getElementById('log').appendChild(d);
        const body = d.querySelector('.vn-auto-body');
        const before = getComputedStyle(body).display;
        d.querySelector('.vn-auto-bar').click();
        return {text: d.innerText, cls: d.className, before, after: getComputedStyle(body).display,
                body: body.textContent, expanded: d.querySelector('.vn-auto-bar').getAttribute('aria-expanded'),
                tag: getComputedStyle(d.querySelector('.vn-auto-tag')).textTransform};
    }""", NOTICE)
    assert "Automated session message".lower() in got["text"].lower()
    assert "Background job finished" in got["text"]
    assert "me" not in got["text"].split() and "Not from you".lower() not in got["text"].lower()
    assert "msg user" not in got["cls"]
    assert got["before"] == "none" and got["after"] == "block" and got["expanded"] == "true"
    assert got["body"] == 'Background command "Wait for the build" completed (exit code 0)'


def test_the_banner_never_puts_text_in_as_html(page):
    html = page.evaluate("""() => {
        const d = document.createElement('div');
        _renderAutomatedMsg(d, '<img src=x onerror=window.__x=1>', '<b>hi</b>', 0);
        return d.querySelector('.vn-auto-src').innerHTML + '|' + d.querySelector('.vn-auto-body').innerHTML;
    }""")
    assert "<img" not in html and "<b>" not in html


def test_a_steps_line_under_the_banner_gets_the_same_room_as_under_a_message(page):
    """``.conversation .live-entry { margin-bottom: 0 }`` must not beat the
    banner's own margin, or the steps line under it (margin-top -4px) rides up
    over its border.  Under a banner it must sit as it does under a message."""
    got = page.evaluate("""() => {
        const log = document.getElementById('log'); log.innerHTML = '';
        const add = (cls, html) => { const d = document.createElement('div'); d.className = cls; d.innerHTML = html || 'x'; log.appendChild(d); return d; };
        const m = add('msg assistant');
        const t1 = add('vn-trail');
        const a = document.createElement('div');
        _renderAutomatedMsg(a, 'VibeNode watchdog', 'hello', 0); log.appendChild(a);
        const t2 = add('vn-trail');
        const gap = (x, y) => Math.round(y.getBoundingClientRect().top - x.getBoundingClientRect().bottom);
        return {afterMsg: gap(m, t1), afterBanner: gap(a, t2)};
    }""")
    assert got["afterBanner"] > 0, got
    assert got["afterBanner"] == got["afterMsg"], got
