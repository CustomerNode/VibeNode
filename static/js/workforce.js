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
  // Skip transition if same mode, or if project-switch loader is covering everything
  const _skipTransition = (viewMode === mode) || !!document.getElementById('project-switch-loader');
  if (!_skipTransition) {
    return _viewTransition(viewMode, mode);
  }
  _setViewModeImmediate(mode, viewMode);
}

function _viewTransition(prevMode, mode) {
  // Set viewMode immediately to prevent double-triggers during animation
  viewMode = mode;
  // Fade out all view containers, then switch, then fade in
  const targets = ['homepage-container', 'main-body', 'kanban-board', 'main-toolbar'].map(id => document.getElementById(id)).filter(Boolean);
  const sidebar = document.querySelector('.sidebar');
  if (sidebar) targets.push(sidebar);

  // Apply fade-out
  for (const el of targets) {
    el.style.transition = 'opacity 0.15s ease';
    el.style.opacity = '0';
  }

  setTimeout(() => {
    _setViewModeImmediate(mode, prevMode);

    // Gather the NOW-visible containers and fade them in
    const freshTargets = ['homepage-container', 'main-body', 'kanban-board', 'main-toolbar'].map(id => document.getElementById(id)).filter(Boolean);
    if (sidebar) freshTargets.push(sidebar);
    for (const el of freshTargets) {
      el.style.opacity = '0';
      el.style.transition = 'opacity 0.2s ease';
    }
    requestAnimationFrame(() => {
      for (const el of freshTargets) el.style.opacity = '1';
    });
    // Clean up inline transition styles after animation
    setTimeout(() => {
      for (const el of freshTargets) {
        el.style.removeProperty('transition');
        el.style.removeProperty('opacity');
      }
    }, 250);
  }, 150);
}

// --- Per-project, per-view state persistence ---
// Saves the navigation position within a view so it can be restored when the
// user switches away and comes back (within the same project or across projects).

function _saveViewPosition(proj, view) {
  if (!proj) return;
  const prefix = 'pvs_' + proj + '_';
  if (view === 'sessions') {
    // Track active session (also updated live in openInGUI)
    if (activeId) localStorage.setItem(prefix + 'sessions', activeId);
  } else if (view === 'kanban') {
    const h = window.location.hash || '';
    if (h.startsWith('#kanban')) {
      // Strip /session/ suffix — session IDs are view-local state
      localStorage.setItem(prefix + 'kanban', h.replace(/\/session\/.*$/, ''));
    }
  } else if (view === 'compose') {
    const h = window.location.hash || '';
    if (h.startsWith('#compose')) {
      localStorage.setItem(prefix + 'compose', h);
    }
  }
}

function _restoreViewPosition(proj, view) {
  if (!proj) return null;
  return localStorage.getItem('pvs_' + proj + '_' + view) || null;
}

