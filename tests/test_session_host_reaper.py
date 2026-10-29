"""The Session Host reaper and protocol policy (spec step 6,
docs/plans/session-host-spec.md).

* A stale record whose host is dead is cleared; its CLI is killed only if it
  is provably still that CLI (pid + create time + name).
* A create-time (or name) mismatch is NEVER killed: a pid read from a file may
  belong to an unrelated program by now.
* A live host whose session this engine slept, or slept and then deleted, is
  shut down by the sweep (the backstop when the close's own shutdown was lost).
* A host speaking another protocol is retired: at once when idle, after its
  turn when busy, and the session is then resumed.
* A host survives its parent being killed (Windows: CREATE_BREAKAWAY_FROM_JOB).
* ``spawn_host`` never uses DETACHED_PROCESS on Windows (it opened a visible
  terminal for every session) and always uses CREATE_NO_WINDOW.

Every process a test here starts is killed in a finalizer, pass or fail;
tests/conftest.py tree-kills any tracked host on top of that.
"""

import asyncio
import json
import os
import re
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

from daemon import session_host_protocol as shp
from daemon import session_host_registry as shr
from daemon import session_host_transport as sht
from daemon.backends.claude import ClaudeAgentSDK
from daemon.session_manager import SessionManager, SessionState
from tests._session_host_helpers import (
    RawClient, is_type, kill_if_same, launch_host, make_fake_cli, pin_preferences, wait_until,
)

ROOT = Path(__file__).resolve().parents[1]
DETACHED_PROCESS = 0x00000008
CREATE_NO_WINDOW = 0x08000000


# ── Fixtures ─────────────────────────────────────────────────────────────

@pytest.fixture
def procs():
    """Start throwaway processes; every one is killed at teardown."""
    started = []

    def start(seconds=120):
        py = getattr(sys, "_base_executable", None) or sys.executable
        kw = {"creationflags": CREATE_NO_WINDOW} if os.name == "nt" else {}
        p = subprocess.Popen([py, "-c", f"import time; time.sleep({seconds})"],
                             stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                             stderr=subprocess.DEVNULL, **kw)
        started.append(p)
        assert wait_until(lambda: shp.process_create_time(p.pid), 5)
        return p
    yield start
    for p in started:
        if p.poll() is None:
            p.kill()
        try:
            p.wait(5)
        except Exception:
            pass


@pytest.fixture
def kills(monkeypatch):
    """Record every kill the registry asks for, instead of doing it."""
    calls = []
    # The registry's own hook, not SessionManager: another test in the same
    # worker may have reloaded daemon.session_manager, and _kill_tree()
    # imports whichever class is current.
    monkeypatch.setattr(shr, "_kill_tree", lambda: calls.append)
    return calls


class _StubSM:
    """Just enough of a SessionManager for reconcile/sweep."""

    def __init__(self, sessions=None):
        self._sessions = dict(sessions or {})
        self._lock = threading.Lock()
        self._host_closed = set()
        self.attached_calls = []

    def _resolve_id(self, sid):
        return sid

    def attach_hosted_sessions(self, records):
        self.attached_calls.append(records)
        return {}

    @staticmethod
    def _is_hosted(info):
        return getattr(info, "_hosted", False) is True


class Pushes:
    def __init__(self):
        self.events = []
        self.lock = threading.Lock()

    def __call__(self, event, data, *a, **k):
        with self.lock:
            self.events.append((event, data))


@pytest.fixture
def fake(tmp_path, monkeypatch):
    monkeypatch.setenv("VIBENODE_SESSION_HOST", "1")
    cwd = tmp_path / "proj"
    cwd.mkdir()
    sdk = ClaudeAgentSDK()
    sdk.host_cli_path = make_fake_cli(tmp_path / "bin", FAKE_CLI_LOG=str(tmp_path / "cli.log"))
    pin_preferences(monkeypatch)
    engines = []

    def engine():
        sm = SessionManager(sdk=sdk)
        sm.start(Pushes())
        engines.append(sm)
        return sm
    yield {"sdk": sdk, "cwd": cwd, "engine": engine, "cli": sdk.host_cli_path}
    for sm in engines:
        if sm._started and sm._loop.is_running():
            try:
                for sid in list(sm._sessions):
                    sm.close_session_sync(sid, timeout=20)
            except Exception:
                pass
            sm.stop()


