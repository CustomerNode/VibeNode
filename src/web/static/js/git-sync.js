/* git-sync.js — git status polling and sync actions */

let _gitStatus = {};

// Drawn icons for the three states (were the text characters ↓ ↑ ↓↑ in a badge).
const _GIT_ICO = (d) => '<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.1" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">' + d + '</svg>';
const _GIT_ICONS = {
  pull: _GIT_ICO('<path d="M12 4v11"/><path d="m7 10.5 5 5 5-5"/><path d="M5 20h14"/>'),      // down to a line: there is something to bring in
  push: _GIT_ICO('<path d="M12 20V9"/><path d="m7 13.5 5-5 5 5"/><path d="M5 4h14"/>'),         // up to a line: there is something to send out
  sync: _GIT_ICO('<path d="M7 4v15"/><path d="m3.5 15.5 3.5 3.5 3.5-3.5"/><path d="M17 20V5"/><path d="m13.5 8.5 3.5-3.5 3.5 3.5"/>'),
};

/** One state button: icon, a count where there is one, and a tooltip that says exactly what is waiting. */
function _setGitBtn(btn, badgeId, kind, count, tip) {
  document.getElementById(badgeId).innerHTML = _GIT_ICONS[kind];
  if (count > 0) btn.dataset.count = String(count); else delete btn.dataset.count;
  btn.title = tip;
  btn.style.display = 'inline-flex';
}

function _applyGitStatus(s) {
  _gitStatus = s;
  const hasPush = s.ahead > 0 || s.uncommitted;
  const hasPull = s.behind > 0;
  const btnUpdate  = document.getElementById('btn-git-update');
  const btnPublish = document.getElementById('btn-git-publish');
  const btnSync    = document.getElementById('btn-git-sync');
  const n = (k, one, many) => k + ' ' + (k === 1 ? one : many);
  const pullTip = n(s.behind || 0, 'update', 'updates') + ' available';
  const pushTip = s.ahead > 0
    ? n(s.ahead, 'change', 'changes') + ' to publish' + (s.uncommitted ? ', plus unsaved edits' : '')
    : 'Unsaved edits to publish';
  if (hasPull && hasPush) {
    // Both directions — show single Sync button
    btnUpdate.style.display = 'none';
    btnPublish.style.display = 'none';
    _setGitBtn(btnSync, 'git-badge-sync', 'sync', 0, pullTip + ' \u00b7 ' + pushTip);
  } else {
    btnSync.style.display = 'none';
    if (hasPull) _setGitBtn(btnUpdate, 'git-badge-pull', 'pull', s.behind, pullTip);
    else btnUpdate.style.display = 'none';
    if (hasPush) _setGitBtn(btnPublish, 'git-badge-push', 'push', s.ahead, pushTip);
    else btnPublish.style.display = 'none';
  }
}

async function pollGitStatus() {
  try {
    const res = await fetch('/api/git-status');
    const s = await res.json();
    _applyGitStatus(s);
  } catch(e) {}
}

// Arm the approval-notification capability on any git-button press. This is
// the reliable "user gesture on their phone" moment for iOS Safari — pressing
// the Update button unlocks the WebAudio context and opportunistically
// requests Notification permission, so later approval pings can actually
// make sound. Idempotent, safe to call every time. See notify.js.
function _armNotifyOnGesture() {
  try {
    if (window.VNNotify && typeof window.VNNotify.arm === 'function') {
      window.VNNotify.arm();
    }
  } catch (e) {}
}

// ── What goes into the commit ──
// The commit message is the user's one-line "What changed" (auto-generated from
// the staged areas when left empty) plus a "Publish gate" line recording which
// test mode ran and how it went, or that it was skipped.  Both are read here
// and sent with the /api/git-sync request (src/web/git_ops.py build_commit_message).
let _publishSummary = '';
let _publishGate = null;       // {mode, passed, skipped, failed, seconds, anyway}

// What each test mode really runs, from /api/test-modes (src/web/routes/test_api.py
// MODES).  The fallback matches it; both modes run the same files, and only
// the per-test timeout differs.  The Code Pipeline tests never run here.
const _TEST_MODE_FALLBACK = {
  fast: {label: 'Fast: unit + browser tests, 60s timeout (Code Pipeline tests not included)'},
  full: {label: 'Full: unit + browser tests, 120s timeout (Code Pipeline tests not included)'},
};
let _testModesCache = null;
async function _testModes() {
  if (_testModesCache) return _testModesCache;
  try {
    const r = await fetch('/api/test-modes');
    const d = r.ok ? await r.json() : null;
    if (d && d.modes && d.modes.fast && d.modes.full) _testModesCache = d.modes;
  } catch (_) {}
  return _testModesCache || _TEST_MODE_FALLBACK;
}

// ── The dialog's parts ──
// Every screen is built from the shared dialog parts (.pm-*) and the test ring
// (.trun, test-runner.js), so it matches Preferences and Recovery & Watchdog.
const _GS_RC = 2 * Math.PI * 52;
const _GS_ICON = {
  check: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.6" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="m5 12.5 4.5 4.5L19 7.5"/></svg>',
  x: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.6" stroke-linecap="round" aria-hidden="true"><path d="m7 7 10 10M17 7 7 17"/></svg>',
};

/** The ring with a big line, a description and a count line under it.
 *  o: {pct, tone: 'ok'|'bad'|'', big, mark, desc, counts, file}
 *  mark: an icon drawn inside the ring (a finished result). */
function _gsRing(o) {
  const off = (_GS_RC * (1 - (o.pct || 0) / 100)).toFixed(2);
  const ring = o.tone === 'ok' ? ' trun-ring-ok' : o.tone === 'bad' ? ' trun-ring-bad' : '';
  const big = o.tone === 'ok' ? ' trun-ok' : o.tone === 'bad' ? ' trun-bad' : '';
  return '<div class="trun"><div class="gs-ringwrap">'
    + '<svg class="trun-ring" id="gs-ring-svg" viewBox="0 0 120 120" aria-hidden="true">'
    +   '<circle class="trun-ring-track" cx="60" cy="60" r="52"/>'
    +   '<circle class="trun-ring-fill' + ring + '" id="gs-ring" cx="60" cy="60" r="52"'
    +     ' stroke-dasharray="' + _GS_RC.toFixed(2) + '" stroke-dashoffset="' + off + '"/>'
    + '</svg>'
    + (o.mark ? '<div class="gs-mark' + big + '">' + o.mark + '</div>' : '')
    + '</div>'
    + '<div class="trun-pct' + big + '" id="gs-big">' + (o.big || '') + '</div>'
    + (o.desc != null ? '<div class="trun-desc" id="sync-step-label">' + o.desc + '</div>' : '')
    + (o.counts != null ? '<div class="trun-counts" id="scan-progress-text">' + o.counts + '</div>' : '')
    + (o.file ? '<div class="gs-file" id="scan-file-name"></div>' : '')
    + '</div>';
}

