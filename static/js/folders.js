/* folders.js — FolderTree data model, CRUD, navigation, server-side persistence */

// ── Current navigation state ───────────────────────────────────────────
let _currentFolderId = null;  // null = root

// ── In-memory cache + server persistence ──────────────────────────────
let _folderTreeCache = null;  // in-memory cache; null = not loaded yet
let _saveDebounceTimer = null;
var _SAVE_DEBOUNCE_MS = 500;

function _loadFolderTree() {
  // Synchronous read from in-memory cache (populated by initFolderTree)
  return _folderTreeCache;
}

function _saveFolderTree(tree) {
  // Update in-memory cache immediately
  _folderTreeCache = tree;
  // Debounced persist to server
  _debouncedServerSave(tree);
}

function _debouncedServerSave(tree) {
  if (_saveDebounceTimer) clearTimeout(_saveDebounceTimer);
  _saveDebounceTimer = setTimeout(function() {
    _saveDebounceTimer = null;
    _persistToServer(tree);
  }, _SAVE_DEBOUNCE_MS);
}

function _persistToServer(tree) {
  fetch('/api/folder-tree', {
    method: 'PUT',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(tree),
  }).catch(function(err) {
    console.error('Failed to save folder tree to server', err);
  });
}

// Public accessors
function loadFolderTree() { return _loadFolderTree(); }
function saveFolderTree(tree) { _saveFolderTree(tree); }
function getFolderTree() { return _loadFolderTree(); }

// ── Initialize from server ────────────────────────────────────────────

async function initFolderTree() {
  try {
    var resp = await fetch('/api/folder-tree');
    if (resp.ok) {
      var data = await resp.json();
      if (data && typeof data === 'object' && (data.version || data.activeTemplate || data.folders)) {
        _folderTreeCache = data;
        return data;
      }
    }
  } catch (e) {
    console.error('Failed to load folder tree from server', e);
  }
  // No tree on server — start with empty tree (templates available in Configure mode)
  _folderTreeCache = null;
  return null;
}

function initFolderTreeFromTemplate(templateKey) {
  return _applyTemplate(templateKey);
}

// ── Template selector popup ────────────────────────────────────────────

function showTemplateSelector(onComplete) {
  _showTemplateSelector(onComplete);
}

function _showTemplateSelector(onComplete) {
  var overlay = document.getElementById('pm-overlay');

  var templateIcon = '<svg width="24" height="24" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"><path d="M22 19a2 2 0 0 1-2 2H4a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h5l2 3h9a2 2 0 0 1 2 2z"/></svg>';

  var tmplEntries = Object.entries(FOLDER_TEMPLATES);
  var cardsHtml = '';
  for (var i = 0; i < tmplEntries.length; i++) {
    var key = tmplEntries[i][0];
    var tmpl = tmplEntries[i][1];
    var badge = escHtml(tmpl.count);
    if (key === 'small-team') badge += '</span> <span style="font-size:9px;background:var(--accent);color:#fff;padding:2px 6px;border-radius:8px;font-weight:700;margin-left:4px;">Recommended';
    cardsHtml += '<div class="add-mode-card' + (key === 'small-team' ? ' active' : '') + '" data-tmpl="' + key + '">'
      + '<div class="add-mode-icon">' + templateIcon + '</div>'
      + '<div class="add-mode-info">'
      + '<div class="add-mode-title">' + escHtml(tmpl.name)
      + ' <span style="font-size:9px;background:var(--border);color:var(--text-muted);padding:2px 6px;border-radius:8px;font-weight:600;margin-left:6px;">' + badge + '</span></div>'
      + '<div class="add-mode-desc">' + escHtml(tmpl.desc) + '</div>'
      + '</div></div>';
  }

  overlay.innerHTML = '<div class="pm-card pm-enter" style="width:420px;">'
    + '<h2 class="pm-title">Choose a Template</h2>'
    + '<div class="pm-body"><p>Select a folder template for your workplace. You can add or remove departments later.</p></div>'
    + '<div style="display:flex;flex-direction:column;gap:8px;margin-bottom:20px;">'
    + cardsHtml
    + '</div>'
    + '</div>';
  overlay.classList.add('show');
  requestAnimationFrame(function() {
    var card = overlay.querySelector('.pm-card');
    if (card) card.classList.remove('pm-enter');
  });

  // Click outside to dismiss (applies empty/no template)
  overlay.onclick = function(e) {
    if (e.target === overlay) {
      _closePm();
      if (typeof onComplete === 'function') onComplete();
    }
  };

  overlay.querySelectorAll('.add-mode-card').forEach(function(card) {
    card.onclick = function() {
      var tmplKey = card.dataset.tmpl;
      _closePm();
      var tree = _applyTemplate(tmplKey);
      if (tree) {
        showToast('Template "' + (FOLDER_TEMPLATES[tmplKey] || {}).name + '" applied');
        if (typeof onComplete === 'function') onComplete();
        filterSessions();
      }
    };
  });
}

