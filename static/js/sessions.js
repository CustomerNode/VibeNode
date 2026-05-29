/* sessions.js — sorting, list rendering, tooltips, column resize, click handling */

// _shortDate() extracted to time-utils.js per plan Section 14 line 2894

// ===========================================================================
// Sidebar multi-selection (Ctrl/Cmd+click)
// ===========================================================================
//
// Independent of `activeId` and `allSessionIds`.
// - `activeId`         = single session displayed in the main panel.
// - `allSessionIds`    = O(1) lookup for streaming-event filtering
//                        (PERF-CRITICAL #15 in CLAUDE.md). DO NOT conflate.
// - `multiSelectedIds` = transient working set for batch actions; cleared
//                        aggressively (see lifecycle table in
//                        docs/plans/sidebar-multi-select-spec.md, Section 4).
//
// Mutation discipline: every change MUST call `_syncMultiSelectionDom()` and
// `_renderMultiSelectionBadge()` so DOM and badge stay in sync without a
// full sidebar re-render.  Selection is NOT persisted across page reloads
// (resolved decision 3 in the spec).
let multiSelectedIds = new Set();

// Re-entrancy guard for bulk actions.  Multiple bulk runs concurrently would
// race overlays, double-fire confirmations, and produce confusing toasts.
let _bulkInFlight = false;

/**
 * Add or remove a session ID from the multi-select set, then sync DOM + badge.
 * Idempotent: clicking the same row toggles its membership.
 * @param {string} sessionId
 */
function _toggleMultiSelect(sessionId) {
  if (!sessionId) return;
  if (multiSelectedIds.has(sessionId)) {
    multiSelectedIds.delete(sessionId);
  } else {
    multiSelectedIds.add(sessionId);
  }
  _syncMultiSelectionDom();
  _renderMultiSelectionBadge();
}

/**
 * Empty the multi-select set and sync DOM + badge.
 * Safe to call when the set is already empty (no-op if so).
 */
function _clearMultiSelect() {
  if (multiSelectedIds.size === 0) {
    // Still ensure badge is gone in case of stale DOM
    _renderMultiSelectionBadge();
    return;
  }
  multiSelectedIds.clear();
  _syncMultiSelectionDom();
  _renderMultiSelectionBadge();
}

/**
 * For each rendered .session-item row, toggle the .multi-selected class
 * to reflect set membership.  O(N) over visible rows; cheap for the
 * sidebar.  Avoids full re-render (which would lose scroll position,
 * tooltip state, and naming-badge in-flight DOM).
 */
function _syncMultiSelectionDom() {
  // Both list-mode (.session-item) and grid-mode (.wf-card) rows participate
  // in multi-select.  Both carry data-sid and react to .multi-selected.
  const rows = document.querySelectorAll('.session-item[data-sid], .wf-card[data-sid]');
  rows.forEach(row => {
    const sid = row.getAttribute('data-sid');
    if (multiSelectedIds.has(sid)) {
      row.classList.add('multi-selected');
    } else {
      row.classList.remove('multi-selected');
    }
  });
}

/**
 * Render or remove the count badge above the column header.  The badge
 * lives in a fixed container at the top of #session-list (created on
 * demand) so we don't have to re-render the list to show/hide it.
 *
 * Uses aria-live="polite" so screen readers announce count changes
 * without interrupting the user.
 */
function _renderMultiSelectionBadge() {
  const list = document.getElementById('session-list');
  if (!list) return;
  let badge = document.getElementById('sidebar-multi-badge');
  const count = multiSelectedIds.size;

  if (count === 0) {
    if (badge) badge.remove();
    return;
  }

  if (!badge) {
    badge = document.createElement('div');
    badge.id = 'sidebar-multi-badge';
    badge.className = 'sidebar-multi-badge';
    badge.setAttribute('aria-live', 'polite');
    // Insert as first child of session-list (above col-header-row).
    list.insertBefore(badge, list.firstChild);
  }
  badge.innerHTML =
    '<span class="sidebar-multi-badge-text">' + count + ' selected</span>'
    + '<button type="button" class="sidebar-multi-badge-clear" '
    + 'onclick="_clearMultiSelect()" title="Clear selection (Esc)">'
    + '<svg width="11" height="11" viewBox="0 0 24 24" fill="none" '
    + 'stroke="currentColor" stroke-width="2.5" stroke-linecap="round">'
    + '<line x1="18" y1="6" x2="6" y2="18"/>'
    + '<line x1="6" y1="6" x2="18" y2="18"/></svg></button>';
}

/**
 * Drop IDs from the multi-select set that no longer exist in
 * `allSessionIds`.  Call this from any code that reassigns or filters
 * `allSessions` in bulk (loadSessions, deleteSession, deleteEmptySessions,
 * project switch teardown).  Without this, stale IDs accumulate and the
 * count badge lies.
 */
function _pruneMultiSelectionToExisting() {
  if (multiSelectedIds.size === 0) return;
  let changed = false;
  for (const id of Array.from(multiSelectedIds)) {
    if (!allSessionIds.has(id)) {
      multiSelectedIds.delete(id);
      changed = true;
    }
  }
  if (changed) {
    _syncMultiSelectionDom();
    _renderMultiSelectionBadge();
  }
}

// One-shot flag set by _sessionRowMouseDown when a Ctrl/Cmd+click has just
// been handled.  The cell's subsequent click event still fires (preventDefault
// on mousedown does NOT cancel click), so singleOrDouble and handleNameClick
// check this flag and bail to avoid opening the session.  The flag is cleared
// by every consumer and after a short timeout as a safety net in case the
// click event never reaches a cell handler (e.g. user dragged off the row).
let _msSuppressNextClick = false;

function _consumeMsSuppression() {
  if (_msSuppressNextClick) {
    _msSuppressNextClick = false;
    return true;
  }
  return false;
}

/**
 * Mousedown handler attached to every .session-item row.  Intercepts
 * Ctrl/Cmd+left-click to toggle multi-selection without opening the
 * session.  Plain clicks fall through to the cell's existing onclick
 * (singleOrDouble or handleNameClick).
 *
 * Mousedown fires BEFORE the cell onclick.  preventDefault() suppresses
 * text-selection side effects, but the click event still fires — so we
 * also raise the `_msSuppressNextClick` flag for the cell handlers to
 * consume.
 */
