/* sessions.js — sorting, list rendering, tooltips, column resize, click handling */

function setSort(mode) {
  if (sortMode === mode) {
    sortAsc = !sortAsc;   // same column — toggle direction
  } else {
    sortMode = mode;
    sortAsc = false;      // new column — default descending
  }
  filterSessions();
}

function sortedSessions(sessions) {
  const copy = [...sessions];
  const dir = sortAsc ? 1 : -1;
  if (sortMode === 'size') {
    copy.sort((a, b) => dir * ((a.file_bytes || 0) - (b.file_bytes || 0)));
  } else if (sortMode === 'name') {
    copy.sort((a, b) => dir * (a.display_title || '').localeCompare(b.display_title || ''));
  } else {
    copy.sort((a, b) => dir * ((a.last_activity_ts || a.sort_ts || 0) - (b.last_activity_ts || b.sort_ts || 0)));
  }
  return copy;
}

function renderList(sessions) {
  const el = document.getElementById('session-list');
  if (!sessions.length) {
    el.innerHTML = '<div style="padding:20px;color:#444;font-size:12px;">No sessions found</div>';
    return;
  }

  const arrow = sortAsc ? '\u2191' : '\u2193';
  const header = `
    <div class="col-header-row">
      <div class="col-header sortable ${sortMode==='name'?'sort-active':''}" id="col-h-name" onclick="setSort('name')" title="Sort by name">
        Name ${sortMode==='name' ? arrow : ''}
        <span class="col-resize-grip" data-col="name"></span>
      </div>
      <div class="col-header sortable ${sortMode==='date'?'sort-active':''}" id="col-h-date" onclick="setSort('date')" title="Sort by date">
        Date ${sortMode==='date' ? arrow : ''}
        <span class="col-resize-grip" data-col="date"></span>
      </div>
      <div class="col-header sortable ${sortMode==='size'?'sort-active':''}" id="col-h-size" onclick="setSort('size')" title="Sort by size">
        Size ${sortMode==='size' ? arrow : ''}
      </div>
    </div>`;

  const rows = sessions.map(s => {
    const isWaiting = !!waitingData[s.id];
    const isRunning = !isWaiting && runningIds.has(s.id);
    const stateClass = isWaiting ? ' waiting' : (isRunning ? ' running' : '');
    const activeClass = s.id === activeId ? ' active' : '';
    const colClick = `onclick="singleOrDouble('${s.id}',event)" style="cursor:pointer;"`;
    const icon = isWaiting
      ? '<span title="Waiting for input" style="color:#ff9500;margin-right:4px;">&#x23F3;</span>'
      : isRunning
      ? '<span title="Running" style="color:#44bb66;margin-right:5px;font-size:9px;">&#9679;</span>'
      : '';
    return `
    <div class="session-item${activeClass}${stateClass}" data-sid="${s.id}">
      <div class="session-col-name" onclick="handleNameClick('${s.id}')" style="cursor:text;" title="Click to rename">
        ${icon}${escHtml(s.display_title)}
      </div>
      <div class="session-col-date" ${colClick}>${escHtml(s.last_activity)}</div>
      <div class="session-col-size" ${colClick}>${escHtml(s.size)}</div>
    </div>`;
  }).join('');

  el.innerHTML = header + rows;
  initColResize();
  attachTooltipListeners();
}

/* ---- Hover tooltip ---- */
function attachTooltipListeners() {
  document.querySelectorAll('.session-item[data-sid]').forEach(row => {
    row.addEventListener('mouseenter', onRowEnter);
    row.addEventListener('mouseleave', onRowLeave);
    row.addEventListener('mousemove',  onRowMove);
  });
}

function onRowEnter(e) {
  const id = e.currentTarget.dataset.sid;
  if (!id) return;
  const s = allSessions.find(x => x.id === id);
  if (!s) return;

  const status = getSessionStatus(id);
  const stateLabels = { question:'\uD83D\uDE4B Question', working:'\u26CF\uFE0F Working', idle:'\uD83D\uDCBB Idle', sleeping:'\uD83D\uDE34 Sleeping' };
  const stateLabel = stateLabels[status] || status;

  const tip = document.getElementById('session-tooltip');
  tip.innerHTML = `
    <div class="tt-title">${escHtml(s.display_title)}</div>
    <div class="tt-meta">
      <span class="tt-state ${status}">${stateLabel}</span>
      <span>${escHtml(s.last_activity)}</span>
      <span>${escHtml(s.size)}</span>
    </div>`;
  tip.classList.add('visible');
  positionTooltip(e);
}

function onRowLeave() {
  const tip = document.getElementById('session-tooltip');
  tip.classList.remove('visible');
}

function onRowMove(e) {
  positionTooltip(e);
}

function positionTooltip(e) {
  const tip = document.getElementById('session-tooltip');
  const margin = 12;
  const vw = window.innerWidth, vh = window.innerHeight;
  const tw = tip.offsetWidth, th = tip.offsetHeight;
  let x = e.clientX + margin;
  let y = e.clientY + margin;
  if (x + tw > vw - 8) x = e.clientX - tw - margin;
  if (y + th > vh - 8) y = e.clientY - th - margin;
  tip.style.left = x + 'px';
  tip.style.top  = y + 'px';
}

/* ---- Column resize ---- */
function initColResize() {
  document.querySelectorAll('.col-resize-grip').forEach(grip => {
    grip.addEventListener('mousedown', e => {
      e.stopPropagation();
      const col = grip.dataset.col;
      const startX = e.clientX;
      const sidebar = document.querySelector('.sidebar');

      // Get current pixel widths from the computed grid
      const computed = getComputedStyle(sidebar);
      const gridCols = getComputedStyle(document.querySelector('.col-header-row'))
        .gridTemplateColumns.split(' ').map(v => parseFloat(v));
      const [wName, wDate, wSize] = gridCols;

      const startVal = col === 'name' ? wName : wDate;

      grip.classList.add('dragging');
      document.body.style.cursor = 'col-resize';
      document.body.style.userSelect = 'none';

      function onMove(ev) {
        const delta = ev.clientX - startX;
        const newVal = Math.max(60, startVal + delta);
        if (col === 'name') {
          document.documentElement.style.setProperty('--col-name', newVal + 'px');
        } else {
          document.documentElement.style.setProperty('--col-date', newVal + 'px');
        }
      }
      function onUp() {
        grip.classList.remove('dragging');
        document.body.style.cursor = '';
        document.body.style.userSelect = '';
        document.removeEventListener('mousemove', onMove);
        document.removeEventListener('mouseup', onUp);
      }
      document.addEventListener('mousemove', onMove);
      document.addEventListener('mouseup', onUp);
    });
  });
}

/* ---- Single / double click ---- */
let _clickTimer = null;
let _lastClickId = null;
let _lastClickTime = 0;

function singleOrDouble(id, e) {
  const now = Date.now();
  const isDouble = (_lastClickId === id && now - _lastClickTime < 400);
  _lastClickId = id;
  _lastClickTime = now;
  if (isDouble) {
    // Double click — cancel pending single-click and open in GUI
    if (_clickTimer) { clearTimeout(_clickTimer); _clickTimer = null; }
    openInGUI(id);
  } else {
    // Single click — delay so double-click can cancel it
    if (_clickTimer) clearTimeout(_clickTimer);
    _clickTimer = setTimeout(() => { _clickTimer = null; selectSession(id); }, 400);
  }
}
