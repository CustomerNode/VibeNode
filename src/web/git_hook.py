"""Git's own hooks run the Publish scan on every commit and push of this repository.

VibeNode's Update scans before it publishes, and the Session Engine refuses a
session's write, but a commit can also come from a terminal, an editor, or a
session running ``git`` itself.  This repository is public, so git checks it
too, on every machine:

- ``pre-commit``: every staged file, as staged, through ``git_scanner.scan_text``
  (names, keys, personal data, swearing, notes about people, and runs of the
  developer's own typed words from ``src/shared/user_words.py``).
- ``commit-msg``: the message, which is published too.
- ``pre-push``: the added lines and messages of every commit being pushed.

The hooks live in ``scripts/git-hooks`` (tracked); ``ensure_installed`` points
``core.hooksPath`` at them, and VibeNode calls it at start.  A developer who
never runs VibeNode sets it once:  ``git config core.hooksPath scripts/git-hooks``.

A scan that cannot run (no git, an error) lets the commit through: Update still
scans before publishing.
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
HOOKS_DIR = "scripts/git-hooks"
_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


def _git(*args, input_bytes=None) -> subprocess.CompletedProcess:
    # Git runs a hook from the top of the repository being committed to.
    return subprocess.run(["git", *args], capture_output=True, input=input_bytes,
                          timeout=60, creationflags=_NO_WINDOW)


def _report(where: str, found: list) -> int:
    if not found:
        return 0
    print(f"\nVibeNode {where}: refused, this repository is public.", file=sys.stderr)
    for f in found[:20]:
        print(f"  {f.get('file', '?')}:{f.get('line', '?')}  {f.get('type')}: {f.get('desc', '')}",
              file=sys.stderr)
    if len(found) > 20:
        print(f"  ... and {len(found) - 20} more", file=sys.stderr)
    print("Reword it (made-up names, neutral words, no quotes of a person), then try again.\n",
          file=sys.stderr)
    return 1


def pre_commit() -> int:
    from web.git_scanner import scan_text, _BINARY_EXT
    r = _git("diff", "--cached", "--name-only", "--diff-filter=ACMR", "-z")
    if r.returncode != 0:
        return 0
    found = []
    for rel in filter(None, r.stdout.decode("utf-8", "replace").split("\0")):
        if _BINARY_EXT.search(rel):
            continue
        blob = _git("show", f":{rel}")
        if blob.returncode != 0 or len(blob.stdout) > 2_000_000:
            continue
        text = blob.stdout.decode("utf-8", "ignore")
        found += [{"file": rel, **f} for f in scan_text(rel, text)]
    return _report("pre-commit", found)


def commit_msg(path: str) -> int:
    from web.git_scanner import _identity_findings, _note_findings, _own_words_findings
    try:
        text = Path(path).read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return 0
    text = "\n".join(l for l in text.splitlines() if not l.startswith("#"))
    found = _identity_findings(text) + _note_findings(text) + _own_words_findings("COMMIT_MSG", text)
    return _report("commit message", [{"file": "commit message", **f} for f in found])


def pre_push(stdin: str) -> int:
    import re
    from web.git_scanner import (_BINARY_EXT, _SCANNER_ITSELF, _identity_findings, _note_findings,
                                 _own_words_findings)
    found = []
    for line in stdin.splitlines():
        parts = line.split()
        if len(parts) != 4 or set(parts[1]) == {"0"}:
            continue                         # a deleted ref publishes nothing
        local, remote = parts[1], parts[3]
        rng = [local, "--not", "--remotes"] if set(remote) == {"0"} else [f"{remote}..{local}"]
        r = _git("log", "-p", "--no-color", "--format=%x00%H%n%B", *rng)
        if r.returncode != 0:
            continue
        for chunk in r.stdout.decode("utf-8", "ignore").split("\x00")[1:]:
            sha, _, body = chunk.partition("\n")
            message, *files = re.split(r"(?m)^diff --git ", body)
            parts_to_scan = [("message", message)]
            for f in files:
                m = re.search(r"(?m)^\+\+\+ b/(.+)$", f)
                path = m.group(1).strip() if m else ""
                # The scanner's own file holds the very examples it refuses,
                # and Publish leaves it out too; binaries are not text.
                if not path or _SCANNER_ITSELF.search(path) or _BINARY_EXT.search(path):
                    continue
                parts_to_scan.append((path, "\n".join(l[1:] for l in f.splitlines()
                                                     if l.startswith("+") and not l.startswith("+++"))))
            for where, text in parts_to_scan:
                for f in _identity_findings(text) + _note_findings(text) + _own_words_findings("PUSH", text):
                    found.append({"file": f"commit {sha[:10]} {where}", **f})
    return _report("pre-push", found)


def ensure_installed(repo: Path = ROOT) -> str:
    """Point this clone's ``core.hooksPath`` at the tracked hooks.  Leaves a
    developer's own hooks alone.  Returns what it did."""
    try:
        cur = subprocess.run(["git", "-C", str(repo), "config", "--get", "core.hooksPath"],
                             capture_output=True, text=True, timeout=10, creationflags=_NO_WINDOW)
        now = cur.stdout.strip()
        if now == HOOKS_DIR:
            return "installed"
        if now:
            return f"left alone: core.hooksPath is {now}"
        own = repo / ".git" / "hooks"
        if own.is_dir() and any(not p.name.endswith(".sample") for p in own.iterdir() if p.is_file()):
            return "left alone: this clone has hooks of its own"
        if not (repo / HOOKS_DIR).is_dir():
            return "no hooks folder"
        subprocess.run(["git", "-C", str(repo), "config", "core.hooksPath", HOOKS_DIR],
                       capture_output=True, timeout=10, creationflags=_NO_WINDOW, check=True)
        return "installed now"
    except Exception as e:
        return f"not installed: {e}"


def main(argv: list) -> int:
    sys.path.insert(0, str(ROOT / "src"))
    try:
        if argv[:1] == ["pre-commit"]:
            return pre_commit()
        if argv[:1] == ["commit-msg"] and len(argv) > 1:
            return commit_msg(argv[1])
        if argv[:1] == ["pre-push"]:
            return pre_push(sys.stdin.read())
    except Exception as e:                   # never lose a commit to a broken scan
        print(f"VibeNode {argv[:1]}: scan skipped ({e})", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
