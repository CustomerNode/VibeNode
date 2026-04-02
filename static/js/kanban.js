/**
 * kanban.js — Workflow board view module.
 *
 * Implements the Kanban board as specified in the plan (Section 11).
 * Structure mirrors the plan's module structure exactly:
 *   State → Core Functions → CRUD → Drag & Drop →
 *   Session Spawner → Sorting & Filtering → Column Config →
 *   WebSocket Handlers → Browser History
 */

// ═══════════════════════════════════════════════════════════════
// STATE
// ═══════════════════════════════════════════════════════════════

let kanbanColumns = [];
let kanbanTasks = [];                               // flat array, with parent_id refs
let kanbanExpandedTasks = new Set(
  JSON.parse(localStorage.getItem('kanbanExpanded') || '[]')
);
let kanbanDragState = null;                          // {taskId, sourceCol, sourceIdx}
let kanbanActiveTagFilter = (() => {
  const stored = sessionStorage.getItem('kanbanTagFilter');
  return stored ? JSON.parse(stored) : [];
})();
let kanbanDetailTaskId = null;
let kanbanAllTags = [];
let kanbanQuillInstance = null;
let kanbanFocusedCard = -1;

// Status colors read from CSS variables for theme support
let _statusColorCache = null;
function _readStatusColors() {
  const s = getComputedStyle(document.documentElement);
  return {
    not_started: s.getPropertyValue('--status-not-started').trim() || '#8b949e',
    working:     s.getPropertyValue('--status-working').trim() || '#58a6ff',
    validating:  s.getPropertyValue('--status-validating').trim() || '#d29922',
    remediating: s.getPropertyValue('--status-remediating').trim() || '#f85149',
    complete:    s.getPropertyValue('--status-complete').trim() || '#3fb950',
  };
}
const KANBAN_STATUS_COLORS = new Proxy({}, {
  get(_, prop) {
    if (!_statusColorCache) _statusColorCache = _readStatusColors();
    return _statusColorCache[prop];
  }
});
// Invalidate cache on theme change
new MutationObserver(() => { _statusColorCache = null; })
  .observe(document.documentElement, { attributes: true, attributeFilter: ['data-theme'] });

const KANBAN_STATUS_LABELS = {
  not_started: 'Not Started',
  working:     'Working',
  validating:  'Validating',
  remediating: 'Remediating',
  complete:    'Complete',
};

/**
 * _resolveSessionName(id) — Get display name for a session ID.
 * Uses allSessions (the same source as list/grid/workforce views)
 * so naming is consistent across the entire app.
 */
function _resolveSessionId(id) {
  // Resolve through remap aliases — the SDK may have assigned a new ID
  if (window._idRemaps && window._idRemaps[id]) return window._idRemaps[id];
  return id;
}

function _resolveSessionName(id) {
  id = _resolveSessionId(id);
  if (typeof allSessions !== 'undefined') {
    const s = allSessions.find(x => x.id === id);
    if (s) return s.display_title || id;
  }
  // UUID not in allSessions — orphaned link (probably from a session ID remap).
  // Show a truncated ID rather than a full UUID wall of text.
  if (id && id.length > 16) return id.slice(0, 8) + '\u2026';
  return id;
}

// ── SVG Icons (replacing emoji entities for theme consistency) ──
const KI = {
  plan:      '<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><path d="M12 2L2 7l10 5 10-5-10-5z"/><path d="M2 17l10 5 10-5"/><path d="M2 12l10 5 10-5"/></svg>',
  chart:     '<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><line x1="18" y1="20" x2="18" y2="10"/><line x1="12" y1="20" x2="12" y2="4"/><line x1="6" y1="20" x2="6" y2="14"/></svg>',
  gear:      '<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><circle cx="12" cy="12" r="3"/><path d="M12.22 2h-.44a2 2 0 0 0-2 2v.18a2 2 0 0 1-1 1.73l-.43.25a2 2 0 0 1-2 0l-.15-.08a2 2 0 0 0-2.73.73l-.22.38a2 2 0 0 0 .73 2.73l.15.1a2 2 0 0 1 1 1.72v.51a2 2 0 0 1-1 1.74l-.15.09a2 2 0 0 0-.73 2.73l.22.38a2 2 0 0 0 2.73.73l.15-.08a2 2 0 0 1 2 0l.43.25a2 2 0 0 1 1 1.73V20a2 2 0 0 0 2 2h.44a2 2 0 0 0 2-2v-.18a2 2 0 0 1 1-1.73l.43-.25a2 2 0 0 1 2 0l.15.08a2 2 0 0 0 2.73-.73l.22-.39a2 2 0 0 0-.73-2.73l-.15-.08a2 2 0 0 1-1-1.74v-.5a2 2 0 0 1 1-1.74l.15-.09a2 2 0 0 0 .73-2.73l-.22-.38a2 2 0 0 0-2.73-.73l-.15.08a2 2 0 0 1-2 0l-.43-.25a2 2 0 0 1-1-1.73V4a2 2 0 0 0-2-2z"/></svg>',
  tag:       '<svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><path d="M20.59 13.41l-7.17 7.17a2 2 0 0 1-2.83 0L2 12V2h10l8.59 8.59a2 2 0 0 1 0 2.82z"/><line x1="7" y1="7" x2="7.01" y2="7"/></svg>',
  clipboard: '<svg width="36" height="36" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round"><path d="M16 4h2a2 2 0 0 1 2 2v14a2 2 0 0 1-2 2H6a2 2 0 0 1-2-2V6a2 2 0 0 1 2-2h2"/><rect x="8" y="2" width="8" height="4" rx="1"/></svg>',
  zap:       '<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><polygon points="13 2 3 14 12 14 11 22 21 10 12 10 13 2"/></svg>',
  chat:      '<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><path d="M21 15a2 2 0 0 1-2 2H7l-4 4V5a2 2 0 0 1 2-2h14a2 2 0 0 1 2 2z"/></svg>',
  link:      '<svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><path d="M10 13a5 5 0 0 0 7.54.54l3-3a5 5 0 0 0-7.07-7.07l-1.72 1.71"/><path d="M14 11a5 5 0 0 0-7.54-.54l-3 3a5 5 0 0 0 7.07 7.07l1.71-1.71"/></svg>',
  db:        '<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><ellipse cx="12" cy="5" rx="9" ry="3"/><path d="M21 12c0 1.66-4 3-9 3s-9-1.34-9-3"/><path d="M3 5v14c0 1.66 4 3 9 3s9-1.34 9-3V5"/></svg>',
  cloud:     '<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><path d="M18 10h-1.26A8 8 0 1 0 9 20h9a5 5 0 0 0 0-10z"/></svg>',
  trendUp:   '<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><polyline points="23 6 13.5 15.5 8.5 10.5 1 18"/><polyline points="17 6 23 6 23 12"/></svg>',
  clock:     '<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><circle cx="12" cy="12" r="10"/><polyline points="12 6 12 12 16 14"/></svg>',
  refresh:   '<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><polyline points="23 4 23 10 17 10"/><polyline points="1 20 1 14 7 14"/><path d="M3.51 9a9 9 0 0 1 14.85-3.36L23 10M1 14l4.64 4.36A9 9 0 0 0 20.49 15"/></svg>',
  user:      '<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><path d="M20 21v-2a4 4 0 0 0-4-4H8a4 4 0 0 0-4 4v2"/><circle cx="12" cy="7" r="4"/></svg>',
  alertTri:  '<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><path d="M10.29 3.86L1.82 18a2 2 0 0 0 1.71 3h16.94a2 2 0 0 0 1.71-3L13.71 3.86a2 2 0 0 0-3.42 0z"/><line x1="12" y1="9" x2="12" y2="13"/><line x1="12" y1="17" x2="12.01" y2="17"/></svg>',
  fileText:  '<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"/><polyline points="14 2 14 8 20 8"/><line x1="16" y1="13" x2="8" y2="13"/><line x1="16" y1="17" x2="8" y2="17"/></svg>',
  calendar:  '<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><rect x="3" y="4" width="18" height="18" rx="2"/><line x1="16" y1="2" x2="16" y2="6"/><line x1="8" y1="2" x2="8" y2="6"/><line x1="3" y1="10" x2="21" y2="10"/></svg>',
  checkCirc: '<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><path d="M22 11.08V12a10 10 0 1 1-5.93-9.14"/><polyline points="22 4 12 14.01 9 11.01"/></svg>',
  search:    '<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><circle cx="11" cy="11" r="8"/><line x1="21" y1="21" x2="16.65" y2="16.65"/></svg>',
  pin:       '<svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><path d="M21 10c0 7-9 13-9 13s-9-6-9-13a9 9 0 0 1 18 0z"/><circle cx="12" cy="10" r="3"/></svg>',
  chevronR:  '<svg width="10" height="10" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round"><polyline points="9 18 15 12 9 6"/></svg>',
  chevronL:  '<svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><polyline points="15 18 9 12 15 6"/></svg>',
  chevronRsm:'<svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><polyline points="9 18 15 12 9 6"/></svg>',
  arrowR:    '<svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><line x1="5" y1="12" x2="19" y2="12"/><polyline points="12 5 19 12 12 19"/></svg>',
  menu:      '<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><line x1="3" y1="6" x2="21" y2="6"/><line x1="3" y1="12" x2="21" y2="12"/><line x1="3" y1="18" x2="21" y2="18"/></svg>',
  dots:      '<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><circle cx="12" cy="5" r="1" fill="currentColor"/><circle cx="12" cy="12" r="1" fill="currentColor"/><circle cx="12" cy="19" r="1" fill="currentColor"/></svg>',
  drag:      '<svg width="12" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><circle cx="9" cy="5" r="1.5" fill="currentColor"/><circle cx="15" cy="5" r="1.5" fill="currentColor"/><circle cx="9" cy="12" r="1.5" fill="currentColor"/><circle cx="15" cy="12" r="1.5" fill="currentColor"/><circle cx="9" cy="19" r="1.5" fill="currentColor"/><circle cx="15" cy="19" r="1.5" fill="currentColor"/></svg>',
  check:     '<svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="3" stroke-linecap="round"><polyline points="20 6 9 17 4 12"/></svg>',
  x:         '<svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><line x1="18" y1="6" x2="6" y2="18"/><line x1="6" y1="6" x2="18" y2="18"/></svg>',
  pencil:    '<svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><path d="M11 4H4a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h14a2 2 0 0 0 2-2v-7"/><path d="M18.5 2.5a2.121 2.121 0 0 1 3 3L12 15l-4 1 1-4 9.5-9.5z"/></svg>',
  sortUD:    '<svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><path d="M7 3v18M7 3l-4 4M7 3l4 4M17 21V3M17 21l-4-4M17 21l4-4"/></svg>',
  square:    '<svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><rect x="3" y="3" width="18" height="18" rx="2"/></svg>',
  bullet:    '<svg width="8" height="8" viewBox="0 0 24 24"><circle cx="12" cy="12" r="6" fill="currentColor"/></svg>',
  play:      '<svg width="10" height="10" viewBox="0 0 24 24" fill="currentColor"><polygon points="6 3 20 12 6 21 6 3"/></svg>',
  plus:      '<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><line x1="12" y1="5" x2="12" y2="19"/><line x1="5" y1="12" x2="19" y2="12"/></svg>',
};


// ═══════════════════════════════════════════════════════════════
// CUSTOM CONFIRM MODAL (replaces browser confirm())
// ═══════════════════════════════════════════════════════════════

function _kanbanConfirm(title, message, onConfirm, opts) {
  const o = opts || {};
  const btnLabel = o.btnLabel || 'Remove';
  const btnStyle = o.btnStyle || 'background:var(--danger);border-color:var(--danger);';
  const overlay = document.getElementById('pm-overlay');
  if (!overlay) { if (onConfirm) onConfirm(); return; }
  overlay.innerHTML = `<div class="pm-card pm-enter" style="max-width:380px;">
    <h2 class="pm-title">${escHtml(title)}</h2>
    <div class="pm-body"><p style="color:var(--text-muted);font-size:13px;">${escHtml(message)}</p></div>
    <div class="pm-actions">
      <button class="pm-btn pm-btn-secondary" onclick="_closePm()">Cancel</button>
      <button class="pm-btn pm-btn-primary" id="kanban-confirm-btn" style="${btnStyle}">${escHtml(btnLabel)}</button>
    </div>
  </div>`;
  overlay.classList.add('show');
  requestAnimationFrame(() => overlay.querySelector('.pm-card')?.classList.remove('pm-enter'));
  overlay.onclick = (e) => { if (e.target === overlay) _closePm(); };
  document.getElementById('kanban-confirm-btn').onclick = () => { _closePm(); if (onConfirm) onConfirm(); };
}

// ═══════════════════════════════════════════════════════════════
// VERIFICATION URL RESOLVER (plan Section 16)
// ═══════════════════════════════════════════════════════════════

function resolveVerificationUrl(url) {
  if (!url) return null;
  if (url.startsWith('http://') || url.startsWith('https://')) return url;
  const base = 'http://localhost:5050';
  return base + (url.startsWith('/') ? url : '/' + url);
}


// ═══════════════════════════════════════════════════════════════
// CORE FUNCTIONS
// ═══════════════════════════════════════════════════════════════

/**
 * loadKanbanBoard() — GET /api/kanban/board, render.
 * Called by setViewMode('kanban') in workforce.js.
 *
 * Debounced: rapid-fire calls (e.g. action handler + WebSocket event both
 * firing within 200ms) are collapsed into a single fetch+render.
 */
var _kanbanDebounceTimer = null;
var _kanbanHasLoaded = false;
var _kanbanFetching = false;       // true while a fetch is in-flight
var _kanbanLastRender = 0;
var _KANBAN_COOLDOWN = 800;

/**
 * resetKanbanState() — Nuclear cleanup of ALL kanban state.
 * Called by setViewMode when leaving kanban. Ensures nothing leaks.
 */
function resetKanbanState() {
  // Drill-down
  kanbanDetailTaskId = null;
  kanbanQuillInstance = null;
  // Board data
  kanbanColumns = [];
  kanbanTasks = [];
  kanbanAllTags = [];
  kanbanDragState = null;
  kanbanFocusedCard = -1;
  // Fetch state
  if (_kanbanDebounceTimer) clearTimeout(_kanbanDebounceTimer);
  _kanbanDebounceTimer = null;
  _kanbanHasLoaded = false;
  _kanbanFetching = false;
  _kanbanLastRender = 0;
  // Retry state
  if (renderTaskDetail._retries) renderTaskDetail._retries = 0;
  // Session spawner state
  window._kanbanPendingTaskLink = null;
  window._kanbanSessionTaskId = null;
  window._kanbanDetailTaskTitle = null;
  // Drag state
  _subtaskDragId = null;
  _subtaskDragEl = null;
  // DOM
  const board = document.getElementById('kanban-board');
  if (board) board.innerHTML = '';
}

function _kanbanSkeleton() {
  let html = '<div class="kanban-columns-wrapper">';
  for (let c = 0; c < 5; c++) {
    html += '<div class="kanban-column kanban-skel-col"><div class="kanban-column-header"><div class="skel-shimmer skel-col-title"></div><div class="skel-shimmer skel-col-count"></div></div><div class="kanban-column-body">';
    const n = 2 + Math.floor(Math.random() * 3);
    for (let i = 0; i < n; i++) {
      html += '<div class="kanban-card kanban-skel-card"><div class="skel-shimmer skel-card-title" style="width:' + (50 + Math.random() * 40) + '%;"></div><div class="skel-shimmer skel-card-meta" style="width:' + (30 + Math.random() * 30) + '%;"></div></div>';
    }
    html += '</div></div>';
  }
  html += '</div>';
  return html;
}

