/* modals.js — header system dropdown, summary, group dropdowns, respond popup */

// --- Header System dropdown ---
function toggleHdrSys() {
  document.getElementById('hdr-sys-dropdown').classList.toggle('open');
}
function closeHdrSys() {
  document.getElementById('hdr-sys-dropdown').classList.remove('open');
}
document.addEventListener('click', function(e) {
  if (!document.getElementById('hdr-sys').contains(e.target)) closeHdrSys();
});

// --- Restart server ---
function restartServer() {
  // Build a modal popup letting the user choose scope
  const overlay = document.createElement('div');
  overlay.className = 'modal-overlay';
  overlay.style.cssText = 'position:fixed;inset:0;background:rgba(0,0,0,.55);z-index:9999;display:flex;align-items:center;justify-content:center';
  overlay.innerHTML = `
    <div style="background:var(--bg-secondary,#1e1e2e);border:1px solid var(--border,#444);border-radius:10px;padding:24px 28px;min-width:340px;max-width:420px;color:var(--text,#cdd6f4);font-family:inherit">
      <h3 style="margin:0 0 14px;font-size:16px">Restart — choose scope</h3>
      <div style="display:flex;flex-direction:column;gap:8px">
        <button class="restart-opt" data-scope="web" style="padding:10px 14px;border-radius:6px;border:1px solid var(--border,#444);background:var(--bg-tertiary,#313244);color:var(--text,#cdd6f4);cursor:pointer;text-align:left">
          <strong>Web Server</strong> (port 5050)<br><span style="font-size:12px;opacity:.7">Reloads Python code. Running agents stay alive.</span>
        </button>
        <button class="restart-opt" data-scope="daemon" style="padding:10px 14px;border-radius:6px;border:1px solid var(--border,#444);background:var(--bg-tertiary,#313244);color:var(--text,#cdd6f4);cursor:pointer;text-align:left">
          <strong>Session Daemon</strong> (port 5051)<br><span style="font-size:12px;opacity:.7">Restarts Claude SDK daemon. All running sessions will be killed.</span>
        </button>
        <button class="restart-opt" data-scope="both" style="padding:10px 14px;border-radius:6px;border:1px solid var(--border,#444);background:var(--bg-tertiary,#313244);color:var(--text,#cdd6f4);cursor:pointer;text-align:left">
          <strong>Both</strong> (5050 + 5051)<br><span style="font-size:12px;opacity:.7">Full restart. All sessions will be killed.</span>
        </button>
      </div>
      <button id="restart-cancel" style="margin-top:14px;padding:6px 16px;border-radius:6px;border:1px solid var(--border,#444);background:transparent;color:var(--text,#cdd6f4);cursor:pointer;float:right">Cancel</button>
    </div>`;
  document.body.appendChild(overlay);

  overlay.querySelector('#restart-cancel').onclick = () => overlay.remove();
  overlay.addEventListener('click', (e) => { if (e.target === overlay) overlay.remove(); });

  overlay.querySelectorAll('.restart-opt').forEach(btn => {
    btn.onmouseenter = () => { btn.style.borderColor = 'var(--accent,#89b4fa)'; };
    btn.onmouseleave = () => { btn.style.borderColor = 'var(--border,#444)'; };
    btn.onclick = () => {
      const scope = btn.dataset.scope;
      overlay.remove();
      _doRestart(scope);
    };
  });
}

async function _doRestart(scope) {
  const labels = { web: 'web server', daemon: 'session daemon', both: 'web server + daemon' };
  try {
    if (typeof showToast === 'function') showToast('Restarting ' + (labels[scope] || scope) + '...');
    await fetch('/api/restart', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ scope: scope }),
    });
  } catch (e) { /* expected — server is shutting down */ }
  // Wait for the server to come back, then reload
  let attempts = 0;
  const check = setInterval(async () => {
    attempts++;
    try {
      const r = await fetch('/', { method: 'HEAD' });
      if (r.ok) { clearInterval(check); window.location.reload(); }
    } catch (e) { /* still down */ }
    if (attempts > 30) { clearInterval(check); window.location.reload(); }
  }, 1000);
}

