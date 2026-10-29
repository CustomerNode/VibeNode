"""
Git operations — background fetch, status cache, and sync logic.
"""

import subprocess
import sys
import threading
import time

from .config import _VIBENODE_DIR

from .platform_utils import NO_WINDOW as _NO_WINDOW
from . import requirements_sync as _reqs
import logging as _logging

_log = _logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Git cache and lock
# ---------------------------------------------------------------------------

_git_cache = {"ahead": 0, "behind": 0, "uncommitted": False, "has_git": False, "ready": False,
              "branch": "", "default_branch": "", "on_default": True}
_git_fetch_lock = threading.Lock()
_sync_cooldown_until = 0.0  # epoch; bg refresh is suppressed until this time
_last_refresh_time = 0.0     # epoch; throttle refresh_if_idle
_REFRESH_MIN_INTERVAL = 45   # seconds; don't re-fetch more often than this


# ---------------------------------------------------------------------------
# Branch helpers
# ---------------------------------------------------------------------------

def _current_branch(proj) -> str:
    """Return the current branch name, or "" if detached HEAD / unknown."""
    try:
        r = subprocess.run(
            ["git", "-C", str(proj), "symbolic-ref", "--short", "-q", "HEAD"],
            capture_output=True, text=True, timeout=5, creationflags=_NO_WINDOW)
        if r.returncode == 0:
            return r.stdout.strip()
    except Exception:
        pass
    return ""


def _default_branch(proj) -> str:
    """Return the repo's default branch (e.g. 'main') via origin/HEAD, or "".

    Empty string means "couldn't determine" — callers must treat that as
    "don't gate" (fail open) so we never block a normal sync on uncertainty.
    """
    try:
        r = subprocess.run(
            ["git", "-C", str(proj), "symbolic-ref", "--short", "-q",
             "refs/remotes/origin/HEAD"],
            capture_output=True, text=True, timeout=5, creationflags=_NO_WINDOW)
        if r.returncode == 0:
            name = r.stdout.strip()  # e.g. "origin/main"
            return name.split("/", 1)[1] if "/" in name else name
    except Exception:
        pass
    return ""


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
    branch = _current_branch(proj)
    default_branch = _default_branch(proj)
    # Fail open: if we can't determine the default branch, treat as "on default"
    # so we never warn spuriously.
    on_default = (not default_branch) or (branch == default_branch)
    _git_cache.update({"has_git": True, "ahead": ahead, "behind": behind,
                       "uncommitted": uncommitted, "ready": True,
                       "branch": branch, "default_branch": default_branch,
                       "on_default": on_default})


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
# Dependencies pulled in by an update
# ---------------------------------------------------------------------------

def _requirements_blob(proj) -> str:
    """The blob id of requirements.txt at HEAD, or '' when unknown.

    Compared before and after a pull: a changed blob means the update added,
    removed or re-pinned a dependency, and pip must run (2026-10-23).
    """
    try:
        r = subprocess.run(
            ["git", "-C", str(proj), "rev-parse", "HEAD:requirements.txt"],
            capture_output=True, text=True, timeout=5, creationflags=_NO_WINDOW)
        if r.returncode == 0:
            return r.stdout.strip()
    except Exception:
        pass
    return ""


def _install_pulled_requirements(proj) -> dict:
    """Run pip for the requirements the pull just landed, with the server's
    interpreter.  Never raises; the result feeds the sync response."""
    return _reqs.install_requirements(sys.executable, proj / "requirements.txt")


# ---------------------------------------------------------------------------
# Commit message for a Publish
# ---------------------------------------------------------------------------
# 74 of the 100 commits before 2026-10-23 were "Update VibeNode <timestamp>":
# nothing said what changed or whether the tests ran.  The message is now the
# user's one-line summary (or an auto summary of the staged areas), then a
# line recording the publish gate outcome.

_SUMMARY_MAX = 120
_AREAS_SHOWN = 6


def _area_of(path: str) -> str:
    parts = [p for p in path.replace("\\", "/").split("/") if p]
    if len(parts) >= 3:
        return parts[0] + "/" + parts[1]
    if len(parts) == 2:
        return parts[0]
    return parts[0] if parts else ""


def summarize_staged(paths) -> str:
    """'Update app/routes, static/js, tests (14 files)' from staged paths."""
    paths = [p for p in paths if p and p.strip()]
    if not paths:
        return "Update VibeNode"
    areas = []
    for p in paths:
        a = _area_of(p.strip())
        if a and a not in areas:
            areas.append(a)
    shown = ", ".join(areas[:_AREAS_SHOWN]) + (", …" if len(areas) > _AREAS_SHOWN else "")
    n = len(paths)
    return "Update %s (%d file%s)" % (shown, n, "" if n == 1 else "s")


