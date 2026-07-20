/* healthchecks.js - Unkillable blocker overlay with pluggable checks.
 *
 * Usage:
 *   registerHealthCheck(id, { label, icon, message, test, action })
 *
 *   - id        unique string key
 *   - label     short name shown as the heading
 *   - icon      HTML string for the big icon
 *   - message   longer explanation shown under the heading
 *   - test()    returns truthy = healthy, falsy = failing
 *   - action    optional { text, onClick } to show a button on the overlay
 *
 * Polls every 3s while blocked, 10s while healthy.
 * First failing check (by registration order) wins the overlay.
 */

var _healthChecks = [];
var _healthPollId = null;
var _healthBlocking = false;
var _currentFailId = null;

function registerHealthCheck(id, opts) {
    for (var i = 0; i < _healthChecks.length; i++) {
        if (_healthChecks[i].id === id) return;
    }
    _healthChecks.push({
        id: id,
        label: opts.label || 'Error',
        icon: opts.icon || '',
        message: opts.message || 'Something went wrong.',
        test: opts.test,
        action: opts.action || null
    });
    if (!_healthPollId) _startHealthPoll();
    _runHealthChecks();
}

function _getHealthOverlay() {
    return document.getElementById('health-blocker');
}

function _runHealthChecks() {
    var failing = null;
    for (var i = 0; i < _healthChecks.length; i++) {
        try {
            if (!_healthChecks[i].test()) {
                failing = _healthChecks[i];
                break;
            }
        } catch (e) {
            // If a test throws, treat it as failing
            failing = _healthChecks[i];
            break;
        }
    }
    var overlay = _getHealthOverlay();
    if (!overlay) return;
    if (failing) {
        // Only update DOM if the failing check changed
        if (_currentFailId !== failing.id) {
            overlay.querySelector('.hb-icon').innerHTML = failing.icon;
            overlay.querySelector('.hb-label').textContent = failing.label;
            overlay.querySelector('.hb-message').textContent = failing.message;
            var btnWrap = overlay.querySelector('.hb-action');
            if (failing.action) {
                btnWrap.innerHTML = '<button class="hb-btn">' + failing.action.text + '</button>';
                btnWrap.querySelector('.hb-btn').onclick = failing.action.onClick;
                btnWrap.style.display = '';
            } else {
                btnWrap.innerHTML = '';
                btnWrap.style.display = 'none';
            }
            _currentFailId = failing.id;
        }
        if (!_healthBlocking) {
            overlay.classList.add('show');
            _healthBlocking = true;
            // Aggressive re-eval while blocked so recovery is near-instant
            // once the underlying probe (server / gstatic / daemon) succeeds.
            _restartHealthPoll(1000);
        }
    } else if (_healthBlocking) {
        overlay.classList.remove('show');
        _healthBlocking = false;
        _currentFailId = null;
        _restartHealthPoll(10000);
    }
}

function _startHealthPoll() {
    _healthPollId = setInterval(_runHealthChecks, 3000);
}

function _restartHealthPoll(ms) {
    if (_healthPollId) clearInterval(_healthPollId);
    _healthPollId = setInterval(_runHealthChecks, ms);
}

/* ---- Built-in: WiFi / Internet connectivity ----
 *
 * Design note (2026-07-20): On mobile over Tailscale, this check used to be
 * the primary source of the "no internet" overlay. Two problems:
 *   1. The gstatic probe fires every 60s. Once it fails, the overlay stays
 *      up for up to a full minute — long enough that users hard-close the app.
 *   2. iOS Safari fires spurious `offline` events during wifi/cellular
 *      handoff. Trusting them flashes the overlay for no real reason.
 *
 * Fix: SERVER-REACHABLE IS THE PRIMARY SIGNAL. If we can talk to our own
 * `/api/ping` (over the network the phone is currently on), we obviously
 * have working internet — the gstatic probe is redundant and only reliable
 * enough to be a tie-breaker when the server ALSO looks down. The wifi
 * overlay now only shows when BOTH signals fail, and the probe cadence
 * accelerates aggressively while blocking so recovery is near-immediate.
 */
var _internetReachable = navigator.onLine;  // seed with browser hint

function _probeInternet() {
    fetch('https://www.gstatic.com/generate_204', {
        method: 'HEAD', mode: 'no-cors', cache: 'no-store'
    })
    .then(function() { _internetReachable = true;  _runHealthChecks(); })
    .catch(function() { _internetReachable = false; _runHealthChecks(); });
}

