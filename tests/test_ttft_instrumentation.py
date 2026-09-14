"""
TTFT instrumentation unit tests.

Covers the env-gated per-turn timing helpers in ``daemon.session_manager``:

* ``_TTFT_TIMING_ENABLED = False`` (env unset) → every helper is a fast no-op:
  no dict allocated on info, no file created, no log line.
* ``_TTFT_TIMING_ENABLED = True`` (env set) → a synthetic turn produces one
  JSONL line with monotonic phase ordering and matching contextual fields.
* Turn that never reaches T4/T5 flushes with ``status="no_reply"``.

These tests DO NOT touch the daemon event loop, the SDK, or the socket
transport — they exercise the helpers directly with a stand-in namespace
object, which is enough to prove the log schema and zero-cost-when-disabled
guarantee.
"""
from __future__ import annotations

import importlib
import json
import os
import sys
import time
import types
from pathlib import Path
from unittest.mock import MagicMock

import pytest


class _FakeTrackedFiles:
    """Duck-type stand-in for daemon.session_manager._TrackedFilesLRU.

    Only ``__len__`` is exercised by the TTFT helpers.
    """
    def __init__(self, n: int = 0) -> None:
        self._n = n

    def __len__(self) -> int:  # pragma: no cover - trivial
        return self._n


class _FakeInfo:
    """Minimal SessionInfo-shaped namespace with the attributes the
    TTFT helpers read/write. Deliberately independent of the real
    SessionInfo dataclass so this test stays fast (no SDK import
    side effects) and so we can prove the helpers only touch the
    fields they claim to."""
    def __init__(self, entries=(), tracked=0, cwd="/tmp/some-project"):
        self.entries = list(entries)
        self.tracked_files = _FakeTrackedFiles(tracked)
        self.cwd = cwd
        self._ttft_turn = None
        self._ttft_turn_counter = 0
        self._ttft_await_first_emit = False
        self._ttft_pending_t0_ns = 0


def _load_sm(monkeypatch, *, enabled: bool):
    """Import daemon.session_manager fresh with VIBENODE_TIMING_TTFT set
    or unset. Uses the SDK-shim pattern from test_send_query_operation_order.py
    so the real Claude CLI SDK isn't required.
    """
    if enabled:
        monkeypatch.setenv("VIBENODE_TIMING_TTFT", "1")
    else:
        monkeypatch.delenv("VIBENODE_TIMING_TTFT", raising=False)

    sdk_mod = MagicMock()
    sdk_types_mod = MagicMock()
    for name in ("ClaudeSDKClient", "ClaudeCodeOptions"):
        setattr(sdk_mod, name, MagicMock)
    for name in (
        "AssistantMessage", "UserMessage", "ResultMessage", "StreamEvent",
        "TextBlock", "ThinkingBlock", "ToolUseBlock", "ToolResultBlock",
        "PermissionResultAllow", "PermissionResultDeny", "ContentBlock",
        "ToolPermissionContext", "Message",
    ):
        setattr(sdk_types_mod, name, MagicMock)
    monkeypatch.setitem(sys.modules, "claude_code_sdk", sdk_mod)
    monkeypatch.setitem(sys.modules, "claude_code_sdk.types", sdk_types_mod)

    import daemon.session_manager as sm
    importlib.reload(sm)
    return sm


@pytest.fixture
def tmp_log_path(tmp_path, monkeypatch):
    """Redirect the TTFT log path to a tmp file for the test."""
    target = tmp_path / "first_token_timing.jsonl"

    def _patched():
        return target

    # Applied after each _load_sm() call in the tests via monkeypatch.setattr.
    return target, _patched


# ---------------------------------------------------------------------------
# Env-var parsing — the ``bool(os.environ.get(...))`` footgun.
# ---------------------------------------------------------------------------
#
# Regression guard for the audit finding on 2026-09-11: the original code
# read the flag as ``bool(os.environ.get("VIBENODE_TIMING_TTFT"))``, which
# treats every non-empty string as True — including "0", "false", and "no".
# A user setting the flag to "0" to DISABLE the instrumentation was silently
# ENABLING it (the exact same "silent falsy default masking real state"
# family as the load-older trim-mismatch bug fixed earlier the same day).
# The daemon uses ``_parse_env_flag``; ws_events uses an inlined match.
# Both must agree that only the standard on-tokens turn instrumentation on.

