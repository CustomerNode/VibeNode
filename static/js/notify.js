/* notify.js — the ways VibeNode asks for attention when you are not looking.
 *
 * WHY THIS EXISTS
 * ---------------
 * Some things need the user while the VibeNode tab is in the background or the
 * phone is in a pocket: an approval page a release script is waiting on, a
 * session the watchdog found stalled. This module turns those into a chime,
 * a vibration, an OS notification, a flashing tab title and a red dot on the
 * favicon, degrading gracefully across desktop, Android and iOS Safari.
 *
 * It is general: it knows nothing about any one project. CustomerNode's
 * release pipeline is simply the first script to call the endpoint behind it.
 *
 * THE RULE FOR ANYTHING THAT STAYS ON (title flash, favicon dot, OS banner)
 * ------------------------------------------------------------------------
 * A chime ends by itself. A flashing title does not, so it is never started
 * by an event alone: it runs only while a REASON is still true, and every
 * tick re-checks the reason before it touches the title. The favicon dot
 * and a reason's OS banner are up only while the title is flashing for it,
 * so the same tick takes them down.
 *
 *   approval   The server knows whether an approval page is still waiting
 *              (app/approval_watch.py) and says so every few seconds in an
 *              `approvals_open` snapshot. Each snapshot is a lease: the
 *              approval counts as open here only until the lease runs out.
 *              So the flash stops when a snapshot arrives without it (it was
 *              answered) AND when snapshots stop arriving (socket dead, web
 *              server restarted). Silence never means "still open".
 *   attention  A session needs the user (`session_stalled`). Nothing can
 *              answer that from outside, so it lasts until this tab is
 *              looked at.
 *
 * Before 2026-10-12 an approval ping started the flash and only looking at
 * that one tab stopped it. It kept flashing after the approval whenever the
 * user stayed on the approval page, answered on another device or had a
 * second tab open; a ping that arrived while the tab was on screen was kept
 * forever and started the flash hours later; and "release is live" pings,
 * which have nothing to approve, flashed "Approval needed" as well.
 * Tests: tests/test_notify_flash.py.
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
 *   window.VNNotify.notice({key, title, body, banner})
 *     One ping: chime, vibration and (unless `banner` is false, and only
 *     while the tab is hidden) an OS notification. Never flashes anything.
 *     Called by the `external_notice` handler in socket.js. Deduplicated per
 *     `key` for 3s so a re-emitted event does not strobe.
 *
 *   window.VNNotify.syncApprovals(open, ttlMs)
 *     The server's current list of open approval pages (`approvals_open` in
 *     socket.js). Anything listed is open for `ttlMs`; anything not listed
 *     is over, now.
 *
 *   window.VNNotify.attention({sessionId, sessionName, title, body})
 *     A session needs the user: the ping layers plus a flash that lasts
 *     until the tab is looked at. Currently the watchdog's `session_stalled`
 *     escalation.
 *
 *   window.VNNotify.state()               what it is flashing about, and why
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
  var DEDUP_MS = 3000;                        // suppress repeats per key
  var TITLE_FLASH_INTERVAL_MS = 1000;         // 1s toggle cadence
  var FAVICON_ID = 'favicon-icon-link';       // we tag the <link rel=icon>
  var DEFAULT_LEASE_MS = 10000;               // a snapshot that names no ttl
  var MAX_LEASE_MS = 30000;                   // no snapshot is believed for longer
  var MAX_REMEMBERED = 50;                    // approvals kept, ended ones included, for their flags

  var _audioCtx = null;                       // lazily created + unlocked
  var _armed = false;                         // audio unlock attempted
  var _notifPermRequested = false;            // requestPermission called once
  var _lastFiredAt = {};                      // key -> ms
  var _originalFaviconHref = null;            // for favicon-dot restore
  var _badgedFaviconHref = null;              // cached red-dot data URL
  var _badgeLoading = false;                  // the badge image is being built
  var _dotWanted = false;                     // the dot should be showing

  // Why this tab may be asking for attention. See THE RULE in the header.
  //   id  -> {title, body, until, seenAt, acked, notified, note}
  //   key -> {title, body, notified, note}
  var _approvals = {};
  var _attention = {};

  var _flashTimer = null;
  var _warning = null;                        // the warning in the title right now, or null
  var _pageTitle = '';                        // the page's own title, put back after a warning

  // ── Utilities ─────────────────────────────────────────────────────────

  function isEnabled() {
    try { return localStorage.getItem(STORAGE_KEY) !== '0'; }
    catch (e) { return true; }
  }

  function setEnabled(on) {
    try { localStorage.setItem(STORAGE_KEY, on ? '1' : '0'); }
    catch (e) {}
    _reconcile();
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
    // event handler chain behind a ping may or may not be — the
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

  // Returns the Notification, or null when none was shown.
  function _showNotification(title, body, tag) {
    try {
      if (typeof Notification === 'undefined') return null;
      if (Notification.permission !== 'granted') return null;
      // Only surface OS notifications while the tab is backgrounded — if
      // the user is looking at VibeNode, the in-page chime is enough and
      // an OS banner is noise.
      if (!document.hidden) return null;
      var n = new Notification(title, {
        body: body,
        icon: '/static/vibenode.png',
        badge: '/static/vibenode.png',
        tag: tag,                      // coalesce a burst into one banner
        renotify: true,
      });
      // Clicking the notification should focus VibeNode.
      n.onclick = function () {
        try { window.focus(); } catch (e) {}
        try { n.close(); } catch (e) {}
      };
      return n;
    } catch (e) { return null; }
  }

  // ── What may flash, and for how long ──────────────────────────────────

  // A lease has to run out on time whatever happens to the clocks. The wall
  // clock can be set back, which would stretch it; the monotonic clock can
  // stand still while the machine sleeps, which would too. So a lease is
  // held against both, and whichever says it is over ends it.
  function _mono() {
    try { return performance.now(); } catch (e) { return Date.now(); }
  }

  function _lease(ms) {
    return { wall: Date.now() + ms, mono: _mono() + ms };
  }

  function _live(a) {
    return !!a.until && Date.now() < a.until.wall && _mono() < a.until.mono;
  }

  // THE decision: what this tab's title should be warning about right now,
  // or null for nothing. Every start and every tick asks it, so no path can
  // flash for a reason that is over.
  function _flashLabel() {
    if (!document.hidden || !isEnabled()) return null;
    var label = null, id, key;
    for (key in _attention) label = _attention[key].title;
    for (id in _approvals) {
      if (!_approvals[id].acked && _live(_approvals[id])) label = 'Approval needed';
    }
    return label;
  }

  function _closeBanner(reason) {
    if (!reason.note) return;
    try { reason.note.close(); } catch (e) {}
    reason.note = null;
  }

  // One OS banner per reason: shown once, when the tab is (or becomes)
  // hidden while the reason holds, and taken down as soon as the title would
  // no longer flash for it. A banner must never be the thing left behind: a
  // tab that is not flashing runs no tick, and so would never notice that
  // the reason behind a banner had ended.
  function _syncBanners() {
    var flashing = document.hidden && isEnabled(), id, key, r;
    for (id in _approvals) {
      r = _approvals[id];
      if (!flashing || r.acked || !_live(r)) { _closeBanner(r); continue; }
      if (r.notified) continue;
      r.notified = true;
      r.note = _showNotification('Approval needed', r.body, 'vn-approval-' + id);
    }
    for (key in _attention) {
      r = _attention[key];
      if (!flashing) { _closeBanner(r); continue; }
      if (r.notified) continue;
      r.notified = true;
      r.note = _showNotification(r.title, r.body, 'vn-' + key);
    }
  }

  // Bring the title flash, the favicon dot and the banners in line with the
  // reasons. Called after anything that can change them. An error in here
  // turns everything off; it never leaves anything on.
  function _reconcile() {
    var label = null;
    try {
      _syncBanners();
      label = _flashLabel();
    } catch (e) { _everythingOff(); return; }
    if (label === null) { _stopFlash(); return; }
    if (_flashTimer) return;
    _flashTimer = setInterval(_flashTick, TITLE_FLASH_INTERVAL_MS);
    _startFaviconDot();
  }

  // ── Title flash while tab is backgrounded ─────────────────────────────
  //
  // On mobile this is the ONLY reliable signal a backgrounded tab can leave
  // for the user, because iOS Safari suspends timers aggressively — but
  // the tab title itself is drawn by the OS and remains visible in the
  // tab switcher.
  //
  // The reason is checked again on every tick, in the same turn as the
  // write. A browser may run this timer once a minute (Chrome does, for a
  // tab hidden five minutes), so a check made when the flash started says
  // nothing about now.

  function _flashTick() {
    var label = null;
    try {
      _syncBanners();
      label = _flashLabel();
    } catch (e) { _everythingOff(); return; }
    if (label === null) { _stopFlash(); return; }
    if (_warning === null) _showWarning('⚠ ' + label);
    else _restoreTitle();
  }

  // The only two places that write document.title.
  function _showWarning(text) {
    _pageTitle = document.title;      // no warning is up, so this is the page's own
    document.title = text;
    // Read it back: the browser trims and collapses whitespace in a title,
    // and _restoreTitle recognises our warning by comparing against this.
    _warning = document.title;
  }

  function _restoreTitle() {
    if (_warning === null) return;
    // Put the page's title back only if ours is still there: another module
    // may have set a new one while the warning was up, and that one stays.
    try { if (document.title === _warning) document.title = _pageTitle; } catch (e) {}
    _warning = null;
  }

  function _stopFlash() {
    if (_flashTimer) {
      clearInterval(_flashTimer);
      _flashTimer = null;
    }
    _restoreTitle();
    _stopFaviconDot();
  }

  // No warning in the title, no dot, no banner, whatever the reasons say.
  // For a page that is about to stop running, and for an error.
  function _everythingOff() {
    var id, key;
    for (id in _approvals) _closeBanner(_approvals[id]);
    for (key in _attention) _closeBanner(_attention[key]);
    _stopFlash();
  }

  // ── Favicon dot while tab is backgrounded ─────────────────────────────
  //
  // We draw the existing favicon into a canvas and overlay a red dot in the
  // top-right corner. This is a subtle but universally visible signal that
  // survives even when the tab is inactive. It starts and stops with the
  // title flash.
  //
  // The badge is built from an image that loads later. `_dotWanted` is what
  // that late load consults: without it, a flash that had already stopped
  // got its red dot painted afterwards, and nothing took it off again.

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
    _dotWanted = true;
    var link = _ensureFaviconLink();
    if (_originalFaviconHref === null) _originalFaviconHref = link.href || '';
    if (_badgedFaviconHref) {
      link.href = _badgedFaviconHref;
      return;
    }
    if (_badgeLoading) return;
    _badgeLoading = true;
    // Build the badged favicon lazily. Some hosts serve the favicon
    // cross-origin (unlikely here — it's under /static/), which would taint
    // the canvas and block toDataURL. If that happens we swap in a
    // solid-color data URL instead.
    var done = function (href) {
      _badgedFaviconHref = href;
      _badgeLoading = false;
      if (_dotWanted) _ensureFaviconLink().href = href;
    };
    var img = new Image();
    img.crossOrigin = 'anonymous';
    img.onload = function () {
      var href;
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
        href = canvas.toDataURL('image/png');
      } catch (e) {
        // Canvas tainted or drawImage failed — fall back to a plain red
        // dot on transparent so the user still gets a visible signal.
        href = _plainRedDotDataUrl();
      }
      done(href);
    };
    img.onerror = function () { done(_plainRedDotDataUrl()); };
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
    if (!_dotWanted) return;          // nothing to take off; leave the icon alone
    _dotWanted = false;
    if (_originalFaviconHref === null) return;
    try { _ensureFaviconLink().href = _originalFaviconHref; } catch (e) {}
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

  // ── Public triggers ───────────────────────────────────────────────────

  // The sound layers. False when this key pinged less than DEDUP_MS ago
  // (some backends can re-emit on reconnect) or pings are switched off.
  function _chime(key) {
    if (!isEnabled()) return false;
    var now = Date.now();
    var last = _lastFiredAt[key] || 0;
    if (key && (now - last) < DEDUP_MS) return false;
    _lastFiredAt[key] = now;
    // Audio always attempts to play — if the ctx is locked (no prior
    // gesture on iOS), it will silently no-op and the other layers still
    // fire.
    try { _playChime(); } catch (e) {}
    try { _vibrate(); } catch (e) {}
    return true;
  }

  // One ping, nothing that stays on. See the header.
  function notice(info) {
    info = info || {};
    if (!_chime(info.key || '')) return;
    if (info.banner !== false) {
      _showNotification(info.title || 'VibeNode', info.body || '', 'vn-notice');
    }
  }

  // The server's list of approval pages that are still waiting. Listed: open
  // for `ttlMs` from now. Not listed: over, now. A record outlives its
  // approval so one that comes back (the server lost sight of its page for
  // a moment) keeps what the user already did about it.
  function syncApprovals(open, ttlMs) {
    var ttl = (typeof ttlMs === 'number' && ttlMs > 0) ? Math.min(ttlMs, MAX_LEASE_MS) : DEFAULT_LEASE_MS;
    var now = Date.now(), listed = {}, i, id, item, a;
    open = Array.isArray(open) ? open : [];
    for (i = 0; i < open.length; i++) {
      item = open[i] || {};
      if (!item.id) continue;
      id = String(item.id);
      listed[id] = true;
      a = _approvals[id];
      if (!a) a = _approvals[id] = { until: null, acked: false, notified: false, note: null };
      a.title = String(item.title || 'Approval');
      a.body = a.title + (item.message ? ' — ' + item.message : '');
      a.until = _lease(ttl);
      a.seenAt = now;
    }
    for (id in _approvals) {
      if (!listed[id]) _approvals[id].until = null;        // answered: over, now
    }
    // Forget the ones heard about longest ago. Whatever is listed now was
    // heard about just now, so these are over (or a snapshot far longer than
    // any the server sends is being cut down to size).
    var ids = Object.keys(_approvals);
    if (ids.length > MAX_REMEMBERED) {
      ids.sort(function (x, y) { return _approvals[x].seenAt - _approvals[y].seenAt; });
      for (i = 0; i < ids.length - MAX_REMEMBERED; i++) {
        _closeBanner(_approvals[ids[i]]);
        delete _approvals[ids[i]];
      }
    }
    _reconcile();
  }

  // A session needs the user for something nothing outside this tab can
  // answer — e.g. the daemon watchdog found it stalled on background work
  // that is not running, and a nudge did not fix it (`session_stalled` in
  // socket.js, daemon/health_monitor.py job 5). Keyed separately from
  // notices so one never swallows the other inside the dedup window.
  function attention(info) {
    info = info || {};
    var sid = info.sessionId || info.session_id || '';
    var name = info.sessionName || 'A session';
    var key = 'attention:' + sid;
    if (!_chime(key)) return;
    if (_attention[key]) _closeBanner(_attention[key]);   // this event's banner replaces the last one's
    _attention[key] = {
      title: info.title || 'Session needs you',
      body: info.body || (name + ' needs you'),
      notified: false,
      note: null,
    };
    _reconcile();
  }

  // What it is asking attention for right now. For the console and the tests.
  function state() {
    var open = [], id, a;
    for (id in _approvals) {
      a = _approvals[id];
      if (_live(a)) {
        open.push({ id: id, acked: a.acked, msLeft: Math.min(a.until.wall - Date.now(), a.until.mono - _mono()) });
      }
    }
    return {
      flashing: !!_flashTimer, warning: _warning, dot: _dotWanted,
      approvals: open, attention: Object.keys(_attention),
      remembered: Object.keys(_approvals).length,
    };
  }

  // ── Page lifecycle ────────────────────────────────────────────────────

  // Looking at the tab is the answer to everything it was flashing about. A
  // reason that arrives while the tab is on screen is not seen yet: it
  // starts flashing if the tab is put in the background while it still
  // holds, and only then.
  function _onVisibilityChange() {
    if (!document.hidden) {
      var id, key;
      for (id in _approvals) _approvals[id].acked = true;
      for (key in _attention) _closeBanner(_attention[key]);
      _attention = {};
    }
    _reconcile();
  }


  // ── Boot ──────────────────────────────────────────────────────────────

  function _boot() {
    _installGestureFallback();
    document.addEventListener('visibilitychange', _onVisibilityChange);
    // A page that is frozen by the browser, parked in the back/forward cache
    // or unloaded stops running. It cannot hear that an approval was
    // answered, so it leaves nothing on. When it runs again it starts over
    // from the reasons, and a lease that ran out meanwhile no longer counts.
    document.addEventListener('freeze', _everythingOff);
    window.addEventListener('pagehide', _everythingOff);
    document.addEventListener('resume', _reconcile);
    window.addEventListener('pageshow', _reconcile);
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', _boot);
  } else {
    _boot();
  }

  // Export public surface.
  window.VNNotify = {
    arm: arm,
    notice: notice,
    attention: attention,
    syncApprovals: syncApprovals,
    state: state,
    setEnabled: setEnabled,
    isEnabled: isEnabled,
  };
})();
