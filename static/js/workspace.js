/* workspace.js — Workplace view: draggable session cards + unified permission panel */
/* State variables (workspaceActive, permissionQueue, etc.) are declared in app.js */

let _wsDragId = null;
let _wsFolderDragId = null;
let _archivedExpanded = false;
let _wsConfigMode = false;
let _wsConfigTab = 'departments'; // 'departments' | 'available' | 'discovery'
let _wsDiscoveryCache = null;
let _wsConfigAssistantId = null;

const _statusMiniSvg = {
  working: '<svg width="10" height="10" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round"><polygon points="13 2 3 14 12 14 11 22 21 10 12 10 13 2"/></svg>',
  question: '<svg width="10" height="10" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><circle cx="12" cy="12" r="10"/><path d="M9.09 9a3 3 0 0 1 5.83 1c0 2-3 3-3 3"/></svg>',
  idle: '<svg width="10" height="10" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round"><polyline points="20 6 9 17 4 12"/></svg>',
  sleeping: '<svg width="10" height="10" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><path d="M21 12.79A9 9 0 1 1 11.21 3 7 7 0 0 0 21 12.79z"/></svg>',
};

function _fmtElapsed(ms) {
  const s = Math.floor(ms / 1000);
  if (s < 60) return s + 's';
  const m = Math.floor(s / 60);
  if (m < 60) return m + 'm ' + (s % 60) + 's';
  const h = Math.floor(m / 60);
  return h + 'h ' + (m % 60) + 'm';
}

// ---- Render workspace into #main-body ----
function renderWorkspace(sessions) {
  const mainBody = document.getElementById('main-body');
  if (!mainBody) return;

  // If a card is expanded (showing live panel), don't re-render workspace
  if (_wsExpandedId) return;

  // Check if folder tree exists — if so, render hierarchical view
  const tree = (typeof getFolderTree === 'function') ? getFolderTree() : null;
  // In config mode, always render hierarchical (use empty tree if needed)
  const effectiveTree = tree || (_wsConfigMode ? { version: 1, folders: {}, rootChildren: [] } : null);
  if (effectiveTree) {
    _renderHierarchicalWorkspace(mainBody, sessions, effectiveTree);
  } else {
    _renderFlatWorkspace(mainBody, sessions);  // existing behavior
  }

  // Atomic sidebar switch: hide list/search/menu, show permission panel
  // Done here (after rendering) so there's no intermediate flash state
  const listEl = document.getElementById('session-list');
  const searchRow = document.querySelector('.sidebar-search-row');
  const menuWrap = document.querySelector('.sidebar-menu-wrap');
  if (listEl) listEl.style.display = 'none';
  if (searchRow) searchRow.style.display = 'none';
  if (menuWrap) menuWrap.style.display = 'none';
  const sidebarPermEl = document.getElementById('sidebar-perm-panel');
  if (sidebarPermEl) {
    sidebarPermEl.innerHTML = _buildPermissionPanel();
    sidebarPermEl.style.display = '';
  }
}

// ---- Flat workspace (original card grid, fallback when no folder tree) ----
function _renderFlatWorkspace(mainBody, sessions) {
  const visible = sessions.filter(s => !workspaceHiddenSessions.has(s.id));

  // Sort by saved position, then by status
  const statusOrder = {question:0, working:1, idle:2, sleeping:3};
  visible.sort((a, b) => {
    const pa = workspaceCardPositions[a.id] ?? 9999;
    const pb = workspaceCardPositions[b.id] ?? 9999;
    if (pa !== pb) return pa - pb;
    const sa = statusOrder[getSessionStatus(a.id)] ?? 3;
    const sb = statusOrder[getSessionStatus(b.id)] ?? 3;
    if (sa !== sb) return sa - sb;
    return (b.last_activity_ts||b.sort_ts||0) - (a.last_activity_ts||a.sort_ts||0);
  });

  const statusSvg = {
    question: '<svg width="32" height="32" viewBox="0 0 24 24" fill="none" stroke="#ff9500" stroke-width="1.5" stroke-linecap="round"><circle cx="12" cy="12" r="10"/><path d="M9.09 9a3 3 0 0 1 5.83 1c0 2-3 3-3 3"/><circle cx="12" cy="17" r=".5" fill="#ff9500"/></svg>',
    working: '<img src="/static/svg/pickaxe.svg" width="32" height="32" style="filter:brightness(0) saturate(100%) invert(55%) sepia(78%) saturate(1000%) hue-rotate(215deg);">',
    idle: '<svg width="32" height="32" viewBox="0 0 24 24" fill="none" stroke="#44aa66" stroke-width="1.5" stroke-linecap="round"><polyline points="20 6 9 17 4 12"/></svg>',
    sleeping: '<img src="/static/svg/sleeping.svg" width="32" height="32" class="sleeping-icon">',
  };
  const statusLabel = {question:'Question', working:'Working', idle:'Idle', sleeping:'Sleeping'};

  let cardsHtml = visible.map(s => {
    const st = getSessionStatus(s.id);
    const emoji = statusSvg[st] || statusSvg.sleeping;
    const label = statusLabel[st] || 'Sleeping';
    const name = escHtml((s.display_title||s.id).slice(0,28) + ((s.display_title||'').length>28?'\u2026':''));
    const date = (s.last_activity||'').split('  ')[0] || '';
    // Elapsed time for working sessions
    let elapsed = '';
    if (st === 'working') {
      const sendTime = _lastSendTimePerSession && _lastSendTimePerSession[s.id];
      if (sendTime) elapsed = _fmtElapsed(Date.now() - sendTime);
      else elapsed = '';
    }

    return `<div class="ws-card ws-${st}" draggable="true" data-sid="${s.id}"
                 ondragstart="wsDragStart(event,'${s.id}')"
                 ondragover="wsDragOver(event)" ondrop="wsDrop(event,'${s.id}')"
                 ondragend="wsDragEnd(event)"
                 onclick="expandWorkspaceCard('${s.id}')"
                 title="${escHtml(s.display_title||'')}">
      <div class="ws-card-top">
        <div class="ws-avatar">${emoji}</div>
        <button class="ws-hide-btn" onclick="event.stopPropagation();wsHideSession('${s.id}')" title="Hide from workspace">&times;</button>
      </div>
      <div class="ws-status-label">${label}</div>
      <div class="ws-name">${name}</div>
      <div class="ws-meta">${escHtml(date)}${elapsed ? ' &middot; ' + elapsed : ''}</div>
    </div>`;
  }).join('');

  if (!visible.length) {
    cardsHtml = '<div style="padding:40px;color:var(--text-faint);font-size:13px;text-align:center;width:100%;">No sessions to display. Start a new session or unhide existing ones.</div>';
  }

  // Hidden sessions count
  let hiddenHtml = '';
  if (workspaceHiddenSessions.size > 0) {
    hiddenHtml = `<div class="ws-hidden-bar">
      <span>${workspaceHiddenSessions.size} hidden session${workspaceHiddenSessions.size > 1 ? 's' : ''}</span>
      <button onclick="wsShowAll()">Show all</button>
    </div>`;
  }

  mainBody.innerHTML =
    '<div class="ws-container">' +
    '<div class="ws-canvas">' + cardsHtml + hiddenHtml + '</div>' +
    '</div>';
}

// ---- Hierarchical workspace (folder tree view) ----
function _renderHierarchicalWorkspace(mainBody, sessions, tree) {
  const currentId = (typeof _currentFolderId !== 'undefined') ? _currentFolderId : null;
  const isRoot = !currentId;

  // Validate _currentFolderId still exists
  if (currentId && !tree.folders[currentId]) {
    if (typeof _currentFolderId !== 'undefined') _currentFolderId = null;
    _renderHierarchicalWorkspace(mainBody, sessions, tree);
    return;
  }

  // Build a session lookup map
  const sessionMap = {};
  for (const s of sessions) sessionMap[s.id] = s;

  // Pre-compute folder contents to decide breadcrumb button visibility
  const childFolders = (typeof getCurrentFolderChildren === 'function')
    ? getCurrentFolderChildren()
    : _getChildFoldersFallback(tree, currentId);

  let folderSessionIds;
  if (isRoot) {
    folderSessionIds = tree.rootSessions || [];
  } else {
    folderSessionIds = (typeof getCurrentFolderSessions === 'function')
      ? getCurrentFolderSessions()
      : _getFolderSessionsFallback(tree, currentId);
  }
  const hasContent = childFolders.length > 0 || folderSessionIds.length > 0;

  let html = '<div class="ws-container">';

  // ---- CONFIG MODE: Department Manager ----
  if (isRoot && _wsConfigMode) {
    html += _renderConfigMode(tree);
    html += '</div>';
    mainBody.innerHTML = html;
    return;
  }

  // ---- ROOT: Command Center Dashboard ----
  if (isRoot) {
    // Aggregate stats across ALL sessions
    const totalSessions = sessions.length;
    let working = 0, waiting = 0, idle = 0, sleeping = 0;
    for (const s of sessions) {
      const st = getSessionStatus(s.id);
      if (st === 'working') working++;
      else if (st === 'question') waiting++;
      else if (st === 'idle') idle++;
      else sleeping++;
    }
    const totalDepts = (tree.rootChildren || []).length;
    html += '<div class="wf-command-center">';

    // Header with Work/Configure toggle
    html += '<div class="wf-cc-header">';
    html += '<div style="display:flex;align-items:center;justify-content:space-between;">';
    html += '<div class="wf-cc-title">Workforce</div>';
    html += '<div class="wf-mode-toggle">';
    html += '<button class="wf-mode-btn' + (_wsConfigMode ? '' : ' active') + '" onclick="_setWsConfigMode(false)">Work</button>';
    html += '<button class="wf-mode-btn' + (_wsConfigMode ? ' active' : '') + '" onclick="_setWsConfigMode(true)">Configure</button>';
    html += '</div>';
    html += '</div>';
    let totalSubDepts = 0;
    const _countSubs = (fids) => { for (const fid of fids) { const f = tree.folders[typeof fid === 'string' ? fid : fid.id]; if (f) { totalSubDepts += (f.children || []).length; _countSubs(f.children || []); } } };
    _countSubs(tree.rootChildren || []);
    html += '<div class="wf-cc-subtitle">' + totalDepts + ' department' + (totalDepts !== 1 ? 's' : '') + (totalSubDepts ? ' &middot; ' + totalSubDepts + ' sub-department' + (totalSubDepts !== 1 ? 's' : '') : '') + ' &middot; ' + totalSessions + ' session' + (totalSessions !== 1 ? 's' : '') + '</div>';
    html += '<div class="wf-cc-opinionated">'
      + '<span class="wf-cc-opinionated-icon" title="VibeNode is opinionated about how knowledge assets are organized.">&#9432;</span> '
      + 'VibeNode organizes your knowledge assets into <strong>departments</strong> &mdash; not skills, not agents. '
      + 'Drop any .md file into a department and invoke it as either. We handle both.'
      + '</div>';
    html += '</div>';

    // Stat cards with icons
    const _ccIcons = {
      working: '<img src="/static/svg/pickaxe.svg" width="20" height="20" style="filter:brightness(0) saturate(100%) invert(55%) sepia(78%) saturate(1000%) hue-rotate(215deg);">',
      waiting: '<svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="#ff9500" stroke-width="2" stroke-linecap="round"><circle cx="12" cy="12" r="10"/><path d="M9.09 9a3 3 0 0 1 5.83 1c0 2-3 3-3 3"/><circle cx="12" cy="17" r=".5" fill="#ff9500"/></svg>',
      idle: '<svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="var(--idle-label)" stroke-width="2" stroke-linecap="round"><polyline points="20 6 9 17 4 12"/></svg>',
      sleeping: '<img src="/static/svg/sleeping.svg" width="20" height="20" class="sleeping-icon">',
    };
    html += '<div class="wf-cc-stats">';
    html += '<div class="wf-cc-stat wf-cc-stat-working" onclick="_openStatusPopup(\'working\')"><div class="wf-cc-stat-icon">' + _ccIcons.working + '</div><div class="wf-cc-stat-num">' + working + '</div><div class="wf-cc-stat-label">Working</div></div>';
    html += '<div class="wf-cc-stat wf-cc-stat-waiting" onclick="_openStatusPopup(\'question\')"><div class="wf-cc-stat-icon">' + _ccIcons.waiting + '</div><div class="wf-cc-stat-num">' + waiting + '</div><div class="wf-cc-stat-label">Waiting</div></div>';
    html += '<div class="wf-cc-stat wf-cc-stat-idle" onclick="_openStatusPopup(\'idle\')"><div class="wf-cc-stat-icon">' + _ccIcons.idle + '</div><div class="wf-cc-stat-num">' + idle + '</div><div class="wf-cc-stat-label">Idle</div></div>';
    html += '<div class="wf-cc-stat wf-cc-stat-sleeping" onclick="_openStatusPopup(\'sleeping\')"><div class="wf-cc-stat-icon">' + _ccIcons.sleeping + '</div><div class="wf-cc-stat-num">' + sleeping + '</div><div class="wf-cc-stat-label">Sleeping</div></div>';
    html += '</div>';

    // Departments section
    html += '<div class="wf-cc-section-label">Departments</div>';
    html += '<div class="ws-canvas">';
    for (const f of childFolders) {
      const fid = (typeof f === 'string') ? f : (f && f.id ? f.id : f);
      html += _buildFolderCard(tree, fid);
    }
    html += '<div class="ws-folder-card ws-add-folder-card" onclick="wsCreateSubfolder(null)">'
      + '<div class="ws-folder-icon"><svg width="24" height="24" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round"><line x1="12" y1="5" x2="12" y2="19"/><line x1="5" y1="12" x2="19" y2="12"/></svg></div>'
      + '<div class="ws-folder-name">New Department</div>'
      + '</div>';
    html += '</div>';

    // Recent sessions — sorted by last activity, show department label
    const recentSessions = sessions
      .filter(s => !workspaceHiddenSessions.has(s.id))
      .sort((a, b) => (b.last_activity_ts || b.sort_ts || 0) - (a.last_activity_ts || a.sort_ts || 0))
      .slice(0, 10);

    if (recentSessions.length) {
      // Build session→folder lookup
      const _sidToFolder = {};
      for (const fid in tree.folders) {
        for (const sid of (tree.folders[fid].sessions || [])) {
          _sidToFolder[sid] = tree.folders[fid].name;
        }
      }

      html += '<div class="wf-cc-section-label">Recent Sessions</div>';
      html += '<div class="wf-cc-recent">';
      for (const s of recentSessions) {
        const st = getSessionStatus(s.id);
        const stIcon = _ccIcons[st] || _ccIcons.sleeping;
        const name = escHtml((s.display_title || s.id.slice(0, 8)).slice(0, 40));
        const dept = _sidToFolder[s.id] ? '<span class="wf-cc-recent-dept">' + escHtml(_sidToFolder[s.id]) + '</span>' : '';
        const date = (s.last_activity || '').split('  ')[0] || '';
        html += '<div class="wf-cc-recent-row" onclick="expandWorkspaceCard(\'' + s.id + '\')">'
          + '<span class="wf-cc-recent-icon">' + stIcon + '</span>'
          + '<span class="wf-cc-recent-name">' + name + '</span>'
          + dept
          + '<span class="wf-cc-recent-date">' + escHtml(date) + '</span>'
          + '</div>';
      }
      html += '</div>';
    }

    html += '</div>'; // close command center
    html += '</div>'; // close ws-container
    mainBody.innerHTML = html;

    // Sidebar
    const listEl = document.getElementById('session-list');
    const searchRow = document.querySelector('.sidebar-search-row');
    const menuWrap = document.querySelector('.sidebar-menu-wrap');
    if (listEl) listEl.style.display = 'none';
    if (searchRow) searchRow.style.display = 'none';
    if (menuWrap) menuWrap.style.display = 'none';
    const sidebarPermEl = document.getElementById('sidebar-perm-panel');
    if (sidebarPermEl) {
      sidebarPermEl.innerHTML = _buildPermissionPanel();
      sidebarPermEl.style.display = '';
    }
    return;
  }

  // ---- NON-ROOT: Folder view with breadcrumbs ----
  html += _buildBreadcrumbs(tree, currentId, hasContent);

  if (childFolders.length) {
    html += '<div class="ws-section-label">Sub-departments</div>';
    html += '<div class="ws-canvas">';
    for (const f of childFolders) {
      const fid = (typeof f === 'string') ? f : (f && f.id ? f.id : f);
      html += _buildFolderCard(tree, fid);
    }
    const addTarget = "'" + currentId + "'";
    html += '<div class="ws-folder-card ws-add-folder-card" onclick="wsCreateSubfolder(' + addTarget + ')">'
      + '<div class="ws-folder-icon"><svg width="24" height="24" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round"><line x1="12" y1="5" x2="12" y2="19"/><line x1="5" y1="12" x2="19" y2="12"/></svg></div>'
      + '<div class="ws-folder-name">New Department</div>'
      + '</div>';
    html += '</div>';
  }

  const folderSessions = folderSessionIds
    .map(sid => {
      // Try allSessions first, then create a stub for SDK-managed sessions
      if (sessionMap[sid]) return sessionMap[sid];
      // Session exists in folder tree but not in allSessions (SDK-managed, no .jsonl yet)
      return { id: sid, display_title: sid.slice(0, 8), custom_title: '', last_activity: '', size: '', message_count: 0, preview: '', sort_ts: 0, last_activity_ts: 0 };
    })
    .filter(s => s && !workspaceHiddenSessions.has(s.id));

  const statusOrder = {question:0, working:1, idle:2, sleeping:3};
  folderSessions.sort((a, b) => {
    const pa = workspaceCardPositions[a.id] ?? 9999;
    const pb = workspaceCardPositions[b.id] ?? 9999;
    if (pa !== pb) return pa - pb;
    const sa = statusOrder[getSessionStatus(a.id)] ?? 3;
    const sb = statusOrder[getSessionStatus(b.id)] ?? 3;
    if (sa !== sb) return sa - sb;
    return (b.last_activity_ts||b.sort_ts||0) - (a.last_activity_ts||a.sort_ts||0);
  });

  const _skillLabel = currentId && tree.folders[currentId] && tree.folders[currentId].skill ? tree.folders[currentId].skill.label : '';
  const _newSessionSkillPill = _skillLabel
    ? '<span class="ws-add-session-pill">' + escHtml(_skillLabel) + '</span>'
    : '';

  if (folderSessions.length || hasContent) {
    if (childFolders.length) {
      html += '<div class="ws-section-label">Sessions</div>';
    }
    html += '<div class="ws-canvas">';
    // "New Session" card first
    html += '<div class="ws-card ws-add-session-card" onclick="addNewAgent()">'
      + '<div class="ws-avatar"><svg width="28" height="28" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round"><line x1="12" y1="5" x2="12" y2="19"/><line x1="5" y1="12" x2="19" y2="12"/></svg></div>'
      + '<div class="ws-name">New Session</div>'
      + _newSessionSkillPill
      + '</div>';
    html += _buildSessionCardsHtml(folderSessions);
    html += '</div>';
    // If no sub-departments exist, show a subtle add button
    if (!childFolders.length && currentId) {
      const addTarget = "'" + currentId + "'";
      html += '<div class="ws-add-subdept-link" onclick="wsCreateSubfolder(' + addTarget + ')">'
        + '<svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><line x1="12" y1="5" x2="12" y2="19"/><line x1="5" y1="12" x2="19" y2="12"/></svg>'
        + ' Add sub-department</div>';
    }
  }

  // Hidden sessions count
  if (workspaceHiddenSessions.size > 0) {
    html += `<div class="ws-hidden-bar">
      <span>${workspaceHiddenSessions.size} hidden session${workspaceHiddenSessions.size > 1 ? 's' : ''}</span>
      <button onclick="wsShowAll()">Show all</button>
    </div>`;
  }

  // Archived section (root level only)
  if (isRoot && tree.archivedFolders && tree.archivedFolders.length) {
    html += _buildArchivedSection(tree);
  }

  // Empty state — centered, clean
  if (!childFolders.length && !folderSessions.length) {
    const addFolderTarget = currentId ? "'" + currentId + "'" : 'null';
    const _emptySkill = currentId && tree.folders[currentId] && tree.folders[currentId].skill ? tree.folders[currentId].skill.label : '';
    const _emptyLabel = _emptySkill ? 'Chat with ' + escHtml(_emptySkill) : 'New Session';
    html += '<div class="ws-empty-state">';
    html += '<div class="ws-empty-icon"><svg width="40" height="40" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1" stroke-linecap="round"><path d="M21 15a2 2 0 0 1-2 2H7l-4 4V5a2 2 0 0 1 2-2h14a2 2 0 0 1 2 2z"/></svg></div>';
    if (_emptySkill) {
      html += '<div class="ws-empty-skill">' + escHtml(_emptySkill) + '</div>';
    }
    html += '<div class="ws-empty-text">No sessions yet</div>';
    html += '<button class="ws-empty-primary" onclick="addNewAgent()">'
      + '<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><line x1="12" y1="5" x2="12" y2="19"/><line x1="5" y1="12" x2="19" y2="12"/></svg> '
      + _emptyLabel + '</button>';
    html += '<button class="ws-empty-secondary" onclick="wsCreateSubfolder(' + addFolderTarget + ')">or add a sub-department</button>';
    html += '</div>';
  }

  html += '</div>';
  mainBody.innerHTML = html;
}

