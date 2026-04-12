/* git-sync.js — git status polling and sync actions */

let _gitStatus = {};

function _applyGitStatus(s) {
  _gitStatus = s;
  const hasPush = s.ahead > 0 || s.uncommitted;
  const hasPull = s.behind > 0;
  const btnUpdate  = document.getElementById('btn-git-update');
  const btnPublish = document.getElementById('btn-git-publish');
  const btnSync    = document.getElementById('btn-git-sync');
  if (hasPull && hasPush) {
    // Both directions — show single Sync button
    btnUpdate.style.display = 'none';
    btnPublish.style.display = 'none';
    document.getElementById('git-badge-sync').textContent = '\u2193\u2191';
    btnSync.style.display = 'inline-flex';
  } else {
    btnSync.style.display = 'none';
    if (hasPull) {
      document.getElementById('git-badge-pull').textContent = '\u2193';
      btnUpdate.style.display = 'inline-flex';
    } else {
      btnUpdate.style.display = 'none';
    }
    if (hasPush) {
      document.getElementById('git-badge-push').textContent = '\u2191';
      btnPublish.style.display = 'inline-flex';
    } else {
      btnPublish.style.display = 'none';
    }
  }
}

async function pollGitStatus() {
  try {
    const res = await fetch('/api/git-status');
    const s = await res.json();
    _applyGitStatus(s);
  } catch(e) {}
}

function openGitPublish() {
  const s = _gitStatus;
  const hasPush = s.ahead > 0 || s.uncommitted;
  if (!hasPush) {
    showGitSyncModal('Publish App Update', '<p style="color:var(--text-muted)">Nothing to publish \u2014 your app is already up to date on remote.</p>',
      [{label:'OK', onclick: closeGitSyncModal}]);
    return;
  }
  let body = '<p>Your local changes are ready to publish.</p>'
    + '<p style="color:var(--text-muted);font-size:12px">They will be saved and uploaded to remote automatically.</p>';
  if (s.behind > 0) {
    body += '<p style="color:var(--text-muted);font-size:12px;margin-top:8px">'
      + s.behind + ' remote update(s) will be pulled in first, then your changes pushed.</p>';
  }
  showGitSyncModal('Publish App Update', body, [
    {label: 'Publish Now', primary: true, onclick: () => executeGitAction('both', 'btn-git-publish', 'Publish App Update')},
    {label: 'Cancel', onclick: closeGitSyncModal}
  ]);
}

function openGitSyncBoth() {
  const s = _gitStatus;
  let body = '<p><b style="color:var(--text-heading)">' + s.behind + ' update(s)</b> to pull and '
    + '<b style="color:var(--text-heading)">' + (s.ahead + (s.uncommitted ? 1 : 0)) + ' change(s)</b> to push.</p>'
    + '<p style="color:var(--text-muted);font-size:12px">Remote updates will be pulled first, merge conflicts resolved automatically, then your changes pushed.</p>';
  showGitSyncModal('Sync App', body, [
    {label: 'Sync Now', primary: true, onclick: () => executeGitAction('both', 'btn-git-sync', 'Sync App')},
    {label: 'Cancel', onclick: closeGitSyncModal}
  ]);
}

function openGitUpdate() {
  const s = _gitStatus;
  if (s.behind === 0) {
    showGitSyncModal('Update App', '<p style="color:var(--text-muted)">Your app is already up to date.</p>',
      [{label:'OK', onclick: closeGitSyncModal}]);
    return;
  }
  showGitSyncModal('Update App', '<p><b style="color:var(--text-heading)">' + s.behind + ' update(s)</b> are available from remote.</p>'
    + '<p style="color:var(--text-muted);font-size:12px">Your app will be updated to the latest version. Your local changes are safe.</p>', [
    {label: 'Update Now', primary: true, onclick: () => executeGitAction('pull', 'btn-git-update', 'Update App')},
    {label: 'Cancel', onclick: closeGitSyncModal}
  ]);
}

// ── Minimize / restore state ──
let _gitSyncMinimized = false;
let _gitSyncFinished = false;  // true when operation completed while minimized
let _gitSyncMiniLabel = '';    // current step label for the mini indicator