def _record(sid, **fields):
    rec = {"sid": sid, "proto": shp.PROTO, "port": 1, "secret": "x" * 64,
           "started_at": 0.0}
    rec.update(fields)
    shp.write_json_atomic(shp.record_path(sid), rec)
    return rec


def _adopted(sm, temp):
    return wait_until(lambda: sm._resolve_id(temp) != temp and sm._resolve_id(temp), 30)


# ── Stale records and the never-kill rule ────────────────────────────────

def test_stale_record_with_a_dead_host_is_cleared(procs, kills):
    gone = procs(0)
    gone.wait(10)
    ct = shp.process_create_time(os.getpid())
    _record("stale-1", host_pid=gone.pid, host_create_time=ct, cli_pid=gone.pid,
            cli_create_time=ct)
    sm = _StubSM()
    assert shr.reconcile(sm) == set()
    assert sm.attached_calls == []                     # nothing live to attach
    assert not shp.record_path("stale-1").exists()
    assert kills == []


def test_cli_that_provably_outlived_its_host_is_killed(procs, kills):
    cli = procs()
    _record("stale-2", host_pid=999999, host_create_time=1.0, cli_pid=cli.pid,
            cli_create_time=shp.process_create_time(cli.pid),
            cli_name=shp.process_name(cli.pid))
    shr.reconcile(_StubSM())
    assert kills == [cli.pid]
    assert not shp.record_path("stale-2").exists()


def test_create_time_mismatch_is_never_killed(procs, kills):
    host, cli = procs(), procs()
    _record("reused-1",
            host_pid=host.pid, host_create_time=shp.process_create_time(host.pid) - 100,
            host_name=shp.process_name(host.pid),
            cli_pid=cli.pid, cli_create_time=shp.process_create_time(cli.pid) - 100,
            cli_name=shp.process_name(cli.pid))
    shr.reconcile(_StubSM())
    # The sweep path for a session this engine knows, too.
    _record("reused-2",
            host_pid=host.pid, host_create_time=shp.process_create_time(host.pid) + 5,
            cli_pid=cli.pid, cli_create_time=shp.process_create_time(cli.pid) + 5)

    class Info:
        state = SessionState.IDLE
    shr.sweep(_StubSM({"reused-2": Info()}))
    assert kills == []
    assert host.poll() is None and cli.poll() is None
    assert not shp.record_path("reused-1").exists()


def test_name_mismatch_is_never_killed(procs, kills):
    cli = procs()
    _record("reused-3", host_pid=999999, host_create_time=1.0, cli_pid=cli.pid,
            cli_create_time=shp.process_create_time(cli.pid), cli_name="claude.exe")
    shr.reconcile(_StubSM())
    assert kills == [] and cli.poll() is None


def test_garbage_record_fields_never_reach_a_kill(kills):
    from unittest.mock import MagicMock
    for bad in (MagicMock(), "1234", True, 1, 0, -1, None, 1.5):
        assert shr.guarded_kill({"sid": "g", "cli_pid": bad, "cli_create_time": MagicMock(),
                                 "host_pid": bad, "host_create_time": 1.0}, kills.append) == []
    _record("garbage", host_pid="nope", host_create_time="x", cli_pid=[1], cli_create_time={})
    shr.reconcile(_StubSM())
    assert kills == []


