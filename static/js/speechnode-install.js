/* speechnode-install.js — VibeNode-owned install/enable orchestration for SpeechNode.
 *
 * WHY THIS EXISTS
 * ---------------
 * The voice engine itself lives in the external `speechnode` package (served at
 * /speechnode/speechnode.js as `window.SpeechNode`). That package ships its own
 * enable/install modal, but its orchestration had three first-run UX bugs:
 *
 *   1. Resume gap — after a web-server restart + auto page-reload, the browser
 *      comes back ENABLED but with the engine phase still `idle` and deps not yet
 *      installed. The package's _boot() only resumes when the phase is already
 *      installing/downloading/loading OR deps are importable, so this exact state
 *      matched neither branch and the install silently never restarted. That was
 *      the "dead screen, had to refresh manually" bug.
 *   2. Frozen-looking bar — the engine reports coarse, jumpy progress while the two
 *      slow steps (faster-whisper wheel build, ~150 MB model download) sit at a
 *      single static percentage for minutes. No motion reads as frozen.
 *   3. No success — completion was surfaced only by a toast fired from the polling
 *      loop; when the page reloaded (bug #1) that loop died and nothing confirmed
 *      success or lit the mic.
 *
 * We can't edit the pip-installed package from VibeNode, so VibeNode OWNS the
 * orchestration here and delegates the real work (install / status / transcribe)
 * to the package's stable public API. This file changes no backend contract and
 * leaves voice.js (which consumes window.SpeechNode.transcribeBlob/isReady)
 * untouched. If the package failed to load, every entry point degrades gracefully
 * and voice falls back to the browser's Web Speech API.
 */
