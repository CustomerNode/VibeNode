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
# URL Detection — scans ALL source files for route/URL patterns
# ---------------------------------------------------------------------------

_SKIP_DIRS = {"node_modules", "dist", "build", ".git", ".claude",
              "__pycache__", ".venv", "venv", ".next", ".nuxt",
              "coverage", ".tox", ".mypy_cache", ".pytest_cache"}
_SOURCE_EXTS = {".py", ".js", ".ts", ".jsx", ".tsx", ".rb", ".go",
                ".java", ".php", ".rs", ".ex", ".exs"}

# Generic patterns that match URL paths across any framework/language.
# Each tuple: (compiled_regex, group_index_for_path, label_func)
_URL_PATTERNS = [
    # --- Decorator / attribute routes (Flask, FastAPI, Bottle, Starlette, etc.) ---
    # @app.route("/path"), @router.get("/path"), @app.api_route("/path")
    (re.compile(
        r'@\w+\.'
        r'(?:route|get|post|put|patch|delete|head|options|api_route|websocket)'
        r'\(\s*["\']([^"\']+)["\']'
    ), 1, lambda m: "route"),

    # --- Function-call route registration (Express, Koa, Hono, etc.) ---
    # app.get("/path"), router.post("/path"), server.route("/path")
    (re.compile(
        r'(?:app|router|server|api)\.'
        r'(get|post|put|patch|delete|all|head|options|route|use)'
        r'\(\s*["\'](/[^"\']*)["\']'
    ), 2, lambda m: m.group(1).upper()),

    # --- URL config patterns (Django, Rails, PHP, etc.) ---
    # path("route/", ...), re_path(r"^route/", ...), Route::get("/path", ...)
    (re.compile(
        r'(?:path|re_path|url)\(\s*[r]?["\']([^"\']+)["\']'
    ), 1, lambda m: "URL pattern"),
    # Rails/Laravel: Route::get("/path", ...), get "/path" => ...
    (re.compile(
        r'Route::\w+\(\s*["\']([^"\']+)["\']'
    ), 1, lambda m: "route"),

    # --- JSX/HTML route components ---
    # <Route path="/path">, <Link to="/path">, <a href="/path">
    (re.compile(
        r'<(?:Route|Link|NavLink|Redirect)\s+(?:path|to|href)\s*=\s*[{]?\s*["\'](/[^"\']*)["\']'
    ), 1, lambda m: "page"),

    # --- String-literal URL paths (fetch, axios, http calls) ---
    # fetch("/api/..."), axios.get("/api/..."), http.get("/api/...")
    (re.compile(
        r'(?:fetch|axios|http|request|got|ky|ofetch|useFetch|\$fetch)\s*'
        r'(?:\.\w+)?\s*\(\s*[`"\'](/[^`"\']+)[`"\']'
    ), 1, lambda m: "API call"),

    # --- Go / Rust / Java style ---
    # http.HandleFunc("/path", ...), HandleFunc("/path", ...)
    # .route("/path"), #[get("/path")]
    (re.compile(
        r'(?:HandleFunc|Handle|ServeMux|route|Mount)\s*\(\s*["\'](/[^"\']+)["\']'
    ), 1, lambda m: "handler"),
    (re.compile(
        r'#\[(get|post|put|patch|delete)\(\s*["\']([^"\']+)["\']'
    ), 2, lambda m: m.group(1).upper()),
]


def detect_verification_urls(project_root=None):
    """Scan all source files for URL/route patterns, framework-agnostic.

    Walks the project tree, reads every source file (skipping vendored dirs),
    and applies generic regex patterns that catch routes across Python, JS/TS,
    Go, Rust, Ruby, PHP, Java, and others.

    Returns a dict mapping route paths to short descriptions.
    """
    if project_root is None:
        project_root = os.getcwd()

    urls = {}

    for dirpath, dirnames, filenames in os.walk(project_root):
        # Prune skipped directories in-place so os.walk doesn't descend
        dirnames[:] = [d for d in dirnames if d not in _SKIP_DIRS]

        for fname in filenames:
            ext = os.path.splitext(fname)[1].lower()
            if ext not in _SOURCE_EXTS:
                continue
            filepath = os.path.join(dirpath, fname)
            try:
                with open(filepath, "r", encoding="utf-8", errors="ignore") as f:
                    content = f.read()
            except Exception:
                continue

            rel = os.path.relpath(filepath, project_root).replace("\\", "/")

            for pattern, group_idx, label_fn in _URL_PATTERNS:
                for match in pattern.finditer(content):
                    path = match.group(group_idx)
                    # Normalize: ensure leading slash
                    if path and not path.startswith("/"):
                        path = "/" + path
                    # Clean template variables: /api/tasks/${id} → /api/tasks/:id
                    path = re.sub(r'\$\{[^}]+\}', ':param', path)
                    # Clean angle-bracket params: /api/tasks/<task_id> → /api/tasks/:task_id
                    path = re.sub(r'<(?:\w+:)?(\w+)>', r':\1', path)
                    # Clean curly-brace params: /api/tasks/{id} → /api/tasks/:id
                    path = re.sub(r'\{(\w+)\}', r':\1', path)
                    # Skip noise: too short, too long, regex fragments, wildcards
                    if not path or path == "/" or len(path) > 120:
                        continue
                    if re.search(r'[\^$*+?\\()\[\]|]', path):
                        continue  # regex metacharacters = not a real URL
                    if path.endswith('...') or '..' in path:
                        continue
                    if path not in urls:
                        label = label_fn(match)
                        urls[path] = f"{label} ({rel})"

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

        # Accept absolute URLs, file:// URIs, and local file paths
        ver_url = sub.get("verification_url")
        if ver_url and not (
            ver_url.startswith(("http://", "https://", "file://", "/"))
            or (len(ver_url) >= 3 and ver_url[1:3] == ":\\")
        ):
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
