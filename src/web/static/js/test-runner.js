/* test-runner.js — regression test runner UI (System menu) */

let _testRunning = false;

/**
 * Stream a test run from /api/run-tests, surviving dropped connections.
 *
 * The run lives on the server (src/web/routes/test_api.py); this stream only
 * watches it.  When the connection drops (a phone locks its screen, the tab
 * is backgrounded, the network changes) the stream ends without a "done"
 * event; this then REATTACHES with the run id and the number of lines
 * already seen, and gets the rest, including the result if the run finished
 * meanwhile.  It retries at once when the tab becomes visible again, so a
 * dropout never leaves the dialog on "running" forever.
 *
 * handlers: onLine(lines), onDone(summary, lines), onError(message, {lost, started})
 */
function _streamTestRun(mode, handlers) {
  const lines = [];
  let runId = null;
  let finished = false;
  let failures = 0;
  let retryTimer = null;
  let retryOwed = false;           // a retry waiting for the tab to be visible
  const MAX_FAILURES = 8;          // ~2 minutes of backoff while visible

  function finish(fn) {
    if (finished) return;
    finished = true;
    if (retryTimer) clearTimeout(retryTimer);
    document.removeEventListener('visibilitychange', onVisible);
    fn();
  }

  function onVisible() {
    if (!finished && document.visibilityState === 'visible' && (retryTimer || retryOwed)) {
      if (retryTimer) clearTimeout(retryTimer);
      retryTimer = null;
      retryOwed = false;
      connect();
    }
  }
  document.addEventListener('visibilitychange', onVisible);

  function scheduleRetry() {
    if (finished) return;
    if (!runId) {
      // Never got a run id: the run may not exist at all.
      finish(() => handlers.onError('Could not start tests.', {lost: false, started: false}));
      return;
    }
    if (document.visibilityState === 'hidden') {
      // Locked phone / background tab: wait for the user to come back
      // (onVisible) instead of burning retries the browser may not run.
      retryOwed = true;
      return;
    }
    failures++;
    if (failures > MAX_FAILURES) {
      finish(() => handlers.onError('Lost the connection to the test run.', {lost: true, started: true}));
      return;
    }
    const delay = Math.min(15000, 1000 * Math.pow(2, failures - 1));
    retryTimer = setTimeout(() => { retryTimer = null; connect(); }, delay);
  }

  function connect() {
    const body = runId ? {mode, run_id: runId, since: lines.length} : {mode};
    fetch('/api/run-tests', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify(body)
    }).then(res => {
      const reader = res.body.getReader();
      const decoder = new TextDecoder();
      let buffer = '';
      function read() {
        reader.read().then(({done, value}) => {
          if (done) { scheduleRetry(); return; }
          buffer += decoder.decode(value, {stream: true});
          const parts = buffer.split('\n\n');
          buffer = parts.pop();
          for (const part of parts) {
            if (!part.startsWith('data: ')) continue;   // keepalive comments
            let d;
            try { d = JSON.parse(part.slice(6)); } catch (_) { continue; }
            if (d.type === 'run') {
              runId = d.run_id;
              failures = 0;            // reached the server: reset the backoff
            } else if (d.type === 'line') {
              lines.push(d.line);
              handlers.onLine(lines);
            } else if (d.type === 'done') {
              finish(() => handlers.onDone(d, lines));
              return;
            } else if (d.type === 'error') {
              finish(() => handlers.onError(d.line, {lost: !!d.lost, started: !!runId, busy: !!d.busy}));
              return;
            }
          }
          read();
        }).catch(scheduleRetry);
      }
      read();
    }).catch(scheduleRetry);
  }
  connect();
}