function _sessionRowMouseDown(e, sessionId) {
  // Only left button; right-click is routed via oncontextmenu.
  if (e.button !== 0) return;
  // Note on view-mode scoping: previously we blocked Ctrl+click outside the
  // 'sessions' view, but the sidebar (and its session rows) are also visible
  // in 'workplace', 'kanban', and 'compose' views.  Blocking the toggle there
  // made the feature appear broken to users who Ctrl+click while a session
  // is open in the main panel.  The toggle itself is harmless in any view —
  // it's only the BULK MENU that needs the sessions-view guard (enforced in
  // sessionContextMenu below) so the menu doesn't surface in compose where
  // it would target sessions across composition boundaries confusingly.
  if (e.ctrlKey || e.metaKey) {
    e.preventDefault();
    e.stopPropagation();
    _toggleMultiSelect(sessionId);
    // Raise the suppression flag so the upcoming click on the inner
    // cell does NOT open the session.  Auto-clear after one tick as a
    // safety net in case the click never arrives.
    _msSuppressNextClick = true;
    setTimeout(function() { _msSuppressNextClick = false; }, 200);
  }
  // Plain click: do nothing here — let the cell's onclick fire as today.
  // (Plain-click selection clear happens inside singleOrDouble/handleNameClick.)
}

/**
 * Bounded-concurrency runner for bulk actions.  Iterates `ids`, invoking
 * `asyncFn(id)` with at most `concurrency` in flight at once.  Continues
 * past per-item failures and collects them for the summary toast.
 *
 * @param {string[]} ids
 * @param {(id:string) => Promise<void>} asyncFn
 * @param {number} concurrency
 * @returns {Promise<{ok:string[], fail:Array<{id:string, err:string}>}>}
 */
async function _runBulk(ids, asyncFn, concurrency) {
  const results = { ok: [], fail: [] };
  if (!ids || !ids.length) return results;
  let i = 0;
  async function worker() {
    while (i < ids.length) {
      const id = ids[i++];
      try {
        await asyncFn(id);
        results.ok.push(id);
      } catch (e) {
        results.fail.push({ id: id, err: String(e && e.message ? e.message : e) });
      }
    }
  }
  const n = Math.min(Math.max(1, concurrency || 1), ids.length);
  await Promise.all(Array.from({ length: n }, worker));
  return results;
}

// Escape key clears the multi-selection (matches Finder/Explorer/VS Code).
// Attached once at script load; safe to bind multiple times because of
// the `_msEscBound` flag.
if (typeof window !== 'undefined' && !window._msEscBound) {
  window._msEscBound = true;
  document.addEventListener('keydown', function(e) {
    if (e.key !== 'Escape') return;
    if (multiSelectedIds.size === 0) return;
    // Don't steal Esc from open modals (pm-overlay, project-overlay, etc.).
    const pmOverlay = document.getElementById('pm-overlay');
    if (pmOverlay && pmOverlay.classList.contains('show')) return;
    const projOverlay = document.getElementById('project-overlay');
    if (projOverlay && projOverlay.classList.contains('show')) return;
    // Don't steal Esc from a focused editable field — user may be canceling
    // an inline rename or clearing a search.
    const ae = document.activeElement;
    if (ae && (ae.tagName === 'INPUT' || ae.tagName === 'TEXTAREA' || ae.isContentEditable)) return;
    _clearMultiSelect();
  });
}

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
    // Date sort uses effective_ts = max(last_message_ts, file_mtime) so
    // sessions you've interacted with (rename, autoname, fork) bubble up
    // even when no new conversation was added. Falls back to legacy fields
    // for clients/payloads that pre-date the effective_ts field.
    const key = s => s.effective_ts || s.last_activity_ts || s.sort_ts || 0;
    copy.sort((a, b) => dir * (key(a) - key(b)));
  }
  return copy;
}

// ───────────────────────────────────────────────────────────────────────
// Subsessions (spec §4.4 / §4.6.1) — sidebar tree helpers
// ───────────────────────────────────────────────────────────────────────
// Disclosure caret state for parents.  Persisted per-parent in
// localStorage so a user's collapse choices survive reload.
function _subsessionCaretKey(parentSid) {
  return 'vn.subsession.caret.' + parentSid;
}
function _isSubsessionExpanded(parentSid) {
  // Default: expanded.  Only collapsed when user explicitly toggled.
  const v = localStorage.getItem(_subsessionCaretKey(parentSid));
  return v === null ? true : v === '1';
}
function _toggleSubsessionCaret(parentSid) {
  const next = !_isSubsessionExpanded(parentSid);
  localStorage.setItem(_subsessionCaretKey(parentSid), next ? '1' : '0');
  // Re-render the sidebar to apply the new collapse state.
  if (typeof filterSessions === 'function') filterSessions();
}

// Build a parent_sid -> [child rows] index from the flat session list.
// Sessions whose parent_session_id is NOT a sibling in the current list
// (e.g. parent was deleted or is in another project) get treated as
// top-level so they never disappear from the UI.
function _buildSubsessionIndex(sessions) {
  const idsInList = new Set(sessions.map(s => s.id));
  const childrenByParent = new Map();
  for (const s of sessions) {
    const p = s.parent_session_id;
    if (p && idsInList.has(p)) {
      if (!childrenByParent.has(p)) childrenByParent.set(p, []);
      childrenByParent.get(p).push(s);
    }
  }
  return childrenByParent;
}

