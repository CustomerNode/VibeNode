/* =============================================================================
   tooltip.js — themed replacement for native browser tooltips
   =============================================================================

   WHY THIS EXISTS
   ---------------
   VibeNode had ~160 `title="..."` attributes across index.html and the JS
   modules. Native tooltips are rendered by the OS, not the page: they use the
   system font at the system size, ignore the app's theme entirely, appear
   after a fixed ~500ms the page cannot tune, and are positioned by the
   platform. In an otherwise fully custom dark UI they were the single most
   obvious "this is a web page" tell.

   DESIGN CONSTRAINT: DO NOT TOUCH THE 160 CALL SITES
   --------------------------------------------------
   This module keeps `title` as the authoring API. Any element with a `title`
   gets a themed tooltip automatically — existing markup, dynamically created
   elements, and anything added in the future all work with zero changes.

   HOW THE NATIVE TOOLTIP IS SUPPRESSED
   ------------------------------------
   There is no CSS or API to disable native tooltips. The only way is for the
   attribute to be absent while the pointer is over the element. So we
   "borrow" it: on mouseover we stash the value and blank the attribute, and
   on mouseout we put it back.

   Borrowing (rather than permanently stripping into a data-* attribute) is
   deliberate and load-bearing. ~25 places in the codebase assign `el.title`
   at runtime to reflect state — voice.js swaps 'Voice input' / 'Stop
   recording', socket.js writes 'Connected' / 'Disconnected', smart-copy.js
   flashes 'Copied!'. If the attribute were stripped permanently those writes
   would land on a dead attribute and the tooltip would freeze at whatever
   text it had when the page loaded.

   Two guards make borrowing safe:
     1. Restore is conditional. We only write the stashed value back if the
        attribute is STILL the empty string we left. If application code set a
        new title while the pointer was over the element, that value wins and
        we never clobber it.
     2. A MutationObserver watches the active element's `title` while the
        tooltip is visible, so a runtime write updates the visible text live.
        This is strictly better than the native behaviour, which would keep
        showing the stale string until you moved away and came back.

   ACCESSIBILITY
   -------------
   The attribute is restored the moment the pointer leaves, so the accessible
   name is intact at all times except during active mouse hover — a state that
   does not apply to screen-reader navigation. No aria-label is synthesised,
   because forcing one onto elements that already have visible text would
   override their accessible name and make things worse.

   ============================================================================= */

