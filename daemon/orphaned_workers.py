"""Detect background workers stranded by Claude Code's nested-agent routing.

The failure (diagnosed 2026-10-06, Claude Code CLI 2.1.291)
-----------------------------------------------------------
A session (the "root") launches background workers with the Agent tool.  A
worker then launches its OWN background agent (typically an adversarial
reviewer) and ends its turn to wait for it, which the CLI explicitly invites
("It may resume on its own when that work completes").  When the nested agent
finishes, the CLI enqueues its completion notice on the ROOT session's queue,
never on the worker that launched it.  The worker is never woken.  The root,
for its part, ends its turn "waiting on the worker's final report".  Nothing
is left running, so nothing will ever wake anyone: the whole tree sits idle
until a human notices.

In the incident that prompted this module, two workers sat for 343 and 317
minutes after their reviewers had finished.  A scan of every transcript on the
machine showed that a nested agent's result has never reached the worker that
launched it in any CLI version observed (2.1.235 through 2.1.291).  Older
workers kept working rather than stopping to wait, so it never deadlocked
before.

VibeNode cannot fix the routing; it lives in the CLI binary.  It CAN detect
the stranded state from the files the CLI writes, and tell the root session
exactly what to forward.  ``HealthMonitor`` (job 4) does that with the result
of :func:`find_orphaned_workers`.

On-disk evidence used (all written by the CLI)
----------------------------------------------
* ``<project>/<sid>.jsonl``: the root transcript.  Every background-task
  completion delivered to the root is recorded as a ``queue-operation`` /
  ``enqueue`` line whose ``content`` is a ``<task-notification>`` block
  (task-id, status, output-file, summary).
* ``<project>/<sid>/subagents/agent-<id>.meta.json``: one per agent.  Nested
  agents carry ``parentAgentId`` (the worker that launched them); direct
  children of the root do not.
* ``<project>/<sid>/subagents/agent-<id>.jsonl``: each agent's own
  transcript.  A stopped agent's last conversational entry is an ``assistant``
  message with ``stop_reason == "end_turn"``.  A resume arrives as a ``user``
  entry whose content is a plain string.

A worker W is "orphaned" on nested agent X when X's completion was delivered
to the root at time T_X, W is currently stopped, and either:

1. W stopped before T_X and has not run since (``stopped-before-result``).
   It was still waiting when X finished, and nothing can wake it.  This is
   the exact shape of the 2026-10-06 incident (both workers).
2. W ran past T_X, but its final message says it is waiting, and nothing other
   than CLI system notifications has resumed it since T_X
   (``still-waiting``).  W kept working while X finished, then stopped to
   wait for a notice that had already gone to the root.

Everything here is read-only and fail-safe.  An unreadable or unexpected file
yields "no orphans", never an exception that reaches the caller, so the
watchdog can never act on a misparse.
"""

import json
import os
import re
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Optional

# ── Notification parsing ──────────────────────────────────────────────────
_TASK_ID_RE = re.compile(r"<task-id>([^<]+)</task-id>")
_STATUS_RE = re.compile(r"<status>([^<]+)</status>")
_OUTPUT_FILE_RE = re.compile(r"<output-file>([^<]+)</output-file>")

# Byte markers for the fast pre-filter over the root transcript.  The scan
# runs on the HealthMonitor thread, so it must not parse every line of a
# multi-megabyte transcript; ``bytes.find`` jumps straight to the few lines
# that can matter.
_NOTIFY_MARK = b"<task-notification>"
_ENQUEUE_MARK = b'"enqueue"'

# Root transcripts are read in blocks of this size, so memory stays bounded
# however large the transcript grows.
_CHUNK_BYTES = 1 << 20

# Agent transcripts: read only the tail to find the last conversational
# entry.  The window grows (x4) up to the cap if a single huge final entry
# does not fit.
_TAIL_BYTES = 256 * 1024
_TAIL_MAX_BYTES = 16 * 1024 * 1024

# A stopped worker announcing that it is still waiting ("I'm waiting on the
# review I launched", "still waiting for its completion notification").
_WAITING_RE = re.compile(r"\bwait(?:ing)?\b", re.IGNORECASE)

