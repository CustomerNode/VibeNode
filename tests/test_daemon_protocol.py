"""Versioned IPC between the web server and the Session Engine.

The engine outlives web restarts and app updates, so the web routinely talks
to an engine older than itself.  Before the handshake, a method the engine
lacked came back as the raw string "Unknown method: ..." and only
``set_session_effort`` knew to translate it.  Now ``ping`` advertises the
engine's protocol version and method list, and the client answers any method
the engine does not list with one typed "unavailable" result.

The client tests run the real ``DaemonClient`` against a fake engine on a
real local socket, so the handshake, the reader thread and the request path
are all exercised.  Nothing here talks to the user's engine on 5051.
"""

import json
import re
import socket
import threading
import time
from pathlib import Path

import pytest

import app.daemon_client as dc
from app.daemon_client import DaemonClient

_ROOT = Path(__file__).resolve().parents[1]


# ── Engine side ───────────────────────────────────────────────────────────

def test_methods_are_the_dispatch_table_plus_the_blocking_method():
    from daemon import daemon_server as ds
    table = set(ds.build_handlers(ds._MethodNamesOnly()))
    assert set(ds.METHODS) == table | {"hook_pre_tool"}
    assert "ping" in ds.METHODS and "get_dormant_states" in ds.METHODS


def test_ping_reports_version_methods_and_start_time():
    from daemon import daemon_server as ds
    p = ds.ping_payload()
    assert p["ok"] is True                         # the heartbeat reads this
    assert isinstance(p["pid"], int)
    assert p["protocol_version"] == ds.PROTOCOL_VERSION >= 1
    assert p["methods"] == list(ds.METHODS)
    assert p["engine_started_at"] <= time.time()


def test_client_expects_the_engines_version_and_uses_only_listed_methods():
    from daemon import daemon_server as ds
    assert dc.EXPECTED_PROTOCOL_VERSION == ds.PROTOCOL_VERSION
    assert dc.CLIENT_RPCS <= set(ds.METHODS)


def test_client_rpc_list_matches_what_the_client_sends():
    src = (_ROOT / "app" / "daemon_client.py").read_text(encoding="utf-8")
    sent = set(re.findall(r"_send_request\(\s*[\"']([A-Za-z_][A-Za-z0-9_]*)[\"']", src))
    assert sent == set(dc.CLIENT_RPCS)


# ── A fake engine on a real socket ────────────────────────────────────────

class FakeEngine:
    """Answers JSON-line requests like daemon_server does.

    ``ping`` is what the engine advertises; methods outside ``handles`` get
    the real engine's "Unknown method" error.
    """

    def __init__(self, ping, handles):
        self.ping = ping
        self.handles = set(handles)
        self.received = []
        self._srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._srv.bind(("127.0.0.1", 0))
        self._srv.listen(4)
        self.port = self._srv.getsockname()[1]
        self._stop = False
        threading.Thread(target=self._accept, daemon=True).start()

    def _accept(self):
        while not self._stop:
            try:
                conn, _ = self._srv.accept()
            except OSError:
                return
            threading.Thread(target=self._serve, args=(conn,), daemon=True).start()

    def _serve(self, conn):
        buf = b""
        while True:
            try:
                data = conn.recv(65536)
            except OSError:
                return
            if not data:
                return
            buf += data
            while b"\n" in buf:
                line, buf = buf.split(b"\n", 1)
                msg = json.loads(line)
                method = msg["method"]
                self.received.append(method)
                if method == "ping":
                    resp = {"req_id": msg["req_id"], "result": self.ping}
                elif method in self.handles:
                    resp = {"req_id": msg["req_id"], "result": {"ok": True, "method": method}}
                else:
                    resp = {"req_id": msg["req_id"], "error": f"Unknown method: {method}"}
                conn.sendall((json.dumps(resp) + "\n").encode())

    def close(self):
        self._stop = True
        self._srv.close()