(function () {
  'use strict';

  // ---------------------------------------------------------------------------
  // Environment guard
  // ---------------------------------------------------------------------------
  // Touch devices have no hover, so a hover tooltip is dead weight there — and
  // worse, `mouseover` fires synthetically on tap, which would flash a tooltip
  // on every button press. VibeNode is used from phones over Tailscale, so this
  // guard is doing real work, not being defensive.
  var canHover = false;
  try {
    canHover = window.matchMedia('(hover: hover) and (pointer: fine)').matches;
  } catch (_) { canHover = false; }
  if (!canHover) return;

  // ---------------------------------------------------------------------------
  // Timing
  // ---------------------------------------------------------------------------
  // SHOW_DELAY is the cold-start delay before the first tooltip appears. It has
  // to be long enough that sweeping the pointer across a toolbar doesn't strobe
  // tooltips, and short enough to feel responsive when you actually pause.
  //
  // WARM_MS implements the standard "tooltip group" behaviour every desktop
  // toolkit has: once one tooltip has been shown, moving to a neighbouring
  // control shows the next one instantly. Without it, scanning a row of icon
  // buttons means waiting out the full delay at every single one.
  var SHOW_DELAY = 420;
  var WARM_MS    = 600;
  var HIDE_DELAY = 60;

  var GAP = 8;          // px between the anchor and the tooltip
  var EDGE = 8;         // min px from the viewport edge

  // ---------------------------------------------------------------------------
  // State
  // ---------------------------------------------------------------------------
  var tipEl = null;       // the tooltip DOM node (created lazily)
  var anchor = null;      // element the visible tooltip belongs to
  var stashed = null;     // borrowed title text for `anchor`
  var showTimer = null;
  var hideTimer = null;
  var lastHiddenAt = 0;   // drives the WARM_MS grace period
  var observer = null;    // watches `anchor`'s title for runtime updates

  // ---------------------------------------------------------------------------
  // DOM
  // ---------------------------------------------------------------------------
  function ensureEl() {
    if (tipEl) return tipEl;
    tipEl = document.createElement('div');
    tipEl.className = 'vn-tip';
    tipEl.setAttribute('role', 'tooltip');
    // aria-hidden: the accessible name still comes from the restored `title`
    // attribute, so exposing this node too would double-announce it.
    tipEl.setAttribute('aria-hidden', 'true');
    document.body.appendChild(tipEl);
    return tipEl;
  }

  // ---------------------------------------------------------------------------
  // Eligibility
  // ---------------------------------------------------------------------------
  // Regions that already run their own richer hover affordance. Showing this
  // generic tooltip there too would stack two panels under one cursor.
  //
  // `.session-item` is the important one: sessions.js drives a full preview
  // card (#session-tooltip) on row hover, while the row's CHILD elements —
  // the state icon, the unread dot, the subsession chevron — each carry their
  // own `title`. Without this exclusion, hovering a state icon would pop the
  // preview card AND a tooltip at the same time. The preview already reports
  // the state, so the row-level affordance wins.
  var EXCLUDED = '.session-item';

  function tipTargetFrom(node) {
    // Walk up from the event target: `title` is inherited visually by children
    // (hovering the <svg> inside a button shows the button's tooltip), so the
    // nearest ancestor carrying a non-empty title is the real anchor.
    var el = node;
    while (el && el !== document.body && el.nodeType === 1) {
      if (el.hasAttribute('data-no-tip')) return null;
      try { if (el.matches(EXCLUDED)) return null; } catch (_) {}
      var t = el.getAttribute('title');
      if (t && t.trim()) return el;
      el = el.parentElement;
    }
    return null;
  }

  // ---------------------------------------------------------------------------
  // Positioning
  // ---------------------------------------------------------------------------
  function place() {
    if (!tipEl || !anchor) return;
    if (!anchor.isConnected) { hide(true); return; }

    var r = anchor.getBoundingClientRect();
    // A zero-size rect means the anchor is display:none or detached mid-hover.
    if (!r.width && !r.height) { hide(true); return; }

    // Measure with the final text already in place. Clearing the transform
    // first stops a previous frame's translate from skewing the measurement.
    tipEl.style.transform = '';
    var tw = tipEl.offsetWidth;
    var th = tipEl.offsetHeight;

    var vw = document.documentElement.clientWidth;
    var vh = document.documentElement.clientHeight;

    // Prefer below; flip above when the bottom would be clipped. Header
    // controls sit near the top and popovers near the bottom, so both
    // directions are genuinely used.
    var below = r.bottom + GAP;
    var above = r.top - th - GAP;
    var top, dir;
    if (below + th <= vh - EDGE) { top = below; dir = 'below'; }
    else if (above >= EDGE)      { top = above; dir = 'above'; }
    else { top = Math.min(Math.max(EDGE, below), vh - th - EDGE); dir = 'below'; }

    // Centre horizontally on the anchor, then clamp into the viewport so a
    // tooltip on a far-right control (the System menu, the git buttons) stays
    // fully readable instead of running off the edge.
    var left = r.left + (r.width / 2) - (tw / 2);
    left = Math.max(EDGE, Math.min(left, vw - tw - EDGE));

    tipEl.style.left = Math.round(left) + 'px';
    tipEl.style.top = Math.round(top) + 'px';
    tipEl.setAttribute('data-dir', dir);
  }

  // ---------------------------------------------------------------------------
  // Show / hide
  // ---------------------------------------------------------------------------
  function show(el) {
    var text = el.getAttribute('title');
    if (!text || !text.trim()) return;

    anchor = el;
    stashed = text;

    // Borrow the attribute — this is what suppresses the native tooltip.
    el.setAttribute('title', '');

    var node = ensureEl();
    node.textContent = text;
    node.classList.add('visible');

    place();
    watchTitle(el);
  }

  function hide(immediate) {
    clearTimeout(showTimer); showTimer = null;
    unwatchTitle();

    if (anchor) {
      // Only give the title back if nothing else claimed it while we held it.
      // If application code wrote a new value during hover, that value is
      // newer than ours and must survive.
      if (anchor.getAttribute('title') === '') {
        anchor.setAttribute('title', stashed == null ? '' : stashed);
      }
      anchor = null;
      stashed = null;
    }

    if (tipEl && tipEl.classList.contains('visible')) {
      tipEl.classList.remove('visible');
      lastHiddenAt = Date.now();
    }
    if (immediate && tipEl) tipEl.setAttribute('data-dir', '');
  }

  // ---------------------------------------------------------------------------
  // Live title updates
  // ---------------------------------------------------------------------------
  // smart-copy.js flashes `btn.title = 'Copied!'` on click — which happens
  // while the pointer is still on the button, i.e. while we are holding the
  // attribute. Without this observer the tooltip would keep showing 'Copy'.
  function watchTitle(el) {
    unwatchTitle();
    if (typeof MutationObserver !== 'function') return;
    observer = new MutationObserver(function () {
      if (!anchor || !tipEl) return;
      var v = anchor.getAttribute('title');
      if (v && v.trim()) {
        // Application code wrote a fresh value. Adopt it as the new text and
        // re-borrow so the native tooltip stays suppressed.
        stashed = v;
        tipEl.textContent = v;
        anchor.setAttribute('title', '');
        place();
      }
    });
    observer.observe(el, { attributes: true, attributeFilter: ['title'] });
  }

  function unwatchTitle() {
    if (observer) { try { observer.disconnect(); } catch (_) {} observer = null; }
  }

  // ---------------------------------------------------------------------------
  // Event wiring (delegated — works for dynamically created elements)
  // ---------------------------------------------------------------------------
  document.addEventListener('mouseover', function (e) {
    var el = tipTargetFrom(e.target);
    if (!el || el === anchor) return;

    clearTimeout(hideTimer); hideTimer = null;
    clearTimeout(showTimer);

    // If a tooltip was visible very recently, skip the delay so moving along a
    // toolbar feels continuous rather than stuttering.
    var warm = (Date.now() - lastHiddenAt) < WARM_MS;

    if (anchor) hide(true);
    showTimer = setTimeout(function () { show(el); }, warm ? 0 : SHOW_DELAY);
  }, true);

  document.addEventListener('mouseout', function (e) {
    var el = tipTargetFrom(e.target);
    if (!el) return;
    // Ignore moves that stay inside the same anchor (e.g. crossing from the
    // button into its own <svg>), otherwise the tooltip flickers.
    if (e.relatedTarget && el.contains(e.relatedTarget)) return;

    clearTimeout(showTimer); showTimer = null;
    clearTimeout(hideTimer);
    hideTimer = setTimeout(function () { hide(); }, HIDE_DELAY);
  }, true);

  // A tooltip lingering over a menu you just opened is worse than no tooltip,
  // so any of these interactions dismisses it immediately.
  ['mousedown', 'click', 'wheel', 'dragstart', 'contextmenu'].forEach(function (evt) {
    document.addEventListener(evt, function () { hide(true); }, true);
  });

  document.addEventListener('keydown', function (e) {
    if (e.key === 'Escape') hide(true);
  }, true);

  // Scrolling anywhere (including inside the session list / conversation)
  // detaches the tooltip from its anchor, so dismiss rather than chase.
  window.addEventListener('scroll', function () { hide(true); }, true);
  window.addEventListener('resize', function () { hide(true); });
  window.addEventListener('blur', function () { hide(true); });

  // Safety net: if the anchor is removed from the DOM while its tooltip is up
  // (very common here — rows re-render on every session_state push), the
  // borrowed title would be lost with the node and the tooltip would hang.
  document.addEventListener('visibilitychange', function () {
    if (document.hidden) hide(true);
  });
  setInterval(function () {
    if (anchor && !anchor.isConnected) hide(true);
  }, 1000);

  // ---------------------------------------------------------------------------
  // Keyboard parity
  // ---------------------------------------------------------------------------
  // Keyboard users get the same tooltip on focus-visible, matching the
  // :focus-visible ring added in the same polish pass.
  document.addEventListener('focusin', function (e) {
    var el = e.target;
    if (!el || el.nodeType !== 1) return;
    try { if (!el.matches(':focus-visible')) return; } catch (_) { return; }
    var t = tipTargetFrom(el);
    if (!t || t === anchor) return;
    clearTimeout(showTimer);
    showTimer = setTimeout(function () { show(t); }, 150);
  });
  document.addEventListener('focusout', function () { hide(true); });
})();
