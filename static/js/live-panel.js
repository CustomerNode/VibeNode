/* live-panel.js — live terminal panel, input bar state machine, GUI session management */

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
let _liveSending = false;  // blocks updateLiveInputBar while sending
let liveAutoScroll = true;
let liveQueuedText = '';
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
  if (runningIds.has(id)) guiOpenAdd(id);
  if (liveSessionId && liveSessionId !== id) stopLivePanel();
  filterSessions();

  // Show title from sidebar data immediately (no mismatch)
  const cached = allSessions.find(x => x.id === id);
  const initTitle = cached ? cached.display_title : 'Loading\u2026';
  setToolbarSession(id, initTitle, !(cached && cached.custom_title), (cached && cached.custom_title) || '');
  document.getElementById('main-body').innerHTML = _chatSkeleton();
  const resp = await fetch('/api/session/' + id);
  const s = await resp.json();
  setToolbarSession(id, s.custom_title || s.display_title, !s.custom_title, s.custom_title || '');

  startLivePanel(id);
}

function startLivePanel(id) {
  stopLivePanel();
  liveSessionId = id;
  liveLineCount = 0;
  liveAutoScroll = true;
  liveQueuedText = '';
  liveBarState = null;  // force fresh render

  const skelHtml = _chatSkeleton().replace('<div class="conversation">', '').replace(/<\/div>$/, '');
  document.getElementById('main-body').innerHTML =
    '<div class="live-panel" id="live-panel">' +
    '<div class="conversation live-log" id="live-log">' + skelHtml + '</div>' +
    '<div class="live-input-bar" id="live-input-bar"></div></div>';

  const logEl = document.getElementById('live-log');
  logEl.addEventListener('scroll', () => {
    const atBottom = logEl.scrollHeight - logEl.scrollTop - logEl.clientHeight < 60;
    liveAutoScroll = atBottom;
  });

  const btnClose = document.getElementById('btn-close');
  if (btnClose) btnClose.disabled = false;

  // Request the log via WebSocket instead of polling
  socket.emit('get_session_log', {session_id: id, since: 0});

  // Render input bar immediately and schedule a re-render in case
  // state events arrived before the DOM was ready
  liveBarState = null;
  updateLiveInputBar();
  setTimeout(() => { liveBarState = null; updateLiveInputBar(); }, 500);
  setTimeout(() => { liveBarState = null; updateLiveInputBar(); }, 2000);
}

function stopLivePanel() {
  liveSessionId = null;
  liveBarState = null;
  const btnClose = document.getElementById('btn-close');
  if (btnClose) btnClose.disabled = true;
}