// --- Persistent Storage modal (System → Persistent Storage) ---
async function openPersistentStorage() {
  let config = {};
  try {
    const r = await fetch('/api/kanban/config');
    if (r.ok) config = await r.json();
  } catch (e) { /* defaults */ }

  const isSupa = config.kanban_backend === 'supabase';
  const overlay = document.getElementById('pm-overlay');
  if (!overlay) return;

  // Update the System dropdown label
  const label = document.getElementById('sys-storage-label');
  if (label) label.textContent = isSupa ? 'Cloud' : 'Local';

  let html = `<div class="pm-card pm-enter" style="max-width:520px;">
    <h2 class="pm-title">Persistent Storage</h2>
    <div class="pm-body">
      <div style="font-size:12px;color:var(--text-muted);margin-bottom:16px;">
        Controls where your <strong>kanban tasks</strong> are stored. This applies to all projects &mdash; not sessions.
        Sessions always stay local. Switching copies all existing tasks to the new backend.
      </div>
      <div style="display:flex;gap:12px;margin-bottom:16px;">
        <div id="kb-opt-sqlite" class="kanban-backend-option${!isSupa ? ' active' : ''}" onclick="selectBackend('sqlite')">
          <div style="font-weight:600;color:${!isSupa ? 'var(--accent)' : 'var(--text)'};margin-bottom:4px;">Local (SQLite)</div>
          <div style="font-size:12px;color:var(--text-muted);">Tasks stay on this machine. Zero config.</div>
          ${!isSupa ? '<div style="font-size:11px;color:var(--green);margin-top:4px;">Currently active</div>' : ''}
          ${isSupa ? '<button class="kanban-settings-btn-accent" onclick="event.stopPropagation();switchToLocal()" id="kb-local-btn" style="margin-top:8px;padding:6px 14px;font-size:12px;">Switch to Local</button><span id="kb-local-status" style="font-size:11px;display:block;margin-top:4px;"></span>' : ''}
        </div>
        <div id="kb-opt-supabase" class="kanban-backend-option${isSupa ? ' active' : ''}" onclick="selectBackend('supabase')">
          <div style="font-weight:600;color:${isSupa ? 'var(--accent)' : 'var(--text)'};margin-bottom:4px;">Cloud (Supabase)</div>
          <div style="font-size:12px;color:var(--text-muted);">Tasks sync to a hosted PostgreSQL database.</div>
          ${isSupa ? '<div style="font-size:11px;color:var(--green);margin-top:4px;">Currently active</div>' : ''}
        </div>
      </div>
      <div id="kb-supabase-config" style="${isSupa ? '' : 'display:none;'}padding:16px;border:1px solid var(--border);border-radius:8px;">
        <div style="font-size:13px;font-weight:600;margin-bottom:8px;color:var(--text-muted);">Supabase Connection</div>
        <div class="kanban-settings-field"><label>Project URL</label><input type="text" id="kb-supa-url" value="${typeof escHtml === 'function' ? escHtml(config.supabase_url || '') : (config.supabase_url || '')}" placeholder="https://your-project.supabase.co"></div>
        <div class="kanban-settings-field"><label>Secret Key <span style="font-size:10px;color:var(--orange);">(service_role)</span></label><input type="password" id="kb-supa-key" value="${typeof escHtml === 'function' ? escHtml(config.supabase_secret_key || '') : (config.supabase_secret_key || '')}" placeholder="eyJhbGciOi..."></div>
        <div style="display:flex;gap:8px;margin-top:10px;align-items:center;">
          <button class="kanban-settings-btn-accent" id="kb-test-btn" onclick="testConnection()" style="padding:8px 18px;font-size:13px;">Step 1: Test Connection</button>
          <button class="kanban-settings-btn-accent" id="kb-switch-btn" onclick="switchToSupabase()" style="display:none;padding:8px 18px;font-size:13px;">Step 3: Switch to Supabase</button>
          <span id="kb-conn-status" style="font-size:12px;margin-left:4px;"></span>
        </div>
        <div id="kb-schema-setup" style="display:none;margin-top:12px;padding:16px;border:2px solid var(--orange);border-radius:8px;background:rgba(210,153,34,0.08);">
          <div style="font-size:14px;font-weight:700;color:var(--orange);margin-bottom:8px;">Step 2: Create database tables</div>
          <div style="font-size:13px;color:var(--text-secondary);margin-bottom:4px;">Your Supabase project is connected but empty. To create the tables automatically:</div>
          <ol style="font-size:12px;color:var(--text-secondary);margin:4px 0 12px 16px;padding:0;line-height:1.8;">
            <li>Go to <strong>supabase.com/dashboard/account/tokens</strong></li>
            <li>Click <strong>Generate new token</strong>, copy it</li>
            <li>Paste it below and click <strong>Setup Database</strong></li>
          </ol>
          <div class="kanban-settings-field" style="margin-bottom:10px;"><label style="font-weight:600;">Access Token</label><input type="password" id="kb-access-token" placeholder="sbp_..." style="font-size:13px;"></div>
          <div style="display:flex;gap:8px;align-items:center;">
            <button class="kanban-settings-btn-accent" onclick="setupSupabaseSchema()" id="kb-setup-btn" style="padding:8px 18px;font-size:13px;">Setup Database</button>
            <span id="kb-setup-status" style="font-size:12px;"></span>
          </div>
        </div>
      </div>
    </div>
    <div class="pm-actions">
      <button class="pm-btn pm-btn-secondary" onclick="_closePm()">Close</button>
    </div>
  </div>`;

  overlay.innerHTML = html;
  overlay.classList.add('show');
  requestAnimationFrame(() => overlay.querySelector('.pm-card')?.classList.remove('pm-enter'));
  overlay.onclick = (e) => { if (e.target === overlay && typeof _closePm === 'function') _closePm(); };
}

