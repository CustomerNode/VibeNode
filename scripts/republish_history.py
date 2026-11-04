#!/usr/bin/env python3
"""Re-scrub and republish VibeNode's public history in one step.

    python scripts/republish_history.py              # prepare locally, show the push command
    python scripts/republish_history.py --push       # also force-push to origin
    python scripts/republish_history.py --message "VibeNode"

Why this exists
---------------
VibeNode is a public repo. When something private has to be removed from the
*history* (not just the current tree), the only guarantee that nothing old
remains published is to replace the published history with a single clean
commit of the current tree (an orphan root). Doing that by hand is error-prone,
so this does it the same way every time, and refuses to publish anything the
commit scanner would flag.

What it does
------------
1. Refuses unless the working tree is clean and on the default branch.
2. Runs the full publish scanner over every tracked + publishable file. If ANY
   finding (a name, a key, a dated note, …) or a layout problem remains, it
   ABORTS: a republish must never carry a leak forward.
3. Saves the current tip on a local backup branch (``republish-backup-<ts>``).
4. Builds a new orphan commit whose tree is exactly the current one, and moves
   the default branch onto it (old history is gone from this line).
5. With ``--push``, force-pushes it to ``origin``. Without, prints the exact
   push command so a person confirms the destructive step.

After it is published, EVERY install already running current VibeNode adopts the
new history by itself on its next Update click — ``do_git_sync`` detects a
remote with no shared commit and resets onto it, keeping a backup branch and any
local edits (see ``src/web/git_ops.py`` ``_adopt_republished_history``). No user
is migrated by hand. An install still on code older than that auto-adopt runs
the one-time double-click fixer once, then is covered forever too.
"""
from __future__ import annotations

import argparse
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _git(repo: Path, *args: str, check: bool = True) -> str:
    r = subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True)
    if check and r.returncode != 0:
        sys.exit(f"git {' '.join(args)} failed:\n{r.stderr or r.stdout}")
    return r.stdout.strip()


def _default_branch(repo: Path) -> str:
    head = _git(repo, "symbolic-ref", "refs/remotes/origin/HEAD", check=False)
    if head:
        return head.rsplit("/", 1)[-1]
    cur = _git(repo, "rev-parse", "--abbrev-ref", "HEAD", check=False)
    return cur or "main"


def scan_tree(repo: Path) -> list:
    """Every publish-scan finding across the current tracked + publishable files,
    plus layout problems. Empty means safe to republish."""
    sys.path.insert(0, str(repo / "src"))
    from web import git_scanner as g
    out = _git(repo, "ls-files", "-c", "-o", "--exclude-standard")
    findings = []
    for rel in out.splitlines():
        p = repo / rel
        if not p.is_file() or p.is_symlink():
            continue
        try:
            raw = p.read_bytes()
        except Exception:
            continue
        if b"\x00" in raw[:8192]:
            continue  # binary: the identity rules cover it at publish; skip here
        for f in g.scan_text(rel.replace("\\", "/"), raw.decode("utf-8", "ignore")):
            findings.append({"file": rel, **f})
    for lp in g.layout_problems(repo):
        findings.append({"file": lp["file"], "type": "layout", "desc": lp["reason"]})
    return findings


def republish(repo: Path, message: str = "VibeNode", push: bool = False,
              allow_dirty: bool = False) -> dict:
    """Prepare (and optionally push) a fresh clean root. Returns a summary dict;
    raises SystemExit on a refusal. Safe to call in a test against a temp repo."""
    default = _default_branch(repo)
    cur = _git(repo, "rev-parse", "--abbrev-ref", "HEAD")
    if cur != default:
        sys.exit(f"On branch {cur!r}, not the default {default!r}. Switch first.")
    if not allow_dirty and _git(repo, "status", "--porcelain"):
        sys.exit("Working tree is not clean. Commit or stash first (or pass --allow-dirty).")

    findings = scan_tree(repo)
    if findings:
        return {"ok": False, "findings": findings, "default": default}

    old = _git(repo, "rev-parse", "HEAD")
    backup = "republish-backup-" + time.strftime("%Y%m%d-%H%M%S")
    _git(repo, "branch", "-f", backup, old)

    _git(repo, "checkout", "--orphan", "_republish_tmp")
    _git(repo, "add", "-A")
    _git(repo, "-c", "user.email=noreply@users.noreply.github.com", "-c", "user.name=VibeNode",
         "commit", "-q", "-m", message)
    new = _git(repo, "rev-parse", "HEAD")
    _git(repo, "branch", "-f", default, new)
    _git(repo, "checkout", "-q", default)
    _git(repo, "branch", "-D", "_republish_tmp", check=False)

    pushed = False
    if push:
        r = subprocess.run(["git", "-C", str(repo), "push", "--force", "origin", default],
                           capture_output=True, text=True)
        if r.returncode != 0:
            sys.exit(f"push failed:\n{r.stderr or r.stdout}\nLocal {default} is the new root; "
                     f"the old tip is on {backup}.")
        pushed = True
    return {"ok": True, "default": default, "old": old, "new": new, "backup": backup, "pushed": pushed}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Re-scrub and republish VibeNode history.")
    ap.add_argument("--message", default="VibeNode", help="commit message for the new root")
    ap.add_argument("--push", action="store_true", help="force-push to origin after preparing")
    ap.add_argument("--allow-dirty", action="store_true", help="skip the clean-tree check (not advised)")
    ap.add_argument("--repo", default=str(ROOT), help=argparse.SUPPRESS)
    args = ap.parse_args(argv)
    repo = Path(args.repo).resolve()

    print("Scanning the whole tree before republishing …")
    res = republish(repo, message=args.message, push=args.push, allow_dirty=args.allow_dirty)
    if not res["ok"]:
        print(f"\nREFUSING: {len(res['findings'])} thing(s) the publish scan would flag remain.")
        for f in res["findings"][:50]:
            print(f"  {f['file']}  {f.get('type')}  {f.get('desc', '')}")
        print("\nFix these first; a republish must not carry them into the new history.")
        return 1
    print("Clean.")
    print(f"Saved the old tip on {res['backup']} ({res['old'][:10]}).")
    print(f"New clean root on {res['default']}: {res['new'][:10]} (tree identical, no old history).")
    if res["pushed"]:
        print("Published.")
    else:
        print("\nPrepared locally. To publish, run:\n  "
              f"git push --force origin {res['default']}")
        print(f"(The old history is on {res['backup']} if you need to undo before pushing.)")
    print("\nUsers on current VibeNode adopt this automatically on their next Update click; "
          "no one is migrated by hand.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
