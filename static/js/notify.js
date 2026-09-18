/* notify.js — General approval-notification capability for VibeNode.
 *
 * WHY THIS EXISTS
 * ---------------
 * When Claude asks for tool-use approval inside a session, the daemon emits a
 * `session_permission` Socket.IO event (see daemon/session_manager.py and the
 * matching handler in static/js/socket.js). Historically that event only
 * updated the sidebar row — silently. On mobile especially, the user had no
 * way to notice an approval was pending without proactively looking at the
 * tab. This module turns every such event into a multi-layered ping that
 * degrades gracefully across desktop, Android, and iOS Safari.
 *
 * This is a *general* capability, not specific to any one project. It fires
 * for every session — including CustomerNode sessions, which are ordinary
 * VibeNode-managed projects and therefore already emit `session_permission`
 * through the same code path without any changes on their side.
 *
 * WHAT WORKS WHERE (iOS Safari being the strict constraint)
 * ---------------------------------------------------------
 * We assume NO user opt-in and NO PWA install. Every iOS user just opens the
 * URL in Safari and expects it to work. Given that, the ceiling is:
 *
 *   Desktop (Chrome/Firefox/Safari):
 *     ✔ WebAudio chime (armed by any tap the user already made)
 *     ✔ Native Notification (opportunistic — we call requestPermission on the
 *        first user gesture we see; if the browser silently declines, we
 *        no-op. We never show a modal purely to ask.)
 *     ✔ Title flash while document.hidden
 *     ✔ Favicon dot while document.hidden
 *
 *   Android Chrome:
 *     ✔ WebAudio chime, ✔ Notification (as above), ✔ navigator.vibrate,
 *     ✔ Title flash, ✔ Favicon dot
 *
 *   iOS Safari (regular tab, no PWA install):
 *     ✔ WebAudio chime IF the audio context has been unlocked. Any tap on
 *        the page unlocks it — including the "Update App" button the user
 *        already presses. Once unlocked in the session it stays unlocked.
 *     ✔ Title flash + favicon dot (visible when they return to the tab)
 *     ✖ navigator.vibrate — not implemented in iOS Safari. We call it
 *        defensively; it's a silent no-op.
 *     ✖ Notification API — iOS Safari only exposes Notifications inside
 *        installed PWAs. Regular tabs get a permission that resolves
 *        "default" and never fires. Our request is harmless in that state.
 *     ✖ Background pings while the tab is suspended / phone is locked —
 *        genuinely impossible without a Service Worker + Web Push + PWA
 *        install. Out of scope by user constraint.
 *
 * PUBLIC API
 * ----------
 *   window.VNNotify.arm()
 *     Call from any real user gesture (click / touchstart handler). Unlocks
 *     the WebAudio context and opportunistically requests Notification
 *     permission. Idempotent and safe to call on every gesture — we track
 *     whether we've already armed. The primary caller wires this into
 *     openGitUpdate/openGitPublish/openGitSyncBoth (git-sync.js) so an iOS
 *     user pressing the Update button on their phone arms the ping
 *     automatically. We also attach passive listeners for the first tap
 *     anywhere on the document as a fallback.
 *
 *   window.VNNotify.approvalNeeded({sessionId, sessionName, toolName})
 *     Called by the session_permission Socket.IO handler in socket.js. Fires
 *     the ping layers. Deduplicated per-session for 3s so a rapid
 *     re-emission of the same event doesn't chain into a strobe.
 *
 *   window.VNNotify.setEnabled(bool)      persist an on/off toggle
 *   window.VNNotify.isEnabled()           read it
 *
 * The enabled flag lives in localStorage under `vn_notify_approvals`
 * (default: on). No UI toggle yet — call VNNotify.setEnabled(false) from
 * the console to silence, or add a Settings row later.
 */

