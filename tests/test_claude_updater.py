"""
Tests for ``app/claude_updater.py`` and its two admin routes.

Covers:
  * ``_STATE_FILE`` stays in sync with run.py's ``_UPDATE_STATE_FILE`` —
    a rename in one file that misses the other would silently split the
    daily-check state from the on-demand path.
  * ``count_active_sessions`` — non-STOPPED sessions count; STOPPED and
    malformed entries do not.
  * ``status_snapshot`` — dict shape, stale threshold, restart_pending
    surface.
  * ``run_update`` — the block-on-sessions guard, the in-progress lock,
    the not-installed short-circuit, and the happy-path version bump.
  * The two admin routes: correct status codes for blocked/failed/ok.
"""

from __future__ import annotations

import json
import re
import threading
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from app import claude_updater


# ---------------------------------------------------------------------------
# State file path — MUST match run.py
# ---------------------------------------------------------------------------

def test_state_file_path_matches_run_py():
    """A rename in either place breaks the daily / on-demand handoff."""
    run_py = Path(__file__).resolve().parent.parent / "run.py"
    text = run_py.read_text(encoding="utf-8")
    m = re.search(
        r"_UPDATE_STATE_FILE\s*=\s*Path\(__file__\)\.resolve\(\)\.parent\s*/\s*"
        r'"([^"]+)"\s*/\s*"([^"]+)"',
        text,
    )
    assert m is not None, "run.py._UPDATE_STATE_FILE definition not found"
    expected = run_py.resolve().parent / m.group(1) / m.group(2)
    assert claude_updater._STATE_FILE == expected


# ---------------------------------------------------------------------------
# count_active_sessions
# ---------------------------------------------------------------------------

def test_count_active_sessions_ignores_stopped_and_malformed():
    sm = MagicMock()
    sm.get_all_states.return_value = [
        {"session_id": "a", "state": "stopped"},
        {"session_id": "b", "state": "idle"},
        {"session_id": "c", "state": "working"},
        {"session_id": "d", "state": "waiting"},
        {"session_id": "e", "state": ""},          # malformed — skip
        "not-a-dict",                              # malformed — skip
        {"session_id": "f", "state": "STOPPED"},   # case-insensitive
    ]
    assert claude_updater.count_active_sessions(sm) == 3


def test_count_active_sessions_none_manager():
    assert claude_updater.count_active_sessions(None) == 0


def test_count_active_sessions_swallows_ipc_failure():
    sm = MagicMock()
    sm.get_all_states.side_effect = RuntimeError("daemon down")
    # Fail-safe: report 0 rather than blocking the update path on IPC health.
    assert claude_updater.count_active_sessions(sm) == 0


# ---------------------------------------------------------------------------
# read_state / status_snapshot
# ---------------------------------------------------------------------------

@pytest.fixture()
def state_file(tmp_path, monkeypatch):
    """Redirect ``_STATE_FILE`` at the tmp dir for isolation."""
    p = tmp_path / ".cache" / "state.json"
    monkeypatch.setattr(claude_updater, "_STATE_FILE", p)
    return p


def test_read_state_missing_returns_empty(state_file):
    assert claude_updater.read_state() == {}


def test_read_state_malformed_returns_empty(state_file):
    state_file.parent.mkdir(parents=True)
    state_file.write_text("{not json")
    assert claude_updater.read_state() == {}


def test_status_snapshot_no_state_marks_stale(state_file, monkeypatch):
    monkeypatch.setattr(claude_updater, "_claude_path", lambda: "/fake/claude")
    monkeypatch.setattr(claude_updater, "current_version", lambda path=None: "2.1.277")
    snap = claude_updater.status_snapshot(None)
    assert snap["installed"] is True
    assert snap["current_version"] == "2.1.277"
    assert snap["last_check"] == 0
    assert snap["last_check_age_seconds"] is None
    assert snap["stale"] is True
    assert snap["restart_pending"] is False
    assert snap["active_sessions"] == 0


