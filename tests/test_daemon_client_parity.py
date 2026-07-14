"""
IPC parity guard — DaemonClient must expose every SessionManager method the
routes call, and every RPC it sends must have a daemon-side handler.

WHY THIS EXISTS
---------------
The web process never holds the real ``SessionManager`` — it holds a
``DaemonClient`` that proxies calls over IPC to the daemon.  The subsessions
feature was written against the in-process ``SessionManager`` API, but the
proxy methods + daemon dispatch handlers were never added.  Production 500'd
with ``AttributeError: 'DaemonClient' object has no attribute
'get_subsession_meta'`` — yet every unit test passed, because
``create_app(testing=True)`` installs a ``MagicMock`` session_manager that
auto-fabricates *any* attribute.

These tests close that blind spot with pure static analysis (no daemon, no
mock): they read the route source and the two IPC source files and assert the
three-way contract holds:

  routes ──call──> DaemonClient ──_send_request──> daemon handlers dict

Discovered + added 2026-05-29.
"""

import re
from pathlib import Path

import pytest

from app.daemon_client import DaemonClient

_ROOT = Path(__file__).resolve().parents[1]
_ROUTES = _ROOT / "app" / "routes" / "sessions_api.py"
_CLIENT = _ROOT / "app" / "daemon_client.py"
_SERVER = _ROOT / "daemon" / "daemon_server.py"


def _routes_sm_calls() -> set:
    """Every ``sm.<method>(`` invoked in the sessions routes.

    ``sm`` is the local alias for ``current_app.session_manager`` throughout
    sessions_api.py.  Dunders are excluded (not part of the proxy contract).
    """
    src = _ROUTES.read_text(encoding="utf-8")
    names = set(re.findall(r"\bsm\.([A-Za-z_][A-Za-z0-9_]*)\s*\(", src))
    return {n for n in names if not n.startswith("__")}


def _client_send_request_names() -> set:
    """Every literal RPC name DaemonClient sends via _send_request(...)."""
    src = _CLIENT.read_text(encoding="utf-8")
    return set(re.findall(r"_send_request\(\s*[\"']([A-Za-z_][A-Za-z0-9_]*)[\"']", src))


def _daemon_handler_keys() -> set:
    """Every method name the daemon dispatches.

    Pulls the string keys from the ``handlers = { ... }`` dict in
    _dispatch_sync, plus the blocking-dispatch method (hook_pre_tool).
    """
    src = _SERVER.read_text(encoding="utf-8")
    m = re.search(r"handlers\s*=\s*\{(.*?)\n\s*\}", src, re.DOTALL)
    assert m, "could not locate the handlers dict in daemon_server.py"
    keys = set(re.findall(r"[\"']([A-Za-z_][A-Za-z0-9_]*)[\"']\s*:", m.group(1)))
    keys.add("hook_pre_tool")  # handled by _dispatch_blocking, not the dict
    return keys


# The subsession surface specifically (the methods that regressed).
SUBSESSION_METHODS = {
    "get_subsession_meta",
    "mark_inbox_dirty",
    "orphan_children_of",
    "detect_rewind_orphans",
    "reanchor_subsession",
    "detach_subsession",
    "set_auto_report_on_idle",
}


class TestRouteToClientParity:
    def test_every_routed_sm_method_exists_on_daemonclient(self):
        """The exact failure mode that hit production: a route calls
        sm.<method> that DaemonClient doesn't define → AttributeError."""
        missing = sorted(
            m for m in _routes_sm_calls() if not hasattr(DaemonClient, m)
        )
        assert not missing, (
            "DaemonClient is missing methods the routes call (would "
            f"AttributeError in production): {missing}"
        )

    def test_subsession_methods_are_proxied(self):
        missing = sorted(m for m in SUBSESSION_METHODS if not hasattr(DaemonClient, m))
        assert not missing, f"subsession methods not proxied on DaemonClient: {missing}"


class TestClientToDaemonParity:
    def test_every_client_rpc_has_a_daemon_handler(self):
        """Every RPC name DaemonClient sends must be dispatchable, else the
        daemon replies 'Unknown method: <name>'."""
        client_rpcs = _client_send_request_names()
        handlers = _daemon_handler_keys()
        unhandled = sorted(client_rpcs - handlers)
        assert not unhandled, (
            "DaemonClient sends RPCs the daemon has no handler for "
            f"(would error 'Unknown method'): {unhandled}"
        )

    def test_subsession_rpcs_are_handled(self):
        handlers = _daemon_handler_keys()
        missing = sorted(SUBSESSION_METHODS - handlers)
        assert not missing, f"daemon has no handler for subsession RPCs: {missing}"


class TestStartSessionForwardsSubsessionKwargs:
    def test_start_session_forwards_parent_linkage(self):
        """A regression on the second half of the bug: even with the methods
        present, start_session must forward session_type/parent_session_id/
        subsession_origin_turn so the child gets its parent pointer."""
        captured = {}

        client = DaemonClient.__new__(DaemonClient)  # no __init__/socket
        client._planner_ids = set()
        client._connected = True

        def fake_send(method, params=None, timeout=30):
            captured["method"] = method
            captured["params"] = params or {}
            return {"ok": True}

        client._send_request = fake_send
        client.start_session(
            session_id="child-1",
            cwd="/x",
            resume=True,
            session_type="subsession",
            parent_session_id="parent-1",
            subsession_origin_turn=42,
        )
        p = captured["params"]
        assert p.get("session_type") == "subsession"
        assert p.get("parent_session_id") == "parent-1"
        assert p.get("subsession_origin_turn") == 42
