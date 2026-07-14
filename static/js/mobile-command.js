/* Mobile Command — System → Mobile Command modal.
 *
 * Private phone access to VibeNode over your Tailscale tailnet. Mirrors the
 * Persistent Storage modal pattern (pm-overlay / pm-card / _closePm). All state
 * lives server-side (app/mobile_command.py); this file just renders it and drives
 * enable/disable, then shows QR codes for the one-time phone setup.
 *
 * Depends on the vendored qrcode-generator lib (static/vendor/qrcode.min.js).
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

// ---------------------------------------------------------------------------
// Entry point (wired to the System menu button)
// ---------------------------------------------------------------------------

// Bounded auto-kick counter for the "starting" state. status() is READ-ONLY and can
// never bring the serve up — only POST /enable runs `tailscale serve`. So when we land
// in "enabled but not serving" (reboot race, or a serve that timed out), we actively
// re-POST /enable a few times, then rest on a manual Retry button. Reset on each open.
let _mcKickCount = 0;
const _MC_MAX_KICKS = 3;

async function openMobileCommand() {
  const overlay = document.getElementById('pm-overlay');
  if (!overlay) return;
  _mcKickCount = 0;
  overlay.innerHTML = `<div class="pm-card pm-enter" style="max-width:560px;"><div class="pm-body">
    <div style="text-align:center;padding:48px 0;color:var(--text-muted);">
      <span class="spinner"></span> Checking…</div></div></div>`;
  overlay.classList.add('show');
  requestAnimationFrame(() => overlay.querySelector('.pm-card')?.classList.remove('pm-enter'));
  overlay.onclick = (e) => { if (e.target === overlay && typeof _closePm === 'function') _closePm(); };
  await _mcRender();
}

async function _mcEnableClicked() {
  _mcKickCount = 0;   // a manual Turn On / Retry resets the auto-kick budget
  const overlay = document.getElementById('pm-overlay');
  if (overlay) overlay.querySelector('.pm-body')?.insertAdjacentHTML('afterbegin',
    '<div style="text-align:center;color:var(--text-muted);margin-bottom:10px;"><span class="spinner"></span> Turning on…</div>');
  const st = await _mcPost('/api/mobile/enable');
  _mcUpdateMenuLabel(st);
  await _mcRender(st);
}

/* Actively (re)establish the serve — POST /enable, which runs `tailscale serve`.
   Used by the bounded auto-kick and the manual Retry button in the "starting" panel.
   The in-flight guard prevents a double-fire when a pending auto-kick timer and a
   manual Retry (or a second timer) overlap — otherwise two concurrent POST /enable
   would launch two `tailscale serve` + two warm threads. */
let _mcKickInFlight = false;
async function _mcKickServe() {
  if (_mcKickInFlight) return;
  _mcKickInFlight = true;
  try {
    const st = await _mcPost('/api/mobile/enable');
    _mcUpdateMenuLabel(st);
    await _mcRender(st);
  } finally {
    _mcKickInFlight = false;
  }
}

function _mcRetryStart() { _mcKickCount = 0; _mcKickServe(); }

async function _mcDisableClicked() {
  const st = await _mcPost('/api/mobile/disable');
  _mcUpdateMenuLabel(st);
  await _mcRender(st);
}

// ---------------------------------------------------------------------------
// Renderer — chooses the right panel from server status
// ---------------------------------------------------------------------------

async function _mcRender(prefetched) {
  const overlay = document.getElementById('pm-overlay');
  if (!overlay) return;
  const st = prefetched || await _mcGetStatus();
  _mcUpdateMenuLabel(st);

  let body;
  if (!st) {
    body = _mcMsg('Couldn’t reach VibeNode', 'Please try again in a moment.');
  } else if (!st.installed) {
    body = _mcNeedsTailscale();
  } else if (!st.logged_in) {
    body = _mcNeedsLogin();
  } else if (!st.enabled) {
    body = _mcIntro();
  } else if (st.needs === 'enable_https') {
    body = _mcNeedsHttps(st);
  } else if (!st.serving) {
    body = _mcStarting(st);
    // status() can't start the serve — actively re-POST /enable, bounded. Guard every
    // deferred call on the overlay still being open so closing the modal kills the loop
    // (no invisible background polling), and stop after _MC_MAX_KICKS so a genuinely
    // stuck serve rests on the manual Retry button instead of looping forever.
    if (_mcKickCount < _MC_MAX_KICKS) {
      _mcKickCount++;
      setTimeout(() => {
        const ov = document.getElementById('pm-overlay');
        if (ov && ov.classList.contains('show') && ov.querySelector('#mc-starting')) _mcKickServe();
      }, 1600);
    }
  } else {
    body = _mcReady(st);
  }

  overlay.innerHTML = `<div class="pm-card" style="max-width:560px;">
    <h2 class="pm-title">Mobile Command</h2>
    <div class="pm-body">${body}</div>
    <div class="pm-actions">
      <button class="pm-btn pm-btn-secondary" onclick="_closePm()">Close</button>
    </div>
  </div>`;

  // Render any QR codes the chosen panel declared.
  if (st && st.serving && st.url) _mcRenderQR('mc-qr-open', st.url);
  if (overlay.querySelector('#mc-qr-app')) _mcRenderQR('mc-qr-app', _MC_APPSTORE_URL);
}

