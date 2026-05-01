/* invoke-workforce.js — Invoke Workforce modal, selection state, message wrapping */

// ═══════════════════════════════════════════════════════════════════════
// STATE
// ═══════════════════════════════════════════════════════════════════════

/** @type {{ id:string, name:string, systemPrompt:string, path:string, source:string }|null} */
window._pendingInvoke = null;

let _invokeModalOpen = false;
let _localWfCache = null;   // { ts, skills, agents }
const _LOCAL_WF_TTL = 15000; // 15s cache
let _invokeRegistry = [];   // temp registry for modal items (avoids inline JSON in onclick)

// ═══════════════════════════════════════════════════════════════════════
// NAME PRETTIFICATION
// ═══════════════════════════════════════════════════════════════════════

function _prettifyName(stem) {
  if (!stem) return '';
  // Split camelCase
  let n = stem.replace(/([a-z])([A-Z])/g, '$1 $2');
  n = n.replace(/[-_]+/g, ' ');
  return n.replace(/\b\w/g, c => c.toUpperCase());
}

// ═══════════════════════════════════════════════════════════════════════
// SLASH COMMAND INTERCEPTION
// ═══════════════════════════════════════════════════════════════════════

function _interceptSlashCommand(text) {
  if (!text || text[0] !== '/') return false;
  const parts = text.split(/\s+/);
  const cmd = parts[0].toLowerCase();
  const arg = parts.slice(1).join(' ').trim();

  switch (cmd) {
    case '/invoke':
      if (!arg) { _openInvokeModal(); return true; }
      _invokeAssetById(arg);
      return true;
    case '/as':
      if (!arg) { showToast('Usage: /as <asset-id>'); return true; }
      _invokeAssetById(arg);
      return true;
    case '/team':
    case '/departments':
      _openInvokeModal();
      return true;
    default:
      return false;
  }
}

function _invokeAssetById(assetId) {
  if (typeof FOLDER_SUPERSET !== 'object' || !FOLDER_SUPERSET) { showToast('No departments loaded'); return; }
  const def = FOLDER_SUPERSET[assetId];
  if (!def || !def.skill || !def.skill.systemPrompt) {
    showToast('Asset not found: ' + assetId);
    return;
  }
  _selectInvoke({
    id: assetId,
    name: def.skill.label || def.name || assetId,
    systemPrompt: def.skill.systemPrompt,
    path: '',
    source: 'department',
  });
}

// ═══════════════════════════════════════════════════════════════════════
// PER-SESSION MODEL OVERRIDE
// A lightweight per-session model+thinking override stored in memory.
// Only applies to the *next* session start (cleared after use).
// Separate from the system-level defaultModel / defaultThinking that are
// persisted in localStorage.
// ═══════════════════════════════════════════════════════════════════════

/** Current per-session model override — null means "use system default". */
// Persist overrides in localStorage so they survive page refresh
window._sessionModelOverride = localStorage.getItem('_sessionModelOverride') || null;
window._sessionThinkingOverride = localStorage.getItem('_sessionThinkingOverride') || null;

/**
 * Return the effective model for the next session:
 * per-session override if set, otherwise the system default.
 */
function _effectiveModel() {
  return window._sessionModelOverride || (typeof defaultModel !== 'undefined' ? defaultModel : 'claude-opus-4-7');
}

/**
 * Return the effective thinking level for the next session:
 * per-session override if set, otherwise the system default.
 */
function _effectiveThinking() {
  return window._sessionThinkingOverride !== null ? window._sessionThinkingOverride
    : (typeof defaultThinking !== 'undefined' ? defaultThinking : '');
}

/**
 * Clear per-session overrides.  Called by _newSessionSubmit after the
 * session is dispatched so the next session reverts to system defaults.
 */
function _clearSessionModelOverride() {
  window._sessionModelOverride = null;
  window._sessionThinkingOverride = null;
  localStorage.removeItem('_sessionModelOverride');
  localStorage.removeItem('_sessionThinkingOverride');
}