// ── Apply template (create tree from scratch) ──────────────────────────

function _applyTemplate(templateKey) {
  var tmpl = FOLDER_TEMPLATES[templateKey];
  if (!tmpl) return null;

  var tree = {
    version: 1,
    activeTemplate: templateKey,
    rootChildren: [],
    rootSessions: [],
    folders: {},
    archivedFolders: [],
  };

  // keys === null means all superset keys (enterprise template)
  var folderIds = tmpl.keys === null ? Object.keys(FOLDER_SUPERSET) : (tmpl.keys || []);
  var folderIdSet = new Set(folderIds);

  for (var i = 0; i < folderIds.length; i++) {
    var id = folderIds[i];
    var def = FOLDER_SUPERSET[id];
    if (!def) continue;

    tree.folders[id] = {
      id: id,
      name: def.name,
      skill: def.skill ? { label: def.skill.label, systemPrompt: def.skill.systemPrompt, icon: '' } : null,
      parentId: def.parentId && folderIdSet.has(def.parentId) ? def.parentId : null,
      children: (def.children || []).filter(function(c) { return folderIdSet.has(c); }),
      sessions: [],
      collapsed: false,
      isCustom: false,
      createdAt: Date.now(),
      _supersetKey: id,
    };

    // Root children = folders whose parent is not in the template
    if (!def.parentId || !folderIdSet.has(def.parentId)) {
      tree.rootChildren.push(id);
    }
  }

  _saveFolderTree(tree);
  return tree;
}

// ── CRUD operations ────────────────────────────────────────────────────

function createFolder(parentId, name, skill) {
  var tree = _loadFolderTree();
  if (!tree) return null;

  var id = crypto.randomUUID();
  tree.folders[id] = {
    id: id,
    name: name,
    skill: skill || null,
    parentId: parentId || null,
    children: [],
    sessions: [],
    collapsed: false,
    isCustom: true,
    createdAt: Date.now(),
  };

  if (parentId && tree.folders[parentId]) {
    tree.folders[parentId].children.push(id);
  } else {
    tree.rootChildren.push(id);
  }

  _saveFolderTree(tree);
  return id;
}

function renameFolder(folderId, newName) {
  var tree = _loadFolderTree();
  if (!tree) return false;

  var folder = tree.folders[folderId];
  if (!folder) return false;

  folder.name = newName;
  _saveFolderTree(tree);
  return true;
}

function editFolderSkill(folderId, skill) {
  return updateFolderSkill(folderId, skill);
}

function updateFolderSkill(folderId, skill) {
  var tree = _loadFolderTree();
  if (!tree) return false;

  var folder = tree.folders[folderId];
  if (!folder) return false;

  folder.skill = skill || null;
  _saveFolderTree(tree);
  return true;
}