function renderLiveEntry(e) {
  const div = document.createElement('div');
  if (!e) return div;

  if (e.kind === 'user' || e.kind === 'asst') {
    const role = e.kind === 'user' ? 'user' : 'assistant';
    div.className = 'msg ' + role;
    const text = e.text || '';
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

    if (text.length > LIMIT) {
      const btn = document.createElement('button');
      btn.className = 'live-expand-btn';
      btn.textContent = '\u2026 show more';
      btn.onclick = () => {
        const bodyEl = div.querySelector('.msg-body');
        if (e.kind === 'asst') { bodyEl.innerHTML = mdParse(text); }
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
  if (_liveSending) return;  // don't overwrite while sending
  if (!liveSessionId) return;
  const id = liveSessionId;
  const bar = document.getElementById('live-input-bar');
  if (!bar) return;

  const kind = sessionKinds[id];  // 'question' | 'working' | 'idle' | undefined
  const isRunning = runningIds.has(id);
  const wd = waitingData[id];     // {question, options, kind} or undefined

  // Compute a state key — for question state, include question text so we re-render if the question changed
  let stateKey;
  if (!isRunning) stateKey = 'ended';
  else if (kind === 'question') stateKey = 'question:' + (wd ? wd.question || '' : '');
  else if (kind === 'idle') stateKey = 'idle';
  else stateKey = 'working';

  // Reset working timer when leaving working state
  if (stateKey !== 'working') {
    _liveWorkingStart = null;
    if (_liveWorkingTimer) { clearInterval(_liveWorkingTimer); _liveWorkingTimer = null; }
  }

  // Don't re-render if the bar is already showing this exact state.
  // This is critical: prevents wiping user's in-progress typed text.
  if (stateKey === liveBarState) return;
  liveBarState = stateKey;

  if (!isRunning) {
    bar.innerHTML =
      '<div style="display:flex;align-items:center;gap:8px;margin-bottom:8px;padding:8px 12px;background:var(--bg-card);border:1px solid var(--border-subtle);border-radius:8px;">' +
      '<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="var(--text-faint)" stroke-width="2" stroke-linecap="round"><circle cx="12" cy="12" r="10"/><line x1="8" y1="12" x2="16" y2="12"/></svg>' +
      '<span style="font-size:12px;color:var(--text-muted);">Session not running. Type a message to resume.</span>' +
      '</div>' +
      '<textarea id="live-input-ta" class="live-textarea" rows="2" placeholder="Type a message to continue\u2026"' +
      ' onkeydown="if(event.key===\'Enter\'&&(event.ctrlKey||event.metaKey)){event.preventDefault();liveSubmitContinue(\'' + id + '\')}"></textarea>' +
      '<div class="live-bar-row">' +
      '<span style="font-size:10px;color:var(--text-faint);">Ctrl+Enter to send</span>' +
      '<button class="live-send-btn" onclick="liveSubmitContinue(\'' + id + '\')">Send \u21b5</button>' +
      '</div>';
    const btnClose = document.getElementById('btn-close');
    if (btnClose) btnClose.disabled = true;
    _guiFocusPending = false;
    setTimeout(() => {
      const logEl = document.getElementById('live-log');
      if (logEl) logEl.scrollTop = logEl.scrollHeight;
      const ta = document.getElementById('live-input-ta');
      if (ta) ta.focus();
    }, 50);

  } else if (kind === 'question') {
    // Claude is asking something — show question text + option buttons + free-form textarea
    const prefill = liveQueuedText;
    liveQueuedText = '';
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
      ' onkeydown="if(event.key===\'Enter\'&&(event.ctrlKey||event.metaKey)){event.preventDefault();liveSubmitWaiting()}"></textarea>' +
      '<div class="live-bar-row">' +
      '<span style="font-size:10px;color:var(--text-faint);">Ctrl+Enter to send</span>' +
      '<button class="live-send-btn waiting" onclick="liveSubmitWaiting()">Send \u21b5</button>' +
      '</div>';
    const ta = document.getElementById('live-input-ta');
    if (ta) {
      if (prefill) ta.value = prefill;
      const shouldFocus = _guiFocusPending || true;
      if (shouldFocus) {
        _guiFocusPending = false;
        setTimeout(() => {
          const logEl = document.getElementById('live-log');
          if (logEl) logEl.scrollTop = logEl.scrollHeight;
          ta.focus();
        }, 50);
      }
    }

  } else if (kind === 'idle') {
    bar.innerHTML =
      '<textarea id="live-input-ta" class="live-textarea" rows="2" placeholder="Type your next command\u2026"' +
      ' onkeydown="if(event.key===\'Enter\'&&(event.ctrlKey||event.metaKey)){event.preventDefault();liveSubmitIdle()}"></textarea>' +
      '<div class="live-bar-row">' +
      '<span style="font-size:10px;color:var(--text-faint);">Ctrl+Enter to send</span>' +
      '<button class="live-send-btn" onclick="liveSubmitIdle()">Send \u21b5</button>' +
      '</div>';
    _guiFocusPending = false;
    setTimeout(() => {
      const logEl = document.getElementById('live-log');
      if (logEl) logEl.scrollTop = logEl.scrollHeight;
      const ta = document.getElementById('live-input-ta');
      if (ta) ta.focus();
    }, 50);

  } else {
    // Start elapsed timer
    if (!_liveWorkingStart) _liveWorkingStart = Date.now();
    const _elapsed = Math.round((Date.now() - _liveWorkingStart) / 1000);
    const _elapsedStr = _elapsed >= 60 ? Math.floor(_elapsed/60) + 'm ' + (_elapsed%60) + 's' : _elapsed + 's';
    bar.innerHTML =
      '<div class="live-working-status">' +
      '<div class="live-working-indicator"><span class="spinner"></span> Working\u2026 <span id="live-elapsed" style="color:var(--text-faint);font-size:10px;margin-left:6px;">' + _elapsedStr + '</span></div>' +
      '<button class="live-stop-btn" onclick="liveSubmitInterrupt()" title="Interrupt session">\u25A0 Stop</button>' +
      '</div>' +
      '<textarea id="live-queue-ta" class="live-textarea" rows="2" ' +
      'style="opacity:0.6;" placeholder="Type your next command \u2014 will send when Claude finishes\u2026"' +
      ' onkeydown="if(event.key===\'Enter\'&&(event.ctrlKey||event.metaKey)){event.preventDefault();liveQueueSave()}">' +
      (liveQueuedText ? escHtml(liveQueuedText) : '') +
      '</textarea>' +
      '<div class="live-bar-row">' +
      '<span id="live-queue-hint" style="font-size:10px;color:var(--text-faint);">' +
      (liveQueuedText ? '<svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" style="vertical-align:middle;"><circle cx="12" cy="12" r="10"/><polyline points="12 6 12 12 16 14"/></svg> Command queued' : 'Will send automatically when done') +
      '</span>' +
      '<button class="live-send-btn" style="background:var(--bg-card);color:var(--text-muted);border-color:var(--border-subtle);" onclick="liveQueueSave()">Queue</button>' +
      '<button class="live-send-btn danger" style="margin-left:2px;" onclick="liveClearQueue()" title="Cancel queued command"><svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round"><line x1="18" y1="6" x2="6" y2="18"/><line x1="6" y1="6" x2="18" y2="18"/></svg></button>' +
      '</div>';
    const qta = document.getElementById('live-queue-ta');
    if (qta) {
      qta.addEventListener('input', () => {
        liveQueuedText = qta.value;
        const hint = document.getElementById('live-queue-hint');
        if (hint) hint.innerHTML = qta.value.trim() ? '<svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" style="vertical-align:middle;"><circle cx="12" cy="12" r="10"/><polyline points="12 6 12 12 16 14"/></svg> Command queued' : 'Will send automatically when done';
      });
    }
    // Start elapsed timer — update only the time span, NOT the whole bar
    if (!_liveWorkingTimer) {
      _liveWorkingTimer = setInterval(() => {
        if (liveBarState !== 'working' || !_liveWorkingStart) return;
        const el = document.getElementById('live-elapsed');
        if (!el) return;
        const s = Math.round((Date.now() - _liveWorkingStart) / 1000);
        el.textContent = s >= 60 ? Math.floor(s/60) + 'm ' + (s%60) + 's' : s + 's';
      }, 1000);
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
  if (ta) {
    liveQueuedText = ta.value.trim();
    showToast(liveQueuedText ? 'Command queued \u2014 will send when Claude finishes' : 'Queue cleared');
  }
}

function liveClearQueue() {
  liveQueuedText = '';
  const ta = document.getElementById('live-queue-ta');
  if (ta) ta.value = '';
  const hint = document.getElementById('live-queue-hint');
  if (hint) hint.textContent = 'Will send automatically when done';
  showToast('Queue cleared');
}

function liveSubmitIdle() {
  const ta = document.getElementById('live-input-ta');
  if (!ta || !liveSessionId) return;
  const text = ta.value.trim();
  if (!text) return;
  _liveSubmitDirect(liveSessionId, text);
  ta.value = '';
}

function _liveSubmitDirect(sid, text, opts) {
  if (!sid) return;
  _liveSending = true;

  // If it's a permission response (from waitingData), use permission_response event
  const wasPermission = !!waitingData[sid];
  if (wasPermission) {
    const actionMap = {yes: 'y', no: 'n', always: 'a', allow: 'y', deny: 'n'};
    const action = actionMap[text.toLowerCase()] || text;
    socket.emit('permission_response', {session_id: sid, action: action});
    // Optimistic clear
    delete waitingData[sid];
    sessionKinds[sid] = 'working';
  } else if (runningIds.has(sid)) {
    // Send message to running session
    socket.emit('send_message', {session_id: sid, text: text});
  }

  // Add optimistic user bubble only for real messages (not permission answers)
  if (!wasPermission && text.length > 1) {
    _addOptimisticBubble(sid, text);
  }

  // Reset sending flag after brief delay (let WebSocket events flow)
  setTimeout(() => {
    _liveSending = false;
    liveBarState = null;
    updateLiveInputBar();
    // Scroll to bottom after bar re-renders
    const logEl = document.getElementById('live-log');
    if (logEl && liveAutoScroll) logEl.scrollTop = logEl.scrollHeight;
  }, 500);
}

function _addOptimisticBubble(sid, text) {
  if (sid !== liveSessionId) return;
  const logEl = document.getElementById('live-log');
  if (!logEl) return;
  const now = new Date();
  const h = now.getHours() % 12 || 12;
  const timestamp = h + ':' + String(now.getMinutes()).padStart(2, '0') + ' ' + (now.getHours() >= 12 ? 'PM' : 'AM');
  const userMsg = document.createElement('div');
  userMsg.className = 'msg user';
  userMsg.innerHTML = '<div class="msg-role">me <span class="msg-time">' + timestamp + '</span></div><div class="msg-body msg-content">' + mdParse(text) + '</div>';
  logEl.appendChild(userMsg);
  logEl.scrollTop = logEl.scrollHeight;
}

function liveSubmitContinue(fromId) {
  const ta = document.getElementById('live-input-ta');
  const text = ta ? ta.value.trim() : '';
  if (!text) return;
  ta.value = '';

  const sid = typeof fromId === 'string' ? fromId : liveSessionId;
  if (!sid) return;

  // Add optimistic user bubble
  _addOptimisticBubble(sid, text);

  // Show sending indicator
  _liveSending = true;
  const bar = document.getElementById('live-input-bar');
  if (bar) bar.innerHTML = '<div class="live-working-status"><div class="live-working-indicator"><span class="spinner"></span> Sending\u2026</div></div>';

  // If session is not running, resume it via WebSocket
  if (!runningIds.has(sid)) {
    socket.emit('start_session', {
      session_id: sid,
      prompt: text,
      cwd: _currentProjectDir(),
      resume: true,
    });
    // Optimistic state
    runningIds.add(sid);
    guiOpenAdd(sid);
    sessionKinds[sid] = 'working';
  } else {
    // Session is running — send message directly
    socket.emit('send_message', {session_id: sid, text: text});
  }

  // Reset sending flag after brief delay
  setTimeout(() => {
    _liveSending = false;
    liveBarState = null;
    updateLiveInputBar();
  }, 1000);
}

function liveSubmitWaiting() {
  const ta = document.getElementById('live-input-ta');
  if (!ta || !liveSessionId) return;
  const text = ta.value.trim();
  if (!text) return;
  ta.value = '';

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
  socket.emit('interrupt_session', {session_id: liveSessionId});
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