@pytest.mark.parametrize("value,expected", [
    ("1", True), ("true", True), ("TRUE", True),
    ("yes", True), ("on", True), ("On", True),
    ("0", False), ("false", False), ("False", False),
    ("no", False), ("off", False),
    ("", False), (" ", False), ("bogus", False),
])
def test_ttft_env_flag_parses_on_tokens_only(monkeypatch, value, expected):
    """The daemon's `_parse_env_flag` must accept only on-tokens.

    "0", "false", "no", "", and garbage strings must all evaluate to False.
    This is the regression guard for the ``bool(os.environ.get(...))`` footgun
    that was patched 2026-09-11.
    """
    sdk_mod = MagicMock()
    sdk_types_mod = MagicMock()
    for name in ("ClaudeSDKClient", "ClaudeCodeOptions"):
        setattr(sdk_mod, name, MagicMock)
    for name in (
        "AssistantMessage", "UserMessage", "ResultMessage", "StreamEvent",
        "TextBlock", "ThinkingBlock", "ToolUseBlock", "ToolResultBlock",
        "PermissionResultAllow", "PermissionResultDeny", "ContentBlock",
        "ToolPermissionContext", "Message",
    ):
        setattr(sdk_types_mod, name, MagicMock)
    monkeypatch.setitem(sys.modules, "claude_code_sdk", sdk_mod)
    monkeypatch.setitem(sys.modules, "claude_code_sdk.types", sdk_types_mod)
    monkeypatch.setenv("VIBENODE_TIMING_TTFT", value)

    import daemon.session_manager as sm
    importlib.reload(sm)

    assert sm._parse_env_flag("VIBENODE_TIMING_TTFT") is expected, (
        f"VIBENODE_TIMING_TTFT={value!r} should parse as {expected}, "
        "but got the opposite — the bool-of-string footgun is back."
    )


def test_ws_events_env_flag_parses_on_tokens_only(monkeypatch):
    """The web-side inlined env parse must agree with the daemon's helper.

    The two live in different modules but must agree bit-for-bit — otherwise
    the server sends a T0 the daemon refuses to stash, or vice versa. This
    test reloads the ws_events module fresh for each parameter to observe
    the module-level constant.
    """
    for value, expected in [
        ("1", True), ("true", True), ("yes", True), ("on", True),
        ("0", False), ("false", False), ("no", False),
        ("", False), ("bogus", False),
    ]:
        monkeypatch.setenv("VIBENODE_TIMING_TTFT", value)
        # ws_events imports config at top; guard against a hard-to-mock
        # side effect by reloading only the module under test.
        import app.routes.ws_events as we
        importlib.reload(we)
        assert we._TTFT_TIMING_ENABLED is expected, (
            f"ws_events _TTFT_TIMING_ENABLED for {value!r}: "
            f"expected {expected}, got {we._TTFT_TIMING_ENABLED}"
        )


# ---------------------------------------------------------------------------
# Disabled path — zero side effects.
# ---------------------------------------------------------------------------

def test_helpers_are_noops_when_disabled(monkeypatch, tmp_log_path):
    target, patched = tmp_log_path
    sm = _load_sm(monkeypatch, enabled=False)
    monkeypatch.setattr(sm, "_ttft_log_path", patched)

    info = _FakeInfo(entries=list(range(10)), tracked=3)
    sm._ttft_start_turn(info)
    sm._ttft_stamp(info, "t2_perf_ns")
    sm._ttft_stamp(info, "t3_perf_ns")
    sm._ttft_note(info, "hello")
    sm._ttft_flush(info, "sid-abc", status="ok")

    # No dict ever allocated on info.
    assert info._ttft_turn is None
    # No log file created.
    assert not target.exists(), "log file must NOT be created when disabled"


# ---------------------------------------------------------------------------
# Enabled — full phase ordering.
# ---------------------------------------------------------------------------

