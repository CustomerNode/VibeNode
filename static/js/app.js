/* app.js — global state, project loading, session loading */

let allSessions = [];
/** O(1) lookup set kept in sync with allSessions — avoids O(n) .find()
 *  on every session_entry/session_permission/session_started event. */
let allSessionIds = new Set();
/** Rebuild allSessionIds from allSessions. Call after any reassignment of allSessions. */
function _rebuildSessionIds() { allSessionIds = new Set(allSessions.map(s => s.id)); }
let activeId = localStorage.getItem('activeSessionId') || null;
let renameTarget = null;
let sortMode = localStorage.getItem('sortMode') || 'date';
let sortAsc  = localStorage.getItem('sortAsc') === 'true';
let viewMode = localStorage.getItem('viewMode') || 'homepage';
// Migrate old view modes (grid/list collapse into sessions)
if (viewMode === 'workforce' || viewMode === 'list') viewMode = 'sessions';
// Guard against invalid view modes persisted in localStorage
if (!['homepage', 'sessions', 'workplace', 'kanban', 'compose'].includes(viewMode)) viewMode = 'sessions';
// First-ever load detection — if no viewMode was ever set, force homepage
if (!localStorage.getItem('viewMode')) viewMode = 'homepage';
// Hash-based routing: URL hash overrides stored viewMode for direct-link support
if (window.location.hash.startsWith('#kanban')) viewMode = 'kanban';
if (window.location.hash.startsWith('#compose')) viewMode = 'compose';
// Session display sub-mode (grid vs list within sessions view)
let sessionDisplayMode = localStorage.getItem('sessionDisplayMode') || 'grid';
if (!['grid', 'list'].includes(sessionDisplayMode)) sessionDisplayMode = 'grid';
let wfSort = localStorage.getItem('wfSort') || 'status';
let runningIds = new Set();
let waitingData = {};   // { session_id: {question, options, kind} }
let sessionKinds = {};   // session_id -> 'question' | 'working' | 'idle'
let liveSessionId = null;
let guiOpenSessions = new Set(JSON.parse(localStorage.getItem('guiOpenSessions') || '[]'));
let _activeGrpPopup = null;
let respondTarget = null;
let _allProjects = [];  // cached project list for overlay

// Workspace / Workplace state (used by workspace.js)
let workspaceActive = false;
let _wsExpandedId = null;
let permissionQueue = [];
let permissionPolicy = localStorage.getItem('permPolicy') || 'manual';
let customPolicies = JSON.parse(localStorage.getItem('customPolicies') || '{}');
let workspaceHiddenSessions = new Set(JSON.parse(localStorage.getItem('wsHiddenSessions') || '[]'));
let workspaceCardPositions = JSON.parse(localStorage.getItem('wsCardPositions') || '{}');
let _answerPending = {};
let _lastAnswer = {};
let _resendCount = {};
let _lastSendTimePerSession = {};
let _waitingPolledOnce = true;  // WebSocket push means we always have state

// Browser URL navigation for chats (mirrors folder navigation pattern)
let _skipChatHistory = false;
let _suppressSessionRestore = false;  // set during project switch to prevent loadSessions from auto-opening a saved session
function _pushChatUrl(chatId) {
  if (_skipChatHistory) return;
  // Don't push chat URLs in kanban mode — kanban manages its own history
  if (typeof viewMode !== 'undefined' && viewMode === 'kanban') return;
  const url = new URL(window.location);
  if (chatId) url.searchParams.set('chat', chatId);
  else url.searchParams.delete('chat');
  history.pushState({
    folder: (typeof _currentFolderId !== 'undefined' ? _currentFolderId : null),
    chat: chatId || null
  }, '', url);
}

async function loadProjects() {
  const res = await fetch('/api/projects');
  _allProjects = await res.json();
  const saved = localStorage.getItem('activeProject');

  // Update the button label
  const savedMatch = _allProjects.find(p => p.encoded === saved);
  _updateProjectLabel(savedMatch || _allProjects[0]);

  // If saved project has sessions use it; otherwise pick the project with the most sessions
  const target = (savedMatch && savedMatch.session_count > 0)
    ? saved
    : (_allProjects.slice().sort((a,b) => b.session_count - a.session_count)[0] || {}).encoded;
  if (target) {
    // If the saved project is the same as the target, skip the full
    // setProject teardown (which destroys live panels and clears state).
    // Just sync the server and load sessions without the nuclear reset.
    if (saved && saved === target) {
      localStorage.setItem('activeProject', target);
      await fetch('/api/set-project', {
        method: 'POST', headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({project: target})
      });
      await loadSessions();
    } else {
      await setProject(target, true);
    }
  } else {
    // No project available — clear skeleton and show prompt
    const listEl = document.getElementById('session-list');
    if (listEl) listEl.innerHTML = '<div class="empty-state" style="padding:24px;text-align:center;color:var(--text-muted);font-size:13px;">No projects found.<br>Click the project selector above to get started.</div>';
    document.getElementById('main-body').innerHTML = '<div style="display:flex;align-items:center;justify-content:center;height:100%;color:var(--text-muted);font-size:14px;flex-direction:column;gap:8px;"><svg width="32" height="32" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round"><path d="M22 19a2 2 0 0 1-2 2H4a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h5l2 3h9a2 2 0 0 1 2 2z"/></svg><span>Select a project to begin</span></div>';
  }
}

function _projectShortName(project) {
  if (project.custom_name) return project.custom_name;
  const parts = project.display.replace(/\\/g, '/').split('/');
  return parts.slice(-2).join('/');
}

function _updateProjectLabel(project) {
  const label = document.getElementById('project-label');
  if (!project) { label.innerHTML = 'Select project <span class="sidebar-mini-label">project</span>'; return; }
  const name = document.createElement('span');
  name.textContent = _projectShortName(project);
  label.innerHTML = name.innerHTML + ' <span class="sidebar-mini-label">project</span>';
}

// Generation counter: bumped on every project switch. Async callbacks
// (WebSocket handlers, fetch responses) check this to discard stale data
// that arrived after the user already switched projects.
let _projectSwitchGen = 0;

async function setProject(encoded, reload = true) {
  ++_projectSwitchGen;  // invalidate all in-flight async for old project
  // Save the current view mode and active session for the OLD project so we
  // can restore them when the user switches back later.
  const prevProject = localStorage.getItem('activeProject');
  if (prevProject) {
    if (activeId) localStorage.setItem('projectSession_' + prevProject, activeId);
    if (viewMode) localStorage.setItem('projectView_' + prevProject, viewMode);
    // Save per-view navigation position (drill-down, active session, etc.)
    if (typeof _saveViewPosition === 'function') _saveViewPosition(prevProject, viewMode);
  }
  // Tear down any active session/live-panel from the old project.
  // Use _skipChatHistory so deselectSession() doesn't push a history entry,
  // but then explicitly scrub the ?chat param so loadSessions() doesn't try
  // to restore a session ID that belongs to the old project.
  _skipChatHistory = true;
  if (activeId) {
    deselectSession();
  } else if (liveSessionId) {
    // Live panel open without activeId (e.g. brand-new session) — tear it down
    stopLivePanel();
  }
  _skipChatHistory = false;
  // Always clear stale session state from URL + localStorage
  activeId = null;
  liveSessionId = null;
  localStorage.removeItem('activeSessionId');
  const _cleanUrl = new URL(window.location);
  if (_cleanUrl.searchParams.has('chat')) {
    _cleanUrl.searchParams.delete('chat');
    history.replaceState({ folder: null, chat: null }, '', _cleanUrl);
  }
  // Reset main body to dashboard so the old chat isn't visible while new
  // project loads.
  const _mb = document.getElementById('main-body');
  if (_mb) _mb.innerHTML = _buildDashboard();
  setToolbarSession(null, 'No session selected', true, '');

  const p = _allProjects.find(x => x.encoded === encoded);
  _updateProjectLabel(p);
  localStorage.setItem('activeProject', encoded);
  // Clear cross-project session cache — sessions from the OLD project were
  // cached as "other project" and must be re-evaluated against the new project.
  // Sessions from the NEW project that were previously hidden will pass through
  // _isOtherProject on their next event. Sessions from the old project will be
  // re-detected and re-cached on their next event.
  if (typeof _clearCrossProjectCache === 'function') _clearCrossProjectCache();
  // Clear session state from the old project so cross-project sessions
  // don't bleed into the sidebar. The upcoming state_snapshot will
  // repopulate with current-project sessions only.
  sessionKinds = {};
  runningIds = new Set();
  waitingData = {};
  // Clear allSessions so old-project sessions don't remain visible in the
  // sidebar/grid during the gap between project switch and loadSessions().
  allSessions = [];
  allSessionIds.clear();
  // Clear per-session state maps that would otherwise carry stale data from
  // the old project into the new one (timestamps, substatus, usage, timers).
  window._sessionStateTs = {};
  window._workingSinceMap = {};
  if (window._sessionSubstatus) window._sessionSubstatus = {};
  if (window._sessionUsage) window._sessionUsage = {};
  // --- Clear view-specific state from the old project ---
  // Folder tree: stale folder IDs / cached tree from old project
  if (typeof _currentFolderId !== 'undefined') _currentFolderId = null;
  if (typeof _folderTreeCache !== 'undefined') _folderTreeCache = null;
  // Workspace: hidden sessions and card positions are per-project
  workspaceHiddenSessions = new Set();
  localStorage.removeItem('wsHiddenSessions');
  workspaceCardPositions = {};
  localStorage.removeItem('wsCardPositions');
  _wsExpandedId = null;
  const _wsBackBtn = document.getElementById('ws-back-btn');
  if (_wsBackBtn) _wsBackBtn.remove();
  // Kanban: reset board state if we're currently in kanban view
  if (viewMode === 'kanban' && typeof resetKanbanState === 'function') {
    resetKanbanState();
    const _kSessionBar = document.getElementById('kanban-session-bar');
    if (_kSessionBar) _kSessionBar.remove();
    // Scrub the drill-down hash so restoreFromHash() doesn't re-open old task
    const _kUrl = new URL(window.location);
    if (_kUrl.hash.startsWith('#kanban/task/')) {
      _kUrl.hash = '#kanban';
      history.replaceState({ view: 'kanban', taskId: null }, '', _kUrl.pathname + _kUrl.search + '#kanban');
    }
  }
  // Compose: reset board state if we're currently in compose view
  if (viewMode === 'compose') {
    if (typeof resetComposeState === 'function') resetComposeState();
    const _composeEl = document.getElementById('compose-board');
    if (_composeEl) { _composeEl.innerHTML = ''; }
    // Scrub any compose sub-route hash back to base
    const _cUrl = new URL(window.location);
    if (_cUrl.hash.startsWith('#compose/')) {
      _cUrl.hash = '#compose';
      history.replaceState({ view: 'compose' }, '', _cUrl.pathname + _cUrl.search + '#compose');
    }
  }
  // Kanban: clear persisted filters / expanded tasks / history that belong to old project
  kanbanExpandedTasks = new Set();
  localStorage.removeItem('kanbanExpanded');
  kanbanActiveTagFilter = [];
  sessionStorage.removeItem('kanbanTagFilter');
  if (typeof _kanbanHistory !== 'undefined') { _kanbanHistory = []; localStorage.removeItem('kanbanRecentHistory'); }
  // Show skeleton immediately
  if (reload) showSkeletonLoader();
  await fetch('/api/set-project', {
    method: 'POST', headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({project: encoded})
  });
  // Reset agent catalog so it gets re-written for the new project
  _agentCatalogPath = null;
  _agentCatalogPromise = null;
  // Update SocketIO query params so reconnects use the new project
  if (typeof socket !== 'undefined') {
    if (socket.io && socket.io.opts) socket.io.opts.query = { project: encoded };
    // Re-sync live session states from daemon (working_since, sessionKinds, etc.)
    if (socket.connected) socket.emit('request_state_snapshot', {project: encoded});
  }
  if (reload) {
    // Restore the last-open session only if the target view is sessions;
    // in every other view, suppress restore so the view resets to base state.
    // Use the saved per-project view (if any) to decide, since viewMode
    // hasn't been updated to the target yet at this point.
    const _targetView = localStorage.getItem('projectView_' + encoded) || viewMode;
    _suppressSessionRestore = (_targetView !== 'sessions');
    loadSessions();
  }
}

// --- Project Overlay ---
function openProjectOverlay() {
  const overlay = document.getElementById('project-overlay');
  const card = document.getElementById('project-card');
  const list = document.getElementById('project-list');
  const saved = localStorage.getItem('activeProject');

  list.innerHTML = _allProjects.map(p => {
    const shortName = _projectShortName(p);
    const isActive = p.encoded === saved;
    return `
    <div class="project-item${isActive ? ' active' : ''}" data-encoded="${escHtml(p.encoded)}" data-name="${escHtml(shortName)}">
      <div class="project-item-info">
        <div class="project-item-name">${escHtml(shortName)}</div>
        <div class="project-item-path">${escHtml(p.display)}</div>
      </div>
      <span class="project-item-count">${p.session_count} sessions</span>
      <div class="project-item-actions">
        <button class="project-act-btn project-rename-btn" title="Rename">
          <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M11 4H4a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h14a2 2 0 0 0 2-2v-7"/><path d="M18.5 2.5a2.121 2.121 0 0 1 3 3L12 15l-4 1 1-4 9.5-9.5z"/></svg>
        </button>
        <button class="project-act-btn danger project-delete-btn" title="Delete">
          <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><polyline points="3 6 5 6 21 6"/><path d="M19 6v14a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2V6m3 0V4a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2"/></svg>
        </button>
      </div>
    </div>`;
  }).join('');

  // Event delegation for project items
  list.querySelectorAll('.project-item').forEach(item => {
    const enc = item.dataset.encoded;
    const name = item.dataset.name;
    item.querySelector('.project-item-info').onclick = () => selectProjectFromOverlay(enc);
    item.querySelector('.project-rename-btn').onclick = e => { e.stopPropagation(); renameProjectOverlay(enc, name); };
    item.querySelector('.project-delete-btn').onclick = e => { e.stopPropagation(); deleteProjectOverlay(enc, name); };
  });

  overlay.classList.add('show');
  card.classList.add('pm-enter');
  requestAnimationFrame(() => card.classList.remove('pm-enter'));

  // Close on backdrop click
  overlay.onclick = e => { if (e.target === overlay) closeProjectOverlay(); };
}

function closeProjectOverlay() {
  document.getElementById('project-overlay').classList.remove('show');
}

async function selectProjectFromOverlay(encoded) {
  closeProjectOverlay();
  const p = _allProjects.find(x => x.encoded === encoded);
  const name = p ? _projectShortName(p) : 'project';

  // Show full-screen loading overlay — enforce a minimum display time
  // so the animation feels intentional, not just a flash.
  _showProjectSwitchLoader(name);
  const _minDisplay = new Promise(r => setTimeout(r, 1800));

  await setProject(encoded, true);

  // Restore the view the user was last using in this project, or fall back
  // to the current view.  Re-entering the mode re-initialises it at its
  // base state with saved drill-down / session position restored by
  // _setViewModeImmediate via _restoreViewPosition().
  const _savedView = localStorage.getItem('projectView_' + encoded);
  const _targetView = _savedView || viewMode || 'homepage';
  if (typeof setViewMode === 'function') setViewMode(_targetView);

  // Wait for minimum display time before dismissing
  await _minDisplay;
  _hideProjectSwitchLoader();
}

function _showProjectSwitchLoader(projectName) {
  let overlay = document.getElementById('project-switch-loader');
  if (overlay) overlay.remove();

  overlay = document.createElement('div');
  overlay.id = 'project-switch-loader';
  overlay.innerHTML = `
    <div class="psl-content">
      <div class="psl-orb-wrap">
        <div class="psl-orb"></div>
        <div class="psl-ring"></div>
        <div class="psl-ring psl-ring-2"></div>
      </div>
      <div class="psl-text">
        <div class="psl-label">Switching to</div>
        <div class="psl-name">${escHtml(projectName)}</div>
      </div>
      <div class="psl-dots"><span></span><span></span><span></span></div>
    </div>`;
  document.body.appendChild(overlay);
  // Force reflow then add .visible for transition
  overlay.offsetHeight;
  overlay.classList.add('visible');
}

function _hideProjectSwitchLoader() {
  const overlay = document.getElementById('project-switch-loader');
  if (!overlay) return;
  overlay.classList.add('done');
  setTimeout(() => overlay.remove(), 900);
}

async function renameProjectOverlay(encoded, currentName) {
  // Close the project overlay first so the prompt dialog has a clean backdrop
  closeProjectOverlay();
  const newName = await showPrompt('Rename Project', '<p>Enter a display name for this project. The directory stays the same.</p>', {
    placeholder: 'Project name',
    value: currentName,
    confirmText: 'Rename',
  });
  if (newName === null || newName === currentName) return;
  // Save to server (project rename endpoint)
  try {
    const resp = await fetch('/api/rename-project', {
      method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({encoded, name: newName})
    });
    const data = await resp.json();
    if (data.ok) {
      showToast('Renamed to "' + newName + '"');
      // Refresh project list and update the label without a full reload
      const res = await fetch('/api/projects');
      _allProjects = await res.json();
      const p = _allProjects.find(x => x.encoded === encoded);
      if (p) _updateProjectLabel(p);
    } else {
      showToast(data.error || 'Rename failed', true);
    }
  } catch(e) {
    showToast('Rename failed', true);
  }
}

async function deleteProjectOverlay(encoded, name) {
  const confirmed = await showConfirm('Delete Project', `<p>Delete <strong>${escHtml(name)}</strong> and all its sessions?</p><p>This cannot be undone.</p>`, {
    danger: true,
    confirmText: 'Delete',
    icon: '<svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><polyline points="3 6 5 6 21 6"/><path d="M19 6v14a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2V6m3 0V4a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2"/></svg>',
  });
  if (!confirmed) return;
  try {
    const resp = await fetch('/api/delete-project', {
      method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({encoded})
    });
    const data = await resp.json();
    if (data.ok) {
      showToast('Project deleted');
      await loadProjects();
      openProjectOverlay();
    } else {
      showToast(data.error || 'Delete failed', true);
    }
  } catch(e) {
    showToast('Delete failed', true);
  }
}

function addProjectOverlay() {
  // Replace the project list with mode picker
  const list = document.getElementById('project-list');
  const footer = document.querySelector('.project-footer');
  footer.style.display = 'none';

  list.innerHTML = `
    <div style="padding:8px 16px 16px;">
      <div class="add-mode-card" onclick="addProjectBrowse()">
        <div class="add-mode-icon">
          <svg width="24" height="24" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5"><path d="M22 19a2 2 0 0 1-2 2H4a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h5l2 3h9a2 2 0 0 1 2 2z"/></svg>
        </div>
        <div class="add-mode-info">
          <div class="add-mode-title">Browse</div>
          <div class="add-mode-desc">Pick a folder from your computer</div>
        </div>
      </div>
      <div class="add-mode-card" onclick="addProjectFind()">
        <div class="add-mode-icon">
          <svg width="24" height="24" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5"><circle cx="11" cy="11" r="8"/><line x1="21" y1="21" x2="16.65" y2="16.65"/></svg>
        </div>
        <div class="add-mode-info">
          <div class="add-mode-title">Find Projects</div>
          <div class="add-mode-desc">Scan your computer for code projects</div>
        </div>
      </div>
      <div class="add-mode-card" onclick="addProjectCreate()">
        <div class="add-mode-icon">
          <svg width="24" height="24" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5"><line x1="12" y1="5" x2="12" y2="19"/><line x1="5" y1="12" x2="19" y2="12"/></svg>
        </div>
        <div class="add-mode-info">
          <div class="add-mode-title">Create New</div>
          <div class="add-mode-desc">Start a new empty project</div>
        </div>
      </div>
      <button class="pm-btn pm-btn-secondary" onclick="openProjectOverlay()" style="width:100%;margin-top:8px;">Back to Projects</button>
    </div>`;
}

async function addProjectBrowse() {
  closeProjectOverlay();
  showToast('Opening folder picker\u2026');
  try {
    const resp = await fetch('/api/add-project', { method: 'POST', headers: {'Content-Type':'application/json'}, body: JSON.stringify({mode:'browse'}) });
    const data = await resp.json();
    if (data.cancelled) { openProjectOverlay(); return; }
    if (data.ok) {
      showToast('Added ' + data.path);
      await loadProjects();
      openProjectOverlay();
    } else {
      showToast(data.error || 'Add failed', true);
      openProjectOverlay();
    }
  } catch(e) { showToast('Add failed', true); openProjectOverlay(); }
}

let _findChat = null;

function addProjectFind() {
  const list = document.getElementById('project-list');
  const footer = document.querySelector('.project-footer');
  footer.style.display = 'none';
  list.style.padding = '0';

  // Destroy previous chat if any
  if (_findChat) { _findChat.destroy(); _findChat = null; }

  list.innerHTML = '<div id="find-chat-container" style="height:340px;"></div>'
    + '<div style="padding:8px 16px 12px;"><button class="pm-btn pm-btn-secondary" onclick="destroyFindChat();openProjectOverlay();" style="width:100%;">Back to Projects</button></div>';

  const container = document.getElementById('find-chat-container');
  _findChat = new ChatComponent(container, {
    placeholder: 'Describe the project you\'re looking for\u2026',
    systemMessage: 'Tell me what project you\'re looking for \u2014 I\'ll search your computer. Try something like "my Python web app" or "the React project I was working on".',
    suggestions: ['Python projects', 'Node.js apps', 'Git repositories', 'Recent code projects'],
    onSend: async (text, messages) => {
      // Check if user clicked an "Add" suggestion
      const addMatch = text.match(/^(.+?)\s*\u2014\s*Add$/);
      if (addMatch && _lastFindMatches) {
        const match = _lastFindMatches.find(m => m.name === addMatch[1]);
        if (match) {
          try {
            const r = await fetch('/api/add-project', { method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify({mode:'path', path: match.path}) });
            const d = await r.json();
            if (d.ok) {
              showToast('Added ' + match.name);
              await loadProjects();
              return { content: '**' + match.name + '** has been added! You can search for more or go back to your projects.', suggestions: ['Back to projects'] };
            }
          } catch(e) {}
          return 'Failed to add that project. Try again.';
        }
      }
      if (text === 'Back to projects') {
        destroyFindChat();
        openProjectOverlay();
        return null;
      }
      if (text === 'Browse for folder') {
        destroyFindChat();
        addProjectBrowse();
        return null;
      }

      // Search via backend
      const resp = await fetch('/api/project-chat', {
        method: 'POST', headers: {'Content-Type':'application/json'},
        body: JSON.stringify({message: text})
      });
      const data = await resp.json();
      _lastFindMatches = data.matches || [];
      return { content: data.content, suggestions: data.suggestions || [] };
    }
  });
}

let _lastFindMatches = [];

function destroyFindChat() {
  if (_findChat) { _findChat.destroy(); _findChat = null; }
  _lastFindMatches = [];
  const list = document.getElementById('project-list');
  if (list) list.style.padding = '';
}

async function addProjectCreate() {
  const name = await showPrompt('Create Project', '<p>Enter a name for your new project.</p>', {
    placeholder: 'My Project',
    confirmText: 'Create',
    icon: '<svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round"><path d="M22 19a2 2 0 0 1-2 2H4a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h5l2 3h9a2 2 0 0 1 2 2z"/></svg>',
  });
  if (!name) return;
  try {
    const resp = await fetch('/api/add-project', {
      method: 'POST', headers: {'Content-Type':'application/json'},
      body: JSON.stringify({mode:'create', name})
    });
    const data = await resp.json();
    if (data.ok) {
      showToast('Created ' + name);
      await loadProjects();
      openProjectOverlay();
    } else { showToast(data.error || 'Create failed', true); }
  } catch(e) { showToast('Create failed', true); }
}

