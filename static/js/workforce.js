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
  if (typeof _updateViewModeButton === 'function') _updateViewModeButton(mode);

  // Clean stale URL state when switching modes
  if (prevMode !== mode) {
    const url = new URL(window.location);
    if (mode !== 'kanban') { url.hash = ''; }
    // Always strip ?chat= when going to homepage, kanban, or workplace
    if (mode === 'homepage' || mode === 'kanban' || mode === 'workplace') { url.searchParams.delete('chat'); }
    history.replaceState({}, '', url.pathname + url.search + url.hash);
  }
  const listEl = document.getElementById('session-list');
  const gridEl = document.getElementById('workforce-grid');
  const kanbanEl = document.getElementById('kanban-board');
  const homepageEl = document.getElementById('homepage-container');

  // --- Clean up when LEAVING a mode ---

  // Leaving kanban: nuclear cleanup
  if (prevMode === 'kanban' && mode !== 'kanban') {
    if (liveSessionId) { if (typeof stopLivePanel === 'function') stopLivePanel(); }
    activeId = null;
    liveSessionId = null;
    localStorage.removeItem('activeSessionId');
    if (typeof resetKanbanState === 'function') resetKanbanState();
    const sessionBar = document.getElementById('kanban-session-bar');
    if (sessionBar) sessionBar.remove();
    if (kanbanEl) { kanbanEl.style.display = 'none'; kanbanEl.innerHTML = ''; }
    document.getElementById('main-body').style.display = '';
    if (mode !== 'homepage') document.getElementById('main-body').innerHTML = _buildDashboard();
    document.getElementById('main-toolbar').style.display = 'none';
    const kanbanSidebar = document.getElementById('kanban-sidebar');
    if (kanbanSidebar) { kanbanSidebar.style.display = 'none'; kanbanSidebar.innerHTML = ''; }
    const kbPermPanel = document.getElementById('sidebar-perm-panel');
    if (kbPermPanel) { kbPermPanel.style.display = 'none'; kbPermPanel.innerHTML = ''; }
    const btnAdd = document.getElementById('btn-add-agent');
    if (btnAdd) btnAdd.style.display = '';
    const cleanUrl = new URL(window.location);
    cleanUrl.hash = '';
    cleanUrl.searchParams.delete('chat');
    history.replaceState({}, '', cleanUrl.pathname + cleanUrl.search);
  }

  // Leaving workplace: clear workspace state
  if (prevMode === 'workplace' && mode !== 'workplace') {
    if (_wsExpandedId) {
      _wsExpandedId = null;
      const backBtn = document.getElementById('ws-back-btn');
      if (backBtn) backBtn.remove();
    }
    if (liveSessionId) stopLivePanel();
    activeId = null;
    localStorage.removeItem('activeSessionId');
    document.getElementById('main-toolbar').style.display = 'none';
    if (mode !== 'homepage') document.getElementById('main-body').innerHTML = _buildDashboard();
    const spp = document.getElementById('sidebar-perm-panel');
    if (spp) { spp.innerHTML = ''; spp.style.display = 'none'; }
    const btnAdd = document.getElementById('btn-add-agent');
    if (btnAdd) btnAdd.style.display = '';
  }

  // Leaving sessions into workplace: clear active session
  if (mode === 'workplace' && prevMode !== 'workplace') {
    if (liveSessionId) stopLivePanel();
    activeId = null;
    localStorage.removeItem('activeSessionId');
    document.getElementById('main-toolbar').style.display = 'none';
  }

  // Leaving homepage: restore sidebar and hide homepage
  if (prevMode === 'homepage' && mode !== 'homepage') {
    if (homepageEl) homepageEl.style.display = 'none';
    document.getElementById('main-body').style.display = '';
    const sidebar = document.querySelector('.sidebar');
    if (sidebar) sidebar.style.display = '';
    const resizeHandle = document.querySelector('.resize-handle');
    if (resizeHandle) resizeHandle.style.display = '';
    // Restore expand button if sidebar was collapsed
    const expandBtn = document.getElementById('btn-sidebar-expand');
    if (expandBtn) {
      const isCollapsed = sidebar && sidebar.classList.contains('collapsed');
      expandBtn.style.display = '';
      expandBtn.classList.toggle('visible', isCollapsed);
    }
  }

  // --- Set new mode state ---
  if (typeof workspaceActive !== 'undefined') workspaceActive = (mode === 'workplace');

  const searchRow = document.querySelector('.sidebar-search-row');
  const menuWrap = document.querySelector('.sidebar-menu-wrap');
  const sidebarPermPanel = document.getElementById('sidebar-perm-panel');

  if (mode === 'homepage') {
    // Deselect any active session so stale content doesn't linger on return
    if (liveSessionId) { if (typeof stopLivePanel === 'function') stopLivePanel(); }
    activeId = null;
    localStorage.removeItem('activeSessionId');
    document.getElementById('main-toolbar').style.display = 'none';
    document.getElementById('main-body').innerHTML = (typeof _buildDashboard === 'function') ? _buildDashboard() : '';
    // Hide sidebar and all content, show homepage full-width
    const sidebar = document.querySelector('.sidebar');
    if (sidebar) sidebar.style.display = 'none';
    const resizeHandle = document.querySelector('.resize-handle');
    if (resizeHandle) resizeHandle.style.display = 'none';
    const expandBtn = document.getElementById('btn-sidebar-expand');
    if (expandBtn) expandBtn.style.display = 'none';
    listEl.style.display = 'none';
    gridEl.classList.remove('visible');
    if (kanbanEl) kanbanEl.style.display = 'none';
    document.getElementById('main-toolbar').style.display = 'none';
    document.getElementById('main-body').style.display = 'none';
    if (homepageEl) {
      homepageEl.innerHTML = (typeof _buildHomepageContent === 'function') ? _buildHomepageContent() : '';
      homepageEl.style.display = '';
    }

  } else if (mode === 'sessions') {
    // Combined grid + list mode
    if (sessionDisplayMode === 'grid') {
      listEl.style.display = 'none';
      gridEl.classList.add('visible');
    } else {
      listEl.style.display = '';
      gridEl.classList.remove('visible');
    }
    if (searchRow) searchRow.style.display = '';
    if (menuWrap) menuWrap.style.display = '';
    if (sidebarPermPanel) sidebarPermPanel.style.display = 'none';
    if (kanbanEl) kanbanEl.style.display = 'none';
    document.getElementById('main-body').style.display = '';
    if (!activeId) document.getElementById('main-toolbar').style.display = 'none';
    const btnAdd = document.getElementById('btn-add-agent');
    if (btnAdd) btnAdd.style.display = '';
    if (homepageEl) homepageEl.style.display = 'none';

  } else if (mode === 'kanban') {
    const cleanUrl = new URL(window.location);
    cleanUrl.searchParams.delete('chat');
    if (!cleanUrl.hash || !cleanUrl.hash.startsWith('#kanban')) {
      cleanUrl.hash = '#kanban';
    }
    history.replaceState({}, '', cleanUrl.pathname + cleanUrl.search + cleanUrl.hash);

    listEl.style.display = 'none';
    gridEl.classList.remove('visible');
    if (searchRow) searchRow.style.display = 'none';
    if (menuWrap) menuWrap.style.display = 'none';
    const btnAdd = document.getElementById('btn-add-agent');
    if (btnAdd) btnAdd.style.display = 'none';
    if (kanbanEl) kanbanEl.style.display = '';
    const kanbanSidebar = document.getElementById('kanban-sidebar');
    if (kanbanSidebar) kanbanSidebar.style.display = '';
    document.getElementById('main-body').style.display = 'none';
    document.getElementById('main-toolbar').style.display = 'none';
    if (homepageEl) homepageEl.style.display = 'none';
    if (typeof initKanban === 'function') initKanban();

  } else if (mode === 'workplace') {
    gridEl.classList.remove('visible');
    if (sidebarPermPanel) { sidebarPermPanel.style.display = 'none'; sidebarPermPanel.innerHTML = ''; }
    const btnAdd = document.getElementById('btn-add-agent');
    if (btnAdd) btnAdd.style.display = 'none';
    if (homepageEl) homepageEl.style.display = 'none';
    document.getElementById('main-body').style.display = '';
    document.getElementById('main-toolbar').style.display = 'none';
  }
  filterSessions();
}