def test_full_turn_ordering_ok(monkeypatch, tmp_log_path):
    target, patched = tmp_log_path
    sm = _load_sm(monkeypatch, enabled=True)
    monkeypatch.setattr(sm, "_ttft_log_path", patched)

    info = _FakeInfo(entries=list(range(42)), tracked=17, cwd="/w/proj-foo")

    # Simulate the server-side T0 stamp arriving via IPC.
    info._ttft_pending_t0_ns = time.time_ns() - 3_000_000  # 3 ms ago

    sm._ttft_start_turn(info)
    assert info._ttft_turn is not None
    assert info._ttft_turn["turn_no"] == 1
    assert info._ttft_turn["entry_count_pre"] == 42
    assert info._ttft_turn["tracked_files_pre"] == 17
    assert info._ttft_turn["project"] == "proj-foo"
    assert info._ttft_pending_t0_ns == 0, "pending T0 must be consumed"

    time.sleep(0.002)
    sm._ttft_stamp(info, "t2_perf_ns")
    time.sleep(0.002)
    sm._ttft_stamp(info, "t3_perf_ns")
    time.sleep(0.002)
    info._ttft_turn["t4_perf_ns"] = time.perf_counter_ns()
    info._ttft_turn["first_msg_kind"] = "assistant"
    info._ttft_turn["jsonl_bytes"] = 12_345_678
    info._ttft_await_first_emit = True
    time.sleep(0.002)
    info._ttft_turn["t5_perf_ns"] = time.perf_counter_ns()

    sm._ttft_flush(info, "sid-abc-def-ghi", status="ok")

    # After flush, the dict is cleared and the flag is off (T5 has already fired).
    assert info._ttft_turn is None
    assert info._ttft_await_first_emit is False

    # Log file exists with exactly one JSONL record.
    assert target.exists()
    lines = target.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1
    rec = json.loads(lines[0])
    assert rec["sid"] == "sid-abc-def-ghi"
    assert rec["project"] == "proj-foo"
    assert rec["turn_no"] == 1
    assert rec["entry_count_pre"] == 42
    assert rec["tracked_files_pre"] == 17
    assert rec["jsonl_bytes"] == 12_345_678
    assert rec["first_msg_kind"] == "assistant"
    assert rec["status"] == "ok"

    # Deltas are non-null and non-negative (monotonic ordering).
    for key in ("T0_T1_ms", "T1_T2_ms", "T2_T3_ms", "T3_T4_ms", "T4_T5_ms"):
        assert rec[key] is not None, f"{key} must be populated"
        assert rec[key] >= 0, f"{key} must be non-negative (got {rec[key]})"


# ---------------------------------------------------------------------------
# Enabled — turn that never emits (no reply).
# ---------------------------------------------------------------------------

def test_no_reply_flush(monkeypatch, tmp_log_path):
    target, patched = tmp_log_path
    sm = _load_sm(monkeypatch, enabled=True)
    monkeypatch.setattr(sm, "_ttft_log_path", patched)

    info = _FakeInfo(entries=[1, 2], tracked=1, cwd="/w/x")
    sm._ttft_start_turn(info)
    time.sleep(0.001)
    sm._ttft_stamp(info, "t2_perf_ns")
    time.sleep(0.001)
    sm._ttft_stamp(info, "t3_perf_ns")
    # NO T4/T5 — simulate a turn that died before any stream reply.
    sm._ttft_flush(info, "sid-empty", status="no_reply")

    rec = json.loads(target.read_text(encoding="utf-8").splitlines()[0])
    assert rec["status"] == "no_reply"
    assert rec["T1_T2_ms"] is not None
    assert rec["T2_T3_ms"] is not None
    # T3→T4 and T4→T5 must be null (T4/T5 never happened).
    assert rec["T3_T4_ms"] is None
    assert rec["T4_T5_ms"] is None

    # Second flush on the same turn is idempotent — no extra record.
    sm._ttft_flush(info, "sid-empty", status="no_reply")
    assert len(target.read_text(encoding="utf-8").splitlines()) == 1


# ---------------------------------------------------------------------------
# Enabled — drain_stale note round-trips into the record.
# ---------------------------------------------------------------------------

def test_note_appears_in_record(monkeypatch, tmp_log_path):
    target, patched = tmp_log_path
    sm = _load_sm(monkeypatch, enabled=True)
    monkeypatch.setattr(sm, "_ttft_log_path", patched)

    info = _FakeInfo()
    sm._ttft_start_turn(info)
    sm._ttft_note(info, "drain_stale_quick")
    sm._ttft_note(info, "extra-marker")
    sm._ttft_flush(info, "sid-note", status="ok")

    rec = json.loads(target.read_text(encoding="utf-8").splitlines()[0])
    assert "drain_stale_quick" in rec["notes"]
    assert "extra-marker" in rec["notes"]