def test_status_snapshot_fresh_state_not_stale(state_file, monkeypatch):
    import time
    state_file.parent.mkdir(parents=True)
    state_file.write_text(json.dumps({
        "last_check": time.time() - 3600,
        "cli_version": "2.1.280",
        "updated_last_check": True,
        "daemon_restart_pending": True,
    }))
    monkeypatch.setattr(claude_updater, "_claude_path", lambda: "/fake/claude")
    monkeypatch.setattr(claude_updater, "current_version", lambda path=None: "2.1.280")
    snap = claude_updater.status_snapshot(None)
    assert snap["stale"] is False
    assert snap["restart_pending"] is True
    assert snap["last_recorded_version"] == "2.1.280"
    assert snap["updated_last_check"] is True


def test_status_snapshot_not_installed(state_file, monkeypatch):
    monkeypatch.setattr(claude_updater, "_claude_path", lambda: None)
    snap = claude_updater.status_snapshot(None)
    assert snap["installed"] is False
    assert snap["current_version"] == ""


# ---------------------------------------------------------------------------
# run_update
# ---------------------------------------------------------------------------

def test_run_update_refuses_when_sessions_running(state_file, monkeypatch):
    monkeypatch.setattr(claude_updater, "_claude_path", lambda: "/fake/claude")
    monkeypatch.setattr(claude_updater, "current_version", lambda path=None: "2.1.277")
    sm = MagicMock()
    sm.get_all_states.return_value = [
        {"session_id": "a", "state": "working"},
    ]
    result = claude_updater.run_update(session_manager=sm, force=False)
    assert result["ok"] is False
    assert result["blocked"] == "sessions_running"
    assert "1 session" in result["error"]


def test_run_update_not_installed(state_file, monkeypatch):
    monkeypatch.setattr(claude_updater, "_claude_path", lambda: None)
    result = claude_updater.run_update(session_manager=None, force=False)
    assert result["ok"] is False
    assert result["blocked"] == "not_installed"


def test_run_update_happy_path_bumps_version(state_file, monkeypatch):
    monkeypatch.setattr(claude_updater, "_claude_path", lambda: "/fake/claude")
    versions = iter(["2.1.277", "2.1.280"])
    monkeypatch.setattr(
        claude_updater, "current_version",
        lambda path=None: next(versions),
    )

    def _fake_run(cmd, timeout):
        assert cmd[0] == "/fake/claude" and cmd[1] == "update"
        return 0, "Updated to 2.1.280"

    monkeypatch.setattr(claude_updater, "_run_captured", _fake_run)

    result = claude_updater.run_update(session_manager=None, force=False)
    assert result["ok"] is True
    assert result["before"] == "2.1.277"
    assert result["after"] == "2.1.280"
    assert result["updated"] is True
    assert result["restart_required"] is False  # no live sessions
    assert result["blocked"] is None

    # State file should have been refreshed.
    written = json.loads(state_file.read_text())
    assert written["cli_version"] == "2.1.280"
    assert written["updated_last_check"] is True


def test_run_update_timeout_returns_error(state_file, monkeypatch):
    monkeypatch.setattr(claude_updater, "_claude_path", lambda: "/fake/claude")
    monkeypatch.setattr(claude_updater, "current_version", lambda path=None: "2.1.277")
    monkeypatch.setattr(
        claude_updater, "_run_captured",
        lambda cmd, timeout: (None, "hung"),
    )
    result = claude_updater.run_update(session_manager=None, force=False)
    assert result["ok"] is False
    assert result["error"] == "timeout"


def test_run_update_npm_fallback_triggers(state_file, monkeypatch):
    monkeypatch.setattr(claude_updater, "_claude_path", lambda: "/fake/claude")
    monkeypatch.setattr(claude_updater, "current_version",
                        lambda path=None: "2.1.277")
    calls = []

    def _fake_which(name):
        return "/fake/" + name

    def _fake_run(cmd, timeout):
        calls.append(cmd)
        if cmd[1] == "update" and cmd[0].endswith("claude"):
            return 1, "Please run: npm update -g @anthropic-ai/claude-code"
        return 0, "npm ok"

    monkeypatch.setattr(claude_updater.shutil, "which", _fake_which)
    monkeypatch.setattr(claude_updater, "_run_captured", _fake_run)

    result = claude_updater.run_update(session_manager=None, force=False)
    # Two calls: claude update, then npm update -g
    assert len(calls) == 2
    assert calls[0][1] == "update"
    assert calls[1][1] == "update"
    assert calls[1][2] == "-g"
    assert "@anthropic-ai/claude-code" in calls[1][3]
    # No version change in this scenario — but the caller can still surface it.
    assert result["updated"] is False


