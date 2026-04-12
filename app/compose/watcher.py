"""
Compose file watcher — monitors compose-context.json files for changes
and emits SocketIO events for real-time board updates.

Uses a background thread that polls for file modifications (no external
dependency like watchdog). Follows the same threading pattern as
daemon_client.py.
"""

import json
import logging
import threading
import time
from pathlib import Path

logger = logging.getLogger(__name__)

_POLL_INTERVAL = 4.0  # seconds between polls
_watcher_thread = None
_stop_event = threading.Event()


def start_compose_watcher(socketio, app):
    """Start the background compose-context.json watcher.

    Monitors all compose-projects/*/compose-context.json files for changes
    and emits compose_context_updated events when modifications are detected.
    """
    global _watcher_thread

    if _watcher_thread is not None and _watcher_thread.is_alive():
        return  # Already running

    _stop_event.clear()

    def _watch():
        from .models import COMPOSE_PROJECTS_DIR

        # Track last-modified times: {file_path_str: mtime}
        last_mtimes = {}

        while not _stop_event.is_set():
            try:
                base = COMPOSE_PROJECTS_DIR
                if not base.is_dir():
                    _stop_event.wait(_POLL_INTERVAL)
                    continue

                for project_dir in base.iterdir():
                    if not project_dir.is_dir():
                        continue

                    ctx_file = project_dir / "compose-context.json"
                    if not ctx_file.is_file():
                        continue

                    key = str(ctx_file)
                    try:
                        mtime = ctx_file.stat().st_mtime
                    except OSError:
                        continue

                    if key in last_mtimes and mtime != last_mtimes[key]:
                        # File changed — emit event
                        try:
                            try:
                                ctx = json.loads(ctx_file.read_text(encoding="utf-8"))
                            except (json.JSONDecodeError, OSError):
                                # File mid-write — skip this cycle, retry next poll
                                last_mtimes[key] = mtime
                                continue
                            project_id = ctx.get("project_id", "")

                            with app.app_context():
                                socketio.emit('compose_context_updated', {
                                    'project_id': project_id,
                                    'context': ctx,
                                })

                                # Check for changing flag changes
                                for section in ctx.get("sections", []):
                                    if section.get("changing"):
                                        socketio.emit('compose_changing', {
                                            'project_id': project_id,
                                            'section_id': section.get("id"),
                                            'changing': True,
                                            'change_note': section.get("change_note"),
                                        })

                            logger.debug(
                                "Compose context updated for project %s",
                                project_id,
                            )
                        except Exception:
                            logger.exception(
                                "Error reading changed context file %s", key
                            )

                    last_mtimes[key] = mtime

            except Exception:
                logger.exception("Error in compose watcher loop")

            _stop_event.wait(_POLL_INTERVAL)

    _watcher_thread = threading.Thread(
        target=_watch, daemon=True, name="compose-watcher"
    )
    _watcher_thread.start()
    logger.info("Compose watcher started (poll interval: %.1fs)", _POLL_INTERVAL)


def stop_compose_watcher():
    """Stop the background watcher thread."""
    _stop_event.set()
    if _watcher_thread is not None:
        _watcher_thread.join(timeout=5.0)
