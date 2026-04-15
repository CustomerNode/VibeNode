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

                    # Source 1: tool_use blocks in assistant messages
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

                    # Source 2: existing file-history-snapshot entries
                    elif t == "file-history-snapshot":
                        snap = obj.get("snapshot", {})
                        for fp, binfo in snap.get("trackedFileBackups", {}).items():
                            if fp:
                                found.add(fp)
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
        """Repair a session that ends with an incomplete assistant turn.

        Moved from session_manager.py L254-302
        (``_repair_incomplete_jsonl``).

        When the daemon is killed mid-response, the last entry in the
        .jsonl is an assistant message with stop_reason=null.  The CLI's
        --resume chokes on this and immediately closes the stream.

        This function detects that case and patches the last line so
        stop_reason="end_turn" and a text block is appended saying the
        session was interrupted.
        """
        jsonl_path = self.find_session_path(session_id, cwd)
        if not jsonl_path or not jsonl_path.exists():
            return False

        try:
            with open(jsonl_path, "r", encoding="utf-8") as f:
                lines = f.readlines()
            if not lines:
                return False

            last_line = lines[-1].strip()
            if not last_line:
                return False

            obj = json.loads(last_line)
            if obj.get("type") != "assistant":
                return False

            msg = obj.get("message", {})
            if msg.get("stop_reason") is not None:
                return False  # already complete

            # Patch the incomplete assistant turn
            logger.info("Repairing incomplete assistant turn in %s", jsonl_path.name)
            msg["stop_reason"] = "end_turn"
            msg["stop_sequence"] = None

            # Append an interruption notice to content so the model knows
            content = msg.get("content", [])
            content.append({
                "type": "text",
                "text": "\n\n[Session interrupted — resuming from last checkpoint]",
            })
            msg["content"] = content

            lines[-1] = json.dumps(obj) + "\n"
            with open(jsonl_path, "w", encoding="utf-8") as f:
                f.writelines(lines)
            return True
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
