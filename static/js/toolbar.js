/* toolbar.js — toolbar session management, inline rename, message rendering, session actions */
let _hpBoardFetching = false;
let _hpBoardLoaded = false;
let _hpComposeFetching = false;
let _hpComposeLoaded = false;
let _hpComposeTasks = [];
let _hpComposeColumns = [];

function setToolbarSession(id, titleText, isUntitled, customTitle) {
  const titleEl = document.getElementById('main-title');
  titleEl.textContent = titleText;
  titleEl.className = 'session-name' + (isUntitled ? ' untitled' : '');
  titleEl.dataset.customTitle = customTitle || '';
  titleEl.dataset.editable = id ? 'true' : 'false';
  titleEl.title = id ? 'Click to rename' : '';
  // Enable/disable action buttons regardless of view mode — the Actions
  // popup uses these same buttons even in kanban mode.
  ['btn-autoname','btn-open','btn-open-gui','btn-delete','btn-duplicate','btn-continue','btn-summary','btn-extract','btn-export','btn-fork','btn-rewind','btn-fork-rewind'].forEach(b => {
    const el = document.getElementById(b);
    if (el) el.disabled = !id;
  });
  // In kanban mode, main-toolbar NEVER shows (actions are in the crumb bar)
  if (typeof viewMode !== 'undefined' && viewMode === 'kanban') {
    document.getElementById('main-toolbar').style.display = 'none';
    return;
  }
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
  if (viewMode === 'homepage') return;
  activeId = null;
  localStorage.removeItem('activeSessionId');
  _pushChatUrl(null);
  if (liveSessionId) { stopLivePanel(); }
  // In workspace mode, return to workspace canvas instead of dashboard
  if (workspaceActive) {
    _wsExpandedId = null;
    const btn = document.getElementById('ws-back-btn');
    if (btn) btn.remove();
    document.getElementById('main-toolbar').style.display = 'none';
    filterSessions();
    return;
  }
  // In kanban mode, restore the board
  if (typeof viewMode !== 'undefined' && viewMode === 'kanban') {
    const kb = document.getElementById('kanban-board');
    const mb = document.getElementById('main-body');
    if (kb) kb.style.display = '';
    if (mb) mb.style.display = 'none';
    document.getElementById('main-toolbar').style.display = 'none';
    const sessionBar = document.getElementById('kanban-session-bar');
    if (sessionBar) sessionBar.remove();
    window._kanbanSessionTaskId = null;
    if (typeof initKanban === 'function') initKanban(true);
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

/**
 * _ensureMainBodyVisible() — If in kanban mode, swap the board out and show
 * main-body + back button. Called before ANY action that renders into main-body.
 * Safe to call from any view — no-ops if not in kanban.
 */
function _ensureMainBodyVisible() {
  if (typeof viewMode === 'undefined' || viewMode !== 'kanban') return;
  // Don't touch kanban-board or main-body — the session renders inside kanban-board
  // with the kanban titlebar. openSessionSpawner in kanban.js handles this.
}

function _backToKanban() { deselectSession(); }
function _showKanbanBackBtn() {}

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

  _ensureMainBodyVisible();

  activeId = id;
  localStorage.setItem('activeSessionId', id || '');
  _pushChatUrl(id);
  // Save draft and stop live panel for a different session
  if (liveSessionId && liveSessionId !== id) {
    stopLivePanel();
  }
  filterSessions();

  setToolbarSession(id, 'Loading\u2026', true, '');
  document.getElementById('main-body').innerHTML = _chatSkeleton();

  const _proj = localStorage.getItem('activeProject') || '';
  const _projQ = _proj ? '?project=' + encodeURIComponent(_proj) : '';
  const resp = await fetch('/api/session/' + id + _projQ);
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
  _addCopyButtonsToConvo();
  setTimeout(() => {
    const convo = document.getElementById('convo');
    if (convo) {
      convo.scrollTop = convo.scrollHeight;
      if (typeof initStickyUserMessages === 'function') initStickyUserMessages(convo);
    }
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

    _userNamedSessions.add(activeId);  // kill auto-naming instantly
    const resp = await fetch('/api/rename/' + activeId, {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({title: val, project: localStorage.getItem('activeProject') || ''})
    });
    const data = await resp.json();
    if (data.ok) {
      _userNamedSessions.add(activeId);  // protect from auto-naming
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

function _buildHomepageContent() {
  const polled = _waitingPolledOnce || false;
  const total = allSessions.length;
  const working = polled ? allSessions.filter(s => runningIds.has(s.id) && sessionKinds[s.id] === 'working').length : 0;
  const idle = polled ? allSessions.filter(s => runningIds.has(s.id) && sessionKinds[s.id] === 'idle').length : 0;
  const question = polled ? allSessions.filter(s => runningIds.has(s.id) && sessionKinds[s.id] === 'question').length : 0;
  const active = working + idle + question;
  const sleeping = total - active;

  // Workflow stats — count tasks per column, use column colors
  // Kick off async fetch if board data hasn't been loaded yet
  if (typeof kanbanTasks !== 'undefined' && kanbanTasks.length === 0 && typeof kanbanColumns !== 'undefined' && kanbanColumns.length === 0 && !_hpBoardFetching) {
    _hpBoardFetching = true;
    fetch('/api/kanban/board').then(r => r.ok ? r.json() : null).then(data => {
      _hpBoardFetching = false;
      _hpBoardLoaded = true;
      if (data) {
        kanbanColumns = data.columns || [];
        kanbanTasks = data.tasks || [];
        if (data.tags && typeof kanbanAllTags !== 'undefined') kanbanAllTags = data.tags;
        // Re-render homepage if still visible
        if (typeof _updateHomepageStats === 'function') _updateHomepageStats();
      }
    }).catch(() => { _hpBoardFetching = false; _hpBoardLoaded = true; });
  }
  // If kanban data was already loaded (e.g. user visited board first), mark as loaded
  if (!_hpBoardLoaded && typeof kanbanTasks !== 'undefined' && (kanbanTasks.length > 0 || typeof _kanbanHasLoaded !== 'undefined' && _kanbanHasLoaded)) {
    _hpBoardLoaded = true;
  }
  const cols = (typeof kanbanColumns !== 'undefined') ? kanbanColumns : [];
  const tasks = (typeof kanbanTasks !== 'undefined') ? kanbanTasks : [];
  const taskTotal = tasks.length;
  const colCounts = [];
  let maxColCount = 0;
  for (const col of cols) {
    const count = tasks.filter(t => t.status === col.status_key).length;
    colCounts.push({ name: col.name, color: col.color || 'var(--border)', count });
    if (count > maxColCount) maxColCount = count;
  }
  // Build column bar viz
  const _wfStillLoading = !_hpBoardLoaded && (taskTotal === 0);
  let colBarsHtml = '';
  if (_wfStillLoading) {
    // Shimmer skeleton while data is loading
    colBarsHtml = '<div class="hp-col skel-shimmer" style="height:60%;"></div>'
      + '<div class="hp-col skel-shimmer" style="height:40%;animation-delay:0.15s;"></div>'
      + '<div class="hp-col skel-shimmer" style="height:75%;animation-delay:0.3s;"></div>'
      + '<div class="hp-col skel-shimmer" style="height:30%;animation-delay:0.45s;"></div>'
      + '<div class="hp-col skel-shimmer" style="height:55%;animation-delay:0.6s;"></div>';
  } else if (colCounts.length && taskTotal > 0) {
    for (const c of colCounts) {
      const pct = maxColCount > 0 ? Math.max(8, (c.count / maxColCount) * 100) : 8;
      colBarsHtml += `<div class="hp-col" style="height:${pct}%;background:${c.color};opacity:0.8;" title="${c.name}: ${c.count}"></div>`;
    }
  } else {
    // Placeholder columns when genuinely no data
    colBarsHtml = '<div class="hp-col" style="height:60%;background:var(--border);opacity:0.3;"></div>'
      + '<div class="hp-col" style="height:40%;background:var(--border);opacity:0.3;"></div>'
      + '<div class="hp-col" style="height:20%;background:var(--border);opacity:0.3;"></div>'
      + '<div class="hp-col" style="height:50%;background:var(--border);opacity:0.3;"></div>';
  }
  // Build workflow stat line
  let wfStatLine = '';
  if (_wfStillLoading) {
    wfStatLine = '<span class="skel-shimmer" style="display:inline-block;width:120px;height:13px;border-radius:4px;vertical-align:middle;"></span>';
  } else if (taskTotal > 0) {
    const parts = colCounts.filter(c => c.count > 0).map(c => `${c.count} ${c.name.toLowerCase()}`);
    wfStatLine = `${taskTotal} task${taskTotal !== 1 ? 's' : ''} &middot; ${parts.join(', ')}`;
  } else {
    wfStatLine = 'No tasks yet';
  }

  // Workforce stats
  const agentCount = (typeof FOLDER_SUPERSET === 'object' && FOLDER_SUPERSET)
    ? Object.keys(FOLDER_SUPERSET).filter(k => FOLDER_SUPERSET[k].skill).length : 0;
  const deptCount = (typeof FOLDER_SUPERSET === 'object' && FOLDER_SUPERSET)
    ? Object.keys(FOLDER_SUPERSET).filter(k => !FOLDER_SUPERSET[k].parentId).length : 0;

  // Session segmented bar
  const barParts = [];
  if (working) barParts.push(`<div style="flex:${working};background:var(--accent);height:100%;border-radius:3px;"></div>`);
  if (question) barParts.push(`<div style="flex:${question};background:#ff9500;height:100%;border-radius:3px;"></div>`);
  if (idle) barParts.push(`<div style="flex:${idle};background:var(--idle-label);height:100%;border-radius:3px;"></div>`);
  if (sleeping) barParts.push(`<div style="flex:${sleeping};background:var(--border);height:100%;border-radius:3px;"></div>`);
  const sessionBar = barParts.length
    ? barParts.join('')
    : '<div style="flex:1;background:var(--border);height:100%;border-radius:3px;"></div>';

  // Session stat line
  const sessionStat = total === 0 ? 'No sessions yet'
    : active > 0 ? `${working} working &middot; ${question} waiting &middot; ${idle} idle`
    : `${total} session${total !== 1 ? 's' : ''} &middot; all sleeping`;

  // Compose stats — async fetch like workflow
  if (!_hpComposeFetching && !_hpComposeLoaded) {
    _hpComposeFetching = true;
    fetch('/api/compose/board').then(r => r.ok ? r.json() : null).then(data => {
      _hpComposeFetching = false;
      _hpComposeLoaded = true;
      if (data) {
        _hpComposeTasks = data.tasks || [];
        _hpComposeColumns = data.columns || [];
        if (typeof _updateHomepageStats === 'function') _updateHomepageStats();
      }
    }).catch(() => { _hpComposeFetching = false; _hpComposeLoaded = true; });
  }
  const composeTasks = _hpComposeTasks || [];
  const composeCols = _hpComposeColumns || [];
  const composeTotal = composeTasks.length;
  const _compStillLoading = !_hpComposeLoaded && composeTotal === 0;

  // Compose column bar viz
  let composeColBarsHtml = '';
  const composeColCounts = [];
  let compMaxCol = 0;
  for (const col of composeCols) {
    const cnt = composeTasks.filter(t => t.status === col.status_key).length;
    composeColCounts.push({ name: col.name, color: col.color || 'var(--border)', count: cnt });
    if (cnt > compMaxCol) compMaxCol = cnt;
  }
  if (_compStillLoading) {
    composeColBarsHtml = '<div class="hp-col skel-shimmer" style="height:55%;"></div>'
      + '<div class="hp-col skel-shimmer" style="height:70%;animation-delay:0.15s;"></div>'
      + '<div class="hp-col skel-shimmer" style="height:35%;animation-delay:0.3s;"></div>'
      + '<div class="hp-col skel-shimmer" style="height:50%;animation-delay:0.45s;"></div>';
  } else if (composeColCounts.length && composeTotal > 0) {
    for (const c of composeColCounts) {
      const pct = compMaxCol > 0 ? Math.max(8, (c.count / compMaxCol) * 100) : 8;
      composeColBarsHtml += `<div class="hp-col" style="height:${pct}%;background:${c.color};opacity:0.8;" title="${c.name}: ${c.count}"></div>`;
    }
  } else {
    composeColBarsHtml = '<div class="hp-col" style="height:50%;background:var(--border);opacity:0.3;"></div>'
      + '<div class="hp-col" style="height:70%;background:var(--border);opacity:0.3;"></div>'
      + '<div class="hp-col" style="height:30%;background:var(--border);opacity:0.3;"></div>'
      + '<div class="hp-col" style="height:55%;background:var(--border);opacity:0.3;"></div>';
  }

  // Compose stat line
  let composeStatLine = '';
  if (_compStillLoading) {
    composeStatLine = '<span class="skel-shimmer" style="display:inline-block;width:120px;height:13px;border-radius:4px;vertical-align:middle;"></span>';
  } else if (composeTotal > 0) {
    const parts = composeColCounts.filter(c => c.count > 0).map(c => `${c.count} ${c.name.toLowerCase()}`);
    composeStatLine = `${composeTotal} section${composeTotal !== 1 ? 's' : ''} &middot; ${parts.join(', ')}`;
  } else {
    composeStatLine = 'No sections yet';
  }

  // Workforce dot grid — colored by department
  const deptColors = ['#58a6ff','#58a6ff','#58a6ff','#58a6ff',
    '#3fb950','#3fb950','#3fb950',
    '#d29922','#d29922',
    '#bc8cff','#bc8cff',
    '#39d2c0','#f85149','#e3b341'];
  let dotGridHtml = '';
  const dotCount = Math.min(agentCount, 36);
  for (let i = 0; i < dotCount; i++) {
    const c = deptColors[i % deptColors.length];
    const delay = (i * 0.02).toFixed(2);
    dotGridHtml += `<div class="hp-dot" style="background:${c};animation-delay:${delay}s"></div>`;
  }
  if (dotCount === 0) {
    for (let i = 0; i < 12; i++) {
      dotGridHtml += `<div class="hp-dot" style="background:var(--border);opacity:0.5;"></div>`;
    }
  }

  // Project selector for homepage
  const _hpProject = _allProjects ? _allProjects.find(p => p.encoded === localStorage.getItem('activeProject')) : null;
  const _hpProjectName = _hpProject ? _projectShortName(_hpProject) : 'No project';

  return `
  <div class="homepage">
    <div class="hp-project-trigger" onclick="openProjectOverlay()">
      <svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5"><path d="M22 19a2 2 0 0 1-2 2H4a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h5l2 3h9a2 2 0 0 1 2 2z"/></svg>
      <span class="hp-project-label">Project:</span>
      <span class="hp-project-name">${escHtml(_hpProjectName)}</span>
      <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" style="opacity:0.35;"><polyline points="6 9 12 15 18 9"/></svg>
    </div>
    <div class="homepage-cards">

      <div class="homepage-card hp-sessions" onclick="setViewMode('sessions')">
        <div class="hp-icon-wrap">
          <svg width="28" height="28" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round"><path d="M21 15a2 2 0 0 1-2 2H7l-4 4V5a2 2 0 0 1 2-2h14a2 2 0 0 1 2 2z"/></svg>
        </div>
        <h3>Sessions</h3>
        <p class="hp-desc">Interactive Claude Code terminals with live streaming, voice input, and permission management.</p>
        <div class="hp-viz">
          <div class="hp-bar">${sessionBar}</div>
        </div>
        <div class="hp-stat">${sessionStat}</div>
        <div class="hp-cta">Open Sessions <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><polyline points="9 18 15 12 9 6"/></svg></div>
      </div>

      <div class="homepage-card hp-workflow" onclick="setViewMode('kanban')">
        <div class="hp-icon-wrap">
          <svg width="28" height="28" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5"><rect x="3" y="3" width="5" height="18" rx="1"/><rect x="10" y="3" width="5" height="12" rx="1"/><rect x="17" y="3" width="5" height="15" rx="1"/></svg>
        </div>
        <h3>Workflow</h3>
        <p class="hp-desc">Hierarchical task board where your roadmap terminates in working Claude sessions at the leaves.</p>
        <div class="hp-viz">
          <div class="hp-columns">${colBarsHtml}</div>
        </div>
        <div class="hp-stat">${wfStatLine}</div>
        <div class="hp-cta">Open Workflow <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><polyline points="9 18 15 12 9 6"/></svg></div>
      </div>

      <div class="homepage-card hp-workforce" onclick="setViewMode('workplace')">
        <div class="hp-icon-wrap">
          <svg width="28" height="28" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5"><circle cx="12" cy="8" r="4"/><path d="M20 21a8 8 0 1 0-16 0"/><circle cx="20" cy="7" r="2.5"/><path d="M23 15a5 5 0 0 0-6 0"/><circle cx="4" cy="7" r="2.5"/><path d="M7 15a5 5 0 0 0-6 0"/></svg>
        </div>
        <h3>Workforce</h3>
        <p class="hp-desc">Knowledge asset library — skills and agent definitions organized into a department hierarchy.</p>
        <div class="hp-viz">
          <div class="hp-dots">${dotGridHtml}</div>
        </div>
        <div class="hp-stat">${agentCount} agent${agentCount !== 1 ? 's' : ''} &middot; ${deptCount} department${deptCount !== 1 ? 's' : ''}</div>
        <div class="hp-cta">Open Workforce <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><polyline points="9 18 15 12 9 6"/></svg></div>
      </div>

      <div class="homepage-card hp-compose" onclick="setViewMode('compose')">
        <div class="hp-icon-wrap">
          <svg width="28" height="28" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round"><path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"/><polyline points="14 2 14 8 20 8"/><line x1="16" y1="13" x2="8" y2="13"/><line x1="16" y1="17" x2="8" y2="17"/><polyline points="10 9 9 9 8 9"/></svg>
        </div>
        <h3>Compose</h3>
        <p class="hp-desc">Parallel content creation — multiple AI agents drafting sections simultaneously through a shared brain.</p>
        <div class="hp-viz">
          <div class="hp-columns">${composeColBarsHtml}</div>
        </div>
        <div class="hp-stat">${composeStatLine}</div>
        <div class="hp-cta">Open Compose <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><polyline points="9 18 15 12 9 6"/></svg></div>
      </div>

    </div>
  </div>`;
}

function _updateHomepageStats() {
  const el = document.getElementById('homepage-container');
  if (el && el.style.display !== 'none') {
    el.innerHTML = _buildHomepageContent();
  }
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
  const _proj2 = localStorage.getItem('activeProject') || '';
  const _projQ2 = _proj2 ? '?project=' + encodeURIComponent(_proj2) : '';
  const resp = await fetch('/api/session/' + id + _projQ2);
  const s = await resp.json();
  document.getElementById('main-body').innerHTML =
    '<div class="conversation" id="convo">' + renderMessages(s.messages) + '</div>';
  _addCopyButtonsToConvo();
  setTimeout(() => {
    const convo = document.getElementById('convo');
    if (convo) {
      convo.scrollTop = convo.scrollHeight;
      if (typeof initStickyUserMessages === 'function') initStickyUserMessages(convo);
    }
  }, 50);
}

/** Add smart copy buttons to all assistant messages in the conversation view */
function _addCopyButtonsToConvo() {
  if (typeof addSmartCopyButtons !== 'function') return;
  document.querySelectorAll('#convo .msg.assistant .msg-body').forEach(body => {
    addSmartCopyButtons(body, body.textContent);
  });
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
        <div class="msg-body msg-content"><pre style="white-space:pre-wrap;margin:0;">${escHtml(cleaned)}</pre></div>
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
  _userNamedSessions.add(activeId);  // kill auto-naming instantly
  const resp = await fetch('/api/rename/' + activeId, {
    method: 'POST', headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({title: newName, project: localStorage.getItem('activeProject') || ''})
  });
  const data = await resp.json();
  if (data.ok) {
    _userNamedSessions.add(activeId);  // protect from auto-naming
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
  _userNamedSessions.add(targetId);  // kill auto-naming instantly
  const resp = await fetch('/api/rename/' + targetId, {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({title, project: localStorage.getItem('activeProject') || ''})
  });
  const data = await resp.json();
  closeRename();

  if (data.ok) {
    _userNamedSessions.add(targetId);  // protect from auto-naming
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
// Sessions the user has manually renamed — auto-naming will never touch these.
const _userNamedSessions = new Set();

async function autoName(id, silent, reEvaluate, promptText) {
  // Never auto-name a session the user has explicitly renamed
  if (_userNamedSessions.has(id)) return;
  const btn = silent ? null : document.getElementById('btn-autoname');
  const btnOrigHtml = btn ? btn.innerHTML : '';
  if (btn) { btn.disabled = true; btn.innerHTML = '<span class="spinner"></span>Naming\u2026'; }

  // Mark in-flight so the sidebar can show a "Naming\u2026" indicator
  _autoNamingInFlight.add(id);
  _updateNamingIndicator(id);

  let data;
  try {
    const payload = reEvaluate ? { re_evaluate: true } : promptText ? { prompt: promptText } : {};
    payload.project = localStorage.getItem('activeProject') || '';
    const headers = { 'Content-Type': 'application/json' };
    const body = JSON.stringify(payload);
    const resp = await fetch('/api/autonname/' + id, { method: 'POST', headers, body });
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

  // If the user renamed this session while the request was in-flight, discard the auto-name result
  if (_userNamedSessions.has(id)) return;

  if (data.ok) {
    // If the session ID was remapped while the autoname LLM call was in-flight,
    // also save the name under the new ID so it isn't lost
    const remappedId = (window._idRemaps && window._idRemaps[id]) || null;
    const effectiveId = remappedId || id;

    const s = allSessions.find(x => x.id === effectiveId) || allSessions.find(x => x.id === id);
    if (s) { s.custom_title = data.title; s.display_title = data.title; }
    filterSessions();

    // Persist the name under the remapped ID too
    if (remappedId) {
      fetch('/api/remap-name', {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({old_id:id, new_id:remappedId, project: localStorage.getItem('activeProject') || ''})}).catch(()=>{});
    }

    // Update toolbar title if this is the active session
    if (effectiveId === activeId || id === activeId) {
      const titleEl = document.getElementById('main-title');
      if (titleEl) { titleEl.textContent = data.title; titleEl.classList.remove('untitled'); titleEl.dataset.customTitle = data.title; }
      // Also update kanban session title bar if present
      const kbTitle = document.querySelector('.kanban-session-title');
      if (kbTitle) kbTitle.textContent = data.title;
    }
    // Update kanban drill-down session row name and breadcrumb in-place
    const _kbRow = document.querySelector('.kanban-drill-session-row[data-session-id="' + effectiveId + '"] .kanban-drill-session-name')
      || document.querySelector('.kanban-drill-session-row[data-session-id="' + id + '"] .kanban-drill-session-name');
    if (_kbRow) _kbRow.textContent = data.title;
    const _kbCrumb = document.querySelector('#kanban-session-bar .kanban-drill-crumb.current');
    if (_kbCrumb && (effectiveId === activeId || id === activeId)) _kbCrumb.textContent = data.title;
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
  let deleteOk = false;
  try {
    const _dp = localStorage.getItem('activeProject') || '';
    const _dpQ = _dp ? '?project=' + encodeURIComponent(_dp) : '';
    const resp = await fetch('/api/delete/' + id + _dpQ, { method: 'DELETE' });
    // Server may return HTML 500 if unlink failed — guard the JSON parse
    try {
      const data = await resp.json();
      deleteOk = !!(data.ok) || resp.status === 404;
    } catch (_jsonErr) {
      // Non-JSON response (e.g. 500 HTML) — the server-side tombstone is
      // already set so the session won't reappear on reload.  Treat as ok
      // so the card is cleaned up immediately.
      deleteOk = true;
    }
  } catch (_fetchErr) {
    // Network error — still clean up the local card; a refresh will
    // reconcile with the server.
    deleteOk = true;
  }

  if (deleteOk) {
    allSessions = allSessions.filter(x => x.id !== id);
    allSessionIds.delete(id);
    // Clean up draft text for deleted session
    if (typeof _clearDraft === 'function') _clearDraft(id);
    // Remove from folder tree
    if (typeof removeSessionFromAllFolders === 'function') removeSessionFromAllFolders(id);
    // Unlink from any kanban tasks (best-effort, don't block)
    fetch('/api/kanban/sessions/' + id + '/unlink-all', { method: 'DELETE' }).catch(() => {});
    if (liveSessionId === id) stopLivePanel();
    // In kanban mode, navigate back to the task (or board) instead of
    // just deselecting — otherwise the user is stuck on a dead view.
    if (typeof viewMode !== 'undefined' && viewMode === 'kanban' && window._kanbanSessionTaskId) {
      _kanbanSessionClose(window._kanbanSessionTaskId);
    } else {
      deselectSession();
    }
    document.getElementById('search').placeholder = 'Search ' + allSessions.length + ' sessions\u2026';
    if (typeof loadProjects === 'function') loadProjects();  // refresh splash-screen session counts
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

  const _dep = localStorage.getItem('activeProject') || '';
  const _depQ = _dep ? '?project=' + encodeURIComponent(_dep) : '';
  const resp = await fetch('/api/delete-empty' + _depQ, { method: 'DELETE' });
  const data = await resp.json();

  if (data.ok) {
    allSessions = allSessions.filter(s => s.message_count > 0);
    _rebuildSessionIds();
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
    if (typeof loadProjects === 'function') loadProjects();  // refresh splash-screen session counts
    showToast(`Deleted ${data.deleted} empty session${data.deleted !== 1 ? 's' : ''}`);
  } else {
    showToast('Delete failed', true);
  }
}

async function duplicateSession(id) {
  const _dupp = localStorage.getItem('activeProject') || '';
  const _duppQ = _dupp ? '?project=' + encodeURIComponent(_dupp) : '';
  const resp = await fetch('/api/duplicate/' + id + _duppQ, { method: 'POST' });
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

  const _contp = localStorage.getItem('activeProject') || '';
  const _contpQ = _contp ? '?project=' + encodeURIComponent(_contp) : '';
  const resp = await fetch('/api/continue/' + id + _contpQ, { method: 'POST' });
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
  const _openp = localStorage.getItem('activeProject') || '';
  const _openpQ = _openp ? '?project=' + encodeURIComponent(_openp) : '';
  const resp = await fetch('/api/open/' + id + _openpQ, { method: 'POST' });
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
    const _tlp = localStorage.getItem('activeProject') || '';
    const _tlpQ = _tlp ? '?project=' + encodeURIComponent(_tlp) : '';
    const url = '/api/session-timeline/' + sessionId + _tlpQ;
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

  // For fork modes, only show user messages (forking from Claude output doesn't make sense)
  const forkOnly = (mode === 'fork' || mode === 'fork-rewind');
  const filtered = forkOnly ? messages.filter(m => m.role === 'user') : messages;

  if (!filtered.length) {
    el.innerHTML = '<div style="padding:20px;color:var(--text-faint);text-align:center;">No user messages found in this session.</div>';
    return;
  }

  let html = '';
  for (const m of filtered) {
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

    const _frkp = localStorage.getItem('activeProject') || '';
    const _frkpQ = _frkp ? '?project=' + encodeURIComponent(_frkp) : '';
    const resp = await fetch(endpoint + _pickerSessionId + _frkpQ, { method: 'POST', headers: hdrs, body });
    if (!resp.ok) {
      let errMsg = 'HTTP ' + resp.status;
      try { const d = await resp.json(); errMsg = d.error || errMsg; } catch(e) {}
      _closePm();
      showToast(errMsg, true);
      return;
    }
    const data = await resp.json();
    _closePm();

    if (data.error) {
      showToast(data.error, true);
      return;
    }

    if (_pickerMode === 'fork' || _pickerMode === 'fork-rewind') {
      await loadSessions();
      if (data.new_id) await openInGUI(data.new_id);

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
