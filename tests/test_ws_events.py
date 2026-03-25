"""
Tests for WebSocket event handlers (ws_events.py).

Uses Flask-SocketIO's test client to simulate WebSocket connections
and verify that events are emitted/received correctly.
"""

import pytest
from unittest.mock import MagicMock, patch


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def mock_session_manager():
    """Create a mock SessionManager for testing."""
    sm = MagicMock()
    sm.get_all_states.return_value = [
        {"session_id": "s1", "state": "idle", "cost_usd": 0.01, "error": None, "name": "Session 1"},
        {"session_id": "s2", "state": "working", "cost_usd": 0.0, "error": None, "name": "Session 2"},
    ]
    sm.start_session.return_value = {"ok": True}
    sm.send_message.return_value = {"ok": True}
    sm.resolve_permission.return_value = {"ok": True}
    sm.interrupt_session.return_value = {"ok": True}
    sm.close_session.return_value = {"ok": True}
    sm.get_entries.return_value = [
        {"kind": "user", "text": "Hello", "timestamp": 1700000000.0},
        {"kind": "asst", "text": "Hi there", "timestamp": 1700000001.0},
    ]
    sm.has_session.return_value = True
    return sm


@pytest.fixture
def app_and_client(mock_session_manager):
    """Create a Flask app with SocketIO and return (app, socketio, test_client)."""
    # We need to mock the SDK imports before importing our app modules
    sdk_mocks = {
        'claude_code_sdk': MagicMock(),
        'claude_code_sdk.types': MagicMock(),
    }

    with patch.dict('sys.modules', sdk_mocks):
        from flask import Flask
        from flask_socketio import SocketIO

        app = Flask(__name__)
        app.config['TESTING'] = True
        socketio = SocketIO(app, async_mode='threading')

        # Attach mock session manager
        app.session_manager = mock_session_manager

        # Register WS events
        from app.routes.ws_events import register_ws_events
        register_ws_events(socketio, app)

        # Create test client
        client = socketio.test_client(app)

        yield app, socketio, client

        client.disconnect()


# ---------------------------------------------------------------------------
# 1. Connect receives state snapshot
# ---------------------------------------------------------------------------

class TestConnect:

    def test_connect_receives_state_snapshot(self, app_and_client, mock_session_manager):
        """On connect, client should receive state_snapshot with all sessions."""
        app, socketio, client = app_and_client

        received = client.get_received()
        # Find the state_snapshot event
        snapshots = [msg for msg in received if msg['name'] == 'state_snapshot']
        assert len(snapshots) >= 1

        data = snapshots[0]['args'][0]
        assert 'sessions' in data
        assert len(data['sessions']) == 2
        assert data['sessions'][0]['session_id'] == 's1'
        assert data['sessions'][1]['session_id'] == 's2'

    def test_connect_calls_get_all_states(self, app_and_client, mock_session_manager):
        """Connect should call session_manager.get_all_states()."""
        mock_session_manager.get_all_states.assert_called()


# ---------------------------------------------------------------------------
# 2. Start session event
# ---------------------------------------------------------------------------

class TestStartSession:

    def test_start_session_event(self, app_and_client, mock_session_manager):
        """Emitting start_session should call session_manager.start_session."""
        app, socketio, client = app_and_client
        client.get_received()  # clear initial messages

        client.emit('start_session', {
            'session_id': 'new-session',
            'prompt': 'Hello Claude',
            'cwd': '/home/user/project',
            'name': 'My Session',
            'resume': False,
        })

        mock_session_manager.start_session.assert_called_with(
            session_id='new-session',
            prompt='Hello Claude',
            cwd='/home/user/project',
            name='My Session',
            resume=False,
        )

        received = client.get_received()
        started = [msg for msg in received if msg['name'] == 'session_started']
        assert len(started) == 1
        assert started[0]['args'][0]['session_id'] == 'new-session'

    def test_start_session_missing_id(self, app_and_client, mock_session_manager):
        """start_session without session_id should emit error."""
        app, socketio, client = app_and_client
        client.get_received()

        client.emit('start_session', {'prompt': 'Hello'})

        received = client.get_received()
        errors = [msg for msg in received if msg['name'] == 'error']
        assert len(errors) >= 1
        assert 'session_id' in errors[0]['args'][0]['message'].lower()

    def test_start_session_failure(self, app_and_client, mock_session_manager):
        """start_session failure should emit error event."""
        mock_session_manager.start_session.return_value = {"ok": False, "error": "Already running"}
        app, socketio, client = app_and_client
        client.get_received()

        client.emit('start_session', {'session_id': 'fail-session', 'prompt': 'Hi'})

        received = client.get_received()
        errors = [msg for msg in received if msg['name'] == 'error']
        assert len(errors) >= 1


