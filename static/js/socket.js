/* socket.js — WebSocket (Socket.IO) event handling, replaces polling.js */

const socket = io();
let _wsConnected = false;

socket.on('connect', () => {
    _wsConnected = true;
    console.log('[WS] Connected');
    // Update status bar connection indicator
    const sbConn = document.getElementById('sb-connection');
    if (sbConn) { sbConn.textContent = '\u25CF'; sbConn.style.color = 'var(--idle-label)'; sbConn.title = 'Connected'; }
    // Sync permission policy to backend on connect
    if (typeof permissionPolicy !== 'undefined') {
        socket.emit('set_permission_policy', { policy: permissionPolicy, customRules: customPolicies || {} });
    }
});

socket.on('disconnect', () => {
    _wsConnected = false;
    console.log('[WS] Disconnected');
    // Update status bar connection indicator
    const sbConn = document.getElementById('sb-connection');
    if (sbConn) { sbConn.textContent = '\u25CF'; sbConn.style.color = 'var(--result-err)'; sbConn.title = 'Disconnected'; }
});

// Server-side error messages
socket.on('error', (data) => {
    const msg = (data && data.message) || 'Unknown error';
    console.error('[WS] Server error:', msg, data && data.session_id ? '(session: ' + data.session_id + ')' : '');
    showToast(msg, true);
});

// Full state snapshot on connect
socket.on('state_snapshot', (data) => {
    const newWaiting = {};
    const newRunning = new Set();
    const newKinds = {};

    (data.sessions || []).forEach(s => {
        const id = s.session_id;
        // Restore working_since for elapsed timer on refresh
        if (s.working_since && s.state === 'working' && id === liveSessionId) {
            _liveWorkingStart = s.working_since * 1000;
        }
        if (s.state === 'waiting') {
            newKinds[id] = 'question';
            if (s.permission) {
                newWaiting[id] = {
                    question: _formatPermissionQuestion(s.permission.tool_name, s.permission.tool_input),
                    options: ['y', 'n', 'a'],
                    kind: 'tool',
                    tool_name: s.permission.tool_name,
                    tool_input: s.permission.tool_input,
                };
            }
        } else if (s.state === 'working' || s.state === 'starting') {
            newKinds[id] = 'working';
        } else if (s.state === 'idle') {
            newKinds[id] = 'idle';
        }
        if (s.state !== 'stopped') newRunning.add(id);
    });

    waitingData = newWaiting;
    runningIds = newRunning;
    sessionKinds = newKinds;

    // Update sidebar row classes
    document.querySelectorAll('.session-item[data-sid]').forEach(row => {
        const id = row.dataset.sid;
        row.classList.remove('si-question', 'si-working', 'si-idle');
        if (newRunning.has(id)) row.classList.add('si-' + (newKinds[id] || 'working'));
    });

    // Clean up guiOpenSessions: if a session the user previously opened in GUI
    // is no longer reported as running by the server, stop treating it as idle.
    // Exception: keep the live panel session.
    guiOpenSessions.forEach(id => {
        if (!newRunning.has(id) && id !== liveSessionId) {
            guiOpenDelete(id);
        }
    });

    // If currently showing a popup for a session that is no longer waiting, close it
    if (respondTarget && !waitingData[respondTarget]) closeRespond();

    // Re-render views
    filterSessions();

    // Update live panel input bar state
    if (liveSessionId) updateLiveInputBar();

    // Refresh dashboard if no session selected (but not in workplace mode —
    // workplace owns main-body and the dashboard would clobber the workspace canvas)
    if (!activeId && !workspaceActive) {
        const dash = document.querySelector('.dashboard');
        if (dash) document.getElementById('main-body').innerHTML = _buildDashboard();
    }

    // Update workspace permission queue after state refresh
    // Auto-approve policies are global — run regardless of view mode
    if (typeof _updatePermissionQueue === 'function') {
        _updatePermissionQueue(waitingData);
    }

    // Update Close Session button enabled state
    const btnClose = document.getElementById('btn-close');
    if (btnClose && activeId) btnClose.disabled = !newRunning.has(activeId) && !guiOpenSessions.has(activeId);
});