function setSessionDisplayMode(mode) {
  sessionDisplayMode = mode;
  localStorage.setItem('sessionDisplayMode', mode);
  // Update active state on menu items
  document.querySelectorAll('[data-display]').forEach(el => {
    el.classList.toggle('active', el.dataset.display === mode);
  });
  if (viewMode === 'sessions') {
    const listEl = document.getElementById('session-list');
    const gridEl = document.getElementById('workforce-grid');
    if (mode === 'grid') {
      listEl.style.display = 'none';
      gridEl.classList.add('visible');
    } else {
      listEl.style.display = '';
      gridEl.classList.remove('visible');
    }
    filterSessions();
  }
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
    const _isCompacting = st === 'working' && window._sessionSubstatus && window._sessionSubstatus[s.id] === 'compacting';
    const emoji = _isCompacting
      ? '<svg class="compacting-icon" width="28" height="28" viewBox="0 0 24 24" fill="none" stroke="#aa88ff" stroke-width="1.5" stroke-linecap="round"><polyline points="4 14 10 14 10 20"/><polyline points="20 10 14 10 14 4"/><line x1="14" y1="10" x2="21" y2="3"/><line x1="3" y1="21" x2="10" y2="14"/></svg>'
      : (statusSvg[st] || statusSvg.sleeping);
    const label = _isCompacting ? 'Compacting' : (statusLabel[st] || 'Sleeping');
    const selClass = s.id === activeId ? ' wf-selected' : '';
    const name = escHtml((s.display_title||s.id).slice(0,22) + ((s.display_title||'').length>22?'\u2026':''));
    const date = _shortDate(s.last_activity);
    return `<div class="wf-card wf-${st}${selClass}" onclick="singleOrDouble('${s.id}',event)" oncontextmenu="sessionContextMenu(event,'${s.id}')" title="${escHtml(s.display_title)} \u2014 double-click to open in VibeNode">
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
