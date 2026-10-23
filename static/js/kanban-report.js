/**
 * kanban-report.js — Report dashboard rendering.
 *
 * Report types:
 *   1. Current Status Distribution (hero)
 *   2. Completion Velocity
 *   3. Cycle Time & Lead Time
 *   4. Throughput & Cumulative Flow (full-width)
 *   5. Time in Status
 *   6. Remediation & Rework Rate
 *   7. Owner Activity & Contribution
 *   8. Tag Breakdown
 *   9. Stale Task Detection
 *  10. Session Efficiency per Task
 *  11. Daily / Weekly Accomplishments Log (full-width)
 */


// ═══════════════════════════════════════════════════════════════
// REPORT DASHBOARD
// ═══════════════════════════════════════════════════════════════

export async function openReportsPanel() {
  const board = document.getElementById('kanban-board');
  if (!board) return;

  board.innerHTML = '<div class="report-loading"><div class="report-loading-spinner"></div>Loading reports\u2026</div>';

  try {
    const [
      velocityRes, cycleRes, timeStatusRes, distRes,
      cflowRes, remRes, ownerRes, tagRes,
      staleRes, sessEffRes, actLogRes,
    ] = await Promise.all([
      fetch('/api/kanban/report/velocity').then(r => r.json()).catch(() => ({})),
      fetch('/api/kanban/report/cycle-time').then(r => r.json()).catch(() => ({})),
      fetch('/api/kanban/report/time-in-status').then(r => r.json()).catch(() => ({})),
      fetch('/api/kanban/report/distribution').then(r => r.json()).catch(() => ({})),
      fetch('/api/kanban/report/cumulative-flow').then(r => r.json()).catch(() => ({})),
      fetch('/api/kanban/report/remediation').then(r => r.json()).catch(() => ({})),
      fetch('/api/kanban/report/owner-activity').then(r => r.json()).catch(() => ({})),
      fetch('/api/kanban/report/tags').then(r => r.json()).catch(() => ({})),
      fetch('/api/kanban/report/stale').then(r => r.json()).catch(() => ({})),
      fetch('/api/kanban/report/session-efficiency').then(r => r.json()).catch(() => ({})),
      fetch('/api/kanban/report/activity-log').then(r => r.json()).catch(() => ({})),
    ]);

    // Breadcrumb
    let html = '<div class="kanban-drill-titlebar">';
    html += '<div class="kanban-drill-breadcrumb">';
    html += '<span class="kanban-drill-crumb" onclick="navigateToBoard()">' + KI.menu + ' Workflow</span>';
    html += '<span class="kanban-drill-sep">' + KI.chevronR + '</span>';
    html += '<span class="kanban-drill-crumb current">' + KI.chart + ' Reports</span>';
    html += '</div></div>';

    html += '<div class="report-dashboard">';

    // ── Hero row: distribution + velocity side by side ──
    html += '<div class="report-hero-row">';
    html += _renderDistributionCard(distRes);
    html += _renderVelocityCard(velocityRes);
    html += '</div>';

    // ── Main grid ──
    html += '<div class="report-grid">';
    html += _renderCycleTimeCard(cycleRes);
    html += _renderRemediationCard(remRes);
    html += _renderTimeInStatusCard(timeStatusRes);
    html += _renderSessionEfficiencyCard(sessEffRes);
    html += _renderOwnerActivityCard(ownerRes);
    html += _renderTagBreakdownCard(tagRes);
    html += _renderStaleCard(staleRes);
    html += '</div>';

    // ── Full-width cards ──
    html += _renderCumulativeFlowCard(cflowRes);
    html += _renderActivityLogCard(actLogRes);

    html += '</div>'; // report-dashboard

    board.innerHTML = html;
  } catch (e) {
    console.error('[Kanban] Reports load failed:', e);
    board.innerHTML = '<div class="kanban-empty-state"><p>Failed to load reports</p><p style="font-size:12px;color:var(--text-faint);">' + escHtml(e.message) + '</p></div>';
  }
}


