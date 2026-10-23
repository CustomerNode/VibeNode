"""A real Session Host process running the fake CLI (tests/fake_cli.py).

Spec step 3 (docs/plans/session-host-spec.md): hello/secret refusal, buffer
and ack, replay after a crash, the initialize cache, orphan exit, shutdown
kills the CLI tree, the record lifecycle.  Runs unskipped on Windows and
POSIX; nothing here touches the real Claude CLI.
"""

import asyncio
import os
import time

import pytest

from daemon import session_host_protocol as shp
from daemon import session_host_transport as sht
from tests._session_host_helpers import (
    RawClient, is_type, launch_host, make_fake_cli, msg_of, read_log, texts, wait_until,
)


@pytest.fixture
def env(tmp_path):
    cwd = tmp_path / "proj"
    cwd.mkdir()
    log = tmp_path / "cli.log"
    return {"cwd": cwd, "log": log, "bin": tmp_path / "bin", "tmp": tmp_path}


def _cli(env, **extra):
    return make_fake_cli(env["bin"], FAKE_CLI_LOG=str(env["log"]), **extra)


def _init_frame(rid):
    return {"type": "control_request", "request_id": rid,
            "request": {"subtype": "initialize", "hooks": None}}


def test_record_lifecycle_hello_turn_and_shutdown(env):
    rec = launch_host("h-life", env["cwd"], _cli(env))
    assert not shp.launch_path("h-life").exists()           # the secret-bearing spec is consumed
    on_disk = shp.read_record("h-life")
    assert on_disk["port"] == rec["port"] and on_disk["proto"] == shp.PROTO
    assert shp.same_process(rec["cli_pid"], rec["cli_create_time"], rec["cli_name"])
    assert shp.same_process(rec["host_pid"], rec["host_create_time"], rec["host_name"])

    async def go():
        c = RawClient(rec)
        hello = await c.connect()
        assert hello["t"] == "hello_ok" and hello["cli_pid"] == rec["cli_pid"]
        assert hello["replay_count"] == 0 and not hello["turn_in_flight"]
        await c.write_line(_init_frame("req_1"))
        await c.until(is_type("control_response"))
        await c.user("hi")
        frames = await c.until(is_type("result"))
        assert texts(frames) == ["echo: hi"]
        assert all(f["seq"] > 0 for f in frames if f["t"] == "msg")
        await c.send({"t": "shutdown"})
        await c.until(lambda f: f.get("t") == "bye")
    asyncio.run(go())
    assert wait_until(lambda: not shp.record_path("h-life").exists(), 5)
    assert sht.wait_host_gone(rec, 5)
    assert wait_until(lambda: not shp.process_alive(rec["cli_pid"]), 5)


def test_bad_secret_and_missing_hello_are_refused(env):
    rec = launch_host("h-auth", env["cwd"], _cli(env))

    async def go():
        bad = RawClient(rec)
        hello = await bad.connect(secret="0" * 64)
        assert hello == {"t": "hello_err", "reason": "auth"}
        assert await bad.frame(5) is None                     # closed
        # Garbage first frame: refused too.
        r, w = await asyncio.open_connection("127.0.0.1", rec["port"])
        w.write(shp.encode({"t": "write", "line": "{}"}))
        await w.drain()
        assert (await shp.read_frame(r))["t"] == "hello_err"
        w.close()
        good = RawClient(rec)
        assert (await good.connect())["t"] == "hello_ok"     # the host is unharmed
        good.abort()
    asyncio.run(go())


def test_crash_mid_turn_then_reattach_replays_the_rest(env):
    rec = launch_host("h-replay", env["cwd"], _cli(env))

    async def go():
        a = RawClient(rec)
        await a.connect()
        await a.write_line(_init_frame("req_1"))
        await a.until(is_type("control_response"))
        await a.user("slow 6")
        await a.until(is_type("assistant"))                   # chunk 0 seen
        a.abort()                                             # engine killed: no detach frame
        await asyncio.sleep(2.0)                              # the CLI keeps going meanwhile
        rec2 = shp.read_record("h-replay")
        assert rec2["detached_at"] > 0 and not rec2["attached"]
        b = RawClient(rec)
        hello = await b.connect()
        assert hello["replay_has_result"] and not hello["turn_in_flight"]
        frames = await b.until(is_type("result"))
        replayed = [f for f in frames if f.get("r")]
        assert replayed and len(replayed) == len([f for f in frames if f["t"] == "msg"])
        got = texts(frames)
        assert "chunk 5" in got and "chunk 0" not in got      # only what came after the crash
        b.abort()
    asyncio.run(go())


def test_initialize_is_answered_from_cache_after_the_first(env):
    rec = launch_host("h-init", env["cwd"], _cli(env))

    async def go():
        a = RawClient(rec)
        await a.connect()
        await a.write_line(_init_frame("req_A"))
        first = msg_of((await a.until(is_type("control_response")))[-1])
        a.abort()
        await asyncio.sleep(0.3)
        b = RawClient(rec)
        assert (await b.connect())["init_cached"]
        await b.write_line(_init_frame("req_B"))
        again = msg_of((await b.until(is_type("control_response")))[-1])
        assert again["response"]["request_id"] == "req_B"
        assert again["response"]["response"] == first["response"]["response"]
        b.abort()
    asyncio.run(go())
    inits = [e for e in read_log(env["log"]) if e.get("subtype") == "initialize"]
    assert len(inits) == 1                                   # the CLI saw one initialize


