"""
AI Task Planner — uses Claude to auto-generate subtask breakdowns.

Given a parent task title and description, asks Claude to produce a structured
list of subtasks. Optionally scans route files to detect verification URLs.
"""

import json
import os
import re
import glob
import uuid as uuid_mod
from datetime import datetime, timezone
from pathlib import Path

from ..db.repository import KanbanRepository, Task


# ---------------------------------------------------------------------------
# URL Detection — scans route files for verification URL candidates
# ---------------------------------------------------------------------------

def detect_verification_urls(project_root=None):
    """Scan Flask route files for URL patterns.

    Returns a dict mapping route paths to descriptions, e.g.:
        {"/api/kanban/board": "GET — get_board", "/login": "GET — login_page"}
    """
    if project_root is None:
        project_root = os.getcwd()

    urls = {}
    route_patterns = [
        os.path.join(project_root, "app", "routes", "*.py"),
        os.path.join(project_root, "app", "**", "*.py"),
        os.path.join(project_root, "routes", "*.py"),
    ]

    for pattern in route_patterns:
        for filepath in glob.glob(pattern, recursive=True):
            try:
                with open(filepath, "r", encoding="utf-8", errors="ignore") as f:
                    content = f.read()
                for match in re.finditer(
                    r'@\w+\.route\(\s*["\']([^"\']+)["\']'
                    r'(?:.*?methods\s*=\s*\[([^\]]+)\])?',
                    content,
                ):
                    path = match.group(1)
                    methods = match.group(2) or '"GET"'
                    method = methods.strip().strip("'\"").split(",")[0].strip().strip("'\"")
                    pos = match.end()
                    func_match = re.search(r'def\s+(\w+)', content[pos:pos + 200])
                    func_name = func_match.group(1) if func_match else "handler"
                    urls[path] = f"{method} — {func_name}"
            except Exception:
                continue

    return urls


# ---------------------------------------------------------------------------
# Planner prompt
# ---------------------------------------------------------------------------

PLANNER_SYSTEM = """You are a task planner for a software development project.
Given a task title and description, break it down into concrete, actionable subtasks.

Rules:
- Each subtask should be independently completable by a single AI coding session
- Order subtasks logically (dependencies first)
- Keep subtask titles concise but descriptive
- Aim for 3-8 subtasks unless the task is very large

Verification URLs:
- Each subtask has an optional verification_url field — an absolute URL (http:// or https://)
  the developer can click to manually validate the feature.
- Default is null unless a dev server base URL is provided below.
- When a base URL is provided, you MUST set verification_url on every task by constructing
  absolute URLs from that base URL + real route paths from the project code.
- Only set to null for purely non-visual tasks (refactoring, config) with no observable endpoint.

{available_urls}

Respond with a JSON array of objects:
[
  {{
    "title": "subtask title",
    "description": "what needs to be done",
    "verification_url": null
  }}
]

Respond ONLY with the JSON array, no other text."""

PLANNER_USER = """Break down this task into subtasks:

Title: {title}
Description: {description}

Context: {context}"""


def build_planner_prompt(task, repo, project_root=None):
    """Build the system and user prompts for the AI planner.

    Returns (system_prompt, user_prompt) tuple.
    """
    urls = detect_verification_urls(project_root)
    url_list = "\n".join(f"  {path}: {desc}" for path, desc in sorted(urls.items()))
    if not url_list:
        url_list = "  (no routes detected)"

    system = PLANNER_SYSTEM.format(available_urls=url_list)

    context_parts = []
    if task.parent_id:
        parent = repo.get_task(task.parent_id)
        if parent:
            context_parts.append(f"Parent task: {parent.title}")
        siblings = repo.get_children(task.parent_id)
        sibling_titles = [s.title for s in siblings if s.id != task.id]
        if sibling_titles:
            context_parts.append(f"Sibling tasks: {', '.join(sibling_titles[:5])}")

    context = "\n".join(context_parts) or "This is a top-level task."

    user = PLANNER_USER.format(
        title=task.title,
        description=task.description or "(no description provided)",
        context=context,
    )

    return system, user


async def run_planner(repo, task_id, project_root=None):
    """Run the AI planner to generate subtask suggestions.

    Returns a list of dicts: [{title, description, verification_url}, ...]
    """
    task = repo.get_task(task_id)
    if task is None:
        raise ValueError(f"Task not found: {task_id}")

    system_prompt, user_prompt = build_planner_prompt(task, repo, project_root)

    # Try claude_code_sdk first
    try:
        from claude_code_sdk import query, ClaudeCodeOptions

        result_text = ""
        async for msg in query(
            prompt=user_prompt,
            options=ClaudeCodeOptions(
                system_prompt=system_prompt,
                max_turns=1,
            ),
        ):
            if hasattr(msg, "content"):
                if isinstance(msg.content, str):
                    result_text += msg.content
                elif isinstance(msg.content, list):
                    for block in msg.content:
                        if hasattr(block, "text"):
                            result_text += block.text

        return _parse_planner_response(result_text)

    except ImportError:
        raise RuntimeError(
            "claude_code_sdk not installed. Run: pip install claude-code-sdk"
        )


def plan_subtasks(task_title, task_description=""):
    """Synchronous wrapper for backward compatibility with the stub API.

    Returns an empty list if async planner can't be called synchronously.
    Use run_planner() for the full async implementation.
    """
    return []


def _parse_planner_response(text):
    """Parse the AI response into a list of subtask dicts."""
    text = text.strip()

    # Handle markdown code blocks
    json_match = re.search(r'\[[\s\S]*\]', text)
    if json_match:
        try:
            subtasks = json.loads(json_match.group())
            result = []
            for item in subtasks:
                if isinstance(item, dict) and "title" in item:
                    result.append({
                        "title": str(item["title"]),
                        "description": str(item.get("description", "")),
                        "verification_url": item.get("verification_url"),
                    })
            return result
        except json.JSONDecodeError:
            pass

    raise ValueError(f"Failed to parse planner response as JSON: {text[:200]}")


def apply_plan(repo, parent_task_id, subtasks, project_id):
    """Create subtasks from a planner result.

    Args:
        repo: KanbanRepository instance.
        parent_task_id: The parent task to create subtasks under.
        subtasks: List of dicts from run_planner().
        project_id: Project ID for the new tasks.

    Returns:
        List of created Task objects.
    """
    now = datetime.now(timezone.utc).isoformat()
    created = []

    for i, sub in enumerate(subtasks):
        task_id = str(uuid_mod.uuid4())
        position = (i + 1) * 1000

        # Only accept absolute URLs — discard anything relative
        ver_url = sub.get("verification_url")
        if ver_url and not ver_url.startswith(("http://", "https://")):
            ver_url = None

        from ..db.repository import Task, TaskStatus
        task_obj = Task(
            id=task_id,
            project_id=project_id,
            parent_id=parent_task_id,
            title=sub["title"],
            description=sub.get("description", ""),
            verification_url=ver_url,
            status=TaskStatus.NOT_STARTED,
            position=position,
            depth=0,
            created_at=now,
            updated_at=now,
        )
        task = repo.create_task(task_obj)
        created.append(task)

    return created