// Update the storage label on page load (retry until element exists)
(async function _updateStorageLabel() {
  for (let i = 0; i < 20; i++) {
    const label = document.getElementById('sys-storage-label');
    if (label) {
      try {
        const r = await fetch('/api/kanban/config');
        if (r.ok) {
          const cfg = await r.json();
          label.textContent = cfg.kanban_backend === 'supabase' ? 'Cloud' : 'Local';
        }
      } catch (e) { /* ignore */ }
      return;
    }
    await new Promise(r => setTimeout(r, 200));
  }
})();

// --- Summary modal ---
async function showSummary(id) {
  document.getElementById('summary-body').innerHTML = '<div style="color:var(--text-faint);font-size:13px;"><span class="spinner"></span> Building summary\u2026</div>';
  document.getElementById('summary-overlay').classList.add('show');

  const resp = await fetch('/api/summary/' + id);
  const data = await resp.json();
  document.getElementById('summary-body').innerHTML = data.html || ('<p style="color:var(--text-muted)">' + (data.error||'No summary available') + '</p>');
}

function closeSummary() {
  document.getElementById('summary-overlay').classList.remove('show');
}

document.getElementById('summary-overlay').addEventListener('click', function(e) {
  if (e.target === this) closeSummary();
});

// --- Group dropdowns ---
function toggleGrpDropdown(grpId) {
  const grp = document.getElementById(grpId);
  const label = grp.querySelector('.btn-group-label');

  // Close any existing popup
  if (_activeGrpPopup) {
    _activeGrpPopup.remove();
    const prevLabel = document.querySelector('.btn-group-label.grp-open');
    if (prevLabel) prevLabel.classList.remove('grp-open');
    if (_activeGrpPopup._grpId === grpId) { _activeGrpPopup = null; return; }
    _activeGrpPopup = null;
  }

  label.classList.add('grp-open');

  // Clone the btn-group-inner buttons into a floating popup
  const inner = grp.querySelector('.btn-group-inner');
  const popup = document.createElement('div');
  popup.className = 'grp-popup';
  popup._grpId = grpId;

  Array.from(inner.children).forEach(el => {
    // Skip permanently hidden elements
    if (el.style.display === 'none') return;
    const clone = el.cloneNode(true);
    // Only wire up click handlers for actual buttons
    if (el.tagName === 'BUTTON') {
      const oc = el.getAttribute('onclick');
      if (oc) clone.setAttribute('onclick', oc);
      clone.addEventListener('click', () => { closeAllGrpDropdowns(); });
    }
    popup.appendChild(clone);
  });

  // Position below the label, aligned to the right edge
  const rect = label.getBoundingClientRect();
  popup.style.top  = (rect.bottom + 4) + 'px';
  popup.style.right = (window.innerWidth - rect.right) + 'px';
  document.body.appendChild(popup);
  _activeGrpPopup = popup;
}

function closeAllGrpDropdowns() {
  if (_activeGrpPopup) { _activeGrpPopup.remove(); _activeGrpPopup = null; }
  document.querySelectorAll('.btn-group-label.grp-open').forEach(l => l.classList.remove('grp-open'));
}

// Close popup when clicking outside
document.addEventListener('click', e => {
  if (!_activeGrpPopup) return;
  if (e.target.closest('.grp-popup') || e.target.closest('.btn-group-label')) return;
  closeAllGrpDropdowns();
});

