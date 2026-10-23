"""SessionDurable (spec steps 1-2, docs/plans/session-host-spec.md).

The state a Session Engine restart used to lose: armed retry and limit
countdowns, allow-lists, aliases, usage, a pending permission.  Written
through to ``data/session_durable/<sid>.json`` (sandboxed by
tests/conftest.py) and put back on recovery.  Plus the timeline entries a
recovered session reloads from its transcript.
"""

import json
import threading
import time
from unittest.mock import MagicMock

import pytest

from daemon import session_durable as sd
from daemon.session_manager import LogEntry, SessionInfo, SessionManager, SessionState


def _info(sid="dur-1", **kw):
    info = SessionInfo(session_id=sid)
    for k, v in kw.items():
        setattr(info, k, v)
    return info


def test_round_trip_keeps_absolute_epochs_and_the_allow_lists():
    at = time.time() + 900
    info = _info(retry_at=at, retry_attempt=2, retry_max=5, retry_reason="overloaded",
                 _api_retry_count=1, limit_reset_at=at + 60, limited_model="fable-x",
                 _limit_resume_at=at + 90, usage={"input_tokens": 10, "_per_call": [1]},
                 cost_usd=0.5)
    info.always_allowed_tools.update({"Bash", "Read"})
    info.almost_always_allowed_tools.add("Edit")
    info._durable_aliases = {"temp-1"}
    sd.save(info)
    rec = sd.load("dur-1")
    assert rec["retry"]["at"] == at and rec["limit"]["resume_at"] == at + 90
    assert rec["always_allowed_tools"] == ["Bash", "Read"]
    assert rec["aliases"] == ["temp-1"]
    assert "_per_call" not in rec["usage"]

    fresh = _info()
    out = sd.apply_static(fresh, rec, attached=False)
    assert out == {"retry_at": at, "lost_permission": ""}
    assert fresh.retry_at == at and fresh.retry_attempt == 2 and fresh.retry_reason == "overloaded"
    assert fresh.always_allowed_tools == {"Bash", "Read"}
    assert fresh.almost_always_allowed_tools == {"Edit"}
    assert fresh.usage == {"input_tokens": 10} and fresh.cost_usd == 0.5
    assert fresh.limited_model == "fable-x"


def test_limit_resume_is_the_countdown_when_no_retry_is_armed():
    at = time.time() + 300
    sd.save(_info(_limit_resume_at=at))
    fresh = _info()
    assert sd.apply_static(fresh, sd.load("dur-1"), attached=False)["retry_at"] == at
    assert "dur-1" in sd.armed_session_ids()


def test_pending_permission_is_reported_lost_on_resume():
    info = _info(pending_tool_name="Bash", pending_tool_input={"command": "ls"},
                 pending_permission=(object(), []))
    sd.save(info)
    rec = sd.load("dur-1")
    assert rec["pending_permission"]["tool_name"] == "Bash"
    assert sd.apply_static(_info(), rec, attached=False)["lost_permission"] == "Bash"


def test_cli_side_state_is_restored_only_when_the_cli_survived():
    sd.save(_info(_wakeup_pending=True, _wakeup_deadline=time.time() + 600,
                  _interrupted=True, retry_at=time.time() + 60, _retry_needs_reconnect=True))
    rec = sd.load("dur-1")
    resumed = _info()
    sd.apply_static(resumed, rec, attached=False)
    assert not getattr(resumed, "_wakeup_pending", False)
    assert not getattr(resumed, "_interrupted", False)
    assert not getattr(resumed, "_retry_needs_reconnect", False)
    attached = _info()
    sd.apply_static(attached, rec, attached=True)
    assert attached._wakeup_pending and attached._interrupted and attached._retry_needs_reconnect


def test_passed_wakeup_deadline_is_dropped():
    sd.save(_info(_wakeup_pending=True, _wakeup_deadline=time.time() - 5))
    attached = _info()
    sd.apply_static(attached, sd.load("dur-1"), attached=True)
    assert not getattr(attached, "_wakeup_pending", False)


def test_debounced_write_lands_once_with_the_latest_state(monkeypatch):
    monkeypatch.setattr(sd, "DEBOUNCE_S", 0.2)
    info = _info(cost_usd=1.0)
    sd.save(info, immediate=False)
    info.cost_usd = 2.0
    sd.save(info, immediate=False)
    assert sd.load("dur-1") is None                  # not yet
    deadline = time.time() + 5
    while sd.load("dur-1") is None and time.time() < deadline:
        time.sleep(0.05)
    assert sd.load("dur-1")["cost_usd"] == 2.0


def test_immediate_write_cancels_a_pending_debounce(monkeypatch):
    monkeypatch.setattr(sd, "DEBOUNCE_S", 0.3)
    info = _info(cost_usd=1.0)
    sd.save(info, immediate=False)
    info.cost_usd = 3.0
    sd.save(info)
    sd.delete("dur-1")
    time.sleep(0.6)
    assert sd.load("dur-1") is None                  # the debounce did not bring it back


def test_rename_and_delete():
    sd.save(_info("temp-x", cost_usd=1.0))
    sd.rename("temp-x", "real-x")
    assert sd.load("temp-x") is None and sd.load("real-x")["cost_usd"] == 1.0
    sd.delete("real-x")
    assert sd.load("real-x") is None


