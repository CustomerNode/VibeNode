/* polling.js — waiting-for-input polling and initial startup calls */

async function pollWaiting() {
  try {
    const resp = await fetch('/api/waiting');
    const list = await resp.json();
    if (!Array.isArray(list)) throw new Error('bad response');
    const newWaiting = {};
    const newRunning = new Set();
    const newKinds = {};
    list.forEach(w => {
      newRunning.add(w.id);
      newKinds[w.id] = w.kind;   // 'question' | 'working' | 'idle'
      if (w.kind === 'question') newWaiting[w.id] = w;
    });

    // Auto-send queued input when Claude transitions from working -> idle
    if (liveSessionId && liveQueuedText) {
      const wasWorking = (sessionKinds[liveSessionId] === 'working');
      const nowIdle    = (newKinds[liveSessionId] === 'idle');
      if (wasWorking && nowIdle) {
        const textToSend = liveQueuedText;
        liveQueuedText = '';
        showToast('Sending queued command\u2026');
        fetch('/api/respond/' + liveSessionId, {
          method: 'POST', headers: {'Content-Type': 'application/json'},
          body: JSON.stringify({text: textToSend})
        }).then(r => r.json()).then(d => {
          if (d.method !== 'sent') showToast('Queue send failed', true);
        });
      }
    }

    // Update row state classes (4 states: si-question, si-working, si-idle, or none)
    document.querySelectorAll('.session-item[data-sid]').forEach(row => {
      const id = row.dataset.sid;
      row.classList.remove('si-question', 'si-working', 'si-idle');
      if (newRunning.has(id)) row.classList.add('si-' + (newKinds[id] || 'working'));
    });

    waitingData = newWaiting;
    runningIds  = newRunning;
    sessionKinds = newKinds;

    // If currently showing a popup for a session that is no longer waiting, close it
    if (respondTarget && !waitingData[respondTarget]) closeRespond();

    // Re-render workforce view if visible (to update status indicators)
    if (viewMode === 'workforce') filterSessions();

    // Update live panel input bar state
    if (liveSessionId) updateLiveInputBar();

    // Update Close Session button enabled state
    const btnClose = document.getElementById('btn-close');
    if (btnClose && activeId) btnClose.disabled = !newRunning.has(activeId) && !guiOpenSessions.has(activeId);

  } catch(e) {}
  finally {
    // Schedule next poll only after this one finishes — avoids overlap if WMI is slow
    setTimeout(pollWaiting, 2000);
  }
}

// ---- Startup ----
pollWaiting(); // self-rescheduling every 2s via finally block
loadProjects();
pollGitStatus();
setInterval(pollGitStatus, 60000);