(function () {
  'use strict';

  // ── State ─────────────────────────────────────────────────────────────
  var STORAGE_KEY = 'vn_notify_approvals';   // '0' = off, anything else = on
  var DEDUP_MS = 3000;                        // suppress repeats per session
  var TITLE_FLASH_INTERVAL_MS = 1000;         // 1s toggle cadence
  var FAVICON_ID = 'favicon-icon-link';       // we tag the <link rel=icon>

  var _audioCtx = null;                       // lazily created + unlocked
  var _armed = false;                         // audio unlock attempted
  var _notifPermRequested = false;            // requestPermission called once
  var _lastFiredAt = {};                      // sessionId -> ms
  var _originalTitle = null;                  // for title-flash restore
  var _titleFlashTimer = null;
  var _originalFaviconHref = null;            // for favicon-dot restore
  var _badgedFaviconHref = null;              // cached red-dot data URL

  // ── Utilities ─────────────────────────────────────────────────────────

  function isEnabled() {
    try { return localStorage.getItem(STORAGE_KEY) !== '0'; }
    catch (e) { return true; }
  }

  function setEnabled(on) {
    try { localStorage.setItem(STORAGE_KEY, on ? '1' : '0'); }
    catch (e) {}
  }

  function log() {
    try { console.log.apply(console, ['[VNNotify]'].concat([].slice.call(arguments))); }
    catch (e) {}
  }

  // ── Audio: WebAudio synth chime (no asset file) ───────────────────────
  //
  // Two overlapping sine tones with a fast decay envelope — a clear, short,
  // pleasant "ping" (~350ms total). Synthesized so there's no static asset
  // to fetch or cache. iOS Safari requires the AudioContext to be created
  // (or resumed) inside a user gesture, so arm() does both.

  function _ensureCtx() {
    if (_audioCtx) return _audioCtx;
    try {
      var Ctor = window.AudioContext || window.webkitAudioContext;
      if (!Ctor) return null;
      _audioCtx = new Ctor();
    } catch (e) { _audioCtx = null; }
    return _audioCtx;
  }

  function _playChime() {
    var ctx = _ensureCtx();
    if (!ctx) return;
    // If still suspended (context created before gesture), best-effort resume.
    // On iOS Safari this only succeeds if we're inside a gesture, but the
    // event handler chain from approvalNeeded may or may not be — the
    // arm() path is what makes this reliable.
    if (ctx.state === 'suspended') {
      try { ctx.resume(); } catch (e) {}
    }
    var now = ctx.currentTime;
    // Two tones for a slightly richer "ding-dong" feel.
    _tone(ctx, 880, now, 0.18, 0.22);           // A5
    _tone(ctx, 1320, now + 0.12, 0.18, 0.22);   // E6
  }

  function _tone(ctx, freq, startAt, dur, peakGain) {
    var osc = ctx.createOscillator();
    var gain = ctx.createGain();
    osc.type = 'sine';
    osc.frequency.setValueAtTime(freq, startAt);
    // Fast attack, exponential decay — avoids the harsh "click" from a
    // rectangular envelope. setValueAtTime(0) + linearRamp emulates a
    // 5ms attack; exponentialRamp gives a musical decay.
    gain.gain.setValueAtTime(0, startAt);
    gain.gain.linearRampToValueAtTime(peakGain, startAt + 0.005);
    gain.gain.exponentialRampToValueAtTime(0.0001, startAt + dur);
    osc.connect(gain).connect(ctx.destination);
    osc.start(startAt);
    osc.stop(startAt + dur + 0.02);
  }

  // ── Vibration (Android; no-op on iOS Safari) ──────────────────────────

  function _vibrate() {
    try {
      if (navigator && typeof navigator.vibrate === 'function') {
        // Short double-buzz pattern: 180ms on, 90ms off, 180ms on.
        navigator.vibrate([180, 90, 180]);
      }
    } catch (e) {}
  }

  // ── Notification API (opportunistic) ──────────────────────────────────

  function _tryRequestNotifPerm() {
    if (_notifPermRequested) return;
    _notifPermRequested = true;
    try {
      if (typeof Notification === 'undefined') return;
      if (Notification.permission === 'default') {
        // Called from a user gesture (arm() is invoked from click handlers).
        // The Promise form is safe in all modern browsers; older Safari
        // supports the callback form — try both defensively.
        try {
          var p = Notification.requestPermission();
          if (p && typeof p.then === 'function') {
            p.then(function (r) { log('Notification permission:', r); });
          }
        } catch (e) {
          Notification.requestPermission(function (r) { log('Notification permission:', r); });
        }
      }
    } catch (e) {}
  }

  function _showNotification(title, body) {
    try {
      if (typeof Notification === 'undefined') return;
      if (Notification.permission !== 'granted') return;
      // Only surface OS notifications while the tab is backgrounded — if
      // the user is looking at VibeNode, the in-page chime is enough and
      // an OS banner is noise.
      if (!document.hidden) return;
      var n = new Notification(title, {
        body: body,
        icon: '/static/vibenode.png',
        badge: '/static/vibenode.png',
        tag: 'vn-approval',            // coalesce a burst into one banner
        renotify: true,
      });
      // Clicking the notification should focus VibeNode.
      n.onclick = function () {
        try { window.focus(); } catch (e) {}
        try { n.close(); } catch (e) {}
      };
    } catch (e) {}
  }

  // ── Title flash while tab is backgrounded ─────────────────────────────
  //
  // On mobile this is the ONLY reliable signal a backgrounded tab can leave
  // for the user, because iOS Safari suspends timers aggressively — but
  // the tab title itself is drawn by the OS and remains visible in the
  // tab switcher. When the user returns to the tab we restore the
  // original title.

  function _startTitleFlash(msg) {
    if (_titleFlashTimer) return;
    if (_originalTitle === null) _originalTitle = document.title;
    var flip = false;
    _titleFlashTimer = setInterval(function () {
      flip = !flip;
      document.title = flip ? ('⚠ ' + msg) : _originalTitle;
    }, TITLE_FLASH_INTERVAL_MS);
  }

  function _stopTitleFlash() {
    if (_titleFlashTimer) {
      clearInterval(_titleFlashTimer);
      _titleFlashTimer = null;
    }
    if (_originalTitle !== null) {
      document.title = _originalTitle;
    }
  }

  // ── Favicon dot while tab is backgrounded ─────────────────────────────
  //
  // We draw the existing favicon into a canvas and overlay a red dot in the
  // top-right corner. This is a subtle but universally visible signal that
  // survives even when the tab is inactive. Restored when the tab regains
  // focus (visibilitychange handler below).

  function _ensureFaviconLink() {
    var link = document.getElementById(FAVICON_ID);
    if (link) return link;
    // Find the existing <link rel="icon"> and tag it so we can find it later.
    link = document.querySelector('link[rel~="icon"]');
    if (!link) {
      link = document.createElement('link');
      link.rel = 'icon';
      document.head.appendChild(link);
    }
    link.id = FAVICON_ID;
    return link;
  }

  function _startFaviconDot() {
    var link = _ensureFaviconLink();
    if (_originalFaviconHref === null) _originalFaviconHref = link.href || '';
    if (_badgedFaviconHref) {
      link.href = _badgedFaviconHref;
      return;
    }
    // Build the badged favicon lazily. Some hosts serve the favicon
    // cross-origin (unlikely here — it's under /static/), which would taint
    // the canvas and block toDataURL. If that happens we swap in a
    // solid-color data URL instead.
    var img = new Image();
    img.crossOrigin = 'anonymous';
    img.onload = function () {
      try {
        var size = 32;
        var canvas = document.createElement('canvas');
        canvas.width = size; canvas.height = size;
        var ctx = canvas.getContext('2d');
        ctx.drawImage(img, 0, 0, size, size);
        // Red dot, top-right, with a subtle white stroke for contrast on
        // dark favicons.
        ctx.beginPath();
        ctx.arc(size - 8, 8, 7, 0, Math.PI * 2);
        ctx.fillStyle = '#ff3b3b';
        ctx.fill();
        ctx.lineWidth = 2;
        ctx.strokeStyle = 'rgba(255,255,255,0.95)';
        ctx.stroke();
        _badgedFaviconHref = canvas.toDataURL('image/png');
        link.href = _badgedFaviconHref;
      } catch (e) {
        // Canvas tainted or drawImage failed — fall back to a plain red
        // dot on transparent so the user still gets a visible signal.
        _badgedFaviconHref = _plainRedDotDataUrl();
        link.href = _badgedFaviconHref;
      }
    };
    img.onerror = function () {
      _badgedFaviconHref = _plainRedDotDataUrl();
      link.href = _badgedFaviconHref;
    };
    img.src = _originalFaviconHref || '/static/images/logo.png';
  }

  function _plainRedDotDataUrl() {
    var canvas = document.createElement('canvas');
    canvas.width = 32; canvas.height = 32;
    var ctx = canvas.getContext('2d');
    ctx.beginPath();
    ctx.arc(16, 16, 12, 0, Math.PI * 2);
    ctx.fillStyle = '#ff3b3b';
    ctx.fill();
    return canvas.toDataURL('image/png');
  }

  function _stopFaviconDot() {
    if (_originalFaviconHref === null) return;
    var link = _ensureFaviconLink();
    link.href = _originalFaviconHref;
  }

  // ── Arm: unlock audio + request notif permission ──────────────────────

  function arm() {
    if (_armed) {
      // Already armed once, but still gently try a resume in case the OS
      // suspended the context. This is cheap and idempotent.
      var ctx = _audioCtx;
      if (ctx && ctx.state === 'suspended') {
        try { ctx.resume(); } catch (e) {}
      }
      return;
    }
    _armed = true;

    var ctx = _ensureCtx();
    if (ctx) {
      try { ctx.resume(); } catch (e) {}
      // Play a silent, essentially-zero-gain tick to fully unlock the
      // pipeline on iOS Safari. Without this some iOS versions leave the
      // context "running" but silently drop the first real playback.
      try {
        var osc = ctx.createOscillator();
        var g = ctx.createGain();
        g.gain.value = 0.0001;
        osc.frequency.value = 440;
        osc.connect(g).connect(ctx.destination);
        osc.start();
        osc.stop(ctx.currentTime + 0.02);
      } catch (e) {}
    }

    _tryRequestNotifPerm();
    log('armed');
  }

  // ── Attach fallback arming to the first tap anywhere on the page ──────
  //
  // The Update/Publish/Sync buttons call arm() explicitly (see git-sync.js),
  // but we also want arming to happen the very first time a user touches
  // ANY interactive control — otherwise a user who never presses Update
  // but taps around would never get audio. Passive + once so this is free.

  function _installGestureFallback() {
    var opts = { once: true, passive: true, capture: true };
    var handler = function () { try { arm(); } catch (e) {} };
    ['pointerdown', 'touchstart', 'mousedown', 'keydown'].forEach(function (ev) {
      try { document.addEventListener(ev, handler, opts); } catch (e) {}
    });
  }

  // ── Public trigger ────────────────────────────────────────────────────

  function approvalNeeded(info) {
    if (!isEnabled()) return;
    info = info || {};
    var sid = info.sessionId || info.session_id || '';
    var name = info.sessionName || 'Claude session';
    var tool = info.toolName || info.tool_name || 'a tool';

    // Dedup: don't strobe if the same session's approval event repeats
    // within DEDUP_MS (some backends can re-emit on reconnect).
    var now = Date.now();
    var last = _lastFiredAt[sid] || 0;
    if (sid && (now - last) < DEDUP_MS) return;
    _lastFiredAt[sid] = now;

    // Audio always attempts to play — if the ctx is locked (no prior
    // gesture on iOS), it will silently no-op and the other layers still
    // fire.
    try { _playChime(); } catch (e) {}
    try { _vibrate(); } catch (e) {}

    var title = 'Approval needed';
    var body = name + ' needs approval for ' + tool;

    if (document.hidden) {
      _showNotification(title, body);
      _startTitleFlash('Approval needed');
      _startFaviconDot();
    } else {
      // Foreground: don't spam OS notification. But if the same request
      // stays unanswered and the user backgrounds the tab, the
      // visibilitychange handler below promotes it.
      _pendingWhileForeground = { title: title, body: body };
    }
  }

  var _pendingWhileForeground = null;

  // If the user backgrounds the tab while an approval is still visibly
  // pending (chime already played, but they didn't act), start the flash
  // so returning to the tab they see it. Cleared once they focus back.
  function _onVisibilityChange() {
    if (document.hidden) {
      if (_pendingWhileForeground) {
        _startTitleFlash(_pendingWhileForeground.title);
        _startFaviconDot();
        _showNotification(_pendingWhileForeground.title, _pendingWhileForeground.body);
      }
    } else {
      _stopTitleFlash();
      _stopFaviconDot();
      _pendingWhileForeground = null;
    }
  }

  // Called by socket.js whenever an approval is resolved (permission
  // acknowledged) so we don't keep flashing after the user has already
  // dealt with it in another tab / on another device.
  function clearPending(sessionId) {
    if (sessionId) delete _lastFiredAt[sessionId];
    _pendingWhileForeground = null;
    _stopTitleFlash();
    _stopFaviconDot();
  }

  // ── Boot ──────────────────────────────────────────────────────────────

  function _boot() {
    _installGestureFallback();
    document.addEventListener('visibilitychange', _onVisibilityChange);
    // Cache the original title now so a later flash restores it correctly
    // even if some other module has since changed document.title.
    _originalTitle = document.title;
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', _boot);
  } else {
    _boot();
  }

  // Export public surface.
  window.VNNotify = {
    arm: arm,
    approvalNeeded: approvalNeeded,
    clearPending: clearPending,
    setEnabled: setEnabled,
    isEnabled: isEnabled,
  };
})();
