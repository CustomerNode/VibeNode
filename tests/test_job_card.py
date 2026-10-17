"""Durable Jobs step 4: the needs-person push and the two buttons.

Only ``needs_person`` and ``failed`` surface, and they surface the way a
stalled session does: a push to every open tab, a card with the reason and
two buttons, Relaunch and Leave it (static/js/jobs.js, wired in socket.js).
Acceptance test 3's client half (the push arrives and shows), and test 7's
card half (a percentage on the card).

The page is the real stylesheet, the real ``utils.js`` (the float stack),
``notify.js`` and ``jobs.js``; the ``job_needs_person`` handler is cut from
the real ``socket.js`` and run against a fake socket; ``fetch`` is stubbed.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

# Every test builds its own page, so a parallel run may split this file
# across workers (see tests/_xdist_schedule.py).
XDIST_SPLITTABLE = True

ROOT = Path(__file__).resolve().parents[1]
STATIC = ROOT / "static"

PAYLOAD = {
    "job_id": "final-rebuild-waiter", "name": "final rebuild waiter", "state": "needs_person",
    "reason": "stopped past an irreversible step ('rebuild launched on donvm' matched); not relaunched",
    "progress": {"n": 90000, "m": 143719, "pct": 62.6, "text": "cells 90000/143719"},
    "session_id": "sess-1", "project": "Altium",
}


def _socket_handlers() -> str:
    src = (STATIC / "js" / "socket.js").read_text(encoding="utf-8")
    m = re.search(r"socket\.on\('job_needs_person'.*?\n\}\);\nsocket\.on\('job_registry_error'.*?\n\}\);\n", src, re.S)
    assert m, "the job handlers are gone from socket.js"
    return m.group(0)


def _page_html() -> str:
    style = (STATIC / "style.css").read_text(encoding="utf-8")
    utils = (STATIC / "js" / "utils.js").read_text(encoding="utf-8")
    notify = (STATIC / "js" / "notify.js").read_text(encoding="utf-8")
    jobs = (STATIC / "js" / "jobs.js").read_text(encoding="utf-8")
    return f"""<!doctype html>
<html data-theme="dark"><head><meta charset="utf-8"><style>{style}</style></head><body>
<div class="layout"><div class="main"><div class="live-panel" id="live-panel">
  <div class="conversation live-log" id="live-log" style="height:300px"></div>
  <div class="live-input-bar" id="live-input-bar">
    <textarea id="live-input-ta" class="live-textarea" rows="2"></textarea>
    <div class="live-bar-row"><button class="live-send-btn" id="live-send-btn">Send</button></div>
  </div>
</div></div></div>
<div class="toast" id="toast"></div>
<div id="pm-overlay"></div>
<script>
  window.__posts = [];
  window.fetch = function (url, opts) {{
    window.__posts.push([url, (opts && opts.method) || 'GET']);
    var body = {{ ok: true, job: {{ id: 'x' }}, jobs: window.__jobs || [], tail: 'line one\\nline two', path: 'C:/log' }};
    return Promise.resolve({{ status: 200, ok: true, json: function () {{ return Promise.resolve(body); }} }});
  }};
  var handlers = {{}};
  var socket = {{ on: function (ev, fn) {{ handlers[ev] = fn; }} }};
  function _closePm() {{ document.getElementById('pm-overlay').classList.remove('show'); }}