// --- Actions popup ---
function openActionsPopup() {
  // Update the status badge
  const statusEl = document.getElementById('actions-status');
  if (activeId) {
    const kind = sessionKinds[activeId] || 'sleeping';
    const icons = {
      question: '<svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="#ff9500" stroke-width="2"><circle cx="12" cy="12" r="10"/><path d="M9.09 9a3 3 0 0 1 5.83 1c0 2-3 3-3 3"/><circle cx="12" cy="17" r=".5" fill="#ff9500"/></svg>',
      working: '<img src="/static/svg/pickaxe.svg" width="12" height="12" style="filter:brightness(0) saturate(100%) invert(55%) sepia(78%) saturate(1000%) hue-rotate(215deg);">',
      idle: '<svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="var(--idle-label)" stroke-width="2"><polyline points="20 6 9 17 4 12"/></svg>',
      sleeping: '<img src="/static/svg/sleeping.svg" width="12" height="12" class="sleeping-icon">',
    };
    const labels = {question:'Waiting for input', working:'Working', idle:'Idle', sleeping:'Not running'};
    statusEl.innerHTML = (icons[kind] || icons.sleeping) + ' ' + (labels[kind] || 'Not running');
  } else {
    statusEl.innerHTML = '';
  }
  document.getElementById('actions-overlay').classList.add('show');
}

function closeActionsPopup() {
  document.getElementById('actions-overlay').classList.remove('show');
}

function switchActionsTab(tabName) {
  document.querySelectorAll('.actions-tab').forEach(t => t.classList.toggle('active', t.getAttribute('onclick').includes("'" + tabName + "'")));
  document.querySelectorAll('.actions-tab-panel').forEach(p => p.classList.toggle('active', p.dataset.tab === tabName));
}

document.getElementById('actions-overlay').addEventListener('click', function(e) {
  if (e.target === this) closeActionsPopup();
});

// --- Respond popup ---
function openRespond(id) {
  const w = waitingData[id];
  if (!w) return;
  respondTarget = id;

  // Question text
  document.getElementById('respond-question').innerHTML = mdParse(w.question || '(no question text)');

  // Option buttons
  const optsEl = document.getElementById('respond-options');
  const orEl   = document.getElementById('respond-or');
  optsEl.innerHTML = '';
  if (w.options && w.options.length) {
    w.options.forEach(opt => {
      const btn = document.createElement('button');
      btn.className = 'respond-opt';
      btn.textContent = opt;
      btn.onclick = () => sendRespond(opt);
      optsEl.appendChild(btn);
    });
    orEl.style.display = 'block';
  } else {
    orEl.style.display = 'none';
  }

  document.getElementById('respond-input').value = '';
  document.getElementById('respond-overlay').classList.add('open');
  // Scroll question to bottom so the most recent part (the actual ask) is visible
  setTimeout(() => {
    const qEl = document.getElementById('respond-question');
    qEl.scrollTop = qEl.scrollHeight;
    const ri = document.getElementById('respond-input');
    ri.value = '';
    _resetTextareaHeight(ri);
    _initAutoResize(ri);
    ri.focus();
  }, 60);
}

function closeRespond() {
  document.getElementById('respond-overlay').classList.remove('open');
  respondTarget = null;
}

function sendRespond(text) {
  if (!text || !respondTarget) return;
  const sid = respondTarget;

  // If it's a permission response (from waitingData), use permission_response event
  if (waitingData[sid]) {
    socket.emit('permission_response', {session_id: sid, action: text});
  } else {
    socket.emit('send_message', {session_id: sid, text: text});
  }

  // Optimistic UI update
  delete waitingData[sid];
  sessionKinds[sid] = 'working';
  closeRespond();
  showToast('Response sent');
}

async function submitRespond() {
  const ri = document.getElementById('respond-input');
  const text = ri.value.trim();
  if (text) {
    _resetTextareaHeight(ri);
    await sendRespond(text);
  }
}

document.getElementById('respond-overlay').addEventListener('click', function(e) {
  if (e.target === this) closeRespond();
});

// Close modal on overlay click
document.getElementById('rename-overlay').addEventListener('click', function(e) {
  if (e.target === this) closeRename();
});
document.getElementById('git-sync-overlay').addEventListener('click', function(e) {
  if (e.target === this) closeGitSyncModal();
});
