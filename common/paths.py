"""Filesystem locations both layers agree on.

Anything under the user's home is computed at call time, never captured at
import: the test suite points ``Path.home()`` at a sandbox per test, and a
module constant baked at import would escape it.
"""

from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
DATA_DIR = REPO_ROOT / "data"


def claude_home() -> Path:
    """``~/.claude``: where Claude Code keeps its transcripts, and where
    VibeNode keeps its own ``gui_*`` state files beside them."""
    return Path.home() / ".claude"


def claude_projects_dir() -> Path:
    """``~/.claude/projects``: one directory per project, named by
    :func:`encode_cwd`."""
    return claude_home() / "projects"


def encode_cwd(cwd: str) -> str:
    """Encode a filesystem path into the Claude project directory name format.

    E.g. ``C:\\Users\\foo\\Bar`` → ``C--Users-foo-Bar``

    IMPORTANT: Underscores AND dots must also be replaced with dashes.  Claude
    Code's own project directory encoding converts both to dashes (e.g.
    ``customerNode_root`` → ``customerNode-root``, and ``.claude`` →
    ``-claude``), but the daemon's CWD preserves the original filesystem
    characters.  Without matching that exactly, sessions whose CWD contains
    underscores or dots fail the project filter silently — they appear to not
    exist even though the daemon has them running.

    The dot-encoding rule matters specifically for the ``_SYSTEM_UTILITY_CWD``
    filter in ``app/routes/project_api.py::api_projects`` and for
    ``_cleanup_system_sessions()`` in ``app/config.py``: the on-disk directory
    is ``C--Users-<user>--claude--system`` (encoded from ``~/.claude/_system``),
    and without the ``.`` → ``-`` rule the filter built the wrong string and
    the system utility project leaked into the user-facing project list AND
    its old JSONLs never got pruned.  Do NOT remove the ``.replace('.', '-')``.
    """
    return (
        cwd.replace("\\", "-")
        .replace("/", "-")
        .replace(":", "-")
        .replace("_", "-")
        .replace(".", "-")
    )


def resolve_sessions_dir(project: str, active_project: str,
                         projects_root: Path, repo_dir: Path) -> Path:
    """Return a project's session directory, auto-detecting if needed.

    When *project* is provided (encoded dir name), use it directly.
    Otherwise fall back to *active_project*, then auto-detect.

    Priority (when no explicit project):
      1. *active_project* (set by the UI project picker, web process only)
      2. Match based on the repo path (*repo_dir*)
      3. Most recently modified .jsonl across all projects

    Never mutates anything but may create *projects_root* if it is missing.
    The web calls this through ``app.config._sessions_dir`` with its own
    (monkeypatchable) globals; the engine calls it through
    :func:`sessions_dir`.
    """
    # --- explicit project parameter ---
    if project:
        if not project.startswith("subagents"):
            p = projects_root / project
            if p.is_dir():
                return p

    # --- active project ---
    if active_project:
        if not active_project.startswith("subagents"):
            p = projects_root / active_project
            if p.is_dir():
                return p

    # Derive from the repo's own path — Claude encodes paths with dashes
    # e.g. C-drive path -> C--path-segments-joined-with-dashes
    if not projects_root.is_dir():
        projects_root.mkdir(parents=True, exist_ok=True)
        return projects_root
    repo_path = encode_cwd(str(repo_dir))
    for d in projects_root.iterdir():
        if not d.is_dir() or d.name.startswith("subagents") or d.name.startswith("_"):
            continue
        # Case-insensitive match — Claude's encoding may differ in case
        if d.name.lower() == repo_path.lower():
            return d

    # Fallback: most recently modified .jsonl
    best, best_ts = None, 0.0
    if not projects_root.is_dir():
        return projects_root
    for d in projects_root.iterdir():
        if not d.is_dir() or d.name.startswith("subagents") or d.name.startswith("_"):
            continue
        for f in d.glob("*.jsonl"):
            if f.stat().st_mtime > best_ts:
                best_ts = f.stat().st_mtime
                best = d
    if best:
        return best
    return projects_root


def sessions_dir(project: str = "") -> Path:
    """The engine's view of :func:`resolve_sessions_dir`: no active project
    (only the web's project picker sets one), so ``project`` or auto-detect.
    Identical to what ``app.config._sessions_dir`` resolved to inside the
    engine process, where its active project was always empty."""
    return resolve_sessions_dir(project, "", claude_projects_dir(), REPO_ROOT)