registerHealthCheck('wifi', {
    label: 'No Internet Connection',
    icon: '<svg width="64" height="64" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"><path d="M1 1l22 22"/><path d="M16.72 11.06A10.94 10.94 0 0 1 19 12.55"/><path d="M5 12.55a10.94 10.94 0 0 1 5.17-2.39"/><path d="M10.71 5.05A16 16 0 0 1 22.56 9"/><path d="M1.42 9a15.91 15.91 0 0 1 4.7-2.88"/><path d="M8.53 16.11a6 6 0 0 1 6.95 0"/><line x1="12" y1="20" x2="12.01" y2="20"/></svg>',
    message: 'VibeNode requires an active internet connection to communicate with Claude. Please check your WiFi or network settings.',
    test: function() {
        // If our own server is reachable, we have internet by definition.
        // This suppresses false-positive wifi overlays during Tailscale
        // reconnection where gstatic can be slow/blocked but /api/ping works.
        if (_serverReachable) return true;
        return _internetReachable;
    }
});

// Browser `offline` event is unreliable on mobile — fires during handoffs
// even though real connectivity is fine. Treat it as a HINT to re-probe,
// never as authoritative. The gstatic + server probes are the source of truth.
window.addEventListener('offline', function() { _probeInternet(); _probeServer(); });
window.addEventListener('online', function() { _probeInternet(); _probeServer(); });

_probeInternet();
// Probe every 10s normally, but drop to 3s while any overlay is blocking so
// recovery is fast. The old 60s cadence made the wifi overlay stick for up to
// a minute after the network was actually back — the "hard-close" trigger.
setInterval(function() {
    _probeInternet();
}, 10000);
setInterval(function() {
    if (_healthBlocking) _probeInternet();
}, 3000);

/* ---- Built-in: VibeNode web server reachable ----
 *
 * The web server is now spawned detached (pythonw on Windows, nohup on
 * POSIX) so the failure mode of "minimized launcher window got closed"
 * is gone. But if the server dies for any other reason — crash, manual
 * kill, port conflict — the user is left with a loaded page whose backend
 * is gone, and the existing wifi check won't catch it (gstatic still
 * responds). This check probes our own /api/ping and shows the same
 * blocker overlay used for wifi / auth issues.
 *
 * Requires N consecutive failures before showing, so a single transient
 * blip during /api/restart doesn't flash the overlay. The restart UI in
 * modals.js handles its own reboot overlay anyway.
 */

var _serverReachable = true;
var _serverFailCount = 0;
// Raised from 2 → 4 (2026-07-20). At 3s cadence that's ~12s before the
// "Unreachable" overlay fires from steady-state polling. Mobile Tailscale
// wifi ↔ cellular handoffs routinely stall for 5–10s; the old 6s threshold
// tripped on every handoff. A genuinely-dead server surfaces via the 503
// path (immediate reload, threshold does NOT apply — see below) or via the
// decisive foreground check (also independent of this threshold).
var _SERVER_FAIL_THRESHOLD = 4;

// When the backend is gone, the reviver holds port 5050 and serves a "Start
// VibeNode" page — but only on a fresh document load. A still-loaded app never
// re-requests '/', so without help the user is stranded on a dead UI until they
// manually exit and re-enter. On failure we reload the page so it lands on the
// reviver's Start page (which then handles start + auto-recovery). Guarded so it
// only fires on a genuine death, never during an in-app restart (modals.js owns
// that flow and shows its own overlay).
var _reloadingForDeath = false;
function _recoverToStartPage() {
    // Don't hijack an in-app restart — modals.js is already driving it.
    if (document.getElementById('restart-overlay')) return;
    if (_reloadingForDeath) return;   // never fire multiple reloads
    _reloadingForDeath = true;
    // Reload once; the Start page we land on is not this app, so it won't loop.
    try { window.location.reload(); } catch (e) { _reloadingForDeath = false; }
}

