"""Session Host protocol pieces that need no process: frames, records,
secrets, the line buffer, the wire vocabulary, the process identity check.
Spec: docs/plans/session-host-spec.md."""

import asyncio
import os
import time

import pytest

from daemon import session_host_protocol as shp
from daemon.backends import claude_wire as wire
from daemon.session_host import LineBuffer
import daemon.session_host as host_mod


def test_frames_round_trip_and_skip_garbage():
    async def go():
        reader = asyncio.StreamReader(limit=shp.FRAME_LIMIT)
        reader.feed_data(b"not json\n" + shp.encode({"t": "msg", "seq": 1, "line": "{\"a\":1}"})
                         + b"[1,2]\n" + shp.encode({"t": "bye"}))
        reader.feed_eof()
        return [await shp.read_frame(reader) for _ in range(3)]
    a, b, c = asyncio.run(go())
    assert a == {"t": "msg", "seq": 1, "line": "{\"a\":1}"}
    assert b == {"t": "bye"}
    assert c is None


def test_records_are_atomic_listed_and_deleted(tmp_path):
    shp.write_json_atomic(shp.record_path("s-1", tmp_path), {"sid": "s-1", "port": 5})
    shp.write_json_atomic(shp.launch_path("s-1", tmp_path), {"sid": "s-1", "secret": "x"})
    recs = shp.list_records(tmp_path)
    assert [r["sid"] for r in recs] == ["s-1"]            # the launch spec is not a record
    assert not [p for p in tmp_path.iterdir() if p.name.startswith(".host.")]
    if os.name != "nt":
        assert (shp.record_path("s-1", tmp_path).stat().st_mode & 0o777) == 0o600
    shp.delete_host_files("s-1", tmp_path)
    assert list(tmp_path.iterdir()) == []


def test_ids_cannot_escape_the_folder(tmp_path):
    p = shp.record_path("../../etc/passwd", tmp_path)
    assert p.parent == tmp_path and ".." not in p.name


def test_secret_check():
    s = shp.new_secret()
    assert len(s) == 64 and shp.secret_ok(s, s)
    assert not shp.secret_ok("x" * 64, s)
    assert not shp.secret_ok(None, s) and not shp.secret_ok(s, "")


def test_switch_reads_env_and_defaults(monkeypatch):
    monkeypatch.delenv("VIBENODE_SESSION_HOST", raising=False)
    assert shp.enabled() is shp.DEFAULT_ENABLED is True     # ON since step 6
    from app import engine_knobs
    assert {k[0]: k[2] for k in engine_knobs.KNOBS}["VIBENODE_SESSION_HOST"] == "1"
    monkeypatch.setenv("VIBENODE_SESSION_HOST", "1")
    assert shp.enabled()
    monkeypatch.setenv("VIBENODE_SESSION_HOST", "0")
    assert not shp.enabled()
    monkeypatch.setenv("VIBENODE_HOST_ORPHAN_MINUTES", "2")
    assert shp.orphan_seconds() == 120
    monkeypatch.setenv("VIBENODE_HOST_ORPHAN_MINUTES", "junk")
    assert shp.orphan_seconds() == 1800


def test_same_process_fails_closed():
    me = os.getpid()
    ct = shp.process_create_time(me)
    assert ct and shp.same_process(me, ct)
    assert shp.same_process(me, ct, shp.process_name(me))
    assert not shp.same_process(me, ct + 30)                  # pid reused by another program
    assert not shp.same_process(me, ct, "claude.exe")         # wrong program
    for bad in (0, 1, -1, None, "123", 1.5, True):
        assert not shp.same_process(bad, ct)
    assert not shp.same_process(me, None) and not shp.same_process(me, "x")