function _taskDetailSkeleton() {
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
          <div class="skel-shimmer" style="width:100%;height:72px;border-radius:8px;margin-bottom:16px;"></div>
          <div style="display:flex;gap:6px;">
            <div class="skel-shimmer" style="width:48px;height:20px;border-radius:10px;"></div>
            <div class="skel-shimmer" style="width:56px;height:20px;border-radius:10px;"></div>
            <div class="skel-shimmer" style="width:40px;height:20px;border-radius:10px;"></div>
          </div>
        </div>
        <div class="kanban-drill-right">
          <div class="skel-shimmer" style="width:120px;height:12px;border-radius:3px;margin-bottom:14px;"></div>
          <div style="border:1px solid var(--border);border-radius:10px;padding:6px 8px;">
            <div class="skel-shimmer" style="width:100%;height:38px;border-radius:8px;margin-bottom:4px;"></div>
            <div class="skel-shimmer" style="width:100%;height:38px;border-radius:8px;margin-bottom:4px;"></div>
            <div class="skel-shimmer" style="width:100%;height:38px;border-radius:8px;"></div>
          </div>
        </div>
      </div>
    </div>`;
}

function initKanban(force) {
  // NEVER blow away a live session
  if (liveSessionId && window._kanbanSessionTaskId) return;
  // Don't refresh the board while the planner slideout is open and visible
  // — it causes "failed to render" when the board DOM gets replaced mid-plan
  const _pp = document.getElementById('kanban-planner-panel');
  if (_pp && _pp.classList.contains('open') && !_pp.classList.contains('minimized') && !force) return;
  // If we just rendered recently and this isn't forced, skip (WS echo suppression)
  if (!force && _kanbanHasLoaded && Date.now() - _kanbanLastRender < _KANBAN_COOLDOWN) return;
  // If a fetch is already in-flight, don't queue another
  if (_kanbanFetching) return;
  if (_kanbanDebounceTimer) clearTimeout(_kanbanDebounceTimer);
  _kanbanDebounceTimer = setTimeout(_initKanbanImpl, _kanbanHasLoaded ? 100 : 0);
}

async function _initKanbanImpl() {
  _kanbanDebounceTimer = null;
  const board = document.getElementById('kanban-board');
  if (!board) return;
  _kanbanFetching = true;

  // Only show loading skeleton on first load — refreshes keep the current board visible
  if (!_kanbanHasLoaded) {
    board.innerHTML = _kanbanSkeleton();
  }

  try {
    const params = new URLSearchParams();
    if (kanbanActiveTagFilter.length > 0) {
      params.set('tags', kanbanActiveTagFilter.join(','));
    }
    const url = '/api/kanban/board' + (params.toString() ? '?' + params.toString() : '');
    const res = await fetch(url);
    if (!res.ok) throw new Error('Failed to load board');
    const data = await res.json();

    kanbanColumns = data.columns || [];
    kanbanTasks = data.tasks || [];

    // Tags are included in the board response — no separate fetch needed
    if (data.tags) kanbanAllTags = data.tags;

    renderKanbanBoard(data);
    _kanbanHasLoaded = true;
    _kanbanFetching = false;
    _kanbanLastRender = Date.now();

    // Set initial history state so browser back works from drill-down
    // BUT don't overwrite a task hash on refresh
    const _curHash = window.location.hash || '';
    if (!_curHash.startsWith('#kanban/task/')) {
      if (!history.state || history.state.view !== 'kanban') {
        history.replaceState({ view: 'kanban', taskId: null }, '', '#kanban');
      }
    }

    // Restore minimized/open planner if persisted from previous page load
    if (typeof _restorePlannerOnLoad === 'function') _restorePlannerOnLoad();

    // Render sidebar controls
    renderKanbanSidebar();

    // Attach keyboard shortcuts
    attachKanbanShortcuts();

    // Restore hash state
    restoreFromHash();
  } catch (e) {
    _kanbanFetching = false;
    console.error('[Kanban] Load failed:', e);
    board.innerHTML =
      '<div class="kanban-empty-state">' +
      '<div style="font-size:15px;font-weight:500;margin-bottom:6px;">Failed to load board</div>' +
      '<div style="font-size:12px;color:var(--text-faint);margin-bottom:14px;">' + escHtml(e.message) + '</div>' +
      '<button class="kanban-create-first-btn" onclick="initKanban(true)">Retry</button>' +
      '</div>';
    // Auto-retry after 5s
    setTimeout(() => { if (!_kanbanHasLoaded) initKanban(true); }, 5000);
  }
}

/**
 * renderKanbanBoard(data) — Build columns + cards DOM.
 * Plan Section 2: horizontally scrollable board of columns.
 */
function renderKanbanBoard(data) {
  const board = document.getElementById('kanban-board');
  if (!board) return;

  const columns = data.columns || [];
  const tasks = data.tasks || [];

  // Empty state (plan lines 3341-3347)
  const allTasks = Array.isArray(tasks) ? tasks : Object.values(tasks).flat();
  if (!columns.length || allTasks.length === 0) {
    board.innerHTML =
      '<div class="kanban-empty-state">' +
      '<div style="margin-bottom:12px;color:var(--text-muted);">' + KI.clipboard + '</div>' +
      '<div style="font-size:16px;font-weight:500;color:var(--text);margin-bottom:6px;">Welcome to your Kanban board</div>' +
      '<div style="font-size:13px;color:var(--text-muted);margin-bottom:16px;">This project doesn\'t have any tasks yet.</div>' +
      '<button class="kanban-create-first-btn" onclick="createTask(\'not_started\')">+ Create your first task</button>' +
      '</div>';
    return;
  }

  // Columns wrapper (horizontally scrollable) — controls are in the sidebar
  let colsHtml = '<div class="kanban-columns-wrapper">';
  for (const col of columns) {
    const colTasks = tasks.filter(t => t.status === col.status_key && !t.parent_id);
    _sortColumnTasks(colTasks, col);
    colsHtml += renderKanbanColumn(col, colTasks);
  }
  colsHtml += '</div>';

  board.innerHTML = colsHtml;
}

const _DEFAULT_COL_SORT = { complete: 'last_updated', validating: 'last_updated', remediating: 'last_updated' };

function _sortColumnTasks(tasks, col) {
  let mode = col.sort_mode || 'manual';
  if (mode === 'manual' && _DEFAULT_COL_SORT[col.status_key]) mode = _DEFAULT_COL_SORT[col.status_key];
  if (mode === 'manual') return;
  const dir = col.sort_direction === 'asc' ? 1 : -1;
  tasks.sort((a, b) => {
    let va, vb;
    if (mode === 'last_updated' || mode === 'date_entered') {
      va = a.updated_at || a.created_at || '';
      vb = b.updated_at || b.created_at || '';
    } else if (mode === 'date_created') {
      va = a.created_at || '';
      vb = b.created_at || '';
    } else if (mode === 'alphabetical') {
      va = (a.title || '').toLowerCase();
      vb = (b.title || '').toLowerCase();
    } else {
      return 0;
    }
    if (va < vb) return -1 * dir;
    if (va > vb) return 1 * dir;
    return 0;
  });
}

/**
 * renderKanbanColumn(col, tasks) — Single column with its cards.
 */
function renderKanbanColumn(col, tasks) {
  const color = KANBAN_STATUS_COLORS[col.status_key] || col.color || 'var(--text-muted)';
  const count = tasks.length;
  const isDragReorder = col.sort_mode === 'manual';

  let html = `<div class="kanban-column" data-status="${escHtml(col.status_key)}"
       ondragover="onKanbanDragOver(event, this)"
       ondragleave="onKanbanDragLeave(event, this)"
       ondrop="onKanbanDrop(event, '${escHtml(col.status_key)}')">
    <div class="kanban-column-header">
      <div class="kanban-column-color-bar" style="background:${escHtml(color)};"></div>
      <span class="kanban-column-name">${escHtml(col.name)}</span>
      <span class="kanban-column-count">${count}</span>
      <button class="kanban-col-gear-btn" onclick="event.stopPropagation();renderSortSelector('${escHtml(col.status_key)}', event)" title="Sort settings">
        <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><circle cx="12" cy="12" r="3"/><path d="M12.22 2h-.44a2 2 0 0 0-2 2v.18a2 2 0 0 1-1 1.73l-.43.25a2 2 0 0 1-2 0l-.15-.08a2 2 0 0 0-2.73.73l-.22.38a2 2 0 0 0 .73 2.73l.15.1a2 2 0 0 1 1 1.72v.51a2 2 0 0 1-1 1.74l-.15.09a2 2 0 0 0-.73 2.73l.22.38a2 2 0 0 0 2.73.73l.15-.08a2 2 0 0 1 2 0l.43.25a2 2 0 0 1 1 1.73V20a2 2 0 0 0 2 2h.44a2 2 0 0 0 2-2v-.18a2 2 0 0 1 1-1.73l.43-.25a2 2 0 0 1 2 0l.15.08a2 2 0 0 0 2.73-.73l.22-.39a2 2 0 0 0-.73-2.73l-.15-.08a2 2 0 0 1-1-1.74v-.5a2 2 0 0 1 1-1.74l.15-.09a2 2 0 0 0 .73-2.73l-.22-.38a2 2 0 0 0-2.73-.73l-.15.08a2 2 0 0 1-2 0l-.43-.25a2 2 0 0 1-1-1.73V4a2 2 0 0 0-2-2z"/></svg>
      </button>
    </div>
    <div class="kanban-column-body" data-status="${escHtml(col.status_key)}">`;

  // Render task cards
  if (tasks.length === 0) {
    // Empty columns are just empty — no placeholder needed
  }
  for (const task of tasks) {
    html += renderTaskCard(task, 0);
  }

  html += '</div></div>';
  return html;
}

/**
 * renderTaskCard(task, depth) — Card HTML with subtask/session info.
 * Plan Section 2 - Task Card Anatomy:
 *   Title (inline editable), Description (collapsed), Subtask progress,
 *   Session count, Expand arrow, Drag handle, Context menu (⋮),
 *   Smart timestamp, Verification URL link, Tag pills, Owner badge.
 */
function renderTaskCard(task, depth) {
  const color = KANBAN_STATUS_COLORS[task.status] || 'var(--text-muted)';
  const label = KANBAN_STATUS_LABELS[task.status] || task.status;
  const time = _shortDate(task.updated_at || task.created_at);
  const verUrl = resolveVerificationUrl(task.verification_url);

  // Subtask text — plan mockup: 3 formats, text only (no progress bar on board cards)
  const childCount = task.children_count || 0;
  const childDone = task.children_complete || 0;
  let subtaskHtml = '';
  if (childCount > 0) {
    let subtaskText;
    if (childDone === 0) {
      subtaskText = childCount + ' subtask' + (childCount !== 1 ? 's' : '');
    } else if (childDone >= childCount) {
      subtaskText = 'All subtasks complete';
    } else {
      subtaskText = childDone + '/' + childCount + ' subtasks done';
    }
    subtaskHtml = `<div class="kanban-card-subtask-text">${subtaskText}</div>`;
  }

  // Session badge — plan mockup: colored pill with tinted background
  const sessCount = task.session_count || 0;
  const activeSess = task.active_sessions || 0;
  let sessBadgeStyle, sessBadgeText;
  if (activeSess > 0) {
    sessBadgeStyle = 'background:var(--status-complete-dim);color:var(--status-complete);';
    sessBadgeText = KI.bullet + ' ' + activeSess + ' active';
  } else {
    sessBadgeStyle = 'background:var(--status-working-dim);color:var(--status-working);';
    sessBadgeText = sessCount + ' session' + (sessCount !== 1 ? 's' : '');
  }
  const sessBadgeHtml = `<div class="kanban-card-session-badge" style="${sessBadgeStyle}">${sessBadgeText}</div>`;

  // Expand arrow removed — drill-down handles subtask/session viewing

  // Verification URL — only shown in drill-down view, not on board cards
  const verIcon = '';

  // Context menu button (⋮)
  const contextBtn = `<button class="kanban-context-btn" onclick="event.stopPropagation();showCardContextMenu('${task.id}', event)" title="Actions">${KI.dots}</button>`;

  // Description (plan line 715: optional, collapsed by default, expandable)
  let descHtml = '';
  if (task.description) {
    const descText = task.description.replace(/<[^>]*>/g, '').trim();
    if (descText) {
      descHtml = `<div class="kanban-card-desc" onclick="event.stopPropagation();this.classList.toggle('expanded')">${escHtml(descText.length > 80 ? descText.slice(0, 78) + '\u2026' : descText)}</div>`;
    }
  }

  // Tag pills
  const tags = task.tags || [];
  let tagHtml = '';
  if (tags.length > 0) {
    tagHtml = '<div class="kanban-card-tags">';
    for (const tag of tags) {
      const tagColor = tagColorHash(tag);
      tagHtml += `<span class="kanban-tag-pill" style="background:${tagColor}22;color:${tagColor};border-color:${tagColor}44;" onclick="event.stopPropagation();applyTagFilter(['${escHtml(tag)}'])">${escHtml(tag)}</span>`;
    }
    tagHtml += '</div>';
  }

  // Owner badge
  const ownerHtml = task.owner
    ? `<span class="kanban-card-owner" title="${escHtml(task.owner)}">${escHtml(task.owner.charAt(0).toUpperCase())}</span>`
    : '';

  // Bottom row: tags + verURL on same row (plan card anatomy lines 714, 719)
  let bottomHtml = '';
  if (tagHtml || verIcon) {
    bottomHtml = `<div class="kanban-card-bottom">${tagHtml}${verIcon}</div>`;
  }

  const depthStyle = depth > 0 ? ` style="margin-left:${depth * 12}px;"` : '';

  const _isNew = window._kanbanHighlightIds && window._kanbanHighlightIds.has(task.id);
  return `<div class="kanban-card${_isNew ? ' kanban-task-highlight' : ''}" data-task-id="${task.id}" data-status="${task.status}"
               draggable="true"
               onclick="navigateToTask('${task.id}')"
               oncontextmenu="event.preventDefault();event.stopPropagation();showCardContextMenu('${task.id}', event)"
               ondragstart="onKanbanDragStart(event, '${task.id}', '${task.status}')"
               ondragend="onKanbanDragEnd(event)"
               ${depthStyle}>
    <div class="kanban-card-header">
      <span class="kanban-drag-handle" onclick="event.stopPropagation()">${KI.drag}</span>
      <div class="kanban-card-title-row">
        <span class="kanban-card-title">${escHtml(task.title)}</span>
        <span class="kanban-card-time" title="${escHtml(task.updated_at || task.created_at || '')}">${escHtml(time)}</span>
      </div>
      ${ownerHtml}
      ${contextBtn}
    </div>
    ${descHtml}
    ${(subtaskHtml || sessCount > 0) ? '<div style="display:flex;gap:6px;align-items:center;margin-top:6px;flex-wrap:wrap;">' + subtaskHtml + (sessCount > 0 ? sessBadgeHtml : '') + '</div>' : ''}
    ${bottomHtml}
  </div>`;
}

/**
 * renderSubtaskList(taskData) — Nested subtask rendering within expanded card.
 * Plan Section 2 - Expanded Card:
 *   Subtask list (mini-kanban cards), Session list with status,
 *   Session spawner button, Add subtask inline, Breadcrumb trail.
 */
function renderExpandedContent(taskData) {
  const children = taskData.children || [];
  const sessions = taskData.sessions || [];
  let html = '';

  // Subtask list — each as a mini-kanban card with status badge
  if (children.length > 0) {
    html += '<div class="kanban-subtask-section">';
    html += '<div class="kanban-expand-section-title">Subtasks</div>';
    for (const child of children) {
      const childColor = KANBAN_STATUS_COLORS[child.status] || 'var(--text-muted)';
      const childLabel = KANBAN_STATUS_LABELS[child.status] || child.status;
      const childProgress = (child.children_count > 0)
        ? `<span class="kanban-subtask-progress">${child.children_complete || 0}/${child.children_count}</span>`
        : '';
      const hasKids = (child.children_count || 0) > 0;
      html += `<div class="kanban-subtask-row" data-task-id="${child.id}"${hasKids ? ` onclick="event.stopPropagation();drillDown('${child.id}','${escHtml(child.title).replace(/'/g, "\\'")}')" style="cursor:pointer;"` : ''}>
        <span class="kanban-subtask-status-dot" style="background:${childColor};"></span>
        <span class="kanban-subtask-title">${escHtml(child.title)}</span>
        ${childProgress}
        <span class="kanban-subtask-badge" style="background:${childColor}20;color:${childColor};">${escHtml(childLabel)}</span>
      </div>`;
    }
    html += '</div>';
  }

  // Session list — with status (Working / Idle / Sleeping)
  // Resolve names from allSessions (same source as grid view) for consistency.
  if (sessions.length > 0) {
    html += '<div class="kanban-session-section">';
    html += '<div class="kanban-expand-section-title">Sessions</div>';
    for (const sess of sessions) {
      const sessId = typeof sess === 'string' ? sess : sess.session_id;
      const sessTitle = _resolveSessionName(sessId);
      const sessStatus = (typeof sess === 'object' && typeof sess.status === 'string' && sess.status) ? sess.status : 'sleeping';
      const dotColor = sessStatus === 'working' ? 'var(--accent)' : sessStatus === 'idle' ? 'var(--green)' : 'var(--text-faint)';
      html += `<div class="kanban-session-row" onclick="event.stopPropagation();selectSession('${escHtml(sessId)}');">
        <span class="kanban-session-dot" style="background:${dotColor};"></span>
        <span class="kanban-session-title">${escHtml(sessTitle)}</span>
        <span class="kanban-session-status">${escHtml(sessStatus)}</span>
      </div>`;
    }
    html += '</div>';
  }

  // Action buttons + inline add-subtask input (plan line 789: inline input)
  html += '<div class="kanban-expand-actions">';
  html += `<button class="kanban-expand-action-btn" onclick="event.stopPropagation();toggleExpandedAddSubtask('${taskData.id}')">+ Add Subtask</button>`;
  html += `<button class="kanban-expand-action-btn" onclick="event.stopPropagation();openSessionSpawner('${taskData.id}')">+ New Session</button>`;
  html += '</div>';
  html += `<div class="kanban-expand-add-subtask" id="kanban-expand-add-${taskData.id}" style="display:none;margin-top:6px;">`;
  html += `<div style="display:flex;gap:6px;align-items:center;">`;
  html += `<input type="text" class="kanban-drill-add-input" id="kanban-expand-input-${taskData.id}" placeholder="Subtask title\u2026" style="flex:1;" onkeydown="if(event.key==='Enter'){event.preventDefault();event.stopPropagation();submitExpandedSubtask('${taskData.id}');}">`;
  html += `<button class="kanban-drill-add-btn" onclick="event.stopPropagation();submitExpandedSubtask('${taskData.id}')">Add</button>`;
  html += '</div></div>';

  return html;
}


/**
 * toggleExpandedAddSubtask / submitExpandedSubtask — inline add subtask in expanded card.
 * Plan line 789: "inline input to quickly add a child task"
 */
function toggleExpandedAddSubtask(parentId) {
  const container = document.getElementById('kanban-expand-add-' + parentId);
  if (!container) return;
  const visible = container.style.display !== 'none';
  container.style.display = visible ? 'none' : 'block';
  if (!visible) {
    const input = document.getElementById('kanban-expand-input-' + parentId);
    if (input) setTimeout(() => input.focus(), 50);
  }
}

async function submitExpandedSubtask(parentId) {
  const input = document.getElementById('kanban-expand-input-' + parentId);
  if (!input) return;
  const title = input.value.trim();
  if (!title) return;
  try {
    const res = await fetch('/api/kanban/tasks', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ title, parent_id: parentId, status: 'not_started' }),
    });
    if (!res.ok) {
      const err = await res.json().catch(() => ({}));
      throw new Error(err.error || 'Create failed');
    }
    input.value = '';
    if (typeof showToast === 'function') showToast('Subtask created');
    initKanban(true);
  } catch (e) {
    if (typeof showToast === 'function') showToast('Failed: ' + e.message, true);
  }
}


// ═══════════════════════════════════════════════════════════════
// CRUD
// ═══════════════════════════════════════════════════════════════

/**
 * createTask(status, parentId) — POST /api/kanban/tasks
 */
function createTask(status, parentId) {
  const overlay = document.getElementById('pm-overlay');
  if (!overlay) return;

  overlay.innerHTML = `<div class="pm-card pm-enter" style="max-width:480px;">
    <h2 class="pm-title" style="display:flex;align-items:center;justify-content:space-between;">
      <span>Add to Board</span>
      <div class="kanban-create-position-row" style="margin:0;">
        <span style="font-size:11px;color:var(--text-dim);">Insert</span>
        <button class="kanban-create-pos-btn active" id="kb-pos-top" onclick="_setInsertPos('top')">Top</button>
        <button class="kanban-create-pos-btn" id="kb-pos-bottom" onclick="_setInsertPos('bottom')">Bottom</button>
      </div>
    </h2>
    <div class="pm-body" style="padding:0;">

      <div class="kanban-create-section">
        <div class="kanban-create-section-label">Quick add</div>
        <div class="kanban-create-quick-row">
          <input type="text" id="kanban-new-task-input" class="kanban-create-input" placeholder="Task title\u2026"
            onkeydown="if(event.key==='Enter'){event.preventDefault();_submitCreateTask('${status || ''}','${parentId || ''}');}">
          <button class="kanban-create-submit" onclick="_submitCreateTask('${status || ''}','${parentId || ''}')">
            <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><line x1="12" y1="5" x2="12" y2="19"/><line x1="5" y1="12" x2="19" y2="12"/></svg>
          </button>
        </div>
      </div>

      <div class="kanban-create-divider"><span>or</span></div>

      <div class="kanban-create-section">
        <div class="kanban-create-section-label">Plan with AI</div>
        ${_plannerStashed ? '<button class="kanban-create-ai-btn" onclick="_resumePlan()" style="margin-bottom:8px;">' + KI.play + ' Resume previous plan <span style="font-size:11px;color:var(--text-faint);margin-left:4px;">(' + _countTasks(_plannerStashed.tasks) + ' tasks)</span></button>' : ''}
        <div class="kanban-create-ai-desc">Describe a goal or feature and Claude will break it into tasks</div>
        <div class="kanban-create-ai-input-row">
          <textarea id="kanban-plan-input" class="kanban-create-textarea" rows="3" placeholder="e.g. Build user authentication with OAuth, session management, and password reset\u2026"
            onkeydown="if(_shouldSend(event)){event.preventDefault();_submitPlanWithAi();}"></textarea>
          <button class="live-send-btn" id="kanban-plan-voice-btn" style="align-self:flex-end;"></button>
        </div>
      </div>

    </div>
  </div>`;
  overlay.classList.add('show');
  requestAnimationFrame(() => {
    overlay.querySelector('.pm-card')?.classList.remove('pm-enter');
    document.getElementById('kanban-new-task-input')?.focus();
    // Wire up voice on the plan textarea
    if (typeof setupVoiceButton === 'function') {
      setupVoiceButton(
        document.getElementById('kanban-plan-input'),
        document.getElementById('kanban-plan-voice-btn'),
        () => _submitPlanWithAi()
      );
    }
  });
  overlay.onclick = (e) => { if (e.target === overlay) _closePm(); };
}

async function _submitPlanWithAi() {
  const ta = document.getElementById('kanban-plan-input');
  const text = ta ? ta.value.trim() : '';
  if (!text) { if (ta) ta.focus(); return; }
  _closePm();

  // Check if validation URL config has been set or dismissed
  try {
    const cfg = await fetch('/api/kanban/config').then(r => r.ok ? r.json() : {});
    if (!cfg.validation_url_enabled && !cfg.validation_url_dismissed && !cfg.validation_base_url) {
      // Wait for the previous modal close animation to finish
      await new Promise(r => setTimeout(r, 220));
      const result = await _showValidationUrlSetupModal();
      if (result === 'never') {
        await fetch('/api/kanban/config', { method: 'PUT', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ validation_url_dismissed: true }) });
      }
      // 'skipped' (click-away / Not Now) = ask again next time
      // 'never' = don't ask again
      // 'enabled' = saved URL, proceed
    }
  } catch (_) { /* config fetch failed, proceed anyway */ }

  _openPlannerSlideout(text);
}

function _showValidationUrlSetupModal() {
  return new Promise((resolve) => {
    const overlay = document.getElementById('pm-overlay');
    if (!overlay) { resolve('skipped'); return; }

    let _resolved = false;
    const _finish = (val) => { if (_resolved) return; _resolved = true; resolve(val); };

    // Ensure overlay is clean before building
    overlay.innerHTML = '';
    overlay.classList.remove('show');

    overlay.innerHTML = `<div class="pm-card pm-enter" style="max-width:480px;">
      <h2 class="pm-title">Validation URLs</h2>
      <div class="pm-body">
        <div style="font-size:13px;color:var(--text-secondary);margin-bottom:16px;">
          The AI planner can generate clickable validation URLs on each task so you can quickly verify features in your browser. Where does your development server run?
        </div>
        <input type="url" id="_val-setup-url" class="pm-input" placeholder="http://localhost:8000" style="width:100%;margin-bottom:8px;" onkeydown="if(event.key==='Enter'){event.preventDefault();document.getElementById('_val-setup-save')?.click();}">
        <div style="font-size:11px;color:var(--text-faint);">You can change this later in Workflow Settings → Validation.</div>
      </div>
      <div class="pm-actions" style="gap:8px;">
        <button class="pm-btn pm-btn-secondary" id="_val-setup-never" style="margin-right:auto;font-size:12px;opacity:0.7;">Don't ask again</button>
        <button class="pm-btn pm-btn-secondary" id="_val-setup-skip">Not now</button>
        <button class="pm-btn pm-btn-primary" id="_val-setup-save">Enable</button>
      </div>
    </div>`;
    overlay.classList.add('show');
    requestAnimationFrame(() => overlay.querySelector('.pm-card')?.classList.remove('pm-enter'));

    const _close = () => {
      const card = overlay.querySelector('.pm-card');
      if (card) card.classList.add('pm-exit');
      setTimeout(() => { overlay.classList.remove('show'); overlay.innerHTML = ''; }, 180);
    };

    // "Not now" = skip this time, ask again next time
    document.getElementById('_val-setup-skip').onclick = () => { _close(); _finish('skipped'); };

    // "Don't ask again" = persist dismissal
    document.getElementById('_val-setup-never').onclick = () => { _close(); _finish('never'); };

    // "Enable" = save URL
    document.getElementById('_val-setup-save').onclick = async () => {
      const url = document.getElementById('_val-setup-url')?.value?.trim() || '';
      if (!url) { document.getElementById('_val-setup-url')?.focus(); return; }
      try {
        await fetch('/api/kanban/config', { method: 'PUT', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ validation_url_enabled: true, validation_base_url: url }) });
      } catch (_) {}
      _close();
      if (typeof showToast === 'function') showToast('Validation URLs enabled');
      _finish('enabled');
    };

    // Click outside = same as "Not now" (will ask again)
    overlay.onclick = (e) => { if (e.target === overlay) { _close(); _finish('skipped'); } };

    // Focus input after animation
    setTimeout(() => document.getElementById('_val-setup-url')?.focus(), 250);
  });
}

let _plannerProposal = null;
let _plannerScopeParentId = null;  // non-null = scoped plan (drill-down subtree)

let _plannerSessionId = null;
let _plannerStashed = null;
let _plannerEntryListener = null;
let _plannerStateListener = null;
let _plannerAccumText = '';

const _PLANNER_SYSTEM = [
  'You are a task planning assistant for a Kanban board.',
  'You MUST respond with ONLY valid JSON. No explanation, no prose, no apologies, no refusals.',
  'Even for trivial, nonsensical, or test inputs, you MUST produce a valid task tree.',
  'NEVER refuse. NEVER explain. NEVER output anything except the JSON object.',
  'Format: {"tasks":[{"title":"...","description":"...","verification_url":null,"subtasks":[]}]}',
  'To edit existing tasks include "id": {"tasks":[{"id":"existing-id","title":"New Title"}]}. Tasks without "id" are new.',
  'Prefer breaking work into subtasks over writing long descriptions. Descriptions should be brief (1-2 sentences max). Use subtasks to express detail.',
  'Rules: concrete actionable tasks, 2-4 nesting levels.',
  'verification_url: absolute URL (http:// or https://) the developer can click to validate the task. Default is null unless a dev server base URL is provided in this prompt.',
].join(' ');
let _plannerTimeout = null;

// ── Build the slide-out panel shell ──
function _buildPlannerPanel() {
  const panel = document.createElement('div');
  panel.id = 'kanban-planner-panel';
  panel.className = 'kanban-planner-panel';
  panel.innerHTML = `
    <div class="kanban-planner-header" onclick="if(document.getElementById('kanban-planner-panel')?.classList.contains('minimized')){event.stopPropagation();_restorePlanner();}">
      <span class="kanban-planner-title">${KI.plan} Plan with AI</span>
      <div style="display:flex;gap:4px;align-items:center;">
        <button class="kanban-planner-close planner-minimize-btn" onclick="event.stopPropagation();_minimizePlanner()" title="Minimize" style="font-size:16px;">&#x2015;</button>
        <button class="kanban-planner-close" onclick="event.stopPropagation();_closePlannerSlideout()" title="Close">&times;</button>
      </div>
    </div>
    <div class="planner-body" id="planner-body">
      <div class="planner-status" id="planner-status">
        <div class="planner-spinner"></div><span>Breaking down your plan\u2026</span>
      </div>
    </div>
    <div class="planner-footer" id="planner-footer">
      <div class="planner-refine-row">
        <textarea id="planner-refine-input" class="kanban-create-textarea" rows="2" placeholder="Ask for changes\u2026"
          onkeydown="if(_shouldSend(event)){event.preventDefault();_refinePlan();}"></textarea>
        <button class="live-send-btn" id="planner-refine-voice" style="align-self:flex-end;"></button>
      </div>
    </div>`;
  return panel;
}

// ── Wire up voice on the refine input ──
function _wirePlannerVoice() {
  if (typeof setupVoiceButton === 'function') {
    setupVoiceButton(
      document.getElementById('planner-refine-input'),
      document.getElementById('planner-refine-voice'),
      () => _refinePlan()
    );
  }
}

// ── Attach persistent socket listeners for this planner session ──
let _plannerStartTime = 0;
let _plannerTimerInterval = null;

function _attachPlannerListeners() {
  _detachPlannerListeners();
  _plannerAccumText = '';
  _plannerStartTime = Date.now();

  // Live timer so user knows it's working
  _plannerTimerInterval = setInterval(() => {
    const el = document.getElementById('planner-timer');
    if (!el) return;
    const secs = Math.floor((Date.now() - _plannerStartTime) / 1000);
    el.textContent = secs + 's';
  }, 1000);

  _plannerEntryListener = (data) => {
    console.log('[planner] entry event:', data.session_id, 'expected:', _plannerSessionId, 'kind:', data.entry?.kind, 'text:', (data.entry?.text || '').slice(0, 80));
    if (data.session_id !== _plannerSessionId) return;
    if (!data.entry) return;
    const kind = data.entry.kind;
    const text = data.entry.text || '';

    // Show tool activity as steps in the progress UI
    if (kind === 'tool_use') {
      const desc = data.entry.desc || data.entry.name || 'Working\u2026';
      _addPlannerStep(desc);
      return;
    }
    if (kind === 'tool_result') return;

    if (!text || kind !== 'asst') return;
    _plannerAccumText += text;
    _updatePlannerProgress();

    // Try to detect complete JSON without waiting for idle
    _tryEarlyParse();
  };

  _plannerStateListener = (data) => {
    console.log('[planner] state event:', data.session_id, 'expected:', _plannerSessionId, 'state:', data.state);
    if (data.session_id !== _plannerSessionId) return;
    if (data.state === 'idle' || data.state === 'stopped') {
      _stopPlannerTimer();
      // Skip if we already have a valid proposal (daemon sends deferred
      // idle re-emit after 3s which would overwrite the result with empty text)
      if (_plannerProposal) return;
      _showPlanResult(_plannerAccumText);
      _plannerAccumText = '';
    }
  };

  socket.on('session_entry', _plannerEntryListener);
  socket.on('session_state', _plannerStateListener);

}

function _stopPlannerTimer() {
  if (_plannerTimerInterval) { clearInterval(_plannerTimerInterval); _plannerTimerInterval = null; }
}

let _plannerSteps = [];
function _addPlannerStep(desc) {
  _plannerSteps.push(desc);
  _updatePlannerProgress();
}
function _updatePlannerProgress() {
  const body = document.getElementById('planner-body');
  if (!body) return;

  // Try to count how many task titles we can see so far in the partial JSON
  const titleMatches = _plannerAccumText.match(/"title"\s*:\s*"[^"]+"/g);
  const taskCount = titleMatches ? titleMatches.length : 0;
  const secs = Math.floor((Date.now() - _plannerStartTime) / 1000);

  let statusHtml = '<div class="planner-status">';
  statusHtml += '<div class="planner-spinner"></div>';
  statusHtml += '<div class="planner-progress-info">';
  if (taskCount > 0) {
    statusHtml += '<span>Building task breakdown\u2026</span>';
    statusHtml += '<span class="planner-progress-count">' + taskCount + ' task' + (taskCount !== 1 ? 's' : '') + ' so far</span>';
  } else if (_plannerSteps.length > 0) {
    statusHtml += '<span>Exploring project\u2026</span>';
  } else {
    statusHtml += '<span>Building task breakdown\u2026</span>';
  }
  statusHtml += '<span class="planner-progress-timer" id="planner-timer">' + secs + 's</span>';
  statusHtml += '</div>';
  // Render tool step log
  if (_plannerSteps.length > 0) {
    statusHtml += '<div class="planner-steps">';
    const show = _plannerSteps.slice(-6);
    const offset = Math.max(0, _plannerSteps.length - 6);
    show.forEach((s, i) => {
      const isLatest = (offset + i) === _plannerSteps.length - 1;
      statusHtml += '<div class="planner-step' + (isLatest ? ' latest' : '') + '">' +
        '<span class="planner-step-dot">\u2022</span> ' + escHtml(s) + '</div>';
    });
    statusHtml += '</div>';
    statusHtml += '<div class="planner-minimize-hint">Tip: minimize to keep working \u2014 it\u2019ll notify you when complete</div>';
  }
  statusHtml += '</div>';

  body.innerHTML = statusHtml;
}

let _earlyParseAttempted = false;
function _tryEarlyParse() {
  if (_earlyParseAttempted) return;
  const text = _plannerAccumText;
  // Find first { then walk forward respecting strings so braces inside "..." are ignored
  const start = text.indexOf('{');
  if (start < 0) return;
  let depth = 0, end = -1, inStr = false, esc = false;
  for (let i = start; i < text.length; i++) {
    const c = text[i];
    if (esc) { esc = false; continue; }
    if (c === '\\' && inStr) { esc = true; continue; }
    if (c === '"') { inStr = !inStr; continue; }
    if (inStr) continue;
    if (c === '{') depth++;
    else if (c === '}') { depth--; if (depth === 0) { end = i; break; } }
  }
  if (end < 0) return; // JSON not complete yet
  const jsonStr = text.slice(start, end + 1);
  try {
    const parsed = JSON.parse(jsonStr);
    if (parsed.tasks && parsed.tasks.length > 0) {
      _earlyParseAttempted = true;
      _stopPlannerTimer();
      _showPlanResult(text);
    }
  } catch (_) { /* not valid yet, keep waiting */ }
}

function _detachPlannerListeners() {
  _stopPlannerTimer();
  if (_plannerEntryListener) { socket.off('session_entry', _plannerEntryListener); _plannerEntryListener = null; }
  if (_plannerStateListener) { socket.off('session_state', _plannerStateListener); _plannerStateListener = null; }
}

// ── Open slide-out and start a planning session ──
async function _openPlannerSlideout(prompt) {
  const old = document.getElementById('kanban-planner-panel');
  if (old) old.remove();
  _plannerProposal = null;

  const newId = crypto.randomUUID();
  _plannerSessionId = newId;

  // Don't add to allSessions — planner is a utility session, not user-visible
  if (typeof _hiddenSessionIds !== 'undefined') _hiddenSessionIds.add(newId);

  // Build and show panel
  const panel = _buildPlannerPanel();
  document.body.appendChild(panel);
  requestAnimationFrame(() => panel.classList.add('open'));
  setTimeout(_wirePlannerVoice, 350);

  _persistPlannerState('open');

  // Attach listeners BEFORE emitting
  _attachPlannerListeners();

  // Fetch validation config to enrich the system prompt
  let valUrlSnippet = '';
  try {
    const cfg = await fetch('/api/kanban/config').then(r => r.ok ? r.json() : {});
    if (cfg.validation_url_enabled && cfg.validation_base_url) {
      valUrlSnippet = ' VALIDATION URLS ENABLED. Dev server base URL: ' + cfg.validation_base_url + '. You MUST set verification_url on EVERY task and subtask by constructing absolute URLs from this base URL. Read the project code to find real route paths, page URLs, and API endpoints. Build verification_url as base URL + the route path (e.g. ' + cfg.validation_base_url + '/dashboard). Every task MUST have a verification_url unless it is purely non-visual work like refactoring or config with no observable endpoint. Try hard to find a relevant URL for each task. For tasks that CREATE new endpoints or pages that do not exist yet, you may still set verification_url to the planned URL — but add a note in the task description like "(new endpoint)" so the developer knows it will only work after implementation.';
    }
  } catch (_) {}

  // Start session via daemon
  _earlyParseAttempted = false;
  _plannerSteps = [];
  runningIds.add(newId);
  sessionKinds[newId] = 'working';
  // For scoped plans (subtree editing), add explicit instruction to NOT include the parent
  let sysPrompt = _PLANNER_SYSTEM + valUrlSnippet;
  if (_plannerScopeParentId) {
    sysPrompt += ' IMPORTANT: You are editing an EXISTING task and its subtree. Return EXACTLY ONE top-level task in your "tasks" array — this is the parent task being edited. Include its updated title, description, and subtasks. The parent will be updated in place and its old subtree will be replaced.';
  }
  socket.emit('start_session', {
    session_id: newId,
    prompt: prompt,
    cwd: typeof _currentProjectDir === 'function' ? _currentProjectDir() : '',
    system_prompt: sysPrompt,
    max_turns: 0,
    session_type: 'planner',
  });
}

// ── Show parsed plan result in the panel body ──
function _showPlanResult(rawText) {
  // Auto-expand if minimized — results are ready
  const panel = document.getElementById('kanban-planner-panel');
  if (panel && panel.classList.contains('minimized')) _restorePlanner();
  const body = document.getElementById('planner-body');
  if (!body) return;
  // If the plan was already accepted, don't overwrite the UI with stale results
  if (!_plannerSessionId && !rawText) return;
  console.log('[planner] rawText (' + rawText.length + '):', rawText.slice(0, 500));
  let parsed = null;
  // Try 1: ```json ... ``` or ``` ... ```
  const m1 = rawText.match(/```(?:json)?\s*([\s\S]*?)```/);
  if (m1) { try { parsed = JSON.parse(m1[1]); } catch (_) {} }
  // Try 2: whole text as JSON
  if (!parsed) { try { parsed = JSON.parse(rawText.trim()); } catch (_) {} }
  // Try 3: string-aware brace extraction (handles braces inside JSON strings)
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
  // Try 4: bare array — wrap in {tasks: [...]}
  if (!parsed) {
    const aStart = rawText.indexOf('[');
    if (aStart >= 0) {
      let depth = 0, end = -1, inStr = false, esc = false;
      for (let i = aStart; i < rawText.length; i++) {
        const c = rawText[i];
        if (esc) { esc = false; continue; }
        if (c === '\\' && inStr) { esc = true; continue; }
        if (c === '"') { inStr = !inStr; continue; }
        if (inStr) continue;
        if (c === '[') depth++;
        else if (c === ']') { depth--; if (depth === 0) { end = i; break; } }
      }
      if (end > aStart) {
        try {
          const arr = JSON.parse(rawText.slice(aStart, end + 1));
          if (Array.isArray(arr) && arr.length && arr[0].title) parsed = { tasks: arr };
        } catch (_) {}
      }
    }
  }

  if (parsed && parsed.tasks && parsed.tasks.length > 0) {
    _plannerProposal = parsed;
    // Persist so it survives page refresh
    try { localStorage.setItem('plannerStash', JSON.stringify(parsed)); } catch(_) {}
    const count = _countTasks(parsed.tasks);
    body.innerHTML =
      '<div class="planner-result">' +
        '<div class="planner-result-header">' + KI.check + ' <strong>' + count + ' tasks</strong> proposed</div>' +
        _renderPlanTree(parsed.tasks) +
        '<div class="planner-actions">' +
          '<button class="planner-accept-btn" id="planner-accept-btn" onclick="_acceptPlan()">Add ' + count + ' tasks to Board</button>' +
        '</div>' +
        '<div class="planner-hint">Want changes? Type below and send.</div>' +
      '</div>';
  } else {
    // Stash failed raw text for debugging
    console.error('[planner] PARSE FAILED. rawText:', rawText);
    try { localStorage.setItem('plannerDebugRaw', rawText); } catch(_) {}
    body.innerHTML =
      '<div class="planner-result">' +
        '<div class="planner-error">Couldn\'t parse a task structure. Try rephrasing below.</div>' +
        (rawText ? '<pre class="planner-stream" style="max-height:200px;overflow:auto;white-space:pre-wrap;font-size:11px;margin-top:8px;padding:8px;background:var(--bg-subtle);border-radius:6px;">' + escHtml(rawText.slice(0, 2000)) + '</pre>' : '') +
      '</div>';
  }
}

// ── Refine: send follow-up to the same session ──
function _refinePlan() {
  const ta = document.getElementById('planner-refine-input');
  const text = ta ? ta.value.trim() : '';
  if (!text) return;
  ta.value = '';

  // If no session exists yet (opened via scoped planner), start one now
  if (!_plannerSessionId) {
    _openPlannerSlideout(text);
    return;
  }

  // Reset accumulator, timer, and early parse flag
  _plannerAccumText = '';
  _earlyParseAttempted = false;
  _plannerStartTime = Date.now();
  _stopPlannerTimer();
  _plannerTimerInterval = setInterval(() => {
    const el = document.getElementById('planner-timer');
    if (!el) return;
    el.textContent = Math.floor((Date.now() - _plannerStartTime) / 1000) + 's';
  }, 1000);

  _updatePlannerProgress();

  // Send message to existing session
  socket.emit('send_message', { session_id: _plannerSessionId, text: text });
}

function _renderPlanTree(tasks, depth) {
  depth = depth || 0;
  let html = '<div class="planner-tree' + (depth === 0 ? ' planner-tree-root' : '') + '">';
  for (let i = 0; i < tasks.length; i++) {
    const t = tasks[i];
    const hasSubs = t.subtasks && t.subtasks.length > 0;
    const totalSubs = hasSubs ? _countTasks(t.subtasks) : 0;
    html += `<div class="planner-node${hasSubs && depth > 0 ? ' collapsed' : ''}" data-depth="${depth}">
      <div class="planner-node-row" onclick="${hasSubs ? 'this.parentElement.classList.toggle(\'collapsed\')' : ''}">
        ${hasSubs ? '<span class="planner-chevron">' + KI.chevronR + '</span>' : '<span class="planner-bullet">' + KI.bullet + '</span>'}
        <span class="planner-node-title">${escHtml(t.title)}</span>
        ${hasSubs ? '<span class="planner-sub-count">' + totalSubs + '</span>' : ''}
      </div>
      ${t.description ? '<div class="planner-node-desc">' + (typeof mdParse === 'function' ? mdParse(t.description) : escHtml(t.description)) + '</div>' : ''}
      ${t.verification_url ? '<div class="planner-node-ver">' + KI.link + ' <a href="' + escHtml(t.verification_url) + '" target="_blank" rel="noopener" onclick="event.stopPropagation()">' + escHtml(t.verification_url) + '</a></div>' : ''}
      ${hasSubs ? _renderPlanTree(t.subtasks, depth + 1) : ''}
    </div>`;
  }
  html += '</div>';
  return html;
}

function _countTasks(tasks) {
  let c = 0;
  for (const t of tasks) { c++; if (t.subtasks) c += _countTasks(t.subtasks); }
  return c;
}

async function _acceptPlan() {
  if (!_plannerProposal || !_plannerProposal.tasks) return;
  const btn = document.getElementById('planner-accept-btn');
  if (btn) { btn.disabled = true; btn.textContent = 'Creating\u2026'; }

  try {
    const res = await fetch('/api/kanban/planner/accept', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ proposal: _plannerProposal, insert_position: _insertPosition, parent_id: _plannerScopeParentId || null }),
    });
    if (!res.ok) throw new Error('Failed to create tasks');
    const data = await res.json();
    const count = data.created_count || 0;
    const createdIds = data.created_ids || [];
    if (typeof showToast === 'function') showToast('Created ' + count + ' task' + (count !== 1 ? 's' : ''));
    _plannerStashed = null;
    const _scopedParent = _plannerScopeParentId;  // save before clearing
    _plannerScopeParentId = null;
    // Detach listeners BEFORE closing so late idle events don't
    // trigger _showPlanResult('') and flash the parse error.
    _detachPlannerListeners();
    _plannerProposal = null;
    _plannerSessionId = null;
    _closePlannerSlideout();
    // Store IDs globally so renderKanbanBoard can highlight them
    window._kanbanHighlightIds = new Set(createdIds);
    // Re-render: if scoped plan, reopen the parent drill-down to show new subtasks
    setTimeout(async () => {
      if (_scopedParent && typeof renderTaskDetail === 'function') {
        await renderTaskDetail(_scopedParent);
      } else {
        if (typeof initKanban === 'function') await initKanban(true);
      }
      // Clear highlight set after animation completes
      setTimeout(() => { window._kanbanHighlightIds = null; }, 4000);
    }, 350);
  } catch (e) {
    if (typeof showToast === 'function') showToast('Failed: ' + e.message, true);
    if (btn) { btn.disabled = false; btn.textContent = 'Add to Board'; }
  }
}

function _closePlannerSlideout() {
  if (_plannerProposal && _plannerProposal.tasks && _plannerProposal.tasks.length > 0) {
    _plannerStashed = _plannerProposal;
  }
  _plannerProposal = null;
  _detachPlannerListeners();
  _plannerSessionId = null;
  const panel = document.getElementById('kanban-planner-panel');
  if (panel) {
    panel.classList.remove('open');
    setTimeout(() => panel.remove(), 300);
  }
  // Clear persisted state
  _persistPlannerState(null);
  // Re-render drill-down so chooser cards reset from spinning state
  if (kanbanDetailTaskId) {
    setTimeout(() => renderTaskDetail(kanbanDetailTaskId), 320);
  }
}

function _minimizePlanner() {
  const panel = document.getElementById('kanban-planner-panel');
  if (!panel) return;
  panel.classList.add('minimized');
  _persistPlannerState('minimized');
}

function _openScopedPlanner(parentId, existingCount) {
  if (existingCount > 0) {
    _kanbanConfirm(
      'Replace subtasks?',
      'This will replace the ' + existingCount + ' existing subtask' + (existingCount !== 1 ? 's' : '') + ' with AI-generated ones.',
      () => _doOpenScopedPlanner(parentId),
      { btnLabel: 'Continue', btnStyle: 'background:var(--purple);border-color:var(--purple);' }
    );
    return;
  }
  _doOpenScopedPlanner(parentId);
}

function _doOpenScopedPlanner(parentId) {
  _plannerScopeParentId = parentId;
  fetch('/api/kanban/tasks/' + parentId).then(r => r.json()).then(data => {
    const title = data.title || 'this task';
    const desc = data.description || '';
    // Tell the user (and eventually the AI) that this is a subtree edit.
    // The prefill is just context for the user to edit before sending.
    _openPlannerPanel('Redesign the subtree for: "' + title + '"' + (desc ? '\nContext: ' + desc : ''));
  }).catch(() => {
    _openPlannerPanel('Redesign this task and its subtree');
  });
}

/** Open the planner panel without starting a session — user types first. */
function _openPlannerPanel(prefill) {
  const old = document.getElementById('kanban-planner-panel');
  if (old) old.remove();
  _plannerProposal = null;

  const panel = _buildPlannerPanel();
  document.body.appendChild(panel);
  requestAnimationFrame(() => panel.classList.add('open'));
  setTimeout(_wirePlannerVoice, 350);

  const tb = document.getElementById('main-toolbar');
  if (tb) tb.style.display = 'none';
  const inputBar = document.getElementById('live-input-bar');
  if (inputBar) inputBar.style.display = 'none';
  _persistPlannerState('open');

  // Show empty state with prompt in the textarea
  const body = document.getElementById('planner-body');
  if (body) body.innerHTML = '<div class="planner-status" style="opacity:0.5;"><span>Describe what you want to plan, then press Enter.</span></div>';
  const ta = document.getElementById('planner-refine-input');
  if (ta && prefill) {
    ta.value = prefill;
    ta.placeholder = 'Describe your plan\u2026';
    setTimeout(() => { ta.focus(); ta.select(); }, 400);
  }
}

function _restorePlanner() {
  const panel = document.getElementById('kanban-planner-panel');
  if (panel) {
    panel.classList.remove('minimized');
  }
  _persistPlannerState('open');
}

function _persistPlannerState(state) {
  // state: 'open', 'minimized', or null (closed)
  const url = new URL(window.location);
  if (state) {
    url.searchParams.set('planner', state);
  } else {
    url.searchParams.delete('planner');
  }
  history.replaceState(null, '', url);
  if (state) {
    localStorage.setItem('plannerState', state);
  } else {
    localStorage.removeItem('plannerState');
    localStorage.removeItem('plannerStash');
  }
  // Stash proposal so it survives refresh
  if (state && _plannerProposal) {
    localStorage.setItem('plannerStash', JSON.stringify(_plannerProposal));
  }
  // Persist session ID so we can reconnect after refresh
  if (state && _plannerSessionId) {
    localStorage.setItem('plannerSessionId', _plannerSessionId);
  } else if (!state) {
    localStorage.removeItem('plannerSessionId');
  }
}

function _restorePlannerOnLoad() {
  const state = new URL(window.location).searchParams.get('planner') || localStorage.getItem('plannerState');
  if (!state) return;
  const stash = localStorage.getItem('plannerStash');
  if (stash) {
    try { _plannerProposal = JSON.parse(stash); } catch (_) {}
  }
  const savedSessionId = localStorage.getItem('plannerSessionId');
  const hasProposal = _plannerProposal || _plannerStashed;
  const hasRunningSession = savedSessionId && !hasProposal;

  if (!hasProposal && !hasRunningSession) return;  // nothing to show or reconnect to
  if (!_plannerProposal && _plannerStashed) _plannerProposal = _plannerStashed;

  // Rebuild panel
  const old = document.getElementById('kanban-planner-panel');
  if (old) old.remove();
  const panel = _buildPlannerPanel();
  document.body.appendChild(panel);

  if (_plannerProposal && _plannerProposal.tasks) {
    _showPlanResult(JSON.stringify(_plannerProposal));
  } else if (hasRunningSession) {
    // Reconnect to the in-progress planner session
    _plannerSessionId = savedSessionId;
    if (typeof _hiddenSessionIds !== 'undefined') _hiddenSessionIds.add(savedSessionId);
    _plannerAccumText = '';
    _plannerSteps = [];
    _earlyParseAttempted = false;
    _plannerStartTime = Date.now();
    _attachPlannerListeners();

    // Backfill: fetch existing session log to recover steps and any text so far
    fetch('/api/session-log/' + savedSessionId).then(r => r.ok ? r.json() : []).then(entries => {
      if (!Array.isArray(entries)) entries = entries.entries || [];
      for (const e of entries) {
        if (e.kind === 'tool_use') {
          _plannerSteps.push(e.desc || e.name || 'Working\u2026');
        } else if (e.kind === 'asst' && e.text) {
          _plannerAccumText += e.text;
        }
      }
      // If the session already finished while we were refreshing, show result
      if (_plannerAccumText) _tryEarlyParse();
      _updatePlannerProgress();
    }).catch(() => { _updatePlannerProgress(); });
  }

  if (state === 'minimized') {
    panel.classList.add('open');
    requestAnimationFrame(() => panel.classList.add('minimized'));
  } else {
    requestAnimationFrame(() => panel.classList.add('open'));
  }
  setTimeout(_wirePlannerVoice, 350);
}

function _resumePlan() {
  if (!_plannerStashed) return;
  _plannerProposal = _plannerStashed;
  _plannerStashed = null;

  const old = document.getElementById('kanban-planner-panel');
  if (old) old.remove();

  // Build panel with tree already rendered
  const panel = _buildPlannerPanel();
  const body = panel.querySelector('#planner-body');
  const count = _countTasks(_plannerProposal.tasks);
  body.innerHTML =
    '<div class="planner-result">' +
      '<div class="planner-result-header">' + KI.check + ' <strong>' + count + ' tasks</strong> proposed</div>' +
      _renderPlanTree(_plannerProposal.tasks) +
      '<div class="planner-actions">' +
        '<button class="planner-accept-btn" id="planner-accept-btn" onclick="_acceptPlan()">Add ' + count + ' tasks to Board</button>' +
      '</div>' +
      '<div class="planner-hint">Want changes? Type below and send.</div>' +
    '</div>';

  document.body.appendChild(panel);
  requestAnimationFrame(() => panel.classList.add('open'));
  _closePm();
  setTimeout(_wirePlannerVoice, 350);
}

let _insertPosition = 'top';
function _setInsertPos(pos) {
  _insertPosition = pos;
  document.getElementById('kb-pos-top')?.classList.toggle('active', pos === 'top');
  document.getElementById('kb-pos-bottom')?.classList.toggle('active', pos === 'bottom');
}

async function _submitCreateTask(status, parentId) {
  const input = document.getElementById('kanban-new-task-input');
  const title = input ? input.value.trim() : '';
  if (!title) { if (input) input.focus(); return; }
  const insertPos = _insertPosition;
  _closePm();

  // Optimistic: insert a ghost card into the target column
  const targetStatus = status || 'not_started';
  const col = document.querySelector(`.kanban-column[data-status="${targetStatus}"] .kanban-column-body`);
  let ghostCard = null;
  if (col) {
    ghostCard = document.createElement('div');
    ghostCard.className = 'kanban-card';
    ghostCard.style.opacity = '0.5';
    ghostCard.innerHTML = `<div class="kanban-card-header"><span class="kanban-card-title">${escHtml(title)}</span></div><div style="font-size:10px;color:var(--text-dim);padding:4px 12px 8px;"><span class="spinner" style="width:10px;height:10px;vertical-align:middle;margin-right:4px;"></span>Creating...</div>`;
    if (insertPos === 'top') {
      col.prepend(ghostCard);
    } else {
      col.appendChild(ghostCard);
    }
    // Update column count
    const countEl = col.closest('.kanban-column')?.querySelector('.kanban-column-count');
    if (countEl) countEl.textContent = col.querySelectorAll('.kanban-card').length;
  }

  try {
    const body = { title, insert_position: insertPos };
    if (status) body.status = status;
    if (parentId) body.parent_id = parentId;

    const res = await fetch('/api/kanban/tasks', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
    });
    if (!res.ok) {
      const err = await res.json().catch(() => ({}));
      throw new Error(err.error || 'Create failed');
    }
    if (typeof showToast === 'function') showToast('Task created');
    initKanban(true);
  } catch (e) {
    console.error('[Kanban] Create failed:', e);
    if (ghostCard) ghostCard.remove();
    if (typeof showToast === 'function') showToast('Failed: ' + e.message, true);
  }
}

/**
 * updateTask(taskId, fields) — PATCH /api/kanban/tasks/:id
 */
async function updateTask(taskId, fields) {
  try {
    const res = await fetch('/api/kanban/tasks/' + taskId, {
      method: 'PATCH',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(fields),
    });
    if (!res.ok) {
      const err = await res.json().catch(() => ({}));
      throw new Error(err.error || 'Update failed');
    }
    return await res.json();
  } catch (e) {
    console.error('[Kanban] Update failed:', e);
    if (typeof showToast === 'function') showToast('Failed: ' + e.message, true);
    return null;
  }
}

/**
 * inlineEditTitle(taskId, event) — Click-to-rename on card title.
 */
function inlineEditTitle(taskId, event) {
  const span = event.target;
  const oldTitle = span.textContent;
  const input = document.createElement('input');
  input.type = 'text';
  input.value = oldTitle;
  input.className = 'kanban-inline-edit';
  input.onblur = async () => {
    const newTitle = input.value.trim();
    if (newTitle && newTitle !== oldTitle) {
      await updateTask(taskId, { title: newTitle });
      initKanban(true);
    } else {
      span.textContent = oldTitle;
      input.replaceWith(span);
    }
  };
  input.onkeydown = (e) => {
    if (e.key === 'Enter') input.blur();
    if (e.key === 'Escape') { input.value = oldTitle; input.blur(); }
  };
  span.replaceWith(input);
  input.focus();
  input.select();
}


// ═══════════════════════════════════════════════════════════════
// EXPAND / DRILL-DOWN
// ═══════════════════════════════════════════════════════════════

/**
 * toggleExpand(taskId) — Expand/collapse subtask & session list within card.
 * Plan Section 2: "click to drill into subtasks & sessions within the card"
 */
async function toggleExpand(taskId) {
  if (kanbanExpandedTasks.has(taskId)) {
    kanbanExpandedTasks.delete(taskId);
    const el = document.getElementById('kanban-children-' + taskId);
    if (el) el.remove();
    const chevron = document.querySelector(`.kanban-card[data-task-id="${taskId}"] .kanban-expand-btn`);
    if (chevron) chevron.classList.remove('expanded');
  } else {
    kanbanExpandedTasks.add(taskId);
    await loadExpandedContent(taskId);
    const chevron = document.querySelector(`.kanban-card[data-task-id="${taskId}"] .kanban-expand-btn`);
    if (chevron) chevron.classList.add('expanded');
  }
  // Persist expanded state
  localStorage.setItem('kanbanExpanded', JSON.stringify([...kanbanExpandedTasks]));
}

/**
 * loadExpandedContent(taskId) — Fetch task detail and render expanded panel.
 */
async function loadExpandedContent(taskId) {
  const card = document.querySelector(`.kanban-card[data-task-id="${taskId}"]`);
  if (!card) return;

  let childrenEl = document.getElementById('kanban-children-' + taskId);
  if (!childrenEl) {
    childrenEl = document.createElement('div');
    childrenEl.className = 'kanban-children';
    childrenEl.id = 'kanban-children-' + taskId;
    card.appendChild(childrenEl);
  }
  childrenEl.innerHTML = '<div class="kanban-expand-loading"><div class="skel-shimmer" style="width:100%;height:28px;border-radius:6px;margin-bottom:4px;"></div><div class="skel-shimmer" style="width:85%;height:28px;border-radius:6px;"></div></div>';

  try {
    const res = await fetch('/api/kanban/tasks/' + taskId);
    if (!res.ok) throw new Error('Failed to load task');
    const taskData = await res.json();
    childrenEl.innerHTML = renderExpandedContent(taskData);
  } catch (e) {
    childrenEl.innerHTML = '<div class="kanban-expand-error">Failed to load</div>';
  }
}

/**
 * drillDown(taskId, title) — Scope the board to a parent task's children.
 * Plan Section 2: Breadcrumb trail for deeply nested subtasks.
 */
async function drillDown(taskId, title) {
  navigateToTask(taskId);
}


// ═══════════════════════════════════════════════════════════════
// CONTEXT MENU
// ═══════════════════════════════════════════════════════════════

function showCardContextMenu(taskId, event) {
  event.stopPropagation();
  // Close any existing menu
  closeContextMenu();

  const menu = document.createElement('div');
  menu.className = 'kanban-context-menu';

  // Position: use mouse coords for right-click, button rect for dot-menu click
  if (event.type === 'contextmenu') {
    menu.style.top = event.clientY + 'px';
    menu.style.left = event.clientX + 'px';
  } else {
    const rect = event.currentTarget.getBoundingClientRect();
    menu.style.top = rect.bottom + 'px';
    menu.style.left = rect.left + 'px';
  }

  // Build menu items
  let items = '';
  items += `<div class="kanban-context-item" onclick="closeContextMenu();inlineEditTitle('${taskId}', event)">Rename</div>`;
  items += `<div class="kanban-context-item" onclick="closeContextMenu();openTaskDetail('${taskId}')">Edit</div>`;
  items += `<div class="kanban-context-item" onclick="closeContextMenu();createTask('not_started','${taskId}')">Add Subtask</div>`;
  items += `<div class="kanban-context-item" onclick="closeContextMenu();openSessionSpawner('${taskId}')">Spawn Session</div>`;

  // Move to submenu
  items += '<div class="kanban-context-separator"></div>';
  for (const col of kanbanColumns) {
    items += `<div class="kanban-context-item kanban-context-move" onclick="closeContextMenu();moveTaskToColumn('${taskId}','${col.status_key}')">Move to ${escHtml(col.name)}</div>`;
  }

  items += '<div class="kanban-context-separator"></div>';
  items += `<div class="kanban-context-item kanban-context-danger" onclick="closeContextMenu();deleteKanbanTask('${taskId}')">Delete</div>`;

  menu.innerHTML = items;
  document.body.appendChild(menu);

  // Close on click outside
  setTimeout(() => {
    document.addEventListener('click', closeContextMenu, { once: true });
  }, 0);
}

function closeContextMenu() {
  document.querySelectorAll('.kanban-context-menu').forEach(el => el.remove());
}

function deleteKanbanTask(taskId) {
  const card = document.querySelector('.kanban-card[data-task-id="' + taskId + '"]');
  const title = card ? (card.querySelector('.kanban-card-title')?.textContent || 'this task') : 'this task';

  // Inline confirm on the card itself
  if (card) {
    const old = card.innerHTML;
    card.innerHTML = '<div class="kanban-delete-confirm"><span>Delete "' + escHtml(title.slice(0, 30)) + '"?</span><div class="kanban-delete-btns"><button class="kanban-delete-yes" onclick="event.stopPropagation();_execDelete(\'' + taskId + '\')">Delete</button><button class="kanban-delete-no" onclick="event.stopPropagation();_cancelDelete(this,\'' + taskId + '\')">Cancel</button></div></div>';
    card._oldHtml = old;
    card.onclick = null;
  } else {
    _execDelete(taskId);
  }
}

function _cancelDelete(btn, taskId) {
  const card = document.querySelector('.kanban-card[data-task-id="' + taskId + '"]');
  if (card && card._oldHtml) { card.innerHTML = card._oldHtml; card.onclick = () => navigateToTask(taskId); }
}

async function _execDelete(taskId) {
  const card = document.querySelector('.kanban-card[data-task-id="' + taskId + '"]');
  if (card) {
    // Update column count immediately
    const col = card.closest('.kanban-column');
    card.remove();
    if (col) {
      const countEl = col.querySelector('.kanban-column-count');
      const remaining = col.querySelectorAll('.kanban-card').length;
      if (countEl) countEl.textContent = remaining;
    }
  }
  try {
    const res = await fetch('/api/kanban/tasks/' + taskId, { method: 'DELETE' });
    if (!res.ok) throw new Error('Delete failed');
    if (typeof showToast === 'function') showToast('Task deleted');
    if (kanbanDetailTaskId) navigateToBoard();
    else initKanban(true);
  } catch (e) {
    if (typeof showToast === 'function') showToast('Failed to delete task');
    initKanban(true);
  }
}

async function moveTaskToColumn(taskId, targetStatus) {
  // Optimistic UI — move card immediately on the board
  if (!kanbanDetailTaskId) {
    const card = document.querySelector(`.kanban-card[data-task-id="${taskId}"]`);
    const targetCol = document.querySelector(`.kanban-column[data-status="${targetStatus}"] .kanban-column-body`);
    if (card && targetCol) {
      card.dataset.status = targetStatus;
      targetCol.appendChild(card);
    }
  }

  try {
    const res = await fetch('/api/kanban/tasks/' + taskId + '/move', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ status: targetStatus, force: true }),
    });
    if (!res.ok) {
      const err = await res.json().catch(() => ({}));
      throw new Error(err.error || 'Move failed');
    }
    if (!kanbanDetailTaskId) initKanban(true);
  } catch (e) {
    if (typeof showToast === 'function') showToast(e.message, true);
    if (!kanbanDetailTaskId) initKanban(true);
  }
}


// ═══════════════════════════════════════════════════════════════
// STATUS MENU (inline status change from drill-down view)
// ═══════════════════════════════════════════════════════════════

function showStatusMenu(taskId, currentStatus, event) {
  event.stopPropagation();
  // Close any existing menus
  closeContextMenu();
  document.querySelectorAll('.kanban-status-menu').forEach(el => el.remove());

  const menu = document.createElement('div');
  menu.className = 'kanban-status-menu';

  const rect = event.currentTarget.getBoundingClientRect();
  menu.style.top = (rect.bottom + 4) + 'px';
  menu.style.left = rect.left + 'px';

  let items = '';
  for (const col of kanbanColumns) {
    const isCurrent = col.status_key === currentStatus;
    const color = KANBAN_STATUS_COLORS[col.status_key] || col.color || 'var(--text-muted)';
    items += `<div class="kanban-status-menu-item${isCurrent ? ' current' : ''}" onclick="event.stopPropagation();closeStatusMenu();changeTaskStatus('${taskId}','${col.status_key}')">
      <span class="kanban-status-menu-dot" style="background:${color};"></span>
      ${escHtml(col.name)}
      ${isCurrent ? ' ' + KI.check : ''}
    </div>`;
  }

  menu.innerHTML = items;
  document.body.appendChild(menu);

  setTimeout(() => {
    document.addEventListener('click', closeStatusMenu, { once: true });
  }, 0);
}

function closeStatusMenu() {
  document.querySelectorAll('.kanban-status-menu').forEach(el => el.remove());
}

async function changeTaskStatus(taskId, newStatus) {
  try {
    const res = await fetch('/api/kanban/tasks/' + taskId + '/move', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ status: newStatus, force: true }),
    });
    if (!res.ok) {
      const err = await res.json().catch(() => ({}));
      throw new Error(err.error || 'Status change failed');
    }
    // Update the status badge in DOM instead of full re-render
    if (kanbanDetailTaskId) {
      const row = document.querySelector('.kanban-drill-subtask-row[data-task-id="' + taskId + '"]');
      if (row) {
        const badge = row.querySelector('.kanban-drill-subtask-status');
        const cc = KANBAN_STATUS_COLORS[newStatus] || 'var(--text-muted)';
        const label = KANBAN_STATUS_LABELS[newStatus] || newStatus;
        if (badge) {
          badge.style.background = cc + '26';
          badge.style.color = cc;
          badge.textContent = label;
          // Update the onclick so the menu shows the correct checkmark next time
          badge.setAttribute('onclick', "event.stopPropagation();showStatusMenu('" + taskId + "', '" + newStatus + "', event)");
        }
      }
      _updateSubtaskCount();
    } else {
      initKanban(true);
    }
  } catch (e) {
    if (typeof showToast === 'function') showToast(e.message, true);
  }
}


// ═══════════════════════════════════════════════════════════════
// DRILL-DOWN TASK DETAIL VIEW (plan Section 2 mockups)
// Full-width view replacing board — breadcrumb navigation,
// two-panel layout (subtasks + sessions), recursive drill-down.
// ═══════════════════════════════════════════════════════════════

/**
 * renderTaskDetail(taskId) — Full-width drill-down view.
 * Plan Section 2: "Clicking a task card replaces the board with a
 * full-width task detail view. The board columns disappear — you're
 * now 'inside' the task. Browser back returns to the board."
 */
async function renderTaskDetail(taskId) {
  // NEVER blow away a live session
  if (liveSessionId && window._kanbanSessionTaskId) return;

  kanbanDetailTaskId = taskId;
  const board = document.getElementById('kanban-board');
  if (!board) return;

  // Only show loading skeleton if the board is empty or showing columns (first load)
  // Don't flash "Loading..." when re-rendering an already-visible drill-down
  if (!board.querySelector('.kanban-drill-titlebar')) {
    board.innerHTML = _taskDetailSkeleton();
  }

  try {
    // Fetch task detail + ancestors in parallel
    const [taskRes, ancRes] = await Promise.all([
      fetch('/api/kanban/tasks/' + taskId),
      fetch('/api/kanban/tasks/' + taskId + '/ancestors'),
    ]);
    if (!taskRes.ok) throw new Error('Failed to load task');
    const task = await taskRes.json();
    // Store title for breadcrumb use by session opener
    window._kanbanDetailTaskTitle = task.title || '';
    let ancestors = [];
    if (ancRes.ok) {
      const ancData = await ancRes.json();
      ancestors = (ancData.ancestors || []).slice().reverse();
    }

    const children = task.children || [];
    const sessions = task.sessions || [];
    const childCount = children.length;
    const childDone = children.filter(c => c.status === 'complete').length;
    const isLeaf = childCount === 0;
    const statusColor = KANBAN_STATUS_COLORS[task.status] || 'var(--text-muted)';
    const statusLabel = KANBAN_STATUS_LABELS[task.status] || task.status;

    // ── Navigation title bar: breadcrumb + actions ──
    let html = '<div class="kanban-drill-titlebar">';
    html += '<div class="kanban-drill-breadcrumb">';
    html += '<span class="kanban-drill-crumb" onclick="navigateToBoard()">' + KI.menu + ' Board</span>';
    for (const a of ancestors) {
      html += '<span class="kanban-drill-sep">' + KI.chevronR + '</span>';
      html += `<span class="kanban-drill-crumb" onclick="navigateToTask('${a.id}')">${escHtml(a.title)}</span>`;
    }
    html += '<span class="kanban-drill-sep">' + KI.chevronR + '</span>';
    html += `<span class="kanban-drill-crumb current">${escHtml(task.title)}</span>`;
    html += '</div></div>';

    // ── Task detail body — left/right split layout ──
    html += '<div class="kanban-drill-body">';
    html += '<div class="kanban-drill-split">';

    // ════════════ LEFT: Task info ════════════
    html += '<div class="kanban-drill-left">';

    // Status badge
    html += `<div class="kanban-drill-status kanban-status-clickable" style="background:${statusColor}26;color:${statusColor};cursor:pointer;" onclick="event.stopPropagation();showStatusMenu('${task.id}', '${task.status}', event)" title="Click to change status">${escHtml(statusLabel)} ${KI.chevronR}</div>`;

    // Title (click to edit)
    html += `<div class="kanban-drill-title" id="kanban-drill-title" onclick="_startTitleEdit('${task.id}', this)" title="Click to edit">${escHtml(task.title)}</div>`;

    // Timestamps
    const createdTime = task.created_at ? _shortDate(task.created_at) : '—';
    const updatedTime = task.updated_at ? _shortDate(task.updated_at) : '—';
    html += `<div style="font-size:11px;color:var(--text-dim);margin:4px 0 16px;">Created ${escHtml(createdTime)} &middot; Updated ${escHtml(updatedTime)}</div>`;

    // Description (Quill RTE — toolbar hidden until focus)
    html += '<div class="kanban-drill-desc-wrap kanban-drill-desc-collapsed">';
    // If description looks like Markdown (has bullet points, bold, etc.), render it as HTML.
    // If it's already HTML (from Quill), pass through as-is.
    const _rawDesc = task.description || '';
    const _descIsHtml = _rawDesc.includes('<p>') || _rawDesc.includes('<ul>') || _rawDesc.includes('<li>') || _rawDesc.includes('<strong>');
    const _descHtml = _descIsHtml ? _rawDesc : (typeof mdParse === 'function' ? mdParse(_rawDesc) : escHtml(_rawDesc));
    html += `<div id="kanban-drill-desc-editor" class="kanban-drill-desc">${_descHtml}</div>`;
    html += '</div>';

    // Verification URL field (between description and tags)
    const _verUrl = resolveVerificationUrl(task.verification_url);
    html += `<div class="kanban-drill-ver-section" id="kanban-drill-ver-section">`;
    if (_verUrl) {
      html += `<div class="kanban-drill-ver-row">`;
      html += `<span class="kanban-drill-ver-icon">${KI.link}</span>`;
      html += `<a class="kanban-drill-ver-link" href="${escHtml(_verUrl)}" target="_blank" rel="noopener" title="${escHtml(_verUrl)}">${escHtml(_verUrl)}</a>`;
      html += `<button class="kanban-drill-ver-action" data-ver-url="${escHtml(task.verification_url || '')}" onclick="event.stopPropagation();_editVerificationUrl('${task.id}', this.dataset.verUrl)" title="Edit URL"><svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><path d="M11 4H4a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h14a2 2 0 0 0 2-2v-7"/><path d="M18.5 2.5a2.12 2.12 0 0 1 3 3L12 15l-4 1 1-4 9.5-9.5z"/></svg></button>`;
      html += `<button class="kanban-drill-ver-action kanban-drill-ver-action-danger" onclick="event.stopPropagation();_clearVerificationUrl('${task.id}')" title="Remove URL"><svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><line x1="18" y1="6" x2="6" y2="18"/><line x1="6" y1="6" x2="18" y2="18"/></svg></button>`;
      html += `</div>`;
    } else {
      html += `<div class="kanban-drill-ver-empty" onclick="_editVerificationUrl('${task.id}', '')">`;
      html += `<span class="kanban-drill-ver-icon" style="opacity:0.4">${KI.link}</span>`;
      html += `<span style="color:var(--text-dim);font-size:12px;">Add validation URL\u2026</span>`;
      html += `</div>`;
    }
    html += `</div>`;

    // Tags (with tag icon)
    const taskTags = task.tags || [];
    html += '<div class="kanban-drill-tags-section">';
    html += '<div class="kanban-drill-tags-list">';
    for (const tag of taskTags) {
      const tc = tagColorHash(tag);
      html += `<span class="kanban-tag-pill" style="background:${tc}22;color:${tc};border-color:${tc}44;">${escHtml(tag)}<button class="kanban-tag-remove" onclick="event.stopPropagation();removeTag('${task.id}','${escHtml(tag.replace(/'/g, "\\'"))}')" title="Remove tag">&times;</button></span>`;
    }
    html += '</div>';
    html += `<span class="kanban-tag-add-trigger">${KI.tag || '<svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M20.59 13.41l-7.17 7.17a2 2 0 0 1-2.83 0L2 12V2h10l8.59 8.59a2 2 0 0 1 0 2.82z"/><line x1="7" y1="7" x2="7.01" y2="7"/></svg>'}<input type="text" id="kanban-tag-input" class="kanban-tag-inline-input" placeholder="Add tag" autocomplete="off" oninput="onTagInput(this.value, '${task.id}')" onkeydown="if(event.key==='Enter'){event.preventDefault();event.stopPropagation();addTagFromInput('${task.id}');}"></span>`;
    html += '<div id="kanban-tag-suggestions" class="kanban-tag-suggestions"></div>';
    html += '</div>';

    html += '</div>'; // drill-left

    // ════════════ RIGHT: Chooser / Subtasks / Sessions ════════════
    const hasChildren = childCount > 0;
    const hasSessions = sessions.length > 0;
    const mode = hasChildren ? 'subtasks' : hasSessions ? 'sessions' : 'empty';

    html += '<div class="kanban-drill-right">';

    if (mode === 'empty') {
      // ── Chooser: no subtasks and no sessions yet ──
      html += '<div class="kanban-drill-chooser">';
      html += '<div style="font-size:12px;color:var(--text-dim);margin-bottom:6px;text-transform:uppercase;letter-spacing:0.5px;font-weight:600;">How to proceed</div>';
      html += `<div class="kanban-drill-chooser-card" onclick="_chooserAction(this, () => createSubtaskInline('${task.id}'))">`;
      html += '<div class="kanban-drill-chooser-icon" style="color:var(--accent);"><svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><line x1="8" y1="6" x2="21" y2="6"/><line x1="8" y1="12" x2="21" y2="12"/><line x1="8" y1="18" x2="21" y2="18"/><line x1="3" y1="6" x2="3.01" y2="6"/><line x1="3" y1="12" x2="3.01" y2="12"/><line x1="3" y1="18" x2="3.01" y2="18"/></svg></div>';
      html += '<div><div class="kanban-drill-chooser-title">Break into subtasks</div>';
      html += '<div class="kanban-drill-chooser-desc">Subdivide into smaller pieces. Each subtask gets its own status and sessions.</div></div>';
      html += '</div>';
      html += `<div class="kanban-drill-chooser-card" onclick="_chooserAction(this, () => openSessionSpawner('${task.id}'))">`;
      html += '<div class="kanban-drill-chooser-icon" style="color:var(--green);"><svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><polygon points="13 2 3 14 12 14 11 22 21 10 12 10 13 2"/></svg></div>';
      html += '<div><div class="kanban-drill-chooser-title">Spawn sessions</div>';
      html += '<div class="kanban-drill-chooser-desc">Start working directly. Spawn Claude sessions scoped to this task.</div></div>';
      html += '</div>';
      html += `<div class="kanban-drill-chooser-card kanban-drill-chooser-ai" onclick="_chooserAction(this, () => _openScopedPlanner('${task.id}', 0))">`;
      html += `<div class="kanban-drill-chooser-icon" style="color:var(--purple);">${KI.plan}</div>`;
      html += '<div><div class="kanban-drill-chooser-title">Plan with AI</div>';
      html += '<div class="kanban-drill-chooser-desc">Describe a goal and Claude will break it down into a structured set of subtasks.</div></div>';
      html += '<svg class="kanban-drill-chooser-ai-arrow" width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><path d="M5 12h14"/><path d="M12 5l7 7-7 7"/></svg>';
      html += '</div>';
      html += '</div>';

    } else if (mode === 'subtasks') {
      const pct = childCount > 0 ? Math.round((childDone / childCount) * 100) : 0;
      html += '<div class="kanban-drill-panel-header">';
      html += `<span style="font-size:11px;font-weight:700;text-transform:uppercase;letter-spacing:0.8px;color:var(--text-dim);">Subtasks</span>`;
      html += `<span class="kanban-drill-switch-icon" onclick="confirmModeSwitch('${task.id}', 'sessions')" title="Switch to sessions">${KI.refresh || '&#8644;'}</span>`;
      html += `<span class="kanban-drill-inline-progress"><span class="kanban-drill-inline-bar"><span class="kanban-drill-inline-fill" style="width:${pct}%"></span></span><span class="kanban-drill-inline-pct">${pct}%</span></span>`;
      html += '</div>';
      html += '<div class="kanban-drill-panel"><div class="kanban-drill-panel-body" id="kanban-subtask-list">';
      for (let si = 0; si < children.length; si++) {
        const child = children[si];
        const cc = KANBAN_STATUS_COLORS[child.status] || 'var(--text-muted)';
        const isWorking = child.status === 'working';
        const childStatusLabel = KANBAN_STATUS_LABELS[child.status] || child.status;

        html += `<div class="kanban-drill-subtask-row" draggable="true" data-task-id="${child.id}" data-idx="${si}" ondragstart="_subtaskDragStart(event)" ondragover="_subtaskDragOver(event)" ondrop="_subtaskDrop(event,'${task.id}')" ondragend="_subtaskDragEnd(event)">`;
        // Drag handle
        html += `<span class="kanban-drill-subtask-grip" title="Drag to reorder">${KI.drag || '<svg width="10" height="10" viewBox="0 0 24 24" fill="currentColor"><circle cx="9" cy="5" r="1.5"/><circle cx="15" cy="5" r="1.5"/><circle cx="9" cy="12" r="1.5"/><circle cx="15" cy="12" r="1.5"/><circle cx="9" cy="19" r="1.5"/><circle cx="15" cy="19" r="1.5"/></svg>'}</span>`;
        // Status badge
        html += `<div class="kanban-drill-subtask-status kanban-status-clickable" style="background:${cc}26;color:${cc};" onclick="event.stopPropagation();showStatusMenu('${child.id}', '${child.status}', event)" title="Click to change status">${escHtml(childStatusLabel)}</div>`;
        // Title — click to drill
        html += `<span class="kanban-drill-subtask-title" onclick="navigateToTask('${child.id}')">${escHtml(child.title)}</span>`;
        // Meta: subtask + session counts
        const childSubs = child.children_count || 0;
        const childSess = child.session_count || 0;
        if (childSubs > 0 || childSess > 0) {
          html += '<span class="kanban-drill-subtask-meta">';
          if (childSubs > 0) html += childSubs + ' subtask' + (childSubs !== 1 ? 's' : '');
          if (childSubs > 0 && childSess > 0) html += ' \u00B7 ';
          if (childSess > 0) html += childSess + ' session' + (childSess !== 1 ? 's' : '');
          html += '</span>';
        }
        // Hover actions: edit, delete
        html += `<div class="kanban-drill-subtask-actions">`;
        html += `<button class="kanban-subtask-action-btn" onclick="event.stopPropagation();_inlineRenameSubtask('${child.id}', this.closest('.kanban-drill-subtask-row').querySelector('.kanban-drill-subtask-title'))" title="Rename"><svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><path d="M11 4H4a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h14a2 2 0 0 0 2-2v-7"/><path d="M18.5 2.5a2.12 2.12 0 0 1 3 3L12 15l-4 1 1-4 9.5-9.5z"/></svg></button>`;
        html += `<button class="kanban-subtask-action-btn kanban-subtask-action-danger" onclick="event.stopPropagation();_deleteSubtask('${task.id}','${child.id}')" title="Delete"><svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><polyline points="3 6 5 6 21 6"/><path d="M19 6v14a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2V6m3 0V4a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2"/></svg></button>`;
        html += `</div>`;
        // Drill chevron
        html += `<span class="kanban-drill-subtask-chevron" onclick="navigateToTask('${child.id}')" title="Open">${KI.chevronR}</span>`;
        html += '</div>';
      }
      // Ghost row — looks like the next subtask, just type
      html += `<div class="kanban-drill-subtask-row kanban-drill-ghost-row" onclick="this.querySelector('input')?.focus()">`;
      html += `<span class="kanban-drill-subtask-grip" style="visibility:hidden;"></span>`;
      html += `<div class="kanban-drill-subtask-status kanban-status-clickable" style="background:var(--bg-subtle);color:var(--text-dim);">new</div>`;
      html += `<input type="text" id="kanban-drill-new-subtask" class="kanban-drill-ghost-input" placeholder="Add subtask\u2026" onkeydown="if(event.key==='Enter'){event.preventDefault();_quickAddSubtask('${task.id}', this);}">`;
      html += `<button class="kanban-drill-ghost-btn" onclick="event.stopPropagation();_quickAddSubtask('${task.id}', document.getElementById('kanban-drill-new-subtask'))">Add</button>`;
      html += `</div>`;
      html += '</div></div>';
      html += `<button class="kanban-drill-ai-plan-btn" onclick="_openScopedPlanner('${task.id}', ${childCount})" title="Re-plan subtasks with AI">${KI.plan} Plan with AI</button>`;

    } else {
      // ── Sessions panel with switch icon ──
      html += '<div class="kanban-drill-panel-header">';
      html += `<span style="font-size:11px;font-weight:700;text-transform:uppercase;letter-spacing:0.8px;color:var(--text-dim);">Sessions</span>`;
      html += `<span class="kanban-drill-switch-icon" onclick="confirmModeSwitch('${task.id}', 'subtasks')" title="Switch to subtasks">${KI.refresh || '&#8644;'}</span>`;
      html += '</div>';
      html += '<div class="kanban-drill-panel"><div class="kanban-drill-panel-body">';
      for (const sess of sessions) {
        const sessId = typeof sess === 'string' ? sess : sess.session_id;
        const sessTitle = _resolveSessionName(sessId);
        const sessStatus = (typeof sess === 'object' && typeof sess.status === 'string' && sess.status) ? sess.status : 'sleeping';
        const sc = sessStatus === 'working' ? 'var(--status-working)' : sessStatus === 'idle' ? 'var(--status-complete)' : 'var(--text-dim)';
        const statusLabel = sessStatus.charAt(0).toUpperCase() + sessStatus.slice(1);

        html += `<div class="kanban-drill-session-row" data-session-id="${escHtml(sessId)}" onclick="_kanbanOpenSession('${task.id}','${escHtml(sessId)}')" style="cursor:pointer;">`;
        html += `<div class="kanban-drill-subtask-status" style="background:${sc}26;color:${sc};">${escHtml(statusLabel)}</div>`;
        html += `<span class="kanban-drill-session-name">${escHtml(sessTitle)}</span>`;
        html += `<div class="kanban-drill-subtask-actions">`;
        html += `<button class="kanban-subtask-action-btn kanban-subtask-action-danger" onclick="event.stopPropagation();_unlinkSession('${task.id}','${escHtml(sessId)}')" title="Remove"><svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><polyline points="3 6 5 6 21 6"/><path d="M19 6v14a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2V6m3 0V4a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2"/></svg></button>`;
        html += `</div>`;
        html += `<span class="kanban-drill-subtask-chevron" onclick="event.stopPropagation();_kanbanOpenSession('${task.id}','${escHtml(sessId)}')" title="Open">${KI.chevronR}</span>`;
        html += '</div>';
      }
      // Ghost row for spawning
      html += `<div class="kanban-drill-session-row kanban-drill-ghost-row" onclick="openSessionSpawner('${task.id}')" style="cursor:pointer;">`;
      html += `<div class="kanban-drill-subtask-status kanban-status-clickable" style="background:var(--bg-subtle);color:var(--text-dim);">new</div>`;
      html += `<span class="kanban-drill-session-name" style="color:var(--text-dim);">Spawn session…</span>`;
      html += `</div>`;
      html += '</div></div>';
    }

    html += '</div>'; // drill-right
    html += '</div>'; // drill-split
    html += '</div>'; // drill-body

    board.innerHTML = html;

    // Initialize Quill RTE — toolbar hidden until focus
    if (typeof Quill !== 'undefined') {
      const descEl = document.getElementById('kanban-drill-desc-editor');
      const descWrap = descEl?.closest('.kanban-drill-desc-wrap');
      if (descEl) {
        kanbanQuillInstance = new Quill(descEl, {
          theme: 'snow',
          placeholder: 'Add a description\u2026',
          modules: {
            toolbar: [
              ['bold', 'italic', 'underline', 'strike'],
              [{ 'list': 'ordered' }, { 'list': 'bullet' }],
              ['link', 'code-block'],
              ['clean'],
            ],
          },
        });

        // Show toolbar on focus, hide on blur
        kanbanQuillInstance.on('selection-change', (range) => {
          if (descWrap) {
            if (range) {
              descWrap.classList.remove('kanban-drill-desc-collapsed');
            } else {
              descWrap.classList.add('kanban-drill-desc-collapsed');
            }
          }
        });

        // Auto-save description on text change (debounced)
        let _descSaveTimer = null;
        kanbanQuillInstance.on('text-change', () => {
          clearTimeout(_descSaveTimer);
          _descSaveTimer = setTimeout(() => {
            const descHtml = kanbanQuillInstance.root.innerHTML;
            updateTask(task.id, { description: descHtml });
          }, 1000);
        });
      }
    }

  } catch (e) {
    console.error('[Kanban] Detail load failed:', e);
    // Auto-retry up to 3 times with increasing delay (task may still be syncing)
    const retryCount = (renderTaskDetail._retries || 0);
    if (retryCount < 3) {
      renderTaskDetail._retries = retryCount + 1;
      console.log('[Kanban] Retrying detail load (' + (retryCount + 1) + '/3)...');
      setTimeout(() => renderTaskDetail(taskId), 800 * (retryCount + 1));
      return;
    }
    renderTaskDetail._retries = 0;
    board.innerHTML =
      '<div class="kanban-empty-state">' +
      '<div style="font-size:15px;font-weight:500;margin-bottom:6px;">Failed to load task</div>' +
      '<div style="font-size:12px;color:var(--text-faint);margin-bottom:14px;">' + escHtml(e.message) + '</div>' +
      '<div style="display:flex;gap:8px;justify-content:center;">' +
      '<button class="kanban-create-first-btn" onclick="renderTaskDetail._retries=0;renderTaskDetail(\'' + escHtml(taskId) + '\')">Retry</button>' +
      '<button class="kanban-create-first-btn" style="background:var(--bg-btn);color:var(--text-secondary);border:1px solid var(--border);" onclick="navigateToBoard()">Back to Board</button>' +
      '</div></div>';
  }
}

