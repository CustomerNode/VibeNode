"""One browser per test worker, shared by consecutive browser test files.

Each browser test file used to start its own Playwright driver and Chromium
and shut both down at the end.  Idle that costs ~0.3s; in the parallel run a
shutdown took 4-30s (measured 2026-10-17: twelve per run, half of them
24-30s), and with files split into chunks across workers there were ~20 per
run.

Now a worker starts one driver + browser on first use and keeps it while it
runs browser test files back to back.  ``tests/conftest.py`` closes it as
soon as the next test is not a browser test (and at session end), so no
Playwright event loop is alive while other tests run.  Isolation is
unchanged: each file's fixtures still open their own contexts and pages, and
``shared_browser_scope`` closes every context a file opened when the file is
done, exactly what closing the browser per file used to do.

Tried and rejected (2026-10-17): launching the installed native ARM64 Chrome
on Windows ARM64 instead of Playwright's bundled x64 build.  Alone it never
stalled where the emulated build did, but in the full run it took the CPU the
Python workers need: no faster (80-90s vs 69-75s) and 5s waits in daemon
tests started timing out.  Keep the bundled build.
"""

from __future__ import annotations

import contextlib

_shared = {"pw": None, "browser": None}


def shared_browser():
    """This worker's browser, started on first use; skips if unavailable."""
    if _shared["browser"] is None:
        import pytest
        pw = pytest.importorskip("playwright.sync_api", reason="playwright not installed")
        mgr = pw.sync_playwright().start()
        try:
            browser = mgr.chromium.launch()
        except Exception as exc:  # browser binary not downloaded on this machine
            mgr.stop()
            pytest.skip(f"chromium unavailable: {str(exc).splitlines()[0][:80]}")
        _shared.update(pw=mgr, browser=browser)
    return _shared["browser"]


@contextlib.contextmanager
def shared_browser_scope():
    """Use the shared browser for one test file; close what the file opened."""
    browser = shared_browser()
    before = set(browser.contexts)
    try:
        yield browser
    finally:
        for ctx in list(browser.contexts):
            if ctx not in before:
                with contextlib.suppress(Exception):
                    ctx.close()


def close_shared_browser():
    browser, mgr = _shared["browser"], _shared["pw"]
    _shared.update(pw=None, browser=None)
    if browser is not None:
        with contextlib.suppress(Exception):
            browser.close()
    if mgr is not None:
        with contextlib.suppress(Exception):
            mgr.stop()


def uses_shared_browser(module) -> bool:
    return getattr(module, "shared_browser_scope", None) is shared_browser_scope
