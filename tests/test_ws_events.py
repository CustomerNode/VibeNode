"""
Tests for WebSocket event handlers (ws_events.py).

Uses Flask-SocketIO's test client to simulate WebSocket connections
and verify that events are emitted/received correctly.
"""

import pytest
import sys as _tsys
from unittest.mock import MagicMock, patch

# Pre-install SDK mocks BEFORE first ws_events import. This ensures a stable
# module identity across every test in this file, so per-test `patch.dict`
# blocks below don't inadvertently drop app.routes.ws_events from sys.modules
# on exit (which caused cache-priming tests to see a stale module).
if 'claude_code_sdk' not in _tsys.modules:
    _tsys.modules['claude_code_sdk'] = MagicMock()
    _tsys.modules['claude_code_sdk.types'] = MagicMock()
# Force import now so subsequent per-test patch.dict blocks are no-ops for us.
from app.routes import ws_events as _ws_events_module  # noqa: F401,E402


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
            'cwd': '/tmp/project',
            'name': 'My Session',
            'resume': False,
        })

        mock_session_manager.start_session.assert_called_once()
        kwargs = mock_session_manager.start_session.call_args.kwargs
        assert kwargs['session_id'] == 'new-session'
        # Prompt gets a timestamp tag appended — check it starts with original
        assert kwargs['prompt'].startswith('Hello Claude')
        assert kwargs['cwd'] == '/tmp/project'
        assert kwargs['name'] == 'My Session'
        assert kwargs['resume'] is False

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

        # ``_ttft_t0_ns`` is the opt-in TTFT wall-clock timestamp; 0 when the
        # env var is unset (default in tests). See daemon/session_manager.py
        # and docs/plans/runs/2026-09-10-1625-ttft-instrumentation/.
        mock_session_manager.send_message.assert_called_with(
            's1', 'What is the weather?', voice=False, _ttft_t0_ns=0,
        )

    def test_send_message_missing_text(self, app_and_client, mock_session_manager):
        """send_message without text should emit error."""
        app, socketio, client = app_and_client
        client.get_received()

        client.emit('send_message', {'session_id': 's1'})

        received = client.get_received()
        errors = [msg for msg in received if msg['name'] == 'error']
        assert len(errors) >= 1

    def test_send_message_failure(self, app_and_client, mock_session_manager):
        """send_message failure should emit send_failed event."""
        mock_session_manager.send_message.return_value = {"ok": False, "error": "Not idle"}
        app, socketio, client = app_and_client
        client.get_received()

        client.emit('send_message', {'session_id': 's2', 'text': 'Hello'})

        received = client.get_received()
        failures = [msg for msg in received if msg['name'] == 'send_failed']
        assert len(failures) >= 1
        assert failures[0]['args'][0]['session_id'] == 's2'
        assert 'error' in failures[0]['args'][0]


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
            's1', allow=True, always=False, almost_always=False
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
            's1', allow=False, always=False, almost_always=False
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
            's1', allow=True, always=True, almost_always=False
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
        """get_session_log should fetch daemon entries with since=0 for comparison."""
        app, socketio, client = app_and_client
        client.get_received()

        client.emit('get_session_log', {'session_id': 's1', 'since': 5})

        # Production always fetches full daemon entries (since=0) to compare
        # count against JSONL-parsed entries and pick the more complete source.
        mock_session_manager.get_entries.assert_called_with('s1', since=0)


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


# ---------------------------------------------------------------------------
# 11. Queue events
# ---------------------------------------------------------------------------

