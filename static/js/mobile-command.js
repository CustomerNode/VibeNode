/* Mobile Command — System → Mobile Command GUIDED SETUP WIZARD.
 *
 * Private phone access to VibeNode over the user's OWN Tailscale tailnet. This is a
 * step-by-step wizard that reads live Tailscale state (via /api/mobile/status) and
 * AUTO-ADVANCES the instant it detects each step is done — so a non-technical user
 * never has to guess whether they did something right, and never lands on a dead page.
 *
 * Design principles (deliberately NOT lazy):
 *   - Every step gives the user a concrete CALL TO ACTION button (download, open app,
 *     open the exact settings page, copy the account, copy the link, open App Store).
 *   - Every step SELF-VERIFIES from state and advances on its own (a manual "Check
 *     again" is present only as a fallback; it shouldn't be needed).
 *   - The account + phone + OS are all read LIVE from THIS machine — nothing about the
 *     user's identity is ever hardcoded. It's always their own account / their own phone.
 *
 * All state lives server-side (app/mobile_command.py). Depends on the vendored
 * qrcode-generator lib (static/vendor/qrcode.min.js) and _closePm (utils.js).
 */

const _MC_APPSTORE_URL = 'https://apps.apple.com/us/app/tailscale/id1470499037';

function _mcEsc(s) {
  return String(s == null ? '' : s)
    .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;').replace(/'/g, '&#39;');
}

async function _mcGetStatus() {
  try {
    const r = await fetch('/api/mobile/status');
    if (r.ok) return await r.json();
  } catch (e) { /* fallthrough */ }
  return null;
}

async function _mcPost(path) {
  try {
    const r = await fetch(path, { method: 'POST' });
    if (r.ok) return await r.json();
  } catch (e) { /* fallthrough */ }
  return null;
}

/* Render a QR code for `text` into element `elId` using the vendored lib. */
function _mcRenderQR(elId, text) {
  const el = document.getElementById(elId);
  if (!el) return;
  if (typeof qrcode !== 'function') { el.textContent = text; return; }
  try {
    const qr = qrcode(0, 'M');        // type 0 = auto-size, M = ~15% error correction
    qr.addData(text);
    qr.make();
    el.innerHTML = qr.createSvgTag({ cellSize: 5, margin: 2, scalable: true });
    const svg = el.querySelector('svg');
    if (svg) { svg.style.width = '100%'; svg.style.height = '100%'; svg.style.background = '#fff'; }
  } catch (e) {
    el.textContent = text;
  }
}

/* Keep the System-menu label (On/Off) in sync. */
function _mcUpdateMenuLabel(st) {
  const label = document.getElementById('sys-mobile-label');
  if (label) label.textContent = (st && st.enabled) ? (st.serving ? 'On' : 'On…') : 'Off';
}

/* Copy helper with inline "Copied ✓" feedback on the clicking button. */
function _mcCopy(text, btn) {
  try {
    navigator.clipboard.writeText(text);
    if (btn) {
      const orig = btn.innerHTML;
      btn.innerHTML = 'Copied ✓';
      setTimeout(() => { btn.innerHTML = orig; }, 1400);
    }
  } catch (e) { /* clipboard may be blocked; the value is shown as text anyway */ }
}

// ---------------------------------------------------------------------------
// Wizard step model — the ladder every setup climbs, in order.
// ---------------------------------------------------------------------------
// Each rung maps to a state the server reports; _mcCurrentStep() picks the lowest
// unsatisfied rung and we render that step. When state changes (poll), we re-render
// and thus auto-advance.
const _MC_STEPS = ['install', 'signin', 'https', 'bridge', 'phone', 'done'];

function _mcCurrentStep(st) {
  if (!st) return 'error';
  if (!st.installed) return 'install';
  if (!st.logged_in) return 'signin';
  // From here the feature must be ON to make progress; if it's off, show the intro.
  if (!st.enabled) return 'intro';
  if (st.needs === 'enable_https') return 'https';
  if (!st.serving) return 'bridge';          // serve coming up (or being (re)started)
  return 'phone';                             // live — do the phone side (and it's reusable forever)
}

