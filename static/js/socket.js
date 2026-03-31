/* socket.js — WebSocket (Socket.IO) event handling, replaces polling.js */

const socket = io();
let _wsConnected = false;

/**
 * Returns true if a session ID belongs to a hidden utility session
 * (planner, auto-title, etc.) that must NEVER appear in the workspace.
 * Called at every entry point where a session can enter the UI.
 */
function _isHiddenSession(id, data) {
    if (!id) return false;
    if (id.startsWith('_title_')) return true;
    if (window._plannerSessionIds && window._plannerSessionIds.has(id)) return true;
    if (data && (data.session_type === 'planner' || data.session_type === 'title')) return true;
    return false;
}

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
    // Refresh session list on reconnect so new/changed sessions appear
    if (typeof loadSessions === 'function') {
        loadSessions();
    }
    // Request full state snapshot to resync indicators
    socket.emit('request_state_snapshot');
    // Retry after 3s in case the first snapshot was silently dropped
    // (e.g. DaemonClient not yet reconnected when server just restarted)
    setTimeout(() => {
        if (socket.connected) socket.emit('request_state_snapshot');
    }, 3000);
});

socket.on('disconnect', () => {
    _wsConnected = false;
    console.log('[WS] Disconnected');
    // Clear staleness timestamps so reconnect snapshot is authoritative
    window._sessionStateTs = {};
    // Update status bar connection indicator
    const sbConn = document.getElementById('sb-connection');
    if (sbConn) { sbConn.textContent = '\u25CF'; sbConn.style.color = 'var(--result-err)'; sbConn.title = 'Disconnected'; }
});

// Server-side error messages
socket.on('error', (data) => {
    const msg = (data && data.message) || 'Unknown error';
    const errSid = data && data.session_id;
    console.error('[WS] Server error:', msg, errSid ? '(session: ' + errSid + ')' : '');
    showToast(msg, true);

    // Reset UI state for the affected session so the user isn't stuck on a
    // "working" bar that will never resolve. Force a state resync to get the
    // real state from the server instead of guessing.
    if (errSid) {
        if (typeof _cancelMessageWatchdog === 'function') _cancelMessageWatchdog(errSid);
        // Request authoritative state from the server via WS
        socket.emit('request_state_snapshot');
        // Also do an HTTP check as ultimate fallback (bypasses WS entirely)
        if (typeof _watchdogHttpCheck === 'function') {
            setTimeout(() => _watchdogHttpCheck(errSid, true), 2000);
        }
        // Immediately revert to idle so the user can at least retry.
        // The snapshot/HTTP check will correct this within a moment.
        if (sessionKinds[errSid] === 'working') {
            sessionKinds[errSid] = 'idle';
            if (errSid === liveSessionId) {
                liveBarState = null;
                if (typeof updateLiveInputBar === 'function') updateLiveInputBar();
            }
        }
    }
});

// Full state snapshot on connect
// Track when each session last got an incremental state update so
// heartbeat snapshots don't revert fresher data.
if (!window._sessionStateTs) window._sessionStateTs = {};

