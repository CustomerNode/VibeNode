"""The Publish dialog records what changed and how the gate went
(src/web/static/js/git-sync.js), in a real browser.

Only the network and the test-run stream are faked; the dialog markup is the
one from index.html and the script is the real file.
"""

from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

from tests._browser import shared_browser_scope

XDIST_SPLITTABLE = True

ROOT = Path(__file__).resolve().parents[1]

MODES = {"fast": {"label": "Fast: unit + browser tests, 60s timeout"},
         "full": {"label": "Full: unit + browser tests, 120s timeout (Code Pipeline tests not included)"}}


def _page_html() -> str:
    src = (ROOT / "src" / "web" / "static" / "js" / "git-sync.js").read_text(encoding="utf-8")
    return f"""<!doctype html><html><head><meta charset="utf-8"></head><body>
<button id="btn-git-update"><span id="git-badge-pull"></span></button>
<button id="btn-git-publish"><span id="git-badge-push"></span></button>
<button id="btn-git-sync"><span id="git-badge-sync"></span></button>
<div class="overlay" id="git-sync-overlay">
  <div id="git-sync-title"></div>
  <button id="git-sync-minimize-btn"></button>
  <div id="git-sync-body"></div>
  <div class="modal-actions" id="git-sync-actions"></div>
</div>
<div id="git-sync-mini"><span id="git-sync-mini-label"></span><span id="git-sync-mini-spinner"></span>
  <button id="git-sync-mini-close"></button></div>
<script>
  window.__posts = [];
  window.__toasts = [];
  window.__syncResult = {{ok: true, messages: ['Pushed.'], git_status: {{ahead: 0, behind: 0, uncommitted: false}}}};
  window.__testDone = null;      // set by a test: the summary the fake run ends with
  function escHtml(s) {{ return String(s).replace(/[&<>"]/g, c => ({{'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}})[c]); }}
  function showToast(m) {{ window.__toasts.push(m); }}
  window.__stream = null;        // set by a test: a ReadableStream of step lines
  window.fetch = function (url, opts) {{
    var body = {{}};
    if (url === '/api/git-sync-stream' && window.__stream) {{
      window.__posts.push(JSON.parse(opts.body));
      return Promise.resolve(new Response(window.__stream, {{headers: {{'Content-Type': 'application/x-ndjson'}}}}));
    }}
    if (url === '/api/test-modes') body = {{modes: {json.dumps(MODES)}}};
    else if (url === '/api/git-sync') {{ window.__posts.push(JSON.parse(opts.body)); body = window.__syncResult; }}
    else if (url === '/api/git-status') body = {{ahead: 1, behind: 0, uncommitted: true, on_default: true}};
    return Promise.resolve({{ok: true, json: () => Promise.resolve(body)}});
  }};
  // The test run: ends at once with window.__testDone.
  function _streamTestRun(mode, h) {{ setTimeout(() => h.onDone(Object.assign({{mode: mode}}, window.__testDone), ['..F.']), 0); }}
</script>
<script>{src}</script>
<script>
  // Push path pieces that need a server: the streaming scan and the delays.
  _runStreamingScan = () => Promise.resolve({{ok: true, files_scanned: 3}});
  _applyGitStatus({{ahead: 1, behind: 0, uncommitted: true, on_default: true}});
</script>
</body></html>"""


@pytest.fixture(scope="module")
def browser():
    with shared_browser_scope() as b:
        yield b


@pytest.fixture
def page(browser):
    pg = browser.new_page(viewport={"width": 1100, "height": 700})
    pg.clock.install()   # the publish path's own pauses (300-400ms) run on _until
    pg.set_content(_page_html())
    yield pg
    pg.close()


def _until(page, expr, seconds=10):
    """Wait for ``expr``, firing the page's timers as they come due."""
    end = time.time() + seconds
    while not page.evaluate(expr):
        assert time.time() < end, f"never became true: {expr}"
        page.clock.run_for(100)


def _click(page, label):
    page.locator("#git-sync-actions button", has_text=label).first.click()


def _gate(page, mode):
    page.click(f"#gs-gate [data-gate={mode}]")


def test_the_dialog_has_the_summary_field_and_honest_mode_labels(page):
    """One Publish button; the test choice is Fast / Full / Skip, and the line
    under it states exactly what the picked mode runs."""
    page.evaluate("() => openGitPublish()")
    page.wait_for_selector("#git-publish-summary")
    assert "What changed?" in page.get_attribute("#git-publish-summary", "aria-label")
    hint = lambda: page.locator("#gs-gate-hint").text_content()
    assert hint() == MODES["fast"]["label"]
    _gate(page, "full")
    assert hint() == MODES["full"]["label"]
    _gate(page, "skip")
    assert hint() == "Skip: no tests run; the commit message says so."
    assert page.locator('#gs-gate [aria-checked="true"]').get_attribute("data-gate") == "skip"
    # The picked option is the grey raised segment, never the purple main button.
    assert page.locator("#gs-gate .pm-btn-primary").count() == 0
    assert page.locator("#gs-gate .on").get_attribute("data-gate") == "skip"
    assert [b.strip() for b in page.locator("#gs-gate button").all_text_contents()] == ["Fast", "Full", "Skip"]
    labels = [b.strip() for b in page.locator("#git-sync-actions button").all_text_contents()]
    assert labels == ["Cancel", "Publish"]