// ═══════════════════════════════════════════════════════════════
// INDIVIDUAL REPORT CARDS
// ═══════════════════════════════════════════════════════════════

function _rcard(title, icon, content, cls) {
  return `<div class="report-card${cls ? ' ' + cls : ''}"><div class="report-card-header">${icon}<span>${title}</span></div><div class="report-card-body">${content}</div></div>`;
}

function _renderDistributionCard(data) {
  const breakdown = data.breakdown || [];
  const total = data.total || 0;
  let body = '';
  if (breakdown.length > 0) {
    body += '<div class="report-dist-bars">';
    const maxCount = Math.max(...breakdown.map(b => b.count || 0), 1);
    for (const b of breakdown) {
      const color = KANBAN_STATUS_COLORS[b.status] || 'var(--text-muted)';
      const label = KANBAN_STATUS_LABELS[b.status] || b.status;
      const pct = Math.round(((b.count || 0) / maxCount) * 100);
      body += `<div class="report-dist-item">
        <div class="report-dist-bar-track"><div class="report-dist-bar-fill" style="width:${pct}%;background:${color};"></div></div>
        <div class="report-dist-meta"><span class="report-dist-label" style="color:${color};">${escHtml(label)}</span><span class="report-dist-count">${b.count || 0}</span></div>
      </div>`;
    }
    body += '</div>';
    body += `<div class="report-total">Total: ${total}</div>`;
  } else {
    body = '<div class="report-empty">No tasks yet</div>';
  }
  return _rcard('Status Distribution', KI.chart, body);
}

function _renderVelocityCard(data) {
  const daily = data.daily || [];
  let body = '';
  if (daily.length > 0) {
    const maxV = Math.max(...daily.map(v => v.completed || 0), 1);
    body += '<div class="report-barchart">';
    for (const v of daily.slice(-14)) {
      const h = Math.max(Math.round(((v.completed || 0) / maxV) * 80), 2);
      const dayLabel = v.day ? v.day.split('-').slice(1).join('/') : '?';
      const val = v.completed || 0;
      body += `<div class="report-bar" title="${val} completed on ${v.day}"><div class="report-bar-fill" style="height:${h}px;background:var(--green);"></div><span class="report-bar-val">${val > 0 ? val : ''}</span><span class="report-bar-label">${dayLabel}</span></div>`;
    }
    body += '</div>';
    const weekly = data.weekly || [];
    if (weekly.length > 0) {
      const totalWeekly = weekly.reduce((s, w) => s + (w.completed || 0), 0);
      const avg = (totalWeekly / weekly.length).toFixed(1);
      body += `<div class="report-footnote">Weekly avg: <strong>${avg}</strong> tasks/week</div>`;
    }
  } else {
    body = '<div class="report-empty">No completion data yet</div>';
  }
  return _rcard('Completion Velocity', KI.trendUp, body);
}

