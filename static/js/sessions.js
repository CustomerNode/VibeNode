/* sessions.js — sorting, list rendering, tooltips, column resize, click handling */

function _shortDate(dateStr) {
  const d = new Date(dateStr);
  if (isNaN(d)) return dateStr;
  const now = new Date();
  const today = new Date(now.getFullYear(), now.getMonth(), now.getDate());
  const target = new Date(d.getFullYear(), d.getMonth(), d.getDate());
  const diffDays = Math.round((today - target) / 86400000);

  let h = d.getHours();
  const ampm = h >= 12 ? 'PM' : 'AM';
  h = h % 12 || 12;
  const min = String(d.getMinutes()).padStart(2, '0');
  const time = h + ':' + min + ' ' + ampm;

  if (diffDays === 0) return time;
  if (diffDays === 1) return 'Yesterday';

  const dayNames = ['Sunday','Monday','Tuesday','Wednesday','Thursday','Friday','Saturday'];
  if (diffDays >= 2 && diffDays <= 6) return dayNames[d.getDay()];

  const months = ['Jan','Feb','Mar','Apr','May','Jun','Jul','Aug','Sep','Oct','Nov','Dec'];
  if (d.getFullYear() !== now.getFullYear()) return months[d.getMonth()] + ' ' + d.getDate() + " '" + String(d.getFullYear()).slice(-2);
  return months[d.getMonth()] + ' ' + d.getDate();
}

function setSort(mode) {
  if (sortMode === mode) {
    sortAsc = !sortAsc;   // same column — toggle direction
  } else {
    sortMode = mode;
    sortAsc = false;      // new column — default descending
  }
  localStorage.setItem('sortMode', sortMode);
  localStorage.setItem('sortAsc', sortAsc);
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
    el.innerHTML = '<div style="padding:20px;color:var(--text-muted);font-size:12px;">No sessions found</div>';
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
    const status = getSessionStatus(s.id);
    const isWaiting = status === 'question';
    const isRunning = status === 'working';
    const isIdle = status === 'idle';
    const stateClass = isWaiting ? ' waiting' : (isRunning || isIdle ? ' running' : '');
    const activeClass = s.id === activeId ? ' active' : '';
    const colClick = `onclick="singleOrDouble('${s.id}',event)" style="cursor:pointer;"`;
    const icon = isWaiting
      ? '<svg class="state-icon" width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="#ff9500" stroke-width="2" stroke-linecap="round" title="Waiting for input"><circle cx="12" cy="12" r="10"/><polyline points="12 6 12 12 16 14"/></svg>'
      : isRunning
      ? '<img class="state-icon" src="/static/svg/pickaxe.svg" width="12" height="12" style="filter:brightness(0) saturate(100%) invert(55%) sepia(78%) saturate(1000%) hue-rotate(215deg);" title="Working">'
      : isIdle
      ? '<svg class="state-icon" width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="#44aa66" stroke-width="2" stroke-linecap="round" title="Idle"><polyline points="20 6 9 17 4 12"/></svg>'
      : '';
    return `
    <div class="session-item${activeClass}${stateClass}" data-sid="${s.id}">
      <div class="session-col-name" onclick="handleNameClick('${s.id}')" style="cursor:text;" title="Click to rename">
        ${icon}${escHtml(s.display_title)}${_autoNamingInFlight.has(s.id) ? '<span class="naming-badge"><span class="naming-dot"></span>Naming\u2026</span>' : ''}
      </div>
      <div class="session-col-date" ${colClick} title="${escHtml(s.last_activity)}">${escHtml(_shortDate(s.last_activity))}</div>
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
  const stateLabels = {
    question:'<svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" style="vertical-align:middle;"><circle cx="12" cy="12" r="10"/><path d="M9.09 9a3 3 0 0 1 5.83 1c0 2-3 3-3 3"/><circle cx="12" cy="17" r=".5" fill="currentColor"/></svg> Question',
    working:'<svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" style="vertical-align:middle;"><polygon points="13 2 3 14 12 14 11 22 21 10 12 10 13 2"/></svg> Working',
    idle:'<svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" style="vertical-align:middle;"><polyline points="20 6 9 17 4 12"/></svg> Idle',
    sleeping:'<svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" style="vertical-align:middle;"><path d="M21 12.79A9 9 0 1 1 11.21 3 7 7 0 0 0 21 12.79z"/></svg> Sleeping'
  };
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
  openInGUI(id);
}
