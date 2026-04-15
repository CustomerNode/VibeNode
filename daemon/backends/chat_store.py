"""Abstract base class for session persistence.

This module defines the ``ChatStore`` abstract class that encapsulates all
session storage operations (reading, writing, discovering, and mutating
session data files).

The Claude implementation reads/writes Claude Code's native JSONL format
(one JSON object per line, stored under ``~/.claude/projects/``).
Other backends could use SQLite, API-based storage, or any other format.

Current JSONL access points in the codebase (to be consolidated behind
this abstraction in Phase 2):

+----------------------------+------------------------------------------+
| File                       | Operations                               |
+============================+==========================================+
| session_manager.py L3326   | ``_find_session_jsonl()`` -- locate file  |
| session_manager.py L254    | ``_repair_incomplete_jsonl()`` -- repair  |
| session_manager.py L3072   | ``_prepopulate_tracked_files()`` -- scan  |
| session_manager.py L3262   | ``_write_file_snapshot()`` -- tail read   |
| session_manager.py L3313   | ``_write_file_snapshot()`` -- append      |
| app/sessions.py L44-505    | Load summaries, full sessions, timelines  |
| app/code_extraction.py     | Extract code blocks from messages         |
| app/routes/sessions_api.py | CRUD operations on session files          |
+----------------------------+------------------------------------------+
"""

from abc import ABC, abstractmethod
from pathlib import Path
from typing import Optional


class ChatStore(ABC):
    """Abstract interface for session persistence.

    Encapsulates all session file reading, writing, and management.
    The Claude implementation reads/writes Claude Code's native JSONL
    format.  Other backends could use SQLite, API storage, etc.

    All methods accept string identifiers (session_id, cwd) rather than
    ``Path`` objects so the interface is storage-format-agnostic.
    """

    # ── Session Discovery ────────────────────────────────────────────

    @abstractmethod
    def find_session_path(
        self, session_id: str, cwd: str = ""
    ) -> Optional[Path]:
        """Locate the storage file/resource for a session.

        Args:
            session_id: The session identifier.
            cwd: Working directory hint for faster lookup.  The Claude
                implementation encodes this to find the project
                directory.

        Returns:
            Path to the session file, or None if not found.

        Claude implementation (session_manager.py L3326-3348):
            Encodes ``cwd`` with ``_encode_cwd()``, checks
            ``~/.claude/projects/<encoded>/<session_id>.jsonl``.
            Falls back to scanning all project directories.
        """
        ...

    # ── Read Operations ──────────────────────────────────────────────

    @abstractmethod
    def read_tracked_files(
        self, session_id: str, cwd: str = ""
    ) -> tuple:
        """Scan session storage for tracked files and metadata.

        Extracts file paths that were edited by tools (Edit, Write,
        MultiEdit, NotebookEdit) and file-history-snapshot entries.

        Args:
            session_id: The session identifier.
            cwd: Working directory for locating the session file.

        Returns:
            A tuple of ``(tracked_files, file_versions,
            last_user_uuid, last_asst_uuid)`` where:

            - ``tracked_files`` (set): File paths modified by tools.
            - ``file_versions`` (dict): Highest version number per file
              from snapshot entries.
            - ``last_user_uuid`` (str): UUID of the last user message.
            - ``last_asst_uuid`` (str): UUID of the last assistant
              message.

        Claude implementation (session_manager.py L3060-3137,
        ``_prepopulate_tracked_files`` scan logic):
            Full JSONL scan for ``tool_use`` blocks in assistant
            messages and ``file-history-snapshot`` entries.  Also
            caches user/assistant UUIDs encountered during the scan.
        """
        ...

    @abstractmethod
    def read_tail_uuids(
        self, session_id: str, cwd: str = ""
    ) -> tuple:
        """Read the last user and assistant UUIDs from the session tail.

        Used for linking ``file-history-snapshot`` entries to the
        correct user/assistant message pair.  Reads only the tail of
        the file for performance (the Claude implementation reads the
        last 64KB).

        Args:
            session_id: The session identifier.
            cwd: Working directory for locating the session file.

        Returns:
            A tuple of ``(last_user_uuid, last_asst_uuid)``.  Both
            are empty strings if not found.

        Claude implementation (session_manager.py L3262-3286):
            Seeks to ``max(0, file_size - 65536)``, reads the tail,
            scans for user/assistant entries with ``uuid`` fields.
        """
        ...

    # ── Write Operations ─────────────────────────────────────────────

    @abstractmethod
    def write_snapshot(
        self, session_id: str, snapshot: dict, cwd: str = ""
    ) -> None:
        """Append a file-history-snapshot entry to session storage.

        Args:
            session_id: The session identifier.
            snapshot: The snapshot dict to write.  For Claude JSONL,
                this is a complete entry with ``type``,
                ``messageId``, ``snapshot``, and
                ``isSnapshotUpdate`` keys.
            cwd: Working directory for locating the session file.

        Claude implementation (session_manager.py L3313-3314):
            ``json.dumps(snapshot_entry) + "\\n"`` appended to the
            JSONL file.
        """
        ...

    # ── Repair Operations ────────────────────────────────────────────

    @abstractmethod
    def repair_incomplete_turn(
        self, session_id: str, cwd: str = ""
    ) -> bool:
        """Repair a session that ends with an incomplete assistant turn.

        When the daemon is killed mid-response, the session may end
        with an incomplete entry.  This method patches it so the
        session can be resumed.

        Args:
            session_id: The session identifier.
            cwd: Working directory for locating the session file.

        Returns:
            True if the session was repaired, False if no repair was
            needed or the session was not found.

        Claude implementation (session_manager.py L254-302,
        ``_repair_incomplete_jsonl``):
            Detects assistant messages with ``stop_reason=null``,
            patches to ``stop_reason="end_turn"``, and appends an
            interruption notice text block.
        """
        ...

    # ── Summary / Display Operations ─────────────────────────────────

    @abstractmethod
    def load_summary(self, session_id: str, cwd: str = "") -> dict:
        """Load a lightweight session summary for the session list UI.

        Returns a dict with enough metadata to display in the sidebar
        without loading all messages.

        Args:
            session_id: The session identifier.
            cwd: Working directory for locating the session file.

        Returns:
            Dict with at minimum ``id``, ``display_title``, ``date``,
            ``preview``, ``message_count``.  Exact keys depend on the
            backend implementation.

        Claude implementation (app/sessions.py L44-81,
        ``load_session_summary``):
            Full JSONL parse extracting metadata fields from each line.
        """
        ...

    @abstractmethod
    def read_entries(
        self, session_id: str, since: int = 0, cwd: str = ""
    ) -> list:
        """Read chat entries for display, optionally from a given index.

        Args:
            session_id: The session identifier.
            since: Entry index to start from (0 = beginning).
                Allows incremental loading for long sessions.
            cwd: Working directory for locating the session file.

        Returns:
            List of entry dicts, each containing at minimum ``type``
            and relevant content fields.

        Claude implementation (app/sessions.py L128-196,
        ``load_session``):
            Full JSONL parse, each line becomes an entry dict with
            ``type``, message content, ``uuid``, tool use details, etc.
        """
        ...