def test_skip_and_publish_sends_the_summary_and_a_skipped_gate(page):
    page.evaluate("() => openGitPublish()")
    page.wait_for_selector("#git-publish-summary")
    page.fill("#git-publish-summary", "  Fix the Sleep button  ")
    _gate(page, "skip")
    _click(page, "Publish")
    _until(page, "() => window.__posts.length === 1")
    post = page.evaluate("() => window.__posts[0]")
    assert post == {"action": "both", "confirm_branch": False,
                    "summary": "Fix the Sleep button", "gate": {"mode": "skipped"}}
    # The dialog shows the server's messages and the field state is reset.
    _until(page, "() => /Pushed\\./.test(document.getElementById('git-sync-body').textContent)")
    assert page.evaluate("() => [_publishSummary, _publishGate]") == ["", None]


def test_a_passing_run_records_its_counts_in_the_gate(page):
    page.evaluate("() => { window.__testDone = {ok: true, passed: 412, failed: 0, errors: 0, skipped: 3, seconds: 87, exit_code: 0}; }")
    page.evaluate("() => openGitPublish()")
    page.wait_for_selector("#git-publish-summary")
    _click(page, "Publish")   # Fast is the default
    _until(page, "() => window.__posts.length === 1")
    post = page.evaluate("() => window.__posts[0]")
    assert post["summary"] == ""
    assert post["gate"] == {"mode": "fast", "passed": 412, "skipped": 3, "failed": 0,
                            "seconds": 87, "anyway": False}


def test_publish_anyway_after_failures_marks_the_gate(page):
    page.evaluate("() => { window.__testDone = {ok: false, passed: 400, failed: 1, errors: 1, skipped: 0, seconds: 90, exit_code: 1}; }")
    page.evaluate("() => openGitPublish()")
    page.wait_for_selector("#git-publish-summary")
    page.fill("#git-publish-summary", "Risky")
    _gate(page, "full")
    _click(page, "Publish")
    _until(page, "() => /Not published/.test(document.getElementById('git-sync-title').textContent)")
    assert page.evaluate("() => window.__posts.length") == 0, "a failed gate must not publish by itself"
    _click(page, "Publish anyway")
    _until(page, "() => window.__posts.length === 1")
    post = page.evaluate("() => window.__posts[0]")
    assert post["summary"] == "Risky"
    assert post["gate"] == {"mode": "full", "passed": 400, "skipped": 0, "failed": 2,
                            "seconds": 90, "anyway": True}


def test_a_pull_that_installed_dependencies_says_so(page):
    page.evaluate("() => { window.__syncResult = {ok: true, messages: ['Pulled latest VibeNode updates from remote.', 'Installed 2 new or updated dependencies.'], pip_ran: true, pip_ok: true, pip_installed: 2, git_status: {ahead: 0, behind: 0}}; }")
    page.evaluate("() => executeGitAction('pull', 'btn-git-update', 'Update App')")
    _until(page, "() => window.__toasts.length === 1")
    assert page.evaluate("() => window.__toasts") == ["Installed 2 new dependencies"]
    assert "Installed 2 new or updated dependencies." in page.locator("#git-sync-body").text_content()


def test_a_failed_dependency_install_shows_pips_output(page):
    page.evaluate("() => { window.__syncResult = {ok: true, messages: ['Pulled latest VibeNode updates from remote.', 'Dependency install failed; see the pip output below.'], pip_ran: true, pip_ok: false, pip_installed: 0, pip_output_tail: 'ERROR: No matching distribution found for nope', git_status: {ahead: 0, behind: 0}}; }")
    page.evaluate("() => executeGitAction('pull', 'btn-git-update', 'Update App')")
    _until(page, "() => window.__toasts.length === 1")
    assert "Dependency install failed" in page.evaluate("() => window.__toasts[0]")
    _until(page, "() => /No matching distribution/.test(document.getElementById('git-sync-body').textContent)")


