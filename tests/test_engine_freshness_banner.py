"""The outdated-engine banner, in a real browser (static/js/engine-freshness.js).

Why (2026-10-20): the Session Engine ran Oct 6 code for two weeks while every
session-recovery fix sat on disk, and nothing told the user.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from tests._browser import shared_browser_scope

XDIST_SPLITTABLE = True

ROOT = Path(__file__).resolve().parents[1]
STALE = {"ok": True, "running": True, "stale": True, "started_at": 1791306216,
         "outdated": ["daemon/session_manager.py", "daemon/health_monitor.py"],
         "restart_armed": False}


def _page_html() -> str:
    src = (ROOT / "static" / "js" / "engine-freshness.js").read_text(encoding="utf-8")
    return f"""<!doctype html><html><head><meta charset="utf-8"></head><body>
<script>
  window.__status = {json.dumps(STALE)};
  window.__posts = [];
  window.__restarts = [];
  function _doRestart(scope) {{ window.__restarts.push(scope); }}
  function showConfirm() {{ return Promise.resolve(true); }}
  window.fetch = function (url, opts) {{
    var body;
    if (url === '/api/engine/status') {{
      body = window.__status;
    }} else {{
      var sent = JSON.parse(opts.body);
      window.__posts.push([url, sent]);
      window.__status = Object.assign({{}}, window.__status,
        {{ restart_armed: sent.arm, waiting_on: sent.arm ? ['Fixer is working'] : [] }});
      body = {{ ok: true }};
    }}
    return Promise.resolve({{ ok: true, json: function () {{ return Promise.resolve(body); }} }});
  }};
</script>
<script>{src}</script>
</body></html>"""


@pytest.fixture(scope="module")
def browser():
    with shared_browser_scope() as b:
        yield b


@pytest.fixture
def page(browser):
    pg = browser.new_page(viewport={"width": 1100, "height": 700})
    pg.set_content(_page_html())
    yield pg
    pg.close()


def test_outdated_engine_shows_the_banner_with_a_restart_when_idle_button(page):
    page.evaluate("() => EngineFreshness.check()")
    page.wait_for_selector("#engine-freshness-banner")
    text = page.locator("#engine-freshness-banner").text_content()
    assert "2 engine files have changed" in text
    assert "not running until it restarts" in text
    buttons = [b.strip() for b in page.locator("#engine-freshness-banner button").all_text_contents()]
    assert buttons == ["Now", "When sessions done", "Hide▾"]


def test_restart_when_idle_arms_the_server_and_shows_what_it_waits_on(page):
    page.evaluate("() => EngineFreshness.check()")
    page.click("#engine-freshness-banner [data-act=arm]")
    page.wait_for_function("() => /Waiting on: Fixer is working/.test("
                           "document.getElementById('engine-freshness-banner').textContent)")
    assert page.evaluate("() => window.__posts") == [["/api/engine/restart-when-idle", {"arm": True}]]
    # Restart now: cancels the wait and restarts through the Developer Tools path.
    page.click("#engine-freshness-banner [data-act=now]")
    page.wait_for_function("() => window.__restarts.length === 1")
    assert page.evaluate("() => window.__restarts") == ["daemon"]
    assert page.evaluate("() => window.__posts")[-1] == ["/api/engine/restart-when-idle", {"arm": False}]


def test_current_engine_shows_nothing(page):
    page.evaluate("() => { window.__status = {ok: true, running: true, stale: false, outdated: []}; }")
    page.evaluate("() => EngineFreshness.check()")
    page.wait_for_timeout(200)
    assert page.locator("#engine-freshness-banner").count() == 0


def _hide(page, key):
    page.click("#engine-freshness-banner [data-act=later]")
    page.wait_for_selector("#engine-freshness-hide-menu")
    page.click(f"#engine-freshness-hide-menu [data-hide=\"{key}\"]")


def test_later_hides_it(page):
    page.evaluate("() => EngineFreshness.check()")
    _hide(page, "4h")
    assert page.locator("#engine-freshness-banner").count() == 0
    page.evaluate("() => EngineFreshness.check()")
    page.wait_for_timeout(200)
    assert page.locator("#engine-freshness-banner").count() == 0


def test_the_waiting_bar_can_be_hidden_and_the_restart_stays_queued(page):
    """2026-10-29, the owner on a phone: "You can't have this always on top I
    can't do anything".  The waiting bar re-drew every 15 s with only Restart
    now and Cancel; Hide removes it without cancelling the queued restart."""
    page.evaluate("() => EngineFreshness.check()")
    page.click("#engine-freshness-banner [data-act=arm]")
    page.wait_for_selector("#engine-freshness-banner [data-act=now]")
    buttons = [b.strip() for b in page.locator("#engine-freshness-banner button").all_text_contents()]
    assert buttons == ["Now", "When sessions done", "Cancel", "Hide▾"]
    assert page.get_attribute("#engine-freshness-banner [data-act=armed]", "aria-pressed") == "true"
    _hide(page, "1h")
    assert page.locator("#engine-freshness-banner").count() == 0
    page.evaluate("() => EngineFreshness.check()")
    page.wait_for_timeout(200)
    assert page.locator("#engine-freshness-banner").count() == 0
    # Hiding never sent a cancel: the only post is the arm.
    assert page.evaluate("() => window.__posts") == [["/api/engine/restart-when-idle", {"arm": True}]]


def test_hide_offers_a_menu_of_times(page):
    """2026-10-30, the owner: "a button that delays the window from returning
    and give some menu of time options"."""
    page.evaluate("() => EngineFreshness.check()")
    page.wait_for_selector("#engine-freshness-banner")
    page.click("#engine-freshness-banner [data-act=later]")
    items = [t.strip() for t in page.locator("#engine-freshness-hide-menu [data-hide]").all_text_contents()]
    assert items == ["For 1 hour", "For 4 hours", "Until tomorrow morning", "For a week", "Until it restarts"]
    # The banner stays until a time is picked; Escape closes the menu only.
    page.keyboard.press("Escape")
    assert page.locator("#engine-freshness-hide-menu").count() == 0
    assert page.locator("#engine-freshness-banner").count() == 1


