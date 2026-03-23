/* workforce.js — workforce grid view mode */

function getSessionStatus(id) {
  const kind = sessionKinds[id];
  if (kind === 'question') return 'question';
  if (kind === 'working') return 'working';
  if (kind === 'idle') return 'idle';
  if (runningIds.has(id)) return 'working';
  // Sessions opened in GUI panel are considered idle even if no OS process detected
  if (guiOpenSessions.has(id)) return 'idle';
  return 'sleeping';
}

function setViewMode(mode) {
  const prevMode = viewMode;
  viewMode = mode;
  localStorage.setItem('viewMode', mode);
  const listEl = document.getElementById('session-list');
  const gridEl = document.getElementById('workforce-grid');
  const btnList = document.getElementById('btn-view-list');
  const btnWf   = document.getElementById('btn-view-workforce');

  // --- Clean up when LEAVING a mode ---

  // Leaving workplace: clear workspace state, stop live panel if expanded card was open
  if (prevMode === 'workplace' && mode !== 'workplace') {
    if (_wsExpandedId) {
      _wsExpandedId = null;
      const backBtn = document.getElementById('ws-back-btn');
      if (backBtn) backBtn.remove();
    }
    if (liveSessionId) stopLivePanel();
    activeId = null;
    localStorage.removeItem('activeSessionId');
    // Restore main-body to dashboard (workplace overwrites it)
    document.getElementById('main-toolbar').style.display = 'none';
    document.getElementById('main-body').innerHTML = _buildDashboard();
    // Clear sidebar permission panel
    const spp = document.getElementById('sidebar-perm-panel');
    if (spp) { spp.innerHTML = ''; spp.style.display = 'none'; }
    // Restore new session button
    const btnAdd = document.getElementById('btn-add-agent');
    if (btnAdd) btnAdd.style.display = '';
  }

  // Leaving workforce or list into workplace: clear active session so
  // main-body is fully owned by workspace renderer
  if (mode === 'workplace' && prevMode !== 'workplace') {
    if (liveSessionId) stopLivePanel();
    activeId = null;
    localStorage.removeItem('activeSessionId');
    document.getElementById('main-toolbar').style.display = 'none';
  }

  // --- Set new mode state ---
  if (typeof workspaceActive !== 'undefined') workspaceActive = (mode === 'workplace');

  // Sidebar elements to show/hide per mode
  const searchRow = document.querySelector('.sidebar-search-row');
  const menuWrap = document.querySelector('.sidebar-menu-wrap');
  const sidebarPermPanel = document.getElementById('sidebar-perm-panel');

  if (mode === 'workforce') {
    listEl.style.display = 'none';
    gridEl.classList.add('visible');
    if (btnList) btnList.classList.remove('active');
    if (btnWf)   btnWf.classList.add('active');
    if (searchRow) searchRow.style.display = '';
    if (menuWrap) menuWrap.style.display = '';
    if (sidebarPermPanel) sidebarPermPanel.style.display = 'none';
  } else if (mode === 'workplace') {
    // Don't hide the session list yet — let it show skeleton until
    // renderWorkspace finishes. renderWorkspace will hide it atomically.
    gridEl.classList.remove('visible');
    if (btnList) btnList.classList.remove('active');
    if (btnWf)   btnWf.classList.remove('active');
    if (sidebarPermPanel) { sidebarPermPanel.style.display = 'none'; sidebarPermPanel.innerHTML = ''; }
    const btnAdd = document.getElementById('btn-add-agent');
    if (btnAdd) btnAdd.style.display = 'none';
  } else {
    listEl.style.display = '';
    gridEl.classList.remove('visible');
    if (btnList) btnList.classList.add('active');
    if (btnWf)   btnWf.classList.remove('active');
    if (searchRow) searchRow.style.display = '';
    if (menuWrap) menuWrap.style.display = '';
    if (sidebarPermPanel) sidebarPermPanel.style.display = 'none';
  }
  filterSessions();
}

function setWfSort(sort) {
  wfSort = sort;
  localStorage.setItem('wfSort', sort);
  ['status','recent','name'].forEach(s => {
    const btn = document.getElementById('wf-btn-' + s);
    if (btn) btn.classList.toggle('active', s === sort);
  });
  filterSessions();
}

function wfSortedSessions(sessions) {
  const copy = [...sessions];
  const statusOrder = {question:0, working:1, idle:2, sleeping:3};
  if (wfSort === 'status') {
    copy.sort((a, b) => {
      const sa = statusOrder[getSessionStatus(a.id)] ?? 3;
      const sb = statusOrder[getSessionStatus(b.id)] ?? 3;
      if (sa !== sb) return sa - sb;
      return (b.last_activity_ts||b.sort_ts||0) - (a.last_activity_ts||a.sort_ts||0);
    });
  } else if (wfSort === 'name') {
    copy.sort((a, b) => (a.display_title||'').localeCompare(b.display_title||''));
  } else {
    // recent
    copy.sort((a, b) => (b.last_activity_ts||b.sort_ts||0) - (a.last_activity_ts||a.sort_ts||0));
  }
  return copy;
}

function renderWorkforce(sessions) {
  const grid = document.getElementById('workforce-grid');
  if (!sessions.length) {
    grid.innerHTML = '<div style="padding:20px;color:var(--text-muted);font-size:12px;">No sessions found</div>';
    return;
  }
  const statusSvg = {
    question: '<svg width="28" height="28" viewBox="0 0 24 24" fill="none" stroke="#ff9500" stroke-width="1.5" stroke-linecap="round"><circle cx="12" cy="12" r="10"/><path d="M9.09 9a3 3 0 0 1 5.83 1c0 2-3 3-3 3"/><circle cx="12" cy="17" r=".5" fill="#ff9500"/></svg>',
    working: '<img src="/static/svg/pickaxe.svg" width="28" height="28" style="filter:brightness(0) saturate(100%) invert(55%) sepia(78%) saturate(1000%) hue-rotate(215deg);">',
    idle: '<svg width="28" height="28" viewBox="0 0 24 24" fill="none" stroke="#44aa66" stroke-width="1.5" stroke-linecap="round"><polyline points="20 6 9 17 4 12"/></svg>',
    sleeping: '<img src="/static/svg/sleeping.svg" width="28" height="28" class="sleeping-icon">',
  };
  const statusLabel = {question:'Question', working:'Working', idle:'Idle', sleeping:'Sleeping'};
  grid.innerHTML = sessions.map(s => {
    const st = getSessionStatus(s.id);
    const emoji = statusSvg[st] || statusSvg.sleeping;
    const label = statusLabel[st] || 'Sleeping';
    const selClass = s.id === activeId ? ' wf-selected' : '';
    const name = escHtml((s.display_title||s.id).slice(0,22) + ((s.display_title||'').length>22?'\u2026':''));
    const date = (s.last_activity||'').split('  ')[0] || '';
    return `<div class="wf-card wf-${st}${selClass}" onclick="singleOrDouble('${s.id}',event)" title="${escHtml(s.display_title)} \u2014 double-click to open in Claude Code GUI">
      <div class="wf-avatar">${emoji}</div>
      <div class="wf-status-label">${label}</div>
      <div class="wf-name">${name}</div>
      <div class="wf-meta">${escHtml(date)}</div>
    </div>`;
  }).join('');
}

function wfCardClick(id) {
  selectSession(id);
}