// Return a (sub)session's pre-defined name lookup helper.
function _findSessionById(sessions, sid) {
  for (const s of sessions) if (s.id === sid) return s;
  return null;
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
  // Sleeping/awaiting-wake-up = substatus 'auto-resuming' regardless of state.
  // Spans both phases:
  //  * idle + auto-resuming = asleep waiting for wake-up to fire
  //  * working + auto-resuming = wake-up firing right now
  // Both phases share the same moon icon so the sidebar/kanban/workforce
  // visually agree with the live panel's "Awaiting wake-up…" label
  // throughout the cycle.  Without this, the sidebar flipped to a
  // pickaxe icon the moment the wake-up fired (state went idle→working)
  // while the live panel still said "Awaiting wake-up…" — that's the
  // state-desync the user reported.
  const _isSleeping = window._sessionSubstatus && window._sessionSubstatus[s.id] === 'auto-resuming';
  const icon = isWaiting
    ? '<svg class="state-icon" width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="#ff9500" stroke-width="2" stroke-linecap="round" title="Waiting for input"><circle cx="12" cy="12" r="10"/><polyline points="12 6 12 12 16 14"/></svg>'
    : _isCompacting
    ? '<svg class="state-icon compacting-icon" width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="#aa88ff" stroke-width="2" stroke-linecap="round" title="Compacting context"><polyline points="4 14 10 14 10 20"/><polyline points="20 10 14 10 14 4"/><line x1="14" y1="10" x2="21" y2="3"/><line x1="3" y1="21" x2="10" y2="14"/></svg>'
    : _isSleeping
    ? '<svg class="state-icon" width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="#8aa9ff" stroke-width="2" stroke-linecap="round" title="Awaiting wake-up"><path d="M21 12.79A9 9 0 1 1 11.21 3 7 7 0 0 0 21 12.79z"/></svg>'
    : isRunning
    ? '<img class="state-icon" src="/static/svg/pickaxe.svg" width="12" height="12" style="filter:brightness(0) saturate(100%) invert(55%) sepia(78%) saturate(1000%) hue-rotate(215deg);" title="Working">'
    : isIdle
    ? '<svg class="state-icon" width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="#44aa66" stroke-width="2" stroke-linecap="round" title="Idle"><polyline points="20 6 9 17 4 12"/></svg>'
    : '';
  // multi-select: include .multi-selected class on initial render so the
  // visual stays in sync after sort/filter re-renders without needing an
  // extra _syncMultiSelectionDom() pass.  onmousedown intercepts Ctrl/Cmd+
  // click to toggle the selection without opening the session.
  const msClass = multiSelectedIds.has(s.id) ? ' multi-selected' : '';

  // \u2500\u2500 Subsessions (spec \u00a74.4 + \u00a74.6.1) \u2500\u2500
  // Subsession child row: indent 16 px, prefix with the \u21b3 glyph, and
  // add a "from: <parent_name>" secondary line.  Pure CSS class \u2014 the
  // existing row markup keeps working unchanged.
  const isSubsession = !!s.parent_session_id;
  const subsessionClass = isSubsession ? ' subsession-row' : '';
  const subsessionGlyph = isSubsession
    ? '<span class="subsession-glyph" aria-hidden="true">\u21b3 </span>'
    : '';
  // "from: <parent name>" secondary line.  Look up the parent's display
  // title from allSessions; fall back to the SID short form when the
  // parent isn't in the current list (e.g. closed-but-not-managed).
  let subsessionFromLine = '';
  if (isSubsession && typeof allSessions !== 'undefined') {
    const parent = allSessions.find(x => x.id === s.parent_session_id);
    const parentName = parent
      ? (parent.custom_title || parent.display_title || s.parent_session_id.slice(0, 8))
      : s.parent_session_id.slice(0, 8);
    subsessionFromLine = '<div class="subsession-from-line" title="Parent session">from: '
      + escHtml(parentName) + '</div>';
  }

  // Inbox badge (\ud83d\udcec N) on parents whose inbox_dirty flag is set.  Tap
  // does NOT pull updates \u2014 it just toasts "N reports waiting" so the
  // user understands what the count means.  Click handler stops
  // propagation so it doesn't open the session.
  const inboxCount = (typeof window.__subsessionInboxCounts === 'object'
    && window.__subsessionInboxCounts) ? window.__subsessionInboxCounts[s.id] : 0;
  const inboxBadge = s.inbox_dirty
    ? `<span class="subsession-inbox-badge"
            aria-label="${escHtml(String(inboxCount || ''))} subsession reports pending"
            onclick="event.stopPropagation(); _showSubsessionBadgeToast('${s.id}');"
            title="Subsession reports waiting \u2014 included on your next message">\ud83d\udcec${inboxCount ? ' ' + inboxCount : ''}</span>`
    : '';

  // Disclosure caret for parents that have at least one child in the
  // current list.  Caret state persists per parent in localStorage.
  const hasChildren = !!(window.__subsessionChildrenIndex
    && window.__subsessionChildrenIndex.has(s.id));
  const isExpanded = hasChildren ? _isSubsessionExpanded(s.id) : true;
  const caretSpan = hasChildren
    ? `<span class="subsession-caret" tabindex="0"
            onclick="event.stopPropagation(); _toggleSubsessionCaret('${s.id}');"
            onkeydown="if(event.key==='Enter'||event.key===' '){event.preventDefault();_toggleSubsessionCaret('${s.id}');}"
            aria-label="Toggle subsessions"
            title="${isExpanded ? 'Collapse' : 'Expand'} subsessions">${isExpanded ? '\u25be' : '\u25b8'}</span>`
    : '';

  return `
  <div class="session-item${activeClass}${stateClass}${msClass}${subsessionClass}${extraClass || ''}" data-sid="${s.id}" onmousedown="_sessionRowMouseDown(event,'${s.id}')" oncontextmenu="sessionContextMenu(event,'${s.id}')">
    <div class="session-col-name" onclick="handleNameClick('${s.id}')" style="cursor:text;" title="Click to rename">
      ${caretSpan}${subsessionGlyph}${icon}${escHtml(s.display_title)}${_autoNamingInFlight.has(s.id) ? '<span class="naming-badge"><span class="naming-dot"></span>Naming\u2026</span>' : ''}${inboxBadge}
      ${subsessionFromLine}
    </div>
    <div class="session-col-date" ${colClick} title="${escHtml(s.last_activity)}" data-short-date="${escHtml(s.last_activity)}">${escHtml(_shortDate(s.last_activity))}</div>
    <div class="session-col-size" ${colClick}>${escHtml(s.size)}</div>
  </div>`;
}

