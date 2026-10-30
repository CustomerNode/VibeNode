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
 *
 * Second banner (2026-10-23): "SDK patches did not apply".  The engine patches
 * the Claude SDK's internals at start (daemon/sdk_patches.py); a patch whose
 * precondition failed used to be one WARNING line in a log, and the engine ran
 * degraded with nobody told.  The engine now records the failed patch names
 * and the SDK version (daemon/engine_code.py); the status endpoint passes them
 * through as sdk_patch_failures / sdk_version, and a red banner names them.
 */
(function () {
  'use strict';
  if (window._engineFreshnessLoaded) return;
  window._engineFreshnessLoaded = true;

  var BANNER_ID = 'engine-freshness-banner';
  var SDK_BANNER_ID = 'sdk-patch-banner';
  var SNOOZE_KEY = 'engineFreshnessSnoozedUntil';
  // "Until it restarts" (2026-10-30): the engine start this hide was for.
  // A later engine start shows the banner again whatever the clock says.
  var SNOOZE_START_KEY = 'engineFreshnessSnoozedStart';
  var MENU_ID = 'engine-freshness-hide-menu';
  var _sdkDismissed = false;   // this page view only: the engine is still unpatched
  var BOOT_DELAY_MS = 4000;
  var RECHECK_MS = 10 * 60 * 1000;
  var ARMED_RECHECK_MS = 15 * 1000;
  var _timer = null;

  // In-memory copy, so "Later" holds for this page even where storage is
  // blocked (private windows, sandboxed documents).
  var _memSnooze = 0;
  var _memSnoozeStart = 0;
  var _lastStartedAt = 0;

  function _snoozedUntil() {
    var stored = 0;
    try { stored = parseInt(localStorage.getItem(SNOOZE_KEY) || '0', 10) || 0; }
    catch (e) { /* storage blocked */ }
    return Math.max(stored, _memSnooze);
  }

  function _snoozedStart() {
    var stored = 0;
    try { stored = parseInt(localStorage.getItem(SNOOZE_START_KEY) || '0', 10) || 0; }
    catch (e) { /* storage blocked */ }
    return stored || _memSnoozeStart;
  }

  /** Hidden right now: a time snooze still running, or a hide "until it
   *  restarts" for the engine start that is still running. */
  function _isSnoozed(data) {
    if (Date.now() < _snoozedUntil()) return true;
    var start = _snoozedStart();
    return !!(start && data && data.started_at && Number(data.started_at) === start);
  }

  // The Hide menu's choices (owner, 2026-10-30: "a button that delays the
  // window from returning ... some menu of time options").
  var HIDE_CHOICES = [
    { key: '1h', label: 'For 1 hour' },
    { key: '4h', label: 'For 4 hours' },
    { key: 'morning', label: 'Until tomorrow morning' },
    { key: 'week', label: 'For a week' },
    { key: 'restart', label: 'Until it restarts' },
  ];

  /** When a choice ends, as a clock time (ms); 0 for "until it restarts". */
  function _hideUntil(key, now) {
    now = now || Date.now();
    if (key === '1h') return now + 3600 * 1000;
    if (key === '4h') return now + 4 * 3600 * 1000;
    if (key === 'week') return now + 7 * 24 * 3600 * 1000;
    if (key === 'morning') {
      var d = new Date(now);
      d.setDate(d.getDate() + 1);
      d.setHours(8, 0, 0, 0);
      return d.getTime();
    }
    return 0;
  }

  function _snoozeChoice(key) {
    if (key === 'restart' && !Number(_lastStartedAt)) key = '4h';  // no start time to key on
    if (key === 'restart') {
      _memSnoozeStart = Number(_lastStartedAt) || 0;
      try { localStorage.setItem(SNOOZE_START_KEY, String(_memSnoozeStart)); }
      catch (e) { /* this page view only */ }
      _remove();
      return;
    }
    _memSnooze = _hideUntil(key);
    try { localStorage.setItem(SNOOZE_KEY, String(_memSnooze)); }
    catch (e) { /* this page view only */ }
    _remove();
    // Come back when the hide ends, not up to ten minutes after it.
    _schedule(Math.max(1000, Math.min(RECHECK_MS, _memSnooze - Date.now() + 500)));
  }

  function _closeMenu() {
    var m = document.getElementById(MENU_ID);
    if (m && m.parentNode) m.parentNode.removeChild(m);
    var b = document.querySelector('#' + BANNER_ID + ' [data-act=later]');
    if (b) b.setAttribute('aria-expanded', 'false');
  }

  function _openMenu(btn) {
    _closeMenu();
    var r = btn.getBoundingClientRect();
    var m = document.createElement('div');
    m.id = MENU_ID;
    m.setAttribute('role', 'menu');
    m.style.cssText = [
      'position:fixed', 'top:' + Math.round(r.bottom + 4) + 'px',
      'right:' + Math.max(8, Math.round(window.innerWidth - r.right)) + 'px',
      'z-index:10000', 'background:#2e1a00', 'color:#ffd9a0',
      'border:1px solid #7a4a10', 'border-radius:8px', 'padding:4px',
      'min-width:190px', 'box-shadow:0 6px 18px rgba(0,0,0,.4)', 'font-size:13px',
    ].join(';');
    m.innerHTML = '<div style="padding:6px 10px 4px;opacity:.7;font-size:12px">Hide this banner</div>'
      + HIDE_CHOICES.map(function (c) {
        return '<button role="menuitem" data-hide="' + c.key + '" style="display:block;width:100%;'
          + 'text-align:left;background:none;border:0;color:inherit;font:inherit;padding:7px 10px;'
          + 'border-radius:6px;cursor:pointer">' + _esc(c.label) + '</button>';
      }).join('');
    document.body.appendChild(m);
    btn.setAttribute('aria-expanded', 'true');
    m.addEventListener('mouseover', function (e) {
      var t = e.target.closest && e.target.closest('[data-hide]');
      Array.prototype.forEach.call(m.querySelectorAll('[data-hide]'), function (b) {
        b.style.background = (b === t) ? 'rgba(255,217,160,.12)' : 'none';
      });
    });
    m.addEventListener('click', function (e) {
      var t = e.target && e.target.closest && e.target.closest('[data-hide]');
      if (!t) return;
      _closeMenu();
      _snoozeChoice(t.getAttribute('data-hide'));
    });
  }

  document.addEventListener('mousedown', function (e) {
    var m = document.getElementById(MENU_ID);
    if (!m) return;
    if (m.contains(e.target)) return;
    if (e.target.closest && e.target.closest('#' + BANNER_ID + ' [data-act=later]')) return;
    _closeMenu();
  }, true);
  document.addEventListener('keydown', function (e) {
    if (e.key === 'Escape') _closeMenu();
  });

  function _remove() {
    _closeMenu();
    var el = document.getElementById(BANNER_ID);
    if (el && el.parentNode) el.parentNode.removeChild(el);
  }

  function _removeSdk() {
    var el = document.getElementById(SDK_BANNER_ID);
    if (el && el.parentNode) el.parentNode.removeChild(el);
  }

  /** Height of the fixed banners already stacked at the top of the page. */
  function _stackTop(ids) {
    var top = 0;
    for (var i = 0; i < ids.length; i++) {
      var el = document.getElementById(ids[i]);
      if (el) top += el.offsetHeight;
    }
    return top;
  }

  function _renderSdk(data) {
    _removeSdk();
    var names = (data.sdk_patch_failures || []).map(String);
    var msg = 'SDK patches did not apply: ' + names.join(', ')
      + (data.sdk_version ? ' (SDK ' + data.sdk_version + ')' : '') + '.';
    if (data.sdk_version_tested === false) {
      msg += ' This SDK version is outside the range the patches were tested against.';
    }
    msg += ' Sessions run without them; restart the Session Engine after fixing the SDK install.';
    var details = (data.sdk_patch_failure_details || []).map(function (d) {
      return (d && d.name ? d.name + ': ' : '') + (d && d.precondition ? d.precondition : '');
    }).filter(Boolean);
    var bar = document.createElement('div');
    bar.id = SDK_BANNER_ID;
    bar.setAttribute('role', 'alert');
    bar.style.cssText = [
      'position:fixed', 'top:' + _stackTop(['claude-update-banner']) + 'px', 'left:0', 'right:0',
      'z-index:9999', 'background:#5a1111', 'color:#ffd6d6', 'padding:8px 12px',
      'display:flex', 'align-items:center', 'gap:10px', 'font-size:13px',
      'box-shadow:0 2px 6px rgba(0,0,0,.3)',
    ].join(';');
    bar.title = details.join('\n');
    bar.innerHTML = '<span style="flex:1">' + _esc(msg) + '</span>'
      + '<button data-act="sdk-dismiss" class="pm-btn pm-btn-secondary" style="padding:4px 12px">Dismiss</button>';
    document.body.appendChild(bar);
    bar.addEventListener('click', function (e) {
      var act = e.target && e.target.getAttribute && e.target.getAttribute('data-act');
      if (act === 'sdk-dismiss') { _sdkDismissed = true; _removeSdk(); }
    });
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
    var top = _stackTop(['claude-update-banner', SDK_BANNER_ID]);
    bar.style.cssText = [
      'position:fixed', 'top:' + top + 'px', 'left:0', 'right:0', 'z-index:9998',
      'background:#4b2a00', 'color:#ffd9a0', 'padding:8px 12px',
      'display:flex', 'align-items:center', 'gap:6px 10px', 'flex-wrap:wrap', 'font-size:13px',
      'box-shadow:0 2px 6px rgba(0,0,0,.3)',
    ].join(';');
    bar.title = armed ? msg : (data.outdated || []).join('\n');
    // At most two lines (2026-10-29): on a phone the waiting list wrapped to
    // eight and the bar covered the chat.  The full text is in the tooltip.
    bar.innerHTML = '<span style="flex:1 1 240px;min-width:0;overflow:hidden;display:-webkit-box;'
      + '-webkit-box-orient:vertical;-webkit-line-clamp:' + (armed ? 1 : 2) + '">' + _esc(msg) + '</span>'
      // One button row in both states (owner, 2026-10-29: "Now, When
      // sessions done, Cancel, Hide").  "When sessions done" shows as pressed
      // while the restart is queued; Cancel exists only then.  Hide keeps a
      // queued restart queued: the server still restarts once nothing is busy.
      + '<span style="display:flex;gap:6px;flex-shrink:0;margin-left:auto">'
      + '<button data-act="now" class="pm-btn pm-btn-secondary" style="padding:4px 10px" '
      +   'title="Restart the Session Engine now">Now</button>'
      + (armed
          ? '<button data-act="armed" class="pm-btn pm-btn-primary" aria-pressed="true" '
            + 'style="padding:4px 10px;cursor:default" title="Queued: restarts once no session is busy">'
            + 'When sessions done</button>'
            + '<button data-act="cancel" class="pm-btn pm-btn-secondary" style="padding:4px 10px">Cancel</button>'
          : '<button data-act="arm" class="pm-btn pm-btn-primary" style="padding:4px 10px" '
            + 'title="Restart once no session is busy">When sessions done</button>')
      + '<button data-act="later" class="pm-btn pm-btn-secondary" style="padding:4px 10px" '
      +   'aria-haspopup="menu" aria-expanded="false" '
      +   'title="' + (armed ? 'Hide it; the restart still happens once no session is busy' : 'Hide it for a while') + '">'
      +   'Hide<span aria-hidden="true" style="margin-left:5px;font-size:10px;opacity:.75;pointer-events:none">▾</span></button>'
      + '</span>';
    document.body.appendChild(bar);
    bar.addEventListener('click', function (e) {
      var hit = e.target && e.target.closest && e.target.closest('[data-act]');
      var act = hit && hit.getAttribute('data-act');
      if (!act) return;
      if (act === 'later') {
        if (document.getElementById(MENU_ID)) _closeMenu();
        else _openMenu(e.target.closest('[data-act]'));
        return;
      }
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

  // The last status read, for the homepage strip (toolbar.js _hpStatusStrip):
  // it shows one line while the engine runs older code.  No fetch of its own.
  function _publish(data) {
    window.EngineFreshness.last = data || null;
    if (typeof viewMode !== 'undefined' && viewMode === 'homepage'
        && typeof _updateHomepageStats === 'function') _updateHomepageStats();
  }

  function _check() {
    fetch('/api/engine/status')
      .then(function (r) { return r.ok ? r.json() : null; })
      .then(function (data) {
        _publish(data);
        if (!data || !data.ok || !data.running) {
          _remove();
          _removeSdk();
          _schedule(RECHECK_MS);
          return;
        }
        var sdkBad = Array.isArray(data.sdk_patch_failures) && data.sdk_patch_failures.length > 0;
        if (sdkBad && !_sdkDismissed) _renderSdk(data); else _removeSdk();
        if (!data.stale && !data.restart_armed) {
          _remove();
          _schedule(RECHECK_MS);
          return;
        }
        // A snooze hides the waiting bar too; the queued restart is server
        // side and still fires (app/engine_freshness.py).
        _lastStartedAt = data.started_at || 0;
        if (_isSnoozed(data)) {
          _remove();
          _schedule(RECHECK_MS);
          return;
        }
        // Leave an open Hide menu alone; the next check redraws.
        if (!document.getElementById(MENU_ID)) _render(data);
        _schedule(data.restart_armed ? ARMED_RECHECK_MS : RECHECK_MS);
      })
      .catch(function () { _schedule(RECHECK_MS); });
  }

  window.EngineFreshness = { check: _check, last: null, _hideUntil: _hideUntil };

  function _boot() { setTimeout(_check, BOOT_DELAY_MS); }
  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', _boot);
  } else {
    _boot();
  }
})();
