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

import base64
import hashlib
import json
import logging
import os
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


# ── Stale-media eviction (Workstream 1 + 2) ──────────────────────────────────
# See docs/plans/large-session-perf.md.  These helpers externalize OLD inline
# image bytes (and the byte-identical `toolUseResult.file.base64` duplicate) to a
# content-addressed file under the user's home, replacing the replayed copy with
# a short text placeholder / metadata marker.  This is LOSSLESS: every byte of
# the original image is preserved on disk and recoverable by sha256.  What changes
# is only what `--resume` re-feeds the model on each round-trip — a stale
# full-resolution screenshot the model already looked at many turns ago.

# media_type → file extension for the externalized decoded bytes.  Falls back to
# ".bin" so an unknown type still externalizes (the sha256 is the real key).
_MEDIA_EXT = {
    "image/jpeg": "jpg",
    "image/jpg": "jpg",
    "image/png": "png",
    "image/gif": "gif",
    "image/webp": "webp",
    "image/bmp": "bmp",
    "image/svg+xml": "svg",
    "image/tiff": "tiff",
}


def _media_ext_for(media_type: str) -> str:
    """Return a file extension for a media_type (never raises)."""
    mt = (media_type or "").strip().lower()
    if mt in _MEDIA_EXT:
        return _MEDIA_EXT[mt]
    if "/" in mt:
        sub = mt.split("/", 1)[1]
        # Strip parameters like ``image/png; foo`` and structured suffixes.
        sub = sub.split(";", 1)[0].split("+", 1)[0].strip()
        if sub.isalnum() and sub:
            return sub
    return "bin"


def _media_dir_for(session_id: str) -> Path:
    """Resolve the per-session externalized-media directory.

    ``~/.claude/session-env/<session_id>/vibenode-media/`` — derived from
    ``Path.home()`` (never a hardcoded user path; public-repo safe).  The
    ``session-env/<sid>/`` dir is CLI-created; we use a ``vibenode-media/``
    subdir so we never collide with anything the CLI owns there.  It lives under
    the user's home, OUTSIDE any repo tree, so it can never be committed.
    """
    return (
        Path.home() / ".claude" / "session-env" / str(session_id)
        / "vibenode-media"
    )


def _externalize_media_bytes(
    media_dir: Path, b64_data: str, media_type: str
) -> tuple:
    """Decode base64 image data and write it (content-addressed) to disk.

    Idempotent: the filename is the sha256 of the DECODED bytes, so writing the
    same content twice is a no-op (and de-dups automatically across the session).

    Returns ``(sha256_hex, relpath, ext)`` on success, or ``(None, None, None)``
    if the base64 cannot be decoded (caller leaves that image inline).
    """
    try:
        raw = base64.b64decode(b64_data, validate=False)
    except Exception:
        return None, None, None
    sha = hashlib.sha256(raw).hexdigest()
    ext = _media_ext_for(media_type)
    fname = f"{sha}.{ext}"
    dest = media_dir / fname
    if not dest.exists():
        media_dir.mkdir(parents=True, exist_ok=True)
        # Write atomically (temp + replace) so a concurrent reader never sees a
        # half-written media file.  Same dir → os.replace is atomic on POSIX/NT.
        tmp = dest.with_name(dest.name + ".tmp")
        with open(tmp, "wb") as wf:
            wf.write(raw)
        os.replace(tmp, dest)
    relpath = f"vibenode-media/{fname}"
    return sha, relpath, ext


def _block_has_inline_image(block) -> bool:
    """True if a tool_result sub-block is an inline base64 image."""
    if not isinstance(block, dict) or block.get("type") != "image":
        return False
    src = block.get("source")
    return (
        isinstance(src, dict)
        and src.get("type") == "base64"
        and bool(src.get("data"))
    )