// Toast shown when the user taps the inbox badge.  Does NOT trigger a
// pull \u2014 that's an explicit affordance in the live panel (Phase 6) to
// avoid accidental "send empty message" surprises.
function _showSubsessionBadgeToast(parentSid) {
  if (typeof showToast === 'function') {
    showToast('Subsession reports waiting. Open the parent session to see and Pull updates.');
  }
  // Open the parent session so the user can see its children.
  if (typeof openInGUI === 'function' && parentSid !== activeId) {
    openInGUI(parentSid);
  }
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
      // Re-render the multi-select badge after the list re-render destroys
      // it (innerHTML = ...) so the count stays visible across sort/filter
      // updates.  Pruning also handles cases where a selected session was
      // filtered out by search and is no longer in `sessions`.
      _renderMultiSelectionBadge();
      return;
    }
  }
  // --- End NB-10 ---

  // ── Subsessions tree layout (spec §4.4 + §4.6.1) ──
  // Build a parent_sid -> [child rows] index from the flat session list.
  // We render top-level sessions in the user's chosen sort order, then
  // immediately after each parent we render its children indented and
  // sorted by their own ts.  Children are also "claimed" so they don't
  // appear again at the top level.
  const childrenIdx = _buildSubsessionIndex(sessions);
  window.__subsessionChildrenIndex = childrenIdx;  // for _renderSessionRow
  const claimedChildren = new Set();
  for (const kids of childrenIdx.values()) {
    for (const k of kids) claimedChildren.add(k.id);
  }

  let rows = '';
  for (const s of sessions) {
    if (claimedChildren.has(s.id)) continue;  // rendered below its parent
    rows += _renderSessionRow(s, '');
    const kids = childrenIdx.get(s.id);
    if (kids && _isSubsessionExpanded(s.id)) {
      // Children sorted by their own effective_ts (newest first by default).
      const kidsSorted = sortedSessions(kids);
      for (const kid of kidsSorted) {
        rows += _renderSessionRow(kid, '');
      }
    }
  }

  el.innerHTML = header + rows;
  initColResize();
  attachTooltipListeners();
  // See note above — keep the badge visible across re-renders.
  _renderMultiSelectionBadge();
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
    sleeping:'<svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" style="vertical-align:middle;"><path d="M21 12.79A9 9 0 1 1 11.21 3 7 7 0 0 0 21 12.79z"/></svg> Awaiting wake-up'
  };
  const _isCompactingTip = status === 'working' && window._sessionSubstatus && window._sessionSubstatus[id] === 'compacting';
  // Sleeping tooltip: substatus 'auto-resuming' regardless of state, so the
  // tooltip stays "Awaiting wake-up" across idle→working→idle within the
  // wake-up cycle.  Without this, the tooltip flickered to "Working" the
  // moment the wake-up fired.
  const _isSleepingTip = window._sessionSubstatus && window._sessionSubstatus[id] === 'auto-resuming';
  const stateLabel = _isCompactingTip
    ? '<svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="#aa88ff" stroke-width="2" stroke-linecap="round" style="vertical-align:middle;"><polyline points="4 14 10 14 10 20"/><polyline points="20 10 14 10 14 4"/><line x1="14" y1="10" x2="21" y2="3"/><line x1="3" y1="21" x2="10" y2="14"/></svg> Compacting'
    : _isSleepingTip
    ? stateLabels.sleeping
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
  // If the prior mousedown was a Ctrl/Cmd+click that already toggled
  // multi-select, swallow this click instead of opening the session.
  // (preventDefault on mousedown does not cancel the subsequent click.)
  if (_consumeMsSuppression()) return;
  // Plain click on a row clears any active multi-selection.  Matches
  // Finder/Explorer/VS Code semantics: clicking elsewhere collapses the
  // working set down to whatever you just clicked.
  if (typeof multiSelectedIds !== 'undefined' && multiSelectedIds.size > 0) {
    _clearMultiSelect();
  }
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

  // ----- Multi-selection routing (Finder/Explorer/VS Code semantics) -----
  // Right-click on a row that's part of a 2+ selection -> show bulk menu.
  // Right-click on a row NOT in the selection -> clear the selection and
  // show the single-row menu (the user expects "act on just this row").
  // Right-click with selection size <= 1 falls through to the single-row
  // path so the bulk menu only appears for multi-target operations.
  //
  // NOTE: We previously gated this on viewMode === 'sessions', but the
  // sidebar (and these rows) are rendered in 'workplace', 'kanban', and
  // 'compose' views too — gating made the bulk menu silently disappear
  // and the single-row menu appear instead, which looked exactly like
  // "only one row is selected" to the user.  The bulk actions themselves
  // are sensible regardless of which main view is active.  Compose view
  // is the one nuance: there sessions are grouped by composition, but the
  // bulk actions (stop/delete/etc) are still well-defined per session ID.
  if (multiSelectedIds.size >= 2 && multiSelectedIds.has(sessionId)) {
    return _bulkContextMenu(e);
  }
  if (multiSelectedIds.size > 0 && !multiSelectedIds.has(sessionId)) {
    _clearMultiSelect();
  }

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
  items += '<div class="ws-ctx-item" onclick="_sessCtx(\'add-compose\',\'' + sessionId + '\')"><svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><path d="M12 20h9"/><path d="M16.5 3.5a2.121 2.121 0 0 1 3 3L7 19l-4 1 1-4L16.5 3.5z"/></svg> Add to Structured composition</div>';
  // Spawn Subsession (spec §4.2 + §4.6) — downward-branching arrow.
  items += '<div class="ws-ctx-item" onclick="_sessCtx(\'spawn-subsession\',\'' + sessionId + '\')"><svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><path d="M6 3v6"/><path d="M6 9c0 4 4 6 8 6"/><polyline points="11 12 14 15 11 18"/></svg> Spawn Subsession</div>';
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
    case 'spawn-subsession':
      _spawnSubsession(sessionId);
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
// Spawn Subsession (spec §4.2 + §4.6) — peel off a side investigation
// ═══════════════════════════════════════════════════════════════
//
// POSTs to /api/sessions/<parent_sid>/spawn-subsession, then opens
// the resulting child in the live panel.  Reloads the session list so
// the new child appears indented under its parent.  Fires a soft
// highlight pulse on both rows for 600 ms (CSS handles the animation).
async function _spawnSubsession(parentSid) {
  if (!parentSid) return;
  const proj = localStorage.getItem('activeProject') || '';
  const url = '/api/sessions/' + encodeURIComponent(parentSid)
    + '/spawn-subsession'
    + (proj ? '?project=' + encodeURIComponent(proj) : '');
  let resp;
  try {
    resp = await fetch(url, {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({}),
    });
  } catch (e) {
    if (typeof showToast === 'function') {
      showToast('Spawn failed: network error', true);
    }
    return;
  }
  let data = null;
  try { data = await resp.json(); } catch (e) {}
  if (!resp.ok || !data || data.ok !== true) {
    const errMsg = (data && data.error) || ('HTTP ' + resp.status);
    if (typeof showToast === 'function') {
      showToast('Spawn failed: ' + errMsg, true);
    }
    return;
  }

  // One-time intro tooltip (spec §4.6.1).
  if (!localStorage.getItem('vn.tip.subsession_intro')) {
    localStorage.setItem('vn.tip.subsession_intro', '1');
    if (typeof showToast === 'function') {
      showToast(
        'This is a subsession. It knows everything its parent knew when it was spawned. '
        + 'Reports flow back to the parent on your next message there.'
      );
    }
  } else if (typeof showToast === 'function') {
    showToast('Subsession spawned under "' + (data.title || parentSid.slice(0, 8)) + '"');
  }

  // Reload the session list so the child appears under its parent.
  if (typeof loadSessions === 'function') {
    try { await loadSessions(); } catch (e) {}
  }

  // Open the new subsession in the live panel.
  if (typeof openInGUI === 'function' && data.new_id) {
    try { await openInGUI(data.new_id); } catch (e) {}
  }

  // Spawn-pulse animation — CSS .subsession-spawn-pulse fades a soft
  // yellow background over 600 ms.  Defer one tick so the new row
  // exists in the DOM before we attach the class.
  setTimeout(function() {
    const childRow = document.querySelector('.session-item[data-sid="' + (data.new_id || '') + '"]');
    if (childRow) childRow.classList.add('subsession-spawn-pulse');
    const parentRow = document.querySelector('.session-item[data-sid="' + parentSid + '"]');
    if (parentRow) parentRow.classList.add('subsession-spawn-pulse');
    setTimeout(function() {
      if (childRow) childRow.classList.remove('subsession-spawn-pulse');
      if (parentRow) parentRow.classList.remove('subsession-spawn-pulse');
    }, 700);
  }, 50);
}