function deleteFolder(folderId) {
  var tree = _loadFolderTree();
  if (!tree) return false;

  var folder = tree.folders[folderId];
  if (!folder) return false;

  var parentId = folder.parentId;

  // Move children to parent (or root)
  for (var i = 0; i < folder.children.length; i++) {
    var childId = folder.children[i];
    var child = tree.folders[childId];
    if (!child) continue;
    child.parentId = parentId;
    if (parentId && tree.folders[parentId]) {
      tree.folders[parentId].children.push(childId);
    } else {
      tree.rootChildren.push(childId);
    }
  }

  // Move sessions to parent (or rootSessions)
  for (var j = 0; j < folder.sessions.length; j++) {
    var sid = folder.sessions[j];
    if (parentId && tree.folders[parentId]) {
      tree.folders[parentId].sessions.push(sid);
    } else {
      tree.rootSessions.push(sid);
    }
  }

  // Remove from parent's children array
  if (parentId && tree.folders[parentId]) {
    tree.folders[parentId].children = tree.folders[parentId].children.filter(function(id) {
      return id !== folderId;
    });
  } else {
    tree.rootChildren = tree.rootChildren.filter(function(id) {
      return id !== folderId;
    });
  }

  // Remove the folder itself
  delete tree.folders[folderId];

  // If navigated into deleted folder, go to parent
  if (_currentFolderId === folderId) {
    _currentFolderId = parentId || null;
  }

  _saveFolderTree(tree);
  return true;
}

// ── Navigation ─────────────────────────────────────────────────────────
// Note: navigateToFolder() is defined in workspace.js (loaded after this file).

function getCurrentFolder() {
  return _currentFolderId;
}

function getBreadcrumbs() {
  var tree = _loadFolderTree();
  if (!tree || !_currentFolderId) return [{ id: null, name: 'Root' }];

  var crumbs = [];
  var id = _currentFolderId;
  var safetyLimit = 50;
  while (id && safetyLimit-- > 0) {
    var f = tree.folders[id];
    if (!f) break;
    crumbs.unshift({ id: f.id, name: f.name });
    id = f.parentId;
  }
  crumbs.unshift({ id: null, name: 'Root' });
  return crumbs;
}

// ── Skill inheritance (walk up ancestors) ──────────────────────────────

function getEffectiveSkill(folderId) {
  var tree = _loadFolderTree();
  if (!tree) return null;

  var id = folderId;
  var safetyLimit = 50;
  while (id && safetyLimit-- > 0) {
    var f = tree.folders[id];
    if (!f) return null;
    if (f.skill) return f.skill;
    id = f.parentId;
  }
  return null;
}

// Alias used by app.js
function getFolderSkill(folderId) {
  return getEffectiveSkill(folderId);
}

// ── Session management in folders ──────────────────────────────────────

function addSessionToFolder(sessionId, folderId) {
  return assignSessionToFolder(sessionId, folderId);
}

function assignSessionToFolder(sessionId, folderId) {
  var tree = _loadFolderTree();
  if (!tree) return false;

  if (folderId && !tree.folders[folderId]) return false;

  // Remove from rootSessions
  tree.rootSessions = tree.rootSessions.filter(function(id) { return id !== sessionId; });

  // Remove from any folder's sessions
  var folders = Object.values(tree.folders);
  for (var i = 0; i < folders.length; i++) {
    folders[i].sessions = folders[i].sessions.filter(function(id) { return id !== sessionId; });
  }

  // Add to target
  if (folderId && tree.folders[folderId]) {
    tree.folders[folderId].sessions.push(sessionId);
  } else {
    tree.rootSessions.push(sessionId);
  }

  _saveFolderTree(tree);
  return true;
}

function _remapSessionInFolders(oldId, newId) {
  var tree = _loadFolderTree();
  if (!tree) return;
  // Replace in rootSessions
  tree.rootSessions = tree.rootSessions.map(function(id) { return id === oldId ? newId : id; });
  // Replace in each folder's sessions
  var folders = Object.values(tree.folders);
  for (var i = 0; i < folders.length; i++) {
    folders[i].sessions = folders[i].sessions.map(function(id) { return id === oldId ? newId : id; });
  }
  _saveFolderTree(tree);
}

function removeSessionFromFolder(sessionId) {
  return unassignSession(sessionId);
}
function removeSessionFromAllFolders(sessionId) {
  return unassignSession(sessionId);
}