// Fallback: get child folder IDs when getCurrentFolderChildren() is not available
function _getChildFoldersFallback(tree, currentId) {
  if (!currentId) return tree.rootChildren || [];
  const folder = tree.folders[currentId];
  return folder ? (folder.children || []) : [];
}

// Fallback: get session IDs for a folder when getCurrentFolderSessions() is not available
function _getFolderSessionsFallback(tree, currentId) {
  if (!currentId) return tree.rootSessions || [];
  const folder = tree.folders[currentId];
  return folder ? (folder.sessions || []) : [];
}

// ---- Build breadcrumb bar ----
function _buildBreadcrumbs(tree, currentId, hasContent) {
  const isRoot = !currentId;

  if (isRoot) {
    let rootHtml = '<div class="ws-breadcrumbs"><span class="ws-crumb-current" style="opacity:0.5;">Root</span></div>';
    return rootHtml;
  }

  const crumbs = (typeof getBreadcrumbs === 'function')
    ? getBreadcrumbs()
    : _buildBreadcrumbsFallback(tree, currentId);

  let html = '<div class="ws-breadcrumbs">';

  for (let i = 0; i < crumbs.length; i++) {
    if (i > 0) html += '<span class="ws-crumb-sep">&rsaquo;</span>';
    const c = crumbs[i];
    const isLast = (i === crumbs.length - 1);
    if (isLast) {
      html += '<span class="ws-crumb-current">' + escHtml(c.name) + '</span>';
      // Skill badge if folder has a skill
      const folder = tree.folders[c.id];
      if (folder && folder.skill && folder.skill.label) {
        html += '<span class="ws-crumb-skill">' + escHtml(folder.skill.label) + '</span>';
      }
    } else {
      const navId = c.id ? "'" + c.id + "'" : 'null';
      html += '<span class="ws-crumb" onclick="navigateToFolder(' + navId + ')" ondragover="event.preventDefault()" ondrop="_wsDropOnCrumb(event, ' + navId + ')">' + escHtml(c.name) + '</span>';
    }
  }

  html += '</div>';
  return html;
}

// Fallback breadcrumb builder when getBreadcrumbs() is not available
function _buildBreadcrumbsFallback(tree, currentId) {
  const chain = [];
  let fid = currentId;
  while (fid) {
    const f = tree.folders[fid];
    if (!f) break;
    chain.unshift({id: fid, name: f.name});
    fid = f.parentId || null;
  }
  return chain;
}

// ---- Build a single folder card ----
function _buildFolderCard(tree, fid) {
  const folder = tree.folders[fid];
  if (!folder) return '';

  const name = escHtml(folder.name || fid);

  // Count children and sessions
  const childCount = (folder.children || []).length;
  const sessionCount = (folder.sessions || []).length;
  const countParts = [];
  if (childCount) countParts.push(childCount + ' sub-department' + (childCount !== 1 ? 's' : ''));
  if (sessionCount) countParts.push(sessionCount + ' session' + (sessionCount !== 1 ? 's' : ''));
  const countsText = countParts.join(' &middot; ');

  // Status counts (recursive)
  let statusHtml = '';
  const counts = (typeof getFolderStatusCounts === 'function') ? getFolderStatusCounts(fid) : null;
  if (counts) {
    const statusEntries = [
      {key: 'working', cls: 'ws-status-working'},
      {key: 'question', cls: 'ws-status-question'},
      {key: 'idle', cls: 'ws-status-idle'},
      {key: 'sleeping', cls: 'ws-status-sleeping'},
    ];
    const parts = [];
    for (const entry of statusEntries) {
      const c = counts[entry.key] || 0;
      if (c > 0) {
        parts.push('<span class="' + entry.cls + '">' + (_statusMiniSvg[entry.key] || '') + ' ' + c + ' ' + entry.key + '</span>');
      }
    }
    if (parts.length) {
      statusHtml = '<div class="ws-folder-status">' + parts.join('') + '</div>';
    }
  }

  return `<div class="ws-folder-card" data-fid="${escHtml(fid)}" draggable="true"
       onclick="navigateToFolder('${fid}')"
       oncontextmenu="_wsFolderContextMenu(event, '${fid}')"
       ondragstart="_wsFolderDragStart(event, '${fid}')"
       ondragover="_wsFolderDragOver(event)"
       ondrop="_wsFolderDrop(event, '${fid}')"
       ondragend="_wsFolderDragEnd(event)">
    <div class="ws-folder-icon">
      <svg width="24" height="24" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round">
        <path d="M22 19a2 2 0 0 1-2 2H4a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h5l2 3h9a2 2 0 0 1 2 2z"/>
      </svg>
    </div>
    <div class="ws-folder-name">${name}</div>
    ${folder.skill && folder.skill.label ? '<div class="ws-folder-skill">' + escHtml(folder.skill.label) + '</div>' : ''}
    ${countsText ? '<div class="ws-folder-counts">' + countsText + '</div>' : ''}
    ${statusHtml}
    <button class="ws-folder-menu-btn" onclick="event.stopPropagation();_wsFolderContextMenu(event,'${fid}')" title="Options">
      <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><circle cx="12" cy="5" r="1"/><circle cx="12" cy="12" r="1"/><circle cx="12" cy="19" r="1"/></svg>
    </button>
  </div>`;
}

// ---- Build session cards HTML (shared by flat and hierarchical views) ----
function _buildSessionCardsHtml(visible) {
  const statusSvg = {
    question: '<svg width="32" height="32" viewBox="0 0 24 24" fill="none" stroke="#ff9500" stroke-width="1.5" stroke-linecap="round"><circle cx="12" cy="12" r="10"/><path d="M9.09 9a3 3 0 0 1 5.83 1c0 2-3 3-3 3"/><circle cx="12" cy="17" r=".5" fill="#ff9500"/></svg>',
    working: '<img src="/static/svg/pickaxe.svg" width="32" height="32" style="filter:brightness(0) saturate(100%) invert(55%) sepia(78%) saturate(1000%) hue-rotate(215deg);">',
    idle: '<svg width="32" height="32" viewBox="0 0 24 24" fill="none" stroke="#44aa66" stroke-width="1.5" stroke-linecap="round"><polyline points="20 6 9 17 4 12"/></svg>',
    sleeping: '<img src="/static/svg/sleeping.svg" width="32" height="32" class="sleeping-icon">',
  };
  const statusLabel = {question:'Question', working:'Working', idle:'Idle', sleeping:'Sleeping'};

  return visible.map(s => {
    const st = getSessionStatus(s.id);
    const emoji = statusSvg[st] || statusSvg.sleeping;
    const label = statusLabel[st] || 'Sleeping';
    const name = escHtml((s.display_title||s.id).slice(0,28) + ((s.display_title||'').length>28?'\u2026':''));
    const date = (s.last_activity||'').split('  ')[0] || '';
    let elapsed = '';
    if (st === 'working') {
      const sendTime = _lastSendTimePerSession && _lastSendTimePerSession[s.id];
      if (sendTime) elapsed = _fmtElapsed(Date.now() - sendTime);
      else elapsed = '';
    }

    return `<div class="ws-card ws-${st}" draggable="true" data-sid="${s.id}"
                 ondragstart="wsDragStart(event,'${s.id}')"
                 ondragover="wsDragOver(event)" ondrop="wsDrop(event,'${s.id}')"
                 ondragend="wsDragEnd(event)"
                 onclick="expandWorkspaceCard('${s.id}')"
                 title="${escHtml(s.display_title||'')}">
      <div class="ws-card-top">
        <div class="ws-avatar">${emoji}</div>
        <button class="ws-hide-btn" onclick="event.stopPropagation();wsHideSession('${s.id}')" title="Hide from workspace">&times;</button>
      </div>
      <div class="ws-status-label">${label}</div>
      <div class="ws-name">${name}</div>
      <div class="ws-meta">${escHtml(date)}${elapsed ? ' &middot; ' + elapsed : ''}</div>
    </div>`;
  }).join('');
}

