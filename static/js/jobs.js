/* jobs.js — Durable Jobs on the client (spec: docs/plans/durable-jobs-spec.md).
 *
 * Invisible unless a person is needed.  The supervisor in the Session Engine
 * (daemon/job_supervisor.py) relaunches a dead job by itself and shows nothing
 * here.  Only two states reach the browser:
 *
 *   job_needs_person   the job stopped past an irreversible step, died three
 *                      times with no progress, or failed outright.  Every open
 *                      tab (the phone included) gets a card in the shared float
 *                      stack above the composer with the reason and two
 *                      buttons, Relaunch and Leave it, and the same attention
 *                      ping a stalled session gets (VNNotify.attention).
 *   job_registry_error the registry file cannot be read.  A plain error toast.
 *
 * The job list and each log tail live under System → Durable Jobs for
 * diagnosis (openDurableJobs); nothing surfaces them unprompted.
 *
 * The card is a float: it is placed by _layoutFloats() in utils.js like every
 * other bottom-of-screen notice, so it never covers the composer's buttons.
 */
(function () {
  'use strict';

  var _cards = {};   // job_id -> element

  function _pct(p) {
    if (!p || p.pct === null || p.pct === undefined) return '';
    return Math.round(p.pct) + '%';
  }

  function _layout() {
    if (typeof _updateFloatOffset === 'function') _updateFloatOffset();
    else if (typeof _layoutFloats === 'function') _layoutFloats();
  }

  function _post(url) {
    return fetch(url, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: '{}' })
      .then(function (r) { return r.json().catch(function () { return {}; }).then(function (j) { j._status = r.status; return j; }); });
  }

  function _dismiss(jobId) {
    var el = _cards[jobId];
    if (!el) return;
    delete _cards[jobId];
    if (_needsCount !== null) _loadNeedsCount();   // an answered card changes the homepage count
    el.classList.remove('show');
    setTimeout(function () {
      try { el.remove(); } catch (e) {}
      _layout();
    }, 250);
  }

  // The needs-person card: reason, progress, Relaunch, Leave it.
  function showCard(data) {
    data = data || {};
    var jobId = data.job_id || '';
    if (!jobId) return null;
    if (_cards[jobId]) _dismiss(jobId);

    var el = document.createElement('div');
    el.className = 'vn-job-toast';
    el.setAttribute('data-job-id', jobId);

    var head = document.createElement('div');
    head.className = 'vn-job-toast-head';
    var title = document.createElement('span');
    title.className = 'vn-job-toast-title';
    title.textContent = (data.state === 'failed' ? 'Job failed: ' : 'Job needs you: ') + (data.name || jobId);
    head.appendChild(title);
    var pct = _pct(data.progress);
    if (pct) {
      var badge = document.createElement('span');
      badge.className = 'vn-job-toast-pct';
      badge.textContent = pct;
      head.appendChild(badge);
    }
    el.appendChild(head);

    var reason = document.createElement('div');
    reason.className = 'vn-job-toast-reason';
    reason.textContent = data.reason || '';
    el.appendChild(reason);

    var row = document.createElement('div');
    row.className = 'vn-job-toast-actions';
    var relaunch = document.createElement('button');
    relaunch.type = 'button';
    relaunch.className = 'vn-job-toast-btn vn-job-relaunch';
    relaunch.textContent = 'Relaunch';
    var leave = document.createElement('button');
    leave.type = 'button';
    leave.className = 'vn-job-toast-btn vn-job-leave';
    leave.textContent = 'Leave it';
    row.appendChild(relaunch);
    row.appendChild(leave);
    el.appendChild(row);

    relaunch.addEventListener('click', function (ev) {
      ev.stopPropagation();
      relaunch.disabled = leave.disabled = true;
      _post('/api/jobs/' + encodeURIComponent(jobId) + '/relaunch').then(function (j) {
        if (typeof showToast === 'function') showToast(j.ok ? 'Relaunched ' + (data.name || jobId) : 'Relaunch failed: ' + (j.error || j._status), !j.ok);
        _dismiss(jobId);
      }).catch(function (e) {
        relaunch.disabled = leave.disabled = false;
        if (typeof showToast === 'function') showToast('Relaunch failed: ' + e.message, true);
      });
    });
    leave.addEventListener('click', function (ev) {
      ev.stopPropagation();
      relaunch.disabled = leave.disabled = true;
      _post('/api/jobs/' + encodeURIComponent(jobId) + '/leave').then(function () { _dismiss(jobId); })
        .catch(function () { _dismiss(jobId); });
    });

    document.body.appendChild(el);
    _cards[jobId] = el;
    _layout();
    requestAnimationFrame(function () { el.classList.add('show'); });
    return el;
  }

  // How many jobs need a person (needs_person or failed), for the homepage
  // strip (toolbar.js _hpStatusStrip).  Read from /api/jobs on first ask, and
  // after that re-read when a job_needs_person push arrives or a card is
  // answered.  Never fetched until something asks (no cost off the homepage).
  // null = not read yet.
  var _needsCount = null;
  var _needsLoading = false;
  function _loadNeedsCount() {
    if (_needsLoading) return;
    _needsLoading = true;
    fetch('/api/jobs').then(function (r) { return r.ok ? r.json() : null; }).then(function (j) {
      var jobs = (j && j.ok && Array.isArray(j.jobs)) ? j.jobs : [];
      // A job the person dismissed (Leave it) no longer needs them.
      _needsCount = jobs.filter(function (x) { return x && !x.acknowledged && (x.state === 'needs_person' || x.state === 'failed'); }).length;
    }).catch(function () { _needsCount = 0; }).then(function () {
      _needsLoading = false;
      if (typeof viewMode !== 'undefined' && viewMode === 'homepage'
          && typeof _updateHomepageStats === 'function') _updateHomepageStats();
    });
  }
  function needsPersonCount() {
    if (_needsCount === null) _loadNeedsCount();
    return _needsCount || 0;
  }

  // Socket handlers (wired from socket.js).
  function onNeedsPerson(data) {
    data = data || {};
    showCard(data);
    if (_needsCount !== null) _loadNeedsCount();   // only once something has asked for it
    try {
      if (window.VNNotify && typeof window.VNNotify.attention === 'function') {
        window.VNNotify.attention({
          sessionId: 'job:' + (data.job_id || ''),
          sessionName: data.name || 'A job',
          title: data.state === 'failed' ? 'Job failed' : 'Job needs you',
          body: (data.name || 'A job') + ': ' + (data.reason || 'needs a decision'),
        });
      }
    } catch (e) { /* notify failures never break the card */ }
  }

  function onRegistryError(data) {
    data = data || {};
    if (typeof showToast === 'function') showToast('Durable Jobs registry unreadable: ' + (data.error || data.path || ''), true);
  }

  // ── System → Durable Jobs ────────────────────────────────────────────
  function _esc(s) {
    return String(s === undefined || s === null ? '' : s).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;');
  }

  function _ago(ts) {
    if (!ts) return '';
    var s = Math.max(0, Math.round(Date.now() / 1000 - ts));
    if (s < 90) return s + 's ago';
    if (s < 5400) return Math.round(s / 60) + ' min ago';
    if (s < 172800) return Math.round(s / 3600) + ' h ago';
    return Math.round(s / 86400) + ' d ago';
  }

  function jobRowHtml(j) {
    var p = j.progress_value || {};
    var pct = _pct(p);
    var bar = pct ? '<div class="vn-job-bar"><div class="vn-job-bar-fill" style="width:' + Math.round(p.pct) + '%"></div></div>' : '';
    var meta = [];
    if (pct) meta.push('<span class="vn-job-pct">' + pct + '</span>' + (p.text ? ' <span class="vn-job-ptext">' + _esc(p.text) + '</span>' : ''));
    if (j.last_seen_alive) meta.push('seen ' + _ago(j.last_seen_alive));
    if (j.relaunch_count) meta.push('relaunched ' + j.relaunch_count + '×');
    if (j.project) meta.push(_esc(j.project));
    return '<div class="vn-job-row" data-job-id="' + _esc(j.id) + '">' +
      '<div class="vn-job-row-head"><span class="vn-job-state vn-job-state-' + _esc(j.state) + '">' + _esc(j.state.replace('_', ' ')) + '</span>' +
      '<span class="vn-job-name">' + _esc(j.name || j.id) + '</span></div>' +
      (j.reason ? '<div class="vn-job-reason">' + _esc(j.reason) + '</div>' : '') +
      bar + '<div class="vn-job-meta">' + meta.join(' · ') + '</div>' +
      '<div class="vn-job-cmd">' + _esc(j.command) + '</div>' +
      '<div class="vn-job-actions"><button type="button" class="vn-job-log-btn">Log</button>' +
      (j.state !== 'finished' ? '<button type="button" class="vn-job-relaunch-btn">Relaunch</button>' : '') +
      '</div><pre class="vn-job-log" hidden></pre></div>';
  }

  function renderList(container, jobs) {
    if (Array.isArray(jobs)) {   // the list already read: keep the homepage count in step
      // A job the person dismissed (Leave it) no longer needs them.
      _needsCount = jobs.filter(function (x) { return x && !x.acknowledged && (x.state === 'needs_person' || x.state === 'failed'); }).length;
    }
    if (!jobs || !jobs.length) {
      container.innerHTML = '<div class="vn-job-empty">No durable jobs registered on this machine. The run-detached skill registers one for every long job it launches.</div>';
      return;
    }
    container.innerHTML = jobs.map(jobRowHtml).join('');
  }

  async function openDurableJobs() {
    var overlay = document.getElementById('pm-overlay');
    if (!overlay) return;
    overlay.innerHTML = '<div class="pm-card pm-enter" style="max-width:640px;"><h2 class="pm-title">Durable Jobs</h2>' +
      '<div class="pm-body"><div class="vn-job-intro">Long jobs launched through VibeNode. They come back by themselves after a restart; one asks for you only when it cannot decide alone.</div>' +
      '<div id="vn-job-list"><span class="spinner"></span> Loading…</div></div>' +
      '<div class="pm-actions"><button type="button" onclick="if(typeof _closePm===\'function\')_closePm()">Close</button></div></div>';
    overlay.classList.add('show');
    requestAnimationFrame(function () { var c = overlay.querySelector('.pm-card'); if (c) c.classList.remove('pm-enter'); });
    overlay.onclick = function (e) { if (e.target === overlay && typeof _closePm === 'function') _closePm(); };
    var list = overlay.querySelector('#vn-job-list');
    list.addEventListener('click', function (e) {
      var row = e.target.closest('.vn-job-row');
      if (!row) return;
      var id = row.getAttribute('data-job-id');
      if (e.target.classList.contains('vn-job-log-btn')) {
        var pre = row.querySelector('.vn-job-log');
        if (!pre.hidden) { pre.hidden = true; return; }
        pre.hidden = false;
        pre.textContent = 'Loading…';
        fetch('/api/jobs/' + encodeURIComponent(id) + '/log').then(function (r) { return r.json(); })
          .then(function (j) { pre.textContent = (j.path ? j.path + '\n\n' : '') + (j.tail || '(empty)'); })
          .catch(function (err) { pre.textContent = 'Could not read the log: ' + err.message; });
      } else if (e.target.classList.contains('vn-job-relaunch-btn')) {
        e.target.disabled = true;
        _post('/api/jobs/' + encodeURIComponent(id) + '/relaunch').then(function (j) {
          if (typeof showToast === 'function') showToast(j.ok ? 'Relaunched' : 'Relaunch failed: ' + (j.error || ''), !j.ok);
          return refresh();
        });
      }
    });
    async function refresh() {
      try {
        var r = await fetch('/api/jobs');
        var j = await r.json();
        if (!j.ok) { list.innerHTML = '<div class="vn-job-empty">' + _esc(j.error || 'Could not read the registry') + '</div>'; return; }
        renderList(list, j.jobs);
      } catch (e) {
        list.innerHTML = '<div class="vn-job-empty">' + _esc(e.message) + '</div>';
      }
    }
    await refresh();
  }

  window.VNJobs = { showCard: showCard, onNeedsPerson: onNeedsPerson, onRegistryError: onRegistryError,
                    renderList: renderList, jobRowHtml: jobRowHtml, dismiss: _dismiss,
                    needsPersonCount: needsPersonCount, refreshNeedsCount: _loadNeedsCount };
  window.openDurableJobs = openDurableJobs;
})();