// Ctrl+Shift+S keyboard shortcut — spawn a subsession from whatever
// session is currently open in the live panel.
document.addEventListener('keydown', function(e) {
  if (e.ctrlKey && e.shiftKey && (e.key === 'S' || e.key === 's')) {
    // Skip if user is typing in an input/textarea.
    const tag = (e.target && e.target.tagName) || '';
    if (tag === 'INPUT' || tag === 'TEXTAREA') return;
    if (e.target && e.target.isContentEditable) return;
    if (typeof activeId !== 'undefined' && activeId) {
      e.preventDefault();
      _spawnSubsession(activeId);
    }
  }
});

// ═══════════════════════════════════════════════════════════════
// Phase 6.5 P1-5 — Rewind-orphan UI prompt
// ═══════════════════════════════════════════════════════════════
//
// When the rewind endpoint returns a non-empty `rewind_orphans` array,
// surface a modal listing each orphaned child with [Re-anchor] and
// [Detach] buttons (spec §6.3).
//
// Re-anchor → POST /api/sessions/<child>/reanchor (server resolves the
//   new origin_turn from the current parent tip)
// Detach    → POST /api/sessions/<child>/detach (clears parent pointer,
//   stamps parent_deleted_at like §6.2 orphan)
//
// Exposed on window so toolbar.js (which holds the rewind response
// handler) can call us without taking a hard dependency on this file.
window._handleRewindOrphans = function(parentSid, orphanSids) {
  if (!Array.isArray(orphanSids) || !orphanSids.length) return;
  const proj = localStorage.getItem('activeProject') || '';
  const projQ = proj ? '?project=' + encodeURIComponent(proj) : '';

  function nameFor(sid) {
    // Best-effort lookup in the existing session list; falls back to
    // the SID prefix so the modal is never empty-labelled.
    if (typeof allSessions !== 'undefined' && Array.isArray(allSessions)) {
      const hit = allSessions.find(function(s) { return s && s.id === sid; });
      if (hit && hit.title) return hit.title;
    }
    return (sid || '').slice(0, 8);
  }

  let modal = document.getElementById('rewind-orphans-modal');
  if (modal) modal.remove();
  modal = document.createElement('div');
  modal.id = 'rewind-orphans-modal';
  modal.className = 'subsession-report-modal';
  let rows = '';
  orphanSids.forEach(function(sid) {
    const safe = String(sid).replace(/[^A-Za-z0-9_-]/g, '');
    rows +=
      '<div class="rewind-orphan-row" data-sid="' + safe + '">'
      + '<div class="rewind-orphan-name">' + escHtml(nameFor(sid)) + '</div>'
      + '<button type="button" class="rewind-orphan-reanchor">Re-anchor at current parent tip</button>'
      + '<button type="button" class="rewind-orphan-detach">Detach</button>'
      + '</div>';
  });
  modal.innerHTML =
    '<div class="subsession-report-card">'
    + '<div class="subsession-report-title">Subsessions orphaned by rewind</div>'
    + '<div class="subsession-report-help">'
    + 'These subsessions were spawned past the line you rewound to.  '
    + 'Pick an action for each.'
    + '</div>'
    + '<div class="rewind-orphans-list">' + rows + '</div>'
    + '<div class="subsession-report-actions">'
    + '<button type="button" class="rewind-orphan-close">Close</button>'
    + '</div>'
    + '</div>';
  document.body.appendChild(modal);

  modal.querySelector('.rewind-orphan-close').addEventListener('click', function() {
    modal.remove();
  });

  Array.prototype.forEach.call(
    modal.querySelectorAll('.rewind-orphan-row'),
    function(row) {
      const childSid = row.getAttribute('data-sid');
      row.querySelector('.rewind-orphan-reanchor').addEventListener('click', async function() {
        try {
          const url = '/api/sessions/' + encodeURIComponent(childSid) + '/reanchor' + projQ;
          const resp = await fetch(url, {
            method: 'POST',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({}),
          });
          const data = await resp.json().catch(function() { return null; });
          if (!resp.ok || !data || data.ok !== true) {
            const err = (data && data.error) || ('HTTP ' + resp.status);
            if (typeof showToast === 'function') showToast('Re-anchor failed: ' + err, true);
            return;
          }
          if (typeof showToast === 'function') {
            showToast('Re-anchored at line ' + (data.subsession_origin_turn || '?'));
          }
          row.remove();
          if (!modal.querySelectorAll('.rewind-orphan-row').length) modal.remove();
        } catch (e) {
          if (typeof showToast === 'function') showToast('Re-anchor failed: network error', true);
        }
      });
      row.querySelector('.rewind-orphan-detach').addEventListener('click', async function() {
        try {
          const url = '/api/sessions/' + encodeURIComponent(childSid) + '/detach' + projQ;
          const resp = await fetch(url, {
            method: 'POST',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({}),
          });
          const data = await resp.json().catch(function() { return null; });
          if (!resp.ok || !data || data.ok !== true) {
            const err = (data && data.error) || ('HTTP ' + resp.status);
            if (typeof showToast === 'function') showToast('Detach failed: ' + err, true);
            return;
          }
          if (typeof showToast === 'function') showToast('Subsession detached.');
          row.remove();
          if (!modal.querySelectorAll('.rewind-orphan-row').length) modal.remove();
        } catch (e) {
          if (typeof showToast === 'function') showToast('Detach failed: network error', true);
        }
      });
    }
  );
};