function _setViewModeImmediate(mode, prevMode) {
  prevMode = prevMode != null ? prevMode : null;

  // Save navigation position of the view we're LEAVING before cleanup destroys it
  const _proj = localStorage.getItem('activeProject');
  if (prevMode && prevMode !== mode) _saveViewPosition(_proj, prevMode);

  // Sidebar multi-selection only makes sense in the sessions view.
  // Leaving sessions for any other mode clears it so the badge doesn't
  // sit stale when the user comes back.  No-op if selection is already
  // empty.  Guarded by typeof so this stays safe if sessions.js is
  // somehow not yet loaded (it always is by the time the view changes).
  if (prevMode === 'sessions' && mode !== 'sessions'
      && typeof _clearMultiSelect === 'function') {
    _clearMultiSelect();
  }
  // Leaving sessions mode: clean up MC state
  if (prevMode === 'sessions' && mode !== 'sessions') {
    document.body.classList.remove('mc-mode');
    const _mcEl = document.getElementById('mission-control');
    if (_mcEl) _mcEl.style.display = 'none';
  }

  viewMode = mode;
  localStorage.setItem('viewMode', mode);
  // Keep per-project view memory in sync so project switches restore it
  if (_proj) localStorage.setItem('projectView_' + _proj, mode);
  if (typeof _updateViewModeButton === 'function') _updateViewModeButton(mode);

  // Clean stale URL state when switching modes
  if (prevMode !== mode) {
    const url = new URL(window.location);
    if (mode !== 'kanban' && mode !== 'compose') { url.hash = ''; }
    // Always strip ?chat= when going to homepage, kanban, workplace, or compose
    if (mode === 'homepage' || mode === 'kanban' || mode === 'workplace' || mode === 'compose') { url.searchParams.delete('chat'); }
    history.replaceState({}, '', url.pathname + url.search + url.hash);
  }
  const listEl = document.getElementById('session-list');
  const gridEl = document.getElementById('workforce-grid');
  const kanbanEl = document.getElementById('kanban-board');
  const composeEl = document.getElementById('compose-board');
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

  // Leaving compose: clean up compose board
  if (prevMode === 'compose' && mode !== 'compose') {
    if (liveSessionId) { if (typeof stopLivePanel === 'function') stopLivePanel(); }
    activeId = null;
    liveSessionId = null;
    localStorage.removeItem('activeSessionId');
    if (typeof resetComposeState === 'function') resetComposeState();
    if (composeEl) { composeEl.style.display = 'none'; }
    const composeSidebar = document.getElementById('compose-sidebar');
    if (composeSidebar) { composeSidebar.style.display = 'none'; }
    const csPermPanel = document.getElementById('sidebar-perm-panel');
    if (csPermPanel) { csPermPanel.style.display = 'none'; csPermPanel.innerHTML = ''; }
    document.getElementById('main-body').style.display = '';
    if (mode !== 'homepage') document.getElementById('main-body').innerHTML = _buildDashboard();
    document.getElementById('main-toolbar').style.display = 'none';
    const btnAdd = document.getElementById('btn-add-agent');
    if (btnAdd) btnAdd.style.display = '';
    const cleanUrl = new URL(window.location);
    cleanUrl.hash = '';
    cleanUrl.searchParams.delete('chat');
    history.replaceState({}, '', cleanUrl.pathname + cleanUrl.search);
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
    if (composeEl) composeEl.style.display = 'none';
    const _mcHome = document.getElementById('mission-control');
    if (_mcHome) _mcHome.style.display = 'none';
    document.getElementById('main-toolbar').style.display = 'none';
    document.getElementById('main-body').style.display = 'none';
    if (homepageEl) {
      homepageEl.innerHTML = (typeof _buildHomepageContent === 'function') ? _buildHomepageContent() : '';
      homepageEl.style.display = '';
    }

  } else if (mode === 'sessions') {
    // Combined grid + list + control mode
    const mcEl = document.getElementById('mission-control');
    if (sessionDisplayMode === 'grid') {
      document.body.classList.remove('mc-mode');
      listEl.style.display = 'none';
      gridEl.classList.add('visible');
      if (mcEl) mcEl.style.display = 'none';
    } else if (sessionDisplayMode === 'control') {
      document.body.classList.add('mc-mode');
      listEl.style.display = 'none';
      gridEl.classList.remove('visible');
      if (mcEl) mcEl.style.display = 'flex';
    } else {
      document.body.classList.remove('mc-mode');
      listEl.style.display = '';
      gridEl.classList.remove('visible');
      if (mcEl) mcEl.style.display = 'none';
    }
    if (searchRow) searchRow.style.display = sessionDisplayMode === 'control' ? 'none' : '';
    if (menuWrap) menuWrap.style.display = '';
    if (sidebarPermPanel) sidebarPermPanel.style.display = 'none';
    if (kanbanEl) kanbanEl.style.display = 'none';
    if (composeEl) composeEl.style.display = 'none';
    document.getElementById('main-body').style.display = sessionDisplayMode === 'control' ? 'none' : '';
    if (!activeId) document.getElementById('main-toolbar').style.display = 'none';
    const btnAdd = document.getElementById('btn-add-agent');
    if (btnAdd) btnAdd.style.display = '';
    if (homepageEl) homepageEl.style.display = 'none';

    // Restore last-open session for this project (if any) when returning to sessions view
    if (!activeId && prevMode && prevMode !== 'sessions') {
      const _savedSid = _restoreViewPosition(_proj, 'sessions');
      if (_savedSid && typeof allSessions !== 'undefined' && allSessions.find(s => s.id === _savedSid)) {
        // Defer so the view finishes rendering first
        setTimeout(() => { if (typeof openInGUI === 'function') openInGUI(_savedSid); }, 0);
      }
    }

  } else if (mode === 'kanban') {
    // Restore saved drill-down hash (if any) before initKanban → restoreFromHash()
    const _savedKHash = _restoreViewPosition(_proj, 'kanban');
    const cleanUrl = new URL(window.location);
    cleanUrl.searchParams.delete('chat');
    if (_savedKHash && _savedKHash.startsWith('#kanban')) {
      cleanUrl.hash = '';  // clear first so replaceState is clean
      history.replaceState({ view: 'kanban', taskId: null }, '', cleanUrl.pathname + cleanUrl.search + _savedKHash);
    } else if (!cleanUrl.hash || !cleanUrl.hash.startsWith('#kanban')) {
      cleanUrl.hash = '#kanban';
      history.replaceState({}, '', cleanUrl.pathname + cleanUrl.search + cleanUrl.hash);
    } else {
      history.replaceState({}, '', cleanUrl.pathname + cleanUrl.search + cleanUrl.hash);
    }

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
    if (composeEl) composeEl.style.display = 'none';
    if (homepageEl) homepageEl.style.display = 'none';
    if (typeof initKanban === 'function') initKanban();

  } else if (mode === 'compose') {
    // Restore saved compose sub-route hash (if any) before initCompose
    const _savedCHash = _restoreViewPosition(_proj, 'compose');
    const cleanUrl = new URL(window.location);
    cleanUrl.searchParams.delete('chat');
    if (_savedCHash && _savedCHash.startsWith('#compose')) {
      cleanUrl.hash = '';
      history.replaceState({ view: 'compose' }, '', cleanUrl.pathname + cleanUrl.search + _savedCHash);
    } else if (!cleanUrl.hash || !cleanUrl.hash.startsWith('#compose')) {
      cleanUrl.hash = '#compose';
      history.replaceState({}, '', cleanUrl.pathname + cleanUrl.search + cleanUrl.hash);
    } else {
      history.replaceState({}, '', cleanUrl.pathname + cleanUrl.search + cleanUrl.hash);
    }

    listEl.style.display = 'none';
    gridEl.classList.remove('visible');
    if (searchRow) searchRow.style.display = 'none';
    if (menuWrap) menuWrap.style.display = 'none';
    const btnAdd = document.getElementById('btn-add-agent');
    if (btnAdd) btnAdd.style.display = 'none';
    if (kanbanEl) kanbanEl.style.display = 'none';
    if (composeEl) composeEl.style.display = '';
    const composeSidebar = document.getElementById('compose-sidebar');
    if (composeSidebar) composeSidebar.style.display = '';
    document.getElementById('main-body').style.display = 'none';
    document.getElementById('main-toolbar').style.display = 'none';
    if (homepageEl) homepageEl.style.display = 'none';
    if (typeof initCompose === 'function') initCompose();

  } else if (mode === 'workplace') {
    gridEl.classList.remove('visible');
    if (sidebarPermPanel) { sidebarPermPanel.style.display = 'none'; sidebarPermPanel.innerHTML = ''; }
    const btnAdd = document.getElementById('btn-add-agent');
    if (btnAdd) btnAdd.style.display = 'none';
    if (kanbanEl) kanbanEl.style.display = 'none';
    if (composeEl) composeEl.style.display = 'none';
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
    const mcEl = document.getElementById('mission-control');
    if (mode === 'grid') {
      document.body.classList.remove('mc-mode');
      listEl.style.display = 'none';
      gridEl.classList.add('visible');
      if (mcEl) mcEl.style.display = 'none';
      // Restore main-body (hidden when in control mode)
      const _mb = document.getElementById('main-body');
      if (_mb) _mb.style.display = '';
      const _sr = document.querySelector('.sidebar-search-row');
      if (_sr) _sr.style.display = '';
      filterSessions();
    } else if (mode === 'list') {
      document.body.classList.remove('mc-mode');
      listEl.style.display = '';
      gridEl.classList.remove('visible');
      if (mcEl) mcEl.style.display = 'none';
      // Restore main-body (hidden when in control mode)
      const _mb2 = document.getElementById('main-body');
      if (_mb2) _mb2.style.display = '';
      const _sr2 = document.querySelector('.sidebar-search-row');
      if (_sr2) _sr2.style.display = '';
      filterSessions();
    } else if (mode === 'control') {
      document.body.classList.add('mc-mode');
      listEl.style.display = 'none';
      gridEl.classList.remove('visible');
      if (mcEl) mcEl.style.display = 'flex';
      // Clear fetch cache so cards load fresh history on each MC entry
      _mcPreviewFetched.clear();
      renderMissionControl();
    }
  }
}

