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

from flask import request as flask_request
from flask_socketio import emit

logger = logging.getLogger(__name__)


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
                entries.append({"kind": "user", "text": content.strip()[:2000]})
            elif isinstance(content, list):
                for block in content:
                    bt = block.get("type", "")
                    if bt == "text" and block.get("text", "").strip():
                        entries.append({"kind": "user", "text": block["text"].strip()[:2000]})
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
                            "text": rt[:600],
                            "is_error": bool(block.get("is_error")),
                        })
        elif t == "assistant":
            msg = obj.get("message", {})
            content = msg.get("content", "")
            if isinstance(content, str) and content.strip():
                entries.append({"kind": "asst", "text": content.strip()[:3000]})
            elif isinstance(content, list):
                for block in content:
                    bt = block.get("type", "")
                    if bt == "text" and block.get("text", "").strip():
                        entries.append({"kind": "asst", "text": block["text"].strip()[:3000]})
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
        """On connect, send current state of all sessions."""
        sm = app.session_manager
        sessions = sm.get_all_states()
        emit('state_snapshot', {'sessions': sessions})
        logger.debug("WebSocket client connected, sent %d session states", len(sessions))

    @socketio.on('disconnect')
    def handle_disconnect():
        logger.debug("WebSocket client disconnected")

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

        if not result.get('ok'):
            emit('error', {
                'message': result.get('error', 'Failed to send message'),
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

        # First try hook-based permission (CLI 2.x)
        with sm._lock:
            info = sm._sessions.get(session_id)
        hook_req_id = getattr(info, '_hook_req_id', None) if info else None

        if hook_req_id:
            from .live_api import resolve_hook_permission
            hook_action = "allow" if allow else "deny"
            resolved = resolve_hook_permission(hook_req_id, hook_action)
            if not resolved:
                emit('error', {
                    'message': 'Hook permission request not found',
                    'session_id': session_id,
                })
        else:
            # Fall back to SDK callback
            result = sm.resolve_permission(session_id, allow=allow, always=always)
            if not result.get('ok'):
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
        """
        if not isinstance(data, dict):
            emit('error', {'message': 'Invalid data'})
            return

        session_id = data.get('session_id', '').strip()
        try:
            since = int(data.get('since', 0))
        except (ValueError, TypeError):
            since = 0

        if not session_id:
            emit('error', {'message': 'session_id is required'})
            return

        sm = app.session_manager

        # Always load the .jsonl history first (the authoritative record)
        entries = _parse_jsonl_entries(app, session_id, since)

        # Append any SDK-only entries (e.g., system messages from current session)
        # that aren't in the .jsonl yet
        if sm.has_session(session_id):
            sdk_entries = sm.get_entries(session_id, since=0)
            # Only add SDK entries that came after the .jsonl entries
            # (SDK entries like "Session interrupted" won't be in the file)
            jsonl_count = len(entries)
            for sdk_e in sdk_entries:
                if sdk_e.get("kind") == "system":
                    entries.append(sdk_e)

        emit('session_log', {
            'session_id': session_id,
            'entries': entries,
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
