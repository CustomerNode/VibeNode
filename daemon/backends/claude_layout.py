"""What VibeNode knows about the Claude Code CLI's vocabulary and disk layout.

The Claude half of the backend seam (``daemon/backends/base.py``:
``BackendKnowledge`` and ``TranscriptLayout``).  Everything the session
manager and the watchdog jobs used to hardcode about Claude (model id
grammar, the wake-up tool, the compaction marker, the usage-carrying stream
event, the file-history store, the subagent and task-output folders, the
API-error stop entry) is spelled here and nowhere above the seam
(``tests/test_backend_seam.py``).

Deliberately free of ``claude_code_sdk`` and of ``common/``: the watchdog
imports it, the watchdog hot-reloads, and its reload test runs against a copy
of the ``daemon`` package alone.  Every member is pure and cheap.
"""

import fnmatch
import os
import re
import tempfile
from pathlib import Path

try:
    from daemon.backends.base import BackendKnowledge, TranscriptLayout
except ImportError:
    # The watchdog hot-reloads into a RUNNING engine whose already-imported
    # daemon.backends.base predates these contracts.  Without this fallback
    # every watchdog reload would fail until the engine restarts.  Watchdog
    # code reaches the layout through this module, so it stays consistent.
    class BackendKnowledge:  # type: ignore[no-redef]
        pass

    class TranscriptLayout:  # type: ignore[no-redef]
        pass

_MODEL_ID_RE = re.compile(r"claude-([a-z]+)-(\d+)(?:-(\d+))?$")
MODEL_FAMILIES = ("fable", "opus", "sonnet", "haiku")


class ClaudeKnowledge(BackendKnowledge):
    """The Claude CLI's vocabulary (see ``BackendKnowledge``)."""

    # ScheduleWakeup is the CLI's explicit wake-up tool.  The manager also
    # matches renamed variants by substring (``_WAKEUP_TOOL_SUBSTRINGS``).
    wakeup_tool_names = frozenset({"ScheduleWakeup"})
    # SystemMessage subtype the CLI sends AFTER a compaction completes.
    compaction_marker = "compact_boundary"
    # Stream-JSON event with the account usage windows (sunk by
    # daemon/sdk_patches.py into SessionManager._on_rate_limit_info).
    rate_limit_event_name = "rate_limit_event"
    # A usage limit that binds Fable continues on the newest Opus.  Keyed by
    # family, never by id, so every future Fable / Opus version is covered.
    limit_switch_families = {"fable": "opus"}

    @property
    def system_markers(self) -> tuple:
        # Shared with the web's renderer; imported lazily so this module
        # stays importable from a bare copy of the daemon package.
        from common.transcripts import CLI_SYSTEM_MARKERS
        return CLI_SYSTEM_MARKERS

    # ── Model naming ──────────────────────────────────────────────────

    def strip_markers(self, model_id: str) -> str:
        """'claude-opus-5-5[1m]' -> 'claude-opus-5-5'; 'opus[1m]' -> 'opus'."""
        return re.sub(r"\[[^\]]*\]", "", str(model_id or "")).strip()

    def _match(self, model_id: str):
        """Match a claude id after dropping marker suffixes ("[1m]") and a
        date snapshot suffix: 'claude-opus-4-20250514' is Opus 4, not Opus
        4.20250514."""
        return _MODEL_ID_RE.match(re.sub(r"-\d{8}$", "", self.strip_markers(model_id)))

    def model_family(self, model_id: str) -> str:
        """'claude-fable-5-1[1m]' -> 'fable'; a family alias ('opus',
        'opus[1m]') -> itself; '' for anything else."""
        m = self._match(model_id)
        if m:
            return m.group(1)
        bare = self.strip_markers(model_id).lower()
        return bare if bare in MODEL_FAMILIES else ""

    def model_version(self, model_id: str) -> tuple:
        """(major, minor) of a claude id: 'claude-opus-5-5' -> (5, 5),
        'claude-opus-5' -> (5, 0), 'claude-haiku-4-5-20251001' -> (4, 5),
        'claude-opus-4-20250514' -> (4, 0).  () for anything else."""
        m = self._match(model_id)
        return (int(m.group(2)), int(m.group(3) or 0)) if m else ()

    def model_label(self, model_id: str) -> str:
        """'claude-opus-5-5[1m]' -> 'Opus 5.5', the picker's naming.  A family
        alias ('opus') reads 'Opus'."""
        m = self._match(model_id)
        if not m:
            return self.strip_markers(model_id).capitalize() or "the model"
        return "%s %s%s" % (m.group(1).capitalize(), m.group(2),
                            "." + m.group(3) if m.group(3) else "")

    def newest_in_family(self, model_ids, family: str) -> str:
        """The highest version among the concrete ``model_ids`` of ``family``,
        or ''.  Marker suffixes ("[1m]") are dropped; a dated snapshot loses a
        tie to its clean alias; family aliases are skipped."""
        best, best_key = "", None
        for raw in model_ids or ():
            mid = self.strip_markers(raw)
            if not mid.startswith("claude-") or self.model_family(mid) != family:
                continue
            key = (self.model_version(mid), not re.search(r"-\d{8}$", mid))
            if best_key is None or key > best_key:
                best, best_key = mid, key
        return best

    def cli_model_id(self, model) -> str:
        """Turn a recorded/confirmed model id into one safe to hand the CLI.

        The CLI reports ``[1m]`` on the id when 1M context is active, and that
        id flows into ``info.model`` and the registry.  ``[1m]`` IS a valid
        ``--model`` / ``set_model`` value for Opus and Sonnet (verified
        2026-10-05 against every model: launch and live switch both come up
        in 1M mode), so it is KEPT: stripping it silently dropped a 1M
        session back to 200K on every wake.  Two exceptions are stripped:
        any other bracketed marker, and ``[1m]`` on Haiku, which the API
        rejects with a 400 ("long context beta is not yet available").
        Mirrors ``SessionModel._cleanId`` on the client.
        """
        m = (model or "").strip()
        m = re.sub(r"\[(?!1m\])[^\]]*\]", "", m)
        if "haiku" in m:
            m = m.replace("[1m]", "")
        return m

    def newest_model_of(self, family: str, listed, known_ids, limited_model: str = "") -> str:
        """The newest model of ``family`` to continue on, or ''.

        From, in order: (1) the session CLI's own family alias resolved
        (``opus`` -> its newest Opus; an alias listed without a resolved id is
        returned as the alias itself, the CLI resolves it); (2) the highest
        version of the family that CLI lists; (3) ``known_ids()``, VibeNode's
        own list, called only when (1) and (2) found nothing.  A 1M-context
        session (``[1m]`` on ``limited_model``) keeps 1M, alias included
        (CLI 2.1.291 accepts ``opus[1m]``, probed 2026-10-10).
        """
        if not isinstance(listed, list):
            listed = []
        target = ""
        for m in listed:
            if isinstance(m, dict) and m.get("value") == family:
                resolved = str(m.get("resolvedModel") or "")
                target = resolved if self.model_family(resolved) == family else family
                break
        if not target:
            target = self.newest_in_family(
                [m.get("resolvedModel") or m.get("value") for m in listed if isinstance(m, dict)],
                family)
        if not target:
            target = self.newest_in_family(known_ids(), family)
        if self.model_family(target):
            target = self.strip_markers(target)
            if "[1m]" in (limited_model or ""):
                target = self.cli_model_id(target + "[1m]")
        return target

    # ── Stream / disk ─────────────────────────────────────────────────

    def stream_usage(self, event_data: dict) -> dict:
        """Read a raw StreamEvent: the per-call usage rides ``message_start``.

        Two SDK shapes are seen.  Older: ``event`` is the STRING
        "message_start" and ``data`` the payload dict.  Current (2026-10-05):
        ``event`` IS the payload dict (``{'type': 'message_start',
        'message': {...}}``) and ``data`` is empty.  Both are normalized.
        """
        event_data = event_data or {}
        evt_type = event_data.get('event', '')
        evt_data = event_data.get('data') or {}
        if isinstance(evt_type, dict):
            if not (isinstance(evt_data, dict) and evt_data):
                evt_data = evt_type
            evt_type = evt_type.get('type', '')
        usage_start = (
            evt_type == 'message_start'
            or (isinstance(evt_data, dict) and evt_data.get('type') == 'message_start')
        )
        usage = None
        if usage_start and isinstance(evt_data, dict):
            msg = evt_data.get('message', {})
            if isinstance(msg, dict) and isinstance(msg.get('usage'), dict):
                usage = msg['usage']
        return {"type": evt_type, "payload": evt_data,
                "usage_start": usage_start, "usage": usage}

    def file_history_dir(self, home) -> Path:
        """``<home>/.claude/file-history``: per-session file backups (the
        CLI's rewind store, which VibeNode writes into too).  ``home`` is
        passed in so callers (and their tests) control ``Path.home()``."""
        return Path(home) / ".claude" / "file-history"