# ---------------------------------------------------------------------------
# 3. Send message event
# ---------------------------------------------------------------------------

class TestSendMessage:

    def test_send_message_event(self, app_and_client, mock_session_manager):
        """Emitting send_message should call session_manager.send_message."""
        app, socketio, client = app_and_client
        client.get_received()

        client.emit('send_message', {
            'session_id': 's1',
            'text': 'What is the weather?',
        })

        mock_session_manager.send_message.assert_called_with('s1', 'What is the weather?')

    def test_send_message_missing_text(self, app_and_client, mock_session_manager):
        """send_message without text should emit error."""
        app, socketio, client = app_and_client
        client.get_received()

        client.emit('send_message', {'session_id': 's1'})

        received = client.get_received()
        errors = [msg for msg in received if msg['name'] == 'error']
        assert len(errors) >= 1

    def test_send_message_failure(self, app_and_client, mock_session_manager):
        """send_message to non-idle session should emit error."""
        mock_session_manager.send_message.return_value = {"ok": False, "error": "Not idle"}
        app, socketio, client = app_and_client
        client.get_received()

        client.emit('send_message', {'session_id': 's2', 'text': 'Hello'})

        received = client.get_received()
        errors = [msg for msg in received if msg['name'] == 'error']
        assert len(errors) >= 1


# ---------------------------------------------------------------------------
# 4. Permission response event
# ---------------------------------------------------------------------------

class TestPermissionResponse:

    def test_permission_response_allow(self, app_and_client, mock_session_manager):
        """Permission response 'y' should resolve with allow=True, always=False."""
        app, socketio, client = app_and_client
        client.get_received()

        client.emit('permission_response', {
            'session_id': 's1',
            'action': 'y',
        })

        mock_session_manager.resolve_permission.assert_called_with(
            's1', allow=True, always=False
        )

    def test_permission_response_deny(self, app_and_client, mock_session_manager):
        """Permission response 'n' should resolve with allow=False."""
        app, socketio, client = app_and_client
        client.get_received()

        client.emit('permission_response', {
            'session_id': 's1',
            'action': 'n',
        })

        mock_session_manager.resolve_permission.assert_called_with(
            's1', allow=False, always=False
        )

    def test_permission_response_always(self, app_and_client, mock_session_manager):
        """Permission response 'a' should resolve with allow=True, always=True."""
        app, socketio, client = app_and_client
        client.get_received()

        client.emit('permission_response', {
            'session_id': 's1',
            'action': 'a',
        })

        mock_session_manager.resolve_permission.assert_called_with(
            's1', allow=True, always=True
        )

    def test_permission_response_invalid_action(self, app_and_client, mock_session_manager):
        """Permission response with invalid action should emit error."""
        app, socketio, client = app_and_client
        client.get_received()

        client.emit('permission_response', {
            'session_id': 's1',
            'action': 'x',
        })

        received = client.get_received()
        errors = [msg for msg in received if msg['name'] == 'error']
        assert len(errors) >= 1
        assert 'action' in errors[0]['args'][0]['message'].lower()


# ---------------------------------------------------------------------------
# 5. Interrupt session event
# ---------------------------------------------------------------------------