def test_run_update_in_progress_returns_fast(state_file, monkeypatch):
    """Concurrent second call must not run `claude update` again."""
    monkeypatch.setattr(claude_updater, "_claude_path", lambda: "/fake/claude")
    monkeypatch.setattr(claude_updater, "current_version", lambda path=None: "2.1.277")

    started = threading.Event()
    release = threading.Event()

    def _slow_run(cmd, timeout):
        started.set()
        release.wait(timeout=5)
        return 0, ""

    monkeypatch.setattr(claude_updater, "_run_captured", _slow_run)

    results = {}

    def _first():
        results["first"] = claude_updater.run_update(session_manager=None)

    t = threading.Thread(target=_first)
    t.start()
    assert started.wait(timeout=5), "first update never started"

    second = claude_updater.run_update(session_manager=None)
    assert second["blocked"] == "in_progress"

    release.set()
    t.join(timeout=5)


# ---------------------------------------------------------------------------
# Admin routes
# ---------------------------------------------------------------------------

@pytest.fixture()
def client(monkeypatch):
    from app import create_app

    app = create_app(testing=True)
    app.session_manager.get_all_states = MagicMock(return_value=[])
    return app.test_client()


def test_route_claude_status_shape(client, monkeypatch):
    monkeypatch.setattr(claude_updater, "_claude_path", lambda: "/fake/claude")
    monkeypatch.setattr(claude_updater, "current_version", lambda path=None: "2.1.277")
    r = client.get("/api/admin/claude-status")
    assert r.status_code == 200
    body = r.get_json()
    assert body["ok"] is True
    for key in ("installed", "current_version", "last_check", "stale",
                "restart_pending", "active_sessions"):
        assert key in body


def test_route_claude_update_ok(client, monkeypatch, state_file):
    monkeypatch.setattr(claude_updater, "_claude_path", lambda: "/fake/claude")
    versions = iter(["2.1.277", "2.1.280"])
    monkeypatch.setattr(claude_updater, "current_version",
                        lambda path=None: next(versions))
    monkeypatch.setattr(claude_updater, "_run_captured",
                        lambda cmd, timeout: (0, "ok"))

    r = client.post("/api/admin/claude-update",
                    json={"force": False},
                    content_type="application/json")
    assert r.status_code == 200
    body = r.get_json()
    assert body["ok"] is True
    assert body["updated"] is True


def test_route_claude_update_blocked_by_sessions(client, monkeypatch):
    monkeypatch.setattr(claude_updater, "_claude_path", lambda: "/fake/claude")
    monkeypatch.setattr(claude_updater, "current_version", lambda path=None: "2.1.277")
    from flask import current_app  # noqa: F401
    # Session manager attached to the test app must report live work.
    client.application.session_manager.get_all_states = MagicMock(
        return_value=[{"session_id": "a", "state": "working"}]
    )
    r = client.post("/api/admin/claude-update",
                    json={"force": False},
                    content_type="application/json")
    assert r.status_code == 409
    body = r.get_json()
    assert body["blocked"] == "sessions_running"


def test_route_claude_update_force_overrides_guard(client, monkeypatch, state_file):
    monkeypatch.setattr(claude_updater, "_claude_path", lambda: "/fake/claude")
    versions = iter(["2.1.277", "2.1.280"])
    monkeypatch.setattr(claude_updater, "current_version",
                        lambda path=None: next(versions))
    monkeypatch.setattr(claude_updater, "_run_captured",
                        lambda cmd, timeout: (0, "ok"))
    client.application.session_manager.get_all_states = MagicMock(
        return_value=[{"session_id": "a", "state": "working"}]
    )
    r = client.post("/api/admin/claude-update",
                    json={"force": True},
                    content_type="application/json")
    assert r.status_code == 200
    body = r.get_json()
    assert body["ok"] is True
    # Live session existed at start-of-update AND after — restart_required.
    assert body["restart_required"] is True
