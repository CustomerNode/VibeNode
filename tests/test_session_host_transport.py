"""Hosted SDK client end to end (spec step 4, docs/plans/session-host-spec.md).

``ClaudeAgentSDK.create_session(hosted=True)`` returns a ``HostedSDKClient``
whose CLI (tests/fake_cli.py) runs in a real Session Host process.  The SDK's
own ``Query`` runs on top of it: initialize, a turn, a permission prompt
through the engine's callback, then shutdown through ``disconnect``.  A second
``connect()`` to the same host is answered from the initialize cache.  Then
the same through a real ``SessionManager``.
"""

import asyncio
import threading
import time

import pytest

from daemon import session_host_protocol as shp
from daemon import session_host_transport as sht
from daemon.backends.base import PermissionAction, PermissionResult, SessionOptions
from daemon.backends.claude import ClaudeAgentSDK
from daemon.backends.claude_host import HostedSDKClient, HostTransport
from daemon.backends.messages import MessageKind
from tests._session_host_helpers import make_fake_cli, pin_preferences, read_log, wait_until


@pytest.fixture
def fake(tmp_path, monkeypatch):
    cwd = tmp_path / "proj"
    cwd.mkdir()
    log = tmp_path / "cli.log"
    sdk = ClaudeAgentSDK()
    sdk.apply_patches()
    sdk.host_cli_path = make_fake_cli(tmp_path / "bin", FAKE_CLI_LOG=str(log))
    pin_preferences(monkeypatch)
    return {"sdk": sdk, "cwd": cwd, "log": log}


async def _turn(sdk, client, prompt):
    await sdk.send_query(client, prompt)
    out = []
    async for m in sdk.receive_response(client):
        out.append(m)
    return out


def _texts(msgs):
    return [b.get("text") for m in msgs if m.kind == MessageKind.ASSISTANT
            for b in m.blocks if b.get("kind") == "text"]


def test_hosted_turn_and_permission_end_to_end(fake):
    sdk = fake["sdk"]
    asked = []

    async def allow(tool_name, tool_input, context):
        asked.append(tool_name)
        return PermissionResult(action=PermissionAction.ALLOW, updated_input=tool_input)

    async def go():
        client = await sdk.create_session(SessionOptions(
            cwd=str(fake["cwd"]), hosted=True, host_sid="t-e2e", permission_callback=allow))
        assert isinstance(client, HostedSDKClient)
        await sdk.connect(client)
        rec = sdk.host_record(client)
        assert rec and rec["sid"] == "t-e2e" and shp.read_record("t-e2e")
        assert sdk.extract_process_pid(client) == rec["cli_pid"] > 1
        assert sdk.is_transport_alive(client)
        assert sdk.supported_models(client)[0]["value"] == "opus"   # initialize ran
        assert _texts(await _turn(sdk, client, "hello")) == ["echo: hello"]
        msgs = await _turn(sdk, client, "perm")
        assert asked == ["Bash"] and "permission granted" in _texts(msgs)
        assert msgs[-1].kind == MessageKind.RESULT
        await sdk.disconnect(client)                        # sends the host "shutdown"
        return rec
    rec = asyncio.run(go())
    assert sht.wait_host_gone(rec, 10)
    assert wait_until(lambda: not shp.process_alive(rec["cli_pid"]), 5)
    assert not shp.record_path("t-e2e").exists()


def test_second_connect_is_answered_from_the_initialize_cache(fake):
    sdk = fake["sdk"]

    async def go():
        a = await sdk.create_session(SessionOptions(cwd=str(fake["cwd"]), hosted=True,
                                                    host_sid="t-cache"))
        await sdk.connect(a)
        rec = dict(sdk.host_record(a))
        models_a = sdk.supported_models(a)
        a._transport.abort_connection()                    # engine killed
        await sdk.disconnect(a)
        b = await sdk.create_session(SessionOptions(cwd=str(fake["cwd"]), attach=rec))
        await sdk.connect(b)
        assert sdk.host_hello(b)["init_cached"]
        assert sdk.supported_models(b) == models_a
        sdk.host_release_replay(b, ())
        assert _texts(await _turn(sdk, b, "again")) == ["echo: again"]
        assert sdk.extract_process_pid(b) == rec["cli_pid"]
        await sdk.disconnect(b)
        return rec
    rec = asyncio.run(go())
    inits = [e for e in read_log(fake["log"]) if e.get("subtype") == "initialize"]
    assert len(inits) == 1
    assert sht.wait_host_gone(rec, 10)


