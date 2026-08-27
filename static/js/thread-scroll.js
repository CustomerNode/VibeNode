/**
 * thread-scroll.js — the session thread's scroll + collapse state machine.
 *
 * ── The problem this replaces ──────────────────────────────────────────────
 * The old model was a single boolean (`liveAutoScroll`) flipped by a `scroll`
 * listener that compared `scrollHeight - scrollTop - clientHeight < 60`.  Two
 * defects fell out of that:
 *
 *   1. Intent was inferred from scroll *position*, which is noisy.  A
 *      programmatic scroll fires an indistinguishable `scroll` event, so a
 *      150ms blind-spot hack (`_autoScrollTopAlignTs`) was needed to tell them
 *      apart — and on mobile, momentum scrolling and iOS Safari's address-bar
 *      collapse (which changes `clientHeight`) both defeat it.
 *
 *   2. Auto-collapse was unsafe *regardless* of state.  Reading a tall, newly
 *      arrived AI message means scrolling DOWN, toward the bottom — so the old
 *      "user scrolled away" heuristic never fired, `_collapseRecentAsst()` ran
 *      anyway, and the message you were mid-way through shrank by thousands of
 *      pixels under your eyes.  This was the single most-reported annoyance.
 *
 * ── The model ─────────────────────────────────────────────────────────────
 * Three states, on a descending ladder of automation:
 *
 *   LIVE     follow the tail + auto-collapse superseded messages
 *   FOLLOW   follow the tail, never auto-collapse
 *   READING  touch nothing
 *
 *                     ┌──────────────────────────────┐
 *     session open ──▶ │            LIVE              │
 *                     └──┬───────────────────────▲───┘
 *         userScroll-away│           send /       │
 *         or userExpand  │           catchUp()    │
 *                     ┌──▼───────────────────────┴───┐
 *                     │           FOLLOW             │
 *                     └──┬───────────────────────▲───┘
 *         userScroll-away│           atBottom     │
 *                        │           (settled)    │
 *                     ┌──▼───────────────────────┴───┐
 *                     │           READING            │
 *                     └──────────────────────────────┘
 *          READING ──send / catchUp()──▶ LIVE (+ scroll to bottom)
 *
 * The governing principle, and the reason FOLLOW exists as a distinct rung:
 *
 *     RESTORE AUTOMATION IN ORDER OF HOW DESTRUCTIVE IT IS.
 *
 * Scrolling is cheap and reversible — you can always scroll back — so it is
 * restored on a *weak* signal (drifting to the bottom).  Collapsing is
 * destructive: it discards the expanded state of something you were reading and
 * there is no undo.  So it is restored only on a *strong, deliberate* signal —
 * sending a message, or tapping the jump-to-latest pill.
 *
 * ── Three mechanisms make it work ─────────────────────────────────────────
 *
 * A. INTENT COMES FROM INPUT EVENTS, NOT SCROLL EVENTS.  `wheel`, `touchmove`,
 *    `mousedown` (scrollbar drags) and navigation `keydown` set a "user is
 *    driving" flag.  A programmatic `scrollTop =` produces none of these, so it
 *    can never be mistaken for user intent.  This is what lets us delete the
 *    150ms blind-spot hack outright.
 *
 * B. "CAUGHT UP" IS A SENTINEL, NOT ARITHMETIC.  A zero-height element pinned
 *    as the last child of the log, watched by an IntersectionObserver.  Immune
 *    to iOS address-bar resize, sub-pixel rounding and high-DPI off-by-ones.
 *    It also resolves defect (2) for free: reading a tall message means the
 *    sentinel is NOT visible, so you are — correctly — not caught up.
 *
 * C. COLLAPSE IS VIEWPORT-SAFE IN EVERY STATE.  `requestCollapse()` never
 *    collapses an element inside the KEEP-ALIVE ZONE — the viewport plus one
 *    viewport of hysteresis on each side; it queues it and fires once the
 *    element is well clear.  The hysteresis matters because the common move is
 *    read a message, scroll down to the newer one, then scroll back up a little
 *    to re-check something: a tight margin would collapse the older message the
 *    instant it cleared the edge and that round trip would lose it.  When a
 *    queued collapse does fire on content ABOVE the viewport, the resulting
 *    height delta is compensated in `scrollTop` so the reader's position does
 *    not jump — the same anchoring technique already used by the load-older
 *    prepend path in socket.js.
 *
 * PERF: one IntersectionObserver + one MutationObserver + one passive scroll
 * listener per attached log, all torn down in attach()/detach().  The scroll
 * listener is rAF-coalesced and only does work when a collapse is queued.
 */
