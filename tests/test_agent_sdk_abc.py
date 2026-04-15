"""Tests for AgentSDK abstract base class, SessionOptions, and PermissionResult.

Validates the abstract interface defined in daemon/backends/base.py.
"""

import pytest
from typing import AsyncIterator, Any

from daemon.backends.base import (
    AgentSDK,
    SessionOptions,
    PermissionAction,
    PermissionResult,
)
from daemon.backends.messages import VibeNodeMessage, MessageKind


# ---------------------------------------------------------------------------
# Minimal concrete implementation for testing
# ---------------------------------------------------------------------------

class StubAgentSDK(AgentSDK):
    """Minimal concrete implementation that satisfies all abstract methods."""

    async def create_session(self, options: SessionOptions) -> Any:
        return {"stub": True, "options": options}

    async def connect(self, client: Any) -> None:
        pass

    async def send_query(self, client: Any, prompt: str) -> None:
        pass

    async def receive_response(self, client: Any) -> AsyncIterator[VibeNodeMessage]:
        yield VibeNodeMessage(kind=MessageKind.RESULT)

    async def interrupt(self, client: Any) -> None:
        pass

    async def disconnect(self, client: Any) -> None:
        pass

    def extract_process_pid(self, client: Any) -> int:
        return 0

    def is_transport_alive(self, client: Any) -> bool:
        return False


# ---------------------------------------------------------------------------
# AgentSDK cannot be instantiated directly
# ---------------------------------------------------------------------------

class TestAgentSDKAbstract:
    """Verify AgentSDK enforces its abstract contract."""

    def test_cannot_instantiate_directly(self):
        with pytest.raises(TypeError, match="abstract method"):
            AgentSDK()

    def test_missing_single_method_raises(self):
        """Omitting even one abstract method prevents instantiation."""

        class Incomplete(AgentSDK):
            async def create_session(self, options):
                return None

            async def connect(self, client):
                pass

            async def send_query(self, client, prompt):
                pass

            async def receive_response(self, client):
                yield VibeNodeMessage(kind=MessageKind.RESULT)

            async def interrupt(self, client):
                pass

            async def disconnect(self, client):
                pass

            def extract_process_pid(self, client):
                return 0

            # Missing: is_transport_alive

        with pytest.raises(TypeError, match="abstract method"):
            Incomplete()

    def test_all_abstract_methods_must_be_implemented(self):
        """Verify the set of abstract methods matches expectations."""
        abstract_methods = AgentSDK.__abstractmethods__
        expected = {
            "create_session",
            "connect",
            "send_query",
            "receive_response",
            "interrupt",
            "disconnect",
            "extract_process_pid",
            "is_transport_alive",
        }
        assert abstract_methods == expected


# ---------------------------------------------------------------------------
# Minimal concrete implementation works
# ---------------------------------------------------------------------------

class TestStubAgentSDK:
    """Verify a minimal concrete implementation can be instantiated."""

    def test_instantiation(self):
        sdk = StubAgentSDK()
        assert sdk is not None

    def test_apply_patches_default(self):
        sdk = StubAgentSDK()
        result = sdk.apply_patches()
        assert result == []

    def test_make_permission_result_allow(self):
        sdk = StubAgentSDK()
        result = sdk.make_permission_result_allow({"file_path": "/foo"})
        assert result.action == PermissionAction.ALLOW
        assert result.updated_input == {"file_path": "/foo"}
        assert result.message == ""
        assert result.interrupt is False

    def test_make_permission_result_allow_non_dict(self):
        """Non-dict input falls back to empty dict."""
        sdk = StubAgentSDK()
        result = sdk.make_permission_result_allow(None)
        assert result.updated_input == {}

    def test_make_permission_result_deny(self):
        sdk = StubAgentSDK()
        result = sdk.make_permission_result_deny(
            message="Not allowed",
            interrupt=True,
        )
        assert result.action == PermissionAction.DENY
        assert result.message == "Not allowed"
        assert result.interrupt is True

    def test_make_permission_result_deny_defaults(self):
        sdk = StubAgentSDK()
        result = sdk.make_permission_result_deny()
        assert result.action == PermissionAction.DENY
        assert result.message == "Denied"
        assert result.interrupt is False

    def test_extract_process_pid_returns_int(self):
        sdk = StubAgentSDK()
        pid = sdk.extract_process_pid(None)
        assert pid == 0
        assert isinstance(pid, int)

    def test_is_transport_alive_returns_bool(self):
        sdk = StubAgentSDK()
        alive = sdk.is_transport_alive(None)
        assert alive is False
        assert isinstance(alive, bool)


