"""
Compose context management — read/write compose-context.json with file locking.

This is the single source of truth for composition state. All mutations go
through these functions. The file watcher (compose_watcher.py) monitors for
changes and emits SocketIO events.
"""

import json
import logging
import os
import tempfile
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from .models import (
    ComposeSection, ComposeDirective, ComposeConflict, SectionStatus,
    project_dir, get_sections,
)

logger = logging.getLogger(__name__)

# File-level lock for atomic context writes
_context_locks = {}  # project_id -> threading.Lock
_locks_lock = threading.Lock()


def _get_lock(project_id: str) -> threading.Lock:
    """Get or create a lock for a specific project's context file."""
    with _locks_lock:
        if project_id not in _context_locks:
            _context_locks[project_id] = threading.Lock()
        return _context_locks[project_id]


# ---------------------------------------------------------------------------
# Read / Write
# ---------------------------------------------------------------------------

def read_context(project_id: str) -> dict:
    """Read compose-context.json for a project. Returns parsed dict."""
    pdir = project_dir(project_id)
    ctx_file = pdir / "compose-context.json"
    if not ctx_file.is_file():
        raise FileNotFoundError(f"No context file for project {project_id}")
    return json.loads(ctx_file.read_text(encoding="utf-8"))


def write_context(project_id: str, context: dict) -> None:
    """Atomic write of compose-context.json (write to temp, rename).

    Uses a per-project lock to prevent concurrent writes from corrupting
    the file.
    """
    lock = _get_lock(project_id)
    pdir = project_dir(project_id)
    ctx_file = pdir / "compose-context.json"

    with lock:
        # Write to temp file in same directory, then rename (atomic on same FS)
        fd, tmp_path = tempfile.mkstemp(
            dir=str(pdir), suffix=".tmp", prefix=".ctx-"
        )
        try:
            with os.fdopen(fd, 'w', encoding='utf-8') as f:
                json.dump(context, f, indent=2, ensure_ascii=False)
            # On Windows, os.rename fails if target exists; use os.replace
            os.replace(tmp_path, str(ctx_file))
        except Exception:
            # Clean up temp file on failure
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
            raise


def _read_and_lock(project_id: str):
    """Read context and acquire the project lock. Caller MUST release lock.

    Returns (context_dict, lock). Use in a try/finally block.
    """
    lock = _get_lock(project_id)
    lock.acquire()
    try:
        ctx = read_context(project_id)
        return ctx, lock
    except Exception:
        lock.release()
        raise


# ---------------------------------------------------------------------------
# Section management in context
# ---------------------------------------------------------------------------

def add_section_to_context(project_id: str, section: ComposeSection) -> None:
    """Add a section to compose-context.json."""
    lock = _get_lock(project_id)
    with lock:
        ctx = read_context(project_id)
        sections = ctx.get("sections", [])
        sections.append(section.to_dict())
        ctx["sections"] = sections
        _update_status_counts(ctx)
        _write_context_unlocked(project_id, ctx)


def update_section_in_context(project_id: str, section: ComposeSection) -> None:
    """Update a section's data in compose-context.json."""
    lock = _get_lock(project_id)
    with lock:
        ctx = read_context(project_id)
        sections = ctx.get("sections", [])
        for i, s in enumerate(sections):
            if s["id"] == section.id:
                sections[i] = section.to_dict()
                break
        ctx["sections"] = sections
        _update_status_counts(ctx)
        _write_context_unlocked(project_id, ctx)


def remove_section_from_context(project_id: str, section_id: str) -> None:
    """Remove a section from compose-context.json."""
    lock = _get_lock(project_id)
    with lock:
        ctx = read_context(project_id)
        sections = ctx.get("sections", [])
        ctx["sections"] = [s for s in sections if s["id"] != section_id]
        _update_status_counts(ctx)
        _write_context_unlocked(project_id, ctx)


def reorder_sections_in_context(project_id: str, order: list) -> None:
    """Reorder sections based on a list of section IDs."""
    lock = _get_lock(project_id)
    with lock:
        ctx = read_context(project_id)
        sections = ctx.get("sections", [])
        section_map = {s["id"]: s for s in sections}
        reordered = []
        for i, sid in enumerate(order):
            if sid in section_map:
                section_map[sid]["order"] = i
                reordered.append(section_map[sid])
        # Append any sections not in the order list
        for s in sections:
            if s["id"] not in {sid for sid in order}:
                reordered.append(s)
        ctx["sections"] = reordered
        _write_context_unlocked(project_id, ctx)


# ---------------------------------------------------------------------------
# Facts
# ---------------------------------------------------------------------------

def update_facts(project_id: str, facts_dict: dict) -> None:
    """Merge new facts into the existing facts in compose-context.json."""
    lock = _get_lock(project_id)
    with lock:
        ctx = read_context(project_id)
        existing_facts = ctx.get("facts", {})
        existing_facts.update(facts_dict)
        ctx["facts"] = existing_facts
        _write_context_unlocked(project_id, ctx)


# ---------------------------------------------------------------------------
# Section status
# ---------------------------------------------------------------------------

