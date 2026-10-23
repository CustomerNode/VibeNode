"""The Session Engine restarts without ending a session (spec step 5,
docs/plans/session-host-spec.md).

SessionManager A runs a hosted session on the fake CLI (tests/fake_cli.py) in
a real Session Host process.  A is then killed the way ``kill -9`` kills it:
its loop stops dead and its socket to the host is closed with no ``detach``.
SessionManager B starts in the same pytest process and must, from the files
alone (host record, registry, SessionDurable, transcript):

* reattach the session (not ``--resume`` it: the CLI keeps its pid),
* receive the buffered stream, including the permission prompt that was open
  when A died, and push it to the browser,
* answer it through ``resolve_permission`` so the turn completes with RESULT,
* hold every timeline entry exactly once (transcript + replay),
* re-arm a SessionDurable countdown, and keep a graceful ``stop()`` detach.
"""

import socket
import threading
import time

import pytest

from daemon import session_host_protocol as shp
from daemon import session_host_transport as sht
from daemon.backends.claude import ClaudeAgentSDK
from daemon.session_manager import SessionManager
from tests._session_host_helpers import make_fake_cli, read_log, wait_until


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


@pytest.fixture
def fake(tmp_path, monkeypatch):
    monkeypatch.setenv("VIBENODE_SESSION_HOST", "1")
    cwd = tmp_path / "proj"
    cwd.mkdir()
    log = tmp_path / "cli.log"
    sdk = ClaudeAgentSDK()
    sdk.host_cli_path = make_fake_cli(tmp_path / "bin", FAKE_CLI_LOG=str(log))
    engines = []

    def engine():
        sm = SessionManager(sdk=sdk)
        push = Pushes()
        sm.start(push)
        engines.append(sm)
        return sm, push
    yield {"sdk": sdk, "cwd": cwd, "log": log, "engine": engine}
    for sm in engines:
        if sm._started and sm._loop.is_running():
            try:
                for sid in list(sm._sessions):
                    sm.close_session_sync(sid, timeout=20)
            except Exception:
                pass
            sm.stop()


def kill_engine(sm: SessionManager) -> None:
    """What ``kill -9`` does to an engine: nothing more runs in it, and the
    OS closes its sockets.  No stop(), no detach, no durable flush."""
    sm._started = False
    for t in ("_orphan_sweep_timer", "_wakeup_queue_watchdog_timer"):
        timer = getattr(sm, t, None)
        if timer is not None:
            timer.cancel()
    for attr in ("_health_monitor", "_job_supervisor"):
        obj = getattr(sm, attr, None)
        if obj is not None:
            try:
                obj.stop()
            except Exception:
                pass
    sm._reg.cancel_timer()
    sm._loop.call_soon_threadsafe(sm._loop.stop)
    sm._thread.join(timeout=10)
    assert not sm._loop.is_running()
    # A killed process reports nothing; its frozen tasks must not either.
    sm._loop.set_exception_handler(lambda loop, ctx: None)
    for info in list(sm._sessions.values()):
        t = getattr(info.client, "_transport", None)
        w = getattr(t, "_writer", None)
        sock = getattr(getattr(w, "transport", None), "_sock", None) if w else None
        if isinstance(sock, socket.socket):
            try:
                sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            sock.close()


def _texts(sm, sid, kind=None):
    return [e["text"] for e in sm.get_entries(sid) if kind is None or e["kind"] == kind]


def _adopted(sm, temp):
    return wait_until(lambda: sm._resolve_id(temp) != temp and sm._resolve_id(temp), 30)


def test_engine_killed_mid_turn_with_a_pending_permission(fake):
    a, push_a = fake["engine"]()
    assert a.start_session("temp-restart", prompt="perm", cwd=str(fake["cwd"]))["ok"]
    sid = _adopted(a, "temp-restart")
    assert sid
    assert wait_until(lambda: push_a.of("session_permission"), 30)
    assert a.get_session_state(sid) == "waiting"
    rec = dict(a._sessions[sid]._host_record)
    a._save_registry_now()            # the registry's debounced save has run by now in real life
    cli_starts = [e for e in read_log(fake["log"]) if e["event"] == "start"]
    assert len(cli_starts) == 1

    kill_engine(a)
    assert wait_until(lambda: (shp.read_record(sid) or {}).get("detached_at", 0) > 0, 10)
    assert shp.process_alive(rec["cli_pid"])          # the CLI did not notice

    b, push_b = fake["engine"]()
    assert sid in b._host_attached                    # reattached, not resumed
    # The prompt that was open when A died is asked again by B.
    perm = wait_until(lambda: push_b.of("session_permission"), 20)
    assert perm and perm[-1]["tool_name"] == "Bash" and perm[-1]["session_id"] == sid
    assert wait_until(lambda: b.get_session_state(sid) == "waiting", 10)
    assert b.resolve_permission(sid, allow=True)["ok"]
    assert wait_until(lambda: b.get_session_state(sid) == "idle", 30)

    assert [e for e in read_log(fake["log"]) if e["event"] == "start"] == cli_starts  # same CLI
    answered = [e for e in read_log(fake["log"]) if e["event"] == "permission_answered"]
    assert len(answered) == 1 and answered[0]["response"].get("behavior") == "allow"
    texts = _texts(b, sid)
    assert texts.count("about to run Bash") == 1      # from the transcript, not twice
    assert texts.count("permission granted") == 1     # from the live stream
    assert texts.index("about to run Bash") < texts.index("permission granted")
    assert [t for t in _texts(b, sid, "user") if t.strip() == "perm"] == ["perm"]
    assert b._sessions[sid]._cli_pid == rec["cli_pid"]

    # The reattached session takes new turns on the same CLI.
    assert b.send_message(sid, "after restart")["ok"]
    assert wait_until(lambda: "echo: after restart" in _texts(b, sid), 20)
    inits = [e for e in read_log(fake["log"]) if e.get("subtype") == "initialize"]
    assert len(inits) == 1                            # B's initialize came from the cache