class TestQueueEvents:

    def test_queue_message(self, app_and_client, mock_session_manager):
        app, socketio, client = app_and_client
        mock_session_manager.queue_message.return_value = {"ok": True}
        client.get_received()

        client.emit('queue_message', {'session_id': 's1', 'text': 'queued msg'})
        mock_session_manager.queue_message.assert_called_with('s1', 'queued msg')

    def test_queue_message_missing_text(self, app_and_client, mock_session_manager):
        app, socketio, client = app_and_client
        client.get_received()

        client.emit('queue_message', {'session_id': 's1'})
        received = client.get_received()
        errors = [msg for msg in received if msg['name'] == 'error']
        assert len(errors) >= 1

    def test_remove_queue_item(self, app_and_client, mock_session_manager):
        app, socketio, client = app_and_client
        mock_session_manager.remove_queue_item.return_value = {"ok": True}
        client.get_received()

        client.emit('remove_queue_item', {'session_id': 's1', 'index': 0})
        mock_session_manager.remove_queue_item.assert_called_with('s1', 0)

    def test_edit_queue_item(self, app_and_client, mock_session_manager):
        app, socketio, client = app_and_client
        mock_session_manager.edit_queue_item.return_value = {"ok": True}
        client.get_received()

        client.emit('edit_queue_item', {'session_id': 's1', 'index': 0, 'text': 'edited'})
        mock_session_manager.edit_queue_item.assert_called_with('s1', 0, 'edited')

    def test_clear_queue(self, app_and_client, mock_session_manager):
        app, socketio, client = app_and_client
        mock_session_manager.clear_queue.return_value = {"ok": True}
        client.get_received()

        client.emit('clear_queue', {'session_id': 's1'})
        mock_session_manager.clear_queue.assert_called_with('s1')

    def test_get_queue(self, app_and_client, mock_session_manager):
        app, socketio, client = app_and_client
        mock_session_manager.get_queue.return_value = [
            {"text": "msg1", "index": 0},
        ]
        client.get_received()

        client.emit('get_queue', {'session_id': 's1'})
        mock_session_manager.get_queue.assert_called_with('s1')

        received = client.get_received()
        queue_msgs = [msg for msg in received if msg['name'] == 'queue_updated']
        assert len(queue_msgs) >= 1


# ---------------------------------------------------------------------------
# 12. Permission policy
# ---------------------------------------------------------------------------

class TestPermissionPolicy:

    def test_set_permission_policy(self, app_and_client, mock_session_manager):
        app, socketio, client = app_and_client
        mock_session_manager.set_permission_policy.return_value = {"ok": True}
        client.get_received()

        client.emit('set_permission_policy', {'policy': 'auto'})
        mock_session_manager.set_permission_policy.assert_called_with('auto', {})

    def test_set_permission_policy_invalid(self, app_and_client, mock_session_manager):
        app, socketio, client = app_and_client
        client.get_received()

        client.emit('set_permission_policy', {'policy': 'invalid_policy'})
        received = client.get_received()
        errors = [msg for msg in received if msg['name'] == 'error']
        assert len(errors) >= 1

    def test_get_permission_policy(self, app_and_client, mock_session_manager):
        app, socketio, client = app_and_client
        mock_session_manager.get_permission_policy.return_value = {
            'policy': 'manual', 'custom_rules': {}
        }
        client.get_received()

        client.emit('get_permission_policy')
        received = client.get_received()
        policy_msgs = [msg for msg in received if msg['name'] == 'permission_policy_loaded']
        assert len(policy_msgs) >= 1


# ---------------------------------------------------------------------------
# 13. UI prefs
# ---------------------------------------------------------------------------

class TestUIPrefs:

    def test_get_ui_prefs(self, app_and_client, mock_session_manager):
        app, socketio, client = app_and_client
        mock_session_manager.get_ui_prefs.return_value = {'theme': 'dark'}
        client.get_received()

        client.emit('get_ui_prefs')
        received = client.get_received()
        pref_msgs = [msg for msg in received if msg['name'] == 'ui_prefs_loaded']
        assert len(pref_msgs) >= 1

    def test_set_ui_prefs(self, app_and_client, mock_session_manager):
        app, socketio, client = app_and_client
        client.get_received()

        client.emit('set_ui_prefs', {'theme': 'light'})
        mock_session_manager.set_ui_prefs.assert_called_with({'theme': 'light'})

    # --- Session retention pref over the socket (added 2026-05-30) ---

    def test_set_retention_via_socket(self, app_and_client, mock_session_manager):
        app, socketio, client = app_and_client
        client.get_received()

        client.emit('set_ui_prefs', {'session_retention_days': 60})
        mock_session_manager.set_ui_prefs.assert_called_with(
            {'session_retention_days': 60}
        )

    def test_get_retention_via_socket(self, app_and_client, mock_session_manager):
        app, socketio, client = app_and_client
        mock_session_manager.get_ui_prefs.return_value = {
            'session_retention_days': 90
        }
        client.get_received()

        client.emit('get_ui_prefs')
        received = client.get_received()
        loaded = next(m for m in received if m['name'] == 'ui_prefs_loaded')
        assert loaded['args'][0].get('session_retention_days') == 90