def test_pending_permission_survives_a_crash(env):
    rec = launch_host("h-perm", env["cwd"], _cli(env))

    async def go():
        a = RawClient(rec)
        await a.connect()
        await a.write_line(_init_frame("req_1"))
        await a.until(is_type("control_response"))
        await a.user("perm")
        asked = msg_of((await a.until(is_type("control_request")))[-1])
        a.abort()
        await asyncio.sleep(0.5)
        b = RawClient(rec)
        hello = await b.connect()
        assert hello["turn_in_flight"]
        replay = await b.until(is_type("control_request"))
        assert msg_of(replay[-1])["request_id"] == asked["request_id"]
        await b.write_line({"type": "control_response", "response": {
            "subtype": "success", "request_id": asked["request_id"],
            "response": {"behavior": "allow", "updatedInput": {"command": "echo hi"}}}})
        done = await b.until(is_type("result"))
        assert "permission granted" in texts(done)
        b.abort()
    asyncio.run(go())


def test_newest_attach_wins(env):
    rec = launch_host("h-newest", env["cwd"], _cli(env))

    async def go():
        a = RawClient(rec)
        await a.connect()
        b = RawClient(rec)
        await b.connect()
        assert await a.frame(5) is None                       # the older engine was dropped
        await b.write_line(_init_frame("req_1"))
        await b.until(is_type("control_response"))
        b.abort()
    asyncio.run(go())


def test_rename_moves_the_record(env):
    rec = launch_host("h-temp", env["cwd"], _cli(env))

    async def go():
        a = RawClient(rec)
        await a.connect()
        await a.send({"t": "rename", "sid": "h-real"})
        await asyncio.sleep(0.5)
        a.abort()
    asyncio.run(go())
    assert wait_until(lambda: shp.read_record("h-real"), 5)
    assert not shp.record_path("h-temp").exists()


def test_cli_exit_is_reported_and_the_host_leaves(env):
    rec = launch_host("h-exit", env["cwd"], _cli(env))

    async def go():
        a = RawClient(rec)
        await a.connect()
        await a.write_line(_init_frame("req_1"))
        await a.until(is_type("control_response"))
        await a.user("exit 3")
        frames = await a.until(lambda f: f.get("t") == "cli_exit")
        assert frames[-1]["code"] == 3
        await a.send({"t": "ack", "seq": 10 ** 6})
    asyncio.run(go())
    assert wait_until(lambda: not shp.record_path("h-exit").exists(), 10)
    assert sht.wait_host_gone(rec, 10)


def test_orphan_with_no_engine_exits(env, monkeypatch):
    monkeypatch.setenv("VIBENODE_HOST_ORPHAN_MINUTES", "0.02")   # floor: 1 s
    rec = launch_host("h-orphan", env["cwd"], _cli(env))
    assert wait_until(lambda: not shp.record_path("h-orphan").exists(), 15)
    assert sht.wait_host_gone(rec, 5)
    assert wait_until(lambda: not shp.process_alive(rec["cli_pid"]), 5)


def test_orphan_waits_for_a_turn_in_flight(env, monkeypatch):
    monkeypatch.setenv("VIBENODE_HOST_ORPHAN_MINUTES", "0.02")
    rec = launch_host("h-busy", env["cwd"], _cli(env))

    async def go():
        a = RawClient(rec)
        await a.connect()
        await a.write_line(_init_frame("req_1"))
        await a.until(is_type("control_response"))
        await a.user("perm")                                  # waits on a permission: in flight
        await a.until(is_type("control_request"))
        a.abort()
    asyncio.run(go())
    time.sleep(3.0)
    assert shp.read_record("h-busy")["turn_in_flight"]
    assert shp.process_alive(rec["host_pid"])                 # not reaped mid-turn


def test_shutdown_kills_the_cli_tree(env):
    rec = launch_host("h-tree", env["cwd"], _cli(env, FAKE_CLI_SPAWN_CHILD="1",
                                                    FAKE_CLI_IGNORE_EOF="1"))
    child = wait_until(lambda: next((e for e in read_log(env["log"]) if e["event"] == "child"), None), 10)
    assert child and shp.process_alive(child["child_pid"])
    assert sht.shutdown_host_sync(rec, wait=15)
    assert wait_until(lambda: not shp.process_alive(rec["cli_pid"]), 5)
    assert wait_until(lambda: not shp.process_alive(child["child_pid"]), 5)
    assert sht.wait_host_gone(rec, 5)
    assert not shp.record_path("h-tree").exists()


def test_a_mismatched_protocol_may_only_retire_the_host(env):
    rec = launch_host("h-proto", env["cwd"], _cli(env))

    async def go():
        old = RawClient(rec)
        hello = await old.connect(proto=shp.PROTO + 1)
        assert hello["t"] == "hello_err" and hello["reason"] == "proto"
        assert hello["turn_in_flight"] is False
        await old.send({"t": "shutdown"})
        await old.until(lambda f: f.get("t") == "bye")
    asyncio.run(go())
    assert sht.wait_host_gone(rec, 10)