# Prefixes of the resume messages the CLI itself injects into an agent when
# its own background Bash or Monitor finishes.  Any OTHER plain-text user
# turn reaching a worker (e.g. "The coordinator sent a message while you
# were working: ...") counts as the root or a human engaging the worker.
# Once that has happened after T_X, the worker is no longer this module's
# business.
_SYSTEM_RESUME_PREFIXES = ("[SYSTEM NOTIFICATION",)

# Human wording for the CLI's task statuses, used in the nudge text.
_STATUS_WORDS = {
    "completed": "finished",
    "failed": "failed",
    "killed": "was stopped",
    "stopped": "was stopped",
}


@dataclass
class Delivery:
    """One background task's completion notice, as recorded in the root."""
    task_id: str
    delivered_at: float          # epoch seconds (transcript timestamp)
    status: str = ""
    output_file: str = ""


@dataclass
class OrphanedWorker:
    """A worker waiting on a nested agent whose result went to the root."""
    worker_id: str
    worker_desc: str
    nested_id: str
    nested_desc: str
    delivered_at: float          # when the nested result reached the root
    status: str                  # nested agent's final status (completed/failed/...)
    output_file: str             # where the nested agent's result can be read
    reason: str                  # "stopped-before-result" | "still-waiting"


@dataclass
class ScanCache:
    """Per-session incremental state, so repeat scans only read new bytes.

    ``offset`` is the byte position just past the last complete root
    transcript line already scanned.  A transcript that shrinks (rewind,
    repair) resets the scan.  ``evaluated_size`` is the root transcript size
    at the last full evaluation.  Every event that can change the answer
    appends to the root transcript (a delivery, the root messaging a worker,
    a worker's own stop notice), so an unchanged size means an unchanged
    answer and the evaluation is skipped.
    """
    offset: int = 0
    evaluated_size: int = -1
    deliveries: dict = field(default_factory=dict)   # task_id -> Delivery (latest)
    metas: dict = field(default_factory=dict)        # filename -> (mtime_ns, meta)


def _parse_ts(value) -> Optional[float]:
    """ISO-8601 transcript timestamp (``...Z``) -> epoch seconds, or None."""
    if not isinstance(value, str) or not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def _collect_deliveries(data: bytes, cache: ScanCache) -> None:
    """Record every ``enqueue`` task-notification found in ``data``.

    ``data`` must consist of complete lines.  Only the latest delivery per
    task id is kept: a resumed agent notifies again when it next stops.
    """
    start = 0
    while True:
        hit = data.find(_NOTIFY_MARK, start)
        if hit < 0:
            return
        line_start = data.rfind(b"\n", 0, hit) + 1
        line_end = data.find(b"\n", hit)
        if line_end < 0:
            line_end = len(data)
        start = line_end + 1
        line = data[line_start:line_end]
        if _ENQUEUE_MARK not in line:
            continue  # the "remove"/attachment/user echoes of the same notice
        try:
            obj = json.loads(line)
        except ValueError:
            continue
        if obj.get("type") != "queue-operation" or obj.get("operation") != "enqueue":
            continue
        content = obj.get("content")
        if not isinstance(content, str):
            continue
        m_id = _TASK_ID_RE.search(content)
        ts = _parse_ts(obj.get("timestamp"))
        if not m_id or ts is None:
            continue
        task_id = m_id.group(1).strip()
        m_status = _STATUS_RE.search(content)
        m_out = _OUTPUT_FILE_RE.search(content)
        prev = cache.deliveries.get(task_id)
        if prev is None or ts >= prev.delivered_at:
            cache.deliveries[task_id] = Delivery(
                task_id=task_id,
                delivered_at=ts,
                status=m_status.group(1).strip() if m_status else "",
                output_file=m_out.group(1).strip() if m_out else "",
            )


