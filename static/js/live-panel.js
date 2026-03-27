/* live-panel.js — live terminal panel, input bar state machine, GUI session management */

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
const _renderedUserTexts = new Set();
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
  _guiFocusPending = true;
  closeAllGrpDropdowns();
  activeId = id;
  localStorage.setItem('activeSessionId', id || '');
  _pushChatUrl(id);
  if (runningIds.has(id)) guiOpenAdd(id);
  if (liveSessionId && liveSessionId !== id) { _autoSendPendingInput(); stopLivePanel(); }
  filterSessions();

  // Show title from sidebar data immediately (no mismatch)
  const cached = allSessions.find(x => x.id === id);
  const initTitle = cached ? cached.display_title : 'Loading\u2026';
  setToolbarSession(id, initTitle, !(cached && cached.custom_title), (cached && cached.custom_title) || '');
  document.getElementById('main-body').innerHTML = _chatSkeleton();
  const resp = await fetch('/api/session/' + id);

  // New session with no .jsonl yet — re-show the new session input
  if (!resp.ok) {
    if (guiOpenSessions.has(id) && !runningIds.has(id)) {
      // Re-create the new session chat view
      setToolbarSession(id, 'New Session', true, '');
      document.getElementById('main-body').innerHTML =
        '<div class="live-panel" id="live-panel">' +
        '<div class="conversation live-log" id="live-log">' +
        '<div class="empty-state" style="padding:60px 0;text-align:center;">' +
        '<svg width="32" height="32" viewBox="0 0 24 24" fill="none" stroke="var(--text-faint)" stroke-width="1.5" stroke-linecap="round" style="margin-bottom:12px;opacity:0.4;"><path d="M21 15a2 2 0 0 1-2 2H7l-4 4V5a2 2 0 0 1 2-2h14a2 2 0 0 1 2 2z"/></svg>' +
        '<div style="color:var(--text-faint);font-size:13px;">What should Claude work on?</div>' +
        (typeof _renderTemplateGrid === 'function' ? _renderTemplateGrid(id) : '') +
        '</div></div>' +
        '<div class="live-output-shelf" id="live-output-shelf"></div>' +
        '<div id="live-queue-area"></div>' +
        '<div class="live-input-bar" id="live-input-bar"></div></div>';
      liveSessionId = id;
      liveBarState = null;
      _renderedUserTexts.clear();
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
  setToolbarSession(id, s.custom_title || s.display_title, !s.custom_title, s.custom_title || '');

  startLivePanel(id);
}

function startLivePanel(id, opts) {
  stopLivePanel();
  liveSessionId = id;
  liveLineCount = 0;
  liveAutoScroll = true;
  if (!(opts && opts.skipLog)) _renderedUserTexts.clear();
  // Queue is restored from per-session localStorage — no reset here
  // Restore working_since from the map (set by state_snapshot/session_state)
  if (window._workingSinceMap && window._workingSinceMap[id]) {
    _liveWorkingStart = window._workingSinceMap[id];
  }
  liveBarState = null;  // force fresh render

  const skipLog = opts && opts.skipLog;
  const skelHtml = skipLog ? '' : _chatSkeleton().replace('<div class="conversation">', '').replace(/<\/div>$/, '');
  document.getElementById('main-body').innerHTML =
    '<div class="live-panel" id="live-panel">' +
    '<div class="conversation live-log" id="live-log">' + skelHtml + '</div>' +
    '<div class="live-output-shelf" id="live-output-shelf"></div>' +
    '<div id="live-queue-area"></div>' +
    '<div class="live-input-bar" id="live-input-bar"></div></div>';

  _clearOutputShelf();
  const logEl = document.getElementById('live-log');
  logEl.addEventListener('scroll', () => {
    const atBottom = logEl.scrollHeight - logEl.scrollTop - logEl.clientHeight < 60;
    liveAutoScroll = atBottom;
  });

  const btnClose = document.getElementById('btn-close');
  if (btnClose) btnClose.disabled = false;

  // Request the log via WebSocket (skip for brand-new sessions — optimistic bubble is enough)
  if (!skipLog) {
    socket.emit('get_session_log', {session_id: id, since: 0});
  }

  // Render input bar immediately and schedule re-renders in case
  // state events arrived before the DOM was ready.
  // BUT: skip re-render if user has already started typing.
  liveBarState = null;
  updateLiveInputBar();
  _renderQueueBanner();
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
      const actionMap = {yes: 'y', no: 'n', always: 'a', allow: 'y', deny: 'n'};
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
    div.className = 'msg ' + role;
    const text = e.text || '';
    const LIMIT = e.kind === 'asst' ? 2000 : 2000;
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

    // Smart copy buttons for assistant messages
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

  } else if (e.kind === 'tool_use') {
    div.className = 'live-entry live-entry-tool';
    const toolLine = document.createElement('div');
    toolLine.className = 'live-tool-line';
    toolLine.innerHTML = '<span class="live-tool-icon"><svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><circle cx="12" cy="12" r="3"/><path d="M19.4 15a1.65 1.65 0 0 0 .33 1.82l.06.06a2 2 0 0 1-2.83 2.83l-.06-.06a1.65 1.65 0 0 0-1.82-.33 1.65 1.65 0 0 0-1 1.51V21a2 2 0 0 1-4 0v-.09A1.65 1.65 0 0 0 9 19.4a1.65 1.65 0 0 0-1.82.33l-.06.06a2 2 0 0 1-2.83-2.83l.06-.06A1.65 1.65 0 0 0 4.68 15a1.65 1.65 0 0 0-1.51-1H3a2 2 0 0 1 0-4h.09A1.65 1.65 0 0 0 4.6 9"/></svg></span>' +
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
    div.className = 'live-entry live-entry-result';
    const text = e.text || e.message || '';
    const line = document.createElement('div');
    line.className = 'live-result-line live-result-err';
    line.style.cursor = 'pointer';
    line.innerHTML = '<svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" style="vertical-align:middle;margin-right:4px;"><path d="M10.29 3.86L1.82 18a2 2 0 0 0 1.71 3h16.94a2 2 0 0 0 1.71-3L13.71 3.86a2 2 0 0 0-3.42 0z"/><line x1="12" y1="9" x2="12" y2="13"/><line x1="12" y1="17" x2="12.01" y2="17"/></svg>' + escHtml(text.slice(0, 120)) + (text.length > 120 ? '\u2026' : '');

    const detail = document.createElement('div');
    detail.className = 'live-tool-detail';
    detail.innerHTML = '<pre style="white-space:pre-wrap;margin:0;color:var(--result-err);">' + escHtml(text) + '</pre>';

    line.onclick = () => detail.classList.toggle('open');
    div.appendChild(line);
    div.appendChild(detail);

  } else if (e.kind === 'stream') {
    // Streaming partial text — render as assistant fragment
    div.className = 'msg assistant';
    const text = e.text || '';
    div.innerHTML = '<div class="msg-role">claude <span class="msg-time" style="color:var(--text-faint);font-size:10px;">streaming\u2026</span></div>' +
      '<div class="msg-body msg-content">' + mdParse(text) + '</div>';
  }

  return div;
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
  else stateKey = 'working:' + _getQueueList(id).length;

  // Reset working timer when leaving working state — but only if we
  // have a definitive non-working state (not just 'ended' from missing data)
  if (!stateKey.startsWith('working') && kind) {
    _liveWorkingStart = null;
    if (_liveWorkingTimer) { clearInterval(_liveWorkingTimer); _liveWorkingTimer = null; }
  }

  // Don't re-render if the bar is already showing this exact state.
  // This is critical: prevents wiping user's in-progress typed text.
  if (stateKey === liveBarState) return;
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
    const optLabels = { y: _chk + ' Yes', n: _xic + ' No', a: _star + ' Always', yes: _chk + ' Yes', no: _xic + ' No' };
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

    bar.innerHTML =
      '<div class="live-working-status">' +
      '<div class="live-working-indicator"><span class="spinner"></span> Working\u2026 <span id="live-elapsed" style="color:var(--text-faint);font-size:10px;margin-left:6px;">' + _elapsedStr + '</span></div>' +
      '<button class="live-stop-btn" onclick="liveSubmitInterrupt()" title="Interrupt session">\u25A0 Stop</button>' +
      '</div>' +
      '<textarea id="live-queue-ta" class="live-textarea live-queue-ta" rows="2" ' +
      'placeholder="' + (qCount ? 'Queue another command\u2026' : 'Type your next command \u2014 will send when Claude finishes\u2026') + '"' +
      ' onkeydown="if(_shouldSend(event)){event.preventDefault();liveQueueSave()}"></textarea>' +
      '<div class="live-bar-row">' +
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
    if (!_liveWorkingTimer) {
      _liveWorkingTimer = setInterval(() => {
        if (!liveBarState || !liveBarState.startsWith('working') || !_liveWorkingStart) return;
        const el = document.getElementById('live-elapsed');
        if (!el) return;
        const s = Math.round((Date.now() - _liveWorkingStart) / 1000);
        el.textContent = s >= 60 ? Math.floor(s/60) + 'm ' + (s%60) + 's' : s + 's';
      }, 1000);
    }
  }

  // ── Restore preserved text from the previous state's textarea ──
  // When transitioning between states (idle↔working, new-chat↔q-chat, etc.)
  // any text the user was typing/dictating is carried across into the new textarea.
  if (_preservedText) {
    const _newTa = document.getElementById('live-input-ta') || document.getElementById('live-queue-ta');
    if (_newTa && !_newTa.value) {
      _newTa.value = _preservedText;
      _autoResizeTextarea(_newTa);
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
  _liveSubmitDirect(liveSessionId, text);
}

function _liveSubmitDirect(sid, text, opts) {
  if (!sid) return;
  _liveSending = true;

  // Only treat as permission response if explicitly flagged (not from queue)
  const isPermission = opts && opts.isPermission;
  const wasPermission = isPermission && !!waitingData[sid];
  if (wasPermission) {
    const actionMap = {yes: 'y', no: 'n', always: 'a', allow: 'y', deny: 'n'};
    const action = actionMap[text.toLowerCase()] || text;
    socket.emit('permission_response', {session_id: sid, action: action});
    // Optimistic clear
    delete waitingData[sid];
    sessionKinds[sid] = 'working';
  } else if (runningIds.has(sid)) {
    // Send message — server will process if idle, or queue if busy
    socket.emit('send_message', {session_id: sid, text: text});
  }

  // Add optimistic user bubble only for real messages (not permission answers)
  if (!wasPermission && text.length > 1) {
    _addOptimisticBubble(sid, text);
  }

  // Optimistically set working state and render the full working bar immediately
  if (!wasPermission) {
    sessionKinds[sid] = 'working';
  }
  liveBarState = null;
  updateLiveInputBar();

  // Reset sending flag after brief delay (keeps session_entry dedup working)
  setTimeout(() => { _liveSending = false; }, 500);
}

function _addOptimisticBubble(sid, text) {
  if (sid !== liveSessionId) return;
  const logEl = document.getElementById('live-log');
  if (!logEl) return;

  // Hard dedup: if this exact text is already tracked, bail out
  const key = text.trim();
  if (_renderedUserTexts.has(key)) return;
  // DOM-level dedup: if the last user bubble has same text, skip
  const _lu = logEl.querySelector('.msg.user:last-child .msg-body');
  if (_lu && _lu.textContent.trim() === key) return;
  _renderedUserTexts.add(key);

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
  const userMsg = document.createElement('div');
  userMsg.className = 'msg user msg-entering';
  userMsg.innerHTML = '<div class="msg-role">me <span class="msg-time">' + timestamp + '</span></div><div class="msg-body msg-content"><pre style="white-space:pre-wrap;margin:0;">' + escHtml(text) + '</pre></div>';
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

  // Add optimistic user bubble
  _addOptimisticBubble(sid, text);

  _liveSending = true;

  // If session is not running, resume it via WebSocket
  if (!runningIds.has(sid)) {
    socket.emit('start_session', {
      session_id: sid,
      prompt: text,
      cwd: _currentProjectDir(),
      resume: true,
    });
    runningIds.add(sid);
    guiOpenAdd(sid);
  } else {
    // Session is running — send message directly
    socket.emit('send_message', {session_id: sid, text: text});
  }

  // Immediately render full working bar
  sessionKinds[sid] = 'working';
  liveBarState = null;
  updateLiveInputBar();

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

  // Use permission_response if there's an active permission request
  if (waitingData[liveSessionId]) {
    // Map common textual responses to the accepted single-char actions
    const actionMap = {yes: 'y', no: 'n', always: 'a', allow: 'y', deny: 'n'};
    const action = actionMap[text.toLowerCase()] || text;
    socket.emit('permission_response', {session_id: liveSessionId, action: action});
    // Optimistic clear
    delete waitingData[liveSessionId];
    sessionKinds[liveSessionId] = 'working';
    liveBarState = null;
    updateLiveInputBar();
  } else {
    // Fallback to direct send
    _liveSubmitDirect(liveSessionId, text);
  }
}

function liveSubmitInterrupt() {
  if (!liveSessionId) return;
  // Preserve any text the user typed into the queue textarea so it isn't
  // lost when the input bar is rebuilt in idle mode.
  const queueTa = document.getElementById('live-queue-ta');
  const preservedText = queueTa ? queueTa.value : '';
  // Clear any queued commands — user is intentionally stopping, so we must
  // NOT auto-send queued text when the session goes idle.
  // Server-side clear ensures queue is emptied even if GUI disconnects.
  socket.emit('clear_queue', {session_id: liveSessionId});
  delete _sessionQueues[liveSessionId]; // optimistic local clear
  _renderQueueBanner();
  socket.emit('interrupt_session', {session_id: liveSessionId});
  // Optimistic: immediately show idle state in the chat UI
  sessionKinds[liveSessionId] = 'idle';
  liveBarState = null;
  updateLiveInputBar();
  // Restore the preserved text into the new idle textarea
  if (preservedText) {
    const idleTa = document.getElementById('live-input-ta');
    if (idleTa) { idleTa.value = preservedText; _initAutoResize(idleTa); }
  }
}

function liveClearDisplay() {
  const logEl = document.getElementById('live-log');
  if (logEl) { logEl.innerHTML = ''; liveLineCount = 0; }
  showToast('Display cleared');
}

function liveCompact() {
  if (!liveSessionId) return;
  socket.emit('send_message', {session_id: liveSessionId, text: '/compact'});
  showToast('Sent /compact command');
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
