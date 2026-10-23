/* compare.js — session comparison modal */

export function openCompare() {
  const sel = document.getElementById('compare-select');
  sel.innerHTML = allSessions
    .filter(s => s.id !== activeId)
    .map(s => `<option value="${s.id}">${escHtml(s.display_title)}</option>`)
    .join('');
  document.getElementById('compare-body').innerHTML = '<p style="color:var(--text-faint);padding:20px;font-size:12px;">Select a session above and click Compare.</p>';
  document.getElementById('compare-overlay').classList.add('open');
}

export function closeCompare() {
  document.getElementById('compare-overlay').classList.remove('open');
}

document.getElementById('compare-overlay').addEventListener('click', function(e) {
  if (e.target === this) closeCompare();
});

export async function runCompare() {
  const id2 = document.getElementById('compare-select').value;
  if (!id2 || !activeId) return;
  const body = document.getElementById('compare-body');
  body.innerHTML = '<p style="padding:20px;color:var(--text-faint);font-size:12px;">Comparing\u2026</p>';
  try {
    const _p = localStorage.getItem('activeProject') || '';
    const r = await fetch(`/api/compare/${activeId}/${id2}?project=${encodeURIComponent(_p)}`);
    const d = await r.json();
    renderCompare(d);
  } catch(e) {
    body.innerHTML = '<p style="padding:20px;color:var(--result-err);font-size:12px;">Error comparing sessions.</p>';
  }
}

function renderCompare(d) {
  const body = document.getElementById('compare-body');
  const s1 = d.session1, s2 = d.session2;
  const meta = `
    <div class="compare-meta">
      <div class="compare-meta-card">
        <h4>${escHtml(s1.title)}</h4>
        <div>${s1.date} \u00b7 ${s1.size} \u00b7 ${s1.message_count} messages</div>
      </div>
      <div class="compare-meta-card">
        <h4>${escHtml(s2.title)}</h4>
        <div>${s2.date} \u00b7 ${s2.size} \u00b7 ${s2.message_count} messages</div>
      </div>
    </div>`;

  const stats = d.stats;
  const statsBar = `<div style="font-size:11px;color:var(--text-muted);margin-bottom:12px;">
    ${stats.s1_blocks} blocks vs ${stats.s2_blocks} blocks \u00a0\u00b7\u00a0
    <span style="color:var(--idle-label)">+${stats.added} added</span> \u00a0
    <span style="color:var(--result-err)">\u2212${stats.removed} removed</span> \u00a0
    <span style="color:var(--accent)">${stats.changed} changed</span>
  </div>`;

  const diffRows = (d.code_diff || []).map(row => {
    const statusColors = {added:'diff-added',removed:'diff-removed',changed:'diff-changed',same:'diff-same'};
    const cls = statusColors[row.status] || '';
    const badge = `<span class="diff-status-badge">${row.status}</span>`;
    const fn = escHtml(row.filename||row.language||'code');
    const c1 = escHtml((row.content1||'(none)').slice(0,500));
    const c2 = escHtml((row.content2||'(none)').slice(0,500));
    return `<div class="diff-row ${cls}" style="margin-bottom:10px;">
      <div>
        <div class="diff-cell-label">${fn} ${badge}</div>
        <div class="diff-cell">${c1}</div>
      </div>
      <div>
        <div class="diff-cell-label">&nbsp;</div>
        <div class="diff-cell">${c2}</div>
      </div>
    </div>`;
  }).join('') || '<p style="color:var(--text-faint);font-size:12px;padding:10px 0;">No code blocks to compare.</p>';

  body.innerHTML = meta + statsBar + `<div class="sum-label" style="margin-bottom:8px;">Code Comparison</div>` + diffRows;
}

// ES module (2026-10-23): deferred, strict, its own scope.  Inline onclick
// handlers and classic scripts reach these through window.
window.openCompare = openCompare;
window.closeCompare = closeCompare;
window.runCompare = runCompare;
