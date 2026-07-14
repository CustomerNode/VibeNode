"""
SDK monkey-patches for Claude Code SDK compatibility.

All runtime patches to the Claude Code SDK are isolated here.
Each patch has structural assertions that fail fast if the SDK
internals it depends on have changed.

Patch inventory:
  1. Safe message parser — handle unknown message types gracefully
  2. Transport adapter injection — reformat permission responses + keep stdin open
  3. Suppress console windows — prevent CLI subprocess from flashing a window (Windows)
  4. Isolate POSIX subprocesses — put each child in its own session/pgrp so
     killpg-based session stop cannot blast the daemon (Linux/macOS)
  5. Raise JSON stream buffer limit — lift the SDK's hardcoded 1MB
     per-message decode ceiling so large tool results / messages don't
     kill the stream and trigger an endless reconnect loop

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
        ("isolate_posix_subprocesses", _apply_patch_isolate_posix_subprocesses),
        ("raise_json_buffer_limit", _apply_patch_raise_json_buffer_limit),
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
        if transport is not None and not isinstance(transport, VibeNodeTransportAdapter):
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


# ═══════════════════════════════════════════════════════════════════════════
# Patch 4: Isolate POSIX subprocesses in their own session / process group
# ═══════════════════════════════════════════════════════════════════════════
#
# When a session is stopped on Linux/macOS, daemon/session_manager.py
# `_kill_process_tree()` calls
#     os.killpg(os.getpgid(cli_pid), signal.SIGTERM)   # then SIGKILL
# to terminate the Claude CLI subprocess and any descendants it spawned
# (test runners, build tools, etc.).
#
# Without isolation, the spawned CLI inherits the daemon's process group.
# run.py / app/daemon_client.py launch the daemon itself with
# start_new_session=True, making the daemon the session leader of every
# descendant — including each Claude CLI child. So
# os.getpgid(cli_pid) returns the daemon's pgid, and killpg blasts the
# daemon along with the targeted CLI. The daemon dies, every other live
# session dies with it, and the launcher restarts the daemon. Stopping
# one session takes down the whole daemon. (Windows is unaffected because
# _kill_process_tree() takes a different `taskkill /F /T /PID` branch.)
#
# Fix: inject start_new_session=True into every Popen on POSIX so each
# spawned subprocess (the SDK's Claude CLI in particular) lands in its
# own session/pgrp. killpg then targets only that subtree. This mirrors
# the isolation that run.py / app/daemon_client.py already apply when
# spawning the daemon itself.
#
# Same low-fragility seam as Patch 3 — Popen.__init__ is stable CPython
# API. Platform-gated to POSIX. Respects an explicit start_new_session
# kwarg from callers, and skips injection when the caller passed a
# preexec_fn (they're managing child setup themselves and combining the
# two would be surprising).
#
# DO NOT change this to patch anyio.open_process or wrap the SDK's
# connect(). See the warning on Patch 3 — that approach was tried and
# broke control-protocol initialization. asyncio's POSIX subprocess
# transport ultimately calls subprocess.Popen, so this patch covers the
# SDK's spawn path through the same low-fragility seam.
# ═══════════════════════════════════════════════════════════════════════════


def _apply_patch_isolate_posix_subprocesses() -> bool:
    """Patch Popen on POSIX to put each subprocess in its own session.

    Prevents `os.killpg(os.getpgid(child_pid), ...)` from blasting the
    daemon when stopping a single session. See the section header above
    for the full root-cause writeup.

    Returns True when applied, False on Windows (no-op there — see Patch 3
    for the Windows-specific Popen patch).

    POLICY (changed 2026-05-09 after debugging the Linux daemon-blast):

        We FORCE ``start_new_session=True`` whenever it is missing OR
        explicitly False.  We only opt out when the caller supplied
        ``preexec_fn`` (they're managing child setup themselves and
        stacking on top of that would be surprising).

        The earlier "respect explicit False" rule was wrong in practice.
        The SDK's spawn path is::

            anyio.open_process(...)          # signature: start_new_session=False  (default!)
              → asyncio.create_subprocess_exec(start_new_session=False, ...)
              → loop.subprocess_exec(...,    start_new_session=False)
              → _UnixSubprocessTransport._start(start_new_session=False)
              → subprocess.Popen(..., start_new_session=False)

        anyio's ``open_process()`` has ``start_new_session: bool = False``
        as a positional default (anyio/_core/_subprocesses.py line 132)
        and unconditionally forwards it to the backend.  So every CLI
        the SDK spawned arrived at our patched ``Popen.__init__`` with
        ``start_new_session=False`` already in kwargs — the old
        "respect explicit False" rule made the patch a silent no-op,
        the CLI inherited the daemon's process group, and "Stop
        Session" called ``killpg`` on the daemon's group.  This is the
        regression the user reported as "stop session blowing up the
        daemon connection on Linux."

        Reverse the rule: on POSIX we require isolation.  Anyone who
        truly needs a child in our session should set ``preexec_fn``
        and configure things themselves, or use ``os.fork`` directly.
    """
    if os.name == "nt":
        return False

    _original_init = _subprocess.Popen.__init__

    def _isolated_session_Popen_init(self: Any, *args: Any, **kwargs: Any) -> None:
        # Skip ONLY when the caller is using preexec_fn (managing child
        # setup themselves).  Otherwise force isolation regardless of
        # whether start_new_session was missing or set to False — see
        # POLICY in the docstring above for why.
        if "preexec_fn" not in kwargs or kwargs.get("preexec_fn") is None:
            kwargs["start_new_session"] = True
        return _original_init(self, *args, **kwargs)

    _subprocess.Popen.__init__ = _isolated_session_Popen_init  # type: ignore[method-assign]
    return True


# ═══════════════════════════════════════════════════════════════════════════
# Patch 5: Raise the JSON stream buffer limit
# ═══════════════════════════════════════════════════════════════════════════
#
# The SDK's subprocess transport (subprocess_cli.py) reads newline-delimited
# JSON from the Claude CLI's stdout and speculatively accumulates each line
# into `json_buffer` until it parses as a complete JSON object.  A module-level
# constant caps that buffer:
#
#     _MAX_BUFFER_SIZE = 1024 * 1024   # 1MB
#     ...
#     if len(json_buffer) > _MAX_BUFFER_SIZE:
#         json_buffer = ""
#         raise SDKJSONDecodeError("JSON message exceeded maximum buffer size ...")
#
# A SINGLE message larger than 1MB therefore kills the whole receive stream.
# This happens routinely on real sessions: one turn emits a large tool result
# (reading a big file, grepping a large log, a big command's stdout) or a large
# assistant/user message, serialized as one JSON line well over 1MB.
#
# Why this manifests as the "reconnect, then broken again" loop:
# session_manager._send_query's except handler classifies this as a
# SDKJSONDecodeError (a CLIJSONDecodeError subclass), NOT a transport error
# ("Stream closed" / "exit code" / ClosedResource / CLIConnection / Process).
# So it takes the immediate `_reconnect_client` branch instead of the
# self-healing/backoff path — and the very next turn produces another oversized
# message that busts the buffer again.  Endless fail → reconnect → fail.
#
# SDK 0.0.25 does NOT expose this limit through ClaudeCodeOptions, so we lift
# it here.  `_read_messages_impl` looks up `_MAX_BUFFER_SIZE` as a module global
# at call time, so reassigning the module attribute takes effect immediately for
# all sessions (existing and future transports).  The cap is a transient safety
# ceiling, not a preallocation — normal messages parse and reset the buffer
# instantly; only a genuinely huge (or malformed/runaway) message ever grows the
# buffer toward the ceiling.  We keep a bounded ceiling (default 64MB) rather
# than removing it entirely so a truly runaway/never-terminating JSON line still
# fails instead of consuming unbounded memory.
#
# Override with VIBENODE_SDK_MAX_BUFFER_MB (integer megabytes) if a workload
# legitimately needs more or you want to tighten it.
# ═══════════════════════════════════════════════════════════════════════════

# Default per-message JSON decode ceiling, in megabytes.  Chosen to comfortably
# cover large tool results (e.g. reading a multi-MB file) while still bounding
# memory against a runaway stream.
_DEFAULT_MAX_BUFFER_MB = 100


def _assert_patch_raise_json_buffer_limit_preconditions() -> int:
    """Verify the SDK buffer-limit seam still exists and return the new limit.

    Fails fast if the SDK renamed/removed the constant or stopped referencing
    it in the read loop — either would silently defeat this patch.
    """
    from claude_code_sdk._internal.transport import subprocess_cli as _sc

    assert hasattr(_sc, "_MAX_BUFFER_SIZE"), (
        "subprocess_cli._MAX_BUFFER_SIZE not found — SDK may have renamed or "
        "removed the JSON buffer ceiling; re-check this patch"
    )
    assert isinstance(_sc._MAX_BUFFER_SIZE, int), (
        "_MAX_BUFFER_SIZE is not an int — SDK internals changed"
    )
    # The read loop must still consult the module global (dynamic lookup) for a
    # module-attribute reassignment to take effect.  The loop lives on the
    # transport class (SubprocessCLITransport._read_messages_impl), not the
    # module namespace.
    assert hasattr(_sc, "SubprocessCLITransport"), (
        "subprocess_cli.SubprocessCLITransport not found — SDK internals changed"
    )
    read_impl = getattr(_sc.SubprocessCLITransport, "_read_messages_impl", None)
    assert read_impl is not None, (
        "SubprocessCLITransport._read_messages_impl not found — SDK internals changed"
    )
    src = inspect.getsource(read_impl)
    assert "_MAX_BUFFER_SIZE" in src, (
        "_read_messages_impl no longer references _MAX_BUFFER_SIZE — reassigning "
        "the module constant would no longer change behaviour"
    )

    # Resolve the target size (allow env override, floor at the SDK default).
    mb = _DEFAULT_MAX_BUFFER_MB
    raw = os.environ.get("VIBENODE_SDK_MAX_BUFFER_MB")
    if raw:
        try:
            parsed = int(raw)
            if parsed > 0:
                mb = parsed
        except ValueError:
            logger.warning(
                "Ignoring invalid VIBENODE_SDK_MAX_BUFFER_MB=%r (not an int)", raw
            )
    return mb * 1024 * 1024


def _apply_patch_raise_json_buffer_limit() -> bool:
    """Raise subprocess_cli._MAX_BUFFER_SIZE above the SDK's 1MB default.

    See the section header for the full root-cause writeup (oversized single
    messages kill the stream and drive an endless reconnect loop).
    """
    new_limit = _assert_patch_raise_json_buffer_limit_preconditions()

    from claude_code_sdk._internal.transport import subprocess_cli as _sc

    old_limit = _sc._MAX_BUFFER_SIZE
    if new_limit <= old_limit:
        # Never shrink below what the SDK already allows.
        logger.info(
            "JSON buffer limit already >= target (%d bytes); leaving unchanged",
            old_limit,
        )
        return False

    _sc._MAX_BUFFER_SIZE = new_limit
    logger.info(
        "Raised SDK JSON buffer limit: %d -> %d bytes (%d MB)",
        old_limit, new_limit, new_limit // (1024 * 1024),
    )
    return True
