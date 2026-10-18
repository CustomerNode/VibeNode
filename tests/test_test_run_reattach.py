"""The browser side of a test run survives a dropped connection.

_streamTestRun() in static/js/test-runner.js watches a run that lives on the
server (app/routes/test_api.py).  Reported 2026-10-18: a Publish started
from a phone sat on "running" for 20 minutes after the phone locked its
screen, because a stream that ended without a "done" event was simply
dropped.  Now it reattaches with the run id and the lines it already has,
waits while the tab is hidden and retries the moment it is visible, and only
gives up (as "lost", never as "passed") when the server no longer has the run.

The real test-runner.js runs in headless Chromium; fetch is faked so the test
decides when each stream delivers, drops or ends.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from tests._browser import shared_browser_scope

ROOT = Path(__file__).resolve().parents[1]
RUNNER_JS = (ROOT / "static" / "js" / "test-runner.js").read_text(encoding="utf-8")

# A fake fetch: every /api/run-tests request gets a stream the test controls.
HARNESS = r"""
window.__reqs = []; window.__streams = []; window.__result = null;
window.showGitSyncModal = () => {}; window.closeGitSyncModal = () => {};
let __hidden = false;
Object.defineProperty(document, 'visibilityState', {get: () => __hidden ? 'hidden' : 'visible'});
window.__setHidden = h => { __hidden = h; document.dispatchEvent(new Event('visibilitychange')); };
window.fetch = (url, opts) => {
  window.__reqs.push(JSON.parse(opts.body));
  let ctl;
  const body = new ReadableStream({start(c) { ctl = c; }});
  window.__streams.push(ctl);
  return Promise.resolve({body});
};
window.__send = (i, obj) => window.__streams[i].enqueue(new TextEncoder().encode('data: ' + JSON.stringify(obj) + '\n\n'));
window.__drop = i => window.__streams[i].close();
window.__start = () => _streamTestRun('full', {
  onLine: () => {},
  onDone: (d, lines) => { window.__result = {done: d, lines: lines.slice()}; },
  onError: (msg, info) => { window.__result = {error: msg, info}; },
});
"""


@pytest.fixture(scope="module")
def browser():
    with shared_browser_scope() as b:
        yield b


@pytest.fixture
def page(browser):
    pg = browser.new_page()
    pg.set_content("<html><body></body></html>")
    pg.add_script_tag(content=RUNNER_JS)
    pg.add_script_tag(content=HARNESS)
    return pg


def _wait(page, expr, timeout=5000):
    page.wait_for_function(expr, timeout=timeout)


@pytest.mark.slow
def test_a_dropped_stream_reattaches_and_gets_the_rest(page):
    page.evaluate("__start()")
    _wait(page, "__streams.length === 1")
    page.evaluate("""() => { __send(0, {type: 'run', run_id: 'r1', mode: 'full'});
                             __send(0, {type: 'line', line: 'a'}); __send(0, {type: 'line', line: 'b'});
                             __drop(0); }""")
    _wait(page, "__streams.length === 2")            # reattached on its own
    assert page.evaluate("__reqs[1]") == {"mode": "full", "run_id": "r1", "since": 2}
    page.evaluate("""() => { __send(1, {type: 'run', run_id: 'r1', mode: 'full'});
                             __send(1, {type: 'line', line: 'c'});
                             __send(1, {type: 'done', ok: true, passed: 3}); }""")
    _wait(page, "__result !== null")
    assert page.evaluate("__result.lines") == ["a", "b", "c"]
    assert page.evaluate("__result.done.ok") is True


@pytest.mark.slow
def test_a_locked_phone_waits_then_reattaches_the_moment_it_is_back(page):
    page.evaluate("__start()")
    _wait(page, "__streams.length === 1")
    page.evaluate("""() => { __send(0, {type: 'run', run_id: 'r2', mode: 'full'});
                             __setHidden(true); __drop(0); }""")
    page.wait_for_timeout(2500)                      # past the first backoff
    assert page.evaluate("__streams.length") == 1 and page.evaluate("__result") is None
    page.evaluate("__setHidden(false)")
    _wait(page, "__streams.length === 2", timeout=1000)
    assert page.evaluate("__reqs[1].run_id") == "r2"


@pytest.mark.slow
def test_a_run_the_server_no_longer_has_is_reported_lost_not_passed(page):
    page.evaluate("__start()")
    _wait(page, "__streams.length === 1")
    page.evaluate("() => { __send(0, {type: 'run', run_id: 'r3', mode: 'full'}); __drop(0); }")
    _wait(page, "__streams.length === 2")
    page.evaluate("""() => __send(1, {type: 'error', line: 'That test run is no longer available.', lost: true})""")
    _wait(page, "__result !== null")
    assert page.evaluate("__result.info") == {"lost": True, "started": True}


@pytest.mark.slow
def test_a_run_that_never_started_is_an_error_not_a_retry_loop(page):
    page.evaluate("__start()")
    _wait(page, "__streams.length === 1")
    page.evaluate("__drop(0)")                       # no run id ever arrived
    _wait(page, "__result !== null")
    assert page.evaluate("__result.info") == {"lost": False, "started": False}
    page.wait_for_timeout(1500)
    assert page.evaluate("__streams.length") == 1