/**
 * openTaskDetail(taskId) — Called from context menu "Edit" or hash restore.
 * Delegates to renderTaskDetail after pushing history.
 */
function confirmModeSwitch(taskId, targetMode) {
  const overlay = document.getElementById('pm-overlay');
  if (!overlay) return;

  const isToSessions = targetMode === 'sessions';
  const title = isToSessions ? 'Switch to Sessions' : 'Switch to Subtasks';
  const what = isToSessions
    ? 'All subtasks (and their children) will be <strong>permanently deleted</strong>.'
    : "All linked sessions will be <strong>unlinked</strong> from this task. The sessions themselves are not deleted — they just won't be associated with this task anymore.";
  const why = isToSessions
    ? "You're choosing to work on this task directly with Claude sessions instead of breaking it into subtasks."
    : "You're choosing to break this task into subtasks instead of working on it directly with sessions.";

  const html = `<div class="pm-card pm-enter" style="max-width:440px;">
    <h2 class="pm-title">${title}</h2>
    <div class="pm-body">
      <p style="font-size:13px;color:var(--text-secondary);margin-bottom:12px;">${why}</p>
      <div style="padding:12px;border-radius:8px;border:1px solid var(--red);background:rgba(248,81,73,0.06);font-size:13px;color:var(--text-secondary);margin-bottom:16px;">
        ${what}
      </div>
      <p style="font-size:12px;color:var(--text-dim);">This cannot be undone.</p>
    </div>
    <div class="pm-actions">
      <button class="pm-btn pm-btn-secondary" onclick="_closePm()">Cancel</button>
      <button class="pm-btn pm-btn-danger" onclick="_executeModeSwitch('${taskId}','${targetMode}')">
        ${isToSessions ? 'Delete subtasks & switch' : 'Unlink sessions & switch'}
      </button>
    </div>
  </div>`;

  overlay.innerHTML = html;
  overlay.classList.add('show');
  requestAnimationFrame(() => overlay.querySelector('.pm-card')?.classList.remove('pm-enter'));
  overlay.onclick = (e) => { if (e.target === overlay) _closePm(); };
}