/**
 * Build the model badge button shown next to /invoke in the input bar.
 * Displays the effective model label; clicking opens the per-session
 * model + thinking selector popup.
 *
 * @param {boolean} [isNewSession=false] — true for brand-new sessions
 *   (button is clickable).  For already-running sessions the button is
 *   informational only (shows the session's current model).
 * @param {string}  [sessionModel=''] — model of the current live session,
 *   used in the running/idle bars to show what model is active.
 */
function _buildSessionModelBtn(isNewSession, sessionModel) {
  const isOverridden = window._sessionModelOverride !== null;
  // Always show override if one is set (applies to next session started), else show session/default
  const label = isOverridden
    ? _modelLabel(window._sessionModelOverride)
    : _modelLabel(sessionModel || (typeof defaultModel !== 'undefined' ? defaultModel : ''));

  if (isNewSession) {
    return '<button class="session-model-btn' + (isOverridden ? ' session-model-overridden' : '') + '" ' +
      'id="session-model-btn" onclick="_openSessionModelSelector()" ' +
      'title="' + (isOverridden ? 'Model overridden — click to change' : 'Click to choose model for this session') + '">' +
      label + '</button>';
  } else {
    return '<button class="session-model-badge' + (isOverridden ? ' session-model-overridden' : '') + '" ' +
      'onclick="_openSessionModelSelector()" ' +
      'title="' + (isOverridden ? 'Model overridden — click to change' : 'Click to change model') + '">' +
      label + '</button>';
  }
}

/**
 * Open a per-session model + thinking level selector popup.
 * Uses the existing pm-overlay.  Selecting a model/thinking level sets
 * the per-session override WITHOUT affecting the system-level defaults
 * stored in localStorage.
 */