// Auto-poll handle so we can stop it when the modal closes.
let _mcPollTimer = null;
function _mcStopPoll() { if (_mcPollTimer) { clearTimeout(_mcPollTimer); _mcPollTimer = null; } }
function _mcSchedulePoll(ms) {
  _mcStopPoll();
  _mcPollTimer = setTimeout(async () => {
    const overlay = document.getElementById('pm-overlay');
    // Stop polling if the modal was closed.
    if (!overlay || !overlay.classList.contains('show') || !overlay.querySelector('.mc-wizard')) {
      _mcStopPoll();
      return;
    }
    await _mcRender();
  }, ms);
}

// ---------------------------------------------------------------------------
// Entry point (wired to the System menu button)
// ---------------------------------------------------------------------------

async function openMobileCommand() {
  const overlay = document.getElementById('pm-overlay');
  if (!overlay) return;
  _mcStopPoll();
  overlay.innerHTML = `<div class="pm-card pm-enter" style="max-width:580px;"><div class="pm-body">
    <div style="text-align:center;padding:48px 0;color:var(--text-muted);">
      <span class="spinner"></span> Checking…</div></div></div>`;
  overlay.classList.add('show');
  requestAnimationFrame(() => overlay.querySelector('.pm-card')?.classList.remove('pm-enter'));
  overlay.onclick = (e) => { if (e.target === overlay) _mcClose(); };
  await _mcRender();
}

function _mcClose() {
  _mcStopPoll();
  if (typeof _closePm === 'function') _closePm();
}

async function _mcEnableClicked() {
  const overlay = document.getElementById('pm-overlay');
  if (overlay) overlay.querySelector('.pm-body')?.insertAdjacentHTML('afterbegin',
    '<div style="text-align:center;color:var(--text-muted);margin-bottom:10px;"><span class="spinner"></span> Turning on…</div>');
  const st = await _mcPost('/api/mobile/enable');
  _mcUpdateMenuLabel(st);
  await _mcRender(st);
}

async function _mcDisableClicked() {
  const st = await _mcPost('/api/mobile/disable');
  _mcUpdateMenuLabel(st);
  await _mcRender(st);
}

/* (Re)establish the serve — POST /enable actually runs `tailscale serve`. */
let _mcKickInFlight = false;
async function _mcKickServe() {
  if (_mcKickInFlight) return;
  _mcKickInFlight = true;
  try {
    const st = await _mcPost('/api/mobile/enable');
    _mcUpdateMenuLabel(st);
    await _mcRender(st);
  } finally { _mcKickInFlight = false; }
}

// ---------------------------------------------------------------------------
// Renderer — draws the current wizard step + schedules the next auto-poll.
// ---------------------------------------------------------------------------

