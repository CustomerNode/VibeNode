/* utils.js — shared helper functions and premium modal system */
// this line does absolutely nothing

const _IS_MAC = /Mac|iPod|iPhone|iPad/.test(navigator.platform || navigator.userAgent);
const _MOD = _IS_MAC ? '\u2318' : 'Ctrl';

var _pmCloseTimer = null;

function escHtml(str) {
  if (!str) return '';
  return String(str)
    .replace(/&/g,'&amp;')
    .replace(/</g,'&lt;')
    .replace(/>/g,'&gt;')
    .replace(/"/g,'&quot;');
}

// ---------------------------------------------------------------------------
// Floating notices: ONE place, clear of the chat composer
// ---------------------------------------------------------------------------
// Every bottom-of-screen notice lives in one stack on the right, just above
// the chat composer (or 20px off the corner when no composer is on screen):
//   - #git-sync-mini       "Pulling & pushing..." (clickable)
//   - .vn-undo-toast       "Session slept · Undo" (clickable)
//   - .compose-undo-toast  "Deleted · Undo" in Compose (clickable)
//   - #toast               plain notices
// They used to sit in three different corners at fixed offsets, and the
// bottom-right ones covered the composer's Send button; the clickable ones
// physically blocked it (reported 2026-10-05).  _layoutFloats() is the ONE
// writer of their `bottom`: it stacks whatever is showing, nearest-first in
// the order above, so two notices never overlap.  Anything that shows or
// hides one of them calls it.  If you add a new floating notice, add it to
// _floatEls() rather than giving it its own corner.
const _FLOAT_GAP = 8;
// Clearance between the bottom of the stack and the top of the composer.
// It MUST exceed the largest entry-animation travel of any float, because each
// one slides up into place from below its final `bottom` and is measurably
// lower for the length of that animation.  At 12px the git-sync indicator
// (translateY(16px)) and the compose undo toast (translateY(20px)) dipped over
// the paste / mic / Send buttons for ~350ms every time they appeared.  The
// travels are normalised to 10px in CSS; 14px keeps a positive gap through the
// whole animation.  If you raise a float's entry transform, raise this too.
const _FLOAT_CLEARANCE = 14;
let _floatLift = 20;

function _floatEls() {
  const out = [];
  const mini = document.getElementById('git-sync-mini');
  if (mini && mini.classList.contains('show')) out.push(mini);
  document.querySelectorAll('.vn-undo-toast, .compose-undo-toast').forEach(e => out.push(e));
  const t = document.getElementById('toast');
  if (t && t.classList.contains('show')) out.push(t);
  return out;
}

function _layoutFloats() {
  let y = _floatLift;
  _floatEls().forEach(e => {
    // A float whose CSS pins it to the TOP opts out with `--vn-float: top`
    // (the phone toast, mobile.css: a capsule under the header).  Writing
    // `bottom` on it as well gave it both edges, and it stretched from the
    // header down to the composer (2026-10-08).  It takes no slot in the stack.
    if (getComputedStyle(e).getPropertyValue('--vn-float').trim() === 'top') {
      e.style.bottom = '';
      return;
    }
    e.style.bottom = y + 'px';
    y += e.offsetHeight + _FLOAT_GAP;
  });
}

// The composer's height changes as you type, so a ResizeObserver keeps the
// lift current while it is on screen.
let _floatRO = null, _floatObserved = null;
function _updateFloatOffset() {
  const bar = document.getElementById('live-input-bar');
  let lift = 20;
  // The on-screen test is getClientRects(), NOT offsetParent.  offsetParent is
  // null for ANY position:fixed element in Blink and WebKit, and the phone
  // layout pins the composer with position:fixed (mobile.css .live-input-bar).
  // So on every phone the bar measured as "not on screen", the lift fell back
  // to 20px, and the whole stack landed squarely on the paste / mic / Send
  // buttons — the exact overlap this stack exists to prevent (reported
  // 2026-10-05, hours after the stack itself shipped).  Verified in Chromium:
  // offsetParent null, lift 20, all three buttons covered; with
  // getClientRects() the lift clears the bar and nothing intersects.
  // getClientRects() is empty only for a genuinely unrendered box
  // (display:none, detached), which is the one case where there is no composer
  // to clear.  Do NOT go back to offsetParent, :visible-style checks, or
  // `bar.offsetHeight` guards that read 0 under a hidden ancestor.
  if (bar && bar.getClientRects().length) {
    const r = bar.getBoundingClientRect();
    if (r.height > 0 && r.top < window.innerHeight) {
      lift = Math.max(20, Math.round(window.innerHeight - r.top + _FLOAT_CLEARANCE));
    }
  }
  _floatLift = lift;
  document.documentElement.style.setProperty('--vn-float-bottom', lift + 'px');
  _layoutFloats();
  if (bar !== _floatObserved && typeof ResizeObserver === 'function') {
    if (_floatRO) _floatRO.disconnect();
    _floatObserved = bar;
    if (bar) {
      if (!_floatRO) _floatRO = new ResizeObserver(() => _updateFloatOffset());
      _floatRO.observe(bar);
    }
  }
}
window.addEventListener('resize', _updateFloatOffset);

function showToast(msg, isError=false) {
  const t = document.getElementById('toast');
  t.textContent = msg;
  t.className = 'toast show' + (isError ? ' error' : '');
  _updateFloatOffset();                 // measure + place with the new text
  setTimeout(() => { t.classList.remove('show'); }, 3000);
}

// ---------------------------------------------------------------------------
// Send Behavior Preference — 'ctrl-enter' (default) or 'enter'
// ---------------------------------------------------------------------------
let sendBehavior = localStorage.getItem('sendBehavior') || 'ctrl-enter';

// Mobile phones: Enter is the natural "new line" key on a virtual keyboard,
// and modifier combos (Shift/Ctrl/Alt+Enter) are impractical on touch. Always
// treat Enter as new-line — the visible send button next to the mic handles
// submission. Match the ≤768px breakpoint used everywhere else (mobile.js).
const _MOBILE_MQ = (typeof window !== 'undefined' && window.matchMedia)
  ? window.matchMedia('(max-width: 768px)')
  : { matches: false };
function _isMobileViewport() { return !!_MOBILE_MQ.matches; }

// ---------------------------------------------------------------------------
// iOS "keyboard-dismiss = send" detection
// ---------------------------------------------------------------------------
// Tapping the iPhone keyboard's Done / dismiss-chevron (the button that feels
// like a "send/confirm check") fires a blur on the focused textarea — but so
// does tapping anywhere else on the page.  We need to distinguish the two so
// only the keyboard-dismiss case auto-sends; tapping away should NOT commit
// a half-typed message.
//
// Trick: the keyboard is system UI, so tapping its Done chevron does NOT
// generate any pointerdown/touchstart event on the document.  Any page-tap
// blur, in contrast, is preceded by a pointerdown within a few ms.  We stamp
// the last pointer time globally (capture-phase, so we see it before any
// stopPropagation), and the blur handler asks "was there a page tap recently?"
// If not — keyboard dismissed itself → treat as send.
let _lastPagePointerTs = 0;
if (typeof document !== 'undefined') {
  const _stamp = () => { _lastPagePointerTs = Date.now(); };
  // pointerdown covers iOS 13+ Safari; touchstart is the pre-pointer-events
  // fallback and is harmless on modern browsers (both fire, stamp is idempotent).
  document.addEventListener('pointerdown', _stamp, { capture: true, passive: true });
  document.addEventListener('touchstart',  _stamp, { capture: true, passive: true });
  document.addEventListener('mousedown',   _stamp, { capture: true, passive: true });
}

/**
 * Returns true if a just-fired blur on a composer textarea was caused by the
 * user tapping the iOS keyboard's own dismiss button (Done / chevron-down),
 * as opposed to tapping somewhere else on the page. Desktop always returns
 * false — no virtual keyboard, no "dismiss" gesture to interpret as send.
 *
 * Call from an inline `onblur=""` on the textarea. Requires nothing else.
 */
function _wasKeyboardDismissBlur() {
  if (!_isMobileViewport()) return false;
  // 250ms window: real page taps land well under 50ms before the blur; the
  // buffer absorbs any browser scheduling jitter without opening the door to
  // "user tapped a second ago, then keyboard closed on its own" false-sends.
  return (Date.now() - _lastPagePointerTs) > 250;
}

/** Returns true if the keyboard event should trigger a send based on preference */
function _shouldSend(e) {
  if (e.key !== 'Enter') return false;
  if (_isMobileViewport()) return false;   // mobile: Enter is always new-line, use the send button
  if (sendBehavior === 'enter') return !e.shiftKey && !e.ctrlKey && !e.altKey && !e.metaKey;
  return e.ctrlKey || e.shiftKey || e.altKey || e.metaKey;
}

// ---------------------------------------------------------------------------
// Auto-resize textarea — grows with content up to CSS max-height, then scrolls
// ---------------------------------------------------------------------------
const _TEXTAREA_MAX_PX = 300; // must match .live-textarea max-height in CSS

/** Resize a textarea to fit its content (up to max-height), then overflow-scroll */
function _autoResizeTextarea(ta) {
  if (!ta) return;
  ta.style.height = 'auto';                       // shrink to content first
  const scrollH = ta.scrollHeight;
  ta.style.height = Math.min(scrollH, _TEXTAREA_MAX_PX) + 'px';
  ta.style.overflowY = scrollH > _TEXTAREA_MAX_PX ? 'auto' : 'hidden';
}

/** Reset a textarea back to its default collapsed height */
function _resetTextareaHeight(ta) {
  if (!ta) return;
  ta.style.height = '';
  ta.style.overflowY = '';
}

/** Attach auto-resize listener to a textarea (safe to call multiple times) */
function _initAutoResize(ta) {
  if (!ta || ta._autoResizeBound) return;
  ta._autoResizeBound = true;
  ta.style.overflowY = 'hidden';                  // start with no scrollbar
  ta.addEventListener('input', () => _autoResizeTextarea(ta));
  // If textarea already has content (e.g. prefilled), resize immediately
  if (ta.value) _autoResizeTextarea(ta);
}

/** Returns HTML for the current send hint + toggle button */
function _sendHint() {
  // On mobile the Enter key always inserts a newline; sending is done via the
  // send button that appears next to the mic. The keyboard-shortcut toggle
  // below is desktop-only, so suppress the hint (and the toggle) on phones.
  if (_isMobileViewport()) return '';
  const full = sendBehavior === 'enter' ? `Enter to send · ${_MOD}+Enter, Shift+Enter, or Alt+Enter for new line` : `${_MOD}+Enter, Shift+Enter, or Alt+Enter to send`;
  // The bar shows the short form; the full list of shortcuts is the tooltip.
  const text = '<span title="' + full + '">' + (sendBehavior === 'enter' ? 'Enter to send' : `${_MOD}+Enter to send`) + '</span>';
  return text + '<span class="send-hint-btn" onclick="_toggleSendBehavior(event)" title="Change send shortcut">'
    + '<svg width="10" height="10" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round">'
    + '<polyline points="17 1 21 5 17 9"/><path d="M3 11V9a4 4 0 0 1 4-4h14"/>'
    + '<polyline points="7 23 3 19 7 15"/><path d="M21 13v2a4 4 0 0 1-4 4H3"/>'
    + '</svg></span>';
}

/** Toggle send behavior between ctrl-enter and enter */
function _toggleSendBehavior(e) {
  if (e) e.stopPropagation();
  sendBehavior = sendBehavior === 'enter' ? 'ctrl-enter' : 'enter';
  localStorage.setItem('sendBehavior', sendBehavior);
  // Persist to server so it survives browser/localStorage resets
  if (typeof socket !== 'undefined' && socket.connected) {
    socket.emit('set_ui_prefs', { sendBehavior: sendBehavior });
  }
  _refreshSendHints();
  showToast('Send: ' + (sendBehavior === 'enter' ? 'Enter to send' : _MOD + '+Enter to send'));
}

/** Refresh all visible send-hint labels */
function _refreshSendHints() {
  document.querySelectorAll('.send-hint').forEach(el => {
    el.innerHTML = _sendHint();
  });
}

// ---------------------------------------------------------------------------
// Premium Modal System — replaces browser confirm/alert/prompt
// ---------------------------------------------------------------------------

/**
 * Show a premium alert modal. Returns a Promise that resolves when dismissed.
 * @param {string} title - Modal title
 * @param {string} message - Body text (supports HTML)
 * @param {object} opts - Optional: { icon, buttonText }
 */
function showAlert(title, message, opts = {}) {
  return new Promise(resolve => {
    if (_pmCloseTimer) { clearTimeout(_pmCloseTimer); _pmCloseTimer = null; }
    const overlay = document.getElementById('pm-overlay');
    const icon = opts.icon || '';
    const btnText = opts.buttonText || 'OK';

    overlay.innerHTML = `
      <div class="pm-card pm-enter">
        ${icon ? '<div class="pm-icon">' + icon + '</div>' : ''}
        <h2 class="pm-title">${escHtml(title)}</h2>
        <div class="pm-body">${message}</div>
        <div class="pm-actions">
          <button class="pm-btn pm-btn-primary" id="pm-ok">${escHtml(btnText)}</button>
        </div>
      </div>`;
    overlay.classList.add('show');
    requestAnimationFrame(() => overlay.querySelector('.pm-card').classList.remove('pm-enter'));

    const close = () => { _closePm(); resolve(); };
    document.getElementById('pm-ok').onclick = close;
    overlay.onclick = e => { if (e.target === overlay) close(); };
    document.getElementById('pm-ok').focus();
  });
}

/**
 * Show a premium confirm modal. Returns a Promise<boolean>.
 * @param {string} title - Modal title
 * @param {string} message - Body text (supports HTML)
 * @param {object} opts - Optional: { icon, confirmText, cancelText, danger }
 */
function showConfirm(title, message, opts = {}) {
  return new Promise(resolve => {
    if (_pmCloseTimer) { clearTimeout(_pmCloseTimer); _pmCloseTimer = null; }
    const overlay = document.getElementById('pm-overlay');
    const icon = opts.icon || '';
    const confirmText = opts.confirmText || 'Confirm';
    const cancelText = opts.cancelText || 'Cancel';
    const dangerClass = opts.danger ? ' pm-btn-danger' : ' pm-btn-primary';

    overlay.innerHTML = `
      <div class="pm-card pm-enter">
        ${icon ? '<div class="pm-icon">' + icon + '</div>' : ''}
        <h2 class="pm-title">${escHtml(title)}</h2>
        <div class="pm-body">${message}</div>
        <div class="pm-actions">
          <button class="pm-btn pm-btn-secondary" id="pm-cancel">${escHtml(cancelText)}</button>
          <button class="pm-btn${dangerClass}" id="pm-confirm">${escHtml(confirmText)}</button>
        </div>
      </div>`;
    overlay.classList.add('show');
    requestAnimationFrame(() => overlay.querySelector('.pm-card').classList.remove('pm-enter'));

    const close = (val) => { _closePm(); resolve(val); };
    document.getElementById('pm-confirm').onclick = () => close(true);
    document.getElementById('pm-cancel').onclick = () => close(false);
    overlay.onclick = e => { if (e.target === overlay) close(false); };
    document.getElementById('pm-confirm').focus();
  });
}

/**
 * Show a premium prompt modal. Returns a Promise<string|null>.
 * @param {string} title - Modal title
 * @param {string} message - Body text
 * @param {object} opts - Optional: { icon, placeholder, value, confirmText, cancelText }
 */
function showPrompt(title, message, opts = {}) {
  return new Promise(resolve => {
    if (_pmCloseTimer) { clearTimeout(_pmCloseTimer); _pmCloseTimer = null; }
    const overlay = document.getElementById('pm-overlay');
    const icon = opts.icon || '';
    const placeholder = opts.placeholder || '';
    const value = opts.value || '';
    const confirmText = opts.confirmText || 'OK';
    const cancelText = opts.cancelText || 'Cancel';

    overlay.innerHTML = `
      <div class="pm-card pm-enter">
        ${icon ? '<div class="pm-icon">' + icon + '</div>' : ''}
        <h2 class="pm-title">${escHtml(title)}</h2>
        <div class="pm-body">${message}</div>
        <input class="pm-input" id="pm-input" type="text"
               placeholder="${escHtml(placeholder)}" value="${escHtml(value)}"
               autocomplete="off" spellcheck="false">
        <div class="pm-actions">
          <button class="pm-btn pm-btn-secondary" id="pm-cancel">${escHtml(cancelText)}</button>
          <button class="pm-btn pm-btn-primary" id="pm-confirm">${escHtml(confirmText)}</button>
        </div>
      </div>`;
    overlay.classList.add('show');
    requestAnimationFrame(() => overlay.querySelector('.pm-card').classList.remove('pm-enter'));

    const input = document.getElementById('pm-input');
    const close = (val) => { _closePm(); resolve(val); };
    document.getElementById('pm-confirm').onclick = () => close(input.value);
    document.getElementById('pm-cancel').onclick = () => close(null);
    overlay.onclick = e => { if (e.target === overlay) close(null); };
    input.onkeydown = e => { if (e.key === 'Enter') close(input.value); if (e.key === 'Escape') close(null); };
    input.focus();
    input.select();
  });
}

function _closePm() {
  const overlay = document.getElementById('pm-overlay');
  const card = overlay.querySelector('.pm-card');
  if (card) card.classList.add('pm-exit');
  if (_pmCloseTimer) clearTimeout(_pmCloseTimer);
  _pmCloseTimer = setTimeout(() => { overlay.classList.remove('show'); overlay.innerHTML = ''; _pmCloseTimer = null; }, 150);
}