// ---- Build archived section (root level only) ----
function _buildArchivedSection(tree) {
  const archivedFolders = tree.archivedFolders || [];
  if (!archivedFolders.length) return '';

  let bodyHtml = '';
  for (const fid of archivedFolders) {
    const folder = tree.folders[fid];
    if (!folder) continue;
    bodyHtml += `<div class="ws-archived-card" data-fid="${escHtml(fid)}">
      <div class="ws-folder-icon">
        <svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" style="opacity:0.5;">
          <path d="M22 19a2 2 0 0 1-2 2H4a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h5l2 3h9a2 2 0 0 1 2 2z"/>
        </svg>
      </div>
      <div class="ws-folder-name" style="flex:1;">${escHtml(folder.name || fid)}</div>
      <button class="ws-archived-btn" onclick="event.stopPropagation();_restoreArchivedFolder('${fid}')" title="Restore">Restore</button>
      <button class="ws-archived-btn danger" onclick="event.stopPropagation();_deleteArchivedFolder('${fid}')" title="Delete permanently">Delete</button>
    </div>`;
  }

  return `<div class="ws-archived-section">
    <div class="ws-archived-header" onclick="_toggleArchived()">
      <svg id="ws-archived-arrow" width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" style="transition:transform 0.15s;${_archivedExpanded ? 'transform:rotate(90deg);' : ''}"><polyline points="9 6 15 12 9 18"/></svg>
      Archived Folders (${archivedFolders.length})
    </div>
    <div class="ws-archived-body" id="ws-archived-body" style="display:${_archivedExpanded ? '' : 'none'};">
      ${bodyHtml}
    </div>
  </div>`;
}

// ---- Archived section toggle ----
function _toggleArchived() {
  _archivedExpanded = !_archivedExpanded;
  const body = document.getElementById('ws-archived-body');
  if (body) body.style.display = _archivedExpanded ? '' : 'none';
  const arrow = document.getElementById('ws-archived-arrow');
  if (arrow) arrow.style.transform = _archivedExpanded ? 'rotate(90deg)' : '';
}

function _restoreArchivedFolder(folderId) {
  const tree = (typeof getFolderTree === 'function') ? getFolderTree() : null;
  if (!tree) return;
  const idx = tree.archivedFolders.indexOf(folderId);
  if (idx === -1) return;
  tree.archivedFolders.splice(idx, 1);
  // Add back to rootChildren
  if (!tree.rootChildren.includes(folderId)) {
    tree.rootChildren.push(folderId);
  }
  saveFolderTree(tree);
  filterSessions();
  showToast('Folder restored');
}

function _deleteArchivedFolder(folderId) {
  const tree = (typeof getFolderTree === 'function') ? getFolderTree() : null;
  if (!tree) return;
  const folder = tree.folders[folderId];
  if (!folder) return;
  // Move sessions to root
  if (folder.sessions && folder.sessions.length) {
    tree.rootSessions = tree.rootSessions || [];
    tree.rootSessions.push(...folder.sessions);
  }
  // Move child sessions recursively to root
  function _collectSessions(id) {
    const f = tree.folders[id];
    if (!f) return;
    if (f.sessions) tree.rootSessions.push(...f.sessions);
    for (const cid of (f.children || [])) _collectSessions(cid);
    delete tree.folders[id];
  }
  _collectSessions(folderId);
  // Remove from archived
  const idx = tree.archivedFolders.indexOf(folderId);
  if (idx !== -1) tree.archivedFolders.splice(idx, 1);
  saveFolderTree(tree);
  filterSessions();
  showToast('Folder permanently deleted');
}

// ---- Folder navigation ----
function navigateToFolder(folderId, skipHistory) {
  _currentFolderId = folderId || null;
  // Push to browser history so back/forward works
  if (!skipHistory) {
    const url = new URL(window.location);
    if (folderId) {
      url.searchParams.set('folder', folderId);
    } else {
      url.searchParams.delete('folder');
    }
    history.pushState({ folder: folderId, chat: activeId || null }, '', url);
  }
  filterSessions();
}

// Restore folder + chat from URL on load + handle back/forward
window.addEventListener('popstate', function(e) {
  // Don't interfere with kanban navigation
  if (typeof viewMode !== 'undefined' && viewMode === 'kanban') return;

  const state = e.state || {};
  const url = new URL(window.location);

  // Handle folder
  const folderId = (typeof state.folder !== 'undefined') ? state.folder : (url.searchParams.get('folder') || null);
  navigateToFolder(folderId, true);

  // Handle chat
  const chatId = (typeof state.chat !== 'undefined') ? state.chat : (url.searchParams.get('chat') || null);
  _skipChatHistory = true;
  if (chatId && chatId !== activeId) {
    if (workspaceActive && typeof expandWorkspaceCard === 'function') {
      expandWorkspaceCard(chatId);
    } else if (typeof openInGUI === 'function') {
      openInGUI(chatId);
    }
  } else if (!chatId && activeId) {
    if (workspaceActive) {
      backToWorkspace();
    } else {
      deselectSession();
    }
  }
  _skipChatHistory = false;
});
// On initial load, restore from URL and set initial history state
(function() {
  const url = new URL(window.location);
  const f = url.searchParams.get('folder');
  const c = url.searchParams.get('chat');
  if (f) _currentFolderId = f;
  history.replaceState({ folder: f || null, chat: c || null }, '', url);
})();

// ---- Permission panel ----
function _buildPermissionPanel() {
  const policyIcons = {
    manual: '<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><path d="M12 22s8-4 8-10V5l-8-3-8 3v7c0 6 8 10 8 10z"/></svg>',
    auto: '<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><polygon points="13 2 3 14 12 14 11 22 21 10 12 10 13 2"/></svg>',
    custom: '<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><circle cx="12" cy="12" r="3"/><path d="M19.4 15a1.65 1.65 0 0 0 .33 1.82l.06.06a2 2 0 0 1-2.83 2.83l-.06-.06a1.65 1.65 0 0 0-1.82-.33 1.65 1.65 0 0 0-1 1.51V21a2 2 0 0 1-4 0v-.09A1.65 1.65 0 0 0 9 19.4a1.65 1.65 0 0 0-1.82.33l-.06.06a2 2 0 0 1-2.83-2.83l.06-.06A1.65 1.65 0 0 0 4.68 15a1.65 1.65 0 0 0-1.51-1H3a2 2 0 0 1 0-4h.09A1.65 1.65 0 0 0 4.6 9"/></svg>',
  };
  const policyLabels = {auto: 'Auto-Approve', manual: 'Manual', custom: 'Custom Rules'};

  let html = '<div class="kanban-sidebar-section">';
  html += '<div class="kanban-sidebar-label">Permissions</div>';

  // Policy button — same style as other sidebar buttons
  html += `<button class="kanban-sidebar-btn" onclick="openPermissionPolicySelector()">
    ${policyIcons[permissionPolicy] || ''} ${policyLabels[permissionPolicy] || 'Manual'}
  </button>`;

  // Pending permission requests
  if (permissionQueue.length > 0) {
    html += permissionQueue.map(entry => {
      const s = allSessions.find(x => x.id === entry.sessionId);
      const name = s ? escHtml((s.display_title||'').slice(0,30)) : entry.sessionId.slice(0,8);
      const toolDisplay = entry.toolName ? escHtml(entry.toolName) : 'unknown';
      const cmdDisplay = entry.command ? escHtml(entry.command.slice(0,60)) : '';
      return `<div class="ws-perm-card" data-sid="${entry.sessionId}">
        <div class="ws-perm-card-top">
          <span class="ws-perm-card-session">${name}</span>
          <span class="ws-perm-tool">${toolDisplay}</span>
        </div>
        ${cmdDisplay ? '<div class="ws-perm-cmd">' + cmdDisplay + '</div>' : ''}
        <div class="ws-perm-actions">
          <button class="ws-perm-btn ws-perm-allow" onclick="wsPermissionAnswer('${entry.sessionId}','y')">Allow</button>
          <button class="ws-perm-btn ws-perm-deny" onclick="wsPermissionAnswer('${entry.sessionId}','n')">Deny</button>
          <button class="ws-perm-btn ws-perm-always" onclick="wsPermissionAnswer('${entry.sessionId}','a')">Always</button>
        </div>
      </div>`;
    }).join('');
  }

  html += '</div>';
  return html;
}

// ---- Permission queue update (called from socket events) ----
// Auto-approve policies are GLOBAL — apply them regardless of view mode.
function _updatePermissionQueue(newWaiting) {

  const newQueue = [];
  for (const [sid, data] of Object.entries(newWaiting)) {
    if (!data) continue;

    const parsed = _parsePermissionQuestion(data.question || '');
    // Prefer direct tool data from socket events over regex-parsed text
    const toolName = data.tool_name || parsed.toolName;
    const command = (data.tool_input && (data.tool_input.command || data.tool_input.file_path || data.tool_input.path)) || parsed.command;
    const entry = {
      sessionId: sid,
      question: data.question || '',
      options: data.options || [],
      kind: data.kind,
      toolName: toolName,
      command: command,
    };

    // Auto-approve check
    if (_applyPolicies(entry)) {
      wsPermissionAnswer(sid, 'y');
      continue;
    }
    newQueue.push(entry);
  }

  permissionQueue = newQueue;

  // Update sidebar permission panel (always visible in workplace mode)
  const sidebarPermEl = document.getElementById('sidebar-perm-panel');
  if (sidebarPermEl && workspaceActive) {
    sidebarPermEl.innerHTML = _buildPermissionPanel();
    sidebarPermEl.style.display = '';
  }

  // When a card is expanded, renderWorkspace() skips, so do incremental update
  if (_wsExpandedId) {
    const permEl = document.getElementById('sidebar-perm-panel');
    if (permEl) permEl.innerHTML = _buildPermissionPanel();
  }
}

