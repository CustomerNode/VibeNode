/* app.js — global state, project loading, session loading */

let allSessions = [];
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

async function setProject(encoded, reload = true) {
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
  if (typeof FOLDER_SUPERSET === 'object' && FOLDER_SUPERSET) {
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

async function loadSessions() {
  showSkeletonLoader();
  // Load workforce assets from disk first (replaces FOLDER_SUPERSET before folder tree reads it)
  // Then load sessions and folder tree in parallel
  if (typeof _loadWorkforceFromDisk === 'function') {
    try { await _loadWorkforceFromDisk(); } catch(e) {}
  }
  // Pass activeProject so the server syncs _active_project before processing.
  // Closes the race where this HTTP fetch arrives before the WebSocket
  // reconnects and sends request_state_snapshot with the project context.
  const _projParam = localStorage.getItem('activeProject') || '';
  const _sessUrl = _projParam ? '/api/sessions?project=' + encodeURIComponent(_projParam) : '/api/sessions';
  const [resp] = await Promise.all([
    fetch(_sessUrl),
    (typeof initFolderTree === 'function') ? initFolderTree().catch(function(){}) : Promise.resolve(),
  ]);
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

/**
 * Initialize the compose board — fetch project data and render header.
 * Called by setViewMode('compose') in workforce.js.
 */
async function initCompose() {
  try {
    const _proj = localStorage.getItem('activeProject') || '';
    const _projQ = _proj ? '?project=' + encodeURIComponent(_proj) : '';
    const resp = await fetch('/api/compose/board' + _projQ);
    const data = await resp.json();
    if (!data || !data.project) {
      _renderComposeEmpty();
      return;
    }
    _composeProject = data.project;
    _composeSections = data.sections || [];
    _composeConflicts = (data.conflicts || []).filter(c => c.status === 'pending');

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

  _renderComposeSidebar();
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

function composeAddSection() {
  if (!_composeProject) return;
  const overlay = document.getElementById('pm-overlay');
  if (!overlay) return;
  _composeInsertPosition = 'top';
  _composeArtifactType = 'text';

  overlay.innerHTML = `<div class="pm-card pm-enter" style="max-width:480px;">
    <h2 class="pm-title" style="display:flex;align-items:center;justify-content:space-between;">
      <span>Add Section</span>
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

  // Optimistic: insert a ghost card into the not_started column
  const col = document.querySelector('.compose-column[data-status="not_started"] .kanban-column-body');
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
      body: JSON.stringify({name, artifact_type: artifactType, insert_position: insertPos}),
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

  let html = '<div class="kanban-sidebar-section">';
  html += '<div class="kanban-sidebar-label">Compose</div>';

  if (_composeProject) {
    html += '<button class="kanban-sidebar-btn" onclick="composeAddSection()"><svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><line x1="12" y1="5" x2="12" y2="19"/><line x1="5" y1="12" x2="19" y2="12"/></svg> New Section</button>';
    html += '<button class="kanban-sidebar-btn" onclick="initCompose()"><svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><polyline points="23 4 23 10 17 10"/><path d="M20.49 15a9 9 0 1 1-2.12-9.36L23 10"/></svg> Refresh</button>';
  } else {
    html += '<button class="kanban-sidebar-btn" onclick="composeCreateProject()"><svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><line x1="12" y1="5" x2="12" y2="19"/><line x1="5" y1="12" x2="19" y2="12"/></svg> New Composition</button>';
  }

  html += '</div>';
  sidebar.innerHTML = html;

  // Permission aggregator
  const permPanel = document.getElementById('sidebar-perm-panel');
  if (permPanel && typeof _buildPermissionPanel === 'function') {
    permPanel.innerHTML = _buildPermissionPanel();
    permPanel.style.display = '';
  }
}

// --- Compose keyboard shortcuts ---

let _composeShortcutsAttached = false;

function attachComposeShortcuts() {
  if (_composeShortcutsAttached) return;
  _composeShortcutsAttached = true;

  document.addEventListener('keydown', (e) => {
    if (typeof viewMode !== 'undefined' && viewMode !== 'compose') return;
    const tag = (e.target.tagName || '').toLowerCase();
    if (tag === 'input' || tag === 'textarea' || tag === 'select' || e.target.isContentEditable) return;
    if (e.ctrlKey || e.metaKey || e.key === 'F5') return;

    switch (e.key) {
      case 'n': e.preventDefault(); if (_composeProject) composeAddSection(); else composeCreateProject(); break;
      case 'r': e.preventDefault(); initCompose(); if (typeof showToast === 'function') showToast('Refreshed'); break;
      case 'Escape': if (_composeSelectedSection) { e.preventDefault(); navigateToComposeBoard(); } break;
    }
  });
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
    const working = _composeSections.filter(s => s.status === 'working').length;
    let parts = [total + ' section' + (total !== 1 ? 's' : '')];
    if (complete > 0) parts.push(complete + ' complete');
    if (working > 0) parts.push(working + ' in progress');
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
}

// --- NB-11: Render section cards in compose board ---

const COMPOSE_STATUS_COLUMNS = [
  { key: 'not_started', label: 'Not Started', color: '#8b949e' },
  { key: 'working',     label: 'Working',     color: '#4ecdc4' },
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
        <div style="font-size:12px;color:var(--text-muted);margin-bottom:14px;">Add a section to break your composition into parts.</div>
        <button class="kanban-create-first-btn" onclick="composeAddSection()">+ Add Section</button>
      </div>`;
    return;
  }

  let html = '<div class="kanban-columns-wrapper compose-columns-wrapper">';

  for (const col of COMPOSE_STATUS_COLUMNS) {
    const colSections = _composeSections.filter(s => s.status === col.key);
    html += `<div class="kanban-column compose-column" data-status="${col.key}">
      <div class="kanban-column-header">
        <div class="kanban-column-color-bar" style="background:${col.color};"></div>
        <span class="kanban-column-name">${col.label}</span>
        <span class="kanban-column-count">${colSections.length}</span>
      </div>
      <div class="kanban-column-body" data-status="${col.key}">`;

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
                   onclick="navigateToSection('${sec.id}')"
                   oncontextmenu="event.preventDefault();event.stopPropagation();_composeCardContextMenu('${sec.id}', event)">
        <div class="compose-card-header">
          <span class="compose-card-artifact-icon">${artifactIcon}</span>
          <div class="compose-card-title-row">
            <span class="compose-card-title">${typeof escHtml === 'function' ? escHtml(sec.name) : sec.name}</span>
            ${changingDot}
          </div>
          <span class="kanban-context-btn" onclick="event.stopPropagation();_composeCardContextMenu('${sec.id}', event)" title="Actions">&#8943;</span>
        </div>
        <div class="compose-card-meta">
          <span class="compose-card-status" style="background:${col.color}22;color:${col.color};">${col.label}</span>
          ${sec.artifact_type ? '<span class="compose-card-time">' + sec.artifact_type + '</span>' : ''}
        </div>
        ${summary}
      </div>`;
    }

    html += '</div></div>';
  }

  html += '</div>';
  board.innerHTML = html;
}

// --- End NB-11 ---

// ═══════════════════════════════════════════════════════════════
// COMPOSE SECTION DETAIL — Drill-down view (mirrors kanban task detail)
// ═══════════════════════════════════════════════════════════════

const COMPOSE_STATUS_OPTIONS = [
  { key: 'not_started', label: 'Not Started', color: '#8b949e' },
  { key: 'working',     label: 'Working',     color: '#4ecdc4' },
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
  let html = '<div class="kanban-drill-titlebar">';
  html += '<div class="kanban-drill-breadcrumb">';
  html += '<span class="kanban-drill-crumb kanban-board-crumb" onclick="navigateToComposeBoard()"><svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><rect x="3" y="3" width="7" height="7"/><rect x="14" y="3" width="7" height="7"/><rect x="3" y="14" width="7" height="7"/><rect x="14" y="14" width="7" height="7"/></svg> Board</span>';
  html += '<span class="kanban-drill-sep"><svg width="10" height="10" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><polyline points="9 18 15 12 9 6"/></svg></span>';
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
    html += '<div class="kanban-drill-subtask-row" style="cursor:pointer;" onclick="_composeOpenSession(\'' + section.session_id + '\')">';
    html += '<span class="kanban-drill-subtask-status" style="background:' + (isRunning ? 'var(--green)26' : 'var(--bg-subtle)') + ';color:' + (isRunning ? 'var(--green)' : 'var(--text-dim)') + ';">' + (isRunning ? 'running' : 'idle') + '</span>';
    html += '<span class="kanban-drill-subtask-title">' + (typeof escHtml === 'function' ? escHtml(sessTitle) : sessTitle) + '</span>';
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

// --- Compose session spawning ---
async function _composeSpawnSession(sectionId) {
  if (typeof addNewAgent !== 'function') {
    if (typeof showToast === 'function') showToast('Session spawner not available', true);
    return;
  }

  const section = _composeSections.find(s => s.id === sectionId);
  if (!section || !_composeProject) return;

  // Set compose_task_id so _newSessionSubmit sends it to the backend,
  // which resolves the compose system prompt and links the session automatically
  _composeSelectedSection = sectionId;
  composeDetailTaskId = 'section:' + _composeProject.id + ':' + sectionId;

  await addNewAgent();

  if (typeof showToast === 'function') showToast('Session started for: ' + section.name);
}

// --- Open existing compose session ---
function _composeOpenSession(sessionId) {
  if (typeof openInGUI === 'function') {
    openInGUI(sessionId);
  } else if (typeof selectSession === 'function') {
    selectSession(sessionId);
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

  if (section.session_id) {
    items += '<div class="kanban-context-item" onclick="closeContextMenu();_composeOpenSession(\'' + section.session_id + '\')">Open Session</div>';
  } else {
    items += '<div class="kanban-context-item" onclick="closeContextMenu();_composeSpawnSession(\'' + sectionId + '\')">Spawn Session</div>';
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

function _composeDeleteSection(sectionId) {
  const card = document.querySelector('.compose-card[data-section-id="' + sectionId + '"]');
  const section = _composeSections.find(s => s.id === sectionId);
  const title = section ? section.name : 'this section';

  if (card) {
    const old = card.innerHTML;
    card.innerHTML = '<div class="kanban-delete-confirm"><span>Delete "' + escHtml(title.slice(0, 30)) + '"?</span><div class="kanban-delete-btns"><button class="kanban-delete-yes" onclick="event.stopPropagation();_execComposeDelete(\'' + sectionId + '\')">Delete</button><button class="kanban-delete-no" onclick="event.stopPropagation();_cancelComposeDelete(this,\'' + sectionId + '\')">Cancel</button></div></div>';
    card._oldHtml = old;
    card.onclick = null;
  } else {
    _execComposeDelete(sectionId);
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
