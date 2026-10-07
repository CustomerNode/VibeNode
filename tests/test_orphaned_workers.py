"""Tests for daemon/orphaned_workers.py — stranded background-worker detection.

Claude Code delivers a nested agent's completion notice (an agent launched BY a
background worker) to the top-level session instead of to the worker that
launched it.  A worker that stopped to wait for its own reviewer therefore
waits forever (incident 2026-10-06: two workers stranded 343 and 317 minutes).
These tests build the exact on-disk shapes the CLI writes (root transcript
``queue-operation`` lines, ``subagents/agent-<id>.meta.json`` +
``agent-<id>.jsonl``) and drive the detector against real files.
"""

import json
from datetime import datetime, timezone

import pytest

from daemon.orphaned_workers import (
    ScanCache,
    build_nudge,
    find_orphaned_workers,
)

SID = "11111111-2222-3333-4444-555555555555"
T0 = 1_780_000_000.0  # fixed epoch base so timestamps are deterministic


def iso(t: float) -> str:
    return datetime.fromtimestamp(t, timezone.utc).isoformat().replace("+00:00", "Z")


def _line(obj) -> str:
    return json.dumps(obj, separators=(",", ":")) + "\n"


class SessionFiles:
    """Builds a root session + subagents tree the way the CLI lays it out."""

    def __init__(self, base):
        self.root = base / "C--proj" / f"{SID}.jsonl"
        self.sub = base / "C--proj" / SID / "subagents"
        self.sub.mkdir(parents=True)
        self.root.write_text(_line({"type": "user", "timestamp": iso(T0),
                                    "message": {"role": "user", "content": "go"}}),
                             encoding="utf-8")

    # ── agents ──
    def agent(self, agent_id, description, parent=None):
        meta = {"agentType": "general-purpose", "description": description,
                "spawnDepth": 2 if parent else 1}
        if parent:
            meta["parentAgentId"] = parent
        (self.sub / f"agent-{agent_id}.meta.json").write_text(json.dumps(meta), encoding="utf-8")
        (self.sub / f"agent-{agent_id}.jsonl").write_text("", encoding="utf-8")

    def _append(self, agent_id, obj):
        with open(self.sub / f"agent-{agent_id}.jsonl", "a", encoding="utf-8") as fh:
            fh.write(_line(obj))

    def tool_round(self, agent_id, t):
        self._append(agent_id, {"type": "assistant", "timestamp": iso(t), "message": {
            "role": "assistant", "stop_reason": "tool_use",
            "content": [{"type": "tool_use", "id": "tu", "name": "Bash", "input": {}}]}})
        self._append(agent_id, {"type": "user", "timestamp": iso(t + 1), "message": {
            "role": "user", "content": [{"type": "tool_result", "tool_use_id": "tu", "content": "ok"}]}})

    def stop(self, agent_id, t, text):
        self._append(agent_id, {"type": "attachment", "timestamp": iso(t)})
        self._append(agent_id, {"type": "assistant", "timestamp": iso(t), "message": {
            "role": "assistant", "stop_reason": "end_turn",
            "content": [{"type": "text", "text": text}]}})

    def resume(self, agent_id, t, text):
        self._append(agent_id, {"type": "user", "timestamp": iso(t),
                                "message": {"role": "user", "content": text}})

    # ── root deliveries ──
    def deliver(self, task_id, t, status="completed"):
        """Append a completion notice the way the CLI records it in the root:
        the enqueue line plus the echoes (remove + user turn) that must NOT
        count as separate deliveries."""
        content = (
            "<task-notification>\n"
            f"<task-id>{task_id}</task-id>\n"
            "<tool-use-id>toolu_x</tool-use-id>\n"
            f"<output-file>C:\\tmp\\tasks\\{task_id}.output</output-file>\n"
            f"<status>{status}</status>\n"
            "<summary>Agent finished</summary>\n"
            "<result>report text</result>\n"
            "</task-notification>"
        )
        with open(self.root, "a", encoding="utf-8") as fh:
            fh.write(_line({"type": "queue-operation", "operation": "enqueue",
                            "timestamp": iso(t), "sessionId": SID, "content": content}))
            fh.write(_line({"type": "queue-operation", "operation": "remove",
                            "timestamp": iso(t + 3), "sessionId": SID, "content": content}))
            fh.write(_line({"type": "user", "timestamp": iso(t + 3),
                            "message": {"role": "user", "content": content}}))

    def root_note(self, t):
        """Unrelated root activity (grows the transcript)."""
        with open(self.root, "a", encoding="utf-8") as fh:
            fh.write(_line({"type": "assistant", "timestamp": iso(t), "message": {
                "role": "assistant", "content": [{"type": "text", "text": "noted"}]}}))


@pytest.fixture
def s(tmp_path):
    return SessionFiles(tmp_path)