function _renderCycleTimeCard(data) {
  const count = data.count || 0;
  const tasks = data.tasks || [];
  let body = '';
  if (count > 0) {
    const medianDays = ((data.median_hours || 0) / 24).toFixed(1);
    const p75Days = ((data.p75_hours || 0) / 24).toFixed(1);
    const p90Days = ((data.p90_hours || 0) / 24).toFixed(1);
    const avgDays = ((data.average_hours || 0) / 24).toFixed(1);
    body += '<div class="report-kpis">';
    body += `<div class="report-kpi"><div class="report-kpi-val">${medianDays}d</div><div class="report-kpi-label">Median</div></div>`;
    body += `<div class="report-kpi"><div class="report-kpi-val">${p75Days}d</div><div class="report-kpi-label">P75</div></div>`;
    body += `<div class="report-kpi"><div class="report-kpi-val">${p90Days}d</div><div class="report-kpi-label">P90</div></div>`;
    body += `<div class="report-kpi"><div class="report-kpi-val">${avgDays}d</div><div class="report-kpi-label">Avg</div></div>`;
    body += `<div class="report-kpi"><div class="report-kpi-val">${count}</div><div class="report-kpi-label">Tasks</div></div>`;
    body += '</div>';
    if (tasks.length > 0) {
      const buckets = { '<1d': 0, '1-2d': 0, '2-3d': 0, '3-5d': 0, '5-7d': 0, '7d+': 0 };
      for (const t of tasks) {
        const days = (t.hours || 0) / 24;
        if (days < 1) buckets['<1d']++;
        else if (days < 2) buckets['1-2d']++;
        else if (days < 3) buckets['2-3d']++;
        else if (days < 5) buckets['3-5d']++;
        else if (days < 7) buckets['5-7d']++;
        else buckets['7d+']++;
      }
      const maxB = Math.max(...Object.values(buckets), 1);
      body += '<div class="report-barchart" style="margin-top:12px;">';
      for (const [label, cnt] of Object.entries(buckets)) {
        const h = Math.max(Math.round((cnt / maxB) * 50), 2);
        body += `<div class="report-bar" title="${cnt} tasks"><div class="report-bar-fill" style="height:${h}px;background:var(--accent);"></div><span class="report-bar-val">${cnt > 0 ? cnt : ''}</span><span class="report-bar-label">${label}</span></div>`;
      }
      body += '</div>';
    }
  } else {
    body = '<div class="report-empty">No cycle time data yet</div>';
  }
  return _rcard('Cycle Time', KI.clock, body);
}

function _renderTimeInStatusCard(data) {
  const statuses = data.statuses || [];
  let body = '';
  if (statuses.length > 0) {
    body += '<table class="report-table"><thead><tr><th>Status</th><th>Avg</th><th>Median</th><th>Max</th></tr></thead><tbody>';
    for (const s of statuses) {
      const color = KANBAN_STATUS_COLORS[s.status] || 'var(--text-muted)';
      const label = KANBAN_STATUS_LABELS[s.status] || s.status;
      const avg = s.avg_days != null ? s.avg_days.toFixed(1) + 'd' : '\u2014';
      const med = s.median_days != null ? s.median_days.toFixed(1) + 'd' : '\u2014';
      const max = s.max_days != null ? s.max_days.toFixed(1) + 'd' : '\u2014';
      body += `<tr><td><span style="color:${color};">${KI.bullet}</span> ${escHtml(label)}</td><td>${avg}</td><td>${med}</td><td>${max}</td></tr>`;
    }
    body += '</tbody></table>';
  } else {
    body = '<div class="report-empty">No status transition data yet</div>';
  }
  return _rcard('Time in Status', KI.clock, body);
}

function _renderRemediationCard(data) {
  const total = data.total_tasks || 0;
  const remediated = data.remediated_tasks || 0;
  const rate = data.rate_percent;
  let body = '';
  if (total > 0) {
    body += '<div class="report-kpis">';
    body += `<div class="report-kpi"><div class="report-kpi-val report-kpi-${rate > 20 ? 'warn' : 'ok'}">${rate != null ? rate + '%' : '\u2014'}</div><div class="report-kpi-label">Rework Rate</div></div>`;
    body += `<div class="report-kpi"><div class="report-kpi-val">${remediated}</div><div class="report-kpi-label">Remediated</div></div>`;
    body += `<div class="report-kpi"><div class="report-kpi-val">${total}</div><div class="report-kpi-label">Total</div></div>`;
    body += '</div>';
  } else {
    body = '<div class="report-empty">No task data yet</div>';
  }
  return _rcard('Remediation', KI.refresh, body);
}