function runTests(mode) {
  if (_testRunning) {
    showGitSyncModal('Tests already running', '<p>Tests are already running. Cancel the current run first.</p>', [
      {label: 'Cancel tests', onclick: () => { cancelTests(); }},
      {label: 'OK', primary: true, onclick: closeGitSyncModal}
    ]);
    return;
  }

  const label = mode === 'fast' ? 'Fast tests' : 'Full tests';
  const desc = mode === 'fast'
    ? 'Running unit and API tests...'
    : 'Running the full test suite with the longer per-test timeout...';

  _testRunning = true;

  // Show the animated modal
  showGitSyncModal('Running ' + label.toLowerCase(), _testRunnerHtml(desc), [], {center: true});

  _streamTestRun(mode, {
    onLine: lines => _updateTestOutput(lines),
    onDone: (summary, lines) => _showTestResults(label, summary, lines),
    onError: (msg, info) => {
      if (info && info.lost) {
        _testRunning = false;
        showGitSyncModal(label + ' interrupted',
          '<p>' + _escTestHtml(msg) + ' Its result was not received.</p>',
          [{label: 'Close', onclick: closeGitSyncModal},
           {label: 'Run again', primary: true, onclick: () => { closeGitSyncModal(); runTests(mode); }}],
          {tone: 'bad'});
      } else {
        _showTestError(label, msg);
      }
    },
  });
}

// Ring geometry for the progress circle (r=52 in a 120x120 box).
const _TRUN_RING_R = 52;
const _TRUN_RING_C = 2 * Math.PI * _TRUN_RING_R;

function _testRunnerHtml(desc) {
  return '<div class="trun">'
    + '<svg class="trun-ring" viewBox="0 0 120 120" aria-hidden="true">'
    +   '<circle class="trun-ring-track" cx="60" cy="60" r="' + _TRUN_RING_R + '"/>'
    +   '<circle class="trun-ring-fill" id="test-progress-ring" cx="60" cy="60" r="' + _TRUN_RING_R + '"'
    +     ' stroke-dasharray="' + _TRUN_RING_C.toFixed(2) + '" stroke-dashoffset="' + _TRUN_RING_C.toFixed(2) + '"/>'
    + '</svg>'
    + '<div class="trun-pct" id="test-progress-pct">0%</div>'
    + '<div class="trun-desc" id="test-status">' + desc + '</div>'
    + '<div class="trun-counts" id="test-progress-counts">Starting…</div>'
    + '</div>';
}