def _tool_use_result_needs_dedup(obj) -> bool:
    """True if this entry carries a non-externalized ``toolUseResult.file.base64``."""
    if not isinstance(obj, dict):
        return False
    tur = obj.get("toolUseResult")
    if not isinstance(tur, dict):
        return False
    f = tur.get("file")
    return isinstance(f, dict) and bool(f.get("base64"))


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

    def _repair_lines(self, lines: list, fname: str = "") -> tuple:
        """Pure in-memory repair of a JSONL line list (Pass 1 + Pass 2).

        Performs exactly the two repairs that ``repair_incomplete_turn`` has
        always done, but on an in-memory ``lines`` list instead of reading/writing
        the file itself.  Returns ``(new_lines, changed, removed_count)``.  Never
        raises for parse errors (unparseable lines pass through untouched).

        Factored out of ``repair_incomplete_turn`` so the combined
        ``prepare_for_resume`` pass can run repair + media eviction in ONE read
        and ONE conditional rewrite without reading/rewriting the file twice.

        **Pass 1 — complete an interrupted trailing turn.**  The last
        conversational entry with ``stop_reason=null`` is flipped to
        ``end_turn``, unreplayable blocks (``_INTERRUPTED_TURN_DROP_TYPES``) are
        dropped, and an interruption notice is appended.

        **Pass 2 — strip unreplayable (empty-but-signed) thinking blocks.**
        FULL-SCOPE by design: these blocks span the whole back half of a
        heavily-resumed transcript, NOT just the tail (verified on the real
        491K transcript), so a tail-only scan is UNSAFE.  An entry whose ONLY
        block is unreplayable is DELETED and the linear ``parentUuid`` chain is
        relinked to the nearest survivor.
        """
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
                    logger.info("Repairing incomplete assistant turn in %s", fname)
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

        return new_lines, changed, len(remove_uuids)

    def repair_incomplete_turn(
        self, session_id: str, cwd: str = ""
    ) -> bool:
        """Make a session's transcript safe for ``--resume`` to replay.

        Originally moved from session_manager.py L254-302
        (``_repair_incomplete_jsonl``).  Performs two repairs, both of which
        prevent the CLI's ``--resume`` from sending the Anthropic API a
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

        NOTE: signature/behavior preserved verbatim (it is on the ``ChatStore``
        ABC and called from 3 sites).  ``prepare_for_resume`` reuses the same
        ``_repair_lines`` helper, so repair behavior stays bit-identical here.
        """
        jsonl_path = self.find_session_path(session_id, cwd)
        if not jsonl_path or not jsonl_path.exists():
            return False

        try:
            with open(jsonl_path, "r", encoding="utf-8") as f:
                lines = f.readlines()
            if not lines:
                return False

            new_lines, changed, removed = self._repair_lines(
                lines, jsonl_path.name
            )

            if changed:
                # Safety: never persist a transcript that doesn't re-parse.
                for nl in new_lines:
                    s = nl.strip()
                    if s:
                        json.loads(s)  # raises -> caught below -> no write
                logger.info(
                    "Healed session %s: removed %d unreplayable-thinking "
                    "entries", jsonl_path.name, removed,
                )
                with open(jsonl_path, "w", encoding="utf-8") as f:
                    f.writelines(new_lines)
            return changed
        except Exception as e:
            logger.warning("repair_incomplete_turn(%s) failed: %s", session_id, e)
            return False

    # ── Stale-media eviction + combined resume pass (WS1/WS2/WS3) ─────

    def _recent_image_line_floor(
        self, lines: list, keep_recent_turns: int
    ) -> int:
        """Return the line index at/after which images are 'recent' (kept inline).

        "User turn" boundary = a non-system ``user`` message — matching how the
        codebase already reasons about turns.  We count user turns from the TAIL,
        skipping sidechain/sub-agent entries and ``user`` entries whose content is
        purely tool_result output (those are tool plumbing, not a human turn).
        Any image at a line index ``>= floor`` is inside the most recent
        ``keep_recent_turns`` and is preserved inline for the model; older images
        (index ``< floor``) are evictable.

        A floor of ``len(lines)`` (keep nothing recent) only happens when
        ``keep_recent_turns <= 0``; a floor of 0 means the whole file is within
        the recent window (nothing is old enough to evict).
        """
        if keep_recent_turns <= 0:
            return len(lines)
        seen = 0
        for idx in range(len(lines) - 1, -1, -1):
            s = lines[idx].strip()
            if not s:
                continue
            try:
                obj = json.loads(s)
            except Exception:
                continue
            if not isinstance(obj, dict) or obj.get("type") != "user":
                continue
            # Skip sidechain / sub-agent user turns.
            if obj.get("isSidechain"):
                continue
            msg = obj.get("message", {})
            content = msg.get("content")
            # A genuine human user turn is either a string, or a content list
            # that contains at least one non-tool_result block.  A list that is
            # ONLY tool_result blocks is tool plumbing, not a human turn.
            is_human_turn = False
            if isinstance(content, str):
                is_human_turn = True
            elif isinstance(content, list):
                is_human_turn = any(
                    not (isinstance(b, dict) and b.get("type") == "tool_result")
                    for b in content
                )
            if not is_human_turn:
                continue
            seen += 1
            if seen >= keep_recent_turns:
                return idx  # this human turn starts the recent-K window
        return 0  # fewer than K user turns total → everything is "recent"

    def _scan_has_evictable(
        self, lines: list, keep_recent_turns: int, dedup_recent: bool
    ) -> bool:
        """Cheap gate: is there any inline image (or dup toolUseResult) to evict?

        Returns True as soon as it finds ONE evictable inline ``image`` sub-block
        outside the recent-K window, or (when ``dedup_recent``) any
        non-externalized ``toolUseResult.file.base64``.  Lets the combined pass
        skip the rewrite entirely when nothing is evictable.
        """
        floor = self._recent_image_line_floor(lines, keep_recent_turns)
        for idx, line in enumerate(lines):
            s = line.strip()
            if not s:
                continue
            try:
                obj = json.loads(s)
            except Exception:
                continue
            if not isinstance(obj, dict):
                continue
            # WS2: a duplicate toolUseResult anywhere is evictable when
            # dedup_recent is on (recent images still drop their dup copy);
            # when off, only entries outside recent-K count.
            if _tool_use_result_needs_dedup(obj):
                if dedup_recent or idx < floor:
                    return True
            # WS1: an inline image OUTSIDE recent-K is evictable.
            if idx < floor:
                msg = obj.get("message", {})
                content = msg.get("content")
                if isinstance(content, list):
                    for block in content:
                        if (isinstance(block, dict)
                                and block.get("type") == "tool_result"):
                            sub = block.get("content")
                            if isinstance(sub, list) and any(
                                _block_has_inline_image(b) for b in sub
                            ):
                                return True
                        elif _block_has_inline_image(block):
                            # assistant-inline image outside recent-K
                            return True
        return False

    def _evict_lines(
        self, lines: list, session_id: str, keep_recent_turns: int,
        dedup_recent: bool,
    ) -> tuple:
        """In-place media eviction (WS1) + toolUseResult de-dup (WS2).

        Mutates each line's parsed object IN PLACE — never adds or removes a
        JSONL line — so ``message.id`` grouping, ``tool_use_id`` pairing, and the
        ``uuid``/``parentUuid`` chain are all untouched by construction.

        For each OLD (index < recency floor) inline ``image`` sub-block of a
        ``tool_result`` (and any assistant-inline image): externalize the decoded
        bytes to ``vibenode-media/<sha256>.<ext>`` and swap the ``image``
        sub-block for the spec's text placeholder, keeping the enclosing
        ``tool_result``'s ``tool_use_id`` and all sibling text blocks verbatim.

        For ``toolUseResult.file.base64`` (auxiliary CLI metadata, never replayed):
        externalize it to the same content-addressed file and replace it with the
        ``{"externalized": true, "sha256", "relpath", "media_type"}`` marker —
        for OLD entries always, and for recent entries too when ``dedup_recent``.

        Per-image isolation: a decode/IO failure on one image leaves THAT image
        inline and continues with the rest.  Returns ``(new_lines, changed)``.
        """
        media_dir = _media_dir_for(session_id)
        floor = self._recent_image_line_floor(lines, keep_recent_turns)
        changed = False
        out = []

        for idx, line in enumerate(lines):
            s = line.strip()
            if not s:
                out.append(line)
                continue
            try:
                obj = json.loads(s)
            except Exception:
                out.append(line)  # unparseable → pass through untouched
                continue
            if not isinstance(obj, dict):
                out.append(line)
                continue

            line_changed = False
            is_old = idx < floor

            # ── WS1: evict OLD inline images in this entry ──
            if is_old:
                msg = obj.get("message")
                if isinstance(msg, dict):
                    content = msg.get("content")
                    if isinstance(content, list):
                        for ci, block in enumerate(content):
                            if not isinstance(block, dict):
                                continue
                            btype = block.get("type")
                            if btype == "tool_result":
                                sub = block.get("content")
                                if isinstance(sub, list):
                                    if self._evict_image_sublist(sub, media_dir):
                                        line_changed = True
                            elif btype == "image":
                                # assistant-inline image: swap in place by index
                                rep = self._evict_one_image(block, media_dir)
                                if rep is not None:
                                    content[ci] = rep
                                    line_changed = True

            # ── WS2: de-dup the redundant toolUseResult.file.base64 ──
            if _tool_use_result_needs_dedup(obj) and (dedup_recent or is_old):
                if self._dedup_tool_use_result(obj, media_dir):
                    line_changed = True

            if line_changed:
                out.append(json.dumps(obj) + "\n")
                changed = True
            else:
                out.append(line)

        return out, changed

    def _evict_one_image(self, block: dict, media_dir: Path):
        """Externalize one inline base64 ``image`` block → text placeholder.

        Returns the replacement text block, or ``None`` if the image could not be
        externalized (caller leaves it inline).
        """
        src = block.get("source")
        if not (isinstance(src, dict) and src.get("type") == "base64"):
            return None
        data = src.get("data")
        if not data:
            return None
        media_type = src.get("media_type", "") or "application/octet-stream"
        try:
            sha, relpath, _ext = _externalize_media_bytes(
                media_dir, data, media_type
            )
        except Exception as e:
            logger.warning("media externalization failed (kept inline): %s", e)
            return None
        if not sha:
            return None
        orig_kb = max(1, round(len(data) * 3 / 4 / 1024))
        return {
            "type": "text",
            "text": (
                "[image evicted from replayed context to save tokens — "
                f"preserved on disk: {relpath} ({media_type}, {orig_kb} KB). "
                "Ask to re-read the source if you need to see it again.]"
            ),
        }

    def _evict_image_sublist(self, sub: list, media_dir: Path) -> bool:
        """Swap every inline ``image`` sub-block in a tool_result content list
        for its text placeholder, in place.  Returns True if anything changed.
        Already-evicted placeholders and text blocks are left verbatim.
        """
        changed = False
        for i, block in enumerate(sub):
            if not _block_has_inline_image(block):
                continue
            rep = self._evict_one_image(block, media_dir)
            if rep is not None:
                sub[i] = rep
                changed = True
        return changed

    def _dedup_tool_use_result(self, obj: dict, media_dir: Path) -> bool:
        """Externalize ``toolUseResult.file.base64`` → reconstructable marker.

        Returns True if the marker was written.  ``toolUseResult`` is auxiliary
        CLI metadata that is NEVER replayed to the model, so this is replay-safe;
        the marker keeps ``sha256``/``relpath`` so the value is fully
        reconstructable as a hedge against a future CLI that consumes it.
        """
        tur = obj.get("toolUseResult")
        if not isinstance(tur, dict):
            return False
        f = tur.get("file")
        if not isinstance(f, dict):
            return False
        data = f.get("base64")
        if not data:
            return False
        media_type = f.get("type", "") or "application/octet-stream"
        try:
            sha, relpath, _ext = _externalize_media_bytes(
                media_dir, data, media_type
            )
        except Exception as e:
            logger.warning("toolUseResult de-dup failed (kept inline): %s", e)
            return False
        if not sha:
            return False
        f.pop("base64", None)
        f["externalized"] = True
        f["sha256"] = sha
        f["relpath"] = relpath
        f["media_type"] = media_type
        return True

    def prepare_for_resume(
        self, session_id: str, cwd: str = "", evict_media: bool = True,
        keep_recent_turns: int = 4, dedup_recent_tooluseresult: bool = True,
    ) -> bool:
        """Combined change-gated pre-``--resume`` pass: repair + media eviction.

        PERF-CRITICAL: runs ONLY in the pre-``--resume`` executor pass (resume +
        reconnect), NEVER on the per-turn send path, and NEVER rewrites the file
        when nothing changed.  Moving this onto the hot path, or removing the
        change-gate, reintroduces a full multi-MB read+rewrite per message.  See
        docs/plans/large-session-perf.md and CLAUDE.md PERF guardrails.

        Combines, in ONE read and AT MOST ONE conditional rewrite:
          1. Repair (Pass 1 trailing-turn completion + Pass 2 full-scope
             empty-thinking strip+relink) — identical to ``repair_incomplete_turn``.
          2. WS1 stale-media eviction: OLD inline images (older than the last
             ``keep_recent_turns`` user turns) are externalized to
             ``~/.claude/session-env/<sid>/vibenode-media/<sha256>.<ext>`` and
             replaced in the replayed transcript by a text placeholder.  LOSSLESS:
             the bytes are recoverable by sha256.  In-place block swap → no JSONL
             line added/removed → tool_use↔tool_result pairing and the
             uuid/parentUuid chain are untouched.
          3. WS2 de-dup: the byte-identical ``toolUseResult.file.base64`` duplicate
             is externalized to the same file and replaced by a reconstructable
             marker (it is auxiliary metadata, never replayed).

        Idempotent: already-evicted images leave a placeholder and an
        ``externalized`` marker, so a re-run finds nothing evictable and writes
        nothing (``changed=False`` → no rewrite → no prompt-cache bust).

        A cheap streaming gate runs BEFORE any full in-memory rewrite: if there is
        nothing to repair AND nothing to evict, we return False without rewriting.

        ``evict_media=False`` reduces this to pure repair (same result as
        ``repair_incomplete_turn``), used as the master-switch off path.

        Returns True iff the file was rewritten.  Never writes a transcript that
        fails to re-parse as valid JSONL (final validity guard, same as repair).
        """
        jsonl_path = self.find_session_path(session_id, cwd)
        if not jsonl_path or not jsonl_path.exists():
            return False

        try:
            with open(jsonl_path, "r", encoding="utf-8") as f:
                lines = f.readlines()
            if not lines:
                return False

            # ── Repair (always; cheap on already-clean files) ──
            new_lines, changed, removed = self._repair_lines(
                lines, jsonl_path.name
            )

            # ── WS1/WS2 eviction (gated) ──
            if evict_media:
                # Cheap-skip: only enter the rewrite branch if the scan finds
                # something evictable.  Operates on the repaired line list so a
                # repaired-but-not-evictable file still rewrites for the repair.
                if self._scan_has_evictable(
                    new_lines, keep_recent_turns, dedup_recent_tooluseresult
                ):
                    try:
                        ev_lines, ev_changed = self._evict_lines(
                            new_lines, session_id, keep_recent_turns,
                            dedup_recent_tooluseresult,
                        )
                        if ev_changed:
                            new_lines = ev_lines
                            changed = True
                    except Exception as e:
                        # Eviction must NEVER block resume or corrupt the file —
                        # fall back to the (still valid) repaired/original lines.
                        logger.warning(
                            "media eviction failed for %s (resume proceeds "
                            "un-evicted): %s", session_id, e,
                        )

            if not changed:
                return False  # nothing changed → no rewrite, no cache bust

            # Safety: never persist a transcript that doesn't re-parse.
            for nl in new_lines:
                s = nl.strip()
                if s:
                    json.loads(s)  # raises -> caught below -> no write
            logger.info(
                "prepare_for_resume %s: repaired %d thinking entries, "
                "media eviction=%s", jsonl_path.name, removed, evict_media,
            )
            # Atomic-ish: temp + os.replace so a partial write is never persisted.
            tmp = jsonl_path.with_name(jsonl_path.name + ".prepare.tmp")
            with open(tmp, "w", encoding="utf-8") as f:
                f.writelines(new_lines)
            os.replace(tmp, jsonl_path)
            return True
        except Exception as e:
            logger.warning("prepare_for_resume(%s) failed: %s", session_id, e)
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
