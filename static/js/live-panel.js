/* live-panel.js — live terminal panel, input bar state machine, GUI session management */

let liveLineCount = 0;
let livePollTimer = null;
let liveAutoScroll = true;
let liveQueuedText = '';
let liveBarState = null;   // 'ended' | 'question:<questionText>' | 'idle' | 'working'
let _guiFocusPending = false;

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
  guiOpenAdd(id);  // track as GUI-open so we show idle state (persisted)
  if (liveSessionId && liveSessionId !== id) stopLivePanel();
  filterSessions();

  setToolbarSession(id, 'Loading\u2026', true, '');
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

  document.getElementById('main-body').innerHTML =
    '<div class="live-panel" id="live-panel">' +
    '<div class="live-log" id="live-log"></div>' +
    '<div class="live-input-bar" id="live-input-bar">' +
    '<div class="live-working"><span class="spinner"></span>Loading session\u2026</div>' +
    '</div></div>';

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
  try {
    const r = await fetch('/api/session-log/' + id + '?since=' + liveLineCount);
    if (!r.ok) throw new Error('bad response');
    const d = await r.json();
    if (liveSessionId !== id) return;  // switched away

    const logEl = document.getElementById('live-log');
    if (!logEl) return;

    if (d.entries && d.entries.length) {
      d.entries.forEach(e => logEl.appendChild(renderLiveEntry(e)));
    }
    liveLineCount = d.total_lines || liveLineCount;

    if (liveAutoScroll) logEl.scrollTop = logEl.scrollHeight;

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
  div.className = 'live-entry';

  if (e.kind === 'user' || e.kind === 'asst') {
    div.classList.add(e.kind === 'user' ? 'live-entry-user' : 'live-entry-asst');
    const LIMIT = e.kind === 'asst' ? 600 : 800;
    const labelDiv = document.createElement('div');
    labelDiv.className = 'live-label';
    labelDiv.textContent = e.kind === 'user' ? 'You' : 'Claude';
    div.appendChild(labelDiv);

    const text = e.text || '';
    const textDiv = document.createElement('div');
    textDiv.className = 'live-text';
    const displayText = text.length > LIMIT ? text.slice(0, LIMIT) : text;
    if (e.kind === 'asst') {
      textDiv.innerHTML = mdParse(displayText);
    } else {
      textDiv.textContent = displayText;
    }
    div.appendChild(textDiv);

    if (text.length > LIMIT) {
      const btn = document.createElement('button');
      btn.className = 'live-expand-btn';
      btn.textContent = '\u2026 show more';
      btn.onclick = () => {
        if (e.kind === 'asst') { textDiv.innerHTML = mdParse(text); }
        else { textDiv.textContent = text; }
        btn.remove();
      };
      div.appendChild(btn);
    }

  } else if (e.kind === 'tool_use') {
    div.classList.add('live-entry-tool');
    const toolLine = document.createElement('div');
    toolLine.className = 'live-tool-line';

    const icon = document.createElement('span');
    icon.className = 'live-tool-icon';
    icon.textContent = '\u2699';

    const nameEl = document.createElement('span');
    nameEl.className = 'live-tool-name';
    nameEl.textContent = e.name || 'tool';

    const descEl = document.createElement('span');
    descEl.className = 'live-tool-desc';
    descEl.textContent = (e.desc || '').slice(0, 120);

    const toggle = document.createElement('button');
    toggle.className = 'live-expand-btn';
    toggle.textContent = '\u25be';

    toolLine.appendChild(icon);
    toolLine.appendChild(nameEl);
    toolLine.appendChild(descEl);
    toolLine.appendChild(toggle);

    const detail = document.createElement('div');
    detail.className = 'live-tool-detail';
    detail.textContent = e.desc || '';

    toolLine.onclick = () => detail.classList.toggle('open');
    div.appendChild(toolLine);
    div.appendChild(detail);

  } else if (e.kind === 'tool_result') {
    div.classList.add('live-entry-result');
    const ok = !e.is_error;
    const text = e.text || '';

    const line = document.createElement('div');
    line.className = 'live-result-line ' + (ok ? 'live-result-ok' : 'live-result-err');
    line.textContent = (ok ? '\u2713 ' : '\u2717 ') + text.slice(0, 80) + (text.length > 80 ? '\u2026' : '');

    const detail = document.createElement('div');
    detail.className = 'live-tool-detail';
    detail.textContent = text;

    line.onclick = () => detail.classList.toggle('open');
    div.appendChild(line);
    div.appendChild(detail);
  }

  return div;
}

