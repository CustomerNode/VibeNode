/* sessions.js — sorting, list rendering, tooltips, column resize, click handling */

// _shortDate() extracted to time-utils.js per plan Section 14 line 2894

function setSort(mode) {
  if (sortMode === mode) {
    sortAsc = !sortAsc;   // same column — toggle direction
  } else {
    sortMode = mode;
    sortAsc = false;      // new column — default descending
  }
  localStorage.setItem('sortMode', sortMode);
  localStorage.setItem('sortAsc', sortAsc);
  filterSessions();
}

function sortedSessions(sessions) {
  const copy = [...sessions];
  const dir = sortAsc ? 1 : -1;
  if (sortMode === 'size') {
    copy.sort((a, b) => dir * ((a.file_bytes || 0) - (b.file_bytes || 0)));
  } else if (sortMode === 'name') {
    copy.sort((a, b) => dir * (a.display_title || '').localeCompare(b.display_title || ''));
  } else {
    copy.sort((a, b) => dir * ((a.last_activity_ts || a.sort_ts || 0) - (b.last_activity_ts || b.sort_ts || 0)));
  }
  return copy;
}

function _renderSessionRow(s, extraClass) {
  const status = getSessionStatus(s.id);
  const isWaiting = status === 'question';
  const isRunning = status === 'working';
  const isIdle = status === 'idle';
  const stateClass = isWaiting ? ' waiting' : (isRunning || isIdle ? ' running' : '');
  const activeClass = s.id === activeId ? ' active' : '';
  const colClick = `onclick="singleOrDouble('${s.id}',event)" style="cursor:pointer;"`;
  const _isCompacting = isRunning && window._sessionSubstatus && window._sessionSubstatus[s.id] === 'compacting';
  const icon = isWaiting
    ? '<svg class="state-icon" width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="#ff9500" stroke-width="2" stroke-linecap="round" title="Waiting for input"><circle cx="12" cy="12" r="10"/><polyline points="12 6 12 12 16 14"/></svg>'
    : _isCompacting
    ? '<svg class="state-icon compacting-icon" width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="#aa88ff" stroke-width="2" stroke-linecap="round" title="Compacting context"><polyline points="4 14 10 14 10 20"/><polyline points="20 10 14 10 14 4"/><line x1="14" y1="10" x2="21" y2="3"/><line x1="3" y1="21" x2="10" y2="14"/></svg>'
    : isRunning
    ? '<img class="state-icon" src="/static/svg/pickaxe.svg" width="12" height="12" style="filter:brightness(0) saturate(100%) invert(55%) sepia(78%) saturate(1000%) hue-rotate(215deg);" title="Working">'
    : isIdle
    ? '<svg class="state-icon" width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="#44aa66" stroke-width="2" stroke-linecap="round" title="Idle"><polyline points="20 6 9 17 4 12"/></svg>'
    : '';
  return `
  <div class="session-item${activeClass}${stateClass}${extraClass || ''}" data-sid="${s.id}" oncontextmenu="sessionContextMenu(event,'${s.id}')">
    <div class="session-col-name" onclick="handleNameClick('${s.id}')" style="cursor:text;" title="Click to rename">
      ${icon}${escHtml(s.display_title)}${_autoNamingInFlight.has(s.id) ? '<span class="naming-badge"><span class="naming-dot"></span>Naming\u2026</span>' : ''}
    </div>
    <div class="session-col-date" ${colClick} title="${escHtml(s.last_activity)}">${escHtml(_shortDate(s.last_activity))}</div>
    <div class="session-col-size" ${colClick}>${escHtml(s.size)}</div>
  </div>`;
}