async function _executeModeSwitch(taskId, targetMode) {
  try {
    const taskRes = await fetch('/api/kanban/tasks/' + taskId);
    const task = await taskRes.json();

    if (targetMode === 'sessions') {
      for (const child of (task.children || [])) {
        await fetch('/api/kanban/tasks/' + child.id, { method: 'DELETE' });
      }
    } else {
      for (const sess of (task.sessions || [])) {
        const sessId = typeof sess === 'string' ? sess : sess.session_id;
        await fetch('/api/kanban/tasks/' + taskId + '/sessions/' + sessId, { method: 'DELETE' });
      }
    }

    if (typeof _closePm === 'function') _closePm();
    // Delay to let Supabase sync deletes before fetching fresh state
    await new Promise(r => setTimeout(r, 500));
    renderTaskDetail._retries = 0;
    renderTaskDetail(taskId);
  } catch (e) {
    if (typeof showToast === 'function') showToast('Failed: ' + e.message, true);
  }
}

async function openTaskDetail(taskId) {
  const state = { view: 'kanban', taskId };
  history.pushState(state, '', window.location.pathname + '#kanban/task/' + taskId);
  await renderTaskDetail(taskId);
}

function closeTaskDetail() {
  kanbanDetailTaskId = null;
  kanbanQuillInstance = null;
  navigateToBoard();
}