/** Move the ring to a known percentage. */
function _gsProgress(pct) {
  _gsStopCreep();
  _gsFill(pct);
  const big = document.getElementById('gs-big');
  if (big) big.textContent = Math.round(pct) + '%';
}

function _gsFill(pct) {
  const fill = document.getElementById('gs-ring');
  if (fill) fill.setAttribute('stroke-dashoffset', (_GS_RC * (1 - Math.max(0, Math.min(100, pct)) / 100)).toFixed(2));
}

// ── The git steps: the ring fills step by step, never spins ──
// The server names its steps up front and says when each starts
// (/api/git-sync-stream).  Within a step the ring moves slowly toward the
// step's end and never reaches it, so it always shows where the run is and
// that it is still moving.  A server without the stream gives one step.
let _gsSteps = null;     // [{key, label}]
let _gsCreepTimer = null;

function _gsStopCreep() {
  if (_gsCreepTimer) { clearInterval(_gsCreepTimer); _gsCreepTimer = null; }
}

/** Show step `i` of the run: its name, "Step i of n", and the ring moving through it. */
function _gsStep(i, label) {
  const n = (_gsSteps && _gsSteps.length) || 1;
  const from = 100 * i / n, span = 100 / n;
  const el = document.getElementById('sync-step-label');
  const text = document.getElementById('scan-progress-text');
  const fname = document.getElementById('scan-file-name');
  const big = document.getElementById('gs-big');
  if (el) el.textContent = label;
  if (text) text.textContent = n > 1 ? 'Step ' + (i + 1) + ' of ' + n : '';
  if (fname) fname.textContent = '';
  if (big) big.textContent = '';
  _gsStopCreep();
  _gsFill(from);
  const t0 = Date.now();
  // Moves 90% of the way through the step's share, quickly at first; a step
  // that runs long still moves, more slowly.
  _gsCreepTimer = setInterval(() => {
    if (!document.getElementById('gs-ring')) return _gsStopCreep();
    _gsFill(from + span * 0.9 * (1 - Math.exp(-(Date.now() - t0) / 5000)));
  }, 120);
  _gitSyncMiniLabel = label;
  if (_gitSyncMinimized) {
    const miniLabel = document.getElementById('git-sync-mini-label');
    if (miniLabel) miniLabel.textContent = label;
  }
}

/** Run the sync, showing each step the server reports.  Returns its result. */
async function _gsRunSync(payload) {
  const req = {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(payload)};
  _gsSteps = null;
  let res = null;
  try { res = await fetch('/api/git-sync-stream', req); } catch (_) { res = null; }
  const ndjson = res && res.ok && res.body && res.headers
    && /ndjson/.test(res.headers.get('content-type') || '');
  if (!ndjson) {
    // A web server from before the stream: one step, the same request.
    _gsSteps = [{key: 'all', label: payload.action === 'pull' ? 'Pulling in updates' : 'Saving and uploading'}];
    _gsStep(0, _gsSteps[0].label);
    const r = await (await fetch('/api/git-sync', req)).json();
    _gsStopCreep();
    return r;
  }
  const reader = res.body.getReader();
  const dec = new TextDecoder();
  let buf = '', result = null;
  for (;;) {
    const {value, done} = await reader.read();
    if (value) buf += dec.decode(value, {stream: true});
    let nl;
    while ((nl = buf.indexOf('\n')) >= 0) {
      const line = buf.slice(0, nl).trim();
      buf = buf.slice(nl + 1);
      if (!line) continue;
      let ev;
      try { ev = JSON.parse(line); } catch (_) { continue; }
      if (ev.type === 'steps') {
        _gsSteps = ev.steps || [];
      } else if (ev.type === 'step') {
        const i = (_gsSteps || []).findIndex(s => s.key === ev.key);
        if (i >= 0) _gsStep(i, _gsSteps[i].label);
      } else if (ev.type === 'done') {
        result = ev.result;
      }
    }
    if (done) break;
  }
  _gsStopCreep();
  if (!result) throw new Error('The sync ended without a result');
  return result;
}

/** Words for each action, from the button label the callers pass. */
function _gsWords(label) {
  if (/^Sync/.test(label)) return {ing: 'Syncing', ed: 'Synced', not: 'Not synced', verb: 'sync', Verb: 'Sync'};
  if (/^Update/.test(label)) return {ing: 'Updating', ed: 'Updated', not: 'Not updated', verb: 'update', Verb: 'Update'};
  return {ing: 'Publishing', ed: 'Published', not: 'Not published', verb: 'publish', Verb: 'Publish'};
}

/** How many things the scan stopped on, for the big line. */
function _gsFound(scan) {
  const n = ((scan && scan.findings) || []).length + ((scan && scan.blocked_files) || []).length;
  return (n || 1) + ' found';
}

/** What changed, and which tests run first: Fast, Full or Skip. */
function _gateChoiceHtml(modes, verb) {
  const hint = {fast: modes.fast.label, full: modes.full.label, skip: 'Skip: no tests run; the commit message says so.'};
  return '<input id="git-publish-summary" class="pm-input" type="text" maxlength="120" autocomplete="off" '
    + 'aria-label="What changed? (one line, optional)" placeholder="What changed? Optional, written for you if empty">'
    + '<div class="gs-label">Tests before ' + verb + '</div>'
    // A segmented switch: the picked one is raised in grey, so it never looks
    // like the purple Publish button below it.
    + '<div class="gs-seg" id="gs-gate" role="radiogroup" aria-label="Tests before ' + verb + '">'
    + [['fast', 'Fast'], ['full', 'Full'], ['skip', 'Skip']].map(([k, l], i) =>
        '<button type="button" role="radio" aria-checked="' + (i === 0) + '" data-gate="' + k + '"'
        + ' data-hint="' + escHtml(hint[k]) + '" class="' + (i === 0 ? 'on' : '') + '"'
        + ' onclick="_gsPickGate(this)">' + l + '</button>').join('')
    + '</div><div class="gs-hint" id="gs-gate-hint">' + escHtml(hint.fast) + '</div>';
}

function _gsPickGate(btn) {
  btn.parentElement.querySelectorAll('[data-gate]').forEach(b => {
    const on = b === btn;
    b.classList.toggle('on', on);
    b.setAttribute('aria-checked', String(on));
  });
  const hint = document.getElementById('gs-gate-hint');
  if (hint) hint.textContent = btn.dataset.hint || '';
}

function _gsSelectedGate() {
  const on = document.querySelector('#gs-gate [aria-checked="true"]');
  return on ? on.dataset.gate : 'fast';
}