// Incremental state updates
socket.on('session_state', (data) => {
    const {session_id, state, cost_usd, error, name, model, working_since} = data;

    // Sync server-side working_since for elapsed timer (survives refresh)
    if (working_since && state === 'working') {
        _liveWorkingStart = working_since * 1000; // server sends seconds, JS uses ms
    } else if (state !== 'working') {
        if (session_id === liveSessionId) _liveWorkingStart = null;
    }

    // Map SDK states to existing UI state names
    if (state === 'waiting') {
        sessionKinds[session_id] = 'question';
    } else if (state === 'working' || state === 'starting') {
        sessionKinds[session_id] = 'working';
    } else if (state === 'idle') {
        sessionKinds[session_id] = 'idle';
    } else if (state === 'stopped') {
        delete sessionKinds[session_id];
    }

    if (state === 'stopped') {
        runningIds.delete(session_id);
    } else {
        runningIds.add(session_id);
    }

    // Update cost display if this is the active session
    if (session_id === activeId && cost_usd != null) {
        const costEl = document.getElementById('session-cost');
        if (costEl) costEl.textContent = '$' + Number(cost_usd).toFixed(4);
        const sbCost = document.getElementById('sb-cost');
        if (sbCost) sbCost.textContent = '$' + Number(cost_usd).toFixed(4);
    }

    // Update status bar model display
    if (session_id === activeId && model) {
        const sbModel = document.getElementById('sb-model');
        if (sbModel) sbModel.textContent = model;
    }

    if (state !== 'waiting') {
        delete waitingData[session_id];
        // Update workspace permission queue if permission was cleared
        if (workspaceActive && typeof _updatePermissionQueue === 'function') {
            _updatePermissionQueue(waitingData);
        }
    }

    // Auto-send queued input when Claude transitions from working -> idle
    if (liveSessionId === session_id && liveQueuedText && state === 'idle') {
        const textToSend = liveQueuedText;
        liveQueuedText = '';
        showToast('Sending queued command\u2026');
        socket.emit('send_message', {session_id: session_id, text: textToSend});
    }

    // Update sidebar row classes
    _updateRowState(session_id, state);

    // If currently showing a popup for a session that is no longer waiting, close it
    if (respondTarget === session_id && !waitingData[session_id]) closeRespond();

    // Refresh active views
    if (viewMode === 'workforce' || viewMode === 'workplace') filterSessions();
    if (liveSessionId === session_id) {
        liveBarState = null;  // force re-render
        updateLiveInputBar();
        // Scroll to bottom on state change (working bar appears/disappears)
        const _logEl = document.getElementById('live-log');
        if (_logEl && liveAutoScroll) setTimeout(() => { _logEl.scrollTop = _logEl.scrollHeight; }, 100);
    }

    // Refresh dashboard if no session selected (skip in workplace mode)
    if (!activeId && !workspaceActive) {
        const dash = document.querySelector('.dashboard');
        if (dash) document.getElementById('main-body').innerHTML = _buildDashboard();
    }

    // Update Close Session button enabled state
    const btnClose = document.getElementById('btn-close');
    if (btnClose && activeId === session_id) {
        btnClose.disabled = state === 'stopped' && !guiOpenSessions.has(session_id);
    }
});

// Live log entries pushed in real-time
socket.on('session_entry', (data) => {
    if (data.session_id !== liveSessionId) return;
    if (!data.entry) return;
    const logEl = document.getElementById('live-log');
    if (!logEl) return;
    // Clear skeleton/placeholder on first real entry
    if (liveLineCount === 0) {
        const skel = logEl.querySelector('.skel-bar, .skeleton-loader, .live-log-empty, .empty-state');
        if (skel) logEl.innerHTML = '';
    }
    // Skip user entries that were already added as optimistic bubbles.
    // The optimistic bubble is added immediately when the user sends a message,
    // and the backend echoes it back as a session_entry. Deduplicate by checking
    // if the last user message in the log matches the incoming text.
    if (data.entry.kind === 'user' && _liveSending) {
        const userMsgs = logEl.querySelectorAll('.msg.user');
        const lastUserMsg = userMsgs.length ? userMsgs[userMsgs.length - 1] : null;
        if (lastUserMsg) {
            const bodyEl = lastUserMsg.querySelector('.msg-body');
            if (bodyEl && bodyEl.textContent.trim() === (data.entry.text || '').trim()) {
                liveLineCount = (data.index != null) ? data.index + 1 : liveLineCount + 1;
                return;  // skip duplicate
            }
        }
    }
    logEl.appendChild(renderLiveEntry(data.entry));
    liveLineCount = (data.index != null) ? data.index + 1 : liveLineCount + 1;
    if (liveAutoScroll) {
        logEl.scrollTop = logEl.scrollHeight;
    }
});

