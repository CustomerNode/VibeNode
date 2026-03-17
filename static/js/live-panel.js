/* live-panel.js — live terminal panel, input bar state machine, GUI session management */

function _formatMsgTime(tsStr) {
  const d = new Date(tsStr);
  if (isNaN(d)) return '';
  let h = d.getHours();
  const ampm = h >= 12 ? 'PM' : 'AM';
  h = h % 12 || 12;
  return h + ':' + String(d.getMinutes()).padStart(2, '0') + ' ' + ampm;
}

let liveLineCount = 0;
let _liveSending = false;  // blocks updateLiveInputBar while sending
let livePollTimer = null;
let liveAutoScroll = true;
let liveQueuedText = '';
let liveBarState = null;   // 'ended' | 'question:<questionText>' | 'idle' | 'working'
let _guiFocusPending = false;
let _optimisticIdleUntil = 0;  // timestamp: trust optimistic idle until this time

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

  fetchLiveLog();
}

function stopLivePanel() {
  if (livePollTimer) { clearTimeout(livePollTimer); livePollTimer = null; }
  liveSessionId = null;
  liveBarState = null;
  const btnClose = document.getElementById('btn-close');
  if (btnClose) btnClose.disabled = true;
}

async function fetchLiveLog() {
  if (!liveSessionId) return;
  const id = liveSessionId;

  // While sending, don't fetch — the optimistic bubble is already in the DOM
  if (_liveSending) {
    if (liveSessionId === id) livePollTimer = setTimeout(fetchLiveLog, 2000);
    return;
  }

  try {
    const r = await fetch('/api/session-log/' + id + '?since=' + liveLineCount);
    if (!r.ok) throw new Error('bad response');
    const d = await r.json();
    if (liveSessionId !== id) return;  // switched away

    const logEl = document.getElementById('live-log');
    if (!logEl) return;

    const hadNew = d.entries && d.entries.length;
    if (hadNew) {
      // Clear skeleton on first real data
      if (liveLineCount === 0) logEl.innerHTML = '';
      d.entries.forEach(e => logEl.appendChild(renderLiveEntry(e)));
    } else if (liveLineCount === 0 && d.total_lines === 0) {
      // No messages yet — just clear the skeleton quietly
      logEl.innerHTML = '';
    }
    liveLineCount = d.total_lines || liveLineCount;

    if (liveAutoScroll) logEl.scrollTop = logEl.scrollHeight;

    // If new entries arrived, check if Claude just finished responding
    if (hadNew) {
      const last = d.entries[d.entries.length - 1];
      if (last.kind === 'asst' && runningIds.has(id)) {
        // Assistant text = Claude finished, optimistically go idle immediately
        // Grace period prevents the next poll from reverting to 'working'
        sessionKinds[id] = 'idle';
        liveBarState = null;
        _optimisticIdleUntil = Date.now() + 12000;
      } else {
        pokeWaiting();
      }
    }

    updateLiveInputBar();
  } catch(e) {}
  finally {
    if (liveSessionId === id) {
      livePollTimer = setTimeout(fetchLiveLog, 2000);
    }
  }
}

