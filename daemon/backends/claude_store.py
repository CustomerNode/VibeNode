"""Claude Code JSONL implementation of ChatStore.

Wraps all JSONL-specific session file operations.  This is the concrete
implementation of ``ChatStore`` for Claude Code's native session format
(one JSON object per line, stored under ``~/.claude/projects/``).

Phase 2 of the OOP abstraction refactor moved the following code here:

- ``_find_session_jsonl`` (session_manager.py L3326-3348)
- ``_repair_incomplete_jsonl`` (session_manager.py L254-302)
- JSONL scan from ``_prepopulate_tracked_files`` (L3072-3131)
- JSONL tail read from ``_write_file_snapshot`` (L3262-3286)
- JSONL append for file-history-snapshot (L3313-3314)

Methods that delegate to ``app/sessions.py`` for backward compatibility:

- ``load_summary`` -> ``app.sessions.load_session_summary``
- ``read_entries`` -> ``app.sessions.load_session``
"""

import json
import logging
from pathlib import Path
from typing import Optional

from daemon.backends.chat_store import ChatStore

logger = logging.getLogger(__name__)

# Text appended to a force-completed assistant turn so the model (and the
# user) can see that the previous response was cut short.
_INTERRUPTION_NOTICE = "\n\n[Session interrupted — resuming from last checkpoint]"

# Block types that must NOT survive when we force-complete an *interrupted*
# trailing turn (stop_reason=null), because replaying them through ``--resume``
# makes the Anthropic API reject the request:
#   • thinking / redacted_thinking — an interrupted turn's thinking is
#     partial/unsigned, and we mutate the message by appending the notice; the
#     API forbids modifying thinking blocks of the latest assistant message.
#   • tool_use — an interrupted turn never produced the matching tool_result,
#     so a dangling tool_use triggers "tool_use ids were found without
#     tool_result blocks" on resume.
_INTERRUPTED_TURN_DROP_TYPES = {"thinking", "redacted_thinking", "tool_use"}


def _is_unreplayable_thinking(block) -> bool:
    """Return True for a thinking block the Anthropic API will reject on resume.

    The Claude CLI persists assistant thinking blocks to the session JSONL
    with their cryptographic ``signature`` but with the ``thinking`` text
    field EMPTY (observed on heavily-resumed sessions — the text is redacted on
    disk while the signature is kept).  When ``--resume`` replays such a block,
    the signature no longer matches the (now empty) text and the request 400s:

        thinking or redacted_thinking blocks in the latest assistant message
        cannot be modified. These blocks must remain as they were in the
        original response.

    A block is unreplayable when it is a ``thinking`` block whose text is
    empty, or a ``redacted_thinking`` block whose ``data`` is empty.  A normal,
    intact thinking block (non-empty text + signature) is replayable and is
    left untouched.
    """
    if not isinstance(block, dict):
        return False
    t = block.get("type")
    if t == "thinking":
        return not str(block.get("thinking", "") or "").strip()
    if t == "redacted_thinking":
        return not str(block.get("data", "") or "").strip()
    return False