def test_turn_that_finished_while_no_engine_was_attached(fake):
    a, _ = fake["engine"]()
    assert a.start_session("temp-slow", prompt="slow 8", cwd=str(fake["cwd"]))["ok"]
    sid = _adopted(a, "temp-slow")
    assert wait_until(lambda: "chunk 1" in _texts(a, sid), 20)
    a._save_registry_now()
    kill_engine(a)
    time.sleep(2.5)                                   # the turn ends while nobody is attached
    b, _ = fake["engine"]()
    assert sid in b._host_attached
    assert wait_until(lambda: b.get_session_state(sid) == "idle", 30)
    texts = _texts(b, sid)
    for i in range(8):
        assert texts.count(f"chunk {i}") == 1, (i, texts)


def test_durable_countdown_is_rearmed_on_reattach(fake):
    a, _ = fake["engine"]()
    assert a.start_session("temp-timer", prompt="hi", cwd=str(fake["cwd"]))["ok"]
    sid = _adopted(a, "temp-timer")
    assert wait_until(lambda: a.get_session_state(sid) == "idle", 30)
    info = a._sessions[sid]
    at = time.time() + 600
    a._loop.call_soon_threadsafe(a._arm_api_retry_at, sid, info, at)
    assert wait_until(lambda: info.retry_at > 0, 5)
    a._save_registry_now()
    kill_engine(a)
    b, _ = fake["engine"]()
    assert sid in b._host_attached
    assert wait_until(lambda: b._sessions.get(sid) and b.get_session_state(sid) == "idle", 20)
    binfo = b._sessions[sid]
    assert wait_until(lambda: getattr(binfo, "_api_retry_task", None) is not None, 10)
    assert abs(binfo.retry_at - at) < 2.0             # the same absolute instant


def test_graceful_stop_detaches_and_the_next_engine_reattaches(fake):
    a, _ = fake["engine"]()
    assert a.start_session("temp-stop", prompt="hi", cwd=str(fake["cwd"]))["ok"]
    sid = _adopted(a, "temp-stop")
    assert wait_until(lambda: a.get_session_state(sid) == "idle", 30)
    rec = dict(a._sessions[sid]._host_record)
    a.stop()
    entry = a._reg.load_registry()["sessions"][sid]
    assert entry.get("hosted") is True and entry["state"] == "idle"
    assert shp.process_alive(rec["cli_pid"])
    b, _ = fake["engine"]()
    assert sid in b._host_attached
    assert wait_until(lambda: b.get_session_state(sid) == "idle", 20)
    assert b.send_message(sid, "still here")["ok"]
    assert wait_until(lambda: "echo: still here" in _texts(b, sid), 20)


def test_sleep_after_reattach_ends_the_host(fake):
    a, _ = fake["engine"]()
    assert a.start_session("temp-sleep", prompt="hi", cwd=str(fake["cwd"]))["ok"]
    sid = _adopted(a, "temp-sleep")
    assert wait_until(lambda: a.get_session_state(sid) == "idle", 30)
    rec = dict(a._sessions[sid]._host_record)
    a._save_registry_now()
    kill_engine(a)
    b, _ = fake["engine"]()
    assert wait_until(lambda: b.get_session_state(sid) == "idle", 20)
    b.close_session_sync(sid, timeout=30)
    assert sht.wait_host_gone(rec, 10)
    assert wait_until(lambda: not shp.process_alive(rec["cli_pid"]), 5)
    assert not shp.record_path(sid).exists()


def test_dead_host_falls_back_to_resume_recovery(fake):
    """A record whose host is gone is cleared; the session is not attached
    (recover_sessions resumes it, as before the Session Host)."""
    shp.write_json_atomic(shp.record_path("ghost-1"), {
        "sid": "ghost-1", "proto": shp.PROTO, "host_pid": 999999, "host_create_time": 1.0,
        "cli_pid": 999998, "cli_create_time": 1.0, "port": 1, "secret": "x" * 64})
    b, _ = fake["engine"]()
    assert "ghost-1" not in b._host_attached
    assert not shp.record_path("ghost-1").exists()