// DECISIVE two-probe check for foreground events. Mobile browsers FREEZE all
// JS timers while the tab is backgrounded / the phone is asleep, so the
// debounced interval below (which needs 2 consecutive failures) is useless
// the moment the user comes back — it would need ~6s of foreground polling
// to trip. This runs a probe the instant the page is foregrounded, and if it
// fails, ONE quick retry ~1.2s later before showing the overlay.
//
// The retry eats the 1–3s Tailscale reconnection window (wifi handoff, radio
// waking, tunnel re-establishing) that used to flash the "Unreachable"
// overlay for every foreground event. A truly-dead server still surfaces
// within ~1.5s — an order of magnitude faster than waiting for the 3s
// interval poll to threshold — but transient network flakes are absorbed.
//
// A 503 from the reviver is unambiguous ("VibeNode is down and I've taken
// over 5050"), so it still reloads immediately without waiting for a retry.
function _decisiveServerCheck() {
    if (_reloadingForDeath) return;
    if (document.getElementById('restart-overlay')) return;  // in-app restart owns it
    _decisiveServerProbe(false);
}

function _decisiveServerProbe(isRetry) {
    var ctrl = null;
    try { ctrl = AbortSignal.timeout ? AbortSignal.timeout(4000) : undefined; } catch (e) {}
    fetch('/api/ping', { method: 'GET', cache: 'no-store', signal: ctrl })
        .then(function(r) {
            if (r.ok) {
                // Healthy — clear any accumulated failures.
                _serverFailCount = 0;
                if (!_serverReachable) { _serverReachable = true; _runHealthChecks(); }
            } else if (r.status === 503) {
                // Reviver DEFINITIVELY owns 5050 — reloading now lands on its
                // Start page. Safe to reload without a retry.
                _serverReachable = false;
                _recoverToStartPage();
            } else {
                // 502 / other = the web server is gone but NOBODY is serving
                // 5050 yet (reviver not up). Retry once before showing overlay
                // to absorb transient Tailscale hiccups.
                if (!isRetry) {
                    setTimeout(function() { _decisiveServerProbe(true); }, 1200);
                } else {
                    _serverReachable = false;
                    _runHealthChecks();
                }
            }
        })
        .catch(function() {
            // Connection refused / timeout — retry once before overlay.
            if (!isRetry) {
                setTimeout(function() { _decisiveServerProbe(true); }, 1200);
            } else {
                _serverReachable = false;
                _runHealthChecks();
            }
        });
}

function _probeServer() {
    fetch('/api/ping', { method: 'GET', cache: 'no-store' })
        .then(function(r) {
            if (r.ok) {
                _serverFailCount = 0;
                // Server reachability implies internet reachability.
                // Refresh the gstatic-derived flag so a subsequent stale
                // probe doesn't flash the wifi overlay.
                _internetReachable = true;
                if (!_serverReachable) {
                    _serverReachable = true;
                    _runHealthChecks();
                }
            } else if (r.status === 503) {
                // A 503 is the reviver DEFINITIVELY announcing "VibeNode is down
                // and I have taken over 5050." That is not a transient blip, so
                // reload IMMEDIATELY — no threshold. This is what makes death
                // detection near-instant even when you are staring at the screen
                // (no foreground event to trigger the decisive check): the very
                // next 3s poll gets a 503 and reloads to the Start page.
                _serverReachable = false;
                _recoverToStartPage();
            } else {
                // 502 / other = web gone, reviver NOT up yet. Do NOT reload —
                // a reload here lands on Tailscale's JS-less 502 page and
                // strands the phone (reproduced: "sits there until I manually
                // refresh"). Just surface the overlay after a small threshold
                // and keep polling; the 503 branch above reloads us into the
                // Start page the instant the reviver binds 5050.
                _serverFailCount++;
                if (_serverFailCount >= _SERVER_FAIL_THRESHOLD && _serverReachable) {
                    _serverReachable = false;
                    _runHealthChecks();
                }
            }
        })
        .catch(function() {
            // Connection refused / timeout = nobody serving 5050. Same policy
            // as 502: overlay after a small threshold, keep polling, NEVER a
            // reload into a dead 502.
            _serverFailCount++;
            if (_serverFailCount >= _SERVER_FAIL_THRESHOLD && _serverReachable) {
                _serverReachable = false;
                _runHealthChecks();
            }
        });
}

registerHealthCheck('server-reachable', {
    label: 'VibeNode Server Unreachable',
    icon: '<svg width="64" height="64" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"><rect x="2" y="3" width="20" height="14" rx="2" ry="2"/><line x1="8" y1="21" x2="16" y2="21"/><line x1="12" y1="17" x2="12" y2="21"/><line x1="2" y1="9" x2="22" y2="9"/></svg>',
    message: 'The VibeNode web server is not responding. Your running sessions are likely safe in the session daemon — relaunch VibeNode from your desktop shortcut or launcher to reconnect. If this overlay persists after relaunch, check logs/_server.log.',
    test: function() { return _serverReachable; }
});

