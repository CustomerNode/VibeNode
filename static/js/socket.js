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
        // Store working_since for elapsed timer — keyed per session
        // so it's available when the user opens the live panel later
        if (s.working_since && s.state === 'working') {
            if (!window._workingSinceMap) window._workingSinceMap = {};
            window._workingSinceMap[id] = s.working_since * 1000;
            if (id === liveSessionId) _liveWorkingStart = s.working_since * 1000;
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

    // Sync server-side queue cache from snapshot
    if (typeof _sessionQueues !== 'undefined') {
        for (const k in _sessionQueues) delete _sessionQueues[k];
        // Prefer top-level queues dict; fall back to per-session queue field
        if (data.queues) {
            for (const k in data.queues) {
                if (Array.isArray(data.queues[k]) && data.queues[k].length) {
                    _sessionQueues[k] = data.queues[k];
                }
            }
        } else {
            (data.sessions || []).forEach(s => {
                if (s.queue && Array.isArray(s.queue) && s.queue.length) {
                    _sessionQueues[s.session_id] = s.queue;
                }
            });
        }
        _queueViewIndex = 0;
        if (typeof _renderQueueBanner === 'function') _renderQueueBanner();
    }

    // Update Close Session button enabled state
    const btnClose = document.getElementById('btn-close');
    if (btnClose && activeId) btnClose.disabled = !newRunning.has(activeId) && !guiOpenSessions.has(activeId);
});