/** Run the chosen gate, then the git action. */
function _gsGo(actionType, actionLabel, btnId) {
  _readSummaryField();
  const mode = _gsSelectedGate();
  if (mode === 'skip') {
    _publishGate = {mode: 'skipped'};
    executeGitAction('both', btnId, actionLabel);
  } else {
    _testThenAction(mode, actionType, actionLabel, btnId);
  }
}

/** Read the summary field before the dialog body is replaced. */
function _readSummaryField() {
  const el = document.getElementById('git-publish-summary');
  _publishSummary = el ? String(el.value || '').trim().slice(0, 120) : '';
}

async function openGitPublish() {
  _armNotifyOnGesture();
  const s = _gitStatus;
  const hasPush = s.ahead > 0 || s.uncommitted;
  if (!hasPush) {
    showGitSyncModal('Nothing to publish', '<p>Your app is already up to date on remote.</p>',
      [{label: 'OK', primary: true, onclick: closeGitSyncModal}]);
    return;
  }
  // Branch guard FIRST — before any publish/test options are shown.
  if (s.on_default === false) {
    _showBranchWarning('both', 'btn-git-publish', 'Publish App Update', s);
    return;
  }
  const what = s.ahead > 0
    ? '<strong>' + s.ahead + ' change' + (s.ahead === 1 ? '' : 's') + '</strong>' + (s.uncommitted ? ' plus unsaved edits' : '')
    : 'Your unsaved edits';
  let body = '<p>' + what + ' will be saved, checked for keys and personal details, then uploaded.'
    + (s.behind > 0 ? ' ' + s.behind + ' remote update' + (s.behind === 1 ? ' is' : 's are') + ' pulled in first.' : '')
    + '</p>';
  body += _gateChoiceHtml(await _testModes(), 'publishing');
  _publishGate = null;
  showGitSyncModal('Publish your changes', body, [
    {label: 'Cancel', onclick: closeGitSyncModal},
    {label: 'Publish', primary: true, onclick: () => _gsGo('publish', 'Publish App Update', 'btn-git-publish')},
  ]);
}

function _testThenPublish(mode) {
  _testThenAction(mode, 'publish', 'Publish App Update', 'btn-git-publish');
}

async function openGitSyncBoth() {
  _armNotifyOnGesture();
  const s = _gitStatus;
  // Branch guard FIRST — before any sync/test options are shown.
  if (s.on_default === false) {
    _showBranchWarning('both', 'btn-git-sync', 'Sync App', s);
    return;
  }
  const out = s.ahead + (s.uncommitted ? 1 : 0);
  const body = '<p><strong>' + s.behind + ' update' + (s.behind === 1 ? '' : 's') + '</strong> to pull in and '
    + '<strong>' + out + ' change' + (out === 1 ? '' : 's') + '</strong> to publish. Updates come in first; '
    + 'if a line changed on both sides, it stops and asks.</p>'
    + _gateChoiceHtml(await _testModes(), 'syncing');
  _publishGate = null;
  showGitSyncModal('Sync VibeNode', body, [
    {label: 'Cancel', onclick: closeGitSyncModal},
    {label: 'Sync', primary: true, onclick: () => _gsGo('sync', 'Sync App', 'btn-git-sync')},
  ]);
}

function _testThenSync(mode) {
  _testThenAction(mode, 'sync', 'Sync App', 'btn-git-sync');
}

/**
 * Shared helper: run tests, then execute a git action if they pass.
 * @param {string} mode - "fast" or "full"
 * @param {string} actionType - "publish" or "sync" (for labels)
 * @param {string} actionLabel - display label for the git action
 * @param {string} btnId - button ID for executeGitAction
 */
// Cap on traceback text embedded in the fix prompt.  The full output is always
// in the saved log file, which the fix session is told to read.
const _TEST_FIX_DETAIL_CAP = 15000;

/**
 * Turn raw pytest -q output into what the failure dialog and the fix session
 * need: the FAILED/ERROR lines, the traceback sections, the run's tail (for
 * runs that died without a summary, e.g. a timeout abort), a copyable text
 * blob, and a ready-to-send prompt.
 */
function _testFailureReport(lines, d, mode) {
  const failLines = lines.filter(l => l.startsWith('FAILED') || l.startsWith('ERROR'));
  // Traceback sections: from "== FAILURES ==" / "== ERRORS ==" up to the
  // short test summary.
  let detail = [];
  let inDetail = false;
  for (const l of lines) {
    if (/^=+ (FAILURES|ERRORS) =+$/.test(l)) { inDetail = true; }
    else if (/^=+ short test summary info =+$/.test(l)) { inDetail = false; }
    if (inDetail) detail.push(l);
  }
  const tail = lines.slice(-40);
  let detailText = detail.join('\n');
  let truncated = false;
  if (detailText.length > _TEST_FIX_DETAIL_CAP) {
    detailText = detailText.slice(0, _TEST_FIX_DETAIL_CAP);
    truncated = true;
  }
  const failText = failLines.join('\n');
  const logPath = d.log_path || '';
  // Same command the Publish gate runs (src/web/routes/test_api.py), including the
  // parallel workers, so "passes for the fix session" means "passes Publish".
  const cmd = 'python -m pytest --tb=short -q --no-header'
    + (mode === 'fast' ? ' --ignore-glob=tests/test_code_pipeline*.py --timeout=60'
                       : ' --ignore-glob=tests/test_code_pipeline*.py --timeout=120')
    + ' -n 8 --dist loadfile tests/';

  let prompt = 'TEST FAILURES ARE BLOCKING PUBLISH\n\n';
  prompt += 'The VibeNode pre-publish test run (' + mode + ' mode) failed. Fix the failures so the developer can publish.\n\n';
  prompt += '## Result\n';
  prompt += 'passed ' + (d.passed || 0) + ', failed ' + (d.failed || 0) + ', errors ' + (d.errors || 0)
    + ', skipped ' + (d.skipped || 0) + ', exit code ' + d.exit_code + '\n';
  prompt += 'Command: `' + cmd + '`\n';
  if (logPath) prompt += 'Full output (read this first, it has every traceback): `' + logPath + '`\n';
  prompt += '\n## Failing tests\n';
  prompt += (failText || '(no FAILED/ERROR summary lines; the run likely aborted. Last output lines:)\n' + tail.join('\n')) + '\n';
  if (detailText) {
    prompt += '\n## Tracebacks' + (truncated ? ' (truncated; see the full log)' : '') + '\n```\n' + detailText + '\n```\n';
  }
  prompt += '\n## Instructions\n';
  prompt += '1. Read the full log, then reproduce each failure by running only that test.\n';
  prompt += '2. Find the root cause. Decide whether the app code is wrong or the test is stale against intended behavior, and fix the right one.\n';
  prompt += '3. Do NOT skip, delete, or weaken tests or assertions to get a pass. If a test is genuinely obsolete, say so and explain why before changing it.\n';
  prompt += '4. Check for order-dependence: a test that passes alone but fails in the full run usually means shared state between test files (see the guard notes in tests/conftest.py).\n';
  prompt += '5. Re-run the failing tests, then the full command above, and confirm 0 failures.\n';
  prompt += '6. Report what failed, why, and what you changed. Do not commit or publish; the developer will press Publish again.\n';
  prompt += '\nFollow CLAUDE.md. This is a public repository, and never restart the Session Engine (the daemon).\n';

  const copyText = (failText || tail.join('\n'))
    + (detailText ? '\n\n' + detailText : '')
    + (logPath ? '\n\nFull output: ' + logPath : '');
  return {failLines, tail, prompt, copyText};
}