// =============================================================================
// Mission Control — full-screen multi-session card grid
// =============================================================================

// Per-session streaming buffers: sid → { text: string, timer: number|null }
const _mcStreamBuffers = new Map();
// Track which sids have had their initial history fetched (avoid redundant API calls)
const _mcPreviewFetched = new Set();
// Current search filter text
let _mcSearchFilter = '';

/**
 * Render (or refresh) all Mission Control cards.
 * Called by setSessionDisplayMode('control'), filterSessions(), and
 * socket event handlers when sessionDisplayMode === 'control'.
 */
function renderMissionControl() {
  const grid = document.getElementById('mc-grid');
  if (!grid) return;

  // Update project label
  const projLabel = document.getElementById('mc-project-label');
  if (projLabel) {
    const proj = localStorage.getItem('activeProject') || '';
    let projDisplay = proj;
    if (Array.isArray(window._allProjects)) {
      const found = window._allProjects.find(p => p.encoded === proj);
      if (found) projDisplay = found.custom_name || found.display || proj;
    }
    projLabel.textContent = projDisplay || 'All Projects';
  }

  // Apply search filter
  const searchLower = _mcSearchFilter.toLowerCase();
  const sessions = wfSortedSessions(
    searchLower
      ? allSessions.filter(s => (s.display_title || '').toLowerCase().includes(searchLower))
      : allSessions
  );

  // Update session count badge
  const countBadge = document.getElementById('mc-session-count');
  if (countBadge) {
    countBadge.textContent = sessions.length === 1 ? '1 session' : sessions.length + ' sessions';
  }

  // Track which sids should currently be in the grid
  const newIds = new Set(sessions.map(s => s.id));

  // Remove cards for sessions that no longer match
  Array.from(grid.querySelectorAll('.mc-card')).forEach(card => {
    if (!newIds.has(card.dataset.sid)) card.remove();
  });

  // Reorder + add/update cards
  sessions.forEach((session, idx) => {
    const sid = session.id;
    const status = getSessionStatus(sid);
    const substatus = window._sessionSubstatus && window._sessionSubstatus[sid];
    const title = session.display_title || session.custom_title || sid.slice(0, 8);

    let card = grid.querySelector(`.mc-card[data-sid="${sid}"]`);
    if (!card) {
      card = _createMcCard(session, status, substatus, title);
      grid.appendChild(card);
    } else {
      _updateMcCardStatus(card, sid, status, substatus, title, session);
      // Fetch history for existing cards that haven't been populated yet
      if (!_mcPreviewFetched.has(sid)) _populateMcPreview(session);
      // Maintain sort order — append to end if not already in position
      if (card !== grid.children[idx]) {
        grid.appendChild(card);
      }
    }
  });

  if (!sessions.length) {
    if (!grid.querySelector('.mc-empty')) {
      const empty = document.createElement('div');
      empty.className = 'mc-empty';
      empty.innerHTML = '<div class="mc-empty-text">No sessions</div>';
      grid.appendChild(empty);
    }
  } else {
    const empty = grid.querySelector('.mc-empty');
    if (empty) empty.remove();
  }
}