(function () {
  'use strict';

  /** The package client, or undefined if /speechnode/speechnode.js didn't load. */
  function SN() { return window.SpeechNode; }

  let _polling = false;       // a poll loop is currently running (single-flight)
  let _resumed = false;       // resumeOnLoad() already ran this page-load
  let _shown = false;         // OUR overlay is currently visible
  let _styleInjected = false;

  // ---- scoped styles (injected from JS; never touches style.css) ----------
  // The progress track carries a continuous sheen so the bar reads as "working"
  // even while the backend percentage is static during the long install steps.
  function _injectStyle() {
    if (_styleInjected) return;
    _styleInjected = true;
    const css = [
      '#pm-overlay .vsn-progress{position:relative;height:8px;border-radius:999px;',
      'background:var(--bg-tertiary,rgba(255,255,255,.08));overflow:hidden;margin:16px 0 10px}',
      '#pm-overlay .vsn-bar{height:100%;border-radius:999px;background:var(--accent,#3b82f6);',
      'width:5%;transition:width .6s ease}',
      '#pm-overlay .vsn-progress::after{content:"";position:absolute;inset:0;border-radius:999px;',
      'background:linear-gradient(90deg,transparent,rgba(255,255,255,.35),transparent);',
      'transform:translateX(-100%);animation:vsnSheen 1.25s ease-in-out infinite}',
      '@keyframes vsnSheen{100%{transform:translateX(100%)}}',
      '#pm-overlay .vsn-sub{font-size:12px;color:var(--text-muted);margin:8px 0 0;line-height:1.5}',
      '#pm-overlay .vsn-ok{color:var(--success,#3fb950);font-weight:600;font-size:15px;margin-bottom:6px}',
      '#pm-overlay .vsn-err{color:var(--result-err,#f85149);font-weight:600;white-space:pre-wrap}',
      '#pm-overlay .vsn-rem{font-size:12px;white-space:pre-wrap;background:var(--bg-tertiary,rgba(255,255,255,.06));',
      'padding:10px;border-radius:6px;max-height:180px;overflow:auto;margin-top:10px}',
    ].join('');
    const el = document.createElement('style');
    el.id = 'vsn-style';
    el.textContent = css;
    document.head.appendChild(el);
  }

  function _esc(str) {
    return String(str == null ? '' : str)
      .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
  }

  function _toast(msg, isErr) {
    if (typeof showToast === 'function') { try { showToast(msg, !!isErr); } catch (e) { /* ignore */ } }
  }

  // Light up the mic NOW (no page refresh). Mirrors the bar-refresh pattern used
  // across voice.js / app.js: the bar skips re-render on unchanged session state,
  // so null the guard first, then re-render on the next tick.
  function _refreshBar() {
    if (typeof updateLiveInputBar !== 'function') return;
    try { if (typeof liveBarState !== 'undefined') liveBarState = null; } catch (e) { /* ignore */ }
    setTimeout(updateLiveInputBar, 0);
  }

  // ---- overlay (reuses VibeNode's native pm-overlay / pm-card classes) -----
  function _overlay() {
    let o = document.getElementById('pm-overlay');
    if (!o) {
      o = document.createElement('div');
      o.id = 'pm-overlay';
      o.className = 'pm-overlay';
      document.body.appendChild(o);
    }
    return o;
  }
  function _close() {
    _shown = false;
    if (typeof _closePm === 'function') { _closePm(); return; }
    const o = document.getElementById('pm-overlay');
    if (o) o.classList.remove('show');
  }

  // Build the card shell once; later renders only swap the inner body so the modal
  // never re-flashes its open animation.
  function _ensureShown() {
    _injectStyle();
    const o = _overlay();
    let card = o.querySelector('.vsn-card');
    if (!card) {
      o.innerHTML = '<div class="pm-card vsn-card pm-enter" style="width:440px;">'
        + '<h2 class="pm-title">SpeechNode</h2><div class="vsn-body"></div></div>';
      o.classList.add('show');
      card = o.querySelector('.vsn-card');
      requestAnimationFrame(() => card.classList.remove('pm-enter'));
      o.onclick = (e) => { if (e.target === o) _close(); };
    } else {
      o.classList.add('show');
    }
    _shown = true;
    return card;
  }

  function _setBody(html) {
    const card = _ensureShown();
    card.querySelector('.vsn-body').innerHTML = html;
    _bindButtons();
  }

  function _bindButtons() {
    const o = document.getElementById('pm-overlay');
    if (!o) return;
    o.querySelectorAll('[data-vsn]').forEach((btn) => {
      btn.onclick = () => {
        const act = btn.dataset.vsn;
        if (act === 'close') { _close(); return; }
        if (act === 'retry') { enable(); return; }
        if (act === 'restart') {
          // Persist intent, then run VibeNode's web-only restart, which shows its
          // own reboot overlay and auto-reloads. On reload, resumeOnLoad() finishes.
          try { const sn = SN(); if (sn) sn.setEnabled(true); } catch (e) { /* ignore */ }
          if (typeof _doRestart === 'function') _doRestart('web');
          else if (typeof restartServer === 'function') restartServer();
          else window.location.reload();
        }
      };
    });
  }

  // ---- body renderers ------------------------------------------------------
  function _phaseCopy(s) {
    switch (s.phase) {
      case 'downloading': return s.message || 'Downloading the model (~150 MB, one time)…';
      case 'loading':     return s.message || 'Loading the model into memory…';
      case 'installing':  return s.message || 'Installing the SpeechNode engine…';
      default:            return s.message || 'Setting up SpeechNode…';
    }
  }
  function _renderProgress(s) {
    const pct = Math.max(4, Math.min(100, s.progress || 5));
    _setBody(
      '<p class="sn-msg">' + _esc(_phaseCopy(s)) + '</p>'
      + '<div class="vsn-progress"><div class="vsn-bar" style="width:' + pct + '%"></div></div>'
      + '<p class="vsn-sub">One-time setup, fully local — nothing leaves your machine. '
      + 'You can keep working or close this; we’ll finish in the background and switch the mic on automatically.</p>'
      + '<div class="pm-actions"><button class="pm-btn pm-btn-secondary" data-vsn="close">Close</button></div>'
    );
  }
  function _renderSuccess(s) {
    _setBody(
      '<div class="vsn-ok">✓ SpeechNode is ready</div>'
      + '<p class="sn-msg">Voice now works in every browser, knows your project’s vocabulary, '
      + 'and punctuates automatically. Model: <code>' + _esc(s.model || 'base.en') + '</code>.</p>'
      + '<div class="pm-actions"><button class="pm-btn pm-btn-primary" data-vsn="close">Done</button></div>'
    );
  }
  function _renderError(s) {
    _setBody(
      '<div class="vsn-err">⚠ ' + _esc(s.error || 'SpeechNode setup failed.') + '</div>'
      + (s.remediation ? '<pre class="vsn-rem">' + _esc(s.remediation) + '</pre>' : '')
      + '<div class="pm-actions">'
      + '<button class="pm-btn pm-btn-secondary" data-vsn="close">Close</button>'
      + '<button class="pm-btn pm-btn-primary" data-vsn="retry">Try again</button></div>'
    );
  }
  function _renderNeedsRestart() {
    _setBody(
      '<p class="sn-msg">SpeechNode’s engine was just installed and needs a '
      + '<strong>one-time restart of the web layer</strong> to switch on. Your running sessions '
      + 'stay alive — only the web server reloads, the page refreshes itself when it’s back, '
      + 'and setup then finishes automatically.</p>'
      + '<div class="pm-actions">'
      + '<button class="pm-btn pm-btn-secondary" data-vsn="close">Later</button>'
      + '<button class="pm-btn pm-btn-primary" data-vsn="restart">Restart &amp; finish setup</button></div>'
    );
  }
  function _renderUnsupported() {
    _setBody(
      '<p class="sn-msg">SpeechNode needs a modern browser with microphone recording '
      + '(<code>MediaRecorder</code>). Please update your browser — standard voice input still works.</p>'
      + '<div class="pm-actions"><button class="pm-btn pm-btn-secondary" data-vsn="close">Close</button></div>'
    );
  }

  // ---- poll loop -----------------------------------------------------------
  // visible:true  -> update the overlay each tick (interactive enable flow)
  // visible:false -> silent background finish (resume-on-load); surfaces a toast
  function _poll(opts) {
    opts = opts || {};
    if (_polling) return;
    const sn = SN();
    if (!sn) return;
    _polling = true;
    const tick = () => {
      sn.refreshStatus().then((s) => {
        s = s || {};
        if (s.ready || s.phase === 'ready') {
          _polling = false;
          _onReady(opts);
          return;
        }
        if (s.phase === 'error') {
          _polling = false;
          if (opts.visible) _renderError(s);
          else _toast(s.error || 'SpeechNode setup failed.', true);
          return;
        }
        if (opts.visible && _shown) _renderProgress(s);
        setTimeout(tick, 1200);
      }).catch(() => setTimeout(tick, 1500));
    };
    setTimeout(tick, 600);
  }

  function _onReady(opts) {
    _refreshBar();
    const s = (SN() && SN().getStatus()) || {};
    if (_shown) _renderSuccess(s);
    else if (!opts.silent) _toast('SpeechNode is ready — voice is on.');
    // silent=true: auto-resume after restart — bar update is the only signal needed;
    // the package's _startPolling() may already fire its own "SpeechNode is ready." toast.
  }

  // ---- public entry points -------------------------------------------------
  // Interactive enable, from the Preferences toggle (or the package promo).
  async function enable() {
    const sn = SN();
    if (!sn) { _toast('SpeechNode is unavailable.', true); return; }
    if (typeof sn.isSupportedBrowser === 'function' && !sn.isSupportedBrowser()) {
      _renderUnsupported();
      return;
    }
    sn.setEnabled(true);
    _renderProgress({ phase: 'installing', message: 'Preparing SpeechNode…', progress: 3 });
    let s = {};
    try { s = (await sn.refreshStatus()) || {}; } catch (e) { s = {}; }
    if (s.phase === 'needs_restart') { _renderNeedsRestart(); return; }
    if (s.ready) { _onReady({ visible: true }); return; }
    try { await sn.startInstall(); } catch (e) { /* surfaced via poll/status */ }
    _renderProgress(sn.getStatus() || { phase: 'installing', progress: 5 });
    _poll({ visible: true });
  }

  // Runs on every page load. Ensures SpeechNode is ready after any restart.
  //
  // The package's own _boot() handles most cases but has two gaps we fill:
  //  • enabled + idle + no deps  — the original dead-state bug (_boot() never fires)
  //  • enabled + loading         — _boot() polls, but its _refreshBar() may not update
  //                                VibeNode's mic reliably. We run our own silent poll
  //                                so _refreshBar() fires from code we control.
  //
  // Server-side, app/__init__.py pre-warms the model on restart so it's often ready
  // by the time this runs. The poll is the fallback for when it isn't.
  function resumeOnLoad() {
    if (_resumed) return;
    _resumed = true;
    const sn = SN();
    if (!sn || !sn.isEnabled()) return;
    if (typeof sn.isSupportedBrowser === 'function' && !sn.isSupportedBrowser()) return;
    if (sn.isReady()) { _refreshBar(); return; }
    sn.refreshStatus().then((s) => {
      s = s || {};
      if (s.ready) { _refreshBar(); return; }
      if (s.phase === 'needs_restart') { _renderNeedsRestart(); return; }
      const inProgress = s.phase === 'installing' || s.phase === 'downloading' || s.phase === 'loading';
      if (inProgress || s.deps_available) {
        // Model is loading (or about to load via package _boot()). Run a silent poll
        // so we own the _refreshBar() call that lights the mic when it's done.
        // No toast — the package's _startPolling() fires "SpeechNode is ready." for us.
        _poll({ visible: false, silent: true });
        return;
      }
      // The gap the package misses: enabled but idle with no deps -> start and poll silently.
      sn.startInstall().then(() => _poll({ visible: false, silent: true })).catch(() => { /* ignore */ });
    }).catch(() => { /* offline / transient — package _boot may still recover */ });
  }

  // ---- wiring --------------------------------------------------------------
  function _init() {
    const sn = SN();
    // Route any window.SpeechNode.enable() callers through the VibeNode flow too.
    if (sn && typeof sn.enable === 'function') {
      try { sn.enable = enable; } catch (e) { /* ignore */ }
    }
    resumeOnLoad();
  }
  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', _init);
  } else {
    _init();
  }

  window.VibeSpeechInstall = { enable, resumeOnLoad };
})();