// --- Sidebar collapse ---
function toggleSidebar() {
  const sidebar = document.querySelector('.sidebar');
  const expandBtn = document.getElementById('btn-sidebar-expand');
  const collapsed = sidebar.classList.toggle('collapsed');
  expandBtn.classList.toggle('visible', collapsed);
  localStorage.setItem('sidebarCollapsed', collapsed ? '1' : '');
}
// Restore on load
if (localStorage.getItem('sidebarCollapsed') === '1') {
  document.querySelector('.sidebar').classList.add('collapsed');
  document.getElementById('btn-sidebar-expand').classList.add('visible');
}

// --- View mode selector ---
const _viewModes = {
  homepage: {
    icon: '<svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M3 9l9-7 9 7v11a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2z"/><polyline points="9 22 9 12 15 12 15 22"/></svg>',
    label: 'Home',
    title: 'Home',
    desc: 'VibeNode homepage',
  },
  sessions: {
    icon: '<svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><rect x="3" y="3" width="7" height="7"/><rect x="14" y="3" width="7" height="7"/><rect x="3" y="14" width="7" height="7"/><rect x="14" y="14" width="7" height="7"/></svg>',
    label: 'Sessions View',
    title: 'Sessions',
    desc: 'Run and interact with your Claude Code sessions',
  },
  kanban: {
    icon: '<svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><rect x="3" y="3" width="5" height="18" rx="1"/><rect x="10" y="3" width="5" height="12" rx="1"/><rect x="17" y="3" width="5" height="15" rx="1"/></svg>',
    label: 'Workflow View',
    title: 'Workflow',
    desc: 'Task board with workflow columns and AI session orchestration',
  },
  workplace: {
    icon: '<svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><rect x="2" y="3" width="20" height="14" rx="2"/><line x1="8" y1="21" x2="16" y2="21"/><line x1="12" y1="17" x2="12" y2="21"/><circle cx="7" cy="10" r="1.5"/><circle cx="17" cy="10" r="1.5"/><path d="M10 10h4"/></svg>',
    label: 'Workforce View',
    title: 'Workforce',
    desc: 'Knowledge asset library — manage skills and agent definitions',
  },
  compose: {
    icon: '<svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M12 20h9"/><path d="M16.5 3.5a2.121 2.121 0 0 1 3 3L7 19l-4 1 1-4L16.5 3.5z"/></svg>',
    label: 'Compose View',
    title: 'Compose',
    desc: 'Documents, diagrams, and knowledge creation board',
  },
};

function openViewModeSelector() {
  setViewMode('homepage');
}

const _viewNames = { homepage: 'Home', sessions: 'Sessions', kanban: 'Workflow', workplace: 'Workforce', compose: 'Compose' };
const _viewIcons = {
  homepage: '<svg width="14" height="14" viewBox="0 0 24 24" fill="currentColor" stroke="none"><path d="M3 9l9-7 9 7v11a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2z"/><path d="M9 22V12h6v10" fill="var(--bg-body)"/></svg>',
  sessions: '<svg width="14" height="14" viewBox="0 0 24 24" fill="currentColor" stroke="none"><rect x="3" y="3" width="7" height="7" rx="1"/><rect x="14" y="3" width="7" height="7" rx="1"/><rect x="3" y="14" width="7" height="7" rx="1"/><rect x="14" y="14" width="7" height="7" rx="1"/></svg>',
  kanban: '<svg width="14" height="14" viewBox="0 0 24 24" fill="currentColor" stroke="none"><rect x="3" y="3" width="5" height="18" rx="1"/><rect x="10" y="3" width="5" height="12" rx="1"/><rect x="17" y="3" width="5" height="15" rx="1"/></svg>',
  workplace: '<svg width="14" height="14" viewBox="0 0 24 24" fill="currentColor" stroke="none"><rect x="2" y="3" width="20" height="14" rx="2"/><rect x="8" y="20" width="8" height="2" rx="1"/><rect x="11" y="17" width="2" height="4"/><circle cx="7" cy="10" r="1.5" fill="var(--bg-body)"/><circle cx="17" cy="10" r="1.5" fill="var(--bg-body)"/><rect x="10" y="9.5" width="4" height="1.5" rx="0.5" fill="var(--bg-body)"/></svg>',
  compose: '<svg width="14" height="14" viewBox="0 0 24 24" fill="currentColor" stroke="none"><path d="M16.5 3.5a2.121 2.121 0 0 1 3 3L7 19l-4 1 1-4L16.5 3.5z"/><rect x="12" y="19" width="9" height="2" rx="1"/></svg>'
};

function _updateViewModeButton(mode) {
  // Update trigger label and icon
  const label = document.getElementById('sidebar-view-label');
  const icon = document.getElementById('sidebar-view-icon');
  if (label) label.textContent = _viewNames[mode] || mode;
  if (icon && _viewIcons[mode]) icon.outerHTML = '<svg id="sidebar-view-icon" ' + _viewIcons[mode].slice(5);
  // Highlight active flyout option
  const flyout = document.getElementById('sidebar-view-flyout');
  if (flyout) {
    flyout.querySelectorAll('.sidebar-view-opt').forEach(btn => {
      btn.classList.toggle('active', btn.dataset.view === mode);
    });
  }
}

// View flyout hover with delayed open (100ms) and delayed collapse (300ms)
(function() {
  let _openTimer = null;
  let _closeTimer = null;
  const wrap = document.querySelector('.sidebar-view-wrap');
  if (!wrap) return;
  wrap.addEventListener('mouseenter', () => {
    if (_closeTimer) { clearTimeout(_closeTimer); _closeTimer = null; }
    _openTimer = setTimeout(() => wrap.classList.add('flyout-open'), 200);
  });
  wrap.addEventListener('mouseleave', () => {
    if (_openTimer) { clearTimeout(_openTimer); _openTimer = null; }
    _closeTimer = setTimeout(() => wrap.classList.remove('flyout-open'), 300);
  });
})();

// Hydrate view mode button on load
_updateViewModeButton(viewMode);

// Hydrate sort label on load
(function() {
  const opts = document.querySelectorAll('.sidebar-sort-opt');
  opts.forEach(el => {
    if (el.dataset.sort === sortMode && String(el.dataset.asc) === String(sortAsc)) {
      el.classList.add('active');
      const sortLabel = document.getElementById('sidebar-sort-label');
      if (sortLabel) sortLabel.textContent = el.textContent;
    }
  });
})();

// --- Sidebar menu (three dots) ---
function toggleSidebarMenu() {
  document.getElementById('sidebar-menu-dropdown').classList.toggle('open');
}
function closeSidebarMenu() {
  document.getElementById('sidebar-menu-dropdown').classList.remove('open');
}

function pickSort(mode, asc) {
  closeSidebarMenu();
  // Find the label from the clicked option
  const opts = document.querySelectorAll('.sidebar-sort-opt');
  let label = mode;
  opts.forEach(el => {
    const match = el.dataset.sort === mode && String(el.dataset.asc) === String(asc);
    el.classList.toggle('active', match);
    if (match) label = el.textContent;
  });
  const sortLabel = document.getElementById('sidebar-sort-label');
  if (sortLabel) sortLabel.textContent = label;
  // Apply sort
  sortAsc = !!asc;
  if (viewMode === 'sessions' && sessionDisplayMode === 'grid') {
    setWfSort(mode === 'date' ? 'recent' : mode);
  } else {
    setSort(mode);
  }
  filterSessions();
}

// Close sidebar menu on outside click
document.addEventListener('click', function(e) {
  const wrap = document.querySelector('.sidebar-menu-wrap');
  if (wrap && !wrap.contains(e.target)) closeSidebarMenu();
});

// --- Sleep All ---
async function sleepAllSessions() {
  const running = allSessions.filter(s => runningIds.has(s.id));
  if (!running.length) { showToast('No running sessions'); return; }
  const ok = await showConfirm('Sleep All Sessions', '<p>Close <strong>' + running.length + '</strong> running session' + (running.length > 1 ? 's' : '') + ' in this workspace?</p>', { danger: true, confirmText: 'Sleep All', icon: '<svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><path d="M21 12.79A9 9 0 1 1 11.21 3 7 7 0 0 0 21 12.79z"/></svg>' });
  if (!ok) return;
  let closed = 0;
  for (const s of running) {
    socket.emit('close_session', {session_id: s.id});
    runningIds.delete(s.id);
    delete sessionKinds[s.id];
    closed++;
  }
  showToast(closed + ' session' + (closed !== 1 ? 's' : '') + ' closed');
  guiOpenSessions.clear();
  localStorage.setItem('guiOpenSessions', '[]');
  if (liveSessionId) {
    liveBarState = null;
    updateLiveInputBar();
  }
  filterSessions();
}

// --- Delete All ---
async function deleteAllSessions() {
  const count = allSessions.length;
  if (!count) { showToast('No sessions to delete'); return; }
  const ok = await showConfirm('Delete All Sessions', '<p>Permanently delete <strong>all ' + count + ' sessions</strong> in this workspace?</p><p>This cannot be undone.</p>', { danger: true, confirmText: 'Delete All', icon: '<svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><path d="M10.29 3.86L1.82 18a2 2 0 0 0 1.71 3h16.94a2 2 0 0 0 1.71-3L13.71 3.86a2 2 0 0 0-3.42 0z"/><line x1="12" y1="9" x2="12" y2="13"/><line x1="12" y1="17" x2="12.01" y2="17"/></svg>' });
  if (!ok) return;
  // Double confirm for safety
  const ok2 = await showConfirm('Are you sure?', '<p>This will permanently delete <strong>' + count + ' sessions</strong> and all their history.</p>', { danger: true, confirmText: 'Yes, delete everything' });
  if (!ok2) return;
  showToast('Deleting ' + count + ' sessions…');
  if (liveSessionId) stopLivePanel();
  deselectSession();
  const _dap = localStorage.getItem('activeProject') || '';
  const _dapQ = _dap ? '?project=' + encodeURIComponent(_dap) : '';
  const resp = await fetch('/api/delete-all' + _dapQ, { method: 'DELETE' });
  const data = await resp.json();
  await loadSessions();
  loadProjects();                       // refresh splash-screen session counts
  showToast((data.deleted || count) + ' sessions deleted');
}

// --- Bulk Operations Modal ---
function openBulkOperations() {
  const overlay = document.getElementById('pm-overlay');

  // Gather counts for dynamic descriptions
  const hiddenCount = (typeof workspaceHiddenSessions !== 'undefined' && workspaceHiddenSessions) ? workspaceHiddenSessions.size : 0;
  const runningCount = allSessions.filter(s => runningIds.has(s.id)).length;
  const emptyCount = allSessions.filter(s => s.message_count === 0).length;
  const untitledCount = allSessions.filter(s => !s.custom_title && s.message_count > 0).length;
  const totalCount = allSessions.length;

  const operations = [
    {
      icon: '<svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><path d="M1 12s4-8 11-8 11 8 11 8-4 8-11 8-11-8-11-8z"/><circle cx="12" cy="12" r="3"/></svg>',
      title: 'Unhide All Sessions',
      desc: hiddenCount > 0 ? hiddenCount + ' hidden session' + (hiddenCount !== 1 ? 's' : '') : 'No hidden sessions',
      disabled: hiddenCount === 0,
      danger: false,
      action: "wsShowAll();closeBulkOperations();showToast('All sessions unhidden')"
    },
    {
      icon: '<svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><path d="M21 12.79A9 9 0 1 1 11.21 3 7 7 0 0 0 21 12.79z"/></svg>',
      title: 'Sleep All Sessions',
      desc: runningCount > 0 ? runningCount + ' running session' + (runningCount !== 1 ? 's' : '') : 'No running sessions',
      disabled: runningCount === 0,
      danger: false,
      action: "closeBulkOperations();sleepAllSessions()"
    },
    {
      icon: '<svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><path d="M12 20h9"/><path d="M16.5 3.5a2.121 2.121 0 0 1 3 3L7 19l-4 1 1-4L16.5 3.5z"/></svg>',
      title: 'Auto-name All Sessions',
      desc: untitledCount > 0 ? untitledCount + ' untitled session' + (untitledCount !== 1 ? 's' : '') : 'All sessions named',
      disabled: untitledCount === 0,
      danger: false,
      action: "closeBulkOperations();autoNameAllSessions()"
    },
    {
      icon: '<svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><polyline points="3 6 5 6 21 6"/><path d="M19 6v14a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2V6m3 0V4a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2"/><line x1="10" y1="11" x2="10" y2="17"/><line x1="14" y1="11" x2="14" y2="17"/></svg>',
      title: 'Delete Empty Sessions',
      desc: emptyCount > 0 ? emptyCount + ' empty session' + (emptyCount !== 1 ? 's' : '') : 'No empty sessions',
      disabled: emptyCount === 0,
      danger: true,
      action: "closeBulkOperations();deleteEmptySessions()"
    },
    {
      icon: '<svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><path d="M10.29 3.86L1.82 18a2 2 0 0 0 1.71 3h16.94a2 2 0 0 0 1.71-3L13.71 3.86a2 2 0 0 0-3.42 0z"/><line x1="12" y1="9" x2="12" y2="13"/><line x1="12" y1="17" x2="12.01" y2="17"/></svg>',
      title: 'Delete All Sessions',
      desc: totalCount > 0 ? totalCount + ' session' + (totalCount !== 1 ? 's' : '') + ' in workspace' : 'No sessions',
      disabled: totalCount === 0,
      danger: true,
      action: "closeBulkOperations();deleteAllSessions()"
    },
  ];

  var html = '<div class="pm-card pm-enter" style="width:480px;max-width:92vw;">';
  html += '<div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:4px;">';
  html += '<h2 class="pm-title" style="margin:0;">Bulk Operations</h2>';
  html += '<button class="pm-btn pm-btn-secondary" style="padding:4px 10px;font-size:12px;" onclick="closeBulkOperations()">&times;</button>';
  html += '</div>';
  html += '<div class="pm-body"><p>Perform actions across all sessions in this workspace.</p></div>';
  html += '<div class="bulk-ops-list">';

  for (var i = 0; i < operations.length; i++) {
    var op = operations[i];
    html += '<div class="bulk-ops-item' + (op.disabled ? ' disabled' : '') + '">';
    html += '<div class="bulk-ops-icon' + (op.danger ? ' danger' : '') + '">' + op.icon + '</div>';
    html += '<div class="bulk-ops-info">';
    html += '<div class="bulk-ops-title">' + op.title + '</div>';
    html += '<div class="bulk-ops-desc">' + op.desc + '</div>';
    html += '</div>';
    html += '<button class="pm-btn ' + (op.danger ? 'pm-btn-danger' : 'pm-btn-secondary') + ' bulk-ops-btn" '
          + (op.disabled ? 'disabled ' : '')
          + 'onclick="' + op.action + '">Run</button>';
    html += '</div>';
  }

  html += '</div></div>';

  overlay.innerHTML = html;
  overlay.classList.add('show');
  requestAnimationFrame(function() {
    var card = overlay.querySelector('.pm-card');
    if (card) card.classList.remove('pm-enter');
  });
}

function closeBulkOperations() {
  _closePm();
}

// --- Auto-name All Sessions ---
async function autoNameAllSessions() {
  const untitled = allSessions.filter(s => !s.custom_title && s.message_count > 0);
  if (!untitled.length) { showToast('All sessions already named'); return; }

  const ok = await showConfirm(
    'Auto-name All Sessions',
    '<p>Auto-name <strong>' + untitled.length + '</strong> untitled session' + (untitled.length !== 1 ? 's' : '') + '?</p>'
    + '<p>This uses AI to generate meaningful names based on each session\u2019s content. Sessions with user-set names will be skipped.</p>',
    {
      confirmText: 'Name All',
      icon: '<svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><path d="M12 20h9"/><path d="M16.5 3.5a2.121 2.121 0 0 1 3 3L7 19l-4 1 1-4L16.5 3.5z"/></svg>'
    }
  );
  if (!ok) return;

  showToast('Naming ' + untitled.length + ' session' + (untitled.length !== 1 ? 's' : '') + '\u2026');
  let named = 0;
  for (const s of untitled) {
    try {
      const resp = await fetch('/api/autonname/' + s.id, { method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({project: localStorage.getItem('activeProject') || ''}) });
      const data = await resp.json();
      if (data.ok && data.title) {
        s.custom_title = data.title;
        s.display_title = data.title;
        named++;
      }
    } catch(e) { /* skip failures silently */ }
  }
  filterSessions();
  // Update toolbar if the active session was renamed
  if (activeId) {
    const active = allSessions.find(x => x.id === activeId);
    if (active && active.custom_title) {
      setToolbarSession(activeId, active.custom_title, false, active.custom_title);
    }
  }
  showToast('Named ' + named + ' session' + (named !== 1 ? 's' : ''));
}

// --- New Agent ---
async function addNewAgent() {
  // If on homepage, switch to sessions mode first
  if (viewMode === 'homepage') setViewMode('sessions');

  const newId = crypto.randomUUID();

  // Optimistic UI: add placeholder to sidebar
  const optimistic = {
    id: newId,
    display_title: 'New Session',
    custom_title: '',
    last_activity: '',
    size: '',
    message_count: 0,
    preview: '',
  };
  allSessions.unshift(optimistic);
  allSessionIds.add(optimistic.id);
  filterSessions();

  // Mark as GUI-opened but DON'T add to runningIds or sessionKinds yet.
  // The session doesn't exist on the server until _newSessionSubmit sends it.
  guiOpenAdd(newId);

  // In workplace mode, expand the card
  if (workspaceActive) {
    // Set workspace expanded state but DON'T call expandWorkspaceCard
    // (it would start the live panel before the user has typed anything)
    _wsExpandedId = newId;
    activeId = newId;
    localStorage.setItem('activeSessionId', newId);
    document.getElementById('main-toolbar').style.display = '';
    setToolbarSession(newId, 'New Session', true, '');
    _addWorkspaceBackBtn();
  } else {
    if (typeof _ensureMainBodyVisible === 'function') _ensureMainBodyVisible();
    activeId = newId;
    localStorage.setItem('activeSessionId', newId);
    setToolbarSession(newId, 'New Session', true, '');
  }

  // Update URL/history so back/forward and reload work the same as clicking a session
  _pushChatUrl(newId);

  // Register session in current folder if in workplace hierarchy
  if (workspaceActive && typeof addSessionToFolder === 'function' && typeof _currentFolderId !== 'undefined' && _currentFolderId) {
    addSessionToFolder(newId, _currentFolderId);
  }

  // Show empty chat with focused input — no dialog, no spinner
  document.getElementById('main-body').innerHTML =
    '<div class="live-panel" id="live-panel">' +
    '<div class="conversation live-log" id="live-log">' +
    '<div class="empty-state" style="padding:60px 0;text-align:center;">' +
    '<svg width="32" height="32" viewBox="0 0 24 24" fill="none" stroke="var(--text-faint)" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round" style="margin-bottom:12px;opacity:0.4;"><polyline points="16 18 22 12 16 6"/><polyline points="8 6 2 12 8 18"/></svg>' +
    '<div class="vibenode-greeting">What will we VibeNode today?</div>' +
    (typeof _renderTemplateGrid === 'function' ? _renderTemplateGrid(newId) : '') +
    '</div></div>' +
    '<div class="live-input-bar" id="live-input-bar"></div></div>';

  // Show idle input bar immediately — user types their first message here
  liveSessionId = newId;
  liveLineCount = 0;
  liveAutoScroll = true;
  liveBarState = null;
  _optimisticMsgId = 0;

  const bar = document.getElementById('live-input-bar');
  if (bar) {
    bar.innerHTML =
      '<textarea id="live-input-ta" class="live-textarea" rows="3" placeholder="Describe what you want Claude to do\u2026" autofocus' +
      ' onkeydown="if(_shouldSend(event)){event.preventDefault();_newSessionSubmit(\'' + newId + '\')}">' +
      '</textarea>' +
      '<div class="live-bar-row">' +
      '<span class="send-hint" style="font-size:10px;color:var(--text-faint);">' + _sendHint() + '</span>' +
      '<button class="live-send-btn" id="live-voice-btn"></button>' +
      '</div>';
    setupVoiceButton(document.getElementById('live-input-ta'), document.getElementById('live-voice-btn'), () => _newSessionSubmit(newId));
    setTimeout(() => {
      const ta = document.getElementById('live-input-ta');
      if (ta) {
        ta.focus();
        ta.addEventListener('input', function() { if (typeof _hideTemplateGrid === 'function') _hideTemplateGrid(); });
      }
    }, 50);
  }
}

// ---------------------------------------------------------------------------
// Agent catalog — write all definitions to a temp file, system prompt gets
// just a single-line pointer.  Keeps the CLI argument short.
// ---------------------------------------------------------------------------

let _agentCatalogPath = null;
let _agentCatalogPromise = null;

/**
 * POST all agent definitions to the backend which writes them to a temp
 * file.  Returns the absolute file path (cached after first call).
 */
async function _ensureAgentCatalog() {
  if (_agentCatalogPath) return _agentCatalogPath;
  if (_agentCatalogPromise) return _agentCatalogPromise;

  _agentCatalogPromise = (async () => {
    try {
      if (typeof FOLDER_SUPERSET !== 'object' || !FOLDER_SUPERSET) return null;

      const agents = Object.entries(FOLDER_SUPERSET)
        .filter(([, def]) => def.skill && def.skill.systemPrompt)
        .map(([id, def]) => ({
          id,
          label: def.skill.label || def.name,
          systemPrompt: def.skill.systemPrompt,
        }));
      if (!agents.length) return null;

      const resp = await fetch('/api/agents/write-catalog', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ agents }),
      });
      const data = await resp.json();
      if (data.ok && data.path) {
        _agentCatalogPath = data.path;
        return _agentCatalogPath;
      }
    } catch (e) {
      console.warn('Failed to write agent catalog:', e);
    }
    return null;
  })();

  return _agentCatalogPromise;
}

/**
 * Returns a single-line system prompt pointer to the agent catalog file.
 */
function _buildAgentDefinitions() {
  if (!_agentCatalogPath) return '';
  return 'You have specialist agents available. Read the agent catalog file at ' +
    _agentCatalogPath + ' for the full list, instructions, and system prompts. ' +
    'You MUST read that file before spawning any agent.';
}