function _chooserAction(card, fn) {
  card.classList.add('chosen');
  card.style.pointerEvents = 'none';
  card.parentElement?.querySelectorAll('.kanban-drill-chooser-card:not(.chosen)').forEach(el => el.remove());
  // Replace card content with loading state
  const titleEl = card.querySelector('.kanban-drill-chooser-title');
  if (titleEl) titleEl.textContent += '...';
  const descEl = card.querySelector('.kanban-drill-chooser-desc');
  if (descEl) descEl.innerHTML = '<span class="spinner" style="margin-right:6px;"></span>Setting up...';
  fn();
}

function _updateSubtaskCount() {
  const panel = document.getElementById('kanban-subtask-list');
  if (!panel) return;
  const rows = panel.querySelectorAll('.kanban-drill-subtask-row[data-task-id]');
  const total = rows.length;
  const done = [...rows].filter(r => {
    const badge = r.querySelector('.kanban-drill-subtask-status');
    return badge && badge.textContent.trim().toLowerCase() === 'complete';
  }).length;
  const pct = total > 0 ? Math.round((done / total) * 100) : 0;
  // Update header text
  const header = document.querySelector('.kanban-drill-panel-header span');
  if (header && header.textContent.includes('Subtasks')) header.textContent = 'Subtasks';
  // Update progress bar
  const fill = document.querySelector('.kanban-drill-inline-fill');
  if (fill) fill.style.width = pct + '%';
  const pctEl = document.querySelector('.kanban-drill-inline-pct');
  if (pctEl) pctEl.textContent = pct + '%';
}

async function _quickAddSubtask(parentId, input) {
  const title = input.value.trim();
  if (!title) return;

  // Optimistic: insert a temporary row immediately
  const ghostRow = input.closest('.kanban-drill-ghost-row');
  const panel = document.getElementById('kanban-subtask-list');
  let tempRow = null;
  if (panel && ghostRow) {
    tempRow = document.createElement('div');
    tempRow.className = 'kanban-drill-subtask-row';
    tempRow.style.opacity = '0.6';
    tempRow.innerHTML = `<span class="kanban-drill-subtask-grip" style="visibility:hidden;"></span><div class="kanban-drill-subtask-status kanban-status-clickable" style="background:var(--bg-subtle);color:var(--text-dim);">new</div><span class="kanban-drill-subtask-title">${escHtml(title)}</span><span class="kanban-drill-subtask-meta"><span class="spinner" style="width:10px;height:10px;"></span></span>`;
    panel.insertBefore(tempRow, ghostRow);
  }

  // Clear input and re-focus for rapid entry
  input.value = '';
  input.focus();
  _updateSubtaskCount();

  // Create on server in background, then upgrade the optimistic row
  try {
    const res = await fetch('/api/kanban/tasks', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ title, parent_id: parentId, status: 'not_started' }),
    });
    if (!res.ok) throw new Error('Create failed');
    const created = await res.json();
    const newId = created.id || created.task?.id;

    // Upgrade the optimistic row into a real one
    if (tempRow && newId) {
      tempRow.style.opacity = '';
      tempRow.draggable = true;
      tempRow.dataset.taskId = newId;
      tempRow.setAttribute('ondragstart', '_subtaskDragStart(event)');
      tempRow.setAttribute('ondragover', '_subtaskDragOver(event)');
      tempRow.setAttribute('ondrop', "_subtaskDrop(event,'" + parentId + "')");
      tempRow.setAttribute('ondragend', '_subtaskDragEnd(event)');
      const cc = KANBAN_STATUS_COLORS['not_started'] || 'var(--text-muted)';
      const label = KANBAN_STATUS_LABELS['not_started'] || 'Not Started';
      tempRow.innerHTML =
        '<span class="kanban-drill-subtask-grip" title="Drag to reorder">' + (KI.drag || '<svg width="10" height="10" viewBox="0 0 24 24" fill="currentColor"><circle cx="9" cy="5" r="1.5"/><circle cx="15" cy="5" r="1.5"/><circle cx="9" cy="12" r="1.5"/><circle cx="15" cy="12" r="1.5"/><circle cx="9" cy="19" r="1.5"/><circle cx="15" cy="19" r="1.5"/></svg>') + '</span>' +
        '<div class="kanban-drill-subtask-status kanban-status-clickable" style="background:' + cc + '26;color:' + cc + ';" onclick="event.stopPropagation();showStatusMenu(\'' + newId + '\', \'not_started\', event)" title="Click to change status">' + escHtml(label) + '</div>' +
        '<span class="kanban-drill-subtask-title" onclick="navigateToTask(\'' + newId + '\')">' + escHtml(title) + '</span>' +
        '<div class="kanban-drill-subtask-actions">' +
        '<button class="kanban-subtask-action-btn" onclick="event.stopPropagation();_inlineRenameSubtask(\'' + newId + '\', this.closest(\'.kanban-drill-subtask-row\').querySelector(\'.kanban-drill-subtask-title\'))" title="Rename"><svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><path d="M11 4H4a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h14a2 2 0 0 0 2-2v-7"/><path d="M18.5 2.5a2.12 2.12 0 0 1 3 3L12 15l-4 1 1-4 9.5-9.5z"/></svg></button>' +
        '<button class="kanban-subtask-action-btn kanban-subtask-action-danger" onclick="event.stopPropagation();_deleteSubtask(\'' + parentId + '\',\'' + newId + '\')" title="Delete"><svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><polyline points="3 6 5 6 21 6"/><path d="M19 6v14a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2V6m3 0V4a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2"/></svg></button>' +
        '</div>' +
        '<span class="kanban-drill-subtask-chevron" onclick="navigateToTask(\'' + newId + '\')" title="Open">' + KI.chevronR + '</span>';
    }
    _updateSubtaskCount();
  } catch (e) {
    if (typeof showToast === 'function') showToast('Failed: ' + e.message, true);
    if (tempRow) tempRow.remove();
  }
}

function _inlineRenameSubtask(taskId, el) {
  const current = el.textContent;
  el.setAttribute('contenteditable', 'true');
  el.classList.add('editing');
  el.focus();
  const range = document.createRange();
  range.selectNodeContents(el);
  const sel = window.getSelection();
  sel.removeAllRanges();
  sel.addRange(range);

  const save = () => {
    el.removeAttribute('contenteditable');
    el.classList.remove('editing');
    const newTitle = el.textContent.trim();
    if (newTitle && newTitle !== current) {
      updateTask(taskId, { title: newTitle });
    } else {
      el.textContent = current;
    }
  };
  el.onblur = save;
  el.onkeydown = (e) => {
    if (e.key === 'Enter') { e.preventDefault(); el.blur(); }
    if (e.key === 'Escape') { el.textContent = current; el.blur(); }
  };
}

function _deleteSubtask(parentId, taskId) {
  // Get the task title for the modal
  const row = document.querySelector(`.kanban-drill-subtask-row[data-task-id="${taskId}"]`);
  const title = row?.querySelector('.kanban-drill-subtask-title')?.textContent || 'this subtask';

  const overlay = document.getElementById('pm-overlay');
  if (!overlay) return;

  const html = `<div class="pm-card pm-enter" style="max-width:400px;">
    <h2 class="pm-title">Delete Subtask</h2>
    <div class="pm-body">
      <p style="font-size:14px;color:var(--text-primary);margin-bottom:8px;font-weight:500;">${escHtml(title)}</p>
      <p style="font-size:13px;color:var(--text-muted);margin-bottom:4px;">This subtask and all of its children will be permanently deleted.</p>
      <p style="font-size:12px;color:var(--text-dim);">This cannot be undone.</p>
    </div>
    <div class="pm-actions">
      <button class="pm-btn pm-btn-secondary" onclick="_closePm()">Cancel</button>
      <button class="pm-btn pm-btn-danger" onclick="_execDeleteSubtask('${parentId}','${taskId}')">Delete</button>
    </div>
  </div>`;

  overlay.innerHTML = html;
  overlay.classList.add('show');
  requestAnimationFrame(() => overlay.querySelector('.pm-card')?.classList.remove('pm-enter'));
  overlay.onclick = (e) => { if (e.target === overlay) _closePm(); };
}

async function _execDeleteSubtask(parentId, taskId) {
  const btn = document.querySelector('.pm-btn-danger');
  if (btn) { btn.disabled = true; btn.textContent = 'Deleting...'; }
  try {
    await fetch('/api/kanban/tasks/' + taskId, { method: 'DELETE' });
    if (typeof _closePm === 'function') _closePm();
    // Remove row from DOM instead of full re-render
    const row = document.querySelector('.kanban-drill-subtask-row[data-task-id="' + taskId + '"]');
    if (row) row.remove();
    _updateSubtaskCount();
  } catch (e) {
    if (typeof showToast === 'function') showToast('Failed: ' + e.message, true);
    if (typeof _closePm === 'function') _closePm();
  }
}

// Subtask drag-and-drop reordering
let _subtaskDragId = null;
let _subtaskDragEl = null;
function _subtaskDragStart(e) {
  _subtaskDragId = e.currentTarget.dataset.taskId;
  _subtaskDragEl = e.currentTarget;
  e.currentTarget.classList.add('dragging');
  e.dataTransfer.effectAllowed = 'move';
}
function _subtaskDragOver(e) {
  e.preventDefault();
  e.dataTransfer.dropEffect = 'move';
  const row = e.currentTarget;
  if (!_subtaskDragEl || row === _subtaskDragEl || !row.dataset.taskId) return;
  const rect = row.getBoundingClientRect();
  const mid = rect.top + rect.height / 2;
  const panel = row.parentNode;
  if (e.clientY < mid) {
    panel.insertBefore(_subtaskDragEl, row);
  } else {
    panel.insertBefore(_subtaskDragEl, row.nextSibling);
  }
}
function _subtaskDragEnd(e) {
  document.querySelectorAll('.kanban-drill-subtask-row').forEach(r => {
    r.classList.remove('dragging', 'drag-above', 'drag-below');
  });
  // Delay clearing refs so _subtaskDrop can still use them
  setTimeout(() => { _subtaskDragId = null; _subtaskDragEl = null; }, 0);
}
async function _subtaskDrop(e, parentId) {
  e.preventDefault();
  if (!_subtaskDragId) return;

  // Rows already in correct position from dragOver — just read the order and sync
  const rows = [...document.querySelectorAll('#kanban-subtask-list .kanban-drill-subtask-row[data-task-id]')];
  const idx = rows.findIndex(r => r.dataset.taskId === _subtaskDragId);
  const afterId = idx > 0 ? rows[idx - 1].dataset.taskId : null;
  const beforeId = idx < rows.length - 1 ? rows[idx + 1].dataset.taskId : null;

  try {
    await fetch('/api/kanban/tasks/' + _subtaskDragId + '/reorder', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ after_id: afterId, before_id: beforeId }),
    });
  } catch (err) {
    if (typeof showToast === 'function') showToast('Reorder failed', true);
  }
}

function _startTitleEdit(taskId, el) {
  // Already editing — let the browser handle caret placement
  if (el.getAttribute('contenteditable') === 'true') return;

  const current = el.textContent;
  el.setAttribute('contenteditable', 'true');
  el.classList.add('editing');
  el.focus();
  // Select all on first click
  const range = document.createRange();
  range.selectNodeContents(el);
  const sel = window.getSelection();
  sel.removeAllRanges();
  sel.addRange(range);

  const save = () => {
    el.removeAttribute('contenteditable');
    el.classList.remove('editing');
    const newTitle = el.textContent.trim();
    if (newTitle && newTitle !== current) {
      updateTask(taskId, { title: newTitle });
      // Update breadcrumb too
      const crumb = document.querySelector('.kanban-drill-crumb.current');
      if (crumb) crumb.textContent = newTitle;
    } else {
      el.textContent = current;
    }
  };

  el.onblur = save;
  el.onkeydown = (e) => {
    if (e.key === 'Enter') { e.preventDefault(); el.blur(); }
    if (e.key === 'Escape') { el.textContent = current; el.blur(); }
  };
}

/**
 * _editVerificationUrl(taskId, current) — Inline edit for verification URL.
 * Replaces the ver section with an input field.
 */
function _editVerificationUrl(taskId, current) {
  const section = document.getElementById('kanban-drill-ver-section');
  if (!section) return;
  section.innerHTML = `<div class="kanban-drill-ver-edit-row">
    <span class="kanban-drill-ver-icon">${KI.link}</span>
    <input type="url" id="kanban-drill-ver-input" class="kanban-drill-ver-input"
      value="${escHtml(current || '')}" placeholder="https://localhost:8000/page"
      autocomplete="off" spellcheck="false">
    <button class="kanban-drill-ver-save-btn" onclick="_saveVerificationUrl('${taskId}')">Save</button>
    <button class="kanban-drill-ver-cancel-btn" onclick="renderTaskDetail('${taskId}')">Cancel</button>
  </div>`;
  const inp = document.getElementById('kanban-drill-ver-input');
  if (inp) { inp.focus(); inp.select(); }
  inp?.addEventListener('keydown', (e) => {
    if (e.key === 'Enter') { e.preventDefault(); _saveVerificationUrl(taskId); }
    if (e.key === 'Escape') { e.preventDefault(); renderTaskDetail(taskId); }
  });
}

async function _saveVerificationUrl(taskId) {
  const inp = document.getElementById('kanban-drill-ver-input');
  if (!inp) return;
  const url = inp.value.trim();
  // Basic validation — must be absolute URL or empty
  if (url && !url.match(/^https?:\/\//i)) {
    if (typeof showToast === 'function') showToast('Please enter an absolute URL (http:// or https://)', true);
    inp.focus();
    return;
  }
  await updateTask(taskId, { verification_url: url || null });
  if (typeof showToast === 'function') showToast(url ? 'Validation URL saved' : 'Validation URL removed');
  renderTaskDetail(taskId);
}

async function _clearVerificationUrl(taskId) {
  await updateTask(taskId, { verification_url: null });
  if (typeof showToast === 'function') showToast('Validation URL removed');
  renderTaskDetail(taskId);
}

/**
 * createSubtaskInline(parentId) — Quick add subtask from drill-down panel.
 * Plan line 789: "inline input to quickly add a child task" — focus the existing inline input.
 */
async function createSubtaskInline(parentId) {
  const input = document.getElementById('kanban-drill-new-subtask');
  if (input) {
    input.scrollIntoView({ behavior: 'smooth', block: 'center' });
    setTimeout(() => input.focus(), 200);
    return;
  }
  // From chooser — create first subtask, wait for server to confirm, then re-render
  try {
    const res = await fetch('/api/kanban/tasks', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ title: 'New subtask', parent_id: parentId, status: 'not_started' }),
    });
    if (!res.ok) throw new Error('Create failed');
    const created = await res.json();
    const newId = created.id || created.task?.id;
    // Small delay to let Supabase PostgREST cache update
    await new Promise(r => setTimeout(r, 300));
    renderTaskDetail._retries = 0;
    await renderTaskDetail(parentId);
    // Trigger inline rename on the new subtask
    if (newId) {
      setTimeout(() => {
        const row = document.querySelector(`.kanban-drill-subtask-row[data-task-id="${newId}"]`);
        const titleEl = row?.querySelector('.kanban-drill-subtask-title');
        if (titleEl) _inlineRenameSubtask(newId, titleEl);
      }, 100);
    }
  } catch (e) {
    if (typeof showToast === 'function') showToast('Failed: ' + e.message, true);
  }
}

/**
 * addSubtaskFromDrill(parentId) — Add subtask from inline input in drill-down.
 */
async function addSubtaskFromDrill(parentId) {
  const input = document.getElementById('kanban-drill-new-subtask');
  if (!input) return;
  const title = input.value.trim();
  if (!title) return;
  try {
    const res = await fetch('/api/kanban/tasks', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ title, parent_id: parentId, status: 'not_started' }),
    });
    if (!res.ok) {
      const err = await res.json().catch(() => ({}));
      throw new Error(err.error || 'Create failed');
    }
    input.value = '';
    if (typeof showToast === 'function') showToast('Subtask created');
    renderTaskDetail(parentId);
  } catch (e) {
    if (typeof showToast === 'function') showToast('Failed: ' + e.message, true);
  }
}


// ═══════════════════════════════════════════════════════════════
// DRAG & DROP (HTML5 native — plan Section 11)
// ═══════════════════════════════════════════════════════════════

let _kanbanDidDrag = false;

function onKanbanDragStart(event, taskId, sourceStatus) {
  _kanbanDidDrag = true;
  kanbanDragState = { taskId, sourceStatus };
  event.dataTransfer.effectAllowed = 'move';
  event.dataTransfer.setData('text/plain', taskId);
  const card = event.target.closest('.kanban-card');
  if (card) {
    requestAnimationFrame(() => card.classList.add('dragging'));
  }
}

function onKanbanDragEnd(event) {
  kanbanDragState = null;
  // Suppress the click that fires after dragend
  setTimeout(() => { _kanbanDidDrag = false; }, 50);
  document.querySelectorAll('.kanban-card.dragging').forEach(el => el.classList.remove('dragging'));
  document.querySelectorAll('.kanban-column.kanban-drop-target').forEach(el => el.classList.remove('kanban-drop-target'));
  document.querySelectorAll('.kanban-drop-indicator').forEach(el => el.remove());
}

function onKanbanDragOver(event, columnEl) {
  event.preventDefault();
  event.dataTransfer.dropEffect = 'move';
  columnEl.classList.add('kanban-drop-target');

  // Check if column sort mode allows reorder
  const status = columnEl.dataset.status;
  const col = kanbanColumns.find(c => c.status_key === status);
  if (col && col.sort_mode !== 'manual') return; // No reorder indicator for auto-sort columns

  const body = columnEl.querySelector('.kanban-column-body');
  if (!body) return;
  body.querySelectorAll('.kanban-drop-indicator').forEach(el => el.remove());

  const cards = [...body.querySelectorAll('.kanban-card:not(.dragging)')];
  const mouseY = event.clientY;
  let insertBefore = null;
  for (const card of cards) {
    const rect = card.getBoundingClientRect();
    if (mouseY < rect.top + rect.height / 2) { insertBefore = card; break; }
  }

  const indicator = document.createElement('div');
  indicator.className = 'kanban-drop-indicator';
  if (insertBefore) body.insertBefore(indicator, insertBefore);
  else {
    const addBtn = body.querySelector('.kanban-add-card');
    if (addBtn) body.insertBefore(indicator, addBtn);
    else body.appendChild(indicator);
  }
}

function onKanbanDragLeave(event, columnEl) {
  const related = event.relatedTarget;
  if (related && columnEl.contains(related)) return;
  columnEl.classList.remove('kanban-drop-target');
  columnEl.querySelectorAll('.kanban-drop-indicator').forEach(el => el.remove());
}

async function onKanbanDrop(event, targetStatus) {
  event.preventDefault();
  document.querySelectorAll('.kanban-column.kanban-drop-target').forEach(el => el.classList.remove('kanban-drop-target'));
  document.querySelectorAll('.kanban-drop-indicator').forEach(el => el.remove());

  if (!kanbanDragState) return;
  const { taskId, sourceStatus } = kanbanDragState;
  kanbanDragState = null;

  if (sourceStatus !== targetStatus) {
    // Cross-column move — optimistic UI: move card immediately
    const card = document.querySelector(`.kanban-card[data-task-id="${taskId}"]`);
    const targetCol = document.querySelector(`.kanban-column[data-status="${targetStatus}"] .kanban-column-body`);
    if (card && targetCol) {
      card.dataset.status = targetStatus;
      targetCol.appendChild(card);
    }

    // Fire-and-forget to server, refresh board when done
    fetch('/api/kanban/tasks/' + taskId + '/move', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ status: targetStatus, force: true }),
    }).then(res => {
      if (!res.ok) return res.json().then(e => { throw new Error(e.error || 'Move failed'); });
      initKanban(true);
    }).catch(e => {
      if (typeof showToast === 'function') showToast(e.message, true);
      initKanban(true); // revert to server state
    });
  } else {
    // Same column — reorder (manual sort only)
    const col = kanbanColumns.find(c => c.status_key === targetStatus);
    if (col && col.sort_mode !== 'manual') return;

    const targetColumn = document.querySelector(`.kanban-column[data-status="${targetStatus}"]`);
    if (!targetColumn) return;
    const body = targetColumn.querySelector('.kanban-column-body');
    if (!body) return;
    const cards = [...body.querySelectorAll('.kanban-card:not(.dragging)')];
    const mouseY = event.clientY;
    let afterId = null, beforeId = null;
    for (let i = 0; i < cards.length; i++) {
      const rect = cards[i].getBoundingClientRect();
      if (mouseY < rect.top + rect.height / 2) {
        beforeId = cards[i].dataset.taskId;
        if (i > 0) afterId = cards[i - 1].dataset.taskId;
        break;
      }
      afterId = cards[i].dataset.taskId;
    }

    // Optimistic: move card in DOM immediately
    const draggedCard = document.querySelector(`.kanban-card[data-task-id="${taskId}"]`);
    if (draggedCard && body) {
      const beforeCard = beforeId ? document.querySelector(`.kanban-card[data-task-id="${beforeId}"]`) : null;
      if (beforeCard) {
        body.insertBefore(draggedCard, beforeCard);
      } else {
        // Insert at end (before the add button if present)
        const addBtn = body.querySelector('.kanban-add-card');
        if (addBtn) body.insertBefore(draggedCard, addBtn);
        else body.appendChild(draggedCard);
      }
    }

    // Sync to server in background
    try {
      const reorderBody = {};
      if (afterId) reorderBody.after_id = afterId;
      if (beforeId) reorderBody.before_id = beforeId;
      fetch('/api/kanban/tasks/' + taskId + '/reorder', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(reorderBody),
      });
    } catch (e) {
      console.error('[Kanban] Reorder failed:', e);
    }
  }
}