async function _mcRender(prefetched) {
  const overlay = document.getElementById('pm-overlay');
  if (!overlay) return;
  const st = prefetched || await _mcGetStatus();
  _mcUpdateMenuLabel(st);

  const step = _mcCurrentStep(st);
  let body, pollMs = 2500;   // default cadence while the user works a step

  switch (step) {
    case 'error':   body = _mcStepError();          pollMs = 4000; break;
    case 'install': body = _mcStepInstall(st);      pollMs = 2500; break;
    case 'signin':  body = _mcStepSignin(st);       pollMs = 2000; break;
    case 'intro':   body = _mcStepIntro(st);        pollMs = 0;    break;   // waits for the user to click Turn On
    case 'https':   body = _mcStepHttps(st);        pollMs = 2500; break;
    case 'bridge':  body = _mcStepBridge(st);       pollMs = 1500; break;
    case 'phone':   body = _mcStepPhone(st);        pollMs = 2500; break;   // keep polling to flip in "phone connected"
    default:        body = _mcStepPhone(st);        pollMs = 2500; break;
  }

  overlay.innerHTML = `<div class="pm-card mc-wizard" style="max-width:580px;">
    <h2 class="pm-title">Set up phone access</h2>
    ${_mcStepper(step, st)}
    <div class="pm-body">${body}</div>
    <div class="pm-actions">
      <button class="pm-btn pm-btn-secondary" onclick="_mcClose()">Close</button>
    </div>
  </div>`;

  // Post-render: draw any QR codes the step declared, then auto-advance next tick.
  if (overlay.querySelector('#mc-qr-app')) _mcRenderQR('mc-qr-app', _MC_APPSTORE_URL);
  if (st && st.url && overlay.querySelector('#mc-qr-open')) _mcRenderQR('mc-qr-open', st.url);
  if (st && st.install_url && overlay.querySelector('#mc-qr-install')) _mcRenderQR('mc-qr-install', st.install_url);

  // Bridge step needs an active nudge: status() can't start the serve — POST /enable does.
  if (step === 'bridge' && !_mcKickInFlight) _mcKickServe();

  if (pollMs > 0) _mcSchedulePoll(pollMs); else _mcStopPoll();
}

// ---------------------------------------------------------------------------
// Progress stepper (the visual ladder across the top)
// ---------------------------------------------------------------------------
function _mcStepper(step, st) {
  // Collapse intro/bridge/done into their neighbor rungs for the dots.
  const map = { intro: 'signin', bridge: 'phone', done: 'phone', error: 'install' };
  const cur = map[step] || step;
  const rungs = [
    { key: 'install', label: 'Install' },
    { key: 'signin',  label: 'Sign in' },
    { key: 'https',   label: 'Enable HTTPS' },
    { key: 'phone',   label: 'Your phone' },
  ];
  // If HTTPS is already on (2nd machine), drop that rung entirely.
  const showHttps = !(st && st.logged_in && st.needs !== 'enable_https' && st.enabled && st.serving) || step === 'https';
  const order = ['install', 'signin', 'https', 'phone'];
  const curIdx = order.indexOf(cur);
  let html = '<div class="mc-steps">';
  rungs.forEach((r) => {
    if (r.key === 'https' && st && st.host_os && !showHttps && step !== 'https') { /* keep it simple: always show */ }
    const idx = order.indexOf(r.key);
    const state = idx < curIdx ? 'done' : (idx === curIdx ? 'active' : 'todo');
    const mark = state === 'done' ? '✓' : (idx + 1);
    html += `<div class="mc-step mc-step-${state}"><span class="mc-step-dot">${mark}</span><span class="mc-step-label">${r.label}</span></div>`;
  });
  html += '</div>';
  return html;
}

// ---------------------------------------------------------------------------
// Steps
// ---------------------------------------------------------------------------

function _mcOsLabel(os) {
  return os === 'win' ? 'Windows' : os === 'mac' ? 'Mac' : os === 'linux' ? 'Linux' : 'this computer';
}

function _mcStepError() {
  return `<div style="text-align:center;padding:26px 0;">
      <div style="font-size:14px;font-weight:600;margin-bottom:6px;">Couldn’t reach VibeNode</div>
      <div style="font-size:12px;color:var(--text-muted);margin-bottom:16px;">It’ll retry automatically…</div>
      <button class="pm-btn pm-btn-secondary" onclick="_mcRender()">Check again now</button>
    </div>`;
}

