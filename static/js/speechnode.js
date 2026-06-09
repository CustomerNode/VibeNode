/* speechnode.js — VibeNode's opt-in, local, codebase-aware voice engine (client).
 *
 * Responsibilities:
 *   - Talk to /api/speechnode/{status,install,transcribe}
 *   - Drive the enable/install UX (progress + success/failure + remediation)
 *   - Expose helpers used by voice.js so the mic works in EVERY browser
 *     (Firefox/Safari included) whenever SpeechNode is ready — no lock-out.
 *
 * SpeechNode is OFF by default. The enabled flag lives in localStorage; the
 * model lives server-side. If anything fails, callers fall back to the existing
 * Web Speech path.
 */
(function () {
  'use strict';

  const BASE = '/api/speechnode';
  let _status = null;     // last /status payload
  let _polling = false;

  // ---- capability + preference -------------------------------------------
  function isSupportedBrowser() {
    return !!(navigator.mediaDevices && navigator.mediaDevices.getUserMedia && window.MediaRecorder);
  }
  function isEnabled() {
    try { return localStorage.getItem('speechNodeEnabled') === 'on'; } catch (e) { return false; }
  }
  function setEnabled(on) {
    try {
      if (on) localStorage.setItem('speechNodeEnabled', 'on');
      else localStorage.removeItem('speechNodeEnabled');
    } catch (e) { /* ignore */ }
  }
  /** Ready to transcribe right now: enabled by the user AND model loaded server-side. */
  function isReady() { return isEnabled() && !!(_status && _status.ready) && isSupportedBrowser(); }
  function getStatus() { return _status; }

  // ---- server calls -------------------------------------------------------
  // A 404 means the SpeechNode backend route isn't loaded yet — i.e. the server
  // was running before this code was added and needs a one-time restart. We model
  // that as a distinct phase so the UI can offer a one-click, auto-reloading fix.
  const _NEEDS_RESTART = { phase: 'needs_restart', deps_available: false, ready: false };

  async function refreshStatus() {
    try {
      const r = await fetch(BASE + '/status');
      if (r.status === 404) { _status = _NEEDS_RESTART; return _status; }
      _status = await r.json();
    } catch (e) { /* keep last */ }
    return _status;
  }
  async function startInstall() {
    try {
      const r = await fetch(BASE + '/install', {
        method: 'POST', headers: { 'Content-Type': 'application/json' }, body: '{}',
      });
      if (r.status === 404) { _status = _NEEDS_RESTART; return _status; }
      _status = await r.json();
    } catch (e) { /* surfaced via status */ }
    return _status;
  }
  /** Upload a recorded blob; returns {ok, text} or {ok:false, error}. */
  async function transcribeBlob(blob, opts) {
    opts = opts || {};
    const type = blob.type || '';
    const ext = type.includes('ogg') ? 'ogg' : type.includes('mp4') ? 'mp4'
              : type.includes('wav') ? 'wav' : 'webm';
    const fd = new FormData();
    fd.append('audio', blob, 'speech.' + ext);
    if (opts.cwd) fd.append('cwd', opts.cwd);
    if (opts.extra) fd.append('extra', opts.extra);
    if (opts.fast) fd.append('fast', '1');
    const r = await fetch(BASE + '/transcribe', { method: 'POST', body: fd });
    return await r.json();
  }

  // ---- enable / install modal --------------------------------------------
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
    if (typeof _closePm === 'function') { _closePm(); return; }
    const o = document.getElementById('pm-overlay');
    if (o) o.classList.remove('show');
  }

  function _render() {
    const o = _overlay();
    const s = _status || {};
    let body = '';

    if (!isSupportedBrowser()) {
      body = `<p class="sn-msg">SpeechNode needs a modern browser with microphone recording
        (<code>MediaRecorder</code>). Please update your browser.</p>
        <div class="pm-actions"><button class="pm-btn pm-btn-secondary" data-sn="close">Close</button></div>`;
    } else if (s.phase === 'needs_restart') {
      body = `<p class="sn-msg">SpeechNode's engine was just added and needs a <strong>one-time
        web-server restart</strong> to switch on. Your running sessions stay alive — only the web
        layer reloads, and <strong>the page refreshes itself</strong> when it's back. You don't
        need to do anything else.</p>
        <div class="pm-actions">
          <button class="pm-btn pm-btn-secondary" data-sn="close">Later</button>
          <button class="pm-btn pm-btn-primary" data-sn="restart">Restart &amp; finish setup</button>
        </div>`;
    } else if (s.phase === 'error') {
      body = `<div class="sn-status sn-error">⚠ ${_esc(s.error || 'Something went wrong.')}</div>
        ${s.remediation ? `<pre class="sn-remediation">${_esc(s.remediation)}</pre>` : ''}
        <div class="pm-actions">
          <button class="pm-btn pm-btn-secondary" data-sn="close">Close</button>
          <button class="pm-btn pm-btn-primary" data-sn="retry">Try Again</button>
        </div>`;
    } else if (isEnabled() && s.ready) {
      body = `<div class="sn-status sn-ok">✓ SpeechNode is enabled and ready.</div>
        <p class="sn-msg">Voice now works in every browser, knows your project's vocabulary,
        and punctuates automatically. Model: <code>${_esc(s.model || 'base.en')}</code>.</p>
        <div class="pm-actions">
          <button class="pm-btn pm-btn-danger" data-sn="disable">Disable</button>
          <button class="pm-btn pm-btn-primary" data-sn="close">Done</button>
        </div>`;
    } else if (s.phase === 'installing' || s.phase === 'downloading' || s.phase === 'loading') {
      const pct = Math.max(2, Math.min(100, s.progress || 5));
      body = `<p class="sn-msg">${_esc(s.message || 'Setting up SpeechNode…')}</p>
        <div class="sn-progress"><div class="sn-progress-bar" style="width:${pct}%"></div></div>
        <p class="sn-sub">This one-time setup downloads a small (~150&nbsp;MB) local model.
        You can keep working — we'll let you know when it's ready.</p>`;
    } else {
      // idle / not installed
      body = `<p class="sn-msg"><strong>SpeechNode</strong> is VibeNode's local voice engine —
        works in <strong>every browser</strong>, knows your codebase's vocabulary, punctuates
        automatically, and runs fully on your machine (no cloud, no per-use cost).</p>
        <p class="sn-sub">Enabling downloads a small (~150&nbsp;MB) model once. Everything stays local.</p>
        <div class="pm-actions">
          <button class="pm-btn pm-btn-secondary" data-sn="close">Not now</button>
          <button class="pm-btn pm-btn-primary" data-sn="enable">Enable SpeechNode</button>
        </div>`;
    }

    // Create the card shell + entrance animation ONCE. Later re-renders (install
    // polling, phase changes) only swap the inner body, so the modal never
    // re-flashes its open animation.
    let card = o.querySelector('.sn-card');
    if (!card) {
      o.innerHTML = `<div class="pm-card sn-card pm-enter" style="width:440px;">
        <h2 class="pm-title">SpeechNode</h2><div class="sn-body"></div></div>`;
      o.classList.add('show');
      card = o.querySelector('.sn-card');
      requestAnimationFrame(() => card.classList.remove('pm-enter'));
      o.onclick = (e) => { if (e.target === o) _close(); };
    }
    card.querySelector('.sn-body').innerHTML = body;

    o.querySelectorAll('[data-sn]').forEach((btn) => {
      btn.onclick = async () => {
        const act = btn.dataset.sn;
        if (act === 'close') { _close(); return; }
        if (act === 'restart') {
          // Remember the user's intent, then trigger the web-only restart, which
          // shows its own progress overlay and reloads the page automatically.
          setEnabled(true);
          if (typeof _doRestart === 'function') _doRestart('web');
          else if (typeof restartServer === 'function') restartServer();
          else window.location.reload();
          return;
        }
        if (act === 'enable' || act === 'retry') { enable(); return; }
        if (act === 'disable') {
          setEnabled(false);
          if (typeof showToast === 'function') showToast('SpeechNode disabled — using standard voice.');
          _close();
          _refreshBar();
          return;
        }
      };
    });
  }

  // Force the live input bar to actually re-render. It skips when the session
  // state key is unchanged — and toggling SpeechNode doesn't change that key, only
  // the voice engine's readiness — so we must null the guard to make the mic appear.
  function _refreshBar() {
    if (typeof updateLiveInputBar !== 'function') return;
    try { if (typeof liveBarState !== 'undefined') liveBarState = null; } catch (e) { /* ignore */ }
    setTimeout(updateLiveInputBar, 0);
  }

  // One-click enable: open the modal immediately and go straight into setup —
  // no second confirmation. Toggling SpeechNode on IS the decision.
  async function enable() {
    setEnabled(true);
    if (!_status || _status.phase === 'idle' || _status.phase === 'error') {
      _status = { phase: 'installing', message: 'Preparing SpeechNode…', progress: 3,
                  deps_available: false, ready: false };
    }
    _render();                       // show the modal instantly (animates once)
    await refreshStatus();
    const s = _status || {};
    if (s.phase !== 'needs_restart' && !s.ready) await startInstall();
    _render();                       // body-only update, no flash
    const s2 = _status || {};
    if (s2.phase === 'installing' || s2.phase === 'downloading' || s2.phase === 'loading') {
      _startPolling();
    } else {
      _refreshBar();   // already ready -> show the mic NOW, no reload
    }
  }

  function _startPolling() {
    if (_polling) return;
    _polling = true;
    const tick = async () => {
      await refreshStatus();
      const s = _status || {};
      const open = document.getElementById('pm-overlay');
      const showing = open && open.classList.contains('show') && open.querySelector('.pm-title');
      if (showing && (open.querySelector('.pm-title').textContent === 'SpeechNode')) _render();
      if (s.phase === 'ready' || s.phase === 'error') {
        _polling = false;
        if (s.phase === 'ready' && typeof showToast === 'function') showToast('SpeechNode is ready.');
        _refreshBar();
        return;
      }
      setTimeout(tick, 1500);
    };
    setTimeout(tick, 1200);
  }

  async function openInstallFlow() {
    await refreshStatus();
    _render();
    const s = _status || {};
    if (s.phase === 'installing' || s.phase === 'downloading' || s.phase === 'loading') _startPolling();
  }

  function _esc(str) {
    return String(str == null ? '' : str)
      .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
  }

  // ---- adoption nudge (tasteful, opt-out) --------------------------------
  // A gentle "try this new feature" prompt for users NOT on SpeechNode. Never
  // shown on the first session, snoozeable ("remind me tomorrow"), and fully
  // dismissable ("don't ask again"), with a multi-day cooldown so it never nags.
  function _promoState() {
    try { return JSON.parse(localStorage.getItem('speechNodePromo') || '{}'); } catch (e) { return {}; }
  }
  function _savePromo(s) {
    try { localStorage.setItem('speechNodePromo', JSON.stringify(s)); } catch (e) { /* ignore */ }
  }
  const _PROMO_COOLDOWN_MS = 3 * 24 * 3600 * 1000;  // ~3 days between un-snoozed shows

  async function _maybeShowPromo() {
    if (isEnabled() || !isSupportedBrowser()) return;
    const s = _promoState();
    if (s.dismissed) return;
    const now = Date.now();
    if (!s.firstSeen) { s.firstSeen = now; _savePromo(s); return; }      // let new users settle in
    if (s.remindAfter && now < s.remindAfter) return;                   // explicitly snoozed
    if (!s.remindAfter && s.lastShown && (now - s.lastShown) < _PROMO_COOLDOWN_MS) return;
    // Only nudge when this machine is actually a good fit — and we'll tell them why.
    let check;
    try {
      const r = await fetch(BASE + '/syscheck');
      if (!r.ok) return;                 // backend not ready / error -> don't nudge
      check = await r.json();
    } catch (e) { return; }
    if (!check || !check.recommended) return;
    setTimeout(() => {
      const o = document.getElementById('pm-overlay');
      if (o && o.classList.contains('show')) return;  // never interrupt an open dialog
      _renderPromo(check);
    }, 4000);
  }

  function _renderPromo(check) {
    const o = _overlay();
    const s = _promoState();
    s.lastShown = Date.now();
    _savePromo(s);
    const specs = [];
    if (check && check.free_gb != null) specs.push(check.free_gb + ' GB free');
    if (check && check.cpu_cores) specs.push(check.cpu_cores + ' CPU cores');
    const why = specs.length ? `We noticed this machine is a great fit — ${specs.join(' and ')}. ` : '';
    o.innerHTML = `<div class="pm-card pm-enter" style="width:440px;">
      <h2 class="pm-title">✨ Your machine looks great for SpeechNode</h2>
      <p class="sn-msg">${why}SpeechNode runs <strong>entirely on your machine</strong>, so a capable
      setup means fast, real-time dictation — that's why we're suggesting it here.</p>
      <p class="sn-sub">It's VibeNode's local voice engine: works in <strong>every browser</strong>,
      knows your project's vocabulary, and punctuates automatically. One click installs a small
      (~0.7&nbsp;GB) model — nothing ever leaves your machine.</p>
      <div class="pm-actions" style="flex-wrap:wrap;gap:8px;">
        <button class="pm-btn pm-btn-secondary" data-promo="never">Don't ask again</button>
        <button class="pm-btn pm-btn-secondary" data-promo="later">Remind me tomorrow</button>
        <button class="pm-btn pm-btn-primary" data-promo="try">Try it now</button>
      </div></div>`;
    o.classList.add('show');
    requestAnimationFrame(() => { const c = o.querySelector('.pm-card'); if (c) c.classList.remove('pm-enter'); });
    o.onclick = (e) => { if (e.target === o) _close(); };  // backdrop = soft dismiss (cooldown applies)
    o.querySelectorAll('[data-promo]').forEach((btn) => {
      btn.onclick = () => {
        const st = _promoState();
        const act = btn.dataset.promo;
        if (act === 'never') {
          st.dismissed = true; _savePromo(st); _close();
          if (typeof showToast === 'function') showToast("Okay — we won't ask again. (Enable anytime in Preferences.)");
        } else if (act === 'later') {
          st.remindAfter = Date.now() + 24 * 3600 * 1000; _savePromo(st); _close();
        } else if (act === 'try') {
          _close(); enable();
        }
      };
    });
  }

  // ---- boot ---------------------------------------------------------------
  // If the user has SpeechNode enabled from a previous session, refresh status
  // (and resume install if it was mid-flight) so the mic lights up when ready.
  function _boot() {
    if (!isEnabled()) { _maybeShowPromo(); return; }
    refreshStatus().then(() => {
      const s = _status || {};
      if (!s.ready && (s.phase === 'installing' || s.phase === 'downloading' || s.phase === 'loading')) {
        _startPolling();
      } else if (!s.ready && s.deps_available) {
        // deps present but model not loaded yet — trigger a load.
        startInstall().then(_startPolling);
      }
      _refreshBar();
    });
  }
  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', _boot);
  } else {
    _boot();
  }

  window.SpeechNode = {
    isSupportedBrowser, isEnabled, setEnabled, isReady, getStatus,
    refreshStatus, startInstall, transcribeBlob, openInstallFlow, enable,
  };
})();
