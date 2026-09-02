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

import ast
import inspect
import re
from pathlib import Path

import pytest

from app.daemon_client import DaemonClient

_ROOT = Path(__file__).resolve().parents[1]
_ROUTES = _ROOT / "app" / "routes" / "sessions_api.py"
_WS_EVENTS = _ROOT / "app" / "routes" / "ws_events.py"
_CLIENT = _ROOT / "app" / "daemon_client.py"
_SERVER = _ROOT / "daemon" / "daemon_server.py"

# Every route module that calls through the ``sm`` proxy alias.
_PROXY_CALLERS = (_ROUTES, _WS_EVENTS)


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


def _routes_sm_call_kwargs():
    """Every ``sm.<method>(..., <kw>=...)`` keyword passed from the routes.

    Yields ``(module, lineno, method, kwarg)``.  Name-level parity is not
    enough: ``set_session_model`` existed on the proxy but did not accept the
    ``resume_turn`` kwarg the socket handler started passing, so EVERY model
    switch raised ``TypeError: ... got an unexpected keyword argument
    'resume_turn'`` and the handler's ``except Exception`` turned it into a
    "Model switch failed" toast.  (Shipped 2026-08-31, found 2026-09-02.)
    """
    for path in _PROXY_CALLERS:
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not (isinstance(node, ast.Call)
                    and isinstance(node.func, ast.Attribute)):
                continue
            base = node.func.value
            basename = getattr(base, "id", None) or getattr(base, "attr", None)
            if basename not in ("sm", "session_manager"):
                continue
            for kw in node.keywords:
                if kw.arg:
                    yield path.name, node.lineno, node.func.attr, kw.arg


class TestRouteToClientSignatureParity:
    def test_every_kwarg_the_routes_pass_is_accepted_by_the_proxy(self):
        """A proxy method that EXISTS can still reject the call.  The routes
        run against DaemonClient in production, so any keyword they pass must
        be in its signature (or absorbed by **kwargs)."""
        bad = []
        for module, lineno, method, kwarg in _routes_sm_call_kwargs():
            fn = getattr(DaemonClient, method, None)
            if fn is None:
                continue  # covered by the name-parity test above
            params = inspect.signature(fn).parameters
            if any(p.kind is inspect.Parameter.VAR_KEYWORD
                   for p in params.values()):
                continue
            if kwarg not in params:
                bad.append(f"{module}:{lineno} "
                           f"DaemonClient.{method}() has no '{kwarg}' param")
        assert not bad, (
            "routes pass keywords the IPC proxy does not accept (TypeError in "
            "production, surfaced to the user as a failure toast):\n  "
            + "\n  ".join(sorted(bad))
        )


class TestSetSessionModelResumeTurn:
    """The model-switch proxy specifically."""

    @staticmethod
    def _client(reply=None):
        client = DaemonClient.__new__(DaemonClient)  # no __init__/socket
        client._planner_ids = set()
        client._connected = True
        sent = []

        def fake_send(method, params=None, timeout=30):
            sent.append((method, params or {}))
            if callable(reply):
                return reply(len(sent))
            return reply if reply is not None else {"ok": True}

        client._send_request = fake_send
        return client, sent

    def test_plain_switch_omits_resume_turn(self):
        """The model badge never asks to spend a turn, and a daemon started
        before the param existed rejects unknown keys outright — so the common
        path must not send it at all."""
        client, sent = self._client()
        client.set_session_model("s1", "claude-sonnet-4-6")
        assert sent == [("set_session_model",
                         {"session_id": "s1", "model": "claude-sonnet-4-6"})]

    def test_cta_forwards_resume_turn(self):
        client, sent = self._client()
        client.set_session_model("s1", "claude-sonnet-4-6", resume_turn=True)
        assert sent[0][1].get("resume_turn") is True

    def test_old_daemon_rejecting_resume_turn_falls_back_to_plain_switch(self):
        """An old in-flight daemon replies with the TypeError text.  Switching
        the model is the part the user asked for, so retry without the param
        rather than failing the whole operation."""
        err = ("set_session_model() got an unexpected keyword argument "
               "'resume_turn'")
        client, sent = self._client(
            reply=lambda n: {"ok": False, "error": err} if n == 1
            else {"ok": True, "model": "claude-sonnet-4-6"}
        )
        result = client.set_session_model("s1", "claude-sonnet-4-6",
                                          resume_turn=True)
        assert len(sent) == 2
        assert "resume_turn" not in sent[1][1]
        assert result["ok"] is True
        # Honesty: the turn did NOT resume, and the payload must not claim it.
        assert not result.get("turn_resumed")

    def test_genuine_failure_is_not_retried(self):
        """Only the unknown-param rejection gets a second attempt — a real
        rejection (bad model id) must surface as-is, not be tried twice."""
        client, sent = self._client(
            reply={"ok": False, "error": "Model switch rejected: no such model"}
        )
        result = client.set_session_model("s1", "bogus", resume_turn=True)
        assert len(sent) == 1
        assert result["ok"] is False


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