// STEP 1 — Install Tailscale on this computer.
function _mcStepInstall(st) {
  const os = st.host_os || '';
  const url = _mcEsc(st.install_url || 'https://tailscale.com/download');
  return `
    <div class="mc-lead">VibeNode reaches your phone privately through <strong>Tailscale</strong> — a free,
      secure connector. First, install it on <strong>${_mcOsLabel(os)}</strong>. It’s a one-time thing.</div>
    <div class="mc-cta-row">
      <a class="mc-cta mc-cta-primary" href="${url}" target="_blank" rel="noopener">⬇︎&nbsp; Download Tailscale for ${_mcOsLabel(os)}</a>
    </div>
    <ol class="mc-ol">
      <li>Click the button above and run the installer.</li>
      <li>Open Tailscale and <strong>sign in</strong> (create a free account if it asks).</li>
    </ol>
    <div class="mc-waiting"><span class="spinner"></span> Waiting for Tailscale to be installed… this screen moves on by itself.</div>
    <div class="mc-fallback"><button class="pm-btn pm-btn-secondary" onclick="_mcRender()">Check again now</button></div>`;
}

// STEP 2 — Sign in (installed but not connected).
function _mcStepSignin(st) {
  return `
    <div class="mc-lead">Tailscale is installed — nice. Now <strong>open it and sign in</strong> on this
      computer (create a free account if it asks). Remember which account you use — you’ll sign your
      phone into the <strong>same one</strong> in a moment.</div>
    <div class="mc-cta-row">
      <a class="mc-cta mc-cta-primary" href="https://login.tailscale.com/start" target="_blank" rel="noopener">Open Tailscale sign-in</a>
    </div>
    <div class="mc-waiting"><span class="spinner"></span> Waiting for you to sign in… this screen moves on by itself.</div>
    <div class="mc-fallback"><button class="pm-btn pm-btn-secondary" onclick="_mcRender()">Check again now</button></div>`;
}

// Between sign-in and the rest: the one-tap "Turn On" (persists the feature).
function _mcStepIntro(st) {
  const acct = st.account ? `<div class="mc-acct-note">Signed in as <strong>${_mcEsc(st.account)}</strong></div>` : '';
  return `
    <div class="mc-lead">You’re signed in. Flip on phone access — do this once and it stays on every time
      VibeNode runs. Your sessions never leave this machine; this only opens a private door for your own devices.</div>
    ${acct}
    <div class="mc-cta-row">
      <button class="mc-cta mc-cta-primary" onclick="_mcEnableClicked()">Turn on phone access</button>
    </div>`;
}

// STEP 3 — Enable HTTPS (one-time, per tailnet). Skipped automatically on machine #2.
function _mcStepHttps(st) {
  const url = _mcEsc(st.https_help || 'https://login.tailscale.com/admin/dns');
  return `
    <div class="mc-lead">Almost there. Turn on <strong>HTTPS</strong> for your Tailscale network — one switch,
      one time. This is what lets your phone use <strong>voice input</strong> (the microphone only works over HTTPS).</div>
    <div class="mc-cta-row">
      <a class="mc-cta mc-cta-primary" href="${url}" target="_blank" rel="noopener">⚙︎&nbsp; Open Tailscale HTTPS settings</a>
    </div>
    <ol class="mc-ol">
      <li>On the page that opens, find <strong>HTTPS Certificates</strong>.</li>
      <li>Click <strong>Enable HTTPS</strong>.</li>
    </ol>
    <div class="mc-waiting"><span class="spinner"></span> Waiting for HTTPS to turn on… this screen moves on by itself.</div>
    <div class="mc-fallback"><button class="pm-btn pm-btn-secondary" onclick="_mcEnableClicked()">Check again now</button></div>`;
}

// STEP 4 — Bridge coming up (VibeNode does this itself; no user action).
function _mcStepBridge(st) {
  const err = st && st.error
    ? `<div class="mc-err">${_mcEsc(st.error)}</div>` : '';
  return `
    <div class="mc-lead">Opening the private door for your phone… this only takes a moment.</div>
    <div class="mc-waiting" id="mc-starting"><span class="spinner"></span> Starting…</div>
    ${err}
    <div class="mc-fallback"><button class="pm-btn pm-btn-secondary" onclick="_mcKickServe()">Retry</button></div>`;
}