async function _newSessionSubmit(sessionId) {
  const ta = document.getElementById('live-input-ta');
  if (!ta) return;
  const text = ta.value.trim();
  if (!text) { showToast('Type a message first'); return; }
  if (typeof _interceptSlashCommand === 'function' && _interceptSlashCommand(text)) {
    ta.value = ''; _resetTextareaHeight(ta); return;
  }
  ta.value = '';  // clear immediately to prevent double-submit on key repeat
  _resetTextareaHeight(ta);
  if (typeof _clearDraft === 'function') _clearDraft(sessionId);

  // NOW seed as running (session will exist on server after this emit)
  runningIds.add(sessionId);
  sessionKinds[sessionId] = 'working';

  // Get skill from current folder (if in workplace mode with folder tree)
  let systemPrompt = null;
  if (workspaceActive && typeof _currentFolderId !== 'undefined' && _currentFolderId) {
    const skill = (typeof getFolderSkill === 'function') ? getFolderSkill(_currentFolderId) : null;
    if (skill && skill.systemPrompt) systemPrompt = skill.systemPrompt;
  }

  // Layer in template system prompt if a template was selected
  if (window._pendingTemplateSystemPrompt) {
    systemPrompt = systemPrompt
      ? systemPrompt + '\n\n' + window._pendingTemplateSystemPrompt
      : window._pendingTemplateSystemPrompt;
    window._pendingTemplateSystemPrompt = null;
  }

  // Ensure agent catalog temp file is written, then inject compact index
  // Skip for compose sessions — their prompt is built entirely server-side
  const _isCompose = viewMode === 'compose' && typeof composeDetailTaskId !== 'undefined' && composeDetailTaskId;
  if (!_isCompose && typeof FOLDER_SUPERSET === 'object' && FOLDER_SUPERSET) {
    await _ensureAgentCatalog();
    const agentBlock = _buildAgentDefinitions();
    if (agentBlock) {
      systemPrompt = systemPrompt
        ? systemPrompt + '\n\n' + agentBlock
        : agentBlock;
    }
  }

  const startOpts = {
    session_id: sessionId,
    prompt: text,
    cwd: _currentProjectDir(),
    name: '',
  };
  if (defaultModel) startOpts.model = defaultModel;
  if (defaultThinking) startOpts.thinking_level = defaultThinking;
  if (systemPrompt) startOpts.system_prompt = systemPrompt;

  // Auto-detect compose task context — inject compose_task_id so the
  // backend can resolve and inject the compose system prompt automatically.
  if (viewMode === 'compose' && typeof composeDetailTaskId !== 'undefined' && composeDetailTaskId) {
    startOpts.compose_task_id = composeDetailTaskId;
  }

  socket.emit('start_session', startOpts);

  // Use the user's first message as a placeholder title until auto-name kicks in,
  // BUT respect any name the user already set — don't overwrite it.
  const s = allSessions.find(x => x.id === sessionId);
  if (s && s.custom_title && _userNamedSessions.has(sessionId)) {
    // User already named this session — keep their title
    setToolbarSession(sessionId, s.custom_title, false, s.custom_title);
  } else {
    const _placeholder = text.split('\n')[0].slice(0, 65) + (text.length > 65 ? '\u2026' : '');
    if (s) { s.display_title = _placeholder; }
    setToolbarSession(sessionId, _placeholder, true, '');
  }
  filterSessions();

  // Register session in current folder
  if (workspaceActive && typeof addSessionToFolder === 'function') {
    addSessionToFolder(sessionId, _currentFolderId);
  }

  // Switch to live panel mode — skip log fetch since this is a brand-new session
  // Clear dedup set right before panel creation so stale entries from
  // any prior session during the async await cannot block the bubble.
  _optimisticMsgId = 0;
  startLivePanel(sessionId, {skipLog: true});

  // Add optimistic user bubble into the fresh log (after startLivePanel creates it)
  _liveSending = true;
  _addOptimisticBubble(sessionId, text);
  setTimeout(() => { _liveSending = false; }, 500);

  // Auto-name immediately using the prompt text (no need to wait for JSONL)
  if (!_userNamedSessions.has(sessionId)) autoName(sessionId, true, false, text);
}

// _showNewSessionDialog removed — addNewAgent now goes straight to chat

// --- Keyboard Navigation ---
document.addEventListener('keydown', (e) => {
  // Never block browser shortcuts (refresh, dev tools, etc)
  if (e.ctrlKey || e.metaKey) return;
  // Don't intercept when typing in inputs
  if (e.target.matches('input, textarea, select, [contenteditable]')) return;
  // Don't intercept if a modal is open
  if (document.getElementById('pm-overlay').classList.contains('show')) return;

  if (e.key === 'n' && !e.ctrlKey && !e.metaKey) {
    e.preventDefault();
    addNewAgent();
  } else if (e.key === 'ArrowDown' || e.key === 'j') {
    e.preventDefault();
    _selectAdjacentSession(1);
  } else if (e.key === 'ArrowUp' || e.key === 'k') {
    e.preventDefault();
    _selectAdjacentSession(-1);
  } else if (e.key === 'Enter') {
    if (activeId && !liveSessionId) {
      e.preventDefault();
      startLivePanel(activeId);
    }
  } else if (e.key === 'Escape') {
    if (liveSessionId) {
      e.preventDefault();
      stopLivePanel();
    }
  } else if (e.key === '?' && e.shiftKey) {
    e.preventDefault();
    _showHelpModal();
  }
});

function _selectAdjacentSession(direction) {
  // Try list view items first, then workforce cards
  let items = Array.from(document.querySelectorAll('.session-item[data-sid]'));
  if (!items.length) {
    // Workforce grid: cards have onclick with session IDs but no data-sid;
    // use the sidebar session list if available, otherwise bail
    return;
  }
  const idx = items.findIndex(el => el.dataset.sid === activeId);
  const next = items[Math.max(0, Math.min(items.length - 1, idx + direction))];
  if (next) selectSession(next.dataset.sid);
}

// --- Help Modal ---
function _showHelpModal() {
  const overlay = document.getElementById('pm-overlay');
  overlay.innerHTML = `
    <div class="pm-card pm-enter" style="width:460px;">
      <h2 class="pm-title">Keyboard Shortcuts</h2>
      <div class="pm-body" style="margin-bottom:0;">
        <table class="help-table">
          <tr><td><kbd>N</kbd></td><td>New session</td></tr>
          <tr><td><kbd>\u2191</kbd> / <kbd>K</kbd></td><td>Previous session</td></tr>
          <tr><td><kbd>\u2193</kbd> / <kbd>J</kbd></td><td>Next session</td></tr>
          <tr><td><kbd>Enter</kbd></td><td>Open live panel</td></tr>
          <tr><td><kbd>Esc</kbd></td><td>Close panel / Interrupt</td></tr>
          <tr><td><kbd>Ctrl+F</kbd></td><td>Find in session</td></tr>
          <tr><td><kbd>Ctrl+Enter</kbd></td><td>Send message</td></tr>
          <tr><td><kbd>?</kbd></td><td>This help</td></tr>
        </table>
      </div>
      <div class="pm-actions" style="margin-top:16px;">
        <button class="pm-btn pm-btn-primary" id="pm-ok">Close</button>
      </div>
    </div>`;
  overlay.classList.add('show');
  requestAnimationFrame(() => overlay.querySelector('.pm-card').classList.remove('pm-enter'));
  const close = () => { _closePm(); };
  document.getElementById('pm-ok').onclick = close;
  overlay.onclick = e => { if (e.target === overlay) close(); };
  document.getElementById('pm-ok').focus();
}

// --- CLAUDE.md Memory Editor ---
async function _showMemoryEditor() {
  const overlay = document.getElementById('pm-overlay');
  overlay.innerHTML = `
    <div class="pm-card pm-enter" style="width:580px;max-height:85vh;display:flex;flex-direction:column;">
      <h2 class="pm-title">CLAUDE.md Editor</h2>
      <div style="display:flex;flex-direction:column;flex:1;min-height:0;gap:12px;">
        <div class="memory-tabs">
          <button class="mem-tab active" id="mem-tab-project">Project</button>
          <button class="mem-tab" id="mem-tab-global">Global</button>
        </div>
        <div id="mem-project" style="display:flex;flex-direction:column;flex:1;min-height:0;">
          <p class="mem-path" id="mem-project-path" style="font-size:10px;color:var(--text-faint);margin-bottom:6px;">Loading...</p>
          <textarea class="ns-textarea" id="mem-project-content" rows="16" placeholder="Project CLAUDE.md content..." style="flex:1;min-height:200px;"></textarea>
        </div>
        <div id="mem-global" style="display:none;flex-direction:column;flex:1;min-height:0;">
          <p class="mem-path" id="mem-global-path" style="font-size:10px;color:var(--text-faint);margin-bottom:6px;">~/.claude/CLAUDE.md</p>
          <textarea class="ns-textarea" id="mem-global-content" rows="16" placeholder="Global CLAUDE.md content..." style="flex:1;min-height:200px;"></textarea>
        </div>
      </div>
      <div class="pm-actions" style="margin-top:14px;">
        <button class="pm-btn pm-btn-secondary" id="mem-cancel">Cancel</button>
        <button class="pm-btn pm-btn-primary" id="mem-save">Save</button>
      </div>
    </div>`;
  overlay.classList.add('show');
  requestAnimationFrame(() => overlay.querySelector('.pm-card').classList.remove('pm-enter'));

  // Tab switching
  const tabProject = document.getElementById('mem-tab-project');
  const tabGlobal = document.getElementById('mem-tab-global');
  const panelProject = document.getElementById('mem-project');
  const panelGlobal = document.getElementById('mem-global');
  tabProject.onclick = () => {
    tabProject.classList.add('active'); tabGlobal.classList.remove('active');
    panelProject.style.display = 'flex'; panelGlobal.style.display = 'none';
  };
  tabGlobal.onclick = () => {
    tabGlobal.classList.add('active'); tabProject.classList.remove('active');
    panelGlobal.style.display = 'flex'; panelProject.style.display = 'none';
  };

  // Load content
  try {
    const [projRes, globalRes] = await Promise.all([
      fetch('/api/claude-md').then(r => r.json()).catch(() => ({content:'', path:'Not found'})),
      fetch('/api/claude-md-global').then(r => r.json()).catch(() => ({content:'', path:'~/.claude/CLAUDE.md'})),
    ]);
    document.getElementById('mem-project-path').textContent = projRes.path || 'Not found';
    document.getElementById('mem-project-content').value = projRes.content || '';
    document.getElementById('mem-global-path').textContent = globalRes.path || '~/.claude/CLAUDE.md';
    document.getElementById('mem-global-content').value = globalRes.content || '';
  } catch(e) {
    showToast('Failed to load CLAUDE.md files', true);
  }

  const close = () => { _closePm(); };
  document.getElementById('mem-cancel').onclick = close;
  overlay.onclick = e => { if (e.target === overlay) close(); };

  document.getElementById('mem-save').onclick = async () => {
    try {
      const results = await Promise.all([
        fetch('/api/claude-md', {
          method: 'PUT', headers: {'Content-Type':'application/json'},
          body: JSON.stringify({content: document.getElementById('mem-project-content').value})
        }).then(r => r.json()).catch(() => ({ok:false})),
        fetch('/api/claude-md-global', {
          method: 'PUT', headers: {'Content-Type':'application/json'},
          body: JSON.stringify({content: document.getElementById('mem-global-content').value})
        }).then(r => r.json()).catch(() => ({ok:false})),
      ]);
      close();
      showToast('CLAUDE.md files saved');
    } catch(e) {
      showToast('Failed to save', true);
    }
  };
}

// --- Skeleton & Sessions ---
function showSkeletonLoader() {
  const el = document.getElementById('session-list');
  let html = '';
  for (let i = 0; i < 20; i++) {
    const nw = 40 + Math.random() * 45;
    const delay = (i * 0.06).toFixed(2);
    html += '<div class="skel-row">'
      + '<div><div class="skel-bar name" style="width:' + nw + '%;animation-delay:' + delay + 's"></div></div>'
      + '<div><div class="skel-bar date" style="animation-delay:' + delay + 's"></div></div>'
      + '<div><div class="skel-bar size" style="animation-delay:' + delay + 's"></div></div>'
      + '</div>';
  }
  el.innerHTML = html;
}

let _loadSessionsAbort = null;

async function loadSessions() {
  showSkeletonLoader();
  // Abort any in-flight loadSessions fetch to prevent stale project data
  if (_loadSessionsAbort) _loadSessionsAbort.abort();
  _loadSessionsAbort = new AbortController();
  const _myAbort = _loadSessionsAbort;
  const _myGen = _projectSwitchGen;  // capture generation at call time

  // Load workforce assets from disk first (replaces FOLDER_SUPERSET before folder tree reads it)
  // Then load sessions and folder tree in parallel
  if (typeof _loadWorkforceFromDisk === 'function') {
    try { await _loadWorkforceFromDisk(); } catch(e) {}
  }
  // If project switched during workforce load, bail
  if (_myGen !== _projectSwitchGen) { console.warn('[loadSessions] project switched during workforce load — discarding'); return; }
  // Pass activeProject so the server syncs _active_project before processing.
  // Closes the race where this HTTP fetch arrives before the WebSocket
  // reconnects and sends request_state_snapshot with the project context.
  const _projParam = localStorage.getItem('activeProject') || '';
  const _sessUrl = _projParam ? '/api/sessions?project=' + encodeURIComponent(_projParam) : '/api/sessions';
  const [resp] = await Promise.all([
    fetch(_sessUrl, {signal: _myAbort.signal}),
    (typeof initFolderTree === 'function') ? initFolderTree().catch(function(){}) : Promise.resolve(),
  ]);
  // Guard: if project changed while we were fetching, discard stale response
  if (_myGen !== _projectSwitchGen) { console.warn('[loadSessions] project switched during fetch — discarding'); return; }
  const _nowProj = localStorage.getItem('activeProject') || '';
  if (_nowProj !== _projParam) { console.warn('[loadSessions] stale response for', _projParam, '— discarding'); return; }
  const _freshSessions = await resp.json();
  // Preserve optimistic entries for sessions the user is actively working in
  // (e.g. just-started session whose JSONL hasn't been created yet).
  // Without this, a concurrent loadSessions() from setProject() wipes the
  // optimistic entry and destroys the live panel.
  const _freshIds = new Set(_freshSessions.map(s => s.id));
  const _preserved = allSessions.filter(s =>
      !_freshIds.has(s.id) && s.id === liveSessionId
  );
  allSessions = _preserved.concat(_freshSessions);
  // Deduplicate by ID — race between remap events and session list fetch
  // can produce two entries with the same ID
  {
    const _seen = new Set();
    allSessions = allSessions.filter(s => {
      if (_seen.has(s.id)) return false;
      _seen.add(s.id);
      return true;
    });
  }
  // Filter out hidden utility sessions (planner, auto-title, etc.)
  // Convention: any session ID starting with "_" is a system/utility session.
  allSessions = allSessions.filter(s =>
    !s.id.startsWith('_') &&
    !(s.session_type && (s.session_type === 'planner' || s.session_type === 'title'))
  );
  // Purge stale alias entries (old pre-remap IDs still on disk or in daemon)
  if (window._idRemaps) {
    allSessions = allSessions.filter(s => !window._idRemaps[s.id]);
  }
  _rebuildSessionIds();
  // Populate _userNamedSessions from server so manual names survive page refresh
  if (typeof _userNamedSessions !== 'undefined') {
    for (const s of allSessions) {
      if (s.user_named) _userNamedSessions.add(s.id);
    }
  }
  document.getElementById('search').placeholder = 'Search ' + allSessions.length + ' sessions\u2026';
  setViewMode(viewMode);
  // Template selector is handled by initFolderTree() — no duplicate call here

  // Re-apply session state CSS classes to the freshly rendered DOM rows.
  // setViewMode() just rebuilt the sidebar/grid from allSessions, but the
  // state_snapshot may have already populated sessionKinds before this
  // render.  Without this, sessions show as "sleeping" until the next
  // individual session_state event arrives.
  if (typeof _updateRowState === 'function') {
    for (const id in sessionKinds) {
      const kind = sessionKinds[id];
      const state = kind === 'question' ? 'waiting' : kind;
      _updateRowState(id, state);
    }
  }

  // Homepage: no session restoration needed, homepage is fully rendered by setViewMode
  if (viewMode === 'homepage') return;

  // Check URL ?chat= param first, then fall back to localStorage
  const _urlChatId = new URL(window.location).searchParams.get('chat');

  // In workplace mode, the workspace canvas is already rendered by setViewMode->filterSessions.
  if (viewMode === 'workplace') {
    if (_urlChatId && allSessions.find(s => s.id === _urlChatId)) {
      _skipChatHistory = true;
      expandWorkspaceCard(_urlChatId);
      _skipChatHistory = false;
    } else {
      activeId = null;
      localStorage.removeItem('activeSessionId');
    }
    return;
  }
  // If the user already has an active live panel (e.g. they started a new
  // session while this async loadSessions was still in flight), don't clobber
  // it with session restoration or the dashboard.
  if (liveSessionId && document.getElementById('live-panel')) {
    filterSessions();
    return;
  }

  // Restore session from URL, then localStorage, then per-project memory, or show dashboard.
  // Skip session restore entirely when the URL hash points to a kanban or compose view —
  // restoreFromHash handles navigation there; restoring a stale activeSessionId
  // would hijack the view and open a session from a different task/subtree.
  // Also skip during project switches — the user wants to stay in the current
  // view at its base state (no session selected).
  const _hashIsKanban = window.location.hash.startsWith('#kanban');
  const _hashIsCompose = window.location.hash.startsWith('#compose');
  const _skipRestore = _suppressSessionRestore;
  _suppressSessionRestore = false;
  const _activeProj = localStorage.getItem('activeProject');
  let _restoreId = (_hashIsKanban || _hashIsCompose || _skipRestore) ? null : (
    _urlChatId || localStorage.getItem('activeSessionId')
    || (_activeProj && localStorage.getItem('projectSession_' + _activeProj))
  );

  // If the stored ID isn't in allSessions, it may have been remapped by the SDK.
  // Ask the server to resolve the alias before giving up.
  if (_restoreId && !allSessions.find(s => s.id === _restoreId)) {
    try {
      const _rsp = localStorage.getItem('activeProject') || '';
      const _rspQ = _rsp ? '?project=' + encodeURIComponent(_rsp) : '';
      const resolveResp = await fetch('/api/resolve-session/' + _restoreId + _rspQ);
      if (resolveResp.ok) {
        const resolved = await resolveResp.json();
        if (resolved.remapped && allSessions.find(s => s.id === resolved.id)) {
          _restoreId = resolved.id;
          localStorage.setItem('activeSessionId', _restoreId);
          const _fixUrl = new URL(window.location);
          _fixUrl.searchParams.set('chat', _restoreId);
          history.replaceState(null, '', _fixUrl);
        }
      }
    } catch(e) { /* resolve failed — fall through to dashboard */ }
  }

  if (_restoreId && allSessions.find(s => s.id === _restoreId)) {
    _skipChatHistory = true;
    openInGUI(_restoreId);
    _skipChatHistory = false;
    // Ensure URL reflects the restored session (replaceState, not push,
    // so we don't create a stale back-button entry).
    const _restoreUrl = new URL(window.location);
    if (!_restoreUrl.searchParams.has('chat') || _restoreUrl.searchParams.get('chat') !== _restoreId) {
      _restoreUrl.searchParams.set('chat', _restoreId);
      history.replaceState({ folder: null, chat: _restoreId }, '', _restoreUrl);
    }
  } else {
    document.getElementById('main-body').innerHTML = _buildDashboard();
  }
}

function filterSessions() {
  const q = document.getElementById('search').value.toLowerCase();
  const filtered = q
    ? allSessions.filter(s =>
        (s.display_title||'').toLowerCase().includes(q) ||
        (s.preview||'').toLowerCase().includes(q)
      )
    : allSessions;
  if (viewMode === 'homepage') {
    if (typeof _updateHomepageStats === 'function') _updateHomepageStats();
    return;
  }
  if (viewMode === 'workplace') {
    renderWorkspace(wfSortedSessions(filtered));
  } else if (viewMode === 'sessions' && sessionDisplayMode === 'grid') {
    renderWorkforce(wfSortedSessions(filtered));
  } else if (viewMode === 'sessions') {
    renderList(sortedSessions(filtered));
  }
}

// ===== Model / Thinking / Template / Department selectors =====

// --- Model Selector ---
let defaultModel = localStorage.getItem('defaultModel') || '';

async function openModelSelector() {
  const overlay = document.getElementById('pm-overlay');

  // Show loading state
  overlay.innerHTML = '<div class="pm-card pm-enter" style="width:380px;"><h2 class="pm-title">Select Model</h2><div class="pm-body"><span class="spinner"></span> Loading models...</div></div>';
  overlay.classList.add('show');
  requestAnimationFrame(() => overlay.querySelector('.pm-card').classList.remove('pm-enter'));
  overlay.onclick = e => { if (e.target === overlay) _closePm(); };

  // Fetch models dynamically from server
  let models;
  try {
    const resp = await fetch('/api/models');
    models = await resp.json();
  } catch (e) {
    models = [
      {id: '', name: 'Default', desc: 'Uses your Claude Code settings', default: true},
      {id: 'sonnet', name: 'Sonnet', desc: 'Fast, capable, balanced'},
      {id: 'opus', name: 'Opus', desc: 'Most capable, deeper reasoning'},
      {id: 'haiku', name: 'Haiku', desc: 'Fastest, most cost-efficient'},
    ];
  }

  let cardsHtml = '';
  for (const m of models) {
    const key = m.id || '';
    const isActive = key === defaultModel || (m.default && !defaultModel);
    const name = m.name || key;
    const desc = m.desc || '';
    const extra = m.context_window ? ' (' + Math.round(m.context_window/1000) + 'K context)' : '';
    const current = m.current ? ' <span style="font-size:9px;background:var(--accent);color:#fff;padding:2px 6px;border-radius:8px;font-weight:700;">Current</span>' : '';
    cardsHtml += '<div class="add-mode-card' + (isActive ? ' active' : '') + '" data-model="' + escHtml(key) + '">'
      + '<div class="add-mode-info">'
      + '<div class="add-mode-title">' + escHtml(name) + extra + current + '</div>'
      + '<div class="add-mode-desc">' + escHtml(desc) + '</div>'
      + '</div></div>';
  }

  overlay.innerHTML = '<div class="pm-card" style="width:380px;">'
    + '<h2 class="pm-title">Select Model</h2>'
    + '<div class="pm-body"><p>Choose the default model for new sessions.</p></div>'
    + '<div style="display:flex;flex-direction:column;gap:8px;margin-bottom:20px;">' + cardsHtml + '</div>'
    + '<div class="pm-actions"><button class="pm-btn pm-btn-secondary" id="pm-model-close">Close</button></div></div>';

  document.getElementById('pm-model-close').onclick = () => _closePm();
  overlay.onclick = e => { if (e.target === overlay) _closePm(); };
  overlay.querySelectorAll('.add-mode-card').forEach(card => {
    card.onclick = () => {
      defaultModel = card.dataset.model;
      localStorage.setItem('defaultModel', defaultModel);
      _closePm();
      _updateModelLabel();
      showToast('Model: ' + (card.querySelector('.add-mode-title').textContent || 'Default'));
    };
  });
}

function _updateModelLabel() {
  const el = document.getElementById('sys-model-label');
  if (!el) return;
  if (!defaultModel) { el.textContent = 'Default'; return; }
  if (defaultModel.includes('sonnet')) el.textContent = 'Sonnet 4';
  else if (defaultModel.includes('opus')) el.textContent = 'Opus 4';
  else if (defaultModel.includes('haiku')) el.textContent = 'Haiku 4.5';
  else el.textContent = defaultModel.split('-').pop();
}
_updateModelLabel();

// --- Thinking Level Selector ---
let defaultThinking = localStorage.getItem('defaultThinking') || '';