# ---------------------------------------------------------------------------
# SessionOptions
# ---------------------------------------------------------------------------

class TestSessionOptions:
    """Verify SessionOptions defaults and construction."""

    def test_defaults(self):
        opts = SessionOptions()
        assert opts.cwd is None
        assert opts.resume is None
        assert opts.model is None
        assert opts.system_prompt is None
        assert opts.max_turns is None
        assert opts.allowed_tools == []
        assert opts.permission_mode == "default"
        assert opts.include_partial_messages is True
        assert opts.extra_args == {}
        assert opts.permission_callback is None

    def test_all_fields(self):
        callback = lambda t, i, c: None
        opts = SessionOptions(
            cwd="/home/user/project",
            resume="session-abc",
            model="claude-sonnet-4-20250514",
            system_prompt="You are a helpful assistant.",
            max_turns=5,
            allowed_tools=["Edit", "Read"],
            permission_mode="acceptEdits",
            include_partial_messages=False,
            extra_args={"verbose": True},
            permission_callback=callback,
        )
        assert opts.cwd == "/home/user/project"
        assert opts.resume == "session-abc"
        assert opts.model == "claude-sonnet-4-20250514"
        assert opts.system_prompt == "You are a helpful assistant."
        assert opts.max_turns == 5
        assert opts.allowed_tools == ["Edit", "Read"]
        assert opts.permission_mode == "acceptEdits"
        assert opts.include_partial_messages is False
        assert opts.extra_args == {"verbose": True}
        assert opts.permission_callback is callback

    def test_allowed_tools_default_is_independent(self):
        """Each instance gets its own list."""
        opts1 = SessionOptions()
        opts2 = SessionOptions()
        opts1.allowed_tools.append("Bash")
        assert opts2.allowed_tools == []

    def test_extra_args_default_is_independent(self):
        """Each instance gets its own dict."""
        opts1 = SessionOptions()
        opts2 = SessionOptions()
        opts1.extra_args["key"] = "value"
        assert opts2.extra_args == {}


# ---------------------------------------------------------------------------
# PermissionAction
# ---------------------------------------------------------------------------

class TestPermissionAction:
    """Verify PermissionAction enum."""

    def test_allow_value(self):
        assert PermissionAction.ALLOW.value == "allow"

    def test_deny_value(self):
        assert PermissionAction.DENY.value == "deny"

    def test_only_two_members(self):
        assert len(PermissionAction) == 2


# ---------------------------------------------------------------------------
# PermissionResult
# ---------------------------------------------------------------------------

class TestPermissionResult:
    """Verify PermissionResult creation."""

    def test_allow_result(self):
        result = PermissionResult(
            action=PermissionAction.ALLOW,
            updated_input={"command": "ls"},
        )
        assert result.action == PermissionAction.ALLOW
        assert result.updated_input == {"command": "ls"}
        assert result.message == ""
        assert result.interrupt is False

    def test_deny_result(self):
        result = PermissionResult(
            action=PermissionAction.DENY,
            message="Tool not permitted",
            interrupt=False,
        )
        assert result.action == PermissionAction.DENY
        assert result.message == "Tool not permitted"
        assert result.interrupt is False

    def test_deny_with_interrupt(self):
        result = PermissionResult(
            action=PermissionAction.DENY,
            message="Session not found",
            interrupt=True,
        )
        assert result.interrupt is True

    def test_defaults(self):
        result = PermissionResult(action=PermissionAction.ALLOW)
        assert result.updated_input == {}
        assert result.message == ""
        assert result.interrupt is False

    def test_updated_input_default_is_independent(self):
        """Each instance gets its own dict."""
        r1 = PermissionResult(action=PermissionAction.ALLOW)
        r2 = PermissionResult(action=PermissionAction.ALLOW)
        r1.updated_input["key"] = "val"
        assert r2.updated_input == {}