/** Copy the failure text, with visible feedback on the button. */
function _copyTestErrors(text, btn) {
  const done = ok => {
    if (btn) {
      const old = btn.textContent;
      btn.textContent = ok ? 'Copied ✓' : 'Copy failed';
      setTimeout(() => { btn.textContent = old; }, 1600);
    } else if (typeof showToast === 'function') {
      showToast(ok ? 'Errors copied' : 'Copy failed');
    }
  };
  if (navigator.clipboard && navigator.clipboard.writeText) {
    navigator.clipboard.writeText(text).then(() => done(true), () => done(_legacyCopy(text)));
  } else {
    done(_legacyCopy(text));
  }
}

function _legacyCopy(text) {
  try {
    const ta = document.createElement('textarea');
    ta.value = text;
    ta.style.position = 'fixed';
    ta.style.opacity = '0';
    document.body.appendChild(ta);
    ta.select();
    const ok = document.execCommand('copy');
    ta.remove();
    return ok;
  } catch (e) { return false; }
}

function _testThenAction(mode, actionType, actionLabel, btnId) {
  closeGitSyncModal();
  const W = _gsWords(actionLabel);
  const verb = actionType === 'publish' ? 'publish' : 'sync';
  const Verb = verb.charAt(0).toUpperCase() + verb.slice(1);
  const desc = (mode === 'fast' ? 'Fast' : 'Full') + ' tests · ' + verb + (verb === 'publish' ? 'es' : 's') + ' when they pass';

  if (typeof _testRunning !== 'undefined' && _testRunning) {
    showGitSyncModal('Tests already running', '<p>Tests are already running in this tab. '
      + 'The earlier run carries on and reports when it finishes.</p>',
      [{label: 'Cancel tests', onclick: () => { if (typeof cancelTests === 'function') cancelTests(); }},
       {label: 'OK', primary: true, onclick: closeGitSyncModal}]);
    return;
  }

  if (typeof _testRunning !== 'undefined') _testRunning = true;

  showGitSyncModal(W.ing, typeof _testRunnerHtml === 'function' ? _testRunnerHtml(desc) : '<p>' + desc + '</p>', [], {center: true});

  const startedAt = Date.now();
  // The run lives on the server and survives a dropped connection; the
  // stream reattaches by itself (_streamTestRun in test-runner.js), so a
  // phone locking its screen mid-run does not leave this dialog on "running".

  _streamTestRun(mode, {
    onLine: lines => { if (typeof _updateTestOutput === 'function') _updateTestOutput(lines); },
    onDone: (d, lines) => {
      if (typeof _testRunning !== 'undefined') _testRunning = false;
      // Recorded in the commit message's "Publish gate" line.
      _publishGate = {
        mode: d.mode || mode,   // the run joined may be a full one
        passed: d.passed || 0,
        skipped: d.skipped || 0,
        failed: (d.failed || 0) + (d.errors || 0),
        seconds: (typeof d.seconds === 'number') ? d.seconds : Math.round((Date.now() - startedAt) / 1000),
        anyway: false,
      };
      // Reuse (src/web/gate_reuse.py): a passing run on identical files stood in
      // for this one, or the A/B tests were left out because nothing they
      // depend on changed.  Both go into the commit's gate line; a run
      // without reuse keeps the gate's old shape.
      if (d.reused_from) _publishGate.reused_from = d.reused_from;
      if (d.ab_reused_from) _publishGate.ab_reused_from = d.ab_reused_from;
      if (d.ok) {
        const reuseNote = d.reused_from
          ? 'Reused the full run that passed at ' + escHtml(d.reused_from) + ' on identical files.'
          : (d.ab_reused_from
            ? 'A/B tests reused from ' + escHtml(d.ab_reused_from) + ': nothing they depend on changed.'
            : null);
        showGitSyncModal(W.ing, _gsRing({pct: 100, tone: 'ok', big: (d.passed || 0).toLocaleString(),
          desc: 'tests passed, ' + verb + 'ing now', counts: reuseNote}), [], {center: true});
        setTimeout(() => {
          executeGitAction('both', btnId, actionLabel);
        }, 1200);
      } else {
        const report = _testFailureReport(lines, d, mode);
        const nFail = report.failLines.length;
        const headline = nFail
          ? 'Fix them, or ' + verb + ' anyway and the commit says so.'
          : 'The test run did not finish cleanly (exit ' + d.exit_code + '). See the output below.';
        const esc = l => (typeof _escTestHtml === 'function' ? _escTestHtml(l) : escHtml(l));
        let body = _gsRing({pct: 100, tone: 'bad', big: nFail ? nFail + ' failed' : 'Stopped',
          desc: 'Nothing was ' + (verb === 'publish' ? 'uploaded' : 'synced') + '.', counts: headline});
        const shown = nFail ? report.failLines : report.tail;
        if (shown.length > 0) {
          body += '<pre id="test-fail-pre" class="gs-pre">' + shown.map(esc).join('\n') + '</pre>';
        }
        if (d.log_path) {
          body += '<div class="gs-note">Full output saved to ' + esc(d.log_path) + '</div>';
        }
        showGitSyncModal(W.not, body, [
          {label: Verb + ' anyway', onclick: () => {
            // Still allowed (a non-technical user must be able to ship), but
            // the commit message will say "published anyway" with the counts.
            if (_publishGate) _publishGate.anyway = true;
            executeGitAction('both', btnId, actionLabel);
          }},
          {label: 'Copy errors', onclick: (e) => _copyTestErrors(report.copyText, e && e.currentTarget)},
          {label: 'Fix errors', primary: true, onclick: () => {
            closeGitSyncModal();
            _startFixSession('Fix Test Failures', 'Fixing failing tests...', report.prompt);
          }},
        ], {sticky: true, center: true, tone: 'bad'});
      }
    },
    onError: (msg, info) => {
      if (typeof _testRunning !== 'undefined') _testRunning = false;
      _publishGate = {mode: 'skipped'};   // nothing ran to completion
      const esc = m => (typeof _escTestHtml === 'function' ? _escTestHtml(m) : escHtml(m));
      if (info && info.busy) {
        // Another run (started elsewhere) is in progress.  Never offer to
        // sync anyway here: say so, and give a way to cancel it.
        showGitSyncModal('Tests already running', '<p>' + esc(msg) + '</p>', [
          {label: 'Cancel that run', onclick: () => { if (typeof cancelTests === 'function') cancelTests(); }},
          {label: 'OK', primary: true, onclick: closeGitSyncModal}
        ]);
        return;
      }
      if (info && info.started) {
        // The run started but its result never arrived.  Never offer to
        // publish anyway here: nothing says the tests passed.
        showGitSyncModal('Test run interrupted', '<p>' + esc(msg) + ' Nothing was ' + verb + 'ed.</p>', [
          {label: 'Close', onclick: closeGitSyncModal},
          {label: 'Run again', primary: true, onclick: () => _testThenAction(mode, actionType, actionLabel, btnId)}
        ], {tone: 'bad'});
        return;
      }
      const why = msg ? ' (' + esc(msg) + ')' : '';
      showGitSyncModal('Tests could not run', '<p>Could not run tests' + why + '. ' + Verb + ' anyway?</p>', [
        {label: 'Cancel', onclick: closeGitSyncModal},
        {label: Verb + ' anyway', onclick: () => executeGitAction('both', btnId, actionLabel)}
      ], {tone: 'bad'});
    },
  });
}

