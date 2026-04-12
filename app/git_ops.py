"""
Git operations — background fetch, status cache, and sync logic.
"""

import subprocess
import sys
import threading
import time

from .config import _VIBENODE_DIR

from .platform_utils import NO_WINDOW as _NO_WINDOW

# ---------------------------------------------------------------------------
# Git cache and lock
# ---------------------------------------------------------------------------

_git_cache = {"ahead": 0, "behind": 0, "uncommitted": False, "has_git": False, "ready": False}
_git_fetch_lock = threading.Lock()
_sync_cooldown_until = 0.0  # epoch; bg refresh is suppressed until this time
_last_refresh_time = 0.0     # epoch; throttle refresh_if_idle
_REFRESH_MIN_INTERVAL = 45   # seconds; don't re-fetch more often than this


# ---------------------------------------------------------------------------
# Background git fetch
# ---------------------------------------------------------------------------

def _bg_git_fetch():
    """Run git fetch + status in background, update cache when done."""
    proj = _VIBENODE_DIR
    if not (proj / ".git").is_dir():
        _git_cache.update({"has_git": False, "ready": True})
        return
    try:
        subprocess.run(["git", "-C", str(proj), "fetch", "--quiet"],
                       capture_output=True, timeout=15, creationflags=_NO_WINDOW)
    except Exception:
        pass
    ahead = behind = 0
    try:
        r = subprocess.run(
            ["git", "-C", str(proj), "rev-list", "--left-right", "--count", "HEAD...@{upstream}"],
            capture_output=True, text=True, timeout=5, creationflags=_NO_WINDOW)
        if r.returncode == 0:
            parts = r.stdout.strip().split()
            if len(parts) == 2:
                ahead, behind = int(parts[0]), int(parts[1])
    except Exception:
        pass
    uncommitted = False
    try:
        dirty = subprocess.run(["git", "-C", str(proj), "status", "--porcelain"],
                               capture_output=True, text=True, timeout=5, creationflags=_NO_WINDOW)
        uncommitted = bool(dirty.stdout.strip())
    except Exception:
        pass
    _git_cache.update({"has_git": True, "ahead": ahead, "behind": behind,
                       "uncommitted": uncommitted, "ready": True})


def start_bg_fetch():
    """Kick off first fetch immediately at startup."""
    threading.Thread(target=_bg_git_fetch, daemon=True).start()


