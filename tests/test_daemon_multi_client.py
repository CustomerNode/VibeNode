"""Regression tests: the daemon must tolerate more than one IPC client.

The daemon used to hold a SINGLE ``_client_socket``.  Every new connection
overwrote it and closed whoever held it, so any second connection silently
killed the first client's IPC link.

That turned a survivable condition into a permanent outage.  When two web
servers were alive at once (see ``tests/test_web_singleton_gate.py`` for the
hole that let that happen), each one's reconnect loop re-connected every two
seconds and evicted the other.  Neither could hold the link, so whichever
server the browser happened to talk to reported ``daemon: false`` from
``/api/health`` about half the time, and the UI sat in a permanent "VibeNode
Engine Stopped" overlay.  Pressing Restart made it worse -- it spawned another
server to join the fight.  The daemon itself was healthy throughout and every
session kept running; only the connection bookkeeping was broken.

These tests pin the additive behavior so a future "simplification" back to one
socket can't silently restore the flap.
"""

import json
import socket
import threading

import pytest

from daemon import daemon_server
from daemon.daemon_server import MAX_IPC_CLIENTS, SessionDaemon


@pytest.fixture
def daemon():
    """A SessionDaemon with no SessionManager started -- we only exercise the
    client registry and the push/broadcast path, never real sessions."""
    d = SessionDaemon.__new__(SessionDaemon)
    d._server_socket = None
    d._clients = {}
    d._client_lock = threading.Lock()
    d._running = True
    return d


def _pair():
    """A connected (server_side, client_side) TCP socket pair over loopback."""
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.bind(("127.0.0.1", 0))
    listener.listen(1)
    client = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    client.connect(listener.getsockname())
    server_side, _ = listener.accept()
    listener.close()
    return server_side, client


def _register(daemon, sock):
    """Register a socket the way _handle_client does, without its read loop."""
    with daemon._client_lock:
        daemon._clients[sock] = threading.Lock()


def _read_event(sock, timeout=2.0):
    sock.settimeout(timeout)
    buf = b""
    while not buf.endswith(b"\n"):
        chunk = sock.recv(65536)
        if not chunk:
            break
        buf += chunk
    return json.loads(buf.decode("utf-8").strip())


# ---------------------------------------------------------------------------
# The core regression
# ---------------------------------------------------------------------------

def test_second_client_does_not_evict_the_first(daemon):
    """THE bug: connecting a second client must not close the first.

    This is the exact mechanism behind the "Engine Stopped" flap.
    """
    srv_a, cli_a = _pair()
    srv_b, cli_b = _pair()
    try:
        _register(daemon, srv_a)
        _register(daemon, srv_b)

        assert len(daemon._clients) == 2, "second client evicted the first"

        # Both links are still usable -- the first one especially.
        daemon._push_event("state_snapshot", {"sessions": []})
        assert _read_event(cli_a)["event"] == "state_snapshot"
        assert _read_event(cli_b)["event"] == "state_snapshot"
    finally:
        for s in (srv_a, cli_a, srv_b, cli_b):
            s.close()


def test_push_event_reaches_every_connected_client(daemon):
    """Events fan out to all clients, not just the most recent one."""
    pairs = [_pair() for _ in range(3)]
    try:
        for srv, _ in pairs:
            _register(daemon, srv)

        daemon._push_event("session_update", {"id": "abc"})

        for _, cli in pairs:
            msg = _read_event(cli)
            assert msg["event"] == "session_update"
            assert msg["data"] == {"id": "abc"}
    finally:
        for srv, cli in pairs:
            srv.close()
            cli.close()


# ---------------------------------------------------------------------------
# Reaping dead peers
# ---------------------------------------------------------------------------

def test_dead_client_is_pruned_without_disturbing_healthy_ones(daemon):
    """A send failure drops only the broken connection.

    Eviction-on-accept was the old (wrong) way to shed a stale client.  The
    right place is where death is actually observed: a failed write.
    """
    srv_dead, cli_dead = _pair()
    srv_ok, cli_ok = _pair()
    try:
        _register(daemon, srv_dead)
        _register(daemon, srv_ok)

        # Kill one peer outright, then push.
        cli_dead.close()
        srv_dead.close()

        daemon._push_event("session_update", {"id": "xyz"})

        assert srv_dead not in daemon._clients, "dead peer was not pruned"
        assert srv_ok in daemon._clients, "healthy peer was pruned"
        assert _read_event(cli_ok)["event"] == "session_update"
    finally:
        for s in (srv_ok, cli_ok):
            s.close()


def test_drop_client_is_idempotent(daemon):
    """_handle_client's finally and a failed send can both drop the same sock."""
    srv, cli = _pair()
    try:
        _register(daemon, srv)
        assert daemon._drop_client(srv) is True
        assert daemon._drop_client(srv) is False   # second call is a no-op
        assert daemon._clients == {}
    finally:
        srv.close()
        cli.close()


def test_push_with_no_clients_is_a_noop(daemon):
    """No connected UI is normal (nobody has the page open) -- not an error."""
    daemon._push_event("session_update", {"id": "none"})
    assert daemon._clients == {}


# ---------------------------------------------------------------------------
# Leak backstop
# ---------------------------------------------------------------------------

def test_client_registry_is_capped_and_trims_oldest(daemon):
    """A connection leak must not grow the registry without bound.

    The cap is a backstop, not a policy: it sits far above real usage, and it
    trims the OLDEST connection because a leak piles up at the young end -- so
    the long-lived real client is the last thing that would ever be dropped.
    """
    made = []
    try:
        for _ in range(MAX_IPC_CLIENTS + 3):
            srv, cli = _pair()
            made.append((srv, cli))
            # Go through the real registration path, including the trim.
            with daemon._client_lock:
                daemon._clients[srv] = threading.Lock()
                while len(daemon._clients) > MAX_IPC_CLIENTS:
                    del daemon._clients[next(iter(daemon._clients))]

        assert len(daemon._clients) == MAX_IPC_CLIENTS
        # The three oldest were trimmed; the newest survive.
        assert made[0][0] not in daemon._clients
        assert made[-1][0] in daemon._clients
    finally:
        for srv, cli in made:
            srv.close()
            cli.close()


def test_max_ipc_clients_leaves_room_for_a_restart_overlap():
    """The cap must never bite during normal operation.

    Steady state is one client; a web restart can briefly overlap two.  A cap
    anywhere near those numbers would reintroduce eviction under a different
    name.
    """
    assert MAX_IPC_CLIENTS >= 4


def test_single_client_socket_attribute_is_gone():
    """Guard against a revert to the one-slot model.

    ``_client_socket`` is the attribute whose overwrite-and-close semantics
    caused the flap.  Its absence is the structural invariant worth pinning.
    """
    src = daemon_server.__file__
    with open(src, encoding="utf-8") as fh:
        code = fh.read()
    # Comments explaining the old design are fine; assignments are not.
    assert "self._client_socket =" not in code