function renderList(sessions) {
  const el = document.getElementById('session-list');
  if (!sessions.length) {
    el.innerHTML = '<div style="padding:20px;color:var(--text-muted);font-size:12px;">No sessions found</div>';
    return;
  }

  const arrow = sortAsc ? '\u2191' : '\u2193';
  const header = `
    <div class="col-header-row">
      <div class="col-header sortable ${sortMode==='name'?'sort-active':''}" id="col-h-name" onclick="setSort('name')" title="Sort by name">
        Name ${sortMode==='name' ? arrow : ''}
        <span class="col-resize-grip" data-col="name"></span>
      </div>
      <div class="col-header sortable ${sortMode==='date'?'sort-active':''}" id="col-h-date" onclick="setSort('date')" title="Sort by date">
        Date ${sortMode==='date' ? arrow : ''}
        <span class="col-resize-grip" data-col="date"></span>
      </div>
      <div class="col-header sortable ${sortMode==='size'?'sort-active':''}" id="col-h-size" onclick="setSort('size')" title="Sort by size">
        Size ${sortMode==='size' ? arrow : ''}
      </div>
    </div>`;

  // --- NB-10: Compose session grouping ---
  if (typeof viewMode !== 'undefined' && viewMode === 'compose' && typeof getComposeSessionGroups === 'function') {
    const grouped = getComposeSessionGroups(sessions);
    if (grouped && grouped.groups && grouped.groups.length > 0) {
      let rows = '';
      for (const group of grouped.groups) {
        // Group header (uses existing CSS class)
        rows += `<div class="compose-session-group-header">
          <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M12 20h9"/><path d="M16.5 3.5a2.121 2.121 0 0 1 3 3L7 19l-4 1 1-4L16.5 3.5z"/></svg>
          ${escHtml(group.name)}
        </div>`;
        // Root session first with left border accent
        if (group.root) {
          rows += _renderSessionRow(group.root, ' compose-session-root');
        }
        // Section sessions indented
        for (const s of group.sections) {
          rows += _renderSessionRow(s, ' compose-session-section');
        }
      }
      // Other sessions below with separator
      if (grouped.other && grouped.other.length > 0) {
        rows += '<div class="compose-session-other-sep" style="height:1px;background:var(--border,#30363d);margin:6px 12px;opacity:0.5;"></div>';
        for (const s of grouped.other) {
          rows += _renderSessionRow(s, '');
        }
      }
      el.innerHTML = header + rows;
      initColResize();
      attachTooltipListeners();
      return;
    }
  }
  // --- End NB-10 ---

  const rows = sessions.map(s => _renderSessionRow(s, '')).join('');

  el.innerHTML = header + rows;
  initColResize();
  attachTooltipListeners();
}

/* ---- Hover tooltip ---- */
function attachTooltipListeners() {
  document.querySelectorAll('.session-item[data-sid]').forEach(row => {
    row.addEventListener('mouseenter', onRowEnter);
    row.addEventListener('mouseleave', onRowLeave);
    row.addEventListener('mousemove',  onRowMove);
  });
}

function onRowEnter(e) {
  const id = e.currentTarget.dataset.sid;
  if (!id) return;
  const s = allSessions.find(x => x.id === id);
  if (!s) return;

  const status = getSessionStatus(id);
  const stateLabels = {
    question:'<svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" style="vertical-align:middle;"><circle cx="12" cy="12" r="10"/><path d="M9.09 9a3 3 0 0 1 5.83 1c0 2-3 3-3 3"/><circle cx="12" cy="17" r=".5" fill="currentColor"/></svg> Question',
    working:'<svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" style="vertical-align:middle;"><polygon points="13 2 3 14 12 14 11 22 21 10 12 10 13 2"/></svg> Working',
    idle:'<svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" style="vertical-align:middle;"><polyline points="20 6 9 17 4 12"/></svg> Idle',
    sleeping:'<svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" style="vertical-align:middle;"><path d="M21 12.79A9 9 0 1 1 11.21 3 7 7 0 0 0 21 12.79z"/></svg> Sleeping'
  };
  const _isCompactingTip = status === 'working' && window._sessionSubstatus && window._sessionSubstatus[id] === 'compacting';
  const stateLabel = _isCompactingTip
    ? '<svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="#aa88ff" stroke-width="2" stroke-linecap="round" style="vertical-align:middle;"><polyline points="4 14 10 14 10 20"/><polyline points="20 10 14 10 14 4"/><line x1="14" y1="10" x2="21" y2="3"/><line x1="3" y1="21" x2="10" y2="14"/></svg> Compacting'
    : (stateLabels[status] || status);

  const tip = document.getElementById('session-tooltip');
  tip.innerHTML = `
    <div class="tt-title">${escHtml(s.display_title)}</div>
    <div class="tt-meta">
      <span class="tt-state ${status}">${stateLabel}</span>
      <span>${escHtml(s.last_activity)}</span>
      <span>${escHtml(s.size)}</span>
    </div>`;
  tip.classList.add('visible');
  positionTooltip(e);
}