def test_sweep_ignores_records_of_sessions_it_does_not_know(procs, kills):
    gone = procs(0)
    gone.wait(10)
    _record("someone-else", host_pid=gone.pid, host_create_time=1.0)
    shr.sweep(_StubSM())
    assert shp.record_path("someone-else").exists()   # not ours: untouched
    assert kills == []


# ── Live hosts of slept and deleted sessions ─────────────────────────────

def _lose_the_close(monkeypatch, sm):
    """A close whose shutdown never reached the host: the client drops its
    socket and the guarded cleanup does nothing."""
    async def drop(client):
        client._transport.abort_connection()
    monkeypatch.setattr(sm._sdk, "disconnect", drop)
    monkeypatch.setattr(shr, "ensure_host_gone", lambda *a, **k: None)


def test_live_host_of_a_slept_session_is_shut_down(fake, monkeypatch):
    sm = fake["engine"]()
    assert sm.start_session("temp-slept", prompt="hi", cwd=str(fake["cwd"]))["ok"]
    sid = _adopted(sm, "temp-slept")
    assert wait_until(lambda: sm.get_session_state(sid) == "idle", 30)
    rec = dict(sm._sessions[sid]._host_record)
    with monkeypatch.context() as m:
        _lose_the_close(m, sm)
        sm.close_session_sync(sid, timeout=20)
    assert sm._sessions[sid].state == SessionState.STOPPED
    assert wait_until(lambda: (shp.read_record(sid) or {}).get("detached_at", 0) > 0, 10)
    assert shp.process_alive(rec["host_pid"])          # the close did not reach it
    monkeypatch.setattr(shr, "SPAWN_GRACE_S", 0.0)
    shr.sweep(sm)
    assert sht.wait_host_gone(rec, 10)
    assert wait_until(lambda: not shp.process_alive(rec["cli_pid"]), 5)
    assert not shp.record_path(sid).exists()


def test_live_host_of_a_deleted_session_is_shut_down(fake, monkeypatch):
    sm = fake["engine"]()
    assert sm.start_session("temp-deleted", prompt="hi", cwd=str(fake["cwd"]))["ok"]
    sid = _adopted(sm, "temp-deleted")
    assert wait_until(lambda: sm.get_session_state(sid) == "idle", 30)
    rec = dict(sm._sessions[sid]._host_record)
    with monkeypatch.context() as m:
        _lose_the_close(m, sm)
        sm.close_session_sync(sid, timeout=20)
    sm.remove_session(sid)
    assert sid not in sm._sessions
    assert shp.process_alive(rec["host_pid"])
    shr.sweep(sm)
    assert sht.wait_host_gone(rec, 10)
    assert wait_until(lambda: not shp.process_alive(rec["cli_pid"]), 5)
    assert not shp.record_path(sid).exists()


def test_live_host_of_an_awake_session_is_left_alone(fake, monkeypatch):
    sm = fake["engine"]()
    assert sm.start_session("temp-awake", prompt="hi", cwd=str(fake["cwd"]))["ok"]
    sid = _adopted(sm, "temp-awake")
    assert wait_until(lambda: sm.get_session_state(sid) == "idle", 30)
    rec = dict(sm._sessions[sid]._host_record)
    monkeypatch.setattr(shr, "SPAWN_GRACE_S", 0.0)
    shr.sweep(sm)
    time.sleep(0.5)
    assert shp.same_process(rec["host_pid"], rec["host_create_time"], rec["host_name"])
    assert sm.send_message(sid, "still")["ok"]
    assert wait_until(lambda: "echo: still" in [e["text"] for e in sm.get_entries(sid)], 20)


# ── Protocol policy ──────────────────────────────────────────────────────

def _other_proto(monkeypatch):
    """This engine now speaks the next protocol; the hosts still speak PROTO."""
    monkeypatch.setattr(shp, "PROTO", shp.PROTO + 1)


