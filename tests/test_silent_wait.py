"""Tests for the silent-wait backstop (HealthMonitor job 5, daemon/silent_wait.py).

Every earlier stall fix recognised one specific cause, and each new cause got
through until someone diagnosed it.  Job 5 recognises the symptom instead: an
IDLE session whose final message says it is waiting on background work it
launched in this task, while nothing is alive (no writes anywhere in its
footprint, no background command running, no scheduled wake-up).  These tests
pin the wording rule against real final messages, the liveness inputs, every
decision branch of the monitor, and the daemon hooks that feed it.
"""

import asyncio
import inspect
import os
import sys
import threading
import time
import types
from pathlib import Path

import pytest

from daemon import health_monitor as hm
from daemon import silent_wait as sw
from daemon.health_monitor import HealthMonitor
from daemon.session_manager import SessionInfo, SessionManager, SessionState
from daemon.silent_wait import (
    footprint_last_write,
    live_commands,
    says_waiting_on_background,
    task_output_dir,
)

# ---------------------------------------------------------------------------
# Wording (real final messages from the local session history)
# ---------------------------------------------------------------------------

WAITING_ON_BACKGROUND = [
    # 2026-10-06: the 4h40m stall
    "| Webhooks and Okta/Google linking (3) | Fixes in, waiting on its final report |\n\n"
    "Once the last two report I'll re-run their tests, then run the full backend suite.",
    "I'll triage once the run finishes; waiting on its completion notice.",
    "The full-backend regression is still running in the background. I'll report when it lands.",
    "Waiting on the backend task notification, then the frontend gate runs immediately.",
    "The adversarial reviewer is still running. I am waiting on their completion notifications.",
    "Root in progress — the task will notify me when it completes.",
    "Re-armed the monitor; e2e is still running.",
]

WAITING_ON_THE_USER_OR_DONE = [
    "Cleaned up. Waiting on your pick (H1 / H2 / H3 / combo) to build it live.",
    "Say the word if you want it before this is called release-ready.",
    "No rush, nothing's live. Ping me when you've got the token.",
    "Waiting for the user to confirm the migration plan.",
    "Done. All 16 findings are fixed on `fix/security-scan-oct06`. Nothing is committed.",
    'Reply "go" to take all three. Once you do, I\'ll start with the seeded audit database.',
    "Commit + push it all?",
    "I'm waiting for your go-ahead before deploying.",
    "Waiting on your decision about the Jira domains.",
    "",
]


@pytest.mark.parametrize("text", WAITING_ON_BACKGROUND)
def test_wording_detects_waiting_on_background(text):
    assert says_waiting_on_background(text)


@pytest.mark.parametrize("text", WAITING_ON_THE_USER_OR_DONE)
def test_wording_ignores_waiting_on_user_and_done(text):
    assert not says_waiting_on_background(text)


# ---------------------------------------------------------------------------
# Footprint
# ---------------------------------------------------------------------------

def _footprint(tmp_path, monkeypatch):
    monkeypatch.setattr(sw.tempfile, "gettempdir", lambda: str(tmp_path / "temp"))
    root = tmp_path / "projects" / "C--proj" / "sid1.jsonl"
    sub = root.parent / "sid1" / "subagents"
    sub.mkdir(parents=True)
    root.write_text("x", encoding="utf-8")
    agent = sub / "agent-a1.jsonl"
    agent.write_text("x", encoding="utf-8")
    tasks = task_output_dir(root)
    tasks.mkdir(parents=True)
    out = tasks / "b1.output"
    out.write_text("x", encoding="utf-8")
    return root, agent, out


def test_task_output_dir_matches_cli_layout(monkeypatch):
    monkeypatch.setattr(sw.tempfile, "gettempdir", lambda: "TMP")
    p = task_output_dir(Path("x") / "C--Users-me-code-Proj" / "abc.jsonl")
    assert p.parts[-5:] == ("TMP", "claude", "C--Users-me-code-Proj", "abc", "tasks")


