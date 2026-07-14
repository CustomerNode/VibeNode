/* VibeNode — Mobile Visual Channel (preview cards + per-session aggregator + sheet)
 *
 * WHY THIS EXISTS
 * ---------------
 * When you code from a phone over Tailscale you cannot see the dev machine's
 * screen. This module is the visual back-channel: the agent emits a PREVIEW
 * MARKER in its message, we turn it into a tappable card in chat, collect all of
 * a session's previews behind a nav button, and open any of them in one iOS-style
 * bottom sheet. No tabs, no new pages.
 *
 * MARKER PROTOCOL (agent -> UI)
 * -----------------------------
 * The agent puts a line like this in its reply (either bracket style works):
 *
 *   [[VN-PREVIEW {"type":"browser","name":"Settings screen","src":"http://localhost:5173/settings"}]]
 *   ⟦VN-PREVIEW {"type":"image","name":"Build error","src":"http://.../shot.png"}⟧
 *
 * type: "browser" (rendered in an <iframe>) or "image" (rendered as <img>).
 * The marker is stripped from the visible message and becomes a card.
 *
 * The server tells the agent to do this (see ws_events.py mobile preamble); this
 * file is purely the client rendering + storage + UI surface.
 */
(function () {
  "use strict";

  // The phone reaches VibeNode at https://<name>.ts.net/ (tailscale serve). That
  // host suffix is our client-side "this turn is remote over Tailscale" signal —
  // NOT the source IP (serve proxies the phone to 127.0.0.1, so remote_addr lies).
  window.VN_IS_TAILNET = /\.ts\.net$/i.test(location.hostname);

  // Accept either ⟦…⟧ or [[…]] wrappers; JSON payload is non-greedy.
  var MARKER_RE = /(?:⟦|\[\[)VN-PREVIEW\s+(\{[\s\S]*?\})(?:⟧|\]\])/g;

  // Per-session preview store. id -> preview; plus insertion order via .seq.
  // preview = {id, type, name, src, ts (gen order), lastOpened (open order)|null}
  var _stores = Object.create(null);   // sessionId -> { byId:{}, seq:int }
  var _openSeq = 0;                     // monotonic "last opened" clock
  var _hasNew = Object.create(null);    // sessionId -> bool (new since last grid open)
  // The currently-open session. `liveSessionId` in live-panel is a `let` global
  // (not on window), so we can't read it here — we track it via the hooks below.
  var _activeSid = null;

  function _store(sid) {
    if (!_stores[sid]) _stores[sid] = { byId: Object.create(null), seq: 0 };
    return _stores[sid];
  }

  function esc(s) {
    return String(s == null ? "" : s)
      .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;").replace(/'/g, "&#39;");
  }

  // Stable id from type+src+name so re-rendering the same message (streaming,
  // show-more) never creates duplicate previews.
  function _pid(p) {
    var basis = (p.type || "") + "|" + (p.src || "") + "|" + (p.name || "");
    var h = 5381;
    for (var i = 0; i < basis.length; i++) h = ((h << 5) + h + basis.charCodeAt(i)) | 0;
    return "vp_" + (h >>> 0).toString(36);
  }

  function _normType(t) { return t === "image" ? "image" : "browser"; }

  // Pull every marker out of `text`, upsert into the session store, and return
  // {clean, found:[preview...]}. Idempotent: safe to call on every re-render.
  function extractForRender(sid, text) {
    var found = [];
    if (!text || text.indexOf("VN-PREVIEW") === -1) return { clean: text, found: found };
    var st = _store(sid);
    var clean = text.replace(MARKER_RE, function (_m, json) {
      var obj;
      try { obj = JSON.parse(json); } catch (e) { return ""; }
      if (!obj || !obj.src) return "";
      var p = {
        type: _normType(obj.type),
        name: (obj.name || "Preview").toString().slice(0, 80),
        src: String(obj.src),
      };
      p.id = _pid(p);
      var existing = st.byId[p.id];
      if (existing) {
        found.push(existing);
      } else {
        p.ts = ++st.seq;          // generation order within the session
        p.lastOpened = null;
        st.byId[p.id] = p;
        found.push(p);
        _hasNew[sid] = true;      // badge the nav button until the grid is opened
      }
      return "";                  // strip marker from the visible message
    });
    // Tidy the blank lines a stripped marker leaves behind.
    clean = clean.replace(/\n{3,}/g, "\n\n").replace(/^\s+|\s+$/g, "");
    return { clean: clean, found: found };
  }

  function list(sid) {
    var st = _stores[sid];
    if (!st) return [];
    var arr = Object.keys(st.byId).map(function (k) { return st.byId[k]; });
    // Sort by last-opened (desc), falling back to generation order (desc).
    arr.sort(function (a, b) {
      var av = a.lastOpened == null ? -Infinity : a.lastOpened;
      var bv = b.lastOpened == null ? -Infinity : b.lastOpened;
      if (av !== bv) return bv - av;
      return b.ts - a.ts;
    });
    return arr;
  }

  function count(sid) { return _stores[sid] ? Object.keys(_stores[sid].byId).length : 0; }

  var TYPE_META = {
    browser: { label: "Browser", glyph: "▦" },
    image:   { label: "Image",   glyph: "\u{1F5BC}" },
  };

  // ---- in-chat cards -------------------------------------------------------

  function _miniHTML(p) {
    // A generic stylized "render" placeholder for the thumbnail (we don't fetch
    // the real pixels for the card — the sheet does that on open).
    var accent = p.type === "image" ? "#30d158" : "#5e5ce6";
    return '<div class="vnp-mini">' +
             '<div class="vnp-mini-top" style="background:' + accent + '"></div>' +
             '<div class="vnp-mini-body">' +
               '<div class="vnp-mini-l" style="width:72%"></div>' +
               '<div class="vnp-mini-l" style="width:90%"></div>' +
               '<div class="vnp-mini-l" style="width:55%"></div>' +
             '</div>' +
           '</div>';
  }

  function _cardHTML(p) {
    var t = TYPE_META[p.type] || TYPE_META.browser;
    return '<div class="vnp-card" data-pid="' + esc(p.id) + '">' +
             '<div class="vnp-card-thumb">' + _miniHTML(p) + '</div>' +
             '<div class="vnp-card-meta">' +
               '<div class="vnp-card-ttl">' + esc(p.name) +
                 ' <span class="vnp-type vnp-type-' + p.type + '">' + t.glyph + ' ' + t.label + '</span>' +
               '</div>' +
               '<div class="vnp-card-sub">tap to open</div>' +
             '</div>' +
           '</div>';
  }

  // Render (or refresh) the card strip inside an assistant message element.
  function appendCards(div, found, sid) {
    if (!div) return;
    var old = div.querySelector(":scope > .vnp-cards");
    if (old) old.remove();
    if (!found || !found.length) { updateNavButton(sid); return; }
    var wrap = document.createElement("div");
    wrap.className = "vnp-cards";
    wrap.innerHTML = found.map(_cardHTML).join("");
    wrap.querySelectorAll(".vnp-card").forEach(function (el) {
      el.addEventListener("click", function () { openPreview(sid, el.getAttribute("data-pid")); });
    });
    div.appendChild(wrap);
    updateNavButton(sid);
  }

  // ---- aggregator nav button ----------------------------------------------

  function ensureNavButton() {
    var tb = document.getElementById("main-toolbar");
    if (!tb || document.getElementById("vnp-navbtn")) return;
    var b = document.createElement("button");
    b.id = "vnp-navbtn";
    b.className = "vnp-navbtn";
    b.setAttribute("aria-label", "Session previews");
    b.style.display = "none";
    b.innerHTML =
      '<span class="vnp-stack">' +
        '<span class="vnp-s vnp-s3"></span>' +
        '<span class="vnp-s vnp-s2"></span>' +
        '<span class="vnp-s vnp-s1"></span>' +
      '</span>' +
      '<span class="vnp-badge" id="vnp-badge">0</span>';
    b.addEventListener("click", function () {
      if (_activeSid) openGrid(_activeSid);
    });
    tb.appendChild(b);
  }

  function updateNavButton(sid) {
    ensureNavButton();
    var b = document.getElementById("vnp-navbtn");
    if (!b) return;
    // Button reflects the currently-open session only.
    if (sid && _activeSid && sid !== _activeSid) return;
    var n = sid ? count(sid) : 0;
    if (!n) { b.style.display = "none"; return; }
    b.style.display = "";
    var badge = document.getElementById("vnp-badge");
    if (badge) badge.textContent = String(n);
    // "New since last gallery open" is shown purely by the badge color (.has-new).
    b.classList.toggle("has-new", !!(sid && _hasNew[sid]));
  }

  // Called by live-panel when the active session changes, so the button tracks it.
  function onSessionActivated(sid) { _activeSid = sid; updateNavButton(sid); }

  // ---- the one bottom sheet (single + grid modes) --------------------------

  function _sheet() {
    var s = document.getElementById("vnp-sheet");
    if (s) return s;
    var back = document.createElement("div");
    back.id = "vnp-sheet-backdrop";
    back.addEventListener("click", closeSheet);
    document.body.appendChild(back);
    s = document.createElement("div");
    s.id = "vnp-sheet";
    s.innerHTML =
      '<div class="vnp-grabber" id="vnp-grabber"></div>' +
      '<div class="vnp-sheet-head">' +
        '<button class="vnp-back" id="vnp-act-grid" title="All previews" aria-label="Back to all previews">' +
          '<svg class="vnp-chev" width="11" height="18" viewBox="0 0 11 18" fill="none" aria-hidden="true">' +
            '<path d="M9.5 1.5 2 9l7.5 7.5" stroke="currentColor" stroke-width="2.4" stroke-linecap="round" stroke-linejoin="round"/>' +
          '</svg><span>All</span>' +
        '</button>' +
        '<div class="vnp-sheet-title" id="vnp-sheet-title">Previews</div>' +
        '<div class="vnp-sheet-actions">' +
          '<button class="vnp-act vnp-act-icon" id="vnp-act-fs" title="Fullscreen" aria-label="Enter fullscreen">' +
            '<svg width="16" height="16" viewBox="0 0 16 16" fill="none" aria-hidden="true">' +
              '<path d="M2 6V2h4M14 6V2h-4M2 10v4h4M14 10v4h-4" stroke="currentColor" stroke-width="1.9" stroke-linecap="round" stroke-linejoin="round"/>' +
            '</svg>' +
          '</button>' +
          '<button class="vnp-act" id="vnp-act-refresh" title="Reload">↻</button>' +
          '<button class="vnp-act" id="vnp-done">Done</button>' +
        '</div>' +
      '</div>' +
      '<div class="vnp-sheet-sub" id="vnp-sheet-sub"></div>' +
      '<div class="vnp-grid" id="vnp-grid"></div>' +
      '<div class="vnp-stage" id="vnp-stage"></div>' +
      // Floating controls that only appear in fullscreen mode. Kept siblings of
      // the stage so they overlay it without being scoped inside the iframe frame.
      '<button class="vnp-fs-exit" id="vnp-fs-exit" title="Exit fullscreen" aria-label="Exit fullscreen">' +
        '<svg width="18" height="18" viewBox="0 0 18 18" fill="none" aria-hidden="true">' +
          '<path d="M4 4l10 10M14 4L4 14" stroke="currentColor" stroke-width="2.2" stroke-linecap="round"/>' +
        '</svg>' +
      '</button>' +
      '<button class="vnp-fs-refresh" id="vnp-fs-refresh" title="Reload" aria-label="Reload">↻</button>';
    document.body.appendChild(s);
    document.getElementById("vnp-grabber").addEventListener("click", _cycleSnap);
    document.getElementById("vnp-done").addEventListener("click", closeSheet);
    document.getElementById("vnp-act-grid").addEventListener("click", function () {
      if (s._sid) openGrid(s._sid);
    });
    var _reloadStage = function () {
      if (s._sid && s._pid) {
        var p = _stores[s._sid] && _stores[s._sid].byId[s._pid];
        if (p) { s._bust = (s._bust || 0) + 1; _renderStage(p, s._bust); }
      }
    };
    document.getElementById("vnp-act-refresh").addEventListener("click", _reloadStage);
    document.getElementById("vnp-fs-refresh").addEventListener("click", _reloadStage);
    document.getElementById("vnp-act-fs").addEventListener("click", _enterFs);
    document.getElementById("vnp-fs-exit").addEventListener("click", _exitFs);
    // ESC exits fullscreen first, then closes the sheet — same as most viewers.
    document.addEventListener("keydown", function (e) {
      if (e.key !== "Escape") return;
      if (!s.classList.contains("show")) return;
      if (s.classList.contains("vnp-fs")) { _exitFs(); e.preventDefault(); }
    });
    _installDrag(s);
    return s;
  }

  // ---- fullscreen mode -----------------------------------------------------
  // Escapes the constraints of the bottom-sheet layout: no header, no letterbox
  // padding, no 380px frame cap, no rounded corners, no width cap. Just the
  // preview edge-to-edge on the phone. Only meaningful in single-preview mode.
  function _enterFs() {
    var s = _sheet();
    if (s.classList.contains("vnp-gridmode")) return;   // no fullscreen for the grid
    s.classList.add("vnp-fs");
  }
  function _exitFs() {
    var s = document.getElementById("vnp-sheet");
    if (s) s.classList.remove("vnp-fs");
  }

  // Snap positions as translateY percent of the sheet: full, half, peek.
  var SNAP_PCT = { full: 0, half: 46, peek: 72 };
  var SNAP_ORDER = ["full", "half", "peek"];

  function _snapClass(name) {
    var s = _sheet();
    s.classList.remove("vnp-full", "vnp-half", "vnp-peek");
    s.classList.add("vnp-" + name);
  }

  function _curSnapPct(s) {
    if (s.classList.contains("vnp-half")) return SNAP_PCT.half;
    if (s.classList.contains("vnp-peek")) return SNAP_PCT.peek;
    return SNAP_PCT.full;
  }

  function _cycleSnap() {
    var s = _sheet();
    var cur = s.classList.contains("vnp-half") ? "half" : s.classList.contains("vnp-peek") ? "peek" : "full";
    _snapClass(SNAP_ORDER[(SNAP_ORDER.indexOf(cur) + 1) % SNAP_ORDER.length]);
  }

  // Drag to resize between snap points; flick/drag past the bottom to dismiss.
  // The drag HANDLE is the whole header (grabber + title), not just the tiny
  // grabber bar — so a downward swipe from the top of the sheet always grabs.
  // The action buttons (back/refresh/done) are excluded so taps still work.
  function _installDrag(s) {
    var handle = s.querySelector(".vnp-sheet-head");
    if (!handle) return;
    var dragging = false, startY = 0, base = 0, h = 1, curPct = 0, moved = false;
    function pt(e) { return e.touches && e.touches[0] ? e.touches[0].clientY : (e.changedTouches && e.changedTouches[0] ? e.changedTouches[0].clientY : e.clientY); }
    function onMove(e) {
      if (!dragging) return;
      curPct = Math.max(0, base + ((pt(e) - startY) / h) * 100);
      if (Math.abs(curPct - base) > 0.5) moved = true;
      s.style.transform = "translateY(" + curPct + "%)";
      if (e.cancelable) e.preventDefault();   // keep the gesture off the chat behind
    }
    function onUp() {
      if (!dragging) return;
      dragging = false;
      s.style.transition = ""; s.style.transform = "";
      document.removeEventListener("touchmove", onMove);
      document.removeEventListener("mousemove", onMove);
      document.removeEventListener("touchend", onUp);
      document.removeEventListener("mouseup", onUp);
      if (!moved) return;                       // a tap, not a drag — leave as-is
      if (curPct > 88) { closeSheet(); return; }
      var near = SNAP_ORDER.reduce(function (a, b) {
        return Math.abs(SNAP_PCT[b] - curPct) < Math.abs(SNAP_PCT[a] - curPct) ? b : a;
      });
      _snapClass(near);
    }
    function onDown(e) {
      // Don't hijack taps on the header's action buttons.
      if (e.target && e.target.closest && e.target.closest(".vnp-act, .vnp-back")) return;
      dragging = true; moved = false;
      startY = pt(e);
      h = s.getBoundingClientRect().height || 1;
      base = _curSnapPct(s);
      curPct = base;
      s.style.transition = "none";
      document.addEventListener("touchmove", onMove, { passive: false });
      document.addEventListener("mousemove", onMove);
      document.addEventListener("touchend", onUp);
      document.addEventListener("mouseup", onUp);
    }
    handle.addEventListener("touchstart", onDown, { passive: true });
    handle.addEventListener("mousedown", onDown);
  }

  function _show(mode) {
    var s = _sheet();
    var back = document.getElementById("vnp-sheet-backdrop");
    s.classList.toggle("vnp-gridmode", mode === "grid");
    s.classList.add("vnp-full");
    requestAnimationFrame(function () { back.classList.add("show"); s.classList.add("show"); });
  }

  function closeSheet() {
    var s = document.getElementById("vnp-sheet");
    var back = document.getElementById("vnp-sheet-backdrop");
    if (s) { s.classList.remove("show"); s.classList.remove("vnp-fs"); }
    if (back) back.classList.remove("show");
  }

  function openGrid(sid) {
    var s = _sheet();
    s._sid = sid; s._pid = null;
    s.classList.remove("vnp-fs");   // grid is never fullscreen
    _hasNew[sid] = false; updateNavButton(sid);
    document.getElementById("vnp-sheet-title").textContent = "Previews · this session";
    var items = list(sid);
    document.getElementById("vnp-sheet-sub").textContent =
      items.length + (items.length === 1 ? " render" : " renders") + " · sorted by last opened";
    var grid = document.getElementById("vnp-grid");
    grid.innerHTML = items.map(function (p) {
      var t = TYPE_META[p.type] || TYPE_META.browser;
      return '<div class="vnp-tile" data-pid="' + esc(p.id) + '">' +
               '<div class="vnp-tile-thumb"><span class="vnp-type vnp-type-' + p.type + ' vnp-tag">' +
                 t.glyph + " " + t.label + '</span>' + _miniHTML(p) + '</div>' +
               '<div class="vnp-tile-cap"><div class="vnp-tile-ttl">' + esc(p.name) + '</div>' +
                 '<div class="vnp-tile-sub">' + (p.lastOpened != null ? "opened" : "new") + '</div></div>' +
             '</div>';
    }).join("") || '<div class="vnp-empty">No previews yet.</div>';
    grid.querySelectorAll(".vnp-tile").forEach(function (el) {
      el.addEventListener("click", function () { openPreview(sid, el.getAttribute("data-pid")); });
    });
    _show("grid");
  }

  function openPreview(sid, pid) {
    var st = _stores[sid];
    var p = st && st.byId[pid];
    if (!p) return;
    p.lastOpened = ++_openSeq;   // opening bumps sort order
    _hasNew[sid] = false; updateNavButton(sid);
    var s = _sheet();
    s._sid = sid; s._pid = pid;
    // Title is JUST the preview name — no type chip, no URL sub-line. The card
    // in chat + the grid tile both show the type; once you're staring at the
    // preview, another chip labeling it "Image" is pure noise. The URL row is
    // hidden in single mode via CSS (it's meaningless for asset:// screenshots
    // and cramped for real URLs).
    document.getElementById("vnp-sheet-title").textContent = p.name;
    document.getElementById("vnp-sheet-sub").textContent = p.src;
    _renderStage(p);
    _show("single");
  }

  // A phone on the tailnet can't reach the dev machine's localhost. Route those
  // URLs through the server-side proxy (same tailnet origin) so the iframe loads.
  function _liveSrc(src) {
    if (/^https?:\/\/(localhost|127\.0\.0\.1|0\.0\.0\.0|\[::1\])(:\d+)?(\/|$)/i.test(src)) {
      return "/api/preview/proxy?u=" + encodeURIComponent(src);
    }
    return src;
  }

  // Cache-bust so ↻ actually re-fetches a freshly-rendered screenshot.
  function _bust(url, n) {
    return url + (url.indexOf("?") === -1 ? "?" : "&") + "_r=" + (n || 0);
  }

  function _renderStage(p, bust) {
    var stage = document.getElementById("vnp-stage");
    if (!stage) return;
    if (p.type === "image") {
      var isrc = bust ? _bust(p.src, bust) : p.src;
      stage.innerHTML = '<div class="vnp-frame"><img class="vnp-img" alt="" src="' + esc(isrc) + '"></div>';
    } else {
      var lsrc = _liveSrc(p.src);
      if (bust) lsrc = _bust(lsrc, bust);
      stage.innerHTML = '<div class="vnp-frame vnp-browser">' +
        '<div class="vnp-urlbar"><span class="vnp-u-dots"><i></i><i></i><i></i></span>' +
        '<span class="vnp-u">' + esc(p.src) + '</span></div>' +
        '<iframe class="vnp-iframe" src="' + esc(lsrc) + '" ' +
        'sandbox="allow-scripts allow-forms allow-same-origin allow-popups" referrerpolicy="no-referrer"></iframe>' +
        '</div>';
    }
  }

  window.MobilePreview = {
    extractForRender: extractForRender,
    appendCards: appendCards,
    updateNavButton: updateNavButton,
    onSessionActivated: onSessionActivated,
    openGrid: openGrid,
    openPreview: openPreview,
    list: list,
    count: count,
  };

  if (document.readyState !== "loading") ensureNavButton();
  else document.addEventListener("DOMContentLoaded", ensureNavButton);
})();