</script>
<script>{utils}</script>
<script>{notify}</script>
<script>{jobs}</script>
<script>{_socket_handlers()}</script>
</body></html>"""


@pytest.fixture(scope="module")
def browser():
    pw = pytest.importorskip("playwright.sync_api", reason="playwright not installed")
    with pw.sync_playwright() as p:
        try:
            b = p.chromium.launch()
        except Exception as exc:  # pragma: no cover
            pytest.skip(f"chromium unavailable: {str(exc).splitlines()[0][:80]}")
        yield b
        b.close()


@pytest.fixture
def page(browser):
    pg = browser.new_page(viewport={"width": 1100, "height": 800})
    pg.set_content(_page_html())
    yield pg
    pg.close()


def test_the_push_shows_a_card_with_reason_progress_and_two_buttons(page):
    page.evaluate("(d) => handlers['job_needs_person'](d)", PAYLOAD)
    page.wait_for_selector(".vn-job-toast.show")
    card = page.locator(".vn-job-toast")
    assert card.count() == 1
    assert "final rebuild waiter" in card.locator(".vn-job-toast-title").text_content()
    assert "rebuild launched on donvm" in card.locator(".vn-job-toast-reason").text_content()
    assert card.locator(".vn-job-toast-pct").text_content() == "63%"
    buttons = [b.strip() for b in card.locator("button").all_text_contents()]
    assert buttons == ["Relaunch", "Leave it"]
    # It asks for attention the way a stalled session does (chime + flash).
    assert "attention:job:final-rebuild-waiter" in page.evaluate("() => VNNotify.state().attention")
    # It sits in the float stack above the composer, not over it.
    bar_top = page.evaluate("() => document.getElementById('live-input-bar').getBoundingClientRect().top")
    card_bottom = page.evaluate("() => document.querySelector('.vn-job-toast').getBoundingClientRect().bottom")
    assert card_bottom <= bar_top


def test_relaunch_posts_relaunch_and_leave_posts_leave(page):
    page.evaluate("(d) => handlers['job_needs_person'](d)", PAYLOAD)
    page.wait_for_selector(".vn-job-toast.show")
    page.click(".vn-job-relaunch")
    page.wait_for_function("() => !document.querySelector('.vn-job-toast')")
    assert page.evaluate("() => window.__posts") == [["/api/jobs/final-rebuild-waiter/relaunch", "POST"]]
    assert "Relaunched" in page.evaluate("() => document.getElementById('toast').textContent")

    page.evaluate("() => { window.__posts = []; }")
    page.evaluate("(d) => handlers['job_needs_person'](d)", dict(PAYLOAD, job_id="auth", name="authorship read",
                                                                  state="failed", reason="launch failed: no python",
                                                                  progress=None))
    page.wait_for_selector(".vn-job-toast.show")
    assert page.locator(".vn-job-toast-title").text_content().startswith("Job failed:")
    assert page.locator(".vn-job-toast-pct").count() == 0
    page.click(".vn-job-leave")
    page.wait_for_function("() => !document.querySelector('.vn-job-toast')")
    assert page.evaluate("() => window.__posts") == [["/api/jobs/auth/leave", "POST"]]


def test_two_cards_stack_and_a_repeat_push_replaces_its_card(page):
    page.evaluate("(d) => handlers['job_needs_person'](d)", PAYLOAD)
    page.evaluate("(d) => handlers['job_needs_person'](d)", dict(PAYLOAD, job_id="b", name="second"))
    page.wait_for_function("() => document.querySelectorAll('.vn-job-toast.show').length === 2")
    bottoms = page.evaluate("() => [...document.querySelectorAll('.vn-job-toast')].map(e => e.getBoundingClientRect().bottom)")
    assert len(set(round(b) for b in bottoms)) == 2, "the two cards must not overlap"
    page.evaluate("(d) => handlers['job_needs_person'](d)", dict(PAYLOAD, reason="again"))
    page.wait_for_function("() => document.querySelectorAll('.vn-job-toast[data-job-id=\"final-rebuild-waiter\"]').length === 1 && document.querySelector('.vn-job-toast[data-job-id=\"final-rebuild-waiter\"] .vn-job-toast-reason').textContent === 'again'")


def test_the_registry_error_is_a_plain_toast(page):
    page.evaluate("() => handlers['job_registry_error']({path: 'C:/x/jobs.json', error: 'not valid JSON'})")
    toast = page.locator("#toast")
    assert "not valid JSON" in toast.text_content()
    assert "error" in toast.get_attribute("class")
    assert page.locator(".vn-job-toast").count() == 0


def test_the_system_list_shows_progress_as_a_percentage(page):
    """Acceptance test 7, card side: "cells 90000/143719" reads 63% on the card."""
    jobs = [{"id": "auth", "name": "authorship read", "state": "running", "command": "python authorship.py",
             "project": "Altium", "last_seen_alive": 1, "relaunch_count": 1, "reason": "",
             "progress_value": {"n": 90000, "m": 143719, "pct": 62.6, "text": "cells 90000/143719"}},
            {"id": "w", "name": "waiter", "state": "finished", "command": "python w.py", "progress_value": None}]
    page.evaluate("(j) => { window.__jobs = j; }", jobs)
    page.evaluate("() => openDurableJobs()")
    page.wait_for_selector(".vn-job-row")
    rows = page.locator(".vn-job-row")
    assert rows.count() == 2
    first = rows.nth(0)
    assert first.locator(".vn-job-pct").text_content() == "63%"
    assert "cells 90000/143719" in first.locator(".vn-job-ptext").text_content()
    assert first.locator(".vn-job-bar-fill").get_attribute("style") == "width:63%"
    assert first.locator(".vn-job-state").text_content() == "running"
    assert rows.nth(1).locator(".vn-job-relaunch-btn").count() == 0, "a finished job offers no relaunch"
    first.locator(".vn-job-log-btn").click()
    page.wait_for_function("() => document.querySelector('.vn-job-log').textContent.includes('line two')")
    assert ["/api/jobs", "/api/jobs/auth/log"] == [u for u, m in page.evaluate("() => window.__posts")]


def test_the_system_menu_and_page_wire_it_in():
    html = (ROOT / "templates" / "index.html").read_text(encoding="utf-8")
    assert 'onclick="openDurableJobs();closeHdrSys()"' in html
    assert "js/jobs.js" in html
    utils = (STATIC / "js" / "utils.js").read_text(encoding="utf-8")
    assert ".vn-job-toast" in utils, "the card must be in the float stack (_floatEls)"