function unassignSession(sessionId) {
  var tree = _loadFolderTree();
  if (!tree) return false;

  // Remove from any folder's sessions
  var folders = Object.values(tree.folders);
  for (var i = 0; i < folders.length; i++) {
    var idx = folders[i].sessions.indexOf(sessionId);
    if (idx !== -1) {
      folders[i].sessions.splice(idx, 1);
      break;
    }
  }

  // Also remove from rootSessions (deduplicate)
  tree.rootSessions = tree.rootSessions.filter(function(id) { return id !== sessionId; });

  // Add to rootSessions
  tree.rootSessions.push(sessionId);

  _saveFolderTree(tree);
  return true;
}

function moveSessionToFolder(sessionId, targetFolderId) {
  removeSessionFromFolder(sessionId);
  addSessionToFolder(sessionId, targetFolderId);
}

function getSessionFolder(sessionId) {
  var tree = _loadFolderTree();
  if (!tree) return null;

  var folders = Object.values(tree.folders);
  for (var i = 0; i < folders.length; i++) {
    if (folders[i].sessions.indexOf(sessionId) !== -1) {
      return folders[i].id;
    }
  }
  return null;
}

// ── Get sessions/children for current folder view ──────────────────────

function getCurrentFolderSessions() {
  var tree = _loadFolderTree();
  if (!tree) return [];
  if (!_currentFolderId) return tree.rootSessions || [];
  var folder = tree.folders[_currentFolderId];
  return folder ? folder.sessions || [] : [];
}

function getCurrentFolderChildren() {
  var tree = _loadFolderTree();
  if (!tree) return [];
  if (!_currentFolderId) {
    return (tree.rootChildren || []).map(function(id) { return tree.folders[id]; }).filter(Boolean);
  }
  var folder = tree.folders[_currentFolderId];
  if (!folder) return [];
  return folder.children.map(function(id) { return tree.folders[id]; }).filter(Boolean);
}

function getFolderChildren(folderId) {
  var tree = _loadFolderTree();
  if (!tree) return { folders: [], sessions: [] };

  if (!folderId) {
    var rootFolders = (tree.rootChildren || [])
      .map(function(id) { return tree.folders[id]; })
      .filter(Boolean);
    return { folders: rootFolders, sessions: tree.rootSessions || [] };
  }

  var folder = tree.folders[folderId];
  if (!folder) return { folders: [], sessions: [] };

  var childFolders = folder.children
    .map(function(id) { return tree.folders[id]; })
    .filter(Boolean);
  return { folders: childFolders, sessions: folder.sessions || [] };
}

function getFolder(folderId) {
  var tree = _loadFolderTree();
  if (!tree) return null;
  return tree.folders[folderId] || null;
}

// ── Recursive status counts ────────────────────────────────────────────

function getFolderStatusCounts(folderId) {
  var tree = _loadFolderTree();
  if (!tree) return { working: 0, question: 0, idle: 0, sleeping: 0, totalSessions: 0, totalFolders: 0 };

  var counts = { working: 0, question: 0, idle: 0, sleeping: 0, totalSessions: 0, totalFolders: 0 };

  function _countRecursive(id) {
    var folder = tree.folders[id];
    if (!folder) return;

    counts.totalFolders++;

    // Count sessions in this folder
    for (var i = 0; i < folder.sessions.length; i++) {
      counts.totalSessions++;
      var status = getSessionStatus(folder.sessions[i]);
      if (status === 'working') counts.working++;
      else if (status === 'question') counts.question++;
      else if (status === 'idle') counts.idle++;
      else counts.sleeping++;
    }

    // Recurse into children
    for (var j = 0; j < folder.children.length; j++) {
      _countRecursive(folder.children[j]);
    }
  }

  _countRecursive(folderId);
  return counts;
}

// ── Folder move / reparent ─────────────────────────────────────────────

function moveFolder(folderId, newParentId) {
  return _reparentFolder(folderId, newParentId);
}

// Alias used by workspace.js
function reparentFolder(folderId, newParentId) {
  return _reparentFolder(folderId, newParentId);
}