def test_idle_host_with_another_protocol_is_retired_at_start(fake, monkeypatch):
    rec = launch_host("old-idle", fake["cwd"], fake["cli"])
    _other_proto(monkeypatch)
    sm = fake["engine"]()
    assert "old-idle" not in sm._host_attached
    assert sht.wait_host_gone(rec, 15)
    assert wait_until(lambda: not shp.process_alive(rec["cli_pid"]), 5)
    assert not shp.record_path("old-idle").exists()


def test_busy_host_with_another_protocol_is_retired_after_its_turn(fake, monkeypatch):
    rec = launch_host("old-busy", fake["cwd"], fake["cli"])

    async def start_turn():
        c = RawClient(rec)
        await c.connect()
        await c.write_line({"type": "control_request", "request_id": "req_1",
                            "request": {"subtype": "initialize", "hooks": None}})
        await c.until(is_type("control_response"))
        await c.user("slow 15")                        # ~3 s of output
        await c.until(is_type("assistant"))
        c.abort()                                      # the old engine is killed
    asyncio.run(start_turn())
    _other_proto(monkeypatch)
    sm = fake["engine"]()
    resumed = []
    monkeypatch.setattr(sm, "start_session", lambda **kw: resumed.append(kw))
    # Busy: kept, not resumed (two CLIs must never run one session).
    assert "old-busy" in sm._host_attached
    assert shp.process_alive(rec["host_pid"])
    shr.sweep(sm)
    assert resumed == [] and shp.process_alive(rec["host_pid"])
    # Its turn ends with no engine attached; the next sweep retires it.
    assert wait_until(lambda: not (shp.read_record("old-busy") or {}).get("turn_in_flight", True), 20)
    shr.sweep(sm)
    assert sht.wait_host_gone(rec, 15)
    assert wait_until(lambda: resumed, 5)
    assert resumed[0]["session_id"] == "old-busy" and resumed[0]["resume"] is True
    assert not shp.record_path("old-busy").exists()


def test_other_protocol_host_stuck_on_a_permission_is_retired_after_the_orphan_window(
        fake, monkeypatch):
    """No engine can answer a permission in a host of another protocol, so
    the turn never ends.  It is waited on for VIBENODE_HOST_ORPHAN_MINUTES,
    then retired mid-turn and the session resumed."""
    rec = launch_host("old-stuck", fake["cwd"], fake["cli"])

    async def ask_permission():
        c = RawClient(rec)
        await c.connect()
        await c.write_line({"type": "control_request", "request_id": "req_1",
                            "request": {"subtype": "initialize", "hooks": None}})
        await c.until(is_type("control_response"))
        await c.user("perm")
        await c.until(is_type("control_request"))
        c.abort()
    asyncio.run(ask_permission())
    _other_proto(monkeypatch)
    sm = fake["engine"]()
    resumed = []
    monkeypatch.setattr(sm, "start_session", lambda **kw: resumed.append(kw))
    assert "old-stuck" in sm._host_attached
    shr.sweep(sm)
    assert resumed == [] and shp.process_alive(rec["host_pid"])      # inside the window
    # The engine's window only (the host read its own at launch).
    monkeypatch.setenv("VIBENODE_HOST_ORPHAN_MINUTES", "0.02")      # floor: 1 s
    time.sleep(1.2)
    shr.sweep(sm)
    assert sht.wait_host_gone(rec, 15)
    assert wait_until(lambda: not shp.process_alive(rec["cli_pid"]), 5)
    assert wait_until(lambda: resumed, 5) and resumed[0]["session_id"] == "old-stuck"


# ── Survival and spawn flags ─────────────────────────────────────────────

_PARENT = r"""
import asyncio, json, os, sys, time
sys.path.insert(0, sys.argv[1])
from tests._session_host_helpers import launch_spec
from daemon import session_host_transport as sht
rec = asyncio.run(sht.spawn_and_wait(sys.argv[2], launch_spec(sys.argv[3], sys.argv[4]),
                                     hosts_dir=sys.argv[5], logs_dir=sys.argv[6], timeout=30))
print(json.dumps({"parent": os.getpid(), "rec": rec}), flush=True)
time.sleep(600)
"""