def refresh_if_idle():
    """Trigger a background refresh if no fetch is currently in progress,
    we're not in the post-sync cooldown, and enough time has elapsed since
    the last refresh. Prevents subprocess spam from rapid polling."""
    global _last_refresh_time
    now = time.time()
    if now < _sync_cooldown_until:
        return  # a sync just finished; trust its cache for a few seconds
    if now - _last_refresh_time < _REFRESH_MIN_INTERVAL:
        return  # already refreshed recently; use cached result
    if not _git_fetch_lock.locked():
        _last_refresh_time = now  # mark eagerly to prevent races
        def _refresh():
            with _git_fetch_lock:
                if time.time() < _sync_cooldown_until:
                    return  # re-check inside the lock
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
    proj = _VIBENODE_DIR
    if not (proj / ".git").is_dir():
        return {"ok": False, "messages": ["VibeNode has no git repo."]}

    messages = []
    ok = True

    if action in ("pull", "both"):
        stash = subprocess.run(["git", "-C", str(proj), "stash", "--include-untracked"],
                               capture_output=True, text=True, timeout=15, creationflags=_NO_WINDOW)
        stashed = "No local changes" not in stash.stdout
        pull = subprocess.run(
            ["git", "-C", str(proj), "pull", "--rebase", "-X", "theirs"],
            capture_output=True, text=True, timeout=30, creationflags=_NO_WINDOW)
        if pull.returncode != 0:
            subprocess.run(["git", "-C", str(proj), "rebase", "--abort"],
                          capture_output=True, creationflags=_NO_WINDOW)
            pull2 = subprocess.run(["git", "-C", str(proj), "pull", "-X", "theirs"],
                                   capture_output=True, text=True, timeout=30, creationflags=_NO_WINDOW)
            if pull2.returncode != 0:
                ok = False
                messages.append("Could not pull: " + pull2.stderr.strip())
                return {"ok": ok, "messages": messages}
            out = pull2.stdout.strip()
        else:
            out = pull.stdout.strip()
        if stashed:
            subprocess.run(["git", "-C", str(proj), "stash", "pop"],
                          capture_output=True, creationflags=_NO_WINDOW)
        if "Already up to date" in out:
            messages.append("VibeNode is already up to date.")
        else:
            messages.append("Pulled latest VibeNode updates from remote.")

    if action in ("push", "both") and ok:
        # ── Security scan before push ──
        from .git_scanner import scan_staged_files
        scan = scan_staged_files(proj)
        if not scan["ok"]:
            return {"ok": False, "messages": messages + [
                "\u26d4 Push blocked by security scan: " + scan["summary"],
                "Run a Code Scan from the toolbar for details."
            ], "scan": scan}

        # Auto-commit any uncommitted changes before pushing
        dirty = subprocess.run(["git", "-C", str(proj), "status", "--porcelain"],
                               capture_output=True, text=True, timeout=5, creationflags=_NO_WINDOW)
        if dirty.stdout.strip():
            from datetime import datetime as _dt
            subprocess.run(["git", "-C", str(proj), "add", "-A"],
                          capture_output=True, creationflags=_NO_WINDOW)

            # Re-scan after staging (git add -A might pick up new files)
            scan2 = scan_staged_files(proj)
            if not scan2["ok"]:
                # Unstage everything to prevent accidental commit
                subprocess.run(["git", "-C", str(proj), "reset", "HEAD"],
                              capture_output=True, creationflags=_NO_WINDOW)
                return {"ok": False, "messages": messages + [
                    "\u26d4 Push blocked after staging: " + scan2["summary"],
                    "Secrets or sensitive files were detected. Run Code Scan for details."
                ], "scan": scan2}

            msg = "Update VibeNode " + _dt.now().strftime("%Y-%m-%d %H:%M")
            subprocess.run(["git", "-C", str(proj), "commit", "-m", msg],
                           capture_output=True, text=True, timeout=10, creationflags=_NO_WINDOW)
            messages.append("Saved your local changes as a new version.")
        push = subprocess.run(["git", "-C", str(proj), "push"],
                               capture_output=True, text=True, timeout=30, creationflags=_NO_WINDOW)
        if push.returncode != 0:
            ok = False
            messages.append("Could not push: " + (push.stderr.strip() or push.stdout.strip()))
        else:
            messages.append("Your VibeNode changes have been pushed to remote.")

    # Update git cache immediately. After a successful push we know ahead=0
    # and uncommitted=False — don't re-check status --porcelain because a
    # concurrent process (e.g. an active agent session) can dirty the tree
    # between our commit and the check, making the button stick.
    global _sync_cooldown_until
    pushed_ok = ok and action in ("push", "both")
    with _git_fetch_lock:
        if pushed_ok:
            # Trust the result: commit+push succeeded → clean state
            _git_cache.update({"has_git": True, "ahead": 0, "behind": 0,
                               "uncommitted": False, "ready": True})
        else:
            # Pull-only or error: re-check actual state
            try:
                r = subprocess.run(
                    ["git", "-C", str(proj), "rev-list", "--left-right", "--count", "HEAD...@{upstream}"],
                    capture_output=True, text=True, timeout=5, creationflags=_NO_WINDOW)
                a = b = 0
                if r.returncode == 0:
                    parts = r.stdout.strip().split()
                    if len(parts) == 2:
                        a, b = int(parts[0]), int(parts[1])
                d = subprocess.run(["git", "-C", str(proj), "status", "--porcelain"],
                                   capture_output=True, text=True, timeout=5, creationflags=_NO_WINDOW)
                _git_cache.update({"has_git": True, "ahead": a, "behind": b,
                                   "uncommitted": bool(d.stdout.strip()), "ready": True})
            except Exception:
                pass
        _sync_cooldown_until = time.time() + 10  # suppress bg refresh for 10s

    return {"ok": ok, "messages": messages}