// ═══════════════════════════════════════════════════════════════
// Add to Structured composition — unified tree picker
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
  html += '<h2 class="pm-title">Add to Structured composition</h2>';
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
  html += '<h2 class="pm-title">Add to Structured composition</h2>';
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
  html += '<h2 class="pm-title">Add to Structured composition</h2>';
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
  html += '<h2 class="pm-title">Add to Structured composition</h2>';
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
      showToast('Added to Structured composition: ' + name);
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

// ===========================================================================
// Bulk context menu (multi-selection right-click)
// ===========================================================================
//
// Built and styled to match the single-row context menu (`.ws-ctx-menu`,
// `.ws-ctx-item`) so users carry the same muscle memory.  The bulk menu
// only appears when right-clicking a row that's in a selection of size
// >= 2 — single-target right-clicks always get the existing single-row
// menu instead.
//
// Action coverage and reasoning are documented in the spec
// (docs/plans/sidebar-multi-select-spec.md, Section 7).

/**
 * Render and show the bulk context menu at the click position.
 * @param {MouseEvent} e
 */
function _bulkContextMenu(e) {
  // Build a snapshot of IDs at menu-open time.  The selection set may
  // mutate before the user picks an item (e.g. project switch teardown)
  // and we want every action to operate on what was on screen when the
  // menu opened, not what's in the set when the user clicks.
  const ids = Array.from(multiSelectedIds);
  const count = ids.length;
  if (count < 2) return; // safety: routing should have prevented this

  const menu = document.createElement('div');
  menu.className = 'session-ctx-menu ws-ctx-menu sidebar-bulk-ctx-menu';

  // Header strip showing the count — reinforces "this acts on N items".
  let html = '<div class="ws-ctx-bulk-header">'
    + '<svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round">'
    + '<rect x="3" y="3" width="7" height="7" rx="1"/><rect x="14" y="3" width="7" height="7" rx="1"/>'
    + '<rect x="3" y="14" width="7" height="7" rx="1"/><rect x="14" y="14" width="7" height="7" rx="1"/></svg>'
    + ' ' + count + ' selected</div>';
  html += '<div class="ws-ctx-divider"></div>';

  // --- Stop all (silent skip for non-running) ---
  html += '<div class="ws-ctx-item" onclick="_bulkStop()">'
    + '<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round">'
    + '<rect x="3" y="3" width="18" height="18" rx="2" ry="2"/></svg>'
    + ' Stop all</div>';

  // --- Auto-name all (with confirmation modal) ---
  html += '<div class="ws-ctx-item" onclick="_bulkAutoName()">'
    + '<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round">'
    + '<path d="M12 20h9"/><path d="M16.5 3.5a2.121 2.121 0 0 1 3 3L7 19l-4 1 1-4L16.5 3.5z"/></svg>'
    + ' Auto-name all</div>';

  // --- Duplicate all ---
  html += '<div class="ws-ctx-item" onclick="_bulkDuplicate()">'
    + '<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round">'
    + '<rect x="9" y="9" width="13" height="13" rx="2" ry="2"/>'
    + '<path d="M5 15H4a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2h9a2 2 0 0 1 2 2v1"/></svg>'
    + ' Duplicate all</div>';

  // --- Add all to Compose (sequential modal per session) ---
  html += '<div class="ws-ctx-item" onclick="_bulkAddToCompose()">'
    + '<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round">'
    + '<path d="M12 20h9"/><path d="M16.5 3.5a2.121 2.121 0 0 1 3 3L7 19l-4 1 1-4L16.5 3.5z"/></svg>'
    + ' Add all to Structured composition</div>';

  html += '<div class="ws-ctx-divider"></div>';

  // --- Delete all (danger; modal confirmation) ---
  html += '<div class="ws-ctx-item danger" onclick="_bulkDelete()">'
    + '<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round">'
    + '<polyline points="3 6 5 6 21 6"/>'
    + '<path d="M19 6v14a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2V6m3 0V4a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2"/></svg>'
    + ' Delete all</div>';

  html += '<div class="ws-ctx-divider"></div>';

  // --- Clear selection ---
  html += '<div class="ws-ctx-item" onclick="_clearMultiSelect();var m=document.querySelector(\'.session-ctx-menu\');if(m)m.remove();">'
    + '<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round">'
    + '<line x1="18" y1="6" x2="6" y2="18"/><line x1="6" y1="6" x2="18" y2="18"/></svg>'
    + ' Clear selection</div>';

  menu.innerHTML = html;
  menu.style.left = e.clientX + 'px';
  menu.style.top = e.clientY + 'px';
  document.body.appendChild(menu);

  // Keep menu in viewport
  requestAnimationFrame(function() {
    const rect = menu.getBoundingClientRect();
    if (rect.right > window.innerWidth) {
      menu.style.left = (window.innerWidth - rect.width - 8) + 'px';
    }
    if (rect.bottom > window.innerHeight) {
      menu.style.top = (window.innerHeight - rect.height - 8) + 'px';
    }
  });

  // Close on click outside
  const closer = function(ev) {
    if (!menu.contains(ev.target)) {
      menu.remove();
      document.removeEventListener('click', closer);
    }
  };
  setTimeout(function() { document.addEventListener('click', closer); }, 0);
}

/**
 * Snapshot the current multi-selection.  Used by every bulk action to
 * freeze the working set at action time so async work isn't disturbed
 * by later user clicks.
 */
function _msSnapshot() {
  return Array.from(multiSelectedIds);
}

/**
 * Acquire the bulk-in-flight lock.  Returns true if acquired, false if
 * a bulk action is already running (in which case the caller shows a
 * toast and bails).
 */
function _bulkAcquire() {
  if (_bulkInFlight) {
    showToast('Another bulk action is in progress', true);
    return false;
  }
  _bulkInFlight = true;
  return true;
}

/** Release the bulk-in-flight lock.  Always pair with _bulkAcquire(). */
function _bulkRelease() {
  _bulkInFlight = false;
}

/**
 * Bulk Stop.  Iterates the selection and emits `close_session` for each
 * running ID; non-running IDs are silently skipped (resolved decision 2
 * in the spec).  No confirmation modal — Stop is non-destructive (nothing
 * is deleted) and matches the single-action UX pattern of "fire and
 * forget" for terminations.
 */
