/* VibeNode — mobile behavior layer (companion to css/mobile.css).
 *
 * Principle: DO NOT change the web design. Same DOM, same components, same
 * interactions. This only makes the existing UI usable on a phone.
 *
 *   1. The REAL sidebar (project selector, view selector, New Session, search,
 *      session list) becomes a native slide-in drawer on mobile, toggled by the
 *      EXISTING collapse/expand buttons (toggleSidebar in app.js). No hamburger,
 *      no invented picker.
 *   2. Chat "•••" action sheet — the open-chat toolbar's actions in a native
 *      sheet (the toolbar's a wide button row that can't fit a phone).
 *
 * Everything is gated on phone widths; desktop is untouched.
 */
(function () {
  "use strict";

  var MQ = window.matchMedia("(max-width: 768px)");
  var isMobile = function () { return MQ.matches; };

  function escapeHtml(s) {
    return String(s == null ? "" : s)
      .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
  }

  // =========================================================================
  // Sidebar drawer — reuses the existing sidebar + its .collapsed state
  // =========================================================================
  function sidebar() { return document.querySelector(".sidebar"); }
  function expandBtn() { return document.getElementById("btn-sidebar-expand"); }
  function currentView() {
    try { return (typeof viewMode !== "undefined") ? viewMode : ""; } catch (e) { return ""; }
  }
  function drawerOpen() { var sb = sidebar(); return !!sb && !sb.classList.contains("collapsed"); }
  // Open/close via the app's own toggleSidebar (keeps its state/localStorage).
  function setDrawerOpen(open) {
    if (drawerOpen() !== open && typeof toggleSidebar === "function") toggleSidebar();
  }

  var backdrop = null;
  function ensureBackdrop() {
    if (backdrop) return backdrop;
    backdrop = document.createElement("div");
    backdrop.id = "mobile-backdrop";
    backdrop.addEventListener("click", function () { setDrawerOpen(false); });
    document.body.appendChild(backdrop);
    return backdrop;
  }

  // Keep the opener button + backdrop in sync with the .collapsed state,
  // whoever changed it (our code, the ‹ collapse button, the → expand button).
  function syncFromState() {
    var sb = sidebar(); if (!sb) return;
    var open = !sb.classList.contains("collapsed");
    var eb = expandBtn();
    if (eb) {
      // The burger opener shows only when drilled in (non-homepage) and the
      // drawer is closed. On the landing it stays hidden (branding shows there).
      var show = isMobile() && !open && currentView() !== "homepage";
      eb.classList.toggle("visible", show);
      eb.style.display = show ? "" : "none";   // beat the boot-time inline display:none
    }
    ensureBackdrop().classList.toggle("show", isMobile() && open);
  }

  // The Sessions view IS the session list — open the drawer for it; every other
  // view starts closed (content first). Re-run on each view change.
  function updateChrome() {
    // Drilled-in (any non-homepage view): hide branding, show the burger.
    // Landing (homepage): show branding, hide the burger.
    document.body.classList.toggle("mob-drilled", isMobile() && currentView() !== "homepage");
  }
  // A session is "open" when app.js's activeId is set (shared global lexical scope).
  function sessionActive() {
    try { return !!activeId; } catch (e) { return false; }
  }
  function applyView() {
    if (!isMobile()) return;
    updateChrome();
    // Auto-open the drawer for the sessions view only while browsing the list (no
    // session open yet). Once a session is active, keep it closed so picking a
    // session reveals the chat instead of the drawer springing back open.
    setDrawerOpen(currentView() === "sessions" && !sessionActive());
    syncFromState();
  }

  // The opener on the homepage jumps to the Sessions view (the homepage sidebar
  // has no list); elsewhere it just toggles the drawer. Set onclick directly so
  // there's no listener race with the inline onclick.
  function wireExpandBtn() {
    var eb = expandBtn();
    if (!eb) return;
    eb.onclick = function () {
      if (isMobile()) { setDrawerOpen(!drawerOpen()); return false; }   // burger toggles the drawer
      if (typeof toggleSidebar === "function") toggleSidebar();          // desktop unchanged
      return false;
    };
  }

  // Icons are swapped for mobile (burger opener + X close) and restored for
  // desktop. Originals are captured once so restoring is exact.
  var _BURGER = '<svg width="24" height="24" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><line x1="3" y1="6" x2="21" y2="6"/><line x1="3" y1="12" x2="21" y2="12"/><line x1="3" y1="18" x2="21" y2="18"/></svg>';
  var _CLOSE = '<svg width="26" height="26" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round"><line x1="18" y1="6" x2="6" y2="18"/><line x1="6" y1="6" x2="18" y2="18"/></svg>';
  var _origExpandIcon, _origCollapseIcon;
  function setMobileIcons(on) {
    var eb = expandBtn(), cb = document.getElementById("btn-sidebar-toggle");
    if (on) {
      if (eb && _origExpandIcon === undefined) _origExpandIcon = eb.innerHTML;
      if (cb && _origCollapseIcon === undefined) _origCollapseIcon = cb.innerHTML;
      if (eb) eb.innerHTML = _BURGER;
      if (cb) cb.innerHTML = _CLOSE;
    } else {
      if (eb && _origExpandIcon !== undefined) eb.innerHTML = _origExpandIcon;
      if (cb && _origCollapseIcon !== undefined) cb.innerHTML = _origCollapseIcon;
    }
  }

  // Re-apply EVERY breakpoint-dependent bit — run on load and whenever the
  // viewport crosses the mobile/desktop boundary, so a resize never needs a refresh.
  function applyResponsive() {
    if (isMobile()) {
      setMobileIcons(true);
      applyView();
    } else {
      // Restore desktop: undo the mobile-only DOM state.
      setMobileIcons(false);
      document.body.classList.remove("mob-drilled");
      var eb = expandBtn(); if (eb) eb.style.display = "";   // let desktop CSS control it
      if (backdrop) backdrop.classList.remove("show");
      closeSheet();
    }
  }

  function initSidebar() {
    var sb = sidebar();
    if (!sb) { setTimeout(initSidebar, 200); return; }

    new MutationObserver(syncFromState).observe(sb, { attributes: true, attributeFilter: ["class"] });
    wireExpandBtn();
    applyResponsive();

    // Picking a session must collapse the drawer so the chat is revealed. openInGUI
    // is the single choke point every open path funnels through (row tap →
    // singleOrDouble → openInGUI; name tap → handleNameClick → openInGUI), so wrap it
    // rather than guess from a click timer. This fires exactly when the session opens,
    // and openInGUI never re-opens the sidebar, so the close reliably sticks.
    if (typeof window.openInGUI === "function" && !window.openInGUI._mobClose) {
      var _openInGUI = window.openInGUI;
      window.openInGUI = function () {
        var r = _openInGUI.apply(this, arguments);
        if (isMobile()) setDrawerOpen(false);
        return r;
      };
      window.openInGUI._mobClose = true;
    }

    // Re-evaluate the drawer whenever the app switches views.
    if (typeof window.setViewMode === "function" && !window.setViewMode._mobWrapped) {
      var _svm = window.setViewMode;
      window.setViewMode = function () { var r = _svm.apply(this, arguments); applyView(); return r; };
      window.setViewMode._mobWrapped = true;
    }
  }

  // =========================================================================
  // Edge-swipe to open / swipe to close the drawer (iOS-style, follows finger)
  //
  // Open: a drag that STARTS within EDGE px of the left screen edge (drawer
  // closed) drags the drawer in 1:1 with the finger. Close: a drag anywhere on
  // the OPEN drawer drags it back out. On release we snap to whichever side the
  // gesture committed to (past ~30% of the width) and hand the final animation +
  // state back to the app's own .collapsed toggle so localStorage stays in sync.
  // =========================================================================
  function initSwipe() {
    var sb = sidebar();
    if (!sb) { setTimeout(initSwipe, 200); return; }

    // A rightward drag starting ANYWHERE on the screen opens the drawer (iOS-style —
    // you can begin the swipe mid-screen, not just at the edge). We only skip the
    // first few px, which mobile browsers reserve for their own back-navigation
    // gesture, and we bail if the drag begins inside something that scrolls
    // horizontally (a code block) so we don't hijack its own left/right scroll.
    var EDGE_MIN = 8;          // ignore the first few px (browser back-swipe territory)
    var SLOP = 8;               // px of movement before we lock horizontal vs vertical
    var COMMIT = 0.3;           // fraction of width past which the gesture "wins"

    // True if el or an ancestor (up to the drawer/body) can actually scroll sideways.
    function inHorizontalScroller(el) {
      for (var n = el; n && n !== document.body; n = n.parentElement) {
        if (n.scrollWidth > n.clientWidth + 2) {
          var ox = getComputedStyle(n).overflowX;
          if (ox === "auto" || ox === "scroll") return true;
        }
      }
      return false;
    }
    var startX = 0, startY = 0, dx = 0;
    var tracking = false;       // a candidate gesture is in progress
    var decided = false;        // locked into a horizontal drag (vs vertical scroll)
    var mode = null;            // "open" | "close"
    var width = 0;

    function drawerWidth() { return sb.getBoundingClientRect().width || window.innerWidth; }

    function onStart(e) {
      tracking = decided = false; mode = null;
      if (!isMobile() || currentView() === "homepage") return;   // no list to reveal on landing
      if (!e.touches || e.touches.length !== 1) return;
      var t = e.touches[0];
      var open = drawerOpen();
      if (!open && t.clientX >= EDGE_MIN && !inHorizontalScroller(e.target)) mode = "open";  // rightward drag anywhere opens
      else if (open) mode = "close";                            // any drag on the drawer closes
      else return;
      startX = t.clientX; startY = t.clientY; dx = 0;
      width = drawerWidth(); tracking = true;
    }

    function onMove(e) {
      if (!tracking) return;
      var t = e.touches[0];
      dx = t.clientX - startX;
      var dy = t.clientY - startY;
      if (!decided) {
        if (Math.abs(dx) < SLOP && Math.abs(dy) < SLOP) return;  // wait for intent
        if (Math.abs(dy) >= Math.abs(dx)) { tracking = false; return; }  // vertical → let it scroll
        // Direction must match the gesture: open = rightward, close = leftward.
        // A drag the "wrong" way (e.g. leftward scroll inside a code block that
        // happens to start in the open band) is released back to the content.
        if ((mode === "open" && dx <= 0) || (mode === "close" && dx >= 0)) { tracking = false; return; }
        decided = true;
        sb.style.transition = "none";                            // follow the finger with no lag
        var bd0 = ensureBackdrop();
        bd0.style.transition = "none";
        bd0.classList.add("show");                               // enable pointer-events; opacity set live
      }
      e.preventDefault();                                        // we own this gesture now
      var base = (mode === "open") ? -width : 0;                 // closed sits at -100%, open at 0
      var pos = Math.max(-width, Math.min(0, base + dx));
      sb.style.transform = "translateX(" + pos + "px)";
      ensureBackdrop().style.opacity = String(0.5 * (1 + pos / width));  // 0 closed → .5 open
    }

    function onEnd() {
      if (!tracking) return;
      var wasDecided = decided;
      tracking = false; decided = false;
      if (!wasDecided) return;                                   // never became a horizontal drag

      var target = (mode === "open") ? (dx > width * COMMIT)     // opened far enough?
                                     : (dx > -width * COMMIT);   // stayed (not dragged far left)?

      // Restore transitions, flip the app's own drawer state, then drop the inline
      // transform so the element animates from where the finger left it to the
      // class position (0 or -100%). setDrawerOpen persists state via toggleSidebar.
      var bd = ensureBackdrop();
      sb.style.transition = "";
      bd.style.transition = "";
      bd.style.opacity = "";
      setDrawerOpen(target);
      sb.style.transform = "";
      syncFromState();
    }

    document.addEventListener("touchstart", onStart, { passive: true });
    document.addEventListener("touchmove", onMove, { passive: false });  // preventDefault needs this
    document.addEventListener("touchend", onEnd, { passive: true });
    document.addEventListener("touchcancel", onEnd, { passive: true });
  }

  // =========================================================================
  // Chat "•••" action sheet (open-chat toolbar actions, iOS bottom sheet)
  // =========================================================================
  function buildSheetRows() {
    var rows = [];
    var titleEl = document.getElementById("main-title");
    if (titleEl && titleEl.dataset.editable === "true") {
      rows.push({ label: "Rename", disabled: false, run: function () {
        if (typeof startToolbarRename === "function") startToolbarRename();
      } });
    }
    document.querySelectorAll("#grp-analyze .btn-group-inner .btn").forEach(function (btn) {
      rows.push({
        label: (btn.textContent || "").trim(),
        disabled: !!btn.disabled,
        run: function () { btn.click(); },
      });
    });
    rows.push({ label: "Actions…", disabled: false, run: function () {
      if (typeof openActionsPopup === "function") openActionsPopup();
    } });
    return rows;
  }

  function closeSheet() {
    var s = document.getElementById("mobile-sheet");
    var b = document.getElementById("mobile-sheet-backdrop");
    if (s) s.classList.remove("show");
    if (b) b.classList.remove("show");
  }

  function openActionSheet() {
    var b = document.getElementById("mobile-sheet-backdrop");
    if (!b) {
      b = document.createElement("div");
      b.id = "mobile-sheet-backdrop";
      b.addEventListener("click", closeSheet);
      document.body.appendChild(b);
    }
    var sheet = document.getElementById("mobile-sheet");
    if (!sheet) {
      sheet = document.createElement("div");
      sheet.id = "mobile-sheet";
      document.body.appendChild(sheet);
    }
    var rows = buildSheetRows();
    var html = '<div class="sheet-card">';
    rows.forEach(function (r, i) {
      html += '<button class="sheet-item" data-i="' + i + '"' +
              (r.disabled ? " disabled" : "") + ">" + escapeHtml(r.label) + "</button>";
    });
    html += '</div><button class="sheet-cancel">Cancel</button>';
    sheet.innerHTML = html;
    sheet.querySelectorAll(".sheet-item").forEach(function (el) {
      el.addEventListener("click", function () {
        var r = rows[+el.getAttribute("data-i")];
        closeSheet();
        if (r && !r.disabled) setTimeout(r.run, 80);
      });
    });
    sheet.querySelector(".sheet-cancel").addEventListener("click", closeSheet);
    requestAnimationFrame(function () { b.classList.add("show"); sheet.classList.add("show"); });
  }

  function ensureMoreButton() {
    var tb = document.getElementById("main-toolbar");
    if (!tb || document.getElementById("toolbar-more")) return;
    var b = document.createElement("button");
    b.id = "toolbar-more";
    b.className = "toolbar-more";
    b.setAttribute("aria-label", "More actions");
    b.innerHTML = '<svg width="22" height="22" viewBox="0 0 24 24" fill="currentColor">' +
      '<circle cx="5" cy="12" r="2"/><circle cx="12" cy="12" r="2"/><circle cx="19" cy="12" r="2"/></svg>';
    b.addEventListener("click", openActionSheet);
    tb.appendChild(b);
  }

  // =========================================================================
  function init() {
    initSidebar();
    initSwipe();
    ensureMoreButton();
  }

  if (MQ.addEventListener) {
    MQ.addEventListener("change", applyResponsive);   // re-apply on every breakpoint cross
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", init);
  } else {
    init();
  }
})();