def _incident(s):
    """The 2026-10-06 shape: worker launches a reviewer, stops to wait,
    the reviewer finishes later and its notice goes to the root."""
    s.agent("w1", "Fix webhook + SSO findings")
    s.agent("r1", "Adversarial review of security diff", parent="w1")
    s.tool_round("w1", T0 + 10)
    s.stop("w1", T0 + 100, "Fixes green. I'm waiting on the adversarial review I launched.")
    s.stop("r1", T0 + 500, "Review done.")
    s.deliver("w1", T0 + 101)          # the worker's own (interim) stop notice
    s.deliver("r1", T0 + 501)          # misrouted: should have gone to w1


# ---------------------------------------------------------------------------
# Detection
# ---------------------------------------------------------------------------

def test_worker_stopped_before_nested_result_is_stranded(s):
    _incident(s)
    found = find_orphaned_workers(s.root, ScanCache())
    assert len(found) == 1
    o = found[0]
    assert (o.worker_id, o.nested_id, o.reason) == ("w1", "r1", "stopped-before-result")
    assert o.worker_desc == "Fix webhook + SSO findings"
    assert o.nested_desc == "Adversarial review of security diff"
    assert o.status == "completed"
    assert o.output_file.endswith("r1.output")
    assert o.delivered_at == pytest.approx(T0 + 501, abs=1e-3)


def test_nested_agent_still_running_is_not_stranded(s):
    s.agent("w1", "worker")
    s.agent("r1", "reviewer", parent="w1")
    s.stop("w1", T0 + 100, "waiting on the reviewer")
    s.tool_round("r1", T0 + 50)        # reviewer still working, no delivery yet
    assert find_orphaned_workers(s.root, ScanCache()) == []


def test_worker_resumed_after_result_is_not_stranded(s):
    _incident(s)
    # The root forwarded the review; the worker acted on it and finished.
    s.resume("w1", T0 + 600, "The coordinator sent a message while you were working: ...")
    s.tool_round("w1", T0 + 610)
    s.stop("w1", T0 + 700, "Final report: all items handled.")
    assert find_orphaned_workers(s.root, ScanCache()) == []


def test_worker_that_kept_working_past_result_then_waits_is_stranded(s):
    s.agent("w1", "worker")
    s.agent("r1", "reviewer", parent="w1")
    s.stop("r1", T0 + 50, "done")
    s.deliver("r1", T0 + 51)            # reviewer finished while w1 still busy
    s.tool_round("w1", T0 + 60)
    s.stop("w1", T0 + 120, "Tests pass. Still waiting for the reviewer to report.")
    found = find_orphaned_workers(s.root, ScanCache())
    assert [(o.worker_id, o.reason) for o in found] == [("w1", "still-waiting")]


def test_cli_system_notice_resume_does_not_count_as_forwarding(s):
    """A worker woken by its OWN background Bash finishing still never got the
    nested result, so it is still stranded."""
    s.agent("w1", "worker")
    s.agent("r1", "reviewer", parent="w1")
    s.stop("r1", T0 + 50, "done")
    s.deliver("r1", T0 + 51)
    s.stop("w1", T0 + 40, "waiting on the scanner and the reviewer")
    s.resume("w1", T0 + 70, "[SYSTEM NOTIFICATION - NOT USER INPUT]\nscanner finished")
    s.stop("w1", T0 + 80, "Scanner green. The reviewer has not reported; still waiting on it.")
    found = find_orphaned_workers(s.root, ScanCache())
    assert [(o.worker_id, o.reason) for o in found] == [("w1", "still-waiting")]


def test_worker_engaged_by_coordinator_after_result_is_left_alone(s):
    s.agent("w1", "worker")
    s.agent("r1", "reviewer", parent="w1")
    s.stop("r1", T0 + 50, "done")
    s.deliver("r1", T0 + 51)
    s.resume("w1", T0 + 70, "The coordinator sent a message while you were working: status?")
    s.stop("w1", T0 + 80, "Still waiting on a separate deploy check.")
    assert find_orphaned_workers(s.root, ScanCache()) == []


def test_finished_worker_not_waiting_is_left_alone(s):
    s.agent("w1", "worker")
    s.agent("r1", "reviewer", parent="w1")
    s.stop("r1", T0 + 50, "done")
    s.deliver("r1", T0 + 51)
    s.tool_round("w1", T0 + 60)
    s.stop("w1", T0 + 120, "Final report: everything is fixed.")
    assert find_orphaned_workers(s.root, ScanCache()) == []


def test_worker_mid_tool_call_is_not_stranded(s):
    _incident(s)
    # Worker was resumed and is now inside a long tool call (last entry is a
    # tool_use with no end_turn): it is running, not stranded.
    s.resume("w1", T0 + 600, "The coordinator sent a message while you were working: go")
    s._append("w1", {"type": "assistant", "timestamp": iso(T0 + 610), "message": {
        "role": "assistant", "stop_reason": "tool_use",
        "content": [{"type": "tool_use", "id": "x", "name": "Bash", "input": {}}]}})
    assert find_orphaned_workers(s.root, ScanCache()) == []


def test_torn_final_line_means_running(s):
    _incident(s)
    with open(s.sub / "agent-w1.jsonl", "a", encoding="utf-8") as fh:
        fh.write('{"type":"assistant","timestamp":"' + iso(T0 + 700) + '","mess')
    assert find_orphaned_workers(s.root, ScanCache()) == []


