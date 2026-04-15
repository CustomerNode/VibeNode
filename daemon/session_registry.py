"""
SessionRegistry -- persistent session registry for crash recovery.

Extracted from SessionManager (Phase 3 OOP decomposition).
"""

import json
import logging
import os
import tempfile
import threading
import time
from pathlib import Path
from typing import Callable, Optional

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Registry file for crash recovery
# ---------------------------------------------------------------------------
REGISTRY_PATH = Path.home() / ".claude" / "gui_active_sessions.json"

# Maximum age (seconds) for a session to be eligible for recovery
MAX_RECOVERY_AGE = 3600  # 1 hour


class SessionRegistry:
    """Persistent session registry for crash recovery."""

    def __init__(self):
        """Initialize the SessionRegistry.

        The registry uses a debounced save pattern: state changes mark
        the registry dirty and schedule a timer.  When the timer fires,
        it snapshots the current state and writes it atomically.  This
        avoids hammering disk on every state transition.

        No arguments -- the registry path is module-level (REGISTRY_PATH)
        to keep the class stateless with respect to configuration.
        """
        self._registry_timer: Optional[threading.Timer] = None
        self._registry_dirty = False

    # ------------------------------------------------------------------
    # Registry persistence
    # ------------------------------------------------------------------

    def load_registry(self) -> dict:
        """Read the session registry from disk. Returns empty dict on error."""
        try:
            if REGISTRY_PATH.exists():
                data = json.loads(REGISTRY_PATH.read_text(encoding="utf-8"))
                if isinstance(data, dict) and "sessions" in data:
                    return data
        except Exception as e:
            logger.warning("Failed to load session registry: %s", e)
        return {"sessions": {}}

    def save_registry_now(self, sessions_snapshot: dict) -> None:
        """Write the session state to the registry file atomically.

        sessions_snapshot is a dict of {sid: {name, cwd, model, ...}} already
        prepared by SessionManager (under its lock). This avoids the registry
        module needing to know about SessionInfo or SessionState.

        Snapshot format on disk:
            {
              "sessions": {
                "<session_id>": {
                  "name": str,       # user-visible session name
                  "state": str,      # "working", "waiting", "idle", etc.
                  "cwd": str,        # working directory path
                  "model": str,      # model identifier
                  "last_activity": float,  # time.time() of last state change
                  "session_type": str,     # "normal" or "planner"
                },
                ...
              }
            }
        """
        try:
            REGISTRY_PATH.parent.mkdir(parents=True, exist_ok=True)
            registry = {"sessions": sessions_snapshot}
            payload = json.dumps(registry, indent=2, ensure_ascii=False)

            # Atomic write pattern: write to a temp file in the same
            # directory, then os.replace() to atomically swap.  This
            # ensures that a crash mid-write never leaves a half-written
            # registry file that would prevent recovery of ALL sessions.
            tmp_fd, tmp_path = tempfile.mkstemp(
                dir=str(REGISTRY_PATH.parent), suffix=".tmp"
            )
            try:
                os.write(tmp_fd, payload.encode("utf-8"))
                os.close(tmp_fd)
                # os.replace is used instead of os.rename because on Windows,
                # os.rename fails if the destination already exists.
                os.replace(tmp_path, str(REGISTRY_PATH))
            except Exception:
                try:
                    os.close(tmp_fd)
                except OSError:
                    pass
                try:
                    os.unlink(tmp_path)
                except OSError:
                    pass
                raise
        except Exception as e:
            logger.warning("Failed to save session registry: %s", e)

    def schedule_registry_save(self, save_fn: Callable[[], None]) -> None:
        """Debounced save -- batches writes so we don't hit disk on every event.

        If a timer is already pending, skip (the pending save will capture
        the latest state).  Otherwise set a 3-second timer.

        save_fn is the callback that prepares the snapshot and calls
        save_registry_now(). This avoids the registry needing to know
        about SessionManager internals.
        """
        # Debounce pattern: if a timer is already running, skip this call.
        # The already-scheduled timer will capture the latest state when it
        # fires (since save_fn reads state at execution time, not at schedule
        # time).  This collapses N rapid state changes into a single disk
        # write, preventing I/O contention during burst activity.
        if self._registry_timer and self._registry_timer.is_alive():
            return
        self._registry_timer = threading.Timer(3.0, save_fn)
        self._registry_timer.daemon = True
        self._registry_timer.start()

    def recover_sessions(self, start_session_fn: Callable, store, max_age: int = None) -> None:
        """Recover sessions that were active before a crash.

        Called once at startup in a background thread. Reads the registry,
        filters out stale or stopped entries, and resumes each one via the
        SDK's --resume flag.

        start_session_fn: callback to SessionManager.start_session
        store: ChatStore instance for finding/repairing session files
        max_age: override MAX_RECOVERY_AGE for testing
        """
        if max_age is None:
            max_age = MAX_RECOVERY_AGE
        try:
            registry = self.load_registry()
            sessions = registry.get("sessions", {})
            if not sessions:
                logger.debug("No sessions to recover from registry")
                return

            now = time.time()
            recovered = 0
            for sid, meta in sessions.items():
                state = meta.get("state", "stopped")
                # Only recover sessions that were mid-task (working/waiting).
                # Idle sessions were done — no need to resume them.
                if state not in ("working", "waiting", "starting"):
                    continue

                # Never recover planner sessions
                if meta.get("session_type") == "planner":
                    continue

                last_activity = meta.get("last_activity", 0)
                age = now - last_activity
                if age > max_age:
                    logger.info(
                        "Skipping stale session %s (%.0f min old)", sid, age / 60
                    )
                    continue

                name = meta.get("name", "")
                cwd = meta.get("cwd", "")
                if cwd:
                    cwd = os.path.normpath(cwd)
                model = meta.get("model", "")

                # Guard: if the .jsonl file was deleted (user chose to delete
                # the session), do NOT recover it — that would undo the delete.
                jsonl_path = store.find_session_path(sid, cwd=cwd)
                if not jsonl_path:
                    logger.info(
                        "Skipping recovery of %s — .jsonl file was deleted", sid
                    )
                    continue

                # Repair incomplete assistant turns so --resume doesn't choke.
                # If the daemon was killed mid-response, the last .jsonl entry
                # is an assistant message with stop_reason=null — the CLI can't
                # resume from that state and the stream dies immediately.
                store.repair_incomplete_turn(sid, cwd=cwd)

                logger.info(
                    "Recovering session %s (%s) from registry", sid, name or "unnamed"
                )

                # Use start_session with resume=True to reconnect via SDK --resume
                result = start_session_fn(
                    session_id=sid,
                    prompt="",       # no new prompt; just reconnect
                    cwd=cwd,
                    name=name,
                    resume=True,
                    model=model if model else None,
                )
                if result.get("ok"):
                    recovered += 1
                else:
                    logger.warning(
                        "Failed to recover session %s: %s",
                        sid, result.get("error", "unknown")
                    )

            if recovered:
                logger.info("Recovered %d session(s) from crash registry", recovered)

            # Clear the registry now that recovery is done; ongoing state
            # changes will re-populate it via _schedule_registry_save()
            # (Don't clear -- let the normal emit_state cycle keep it updated)

        except Exception as e:
            logger.exception("Session recovery failed: %s", e)

    def cancel_timer(self) -> None:
        """Cancel any pending debounced save timer."""
        if self._registry_timer:
            self._registry_timer.cancel()
            self._registry_timer = None