def test_footprint_takes_newest_write_anywhere(tmp_path, monkeypatch):
    root, agent, out = _footprint(tmp_path, monkeypatch)
    os.utime(root, (1000, 1000))
    os.utime(agent, (2000, 2000))
    os.utime(out, (3000, 3000))
    assert footprint_last_write(root) == pytest.approx(3000)
    os.utime(out, (500, 500))
    assert footprint_last_write(root) == pytest.approx(2000)   # agent transcript


def test_footprint_with_nothing_on_disk_is_zero(tmp_path, monkeypatch):
    monkeypatch.setattr(sw.tempfile, "gettempdir", lambda: str(tmp_path / "temp"))
    assert footprint_last_write(tmp_path / "nope" / "sid.jsonl") == 0.0


# ---------------------------------------------------------------------------
# Live background commands (fake psutil: no real process tree needed)
# ---------------------------------------------------------------------------

class _Proc:
    def __init__(self, pid, name, created, children=(), broken=False):
        self.pid, self._name, self._created = pid, name, created
        self._children, self._broken = list(children), broken

    def name(self):
        if self._broken:
            raise _psutil_mod.Error()
        return self._name

    def create_time(self):
        return self._created

    def children(self, recursive=False):
        out = []
        for c in self._children:
            out.append(c)
            if recursive:
                out.extend(c.children(recursive=True))
        return out


_psutil_mod = types.ModuleType("psutil")


class _PsError(Exception):
    pass


_psutil_mod.Error = _PsError


def _install_psutil(monkeypatch, procs):
    def Process(pid):
        if pid not in procs:
            raise _PsError()
        return procs[pid]
    _psutil_mod.Process = Process
    monkeypatch.setitem(sys.modules, "psutil", _psutil_mod)


def test_live_commands_lists_shells_started_after_cli(monkeypatch):
    mcp_launcher = _Proc(11, "cmd.exe", 1001)                    # started with the CLI
    node_server = _Proc(12, "node.exe", 1002)                    # not a shell
    bg_shell = _Proc(13, "bash.exe", 5000, [_Proc(14, "python.exe", 5001)])
    broken = _Proc(15, "bash.exe", 6000, broken=True)            # vanished mid-scan
    cli = _Proc(10, "claude.exe", 1000, [mcp_launcher, node_server, bg_shell, broken])
    _install_psutil(monkeypatch, {10: cli})
    assert live_commands(10) == ["bash[13]"]


def test_live_commands_empty_when_nothing_runs(monkeypatch):
    _install_psutil(monkeypatch, {10: _Proc(10, "claude.exe", 1000, [_Proc(11, "conhost.exe", 1000)])})
    assert live_commands(10) == []


@pytest.mark.parametrize("pid", [0, None, "garbage", 99])
def test_live_commands_unknown_without_a_live_cli_pid(monkeypatch, pid):
    _install_psutil(monkeypatch, {10: _Proc(10, "claude.exe", 1000)})
    assert live_commands(pid) is None


def test_live_commands_unknown_for_recycled_pid(monkeypatch):
    _install_psutil(monkeypatch, {10: _Proc(10, "explorer.exe", 1000, [_Proc(11, "bash.exe", 9000)])})
    assert live_commands(10) is None


def test_live_commands_unknown_without_psutil(monkeypatch):
    monkeypatch.setitem(sys.modules, "psutil", None)   # import psutil -> ImportError
    assert live_commands(10) is None


# ---------------------------------------------------------------------------
# HealthMonitor job 5 decisions
# ---------------------------------------------------------------------------

class Entry:
    def __init__(self, kind, text):
        self.kind, self.text, self.timestamp = kind, text, time.time()


