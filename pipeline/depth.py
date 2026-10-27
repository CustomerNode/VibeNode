"""Depth is a rule computed from the diff, never an opinion about the task.

    python -m pipeline.depth <run-dir>        print the depth the diff requires

``full`` when any changed file is protected, or the change is larger than a
bounded change; ``light`` otherwise.  Light depth drops the Plan Team and the
Review Team.  It never drops the independent test on the final tree, checker
provenance, or the audit's fourth question.

Protected, by default: anything under ``daemon/`` or ``common/``;
authentication, authorization, billing, payment and permission code; schemas
and migrations; IPC and WebSocket contracts; any file containing a
``PERF-CRITICAL`` marker.  A repository adds its own patterns in
``.pipeline-protected`` at its root, one glob per line, ``#`` for comments.
The harness recomputes the depth at grading time; the stages required are
the ones the diff demands, whatever an agent wrote in ``run.json``.
"""

from __future__ import annotations

import argparse
import fnmatch
import json
import re
import sys
from pathlib import Path

from pipeline.evidence import RUNS_SUBDIR, _excluded, _is_git_repo, git, load_run

# A "bounded change" (spec 6.6): light depth is allowed up to this many
# changed files and this many changed lines, counted against the base revision.
LIGHT_MAX_FILES = 5
LIGHT_MAX_LINES = 200

DEFAULT_PROTECTED = (
    "daemon/*", "daemon/**", "common/*", "common/**",
    "*auth*", "**/*auth*", "*permission*", "**/*permission*",
    "*billing*", "**/*billing*", "*payment*", "**/*payment*",
    "*schema*", "**/*schema*", "*migration*", "**/*migration*", "**/migrations/**",
    "*ipc*", "**/*ipc*", "**/ws_events.py", "**/daemon_server.py", "**/daemon_client.py",
    ".github/**", "requirements*.txt", "pyproject.toml", "setup.py", "setup.cfg",
)
PERF_MARKER = re.compile(r"PERF-CRITICAL")


def repo_protected_patterns(repo: Path) -> list[str]:
    p = repo / ".pipeline-protected"
    if not p.is_file():
        return []
    out = []
    for line in p.read_text(encoding="utf-8", errors="replace").splitlines():
        line = line.strip()
        if line and not line.startswith("#"):
            out.append(line)
    return out


def changed_files(repo: Path, base_rev: str) -> list[str]:
    """Paths changed since *base_rev* plus untracked files, run dir excluded."""
    files: set[str] = set()
    if _is_git_repo(repo) and base_rev not in ("no-git", "no-commits", ""):
        out = git(repo, "diff", "--name-only", base_rev, "--", ".", f":(exclude){RUNS_SUBDIR.as_posix()}", check=False)
        files.update(l.strip().replace("\\", "/") for l in out.splitlines() if l.strip())
        untracked = git(repo, "ls-files", "--others", "--exclude-standard", "-z", check=False).split("\0")
        files.update(u.replace("\\", "/") for u in untracked if u)
    return sorted(f for f in files if not _excluded(f))


def changed_lines(repo: Path, base_rev: str) -> int:
    """Added plus removed lines since *base_rev*, plus every line of each
    untracked file (a new file is all additions)."""
    total = 0
    if _is_git_repo(repo) and base_rev not in ("no-git", "no-commits", ""):
        out = git(repo, "diff", "--numstat", base_rev, "--", ".", f":(exclude){RUNS_SUBDIR.as_posix()}", check=False)
        for line in out.splitlines():
            parts = line.split("\t")
            if len(parts) >= 2:
                for n in parts[:2]:
                    if n.isdigit():
                        total += int(n)
        untracked = git(repo, "ls-files", "--others", "--exclude-standard", "-z", check=False).split("\0")
        for u in untracked:
            if u and not _excluded(u):
                try:
                    total += sum(1 for _ in open(repo / u, encoding="utf-8", errors="replace"))
                except OSError:
                    pass
    return total


def _matches(rel: str, patterns) -> str | None:
    rel = rel.replace("\\", "/")
    for pat in patterns:
        if fnmatch.fnmatch(rel, pat) or fnmatch.fnmatch(rel.rsplit("/", 1)[-1], pat):
            return pat
    return None


def _has_perf_marker(repo: Path, rel: str) -> bool:
    p = repo / rel
    try:
        if p.is_file() and p.stat().st_size < 4_000_000:
            return PERF_MARKER.search(p.read_text(encoding="utf-8", errors="replace")) is not None
    except OSError:
        pass
    return False


def _rev_known(repo: Path, rev: str) -> bool:
    """True when *rev* names a commit in *repo*."""
    try:
        git(repo, "cat-file", "-e", f"{rev}^{{commit}}")
        return True
    except (RuntimeError, OSError):
        return False


def required_depth(repo: Path, base_rev: str) -> tuple[str, list[str], dict]:
    """('full' | 'light', reasons, facts)."""
    if _is_git_repo(repo) and base_rev not in ("no-git", "no-commits", "") and not _rev_known(repo, base_rev):
        # ``git diff <unknown rev>`` fails and, with ``check=False``, reads as
        # "no changes": a rebased or garbage-collected base must fail closed,
        # never grade as a light change.
        facts = {"files": [], "lines": 0, "base_rev": base_rev}
        return "full", [f"base revision {base_rev[:12]} is not in this repository, so the change cannot be bounded"], facts
    files = changed_files(repo, base_rev)
    lines = changed_lines(repo, base_rev)
    patterns = list(DEFAULT_PROTECTED) + repo_protected_patterns(repo)
    reasons: list[str] = []
    for f in files:
        pat = _matches(f, patterns)
        if pat:
            reasons.append(f"{f} is protected ({pat})")
        elif _has_perf_marker(repo, f):
            reasons.append(f"{f} carries a PERF-CRITICAL marker")
    if len(files) > LIGHT_MAX_FILES:
        reasons.append(f"{len(files)} files changed (light allows {LIGHT_MAX_FILES})")
    if lines > LIGHT_MAX_LINES:
        reasons.append(f"{lines} lines changed (light allows {LIGHT_MAX_LINES})")
    facts = {"files": files, "lines": lines, "base_rev": base_rev}
    if reasons:
        return "full", reasons, facts
    if not files:
        return "light", ["no changes yet against the base revision"], facts
    return "light", [f"{len(files)} files, {lines} lines, none protected"], facts


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m pipeline.depth")
    ap.add_argument("run_dir")
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args(argv)
    run_dir = Path(a.run_dir).resolve()
    run = load_run(run_dir)
    repo = Path(run["repo"])
    depth, reasons, facts = required_depth(repo, run.get("base_rev", ""))
    run["depth"] = depth
    run["depth_reasons"] = reasons
    (run_dir / "run.json").write_text(json.dumps(run, indent=2, sort_keys=True), encoding="utf-8")
    if a.json:
        print(json.dumps({"depth": depth, "reasons": reasons, **facts}, indent=2))
    else:
        print(f"PIPELINE-DEPTH {depth}")
        for r in reasons:
            print(f"  - {r}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
