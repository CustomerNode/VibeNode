/* socket.js — WebSocket (Socket.IO) event handling, replaces polling.js */

// Pass activeProject as a query param so the server can filter the initial
// state_snapshot by the correct project — before the client has a chance to
// send request_state_snapshot.  This fixes sessions from the wrong project
// appearing (or real sessions being excluded) on the very first snapshot.
const _socketProject = localStorage.getItem('activeProject') || '';
const socket = io({ query: { project: _socketProject } });
let _wsConnected = false;

// ── Sub-agent team tracking ──
// Maps session_id → { toolUseId: { desc, startTime, status: 'working'|'done' } }
window._subAgents = {};


/**
 * Returns true if a session ID belongs to a hidden utility session
 * (planner, auto-title, etc.) that must NEVER appear in the workspace.
 * Called at every entry point where a session can enter the UI.
 *
 * Cross-project isolation is handled at the DISPLAY level (filterSessions,
 * server-side snapshot filtering) — NOT here. Filtering events at this level
 * blocks streaming for newly started sessions.
 */
// Persistent set of session IDs known to be hidden utilities.
// Populated when _isHiddenSession detects one, so subsequent events
// (e.g. session_entry which lacks session_type) can still filter them.
const _hiddenSessionIds = new Set();

function _isHiddenSession(id, data) {
    if (!id) return false;
    // Fast path: already known hidden utility
    if (_hiddenSessionIds.has(id)) return true;
    // Convention: any session ID starting with "_" is a system/utility session
    // (_title_*, _planner_*, and any future utility sessions).
    if (id.startsWith('_')) { _hiddenSessionIds.add(id); return true; }
    if (data && (data.session_type === 'planner' || data.session_type === 'title')) {
        _hiddenSessionIds.add(id);
        return true;
    }
    // NOTE: Cross-project filtering is NOT done here. Blocking events at this
    // level breaks streaming for new sessions. Instead, cross-project sessions
    // are filtered at the DISPLAY level in filterSessions() and state_snapshot.
    return false;
}

// Clear cross-project caches when switching projects. _hiddenSessionIds
// can accumulate IDs from the old project that would incorrectly suppress
// events for the new project if IDs overlap.  Also clear staleness
// timestamps so the incoming snapshot is authoritative.
function _clearCrossProjectCache() {
    _hiddenSessionIds.clear();
    window._sessionStateTs = {};
}

/**
 * Check if a session's cwd belongs to the currently active project.
 * Used at the DISPLAY level to prevent cross-project sessions from
 * bleeding into the sidebar/allSessions. NOT used to block events.
 */
