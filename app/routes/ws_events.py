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

from flask import request as flask_request
from flask_socketio import emit

logger = logging.getLogger(__name__)

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


def _parse_jsonl_entries(app, session_id: str, since: int = 0) -> list:
    """Parse .jsonl file on disk to produce structured log entries.

    Reuses the same logic as live_api.py's api_session_log endpoint so that
    historical sessions display correctly in the live panel.
    """
    from ..config import _sessions_dir

    path = _sessions_dir() / f"{session_id}.jsonl"
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

    @socketio.on('connect')
    def handle_connect():
        """On connect, send current state of all sessions including queue data."""
        sm = app.session_manager
        sessions = sm.get_all_states()
        # Build top-level queues dict from per-session data for frontend sync
        queues = {}
        for s in sessions:
            q = s.get('queue')
            if q:
                queues[s['session_id']] = q
        # Include ID aliases so the client can resolve remapped session IDs
        # (e.g. client stored old UUID in localStorage, server knows the new one)
        aliases = dict(sm._id_aliases) if hasattr(sm, '_id_aliases') else {}
        emit('state_snapshot', {'sessions': sessions, 'queues': queues, 'aliases': aliases})
        logger.debug("WebSocket client connected, sent %d session states", len(sessions))

    @socketio.on('disconnect')
    def handle_disconnect():
        logger.debug("WebSocket client disconnected")

    @socketio.on('request_state_snapshot')
    def handle_request_state_snapshot():
        """Re-send full state snapshot on demand (e.g. after workspace switch)."""
        sm = app.session_manager
        if hasattr(sm, "is_connected") and not sm.is_connected:
            emit("state_snapshot", {"sessions": [], "queues": {}, "aliases": {}})
            return
        sessions = sm.get_all_states()
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

        # Route utility sessions to a separate project so their JSONL files
        # never appear in the user's project.
        if session_type in ('planner', 'title'):
            from ..config import _SYSTEM_UTILITY_CWD
            cwd = _SYSTEM_UTILITY_CWD

        if not session_id:
            emit('error', {'message': 'session_id is required'})
            return

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
            emit('session_started', {'session_id': session_id})
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

        if not session_id:
            emit('error', {'message': 'session_id is required'})
            return
        if not text:
            emit('error', {'message': 'text is required'})
            return

        sm = app.session_manager
        result = sm.send_message(session_id, text)

        if result.get('ok'):
            # Acknowledge receipt so the client knows the message was accepted.
            # Without this, the client has no confirmation that the server
            # processed the message — if subsequent push events are lost
            # (SocketIO transport hiccup), the client can't distinguish
            # "processing but events lost" from "message never received".
            emit('message_ack', {'session_id': session_id})
        else:
            # If session isn't idle, auto-queue instead of erroring
            err = result.get('error', '')
            if 'not idle' in err:
                q_result = sm.queue_message(session_id, text)
                if q_result.get('ok'):
                    emit('message_ack', {'session_id': session_id, 'queued': True})
                else:
                    emit('error', {
                        'message': q_result.get('error', 'Failed to queue message'),
                        'session_id': session_id,
                    })
            else:
                emit('error', {
                    'message': err or 'Failed to send message',
                    'session_id': session_id,
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
        if action not in ('y', 'n', 'a'):
            emit('error', {'message': 'action must be y, n, or a'})
            return

        allow = action in ('y', 'a')
        always = action == 'a'

        sm = app.session_manager

        # Daemon handles both hook and SDK permissions via resolve_permission
        result = sm.resolve_permission(session_id, allow=allow, always=always)
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

        if not session_id:
            emit('error', {'message': 'session_id is required'})
            return

        sm = app.session_manager

        # Load from .jsonl first, then SDK entries for anything not yet on disk
        jsonl_entries = _parse_jsonl_entries(app, session_id, since)
        sdk_entries = sm.get_entries(session_id, since=0) if sm.has_session(session_id) else []

        if jsonl_entries and not sdk_entries:
            # Historical session — only .jsonl
            entries = jsonl_entries
        elif sdk_entries and not jsonl_entries:
            # SDK-only session (no .jsonl yet)
            entries = sdk_entries
        elif sdk_entries and jsonl_entries:
            # Both exist — use whichever has more entries (SDK is more current)
            entries = sdk_entries if len(sdk_entries) >= len(jsonl_entries) else jsonl_entries
        else:
            entries = []

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

    @socketio.on('set_permission_policy')
    def handle_set_permission_policy(data):
        """Sync permission policy from browser to server."""
        if not isinstance(data, dict):
            emit('error', {'message': 'Invalid data'})
            return

        policy = (data.get('policy') or '').strip()
        custom_rules = data.get('customRules') or {}

        if policy not in ('manual', 'auto', 'custom'):
            emit('error', {'message': 'Invalid policy: must be manual, auto, or custom'})
            return

        sm = app.session_manager
        sm.set_permission_policy(policy, custom_rules)
        logger.debug("Permission policy synced: %s", policy)

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
