/* live-panel.js — live terminal panel, input bar state machine, GUI session management */

// ── Draft persistence: preserve unsent text across session/view switches AND page reloads ──
const _LS_DRAFTS_KEY = 'vibenode_drafts';

function _loadDraftsFromStorage() {
  try {
    const raw = localStorage.getItem(_LS_DRAFTS_KEY);
    return raw ? JSON.parse(raw) : {};
  } catch (_) { return {}; }
}

function _persistDraftsToStorage(drafts) {
  try { localStorage.setItem(_LS_DRAFTS_KEY, JSON.stringify(drafts)); } catch (_) {}
}

const _drafts = _loadDraftsFromStorage();  // { sessionId: string }

function _saveDraft(sessionId, text) {
  if (!sessionId) return;
  if (text) {
    _drafts[sessionId] = text;
  } else {
    delete _drafts[sessionId];
  }
  _persistDraftsToStorage(_drafts);
}

function _getDraft(sessionId) {
  return _drafts[sessionId] || '';
}

function _clearDraft(sessionId) {
  delete _drafts[sessionId];
  _persistDraftsToStorage(_drafts);
}

// ── Strip "Sent from Q at ..." footer from message text for display ──
function _stripVnMeta(text) {
  const m = text.match(/\n{1,2}Sent from Q at (\d{4}-\d{2}-\d{2} \d{1,2}:\d{2} [AP]M)(\s*\(transcribed from voice[^)]*\))?\s*$/);
  if (!m) return {clean: text, sentAt: null, voice: false};
  return {clean: text.replace(m[0], ''), sentAt: m[1], voice: !!m[2]};
}

function _formatSentAt(str) {
  if (!str) return '';
  // Already human-readable like "2026-04-11 2:30 PM", just extract the time part
  const timePart = str.match(/(\d{1,2}:\d{2} [AP]M)$/);
  return timePart ? timePart[1] : str;
}

function _saveDraftFromDOM() {
  if (!liveSessionId) return;
  const ta = document.getElementById('live-input-ta') || document.getElementById('live-queue-ta');
  if (ta && ta.value.trim()) {
    _saveDraft(liveSessionId, ta.value);
  } else {
    _clearDraft(liveSessionId);
  }
}

// Save drafts before page unload (server restart, refresh, tab close)
window.addEventListener('beforeunload', _saveDraftFromDOM);

function _updateLastMessageTimes() {
  const log = document.getElementById('live-log');
  if (!log) return;
  // Clear existing
  log.querySelectorAll('.show-time').forEach(el => el.classList.remove('show-time'));
  // Find last user and last assistant
  const users = log.querySelectorAll('.msg.user');
  const assts = log.querySelectorAll('.msg.assistant');
  if (users.length) users[users.length - 1].classList.add('show-time');
  if (assts.length) assts[assts.length - 1].classList.add('show-time');
}

function _formatMsgTime(tsStr) {
  // Backend sends Unix seconds (time.time()), JS Date expects milliseconds
  const val = typeof tsStr === 'number' && tsStr < 1e12 ? tsStr * 1000 : tsStr;
  const d = new Date(val);
  if (isNaN(d)) return '';
  let h = d.getHours();
  const ampm = h >= 12 ? 'PM' : 'AM';
  h = h % 12 || 12;
  return h + ':' + String(d.getMinutes()).padStart(2, '0') + ' ' + ampm;
}

let liveLineCount = 0;
let _liveSending = false;
let liveAutoScroll = true;
// Monotonic counter used to tag optimistic user bubbles so they can be
// matched/replaced when the server echoes the entry back — without any
// text-based comparison that would eat legitimate duplicate messages.
let _optimisticMsgId = 0;

// Client-side pagination for long chat threads.
// The server sends ALL entries; we stash them in memory and only render
// the last PAGE_SIZE initially. "Load older" renders more from the stash.
const LIVE_PAGE_SIZE = 100;
let _liveEntryStash = [];       // full entry list from server (kept in memory)
let _liveRenderedFrom = 0;      // index into _liveEntryStash of the oldest rendered entry

function _createLoadMoreButton() {
  const wrap = document.createElement('div');
  wrap.className = 'live-load-more';
  const remaining = _liveRenderedFrom;
  wrap.innerHTML =
    '<button class="live-load-more-btn" id="live-load-more-btn" onclick="liveLoadMore()">' +
    '<svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" style="vertical-align:middle;"><polyline points="18 15 12 9 6 15"/></svg> ' +
    'Load older messages' +
    '<span style="color:var(--text-faint);margin-left:4px;">(' + remaining + ' more)</span>' +
    '</button>';
  return wrap;
}

function liveLoadMore() {
  if (_liveRenderedFrom <= 0 || !liveSessionId) return;
  const logEl = document.getElementById('live-log');
  if (!logEl) return;

  const prevHeight = logEl.scrollHeight;
  const prevScroll = logEl.scrollTop;

  // Remove existing "load more" button
  const existingBtn = logEl.querySelector('.live-load-more');
  if (existingBtn) existingBtn.remove();

  // Render the next batch from the stash
  const start = Math.max(0, _liveRenderedFrom - LIVE_PAGE_SIZE);
  const batch = _liveEntryStash.slice(start, _liveRenderedFrom);
  _liveRenderedFrom = start;

  const frag = document.createDocumentFragment();
  if (_liveRenderedFrom > 0) {
    frag.appendChild(_createLoadMoreButton());
  }
  batch.forEach((entry) => {
    frag.appendChild(renderLiveEntry(entry));
  });

  logEl.insertBefore(frag, logEl.firstChild);

  // Restore scroll position so viewport stays on the same messages
  const newHeight = logEl.scrollHeight;
  logEl.scrollTop = prevScroll + (newHeight - prevHeight);
}
// Per-session queue — server-backed local cache (synced via queue_updated events)
const _sessionQueues = {};
let _queueViewIndex = 0;

// Migrate any old localStorage queues to server on first load
(function _migrateOldQueues() {
  try {
    const raw = JSON.parse(localStorage.getItem('_sessionQueues') || '{}');
    let migrated = false;
    for (const k in raw) {
      const arr = Array.isArray(raw[k]) ? raw[k] : (raw[k] ? [raw[k]] : []);
      if (arr.length) {
        _sessionQueues[k] = arr;
        // Push to server once socket is connected
        setTimeout(() => {
          if (typeof socket !== 'undefined' && socket.connected) {
            arr.forEach(text => socket.emit('queue_message', {session_id: k, text: text}));
          }
        }, 2000);
        migrated = true;
      }
    }
    if (migrated) localStorage.removeItem('_sessionQueues');
  } catch(e) {}
})();

// Read-only local cache accessors (data comes from server via queue_updated events)
function _getQueueList(sid) { return (sid && _sessionQueues[sid]) || []; }
function _getQueue(sid) { const q = _getQueueList(sid); return q.length ? q[0] : ''; }

// Server-backed queue mutations — emit to server, cache updated via queue_updated event
function _addQueue(sid, text) {
  if (!sid || !text) return;
  // Optimistic local update for instant UI feedback
  if (!_sessionQueues[sid]) _sessionQueues[sid] = [];
  _sessionQueues[sid].push(text);
  _queueViewIndex = _sessionQueues[sid].length - 1;
  // Send to server (authoritative)
  socket.emit('queue_message', {session_id: sid, text: text});
}
function _removeQueueAt(sid, idx) {
  if (!sid || !_sessionQueues[sid]) return;
  // Optimistic local update
  _sessionQueues[sid].splice(idx, 1);
  if (!_sessionQueues[sid].length) delete _sessionQueues[sid];
  else if (_queueViewIndex >= _sessionQueues[sid].length) _queueViewIndex = _sessionQueues[sid].length - 1;
  // Send to server
  socket.emit('remove_queue_item', {session_id: sid, index: idx});
}
function _setQueue(sid, text) {
  if (!sid) return;
  // Clear then add — server-backed
  socket.emit('clear_queue', {session_id: sid});
  if (text) {
    _sessionQueues[sid] = [text];
    _queueViewIndex = 0;
    socket.emit('queue_message', {session_id: sid, text: text});
  } else {
    delete _sessionQueues[sid];
    _queueViewIndex = 0;
  }
}
// _shiftQueue is no longer used — server auto-dispatches from queue on idle

/** Render (or clear) the queue banner in the dedicated #live-queue-area div. */
function _renderQueueBanner() {
  const area = document.getElementById('live-queue-area');
  if (!area) return;
  const sid = liveSessionId;
  const list = _getQueueList(sid);
  if (!list.length) { area.innerHTML = ''; return; }
  const idx = Math.min(_queueViewIndex, list.length - 1);
  _queueViewIndex = idx;
  const total = list.length;
  const text = list[idx];

  let navHtml = '';
  if (total > 1) {
    navHtml =
      '<button class="live-queue-nav-btn" onclick="liveQueueNav(-1)" title="Previous"' + (idx === 0 ? ' disabled' : '') + '>' +
      '<svg width="10" height="10" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round"><polyline points="15 18 9 12 15 6"/></svg></button>' +
      '<span style="font-size:10px;color:var(--text-faint);min-width:28px;text-align:center;">' + (idx+1) + '/' + total + '</span>' +
      '<button class="live-queue-nav-btn" onclick="liveQueueNav(1)" title="Next"' + (idx === total-1 ? ' disabled' : '') + '>' +
      '<svg width="10" height="10" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round"><polyline points="9 18 15 12 9 6"/></svg></button>';
  }

  area.innerHTML =
    '<div class="live-queue-banner">' +
    '<div class="live-queue-banner-header">' +
    '<span class="live-queue-banner-label">' +
    '<svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" style="vertical-align:middle;margin-right:4px;"><circle cx="12" cy="12" r="10"/><polyline points="12 6 12 12 16 14"/></svg>' +
    'Queued' + (total > 1 ? ' (' + total + ')' : '') + ' \u2014 will send when idle</span>' +
    '<span style="display:flex;align-items:center;gap:4px;">' +
    navHtml +
    '<button class="live-queue-cancel-btn" onclick="liveEditQueue()" title="Edit this command">' +
    '<svg width="10" height="10" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" style="vertical-align:middle;margin-right:2px;"><path d="M11 4H4a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h14a2 2 0 0 0 2-2v-7"/><path d="M18.5 2.5a2.121 2.121 0 0 1 3 3L12 15l-4 1 1-4 9.5-9.5z"/></svg>' +
    'Edit</button>' +
    '<button class="live-queue-cancel-btn" onclick="liveClearQueue()" title="Remove this command" style="color:var(--result-err,#c44);border-color:rgba(204,68,68,0.25);">' +
    '<svg width="10" height="10" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" style="vertical-align:middle;margin-right:2px;"><line x1="18" y1="6" x2="6" y2="18"/><line x1="6" y1="6" x2="18" y2="18"/></svg>' +
    'Remove</button>' +
    '</span>' +
    '</div>' +
    '<div class="live-queue-banner-text">' + escHtml(text) + '</div>' +
    '</div>';
}

function liveQueueNav(dir) {
  const list = _getQueueList(liveSessionId);
  if (!list.length) return;
  _queueViewIndex = Math.max(0, Math.min(list.length - 1, _queueViewIndex + dir));
  _renderQueueBanner();
}

// ---------------------------------------------------------------------------
// Output shelf — file cards for output files created during the session
// ---------------------------------------------------------------------------
const _OUTPUT_EXTS = new Set([
  'xlsx','xlsm','xls','docx','doc','pptx','ppt','pdf',
  'png','jpg','jpeg','gif','svg','bmp',
  'csv','json','zip','html','txt'
]);
const _outputShelfPaths = new Set();