function onRowLeave() {
  const tip = document.getElementById('session-tooltip');
  tip.classList.remove('visible');
}

function onRowMove(e) {
  positionTooltip(e);
}

function positionTooltip(e) {
  const tip = document.getElementById('session-tooltip');
  const margin = 12;
  const vw = window.innerWidth, vh = window.innerHeight;
  const tw = tip.offsetWidth, th = tip.offsetHeight;
  let x = e.clientX + margin;
  let y = e.clientY + margin;
  if (x + tw > vw - 8) x = e.clientX - tw - margin;
  if (y + th > vh - 8) y = e.clientY - th - margin;
  tip.style.left = x + 'px';
  tip.style.top  = y + 'px';
}

/* ---- Column resize ---- */
function initColResize() {
  document.querySelectorAll('.col-resize-grip').forEach(grip => {
    grip.addEventListener('mousedown', e => {
      e.stopPropagation();
      const col = grip.dataset.col;
      const startX = e.clientX;
      const sidebar = document.querySelector('.sidebar');

      // Get current pixel widths from the computed grid
      const computed = getComputedStyle(sidebar);
      const gridCols = getComputedStyle(document.querySelector('.col-header-row'))
        .gridTemplateColumns.split(' ').map(v => parseFloat(v));
      const [wName, wDate, wSize] = gridCols;

      const startVal = col === 'name' ? wName : wDate;

      grip.classList.add('dragging');
      document.body.style.cursor = 'col-resize';
      document.body.style.userSelect = 'none';

      function onMove(ev) {
        const delta = ev.clientX - startX;
        const newVal = Math.max(60, startVal + delta);
        if (col === 'name') {
          document.documentElement.style.setProperty('--col-name', newVal + 'px');
        } else {
          document.documentElement.style.setProperty('--col-date', newVal + 'px');
        }
      }
      function onUp() {
        grip.classList.remove('dragging');
        document.body.style.cursor = '';
        document.body.style.userSelect = '';
        document.removeEventListener('mousemove', onMove);
        document.removeEventListener('mouseup', onUp);
      }
      document.addEventListener('mousemove', onMove);
      document.addEventListener('mouseup', onUp);
    });
  });
}

/* ---- Single / double click ---- */
let _clickTimer = null;
let _lastClickId = null;
let _lastClickTime = 0;

function singleOrDouble(id, e) {
  openInGUI(id);
}

