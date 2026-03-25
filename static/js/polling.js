/* polling.js — waiting-for-input polling and initial startup calls */

// --- Auto-name tracking ---
// Prevents hammering the auto-name endpoint for the same session on every poll.
// Cleared every ~2 min to allow retries for sessions whose first attempt was too early.
const _autoNamePending = new Set();
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

    // Auto-send queued input when any session transitions from working -> idle
    // (works even if user navigated away from the session)
    for (const _qSid of Object.keys(_sessionQueues)) {
      const _qText = _getQueue(_qSid);
      if (!_qText || _liveSending) continue;
      const wasWorking = (sessionKinds[_qSid] === 'working');
      const nowIdle    = (newKinds[_qSid] === 'idle');
      if (wasWorking && nowIdle) {
        _shiftQueue(_qSid);
        const remaining = _getQueueList(_qSid).length;
        showToast('Sending queued command\u2026' + (remaining ? ' (' + remaining + ' remaining)' : ''));
        if (liveSessionId === _qSid) {
          _renderQueueBanner();
          liveBarState = null;
          _liveSubmitDirect(_qSid, _qText, {});
        } else {
          socket.emit('send_message', {session_id: _qSid, text: _qText});
        }
      }
    }

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
    _autoNamePollCount++;

    // 1. State-transition trigger: when a session leaves "working" state,
    //    it has likely just produced content — perfect moment to auto-name.
    for (const id in sessionKinds) {
      if (sessionKinds[id] === 'working' && newKinds[id] !== 'working') {
        const s = allSessions.find(x => x.id === id);
        if (s && !s.custom_title && !_autoNamePending.has(id)) {
          _autoNamePending.add(id);
          // Small delay lets the .jsonl flush to disk before we read it
          setTimeout(() => autoName(id, true), 1500);
        }
      }
    }

    // 2. Sweep for untitled sessions.  Runs every poll cycle when unnamed
    //    sessions exist, otherwise backs off to every ~30 s as a background check.
    const _untitled = allSessions.filter(s => !s.custom_title && !_autoNamePending.has(s.id));
    if (_untitled.length > 0 || _autoNamePollCount % 15 === 0) {
      for (const s of _untitled) {
        _autoNamePending.add(s.id);
        autoName(s.id, true);
      }
    }

    // 3. Clear pending set every ~20 s so failed attempts retry quickly
    if (_autoNamePollCount % 10 === 0) _autoNamePending.clear();

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
    if (viewMode === 'workforce' || viewMode === 'workplace') filterSessions();

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
pollWaiting(); // self-rescheduling every 2s via finally block
loadProjects();
pollGitStatus();
setInterval(pollGitStatus, 60000);
