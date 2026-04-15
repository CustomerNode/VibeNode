"""
MessageQueue -- server-side per-session FIFO message queue with persistence.

Extracted from SessionManager (Phase 3 OOP decomposition).
"""

import json
import logging
import threading
from pathlib import Path
from typing import Callable, Optional

logger = logging.getLogger(__name__)


class MessageQueue:
    """Server-side per-session FIFO message queue with persistence."""

    def __init__(self, push_callback=None):
        self._queues: dict[str, list[str]] = {}
        self._queue_lock = threading.Lock()
        self._queue_path = Path.home() / ".claude" / "gui_message_queues.json"
        self._queue_save_timer: Optional[threading.Timer] = None
        self._push_callback = push_callback
        self._load_queues()

    # ------------------------------------------------------------------
    # Persistence
    # ------------------------------------------------------------------

    def _load_queues(self) -> None:
        """Load persisted queues from disk."""
        try:
            if self._queue_path.exists():
                raw = json.loads(self._queue_path.read_text(encoding="utf-8"))
                if isinstance(raw, dict):
                    for k, v in raw.items():
                        if isinstance(v, list) and all(isinstance(x, str) for x in v):
                            self._queues[k] = v
        except Exception as e:
            logger.warning("Failed to load queues: %s", e)

    def _save_queues_now(self) -> None:
        """Persist queues to disk immediately."""
        try:
            self._queue_path.parent.mkdir(parents=True, exist_ok=True)
            with self._queue_lock:
                data = {k: v for k, v in self._queues.items() if v}
            self._queue_path.write_text(json.dumps(data), encoding="utf-8")
        except Exception as e:
            logger.debug("Failed to save queues: %s", e)

    # PERF-CRITICAL: Debounced 1s timer batches disk writes — do NOT call _save_queues_now() directly. See CLAUDE.md #8.
    #
    # LESSON LEARNED (2026-04-12): _save_queues was originally synchronous —
    # every queue_message, remove_queue_item, edit_queue_item, clear_queue,
    # and auto-dispatch called it, each writing the ENTIRE queue state to disk.
    # Under rapid-fire operations (auto-dispatch chains processing multiple
    # queued messages), this created I/O contention.  The debounce timer
    # collapses N writes within 1 second into a single disk write.  The
    # shutdown path (stop()) cancels the timer and calls _save_queues_now()
    # directly to ensure no data loss on clean exit.
    def save_queues(self) -> None:
        """Debounced queue save -- batches writes so rapid-fire queue
        operations don't hammer the disk.

        Uses the same pattern as _schedule_registry_save(): if a timer
        is already pending, skip (the pending save will capture the
        latest state).  Otherwise set a 1-second timer.
        """
        if self._queue_save_timer and self._queue_save_timer.is_alive():
            # A save is already scheduled; it will pick up the newest state
            return
        self._queue_save_timer = threading.Timer(1.0, self._save_queues_now)
        self._queue_save_timer.daemon = True
        self._queue_save_timer.start()

    # ------------------------------------------------------------------
    # Push updates
    # ------------------------------------------------------------------

    def set_push_callback(self, cb) -> None:
        """Set or update the push callback (e.g., after SessionManager.start)."""
        self._push_callback = cb

    def emit_queue_update(self, session_id: str) -> None:
        """Push queue state to connected clients."""
        with self._queue_lock:
            items = list(self._queues.get(session_id, []))
        if self._push_callback:
            self._push_callback('queue_updated', {
                'session_id': session_id,
                'queue': items,
            })

    # ------------------------------------------------------------------
    # Queue operations
    # ------------------------------------------------------------------

    def queue_message(self, session_id: str, text: str) -> dict:
        """Add a message to a session's queue."""
        with self._queue_lock:
            if session_id not in self._queues:
                self._queues[session_id] = []
            self._queues[session_id].append(text)
        self.save_queues()
        self.emit_queue_update(session_id)
        logger.info("Queued message for %s (%d in queue)", session_id,
                     len(self._queues.get(session_id, [])))
        return {"ok": True, "queued": True}

    def get_queue(self, session_id: str) -> list:
        """Return the queue for a session."""
        with self._queue_lock:
            return list(self._queues.get(session_id, []))

    def remove_queue_item(self, session_id: str, index: int) -> dict:
        """Remove one item from a session's queue by index."""
        with self._queue_lock:
            q = self._queues.get(session_id, [])
            if 0 <= index < len(q):
                q.pop(index)
                if not q:
                    self._queues.pop(session_id, None)
            else:
                return {"ok": False, "error": "Index out of range"}
        self.save_queues()
        self.emit_queue_update(session_id)
        return {"ok": True}

    def edit_queue_item(self, session_id: str, index: int, text: str) -> dict:
        """Edit one item in a session's queue by index."""
        with self._queue_lock:
            q = self._queues.get(session_id, [])
            if 0 <= index < len(q):
                q[index] = text
            else:
                return {"ok": False, "error": "Index out of range"}
        self.save_queues()
        self.emit_queue_update(session_id)
        return {"ok": True}

    def clear_queue(self, session_id: str) -> dict:
        """Clear all queued messages for a session."""
        with self._queue_lock:
            self._queues.pop(session_id, None)
        self.save_queues()
        self.emit_queue_update(session_id)
        return {"ok": True}

    def try_dispatch_queue(self, session_id: str,
                           send_fn: Callable[[str, str], dict]) -> None:
        """If session has queued items, dispatch the first one.

        Called from _emit_state on IDLE transitions. send_fn is typically
        SessionManager.send_message. This avoids a circular dependency:
        MessageQueue doesn't know about SessionManager, it just calls
        the function it's given.
        """
        with self._queue_lock:
            q = self._queues.get(session_id, [])
            if not q:
                return
            text = q.pop(0)
            remaining = len(q)
            if not q:
                self._queues.pop(session_id, None)

        self.save_queues()
        self.emit_queue_update(session_id)

        logger.info("Auto-dispatching queued message for %s (%d remaining)",
                     session_id, remaining)

        # Notify frontend that a queued message is being sent
        if self._push_callback:
            self._push_callback('queue_dispatched', {
                'session_id': session_id,
                'text': text,
                'remaining': remaining,
            })

        # send_message checks state==IDLE and sets WORKING atomically
        result = send_fn(session_id, text)
        if not result.get("ok") and not result.get("queued"):
            # Send failed (race condition) — re-queue at front
            logger.warning("Queue dispatch failed for %s: %s — re-queuing",
                          session_id, result.get("error"))
            with self._queue_lock:
                if session_id not in self._queues:
                    self._queues[session_id] = []
                self._queues[session_id].insert(0, text)
            self.save_queues()
            self.emit_queue_update(session_id)

    # ------------------------------------------------------------------
    # Direct access (for SessionManager internals that read _queues)
    # ------------------------------------------------------------------

    def get_queue_data(self, session_id: str) -> list:
        """Return raw queue list for a session (for inclusion in state dicts)."""
        with self._queue_lock:
            return list(self._queues.get(session_id, []))

    def remap_session_id(self, old_id: str, new_id: str) -> None:
        """Remap queue from old session ID to new session ID (SDK session remap)."""
        with self._queue_lock:
            if old_id in self._queues:
                self._queues[new_id] = self._queues.pop(old_id)
        self.save_queues()

    def pop_queue(self, session_id: str) -> None:
        """Remove queue for a session (used during interrupt)."""
        with self._queue_lock:
            removed = self._queues.pop(session_id, None)
        if removed is not None:
            self.save_queues()
            self.emit_queue_update(session_id)

    def cancel_timer(self) -> None:
        """Cancel any pending debounced save timer."""
        if self._queue_save_timer:
            self._queue_save_timer.cancel()
            self._queue_save_timer = None

    def flush(self) -> None:
        """Cancel timer and save immediately (for clean shutdown)."""
        self.cancel_timer()
        self._save_queues_now()
