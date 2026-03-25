/* toolbar.js — toolbar session management, inline rename, message rendering, session actions */

function setToolbarSession(id, titleText, isUntitled, customTitle) {
  const titleEl = document.getElementById('main-title');
  titleEl.textContent = titleText;
  titleEl.className = 'session-name' + (isUntitled ? ' untitled' : '');
  titleEl.dataset.customTitle = customTitle || '';
  titleEl.dataset.editable = id ? 'true' : 'false';
  titleEl.title = id ? 'Click to rename' : '';
  ['btn-autoname','btn-open','btn-open-gui','btn-delete','btn-duplicate','btn-continue','btn-summary','btn-extract','btn-export','btn-fork','btn-rewind','btn-fork-rewind'].forEach(b => {
    document.getElementById(b).disabled = !id;
  });
  // Hide entire toolbar when no session is selected
  document.getElementById('main-toolbar').style.display = id ? '' : 'none';
  // btn-close enabled when session is running or open in GUI
  const btnClose = document.getElementById('btn-close');
  if (btnClose) btnClose.disabled = !id || (!runningIds.has(id) && !guiOpenSessions.has(id));
  // Reset cost badge and status bar on session switch
  const costEl = document.getElementById('session-cost');
  if (costEl) costEl.textContent = '$0.00';
  const sbCost = document.getElementById('sb-cost');
  if (sbCost) sbCost.textContent = '$0.00';
  const sbModel = document.getElementById('sb-model');
  if (sbModel) sbModel.textContent = '\u2014';
}

function deselectSession() {
  activeId = null;
  localStorage.removeItem('activeSessionId');
  _pushChatUrl(null);
  if (liveSessionId) stopLivePanel();
  // In workspace mode, return to workspace canvas instead of dashboard
  if (workspaceActive) {
    _wsExpandedId = null;
    const btn = document.getElementById('ws-back-btn');
    if (btn) btn.remove();
    document.getElementById('main-toolbar').style.display = 'none';
    filterSessions();
    return;
  }
  filterSessions();
  setToolbarSession(null, 'No session selected', true, '');
  document.getElementById('main-body').innerHTML = _buildDashboard();
}

function handleSessionClick(id) {
  if (id === activeId) { startListInlineRename(); } else { selectSession(id); }
}

async function handleNameClick(id) {
  if (id !== activeId) {
    openInGUI(id);
  } else {
    startListInlineRename();
  }
}

