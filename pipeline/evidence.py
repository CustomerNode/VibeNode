"""Evidence: a verification criterion executed by a program, tied to a tree.

``run_criterion`` runs one criterion from the plan, in the repository, and
writes ``<run dir>/evidence/<id>.json`` holding the command, its exit status,
its output, whether ``expect`` matched, the SHA of the plan block it read, and
the fingerprint of the working tree it ran against.  It then prints one line::

    PIPELINE-EVIDENCE id=<id> sha=<sha256 of the file> passed=<true|false> fingerprint=<12 chars>

The Session Engine records that line as the command's stdout.  The harness
accepts an evidence file only when a transcript shows the line with the sha of
the bytes on disk, so a file an agent wrote by hand has no matching line and a
file edited after the fact no longer matches its line.

The tree fingerprint is HEAD plus the diff against it plus the content of
untracked files, with the run directory root excluded so writing stage files
does not move it.  HIGH applies to one fingerprint: the one every required
criterion ran against, which must be the tree at grading time.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

from pipeline.plan import Criterion, PlanError, load_plan

RUNS_SUBDIR = Path("docs") / "plans" / "runs"
EVIDENCE_DIR = "evidence"
EVIDENCE_PREFIX = "PIPELINE-EVIDENCE"
# Characters of stdout and of stderr kept in the evidence file.  Enough for a
# full pytest failure section; the transcript holds the rest.
OUTPUT_KEEP = 20_000


def git(repo: Path, *args: str, check: bool = True) -> str:
    """Run ``git <args>`` in *repo* and return stdout; raises RuntimeError on a
    non-zero exit unless ``check`` is false."""
    r = subprocess.run(["git", *args], cwd=str(repo), capture_output=True, text=True,
                       encoding="utf-8", errors="replace")
    if check and r.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)} failed: {r.stderr.strip()}")
    return r.stdout


def _is_git_repo(repo: Path) -> bool:
    try:
        return git(repo, "rev-parse", "--is-inside-work-tree").strip() == "true"
    except (RuntimeError, OSError):
        return False


def head_rev(repo: Path) -> str:
    """HEAD's commit id, ``no-git`` outside a repository, ``no-commits`` before
    the first commit."""
    if not _is_git_repo(repo):
        return "no-git"
    try:
        return git(repo, "rev-parse", "HEAD").strip()
    except RuntimeError:
        return "no-commits"


_NOISE_PARTS = ("__pycache__", ".pytest_cache", ".mypy_cache", ".ruff_cache", "node_modules")


def _excluded(rel: str) -> bool:
    """Paths that never count toward the fingerprint: the run directory itself
    and interpreter caches a test run creates in a repository that does not
    ignore them."""
    rel = rel.replace("\\", "/")
    if rel.startswith(RUNS_SUBDIR.as_posix() + "/") or rel == RUNS_SUBDIR.as_posix():
        return True
    parts = rel.split("/")
    return any(p in _NOISE_PARTS for p in parts) or rel.endswith((".pyc", ".pyo"))


def tree_fingerprint(repo: Path) -> str:
    """One hash for the exact state of the working tree."""
    h = hashlib.sha256()
    h.update(head_rev(repo).encode())
    if not _is_git_repo(repo):
        for root, dirs, files in os.walk(repo):
            dirs[:] = sorted(d for d in dirs if d not in (".git", "node_modules", "__pycache__"))
            for f in sorted(files):
                p = Path(root) / f
                rel = p.relative_to(repo).as_posix()
                if _excluded(rel):
                    continue
                h.update(rel.encode())
                h.update(_file_sha(p).encode())
        return h.hexdigest()
    exclude = f":(exclude){RUNS_SUBDIR.as_posix()}"
    if head_rev(repo) not in ("no-commits",):
        h.update(git(repo, "diff", "--no-color", "--binary", "HEAD", "--", ".", exclude).encode("utf-8", "replace"))
    else:
        h.update(git(repo, "diff", "--no-color", "--binary", "--cached", "--", ".", exclude).encode("utf-8", "replace"))
    untracked = git(repo, "ls-files", "--others", "--exclude-standard", "-z").split("\0")
    for rel in sorted(u for u in untracked if u and not _excluded(u)):
        h.update(rel.encode("utf-8", "replace"))
        h.update(_file_sha(repo / rel).encode())
    return h.hexdigest()


def _file_sha(p: Path) -> str:
    h = hashlib.sha256()
    try:
        with open(p, "rb") as fh:
            for chunk in iter(lambda: fh.read(1 << 16), b""):
                h.update(chunk)
    except OSError:
        return "unreadable"
    return h.hexdigest()


def evidence_sha(path: Path) -> str:
    """SHA-256 of the evidence file's bytes: the value the harness matches
    against the recorded ``PIPELINE-EVIDENCE`` line."""
    return _file_sha(path)


def evidence_line(cid: str, sha: str, passed: bool, fingerprint: str) -> str:
    """The one line ``run_check`` prints last; the harness searches transcripts
    for its ``id=`` and ``sha=`` part."""
    return f"{EVIDENCE_PREFIX} id={cid} sha={sha} passed={'true' if passed else 'false'} fingerprint={fingerprint[:12]}"


def load_run(run_dir: Path) -> dict:
    """The run's ``run.json``; raises PlanError when it is missing or invalid."""
    p = run_dir / "run.json"
    if not p.is_file():
        raise PlanError(f"{p} is missing; create the run with 'python -m pipeline.start'")
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except ValueError as e:
        raise PlanError(f"{p} is not valid JSON: {e}") from None