function openThinkingSelector() {
  const overlay = document.getElementById('pm-overlay');
  const levels = [
    {key: '', name: 'Default', desc: 'Use model default'},
    {key: 'none', name: 'None', desc: 'No extended thinking'},
    {key: 'low', name: 'Low', desc: 'Brief reasoning step'},
    {key: 'medium', name: 'Medium', desc: 'Moderate reasoning'},
    {key: 'high', name: 'High', desc: 'Deep reasoning for hard tasks'},
  ];
  let html = '<div class="pm-card pm-enter" style="width:380px;">'
    + '<h2 class="pm-title">Thinking Level</h2>'
    + '<div class="pm-body"><p>Set the extended thinking level for new sessions.</p></div>'
    + '<div style="display:flex;flex-direction:column;gap:8px;margin-bottom:20px;">';
  for (const l of levels) {
    const isActive = l.key === defaultThinking;
    html += `<div class="add-mode-card${isActive ? ' active' : ''}" data-level="${l.key}">
      <div class="add-mode-info">
        <div class="add-mode-title">${l.name}</div>
        <div class="add-mode-desc">${l.desc}</div>
      </div>
    </div>`;
  }
  html += '</div><div class="pm-actions"><button class="pm-btn pm-btn-secondary" id="pm-think-close">Close</button></div></div>';
  overlay.innerHTML = html;
  overlay.classList.add('show');
  requestAnimationFrame(() => overlay.querySelector('.pm-card').classList.remove('pm-enter'));
  document.getElementById('pm-think-close').onclick = () => _closePm();
  overlay.onclick = e => { if (e.target === overlay) _closePm(); };
  overlay.querySelectorAll('.add-mode-card').forEach(card => {
    card.onclick = () => {
      defaultThinking = card.dataset.level;
      localStorage.setItem('defaultThinking', defaultThinking);
      _closePm();
      _updateThinkingLabel();
      showToast('Thinking: ' + card.querySelector('.add-mode-title').textContent);
    };
  });
}

function _updateThinkingLabel() {
  const el = document.getElementById('sys-thinking-label');
  if (!el) return;
  el.textContent = defaultThinking ? defaultThinking.charAt(0).toUpperCase() + defaultThinking.slice(1) : 'Default';
}
_updateThinkingLabel();

// --- Preferences Modal ---
function openPreferences() {
  const overlay = document.getElementById('pm-overlay');
  const options = [
    {key: 'ctrl-enter', name: 'Ctrl+Enter, Shift+Enter, or Alt+Enter to send', desc: 'Any modifier + Enter sends. Enter alone adds a new line.'},
    {key: 'enter', name: 'Enter to send', desc: 'Press Enter to send. Ctrl+Enter, Shift+Enter, and Alt+Enter add a new line.'},
  ];
  let html = '<div class="pm-card pm-enter" style="width:420px;">'
    + '<h2 class="pm-title">Preferences</h2>'
    + '<div class="pm-body"><p style="margin-bottom:4px;font-weight:600;font-size:13px;">Send Behavior</p>'
    + '<p style="font-size:12px;color:var(--text-muted);">Choose how messages are sent from input fields.</p></div>'
    + '<div style="display:flex;flex-direction:column;gap:8px;margin-bottom:20px;">';
  for (const o of options) {
    const isActive = o.key === sendBehavior;
    html += `<div class="add-mode-card${isActive ? ' active' : ''}" data-pref="${o.key}">
      <div class="add-mode-info">
        <div class="add-mode-title">${o.name}</div>
        <div class="add-mode-desc">${o.desc}</div>
      </div>
    </div>`;
  }
  html += '</div>';

  // --- Sticky Chats toggle ---
  const stickyOn = localStorage.getItem('stickyUserMsgs') !== 'off';
  html += '<div class="pm-body" style="margin-top:8px;"><p style="margin-bottom:4px;font-weight:600;font-size:13px;">Sticky Chats</p>'
    + '<p style="font-size:12px;color:var(--text-muted);">When scrolling through long responses, your most recent message stays pinned at the top so you can see what you sent.</p></div>'
    + '<div style="display:flex;flex-direction:column;gap:8px;margin-bottom:20px;">';
  const stickyOpts = [
    {key: 'on',  name: 'Enabled',  desc: 'Pin your most recent chat at the top while scrolling.'},
    {key: 'off', name: 'Disabled', desc: 'Normal scroll behavior \u2014 no pinned messages.'},
  ];
  for (const o of stickyOpts) {
    const isActive = (o.key === 'on') === stickyOn;
    html += `<div class="add-mode-card${isActive ? ' active' : ''}" data-sticky="${o.key}">
      <div class="add-mode-info">
        <div class="add-mode-title">${o.name}</div>
        <div class="add-mode-desc">${o.desc}</div>
      </div>
    </div>`;
  }
  html += '</div>';

  html += '<div class="pm-actions"><button class="pm-btn pm-btn-secondary" id="pm-pref-close">Close</button></div></div>';
  overlay.innerHTML = html;
  overlay.classList.add('show');
  requestAnimationFrame(() => overlay.querySelector('.pm-card').classList.remove('pm-enter'));
  document.getElementById('pm-pref-close').onclick = () => _closePm();
  overlay.onclick = e => { if (e.target === overlay) _closePm(); };
  overlay.querySelectorAll('.add-mode-card[data-pref]').forEach(card => {
    card.onclick = () => {
      sendBehavior = card.dataset.pref;
      localStorage.setItem('sendBehavior', sendBehavior);
      _closePm();
      showToast('Send: ' + card.querySelector('.add-mode-title').textContent);
      _refreshSendHints();
    };
  });
  overlay.querySelectorAll('.add-mode-card[data-sticky]').forEach(card => {
    card.onclick = () => {
      const val = card.dataset.sticky;
      if (val === 'off') {
        localStorage.setItem('stickyUserMsgs', 'off');
        // Remove any active pin immediately
        document.querySelectorAll('.msg.user.sticky-pinned').forEach(m => m.classList.remove('sticky-pinned'));
      } else {
        localStorage.removeItem('stickyUserMsgs');
        // Re-init on current conversation
        const c = document.getElementById('live-log') || document.getElementById('convo');
        if (c && typeof initStickyUserMessages === 'function') initStickyUserMessages(c);
      }
      _closePm();
      showToast('Sticky chats: ' + card.querySelector('.add-mode-title').textContent);
    };
  });
}

function openWorkspaceTemplateSelector() {
  if (typeof showTemplateSelector !== 'function') {
    showToast('Folder system not loaded');
    return;
  }
  showTemplateSelector(() => {
    filterSessions();
    showToast('Workspace template applied');
  });
}

function openAddDepartment() {
  if (typeof FOLDER_SUPERSET === 'undefined' || typeof getFolderTree !== 'function') {
    showToast('Folder system not loaded');
    return;
  }
  const tree = getFolderTree();
  if (!tree) {
    showToast('Set up a workspace template first');
    return;
  }

  // Get available departments (root-level folders not already in tree)
  const existing = new Set(Object.keys(tree.folders));
  const available = Object.entries(FOLDER_SUPERSET)
    .filter(([id, def]) => !def.parentId && !existing.has(id))
    .map(([id, def]) => ({id, name: def.name, childCount: def.children.length}));

  if (!available.length) {
    showToast('All departments already added');
    return;
  }

  const overlay = document.getElementById('pm-overlay');
  let html = '<div class="pm-card pm-enter" style="width:420px;max-height:80vh;display:flex;flex-direction:column;">'
    + '<h2 class="pm-title">Add Department</h2>'
    + '<div class="pm-body" style="overflow-y:auto;flex:1;min-height:0;">'
    + '<p>Select a department to add to your workspace.</p>'
    + '<div style="display:flex;flex-direction:column;gap:6px;margin-top:12px;">';

  for (const dept of available) {
    html += `<div class="add-mode-card" data-dept="${dept.id}" style="padding:12px;">
      <div class="add-mode-info">
        <div class="add-mode-title">${escHtml(dept.name)}</div>
        <div class="add-mode-desc">${dept.childCount} sub-folders</div>
      </div>
    </div>`;
  }

  html += '</div></div>'
    + '<div class="pm-actions"><button class="pm-btn pm-btn-secondary" id="pm-dept-close">Close</button></div></div>';
  overlay.innerHTML = html;
  overlay.classList.add('show');
  requestAnimationFrame(() => overlay.querySelector('.pm-card').classList.remove('pm-enter'));
  document.getElementById('pm-dept-close').onclick = () => _closePm();
  overlay.onclick = e => { if (e.target === overlay) _closePm(); };

  overlay.querySelectorAll('.add-mode-card').forEach(card => {
    card.onclick = () => {
      const deptId = card.dataset.dept;
      addDepartmentFromSuperset(deptId);
      _closePm();
      filterSessions();
      showToast('Added ' + card.querySelector('.add-mode-title').textContent);
    };
  });
}


// ═══════════════════════════════════════════════════════════════
// COMPOSE ROOT ORCHESTRATOR — Header, Input Target, State
// ═══════════════════════════════════════════════════════════════

// Current compose state
let _composeProject = null;       // active ComposeProject object
let _composeSections = [];        // section list
let _composeConflicts = [];       // pending conflicts
let composeDetailTaskId = null;   // compose_task_id for session start
let _composeSelectedSection = null; // currently selected section id (null = root)
let _activeComposeProjectId = null; // selected composition ID (null = auto-select most recent)
let _composeProjectsList = [];     // all compositions for the active project
let _composeInitToken = 0;         // concurrency guard for initCompose()
let _composeSelected = new Set();  // multi-select: set of composition IDs
let _composeLastClickedId = null;  // for shift-click range selection
let _composeSearchFilter = '';     // sidebar search filter text
let _composePendingDeletes = [];   // [{ids: [...], timer: timeoutId, toastEl: el}]
let _composeFocusedId = null;      // keyboard-focused composition ID
let _composeActionHistory = [];    // [{label: '...', time: Date.now()}] — last 5 actions

/**
 * Initialize the compose board — fetch project data and render header.
 * Called by setViewMode('compose') in workforce.js.
 */
async function initCompose() {
  const _initToken = ++_composeInitToken;
  try {
    const _proj = localStorage.getItem('activeProject') || '';

    // Restore composition selection from localStorage if not already set
    if (!_activeComposeProjectId && _proj) {
      _activeComposeProjectId = localStorage.getItem('activeComposition:' + _proj) || null;
    }

    // Single fetch — board endpoint returns sibling_projects for the sidebar
    // Always pass &project= so the sidebar stays rooted in the active VibeNode
    // project even when viewing a cross-project pinned composition.
    let query = '';
    if (_activeComposeProjectId) {
      query = '?project_id=' + encodeURIComponent(_activeComposeProjectId);
      if (_proj) query += '&project=' + encodeURIComponent(_proj);
    } else if (_proj) {
      query = '?project=' + encodeURIComponent(_proj);
    }
    const resp = await fetch('/api/compose/board' + query);
    if (_initToken !== _composeInitToken) return;
    const data = await resp.json();

    // Populate sidebar list from sibling_projects (included in board response)
    _composeProjectsList = (data && data.sibling_projects) ? data.sibling_projects : [];

    if (!data || !data.project) {
      _activeComposeProjectId = null;
      _composeSelectedSection = null;
      _composeProject = null;
      _renderComposeEmpty();
      _renderComposeSidebar();
      attachComposeShortcuts();
      return;
    }

    _composeProject = data.project;
    _activeComposeProjectId = data.project.id;
    _composeSections = data.sections || [];
    _composeConflicts = (data.conflicts || []).filter(c => c.status === 'pending');

    // Persist selection
    if (_proj) {
      localStorage.setItem('activeComposition:' + _proj, _activeComposeProjectId);
    }

    // Check if we should restore a section drill-down from URL hash
    if (_restoreComposeSectionFromHash()) {
      // drill-down restored — header stays hidden, keep section's composeDetailTaskId
    } else {
      // Show header and input target for board view
      const header = document.getElementById('compose-root-header');
      const target = document.getElementById('compose-input-target');
      if (header) header.style.display = 'flex';
      if (target) target.style.display = 'flex';
      _updateComposeRootHeader();
      _updateComposeInputTarget();
      _renderComposeSectionCards();
      // Set default compose_task_id to root (only when showing board, not drill-down)
      composeDetailTaskId = 'root:' + _composeProject.id;
    }

  } catch (e) {
    console.error('Failed to init compose:', e);
    _renderComposeEmpty();
  }

  _renderComposeSidebar();
  attachComposeShortcuts();
}

function _renderComposeEmpty() {
  const nameEl = document.getElementById('compose-root-name');
  if (nameEl) nameEl.textContent = 'No composition yet';
  const statusEl = document.getElementById('compose-root-status');
  if (statusEl) statusEl.textContent = '';

  // Hide header bar when no project exists
  const header = document.getElementById('compose-root-header');
  if (header) header.style.display = 'none';
  const target = document.getElementById('compose-input-target');
  if (target) target.style.display = 'none';

  const board = document.getElementById('compose-sections-board');
  if (board) {
    board.innerHTML = `
      <div class="compose-empty-board">
        <svg width="36" height="36" viewBox="0 0 24 24" fill="none" stroke="var(--text-muted)" stroke-width="1.5" stroke-linecap="round">
          <path d="M12 20h9"/><path d="M16.5 3.5a2.121 2.121 0 0 1 3 3L7 19l-4 1 1-4L16.5 3.5z"/>
        </svg>
        <div style="font-size:16px;font-weight:500;color:var(--text);margin:12px 0 6px;">Welcome to Compose</div>
        <div style="font-size:13px;color:var(--text-muted);margin-bottom:16px;">Orchestrate multiple sections with AI-powered composition.</div>
        <button class="kanban-create-first-btn" onclick="composeCreateProject()">+ Create your first composition</button>
      </div>`;
  }

}

function composeCreateProject() {
  const overlay = document.getElementById('pm-overlay');
  if (!overlay) return;

  overlay.innerHTML = `<div class="pm-card pm-enter" style="max-width:480px;">
    <h2 class="pm-title">New Composition</h2>
    <div class="pm-body" style="padding:0;">
      <div class="kanban-create-section">
        <div class="kanban-create-section-label">Project name</div>
        <div class="kanban-create-quick-row">
          <input type="text" id="compose-new-project-input" class="kanban-create-input" placeholder="e.g. Blog Series, Product Launch\u2026"
            onkeydown="if(event.key==='Enter'){event.preventDefault();_submitComposeProject();}">
          <button class="kanban-create-submit" onclick="_submitComposeProject()">
            <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><line x1="12" y1="5" x2="12" y2="19"/><line x1="5" y1="12" x2="19" y2="12"/></svg>
          </button>
        </div>
      </div>
    </div>
  </div>`;
  overlay.classList.add('show');
  requestAnimationFrame(() => {
    overlay.querySelector('.pm-card')?.classList.remove('pm-enter');
    document.getElementById('compose-new-project-input')?.focus();
  });
  overlay.onclick = (e) => { if (e.target === overlay) _closePm(); };
}

async function _submitComposeProject() {
  const input = document.getElementById('compose-new-project-input');
  const name = input ? input.value.trim() : '';
  if (!name) { if (input) input.focus(); return; }
  _closePm();
  try {
    const resp = await fetch('/api/compose/projects', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({name, parent_project: localStorage.getItem('activeProject') || ''}),
    });
    if (!resp.ok) throw new Error('Server error (' + resp.status + ')');
    const data = await resp.json();
    if (data && data.ok) {
      showToast('Created composition: ' + name);
      // Auto-switch to the newly created composition
      if (data.project && data.project.id) {
        _activeComposeProjectId = data.project.id;
      }
      initCompose();
    } else {
      showToast(data.error || 'Failed to create composition', 'error');
    }
  } catch (e) {
    console.error('Failed to create compose project:', e);
    showToast('Failed to create composition', 'error');
  }
}

let _composeInsertPosition = 'top';
let _composeAddParentId = null;

function composeAddSection(parentId) {
  if (!_composeProject) return;
  const overlay = document.getElementById('pm-overlay');
  if (!overlay) return;
  _composeInsertPosition = 'top';
  _composeArtifactType = 'text';
  _composeAddParentId = parentId || null;

  const _modalTitle = parentId ? 'Add Subsection' : 'Add Section';
  overlay.innerHTML = `<div class="pm-card pm-enter" style="max-width:480px;">
    <h2 class="pm-title" style="display:flex;align-items:center;justify-content:space-between;">
      <span>${_modalTitle}</span>
      <div class="kanban-create-position-row" style="margin:0;">
        <span style="font-size:11px;color:var(--text-dim);">Insert</span>
        <button class="kanban-create-pos-btn active" id="cs-pos-top" onclick="_setComposeInsertPos('top')">Top</button>
        <button class="kanban-create-pos-btn" id="cs-pos-bottom" onclick="_setComposeInsertPos('bottom')">Bottom</button>
      </div>
    </h2>
    <div class="pm-body" style="padding:0;">

      <div class="kanban-create-section">
        <div class="kanban-create-section-label">Quick add</div>
        <div class="kanban-create-quick-row">
          <input type="text" id="compose-new-section-input" class="kanban-create-input" placeholder="Section name\u2026"
            onkeydown="if(event.key==='Enter'){event.preventDefault();_submitComposeSection();}">
          <button class="kanban-create-submit" onclick="_submitComposeSection()">
            <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><line x1="12" y1="5" x2="12" y2="19"/><line x1="5" y1="12" x2="19" y2="12"/></svg>
          </button>
        </div>
      </div>

      <div class="kanban-create-section">
        <div class="kanban-create-section-label">Type</div>
        <div style="display:flex;gap:6px;flex-wrap:wrap;">
          <button class="kanban-create-pos-btn active" data-atype="text" onclick="_setComposeArtifactType(this,'text')">Text</button>
          <button class="kanban-create-pos-btn" data-atype="code" onclick="_setComposeArtifactType(this,'code')">Code</button>
          <button class="kanban-create-pos-btn" data-atype="data" onclick="_setComposeArtifactType(this,'data')">Data</button>
        </div>
      </div>

    </div>
  </div>`;
  overlay.classList.add('show');
  requestAnimationFrame(() => {
    overlay.querySelector('.pm-card')?.classList.remove('pm-enter');
    document.getElementById('compose-new-section-input')?.focus();
  });
  overlay.onclick = (e) => { if (e.target === overlay) _closePm(); };
}

let _composeArtifactType = 'text';

function _setComposeInsertPos(pos) {
  _composeInsertPosition = pos;
  const top = document.getElementById('cs-pos-top');
  const bot = document.getElementById('cs-pos-bottom');
  if (top) top.classList.toggle('active', pos === 'top');
  if (bot) bot.classList.toggle('active', pos === 'bottom');
}

function _setComposeArtifactType(btn, type) {
  _composeArtifactType = type;
  const siblings = btn.parentElement.querySelectorAll('.kanban-create-pos-btn');
  siblings.forEach(b => b.classList.toggle('active', b === btn));
}

async function _submitComposeSection() {
  const input = document.getElementById('compose-new-section-input');
  const name = input ? input.value.trim() : '';
  if (!name) { if (input) input.focus(); return; }
  const insertPos = _composeInsertPosition;
  const artifactType = _composeArtifactType;
  _closePm();

  // Optimistic: insert a ghost card into the drafting column
  const col = document.querySelector('.compose-column[data-status="drafting"] .kanban-column-body');
  let ghostCard = null;
  if (col) {
    ghostCard = document.createElement('div');
    ghostCard.className = 'kanban-card compose-card';
    ghostCard.style.opacity = '0.5';
    ghostCard.innerHTML = '<div class="compose-card-header"><span class="compose-card-title">' + (typeof escHtml === 'function' ? escHtml(name) : name) + '</span></div><div style="font-size:10px;color:var(--text-dim);padding:4px 12px 8px;"><span class="spinner" style="width:10px;height:10px;vertical-align:middle;margin-right:4px;"></span>Creating...</div>';
    if (insertPos === 'top') { col.prepend(ghostCard); } else { col.appendChild(ghostCard); }
    const countEl = col.closest('.compose-column')?.querySelector('.kanban-column-count');
    if (countEl) countEl.textContent = col.querySelectorAll('.kanban-card').length;
  }

  try {
    const resp = await fetch('/api/compose/projects/' + _composeProject.id + '/sections', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({name, artifact_type: artifactType, insert_position: insertPos, parent_id: _composeAddParentId || undefined}),
    });
    if (!resp.ok) throw new Error('Server error (' + resp.status + ')');
    const data = await resp.json();
    if (data && data.ok) {
      showToast('Added section: ' + name);
      initCompose();
    } else {
      if (ghostCard) ghostCard.remove();
      showToast(data.error || 'Failed to add section', 'error');
    }
  } catch (e) {
    console.error('Failed to add compose section:', e);
    if (ghostCard) ghostCard.remove();
    showToast('Failed to add section', 'error');
  }
}

// --- Compose sidebar ---

