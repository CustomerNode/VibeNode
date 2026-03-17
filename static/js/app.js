/* app.js — global state, project loading, session loading */

let allSessions = [];
let activeId = localStorage.getItem('activeSessionId') || null;
let renameTarget = null;
let sortMode = localStorage.getItem('sortMode') || 'date';
let sortAsc  = localStorage.getItem('sortAsc') === 'true';
let viewMode = localStorage.getItem('viewMode') || 'workforce';
let wfSort = localStorage.getItem('wfSort') || 'status';
let runningIds = new Set();
let waitingData = {};   // { session_id: {question, options, kind} }
let sessionKinds = {};   // session_id -> 'question' | 'working' | 'idle'
let liveSessionId = null;
let guiOpenSessions = new Set(JSON.parse(localStorage.getItem('guiOpenSessions') || '[]'));
let _activeGrpPopup = null;
let respondTarget = null;
let _allProjects = [];  // cached project list for overlay

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
  if (target) await setProject(target, true);
}

function _projectShortName(project) {
  if (project.custom_name) return project.custom_name;
  const parts = project.display.replace(/\\/g, '/').split('/');
  return parts.slice(-2).join('/');
}

function _updateProjectLabel(project) {
  const label = document.getElementById('project-label');
  if (!project) { label.textContent = 'Select project'; return; }
  label.textContent = _projectShortName(project);
}

async function setProject(encoded, reload = true) {
  if (activeId) deselectSession();
  const p = _allProjects.find(x => x.encoded === encoded);
  _updateProjectLabel(p);
  localStorage.setItem('activeProject', encoded);
  // Show skeleton immediately
  if (reload) showSkeletonLoader();
  await fetch('/api/set-project', {
    method: 'POST', headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({project: encoded})
  });
  if (reload) loadSessions();
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
  showToast('Switching workspace\u2026');
  await setProject(encoded, true);
}

async function renameProjectOverlay(encoded, currentName) {
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
      showToast('Project renamed');
      await loadProjects();
      openProjectOverlay();
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
    icon: '\uD83D\uDDD1\uFE0F',
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
    icon: '\uD83D\uDCC1',
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
  workforce: {
    icon: '<svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><rect x="3" y="3" width="7" height="7"/><rect x="14" y="3" width="7" height="7"/><rect x="3" y="14" width="7" height="7"/><rect x="14" y="14" width="7" height="7"/></svg>',
    label: 'Grid View',
    title: 'Grid',
    desc: 'Visual cards showing session status at a glance',
  },
  list: {
    icon: '<svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><line x1="8" y1="6" x2="21" y2="6"/><line x1="8" y1="12" x2="21" y2="12"/><line x1="8" y1="18" x2="21" y2="18"/><line x1="3" y1="6" x2="3.01" y2="6"/><line x1="3" y1="12" x2="3.01" y2="12"/><line x1="3" y1="18" x2="3.01" y2="18"/></svg>',
    label: 'List View',
    title: 'List',
    desc: 'Compact table with name, date, and size columns',
  },
  workplace: {
    icon: '<svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><rect x="2" y="3" width="20" height="14" rx="2"/><line x1="8" y1="21" x2="16" y2="21"/><line x1="12" y1="17" x2="12" y2="21"/><circle cx="7" cy="10" r="1.5"/><circle cx="17" cy="10" r="1.5"/><path d="M10 10h4"/></svg>',
    label: 'Workplace',
    title: 'Workplace',
    desc: 'Virtual office space for agent collaboration',
    badge: 'Coming Soon',
  },
};