def _staged_paths(proj) -> list:
    try:
        r = subprocess.run(
            ["git", "-C", str(proj), "diff", "--cached", "--name-only"],
            capture_output=True, text=True, timeout=10, creationflags=_NO_WINDOW)
        if r.returncode == 0:
            return [l.strip() for l in r.stdout.splitlines() if l.strip()]
    except Exception:
        pass
    return []


def format_publish_gate(gate) -> str:
    """One line recording how the publish gate went.

    ``gate`` is the dict the browser sends: ``{"mode": "fast"|"full"|"skipped",
    "passed", "skipped", "failed", "seconds", "anyway": bool}``.  Missing or
    unreadable means the caller did not say, which is recorded as skipped.
    """
    gate = gate if isinstance(gate, dict) else {}
    mode = str(gate.get("mode") or "skipped").lower()
    if mode not in ("fast", "full"):
        return "Publish gate: skipped (no tests run)"

    def _n(key):
        try:
            return int(gate.get(key) or 0)
        except (TypeError, ValueError):
            return 0
    line = "Publish gate: %s (%d passed, %d skipped, %d failed, %ds)" % (
        mode, _n("passed"), _n("skipped"), _n("failed"), _n("seconds"))
    if gate.get("anyway"):
        line += ", published anyway"
    return line


def build_commit_message(summary: str, gate, staged_paths) -> str:
    summary = " ".join(str(summary or "").split())[:_SUMMARY_MAX].strip()
    if not summary:
        summary = summarize_staged(staged_paths)
    return summary + "\n\n" + format_publish_gate(gate) + "\n"


# ---------------------------------------------------------------------------
# Git sync logic
# ---------------------------------------------------------------------------

def _unmerged_paths(proj) -> list:
    """Files git still marks as conflicted (unmerged), e.g. after a stash pop clash."""
    try:
        r = subprocess.run(["git", "-C", str(proj), "diff", "--name-only", "--diff-filter=U"],
                           capture_output=True, text=True, timeout=10, creationflags=_NO_WINDOW)
    except Exception:
        return []
    return [p for p in r.stdout.splitlines() if p.strip()] if r.returncode == 0 else []


def _run_post_update(proj) -> bool:
    """Run ``app/post_update.py`` in a fresh interpreter from the pulled tree.
    The running server's own modules are the OLD code, so importing them
    here would run the old migrations.  Never fails the update."""
    try:
        r = subprocess.run([sys.executable, "-m", "app.post_update"], cwd=str(proj),
                           capture_output=True, text=True, timeout=120,
                           creationflags=_NO_WINDOW)
        _log.info("post_update rc=%s %s", r.returncode, (r.stdout or r.stderr or "").strip()[-500:])
        return r.returncode == 0
    except Exception as e:
        _log.warning("post_update failed: %s", e)
        return False


