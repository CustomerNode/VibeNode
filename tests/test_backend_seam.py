"""The orchestration layer reads Claude's vocabulary through the backend seam.

``daemon/backends/base.py`` defines what the manager may ask a backend
(``BackendKnowledge``: model naming, wake-up tool, compaction marker, usage
parsing, file-history store; ``TranscriptLayout``: subagent files, task
output, API-error stops).  ``daemon/backends/claude_layout.py`` is the Claude
answer.  Above the seam, the session manager and the watchdog jobs must not
spell Claude's literals themselves: that is how the knowledge used to spread
over ~200 lines of session_manager.py and four watchdog files.

The scan covers CODE: string literals (f-string parts included) outside
docstrings.  Comments and docstrings are exempt on purpose: they document
the Claude behaviour the code handles, and that history is load-bearing for
maintainers (CLAUDE.md cites it).  Each remaining literal is listed in
``EXEMPT`` with its reason, and is pinned by count so a new one fails.
"""

import ast
import types
from pathlib import Path
from unittest.mock import MagicMock

import pytest

ROOT = Path(__file__).resolve().parents[1]

SCANNED = (
    "daemon/session_manager.py",
    "daemon/health_monitor.py",
    "daemon/orphaned_workers.py",
    "daemon/silent_wait.py",
    "daemon/limit_watch.py",
)

# Substrings that are Claude CLI vocabulary.  "--model"/"--resume"/"--effort"
# are matched as literals; a bare ".claude" literal is a path segment of the
# CLI's home directory.
TOKENS = (
    "~/.claude", ".claude/", "claude-opus", "claude-sonnet", "claude-fable",
    "ScheduleWakeup", "compact_boundary", "isApiErrorMessage",
    "--model", "--resume", "--effort", "message_start", "rate_limit_event",
)
EXACT_LITERALS = (".claude",)

# (file, token, literal) -> allowed count, with the reason.
EXEMPT = {
    ("daemon/session_manager.py", ".claude", ".claude"): (
        1, "_SKIP_DIRS: a PROJECT's own .claude/ folder (tool config, not "
           "source) is pruned from the file-tracking walk; it is a directory "
           "name in the user's repo, not the CLI's session layout"),
}


def _docstring_ids(tree):
    out = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            body = node.body
            if (body and isinstance(body[0], ast.Expr)
                    and isinstance(body[0].value, ast.Constant)
                    and isinstance(body[0].value.value, str)):
                out.add(id(body[0].value))
    return out