async function _openSessionModelSelector() {
  const overlay = document.getElementById('pm-overlay');
  if (!overlay) return;

  overlay.innerHTML = '<div class="pm-card pm-enter" style="width:400px;">' +
    '<h2 class="pm-title">Session Model</h2>' +
    '<div class="pm-body"><p>Choose model and thinking level for <strong>this session</strong>. System default is unchanged.</p></div>' +
    '<div style="display:flex;flex-direction:column;gap:8px;margin-bottom:16px;" id="sm-model-list">' +
    '<span class="spinner"></span></div>' +
    '<div id="sm-thinking-section" style="display:none;margin-bottom:16px;">' +
    '<div style="font-size:11px;font-weight:600;color:var(--text-faint);text-transform:uppercase;letter-spacing:.06em;margin-bottom:8px;">Thinking Level</div>' +
    '<div style="display:flex;flex-direction:column;gap:6px;" id="sm-thinking-list"></div>' +
    '</div>' +
    '<div class="pm-actions">' +
    '<button class="pm-btn pm-btn-secondary" onclick="_clearSessionModelOverrideAndClose()">Reset to Default</button>' +
    '<button class="pm-btn pm-btn-primary" id="sm-apply-btn" disabled onclick="_applySessionModelOverride()">Apply</button>' +
    '</div></div>';
  overlay.classList.add('show');
  requestAnimationFrame(() => { const c = overlay.querySelector('.pm-card'); if (c) c.classList.remove('pm-enter'); });
  overlay.onclick = e => { if (e.target === overlay) _closePm(); };

  // Fetch models
  let models;
  try {
    const resp = await fetch('/api/models');
    models = await resp.json();
  } catch (e) {
    models = [
      {id: 'claude-opus-4-7',  name: 'Opus 4.7',   desc: '1M context, deepest reasoning'},
      {id: 'claude-opus-4-6',  name: 'Opus 4.6',   desc: 'Deep reasoning, 200K context'},
      {id: 'claude-sonnet-4-6',name: 'Sonnet 4.6', desc: 'Fast, capable, balanced'},
      {id: 'claude-haiku-4-5', name: 'Haiku 4.5',  desc: 'Fastest, most cost-efficient'},
    ];
  }

  // Track pending selections
  let pendingModel = window._sessionModelOverride || _effectiveModel();
  let pendingThinking = window._sessionThinkingOverride !== null
    ? window._sessionThinkingOverride
    : (typeof defaultThinking !== 'undefined' ? defaultThinking : '');

  function _renderModels() {
    const list = document.getElementById('sm-model-list');
    if (!list) return;
    let html = '';
    for (const m of models) {
      const active = m.id === pendingModel;
      html += '<div class="add-mode-card' + (active ? ' active' : '') + '" data-model="' + (m.id || '') + '" ' +
        'onclick="_smSelectModel(this)">' +
        '<div class="add-mode-info">' +
        '<div class="add-mode-title">' + (m.name || m.id) + '</div>' +
        (m.desc ? '<div class="add-mode-desc">' + m.desc + '</div>' : '') +
        '</div></div>';
    }
    list.innerHTML = html;
  }

  function _renderThinking() {
    const section = document.getElementById('sm-thinking-section');
    const list = document.getElementById('sm-thinking-list');
    if (!section || !list) return;
    section.style.display = '';
    const levels = [
      {key: '', label: 'Default', desc: 'Model default'},
      {key: 'none', label: 'None', desc: 'No extended thinking'},
      {key: 'low', label: 'Low', desc: 'Brief reasoning'},
      {key: 'medium', label: 'Medium', desc: 'Moderate reasoning'},
      {key: 'high', label: 'High', desc: 'Deep reasoning'},
    ];
    let html = '';
    for (const l of levels) {
      const active = l.key === pendingThinking;
      html += '<div class="add-mode-card' + (active ? ' active' : '') + '" style="padding:8px 12px;" data-level="' + l.key + '" ' +
        'onclick="_smSelectThinking(this)">' +
        '<div class="add-mode-info">' +
        '<div class="add-mode-title" style="font-size:12px;">' + l.label + '</div>' +
        '<div class="add-mode-desc" style="font-size:11px;">' + l.desc + '</div>' +
        '</div></div>';
    }
    list.innerHTML = html;
  }

  _renderModels();
  _renderThinking();

  // Enable apply only when a selection differs from current state
  function _refreshApply() {
    const btn = document.getElementById('sm-apply-btn');
    if (btn) btn.disabled = false; // always allow apply after any interaction
  }

  // Expose helpers to inline onclick handlers
  window._smSelectModel = function(card) {
    document.querySelectorAll('#sm-model-list .add-mode-card').forEach(c => c.classList.remove('active'));
    card.classList.add('active');
    pendingModel = card.dataset.model;
    _refreshApply();
  };
  window._smSelectThinking = function(card) {
    document.querySelectorAll('#sm-thinking-list .add-mode-card').forEach(c => c.classList.remove('active'));
    card.classList.add('active');
    pendingThinking = card.dataset.level;
    _refreshApply();
  };
  window._applySessionModelOverride = function() {
    window._sessionModelOverride = pendingModel;
    window._sessionThinkingOverride = pendingThinking;
    if (pendingModel) localStorage.setItem('_sessionModelOverride', pendingModel);
    else localStorage.removeItem('_sessionModelOverride');
    if (pendingThinking) localStorage.setItem('_sessionThinkingOverride', pendingThinking);
    else localStorage.removeItem('_sessionThinkingOverride');
    _closePm();
    // Refresh any visible session-model-btn to show updated label
    _refreshSessionModelBtn();
    const label = _modelLabel(pendingModel);
    const thinking = pendingThinking
      ? ' + ' + pendingThinking.charAt(0).toUpperCase() + pendingThinking.slice(1) + ' thinking'
      : '';
    if (typeof showToast === 'function') showToast('Session: ' + label + thinking);
  };
  window._clearSessionModelOverrideAndClose = function() {
    window._sessionModelOverride = null;
    window._sessionThinkingOverride = null;
    localStorage.removeItem('_sessionModelOverride');
    localStorage.removeItem('_sessionThinkingOverride');
    _closePm();
    _refreshSessionModelBtn();
    if (typeof showToast === 'function') showToast('Session model reset to system default');
  };
}