function _renderComposeSidebar() {
  const sidebar = document.getElementById('compose-sidebar');
  if (!sidebar) return;

  const _penIcon = '<svg width="14" height="14" viewBox="0 0 24 24" fill="currentColor" stroke="none"><path d="M16.5 3.5a2.121 2.121 0 0 1 3 3L7 19l-4 1 1-4L16.5 3.5z"/><rect x="12" y="19" width="9" height="2" rx="1"/></svg>';
  const _plusIcon = '<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><line x1="12" y1="5" x2="12" y2="19"/><line x1="5" y1="12" x2="19" y2="12"/></svg>';
  const _refreshIcon = '<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><polyline points="23 4 23 10 17 10"/><path d="M20.49 15a9 9 0 1 1-2.12-9.36L23 10"/></svg>';

  // ── Compositions list ──
  let html = '<div class="kanban-sidebar-section">';
  html += '<div class="kanban-sidebar-label">Compositions</div>';

  // Search filter
  if (_composeProjectsList.length > 3) {
    html += '<input type="text" id="compose-sidebar-search" class="compose-sidebar-search" placeholder="Filter\u2026" value="' + (typeof escHtml === 'function' ? escHtml(_composeSearchFilter) : _composeSearchFilter) + '" oninput="_composeFilterSidebar(this.value)">';
  }

  // Bulk action bar (shown when items are selected)
  if (_composeSelected.size > 0) {
    html += '<div class="compose-bulk-bar">';
    html += '<span class="compose-bulk-count">' + _composeSelected.size + ' selected</span>';
    html += '<button class="compose-bulk-btn" onclick="_composeBulkPin()" title="Pin selected">Pin</button>';
    html += '<button class="compose-bulk-btn compose-bulk-danger" onclick="_composeBulkDelete()" title="Delete selected">Delete</button>';
    html += '<button class="compose-bulk-btn" onclick="_composeBulkClear()" title="Clear selection">\u2715</button>';
    html += '</div>';
  }

  if (_composeProjectsList.length > 0) {
    html += '<div id="compose-sidebar-list" class="compose-sidebar-list">';
    const _activeProj = localStorage.getItem('activeProject') || '';
    const filterLower = _composeSearchFilter.toLowerCase();
    for (const cp of _composeProjectsList) {
      // Skip pending deletes and search filter
      if (_composeIsPendingDelete(cp.id)) continue;
      if (filterLower && cp.name.toLowerCase().indexOf(filterLower) === -1) continue;

      const isActive = cp.id === _activeComposeProjectId;
      const isPinned = cp.pinned;
      const isSelected = _composeSelected.has(cp.id);
      const isFocused = cp.id === _composeFocusedId;
      const isCrossProject = isPinned && cp.parent_project && cp.parent_project !== _activeProj;
      const cls = 'kanban-sidebar-btn' + (isActive ? ' compose-sidebar-active' : '') + (isPinned ? ' compose-sidebar-pinned' : '');
      const name = typeof escHtml === 'function' ? escHtml(cp.name) : cp.name;
      const pinDot = isPinned ? '<span class="compose-pin-dot" title="Pinned' + (isCrossProject ? ' (from another project)' : '') + '"></span>' : '';
      const checkbox = '<input type="checkbox" class="compose-select-cb" ' + (isSelected ? 'checked' : '') + ' onclick="event.stopPropagation();_composeToggleSelect(event,\'' + cp.id + '\')" title="Select">';
      const canDrag = !isCrossProject && !_composeSearchFilter;
      html += '<div class="compose-sidebar-item' + (isSelected ? ' compose-sidebar-selected' : '') + (isFocused ? ' compose-sidebar-focused' : '') + '" draggable="' + (canDrag ? 'true' : 'false') + '" data-compose-id="' + cp.id + '"' + (isCrossProject ? ' data-cross-project="1"' : '') + '>';
      html += checkbox;
      html += '<button class="' + cls + '" onclick="switchComposition(\'' + cp.id + '\')" oncontextmenu="event.preventDefault();_composeCtxMenu(event,\'' + cp.id + '\')">' + _penIcon + ' ' + name + pinDot + '</button>';
      // Status indicator
      const st = cp.status;
      if (st) {
        let dotCls = 'compose-status-dot';
        let dotTitle = '';
        if (cp.has_conflicts) {
          dotCls += ' compose-status-conflict';
          dotTitle = 'Has conflicts';
        } else if (st.total_sections === 0) {
          dotCls += ' compose-status-empty';
          dotTitle = 'No sections';
        } else if (st.complete === st.total_sections) {
          dotCls += ' compose-status-done';
          dotTitle = 'All complete';
        } else if (st.in_progress > 0) {
          dotCls += ' compose-status-active';
          dotTitle = st.in_progress + ' in progress';
        } else {
          dotCls += ' compose-status-idle';
          dotTitle = (st.total_sections - st.complete - st.in_progress) + ' idle';
        }
        const fraction = st.total_sections > 0 ? st.complete + '/' + st.total_sections : '';
        html += '<span class="compose-status-badge" title="' + dotTitle + '" data-fraction="' + fraction + '" data-compose-id="' + cp.id + '"><span class="' + dotCls + '"></span>' + fraction + '</span>';
      }
      html += '</div>';
    }
    html += '</div>';
  }

  html += '<button class="kanban-sidebar-btn" onclick="composeCreateProject()">' + _plusIcon + ' New Composition</button>';
  html += '</div>';

  // ── Actions ──
  if (_composeProject) {
    html += '<div class="kanban-sidebar-section">';
    html += '<div class="kanban-sidebar-label">Actions</div>';
    html += '<button class="kanban-sidebar-btn" onclick="composeAddSection()">' + _plusIcon + ' New Section</button>';
    html += '<button class="kanban-sidebar-btn" onclick="initCompose()">' + _refreshIcon + ' Refresh</button>';
    html += '</div>';
  }

  // ── Action history ──
  if (_composeActionHistory.length > 0) {
    html += '<div class="kanban-sidebar-section" id="compose-action-history">';
    html += '<div class="kanban-sidebar-label">Recent</div>';
    for (const entry of _composeActionHistory) {
      const ago = _composeTimeAgo(entry.time);
      const label = typeof escHtml === 'function' ? escHtml(entry.label) : entry.label;
      html += '<div class="compose-history-item"><span class="compose-history-label">' + label + '</span><span class="compose-history-ago">' + ago + '</span></div>';
    }
    html += '</div>';
  }

  // Detect fraction changes before replacing DOM
  const _oldBadges = {};
  sidebar.querySelectorAll('.compose-status-badge[data-compose-id]').forEach(el => {
    _oldBadges[el.dataset.composeId] = el.dataset.fraction;
  });

  sidebar.innerHTML = html;

  // Pulse badges whose fractions changed
  sidebar.querySelectorAll('.compose-status-badge[data-compose-id]').forEach(el => {
    const prev = _oldBadges[el.dataset.composeId];
    if (prev !== undefined && prev !== el.dataset.fraction) {
      el.classList.add('compose-status-changed');
      setTimeout(() => el.classList.remove('compose-status-changed'), 600);
    }
  });

  // Attach drag-and-drop to composition list
  _attachComposeDragDrop();

  // Permission aggregator
  const permPanel = document.getElementById('sidebar-perm-panel');
  if (permPanel && typeof _buildPermissionPanel === 'function') {
    permPanel.innerHTML = _buildPermissionPanel();
    permPanel.style.display = '';
  }
}

// --- Drag-and-drop reorder for sidebar compositions ---

function _attachComposeDragDrop() {
  const list = document.getElementById('compose-sidebar-list');
  if (!list) return;

  let _dragItem = null;

  list.addEventListener('dragstart', (e) => {
    // Disable drag-reorder while search filter is active — the filtered DOM
    // only contains a subset of items, so reorder would send an incomplete list.
    if (_composeSearchFilter) { e.preventDefault(); return; }
    _dragItem = e.target.closest('.compose-sidebar-item');
    if (!_dragItem) return;
    _dragItem.classList.add('compose-dragging');
    e.dataTransfer.effectAllowed = 'move';
  });

  list.addEventListener('dragend', () => {
    if (_dragItem) _dragItem.classList.remove('compose-dragging');
    _dragItem = null;
    // Remove any lingering drag-over indicators
    list.querySelectorAll('.compose-drag-over').forEach(el => el.classList.remove('compose-drag-over'));
  });

  list.addEventListener('dragover', (e) => {
    e.preventDefault();
    e.dataTransfer.dropEffect = 'move';
    const target = e.target.closest('.compose-sidebar-item');
    if (!target || target === _dragItem || target.dataset.crossProject) return;
    // Add visual indicator
    list.querySelectorAll('.compose-drag-over').forEach(el => el.classList.remove('compose-drag-over'));
    target.classList.add('compose-drag-over');
  });

  list.addEventListener('dragleave', (e) => {
    const target = e.target.closest('.compose-sidebar-item');
    if (target) target.classList.remove('compose-drag-over');
  });

  list.addEventListener('drop', (e) => {
    e.preventDefault();
    const target = e.target.closest('.compose-sidebar-item');
    if (!target || !_dragItem || target === _dragItem || target.dataset.crossProject) return;
    target.classList.remove('compose-drag-over');

    // Reorder DOM
    const items = [...list.querySelectorAll('.compose-sidebar-item')];
    const fromIdx = items.indexOf(_dragItem);
    const toIdx = items.indexOf(target);
    if (fromIdx < toIdx) {
      target.after(_dragItem);
    } else {
      target.before(_dragItem);
    }

    // Collect new order and send to backend (exclude cross-project pinned items)
    const newOrder = [...list.querySelectorAll('.compose-sidebar-item')]
      .filter(el => !el.dataset.crossProject)
      .map(el => el.dataset.composeId);
    // Update local list to match new order, preserving cross-project pinned items at the end
    const idMap = {};
    _composeProjectsList.forEach(p => { idMap[p.id] = p; });
    const reordered = newOrder.map(id => idMap[id]).filter(Boolean);
    const crossProjectItems = _composeProjectsList.filter(p => {
      const el = list.querySelector('[data-compose-id="' + p.id + '"]');
      return el && el.dataset.crossProject;
    });
    _composeProjectsList = reordered.concat(crossProjectItems);

    fetch('/api/compose/projects/reorder', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({order: newOrder}),
    }).catch(err => console.error('Failed to save composition order:', err));
  });
}

// --- Switch composition ---

function switchComposition(projectId) {
  if (projectId === _activeComposeProjectId) return;
  _activeComposeProjectId = projectId;
  _composeFocusedId = projectId;
  _composeSelectedSection = null;
  // Persist selection
  const _proj = localStorage.getItem('activeProject') || '';
  if (_proj) localStorage.setItem('activeComposition:' + _proj, projectId);
  // Clear drill-down hash if present
  const url = new URL(window.location);
  if (url.hash.startsWith('#compose/')) {
    url.hash = '#compose';
    history.replaceState({ view: 'compose' }, '', url.pathname + url.search + '#compose');
  }
  initCompose();
}

// --- Compose context menu (right-click on composition) ---

function _composeCtxMenuClose() {
  const el = document.getElementById('compose-ctx-menu');
  if (el) {
    if (el._closeHandler) document.removeEventListener('click', el._closeHandler, true);
    el.remove();
  }
}

function _composeCtxMenu(event, projectId) {
  // Remove any existing context menu (and its listener)
  _composeCtxMenuClose();

  const menu = document.createElement('div');
  menu.id = 'compose-ctx-menu';
  menu.className = 'compose-ctx-menu';
  menu.style.left = event.clientX + 'px';
  menu.style.top = event.clientY + 'px';

  const renameIcon = '<svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><path d="M12 20h9"/><path d="M16.5 3.5a2.121 2.121 0 0 1 3 3L7 19l-4 1 1-4L16.5 3.5z"/></svg>';
  const dupeIcon = '<svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><rect x="9" y="9" width="13" height="13" rx="2"/><path d="M5 15H4a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2h9a2 2 0 0 1 2 2v1"/></svg>';
  const pinIcon = '<svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><path d="M12 2v8l4 4H8l4-4z"/><line x1="12" y1="22" x2="12" y2="14"/></svg>';
  const deleteIcon = '<svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><polyline points="3 6 5 6 21 6"/><path d="M19 6v14a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2V6m3 0V4a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2"/></svg>';

  // Check if composition is pinned
  const cp = _composeProjectsList.find(p => p.id === projectId);
  const isPinned = cp && cp.pinned;
  const pinLabel = isPinned ? 'Unpin' : 'Pin';

  menu.innerHTML =
    '<div class="compose-ctx-item" onclick="_composeRename(\'' + projectId + '\')">' + renameIcon + ' Rename</div>' +
    '<div class="compose-ctx-item" onclick="_composeDuplicate(\'' + projectId + '\')">' + dupeIcon + ' Duplicate</div>' +
    '<div class="compose-ctx-item" onclick="_composeTogglePin(\'' + projectId + '\')">' + pinIcon + ' ' + pinLabel + '</div>' +
    '<div class="compose-ctx-item compose-ctx-danger" onclick="_composeDelete(\'' + projectId + '\')">' + deleteIcon + ' Delete</div>';

  document.body.appendChild(menu);

  // Clamp position so menu doesn't overflow viewport
  requestAnimationFrame(() => {
    const rect = menu.getBoundingClientRect();
    if (rect.right > window.innerWidth) menu.style.left = Math.max(0, window.innerWidth - rect.width - 4) + 'px';
    if (rect.bottom > window.innerHeight) menu.style.top = Math.max(0, window.innerHeight - rect.height - 4) + 'px';
  });

  // Close on click anywhere else
  const _close = (e) => {
    if (!menu.contains(e.target)) {
      menu.remove();
      document.removeEventListener('click', _close, true);
    }
  };
  menu._closeHandler = _close;
  setTimeout(() => document.addEventListener('click', _close, true), 0);
}

async function _composeRename(projectId) {
  _composeCtxMenuClose();

  const cp = _composeProjectsList.find(p => p.id === projectId);
  const oldName = cp ? cp.name : '';
  const newName = prompt('Rename composition:', oldName);
  if (!newName || !newName.trim() || newName.trim() === oldName) return;

  try {
    const resp = await fetch('/api/compose/projects/' + projectId, {
      method: 'PUT',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({name: newName.trim()}),
    });
    if (!resp.ok) throw new Error('Rename failed');
    const data = await resp.json();
    if (data && data.ok) {
      showToast('Renamed to "' + newName.trim() + '"');
      _composeLogAction('Renamed to "' + newName.trim() + '"');
      initCompose();
    } else {
      showToast(data.error || 'Rename failed', 'error');
    }
  } catch (e) {
    console.error('Failed to rename composition:', e);
    showToast('Failed to rename composition', 'error');
  }
}

function _composeIsPendingDelete(id) {
  return _composePendingDeletes.some(pd => pd.ids.includes(id));
}

function _composeFlushPendingDeletes() {
  for (const pd of _composePendingDeletes) {
    clearTimeout(pd.timer);
    if (pd.toastEl && pd.toastEl.parentNode) pd.toastEl.remove();
    _composeExecuteDeletes(pd.ids);
  }
  _composePendingDeletes = [];
}

async function _composeExecuteDeletes(ids) {
  for (const pid of ids) {
    try {
      await fetch('/api/compose/projects/' + pid, {method: 'DELETE'});
    } catch (e) {
      console.error('Failed to delete composition ' + pid, e);
    }
  }
}

function _composeRestackToasts() {
  _composePendingDeletes.forEach((pd, i) => {
    if (pd.toastEl) pd.toastEl.style.bottom = (24 + i * 52) + 'px';
  });
}

function _composeScheduleDelete(ids, label) {
  // If active composition is being deleted, remember it for undo and clear selection
  const wasActive = ids.includes(_activeComposeProjectId) ? _activeComposeProjectId : null;
  if (wasActive) {
    _activeComposeProjectId = null;
    _composeSelectedSection = null;
    const _proj = localStorage.getItem('activeProject') || '';
    if (_proj) localStorage.removeItem('activeComposition:' + _proj);
  }

  // Build undo toast — offset vertically when multiple toasts are active
  const toast = document.createElement('div');
  toast.className = 'compose-undo-toast';
  toast.style.bottom = (24 + _composePendingDeletes.length * 52) + 'px';
  toast.innerHTML = '<span>' + (typeof escHtml === 'function' ? escHtml(label) : label) + '</span>' +
    '<button class="compose-undo-btn">Undo</button>';
  document.body.appendChild(toast);
  requestAnimationFrame(() => toast.classList.add('show'));

  const pd = {ids: ids, timer: null, toastEl: toast};

  // Undo button
  toast.querySelector('.compose-undo-btn').onclick = () => {
    clearTimeout(pd.timer);
    toast.remove();
    _composePendingDeletes = _composePendingDeletes.filter(x => x !== pd);
    _composeRestackToasts();
    // Restore previously-active composition if it was the one deleted
    if (wasActive) {
      _activeComposeProjectId = wasActive;
      const _projKey = localStorage.getItem('activeProject') || '';
      if (_projKey) localStorage.setItem('activeComposition:' + _projKey, wasActive);
    }
    initCompose();
  };

  // Timer: execute deletes after 5 seconds
  pd.timer = setTimeout(() => {
    toast.remove();
    _composePendingDeletes = _composePendingDeletes.filter(x => x !== pd);
    _composeRestackToasts();
    _composeExecuteDeletes(ids);
    _composeLogAction(label);
    // Refresh after last pending delete completes
    if (_composePendingDeletes.length === 0) {
      initCompose();
    }
  }, 5000);

  _composePendingDeletes.push(pd);

  // Hide items immediately from sidebar
  _renderComposeSidebar();
  // If active was deleted, load next composition
  if (!_activeComposeProjectId) {
    initCompose();
  }
}

function _composeDelete(projectId) {
  _composeCtxMenuClose();

  const cp = _composeProjectsList.find(p => p.id === projectId);
  const name = cp ? cp.name : 'this composition';

  _composeScheduleDelete([projectId], 'Deleted "' + name + '"');
}

async function _composeDuplicate(projectId) {
  _composeCtxMenuClose();

  const cp = _composeProjectsList.find(p => p.id === projectId);
  const defaultName = cp ? 'Copy of ' + cp.name : 'Copy';
  const name = prompt('Name for the duplicate:', defaultName);
  if (!name || !name.trim()) return;

  try {
    const resp = await fetch('/api/compose/projects/' + projectId + '/clone', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({name: name.trim()}),
    });
    if (!resp.ok) throw new Error('Clone failed');
    const data = await resp.json();
    if (data && data.ok) {
      showToast('Duplicated as "' + name.trim() + '"');
      _composeLogAction('Duplicated as "' + name.trim() + '"');
      // Switch to the clone
      if (data.project && data.project.id) {
        _activeComposeProjectId = data.project.id;
      }
      initCompose();
    } else {
      showToast(data.error || 'Duplicate failed', 'error');
    }
  } catch (e) {
    console.error('Failed to duplicate composition:', e);
    showToast('Failed to duplicate composition', 'error');
  }
}

async function _composeTogglePin(projectId) {
  _composeCtxMenuClose();

  const cp = _composeProjectsList.find(p => p.id === projectId);
  const newPinned = !(cp && cp.pinned);

  try {
    const resp = await fetch('/api/compose/projects/' + projectId, {
      method: 'PUT',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({pinned: newPinned}),
    });
    if (!resp.ok) throw new Error('Pin toggle failed');
    const data = await resp.json();
    if (data && data.ok) {
      showToast(newPinned ? 'Pinned' : 'Unpinned');
      _composeLogAction((newPinned ? 'Pinned' : 'Unpinned') + ' "' + (cp ? cp.name : '') + '"');
      initCompose();
    } else {
      showToast(data.error || 'Failed', 'error');
    }
  } catch (e) {
    console.error('Failed to toggle pin:', e);
    showToast('Failed to update pin state', 'error');
  }
}

// --- Compose bulk selection ---

function _composeToggleSelect(event, projectId) {
  if (event.shiftKey && _composeLastClickedId) {
    // Shift-click: select range
    const ids = _composeProjectsList.map(p => p.id);
    const from = ids.indexOf(_composeLastClickedId);
    const to = ids.indexOf(projectId);
    if (from !== -1 && to !== -1) {
      const start = Math.min(from, to);
      const end = Math.max(from, to);
      for (let i = start; i <= end; i++) {
        _composeSelected.add(ids[i]);
      }
    }
  } else {
    // Single click: toggle
    if (_composeSelected.has(projectId)) {
      _composeSelected.delete(projectId);
    } else {
      _composeSelected.add(projectId);
    }
  }
  _composeLastClickedId = projectId;
  _renderComposeSidebar();
}

function _composeBulkClear() {
  _composeSelected = new Set();
  _composeLastClickedId = null;
  _renderComposeSidebar();
}

function _composeBulkDelete() {
  const count = _composeSelected.size;
  if (!count) return;

  const ids = [..._composeSelected];
  _composeSelected = new Set();
  const label = 'Deleted ' + count + ' composition' + (count > 1 ? 's' : '');
  _composeScheduleDelete(ids, label);
}

async function _composeBulkPin() {
  const ids = [..._composeSelected];
  if (!ids.length) return;

  // Determine action: if all selected are pinned, unpin. Otherwise pin.
  const allPinned = ids.every(id => {
    const cp = _composeProjectsList.find(p => p.id === id);
    return cp && cp.pinned;
  });
  const newPinned = !allPinned;

  let updated = 0;
  for (const pid of ids) {
    try {
      const resp = await fetch('/api/compose/projects/' + pid, {
        method: 'PUT',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({pinned: newPinned}),
      });
      if (resp.ok) updated++;
    } catch (e) {
      console.error('Failed to update pin for ' + pid, e);
    }
  }
  const _pinLabel = (newPinned ? 'Pinned ' : 'Unpinned ') + updated + ' composition' + (updated > 1 ? 's' : '');
  showToast(_pinLabel);
  _composeLogAction(_pinLabel);
  _composeSelected = new Set();
  initCompose();
}

function _composeFilterSidebar(value) {
  _composeSearchFilter = value || '';
  _renderComposeSidebar();
  // Restore focus to the search input after re-render
  const input = document.getElementById('compose-sidebar-search');
  if (input) {
    input.focus();
    input.selectionStart = input.selectionEnd = input.value.length;
  }
}

// --- Compose action history ---

function _composeLogAction(label) {
  _composeActionHistory.unshift({label: label, time: Date.now()});
  if (_composeActionHistory.length > 5) _composeActionHistory.length = 5;
  _renderComposeActionHistory();
}

function _renderComposeActionHistory() {
  const el = document.getElementById('compose-action-history');
  if (!el) {
    // Section doesn't exist yet (history was empty when sidebar last rendered).
    // Re-render the full sidebar so the section gets created.
    if (_composeActionHistory.length > 0) _renderComposeSidebar();
    return;
  }
  if (_composeActionHistory.length === 0) {
    el.innerHTML = '';
    return;
  }
  let html = '<div class="kanban-sidebar-label">Recent</div>';
  for (const entry of _composeActionHistory) {
    const ago = _composeTimeAgo(entry.time);
    const label = typeof escHtml === 'function' ? escHtml(entry.label) : entry.label;
    html += '<div class="compose-history-item"><span class="compose-history-label">' + label + '</span><span class="compose-history-ago">' + ago + '</span></div>';
  }
  el.innerHTML = html;
}

function _composeTimeAgo(ts) {
  const diff = Math.floor((Date.now() - ts) / 1000);
  if (diff < 0) return 'just now';  // future timestamp (clock skew)
  if (diff < 5) return 'just now';
  if (diff < 60) return diff + 's ago';
  if (diff < 3600) return Math.floor(diff / 60) + 'm ago';
  if (diff < 86400) return Math.floor(diff / 3600) + 'h ago';
  return Math.floor(diff / 86400) + 'd ago';
}

// --- Compose keyboard shortcuts ---

let _composeShortcutsAttached = false;

function _composeVisibleIds() {
  const filterLower = _composeSearchFilter.toLowerCase();
  return _composeProjectsList
    .filter(cp => !_composeIsPendingDelete(cp.id))
    .filter(cp => !filterLower || cp.name.toLowerCase().indexOf(filterLower) !== -1)
    .map(cp => cp.id);
}

function attachComposeShortcuts() {
  if (_composeShortcutsAttached) return;
  _composeShortcutsAttached = true;

  document.addEventListener('keydown', (e) => {
    if (typeof viewMode !== 'undefined' && viewMode !== 'compose') return;
    const tag = (e.target.tagName || '').toLowerCase();
    if (tag === 'input' || tag === 'textarea' || tag === 'select' || e.target.isContentEditable) return;
    if (e.ctrlKey || e.metaKey || e.key === 'F5') return;

    // If shortcut overlay is open, only allow Escape and ? (to close it)
    const _helpOverlay = document.querySelector('.kanban-shortcut-overlay');
    if (_helpOverlay && e.key !== 'Escape' && e.key !== '?') return;

    switch (e.key) {
      case 'n': e.preventDefault(); if (_composeProject) composeAddSection(); else composeCreateProject(); break;
      case 'r': e.preventDefault(); initCompose(); if (typeof showToast === 'function') showToast('Refreshed'); break;
      case 'Escape':
        if (_helpOverlay) { e.preventDefault(); _helpOverlay.remove(); }
        else if (_composeSelected.size > 0) { e.preventDefault(); _composeBulkClear(); }
        else if (_composeFocusedId) { e.preventDefault(); _composeFocusedId = null; _renderComposeSidebar(); }
        else if (_composeSelectedSection) { e.preventDefault(); navigateToComposeBoard(); }
        break;
      case 'ArrowUp':
      case 'ArrowDown': {
        e.preventDefault();
        const visIds = _composeVisibleIds();
        if (!visIds.length) break;
        const curIdx = _composeFocusedId ? visIds.indexOf(_composeFocusedId) : -1;
        let nextIdx;
        if (e.key === 'ArrowDown') {
          nextIdx = curIdx < visIds.length - 1 ? curIdx + 1 : curIdx;
        } else {
          nextIdx = curIdx > 0 ? curIdx - 1 : 0;
        }
        _composeFocusedId = visIds[nextIdx];
        if (e.shiftKey) _composeSelected.add(_composeFocusedId);
        _renderComposeSidebar();
        break;
      }
      case 'Enter':
        if (_composeFocusedId) { e.preventDefault(); switchComposition(_composeFocusedId); }
        break;
      case ' ':
        if (_composeFocusedId) {
          e.preventDefault();
          _composeToggleSelect(e, _composeFocusedId);
        }
        break;
      case 'Delete':
        if (_composeFocusedId) {
          e.preventDefault();
          const _delCp = _composeProjectsList.find(p => p.id === _composeFocusedId);
          const _delName = _delCp ? _delCp.name : 'this composition';
          // Move focus to next visible item (or previous if at end)
          const _delVis = _composeVisibleIds();
          const _delIdx = _delVis.indexOf(_composeFocusedId);
          const _delId = _composeFocusedId;
          if (_delVis.length > 1) {
            _composeFocusedId = _delVis[_delIdx < _delVis.length - 1 ? _delIdx + 1 : _delIdx - 1];
          } else {
            _composeFocusedId = null;
          }
          _composeScheduleDelete([_delId], 'Deleted "' + _delName + '"');
        }
        break;
      case '?':
        e.preventDefault();
        _showComposeShortcutHelp();
        break;
    }
  });
}