function _outputFileIcon(ext) {
  ext = (ext || '').toLowerCase();
  if (['xlsx','xlsm','xls','csv'].includes(ext)) return '<span style="color:#22a55b;">&#128203;</span>';
  if (['docx','doc','txt'].includes(ext)) return '<span style="color:#4a90d9;">&#128196;</span>';
  if (['pptx','ppt'].includes(ext)) return '<span style="color:#e07020;">&#128202;</span>';
  if (ext === 'pdf') return '<span style="color:#cc3333;">&#128213;</span>';
  if (['png','jpg','jpeg','gif','svg','bmp'].includes(ext)) return '<span style="color:#9b59b6;">&#128444;</span>';
  if (ext === 'zip') return '<span style="color:#888;">&#128230;</span>';
  if (ext === 'json') return '<span style="color:#e8a838;">&#123;&#125;</span>';
  if (ext === 'html') return '<span style="color:#e06050;">&#9674;</span>';
  return '<span style="color:#888;">&#128196;</span>';
}

function _tryAddOutputCard(entry) {
  if (!entry) return;
  // Strategy 1: Direct file tool (Write, Edit, etc.)
  if (entry.kind === 'tool_use') {
    const name = (entry.name || '').toLowerCase();
    if (['write','edit','multiedit','notebookedit'].includes(name)) {
      let filePath = entry.desc || '';
      if (!filePath) return;
      filePath = filePath.replace(/\s*\(write \d+ chars\)\s*$/, '');
      filePath = filePath.replace(/^file_path:\s*/, '');
      filePath = filePath.trim();
      _addOutputCardForPath(filePath);
      return;
    }
    // Strategy 1b: Bash commands that reference output files (e.g. python scripts saving xlsx)
    if (name === 'bash') {
      const desc = entry.desc || '';
      _scanTextForOutputPaths(desc);
      return;
    }
  }

  // Strategy 2: Scan tool_result and asst text for file paths with output extensions
  // Catches files created via Bash/python scripts (xlsx, docx, pptx, etc.)
  if (entry.kind === 'tool_result' || entry.kind === 'asst') {
    _scanTextForOutputPaths(entry.text || '');
    return;
  }
}