/* ---- Right-click context menu ---- */
function sessionContextMenu(e, sessionId) {
  e.preventDefault();
  e.stopPropagation();

  // Hide tooltip if visible
  const tip = document.getElementById('session-tooltip');
  if (tip) tip.classList.remove('visible');

  // Remove any existing context menu
  var old = document.querySelector('.session-ctx-menu');
  if (old) old.remove();

  const isActive = sessionId === activeId;
  const isRunning = runningIds.has(sessionId);
  const isOpenInGui = guiOpenSessions.has(sessionId);

  var menu = document.createElement('div');
  menu.className = 'session-ctx-menu ws-ctx-menu';

  // Build menu items
  var items = '';

  // Open (if not already active)
  if (!isActive) {
    items += '<div class="ws-ctx-item" onclick="_sessCtx(\'open\',\'' + sessionId + '\')"><svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><path d="M18 13v6a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V8a2 2 0 0 1 2-2h6"/><polyline points="15 3 21 3 21 9"/><line x1="10" y1="14" x2="21" y2="3"/></svg> Open</div>';
  }

  // Auto-name
  items += '<div class="ws-ctx-item" onclick="_sessCtx(\'autoname\',\'' + sessionId + '\')"><svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><path d="M12 20h9"/><path d="M16.5 3.5a2.121 2.121 0 0 1 3 3L7 19l-4 1 1-4L16.5 3.5z"/></svg> Auto-name</div>';

  // Rename
  items += '<div class="ws-ctx-item" onclick="_sessCtx(\'rename\',\'' + sessionId + '\')"><svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><path d="M17 3a2.828 2.828 0 1 1 4 4L7.5 20.5 2 22l1.5-5.5L17 3z"/></svg> Rename</div>';

  items += '<div class="ws-ctx-divider"></div>';

  // Link to task/section
  items += '<div class="ws-ctx-item" onclick="_sessCtx(\'link-workflow\',\'' + sessionId + '\')"><svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><path d="M10 13a5 5 0 0 0 7.54.54l3-3a5 5 0 0 0-7.07-7.07l-1.72 1.71"/><path d="M14 11a5 5 0 0 0-7.54-.54l-3 3a5 5 0 0 0 7.07 7.07l1.71-1.71"/></svg> Link to Workflow Task</div>';
  items += '<div class="ws-ctx-item" onclick="_sessCtx(\'add-compose\',\'' + sessionId + '\')"><svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><path d="M12 20h9"/><path d="M16.5 3.5a2.121 2.121 0 0 1 3 3L7 19l-4 1 1-4L16.5 3.5z"/></svg> Add to Compose</div>';
  items += '<div class="ws-ctx-item" onclick="_sessCtx(\'create-workflow\',\'' + sessionId + '\')"><svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><line x1="12" y1="5" x2="12" y2="19"/><line x1="5" y1="12" x2="19" y2="12"/></svg> Create Workflow Task</div>';

  items += '<div class="ws-ctx-divider"></div>';

  // Continue
  items += '<div class="ws-ctx-item" onclick="_sessCtx(\'continue\',\'' + sessionId + '\')"><svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><polyline points="13 17 18 12 13 7"/><polyline points="6 17 11 12 6 7"/></svg> Continue</div>';

  // Duplicate
  items += '<div class="ws-ctx-item" onclick="_sessCtx(\'duplicate\',\'' + sessionId + '\')"><svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><rect x="9" y="9" width="13" height="13" rx="2" ry="2"/><path d="M5 15H4a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2h9a2 2 0 0 1 2 2v1"/></svg> Duplicate</div>';

  // Save as Template
  items += '<div class="ws-ctx-item" onclick="_sessCtx(\'save-template\',\'' + sessionId + '\')"><svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><path d="M19 21H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h11l5 5v11a2 2 0 0 1-2 2z"/><polyline points="17 21 17 13 7 13 7 21"/><polyline points="7 3 7 8 15 8"/></svg> Save as Template</div>';

  // Open in Terminal
  items += '<div class="ws-ctx-item" onclick="_sessCtx(\'terminal\',\'' + sessionId + '\')"><svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><polyline points="4 17 10 11 4 5"/><line x1="12" y1="19" x2="20" y2="19"/></svg> Open in Terminal</div>';

  // Active-session-only actions
  if (isActive) {
    items += '<div class="ws-ctx-divider"></div>';
    items += '<div class="ws-ctx-item" onclick="_sessCtx(\'compact\',\'' + sessionId + '\')"><svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><polyline points="4 14 10 14 10 20"/><polyline points="20 10 14 10 14 4"/><line x1="14" y1="10" x2="21" y2="3"/><line x1="3" y1="21" x2="10" y2="14"/></svg> Compact Context</div>';
  }

  // Stop (if running or open in GUI)
  if (isRunning || isOpenInGui) {
    items += '<div class="ws-ctx-divider"></div>';
    items += '<div class="ws-ctx-item danger" onclick="_sessCtx(\'stop\',\'' + sessionId + '\')"><svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><rect x="3" y="3" width="18" height="18" rx="2" ry="2"/></svg> Stop Session</div>';
  }

  // Delete (always available)
  items += '<div class="ws-ctx-divider"></div>';
  items += '<div class="ws-ctx-item danger" onclick="_sessCtx(\'delete\',\'' + sessionId + '\')"><svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><polyline points="3 6 5 6 21 6"/><path d="M19 6v14a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2V6m3 0V4a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2"/></svg> Delete</div>';

  menu.innerHTML = items;
  menu.style.left = e.clientX + 'px';
  menu.style.top = e.clientY + 'px';
  document.body.appendChild(menu);

  // Ensure menu stays within viewport
  requestAnimationFrame(function() {
    var rect = menu.getBoundingClientRect();
    if (rect.right > window.innerWidth) {
      menu.style.left = (window.innerWidth - rect.width - 8) + 'px';
    }
    if (rect.bottom > window.innerHeight) {
      menu.style.top = (window.innerHeight - rect.height - 8) + 'px';
    }
  });

  // Close on click outside
  var closer = function(ev) {
    if (!menu.contains(ev.target)) {
      menu.remove();
      document.removeEventListener('click', closer);
    }
  };
  setTimeout(function() { document.addEventListener('click', closer); }, 0);
}