// ---------------------------------------------------------------------------
// Panels
// ---------------------------------------------------------------------------

function _mcHint() {
  return `<div style="font-size:12px;color:var(--text-muted);margin-bottom:16px;">
    Use VibeNode from your phone over your private Tailscale network. Your sessions and
    data stay on <strong>this machine</strong> — nothing is uploaded, and only your own
    devices can reach it. VibeNode stays on localhost; this just opens a private door.
  </div>`;
}

function _mcIntro() {
  return _mcHint() + `
    <div style="text-align:center;padding:8px 0 4px;">
      <button class="kanban-settings-btn-accent" onclick="_mcEnableClicked()"
              style="padding:12px 28px;font-size:15px;font-weight:600;">Turn On</button>
      <div style="font-size:11px;color:var(--text-faint);margin-top:10px;">
        Flip it on once — it stays on every time VibeNode runs.</div>
    </div>`;
}

function _mcNeedsTailscale() {
  return `<div style="font-size:13px;color:var(--text-secondary);margin-bottom:14px;">
      Mobile Command uses <strong>Tailscale</strong> — a free, private connector — to reach
      this machine from your phone. It isn’t installed yet.</div>
    <ol style="font-size:13px;color:var(--text-secondary);line-height:1.9;margin:0 0 16px 18px;">
      <li>Install Tailscale on this computer and sign in.</li>
      <li>Come back here and click <strong>Re-check</strong>.</li>
    </ol>
    <div style="display:flex;gap:8px;justify-content:center;">
      <a class="kanban-settings-btn-accent" href="https://tailscale.com/download" target="_blank"
         rel="noopener" style="padding:10px 20px;font-size:13px;text-decoration:none;">Download Tailscale</a>
      <button class="pm-btn pm-btn-secondary" onclick="_mcRender()">Re-check</button>
    </div>`;
}

function _mcNeedsLogin() {
  return `<div style="font-size:13px;color:var(--text-secondary);margin-bottom:14px;">
      Tailscale is installed but not connected on this computer (signed out or paused).
      Open the Tailscale app, sign in / connect, then click <strong>Re-check</strong>.</div>
    <div style="text-align:center;">
      <button class="pm-btn pm-btn-secondary" onclick="_mcRender()">Re-check</button>
    </div>`;
}

function _mcNeedsHttps(st) {
  const url = _mcEsc(st.https_help || 'https://login.tailscale.com/admin/dns');
  return `<div style="font-size:13px;color:var(--text-secondary);margin-bottom:14px;">
      Almost there. Your Tailscale network needs <strong>HTTPS</strong> turned on once
      (a single switch in your Tailscale admin page). This is what lets your phone use
      <strong>voice input</strong> — iOS only allows the microphone over HTTPS. After this
      one-time switch, mobile access works forever.</div>
    <ol style="font-size:13px;color:var(--text-secondary);line-height:1.9;margin:0 0 16px 18px;">
      <li>Open your Tailscale admin page (button below).</li>
      <li>Enable <strong>HTTPS Certificates</strong>.</li>
      <li>Come back and click <strong>Retry</strong>.</li>
    </ol>
    <div style="display:flex;gap:8px;justify-content:center;">
      <a class="kanban-settings-btn-accent" href="${url}" target="_blank" rel="noopener"
         style="padding:10px 20px;font-size:13px;text-decoration:none;">Open Tailscale admin</a>
      <button class="pm-btn pm-btn-secondary" onclick="_mcEnableClicked()">Retry</button>
    </div>`;
}

function _mcStarting(st) {
  const err = (st && st.error)
    ? `<div style="font-size:12px;color:var(--red,#ff4d4f);margin-top:12px;word-break:break-word;">${_mcEsc(st.error)}</div>`
    : '';
  // The id lets the bounded auto-kick verify this panel is still on screen before it
  // fires (see _mcRender). The Retry button is the guaranteed recovery path if the
  // serve is genuinely stuck (reboot race / timeout) — it re-runs enable().
  return `<div id="mc-starting" style="text-align:center;padding:32px 0;color:var(--text-muted);">
      <span class="spinner"></span> Opening the private door for your phone…
      ${err}
      <div style="margin-top:18px;">
        <button class="pm-btn pm-btn-secondary" onclick="_mcRetryStart()">Retry</button>
      </div>
    </div>`;
}

