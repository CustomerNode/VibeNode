/* workspace.js — Workplace view: draggable session cards + unified permission panel */
/* State variables (workspaceActive, permissionQueue, etc.) are declared in app.js */

let _wsDragId = null;
let _wsFolderDragId = null;
let _archivedExpanded = false;

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
  if (tree) {
    _renderHierarchicalWorkspace(mainBody, sessions, tree);
  } else {
    _renderFlatWorkspace(mainBody, sessions);  // existing behavior
  }

  // Permission panel goes in sidebar (unchanged)
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

  let html = '<div class="ws-container">';

  // Breadcrumb bar
  html += _buildBreadcrumbs(tree, currentId);

  // Child folders of current level
  const childFolders = (typeof getCurrentFolderChildren === 'function')
    ? getCurrentFolderChildren()
    : _getChildFoldersFallback(tree, currentId);

  if (childFolders.length) {
    html += '<div class="ws-section-label">Folders</div>';
    html += '<div class="ws-canvas">';
    for (const fid of childFolders) {
      html += _buildFolderCard(tree, fid);
    }
    html += '</div>';
  }

  // Session cards for current folder
  let folderSessionIds;
  if (isRoot) {
    folderSessionIds = tree.rootSessions || [];
  } else {
    folderSessionIds = (typeof getCurrentFolderSessions === 'function')
      ? getCurrentFolderSessions()
      : _getFolderSessionsFallback(tree, currentId);
  }

  const folderSessions = folderSessionIds
    .map(sid => sessionMap[sid])
    .filter(s => s && !workspaceHiddenSessions.has(s.id));

  // Sort sessions same as flat view
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

  if (folderSessions.length) {
    if (childFolders.length) {
      html += '<div class="ws-section-label">Sessions</div>';
    }
    html += '<div class="ws-canvas">';
    html += _buildSessionCardsHtml(folderSessions);
    html += '</div>';
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

  // Empty state — no folders and no sessions
  if (!childFolders.length && !folderSessions.length) {
    html += `<div class="ws-empty-folder">
      <svg width="48" height="48" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1" stroke-linecap="round" style="opacity:0.3;">
        <path d="M22 19a2 2 0 0 1-2 2H4a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h5l2 3h9a2 2 0 0 1 2 2z"/>
      </svg>
      <div>This folder is empty</div>
      <div style="font-size:11px;color:var(--text-faint);">Drag sessions here or create sub-folders</div>
    </div>`;
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
function _buildBreadcrumbs(tree, currentId) {
  const isRoot = !currentId;

  if (isRoot) {
    return '<div class="ws-breadcrumbs"><span class="ws-crumb-current" style="opacity:0.5;">Root</span></div>';
  }

  const crumbs = (typeof getBreadcrumbs === 'function')
    ? getBreadcrumbs()
    : _buildBreadcrumbsFallback(tree, currentId);

  let html = '<div class="ws-breadcrumbs">';

  // Root crumb (always clickable when not at root)
  html += '<span class="ws-crumb" onclick="navigateToFolder(null)" ondragover="event.preventDefault()" ondrop="_wsDropOnCrumb(event, null)">Root</span>';

  for (let i = 0; i < crumbs.length; i++) {
    html += '<span class="ws-crumb-sep">&rsaquo;</span>';
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
      html += '<span class="ws-crumb" onclick="navigateToFolder(\'' + c.id + '\')" ondragover="event.preventDefault()" ondrop="_wsDropOnCrumb(event, \'' + c.id + '\')">' + escHtml(c.name) + '</span>';
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
  if (childCount) countParts.push(childCount + ' folder' + (childCount !== 1 ? 's' : ''));
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
    ${countsText ? '<div class="ws-folder-counts">' + countsText + '</div>' : ''}
    ${statusHtml}
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
function navigateToFolder(folderId) {
  _currentFolderId = folderId || null;
  filterSessions();
}

// ---- Permission panel ----
function _buildPermissionPanel() {
  const policyIcons = {
    manual: '<svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><path d="M12 22s8-4 8-10V5l-8-3-8 3v7c0 6 8 10 8 10z"/></svg>',
    auto: '<svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><polygon points="13 2 3 14 12 14 11 22 21 10 12 10 13 2"/></svg>',
    custom: '<svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><circle cx="12" cy="12" r="3"/><path d="M19.4 15a1.65 1.65 0 0 0 .33 1.82l.06.06a2 2 0 0 1-2.83 2.83l-.06-.06a1.65 1.65 0 0 0-1.82-.33 1.65 1.65 0 0 0-1 1.51V21a2 2 0 0 1-4 0v-.09A1.65 1.65 0 0 0 9 19.4a1.65 1.65 0 0 0-1.82.33l-.06.06a2 2 0 0 1-2.83-2.83l.06-.06A1.65 1.65 0 0 0 4.68 15a1.65 1.65 0 0 0-1.51-1H3a2 2 0 0 1 0-4h.09A1.65 1.65 0 0 0 4.6 9"/></svg>',
  };
  const policyLabels = {auto: 'Auto-Approve', manual: 'Manual', custom: 'Custom Rules'};

  let headerHtml = `<div class="ws-perm-header">
    <span class="ws-perm-title">Permissions</span>
    <button class="ws-policy-btn" onclick="openPermissionPolicySelector()">
      <span class="ws-policy-indicator ws-policy-${permissionPolicy}">${policyIcons[permissionPolicy] || ''} ${policyLabels[permissionPolicy] || 'Manual'}</span>
      <svg width="10" height="10" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" style="opacity:0.5;"><polyline points="6 9 12 15 18 9"/></svg>
    </button>
  </div>`;

  let rowsHtml = '';
  if (permissionQueue.length === 0) {
    rowsHtml = '<div class="ws-perm-empty">No pending permission requests</div>';
  } else {
    rowsHtml = permissionQueue.map(entry => {
      const s = allSessions.find(x => x.id === entry.sessionId);
      const name = s ? escHtml((s.display_title||'').slice(0,30)) : entry.sessionId.slice(0,8);
      const toolDisplay = entry.toolName ? escHtml(entry.toolName) : 'unknown';
      const cmdDisplay = entry.command ? escHtml(entry.command.slice(0,80)) : '';
      return `<div class="ws-perm-row" data-sid="${entry.sessionId}">
        <div class="ws-perm-session">${name}</div>
        <div class="ws-perm-detail">
          <span class="ws-perm-tool">${toolDisplay}</span>
          ${cmdDisplay ? '<span class="ws-perm-cmd">' + cmdDisplay + '</span>' : ''}
        </div>
        <div class="ws-perm-actions">
          <button class="ws-perm-btn ws-perm-allow" onclick="wsPermissionAnswer('${entry.sessionId}','y')" title="Allow">Allow</button>
          <button class="ws-perm-btn ws-perm-deny" onclick="wsPermissionAnswer('${entry.sessionId}','n')" title="Deny">Deny</button>
          <button class="ws-perm-btn ws-perm-always" onclick="wsPermissionAnswer('${entry.sessionId}','a')" title="Always Allow">Always</button>
        </div>
      </div>`;
    }).join('');
  }

  return `<div class="ws-perm-panel">
    ${headerHtml}
    <div class="ws-perm-body">${rowsHtml}</div>
  </div>`;
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
  if (sidebarPermEl && sidebarPermEl.style.display !== 'none') {
    sidebarPermEl.innerHTML = _buildPermissionPanel();
  }

  // When a card is expanded, renderWorkspace() skips, so do incremental update
  if (_wsExpandedId) {
    const panel = document.querySelector('.ws-perm-panel');
    if (panel) {
      const temp = document.createElement('div');
      temp.innerHTML = _buildPermissionPanel();
      const newPanel = temp.firstElementChild;
      panel.replaceWith(newPanel);
    }
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
    const panel = document.querySelector('.ws-perm-panel');
    if (panel) {
      const temp = document.createElement('div');
      temp.innerHTML = _buildPermissionPanel();
      panel.replaceWith(temp.firstElementChild);
    }
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

  // Re-render panel
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
    <div class="ws-ctx-item" onclick="_wsCtxAddSub('${folderId}')">Add Sub-folder</div>
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
    placeholder: 'Folder name',
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
  var name = await showPrompt('Add Sub-folder', '', {
    placeholder: 'Folder name',
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
  var ok = await showConfirm('Delete Folder', `<p>Delete <strong>${escHtml(folder.name)}</strong>?</p><p>Sub-folders and sessions will be moved to the parent.</p>`, {
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