/**
 * Refresh the session-model-btn in the current input bar after
 * an override is applied or cleared, without re-rendering the full bar.
 */
function _refreshSessionModelBtn() {
  const label = _modelLabel(_effectiveModel());
  const isOverridden = window._sessionModelOverride !== null;

  // New-session button (has id)
  const btn = document.getElementById('session-model-btn');
  if (btn) {
    btn.textContent = label;
    btn.classList.toggle('session-model-overridden', isOverridden);
    btn.title = isOverridden
      ? 'Model overridden — click to change'
      : 'Click to choose model for this session';
  }

  // Running-session badge (no id, use class)
  const badge = document.querySelector('.session-model-badge');
  if (badge) {
    badge.textContent = label;
    badge.title = isOverridden
      ? 'Next session: ' + label + ' — click to change'
      : 'Click to set model for next session';
  }
}

// ═══════════════════════════════════════════════════════════════════════
// INVOKE BUTTON BUILDER (for the input bar)
// ═══════════════════════════════════════════════════════════════════════

function _buildInvokeBtn() {
  return '<button class="invoke-btn" id="invoke-btn" onclick="_openInvokeModal()" title="Invoke Workforce">' +
    '<svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="url(#invoke-grad)" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round">' +
    '<defs><linearGradient id="invoke-grad" x1="0%" y1="0%" x2="100%" y2="100%"><stop offset="0%" stop-color="#a855f7"/><stop offset="100%" stop-color="#3b82f6"/></linearGradient></defs>' +
    '<polygon points="13 2 3 14 12 14 11 22 21 10 12 10 13 2"/>' +
    '</svg>' +
    '<span class="invoke-btn-label">/invoke</span>' +
    '</button>';
}

/**
 * Wrap invoke button + model badge + context circle in a left-pinned group.
 *
 * @param {string}  ctxHtml       - Context bar HTML (from _buildCtxBarCompact).
 * @param {boolean} [isNewSession=false] - true only for brand-new sessions
 *   where the model button is interactive.
 * @param {string}  [sessionModel=''] - model of the current live session
 *   (used in idle/waiting/working bars for display purposes).
 */
function _buildBarLeftGroup(ctxHtml, isNewSession, sessionModel) {
  return '<div class="bar-left-group">' +
    (typeof _buildInvokeBtn === 'function' ? _buildInvokeBtn() : '') +
    _buildSessionModelBtn(isNewSession || false, sessionModel || '') +
    (ctxHtml || '') +
    '</div>';
}

// ═══════════════════════════════════════════════════════════════════════
// FETCH LOCAL SKILLS / AGENTS
// ═══════════════════════════════════════════════════════════════════════

async function _fetchLocalWorkforce() {
  if (_localWfCache && (Date.now() - _localWfCache.ts < _LOCAL_WF_TTL)) {
    return { skills: _localWfCache.skills, agents: _localWfCache.agents };
  }
  try {
    const resp = await fetch('/api/invoke/discover?depth=2');
    const data = await resp.json();
    if (data.ok) {
      _localWfCache = {
        ts: Date.now(),
        skills: data.local_skills || [],
        agents: data.local_agents || [],
      };
      return { skills: _localWfCache.skills, agents: _localWfCache.agents };
    }
  } catch (e) {
    console.warn('Failed to fetch local workforce:', e);
  }
  return { skills: [], agents: [] };
}

// ═══════════════════════════════════════════════════════════════════════
// INVOKE MODAL
// ═══════════════════════════════════════════════════════════════════════