(function () {
  'use strict';

  var LIVE = 'LIVE', FOLLOW = 'FOLLOW', READING = 'READING';

  // ── Tunables ────────────────────────────────────────────────────────────
  // Sentinel visible AND no input for this long => genuinely caught up.
  // Without a settle delay a fast mobile flick that overshoots into the bottom
  // rubber-band would flip us into FOLLOW mid-flick and then yank the view.
  var SETTLE_MS = 200;
  // How long the "user is driving" flag survives the last input event. Covers
  // iOS momentum scrolling, which keeps firing scroll events after touchend.
  var DRIVE_DECAY_MS = 150;
  // Multiplier on the viewport height defining the "keep alive" zone around
  // the visible box. A message is collapsed only once it is this far BEYOND
  // the edge — merely being off-screen is not enough.
  //
  // Why a whole viewport instead of a few px of slack: the common move is to
  // read a message, scroll down to the newer one, then scroll back up a little
  // to re-check something. With a tight margin the older message collapses the
  // instant it clears the edge, so scrolling back finds a truncated stub. One
  // viewport of hysteresis makes that round trip lossless, and costs only one
  // screenful of extra expanded DOM.
  var KEEP_ALIVE_VIEWPORTS = 1;
  var KEEP_ALIVE_FALLBACK_PX = 600;   // if clientHeight is not measurable yet
  // Bottom tolerance for the sentinel. Touch devices get more: rubber-banding
  // and dynamic viewport chrome make the exact bottom edge fuzzy.
  var _isTouch = (function () {
    try { return window.matchMedia('(pointer: coarse)').matches; } catch (e) { return false; }
  })();
  var BOTTOM_SLOP = _isTouch ? 120 : 80;
  // A childList mutation adding more than this many nodes is a full re-render
  // (innerHTML replace), not new chat activity — don't count it toward the pill.
  var RERENDER_NODE_THRESHOLD = 5;

  // ── Per-attachment state ────────────────────────────────────────────────
  var _state = LIVE;
  var _sid = null;
  var _logEl = null;
  var _sentinel = null;
  var _pillWrap = null;
  var _pillEl = null;
  var _io = null;
  var _mo = null;
  var _atBottom = true;
  var _driving = false;
  var _driveTimer = null;
  var _settleTimer = null;
  var _rafId = null;
  var _pending = [];      // deferred collapses: [{el, fn}]
  var _newCount = 0;
  var _listeners = [];    // [[target, type, fn, opts]] for teardown

  // ── State transitions ───────────────────────────────────────────────────

  function _setState(next, why) {
    if (next === _state) return;
    var prev = _state;
    _state = next;

    // Keep the legacy global in sync. Many call sites across live-panel.js and
    // socket.js still do `if (liveAutoScroll) logEl.scrollTop = ...`; mapping
    // READING -> false makes every one of them correct with no edit.
    try { liveAutoScroll = (_state !== READING); } catch (e) { /* not loaded yet */ }
    window._threadScrollState = _state;

    if (_state === LIVE) {
      _newCount = 0;
      _flushPending();
    }
    _renderPill();

    if (window._threadScrollDebug) {
      console.log('[thread-scroll]', prev, '->', _state, '(' + (why || '') + ')');
    }
  }

  function _markDriving() {
    _driving = true;
    if (_driveTimer) clearTimeout(_driveTimer);
    _driveTimer = setTimeout(function () {
      _driving = false;
      _maybeSettle();
    }, DRIVE_DECAY_MS);
    // If we already know we're off the bottom, drop out immediately rather than
    // waiting for the observer to fire.
    if (!_atBottom && _state !== READING) _setState(READING, 'user-scroll');
  }

  function _maybeSettle() {
    if (_settleTimer) { clearTimeout(_settleTimer); _settleTimer = null; }
    if (!_atBottom || _state !== READING) return;
    _settleTimer = setTimeout(function () {
      _settleTimer = null;
      // Re-check everything — the user may have flicked away again.
      if (_atBottom && !_driving && _state === READING) {
        _setState(FOLLOW, 'settled-at-bottom');
      }
    }, SETTLE_MS);
  }

  // ── Sentinel / observers ────────────────────────────────────────────────

  /**
   * Keep the tail as [ ...content, pillWrap, sentinel ].
   *
   * The bail-out check must test BOTH slots together. Checking them
   * independently ("is pillWrap last? is sentinel last?") is an infinite loop:
   * satisfying one condition breaks the other, so every MutationObserver pass
   * reorders the pair and schedules another pass, forever.
   */
  function _placeTail() {
    if (!_logEl || !_sentinel) return;
    var kids = _logEl.children;
    var n = kids.length;
    var last = n ? kids[n - 1] : null;
    var secondLast = n >= 2 ? kids[n - 2] : null;
    if (last === _sentinel && (!_pillWrap || secondLast === _pillWrap)) return;
    if (_pillWrap) _logEl.appendChild(_pillWrap);
    _logEl.appendChild(_sentinel);
  }

  function _onSentinel(entries) {
    if (!entries || !entries.length) return;
    _atBottom = !!entries[entries.length - 1].isIntersecting;

    if (_atBottom) {
      _newCount = 0;
      _renderPill();
      _maybeSettle();
      return;
    }

    // Scrolled off the bottom. Only a USER-driven scroll demotes us to READING;
    // a programmatic top-align must not. This is the replacement for the old
    // 150ms `_autoScrollTopAlignTs` blind spot.
    if (_driving && _state !== READING) _setState(READING, 'user-scroll-away');
    _renderPill();
  }

  function _onMutation(records) {
    if (!_logEl) return;

    // Re-pin the tail. Our own append arrives as a later record, by which point
    // the sentinel IS last, so this cannot loop.
    _placeTail();

    if (_atBottom) return;   // nothing to announce; the user can see it

    var added = 0;
    for (var i = 0; i < records.length; i++) {
      var nodes = records[i].addedNodes;
      for (var j = 0; j < nodes.length; j++) {
        var n = nodes[j];
        if (n.nodeType !== 1) continue;
        if (n === _sentinel || n === _pillWrap) continue;
        if (n.classList && (n.classList.contains('msg') || n.classList.contains('live-entry'))) added++;
      }
    }
    // A big batch is a full re-render, not new chat activity.
    if (!added || added > RERENDER_NODE_THRESHOLD) return;
    _newCount += added;
    _renderPill();
  }

  function _onScrollPassive() {
    if (_rafId) return;
    _rafId = requestAnimationFrame(function () {
      _rafId = null;
      _flushPending();
    });
  }

  // ── Viewport-safe collapse ──────────────────────────────────────────────

  /**
   * Is `el` inside the keep-alive zone — the viewport plus one viewport of
   * hysteresis on each side? Anything in here is off limits for auto-collapse.
   */
  function _inKeepAliveZone(el) {
    if (!_logEl || !el || !el.isConnected) return false;
    var pad = (_logEl.clientHeight || KEEP_ALIVE_FALLBACK_PX) * KEEP_ALIVE_VIEWPORTS;
    var r = el.getBoundingClientRect();
    var c = _logEl.getBoundingClientRect();
    return r.bottom > c.top - pad && r.top < c.bottom + pad;
  }

  /**
   * Run `fn` (which changes `el`'s height) while holding the reader's position
   * steady. Only content ABOVE the viewport can displace what the user is
   * looking at, so that is the only case we compensate.
   */
  function _anchored(el, fn) {
    if (!_logEl) { fn(); return; }
    var cTop = _logEl.getBoundingClientRect().top;
    var isAbove = el.getBoundingClientRect().bottom <= cTop;
    var beforeH = _logEl.scrollHeight;
    var beforeTop = _logEl.scrollTop;
    fn();
    if (!isAbove) return;
    var delta = _logEl.scrollHeight - beforeH;
    if (delta) _logEl.scrollTop = Math.max(0, beforeTop + delta);
  }

  function _flushPending() {
    if (_state !== LIVE || !_pending.length) return;
    var keep = [];
    for (var i = 0; i < _pending.length; i++) {
      var p = _pending[i];
      if (!p.el || !p.el.isConnected) continue;         // element went away
      if (_inKeepAliveZone(p.el)) { keep.push(p); continue; }
      try { _anchored(p.el, p.fn); } catch (e) { /* non-fatal */ }
    }
    _pending = keep;
  }

  // ── Jump-to-latest pill ─────────────────────────────────────────────────

  function _buildPill() {
    _pillWrap = document.createElement('div');
    _pillWrap.className = 'vn-jump-wrap';
    _pillWrap.setAttribute('aria-hidden', 'true');

    _pillEl = document.createElement('button');
    _pillEl.type = 'button';
    _pillEl.className = 'vn-jump-pill';
    _pillEl.addEventListener('click', function (ev) {
      ev.preventDefault();
      ev.stopPropagation();
      catchUp();
    });
    _pillWrap.appendChild(_pillEl);
  }

  function _renderPill() {
    if (!_pillEl || !_pillWrap) return;
    // The pill means "there is new content you are not seeing". At the bottom
    // there is nothing to jump to, so it stays hidden regardless of state.
    var show = !_atBottom && _newCount > 0;
    if (!show) {
      _pillWrap.classList.remove('visible');
      _pillWrap.setAttribute('aria-hidden', 'true');
      return;
    }
    // Compact label on purpose — the pill is an ambient hint, not a banner.
    _pillEl.innerHTML =
      '<svg width="11" height="11" viewBox="0 0 24 24" fill="none" stroke="currentColor" ' +
      'stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round">' +
      '<line x1="12" y1="5" x2="12" y2="19"/><polyline points="19 12 12 19 5 12"/></svg>' +
      '<span>' + _newCount + ' new</span>';
    _pillEl.setAttribute('aria-label',
      'Jump to latest — ' + _newCount + ' new message' + (_newCount === 1 ? '' : 's'));
    _pillWrap.classList.add('visible');
    _pillWrap.setAttribute('aria-hidden', 'false');
  }

  // ── Public API ──────────────────────────────────────────────────────────

  function _on(target, type, fn, opts) {
    target.addEventListener(type, fn, opts);
    _listeners.push([target, type, fn, opts]);
  }

  /**
   * Bind the state machine to a live-log scroll container. Idempotent per
   * element; re-attaching (e.g. on session switch) tears the old one down and
   * resets to LIVE — state must never leak across sessions.
   */
  function attach(logEl, sessionId) {
    detach();
    if (!logEl) return;
    _logEl = logEl;
    _sid = sessionId || null;
    _state = LIVE;
    _atBottom = true;
    _driving = false;
    _newCount = 0;
    _pending = [];
    try { liveAutoScroll = true; } catch (e) { /* not loaded yet */ }
    window._threadScrollState = LIVE;

    if (!_sentinel) {
      _sentinel = document.createElement('div');
      _sentinel.className = 'vn-thread-sentinel';
      _sentinel.setAttribute('aria-hidden', 'true');
    }
    if (!_pillWrap) _buildPill();
    _placeTail();
    _renderPill();

    // (B) "Caught up" is an observed sentinel, not arithmetic.
    if (typeof IntersectionObserver === 'function') {
      _io = new IntersectionObserver(_onSentinel, {
        root: _logEl,
        rootMargin: '0px 0px ' + BOTTOM_SLOP + 'px 0px',
        threshold: 0,
      });
      _io.observe(_sentinel);
    }

    // Keeps the sentinel pinned last across every append site, and counts new
    // entries for the pill — no edits needed at the ~8 append call sites.
    if (typeof MutationObserver === 'function') {
      _mo = new MutationObserver(_onMutation);
      _mo.observe(_logEl, { childList: true });
    }

    // (A) Intent comes from input events, never from the scroll event.
    _on(_logEl, 'wheel', _markDriving, { passive: true });
    _on(_logEl, 'touchmove', _markDriving, { passive: true });
    _on(_logEl, 'touchstart', _markDriving, { passive: true });
    _on(_logEl, 'mousedown', _markDriving, { passive: true });   // scrollbar drag
    _on(_logEl, 'keydown', function (e) {
      var t = e.target;
      if (t && (t.tagName === 'TEXTAREA' || t.tagName === 'INPUT' || t.isContentEditable)) return;
      if (/^(ArrowUp|ArrowDown|PageUp|PageDown|Home|End)$/.test(e.key)) _markDriving();
    });
    // Position-only listener: drains the deferred-collapse queue. Never used
    // to decide intent.
    _on(_logEl, 'scroll', _onScrollPassive, { passive: true });
  }

  function detach() {
    for (var i = 0; i < _listeners.length; i++) {
      var l = _listeners[i];
      try { l[0].removeEventListener(l[1], l[2], l[3]); } catch (e) { /* ignore */ }
    }
    _listeners = [];
    if (_io) { try { _io.disconnect(); } catch (e) {} _io = null; }
    if (_mo) { try { _mo.disconnect(); } catch (e) {} _mo = null; }
    if (_driveTimer) { clearTimeout(_driveTimer); _driveTimer = null; }
    if (_settleTimer) { clearTimeout(_settleTimer); _settleTimer = null; }
    if (_rafId) { cancelAnimationFrame(_rafId); _rafId = null; }
    _pending = [];
    _logEl = null;
    _sid = null;
  }

  /**
   * Ask to collapse `el` by running `fn`. Honors the state machine AND the
   * hard keep-alive rule: content the user can see — or could reach with a
   * small scroll back — is never collapsed, in any state. Deferred work is
   * retried as the user scrolls (in LIVE) and flushed wholesale on re-entering
   * LIVE.
   */
  function requestCollapse(el, fn) {
    if (typeof fn !== 'function') return;
    if (!el) { fn(); return; }
    if (_state === LIVE && !_inKeepAliveZone(el)) {
      try { _anchored(el, fn); } catch (e) { fn(); }
      return;
    }
    for (var i = 0; i < _pending.length; i++) if (_pending[i].el === el) return;  // already queued
    _pending.push({ el: el, fn: fn });
  }

  /** The user expanded something — that is a reading signal. */
  function noteUserExpand() {
    if (_state !== READING) _setState(READING, 'user-expand');
  }

  /**
   * The user sent a message. Unambiguous "I'm driving again" intent, so it
   * restores full LIVE from ANY scroll position and jumps to the bottom — if
   * you scrolled up to copy something and then sent, you want the reply.
   */
  function onSend() {
    _newCount = 0;
    _setState(LIVE, 'send');
    scrollToBottom();
  }

  /** Explicit catch-up (jump pill). The strong signal that restores LIVE. */
  function catchUp() {
    _newCount = 0;
    _setState(LIVE, 'catch-up');
    scrollToBottom();
  }

  function scrollToBottom() {
    if (!_logEl) return;
    _logEl.scrollTop = _logEl.scrollHeight;
    _atBottom = true;
    _renderPill();
  }

  function state() { return _state; }
  function isReading() { return _state === READING; }
  function mayAutoScroll() { return _state !== READING; }
  function mayAutoCollapse() { return _state === LIVE; }
  function atBottom() { return _atBottom; }

  window.ThreadScroll = {
    attach: attach,
    detach: detach,
    requestCollapse: requestCollapse,
    noteUserExpand: noteUserExpand,
    onSend: onSend,
    catchUp: catchUp,
    scrollToBottom: scrollToBottom,
    state: state,
    isReading: isReading,
    mayAutoScroll: mayAutoScroll,
    mayAutoCollapse: mayAutoCollapse,
    atBottom: atBottom,
    STATES: { LIVE: LIVE, FOLLOW: FOLLOW, READING: READING },
  };
})();
