/* Weekly usage pace pop-ups: a big number and the 24-hour curve.
 *
 * Fast day: every time 24 hours spend another whole day of the weekly limit
 * (1/7), "N days of usage in the last 24 hours" with Stop all sessions / Keep
 * going.  Spare: when the week has at least twice the usage the days to the
 * reset would normally spend, days left vs days to reset, a project (the one
 * with the most sessions this month first), a default job or your own, and
 * Start it.  Both are on unless turned off in Preferences or with the
 * pop-up's own box.  Detection: src/web/usage_pace.py; routes:
 * src/web/routes/usage_pace_api.py.  Loads after socket.js (uses its `socket`).
 */
(function () {
  'use strict';

  const TASKS = [
    ['Load optimization', 'Profile page and API load times in this project, find the biggest costs, and speed them up without changing behaviour.'],
    ['Security pass', 'Do a thorough security review of this project: find vulnerabilities (injection, auth, secrets, unsafe file or network access) and fix them.'],
    ['Test suite runtime', 'Make this project\'s test suite run faster without weakening any test: measure, find the slowest parts, and fix them.'],
    ['Dead code cleanup', 'Find code in this project that nothing uses any more and remove it, proving with the tests that nothing breaks.'],
  ];
  const PREF = { fast_day: 'usage_pace_warning', spare: 'usage_spare_suggest' };
  const queue = [];
  let showing = null;

  const esc = s => String(s == null ? '' : s).replace(/[&<>"']/g,
    c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
  const toast = (m, err) => { if (typeof showToast === 'function') showToast(m, err); };
  const post = (url, body) => fetch(url, { method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body || {}) }).then(r => r.json().then(j => (r.ok ? j : Promise.reject(j))));

  function fmtDays(n) {
    const r = Math.round(n * 10) / 10;
    return (Number.isInteger(r) ? String(r) : r.toFixed(1));
  }

  function resetText(ts) {
    if (!ts) return '';
    const d = new Date(ts * 1000);
    return d.toLocaleString([], { weekday: 'short', hour: 'numeric', minute: '2-digit' });
  }

  // The last 24 hours as a line: x = hours ago (24 at the left), y = share used.
  function curveSvg(curve, days) {
    const pts = Array.isArray(curve) && curve.length > 1 ? curve : [[24, 0], [0, days / 7]];
    const top = Math.max(pts[pts.length - 1][1], 1 / 7) * 1.1;
    const xy = pts.map(([h, u]) => (100 - (h / 24) * 100).toFixed(1) + ',' + (29 - (u / top) * 27).toFixed(1));
    return '<svg class="vn-up-spark" viewBox="0 0 100 30" preserveAspectRatio="none" aria-hidden="true">'
      + '<polyline fill="none" stroke="#e0a030" stroke-width="1.5" vector-effect="non-scaling-stroke" points="' + xy.join(' ') + '"/></svg>'
      + '<div class="vn-up-axis"><span>24 h ago</span><span>now</span></div>';
  }

  const offBox = '<label class="vn-up-off"><input type="checkbox" class="vn-up-offbox"> Don\'t show this again (Settings &rarr; Preferences)</label>';

  function fastHtml(e) {
    const n = e.days || 1;
    return '<div class="pm-card pm-enter vn-up" data-kind="fast_day">'
      + '<div class="pm-body vn-up-center"><div class="vn-up-big">' + n + '</div>'
      + '<div class="vn-up-sub">day' + (n > 1 ? 's' : '') + ' of usage in the last 24 hours</div>'
      + curveSvg(e.curve, n)
      + '<div class="vn-up-note">' + (e.week_percent != null ? 'Week ' + esc(e.week_percent) + '% used' : '')
      + (e.resets_at ? ' &middot; resets ' + esc(resetText(e.resets_at)) : '') + '</div>'
      + offBox + '</div>'
      + '<div class="pm-actions"><button class="pm-btn pm-btn-danger vn-up-stop">Stop all sessions</button>'
      + '<button class="pm-btn pm-btn-primary vn-up-keep">Keep going</button></div></div>';
  }

  function spareHtml(e) {
    return '<div class="pm-card pm-enter vn-up" data-kind="spare">'
      + '<div class="pm-body"><div class="vn-up-pair">'
      + '<div><div class="vn-up-big vn-up-green">' + fmtDays(e.days_left || 0) + '</div><small>days of usage left</small></div>'
      + '<div><div class="vn-up-big">' + fmtDays(e.days_to_reset || 0) + '</div><small>days to the reset</small></div></div>'
      + '<label class="vn-up-fld">Project</label><select class="vn-up-proj"><option value="">Loading projects&hellip;</option></select>'
      + '<label class="vn-up-fld">Job</label><select class="vn-up-job">'
      + TASKS.map((t, i) => '<option value="' + i + '">' + esc(t[0]) + '</option>').join('')
      + '<option value="own">Type my own&hellip;</option></select>'
      + '<label class="vn-up-fld">Or type your own</label>'
      + '<textarea class="vn-up-task" placeholder="Describe a long job for it to work through on its own"></textarea>'
      + offBox + '</div>'
      + '<div class="pm-actions"><button class="pm-btn pm-btn-secondary vn-up-later">Not now</button>'
      + '<button class="pm-btn pm-btn-primary vn-up-go">Start it</button></div></div>';
  }

  function overlayBusy() {
    const o = document.getElementById('pm-overlay');
    return o && o.classList.contains('show') && !o.querySelector('.vn-up');
  }

  function show(e) {
    if (!e || !PREF[e.kind]) return;
    const i = queue.findIndex(q => q.kind === e.kind);
    if (i >= 0) queue.splice(i, 1);
    if (showing && showing.kind === e.kind) { showing = null; }   // newer level replaces it
    queue.push(e);
    pump();
  }

  function pump() {
    if (showing || !queue.length) return;
    if (overlayBusy()) { setTimeout(pump, 5000); return; }
    // The warning first: it may want everything stopped.
    queue.sort((a, b) => (a.kind === 'fast_day' ? -1 : 1) - (b.kind === 'fast_day' ? -1 : 1));
    const e = queue.shift();
    const o = document.getElementById('pm-overlay');
    if (!o) return;
    showing = e;
    o.innerHTML = e.kind === 'fast_day' ? fastHtml(e) : spareHtml(e);
    o.classList.add('show');
    o.onclick = null;   // a stray click outside must not dismiss a warning
    requestAnimationFrame(() => { const c = o.querySelector('.pm-card'); if (c) c.classList.remove('pm-enter'); });
    wire(o, e);
  }

  function close(e, opts) {
    const o = document.getElementById('pm-overlay');
    const box = o && o.querySelector('.vn-up-offbox');
    if (box && box.checked && !(opts && opts.remote)) {
      fetch('/api/kanban/config', { method: 'PUT', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ [PREF[e.kind]]: false }) })
        .then(r => { if (!r.ok) throw 0; toast(e.kind === 'fast_day' ? 'Fast-day warnings off' : 'Spare-usage suggestions off'); })
        .catch(() => toast('Could not save that setting', true));
    }
    if (!(opts && opts.remote)) post('/api/usage-pace/ack', { kind: e.kind }).catch(() => {});
    showing = null;
    if (typeof _closePm === 'function') _closePm();
    setTimeout(pump, 400);
  }

  function wire(o, e) {
    const q = s => o.querySelector(s);
    if (e.kind === 'fast_day') {
      q('.vn-up-keep').onclick = () => close(e);
      q('.vn-up-stop').onclick = () => {
        q('.vn-up-stop').disabled = true;
        post('/api/usage-pace/stop-all').then(r => {
          markStopped(r.closed || []);
          const n = (r.closed || []).length;
          toast(n ? n + ' session' + (n > 1 ? 's' : '') + ' stopped' : 'No running sessions');
          close(e);
        }).catch(() => { q('.vn-up-stop').disabled = false; toast('Could not stop the sessions', true); });
      };
      return;
    }
    const proj = q('.vn-up-proj'), job = q('.vn-up-job'), ta = q('.vn-up-task');
    fetch('/api/usage-pace/projects').then(r => r.json()).then(d => {
      const ps = (d && d.projects) || [];
      proj.innerHTML = ps.length ? ps.map((p, i) => '<option value="' + esc(p.cwd) + '">' + esc(p.name)
        + ' (' + p.recent + ' session' + (p.recent === 1 ? '' : 's') + ' this month)' + '</option>').join('')
        : '<option value="">No projects with sessions this month</option>';
    }).catch(() => { proj.innerHTML = '<option value="">Could not load projects</option>'; });
    job.onchange = () => { if (job.value === 'own') ta.focus(); };
    ta.oninput = () => { if (ta.value.trim()) job.value = 'own'; };
    q('.vn-up-later').onclick = () => close(e);
    q('.vn-up-go').onclick = () => {
      const typed = ta.value.trim();
      const pick = TASKS[Number(job.value)];
      const task = typed || (pick ? pick[1] : '');
      if (!task) { ta.focus(); return; }
      if (!proj.value) { toast('Pick a project first', true); return; }
      const go = q('.vn-up-go'); go.disabled = true;
      const name = typed ? typed.slice(0, 60) : pick[0];
      post('/api/usage-pace/start', { cwd: proj.value, task, name }).then(() => {
        toast('Started "' + name + '" in ' + proj.options[proj.selectedIndex].text.replace(/ \(.*\)$/, ''));
        close(e);
      }).catch(err => { go.disabled = false; toast('Could not start it: ' + ((err && err.error) || 'error'), true); });
    };
  }

  // Every tab that knows these sessions treats the stop as the person's own,
  // so ghost recovery never wakes them ("Sleep must stick").
  function markStopped(ids) {
    ids.forEach(id => {
      if (typeof markUserStopped === 'function') markUserStopped(id);
      if (window.SessionStore && typeof runningIds !== 'undefined' && runningIds.delete) runningIds.delete(id);
    });
  }

  function init() {
    if (typeof socket !== 'undefined' && socket && socket.on) {
      socket.on('usage_pace', show);
      socket.on('usage_pace_cleared', d => {
        const k = d && d.kind;
        const i = queue.findIndex(x => x.kind === k);
        if (i >= 0) queue.splice(i, 1);
        if (showing && showing.kind === k) close(showing, { remote: true });
      });
      socket.on('usage_pace_stopped', d => markStopped((d && d.session_ids) || []));
    }
    fetch('/api/usage-pace').then(r => (r.ok ? r.json() : null))
      .then(d => ((d && d.pending) || []).forEach(show)).catch(() => {});
  }

  window.VNUsagePace = { show, TASKS };
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', init);
  else init();
})();