def test_detach_leaves_the_cli_running(fake):
    sdk = fake["sdk"]

    async def go():
        c = await sdk.create_session(SessionOptions(cwd=str(fake["cwd"]), hosted=True,
                                                    host_sid="t-detach"))
        await sdk.connect(c)
        rec = dict(sdk.host_record(c))
        await sdk.host_detach(c)
        return rec
    rec = asyncio.run(go())
    time.sleep(0.5)
    assert shp.process_alive(rec["cli_pid"]) and sht.wait_host_gone(rec, 0.5) is False
    on_disk = shp.read_record("t-detach")
    assert on_disk and on_disk["detached_at"] > 0


def test_mock_clients_never_look_hosted():
    from unittest.mock import MagicMock
    sdk = ClaudeAgentSDK()
    c = MagicMock()
    assert sdk.host_record(c) is None and sdk.host_hello(c) == {}
    assert sdk._host_transport(c) is None


def test_hosted_switch_off_keeps_the_in_process_client(fake, monkeypatch):
    from claude_code_sdk import ClaudeSDKClient
    sdk = fake["sdk"]
    c = asyncio.run(sdk.create_session(SessionOptions(cwd=str(fake["cwd"]))))
    assert type(c) is ClaudeSDKClient


# ── Through a real SessionManager ─────────────────────────────────────────

class Pushes:
    def __init__(self):
        self.events = []
        self.lock = threading.Lock()

    def __call__(self, event, data, *a, **k):
        with self.lock:
            self.events.append((event, data))

    def of(self, name):
        with self.lock:
            return [d for e, d in self.events if e == name]


def _make_sm(fake):
    from daemon.session_manager import SessionManager
    sm = SessionManager(sdk=fake["sdk"])
    push = Pushes()
    sm.start(push)
    return sm, push


def _state(sm, sid):
    return sm.get_session_state(sid)


def test_session_manager_runs_a_hosted_session(fake, monkeypatch):
    monkeypatch.setenv("VIBENODE_SESSION_HOST", "1")
    sm, push = _make_sm(fake)
    try:
        assert sm.start_session("temp-sm-1", prompt="hello", cwd=str(fake["cwd"]))["ok"]
        sid = wait_until(lambda: sm._resolve_id("temp-sm-1") != "temp-sm-1"
                         and sm._resolve_id("temp-sm-1"), 30)
        assert sid, "the CLI id was never adopted"
        assert wait_until(lambda: _state(sm, sid) == "idle", 30)
        info = sm._sessions[sid]
        assert sm._is_hosted(info)
        texts = [e["text"] for e in sm.get_entries(sid)]
        assert "echo: hello" in texts
        # The host refiled its record under the adopted id.
        assert wait_until(lambda: shp.read_record(sid), 5)
        assert not shp.record_path("temp-sm-1").exists()
        rec = dict(info._host_record)
        assert sm.send_message(sid, "second")["ok"]
        assert wait_until(lambda: "echo: second" in [e["text"] for e in sm.get_entries(sid)], 20)
        assert wait_until(lambda: _state(sm, sid) == "idle", 20)
        sm.close_session_sync(sid, timeout=30)
        assert sht.wait_host_gone(rec, 10)
        assert wait_until(lambda: not shp.process_alive(rec["cli_pid"]), 5)
        assert not shp.record_path(sid).exists()
    finally:
        sm.stop()


def test_session_manager_switch_off_is_unchanged(fake, monkeypatch):
    monkeypatch.setenv("VIBENODE_SESSION_HOST", "0")
    sm, _ = _make_sm(fake)
    try:
        assert not sm._host_wanted("")
        monkeypatch.setenv("VIBENODE_SESSION_HOST", "1")
        assert sm._host_wanted("") and not sm._host_wanted("planner")
        assert not sm._host_wanted("title")
    finally:
        sm.stop()
