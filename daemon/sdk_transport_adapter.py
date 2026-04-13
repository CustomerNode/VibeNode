"""
Transport adapter that wraps the SDK's Transport to fix wire protocol
issues without replacing SDK internal methods.

This replaces the fragile Patches 2b and 3 from the original monkey-patching
approach. Instead of replacing entire SDK methods, it wraps the Transport
at the ABC boundary — intercepting write() to reformat permission responses
and suppressing end_input() to keep stdin open for the control protocol.

See docs/plans/sdk-monkey-patching-plan.md for full context.
"""

import json
import logging
from collections.abc import AsyncIterator
from typing import Any

from claude_code_sdk._internal.transport import Transport

logger = logging.getLogger(__name__)


class VibeNodeTransportAdapter(Transport):
    """
    Wraps an SDK Transport to:
    1. Reformat permission responses from SDK format to CLI 2.x format
    2. Optionally keep stdin open for bidirectional control protocol

    The SDK sends permission responses as {"allow": True, "input": {...}}
    but CLI 2.x expects {"behavior": "allow", "updatedInput": {...}}.

    The SDK calls end_input() after streaming the prompt, which closes stdin.
    When can_use_tool is set, we need stdin open for the permission
    request/response cycle and follow-up messages.
    """

    def __init__(self, inner: Transport, *, keep_stdin_open: bool = False):
        self._inner = inner
        self._keep_stdin_open = keep_stdin_open

    # -- write: intercept permission responses and reformat ----------------

    async def write(self, data: str) -> None:
        """Intercept permission responses and reformat for CLI 2.x."""
        try:
            msg = json.loads(data.rstrip("\n"))
            if self._is_permission_response(msg):
                data = self._reformat_permission_response(msg)
        except (json.JSONDecodeError, KeyError, TypeError):
            pass  # Pass through unmodified on any parse failure

        await self._inner.write(data)

    @staticmethod
    def _is_permission_response(msg: dict) -> bool:
        """Check if a message is a permission control response."""
        if msg.get("type") != "control_response":
            return False
        response = msg.get("response", {})
        if response.get("subtype") != "success":
            return False
        inner = response.get("response", {})
        return "allow" in inner

    @staticmethod
    def _reformat_permission_response(msg: dict) -> str:
        """Convert SDK permission format to CLI 2.x format.

        SDK format:    {"allow": True, "input": {...}}
                   or  {"allow": False, "reason": "..."}

        CLI 2.x format: {"behavior": "allow", "updatedInput": {...}}
                    or  {"behavior": "deny", "message": "..."}
        """
        response = msg["response"]
        inner = response["response"]

        if inner.get("allow"):
            inner_reformatted = {
                "behavior": "allow",
                "updatedInput": inner.get("input", {}),
            }
        else:
            inner_reformatted = {
                "behavior": "deny",
                "message": inner.get("reason", "Denied"),
            }

        response["response"] = inner_reformatted
        msg["response"] = response
        return json.dumps(msg) + "\n"

    # -- end_input: conditionally suppress to keep stdin open --------------

    async def end_input(self) -> None:
        """Conditionally suppress end_input to keep stdin open."""
        if self._keep_stdin_open:
            logger.debug(
                "Suppressing end_input() — keeping stdin open for control protocol"
            )
            return
        await self._inner.end_input()

    # -- delegate everything else unchanged --------------------------------

    async def connect(self) -> None:
        return await self._inner.connect()

    def read_messages(self) -> AsyncIterator[dict[str, Any]]:
        return self._inner.read_messages()

    async def close(self) -> None:
        return await self._inner.close()

    def is_ready(self) -> bool:
        return self._inner.is_ready()

    # -- expose inner transport for error recovery -------------------------
    # VibeNode's error handling needs access to the underlying process for
    # cleanup (kill process tree on transport write failure). Expose it
    # through a property rather than forcing callers to dig through layers.

    @property
    def inner(self) -> Transport:
        """Access the underlying transport for error recovery."""
        return self._inner
