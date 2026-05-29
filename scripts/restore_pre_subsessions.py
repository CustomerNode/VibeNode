#!/usr/bin/env python3
"""
Restore script — undoes the Subsessions feature work and returns the project
to the pre-subsessions state.

Reads the manifest written by scripts/snapshot_pre_subsessions.py and:
  1. (Optional) git checkout the commit captured at snapshot time.
  2. Restores compose-projects/, gui_active_sessions.json, kanban_config.json
     from the snapshot, replacing any current contents.
  3. Removes ~/.claude/vibenode-state/ if present (the Subsessions feature
     created it; it didn't exist at snapshot time).

Confirms with the user (interactive y/N) before EACH destructive step unless
--yes is given.

Per docs/plans/subsessions-spec.md §14.2.

NEVER restarts any server.  Per CLAUDE.md, AI tools cannot restart the daemon.
The user restarts it manually via the GUI after running this script.

USAGE:
    python scripts/restore_pre_subsessions.py
    python scripts/restore_pre_subsessions.py --snapshot ~/.vibenode/restore-points/pre-subsessions-20260529T010203Z
    python scripts/restore_pre_subsessions.py --no-checkout    # restore data only
    python scripts/restore_pre_subsessions.py --yes            # skip prompts (dangerous)
"""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
RESTORE_ROOT = Path.home() / ".vibenode" / "restore-points"


def _confirm(prompt: str, assume_yes: bool) -> bool:
    if assume_yes:
        print(f"[--yes] {prompt}")
        return True
    try:
        ans = input(f"{prompt} [y/N]: ").strip().lower()
    except EOFError:
        return False
    return ans == "y"


def _latest_snapshot() -> Path | None:
    if not RESTORE_ROOT.exists():
        return None
    candidates = sorted(
        p for p in RESTORE_ROOT.iterdir() if p.is_dir() and p.name.startswith("pre-subsessions-")
    )
    return candidates[-1] if candidates else None


def main() -> int:
    parser = argparse.ArgumentParser(description="Restore pre-subsessions state.")
    parser.add_argument(
        "--snapshot",
        help="Specific snapshot directory.  Defaults to the most recent "
        "pre-subsessions-* under ~/.vibenode/restore-points/.",
    )
    parser.add_argument(
        "--commit",
        help="Commit SHA to check out.  Defaults to the SHA recorded in the snapshot manifest.",
    )
    parser.add_argument(
        "--no-checkout",
        action="store_true",
        help="Skip the git checkout step; restore on-disk data only.",
    )
    parser.add_argument(
        "--yes",
        action="store_true",
        help="Skip all interactive confirmations.  Dangerous — confirms every destructive step automatically.",
    )
    args = parser.parse_args()

    snapshot = Path(args.snapshot) if args.snapshot else _latest_snapshot()
    if snapshot is None or not snapshot.exists():
        print(
            f"ERROR: no snapshot found under {RESTORE_ROOT}.  Pass --snapshot explicitly.",
            file=sys.stderr,
        )
        return 1

    manifest_path = snapshot / "MANIFEST.json"
    if not manifest_path.exists():
        print(f"ERROR: manifest missing at {manifest_path}.", file=sys.stderr)
        return 1

    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    target_commit = args.commit or manifest["commit"]

    print("=== Subsessions restore ===")
    print(f"Snapshot:   {snapshot}")
    print(f"Tag:        {manifest['tag']}")
    print(f"Commit:     {target_commit}")
    print(f"Created:    {manifest['created']}")
    print()

    # Step 1: git checkout.
    if not args.no_checkout:
        prompt = f"Run 'git fetch origin' and 'git checkout {target_commit}' in {PROJECT_ROOT}?"
        if _confirm(prompt, args.yes):
            subprocess.run(["git", "-C", str(PROJECT_ROOT), "fetch", "origin"], check=True)
            subprocess.run(["git", "-C", str(PROJECT_ROOT), "checkout", target_commit], check=True)
            print(f"Checked out {target_commit}.")
        else:
            print("Skipped git checkout.")

    # Step 2+: per-item restore.
    for item in manifest["items"]:
        src = Path(item["destination"])  # snapshot copy is the source for restore
        dst = Path(item["source"])  # original location is the destination

        if not item["existed"]:
            if dst.exists():
                if _confirm(
                    f"Delete {dst}?  (Did not exist at snapshot time; exists now — probably added by the work.)",
                    args.yes,
                ):
                    if dst.is_dir():
                        shutil.rmtree(dst)
                    else:
                        dst.unlink()
                    print(f"Deleted {dst}.")
            continue

        if _confirm(
            f"Restore {dst} from snapshot?  (Will replace any current contents.)",
            args.yes,
        ):
            if dst.exists():
                if dst.is_dir():
                    shutil.rmtree(dst)
                else:
                    dst.unlink()
            if src.is_dir():
                shutil.copytree(src, dst)
            else:
                dst.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(src, dst)
            print(f"Restored {dst}.")

    # Step 3: remove ~/.claude/vibenode-state/ if present.
    vibenode_state = Path.home() / ".claude" / "vibenode-state"
    if vibenode_state.exists():
        if _confirm(
            f"Remove {vibenode_state}?  (Created by Subsessions feature; not in snapshot.)",
            args.yes,
        ):
            shutil.rmtree(vibenode_state)
            print(f"Removed {vibenode_state}.")

    print()
    print("Restore complete.")
    print()
    print("NOTE: This script does NOT restart any server.  Per VibeNode CLAUDE.md, AI tools")
    print("      cannot restart the daemon.  You must restart it manually via the GUI:")
    print("        System -> Restart Server -> Session Daemon")
    return 0


if __name__ == "__main__":
    sys.exit(main())
