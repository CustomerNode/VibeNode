"""Depth is a rule computed from the diff, never an opinion about the task.

    python -m pipeline.depth <run-dir>        print the depth the diff requires

``full`` when any changed file is protected, a changed line carries a risk
signal, the change is larger than a bounded change, there is no base revision
to bound it against, or a checker escalated; ``light`` otherwise.  Light depth
drops the Plan Team and the Review Team.  It never drops the independent test
on the final tree, checker provenance, or the audit's fourth question.

Protected, by default: anything under ``daemon/`` or ``common/``;
authentication, authorization, billing, payment and permission code; schemas
and migrations; IPC and WebSocket contracts; the pipeline's own declarations;
any file containing a ``PERF-CRITICAL`` marker.  A repository adds its own
patterns in ``.pipeline-protected`` at its root, one glob per line, ``#`` for
comments.  ``pipeline.start`` copies that file into the run, and the depth is
computed from the COPY plus whatever the file says now: a run can add
protection, it cannot remove it by editing or deleting the file.

Risk signals are a short list of things an added or removed line can do that
the file count cannot see: run a shell, swallow an exception, skip a test,
remove an assertion or an access check, disable TLS, inject raw HTML.  One hit
is full depth.  A pure rename (identical content) does not count toward size.
A comment line counts only for a suppressed check, and a test file does not
count for signals about production capabilities (a subprocess, a thread, an
HTML or SQL fixture); whatever weakens the tests themselves still counts.

The harness recomputes the depth at grading time; the stages required are the
ones the diff demands, whatever an agent wrote in ``run.json``.  A checker that
ran ``pipeline.escalate`` makes it full, and that cannot be undone.
"""

from __future__ import annotations

import argparse
import fnmatch
import json
import re
import sys
from pathlib import Path

from pipeline.evidence import (RUNS_SUBDIR, _excluded, _is_git_repo, git, load_run, run_repo, untracked_files,
                               walk_files)
from pipeline.plan import PINNED_PROTECTED_FILE, PROTECTED_FILE

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
    # The pipeline's own declarations: weakening either is a full-depth change.
    ".pipeline-verify", ".pipeline-protected",
)
PERF_MARKER = re.compile(r"PERF-CRITICAL")

# Files the risk scan skips: prose about code is not code.
_PROSE_SUFFIXES = (".md", ".rst", ".txt", ".csv", ".svg")
_RISK_MAX_BYTES = 2_000_000

# What an ADDED line can do that deserves a reviewer.  Short on purpose.
ADDED_RISK = (
    (re.compile(r"shell\s*=\s*True"), "shell execution"),
    (re.compile(r"\bos\.system\s*\(|\bsubprocess\.(?:run|Popen|call|check_output|check_call)\s*\(|"
                r"\bchild_process\b|\bexecSync\s*\("), "process execution"),
    (re.compile(r"\beval\s*\(|\bexec\s*\(|\bnew Function\s*\("), "dynamic code execution"),
    (re.compile(r"^\s*except\b[^\n]*:\s*(?:pass|\.\.\.)\s*$"), "swallowed exception"),
    (re.compile(r"#\s*noqa\b|#\s*type:\s*ignore|eslint-disable|@ts-ignore|@ts-nocheck|#\s*pragma:\s*no cover"),
     "suppressed check"),
    (re.compile(r"pytest\.mark\.(?:skip|xfail)|@unittest\.skip|\bit\.skip\(|\bxit\(|\btest\.skip\("), "skipped test"),
    (re.compile(r"--no-verify\b|\bgit\s+push\b[^\n]*(?:--force\b|\s-f\b)|\brm\s+-rf\b|\bchmod\s+777\b|\bsudo\b"),
     "destructive or hook-bypassing command"),
    (re.compile(r"\bthreading\.(?:Thread|Lock|RLock)\b|\basyncio\.(?:create_task|Lock)\b|\bmultiprocessing\b|"
                r"\bnew Worker\("), "concurrency"),
    (re.compile(r"(?i)\b(?:password|passwd|secret|token|api[_-]?key)\s*[:=]\s*['\"][^'\"]{8,}['\"]"),
     "credential literal"),
    (re.compile(r"\bverify\s*=\s*False\b|rejectUnauthorized\s*:\s*false"), "TLS verification disabled"),
    (re.compile(r"\bdangerouslySetInnerHTML\b|\.innerHTML\s*=|\binsertAdjacentHTML\s*\("), "raw HTML injection"),
    (re.compile(r"(?i)\bDROP\s+TABLE\b|\bTRUNCATE\s+TABLE\b|\bALTER\s+TABLE\b"), "schema change"),
)
# What a REMOVED line protected.
REMOVED_RISK = (
    (re.compile(r"^\s*assert\b"), "removed assertion"),
    (re.compile(r"^\s*raise\b"), "removed raise"),
    (re.compile(r"@local_only\b|@login_required\b|@requires_auth\b|@require_permission\b|\bcheck_permission\s*\(|"
                r"\bauthorize\s*\("), "removed access control"),
    (re.compile(r"check\s*=\s*True"), "removed check=True"),
    (re.compile(r"^\s*def test_|^\s*it\s*\(\s*['\"]|^\s*test\s*\(\s*['\"]"), "removed test"),
)
_EXCEPT_RE = re.compile(r"^\s*except\b[^\n]*:\s*$")
_PASS_RE = re.compile(r"^\s*(?:pass|\.\.\.)\s*$")