function _reparentFolder(folderId, newParentId) {
  var tree = _loadFolderTree();
  if (!tree) return false;

  var folder = tree.folders[folderId];
  if (!folder) return false;

  // Cannot move to self
  if (folderId === newParentId) return false;

  // Cycle prevention: walk up from newParentId; if we hit folderId, block
  if (newParentId) {
    var checkId = newParentId;
    var safetyLimit = 50;
    while (checkId && safetyLimit-- > 0) {
      if (checkId === folderId) return false;  // would create a cycle
      var parent = tree.folders[checkId];
      if (!parent) break;
      checkId = parent.parentId;
    }
  }

  // Validate new parent exists (if not null)
  if (newParentId && !tree.folders[newParentId]) return false;

  var oldParentId = folder.parentId;

  // Remove from old parent's children
  if (oldParentId && tree.folders[oldParentId]) {
    tree.folders[oldParentId].children = tree.folders[oldParentId].children.filter(function(id) {
      return id !== folderId;
    });
  } else {
    tree.rootChildren = tree.rootChildren.filter(function(id) {
      return id !== folderId;
    });
  }

  // Add to new parent's children
  folder.parentId = newParentId || null;
  if (newParentId && tree.folders[newParentId]) {
    tree.folders[newParentId].children.push(folderId);
  } else {
    tree.rootChildren.push(folderId);
  }

  _saveFolderTree(tree);
  return true;
}

function moveFolderToParent(folderId, newParentId) {
  return _reparentFolder(folderId, newParentId);
}

function isDescendantOf(folderId, ancestorId) {
  var tree = _loadFolderTree();
  if (!tree) return false;
  var id = folderId;
  while (id) {
    if (id === ancestorId) return true;
    var f = tree.folders[id];
    if (!f) return false;
    id = f.parentId;
  }
  return false;
}

// ── Reorder within same parent ─────────────────────────────────────────

function reorderFolder(folderId, targetFolderId) {
  var tree = _loadFolderTree();
  if (!tree) return false;

  var folder = tree.folders[folderId];
  var target = tree.folders[targetFolderId];
  if (!folder || !target) return false;

  // Must share same parent
  if (folder.parentId !== target.parentId) return false;

  var arr = folder.parentId ? tree.folders[folder.parentId].children : tree.rootChildren;
  var fromIdx = arr.indexOf(folderId);
  var toIdx = arr.indexOf(targetFolderId);
  if (fromIdx === -1 || toIdx === -1) return false;

  arr.splice(fromIdx, 1);
  arr.splice(toIdx, 0, folderId);

  _saveFolderTree(tree);
  return true;
}

function reorderSession(sessionId, targetSessionId) {
  var tree = _loadFolderTree();
  if (!tree) return false;

  // Find which array both sessions are in
  var arr = null;

  if (tree.rootSessions.indexOf(sessionId) !== -1 && tree.rootSessions.indexOf(targetSessionId) !== -1) {
    arr = tree.rootSessions;
  } else {
    var folders = Object.values(tree.folders);
    for (var i = 0; i < folders.length; i++) {
      if (folders[i].sessions.indexOf(sessionId) !== -1 && folders[i].sessions.indexOf(targetSessionId) !== -1) {
        arr = folders[i].sessions;
        break;
      }
    }
  }

  if (!arr) return false;

  var fromIdx = arr.indexOf(sessionId);
  var toIdx = arr.indexOf(targetSessionId);
  if (fromIdx === -1 || toIdx === -1) return false;

  arr.splice(fromIdx, 1);
  arr.splice(toIdx, 0, sessionId);

  _saveFolderTree(tree);
  return true;
}

// ── Toggle collapsed state ─────────────────────────────────────────────

function toggleFolderCollapsed(folderId) {
  var tree = _loadFolderTree();
  if (!tree) return false;

  var folder = tree.folders[folderId];
  if (!folder) return false;

  folder.collapsed = !folder.collapsed;
  _saveFolderTree(tree);
  return folder.collapsed;
}

// ── Template switching with archive system ─────────────────────────────