function _sessCtx(action, sessionId) {
  // Remove context menu
  var menu = document.querySelector('.session-ctx-menu');
  if (menu) menu.remove();

  switch (action) {
    case 'open':
      openInGUI(sessionId);
      break;
    case 'autoname':
      autoName(sessionId);
      break;
    case 'rename':
      // Open the session first if not active, then trigger inline rename
      if (sessionId !== activeId) {
        openInGUI(sessionId).then(function() {
          setTimeout(function() { handleNameClick(sessionId); }, 200);
        });
      } else {
        handleNameClick(sessionId);
      }
      break;
    case 'link-workflow':
      openTaskPickerModal(sessionId, 'workflow');
      break;
    case 'add-compose':
      _addToCompose(sessionId);
      break;
    case 'create-workflow':
      createWorkflowTaskFromSession(sessionId);
      break;
    case 'continue':
      continueSession(sessionId);
      break;
    case 'duplicate':
      duplicateSession(sessionId);
      break;
    case 'save-template':
      if (typeof _saveSessionAsTemplate === 'function') {
        _saveSessionAsTemplate(sessionId);
      } else {
        showToast('Template system not available', true);
      }
      break;
    case 'terminal':
      openInClaude(sessionId);
      break;
    case 'compact':
      liveCompact();
      break;
    case 'stop':
      closeSession(sessionId);
      break;
    case 'delete':
      deleteSession(sessionId);
      break;
  }
}

// ═══════════════════════════════════════════════════════════════
// Add to Compose — unified tree picker
// ═══════════════════════════════════════════════════════════════

// Sanitize an ID for safe embedding in onclick attributes
function _atcSafeId(id) { return String(id || '').replace(/[^a-zA-Z0-9_-]/g, ''); }

// State for the active picker (reset on close)
let _atcSessionId = null;
let _atcProjectId = null;
let _atcSections = [];

function _atcSessionName() {
  const sess = (typeof allSessions !== 'undefined')
    ? allSessions.find(s => s.id === _atcSessionId) : null;
  if (!sess) return 'Untitled Session';
  return sess.custom_title || sess.display_title || 'Untitled Session';
}

async function _addToCompose(sessionId) {
  const overlay = document.getElementById('pm-overlay');
  if (!overlay) return;

  _atcSessionId = sessionId;

  // Fetch compositions filtered to the active project.
  // MUST pass ?project= so the API only returns compositions belonging to
  // this project (+ pinned). Without it every composition across all projects
  // is returned and the picker shows unrelated items. (fix: 2026-04-13)
  const _proj = localStorage.getItem('activeProject') || '';
  let projects = [];
  try {
    const resp = await fetch('/api/compose/projects' + (_proj ? '?project=' + encodeURIComponent(_proj) : ''));
    if (resp.ok) {
      const data = await resp.json();
      if (data.ok) projects = data.projects || [];
    }
  } catch (e) {}

  // Sort: current project first, others dimmed
  projects.forEach(p => {
    p._isCurrentProject = !_proj || !p.parent_project || p.parent_project === _proj;
  });
  projects.sort((a, b) => (a._isCurrentProject ? 0 : 1) - (b._isCurrentProject ? 0 : 1));

  // Zero compositions — inline create
  if (projects.length === 0) {
    _atcShowCreateComposition(overlay);
    return;
  }

  // One composition — skip straight to tree picker
  if (projects.length === 1) {
    _atcLoadTree(overlay, projects[0].id);
    return;
  }

  // Multiple — show composition picker
  let html = '<div class="pm-card pm-enter" style="max-width:480px;">';
  html += '<h2 class="pm-title">Add to Compose</h2>';
  html += '<div class="pm-body" style="padding:0;"><div class="kanban-create-section">';
  html += '<div class="kanban-create-section-label">Choose composition</div>';

  for (const p of projects) {
    const dim = p._isCurrentProject ? '' : ' opacity:0.5;';
    const tag = p._isCurrentProject ? '' : ' <span style="font-size:10px;color:var(--text-faint);">(other project)</span>';
    const esc = typeof escHtml === 'function' ? escHtml(p.name) : p.name;
    html += '<div class="kanban-drill-chooser-card" style="cursor:pointer;' + dim + '"'
      + ' onclick="_atcLoadTree(document.getElementById(\'pm-overlay\'),\'' + _atcSafeId(p.id) + '\')">'
      + '<div class="kanban-drill-chooser-icon" style="color:var(--accent);">'
      + '<svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><path d="M12 20h9"/><path d="M16.5 3.5a2.121 2.121 0 0 1 3 3L7 19l-4 1 1-4L16.5 3.5z"/></svg></div>'
      + '<div><div class="kanban-drill-chooser-title">' + esc + tag + '</div></div></div>';
  }

  html += '</div></div></div>';
  _atcShowOverlay(overlay, html);
}