// 3s cadence. Death is caught on the next poll after the reviver takes over
// (immediate reload on its 503), so a foregrounded phone recovers in ~3s even
// with no foreground event to fire the decisive check. A genuine in-app restart
// is still safe: modals.js owns its own overlay and _recoverToStartPage/the
// decisive check both bail when #restart-overlay is present.
_probeServer();
setInterval(_probeServer, 3000);

// Every path back to the foreground runs the DECISIVE check (single probe,
// immediate reload if the backend is gone). We bind ALL of visibilitychange,
// pageshow, focus, and online because no single one fires reliably across iOS
// Safari, Android Chrome, and standalone PWA/webview — belt and suspenders so
// foregrounding ALWAYS re-checks and self-recovers with no manual refresh.
document.addEventListener('visibilitychange', function() {
    if (document.visibilityState === 'visible') {
        _decisiveServerCheck();
        _decisiveDaemonCheck();
        _probeInternet();
    }
});
window.addEventListener('pageshow', function() { _decisiveServerCheck(); _decisiveDaemonCheck(); });
window.addEventListener('focus', function() { _decisiveServerCheck(); });
window.addEventListener('online', function() { _decisiveServerCheck(); });

/* ---- Built-in: Session daemon reachable (web up, daemon down) ----
 *
 * VibeNode's SECOND "up but broken" failure mode: the web server (5050) stays
 * alive while the session DAEMON (5051) dies — a crash, or an agent killing it
 * under load (the exact thing this whole feature exists for). In that state the
 * UI loads and /api/ping returns 200, so the server-reachable check above is
 * happy — but nothing actually works. Previously the user got only a fleeting
 * 'Lost connection to daemon' toast over a dead UI, with no way to recover from
 * a phone. This check turns that into a real blocking overlay with a Restart
 * button. /api/health reports the daemon's connection state (see main.py).
 *
 * Registered AFTER server-reachable so a full web outage (which fails BOTH
 * checks) shows the more fundamental 'server unreachable' overlay first.
 */
var _daemonReachable = true;
var _daemonFailCount = 0;
// Raised from 3 → 5 (2026-07-20). At 5s cadence that's ~25s before the
// "Engine Stopped" overlay fires from steady-state polling. Same rationale
// as the server threshold: mobile Tailscale handoffs and phone-lock wake
// windows can look like brief daemon-unreachable spells; the old 15s window
// clipped them. The decisive foreground check (with its own retry) still
// catches a truly-dead daemon within ~2s of a foreground event.
var _DAEMON_FAIL_THRESHOLD = 5;
var _daemonRestarting = false;    // set while our Restart button drives a reboot

function _probeDaemon() {
    // Don't fight an in-app restart (modals.js owns that overlay) or our own
    // restart-in-progress.
    if (document.getElementById('restart-overlay') || _daemonRestarting) return;
    fetch('/api/health', { method: 'GET', cache: 'no-store' })
        .then(function(r) {
            if (!r.ok) return;   // web itself down — server-reachable handles it
            return r.json();
        })
        .then(function(d) {
            if (!d) return;
            if (d.daemon) {
                _daemonFailCount = 0;
                if (!_daemonReachable) { _daemonReachable = true; _runHealthChecks(); }
            } else {
                _daemonFailCount++;
                if (_daemonFailCount >= _DAEMON_FAIL_THRESHOLD && _daemonReachable) {
                    _daemonReachable = false;
                    _runHealthChecks();
                }
            }
        })
        .catch(function() { /* web unreachable — server-reachable check owns this */ });
}

// DECISIVE daemon check for foreground events — same reasoning as
// _decisiveServerCheck: on foreground, evaluate quickly rather than waiting
// for the 3-strike interval that mobile timer-freezing makes useless. Uses
// a two-probe pattern (one retry ~1.5s later) to absorb transient
// Tailscale/network hiccups where /api/health returns daemon:false for a
// beat during reconnection. If the web itself is down this no-ops and
// _decisiveServerCheck handles the reload.
function _decisiveDaemonCheck() {
    if (_daemonRestarting || _reloadingForDeath) return;
    if (document.getElementById('restart-overlay')) return;
    _decisiveDaemonProbe(false);
}