def update_section_status(project_id: str, section_id: str,
                          status: Optional[str] = None,
                          summary: Optional[str] = None,
                          changing: Optional[bool] = None,
                          change_note: Optional[str] = None) -> None:
    """Update a section's status, summary, and/or changing flag."""
    lock = _get_lock(project_id)
    with lock:
        ctx = read_context(project_id)
        sections = ctx.get("sections", [])
        for s in sections:
            if s["id"] == section_id:
                if status is not None:
                    s["status"] = status
                if summary is not None:
                    s["summary"] = summary
                if changing is not None:
                    s["changing"] = changing
                if change_note is not None:
                    s["change_note"] = change_note
                break
        _update_status_counts(ctx)
        _write_context_unlocked(project_id, ctx)


# ---------------------------------------------------------------------------
# Directives
# ---------------------------------------------------------------------------

def add_directive(project_id: str, directive: ComposeDirective) -> ComposeDirective:
    """Append a directive with auto-incrementing gen number."""
    lock = _get_lock(project_id)
    with lock:
        ctx = read_context(project_id)
        directives = ctx.get("directives", [])
        # Auto-increment gen
        max_gen = max((d.get("gen", 0) for d in directives), default=0)
        directive.gen = max_gen + 1
        directives.append(directive.to_dict())
        ctx["directives"] = directives
        _write_context_unlocked(project_id, ctx)
    return directive


def get_directives(project_id: str) -> list:
    """Get all directives from context."""
    ctx = read_context(project_id)
    return ctx.get("directives", [])


# ---------------------------------------------------------------------------
# Changing flag
# ---------------------------------------------------------------------------

def set_changing(project_id: str, section_id: str,
                 change_note: str = "", set_by: str = "root") -> None:
    """Set changing:true on a section. Root can set on any section."""
    lock = _get_lock(project_id)
    with lock:
        ctx = read_context(project_id)
        sections = ctx.get("sections", [])
        found = False
        for s in sections:
            if s["id"] == section_id:
                s["changing"] = True
                s["change_note"] = change_note
                s["changing_set_by"] = set_by
                found = True
                break
        if not found:
            raise ValueError(f"Section {section_id} not found")
        _write_context_unlocked(project_id, ctx)


def clear_changing(project_id: str, section_id: str,
                   cleared_by: str = "") -> None:
    """Clear changing flag. Only the section itself can clear its own flag."""
    lock = _get_lock(project_id)
    with lock:
        ctx = read_context(project_id)
        sections = ctx.get("sections", [])
        for s in sections:
            if s["id"] == section_id:
                # Validation: only the section can clear its own flag
                if s.get("changing_set_by") and cleared_by != section_id:
                    raise ValueError(
                        f"Only section {section_id} can clear its own changing flag. "
                        f"Attempted by: {cleared_by}"
                    )
                s["changing"] = False
                s["change_note"] = None
                s["changing_set_by"] = None
                break
        _write_context_unlocked(project_id, ctx)


# ---------------------------------------------------------------------------
# Conflicts
# ---------------------------------------------------------------------------

def add_conflict(project_id: str, conflict: ComposeConflict) -> None:
    """Add a conflict to compose-context.json."""
    lock = _get_lock(project_id)
    with lock:
        ctx = read_context(project_id)
        conflicts = ctx.get("conflicts", [])
        conflicts.append(conflict.to_dict())
        ctx["conflicts"] = conflicts
        _write_context_unlocked(project_id, ctx)


def update_conflict(project_id: str, conflict_id: str, updates: dict) -> None:
    """Update a conflict's fields in context."""
    lock = _get_lock(project_id)
    with lock:
        ctx = read_context(project_id)
        conflicts = ctx.get("conflicts", [])
        for c in conflicts:
            if c["id"] == conflict_id:
                c.update(updates)
                break
        ctx["conflicts"] = conflicts
        _write_context_unlocked(project_id, ctx)


def get_pending_conflicts(project_id: str) -> list:
    """Get all pending conflicts."""
    ctx = read_context(project_id)
    return [c for c in ctx.get("conflicts", []) if c.get("status") == "pending"]


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _write_context_unlocked(project_id: str, context: dict) -> None:
    """Write context without acquiring lock (caller must hold lock)."""
    pdir = project_dir(project_id)
    ctx_file = pdir / "compose-context.json"

    fd, tmp_path = tempfile.mkstemp(
        dir=str(pdir), suffix=".tmp", prefix=".ctx-"
    )
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as f:
            json.dump(context, f, indent=2, ensure_ascii=False)
        os.replace(tmp_path, str(ctx_file))
    except Exception:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


def _update_status_counts(ctx: dict) -> None:
    """Recalculate status counts from sections list."""
    sections = ctx.get("sections", [])
    total = len(sections)
    complete = sum(1 for s in sections if s.get("status") == "complete")
    working = sum(1 for s in sections if s.get("status") == "working")
    ctx["status"] = {
        "total_sections": total,
        "complete": complete,
        "in_progress": working,
        "not_started": total - complete - working,
    }