// ═══════════════════════════════════════════════════════════════
// SESSION SPAWNER (plan Section 5)
// ═══════════════════════════════════════════════════════════════

async function openSessionSpawner(taskId) {
  if (typeof addNewAgent !== 'function') {
    if (typeof showToast === 'function') showToast('Session spawner not available', true);
    return;
  }

  // Fetch task context; use stored title for breadcrumbs
  let taskTitle = window._kanbanDetailTaskTitle || '';
  try {
    const ctxRes = await fetch('/api/kanban/tasks/' + taskId + '/context');
    if (ctxRes.ok) {
      const ctxData = await ctxRes.json();
      if (ctxData.context) {
        window._pendingTemplateSystemPrompt = window._pendingTemplateSystemPrompt
          ? ctxData.context + '\n\n' + window._pendingTemplateSystemPrompt
          : ctxData.context;
      }
    }
    // Fetch title only if not already stored
    if (!taskTitle) {
      const taskRes = await fetch('/api/kanban/tasks/' + taskId);
      if (taskRes.ok) { taskTitle = (await taskRes.json()).title || ''; }
    }
  } catch (e) {}

  window._kanbanPendingTaskLink = taskId;
  window._kanbanSessionTaskId = taskId;

  const board = document.getElementById('kanban-board');
  if (!board) return;

  const newId = crypto.randomUUID();

  // Extend the existing kanban titlebar — add Session crumb + Actions/Analyze
  const titlebar = board.querySelector('.kanban-drill-titlebar');
  if (titlebar) {
    // Add Session to the breadcrumb
    const crumbs = titlebar.querySelector('.kanban-drill-breadcrumb');
    if (crumbs) {
      // Make current crumb clickable (it was the task name)
      const current = crumbs.querySelector('.kanban-drill-crumb.current');
      if (current) {
        current.classList.remove('current');
        current.setAttribute('onclick', "_kanbanSessionClose('" + escHtml(taskId) + "')");
      }
      crumbs.innerHTML += '<span class="kanban-drill-sep">' + KI.chevronR + '</span>';
      crumbs.innerHTML += '<span class="kanban-drill-crumb current">Session</span>';
    }
    // Add Actions + Analyze to the right
    let actionsHtml = '<div class="kanban-drill-actions" id="kanban-session-actions">';
    actionsHtml += '<span class="btn-group-label" onclick="openActionsPopup()">Actions</span>';
    actionsHtml += '<div class="btn-group-divider"></div>';
    actionsHtml += '<span class="btn-group-label" onclick="toggleGrpDropdown(\'grp-analyze\')">Analyze &#9662;</span>';
    actionsHtml += '</div>';
    titlebar.insertAdjacentHTML('beforeend', actionsHtml);
  }

  // Replace task detail body with live panel
  const drillBody = board.querySelector('.kanban-drill-body') || board.querySelector('.kanban-drill-split');
  const target = drillBody || board;
  // Clear everything after the titlebar
  const children = [...board.children];
  for (const child of children) {
    if (child !== titlebar) child.remove();
  }

  // Render live panel inside kanban-board
  const sessionDiv = document.createElement('div');
  sessionDiv.className = 'kanban-session-body';
  sessionDiv.innerHTML =
    '<div class="live-panel" id="live-panel">' +
    '<div class="conversation live-log" id="live-log">' +
    '<div class="empty-state" style="padding:60px 0;text-align:center;">' +
    '<svg width="32" height="32" viewBox="0 0 24 24" fill="none" stroke="var(--text-faint)" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round" style="margin-bottom:12px;opacity:0.4;"><polyline points="16 18 22 12 16 6"/><polyline points="8 6 2 12 8 18"/></svg>' +
    '<div style="color:var(--text-faint);font-size:13px;">What will we VibeNode today?</div>' +
    '</div></div>' +
    '<div class="live-input-bar" id="live-input-bar"></div>' +
    '</div>';
  board.appendChild(sessionDiv);

  // Push history
  history.pushState({ view: 'kanban', taskId: taskId, session: true, sessionId: newId }, '', window.location.pathname + '#kanban/task/' + taskId + '/session/' + newId);

  // Set up session state
  allSessions.unshift({
    id: newId, display_title: 'New Session', custom_title: '',
    last_activity: '', size: '', message_count: 0, preview: '',
  });
  if (typeof guiOpenAdd === 'function') guiOpenAdd(newId);
  filterSessions();
  activeId = newId;
  liveSessionId = newId;
  liveLineCount = 0;
  liveAutoScroll = true;
  liveBarState = null;
  if (typeof _renderedUserTexts !== 'undefined') _renderedUserTexts.clear();

  // Input bar
  const bar = document.getElementById('live-input-bar');
  if (bar) {
    bar.innerHTML =
      '<textarea id="live-input-ta" class="live-textarea" rows="3" placeholder="Describe what you want Claude to do\u2026" autofocus' +
      ' onkeydown="if(_shouldSend(event)){event.preventDefault();_newSessionSubmit(\'' + newId + '\')}">' +
      '</textarea>' +
      '<div class="live-bar-row">' +
      '<span class="send-hint" style="font-size:10px;color:var(--text-faint);">' + (typeof _sendHint === 'function' ? _sendHint() : '') + '</span>' +
      '<button class="live-send-btn" id="live-voice-btn"></button>' +
      '</div>';
    if (typeof setupVoiceButton === 'function') {
      setupVoiceButton(document.getElementById('live-input-ta'), document.getElementById('live-voice-btn'), () => _newSessionSubmit(newId));
    }
    setTimeout(() => { const ta = document.getElementById('live-input-ta'); if (ta) ta.focus(); }, 50);
  }

  // Force main-toolbar hidden — setting activeId may have triggered it
  document.getElementById('main-toolbar').style.display = 'none';

  // Link session to task
  setTimeout(async () => {
    const pendingTask = window._kanbanPendingTaskLink;
    if (newId && pendingTask) {
      await onSessionCreatedForTask(pendingTask, newId);
      delete window._kanbanPendingTaskLink;
    }
  }, 500);
}

function _kanbanOpenSession(taskId, sessionId) {
  sessionId = _resolveSessionId(sessionId);
  kanbanDetailTaskId = taskId;
  // Push history so back/forward works
  history.pushState(
    { view: 'kanban', taskId: taskId, session: true, sessionId: sessionId },
    '', window.location.pathname + '#kanban/task/' + taskId + '/session/' + sessionId
  );
  _openSessionInKanban(sessionId);
}

async function _unlinkSession(taskId, sessionId) {
  sessionId = _resolveSessionId(sessionId);
  try {
    await fetch('/api/kanban/tasks/' + taskId + '/sessions/' + sessionId, { method: 'DELETE' });
    // Remove the row from DOM
    const row = document.querySelector('.kanban-drill-session-row[data-session-id="' + sessionId + '"]');
    if (row) row.remove();
  } catch (e) {
    if (typeof showToast === 'function') showToast('Failed: ' + e.message, true);
  }
}

function _kanbanSessionClose(target) {
  if (liveSessionId) { if (typeof _autoSendPendingInput === 'function') _autoSendPendingInput(); if (typeof stopLivePanel === 'function') stopLivePanel(); }
  activeId = null;
  liveSessionId = null;
  window._kanbanSessionTaskId = null;
  localStorage.removeItem('activeSessionId');
  // Remove crumb bar
  const bar = document.getElementById('kanban-session-bar');
  if (bar) bar.remove();
  // Restore panels
  const mb = document.getElementById('main-body');
  if (mb) mb.style.display = 'none';
  const kb = document.getElementById('kanban-board');
  if (kb) kb.style.display = '';
  // Navigate
  if (target === 'board') navigateToBoard();
  else navigateToTask(target);
}

// Open an existing session in kanban — uses #main-body (where startLivePanel writes)
// but adds a kanban crumb bar above it
function _openSessionInKanban(sessionId) {
  sessionId = _resolveSessionId(sessionId);
  const s = allSessions.find(x => x.id === sessionId);
  const sessionName = s ? (s.display_title || 'Session') : 'Session';
  const taskId = kanbanDetailTaskId || null;

  // Snapshot the existing breadcrumb from the drill-down before we hide it
  const kb = document.getElementById('kanban-board');
  const existingBreadcrumb = kb ? kb.querySelector('.kanban-drill-breadcrumb') : null;
  let crumbInnerHtml = '';
  if (existingBreadcrumb) {
    // Make the "current" crumb clickable (it was the task name — navigate back to it)
    const clone = existingBreadcrumb.cloneNode(true);
    const current = clone.querySelector('.kanban-drill-crumb.current');
    if (current && taskId) {
      current.classList.remove('current');
      current.setAttribute('onclick', "_kanbanSessionClose('" + escHtml(taskId) + "')");
    }
    crumbInnerHtml = clone.innerHTML;
  } else {
    // Fallback: build minimal breadcrumb
    crumbInnerHtml = '<span class="kanban-drill-crumb" onclick="_kanbanSessionClose(\'board\')">' + KI.menu + ' Board</span>';
    if (taskId) {
      const taskTitle = window._kanbanDetailTaskTitle || '';
      if (taskTitle) {
        crumbInnerHtml += '<span class="kanban-drill-sep">' + KI.chevronR + '</span>';
        crumbInnerHtml += '<span class="kanban-drill-crumb" onclick="_kanbanSessionClose(\'' + escHtml(taskId) + '\')">' + escHtml(taskTitle) + '</span>';
      }
    }
  }

  // Hide kanban-board, show main-body
  if (kb) kb.style.display = 'none';
  const mb = document.getElementById('main-body');
  if (mb) mb.style.display = '';

  // Remove old crumb bar if exists
  const old = document.getElementById('kanban-session-bar');
  if (old) old.remove();

  // Build crumb bar: existing breadcrumb + session crumb
  let crumbHtml = '<div class="kanban-drill-titlebar" id="kanban-session-bar">';
  crumbHtml += '<div class="kanban-drill-breadcrumb">';
  crumbHtml += crumbInnerHtml;
  crumbHtml += '<span class="kanban-drill-sep">' + KI.chevronR + '</span>';
  crumbHtml += '<span class="kanban-drill-crumb current">' + escHtml(sessionName) + '</span>';
  crumbHtml += '</div>';
  crumbHtml += '<div class="kanban-drill-actions">';
  crumbHtml += '<span class="btn-group-label" onclick="openActionsPopup()">Actions</span>';
  crumbHtml += '<div class="btn-group-divider"></div>';
  crumbHtml += '<span class="btn-group-label" onclick="toggleGrpDropdown(\'grp-analyze\')">Analyze &#9662;</span>';
  crumbHtml += '</div>';
  crumbHtml += '</div>';

  if (mb) mb.insertAdjacentHTML('beforebegin', crumbHtml);

  window._kanbanSessionTaskId = taskId;

  // Now let the normal openInGUI flow handle rendering
  // (we already intercepted at the top — call the rest directly)
  _guiFocusPending = true;
  activeId = sessionId;
  localStorage.setItem('activeSessionId', sessionId);
  if (runningIds.has(sessionId)) guiOpenAdd(sessionId);
  if (liveSessionId && liveSessionId !== sessionId) { _autoSendPendingInput(); stopLivePanel(); }
  filterSessions();

  // Start live panel — writes to #main-body
  if (typeof startLivePanel === 'function') startLivePanel(sessionId);
}

/**
 * onSessionCreatedForTask(taskId, sessionId) — plan line 2394.
 * Called after a session is created and scoped to a task.
 * Links the session to the task and triggers status update.
 */
async function onSessionCreatedForTask(taskId, sessionId) {
  try {
    // Link session to task
    await fetch('/api/kanban/tasks/' + taskId + '/sessions', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ session_id: sessionId }),
    });
    // Do NOT re-render if a live session is active — it would destroy the live panel
    if (liveSessionId) return;
    // Refresh the board / detail view to show updated state
    if (kanbanDetailTaskId === taskId) {
      await new Promise(r => setTimeout(r, 300));
      renderTaskDetail._retries = 0;
      renderTaskDetail(taskId);
    } else {
      initKanban(true);
    }
  } catch (e) {
    console.error('[Kanban] onSessionCreatedForTask failed:', e);
  }
}


// ═══════════════════════════════════════════════════════════════
// VALIDATION CEREMONY (plan Section 4 — validating → complete)
// ═══════════════════════════════════════════════════════════════

async function showValidationCeremony(taskId) {
  let task;
  try {
    const res = await fetch('/api/kanban/tasks/' + taskId);
    if (!res.ok) throw new Error('Failed to load task');
    task = await res.json();
  } catch (e) {
    if (typeof showToast === 'function') showToast('Failed to load task', true);
    return;
  }

  const verUrl = resolveVerificationUrl(task.verification_url);
  const children = task.children || [];
  const overlay = document.getElementById('pm-overlay');
  if (!overlay) return;

  let subtaskHtml = '';
  for (const child of children) {
    const isDone = child.status === 'complete';
    const icon = isDone ? KI.check : KI.square;
    const cc = KANBAN_STATUS_COLORS[child.status] || 'var(--text-muted)';
    subtaskHtml += `<div class="kanban-val-checklist-item${isDone ? ' done' : ''}">
      <span>${icon}</span>
      <span>${escHtml(child.title)}</span>
      <span style="color:${cc};margin-left:auto;font-size:11px;">${escHtml(KANBAN_STATUS_LABELS[child.status] || child.status)}</span>
    </div>`;
  }

  overlay.innerHTML = `
    <div class="pm-card pm-enter kanban-validation-modal">
      <h2 class="pm-title">Validation Ceremony</h2>
      <div class="pm-body"><p>Review <strong>${escHtml(task.title)}</strong> before marking as complete:</p></div>
      ${verUrl ? `<div class="kanban-val-section"><h4>Verification URL</h4><div class="kanban-val-ver-row"><code>${escHtml(verUrl)}</code><a href="${escHtml(verUrl)}" target="_blank" rel="noopener" class="kanban-val-open-btn">Open</a></div></div>` : ''}
      ${children.length > 0 ? `<div class="kanban-val-section"><h4>Subtask Checklist</h4><div class="kanban-val-checklist">${subtaskHtml}</div></div>` : ''}
      <div class="kanban-val-section"><h4>Issues Found (optional)</h4><textarea id="kanban-val-issues" class="kanban-val-textarea" rows="3" placeholder="Describe any issues found..."></textarea></div>
      <div class="pm-actions">
        <button class="pm-btn pm-btn-danger" id="kanban-val-reject">Reject</button>
        <button class="pm-btn pm-btn-secondary" id="kanban-val-cancel">Cancel</button>
        <button class="pm-btn pm-btn-primary" id="kanban-val-approve">Approve</button>
      </div>
    </div>`;

  overlay.classList.add('show');
  requestAnimationFrame(() => overlay.querySelector('.pm-card')?.classList.remove('pm-enter'));

  document.getElementById('kanban-val-approve').onclick = async () => {
    if (typeof _closePm === 'function') _closePm();
    try {
      await fetch('/api/kanban/tasks/' + taskId + '/move', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ status: 'complete', force: true }),
      });
      if (typeof showToast === 'function') showToast('Task approved');
      initKanban(true);
    } catch (e) {
      if (typeof showToast === 'function') showToast('Failed: ' + e.message, true);
    }
  };

  document.getElementById('kanban-val-reject').onclick = async () => {
    const issues = document.getElementById('kanban-val-issues')?.value?.trim();
    if (typeof _closePm === 'function') _closePm();
    if (issues) {
      try {
        await fetch('/api/kanban/tasks/' + taskId + '/issues', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ description: issues }),
        });
      } catch (e) { /* ignore */ }
    }
    try {
      await fetch('/api/kanban/tasks/' + taskId + '/move', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ status: 'remediating', force: true }),
      });
      if (typeof showToast === 'function') showToast('Rejected — moved to Remediating');
      initKanban(true);
    } catch (e) {
      if (typeof showToast === 'function') showToast('Failed: ' + e.message, true);
    }
  };

  document.getElementById('kanban-val-cancel').onclick = () => {
    if (typeof _closePm === 'function') _closePm();
  };
  overlay.onclick = (e) => { if (e.target === overlay && typeof _closePm === 'function') _closePm(); };
}


// ═══════════════════════════════════════════════════════════════
// SORTING & FILTERING (plan Section 16)
// ═══════════════════════════════════════════════════════════════

/**
 * renderSortSelector(statusKey, event) — Dropdown per column header.
 */
function renderSortSelector(statusKey, event) {
  event.stopPropagation();
  document.querySelectorAll('.kanban-col-config-dropdown').forEach(el => el.remove());

  const col = kanbanColumns.find(c => c.status_key === statusKey);
  if (!col) return;

  const btn = event.currentTarget;
  const rect = btn.getBoundingClientRect();
  const dropdown = document.createElement('div');
  dropdown.className = 'kanban-col-config-dropdown';
  dropdown.innerHTML = `
    <div class="kanban-col-config-field">
      <label>Sort by</label>
      <select id="kanban-cfg-sort-mode">
        <option value="manual"${col.sort_mode === 'manual' ? ' selected' : ''}>Manual (drag)</option>
        <option value="last_updated"${(col.sort_mode === 'last_updated' || col.sort_mode === 'date_entered') ? ' selected' : ''}>Last updated</option>
        <option value="date_created"${col.sort_mode === 'date_created' ? ' selected' : ''}>Date created</option>
        <option value="alphabetical"${col.sort_mode === 'alphabetical' ? ' selected' : ''}>Alphabetical</option>
      </select>
    </div>
    <div class="kanban-col-config-field">
      <label>Direction</label>
      <select id="kanban-cfg-sort-dir">
        <option value="asc"${col.sort_direction === 'asc' ? ' selected' : ''}>Ascending</option>
        <option value="desc"${col.sort_direction === 'desc' ? ' selected' : ''}>Descending</option>
      </select>
    </div>
    <button class="kanban-col-config-save" onclick="saveSortConfig('${escHtml(statusKey)}')">Save</button>`;
  document.body.appendChild(dropdown);
  // Clamp to viewport
  const dw = dropdown.offsetWidth;
  const dh = dropdown.offsetHeight;
  let left = rect.left;
  let top = rect.bottom + 4;
  if (left + dw > window.innerWidth - 8) left = window.innerWidth - dw - 8;
  if (left < 8) left = 8;
  if (top + dh > window.innerHeight - 8) top = rect.top - dh - 4;
  dropdown.style.top = top + 'px';
  dropdown.style.left = left + 'px';
  setTimeout(() => {
    document.addEventListener('click', function handler(e) {
      if (!dropdown.contains(e.target) && e.target !== btn) { dropdown.remove(); document.removeEventListener('click', handler); }
    });
  }, 0);
}

async function saveSortConfig(statusKey) {
  const mode = document.getElementById('kanban-cfg-sort-mode').value;
  const dir = document.getElementById('kanban-cfg-sort-dir').value;
  const col = kanbanColumns.find(c => c.status_key === statusKey);
  if (col) { col.sort_mode = mode; col.sort_direction = dir; }
  try {
    await fetch('/api/kanban/columns', {
      method: 'PUT',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(kanbanColumns),
    });
    document.querySelectorAll('.kanban-col-config-dropdown').forEach(el => el.remove());
    initKanban(true);
  } catch (e) {
    if (typeof showToast === 'function') showToast('Failed to save: ' + e.message, true);
  }
}

/**
 * renderTagFilterBar() — Board-level tag multi-select filter.
 * Plan Section 16: filter bar in board header.
 */
function renderTagFilterBar() {
  if (kanbanAllTags.length === 0 && kanbanActiveTagFilter.length === 0) return '';

  let html = '<div class="kanban-tag-filter-bar">';
  html += KI.tag + ' Filter: ';
  for (const tag of kanbanAllTags) {
    const tc = tagColorHash(tag);
    const active = kanbanActiveTagFilter.includes(tag) ? ' kanban-tag-active' : '';
    html += `<span class="kanban-tag-pill kanban-tag-filter-pill${active}" style="background:${tc}22;color:${tc};border-color:${tc}44;" onclick="toggleTagFilter('${escHtml(tag)}')">${escHtml(tag)}</span>`;
  }
  if (kanbanActiveTagFilter.length > 0) {
    html += '<button class="kanban-tag-clear-btn" style="opacity:0.5;" onclick="clearTagFilter()">&times;</button>';
  }
  html += '</div>';
  return html;
}

let _kanbanTagPopupOpen = false;

function toggleKanbanTagPopup() {
  _kanbanTagPopupOpen = !_kanbanTagPopupOpen;
  const popup = document.getElementById('kanban-tag-popup');
  if (!popup) return;
  if (_kanbanTagPopupOpen) {
    popup.classList.add('open');
    const close = (e) => {
      if (!e.target.closest('.kanban-sidebar-tag-wrap')) {
        _kanbanTagPopupOpen = false;
        popup.classList.remove('open');
        document.removeEventListener('click', close);
      }
    };
    setTimeout(() => document.addEventListener('click', close), 0);
  } else {
    popup.classList.remove('open');
  }
}

function _restoreTagPopup() {
  if (!_kanbanTagPopupOpen) return;
  const popup = document.getElementById('kanban-tag-popup');
  if (popup) popup.classList.add('open');
}

function toggleTagFilter(tag) {
  const idx = kanbanActiveTagFilter.indexOf(tag);
  if (idx >= 0) kanbanActiveTagFilter.splice(idx, 1);
  else kanbanActiveTagFilter.push(tag);
  sessionStorage.setItem('kanbanTagFilter', JSON.stringify(kanbanActiveTagFilter));
  _kanbanTagPopupOpen = true;
  initKanban(true);
}

function clearTagFilter() {
  kanbanActiveTagFilter = [];
  sessionStorage.removeItem('kanbanTagFilter');
  _kanbanTagPopupOpen = false;
  initKanban(true);
}

function applyTagFilter(tags) {
  kanbanActiveTagFilter = tags;
  sessionStorage.setItem('kanbanTagFilter', JSON.stringify(kanbanActiveTagFilter));
  initKanban(true);
}

function isDragReorderEnabled(col) {
  return col.sort_mode === 'manual';
}


// ═══════════════════════════════════════════════════════════════
// TAG MANAGEMENT
// ═══════════════════════════════════════════════════════════════

async function addTagFromInput(taskId) {
  const input = document.getElementById('kanban-tag-input');
  if (!input) return;
  const tag = input.value.trim();
  if (!tag) return;
  try {
    const resp = await fetch('/api/kanban/tasks/' + taskId + '/tags', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ tag }),
    });
    if (!resp.ok) {
      const err = await resp.json().catch(() => ({}));
      if (typeof showToast === 'function') showToast(err.error || 'Failed to add tag', true);
      return;
    }
    input.value = '';
    openTaskDetail(taskId); // refresh
  } catch (e) {
    if (typeof showToast === 'function') showToast('Failed to add tag', true);
  }
}

async function removeTag(taskId, tag) {
  try {
    await fetch('/api/kanban/tasks/' + taskId + '/tags/' + encodeURIComponent(tag), { method: 'DELETE' });
    openTaskDetail(taskId); // refresh
  } catch (e) { /* ignore */ }
}

/**
 * onTagInput(query, taskId) — Tag autocomplete typeahead.
 * Plan Section 16, lines 3098-3121: queries /api/kanban/tags/suggest?q=
 */
let _tagSuggestTimer = null;
async function onTagInput(query, taskId) {
  clearTimeout(_tagSuggestTimer);
  const container = document.getElementById('kanban-tag-suggestions');
  if (!container) return;
  if (query.length < 1) { container.innerHTML = ''; return; }
  _tagSuggestTimer = setTimeout(async () => {
    try {
      const resp = await fetch('/api/kanban/tags/suggest?q=' + encodeURIComponent(query));
      const data = await resp.json();
      renderTagSuggestions(data.tags || [], taskId);
    } catch (e) { /* ignore */ }
  }, 150);
}

function renderTagSuggestions(suggestions, taskId) {
  const container = document.getElementById('kanban-tag-suggestions');
  if (!container) return;
  if (!suggestions.length) { container.innerHTML = ''; return; }
  let html = '';
  for (const item of suggestions) {
    // Support both new object format {tag, usage_count} and legacy plain strings
    const tag = typeof item === 'string' ? item : item.tag;
    const usageCount = typeof item === 'object' && item.usage_count != null ? item.usage_count : null;
    const tc = tagColorHash(tag);
    const usageBadge = usageCount != null ? `<span class="kanban-tag-usage-count">(${usageCount})</span>` : '';
    html += `<div class="kanban-tag-suggestion" onclick="selectTagSuggestion('${escHtml(tag.replace(/'/g, "\\'"))}','${taskId}')" style="color:${tc};">${escHtml(tag)} ${usageBadge}</div>`;
  }
  container.innerHTML = html;
}

async function selectTagSuggestion(tag, taskId) {
  const input = document.getElementById('kanban-tag-input');
  if (input) input.value = '';
  const container = document.getElementById('kanban-tag-suggestions');
  if (container) container.innerHTML = '';
  try {
    await fetch('/api/kanban/tasks/' + taskId + '/tags', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ tag }),
    });
    openTaskDetail(taskId);
  } catch (e) { /* ignore */ }
}