// Incremental state updates
socket.on('session_state', (data) => {
    const {session_id, state, cost_usd, error, name, model, working_since} = data;

    // Sync server-side working_since for elapsed timer (survives refresh)
    if (!window._workingSinceMap) window._workingSinceMap = {};
    if (working_since && state === 'working') {
        window._workingSinceMap[session_id] = working_since * 1000;
        if (session_id === liveSessionId) _liveWorkingStart = working_since * 1000;
    } else if (state !== 'working') {
        delete window._workingSinceMap[session_id];
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

    // Sync queue cache from server state event (authoritative source).
    // Every session_state event now carries the current queue from the server.
    // Auto-dispatch is handled server-side in SessionManager._emit_state.
    if (typeof _sessionQueues !== 'undefined') {
        if (data.queue && Array.isArray(data.queue) && data.queue.length) {
            _sessionQueues[session_id] = data.queue;
        } else {
            delete _sessionQueues[session_id];
        }
        _queueViewIndex = 0;
    }
    if (session_id === liveSessionId && typeof _renderQueueBanner === 'function') {
        _renderQueueBanner();
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
    // Hard dedup via shared Set + DOM check
    if (data.entry.kind === 'user') {
        const key = (data.entry.text || '').trim();
        if (_renderedUserTexts.has(key)) {
            liveLineCount = (data.index != null) ? data.index + 1 : liveLineCount + 1;
            return;
        }
        // DOM-level dedup: check if an optimistic bubble already shows this text
        const _lu = logEl.querySelector('.msg.user:last-child .msg-body');
        if (_lu && _lu.textContent.trim() === key) {
            _renderedUserTexts.add(key);
            liveLineCount = (data.index != null) ? data.index + 1 : liveLineCount + 1;
            return;
        }
        _renderedUserTexts.add(key);
    }
    logEl.appendChild(renderLiveEntry(data.entry));
    liveLineCount = (data.index != null) ? data.index + 1 : liveLineCount + 1;
    if (typeof _updateLastMessageTimes === 'function') _updateLastMessageTimes();
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

// Server-side queue updates — replaces client-side localStorage queue
socket.on('queue_updated', (data) => {
    const sid = data.session_id;
    const items = data.queue || [];
    // Update local cache from authoritative server data
    if (items.length) {
        _sessionQueues[sid] = items;
    } else {
        delete _sessionQueues[sid];
    }
    _queueViewIndex = 0;
    // Re-render queue banner if viewing this session
    if (sid === liveSessionId && typeof _renderQueueBanner === 'function') {
        _renderQueueBanner();
    }
});

// Server auto-dispatched a queued message — show toast + optimistic user bubble
socket.on('queue_dispatched', (data) => {
    const sid = data.session_id;
    const text = data.text || '';
    const remaining = data.remaining || 0;
    showToast('Sending queued command\u2026' + (remaining ? ' (' + remaining + ' remaining)' : ''));
    // Add optimistic user bubble for the dispatched message
    if (sid === liveSessionId && text && typeof _addOptimisticBubble === 'function') {
        _addOptimisticBubble(sid, text);
    }
});

// Session started confirmation
socket.on('session_started', (data) => {
    if (data.session_id) {
        runningIds.add(data.session_id);
        // Don't overwrite optimistic 'working' state (set before start_session emit)
        if (sessionKinds[data.session_id] !== 'working') {
            sessionKinds[data.session_id] = 'idle';
        }
        _updateRowState(data.session_id, sessionKinds[data.session_id] || 'idle');
    }
});

// Session ID remapped — SDK assigned a different ID than the one we generated
socket.on('session_id_remapped', (data) => {
    const oldId = data.old_id;
    const newId = data.new_id;
    if (!oldId || !newId) return;
    console.log('[WS] Session ID remapped:', oldId, '->', newId);

    // Update allSessions array
    const s = allSessions.find(x => x.id === oldId);
    if (s) s.id = newId;

    // Update activeId and URL — use replaceState (not pushState) so the
    // temporary client-generated UUID does not linger in browser history
    // and break back/forward/refresh navigation
    if (activeId === oldId) {
        activeId = newId;
        localStorage.setItem('activeSessionId', newId);
        const _remapUrl = new URL(window.location);
        _remapUrl.searchParams.set('chat', newId);
        history.replaceState({
            folder: (typeof _currentFolderId !== 'undefined' ? _currentFolderId : null),
            chat: newId
        }, '', _remapUrl);
    }

    // Update liveSessionId
    if (liveSessionId === oldId) liveSessionId = newId;

    // Update runningIds
    if (runningIds.has(oldId)) {
        runningIds.delete(oldId);
        runningIds.add(newId);
    }

    // Update sessionKinds
    if (sessionKinds[oldId] !== undefined) {
        sessionKinds[newId] = sessionKinds[oldId];
        delete sessionKinds[oldId];
    }

    // Update waitingData
    if (waitingData[oldId]) {
        waitingData[newId] = waitingData[oldId];
        delete waitingData[oldId];
    }

    // Update guiOpenSessions
    if (guiOpenSessions.has(oldId)) {
        guiOpenDelete(oldId);
        guiOpenAdd(newId);
    }

    // Update _userNamedSessions (remap user-set name protection to new ID)
    if (typeof _userNamedSessions !== 'undefined' && _userNamedSessions.has(oldId)) {
        _userNamedSessions.delete(oldId);
        _userNamedSessions.add(newId);
        // Re-persist the name under the new UUID on the server so it survives page refresh
        fetch('/api/remap-name', {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({old_id:oldId, new_id:newId})}).catch(()=>{});
    }

    // Update working since map
    if (window._workingSinceMap && window._workingSinceMap[oldId]) {
        window._workingSinceMap[newId] = window._workingSinceMap[oldId];
        delete window._workingSinceMap[oldId];
    }

    // Update folder tree mapping (workplace mode)
    if (typeof _remapSessionInFolders === 'function') {
        _remapSessionInFolders(oldId, newId);
    }

    // Update toolbar data attribute
    setToolbarSession(newId,
        s ? (s.custom_title || s.display_title) : 'New Session',
        s ? !s.custom_title : true,
        s ? (s.custom_title || '') : '');

    // Re-render sidebar
    filterSessions();

    // Re-schedule auto-naming with the new ID (the old setTimeout closures
    // captured the old ID, which won't match any .jsonl file on disk).
    // Skip if the user has manually named this session.
    if (typeof autoName === 'function' && !(typeof _userNamedSessions !== 'undefined' && _userNamedSessions.has(newId))) {
        setTimeout(() => autoName(newId, true), 3000);
    }
});

// Session log response (for panel open)
socket.on('session_log', (data) => {
    if (data.session_id !== liveSessionId) return;
    const logEl = document.getElementById('live-log');
    if (!logEl) return;
    logEl.innerHTML = '';
    _renderedUserTexts.clear();
    if (data.entries && data.entries.length) {
        data.entries.forEach((entry, i) => {
            // Register user texts so session_entry dedup stays in sync
            if (entry.kind === 'user' && entry.text) {
                const key = entry.text.trim();
                if (_renderedUserTexts.has(key)) return; // skip dupe in log itself
                _renderedUserTexts.add(key);
            }
            logEl.appendChild(renderLiveEntry(entry));
        });
        liveLineCount = data.entries.length;
    } else {
        liveLineCount = 0;
    }
    if (typeof _updateLastMessageTimes === 'function') _updateLastMessageTimes();
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