def parse_patterns(text: str) -> list[str]:
    out = []
    for line in text.splitlines():
        line = line.strip()
        if line and not line.startswith("#"):
            out.append(line)
    return out


def repo_protected_patterns(repo: Path) -> list[str]:
    """The patterns in the repository's ``.pipeline-protected`` as it is NOW."""
    p = repo / PROTECTED_FILE
    if not p.is_file():
        return []
    return parse_patterns(p.read_text(encoding="utf-8", errors="replace"))


def pinned_protected_patterns(run_dir: Path) -> list[str]:
    """The patterns ``pipeline.start`` copied into the run; [] when the
    repository declared none at the start."""
    p = run_dir / PINNED_PROTECTED_FILE
    if not p.is_file():
        return []
    return parse_patterns(p.read_text(encoding="utf-8", errors="replace"))


def _has_base(repo: Path, base_rev: str) -> bool:
    return _is_git_repo(repo) and base_rev not in ("no-git", "no-commits", "")


def _exclude_arg() -> str:
    return f":(exclude){RUNS_SUBDIR.as_posix()}"


def renamed_files(repo: Path, base_rev: str) -> set[str]:
    """Old AND new paths of files that are pure renames (identical content)
    since *base_rev*; they do not count toward the size of the change.  Both
    paths still count for protection (``changed_files`` lists both)."""
    if not _has_base(repo, base_rev):
        return set()
    out = git(repo, "diff", "-M", "--name-status", "-z", base_rev, "--", ".", _exclude_arg(), check=False)
    parts = out.split("\0")
    renamed: set[str] = set()
    i = 0
    while i < len(parts):
        status = parts[i]
        if status.startswith("R"):
            if status == "R100" and i + 2 < len(parts):
                renamed.add(parts[i + 1].replace("\\", "/"))
                renamed.add(parts[i + 2].replace("\\", "/"))
            i += 3
        else:
            i += 2
    return renamed


def changed_files(repo: Path, base_rev: str) -> list[str]:
    """Paths changed since *base_rev* plus untracked files, run dir excluded.
    Without a base revision every file in the tree is the change."""
    if not _has_base(repo, base_rev):
        return walk_files(repo)
    files: set[str] = set()
    # ``--no-renames``: a rename is listed as its old path AND its new path.
    # With rename detection on (git's default) only the new path appeared, so
    # ``git mv daemon/core.py core.py`` moved protected code out of protection
    # and graded light.  Found by the Final Audit of the evidence-chain run.
    # The size discount for pure renames is computed separately (renamed_files).
    out = git(repo, "diff", "--no-renames", "--name-only", base_rev, "--", ".", _exclude_arg(), check=False)
    files.update(l.strip().replace("\\", "/") for l in out.splitlines() if l.strip())
    files.update(untracked_files(repo))
    return sorted(f for f in files if not _excluded(f))


def _count_lines(p: Path) -> int:
    try:
        with open(p, encoding="utf-8", errors="replace") as fh:
            return sum(1 for _ in fh)
    except OSError:
        return 0


def changed_lines(repo: Path, base_rev: str, *, skip: set[str] | None = None) -> int:
    """Added plus removed lines since *base_rev*, plus every line of each
    untracked file (a new file is all additions), *skip* paths not counted."""
    skip = skip or set()
    if not _has_base(repo, base_rev):
        return sum(_count_lines(repo / f) for f in walk_files(repo))
    total = 0
    out = git(repo, "diff", "--numstat", base_rev, "--", ".", _exclude_arg(), check=False)
    for line in out.splitlines():
        parts = line.split("\t")
        if len(parts) >= 3 and parts[2].replace("\\", "/") in skip:
            continue
        if len(parts) >= 2:
            for n in parts[:2]:
                if n.isdigit():
                    total += int(n)
    for u in untracked_files(repo):
        if u not in skip:
            total += _count_lines(repo / u)
    return total


