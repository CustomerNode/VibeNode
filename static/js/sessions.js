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

  const rows = sessions.map(s => {
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
    <div class="session-item${activeClass}${stateClass}" data-sid="${s.id}" oncontextmenu="sessionContextMenu(event,'${s.id}')">
      <div class="session-col-name" onclick="handleNameClick('${s.id}')" style="cursor:text;" title="Click to rename">
        ${icon}${escHtml(s.display_title)}${_autoNamingInFlight.has(s.id) ? '<span class="naming-badge"><span class="naming-dot"></span>Naming\u2026</span>' : ''}
      </div>
      <div class="session-col-date" ${colClick} title="${escHtml(s.last_activity)}">${escHtml(_shortDate(s.last_activity))}</div>
      <div class="session-col-size" ${colClick}>${escHtml(s.size)}</div>
    </div>`;
  }).join('');

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

  // Continue
  items += '<div class="ws-ctx-item" onclick="_sessCtx(\'continue\',\'' + sessionId + '\')"><svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><polyline points="13 17 18 12 13 7"/><polyline points="6 17 11 12 6 7"/></svg> Continue</div>';

  // Duplicate
  items += '<div class="ws-ctx-item" onclick="_sessCtx(\'duplicate\',\'' + sessionId + '\')"><svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><rect x="9" y="9" width="13" height="13" rx="2" ry="2"/><path d="M5 15H4a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2h9a2 2 0 0 1 2 2v1"/></svg> Duplicate</div>';

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
    case 'continue':
      continueSession(sessionId);
      break;
    case 'duplicate':
      duplicateSession(sessionId);
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