def test_line_buffer_trims_only_through_a_finished_turn(tmp_path):
    b = LineBuffer(tmp_path / "x.buf")
    for i, end in ((1, False), (2, True), (3, False), (4, False)):
        b.append(i, float(i), f"l{i}", end)
    b.trim_through(2)
    assert [s for s, _, _ in b.since(0)] == [3, 4]
    assert [s for s, _, _ in b.since(3.5)] == [4]


def test_line_buffer_spills_and_caps(tmp_path, monkeypatch):
    monkeypatch.setattr(host_mod, "MEM_CAP", 100)
    monkeypatch.setattr(host_mod, "HARD_CAP", 400)
    b = LineBuffer(tmp_path / "x.buf")
    for i in range(1, 11):
        b.append(i, float(i), "x" * 30 + str(i), False)
    assert b.spilled and (tmp_path / "x.buf").exists()
    got = [s for s, _, _ in b.since(0)]
    assert got == sorted(got) and got[-1] == 10
    for i in range(11, 40):
        b.append(i, float(i), "y" * 30, False)
    assert b.overflow                                         # hard cap reached
    b.trim_through(1 << 40)
    assert not (tmp_path / "x.buf").exists() and b.since(0) == []


def test_wire_vocabulary():
    init = {"type": "control_request", "request_id": "r1", "request": {"subtype": "initialize"}}
    perm = {"type": "control_request", "request_id": "c1", "request": {"subtype": "can_use_tool"}}
    resp = {"type": "control_response", "response": {"subtype": "success", "request_id": "r1",
                                                     "response": {"models": []}}}
    assert wire.init_request_id(init) == "r1" and wire.init_request_id(perm) is None
    assert wire.cli_request_id(perm) == "c1"
    assert wire.response_request_id(resp) == "r1"
    again = wire.cached_init_answer(resp["response"], "r9")
    assert again["response"]["request_id"] == "r9" and again["response"]["response"] == {"models": []}
    assert resp["response"]["request_id"] == "r1"             # the cache is not mutated
    assert wire.is_turn_end({"type": "result"}) and wire.is_turn_activity({"type": "assistant"})
    assert wire.dedup_key({"type": "assistant", "uuid": "u"}) == "u"
    assert wire.dedup_key({"type": "result", "uuid": "u"}) is None


def test_launch_spec_is_the_sdk_command_line():
    """The engine builds the argv with the SDK's own transport, so a hosted
    CLI starts with exactly the flags an in-process one would."""
    import json as _json
    from claude_code_sdk import ClaudeCodeOptions
    from daemon.backends.claude_host import launch_spec_from_options

    async def cb(*a):
        return None
    opts = ClaudeCodeOptions(cwd="/x", model="m", can_use_tool=cb, extra_args={"effort": "high"},
                             permission_prompt_tool_name="stdio", resume="sess-1")
    spec = launch_spec_from_options(opts, "/bin/fake")
    argv = spec["argv"]
    assert argv[0] == "/bin/fake"
    for flag, value in (("--model", "m"), ("--effort", "high"), ("--resume", "sess-1"),
                        ("--permission-prompt-tool", "stdio"), ("--input-format", "stream-json")):
        assert argv[argv.index(flag) + 1] == value
    assert spec["cwd"] == "/x" and spec["env"]["PWD"] == "/x"
    assert spec["env"]["CLAUDE_CODE_ENTRYPOINT"] == "sdk-py" and spec["test_cli"] is True
    _json.dumps(spec)                                          # all of it crosses a file


def test_host_refuses_the_real_cli_under_test(monkeypatch):
    from daemon.session_host import check_launch_allowed
    monkeypatch.setenv("VIBENODE_HOST_FORBID_REAL_CLI", "1")
    with pytest.raises(RuntimeError, match="real CLI"):
        check_launch_allowed({"argv": ["claude"], "test_cli": False})
    check_launch_allowed({"argv": ["fake"], "test_cli": True})
    with pytest.raises(RuntimeError, match="argv"):
        check_launch_allowed({"argv": [], "test_cli": True})