def diff_lines(repo: Path, base_rev: str) -> tuple[list[tuple[str, str]], list[tuple[str, str]]]:
    """(added, removed) as (file, line) pairs since *base_rev*; untracked files
    count as added in full.  Prose files are skipped."""
    added: list[tuple[str, str]] = []
    removed: list[tuple[str, str]] = []
    if _has_base(repo, base_rev):
        out = git(repo, "diff", "-U0", "--no-color", base_rev, "--", ".", _exclude_arg(), check=False)
        current = minus = ""
        for line in out.splitlines():
            if line.startswith("--- "):
                minus = line[4:].replace("\\", "/")
                minus = minus[2:] if minus.startswith("a/") else minus
                continue
            if line.startswith("+++ "):
                current = line[4:].replace("\\", "/")
                current = current[2:] if current.startswith("b/") else current
                if current == "/dev/null":  # a deleted file: its removed lines belong to the old path
                    current = minus
                continue
            if line.startswith("@@") or line.startswith("diff ") or line.startswith("index "):
                continue
            if not current or _excluded(current):
                continue
            if current.endswith(_PROSE_SUFFIXES) and not current.endswith(".gitignore"):
                continue
            if line.startswith("+"):
                added.append((current, line[1:]))
            elif line.startswith("-"):
                removed.append((current, line[1:]))
        new_files = untracked_files(repo)
    else:
        new_files = walk_files(repo)
    for rel in new_files:
        if _excluded(rel) or rel.endswith(_PROSE_SUFFIXES):
            continue
        p = repo / rel
        try:
            if not p.is_file() or p.stat().st_size > _RISK_MAX_BYTES:
                continue
            for line in p.read_text(encoding="utf-8", errors="replace").splitlines():
                added.append((rel, line))
        except OSError:
            continue
    return added, removed


# A comment line describes code; it does not run.  Only the "suppressed check"
# signal lives in comments (``# noqa``, ``eslint-disable``), so it is the one
# label still read there.
_COMMENT_RE = re.compile(r"^\s*(?:#|//|/\*|\*|<!--)")
_COMMENT_LABELS = ("suppressed check",)
# Signals about what PRODUCTION code can do.  In a test file, a subprocess, a
# thread, an ``innerHTML`` fixture or a ``DROP TABLE`` string is how the test
# exercises the code, not a new capability of the product.  Everything that
# weakens the tests themselves (a skip, a suppressed check, a swallowed
# exception, a removed assertion or test) and every credential literal and
# destructive command still counts in a test file.
_TEST_EXEMPT_LABELS = ("shell execution", "process execution", "dynamic code execution", "concurrency",
                       "raw HTML injection", "TLS verification disabled", "schema change")
_TEST_DIRS = ("tests", "test", "__tests__", "spec", "specs")
_TEST_NAME_RE = re.compile(r"^(?:test_.*\.py|.*_test\.py|.*\.(?:test|spec)\.[cm]?[jt]sx?)$")


def is_test_file(rel: str) -> bool:
    """A test file by location or name.  ``conftest.py`` steers every test
    and is NOT one: its changes are scanned like production code."""
    parts = rel.replace("\\", "/").split("/")
    if parts[-1] == "conftest.py":
        return False
    return any(p in _TEST_DIRS for p in parts[:-1]) or _TEST_NAME_RE.match(parts[-1]) is not None


def risk_signals(added: list[tuple[str, str]], removed: list[tuple[str, str]]) -> list[str]:
    """One reason per (file, label): the first line that showed it.  Comment
    lines count only for suppressed checks, and test files do not count for
    signals about production capabilities (``_TEST_EXEMPT_LABELS``)."""
    seen: dict[tuple[str, str], str] = {}
    prev_file, prev_except = "", False
    for f, line in added:
        comment = _COMMENT_RE.match(line) is not None
        test = is_test_file(f)
        for rx, label in ADDED_RISK:
            if comment and label not in _COMMENT_LABELS:
                continue
            if test and label in _TEST_EXEMPT_LABELS:
                continue
            if rx.search(line):
                seen.setdefault((f, label), line.strip())
        # A two-line swallowed exception: ``except …:`` then ``pass``.
        if prev_except and f == prev_file and _PASS_RE.match(line):
            seen.setdefault((f, "swallowed exception"), line.strip())
        prev_file, prev_except = f, bool(_EXCEPT_RE.match(line))
    for f, line in removed:
        for rx, label in REMOVED_RISK:
            if rx.search(line):
                seen.setdefault((f, label), line.strip())
    # A new ignore rule hides files from the fingerprint and from this scan
    # (``.gitignore`` is tracked, so the rule itself is in the diff).
    for f, line in added:
        if (f == ".gitignore" or f.endswith("/.gitignore")) and line.strip() and not line.lstrip().startswith("#"):
            seen.setdefault((f, "new ignore rule"), line.strip())
    return [f"risk: {label} in {f} ({shown[:60]!r})" for (f, label), shown in sorted(seen.items())]


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