# ---------------------------------------------------------------------------
# 14. Load-older regression guard (2026-09-11-1546-load-older-fix)
#
# Fix #1 (2026-09-10-1715-session-log-tail) introduced an initial-page fast
# path that trusts ``sm.get_entry_count`` as the JSONL total.  For big
# sessions the daemon trims ``info.entries`` to 200 while the JSONL grows
# unbounded, so the fast path emits ``total=200, has_more=True/False``
# incoherent with the true JSONL indices.  These tests verify the fast
# path is bypassed whenever the daemon does NOT have the full history,
# and preserved when it does.
# ---------------------------------------------------------------------------

class TestLoadOlderRegression:

    def _make_jsonl(self, tmp_path, n_entries: int, session_id: str = "big"):
        """Write a synthetic JSONL file with ``n_entries`` user messages."""
        import json as _json
        sess_dir = tmp_path / "proj"
        sess_dir.mkdir(exist_ok=True)
        p = sess_dir / f"{session_id}.jsonl"
        with p.open("w", encoding="utf-8") as fh:
            for i in range(n_entries):
                obj = {"type": "user", "message": {"content": f"msg {i}"}}
                fh.write(_json.dumps(obj) + "\n")
        return sess_dir, p

    def _patched_client(self, sm, tmp_path, monkeypatch):
        """Rebuild the app_and_client fixture with a custom SessionManager and
        a monkeypatched _sessions_dir so parse hits our synthetic JSONL."""
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
            app.session_manager = sm

            # Import ws_events AFTER sdk mocks are in place.
            from app.routes import ws_events as _wsev
            # Reset the module-level entry cache so tests don't leak into each
            # other, and monkeypatch _sessions_dir to point at tmp_path/proj.
            _wsev._entry_cache.clear()
            sess_dir = tmp_path / "proj"

            def _fake_sessions_dir(project: str = ""):
                return sess_dir

            monkeypatch.setattr("app.config._sessions_dir", _fake_sessions_dir)

            from app.routes.ws_events import register_ws_events
            register_ws_events(socketio, app)
            client = socketio.test_client(app)
            return app, socketio, client, _wsev

    # -- Scenario A: daemon trimmed (200 entries, flag set) + JSONL 2000 --
    def test_trimmed_daemon_falls_through_to_legacy(self, tmp_path, monkeypatch):
        sess_dir, _ = self._make_jsonl(tmp_path, 2000, session_id="big")
        sm = MagicMock()
        sm.has_session.return_value = True
        # Sticky trim flag -- the fast path MUST bypass.
        sm.get_entry_trim_status.return_value = (200, True)
        sm.get_entry_count.return_value = 200  # keep old callers correct
        # If the fast path is (correctly) bypassed, legacy path calls
        # get_entries(sid, since=0) to compare against JSONL.  Return an
        # empty daemon buffer so the JSONL wins.
        sm.get_entries.return_value = []

        app, socketio, client, wsev = self._patched_client(sm, tmp_path, monkeypatch)
        client.get_received()
        client.emit('get_session_log', {
            'session_id': 'big', 'limit': 100, 'since': 0,
        })

        received = client.get_received()
        logs = [msg for msg in received if msg['name'] == 'session_log']
        assert len(logs) == 1, f"expected 1 session_log, got {len(logs)}"
        data = logs[0]['args'][0]
        assert data['total'] == 2000, (
            f"trim-flag bypass failed: total={data['total']} (want 2000)"
        )
        assert data['offset'] == 1900, (
            f"trim-flag bypass failed: offset={data['offset']} (want 1900)"
        )
        assert data['has_more'] is True
        assert len(data['entries']) == 100
        client.disconnect()

    # -- Scenario B: small session, daemon has full history, no trim,
    # entry_cache is WARM (i.e. this is the 2nd+ visit to the session). --
    # BUGFIX 2026-09-11 (regression audit): the fast path requires a warm
    # entry_cache to verify daemon_count >= jsonl_len before emitting daemon-
    # coordinate values. Cold cache falls through to legacy (see
    # test_full_history_cold_cache_falls_through). This test asserts the
    # WARM-cache behavior (fast path).
    def test_full_history_uses_fast_path(self, tmp_path, monkeypatch):
        sess_dir, jsonl_path = self._make_jsonl(tmp_path, 150, session_id="small")
        sm = MagicMock()
        sm.has_session.return_value = True
        sm.get_entry_trim_status.return_value = (150, False)
        sm.get_entry_count.return_value = 150
        # Fast path fetches only the last `limit` entries; the daemon returns
        # a slice of the last 100.
        sm.get_entries.return_value = [
            {"kind": "user", "text": f"msg {i}"} for i in range(50, 150)
        ]

        app, socketio, client, wsev = self._patched_client(sm, tmp_path, monkeypatch)
        # Prime the entry_cache so the fast path can verify daemon_count >=
        # jsonl_len.  Real world: this is the second visit to a session where
        # a prior legacy parse populated the cache.
        import os as _os
        _st = _os.stat(jsonl_path)
        wsev._entry_cache['small'] = (
            _st.st_mtime, _st.st_size,
            [{"kind": "user", "text": f"msg {i}"} for i in range(150)],
        )
        client.get_received()
        client.emit('get_session_log', {
            'session_id': 'small', 'limit': 100, 'since': 0,
        })

        received = client.get_received()
        logs = [msg for msg in received if msg['name'] == 'session_log']
        assert len(logs) == 1
        data = logs[0]['args'][0]
        # Fast path emits total = daemon_count.
        assert data['total'] == 150
        assert data['offset'] == 50
        assert data['has_more'] is True
        # Fast path calls get_entries with since=start_idx=50.
        sm.get_entries.assert_called_with('small', since=50)
        client.disconnect()

    # -- Scenario B2: full history but cold cache -> MUST fall through --
    # This is the regression the audit caught: on cold cache, we cannot
    # verify daemon_count matches JSONL entry count.  Fast path is unsafe
    # unless proven safe.  Test locks in fall-through.
    def test_full_history_cold_cache_falls_through(self, tmp_path, monkeypatch):
        sess_dir, jsonl_path = self._make_jsonl(tmp_path, 150, session_id="cold")
        sm = MagicMock()
        sm.has_session.return_value = True
        # Daemon reports 150 (looks like full history), but cache is empty
        # so ws_events cannot verify.  Must fall through.
        sm.get_entry_trim_status.return_value = (150, False)
        sm.get_entry_count.return_value = 150
        # Legacy path calls get_entries(sid, since=0) to compare against JSONL.
        # Return an empty daemon buffer so JSONL wins.
        sm.get_entries.return_value = []

        app, socketio, client, wsev = self._patched_client(sm, tmp_path, monkeypatch)
        # NOTE: no cache priming -- this is the cold-cache case.
        assert 'cold' not in wsev._entry_cache
        client.get_received()
        client.emit('get_session_log', {
            'session_id': 'cold', 'limit': 100, 'since': 0,
        })

        received = client.get_received()
        logs = [msg for msg in received if msg['name'] == 'session_log']
        assert len(logs) == 1, f"expected 1 session_log, got {len(logs)}"
        data = logs[0]['args'][0]
        # Legacy path parses the 150-entry JSONL.  With limit=100 the client
        # gets the last 100; offset=50; total=150.
        assert data['total'] == 150, f"got total={data['total']}"
        assert data['offset'] == 50, f"got offset={data['offset']}"
        assert data['has_more'] is True
        assert len(data['entries']) == 100
        # Legacy path calls get_entries with since=0 (NOT since=50).
        sm.get_entries.assert_called_with('cold', since=0)
        client.disconnect()

    # -- Scenario C: freshly resumed, daemon has 0 entries yet --
    def test_fresh_resume_falls_through(self, tmp_path, monkeypatch):
        sess_dir, _ = self._make_jsonl(tmp_path, 30, session_id="fresh")
        sm = MagicMock()
        sm.has_session.return_value = True
        # daemon.info.entries hasn't been hydrated yet.
        sm.get_entry_trim_status.return_value = (0, False)
        sm.get_entry_count.return_value = 0
        sm.get_entries.return_value = []

        app, socketio, client, wsev = self._patched_client(sm, tmp_path, monkeypatch)
        client.get_received()
        client.emit('get_session_log', {
            'session_id': 'fresh', 'limit': 100, 'since': 0,
        })

        received = client.get_received()
        logs = [msg for msg in received if msg['name'] == 'session_log']
        assert len(logs) == 1
        data = logs[0]['args'][0]
        # Legacy path parses the 30-entry JSONL.  30 <= limit, so no
        # pagination -> total=30, offset=0.
        assert data['total'] == 30
        assert data['has_more'] is False
        assert len(data['entries']) == 30
        client.disconnect()

    # -- Scenario D: end-to-end pagination coherence over the trim boundary --
    # This is the core user-visible regression: initial page then load-more
    # over `before=offset` must cover contiguous JSONL indices with no gap.
    def test_load_more_pagination_is_contiguous(self, tmp_path, monkeypatch):
        sess_dir, jsonl_path = self._make_jsonl(tmp_path, 2000, session_id="huge")
        sm = MagicMock()
        sm.has_session.return_value = True
        sm.get_entry_trim_status.return_value = (200, True)  # trimmed
        sm.get_entry_count.return_value = 200
        sm.get_entries.return_value = []  # force JSONL to win in legacy path

        app, socketio, client, wsev = self._patched_client(sm, tmp_path, monkeypatch)
        client.get_received()

        # 1) Initial load.
        client.emit('get_session_log', {
            'session_id': 'huge', 'limit': 100, 'since': 0,
        })
        received = client.get_received()
        first = next(m for m in received if m['name'] == 'session_log')['args'][0]
        assert first['total'] == 2000
        assert first['offset'] == 1900
        assert first['has_more'] is True
        assert first['entries'][0]['text'] == 'msg 1900'
        assert first['entries'][-1]['text'] == 'msg 1999'

        # 2) Load older: before = first['offset'] = 1900.
        client.emit('get_session_log', {
            'session_id': 'huge', 'limit': 100, 'since': 0,
            'before': first['offset'],
        })
        received = client.get_received()
        second = next(m for m in received if m['name'] == 'session_log')['args'][0]
        assert second['total'] == 2000
        # end=1900, start=1800, page=entries[1800:1900]
        assert second['offset'] == 1800
        assert second['has_more'] is True
        assert second['entries'][0]['text'] == 'msg 1800'
        assert second['entries'][-1]['text'] == 'msg 1899'

        # Contiguity: last index of page 2 (1899) + 1 == first index of page 1 (1900).
        last_of_older = int(second['entries'][-1]['text'].split()[1])
        first_of_newer = int(first['entries'][0]['text'].split()[1])
        assert last_of_older + 1 == first_of_newer, (
            f"pagination gap: older ends at {last_of_older}, "
            f"newer starts at {first_of_newer}"
        )
        client.disconnect()

    # -- Scenario E: defense-in-depth.  Even if the daemon LIES about
    # trimming (trim flag says False but daemon has fewer entries than
    # JSONL), the cold-cache guard added by the 2026-09-11 regression audit
    # (see 2026-09-11-1600-regression-audit) catches it and falls through
    # to legacy.  This test protects against the exact original bug even
    # in the presence of a broken trim flag.
    def test_lying_trim_flag_still_caught_by_cache_guard(self, tmp_path, monkeypatch):
        self._make_jsonl(tmp_path, 2000, session_id="pre")
        sm = MagicMock()
        sm.has_session.return_value = True
        # SIMULATED BUG: daemon claims not-trimmed even though it obviously is
        # (200 in-memory but JSONL has 2000).  This models a hypothetical
        # future regression where the trim flag stops being set.
        sm.get_entry_trim_status.return_value = (200, False)
        sm.get_entry_count.return_value = 200
        # Legacy path calls get_entries(sid, since=0) to compare against JSONL.
        sm.get_entries.return_value = []

        app, socketio, client, wsev = self._patched_client(sm, tmp_path, monkeypatch)
        # NOTE: no cache priming -- cold cache is the second line of defense.
        assert 'pre' not in wsev._entry_cache
        client.get_received()
        client.emit('get_session_log', {
            'session_id': 'pre', 'limit': 100, 'since': 0,
        })
        received = client.get_received()
        data = next(m for m in received if m['name'] == 'session_log')['args'][0]
        # Cold-cache guard forces fall-through to legacy -> truthful counts
        # from JSONL.  If the guard is ever removed, this test flips back to
        # asserting the pre-fix bug (total=200, offset=100) and fails loudly.
        assert data['total'] == 2000, (
            f"defense-in-depth failed: total={data['total']} (want 2000). "
            "Cold-cache guard was removed?"
        )
        assert data['offset'] == 1900
        client.disconnect()