/* The main event: it's live. Show the one-time phone setup with QR codes. */
function _mcReady(st) {
  const url = _mcEsc(st.url);
  return `
    <div style="display:flex;align-items:center;gap:8px;justify-content:space-between;margin-bottom:16px;
                padding:10px 14px;background:rgba(52,199,89,0.10);border:1px solid rgba(52,199,89,0.35);border-radius:8px;">
      <div style="font-size:13px;color:var(--green,#34c759);font-weight:600;">● On — your phone can reach VibeNode</div>
      <button class="pm-btn pm-btn-secondary" onclick="_mcDisableClicked()" style="padding:5px 12px;font-size:12px;">Turn Off</button>
    </div>

    <div style="font-size:12px;color:var(--text-muted);margin-bottom:14px;text-align:center;">
      Do this once on your phone. After that, just tap the icon — it always works.</div>

    <div style="display:flex;gap:18px;align-items:flex-start;flex-wrap:wrap;justify-content:center;">
      <div style="flex:1;min-width:210px;text-align:center;">
        <div style="font-size:13px;font-weight:600;margin-bottom:8px;">① First time only: get the app</div>
        <div id="mc-qr-app" style="width:150px;height:150px;margin:0 auto 8px;border-radius:8px;overflow:hidden;"></div>
        <div style="font-size:11px;color:var(--text-muted);line-height:1.6;">
          Scan with your phone camera → install <strong>Tailscale</strong> → sign in the same
          as this computer → tap <strong>Allow</strong> for VPN.</div>
      </div>
      <div style="flex:1;min-width:210px;text-align:center;">
        <div style="font-size:13px;font-weight:600;margin-bottom:8px;">② Open VibeNode</div>
        <div id="mc-qr-open" style="width:150px;height:150px;margin:0 auto 8px;border-radius:8px;overflow:hidden;"></div>
        <div style="font-size:11px;color:var(--text-muted);line-height:1.6;">
          Scan to open VibeNode, then tap <strong>Share → Add to Home Screen</strong>.
          The icon is named <strong>${_mcEsc(st.device_name || 'VibeNode')}</strong> — tap it anytime.</div>
      </div>
    </div>

    <div style="margin-top:18px;padding-top:14px;border-top:1px solid var(--border-subtle);">
      <div style="font-size:12px;color:var(--text-secondary);margin-bottom:7px;">
        Home-Screen name for <strong>this computer</strong> — so multiple machines are easy to tell apart:</div>
      <div style="display:flex;gap:8px;">
        <input id="mc-device-name" type="text" maxlength="40" value="${_mcEsc(st.device_name || '')}"
               placeholder="e.g. Studio Mac"
               onkeydown="if(event.key==='Enter')_mcSaveName()"
               style="flex:1;min-width:0;padding:9px 11px;font-size:14px;color:var(--text-primary);
                      background:var(--bg-input,rgba(255,255,255,0.04));border:1px solid var(--border);border-radius:8px;">
        <button class="pm-btn pm-btn-secondary" onclick="_mcSaveName()" style="padding:6px 16px;font-size:13px;">Save</button>
      </div>
      <div id="mc-name-hint" style="font-size:11px;color:var(--text-faint);margin-top:6px;">
        Tip: you can also rename it right on iOS when you tap <strong>Add to Home Screen</strong>.</div>
    </div>

    <div style="margin-top:16px;font-size:11px;color:var(--text-faint);text-align:center;word-break:break-all;">
      ${url}</div>`;
}

/* Save this computer's Home-Screen label. Persists server-side so the phone picks
   it up on the next load (the title is rendered into the page's meta tags). */
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
    const hint = document.getElementById('mc-name-hint');
    if (hint) hint.innerHTML = 'Saved ✓ — reopen VibeNode on your phone (or re-add it) to see the new name.';
  } catch (e) {
    el.disabled = false;
  }
}

function _mcMsg(title, sub) {
  return `<div style="text-align:center;padding:28px 0;">
      <div style="font-size:14px;font-weight:600;margin-bottom:6px;">${_mcEsc(title)}</div>
      <div style="font-size:12px;color:var(--text-muted);margin-bottom:16px;">${_mcEsc(sub || '')}</div>
      <button class="pm-btn pm-btn-secondary" onclick="_mcRender()">Try again</button>
    </div>`;
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