// STEP 5 — Your phone. The reusable, everyday screen once it's live.
function _mcStepPhone(st) {
  const url = _mcEsc(st.url || '');
  const acct = _mcEsc(st.account || '');
  const dev = _mcEsc(st.device_name || 'VibeNode');

  // The unambiguous positive signal: has a phone joined this tailnet yet?
  const phoneLine = st.phone_connected
    ? `<div class="mc-phone-ok">✓ ${_mcEsc(st.phone_name || 'Your phone')} is connected to your network</div>`
    : `<div class="mc-phone-wait"><span class="spinner"></span> Waiting for your phone to join your network…</div>`;

  const acctCta = acct
    ? `<div class="mc-acct-box">
         <div class="mc-acct-label">On your phone, sign Tailscale into this <strong>exact same account</strong>:</div>
         <div class="mc-acct-row">
           <code class="mc-acct-code">${acct}</code>
           <button class="pm-btn pm-btn-secondary mc-copy" onclick="_mcCopy('${acct.replace(/'/g, "\\'")}', this)">📋 Copy</button>
         </div>
       </div>`
    : '';

  return `
    <div class="mc-live-banner">
      <span class="mc-live-dot">●</span> On — your phone can reach VibeNode
      <button class="pm-btn pm-btn-secondary mc-turnoff" onclick="_mcDisableClicked()">Turn off</button>
    </div>

    <div class="mc-lead" style="text-align:center;">Do this once on your phone. After that, just tap the icon — it always works.</div>

    <div class="mc-phone-grid">
      <div class="mc-phone-col">
        <div class="mc-col-title">① Get the Tailscale app</div>
        <div id="mc-qr-app" class="mc-qr"></div>
        <div class="mc-cta-row">
          <a class="mc-cta mc-cta-mini" href="${_MC_APPSTORE_URL}" target="_blank" rel="noopener">Open the App Store</a>
        </div>
        <div class="mc-col-note">Scan with the phone camera → install <strong>Tailscale</strong> → sign in → tap <strong>Allow</strong> for VPN.</div>
      </div>
      <div class="mc-phone-col">
        <div class="mc-col-title">② Open VibeNode</div>
        <div id="mc-qr-open" class="mc-qr"></div>
        <div class="mc-cta-row">
          <button class="pm-btn pm-btn-secondary mc-copy" onclick="_mcCopy('${url.replace(/'/g, "\\'")}', this)">📋 Copy link</button>
        </div>
        <div class="mc-col-note">Scan to open, then <strong>Share → Add to Home Screen</strong>. The icon is named <strong>${dev}</strong>.</div>
      </div>
    </div>

    ${acctCta}
    ${phoneLine}

    <div class="mc-name-row">
      <div class="mc-name-label">Name this computer (so multiple machines are easy to tell apart on your phone):</div>
      <div class="mc-name-inputrow">
        <input id="mc-device-name" type="text" maxlength="40" value="${dev}" placeholder="e.g. Studio Mac"
               onkeydown="if(event.key==='Enter')_mcSaveName()" class="mc-name-input">
        <button class="pm-btn pm-btn-secondary" onclick="_mcSaveName()">Save</button>
      </div>
    </div>

    <div class="mc-url">${url}</div>`;
}

/* Save this computer's Home-Screen label. */
async function _mcSaveName() {
  const el = document.getElementById('mc-device-name');
  if (!el) return;
  const name = el.value.trim();
  el.disabled = true;
  try {
    await fetch('/api/mobile/name', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ name }),
    });
  } catch (e) { /* ignore */ }
  el.disabled = false;
}

// Sync the System-menu label on page load (retry until the element exists).
(async function _mcInitLabel() {
  for (let i = 0; i < 20; i++) {
    if (document.getElementById('sys-mobile-label')) {
      const st = await _mcGetStatus();
      _mcUpdateMenuLabel(st);
      return;
    }
    await new Promise(r => setTimeout(r, 200));
  }
})();