async function _openInvokeModal() {
  if (_invokeModalOpen) { _closeInvokeModal(); return; }

  // Create overlay
  const overlay = document.createElement('div');
  overlay.id = 'invoke-modal-overlay';
  overlay.className = 'invoke-overlay';
  overlay.onclick = (e) => { if (e.target === overlay) _closeInvokeModal(); };

  // Modal card
  const modal = document.createElement('div');
  modal.className = 'invoke-modal';
  modal.innerHTML = '<div class="invoke-modal-loading"><span class="spinner"></span> Loading workforce\u2026</div>';
  overlay.appendChild(modal);
  document.body.appendChild(overlay);
  _invokeModalOpen = true;

  requestAnimationFrame(() => overlay.classList.add('show'));

  // Fetch data
  const local = await _fetchLocalWorkforce();
  const departments = _getDeptTree();

  // Build modal content
  let h = '';
  // Header
  h += '<div class="invoke-modal-header">';
  h += '<h2 class="invoke-modal-title">Invoke Workforce</h2>';
  h += '<button class="invoke-modal-close" onclick="_closeInvokeModal()">&times;</button>';
  h += '</div>';

  // Search
  h += '<input type="text" class="invoke-search" id="invoke-search" placeholder="Search skills, agents, departments\u2026" oninput="_filterInvokeModal(this.value)">';

  // Reset registry — stores item data by index to avoid inline JSON in onclick
  _invokeRegistry = [];

  // Sections container
  h += '<div class="invoke-sections">';

  // --- Local Skills ---
  h += '<div class="invoke-section" data-section="skills">';
  h += '<div class="invoke-section-label">Local Skills</div>';
  h += '<div class="invoke-section-scroll" id="invoke-skills">';
  if (local.skills.length) {
    for (const sk of local.skills) {
      const name = sk.name || _prettifyName(sk.id);
      const idx = _invokeRegistry.length;
      _invokeRegistry.push({id:sk.id, name:name, systemPrompt:sk.systemPrompt||'', path:sk.path||'', source:'local_skill'});
      h += '<div class="invoke-item" data-search="' + escHtml(name.toLowerCase()) + '" onclick="_selectInvokeByIdx(' + idx + ')">';
      h += '<div class="invoke-item-icon"><svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><path d="M14.7 6.3a1 1 0 0 0 0 1.4l1.6 1.6a1 1 0 0 0 1.4 0l3.77-3.77a6 6 0 0 1-7.94 7.94l-6.91 6.91a2.12 2.12 0 0 1-3-3l6.91-6.91a6 6 0 0 1 7.94-7.94l-3.76 3.76z"/></svg></div>';
      h += '<div class="invoke-item-info"><div class="invoke-item-name">' + escHtml(name) + '</div>';
      if (sk.relativePath) h += '<div class="invoke-item-path">' + escHtml(sk.relativePath) + '</div>';
      h += '</div></div>';
    }
  } else {
    h += '<div class="invoke-empty">No local skills found. Add <code>.md</code> files to your project\'s <code>skills/</code> folder.</div>';
  }
  h += '</div></div>';

  // --- Local Agents ---
  h += '<div class="invoke-section" data-section="agents">';
  h += '<div class="invoke-section-label">Local Agents</div>';
  h += '<div class="invoke-section-scroll" id="invoke-agents">';
  if (local.agents.length) {
    for (const ag of local.agents) {
      const name = ag.name || _prettifyName(ag.id);
      const idx = _invokeRegistry.length;
      _invokeRegistry.push({id:ag.id, name:name, systemPrompt:ag.systemPrompt||'', path:ag.path||'', source:'local_agent'});
      h += '<div class="invoke-item" data-search="' + escHtml(name.toLowerCase()) + '" onclick="_selectInvokeByIdx(' + idx + ')">';
      h += '<div class="invoke-item-icon"><svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><rect x="3" y="11" width="18" height="10" rx="2"/><circle cx="12" cy="5" r="2"/><line x1="12" y1="7" x2="12" y2="11"/><circle cx="8" cy="16" r="1" fill="currentColor"/><circle cx="16" cy="16" r="1" fill="currentColor"/></svg></div>';
      h += '<div class="invoke-item-info"><div class="invoke-item-name">' + escHtml(name) + '</div>';
      if (ag.relativePath) h += '<div class="invoke-item-path">' + escHtml(ag.relativePath) + '</div>';
      h += '</div></div>';
    }
  } else {
    h += '<div class="invoke-empty">No local agents found. Add <code>.md</code> files to your project\'s <code>agents/</code> folder.</div>';
  }
  h += '</div></div>';

  // --- Departments ---
  h += '<div class="invoke-section" data-section="departments">';
  h += '<div class="invoke-section-label">Departments</div>';
  h += '<div class="invoke-section-scroll" id="invoke-departments">';
  if (departments.length) {
    for (const dept of departments) {
      h += _buildInvokeDeptNode(dept, 0);
    }
  } else {
    h += '<div class="invoke-empty">No departments configured. Visit the Workforce view to set them up.</div>';
  }
  h += '</div></div>';

  h += '</div>'; // .invoke-sections

  // Footer
  h += '<div class="invoke-modal-footer">';
  h += '<button class="invoke-manage-btn" onclick="_goToWorkforce()">Manage Workforce</button>';
  h += '</div>';

  modal.innerHTML = h;

  // Focus search
  const search = document.getElementById('invoke-search');
  if (search) search.focus();

  // Escape key
  modal._escHandler = (e) => { if (e.key === 'Escape') _closeInvokeModal(); };
  document.addEventListener('keydown', modal._escHandler);
}

