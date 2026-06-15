"""
Subsession inbox storage (spec §4.3).

Each parent session has a per-parent inbox file:

    ~/.claude/vibenode-state/<parent_session_id>/inbox.json

A subsession writes a report into its parent's inbox by appending a
``pending_reports[]`` entry; the parent's next ``send_message`` turn
reads the file, prepends every ``delivered: false`` entry as a delimited
block onto the user's text, marks them ``delivered: true``, and writes
the file back atomically.

This module is intentionally narrow:
  - File I/O only (no Flask, no SDK, no SessionManager).
  - All public functions take a parent SID and produce / mutate the
    on-disk inbox.
  - Atomic write uses the same tempfile + os.replace pattern as
    SessionRegistry.save_registry_now (spec §4.3.3 calls this out
    explicitly — do not roll a different pattern).
  - 100-entry cap with delivered-first eviction (spec §7.3).
  - Missing / corrupted files are tolerated.  On JSON parse failure
    the file is renamed inbox.json.broken-<ts> and the inbox is
    treated as empty — never raised into send_message (spec §9).

Schema (spec §4.3.2):

    {
      "version": 1,
      "pending_reports": [
        {
          "report_id": "<uuid4>",
          "child_session_id": "...",
          "child_name": "<human-readable>",
          "summary": "<plain-text summary>",
          "attachments": [
            {"type": "file_ref", "path": "...", "line": 882}
          ],
          "reported_at": "2026-05-28T20:14:00Z",
          "delivered": false
        }
      ]
    }
"""

from __future__ import annotations

import json
import logging
import os
import re
import tempfile
import threading
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# SID validation (phase 6.5 P1-2)
# ---------------------------------------------------------------------------
#
# inbox paths interpolate the parent SID directly into a filesystem path under
# ~/.claude/vibenode-state/<parent_sid>/.  Without validation, an attacker (or
# a buggy caller) could pass "..\\..\\evil" on Windows or "../../evil" on POSIX
# and resolve outside the state dir.
#
# VibeNode session SIDs come from several sources:
#   - str(uuid.uuid4())                        - canonical, most common
#   - f"_title_{uuid.uuid4().hex[:8]}"         - title-only side sessions
#   - Aliases (sm._id_aliases) - same character class as their canonical form
#
# All of these are alphanumeric with optional dashes and underscores.  None
# of the legitimate forms contains the dangerous characters: ``/``, ``\\``,
# ``..``, ``:`` (Windows drive letter), or null bytes.  We accept the
# permissive character class and explicitly forbid the dangerous shapes —
# this is the smallest-blast-radius defense that doesn't break legacy short
# IDs (e.g. _title_a1b2c3d4) or the integration test fixtures.

_SID_SAFE_CHARS_RE = re.compile(r"^[A-Za-z0-9_\-]+$")
_SID_MAX_LEN = 128  # generous; real SIDs are <= 40 chars


def _validate_sid(sid: str) -> None:
    """Raise ValueError if *sid* could be used for path traversal.

    Accepts canonical UUID4 strings, short title SIDs (``_title_xxxxxxxx``)
    and other alphanumeric forms.  Rejects:
      - Non-string types and empty strings.
      - Anything containing ``/``, ``\\``, ``..``, ``:``, or null bytes.
      - Anything with characters outside ``[A-Za-z0-9_-]``.
      - Anything longer than _SID_MAX_LEN (defense against absurd input).
    """
    if not isinstance(sid, str) or not sid:
        raise ValueError(
            f"Invalid session id (must be non-empty string): {sid!r}"
        )
    if len(sid) > _SID_MAX_LEN:
        raise ValueError(
            f"Invalid session id (exceeds {_SID_MAX_LEN} chars): {sid[:32]!r}..."
        )
    # Explicit path-traversal rejection — covers ".", "..", and ".x" forms
    # the regex would otherwise reject implicitly, with a clearer error.
    if ".." in sid or "/" in sid or "\\" in sid or ":" in sid or "\x00" in sid:
        raise ValueError(
            f"Invalid session id (contains path-traversal characters): {sid!r}"
        )
    if not _SID_SAFE_CHARS_RE.match(sid):
        raise ValueError(
            f"Invalid session id (allowed chars: [A-Za-z0-9_-]): {sid!r}"
        )


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Per-process lock keyed by parent SID.  Read-modify-write of an inbox
# (drain in send_message + concurrent reports from two children) must
# not interleave; the atomic os.replace protects the file on disk, but
# the lock protects in-process callers from racing each other into a
# write-after-write that loses one of the two updates.
_INBOX_LOCKS: dict[str, threading.Lock] = {}
_INBOX_LOCKS_MASTER = threading.Lock()

