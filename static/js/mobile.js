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