def test_each_choice_hides_for_its_own_time(page):
    got = page.evaluate("""() => {
      const now = new Date(2026, 9, 30, 11, 54).getTime();
      const f = EngineFreshness._hideUntil;
      const m = new Date(f('morning', now));
      return {h1: (f('1h', now) - now) / 3600000, h4: (f('4h', now) - now) / 3600000,
              wk: (f('week', now) - now) / 86400000,
              morning: [m.getDate(), m.getHours(), m.getMinutes()]};
    }""")
    assert got == {"h1": 1, "h4": 4, "wk": 7, "morning": [31, 8, 0]}


def test_a_hide_that_runs_out_brings_the_banner_back(page):
    page.evaluate("() => EngineFreshness.check()")
    _hide(page, "1h")
    page.evaluate("() => EngineFreshness.check()")
    page.wait_for_timeout(200)
    assert page.locator("#engine-freshness-banner").count() == 0
    # Two hours later the hour is over.
    page.evaluate("() => { const real = Date.now; Date.now = () => real() + 2 * 3600 * 1000; }")
    page.evaluate("() => EngineFreshness.check()")
    page.wait_for_selector("#engine-freshness-banner")


def test_until_it_restarts_holds_for_this_engine_and_returns_after_a_restart(page):
    page.evaluate("() => EngineFreshness.check()")
    _hide(page, "restart")
    page.evaluate("() => EngineFreshness.check()")
    page.wait_for_timeout(200)
    assert page.locator("#engine-freshness-banner").count() == 0
    # The engine restarted and is behind again: the banner is back.
    page.evaluate("() => { window.__status = Object.assign({}, window.__status, {started_at: 1791400000}); }")
    page.evaluate("() => EngineFreshness.check()")
    page.wait_for_selector("#engine-freshness-banner")