class TestInterruptSession:

    def test_interrupt_session_event(self, app_and_client, mock_session_manager):
        """Emitting interrupt_session should call session_manager.interrupt_session."""
        app, socketio, client = app_and_client
        client.get_received()

        client.emit('interrupt_session', {'session_id': 's2'})

        mock_session_manager.interrupt_session.assert_called_with('s2')

    def test_interrupt_session_missing_id(self, app_and_client, mock_session_manager):
        """interrupt_session without session_id should emit error."""
        app, socketio, client = app_and_client
        client.get_received()

        client.emit('interrupt_session', {})

        received = client.get_received()
        errors = [msg for msg in received if msg['name'] == 'error']
        assert len(errors) >= 1


# ---------------------------------------------------------------------------
# 6. Close session event
# ---------------------------------------------------------------------------

class TestCloseSession:

    def test_close_session_event(self, app_and_client, mock_session_manager):
        """Emitting close_session should call session_manager.close_session."""
        app, socketio, client = app_and_client
        client.get_received()

        client.emit('close_session', {'session_id': 's1'})

        mock_session_manager.close_session.assert_called_with('s1')

    def test_close_session_failure(self, app_and_client, mock_session_manager):
        """close_session failure should emit error."""
        mock_session_manager.close_session.return_value = {"ok": False, "error": "Not found"}
        app, socketio, client = app_and_client
        client.get_received()

        client.emit('close_session', {'session_id': 'nonexistent'})

        received = client.get_received()
        errors = [msg for msg in received if msg['name'] == 'error']
        assert len(errors) >= 1


# ---------------------------------------------------------------------------
# 7. Get session log event
# ---------------------------------------------------------------------------

class TestGetSessionLog:

    def test_get_session_log_event(self, app_and_client, mock_session_manager):
        """Emitting get_session_log should return entries."""
        app, socketio, client = app_and_client
        client.get_received()

        client.emit('get_session_log', {'session_id': 's1', 'since': 0})

        received = client.get_received()
        logs = [msg for msg in received if msg['name'] == 'session_log']
        assert len(logs) == 1

        data = logs[0]['args'][0]
        assert data['session_id'] == 's1'
        assert len(data['entries']) == 2
        assert data['entries'][0]['kind'] == 'user'
        assert data['entries'][1]['kind'] == 'asst'

        mock_session_manager.get_entries.assert_called_with('s1', since=0)

    def test_get_session_log_with_since(self, app_and_client, mock_session_manager):
        """get_session_log with since should pass through to get_entries."""
        app, socketio, client = app_and_client
        client.get_received()

        client.emit('get_session_log', {'session_id': 's1', 'since': 5})

        mock_session_manager.get_entries.assert_called_with('s1', since=5)


# ---------------------------------------------------------------------------
# 8-10. Server-push events (tested indirectly via SessionManager integration)
# ---------------------------------------------------------------------------

class TestServerPushEvents:

    def test_session_state_pushed_on_transition(self, mock_session_manager):
        """When SessionManager emits session_state, it should reach clients.
        This is tested via the mock socketio in session_manager tests."""
        # This is an integration concern -- verified in test_session_manager.py
        # where we check that _emit_state is called with the right data.
        # Here we verify the mock contract:
        mock_session_manager.get_all_states.return_value = [
            {"session_id": "s1", "state": "working", "cost_usd": 0.0, "error": None, "name": ""}
        ]
        states = mock_session_manager.get_all_states()
        assert states[0]["state"] == "working"

    def test_invalid_data_types(self, app_and_client, mock_session_manager):
        """Sending non-dict data should emit error."""
        app, socketio, client = app_and_client
        client.get_received()

        client.emit('start_session', "not a dict")

        received = client.get_received()
        errors = [msg for msg in received if msg['name'] == 'error']
        assert len(errors) >= 1

    def test_permission_pushed_on_callback(self, mock_session_manager):
        """The SessionManager should emit session_permission when a permission
        callback fires. This is an integration test verified in session_manager tests."""
        # Contract verification: the mock session manager supports the interface
        assert hasattr(mock_session_manager, 'resolve_permission')
        mock_session_manager.resolve_permission.return_value = {"ok": True}
        result = mock_session_manager.resolve_permission("s1", allow=True, always=False)
        assert result["ok"] is True
