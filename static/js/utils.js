/* utils.js — shared helper functions and premium modal system */

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
  setTimeout(() => { overlay.classList.remove('show'); overlay.innerHTML = ''; }, 150);
}