// --- Zero compositions: inline create ---
function _atcShowCreateComposition(overlay) {
  let html = '<div class="pm-card pm-enter" style="max-width:400px;">';
  html += '<h2 class="pm-title">Add to Compose</h2>';
  html += '<div class="pm-body">';
  html += '<div class="kanban-create-section-label">No compositions yet. Create one:</div>';
  html += '<input type="text" id="atc-new-comp-name" class="kanban-input" placeholder="Composition name" style="width:100%;margin:8px 0;" autofocus>';
  html += '<button class="kanban-sidebar-btn" style="width:100%;" onclick="_atcCreateComposition()">Create</button>';
  html += '</div></div>';
  _atcShowOverlay(overlay, html);
  setTimeout(() => { const inp = document.getElementById('atc-new-comp-name'); if (inp) inp.focus(); }, 60);
}

async function _atcCreateComposition() {
  const inp = document.getElementById('atc-new-comp-name');
  const name = (inp ? inp.value : '').trim();
  if (!name) { if (inp) inp.focus(); return; }

  try {
    const resp = await fetch('/api/compose/projects', {
      method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({name}),
    });
    const data = await resp.json();
    if (data.ok && data.project) {
      _atcLoadTree(document.getElementById('pm-overlay'), data.project.id);
    } else {
      showToast(data.error || 'Failed to create composition', 'error');
    }
  } catch (e) {
    showToast('Failed to create composition', 'error');
  }
}

// --- Tree picker ---
async function _atcLoadTree(overlay, projectId) {
  if (!overlay) return;
  _atcProjectId = projectId;

  // Show loading state
  let html = '<div class="pm-card pm-enter" style="max-width:520px;">';
  html += '<h2 class="pm-title">Add to Compose</h2>';
  html += '<div class="pm-body" style="text-align:center;padding:24px;color:var(--text-muted);">Loading sections...</div>';
  html += '</div>';
  _atcShowOverlay(overlay, html);

  // Fetch sections
  try {
    const resp = await fetch('/api/compose/board?project_id=' + encodeURIComponent(projectId));
    const data = await resp.json();
    _atcSections = (data && data.sections) || [];
  } catch (e) {
    _atcSections = [];
  }

  _atcRenderTree(overlay);
}