socket.on('state_snapshot', (data) => {
    const snapTime = Date.now();
    const newWaiting = {};
    const newRunning = new Set();
    const newKinds = {};

    // Rebuild working_since map from scratch so stale entries are cleared
    // (fixes timers stuck ticking after a missed WORKING->IDLE event)
    window._workingSinceMap = {};
    if (!window._sessionSubstatus) window._sessionSubstatus = {};
    if (!window._sessionUsage) window._sessionUsage = {};

    (data.sessions || []).forEach(s => {
        const id = s.session_id;
        if (_isHiddenSession(id, s)) return;
        // Skip this session if we got a fresher incremental update
        // in the last 5 seconds (avoids heartbeat snapshot reverting
        // a real-time session_state event that arrived after the
        // snapshot was built on the server).
        const lastTs = window._sessionStateTs[id] || 0;
        if (lastTs > snapTime - 5000) {
            // Preserve existing state for this session
            if (sessionKinds[id]) newKinds[id] = sessionKinds[id];
            if (runningIds.has(id)) newRunning.add(id);
            if (waitingData[id]) newWaiting[id] = waitingData[id];
            if (window._workingSinceMap[id]) {
                // keep existing — don't clear
            }
            return; // skip snapshot data for this session
        }
        // Sync substatus and usage from snapshot
        if (s.substatus) {
            window._sessionSubstatus[id] = s.substatus;
        } else {
            delete window._sessionSubstatus[id];
        }
        if (s.usage) {
            window._sessionUsage[id] = s.usage;
        }
        // Store working_since for elapsed timer — keyed per session
        // so it's available when the user opens the live panel later
        if (s.working_since && s.state === 'working') {
            window._workingSinceMap[id] = s.working_since * 1000;
            if (id === liveSessionId) _liveWorkingStart = s.working_since * 1000;
        } else if (id === liveSessionId) {
            _liveWorkingStart = null;
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

    // Detect working→idle transition for the live session before replacing
    // state. If we missed the real-time session_state/session_entry events
    // (SocketIO transport hiccup, tab sleeping, etc.), entries would be
    // missing from the DOM. Re-fetch them so the response appears without
    // requiring a manual page refresh.
    if (liveSessionId && sessionKinds[liveSessionId] === 'working' &&
        newKinds[liveSessionId] && newKinds[liveSessionId] !== 'working') {
        console.warn('[state_snapshot] Live session', liveSessionId,
            'transitioned from working →', newKinds[liveSessionId],
            '— re-fetching entries in case real-time events were lost');
        socket.emit('get_session_log', {session_id: liveSessionId, since: 0});
    }

    waitingData = newWaiting;
    runningIds = newRunning;
    sessionKinds = newKinds;

    // Populate _idRemaps from server aliases so kanban and other code
    // can resolve old→new session IDs even after a page refresh
    if (data.aliases) {
        if (!window._idRemaps) window._idRemaps = {};
        for (const oldId in data.aliases) {
            window._idRemaps[oldId] = data.aliases[oldId];
        }
    }

    // Purge stale alias entries — if a session was remapped (old→new), remove
    // the old-ID entry so it doesn't duplicate the new-ID entry.
    if (window._idRemaps) {
        allSessions = allSessions.filter(s => !window._idRemaps[s.id]);
    }

    // Inject stub entries into allSessions for SDK-managed sessions that
    // haven't written a .jsonl yet (e.g. first response still in progress).
    // Without this, sessions disappear from the sidebar on page refresh
    // until their first .jsonl flush completes.
    let _injectedStubs = false;
    (data.sessions || []).forEach(s => {
        const id = s.session_id;
        if (s.state === 'stopped') return;
        if (_isHiddenSession(id, s)) return;
        // Skip old pre-remap IDs that have already been replaced
        if (window._idRemaps && window._idRemaps[id]) return;
        if (!allSessions.find(x => x.id === id)) {
            // Don't inject stub if an old alias for this ID still exists
            if (window._idRemaps) {
                let _hasOld = false;
                for (const oldId in window._idRemaps) {
                    if (window._idRemaps[oldId] === id && allSessions.find(x => x.id === oldId)) {
                        _hasOld = true; break;
                    }
                }
                if (_hasOld) return;
            }
            allSessions.unshift({
                id: id,
                display_title: s.name || 'New Session',
                custom_title: s.name || '',
                last_activity: '',
                last_activity_ts: Date.now() / 1000,
                sort_ts: Date.now() / 1000,
                size: '',
                file_bytes: 0,
                message_count: 0,
                preview: '',
            });
            _injectedStubs = true;
        }
    });

    // Final dedup — guarantee no two entries share the same ID
    {
        const _seen = new Set();
        allSessions = allSessions.filter(s => {
            if (_seen.has(s.id)) return false;
            _seen.add(s.id);
            return true;
        });
    }

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

    // Session restoration is handled by loadSessions() + /api/resolve-session.
    // Do NOT call openInGUI here — it races with loadSessions and causes
    // double-initialization of the live panel, dropping messages.

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
    if (_isHiddenSession(session_id, data)) return;
    const substatus = data.substatus || '';
    const usage = data.usage || null;

    // Cancel watchdog on definitive state changes (idle/stopped/waiting).
    // For working/starting events, RESET the watchdog so it keeps monitoring
    // for the final IDLE event (which can be silently lost).
    if (state !== 'working' && state !== 'starting') {
        if (typeof _cancelMessageWatchdog === 'function') _cancelMessageWatchdog(session_id);
    } else {
        if (typeof _resetMessageWatchdog === 'function') _resetMessageWatchdog(session_id);
    }

    // Stamp receipt time so heartbeat snapshots don't revert this
    if (!window._sessionStateTs) window._sessionStateTs = {};
    window._sessionStateTs[session_id] = Date.now();

    // Track substatus (e.g. "compacting") per session.
    // Don't clear an optimistic "compacting" substatus when a WORKING
    // state event arrives without substatus — compact_boundary confirms it.
    if (!window._sessionSubstatus) window._sessionSubstatus = {};
    if (substatus) {
        window._sessionSubstatus[session_id] = substatus;
    } else if (state !== 'working' || window._sessionSubstatus[session_id] !== 'compacting') {
        delete window._sessionSubstatus[session_id];
    }

    // Track token usage per session for context window indicator
    if (!window._sessionUsage) window._sessionUsage = {};
    if (usage) {
        window._sessionUsage[session_id] = usage;
    }

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

    // Refresh active views — always call filterSessions so sidebar icons
    // update for substatus changes (e.g. compacting indicator)
    filterSessions();
    if (liveSessionId === session_id) {
        liveBarState = null;  // force re-render
        updateLiveInputBar();
        // Scroll to bottom on state change (working bar appears/disappears)
        const _logEl = document.getElementById('live-log');
        if (_logEl && liveAutoScroll) setTimeout(() => { _logEl.scrollTop = _logEl.scrollHeight; }, 100);

        // Self-healing: check if frontend is missing entries vs backend.
        if (state === 'idle' || state === 'stopped') {
            const sc = data.entry_count;
            if (sc != null && sc > liveLineCount) {
                console.warn('[entry-catchup] Backend has', sc, 'entries but frontend has', liveLineCount);
                socket.emit('get_session_log', {session_id: session_id, since: 0});
            } else {
                setTimeout(() => {
                    if (liveSessionId === session_id) {
                        socket.emit('get_session_log', {session_id: session_id, since: 0});
                    }
                }, 500);
            }
        }
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

    // ── Kanban task-session state bridge ──
    // When a session changes state, check if it's linked to a kanban task
    // and trigger the appropriate task state machine transition.
    if (state === 'working' || state === 'idle' || state === 'stopped') {
        fetch('/api/kanban/session-state-change', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ session_id: session_id, state: state }),
        }).catch(() => { /* ignore — kanban bridge is best-effort */ });
    }

    // ── Update kanban drill-down session badges in-place ──
    const sessRow = document.querySelector('.kanban-drill-session-row[data-session-id="' + session_id + '"]');
    if (sessRow) {
        const badge = sessRow.querySelector('.kanban-drill-session-badge');
        if (badge) {
            const label = state === 'working' ? 'Working' : state === 'idle' ? 'Idle' : 'Sleeping';
            badge.textContent = label;
            if (state === 'working') badge.style.cssText = 'background:var(--status-working-dim);color:var(--status-working);';
            else if (state === 'idle') badge.style.cssText = 'background:var(--status-complete-dim);color:var(--status-complete);';
            else badge.style.cssText = 'background:var(--status-not-started-dim);color:var(--text-dim);';
        }
        // Update row highlight
        if (state === 'working') {
            sessRow.style.background = 'var(--status-working-dim)';
            sessRow.style.border = '1px solid var(--status-working)';
            sessRow.style.borderRadius = '6px';
        } else {
            sessRow.style.background = '';
            sessRow.style.border = '';
            sessRow.style.borderRadius = '';
        }
    }
});

// Server confirms it received and accepted our send_message.
// This is the positive acknowledgment that the message pipeline is working.
// Reset the watchdog — we know the server got it, so events should follow.
socket.on('message_ack', (data) => {
    if (!data || !data.session_id) return;
    console.log('[WS] message_ack for', data.session_id, data.queued ? '(queued)' : '');
    // Reset watchdog timer — server confirmed receipt, give it more time
    // for the first response event to arrive
    if (typeof _resetMessageWatchdog === 'function') _resetMessageWatchdog(data.session_id);
});

// Lightweight per-call token usage updates (from StreamEvent message_start).
// This gives us the REAL context window size, not cumulative session totals.
socket.on('session_usage', (data) => {
    if (!data || !data.session_id || !data.usage) return;
    // Reset (not cancel) watchdog — usage data means response is flowing
    // but session hasn't completed yet.  Keep monitoring for IDLE.
    if (typeof _resetMessageWatchdog === 'function') _resetMessageWatchdog(data.session_id);
    if (!window._sessionUsage) window._sessionUsage = {};
    window._sessionUsage[data.session_id] = data.usage;
    // Force re-render of input bar so ctx % updates (clear stateKey cache)
    if (data.session_id === liveSessionId) {
        liveBarState = null;
        if (typeof updateLiveInputBar === 'function') updateLiveInputBar();
    }
});

// Live log entries pushed in real-time
socket.on('session_entry', (data) => {
    // Reset (not cancel) watchdog — data is flowing but session hasn't
    // completed yet.  Keep the watchdog alive so a lost IDLE event triggers
    // recovery within 10s of the last streaming activity.
    if (data.session_id && typeof _resetMessageWatchdog === 'function') _resetMessageWatchdog(data.session_id);

    // Consistency check: if we're getting entries for a session the UI
    // thinks is idle/stopped, our state is stale — request a refresh.
    // This catches the case where the WORKING event was silently lost.
    if (data.session_id && sessionKinds[data.session_id] !== 'working' && sessionKinds[data.session_id] !== 'question') {
        const _entryKind = data.entry && data.entry.kind;
        // Only for assistant/tool content (not user echoes which can arrive after idle)
        if (_entryKind === 'asst' || _entryKind === 'tool_use' || _entryKind === 'tool_result') {
            console.warn('[entry-consistency] Got', _entryKind, 'entry for', data.session_id,
                'but UI thinks state is', sessionKinds[data.session_id] || 'unknown', '— requesting state refresh');
            sessionKinds[data.session_id] = 'working';
            runningIds.add(data.session_id);
            if (data.session_id === liveSessionId) {
                liveBarState = null;
                if (typeof updateLiveInputBar === 'function') updateLiveInputBar();
            }
            _updateRowState(data.session_id, 'working');
            socket.emit('request_state_snapshot');
        }
    }

    // Track highest server entry index for periodic sync
    if (data.session_id && data.index != null) {
        if (!window._srvIdx) window._srvIdx = {};
        window._srvIdx[data.session_id] = data.index;
    }
    if (data.session_id !== liveSessionId) {
        console.debug('[entry] sid mismatch:', data.session_id, '!=', liveSessionId);
        return;
    }
    if (!data.entry) return;
    const logEl = document.getElementById('live-log');
    if (!logEl) {
        console.warn('[entry-drop] live-log not in DOM! kind:', data.entry.kind, 'idx:', data.index);
        return;
    }
    // Clear skeleton/placeholder on first real entry
    if (liveLineCount === 0) {
        const skel = logEl.querySelector('.skel-bar, .skeleton-loader, .live-log-empty, .empty-state');
        if (skel) logEl.innerHTML = '';
    }
    // Index-based dedup: skip entries we've already rendered
    if (data.index != null && data.index < liveLineCount) {
        console.debug('[entry-dedup] idx', data.index, '<', liveLineCount);
        return;
    }
    // DOM-level dedup for user messages: the frontend renders an optimistic
    // bubble immediately on send. When the server echoes it back, check if
    // the last user bubble already shows this text to avoid a double-bubble.
    if (data.entry.kind === 'user') {
        const key = (data.entry.text || '').trim();
        const _lu = logEl.querySelector('.msg.user:last-child .msg-body');
        if (_lu && _lu.textContent.trim() === key) {
            liveLineCount = (data.index != null) ? data.index + 1 : liveLineCount + 1;
            return;
        }
    }
    logEl.appendChild(renderLiveEntry(data.entry));
    if (typeof _tryAddOutputCard === 'function') _tryAddOutputCard(data.entry);
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

// System messages from SDK (debug/info)
socket.on('system_message', (data) => {
    console.log('[WS] SystemMessage:', data.subtype, data.data);
});

// Session started confirmation
socket.on('session_started', (data) => {
    if (data.session_id) {
        if (_isHiddenSession(data.session_id, data)) return;
        runningIds.add(data.session_id);
        // Don't overwrite optimistic 'working' state (set before start_session emit)
        if (sessionKinds[data.session_id] !== 'working') {
            sessionKinds[data.session_id] = 'idle';
        }
        // Ensure session exists in allSessions (may be missing after page refresh
        // if .jsonl hasn't been written yet)
        if (!allSessions.find(x => x.id === data.session_id)) {
            allSessions.unshift({
                id: data.session_id,
                display_title: data.name || 'New Session',
                custom_title: data.name || '',
                last_activity: '',
                last_activity_ts: Date.now() / 1000,
                sort_ts: Date.now() / 1000,
                size: '',
                file_bytes: 0,
                message_count: 0,
                preview: '',
            });
            filterSessions();
        }
        _updateRowState(data.session_id, sessionKinds[data.session_id] || 'idle');
    }
});

// Track old→new ID remaps so in-flight autoname calls can save under the new ID
if (!window._idRemaps) window._idRemaps = {};  // oldId -> newId

// Session ID remapped — SDK assigned a different ID than the one we generated
socket.on('session_id_remapped', (data) => {
    const oldId = data.old_id;
    const newId = data.new_id;
    if (!oldId || !newId) return;
    console.log('[WS] Session ID remapped:', oldId, '->', newId);

    if (_isHiddenSession(oldId, data)) return;

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
    }
    // Always remap the persisted name (covers both user-set AND auto-named titles).
    // Without this, auto-names saved under the old client-generated UUID are lost
    // when the SDK remaps to a new ID.
    window._idRemaps[oldId] = newId;
    fetch('/api/remap-name', {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({old_id:oldId, new_id:newId})}).catch(()=>{});

    // Update working since map
    if (window._workingSinceMap && window._workingSinceMap[oldId]) {
        window._workingSinceMap[newId] = window._workingSinceMap[oldId];
        delete window._workingSinceMap[oldId];
    }

    // Update folder tree mapping (workplace mode)
    if (typeof _remapSessionInFolders === 'function') {
        _remapSessionInFolders(oldId, newId);
    }

    // Remap kanban task-session link (server-side DB is updated by session_manager,
    // but update client-side kanban state and URL hash too)
    if (window.location.hash.includes(oldId)) {
        const newHash = window.location.hash.replace(oldId, newId);
        history.replaceState(history.state, '', window.location.pathname + newHash);
    }
    if (history.state && history.state.sessionId === oldId) {
        history.replaceState(Object.assign({}, history.state, { sessionId: newId }), '', window.location.href);
    }

    // Update toolbar data attribute
    setToolbarSession(newId,
        s ? (s.custom_title || s.display_title) : 'New Session',
        s ? !s.custom_title : true,
        s ? (s.custom_title || '') : '');

    // Track remapped planner session ID so state/entry listeners match both IDs.
    // Don't overwrite _plannerSessionId — entry events still use the original ID.
    if (typeof _plannerSessionId !== 'undefined' && _plannerSessionId === oldId) {
        _plannerRemappedId = newId;
    }

    // Re-render sidebar
    filterSessions();

    // Re-schedule auto-naming with the new ID (the old setTimeout closures
    // captured the old ID, which won't match any .jsonl file on disk).
    // Skip if the user has manually named this session.
    if (typeof autoName === 'function' && !(typeof _userNamedSessions !== 'undefined' && _userNamedSessions.has(newId))) {
        setTimeout(() => autoName(newId, true), 3000);
    }
});

// Session log response (for panel open) — client-side pagination.
// Server sends ALL entries; we stash them and only render the last PAGE_SIZE.
socket.on('session_log', (data) => {
    if (data.session_id !== liveSessionId) return;
    const logEl = document.getElementById('live-log');
    if (!logEl) return;

    const allEntries = data.entries || [];
    console.log('[WS] session_log: received ' + allEntries.length + ' entries, will render last ' + LIVE_PAGE_SIZE);

    // Stash all entries in memory for "Load older" to pull from
    _liveEntryStash = allEntries;

    logEl.innerHTML = '';
    _renderedUserTexts.clear();
    if (typeof _clearOutputShelf === 'function') _clearOutputShelf();

    // Only render the last LIVE_PAGE_SIZE entries, but always extend back
    // to include the most recent user message so the prompt is visible.
    let start = Math.max(0, allEntries.length - LIVE_PAGE_SIZE);
    if (start > 0) {
        let hasUser = false;
        for (let i = allEntries.length - 1; i >= start; i--) {
            if (allEntries[i].kind === 'user') { hasUser = true; break; }
        }
        if (!hasUser) {
            for (let i = start - 1; i >= 0; i--) {
                if (allEntries[i].kind === 'user') { start = i; break; }
            }
        }
    }
    _liveRenderedFrom = start;
    const visible = allEntries.slice(start);

    // Show "Load older" button if there are hidden entries
    if (_liveRenderedFrom > 0) {
        logEl.appendChild(_createLoadMoreButton());
    }

    if (visible.length) {
        visible.forEach((entry) => {
            if (entry.kind === 'user' && entry.text) {
                const key = entry.text.trim();
                if (_renderedUserTexts.has(key)) return;
                _renderedUserTexts.add(key);
            }
            logEl.appendChild(renderLiveEntry(entry));
            if (typeof _tryAddOutputCard === 'function') _tryAddOutputCard(entry);
        });
        liveLineCount = allEntries.length;
    } else {
        liveLineCount = 0;
    }

    // Also register user texts from the stashed (non-rendered) entries
    // so real-time session_entry dedup still works correctly
    for (let i = 0; i < start; i++) {
        const e = allEntries[i];
        if (e.kind === 'user' && e.text) _renderedUserTexts.add(e.text.trim());
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

// ── Kanban board real-time events ──
// These handlers call functions defined in kanban.js (loaded before socket.js).
socket.on('kanban_task_created', (data) => {
    if (typeof _kanbanOnTaskCreated === 'function') _kanbanOnTaskCreated(data);
});
socket.on('kanban_task_updated', (data) => {
    if (typeof _kanbanOnTaskUpdated === 'function') _kanbanOnTaskUpdated(data);
});
// No kanban_task_deleted — tasks are NEVER deleted (plan line 2384)
socket.on('kanban_task_moved', (data) => {
    if (typeof _kanbanOnTaskMoved === 'function') _kanbanOnTaskMoved(data);
});
socket.on('kanban_board_refresh', (data) => {
    if (typeof _kanbanOnBoardRefresh === 'function') _kanbanOnBoardRefresh(data);
});

// ---- Periodic state resync heartbeat ----
// SocketIO events can be silently lost (transport hiccup, emit failure,
// tab sleep). Re-request full state every 30s so stale UI self-corrects
// within one interval instead of requiring a manual refresh.
setInterval(() => {
    if (socket.connected) socket.emit('request_state_snapshot');
}, 15000);


// ---- Periodic entry sync (bulletproof fallback) ----
setInterval(function(){if(!liveSessionId||!socket.connected)return;if(sessionKinds[liveSessionId]!=="working")return;var si=window._srvIdx&&window._srvIdx[liveSessionId];if(si!=null&&si>=liveLineCount+2){console.warn("[entry-sync] srv",si,"rendered",liveLineCount);socket.emit("get_session_log",{session_id:liveSessionId,since:0})}},5000);

// ---- Continuous stuck-session watchdog ----
// Catches sessions stuck in "working" that the per-submit watchdog missed
// (e.g. page refresh during active session, or submit before JS loaded).
// Runs every 10s, checks if any session has been "working" for >20s with
// no state events, and forces an HTTP state check to get ground truth.
setInterval(() => {
    if (!window._sessionStateTs) return;
    const now = Date.now();
    for (const sid in sessionKinds) {
        if (sessionKinds[sid] !== 'working') continue;
        const lastEvent = window._sessionStateTs[sid] || 0;
        // If we got a state event within the last 20s, it's probably fine
        if (lastEvent && (now - lastEvent) < 20000) continue;
        // This session looks stuck — do an HTTP check
        if (typeof _watchdogHttpCheck === 'function') {
            console.warn('[bg-watchdog] Session', sid, 'appears stuck working (no events for',
                Math.round((now - lastEvent) / 1000) + 's) — checking via HTTP');
            _watchdogHttpCheck(sid, true);
        }
    }
}, 10000);

// ---- Startup ----
loadProjects();
pollGitStatus();
setInterval(pollGitStatus, 60000);
// Initialize folder tree from server (shows template selector on first run)
if (typeof initFolderTree === 'function') {
  initFolderTree().catch(function(e) { console.error('initFolderTree failed', e); });
}