def test_a_clash_is_not_a_security_issue_and_offers_fix_with_ai(page):
    """An Update whose pull clashed with local changes must offer a way to
    get it fixed: the same Fix with AI as a scan finding, and the session it
    opens names the clashed files."""
    page.evaluate("""() => {
      window.__syncResult = {ok: false, conflicts: ['tests/test_card_answered.py'],
        messages: ['Pulled the update, but your local changes clashed with it in: tests/test_card_answered.py.',
                   'Nothing was published.']};
      window.__fix = null;
      _startFixSession = (title, preview, prompt) => { window.__fix = {title, prompt}; };
    }""")
    page.evaluate("() => executeGitAction('both', 'btn-git-publish', 'Publish App Update')")
    _until(page, "() => /changes clash/i.test(document.getElementById('git-sync-title').textContent)")
    assert "Security" not in page.locator("#git-sync-title").text_content()
    _click(page, "Fix with AI")
    fix = page.evaluate("() => window.__fix")
    assert fix["title"] == "Resolve Update Clash"
    assert "tests/test_card_answered.py" in fix["prompt"]
    assert "Do NOT make new commits or push" in fix["prompt"]
    assert "git pull --rebase" not in fix["prompt"]   # a stash clash: markers are on disk


def test_a_clash_in_the_pull_itself_tells_the_session_to_redo_the_pull(page):
    """The pull was undone (src/web/git_ops.py `pull_clash`), so there are no
    markers yet: the session must redo the pull, and never with -X theirs."""
    page.evaluate("""() => {
      window.__syncResult = {ok: false, conflicts: ['src/web/static/js/last-card.js'], pull_clash: true,
        messages: ['The update clashes with your saved changes in: static/js/last-card.js.']};
      window.__fix = null;
      _startFixSession = (title, preview, prompt) => { window.__fix = {title, prompt}; };
    }""")
    page.evaluate("() => executeGitAction('both', 'btn-git-publish', 'Publish App Update')")
    _until(page, "() => /changes clash/i.test(document.getElementById('git-sync-title').textContent)")
    _click(page, "Fix with AI")
    prompt = page.evaluate("() => window.__fix.prompt")
    assert "git pull --rebase" in prompt and "never with -X theirs" in prompt


def test_publishing_fills_the_ring_step_by_step_and_never_spins(page):
    """The server names its steps and says when each starts
    (/api/git-sync-stream); the dialog shows the step, "Step i of n", and a
    ring that has moved, never a spinning ring."""
    page.evaluate("""() => {
      const enc = new TextEncoder();
      window.__push = null;
      window.__stream = new ReadableStream({start(c) { window.__push = l => c.enqueue(enc.encode(JSON.stringify(l) + String.fromCharCode(10))); window.__end = () => c.close(); }});
    }""")
    page.evaluate("() => { executeGitAction('push', 'btn-git-publish', 'Publish App Update'); }")   # not awaited: it ends when the stream does
    _until(page, "() => window.__push && window.__posts.length === 1")
    page.evaluate("""() => {
      __push({type: 'steps', steps: [{key: 'scan', label: 'Checking files for keys'},
        {key: 'save', label: 'Saving a new version'}, {key: 'upload', label: 'Uploading'}]});
      __push({type: 'step', key: 'save'});
    }""")
    _until(page, "() => /Saving a new version/.test(document.getElementById('sync-step-label').textContent)")
    assert page.locator("#scan-progress-text").text_content() == "Step 2 of 3"
    page.clock.run_for(3000)
    full = 2 * 3.141592653589793 * 52
    off = float(page.get_attribute("#gs-ring", "stroke-dashoffset"))
    assert full * (1 - 2 / 3) < off < full * (1 - 1 / 3), "the ring sits inside step 2's share"
    assert page.evaluate("() => document.querySelectorAll('.gs-spin').length") == 0
    # Minimized, the pill shows the same progress: a filled ring and a percentage, no spinner.
    page.evaluate("() => minimizeGitSyncModal()")
    page.clock.run_for(600)
    pct = page.locator("#git-sync-mini-pct").text_content()
    assert pct.endswith("%") and 34 <= int(pct[:-1]) < 67, pct
    assert page.locator("#git-sync-mini-spinner.working").count() == 0
    mini_off = float(page.get_attribute("#git-sync-mini-ring", "stroke-dashoffset"))
    mini_full = 2 * 3.141592653589793 * 8
    assert abs((1 - mini_off / mini_full) - (1 - off / full)) < 0.1
    page.evaluate("""() => { __push({type: 'done', result: {ok: true, messages: ['Pushed.'],
      git_status: {ahead: 0, behind: 0, uncommitted: false}}}); __end(); }""")
    _until(page, "() => /Published/.test(document.getElementById('git-sync-mini-label').textContent)")
    assert page.locator("#git-sync-mini-spinner.done").count() == 1


def test_the_publish_dialog_code_refreshes_in_an_open_tab():
    src = (ROOT / "src" / "web" / "static" / "js" / "live-assets.js").read_text(encoding="utf-8")
    assert "'js/git-sync.js'" in src