// Permission requests
socket.on('session_permission', (data) => {
    waitingData[data.session_id] = {
        question: _formatPermissionQuestion(data.tool_name, data.tool_input),
        options: ['y', 'n', 'a'],
        kind: 'tool',
        tool_name: data.tool_name,
        tool_input: data.tool_input,
    };
    sessionKinds[data.session_id] = 'question';

    if (liveSessionId === data.session_id) {
        liveBarState = null;
        updateLiveInputBar();
    }
    // Update sidebar
    _updateRowState(data.session_id, 'waiting');
    // Update workspace permission queue if active
    // Auto-approve policies are global — run regardless of view mode
    if (typeof _updatePermissionQueue === 'function') {
        _updatePermissionQueue(waitingData);
    }
    if (viewMode === 'workforce' || viewMode === 'workplace') filterSessions();
});

// Session started confirmation
socket.on('session_started', (data) => {
    if (data.session_id) {
        runningIds.add(data.session_id);
        sessionKinds[data.session_id] = 'idle';
        _updateRowState(data.session_id, 'idle');
    }
});

// Session log response (for panel open)
socket.on('session_log', (data) => {
    if (data.session_id !== liveSessionId) return;
    const logEl = document.getElementById('live-log');
    if (!logEl) return;
    logEl.innerHTML = '';
    if (data.entries && data.entries.length) {
        data.entries.forEach((entry, i) => {
            logEl.appendChild(renderLiveEntry(entry));
        });
        liveLineCount = data.entries.length;
    } else {
        liveLineCount = 0;
    }
    if (liveAutoScroll) logEl.scrollTop = logEl.scrollHeight;
});

// Helper: format permission question for display
function _formatPermissionQuestion(toolName, toolInput) {
    let desc = '';
    if (!toolInput) {
        desc = '(no details)';
    } else if (toolInput.command) {
        desc = toolInput.command;
    } else if (toolInput.file_path) {
        desc = toolInput.file_path;
    } else if (toolInput.path) {
        desc = toolInput.path;
    } else if (toolInput.pattern) {
        desc = toolInput.pattern;
    } else {
        desc = JSON.stringify(toolInput).slice(0, 200);
    }
    return 'Claude wants to use ' + toolName + ':\n\n' + desc;
}

// Helper: update sidebar row classes
function _updateRowState(sessionId, state) {
    const row = document.querySelector('.session-item[data-sid="' + sessionId + '"]');
    if (!row) return;
    row.classList.remove('si-question', 'si-working', 'si-idle');
    if (state === 'waiting') row.classList.add('si-question');
    else if (state === 'working' || state === 'starting') row.classList.add('si-working');
    else if (state === 'idle') row.classList.add('si-idle');
}

// Helper: get the active project's filesystem path
function _currentProjectDir() {
    const encoded = localStorage.getItem('activeProject');
    if (!encoded) return '';
    const p = _allProjects.find(x => x.encoded === encoded);
    return p ? p.display : '';
}

// Replace pokeWaiting - no longer needed but keep as no-op for compatibility
function pokeWaiting() {
    // WebSocket push makes this unnecessary
}

// ---- Startup ----
loadProjects();
pollGitStatus();
setInterval(pollGitStatus, 60000);
// Initialize folder tree from server (shows template selector on first run)
if (typeof initFolderTree === 'function') {
  initFolderTree().catch(function(e) { console.error('initFolderTree failed', e); });
}