// ---- Parse question text into tool name + command ----
function _parsePermissionQuestion(text) {
  // Common patterns from Claude Code permission prompts
  let toolName = '';
  let command = '';

  // Pattern: "Tool: Read ..." or similar
  const toolMatch = text.match(/(?:Tool|Action|Permission):\s*(\w+)/i);
  if (toolMatch) toolName = toolMatch[1];

  // Pattern: tool name appears as first word after common prefixes
  if (!toolName) {
    const nameMatch = text.match(/(?:Allow|Run|Execute|Use)\s+(\w+)/i);
    if (nameMatch) toolName = nameMatch[1];
  }

  // Try to extract command/path from the question
  const cmdMatch = text.match(/`([^`]+)`/);
  if (cmdMatch) command = cmdMatch[1];

  // Fallback: try to identify tool from common patterns
  if (!toolName) {
    if (/\bread\b/i.test(text)) toolName = 'Read';
    else if (/\bwrite\b/i.test(text)) toolName = 'Write';
    else if (/\bedit\b/i.test(text)) toolName = 'Edit';
    else if (/\bbash\b/i.test(text)) toolName = 'Bash';
    else if (/\bglob\b/i.test(text)) toolName = 'Glob';
    else if (/\bgrep\b/i.test(text)) toolName = 'Grep';
  }

  return { toolName, command };
}

// ---- Policy matching ----
function _applyPolicies(entry) {
  if (permissionPolicy === 'manual') return false;
  if (permissionPolicy === 'auto') return true;

  // Custom policy
  if (permissionPolicy === 'custom') {
    const tool = (entry.toolName || '').toLowerCase();
    if (customPolicies.approveAllReads && tool === 'read') return true;
    if (customPolicies.approveProjectReads && tool === 'read') return true;
    if (customPolicies.approveAllBash && tool === 'bash') return true;
    if (customPolicies.approveProjectWrites && (tool === 'write' || tool === 'edit')) return true;
    if (customPolicies.approveGlob && tool === 'glob') return true;
    if (customPolicies.approveGrep && tool === 'grep') return true;
    if (customPolicies.customPattern) {
      try {
        const re = new RegExp(customPolicies.customPattern, 'i');
        if (re.test(entry.question)) return true;
      } catch(e) {}
    }
  }
  return false;
}

// ---- Send permission answer via WebSocket ----
async function wsPermissionAnswer(sessionId, answer) {
  // Optimistic UI
  permissionQueue = permissionQueue.filter(e => e.sessionId !== sessionId);
  delete waitingData[sessionId];
  sessionKinds[sessionId] = 'working';
  if (workspaceActive && !_wsExpandedId) {
    const permEl = document.getElementById('sidebar-perm-panel');
    if (permEl) permEl.innerHTML = _buildPermissionPanel();
    const card = document.querySelector('.ws-card[data-sid="' + sessionId + '"]');
    if (card) card.className = 'ws-card ws-working';
  }

  // Send via WebSocket
  socket.emit('permission_response', {session_id: sessionId, action: answer});
}

// ---- Policy controls ----
function openPermissionPolicySelector() {
  const overlay = document.getElementById('pm-overlay');
  const policies = [
    {key: 'manual', icon: '<svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round"><path d="M12 22s8-4 8-10V5l-8-3-8 3v7c0 6 8 10 8 10z"/><polyline points="9 12 11 14 15 10"/></svg>', title: 'Manual', desc: 'Review and approve each tool use individually'},
    {key: 'auto', icon: '<svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round"><polygon points="13 2 3 14 12 14 11 22 21 10 12 10 13 2"/></svg>', title: 'Auto-Approve All', desc: 'Automatically approve all permission requests'},
    {key: 'custom', icon: '<svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round"><circle cx="12" cy="12" r="3"/><path d="M19.4 15a1.65 1.65 0 0 0 .33 1.82l.06.06a2 2 0 0 1-2.83 2.83l-.06-.06a1.65 1.65 0 0 0-1.82-.33 1.65 1.65 0 0 0-1 1.51V21a2 2 0 0 1-4 0v-.09A1.65 1.65 0 0 0 9 19.4a1.65 1.65 0 0 0-1.82.33l-.06.06a2 2 0 0 1-2.83-2.83l.06-.06A1.65 1.65 0 0 0 4.68 15a1.65 1.65 0 0 0-1.51-1H3a2 2 0 0 1 0-4h.09A1.65 1.65 0 0 0 4.6 9a1.65 1.65 0 0 0-.33-1.82l-.06-.06a2 2 0 0 1 2.83-2.83l.06.06A1.65 1.65 0 0 0 9 4.68a1.65 1.65 0 0 0 1-1.51V3a2 2 0 0 1 4 0v.09a1.65 1.65 0 0 0 1 1.51 1.65 1.65 0 0 0 1.82-.33l.06-.06a2 2 0 0 1 2.83 2.83l-.06.06A1.65 1.65 0 0 0 19.4 9a1.65 1.65 0 0 0 1.51 1H21a2 2 0 0 1 0 4h-.09a1.65 1.65 0 0 0-1.51 1z"/></svg>', title: 'Custom Rules', desc: 'Define per-tool auto-approve rules'},
  ];

  let html = '<div class="pm-card pm-enter" style="width:380px;">'
    + '<h2 class="pm-title">Permission Policy</h2>'
    + '<div class="pm-body"><p>Choose how tool permission requests are handled.</p></div>'
    + '<div style="display:flex;flex-direction:column;gap:8px;margin-bottom:20px;">';

  for (const p of policies) {
    const isActive = p.key === permissionPolicy;
    html += `<div class="add-mode-card${isActive ? ' active' : ''}" data-policy="${p.key}">
      <div class="add-mode-icon" style="font-size:20px;">${p.icon}</div>
      <div class="add-mode-info">
        <div class="add-mode-title">${p.title}</div>
        <div class="add-mode-desc">${p.desc}</div>
      </div>
    </div>`;
  }

  html += '</div><div class="pm-actions"><button class="pm-btn pm-btn-secondary" id="pm-policy-close">Close</button></div></div>';
  overlay.innerHTML = html;
  overlay.classList.add('show');
  requestAnimationFrame(() => overlay.querySelector('.pm-card').classList.remove('pm-enter'));

  document.getElementById('pm-policy-close').onclick = () => _closePm();
  overlay.onclick = e => { if (e.target === overlay) _closePm(); };

  overlay.querySelectorAll('.add-mode-card').forEach(card => {
    card.onclick = () => {
      const key = card.dataset.policy;
      _closePm();
      setPermissionPolicy(key);
      if (key === 'custom') {
        // Delay opening custom policies modal until _closePm() animation completes
        // _closePm() uses a 150ms timeout to clear the overlay, so we wait 200ms
        setTimeout(() => openCustomPolicies(), 200);
      } else {
        showToast('Policy: ' + policies.find(p => p.key === key).title);
      }
    };
  });
}

function setPermissionPolicy(policy) {
  permissionPolicy = policy;
  localStorage.setItem('permPolicy', policy);

  // Sync to backend for server-side auto-approve
  if (typeof socket !== 'undefined') {
    socket.emit('set_permission_policy', { policy: policy, customRules: customPolicies });
  }

  // If switching to auto, approve all current queue items
  if (policy === 'auto') {
    const pending = [...permissionQueue];
    permissionQueue = [];
    pending.forEach(entry => wsPermissionAnswer(entry.sessionId, 'y'));
  }

  // Re-render permission panel in sidebar
  const permEl = document.getElementById('sidebar-perm-panel');
  if (permEl) permEl.innerHTML = _buildPermissionPanel();

  // Re-render workspace if active
  if (workspaceActive && !_wsExpandedId) {
    filterSessions();
  }
}

function openCustomPolicies() {
  const overlay = document.getElementById('pm-overlay');
  const cp = customPolicies;
  overlay.innerHTML = `
    <div class="pm-card pm-enter" style="width:400px;max-height:85vh;display:flex;flex-direction:column;">
      <h2 class="pm-title">Custom Auto-Approve Rules</h2>
      <div class="pm-body" style="overflow-y:auto;flex:1;min-height:0;">
        <p style="margin-bottom:12px;">Select which tool types to auto-approve:</p>
        <label class="ws-policy-check"><input type="checkbox" id="cp-reads" ${cp.approveAllReads?'checked':''}> Approve all Read operations</label>
        <label class="ws-policy-check"><input type="checkbox" id="cp-glob" ${cp.approveGlob?'checked':''}> Approve Glob (file search)</label>
        <label class="ws-policy-check"><input type="checkbox" id="cp-grep" ${cp.approveGrep?'checked':''}> Approve Grep (content search)</label>
        <label class="ws-policy-check"><input type="checkbox" id="cp-writes" ${cp.approveProjectWrites?'checked':''}> Approve Write/Edit operations</label>
        <label class="ws-policy-check"><input type="checkbox" id="cp-bash" ${cp.approveAllBash?'checked':''}> Approve Bash commands</label>
        <div style="margin-top:12px;">
          <label style="font-size:11px;color:var(--text-muted);display:block;margin-bottom:4px;">Custom regex pattern (matches question text):</label>
          <input class="pm-input" id="cp-pattern" type="text" value="${escHtml(cp.customPattern||'')}" placeholder="e.g. safe_directory.*">
        </div>
      </div>
      <div class="pm-actions">
        <button class="pm-btn pm-btn-secondary" onclick="_closePm()">Cancel</button>
        <button class="pm-btn pm-btn-primary" onclick="saveCustomPolicies()">Save</button>
      </div>
    </div>`;
  overlay.classList.add('show');
  requestAnimationFrame(() => overlay.querySelector('.pm-card').classList.remove('pm-enter'));
  overlay.onclick = e => { if (e.target === overlay) _closePm(); };
}

function saveCustomPolicies() {
  customPolicies = {
    approveAllReads: document.getElementById('cp-reads').checked,
    approveGlob: document.getElementById('cp-glob').checked,
    approveGrep: document.getElementById('cp-grep').checked,
    approveProjectWrites: document.getElementById('cp-writes').checked,
    approveAllBash: document.getElementById('cp-bash').checked,
    customPattern: document.getElementById('cp-pattern').value.trim(),
  };
  localStorage.setItem('customPolicies', JSON.stringify(customPolicies));

  // Sync to backend for server-side auto-approve
  if (typeof socket !== 'undefined') {
    socket.emit('set_permission_policy', { policy: permissionPolicy, customRules: customPolicies });
  }

  _closePm();
  showToast('Custom policies saved');
  if (workspaceActive) filterSessions();
}

// ---- Card expand/collapse ----
function expandWorkspaceCard(id) {
  _wsExpandedId = id;
  activeId = id;
  localStorage.setItem('activeSessionId', id);
  _pushChatUrl(id);
  if (runningIds.has(id)) guiOpenAdd(id);

  const cached = allSessions.find(x => x.id === id);
  const initTitle = cached ? cached.display_title : 'Loading\u2026';

  // Show toolbar with a back button prepended
  document.getElementById('main-toolbar').style.display = '';
  setToolbarSession(id, initTitle, !(cached && cached.custom_title), (cached && cached.custom_title) || '');

  // Add back button
  _addWorkspaceBackBtn();

  if (liveSessionId && liveSessionId !== id) stopLivePanel();
  startLivePanel(id);
  filterSessions(); // update sidebar selection
}

function _addWorkspaceBackBtn() {
  const toolbar = document.getElementById('main-toolbar');
  if (!toolbar) return;
  // Remove existing back button if any
  const existing = document.getElementById('ws-back-btn');
  if (existing) existing.remove();

  const btn = document.createElement('button');
  btn.id = 'ws-back-btn';
  btn.className = 'ws-back-btn';
  btn.innerHTML = '<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><polyline points="15 18 9 12 15 6"/></svg> Workspace';
  btn.onclick = backToWorkspace;
  toolbar.insertBefore(btn, toolbar.firstChild);
}

function backToWorkspace() {
  _wsExpandedId = null;
  if (liveSessionId) stopLivePanel();
  activeId = null;
  localStorage.removeItem('activeSessionId');
  _pushChatUrl(null);

  // Remove back button
  const btn = document.getElementById('ws-back-btn');
  if (btn) btn.remove();

  // Hide toolbar
  document.getElementById('main-toolbar').style.display = 'none';

  filterSessions();
}

// ---- Drag and drop (session cards) ----
function wsDragStart(e, id) {
  _wsDragId = id;
  e.dataTransfer.effectAllowed = 'move';
  e.dataTransfer.setData('text/plain', id);
  // Add drag class after a tick so the card doesn't immediately style
  setTimeout(() => {
    const card = document.querySelector('.ws-card[data-sid="' + id + '"]');
    if (card) card.classList.add('ws-dragging');
  }, 0);
}

function wsDragEnd(e) {
  _wsDragId = null;
  document.querySelectorAll('.ws-card.ws-dragging').forEach(c => c.classList.remove('ws-dragging'));
  document.querySelectorAll('.ws-drop-target').forEach(c => c.classList.remove('ws-drop-target'));
  document.querySelectorAll('.ws-drop-hover').forEach(c => c.classList.remove('ws-drop-hover'));
}

function wsDragOver(e) {
  e.preventDefault();
  e.dataTransfer.dropEffect = 'move';
}

function wsDrop(e, targetId) {
  e.preventDefault();
  const sourceId = _wsDragId;
  _wsDragId = null;

  if (!sourceId || sourceId === targetId) return;

  // Swap positions
  const cards = document.querySelectorAll('.ws-card[data-sid]');
  const ids = Array.from(cards).map(c => c.dataset.sid);
  const srcIdx = ids.indexOf(sourceId);
  const tgtIdx = ids.indexOf(targetId);
  if (srcIdx === -1 || tgtIdx === -1) return;

  // Move source to target position
  ids.splice(srcIdx, 1);
  ids.splice(tgtIdx, 0, sourceId);

  // Save positions
  ids.forEach((id, i) => workspaceCardPositions[id] = i);
  localStorage.setItem('wsCardPositions', JSON.stringify(workspaceCardPositions));

  // Re-render
  filterSessions();
}

// ---- Drag and drop (folder cards) ----
function _wsFolderDragStart(e, folderId) {
  _wsFolderDragId = folderId;
  e.dataTransfer.effectAllowed = 'move';
  e.dataTransfer.setData('text/plain', 'folder:' + folderId);
  setTimeout(() => {
    const card = document.querySelector('.ws-folder-card[data-fid="' + folderId + '"]');
    if (card) card.classList.add('ws-dragging');
  }, 0);
}

function _wsFolderDragEnd(e) {
  _wsFolderDragId = null;
  document.querySelectorAll('.ws-folder-card.ws-dragging').forEach(c => c.classList.remove('ws-dragging'));
  document.querySelectorAll('.ws-drop-target').forEach(c => c.classList.remove('ws-drop-target'));
  document.querySelectorAll('.ws-drop-hover').forEach(c => c.classList.remove('ws-drop-hover'));
}

function _wsFolderDragOver(e) {
  e.preventDefault();
  e.dataTransfer.dropEffect = 'move';
  const card = e.currentTarget;
  card.classList.add('ws-drop-target');
}

function _wsFolderDrop(e, targetFolderId) {
  e.preventDefault();
  e.stopPropagation();
  const card = e.currentTarget;
  card.classList.remove('ws-drop-target');

  var data = e.dataTransfer.getData('text/plain');

  // Session dropped on folder
  if (_wsDragId) {
    moveSessionToFolder(_wsDragId, targetFolderId);
    _wsDragId = null;
    filterSessions();
    return;
  }

  // Folder dropped on folder — reorder if same level, or reparent
  if (_wsFolderDragId && _wsFolderDragId !== targetFolderId) {
    // Cycle prevention
    if (typeof isDescendantOf === 'function' && isDescendantOf(targetFolderId, _wsFolderDragId)) {
      showToast('Cannot move folder into its own descendant');
      return;
    }
    reorderFolder(_wsFolderDragId, targetFolderId);
    _wsFolderDragId = null;
    filterSessions();
    return;
  }
}

// ---- Session / folder drop on breadcrumb ----
function _wsDropOnCrumb(e, folderId) {
  e.preventDefault();
  // Session dropped on breadcrumb
  if (_wsDragId) {
    moveSessionToFolder(_wsDragId, folderId);
    _wsDragId = null;
    filterSessions();
    return;
  }
  // Folder dropped on breadcrumb — reparent
  if (_wsFolderDragId) {
    if (typeof isDescendantOf === 'function' && folderId && isDescendantOf(folderId, _wsFolderDragId)) {
      showToast('Cannot move folder into its own descendant');
      return;
    }
    moveFolderToParent(_wsFolderDragId, folderId);
    _wsFolderDragId = null;
    filterSessions();
  }
}

// ---- Context menu (right-click on folder) ----
function _wsFolderContextMenu(e, folderId) {
  e.preventDefault();
  e.stopPropagation();

  // Remove any existing context menu
  var old = document.querySelector('.ws-ctx-menu');
  if (old) old.remove();

  var menu = document.createElement('div');
  menu.className = 'ws-ctx-menu';
  menu.style.left = e.clientX + 'px';
  menu.style.top = e.clientY + 'px';
  menu.innerHTML = `
    <div class="ws-ctx-item" onclick="_wsCtxRename('${folderId}')">Rename</div>
    <div class="ws-ctx-item" onclick="_wsCtxEditSkill('${folderId}')">Edit Skill</div>
    <div class="ws-ctx-item" onclick="_wsCtxAddSub('${folderId}')">Add Sub-department</div>
    <div class="ws-ctx-divider"></div>
    <div class="ws-ctx-item danger" onclick="_wsCtxDelete('${folderId}')">Delete</div>
  `;
  document.body.appendChild(menu);

  // Ensure menu stays within viewport
  requestAnimationFrame(() => {
    var rect = menu.getBoundingClientRect();
    if (rect.right > window.innerWidth) {
      menu.style.left = (window.innerWidth - rect.width - 8) + 'px';
    }
    if (rect.bottom > window.innerHeight) {
      menu.style.top = (window.innerHeight - rect.height - 8) + 'px';
    }
  });

  // Close on click outside
  var closer = (ev) => {
    if (!menu.contains(ev.target)) {
      menu.remove();
      document.removeEventListener('click', closer);
    }
  };
  setTimeout(() => document.addEventListener('click', closer), 0);
}

async function _wsCtxRename(folderId) {
  document.querySelector('.ws-ctx-menu')?.remove();
  var tree = (typeof getFolderTree === 'function') ? getFolderTree() : null;
  var folder = tree?.folders[folderId];
  if (!folder) return;
  var newName = await showPrompt('Rename Folder', '', {
    placeholder: 'Department name',
    value: folder.name,
    confirmText: 'Rename',
  });
  if (newName && newName !== folder.name) {
    renameFolder(folderId, newName);
    filterSessions();
  }
}

async function _wsCtxEditSkill(folderId) {
  document.querySelector('.ws-ctx-menu')?.remove();
  var tree = (typeof getFolderTree === 'function') ? getFolderTree() : null;
  var folder = tree?.folders[folderId];
  if (!folder) return;

  var overlay = document.getElementById('pm-overlay');
  overlay.innerHTML = `
    <div class="pm-card pm-enter" style="width:500px;max-height:85vh;display:flex;flex-direction:column;">
      <h2 class="pm-title">Edit Skill — ${escHtml(folder.name)}</h2>
      <div class="pm-body" style="overflow-y:auto;flex:1;min-height:0;">
        <label style="font-size:11px;color:var(--text-muted);display:block;margin-bottom:4px;">Skill Label</label>
        <input class="pm-input" id="skill-label-input" type="text" value="${escHtml(folder.skill?.label || '')}" placeholder="e.g. Frontend Engineer">
        <label style="font-size:11px;color:var(--text-muted);display:block;margin:12px 0 4px;">System Prompt</label>
        <textarea class="ns-textarea" id="skill-prompt-input" rows="10" placeholder="System prompt for Claude sessions in this folder...">${escHtml(folder.skill?.systemPrompt || '')}</textarea>
      </div>
      <div class="pm-actions">
        <button class="pm-btn pm-btn-secondary" onclick="_closePm()">Cancel</button>
        <button class="pm-btn pm-btn-primary" id="skill-save-btn">Save</button>
      </div>
    </div>`;
  overlay.classList.add('show');
  requestAnimationFrame(() => overlay.querySelector('.pm-card').classList.remove('pm-enter'));
  overlay.onclick = e => { if (e.target === overlay) _closePm(); };

  document.getElementById('skill-save-btn').onclick = () => {
    var label = document.getElementById('skill-label-input').value.trim();
    var prompt = document.getElementById('skill-prompt-input').value.trim();
    editFolderSkill(folderId, label || prompt ? {label, systemPrompt: prompt, icon: ''} : null);
    _closePm();
    showToast('Skill updated');
    filterSessions();
  };
}

async function _wsCtxAddSub(folderId) {
  document.querySelector('.ws-ctx-menu')?.remove();
  var name = await showPrompt('Add Sub-department', '', {
    placeholder: 'Department name',
    confirmText: 'Create',
  });
  if (name) {
    createFolder(folderId, name, null);
    filterSessions();
    showToast('Folder created');
  }
}

async function _wsCtxDelete(folderId) {
  document.querySelector('.ws-ctx-menu')?.remove();
  var tree = (typeof getFolderTree === 'function') ? getFolderTree() : null;
  var folder = tree?.folders[folderId];
  if (!folder) return;
  var ok = await showConfirm('Delete Department', `<p>Delete <strong>${escHtml(folder.name)}</strong>?</p><p>Sub-departments and sessions will be moved to the parent.</p>`, {
    danger: true,
    confirmText: 'Delete',
  });
  if (ok) {
    // If we're inside the folder being deleted, navigate to parent
    if (typeof _currentFolderId !== 'undefined' && _currentFolderId === folderId) {
      navigateToFolder(folder.parentId);
    }
    deleteFolder(folderId);
    filterSessions();
    showToast('Folder deleted');
  }
}

// ---- Hide/Show sessions ----
async function wsEditFolderSkill(folderId) {
  const tree = (typeof getFolderTree === 'function') ? getFolderTree() : null;
  if (!tree || !tree.folders[folderId]) return;
  const folder = tree.folders[folderId];
  const skill = folder.skill || { label: '', systemPrompt: '', icon: '' };

  const overlay = document.getElementById('pm-overlay');
  overlay.innerHTML = `
    <div class="pm-card pm-enter" style="width:500px;max-height:85vh;display:flex;flex-direction:column;">
      <h2 class="pm-title">Edit Skill — ${escHtml(folder.name)}</h2>
      <div class="pm-body" style="overflow-y:auto;flex:1;min-height:0;">
        <div style="margin-bottom:12px;">
          <label style="font-size:11px;color:var(--text-muted);display:block;margin-bottom:4px;">Role Title</label>
          <input class="pm-input" id="skill-label" type="text" value="${escHtml(skill.label)}" placeholder="e.g. Senior Frontend Engineer" style="margin-bottom:0;">
        </div>
        <div>
          <label style="font-size:11px;color:var(--text-muted);display:block;margin-bottom:4px;">System Prompt</label>
          <textarea class="ns-textarea" id="skill-prompt" rows="8" placeholder="Describe the expertise and behavior for sessions in this folder..." style="min-height:120px;">${escHtml(skill.systemPrompt)}</textarea>
        </div>
      </div>
      <div class="pm-actions">
        <button class="pm-btn pm-btn-secondary" id="skill-cancel">Cancel</button>
        <button class="pm-btn pm-btn-primary" id="skill-save">Save</button>
      </div>
    </div>`;
  overlay.classList.add('show');
  requestAnimationFrame(() => overlay.querySelector('.pm-card').classList.remove('pm-enter'));

  document.getElementById('skill-cancel').onclick = () => _closePm();
  overlay.onclick = e => { if (e.target === overlay) _closePm(); };
  document.getElementById('skill-save').onclick = () => {
    const newLabel = document.getElementById('skill-label').value.trim();
    const newPrompt = document.getElementById('skill-prompt').value.trim();
    if (typeof setFolderSkill === 'function') {
      setFolderSkill(folderId, { label: newLabel, systemPrompt: newPrompt, icon: '' });
    } else {
      folder.skill = { label: newLabel, systemPrompt: newPrompt, icon: '' };
      if (typeof saveFolderTree === 'function') saveFolderTree(tree);
    }
    _closePm();
    filterSessions();
    showToast('Skill updated: ' + (newLabel || folder.name));
  };
  document.getElementById('skill-label').focus();
}

async function wsCreateSubfolder(parentId) {
  const overlay = document.getElementById('pm-overlay');
  overlay.innerHTML = `
    <div class="pm-card pm-enter" style="width:460px;">
      <h2 class="pm-title">New Department</h2>
      <div class="pm-body">
        <div style="margin-bottom:14px;">
          <label style="font-size:11px;color:var(--text-muted);display:block;margin-bottom:4px;">Name</label>
          <input class="pm-input" id="dept-name" type="text" placeholder="e.g. Frontend, QA, Marketing" autocomplete="off" style="margin-bottom:0;">
        </div>
        <div style="margin-bottom:14px;">
          <label style="font-size:11px;color:var(--text-muted);display:block;margin-bottom:4px;">Role Title <span style="opacity:0.5;">(optional)</span></label>
          <input class="pm-input" id="dept-skill-label" type="text" placeholder="e.g. Senior Frontend Engineer" style="margin-bottom:0;">
        </div>
        <div>
          <label style="font-size:11px;color:var(--text-muted);display:block;margin-bottom:4px;">System Prompt <span style="opacity:0.5;">(optional)</span></label>
          <textarea class="ns-textarea" id="dept-skill-prompt" rows="4" placeholder="Describe the expertise for sessions in this department..."></textarea>
        </div>
      </div>
      <div class="pm-actions">
        <button class="pm-btn pm-btn-secondary" id="dept-cancel">Cancel</button>
        <button class="pm-btn pm-btn-primary" id="dept-create">Create</button>
      </div>
    </div>`;
  overlay.classList.add('show');
  requestAnimationFrame(() => overlay.querySelector('.pm-card').classList.remove('pm-enter'));

  document.getElementById('dept-cancel').onclick = () => _closePm();
  overlay.onclick = e => { if (e.target === overlay) _closePm(); };

  document.getElementById('dept-create').onclick = () => {
    const name = document.getElementById('dept-name').value.trim();
    if (!name) { showToast('Enter a department name'); return; }
    const skillLabel = document.getElementById('dept-skill-label').value.trim();
    const skillPrompt = document.getElementById('dept-skill-prompt').value.trim();

    if (typeof createFolder === 'function') {
      const skill = (skillLabel || skillPrompt) ? { label: skillLabel, systemPrompt: skillPrompt, icon: '' } : null;
      createFolder(parentId, name, skill);
    }
    _closePm();
    filterSessions();
    showToast('Department created: ' + name);
  };

  const nameInput = document.getElementById('dept-name');
  nameInput.onkeydown = e => { if (e.key === 'Enter') document.getElementById('dept-create').click(); if (e.key === 'Escape') _closePm(); };
  nameInput.focus();
}

// AI assist feature removed

// (AI assist removed — old code below was dead)
function _openStatusPopup(status) {
  const statusLabels = { working: 'Working', question: 'Waiting', idle: 'Idle', sleeping: 'Sleeping' };
  const label = statusLabels[status] || status;

  // Build session-to-folder lookup
  const tree = (typeof getFolderTree === 'function') ? getFolderTree() : null;
  const _sidToFolder = {};
  if (tree) {
    for (const fid in tree.folders) {
      for (const sid of (tree.folders[fid].sessions || [])) {
        _sidToFolder[sid] = tree.folders[fid].name;
      }
    }
  }

  // Filter sessions by status
  let filtered = allSessions.filter(s => getSessionStatus(s.id) === status);

  const overlay = document.getElementById('pm-overlay');

  const _renderList = (sessions, sortKey, sortAsc, query) => {
    // Filter by search
    let list = sessions;
    if (query) {
      const q = query.toLowerCase();
      list = list.filter(s => (s.display_title || '').toLowerCase().includes(q) || (_sidToFolder[s.id] || '').toLowerCase().includes(q));
    }
    // Sort
    list.sort((a, b) => {
      let va, vb;
      if (sortKey === 'name') { va = (a.display_title || '').toLowerCase(); vb = (b.display_title || '').toLowerCase(); }
      else if (sortKey === 'dept') { va = (_sidToFolder[a.id] || '').toLowerCase(); vb = (_sidToFolder[b.id] || '').toLowerCase(); }
      else { va = a.last_activity_ts || a.sort_ts || 0; vb = b.last_activity_ts || b.sort_ts || 0; }
      if (va < vb) return sortAsc ? -1 : 1;
      if (va > vb) return sortAsc ? 1 : -1;
      return 0;
    });

    let rowsHtml = '';
    if (!list.length) {
      rowsHtml = '<div style="padding:20px;text-align:center;color:var(--text-faint);">No sessions</div>';
    } else {
      for (const s of list) {
        const name = escHtml((s.display_title || s.id.slice(0, 8)).slice(0, 50));
        const dept = _sidToFolder[s.id] || '';
        const date = (s.last_activity || '').split('  ')[0] || '';
        rowsHtml += '<div class="wf-status-row" onclick="_closePm();expandWorkspaceCard(\'' + s.id + '\')">'
          + '<div class="wf-status-row-name">' + name + '</div>'
          + (dept ? '<div class="wf-status-row-dept">' + escHtml(dept) + '</div>' : '')
          + '<div class="wf-status-row-date">' + escHtml(date) + '</div>'
          + '</div>';
      }
    }
    const bodyEl = document.getElementById('wf-status-body');
    if (bodyEl) bodyEl.innerHTML = rowsHtml;
    const countEl = document.getElementById('wf-status-count');
    if (countEl) countEl.textContent = list.length + ' session' + (list.length !== 1 ? 's' : '');
  };

  let currentSort = 'date';
  let currentAsc = false;
  let currentQuery = '';

  overlay.innerHTML = '<div class="pm-card pm-enter" style="width:560px;max-height:80vh;display:flex;flex-direction:column;">'
    + '<h2 class="pm-title">' + label + ' Sessions <span id="wf-status-count" style="font-size:12px;font-weight:400;color:var(--text-muted);margin-left:8px;"></span></h2>'
    + '<div style="display:flex;gap:8px;margin-bottom:12px;align-items:center;">'
    + '<input class="pm-input" id="wf-status-search" type="text" placeholder="Search sessions..." style="margin:0;flex:1;">'
    + '<select class="ns-select" id="wf-status-sort" style="width:auto;padding:6px 8px;font-size:11px;">'
    + '<option value="date-desc">Newest</option>'
    + '<option value="date-asc">Oldest</option>'
    + '<option value="name-asc">Name A-Z</option>'
    + '<option value="name-desc">Name Z-A</option>'
    + '<option value="dept-asc">Department A-Z</option>'
    + '</select>'
    + '</div>'
    + '<div id="wf-status-body" style="overflow-y:auto;flex:1;min-height:0;"></div>'
    + '<div class="pm-actions"><button class="pm-btn pm-btn-secondary" id="wf-status-close">Close</button></div>'
    + '</div>';
  overlay.classList.add('show');
  requestAnimationFrame(() => overlay.querySelector('.pm-card').classList.remove('pm-enter'));

  _renderList(filtered, currentSort, currentAsc, currentQuery);

  document.getElementById('wf-status-close').onclick = () => _closePm();
  overlay.onclick = e => { if (e.target === overlay) _closePm(); };

  document.getElementById('wf-status-search').oninput = function() {
    currentQuery = this.value;
    _renderList(filtered, currentSort, currentAsc, currentQuery);
  };
  document.getElementById('wf-status-sort').onchange = function() {
    const v = this.value.split('-');
    currentSort = v[0];
    currentAsc = v[1] === 'asc';
    _renderList(filtered, currentSort, currentAsc, currentQuery);
  };
  document.getElementById('wf-status-search').focus();
}

function wsHideSession(id) {
  workspaceHiddenSessions.add(id);
  localStorage.setItem('wsHiddenSessions', JSON.stringify([...workspaceHiddenSessions]));
  filterSessions();
}

function wsShowSession(id) {
  workspaceHiddenSessions.delete(id);
  localStorage.setItem('wsHiddenSessions', JSON.stringify([...workspaceHiddenSessions]));
  filterSessions();
}

function wsShowAll() {
  workspaceHiddenSessions.clear();
  localStorage.setItem('wsHiddenSessions', '[]');
  filterSessions();
}

// ═══════════════════════════════════════════════════════════════════════
// CONFIG MODE — Department Manager
// ═══════════════════════════════════════════════════════════════════════

function _setWsConfigMode(enabled) {
  _wsConfigMode = enabled;
  filterSessions();
}

function _setWsConfigTab(tab) {
  _wsConfigTab = tab;
  filterSessions();
}

function _renderConfigMode(tree) {
  let h = '';
  h += '<div class="wf-config-container">';

  // Header with toggle
  h += '<div class="wf-config-header">';
  h += '<div style="display:flex;align-items:center;justify-content:space-between;">';
  h += '<div class="wf-cc-title">Configure Departments</div>';
  h += '<div class="wf-mode-toggle">';
  h += '<button class="wf-mode-btn" onclick="_setWsConfigMode(false)">Work</button>';
  h += '<button class="wf-mode-btn active" onclick="_setWsConfigMode(true)">Configure</button>';
  h += '</div>';
  h += '</div>';
  h += '<div class="wf-cc-subtitle" style="margin-top:4px;">Manage your department hierarchy, browse available assets, and discover what\'s installed on your system.</div>';
  h += '</div>';

  // Tab bar
  h += '<div class="wf-config-tabs">';
  h += '<button class="wf-config-tab' + (_wsConfigTab === 'departments' ? ' active' : '') + '" onclick="_setWsConfigTab(\'departments\')">My Departments</button>';
  h += '<button class="wf-config-tab' + (_wsConfigTab === 'available' ? ' active' : '') + '" onclick="_setWsConfigTab(\'available\')">Available</button>';
  h += '<button class="wf-config-tab' + (_wsConfigTab === 'discovery' ? ' active' : '') + '" onclick="_setWsConfigTab(\'discovery\')">Discovery</button>';
  h += '<div style="flex:1;"></div>';
  // AI Assistant button removed — organize and finder AIs live on their respective tabs
  h += '</div>';

  // Tab content
  h += '<div class="wf-config-body">';
  if (_wsConfigTab === 'departments') {
    h += _renderConfigDepartments(tree);
  } else if (_wsConfigTab === 'available') {
    h += _renderConfigAvailable();
  } else if (_wsConfigTab === 'discovery') {
    h += _renderConfigDiscovery();
  }
  h += '</div>';

  h += '</div>';
  return h;
}

// ---- Tab 1: My Departments ----
let _configExpandedDepts = new Set(); // track which departments are expanded

function _toggleConfigDept(fid) {
  if (_configExpandedDepts.has(fid)) _configExpandedDepts.delete(fid);
  else _configExpandedDepts.add(fid);
  filterSessions();
}

function _renderConfigDepartments(tree) {
  let h = '';
  const roots = tree.rootChildren || [];

  // Action bar pinned at top
  h += '<div class="wf-config-action-bar">';
  h += '<button class="wf-config-action-btn wf-config-action-primary" onclick="wsCreateSubfolder(null)">';
  h += '<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round"><line x1="12" y1="5" x2="12" y2="19"/><line x1="5" y1="12" x2="19" y2="12"/></svg>';
  h += ' New Department</button>';
  h += '<button class="wf-config-action-btn wf-config-action-ai" onclick="_openOrganizeAI()">';
  h += '<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><path d="M12 2l2.4 7.4H22l-6.2 4.5 2.4 7.4L12 16.8l-6.2 4.5 2.4-7.4L2 9.4h7.6z"/></svg>';
  h += ' Organize with AI</button>';
  h += '<div class="wf-config-action-bar-summary">' + roots.length + ' department' + (roots.length !== 1 ? 's' : '');
  // Count total agents
  let totalAgents = 0;
  for (const rf of roots) {
    const fid = typeof rf === 'string' ? rf : rf.id;
    totalAgents += _countAgentsRecursive(tree, fid);
  }
  h += ' &middot; ' + totalAgents + ' asset' + (totalAgents !== 1 ? 's' : '') + '</div>';
  h += '</div>';

  if (!roots.length) {
    h += '<div class="wf-config-empty-state">';
    h += '<div class="wf-config-empty-title">No departments configured yet</div>';
    h += '<div class="wf-config-empty-desc">Pick a starter template to get going instantly, or add departments one at a time.</div>';
    h += '<div class="wf-config-templates">';
    h += '<div class="wf-config-template-card" onclick="_applyQuickTemplate(\'personal\')">';
    h += '<div class="wf-config-template-icon"><svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="var(--accent)" stroke-width="1.5"><circle cx="12" cy="8" r="5"/><path d="M20 21a8 8 0 0 0-16 0"/></svg></div>';
    h += '<div class="wf-config-template-name">Personal</div>';
    h += '<div class="wf-config-template-meta">5 departments &middot; Coding, writing, docs, research</div>';
    h += '</div>';
    h += '<div class="wf-config-template-card" onclick="_applyQuickTemplate(\'small-team\')">';
    h += '<div class="wf-config-template-icon"><svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="#3fb950" stroke-width="1.5"><path d="M17 21v-2a4 4 0 0 0-4-4H5a4 4 0 0 0-4 4v2"/><circle cx="9" cy="7" r="4"/><path d="M23 21v-2a4 4 0 0 0-3-3.87"/><path d="M16 3.13a4 4 0 0 1 0 7.75"/></svg></div>';
    h += '<div class="wf-config-template-name">Small Team</div>';
    h += '<div class="wf-config-template-meta">17 departments &middot; Eng, product, QA, docs, marketing</div>';
    h += '</div>';
    h += '<div class="wf-config-template-card" onclick="_applyQuickTemplate(\'enterprise\')">';
    h += '<div class="wf-config-template-icon"><svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="#bc8cff" stroke-width="1.5"><rect x="2" y="7" width="20" height="14" rx="2"/><path d="M16 7V5a2 2 0 0 0-2-2h-4a2 2 0 0 0-2 2v2"/></svg></div>';
    h += '<div class="wf-config-template-name">Enterprise</div>';
    h += '<div class="wf-config-template-meta">72 assets &middot; Full org chart across 17 departments</div>';
    h += '</div>';
    h += '</div>';
    h += '</div>';
    return h;
  }

  // Department tree
  h += '<div class="wf-config-tree">';
  for (const rf of roots) {
    const fid = typeof rf === 'string' ? rf : rf.id;
    h += _renderConfigNode(tree, fid, 0);
  }
  h += '</div>';

  return h;
}

function _countAgentsRecursive(tree, fid) {
  const folder = tree.folders[fid];
  if (!folder) return 0;
  const children = folder.children || [];
  let count = 0;
  for (const ck of children) {
    const cid = typeof ck === 'string' ? ck : ck.id;
    const cfolder = tree.folders[cid];
    if (cfolder && (cfolder.children || []).length > 0) {
      count += _countAgentsRecursive(tree, cid);
    } else {
      count++;
    }
  }
  return count;
}

function _renderConfigNode(tree, fid, depth) {
  const folder = tree.folders[fid];
  if (!folder) return '';
  const def = (typeof FOLDER_SUPERSET !== 'undefined' && FOLDER_SUPERSET[fid]) ? FOLDER_SUPERSET[fid] : null;
  const label = def ? (def.skill ? def.skill.label : def.name) : fid;
  const children = folder.children || [];
  const hasChildren = children.length > 0;
  const isExpanded = _configExpandedDepts.has(fid);
  const tier = _detectTierFromDef(def);
  const indent = depth * 20;

  // Check if children are departments (have their own children) or leaf agents
  let childDepts = 0, childAgents = 0;
  for (const ck of children) {
    const cid = typeof ck === 'string' ? ck : ck.id;
    const cf = tree.folders[cid];
    if (cf && (cf.children || []).length > 0) childDepts++;
    else childAgents++;
  }

  let h = '';
  h += '<div class="wf-config-node' + (depth === 0 ? ' wf-config-node-root' : '') + '" style="padding-left:' + indent + 'px;">';

  // Row — departments expand/collapse, leaf assets toggle detail view
  h += '<div class="wf-config-node-row' + (hasChildren ? ' wf-config-node-expandable' : ' wf-config-node-leaf') + '" onclick="' + (hasChildren ? '_toggleConfigDept(\'' + fid + '\')' : '_toggleConfigDetail(\'' + fid + '\')') + '">';

  // Expand chevron or bullet
  if (hasChildren) {
    h += '<svg class="wf-config-chevron' + (isExpanded ? ' expanded' : '') + '" width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round"><polyline points="9 6 15 12 9 18"/></svg>';
  } else {
    h += '<span class="wf-config-bullet"></span>';
  }

  // Icon based on depth
  if (depth === 0) {
    h += '<svg class="wf-config-node-icon" width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="var(--accent)" stroke-width="1.5" stroke-linecap="round"><path d="M22 19a2 2 0 0 1-2 2H4a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h5l2 3h9a2 2 0 0 1 2 2z"/></svg>';
  } else {
    h += '<svg class="wf-config-node-icon" width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="var(--text-faint)" stroke-width="2" stroke-linecap="round"><circle cx="12" cy="12" r="3"/></svg>';
  }

  // Name + meta
  h += '<span class="wf-config-node-name">' + _esc(label) + '</span>';
  h += '<span class="wf-config-tier-badge wf-tier-' + tier + '">' + tier + '</span>';

  if (hasChildren) {
    const parts = [];
    if (childDepts) parts.push(childDepts + ' sub-dept' + (childDepts !== 1 ? 's' : ''));
    if (childAgents) parts.push(childAgents + ' asset' + (childAgents !== 1 ? 's' : ''));
    h += '<span class="wf-config-node-count">' + parts.join(', ') + '</span>';
  }

  h += '</div>'; // end row

  // Detail panel for leaf assets (shown when clicked)
  if (!hasChildren && _configExpandedDepts.has(fid) && def && def.skill) {
    h += '<div class="wf-config-detail" style="padding-left:' + (indent + 28) + 'px;">';
    const prompt = def.skill.systemPrompt || '';
    h += '<div class="wf-config-detail-preview">' + _esc(prompt.substring(0, 300)) + (prompt.length > 300 ? '...' : '') + '</div>';
    h += '<div class="wf-config-detail-actions">';
    h += '<button class="wf-config-detail-btn" onclick="event.stopPropagation();_editConfigAsset(\'' + fid + '\')"><svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><path d="M11 4H4a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h14a2 2 0 0 0 2-2v-7"/><path d="M18.5 2.5a2.121 2.121 0 0 1 3 3L12 15l-4 1 1-4 9.5-9.5z"/></svg> Edit</button>';
    h += '<button class="wf-config-detail-btn wf-config-detail-btn-danger" onclick="event.stopPropagation();_deleteConfigAsset(\'' + fid + '\')"><svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><polyline points="3 6 5 6 21 6"/><path d="M19 6v14a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2V6m3 0V4a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2"/></svg> Delete</button>';
    h += '</div>';
    h += '</div>';
  }

  // Children (if expanded)
  if (hasChildren && isExpanded) {
    h += '<div class="wf-config-node-children">';
    for (const ck of children) {
      const cid = typeof ck === 'string' ? ck : ck.id;
      h += _renderConfigNode(tree, cid, depth + 1);
    }
    h += '</div>';
  }

  h += '</div>'; // end node
  return h;
}

function _toggleConfigDetail(fid) {
  if (_configExpandedDepts.has(fid)) _configExpandedDepts.delete(fid);
  else _configExpandedDepts.add(fid);
  filterSessions();
}

function _editConfigAsset(fid) {
  // TODO: open inline editor for the asset's system prompt
  showToast('Editing coming soon — edit workforce/' + fid + '.md directly for now');
}

async function _deleteConfigAsset(fid) {
  if (!confirm('Delete "' + fid + '"? This removes the .md file from your workforce directory.')) return;
  try {
    const resp = await fetch('/api/workforce/delete-asset', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ id: fid }),
    });
    if (resp.ok) {
      // Remove from folder tree
      const tree = (typeof getFolderTree === 'function') ? getFolderTree() : null;
      if (tree && tree.folders) {
        // Find parent and remove from its children
        for (const [pid, folder] of Object.entries(tree.folders)) {
          if (folder.children) {
            folder.children = folder.children.filter(c => (typeof c === 'string' ? c : c.id) !== fid);
          }
        }
        delete tree.folders[fid];
        // Remove from rootChildren if there
        if (tree.rootChildren) {
          tree.rootChildren = tree.rootChildren.filter(c => (typeof c === 'string' ? c : c.id) !== fid);
        }
        await fetch('/api/folder-tree', {
          method: 'PUT',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify(tree),
        });
        _folderTreeCache = tree;
      }
      if (typeof _agentCatalogPath !== 'undefined') { _agentCatalogPath = null; _agentCatalogPromise = null; }
      if (typeof _loadWorkforceFromDisk === 'function') await _loadWorkforceFromDisk();
      showToast('Deleted: ' + fid);
      filterSessions();
    } else {
      showToast('Delete failed');
    }
  } catch (e) {
    showToast('Delete failed: ' + e.message);
  }
}

function _detectTierFromDef(def) {
  if (!def || !def.skill) return 'role';
  // TODO: detect from frontmatter when available
  return 'role';
}

function _esc(s) {
  if (!s) return '';
  return s.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;');
}

// ---- Tab 2: Available ----
function _renderConfigAvailable() {
  let h = '';

  const packs = [
    { id: 'gstack', name: 'garrytan/gstack', stars: '62K', count: '23 skills', desc: 'Full dev team — code review, QA with real browser, security audit (OWASP + STRIDE), shipping, deploy, retros. By Garry Tan (YC).', url: 'https://github.com/garrytan/gstack', git: 'https://github.com/garrytan/gstack.git', setup: './setup', icon: '<svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="#bc8cff" stroke-width="1.5" stroke-linecap="round"><path d="M12 2l2.4 7.4H22l-6.2 4.5 2.4 7.4L12 16.8l-6.2 4.5 2.4-7.4L2 9.4h7.6z"/></svg>' },
    { id: 'anthropic-skills', name: 'anthropics/skills', stars: '109K', count: '17 skills', desc: 'Anthropic\'s official reference skills — PDF generation, PPTX, frontend design, data analysis, and more.', url: 'https://github.com/anthropics/skills', git: 'https://github.com/anthropics/skills.git', icon: '<svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="#58a6ff" stroke-width="1.5" stroke-linecap="round"><path d="M9 12l2 2 4-4"/><circle cx="12" cy="12" r="10"/></svg>' },
    { id: 'everything-cc', name: 'affaan-m/everything-claude-code', stars: '120K', count: '40+ skills, 13 agents', desc: 'Comprehensive agent harness — parallel execution, performance monitoring, structured output pipelines.', url: 'https://github.com/affaan-m/everything-claude-code', git: 'https://github.com/affaan-m/everything-claude-code.git', icon: '<svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="#3fb950" stroke-width="1.5" stroke-linecap="round"><rect x="2" y="3" width="20" height="14" rx="2"/><line x1="8" y1="21" x2="16" y2="21"/><line x1="12" y1="17" x2="12" y2="21"/></svg>' },
    { id: 'wshobson-agents', name: 'wshobson/agents', stars: '31K', count: '112 agents, 146 skills', desc: 'Massive collection — 72 plugins covering every dev workflow. Agents + skills + dev tools.', url: 'https://github.com/wshobson/agents', git: 'https://github.com/wshobson/agents.git', icon: '<svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="#e3b341" stroke-width="1.5" stroke-linecap="round"><circle cx="12" cy="8" r="5"/><path d="M20 21a8 8 0 0 0-16 0"/></svg>' },
    { id: 'antigravity', name: 'sickn33/antigravity-awesome-skills', stars: '29K', count: '1,340+ skills', desc: 'Curated mega-list of installable skills for Claude Code, Cursor, and Codex.', url: 'https://github.com/sickn33/antigravity-awesome-skills', git: 'https://github.com/sickn33/antigravity-awesome-skills.git', icon: '<svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="#ff7b72" stroke-width="1.5" stroke-linecap="round"><path d="M4 19.5A2.5 2.5 0 0 1 6.5 17H20"/><path d="M6.5 2H20v20H6.5A2.5 2.5 0 0 1 4 19.5v-15A2.5 2.5 0 0 1 6.5 2z"/></svg>' },
    { id: 'alirezarezvani-skills', name: 'alirezarezvani/claude-skills', stars: '5.2K', count: '220+ skills', desc: 'Multi-domain skills with Python tool integrations.', url: 'https://github.com/alirezarezvani/claude-skills', git: 'https://github.com/alirezarezvani/claude-skills.git', icon: '<svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="#39d2c0" stroke-width="1.5" stroke-linecap="round"><polyline points="16 18 22 12 16 6"/><polyline points="8 6 2 12 8 18"/></svg>' },
  ];

  // Built-in tiers — count only built-in departments (not community packs)
  const tree = (typeof getFolderTree === 'function') ? getFolderTree() : null;
  // Built-in keys are ones that exist in the hardcoded FOLDER_SUPERSET fallback (no source field)
  const _builtinKeys = typeof FOLDER_TEMPLATES !== 'undefined' && FOLDER_TEMPLATES.enterprise && FOLDER_TEMPLATES.enterprise.keys === null
    ? new Set(Object.keys(typeof _HARDCODED_SUPERSET !== 'undefined' ? _HARDCODED_SUPERSET : {}))
    : null;
  const installedCount = tree && tree.rootChildren
    ? tree.rootChildren.filter(rc => {
        const fid = typeof rc === 'string' ? rc : rc.id;
        // If we have FOLDER_SUPERSET, check if this dept's assets have source field (=community pack)
        if (typeof FOLDER_SUPERSET === 'object' && FOLDER_SUPERSET && FOLDER_SUPERSET[fid]) {
          // Community packs are imported with source in their systemPrompt frontmatter
          // Simplest check: built-in departments don't start with known pack prefixes
          const knownPacks = ['gstack','anthropic-skills','everything-cc','wshobson-agents','antigravity','alirezarezvani-skills'];
          return !knownPacks.includes(fid);
        }
        return true;
      }).length
    : 0;

  h += '<div class="wf-avail-section-title">VibeNode Built-in Library</div>';
  h += '<div class="wf-avail-section-desc">Role-based assets organized into departments. Pick a size or uninstall to start fresh.</div>';
  h += '<div class="wf-avail-tiers">';

  // Tiers are supersets: enterprise > small-team > personal
  // tierLevel: 0=none, 1=personal, 2=small-team, 3=enterprise
  const tiers = [
    { key: 'personal', level: 1, name: 'Personal', count: '5 depts', desc: 'Coding, writing, docs, research, learning', icon: '<svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="var(--accent)" stroke-width="1.5"><circle cx="12" cy="8" r="5"/><path d="M20 21a8 8 0 0 0-16 0"/></svg>' },
    { key: 'small-team', level: 2, name: 'Small Team', count: '17 depts', desc: 'Engineering, product, QA, docs, marketing', icon: '<svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="#3fb950" stroke-width="1.5"><path d="M17 21v-2a4 4 0 0 0-4-4H5a4 4 0 0 0-4 4v2"/><circle cx="9" cy="7" r="4"/><path d="M23 21v-2a4 4 0 0 0-3-3.87"/><path d="M16 3.13a4 4 0 0 1 0 7.75"/></svg>' },
    { key: 'enterprise', level: 3, name: 'Enterprise', count: '17 depts, 72 assets', desc: 'Full org chart — eng, QA, product, data, security, legal, marketing, sales, CS, HR, finance, ops', icon: '<svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="#bc8cff" stroke-width="1.5"><rect x="2" y="7" width="20" height="14" rx="2"/><path d="M16 7V5a2 2 0 0 0-2-2h-4a2 2 0 0 0-2 2v2"/></svg>' },
  ];

  // Detect current tier from installed count
  let currentLevel = 0;
  if (installedCount >= 15) currentLevel = 3;       // enterprise
  else if (installedCount >= 5) currentLevel = 2;   // small-team
  else if (installedCount > 0) currentLevel = 1;    // personal

  for (const t of tiers) {
    const isActive = t.level === currentLevel;
    const isSubset = t.level < currentLevel;  // below current — grayed out
    const isUpgrade = t.level > currentLevel; // above current — clickable upgrade

    h += '<div class="wf-avail-tier-card' + (isActive ? ' active' : '') + (isSubset ? ' subset' : '') + '">';
    h += '<div class="wf-avail-tier-top">';
    h += '<div class="wf-avail-card-icon">' + t.icon + '</div>';
    h += '<div style="flex:1;min-width:0;">';
    h += '<div class="wf-avail-tier-name">' + t.name + '</div>';
    h += '<div class="wf-avail-tier-count">' + t.count + '</div>';
    h += '</div>';
    if (isActive) {
      h += '<span class="wf-config-source-badge">Active</span>';
    } else if (isSubset) {
      h += '<span class="wf-config-source-badge" style="opacity:0.4;">Included</span>';
    }
    h += '</div>';
    h += '<div class="wf-avail-tier-desc">' + t.desc + '</div>';
    h += '<div class="wf-avail-card-actions">';
    if (isActive) {
      h += '<button class="wf-avail-uninstall-btn" onclick="_uninstallBuiltin()">Uninstall</button>';
    } else if (isUpgrade) {
      h += '<button class="wf-avail-install-btn" onclick="_applyQuickTemplate(\'' + t.key + '\')">Install</button>';
    }
    // subsets get no button — they're already included
    h += '</div>';
    h += '</div>';
  }
  h += '</div>';

  // Community packs
  h += '<div class="wf-avail-section-title">Community Skill Packs</div>';
  h += '<div class="wf-avail-section-desc">Install a pack to clone it to your system and import its skills into your departments.</div>';

  h += '<div class="wf-avail-grid">';
  for (const p of packs) {
    h += '<div class="wf-avail-card">';
    h += '<div class="wf-avail-card-top">';
    h += '<div class="wf-avail-card-icon">' + p.icon + '</div>';
    h += '<div class="wf-avail-card-info">';
    h += '<a href="' + p.url + '" target="_blank" class="wf-avail-card-name">' + _esc(p.name) + '</a>';
    h += '<div class="wf-avail-card-meta">';
    h += '<span class="wf-avail-card-stars"><svg width="11" height="11" viewBox="0 0 24 24" fill="#e3b341" stroke="none"><path d="M12 2l2.4 7.4H22l-6.2 4.5 2.4 7.4L12 16.8l-6.2 4.5 2.4-7.4L2 9.4h7.6z"/></svg> ' + p.stars + '</span>';
    h += '<span class="wf-avail-card-count">' + p.count + '</span>';
    h += '</div>';
    h += '</div>';
    h += '</div>';
    h += '<div class="wf-avail-card-desc">' + _esc(p.desc) + '</div>';
    // Check if pack is already installed (assets with this pack prefix exist)
    const packInstalled = typeof FOLDER_SUPERSET === 'object' && FOLDER_SUPERSET && Object.keys(FOLDER_SUPERSET).some(k => k.startsWith(p.id + '-'));
    h += '<div class="wf-avail-card-actions">';
    if (packInstalled) {
      h += '<span class="wf-config-source-badge">Installed</span>';
      h += '<button class="wf-avail-uninstall-btn" onclick="_uninstallPack(\'' + _esc(p.id) + '\')">Uninstall</button>';
    } else {
      h += '<button class="wf-avail-install-btn" onclick="_installPack(\'' + _esc(p.id) + '\')">';
      h += '<svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round"><path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"/><polyline points="7 10 12 15 17 10"/><line x1="12" y1="15" x2="12" y2="3"/></svg>';
      h += ' Install</button>';
    }
    h += '<a href="' + p.url + '" target="_blank" class="wf-avail-view-btn">View on GitHub</a>';
    h += '</div>';
    h += '</div>';
  }
  h += '</div>';

  // System scan CTA
  h += '<div class="wf-avail-system-cta">';
  h += '<div>';
  h += '<div class="wf-avail-system-cta-title">Already have agents or skills installed?</div>';
  h += '<div class="wf-avail-system-cta-desc">Scan <code>~/.claude/agents/</code> and <code>~/.claude/skills/</code> for definitions to import into your departments.</div>';
  h += '</div>';
  h += '<button class="wf-avail-scan-btn" onclick="_setWsConfigTab(\'discovery\')">';
  h += '<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><circle cx="11" cy="11" r="8"/><line x1="21" y1="21" x2="16.65" y2="16.65"/></svg>';
  h += ' Scan &amp; Import</button>';
  h += '</div>';

  return h;
}

// ---- Tab 3: Discovery ----
function _renderConfigDiscovery() {
  let h = '';

  h += '<div class="wf-avail-section-desc" style="margin-bottom:16px;">Scans your system for agent and skill definitions in <code>~/.claude/agents/</code> and <code>~/.claude/skills/</code>. Use this tab to import custom skill packs or agent files you\'ve installed manually.</div>';

  if (!_wsDiscoveryCache) {
    h += '<div class="wf-config-discovery-loading">';
    h += '<div class="planner-spinner"></div>';
    h += '<span>Scanning filesystem for agent and skill definitions...</span>';
    h += '</div>';
    _runDiscoveryScan();
    return h;
  }

  const discovered = _wsDiscoveryCache;
  if (!discovered.length) {
    h += '<div class="wf-config-empty">No agent or skill definitions found on your system. Install a skill pack (like gstack) or create agents in <code>~/.claude/agents/</code>.</div>';
    return h;
  }

  // Group by source/pack
  const groups = {};
  for (const d of discovered) {
    const key = d.pack || d.source || 'unknown';
    if (!groups[key]) groups[key] = { label: d.pack || d.source, items: [] };
    groups[key].items.push(d);
  }

  for (const [key, group] of Object.entries(groups)) {
    h += '<div class="wf-config-section">';
    h += '<div class="wf-config-section-title">' + _esc(group.label) + ' <span class="wf-config-section-count">' + group.items.length + ' asset' + (group.items.length !== 1 ? 's' : '') + '</span></div>';
    h += '<div class="wf-config-discovery-list">';
    for (const item of group.items) {
      const imported = item.already_imported;
      h += '<div class="wf-config-discovery-item' + (imported ? ' imported' : '') + '">';
      h += '<div class="wf-config-discovery-item-header">';
      h += '<span class="wf-config-agent-name">' + _esc(item.name) + '</span>';
      h += '<span class="wf-config-tier-badge wf-tier-' + item.tier + '">' + item.tier + '</span>';
      if (imported) {
        h += '<span class="wf-config-imported-badge">&#10003; Imported</span>';
        h += '<button class="wf-avail-uninstall-btn" style="padding:3px 8px;font-size:11px;" onclick="_removeDiscoveredAsset(\'' + _esc(item.id) + '\')">Remove</button>';
      } else {
        h += '<button class="wf-config-import-btn" onclick="_importDiscoveredAsset(\'' + _esc(item.id) + '\')">Import</button>';
      }
      h += '</div>';
      if (item.systemPrompt) {
        h += '<div class="wf-config-discovery-preview">' + _esc(item.systemPrompt.substring(0, 120)) + (item.systemPrompt.length > 120 ? '...' : '') + '</div>';
      }
      h += '</div>';
    }
    h += '</div>';
    h += '</div>';
  }

  h += '<div style="display:flex;gap:8px;margin-top:16px;padding:0 16px;">';
  h += '<button class="wf-config-add-dept" onclick="_runDiscoveryScan(true)">&#8635; Rescan</button>';
  h += '<button class="wf-config-organize-btn" onclick="_openFinderAI()"><svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><circle cx="11" cy="11" r="8"/><line x1="21" y1="21" x2="16.65" y2="16.65"/></svg> Find More with AI</button>';
  h += '</div>';
  return h;
}

async function _runDiscoveryScan(force) {
  if (force) _wsDiscoveryCache = null;
  try {
    const resp = await fetch('/api/workforce/discover');
    if (!resp.ok) { _wsDiscoveryCache = []; filterSessions(); return; }
    const data = await resp.json();
    _wsDiscoveryCache = data.ok ? (data.discovered || []) : [];
  } catch (e) {
    console.warn('[workforce] Discovery scan failed:', e);
    _wsDiscoveryCache = [];
  }
  filterSessions();
}

async function _importDiscoveredAsset(id) {
  const item = (_wsDiscoveryCache || []).find(d => d.id === id);
  if (!item) { showToast('Asset not found'); return; }

  // Write the .md file to workforce/
  const fm = item.frontmatter || {};
  let content = '---\n';
  content += 'id: ' + item.id + '\n';
  content += 'name: ' + (fm.name || item.name) + '\n';
  content += 'department: ' + (item.pack || 'Imported')+ '\n';
  if (fm['allowed-tools']) content += 'allowed-tools: ' + JSON.stringify(fm['allowed-tools']) + '\n';
  if (fm.version) content += 'version: ' + fm.version + '\n';
  if (item.pack) content += 'source: ' + item.pack + '\n';
  content += '---\n\n' + (item.systemPrompt || '');

  try {
    const resp = await fetch('/api/workforce/write-asset', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ id: item.id, content: content }),
    });
    if (resp.ok) {
      item.already_imported = true;
      showToast('Imported: ' + item.name);
      filterSessions();
    } else {
      showToast('Import failed');
    }
  } catch (e) {
    showToast('Import failed: ' + e.message);
  }
}

// ---- Remove single imported asset ----
async function _removeDiscoveredAsset(id) {
  const safe = id.replace(/[^a-zA-Z0-9_-]/g, '');
  if (!safe) return;
  try {
    const resp = await fetch('/api/workforce/delete-asset', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ id: safe }),
    });
    if (resp.ok) {
      const item = (_wsDiscoveryCache || []).find(d => d.id === id);
      if (item) item.already_imported = false;
      showToast('Removed: ' + safe);
      filterSessions();
    }
  } catch (e) {
    showToast('Remove failed: ' + e.message);
  }
}

// ---- Uninstall community pack ----
async function _uninstallPack(packId) {
  if (!confirm('Remove all imported assets from "' + packId + '" and delete the cloned directory?')) return;
  try {
    const resp = await fetch('/api/workforce/uninstall-pack', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ pack_id: packId }),
    });
    const data = await resp.json();
    if (data.ok) {
      if (typeof _agentCatalogPath !== 'undefined') { _agentCatalogPath = null; _agentCatalogPromise = null; }
      _wsDiscoveryCache = null;
      showToast('Uninstalled: ' + packId + ' (' + (data.assets_deleted || 0) + ' assets, ' + (data.dir_deleted ? 'directory removed' : 'no directory found') + ')');
      filterSessions();
    } else {
      showToast('Uninstall failed: ' + (data.error || 'unknown'));
    }
  } catch (e) {
    showToast('Uninstall failed: ' + e.message);
  }
}

// ---- Quick template install (deterministic, instant) ----
function _applyQuickTemplate(templateKey) {
  if (typeof FOLDER_TEMPLATES === 'undefined') { showToast('Templates not available'); return; }
  const tmpl = FOLDER_TEMPLATES[templateKey];
  if (!tmpl) { showToast('Template not found'); return; }
  if (typeof initFolderTreeFromTemplate === 'function') {
    initFolderTreeFromTemplate(templateKey);
    showToast('Installed: ' + tmpl.name);
    filterSessions();
  } else {
    showToast('Template system not loaded');
  }
}

// ---- Uninstall built-in library ----
async function _uninstallBuiltin() {
  if (!confirm('Remove all built-in departments and assets? You can reinstall anytime from the Available tab.')) return;
  try {
    const resp = await fetch('/api/workforce/uninstall-builtin', { method: 'POST' });
    if (resp.ok) {
      // Clear folder tree
      await fetch('/api/folder-tree', {
        method: 'PUT',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ version: 1, folders: {}, rootChildren: [] }),
      });
      _folderTreeCache = { version: 1, folders: {}, rootChildren: [] };
      if (typeof _agentCatalogPath !== 'undefined') { _agentCatalogPath = null; _agentCatalogPromise = null; }
      _wsConfigMode = true; // Stay in config mode so user sees empty state
      showToast('Built-in library removed');
      filterSessions();
    } else {
      showToast('Uninstall failed');
    }
  } catch (e) {
    showToast('Uninstall failed: ' + e.message);
  }
}

// ---- Organize AI (My Departments tab) ----
function _openOrganizeAI() {
  _openWfSlideout('organize', 'Organize Departments',
    'Propose a department hierarchy that makes sense for my current setup. Look at what agents I have and suggest how to reorganize them.',
    [
      'You are a department organization assistant for VibeNode.',
      'The user wants you to propose a better hierarchy for their departments.',
      'Read the workforce/ directory and workforce-map.md to understand what exists.',
      'Propose changes to the hierarchy by editing workforce-map.md.',
      'You can also create new .md agent files or reorganize existing ones.',
      'Be concise. Propose the structure, explain why, then make the changes if the user agrees.',
    ].join('\n')
  );
}

// ---- Install Pack (Available tab) ----
async function _installPack(packId) {
  // Find the pack definition
  const packDefs = {
    'gstack': { git: 'https://github.com/garrytan/gstack.git', setup: './setup' },
    'anthropic-skills': { git: 'https://github.com/anthropics/skills.git' },
    'everything-cc': { git: 'https://github.com/affaan-m/everything-claude-code.git' },
    'wshobson-agents': { git: 'https://github.com/wshobson/agents.git' },
    'antigravity': { git: 'https://github.com/sickn33/antigravity-awesome-skills.git' },
    'alirezarezvani-skills': { git: 'https://github.com/alirezarezvani/claude-skills.git' },
  };
  const def = packDefs[packId];
  if (!def) { showToast('Unknown pack: ' + packId); return; }

  // Show inline progress
  const btn = event && event.target ? event.target : null;
  const origText = btn ? btn.innerHTML : '';
  if (btn) { btn.innerHTML = 'Installing...'; btn.disabled = true; }

  try {
    const resp = await fetch('/api/workforce/install-pack', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ pack_id: packId, git_url: def.git, setup_cmd: def.setup || null }),
    });
    const data = await resp.json();
    if (data.ok) {
      if (typeof _agentCatalogPath !== 'undefined') { _agentCatalogPath = null; _agentCatalogPromise = null; }
      _wsDiscoveryCache = null;
      // Add pack as a department in the folder tree + reload FOLDER_SUPERSET from disk
      await _addPackToFolderTree(packId, data.imported || 0);
      if (typeof _loadWorkforceFromDisk === 'function') await _loadWorkforceFromDisk();
      showToast('Installed ' + packId + ': ' + (data.imported || 0) + ' assets imported');
      filterSessions();
    } else {
      showToast('Install failed: ' + (data.error || 'unknown'));
      if (btn) { btn.innerHTML = origText; btn.disabled = false; }
    }
  } catch (e) {
    showToast('Install failed: ' + e.message);
    if (btn) { btn.innerHTML = origText; btn.disabled = false; }
  }
}

// ---- Add imported pack to folder tree ----
async function _addPackToFolderTree(packId) {
  // Get current tree
  let tree;
  try {
    const resp = await fetch('/api/folder-tree');
    if (resp.ok) tree = await resp.json();
  } catch(e) {}
  if (!tree || !tree.folders) tree = { version: 1, folders: {}, rootChildren: [] };

  // Find all FOLDER_SUPERSET keys that start with packId-
  // (they were just loaded by _loadWorkforceFromDisk or will be)
  // For now, read from the workforce assets API
  let packAssetIds = [];
  try {
    const resp = await fetch('/api/workforce/assets');
    if (resp.ok) {
      const data = await resp.json();
      if (data.ok && data.assets) {
        packAssetIds = data.assets
          .filter(a => a.source === packId || a.id.startsWith(packId + '-'))
          .map(a => a.id);
      }
    }
  } catch(e) {}

  if (!packAssetIds.length) return;

  // Create pack department if it doesn't exist
  if (!tree.folders[packId]) {
    tree.folders[packId] = { id: packId, name: packId, children: [], sessions: [] };
    if (!tree.rootChildren) tree.rootChildren = [];
    tree.rootChildren.push(packId);
  }

  // Add child assets
  const existing = new Set((tree.folders[packId].children || []).map(c => typeof c === 'string' ? c : c.id));
  for (const aid of packAssetIds) {
    if (aid === packId) continue; // skip the department itself
    if (existing.has(aid)) continue;
    if (!tree.folders[aid]) {
      tree.folders[aid] = { id: aid, name: aid, children: [], sessions: [] };
    }
    tree.folders[packId].children.push(aid);
  }

  // Save
  try {
    await fetch('/api/folder-tree', {
      method: 'PUT',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(tree),
    });
    _folderTreeCache = tree;
  } catch(e) {}
}

// ---- Discovery AI Finder (Discovery tab) ----
function _openFinderAI() {
  _openWfSlideout('finder', 'Find Skills',
    'Help me find agent and skill definitions that aren\'t currently on my system. I\'m looking for useful skills to add to my departments.',
    [
      'You are a skill discovery assistant for VibeNode.',
      'The user wants to find new agent/skill definitions to import.',
      'You can:',
      '- Search GitHub for repos with Claude Code skills or agent definitions',
      '- Clone repos and scan for .md skill files',
      '- Import found skills into the workforce/ directory',
      '- Help the user understand what skills are available',
      'The workforce directory is at: ' + (typeof _currentProjectDir === 'function' ? _currentProjectDir() : '') + '/workforce/',
      'When importing, create .md files with frontmatter (id, name, department) and update workforce-map.md.',
    ].join('\n')
  );
}

// ---- Shared slide-out panel builder ----
function _openWfSlideout(mode, title, initialPrompt, sysPromptExtra) {
  const existing = document.getElementById('workforce-assistant-panel');
  if (existing) existing.remove();

  const panel = document.createElement('div');
  panel.id = 'workforce-assistant-panel';
  panel.className = 'kanban-planner-panel';

  panel.innerHTML =
    '<div class="kanban-planner-header" onclick="_expandWfAssistantIfMinimized()">' +
      '<span class="kanban-planner-title"><svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" style="vertical-align:-2px;margin-right:4px;"><path d="M12 2l2.4 7.4H22l-6.2 4.5 2.4 7.4L12 16.8l-6.2 4.5 2.4-7.4L2 9.4h7.6z"/></svg>' + _esc(title) + '</span>' +
      '<div style="display:flex;gap:4px;align-items:center;">' +
        '<button class="kanban-planner-close wf-minimize-btn" onclick="event.stopPropagation();_minimizeWfAssistant()">&#x2015;</button>' +
        '<button class="kanban-planner-close" onclick="event.stopPropagation();_closeWfAssistant()">&#215;</button>' +
      '</div>' +
    '</div>' +
    '<div class="planner-body" id="wf-assistant-body">' +
      '<div class="planner-status" id="wf-assistant-status">' +
        '<div class="planner-spinner"></div><span>Working...</span>' +
      '</div>' +
    '</div>' +
    '<div class="planner-footer" id="wf-assistant-footer">' +
      '<div class="planner-refine-row">' +
        '<textarea id="wf-assistant-input" class="kanban-create-textarea" placeholder="Follow up..." rows="2" onkeydown="if(event.key===\'Enter\'&&!event.shiftKey){event.preventDefault();_sendWfAssistantMsg();}"></textarea>' +
        '<button class="live-send-btn" onclick="_sendWfAssistantMsg()" title="Send">' +
          '<svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><line x1="22" y1="2" x2="11" y2="13"/><polygon points="22 2 15 22 11 13 2 9 22 2"/></svg>' +
        '</button>' +
      '</div>' +
    '</div>';

  document.body.appendChild(panel);
  requestAnimationFrame(() => requestAnimationFrame(() => panel.classList.add('open')));

  // Build system prompt with department context
  const tree = (typeof getFolderTree === 'function') ? getFolderTree() : null;
  const deptInfo = [];
  if (tree && tree.rootChildren) {
    for (const rf of tree.rootChildren) {
      const fid = typeof rf === 'string' ? rf : rf.id;
      const folder = tree.folders[fid];
      const def = FOLDER_SUPERSET[fid];
      if (!folder || !def) continue;
      const label = def.skill ? def.skill.label : def.name;
      const kids = (folder.children || []).map(c => {
        const cid = typeof c === 'string' ? c : c.id;
        const cd = FOLDER_SUPERSET[cid];
        return cd ? (cd.skill ? cd.skill.label : cd.name) : cid;
      });
      deptInfo.push(label + ': ' + (kids.length ? kids.join(', ') : '(empty)'));
    }
  }

  const sysPrompt = 'Current departments:\n' + (deptInfo.length ? deptInfo.join('\n') : '(none)') + '\n\n' + sysPromptExtra;

  const newId = crypto.randomUUID();
  _wsConfigAssistantId = newId;
  if (typeof _hiddenSessionIds !== 'undefined') _hiddenSessionIds.add(newId);

  socket.emit('start_session', {
    session_id: newId,
    prompt: initialPrompt,
    cwd: typeof _currentProjectDir === 'function' ? _currentProjectDir() : '',
    system_prompt: sysPrompt,
    max_turns: 5,
    session_type: 'planner',
  });

  _attachWfAssistantListeners();
}

function _closeWfAssistant() {
  const panel = document.getElementById('workforce-assistant-panel');
  if (!panel) return;
  panel.classList.remove('open');
  setTimeout(() => panel.remove(), 300);
  _wsConfigAssistantId = null;
}

function _minimizeWfAssistant() {
  const panel = document.getElementById('workforce-assistant-panel');
  if (!panel) return;
  panel.classList.add('minimized');
  const minBtn = panel.querySelector('.wf-minimize-btn');
  if (minBtn) minBtn.style.display = 'none';
}

function _expandWfAssistantIfMinimized() {
  const panel = document.getElementById('workforce-assistant-panel');
  if (!panel || !panel.classList.contains('minimized')) return;
  panel.classList.remove('minimized');
  const minBtn = panel.querySelector('.wf-minimize-btn');
  if (minBtn) minBtn.style.display = '';
}

function _startWfAssistantSession() {
  const newId = crypto.randomUUID();
  _wsConfigAssistantId = newId;
  if (typeof _hiddenSessionIds !== 'undefined') _hiddenSessionIds.add(newId);

  // Build contextual system prompt
  const tree = (typeof getFolderTree === 'function') ? getFolderTree() : null;
  const deptInfo = [];
  if (tree && tree.rootChildren) {
    for (const rf of tree.rootChildren) {
      const fid = typeof rf === 'string' ? rf : rf.id;
      const folder = tree.folders[fid];
      const def = FOLDER_SUPERSET[fid];
      if (!folder || !def) continue;
      const label = def.skill ? def.skill.label : def.name;
      const kids = (folder.children || []).map(c => {
        const cid = typeof c === 'string' ? c : c.id;
        const cd = FOLDER_SUPERSET[cid];
        return cd ? (cd.skill ? cd.skill.label : cd.name) : cid;
      });
      deptInfo.push(label + ': ' + (kids.length ? kids.join(', ') : '(empty)'));
    }
  }

  const sysPrompt = [
    'You are a workforce configuration assistant for VibeNode.',
    'You help the user manage their department hierarchy and knowledge assets.',
    'Departments contain agents (markdown files with system prompts).',
    'The user\'s current departments:\n' + (deptInfo.length ? deptInfo.join('\n') : '(none configured)'),
    '',
    'You can:',
    '- Create new departments and agents by writing .md files to the workforce/ directory',
    '- Edit the workforce-map.md hierarchy file',
    '- Explain what existing agents do',
    '- Suggest department structures',
    '- Import skills from GitHub repos the user mentions',
    '',
    'The current tab is: ' + _wsConfigTab,
    'When creating agents, use this format for .md files:',
    '---',
    'id: agent-id',
    'name: Agent Name',
    'department: Department Name',
    '---',
    '',
    'Agent instructions here...',
    '',
    'Always respond conversationally. After making changes, tell the user to refresh the view.',
  ].join('\n');

  socket.emit('start_session', {
    session_id: newId,
    prompt: 'Ready to help configure your departments. What would you like to do?',
    cwd: typeof _currentProjectDir === 'function' ? _currentProjectDir() : '',
    system_prompt: sysPrompt,
    max_turns: 3,
    session_type: 'planner',
  });

  // Listen for responses
  _attachWfAssistantListeners();
}

let _wfAssistantEntryListener = null;
let _wfAssistantStateListener = null;
let _wfAssistantAccum = '';
let _wfAssistantTimeout = null;

function _attachWfAssistantListeners() {
  if (_wfAssistantEntryListener) socket.off('session_entry', _wfAssistantEntryListener);
  if (_wfAssistantStateListener) socket.off('session_state', _wfAssistantStateListener);
  if (_wfAssistantTimeout) clearTimeout(_wfAssistantTimeout);

  // Hide spinner once first text arrives
  let _gotFirstText = false;

  _wfAssistantEntryListener = function(data) {
    if (data.session_id !== _wsConfigAssistantId) return;
    if (data.type === 'assistant' && data.message) {
      _wfAssistantAccum += data.message;
      // Hide spinner on first text
      if (!_gotFirstText) {
        _gotFirstText = true;
        const status = document.getElementById('wf-assistant-status');
        if (status) status.style.display = 'none';
      }
      const body = document.getElementById('wf-assistant-body');
      if (body) {
        let respDiv = body.querySelector('.wf-assistant-response:last-child');
        if (!respDiv || respDiv.dataset.final === 'true') {
          respDiv = document.createElement('div');
          respDiv.className = 'wf-assistant-response';
          body.appendChild(respDiv);
        }
        respDiv.innerHTML = '<div style="padding:12px 16px;font-size:13px;color:var(--text-primary);white-space:pre-wrap;line-height:1.5;">' + _esc(_wfAssistantAccum) + '</div>';
        body.scrollTop = body.scrollHeight;
      }
    }
  };

  _wfAssistantStateListener = function(data) {
    if (data.session_id !== _wsConfigAssistantId) return;
    if (data.state === 'idle' || data.state === 'sleeping') {
      // Session finished — hide spinner, show completion
      const status = document.getElementById('wf-assistant-status');
      if (status) status.style.display = 'none';
      if (!_wfAssistantAccum) {
        // No text received — show error
        const body = document.getElementById('wf-assistant-body');
        if (body) {
          body.innerHTML = '<div style="padding:16px;color:var(--text-faint);font-size:13px;">Session completed but no response was received. The assistant may have timed out. Try again or use a simpler request.</div>';
        }
      }
    }
  };

  socket.on('session_entry', _wfAssistantEntryListener);
  socket.on('session_state', _wfAssistantStateListener);

  // Timeout fallback — if nothing happens in 30s, show error
  _wfAssistantTimeout = setTimeout(function() {
    if (!_wfAssistantAccum && _wsConfigAssistantId) {
      const status = document.getElementById('wf-assistant-status');
      if (status) {
        status.innerHTML = '<span style="color:var(--text-faint);">Taking longer than expected. The assistant is still working — you can wait or close and try again.</span>';
      }
    }
  }, 30000);
}

function _sendWfAssistantMsg() {
  const input = document.getElementById('wf-assistant-input');
  if (!input) return;
  const text = input.value.trim();
  if (!text) return;
  input.value = '';

  // Mark previous response as final
  const body = document.getElementById('wf-assistant-body');
  if (body) {
    const lastResp = body.querySelector('.wf-assistant-response:last-child');
    if (lastResp) lastResp.dataset.final = 'true';
    // Add user message bubble
    const userDiv = document.createElement('div');
    userDiv.className = 'wf-assistant-user-msg';
    userDiv.innerHTML = '<div style="padding:8px 16px;font-size:13px;color:var(--accent);font-weight:500;">' + _esc(text) + '</div>';
    body.appendChild(userDiv);
    body.scrollTop = body.scrollHeight;
  }

  _wfAssistantAccum = '';

  if (!_wsConfigAssistantId) {
    _startWfAssistantSession();
    // Wait a beat then send
    setTimeout(() => {
      socket.emit('send_message', { session_id: _wsConfigAssistantId, message: text });
    }, 500);
  } else {
    socket.emit('send_message', { session_id: _wsConfigAssistantId, message: text });
  }
}