def test_direct_children_only_never_flagged(s):
    s.agent("w1", "worker")                    # no parentAgentId: root's own child
    s.stop("w1", T0 + 100, "waiting on things")
    s.deliver("w1", T0 + 101)
    cache = ScanCache()
    assert find_orphaned_workers(s.root, cache) == []
    assert cache.offset == 0                   # root transcript not even read


def test_delivery_before_since_is_ignored(s):
    _incident(s)
    assert find_orphaned_workers(s.root, ScanCache(), since=T0 + 900) == []
    assert len(find_orphaned_workers(s.root, ScanCache(), since=T0 + 400)) == 1


def test_no_subagents_dir(tmp_path):
    root = tmp_path / "p" / f"{SID}.jsonl"
    root.parent.mkdir()
    root.write_text("", encoding="utf-8")
    assert find_orphaned_workers(root, ScanCache()) == []


def test_only_enqueue_lines_count_as_deliveries(s):
    s.agent("w1", "worker")
    s.agent("r1", "reviewer", parent="w1")
    s.stop("w1", T0 + 100, "waiting on the reviewer")
    content = "<task-notification>\n<task-id>r1</task-id>\n<status>completed</status>\n</task-notification>"
    with open(s.root, "a", encoding="utf-8") as fh:
        fh.write(_line({"type": "queue-operation", "operation": "remove",
                        "timestamp": iso(T0 + 501), "content": content}))
        fh.write(_line({"type": "user", "timestamp": iso(T0 + 501),
                        "message": {"role": "user", "content": content}}))
    assert find_orphaned_workers(s.root, ScanCache()) == []


# ---------------------------------------------------------------------------
# Incremental scanning
# ---------------------------------------------------------------------------

def test_unchanged_transcript_is_not_reevaluated(s):
    _incident(s)
    cache = ScanCache()
    assert len(find_orphaned_workers(s.root, cache)) == 1
    assert find_orphaned_workers(s.root, cache) == []       # nothing changed
    s.root_note(T0 + 800)                                    # transcript grew
    assert len(find_orphaned_workers(s.root, cache)) == 1    # re-evaluated


def test_partial_trailing_line_is_read_once_complete(s):
    s.agent("w1", "worker")
    s.agent("r1", "reviewer", parent="w1")
    s.stop("w1", T0 + 100, "waiting on the reviewer")
    s.stop("r1", T0 + 500, "done")
    cache = ScanCache()
    full = _line({"type": "queue-operation", "operation": "enqueue", "timestamp": iso(T0 + 501),
                  "content": "<task-notification>\n<task-id>r1</task-id>\n"
                             "<status>completed</status>\n</task-notification>"})
    with open(s.root, "a", encoding="utf-8") as fh:
        fh.write(full[:40])                                  # CLI mid-write
    assert find_orphaned_workers(s.root, cache) == []
    with open(s.root, "a", encoding="utf-8") as fh:
        fh.write(full[40:])                                  # line completed
    assert [o.nested_id for o in find_orphaned_workers(s.root, cache)] == ["r1"]


def test_rewritten_shorter_transcript_is_rescanned(s):
    _incident(s)
    cache = ScanCache()
    assert len(find_orphaned_workers(s.root, cache)) == 1
    # Rewind/repair rewrites the transcript shorter, dropping the delivery.
    s.root.write_text(_line({"type": "user", "timestamp": iso(T0), "message": {
        "role": "user", "content": "go"}}), encoding="utf-8")
    assert find_orphaned_workers(s.root, cache) == []
    assert "r1" not in cache.deliveries


def test_latest_delivery_of_a_resumed_nested_agent_wins(s):
    _incident(s)
    s.resume("w1", T0 + 600, "The coordinator sent a message while you were working: go")
    s.stop("w1", T0 + 650, "Waiting on a second review pass.")
    # The reviewer is resumed and stops again: a NEW delivery after the
    # worker's latest stop strands the worker again.
    s.deliver("r1", T0 + 900)
    found = find_orphaned_workers(s.root, ScanCache())
    assert [(o.nested_id, o.reason) for o in found] == [("r1", "stopped-before-result")]
    assert found[0].delivered_at == pytest.approx(T0 + 900, abs=1e-3)


def test_large_root_transcript_spanning_chunks(s, monkeypatch):
    import daemon.orphaned_workers as ow
    monkeypatch.setattr(ow, "_CHUNK_BYTES", 64)              # force many chunks
    _incident(s)
    for i in range(50):
        s.root_note(T0 + 1000 + i)
    found = find_orphaned_workers(s.root, ScanCache())
    assert [o.nested_id for o in found] == ["r1"]


# ---------------------------------------------------------------------------
# Nudge text
# ---------------------------------------------------------------------------

def test_nudge_names_worker_result_and_the_fix(s):
    _incident(s)
    text = build_nudge(find_orphaned_workers(s.root, ScanCache()))
    assert text.startswith("[VibeNode watchdog]")
    assert "w1" in text and "r1" in text
    assert "Fix webhook + SSO findings" in text
    assert "r1.output" in text
    assert "SendMessage" in text
    assert "Do not end your turn" in text