function _mcTimeAgo(ts) {
  if (!ts) return '';
  const diff = Math.floor(Date.now() / 1000) - ts;
  if (diff < 60) return 'just now';
  if (diff < 3600) return Math.floor(diff / 60) + 'm ago';
  if (diff < 86400) return Math.floor(diff / 3600) + 'h ago';
  return Math.floor(diff / 86400) + 'd ago';
}

function _createMcCard(session, status, substatus, title) {
  const sid = session.id;
  const statusLabel = _getMcStatusLabel(status, substatus);

  const card = document.createElement('div');
  card.className = 'mc-card mc-card-' + status;
  card.dataset.sid = sid;

  // Left accent strip (CSS applies color based on status class)
  const accent = document.createElement('div');
  accent.className = 'mc-card-accent';

  // Build header
  const header = document.createElement('div');
  header.className = 'mc-card-header';
  header.title = 'Click to open in normal view';
  header.onclick = () => _mcOpenSession(sid);

  const metaParts = [];
  if (session.message_count) metaParts.push(session.message_count + ' msgs');
  const timeAgo = _mcTimeAgo(session.last_activity_ts);
  if (timeAgo) metaParts.push(timeAgo);

  header.innerHTML =
    '<div class="mc-status-dot ' + status + '" id="mc-dot-' + sid + '"></div>' +
    '<div class="mc-card-header-info">' +
      '<div class="mc-card-name" id="mc-name-' + sid + '" title="' + escHtml(title) + '">' + escHtml(title) + '</div>' +
      '<div class="mc-card-meta" id="mc-meta-' + sid + '">' + escHtml(metaParts.join(' · ')) + '</div>' +
    '</div>' +
    '<div class="mc-card-status ' + _getMcStatusClass(status) + '" id="mc-status-' + sid + '">' + escHtml(statusLabel) + '</div>';

  // Preview area
  const preview = document.createElement('div');
  preview.className = 'mc-preview';
  preview.id = 'mc-preview-' + sid;
  preview.innerHTML = '<span class="mc-preview-placeholder">No recent messages</span>';

  // Input row
  const inputRow = document.createElement('div');
  inputRow.className = 'mc-card-input';

  const textarea = document.createElement('textarea');
  textarea.className = 'mc-input';
  textarea.id = 'mc-input-' + sid;
  textarea.placeholder = 'Message…';
  textarea.rows = 1;
  textarea.addEventListener('keydown', (e) => _mcInputKeyDown(e, sid));
  textarea.addEventListener('input', () => {
    textarea.style.height = 'auto';
    textarea.style.height = Math.min(textarea.scrollHeight, 120) + 'px';
    textarea.scrollTop = textarea.scrollHeight;
    inputRow.classList.toggle('has-text', textarea.value.trim().length > 0);
  });

  const voiceBtn = document.createElement('button');
  voiceBtn.className = 'mc-voice-btn';
  voiceBtn.id = 'mc-voice-' + sid;
  voiceBtn.title = 'Voice input';
  voiceBtn.innerHTML = _micSvg || '<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><rect x="9" y="1" width="6" height="12" rx="3"/><path d="M19 10v2a7 7 0 0 1-14 0v-2"/><line x1="12" y1="23" x2="12" y2="19"/></svg>';

  inputRow.appendChild(textarea);
  inputRow.appendChild(voiceBtn);

  card.appendChild(accent);
  card.appendChild(header);
  card.appendChild(preview);
  card.appendChild(inputRow);

  // Wire up voice if available
  if (typeof setupVoiceButton === 'function') {
    setupVoiceButton(textarea, voiceBtn, () => _mcSendMessage(sid));
  }

  // Populate preview with buffered content or fetch history
  _populateMcPreview(session);

  return card;
}