function switchTemplate(newTemplateKey) {
  var newTmpl = FOLDER_TEMPLATES[newTemplateKey];
  if (!newTmpl) return false;

  var tree = _loadFolderTree();
  if (!tree) {
    // No tree, just apply fresh
    _applyTemplate(newTemplateKey);
    return true;
  }

  var newKeys = newTmpl.keys === null ? Object.keys(FOLDER_SUPERSET) : (newTmpl.keys || []);
  var newKeySet = new Set(newKeys);

  // Archive folders that are NOT in the new template (unless isCustom)
  var allFolders = Object.values(tree.folders);
  var toArchive = [];
  for (var i = 0; i < allFolders.length; i++) {
    var f = allFolders[i];
    if (f.isCustom) continue;
    if (f._supersetKey && !newKeySet.has(f._supersetKey)) {
      toArchive.push(f.id);
    }
  }
  for (var j = 0; j < toArchive.length; j++) {
    _archiveFolder(tree, toArchive[j]);
  }

  // Restore archived folders that ARE in the new template
  var toRestore = [];
  var archived = tree.archivedFolders || [];
  for (var k = 0; k < archived.length; k++) {
    if (archived[k]._supersetKey && newKeySet.has(archived[k]._supersetKey)) {
      toRestore.push(archived[k]);
    }
  }
  for (var m = 0; m < toRestore.length; m++) {
    _restoreFromArchive(tree, toRestore[m]);
  }

  // Add new folders from superset that aren't already in tree
  var currentKeys = new Set();
  var currentFolders = Object.values(tree.folders);
  for (var n = 0; n < currentFolders.length; n++) {
    if (currentFolders[n]._supersetKey) currentKeys.add(currentFolders[n]._supersetKey);
  }

  for (var p = 0; p < newKeys.length; p++) {
    var nk = newKeys[p];
    if (currentKeys.has(nk)) continue;
    var def = FOLDER_SUPERSET[nk];
    if (!def) continue;

    // Determine parent: find it in the current tree
    var pId = null;
    if (def.parentId && tree.folders[def.parentId]) {
      pId = def.parentId;
    } else if (def.parentId) {
      var treeFolders = Object.values(tree.folders);
      for (var q = 0; q < treeFolders.length; q++) {
        if (treeFolders[q]._supersetKey === def.parentId) {
          pId = treeFolders[q].id;
          break;
        }
      }
    }

    tree.folders[nk] = {
      id: nk,
      name: def.name,
      skill: def.skill ? { label: def.skill.label, systemPrompt: def.skill.systemPrompt, icon: '' } : null,
      parentId: pId,
      children: [],
      sessions: [],
      collapsed: false,
      isCustom: false,
      createdAt: Date.now(),
      _supersetKey: nk,
    };

    if (pId && tree.folders[pId]) {
      if (tree.folders[pId].children.indexOf(nk) === -1) {
        tree.folders[pId].children.push(nk);
      }
    }
  }

  // Rebuild rootChildren
  tree.rootChildren = Object.values(tree.folders)
    .filter(function(f) { return f.parentId === null; })
    .map(function(f) { return f.id; });

  tree.activeTemplate = newTemplateKey;
  _saveFolderTree(tree);
  return true;
}

// Internal: archive a folder
function _archiveFolder(tree, folderId) {
  var folder = tree.folders[folderId];
  if (!folder) return;

  var parentId = folder.parentId;

  // Move sessions to parent or rootSessions
  for (var i = 0; i < folder.sessions.length; i++) {
    if (parentId && tree.folders[parentId]) {
      tree.folders[parentId].sessions.push(folder.sessions[i]);
    } else {
      tree.rootSessions.push(folder.sessions[i]);
    }
  }

  // Move children to parent or root
  for (var j = 0; j < folder.children.length; j++) {
    var childId = folder.children[j];
    var child = tree.folders[childId];
    if (!child) continue;
    child.parentId = parentId;
    if (parentId && tree.folders[parentId]) {
      tree.folders[parentId].children.push(childId);
    } else {
      tree.rootChildren.push(childId);
    }
  }

  // Remove from parent's children
  if (parentId && tree.folders[parentId]) {
    tree.folders[parentId].children = tree.folders[parentId].children.filter(function(id) {
      return id !== folderId;
    });
  } else {
    tree.rootChildren = tree.rootChildren.filter(function(id) {
      return id !== folderId;
    });
  }

  // Store archived copy
  if (!tree.archivedFolders) tree.archivedFolders = [];
  tree.archivedFolders.push({
    id: folder.id,
    name: folder.name,
    skill: folder.skill,
    isCustom: folder.isCustom,
    createdAt: folder.createdAt,
    _supersetKey: folder._supersetKey,
    archivedAt: Date.now(),
  });

  // Remove from active tree
  delete tree.folders[folderId];

  if (_currentFolderId === folderId) {
    _currentFolderId = parentId || null;
  }
}

