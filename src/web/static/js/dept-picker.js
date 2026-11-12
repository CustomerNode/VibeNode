/* dept-picker.js — Department picker flyout + slash command invocation for sessions */

// ═══════════════════════════════════════════════════════════════════════
// SLASH COMMAND INTERCEPTION
// ═══════════════════════════════════════════════════════════════════════

/**
 * Intercept slash commands in the live input.
 * Returns true if the command was handled (caller should not send to session).
 */
function _interceptSlashCommand(text) {
  if (!text || text[0] !== '/') return false;
  const parts = text.split(/\s+/);
  const cmd = parts[0].toLowerCase();
  const arg = parts.slice(1).join(' ').trim();

  switch (cmd) {
    case '/invoke':
    case '/as':
      if (!arg) { showToast('Usage: /invoke <asset-id>'); return true; }
      _invokeAssetById(arg);
      return true;

    case '/team':
    case '/departments':
      _openDeptPickerFlyout();
      return true;

    default:
      return false;
  }
}

// ═══════════════════════════════════════════════════════════════════════
// INVOKE ASSET BY ID
// ═══════════════════════════════════════════════════════════════════════

function _invokeAssetById(assetId) {
  if (!liveSessionId) { showToast('No active session'); return; }
  if (typeof FOLDER_SUPERSET !== 'object' || !FOLDER_SUPERSET) { showToast('No departments loaded'); return; }

  const def = FOLDER_SUPERSET[assetId];
  if (!def || !def.skill || !def.skill.systemPrompt) {
    showToast('Asset not found: ' + assetId);
    return;
  }

  const prompt = '[Invoking department asset: ' + (def.skill.label || assetId) + ']\n\n' + def.skill.systemPrompt;
  socket.emit('send_message', { session_id: liveSessionId, message: prompt });
  showToast('Invoked: ' + (def.skill.label || assetId));

  // Track recently used
  _trackRecentAsset(assetId);
}

let _recentAssets = JSON.parse(localStorage.getItem('recentDeptAssets') || '[]');

function _trackRecentAsset(id) {
  _recentAssets = _recentAssets.filter(x => x !== id);
  _recentAssets.unshift(id);
  if (_recentAssets.length > 8) _recentAssets = _recentAssets.slice(0, 8);
  localStorage.setItem('recentDeptAssets', JSON.stringify(_recentAssets));
}

// ═══════════════════════════════════════════════════════════════════════
// DEPARTMENT PICKER FLYOUT (in-session)
// ═══════════════════════════════════════════════════════════════════════

let _deptPickerOpen = false;

function _openDeptPickerFlyout() {
  if (_deptPickerOpen) { _closeDeptPickerFlyout(); return; }

  const tree = (typeof getFolderTree === 'function') ? getFolderTree() : null;
  if (!tree || !tree.rootChildren || !tree.rootChildren.length) {
    showToast('No departments installed. Go to Workforce > Configure to add some.');
    return;
  }

  // Build flyout
  const flyout = document.createElement('div');
  flyout.id = 'dept-picker-flyout';
  flyout.className = 'dept-picker-flyout';

  let h = '';
  h += '<div class="dept-picker-header">';
  h += '<input type="text" class="dept-picker-search" id="dept-picker-search" placeholder="Search departments…" oninput="_filterDeptPicker(this.value)">';
  h += '<button class="dept-picker-close" onclick="_closeDeptPickerFlyout()">&times;</button>';
  h += '</div>';

  // Recent assets
  if (_recentAssets.length && typeof FOLDER_SUPERSET === 'object' && FOLDER_SUPERSET) {
    h += '<div class="dept-picker-section-label">Recent</div>';
    h += '<div class="dept-picker-items" id="dept-picker-recent">';
    for (const rid of _recentAssets) {
      const rdef = FOLDER_SUPERSET[rid];
      if (!rdef || !rdef.skill) continue;
      h += '<div class="dept-picker-item" data-id="' + rid + '" onclick="_deptPickerSelect(\'' + rid + '\')">';
      h += '<span class="dept-picker-item-name">' + (rdef.skill.label || rdef.name || rid) + '</span>';
      h += '</div>';
    }
    h += '</div>';
  }

  // Department tree
  h += '<div class="dept-picker-section-label">Departments</div>';
  h += '<div class="dept-picker-items" id="dept-picker-tree">';
  for (const rc of tree.rootChildren) {
    const fid = typeof rc === 'string' ? rc : rc.id;
    h += _buildDeptPickerNode(tree, fid, 0);
  }
  h += '</div>';

  flyout.innerHTML = h;

  // Position relative to the input bar
  const bar = document.getElementById('live-input-bar');
  if (bar) {
    bar.parentElement.style.position = 'relative';
    bar.parentElement.appendChild(flyout);
  } else {
    document.body.appendChild(flyout);
  }

  _deptPickerOpen = true;
  requestAnimationFrame(() => {
    flyout.classList.add('open');
    const search = document.getElementById('dept-picker-search');
    if (search) search.focus();
  });

  // Close on click outside
  setTimeout(() => {
    document.addEventListener('click', _deptPickerOutsideClick);
  }, 100);
}