function renderLiveEntry(e) {
  const div = document.createElement('div');

  if (e.kind === 'user' || e.kind === 'asst') {
    const role = e.kind === 'user' ? 'user' : 'assistant';
    div.className = 'msg ' + role;
    const text = e.text || '';
    const LIMIT = e.kind === 'asst' ? 600 : 800;
    const displayText = text.length > LIMIT ? text.slice(0, LIMIT) : text;

    const roleLabel = e.kind === 'user' ? 'me' : 'claude';
    const ts = e.ts ? _formatMsgTime(e.ts) : '';
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
    toolLine.innerHTML = '<span class="live-tool-icon">\u2699</span>' +
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
    line.textContent = (ok ? '\u2713 ' : '\u2717 ') + text.slice(0, 80) + (text.length > 80 ? '\u2026' : '');

    const detail = document.createElement('div');
    detail.className = 'live-tool-detail';
    detail.innerHTML = mdParse(_colorDiffLines(escHtml(text)));

    line.onclick = () => detail.classList.toggle('open');
    div.appendChild(line);
    div.appendChild(detail);
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

  // Don't re-render if the bar is already showing this exact state.
  // This is critical: prevents the 2s poll from wiping user's in-progress typed text.
  // Exception: working state updates the timer tick via interval, not re-render.
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
    const optLabels = { y: '\u2713 Yes', n: '\u2717 No', a: '\u2605 Always', yes: '\u2713 Yes', no: '\u2717 No' };
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
      '<div class="live-waiting-label">\uD83D\uDCAC Claude has a question</div>' +
      questionHTML +
      optBtns +
      '<textarea id="live-input-ta" class="live-textarea waiting-focus" rows="2" placeholder="Type your response\u2026 (or click an option above)"' +
      ' onkeydown="if(event.key===\'Enter\'&&(event.ctrlKey||event.metaKey)){event.preventDefault();liveSubmitWaiting()}"></textarea>' +
      '<div class="live-bar-row">' +
      '<span style="font-size:10px;color:#554400;">Ctrl+Enter to send</span>' +
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
      '<span style="font-size:10px;color:#444;">Ctrl+Enter to send</span>' +
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
    bar.innerHTML =
      '<div class="live-working-status">' +
      '<div class="live-working-indicator"><span class="spinner"></span> Working\u2026</div>' +
      '</div>' +
      '<textarea id="live-queue-ta" class="live-textarea" rows="2" ' +
      'style="opacity:0.6;" placeholder="Type your next command \u2014 will send when Claude finishes\u2026"' +
      ' onkeydown="if(event.key===\'Enter\'&&(event.ctrlKey||event.metaKey)){event.preventDefault();liveQueueSave()}">' +
      (liveQueuedText ? escHtml(liveQueuedText) : '') +
      '</textarea>' +
      '<div class="live-bar-row">' +
      '<span id="live-queue-hint" style="font-size:10px;color:#555;">' +
      (liveQueuedText ? '\u23f3 Command queued' : 'Will send automatically when done') +
      '</span>' +
      '<button class="live-send-btn" style="background:#2a2a2a;color:#666;border-color:#333;" onclick="liveQueueSave()">Queue</button>' +
      '<button class="live-send-btn" style="background:#1a0000;color:#664444;border-color:#330000;margin-left:2px;" onclick="liveClearQueue()" title="Cancel queued command">\u2715</button>' +
      '</div>';
    const qta = document.getElementById('live-queue-ta');
    if (qta) {
      qta.addEventListener('input', () => {
        liveQueuedText = qta.value;
        const hint = document.getElementById('live-queue-hint');
        if (hint) hint.textContent = qta.value.trim() ? '\u23f3 Command queued' : 'Will send automatically when done';
      });
    }
  }
}