def _shell_command(cmd: str, cwd: Path) -> str:
    """The command as the platform shell must see it.

    ``shell=True`` is ``cmd.exe`` on Windows, and cmd splits a leading
    relative path on ``/`` (``.venv/Scripts/python.exe -m pytest`` fails with
    "'.venv' is not recognized").  Plans are written with forward slashes
    because every other tool accepts them, so when the first token names a
    file under *cwd* its slashes are turned into backslashes.  Nothing else in
    the command is touched; on POSIX the command is returned as is.
    """
    if os.name != "nt":
        return cmd
    m = re.match(r"\s*([^\s\"']+)", cmd)
    if not m or "/" not in m.group(1):
        return cmd
    head = m.group(1)
    if not (cwd / head).is_file():
        return cmd
    return cmd[:m.start(1)] + head.replace("/", "\\") + cmd[m.end(1):]


def run_criterion(run_dir: Path, cid: str, *, repo: Path | None = None) -> tuple[dict, Path]:
    """Execute one criterion and record the evidence.  Returns (record, path).

    The record's ``passed`` is true only when the command exited 0, ``expect``
    matched (if given), it did not time out, and the working tree's
    fingerprint is the same after the command as before it.
    """
    run_dir = run_dir.resolve()
    run = load_run(run_dir)
    repo = (repo or Path(run["repo"])).resolve()
    criteria, plan_sha, plan_src = load_plan(run_dir)
    by_id = {c.id: c for c in criteria}
    if cid not in by_id:
        raise PlanError(f"no criterion {cid!r} in {plan_src.name}; have: {', '.join(by_id)}")
    c: Criterion = by_id[cid]
    if not c.executable:
        raise PlanError(f"criterion {cid!r} is marked executable: false ({c.reason}); nothing to run")
    cwd = (repo / c.cwd).resolve() if c.cwd else repo
    fp_before = tree_fingerprint(repo)
    started = datetime.now(timezone.utc).isoformat(timespec="seconds")
    t0 = time.monotonic()
    timed_out = False
    try:
        r = subprocess.run(_shell_command(c.cmd, cwd), shell=True, cwd=str(cwd), capture_output=True,
                           text=True, encoding="utf-8", errors="replace", timeout=c.timeout)
        exit_code, stdout, stderr = r.returncode, r.stdout, r.stderr
    except subprocess.TimeoutExpired as e:
        timed_out = True
        exit_code = -1
        stdout = (e.stdout or b"").decode("utf-8", "replace") if isinstance(e.stdout, bytes) else (e.stdout or "")
        stderr = (e.stderr or b"").decode("utf-8", "replace") if isinstance(e.stderr, bytes) else (e.stderr or "")
    elapsed = round(time.monotonic() - t0, 3)
    fp_after = tree_fingerprint(repo)
    output = stdout + ("\n" + stderr if stderr else "")
    expect_matched = None
    if c.expect:
        expect_matched = re.search(c.expect, output, re.MULTILINE) is not None
    passed = (exit_code == 0) and (expect_matched is not False) and not timed_out and fp_before == fp_after
    record = {
        "id": c.id,
        "cmd": c.cmd,
        "cwd": str(cwd),
        "repo": str(repo),
        "run_id": run.get("run_id"),
        "started": started,
        "elapsed_s": elapsed,
        "exit_code": exit_code,
        "timed_out": timed_out,
        "expect": c.expect,
        "expect_matched": expect_matched,
        "required": c.required,
        "stdout_tail": stdout[-OUTPUT_KEEP:],
        "stderr_tail": stderr[-OUTPUT_KEEP:],
        "plan_sha": plan_sha,
        "plan_file": plan_src.name,
        "fingerprint": fp_after,
        "tree_stable": fp_before == fp_after,
        "passed": passed,
        "recorder": "pipeline.run_check",
        "python": sys.version.split()[0],
        "platform": sys.platform,
    }
    edir = run_dir / EVIDENCE_DIR
    edir.mkdir(parents=True, exist_ok=True)
    path = edir / f"{c.id}.json"
    data = json.dumps(record, indent=2, sort_keys=True).encode("utf-8")
    path.write_bytes(data)
    return record, path