async function openViewModeSelector() {
  const overlay = document.getElementById('pm-overlay');
  const current = viewMode;
  let html = '<div class="pm-card pm-enter" style="width:380px;">'
    + '<h2 class="pm-title">View Mode</h2>'
    + '<div class="pm-body"><p>Choose how your sessions are displayed.</p></div>'
    + '<div style="display:flex;flex-direction:column;gap:8px;margin-bottom:20px;">';

  for (const [key, m] of Object.entries(_viewModes)) {
    const isActive = key === current;
    const disabled = m.badge ? ' style="opacity:0.5;cursor:default;"' : '';
    html += `<div class="add-mode-card${isActive ? ' active' : ''}" data-mode="${key}"${disabled}>
      <div class="add-mode-icon">${m.icon}</div>
      <div class="add-mode-info">
        <div class="add-mode-title">${m.title}${m.badge ? ' <span style="font-size:9px;background:var(--accent);color:#fff;padding:2px 6px;border-radius:8px;font-weight:700;margin-left:6px;">' + m.badge + '</span>' : ''}</div>
        <div class="add-mode-desc">${m.desc}</div>
      </div>
    </div>`;
  }
  html += '</div><div class="pm-actions"><button class="pm-btn pm-btn-secondary" id="pm-vm-close">Close</button></div></div>';
  overlay.innerHTML = html;
  overlay.classList.add('show');
  requestAnimationFrame(() => overlay.querySelector('.pm-card').classList.remove('pm-enter'));

  document.getElementById('pm-vm-close').onclick = () => _closePm();
  overlay.onclick = e => { if (e.target === overlay) _closePm(); };

  overlay.querySelectorAll('.add-mode-card').forEach(card => {
    const mode = card.dataset.mode;
    if (_viewModes[mode].badge) return; // disabled
    card.onclick = () => {
      _closePm();
      setViewMode(mode);
      _updateViewModeButton(mode);
      showToast(_viewModes[mode].label);
    };
  });
}

function _updateViewModeButton(mode) {
  const m = _viewModes[mode] || _viewModes.workforce;
  const iconEl = document.getElementById('view-mode-icon');
  const labelEl = document.getElementById('view-mode-label');
  if (iconEl) iconEl.outerHTML = m.icon.replace('width="18"', 'width="14" id="view-mode-icon"').replace('height="18"', 'height="14"');
  if (labelEl) labelEl.textContent = m.label;
}

// Hydrate view mode button on load
_updateViewModeButton(viewMode);