function livePickOption(val) {
  // Fill the textarea with the option value and submit
  const ta = document.getElementById('live-input-ta');
  if (ta) ta.value = val;
  liveSubmitWaiting();
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

async function liveSubmitIdle() {
  const ta = document.getElementById('live-input-ta');
  if (!ta || !liveSessionId) return;
  const text = ta.value.trim();
  if (!text) return;
  // Reuse the same logic as continue
  await liveSubmitContinue(liveSessionId);
}

async function liveSubmitContinue(fromId) {
  const ta = document.getElementById('live-input-ta');
  const text = ta ? ta.value.trim() : '';
  if (!text) return;

  // IMMEDIATE feedback — show user message in chat, replace input bar with sending indicator
  const savedText = text;
  ta.value = '';
  ta.disabled = true;
  const now = new Date();
  const timeStr = now.getHours() % 12 || 12;
  const timestamp = timeStr + ':' + String(now.getMinutes()).padStart(2,'0') + ' ' + (now.getHours() >= 12 ? 'PM' : 'AM');
  const logEl = document.getElementById('live-log');
  if (logEl) {
    const userMsg = document.createElement('div');
    userMsg.className = 'msg user';
    userMsg.innerHTML = '<div class="msg-role">me <span class="msg-time">' + timestamp + '</span></div><div class="msg-body msg-content">' + mdParse(savedText) + '</div>';
    logEl.appendChild(userMsg);
    logEl.scrollTop = logEl.scrollHeight;
  }
  // Replace entire input bar with sending status — block polls from overwriting
  _liveSending = true;
  const bar = document.getElementById('live-input-bar');
  if (bar) bar.innerHTML = '<div class="live-working-status"><div class="live-working-indicator"><span class="spinner"></span> Sending\u2026</div></div>';

  // Try SendKeys
  try {
    const r = await fetch('/api/respond/' + fromId, {
      method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({text: savedText})
    });
    const d = await r.json();
    if (d.ok === false) console.warn('respond failed:', d);
    if (d.method === 'sent') {
      // Success — sync line count so we don't duplicate the optimistic bubble
      try {
        const sync = await fetch('/api/session-log/' + fromId + '?since=' + liveLineCount);
        const sd = await sync.json();
        if (sd.total_lines) liveLineCount = sd.total_lines;
      } catch(e) {}
      _liveSending = false;
      ta.disabled = false;
      liveBarState = null;
      pokeWaiting();
      return;
    }
  } catch(e) {}

  // SendKeys failed — resume the SAME session
  if (bar) bar.innerHTML = '<div class="live-working-status"><div class="live-working-indicator"><span class="spinner"></span> Resuming session\u2026</div></div>';
  try {
    await fetch('/api/new-session', {
      method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({resume_id: fromId})
    });
  } catch(e) {}

  // Wait for terminal to start, then retry SendKeys (3 attempts, 2s apart)
  let sent = false;
  for (let attempt = 0; attempt < 3; attempt++) {
    await new Promise(r => setTimeout(r, attempt === 0 ? 4000 : 2000));
    try {
      const r2 = await fetch('/api/respond/' + fromId, {
        method: 'POST', headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({text: savedText})
      });
      const d2 = await r2.json();
      if (d2.method === 'sent') {
            // Sync line count so poll doesn't duplicate the optimistic bubble
            try {
              const sync = await fetch('/api/session-log/' + fromId + '?since=' + liveLineCount);
              const sd = await sync.json();
              if (sd.total_lines) liveLineCount = sd.total_lines;
            } catch(e) {}
            sent = true; break;
          }
    } catch(e) {}
  }

  if (!sent) {
    // All retries failed — show persistent in-body error
    if (bar) bar.innerHTML =
      '<div style="padding:12px;background:var(--bg-card);border:1px solid #663333;border-radius:8px;margin-bottom:8px;">' +
      '<div style="color:#cc6666;font-size:13px;margin-bottom:8px;">Could not deliver message. The session terminal may not have started.</div>' +
      '<div style="color:var(--text-muted);font-size:12px;margin-bottom:8px;">Your message: <em>' + escHtml(savedText.slice(0, 200)) + '</em></div>' +
      '<button class="live-send-btn" onclick="liveRetrySend(\'' + fromId.replace(/'/g,"\\'") + '\', ' + JSON.stringify(savedText) + ')">Retry Send</button>' +
      '</div>';
  }

  _liveSending = false;
  ta.disabled = false;
  liveBarState = null;
  pokeWaiting();
}

async function liveRetrySend(sessionId, text) {
  const bar = document.getElementById('live-input-bar');
  if (bar) bar.innerHTML = '<div class="live-working-status"><div class="live-working-indicator"><span class="spinner"></span> Retrying\u2026</div></div>';
  _liveSending = true;
  try {
    const r = await fetch('/api/respond/' + sessionId, {
      method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({text: text})
    });
    const d = await r.json();
    if (d.method === 'sent') {
      showToast('Message sent');
      _liveSending = false;
      liveBarState = null;
      pokeWaiting();
      return;
    }
  } catch(e) {}
  _liveSending = false;
  liveBarState = null;
  showToast('Still could not send. Check if the terminal is running.', true);
  updateLiveInputBar();
}

async function liveSubmitWaiting() {
  const ta = document.getElementById('live-input-ta');
  if (!ta || !liveSessionId) return;
  const text = ta.value.trim();
  if (!text) return;
  // Reuse the same logic — try send, auto-resume if needed
  await liveSubmitContinue(liveSessionId);
}

async function closeSession(id) {
  if (!id) return;
  const s = allSessions.find(x => x.id === id);
  const name = (s && s.display_title) || id.slice(0, 8);
  const confirmed = await showConfirm('Close Session', '<p>Close <strong>' + escHtml(name) + '</strong>?</p><p>This will stop the running Claude process and close the terminal window.</p>', { danger: true, confirmText: 'Close', icon: '\u23F9\uFE0F' });
  if (!confirmed) return;
  // Attempt to kill the process (may already be stopped — that's fine)
  if (runningIds.has(id)) {
    const r = await fetch('/api/close/' + id, { method: 'POST' });
    const d = await r.json();
    if (!d.ok) showToast('Process stop: ' + (d.error || 'unknown'));
  }
  guiOpenDelete(id);
  runningIds.delete(id);
  showToast('Session closed');
  // Update the input bar to reflect stopped state — keep chat visible
  updateLiveInputBar();
  filterSessions();
}
