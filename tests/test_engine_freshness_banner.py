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
    assert buttons == ["Restart when idle", "Later"]


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


def test_later_hides_it(page):
    page.evaluate("() => EngineFreshness.check()")
    page.click("#engine-freshness-banner [data-act=later]")
    assert page.locator("#engine-freshness-banner").count() == 0
    page.evaluate("() => EngineFreshness.check()")
    page.wait_for_timeout(200)
    assert page.locator("#engine-freshness-banner").count() == 0


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