def required_depth(repo: Path, base_rev: str, *, pinned_patterns: list[str] | None = None,
                   escalations: list[str] | None = None) -> tuple[str, list[str], dict]:
    """('full' | 'light', reasons, facts).

    *pinned_patterns* are the protections ``pipeline.start`` copied into the
    run; they are enforced together with the repository's current file, so a
    run can add protection and never remove it.  *escalations* are the
    reasons checkers gave when they demanded full depth; any makes it full.
    """
    facts: dict = {"files": [], "lines": 0, "base_rev": base_rev, "renamed": [], "risks": []}
    if escalations:
        facts["escalated"] = list(escalations)
        return "full", [f"escalated by a checker: {r}" for r in escalations], facts
    if _is_git_repo(repo) and base_rev not in ("no-git", "no-commits", "") and not _rev_known(repo, base_rev):
        # ``git diff <unknown rev>`` fails and, with ``check=False``, reads as
        # "no changes": a rebased or garbage-collected base must fail closed,
        # never grade as a light change.
        return "full", [f"base revision {base_rev[:12]} is not in this repository, so the change cannot be bounded"], facts
    if not _has_base(repo, base_rev):
        files = walk_files(repo)
        facts.update({"files": files, "lines": changed_lines(repo, base_rev)})
        why = "no git repository" if base_rev == "no-git" or not _is_git_repo(repo) else "no commit yet"
        return "full", [f"{why}: there is no base revision to bound the change against "
                        f"({len(files)} files, {facts['lines']} lines, all of them new)"], facts
    files = changed_files(repo, base_rev)
    renamed = renamed_files(repo, base_rev) & set(files)
    sized = [f for f in files if f not in renamed]
    lines = changed_lines(repo, base_rev, skip=renamed)
    patterns = list(DEFAULT_PROTECTED) + list(pinned_patterns or []) + repo_protected_patterns(repo)
    reasons: list[str] = []
    for f in files:
        pat = _matches(f, patterns)
        if pat:
            reasons.append(f"{f} is protected ({pat})")
        elif _has_perf_marker(repo, f):
            reasons.append(f"{f} carries a PERF-CRITICAL marker")
    added, removed = diff_lines(repo, base_rev)
    risks = risk_signals(added, removed)
    reasons += risks
    if len(sized) > LIGHT_MAX_FILES:
        reasons.append(f"{len(sized)} files changed (light allows {LIGHT_MAX_FILES})")
    if lines > LIGHT_MAX_LINES:
        reasons.append(f"{lines} lines changed (light allows {LIGHT_MAX_LINES})")
    facts.update({"files": files, "lines": lines, "renamed": sorted(renamed), "risks": risks})
    if reasons:
        return "full", reasons, facts
    if not files:
        return "light", ["no changes yet against the base revision"], facts
    n = len(renamed) // 2  # each pure rename contributes its old and its new path
    note = f" ({n} pure rename{'s' if n != 1 else ''} not counted)" if n else ""
    return "light", [f"{len(sized)} files, {lines} lines, none protected, no risk signals{note}"], facts


def recorded_escalations(run_dir: Path) -> list[str]:
    """Reasons in ``evidence/escalation.json`` (for the CLI's report; the
    harness reads the checkers' transcripts, which cannot be deleted)."""
    p = run_dir / "evidence" / "escalation.json"
    if not p.is_file():
        return []
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except ValueError:
        return []
    return [str(r.get("reason") or "") for r in data if isinstance(r, dict)] if isinstance(data, list) else []


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m pipeline.depth")
    ap.add_argument("run_dir")
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args(argv)
    run_dir = Path(a.run_dir).resolve()
    run = load_run(run_dir)
    repo = run_repo(run_dir)
    depth, reasons, facts = required_depth(repo, run.get("base_rev", ""),
                                           pinned_patterns=pinned_protected_patterns(run_dir),
                                           escalations=recorded_escalations(run_dir))
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
