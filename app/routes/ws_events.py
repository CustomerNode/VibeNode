"""
WebSocket event handlers for real-time session communication via Flask-SocketIO.

Server -> Client events:
    state_snapshot, session_state, session_entry, session_permission,
    session_started, session_log

Client -> Server events:
    connect, start_session, send_message, permission_response,
    interrupt_session, close_session, get_session_log, set_permission_policy
"""

import json
import logging
import re
from concurrent.futures import ThreadPoolExecutor

from flask import request as flask_request
from flask_socketio import emit

logger = logging.getLogger(__name__)

# Module-level thread pool for parallelizing independent setup work
# (e.g. compose resolution + cross-session awareness).  A single
# shared executor avoids creating/destroying threads on every session start.
_setup_executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix="ws-setup")

# Markers that indicate a UserMessage is SDK/CLI system content, not human input
_SYSTEM_USER_MARKERS = (
    "This session is being continued from a previous conversation",
    "<system-reminder>",
    "<local-command-stdout>",
    "<local-command-caveat>",
    "<command-name>",
    "<command-message>",
    "<command-args>",
)

def _is_system_user_content(text: str) -> bool:
    for marker in _SYSTEM_USER_MARKERS:
        if marker in text:
            return True
    return False

def _system_user_label(text: str) -> str:
    if "This session is being continued from a previous conversation" in text:
        return "Session continued from previous conversation"
    m = re.search(r'<command-name>(/?\w+)</command-name>', text)
    if m:
        cmd = m.group(1)
        m2 = re.search(r'<local-command-stdout>(.*?)</local-command-stdout>', text, re.DOTALL)
        stdout = m2.group(1).strip() if m2 else ""
        return f"{cmd}: {stdout[:100]}" if stdout else f"Local command: {cmd}"
    m = re.search(r'<local-command-stdout>(.*?)</local-command-stdout>', text, re.DOTALL)
    if m:
        return f"Command output: {m.group(1).strip()[:100]}"
    return "System message"


def _parse_jsonl_entries(app, session_id: str, since: int = 0, project: str = "") -> list:
    """Parse .jsonl file on disk to produce structured log entries.

    Reuses the same logic as live_api.py's api_session_log endpoint so that
    historical sessions display correctly in the live panel.
    """
    from ..config import _sessions_dir

    path = _sessions_dir(project) / f"{session_id}.jsonl"
    if not path.exists():
        return []
    try:
        raw_lines = [l for l in path.read_text(encoding="utf-8").splitlines() if l.strip()]
    except Exception:
        return []

    entries = []
    for raw in raw_lines[since:]:
        try:
            obj = json.loads(raw)
        except Exception:
            continue
        t = obj.get("type", "")
        if t in ("file-history-snapshot", "custom-title", "progress"):
            continue
        if t == "user":
            msg = obj.get("message", {})
            content = msg.get("content", "")
            if isinstance(content, str) and content.strip():
                text = content.strip()[:20000]
                if _is_system_user_content(text):
                    entries.append({"kind": "system", "text": _system_user_label(text)})
                else:
                    entries.append({"kind": "user", "text": text})
            elif isinstance(content, list):
                for block in content:
                    bt = block.get("type", "")
                    if bt == "text" and block.get("text", "").strip():
                        text = block["text"].strip()[:20000]
                        if _is_system_user_content(text):
                            entries.append({"kind": "system", "text": _system_user_label(text)})
                        else:
                            entries.append({"kind": "user", "text": text})
                    elif bt == "tool_result":
                        rc = block.get("content", "")
                        if isinstance(rc, list):
                            rt = " ".join(
                                b.get("text", "") for b in rc
                                if isinstance(b, dict) and b.get("type") == "text"
                            )
                        else:
                            rt = str(rc)
                        entries.append({
                            "kind": "tool_result",
                            "tool_use_id": block.get("tool_use_id", ""),
                            "text": rt[:20000],
                            "is_error": bool(block.get("is_error")),
                        })
        elif t == "assistant":
            msg = obj.get("message", {})
            content = msg.get("content", "")
            if isinstance(content, str) and content.strip():
                entries.append({"kind": "asst", "text": content.strip()[:50000]})
            elif isinstance(content, list):
                for block in content:
                    bt = block.get("type", "")
                    if bt == "text" and block.get("text", "").strip():
                        entries.append({"kind": "asst", "text": block["text"].strip()[:50000]})
                    elif bt == "tool_use":
                        inp = block.get("input") or {}
                        if "command" in inp:
                            desc = inp["command"][:300]
                        elif "path" in inp:
                            desc = inp["path"]
                            if "content" in inp:
                                desc += f" (write {len(str(inp.get('content', '')))} chars)"
                        elif "pattern" in inp:
                            desc = inp["pattern"][:200]
                        elif inp:
                            first_key = next(iter(inp))
                            desc = f"{first_key}: {str(inp[first_key])[:200]}"
                        else:
                            desc = ""
                        entries.append({
                            "kind": "tool_use",
                            "id": block.get("id", ""),
                            "name": block.get("name", ""),
                            "desc": desc,
                        })
    return entries


