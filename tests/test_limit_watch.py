"""Tests for the usage-limit resume backstop (HealthMonitor job 6, daemon/limit_watch.py).

The rule this pins: a session stopped by a usage limit must ALWAYS continue
once the limit resets, even if the Session Engine restarted (losing the
in-memory timer), the message wording changed, or the in-memory retry path
missed it.  It must also never override an explicit Stop / Sleep / Delete.
The backstop reads the CLI's own structural tag on the stop entry
(``isApiErrorMessage`` + ``error: "rate_limit"``), so these tests write
transcripts in exactly that shape.
"""

import json
import threading
import time
from datetime import datetime, timezone

import pytest

from daemon import health_monitor as hm
from daemon import limit_watch as lw
from daemon.health_monitor import HealthMonitor
from daemon.session_manager import SessionManager, SessionState

CONT = SessionManager._API_RETRY_CONTINUE_PROMPT
PARSE = SessionManager._parse_usage_limit


def iso(t):
    return datetime.fromtimestamp(t, timezone.utc).isoformat().replace("+00:00", "Z")


def clock(t):
    """'3:07pm' for epoch t in local time: the zoneless form the parser reads
    as local time, so these tests do not depend on tzdata being installed."""
    d = datetime.fromtimestamp(t)
    h12 = d.hour % 12 or 12
    return "%d:%02d%s" % (h12, d.minute, "am" if d.hour < 12 else "pm")


class Transcript:
    def __init__(self, path):
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("", encoding="utf-8")

    def _add(self, obj):
        with open(self.path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(obj, separators=(",", ":")) + "\n")

    def user(self, t, text="fix all of these"):
        self._add({"type": "user", "timestamp": iso(t),
                   "message": {"role": "user", "content": text}})

    def reply(self, t, text="done"):
        self._add({"type": "assistant", "timestamp": iso(t), "message": {
            "role": "assistant", "model": "claude-fable-5-1", "stop_reason": "end_turn",
            "content": [{"type": "text", "text": text}]}})

    def tool_round(self, t):
        self._add({"type": "assistant", "timestamp": iso(t), "message": {
            "role": "assistant", "stop_reason": "tool_use",
            "content": [{"type": "tool_use", "id": "x", "name": "Bash", "input": {}}]}})
        self._add({"type": "user", "timestamp": iso(t + 1), "message": {
            "role": "user", "content": [{"type": "tool_result", "tool_use_id": "x", "content": "ok"}]}})

    def stop(self, t, text, error="rate_limit"):
        self._add({"type": "assistant", "timestamp": iso(t), "isApiErrorMessage": True,
                   "error": error, "message": {
                       "role": "assistant", "model": "<synthetic>", "stop_reason": "stop_sequence",
                       "content": [{"type": "text", "text": text}]}})

    def noise(self, t):
        self._add({"type": "file-history-snapshot", "timestamp": iso(t)})


def limit_text(reset_t):
    return "You've hit your session limit · resets %s" % clock(reset_t)


FABLE = ("You've reached your Fable limit. Switch to another model, or manage "
         "usage credits at claude.ai/settings/usage")


# ---------------------------------------------------------------------------
# limit_watch: reading the transcript
# ---------------------------------------------------------------------------

def test_detects_a_transcript_ending_on_a_limit(tmp_path):
    t = Transcript(tmp_path / "s.jsonl")
    now = time.time()
    t.user(now - 4000)
    t.tool_round(now - 3900)
    t.stop(now - 3800, limit_text(now - 3000))
    t.noise(now - 3790)                      # non-conversational lines are ignored
    stop = lw.stopped_on_error(t.path, CONT)
    assert stop["error"] == "rate_limit" and stop["failures"] == 1
    assert stop["ts"] == pytest.approx(now - 3800, abs=1)


def test_a_reply_after_the_limit_means_it_resumed(tmp_path):
    t = Transcript(tmp_path / "s.jsonl")
    now = time.time()
    t.stop(now - 3800, limit_text(now - 3000))
    t.user(now - 2900, CONT)
    t.reply(now - 2800)
    assert lw.stopped_on_error(t.path, CONT) is None


def test_a_resume_in_flight_is_not_a_stop(tmp_path):
    t = Transcript(tmp_path / "s.jsonl")
    now = time.time()
    t.stop(now - 3800, limit_text(now - 3000))
    t.user(now - 10, CONT)                   # sent, no reply yet
    assert lw.stopped_on_error(t.path, CONT) is None


def test_failed_probes_are_counted_across_our_resends(tmp_path):
    t = Transcript(tmp_path / "s.jsonl")
    now = time.time()
    t.user(now - 9000, "do the work")
    t.stop(now - 8000, FABLE)
    t.user(now - 6000, CONT)
    t.stop(now - 5999, FABLE)
    t.user(now - 2000, CONT)
    t.stop(now - 1999, FABLE)
    assert lw.stopped_on_error(t.path, CONT)["failures"] == 3
    t.user(now - 100, "something new from the user")
    t.stop(now - 99, FABLE)
    assert lw.stopped_on_error(t.path, CONT)["failures"] == 1