def _scan_deliveries(root_path: Path, cache: ScanCache) -> None:
    """Incrementally read new complete lines of the root transcript."""
    try:
        size = os.path.getsize(root_path)
    except OSError:
        return
    if size < cache.offset:
        # Transcript was rewritten shorter (rewind / repair): start over.
        cache.offset = 0
        cache.deliveries.clear()
    if size <= cache.offset:
        return
    try:
        with open(root_path, "rb") as fh:
            fh.seek(cache.offset)
            pos = cache.offset
            carry = b""
            while pos < size:
                block = fh.read(min(_CHUNK_BYTES, size - pos))
                if not block:
                    break
                pos += len(block)
                data = carry + block
                cut = data.rfind(b"\n")
                if cut < 0:
                    carry = data      # one long line spanning blocks
                    continue
                complete, carry = data[:cut + 1], data[cut + 1:]
                _collect_deliveries(complete, cache)
                # A trailing partial line (still being written) is re-read
                # on the next scan.
                cache.offset = pos - len(carry)
    except OSError:
        return


def _load_metas(sub_dir: Path, cache: ScanCache) -> dict:
    """agent_id -> meta dict for every ``agent-<id>.meta.json`` in ``sub_dir``.

    Parsed metas are cached by mtime: the CLI rewrites a meta file when the
    agent is resumed, so the mtime is a sufficient invalidation key.
    """
    out = {}
    try:
        it = os.scandir(sub_dir)
    except OSError:
        return out
    with it:
        for entry in it:
            name = entry.name
            if not (name.startswith("agent-") and name.endswith(".meta.json")):
                continue
            try:
                mtime_ns = entry.stat().st_mtime_ns
            except OSError:
                continue
            hit = cache.metas.get(name)
            if hit is None or hit[0] != mtime_ns:
                try:
                    with open(entry.path, encoding="utf-8") as fh:
                        meta = json.load(fh)
                except (OSError, ValueError):
                    continue
                if not isinstance(meta, dict):
                    continue
                hit = (mtime_ns, meta)
                cache.metas[name] = hit
            out[name[len("agent-"):-len(".meta.json")]] = hit[1]
    return out


def _last_conversational_entry(path: Path) -> Optional[dict]:
    """The last ``user``/``assistant`` entry of an agent transcript, or None.

    Returns None if the file is missing, or if its final line is torn (the
    agent is mid-write, so it is running).  Callers treat None as "not
    stopped", which keeps the watchdog quiet.
    """
    try:
        size = os.path.getsize(path)
    except OSError:
        return None
    window = _TAIL_BYTES
    while True:
        start = max(0, size - window)
        try:
            with open(path, "rb") as fh:
                fh.seek(start)
                data = fh.read(size - start)
        except OSError:
            return None
        lines = data.split(b"\n")
        if start > 0:
            lines = lines[1:]  # the first line of a mid-file window is partial
        seen_complete = False
        for raw in reversed(lines):
            raw = raw.strip()
            if not raw:
                continue
            try:
                obj = json.loads(raw)
            except ValueError:
                if not seen_complete:
                    return None  # torn final line: agent is writing
                continue
            seen_complete = True
            if isinstance(obj, dict) and obj.get("type") in ("user", "assistant"):
                return obj
        if start == 0 or window >= _TAIL_MAX_BYTES:
            return None
        window *= 4


def _final_text(entry: dict) -> str:
    """Concatenated text blocks of an assistant transcript entry."""
    content = (entry.get("message") or {}).get("content")
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    return " ".join(
        b.get("text", "") for b in content
        if isinstance(b, dict) and b.get("type") == "text"
    )


def _stopped_state(path: Path) -> Optional[tuple]:
    """``(stopped_at, final_text)`` if the agent's turn has ended, else None."""
    last = _last_conversational_entry(path)
    if last is None or last.get("type") != "assistant":
        return None
    if (last.get("message") or {}).get("stop_reason") != "end_turn":
        return None
    stopped_at = _parse_ts(last.get("timestamp"))
    if stopped_at is None:
        return None
    return stopped_at, _final_text(last)


def _resumed_by_others_since(path: Path, since: float) -> bool:
    """True if anything other than a CLI system notice resumed the agent
    after ``since``.

    Only called on the rare ``still-waiting`` path, so a full read is fine.
    Fails *closed*: if the file cannot be read, it reports True, so the
    watchdog stays quiet.
    """
    try:
        with open(path, "rb") as fh:
            data = fh.read()
    except OSError:
        return True
    for raw in data.split(b"\n"):
        if b'"user"' not in raw or b"tool_result" in raw:
            continue
        try:
            obj = json.loads(raw)
        except ValueError:
            continue
        if not isinstance(obj, dict) or obj.get("type") != "user":
            continue
        ts = _parse_ts(obj.get("timestamp"))
        if ts is None or ts <= since:
            continue
        content = (obj.get("message") or {}).get("content")
        if isinstance(content, str) and not content.lstrip().startswith(_SYSTEM_RESUME_PREFIXES):
            return True
    return False