// pytest -q progress rows: a run of result characters, optionally followed by
// "[ NN%]".  Rows are sized for an 80-column terminal, wider than the dialog,
// so they are summarized as a progress ring + counts instead of shown raw.
// A run of result characters that ENDS at a boundary: end of line, space,
// "[", or "_" (daemon log lines start with "_").  The boundary keeps words
// such as "Exception" or "FAILED" from being read as results.
const _TRUN_RESULT_RUN = /^[.sFEx]+(?=$|\s|\[|_)/;
// Not anchored to the end: daemon log text can print right after "[100%]".
const _TRUN_PCT = /\[\s*(\d+)%\]/;

/** Fold raw pytest output into {pct, passed, failed, errors, skipped, other}. */
function _summarizeTestProgress(lines) {
  let pct = 0, passed = 0, failed = 0, errors = 0, skipped = 0;
  const other = [];
  for (const raw of lines) {
    const line = String(raw || '');
    const m = line.match(_TRUN_PCT);
    if (m) pct = Math.min(100, parseInt(m[1], 10));
    const run = line.match(_TRUN_RESULT_RUN);
    if (run) {
      // A progress row (daemon log text sometimes prints mid-row; count only
      // the leading result characters and treat any trailing text as output).
      for (const ch of run[0]) {
        if (ch === '.') passed++;
        else if (ch === 'F') failed++;
        else if (ch === 'E') errors++;
        else if (ch === 's' || ch === 'x') skipped++;
      }
      const rest = line.slice(run[0].length).replace(_TRUN_PCT, '').trim();
      if (rest) other.push(rest);
    } else if (line.trim()) {
      other.push(line);
    }
  }
  return {pct, passed, failed, errors, skipped, other};
}

function _updateTestOutput(lines) {
  const s = _summarizeTestProgress(lines);
  const ring = document.getElementById('test-progress-ring');
  const pct = document.getElementById('test-progress-pct');
  const counts = document.getElementById('test-progress-counts');
  const bad = s.failed + s.errors > 0;
  if (ring) {
    ring.setAttribute('stroke-dashoffset', (_TRUN_RING_C * (1 - s.pct / 100)).toFixed(2));
    ring.classList.toggle('trun-ring-bad', bad);
  }
  if (pct) {
    pct.textContent = s.pct + '%';
    pct.classList.toggle('trun-bad', bad);
  }
  if (counts) {
    const parts = [s.passed.toLocaleString() + ' passed'];
    if (s.failed) parts.push('<span class="trun-bad">' + s.failed + ' failed</span>');
    if (s.errors) parts.push('<span class="trun-bad">' + s.errors + (s.errors === 1 ? ' error' : ' errors') + '</span>');
    if (s.skipped) parts.push(s.skipped + ' skipped');
    counts.innerHTML = parts.join(' · ');
  }
  // Before the first test runs, the suite may be waiting for another
  // session's parallel run (one run at a time, tests/_run_lock.py), or it
  // may have reused a recorded run.  Those lines are the only output then;
  // without them the dialog would show only a spinner.
  const status = document.getElementById('test-status');
  if (status && !s.passed && !s.failed && !s.errors) {
    const note = s.other.filter(l => /^\[tests\] |^Reused the full run|^A\/B tests reused/.test(l)).pop();
    if (note) status.textContent = note.replace(/\s*\(VIBENODE_[A-Z_]+=0 skips the wait\)\.?$/, '.');
  }
}

function _showTestResults(label, summary, lines) {
  _testRunning = false;

  let body;
  const btns = [];

  if (summary.ok) {
    // Reuse (src/web/gate_reuse.py): say when the run was not repeated.
    const reuse = summary.reused_from
      ? ' Reused the full run that passed at ' + escHtml(summary.reused_from) + ' on identical files.'
      : (summary.ab_reused_from
        ? ' A/B tests reused from ' + escHtml(summary.ab_reused_from) + ': nothing they depend on changed.'
        : '');
    body = _gsRing({pct: 100, tone: 'ok', big: (summary.passed || 0).toLocaleString(), desc: 'tests passed',
      counts: (summary.skipped ? summary.skipped + ' skipped.' : '') + reuse || null});
    btns.push({label: 'Done', primary: true, onclick: closeGitSyncModal});
  } else {
    body = _gsRing({pct: 100, tone: 'bad', big: (summary.failed || 0) + ' failed',
      desc: summary.passed + ' passed, ' + summary.failed + ' failed'
        + (summary.errors ? ', ' + summary.errors + ' errors' : '')
        + (summary.skipped ? ', ' + summary.skipped + ' skipped' : '')});

    // Show the failure lines
    const failLines = lines.filter(l =>
      l.startsWith('FAILED') || l.startsWith('ERROR') || l.includes('AssertionError') || l.includes('assert ')
    );
    if (failLines.length > 0) {
      body += '<pre class="gs-pre">' + failLines.map(l => _escTestHtml(l)).join('\n') + '</pre>';
    }

    btns.push({label: 'Done', primary: true, onclick: closeGitSyncModal});
  }

  showGitSyncModal(summary.ok ? 'All tests passed' : 'Tests failed', body, btns,
    {center: true, tone: summary.ok ? 'ok' : 'bad', sticky: !summary.ok});
}

function _showTestError(label, msg) {
  _testRunning = false;
  showGitSyncModal('Tests could not run',
    '<p>' + _escTestHtml(msg) + '</p>',
    [{label: 'OK', primary: true, onclick: closeGitSyncModal}], {tone: 'bad'});
}

function cancelTests() {
  fetch('/api/cancel-tests', {method: 'POST'}).then(() => {
    _testRunning = false;
    closeGitSyncModal();
  }).catch(() => {});
}

function _escTestHtml(s) {
  const d = document.createElement('div');
  d.textContent = s;
  return d.innerHTML;
}