def test_the_waiting_bar_is_at_most_two_lines_on_a_phone(browser):
    pg = browser.new_page(viewport={"width": 390, "height": 844})
    try:
        pg.set_content(_page_html())
        pg.evaluate("() => { window.__status = Object.assign({}, window.__status, {restart_armed: true,"
                    " waiting_on: ['057f5033 is working', '71bd161d wrote output 214s ago',"
                    " 'Fix Test Failures wrote output 94s ago', 'a0e9e819 wrote output 190s ago',"
                    " 'x', 'y']}); }")
        pg.evaluate("() => EngineFreshness.check()")
        pg.wait_for_selector("#engine-freshness-banner")
        h = pg.evaluate("() => document.getElementById('engine-freshness-banner').offsetHeight")
        assert h <= 80, h
    finally:
        pg.close()


def test_the_page_loads_the_banner():
    html = (ROOT / "templates" / "index.html").read_text(encoding="utf-8")
    assert "js/engine-freshness.js" in html


# ── SDK patches did not apply (2026-10-23) ───────────────────────────────

SDK_BAD = {"ok": True, "running": True, "stale": False, "outdated": [], "restart_armed": False,
           "sdk_version": "0.0.31", "sdk_version_tested": True,
           "sdk_patch_failures": ["transport_adapter", "raise_json_buffer_limit"],
           "sdk_patch_failure_details": [
               {"name": "transport_adapter", "precondition": "Query.__init__ missing 'transport' param"}]}


def test_failed_sdk_patches_show_a_red_banner_naming_them(page):
    page.evaluate("(s) => { window.__status = s; }", SDK_BAD)
    page.evaluate("() => EngineFreshness.check()")
    page.wait_for_selector("#sdk-patch-banner")
    bar = page.locator("#sdk-patch-banner")
    text = bar.text_content()
    assert "SDK patches did not apply: transport_adapter, raise_json_buffer_limit (SDK 0.0.31)" in text
    assert bar.get_attribute("role") == "alert"
    assert "missing 'transport' param" in bar.get_attribute("title")
    # Red, and separate from the amber "outdated code" banner.
    bg = page.evaluate("() => getComputedStyle(document.getElementById('sdk-patch-banner')).backgroundColor")
    assert bg == "rgb(90, 17, 17)"
    assert page.locator("#engine-freshness-banner").count() == 0


def test_both_banners_stack_when_the_engine_is_also_outdated(page):
    both = dict(STALE, **{k: SDK_BAD[k] for k in ("sdk_version", "sdk_patch_failures")})
    page.evaluate("(s) => { window.__status = s; }", both)
    page.evaluate("() => EngineFreshness.check()")
    page.wait_for_selector("#engine-freshness-banner")
    page.wait_for_selector("#sdk-patch-banner")
    sdk_box = page.locator("#sdk-patch-banner").bounding_box()
    stale_box = page.locator("#engine-freshness-banner").bounding_box()
    assert stale_box["y"] >= sdk_box["y"] + sdk_box["height"] - 1, (sdk_box, stale_box)


def test_untested_sdk_version_is_mentioned(page):
    page.evaluate("(s) => { window.__status = Object.assign({}, s, {sdk_version_tested: false}); }", SDK_BAD)
    page.evaluate("() => EngineFreshness.check()")
    page.wait_for_selector("#sdk-patch-banner")
    assert "outside the range" in page.locator("#sdk-patch-banner").text_content()


def test_dismiss_hides_it_for_this_page_only(page):
    page.evaluate("(s) => { window.__status = s; }", SDK_BAD)
    page.evaluate("() => EngineFreshness.check()")
    page.wait_for_selector("#sdk-patch-banner")
    page.click("#sdk-patch-banner [data-act=sdk-dismiss]")
    assert page.locator("#sdk-patch-banner").count() == 0
    page.evaluate("() => EngineFreshness.check()")
    page.wait_for_timeout(200)
    assert page.locator("#sdk-patch-banner").count() == 0


def test_an_engine_with_all_patches_applied_shows_no_sdk_banner(page):
    page.evaluate("() => { window.__status = {ok: true, running: true, stale: false, outdated: [],"
                  " sdk_version: '0.0.25', sdk_patch_failures: []}; }")
    page.evaluate("() => EngineFreshness.check()")
    page.wait_for_timeout(200)
    assert page.locator("#sdk-patch-banner").count() == 0