function _updateMcCardStatus(card, sid, status, substatus, title, session) {
  card.className = 'mc-card mc-card-' + status;
  const dot = document.getElementById('mc-dot-' + sid);
  if (dot) dot.className = 'mc-status-dot ' + status;
  const statusEl = document.getElementById('mc-status-' + sid);
  if (statusEl) {
    statusEl.textContent = _getMcStatusLabel(status, substatus);
    statusEl.className = 'mc-card-status ' + _getMcStatusClass(status);
  }
  const nameEl = document.getElementById('mc-name-' + sid);
  if (nameEl && title) {
    nameEl.title = title;
    nameEl.textContent = title;
  }
  if (session) {
    const metaEl = document.getElementById('mc-meta-' + sid);
    if (metaEl) {
      const metaParts = [];
      if (session.message_count) metaParts.push(session.message_count + ' msgs');
      const timeAgo = _mcTimeAgo(session.last_activity_ts);
      if (timeAgo) metaParts.push(timeAgo);
      metaEl.textContent = metaParts.join(' · ');
    }
  }
}

function _getMcStatusLabel(status, substatus) {
  if (substatus === 'compacting') return 'Compacting…';
  if (substatus === 'auto-resuming') return 'Resuming…';
  if (status === 'working') return 'Working';
  if (status === 'question') return 'Needs input';
  if (status === 'idle') return 'Idle';
  return 'Sleeping';
}