class Info:
    def __init__(self, text="Fixes in, waiting on its final report.", bg_ago=1500, task_ago=3600):
        now = time.time()
        self.session_id = "s1"
        self.name = "Security fixes"
        self.state = SessionState.IDLE
        self.entries = [Entry("user", "fix all of these"), Entry("asst", text)]
        self.working_since = 0.0
        self.substatus = ""
        self._lock = threading.Lock()
        self._interrupted = False
        self._wakeup_pending = False
        self._wakeup_is_scheduled = False
        self._wakeup_deadline = 0.0
        self.created_ts = now - 7200
        self._task_started_at = now - task_ago
        self._bg_work_at = now - bg_ago
        self._cli_pid = 4242
        self.cwd = ""


class Queue:
    def __init__(self):
        self.q = {}

    def get_queue_data(self, sid):
        return self.q.get(sid)


class Manager:
    def __init__(self, info, send_ok=True):
        self._lock = threading.Lock()
        self._sessions = {info.session_id: info}
        self._mq = Queue()
        self.sent, self.emitted, self.pushed = [], [], []
        self._send_ok = send_ok
        self._push_callback = lambda ev, data: self.pushed.append((ev, data))

    def send_message(self, sid, text):
        self.sent.append((sid, text))
        if not self._send_ok:
            return {"ok": False, "error": "boom"}
        info = self._sessions[sid]
        info.state = SessionState.WORKING
        info.working_since = time.time()
        info._task_started_at = time.time()      # mirrors the real genuine-send stamp
        return {"ok": True}

    def interrupt_session(self, sid, clear_queue=True):
        return {"ok": True}

    def _emit_entry(self, sid, entry, index):
        self.emitted.append((sid, entry, index))


@pytest.fixture
def liveness(monkeypatch):
    """Control the two liveness inputs: running commands and footprint writes."""
    state = {"live": [], "last_write": 0.0}
    monkeypatch.setattr(hm, "live_commands", lambda pid: state["live"])
    monkeypatch.setattr(hm, "footprint_last_write", lambda path: state["last_write"])
    return state


def _idle_for(mon, info, minutes):
    """Pretend the monitor has watched ``info`` sit IDLE for ``minutes``."""
    mon._idle_since[info.session_id] = time.time() - minutes * 60
    mon._silent_checked_at.pop(info.session_id, None)
    mon.tick()


def _setup(text=None, **kw):
    info = Info(**({"text": text} if text is not None else {}), **kw)
    sm = Manager(info)
    return info, sm, HealthMonitor(sm)


def test_dead_wait_nudged_after_ten_quiet_minutes(liveness):
    info, sm, mon = _setup()
    mon.tick()                                   # first IDLE sighting: nothing yet
    _idle_for(mon, info, 5)
    assert sm.sent == []                         # 5 min: inside the window
    _idle_for(mon, info, 15)
    assert len(sm.sent) == 1
    text = sm.sent[0][1]
    assert text.startswith("[VibeNode watchdog]") and "15 minutes" in text
    assert any("waiting 15 min" in e.text for _, e, _ in sm.emitted)
    assert sm.pushed == []                       # a nudge is not an escalation


def test_waiting_on_the_user_is_left_alone(liveness):
    info, sm, mon = _setup(text="Waiting on your pick (H1 / H2) to build it live.")
    _idle_for(mon, info, 60)
    assert sm.sent == []


def test_finished_turn_is_left_alone(liveness):
    info, sm, mon = _setup(text="Done. Everything is fixed and verified.")
    _idle_for(mon, info, 60)
    assert sm.sent == []


def test_no_background_work_in_this_task_is_left_alone(liveness):
    # Background work happened, but before the user's latest message.
    info, sm, mon = _setup(bg_ago=7000, task_ago=3600)
    _idle_for(mon, info, 60)
    assert sm.sent == []


def test_running_command_needs_the_long_window(liveness):
    liveness["live"] = ["bash[77]"]
    info, sm, mon = _setup()
    _idle_for(mon, info, 30)
    assert sm.sent == []                         # quiet but alive: healthy so far
    info._bg_work_at = time.time() - 3600
    _idle_for(mon, info, 50)
    assert len(sm.sent) == 1
    assert "bash[77]" in sm.sent[0][1]           # names what is still running


