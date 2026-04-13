"""
SDK monkey-patches for Claude Code SDK compatibility.

All runtime patches to the Claude Code SDK are isolated here.
Each patch has structural assertions that fail fast if the SDK
internals it depends on have changed.

Patch inventory:
  1. Safe message parser — handle unknown message types gracefully
  2. Transport adapter injection — reformat permission responses + keep stdin open
  3. Suppress console windows — prevent CLI subprocess from flashing a window (Windows)

Patches 2b and 3 from the original session_manager.py monkey-patching have been
replaced by the Transport Adapter pattern (see sdk_transport_adapter.py).

See docs/plans/sdk-monkey-patching-plan.md for full context.
"""

import inspect
import json
import logging
import os
import subprocess as _subprocess
from typing import Any

import claude_code_sdk

logger = logging.getLogger(__name__)

_SDK_VERSION = getattr(claude_code_sdk, "__version__", "0.0.0")

# Guard against double-application (e.g. tests + session_manager both import)
_patches_applied = False


# ── Helpers ──────────────────────────────────────────────────────────────


def _version_parts(v: str) -> tuple[int, ...]:
    """Parse a version string into a tuple of ints for comparison."""
    parts = []
    for p in v.split("."):
        try:
            parts.append(int(p))
        except ValueError:
            break
    return tuple(parts)


# ── Public entry point ───────────────────────────────────────────────────


def apply_patches() -> list[str]:
    """Apply all SDK patches. Call once at module load.

    Returns a list of patch names that were successfully applied.
    Idempotent — safe to call multiple times.
    """
    global _patches_applied
    if _patches_applied:
        logger.debug("SDK patches already applied, skipping")
        return []
    _patches_applied = True

    logger.info("Applying SDK patches for claude-code-sdk %s", _SDK_VERSION)

    applied: list[str] = []
    for name, fn in [
        ("safe_parse_message", _apply_patch_safe_parse_message),
        ("transport_adapter", _apply_patch_transport_adapter),
        ("suppress_console_windows", _apply_patch_suppress_console_windows),
    ]:
        try:
            if fn():
                applied.append(name)
                logger.info("Applied SDK patch: %s", name)
            else:
                logger.info("Skipped SDK patch: %s (not needed or not applicable)", name)
        except Exception as e:
            logger.warning("Failed to apply SDK patch %s: %s", name, e)

    logger.info("SDK patches complete. Applied: %s", applied)
    return applied


# ═══════════════════════════════════════════════════════════════════════════
# Patch 1: Safe Message Parser
# ═══════════════════════════════════════════════════════════════════════════
#
# The SDK raises MessageParseError for message types it doesn't recognise
# (e.g. "rate_limit_event"), which kills the entire receive_messages()
# generator.  We wrap parse_message to return None for unknown-but-valid
# messages (those that have a "type" field the SDK doesn't handle yet).
#
# The patch must be applied in TWO locations because client.py imports
# parse_message by name at module load:
#   from ._internal.message_parser import parse_message
# Patching only the source module doesn't affect the already-imported name.
# ═══════════════════════════════════════════════════════════════════════════


def _assert_patch_parse_message_preconditions() -> None:
    """Verify the SDK structures this patch depends on still exist."""
    from claude_code_sdk._internal import message_parser as mp
    from claude_code_sdk._errors import MessageParseError  # noqa: F401

    assert hasattr(mp, "parse_message"), "message_parser.parse_message not found"
    assert callable(mp.parse_message), "parse_message is not callable"

    # Verify MessageParseError has the .data attribute we rely on
    err = MessageParseError("test", data={"type": "test"})
    assert hasattr(err, "data"), "MessageParseError missing .data attribute"


def _apply_patch_safe_parse_message() -> bool:
    """Wrap parse_message to tolerate unknown message types."""
    _assert_patch_parse_message_preconditions()

    from claude_code_sdk._errors import MessageParseError
    import claude_code_sdk._internal.message_parser as _parser_mod
    import claude_code_sdk.client as _client_mod

    _original = _parser_mod.parse_message

    def _safe_parse_message(data: dict[str, Any]) -> Any:
        try:
            return _original(data)
        except MessageParseError as e:
            # Unknown-but-valid message: has a type field the SDK doesn't recognise
            if isinstance(getattr(e, "data", None), dict) and e.data.get("type"):
                logger.debug(
                    "Skipping unrecognised SDK message type: %s", e.data.get("type")
                )
                return None
            raise  # Re-raise for genuinely malformed messages

    # Apply to both locations (see docstring above)
    _parser_mod.parse_message = _safe_parse_message
    _client_mod.parse_message = _safe_parse_message
    return True