function _renderSessionEfficiencyCard(data) {
  const total = data.total || 0;
  const withS = data.with_sessions || 0;
  const withoutS = data.without_sessions || 0;
  const rate = data.utilization_percent;
  let body = '';
  if (total > 0) {
    body += '<div class="report-kpis">';
    body += `<div class="report-kpi"><div class="report-kpi-val">${rate != null ? rate + '%' : '\u2014'}</div><div class="report-kpi-label">Utilization</div></div>`;
    body += `<div class="report-kpi"><div class="report-kpi-val">${withS}</div><div class="report-kpi-label">With Sessions</div></div>`;
    body += `<div class="report-kpi"><div class="report-kpi-val">${withoutS}</div><div class="report-kpi-label">Without</div></div>`;
    body += `<div class="report-kpi"><div class="report-kpi-val">${total}</div><div class="report-kpi-label">Total</div></div>`;
    body += '</div>';
  } else {
    body = '<div class="report-empty">No session data yet</div>';
  }
  return _rcard('Session Efficiency', KI.zap, body);
}

function _renderOwnerActivityCard(data) {
  const workload = data.workload || [];
  let body = '';
  if (workload.length > 0) {
    body += '<table class="report-table"><thead><tr><th>Owner</th><th>Claimed</th><th>Done</th><th>Active</th></tr></thead><tbody>';
    for (const o of workload) {
      body += `<tr><td class="report-owner-cell">${escHtml(o.owner || 'Unclaimed')}</td><td>${o.claimed || 0}</td><td>${o.completed || 0}</td><td>${o.in_progress || 0}</td></tr>`;
    }
    body += '</tbody></table>';
  } else {
    body = '<div class="report-empty">No owner data</div>';
  }
  return _rcard('Owner Activity', KI.user, body);
}

function _renderTagBreakdownCard(data) {
  const tags = data.tags || [];
  let body = '';
  if (tags.length > 0) {
    const maxCount = Math.max(...tags.map(t => t.count || 0), 1);
    body += '<div class="report-tag-list">';
    for (const t of tags.slice(0, 10)) {
      const tc = tagColorHash(t.tag || '');
      const pct = Math.round(((t.count || 0) / maxCount) * 100);
      body += `<div class="report-tag-row">
        <span class="kanban-tag-pill" style="background:${tc}22;color:${tc};border-color:${tc}44;">${escHtml(t.tag || '')}</span>
        <div class="report-tag-bar"><div style="width:${pct}%;background:${tc};opacity:0.5;height:100%;border-radius:3px;"></div></div>
        <span class="report-tag-count">${t.count || 0}</span>
      </div>`;
    }
    body += '</div>';
  } else {
    body = '<div class="report-empty">No tags yet</div>';
  }
  return _rcard('Tag Breakdown', KI.tag, body);
}

function _renderStaleCard(data) {
  const stale = data.stale || [];
  const threshold = data.threshold_days || 3;
  let body = `<div class="report-footnote" style="margin-bottom:8px;">Inactive for ${threshold}+ days</div>`;
  if (stale.length > 0) {
    for (const s of stale.slice(0, 8)) {
      const sc = KANBAN_STATUS_COLORS[s.status] || 'var(--text-muted)';
      body += `<div class="report-stale-row">
        <span class="report-stale-dot" style="color:${sc};">${KI.bullet}</span>
        <span class="report-stale-title" onclick="navigateToTask('${s.id}')">${escHtml(s.title)}</span>
        <span class="report-stale-days">${s.days_stale || '?'}d</span>
      </div>`;
    }
  } else {
    body += '<div class="report-empty">${KI.checkCirc} No stale tasks</div>';
  }
  return _rcard('Stale Tasks', KI.alertTri, body);
}

