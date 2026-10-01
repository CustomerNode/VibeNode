/* test-runner.js — regression test runner UI (System menu) */

let _testRunning = false;

function runTests(mode) {
  if (_testRunning) {
    showGitSyncModal('Tests Running', '<p style="color:var(--text-muted)">Tests are already running. Cancel the current run first.</p>', [
      {label: 'Cancel Tests', primary: false, onclick: () => { cancelTests(); }},
      {label: 'OK', onclick: closeGitSyncModal}
    ]);
    return;
  }

  const label = mode === 'fast' ? 'Run Tests (Fast)' : 'Run Tests (Full)';
  const desc = mode === 'fast'
    ? 'Running unit and API tests...'
    : 'Running full test suite including e2e...';

  _testRunning = true;

  // Show the animated modal
  showGitSyncModal(label, _testRunnerHtml(desc), []);

  let lines = [];

  // POST to start the test run
  fetch('/api/run-tests', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({mode})
  }).then(res => {
    const reader = res.body.getReader();
    const decoder = new TextDecoder();
    let buffer = '';

    function read() {
      reader.read().then(({done, value}) => {
        if (done) {
          _testRunning = false;
          return;
        }
        buffer += decoder.decode(value, {stream: true});
        const parts = buffer.split('\n\n');
        buffer = parts.pop(); // keep incomplete chunk

        for (const part of parts) {
          if (!part.startsWith('data: ')) continue;
          try {
            const d = JSON.parse(part.slice(6));
            if (d.type === 'line') {
              lines.push(d.line);
              _updateTestOutput(lines);
            } else if (d.type === 'done') {
              _showTestResults(label, d, lines);
            } else if (d.type === 'error') {
              _showTestError(label, d.line);
            }
          } catch(_) {}
        }
        read();
      }).catch(() => { _testRunning = false; });
    }
    read();
  }).catch(e => {
    _testRunning = false;
    _showTestError(label, 'Could not start tests: ' + e.message);
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
}

function _showTestResults(label, summary, lines) {
  _testRunning = false;

  let body;
  const btns = [];

  if (summary.ok) {
    body = '<div style="text-align:center;padding:16px 0;">'
      + '<div style="font-weight:600;color:var(--accent-green,#4ecdc4);font-size:16px;">All Tests Passed</div>'
      + '<div style="font-size:13px;color:var(--text-muted);margin-top:6px;">'
      + summary.passed + ' passed'
      + (summary.skipped ? ', ' + summary.skipped + ' skipped' : '')
      + '</div></div>';
    btns.push({label: 'OK', primary: true, onclick: closeGitSyncModal});
  } else {
    // Show failure summary + scrollable output
    body = '<div style="text-align:center;padding:8px 0;">'
      + '<div style="font-weight:600;color:var(--result-err,#ff4444);font-size:16px;">'
      + summary.failed + ' Test' + (summary.failed !== 1 ? 's' : '') + ' Failed</div>'
      + '<div style="font-size:13px;color:var(--text-muted);margin-top:4px;">'
      + summary.passed + ' passed, ' + summary.failed + ' failed'
      + (summary.errors ? ', ' + summary.errors + ' errors' : '')
      + (summary.skipped ? ', ' + summary.skipped + ' skipped' : '')
      + '</div></div>';

    // Show the failure lines
    const failLines = lines.filter(l =>
      l.startsWith('FAILED') || l.startsWith('ERROR') || l.includes('AssertionError') || l.includes('assert ')
    );
    if (failLines.length > 0) {
      body += '<pre style="text-align:left;font-size:10px;font-family:monospace;'
        + 'max-height:250px;overflow-y:auto;background:rgba(255,60,60,0.06);'
        + 'border:1px solid rgba(255,60,60,0.15);border-radius:6px;'
        + 'padding:8px 10px;margin-top:10px;color:var(--text-secondary);'
        + 'white-space:pre-wrap;word-break:break-all;">'
        + failLines.map(l => _escTestHtml(l)).join('\n')
        + '</pre>';
    }

    btns.push({label: 'OK', onclick: closeGitSyncModal});
  }

  showGitSyncModal(
    summary.ok ? label + ' \u2713' : label + ' \u2014 Failures Found',
    body,
    btns
  );
}

function _showTestError(label, msg) {
  _testRunning = false;
  showGitSyncModal(label + ' \u2014 Error',
    '<p style="color:var(--result-err)">' + _escTestHtml(msg) + '</p>',
    [{label: 'OK', onclick: closeGitSyncModal}]);
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