function _getMcStatusClass(status) {
  if (status === 'working') return 'status-working';
  if (status === 'question') return 'status-question';
  if (status === 'idle') return 'status-idle';
  return 'status-sleeping';
}

async function _populateMcPreview(session) {
  const sid = session.id;

  // Avoid redundant API calls on rapid re-renders
  if (_mcPreviewFetched.has(sid)) return;
  _mcPreviewFetched.add(sid);

  const previewEl = document.getElementById('mc-preview-' + sid);
  if (!previewEl) return;

  const project = localStorage.getItem('activeProject') || '';
  const url = '/api/session-log/' + encodeURIComponent(sid) +
              '?last=20' +
              (project ? '&project=' + encodeURIComponent(project) : '');

  try {
    const resp = await fetch(url);
    if (!resp.ok) { _mcPreviewFetched.delete(sid); return; }
    const data = await resp.json();
    const entries = (data.entries || []).filter(e =>
      e.kind === 'user' || e.kind === 'asst' || e.kind === 'assistant'
    );

    if (!entries.length) return;

    // Don't overwrite if live streaming already started while we were fetching
    if (previewEl.querySelector('.mc-stream-live')) return;

    previewEl.innerHTML = '';
    if (typeof renderLiveEntry === 'function') {
      entries.forEach(e => {
        const el = renderLiveEntry(e);
        el.querySelectorAll('.live-expand-btn, .vn-msg-footer, .smart-copy-wrap').forEach(n => n.remove());
        previewEl.appendChild(el);
      });
    }
    previewEl.scrollTop = previewEl.scrollHeight;
  } catch (e) {
    console.warn('[MC] preview fetch failed for', sid, e);
    _mcPreviewFetched.delete(sid);
  }
}

/** Filter handler called by the MC search input */
function mcFilterSessions() {
  const input = document.getElementById('mc-search');
  _mcSearchFilter = input ? input.value : '';
  renderMissionControl();
}

/** Open a session in normal grid view */
function _mcOpenSession(sid) {
  setSessionDisplayMode('grid');
  // openInGUI is the function that loads a session into the live panel
  if (typeof openInGUI === 'function') {
    openInGUI(sid);
  }
}

function _mcInputKeyDown(event, sid) {
  if (event.key === 'Enter' && !event.shiftKey) {
    event.preventDefault();
    _mcSendMessage(sid);
  }
}

