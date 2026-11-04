"""The outdated-engine banner, in a real browser (src/web/static/js/engine-freshness.js).

Why: the Session Engine keeps the code it started with, so fixes on disk do not
run until it restarts.  An outdated engine queues a restart for when it is idle.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from tests._browser import shared_browser_scope
from tests._fake_app import PAGE_CLOCK, PageClock

XDIST_SPLITTABLE = True

ROOT = Path(__file__).resolve().parents[1]
STALE = {"ok": True, "running": True, "stale": True, "started_at": 1791306216,
         "outdated": ["src/engine/session_manager.py", "src/engine/health_monitor.py"],
         "restart_armed": False}


def _page_html() -> str:
    src = (ROOT / "src" / "web" / "static" / "js" / "engine-freshness.js").read_text(encoding="utf-8")
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
    pg.evaluate(PAGE_CLOCK)     # the banner's timers run on _settle(), not on the wall clock
    pg.set_content(_page_html())
    yield pg
    pg.close()


def _settle(page, ms=200):
    """Every timer of the page due in the next ``ms`` has fired."""
    PageClock(page).run_for(ms)


# ── No restart banner, ever ──────────────────────────────────────────────

def test_an_outdated_engine_shows_no_banner_and_queues_the_idle_restart_once(page):
    page.evaluate("() => EngineFreshness.check()")
    _settle(page)
    assert page.locator("#engine-freshness-banner").count() == 0
    assert page.evaluate("() => window.__posts") == [["/api/engine/restart-when-idle", {"arm": True}]]
    # Once per engine start: later checks of the same engine post nothing more.
    page.evaluate("(s) => { window.__status = Object.assign({}, s, {restart_armed: false}); }", STALE)
    page.evaluate("() => EngineFreshness.check()")
    _settle(page)
    assert len(page.evaluate("() => window.__posts")) == 1
    # A new engine start that is outdated again gets its own restart.
    page.evaluate("(s) => { window.__status = Object.assign({}, s, {started_at: s.started_at + 500}); }", STALE)
    page.evaluate("() => EngineFreshness.check()")
    _settle(page)
    assert len(page.evaluate("() => window.__posts")) == 2
    assert page.locator("#engine-freshness-banner").count() == 0


def test_a_current_engine_posts_nothing(page):
    page.evaluate("() => { window.__status = {ok: true, running: true, stale: false, outdated: []}; }")
    page.evaluate("() => EngineFreshness.check()")
    _settle(page)
    assert page.evaluate("() => window.__posts") == []
    assert page.locator("#engine-freshness-banner").count() == 0


def test_the_banner_code_is_gone():
    src = (ROOT / "src" / "web" / "static" / "js" / "engine-freshness.js").read_text(encoding="utf-8")
    for gone in ("engine-freshness-banner", "data-act=\"arm\"", "Hide this banner", "_confirmArmed"):
        assert gone not in src, gone


def test_the_page_loads_the_script():
    html = (ROOT / "src" / "web" / "templates" / "index.html").read_text(encoding="utf-8")
    assert "js/engine-freshness.js" in html


# ── SDK patches did not apply ────────────────────────────────────────────

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
    _settle(page)
    assert page.locator("#sdk-patch-banner").count() == 0


def test_an_engine_with_all_patches_applied_shows_no_sdk_banner(page):
    page.evaluate("() => { window.__status = {ok: true, running: true, stale: false, outdated: [],"
                  " sdk_version: '0.0.25', sdk_patch_failures: []}; }")
    page.evaluate("() => EngineFreshness.check()")
    _settle(page)
    assert page.locator("#sdk-patch-banner").count() == 0