# ═══════════════════════════════════════════════════════════════════════════
# Patch 2: Transport Adapter Injection
# ═══════════════════════════════════════════════════════════════════════════
#
# Replaces the old Patches 2b (permission response format) and 3 (keep
# stdin open) with a single, thin patch that wraps the transport in a
# VibeNodeTransportAdapter after the Query is created.
#
# Instead of replacing _handle_control_request (HIGH fragility) and
# stream_input (MEDIUM-HIGH fragility), we inject a Transport wrapper
# that intercepts write() and end_input() — both public Transport ABC
# methods.  This survives SDK internal refactors as long as the
# Transport interface is stable.
#
# The injection point is Query.__init__ — we wrap it to set
# self.transport = VibeNodeTransportAdapter(self.transport, ...) after
# the original __init__ completes.
# ═══════════════════════════════════════════════════════════════════════════


def _assert_patch_transport_adapter_preconditions() -> None:
    """Verify the SDK structures this patch depends on still exist."""
    from claude_code_sdk._internal.query import Query
    from claude_code_sdk._internal.transport import Transport  # noqa: F401

    # Query.__init__ must accept 'transport' and 'can_use_tool'.
    # If our patch has already been applied, the signature will be
    # (self, *args, **kwargs) — in that case check the wrapped original.
    init_fn = Query.__init__
    wrapped = getattr(init_fn, "__wrapped__", None)
    check_fn = wrapped if wrapped is not None else init_fn

    sig = inspect.signature(check_fn)
    params = list(sig.parameters.keys())
    # If already patched, params will be ['self', 'args', 'kwargs'] — that's OK
    if "args" not in params:
        assert "transport" in params, f"Query.__init__ missing 'transport' param: {params}"
        assert "can_use_tool" in params, f"Query.__init__ missing 'can_use_tool' param: {params}"

    # Verify the permission response format we're fixing still uses {"allow": ...}
    handle_src = inspect.getsource(Query._handle_control_request)
    assert '"allow"' in handle_src, (
        "SDK may have already fixed the permission response format — "
        "check if this patch is still needed"
    )


def _apply_patch_transport_adapter() -> bool:
    """Inject VibeNodeTransportAdapter into Query after construction."""
    _assert_patch_transport_adapter_preconditions()

    from claude_code_sdk._internal.query import Query
    from daemon.sdk_transport_adapter import VibeNodeTransportAdapter

    _original_init = Query.__init__

    def _patched_init(self: Any, *args: Any, **kwargs: Any) -> None:
        _original_init(self, *args, **kwargs)
        # Wrap transport if it was set by __init__
        transport = getattr(self, "transport", None)
        if transport is not None:
            has_permission_handler = getattr(self, "can_use_tool", None) is not None
            self.transport = VibeNodeTransportAdapter(
                transport,
                keep_stdin_open=has_permission_handler,
            )
            logger.debug(
                "Injected VibeNodeTransportAdapter (keep_stdin_open=%s)",
                has_permission_handler,
            )

    Query.__init__ = _patched_init  # type: ignore[method-assign]
    return True


# ═══════════════════════════════════════════════════════════════════════════
# Patch 3: Suppress Console Windows (Windows only)
# ═══════════════════════════════════════════════════════════════════════════
#
# When the daemon is spawned with CREATE_NO_WINDOW, any subprocess it
# spawns (including the SDK's Claude CLI) would flash a console window.
# This patches Popen.__init__ to inject CREATE_NO_WINDOW automatically.
#
# This is the LOWEST fragility patch — Popen.__init__ is stable CPython
# API, the patch respects existing creationflags, and it's platform-gated.
#
# DO NOT change this to patch anyio.open_process or wrap the SDK's connect().
# That approach was tried and broke: it races when multiple sessions connect
# concurrently, and it breaks the SDK's control protocol initialization.
# ═══════════════════════════════════════════════════════════════════════════


def _apply_patch_suppress_console_windows() -> bool:
    """Patch Popen to suppress console windows on Windows."""
    if os.name != "nt":
        return False

    if not hasattr(_subprocess, "CREATE_NO_WINDOW"):
        logger.debug("CREATE_NO_WINDOW not available on this Python build")
        return False

    _original_init = _subprocess.Popen.__init__

    def _no_window_Popen_init(self: Any, *args: Any, **kwargs: Any) -> None:
        # Only inject if creationflags wasn't explicitly set
        if "creationflags" not in kwargs or kwargs["creationflags"] == 0:
            kwargs["creationflags"] = (
                kwargs.get("creationflags", 0) | _subprocess.CREATE_NO_WINDOW
            )
        return _original_init(self, *args, **kwargs)

    _subprocess.Popen.__init__ = _no_window_Popen_init  # type: ignore[method-assign]
    return True