function _mcSendMessage(sid, text) {
  const textarea = document.getElementById('mc-input-' + sid);
  const msg = (text !== undefined ? text : (textarea ? textarea.value.trim() : ''));
  if (!msg) return;
  if (textarea && text === undefined) {
    textarea.value = '';
    textarea.style.height = 'auto';
  }

  const _wasRunning = runningIds.has(sid);

  // Optimistic state update so the status dot flips to working immediately
  sessionKinds[sid] = 'working';
  runningIds.add(sid);
  if (typeof guiOpenAdd === 'function') guiOpenAdd(sid);
  renderMissionControl();

  if (_wasRunning) {
    // Session is alive — send_message handles idle/working/queue automatically
    socket.emit('send_message', { session_id: sid, text: msg });
  } else {
    // Session is sleeping — resume it with this message as the prompt
    socket.emit('start_session', {
      session_id: sid,
      prompt: msg,
      cwd: (typeof _currentProjectDir === 'function') ? _currentProjectDir() : '',
      resume: true,
    });
  }
}

/**
 * Called from socket.js stream_event handler to update a card's preview area
 * with streaming delta text. Throttled to 80ms (matching live panel).
 */
function mcUpdateStreamPreview(sid, deltaText) {
  if (sessionDisplayMode !== 'control') return;

  let buf = _mcStreamBuffers.get(sid);
  if (!buf) {
    buf = { text: '', timer: null };
    _mcStreamBuffers.set(sid, buf);
  }
  buf.text += deltaText;

  if (!buf.timer) {
    buf.timer = setTimeout(() => {
      buf.timer = null;
      const previewEl = document.getElementById('mc-preview-' + sid);
      if (!previewEl) return;
      let liveEl = previewEl.querySelector('.mc-stream-live');
      if (!liveEl) {
        liveEl = document.createElement('div');
        liveEl.className = 'mc-stream-live';
        previewEl.appendChild(liveEl);
      }
      liveEl.textContent = buf.text.slice(-600);
      previewEl.scrollTop = previewEl.scrollHeight;
    }, 80);
  }
}

/**
 * Called from socket.js session_entry handler when a complete entry arrives.
 * Appends the finalized entry as a rendered chat bubble.
 */
