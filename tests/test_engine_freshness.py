"""The user is told when the Session Engine runs outdated code, and can have it
restart itself once nothing is busy.

Why (2026-10-20): the engine had run since Oct 6.  Every session-recovery fix
since then sat on disk without running, and nothing said so.
"""

import json
import os
import threading
import time

import pytest

from app import engine_freshness as ef
from daemon import engine_code


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
    _touch(repo / "daemon" / "session_manager.py", started - 3600)   # before start
    _touch(repo / "daemon" / "silent_wait.py", started + 86400)      # after start
    assert ef.outdated_files(started, None) == ["daemon/silent_wait.py"]


def test_engine_record_decides_exactly(repo):
    started = time.time() - 86400
    a = _touch(repo / "daemon" / "session_manager.py", started + 10)
    _touch(repo / "app" / "config.py", started - 10)
    loaded = {"daemon/session_manager.py": a,
              "app/config.py": os.stat(repo / "app" / "config.py").st_mtime_ns - 1}
    # session_manager unchanged since load (even though newer than start);
    # app/config.py, imported by the engine, changed after load.
    assert ef.outdated_files(started, loaded) == ["app/config.py"]


def test_watchdog_files_get_time_to_hot_reload(repo):
    now = time.time()
    loaded = {"daemon/silent_wait.py": _touch(repo / "daemon" / "silent_wait.py", now - 7200)}
    _touch(repo / "daemon" / "silent_wait.py", now - 30)       # just edited
    assert ef.outdated_files(now - 86400, loaded, now=now) == []
    # ...but if it is still not live well after that, it is outdated.
    assert ef.outdated_files(now - 86400, loaded, now=now + 3600) == ["daemon/silent_wait.py"]


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
    assert "daemon/engine_code.py" in record["loaded"]
    assert not any(k.startswith(".venv") for k in record["loaded"])
    record["loaded"]["daemon/engine_code.py"] = 1
    (tmp_path / "engine_code.json").write_text(json.dumps(record))
    engine_code.note_reloaded(["daemon/engine_code.py"])
    record = json.loads((tmp_path / "engine_code.json").read_text())
    assert record["loaded"]["daemon/engine_code.py"] == os.stat(
        engine_code.ROOT / "daemon" / "engine_code.py").st_mtime_ns


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
    """2026-10-29, the owner: "why wait five minutes, why not as soon as
    quiet".  Output 60 s old still holds the restart; 120 s old does not."""
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
