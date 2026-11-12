"""The user is told when the Session Engine runs outdated code, and can have it
restart itself once nothing is busy.

Why: the engine runs for weeks, so a session-recovery fix can sit on disk
without running, and nothing would say so.
"""

import json
import os
import threading
import time

import pytest

from web import engine_freshness as ef
from engine import engine_code


def _touch(path, when):
    path.parent.mkdir(parents=True, exist_ok=True)
    if not path.exists():
        path.write_text("x = 1\n", encoding="utf-8")
    os.utime(path, (when, when))
    return os.stat(path).st_mtime_ns


@pytest.fixture
def repo(tmp_path, monkeypatch):
    monkeypatch.setattr(ef, "ROOT", tmp_path)
    return tmp_path


# ── What counts as outdated ───────────────────────────────────────────────

def test_old_engine_without_a_record_is_judged_by_its_start_time(repo):
    started = time.time() - 14 * 86400
    _touch(repo / "src" / "engine" / "session_manager.py", started - 3600)   # before start
    _touch(repo / "src" / "engine" / "silent_wait.py", started + 86400)      # after start
    assert ef.outdated_files(started, None) == ["src/engine/silent_wait.py"]


def test_engine_record_decides_exactly(repo):
    started = time.time() - 86400
    a = _touch(repo / "src" / "engine" / "session_manager.py", started + 10)
    _touch(repo / "src" / "web" / "config.py", started - 10)
    loaded = {"src/engine/session_manager.py": a,
              "src/web/config.py": os.stat(repo / "src" / "web" / "config.py").st_mtime_ns - 1}
    # session_manager unchanged since load (even though newer than start);
    # src/web/config.py, imported by the engine, changed after load.
    assert ef.outdated_files(started, loaded) == ["src/web/config.py"]


def test_watchdog_files_get_time_to_hot_reload(repo):
    now = time.time()
    loaded = {"src/engine/silent_wait.py": _touch(repo / "src" / "engine" / "silent_wait.py", now - 7200)}
    _touch(repo / "src" / "engine" / "silent_wait.py", now - 30)       # just edited
    assert ef.outdated_files(now - 86400, loaded, now=now) == []
    # ...but if it is still not live well after that, it is outdated.
    assert ef.outdated_files(now - 86400, loaded, now=now + 3600) == ["src/engine/silent_wait.py"]


def test_status_when_no_engine_is_running(monkeypatch):
    monkeypatch.setattr(ef, "engine_process", lambda: None)
    body = ef.status()
    assert body["running"] is False and body["stale"] is False


# ── The engine's own record ───────────────────────────────────────────────

def test_engine_writes_and_updates_its_code_record(tmp_path, monkeypatch):
    monkeypatch.setattr(engine_code, "_in_tests", lambda: False)
    monkeypatch.setattr(engine_code, "record_path", lambda: tmp_path / "engine_code.json")
    engine_code.record_loaded_code()
    record = json.loads((tmp_path / "engine_code.json").read_text())
    assert record["pid"] == os.getpid()
    assert "src/engine/engine_code.py" in record["loaded"]
    assert not any(k.startswith(".venv") for k in record["loaded"])
    record["loaded"]["src/engine/engine_code.py"] = 1
    (tmp_path / "engine_code.json").write_text(json.dumps(record))
    engine_code.note_reloaded(["src/engine/engine_code.py"])
    record = json.loads((tmp_path / "engine_code.json").read_text())
    assert record["loaded"]["src/engine/engine_code.py"] == os.stat(
        engine_code.ROOT / "src" / "engine" / "engine_code.py").st_mtime_ns


def test_a_test_run_never_overwrites_the_real_engine_record(monkeypatch, tmp_path):
    monkeypatch.setattr(engine_code, "record_path", lambda: tmp_path / "engine_code.json")
    engine_code.record_loaded_code()
    assert not (tmp_path / "engine_code.json").exists()


def test_record_from_another_process_is_ignored(tmp_path, monkeypatch):
    monkeypatch.setattr(ef, "RECORD_PATH", tmp_path / "engine_code.json")
    (tmp_path / "engine_code.json").write_text(json.dumps({"pid": 1, "loaded": {"a": 1}}))
    assert ef._read_record(2) is None
    assert ef._read_record(1) == {"a": 1}


# ── Restart when idle ─────────────────────────────────────────────────────

class _SM:
    def __init__(self, states):
        self.states = states

    def get_all_states(self):
        return list(self.states)


def test_busy_sessions_block_the_restart(monkeypatch, tmp_path):
    monkeypatch.setattr(ef, "engine_process", lambda: None)
    quiet = tmp_path / "quiet.jsonl"
    loud = tmp_path / "loud.jsonl"
    _touch(quiet, time.time() - 3600)
    _touch(loud, time.time() - 20)
    paths = {"q": str(quiet), "l": str(loud)}
    monkeypatch.setattr(ef, "_transcript_for", lambda sid: paths.get(sid))
    sm = _SM([{"session_id": "w", "state": "working", "name": "Fixer"},
              {"session_id": "a", "state": "waiting", "name": "Asker"},
              {"session_id": "q", "state": "idle"},
              {"session_id": "l", "state": "idle", "name": "Orchestrator"}])
    reasons = ef.busy_reasons(sm)
    assert any("Fixer is working" in r for r in reasons)
    assert any("Asker is waiting" in r for r in reasons)
    assert any("Orchestrator wrote output" in r for r in reasons)
    assert not any(r.startswith("q ") for r in reasons)


