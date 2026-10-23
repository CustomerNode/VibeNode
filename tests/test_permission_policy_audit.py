"""Every permission-policy change is recorded with who sent it.

The policy is global (one setting for every device), and a "Claude Auto" save
on 2026-10-14 went untraced for nine days because nothing recorded which
device sent it. These tests drive the real `set_permission_policy` socket
handler and check the line it writes to logs/permission_policy.log.
"""
from unittest.mock import MagicMock

import pytest


@pytest.fixture
def ws(tmp_path, monkeypatch):
    from flask import Flask
    from flask_socketio import SocketIO
    import app.routes.ws_events as ws_events

    log_path = tmp_path / "permission_policy.log"
    monkeypatch.setattr(ws_events, "_POLICY_CHANGE_LOG", log_path)

    app = Flask(__name__)
    app.config["TESTING"] = True
    socketio = SocketIO(app, async_mode="threading")

    state = {"policy": "claude_auto", "custom_rules": {}}
    sm = MagicMock()
    sm.get_all_states.return_value = []
    sm.get_permission_policy.side_effect = lambda: dict(state)
    sm.set_permission_policy.side_effect = (
        lambda p, rules=None: state.update(policy=p, custom_rules=rules or {}))
    app.session_manager = sm

    ws_events.register_ws_events(socketio, app)
    client = socketio.test_client(
        app, headers={"User-Agent": "TestPhone/1.0",
                      "Tailscale-User-Login": "someone@example.com"})
    client.get_received()
    return client, sm, log_path


def _lines(path):
    return path.read_text(encoding="utf-8").splitlines() if path.exists() else []


def test_change_records_old_new_source_and_device(ws):
    client, sm, log_path = ws
    client.emit("set_permission_policy",
                {"policy": "auto", "customRules": {}, "source": "picker"})

    sm.set_permission_policy.assert_called_once_with("auto", {})
    lines = _lines(log_path)
    assert len(lines) == 1
    line = lines[0]
    assert "claude_auto -> auto" in line
    assert "via=picker" in line
    assert "tailnet=someone@example.com" in line
    assert "ua=TestPhone/1.0" in line
    assert "sid=" in line


def test_reselecting_the_same_policy_is_not_recorded(ws):
    client, _, log_path = ws
    client.emit("set_permission_policy",
                {"policy": "claude_auto", "customRules": {}, "source": "picker"})
    assert _lines(log_path) == []


def test_custom_rules_save_is_recorded_even_without_a_policy_change(ws):
    client, _, log_path = ws
    client.emit("set_permission_policy",
                {"policy": "claude_auto", "customRules": {"approveAllBash": True},
                 "source": "custom_rules"})
    lines = _lines(log_path)
    assert len(lines) == 1 and "via=custom_rules" in lines[0]


def test_invalid_policy_is_rejected_and_not_recorded(ws):
    client, sm, log_path = ws
    client.emit("set_permission_policy", {"policy": "hacker_mode"})
    sm.set_permission_policy.assert_not_called()
    assert _lines(log_path) == []


def test_unwritable_log_never_blocks_the_change(ws, monkeypatch, tmp_path):
    import app.routes.ws_events as ws_events
    client, sm, _ = ws
    blocker = tmp_path / "not_a_dir"
    blocker.write_text("x")
    monkeypatch.setattr(ws_events, "_POLICY_CHANGE_LOG", blocker / "p.log")
    client.emit("set_permission_policy", {"policy": "manual", "source": "picker"})
    sm.set_permission_policy.assert_called_once_with("manual", {})