function _closeInvokeModal() {
  const overlay = document.getElementById('invoke-modal-overlay');
  if (overlay) {
    if (overlay.querySelector('.invoke-modal')._escHandler) {
      document.removeEventListener('keydown', overlay.querySelector('.invoke-modal')._escHandler);
    }
    overlay.classList.remove('show');
    setTimeout(() => overlay.remove(), 200);
  }
  _invokeModalOpen = false;
}

function _filterInvokeModal(query) {
  const q = query.toLowerCase();
  const items = document.querySelectorAll('#invoke-modal-overlay .invoke-item, #invoke-modal-overlay .invoke-dept-header');
  for (const item of items) {
    const s = item.dataset.search || '';
    item.style.display = (!q || s.includes(q)) ? '' : 'none';
  }
  // Show/hide sections based on whether they have visible items
  const sections = document.querySelectorAll('#invoke-modal-overlay .invoke-section');
  for (const sec of sections) {
    const visibleItems = sec.querySelectorAll('.invoke-item:not([style*="display: none"]), .invoke-dept-header:not([style*="display: none"])');
    const emptyMsg = sec.querySelector('.invoke-empty');
    // Don't hide sections, just let items filter
  }
}

// ═══════════════════════════════════════════════════════════════════════
// DEPARTMENT TREE HELPERS
// ═══════════════════════════════════════════════════════════════════════

function _getDeptTree() {
  if (typeof FOLDER_SUPERSET !== 'object' || !FOLDER_SUPERSET) return [];
  const tree = (typeof getFolderTree === 'function') ? getFolderTree() : null;
  if (!tree || !tree.rootChildren || !tree.rootChildren.length) return [];
  return tree.rootChildren.map(rc => {
    const fid = typeof rc === 'string' ? rc : rc.id;
    return _buildDeptData(tree, fid);
  }).filter(Boolean);
}