function openGitUpdate() {
  _armNotifyOnGesture();
  const s = _gitStatus;
  if (s.behind === 0) {
    showGitSyncModal('Already up to date', '<p>Your app is already up to date.</p>',
      [{label: 'OK', primary: true, onclick: closeGitSyncModal}]);
    return;
  }
  // Branch guard FIRST — pulling while off-branch updates the wrong branch.
  if (s.on_default === false) {
    _showBranchWarning('pull', 'btn-git-update', 'Update App', s);
    return;
  }
  showGitSyncModal('Update VibeNode', '<p><strong>' + s.behind + ' update' + (s.behind === 1 ? ' is' : 's are') + '</strong> ready. '
    + 'Your own changes stay as they are; if a line changed on both sides, it stops and asks.</p>', [
    {label: 'Not now', onclick: closeGitSyncModal},
    {label: 'Update', primary: true, onclick: () => executeGitAction('pull', 'btn-git-update', 'Update App')}
  ]);
}

// ── Minimize / restore state ──
let _gitSyncMinimized = false;
let _gitSyncFinished = false;  // true when operation completed while minimized
let _gitSyncMiniLabel = '';    // current step label for the mini indicator

function showGitSyncModal(title, body, btns, opts) {
  opts = opts || {};
  // sticky: ignore backdrop clicks; only the dialog's own buttons close it.
  // Used for results the user needs to read or copy (test failures): a text
  // selection that ends on the backdrop counts as a backdrop click and used to
  // close the dialog mid-copy.
  document.getElementById('git-sync-overlay').dataset.sticky = opts.sticky ? '1' : '';
  // center: the ring screens (running and results); questions stay left-aligned.
  const card = document.getElementById('git-sync-card');
  if (card) card.classList.toggle('gs-center', !!opts.center);
  document.getElementById('git-sync-title').textContent = title;
  document.getElementById('git-sync-body').innerHTML = body;
  const acts = document.getElementById('git-sync-actions');
  acts.innerHTML = '';
  btns.forEach(b => {
    const el = document.createElement('button');
    el.type = 'button';
    el.className = 'pm-btn ' + (b.primary ? 'pm-btn-primary' : 'pm-btn-secondary');
    el.textContent = b.label;
    el.onclick = b.onclick;
    acts.appendChild(el);
  });
  // The corner minus (run in the background) shows only while something runs,
  // which is when there are no buttons.
  const minBtn = document.getElementById('git-sync-minimize-btn');
  if (minBtn) minBtn.style.display = btns.length === 0 ? 'inline-flex' : 'none';
  // A result the backdrop cannot close (sticky) gets the corner close mark,
  // so its own row keeps only the buttons that do something.
  const closeBtn = document.getElementById('git-sync-close-btn');
  if (closeBtn) closeBtn.style.display = (opts.sticky && btns.length) ? 'inline-flex' : 'none';
  // Track label for mini indicator
  _gitSyncMiniLabel = title;
  // If minimized, update the mini indicator label instead of showing the overlay
  if (_gitSyncMinimized) {
    const label = document.getElementById('git-sync-mini-label');
    if (label) label.textContent = title;
    // If this is a completion call (has buttons), notify mini
    if (btns.length > 0) {
      const isOk = opts.tone ? opts.tone === 'ok' : (title.includes('✓') || title.includes('All Clear'));
      _notifyMiniComplete(title, isOk);
    }
    return;  // don't show overlay — user minimized it
  }
  document.getElementById('git-sync-overlay').classList.add('show');
}

function closeGitSyncModal() {
  document.getElementById('git-sync-overlay').classList.remove('show');
  _dismissMiniIndicator();
}

function minimizeGitSyncModal() {
  _gitSyncMinimized = true;
  document.getElementById('git-sync-overlay').classList.remove('show');
  // Show floating mini indicator
  const mini = document.getElementById('git-sync-mini');
  const label = document.getElementById('git-sync-mini-label');
  const spinner = document.getElementById('git-sync-mini-spinner');
  const closeBtn = document.getElementById('git-sync-mini-close');
  label.textContent = _gitSyncMiniLabel || document.getElementById('git-sync-title').textContent || 'Working...';
  // A small copy of the dialog's ring, filled as far as the dialog's is: the
  // pill shows how far along the run is, never a spinner.
  spinner.className = 'git-sync-mini-spinner progress';
  spinner.innerHTML = '<svg viewBox="0 0 20 20" aria-hidden="true"><circle class="gsm-track" cx="10" cy="10" r="8"/>'
    + '<circle class="gsm-fill" id="git-sync-mini-ring" cx="10" cy="10" r="8" stroke-dasharray="' + _GSM_RC.toFixed(2)
    + '" stroke-dashoffset="' + _GSM_RC.toFixed(2) + '"/></svg>';
  closeBtn.style.display = 'none';
  mini.classList.add('show');
  _gsMiniFollow();
  if (typeof _updateFloatOffset === 'function') _updateFloatOffset();   // shared float stack (utils.js)
}

const _GSM_RC = 2 * Math.PI * 8;
let _gsMiniTimer = null;