function _atcRenderTree(overlay) {
  if (!overlay) return;

  // Build tree from flat list
  const sections = _atcSections;
  const byParent = {};
  for (const s of sections) {
    const pid = s.parent_id || '__root__';
    if (!byParent[pid]) byParent[pid] = [];
    byParent[pid].push(s);
  }

  // Sort children within each parent per spec:
  // 1. Unlinked first (actionable), 2. Has unlinked children, 3. Linked (greyed)
  // Within each group, preserve order field
  function _hasUnlinkedDescendant(s) {
    const children = byParent[s.id] || [];
    for (const c of children) {
      if (!c.session_id) return true;
      if (_hasUnlinkedDescendant(c)) return true;
    }
    return false;
  }

  for (const pid of Object.keys(byParent)) {
    byParent[pid].sort((a, b) => {
      const aLinked = !!a.session_id;
      const bLinked = !!b.session_id;
      if (aLinked !== bLinked) return aLinked ? 1 : -1; // unlinked first
      if (aLinked && bLinked) {
        const aDesc = _hasUnlinkedDescendant(a);
        const bDesc = _hasUnlinkedDescendant(b);
        if (aDesc !== bDesc) return aDesc ? -1 : 1;
      }
      return (a.order || 0) - (b.order || 0);
    });
  }

  // Status dot colors
  const statusColors = {
    not_started: 'var(--text-faint)',
    draft: 'var(--text-faint)',
    writing: '#4a9eff',
    working: '#4a9eff',
    waiting: '#e8a838',
    blocked: '#e85050',
    complete: '#50c878',
    archived: '#666',
  };

  // Render tree recursively
  function renderLevel(parentId, depth) {
    const children = byParent[parentId] || [];
    if (depth > 5) return ''; // max depth
    let out = '';

    // [+ Add here] at top of this level
    out += '<div class="atc-add-marker" style="padding-left:' + (depth * 20 + 8) + 'px;">'
      + '<button class="atc-add-btn" onclick="_atcStartAdd(\'' + (parentId === '__root__' ? '' : _atcSafeId(parentId))
      + '\',' + (children.length ? (children[0].order || 0) - 1 : 0) + ')">'
      + '<svg width="10" height="10" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5"><line x1="12" y1="5" x2="12" y2="19"/><line x1="5" y1="12" x2="19" y2="12"/></svg>'
      + ' Add here</button></div>';

    for (let i = 0; i < children.length; i++) {
      const s = children[i];
      const linked = !!s.session_id;
      const color = statusColors[s.status] || statusColors.not_started;
      const esc = typeof escHtml === 'function' ? escHtml(s.name) : s.name;
      const indent = depth * 20 + 8;

      out += '<div class="atc-section' + (linked ? ' atc-linked' : '') + '" style="padding-left:' + indent + 'px;">';
      // Status dot + name
      out += '<span class="atc-dot" style="background:' + color + ';"></span>';
      out += '<span class="atc-name">' + esc + '</span>';
      out += '<span class="atc-status">' + (s.status || '').replace(/_/g, ' ') + '</span>';

      if (linked) {
        // Greyed out — show linked session name
        const sessName = s.session_id.slice(0, 8);
        out += '<span class="atc-linked-label">linked</span>';
      } else {
        // Unlinked — show [Attach here]
        out += '<button class="atc-attach-btn" onclick="_atcAttach(\'' + _atcSafeId(s.id) + '\')">'
          + 'Attach here</button>';
      }
      out += '</div>';

      // [+ Add as child]
      out += '<div class="atc-add-marker" style="padding-left:' + ((depth + 1) * 20 + 8) + 'px;">'
        + '<button class="atc-add-btn atc-add-child" onclick="_atcStartAdd(\'' + _atcSafeId(s.id) + '\',0)">'
        + '<svg width="10" height="10" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5"><line x1="12" y1="5" x2="12" y2="19"/><line x1="5" y1="12" x2="19" y2="12"/></svg>'
        + ' Add as child</button></div>';

      // Render children recursively
      out += renderLevel(s.id, depth + 1);

      // [+ Add here] after this sibling (insertion point)
      if (i < children.length - 1) {
        const nextOrder = children[i + 1] ? ((s.order || 0) + ((children[i + 1].order || 0) - (s.order || 0)) / 2) : (s.order || 0) + 1;
        out += '<div class="atc-add-marker" style="padding-left:' + (depth * 20 + 8) + 'px;">'
          + '<button class="atc-add-btn" onclick="_atcStartAdd(\'' + (parentId === '__root__' ? '' : _atcSafeId(parentId))
          + '\',' + nextOrder + ')">'
          + '<svg width="10" height="10" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5"><line x1="12" y1="5" x2="12" y2="19"/><line x1="5" y1="12" x2="19" y2="12"/></svg>'
          + ' Add here</button></div>';
      }
    }
    return out;
  }

  let html = '<div class="pm-card pm-enter" style="max-width:520px;">';
  html += '<h2 class="pm-title">Add to Compose</h2>';
  html += '<div class="pm-body" style="padding:0;">';
  html += '<div class="atc-tree" id="atc-tree">';
  html += renderLevel('__root__', 0);
  if (sections.length === 0) {
    // Empty composition — just the root add button is already rendered
  }
  html += '</div>';
  // Inline naming area (hidden until user clicks Add)
  html += '<div id="atc-naming" style="display:none;padding:12px;border-top:1px solid var(--border,#30363d);">';
  html += '<div class="kanban-create-section-label" id="atc-naming-label">Section name</div>';
  html += '<input type="text" id="atc-name-input" class="kanban-input" style="width:100%;margin:4px 0 8px;" placeholder="Section name">';
  html += '<div style="display:flex;gap:8px;">';
  html += '<button class="kanban-sidebar-btn" style="flex:1;" onclick="_atcConfirmAdd()">Add</button>';
  html += '<button class="kanban-sidebar-btn" style="flex:1;opacity:0.6;" onclick="_atcCancelAdd()">Cancel</button>';
  html += '</div></div>';
  html += '</div></div>';

  _atcShowOverlay(overlay, html);
}