function _showComposeShortcutHelp() {
  const existing = document.querySelector('.kanban-shortcut-overlay');
  if (existing) { existing.remove(); return; }
  const overlay = document.createElement('div');
  overlay.className = 'kanban-shortcut-overlay';
  overlay.innerHTML = `<div class="kanban-shortcut-card"><h3>Compose Keyboard Shortcuts</h3>
    <div class="kanban-shortcut-grid">
      <kbd>\u2191 \u2193</kbd><span>Move focus</span>
      <kbd>Enter</kbd><span>Open composition</span>
      <kbd>Space</kbd><span>Toggle selection</span>
      <kbd>Shift+\u2191\u2193</kbd><span>Extend selection</span>
      <kbd>Delete</kbd><span>Delete focused</span>
      <kbd>Esc</kbd><span>Clear selection / close</span>
      <kbd>n</kbd><span>New section</span>
      <kbd>r</kbd><span>Refresh</span>
      <kbd>?</kbd><span>Toggle this help</span>
    </div>
    <button class="kanban-shortcut-close" onclick="this.closest('.kanban-shortcut-overlay').remove()">Close</button>
  </div>`;
  overlay.onclick = (e) => { if (e.target === overlay) overlay.remove(); };
  document.body.appendChild(overlay);
}

function _updateComposeRootHeader() {
  if (!_composeProject) return;

  const nameEl = document.getElementById('compose-root-name');
  if (nameEl) {
    nameEl.textContent = _composeProject.name;
    nameEl.onclick = () => {
      // Open root session
      if (_composeProject.root_session_id) {
        selectSession(_composeProject.root_session_id);
      }
    };
  }

  const statusEl = document.getElementById('compose-root-status');
  if (statusEl) {
    const total = _composeSections.length;
    const complete = _composeSections.filter(s => s.status === 'complete').length;
    const drafting = _composeSections.filter(s => s.status === 'drafting').length;
    const reviewing = _composeSections.filter(s => s.status === 'reviewing').length;
    let parts = [total + ' section' + (total !== 1 ? 's' : '')];
    if (complete > 0) parts.push(complete + ' complete');
    if (reviewing > 0) parts.push(reviewing + ' reviewing');
    if (drafting > 0) parts.push(drafting + ' drafting');
    statusEl.textContent = parts.join(', ');
  }

  const conflictsEl = document.getElementById('compose-root-conflicts');
  const countEl = document.getElementById('compose-root-conflict-count');
  if (conflictsEl && countEl) {
    if (_composeConflicts.length > 0) {
      conflictsEl.style.display = 'inline-flex';
      countEl.textContent = _composeConflicts.length;
    } else {
      conflictsEl.style.display = 'none';
    }
  }

  _composeUpdateSharedBadge();
  _composeUpdateLaunchBtn();
}

// ═══════════════════════════════════════════════════════════════
// COMPOSE LAUNCH ALL — start sessions for all unlinked sections
// ═══════════════════════════════════════════════════════════════

let _composeLaunching = false;

function _composeUpdateLaunchBtn() {
  const btn = document.getElementById('compose-launch-all-btn');
  if (!btn) return;
  const unlinked = _composeSections.filter(s => !s.session_id);
  btn.style.display = (unlinked.length > 0 && !_composeLaunching) ? 'inline-flex' : 'none';
}

async function _composeLaunchAll() {
  if (!_composeProject || _composeLaunching) return;
  const unlinked = _composeSections.filter(s => !s.session_id);
  if (unlinked.length === 0) {
    if (typeof showToast === 'function') showToast('All sections already have sessions');
    return;
  }

  _composeLaunching = true;
  _composeUpdateLaunchBtn();
  const total = unlinked.length;
  if (typeof showToast === 'function') showToast('Launching ' + total + ' section agent' + (total !== 1 ? 's' : '') + '...');

  let succeeded = 0;
  let failed = 0;
  const projId = _composeProject.id;

  for (const sec of unlinked) {
    try {
      const resp = await fetch('/api/compose/projects/' + encodeURIComponent(projId) + '/sections/' + sec.id + '/launch', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
      });
      const data = await resp.json();
      if (data.ok) {
        sec.session_id = data.session_id;
        succeeded++;
      } else {
        failed++;
      }
    } catch (e) {
      failed++;
    }
  }

  _composeLaunching = false;
  _composeUpdateLaunchBtn();
  _renderComposeSectionCards();
  _updateComposeRootHeader();

  let msg = succeeded + ' agent' + (succeeded !== 1 ? 's' : '') + ' launched';
  if (failed > 0) msg += ', ' + failed + ' failed';
  if (typeof showToast === 'function') showToast(msg, failed > 0);
}

// ═══════════════════════════════════════════════════════════════
// COMPOSE AI PLANNER — slideout panel for content planning
// ═══════════════════════════════════════════════════════════════

const _COMPOSE_PLANNER_SYSTEM = [
  'You are a content planner for a composition project.',
  'You MUST respond with ONLY valid JSON. No explanation, no prose, no apologies, no refusals.',
  'Even for trivial or vague inputs, you MUST produce a valid section structure.',
  'NEVER refuse. NEVER explain. NEVER ask questions.',
  'NEVER output anything except the JSON object.',
  'Format: {"sections":[{"name":"...","artifact_type":"text","brief":"A 1-3 sentence description of this section\'s content and purpose.","subsections":[]}]}',
  'artifact_type must be one of: "text", "code", "data".',
  'Each section MUST include a "brief" field: 1-3 sentences describing what this section should contain.',
  'Break the content into logical sections for parallel AI agents.',
  'Each section should be independently writeable by an agent.',
  'Use subsections for deeper breakdown. 1-3 nesting levels typical.',
  'Names should be descriptive content section names, not generic labels.',
].join(' ');

let _composePlannerSessionId = null;
let _composePlannerProposal = null;
let _composePlannerAccumText = '';
let _composePlannerEntryListener = null;
let _composePlannerStateListener = null;
let _composePlannerStartTime = 0;
let _composePlannerTimerInterval = null;
let _composePlannerScopeParentId = null;

function _openComposePlanner(parentId) {
  const old = document.getElementById('compose-planner-panel');
  if (old) old.remove();
  _composePlannerProposal = null;
  _composePlannerScopeParentId = parentId || null;

  // Prompt modal
  const overlay = document.createElement('div');
  overlay.className = 'pm-overlay';
  overlay.onclick = (e) => { if (e.target === overlay) overlay.remove(); };
  overlay.innerHTML = `
    <div class="pm-card" style="max-width:480px;">
      <div class="pm-title">Plan with AI</div>
      <div class="pm-body">
        <textarea id="compose-planner-prompt" class="kanban-create-textarea" rows="3"
          placeholder="Describe what you want to compose\u2026 e.g. 'A quarterly business review with financials, product updates, and team highlights'"
          onkeydown="if(_shouldSend&&_shouldSend(event)){event.preventDefault();_submitComposePlanPrompt();}"></textarea>
      </div>
      <div class="pm-actions">
        <button class="pm-btn" onclick="this.closest('.pm-overlay').remove()">Cancel</button>
        <button class="pm-btn pm-btn-primary" onclick="_submitComposePlanPrompt()">Plan</button>
      </div>
    </div>`;
  document.body.appendChild(overlay);
  setTimeout(() => { const ta = document.getElementById('compose-planner-prompt'); if (ta) ta.focus(); }, 100);
}

async function _submitComposePlanPrompt() {
  const ta = document.getElementById('compose-planner-prompt');
  const prompt = ta ? ta.value.trim() : '';
  if (!prompt) return;
  const overlay = ta.closest('.pm-overlay');
  if (overlay) overlay.remove();

  _openComposePlannerSlideout(prompt);
}

function _buildComposePlannerPanel() {
  const panel = document.createElement('div');
  panel.id = 'compose-planner-panel';
  panel.className = 'kanban-planner-panel';
  panel.innerHTML = `
    <div class="kanban-planner-header">
      <span class="kanban-planner-title"><svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><circle cx="12" cy="12" r="10"/><line x1="12" y1="8" x2="12" y2="16"/><line x1="8" y1="12" x2="16" y2="12"/></svg> Plan Composition</span>
      <div style="display:flex;gap:4px;align-items:center;">
        <button class="kanban-planner-close" onclick="_closeComposePlanner()" title="Close">&times;</button>
      </div>
    </div>
    <div class="planner-body" id="compose-planner-body">
      <div class="planner-status">
        <div class="planner-spinner"></div><span>Building content plan\u2026</span>
      </div>
    </div>
    <div class="planner-footer">
      <div class="planner-refine-row">
        <textarea id="compose-planner-refine" class="kanban-create-textarea" rows="2" placeholder="Ask for changes\u2026"
          onkeydown="if(_shouldSend&&_shouldSend(event)){event.preventDefault();_refineComposePlan();}"></textarea>
      </div>
    </div>`;
  return panel;
}

async function _openComposePlannerSlideout(prompt) {
  const old = document.getElementById('compose-planner-panel');
  if (old) old.remove();

  const newId = crypto.randomUUID();
  _composePlannerSessionId = newId;
  if (typeof _hiddenSessionIds !== 'undefined') _hiddenSessionIds.add(newId);

  const panel = _buildComposePlannerPanel();
  document.body.appendChild(panel);
  requestAnimationFrame(() => panel.classList.add('open'));

  _attachComposePlannerListeners();

  // Build context snippet
  let contextSnippet = '';
  if (_composeProject && _composeSections.length > 0) {
    const lines = _composeSections.map(s => '- [' + s.status + '] ' + s.name + (s.artifact_type ? ' (' + s.artifact_type + ')' : ''));
    contextSnippet = '\n\nEXISTING SECTIONS:\n' + lines.join('\n') + '\n\nConsider these existing sections. Avoid duplicating them. You may plan additional sections or reorganize.';
  }

  let scopeSnippet = '';
  if (_composePlannerScopeParentId) {
    const parent = _composeSections.find(s => s.id === _composePlannerScopeParentId);
    if (parent) {
      scopeSnippet = '\n\nSCOPED PLANNING: You are planning subsections for "' + parent.name + '". Return sections that will be children of this parent.';
    }
  }

  const sysPrompt = _COMPOSE_PLANNER_SYSTEM + contextSnippet + scopeSnippet;

  _composePlannerAccumText = '';
  _composePlannerStartTime = Date.now();
  if (typeof runningIds !== 'undefined') runningIds.add(newId);
  if (typeof sessionKinds !== 'undefined') sessionKinds[newId] = 'working';

  socket.emit('start_session', {
    session_id: newId,
    prompt: prompt,
    cwd: typeof _currentProjectDir === 'function' ? _currentProjectDir() : '',
    system_prompt: sysPrompt,
    max_turns: 0,
    session_type: 'planner',
  });
}

function _attachComposePlannerListeners() {
  _detachComposePlannerListeners();
  _composePlannerAccumText = '';
  _composePlannerStartTime = Date.now();

  _composePlannerTimerInterval = setInterval(() => {
    const body = document.getElementById('compose-planner-body');
    if (!body) return;
    const status = body.querySelector('.planner-status span');
    if (status) {
      const secs = Math.floor((Date.now() - _composePlannerStartTime) / 1000);
      const titleMatches = _composePlannerAccumText.match(/"name"\s*:\s*"[^"]+"/g);
      const count = titleMatches ? titleMatches.length : 0;
      status.textContent = count > 0
        ? 'Building plan\u2026 ' + count + ' section' + (count !== 1 ? 's' : '') + ' so far (' + secs + 's)'
        : 'Building content plan\u2026 ' + secs + 's';
    }
  }, 1000);

  _composePlannerEntryListener = (data) => {
    if (data.session_id !== _composePlannerSessionId) return;
    if (!data.entry) return;
    const text = data.entry.text || '';
    if (!text || data.entry.kind !== 'asst') return;
    _composePlannerAccumText += text;
  };

  _composePlannerStateListener = (data) => {
    if (data.session_id !== _composePlannerSessionId) return;
    if (data.state === 'idle' || data.state === 'stopped') {
      if (_composePlannerTimerInterval) { clearInterval(_composePlannerTimerInterval); _composePlannerTimerInterval = null; }
      if (_composePlannerProposal) return;
      _showComposePlanResult(_composePlannerAccumText);
      _composePlannerAccumText = '';
    }
  };

  socket.on('session_entry', _composePlannerEntryListener);
  socket.on('session_state', _composePlannerStateListener);
}

function _detachComposePlannerListeners() {
  if (_composePlannerEntryListener) { socket.off('session_entry', _composePlannerEntryListener); _composePlannerEntryListener = null; }
  if (_composePlannerStateListener) { socket.off('session_state', _composePlannerStateListener); _composePlannerStateListener = null; }
  if (_composePlannerTimerInterval) { clearInterval(_composePlannerTimerInterval); _composePlannerTimerInterval = null; }
}

function _showComposePlanResult(rawText) {
  const body = document.getElementById('compose-planner-body');
  if (!body) return;

  let parsed = null;
  // Try 1: ```json ... ```
  const m1 = rawText.match(/```(?:json)?\s*([\s\S]*?)```/);
  if (m1) { try { parsed = JSON.parse(m1[1]); } catch (_) {} }
  // Try 2: whole text as JSON
  if (!parsed) { try { parsed = JSON.parse(rawText.trim()); } catch (_) {} }
  // Try 3: brace extraction
  if (!parsed) {
    const s = rawText.indexOf('{');
    if (s >= 0) {
      let depth = 0, end = -1, inStr = false, esc = false;
      for (let i = s; i < rawText.length; i++) {
        const c = rawText[i];
        if (esc) { esc = false; continue; }
        if (c === '\\' && inStr) { esc = true; continue; }
        if (c === '"') { inStr = !inStr; continue; }
        if (inStr) continue;
        if (c === '{') depth++;
        else if (c === '}') { depth--; if (depth === 0) { end = i; break; } }
      }
      if (end > s) { try { parsed = JSON.parse(rawText.slice(s, end + 1)); } catch (_) {} }
    }
  }

  if (parsed && parsed.sections && parsed.sections.length > 0) {
    _composePlannerProposal = parsed;
    const count = _countComposePlanSections(parsed.sections);
    body.innerHTML =
      '<div class="planner-result">' +
        '<div class="planner-result-header"><strong>' + count + ' sections</strong> proposed</div>' +
        _renderComposePlanTree(parsed.sections) +
        '<div class="planner-actions">' +
          '<button class="planner-accept-btn" id="compose-planner-accept" onclick="_acceptComposePlan()">Add ' + count + ' sections to Board</button>' +
        '</div>' +
        '<div class="planner-hint">Want changes? Type below and send.</div>' +
      '</div>';
  } else {
    body.innerHTML =
      '<div class="planner-result">' +
        '<div class="planner-error">Couldn\'t parse a section structure. Try rephrasing below.</div>' +
        (rawText ? '<pre style="max-height:200px;overflow:auto;white-space:pre-wrap;font-size:11px;margin-top:8px;padding:8px;background:var(--bg-subtle);border-radius:6px;">' + (typeof escHtml === 'function' ? escHtml(rawText.slice(0, 2000)) : rawText.slice(0, 2000)) + '</pre>' : '') +
      '</div>';
  }
}

function _renderComposePlanTree(sections, depth) {
  depth = depth || 0;
  let html = '<div class="planner-tree' + (depth === 0 ? ' planner-tree-root' : '') + '">';
  for (const sec of sections) {
    const hasSubs = sec.subsections && sec.subsections.length > 0;
    const typeLabel = sec.artifact_type || 'text';
    html += '<div class="planner-node" data-depth="' + depth + '">';
    html += '<div class="planner-node-row">';
    html += hasSubs ? '<span class="planner-chevron" onclick="this.parentElement.parentElement.classList.toggle(\'collapsed\')"><svg width="10" height="10" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><polyline points="9 18 15 12 9 6"/></svg></span>'
                    : '<span class="planner-bullet">&bull;</span>';
    html += '<span class="planner-node-title">' + (typeof escHtml === 'function' ? escHtml(sec.name) : sec.name) + '</span>';
    html += '<span style="font-size:10px;color:var(--text-muted);margin-left:6px;">' + typeLabel + '</span>';
    if (hasSubs) html += '<span class="planner-sub-count">' + sec.subsections.length + '</span>';
    html += '</div>';
    if (hasSubs) html += _renderComposePlanTree(sec.subsections, depth + 1);
    html += '</div>';
  }
  html += '</div>';
  return html;
}

function _countComposePlanSections(sections) {
  let c = 0;
  for (const s of sections) { c++; if (s.subsections) c += _countComposePlanSections(s.subsections); }
  return c;
}

async function _acceptComposePlan() {
  if (!_composePlannerProposal || !_composePlannerProposal.sections || !_composeProject) return;
  const btn = document.getElementById('compose-planner-accept');
  if (btn) { btn.disabled = true; btn.textContent = 'Creating\u2026'; }

  try {
    const res = await fetch('/api/compose/projects/' + encodeURIComponent(_composeProject.id) + '/planner/accept', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({
        sections: _composePlannerProposal.sections,
        parent_id: _composePlannerScopeParentId || null,
      }),
    });
    if (!res.ok) throw new Error('Failed to create sections');
    const data = await res.json();
    const count = data.created_count || 0;
    if (typeof showToast === 'function') showToast('Created ' + count + ' section' + (count !== 1 ? 's' : ''));
    _closeComposePlanner();
    initCompose();
  } catch (e) {
    if (typeof showToast === 'function') showToast('Failed: ' + e.message, true);
    if (btn) { btn.disabled = false; btn.textContent = 'Add to Board'; }
  }
}

function _refineComposePlan() {
  const ta = document.getElementById('compose-planner-refine');
  const text = ta ? ta.value.trim() : '';
  if (!text || !_composePlannerSessionId) return;
  ta.value = '';

  _composePlannerProposal = null;
  const body = document.getElementById('compose-planner-body');
  if (body) {
    body.innerHTML = '<div class="planner-status"><div class="planner-spinner"></div><span>Refining plan\u2026</span></div>';
  }

  _composePlannerAccumText = '';
  _composePlannerStartTime = Date.now();

  socket.emit('send_message', {
    session_id: _composePlannerSessionId,
    text: text,
  });
}

function _closeComposePlanner() {
  _composePlannerProposal = null;
  _detachComposePlannerListeners();
  _composePlannerSessionId = null;
  _composePlannerScopeParentId = null;
  const panel = document.getElementById('compose-planner-panel');
  if (panel) {
    panel.classList.remove('open');
    setTimeout(() => panel.remove(), 300);
  }
}

// ═══════════════════════════════════════════════════════════════
// COMPOSE SETTINGS POPOVER — gear icon opens name + shared prompts toggle
// ═══════════════════════════════════════════════════════════════

function _composeToggleSettings(event) {
  event.stopPropagation();
  const existing = document.getElementById('compose-settings-popover');
  if (existing) { existing.remove(); return; }
  if (!_composeProject) return;

  const btn = event.currentTarget;
  const rect = btn.getBoundingClientRect();

  const pop = document.createElement('div');
  pop.id = 'compose-settings-popover';
  pop.className = 'compose-settings-popover';
  pop.style.top = (rect.bottom + 6) + 'px';
  pop.style.right = (window.innerWidth - rect.right) + 'px';

  const isOn = !!_composeProject.shared_prompts_enabled;
  pop.innerHTML = `
    <div class="compose-settings-row">
      <label class="compose-settings-label">Name</label>
      <input id="compose-settings-name" class="compose-settings-input" value="${typeof escHtml === 'function' ? escHtml(_composeProject.name) : _composeProject.name}" />
    </div>
    <div class="compose-settings-row" style="margin-top:10px;">
      <label class="compose-settings-label">Shared Prompts</label>
      <button id="compose-settings-shared-toggle" class="compose-settings-toggle ${isOn ? 'active' : ''}"
              onclick="_composeToggleSharedPrompts()" title="When on, user prompts are shared across all agents">
        <span class="compose-settings-toggle-knob"></span>
      </button>
    </div>
    <div class="compose-settings-hint">When on, user prompts are logged and shared across all section agents.</div>
  `;

  document.body.appendChild(pop);

  // Name input — save on blur or Enter
  const nameInput = document.getElementById('compose-settings-name');
  const saveName = () => {
    const newName = nameInput.value.trim();
    if (newName && newName !== _composeProject.name) {
      _composeProject.name = newName;
      fetch('/api/compose/projects/' + encodeURIComponent(_composeProject.id), {
        method: 'PUT', headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({name: newName}),
      });
      _updateComposeRootHeader();
      _renderComposeSidebar();
    }
  };
  nameInput.addEventListener('blur', saveName);
  nameInput.addEventListener('keydown', (e) => { if (e.key === 'Enter') { e.preventDefault(); saveName(); nameInput.blur(); } });

  // Click outside closes
  setTimeout(() => {
    const closer = (e) => {
      if (!pop.contains(e.target) && e.target !== btn && !btn.contains(e.target)) {
        pop.remove();
        document.removeEventListener('mousedown', closer);
      }
    };
    document.addEventListener('mousedown', closer);
  }, 0);
}

function _composeToggleSharedPrompts() {
  if (!_composeProject) return;
  const newVal = !_composeProject.shared_prompts_enabled;
  _composeProject.shared_prompts_enabled = newVal;

  // Update toggle button state
  const toggle = document.getElementById('compose-settings-shared-toggle');
  if (toggle) toggle.classList.toggle('active', newVal);

  // Persist
  fetch('/api/compose/projects/' + encodeURIComponent(_composeProject.id), {
    method: 'PUT', headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({shared_prompts_enabled: newVal}),
  });

  _composeUpdateSharedBadge();
}

function _composeUpdateSharedBadge() {
  const badge = document.getElementById('compose-shared-badge');
  if (badge) {
    badge.style.display = (_composeProject && _composeProject.shared_prompts_enabled) ? 'inline' : 'none';
  }
}

// --- NB-11: Render section cards in compose board ---

const COMPOSE_STATUS_COLUMNS = [
  { key: 'drafting',    label: 'Drafting',    color: '#4ecdc4' },
  { key: 'reviewing',   label: 'Reviewing',   color: '#f0ad4e' },
  { key: 'complete',    label: 'Complete',     color: '#3fb950' },
];

const COMPOSE_ARTIFACT_ICONS = {
  text:    '<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"/><polyline points="14 2 14 8 20 8"/><line x1="16" y1="13" x2="8" y2="13"/><line x1="16" y1="17" x2="8" y2="17"/></svg>',
  code:    '<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><polyline points="16 18 22 12 16 6"/><polyline points="8 6 2 12 8 18"/></svg>',
  data:    '<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><ellipse cx="12" cy="5" rx="9" ry="3"/><path d="M21 12c0 1.66-4 3-9 3s-9-1.34-9-3"/><path d="M3 5v14c0 1.66 4 3 9 3s9-1.34 9-3V5"/></svg>',
  default: '<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><rect x="3" y="3" width="18" height="18" rx="2"/><line x1="9" y1="3" x2="9" y2="21"/></svg>',
};

