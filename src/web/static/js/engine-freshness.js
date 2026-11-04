/* engine-freshness.js — no restart banner; only the SDK-patch alert
 *
 * When the engine runs outdated code, the web server queues an idle restart
 * by itself (start_auto_restart in src/web/engine_freshness.py), and this file
 * only asks it to (POST /api/engine/restart-when-idle, once per engine start)
 * in case the web server predates that.  Do NOT bring back a banner, button
 * or hide menu for an outdated engine.
 *
 * Kept: the red "SDK patches did not apply" banner.  The engine
 * patches the Claude SDK's internals at start (src/engine/sdk_patches.py); a patch
 * that cannot apply means sessions run degraded until someone fixes the SDK
 * install, which a restart alone does not do.
 */
(function () {
  'use strict';
  if (window._engineFreshnessLoaded) return;
  window._engineFreshnessLoaded = true;

  var SDK_BANNER_ID = 'sdk-patch-banner';
  var _sdkDismissed = false;   // this page view only: the engine is still unpatched
  var BOOT_DELAY_MS = 4000;
  var RECHECK_MS = 10 * 60 * 1000;
  var _timer = null;
  var _armedFor = null;        // engine start time we already queued a restart for

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

  function _schedule(ms) {
    clearTimeout(_timer);
    _timer = setTimeout(_check, ms);
  }

  function _publish(data) {
    window.EngineFreshness.last = data || null;
  }

  function _check() {
    fetch('/api/engine/status')
      .then(function (r) { return r.ok ? r.json() : null; })
      .then(function (data) {
        _publish(data);
        _schedule(RECHECK_MS);
        if (!data || !data.ok || !data.running) { _removeSdk(); return; }
        var sdkBad = Array.isArray(data.sdk_patch_failures) && data.sdk_patch_failures.length > 0;
        if (sdkBad && !_sdkDismissed) _renderSdk(data); else _removeSdk();
        // Outdated code: queue the restart-once-idle, silently, once per start.
        if (data.stale && !data.restart_armed && _armedFor !== data.started_at) {
          _armedFor = data.started_at;
          fetch('/api/engine/restart-when-idle', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ arm: true }),
          }).catch(function () { _armedFor = null; });
        }
      })
      .catch(function () { _schedule(RECHECK_MS); });
  }

  window.EngineFreshness = { check: _check, last: null };

  function _boot() { setTimeout(_check, BOOT_DELAY_MS); }
  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', _boot);
  } else {
    _boot();
  }
})();