// Hydrate sort label on load
(function() {
  const opts = document.querySelectorAll('.sidebar-sort-opt');
  opts.forEach(el => {
    if (el.dataset.sort === sortMode && String(el.dataset.asc) === String(sortAsc)) {
      el.classList.add('active');
      document.getElementById('sidebar-sort-label').textContent = el.textContent;
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
  document.getElementById('sidebar-sort-label').textContent = label;
  // Apply sort
  sortAsc = !!asc;
  if (viewMode === 'workforce') {
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
  const ok = await showConfirm('Sleep All Sessions', '<p>Close <strong>' + running.length + '</strong> running session' + (running.length > 1 ? 's' : '') + ' in this workspace?</p>', { danger: true, confirmText: 'Sleep All', icon: '\uD83D\uDCA4' });
  if (!ok) return;
  let closed = 0;
  for (const s of running) {
    try {
      const r = await fetch('/api/close/' + s.id, { method: 'POST' });
      const d = await r.json();
      if (d.ok) closed++;
    } catch(e) {}
  }
  showToast(closed + ' session' + (closed !== 1 ? 's' : '') + ' closed');
  guiOpenSessions.clear();
  localStorage.setItem('guiOpenSessions', '[]');
  if (liveSessionId) updateLiveInputBar();
  pollWaiting();
}

// --- Delete All ---
async function deleteAllSessions() {
  const count = allSessions.length;
  if (!count) { showToast('No sessions to delete'); return; }
  const ok = await showConfirm('Delete All Sessions', '<p>Permanently delete <strong>all ' + count + ' sessions</strong> in this workspace?</p><p>This cannot be undone.</p>', { danger: true, confirmText: 'Delete All', icon: '\u26A0\uFE0F' });
  if (!ok) return;
  // Double confirm for safety
  const ok2 = await showConfirm('Are you sure?', '<p>This will permanently delete <strong>' + count + ' sessions</strong> and all their history.</p>', { danger: true, confirmText: 'Yes, delete everything' });
  if (!ok2) return;
  let deleted = 0;
  for (const s of [...allSessions]) {
    try {
      const r = await fetch('/api/delete/' + s.id, { method: 'DELETE' });
      const d = await r.json();
      if (d.ok) deleted++;
    } catch(e) {}
  }
  if (liveSessionId) stopLivePanel();
  deselectSession();
  await loadSessions();
  showToast(deleted + ' sessions deleted');
}

// --- New Agent ---
async function addNewAgent() {
  const name = await showPrompt('New Session', '<p>Give this session a name (optional).</p>', {
    placeholder: 'e.g. Fix login bug',
    confirmText: 'Start',
    icon: '\u2795',
  });
  if (name === null) return;

  // Optimistic UI: add placeholder to sidebar + show spinner in main body
  const tempId = '_pending_' + Date.now();
  const optimistic = {
    id: tempId,
    display_title: name || 'New Session',
    custom_title: name || '',
    last_activity: 'Starting\u2026',
    size: '',
    message_count: 0,
    preview: '',
  };
  allSessions.unshift(optimistic);
  filterSessions();
  // Highlight the optimistic entry
  activeId = tempId;
  document.getElementById('main-body').innerHTML =
    '<div class="live-panel" id="live-panel">' +
    '<div class="conversation live-log" id="live-log">' +
    '<div class="empty-state" style="padding:40px 0;">' +
    '<div class="spinner" style="margin:0 auto 12px;"></div>' +
    '<div style="color:var(--text-muted);font-size:13px;">Starting new session\u2026</div>' +
    '</div></div>' +
    '<div class="live-input-bar" id="live-input-bar"></div></div>';

  try {
    const resp = await fetch('/api/new-session', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({name: name || ''})
    });
    const data = await resp.json();
    // Remove optimistic entry
    allSessions = allSessions.filter(s => s.id !== tempId);
    if (data.ok && data.new_id) {
      guiOpenAdd(data.new_id);
      // Pre-seed as running+idle so the input bar doesn't flash "not running"
      runningIds.add(data.new_id);
      sessionKinds[data.new_id] = 'idle';
      // Quietly refresh sidebar (no skeleton flash)
      const sr = await fetch('/api/sessions');
      allSessions = await sr.json();
      document.getElementById('search').placeholder = 'Search ' + allSessions.length + ' sessions\u2026';
      filterSessions();
      // Open live panel directly (skip chat skeleton since we already show spinner)
      activeId = data.new_id;
      localStorage.setItem('activeSessionId', data.new_id);
      const cached = allSessions.find(x => x.id === data.new_id);
      setToolbarSession(data.new_id, (cached && cached.custom_title) || name || 'New Session', !cached, name || '');
      startLivePanel(data.new_id);
      showToast('Session started');
    } else if (data.ok) {
      const sr = await fetch('/api/sessions');
      allSessions = await sr.json();
      filterSessions();
      showToast('Session launched \u2014 check sidebar for new entry');
    } else {
      filterSessions();
      showToast(data.error || 'Could not start session', true);
      document.getElementById('main-body').innerHTML = _buildDashboard();
    }
  } catch(e) {
    allSessions = allSessions.filter(s => s.id !== tempId);
    filterSessions();
    showToast('Could not start session', true);
    document.getElementById('main-body').innerHTML = _buildDashboard();
  }
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
  const resp = await fetch('/api/sessions');
  allSessions = await resp.json();
  document.getElementById('search').placeholder = 'Search ' + allSessions.length + ' sessions\u2026';
  setViewMode(viewMode);
  // Restore previously selected session or show dashboard
  const savedSession = localStorage.getItem('activeSessionId');
  if (savedSession && allSessions.find(s => s.id === savedSession)) {
    openInGUI(savedSession);
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
  if (viewMode === 'workforce') {
    renderWorkforce(wfSortedSessions(filtered));
  } else {
    renderList(sortedSessions(filtered));
  }
}
