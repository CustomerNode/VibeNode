/* polling.js — waiting-for-input polling and initial startup calls */

// --- Auto-name tracking ---
// Maps session_id → attempt count.  Max 2 attempts per session, ever.
// Never cleared — once a session hits 2 attempts we stop trying.
const _autoNameAttempts = {};
let _autoNamePollCount = 0;

async function pollWaiting() {
  try {
    const _inclParam = liveSessionId ? '?include=' + encodeURIComponent(liveSessionId) : '';
    const resp = await fetch('/api/waiting' + _inclParam);
    const list = await resp.json();
    if (!Array.isArray(list)) throw new Error('bad response');
    const newWaiting = {};
    const newRunning = new Set();
    const newKinds = {};
    list.forEach(w => {
      // Only treat sessions with an actual running process (pid > 0) as running.
      // Sessions with pid=0 (dead, included via ?include= or mtime-only) must NOT
      // be in runningIds — otherwise the UI shows idle/working/question states for
      // sessions that aren't actually running, and permission answers silently fail.
      if (w.pid > 0) newRunning.add(w.id);
      newKinds[w.id] = w.kind;   // 'question' | 'working' | 'idle'
      if (w.kind === 'question') newWaiting[w.id] = w;
    });

    // Queue auto-dispatch is now handled server-side — no client-side auto-send needed.

    // Update row state classes (4 states: si-question, si-working, si-idle, or none)
    document.querySelectorAll('.session-item[data-sid]').forEach(row => {
      const id = row.dataset.sid;
      row.classList.remove('si-question', 'si-working', 'si-idle');
      if (newRunning.has(id)) row.classList.add('si-' + (newKinds[id] || 'working'));
    });

    // Clear _answerPending when the server reports a non-question state,
    // meaning the answered question has resolved and the server's tracking
    // has already taken over suppression.
    for (const sid in _answerPending) {
      if (newKinds[sid] && newKinds[sid] !== 'question') {
        delete _answerPending[sid];
        delete _lastAnswer[sid];
        delete _resendCount[sid];
      }
    }

    // --- Auto-name untitled sessions reactively ---
    // Hard cap: max 2 attempts per session.  If it didn't work twice, the
    // heuristic fallback already ran — stop spawning CLI processes.
    _autoNamePollCount++;

    function _tryAutoName(id) {
      if (_userNamedSessions.has(id)) return;
      const attempts = _autoNameAttempts[id] || 0;
      if (attempts >= 2) return;
      _autoNameAttempts[id] = attempts + 1;
      autoName(id, true);
    }

    // 1. State-transition trigger: when a session leaves "working" state,
    //    it has likely just produced content — perfect moment to auto-name.
    for (const id in sessionKinds) {
      if (sessionKinds[id] === 'working' && newKinds[id] !== 'working') {
        const s = allSessions.find(x => x.id === id);
        if (s && !s.custom_title) {
          // Small delay lets the .jsonl flush to disk before we read it
          setTimeout(() => _tryAutoName(id), 1500);
        }
      }
    }

    // 2. Sweep for untitled sessions — only on every 15th poll (~30s)
    //    to avoid hammering during high-concurrency bursts.
    if (_autoNamePollCount % 15 === 0) {
      const _untitled = allSessions.filter(s =>
        !s.custom_title && !_userNamedSessions.has(s.id) && (_autoNameAttempts[s.id] || 0) < 2
      );
      for (const s of _untitled) {
        _tryAutoName(s.id);
      }
    }

    waitingData = newWaiting;
    runningIds  = newRunning;
    sessionKinds = newKinds;
    _waitingPolledOnce = true;

    // Clean up guiOpenSessions: if a session the user previously opened in GUI
    // is no longer reported as running by the server, stop treating it as idle.
    // Exception: keep the live panel session — it may briefly drop out of the
    // server scan while transitioning (e.g. process just starting up).
    guiOpenSessions.forEach(id => {
      if (!newRunning.has(id) && id !== liveSessionId) {
        guiOpenDelete(id);
      }
    });

    // If currently showing a popup for a session that is no longer waiting, close it
    if (respondTarget && !waitingData[respondTarget]) closeRespond();

    // Update workspace permission queue if active
    if (workspaceActive && typeof _updatePermissionQueue === 'function') {
      _updatePermissionQueue(newWaiting);
    }

    // Re-render workforce/workspace view if visible (to update status indicators)
    if (viewMode === 'sessions' || viewMode === 'workplace' || viewMode === 'homepage') filterSessions();

    // Update live panel input bar state
    if (liveSessionId) updateLiveInputBar();

    // Refresh dashboard if no session selected (skip in workspace mode)
    if (!activeId && !workspaceActive) {
      const dash = document.querySelector('.dashboard');
      if (dash) document.getElementById('main-body').innerHTML = _buildDashboard();
    }

    // Update Close Session button enabled state
    const btnClose = document.getElementById('btn-close');
    if (btnClose && activeId) btnClose.disabled = !newRunning.has(activeId) && !guiOpenSessions.has(activeId);

  } catch(e) { console.error('[pollWaiting] error:', e); }
  finally {
    // Schedule next poll only after this one finishes — avoids overlap if WMI is slow
    _waitingPollTimer = setTimeout(pollWaiting, 2000);
  }
}

let _waitingPollTimer = null;
let _waitingPolledOnce = false;

/** Poke the poll loop to run sooner (without creating duplicate chains). */
function pokeWaiting() {
  if (_waitingPollTimer) clearTimeout(_waitingPollTimer);
  _waitingPollTimer = setTimeout(pollWaiting, 100);
}

// ---- Startup ----
// pollWaiting() — disabled: /api/waiting was removed when we moved to the
// SDK/WebSocket approach.  State tracking is now handled by session_state
// and state_snapshot WebSocket events.  The poll was silently 404-ing every
// 2s and all its side effects (auto-naming, filterSessions, updateLiveInputBar)
// were never executing.
loadProjects();
pollGitStatus();
setInterval(pollGitStatus, 60000);