function _decisiveDaemonProbe(isRetry) {
    fetch('/api/health', { method: 'GET', cache: 'no-store' })
        .then(function(r) { return r.ok ? r.json() : null; })
        .then(function(d) {
            if (!d) return;   // web down — server check owns it
            if (d.daemon) {
                _daemonFailCount = 0;
                if (!_daemonReachable) { _daemonReachable = true; _runHealthChecks(); }
            } else if (!isRetry) {
                setTimeout(function() { _decisiveDaemonProbe(true); }, 1500);
            } else {
                _daemonReachable = false;
                _runHealthChecks();
            }
        })
        .catch(function() { /* web unreachable — server check owns it */ });
}

registerHealthCheck('daemon-reachable', {
    label: 'VibeNode Engine Stopped',
    icon: '<svg width="64" height="64" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"><path d="M12 2v10"/><path d="M18.4 6.6a9 9 0 1 1-12.8 0"/></svg>',
    message: 'The VibeNode engine that runs your sessions has stopped. Tap Restart to bring it back — your session history is safe.',
    test: function() { return _daemonReachable; },
    action: {
        text: 'Restart VibeNode',
        onClick: function() {
            var btn = document.querySelector('#health-blocker .hb-btn');
            if (btn) { btn.disabled = true; btn.textContent = 'Restarting…'; }
            _daemonRestarting = true;
            // Full clean restart: brings the daemon (and web) back to a known-good
            // state. The reload afterward hits either the booting server (app.js
            // retries until /api/projects answers) or the reviver's Start page —
            // both recover on their own.
            fetch('/api/restart', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ scope: 'both' })
            }).catch(function() { /* expected: server goes down mid-request */ });
            // Poll /api/health until the stack is FULLY ready (web + daemon),
            // then reload into a working app instead of a half-booted one.
            var waited = 0;
            var iv = setInterval(function() {
                waited += 2;
                fetch('/api/health', { cache: 'no-store' })
                    .then(function(r) { return r.ok ? r.json() : null; })
                    .then(function(d) {
                        if (d && d.daemon) {
                            clearInterval(iv);
                            window.location.reload();
                        }
                    })
                    .catch(function() { /* still down — keep waiting */ });
                if (waited > 90) { clearInterval(iv); window.location.reload(); }
            }, 2000);
        }
    }
});

_probeDaemon();
setInterval(_probeDaemon, 5000);
document.addEventListener('visibilitychange', function() {
    if (document.visibilityState === 'visible') _probeDaemon();
});

// Socket push gives an instant signal too — when the web server reports the
// daemon reconnected, clear our failure state immediately (don't wait for the
// next poll). socket.js fires window event 'vn-daemon-status' (added there).
window.addEventListener('vn-daemon-status', function(e) {
    var up = e && e.detail && e.detail.up;
    if (up) { _daemonFailCount = 0; if (!_daemonReachable) { _daemonReachable = true; _daemonRestarting = false; _runHealthChecks(); } }
});

/* ---- Built-in: Claude Code auth ---- */

var _claudeLoggedIn = true; // assume ok until first poll says otherwise

(function _pollAuthStatus() {
    fetch('/api/auth-status')
        .then(function(r) { return r.json(); })
        .then(function(d) {
            var prev = _claudeLoggedIn;
            _claudeLoggedIn = !!d.loggedIn;
            // Immediately re-evaluate if state changed
            if (prev !== _claudeLoggedIn) _runHealthChecks();
        })
        .catch(function() { /* can't reach our own server — wifi check will handle it */ });
    setTimeout(_pollAuthStatus, _healthBlocking ? 5000 : 30000);
})();

registerHealthCheck('claude-auth', {
    label: 'Not Logged In to Claude',
    icon: '<svg width="64" height="64" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"><rect x="3" y="11" width="18" height="11" rx="2" ry="2"/><path d="M7 11V7a5 5 0 0 1 10 0v4"/><line x1="12" y1="16" x2="12" y2="19"/></svg>',
    message: 'VibeNode requires an active Claude Code login. Click below to open the login flow, then come back here — it will reconnect automatically.',
    test: function() { return _claudeLoggedIn; },
    action: {
        text: 'Log In to Claude',
        onClick: function() {
            fetch('/api/auth-login', { method: 'POST' });
        }
    }
});
