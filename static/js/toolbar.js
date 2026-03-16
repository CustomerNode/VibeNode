/* toolbar.js — toolbar session management, inline rename, message rendering, session actions */

function setToolbarSession(id, titleText, isUntitled, customTitle) {
  const titleEl = document.getElementById('main-title');
  titleEl.textContent = titleText;
  titleEl.className = 'session-name' + (isUntitled ? ' untitled' : '');
  titleEl.dataset.customTitle = customTitle || '';
  titleEl.dataset.editable = id ? 'true' : 'false';
  titleEl.title = id ? 'Click to rename' : '';
  ['btn-autoname','btn-open','btn-open-gui','btn-delete','btn-duplicate','btn-continue','btn-summary','btn-extract','btn-export'].forEach(b => {
    document.getElementById(b).disabled = !id;
  });
  // Hide entire toolbar when no session is selected
  document.getElementById('main-toolbar').style.display = id ? '' : 'none';
  // btn-close enabled when session is running or open in GUI
  const btnClose = document.getElementById('btn-close');
  if (btnClose) btnClose.disabled = !id || (!runningIds.has(id) && !guiOpenSessions.has(id));
}

function handleSessionClick(id) {
  if (id === activeId) { startListInlineRename(); } else { selectSession(id); }
}

async function handleNameClick(id) {
  if (id !== activeId) {
    await selectSession(id);   // first click — just select
  } else {
    startListInlineRename();   // second click on already-active row — rename
  }
}

async function selectSession(id) {
  activeId = id;
  // Stop live panel for a different session
  if (liveSessionId && liveSessionId !== id) stopLivePanel();
  filterSessions();

  setToolbarSession(id, 'Loading\u2026', true, '');
  document.getElementById('main-body').innerHTML =
    '<div class="empty-state"><div class="spinner"></div></div>';

  const resp = await fetch('/api/session/' + id);
  const s = await resp.json();

  const titleText = s.custom_title || s.display_title;
  setToolbarSession(id, titleText, !s.custom_title, s.custom_title || '');

  // Single click always shows static preview; double click / openInGUI starts live panel
  document.getElementById('main-body').innerHTML =
    '<div class="conversation" id="convo">' + renderMessages(s.messages) + '</div>';
  setTimeout(() => {
    const convo = document.getElementById('convo');
    if (convo) convo.scrollTop = convo.scrollHeight;
  }, 50);
}

function startListInlineRename() {
  if (!activeId) return;

  // Find the active row's name cell in the list
  const activeRow = document.querySelector('.session-item.active');
  if (!activeRow) return;
  const nameCell = activeRow.querySelector('.session-col-name');
  if (!nameCell) return;

  const s = allSessions.find(x => x.id === activeId);
  const current = (s && (s.custom_title || s.display_title)) || '';
  const originalHTML = nameCell.innerHTML;

  // Replace cell content with an input
  const input = document.createElement('input');
  input.style.cssText = 'width:100%;background:#1a1a2e;border:1px solid #7c7cff;border-radius:4px;padding:2px 6px;color:#fff;font-size:12px;outline:none;';
  input.value = current;
  input.placeholder = 'Enter a name\u2026';
  nameCell.innerHTML = '';
  nameCell.appendChild(input);

  // Prevent row click from firing while editing
  activeRow.onclick = null;
  input.focus();
  input.select();  // all text selected — edit in place or Delete to clear

  let committed = false;
  async function commit() {
    if (committed) return;
    committed = true;
    const val = input.value.trim();
    // Restore click handler
    activeRow.onclick = () => handleSessionClick(activeId);

    if (!val || val === current) {
      nameCell.innerHTML = originalHTML;
      return;
    }

    const resp = await fetch('/api/rename/' + activeId, {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({title: val})
    });
    const data = await resp.json();
    if (data.ok) {
      if (s) { s.custom_title = data.title; s.display_title = data.title; }
      setToolbarSession(activeId, data.title, false, data.title);
      nameCell.textContent = data.title;
      showToast('Renamed to "' + data.title + '"');
    } else {
      nameCell.innerHTML = originalHTML;
      showToast(data.error || 'Rename failed', true);
    }
  }

  input.addEventListener('keydown', e => {
    if (e.key === 'Enter') { e.preventDefault(); commit(); }
    if (e.key === 'Escape') { committed = true; activeRow.onclick = () => handleSessionClick(activeId); nameCell.innerHTML = originalHTML; }
  });
  input.addEventListener('blur', commit);
}