class ClaudeJsonlStore(ChatStore):
    """Claude Code JSONL implementation of ChatStore.

    Reads and writes Claude Code's native ``.jsonl`` session format.
    All JSONL-specific parsing logic lives here so that
    ``session_manager.py`` never touches JSONL internals directly.
    """

    # ── Session Discovery ────────────────────────────────────────────

    def find_session_path(
        self, session_id: str, cwd: str = ""
    ) -> Optional[Path]:
        """Locate the .jsonl file for a session on disk.

        Moved from session_manager.py L3326-3348 (_find_session_jsonl).
        """
        from app.config import _encode_cwd
        projects_dir = Path.home() / ".claude" / "projects"

        # Try the encoded cwd first (fastest path)
        if cwd:
            encoded = _encode_cwd(cwd)
            candidate = projects_dir / encoded / f"{session_id}.jsonl"
            if candidate.exists():
                return candidate

        # Fallback: scan project directories
        if projects_dir.is_dir():
            for d in projects_dir.iterdir():
                if d.is_dir() and not d.name.startswith("subagents"):
                    candidate = d / f"{session_id}.jsonl"
                    if candidate.exists():
                        return candidate
        return None

    # ── Read Operations ──────────────────────────────────────────────

    def read_tracked_files(
        self, session_id: str, cwd: str = ""
    ) -> tuple:
        """Scan session JSONL for tracked files and metadata.

        Moved from session_manager.py L3072-3131 (scan logic from
        ``_prepopulate_tracked_files``).

        PERF-CRITICAL: tracked_files snowball-on-resume — see CLAUDE.md #7.

        ``found`` is populated ONLY from Source 1 (Edit/Write/MultiEdit/
        NotebookEdit ``tool_use`` blocks).  Source 2 (file-history-snapshot
        ``trackedFileBackups`` dicts) is consulted ONLY for ``max_version``
        bookkeeping — it must NOT contribute to ``found``.

        Why: every post-turn snapshot's ``trackedFileBackups`` dict can
        contain ``fs_snapshot_extras`` (files changed by Bash/Agent that
        were NEVER directly edited via a tool_use, captured per CLAUDE.md
        #7 as snapshot-only).  If we re-feed those into ``found``, the
        in-memory ``info.tracked_files`` snowballs back to thousands of
        entries on the next ``_prepopulate_tracked_files`` call (which
        happens whenever a fresh ``SessionInfo`` is created — recovery,
        daemon restart, resume).  The next pre-turn ``_write_file_snapshot``
        then re-reads + MD5-hashes every one of those files, producing
        130-380 s pre-turn waits on Windows with Defender real-time scan.

        Source 1 alone is the canonical truth: anything the CLI or daemon
        edited via a real tool_use is captured there.  Files that were
        only filesystem-detected changes were intentionally kept out of
        ``tracked_files`` and must stay out across resume.

        Returns:
            (tracked_files: set, file_versions: dict,
             last_user_uuid: str, last_asst_uuid: str)
        """
        jsonl_path = self.find_session_path(session_id, cwd)
        if not jsonl_path or not jsonl_path.exists():
            return set(), {}, "", ""

        # The set of tool names whose file_path/path input indicates a tracked
        # file.  These are the tools that create or modify files -- their
        # targets need file-history snapshots for the rewind feature.
        edit_tools = {'Edit', 'Write', 'MultiEdit', 'NotebookEdit'}
        found = set()
        max_version = {}
        last_user_uuid = ""
        last_asst_uuid = ""

        try:
            # UTF-8 with errors="replace" prevents crashes on sessions that
            # contain binary data or truncated multi-byte sequences (which
            # can happen if the daemon was killed mid-write).
            with open(jsonl_path, "r", encoding="utf-8", errors="replace") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        obj = json.loads(line)
                    except Exception:
                        continue

                    t = obj.get("type", "")

                    # Cache user/assistant UUIDs as we scan.  We overwrite on
                    # each occurrence so at the end of the scan we have the
                    # LAST user and assistant UUIDs -- needed for inserting
                    # file-history-snapshot entries at the correct position
                    # in the JSONL conversation flow.
                    uid = obj.get("uuid", "")
                    if uid:
                        if t == "user":
                            last_user_uuid = uid
                        elif t == "assistant":
                            last_asst_uuid = uid

                    # Source 1: tool_use blocks in assistant messages.
                    # This is the ONLY source that contributes to ``found``.
                    if t == "assistant":
                        msg = obj.get("message", {})
                        content = msg.get("content", [])
                        if not isinstance(content, list):
                            continue
                        for block in content:
                            if not isinstance(block, dict):
                                continue
                            if block.get("type") != "tool_use":
                                continue
                            if block.get("name") not in edit_tools:
                                continue
                            inp = block.get("input", {})
                            fp = inp.get("file_path", "") or inp.get("path", "")
                            if fp:
                                found.add(fp)

                    # Source 2: existing file-history-snapshot entries —
                    # used ONLY to recover version counters so newly written
                    # backup file names don't collide.  Files in this dict
                    # are NOT added to ``found`` (snowball prevention; see
                    # docstring + CLAUDE.md #7).
                    elif t == "file-history-snapshot":
                        snap = obj.get("snapshot", {})
                        for fp, binfo in snap.get("trackedFileBackups", {}).items():
                            if isinstance(binfo, dict):
                                v = binfo.get("version", 0)
                                if v > max_version.get(fp, 0):
                                    max_version[fp] = v
        except Exception as e:
            logger.warning("read_tracked_files failed for %s: %s", session_id, e)

        return found, max_version, last_user_uuid, last_asst_uuid

    def read_tail_uuids(
        self, session_id: str, cwd: str = ""
    ) -> tuple:
        """Read the last user and assistant UUIDs from the session tail.

        Moved from session_manager.py L3262-3286 (inline in
        ``_write_file_snapshot``).  Reads only the last 64KB for
        performance.

        Returns:
            (last_user_uuid: str, last_asst_uuid: str)
        """
        jsonl_path = self.find_session_path(session_id, cwd)
        if not jsonl_path or not jsonl_path.exists():
            return "", ""

        last_user_uuid = ""
        last_asst_uuid = ""
        try:
            file_size = jsonl_path.stat().st_size
            # Read only the last 64KB of the file instead of the entire thing.
            # Session JSONL files can grow to tens of megabytes.  The UUIDs
            # we need are in the most recent entries, so reading the tail is
            # sufficient and avoids a full-file scan that would add latency
            # to every turn.  64KB is enough to contain several complete
            # JSON lines even for large tool results.
            tail_size = min(file_size, 65536)
            with open(jsonl_path, "rb") as rf:
                rf.seek(max(0, file_size - tail_size))
                tail = rf.read().decode("utf-8", errors="replace")
            for raw_line in tail.splitlines():
                raw_line = raw_line.strip()
                if not raw_line:
                    continue
                try:
                    obj = json.loads(raw_line)
                except Exception:
                    continue
                t = obj.get("type", "")
                uid = obj.get("uuid", "")
                if t == "user" and uid:
                    last_user_uuid = uid
                elif t == "assistant" and uid:
                    last_asst_uuid = uid
        except Exception:
            pass

        return last_user_uuid, last_asst_uuid

    # ── Write Operations ─────────────────────────────────────────────

    def write_snapshot(
        self, session_id: str, snapshot: dict, cwd: str = ""
    ) -> None:
        """Append a file-history-snapshot entry to the session JSONL.

        Moved from session_manager.py L3313-3314 (inline in
        ``_write_file_snapshot``).
        """
        jsonl_path = self.find_session_path(session_id, cwd)
        if not jsonl_path or not jsonl_path.exists():
            logger.warning(
                "write_snapshot: JSONL not found (cwd=%s, sid=%s)", cwd, session_id
            )
            return

        with open(jsonl_path, "a", encoding="utf-8") as f:
            f.write(json.dumps(snapshot) + "\n")

    # ── Repair Operations ────────────────────────────────────────────

    def repair_incomplete_turn(
        self, session_id: str, cwd: str = ""
    ) -> bool:
        """Make a session's transcript safe for ``--resume`` to replay.

        Originally moved from session_manager.py L254-302
        (``_repair_incomplete_jsonl``).  Now performs two repairs, both of
        which prevent the CLI's ``--resume`` from sending the Anthropic API a
        transcript it will reject.

        **Pass 1 — complete an interrupted trailing turn.**
        When the daemon is killed mid-response, the last conversational entry
        is an assistant message with ``stop_reason=null``.  ``--resume`` chokes
        on that and immediately closes the stream.  We flip it to
        ``stop_reason="end_turn"``, drop any blocks that cannot be replayed
        (see ``_INTERRUPTED_TURN_DROP_TYPES``), and append an interruption
        notice so the entry still has content.

        **Pass 2 — strip unreplayable thinking blocks (the 400 fix).**
        On heavily-resumed sessions the CLI persists assistant thinking blocks
        with their ``signature`` but an EMPTY ``thinking`` text.  Replaying
        those makes the API 400 with "thinking or redacted_thinking blocks in
        the latest assistant message cannot be modified".  We remove every such
        block (see ``_is_unreplayable_thinking``).  The Anthropic API expressly
        allows omitting thinking blocks from prior assistant turns, so this is
        safe; intact thinking blocks (non-empty text + signature) are kept.

        The CLI writes one content block per JSONL line but groups consecutive
        lines that share ``message.id`` into a single API message.  An entry
        whose ONLY block is unreplayable would become empty — a shape the CLI
        never emits — so instead of writing ``content: []`` we DELETE that line
        and relink the linear ``parentUuid`` chain to the nearest survivor, so
        the healed file matches shapes the CLI actually produces.

        Returns True if anything was changed (and rewritten to disk).  Never
        writes a result that fails to re-parse as valid JSONL.
        """
        jsonl_path = self.find_session_path(session_id, cwd)
        if not jsonl_path or not jsonl_path.exists():
            return False

        try:
            with open(jsonl_path, "r", encoding="utf-8") as f:
                lines = f.readlines()
            if not lines:
                return False

            changed = False

            # ── Pass 1: complete an interrupted trailing assistant turn ──
            # Find the last non-empty conversational entry.  File-history
            # snapshots can trail a real turn, so skip them while searching;
            # stop at the first user/assistant (or unparseable) line.
            for idx in range(len(lines) - 1, -1, -1):
                stripped = lines[idx].strip()
                if not stripped:
                    continue
                try:
                    obj = json.loads(stripped)
                except Exception:
                    break  # can't parse — don't risk mis-repairing
                if obj.get("type") == "file-history-snapshot":
                    continue
                if obj.get("type") == "assistant":
                    msg = obj.get("message", {})
                    if msg.get("stop_reason") is None:
                        logger.info(
                            "Repairing incomplete assistant turn in %s",
                            jsonl_path.name,
                        )
                        msg["stop_reason"] = "end_turn"
                        msg["stop_sequence"] = None
                        raw = msg.get("content", [])
                        if not isinstance(raw, list):
                            raw = []
                        kept = [
                            b for b in raw
                            if not (isinstance(b, dict)
                                    and b.get("type") in _INTERRUPTED_TURN_DROP_TYPES)
                        ]
                        kept.append({"type": "text", "text": _INTERRUPTION_NOTICE})
                        msg["content"] = kept
                        lines[idx] = json.dumps(obj) + "\n"
                        changed = True
                break  # only the trailing entry matters for Pass 1

            # ── Pass 2: strip unreplayable (empty-text) thinking blocks ──
            # First parse every line and, for each assistant entry, decide
            # whether stripping leaves it empty (→ delete the line) or keeps
            # some content (→ rewrite with the bad blocks removed).
            parsed = []          # (obj_or_None, original_raw_line)
            parent_of = {}       # uuid -> parentUuid (pre-removal, for relink)
            for line in lines:
                s = line.strip()
                if not s:
                    parsed.append((None, line))
                    continue
                try:
                    obj = json.loads(s)
                except Exception:
                    parsed.append((None, line))
                    continue
                parsed.append((obj, line))
                if isinstance(obj, dict) and obj.get("uuid"):
                    parent_of[obj["uuid"]] = obj.get("parentUuid")

            remove_uuids = set()
            # 2a) classify: which assistant entries to strip vs. delete
            for obj, _line in parsed:
                if not isinstance(obj, dict) or obj.get("type") != "assistant":
                    continue
                content = obj.get("message", {}).get("content", [])
                if not isinstance(content, list) or not content:
                    continue
                if any(_is_unreplayable_thinking(b) for b in content):
                    if all(_is_unreplayable_thinking(b) for b in content):
                        # entry is ONLY unreplayable thinking — delete the line
                        if obj.get("uuid"):
                            remove_uuids.add(obj["uuid"])

            def _surviving_parent(p):
                """Walk up the parentUuid chain past any removed entries."""
                seen = 0
                while p in remove_uuids and seen < 100000:
                    p = parent_of.get(p)
                    seen += 1
                return p

            # 2b) rebuild the line list
            new_lines = []
            for obj, line in parsed:
                if not isinstance(obj, dict):
                    new_lines.append(line)
                    continue
                if obj.get("uuid") in remove_uuids:
                    changed = True
                    continue  # drop the unreplayable-thinking-only entry
                touched = False
                # relink parent if it pointed at a removed entry
                p = obj.get("parentUuid")
                if p in remove_uuids:
                    obj["parentUuid"] = _surviving_parent(p)
                    touched = True
                # strip unreplayable blocks from a mixed assistant entry
                if obj.get("type") == "assistant":
                    content = obj.get("message", {}).get("content", [])
                    if isinstance(content, list) and any(
                        _is_unreplayable_thinking(b) for b in content
                    ):
                        kept = [b for b in content
                                if not _is_unreplayable_thinking(b)]
                        if kept:  # empty-only entries were already removed above
                            obj["message"]["content"] = kept
                            touched = True
                if touched:
                    new_lines.append(json.dumps(obj) + "\n")
                    changed = True
                else:
                    new_lines.append(line)

            if changed:
                # Safety: never persist a transcript that doesn't re-parse.
                for nl in new_lines:
                    s = nl.strip()
                    if s:
                        json.loads(s)  # raises -> caught below -> no write
                logger.info(
                    "Healed session %s: removed %d unreplayable-thinking "
                    "entries", jsonl_path.name, len(remove_uuids),
                )
                with open(jsonl_path, "w", encoding="utf-8") as f:
                    f.writelines(new_lines)
            return changed
        except Exception as e:
            logger.warning("repair_incomplete_turn(%s) failed: %s", session_id, e)
            return False

    # ── Summary / Display Operations ─────────────────────────────────

    def load_summary(self, session_id: str, cwd: str = "") -> dict:
        """Load a lightweight session summary for the session list UI.

        Delegates to app.sessions.load_session_summary for backward
        compatibility.  The existing function will be consolidated
        here in Phase 3.
        """
        jsonl_path = self.find_session_path(session_id, cwd)
        if not jsonl_path:
            return {}
        try:
            from app.sessions import load_session_summary
            return load_session_summary(jsonl_path)
        except Exception as e:
            logger.warning("load_summary failed for %s: %s", session_id, e)
            return {}

    def read_entries(
        self, session_id: str, since: int = 0, cwd: str = ""
    ) -> list:
        """Read chat entries for display.

        Delegates to app.sessions.load_session for backward
        compatibility.  The existing function will be consolidated
        here in Phase 3.
        """
        jsonl_path = self.find_session_path(session_id, cwd)
        if not jsonl_path:
            return []
        try:
            from app.sessions import load_session
            result = load_session(jsonl_path)
            messages = result.get("messages", [])
            if since > 0:
                return messages[since:]
            return messages
        except Exception as e:
            logger.warning("read_entries failed for %s: %s", session_id, e)
            return []