function _buildDeptData(tree, fid) {
  const folder = tree.folders[fid];
  if (!folder) return null;
  const def = FOLDER_SUPERSET[fid];
  const label = def ? (def.skill ? def.skill.label : def.name) : fid;
  const children = (folder.children || []).map(ck => {
    const cid = typeof ck === 'string' ? ck : ck.id;
    return _buildDeptData(tree, cid);
  }).filter(Boolean);
  return {
    id: fid,
    name: label,
    systemPrompt: def && def.skill ? def.skill.systemPrompt : '',
    children: children,
    isLeaf: children.length === 0,
  };
}

function _buildInvokeDeptNode(node, depth) {
  if (!node) return '';
  const indent = depth * 16;
  const searchName = (node.name || '').toLowerCase();
  let h = '';

  if (node.children && node.children.length) {
    // Department with children — collapsible
    const toggleId = 'invoke-dept-' + node.id;
    h += '<div class="invoke-dept-header" data-search="' + escHtml(searchName) + '" style="padding-left:' + indent + 'px;" onclick="_toggleInvokeDept(\'' + toggleId + '\', this)">';
    h += '<svg class="invoke-dept-chevron" width="10" height="10" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round"><polyline points="9 18 15 12 9 6"/></svg>';
    h += '<span>' + escHtml(node.name) + '</span>';
    if (node.systemPrompt) {
      const idx = _invokeRegistry.length;
      _invokeRegistry.push({id:node.id, name:node.name, systemPrompt:node.systemPrompt, path:'', source:'department'});
      h += '<button class="invoke-dept-use" onclick="event.stopPropagation();_selectInvokeByIdx(' + idx + ')" title="Invoke this department">Use</button>';
    }
    h += '</div>';
    h += '<div class="invoke-dept-children collapsed" id="' + toggleId + '">';
    for (const child of node.children) {
      h += _buildInvokeDeptNode(child, depth + 1);
    }
    h += '</div>';
  } else {
    // Leaf — clickable
    const idx = _invokeRegistry.length;
    _invokeRegistry.push({id:node.id, name:node.name, systemPrompt:node.systemPrompt||'', path:'', source:'department'});
    h += '<div class="invoke-item" data-search="' + escHtml(searchName) + '" style="padding-left:' + indent + 'px;" onclick="_selectInvokeByIdx(' + idx + ')">';
    h += '<div class="invoke-item-icon"><svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><circle cx="12" cy="7" r="4"/><path d="M5.8 21a7 7 0 0 1 12.4 0"/></svg></div>';
    h += '<div class="invoke-item-info"><div class="invoke-item-name">' + escHtml(node.name) + '</div></div>';
    h += '</div>';
  }
  return h;
}

function _toggleInvokeDept(id, header) {
  const el = document.getElementById(id);
  if (!el) return;
  const collapsed = el.classList.toggle('collapsed');
  const chev = header.querySelector('.invoke-dept-chevron');
  if (chev) chev.style.transform = collapsed ? '' : 'rotate(90deg)';
}

// ═══════════════════════════════════════════════════════════════════════
// SELECTION + VISUAL STATE
// ═══════════════════════════════════════════════════════════════════════

function _selectInvokeByIdx(idx) {
  const item = _invokeRegistry[idx];
  if (!item) return;
  _closeInvokeModal();
  _selectInvoke(item);
}

function _selectInvokeFromModal(item) {
  _closeInvokeModal();
  _selectInvoke(item);
}

function _selectInvoke(item) {
  window._pendingInvoke = item;
  _applyInvokeVisual();
  // Focus the textarea and trigger input event so voice.js updateIcon() shows the send button
  const ta = document.getElementById('live-input-ta') || document.getElementById('live-queue-ta');
  if (ta) {
    ta.focus();
    ta.dispatchEvent(new Event('input', {bubbles: true}));
  }
}