def _hard_kill(pid):
    """What the engine restart does to the engine: kill one pid, no tree."""
    if os.name == "nt":
        subprocess.run(["taskkill", "/F", "/PID", str(pid)], capture_output=True,
                       timeout=15, creationflags=CREATE_NO_WINDOW)
    else:
        import signal
        os.kill(pid, signal.SIGKILL)


def test_host_survives_parent_kill(tmp_path):
    """A parent (standing in for the engine) spawns a host the engine's way,
    then is killed by pid.  The host must still answer hello and relay."""
    cwd = tmp_path / "proj"
    cwd.mkdir()
    cli = make_fake_cli(tmp_path / "bin")
    hosts, logs = tmp_path / "hosts", tmp_path / "logs"
    kw = {"creationflags": CREATE_NO_WINDOW} if os.name == "nt" else {"start_new_session": True}
    parent = subprocess.Popen(
        [sys.executable, "-c", _PARENT, str(ROOT), "survivor", str(cwd), cli, str(hosts), str(logs)],
        stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
        cwd=str(ROOT), **kw)
    rec, parent_pid, spawn = None, None, None
    try:
        line = parent.stdout.readline().decode("utf-8", "replace")
        assert line.strip(), "the parent did not report a host"
        got = json.loads(line)
        rec, parent_pid = got["rec"], got["parent"]
        spawn = (rec.get("spawn_pid"), shp.process_create_time(rec.get("spawn_pid")))
        assert shp.same_process(rec["host_pid"], rec["host_create_time"], rec["host_name"])

        _hard_kill(parent_pid)
        if parent.pid != parent_pid:
            _hard_kill(parent.pid)                     # the venv launcher, too
        parent.wait(15)
        assert wait_until(lambda: not shp.process_alive(parent_pid), 10)
        time.sleep(1.0)

        assert shp.same_process(rec["host_pid"], rec["host_create_time"], rec["host_name"])
        assert shp.process_alive(rec["cli_pid"])

        async def hello():
            c = RawClient(rec)
            h = await c.connect()
            assert h["t"] == "hello_ok" and h["cli_pid"] == rec["cli_pid"]
            await c.write_line({"type": "control_request", "request_id": "req_1",
                                "request": {"subtype": "initialize", "hooks": None}})
            await c.until(is_type("control_response"))
            await c.user("after the parent died")
            frames = await c.until(is_type("result"))
            assert any("echo: after the parent died" in json.dumps(f) for f in frames)
            await c.send({"t": "shutdown"})
            await c.until(lambda f: f.get("t") == "bye")
        asyncio.run(hello())
        assert sht.wait_host_gone(rec, 10)
    finally:
        if parent.poll() is None:
            parent.kill()
        if rec:
            if shp.process_alive(rec.get("host_pid")):
                sht.shutdown_host_sync(rec, wait=10)
            kill_if_same(rec.get("cli_pid"), rec.get("cli_create_time"), rec.get("cli_name") or "")
            kill_if_same(rec.get("host_pid"), rec.get("host_create_time"), rec.get("host_name") or "")
            if spawn and spawn[1]:
                kill_if_same(spawn[0], spawn[1])


def test_host_exits_when_its_test_owner_dies(tmp_path, procs, monkeypatch):
    """The backstop for a stopped or killed pytest run: a host started under
    the suite shuts down (and kills its CLI) once its owner process is gone."""
    owner = procs()
    monkeypatch.setenv("VIBENODE_HOST_TEST_OWNER",
                       f"{owner.pid}:{shp.process_create_time(owner.pid)}")
    cwd = tmp_path / "proj"
    cwd.mkdir()
    rec = launch_host("owned", cwd, make_fake_cli(tmp_path / "bin", FAKE_CLI_IGNORE_EOF="1"))
    time.sleep(1.5)
    assert shp.same_process(rec["host_pid"], rec["host_create_time"], rec["host_name"])
    owner.kill()
    owner.wait(10)
    assert sht.wait_host_gone(rec, 15)
    assert wait_until(lambda: not shp.process_alive(rec["cli_pid"]), 5)
    assert not shp.record_path("owned").exists()