# Maximum pending_reports entries.  When the 101st write arrives, evict
# the oldest delivered: true first, then the oldest entry overall.
MAX_PENDING_REPORTS = 100

# Schema version.  Bump only when the on-disk shape changes in a way
# that older readers cannot tolerate.  Older readers ignore unknown
# fields, so additive changes do NOT need a bump.
INBOX_SCHEMA_VERSION = 1


# ---------------------------------------------------------------------------
# Path resolution
# ---------------------------------------------------------------------------

def _vibenode_state_root() -> Path:
    """Root of per-parent inbox directories: ~/.claude/vibenode-state/.

    Resolved at call time so the test conftest's _isolate_daemon_home
    fixture (which patches Path.home() to a tmp dir) actually takes
    effect.  Storing the path at module import would lock production
    state under the test sandbox.
    """
    return Path.home() / ".claude" / "vibenode-state"


def inbox_dir_for(parent_sid: str) -> Path:
    """Return the inbox directory for *parent_sid*.

    Phase 6.5 P1-2: validates the SID shape before interpolation to defeat
    a ``..\\..\\evil`` path-traversal from a buggy or malicious caller.
    """
    _validate_sid(parent_sid)
    return _vibenode_state_root() / parent_sid


def inbox_path_for(parent_sid: str) -> Path:
    """Return the inbox.json path for *parent_sid*."""
    return inbox_dir_for(parent_sid) / "inbox.json"


# ---------------------------------------------------------------------------
# Locks
# ---------------------------------------------------------------------------

def _lock_for(parent_sid: str) -> threading.Lock:
    """Return (creating if needed) the per-parent in-process lock."""
    with _INBOX_LOCKS_MASTER:
        lock = _INBOX_LOCKS.get(parent_sid)
        if lock is None:
            lock = threading.Lock()
            _INBOX_LOCKS[parent_sid] = lock
        return lock


# ---------------------------------------------------------------------------
# Read
# ---------------------------------------------------------------------------

def _empty_inbox() -> dict:
    return {"version": INBOX_SCHEMA_VERSION, "pending_reports": []}


def load_inbox(parent_sid: str) -> dict:
    """Read the inbox for *parent_sid* and return it as a dict.

    Returns ``_empty_inbox()`` if:
      - The file does not exist.
      - The file is unreadable.
      - The file fails to parse as JSON (and the corrupted file is
        renamed to inbox.json.broken-<timestamp> per spec §9).
    """
    path = inbox_path_for(parent_sid)
    if not path.exists():
        return _empty_inbox()
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as e:
        logger.warning("inbox: failed to read %s: %s", path, e)
        return _empty_inbox()
    try:
        data = json.loads(text)
    except json.JSONDecodeError as e:
        # Spec §9 — rename and continue.  send_message must not raise.
        try:
            broken_name = f"inbox.json.broken-{int(time.time())}"
            broken_path = path.with_name(broken_name)
            path.rename(broken_path)
            logger.warning(
                "inbox: corrupted JSON at %s renamed to %s (parse error: %s)",
                path, broken_path, e,
            )
        except OSError as rename_err:
            logger.warning(
                "inbox: corrupted JSON at %s could not be renamed: %s",
                path, rename_err,
            )
        return _empty_inbox()
    # Normalize: missing keys default to empty.
    if not isinstance(data, dict):
        return _empty_inbox()
    if "version" not in data:
        data["version"] = INBOX_SCHEMA_VERSION
    if "pending_reports" not in data or not isinstance(
        data.get("pending_reports"), list
    ):
        data["pending_reports"] = []
    return data