function _bulkStop() {
  // Close the menu first so it doesn't linger.
  const menu = document.querySelector('.session-ctx-menu');
  if (menu) menu.remove();

  if (!_bulkAcquire()) return;
  try {
    const ids = _msSnapshot();
    let stopped = 0;
    for (const id of ids) {
      if (typeof runningIds !== 'undefined' && runningIds.has(id)) {
        if (typeof socket !== 'undefined') {
          socket.emit('close_session', { session_id: id });
        }
        if (typeof guiOpenDelete === 'function') guiOpenDelete(id);
        runningIds.delete(id);
        if (typeof sessionKinds !== 'undefined') delete sessionKinds[id];
        stopped++;
      }
    }
    showToast('Stopped ' + stopped + ' session' + (stopped === 1 ? '' : 's'));
    _clearMultiSelect();
    if (typeof filterSessions === 'function') filterSessions();
  } finally {
    _bulkRelease();
  }
}

/**
 * Bulk Delete.  Modal confirmation listing count + sample of names,
 * then deletes per-ID via the existing /api/delete/<id> endpoint with
 * concurrency=4.  Mirrors single-session deleteSession's cleanup steps
 * (allSessions filter, allSessionIds delete, draft clear, folder unlink,
 * kanban unlink, deselect if active) without firing the per-session
 * confirm modal each time.
 */
async function _bulkDelete() {
  const menu = document.querySelector('.session-ctx-menu');
  if (menu) menu.remove();

  if (!_bulkAcquire()) return;
  try {
    const ids = _msSnapshot();
    // Diagnostic for "deletes only one" reports — captured at action time so
    // the count we operated on is recorded even if state mutates later.
    console.log('[bulk-delete] starting with', ids.length, 'ids:', ids);
    if (!ids.length) return;

    // Build name preview: first 3 names + "and N-3 more".
    const names = ids.map(id => {
      const s = (typeof allSessions !== 'undefined') ? allSessions.find(x => x.id === id) : null;
      return (s && s.display_title) || id.slice(0, 8);
    });
    const sample = names.slice(0, 3).map(n => '<li>' + escHtml(n) + '</li>').join('');
    const more = names.length > 3 ? '<li>and ' + (names.length - 3) + ' more…</li>' : '';
    const body = '<p>Delete <strong>' + ids.length + '</strong> session'
      + (ids.length === 1 ? '' : 's') + '?</p>'
      + '<ul style="margin:6px 0 8px 20px;color:var(--text-secondary);font-size:12px;">'
      + sample + more + '</ul>'
      + '<p>This cannot be undone.</p>';
    const confirmed = await showConfirm('Delete sessions', body, {
      danger: true,
      confirmText: 'Delete ' + ids.length,
      icon: '<svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round">'
        + '<polyline points="3 6 5 6 21 6"/>'
        + '<path d="M19 6v14a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2V6m3 0V4a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2"/></svg>',
    });
    if (!confirmed) return;

    showToast('Deleting ' + ids.length + ' session' + (ids.length === 1 ? '' : 's') + '…');

    // Track which IDs were active so we can deselect at the end.
    const hadActive = (typeof activeId !== 'undefined') && activeId && ids.indexOf(activeId) >= 0;
    const proj = localStorage.getItem('activeProject') || '';
    const projQ = proj ? '?project=' + encodeURIComponent(proj) : '';

    /**
     * Delete a single session by ID.  Mirrors the per-action half of
     * deleteSession() in toolbar.js: stop if running, DELETE the JSONL,
     * clean up local state.  Treats 404 as success (matches existing
     * logic for "another tab already deleted it").  Throws on hard
     * failure so _runBulk records it.
     */
    async function deleteOne(id) {
      // Stop if running so the daemon releases the file before we delete it.
      if (typeof runningIds !== 'undefined' && runningIds.has(id)) {
        if (typeof socket !== 'undefined') socket.emit('close_session', { session_id: id });
        if (typeof guiOpenDelete === 'function') guiOpenDelete(id);
        runningIds.delete(id);
      }
      let okFlag = false;
      try {
        const resp = await fetch('/api/delete/' + id + projQ, { method: 'DELETE' });
        try {
          const data = await resp.json();
          okFlag = !!(data && data.ok) || resp.status === 404;
        } catch (_jsonErr) {
          // Non-JSON response (e.g. 500 HTML) — server-side tombstone is
          // already set; treat as ok so card is removed locally.
          okFlag = true;
        }
      } catch (_fetchErr) {
        // Network error — clean up locally; reload will reconcile.
        okFlag = true;
      }
      if (!okFlag) throw new Error('delete failed');

      // Local cleanup (mirrors deleteSession in toolbar.js).
      if (typeof allSessions !== 'undefined') {
        allSessions = allSessions.filter(x => x.id !== id);
      }
      if (typeof allSessionIds !== 'undefined') allSessionIds.delete(id);
      if (typeof _clearDraft === 'function') _clearDraft(id);
      if (typeof removeSessionFromAllFolders === 'function') removeSessionFromAllFolders(id);
      // Best-effort kanban unlink; do not block on it.
      fetch('/api/kanban/sessions/' + id + '/unlink-all', { method: 'DELETE' }).catch(() => {});
      if (typeof liveSessionId !== 'undefined' && liveSessionId === id
          && typeof stopLivePanel === 'function') {
        stopLivePanel();
      }
    }

    const result = await _runBulk(ids, deleteOne, 4);
    console.log('[bulk-delete] result:', { ok: result.ok, fail: result.fail });

    // If the active session was in the deleted set, navigate away from it.
    if (hadActive && typeof deselectSession === 'function'
        && typeof activeId !== 'undefined' && !allSessionIds.has(activeId)) {
      deselectSession();
    }

    // Refresh sidebar count + project counts.
    const searchEl = document.getElementById('search');
    if (searchEl && typeof allSessions !== 'undefined') {
      searchEl.placeholder = 'Search ' + allSessions.length + ' sessions…';
    }
    if (typeof loadProjects === 'function') loadProjects();
    if (typeof filterSessions === 'function') filterSessions();

    _clearMultiSelect();

    if (result.fail.length === 0) {
      showToast('Deleted ' + result.ok.length + ' session' + (result.ok.length === 1 ? '' : 's'));
    } else if (result.ok.length === 0) {
      showToast('Delete failed for ' + result.fail.length + ' session(s)', true);
    } else {
      showToast('Deleted ' + result.ok.length + ' of ' + ids.length
        + '. ' + result.fail.length + ' failed.', true);
    }
    if (result.fail.length) {
      // Console table for diagnostics (per spec error-handling table).
      try { console.table(result.fail); } catch (_) {}
    }
  } finally {
    _bulkRelease();
  }
}