def test_unknown_liveness_uses_the_middle_window(liveness):
    liveness["live"] = None                      # psutil missing
    info, sm, mon = _setup()
    _idle_for(mon, info, 20)
    assert sm.sent == []
    info._bg_work_at = time.time() - 3600
    _idle_for(mon, info, 35)
    assert len(sm.sent) == 1


def test_recent_footprint_write_means_alive(liveness, tmp_path):
    info, sm, mon = _setup()
    mon._transcript_paths[info.session_id] = tmp_path   # any existing path
    liveness["last_write"] = time.time() - 60           # an agent wrote 1 min ago
    _idle_for(mon, info, 30)
    assert sm.sent == []


def test_recent_task_event_means_alive(liveness):
    info, sm, mon = _setup(bg_ago=60)                   # task_progress 1 min ago
    _idle_for(mon, info, 30)
    assert sm.sent == []


def test_scheduled_wakeup_pending_is_left_alone(liveness):
    info, sm, mon = _setup()
    info._wakeup_is_scheduled = True
    info._wakeup_deadline = time.time() + 600
    _idle_for(mon, info, 30)
    assert sm.sent == []
    info._wakeup_deadline = time.time() - 1             # overdue: no longer protects
    _idle_for(mon, info, 30)
    assert len(sm.sent) == 1


def test_user_stopped_or_queued_is_left_alone(liveness):
    info, sm, mon = _setup()
    info._interrupted = True
    _idle_for(mon, info, 30)
    assert sm.sent == []
    info._interrupted = False
    sm._mq.q[info.session_id] = {"messages": ["next"]}
    _idle_for(mon, info, 30)
    assert sm.sent == []


def test_nudge_once_then_escalate_once(liveness):
    info, sm, mon = _setup()
    _idle_for(mon, info, 15)
    assert len(sm.sent) == 1                     # the one nudge

    mon.tick()                                   # nudge turn running: WORKING
    info.state = SessionState.IDLE               # ...and it ends still "waiting"
    info.entries.append(Entry("asst", "Still waiting on the reviewer's report."))
    mon.tick()                                   # new IDLE episode starts
    _idle_for(mon, info, 15)

    assert len(sm.sent) == 1                     # never a second nudge
    errors = [e for _, e, _ in sm.emitted if getattr(e, "is_error", False)]
    assert len(errors) == 1 and "It needs you" in errors[0].text
    assert [ev for ev, _ in sm.pushed] == ["session_stalled"]
    assert sm.pushed[0][1]["session_id"] == info.session_id
    assert sm.pushed[0][1]["name"] == "Security fixes"

    _idle_for(mon, info, 60)                     # still dead: told once is enough
    assert len(sm.sent) == 1 and len(sm.pushed) == 1


def test_new_user_task_rearms_the_backstop(liveness):
    info, sm, mon = _setup()
    _idle_for(mon, info, 15)
    mon.tick()
    info.state = SessionState.IDLE
    mon.tick()
    _idle_for(mon, info, 15)                     # escalated
    assert len(sm.pushed) == 1
    # The user sends a new task that launches new background work, which dies.
    info._task_started_at = time.time() - 1200
    info._bg_work_at = time.time() - 1100
    mon.tick()
    _idle_for(mon, info, 15)
    assert len(sm.sent) == 2                     # a fresh task gets a fresh nudge


def test_failed_nudge_escalates_immediately(liveness):
    info = Info()
    sm = Manager(info, send_ok=False)
    mon = HealthMonitor(sm)
    _idle_for(mon, info, 15)
    assert [ev for ev, _ in sm.pushed] == ["session_stalled"]