/** How far the dialog's ring is filled (0..1): the git steps, the scan or the tests. */
function _gsDialogFraction() {
  const ring = document.getElementById('gs-ring') || document.getElementById('test-progress-ring');
  if (!ring) return null;
  const c = parseFloat(ring.getAttribute('stroke-dasharray'));
  const off = parseFloat(ring.getAttribute('stroke-dashoffset'));
  if (!(c > 0) || isNaN(off)) return null;
  return Math.max(0, Math.min(1, 1 - off / c));
}

/** While minimized, keep the pill's ring and percentage in step with the dialog's. */
function _gsMiniFollow() {
  if (_gsMiniTimer) clearInterval(_gsMiniTimer);
  const tick = () => {
    if (!_gitSyncMinimized || _gitSyncFinished) { clearInterval(_gsMiniTimer); _gsMiniTimer = null; return; }
    const f = _gsDialogFraction();
    const ring = document.getElementById('git-sync-mini-ring');
    if (ring && f !== null) ring.setAttribute('stroke-dashoffset', (_GSM_RC * (1 - f)).toFixed(2));
    let pct = document.getElementById('git-sync-mini-pct');
    const label = document.getElementById('git-sync-mini-label');
    if (!pct && label) {
      pct = document.createElement('span');
      pct.id = 'git-sync-mini-pct';
      pct.className = 'git-sync-mini-pct';
      label.after(pct);
    }
    if (pct) pct.textContent = f === null ? '' : Math.round(f * 100) + '%';
  };
  tick();
  _gsMiniTimer = setInterval(tick, 250);
}

function restoreGitSyncModal() {
  _gitSyncMinimized = false;
  document.getElementById('git-sync-mini').classList.remove('show');
  if (typeof _layoutFloats === 'function') _layoutFloats();
  document.getElementById('git-sync-overlay').classList.add('show');
}

function _dismissMiniIndicator() {
  _gitSyncMinimized = false;
  _gitSyncFinished = false;
  document.getElementById('git-sync-mini').classList.remove('show');
  if (typeof _layoutFloats === 'function') _layoutFloats();
}

function dismissGitSyncMini() {
  // If finished, just dismiss. If still working, restore modal instead.
  if (_gitSyncFinished) {
    _dismissMiniIndicator();
  } else {
    restoreGitSyncModal();
  }
}

// Called when operation finishes — if minimized, update the mini indicator
function _notifyMiniComplete(title, isOk) {
  if (!_gitSyncMinimized) return;
  _gitSyncFinished = true;
  const label = document.getElementById('git-sync-mini-label');
  const spinner = document.getElementById('git-sync-mini-spinner');
  const closeBtn = document.getElementById('git-sync-mini-close');
  label.textContent = title;
  spinner.innerHTML = '';
  spinner.className = 'git-sync-mini-spinner ' + (isOk ? 'done' : 'error');
  const pct = document.getElementById('git-sync-mini-pct');
  if (pct) pct.textContent = '';
  closeBtn.style.display = 'inline-flex';
  if (typeof _layoutFloats === 'function') _layoutFloats();   // its size just changed
  // Flash the indicator to draw attention
  const mini = document.getElementById('git-sync-mini');
  mini.classList.add('flash');
  setTimeout(() => mini.classList.remove('flash'), 1500);
}

function _syncStatusHtml(stepLabel) {
  return _gsRing({pct: 0, desc: stepLabel || 'Starting…', counts: ''});
}

// ── Branch guard warning ──
// Shown when a sync/publish is attempted while NOT on the repo's default
// branch. Preserves the Fork feature (you can proceed) but stops a
// non-technical user from silently pushing to the wrong branch.
function _showBranchWarning(action, btnId, btnLabel, info) {
  info = info || {};
  const branch = info.branch || '';
  const def = info.default_branch || 'main';
  const branchLabel = branch ? branch : 'a detached HEAD (no branch)';
  const body = '<p>You are on <strong>' + escHtml(branchLabel) + '</strong>, not <strong>' + escHtml(def) + '</strong>. '
    + 'Going ahead sends your work there, not to your main app.</p>'
    + '<p>Unless you meant this, cancel and switch back to <strong>' + escHtml(def) + '</strong> first.</p>';
  showGitSyncModal('You are not on ' + def, body, [
    {label: 'Sync to “' + branchLabel + '” anyway',
     onclick: () => executeGitAction(action, btnId, btnLabel, true)},
    {label: 'Cancel', primary: true, onclick: closeGitSyncModal}
  ], {tone: 'bad'});
}

async function executeGitAction(action, btnId, btnLabel, confirmBranch) {
  closeGitSyncModal();
  // Reset minimize state for new operation
  _gitSyncMinimized = false;
  _gitSyncFinished = false;
  _dismissMiniIndicator();
  const btn = document.getElementById(btnId);
  btn.disabled = true;

  // ── Branch guard: warn before syncing while on a non-default branch. ──
  // Uses the last-polled status for an instant pre-flight; the server enforces
  // the same check again in case this status is stale.
  if (!confirmBranch && _gitStatus && _gitStatus.on_default === false) {
    btn.disabled = false;
    _showBranchWarning(action, btnId, btnLabel, _gitStatus);
    return;
  }

  const isPush = action === 'push' || action === 'both';
  const isPull = action === 'pull' || action === 'both';

  const W = _gsWords(btnLabel);
  // Show the ring for all actions: the scan's progress first when pushing.
  showGitSyncModal(W.ing, isPush ? _scanAnimationHtml() : _syncStatusHtml('Starting…'),
    [], {center: true});

  // For push/sync: run streaming scan first
  if (isPush) {
    try {
      const preScan = await _runStreamingScan();
      await new Promise(r => setTimeout(r, 300));
      if (!preScan.ok) {
        const body = _gsRing({pct: 100, tone: 'bad', big: _gsFound(preScan),
          desc: 'The security scan stopped it. Nothing was uploaded.'}) + _renderScanFindings(preScan);
        showGitSyncModal(W.not, body, [
          {label: 'Close', onclick: closeGitSyncModal},
          {label: 'Fix with AI', primary: true, onclick: () => { closeGitSyncModal(); _launchRemediationSession(preScan); }}
        ], {center: true, tone: 'bad'});
        btn.disabled = false;
        return;
      }
    } catch(_) {
      // Stream failed — fall through to normal sync (server has its own scan)
    }
  }

  try {
    const payload = {action, confirm_branch: !!confirmBranch};
    if (isPush) {
      payload.summary = _publishSummary || '';
      payload.gate = _publishGate || {mode: 'skipped'};
    }
    const r = await _gsRunSync(payload);
    if (isPush && !r.needs_branch_confirm) { _publishSummary = ''; _publishGate = null; }

    // Server-side branch guard (in case the polled status was stale).
    if (r.needs_branch_confirm) {
      if (r.git_status) _applyGitStatus(r.git_status);
      _showBranchWarning(action, btnId, btnLabel, r);
      return;  // finally{} re-enables the button
    }

    const scanBad = !!(r.scan && !r.scan.ok);
    const clash = !!(r.conflicts && r.conflicts.length);
    let body = _gsRing({pct: 100, tone: r.ok ? 'ok' : 'bad',
      mark: r.ok ? _GS_ICON.check : (scanBad ? null : _GS_ICON.x), big: scanBad ? _gsFound(r.scan) : ''})
      + '<ul class="gs-lines">' + (r.messages || []).map(m => '<li>' + escHtml(m) + '</li>').join('') + '</ul>';

    if (scanBad) {
      body += _renderScanFindings(r.scan);
    }

    // The pull changed requirements.txt and pip ran (src/web/git_ops.py).
    if (r.pip_ran) {
      if (r.pip_ok) {
        const n = r.pip_installed || 0;
        if (typeof showToast === 'function') {
          showToast(n ? 'Installed ' + n + ' new dependenc' + (n === 1 ? 'y' : 'ies') : 'Dependencies already satisfied');
        }
      } else {
        if (typeof showToast === 'function') showToast('Dependency install failed — see the Update dialog');
        body += '<pre class="gs-pre">' + escHtml(r.pip_output_tail || '(no pip output)') + '</pre>';
      }
    }

    const btns = [{label: r.ok ? 'Done' : 'Close', primary: r.ok, onclick: closeGitSyncModal}];
    if (scanBad) {
      btns.push({label: 'Fix with AI', primary: true, onclick: () => { closeGitSyncModal(); _launchRemediationSession(r.scan); }});
    } else if (clash) {
      // A clash stopped it: offer the same way out as a scan finding.
      btns.push({label: 'Fix with AI', primary: true, onclick: () => { closeGitSyncModal(); _launchClashSession(r.conflicts, !!r.pull_clash); }});
    }
    // Only a scan finding is a security issue; a clash or a failed pull gets
    // its own title.
    const title = r.ok ? W.ed : clash ? W.not + ': changes clash' : W.not;
    showGitSyncModal(title, body, btns, {center: true, tone: r.ok ? 'ok' : 'bad'});
    if (r.git_status) {
      _applyGitStatus(r.git_status);
    } else {
      await pollGitStatus();
    }
  } catch(e) {
    _gsStopCreep();
    showGitSyncModal('Something went wrong', '<p>Could not complete. Try again.</p>',
      [{label: 'OK', primary: true, onclick: closeGitSyncModal}], {tone: 'bad'});
  } finally {
    btn.disabled = false;
  }
}