function submitListInlineRename() {
  // Alias for compatibility — commit is handled by blur/enter in startListInlineRename
}

function cancelListInlineRename() {
  // Alias for compatibility — escape is handled in startListInlineRename
}

function renderMessages(messages) {
  if (!messages || !messages.length) return '<div style="color:#444;font-size:13px;">No messages</div>';
  return messages.map(m => {
    let body;
    if (m.role === 'assistant') {
      body = mdParse(m.content || '');
    } else {
      body = '<pre style="white-space:pre-wrap;margin:0;">' + escHtml(m.content || '(empty)') + '</pre>';
    }
    return `<div class="msg ${m.role}">
      <div class="msg-role">${m.role}</div>
      <div class="msg-body msg-content">${body}</div>
    </div>`;
  }).join('');
}

function openRename(id, currentTitle) {
  renameTarget = id;
  const input = document.getElementById('rename-input');
  input.value = currentTitle || '';
  document.getElementById('rename-overlay').classList.add('show');
  setTimeout(() => { input.focus(); input.select(); }, 50);
}

function closeRename() {
  document.getElementById('rename-overlay').classList.remove('show');
  renameTarget = null;
}

async function submitRename() {
  const title = document.getElementById('rename-input').value.trim();
  if (!title || !renameTarget) return;

  const resp = await fetch('/api/rename/' + renameTarget, {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({title})
  });
  const data = await resp.json();
  closeRename();

  if (data.ok) {
    // Update local list
    const s = allSessions.find(x => x.id === renameTarget);
    if (s) { s.custom_title = data.title; s.display_title = data.title; }
    filterSessions();
    // Update toolbar title
    const titleEl = document.getElementById('main-title');
    if (titleEl) { titleEl.textContent = data.title; titleEl.classList.remove('untitled'); }
    showToast('Renamed to "' + data.title + '"');
  } else {
    showToast(data.error || 'Rename failed', true);
  }
}

async function autoName(id) {
  const btn = document.getElementById('autoname-btn');
  if (btn) { btn.disabled = true; btn.innerHTML = '<span class="spinner"></span>Naming\u2026'; }

  let data;
  try {
    const resp = await fetch('/api/autonname/' + id, { method: 'POST' });
    data = await resp.json();
  } catch(e) {
    if (btn) { btn.disabled = false; btn.textContent = 'Auto-name'; }
    showToast('Auto-name failed: ' + e.message, true);
    return;
  }

  if (btn) { btn.disabled = false; btn.textContent = 'Auto-name'; }

  if (data.ok) {
    const s = allSessions.find(x => x.id === id);
    if (s) { s.custom_title = data.title; s.display_title = data.title; }
    filterSessions();
    const titleEl = document.getElementById('main-title');
    if (titleEl) { titleEl.textContent = data.title; titleEl.classList.remove('untitled'); }
    showToast('Auto-named: "' + data.title + '"');
  } else {
    showToast('Auto-name failed: ' + (data.error || 'unknown error'), true);
  }
}

