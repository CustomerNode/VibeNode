/* live-assets.js — open tabs pick up a changed card without a reload (2026-10-28).
 *
 * The owner, after a session renamed the card's "Your move" heading and its
 * own card, drawn by the old code in his open tab, still said "Your move" and
 * told him to reload: "do you see how little sense that makes".  The page only
 * ever loaded a script once, so every change to the card waited for a reload
 * nobody asked for.
 *
 * Each watched asset is checked with a HEAD request (the unversioned static
 * URL is served no-cache with Last-Modified) when the tab comes back into
 * view, on focus, and every 30 s while visible.  When the file on disk is
 * newer than the copy this page loaded (the ?v= mtime from versioned_static),
 * the new copy is loaded and `after()` runs.  For the card:
 *   - last-card.css: the <link> gets the new ?v=;
 *   - last-card.js: the new script is added; it replaces window.VNLastCard and
 *     the old copy's document listener stands down (it checks that it is
 *     still the current one), then every drawn card is drawn again from the
 *     JSON it keeps in data-card.  A card whose reply was already sent keeps
 *     its state and is left alone.
 *
 * Only files that are safe to load twice are watched.  Do NOT add a file here
 * without making it hand over cleanly to a newer copy of itself.
 *
 * Files that can NOT be loaded twice (sessions.js declares top-level `let`s)
 * are watched with kind 'reload' (2026-10-28: the owner double-clicked a Done
 * session and it woke, because his tab still ran the double-click from before
 * the change).  When one is newer on disk the tab reloads itself the next
 * time it goes out of view, so he never sees it happen and comes back to the
 * new code.  It never reloads while he looks at the tab, while the chat box
 * holds unsent text, or during an in-app restart.
 */
(function () {
  'use strict';

  const CHECK_MS = 30000;
  const _watches = [];

  function _loadedVersion(sel, attr) {
    const el = document.querySelector(sel);
    const m = el && /[?&]v=(\d+)/.exec(el.getAttribute(attr) || '');
    return m ? Number(m[1]) : 0;
  }

  async function _diskVersion(path) {
    try {
      const r = await fetch('/static/' + path, { method: 'HEAD', cache: 'no-store' });
      const lm = r.ok ? r.headers.get('Last-Modified') : '';
      const t = lm ? Date.parse(lm) : NaN;
      return isNaN(t) ? 0 : Math.floor(t / 1000);
    } catch (_e) { return 0; }
  }

  function _addScript(path, v) {
    return new Promise((resolve, reject) => {
      const s = document.createElement('script');
      s.src = '/static/' + path + '?v=' + v;
      s.onload = resolve;
      s.onerror = reject;
      document.head.appendChild(s);
    });
  }

  /** Watch one file.  kind 'js' | 'css'; after() runs once the new copy is in. */
  function watch(path, kind, after) {
    const sel = kind === 'css' ? 'link[rel="stylesheet"][href*="' + path + '"]' : 'script[src*="' + path + '"]';
    const attr = kind === 'css' ? 'href' : 'src';
    _watches.push({ path, kind, sel, attr, after, have: _loadedVersion(sel, attr), busy: false });
  }

  let _stale = false;
  /** Reload a tab whose code is out of date, only while nobody is looking. */
  function _reloadIfStale() {
    if (!_stale || document.visibilityState !== 'hidden') return;
    const ta = document.getElementById('live-input-ta');
    if (ta && ta.value && ta.value.trim()) return;           // unsent text: keep it
    if (document.getElementById('restart-overlay')) return;  // the restart reloads it
    _stale = false;
    try { location.reload(); } catch (_e) { /* nothing else to do */ }
  }

  async function check() {
    if (document.visibilityState === 'hidden') return;
    for (const w of _watches) {
      if (w.busy || !w.have) continue;   // no ?v= on the page: nothing to compare with
      w.busy = true;
      try {
        const v = await _diskVersion(w.path);
        if (v && v > w.have) {
          if (w.kind === 'reload') {
            if (!_stale) console.info('[live-assets] ' + w.path + ' changed: this tab reloads the next time it is out of view');
            _stale = true;
            continue;
          }
          if (w.kind === 'css') {
            const link = document.querySelector(w.sel);
            if (link) link.setAttribute('href', '/static/' + w.path + '?v=' + v);
          } else {
            await _addScript(w.path, v);
          }
          w.have = v;
          console.info('[live-assets] loaded the new ' + w.path);
          if (w.after) { try { w.after(); } catch (e) { console.warn('[live-assets]', e); } }
        }
      } catch (e) {
        console.warn('[live-assets] could not load the new ' + w.path, e);
      } finally {
        w.busy = false;
      }
    }
  }

  /** Draw every card on the page again with the current VNLastCard. */
  function redrawCards() {
    if (!window.VNLastCard) return;
    const parents = new Set();
    document.querySelectorAll('.lm[data-card]').forEach(card => {
      if (card.querySelector('.lm-sent.on')) return;   // a reply was sent: keep its state
      const ph = document.createElement('div');
      ph.className = 'vn-card-ph';
      ph.setAttribute('data-card', card.getAttribute('data-card'));
      card.replaceWith(ph);
      if (ph.parentElement) parents.add(ph.parentElement);
    });
    parents.forEach(p => window.VNLastCard.renderIn(p));
  }

  watch('js/last-card.js', 'js', redrawCards);
  watch('css/last-card.css', 'css', null);
  // Can't be loaded twice: reload the tab when it is out of view.
  ['js/sessions.js', 'js/session-verb.js', 'js/session-verdict.js', 'js/socket.js', 'js/live-panel.js', 'js/app.js', 'js/toolbar.js',
   // The thread's scroll rules (the classic facade wraps it, so never twice).
   'js/thread-scroll.js', 'js/thread-scroll-classic.js',
   // The publish dialog: a tab open since before a fix still titled a clash
   // "Security Issue" with nothing to press (2026-10-29).
   'js/git-sync.js']
    .forEach(p => watch(p, 'reload', null));

  document.addEventListener('visibilitychange', () => {
    if (document.visibilityState === 'visible') check(); else _reloadIfStale();
  });
  window.addEventListener('focus', check);
  setInterval(check, CHECK_MS);

  window.VNLiveAssets = { check, watch, redrawCards, stale: () => _stale };
})();
