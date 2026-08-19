/**
 * Embed mode — run VibeNode inside another app, pinned to a single project.
 *
 * Adding `?embed=1&project=<encoded>` to the URL turns VibeNode into a
 * sessions-only panel for one project: the project switcher, the view switcher
 * and the git publish controls are hidden, and the view is locked to Sessions.
 * The host app gets a chat surface without the surrounding navigation, which is
 * chrome it already provides itself.
 *
 * Without `?embed`, this file does nothing at all. Normal VibeNode is untouched.
 *
 * THE IMPORTANT PART — why this does not just call /api/set-project
 * ----------------------------------------------------------------
 * VibeNode tracks the current project in `localStorage.activeProject`, and
 * localStorage is shared by every tab on the origin. An embed that simply wrote
 * its project there would silently repoint the user's *main* VibeNode window to
 * whatever the host app happened to be showing. Same for `/api/set-project`,
 * which mutates server-side global state for every client at once.
 *
 * So embed mode never writes either one. It shims `localStorage` so reads of
 * `activeProject` return the pinned project while writes to it are dropped.
 * Every existing caller — and there are a dozen or more across app.js,
 * compose.js and friends — keeps working unmodified, and the host window's own
 * project is left exactly where the user put it. The server already supports
 * this: `/api/sessions` and its neighbours accept `?project=<encoded>` per
 * request precisely so the global does not have to move.
 *
 * Params
 * ------
 *   embed=1                 required; enables the mode
 *   project=<encoded|path>  optional; pin to this project. Accepts either
 *                           VibeNode's encoded form ("C--Users-me-code-Thing")
 *                           or a plain path ("C:/Users/me/code/Thing").
 *   view=<mode>             optional; defaults to "sessions".
 *   chrome=1                optional; keep the hidden controls visible, for
 *                           debugging an embed without losing navigation.
 */
(function () {
  'use strict';

  var params = new URLSearchParams(location.search);
  if (!params.has('embed')) return;          // no-op for normal usage

  var raw = (params.get('project') || '').trim();
  var view = (params.get('view') || 'sessions').trim();
  var keepChrome = params.get('chrome') === '1';

  /**
   * Encode a filesystem path the way Claude Code names project folders:
   * "C:/Users/me/code/Thing" -> "C--Users-me-code-Thing".
   *
   * Mirrors _encode_cwd() in app/config.py — separators, colons, underscores
   * and dots all become dashes. The underscore and dot rules are easy to miss
   * and fail silently: the pinned project simply never matches a directory and
   * the panel looks empty for no visible reason. Keep the two in agreement.
   *
   * A value with no path separators is assumed to be already encoded and is
   * passed through, so a host app can supply either form.
   */
  function encodeProject(value) {
    if (!value) return '';
    if (value.indexOf('/') === -1 && value.indexOf('\\') === -1 &&
        value.indexOf(':') === -1) {
      return value;                          // already encoded
    }
    return value
      .replace(/\\/g, '-')
      .replace(/\//g, '-')
      .replace(/:/g, '-')
      .replace(/_/g, '-')
      .replace(/\./g, '-');
  }

  var project = encodeProject(raw);

  // Keys an embed must never persist, because they belong to whichever window
  // the user is driving directly, not to a panel inside someone else's app.
  function isHostOwnedKey(key) {
    return key === 'activeProject' ||
           key === 'viewMode' ||
           key.indexOf('projectView_') === 0;
  }

  if (project) {
    // Define the overrides as OWN properties of localStorage rather than
    // patching Storage.prototype. Two reasons:
    //   1. It works whether the real methods live on the prototype (browsers)
    //      or directly on the object (some polyfills and test doubles). A
    //      prototype patch silently does nothing in the latter case, which
    //      would drop the isolation guarantee without any visible error.
    //   2. It leaves sessionStorage and any other Storage instance alone.
    // defineProperty is used instead of plain assignment because Storage has a
    // named-property setter -- `localStorage.getItem = fn` can store an item
    // under the key "getItem" instead of shadowing the method.
    var realGet = localStorage.getItem.bind(localStorage);
    var realSet = localStorage.setItem.bind(localStorage);

    Object.defineProperty(localStorage, 'getItem', {
      configurable: true,
      writable: true,
      value: function (key) {
        if (key === 'activeProject') return project;
        // Pin the per-project view too, so a stale saved view cannot pull the
        // embed onto kanban or compose on load.
        if (key === 'projectView_' + project) return view;
        return realGet(key);
      }
    });

    Object.defineProperty(localStorage, 'setItem', {
      configurable: true,
      writable: true,
      value: function (key, value) {
        if (isHostOwnedKey(key)) return;        // dropped, never persisted
        return realSet(key, value);
      }
    });
  }

  // Published so other scripts (and the host app) can detect the mode.
  window.VN_EMBED = { enabled: true, project: project, view: view };

  function applyEmbedChrome() {
    document.body.classList.add('vn-embed');
    if (keepChrome) document.body.classList.add('vn-embed-chrome');

    // Neutralise the project picker even if something reaches it another way
    // (keyboard shortcut, stray call). Failing closed beats a half-hidden UI
    // that still lets the embed wander off its project.
    if (typeof window.openProjectOverlay === 'function') {
      window.openProjectOverlay = function () { /* disabled in embed mode */ };
    }
  }

  /**
   * Lock the view.
   *
   * setViewMode is wrapped rather than replaced so the real implementation
   * still runs — the panel needs Sessions to actually render. mobile.js already
   * wraps it the same way, so the pattern composes.
   */
  function lockView() {
    if (typeof window.setViewMode !== 'function' || window.setViewMode._vnEmbedWrapped) {
      return false;
    }
    var original = window.setViewMode;
    window.setViewMode = function (mode) {
      return original.apply(this, [view]);    // every request resolves to `view`
    };
    window.setViewMode._vnEmbedWrapped = true;
    try {
      original.call(window, view);
    } catch (e) {
      // The app may not be ready on the first attempt; the poll below retries.
      return false;
    }
    return true;
  }

  function start() {
    applyEmbedChrome();
    if (lockView()) return;
    // app.js sets its own view during boot, so keep trying briefly until the
    // function exists and the lock sticks.
    var tries = 0;
    var timer = setInterval(function () {
      if (lockView() || ++tries > 40) clearInterval(timer);
    }, 100);
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', start);
  } else {
    start();
  }
})();