function _sessionBelongsToActiveProject(cwd) {
    if (!cwd) return true;  // no cwd → don't filter
    const active = localStorage.getItem('activeProject');
    if (!active) return true;  // no active project → don't filter
    const encoded = cwd.replace(/\\/g, '-').replace(/\//g, '-').replace(/:/g, '-');
    return encoded.toLowerCase() === active.toLowerCase();
}

socket.on('connect', () => {
    _wsConnected = true;
    console.log('[WS] Connected');
    // Update SocketIO query params with current project so reconnects
    // and the server's handle_connect use the right project context.
    const _curProj = localStorage.getItem('activeProject') || '';
    if (socket.io && socket.io.opts) socket.io.opts.query = { project: _curProj };
    // Update status bar connection indicator
    const sbConn = document.getElementById('sb-connection');
    if (sbConn) { sbConn.textContent = '\u25CF'; sbConn.style.color = 'var(--idle-label)'; sbConn.title = 'Connected'; }
    // Fetch persisted permission policy from backend (don't push localStorage defaults)
    socket.emit('get_permission_policy');
    // Fetch persisted UI preferences (sendBehavior, etc.) from backend
    socket.emit('get_ui_prefs');
    // Refresh session list on reconnect — but only AFTER initial load
    // is complete.  On first connect, loadProjects() handles session loading
    // (with proper project sync).  Calling loadSessions() here too causes
    // a race that tears down the live panel mid-stream.
    if (typeof loadSessions === 'function' && window._initialLoadDone) {
        loadSessions();
    }
    // Request full state snapshot to resync indicators.
    // Include activeProject so the server syncs its _active_project
    // (which resets to VibeNode on web restart) before filtering.
    const _ap = _curProj;
    socket.emit('request_state_snapshot', {project: _ap});
    // Retry after 3s in case the first snapshot was silently dropped
    // (e.g. DaemonClient not yet reconnected when server just restarted)
    setTimeout(() => {
        if (socket.connected) socket.emit('request_state_snapshot', {project: _ap});
    }, 3000);
});

// Restore persisted permission policy from backend on connect
socket.on('permission_policy_loaded', (data) => {
    if (data && data.policy && ['manual', 'auto', 'almost_always', 'custom'].includes(data.policy)) {
        permissionPolicy = data.policy;
        localStorage.setItem('permPolicy', data.policy);
        if (data.custom_rules && typeof data.custom_rules === 'object') {
            customPolicies = data.custom_rules;
            localStorage.setItem('customPolicies', JSON.stringify(data.custom_rules));
        }
        // Refresh permission UI if it exists
        if (typeof renderPermissionPanel === 'function') renderPermissionPanel();
    }
});

// Restore persisted UI preferences (sendBehavior, etc.) from backend on connect.
// If the server has no prefs yet, seed it from localStorage so existing
// preferences are captured immediately.
socket.on('ui_prefs_loaded', (data) => {
    if (!data || typeof data !== 'object') return;
    if (data.sendBehavior && ['enter', 'ctrl-enter'].includes(data.sendBehavior)) {
        // Server has a saved preference — apply it
        sendBehavior = data.sendBehavior;
        localStorage.setItem('sendBehavior', data.sendBehavior);
        if (typeof _refreshSendHints === 'function') _refreshSendHints();
    } else {
        // Server has no saved sendBehavior — seed it from current localStorage
        const local = localStorage.getItem('sendBehavior');
        if (local && ['enter', 'ctrl-enter'].includes(local)) {
            socket.emit('set_ui_prefs', { sendBehavior: local });
        }
    }
});

// Daemon reconnection status — live toasts showing recovery progress
socket.on('daemon_reconnect', (data) => {
    const status = data.status;
    const msg = data.message || 'Daemon connection issue';
    if (status === 'connected') {
        showToast(msg);
        // Resync state after reconnect
        setTimeout(() => {
            if (socket.connected) socket.emit('request_state_snapshot');
        }, 500);
    } else if (status === 'disconnected' || status === 'connecting' || status === 'restarting') {
        showToast(msg, true);
    }
    // Update status bar indicator
    const sbConn = document.getElementById('sb-connection');
    if (sbConn) {
        if (status === 'connected') {
            sbConn.style.color = 'var(--accent)';
            sbConn.title = 'Connected';
        } else {
            sbConn.style.color = 'var(--warning, orange)';
            sbConn.title = msg;
        }
    }
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
    // Guard: discard snapshots that arrive after a project switch.
    // _projectSwitchGen is bumped by setProject(); if a snapshot was requested
    // for the old project, its response arrives with a stale generation.
    const _snapGen = (typeof _projectSwitchGen !== 'undefined') ? _projectSwitchGen : 0;
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
                    options: ['y', 'n', 'aa', 'a'],
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
    //
    // BUT: only re-fetch if the DOM looks empty or stale. If real-time
    // streaming already populated the log, a re-fetch would destructively
    // wipe the DOM and re-render with pagination (LIVE_PAGE_SIZE), slicing
    // off the tail of the response the user just watched stream in.
    if (liveSessionId && sessionKinds[liveSessionId] === 'working' &&
        newKinds[liveSessionId] && newKinds[liveSessionId] !== 'working') {
        const logEl = document.getElementById('live-log');
        const domHasEntries = logEl && logEl.querySelectorAll('.msg').length > 0;
        if (!domHasEntries) {
            console.warn('[state_snapshot] Live session', liveSessionId,
                'transitioned from working →', newKinds[liveSessionId],
                '— DOM is empty, re-fetching entries');
            socket.emit('get_session_log', {session_id: liveSessionId, since: 0, limit: LIVE_PAGE_SIZE, project: localStorage.getItem('activeProject') || ''});
        } else {
            console.log('[state_snapshot] Live session', liveSessionId,
                'transitioned from working →', newKinds[liveSessionId],
                '— DOM already has entries, skipping destructive re-fetch');
        }
    }

    // Preserve optimistic state for sessions the frontend knows are running
    // but the server snapshot doesn't include yet (e.g. just-started session
    // whose start_session hasn't registered on the daemon before the snapshot
    // was built — common during project switch).
    // Only preserve if the session is in allSessions (belongs to active
    // project). Without this guard, sessions from the OLD project bleed
    // into the new project's state after a project switch.
    for (const id in sessionKinds) {
        if (sessionKinds[id] === 'working' && !newKinds[id]) {
            if (_hiddenSessionIds.has(id)) continue;
            if (!allSessionIds.has(id) && id !== liveSessionId) continue;
            newKinds[id] = 'working';
            newRunning.add(id);
        }
    }

    // Final guard: if the user switched projects while we were processing
    // this snapshot, discard everything — the new project's snapshot will
    // arrive shortly and we don't want to pollute its state.
    if (typeof _projectSwitchGen !== 'undefined' && _snapGen !== _projectSwitchGen) {
        console.warn('[state_snapshot] project switched during processing — discarding stale snapshot');
        return;
    }

    waitingData = newWaiting;
    runningIds = newRunning;
    sessionKinds = newKinds;

    // Purge stale sub-agents for sessions that are no longer working.
    // The real-time session_state handler clears on idle, but if that
    // event was missed (tab sleeping, transport hiccup), old agents
    // would persist and reappear when the session starts a new turn.
    if (window._subAgents) {
        for (const _saId in window._subAgents) {
            if (newKinds[_saId] !== 'working') {
                delete window._subAgents[_saId];
            }
        }
    }

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
        _rebuildSessionIds();  // keep Set in sync after filter
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
        if (!allSessionIds.has(id)) {
            // Don't inject stub if an old alias for this ID still exists
            if (window._idRemaps) {
                let _hasOld = false;
                for (const oldId in window._idRemaps) {
                    if (window._idRemaps[oldId] === id && allSessionIds.has(oldId)) {
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
    _rebuildSessionIds();

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

    // Re-apply row state CSS classes AFTER filterSessions() re-creates the DOM.
    // The earlier class update (lines above) targeted rows that filterSessions()
    // just destroyed and rebuilt, so those classes were lost.
    for (const id in sessionKinds) {
        const kind = sessionKinds[id];
        const state = kind === 'question' ? 'waiting' : kind;
        _updateRowState(id, state);
    }

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
    // Cross-project filtering: drop state events from other projects so
    // sessions don't bleed into sessionKinds/runningIds/sidebar.
    // Always allow the live session through (user is actively watching it).
    if (data.cwd && session_id !== liveSessionId && !_sessionBelongsToActiveProject(data.cwd)) return;
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
    // But if the server explicitly sends substatus="" (key present, empty),
    // that means compaction finished — always honour it.
    if (!window._sessionSubstatus) window._sessionSubstatus = {};
    const substatusExplicit = data.hasOwnProperty('substatus');
    if (substatus) {
        window._sessionSubstatus[session_id] = substatus;
    } else if (substatusExplicit || state !== 'working' || window._sessionSubstatus[session_id] !== 'compacting') {
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
        // Clear sub-agents from previous turns when starting a new turn
        // (idle/question → working transition means old agents are stale)
        if (sessionKinds[session_id] && sessionKinds[session_id] !== 'working') {
            delete window._subAgents[session_id];
        }
        sessionKinds[session_id] = 'working';
    } else if (state === 'idle') {
        sessionKinds[session_id] = 'idle';
        // Clean up any leftover streaming bubble when session goes idle
        if (session_id === liveSessionId) {
            const _sb = document.querySelector('.msg.assistant.streaming-bubble');
            if (_sb) _sb.remove();
        }
        // Clear sub-agent tracking when session finishes
        delete window._subAgents[session_id];
    } else if (state === 'stopped') {
        delete sessionKinds[session_id];
        delete window._subAgents[session_id];
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
        // Only re-fetch if the backend genuinely has MORE entries than the
        // DOM. Never do a blind re-fetch — it wipes the DOM and re-renders
        // with pagination, slicing off the tail of the response.
        if (state === 'idle' || state === 'stopped') {
            const sc = data.entry_count;
            if (sc != null && sc > liveLineCount) {
                console.warn('[entry-catchup] Backend has', sc, 'entries but frontend has', liveLineCount, '— re-fetching');
                socket.emit('get_session_log', {session_id: session_id, since: 0, limit: LIVE_PAGE_SIZE, project: localStorage.getItem('activeProject') || ''});
            }
            // No blind 500ms re-fetch — it destroys already-rendered content
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

    // ── Compose session-dot live update ──
    // When a session changes state, find any compose card linked to it
    // and update just the dot (no full board re-render).
    if (typeof _composeSections !== 'undefined' && _composeSections) {
        const _cSec = _composeSections.find(s => s.session_id === session_id);
        if (_cSec) {
            const _cCard = document.querySelector('.compose-card[data-section-id="' + _cSec.id + '"]');
            if (_cCard) {
                const _dot = _cCard.querySelector('.compose-session-dot');
                if (_dot) {
                    if (state === 'working' || state === 'starting') {
                        _dot.className = 'compose-session-dot running';
                    } else if (state === 'stopped') {
                        _dot.className = 'compose-session-dot idle';
                    } else {
                        _dot.className = 'compose-session-dot idle';
                    }
                }
            }
        }
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

// Message send failed — show a clear inline error with the user's text
// preserved so they can copy/paste and retry. This happens when the daemon
// is overloaded (e.g. multiple long sessions doing heavy I/O).
socket.on('send_failed', (data) => {
    if (!data || !data.session_id) return;
    const sid = data.session_id;
    console.error('[WS] send_failed for', sid, data.error);

    // Cancel watchdog — we know it failed
    if (typeof _cancelMessageWatchdog === 'function') _cancelMessageWatchdog(sid);

    // Revert session to idle so user can retry
    sessionKinds[sid] = 'idle';
    runningIds.delete(sid);
    if (sid === liveSessionId) {
        liveBarState = null;
        if (typeof updateLiveInputBar === 'function') updateLiveInputBar();

        // Put the message text back in the input box so it's not lost
        const ta = document.getElementById('live-input-ta');
        if (ta && data.text && !ta.value.trim()) {
            ta.value = data.text;
            if (typeof _resetTextareaHeight === 'function') _resetTextareaHeight(ta);
        }

        // Remove the optimistic bubble — it didn't go through
        const logEl = document.getElementById('live-log');
        if (logEl) {
            const optimistic = logEl.querySelector('.msg.user.optimistic-bubble:last-child');
            if (optimistic) optimistic.remove();
        }

        // Show an inline system message explaining what happened,
        // including the user's original text so it's never truly lost
        if (logEl && typeof renderLiveEntry === 'function') {
            const isTimeout = (data.error || '').includes('timeout');
            let hint = isTimeout
                ? '⚠️ Message not delivered — the server was busy processing other sessions. This can happen when running many long sessions at once. Try closing some idle sessions or sending again in a moment.'
                : '⚠️ Message not delivered. Error: ' + (data.error || 'unknown');
            if (data.text) {
                hint += '\n\nYour message (also restored to the input box):\n> ' + data.text;
            }
            logEl.appendChild(renderLiveEntry({
                kind: 'system',
                text: hint,
                is_error: true,
            }));
            if (liveAutoScroll) logEl.scrollTop = logEl.scrollHeight;
        }
    }
    _updateRowState(sid, 'idle');
});

// Lightweight per-call token usage updates (from StreamEvent message_start).
// This gives us the REAL context window size, not cumulative session totals.
socket.on('session_usage', (data) => {
    if (!data || !data.session_id || !data.usage) return;
    if (_hiddenSessionIds.has(data.session_id)) return;
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

// ── Real-time streaming text deltas ──────────────────────────────────
// The backend forwards raw Claude SDK StreamEvents. We use
// content_block_delta events to build a live-typing assistant bubble
// that gets replaced by the final 'asst' session_entry when complete.
socket.on('stream_event', (data) => {
    if (!data || !data.session_id || !data.event) return;
    if (_hiddenSessionIds.has(data.session_id)) return;

    // ── Session ID match (same alias logic as session_entry) ──
    let _sidMatch = (data.session_id === liveSessionId);
    if (!_sidMatch && liveSessionId && window._idRemaps) {
        if (window._idRemaps[liveSessionId] === data.session_id) {
            _sidMatch = true;
        }
        for (const oldId in window._idRemaps) {
            if (window._idRemaps[oldId] === liveSessionId && oldId === data.session_id) {
                _sidMatch = true;
                break;
            }
        }
    }
    if (!_sidMatch) return;

    const evtType = data.event.event || '';
    const evtData = data.event.data || {};

    // ── content_block_delta → append text to streaming bubble ──
    if (evtType === 'content_block_delta') {
        // SDK shape: data.delta may be {type:"text_delta", text:"..."} or a plain string
        let chunk = '';
        const delta = evtData.delta;
        if (typeof delta === 'string') {
            chunk = delta;
        } else if (delta && typeof delta === 'object') {
            chunk = delta.text || '';
        }
        if (!chunk) return;

        const logEl = document.getElementById('live-log');
        if (!logEl) return;

        // Find or create the streaming bubble
        let bubble = logEl.querySelector('.msg.assistant.streaming-bubble');
        if (!bubble) {
            // Clear skeleton on first real content
            if (liveLineCount === 0) {
                const skel = logEl.querySelector('.skel-bar, .skeleton-loader, .live-log-empty, .empty-state');
                if (skel) logEl.innerHTML = '';
            }
            bubble = document.createElement('div');
            bubble.className = 'msg assistant streaming-bubble';
            bubble.innerHTML =
                '<div class="msg-role">claude <span class="msg-time" style="color:var(--text-faint);font-size:10px;">streaming\u2026</span></div>' +
                '<div class="msg-body msg-content"></div>';
            logEl.appendChild(bubble);
        }

        // Append chunk to raw text accumulator, then re-render markdown
        // Throttle markdown re-parsing to avoid jank on fast streams
        if (!bubble._rawText) bubble._rawText = '';
        bubble._rawText += chunk;
        const bodyEl = bubble.querySelector('.msg-body');
        if (bodyEl) {
            const now = Date.now();
            const elapsed = now - (bubble._lastRender || 0);
            if (elapsed >= 80 && typeof mdParse === 'function') {
                bodyEl.innerHTML = mdParse(bubble._rawText);
                bubble._lastRender = now;
                clearTimeout(bubble._renderTimer);
                bubble._renderTimer = 0;
            } else if (!bubble._renderTimer) {
                // Schedule a trailing render so the last chunk always shows
                bubble._renderTimer = setTimeout(() => {
                    if (bodyEl && typeof mdParse === 'function') {
                        bodyEl.innerHTML = mdParse(bubble._rawText || '');
                    }
                    bubble._lastRender = Date.now();
                    bubble._renderTimer = 0;
                }, 80);
            }
        }

        if (liveAutoScroll) {
            logEl.scrollTop = logEl.scrollHeight;
        }

        // Reset watchdog — data is flowing
        if (typeof _resetMessageWatchdog === 'function') _resetMessageWatchdog(liveSessionId);
    }
});

// Live log entries pushed in real-time
socket.on('session_entry', (data) => {
    // Never process entries for hidden utility sessions (planner, title).
    if (_hiddenSessionIds.has(data.session_id)) return;

    // Skip entries for sessions not in the current project.
    // session_entry lacks cwd, but if this session_id isn't the live session
    // and isn't in allSessions, it's cross-project — drop it entirely so the
    // consistency check below doesn't inject it into the sidebar.
    if (data.session_id !== liveSessionId && !allSessionIds.has(data.session_id)) return;

    // NOTE: Watchdog reset moved AFTER session_id match check below.
    // Previously it was here, so entries for a mismatched session_id (e.g.
    // stale pre-remap alias) kept resetting the watchdog without rendering,
    // permanently defeating the recovery safety net.

    // PERF-CRITICAL: performance.mark/measure instrumentation — do NOT remove. See CLAUDE.md #17.
    // Performance: measure time from submit to first entry (once per turn)
    if (performance.getEntriesByName('submit-' + data.session_id).length) {
        performance.mark('first-entry-' + data.session_id);
        try {
            performance.measure(
                'time-to-first-entry-' + data.session_id,
                'submit-' + data.session_id,
                'first-entry-' + data.session_id
            );
            var _perfMeasure = performance.getEntriesByName('time-to-first-entry-' + data.session_id)[0];
            if (_perfMeasure) {
                console.debug('[PERF] Time to first entry for %s: %dms', data.session_id.slice(0, 12), Math.round(_perfMeasure.duration));
            }
        } catch (_e) { /* ignore measurement errors */ }
        performance.clearMarks('submit-' + data.session_id);
        performance.clearMarks('first-entry-' + data.session_id);
        performance.clearMeasures('time-to-first-entry-' + data.session_id);
    }

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

    // Session ID match — also check aliases. The daemon resolves session IDs
    // through its alias table, so entries can arrive under the canonical (new)
    // ID while liveSessionId still holds the old pre-remap ID. Auto-heal.
    let _sidMatch = (data.session_id === liveSessionId);
    if (!_sidMatch && liveSessionId && window._idRemaps) {
        if (window._idRemaps[liveSessionId] === data.session_id) {
            console.warn('[entry] auto-healing liveSessionId alias:',
                liveSessionId, '→', data.session_id);
            liveSessionId = data.session_id;
            _sidMatch = true;
        }
        for (const oldId in window._idRemaps) {
            if (window._idRemaps[oldId] === liveSessionId && oldId === data.session_id) {
                console.warn('[entry] accepting late pre-remap entry:', data.session_id,
                    '(remapped to', liveSessionId, ')');
                _sidMatch = true;
                break;
            }
        }
    }
    if (!_sidMatch) {
        console.warn('[entry] sid mismatch:', data.session_id, '!=', liveSessionId,
            'kind:', data.entry && data.entry.kind, 'idx:', data.index);
        return;
    }

    // Reset (not cancel) watchdog — data is flowing AND matches our live
    // session. Must be AFTER the session_id match check above.
    if (typeof _resetMessageWatchdog === 'function') _resetMessageWatchdog(liveSessionId);
    if (!data.entry) return;
    const logEl = document.getElementById('live-log');
    if (!logEl) {
        console.warn('[entry-drop] live-log not in DOM! kind:', data.entry.kind, 'idx:', data.index);
        return;
    }
    // Remove streaming bubble when the final assistant entry arrives
    // (or any other entry type — the complete entry replaces the stream)
    const _streamBubble = logEl.querySelector('.msg.assistant.streaming-bubble');
    if (_streamBubble) {
        _streamBubble.remove();
    }
    // Clear skeleton/placeholder on first real entry
    if (liveLineCount === 0) {
        const skel = logEl.querySelector('.skel-bar, .skeleton-loader, .live-log-empty, .empty-state');
        if (skel) logEl.innerHTML = '';
    }
    // Optimistic bubble dedup: if this is a user entry echoed back by the
    // server, check if we already have an optimistic bubble for it. Remove
    // the optimistic bubble and render the server-confirmed version instead.
    // Uses position (last optimistic bubble), NOT text matching — so
    // legitimate duplicate messages ("yes", "ok") always render.
    if (data.entry.kind === 'user') {
        const optimistic = logEl.querySelector('.msg.user.optimistic-bubble:last-child');
        if (optimistic) {
            // The last element is our optimistic bubble — replace it with
            // the server-confirmed entry (which has correct formatting/index).
            optimistic.remove();
        }
    }
    logEl.appendChild(renderLiveEntry(data.entry));
    if (typeof _tryAddOutputCard === 'function') _tryAddOutputCard(data.entry);
    liveLineCount = (data.index != null) ? data.index + 1 : liveLineCount + 1;
    if (typeof _updateLastMessageTimes === 'function') _updateLastMessageTimes();
    if (liveAutoScroll) {
        logEl.scrollTop = logEl.scrollHeight;
    }
    // ── Sub-agent team tracking ──
    if (data.entry.kind === 'tool_use' && data.entry.name === 'Agent') {
        if (!window._subAgents[data.session_id]) window._subAgents[data.session_id] = {};
        window._subAgents[data.session_id][data.entry.id] = {
            desc: data.entry.desc || 'Sub-agent',
            startTime: Date.now(),
            status: 'working'
        };
        // Trigger working bar re-render to show new agent
        if (data.session_id === liveSessionId && typeof updateLiveInputBar === 'function') {
            liveBarState = null;  // force re-render
            updateLiveInputBar();
        }
    }
    if (data.entry.kind === 'tool_result' && data.entry.tool_use_id) {
        const agents = window._subAgents[data.session_id];
        if (agents && agents[data.entry.tool_use_id]) {
            agents[data.entry.tool_use_id].status = 'done';
            agents[data.entry.tool_use_id].endTime = Date.now();
            // Trigger working bar re-render to show completion
            if (data.session_id === liveSessionId && typeof updateLiveInputBar === 'function') {
                liveBarState = null;  // force re-render
                updateLiveInputBar();
            }
        }
    }
    // ── Kanban AI status markers: detect and forward to backend ──
    if (data.entry.kind === 'asst' && data.entry.text) {
        _processKanbanStatusMarkers(data.entry.text);
    }
});

// Permission requests
socket.on('session_permission', (data) => {
    if (_hiddenSessionIds.has(data.session_id)) return;
    // ── Cross-project filtering: permission events don't include cwd,
    // but if this session_id isn't in allSessions and isn't the live session,
    // it belongs to another project — drop it so permission prompts from
    // other projects don't appear in the current project's UI.
    if (data.session_id !== liveSessionId && !allSessionIds.has(data.session_id)) return;
    waitingData[data.session_id] = {
        question: _formatPermissionQuestion(data.tool_name, data.tool_input),
        options: ['y', 'n', 'aa', 'a'],
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
    if (viewMode === 'sessions' || viewMode === 'workplace' || viewMode === 'homepage') filterSessions();
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
    // Clear local queue when all items dispatched
    if (remaining === 0) {
        delete _sessionQueues[sid];
        if (sid === liveSessionId && typeof _renderQueueBanner === 'function') _renderQueueBanner();
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
        // Only inject stub into allSessions if this session belongs to the
        // current project. Otherwise it bleeds into the sidebar.
        const _isSameProject = !data.cwd || _sessionBelongsToActiveProject(data.cwd);
        if (_isSameProject && !allSessionIds.has(data.session_id)) {
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
            allSessionIds.add(data.session_id);
            filterSessions();
        }
        if (_isSameProject) _updateRowState(data.session_id, sessionKinds[data.session_id] || 'idle');
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

    // Update planner session ID if it was remapped (before hidden check)
    if (typeof _plannerSessionId !== 'undefined' && _plannerSessionId === oldId) {
        _plannerSessionId = newId;
    }

    if (_isHiddenSession(oldId, data)) return;

    // Update allSessions array and ID set
    const s = allSessions.find(x => x.id === oldId);
    if (s) {
        s.id = newId;
        allSessionIds.delete(oldId);
        allSessionIds.add(newId);
    }

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
    fetch('/api/remap-name', {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({old_id:oldId, new_id:newId, project: localStorage.getItem('activeProject') || ''})}).catch(()=>{});

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

// Session log response — server-side pagination.
// Server sends only the requested page; "Load older" fetches from server.
socket.on('session_log', (data) => {
    if (data.session_id !== liveSessionId) return;
    const logEl = document.getElementById('live-log');
    if (!logEl) return;

    const entries = data.entries || [];
    const total = data.total || entries.length;
    const offset = data.offset || 0;
    const hasMore = data.has_more || false;
    const isPrepend = data.prepend || false;

    console.log('[WS] session_log: received', entries.length, 'entries (offset=' + offset +
        ', total=' + total + ', has_more=' + hasMore + ', prepend=' + isPrepend + ')');

    // --- Prepend path: "Load older" response ---
    if (isPrepend) {
        const existingBtn = logEl.querySelector('.live-load-more');
        if (existingBtn) existingBtn.remove();

        const prevHeight = logEl.scrollHeight;
        const prevScroll = logEl.scrollTop;
        const frag = document.createDocumentFragment();

        // Update pagination state
        _liveRenderedFrom = offset;
        _liveEntryStash = [];  // not used in server-pagination mode

        if (offset > 0) {
            frag.appendChild(_createLoadMoreButton());
        }
        entries.forEach((entry) => {
            frag.appendChild(renderLiveEntry(entry));
        });
        logEl.insertBefore(frag, logEl.firstChild);

        // Restore scroll position so viewport stays on the same messages
        const newHeight = logEl.scrollHeight;
        logEl.scrollTop = prevScroll + (newHeight - prevHeight);
        return;
    }

    // --- Initial load path ---

    // Guard: if the DOM already has MORE entries than this response covers
    // (e.g. daemon restarted and hasn't fully re-read the JSONL yet), do NOT
    // wipe the DOM — we'd be destroying data the user can already see.
    const effectiveCount = offset + entries.length;
    if (effectiveCount < liveLineCount && liveLineCount > 0) {
        console.warn('[WS] session_log covers fewer entries (' + effectiveCount +
            ') than DOM (' + liveLineCount + ') — skipping destructive re-render');
        return;
    }

    // Clear and re-render
    _liveEntryStash = [];  // server pagination — no client stash needed
    _liveRenderedFrom = offset;

    // Clear stale sub-agent tracking — the full reload replaces everything
    // and historical entries should NOT re-populate the agent bar.
    delete window._subAgents[data.session_id];

    logEl.innerHTML = '';
    _optimisticMsgId = 0;
    if (typeof _clearOutputShelf === 'function') _clearOutputShelf();

    // Show "Load older" button if server says there are more
    if (hasMore) {
        logEl.appendChild(_createLoadMoreButton());
    }

    if (entries.length) {
        entries.forEach((entry) => {
            logEl.appendChild(renderLiveEntry(entry));
            if (typeof _tryAddOutputCard === 'function') _tryAddOutputCard(entry);
        });
        liveLineCount = total;
    } else {
        liveLineCount = 0;
    }

    if (typeof _updateLastMessageTimes === 'function') _updateLastMessageTimes();
    if (liveAutoScroll) logEl.scrollTop = logEl.scrollHeight;

    // Performance: measure session switch time (from get_session_log emit to render complete)
    if (performance.getEntriesByName('switch-' + data.session_id).length) {
        performance.mark('switch-rendered-' + data.session_id);
        try {
            performance.measure(
                'session-switch-' + data.session_id,
                'switch-' + data.session_id,
                'switch-rendered-' + data.session_id
            );
            var _switchMeasure = performance.getEntriesByName('session-switch-' + data.session_id)[0];
            if (_switchMeasure) {
                console.debug('[PERF] Session switch for %s: %dms (%d entries)',
                    data.session_id.slice(0, 12), Math.round(_switchMeasure.duration), entries.length);
            }
        } catch (_e) { /* ignore measurement errors */ }
        performance.clearMarks('switch-' + data.session_id);
        performance.clearMarks('switch-rendered-' + data.session_id);
        performance.clearMeasures('session-switch-' + data.session_id);
    }
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

// ── Compose board real-time events ──
// These handlers call functions defined in compose.js (loaded before socket.js).
socket.on('compose_task_created', (data) => {
    if (typeof _composeOnTaskCreated === 'function') _composeOnTaskCreated(data);
});
socket.on('compose_task_updated', (data) => {
    if (typeof _composeOnTaskUpdated === 'function') _composeOnTaskUpdated(data);
});
socket.on('compose_task_moved', (data) => {
    if (typeof _composeOnTaskMoved === 'function') _composeOnTaskMoved(data);
});
socket.on('compose_board_refresh', (data) => {
    if (typeof _composeOnBoardRefresh === 'function') _composeOnBoardRefresh(data);
});
// Changing-flag protocol: agents set changing=true before mutations,
// clear it when done.  Siblings see the yellow dot update in real time.
socket.on('compose_changing', (data) => {
    if (typeof _composeOnChanging === 'function') _composeOnChanging(data);
});

// Directive conflict detection: when a directive is logged and the backend
// detects ambiguous conflicts, surface the resolution UI in the live chat.
socket.on('compose_directive_logged', (data) => {
    if (typeof _injectDirectiveConflict === 'function') _injectDirectiveConflict(data);
});

// Context-updated push: compose-context.json was modified (either via API
// or detected by the file watcher when an agent writes directly to disk).
// Refreshes the board so parallel agents' progress reflects in real time.
socket.on('compose_context_updated', (data) => {
    if (typeof _composeOnContextUpdated === 'function') _composeOnContextUpdated(data);
});

// Directive conflict resolved: update any open conflict cards in the chat.
// Backend emits { project_id, conflict_id, action }. Match cards by
// data-conflict-id attribute or by element id 'dc-{conflict_id}'.
socket.on('compose_directive_conflict_resolved', (data) => {
    if (!data || !data.conflict_id) return;
    const cards = document.querySelectorAll('.live-directive-conflict:not(.dc-resolved)');
    cards.forEach(card => {
        const cardConflictId = card.dataset.conflictId || '';
        const matchById = card.id === 'dc-' + data.conflict_id;
        if (cardConflictId === data.conflict_id || matchById) {
            if (typeof _markConflictResolved === 'function') {
                _markConflictResolved(card.id, data.action, data);
            }
        }
    });
});

// ---- Kanban AI status marker processing ----
// Detects <!-- kanban:status task_id=UUID status=VALUE --> markers in AI
// output and forwards them to the backend for automatic status transitions.
const _kanbanStatusMarkerRe = /<!--\s*kanban:status\s+task_id=([a-f0-9-]+)\s+status=(\w+)\s*-->/gi;
const _processedKanbanMarkers = new Set();

function _processKanbanStatusMarkers(text) {
    if (!text) return;
    let match;
    _kanbanStatusMarkerRe.lastIndex = 0;
    while ((match = _kanbanStatusMarkerRe.exec(text)) !== null) {
        const taskId = match[1];
        const newStatus = match[2];
        const key = taskId + ':' + newStatus;
        // Dedup — don't send the same status change twice
        if (_processedKanbanMarkers.has(key)) continue;
        _processedKanbanMarkers.add(key);
        // Clear dedup after 30s so the same transition can fire again later
        setTimeout(() => _processedKanbanMarkers.delete(key), 30000);
        // Fire and forget — send to backend
        fetch('/api/kanban/tasks/' + taskId + '/ai-status', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ new_status: newStatus, session_id: liveSessionId || '' }),
        }).then(r => {
            if (r.ok) console.log('[kanban-ai] Status updated:', taskId, '->', newStatus);
            else r.json().then(d => console.warn('[kanban-ai] Status change rejected:', d.error || d));
        }).catch(e => console.warn('[kanban-ai] Status change failed:', e));
    }
}

// ---- Periodic state resync heartbeat ----
// SocketIO events can be silently lost (transport hiccup, emit failure,
// tab sleep). Re-request full state every 30s so stale UI self-corrects
// within one interval instead of requiring a manual refresh.
setInterval(() => {
    if (socket.connected) socket.emit('request_state_snapshot');
}, 30000);


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
        // Skip if per-submit watchdog is already monitoring this session
        if (window._watchdogSid === sid && window._watchdogTimer) continue;
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
window._initialLoadDone = false;
loadProjects().then(() => { window._initialLoadDone = true; }).catch(() => { window._initialLoadDone = true; });
// Git status polling — initial check + 60s interval
if (typeof pollGitStatus === 'function') {
  pollGitStatus();
  setInterval(pollGitStatus, 60000);
}
// Initialize folder tree from server (shows template selector on first run)
if (typeof initFolderTree === 'function') {
  initFolderTree().catch(function(e) { console.error('initFolderTree failed', e); });
}
