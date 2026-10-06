"""The wake resync must never call ``socket.connect()`` on a connecting socket.

``_wakeSocketResync()`` in ``static/js/socket.js`` runs on visibilitychange /
pageshow / focus / online.  ``pageshow`` fires on EVERY page load, usually
before the Socket.IO handshake has finished (``handle_connect`` does a daemon
IPC first).  It used to call ``socket.connect()`` whenever ``socket.connected``
was false.  On a socket whose transport is open but whose namespace handshake is
still pending, that sends a SECOND ``CONNECT`` packet; python-socketio answers
the duplicate with ``CONNECT_ERROR`` ("Unable to connect"), and the Socket.IO
client then destroys the socket's listeners while ``socket.connected`` is true
from the first handshake.  The socket looks connected, still sends, and never
receives.

Measured on 2026-10-06: the first chat thread after a page load rendered at
about 20s (when the skeleton watchdog finally cycled the socket) before the
fix, and at about 0.4s after it.

Two layers:

* behaviour: the real function body is lifted out of ``socket.js`` and run in
  Node against a stub socket in each connection state;
* source guards, no Node needed: the guard is still in front of the call, and
  no new ``socket.connect()`` call site has appeared elsewhere.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
JS_DIR = ROOT / "static" / "js"
SOCKET_JS = JS_DIR / "socket.js"


def _function_source(src: str, name: str) -> str:
    """The full text of ``function <name>(...) { ... }``, braces matched."""
    start = src.index(f"function {name}(")
    i = src.index("{", start)
    depth = 0
    while i < len(src):
        if src[i] == "{":
            depth += 1
        elif src[i] == "}":
            depth -= 1
            if depth == 0:
                return src[start : i + 1]
        i += 1
    raise AssertionError(f"unbalanced braces in {name}")


def _wake_source() -> str:
    return _function_source(SOCKET_JS.read_text(encoding="utf-8"), "_wakeSocketResync")


# name -> (socket stub fields, connect() expected, snapshot emit expected)
STATES = {
    # The page-load case: transport open, namespace CONNECT sent, ack pending.
    # connect() here is what produced the deaf socket.
    "handshake pending": (
        {"connected": False, "active": True, "readyState": "open"}, False, False),
    # Transport itself still opening.
    "transport opening": (
        {"connected": False, "active": True, "readyState": "opening"}, False, False),
    # Gave up (server-side disconnect, or destroyed after an error): needs us.
    "inactive, transport open": (
        {"connected": False, "active": False, "readyState": "open"}, True, False),
    "inactive, transport closed": (
        {"connected": False, "active": False, "readyState": "closed"}, True, False),
    # Still wants to reconnect but the transport is down: connect() is safe
    # there (nothing is open to send a duplicate CONNECT on) and was the
    # original purpose of the wake path.
    "active, transport closed": (
        {"connected": False, "active": True, "readyState": "closed"}, True, False),
    # Healthy socket: passive refresh only, never a connect or a cycle.
    "connected": (
        {"connected": True, "active": True, "readyState": "open"}, False, True),
}

_HARNESS = r"""
const states = %s;
const out = {};
for (const name of Object.keys(states)) {
  const st = states[name];
  const calls = { connect: 0, disconnect: 0, emits: [] };
  const socket = {
    connected: st.connected,
    active: st.active,
    io: { _readyState: st.readyState },
    connect() { calls.connect++; return this; },
    disconnect() { calls.disconnect++; return this; },
    emit(ev) { calls.emits.push(ev); },
  };
  const localStorage = { getItem() { return 'proj'; } };
  const console = { log() {}, warn() {} };
  let _lastWakeResyncAt = 0;
  const _WAKE_DEBOUNCE_MS = 750;
  %s
  _wakeSocketResync();
  out[name] = calls;
}
process.stdout.write(JSON.stringify(out));
"""


@pytest.fixture(scope="module")
def results():
    node = shutil.which("node")
    if not node:
        pytest.skip("node not installed")
    script = _HARNESS % (
        json.dumps({k: v[0] for k, v in STATES.items()}),
        _wake_source(),
    )
    proc = subprocess.run([node, "-e", script], capture_output=True, text=True, timeout=30)
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout)


@pytest.mark.parametrize("state", list(STATES))
def test_wake_resync_per_connection_state(results, state):
    _, want_connect, want_snapshot = STATES[state]
    got = results[state]
    assert got["connect"] == (1 if want_connect else 0), (
        f"{state}: socket.connect() called {got['connect']}x. Calling it while the "
        f"handshake is pending sends a duplicate CONNECT and leaves a deaf socket."
    )
    assert got["disconnect"] == 0, f"{state}: the wake path must never cycle the socket"
    assert ("request_state_snapshot" in got["emits"]) == want_snapshot, (
        f"{state}: unexpected emits {got['emits']}"
    )


def test_guard_sits_in_front_of_the_connect_call():
    """Source-level backstop for machines without Node."""
    code = "\n".join(line.split("//")[0] for line in _wake_source().splitlines())
    assert code.count("socket.connect()") == 1, "exactly one connect() call expected"
    guard = re.search(r"if\s*\(\s*socket\.active\b[^)]*\)\s*return\s*;", code)
    assert guard, (
        "_wakeSocketResync lost its `if (socket.active && ...) return;` guard. "
        "Without it every page load double-connects and the first chat thread "
        "sits on its skeleton for ~16-20s."
    )
    assert guard.start() < code.index("socket.connect()"), (
        "the socket.active guard must come before socket.connect()"
    )


def test_no_new_bare_connect_call_sites():
    """Any other ``socket.connect()`` can reintroduce the duplicate CONNECT.

    Two call sites are known and safe: the wake path (guarded, tested above) and
    the skeleton watchdog's stage-2 cycle in live-panel.js, which calls
    ``socket.disconnect()`` first, so the transport is torn down and no
    handshake is pending.  A new one must be reviewed against this hazard and
    added here on purpose.
    """
    allowed = {"socket.js": 1, "live-panel.js": 1}
    found = {}
    for path in sorted(JS_DIR.glob("*.js")):
        n = 0
        for line in path.read_text(encoding="utf-8").splitlines():
            code = line.split("//")[0]
            n += len(re.findall(r"\bsocket\.connect\(\)", code))
        if n:
            found[path.name] = n
    assert found == allowed, (
        f"socket.connect() call sites changed: {found} (expected {allowed}). "
        "See the module docstring before adding one."
    )

    live = (JS_DIR / "live-panel.js").read_text(encoding="utf-8")
    assert re.search(r"socket\.disconnect\(\);\s*socket\.connect\(\)", live), (
        "live-panel.js must disconnect before it connects when cycling the socket"
    )