/**
 * Bulk Auto-name.  Confirmation modal warns about API token cost
 * (resolved decision 1 in the spec), then invokes the existing
 * autoName(id, silent=true) per ID with concurrency=2 (LLM rate-limit
 * aware).  Per-call toasts are suppressed by the silent flag; we show
 * one summary toast at the end.
 */
async function _bulkAutoName() {
  const menu = document.querySelector('.session-ctx-menu');
  if (menu) menu.remove();

  if (!_bulkAcquire()) return;
  try {
    const ids = _msSnapshot();
    if (!ids.length) return;

    const confirmed = await showConfirm('Auto-name sessions',
      '<p>Auto-name <strong>' + ids.length + '</strong> session'
      + (ids.length === 1 ? '' : 's') + '?</p>'
      + '<p style="color:var(--text-muted);font-size:12px;">This will cost API tokens '
      + '(one short LLM call per session).</p>',
      { confirmText: 'Auto-name ' + ids.length });
    if (!confirmed) return;

    showToast('Auto-naming ' + ids.length + ' session' + (ids.length === 1 ? '' : 's') + '…');

    const result = await _runBulk(ids, function(id) {
      return autoName(id, true);
    }, 2);

    _clearMultiSelect();
    if (result.fail.length === 0) {
      showToast('Auto-named ' + result.ok.length + ' session' + (result.ok.length === 1 ? '' : 's'));
    } else {
      showToast('Auto-named ' + result.ok.length + ' of ' + ids.length
        + '. ' + result.fail.length + ' failed.', true);
      try { console.table(result.fail); } catch (_) {}
    }
  } finally {
    _bulkRelease();
  }
}

/**
 * Bulk Duplicate.  No confirmation (file copy is cheap and reversible).
 * Concurrency=4.  Each duplicate hits POST /api/duplicate/<id>; we skip
 * the per-call loadSessions() that single-action duplicateSession does
 * and call it once at the end so we don't re-render the sidebar four
 * times in a row.
 */
async function _bulkDuplicate() {
  const menu = document.querySelector('.session-ctx-menu');
  if (menu) menu.remove();

  if (!_bulkAcquire()) return;
  try {
    const ids = _msSnapshot();
    if (!ids.length) return;

    showToast('Duplicating ' + ids.length + ' session' + (ids.length === 1 ? '' : 's') + '…');

    const proj = localStorage.getItem('activeProject') || '';
    const projQ = proj ? '?project=' + encodeURIComponent(proj) : '';

    async function dupOne(id) {
      const resp = await fetch('/api/duplicate/' + id + projQ, { method: 'POST' });
      const data = await resp.json();
      if (!data || !data.ok) throw new Error((data && data.error) || 'duplicate failed');
    }

    const result = await _runBulk(ids, dupOne, 4);

    // Single sidebar refresh at the end surfaces all the new IDs.
    if (typeof loadSessions === 'function') {
      try { await loadSessions(); } catch (_) {}
    }
    _clearMultiSelect();

    if (result.fail.length === 0) {
      showToast('Duplicated ' + result.ok.length + ' session' + (result.ok.length === 1 ? '' : 's'));
    } else {
      showToast('Duplicated ' + result.ok.length + ' of ' + ids.length
        + '. ' + result.fail.length + ' failed.', true);
      try { console.table(result.fail); } catch (_) {}
    }
  } finally {
    _bulkRelease();
  }
}

/**
 * Bulk Add to Structured composition.  Sequentially opens the existing _addToCompose()
 * picker for each selected session.  Reusing the single-session function
 * preserves the `?project=` filter rule (CLAUDE.md Compose project-scoping
 * item 1) and the existing modal UX users already know.
 *
 * "Apply to all remaining" affordance is deferred to v1.1 (resolved
 * decision 4 in the spec).  v1 ships sequential single-prompt.
 *
 * Picker close detection: we poll pm-overlay's `.show` class via
 * requestAnimationFrame.  If the user closes the picker without picking
 * (cancel or backdrop click), we treat that session as skipped and move on.
 */
async function _bulkAddToCompose() {
  const menu = document.querySelector('.session-ctx-menu');
  if (menu) menu.remove();

  if (!_bulkAcquire()) return;
  try {
    const ids = _msSnapshot();
    if (!ids.length) return;

    const overlay = document.getElementById('pm-overlay');
    if (!overlay) {
      showToast('Cannot open Add to Structured composition picker', true);
      return;
    }

    let added = 0;
    let skipped = 0;
    for (let i = 0; i < ids.length; i++) {
      const id = ids[i];
      // Skip IDs that disappeared between selection and now.
      if (typeof allSessionIds !== 'undefined' && !allSessionIds.has(id)) {
        skipped++;
        continue;
      }
      // Open the picker for this session.  _addToCompose() awaits the
      // composition list fetch but returns before the user interacts.
      try {
        await _addToCompose(id);
      } catch (_e) {
        skipped++;
        continue;
      }
      // Wait for the overlay to close (user picks or cancels).  Cap
      // the wait at 5 minutes per session to avoid hanging forever if
      // something glitches.
      const closed = await _waitForOverlayClose(overlay, 5 * 60 * 1000);
      if (closed === 'timeout') {
        // Safety bail: stop the bulk loop, leave overlay alone.
        showToast('Add to Structured composition timed out — bulk halted', true);
        break;
      }
      // We can't easily distinguish "added" from "cancelled" without
      // hooking into _atc internals; rely on the per-call success toast
      // for that.  Treat every closed iteration as +1 attempted.
      added++;
    }
    _clearMultiSelect();
    showToast('Add to Structured composition finished (' + added + ' processed'
      + (skipped ? ', ' + skipped + ' skipped' : '') + ')');
  } finally {
    _bulkRelease();
  }
}

/**
 * Resolve when the pm-overlay loses its `.show` class (i.e. the picker
 * has been closed by the user or by a successful action).
 * Resolves to 'closed' on close, 'timeout' if we exceed the cap.
 *
 * Uses requestAnimationFrame for cheap polling (~16ms) so the next
 * picker can open as soon as the previous one finishes its 150ms
 * close transition.
 */
function _waitForOverlayClose(overlay, timeoutMs) {
  return new Promise(function(resolve) {
    const start = Date.now();
    function tick() {
      if (!overlay.classList.contains('show')) {
        resolve('closed');
        return;
      }
      if (Date.now() - start > timeoutMs) {
        resolve('timeout');
        return;
      }
      requestAnimationFrame(tick);
    }
    // Give the overlay a tick to register .show after _addToCompose's
    // async fetch resolves and _atcShowOverlay runs.
    setTimeout(tick, 50);
  });
}