// Internal: restore a folder from archive
function _restoreFromArchive(tree, archivedFolder) {
  if (!archivedFolder) return;

  // Remove from archive list
  tree.archivedFolders = (tree.archivedFolders || []).filter(function(af) {
    return af.id !== archivedFolder.id;
  });

  // Determine parent from superset definition
  var parentId = null;
  if (archivedFolder._supersetKey) {
    var def = FOLDER_SUPERSET[archivedFolder._supersetKey];
    if (def && def.parentId) {
      // Try direct ID match first, then _supersetKey match
      if (tree.folders[def.parentId]) {
        parentId = def.parentId;
      } else {
        var treeFolders = Object.values(tree.folders);
        for (var i = 0; i < treeFolders.length; i++) {
          if (treeFolders[i]._supersetKey === def.parentId) {
            parentId = treeFolders[i].id;
            break;
          }
        }
      }
    }
  }

  var id = archivedFolder.id;
  tree.folders[id] = {
    id: id,
    name: archivedFolder.name,
    skill: archivedFolder.skill,
    parentId: parentId,
    children: [],
    sessions: [],
    collapsed: false,
    isCustom: archivedFolder.isCustom || false,
    createdAt: archivedFolder.createdAt || Date.now(),
    _supersetKey: archivedFolder._supersetKey,
  };

  if (parentId && tree.folders[parentId]) {
    tree.folders[parentId].children.push(id);
  } else {
    tree.rootChildren.push(id);
  }
}

// ── Archive operations ─────────────────────────────────────────────────

function restoreArchivedFolder(folderId) {
  var tree = _loadFolderTree();
  if (!tree) return false;

  var archived = (tree.archivedFolders || []).find(function(af) {
    return af.id === folderId;
  });
  if (!archived) return false;

  _restoreFromArchive(tree, archived);

  // Rebuild rootChildren
  tree.rootChildren = Object.values(tree.folders)
    .filter(function(f) { return f.parentId === null; })
    .map(function(f) { return f.id; });

  _saveFolderTree(tree);
  return true;
}

function deleteArchivedFolder(folderId) {
  var tree = _loadFolderTree();
  if (!tree) return false;

  var idx = (tree.archivedFolders || []).findIndex(function(af) {
    return af.id === folderId;
  });
  if (idx === -1) return false;

  tree.archivedFolders.splice(idx, 1);
  _saveFolderTree(tree);
  return true;
}

function getArchivedFolders() {
  var tree = _loadFolderTree();
  if (!tree) return [];
  return tree.archivedFolders || [];
}

// ── Add department from superset ───────────────────────────────────────

function addDepartmentFromSuperset(deptId) {
  return addDepartment(deptId);
}