// --- Add action: show naming input ---
let _atcPendingParentId = null;
let _atcPendingOrder = 0;

function _atcStartAdd(parentId, order) {
  _atcPendingParentId = parentId || null;
  _atcPendingOrder = order;
  const naming = document.getElementById('atc-naming');
  const input = document.getElementById('atc-name-input');
  const label = document.getElementById('atc-naming-label');
  if (!naming || !input) return;

  input.value = _atcSessionName();
  label.textContent = parentId ? 'Add as child — section name:' : 'New section name:';
  naming.style.display = '';
  input.focus();
  input.select();

  // Enter to confirm
  input.onkeydown = function(e) {
    if (e.key === 'Enter') { e.preventDefault(); _atcConfirmAdd(); }
    if (e.key === 'Escape') { e.preventDefault(); _atcCancelAdd(); }
  };
}

function _atcCancelAdd() {
  const naming = document.getElementById('atc-naming');
  if (naming) naming.style.display = 'none';
  _atcPendingParentId = null;
}

async function _atcConfirmAdd() {
  const input = document.getElementById('atc-name-input');
  const name = (input ? input.value : '').trim();
  if (!name) { if (input) input.focus(); return; }

  // Disable UI while saving
  const btns = document.querySelectorAll('#atc-naming button');
  btns.forEach(b => b.disabled = true);
  if (input) input.disabled = true;

  try {
    const resp = await fetch('/api/compose/projects/' + _atcProjectId + '/sections/add-and-link', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({
        name: name,
        session_id: _atcSessionId,
        parent_id: _atcPendingParentId,
        order: _atcPendingOrder,
      }),
    });
    const data = await resp.json();
    if (data.ok) {
      _closePm();
      showToast('Added to Compose: ' + name);
    } else if (resp.status === 409) {
      // Session already linked
      showToast('Session already linked to "' + (data.linked_section || 'another section') + '"', 'error');
      btns.forEach(b => b.disabled = false);
      if (input) input.disabled = false;
    } else {
      showToast(data.error || 'Failed to add section', 'error');
      btns.forEach(b => b.disabled = false);
      if (input) input.disabled = false;
    }
  } catch (e) {
    showToast('Failed to add section', 'error');
    btns.forEach(b => b.disabled = false);
    if (input) input.disabled = false;
  }
}

// --- Attach to existing unlinked section ---
async function _atcAttach(sectionId) {
  try {
    const resp = await fetch('/api/compose/projects/' + _atcProjectId + '/sections/' + sectionId, {
      method: 'PUT',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({session_id: _atcSessionId}),
    });
    const data = await resp.json();
    if (data.ok) {
      _closePm();
      showToast('Session attached to section');
    } else {
      showToast(data.error || 'Failed to attach', 'error');
    }
  } catch (e) {
    showToast('Failed to attach session', 'error');
  }
}

// --- Overlay helpers ---
function _atcShowOverlay(overlay, html) {
  overlay.innerHTML = html;
  overlay.classList.add('show');
  requestAnimationFrame(() => {
    const card = overlay.querySelector('.pm-card');
    if (card) card.classList.remove('pm-enter');
  });
  overlay.onclick = function(e) { if (e.target === overlay) _closePm(); };
}