async function claimTask(taskId) {
  try {
    await fetch('/api/kanban/tasks/' + taskId + '/claim', { method: 'POST' });
    openTaskDetail(taskId);
  } catch (e) { /* ignore */ }
}

async function unclaimTask(taskId) {
  try {
    await fetch('/api/kanban/tasks/' + taskId + '/unclaim', { method: 'POST' });
    openTaskDetail(taskId);
  } catch (e) { /* ignore */ }
}


// ═══════════════════════════════════════════════════════════════
// COLUMN CONFIG (plan Section 2)
// ═══════════════════════════════════════════════════════════════

const COLUMN_COLOR_PALETTE = [
  '#8b949e', '#58a6ff', '#d29922', '#f85149', '#3fb950',
  '#bc8cff', '#39d2c0', '#e3b341', '#f778ba', '#79c0ff',
  '#a5d6ff', '#ffa657', '#ff7b72', '#7ee787', '#d2a8ff',
];

function openColumnSettings() { openKanbanSettings('columns'); }

// Legacy column settings — kept for reference, replaced by tabbed openKanbanSettings()
async function _openColumnSettingsLegacy_UNUSED() {
  const overlay = document.getElementById('pm-overlay');
  if (!overlay) return;

  // Fetch current columns
  let columns = [...kanbanColumns];

  function render() {
    let html = `
    <div class="pm-card pm-enter" style="max-width:520px;">
      <h2 class="pm-title">Column Settings</h2>
      <div class="pm-body">
        <div style="font-size:12px;color:var(--text-faint);margin-bottom:12px;">Drag to reorder &middot; Click color to change &middot; Click name to rename</div>
        <div class="kanban-colcfg-list" id="kanban-colcfg-list">`;

    for (let i = 0; i < columns.length; i++) {
      const col = columns[i];
      const color = col.color || KANBAN_STATUS_COLORS[col.status_key] || 'var(--text-muted)';
      html += `<div class="kanban-colcfg-row" draggable="true" data-col-idx="${i}"
                    ondragstart="onColCfgDragStart(event, ${i})"
                    ondragover="onColCfgDragOver(event, ${i})"
                    ondrop="onColCfgDrop(event, ${i})"
                    ondragend="onColCfgDragEnd(event)">
        <span class="kanban-colcfg-drag">${KI.drag}</span>
        <div class="kanban-colcfg-color" style="background:${escHtml(color)};" onclick="openColColorPicker(${i}, this)" title="Change color"></div>
        <span class="kanban-colcfg-name" ondblclick="startColRename(${i}, this)">${escHtml(col.name)}</span>
        <span class="kanban-colcfg-key">${escHtml(col.status_key)}</span>
        <div class="kanban-colcfg-actions">
          <button class="kanban-colcfg-btn" onclick="startColRename(${i}, this.closest('.kanban-colcfg-row').querySelector('.kanban-colcfg-name'))" title="Rename">${KI.pencil}</button>
          <button class="kanban-colcfg-btn danger" onclick="removeColumn(${i})" title="Remove">${KI.x}</button>
        </div>
      </div>`;
    }

    html += `</div>
        <div class="kanban-colcfg-add">
          <input type="text" id="kanban-colcfg-new-name" placeholder="New column name..." onkeydown="if(event.key==='Enter')addNewColumn();">
          <button onclick="addNewColumn()">+ Add</button>
        </div>
      </div>
      <div class="pm-actions">
        <button class="pm-btn pm-btn-secondary" onclick="_closePm()">Cancel</button>
        <button class="pm-btn pm-btn-primary" onclick="saveColumnSettings()">Save</button>
      </div>
    </div>`;

    overlay.innerHTML = html;
    overlay.classList.add('show');
    requestAnimationFrame(() => overlay.querySelector('.pm-card')?.classList.remove('pm-enter'));
    overlay.onclick = (e) => { if (e.target === overlay && typeof _closePm === 'function') _closePm(); };
  }

  // Column drag-reorder state
  let _colDragIdx = null;

  window.onColCfgDragStart = function(e, idx) {
    _colDragIdx = idx;
    e.dataTransfer.effectAllowed = 'move';
    e.target.style.opacity = '0.4';
  };
  window.onColCfgDragOver = function(e, idx) {
    e.preventDefault();
    e.dataTransfer.dropEffect = 'move';
  };
  window.onColCfgDrop = function(e, targetIdx) {
    e.preventDefault();
    if (_colDragIdx == null || _colDragIdx === targetIdx) return;
    const [moved] = columns.splice(_colDragIdx, 1);
    columns.splice(targetIdx, 0, moved);
    _colDragIdx = null;
    render();
  };
  window.onColCfgDragEnd = function(e) {
    e.target.style.opacity = '';
    _colDragIdx = null;
  };

  window.startColRename = function(idx, el) {
    const col = columns[idx];
    const input = document.createElement('input');
    input.type = 'text';
    input.value = col.name;
    input.className = 'kanban-colcfg-name-input';
    input.onblur = () => {
      const newName = input.value.trim();
      if (newName && newName !== col.name) col.name = newName;
      render();
    };
    input.onkeydown = (e) => {
      if (e.key === 'Enter') input.blur();
      if (e.key === 'Escape') { input.value = col.name; input.blur(); }
    };
    el.replaceWith(input);
    input.focus();
    input.select();
  };

  window.openColColorPicker = function(idx, el) {
    document.querySelectorAll('.kanban-colcfg-colorpicker').forEach(p => p.remove());
    const picker = document.createElement('div');
    picker.className = 'kanban-colcfg-colorpicker';
    const rect = el.getBoundingClientRect();
    picker.style.top = (rect.bottom + 4) + 'px';
    picker.style.left = rect.left + 'px';
    picker.style.position = 'fixed';
    for (const c of COLUMN_COLOR_PALETTE) {
      const current = columns[idx].color || KANBAN_STATUS_COLORS[columns[idx].status_key] || '';
      const swatch = document.createElement('div');
      swatch.className = 'kanban-colcfg-swatch' + (c === current ? ' selected' : '');
      swatch.style.background = c;
      swatch.onclick = () => {
        columns[idx].color = c;
        picker.remove();
        render();
      };
      picker.appendChild(swatch);
    }
    document.body.appendChild(picker);
    setTimeout(() => {
      document.addEventListener('click', function handler(e) {
        if (!picker.contains(e.target)) { picker.remove(); document.removeEventListener('click', handler); }
      });
    }, 0);
  };

  window.removeColumn = function(idx) {
    if (columns.length <= 1) {
      if (typeof showToast === 'function') showToast('Cannot remove the last column', true);
      return;
    }
    const name = columns[idx].name;
    const row = document.querySelector('.kanban-colcfg-row:nth-child(' + (idx + 1) + ')');
    if (!row) { columns.splice(idx, 1); render(); return; }
    const old = row.innerHTML;
    row.innerHTML = '<div class="kanban-colcfg-confirm">Remove "' + escHtml(name) + '"? <button class="kanban-colcfg-btn danger" id="_col_rm_yes">Remove</button> <button class="kanban-colcfg-btn" id="_col_rm_no">Cancel</button></div>';
    document.getElementById('_col_rm_yes').onclick = () => { columns.splice(idx, 1); render(); };
    document.getElementById('_col_rm_no').onclick = () => { row.innerHTML = old; };
  };

  window.addNewColumn = function() {
    const input = document.getElementById('kanban-colcfg-new-name');
    if (!input) return;
    const name = input.value.trim();
    if (!name) return;
    const key = name.toLowerCase().replace(/[^a-z0-9]+/g, '_').replace(/^_|_$/g, '');
    if (columns.some(c => c.status_key === key)) {
      if (typeof showToast === 'function') showToast('A column with that key already exists', true);
      return;
    }
    columns.push({
      name,
      status_key: key,
      position: columns.length,
      color: COLUMN_COLOR_PALETTE[columns.length % COLUMN_COLOR_PALETTE.length],
    });
    input.value = '';
    render();
  };

  window.saveColumnSettings = async function() {
    try {
      const updates = columns.map((col, i) => ({
        id: col.id,
        name: col.name,
        status_key: col.status_key,
        position: i,
        color: col.color || KANBAN_STATUS_COLORS[col.status_key] || '',
      }));
      const res = await fetch('/api/kanban/columns', {
        method: 'PUT',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ columns: updates }),
      });
      if (!res.ok) {
        const err = await res.json().catch(() => ({}));
        throw new Error(err.error || 'Save failed');
      }
      if (typeof _closePm === 'function') _closePm();
      if (typeof showToast === 'function') showToast('Columns updated');
      initKanban(true);
    } catch (e) {
      if (typeof showToast === 'function') showToast('Failed: ' + e.message, true);
    }
  };

  render();
}


// ═══════════════════════════════════════════════════════════════
// SETTINGS MODAL (plan Section 8c — Database Config UI)
// ═══════════════════════════════════════════════════════════════

async function openKanbanSettings(initialTab) {
  const tab = initialTab || 'columns';

  // Fetch config + columns in parallel
  let config = {};
  let columns = [];
  try {
    const [cfgRes, colRes] = await Promise.all([
      fetch('/api/kanban/config').then(r => r.ok ? r.json() : {}),
      fetch('/api/kanban/columns').then(r => r.ok ? r.json() : []),
    ]);
    config = cfgRes;
    columns = Array.isArray(colRes) ? colRes : (colRes.columns || []);
  } catch (e) { /* use defaults */ }

  const overlay = document.getElementById('pm-overlay');
  if (!overlay) return;

  const isSupa = config.kanban_backend === 'supabase';

  // ── Tab buttons ──
  let html = `<div class="pm-card pm-enter" style="max-width:560px;">
    <h2 class="pm-title">Workflow Settings</h2>
    <div class="kanban-settings-tabs">
      <button class="kanban-settings-tab${tab === 'columns' ? ' active' : ''}" onclick="switchSettingsTab('columns')">Columns</button>
      <button class="kanban-settings-tab${tab === 'preferences' ? ' active' : ''}" onclick="switchSettingsTab('preferences')">Preferences</button>
      <button class="kanban-settings-tab${tab === 'validation' ? ' active' : ''}" onclick="switchSettingsTab('validation')">Validation</button>
    </div>
    <div class="pm-body">`;

  // ── Columns tab ──
  html += `<div id="kb-tab-columns" class="kanban-settings-tab-content" style="${tab !== 'columns' ? 'display:none;' : ''}">`;
  html += '<div style="font-size:12px;color:var(--text-faint);margin-bottom:12px;">Drag to reorder &middot; Click color to change &middot; Double-click name to rename</div>';
  html += '<div class="kanban-colcfg-list" id="kanban-colcfg-list">';
  for (let i = 0; i < columns.length; i++) {
    const col = columns[i];
    const color = col.color || KANBAN_STATUS_COLORS[col.status_key] || 'var(--text-muted)';
    html += `<div class="kanban-colcfg-row" draggable="true" data-col-idx="${i}"
                  ondragstart="onColCfgDragStart(event, ${i})"
                  ondragover="onColCfgDragOver(event, ${i})"
                  ondrop="onColCfgDrop(event, ${i})"
                  ondragend="onColCfgDragEnd(event)">
      <span class="kanban-colcfg-drag">${KI.drag}</span>
      <div class="kanban-colcfg-color" style="background:${escHtml(color)};" onclick="openColColorPicker(${i}, this)" title="Change color"></div>
      <span class="kanban-colcfg-name" ondblclick="startColRename(${i}, this)">${escHtml(col.name)}</span>
      <span class="kanban-colcfg-key">${escHtml(col.status_key)}</span>
      <div class="kanban-colcfg-actions">
        <button class="kanban-colcfg-btn" onclick="startColRename(${i}, this.closest('.kanban-colcfg-row').querySelector('.kanban-colcfg-name'))" title="Rename">${KI.pencil}</button>
        <button class="kanban-colcfg-btn danger" onclick="removeColumn(${i})" title="Remove">${KI.x}</button>
      </div>
    </div>`;
  }
  html += '</div>';
  html += `<div class="kanban-colcfg-add">
    <input type="text" id="kanban-colcfg-new-name" placeholder="New column name..." onkeydown="if(event.key==='Enter')addNewColumn();">
    <button onclick="addNewColumn()">+ Add</button>
  </div>`;
  html += '</div>';

  // ── Preferences tab ──
  const _chk = (key, def_) => { const v = config[key]; return (v === true || v === 'true') ? 'checked' : (v === false || v === 'false') ? '' : (def_ ? 'checked' : ''); };
  html += `<div id="kb-tab-preferences" class="kanban-settings-tab-content" style="${tab !== 'preferences' ? 'display:none;' : ''}">
      <div style="font-size:14px;font-weight:600;margin-bottom:12px;">Automatic Status Changes</div>
      <div class="kanban-settings-row">
        <div><div style="font-size:13px;font-weight:500;">Session starts → Working</div><div style="font-size:12px;color:var(--text-dim);">When a session is linked, move the task to Working</div></div>
        <label class="kanban-toggle"><input type="checkbox" id="kb-auto-start" ${_chk('auto_start_on_session', true)}><span class="kanban-toggle-slider"></span></label>
      </div>
      <div class="kanban-settings-row">
        <div><div style="font-size:13px;font-weight:500;">Child Working → Parent Working</div><div style="font-size:12px;color:var(--text-dim);">When a subtask starts, move its parent to Working too</div></div>
        <label class="kanban-toggle"><input type="checkbox" id="kb-auto-parent-working" ${_chk('auto_parent_working', true)}><span class="kanban-toggle-slider"></span></label>
      </div>
      <div class="kanban-settings-row">
        <div><div style="font-size:13px;font-weight:500;">Child Remediating → Reopen Parent</div><div style="font-size:12px;color:var(--text-dim);">If a subtask needs rework, reopen the parent task</div></div>
        <label class="kanban-toggle"><input type="checkbox" id="kb-auto-parent-reopen" ${_chk('auto_parent_reopen', true)}><span class="kanban-toggle-slider"></span></label>
      </div>
      <div class="kanban-settings-row">
        <div><div style="font-size:13px;font-weight:500;">All done → Validating</div><div style="font-size:12px;color:var(--text-dim);">When all sessions and subtasks finish, move to Validating</div></div>
        <label class="kanban-toggle"><input type="checkbox" id="kb-auto-advance" ${_chk('auto_advance_to_validating', false)}><span class="kanban-toggle-slider"></span></label>
      </div>
      <div style="font-size:14px;font-weight:600;margin:16px 0 12px;">General</div>
      <div class="kanban-settings-row">
        <div><div style="font-size:13px;font-weight:500;">Column page size</div><div style="font-size:12px;color:var(--text-dim);">Max tasks per column before pagination</div></div>
        <input type="number" id="kb-page-size" value="${config.kanban_page_size || 50}" style="width:60px;text-align:center;" class="kanban-settings-input">
      </div>
    </div>`;

  // ── Validation tab ──
  const _valEnabled = config.validation_url_enabled === true || config.validation_url_enabled === 'true';
  const _valBaseUrl = config.validation_base_url || '';
  html += `<div id="kb-tab-validation" class="kanban-settings-tab-content" style="${tab !== 'validation' ? 'display:none;' : ''}">
      <div style="font-size:14px;font-weight:600;margin-bottom:12px;">Validation URLs</div>
      <div style="font-size:12px;color:var(--text-dim);margin-bottom:16px;">When enabled, the AI planner will generate clickable validation URLs on tasks so you can quickly verify features in your browser.</div>
      <div class="kanban-settings-row">
        <div><div style="font-size:13px;font-weight:500;">Enable validation URLs</div><div style="font-size:12px;color:var(--text-dim);">AI planner will include URLs on proposed tasks</div></div>
        <label class="kanban-toggle"><input type="checkbox" id="kb-val-enabled" ${_valEnabled ? 'checked' : ''} onchange="document.getElementById('kb-val-base-row').style.opacity=this.checked?'1':'0.4'"><span class="kanban-toggle-slider"></span></label>
      </div>
      <div id="kb-val-base-row" style="margin-top:12px;opacity:${_valEnabled ? '1' : '0.4'}">
        <div style="font-size:13px;font-weight:500;margin-bottom:6px;">Dev server base URL</div>
        <div style="font-size:12px;color:var(--text-dim);margin-bottom:8px;">The address where your development server runs (e.g. http://localhost:3000)</div>
        <input type="url" id="kb-val-base-url" class="pm-input" placeholder="http://localhost:8000" value="${escHtml(_valBaseUrl)}" style="width:100%;">
      </div>
    </div>`;

  // ── Footer ──
  html += `</div>
    <div class="pm-actions">
      <button class="pm-btn pm-btn-secondary" onclick="_closeKanbanSettings()">Cancel</button>
      <button class="pm-btn pm-btn-primary" onclick="saveAllKanbanSettings(false);_closeKanbanSettings();">Save</button>
    </div>
  </div>`;

  overlay.innerHTML = html;
  overlay.classList.add('show');
  requestAnimationFrame(() => overlay.querySelector('.pm-card')?.classList.remove('pm-enter'));
  overlay.onclick = (e) => { if (e.target === overlay) _closeKanbanSettings(); };

  // Store columns reference for column editing functions
  window._kbSettingsColumns = columns;

  // No autosave — explicit save only via the Save button

  // Wire up column editing functions (same as before but using _kbSettingsColumns)
  const render = () => {
    const listEl = document.getElementById('kanban-colcfg-list');
    if (!listEl) return;
    let listHtml = '';
    for (let i = 0; i < columns.length; i++) {
      const col = columns[i];
      const color = col.color || KANBAN_STATUS_COLORS[col.status_key] || 'var(--text-muted)';
      listHtml += `<div class="kanban-colcfg-row" draggable="true" data-col-idx="${i}"
                        ondragstart="onColCfgDragStart(event, ${i})"
                        ondragover="onColCfgDragOver(event, ${i})"
                        ondrop="onColCfgDrop(event, ${i})"
                        ondragend="onColCfgDragEnd(event)">
        <span class="kanban-colcfg-drag">${KI.drag}</span>
        <div class="kanban-colcfg-color" style="background:${escHtml(color)};" onclick="openColColorPicker(${i}, this)" title="Change color"></div>
        <span class="kanban-colcfg-name" ondblclick="startColRename(${i}, this)">${escHtml(col.name)}</span>
        <span class="kanban-colcfg-key">${escHtml(col.status_key)}</span>
        <div class="kanban-colcfg-actions">
          <button class="kanban-colcfg-btn" onclick="startColRename(${i}, this.closest('.kanban-colcfg-row').querySelector('.kanban-colcfg-name'))" title="Rename">${KI.pencil}</button>
          <button class="kanban-colcfg-btn danger" onclick="removeColumn(${i})" title="Remove">${KI.x}</button>
        </div>
      </div>`;
    }
    listEl.innerHTML = listHtml;
    // Don't auto-save on render — only save on explicit user actions
    // (toggle change, close button). Render fires on drag/drop/rename
    // which haven't been confirmed yet, and if the initial fetch failed
    // this would wipe columns.
  };

  // Column editing globals
  let _colDragIdx = null;

  window.onColCfgDragStart = (e, idx) => { _colDragIdx = idx; e.dataTransfer.effectAllowed = 'move'; e.target.style.opacity = '0.4'; };
  window.onColCfgDragOver = (e) => { e.preventDefault(); e.dataTransfer.dropEffect = 'move'; };
  window.onColCfgDrop = (e, targetIdx) => { e.preventDefault(); if (_colDragIdx == null || _colDragIdx === targetIdx) return; const [moved] = columns.splice(_colDragIdx, 1); columns.splice(targetIdx, 0, moved); _colDragIdx = null; render(); };
  window.onColCfgDragEnd = (e) => { e.target.style.opacity = ''; _colDragIdx = null; };
  window.startColRename = (idx, el) => { const col = columns[idx]; const input = document.createElement('input'); input.type = 'text'; input.value = col.name; input.className = 'kanban-colcfg-name-input'; input.onblur = () => { const n = input.value.trim(); if (n && n !== col.name) col.name = n; render(); }; input.onkeydown = (ev) => { if (ev.key === 'Enter') input.blur(); if (ev.key === 'Escape') { input.value = col.name; input.blur(); } }; el.replaceWith(input); input.focus(); input.select(); };
  window.openColColorPicker = (idx, el) => { document.querySelectorAll('.kanban-colcfg-colorpicker').forEach(p => p.remove()); const picker = document.createElement('div'); picker.className = 'kanban-colcfg-colorpicker'; const rect = el.getBoundingClientRect(); picker.style.cssText = 'position:fixed;top:' + (rect.bottom + 4) + 'px;left:' + rect.left + 'px;'; for (const c of COLUMN_COLOR_PALETTE) { const swatch = document.createElement('div'); swatch.className = 'kanban-colcfg-swatch' + (c === columns[idx].color ? ' selected' : ''); swatch.style.background = c; swatch.onclick = () => { columns[idx].color = c; picker.remove(); render(); }; picker.appendChild(swatch); } document.body.appendChild(picker); setTimeout(() => { document.addEventListener('click', function handler(ev) { if (!picker.contains(ev.target)) { picker.remove(); document.removeEventListener('click', handler); } }); }, 0); };
  window.removeColumn = (idx) => { if (columns.length <= 1) { if (typeof showToast === 'function') showToast('Cannot remove the last column', true); return; } const name = columns[idx].name; const row = document.querySelector('.kanban-colcfg-row:nth-child(' + (idx + 1) + ')'); if (!row) { columns.splice(idx, 1); render(); return; } const old = row.innerHTML; row.innerHTML = '<div class="kanban-colcfg-confirm">Remove "' + escHtml(name) + '"? <button class="kanban-colcfg-btn danger" id="_col_rm_yes">Remove</button> <button class="kanban-colcfg-btn" id="_col_rm_no">Cancel</button></div>'; document.getElementById('_col_rm_yes').onclick = () => { columns.splice(idx, 1); render(); }; document.getElementById('_col_rm_no').onclick = () => { row.innerHTML = old; }; };
  window.addNewColumn = () => { const input = document.getElementById('kanban-colcfg-new-name'); if (!input) return; const name = input.value.trim(); if (!name) return; const key = name.toLowerCase().replace(/[^a-z0-9]+/g, '_').replace(/^_|_$/g, ''); if (columns.some(c => c.status_key === key)) { if (typeof showToast === 'function') showToast('A column with that key already exists', true); return; } columns.push({ id: crypto.randomUUID ? crypto.randomUUID() : Date.now().toString(36) + Math.random().toString(36).slice(2), name, status_key: key, position: columns.length, color: COLUMN_COLOR_PALETTE[columns.length % COLUMN_COLOR_PALETTE.length], sort_mode: 'manual', sort_direction: 'desc' }); input.value = ''; render(); };

  // Tab switcher
  window.switchSettingsTab = (t) => {
    document.querySelectorAll('.kanban-settings-tab').forEach(b => b.classList.toggle('active', b.textContent.toLowerCase().includes(t)));
    document.querySelectorAll('.kanban-settings-tab-content').forEach(c => c.style.display = 'none');
    const el = document.getElementById('kb-tab-' + t);
    if (el) el.style.display = '';
  };

  // Save (quiet=true for autosave, false for explicit close)
  window.saveAllKanbanSettings = async (quiet) => {
    try {
      const updates = columns.map((col, i) => ({
        id: col.id || (crypto.randomUUID ? crypto.randomUUID() : Date.now().toString(36) + Math.random().toString(36).slice(2)),
        name: col.name,
        status_key: col.status_key,
        position: i,
        color: col.color || KANBAN_STATUS_COLORS[col.status_key] || '',
        sort_mode: col.sort_mode || 'manual',
        sort_direction: col.sort_direction || 'desc',
      }));
      await fetch('/api/kanban/columns', {
        method: 'PUT',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(updates),
      });

      const pageSize = document.getElementById('kb-page-size')?.value || '50';
      const supaUrl = document.getElementById('kb-supa-url')?.value?.trim() || '';
      const supaKey = document.getElementById('kb-supa-key')?.value?.trim() || '';

      const cfgBody = {
        auto_start_on_session: document.getElementById('kb-auto-start')?.checked ?? true,
        auto_parent_working: document.getElementById('kb-auto-parent-working')?.checked ?? true,
        auto_parent_reopen: document.getElementById('kb-auto-parent-reopen')?.checked ?? true,
        auto_advance_to_validating: document.getElementById('kb-auto-advance')?.checked ?? false,
        kanban_page_size: parseInt(pageSize, 10),
        validation_url_enabled: document.getElementById('kb-val-enabled')?.checked ?? false,
        validation_base_url: document.getElementById('kb-val-base-url')?.value?.trim() || '',
      };
      if (supaUrl) cfgBody.supabase_url = supaUrl;
      if (supaKey) cfgBody.supabase_secret_key = supaKey;

      await fetch('/api/kanban/config', {
        method: 'PUT',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(cfgBody),
      });

      if (!quiet) await initKanban(true);
    } catch (e) {
      console.error('[Kanban] saveAllKanbanSettings error:', e);
      if (!quiet && typeof showToast === 'function') showToast('Failed: ' + e.message, true);
    }
  };
}

function selectBackend(backend) {
  const sqliteOpt = document.getElementById('kb-opt-sqlite');
  const supaOpt = document.getElementById('kb-opt-supabase');
  const supaConfig = document.getElementById('kb-supabase-config');
  if (sqliteOpt) sqliteOpt.classList.toggle('active', backend === 'sqlite');
  if (supaOpt) supaOpt.classList.toggle('active', backend === 'supabase');
  if (supaConfig) supaConfig.style.display = (backend === 'supabase') ? '' : 'none';
  if (backend === 'supabase' && typeof loadBackupList === 'function') loadBackupList();
}

function _closeKanbanSettings() {
  // Cancel — discard changes, don't save
  if (typeof _closePm === 'function') _closePm();
  initKanban(true);
}

