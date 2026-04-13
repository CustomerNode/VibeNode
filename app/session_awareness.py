"""
Cross-session awareness for system prompt injection.

Builds a lightweight context block (~300-400 tokens) listing all other
active sessions in the same project, their status, duration, and recently
edited files.  Injected into every session's system prompt at start so
Claude knows what else is running and can avoid file conflicts.

Gated by the ``cross_session_awareness`` preference in kanban_config.json.
"""

import os
import threading
import time

from .config import cwd_matches_active_project, _encode_cwd

# ---------------------------------------------------------------------------
# Caps
# ---------------------------------------------------------------------------

_MAX_SESSIONS = 12
_MAX_FILES_PER_SESSION = 3

# PERF-CRITICAL: get_all_states() cache with 2s TTL — do NOT remove or bypass. See CLAUDE.md #10.
#
# LESSON LEARNED (2026-04-12): build_cross_session_context() is called on every
# session start.  It calls daemon_client.get_all_states() which is a blocking
# TCP IPC round-trip — the Flask-SocketIO worker thread is blocked via
# event.wait(timeout=30) until the daemon responds.  With 5-10 concurrent
# sessions starting, each triggers its own round-trip, serializing all session
# state each time (20-100ms per call under load).  The 2-second cache eliminates
# repeated IPC during burst session starts.  Staleness is negligible — this data
# is advisory (injected into system prompts once at session creation) and sessions
# run for minutes/hours.  The IPC call runs OUTSIDE the lock so cache-hit threads
# are never blocked by a concurrent cache-miss thread doing the actual IPC.
# ---------------------------------------------------------------------------
# get_all_states() cache — avoids blocking IPC round-trip on every call
# ---------------------------------------------------------------------------

_states_cache: list | None = None
_states_cache_time: float = 0.0
_states_cache_lock = threading.Lock()
_STATES_CACHE_TTL = 2.0  # seconds


def _get_all_states_cached(daemon_client) -> list:
    """Return cached result of daemon_client.get_all_states().

    The cache has a 2-second TTL.  Cross-session awareness is advisory
    data injected once at session creation, so brief staleness is fine.
    """
    global _states_cache, _states_cache_time
    now = time.monotonic()
    with _states_cache_lock:
        if _states_cache is not None and (now - _states_cache_time) < _STATES_CACHE_TTL:
            return _states_cache
    # Cache miss — do the IPC call outside the lock to avoid blocking
    # other threads that could serve a cache hit.
    result = daemon_client.get_all_states()
    with _states_cache_lock:
        _states_cache = result
        _states_cache_time = time.monotonic()
    return result

# ---------------------------------------------------------------------------
# Template
# ---------------------------------------------------------------------------

_CROSS_SESSION_TEMPLATE = """## Other Active Sessions in This Project

The following sessions are currently active in the same project as you:

{session_lines}

### Multi-Session Conflict Guidance

Multiple sessions are working in this project simultaneously.  Follow
these rules:

1. **Before editing a file that another session has recently touched**
   (listed above), re-read it first so you are working from the latest
   version on disk.

2. **If you hit unexpected errors** — import failures, missing functions,
   changed signatures — and you can see above that another session is
   actively editing related files, **do not fight it.**  The other session
   is likely mid-refactor.  Back off from that file and move on to other
   parts of your task.

3. **If you cannot complete part of your task** because it conflicts with
   another active session's work, that is OK.  Complete everything else,
   then clearly surface the incomplete part to the user in your final
   response:

   **\u26a0 Not completed \u2014 potential conflict with another active session:**
   Describe what you could not do and which file(s) were affected so the
   user can retry after the other session finishes.

4. **Keep edits surgical** on any file another session may also be
   touching.  Small, targeted changes are far less likely to collide than
   large rewrites.

5. **Do not attempt to coordinate with or send messages to other sessions.**
   Just be aware of them, work around conflicts when needed, and tell the
   user what you skipped."""


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _format_duration(created_ts: float) -> str:
    """Return a human-readable duration since *created_ts*."""
    if not created_ts:
        return "unknown"
    try:
        delta = time.time() - created_ts
        minutes = int(delta / 60)
        if minutes < 1:
            return "<1m"
        if minutes < 60:
            return f"{minutes}m"
        hours = minutes // 60
        remaining = minutes % 60
        if remaining:
            return f"{hours}h{remaining}m"
        return f"{hours}h"
    except Exception:
        return "unknown"


def _basenames(file_paths: list, limit: int = _MAX_FILES_PER_SESSION) -> str:
    """Return a comma-separated list of unique basenames from *file_paths*."""
    if not file_paths:
        return "(no file edits yet)"
    seen = []
    for fp in reversed(file_paths):  # most recent last in the list
        name = os.path.basename(fp)
        if name and name not in seen:
            seen.append(name)
        if len(seen) >= limit:
            break
    return ", ".join(seen) if seen else "(no file edits yet)"


# ---------------------------------------------------------------------------
# Builder
# ---------------------------------------------------------------------------

def build_cross_session_context(
    daemon_client,
    project: str,
    current_session_id: str,
) -> str | None:
    """Build a cross-session awareness block for the system prompt.

    Returns ``None`` when there are no other active sessions in the
    project (avoids injecting empty noise).

    Args:
        daemon_client: Object with a ``get_all_states()`` method that
            returns a list of session state dicts (the daemon's
            ``SessionManager`` or the web server's ``DaemonClient``).
        project: Encoded project string (from ``_encode_cwd``).
        current_session_id: Session ID to exclude from the listing.

    Returns:
        Formatted context string ready for system prompt injection, or
        ``None`` if there are no other qualifying sessions.
    """
    try:
        all_states = _get_all_states_cached(daemon_client)
    except Exception:
        return None

    if not all_states:
        return None

    # Active states we care about
    active_states = {"working", "idle", "waiting"}

    lines = []
    for state in all_states:
        sid = state.get("session_id", "")

        # Skip self
        if sid == current_session_id:
            continue

        # Skip utility sessions (planner, title generation, etc.)
        stype = state.get("session_type", "")
        if stype in ("planner", "title"):
            continue

        # Skip non-active sessions
        if state.get("state", "") not in active_states:
            continue

        # Skip sessions from other projects
        session_cwd = state.get("cwd", "")
        if session_cwd and not cwd_matches_active_project(session_cwd, project=project):
            continue

        # Build the line
        name = state.get("name", "").strip()
        display = f'"{name}"' if name else "(unnamed)"
        status = state.get("state", "unknown")
        duration = _format_duration(state.get("created_ts", 0))
        files = _basenames(state.get("tracked_files", []))

        lines.append(f"- {display} [{status}, {duration}] \u2014 recent files: {files}")

        if len(lines) >= _MAX_SESSIONS:
            break

    if not lines:
        return None

    session_lines = "\n".join(lines)

    # Check how many were truncated
    total_active = len(lines)
    if total_active >= _MAX_SESSIONS:
        remaining = sum(
            1 for s in all_states
            if s.get("session_id") != current_session_id
            and s.get("session_type", "") not in ("planner", "title")
            and s.get("state", "") in active_states
        ) - _MAX_SESSIONS
        if remaining > 0:
            session_lines += f"\n(+{remaining} more)"

    return _CROSS_SESSION_TEMPLATE.format(session_lines=session_lines)
