"""Guarantee: a session stopped by a usage limit always continues after it resets.

Two independent layers keep this promise.

Layer 1 lives in session_manager.py.  When a turn ends on a usage limit, the
daemon parses the reset time and schedules a timed resume
(``_schedule_limit_resume`` -> ``_arm_api_retry``).  It is immediate and shows
a countdown, but it is in-memory.  A Session Engine restart loses the timer,
and it only fires if the daemon recognised the message and saw the turn end on
a path that arms retries.

Layer 2 is this module, used by HealthMonitor job 6, and works from disk.  The
CLI writes every usage-limit stop into the transcript as a synthetic assistant
entry with ``isApiErrorMessage: true`` and ``error: "rate_limit"``.  That is a
structural tag, independent of how the message is worded (all 28 limit stops
in local history carry it).  If a session's transcript still ENDS on such an
entry after its reset time (plus a grace period) has passed, nothing resumed
it, whatever the reason.  So Layer 2 resumes it.  The same applies to
``server_error`` stops ("Connection lost mid-response"), which are always
transient.

Layer 2 must never override the user.  Only sessions the daemon lists as live
and IDLE, or as dormant in its restart memory, qualify.  An explicit Stop,
Sleep or Delete removes a session from both (``_interrupted``, STOPPED,
``forget_dormant``), so "Sleep must stick" holds.  See HealthMonitor job 6 for
those checks.  This module only reads files and does the arithmetic.  It never
raises; an unreadable or unexpected transcript means "nothing to do".
"""

import json
import os
from typing import Optional

# Error kinds the CLI tags on a synthetic stop entry that are worth resuming
# after a wait.  Both observed in local history.
RESUMABLE_ERRORS = frozenset({"rate_limit", "server_error"})

# Continue this long after a stated reset.  Longer than Layer 1's grace (90s +
# up to 30s spread) so that, when Layer 1 is alive, it always acts first and
# Layer 2 stays a pure backstop.
LIMIT_GRACE_SECONDS = 300.0
# No reset time in the message: probe after this long, doubling with each
# consecutive failed probe, up to the cap ("eventually", without hammering a
# multi-day limit every 30 minutes).
PROBE_BASE_SECONDS = 1800.0
PROBE_CAP_SECONDS = 4 * 3600.0
# A transient server error that ended a turn and was never retried.
SERVER_ERROR_DELAY_SECONDS = 600.0

_TAIL_BYTES = 512 * 1024


def _parse_ts(value) -> Optional[float]:
    if not isinstance(value, str) or not value:
        return None
    try:
        from datetime import datetime
        return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def _text(entry: dict) -> str:
    content = (entry.get("message") or {}).get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return " ".join(b.get("text", "") for b in content
                        if isinstance(b, dict) and b.get("type") == "text")
    return ""


def _is_tool_result(entry: dict) -> bool:
    content = (entry.get("message") or {}).get("content")
    return isinstance(content, list) and any(
        isinstance(b, dict) and b.get("type") == "tool_result" for b in content)


def _tail_entries(path) -> list:
    """Parsed conversational entries (user/assistant) from the transcript tail.

    Returns [] if the file is missing or its final line is torn (the CLI is
    mid-write, so the session is active).
    """
    try:
        size = os.path.getsize(path)
        with open(path, "rb") as fh:
            start = max(0, size - _TAIL_BYTES)
            fh.seek(start)
            data = fh.read()
    except OSError:
        return []
    lines = data.split(b"\n")
    if start > 0:
        lines = lines[1:]
    out = []
    for i, raw in enumerate(lines):
        raw = raw.strip()
        if not raw:
            continue
        try:
            obj = json.loads(raw)
        except ValueError:
            if i == len(lines) - 1 or not any(x.strip() for x in lines[i + 1:]):
                return []  # torn final line: the CLI is writing right now
            continue
        if isinstance(obj, dict) and obj.get("type") in ("user", "assistant"):
            out.append(obj)
    return out


def stopped_on_error(path, continue_prompt: str = "") -> Optional[dict]:
    """The resumable API-error stop a transcript ends on, or None.

    Returns ``{"ts", "error", "text", "failures"}``.  ``failures`` counts the
    consecutive stops of the same kind at the end of the transcript, looking
    past our own ``continue_prompt`` resends, so a repeatedly failing probe
    backs off.
    """
    entries = _tail_entries(path)
    if not entries:
        return None
    last = entries[-1]
    if last.get("type") != "assistant" or not last.get("isApiErrorMessage"):
        return None
    err = last.get("error") or ""
    if err not in RESUMABLE_ERRORS:
        return None
    ts = _parse_ts(last.get("timestamp"))
    if ts is None:
        return None
    failures = 0
    for entry in reversed(entries):
        if entry.get("type") == "assistant":
            if entry.get("isApiErrorMessage") and (entry.get("error") or "") == err:
                failures += 1
                continue
            break  # a real reply: the streak ends
        if _is_tool_result(entry):
            break
        if continue_prompt and _text(entry).strip() == continue_prompt.strip():
            continue  # our own resume attempt
        break  # the user (or a notification) said something new
    return {"ts": ts, "error": err, "text": _text(last), "failures": max(1, failures)}


def due_at(stop: dict, parse_usage_limit) -> float:
    """When Layer 2 should resume a session that stopped on ``stop``.

    ``parse_usage_limit`` is ``SessionManager._parse_usage_limit``, passed in so
    both layers read reset times identically.
    """
    if stop["error"] == "server_error":
        return stop["ts"] + SERVER_ERROR_DELAY_SECONDS
    try:
        _is_limit, reset = parse_usage_limit(stop["text"], now=stop["ts"])
    except Exception:
        reset = 0.0
    probe = min(PROBE_CAP_SECONDS, PROBE_BASE_SECONDS * (2 ** (stop["failures"] - 1)))
    if reset and reset > stop["ts"] - 3600:
        due = reset + LIMIT_GRACE_SECONDS
        if due > stop["ts"]:
            return due
        # This stop was written AFTER its own stated reset (plus grace): the
        # displayed time is stale or rounded (we already resumed at it and the
        # limit was still there).  Back off like an unknown reset, rather
        # than re-resuming every sweep against a wall.
    return stop["ts"] + probe