async function testConnection() {
  const url = document.getElementById('kb-supa-url')?.value?.trim();
  const key = document.getElementById('kb-supa-key')?.value?.trim();
  const status = document.getElementById('kb-conn-status');
  const btn = document.getElementById('kb-test-btn');
  const schemaPanel = document.getElementById('kb-schema-setup');
  const switchBtn = document.getElementById('kb-switch-btn');

  if (!url || !key) {
    if (status) { status.style.color = 'var(--orange)'; status.textContent = 'Enter URL and key first'; }
    return;
  }

  // Reset state
  if (schemaPanel) schemaPanel.style.display = 'none';
  if (switchBtn) switchBtn.style.display = 'none';
  if (btn) { btn.disabled = true; btn.textContent = 'Testing...'; }
  if (status) { status.style.color = 'var(--text-muted)'; status.textContent = 'Connecting...'; }

  try {
    const res = await fetch('/api/kanban/migrate/test', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ target: 'supabase', supabase_url: url, supabase_secret_key: key }),
    });
    const data = await res.json();
    if (data.ok) {
      // Schema exists — ready to switch
      if (status) { status.style.color = 'var(--green)'; status.textContent = '\u2713 Connected \u2014 ready to use'; }
      if (switchBtn) switchBtn.style.display = '';
    } else if (data.needs_schema) {
      // Connection works but tables missing — show setup panel
      if (status) { status.style.color = 'var(--orange)'; status.textContent = 'Step 2: Set up the database'; }
      if (schemaPanel) schemaPanel.style.display = '';
    } else {
      if (status) { status.style.color = 'var(--red, #f85149)'; status.textContent = '\u2717 ' + (data.error || 'Connection failed'); }
    }
  } catch (e) {
    if (status) { status.style.color = 'var(--red, #f85149)'; status.textContent = '\u2717 ' + e.message; }
  } finally {
    if (btn) { btn.disabled = false; btn.textContent = 'Test Connection'; }
  }
}

async function setupSupabaseSchema() {
  const url = document.getElementById('kb-supa-url')?.value?.trim();
  const token = document.getElementById('kb-access-token')?.value?.trim();
  const status = document.getElementById('kb-setup-status');
  const btn = document.getElementById('kb-setup-btn');

  if (!token) {
    if (status) { status.style.color = 'var(--orange)'; status.textContent = 'Paste your access token first'; }
    return;
  }

  if (btn) { btn.disabled = true; btn.textContent = 'Setting up...'; }
  if (status) { status.style.color = 'var(--text-muted)'; status.textContent = 'Creating tables...'; }

  try {
    const res = await fetch('/api/kanban/setup-schema', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ supabase_url: url, access_token: token }),
    });
    const data = await res.json();
    if (data.ok) {
      if (status) { status.style.color = 'var(--green)'; status.textContent = '\u2713 Tables created! Switching...'; }
      // Auto-switch to Supabase now that schema is ready
      setTimeout(() => switchToSupabase(), 500);
    } else {
      if (status) { status.style.color = 'var(--red, #f85149)'; status.textContent = '\u2717 ' + (data.error || 'Setup failed'); }
    }
  } catch (e) {
    if (status) { status.style.color = 'var(--red, #f85149)'; status.textContent = '\u2717 ' + e.message; }
  } finally {
    if (btn) { btn.disabled = false; btn.textContent = 'Setup Database'; }
  }
}

// saveKanbanSettings merged into saveAllKanbanSettings in openKanbanSettings()

async function switchToSupabase() {
  const url = document.getElementById('kb-supa-url')?.value?.trim();
  const key = document.getElementById('kb-supa-key')?.value?.trim();
  const status = document.getElementById('kb-conn-status');

  if (!url || !key) {
    if (status) { status.style.color = 'var(--orange)'; status.textContent = 'Enter URL and key first'; }
    return;
  }

  const switchBtn = document.getElementById('kb-switch-btn');

  // If not yet confirmed, show confirmation inline instead of browser alert
  if (!switchBtn?.dataset.confirmed) {
    if (status) {
      status.innerHTML = '<span style="color:var(--orange);">This will copy your local tasks to Supabase and replace any existing cloud data. Local data is kept as backup.</span>';
    }
    if (switchBtn) {
      switchBtn.textContent = 'Confirm Switch';
      switchBtn.dataset.confirmed = '1';
      switchBtn.style.background = 'var(--orange)';
    }
    return;
  }

  // Reset button state
  if (switchBtn) { switchBtn.disabled = true; switchBtn.textContent = 'Migrating...'; switchBtn.style.background = ''; }
  if (status) { status.style.color = 'var(--text-muted)'; status.textContent = 'Copying tasks to Supabase...'; }

  try {
    const res = await fetch('/api/kanban/migrate', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ target: 'supabase', supabase_url: url, supabase_secret_key: key }),
    });
    const data = await res.json();
    if (data.ok) {
      // Update the System dropdown label
      const sysLabel = document.getElementById('sys-storage-label');
      if (sysLabel) sysLabel.textContent = 'Cloud';

      // Show success in the modal before closing
      if (status) { status.style.color = 'var(--green)'; status.textContent = '\u2713 Switched to Supabase! All tasks migrated.'; }
      if (switchBtn) { switchBtn.textContent = '\u2713 Done'; switchBtn.disabled = true; }
      if (typeof showToast === 'function') showToast('Switched to Supabase!');
      // Reload the board after a moment so user sees the success
      setTimeout(async () => {
        if (typeof _closePm === 'function') _closePm();
        await initKanban(true);
      }, 1500);
    } else {
      if (status) { status.style.color = 'var(--red, #f85149)'; status.textContent = '\u2717 ' + (data.error || 'Migration failed'); }
      if (switchBtn) { switchBtn.disabled = false; switchBtn.textContent = 'Step 3: Switch to Supabase'; delete switchBtn.dataset.confirmed; }
    }
  } catch (e) {
    if (status) { status.style.color = 'var(--red, #f85149)'; status.textContent = '\u2717 ' + e.message; }
    if (switchBtn) { switchBtn.disabled = false; switchBtn.textContent = 'Step 3: Switch to Supabase'; delete switchBtn.dataset.confirmed; }
  }
}


async function switchToLocal() {
  const status = document.getElementById('kb-local-status');
  const btn = document.getElementById('kb-local-btn');

  if (btn) { btn.disabled = true; btn.textContent = 'Migrating...'; }
  if (status) { status.style.color = 'var(--text-muted)'; status.textContent = 'Copying tasks to local...'; }

  try {
    const res = await fetch('/api/kanban/migrate', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ target: 'sqlite' }),
    });
    const data = await res.json();
    if (data.ok) {
      const sysLabel = document.getElementById('sys-storage-label');
      if (sysLabel) sysLabel.textContent = 'Local';
      if (status) { status.style.color = 'var(--green)'; status.textContent = '\u2713 Switched to Local! All tasks migrated.'; }
      if (typeof showToast === 'function') showToast('Switched to Local!');
      setTimeout(async () => {
        if (typeof _closePm === 'function') _closePm();
        await initKanban(true);
      }, 1500);
    } else {
      if (status) { status.style.color = 'var(--red, #f85149)'; status.textContent = '\u2717 ' + (data.error || 'Migration failed'); }
      if (btn) { btn.disabled = false; btn.textContent = 'Switch to Local'; }
    }
  } catch (e) {
    if (status) { status.style.color = 'var(--red, #f85149)'; status.textContent = '\u2717 ' + e.message; }
    if (btn) { btn.disabled = false; btn.textContent = 'Switch to Local'; }
  }
}

// ---------------------------------------------------------------------------
// Cloud Backups
// ---------------------------------------------------------------------------

async function downloadCloudBackup() {
  const btn = document.getElementById('kb-backup-dl-btn');
  const status = document.getElementById('kb-backup-dl-status');

  if (btn) { btn.disabled = true; btn.textContent = 'Saving\u2026'; }
  if (status) { status.style.color = 'var(--text-muted)'; status.textContent = ''; }

  try {
    const res = await fetch('/api/kanban/backup/download', { method: 'POST' });
    const data = await res.json();
    if (data.ok) {
      if (status) { status.style.color = 'var(--green)'; status.textContent = `\u2713 Saved ${data.filename} (${data.record_count} records)`; }
      if (typeof showToast === 'function') showToast('Backup saved!');
      loadBackupList();
    } else {
      if (status) { status.style.color = 'var(--red, #f85149)'; status.textContent = '\u2717 ' + (data.error || 'Backup failed'); }
    }
  } catch (e) {
    if (status) { status.style.color = 'var(--red, #f85149)'; status.textContent = '\u2717 ' + e.message; }
  } finally {
    if (btn) { btn.disabled = false; btn.textContent = 'Download Backup'; }
  }
}

async function loadBackupList() {
  const container = document.getElementById('kb-backup-list');
  if (!container) return;

  try {
    const res = await fetch('/api/kanban/backup/list');
    const data = await res.json();
    if (!data.ok || !data.backups || data.backups.length === 0) {
      container.innerHTML = '<span style="color:var(--text-faint);font-size:11px;">No backups yet.</span>';
      return;
    }

    const rows = data.backups.map(b => {
      const date = new Date(b.modified);
      const dateStr = date.toLocaleDateString(undefined, { month: 'short', day: 'numeric', year: 'numeric' });
      const timeStr = date.toLocaleTimeString(undefined, { hour: '2-digit', minute: '2-digit' });
      const sizeKB = (b.size / 1024).toFixed(1);
      return `<div style="display:flex;align-items:center;justify-content:space-between;padding:6px 8px;border-radius:6px;background:var(--bg-secondary);margin-bottom:4px;">
        <div style="flex:1;min-width:0;">
          <div style="font-size:12px;font-weight:500;color:var(--text);white-space:nowrap;overflow:hidden;text-overflow:ellipsis;">${dateStr} ${timeStr}</div>
          <div style="font-size:10px;color:var(--text-faint);">${b.record_count} records &middot; ${sizeKB} KB</div>
        </div>
        <div style="display:flex;gap:4px;flex-shrink:0;margin-left:8px;">
          <button class="kanban-settings-btn-accent" onclick="restoreCloudBackup('${b.filename}')" style="padding:4px 10px;font-size:11px;">Restore</button>
          <button style="padding:4px 8px;font-size:11px;background:none;border:1px solid var(--border);border-radius:4px;color:var(--text-muted);cursor:pointer;" onclick="deleteCloudBackup('${b.filename}')">\u2717</button>
        </div>
      </div>`;
    }).join('');

    container.innerHTML = rows;
  } catch (e) {
    container.innerHTML = `<span style="color:var(--red,#f85149);font-size:11px;">${e.message}</span>`;
  }
}

async function restoreCloudBackup(filename) {
  const container = document.getElementById('kb-backup-list');

  // Inline confirmation — find the row's restore button and swap it
  const btns = container?.querySelectorAll('button.kanban-settings-btn-accent');
  let targetBtn = null;
  btns?.forEach(b => { if (b.getAttribute('onclick')?.includes(filename)) targetBtn = b; });

  if (targetBtn && !targetBtn.dataset.confirmed) {
    targetBtn.dataset.confirmed = '1';
    targetBtn.textContent = 'Confirm?';
    targetBtn.style.background = 'var(--orange)';
    return;
  }

  if (targetBtn) { targetBtn.disabled = true; targetBtn.textContent = 'Restoring\u2026'; targetBtn.style.background = ''; }

  try {
    const res = await fetch('/api/kanban/backup/restore', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ filename }),
    });
    const data = await res.json();
    if (data.ok) {
      if (typeof showToast === 'function') showToast(`Restored ${data.record_count} records!`);
      // Reload the board
      setTimeout(async () => {
        if (typeof _closePm === 'function') _closePm();
        await initKanban(true);
      }, 800);
    } else {
      if (typeof showToast === 'function') showToast('Restore failed: ' + (data.error || 'Unknown error'), true);
      if (targetBtn) { targetBtn.disabled = false; targetBtn.textContent = 'Restore'; delete targetBtn.dataset.confirmed; }
    }
  } catch (e) {
    if (typeof showToast === 'function') showToast('Restore failed: ' + e.message, true);
    if (targetBtn) { targetBtn.disabled = false; targetBtn.textContent = 'Restore'; delete targetBtn.dataset.confirmed; }
  }
}

async function deleteCloudBackup(filename) {
  // Find and confirm
  const container = document.getElementById('kb-backup-list');
  const delBtns = container?.querySelectorAll('button:not(.kanban-settings-btn-accent)');
  let targetBtn = null;
  delBtns?.forEach(b => { if (b.getAttribute('onclick')?.includes(filename)) targetBtn = b; });

  if (targetBtn && !targetBtn.dataset.confirmed) {
    targetBtn.dataset.confirmed = '1';
    targetBtn.textContent = '!!';
    targetBtn.style.color = 'var(--red, #f85149)';
    targetBtn.style.borderColor = 'var(--red, #f85149)';
    return;
  }

  try {
    const res = await fetch('/api/kanban/backup/delete', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ filename }),
    });
    const data = await res.json();
    if (data.ok) {
      if (typeof showToast === 'function') showToast('Backup deleted');
      loadBackupList();
    } else {
      if (typeof showToast === 'function') showToast('Delete failed: ' + (data.error || ''), true);
    }
  } catch (e) {
    if (typeof showToast === 'function') showToast('Delete failed: ' + e.message, true);
  }
}

// Reports rendering is in kanban-report.js per plan Section 14 file structure


// ═══════════════════════════════════════════════════════════════
// SIDEBAR CONTROLS
// ═══════════════════════════════════════════════════════════════

function renderKanbanSidebar() {
  const sidebar = document.getElementById('kanban-sidebar');
  if (!sidebar) return;

  let html = '';

  // ── Actions ──
  html += '<div class="kanban-sidebar-section">';
  html += '<div class="kanban-sidebar-label">Kanban</div>';
  html += '<button class="kanban-sidebar-btn" onclick="createTask(\'not_started\')"><svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><line x1="12" y1="5" x2="12" y2="19"/><line x1="5" y1="12" x2="19" y2="12"/></svg> New Task</button>';
  html += '<button class="kanban-sidebar-btn" onclick="openReportsPanel()">' + KI.chart + ' Report</button>';
  html += '<button class="kanban-sidebar-btn" onclick="openKanbanSettings(\'columns\')">' + KI.gear + ' Settings</button>';

  // ── Tags (popup) ──
  if (kanbanAllTags.length > 0 || kanbanActiveTagFilter.length > 0) {
    const hasActive = kanbanActiveTagFilter.length > 0;
    html += '<div class="kanban-sidebar-tag-wrap">';
    html += '<button class="kanban-sidebar-btn' + (hasActive ? ' kanban-sidebar-btn-tag-active' : '') + '" onclick="event.stopPropagation();toggleKanbanTagPopup()">';
    html += KI.tag + ' Tags';
    if (hasActive) html += ' <span class="kanban-sidebar-tag-badge">' + kanbanActiveTagFilter.length + '</span>';
    html += '</button>';
    html += '<div class="kanban-sidebar-tag-popup" id="kanban-tag-popup">';
    for (const tag of kanbanAllTags) {
      const tc = tagColorHash(tag);
      const active = kanbanActiveTagFilter.includes(tag) ? ' kanban-tag-active' : '';
      html += `<span class="kanban-tag-pill kanban-tag-filter-pill${active}" style="background:${tc}22;color:${tc};border-color:${tc}44;" onclick="event.stopPropagation();toggleTagFilter('${escHtml(tag)}')">${escHtml(tag)}</span>`;
    }
    if (hasActive) {
      html += '<span class="kanban-tag-pill kanban-tag-filter-pill" style="background:none;color:var(--text-faint);border-color:var(--border);" onclick="event.stopPropagation();clearTagFilter()">clear</span>';
    }
    html += '</div>';
    html += '</div>';
  }

  html += '</div>';

  sidebar.innerHTML = html;

  _restoreTagPopup();

  // ── Permission aggregator (reuse from workspace.js) ──
  const permPanel = document.getElementById('sidebar-perm-panel');
  if (permPanel && typeof _buildPermissionPanel === 'function') {
    permPanel.innerHTML = _buildPermissionPanel();
    permPanel.style.display = '';
  }
}


// ═══════════════════════════════════════════════════════════════
// KEYBOARD SHORTCUTS (plan Section 15 Phase 4)
// ═══════════════════════════════════════════════════════════════

let _kanbanShortcutsAttached = false;

function attachKanbanShortcuts() {
  if (_kanbanShortcutsAttached) return;
  _kanbanShortcutsAttached = true;

  document.addEventListener('keydown', (e) => {
    if (typeof viewMode !== 'undefined' && viewMode !== 'kanban') return;
    const tag = (e.target.tagName || '').toLowerCase();
    if (tag === 'input' || tag === 'textarea' || tag === 'select' || e.target.isContentEditable) return;

    // Never intercept browser refresh (Ctrl+R, Ctrl+Shift+R, F5)
    if (e.ctrlKey || e.metaKey || e.key === 'F5') return;

    switch (e.key) {
      case 'n': e.preventDefault(); createTask('not_started'); break;
      case 'r': e.preventDefault(); initKanban(true); if (typeof showToast === 'function') showToast('Refreshed'); break;
      case 'Escape': closeTaskDetail(); break;
      case '?': e.preventDefault(); showShortcutHelp(); break;
    }
  });
}

function showShortcutHelp() {
  const existing = document.querySelector('.kanban-shortcut-overlay');
  if (existing) { existing.remove(); return; }
  const overlay = document.createElement('div');
  overlay.className = 'kanban-shortcut-overlay';
  overlay.innerHTML = `<div class="kanban-shortcut-card"><h3>Keyboard Shortcuts</h3>
    <div class="kanban-shortcut-grid">
      <kbd>n</kbd><span>New task</span>
      <kbd>r</kbd><span>Refresh board</span>
      <kbd>Esc</kbd><span>Close panel</span>
      <kbd>?</kbd><span>Toggle this help</span>
    </div>
    <button class="kanban-shortcut-close" onclick="this.closest('.kanban-shortcut-overlay').remove()">Close</button>
  </div>`;
  overlay.onclick = (e) => { if (e.target === overlay) overlay.remove(); };
  document.body.appendChild(overlay);
}


// ═══════════════════════════════════════════════════════════════
// WEBSOCKET HANDLERS (plan Section 12)
// ═══════════════════════════════════════════════════════════════

function onKanbanTaskCreated(data) {
  if (typeof viewMode !== 'undefined' && viewMode !== 'kanban') return;
  if (kanbanDetailTaskId) return;
  initKanban();
}

function onKanbanTaskUpdated(data) {
  if (typeof viewMode !== 'undefined' && viewMode !== 'kanban') return;
  if (kanbanDetailTaskId) return;
  initKanban();
}

function onKanbanTaskMoved(data) {
  if (typeof viewMode !== 'undefined' && viewMode !== 'kanban') return;
  if (kanbanDetailTaskId) return;
  initKanban();
}

// No onKanbanTaskDeleted — tasks are NEVER deleted (plan line 2384)

// Plan line 2281: kanban_task_moved — Task ID + old/new column + sort position
// (duplicate removed)

function _kanbanOnTaskCreated(data) { onKanbanTaskCreated(data); }
function _kanbanOnTaskUpdated(data) { onKanbanTaskUpdated(data); }
function _kanbanOnTaskMoved(data) { onKanbanTaskMoved(data); }
function _kanbanOnBoardRefresh(data) { if (kanbanDetailTaskId) return; initKanban(); }


// ═══════════════════════════════════════════════════════════════
// BROWSER HISTORY (hash-based routing — plan Section 11)
// ═══════════════════════════════════════════════════════════════

// Plan Section 2: Browser History & Navigation Integration
// URL scheme: #kanban (board), #kanban/task/{id} (drill-down)

function navigateToTask(taskId) {
  if (_kanbanDidDrag) return;
  if (liveSessionId && window._kanbanSessionTaskId) return;
  const board = document.getElementById('kanban-board');
  if (board) board.innerHTML = _taskDetailSkeleton();
  const state = { view: 'kanban', taskId };
  // Clean URL — strip query params, use only the hash
  history.pushState(state, '', window.location.pathname + '#kanban/task/' + taskId);
  renderTaskDetail(taskId);
}

function navigateToBoard() {
  if (liveSessionId && window._kanbanSessionTaskId) return;
  const board = document.getElementById('kanban-board');
  if (board) board.innerHTML = _kanbanSkeleton();
  const state = { view: 'kanban', taskId: null };
  history.pushState(state, '', window.location.pathname + '#kanban');
  kanbanDetailTaskId = null;
  _kanbanFetching = false; // Reset in case stuck
  _kanbanHasLoaded = false; // Force immediate fetch, no debounce
  initKanban(true);
}

function restoreFromHash() {
  const hash = window.location.hash;
  if (!hash || !hash.startsWith('#kanban')) return;

  // Ensure kanban mode first
  if (typeof viewMode !== 'undefined' && viewMode !== 'kanban' && typeof setViewMode === 'function') {
    setViewMode('kanban');
    return;
  }

  // Session URL: #kanban/task/{taskId}/session/{sessionId}
  const sessMatch = hash.match(/^#kanban\/task\/([^/]+)\/session\/(.+)$/);
  if (sessMatch) {
    const taskId = sessMatch[1];
    let sessionId = _resolveSessionId(sessMatch[2]);
    kanbanDetailTaskId = taskId;

    // After a page refresh _idRemaps is empty — ask the server to resolve
    // the session ID in case it was remapped before the refresh.
    const _doOpen = (sid) => renderTaskDetail(taskId).then(() => _openSessionInKanban(sid));
    if (sessionId === sessMatch[2] && !allSessions.find(x => x.id === sessionId)) {
      fetch('/api/resolve-session/' + sessionId).then(r => r.json()).then(d => {
        if (d.remapped) {
          sessionId = d.id;
          // Fix the URL hash to the canonical ID
          history.replaceState(null, '', window.location.pathname + '#kanban/task/' + taskId + '/session/' + sessionId);
        }
        _doOpen(sessionId);
      }).catch(() => _doOpen(sessionId));
    } else {
      _doOpen(sessionId);
    }
    return;
  }

  // Task URL: #kanban/task/{taskId}
  const taskMatch = hash.match(/^#kanban\/task\/([^/]+)$/);
  if (taskMatch) {
    renderTaskDetail(taskMatch[1]);
  } else if (hash === '#kanban') {
    // Plan line 1048: setViewMode before rendering board
    // Only switch mode if not already in kanban — board is already rendered by initKanban()
    if (typeof viewMode !== 'undefined' && viewMode !== 'kanban' && typeof setViewMode === 'function') {
      setViewMode('kanban');
    }
    // Don't call initKanban() here — restoreFromHash is called from initKanban, would cause infinite recursion
  }
}

// Handle browser back/forward buttons
window.addEventListener('popstate', (e) => {
  if (typeof viewMode !== 'undefined' && viewMode !== 'kanban') return;

  // If a live session is active in kanban, close it first then navigate
  if (liveSessionId && window._kanbanSessionTaskId) {
    if (typeof _autoSendPendingInput === 'function') _autoSendPendingInput();
    if (typeof stopLivePanel === 'function') stopLivePanel();
    liveSessionId = null;
    activeId = null;
    window._kanbanSessionTaskId = null;
    // Remove session crumb bar
    const sessionBar = document.getElementById('kanban-session-bar');
    if (sessionBar) sessionBar.remove();
    // Restore kanban board visibility
    const kb = document.getElementById('kanban-board');
    if (kb) kb.style.display = '';
    const mb = document.getElementById('main-body');
    if (mb) mb.style.display = 'none';
    // Clear the kanban session body if it exists
    const ksb = document.querySelector('.kanban-session-body');
    if (ksb) ksb.remove();
  }

  const board = document.getElementById('kanban-board');

  const goToTask = (taskId) => {
    if (board) board.innerHTML = _taskDetailSkeleton();
    renderTaskDetail(taskId);
  };
  const goToBoard = () => {
    kanbanDetailTaskId = null;
    if (board) board.innerHTML = _kanbanSkeleton();
    initKanban(true);
  };

  if (e.state?.view === 'kanban') {
    if (e.state.session && e.state.taskId && e.state.sessionId) {
      // Forward into a session — wait for drill-down to render first
      kanbanDetailTaskId = e.state.taskId;
      const _sid = _resolveSessionId(e.state.sessionId);
      renderTaskDetail(e.state.taskId).then(() => _openSessionInKanban(_sid));
    } else if (e.state.taskId) {
      goToTask(e.state.taskId);
    } else {
      goToBoard();
    }
  } else {
    const hash = window.location.hash;
    // Check for session URL: #kanban/task/{taskId}/session/{sessionId}
    const sessMatch = hash.match(/^#kanban\/task\/([^/]+)\/session\/(.+)$/);
    if (sessMatch) {
      kanbanDetailTaskId = sessMatch[1];
      const _sid = _resolveSessionId(sessMatch[2]);
      renderTaskDetail(sessMatch[1]).then(() => _openSessionInKanban(_sid));
    } else if (hash && hash.startsWith('#kanban/task/')) {
      const tid = hash.replace('#kanban/task/', '').replace('/session', '');
      if (tid) goToTask(tid);
    } else {
      goToBoard();
    }
  }
});


// ═══════════════════════════════════════════════════════════════
// UTILITIES
// ═══════════════════════════════════════════════════════════════

function tagColorHash(tag) {
  let hash = 0;
  for (let i = 0; i < tag.length; i++) hash = tag.charCodeAt(i) + ((hash << 5) - hash);
  const colors = ['#58a6ff', '#3fb950', '#d29922', '#f85149', '#bc8cff', '#39d2c0', '#e3b341', '#f778ba'];
  return colors[Math.abs(hash) % colors.length];
}