def test_quiet_engine_is_safe_to_restart(monkeypatch, tmp_path):
    monkeypatch.setattr(ef, "engine_process", lambda: None)
    quiet = tmp_path / "quiet.jsonl"
    _touch(quiet, time.time() - 3600)
    monkeypatch.setattr(ef, "_transcript_for", lambda sid: str(quiet))
    assert ef.busy_reasons(_SM([{"session_id": "q", "state": "idle"},
                                {"session_id": "z", "state": "stopped"}])) == []


def test_the_quiet_window_is_ninety_seconds(monkeypatch, tmp_path):
    """The restart waits only for a short quiet window, not five minutes.
    Output 60 s old still holds the restart; 120 s old does not."""
    assert ef.QUIET_SECONDS == 90.0
    monkeypatch.setattr(ef, "engine_process", lambda: None)
    recent = tmp_path / "recent.jsonl"
    older = tmp_path / "older.jsonl"
    _touch(recent, time.time() - 60)
    _touch(older, time.time() - 120)
    monkeypatch.setattr(ef, "_transcript_for", lambda sid: str(recent))
    assert ef.busy_reasons(_SM([{"session_id": "r", "state": "idle"}]))
    monkeypatch.setattr(ef, "_transcript_for", lambda sid: str(older))
    assert ef.busy_reasons(_SM([{"session_id": "o", "state": "idle"}])) == []


def test_unreadable_states_count_as_busy():
    class Broken:
        def get_all_states(self):
            raise RuntimeError("ipc down")
    assert ef.busy_reasons(Broken())


def test_waiter_restarts_once_when_idle_and_not_before(monkeypatch):
    monkeypatch.setattr(ef, "POLL_SECONDS", 0.01)
    calls = {"busy": 3}

    def busy(_sm):
        if calls["busy"] > 0:
            calls["busy"] -= 1
            return ["Fixer is working"]
        return []
    monkeypatch.setattr(ef, "busy_reasons", busy)
    monkeypatch.setattr(ef, "RECHECK_PAUSE_SECONDS", 0.01)
    fired = threading.Event()
    waiter = ef._RestartWaiter()
    assert waiter.arm(_SM([]), restart=fired.set)
    assert not waiter.arm(_SM([]), restart=fired.set)       # one waiter at a time
    assert fired.wait(5)
    assert calls["busy"] == 0


def test_cancel_stops_the_waiter(monkeypatch):
    monkeypatch.setattr(ef, "POLL_SECONDS", 0.01)
    monkeypatch.setattr(ef, "busy_reasons", lambda _sm: ["Fixer is working"])
    fired = threading.Event()
    waiter = ef._RestartWaiter()
    waiter.arm(_SM([]), restart=fired.set)
    waiter.disarm()
    deadline = time.time() + 5
    while waiter.armed() and time.time() < deadline:
        time.sleep(0.01)
    assert not waiter.armed()
    assert not fired.is_set()


# ── Automatic idle restart, no banner ────────────────────────────────────

def test_an_outdated_engine_queues_the_idle_restart_by_itself_once_per_start():
    auto = ef._AutoRestart()
    armed = []
    st = {"running": True, "stale": True, "started_at": 100.0, "outdated": ["src/engine/x.py"],
          "restart_armed": False}
    assert auto.tick(object(), status_fn=lambda: st, arm_fn=armed.append) is True
    # The same engine start never gets a second one (a failed restart cannot loop).
    assert auto.tick(object(), status_fn=lambda: st, arm_fn=armed.append) is False
    assert len(armed) == 1
    # A new start that is outdated again does.
    st2 = dict(st, started_at=200.0)
    assert auto.tick(object(), status_fn=lambda: st2, arm_fn=armed.append) is True
    assert len(armed) == 2


def test_nothing_is_queued_for_a_current_engine_or_one_already_queued():
    auto = ef._AutoRestart()
    armed = []
    for st in ({"running": True, "stale": False, "started_at": 1.0},
               {"running": False, "stale": False},
               {"running": True, "stale": True, "started_at": 1.0, "restart_armed": True}):
        assert auto.tick(object(), status_fn=lambda st=st: st, arm_fn=armed.append) is False
    assert auto.tick(None, status_fn=lambda: {"running": True, "stale": True}, arm_fn=armed.append) is False
    assert armed == []


def test_the_auto_restart_has_a_kill_switch(monkeypatch):
    monkeypatch.setenv("VIBENODE_ENGINE_AUTO_RESTART", "0")
    assert ef.auto_restart_enabled() is False
    monkeypatch.delenv("VIBENODE_ENGINE_AUTO_RESTART")
    assert ef.auto_restart_enabled() is True