// ── Security scan rendering ──
function _renderScanFindings(scan) {
  const row = (name, value, desc) => '<div class="rw-row"><div class="rw-head"><span class="rw-name">' + name + '</span>'
    + '<span class="rw-value">' + value + '</span></div>' + (desc ? '<div class="rw-desc">' + desc + '</div>' : '') + '</div>';
  let html = '<div class="gs-findings"><div class="rw-group-label">' + escHtml(scan.summary)
    + ' · ' + (scan.files_scanned || 0) + ' files scanned</div>';
  (scan.blocked_files || []).forEach(f => {
    html += row(escHtml(f.file), 'blocked file', escHtml(f.reason));
  });
  const findings = scan.findings || [];
  findings.slice(0, 15).forEach(f => {
    html += row(escHtml(f.file) + ':' + f.line, escHtml(f.type), 'Looks like <code>' + escHtml(f.match) + '</code>');
  });
  if (findings.length > 15) {
    html += '<div class="rw-desc">…and ' + (findings.length - 15) + ' more</div>';
  }
  return html + '</div>';
}

function _scanAnimationHtml() {
  return _gsRing({pct: 0, big: '0%', desc: 'Checking for keys and personal details', counts: 'Connecting…', file: true});
}

function _runStreamingScan() {
  return new Promise((resolve, reject) => {
    const es = new EventSource('/api/git-scan-stream');
    const text = () => document.getElementById('scan-progress-text');
    const fname = () => document.getElementById('scan-file-name');

    es.onmessage = (ev) => {
      try {
        const d = JSON.parse(ev.data);
        if (d.type === 'progress') {
          _gsProgress((d.current / d.total) * 100);
          if (text()) text().textContent = d.current + ' of ' + d.total + ' files';
          if (fname()) fname().textContent = d.file;
        } else if (d.type === 'done') {
          es.close();
          _gsProgress(100);
          if (text()) text().textContent = d.files_scanned + ' files checked';
          if (fname()) fname().textContent = '';
          resolve(d);
        }
      } catch(_) {}
    };
    es.onerror = () => {
      es.close();
      reject(new Error('Scan stream failed'));
    };
  });
}

async function runCodeScan() {
  showGitSyncModal('Security scan', _scanAnimationHtml(), [], {center: true});
  try {
    const scan = await _runStreamingScan();
    // Brief pause so user sees 100%
    await new Promise(r => setTimeout(r, 400));
    if (scan.ok) {
      showGitSyncModal('Security scan', _gsRing({pct: 100, tone: 'ok', mark: _GS_ICON.check, desc: 'All clear',
        counts: scan.files_scanned + ' files checked, no keys or personal details found.'}),
        [{label: 'Done', primary: true, onclick: closeGitSyncModal}], {center: true, tone: 'ok'});
    } else {
      showGitSyncModal('Security scan', _gsRing({pct: 100, tone: 'bad', big: _gsFound(scan), desc: 'Fix these before publishing.'})
        + _renderScanFindings(scan), [
        {label: 'Close', onclick: closeGitSyncModal},
        {label: 'Fix with AI', primary: true, onclick: () => { closeGitSyncModal(); _launchRemediationSession(scan); }}
      ], {center: true, tone: 'bad'});
    }
  } catch(e) {
    showGitSyncModal('Something went wrong', '<p>Could not run the scan. Try again.</p>',
      [{label: 'OK', primary: true, onclick: closeGitSyncModal}], {tone: 'bad'});
  }
}

// ── Launch AI remediation session ──
// Spins up a new Claude session with the scan results and a remediation prompt.
// Switches to sessions view and opens the new session automatically.
let _lastScanResults = null;