class ClaudeTranscriptLayout(TranscriptLayout):
    """The Claude CLI's on-disk session layout (see ``TranscriptLayout``).

    * ``<project>/<sid>/subagents/agent-<id>.meta.json`` and
      ``agent-<id>.jsonl``: one pair per agent the session launched.
    * ``<tempdir>/claude[-<uid>]/<project>/<sid>/tasks/<id>.output``:
      background-task output files.
    * An API-error stop is an ``assistant`` entry with
      ``isApiErrorMessage: true`` and ``error`` (e.g. ``rate_limit``).
    """

    subagent_meta_glob = "agent-*.meta.json"

    def subagents_dir(self, root_transcript) -> Path:
        root = Path(root_transcript)
        return root.parent / root.stem / "subagents"

    def subagent_meta_id(self, filename: str):
        """``agent-<id>.meta.json`` -> ``<id>``; None for any other file."""
        if not fnmatch.fnmatchcase(filename, self.subagent_meta_glob):
            return None
        return filename[len("agent-"):-len(".meta.json")]

    def subagent_transcript(self, sub_dir, agent_id: str) -> Path:
        return Path(sub_dir) / f"agent-{agent_id}.jsonl"

    def is_api_error_entry(self, entry):
        if (isinstance(entry, dict) and entry.get("type") == "assistant"
                and entry.get("isApiErrorMessage")):
            return entry.get("error") or ""
        return None

    def task_output_dir(self, root_transcript) -> Path:
        """Where the CLI writes this session's background-task output files.

        ``<tempdir>/claude/<project dir>/<session id>/tasks`` on Windows, and
        ``<tempdir>/claude-<uid>/...`` on Linux and macOS (seen 2026-10-20:
        ``/tmp/claude-1000/-home-.../<sid>/tasks/<id>.output``).  This is the
        directory of every ``<output-file>`` path in the CLI's task
        notifications.  The per-user name is preferred when it exists.
        """
        root = Path(root_transcript)
        tmp = Path(tempfile.gettempdir())
        names = ["claude"]
        getuid = getattr(os, "getuid", None)
        if getuid is not None:
            names.insert(0, f"claude-{getuid()}")
        candidates = [tmp / name / root.parent.name / root.stem / "tasks" for name in names]
        for candidate in candidates:
            if candidate.is_dir():
                return candidate
        return candidates[-1]


CLAUDE_KNOWLEDGE = ClaudeKnowledge()
