/* workforce.js — workforce grid view mode */

function getSessionStatus(id) {
  if (!runningIds.has(id)) {
    // Sessions opened in GUI panel are considered idle even if no OS process detected
    if (guiOpenSessions.has(id)) return 'idle';
    return 'sleeping';
  }
  return sessionKinds[id] || 'working';
}

function setViewMode(mode) {
  viewMode = mode;
  localStorage.setItem('viewMode', mode);
  const listEl = document.getElementById('session-list');
  const gridEl = document.getElementById('workforce-grid');
  const sortBar = document.getElementById('wf-sort-bar');
  const btnList = document.getElementById('btn-view-list');
  const btnWf   = document.getElementById('btn-view-workforce');
  if (mode === 'workforce') {
    listEl.style.display = 'none';
    gridEl.classList.add('visible');
    sortBar.style.display = 'flex';
    if (btnList) btnList.classList.remove('active');
    if (btnWf)   btnWf.classList.add('active');
  } else {
    listEl.style.display = '';
    gridEl.classList.remove('visible');
    sortBar.style.display = 'none';
    if (btnList) btnList.classList.add('active');
    if (btnWf)   btnWf.classList.remove('active');
  }
  filterSessions();
}

function setWfSort(sort) {
  wfSort = sort;
  localStorage.setItem('wfSort', sort);
  ['status','recent','name'].forEach(s => {
    const btn = document.getElementById('wf-btn-' + s);
    if (btn) btn.classList.toggle('active', s === sort);
  });
  filterSessions();
}

function wfSortedSessions(sessions) {
  const copy = [...sessions];
  const statusOrder = {question:0, working:1, idle:2, sleeping:3};
  if (wfSort === 'status') {
    copy.sort((a, b) => {
      const sa = statusOrder[getSessionStatus(a.id)] ?? 3;
      const sb = statusOrder[getSessionStatus(b.id)] ?? 3;
      if (sa !== sb) return sa - sb;
      return (b.last_activity_ts||b.sort_ts||0) - (a.last_activity_ts||a.sort_ts||0);
    });
  } else if (wfSort === 'name') {
    copy.sort((a, b) => (a.display_title||'').localeCompare(b.display_title||''));
  } else {
    // recent
    copy.sort((a, b) => (b.last_activity_ts||b.sort_ts||0) - (a.last_activity_ts||a.sort_ts||0));
  }
  return copy;
}

function renderWorkforce(sessions) {
  const grid = document.getElementById('workforce-grid');
  if (!sessions.length) {
    grid.innerHTML = '<div style="padding:20px;color:#444;font-size:12px;">No sessions found</div>';
    return;
  }
  const statusEmoji = {question:'&#x1F64B;', working:'&#x26CF;&#xFE0F;', idle:'&#x1F4BB;', sleeping:'&#x1F634;'};
  const statusLabel = {question:'Question', working:'Working', idle:'Idle', sleeping:'Sleeping'};
  grid.innerHTML = sessions.map(s => {
    const st = getSessionStatus(s.id);
    const emoji = statusEmoji[st] || '&#x1F634;';
    const label = statusLabel[st] || 'Sleeping';
    const selClass = s.id === activeId ? ' wf-selected' : '';
    const name = escHtml((s.display_title||s.id).slice(0,22) + ((s.display_title||'').length>22?'\u2026':''));
    const date = (s.last_activity||'').split('  ')[0] || '';
    return `<div class="wf-card wf-${st}${selClass}" onclick="singleOrDouble('${s.id}',event)" title="${escHtml(s.display_title)} \u2014 double-click to open in Claude Code GUI">
      <div class="wf-avatar">${emoji}</div>
      <div class="wf-status-label">${label}</div>
      <div class="wf-name">${name}</div>
      <div class="wf-meta">${escHtml(date)}</div>
    </div>`;
  }).join('');
}

function wfCardClick(id) {
  selectSession(id);
}
