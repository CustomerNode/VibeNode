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

// --- Summary modal ---
async function showSummary(id) {
  document.getElementById('summary-body').innerHTML = '<div style="color:#555;font-size:13px;"><span class="spinner"></span> Building summary\u2026</div>';
  document.getElementById('summary-overlay').classList.add('show');

  const resp = await fetch('/api/summary/' + id);
  const data = await resp.json();
  document.getElementById('summary-body').innerHTML = data.html || ('<p style="color:#888">' + (data.error||'No summary available') + '</p>');
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

  // Add session status header for Session group
  if (grpId === 'grp-session' && activeId) {
    const kind = sessionKinds[activeId] || 'sleeping';
    const icons = {
      question: '<svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="#ff9500" stroke-width="2"><circle cx="12" cy="12" r="10"/><path d="M9.09 9a3 3 0 0 1 5.83 1c0 2-3 3-3 3"/><circle cx="12" cy="17" r=".5" fill="#ff9500"/></svg>',
      working: '<img src="/static/svg/pickaxe.svg" width="12" height="12" style="filter:brightness(0) saturate(100%) invert(55%) sepia(78%) saturate(1000%) hue-rotate(215deg);">',
      idle: '<svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="var(--idle-label)" stroke-width="2"><polyline points="20 6 9 17 4 12"/></svg>',
      sleeping: '<img src="/static/svg/sleeping.svg" width="12" height="12" class="sleeping-icon">',
    };
    const labels = {question:'Waiting for input', working:'Working', idle:'Idle', sleeping:'Not running'};
    const hdr = document.createElement('div');
    hdr.style.cssText = 'display:flex;align-items:center;gap:6px;padding:8px 12px 6px;font-size:11px;color:var(--text-faint);border-bottom:1px solid var(--border-subtle);margin-bottom:4px;';
    hdr.innerHTML = (icons[kind] || icons.sleeping) + ' ' + (labels[kind] || 'Not running');
    popup.appendChild(hdr);
  }

  Array.from(inner.children).forEach(btn => {
    // Skip permanently hidden buttons
    if (btn.style.display === 'none') return;
    const clone = btn.cloneNode(true);
    const oc = btn.getAttribute('onclick');
    if (oc) clone.setAttribute('onclick', oc);
    clone.addEventListener('click', () => { closeAllGrpDropdowns(); });
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
    document.getElementById('respond-input').focus();
  }, 60);
}

function closeRespond() {
  document.getElementById('respond-overlay').classList.remove('open');
  respondTarget = null;
}

async function sendRespond(text) {
  if (!text || !respondTarget) return;
  const sendBtn = document.getElementById('respond-send');
  sendBtn.disabled = true; sendBtn.textContent = 'Sending\u2026';
  try {
    const ctrl = new AbortController();
    const timer = setTimeout(() => ctrl.abort(), 8000);
    const r = await fetch('/api/respond/' + respondTarget, {
      method: 'POST', headers: {'Content-Type':'application/json'},
      body: JSON.stringify({text}), signal: ctrl.signal
    });
    clearTimeout(timer);
    const d = await r.json();
    if (d.method === 'sent') {
      closeRespond();
      setTimeout(pollWaiting, 1000);
    } else if (d.method === 'clipboard') {
      closeRespond();
      showAlert('Copied to Clipboard', '<p>' + escHtml(d.message) + '</p>', { icon: '\uD83D\uDCCB' });
    } else {
      showAlert('Send Failed', '<p>' + escHtml(d.err || d.method) + '</p>', { icon: '\u26A0\uFE0F' });
    }
  } catch(e) {
    if (e.name === 'AbortError') showAlert('Timed Out', '<p>Response copied to clipboard. Switch to your terminal and paste.</p>', { icon: '\u23F1\uFE0F' });
    else showAlert('Error', '<p>' + escHtml(e.message) + '</p>', { icon: '\u26A0\uFE0F' });
  }
  finally { sendBtn.disabled = false; sendBtn.textContent = 'Send \u21b5'; }
}

async function submitRespond() {
  const text = document.getElementById('respond-input').value.trim();
  if (text) await sendRespond(text);
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
