"""A slow or unreachable Workflow board store never stalls the page.

The board can live in Supabase.  When that project stopped answering, every
session state change in the browser posted to /api/kanban/session-state-change
and waited on it; a browser opens at most six connections to one site, so the
tab's own requests (its health ping, opening a session, switching projects)
queued behind them: "Server Unreachable", slow opens, failed switches.

Three pieces, one test each: the route answers before touching the store, a
failed store connect is not retried by every caller for a while, and every
Supabase call has a time limit.
"""
import threading
import time
from unittest import mock

import pytest

from web import create_app
from web import db as db_mod
from web.routes import kanban_api


@pytest.fixture
def client():
    return create_app(testing=True).test_client()


def test_the_route_answers_while_the_store_hangs(client, monkeypatch):
    release = threading.Event()
    started = threading.Event()

    def hang(sid, state):
        started.set()
        release.wait(10)

    monkeypatch.setattr(kanban_api, "_state_bridge", kanban_api._StateBridge(hang))
    try:
        t = time.monotonic()
        for i in range(20):
            r = client.post("/api/kanban/session-state-change",
                            json={"session_id": "s%d" % (i % 3), "state": "working"})
            assert r.status_code == 202
        assert time.monotonic() - t < 2, "the route waited on the store"
        assert started.wait(5)
    finally:
        release.set()


def test_a_burst_for_one_session_keeps_only_the_newest_state():
    seen = []
    gate = threading.Event()

    def apply(sid, state):
        gate.wait(5)
        seen.append((sid, state))

    bridge = kanban_api._StateBridge(apply)
    bridge.submit("a", "working")          # taken by the worker, waits on gate
    time.sleep(0.1)
    for st in ("idle", "working", "stopped"):
        bridge.submit("a", st)
    gate.set()
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline and len(seen) < 2:
        time.sleep(0.02)
    time.sleep(0.1)
    assert seen == [("a", "working"), ("a", "stopped")]


def test_a_failed_store_connect_is_not_retried_by_every_caller(monkeypatch):
    calls = []

    class Broken:
        def __init__(self, **kw):
            pass

        def initialize(self):
            calls.append(1)
            raise ConnectionError("not answering")

    import web.db.supabase_backend as sb
    monkeypatch.setattr(sb, "SupabaseRepository", Broken)
    monkeypatch.setattr("web.config.get_kanban_config",
                        lambda: {"kanban_backend": "supabase",
                                 "supabase_url": "https://example.invalid",
                                 "supabase_secret_key": "made-up"})
    monkeypatch.setattr(db_mod, "_repo", None)
    monkeypatch.setattr(db_mod, "_failed_error", None)
    for _ in range(5):
        with pytest.raises(ConnectionError):
            db_mod.create_repository()
    assert len(calls) == 1
    monkeypatch.setattr(db_mod, "_failed_at", time.monotonic() - db_mod.FAILURE_PAUSE_S - 1)
    with pytest.raises(ConnectionError):
        db_mod.create_repository()
    assert len(calls) == 2


def test_every_supabase_call_has_a_time_limit(monkeypatch):
    import web.db.supabase_backend as sb
    got = {}

    def fake_create(url, key, options=None):
        got["options"] = options
        m = mock.MagicMock()
        return m

    monkeypatch.setattr(sb, "create_client", fake_create)
    sb.SupabaseRepository(url="https://example.invalid", key="made-up").initialize()
    assert got["options"] is not None
    assert got["options"].postgrest_client_timeout == sb.REQUEST_TIMEOUT_S
    assert sb.REQUEST_TIMEOUT_S <= 15