function addDepartment(supersetKey) {
  var tree = _loadFolderTree();
  if (!tree) return null;

  var def = FOLDER_SUPERSET[supersetKey];
  if (!def) return null;

  // Check if already exists
  if (tree.folders[supersetKey]) return supersetKey;

  // Check and restore from archive if present
  var archIdx = (tree.archivedFolders || []).findIndex(function(af) {
    return af._supersetKey === supersetKey;
  });
  if (archIdx !== -1) {
    var archived = tree.archivedFolders[archIdx];
    _restoreFromArchive(tree, archived);
    tree.rootChildren = Object.values(tree.folders)
      .filter(function(f) { return f.parentId === null; })
      .map(function(f) { return f.id; });
    _saveFolderTree(tree);
    return archived.id;
  }

  // Determine parent in tree
  var parentId = null;
  if (def.parentId && tree.folders[def.parentId]) {
    parentId = def.parentId;
  }

  // Create the department folder
  tree.folders[supersetKey] = {
    id: supersetKey,
    name: def.name,
    skill: def.skill ? { label: def.skill.label, systemPrompt: def.skill.systemPrompt, icon: '' } : null,
    parentId: parentId,
    children: [],
    sessions: [],
    collapsed: false,
    isCustom: false,
    createdAt: Date.now(),
    _supersetKey: supersetKey,
  };

  if (parentId && tree.folders[parentId]) {
    tree.folders[parentId].children.push(supersetKey);
  } else {
    tree.rootChildren.push(supersetKey);
  }

  // Also add all child folders from the superset definition
  var childKeys = def.children || [];
  for (var c = 0; c < childKeys.length; c++) {
    var ck = childKeys[c];
    if (tree.folders[ck]) continue;  // already exists
    var childDef = FOLDER_SUPERSET[ck];
    if (!childDef) continue;

    tree.folders[ck] = {
      id: ck,
      name: childDef.name,
      skill: childDef.skill ? { label: childDef.skill.label, systemPrompt: childDef.skill.systemPrompt, icon: '' } : null,
      parentId: supersetKey,
      children: [],
      sessions: [],
      collapsed: false,
      isCustom: false,
      createdAt: Date.now(),
      _supersetKey: ck,
    };
    tree.folders[supersetKey].children.push(ck);
  }

  // Wire any orphaned children that should be under this department
  var allFolders = Object.values(tree.folders);
  for (var i = 0; i < allFolders.length; i++) {
    var f = allFolders[i];
    if (f.id === supersetKey) continue;
    if (f._supersetKey) {
      var fDef = FOLDER_SUPERSET[f._supersetKey];
      if (fDef && fDef.parentId === supersetKey && f.parentId === null) {
        tree.rootChildren = tree.rootChildren.filter(function(rid) { return rid !== f.id; });
        f.parentId = supersetKey;
        if (tree.folders[supersetKey].children.indexOf(f.id) === -1) {
          tree.folders[supersetKey].children.push(f.id);
        }
      }
    }
  }

  // Rebuild rootChildren
  tree.rootChildren = Object.values(tree.folders)
    .filter(function(f) { return f.parentId === null; })
    .map(function(f) { return f.id; });

  _saveFolderTree(tree);
  return supersetKey;
}

function getAvailableDepartments() {
  var tree = _loadFolderTree();

  // Collect superset keys already in the active tree
  var usedKeys = new Set();
  if (tree) {
    var folders = Object.values(tree.folders);
    for (var i = 0; i < folders.length; i++) {
      if (folders[i]._supersetKey) usedKeys.add(folders[i]._supersetKey);
    }
  }

  // Return superset entries not already in the tree
  var available = [];
  var entries = Object.entries(FOLDER_SUPERSET);
  for (var j = 0; j < entries.length; j++) {
    var key = entries[j][0];
    var def = entries[j][1];
    if (!usedKeys.has(key)) {
      available.push({
        key: key,
        name: def.name,
        parentId: def.parentId,
        skill: def.skill,
      });
    }
  }
  return available;
}

// ── Prune orphan sessions ──────────────────────────────────────────────

function pruneOrphanSessions(validSessionIds) {
  var tree = _loadFolderTree();
  if (!tree) return;

  var valid = new Set(validSessionIds);
  var changed = false;

  var origRootLen = tree.rootSessions.length;
  tree.rootSessions = tree.rootSessions.filter(function(id) { return valid.has(id); });
  if (tree.rootSessions.length !== origRootLen) changed = true;

  var folders = Object.values(tree.folders);
  for (var i = 0; i < folders.length; i++) {
    var origLen = folders[i].sessions.length;
    folders[i].sessions = folders[i].sessions.filter(function(id) { return valid.has(id); });
    if (folders[i].sessions.length !== origLen) changed = true;
  }

  if (changed) {
    _saveFolderTree(tree);
  }
}