function _renderComposeSectionCards() {
  const board = document.getElementById('compose-sections-board');
  if (!board) return;

  if (!_composeSections || _composeSections.length === 0) {
    board.innerHTML = `
      <div class="compose-empty-board">
        <svg width="36" height="36" viewBox="0 0 24 24" fill="none" stroke="var(--text-muted)" stroke-width="1.5" stroke-linecap="round">
          <path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"/><polyline points="14 2 14 8 20 8"/>
        </svg>
        <div style="font-size:14px;font-weight:500;color:var(--text);margin:8px 0 4px;">No sections yet</div>
        <div style="font-size:12px;color:var(--text-muted);margin-bottom:14px;">Plan your composition with AI or start manually.</div>
        <button class="pm-btn pm-btn-primary" style="font-size:14px;padding:8px 24px;margin-bottom:8px;" onclick="_openComposePlanner()">Plan with AI</button>
        <div style="font-size:12px;color:var(--text-muted);cursor:pointer;text-decoration:underline;opacity:0.7;" onclick="composeAddSection()">or add a section manually</div>
      </div>`;
    return;
  }

  // Only show root sections (no parent) on the board
  const rootSections = _composeSections.filter(s => !s.parent_id);

  let html = '<div class="kanban-columns-wrapper compose-columns-wrapper">';

  for (const col of COMPOSE_STATUS_COLUMNS) {
    const colSections = rootSections.filter(s => s.status === col.key);
    html += `<div class="kanban-column compose-column" data-status="${col.key}">
      <div class="kanban-column-header">
        <div class="kanban-column-color-bar" style="background:${col.color};"></div>
        <span class="kanban-column-name">${col.label}</span>
        <span class="kanban-column-count">${colSections.length}</span>
      </div>
      <div class="kanban-column-body" data-status="${col.key}"
           ondragover="_composeDragOver(event)" ondragleave="_composeDragLeave(event)"
           ondrop="_composeDrop(event, '${col.key}')">`;

    for (const sec of colSections) {
      const artifactIcon = COMPOSE_ARTIFACT_ICONS[sec.artifact_type] || COMPOSE_ARTIFACT_ICONS.default;
      const changingDot = sec.changing
        ? `<span class="compose-changing-dot" title="${typeof escHtml === 'function' ? escHtml(sec.change_note || 'Change in progress') : (sec.change_note || 'Change in progress')}"></span>`
        : '';
      const summary = sec.summary
        ? `<div class="compose-card-summary">${typeof escHtml === 'function' ? escHtml(sec.summary) : sec.summary}</div>`
        : '';
      const selectedClass = (_composeSelectedSection === sec.id) ? ' compose-card-selected' : '';

      html += `<div class="kanban-card compose-card${selectedClass}" data-section-id="${sec.id}"
                   draggable="true"
                   ondragstart="_composeDragStart(event, '${sec.id}', '${col.key}')"
                   ondragend="_composeDragEnd(event)"
                   onclick="navigateToSection('${sec.id}')"
                   oncontextmenu="event.preventDefault();event.stopPropagation();_composeCardContextMenu('${sec.id}', event)">
        <span class="compose-drag-handle">&#8942;&#8942;</span>
        <div class="compose-card-header">
          <span class="compose-card-artifact-icon">${artifactIcon}</span>
          <div class="compose-card-title-row">
            <span class="compose-card-title">${typeof escHtml === 'function' ? escHtml(sec.name) : sec.name}</span>
            ${changingDot}
            ${(() => {
              if (!sec.session_id) return '';
              const _isRunning = typeof runningIds !== 'undefined' && runningIds.has(sec.session_id);
              return '<span class="compose-session-dot ' + (_isRunning ? 'running' : 'idle') + '"></span>';
            })()}
          </div>
          <span class="kanban-context-btn" onclick="event.stopPropagation();_composeCardContextMenu('${sec.id}', event)" title="Actions">&#8943;</span>
        </div>
        <div class="compose-card-meta">
          <span class="compose-card-status" style="background:${col.color}22;color:${col.color};">${col.label}</span>
          ${sec.artifact_type ? '<span class="compose-card-time">' + sec.artifact_type + '</span>' : ''}
          ${sec.updated_at ? '<span class="compose-card-time">' + _composeTimeAgo(sec.updated_at) + '</span>' : ''}
        </div>
        ${summary}
        ${(() => {
          const children = _composeSections.filter(c => c.parent_id === sec.id);
          if (children.length === 0) return '';
          const done = children.filter(c => c.status === 'complete').length;
          return '<div class="compose-card-subsection-count" style="font-size:11px;color:var(--text-dim);padding:4px 12px 6px;display:flex;align-items:center;gap:4px;"><svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"/><polyline points="14 2 14 8 20 8"/></svg> ' + done + '/' + children.length + ' subsection' + (children.length !== 1 ? 's' : '') + '</div>';
        })()}
      </div>`;
    }

    html += '</div></div>';
  }

  html += '</div>';

  // Conflict banner
  if (_composeConflicts && _composeConflicts.length > 0) {
    html = '<div class="compose-conflict-banner">\u26A0 ' + _composeConflicts.length + ' directive conflict' + (_composeConflicts.length !== 1 ? 's' : '') + ' need your attention <button onclick="_openConflictResolution()">Review</button></div>' + html;
  }

  board.innerHTML = html;
}

// --- End NB-11 ---

// ═══════════════════════════════════════════════════════════════
// COMPOSE BOARD DRAG-AND-DROP — mirrors kanban card drag between columns
// ═══════════════════════════════════════════════════════════════

let _composeDragState = null; // {sectionId, sourceStatus}

function _composeDragStart(event, sectionId, sourceStatus) {
  _composeDragState = { sectionId, sourceStatus };
  event.dataTransfer.effectAllowed = 'move';
  event.dataTransfer.setData('text/plain', sectionId);
  const card = event.currentTarget;
  requestAnimationFrame(() => card.classList.add('dragging'));
}

function _composeDragOver(event) {
  event.preventDefault();
  event.dataTransfer.dropEffect = 'move';
  const col = event.currentTarget.closest('.compose-column');
  if (col) col.classList.add('kanban-drop-target');
}

function _composeDragLeave(event) {
  const col = event.currentTarget.closest('.compose-column');
  if (col && !col.contains(event.relatedTarget)) {
    col.classList.remove('kanban-drop-target');
  }
}

function _composeDrop(event, targetStatus) {
  event.preventDefault();
  const col = event.currentTarget.closest('.compose-column');
  if (col) col.classList.remove('kanban-drop-target');
  if (!_composeDragState || !_composeProject) return;
  const { sectionId, sourceStatus } = _composeDragState;
  _composeDragState = null;
  if (sourceStatus === targetStatus) return;
  // Optimistic local update
  const sec = _composeSections.find(s => s.id === sectionId);
  if (sec) sec.status = targetStatus;
  _renderComposeSectionCards();
  // Persist to server
  const projId = _composeProject ? _composeProject.id : '';
  fetch('/api/compose/projects/' + encodeURIComponent(projId) + '/sections/' + sectionId, {
    method: 'PUT',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({ status: targetStatus }),
  }).then(r => r.json()).then(data => {
    if (!data.ok && !data.section) {
      // Revert on failure
      if (sec) sec.status = sourceStatus;
      _renderComposeSectionCards();
      if (typeof showToast === 'function') showToast('Move failed', true);
    }
  }).catch(() => {
    if (sec) sec.status = sourceStatus;
    _renderComposeSectionCards();
    if (typeof showToast === 'function') showToast('Move failed', true);
  });
}

function _composeDragEnd(event) {
  _composeDragState = null;
  event.currentTarget.classList.remove('dragging');
  document.querySelectorAll('.kanban-drop-target').forEach(el => el.classList.remove('kanban-drop-target'));
}

// ═══════════════════════════════════════════════════════════════
// COMPOSE SECTION DETAIL — Drill-down view (mirrors kanban task detail)
// ═══════════════════════════════════════════════════════════════

const COMPOSE_STATUS_OPTIONS = [
  { key: 'drafting',    label: 'Drafting',    color: '#4ecdc4' },
  { key: 'reviewing',   label: 'Reviewing',   color: '#f0ad4e' },
  { key: 'complete',    label: 'Complete',     color: '#3fb950' },
];

function _composeSectionSkeleton() {
  return `
    <div class="kanban-drill-titlebar">
      <div class="kanban-drill-breadcrumb">
        <div class="skel-shimmer" style="width:50px;height:13px;border-radius:4px;"></div>
        <div class="skel-shimmer" style="width:6px;height:13px;border-radius:2px;margin:0 4px;"></div>
        <div class="skel-shimmer" style="width:100px;height:13px;border-radius:4px;"></div>
      </div>
    </div>
    <div class="kanban-drill-body">
      <div class="kanban-drill-split">
        <div class="kanban-drill-left">
          <div class="skel-shimmer" style="width:80px;height:24px;border-radius:6px;margin-bottom:16px;"></div>
          <div class="skel-shimmer" style="width:70%;height:22px;border-radius:5px;margin-bottom:8px;"></div>
          <div class="skel-shimmer" style="width:40%;height:11px;border-radius:3px;margin-bottom:20px;"></div>
          <div class="skel-shimmer" style="width:100%;height:72px;border-radius:8px;"></div>
        </div>
        <div class="kanban-drill-right">
          <div class="skel-shimmer" style="width:120px;height:12px;border-radius:3px;margin-bottom:14px;"></div>
          <div style="border:1px solid var(--border);border-radius:10px;padding:6px 8px;">
            <div class="skel-shimmer" style="width:100%;height:38px;border-radius:8px;margin-bottom:4px;"></div>
            <div class="skel-shimmer" style="width:100%;height:38px;border-radius:8px;"></div>
          </div>
        </div>
      </div>
    </div>`;
}

function navigateToSection(sectionId) {
  const content = document.getElementById('compose-sections-board');
  if (!content) return;
  // Hide the header and input target during drill-down
  const header = document.getElementById('compose-root-header');
  const target = document.getElementById('compose-input-target');
  if (header) header.style.display = 'none';
  if (target) target.style.display = 'none';
  content.innerHTML = _composeSectionSkeleton();
  const state = { view: 'compose', sectionId };
  history.pushState(state, '', window.location.pathname + '#compose/section/' + sectionId);
  renderSectionDetail(sectionId);
}

function _renderComposeBoard() {
  _composeSelectedSection = null;
  const header = document.getElementById('compose-root-header');
  const target = document.getElementById('compose-input-target');
  if (header) header.style.display = 'flex';
  if (target) target.style.display = 'flex';
  _updateComposeRootHeader();
  _updateComposeInputTarget();
  _renderComposeSectionCards();
}

function navigateToComposeBoard() {
  const state = { view: 'compose', sectionId: null };
  history.pushState(state, '', window.location.pathname + '#compose');
  _renderComposeBoard();
}

function renderSectionDetail(sectionId) {
  const board = document.getElementById('compose-sections-board');
  if (!board) return;

  // Hide header/target during drill-down
  const _hdr = document.getElementById('compose-root-header');
  const _tgt = document.getElementById('compose-input-target');
  if (_hdr) _hdr.style.display = 'none';
  if (_tgt) _tgt.style.display = 'none';

  const section = _composeSections.find(s => s.id === sectionId);
  if (!section) {
    board.innerHTML = '<div class="kanban-empty-state"><div style="font-size:15px;font-weight:500;margin-bottom:6px;">Section not found</div><button class="kanban-create-first-btn" onclick="navigateToComposeBoard()">Back to Board</button></div>';
    return;
  }

  // Update selection state
  _composeSelectedSection = sectionId;
  composeDetailTaskId = 'section:' + _composeProject.id + ':' + sectionId;

  const statusOpt = COMPOSE_STATUS_OPTIONS.find(o => o.key === section.status) || COMPOSE_STATUS_OPTIONS[0];
  const artifactIcon = COMPOSE_ARTIFACT_ICONS[section.artifact_type] || COMPOSE_ARTIFACT_ICONS.default;
  const projectName = _composeProject ? _composeProject.name : 'Composition';

  // ── Breadcrumb ──
  const _bcSep = '<span class="kanban-drill-sep"><svg width="10" height="10" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><polyline points="9 18 15 12 9 6"/></svg></span>';
  let html = '<div class="kanban-drill-titlebar">';
  html += '<div class="kanban-drill-breadcrumb">';
  html += '<span class="kanban-drill-crumb kanban-board-crumb" onclick="navigateToComposeBoard()"><svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><rect x="3" y="3" width="7" height="7"/><rect x="14" y="3" width="7" height="7"/><rect x="3" y="14" width="7" height="7"/><rect x="14" y="14" width="7" height="7"/></svg> Board</span>';
  if (section.parent_id) {
    const parentSec = _composeSections.find(s => s.id === section.parent_id);
    if (parentSec) {
      html += _bcSep;
      html += '<span class="kanban-drill-crumb" onclick="navigateToSection(\'' + parentSec.id + '\')" style="cursor:pointer;">' + (typeof escHtml === 'function' ? escHtml(parentSec.name) : parentSec.name) + '</span>';
    }
  }
  html += _bcSep;
  html += '<span class="kanban-drill-crumb current">' + (typeof escHtml === 'function' ? escHtml(section.name) : section.name) + '</span>';
  html += '</div></div>';

  // ── Detail body — left/right split ──
  html += '<div class="kanban-drill-body">';
  html += '<div class="kanban-drill-split">';

  // ════════════ LEFT: Section info ════════════
  html += '<div class="kanban-drill-left">';

  // Status badge (clickable)
  html += '<div class="kanban-drill-status kanban-status-clickable" style="background:' + statusOpt.color + '26;color:' + statusOpt.color + ';cursor:pointer;" onclick="event.stopPropagation();_composeStatusMenu(\'' + section.id + '\', \'' + section.status + '\', event)" title="Click to change status">' + statusOpt.label + ' <svg width="10" height="10" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><polyline points="9 18 15 12 9 6"/></svg></div>';

  // Title (click to edit)
  html += '<div class="kanban-drill-title" id="compose-drill-title" onclick="_composeStartTitleEdit(\'' + section.id + '\', this)" title="Click to edit">' + (typeof escHtml === 'function' ? escHtml(section.name) : section.name) + '</div>';

  // Artifact type badge
  html += '<div style="display:flex;align-items:center;gap:6px;margin:4px 0 16px;font-size:12px;color:var(--text-dim);">';
  html += '<span>' + artifactIcon + '</span>';
  html += '<span>' + (section.artifact_type || 'text') + '</span>';
  if (section.changing) {
    html += '<span style="color:var(--warning);margin-left:8px;" title="' + (typeof escHtml === 'function' ? escHtml(section.change_note || 'Change in progress') : (section.change_note || 'Change in progress')) + '">&#9679; changing</span>';
  }
  html += '</div>';

  // Summary / description area
  html += '<div class="kanban-drill-desc-wrap">';
  if (section.summary) {
    html += '<div class="kanban-drill-desc" style="min-height:60px;">' + (typeof escHtml === 'function' ? escHtml(section.summary) : section.summary) + '</div>';
  } else {
    html += '<div class="kanban-drill-desc" style="min-height:60px;color:var(--text-dim);font-style:italic;">No summary yet. The AI agent will update this as it works.</div>';
  }
  html += '</div>';

  // ── Output preview panel (collapsed by default) ──
  html += '<div class="compose-preview-panel" id="compose-preview-panel-' + sectionId + '">';
  html += '<div class="compose-preview-header" onclick="_composeTogglePreview(\'' + sectionId + '\')">';
  html += '<svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" id="compose-preview-chevron-' + sectionId + '" style="transition:transform 0.2s;transform:rotate(0deg);"><polyline points="9 18 15 12 9 6"/></svg>';
  html += '<span>Output</span>';
  html += '</div>';
  html += '<div class="compose-preview-body" id="compose-preview-body-' + sectionId + '" style="display:none;"></div>';
  html += '</div>';

  // ── Subsections list (children of this section) ──
  const _childSections = _composeSections.filter(c => c.parent_id === sectionId);
  if (_childSections.length > 0 || !section.parent_id) {
    html += '<div style="margin-top:20px;">';
    html += '<div class="kanban-drill-panel-header" style="display:flex;align-items:center;justify-content:space-between;">';
    html += '<span style="font-size:11px;font-weight:700;text-transform:uppercase;letter-spacing:0.8px;color:var(--text-dim);">Subsections</span>';
    const _childDone = _childSections.filter(c => c.status === 'complete').length;
    if (_childSections.length > 0) {
      const _childPct = Math.round((_childDone / _childSections.length) * 100);
      html += '<span class="kanban-drill-inline-progress"><span class="kanban-drill-inline-bar"><span class="kanban-drill-inline-fill" style="width:' + _childPct + '%"></span></span><span class="kanban-drill-inline-pct">' + _childPct + '%</span></span>';
    }
    html += '</div>';
    html += '<div class="kanban-drill-panel"><div class="kanban-drill-panel-body">';
    for (const child of _childSections) {
      const _cStatus = COMPOSE_STATUS_OPTIONS.find(o => o.key === child.status) || COMPOSE_STATUS_OPTIONS[0];
      html += '<div class="kanban-drill-subtask-row" data-section-id="' + child.id + '" style="cursor:pointer;" onclick="navigateToSection(\'' + child.id + '\')">';
      html += '<div class="kanban-drill-subtask-status" style="background:' + _cStatus.color + '26;color:' + _cStatus.color + ';">' + _cStatus.label + '</div>';
      html += '<span class="kanban-drill-subtask-title">' + (typeof escHtml === 'function' ? escHtml(child.name) : child.name) + '</span>';
      // Grandchild count
      const _gcCount = _composeSections.filter(gc => gc.parent_id === child.id).length;
      if (_gcCount > 0) {
        html += '<span class="kanban-drill-subtask-meta">' + _gcCount + ' subsection' + (_gcCount !== 1 ? 's' : '') + '</span>';
      }
      html += '<span class="kanban-drill-subtask-chevron"><svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><polyline points="9 18 15 12 9 6"/></svg></span>';
      html += '</div>';
    }
    // Add Subsection button
    html += '<div class="kanban-drill-subtask-row kanban-drill-ghost-row" onclick="composeAddSection(\'' + sectionId + '\')" style="cursor:pointer;justify-content:center;">';
    html += '<span style="font-size:12px;color:var(--text-dim);">+ Add Subsection</span>';
    html += '</div>';
    html += '</div></div>';
    html += '</div>';
  }

  html += '</div>'; // drill-left

  // ════════════ RIGHT: Session panel ════════════
  html += '<div class="kanban-drill-right">';

  if (section.session_id) {
    // Session exists — show it
    const sess = (typeof allSessions !== 'undefined') ? allSessions.find(s => s.id === section.session_id) : null;
    const sessTitle = sess ? (sess.custom_title || sess.display_title || 'Session') : 'Session';
    const isRunning = (typeof runningIds !== 'undefined') ? runningIds.has(section.session_id) : false;

    html += '<div style="font-size:11px;font-weight:700;text-transform:uppercase;letter-spacing:0.8px;color:var(--text-dim);margin-bottom:8px;">Session</div>';
    html += '<div class="kanban-drill-panel"><div class="kanban-drill-panel-body">';
    const _eSid = (typeof escHtml === 'function' ? escHtml(section.session_id) : section.session_id);
    const _eSessTitle = (typeof escHtml === 'function' ? escHtml(sessTitle) : sessTitle);
    html += '<div class="kanban-drill-subtask-row" style="cursor:pointer;" onclick="_composeOpenSession(\'' + _eSid + '\')">';
    html += '<span class="kanban-drill-subtask-status" style="background:' + (isRunning ? 'var(--green)26' : 'var(--bg-subtle)') + ';color:' + (isRunning ? 'var(--green)' : 'var(--text-dim)') + ';">' + (isRunning ? 'running' : 'idle') + '</span>';
    html += '<span class="kanban-drill-subtask-title">' + _eSessTitle + '</span>';
    html += '<div class="kanban-drill-subtask-actions" style="margin-left:auto;display:flex;gap:4px;" onclick="event.stopPropagation();">';
    html += '<button class="kanban-drill-action-btn" title="Rename" onclick="_composeRenameSession(\'' + _eSid + '\')"><svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><path d="M11 4H4a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h14a2 2 0 0 0 2-2v-7"/><path d="M18.5 2.5a2.121 2.121 0 0 1 3 3L12 15l-4 1 1-4 9.5-9.5z"/></svg></button>';
    html += '<button class="kanban-drill-action-btn" title="Unlink" onclick="_composeUnlinkSession(\'' + section.id + '\',\'' + _eSid + '\')"><svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><line x1="18" y1="6" x2="6" y2="18"/><line x1="6" y1="6" x2="18" y2="18"/></svg></button>';
    html += '</div>';
    html += '<span class="kanban-drill-subtask-chevron"><svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><polyline points="9 18 15 12 9 6"/></svg></span>';
    html += '</div>';
    html += '</div></div>';
  } else {
    // No session yet — show chooser
    html += '<div class="kanban-drill-chooser">';
    html += '<div style="font-size:12px;color:var(--text-dim);margin-bottom:6px;text-transform:uppercase;letter-spacing:0.5px;font-weight:600;">How to proceed</div>';

    html += '<div class="kanban-drill-chooser-card" onclick="_composeSpawnSession(\'' + section.id + '\')">';
    html += '<div class="kanban-drill-chooser-icon" style="color:var(--green);"><svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><polygon points="13 2 3 14 12 14 11 22 21 10 12 10 13 2"/></svg></div>';
    html += '<div><div class="kanban-drill-chooser-title">Spawn session</div>';
    html += '<div class="kanban-drill-chooser-desc">Start an AI agent scoped to this section. It will read the shared context and work on ' + (section.artifact_type || 'text') + ' output.</div></div>';
    html += '</div>';

    html += '<div class="kanban-drill-chooser-card" onclick="_composeLinkSession(\'' + section.id + '\')">';
    html += '<div class="kanban-drill-chooser-icon" style="color:var(--accent);"><svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><path d="M10 13a5 5 0 0 0 7.54.54l3-3a5 5 0 0 0-7.07-7.07l-1.72 1.71"/><path d="M14 11a5 5 0 0 0-7.54-.54l-3 3a5 5 0 0 0 7.07 7.07l1.71-1.71"/></svg></div>';
    html += '<div><div class="kanban-drill-chooser-title">Link existing session</div>';
    html += '<div class="kanban-drill-chooser-desc">Attach a session that\'s already running.</div></div>';
    html += '</div>';

    html += '<div class="kanban-drill-chooser-card" onclick="_openComposePlanner(\'' + section.id + '\')">';
    html += '<div class="kanban-drill-chooser-icon" style="color:var(--blue, #58a6ff);"><svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><path d="M12 2a7 7 0 0 1 7 7c0 2.38-1.19 4.47-3 5.74V17a1 1 0 0 1-1 1H9a1 1 0 0 1-1-1v-2.26C6.19 13.47 5 11.38 5 9a7 7 0 0 1 7-7z"/><line x1="9" y1="21" x2="15" y2="21"/><line x1="10" y1="24" x2="14" y2="24"/></svg></div>';
    html += '<div><div class="kanban-drill-chooser-title">Plan subsections with AI</div>';
    html += '<div class="kanban-drill-chooser-desc">Break this section into subsections using the AI planner.</div></div>';
    html += '</div>';

    html += '</div>';
  }

  html += '</div>'; // drill-right
  html += '</div>'; // drill-split
  html += '</div>'; // drill-body

  board.innerHTML = html;
}

