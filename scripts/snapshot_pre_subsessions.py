#!/usr/bin/env python3
"""
Phase 0 snapshot script for the VibeNode Subsessions feature.

Snapshots user-state directories that the Subsessions work will touch, into a
timestamped folder under ~/.vibenode/restore-points/.  Performs a byte-count
self-check after every copy and refuses to claim success on mismatch.

After a successful run, updates docs/plans/subsessions-spec.md §14.5 with the
captured tag, commit, created timestamp, and snapshot path.

Per docs/plans/subsessions-spec.md §14.1.

USAGE:
    python scripts/snapshot_pre_subsessions.py
    python scripts/snapshot_pre_subsessions.py --dry-run
    python scripts/snapshot_pre_subsessions.py --spec /custom/path/spec.md

This script makes NO destructive changes to the source files.  It only reads
from the source locations and writes COPIES into the snapshot directory plus
optionally updates the spec file.  It NEVER restarts any server.
"""

from __future__ import annotations

import argparse
import datetime as _dt
import json
import shutil
import subprocess
import sys
from pathlib import Path

# Resolve project root from this file's location; do NOT hardcode any user paths.
PROJECT_ROOT = Path(__file__).resolve().parents[1]
SPEC_PATH_DEFAULT = PROJECT_ROOT / "docs" / "plans" / "subsessions-spec.md"

TAG = "pre-subsessions-v1"


def _byte_count(path: Path) -> int:
    """Return total byte count: file size, or recursive sum for a directory."""
    if not path.exists():
        return 0
    if path.is_file():
        return path.stat().st_size
    total = 0
    for child in path.rglob("*"):
        if child.is_file():
            total += child.stat().st_size
    return total


def _copy_thing(src: Path, dst: Path) -> int:
    """Copy file or directory to dst.  Returns destination byte count."""
    dst.parent.mkdir(parents=True, exist_ok=True)
    if src.is_file():
        shutil.copy2(src, dst)
    else:
        # copytree requires dst not exist; we guarantee that because dst is
        # under a freshly-created timestamped folder.
        shutil.copytree(src, dst)
    return _byte_count(dst)


def _git_head_sha() -> str:
    result = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=PROJECT_ROOT,
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


def main() -> int:
    parser = argparse.ArgumentParser(description="Snapshot user-state before Subsessions work.")
    parser.add_argument(
        "--spec",
        default=str(SPEC_PATH_DEFAULT),
        help="Path to the spec file whose §14.5 should be filled in.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print what would be done without writing anything.",
    )
    args = parser.parse_args()

    try:
        commit_sha = _git_head_sha()
    except subprocess.CalledProcessError as exc:
        print(f"ERROR: could not read git HEAD: {exc}", file=sys.stderr)
        return 1

    now = _dt.datetime.now(_dt.timezone.utc)
    iso_compact = now.strftime("%Y%m%dT%H%M%SZ")
    iso_human = now.replace(microsecond=0).isoformat()

    restore_root = Path.home() / ".vibenode" / "restore-points" / f"pre-subsessions-{iso_compact}"

    # Items to snapshot.  Tuple = (label, source path).
    home = Path.home()
    items = [
        ("compose-projects", home / ".vibenode" / "compose-projects"),
        ("gui_active_sessions.json", home / ".claude" / "gui_active_sessions.json"),
        ("kanban_config.json", PROJECT_ROOT / "kanban_config.json"),
    ]

    print("=== Subsessions Phase 0 snapshot ===")
    print(f"Tag:        {TAG}")
    print(f"Commit:     {commit_sha}")
    print(f"Created:    {iso_human}")
    print(f"Snapshot:   {restore_root}")
    print()

    if args.dry_run:
        print("(dry-run; no files written)")
        for label, src in items:
            print(f"  would snapshot: {label}  exists={src.exists()}  source={src}")
        return 0

    restore_root.mkdir(parents=True, exist_ok=True)

    manifest = {
        "tag": TAG,
        "commit": commit_sha,
        "created": iso_human,
        "snapshot_dir": str(restore_root),
        "items": [],
    }

    for label, src in items:
        dst = restore_root / label
        if not src.exists():
            print(f"  [skip]      {label}  (source does not exist: {src})")
            manifest["items"].append(
                {
                    "name": label,
                    "source": str(src),
                    "destination": str(dst),
                    "existed": False,
                    "bytes_source": 0,
                    "bytes_destination": 0,
                    "self_check_ok": True,
                }
            )
            continue

        src_bytes = _byte_count(src)
        dst_bytes = _copy_thing(src, dst)
        ok = src_bytes == dst_bytes
        marker = "OK" if ok else "MISMATCH"
        print(f"  [{marker}]        {label}  src={src_bytes}B  dst={dst_bytes}B")

        manifest["items"].append(
            {
                "name": label,
                "source": str(src),
                "destination": str(dst),
                "existed": True,
                "bytes_source": src_bytes,
                "bytes_destination": dst_bytes,
                "self_check_ok": ok,
            }
        )

        if not ok:
            print(
                f"\nERROR: byte-count mismatch for {label}.  Snapshot aborted; do NOT proceed to build.",
                file=sys.stderr,
            )
            return 2

    manifest_path = restore_root / "MANIFEST.json"
    manifest_path.write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    print(f"\nManifest:   {manifest_path}")

    # Fill in spec §14.5 if the placeholder is present.
    spec_path = Path(args.spec)
    if spec_path.exists():
        text = spec_path.read_text(encoding="utf-8")
        placeholder = (
            "TAG:      pre-subsessions-v1\n"
            "COMMIT:   <FILL ME — set by scripts/snapshot_pre_subsessions.py on first run>\n"
            "CREATED:  <ISO8601 UTC>\n"
            "SNAPSHOT: ~/.vibenode/restore-points/pre-subsessions-<ISO8601>/"
        )
        filled = (
            f"TAG:      {TAG}\n"
            f"COMMIT:   {commit_sha}\n"
            f"CREATED:  {iso_human}\n"
            f"SNAPSHOT: {restore_root}"
        )
        if placeholder in text:
            spec_path.write_text(text.replace(placeholder, filled), encoding="utf-8")
            print(f"Spec §14.5 updated in {spec_path}")
        else:
            print(
                "NOTE: §14.5 placeholder not found in spec (already filled, or text differs); spec left unchanged.",
                file=sys.stderr,
            )
    else:
        print(f"NOTE: spec file not found at {spec_path}; skipping §14.5 update.", file=sys.stderr)

    print("\nSnapshot complete.  Self-check passed.  Phase 0 ready.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