def find_orphaned_workers(root_transcript: Path, cache: ScanCache,
                          since: float = 0.0) -> list:
    """Workers stranded on nested agents whose results went to the root.

    Args:
        root_transcript: Path to the root session's ``<sid>.jsonl``.
        cache: The session's :class:`ScanCache` (reused across calls).
        since: Ignore deliveries before this epoch time.  The watchdog passes
            the moment the daemon took the session on, so history from
            before a daemon restart can never trigger a nudge.

    Returns:
        ``OrphanedWorker`` list sorted by delivery time.  It is empty when
        nothing is stranded, or when nothing changed since the previous call
        (results are only reported once per change, see ``ScanCache``).
    """
    root_transcript = Path(root_transcript)
    sub_dir = root_transcript.parent / root_transcript.stem / "subagents"
    if not sub_dir.is_dir():
        return []
    try:
        size = os.path.getsize(root_transcript)
    except OSError:
        return []
    if size == cache.evaluated_size:
        return []
    metas = _load_metas(sub_dir, cache)
    nested = {aid: m for aid, m in metas.items() if m.get("parentAgentId")}
    if not nested:
        cache.evaluated_size = size
        return []
    _scan_deliveries(root_transcript, cache)
    cache.evaluated_size = size

    found = []
    for nested_id, nested_meta in nested.items():
        delivery = cache.deliveries.get(nested_id)
        if delivery is None or delivery.delivered_at < since:
            continue
        worker_id = str(nested_meta.get("parentAgentId"))
        worker_path = sub_dir / f"agent-{worker_id}.jsonl"
        state = _stopped_state(worker_path)
        if state is None:
            continue  # worker is running (or unreadable): leave it alone
        stopped_at, final_text = state
        if stopped_at < delivery.delivered_at:
            reason = "stopped-before-result"
        elif (_WAITING_RE.search(final_text)
              and not _resumed_by_others_since(worker_path, delivery.delivered_at)):
            reason = "still-waiting"
        else:
            continue
        worker_meta = metas.get(worker_id) or {}
        found.append(OrphanedWorker(
            worker_id=worker_id,
            worker_desc=str(worker_meta.get("description") or "")[:120],
            nested_id=nested_id,
            nested_desc=str(nested_meta.get("description") or "")[:120],
            delivered_at=delivery.delivered_at,
            status=delivery.status,
            output_file=delivery.output_file,
            reason=reason,
        ))
    found.sort(key=lambda o: o.delivered_at)
    return found


def build_nudge(orphans: list) -> str:
    """The message sent to the root session to un-strand its workers."""
    lines = [
        "[VibeNode watchdog] Background work is stuck: a worker is waiting for "
        "a result that was delivered to you instead of to it.",
        "",
    ]
    for o in orphans:
        when = time.strftime("%H:%M", time.localtime(o.delivered_at))
        did = _STATUS_WORDS.get(o.status, o.status or "finished")
        where = f" Its result: {o.output_file}" if o.output_file else ""
        lines.append(
            f'- Worker {o.worker_id} ("{o.worker_desc}") launched agent '
            f'{o.nested_id} ("{o.nested_desc}"). That agent {did} at {when}, '
            f"but the worker never received it and will not resume on its "
            f"own.{where}"
        )
    lines += [
        "",
        "Claude Code delivers a nested agent's completion notice to the "
        "top-level session (you), never to the worker that launched it. For "
        "each worker above, send it that result with SendMessage (load the "
        'tool with ToolSearch "select:SendMessage" if needed) and tell it to '
        "finish, or finish that worker's remaining work yourself. Do not end "
        "your turn waiting on these workers until each one has been sent its "
        "result.",
    ]
    return "\n".join(lines)