// --- Compose status menu (mirrors kanban showStatusMenu) ---
function _composeStatusMenu(sectionId, currentStatus, event) {
  // Remove existing menu
  const old = document.querySelector('.kanban-status-dropdown');
  if (old) old.remove();

  const menu = document.createElement('div');
  menu.className = 'kanban-status-dropdown';
  menu.style.position = 'fixed';
  menu.style.left = event.clientX + 'px';
  menu.style.top = event.clientY + 'px';
  menu.style.zIndex = '9999';

  for (const opt of COMPOSE_STATUS_OPTIONS) {
    const item = document.createElement('div');
    item.className = 'kanban-status-option' + (opt.key === currentStatus ? ' active' : '');
    item.innerHTML = '<span class="kanban-status-dot" style="background:' + opt.color + ';"></span> ' + opt.label;
    item.onclick = async () => {
      menu.remove();
      try {
        await fetch('/api/compose/projects/' + _composeProject.id + '/sections/' + sectionId + '/status', {
          method: 'PUT',
          headers: {'Content-Type': 'application/json'},
          body: JSON.stringify({status: opt.key}),
        });
        // Update local state and re-render
        const sec = _composeSections.find(s => s.id === sectionId);
        if (sec) sec.status = opt.key;
        renderSectionDetail(sectionId);
      } catch (e) {
        console.error('Failed to update status:', e);
        if (typeof showToast === 'function') showToast('Failed to update status', 'error');
      }
    };
    menu.appendChild(item);
  }

  document.body.appendChild(menu);
  const close = (e) => { if (!menu.contains(e.target)) { menu.remove(); document.removeEventListener('click', close); } };
  setTimeout(() => document.addEventListener('click', close), 0);
}

// --- Compose inline title edit ---
function _composeStartTitleEdit(sectionId, el) {
  const section = _composeSections.find(s => s.id === sectionId);
  if (!section) return;
  const current = section.name;
  el.innerHTML = '<input type="text" class="kanban-drill-title-input" value="' + (typeof escHtml === 'function' ? escHtml(current) : current) + '" style="width:100%;font-size:inherit;font-weight:inherit;font-family:inherit;background:var(--bg-subtle);border:1px solid var(--border);border-radius:6px;padding:4px 8px;color:var(--text);outline:none;">';
  const input = el.querySelector('input');
  input.focus();
  input.select();
  const save = async () => {
    const newName = input.value.trim();
    if (!newName || newName === current) {
      el.textContent = current;
      return;
    }
    el.textContent = newName;
    try {
      await fetch('/api/compose/projects/' + _composeProject.id + '/sections/' + sectionId, {
        method: 'PUT',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({name: newName}),
      });
      section.name = newName;
      if (typeof showToast === 'function') showToast('Renamed section');
    } catch (e) {
      el.textContent = current;
      if (typeof showToast === 'function') showToast('Failed to rename', 'error');
    }
  };
  input.onblur = save;
  input.onkeydown = (e) => { if (e.key === 'Enter') { e.preventDefault(); input.blur(); } if (e.key === 'Escape') { el.textContent = current; } };
}

// --- Compose session spawning (via /launch endpoint) ---
async function _composeSpawnSession(sectionId) {
  const section = _composeSections.find(s => s.id === sectionId);
  if (!section || !_composeProject) return;

  if (typeof showToast === 'function') showToast('Launching session for: ' + section.name);

  try {
    const resp = await fetch('/api/compose/projects/' + encodeURIComponent(_composeProject.id) + '/sections/' + sectionId + '/launch', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
    });
    const data = await resp.json();
    if (data.ok && data.session_id) {
      section.session_id = data.session_id;
      _composeSelectedSection = sectionId;
      composeDetailTaskId = 'section:' + _composeProject.id + ':' + sectionId;
      if (typeof showToast === 'function') showToast('Session started for: ' + section.name);
      if (_composeSelectedSection === sectionId) renderSectionDetail(sectionId);
      _renderComposeSectionCards();
    } else {
      if (typeof showToast === 'function') showToast(data.error || 'Failed to launch session', true);
    }
  } catch (e) {
    if (typeof showToast === 'function') showToast('Failed to launch session', true);
  }
}

// --- Open existing compose session ---
function _composeOpenSession(sessionId) {
  if (typeof openInGUI === 'function') {
    openInGUI(sessionId);
  } else if (typeof selectSession === 'function') {
    selectSession(sessionId);
  }
}

// Open a session inside compose view (mirrors _openSessionInKanban)
function _openSessionInCompose(sessionId) {
  const s = (typeof allSessions !== 'undefined') ? allSessions.find(x => x.id === sessionId) : null;
  const sessionName = s ? (s.custom_title || s.display_title || 'Session') : 'Session';
  const sectionId = _composeSelectedSection || null;
  const section = sectionId ? _composeSections.find(x => x.id === sectionId) : null;
  const sectionTitle = section ? (section.title || 'Section') : '';

  // Hide compose board, show main-body
  const cb = document.getElementById('compose-board');
  if (cb) cb.style.display = 'none';
  const mb = document.getElementById('main-body');
  if (mb) mb.style.display = '';

  // Remove old crumb bar
  const old = document.getElementById('compose-session-bar');
  if (old) old.remove();

  // Build breadcrumb bar
  const _esc = typeof escHtml === 'function' ? escHtml : (x => x);
  let crumbHtml = '<div class="kanban-drill-titlebar" id="compose-session-bar">';
  crumbHtml += '<div class="kanban-drill-breadcrumb">';
  crumbHtml += '<span class="kanban-drill-crumb kanban-board-crumb" onclick="_composeSessionClose(\'board\')"><svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><line x1="3" y1="12" x2="21" y2="12"/><line x1="3" y1="6" x2="21" y2="6"/><line x1="3" y1="18" x2="21" y2="18"/></svg> Compose</span>';
  if (sectionId && sectionTitle) {
    crumbHtml += '<span class="kanban-drill-sep"><svg width="10" height="10" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><polyline points="9 18 15 12 9 6"/></svg></span>';
    crumbHtml += '<span class="kanban-drill-crumb" onclick="_composeSessionClose(\'' + _esc(sectionId) + '\')">' + _esc(sectionTitle) + '</span>';
  }
  crumbHtml += '<span class="kanban-drill-sep"><svg width="10" height="10" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><polyline points="9 18 15 12 9 6"/></svg></span>';
  crumbHtml += '<span class="kanban-drill-crumb current">' + _esc(sessionName) + '</span>';
  crumbHtml += '</div>';
  crumbHtml += '<div class="kanban-drill-actions">';
  crumbHtml += '<span class="btn-group-label" onclick="openActionsPopup()">Actions</span>';
  crumbHtml += '</div>';
  crumbHtml += '</div>';

  if (mb) mb.insertAdjacentHTML('beforebegin', crumbHtml);

  window._composeSessionSectionId = sectionId;

  // Open the session in the live panel
  _guiFocusPending = true;
  activeId = sessionId;
  localStorage.setItem('activeSessionId', sessionId);
  if (typeof runningIds !== 'undefined' && runningIds.has(sessionId)) guiOpenAdd(sessionId);
  if (typeof liveSessionId !== 'undefined' && liveSessionId && liveSessionId !== sessionId) { stopLivePanel(); }
  filterSessions();
}

// Rename a session linked in compose
function _composeRenameSession(sessionId) {
  const s = (typeof allSessions !== 'undefined') ? allSessions.find(x => x.id === sessionId) : null;
  const current = s ? (s.custom_title || s.display_title || '') : '';
  const newName = prompt('Rename session:', current);
  if (newName === null || !newName.trim()) return;
  const proj = localStorage.getItem('activeProject') || '';
  fetch('/api/rename/' + sessionId, {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({title: newName.trim(), project: proj})
  }).then(r => r.json()).then(data => {
    if (data.ok) {
      if (s) { s.custom_title = newName.trim(); s.display_title = newName.trim(); }
      if (typeof showToast === 'function') showToast('Session renamed');
      // Re-render section detail to show new name
      if (_composeSelectedSection) renderSectionDetail(_composeSelectedSection);
    }
  }).catch(() => { if (typeof showToast === 'function') showToast('Rename failed', true); });
}

// Unlink a session from a compose section
function _composeUnlinkSession(sectionId, sessionId) {
  if (!confirm('Unlink this session from the section? The session will still exist in the sessions view.')) return;
  const projId = _composeProject ? _composeProject.id : '';
  fetch('/api/compose/projects/' + encodeURIComponent(projId) + '/sections/' + sectionId, {
    method: 'PUT',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({session_id: ''})
  }).then(r => r.json()).then(data => {
    if (data.ok || data.section) {
      // Update local state
      const sec = _composeSections.find(x => x.id === sectionId);
      if (sec) sec.session_id = '';
      if (typeof showToast === 'function') showToast('Session unlinked');
      renderSectionDetail(sectionId);
    }
  }).catch(() => { if (typeof showToast === 'function') showToast('Unlink failed', true); });
}

// Close session view and return to compose board
function _composeSessionClose(target) {
  if (typeof liveSessionId !== 'undefined' && liveSessionId) { if (typeof stopLivePanel === 'function') stopLivePanel(); }
  activeId = null;
  if (typeof liveSessionId !== 'undefined') liveSessionId = null;
  window._composeSessionSectionId = null;
  localStorage.removeItem('activeSessionId');
  // Remove crumb bar
  const bar = document.getElementById('compose-session-bar');
  if (bar) bar.remove();
  // Restore panels
  const mb = document.getElementById('main-body');
  if (mb) mb.style.display = 'none';
  const cb = document.getElementById('compose-board');
  if (cb) cb.style.display = '';
  // Navigate back
  if (target === 'board') {
    navigateToComposeBoard();
  } else {
    renderSectionDetail(target);
  }
}

// --- Restore compose view from hash ---
function _restoreComposeSectionFromHash() {
  const hash = window.location.hash || '';
  if (hash.startsWith('#compose/section/')) {
    const sectionId = hash.replace('#compose/section/', '');
    if (sectionId && _composeSections.find(s => s.id === sectionId)) {
      renderSectionDetail(sectionId);
      return true;
    }
  }
  return false;
}

// --- Compose card context menu (right-click or dot-menu) ---

function _composeCardContextMenu(sectionId, event) {
  event.stopPropagation();
  // Close any existing menu
  if (typeof closeContextMenu === 'function') closeContextMenu();

  const section = _composeSections.find(s => s.id === sectionId);
  if (!section || !_composeProject) return;

  const menu = document.createElement('div');
  menu.className = 'kanban-context-menu';

  if (event.type === 'contextmenu') {
    menu.style.top = event.clientY + 'px';
    menu.style.left = event.clientX + 'px';
  } else {
    const rect = event.currentTarget.getBoundingClientRect();
    menu.style.top = rect.bottom + 'px';
    menu.style.left = rect.left + 'px';
  }

  let items = '';
  items += '<div class="kanban-context-item" onclick="closeContextMenu();navigateToSection(\'' + sectionId + '\')">Open</div>';
  items += '<div class="kanban-context-item" onclick="closeContextMenu();_composeRenameSection(\'' + sectionId + '\')">Rename</div>';
  items += '<div class="kanban-context-item" onclick="closeContextMenu();composeAddSection(\'' + sectionId + '\')">Add Subsection</div>';

  if (section.session_id) {
    items += '<div class="kanban-context-item" onclick="closeContextMenu();_composeOpenSession(\'' + section.session_id + '\')">Open Session</div>';
  } else {
    items += '<div class="kanban-context-item" onclick="closeContextMenu();_composeSpawnSession(\'' + sectionId + '\')">Spawn Session</div>';
    items += '<div class="kanban-context-item" onclick="closeContextMenu();_composeLinkSession(\'' + sectionId + '\')">Link Session</div>';
  }

  // Move to status
  items += '<div class="kanban-context-separator"></div>';
  for (const opt of COMPOSE_STATUS_OPTIONS) {
    if (opt.key !== section.status) {
      items += '<div class="kanban-context-item kanban-context-move" onclick="closeContextMenu();_composeMoveSection(\'' + sectionId + '\',\'' + opt.key + '\')">Move to ' + opt.label + '</div>';
    }
  }

  items += '<div class="kanban-context-separator"></div>';
  items += '<div class="kanban-context-item kanban-context-danger" onclick="closeContextMenu();_composeDeleteSection(\'' + sectionId + '\')">Delete</div>';

  menu.innerHTML = items;
  document.body.appendChild(menu);

  setTimeout(() => {
    document.addEventListener('click', closeContextMenu, { once: true });
  }, 0);
}

function _composeRenameSection(sectionId) {
  const section = _composeSections.find(s => s.id === sectionId);
  if (!section || !_composeProject) return;
  const newName = prompt('Rename section:', section.name);
  if (!newName || !newName.trim() || newName.trim() === section.name) return;
  fetch('/api/compose/projects/' + _composeProject.id + '/sections/' + sectionId, {
    method: 'PUT',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({name: newName.trim()}),
  }).then(r => r.json()).then(data => {
    if (data && data.ok) {
      section.name = newName.trim();
      _renderComposeSectionCards();
      showToast('Renamed section');
    } else {
      showToast(data.error || 'Failed to rename', 'error');
    }
  }).catch(() => showToast('Failed to rename', 'error'));
}

async function _composeMoveSection(sectionId, newStatus) {
  const section = _composeSections.find(s => s.id === sectionId);
  if (!section || !_composeProject) return;
  try {
    const resp = await fetch('/api/compose/projects/' + _composeProject.id + '/sections/' + sectionId + '/status', {
      method: 'PUT',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({status: newStatus}),
    });
    if (!resp.ok) throw new Error('Failed');
    section.status = newStatus;
    _renderComposeSectionCards();
    const label = (COMPOSE_STATUS_OPTIONS.find(o => o.key === newStatus) || {}).label || newStatus;
    showToast('Moved to ' + label);
  } catch (e) {
    showToast('Failed to move section', 'error');
  }
}

async function _composeDeleteSection(sectionId) {
  const section = _composeSections.find(s => s.id === sectionId);
  const title = section ? section.name : 'this section';

  // Check for children — if any, show cascade confirmation modal
  if (_composeProject) {
    try {
      const resp = await fetch('/api/compose/projects/' + encodeURIComponent(_composeProject.id) + '/sections/' + sectionId + '/children');
      const data = await resp.json();
      if (data.count > 0) {
        _showCascadeDeleteModal(sectionId, title, data.children || [], data.count);
        return;
      }
    } catch (e) { /* fall through to inline confirm */ }
  }

  // No children — use inline confirm on the card
  const card = document.querySelector('.compose-card[data-section-id="' + sectionId + '"]');
  if (card) {
    const old = card.innerHTML;
    card.innerHTML = '<div class="kanban-delete-confirm"><span>Delete "' + escHtml(title.slice(0, 30)) + '"?</span><div class="kanban-delete-btns"><button class="kanban-delete-yes" onclick="event.stopPropagation();_execComposeDelete(\'' + sectionId + '\')">Delete</button><button class="kanban-delete-no" onclick="event.stopPropagation();_cancelComposeDelete(this,\'' + sectionId + '\')">Cancel</button></div></div>';
    card._oldHtml = old;
    card.onclick = null;
  } else {
    _execComposeDelete(sectionId);
  }
}

function _showCascadeDeleteModal(sectionId, title, children, count) {
  const _esc = typeof escHtml === 'function' ? escHtml : (x => x);
  const overlay = document.createElement('div');
  overlay.className = 'pm-overlay';
  overlay.style.cssText = 'display:flex;position:fixed;inset:0;background:rgba(0,0,0,0.6);backdrop-filter:blur(4px);z-index:5000;align-items:center;justify-content:center;';
  overlay.onclick = (e) => { if (e.target === overlay) overlay.remove(); };
  let childList = '';
  for (const c of children) {
    childList += '<li>' + _esc(c.name || c.id) + '</li>';
  }
  overlay.innerHTML = '<div class="pm-card compose-cascade-modal">' +
    '<div class="pm-title">Delete section and ' + count + ' subsection' + (count !== 1 ? 's' : '') + '?</div>' +
    '<div class="pm-body">' +
    '<p style="font-size:13px;color:var(--text-secondary);margin:0 0 8px;">Deleting <strong>' + _esc(title) + '</strong> will also remove:</p>' +
    '<ul class="cascade-children-list">' + childList + '</ul>' +
    '<div class="cascade-warning">This cannot be undone.</div>' +
    '</div>' +
    '<div class="pm-actions">' +
    '<button class="pm-btn" onclick="this.closest(\'.pm-overlay\').remove()">Cancel</button>' +
    '<button class="pm-btn" style="background:#ef4444;color:#fff;border-color:#ef4444;" onclick="this.closest(\'.pm-overlay\').remove();_execCascadeDelete(\'' + sectionId + '\')">Delete All</button>' +
    '</div></div>';
  document.body.appendChild(overlay);
}

async function _execCascadeDelete(sectionId) {
  if (!_composeProject) return;
  try {
    const resp = await fetch('/api/compose/projects/' + encodeURIComponent(_composeProject.id) + '/sections/' + sectionId + '?cascade=true', { method: 'DELETE' });
    if (!resp.ok) throw new Error('Delete failed');
    // Remove section and all its descendants from local state
    const toRemove = new Set([sectionId]);
    let changed = true;
    while (changed) {
      changed = false;
      for (const s of _composeSections) {
        if (s.parent_id && toRemove.has(s.parent_id) && !toRemove.has(s.id)) {
          toRemove.add(s.id);
          changed = true;
        }
      }
    }
    _composeSections = _composeSections.filter(s => !toRemove.has(s.id));
    if (_composeSelectedSection && toRemove.has(_composeSelectedSection)) {
      _composeSelectedSection = null;
      _renderComposeBoard();
    } else {
      _renderComposeSectionCards();
    }
    _updateComposeRootHeader();
    showToast('Section and subsections deleted');
  } catch (e) {
    showToast('Failed to delete section', true);
  }
}

function _cancelComposeDelete(btn, sectionId) {
  const card = document.querySelector('.compose-card[data-section-id="' + sectionId + '"]');
  if (card && card._oldHtml) {
    card.innerHTML = card._oldHtml;
    card.onclick = () => navigateToSection(sectionId);
  }
}

async function _execComposeDelete(sectionId) {
  if (!_composeProject) return;
  const card = document.querySelector('.compose-card[data-section-id="' + sectionId + '"]');
  if (card) {
    const col = card.closest('.compose-column');
    card.remove();
    if (col) {
      const countEl = col.querySelector('.kanban-column-count');
      if (countEl) countEl.textContent = col.querySelectorAll('.kanban-card').length;
    }
  }
  try {
    const resp = await fetch('/api/compose/projects/' + _composeProject.id + '/sections/' + sectionId, { method: 'DELETE' });
    if (!resp.ok) throw new Error('Delete failed');
    _composeSections = _composeSections.filter(s => s.id !== sectionId);
    if (_composeSelectedSection === sectionId) _composeSelectedSection = null;
    showToast('Section deleted');
    _updateComposeRootHeader();
  } catch (e) {
    showToast('Failed to delete section', 'error');
    initCompose();
  }
}

function _updateComposeInputTarget() {
  const nameEl = document.getElementById('compose-input-target-name');
  if (!nameEl) return;

  if (_composeSelectedSection) {
    const section = _composeSections.find(s => s.id === _composeSelectedSection);
    nameEl.textContent = section ? section.name : 'unknown section';
    composeDetailTaskId = 'section:' + _composeProject.id + ':' + _composeSelectedSection;
  } else {
    nameEl.textContent = _composeProject ? _composeProject.name + ' (root)' : 'composition';
    composeDetailTaskId = _composeProject ? 'root:' + _composeProject.id : null;
  }
}

/**
 * Select a compose section (updates input target and compose_task_id).
 * Pass null to target the root orchestrator.
 */
function composeSelectSection(sectionId) {
  _composeSelectedSection = sectionId;
  _updateComposeInputTarget();
}

/**
 * Reset compose state — called when switching away from compose view.
 */
function resetComposeState() {
  _composeProject = null;
  _composeSections = [];
  _composeConflicts = [];
  composeDetailTaskId = null;
  _composeSelectedSection = null;
  _activeComposeProjectId = null;
  _composeProjectsList = [];
  _composeInitToken++;  // cancel any in-flight initCompose()
  _composeSelected = new Set();
  _composeLastClickedId = null;
  _composeSearchFilter = '';
  _composeFlushPendingDeletes();
  _composeFocusedId = null;
  _composeActionHistory = [];
  const header = document.getElementById('compose-root-header');
  const target = document.getElementById('compose-input-target');
  if (header) header.style.display = 'none';
  if (target) target.style.display = 'none';
}

/**
 * Group compose sessions in the sidebar under composition name.
 * Called during session list rendering when in compose mode.
 */
// Socket event handlers for compose updates
function _composeOnBoardRefresh(data) {
  if (viewMode === 'compose') initCompose();
}

function _composeOnTaskCreated(data) {
  if (viewMode === 'compose') initCompose();
}

function _composeOnTaskUpdated(data) {
  if (viewMode === 'compose') initCompose();
}

function _composeOnTaskMoved(data) {
  if (viewMode === 'compose') initCompose();
}

function _composeOnContextUpdated(data) {
  if (viewMode !== 'compose') return;
  if (!_composeProject || !data) return;
  const ctx = data.context;
  if (!ctx) return;
  // Update sections from context
  if (ctx.sections) {
    _composeSections = ctx.sections;
  }
  if (ctx.conflicts) {
    _composeConflicts = ctx.conflicts.filter(c => c.status === 'pending');
  }
  // If the selected section was deleted, fall back to root
  if (_composeSelectedSection && !_composeSections.find(s => s.id === _composeSelectedSection)) {
    _composeSelectedSection = null;
    _renderComposeBoard();
    return;
  }
  // If in drill-down, re-render the detail view; otherwise re-render the board
  if (_composeSelectedSection) {
    renderSectionDetail(_composeSelectedSection);
  } else {
    _updateComposeRootHeader();
    _updateComposeInputTarget();
    _renderComposeSectionCards();
  }
}

function _composeOnChanging(data) {
  if (viewMode !== 'compose') return;
  // Update the section in local state and re-render
  if (data && data.section_id) {
    const sec = _composeSections.find(s => s.id === data.section_id);
    if (sec) {
      sec.changing = data.changing;
      sec.change_note = data.change_note || null;
    }
    // If viewing this section's detail, re-render it; otherwise re-render cards
    if (_composeSelectedSection === data.section_id) {
      renderSectionDetail(data.section_id);
    } else if (!_composeSelectedSection) {
      _renderComposeSectionCards();
    }
  }
}

function getComposeSessionGroups(sessions) {
  if (!_composeProject) return null;

  const groups = [];
  const rootSessionId = _composeProject.root_session_id;
  const sectionSessionIds = new Set(
    _composeSections
      .filter(s => s.session_id)
      .map(s => s.session_id)
  );

  const composeSessions = sessions.filter(s =>
    s.id === rootSessionId || sectionSessionIds.has(s.id)
  );
  const otherSessions = sessions.filter(s =>
    s.id !== rootSessionId && !sectionSessionIds.has(s.id)
  );

  if (composeSessions.length > 0) {
    // Root session first
    const root = composeSessions.find(s => s.id === rootSessionId);
    const sections = composeSessions.filter(s => s.id !== rootSessionId);
    groups.push({
      name: _composeProject.name,
      root: root || null,
      sections: sections,
    });
  }

  return { groups, other: otherSessions };
}