function _renderCumulativeFlowCard(data) {
  const trend = data.trend || [];
  let body = '';
  if (trend.length > 0) {
    const maxVal = Math.max(...trend.map(c => Math.max(c.cumulative || 0, c.cumulative_created || 0)), 1);
    body += '<div class="report-legend"><span class="report-legend-item"><span class="report-legend-dot" style="background:var(--accent);"></span>Completed</span><span class="report-legend-item"><span class="report-legend-dot" style="background:var(--green);opacity:0.4;"></span>Created</span><span class="report-legend-item" style="color:var(--text-faint);">Gap = WIP</span></div>';
    body += '<div class="report-barchart report-barchart-tall">';
    for (const c of trend.slice(-21)) {
      const hCompleted = Math.max(Math.round(((c.cumulative || 0) / maxVal) * 100), 2);
      const hCreated = Math.round(((c.cumulative_created || 0) / maxVal) * 100);
      const dayLabel = c.day ? c.day.split('-').slice(1).join('/') : '?';
      body += `<div class="report-bar report-bar-stacked" title="Created: ${c.cumulative_created || 0}, Completed: ${c.cumulative || 0}">
        <div class="report-bar-bg" style="height:${hCreated}px;background:var(--green);opacity:0.25;"></div>
        <div class="report-bar-fill" style="height:${hCompleted}px;background:var(--accent);"></div>
        <span class="report-bar-label">${dayLabel}</span>
      </div>`;
    }
    body += '</div>';
  } else {
    body = '<div class="report-empty">No completion trend data yet</div>';
  }
  return _rcard('Cumulative Flow', KI.trendUp, body, 'report-card-wide');
}

function _renderActivityLogCard(data) {
  const entries = data.entries || [];
  let body = '';
  if (entries.length > 0) {
    const grouped = {};
    for (const e of entries) {
      const day = e.day || 'Unknown';
      if (!grouped[day]) grouped[day] = [];
      grouped[day].push(e);
    }
    for (const [day, items] of Object.entries(grouped)) {
      let dateLabel = day;
      try {
        const d = new Date(day + 'T00:00:00');
        const days = ['Sun', 'Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat'];
        const months = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec'];
        dateLabel = `${days[d.getDay()]}, ${months[d.getMonth()]} ${d.getDate()}`;
      } catch (_) {}

      body += `<div class="report-log-day">${KI.calendar} ${escHtml(dateLabel)}</div>`;
      body += '<div class="report-log-entries">';
      for (const item of items) {
        let icon = KI.bullet, color = 'var(--text-dim)';
        if (item.new_status === 'complete') { icon = KI.check; color = 'var(--green)'; }
        else if (item.new_status === 'working') { icon = KI.play; color = 'var(--accent)'; }
        else if (item.new_status === 'remediating') { icon = KI.refresh; color = 'var(--orange)'; }
        else if (item.new_status === 'validating') { icon = KI.search; color = 'var(--orange)'; }

        const title = item.title || 'Untitled';
        const toStatus = KANBAN_STATUS_LABELS[item.new_status] || item.new_status || '?';
        const time = item.time || '';

        body += `<div class="report-log-entry"><span class="report-log-icon" style="color:${color};">${icon}</span><span class="report-log-title">${escHtml(title)}</span><span class="report-log-status">\u2192 ${escHtml(toStatus)}</span>${time ? '<span class="report-log-time">' + escHtml(time) + '</span>' : ''}</div>`;
      }
      body += '</div>';
    }
    if (data.has_more) {
      body += `<div style="text-align:center;margin-top:12px;"><button class="kanban-sidebar-btn" style="display:inline-flex;width:auto;" onclick="_loadMoreActivityLog('${data.next_cursor || ''}')">Load more\u2026</button></div>`;
    }
  } else {
    body = '<div class="report-empty">No activity recorded yet</div>';
  }
  return _rcard('Activity Log', KI.fileText, body, 'report-card-wide');
}


// ═══════════════════════════════════════════════════════════════
// ACTIVITY LOG PAGINATION
// ═══════════════════════════════════════════════════════════════

export async function _loadMoreActivityLog(cursor) {
  try {
    const res = await fetch('/api/kanban/report/activity-log?cursor=' + encodeURIComponent(cursor));
    if (!res.ok) throw new Error('Failed to load');
    openReportsPanel();
  } catch (e) {
    if (typeof showToast === 'function') showToast('Failed to load more: ' + e.message, true);
  }
}

// ES module (2026-10-23): deferred, strict, its own scope.  Inline onclick
// handlers and classic scripts reach these through window.
window.openReportsPanel = openReportsPanel;
window._loadMoreActivityLog = _loadMoreActivityLog;
