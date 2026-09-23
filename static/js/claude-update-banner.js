/* claude-update-banner.js — startup CLI-freshness banner
 *
 * Polls /api/admin/claude-status on load. Shows a top-of-window banner
 * when the CLI is stale (no successful update check in ≥ 30 days) or
 * when a previous update completed but the daemon still runs the old
 * binary. One click runs `claude update` via the admin endpoint.
 *
 * Silent when everything is current. Zero-config: no user setup needed
 * to get "your CLI is out of date" surfaced before they hit the model
 * switch that would otherwise fail with claude_code_version_too_old.
 *
 * Depends on: showConfirm/showToast in utils.js.
 */

(function () {
  'use strict';

  // Guard against double-load. index.html loads scripts in a fixed order,
  // but a hot-reload or a mobile back-forward cache can hit this twice.
  if (window._claudeUpdateBannerLoaded) return;
  window._claudeUpdateBannerLoaded = true;

  var BANNER_ID = 'claude-update-banner';
  var DISMISSED_KEY = 'claudeUpdateBannerDismissedUntil';

  // Give the app 3s to finish first paint before we check — the banner
  // is not urgent and we do not want to compete with critical boot XHRs.
  var BOOT_DELAY_MS = 3000;

  function _dismissedUntil() {
    try {
      var v = parseInt(localStorage.getItem(DISMISSED_KEY) || '0', 10);
      return isFinite(v) ? v : 0;
    } catch (e) { return 0; }
  }

  function _dismiss(hours) {
    try {
      var until = Date.now() + hours * 3600 * 1000;
      localStorage.setItem(DISMISSED_KEY, String(until));
    } catch (e) { /* localStorage unavailable — dismiss for this pageview only */ }
    var el = document.getElementById(BANNER_ID);
    if (el && el.parentNode) el.parentNode.removeChild(el);
  }

  function _renderBanner(status) {
    if (document.getElementById(BANNER_ID)) return;
    var stale = !!status.stale;
    var restartPending = !!status.restart_pending;

    var msg;
    if (restartPending) {
      // Update already downloaded; daemon still on the old binary. This is
      // the case the daily worker flags after it updates and can't restart.
      msg = 'Claude Code was updated to ' + (status.current_version || 'a new version')
          + '. Restart the session daemon to apply it.';
    } else {
      // Straight-up stale. Frame it around the concrete failure mode so
      // the user knows why they should care.
      msg = 'Your Claude Code CLI hasn’t been updated in a while. New models '
          + 'released by Anthropic will refuse to run until it’s updated.';
    }

    var bar = document.createElement('div');
    bar.id = BANNER_ID;
    bar.setAttribute('role', 'status');
    bar.style.cssText = [
      'position:fixed', 'top:0', 'left:0', 'right:0', 'z-index:9998',
      'background:#4b3d00', 'color:#ffe89a', 'padding:8px 12px',
      'display:flex', 'align-items:center', 'gap:12px', 'font-size:13px',
      'box-shadow:0 2px 6px rgba(0,0,0,.3)',
    ].join(';');
    bar.innerHTML =
      '<span style="flex:1">' + _esc(msg) + '</span>' +
      '<button id="cub-update" class="pm-btn pm-btn-primary" style="padding:4px 12px">Update Now</button>' +
      '<button id="cub-later" class="pm-btn pm-btn-secondary" style="padding:4px 12px">Later</button>';

    document.body.appendChild(bar);

    document.getElementById('cub-update').onclick = function () {
      _runUpdate(restartPending);
    };
    document.getElementById('cub-later').onclick = function () {
      // 24-hour snooze. Long enough not to nag, short enough that a truly
      // stale CLI can’t sit quietly for a week.
      _dismiss(24);
    };
  }

  function _esc(s) {
    if (typeof escHtml === 'function') return escHtml(s);
    return String(s || '').replace(/[&<>"]/g, function (c) {
      return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c];
    });
  }

  function _runUpdate(force) {
    var confirmFn = (typeof showConfirm === 'function') ? showConfirm : null;
    var proceed = confirmFn
      ? confirmFn('Update Claude Code',
          '<p>Run <code>claude update</code> now?</p>' +
          '<p style="opacity:.75;font-size:12px;margin-top:8px">' +
          'Any live Claude sessions on this machine will be interrupted. ' +
          'They can be resumed afterward.</p>',
          { confirmText: 'Update Now', cancelText: 'Cancel' })
      : Promise.resolve(true);

    proceed.then(function (ok) {
      if (!ok) return;
      if (typeof showToast === 'function') {
        showToast('Updating Claude Code — this can take up to a minute…');
      }
      fetch('/api/admin/claude-update', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ force: !!force }),
      })
        .then(function (r) { return r.json().then(function (d) { return { ok: r.ok, data: d || {} }; }); })
        .then(function (res) {
          var d = res.data;
          if (!res.ok || !d.ok) {
            var msg = d.error ? ('Update failed: ' + d.error)
                              : 'Update failed — run `claude update` from a terminal.';
            if (typeof showToast === 'function') showToast(msg, true);
            return;
          }
          if (d.updated) {
            if (typeof showToast === 'function') {
              showToast('Claude CLI updated: ' + (d.before || '?') + ' → ' + (d.after || '?'));
            }
          } else {
            if (typeof showToast === 'function') {
              showToast('Claude CLI already at ' + (d.after || '?'));
            }
          }
          // Clear the banner regardless — the daily-check state was refreshed.
          _dismiss(0);
        })
        .catch(function (e) {
          if (typeof showToast === 'function') {
            showToast('Update request failed: ' + (e && e.message || e), true);
          }
        });
    });
  }

  function _check() {
    if (Date.now() < _dismissedUntil()) return;
    fetch('/api/admin/claude-status')
      .then(function (r) { return r.ok ? r.json() : null; })
      .then(function (data) {
        if (!data || !data.ok) return;
        // installed=false means `claude` isn’t on PATH — banner is
        // pointless in that case, the model UI has bigger problems to
        // report. Skip silently.
        if (!data.installed) return;
        if (!data.stale && !data.restart_pending) return;
        _renderBanner(data);
      })
      .catch(function () { /* offline — banner is best-effort */ });
  }

  function _boot() { setTimeout(_check, BOOT_DELAY_MS); }
  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', _boot);
  } else {
    _boot();
  }
})();