def register_ws_events(socketio, app):
    """Register all WebSocket event handlers with the SocketIO instance."""

    def _filter_sessions_for_project(sessions: list, project: str = "") -> list:
        """Return only sessions whose cwd matches the active project."""
        from ..config import cwd_matches_active_project
        return [s for s in sessions
                if not s.get("cwd") or cwd_matches_active_project(s["cwd"], project=project)]

    @socketio.on('connect')
    def handle_connect():
        """On connect, send a full state snapshot immediately.

        Uses the project from the SocketIO query param (set from localStorage
        at page load) so sessions are filtered correctly.  The client also
        sends request_state_snapshot right after connect as a backup, but
        this initial snapshot ensures running sessions show their true state
        without any delay.
        """
        from flask import request as flask_request
        project = ""
        try:
            project = (flask_request.args.get("project") or "").strip()
        except Exception:
            pass
        sm = app.session_manager
        sessions = _filter_sessions_for_project(sm.get_all_states(), project=project)
        queues = {}
        for s in sessions:
            q = s.get('queue')
            if q:
                queues[s['session_id']] = q
        aliases = dict(sm._id_aliases) if hasattr(sm, '_id_aliases') else {}
        emit('state_snapshot', {'sessions': sessions, 'queues': queues, 'aliases': aliases})
        logger.debug("WebSocket client connected, sent %d session states", len(sessions))

    @socketio.on('disconnect')
    def handle_disconnect():
        logger.debug("WebSocket client disconnected")

    @socketio.on('request_state_snapshot')
    def handle_request_state_snapshot(data=None):
        """Re-send full state snapshot on demand (e.g. after workspace switch).

        Accepts optional ``{project: "encoded-name"}`` so the snapshot is
        filtered to the correct project without mutating _active_project.
        """
        project = ""
        if isinstance(data, dict):
            project = (data.get("project") or "").strip()
        # Sync the global _active_project as a safety net for endpoints
        # that don't receive an explicit project param (e.g. get_session_log).
        if project:
            from ..config import set_active_project, get_active_project, _CLAUDE_PROJECTS
            if get_active_project() != project and (_CLAUDE_PROJECTS / project).is_dir():
                set_active_project(project)
                logger.info("Synced active project from client: %s", project)
        sm = app.session_manager
        if hasattr(sm, "is_connected") and not sm.is_connected:
            emit("state_snapshot", {"sessions": [], "queues": {}, "aliases": {}})
            return
        sessions = _filter_sessions_for_project(sm.get_all_states(), project=project)
        queues = {}
        for s in sessions:
            q = s.get('queue')
            if q:
                queues[s['session_id']] = q
        aliases = dict(sm._id_aliases) if hasattr(sm, '_id_aliases') else {}
        emit('state_snapshot', {'sessions': sessions, 'queues': queues, 'aliases': aliases})

    @socketio.on('start_session')
    def handle_start_session(data):
        """Start a new or resumed SDK session."""
        if not isinstance(data, dict):
            emit('error', {'message': 'Invalid data'})
            return

        session_id = data.get('session_id', '').strip()
        prompt = data.get('prompt', '').strip()
        cwd = data.get('cwd', '').strip()
        name = data.get('name', '').strip()
        resume = bool(data.get('resume', False))

        # Optional session options
        model = (data.get('model') or '').strip() or None
        system_prompt = (data.get('system_prompt') or '').strip() or None
        thinking_level = (data.get('thinking_level') or '').strip() or None
        max_turns = data.get('max_turns')
        if max_turns is not None:
            try:
                max_turns = int(max_turns)
            except (ValueError, TypeError):
                max_turns = None
        allowed_tools = data.get('allowed_tools')
        if not isinstance(allowed_tools, list):
            allowed_tools = None
        permission_mode = (data.get('permission_mode') or '').strip() or None
        if permission_mode and permission_mode not in ('default', 'plan', 'acceptEdits', 'bypassPermissions'):
            permission_mode = None
        session_type = (data.get('session_type') or '').strip() or ""

        # --- Compose task detection + cross-session awareness ----
        # These two operations are independent and can run in parallel
        # when both are needed.  Each modifies system_prompt, so we
        # collect results and apply them sequentially after both finish.
        compose_task_id = (data.get('compose_task_id') or '').strip() or None

        # Route utility sessions to a separate project so their JSONL files
        # never appear in the user's project.
        if session_type in ('planner', 'title'):
            from pathlib import Path as _Path
            from ..config import _SYSTEM_UTILITY_CWD
            _Path(_SYSTEM_UTILITY_CWD).mkdir(parents=True, exist_ok=True)
            cwd = _SYSTEM_UTILITY_CWD

        if not session_id:
            emit('error', {'message': 'session_id is required'})
            return

        # Check if cross-session awareness is enabled (for non-utility sessions)
        want_awareness = False
        if session_type not in ('planner', 'title'):
            try:
                from ..config import get_kanban_config
                want_awareness = get_kanban_config().get("cross_session_awareness", True)
            except Exception:
                pass

        # -- Helper functions for parallel execution --
        def _resolve_compose():
            """Resolve compose system prompt.  Returns prompt string or None."""
            try:
                from .compose_api import resolve_compose_system_prompt
                cp_result = resolve_compose_system_prompt(compose_task_id)
                if cp_result.get('ok') and cp_result.get('system_prompt'):
                    logger.info(
                        "Injected compose system prompt for task %s "
                        "(role=%s) into session %s",
                        compose_task_id, cp_result.get('agent_role', '?'),
                        session_id,
                    )
                    return cp_result['system_prompt']
                else:
                    logger.warning(
                        "Could not resolve compose prompt for task %s: %s",
                        compose_task_id, cp_result.get('error', 'unknown'),
                    )
            except Exception:
                logger.exception(
                    "Error resolving compose prompt for task %s", compose_task_id
                )
            return None

        def _resolve_awareness():
            """Build cross-session awareness context.  Returns context string or None."""
            try:
                from ..config import _encode_cwd
                from ..session_awareness import build_cross_session_context
                return build_cross_session_context(
                    daemon_client=app.session_manager,
                    project=_encode_cwd(cwd),
                    current_session_id=session_id,
                )
            except Exception:
                logger.debug("Cross-session awareness injection failed", exc_info=True)
            return None

        compose_prompt = None
        cross_ctx = None

        if compose_task_id and want_awareness:
            # Both needed — run in parallel
            compose_future = _setup_executor.submit(_resolve_compose)
            awareness_future = _setup_executor.submit(_resolve_awareness)
            try:
                compose_prompt = compose_future.result(timeout=10)
            except Exception:
                logger.debug("Compose resolution timed out or failed", exc_info=True)
            try:
                cross_ctx = awareness_future.result(timeout=10)
            except Exception:
                logger.debug("Awareness resolution timed out or failed", exc_info=True)
        elif compose_task_id:
            compose_prompt = _resolve_compose()
        elif want_awareness:
            cross_ctx = _resolve_awareness()

        # Apply results to system_prompt (order: compose first, awareness second)
        if compose_prompt:
            system_prompt = (
                system_prompt + '\n\n' + compose_prompt
                if system_prompt else compose_prompt
            )
        if cross_ctx:
            system_prompt = (
                system_prompt + '\n\n' + cross_ctx
                if system_prompt else cross_ctx
            )

        # Tag initial prompt with metadata (timestamp + voice indicator)
        voice = bool(data.get('voice'))
        if prompt and session_type not in ('planner', 'title'):
            from datetime import datetime as _dt
            _ts_tag = '\n\nSent from Q at ' + _dt.now().strftime('%Y-%m-%d %I:%M %p')
            if voice:
                _ts_tag += ' (transcribed from voice \u2014 may contain transcription errors)'
            prompt = prompt + _ts_tag

        sm = app.session_manager
        result = sm.start_session(
            session_id=session_id,
            prompt=prompt,
            cwd=cwd,
            name=name,
            resume=resume,
            model=model,
            system_prompt=system_prompt,
            max_turns=max_turns,
            allowed_tools=allowed_tools,
            permission_mode=permission_mode,
            session_type=session_type,
        )

        if result.get('ok'):
            # Emit immediately so the client isn't blocked
            emit('session_started', {'session_id': session_id})
            # Link session to compose task after emitting (non-blocking)
            if compose_task_id:
                try:
                    from .compose_api import link_session_to_compose_task
                    link_session_to_compose_task(compose_task_id, session_id)
                except Exception:
                    logger.exception(
                        "Failed to auto-link session %s to compose task %s",
                        session_id, compose_task_id,
                    )
        else:
            emit('error', {
                'message': result.get('error', 'Failed to start session'),
                'session_id': session_id,
            })

    @socketio.on('send_message')
    def handle_send_message(data):
        """Send a follow-up message to an idle session."""
        if not isinstance(data, dict):
            emit('error', {'message': 'Invalid data'})
            return

        session_id = data.get('session_id', '').strip()
        text = data.get('text', '').strip()
        voice = bool(data.get('voice'))

        if not session_id:
            emit('error', {'message': 'session_id is required'})
            return
        if not text:
            emit('error', {'message': 'text is required'})
            return

        sm = app.session_manager
        result = sm.send_message(session_id, text, voice=voice)

        if result.get('ok'):
            # Acknowledge receipt so the client knows the message was accepted.
            emit('message_ack', {'session_id': session_id})
        elif result.get('queued'):
            # Daemon auto-queued because session wasn't idle
            emit('message_ack', {'session_id': session_id, 'queued': True})
        else:
            # Send failed (IPC timeout, daemon busy, etc).
            # Emit a send_failed event so the frontend can show the user
            # exactly what happened and preserve their message text inline.
            err = result.get('error', 'Unknown error')
            logger.warning("send_message failed for %s: %s", session_id, err)
            emit('send_failed', {
                'session_id': session_id,
                'error': err,
                'text': text,
            })

    @socketio.on('permission_response')
    def handle_permission_response(data):
        """Resolve a pending permission request."""
        if not isinstance(data, dict):
            emit('error', {'message': 'Invalid data'})
            return

        session_id = data.get('session_id', '').strip()
        action = data.get('action', '').strip().lower()

        if not session_id:
            emit('error', {'message': 'session_id is required'})
            return
        if action not in ('y', 'n', 'a', 'aa'):
            emit('error', {'message': 'action must be y, n, a, or aa'})
            return

        allow = action in ('y', 'a', 'aa')
        always = action == 'a'
        almost_always = action == 'aa'

        sm = app.session_manager

        # Daemon handles both hook and SDK permissions via resolve_permission
        result = sm.resolve_permission(session_id, allow=allow, always=always,
                                       almost_always=almost_always)
        if isinstance(result, dict) and not result.get('ok'):
            emit('error', {
                'message': result.get('error', 'Failed to resolve permission'),
                'session_id': session_id,
            })

    @socketio.on('interrupt_session')
    def handle_interrupt_session(data):
        """Interrupt a running session."""
        if not isinstance(data, dict):
            emit('error', {'message': 'Invalid data'})
            return

        session_id = data.get('session_id', '').strip()
        if not session_id:
            emit('error', {'message': 'session_id is required'})
            return

        sm = app.session_manager
        result = sm.interrupt_session(session_id)

        if not result.get('ok'):
            emit('error', {
                'message': result.get('error', 'Failed to interrupt session'),
                'session_id': session_id,
            })

    @socketio.on('close_session')
    def handle_close_session(data):
        """Close and disconnect a session."""
        if not isinstance(data, dict):
            emit('error', {'message': 'Invalid data'})
            return

        session_id = data.get('session_id', '').strip()
        if not session_id:
            emit('error', {'message': 'session_id is required'})
            return

        sm = app.session_manager
        result = sm.close_session(session_id)

        if not result.get('ok'):
            emit('error', {
                'message': result.get('error', 'Failed to close session'),
                'session_id': session_id,
            })

    @socketio.on('get_session_log')
    def handle_get_session_log(data):
        """Return accumulated log entries for a session.

        First checks the SDK SessionManager for live entries, then falls
        back to parsing the .jsonl file on disk for historical sessions.

        Supports pagination via ``limit`` and ``before`` parameters:
        - ``limit``: max entries to return (omit for all — backwards compat)
        - ``before``: return entries ending before this index (for "load older")
        Response includes ``total``, ``offset``, ``has_more``, and ``prepend``
        so the client can render a "Load older messages" button and prepend.
        """
        if not isinstance(data, dict):
            emit('error', {'message': 'Invalid data'})
            return

        session_id = data.get('session_id', '').strip()
        try:
            since = int(data.get('since', 0))
        except (ValueError, TypeError):
            since = 0

        # Pagination params
        limit = data.get('limit')  # None = all (backwards compat)
        before = data.get('before')  # Load entries before this index
        if limit is not None:
            try:
                limit = int(limit)
            except (ValueError, TypeError):
                limit = None
        if before is not None:
            try:
                before = int(before)
            except (ValueError, TypeError):
                before = None

        project = (data.get("project") or "").strip()

        if not session_id:
            emit('error', {'message': 'session_id is required'})
            return

        sm = app.session_manager

        # JSONL is the single source of truth — the SDK writes it and
        # it's complete for any idle session.
        #
        # For WORKING sessions, the SDK hasn't flushed the current turn
        # yet, so the JSONL is missing the latest entries. In that case
        # the daemon's in-memory entries (which include the current turn)
        # are more complete — use those instead.  No merge, no
        # fingerprinting: just pick the source that has more data.
        #
        # Optimization: skip daemon IPC (has_session + get_entries) when
        # the session has JSONL data and isn't actively streaming. The
        # client passes an `is_working` hint based on its sessionKinds
        # state. Each IPC call is a blocking TCP round-trip, so skipping
        # both saves ~2x the IPC latency on session switch/resume.
        is_working = bool(data.get('is_working', False))
        entries = _parse_jsonl_entries(app, session_id, since, project=project)

        if entries and not is_working:
            # JSONL has data and session isn't actively streaming —
            # disk is the complete source of truth, skip IPC entirely.
            pass
        elif sm.has_session(session_id):
            # Session is working (unflushed entries) or no JSONL yet —
            # daemon may have more data than disk.
            sdk_entries = sm.get_entries(session_id, since=0)
            if len(sdk_entries) > len(entries):
                entries = sdk_entries

        total = len(entries)

        if before is not None:
            # Loading older entries: return entries ending before `before` index
            end = min(before, total)
            start = max(0, end - (limit or 50))
            page = entries[start:end]
            emit('session_log', {
                'session_id': session_id,
                'entries': page,
                'total': total,
                'offset': start,
                'has_more': start > 0,
                'prepend': True,
            })
        elif limit and total > limit:
            # Initial load with pagination: return last `limit` entries
            start = total - limit
            page = entries[start:]
            emit('session_log', {
                'session_id': session_id,
                'entries': page,
                'total': total,
                'offset': start,
                'has_more': start > 0,
            })
        else:
            # No pagination needed (backwards compat or small log)
            emit('session_log', {
                'session_id': session_id,
                'entries': entries,
                'total': total,
                'offset': 0,
                'has_more': False,
            })

    @socketio.on('get_permission_policy')
    def handle_get_permission_policy():
        """Return the persisted permission policy to the browser."""
        sm = app.session_manager
        try:
            result = sm.get_permission_policy()
            emit('permission_policy_loaded', result)
        except Exception as e:
            logger.warning("Failed to get permission policy: %s", e)
            emit('permission_policy_loaded', {'policy': 'manual', 'custom_rules': {}})

    @socketio.on('set_permission_policy')
    def handle_set_permission_policy(data):
        """Sync permission policy from browser to server."""
        if not isinstance(data, dict):
            emit('error', {'message': 'Invalid data'})
            return

        policy = (data.get('policy') or '').strip()
        custom_rules = data.get('customRules') or {}

        if policy not in ('manual', 'auto', 'almost_always', 'custom'):
            emit('error', {'message': 'Invalid policy: must be manual, auto, almost_always, or custom'})
            return

        sm = app.session_manager
        sm.set_permission_policy(policy, custom_rules)
        logger.debug("Permission policy synced: %s", policy)

    @socketio.on('get_ui_prefs')
    def handle_get_ui_prefs():
        """Return persisted UI preferences to the browser."""
        sm = app.session_manager
        try:
            result = sm.get_ui_prefs()
            emit('ui_prefs_loaded', result)
        except Exception as e:
            logger.warning("Failed to get UI prefs: %s", e)
            emit('ui_prefs_loaded', {})

    @socketio.on('set_ui_prefs')
    def handle_set_ui_prefs(data):
        """Persist UI preferences from browser."""
        if not isinstance(data, dict):
            return
        sm = app.session_manager
        sm.set_ui_prefs(data)
        logger.debug("UI prefs synced: %s", list(data.keys()))

    # ------------------------------------------------------------------
    # Server-side message queue events
    # ------------------------------------------------------------------

    @socketio.on('queue_message')
    def handle_queue_message(data):
        """Add a message to a session's server-side queue."""
        if not isinstance(data, dict):
            emit('error', {'message': 'Invalid data'})
            return
        session_id = (data.get('session_id') or '').strip()
        text = (data.get('text') or '').strip()
        if not session_id or not text:
            emit('error', {'message': 'session_id and text are required'})
            return
        sm = app.session_manager
        result = sm.queue_message(session_id, text)
        if not result.get('ok'):
            emit('error', {'message': result.get('error', 'Failed to queue'), 'session_id': session_id})

    @socketio.on('remove_queue_item')
    def handle_remove_queue_item(data):
        """Remove one item from a session's queue."""
        if not isinstance(data, dict):
            emit('error', {'message': 'Invalid data'})
            return
        session_id = (data.get('session_id') or '').strip()
        index = data.get('index', -1)
        if not session_id:
            emit('error', {'message': 'session_id is required'})
            return
        sm = app.session_manager
        result = sm.remove_queue_item(session_id, int(index))
        if not result.get('ok'):
            emit('error', {'message': result.get('error', 'Failed to remove'), 'session_id': session_id})

    @socketio.on('edit_queue_item')
    def handle_edit_queue_item(data):
        """Edit one item in a session's queue."""
        if not isinstance(data, dict):
            emit('error', {'message': 'Invalid data'})
            return
        session_id = (data.get('session_id') or '').strip()
        index = data.get('index', -1)
        text = (data.get('text') or '').strip()
        if not session_id or not text:
            emit('error', {'message': 'session_id and text are required'})
            return
        sm = app.session_manager
        result = sm.edit_queue_item(session_id, int(index), text)
        if not result.get('ok'):
            emit('error', {'message': result.get('error', 'Failed to edit'), 'session_id': session_id})

    @socketio.on('clear_queue')
    def handle_clear_queue(data):
        """Clear all queued messages for a session."""
        if not isinstance(data, dict):
            emit('error', {'message': 'Invalid data'})
            return
        session_id = (data.get('session_id') or '').strip()
        if not session_id:
            emit('error', {'message': 'session_id is required'})
            return
        sm = app.session_manager
        result = sm.clear_queue(session_id)
        if not result.get('ok'):
            emit('error', {'message': result.get('error', 'Failed to clear'), 'session_id': session_id})

    @socketio.on('get_queue')
    def handle_get_queue(data):
        """Return the current queue for a session."""
        if not isinstance(data, dict):
            emit('error', {'message': 'Invalid data'})
            return
        session_id = (data.get('session_id') or '').strip()
        if not session_id:
            emit('error', {'message': 'session_id is required'})
            return
        sm = app.session_manager
        items = sm.get_queue(session_id)
        emit('queue_updated', {'session_id': session_id, 'queue': items})