async function deleteSession(id) {
  const s = allSessions.find(x => x.id === id);
  const name = (s && s.display_title) || id.slice(0, 8);
  const confirmed = await showConfirm('Delete Session', '<p>Delete <strong>' + escHtml(name) + '</strong>?</p><p>This cannot be undone.</p>', { danger: true, confirmText: 'Delete', icon: '\uD83D\uDDD1\uFE0F' });
  if (!confirmed) return;

  const resp = await fetch('/api/delete/' + id, { method: 'DELETE' });
  const data = await resp.json();

  if (data.ok) {
    allSessions = allSessions.filter(x => x.id !== id);
    activeId = null;
    filterSessions();
    document.getElementById('session-count').textContent = allSessions.length + ' sessions';
    setToolbarSession(null, 'No session selected', true, '');
    document.getElementById('main-body').innerHTML =
      '<div class="empty-state"><div class="icon">\uD83D\uDDD1</div><div>Session deleted</div></div>';
    showToast('Session deleted');
  } else {
    showToast('Delete failed', true);
  }
}

async function deleteEmptySessions() {
  const empty = allSessions.filter(s => s.message_count === 0);
  if (!empty.length) { showToast('No empty sessions found'); return; }
  const confirmed = await showConfirm('Delete Empty Sessions', `<p>Delete <strong>${empty.length}</strong> empty session${empty.length > 1 ? 's' : ''}?</p>`, { danger: true, confirmText: 'Delete All', icon: '\uD83E\uDDF9' });
  if (!confirmed) return;

  const resp = await fetch('/api/delete-empty', { method: 'DELETE' });
  const data = await resp.json();

  if (data.ok) {
    allSessions = allSessions.filter(s => s.message_count > 0);
    if (empty.find(s => s.id === activeId)) {
      activeId = null;
      setToolbarSession(null, 'No session selected', true, '');
      document.getElementById('main-body').innerHTML =
        '<div class="empty-state"><div class="icon">\uD83D\uDDD1</div><div>Sessions deleted</div></div>';
    }
    filterSessions();
    document.getElementById('session-count').textContent = allSessions.length + ' sessions';
    showToast(`Deleted ${data.deleted} empty session${data.deleted !== 1 ? 's' : ''}`);
  } else {
    showToast('Delete failed', true);
  }
}

async function duplicateSession(id) {
  const resp = await fetch('/api/duplicate/' + id, { method: 'POST' });
  const data = await resp.json();
  if (data.ok) {
    await loadSessions();
    showToast('Session duplicated');
  } else {
    showToast('Duplicate failed: ' + (data.error || 'unknown'), true);
  }
}

async function continueSession(id) {
  const btn = document.getElementById('btn-continue');
  btn.disabled = true; btn.textContent = 'Building\u2026';

  const resp = await fetch('/api/continue/' + id, { method: 'POST' });
  const data = await resp.json();

  btn.disabled = false; btn.textContent = 'Continue Session';

  if (data.ok) {
    await loadSessions();
    // Select and open the new session
    await selectSession(data.new_id);
    showToast('New continuation session created \u2014 open it in Claude to continue');
  } else {
    showToast('Failed: ' + (data.error || 'unknown'), true);
  }
}

async function openInClaude(id) {
  const resp = await fetch('/api/open/' + id, { method: 'POST' });
  const data = await resp.json();
  if (data.ok) showToast('Opening session in Claude\u2026');
  else showToast('Failed to open: ' + (data.error || 'unknown'), true);
}

/* ---- Sidebar resize ---- */
(function() {
  const handle = document.getElementById('resize-handle');
  const sidebar = document.querySelector('.sidebar');
  let dragging = false, startX = 0, startW = 0;

  handle.addEventListener('mousedown', e => {
    dragging = true;
    startX = e.clientX;
    startW = sidebar.offsetWidth;
    handle.classList.add('dragging');
    document.body.style.cursor = 'col-resize';
    document.body.style.userSelect = 'none';
  });

  document.addEventListener('mousemove', e => {
    if (!dragging) return;
    const newW = Math.min(600, Math.max(180, startW + e.clientX - startX));
    document.documentElement.style.setProperty('--sidebar-w', newW + 'px');
  });

  document.addEventListener('mouseup', () => {
    if (!dragging) return;
    dragging = false;
    handle.classList.remove('dragging');
    document.body.style.cursor = '';
    document.body.style.userSelect = '';
  });
})();