function _applyInvokeVisual() {
  const invoke = window._pendingInvoke;
  if (!invoke) { _removeInvokeVisual(); return; }

  // Find the textarea wrapper area
  const ta = document.getElementById('live-input-ta') || document.getElementById('live-queue-ta');
  if (!ta) return;

  // Add gradient class to textarea
  ta.classList.add('invoke-active');

  // Add floating label if not present
  let label = document.getElementById('invoke-float-label');
  if (!label) {
    label = document.createElement('div');
    label.id = 'invoke-float-label';
    label.className = 'invoke-float-label';
    ta.parentElement.insertBefore(label, ta);
  }
  label.innerHTML =
    '<span class="invoke-float-name">Invoke ' + escHtml(invoke.name) + '</span>' +
    '<button class="invoke-float-cancel" onclick="_cancelInvoke()" title="Cancel invoke">&times;</button>';
}

function _removeInvokeVisual() {
  const ta = document.getElementById('live-input-ta') || document.getElementById('live-queue-ta');
  if (ta) ta.classList.remove('invoke-active');
  const label = document.getElementById('invoke-float-label');
  if (label) label.remove();
}

function _cancelInvoke() {
  window._pendingInvoke = null;
  _removeInvokeVisual();
  // Trigger input event so voice.js updateIcon() re-evaluates send button visibility
  const ta = document.getElementById('live-input-ta') || document.getElementById('live-queue-ta');
  if (ta) ta.dispatchEvent(new Event('input', {bubbles: true}));
}

// ═══════════════════════════════════════════════════════════════════════
// MESSAGE WRAPPING — wrap invoke content into [[invoke]]...[[/invoke]]
// ═══════════════════════════════════════════════════════════════════════

/**
 * If a pending invoke is set, wrap the user's text with the invoke block.
 * Returns the final text to send. Clears _pendingInvoke.
 */
function _wrapInvokeMessage(userText) {
  const invoke = window._pendingInvoke;
  if (!invoke) return userText;

  window._pendingInvoke = null;
  _removeInvokeVisual();

  const block = '[[invoke::' + invoke.name + '::path=' + (invoke.path || '') + ']]\n' +
    invoke.systemPrompt + '\n[[/invoke]]';

  return userText ? block + '\n\n' + userText : block;
}

/**
 * Build a system prompt notice for the invoked skill.
 * Returns a short string to prepend to the session system prompt.
 */
function _buildInvokeNotice() {
  const invoke = window._pendingInvoke;
  if (!invoke) return '';
  let notice = '\n\nThe user has invoked a workforce skill: "' + invoke.name + '"';
  if (invoke.path) notice += ' (from ' + invoke.path + ')';
  notice += '. The skill instructions are included in the user\'s message wrapped in [[invoke]]...[[/invoke]] tags. Follow those instructions for this request.';
  return notice;
}

// ═══════════════════════════════════════════════════════════════════════
// PILL RENDERING — detect [[invoke]] blocks in messages and render pills
// ═══════════════════════════════════════════════════════════════════════

const _INVOKE_RE = /\[\[invoke::(.+?)::path=([^\]]*)\]\][\s\S]*?\[\[\/invoke\]\]/g;

function _renderInvokePills(text) {
  // Returns { html: string, remainder: string }
  // html = pill HTML for the invoke block
  // remainder = user text after the invoke block
  const match = _INVOKE_RE.exec(text);
  _INVOKE_RE.lastIndex = 0; // reset regex state
  if (!match) return null;

  const name = match[1];
  const before = text.slice(0, match.index).trim();
  const after = text.slice(match.index + match[0].length).trim();

  return {
    pillHtml: '<span class="invoke-pill">' + escHtml(name) + '</span>',
    remainder: (before ? before + '\n' : '') + after,
  };
}

// ═══════════════════════════════════════════════════════════════════════
// NAVIGATE TO WORKFORCE
// ═══════════════════════════════════════════════════════════════════════

function _goToWorkforce() {
  _closeInvokeModal();
  if (typeof setViewMode === 'function') {
    setViewMode('workplace');
  }
}