def test_kill_switch_reads_and_writes_nothing(monkeypatch):
    sd.save(_info(cost_usd=1.0))
    monkeypatch.setenv("VIBENODE_SESSION_DURABLE", "0")
    assert sd.load("dur-1") is None
    sd.save(_info("dur-2", cost_usd=1.0))
    assert not sd.record_path("dur-2").exists()
    assert sd.armed_session_ids() == set()


def test_unreadable_or_foreign_records_are_ignored():
    p = sd.record_path("bad")
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("{not json", encoding="utf-8")
    assert sd.load("bad") is None
    p.write_text(json.dumps({"version": 999}), encoding="utf-8")
    assert sd.load("bad") is None
    assert sd.apply_static(_info(), "nonsense", attached=False) == {"retry_at": 0.0,
                                                                    "lost_permission": ""}


def test_hostile_ids_stay_in_the_folder():
    assert sd.record_path("../../etc/passwd").parent == sd.DURABLE_DIR
    assert sd.record_path("..\\..\\x").name == "x.json"


def test_concurrent_writes_and_reads_never_lose_a_record():
    """Windows refuses os.replace while the file is open: both sides retry."""
    info = _info(cost_usd=1.0)
    sd.save(info)
    stop = threading.Event()
    errors = []

    def writer():
        while not stop.is_set():
            try:
                sd.save(info)
            except Exception as exc:          # save never raises, but be sure
                errors.append(exc)
    t = threading.Thread(target=writer)
    t.start()
    misses = sum(1 for _ in range(3000) if sd.load("dur-1") is None)
    stop.set()
    t.join()
    assert misses == 0 and not errors


# ── Engine side ───────────────────────────────────────────────────────────

def _sm():
    return SessionManager(sdk=MagicMock())


def test_engine_never_saves_a_stopped_or_utility_session():
    sm = _sm()
    sm._durable_save(_info("stopped-1", state=SessionState.STOPPED, cost_usd=1.0))
    sm._durable_save(_info("planner-1", session_type="planner", cost_usd=1.0))
    sm._durable_save(_info("title-1", session_type="title", cost_usd=1.0))
    assert sd.load("stopped-1") is None and sd.load("planner-1") is None
    assert sd.load("title-1") is None
    sm._durable_save(_info("live-1", state=SessionState.IDLE, cost_usd=1.0))
    assert sd.load("live-1")["cost_usd"] == 1.0


def test_lost_permission_becomes_one_system_entry():
    sm = _sm()
    sm._push_callback = lambda *a, **k: None
    info = _info("perm-1", state=SessionState.IDLE)
    info._durable_restore = {"retry_at": 0.0, "lost_permission": "Bash"}
    sm._durable_restore_timers("perm-1", info)
    sm._durable_restore_timers("perm-1", info)       # consumed: never twice
    lost = [e for e in info.entries if "permission request for Bash was lost" in e.text]
    assert len(lost) == 1 and lost[0].is_error


class _Store:
    def __init__(self, rows):
        self.rows = rows
        self.calls = []

    def read_log_entries(self, sid, cwd="", max_entries=200, until_ts=0.0):
        self.calls.append((sid, cwd, max_entries, until_ts))
        return [r for r in self.rows if not until_ts or not isinstance(r, dict)
                or float(r.get("timestamp") or 0) <= until_ts]


def test_entries_come_back_from_the_transcript_flagged_and_trimmed():
    rows = [{"kind": "user", "text": "hi", "timestamp": 100.0, "uuid": "u1"},
            {"kind": "asst", "text": "hello", "timestamp": 101.0, "uuid": "u2"},
            {"kind": "asst", "text": "later", "timestamp": 200.0, "uuid": "u3"},
            {"text": "no kind"}, "not a dict"]
    sm = _sm()
    sm._store = _Store(rows)
    info = _info("tr-1")
    assert sm._restore_entries_from_transcript(info, "tr-1", "/x", until_ts=150.0) == 2
    assert [e.text for e in info.entries] == ["hi", "hello"]
    assert all(e.restored for e in info.entries)
    assert info._entries_trimmed is True
    assert info._restored_uuids == {"u1", "u2"}
    assert sm._store.calls[0][2] == SessionManager._RESTORED_ENTRY_COUNT


def test_transcript_never_overwrites_a_live_log():
    sm = _sm()
    sm._store = _Store([{"kind": "user", "text": "old", "timestamp": 1.0}])
    info = _info("tr-2")
    info.entries = [LogEntry(kind="user", text="live")]
    assert sm._restore_entries_from_transcript(info, "tr-2", "") == 0
    assert [e.text for e in info.entries] == ["live"]


def test_transcript_restore_obeys_the_kill_switch(monkeypatch):
    monkeypatch.setenv("VIBENODE_SESSION_DURABLE", "0")
    sm = _sm()
    sm._store = _Store([{"kind": "user", "text": "x", "timestamp": 1.0}])
    info = _info("tr-3")
    assert sm._restore_entries_from_transcript(info, "tr-3", "") == 0 and info.entries == []