@pytest.mark.parametrize("error", ["authentication_failed", "invalid_request", ""])
def test_non_resumable_errors_are_ignored(tmp_path, error):
    t = Transcript(tmp_path / "s.jsonl")
    t.stop(time.time() - 100, "API Error: 401", error=error)
    assert lw.stopped_on_error(t.path, CONT) is None


def test_torn_final_line_means_active(tmp_path):
    t = Transcript(tmp_path / "s.jsonl")
    now = time.time()
    t.stop(now - 3800, limit_text(now - 3000))
    with open(t.path, "a", encoding="utf-8") as fh:
        fh.write('{"type":"assistant","timestamp":"' + iso(now) + '","mess')
    assert lw.stopped_on_error(t.path, CONT) is None


# ---------------------------------------------------------------------------
# limit_watch: when to resume
# ---------------------------------------------------------------------------

def test_due_after_stated_reset_plus_grace():
    ts = time.time() - 3600
    reset = ts + 1500
    stop = {"ts": ts, "error": "rate_limit", "text": limit_text(reset), "failures": 1}
    expected = datetime.fromtimestamp(reset).replace(second=0, microsecond=0).timestamp()
    assert lw.due_at(stop, PARSE) == pytest.approx(expected + lw.LIMIT_GRACE_SECONDS)


@pytest.mark.parametrize("failures, expected", [
    (1, 1800), (2, 3600), (3, 7200), (4, 14400), (9, 14400),
])
def test_unknown_reset_probes_with_backoff(failures, expected):
    stop = {"ts": 1000.0, "error": "rate_limit", "text": FABLE, "failures": failures}
    assert lw.due_at(stop, PARSE) == 1000.0 + expected


def test_stale_stated_reset_backs_off_instead_of_hammering():
    """The stop was written after its own stated reset: we already resumed at
    that time and the limit was still there.  Probe with backoff instead of
    re-resuming at every sweep."""
    ts = time.time()
    stated = datetime.fromtimestamp(ts - 1200).replace(second=0, microsecond=0).timestamp()
    stop = {"ts": ts, "error": "rate_limit", "text": limit_text(stated), "failures": 2}
    assert lw.due_at(stop, PARSE) == pytest.approx(ts + 3600)


def test_server_error_resumes_after_ten_minutes():
    stop = {"ts": 1000.0, "error": "server_error",
            "text": "API Error: Connection lost mid-response.", "failures": 1}
    assert lw.due_at(stop, PARSE) == 1000.0 + lw.SERVER_ERROR_DELAY_SECONDS


# ---------------------------------------------------------------------------
# HealthMonitor job 6
# ---------------------------------------------------------------------------

class Info:
    def __init__(self, sid, state=SessionState.IDLE):
        self.session_id = sid
        self.state = state
        self.name = sid
        self.cwd = "C:/proj"
        self.session_type = ""
        self.entries = []
        self.working_since = 0.0
        self.substatus = ""
        self._lock = threading.Lock()
        self._interrupted = False
        self._wakeup_pending = False
        self.retry_at = 0.0
        self.created_ts = time.time()


class Store:
    def __init__(self):
        self.paths = {}

    def find_session_path(self, sid, cwd=""):
        return self.paths.get(sid)


class Queue:
    def get_queue_data(self, sid):
        return None


class Manager:
    _parse_usage_limit = staticmethod(SessionManager._parse_usage_limit)
    _API_RETRY_CONTINUE_PROMPT = CONT

    def __init__(self):
        self._lock = threading.Lock()
        self._sessions = {}
        self._store = Store()
        self._mq = Queue()
        self.dormant = {}
        self.sent, self.emitted = [], []

    def get_dormant_states(self):
        return dict(self.dormant)

    def send_message(self, sid, text):
        self.sent.append((sid, text))
        return {"ok": True}

    def interrupt_session(self, sid, clear_queue=True):
        return {"ok": True}

    def _emit_entry(self, sid, entry, index):
        self.emitted.append((sid, entry, index))


@pytest.fixture
def env(tmp_path):
    sm = Manager()
    mon = HealthMonitor(sm)
    mon._started_at = time.time() - 3600           # past the startup grace

    def session(sid, live=True, state=SessionState.IDLE):
        t = Transcript(tmp_path / f"{sid}.jsonl")
        sm._store.paths[sid] = t.path
        if live:
            sm._sessions[sid] = Info(sid, state)
        else:
            sm.dormant[sid] = {"cwd": "C:/proj", "session_type": "", "last_state": "idle"}
        return t

    def sweep():
        mon._limit_swept_at = 0.0
        mon.tick()

    return sm, mon, session, sweep


