"""
Compose conflict detection — scans directives for conflicts and resolves them.

Three-path resolution:
1. Clearly global → auto-resolve, mark old superseded
2. Clearly contextual → auto-resolve, scope both
3. Ambiguous → create ComposeConflict, emit event, wait for user
"""

import logging
import re
from datetime import datetime, timezone
from typing import Optional

from .models import (
    ComposeConflict, ComposeDirective, ConflictStatus,
)
from .context_manager import (
    read_context, add_directive, add_conflict, update_conflict,
    get_directives, set_changing, write_context, _get_lock,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Keyword detection for auto-resolution
# ---------------------------------------------------------------------------

# Keywords that signal a global/override intent
GLOBAL_KEYWORDS = [
    r"\bactually\b",
    r"\bacross the board\b",
    r"\beverywhere\b",
    r"\ball sections\b",
    r"\bglobally\b",
    r"\boverride\b",
    r"\balways\b",
    r"\bnever\b",
    r"\bfor everything\b",
    r"\bin all cases\b",
]

# Keywords that signal a section-scoped intent
CONTEXTUAL_KEYWORDS = [
    r"\bfor this section\b",
    r"\bjust here\b",
    r"\bonly in\b",
    r"\bonly for\b",
    r"\bin this part\b",
    r"\bthis chapter\b",
    r"\bthis section only\b",
    r"\bjust this\b",
    r"\blocally\b",
]


def _has_global_signal(text: str) -> bool:
    """Check if text contains keywords indicating global intent."""
    text_lower = text.lower()
    return any(re.search(pattern, text_lower) for pattern in GLOBAL_KEYWORDS)


def _has_contextual_signal(text: str) -> bool:
    """Check if text contains keywords indicating section-specific intent."""
    text_lower = text.lower()
    return any(re.search(pattern, text_lower) for pattern in CONTEXTUAL_KEYWORDS)


def _directives_conflict(a_content: str, b_content: str) -> bool:
    """Simple heuristic: two directives conflict if they address the same topic
    but give different instructions.

    For now, uses word overlap to detect topic similarity. A more sophisticated
    approach would use embeddings or LLM classification.
    """
    # Normalize
    a_words = set(re.findall(r'\b\w+\b', a_content.lower()))
    b_words = set(re.findall(r'\b\w+\b', b_content.lower()))

    # Remove stop words
    stop_words = {
        'the', 'a', 'an', 'is', 'are', 'was', 'were', 'be', 'been', 'being',
        'have', 'has', 'had', 'do', 'does', 'did', 'will', 'would', 'could',
        'should', 'may', 'might', 'shall', 'can', 'need', 'must', 'to', 'of',
        'in', 'for', 'on', 'with', 'at', 'by', 'from', 'it', 'this', 'that',
        'and', 'or', 'but', 'not', 'no', 'all', 'use', 'just', 'only',
    }
    a_meaningful = a_words - stop_words
    b_meaningful = b_words - stop_words

    if not a_meaningful or not b_meaningful:
        return False

    # Topic overlap: shared meaningful words
    overlap = a_meaningful & b_meaningful
    min_size = min(len(a_meaningful), len(b_meaningful))

    # If >30% overlap in meaningful words, they address the same topic
    if min_size > 0 and len(overlap) / min_size > 0.3:
        return True

    return False


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def detect_conflicts(project_id: str, new_directive: ComposeDirective) -> list:
    """Scan existing active directives for conflicts with a new directive.

    Returns list of ComposeConflict objects created (empty if no conflicts
    or all auto-resolved).
    """
    existing = get_directives(project_id)
    active = [d for d in existing if d.get("status") == "active"]

    conflicts_created = []

    for existing_d in active:
        # Skip self-comparison
        if existing_d.get("id") == new_directive.id:
            continue

        # Check if they address the same topic
        if not _directives_conflict(existing_d.get("content", ""), new_directive.content):
            continue

        # Found a potential conflict. Always surface to user for resolution.
        new_content = new_directive.content
        old_content = existing_d.get("content", "")

        recommendation = generate_recommendation(old_content, new_content)
        conflict = ComposeConflict.create(
            project_id=project_id,
            directive_a_id=existing_d.get("id", ""),
            directive_b_id=new_directive.id,
            directive_a_content=old_content,
            directive_b_content=new_content,
            recommendation=recommendation,
        )
        add_conflict(project_id, conflict)
        conflicts_created.append(conflict)

        logger.info(
            "Ambiguous conflict detected between '%s' and '%s'. Created conflict %s",
            old_content[:50], new_content[:50], conflict.id,
        )

    return conflicts_created


def generate_recommendation(directive_a_content: str, directive_b_content: str) -> str:
    """Generate a human-readable recommendation for resolving a conflict.

    Provides a clear explanation of both directives and a suggested resolution.
    """
    return (
        f"Two directives address the same topic but differ:\n\n"
        f"Directive A: \"{directive_a_content}\"\n"
        f"Directive B: \"{directive_b_content}\"\n\n"
        f"Options:\n"
        f"- SUPERSEDE: Directive B replaces Directive A globally\n"
        f"- SCOPE: Both apply but to different sections\n"
        f"- KEEP BOTH: Both remain active (may cause inconsistency)\n\n"
        f"Recommendation: If Directive B is a refinement of A, supersede. "
        f"If they apply to different contexts, scope them."
    )


def resolve_conflict(project_id: str, conflict_id: str, action: str) -> dict:
    """Resolve a directive conflict.

    Actions:
        - supersede: New directive (B) replaces old (A)
        - scope: Both remain active, scoped to their respective sections
        - keep_both: Both remain active as-is

    Returns dict with resolution details.
    """
    lock = _get_lock(project_id)
    with lock:
        ctx = read_context(project_id)
        conflicts = ctx.get("conflicts", [])

        # Find the conflict
        conflict_data = None
        for c in conflicts:
            if c.get("id") == conflict_id:
                conflict_data = c
                break

        if not conflict_data:
            return {'resolved': False, 'error': f'Conflict not found: {conflict_id}'}

        if conflict_data.get("status") == "resolved":
            return {'resolved': False, 'error': 'Conflict already resolved'}

        now = datetime.now(timezone.utc).isoformat()
        directive_a_id = conflict_data.get("directive_a_id")
        directive_b_id = conflict_data.get("directive_b_id")
        affected_sections = []

        if action == "supersede":
            # Mark directive A as superseded
            directives = ctx.get("directives", [])
            for d in directives:
                if d.get("id") == directive_a_id:
                    d["status"] = "superseded"
                    # Find sections affected by directive A
                    scope = d.get("scope", "global")
                    if scope != "global":
                        affected_sections.append(scope)
                    break
            ctx["directives"] = directives

        elif action == "scope":
            # Both remain active; the UI should prompt for scope assignment
            # For now, keep both as-is
            pass

        elif action == "keep_both":
            # Both remain active
            pass

        # Mark conflict as resolved
        conflict_data["status"] = "resolved"
        conflict_data["resolution_action"] = action
        conflict_data["resolved_at"] = now
        conflict_data["resolution"] = f"User chose: {action}"
        ctx["conflicts"] = conflicts

        # Log resolution as a new directive
        resolution_directive = {
            "id": f"resolution-{conflict_id[:8]}",
            "gen": max((d.get("gen", 0) for d in ctx.get("directives", [])), default=0) + 1,
            "scope": "global",
            "content": f"Conflict resolved ({action}): {conflict_data.get('directive_b_content', '')}",
            "source": "root",
            "status": "active",
            "created_at": now,
        }
        ctx.setdefault("directives", []).append(resolution_directive)

        # Write context (we already hold the lock)
        from .context_manager import _write_context_unlocked
        _write_context_unlocked(project_id, ctx)

    # Set changing flag on affected sections (outside the context lock)
    for section_id in affected_sections:
        try:
            set_changing(
                project_id, section_id,
                change_note=f"Directive conflict resolved ({action}). Review your content.",
                set_by="root",
            )
        except Exception:
            logger.exception("Failed to set changing flag on section %s", section_id)

    return {
        'resolved': True,
        'conflict_id': conflict_id,
        'action': action,
        'affected_sections': affected_sections,
    }


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _supersede_directive(project_id: str, directive_id: str) -> None:
    """Mark a directive as superseded in compose-context.json."""
    lock = _get_lock(project_id)
    with lock:
        ctx = read_context(project_id)
        directives = ctx.get("directives", [])
        for d in directives:
            if d.get("id") == directive_id:
                d["status"] = "superseded"
                break
        from .context_manager import _write_context_unlocked
        _write_context_unlocked(project_id, ctx)