function _launchRemediationSession(scan) {
  _lastScanResults = scan;

  // Build a detailed prompt for the AI
  let prompt = 'SECURITY SCAN VIOLATION — REMEDIATION REQUIRED\n\n';
  prompt += 'The VibeNode pre-push security scanner has detected issues that are blocking publish.\n';
  prompt += 'You must remediate these issues so the developer can safely publish the app.\n\n';
  prompt += '## Scan Summary\n';
  prompt += scan.summary + ' (' + scan.files_scanned + ' files scanned)\n\n';

  if (scan.blocked_files && scan.blocked_files.length) {
    prompt += '## Blocked Files\n';
    scan.blocked_files.forEach(f => {
      prompt += '- `' + f.file + '` — ' + f.reason + '\n';
    });
    prompt += '\n';
  }

  if (scan.findings && scan.findings.length) {
    prompt += '## Potential Secrets Found\n';
    scan.findings.forEach(f => {
      prompt += '- `' + f.file + ':' + f.line + '` — ' + f.type + ': `' + f.match + '`\n';
    });
    prompt += '\n';
  }

  prompt += '## Instructions\n';
  prompt += '1. Read each flagged file and understand the issue\n';
  prompt += '2. For real secrets: move them to environment variables or kanban_config.json (gitignored)\n';
  prompt += '3. For false positives in source code patterns (like regex patterns that look like keys): add the file to the scanner\'s skip list in app/git_scanner.py\n';
  prompt += '4. For forbidden files (.env, credentials, etc): ensure they are in .gitignore and remove from tracking with `git rm --cached`\n';
  prompt += '5. After making fixes, run the scan again by calling: curl http://localhost:5050/api/git-scan\n';
  prompt += '6. Verify the scan returns {"ok": true} before considering the task complete\n';
  prompt += '\nIMPORTANT: This is a public repository. Everything committed will be visible on the internet.\n';

  _startFixSession('Security Remediation', 'Fixing security scan violations...', prompt);
}

// ── Launch AI clash-resolution session ──
// An Update pulled code that clashed with local changes (src/web/git_ops.py
// returns `conflicts`).  The session resolves the clash; it never publishes.
// `pulled` (the result's `pull_clash`): the pull itself clashed and was undone,
// so there are no markers on disk yet; the session redoes the pull by hand.
function _launchClashSession(files, pulled) {
  let prompt = 'UPDATE STOPPED: CHANGES CLASH\n\n';
  prompt += 'Publishing VibeNode stopped because the pulled code clashed with local changes in:\n';
  files.forEach(f => { prompt += '- `' + f + '`\n'; });
  prompt += '\n## Instructions\n';
  if (pulled) {
    prompt += '0. The Update undid its pull, so the files have no conflict markers yet. Stash any uncommitted changes, run `git pull --rebase` (never with -X theirs or -X ours), and for each clash: resolve it keeping the intent of both sides, `git add` the file, `git rebase --continue`. Then `git stash pop` and resolve the same way.\n';
  }
  prompt += '1. Open each file and resolve every <<<<<<< / ======= / >>>>>>> block, keeping the intent of both sides.\n';
  prompt += '2. Run `git status`: no file may stay unmerged (UU). Unstage with `git reset` so the tree is back to plain local changes.\n';
  prompt += '3. Leave the stash git kept (`git stash list`) alone; it is a backup of the same changes.\n';
  prompt += '4. Run the tests for the files you touched, then grep the repo for leftover conflict markers.\n';
  prompt += '5. Do NOT make new commits or push (finishing the rebase is fine). The developer will press Publish again.\n';
  prompt += '\nIMPORTANT: This is a public repository. Everything committed will be visible on the internet.\n';
  _startFixSession('Resolve Update Clash', 'Resolving the clash...', prompt);
}

/**
 * Start a new visible session that immediately works on `prompt`.
 * Shared by the security-scan "Fix with AI" and the test-failure "Fix errors"
 * buttons.  Uses the system default model and thinking level, like any new
 * session the user starts.
 */
let _repoProjectCache = null;
/** The VibeNode repo as a project ({root, project, registered}), or null. */
async function _repoProject() {
  if (_repoProjectCache) return _repoProjectCache;
  try {
    const r = await fetch('/api/repo-project');
    const d = r.ok ? await r.json() : null;
    if (d && d.ok && d.root && d.project) _repoProjectCache = d;
  } catch (_) {}
  return _repoProjectCache;
}

async function _startFixSession(title, preview, prompt) {
  // The fix session works on THIS repo (its tests, its scan), so it is created
  // in the repo's own project, never in whatever project the browser is
  // showing.  If the browser is elsewhere, switch first so

  // the user watches the session where it actually lives.
  const repo = await _repoProject();
  let cwd = (typeof _currentProjectDir === 'function') ? _currentProjectDir() : '';
  let project = localStorage.getItem('activeProject') || '';
  if (repo) {
    cwd = repo.root;
    project = repo.project;
    if (localStorage.getItem('activeProject') !== repo.project) {
      const known = (typeof _allProjects !== 'undefined') && _allProjects.some(p => p.encoded === repo.project);
      if (known && typeof setProject === 'function') {
        await setProject(repo.project, true);
      } else if (typeof showToast === 'function') {
        showToast('Fix session created in the VibeNode project');
      }
    }
  }

  // Switch to sessions view and create a new session
  if (typeof setViewMode === 'function' && typeof viewMode !== 'undefined' && viewMode !== 'sessions') {
    setViewMode('sessions');
  }

  // Create a new session with the remediation prompt
  const newId = crypto.randomUUID();
  const optimistic = {
    id: newId,
    display_title: title,
    custom_title: title,
    last_activity: '',
    size: '',
    message_count: 0,
    preview: preview,
  };

  if (typeof allSessions !== 'undefined') {
    allSessions.unshift(optimistic);
    if (typeof allSessionIds !== 'undefined') allSessionIds.add(optimistic.id);
  }
  if (typeof filterSessions === 'function') filterSessions();
  if (typeof guiOpenAdd === 'function') guiOpenAdd(newId);

  // Set as active session
  if (typeof activeId !== 'undefined') activeId = newId;
  liveSessionId = newId;
  localStorage.setItem('activeSessionId', newId);

  // Update URL
  if (typeof _pushChatUrl === 'function') _pushChatUrl(newId);

  // Mark as running and emit start_session
  if (typeof runningIds !== 'undefined') runningIds.add(newId);
  if (typeof sessionKinds !== 'undefined') sessionKinds[newId] = 'working';

  const startOpts = {
    session_id: newId,
    prompt: prompt,
    cwd: cwd,
    project: project,
    name: title,
  };
  if (typeof SessionModel !== 'undefined') {
    const _m = SessionModel.getDefault();
    const _t = SessionModel.getDefaultThinking();
    if (_m) startOpts.model = _m;
    if (_t) startOpts.thinking_level = _t;
  }

  socket.emit('start_session', startOpts);

  // Switch to live panel and show the session
  if (typeof startLivePanel === 'function') startLivePanel(newId, {skipLog: true});

  // Add optimistic user bubble
  if (typeof _addOptimisticBubble === 'function') {
    _liveSending = true;
    _addOptimisticBubble(newId, prompt);
    setTimeout(() => { _liveSending = false; }, 500);
  }

  // Update toolbar
  if (typeof setToolbarSession === 'function') setToolbarSession(newId, title, false, title);
  if (typeof updateLiveInputBar === 'function') updateLiveInputBar();
}