function _closeDeptPickerFlyout() {
  const flyout = document.getElementById('dept-picker-flyout');
  if (flyout) {
    flyout.classList.remove('open');
    setTimeout(() => flyout.remove(), 200);
  }
  _deptPickerOpen = false;
  document.removeEventListener('click', _deptPickerOutsideClick);
}

function _deptPickerOutsideClick(e) {
  const flyout = document.getElementById('dept-picker-flyout');
  const btn = document.getElementById('dept-picker-btn');
  if (flyout && !flyout.contains(e.target) && (!btn || !btn.contains(e.target))) {
    _closeDeptPickerFlyout();
  }
}

function _deptPickerSelect(assetId) {
  _closeDeptPickerFlyout();
  _invokeAssetById(assetId);
}

function _buildDeptPickerNode(tree, fid, depth) {
  const folder = tree.folders[fid];
  if (!folder) return '';
  const def = (typeof FOLDER_SUPERSET !== 'undefined' && FOLDER_SUPERSET[fid]) ? FOLDER_SUPERSET[fid] : null;
  const label = def ? (def.skill ? def.skill.label : def.name) : fid;
  const children = folder.children || [];
  const hasChildren = children.length > 0;
  const indent = depth * 16;

  let h = '';
  if (hasChildren) {
    // Department header
    h += '<div class="dept-picker-dept" style="padding-left:' + indent + 'px;" data-search="' + label.toLowerCase() + '">';
    h += '<svg width="10" height="10" viewBox="0 0 24 24" fill="none" stroke="var(--text-faint)" stroke-width="2" stroke-linecap="round"><path d="M22 19a2 2 0 0 1-2 2H4a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h5l2 3h9a2 2 0 0 1 2 2z"/></svg>';
    h += '<span>' + label + '</span>';
    h += '</div>';
    for (const ck of children) {
      const cid = typeof ck === 'string' ? ck : ck.id;
      h += _buildDeptPickerNode(tree, cid, depth + 1);
    }
  } else {
    // Leaf asset — clickable
    h += '<div class="dept-picker-item" style="padding-left:' + indent + 'px;" data-id="' + fid + '" data-search="' + label.toLowerCase() + '" onclick="_deptPickerSelect(\'' + fid + '\')">';
    h += '<span class="dept-picker-item-name">' + label + '</span>';
    h += '</div>';
  }
  return h;
}

function _filterDeptPicker(query) {
  const q = query.toLowerCase();
  const treeEl = document.getElementById('dept-picker-tree');
  const recentEl = document.getElementById('dept-picker-recent');
  if (!treeEl) return;

  const items = treeEl.querySelectorAll('[data-search]');
  for (const item of items) {
    const match = !q || item.dataset.search.includes(q);
    item.style.display = match ? '' : 'none';
  }
  if (recentEl) {
    recentEl.style.display = q ? 'none' : '';
  }
}

// ═══════════════════════════════════════════════════════════════════════
// PICKER BUTTON (added to live panel toolbar)
// ═══════════════════════════════════════════════════════════════════════

function _buildDeptPickerBtn() {
  return '<button class="live-send-btn dept-picker-btn" id="dept-picker-btn" onclick="_openDeptPickerFlyout()" title="Browse departments">' +
    '<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round">' +
    '<path d="M17 21v-2a4 4 0 0 0-4-4H5a4 4 0 0 0-4 4v2"/>' +
    '<circle cx="9" cy="7" r="4"/>' +
    '<path d="M23 21v-2a4 4 0 0 0-3-3.87"/>' +
    '<path d="M16 3.13a4 4 0 0 1 0 7.75"/>' +
    '</svg></button>';
}