def _expired_limit(t):
    now = time.time()
    t.user(now - 7200)
    t.tool_round(now - 7100)
    t.stop(now - 7000, limit_text(now - 6000))     # reset 1h40m ago


def test_expired_limit_on_live_idle_session_is_resumed_once(env):
    sm, mon, session, sweep = env
    _expired_limit(session("s1"))
    sweep()
    assert sm.sent == [("s1", CONT)]
    assert any("Continuing automatically" in e.text for _, e, _ in sm.emitted)
    sweep()                                         # same stop, just retried
    assert len(sm.sent) == 1


def test_limit_not_yet_reset_is_left_waiting(env):
    sm, mon, session, sweep = env
    t = session("s1")
    now = time.time()
    t.user(now - 700)
    t.stop(now - 600, limit_text(now + 3600))      # resets in an hour
    sweep()
    assert sm.sent == []


def test_dormant_session_is_resumed_after_an_engine_restart(env):
    """The case Layer 1 cannot cover: its in-memory timer died with the old
    Session Engine; the session survives only as restart memory."""
    sm, mon, session, sweep = env
    _expired_limit(session("s1", live=False))
    sweep()
    assert sm.sent == [("s1", CONT)]


def test_session_the_user_stopped_is_never_resumed(env, tmp_path):
    """Stopped / slept / deleted sessions are neither live-IDLE nor in the
    restart memory, so their transcripts are never even considered."""
    sm, mon, session, sweep = env
    t = Transcript(tmp_path / "gone.jsonl")
    sm._store.paths["gone"] = t.path               # transcript exists on disk...
    _expired_limit(t)                              # ...and ends on an expired limit
    stopped = session("st", state=SessionState.STOPPED)
    _expired_limit(stopped)
    interrupted = session("int")
    _expired_limit(interrupted)
    sm._sessions["int"]._interrupted = True        # user pressed Stop
    sweep()
    assert sm.sent == []


def test_working_session_is_left_alone(env):
    sm, mon, session, sweep = env
    _expired_limit(session("s1", state=SessionState.WORKING))
    sweep()
    assert sm.sent == []


def test_layer1_countdown_owns_it_until_overdue(env):
    sm, mon, session, sweep = env
    _expired_limit(session("s1"))
    info = sm._sessions["s1"]
    info.retry_at = time.time() + 60               # Layer 1 will fire shortly
    sweep()
    assert sm.sent == []
    info.retry_at = time.time() - hm.LIMIT_STALE_COUNTDOWN_SECONDS - 5   # timer died
    sweep()
    assert sm.sent == [("s1", CONT)]


def test_unknown_reset_waits_for_the_probe(env):
    sm, mon, session, sweep = env
    t = session("s1")
    now = time.time()
    t.user(now - 1000)
    t.stop(now - 900, FABLE)                       # no reset time: probe at +30m
    sweep()
    assert sm.sent == []
    t2 = session("s2")
    t2.user(now - 4000)
    t2.stop(now - 3900, FABLE)                     # 65 min ago: probe is due
    sweep()
    assert sm.sent == [("s2", CONT)]


def test_server_error_stop_is_resumed(env):
    sm, mon, session, sweep = env
    t = session("s1")
    now = time.time()
    t.user(now - 1300)
    t.stop(now - 1200, "API Error: Connection lost mid-response.", error="server_error")
    sweep()
    assert sm.sent == [("s1", CONT)]


def test_reattempts_if_a_resume_did_not_take(env):
    sm, mon, session, sweep = env
    _expired_limit(session("s1"))
    sweep()
    assert len(sm.sent) == 1
    sid, (stop_ts, _at) = "s1", mon._limit_attempts["s1"]
    mon._limit_attempts[sid] = (stop_ts, time.time() - hm.LIMIT_REATTEMPT_SECONDS - 1)
    sweep()                                        # transcript still ends on the stop
    assert len(sm.sent) == 2


def test_startup_grace_and_kill_switch(env, monkeypatch):
    sm, mon, session, sweep = env
    _expired_limit(session("s1"))
    mon._started_at = time.time()                  # daemon just started
    sweep()
    assert sm.sent == []
    mon._started_at = time.time() - 3600
    monkeypatch.setattr(hm, "LIMIT_BACKSTOP", False)
    sweep()
    assert sm.sent == []


def test_utility_sessions_are_skipped(env):
    sm, mon, session, sweep = env
    _expired_limit(session("p1"))
    sm._sessions["p1"].session_type = "planner"
    sweep()
    assert sm.sent == []


def test_unchanged_transcript_is_not_reread(env, monkeypatch):
    sm, mon, session, sweep = env
    t = session("s1")
    now = time.time()
    t.user(now - 700)
    t.stop(now - 600, limit_text(now + 3600))
    reads = []
    real = hm.stopped_on_error
    monkeypatch.setattr(hm, "stopped_on_error", lambda *a, **k: reads.append(1) or real(*a, **k))
    sweep()
    sweep()
    assert len(reads) == 1