@pytest.mark.skipif(os.name != "nt", reason="Windows creation flags")
def test_spawn_host_never_uses_detached_process(monkeypatch, tmp_path):
    """DETACHED_PROCESS made Windows ignore CREATE_NO_WINDOW: every session
    opened a visible terminal.  Both spawn attempts (with breakaway, and the
    retry when a job refuses breakaway) must be windowless and not detached."""
    calls = []

    class Refuse:
        def __init__(self, cmd, creationflags=0, **kw):
            calls.append(creationflags)
            if len(calls) == 1:
                raise OSError("breakaway refused")
            self.pid = 0
    monkeypatch.setattr(sht.subprocess, "Popen", Refuse)
    sht.spawn_host("flags", tmp_path / "h", tmp_path / "l")
    assert len(calls) == 2
    for flags in calls:
        assert not flags & DETACHED_PROCESS, hex(flags)
        assert flags & CREATE_NO_WINDOW, hex(flags)
        assert flags & subprocess.CREATE_NEW_PROCESS_GROUP
    assert calls[0] & 0x01000000 and not calls[1] & 0x01000000   # breakaway, then without


def test_no_tree_kill_outside_kill_process_tree():
    """Spec risk 1: a ``taskkill /T`` (or killpg) aimed at the engine would
    take every Session Host with it (they are its children by parent pid).
    The only tree kill allowed in app/, daemon/ and run.py is
    ``SessionManager._kill_process_tree``, which refuses this process's pid."""
    import ast
    files = list((ROOT / "app").rglob("*.py")) + list((ROOT / "daemon").rglob("*.py")) \
        + [ROOT / "run.py", ROOT / "session_manager.py"]

    def is_tree_kill(node) -> bool:
        if isinstance(node, ast.Call):
            fn = node.func
            if isinstance(fn, ast.Attribute) and fn.attr == "killpg":
                return True
        if isinstance(node, (ast.List, ast.Tuple)):
            vals = [e.value for e in node.elts if isinstance(e, ast.Constant)
                    and isinstance(e.value, str)]
            return any("taskkill" in v.lower() for v in vals) and any(v.upper() == "/T" for v in vals)
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            v = node.value.lower()
            return "taskkill" in v and re.search(r"\s/t(\s|$)", v) is not None
        return False

    offenders = []
    for f in files:
        if not f.exists():
            continue
        tree = ast.parse(f.read_text(encoding="utf-8", errors="replace"))
        allowed = set()
        for fn in ast.walk(tree):
            if isinstance(fn, ast.FunctionDef) and fn.name == "_kill_process_tree":
                allowed |= {id(n) for n in ast.walk(fn)}
        for node in ast.walk(tree):
            if id(node) not in allowed and is_tree_kill(node):
                offenders.append(f"{f.relative_to(ROOT)}:{getattr(node, 'lineno', '?')}")
    assert not offenders, offenders


def test_no_session_host_code_uses_detached_process():
    """Source guard for every platform: the constant may appear in comments
    only, never in code."""
    files = [ROOT / "daemon" / n for n in (
        "session_host.py", "session_host_protocol.py", "session_host_transport.py",
        "session_host_registry.py")] + [ROOT / "daemon" / "backends" / "claude_host.py"]
    for f in files:
        for n, line in enumerate(f.read_text(encoding="utf-8").splitlines(), 1):
            code = line.split("#", 1)[0]
            assert not re.search(r"DETACHED_PROCESS|0x0*8\b", code), f"{f.name}:{n}: {line.strip()}"
