"""
Compose system prompt builder — constructs the system prompt for root
orchestrator and section agents based on their role and current context.

The compose_task_id encodes the role:
  - "root:{project_id}" — root orchestrator prompt
  - "section:{project_id}:{section_id}" — section agent prompt
"""

import json
import logging
from typing import Optional

from .models import (
    ComposeProject, ComposeSection, SectionStatus,
    get_project, get_sections, get_section, save_project,
)
from .context_manager import read_context, update_section_in_context

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Root orchestrator system prompt template
# ---------------------------------------------------------------------------

ROOT_SYSTEM_PROMPT = """You are the Root Orchestrator for the Compose project "{project_name}".
You see the full composition, not one section. Users talk to you about the whole project: structure, coherence, conflicts, and export.

Before EVERY action, read compose-context.json to understand:
- All section statuses and summaries
- All facts discovered across the composition
- All pending directive conflicts
- All active changing flags

YOUR THREE RESPONSIBILITIES:
1. STRUCTURE: You add, remove, reorder, merge, and split sections. Section agents never modify the tree. When creating a new section, scaffold its folder, add it to compose-context.json, and write an initial brief for the section agent.
2. COHERENCE: When sections contradict each other or drift apart, diagnose the gap. Fix factual errors in compose-context.json directly. For content changes, set changing:true on the target section with a change_note, then send a specific directive to the section agent. Never set changing:false on another agent's section.
3. ASSEMBLY & EXPORT: You own final export. Read all section source files, assemble in correct order per the composition hierarchy, apply export config (template, styles, format), run the export pipeline, deliver the final file.

CONFLICT RESOLUTION:
When an ambiguous directive conflict surfaces, present BOTH directives to the user with a recommendation and reasoning. Never silently pick one interpretation. Let the user resolve in one click or one sentence. After resolution, update compose-context.json and signal affected sections via the changing flag.

ROUTING:
If the user sends you a message that is clearly about a specific section, route the directive to that section. You manage the whole tree. Interpret intent from content, not just from which card was selected.

After EVERY meaningful update:
- Update composition-level status in compose-context.json
- Add any cross-section facts or decisions
- Log conflict resolutions as new directives with explicit scope

## Current State

### Sections ({section_count} total)
{section_list}

### Facts
{facts_list}

### Pending Conflicts
{conflicts_list}

### Active Directives
{directives_list}
"""


# ---------------------------------------------------------------------------
# Section agent system prompt template
# ---------------------------------------------------------------------------

SECTION_SYSTEM_PROMPT = """You are a Compose section agent working on "{section_name}" within the project "{project_name}".

You are responsible for ONE section of a larger composition. The root orchestrator manages the overall structure. You manage your section's content.

## Your Section
- **Name:** {section_name}
- **Status:** {section_status}
- **Type:** {artifact_type}

## Your Responsibilities
1. Write and refine the content for this section
2. Follow directives from the root orchestrator
3. Report facts you discover that might affect other sections
4. Update your section status and summary when you make progress

## Rules
- Do NOT modify other sections or the composition structure
- If you receive a changing flag with a change_note, address it and clear the flag when done
- If you discover facts that contradict other sections, report them (the root will handle cross-section coherence)

## Project Context
{project_context}

## Section Directives
{section_directives}
"""


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def parse_compose_task_id(compose_task_id: str) -> dict:
    """Parse a compose_task_id into its components.

    Formats:
        "root:{project_id}" -> {"role": "root", "project_id": "..."}
        "section:{project_id}:{section_id}" -> {"role": "section", "project_id": "...", "section_id": "..."}

    Returns dict with 'role', 'project_id', and optionally 'section_id'.
    Raises ValueError if format is invalid.
    """
    parts = compose_task_id.split(":", 2)
    if len(parts) < 2:
        raise ValueError(f"Invalid compose_task_id format: {compose_task_id}")

    role = parts[0]
    if role == "root":
        return {"role": "root", "project_id": parts[1]}
    elif role == "section":
        if len(parts) < 3:
            raise ValueError(f"Section task_id must have project_id and section_id: {compose_task_id}")
        return {"role": "section", "project_id": parts[1], "section_id": parts[2]}
    else:
        raise ValueError(f"Unknown compose role: {role}")


def build_compose_prompt(compose_task_id: str) -> dict:
    """Build the system prompt for a compose session.

    Args:
        compose_task_id: Encoded task identifier ("root:{pid}" or "section:{pid}:{sid}")

    Returns:
        {'ok': True, 'system_prompt': '...', 'agent_role': 'root'|'section'}
        or {'ok': False, 'error': '...'}
    """
    try:
        parsed = parse_compose_task_id(compose_task_id)
    except ValueError as e:
        return {'ok': False, 'error': str(e)}

    project_id = parsed["project_id"]
    project = get_project(project_id)
    if not project:
        return {'ok': False, 'error': f'Project not found: {project_id}'}

    role = parsed["role"]

    if role == "root":
        prompt = _build_root_prompt(project)
        return {'ok': True, 'system_prompt': prompt, 'agent_role': 'root'}

    elif role == "section":
        section_id = parsed["section_id"]
        # Read context once and find the section in it to avoid double file read
        try:
            ctx = read_context(project_id)
        except Exception:
            ctx = {"sections": []}
        section = None
        for s_dict in ctx.get("sections", []):
            if s_dict.get("id") == section_id:
                section = ComposeSection.from_dict(s_dict)
                break
        if not section:
            return {'ok': False, 'error': f'Section not found: {section_id}'}
        prompt = _build_section_prompt(project, section, ctx=ctx)
        return {'ok': True, 'system_prompt': prompt, 'agent_role': 'section'}

    return {'ok': False, 'error': f'Unknown role: {role}'}