def do_git_sync(action: str, confirm_branch: bool = False,
                summary: str = "", gate=None) -> dict:
    """
    Perform git sync (pull, push, or both).
    Returns {"ok": bool, "messages": list[str]} plus, after a pull that changed
    requirements.txt, ``pip_ran`` / ``pip_ok`` / ``pip_installed`` /
    ``pip_output_tail``.

    ``summary`` is the user's one-line "What changed" for the commit (auto
    generated from the staged paths when empty); ``gate`` records the publish
    gate outcome (see ``format_publish_gate``).

    If the working copy is not on the repo's default branch, this short-circuits
    and returns {"needs_branch_confirm": True, ...} WITHOUT touching git, so a
    user (or agent) who landed on a feature branch via Fork doesn't silently
    commit/push there. Pass confirm_branch=True to proceed on the current branch.
    """
    proj = _VIBENODE_DIR
    if not (proj / ".git").is_dir():
        return {"ok": False, "messages": ["VibeNode has no git repo."]}

    # ── Branch guard (short-circuit before any git operation) ──
    if not confirm_branch:
        cur = _current_branch(proj)
        default = _default_branch(proj)
        if default and cur != default:
            branch_label = cur if cur else "a detached HEAD (no branch)"
            return {
                "ok": False,
                "needs_branch_confirm": True,
                "branch": cur,
                "default_branch": default,
                "messages": [
                    "You are on branch '" + branch_label + "', not '" + default + "'.",
                    "Syncing now will publish to '" + branch_label + "', not your main "
                    "app. Switch to '" + default + "' first, or continue only if you "
                    "meant to work on this branch.",
                ],
            }

    messages = []
    ok = True
    pip_result = None

    if action in ("pull", "both"):
        # A clash left by an earlier Update's stash pop blocks everything after
        # it: `git stash` refuses ("needs merge") and the pull fails with git's
        # raw "you have unmerged files" (2026-10-29, the second press of Update
        # after a clash).  Say what it is instead, and touch nothing.
        clashed = _unmerged_paths(proj)
        if clashed:
            return {"ok": False, "messages": [
                "An earlier Update left a clash between your local changes and the pulled "
                "code in: " + ", ".join(clashed) + ".",
                "Nothing was pulled or published. Resolve the clash, then Update again.",
            ], "conflicts": clashed}
        reqs_before = _requirements_blob(proj)
        stash = subprocess.run(["git", "-C", str(proj), "stash", "--include-untracked"],
                               capture_output=True, text=True, timeout=15, creationflags=_NO_WINDOW)
        # Only a stash that ran and saved something may be popped later: a
        # failed `git stash` must never lead to popping an older, unrelated one.
        stashed = stash.returncode == 0 and "No local changes" not in stash.stdout
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
            pop = subprocess.run(["git", "-C", str(proj), "stash", "pop"],
                                 capture_output=True, text=True, creationflags=_NO_WINDOW)
            # A pop that clashes with the pulled code leaves conflict markers in
            # the working tree and the stash in place. Carrying on would let the
            # push step `git add -A` and publish the markers (2026-10-28: it left
            # app/git_scanner.py with <<<<<<< lines; only a scan finding stopped
            # the push). Stop here; nothing is lost, the stash is still there.
            clashed = _unmerged_paths(proj)
            if pop.returncode != 0 or clashed:
                where = ", ".join(clashed) if clashed else (pop.stderr or pop.stdout or "").strip()
                return {"ok": False, "messages": messages + [
                    "Pulled the update, but your local changes clashed with it in: " + where + ".",
                    "Nothing was published. Your changes are kept (git stash); resolve the clash, then Update again.",
                ], "conflicts": clashed}
        if "Already up to date" in out:
            messages.append("VibeNode is already up to date.")
        else:
            messages.append("Pulled latest VibeNode updates from remote.")
        # The update changed requirements.txt: install what it asks for, with
        # the interpreter that runs this server.  A pip failure is reported
        # but never undoes the pull.
        reqs_after = _requirements_blob(proj)
        if reqs_after and reqs_after != reqs_before:
            pip_result = _install_pulled_requirements(proj)
            if pip_result["pip_ok"]:
                n = pip_result.get("pip_installed", 0)
                messages.append("Installed %d new or updated dependenc%s." % (n, "y" if n == 1 else "ies")
                                if n else "Dependencies already satisfied.")
            else:
                messages.append("Dependency install failed; the code update landed but some "
                                "features may not work until it is fixed. See the pip output below.")
        # Data migrations that ship with the update run NOW, with the pulled
        # code, not at the web server's next restart (app/post_update.py).
        if "Already up to date" not in out:
            _run_post_update(proj)

    if action in ("push", "both") and ok:
        # A clash left over from an earlier run is never committed: `git add -A`
        # would mark it resolved and publish the conflict markers.
        clashed = _unmerged_paths(proj)
        if clashed:
            return {"ok": False, "messages": messages + [
                "⛔ Not published: unresolved clash in " + ", ".join(clashed) + ".",
                "Resolve it, then Update again.",
            ], "conflicts": clashed}

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

            msg = build_commit_message(summary, gate, _staged_paths(proj))
            commit = subprocess.run(["git", "-C", str(proj), "commit", "-m", msg],
                                    capture_output=True, text=True, timeout=10, creationflags=_NO_WINDOW)
            if commit.returncode != 0:
                err = (commit.stderr.strip() or commit.stdout.strip() or
                       "git commit failed with no output")
                hint = ""
                if "Author identity unknown" in err or "Please tell me who you are" in err:
                    hint = (" Your git identity is missing. Run these in PowerShell:\n"
                            '   git config --global user.name "Your Name"\n'
                            '   git config --global user.email "you@example.com"')
                return {"ok": False, "messages": messages + [
                    "Could not save your changes: " + err + hint,
                ]}
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

    result = {"ok": ok, "messages": messages}
    if pip_result is not None:
        result.update({k: pip_result[k] for k in
                       ("pip_ran", "pip_ok", "pip_installed", "pip_output_tail")})
    return result