function _scanTextForOutputPaths(text) {
  if (!text) return;
  // Match Windows paths ending in known extensions (handles both \ and / separators)
  // Also match paths inside quotes: r'C:\...\file.xlsx' or "C:\...\file.xlsx"
  const pathPattern = /([A-Za-z]:[\\\/][^\s"'<>|*?\n]+\.(?:xlsx|xlsm|xls|docx|doc|pptx|ppt|pdf|png|jpg|jpeg|gif|svg|csv|json|html))\b/gi;
  let match;
  while ((match = pathPattern.exec(text)) !== null) {
    let p = match[1];
    // Clean up common wrapper chars
    p = p.replace(/^['"]|['"]$/g, '');
    _addOutputCardForPath(p);
  }
}

function _addOutputCardForPath(filePath) {
  if (!filePath) return;
  filePath = filePath.trim();
  // Check extension against allowlist
  const dot = filePath.lastIndexOf('.');
  if (dot < 0) return;
  const ext = filePath.slice(dot + 1).toLowerCase();
  if (!_OUTPUT_EXTS.has(ext)) return;
  // Dedup
  if (_outputShelfPaths.has(filePath)) return;
  _outputShelfPaths.add(filePath);
  const logEl = document.getElementById('live-log');
  if (!logEl) return;
  // Build card inline in conversation
  const card = document.createElement('div');
  card.className = 'live-output-card-inline';
  const basename = filePath.split(/[/\\]/).pop() || filePath;
  card.title = filePath;
  card.innerHTML =
    '<span class="live-output-card-icon">' + _outputFileIcon(ext) + '</span>' +
    '<span class="live-output-card-name">' + escHtml(basename) + '</span>' +
    '<span class="live-output-card-size" data-path="' + escHtml(filePath) + '"></span>' +
    '<button class="live-output-card-open" title="Open file" onclick="event.stopPropagation();_openOutputFile(\'' + escHtml(filePath.replace(/\\/g,'\\\\').replace(/'/g,"\\'")) + '\')">' +
    '<svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><path d="M18 13v6a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V8a2 2 0 0 1 2-2h6"/><polyline points="15 3 21 3 21 9"/><line x1="10" y1="14" x2="21" y2="3"/></svg>' +
    '</button>' +
    '<button class="live-output-card-dl" title="Save to..." onclick="event.stopPropagation();_downloadOutputFile(\'' + escHtml(filePath.replace(/\\/g,'\\\\').replace(/'/g,"\\'")) + '\')">' +
    '<svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round"><path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"/><polyline points="7 10 12 15 17 10"/><line x1="12" y1="15" x2="12" y2="3"/></svg>' +
    '</button>';
  logEl.appendChild(card);
  if (liveAutoScroll) logEl.scrollTop = logEl.scrollHeight;
  // Fetch file size asynchronously
  fetch('/api/file-info?path=' + encodeURIComponent(filePath))
    .then(r => r.json())
    .then(info => {
      const sizeEl = card.querySelector('.live-output-card-size');
      if (sizeEl && info.size) sizeEl.textContent = info.size;
    })
    .catch(() => {});
}

function _openOutputFile(filePath) {
  fetch('/api/open-file', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({path: filePath})
  }).then(r => r.json()).then(d => {
    if (d.error) showToast(d.error, true);
  }).catch(() => showToast('Failed to open file', true));
}

function _downloadOutputFile(filePath) {
  // Open folder picker so user can choose where to save
  if (typeof _fdShowPicker === 'function' && typeof _fdOnPickerDone !== 'undefined') {
    _fdOnPickerDone = function(targetDir) {
      fetch('/api/copy-file-to', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({source: filePath, target_dir: targetDir})
      }).then(r => r.json()).then(d => {
        if (d.ok) showToast('Saved to ' + d.path);
        else showToast(d.error || 'Save failed', true);
      }).catch(() => showToast('Save failed', true));
    };
    _fdShowPicker(null);  // null = no file upload, just pick a folder
  } else {
    // Fallback: save to Downloads directly
    fetch('/api/download-to-downloads', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({path: filePath})
    }).then(r => r.json()).then(d => {
      if (d.ok) showToast('Copied to Downloads: ' + d.filename);
      else showToast(d.error || 'Download failed', true);
    }).catch(() => showToast('Download failed', true));
  }
}

function _clearOutputShelf() {
  _outputShelfPaths.clear();
  const shelf = document.getElementById('live-output-shelf');
  if (shelf) shelf.innerHTML = '';
}

let liveBarState = null;   // 'ended' | 'question:<questionText>' | 'idle' | 'working'
let _guiFocusPending = false;
let _liveWorkingStart = null;  // timestamp when working state began
let _liveWorkingTimer = null;  // interval for elapsed time updates

function guiOpenAdd(id) {
  guiOpenSessions.add(id);
  localStorage.setItem('guiOpenSessions', JSON.stringify([...guiOpenSessions]));
}
function guiOpenDelete(id) {
  guiOpenSessions.delete(id);
  localStorage.setItem('guiOpenSessions', JSON.stringify([...guiOpenSessions]));
}

async function openInGUI(id) {
  // In kanban mode, render session inside the kanban board with kanban titlebar
  if (typeof viewMode !== 'undefined' && viewMode === 'kanban' && typeof _openSessionInKanban === 'function') {
    _openSessionInKanban(id);
    return;
  }
  // In compose mode, render session inside the compose board with compose titlebar
  if (typeof viewMode !== 'undefined' && viewMode === 'compose' && typeof _openSessionInCompose === 'function') {
    _openSessionInCompose(id);
    return;
  }
  _guiFocusPending = true;
  closeAllGrpDropdowns();
  if (typeof _ensureMainBodyVisible === 'function') _ensureMainBodyVisible();
  activeId = id;
  localStorage.setItem('activeSessionId', id || '');
  // Track most-recent session per project so view/project switches can restore it
  const _proj = localStorage.getItem('activeProject');
  if (_proj && id) {
    localStorage.setItem('projectSession_' + _proj, id);
    localStorage.setItem('pvs_' + _proj + '_sessions', id);
  }
  _pushChatUrl(id);
  if (runningIds.has(id)) guiOpenAdd(id);
  if (liveSessionId && liveSessionId !== id) { stopLivePanel(); }
  filterSessions();

  // Show title from sidebar data immediately (no mismatch)
  const cached = allSessions.find(x => x.id === id);
  const initTitle = cached ? cached.display_title : 'Loading\u2026';
  setToolbarSession(id, initTitle, !(cached && cached.custom_title), (cached && cached.custom_title) || '');
  document.getElementById('main-body').innerHTML = _chatSkeleton();
  const _lpProj = localStorage.getItem('activeProject') || '';
  const _lpProjQ = _lpProj ? '&project=' + encodeURIComponent(_lpProj) : '';
  const resp = await fetch('/api/session/' + id + '?meta_only=1' + _lpProjQ);

  // New session with no .jsonl yet — re-show the new session input
  if (!resp.ok) {
    if (guiOpenSessions.has(id) && !runningIds.has(id)) {
      // Re-create the new session chat view — preserve auto-name from sidebar if available
      const _ct = cached && cached.custom_title ? cached.custom_title : (cached && cached.display_title && cached.display_title !== 'New Session' ? cached.display_title : '');
      setToolbarSession(id, _ct || 'New Session', !_ct, _ct || '');
      document.getElementById('main-body').innerHTML =
        '<div class="live-panel" id="live-panel">' +
        '<div class="conversation live-log" id="live-log">' +
        '<div class="empty-state" style="padding:60px 0;text-align:center;">' +
        '<svg width="32" height="32" viewBox="0 0 24 24" fill="none" stroke="var(--text-faint)" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round" style="margin-bottom:12px;opacity:0.4;"><polyline points="16 18 22 12 16 6"/><polyline points="8 6 2 12 8 18"/></svg>' +
        '<div class="vibenode-greeting">What will we VibeNode today?</div>' +
        (typeof _renderTemplateGrid === 'function' ? _renderTemplateGrid(id) : '') +
        '</div></div>' +
        '<div class="live-output-shelf" id="live-output-shelf"></div>' +
        '<div id="live-queue-area"></div>' +
        '<div class="live-input-bar" id="live-input-bar"></div></div>';
      liveSessionId = id;
      liveBarState = null;
      _optimisticMsgId = 0;
      const bar = document.getElementById('live-input-bar');
      if (bar) {
        bar.innerHTML =
          '<textarea id="live-input-ta" class="live-textarea" rows="3" placeholder="Describe what you want Claude to do\u2026" autofocus' +
          ' onkeydown="if(_shouldSend(event)){event.preventDefault();_newSessionSubmit(\'' + id + '\')}">' +
          '</textarea>' +
          '<div class="live-bar-row">' +
          '<span class="send-hint" style="font-size:10px;color:var(--text-faint);">' + _sendHint() + '</span>' +
          '<button class="live-send-btn" id="live-voice-btn"></button>' +
          '</div>';
        setupVoiceButton(document.getElementById('live-input-ta'), document.getElementById('live-voice-btn'), () => _newSessionSubmit(id));
        setTimeout(() => { const ta = document.getElementById('live-input-ta'); if (ta) { ta.focus(); _initAutoResize(ta); ta.addEventListener('input', function() { if (typeof _hideTemplateGrid === 'function') _hideTemplateGrid(); }); } }, 50);
      }
      return;
    }
    // Truly not found — show error
    document.getElementById('main-body').innerHTML = '<div style="padding:40px;color:var(--text-faint);text-align:center;">Session not found</div>';
    return;
  }

  const s = await resp.json();
  // Prefer server title, but fall back to cached sidebar title if server returns empty/generic
  const _serverTitle = s.custom_title || s.display_title;
  const _cachedFallback = cached && cached.custom_title ? cached.custom_title : (cached && cached.display_title && cached.display_title !== 'New Session' ? cached.display_title : '');
  const _finalTitle = (_serverTitle && _serverTitle !== 'New Session') ? _serverTitle : (_cachedFallback || _serverTitle);
  const _finalCustom = s.custom_title || _cachedFallback || '';
  setToolbarSession(id, _finalTitle, !_finalCustom, _finalCustom);

  startLivePanel(id);
}

function startLivePanel(id, opts) {
  stopLivePanel();
  liveSessionId = id;
  liveLineCount = 0;
  liveAutoScroll = true;
  if (!(opts && opts.skipLog)) _optimisticMsgId = 0;
  // Queue is restored from per-session localStorage — no reset here
  // Restore working_since from the map (set by state_snapshot/session_state)
  if (window._workingSinceMap && window._workingSinceMap[id]) {
    _liveWorkingStart = window._workingSinceMap[id];
  }
  liveBarState = null;  // force fresh render

  const skipLog = opts && opts.skipLog;
  const skelHtml = skipLog ? '' : _chatSkeleton().replace('<div class="conversation">', '').replace(/<\/div>$/, '');
  const panelHtml =
    '<div class="live-panel" id="live-panel">' +
    '<div class="conversation live-log" id="live-log">' + skelHtml + '</div>' +
    '<div class="live-output-shelf" id="live-output-shelf"></div>' +
    '<div id="live-queue-area"></div>' +
    '<div class="live-input-bar" id="live-input-bar"></div></div>';

  // In kanban mode, write to the kanban session body if it exists
  const kanbanSessionBody = document.querySelector('.kanban-session-body');
  if (kanbanSessionBody) {
    kanbanSessionBody.innerHTML = panelHtml;
  } else {
    document.getElementById('main-body').innerHTML = panelHtml;
  }

  _clearOutputShelf();
  const logEl = document.getElementById('live-log');
  logEl.addEventListener('scroll', () => {
    const atBottom = logEl.scrollHeight - logEl.scrollTop - logEl.clientHeight < 60;
    liveAutoScroll = atBottom;
  });

  // Initialize sticky user message bar on the live log container
  if (typeof initStickyUserMessages === 'function') initStickyUserMessages(logEl);

  const btnClose = document.getElementById('btn-close');
  if (btnClose) btnClose.disabled = false;

  // Reset client-side pagination stash for new panel
  _liveEntryStash = [];
  _liveRenderedFrom = 0;

  // Request the log via WebSocket (skip for brand-new sessions — optimistic bubble is enough)
  if (!skipLog) {
    socket.emit('get_session_log', {session_id: id, since: 0, project: localStorage.getItem('activeProject') || '', is_working: sessionKinds[id] === 'working' || sessionKinds[id] === 'question'});
  }


  // Render input bar immediately and schedule re-renders in case
  // state events arrived before the DOM was ready.
  // BUT: skip re-render if user has already started typing.
  liveBarState = null;
  updateLiveInputBar();
  _renderQueueBanner();

  // Eagerly fetch this session's state from the daemon so we don't
  // show "sleeping" while waiting for the full state_snapshot.
  if (!runningIds.has(id) && !sessionKinds[id]) {
    fetch('/api/session/' + id + '?meta_only=1' + (_lpProj ? '&project=' + encodeURIComponent(_lpProj) : ''))
      .then(r => r.ok ? r.json() : null)
      .then(d => {
        if (!d || liveSessionId !== id) return;
        // Check daemon state directly
        socket.emit('request_state_snapshot', { project: localStorage.getItem('activeProject') || '' });
      }).catch(() => {});
  }

  setTimeout(() => {
    const ta = document.getElementById('live-input-ta');
    if (ta && ta.value.trim()) return; // user is typing, don't clobber
    updateLiveInputBar();
    _renderQueueBanner();
  }, 500);
  setTimeout(() => {
    const ta = document.getElementById('live-input-ta');
    if (ta && ta.value.trim()) return;
    updateLiveInputBar();
    _renderQueueBanner();
  }, 2000);
}

function stopLivePanel() {
  _saveDraftFromDOM();
  if (typeof _stopActiveVoice === 'function') _stopActiveVoice();
  liveSessionId = null;
  liveBarState = null;
  _clearOutputShelf();
  const btnClose = document.getElementById('btn-close');
  if (btnClose) btnClose.disabled = true;
}

/**
 * Auto-send any pending text in the live panel textareas before switching
 * away from the current session.  Fires the appropriate socket event
 * without heavy DOM manipulation (since the panel is about to be torn down).
 */
function _autoSendPendingInput() {
  if (!liveSessionId) return;
  const id = liveSessionId;

  const inputTa = document.getElementById('live-input-ta');
  const queueTa = document.getElementById('live-queue-ta');

  if (inputTa && inputTa.value.trim()) {
    const text = inputTa.value.trim();
    inputTa.value = '';
    const kind = sessionKinds[id];
    const isRunning = runningIds.has(id);

    if (!isRunning && guiOpenSessions.has(id) && !kind) {
      // New session that hasn't been submitted yet — start it
      runningIds.add(id);
      sessionKinds[id] = 'working';
      const startOpts = { session_id: id, prompt: text, cwd: _currentProjectDir(), name: '' };
      if (typeof defaultModel !== 'undefined' && defaultModel) startOpts.model = defaultModel;
      if (typeof defaultThinking !== 'undefined' && defaultThinking) startOpts.thinking_level = defaultThinking;
      socket.emit('start_session', startOpts);
      // Set placeholder title from message text
      const s = allSessions.find(x => x.id === id);
      const _ph = text.split('\n')[0].slice(0, 65) + (text.length > 65 ? '\u2026' : '');
      if (s) s.display_title = _ph;
      filterSessions();
    } else if (!isRunning) {
      // Ended session — resume with the typed text
      socket.emit('start_session', { session_id: id, prompt: text, cwd: _currentProjectDir(), resume: true });
      runningIds.add(id);
      guiOpenAdd(id);
    } else if (kind === 'question' && waitingData[id]) {
      // Permission/question response
      const actionMap = {yes: 'y', no: 'n', always: 'a', 'almost always': 'aa', 'almost-always': 'aa', 'almostalways': 'aa', allow: 'y', deny: 'n'};
      const action = actionMap[text.toLowerCase()] || text;
      socket.emit('permission_response', {session_id: id, action: action});
      delete waitingData[id];
      sessionKinds[id] = 'working';
    } else if (kind === 'idle' || kind === 'question') {
      // Idle or question without waitingData — send as a message
      socket.emit('send_message', {session_id: id, text: text});
      sessionKinds[id] = 'working';
    }
  } else if (queueTa && queueTa.value.trim()) {
    // Working state — queue the typed text
    const text = queueTa.value.trim();
    queueTa.value = '';
    _addQueue(id, text);
  }
}

function renderLiveEntry(e) {
  const div = document.createElement('div');
  if (!e) return div;

  if (e.kind === 'user' || e.kind === 'asst') {
    const role = e.kind === 'user' ? 'user' : 'assistant';
    let text = e.text || '';
    let vnSentAt = null;
    let vnIsVoice = false;
    if (e.kind === 'user') {
      const vnMeta = _stripVnMeta(text);
      text = vnMeta.clean;
      vnSentAt = vnMeta.sentAt;
      vnIsVoice = vnMeta.voice;
    }

    // Render bracketed messages like [Request interrupted by user] as centered pills
    if (e.kind === 'user' && /^\[.+\]$/.test(text.trim())) {
      div.className = 'live-entry live-interrupted';
      div.innerHTML =
        '<div class="live-interrupted-pill">' +
        '<span>' + escHtml(text.trim().slice(1, -1)) + '</span>' +
        '</div>';
      return div;
    }

    div.className = 'msg ' + role;
    const LIMIT = e.kind === 'asst' ? 600 : 800;
    const displayText = text.length > LIMIT ? text.slice(0, LIMIT) : text;

    const roleLabel = e.kind === 'user' ? 'me' : 'claude';
    const ts = e.timestamp ? _formatMsgTime(e.timestamp) : '';
    let body;
    if (e.kind === 'asst') {
      body = mdParse(displayText);
    } else {
      body = '<pre style="white-space:pre-wrap;margin:0;">' + escHtml(displayText) + '</pre>';
    }
    div.innerHTML = '<div class="msg-role">' + roleLabel + (ts ? ' <span class="msg-time">' + ts + '</span>' : '') + '</div>' +
      '<div class="msg-body msg-content">' + body + '</div>';

    if (e.kind === 'asst' && typeof addSmartCopyButtons === 'function') {
      addSmartCopyButtons(div.querySelector('.msg-body'), displayText);
    }

    if (text.length > LIMIT) {
      const btn = document.createElement('button');
      btn.className = 'live-expand-btn';
      btn.textContent = '\u2026 show more';
      btn.onclick = () => {
        const bodyEl = div.querySelector('.msg-body');
        if (e.kind === 'asst') {
          bodyEl.innerHTML = mdParse(text);
          if (typeof addSmartCopyButtons === 'function') addSmartCopyButtons(bodyEl, text);
        }
        else { bodyEl.innerHTML = '<pre style="white-space:pre-wrap;margin:0;">' + escHtml(text) + '</pre>'; }
        btn.remove();
      };
      div.appendChild(btn);
    }

    // Add VN metadata footer for user messages
    if (e.kind === 'user' && vnSentAt) {
      const footer = document.createElement('div');
      footer.className = 'vn-msg-footer';
      footer.innerHTML = (vnIsVoice ? '<span class="vn-msg-footer-icon">\ud83c\udf99\ufe0f</span> Transcribed from voice \u00b7 ' : '<span class="vn-msg-footer-icon">\u26a1</span> ') + 'Sent at ' + _formatSentAt(vnSentAt);
      div.appendChild(footer);
    }

  } else if (e.kind === 'tool_use') {
    const _isAgent = (e.name === 'Agent');
    div.className = 'live-entry live-entry-tool' + (_isAgent ? ' live-entry-agent' : '');
    const toolLine = document.createElement('div');
    toolLine.className = 'live-tool-line';
    // Use a special robot icon for Agent tool, gear icon for everything else
    const _toolSvg = _isAgent
      ? '<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><rect x="3" y="11" width="18" height="10" rx="2"/><circle cx="12" cy="5" r="2"/><line x1="12" y1="7" x2="12" y2="11"/><circle cx="8" cy="16" r="1" fill="currentColor"/><circle cx="16" cy="16" r="1" fill="currentColor"/></svg>'
      : '<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><circle cx="12" cy="12" r="3"/><path d="M19.4 15a1.65 1.65 0 0 0 .33 1.82l.06.06a2 2 0 0 1-2.83 2.83l-.06-.06a1.65 1.65 0 0 0-1.82-.33 1.65 1.65 0 0 0-1 1.51V21a2 2 0 0 1-4 0v-.09A1.65 1.65 0 0 0 9 19.4a1.65 1.65 0 0 0-1.82.33l-.06.06a2 2 0 0 1-2.83-2.83l.06-.06A1.65 1.65 0 0 0 4.68 15a1.65 1.65 0 0 0-1.51-1H3a2 2 0 0 1 0-4h.09A1.65 1.65 0 0 0 4.6 9"/></svg>';
    toolLine.innerHTML = '<span class="live-tool-icon">' + _toolSvg + '</span>' +
      '<span class="live-tool-name">' + escHtml(e.name || 'tool') + '</span>' +
      '<span class="live-tool-desc">' + escHtml((e.desc || '').slice(0, 120)) + '</span>' +
      '<button class="live-expand-btn">\u25be</button>';

    const detail = document.createElement('div');
    detail.className = 'live-tool-detail';
    detail.textContent = e.desc || '';

    toolLine.onclick = () => detail.classList.toggle('open');
    div.appendChild(toolLine);
    div.appendChild(detail);

  } else if (e.kind === 'tool_result') {
    div.className = 'live-entry live-entry-result';
    const ok = !e.is_error;
    const text = e.text || '';

    const line = document.createElement('div');
    line.className = 'live-result-line ' + (ok ? 'live-result-ok' : 'live-result-err');
    line.style.cursor = 'pointer';
    const checkSvg = '<svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" style="vertical-align:middle;margin-right:4px;"><polyline points="20 6 9 17 4 12"/></svg>';
    const xSvg = '<svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" style="vertical-align:middle;margin-right:4px;"><line x1="18" y1="6" x2="6" y2="18"/><line x1="6" y1="6" x2="18" y2="18"/></svg>';
    line.innerHTML = (ok ? checkSvg : xSvg) + escHtml(text.slice(0, 80)) + (text.length > 80 ? '\u2026' : '');

    const detail = document.createElement('div');
    detail.className = 'live-tool-detail';
    detail.innerHTML = mdParse(_colorDiffLines(escHtml(text)));

    line.onclick = () => detail.classList.toggle('open');
    div.appendChild(line);
    div.appendChild(detail);

  } else if (e.kind === 'system') {
    const text = e.text || e.message || '';

    /* ── Special: interrupted by user ── */
    if (/interrupted by user/i.test(text)) {
      div.className = 'live-entry live-interrupted';
      div.innerHTML =
        '<div class="live-interrupted-pill">' +
          '<svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round">' +
            '<rect x="6" y="6" width="12" height="12" rx="2"/>' +
          '</svg>' +
          '<span>Request stopped by user</span>' +
        '</div>';
      return div;
    }

    div.className = 'live-entry live-entry-result';
    const isErr = !!e.is_error;
    const line = document.createElement('div');
    line.className = isErr ? 'live-result-line live-result-err' : 'live-result-line';
    line.style.cursor = 'pointer';
    if (!isErr) line.style.color = 'var(--text-muted)';
    const icon = isErr
      ? '<svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" style="vertical-align:middle;margin-right:4px;"><path d="M10.29 3.86L1.82 18a2 2 0 0 0 1.71 3h16.94a2 2 0 0 0 1.71-3L13.71 3.86a2 2 0 0 0-3.42 0z"/><line x1="12" y1="9" x2="12" y2="13"/><line x1="12" y1="17" x2="12.01" y2="17"/></svg>'
      : '<svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" style="vertical-align:middle;margin-right:4px;"><circle cx="12" cy="12" r="10"/><line x1="12" y1="16" x2="12" y2="12"/><line x1="12" y1="8" x2="12.01" y2="8"/></svg>';
    line.innerHTML = icon + escHtml(text.slice(0, 120)) + (text.length > 120 ? '\u2026' : '');

    const detail = document.createElement('div');
    detail.className = 'live-tool-detail';
    detail.innerHTML = '<pre style="white-space:pre-wrap;margin:0;color:' + (isErr ? 'var(--result-err)' : 'var(--text-muted)') + ';">' + escHtml(text) + '</pre>';

    line.onclick = () => detail.classList.toggle('open');
    div.appendChild(line);
    div.appendChild(detail);

  } else if (e.kind === 'stream') {
    // Streaming partial text — render as assistant fragment
    div.className = 'msg assistant';
    const text = e.text || '';
    div.innerHTML = '<div class="msg-role">claude <span class="msg-time" style="color:var(--text-faint);font-size:10px;">streaming\u2026</span></div>' +
      '<div class="msg-body msg-content">' + mdParse(text) + '</div>';

  } else if (e.kind === 'permission') {
    // Auto-approval audit log entry
    const text = e.text || '';
    const isErr = !!e.is_error;
    div.className = 'live-entry live-entry-permission';
    const shieldIcon = isErr
      ? '<svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="var(--result-err)" stroke-width="2" stroke-linecap="round" style="vertical-align:middle;margin-right:4px;"><path d="M12 22s8-4 8-10V5l-8-3-8 3v7c0 6 8 10 8 10z"/><line x1="12" y1="9" x2="12" y2="15"/><line x1="12" y1="17" x2="12.01" y2="17"/></svg>'
      : '<svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="var(--text-faint)" stroke-width="2" stroke-linecap="round" style="vertical-align:middle;margin-right:4px;"><path d="M12 22s8-4 8-10V5l-8-3-8 3v7c0 6 8 10 8 10z"/></svg>';
    const line = document.createElement('div');
    line.className = isErr ? 'live-result-line live-result-err' : 'live-result-line';
    line.style.cursor = 'pointer';
    line.style.fontSize = '11px';
    if (!isErr) line.style.color = 'var(--text-faint)';
    line.innerHTML = shieldIcon + escHtml(text.split('\n')[0]);

    const detail = document.createElement('div');
    detail.className = 'live-tool-detail';
    detail.innerHTML = '<pre style="white-space:pre-wrap;margin:0;font-size:11px;color:' + (isErr ? 'var(--result-err)' : 'var(--text-faint)') + ';">' + escHtml(text) + '</pre>';

    line.onclick = () => detail.classList.toggle('open');
    div.appendChild(line);
    div.appendChild(detail);

  } else if (e.kind === 'directive_conflict') {
    // Directive conflict resolution card — surfaced when an ambiguous
    // conflict is detected between two user directives.
    div.className = 'live-entry live-directive-conflict';
    const c = e.conflict || {};
    const newDir = e.new_directive || {};
    const existDir = e.existing_directive || {};
    const projectId = e.project_id || '';
    // Use backend conflict id for the card element; fall back to composite id
    const backendConflictId = c.id || '';
    const conflictElId = 'dc-' + (backendConflictId || (newDir.id || '') + '-' + (existDir.id || ''));

    const newTime = newDir.time || newDir.created_at ? _formatMsgTime(newDir.time || newDir.created_at) : '';
    const existTime = existDir.time || existDir.created_at ? _formatMsgTime(existDir.time || existDir.created_at) : '';
    const newScope = newDir.scope || newDir.said_to || 'unscoped';
    const existScope = existDir.scope || existDir.said_to || 'unscoped';

    // Escape for safe use inside single-quoted JS strings in onclick attrs
    const _a = _escJsAttr;
    const aProjId = _a(projectId), aBackendCid = _a(backendConflictId);
    const aCid = _a(conflictElId);

    div.id = conflictElId;
    // Store backend conflict_id as a data attribute for resolution lookups
    div.dataset.conflictId = backendConflictId;
    div.innerHTML =
      '<div class="dc-header">' +
        '<svg class="dc-icon" width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round">' +
          '<path d="M10.29 3.86L1.82 18a2 2 0 0 0 1.71 3h16.94a2 2 0 0 0 1.71-3L13.71 3.86a2 2 0 0 0-3.42 0z"/>' +
          '<line x1="12" y1="9" x2="12" y2="13"/><line x1="12" y1="17" x2="12.01" y2="17"/>' +
        '</svg>' +
        '<span class="dc-title">Directive Conflict</span>' +
      '</div>' +
      '<div class="dc-body">' +
        '<div class="dc-directive dc-directive-old">' +
          '<div class="dc-dir-meta">' +
            '<span class="dc-dir-id">' + escHtml(existDir.id || '?') + '</span>' +
            '<span class="dc-dir-scope">' + escHtml(existScope) + '</span>' +
            (existTime ? '<span class="dc-dir-time">' + existTime + '</span>' : '') +
          '</div>' +
          '<div class="dc-dir-text">' + escHtml(existDir.directive || existDir.content || '') + '</div>' +
        '</div>' +
        '<div class="dc-vs">vs</div>' +
        '<div class="dc-directive dc-directive-new">' +
          '<div class="dc-dir-meta">' +
            '<span class="dc-dir-id">' + escHtml(newDir.id || '?') + '</span>' +
            '<span class="dc-dir-scope">' + escHtml(newScope) + '</span>' +
            (newTime ? '<span class="dc-dir-time">' + newTime + '</span>' : '') +
          '</div>' +
          '<div class="dc-dir-text">' + escHtml(newDir.directive || newDir.content || '') + '</div>' +
        '</div>' +
        (c.recommendation ? '<div class="dc-reason">' + escHtml(c.recommendation) + '</div>' : '') +
        (c.reason ? '<div class="dc-reason">' + escHtml(c.reason) + '</div>' : '') +
      '</div>' +
      '<div class="dc-actions" id="' + escHtml(conflictElId) + '-actions">' +
        '<button class="dc-btn dc-btn-supersede" title="New directive (B) replaces old (A)"' +
          ' onclick="_resolveDirectiveConflict(\'' + aProjId + '\',\'' + aBackendCid + '\',\'supersede\',\'' + aCid + '\')">Supersede</button>' +
        '<button class="dc-btn dc-btn-scope" title="Both apply to different sections"' +
          ' onclick="_resolveDirectiveConflict(\'' + aProjId + '\',\'' + aBackendCid + '\',\'scope\',\'' + aCid + '\')">Scope</button>' +
        '<button class="dc-btn dc-btn-keep" title="Keep both active"' +
          ' onclick="_resolveDirectiveConflict(\'' + aProjId + '\',\'' + aBackendCid + '\',\'keep_both\',\'' + aCid + '\')">Keep Both</button>' +
      '</div>' +
      '<div class="dc-freeform" id="' + escHtml(conflictElId) + '-freeform">' +
        '<input type="text" class="dc-freeform-input" placeholder="Or describe how to resolve\u2026"' +
          ' onkeydown="if(event.key===\'Enter\'){event.preventDefault();_resolveDirectiveConflictFreeform(\'' + aProjId + '\',\'' + aBackendCid + '\',this.value,\'' + aCid + '\');}">' +
      '</div>';
  }

  return div;
}

// ═══════════════════════════════════════════════════════════════
// DIRECTIVE CONFLICT RESOLUTION
// ═══════════════════════════════════════════════════════════════

/** Escape a string for safe embedding inside a single-quoted JS string in an
 *  onclick attribute.  Handles ', ", &, <, >, and backslash. */
function _escJsAttr(s) {
  if (!s) return '';
  return String(s)
    .replace(/\\/g, '\\\\')
    .replace(/'/g, '\\&#39;')
    .replace(/"/g, '&quot;')
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;');
}

/**
 * Resolve a directive conflict via one-click button (supersede/scope/keep_both).
 * Sends { conflict_id, action } to match the backend contract.
 */
async function _resolveDirectiveConflict(projectId, conflictId, action, conflictElId) {
  const card = document.getElementById(conflictElId);
  const actionsEl = card && card.querySelector('.dc-actions');
  const freeformEl = card && card.querySelector('.dc-freeform');
  // Save original buttons HTML so we can restore on error
  const _savedActions = actionsEl ? actionsEl.innerHTML : '';
  if (actionsEl) actionsEl.innerHTML = '<span class="dc-resolving">Resolving\u2026</span>';
  if (freeformEl) freeformEl.style.display = 'none';

  const body = {
    conflict_id: conflictId,
    action: action,
  };

  try {
    const res = await fetch('/api/compose/projects/' + encodeURIComponent(projectId) + '/directives/resolve', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
    });
    const data = await res.json();
    if (!res.ok) throw new Error(data.error || 'Resolution failed');
    _markConflictResolved(conflictElId, action, data);
  } catch (err) {
    console.error('[directive-conflict] Resolution error:', err);
    // Restore buttons so user can retry
    if (actionsEl) actionsEl.innerHTML = '<span class="dc-error">Failed: ' + escHtml(err.message) + ' </span>' + _savedActions;
    if (freeformEl) freeformEl.style.display = '';
  }
}

/**
 * Resolve a directive conflict via free-form text input.
 * Interprets the text to determine action, then sends { conflict_id, action }
 * to match the backend contract.
 */
async function _resolveDirectiveConflictFreeform(projectId, conflictId, text, conflictElId) {
  if (!text || !text.trim()) return;
  const card = document.getElementById(conflictElId);
  const actionsEl = card && card.querySelector('.dc-actions');
  const freeformEl = card && card.querySelector('.dc-freeform');
  const _savedActions = actionsEl ? actionsEl.innerHTML : '';
  if (actionsEl) actionsEl.innerHTML = '<span class="dc-resolving">Resolving\u2026</span>';
  if (freeformEl) freeformEl.style.display = 'none';

  // Heuristic: detect intent from text using word boundaries to reduce false positives
  const lower = text.toLowerCase().trim();
  let action = 'supersede';  // default
  if (/\bkeep both\b|\bboth\b.*\bvalid\b|\bintentionally different\b|\bintentional\b/i.test(lower)) action = 'keep_both';
  else if (/\bscope\b|\bseparate\b|\beach section\b|\btheir own\b/i.test(lower)) action = 'scope';

  try {
    const res = await fetch('/api/compose/projects/' + encodeURIComponent(projectId) + '/directives/resolve', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        conflict_id: conflictId,
        action: action,
      }),
    });
    const data = await res.json();
    if (!res.ok) throw new Error(data.error || 'Resolution failed');
    _markConflictResolved(conflictElId, action, data);
  } catch (err) {
    console.error('[directive-conflict] Freeform resolution error:', err);
    if (actionsEl) actionsEl.innerHTML = '<span class="dc-error">Failed: ' + escHtml(err.message) + ' </span>' + _savedActions;
    if (freeformEl) freeformEl.style.display = '';
  }
}

/**
 * Update the conflict card UI after successful resolution.
 */
function _markConflictResolved(conflictElId, resolution, data) {
  const card = document.getElementById(conflictElId);
  if (!card) return;
  card.classList.add('dc-resolved');

  const labels = {
    supersede: 'Superseded',
    scope: 'Scoped separately',
    keep_both: 'Kept both',
  };
  const label = labels[resolution] || resolution;
  const note = data.resolution_directive
    ? data.resolution_directive.directive || ''
    : '';

  const actionsEl = card.querySelector('.dc-actions');
  if (actionsEl) {
    actionsEl.innerHTML =
      '<div class="dc-resolved-badge">' +
        '<svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round"><polyline points="20 6 9 17 4 12"/></svg> ' +
        '<span>' + escHtml(label) + '</span>' +
      '</div>' +
      (note ? '<div class="dc-resolved-note">' + escHtml(note) + '</div>' : '');
  }
  const freeformEl = card.querySelector('.dc-freeform');
  if (freeformEl) freeformEl.remove();
}

/**
 * Inject a directive conflict card into the live chat log.
 * Called from socket event handler when compose_directive_logged fires
 * with ambiguous conflicts.
 */
function _injectDirectiveConflict(data) {
  const logEl = document.getElementById('live-log');
  if (!logEl) return;

  const directive = data.directive || {};
  const conflicts = data.conflicts || [];

  // Only surface ambiguous conflicts — global/contextual are auto-resolved
  const ambiguous = conflicts.filter(c => c.classification === 'ambiguous');
  if (!ambiguous.length) return;

  for (const conflict of ambiguous) {
    // Build a synthetic entry for the conflict card renderer.
    // Map backend ComposeConflict fields to what renderLiveEntry expects.
    const existingStub = {
      id: conflict.existing_id || conflict.directive_a_id || '',
      directive: conflict.existing_text || conflict.directive_a_content || '',
      content: conflict.existing_text || conflict.directive_a_content || '',
      time: conflict.existing_time || '',
      scope: conflict.existing_scope || null,
    };

    const entry = {
      kind: 'directive_conflict',
      project_id: data.project_id,
      new_directive: directive,
      existing_directive: existingStub,
      conflict: conflict,
    };

    const el = renderLiveEntry(entry);
    logEl.appendChild(el);

    // Auto-scroll if enabled
    if (liveAutoScroll) {
      logEl.scrollTop = logEl.scrollHeight;
    }
  }
}

/** Build a compact context circle indicator. Clickable to trigger compact when not working. */
function _buildCtxBarCompact(id, disabled) {
  const _usage = (window._sessionUsage && window._sessionUsage[id]) || null;
  if (!_usage) return '';
  const _ctxWindow = 200000;
  let _tokens = (_usage.input_tokens || 0)
    + (_usage.cache_read_input_tokens || 0)
    + (_usage.cache_creation_input_tokens || 0);
  if (_tokens <= 0 || _tokens > _ctxWindow * 1.5) return '';
  const _pct = Math.min(100, Math.round((_tokens / _ctxWindow) * 100));
  const _color = _pct >= 90 ? 'var(--result-err)' : _pct >= 70 ? '#ffb700' : 'var(--accent)';
  // SVG circle: radius=7, circumference=~43.98
  const _circ = 2 * Math.PI * 7;
  const _filled = _circ * (_pct / 100);
  const _cls = disabled ? 'ctx-circle ctx-disabled' : 'ctx-circle ctx-clickable';
  const _click = disabled ? '' : ' onclick="liveCompact()"';
  const _title = disabled ? 'Context ' + _pct + '%' : 'Context ' + _pct + '% — click to compact';
  return '<div class="' + _cls + '"' + _click + ' title="' + _title + '">' +
    '<svg width="18" height="18" viewBox="0 0 18 18">' +
    '<circle cx="9" cy="9" r="7" fill="none" stroke="var(--border-subtle)" stroke-width="2"/>' +
    '<circle cx="9" cy="9" r="7" fill="none" stroke="' + _color + '" stroke-width="2"' +
    ' stroke-dasharray="' + _filled.toFixed(2) + ' ' + _circ.toFixed(2) + '"' +
    ' stroke-linecap="round" transform="rotate(-90 9 9)"/>' +
    '</svg></div>';
}

function updateLiveInputBar() {
  if (!liveSessionId) return;
  const id = liveSessionId;
  const bar = document.getElementById('live-input-bar');
  if (!bar) return;

  // Track whether focus was inside this bar before re-render so we can
  // restore it synchronously after innerHTML replacement (avoids focus flash).
  const _barHadFocus = bar.contains(document.activeElement);

  // Don't touch the bar for sessions that haven't started on the server yet.
  // addNewAgent() renders its own input bar with _newSessionSubmit handler.
  // If neither runningIds nor sessionKinds know about this session, leave it alone.
  const isRunning = runningIds.has(id);
  const kind = sessionKinds[id];  // 'question' | 'working' | 'idle' | undefined
  if (!isRunning && !kind && guiOpenSessions.has(id)) return;

  // Capture any text the user has typed in either textarea so we can
  // preserve it across state transitions (e.g. idle → working, working → idle).
  // This replaces the old early-return that blocked state transitions entirely.
  const _existingTa = bar.querySelector('#live-input-ta') || bar.querySelector('#live-queue-ta');
  const _preservedText = _existingTa ? _existingTa.value : '';
  const wd = waitingData[id];     // {question, options, kind} or undefined

  // Compute a state key — for question state, include question text so we re-render if the question changed
  let stateKey;
  if (!isRunning) stateKey = 'ended';
  else if (kind === 'question') stateKey = 'question:' + (wd ? wd.question || '' : '');
  else if (kind === 'idle') stateKey = 'idle';
  else {
    // Include sub-agent count + status in state key so bar re-renders when agents change
    const _sa = (window._subAgents && window._subAgents[id]) || {};
    const _saKey = Object.values(_sa).map(a => a.status).join(',');
    stateKey = 'working:' + _getQueueList(id).length + ':sa:' + _saKey;
  }

  // Reset working timer when leaving working state — but only if we
  // have a definitive non-working state (not just 'ended' from missing data)
  if (!stateKey.startsWith('working') && kind) {
    _liveWorkingStart = null;
    if (_liveWorkingTimer) { clearInterval(_liveWorkingTimer); _liveWorkingTimer = null; }
  }

  // Don't re-render if the bar is already showing this exact state.
  // This is critical: prevents wiping user's in-progress typed text.
  if (stateKey === liveBarState) return;

  // Don't rebuild bar while voice is actively recording inside it — innerHTML
  // would destroy the textarea the recognition targets, cutting the user off.
  // The bar catches up via deferred updateLiveInputBar() call in voice onend.
  if (typeof _activeRecognition !== 'undefined' && _activeRecognition &&
      _activeRecognition._target && bar.contains(_activeRecognition._target)) {
    return;  // liveBarState NOT updated — so deferred call will still see a mismatch
  }

  const wasTransition = liveBarState !== null;  // true if switching between states
  liveBarState = stateKey;

  // Animate the bar content when switching between states (not on initial render)
  if (wasTransition) {
    bar.classList.add('bar-transitioning');
    setTimeout(() => bar.classList.remove('bar-transitioning'), 350);
  }

  if (!isRunning) {
    bar.innerHTML =
      '<div style="display:flex;align-items:center;gap:8px;margin-bottom:8px;padding:8px 12px;background:var(--bg-card);border:1px solid var(--border-subtle);border-radius:8px;">' +
      '<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="var(--text-faint)" stroke-width="2" stroke-linecap="round"><circle cx="12" cy="12" r="10"/><line x1="8" y1="12" x2="16" y2="12"/></svg>' +
      '<span style="font-size:12px;color:var(--text-muted);">Session not running. Type a message to resume.</span>' +
      '</div>' +
      '<textarea id="live-input-ta" class="live-textarea" rows="2" placeholder="Type a message to continue\u2026"' +
      ' onkeydown="if(_shouldSend(event)){event.preventDefault();liveSubmitContinue(\'' + id + '\')}"></textarea>' +
      '<div class="live-bar-row">' +
      _buildCtxBarCompact(id) +
      '<span class="send-hint" style="font-size:10px;color:var(--text-faint);">' + _sendHint() + '</span>' +
      '<button class="live-send-btn" id="live-voice-btn"></button>' +
      '</div>';
    const btnClose = document.getElementById('btn-close');
    if (btnClose) btnClose.disabled = true;
    _guiFocusPending = false;
    setupVoiceButton(document.getElementById('live-input-ta'), document.getElementById('live-voice-btn'), () => liveSubmitContinue(id));
    if (_barHadFocus) { const ta = document.getElementById('live-input-ta'); if (ta) ta.focus(); }
    setTimeout(() => {
      const logEl = document.getElementById('live-log');
      if (logEl) logEl.scrollTop = logEl.scrollHeight;
      const ta = document.getElementById('live-input-ta');
      if (ta) { if (!_barHadFocus) ta.focus(); _initAutoResize(ta); }
    }, 50);

  } else if (kind === 'question') {
    // Claude is asking something — show question text + option buttons + free-form textarea
    // Queue is managed server-side; never pop queue items into permission responses
    _renderQueueBanner();
    const questionText = (wd && wd.question) ? wd.question : '';
    const options = (wd && wd.options) ? wd.options : null;

    // Render question bubble
    let questionHTML = '';
    if (questionText) {
      // Escape and show last ~400 chars of question (truncate top if very long)
      const display = questionText.length > 400 ? '\u2026' + questionText.slice(-400) : questionText;
      questionHTML = '<div class="live-question-text">' + mdParse(display) + '</div>';
    }

    // Render option buttons (y/n/a, yes/no, or numbered list)
    const isTool = (wd && wd.kind === 'tool');
    const _chk = '<svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" style="vertical-align:middle;"><polyline points="20 6 9 17 4 12"/></svg>';
    const _xic = '<svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" style="vertical-align:middle;"><line x1="18" y1="6" x2="6" y2="18"/><line x1="6" y1="6" x2="18" y2="18"/></svg>';
    const _star = '<svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" style="vertical-align:middle;"><polygon points="12 2 15.09 8.26 22 9.27 17 14.14 18.18 21.02 12 17.77 5.82 21.02 7 14.14 2 9.27 8.91 8.26 12 2"/></svg>';
    const _shield = '<svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" style="vertical-align:middle;"><path d="M12 22s8-4 8-10V5l-8-3-8 3v7c0 6 8 10 8 10z"/></svg>';
    const optLabels = { y: _chk + ' Yes', n: _xic + ' No', aa: _shield + ' Auto Most', a: _star + ' Always', yes: _chk + ' Yes', no: _xic + ' No' };
    let optBtns = '';
    if (options && options.length) {
      optBtns = '<div class="live-option-btns">' +
        options.map((opt) => {
          // Numbered option: "1. Do X" -> label = "1. Do X", send = "1"
          // Single token option: "y" / "n" / "yes" / "no" / "a" -> expand label for tool prompts
          const isNumbered = /^\d+\./.test(opt);
          const sendVal = isNumbered ? opt.match(/^(\d+)\./)[1] : opt;
          const label = (!isNumbered && isTool && optLabels[opt.toLowerCase()])
            ? optLabels[opt.toLowerCase()] : escHtml(opt);
          const safeVal = sendVal.replace(/\\/g,'\\\\').replace(/'/g,"\\'");
          return '<button class="live-opt-btn" onclick="livePickOption(\'' + safeVal + '\')">' + label + '</button>';
        }).join('') +
      '</div>';
    }

    bar.innerHTML =
      '<div class="live-waiting-label"><svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" style="vertical-align:middle;margin-right:4px;"><path d="M21 15a2 2 0 0 1-2 2H7l-4 4V5a2 2 0 0 1 2-2h14a2 2 0 0 1 2 2z"/></svg> Claude has a question</div>' +
      questionHTML +
      optBtns +
      '<textarea id="live-input-ta" class="live-textarea waiting-focus" rows="2" placeholder="Type your response\u2026 (or click an option above)"' +
      ' onkeydown="if(_shouldSend(event)){event.preventDefault();liveSubmitWaiting()}"></textarea>' +
      '<div class="live-bar-row">' +
      _buildCtxBarCompact(id) +
      '<span class="send-hint" style="font-size:10px;color:var(--text-faint);">' + _sendHint() + '</span>' +
      '<button class="live-send-btn waiting" id="live-voice-btn"></button>' +
      '</div>';
    setupVoiceButton(document.getElementById('live-input-ta'), document.getElementById('live-voice-btn'), liveSubmitWaiting);
    const ta = document.getElementById('live-input-ta');
    if (ta) {
      _guiFocusPending = false;
      if (_barHadFocus) ta.focus();
      _initAutoResize(ta);
      setTimeout(() => {
        const logEl = document.getElementById('live-log');
        if (logEl) logEl.scrollTop = logEl.scrollHeight;
        if (!_barHadFocus) ta.focus();
      }, 50);
    }

  } else if (kind === 'idle') {
    bar.innerHTML =
      '<textarea id="live-input-ta" class="live-textarea" rows="2" placeholder="Type your next command\u2026"' +
      ' onkeydown="if(_shouldSend(event)){event.preventDefault();liveSubmitIdle()}"></textarea>' +
      '<div class="live-bar-row">' +
      _buildCtxBarCompact(id) +
      '<span class="send-hint" style="font-size:10px;color:var(--text-faint);">' + _sendHint() + '</span>' +
      '<button class="live-send-btn" id="live-voice-btn"></button>' +
      '</div>';
    setupVoiceButton(document.getElementById('live-input-ta'), document.getElementById('live-voice-btn'), liveSubmitIdle);
    _guiFocusPending = false;
    if (_barHadFocus) { const ta = document.getElementById('live-input-ta'); if (ta) ta.focus(); }
    setTimeout(() => {
      const logEl = document.getElementById('live-log');
      if (logEl) logEl.scrollTop = logEl.scrollHeight;
      const ta = document.getElementById('live-input-ta');
      if (ta) { if (!_barHadFocus) ta.focus(); _initAutoResize(ta); }
    }, 50);

  } else {
    // Working state — banner is rendered separately in #live-queue-area
    if (!_liveWorkingStart) _liveWorkingStart = Date.now();
    const _elapsed = Math.round((Date.now() - _liveWorkingStart) / 1000);
    const _elapsedStr = _elapsed >= 60 ? Math.floor(_elapsed/60) + 'm ' + (_elapsed%60) + 's' : _elapsed + 's';
    const qCount = _getQueueList(id).length;

    // Detect compacting substatus
    const _sub = (window._sessionSubstatus && window._sessionSubstatus[id]) || '';
    const _isCompacting = _sub === 'compacting';
    const _statusLabel = _isCompacting ? 'Compacting\u2026' : 'Working\u2026';
    const _spinnerClass = _isCompacting ? 'spinner compacting-spinner' : 'spinner';

    // Build sub-agent team strip
    const _agents = (window._subAgents && window._subAgents[id]) || {};
    const _agentEntries = Object.entries(_agents);
    const _workingCount = _agentEntries.length > 0 ? _agentEntries.filter(([, a]) => a.status === 'working').length : 0;
    const _doneCount = _agentEntries.length > 0 ? _agentEntries.filter(([, a]) => a.status === 'done').length : 0;
    const _allDone = _agentEntries.length > 0 && _workingCount === 0;
    let _agentStripHtml = '';
    let _agentFooterHtml = '';

    if (_agentEntries.length > 0) {
      if (_allDone) {
        // All agents finished — collapse to minimal footer inside the working bar
        _agentFooterHtml = '<div class="sub-agent-footer">' +
          '<svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round"><polyline points="20 6 9 17 4 12"/></svg>' +
          '<span>' + _doneCount + ' agent' + (_doneCount > 1 ? 's' : '') + ' completed</span>' +
          '</div>';
      } else {
        // Some agents still working — show full strip with pills
        const _teamLabel = _workingCount + ' agent' + (_workingCount > 1 ? 's' : '') + ' active' + (_doneCount > 0 ? ' \u00b7 ' + _doneCount + ' done' : '');
        _agentStripHtml = '<div class="sub-agent-strip">' +
          '<div class="sub-agent-header">' +
          '<svg class="sub-agent-team-icon" width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><path d="M17 21v-2a4 4 0 0 0-4-4H5a4 4 0 0 0-4 4v2"/><circle cx="9" cy="7" r="4"/><path d="M23 21v-2a4 4 0 0 0-3-3.87"/><path d="M16 3.13a4 4 0 0 1 0 7.75"/></svg>' +
          '<span class="sub-agent-team-label">' + _teamLabel + '</span>' +
          '</div>' +
          '<div class="sub-agent-pills">';
        _agentEntries.forEach(([tuId, ag], idx) => {
          const isDone = ag.status === 'done';
          const agentElapsed = isDone && ag.endTime
            ? Math.round((ag.endTime - ag.startTime) / 1000)
            : Math.round((Date.now() - ag.startTime) / 1000);
          const agentTimeStr = agentElapsed >= 60 ? Math.floor(agentElapsed/60) + 'm ' + (agentElapsed%60) + 's' : agentElapsed + 's';
          const statusIcon = isDone
            ? '<svg class="sub-agent-check" width="11" height="11" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="3" stroke-linecap="round"><polyline points="20 6 9 17 4 12"/></svg>'
            : '<span class="sub-agent-spinner"></span>';
          const pillClass = 'sub-agent-pill' + (isDone ? ' done' : ' active');
          _agentStripHtml += '<div class="' + pillClass + '" style="animation-delay:' + (idx * 0.06) + 's" data-agent-id="' + escHtml(tuId) + '">' +
            statusIcon +
            '<span class="sub-agent-label">' + escHtml(ag.desc || 'Agent') + '</span>' +
            '<span class="sub-agent-time">' + agentTimeStr + '</span>' +
            '</div>';
        });
        _agentStripHtml += '</div></div>';
      }
    }

    const _hasActiveAgents = _agentEntries.length > 0 && !_allDone;
    bar.innerHTML =
      '<div class="live-working-status' + (_hasActiveAgents ? ' has-agents' : '') + '">' +
      '<div class="live-working-indicator"><span class="' + _spinnerClass + '"></span> ' + _statusLabel + ' <span id="live-elapsed" style="color:var(--text-faint);font-size:10px;margin-left:6px;">' + _elapsedStr + '</span></div>' +
      (_agentFooterHtml ? _agentFooterHtml : '') +
      '<button class="live-stop-btn" onclick="liveSubmitInterrupt()" title="Interrupt session">\u25A0 Stop</button>' +
      '</div>' +
      _agentStripHtml +
      '<textarea id="live-queue-ta" class="live-textarea live-queue-ta" rows="2" ' +
      'placeholder="' + (qCount ? 'Queue another command\u2026' : 'Type your next command \u2014 will send when Claude finishes\u2026') + '"' +
      ' onkeydown="if(_shouldSend(event)){event.preventDefault();liveQueueSave()}"></textarea>' +
      '<div class="live-bar-row">' +
      _buildCtxBarCompact(id, true) +
      '<span id="live-queue-hint" style="font-size:10px;color:var(--text-faint);">' +
      (qCount ? qCount + ' queued \u2022 will send in order when idle' : 'Will send automatically when done') +
      '</span>' +
      '<button class="live-send-btn" id="live-voice-btn"></button>' +
      '</div>';
    setupVoiceButton(document.getElementById('live-queue-ta'), document.getElementById('live-voice-btn'), liveQueueSave);
    if (_barHadFocus) { const ta = document.getElementById('live-queue-ta'); if (ta) ta.focus(); }
    setTimeout(() => {
      const ta = document.getElementById('live-queue-ta');
      if (ta) { if (!_barHadFocus) ta.focus(); _initAutoResize(ta); }
    }, 50);
    _renderQueueBanner();
    // Start elapsed timer — update only the time span, NOT the whole bar
    // Update elapsed timer and compacting label dynamically (no ctx bar in working state)
    if (!_liveWorkingTimer) {
      _liveWorkingTimer = setInterval(() => {
        if (!liveBarState || !liveBarState.startsWith('working') || !_liveWorkingStart) return;
        const el = document.getElementById('live-elapsed');
        if (!el) return;
        const s = Math.round((Date.now() - _liveWorkingStart) / 1000);
        el.textContent = s >= 60 ? Math.floor(s/60) + 'm ' + (s%60) + 's' : s + 's';
        // Update compacting label dynamically without full re-render
        const indicator = el.closest('.live-working-indicator');
        if (indicator) {
          const curSub = (window._sessionSubstatus && window._sessionSubstatus[liveSessionId]) || '';
          const spinnerEl = indicator.querySelector('.spinner');
          if (curSub === 'compacting') {
            if (spinnerEl && !spinnerEl.classList.contains('compacting-spinner')) spinnerEl.classList.add('compacting-spinner');
            const textNodes = [...indicator.childNodes].filter(n => n.nodeType === 3);
            if (textNodes.length && !textNodes[0].textContent.includes('Compacting')) textNodes[0].textContent = ' Compacting\u2026 ';
          } else {
            if (spinnerEl) spinnerEl.classList.remove('compacting-spinner');
            const textNodes = [...indicator.childNodes].filter(n => n.nodeType === 3);
            if (textNodes.length && textNodes[0].textContent.includes('Compacting')) textNodes[0].textContent = ' Working\u2026 ';
          }
        }
        // Update sub-agent elapsed times without full re-render
        const agentPills = document.querySelectorAll('.sub-agent-pill.active');
        if (agentPills.length) {
          const agents = (window._subAgents && window._subAgents[liveSessionId]) || {};
          agentPills.forEach(pill => {
            const agId = pill.dataset.agentId;
            const ag = agents[agId];
            if (ag && ag.status === 'working') {
              const timeEl = pill.querySelector('.sub-agent-time');
              if (timeEl) {
                const elapsed = Math.round((Date.now() - ag.startTime) / 1000);
                timeEl.textContent = elapsed >= 60 ? Math.floor(elapsed/60) + 'm ' + (elapsed%60) + 's' : elapsed + 's';
              }
            }
          });
        }
      }, 1000);
    }
  }

  // ── Restore text: same-session state transition OR saved draft ──
  const _restoreText = _preservedText || _getDraft(id);
  if (_restoreText) {
    const _newTa = document.getElementById('live-input-ta') || document.getElementById('live-queue-ta');
    if (_newTa && !_newTa.value) {
      _newTa.value = _restoreText;
      _autoResizeTextarea(_newTa);
      _newTa.dispatchEvent(new Event('input'));
    }
  }
}

function livePickOption(val) {
  if (!liveSessionId) return;
  socket.emit('permission_response', {session_id: liveSessionId, action: val});
  // Optimistic: clear waiting state locally
  delete waitingData[liveSessionId];
  sessionKinds[liveSessionId] = 'working';
  liveBarState = null;
  updateLiveInputBar();
}

function liveQueueSave() {
  const ta = document.getElementById('live-queue-ta');
  if (!ta || !liveSessionId) return;
  const text = ta.value.trim();
  if (!text) return;

  // If the session went idle while we were recording (e.g. voice dictation
  // started during 'working' but Claude finished before we stopped talking),
  // send directly instead of queuing — it would just sit in the queue until
  // the next idle transition otherwise.
  const kind = sessionKinds[liveSessionId];
  if (kind === 'idle' || kind === 'question') {
    ta.value = '';
    _resetTextareaHeight(ta);
    _liveSubmitDirect(liveSessionId, text);
    return;
  }

  _addQueue(liveSessionId, text);
  ta.value = '';
  _resetTextareaHeight(ta);
  ta.placeholder = 'Queue another command\u2026';
  const total = _getQueueList(liveSessionId).length;
  showToast('Command queued (' + total + ')');
  _renderQueueBanner();
  const hint = document.getElementById('live-queue-hint');
  if (hint) hint.textContent = total + ' queued \u2022 will send in order when idle';
  // Scroll chat to bottom so banner is visible
  const logEl = document.getElementById('live-log');
  if (logEl) logEl.scrollTop = logEl.scrollHeight;
  // Keep focus on textarea
  ta.focus();
}

function liveClearQueue() {
  if (!liveSessionId) return;
  const list = _getQueueList(liveSessionId);
  if (!list.length) return;
  _removeQueueAt(liveSessionId, _queueViewIndex);
  const remaining = _getQueueList(liveSessionId).length;
  showToast(remaining ? 'Removed \u2014 ' + remaining + ' remaining' : 'Queue cleared');
  _renderQueueBanner();
  const ta = document.getElementById('live-queue-ta');
  if (ta) ta.placeholder = remaining ? 'Queue another command\u2026' : 'Type your next command \u2014 will send when Claude finishes\u2026';
  const hint = document.getElementById('live-queue-hint');
  if (hint) hint.textContent = remaining ? remaining + ' queued \u2022 will send in order when idle' : 'Will send automatically when done';
  if (ta) ta.focus();
}

function liveEditQueue() {
  if (!liveSessionId) return;
  const list = _getQueueList(liveSessionId);
  if (!list.length) return;
  const text = list[_queueViewIndex];
  _removeQueueAt(liveSessionId, _queueViewIndex);
  _renderQueueBanner();
  const remaining = _getQueueList(liveSessionId).length;
  const ta = document.getElementById('live-queue-ta');
  if (ta) {
    ta.value = text;
    _autoResizeTextarea(ta);
    ta.placeholder = remaining ? 'Queue another command\u2026' : 'Type your next command \u2014 will send when Claude finishes\u2026';
    ta.focus();
  }
  const hint = document.getElementById('live-queue-hint');
  if (hint) hint.textContent = remaining ? remaining + ' queued \u2022 Press Enter to re-queue' : 'Will send automatically when done';
}

function liveSubmitIdle() {
  const ta = document.getElementById('live-input-ta');
  if (!ta || !liveSessionId) return;
  const text = ta.value.trim();
  if (!text) return;
  ta.value = '';
  _resetTextareaHeight(ta);
  // Check and consume voice flag
  const wasVoice = (typeof _lastSubmitWasVoice !== 'undefined' && _lastSubmitWasVoice);
  if (wasVoice) _lastSubmitWasVoice = false;
  _liveSubmitDirect(liveSessionId, text, wasVoice ? {voice: true} : undefined);
}

function _liveSubmitDirect(sid, text, opts) {
  if (!sid) return;
  _liveSending = true;
  _clearDraft(sid);
  const _isVoice = opts && opts.voice;

  // Clear stale client-side queue — message is being sent directly
  delete _sessionQueues[sid];
  _renderQueueBanner();

  // Only treat as permission response if explicitly flagged (not from queue)
  const isPermission = opts && opts.isPermission;
  const wasPermission = isPermission && !!waitingData[sid];
  if (wasPermission) {
    const actionMap = {yes: 'y', no: 'n', always: 'a', 'almost always': 'aa', 'almost-always': 'aa', 'almostalways': 'aa', allow: 'y', deny: 'n'};
    const action = actionMap[text.toLowerCase()] || text;
    socket.emit('permission_response', {session_id: sid, action: action});
    // Optimistic clear
    delete waitingData[sid];
    sessionKinds[sid] = 'working';
  } else if (runningIds.has(sid)) {
    // Send message — server will process if idle, or queue if busy
    socket.emit('send_message', {session_id: sid, text: text, voice: _isVoice || undefined});
    // Ghost session safety net: if no session_state event arrives within 3s,
    // the daemon has lost this session. Fall back to start_session to recover.
    const _ghostTimer = setTimeout(() => {
      socket.off('session_state', _ghostCancel);
      if (liveSessionId !== sid) return;
      console.warn('[submit] No daemon response for', sid, 'after 3s — ghost recovery: close then restart');
      // Close the zombie session first, then restart after a short delay
      socket.emit('close_session', {session_id: sid});
      runningIds.delete(sid);
      delete sessionKinds[sid];
      setTimeout(() => {
        if (liveSessionId !== sid) return;
        socket.emit('start_session', {
          session_id: sid,
          prompt: text,
          cwd: (typeof _currentProjectDir === 'function') ? _currentProjectDir() : '',
          resume: true,
        });
        runningIds.add(sid);
        sessionKinds[sid] = 'working';
        updateLiveInputBar();
      }, 1500);
    }, 3000);
    const _ghostCancel = function(data) {
      if (data && data.session_id === sid && data.state !== 'stopped') {
        clearTimeout(_ghostTimer);
        socket.off('session_state', _ghostCancel);
      }
    };
    socket.on('session_state', _ghostCancel);
  } else {
    // Session not in runningIds — try send_message first (it handles
    // IDLE/WORKING/WAITING gracefully with auto-queue), then fall back to
    // start_session only if the session truly doesn't exist on the backend.
    // This prevents the "Session already running" error when the frontend's
    // runningIds is stale (missed WebSocket event) but the backend still has
    // the session alive.
    console.warn('[submit] Session', sid, 'not in runningIds — trying send_message first');
    // Install a one-shot error listener: if send_message fails because the
    // session doesn't exist or is stopped, fall back to start_session.
    const _fallbackHandler = function(err) {
      if (err && err.session_id === sid && /not found|is stopped/i.test(err.message || '')) {
        socket.off('error', _fallbackHandler);
        clearTimeout(_ghostTimer2);  // fallback fired — no need for ghost timer
        socket.off('session_state', _ghostCancel2);
        console.warn('[submit] send_message failed (' + err.message + ') — resuming with start_session');
        socket.emit('start_session', {
          session_id: sid,
          prompt: text,
          cwd: (typeof _currentProjectDir === 'function') ? _currentProjectDir() : '',
          resume: true,
        });
        sessionKinds[sid] = 'working';
        updateLiveInputBar();
      }
    };
    socket.on('error', _fallbackHandler);
    // Ghost safety net: if neither an error nor a session_state arrives
    // within 3s, the daemon silently dropped the message. Resume directly.
    const _ghostTimer2 = setTimeout(() => {
      socket.off('error', _fallbackHandler);
      socket.off('session_state', _ghostCancel2);
      if (liveSessionId !== sid) return;
      console.warn('[submit] No daemon response for', sid, 'after 3s (not-running path) — close then restart');
      socket.emit('close_session', {session_id: sid});
      setTimeout(() => {
        if (liveSessionId !== sid) return;
        socket.emit('start_session', {
          session_id: sid,
          prompt: text,
          cwd: (typeof _currentProjectDir === 'function') ? _currentProjectDir() : '',
          resume: true,
        });
        sessionKinds[sid] = 'working';
        updateLiveInputBar();
      }, 1500);
    }, 3000);
    const _ghostCancel2 = function(data) {
      if (data && data.session_id === sid && data.state !== 'stopped') {
        clearTimeout(_ghostTimer2);
        socket.off('session_state', _ghostCancel2);
        socket.off('error', _fallbackHandler);
      }
    };
    socket.on('session_state', _ghostCancel2);
    socket.emit('send_message', {session_id: sid, text: text});
    runningIds.add(sid);
    guiOpenAdd(sid);
  }

  // Add optimistic user bubble only for real messages (not permission answers, not slash commands)
  const _isSlash = text.trim().startsWith('/') && !text.trim().includes(' ');
  if (!wasPermission && text.length > 1 && !_isSlash) {
    _addOptimisticBubble(sid, text, _isVoice);
  }

  // Optimistically set working state and render the full working bar immediately
  if (!wasPermission) {
    sessionKinds[sid] = 'working';
  }
  liveBarState = null;
  updateLiveInputBar();

  // Start the message watchdog — if we don't get ANY response activity within
  // 10 seconds, force a state resync to unstick the UI.
  _startMessageWatchdog(sid);

  // Reset sending flag after brief delay (keeps session_entry dedup working)
  setTimeout(() => { _liveSending = false; }, 500);
}

// ── Message watchdog ──
// After sending a message, if no session_entry or session_state event arrives
// for this session within 10s, force a state resync from the server. This is
// the catch-all safety net that prevents the UI from ever getting permanently
// stuck in "working" when a response was silently lost.
//
// Escalation ladder:
//   10s — request_state_snapshot via WebSocket (fast, covers most hiccups)
//   16s — direct HTTP fetch of /api/live/state/<sid> to get ground truth
//   22s — force UI to idle if server says idle/stopped but WS event was lost
let _watchdogTimer = null;
let _watchdogSid = null;

function _startMessageWatchdog(sid) {
  // Clear any previous watchdog
  if (_watchdogTimer) clearTimeout(_watchdogTimer);
  _watchdogSid = sid;
  _watchdogTimer = setTimeout(() => {
    _watchdogTimer = null;
    if (_watchdogSid !== sid || sessionKinds[sid] !== 'working') return;

    // Tier 1: WS snapshot resync
    console.warn('[watchdog] No response activity for', sid, 'after 10s — requesting state snapshot');
    socket.emit('request_state_snapshot');

    // Tier 2: direct HTTP fetch (bypasses WS entirely)
    setTimeout(() => {
      if (sessionKinds[sid] !== 'working') return;
      console.warn('[watchdog] Still stuck after WS resync for', sid, '— fetching state via HTTP');
      _watchdogHttpCheck(sid);
    }, 6000);

    // Tier 3: last resort — fetch again and force-apply
    setTimeout(() => {
      if (sessionKinds[sid] !== 'working') return;
      console.warn('[watchdog] STILL stuck for', sid, 'after 22s — force-fetching and applying');
      _watchdogHttpCheck(sid, true);
    }, 12000);
  }, 10000);
}

function _watchdogHttpCheck(sid, forceApply) {
  fetch('/api/live/state/' + encodeURIComponent(sid))
    .then(r => r.ok ? r.json() : null)
    .then(data => {
      if (!data) return;
      const serverState = data.state;
      const serverEntryCount = data.entry_count || 0;
      console.log('[watchdog] HTTP state for', sid, '=', serverState,
        'entries:', serverEntryCount, '(UI: working, rendered:', liveLineCount, ')');
      if (serverState && serverState !== 'working') {
        // Server knows it's not working — force the UI to match
        console.warn('[watchdog] Forcing UI state to', serverState, 'for', sid);
        if (serverState === 'waiting') {
          sessionKinds[sid] = 'question';
        } else if (serverState === 'idle') {
          sessionKinds[sid] = 'idle';
        } else if (serverState === 'stopped') {
          delete sessionKinds[sid];
          runningIds.delete(sid);
        }
        if (sid === liveSessionId) {
          liveBarState = null;
          updateLiveInputBar();
          // Re-fetch entries ONLY if DOM is empty — if entries are already
          // rendered from real-time streaming, a re-fetch would wipe them
          // and re-render with pagination, slicing off the response tail.
          const _wdLog = document.getElementById('live-log');
          const _wdHasEntries = _wdLog && _wdLog.querySelectorAll('.msg').length > 0;
          if (!_wdHasEntries) {
            socket.emit('get_session_log', {session_id: sid, since: 0, project: localStorage.getItem('activeProject') || '', is_working: sessionKinds[sid] === 'working' || sessionKinds[sid] === 'question'});
          }
        }
        filterSessions();
      } else if (serverState === 'working') {
        // Server also thinks working — check for missing entries even
        // while the session is still active. If real-time events were
        // lost mid-stream, we might have fewer entries than the server.
        if (sid === liveSessionId && serverEntryCount > liveLineCount + 1) {
          console.warn('[watchdog] Entry mismatch while working: server has', serverEntryCount,
            'but frontend has', liveLineCount, '— re-fetching');
          socket.emit('get_session_log', {session_id: sid, since: 0, project: localStorage.getItem('activeProject') || '', is_working: true});
        }
        if (forceApply) {
          // Request a fresh snapshot to resync any other stale state
          socket.emit('request_state_snapshot');
        }
      }
    })
    .catch(err => console.error('[watchdog] HTTP check failed:', err));
}

// Called by socket event handlers to signal that response activity was received,
// cancelling the watchdog (everything is fine, response is flowing).
function _cancelMessageWatchdog(sid) {
  if (_watchdogTimer && _watchdogSid === sid) {
    clearTimeout(_watchdogTimer);
    _watchdogTimer = null;
    _watchdogSid = null;
  }
}

// Called by streaming event handlers (session_entry, session_usage) to signal
// that data IS flowing but the session has not completed yet.  Instead of
// cancelling the watchdog outright (which leaves no safety-net if the final
// IDLE event is silently lost), restart the timer so we keep monitoring.
function _resetMessageWatchdog(sid) {
  // Only reset if the session is still working
  if (sessionKinds[sid] !== 'working') return;
  _startMessageWatchdog(sid);
}

function _addOptimisticBubble(sid, text, isVoice) {
  if (sid !== liveSessionId) return;
  const logEl = document.getElementById('live-log');
  if (!logEl) return;

  // Clear skeleton/placeholder on first message
  const skel = logEl.querySelector('.skel-bar, .skeleton-loader, .skel-row');
  if (skel) logEl.innerHTML = '';

  // Fade out empty state if this is the first message
  const emptyEl = logEl.querySelector('.empty-state');
  if (emptyEl) {
    emptyEl.classList.add('fading-out');
    emptyEl.addEventListener('animationend', () => emptyEl.remove(), {once: true});
  }

  const now = new Date();
  const h = now.getHours() % 12 || 12;
  const timestamp = h + ':' + String(now.getMinutes()).padStart(2, '0') + ' ' + (now.getHours() >= 12 ? 'PM' : 'AM');
  const msgId = ++_optimisticMsgId;
  const userMsg = document.createElement('div');
  userMsg.className = 'msg user msg-entering optimistic-bubble';
  userMsg.dataset.optimisticId = msgId;
  userMsg.innerHTML = '<div class="msg-role">me <span class="msg-time">' + timestamp + '</span></div><div class="msg-body msg-content"><pre style="white-space:pre-wrap;margin:0;">' + escHtml(text) + '</pre></div>';
  // Add VN metadata footer
  const vnFooter = document.createElement('div');
  vnFooter.className = 'vn-msg-footer';
  const nowH = now.getHours() % 12 || 12;
  const _timeStr = nowH + ':' + String(now.getMinutes()).padStart(2, '0') + ' ' + (now.getHours() >= 12 ? 'PM' : 'AM');
  vnFooter.innerHTML = (isVoice ? '<span class="vn-msg-footer-icon">\ud83c\udf99\ufe0f</span> Transcribed from voice \u00b7 ' : '<span class="vn-msg-footer-icon">\u26a1</span> ') + 'Sent at ' + _timeStr;
  userMsg.appendChild(vnFooter);
  userMsg.addEventListener('animationend', () => userMsg.classList.remove('msg-entering'), {once: true});
  logEl.appendChild(userMsg);
  logEl.scrollTop = logEl.scrollHeight;
}

function liveSubmitContinue(fromId) {
  const ta = document.getElementById('live-input-ta');
  const text = ta ? ta.value.trim() : '';
  if (!text) return;
  ta.value = '';
  _resetTextareaHeight(ta);

  const sid = typeof fromId === 'string' ? fromId : liveSessionId;
  if (!sid) return;
  _clearDraft(sid);

  // Check and consume voice flag
  const wasVoice = (typeof _lastSubmitWasVoice !== 'undefined' && _lastSubmitWasVoice);
  if (wasVoice) _lastSubmitWasVoice = false;

  // Add optimistic user bubble (with voice tag if applicable)
  _addOptimisticBubble(sid, text, wasVoice);

  _liveSending = true;

  // Clear any stale client-side queue from a previous interrupt —
  // the message is being sent directly, not queued.
  delete _sessionQueues[sid];
  _renderQueueBanner();

  // If session is not running, resume it via WebSocket
  if (!runningIds.has(sid)) {
    socket.emit('start_session', {
      session_id: sid,
      prompt: text,
      cwd: _currentProjectDir(),
      resume: true,
      voice: wasVoice || undefined,
    });
    runningIds.add(sid);
    guiOpenAdd(sid);
  } else {
    // Session is running — send message directly
    socket.emit('send_message', {session_id: sid, text: text, voice: wasVoice || undefined});
  }

  // Immediately render full working bar
  sessionKinds[sid] = 'working';
  liveBarState = null;
  updateLiveInputBar();

  // Start watchdog for this submit path too
  _startMessageWatchdog(sid);

  // Reset sending flag after brief delay (keeps session_entry dedup working)
  setTimeout(() => { _liveSending = false; }, 1000);
}

function liveSubmitWaiting() {
  const ta = document.getElementById('live-input-ta');
  if (!ta || !liveSessionId) return;
  const text = ta.value.trim();
  if (!text) return;
  ta.value = '';
  _resetTextareaHeight(ta);
  _clearDraft(liveSessionId);

  // Use permission_response if there's an active permission request
  if (waitingData[liveSessionId]) {
    // Map common textual responses to the accepted single-char actions
    const actionMap = {yes: 'y', no: 'n', always: 'a', 'almost always': 'aa', 'almost-always': 'aa', 'almostalways': 'aa', allow: 'y', deny: 'n'};
    const action = actionMap[text.toLowerCase()] || text;
    socket.emit('permission_response', {session_id: liveSessionId, action: action});
    // Optimistic clear
    delete waitingData[liveSessionId];
    sessionKinds[liveSessionId] = 'working';
    liveBarState = null;
    updateLiveInputBar();
    _startMessageWatchdog(liveSessionId);
  } else {
    // Fallback to direct send (watchdog is started inside _liveSubmitDirect)
    _liveSubmitDirect(liveSessionId, text);
  }
}

function liveSubmitInterrupt() {
  if (!liveSessionId) return;
  // Grab any text the user typed into the queue textarea (not yet queued).
  const queueTa = document.getElementById('live-queue-ta');
  const pendingText = queueTa ? queueTa.value.trim() : '';
  // Collect already-queued messages and merge with pending textarea text.
  // The merged text is placed in the idle input so the user can send it.
  const queued = _getQueueList(liveSessionId);
  const parts = [...queued];
  if (pendingText) parts.push(pendingText);
  const mergedText = parts.join('\n\n');
  // Clear queue and interrupt normally
  socket.emit('clear_queue', {session_id: liveSessionId});
  delete _sessionQueues[liveSessionId];
  _renderQueueBanner();
  socket.emit('interrupt_session', {session_id: liveSessionId});
  // Optimistic: immediately show idle state in the chat UI
  sessionKinds[liveSessionId] = 'idle';
  liveBarState = null;
  updateLiveInputBar();
  // Place merged text into the idle textarea so user can send it
  if (mergedText) {
    const idleTa = document.getElementById('live-input-ta');
    if (idleTa) { idleTa.value = mergedText; _initAutoResize(idleTa); }
  }
}

function liveClearDisplay() {
  const logEl = document.getElementById('live-log');
  if (logEl) { logEl.innerHTML = ''; liveLineCount = 0; }
  _liveEntryStash = [];
  _liveRenderedFrom = 0;
  showToast('Display cleared');
}

function liveCompact() {
  if (!liveSessionId) return;
  // Optimistically set compacting substatus so bar immediately shows
  // "Compacting…" instead of "Working…" while waiting for compact_boundary
  if (!window._sessionSubstatus) window._sessionSubstatus = {};
  window._sessionSubstatus[liveSessionId] = 'compacting';
  socket.emit('send_message', {session_id: liveSessionId, text: '/compact'});
  showToast('Compacting context\u2026');
}

async function closeSession(id) {
  if (!id) return;
  const s = allSessions.find(x => x.id === id);
  const name = (s && s.display_title) || id.slice(0, 8);
  const confirmed = await showConfirm('Close Session', '<p>Close <strong>' + escHtml(name) + '</strong>?</p><p>This will stop the running Claude process and close the terminal window.</p>', { danger: true, confirmText: 'Close', icon: '<svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><rect x="3" y="3" width="18" height="18" rx="2"/></svg>' });
  if (!confirmed) return;
  // Close via WebSocket
  if (runningIds.has(id)) {
    socket.emit('close_session', {session_id: id});
  }
  guiOpenDelete(id);
  runningIds.delete(id);
  delete sessionKinds[id];
  showToast('Session closed');
  // Update the input bar to reflect stopped state — keep chat visible
  liveBarState = null;
  updateLiveInputBar();
  filterSessions();
}