async function selectSession(id) {
  // In workspace mode, delegate to expandWorkspaceCard to prevent poll clobbering
  if (workspaceActive && typeof expandWorkspaceCard === 'function') {
    expandWorkspaceCard(id);
    return;
  }

  activeId = id;
  localStorage.setItem('activeSessionId', id || '');
  _pushChatUrl(id);
  // Stop live panel for a different session
  if (liveSessionId && liveSessionId !== id) stopLivePanel();
  filterSessions();

  setToolbarSession(id, 'Loading\u2026', true, '');
  document.getElementById('main-body').innerHTML = _chatSkeleton();

  const resp = await fetch('/api/session/' + id);
  const s = await resp.json();

  const titleText = s.custom_title || s.display_title;
  setToolbarSession(id, titleText, !s.custom_title, s.custom_title || '');

  // Single click always shows static preview; double click / openInGUI starts live panel
  // For very long sessions, show only the last 200 messages with a "Load more" button
  const MAX_INITIAL = 200;
  const msgs = s.messages || [];
  const truncated = msgs.length > MAX_INITIAL;
  const visibleMsgs = truncated ? msgs.slice(-MAX_INITIAL) : msgs;
  let loadMoreHtml = '';
  if (truncated) {
    loadMoreHtml = '<div style="text-align:center;padding:12px;"><button class="btn" id="btn-load-all" onclick="loadAllMessages(\'' + id + '\')">'
      + 'Load all ' + msgs.length + ' messages</button></div>';
  }
  document.getElementById('main-body').innerHTML =
    '<div class="conversation" id="convo">' + loadMoreHtml + renderMessages(visibleMsgs) + '</div>';
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
  input.style.cssText = 'width:100%;background:var(--bg-input);border:1px solid var(--border-focus);border-radius:4px;padding:2px 6px;color:var(--text-primary);font-size:12px;outline:none;';
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

function _buildDashboard() {
  const project = _allProjects.find(p => p.encoded === localStorage.getItem('activeProject'));
  const projectName = project ? _projectShortName(project) : 'No project';
  const total = allSessions.length;
  const polled = _waitingPolledOnce || false;
  const working = polled ? allSessions.filter(s => runningIds.has(s.id) && sessionKinds[s.id] === 'working').length : '-';
  const idle = polled ? allSessions.filter(s => runningIds.has(s.id) && sessionKinds[s.id] === 'idle').length : '-';
  const question = polled ? allSessions.filter(s => runningIds.has(s.id) && sessionKinds[s.id] === 'question').length : '-';
  const sleeping = polled ? total - (typeof working === 'number' ? working + idle + question : 0) : '-';

  const stats = [
    {label: 'Working', count: working, color: 'var(--accent)', icon: '<img src="/static/svg/pickaxe.svg" width="16" height="16" style="filter:brightness(0) saturate(100%) invert(55%) sepia(78%) saturate(1000%) hue-rotate(215deg);">'},
    {label: 'Waiting', count: question, color: '#ff9500', icon: '<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="#ff9500" stroke-width="2" stroke-linecap="round"><circle cx="12" cy="12" r="10"/><path d="M9.09 9a3 3 0 0 1 5.83 1c0 2-3 3-3 3"/><circle cx="12" cy="17" r=".5" fill="#ff9500"/></svg>'},
    {label: 'Idle', count: idle, color: 'var(--idle-label)', icon: '<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="var(--idle-label)" stroke-width="2" stroke-linecap="round"><polyline points="20 6 9 17 4 12"/></svg>'},
    {label: 'Sleeping', count: sleeping, color: 'var(--text-faint)', icon: '<img src="/static/svg/sleeping.svg" width="16" height="16" class="sleeping-icon">'},
  ];

  return `
  <div class="dashboard">
    <div class="dash-header">
      <div class="dash-project" onclick="openProjectOverlay()">
        <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5"><path d="M22 19a2 2 0 0 1-2 2H4a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h5l2 3h9a2 2 0 0 1 2 2z"/></svg>
        <div>
          <div class="dash-project-name">${escHtml(projectName)}</div>
          <div class="dash-project-sub">${total} sessions</div>
        </div>
        <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" style="margin-left:auto;opacity:0.4;"><polyline points="6 9 12 15 18 9"/></svg>
      </div>
    </div>

    <div class="dash-stats">
      ${stats.map(s => `
        <div class="dash-stat">
          <div class="dash-stat-icon">${s.icon}</div>
          <div class="dash-stat-count" style="color:${s.color}">${s.count}</div>
          <div class="dash-stat-label">${s.label}</div>
        </div>
      `).join('')}
    </div>

    <button class="dash-new-btn" onclick="addNewAgent()">
      <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><line x1="12" y1="5" x2="12" y2="19"/><line x1="5" y1="12" x2="19" y2="12"/></svg>
      New Session
    </button>

    <div class="dash-hint">
      <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5" style="opacity:0.3;flex-shrink:0;"><polyline points="15 18 9 12 15 6"/></svg>
      <span>${total > 0 ? 'Select a session from the sidebar to view its conversation' : 'Select a project to get started'}</span>
    </div>
  </div>`;
}

function dashStartSession() {
  const input = document.getElementById('dash-new-input');
  const text = input ? input.value.trim() : '';
  if (!text) { showToast('Type a message first'); return; }
  // TODO: wire to actual session creation
  showToast('Starting session\u2026');
  addNewAgent();
}

function _colorDiffLines(html) {
  return html.split('\n').map(line => {
    if (/^\+[^+]/.test(line)) return '<span style="color:var(--idle-label);opacity:0.8;">' + line + '</span>';
    if (/^-[^-]/.test(line)) return '<span style="color:var(--result-err);opacity:0.8;">' + line + '</span>';
    if (/^@@/.test(line)) return '<span style="color:var(--accent);opacity:0.6;">' + line + '</span>';
    return line;
  }).join('\n');
}

function _chatSkeleton() {
  let html = '<div class="conversation">';
  const msgs = [
    {role:'user', lines:2},
    {role:'asst', lines:4},
    {role:'user', lines:1},
    {role:'asst', lines:6},
    {role:'user', lines:2},
    {role:'asst', lines:5},
  ];
  for (let i = 0; i < msgs.length; i++) {
    const m = msgs[i];
    const d = (i * 0.1).toFixed(2);
    const isUser = m.role === 'user';
    html += `<div class="msg ${isUser ? 'user' : 'assistant'}" style="margin-bottom:20px;">`;
    // Role label skeleton
    html += `<div style="margin-bottom:5px;"><div class="skel-bar" style="width:40px;height:7px;animation-delay:${d}s;border-radius:3px;"></div></div>`;
    // Bubble skeleton — same style for both sides
    const bw = isUser ? (200 + Math.random() * 150) : (300 + Math.random() * 200);
    html += `<div class="msg-body" style="width:${bw}px;max-width:85%;padding:14px 16px;">`;
    for (let l = 0; l < m.lines; l++) {
      const lw = l === m.lines - 1 ? (40 + Math.random() * 35) : (75 + Math.random() * 25);
      html += `<div class="skel-bar" style="width:${lw}%;height:11px;margin-bottom:${l < m.lines - 1 ? 8 : 0}px;border-radius:4px;animation-delay:${(parseFloat(d) + l * 0.04).toFixed(2)}s;"></div>`;
    }
    html += '</div></div>';
  }
  html += '</div>';
  return html;
}

async function loadAllMessages(id) {
  const btn = document.getElementById('btn-load-all');
  if (btn) { btn.disabled = true; btn.innerHTML = '<span class="spinner"></span> Loading...'; }
  const resp = await fetch('/api/session/' + id);
  const s = await resp.json();
  document.getElementById('main-body').innerHTML =
    '<div class="conversation" id="convo">' + renderMessages(s.messages) + '</div>';
  setTimeout(() => {
    const convo = document.getElementById('convo');
    if (convo) convo.scrollTop = convo.scrollHeight;
  }, 50);
}

function _cleanUserContent(text) {
  // Strip all XML-like system tags injected by Claude Code / IDE
  return text
    .replace(/<[a-z_-]+(?:\s[^>]*)?>[\s\S]*?<\/[a-z_-]+>/g, '')  // matched pairs
    .replace(/<[a-z_-]+(?:\s[^>]*)?\/>/g, '')  // self-closing
    .replace(/<[a-z_-]+(?:\s[^>]*)?>[\s\S]*$/g, '')  // unclosed tag to end
    .trim();
}

function _isSystemMessage(text) {
  const t = text.trim();
  return /^<[a-z_-]+[\s>]/.test(t) ||
    /^This (session is being continued|is a continuation)/.test(t) ||
    /^The user (opened|selected|is viewing)/.test(t) ||
    /^\*\*What we were working on/.test(t) ||
    /^\*\*Key context/.test(t) ||
    /^\*\*Most recent exchanges/.test(t);
}

function renderMessages(messages) {
  if (!messages || !messages.length) return '<div class="empty-state" style="padding:40px 0;"><div style="color:var(--text-faint);font-size:13px;">No messages yet</div></div>';
  return messages.filter(m => m.content).map(m => {
    // Tool use: gear icon + tool names
    if (m.type === 'tool') {
      const names = m.content.replace(/[\[\]]/g, '');
      return `<div class="live-entry live-entry-tool">
        <div class="live-tool-line" onclick="this.nextElementSibling.classList.toggle('open')">
          <span class="live-tool-icon"><svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><circle cx="12" cy="12" r="3"/><path d="M19.4 15a1.65 1.65 0 0 0 .33 1.82l.06.06a2 2 0 0 1-2.83 2.83l-.06-.06a1.65 1.65 0 0 0-1.82-.33 1.65 1.65 0 0 0-1 1.51V21a2 2 0 0 1-4 0v-.09A1.65 1.65 0 0 0 9 19.4a1.65 1.65 0 0 0-1.82.33l-.06.06a2 2 0 0 1-2.83-2.83l.06-.06A1.65 1.65 0 0 0 4.68 15a1.65 1.65 0 0 0-1.51-1H3a2 2 0 0 1 0-4h.09A1.65 1.65 0 0 0 4.6 9"/></svg></span>
          <span class="live-tool-name">${escHtml(names)}</span>
          <button class="live-expand-btn">\u25be</button>
        </div>
        <div class="live-tool-detail">${escHtml(m.content)}</div>
      </div>`;
    }
    // Tool result: expandable output
    if (m.type === 'tool_result') {
      const text = m.content;
      const isShort = text.split('\n').length <= 6;
      return `<div class="live-entry live-entry-result">
        <div class="live-result-line live-result-ok" onclick="this.nextElementSibling.classList.toggle('open')" style="cursor:pointer;">
          <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" style="vertical-align:middle;margin-right:4px;"><polyline points="20 6 9 17 4 12"/></svg>${escHtml(text.slice(0, 80))}${text.length > 80 ? '\u2026' : ''}
        </div>
        <div class="live-tool-detail${isShort ? ' open' : ''}">${mdParse(_colorDiffLines(escHtml(text)))}</div>
      </div>`;
    }
    // User message
    if (m.role === 'user') {
      const cleaned = _cleanUserContent(m.content);
      if (!cleaned) return ''; // skip empty after cleaning
      // System-injected messages render as context, not "me"
      if (_isSystemMessage(m.content)) {
        return `<div class="live-entry live-entry-result">
          <div class="live-result-line" style="color:var(--text-faint);cursor:pointer;" onclick="this.nextElementSibling.classList.toggle('open')">
            <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" style="vertical-align:middle;margin-right:4px;"><circle cx="12" cy="12" r="10"/><line x1="12" y1="16" x2="12" y2="12"/><line x1="12" y1="8" x2="12.01" y2="8"/></svg>${escHtml(cleaned.slice(0, 100))}${cleaned.length > 100 ? '\u2026' : ''}
          </div>
          <div class="live-tool-detail">${mdParse(escHtml(cleaned))}</div>
        </div>`;
      }
      return `<div class="msg user">
        <div class="msg-role">me</div>
        <div class="msg-body msg-content">${mdParse(cleaned)}</div>
      </div>`;
    }
    // Assistant message
    return `<div class="msg assistant">
      <div class="msg-role">claude</div>
      <div class="msg-body msg-content">${mdParse(m.content || '')}</div>
    </div>`;
  }).join('');
}

async function startToolbarRename() {
  if (!activeId) return;
  const titleEl = document.getElementById('main-title');
  const current = titleEl.dataset.customTitle || titleEl.textContent;
  const newName = await showPrompt('Rename Session', '<p>Enter a new name for this session.</p>', {
    value: current,
    confirmText: 'Save',
    placeholder: 'Session name',
  });
  if (newName === null) return;
  const resp = await fetch('/api/rename/' + activeId, {
    method: 'POST', headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({title: newName})
  });
  const data = await resp.json();
  if (data.ok) {
    setToolbarSession(activeId, newName || activeId, !newName, newName);
    const s = allSessions.find(x => x.id === activeId);
    if (s) { s.custom_title = newName; s.display_title = newName || s.display_title; }
    filterSessions();
    showToast('Renamed');
  } else {
    showToast(data.error || 'Rename failed', true);
  }
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

  // Save renameTarget before closeRename() nulls it
  const targetId = renameTarget;
  const resp = await fetch('/api/rename/' + targetId, {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({title})
  });
  const data = await resp.json();
  closeRename();

  if (data.ok) {
    // Update local list
    const s = allSessions.find(x => x.id === targetId);
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

const _autoNamingInFlight = new Set();

async function autoName(id, silent) {
  const btn = silent ? null : document.getElementById('btn-autoname');
  const btnOrigHtml = btn ? btn.innerHTML : '';
  if (btn) { btn.disabled = true; btn.innerHTML = '<span class="spinner"></span>Naming\u2026'; }

  // Mark in-flight so the sidebar can show a "Naming\u2026" indicator
  _autoNamingInFlight.add(id);
  _updateNamingIndicator(id);

  let data;
  try {
    const resp = await fetch('/api/autonname/' + id, { method: 'POST' });
    data = await resp.json();
  } catch(e) {
    _autoNamingInFlight.delete(id);
    _updateNamingIndicator(id);
    if (btn) { btn.disabled = false; btn.innerHTML = btnOrigHtml; }
    if (!silent) showToast('Auto-name failed: ' + e.message, true);
    return;
  }

  _autoNamingInFlight.delete(id);
  if (btn) { btn.disabled = false; btn.innerHTML = btnOrigHtml; }

  if (data.ok) {
    const s = allSessions.find(x => x.id === id);
    if (s) { s.custom_title = data.title; s.display_title = data.title; }
    filterSessions();
    // Only touch the toolbar title if this is the active session
    if (id === activeId) {
      const titleEl = document.getElementById('main-title');
      if (titleEl) { titleEl.textContent = data.title; titleEl.classList.remove('untitled'); }
    }
    if (!silent) showToast('Auto-named: "' + data.title + '"');
  } else {
    _updateNamingIndicator(id);
    if (!silent) showToast('Auto-name failed: ' + (data.error || 'unknown error'), true);
  }
}

/** Update the "Naming…" indicator on a sidebar row without a full re-render. */
function _updateNamingIndicator(id) {
  const row = document.querySelector('.session-item[data-sid="' + id + '"]');
  if (!row) return;
  const nameCell = row.querySelector('.session-col-name');
  if (!nameCell) return;
  const existing = nameCell.querySelector('.naming-badge');
  if (_autoNamingInFlight.has(id)) {
    if (!existing) {
      const badge = document.createElement('span');
      badge.className = 'naming-badge';
      badge.innerHTML = '<span class="naming-dot"></span>Naming\u2026';
      nameCell.appendChild(badge);
    }
  } else if (existing) {
    existing.remove();
  }
}

async function deleteSession(id) {
  const s = allSessions.find(x => x.id === id);
  const name = (s && s.display_title) || id.slice(0, 8);
  const confirmed = await showConfirm('Delete Session', '<p>Delete <strong>' + escHtml(name) + '</strong>?</p><p>This cannot be undone.</p>', { danger: true, confirmText: 'Delete', icon: '<svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><polyline points="3 6 5 6 21 6"/><path d="M19 6v14a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2V6m3 0V4a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2"/></svg>' });
  if (!confirmed) return;

  // Close the session if it's still running
  if (runningIds.has(id)) {
    showToast('Stopping session\u2026');
    socket.emit('close_session', {session_id: id});
    guiOpenDelete(id);
    runningIds.delete(id);
  }

  showToast('Deleting session\u2026');
  const resp = await fetch('/api/delete/' + id, { method: 'DELETE' });
  const data = await resp.json();

  // Always clean up UI even if backend file doesn't exist (new sessions)
  if (data.ok || resp.status === 404) {
    allSessions = allSessions.filter(x => x.id !== id);
    // Remove from folder tree
    if (typeof removeSessionFromAllFolders === 'function') removeSessionFromAllFolders(id);
    if (liveSessionId === id) stopLivePanel();
    deselectSession();
    document.getElementById('search').placeholder = 'Search ' + allSessions.length + ' sessions\u2026';
    showToast('Session deleted');
  } else {
    showToast('Delete failed', true);
  }
}

async function deleteEmptySessions() {
  const empty = allSessions.filter(s => s.message_count === 0);
  if (!empty.length) { showToast('No empty sessions found'); return; }
  const confirmed = await showConfirm('Delete Empty Sessions', `<p>Delete <strong>${empty.length}</strong> empty session${empty.length > 1 ? 's' : ''}?</p>`, { danger: true, confirmText: 'Delete All', icon: '<svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><polyline points="3 6 5 6 21 6"/><path d="M19 6v14a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2V6m3 0V4a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2"/><line x1="10" y1="11" x2="10" y2="17"/><line x1="14" y1="11" x2="14" y2="17"/></svg>' });
  if (!confirmed) return;

  const resp = await fetch('/api/delete-empty', { method: 'DELETE' });
  const data = await resp.json();

  if (data.ok) {
    allSessions = allSessions.filter(s => s.message_count > 0);
    if (empty.find(s => s.id === activeId)) {
      if (workspaceActive) {
        _wsExpandedId = null;
        if (liveSessionId) stopLivePanel();
        activeId = null;
        document.getElementById('main-toolbar').style.display = 'none';
      } else {
        activeId = null;
        setToolbarSession(null, 'No session selected', true, '');
        document.getElementById('main-body').innerHTML =
          '<div class="empty-state"><div class="icon"><svg width="32" height="32" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round"><polyline points="3 6 5 6 21 6"/><path d="M19 6v14a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2V6m3 0V4a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2"/></svg></div><div>Sessions deleted</div></div>';
      }
    }
    filterSessions();
    const sessionCountEl = document.getElementById('session-count');
    if (sessionCountEl) sessionCountEl.textContent = allSessions.length + ' sessions';
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
  const btnOrigHtml = btn ? btn.innerHTML : '';
  if (btn) { btn.disabled = true; btn.innerHTML = '<span class="spinner"></span> Building\u2026'; }

  const resp = await fetch('/api/continue/' + id, { method: 'POST' });
  const data = await resp.json();

  if (btn) { btn.disabled = false; btn.innerHTML = btnOrigHtml; }

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

/* ---- Fork / Rewind / Fork+Rewind message picker ---- */

let _pickerSelectedLine = null;
let _pickerMode = null;
let _pickerSessionId = null;

async function showMessagePicker(sessionId, mode) {
  _pickerMode = mode;
  _pickerSessionId = sessionId;
  _pickerSelectedLine = null;

  const titles = {
    'fork': 'Fork Conversation',
    'rewind': 'Rewind Code',
    'fork-rewind': 'Fork + Rewind Code',
  };
  const descs = {
    'fork': 'Create a new session forked from the selected message. All messages after it will be excluded.',
    'rewind': 'Restore source files to the state they were in at the selected message.',
    'fork-rewind': 'Fork the conversation AND restore source files to the selected message.',
  };

  const overlay = document.getElementById('pm-overlay');
  overlay.innerHTML = `
    <div class="pm-card pm-enter" style="width:680px;max-width:94vw;max-height:85vh;display:flex;flex-direction:column;">
      <h2 class="pm-title">${escHtml(titles[mode] || 'Select Message')}</h2>
      <div class="pm-body" style="margin-bottom:12px;flex-shrink:0;">
        <p>${descs[mode] || ''}</p>
      </div>
      <div class="msg-timeline" id="msg-timeline" style="flex:1;overflow-y:auto;min-height:100px;">
        <div style="padding:24px;text-align:center;color:var(--text-faint);font-size:13px;">
          <span class="spinner"></span> Loading messages\u2026
        </div>
      </div>
      <div class="pm-actions" style="flex-shrink:0;padding-top:12px;">
        <button class="pm-btn pm-btn-secondary" id="pm-cancel">Cancel</button>
        <button class="pm-btn pm-btn-primary" id="pm-confirm" disabled>${escHtml(titles[mode] || 'Confirm')}</button>
      </div>
    </div>`;
  overlay.classList.add('show');
  requestAnimationFrame(() => overlay.querySelector('.pm-card').classList.remove('pm-enter'));

  document.getElementById('pm-cancel').onclick = () => _closePm();
  overlay.onclick = e => { if (e.target === overlay) _closePm(); };
  document.getElementById('pm-confirm').onclick = () => _confirmPicker();

  // Fetch timeline
  try {
    const url = '/api/session-timeline/' + sessionId;
    const resp = await fetch(url);
    if (!resp.ok) {
      let errMsg = 'HTTP ' + resp.status;
      try { const d = await resp.json(); errMsg = d.error || errMsg; } catch(e) {}
      document.getElementById('msg-timeline').innerHTML = '<div style="padding:20px;color:#ff9500;font-size:12px;">' + escHtml(errMsg) + '</div>';
      return;
    }
    const data = await resp.json();
    if (data.error) {
      document.getElementById('msg-timeline').innerHTML = '<div style="padding:20px;color:#ff9500;font-size:12px;">' + escHtml(data.error) + '</div>';
      return;
    }
    _renderTimeline(data.messages, data.has_snapshots, mode);
  } catch (e) {
    document.getElementById('msg-timeline').innerHTML = '<div style="padding:20px;color:#ff9500;font-size:12px;">Failed to load: ' + escHtml(e.message) + '</div>';
  }
}

function _renderTimeline(messages, hasSnapshots, mode) {
  const el = document.getElementById('msg-timeline');
  if (!messages || !messages.length) {
    el.innerHTML = '<div style="padding:20px;color:var(--text-faint);text-align:center;">No messages found in this session.</div>';
    return;
  }

  // For rewind modes, warn if no snapshots
  if ((mode === 'rewind' || mode === 'fork-rewind') && !hasSnapshots) {
    el.innerHTML = '<div style="padding:20px;color:#ff9500;text-align:center;">This session has no file snapshots. Code rewind is not available.</div>';
    return;
  }

  let html = '';
  for (const m of messages) {
    const roleClass = m.role === 'user' ? 'user' : 'assistant';
    const roleLabel = m.role === 'user' ? 'me' : 'claude';

    // Format timestamp
    let tsDisplay = '';
    if (m.ts) {
      try {
        const d = new Date(m.ts);
        tsDisplay = d.toLocaleTimeString([], {hour:'numeric', minute:'2-digit'});
      } catch(e) { tsDisplay = ''; }
    }

    // Change counts
    let changesHtml = '';
    if (m.changes && (m.changes.added || m.changes.removed)) {
      const parts = [];
      if (m.changes.added) parts.push('<span class="tl-add">+' + m.changes.added + '</span>');
      if (m.changes.removed) parts.push('<span class="tl-rem">-' + m.changes.removed + '</span>');
      changesHtml = parts.join(' ');
    }

    // File badges
    let filesHtml = '';
    if (m.changes && m.changes.files && m.changes.files.length) {
      filesHtml = '<span class="tl-files">' + m.changes.files.map(f => escHtml(f)).join(', ') + '</span>';
    }

    // Snapshot indicator
    const snapIcon = m.has_snapshot ? '<span class="tl-snap" title="File snapshot available">&#128190;</span>' : '';

    html += '<div class="tl-row" data-line="' + m.line_number + '" onclick="_selectTimelineRow(this)">'
      + '<span class="tl-idx">#' + (m.index + 1) + '</span>'
      + '<span class="tl-role ' + roleClass + '">' + roleLabel + '</span>'
      + '<span class="tl-preview">' + escHtml(m.preview) + '</span>'
      + (filesHtml ? '<span class="tl-file-wrap">' + filesHtml + '</span>' : '')
      + (changesHtml ? '<span class="tl-changes">' + changesHtml + '</span>' : '')
      + snapIcon
      + '<span class="tl-ts">' + escHtml(tsDisplay) + '</span>'
      + '</div>';
  }
  el.innerHTML = html;
}

function _selectTimelineRow(rowEl) {
  // Deselect previous
  const prev = document.querySelector('.tl-row.selected');
  if (prev) prev.classList.remove('selected');
  rowEl.classList.add('selected');
  _pickerSelectedLine = parseInt(rowEl.dataset.line, 10);
  const btn = document.getElementById('pm-confirm');
  if (btn) btn.disabled = false;
}

async function _confirmPicker() {
  if (!_pickerSelectedLine || !_pickerSessionId || !_pickerMode) return;

  const btn = document.getElementById('pm-confirm');
  if (btn) { btn.disabled = true; btn.innerHTML = '<span class="spinner"></span> Working\u2026'; }

  const body = JSON.stringify({ up_to_line: _pickerSelectedLine });
  const hdrs = { 'Content-Type': 'application/json' };

  try {
    let endpoint = '';
    if (_pickerMode === 'fork') endpoint = '/api/fork/';
    else if (_pickerMode === 'rewind') endpoint = '/api/rewind/';
    else endpoint = '/api/fork-rewind/';

    const resp = await fetch(endpoint + _pickerSessionId, { method: 'POST', headers: hdrs, body });
    const data = await resp.json();
    _closePm();

    if (data.error) {
      showToast(data.error, true);
      return;
    }

    if (_pickerMode === 'fork' || _pickerMode === 'fork-rewind') {
      await loadSessions();
      if (data.new_id) await selectSession(data.new_id);

      let msg = 'Session forked';
      if (data.files_restored && data.files_restored.length) {
        msg += ' and ' + data.files_restored.length + ' file(s) restored';
      }
      showToast(msg);
    } else {
      // rewind only
      let msg = '';
      if (data.files_restored && data.files_restored.length) {
        msg = data.files_restored.length + ' file(s) restored to earlier state';
      } else {
        msg = 'Rewind complete (no files to restore)';
      }
      if (data.files_skipped && data.files_skipped.length) {
        msg += ' (' + data.files_skipped.length + ' skipped)';
      }
      showToast(msg);
    }
  } catch (e) {
    _closePm();
    showToast('Operation failed: ' + e.message, true);
  }
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