function updateLiveInputBar() {
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
  if (stateKey === liveBarState) return;
  liveBarState = stateKey;

  if (!isRunning) {
    bar.innerHTML =
      '<div class="live-ended" style="margin-bottom:6px;">' +
      '<span style="color:#555;font-size:11px;">Session ended \u2014 start a new message to continue</span>' +
      '</div>' +
      '<textarea id="live-input-ta" class="live-textarea" rows="2" placeholder="Type a message to start a new session\u2026"' +
      ' onkeydown="if(event.key===\'Enter\'&&(event.ctrlKey||event.metaKey))liveSubmitContinue(\'' + id + '\')"></textarea>' +
      '<div class="live-bar-row">' +
      '<span style="font-size:10px;color:#444;">Ctrl+Enter to send</span>' +
      '<button class="live-send-btn" onclick="liveSubmitContinue(\'' + id + '\')">Send \u21b5</button>' +
      '</div>';
    const btnClose = document.getElementById('btn-close');
    if (btnClose) btnClose.disabled = true;
    if (_guiFocusPending) {
      _guiFocusPending = false;
      setTimeout(() => {
        const logEl = document.getElementById('live-log');
        if (logEl) logEl.scrollTop = logEl.scrollHeight;
        const ta = document.getElementById('live-input-ta');
        if (ta) ta.focus();
      }, 50);
    }

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
      questionHTML = '<div class="live-question-text">' + escHtml(display) + '</div>';
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
      ' onkeydown="if(event.key===\'Enter\'&&(event.ctrlKey||event.metaKey))liveSubmitWaiting()"></textarea>' +
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
      ' onkeydown="if(event.key===\'Enter\'&&(event.ctrlKey||event.metaKey))liveSubmitIdle()"></textarea>' +
      '<div class="live-bar-row">' +
      '<span style="font-size:10px;color:#444;">Ctrl+Enter to send</span>' +
      '<button class="live-send-btn" onclick="liveSubmitIdle()">Send \u21b5</button>' +
      '</div>';
    if (_guiFocusPending) {
      _guiFocusPending = false;
      setTimeout(() => {
        const logEl = document.getElementById('live-log');
        if (logEl) logEl.scrollTop = logEl.scrollHeight;
        const ta = document.getElementById('live-input-ta');
        if (ta) ta.focus();
      }, 50);
    }

  } else {
    bar.innerHTML =
      '<div class="live-working" style="margin-bottom:6px;"><span class="spinner"></span>Claude is working\u2026</div>' +
      '<textarea id="live-queue-ta" class="live-textarea" rows="2" ' +
      'style="opacity:0.6;" placeholder="Type your next command \u2014 will send when Claude finishes\u2026">' +
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
  ta.disabled = true;
  try {
    const ctrl = new AbortController();
    const timer = setTimeout(() => ctrl.abort(), 8000);
    const r = await fetch('/api/respond/' + liveSessionId, {
      method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({text}), signal: ctrl.signal
    });
    clearTimeout(timer);
    const d = await r.json();
    if (d.method === 'sent') {
      ta.value = '';
      setTimeout(pollWaiting, 500);
    } else if (d.method === 'clipboard') {
      showAlert('Copied to Clipboard', '<p>' + escHtml(d.message) + '</p>', { icon: '\uD83D\uDCCB' });
    } else {
      showAlert('Send Failed', '<p>' + escHtml(d.err || d.method) + '</p>', { icon: '\u26A0\uFE0F' });
    }
  } catch(e) {
    if (e.name === 'AbortError') showAlert('Timed Out', '<p>Copied to clipboard. Paste in your terminal.</p>', { icon: '\u23F1\uFE0F' });
    else showAlert('Error', '<p>' + escHtml(e.message) + '</p>', { icon: '\u26A0\uFE0F' });
  } finally {
    if (ta) ta.disabled = false;
  }
}

async function liveSubmitContinue(fromId) {
  const ta = document.getElementById('live-input-ta');
  const text = ta ? ta.value.trim() : '';
  // Continue the session (creates new session), then send the typed text
  const resp = await fetch('/api/continue/' + fromId, { method: 'POST' });
  const data = await resp.json();
  if (!data.ok) { showToast('Could not continue session'); return; }
  await loadSessions();
  _guiFocusPending = true;
  await openInGUI(data.new_id);
  if (text) {
    // Wait for the new session to start, then send the text
    setTimeout(async () => {
      const r = await fetch('/api/respond/' + data.new_id, {
        method: 'POST', headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({text})
      });
    }, 1500);
  }
}

async function liveSubmitWaiting() {
  const ta = document.getElementById('live-input-ta');
  if (!ta || !liveSessionId) return;
  const text = ta.value.trim();
  if (!text) return;
  ta.disabled = true;
  try {
    const ctrl = new AbortController();
    const timer = setTimeout(() => ctrl.abort(), 8000);
    const r = await fetch('/api/respond/' + liveSessionId, {
      method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({text}), signal: ctrl.signal
    });
    clearTimeout(timer);
    const d = await r.json();
    if (d.method === 'sent') {
      ta.value = '';
      liveBarState = null;  // force bar to re-render next poll (question -> working)
      setTimeout(pollWaiting, 500);
    } else if (d.method === 'clipboard') {
      showAlert('Copied to Clipboard', '<p>' + escHtml(d.message) + '</p>', { icon: '\uD83D\uDCCB' });
    } else {
      showAlert('Send Failed', '<p>' + escHtml(d.err || d.method) + '</p>', { icon: '\u26A0\uFE0F' });
    }
  } catch(e) {
    if (e.name === 'AbortError') showAlert('Timed Out', '<p>Response copied to clipboard. Paste in your terminal.</p>', { icon: '\u23F1\uFE0F' });
    else showAlert('Error', '<p>' + escHtml(e.message) + '</p>', { icon: '\u26A0\uFE0F' });
  } finally {
    if (ta) ta.disabled = false;
  }
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
  // Always clear GUI state and show static preview
  stopLivePanel();
  guiOpenDelete(id);
  runningIds.delete(id);
  showToast('Session closed');
  const sr = await fetch('/api/session/' + id);
  const sess = await sr.json();
  document.getElementById('main-body').innerHTML =
    '<div class="conversation" id="convo">' + renderMessages(sess.messages) + '</div>';
  setTimeout(() => { const c = document.getElementById('convo'); if (c) c.scrollTop = c.scrollHeight; }, 50);
  filterSessions();
}
