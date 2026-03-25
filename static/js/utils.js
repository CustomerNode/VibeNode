/* utils.js — shared helper functions and premium modal system */

var _pmCloseTimer = null;

function escHtml(str) {
  if (!str) return '';
  return String(str)
    .replace(/&/g,'&amp;')
    .replace(/</g,'&lt;')
    .replace(/>/g,'&gt;')
    .replace(/"/g,'&quot;');
}

function showToast(msg, isError=false) {
  const t = document.getElementById('toast');
  t.textContent = msg;
  t.className = 'toast show' + (isError ? ' error' : '');
  setTimeout(() => { t.classList.remove('show'); }, 3000);
}

// ---------------------------------------------------------------------------
// Send Behavior Preference — 'ctrl-enter' (default) or 'enter'
// ---------------------------------------------------------------------------
let sendBehavior = localStorage.getItem('sendBehavior') || 'ctrl-enter';

/** Returns true if the keyboard event should trigger a send based on preference */
function _shouldSend(e) {
  if (sendBehavior === 'enter') return e.key === 'Enter' && !e.shiftKey;
  return e.key === 'Enter' && (e.ctrlKey || e.metaKey);
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
  const text = sendBehavior === 'enter' ? 'Enter to send · Shift+Enter for new line' : 'Ctrl+Enter to send';
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
  _refreshSendHints();
  showToast('Send: ' + (sendBehavior === 'enter' ? 'Enter to send' : 'Ctrl+Enter to send'));
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