def test_job5_can_be_disabled(liveness, monkeypatch):
    monkeypatch.setattr(hm, "SILENT_WAIT_RECOVERY", False)
    info, sm, mon = _setup()
    _idle_for(mon, info, 60)
    assert sm.sent == [] and sm.pushed == []


def test_job5_bookkeeping_pruned_with_session(liveness):
    info, sm, mon = _setup()
    _idle_for(mon, info, 15)
    assert info.session_id in mon._silent_nudged
    del sm._sessions[info.session_id]
    mon.tick()
    assert info.session_id not in mon._silent_nudged
    assert info.session_id not in mon._silent_checked_at
    assert info.session_id not in mon._silent_escalated


# ---------------------------------------------------------------------------
# Daemon hooks that feed job 5 (session_manager.py)
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("name, inp, expected", [
    ("Agent", {"description": "d", "prompt": "p"}, True),          # background default
    ("Agent", {"run_in_background": False}, False),                # explicit foreground
    ("Task", {}, True),
    ("Agent", None, True),
    ("Bash", {"command": "pytest", "run_in_background": True}, True),
    ("Bash", {"command": "pytest"}, False),
    ("PowerShell", {"command": "x", "run_in_background": True}, True),
    ("Monitor", {}, True),
    ("ScheduleWakeup", {"delaySeconds": 60}, False),               # a timer, not work
    ("Read", {"file_path": "x"}, False),
    ("", {}, False),
])
def test_tool_launches_background(name, inp, expected):
    assert SessionManager._tool_launches_background(name, inp) is expected


def _run_process_message(sm, info, msg):
    loop = asyncio.new_event_loop()
    try:
        loop.run_until_complete(sm._process_message(info.session_id, msg))
    finally:
        loop.close()


def _live_manager(sid):
    sm = SessionManager()
    info = SessionInfo(session_id=sid)
    info.state = SessionState.WORKING
    with sm._lock:
        sm._sessions[sid] = info
    sm._push_callback = lambda *a, **k: None
    return sm, info


@pytest.mark.parametrize("tool, inp, stamped", [
    ("Agent", {"description": "review", "prompt": "go"}, True),
    ("Bash", {"command": "ls"}, False),
])
def test_tool_use_stamps_background_work(tool, inp, stamped):
    from daemon.backends.messages import BlockKind, MessageKind, VibeNodeMessage
    sm, info = _live_manager("bg-tool-sid")
    msg = VibeNodeMessage(kind=MessageKind.ASSISTANT, blocks=[{
        "kind": BlockKind.TOOL_USE.value, "name": tool, "id": "tu1", "input": inp,
    }])
    _run_process_message(sm, info, msg)
    assert (info._bg_work_at > 0) is stamped


@pytest.mark.parametrize("subtype, stamped", [
    ("task_started", True),
    ("task_progress", True),
    ("task_notification", True),
    ("background_tasks_changed", True),
    ("status", False),
    ("thinking_tokens", False),
])
def test_cli_task_events_stamp_background_work(subtype, stamped):
    from daemon.backends.messages import MessageKind, VibeNodeMessage
    sm, info = _live_manager("bg-sys-sid")
    msg = VibeNodeMessage(kind=MessageKind.SYSTEM, subtype=subtype,
                          data={"type": "system", "subtype": subtype, "task_id": "t1"})
    _run_process_message(sm, info, msg)
    assert (info._bg_work_at > 0) is stamped


def test_genuine_send_stamps_task_start_and_resends_do_not():
    """send_message must stamp _task_started_at inside the genuine-send block
    (user message / queue dispatch / watchdog nudge), not for self-heal or
    auto-retry resends, which continue the current task."""
    src = inspect.getsource(SessionManager.send_message)
    gate = src.index("if not _self_heal and not _auto_retry:")
    stamp = src.index("info._task_started_at = time.time()")
    retry_reset = src.index("self._clear_api_retry(info, reset_count=True)")
    assert gate < stamp < retry_reset