function mcFinalizeEntry(sid, text, kind) {
  if (sessionDisplayMode !== 'control') return;

  // Clear streaming buffer
  const buf = _mcStreamBuffers.get(sid);
  if (buf) {
    if (buf.timer) { clearTimeout(buf.timer); buf.timer = null; }
    buf.text = '';
    _mcStreamBuffers.delete(sid);
  }

  // Invalidate fetch cache so next MC open shows this newest entry
  _mcPreviewFetched.delete(sid);

  if (kind !== 'asst' && kind !== 'assistant' && kind !== 'user') return;

  const previewEl = document.getElementById('mc-preview-' + sid);
  if (!previewEl) return;

  // Remove the streaming element
  const liveEl = previewEl.querySelector('.mc-stream-live');
  if (liveEl) liveEl.remove();

  if (text && typeof renderLiveEntry === 'function') {
    const entryKind = (kind === 'assistant') ? 'asst' : kind;
    const el = renderLiveEntry({ kind: entryKind, text });
    el.querySelectorAll('.live-expand-btn, .vn-msg-footer, .smart-copy-wrap').forEach(n => n.remove());
    previewEl.appendChild(el);
    previewEl.scrollTop = previewEl.scrollHeight;
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
  // Date key matches sortedSessions() in sessions.js — uses effective_ts
  // (max of last-message, file mtime, and last-access) so any interaction
  // bubbles the session up, including view-only opens that don't write
  // to the .jsonl file.
  const dateKey = s => s.effective_ts || s.last_activity_ts || s.sort_ts || 0;
  if (wfSort === 'status') {
    copy.sort((a, b) => {
      const sa = statusOrder[getSessionStatus(a.id)] ?? 3;
      const sb = statusOrder[getSessionStatus(b.id)] ?? 3;
      if (sa !== sb) return sa - sb;
      return dateKey(b) - dateKey(a);
    });
  } else if (wfSort === 'name') {
    copy.sort((a, b) => (a.display_title||'').localeCompare(b.display_title||''));
  } else {
    // recent
    copy.sort((a, b) => dateKey(b) - dateKey(a));
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
    // Sleeping = substatus 'auto-resuming' regardless of state.  Covers
    // both phases: idle+auto-resuming (waiting for wake-up) and
    // working+auto-resuming (wake-up firing).  Keeps the workforce card
    // visually consistent with the live panel's "Awaiting wake-up…"
    // throughout the cycle.
    const _isSleepingCard = window._sessionSubstatus && window._sessionSubstatus[s.id] === 'auto-resuming';
    const emoji = _isCompacting
      ? '<svg class="compacting-icon" width="28" height="28" viewBox="0 0 24 24" fill="none" stroke="#aa88ff" stroke-width="1.5" stroke-linecap="round"><polyline points="4 14 10 14 10 20"/><polyline points="20 10 14 10 14 4"/><line x1="14" y1="10" x2="21" y2="3"/><line x1="3" y1="21" x2="10" y2="14"/></svg>'
      : _isSleepingCard
      ? statusSvg.sleeping
      : (statusSvg[st] || statusSvg.sleeping);
    const label = _isCompacting ? 'Compacting' : (_isSleepingCard ? 'Awaiting wake-up' : (statusLabel[st] || 'Sleeping'));
    const selClass = s.id === activeId ? ' wf-selected' : '';
    // Multi-select: include .multi-selected class on initial render so the
    // visual stays in sync after re-renders without an extra DOM pass.
    // onmousedown (added below) intercepts Ctrl/Cmd+click to toggle the
    // sidebar multi-selection without opening the session.  See
    // _sessionRowMouseDown in sessions.js for the full mechanism.
    const msClass = (typeof multiSelectedIds !== 'undefined' && multiSelectedIds.has(s.id)) ? ' multi-selected' : '';
    const name = escHtml((s.display_title||s.id).slice(0,22) + ((s.display_title||'').length>22?'\u2026':''));
    const date = _shortDate(s.last_activity);
    return `<div class="wf-card wf-${st}${selClass}${msClass}" data-sid="${s.id}" onmousedown="_sessionRowMouseDown(event,'${s.id}')" onclick="singleOrDouble('${s.id}',event)" oncontextmenu="sessionContextMenu(event,'${s.id}')" title="${escHtml(s.display_title)} \u2014 double-click to open in VibeNode">
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

// --- Compose hash-based routing: popstate handler ---
// Handles browser back/forward when navigating into or out of compose view.
// When compose gains sub-routes (e.g. #compose/project/{id}), extend this handler.
window.addEventListener('popstate', (e) => {
  const hash = window.location.hash;

  // If we were in compose and navigated away (back button), switch to the target mode
  if (typeof viewMode !== 'undefined' && viewMode === 'compose' && !hash.startsWith('#compose')) {
    if (typeof setViewMode !== 'function') return;
    // Determine target from the new hash
    if (hash.startsWith('#kanban')) {
      // Kanban's popstate guards on viewMode==='kanban', so we must switch first
      setViewMode('kanban');
    } else {
      // No hash or unknown hash — fall back to sessions view
      setViewMode('sessions');
    }
    return;
  }

  // If hash points to compose but we're not in compose mode, switch to it
  if (hash.startsWith('#compose') && typeof viewMode !== 'undefined' && viewMode !== 'compose') {
    if (typeof setViewMode === 'function') {
      setViewMode('compose');
    }
    return;
  }

  // Within compose: handle section drill-down vs board navigation
  // Use _renderComposeBoard (no pushState) to avoid creating duplicate history entries
  if (hash.startsWith('#compose') && typeof viewMode !== 'undefined' && viewMode === 'compose') {
    if (hash.startsWith('#compose/section/')) {
      const sectionId = hash.replace('#compose/section/', '');
      if (typeof renderSectionDetail === 'function') renderSectionDetail(sectionId);
    } else {
      if (typeof _renderComposeBoard === 'function') _renderComposeBoard();
    }
  }
});

// Restore compose view from hash on initial page load (called after sessions load).
// Similar to kanban's restoreFromHash() — ensures direct links to #compose work.
function restoreComposeFromHash() {
  const hash = window.location.hash;
  if (!hash || !hash.startsWith('#compose')) return;

  if (typeof viewMode !== 'undefined' && viewMode !== 'compose' && typeof setViewMode === 'function') {
    setViewMode('compose');
  }
}