function showGitSyncModal(title, body, btns) {
  document.getElementById('git-sync-title').textContent = title;
  document.getElementById('git-sync-body').innerHTML = body;
  const acts = document.getElementById('git-sync-actions');
  acts.innerHTML = '';
  btns.forEach(b => {
    const el = document.createElement('button');
    el.className = 'btn' + (b.primary ? ' primary' : '');
    el.textContent = b.label;
    el.onclick = b.onclick;
    acts.appendChild(el);
  });
  // Show minimize button only when there are no action buttons (in-progress state)
  const minBtn = document.getElementById('git-sync-minimize-btn');
  if (minBtn) minBtn.style.display = btns.length === 0 ? 'inline-flex' : 'none';
  // Track label for mini indicator
  _gitSyncMiniLabel = title;
  // If minimized, update the mini indicator label instead of showing the overlay
  if (_gitSyncMinimized) {
    const label = document.getElementById('git-sync-mini-label');
    if (label) label.textContent = title;
    // If this is a completion call (has buttons), notify mini
    if (btns.length > 0) {
      const isOk = title.includes('\u2713') || title.includes('All Clear');
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
  spinner.className = 'git-sync-mini-spinner working';
  closeBtn.style.display = 'none';
  mini.classList.add('show');
}

function restoreGitSyncModal() {
  _gitSyncMinimized = false;
  document.getElementById('git-sync-mini').classList.remove('show');
  document.getElementById('git-sync-overlay').classList.add('show');
}

function _dismissMiniIndicator() {
  _gitSyncMinimized = false;
  _gitSyncFinished = false;
  document.getElementById('git-sync-mini').classList.remove('show');
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
  spinner.className = 'git-sync-mini-spinner ' + (isOk ? 'done' : 'error');
  closeBtn.style.display = 'inline-flex';
  // Flash the indicator to draw attention
  const mini = document.getElementById('git-sync-mini');
  mini.classList.add('flash');
  setTimeout(() => mini.classList.remove('flash'), 1500);
}

function _syncStatusHtml(stepLabel) {
  return '<div class="scan-anim">'
    + '<div class="scan-shield">'
    +   '<svg width="48" height="48" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round">'
    +     '<path d="M12 22s8-4 8-10V5l-8-3-8 3v7c0 6 8 10 8 10z"/>'
    +   '</svg>'
    +   '<div class="scan-beam"></div>'
    + '</div>'
    + '<div class="scan-label" id="sync-step-label">' + (stepLabel || 'Working...') + '</div>'
    + '<div class="scan-progress">'
    +   '<div class="scan-progress-bar"><div class="scan-progress-fill" id="scan-progress-fill" style="width:0%"></div></div>'
    +   '<div class="scan-progress-text" id="scan-progress-text"></div>'
    +   '<div class="scan-file-name" id="scan-file-name" style="font-size:10px;color:var(--text-faint,#666);margin-top:3px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;max-width:380px;"></div>'
    + '</div>'
    + '</div>';
}

function _setSyncStep(label, pct) {
  const el = document.getElementById('sync-step-label');
  const fill = document.getElementById('scan-progress-fill');
  const text = document.getElementById('scan-progress-text');
  const fname = document.getElementById('scan-file-name');
  if (el) el.textContent = label;
  if (fill) fill.style.width = (pct || 0) + '%';
  if (text) text.textContent = '';
  if (fname) fname.textContent = '';
  // Update mini indicator label if minimized
  _gitSyncMiniLabel = label;
  if (_gitSyncMinimized) {
    const miniLabel = document.getElementById('git-sync-mini-label');
    if (miniLabel) miniLabel.textContent = label;
  }
}

async function executeGitAction(action, btnId, btnLabel) {
  closeGitSyncModal();
  // Reset minimize state for new operation
  _gitSyncMinimized = false;
  _gitSyncFinished = false;
  _dismissMiniIndicator();
  const btn = document.getElementById(btnId);
  btn.disabled = true;

  const isPush = action === 'push' || action === 'both';
  const isPull = action === 'pull' || action === 'both';

  // Show animated modal for all actions
  const firstStep = isPull ? 'Pulling latest updates...' : 'Scanning repository...';
  showGitSyncModal(btnLabel, isPush ? _scanAnimationHtml() : _syncStatusHtml(firstStep), []);

  // For push/sync: run streaming scan first
  if (isPush) {
    try {
      const preScan = await _runStreamingScan();
      await new Promise(r => setTimeout(r, 300));
      if (!preScan.ok) {
        let body = '<ul style="margin:10px 0 0 16px;color:var(--text-secondary);">'
          + '<li>Push blocked by security scan: ' + escHtml(preScan.summary) + '</li></ul>'
          + _renderScanFindings(preScan);
        const scanBtns = [
          {label: 'Fix with AI', primary: true, onclick: () => { closeGitSyncModal(); _launchRemediationSession(preScan); }},
          {label: 'Close', onclick: closeGitSyncModal}
        ];
        showGitSyncModal('Push Blocked \u2014 Security Issue', body, scanBtns);
        btn.disabled = false;
        return;
      }
      // Scan passed — transition to sync step
      _setSyncStep(isPull ? 'Pulling & pushing...' : 'Pushing changes...', 0);
    } catch(_) {
      // Stream failed — fall through to normal sync (server has its own scan)
    }
  }

  // Show indeterminate progress during the git operation
  const _pulseTimer = setInterval(() => {
    const fill = document.getElementById('scan-progress-fill');
    if (!fill) { clearInterval(_pulseTimer); return; }
    // Gentle pulse between 20-80% to show activity
    const t = (Date.now() % 2000) / 2000;
    const pct = 20 + 60 * (0.5 + 0.5 * Math.sin(t * Math.PI * 2));
    fill.style.width = pct + '%';
  }, 50);

  try {
    const res = await fetch('/api/git-sync', {
      method: 'POST', headers: {'Content-Type':'application/json'},
      body: JSON.stringify({action})
    });
    clearInterval(_pulseTimer);
    const r = await res.json();

    // Snap to 100%
    const fill = document.getElementById('scan-progress-fill');
    if (fill) fill.style.width = '100%';
    await new Promise(r => setTimeout(r, 300));

    let body = '<ul style="margin:10px 0 0 16px;color:var(--text-secondary);">'
      + r.messages.map(m => '<li>' + escHtml(m) + '</li>').join('') + '</ul>';

    if (r.scan && !r.scan.ok) {
      body += _renderScanFindings(r.scan);
    }

    const btns = [{label:'OK', primary: r.ok, onclick: closeGitSyncModal}];
    if (r.scan && !r.scan.ok) {
      btns.unshift({label: 'Fix with AI', primary: true, onclick: () => { closeGitSyncModal(); _launchRemediationSession(r.scan); }});
    }
    showGitSyncModal(r.ok ? btnLabel + ' \u2713' : 'Push Blocked \u2014 Security Issue', body, btns);
    if (r.git_status) {
      _applyGitStatus(r.git_status);
    } else {
      await pollGitStatus();
    }
  } catch(e) {
    clearInterval(_pulseTimer);
    showGitSyncModal('Error', '<p style="color:var(--result-err)">Could not complete. Try again.</p>',
      [{label:'OK', onclick: closeGitSyncModal}]);
  } finally {
    btn.disabled = false;
  }
}

// ── Security scan rendering ──
function _renderScanFindings(scan) {
  let html = '<div style="margin-top:12px;padding:10px;background:rgba(255,60,60,0.08);border:1px solid rgba(255,60,60,0.2);border-radius:8px;">';
  html += '<div style="font-weight:600;color:var(--result-err,#ff4444);font-size:12px;margin-bottom:6px;">Security Scan Results</div>';
  html += '<div style="font-size:11px;color:var(--text-secondary);margin-bottom:8px;">' + escHtml(scan.summary) + ' (' + scan.files_scanned + ' files scanned)</div>';

  if (scan.blocked_files && scan.blocked_files.length) {
    html += '<div style="font-size:11px;font-weight:600;color:var(--text-heading);margin:6px 0 3px;">Blocked Files:</div>';
    html += '<ul style="margin:0 0 0 14px;font-size:11px;color:var(--text-secondary);">';
    scan.blocked_files.forEach(f => {
      html += '<li><code style="color:var(--result-err)">' + escHtml(f.file) + '</code> — ' + escHtml(f.reason) + '</li>';
    });
    html += '</ul>';
  }

  if (scan.findings && scan.findings.length) {
    html += '<div style="font-size:11px;font-weight:600;color:var(--text-heading);margin:6px 0 3px;">Potential Secrets:</div>';
    html += '<ul style="margin:0 0 0 14px;font-size:11px;color:var(--text-secondary);">';
    scan.findings.slice(0, 15).forEach(f => {
      html += '<li><code style="color:var(--result-err)">' + escHtml(f.file) + ':' + f.line + '</code> — '
        + escHtml(f.type) + ': <code>' + escHtml(f.match) + '</code></li>';
    });
    if (scan.findings.length > 15) {
      html += '<li style="color:var(--text-muted)">...and ' + (scan.findings.length - 15) + ' more</li>';
    }
    html += '</ul>';
  }

  html += '</div>';
  return html;
}

function _scanAnimationHtml() {
  return '<div class="scan-anim">'
    + '<div class="scan-shield">'
    +   '<svg width="48" height="48" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round">'
    +     '<path d="M12 22s8-4 8-10V5l-8-3-8 3v7c0 6 8 10 8 10z"/>'
    +   '</svg>'
    +   '<div class="scan-beam"></div>'
    + '</div>'
    + '<div class="scan-label">Scanning repository...</div>'
    + '<div class="scan-progress">'
    +   '<div class="scan-progress-bar"><div class="scan-progress-fill" id="scan-progress-fill"></div></div>'
    +   '<div class="scan-progress-text" id="scan-progress-text">Connecting...</div>'
    +   '<div class="scan-file-name" id="scan-file-name" style="font-size:10px;color:var(--text-faint,#666);margin-top:3px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;max-width:380px;"></div>'
    + '</div>'
    + '</div>';
}

function _runStreamingScan() {
  return new Promise((resolve, reject) => {
    const es = new EventSource('/api/git-scan-stream');
    const fill = () => document.getElementById('scan-progress-fill');
    const text = () => document.getElementById('scan-progress-text');
    const fname = () => document.getElementById('scan-file-name');

    es.onmessage = (ev) => {
      try {
        const d = JSON.parse(ev.data);
        if (d.type === 'progress') {
          const pct = Math.round((d.current / d.total) * 100);
          if (fill()) fill().style.width = pct + '%';
          if (text()) text().textContent = d.current + ' / ' + d.total + ' files';
          if (fname()) fname().textContent = d.file;
        } else if (d.type === 'done') {
          es.close();
          if (fill()) fill().style.width = '100%';
          if (text()) text().textContent = d.files_scanned + ' / ' + d.files_scanned + ' files';
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
  showGitSyncModal('Server Scan', _scanAnimationHtml(), []);
  try {
    const scan = await _runStreamingScan();
    // Brief pause so user sees 100%
    await new Promise(r => setTimeout(r, 400));
    let body;
    const btns = [];
    if (scan.ok) {
      body = '<div style="text-align:center;padding:16px 0;">'
        + '<div style="font-weight:600;color:var(--accent-green,#4ecdc4);">All Clear</div>'
        + '<div style="font-size:12px;color:var(--text-muted);margin-top:4px;">' + scan.files_scanned + ' files scanned — no secrets or sensitive data detected.</div>'
        + '</div>';
      btns.push({label: 'OK', primary: true, onclick: closeGitSyncModal});
    } else {
      body = _renderScanFindings(scan);
      btns.push({label: 'Fix with AI', primary: true, onclick: () => { closeGitSyncModal(); _launchRemediationSession(scan); }});
      btns.push({label: 'Close', onclick: closeGitSyncModal});
    }
    showGitSyncModal('Server Scan Results', body, btns);
  } catch(e) {
    showGitSyncModal('Error', '<p style="color:var(--result-err)">Could not run scan. Try again.</p>',
      [{label:'OK', onclick: closeGitSyncModal}]);
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

  // Switch to sessions view and create a new session
  if (typeof setViewMode === 'function' && typeof viewMode !== 'undefined' && viewMode !== 'sessions') {
    setViewMode('sessions');
  }

  // Create a new session with the remediation prompt
  const newId = crypto.randomUUID();
  const optimistic = {
    id: newId,
    display_title: 'Security Remediation',
    custom_title: 'Security Remediation',
    last_activity: '',
    size: '',
    message_count: 0,
    preview: 'Fixing security scan violations...',
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
    cwd: (typeof _currentProjectDir === 'function') ? _currentProjectDir() : '',
    name: 'Security Remediation',
  };

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
  if (typeof setToolbarSession === 'function') setToolbarSession(newId, 'Security Remediation', false, 'Security Remediation');
  if (typeof updateLiveInputBar === 'function') updateLiveInputBar();
}
