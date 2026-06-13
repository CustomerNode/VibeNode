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
            _restartHealthPoll(3000);
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

/* ---- Built-in: WiFi / Internet connectivity ---- */

/*
 * Two-layer detection:
 *   1. navigator.onLine  — instant but unreliable on Windows (stays true if
 *      any adapter is up, even loopback)
 *   2. Real probe        — HEAD request to a known external URL to confirm
 *      actual internet reachability.  Falls back to navigator.onLine if the
 *      probe can't run yet.
 */
var _internetReachable = navigator.onLine;  // seed with browser hint

function _probeInternet() {
    // Tiny cacheless HEAD to a highly-available endpoint
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
    test: function() { return _internetReachable; }
});

// Browser events give us an instant trigger for the obvious cases
window.addEventListener('offline', function() {
    _internetReachable = false;
    _runHealthChecks();
});
window.addEventListener('online', function() {
    // Don't trust online event blindly — verify with a real probe
    _probeInternet();
});

// Run the real probe on load and then on the same cadence as the health poll
_probeInternet();
setInterval(_probeInternet, 60000);

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
var _SERVER_FAIL_THRESHOLD = 3;  // ~9-12s of consecutive failures

function _probeServer() {
    fetch('/api/ping', { method: 'GET', cache: 'no-store' })
        .then(function(r) {
            if (r.ok) {
                _serverFailCount = 0;
                if (!_serverReachable) {
                    _serverReachable = true;
                    _runHealthChecks();
                }
            } else {
                _serverFailCount++;
                if (_serverFailCount >= _SERVER_FAIL_THRESHOLD && _serverReachable) {
                    _serverReachable = false;
                    _runHealthChecks();
                }
            }
        })
        .catch(function() {
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

// Fixed 5s cadence × 3-failure threshold = ~15s before the overlay shows.
// That comfortably outlasts a normal /api/restart cycle (modals.js puts up
// its own full-page restart overlay during the gap), so a restart never
// flashes this overlay. A real outage flips within 15s.
_probeServer();
setInterval(_probeServer, 5000);

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
