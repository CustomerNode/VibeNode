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

  Array.from(inner.children).forEach(btn => {
    const clone = btn.cloneNode(true);
    clone.style.removeProperty('display');
    // Wire the onclick — copy the attribute
    const oc = btn.getAttribute('onclick');
    if (oc) clone.setAttribute('onclick', oc);
    clone.addEventListener('click', () => { closeAllGrpDropdowns(); });
    popup.appendChild(clone);
  });

  // Position below the label
  const rect = label.getBoundingClientRect();
  popup.style.top  = (rect.bottom + 4) + 'px';
  popup.style.left = rect.left + 'px';
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
  document.getElementById('respond-question').textContent = w.question || '(no question text)';

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
