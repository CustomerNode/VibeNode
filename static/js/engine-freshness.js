/* engine-freshness.js — "the Session Engine is running outdated code" banner
 *
 * Why (2026-10-20): the Session Engine keeps the code it started with until
 * it restarts, and it had run since Oct 6.  Every session-recovery fix since
 * then sat on disk without running, and nothing said so.  This banner says
 * so, and offers "Restart when idle": the server waits until no session is
 * busy (working, asking, writing output, or running a background command)
 * and then restarts the engine.  "Restart now" goes through the same
 * Developer Tools restart as before.
 *
 * Server side: GET /api/engine/status, POST /api/engine/restart-when-idle
 * (app/engine_freshness.py).  Silent when the engine is current.
 */
(function () {
  'use strict';
  if (window._engineFreshnessLoaded) return;
  window._engineFreshnessLoaded = true;

  var BANNER_ID = 'engine-freshness-banner';
  var SNOOZE_KEY = 'engineFreshnessSnoozedUntil';
  var BOOT_DELAY_MS = 4000;
  var RECHECK_MS = 10 * 60 * 1000;
  var ARMED_RECHECK_MS = 15 * 1000;
  var _timer = null;

  // In-memory copy, so "Later" holds for this page even where storage is
  // blocked (private windows, sandboxed documents).
  var _memSnooze = 0;

  function _snoozedUntil() {
    var stored = 0;
    try { stored = parseInt(localStorage.getItem(SNOOZE_KEY) || '0', 10) || 0; }
    catch (e) { /* storage blocked */ }
    return Math.max(stored, _memSnooze);
  }

  function _snooze(hours) {
    _memSnooze = Date.now() + hours * 3600 * 1000;
    try { localStorage.setItem(SNOOZE_KEY, String(_memSnooze)); }
    catch (e) { /* this page view only */ }
    _remove();
  }

  function _remove() {
    var el = document.getElementById(BANNER_ID);
    if (el && el.parentNode) el.parentNode.removeChild(el);
  }

  function _esc(s) {
    if (typeof escHtml === 'function') return escHtml(s);
    return String(s == null ? '' : s).replace(/[&<>"]/g, function (c) {
      return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c];
    });
  }

  function _since(ts) {
    try {
      return new Date(ts * 1000).toLocaleString(undefined, {
        month: 'short', day: 'numeric', hour: 'numeric', minute: '2-digit',
      });
    } catch (e) { return 'an earlier start'; }
  }

  function _post(arm) {
    return fetch('/api/engine/restart-when-idle', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ arm: arm }),
    }).then(function (r) { return r.json(); });
  }

  function _render(data) {
    _remove();
    var armed = !!data.restart_armed;
    var n = (data.outdated || []).length;
    var msg;
    if (armed) {
      var waits = data.waiting_on || [];
      msg = 'The Session Engine will restart as soon as no session is busy.'
          + (waits.length ? ' Waiting on: ' + waits.slice(0, 4).join('; ')
                            + (waits.length > 4 ? ' …' : '') + '.' : '');
    } else {
      msg = 'The Session Engine has been running since ' + _since(data.started_at)
          + '. ' + n + ' engine file' + (n === 1 ? ' has' : 's have')
          + ' changed since then, and those changes (session recovery fixes '
          + 'included) are not running until it restarts.';
    }
    var bar = document.createElement('div');
    bar.id = BANNER_ID;
    bar.setAttribute('role', 'status');
    var other = document.getElementById('claude-update-banner');
    var top = other ? other.offsetHeight : 0;
    bar.style.cssText = [
      'position:fixed', 'top:' + top + 'px', 'left:0', 'right:0', 'z-index:9998',
      'background:#4b2a00', 'color:#ffd9a0', 'padding:8px 12px',
      'display:flex', 'align-items:center', 'gap:10px', 'font-size:13px',
      'box-shadow:0 2px 6px rgba(0,0,0,.3)',
    ].join(';');
    bar.title = (data.outdated || []).join('\n');
    bar.innerHTML = '<span style="flex:1">' + _esc(msg) + '</span>'
      + (armed
          ? '<button data-act="now" class="pm-btn pm-btn-secondary" style="padding:4px 12px">Restart now</button>'
            + '<button data-act="cancel" class="pm-btn pm-btn-secondary" style="padding:4px 12px">Cancel</button>'
          : '<button data-act="arm" class="pm-btn pm-btn-primary" style="padding:4px 12px">Restart when idle</button>'
            + '<button data-act="later" class="pm-btn pm-btn-secondary" style="padding:4px 12px">Later</button>');
    document.body.appendChild(bar);
    bar.addEventListener('click', function (e) {
      var act = e.target && e.target.getAttribute && e.target.getAttribute('data-act');
      if (!act) return;
      if (act === 'later') { _snooze(4); return; }
      if (act === 'arm') { _post(true).then(_check); return; }
      if (act === 'cancel') { _post(false).then(_check); return; }
      if (act === 'now') {
        var ask = (typeof showConfirm === 'function')
          ? showConfirm('Restart the Session Engine now?',
              '<p>Sessions that are working right now will be cut off. '
              + 'Idle sessions can be woken afterwards.</p>',
              { confirmText: 'Restart now', cancelText: 'Keep waiting' })
          : Promise.resolve(window.confirm('Restart the Session Engine now?'));
        ask.then(function (ok) {
          if (!ok) return;
          _post(false).finally(function () {
            if (typeof _doRestart === 'function') _doRestart('daemon');
          });
        });
      }
    });
  }

  function _schedule(ms) {
    clearTimeout(_timer);
    _timer = setTimeout(_check, ms);
  }

  function _check() {
    fetch('/api/engine/status')
      .then(function (r) { return r.ok ? r.json() : null; })
      .then(function (data) {
        if (!data || !data.ok || !data.running || (!data.stale && !data.restart_armed)) {
          _remove();
          _schedule(RECHECK_MS);
          return;
        }
        if (!data.restart_armed && Date.now() < _snoozedUntil()) {
          _remove();
          _schedule(RECHECK_MS);
          return;
        }
        _render(data);
        _schedule(data.restart_armed ? ARMED_RECHECK_MS : RECHECK_MS);
      })
      .catch(function () { _schedule(RECHECK_MS); });
  }

  window.EngineFreshness = { check: _check };

  function _boot() { setTimeout(_check, BOOT_DELAY_MS); }
  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', _boot);
  } else {
    _boot();
  }
})();