@pytest.fixture
def engine_and_client(monkeypatch):
    made = []

    def make(ping, handles):
        engine = FakeEngine(ping, handles)
        monkeypatch.setattr(dc, "DAEMON_PORT", engine.port)
        client = DaemonClient()
        client.start(socketio=None)
        deadline = time.time() + 5
        # The connect-time resync is ping, then aliases, then policy.
        while "get_permission_policy" not in engine.received and time.time() < deadline:
            time.sleep(0.02)
        time.sleep(0.05)
        made.append((engine, client))
        return engine, client

    yield make
    for engine, client in made:
        client.stop()
        engine.close()


def test_engine_listing_fewer_methods_gets_a_typed_unavailable(engine_and_client, caplog):
    ping = {"ok": True, "pid": 4242, "protocol_version": 1,
            "methods": ["ping", "get_aliases", "get_permission_policy", "get_session_state"],
            "engine_started_at": 1.0}
    with caplog.at_level("WARNING", logger="app.daemon_client"):
        engine, client = engine_and_client(ping, ping["methods"])
    info = client.protocol_info()
    assert info["known"] and info["protocol_version"] == 1 and info["pid"] == 4242
    assert client.supports("get_session_state") is True
    assert client.supports("get_dormant_states") is False

    sent_before = list(engine.received)
    result = client.set_session_effort("s1", "high")
    assert result["ok"] is False and result["live_unavailable"] is True
    assert client.get_dormant_states() == {}           # degrades like before
    r = client.set_session_model("s1", "m")
    assert r["live_unavailable"] is True and r["unavailable_method"] == "set_session_model"
    # Answered locally: none of those reached the engine.
    assert engine.received == sent_before
    # A listed method still goes through.
    assert client.get_session_state("s1") == {"ok": True, "method": "get_session_state"}
    # Exactly one skew line, naming what is missing.
    skew = [r for r in caplog.records if "protocol skew" in r.getMessage()]
    assert len(skew) == 1 and "get_dormant_states" in skew[0].getMessage()


def test_engine_older_than_the_handshake_still_works(engine_and_client, caplog):
    """Its ping has no version or method list: version 0, methods unknown.
    Everything is sent; an "Unknown method" reply becomes the typed result."""
    with caplog.at_level("WARNING", logger="app.daemon_client"):
        engine, client = engine_and_client(
            {"ok": True, "pid": 7}, {"get_aliases", "get_permission_policy", "get_session_state"})
    info = client.protocol_info()
    assert info["known"] and info["protocol_version"] == 0 and info["methods"] is None
    assert client.supports("get_dormant_states") is True     # optimistic
    assert client.get_session_state("s1") == {"ok": True, "method": "get_session_state"}

    result = client.set_session_effort("s1", "high")
    assert "set_session_effort" in engine.received           # it was asked
    assert result["ok"] is False and result["live_unavailable"] is True
    assert client.get_dormant_states() == {}
    r = client.close_session("s1")
    assert r["live_unavailable"] is True and r["error"].startswith("Unknown method")
    assert any("predates the handshake" in r.getMessage() for r in caplog.records)


def test_matching_engine_logs_no_skew(engine_and_client, caplog):
    from daemon import daemon_server as ds
    with caplog.at_level("WARNING", logger="app.daemon_client"):
        _, client = engine_and_client(ds.ping_payload(), ds.METHODS)
    assert client.supports("get_dormant_states")
    assert not [r for r in caplog.records if "protocol skew" in r.getMessage()]


def test_heartbeat_ping_refreshes_protocol_and_logs_once():
    client = DaemonClient.__new__(DaemonClient)
    client._protocol = None
    client._protocol_logged = None
    ping = {"ok": True, "pid": 1, "protocol_version": 0}
    client._note_protocol(ping)
    client._note_protocol(ping)
    assert client.protocol_info()["protocol_version"] == 0
    client._note_protocol({"ok": True, "pid": 2, "protocol_version": 1,
                           "methods": sorted(dc.CLIENT_RPCS)})
    assert client.protocol_info()["pid"] == 2
    assert client.supports("get_dormant_states")
    client._note_protocol({"ok": False})               # a failed ping changes nothing
    assert client.protocol_info()["pid"] == 2


def test_client_without_handshake_state_is_optimistic():
    """A client built without __init__ (as several tests do) must not crash."""
    client = DaemonClient.__new__(DaemonClient)
    assert client.supports("anything") is True
    assert client.protocol_info()["known"] is False