# ---------------------------------------------------------------------------
# Write
# ---------------------------------------------------------------------------

def _atomic_write_inbox(parent_sid: str, inbox: dict) -> None:
    """Write *inbox* atomically to the parent's inbox.json.

    Mirrors SessionRegistry.save_registry_now: writes to a temp file in
    the same directory, then ``os.replace`` to swap.  Lazy-creates the
    parent's vibenode-state/<sid>/ directory on first write (spec §4.3.3).
    """
    path = inbox_path_for(parent_sid)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(inbox, indent=2, ensure_ascii=False)

    tmp_fd, tmp_path = tempfile.mkstemp(
        dir=str(path.parent), suffix=".tmp"
    )
    try:
        os.write(tmp_fd, payload.encode("utf-8"))
        os.close(tmp_fd)
        # os.replace is atomic on POSIX and best-effort on Windows
        # (matches the SessionRegistry pattern).  On Windows the call
        # can fail with PermissionError if AV or another process holds
        # a handle — caller is expected to retry on transient failures
        # at the report-to-parent endpoint level (the inbox path is
        # not in the PERF-CRITICAL send hot path).
        os.replace(tmp_path, str(path))
    except Exception:
        try:
            os.close(tmp_fd)
        except OSError:
            pass
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


# ---------------------------------------------------------------------------
# Cap enforcement (spec §7.3)
# ---------------------------------------------------------------------------

def _enforce_cap(reports: list) -> list:
    """Trim *reports* to at most MAX_PENDING_REPORTS entries.

    Eviction policy: delivered entries first (oldest first by list
    order), then undelivered entries (oldest first).  This biases the
    inbox toward keeping fresh undelivered reports visible to the
    parent on its next turn — a misbehaving auto-report child can't
    silently push out a real human-authored report.
    """
    if len(reports) <= MAX_PENDING_REPORTS:
        return reports
    # Drop delivered entries first.
    keep_undelivered = [r for r in reports if not r.get("delivered")]
    keep_delivered = [r for r in reports if r.get("delivered")]
    while len(keep_undelivered) + len(keep_delivered) > MAX_PENDING_REPORTS:
        if keep_delivered:
            keep_delivered.pop(0)
        else:
            keep_undelivered.pop(0)
    return keep_delivered + keep_undelivered


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def append_report(
    parent_sid: str,
    child_sid: str,
    child_name: str,
    summary: str,
    attachments: Optional[list] = None,
    causal_chain_id: Optional[str] = None,
) -> dict:
    """Append a new report to the parent's inbox.

    Returns the appended entry (dict).  Thread-safe via per-parent
    in-process lock; atomic on disk via os.replace.

    ``causal_chain_id`` (optional) is the spawn-time lineage UUID stamped
    on the child at spawn (Patent 04/06 chain-of-custody pattern).  It is
    threaded through the report so a parent decision can be traced back to
    the exact child conclusion that drove it.  Older inboxes without the
    field read it back as absent; older readers ignore the extra key.
    """
    entry = {
        "report_id": str(uuid.uuid4()),
        "child_session_id": child_sid,
        "child_name": child_name or "",
        "summary": summary or "",
        "attachments": list(attachments) if attachments else [],
        "reported_at": datetime.now(timezone.utc).isoformat(
            timespec="seconds"
        ).replace("+00:00", "Z"),
        "delivered": False,
    }
    if causal_chain_id:
        entry["causal_chain_id"] = causal_chain_id
    with _lock_for(parent_sid):
        inbox = load_inbox(parent_sid)
        inbox["pending_reports"].append(entry)
        inbox["pending_reports"] = _enforce_cap(inbox["pending_reports"])
        _atomic_write_inbox(parent_sid, inbox)
    return entry


