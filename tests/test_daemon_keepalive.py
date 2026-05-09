"""Regression tests for daemon-client sleep/resume resilience.

The web server connects to the session daemon over a loopback TCP socket.
Before this fix, a Linux suspend/resume cycle left the connection in a
zombie state that the application could not detect — recv() blocked
forever and the user had to kill and restart the entire server to recover.

Two defenses live in `app/daemon_client.py` and `daemon/daemon_server.py`:

  1. ``_enable_tcp_keepalive`` — sets SO_KEEPALIVE plus tightened
     TCP_KEEPIDLE / TCP_KEEPINTVL / TCP_KEEPCNT timers (Linux) or
     TCP_KEEPALIVE (macOS).  Both client and daemon enable it.

  2. A heartbeat thread on the client that pings the daemon every
     HEARTBEAT_INTERVAL seconds and forcibly disconnects on failure,
     which wakes the blocked reader thread and triggers reconnect.

These tests pin both behaviors so a future "simplification" can't
silently revert the fix.
"""

import socket

import pytest

from app import daemon_client


def _make_pair():
    """Return a (server_accepted, client) socket pair for testing.

    Uses a real loopback connection so platform-specific socket
    options actually take effect (mocking would not validate the fix).
    """
    server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server.bind(("127.0.0.1", 0))
    server.listen(1)
    port = server.getsockname()[1]

    client = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    client.connect(("127.0.0.1", port))
    accepted, _ = server.accept()
    server.close()
    return accepted, client


def test_enable_tcp_keepalive_sets_so_keepalive():
    """SO_KEEPALIVE must be enabled — that's the one universal flag."""
    accepted, client = _make_pair()
    try:
        daemon_client._enable_tcp_keepalive(client)
        val = client.getsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE)
        assert val != 0, (
            "SO_KEEPALIVE must be enabled on the daemon-client socket. "
            "Without it, Linux sleep/resume leaves the connection in a "
            "zombie state and the UI hangs until the server is restarted."
        )
    finally:
        client.close()
        accepted.close()


@pytest.mark.skipif(
    not hasattr(socket, "TCP_KEEPIDLE"),
    reason="TCP_KEEPIDLE is Linux-specific",
)
def test_enable_tcp_keepalive_tightens_linux_timers():
    """On Linux, the keepalive timers must be tightened from defaults.

    The kernel default is ~2 hours idle + 75s probe window.  The fix
    tightens this to ~30s total so the UI recovers promptly after a
    suspend/resume cycle.  If anyone relaxes these values back toward
    the kernel defaults, the original bug returns.
    """
    accepted, client = _make_pair()
    try:
        daemon_client._enable_tcp_keepalive(client)
        idle = client.getsockopt(socket.IPPROTO_TCP, socket.TCP_KEEPIDLE)
        intvl = client.getsockopt(socket.IPPROTO_TCP, socket.TCP_KEEPINTVL)
        cnt = client.getsockopt(socket.IPPROTO_TCP, socket.TCP_KEEPCNT)
        # Allow some slack but reject anything close to kernel defaults.
        assert idle <= 30, f"TCP_KEEPIDLE={idle}s — too slow for sleep/resume"
        assert intvl <= 10, f"TCP_KEEPINTVL={intvl}s — probes too far apart"
        assert cnt <= 5, f"TCP_KEEPCNT={cnt} — too many probes before giving up"
    finally:
        client.close()
        accepted.close()


def test_daemon_server_uses_same_helper():
    """The daemon side must enable keepalive on accepted clients too.

    Otherwise the daemon holds the dead socket open after a sleep/
    resume and refuses to release the slot, blocking reconnection.
    """
    from daemon import daemon_server

    accepted, client = _make_pair()
    try:
        daemon_server._enable_tcp_keepalive(accepted)
        val = accepted.getsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE)
        assert val != 0, (
            "Daemon-side accepted sockets must also have SO_KEEPALIVE. "
            "Without it, the daemon hangs onto zombie peers after sleep."
        )
    finally:
        client.close()
        accepted.close()


def test_force_disconnect_closes_socket_and_starts_reconnect(monkeypatch):
    """_force_disconnect must close the socket and trigger reconnect.

    The reader thread is blocked on recv(); only a shutdown()/close()
    will unblock it.  And without _start_reconnect being called, the
    UI would stay disconnected forever.
    """
    accepted, client = _make_pair()
    dc = daemon_client.DaemonClient()
    dc._sock = client
    dc._connected = True
    dc._should_run = True

    started = {"count": 0}

    def fake_start_reconnect():
        started["count"] += 1

    monkeypatch.setattr(dc, "_start_reconnect", fake_start_reconnect)

    dc._force_disconnect()

    assert dc._connected is False, "Must mark disconnected"
    assert dc._sock is None, "Must clear the socket reference"
    assert started["count"] == 1, "Must trigger reconnect"

    # The original socket should now be closed.
    with pytest.raises(OSError):
        client.send(b"x")

    accepted.close()


def test_heartbeat_constants_are_reasonable():
    """Sanity bounds on heartbeat tuning — guards against bad edits.

    Too long an interval defeats the purpose (sleep/resume detection
    becomes slow).  Too short a timeout could cause spurious reconnects
    on a momentarily busy daemon.
    """
    assert 5 <= daemon_client.HEARTBEAT_INTERVAL <= 60
    assert 2 <= daemon_client.HEARTBEAT_TIMEOUT <= 30
    # Timeout must be shorter than the interval — otherwise overlapping
    # probes can pile up if the daemon momentarily stalls.
    assert daemon_client.HEARTBEAT_TIMEOUT < daemon_client.HEARTBEAT_INTERVAL
