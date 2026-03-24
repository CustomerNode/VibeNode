"""
Git operations — background fetch, status cache, and sync logic.
"""

import subprocess
import threading

from .config import _CLAUDECODEGUI_DIR

# ---------------------------------------------------------------------------
# Git cache and lock
# ---------------------------------------------------------------------------

_git_cache = {"ahead": 0, "behind": 0, "uncommitted": False, "has_git": False, "ready": False}
_git_fetch_lock = threading.Lock()


# ---------------------------------------------------------------------------
# Background git fetch
# ---------------------------------------------------------------------------

def _bg_git_fetch():
    """Run git fetch + status in background, update cache when done."""
    proj = _CLAUDECODEGUI_DIR
    if not (proj / ".git").is_dir():
        _git_cache.update({"has_git": False, "ready": True})
        return
    try:
        subprocess.run(["git", "-C", str(proj), "fetch", "--quiet"],
                       capture_output=True, timeout=15)
    except Exception:
        pass
    ahead = behind = 0
    try:
        r = subprocess.run(
            ["git", "-C", str(proj), "rev-list", "--left-right", "--count", "HEAD...@{upstream}"],
            capture_output=True, text=True, timeout=5)
        if r.returncode == 0:
            parts = r.stdout.strip().split()
            if len(parts) == 2:
                ahead, behind = int(parts[0]), int(parts[1])
    except Exception:
        pass
    uncommitted = False
    try:
        dirty = subprocess.run(["git", "-C", str(proj), "status", "--porcelain"],
                               capture_output=True, text=True, timeout=5)
        # Ignore .claude/worktrees — internal Claude Code artifacts, not real changes
        lines = [l for l in dirty.stdout.strip().splitlines()
                 if not l.lstrip(" M?!").startswith(".claude/worktrees")]
        uncommitted = bool(lines)
    except Exception:
        pass
    _git_cache.update({"has_git": True, "ahead": ahead, "behind": behind,
                       "uncommitted": uncommitted, "ready": True})


def start_bg_fetch():
    """Kick off first fetch immediately at startup."""
    threading.Thread(target=_bg_git_fetch, daemon=True).start()


def refresh_if_idle():
    """Trigger a background refresh if no fetch is currently in progress."""
    if not _git_fetch_lock.locked():
        def _refresh():
            with _git_fetch_lock:
                _bg_git_fetch()
        threading.Thread(target=_refresh, daemon=True).start()


def get_git_cache() -> dict:
    """Return the current git cache dict."""
    return _git_cache


# ---------------------------------------------------------------------------
# Git sync logic
# ---------------------------------------------------------------------------

def do_git_sync(action: str) -> dict:
    """
    Perform git sync (pull, push, or both).
    Returns {"ok": bool, "messages": list[str]}.
    """
    proj = _CLAUDECODEGUI_DIR
    if not (proj / ".git").is_dir():
        return {"ok": False, "messages": ["ClaudeCodeGUI has no git repo."]}

    messages = []
    ok = True

    if action in ("pull", "both"):
        stash = subprocess.run(["git", "-C", str(proj), "stash", "--include-untracked"],
                               capture_output=True, text=True, timeout=15)
        stashed = "No local changes" not in stash.stdout
        pull = subprocess.run(
            ["git", "-C", str(proj), "pull", "--rebase", "-X", "theirs"],
            capture_output=True, text=True, timeout=30)
        if pull.returncode != 0:
            subprocess.run(["git", "-C", str(proj), "rebase", "--abort"], capture_output=True)
            pull2 = subprocess.run(["git", "-C", str(proj), "pull", "-X", "theirs"],
                                   capture_output=True, text=True, timeout=30)
            if pull2.returncode != 0:
                ok = False
                messages.append("Could not pull: " + pull2.stderr.strip())
                return {"ok": ok, "messages": messages}
            out = pull2.stdout.strip()
        else:
            out = pull.stdout.strip()
        if stashed:
            subprocess.run(["git", "-C", str(proj), "stash", "pop"], capture_output=True)
        if "Already up to date" in out:
            messages.append("Claude Code GUI is already up to date.")
        else:
            messages.append("Pulled latest Claude Code GUI updates from remote.")

    if action in ("push", "both") and ok:
        # Auto-commit any uncommitted changes before pushing
        dirty = subprocess.run(["git", "-C", str(proj), "status", "--porcelain"],
                               capture_output=True, text=True, timeout=5)
        dirty_lines = [l for l in dirty.stdout.strip().splitlines()
                       if not l.lstrip(" M?!").startswith(".claude/worktrees")]
        if dirty_lines:
            from datetime import datetime as _dt
            subprocess.run(["git", "-C", str(proj), "add", "-A"], capture_output=True)
            msg = "Update Claude Code GUI " + _dt.now().strftime("%Y-%m-%d %H:%M")
            subprocess.run(["git", "-C", str(proj), "commit", "-m", msg],
                           capture_output=True, text=True, timeout=10)
            messages.append("Saved your local changes as a new version.")
        push = subprocess.run(["git", "-C", str(proj), "push"],
                               capture_output=True, text=True, timeout=30)
        if push.returncode != 0:
            ok = False
            messages.append("Could not push: " + (push.stderr.strip() or push.stdout.strip()))
        else:
            messages.append("Your Claude Code GUI changes have been pushed to remote.")

    # Update git cache immediately so the next pollGitStatus gets fresh data
    try:
        r = subprocess.run(
            ["git", "-C", str(proj), "rev-list", "--left-right", "--count", "HEAD...@{upstream}"],
            capture_output=True, text=True, timeout=5)
        a = b = 0
        if r.returncode == 0:
            parts = r.stdout.strip().split()
            if len(parts) == 2:
                a, b = int(parts[0]), int(parts[1])
        d = subprocess.run(["git", "-C", str(proj), "status", "--porcelain"],
                           capture_output=True, text=True, timeout=5)
        dlines = [l for l in d.stdout.strip().splitlines()
                  if not l.lstrip(" M?!").startswith(".claude/worktrees")]
        _git_cache.update({"has_git": True, "ahead": a, "behind": b,
                           "uncommitted": bool(dlines), "ready": True})
    except Exception:
        pass

    return {"ok": ok, "messages": messages}