def link_session(compose_task_id: str, session_id: str) -> None:
    """Link a session to a compose task (root or section).

    Updates the project's root_session_id or the section's session_id.
    """
    try:
        parsed = parse_compose_task_id(compose_task_id)
    except ValueError:
        logger.warning("Invalid compose_task_id for link: %s", compose_task_id)
        return

    project_id = parsed["project_id"]
    role = parsed["role"]

    if role == "root":
        project = get_project(project_id)
        if project:
            project.root_session_id = session_id
            save_project(project)
            logger.info("Linked session %s as root for project %s", session_id, project_id)

    elif role == "section":
        section_id = parsed["section_id"]
        section = get_section(project_id, section_id)
        if section:
            section.session_id = session_id
            update_section_in_context(project_id, section)
            logger.info(
                "Linked session %s to section %s in project %s",
                session_id, section_id, project_id,
            )


def make_root_task_id(project_id: str) -> str:
    """Generate the compose_task_id for a root orchestrator."""
    return f"root:{project_id}"


def make_section_task_id(project_id: str, section_id: str) -> str:
    """Generate the compose_task_id for a section agent."""
    return f"section:{project_id}:{section_id}"


# ---------------------------------------------------------------------------
# Prompt builders (internal)
# ---------------------------------------------------------------------------

def _build_root_prompt(project: ComposeProject) -> str:
    """Build the full root orchestrator system prompt with current context."""
    try:
        ctx = read_context(project.id)
    except Exception:
        ctx = {"sections": [], "facts": {}, "directives": [], "conflicts": []}

    sections = ctx.get("sections", [])
    facts = ctx.get("facts", {})
    directives = ctx.get("directives", [])
    conflicts = [c for c in ctx.get("conflicts", []) if c.get("status") == "pending"]

    # Format section list
    if sections:
        section_lines = []
        for s in sections:
            status = s.get("status", "drafting")
            changing = " [CHANGING]" if s.get("changing") else ""
            summary = f" -- {s.get('summary')}" if s.get("summary") else ""
            section_lines.append(f"- [{status}] {s.get('name')}{changing}{summary}")
        section_list = "\n".join(section_lines)
    else:
        section_list = "(no sections yet)"

    # Format facts
    if facts:
        facts_lines = [f"- {k}: {v}" for k, v in facts.items()]
        facts_list = "\n".join(facts_lines)
    else:
        facts_list = "(no facts recorded)"

    # Format conflicts
    if conflicts:
        conflict_lines = []
        for c in conflicts:
            conflict_lines.append(
                f"- CONFLICT [{c.get('id', '?')[:8]}]: "
                f"\"{c.get('directive_a_content', '?')}\" vs "
                f"\"{c.get('directive_b_content', '?')}\""
            )
        conflicts_list = "\n".join(conflict_lines)
    else:
        conflicts_list = "(no pending conflicts)"

    # Format directives
    active_directives = [d for d in directives if d.get("status") == "active"]
    if active_directives:
        dir_lines = []
        for d in active_directives:
            scope = d.get("scope", "global")
            dir_lines.append(f"- [gen {d.get('gen', '?')}] ({scope}) {d.get('content', '')}")
        directives_list = "\n".join(dir_lines)
    else:
        directives_list = "(no active directives)"

    return ROOT_SYSTEM_PROMPT.format(
        project_name=project.name,
        section_count=len(sections),
        section_list=section_list,
        facts_list=facts_list,
        conflicts_list=conflicts_list,
        directives_list=directives_list,
    )


def _build_section_prompt(project: ComposeProject, section: ComposeSection, ctx=None) -> str:
    """Build the section agent system prompt with project context."""
    if ctx is None:
        try:
            ctx = read_context(project.id)
        except Exception:
            ctx = {"sections": [], "facts": {}, "directives": []}

    # Project context: other sections + facts
    other_sections = [
        s for s in ctx.get("sections", []) if s.get("id") != section.id
    ]
    if other_sections:
        ctx_lines = ["Other sections in this composition:"]
        for s in other_sections:
            ctx_lines.append(f"- [{s.get('status')}] {s.get('name')}")
        project_context = "\n".join(ctx_lines)
    else:
        project_context = "(this is the only section)"

    # Add facts
    facts = ctx.get("facts", {})
    if facts:
        project_context += "\n\nKnown facts:\n"
        project_context += "\n".join(f"- {k}: {v}" for k, v in facts.items())

    # Section-specific directives
    directives = ctx.get("directives", [])
    section_dirs = [
        d for d in directives
        if d.get("status") == "active" and d.get("scope") in (section.id, "global")
    ]
    if section_dirs:
        dir_lines = []
        for d in section_dirs:
            scope_label = "global" if d.get("scope") == "global" else "this section"
            dir_lines.append(f"- [gen {d.get('gen')}] ({scope_label}) {d.get('content')}")
        section_directives = "\n".join(dir_lines)
    else:
        section_directives = "(no directives)"

    return SECTION_SYSTEM_PROMPT.format(
        section_name=section.name,
        project_name=project.name,
        section_status=section.status.value,
        artifact_type=section.artifact_type or "text",
        project_context=project_context,
        section_directives=section_directives,
    )