def drain_undelivered(parent_sid: str) -> list:
    """Atomically collect every ``delivered: false`` report and mark
    them delivered on disk.

    Returns the drained entries (in FIFO order — oldest undelivered
    first).  Called by SessionManager.send_message in Phase 4.

    If no undelivered entries exist, returns ``[]`` without touching
    the file — keeps the hot path cheap.
    """
    with _lock_for(parent_sid):
        inbox = load_inbox(parent_sid)
        undelivered = [
            r for r in inbox["pending_reports"] if not r.get("delivered")
        ]
        if not undelivered:
            return []
        # Mark in place — preserves list order.
        for r in inbox["pending_reports"]:
            if not r.get("delivered"):
                r["delivered"] = True
        _atomic_write_inbox(parent_sid, inbox)
        return undelivered


def has_undelivered(parent_sid: str) -> bool:
    """Return True if the parent's inbox has at least one
    ``delivered: false`` entry on disk.

    Used by SessionManager recovery to repopulate the in-memory
    ``inbox_dirty`` flag at startup.
    """
    inbox = load_inbox(parent_sid)
    return any(not r.get("delivered") for r in inbox["pending_reports"])


def undelivered_count(parent_sid: str) -> int:
    """Return the number of ``delivered: false`` entries in the
    parent's inbox (for the sidebar badge)."""
    inbox = load_inbox(parent_sid)
    return sum(1 for r in inbox["pending_reports"] if not r.get("delivered"))


def remove_inbox(parent_sid: str) -> None:
    """Remove the parent's entire vibenode-state/<sid>/ directory.

    Called when a parent session is deleted (spec §6.2).  Tolerates
    missing directories.
    """
    import shutil

    inbox_dir = inbox_dir_for(parent_sid)
    if inbox_dir.exists():
        try:
            shutil.rmtree(inbox_dir)
        except OSError as e:
            logger.warning(
                "inbox: failed to remove %s: %s", inbox_dir, e
            )


# ---------------------------------------------------------------------------
# Delimited block (spec §4.3.4)
# ---------------------------------------------------------------------------

def format_drain_block(entries: list) -> str:
    """Format *entries* into the prepended block sent to the parent.

    Block shape (spec §4.3.4):

        [Subsession reports — surfaced before your next message]
        From subsession "<child_name>" (<child_sid_short>):
        <summary>
        ---
        [Your message]
        <original user text>      <-- caller appends this

    Returns the block text WITHOUT the trailing "[Your message]\n"
    separator — the caller in send_message decides whether to append
    it (empty-text "Pull updates" branch omits the suffix per spec §4.3.5).
    """
    if not entries:
        return ""
    lines = ["[Subsession reports — surfaced before your next message]"]
    for entry in entries:
        child_name = entry.get("child_name") or "(unnamed subsession)"
        child_sid = entry.get("child_session_id") or ""
        short_sid = child_sid[:8] if child_sid else "????"
        summary = entry.get("summary") or "(no summary provided)"
        lines.append(
            f'From subsession "{child_name}" ({short_sid}):'
        )
        lines.append(summary)
        # Render structured attachments (spec §4.3.2 / §6.7).  v1 surfaced
        # only the summary; the schema has always carried attachments[] so
        # we now render file references as "path:line" so the parent can
        # act on them directly.  Unknown attachment types are rendered by
        # their type name so nothing is silently dropped.
        rendered_refs = _format_attachments(entry.get("attachments"))
        if rendered_refs:
            lines.append(f"  refs: {rendered_refs}")
    return "\n".join(lines)


def _format_attachments(attachments: Optional[list]) -> str:
    """Render an entry's ``attachments[]`` into a compact one-line string.

    ``file_ref`` attachments become ``path:line`` (line omitted when
    absent).  Any other attachment type is rendered as its ``type`` name
    so the parent at least knows something was attached.  Returns an
    empty string when there is nothing to render.
    """
    if not attachments or not isinstance(attachments, list):
        return ""
    parts = []
    for att in attachments:
        if not isinstance(att, dict):
            continue
        if att.get("type") == "file_ref":
            path = att.get("path") or ""
            if not path:
                continue
            line = att.get("line")
            parts.append(f"{path}:{line}" if line else path)
        else:
            kind = att.get("type")
            if kind:
                parts.append(str(kind))
    return ", ".join(parts)