def code_literals(path: Path):
    """``(lineno, value)`` of every non-docstring string constant."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    docs = _docstring_ids(tree)
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str) and id(node) not in docs:
            yield node.lineno, node.value


def hits(rel: str):
    out = []
    for line, value in code_literals(ROOT / rel):
        for token in TOKENS:
            if token in value:
                out.append((rel, token, value, line))
        for token in EXACT_LITERALS:
            if value == token:
                out.append((rel, token, value, line))
    return out


@pytest.mark.parametrize("rel", SCANNED)
def test_no_claude_literals_above_the_seam(rel):
    counts = {}
    unexpected = []
    for f, token, value, line in hits(rel):
        key = (f, token, value)
        if key in EXEMPT:
            counts[key] = counts.get(key, 0) + 1
        else:
            unexpected.append(f"{f}:{line} [{token}] {value[:80]!r}")
    for key, n in counts.items():
        allowed, why = EXEMPT[key]
        assert n <= allowed, f"{key} appears {n}x, exemption allows {allowed} ({why})"
    assert not unexpected, (
        "Claude CLI vocabulary spelled above the backend seam; read it from "
        "self._BACKEND / the TranscriptLayout instead "
        "(daemon/backends/claude_layout.py):\n  " + "\n  ".join(unexpected))


def test_exemptions_are_still_needed():
    """A stale exemption would quietly allow a future regression."""
    seen = {(f, t, v) for rel in SCANNED for f, t, v, _ in hits(rel)}
    stale = [k for k in EXEMPT if k not in seen]
    assert not stale, f"remove stale exemptions: {stale}"


def test_the_scan_sees_code_but_not_docstrings(tmp_path):
    f = tmp_path / "x.py"
    f.write_text('def f():\n    """compact_boundary arrives late."""\n'
                 '    # compact_boundary here too\n'
                 '    return x == "compact_boundary"\n', encoding="utf-8")
    assert [v for _, v in code_literals(f)] == ["compact_boundary"]


def test_the_knowledge_lives_in_the_claude_backend():
    """Guard the guard: the vocabulary moved, it did not vanish."""
    values = " ".join(v for _, v in code_literals(ROOT / "daemon/backends/claude_layout.py"))
    for token in ("ScheduleWakeup", "compact_boundary", "isApiErrorMessage",
                  "message_start", "rate_limit_event", "file-history"):
        assert token in values, token


# ── Behaviour of the Claude knowledge (moved verbatim; pinned here) ───────

from daemon.backends.base import AgentSDK, BackendKnowledge, TranscriptLayout  # noqa: E402
from daemon.backends.claude_layout import (  # noqa: E402
    CLAUDE_KNOWLEDGE as K, ClaudeKnowledge, ClaudeTranscriptLayout,
)


def test_stream_usage_reads_both_sdk_shapes():
    usage = {"input_tokens": 5, "cache_read_input_tokens": 7}
    current = {"event": {"type": "message_start", "message": {"usage": usage}}, "data": {}}
    older = {"event": "message_start", "data": {"type": "message_start",
                                                "message": {"usage": usage}}}
    for shape in (current, older):
        r = K.stream_usage(shape)
        assert r["usage_start"] is True and r["usage"] == usage and r["type"] == "message_start"
    other = K.stream_usage({"event": {"type": "content_block_delta"}, "data": {}})
    assert other["usage_start"] is False and other["usage"] is None
    assert K.stream_usage({})["usage"] is None


def test_wakeup_compaction_and_limit_switch_vocabulary():
    assert "ScheduleWakeup" in K.wakeup_tool_names
    assert K.compaction_marker == "compact_boundary"
    assert K.rate_limit_event_name == "rate_limit_event"
    assert K.limit_switch_families == {"fable": "opus"}
    assert "<system-reminder>" in K.system_markers


def test_cli_model_id_and_newest_model_of():
    assert K.cli_model_id("claude-opus-5[1m]") == "claude-opus-5[1m]"
    assert K.cli_model_id("claude-haiku-4-5[1m]") == "claude-haiku-4-5"
    assert K.cli_model_id("claude-opus-5[beta][1m]") == "claude-opus-5[1m]"
    listed = [{"value": "opus", "resolvedModel": "claude-opus-5-5"}]
    never = lambda: pytest.fail("known_ids must not be read when the CLI answered")  # noqa: E731
    assert K.newest_model_of("opus", listed, never) == "claude-opus-5-5"
    assert K.newest_model_of("opus", listed, never, "claude-fable-5-1[1m]") == "claude-opus-5-5[1m]"
    assert K.newest_model_of("opus", [], lambda: ["claude-opus-5", "claude-opus-6"]) == "claude-opus-6"


def test_transcript_layout(tmp_path):
    L = ClaudeTranscriptLayout()
    root = tmp_path / "proj" / "sid.jsonl"
    assert L.subagents_dir(root) == tmp_path / "proj" / "sid" / "subagents"
    assert L.subagent_meta_id("agent-a1.meta.json") == "a1"
    assert L.subagent_meta_id("agent-a1.jsonl") is None
    assert L.subagent_meta_id("Agent-a1.meta.json") is None      # case-sensitive, as before
    assert L.subagent_transcript(tmp_path, "a1") == tmp_path / "agent-a1.jsonl"
    assert L.is_api_error_entry({"type": "assistant", "isApiErrorMessage": True,
                                 "error": "rate_limit"}) == "rate_limit"
    assert L.is_api_error_entry({"type": "assistant", "isApiErrorMessage": True}) == ""
    assert L.is_api_error_entry({"type": "assistant"}) is None
    assert L.is_api_error_entry({"type": "user", "isApiErrorMessage": True}) is None
    assert K.file_history_dir(tmp_path) == tmp_path / ".claude" / "file-history"


def test_a_backend_that_knows_nothing_switches_and_parses_nothing():
    B = BackendKnowledge()
    assert B.model_family("claude-opus-5") == "" and B.limit_switch_families == {}
    assert B.stream_usage({"event": "message_start"})["usage"] is None
    assert TranscriptLayout().is_api_error_entry({"isApiErrorMessage": True}) is None


def test_claude_sdk_exposes_its_knowledge_and_layout():
    from daemon.backends.claude import ClaudeAgentSDK
    sdk = ClaudeAgentSDK()
    assert isinstance(sdk.knowledge, ClaudeKnowledge)
    assert isinstance(sdk.transcript_layout(), ClaudeTranscriptLayout)


def test_session_manager_reads_the_seam():
    from daemon.session_manager import SessionManager
    assert SessionManager._BACKEND is K
    assert SessionManager._WAKEUP_TOOL_NAMES == K.wakeup_tool_names
    assert SessionManager._LIMIT_SWITCH_FAMILIES == {"fable": "opus"}
    # A test double backend (MagicMock) still gets a real layout.
    sm = SessionManager(sdk=MagicMock(), store=MagicMock())
    assert isinstance(sm.backend_layout, ClaudeTranscriptLayout)


def test_watchdog_works_against_a_session_manager_without_backend_layout():
    """Hot reload pairs new watchdog code with an older SessionManager."""
    from daemon import health_monitor as hm
    old_sm = types.SimpleNamespace(_lock=None, _sessions={}, _health_monitor=None)
    mon = hm.HealthMonitor.__new__(hm.HealthMonitor)
    mon._sm = old_sm
    assert isinstance(mon._layout(), ClaudeTranscriptLayout)
    old_sm.backend_layout = MagicMock()          # not a TranscriptLayout
    assert isinstance(mon._layout(), ClaudeTranscriptLayout)
