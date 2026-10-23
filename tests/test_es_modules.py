"""Leaf scripts loaded as ES modules (added 2026-10-23).

A ``<script type="module">`` is deferred, strict and has its own scope, so
only leaf files whose functions are first used by a person (a click) after the
page has loaded can be modules.  Converted: compare.js, extract.js and
kanban-report.js (every external caller is an inline onclick handler).  The
rejected candidates and why are in the C2 log
(docs/plans/runs/2026-10-23-0900-review-exec/C2-frontend.md); the short form is
in ``REJECTED`` below so a future conversion cannot quietly undo the reasoning.

Checks:
  * index.html loads exactly these files with ``type="module"``;
  * each one ``export``s its public functions and shims every export onto
    ``window`` (inline handlers and classic scripts can only see window);
  * nothing else in static/js was made a module;
  * in the real page (headless Chromium, faked network) the shims are
    functions after load and each entry point runs without a page error.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from tests._browser import shared_browser_scope
from tests._fake_app import FakeApp, fake_sessions

XDIST_SPLITTABLE = True

ROOT = Path(__file__).resolve().parents[1]
JS = ROOT / "static" / "js"
INDEX = ROOT / "templates" / "index.html"

MODULES = ["compare.js", "extract.js", "kanban-report.js"]

# Candidates looked at and kept classic (see the C2 log for the evidence).
REJECTED = {
    "time-utils.js": "_shortDate is called unguarded by renderList (sessions.js) and kanban.js renders, "
                     "which can run from a fetch that resolves before deferred modules execute",
    "smart-copy.js": "addSmartCopyButtons runs in the live-panel / chat render path, guarded by typeof: "
                     "an early render would silently lose its copy buttons",
    "embed.js": "loaded in <head> and must shim localStorage before every other script runs",
}

_EXPORT = re.compile(r"^export\s+(?:async\s+)?function\s+([A-Za-z_$][\w$]*)", re.M)


def _index() -> str:
    return INDEX.read_text(encoding="utf-8")


def test_index_loads_the_converted_files_as_modules():
    html = _index()
    for f in MODULES:
        tag = "<script type=\"module\" src=\"{{ versioned_static('js/%s') }}\"></script>" % f
        assert html.count(tag) == 1, f"{f} is not loaded as a module"
        assert "<script src=\"{{ versioned_static('js/%s') }}\">" % f not in html, f"{f} is also loaded classic"


def test_no_other_script_is_a_module():
    mods = re.findall(r"<script type=\"module\" src=\"\{\{ versioned_static\('js/([^']+)'\) \}\}\"", _index())
    assert sorted(mods) == sorted(MODULES)
    for f in REJECTED:
        assert f not in mods, f"{f} was rejected as a module: {REJECTED[f]}"


@pytest.mark.parametrize("name", MODULES)
def test_every_export_has_a_window_shim(name):
    src = (JS / name).read_text(encoding="utf-8")
    exports = _EXPORT.findall(src)
    assert exports, f"{name} exports nothing"
    for fn in exports:
        assert re.search(r"^window\.%s\s*=\s*%s;" % (re.escape(fn), re.escape(fn)), src, re.M), \
            f"{name}: export {fn} has no window.{fn} = {fn} shim"
    # Every function an inline handler in index.html calls from this file is exported.
    declared = set(re.findall(r"^(?:export\s+)?(?:async\s+)?function\s+([A-Za-z_$][\w$]*)", src, re.M))
    handlers = set(re.findall(r"on[a-z]+=\"([A-Za-z_$][\w$]*)\(", _index()))
    for fn in declared & handlers:
        assert fn in exports, f"index.html calls {fn}() inline but {name} does not export it"


@pytest.fixture(scope="module")
def browser():
    with shared_browser_scope() as b:
        yield b


@pytest.mark.slow
def test_module_entry_points_run_in_the_real_page(browser):
    rows = fake_sessions(3)
    routes = {"/api/extract-code/": lambda req: (200, {"blocks": [{"language": "py", "content": "print(1)",
                                                                   "is_shell": False, "duplicate_of": None}]}),
              "/api/compare/": lambda req: (200, {"session1": {}, "session2": {}})}
    app = FakeApp(browser, rows, extra_routes=routes)
    try:
        app.page.wait_for_function("document.readyState === 'complete'", timeout=10000)
        for fn in ["openCompare", "closeCompare", "runCompare", "openExtract", "closeExtract", "copyBlock",
                   "triggerExport", "openReportsPanel", "_loadMoreActivityLog"]:
            assert app.eval(f"() => typeof window.{fn}") == "function", fn
        # Compare: the overlay opens and closes through the module's handlers.
        app.eval("() => { activeId = %r; openCompare(); }" % rows[0]["id"])
        assert app.eval("() => document.getElementById('compare-overlay').classList.contains('open')")
        app.eval("() => document.getElementById('compare-overlay').click()")   # listener added at module top level
        assert not app.eval("() => document.getElementById('compare-overlay').classList.contains('open')")
        # Extract: the module's own state (extractBlocks) feeds the inline copyBlock handler.
        app.eval("() => openExtract()")
        app.page.wait_for_function("document.querySelectorAll('#extract-body .code-block-card').length === 1",
                                   timeout=5000)
        assert "copyBlock(0)" in app.eval("() => document.querySelector('#extract-body .code-copy-btn').getAttribute('onclick')")
        app.eval("() => closeExtract()")
        # Reports: renders into the Workflow board from empty report data.
        app.eval("() => { document.getElementById('kanban-board').style.display = ''; return openReportsPanel(); }")
        app.page.wait_for_function("!!document.querySelector('#kanban-board .report-dashboard')", timeout=5000)
        assert not app.errors, app.errors
    finally:
        app.close()
