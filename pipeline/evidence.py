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

The tree fingerprint is the content of every file in the working tree (git's
own blob ids), with the run directory excluded so writing stage files does not
move it, and with HEAD left out so a commit of unchanged code does not move it
either.  HIGH applies to one fingerprint: the one every required criterion ran
against, which must be the tree at grading time.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
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


def walk_files(repo: Path) -> list[str]:
    """Every file under *repo* (relative posix paths), ``.git``, caches and the
    run directory excluded.  For repositories without git, or without a
    commit yet, this is the whole change."""
    out = []
    for root, dirs, files in os.walk(repo):
        dirs[:] = sorted(d for d in dirs if d not in (".git", "node_modules", "__pycache__"))
        for f in sorted(files):
            rel = (Path(root) / f).relative_to(repo).as_posix()
            if not _excluded(rel):
                out.append(rel)
    return out


def untracked_files(repo: Path) -> list[str]:
    """Untracked files, ignoring only what the repository's own tracked
    ``.gitignore`` files say.  ``--exclude-standard`` also honours
    ``.git/info/exclude`` and the user's global excludes, neither of which is
    in the tree, so a file listed there (a root ``conftest.py`` that controls
    every pytest outcome) escaped the fingerprint and the depth scan.  Found
    by the Test Team of the evidence-chain run."""
    out = git(repo, "ls-files", "--others", "--exclude-per-directory=.gitignore", "-z", check=False)
    listed = {p for p in out.split("\0") if p}
    # An untracked ``.gitignore`` is honoured like a tracked one and may list
    # itself, so ``sub/.gitignore`` holding ``.gitignore`` and ``conftest.py``
    # hid both from the listing above (found by the Review Team, loop 1).
    # Every untracked ``.gitignore`` the listing did not show counts unless
    # the DIRECTORY it sits in is ignored by a rule in a tracked file (a
    # virtualenv's own ``.gitignore`` says ``*`` and hides itself, but the
    # tracked root file already hides the whole directory).  An ignore file's
    # rules never apply to its own directory, so the attacker's ``sub/`` is
    # not ignored by anything tracked and ``sub/.gitignore`` surfaces; its
    # rules then reach the risk scan as a new ignore rule.
    hidden = [p for p in git(repo, "ls-files", "--others", "-z", "--", ":(glob)**/.gitignore", check=False).split("\0")
              if p and p not in listed and not _excluded(p)]
    if hidden:
        tracked = {p for p in git(repo, "ls-files", "-z", check=False).split("\0") if p}
        probes = {p: (p.rsplit("/", 1)[0] if "/" in p else p) for p in hidden}
        r = subprocess.run(["git", "check-ignore", "-v", "-z", "--no-index", "--stdin"], cwd=str(repo),
                           capture_output=True, text=True, encoding="utf-8", errors="replace",
                           input="\0".join(sorted(set(probes.values()))) + "\0")
        fields = r.stdout.split("\0")
        legit = set()
        for i in range(0, len(fields) - 3, 4):
            source, _line, _pattern, path = fields[i:i + 4]
            if source.replace("\\", "/") in tracked:
                legit.add(path)
        for p, probe in probes.items():
            if probe not in legit:
                listed.add(p)
    return sorted(p for p in listed if not _excluded(p))


def interpreter_site_dirs() -> list[Path]:
    """Where ``{python}`` loads startup code from: its site-packages and the
    user site.  Everything there is outside the repository's fingerprint."""
    import site
    dirs: list[Path] = []
    try:
        dirs += [Path(d) for d in site.getsitepackages()]
    except (AttributeError, OSError):
        pass
    try:
        dirs.append(Path(site.getusersitepackages()))
    except (AttributeError, OSError):
        pass
    return dirs


def site_fingerprint(dirs: list[Path] | None = None) -> str:
    """One hash for what steers the interpreter before the verified command
    runs: the interpreter path, every ``.pth`` file and ``sitecustomize`` /
    ``usercustomize`` module on its site paths, and ``PYTHONPATH`` /
    ``PYTHONHOME``.  A ``.pth`` written into the gitignored virtualenv steers
    every pytest run with nothing in the tree to show for it (found by the
    Review Team of the evidence-chain run); this fingerprint is pinned at the
    start and recorded with every piece of evidence, so such a write after
    the start is a finding."""
    h = hashlib.sha256()
    h.update(os.path.normcase(os.path.abspath(sys.executable)).encode("utf-8", "replace"))
    h.update(b"\0PYTHONPATH=" + os.environ.get("PYTHONPATH", "").encode("utf-8", "replace"))
    h.update(b"\0PYTHONHOME=" + os.environ.get("PYTHONHOME", "").encode("utf-8", "replace"))
    for d in (dirs if dirs is not None else interpreter_site_dirs()):
        if not d.is_dir():
            continue
        names = sorted(p.name for p in d.iterdir()
                       if p.is_file() and (p.suffix == ".pth" or p.name in ("sitecustomize.py", "usercustomize.py")))
        for name in names:
            h.update(f"\0{d.as_posix()}/{name}\0".encode("utf-8", "replace"))
            h.update(_file_sha(d / name).encode())
        # pytest (and other runners) auto-load plugins declared in installed
        # distributions' entry points (``pytest11``), so ``pip install`` of a
        # plugin steers the suite with no ``.pth`` at all.  The set of
        # installed distributions and their entry points are part of the
        # state (found by the Review Team, loop 2).
        for dist in sorted(p for p in d.iterdir() if p.is_dir() and p.suffix in (".dist-info", ".egg-info")):
            h.update(f"\0dist:{dist.name}\0".encode("utf-8", "replace"))
            ep = dist / "entry_points.txt"
            if ep.is_file():
                h.update(_file_sha(ep).encode())
    return h.hexdigest()


def local_excludes(repo: Path) -> list[str]:
    """Active (non-comment) lines of ``.git/info/exclude``, for the report."""
    try:
        top = git(repo, "rev-parse", "--git-path", "info/exclude").strip()
    except (RuntimeError, OSError):
        return []
    p = Path(top) if Path(top).is_absolute() else repo / top
    if not p.is_file():
        return []
    return [ln.strip() for ln in p.read_text(encoding="utf-8", errors="replace").splitlines()
            if ln.strip() and not ln.lstrip().startswith("#")]


# Files a test runner loads by name, wherever they sit.  An ignored copy of
# one steers the suite with nothing in the tracked tree to show for it
# (external review, finding 6).  Short on purpose: runner configuration and
# startup hooks, not every file.
STEERING_GLOBS = ("conftest.py", "pytest.ini", "tox.ini", "setup.cfg", "pyproject.toml", "sitecustomize.py",
                  "usercustomize.py", "jest.config.*", "vitest.config.*", "babel.config.*", ".babelrc",
                  ".mocharc*", "karma.conf.*")
# Directories whose contents are dependencies, not the project: ignored here.
_DEP_PARTS = ("node_modules", "site-packages", "dist-packages", ".git", ".tox", ".nox")


def ignored_steering_files(repo: Path) -> list[str]:
    """Ignored files that a test runner would load: ``conftest.py`` and runner
    configuration anywhere in the tree, outside dependency directories and
    virtualenvs."""
    specs = [f":(glob)**/{g}" for g in STEERING_GLOBS]
    out = git(repo, "ls-files", "--others", "--ignored", "--exclude-standard", "-z", "--", *specs, check=False)
    found = []
    for p in out.split("\0"):
        if not p or _excluded(p):
            continue
        parts = p.split("/")
        if any(x in _DEP_PARTS or x.startswith((".venv", "venv")) for x in parts[:-1]):
            continue
        found.append(p)
    return sorted(found)


def _worktree_mode(p: Path, index_mode: str | None) -> str:
    """The git mode of a file as it is in the working tree.  On Windows git
    does not track the executable bit, so the index's mode stands."""
    if os.name == "nt":
        return index_mode or "100644"
    try:
        return "100755" if os.stat(p).st_mode & 0o111 else "100644"
    except OSError:
        return index_mode or "100644"


def tree_fingerprint(repo: Path) -> str:
    """One hash for the CONTENT of the working tree, independent of git's
    commit identity.

    A normal commit of already-verified code changes HEAD but not one byte of
    source, and must not make the verification stale.  So the fingerprint is
    built from the blob id of every file present in the tree: the index's blob
    for a file whose worktree copy matches the index, ``git hash-object`` for
    a modified or untracked file.  Both are git's own content hash, so a file
    hashes the same before ``git add``, after it, and after the commit.
    Deleted files are absent; the run directory and caches never count.
    Outside git, the files' SHA-256s are used instead.
    """
    h = hashlib.sha256()
    if not _is_git_repo(repo):
        for rel in walk_files(repo):
            h.update(rel.encode())
            h.update(_file_sha(repo / rel).encode())
        return h.hexdigest()
    # Each entry is "<mode> <content id>": the file mode counts too, so making
    # a script non-executable (same bytes) is a change (external review,
    # finding 8).  A symlink's content id is that of its target path.
    entries: dict[str, str] = {}
    index_mode: dict[str, str] = {}
    # ONE git call (it was four: ``ls-files -s``, ``ls-files --deleted``,
    # ``diff --name-only`` and ``ls-files -v``).  Each git start costs about
    # 30 ms on Windows and a fingerprint is taken many times per run.  With
    # ``-c -d -m -s -v`` every line is "<tag> <mode> <id> <stage>\t<path>":
    #   - ``R``: deleted from the worktree (what ``--deleted`` listed);
    #   - ``C``: modified against the index, deleted ones included (what
    #     ``diff --name-only`` listed);
    #   - any other tag is the cached entry; ``-v`` lowercases it for an
    #     assume-unchanged file, and ``S`` is skip-worktree (what ``-v``
    #     listed).
    # ``-m`` honours assume-unchanged and skip-worktree exactly as ``diff``
    # does, which is why the flagged files are still hashed below.
    deleted: set[str] = set()
    modified: set[str] = set()
    # ``git update-index --assume-unchanged`` (lowercase tag) or
    # ``--skip-worktree`` (``S``) makes git stop looking at the file: ``diff``
    # and ``--deleted`` report nothing however much it changes, and the index
    # blob would stand in for content that is no longer there.  Hash those
    # files from the worktree like any modified file.
    flagged = set()
    for ent in git(repo, "ls-files", "-c", "-d", "-m", "-s", "-v", "-z").split("\0"):
        if not ent or "\t" not in ent or len(ent) < 2 or ent[1] != " ":
            continue
        meta, path = ent.split("\t", 1)
        tag, parts = meta[0], meta[2:].split()
        if tag in "Rr":
            deleted.add(path)
        elif tag in "Cc":
            modified.add(path)
        else:
            if tag.islower() or tag == "S":
                flagged.add(path)
            if len(parts) >= 3 and parts[2] == "0" and not _excluded(path):
                entries[path] = f"{parts[0]} {parts[1]}"
                index_mode[path] = parts[0]
    for p in deleted:
        entries.pop(p, None)
    untracked = set(untracked_files(repo))
    for p in flagged:
        if not (repo / p).is_file():
            entries.pop(p, None)
    steering = set(ignored_steering_files(repo))
    candidates = (modified | untracked | flagged | steering) - deleted
    links = sorted(p for p in candidates if not _excluded(p) and os.path.islink(repo / p))
    for p in links:
        entries[p] = "120000 link:" + hashlib.sha256(os.readlink(repo / p).encode("utf-8", "replace")).hexdigest()
    to_hash = sorted(p for p in candidates
                     if not _excluded(p) and p not in links and (repo / p).is_file())
    if to_hash:
        out = subprocess.run(["git", "hash-object", "--stdin-paths"], cwd=str(repo), capture_output=True,
                             text=True, encoding="utf-8", errors="replace", input="\n".join(to_hash) + "\n")
        shas = out.stdout.split()
        if not (out.returncode == 0 and len(shas) == len(to_hash)):
            shas = [_file_sha(repo / p) for p in to_hash]
        for p, sha in zip(to_hash, shas):
            entries[p] = f"{_worktree_mode(repo / p, index_mode.get(p))} {sha}"
    for p in steering:
        # An ignored file that steers the tests is marked as such, so adding,
        # changing or removing one is visible in the fingerprint.
        if p in entries:
            entries[p] = "ignored " + entries[p]
    for path in sorted(entries):
        h.update(path.encode("utf-8", "replace"))
        h.update(entries[path].encode())
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


def baseline_snapshot(repo: Path, run_id: str) -> str | None:
    """The commit the regression demonstration runs against: the code as it
    was when the run started.

    A clean tree is HEAD.  A dirty tree is captured with ``git stash create``
    (a commit object of the tracked changes; the working tree and the index
    are not touched) and kept alive under ``refs/pipeline/<run id>`` so
    garbage collection cannot remove it.  ``empty`` before the first commit;
    ``None`` outside a repository.  Untracked files present at the start are
    not part of the snapshot.
    """
    rev = head_rev(repo)
    if rev == "no-git":
        return None
    if rev == "no-commits":
        return "empty"
    stash = git(repo, "stash", "create", check=False).strip()
    if not stash:
        return rev
    git(repo, "update-ref", f"refs/pipeline/{run_id}", stash, check=False)
    return stash


def expand_cmd(cmd: str) -> str:
    """``{python}`` is the interpreter running the check, quoted if needed, so
    a plan or a ``.pipeline-verify`` never names one machine's virtualenv."""
    exe = sys.executable
    if " " in exe:
        exe = '"' + exe + '"'
    return cmd.replace("{python}", exe)


def evidence_sha(path: Path) -> str:
    """SHA-256 of the evidence file's bytes: the value the harness matches
    against the recorded ``PIPELINE-EVIDENCE`` line."""
    return _file_sha(path)


def baseline_label(cid: str) -> str:
    """The id a baseline execution prints in its evidence line."""
    return f"{cid}@baseline"


def impl_identity() -> tuple[str, str]:
    """(sha, directory) of the ``pipeline`` package actually running: a hash
    over every module's name and bytes.  A project-local package named
    ``pipeline`` that stands in for this one has a different identity (external
    review, finding 7).  This protects against running the wrong program by
    accident, not against a determined attacker on this machine."""
    here = Path(__file__).resolve().parent
    h = hashlib.sha256()
    for p in sorted(here.glob("*.py")):
        h.update(p.name.encode())
        h.update(_file_sha(p).encode())
    return h.hexdigest(), str(here)


def evidence_line(cid: str, sha: str, passed: bool, fingerprint: str, impl: str | None = None) -> str:
    """The line ``run_check`` prints first and last; the harness searches
    transcripts for its ``id=`` and ``sha=`` part and reads ``impl=``."""
    line = f"{EVIDENCE_PREFIX} id={cid} sha={sha} passed={'true' if passed else 'false'} fingerprint={fingerprint[:12]}"
    return line + (f" impl={impl[:12]}" if impl else "")


def load_run(run_dir: Path) -> dict:
    """The run's ``run.json``; raises PlanError when it is missing or invalid."""
    p = run_dir / "run.json"
    if not p.is_file():
        raise PlanError(f"{p} is missing; create the run with 'python -m pipeline.start'")
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except ValueError as e:
        raise PlanError(f"{p} is not valid JSON: {e}") from None


def run_repo(run_dir: Path) -> Path:
    """The repository a run belongs to, derived from where the run lives
    (``<repo>/docs/plans/runs/<run>``), never from ``run.json``.  The start
    line the engine recorded pins the run directory, so this pins the
    repository too: editing ``repo`` in ``run.json`` cannot point the checks
    at another clone (external review, finding 3)."""
    run_dir = run_dir.resolve()
    depth = len(RUNS_SUBDIR.parts)
    if len(run_dir.parents) <= depth:
        raise PlanError(f"{run_dir} is not a run directory under <repo>/{RUNS_SUBDIR.as_posix()}")
    repo = run_dir.parents[depth]
    if run_dir.parent != (repo / RUNS_SUBDIR).resolve():
        raise PlanError(f"{run_dir} is not a run directory under <repo>/{RUNS_SUBDIR.as_posix()}")
    return repo


def checked_repo(run_dir: Path, override: Path | None = None) -> Path:
    """``run_repo``, refusing an explicit override that names another place."""
    repo = run_repo(run_dir)
    if override is not None and os.path.normcase(str(override.resolve())) != os.path.normcase(str(repo)):
        raise PlanError(f"this run belongs to {repo}; it cannot be checked against {override}")
    return repo


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


# Environment variables that change what a verification command does without
# appearing in it.  ``PYTEST_ADDOPTS='-k one' python -m pipeline.run_check …``
# recorded a genuine passing run of one test as the whole suite (found by the
# Test Team of the evidence-chain run).  The child never sees these; the
# evidence records which ones were set.
ENV_STRIPPED = ("PYTEST_ADDOPTS", "PYTEST_PLUGINS", "PYTEST_DISABLE_PLUGIN_AUTOLOAD", "PYTHONSTARTUP",
                "NODE_OPTIONS", "JEST_OPTIONS")
# Secret-shaped variables never reach a verification command (Phase 1, F).
# A test the project needs a secret for is not a pipeline check yet; there is
# no exception key.  Stripping variables is not a sandbox: the command still
# has the filesystem, the network and every credential store on the machine.
ENV_SECRET_RE = re.compile(
    r"TOKEN|SECRET|PASSWORD|PASSWD|API_KEY|_KEY$|^ANTHROPIC_|^AWS_|^AZURE_|^GH_|^SUPABASE_|^CLAUDE_CODE_MESSAGING_",
    re.IGNORECASE)


def is_secret_name(name: str) -> bool:
    return ENV_SECRET_RE.search(name) is not None


def child_env() -> tuple[dict, list[str]]:
    """The environment a verification command runs in, and the names of the
    variables stripped from it (names only; values are never recorded)."""
    env = dict(os.environ)
    stripped = sorted(k for k in env if k in ENV_STRIPPED or is_secret_name(k))
    for k in stripped:
        env.pop(k, None)
    return env, stripped


def _execute(cmd: str, cwd: Path, timeout: int) -> tuple[int, str, str, bool, float, list[str]]:
    """Run *cmd* through the platform shell in *cwd*: (exit, stdout, stderr,
    timed_out, seconds, env variables stripped)."""
    env, stripped = child_env()
    t0 = time.monotonic()
    try:
        r = subprocess.run(_shell_command(expand_cmd(cmd), cwd), shell=True, cwd=str(cwd), capture_output=True,
                           timeout=timeout, env=env)
        return r.returncode, _decode(r.stdout), _decode(r.stderr), False, round(time.monotonic() - t0, 3), stripped
    except subprocess.TimeoutExpired as e:
        return -1, _decode(e.stdout), _decode(e.stderr), True, round(time.monotonic() - t0, 3), stripped


def _decode(data) -> str:
    """A child's output as text.  UTF-8 first; a Windows child writing to a
    pipe uses the ANSI code page instead (pytest's "…" arrives as 0x85), so
    fall back to the locale's encoding rather than recording U+FFFD."""
    if data is None:
        return ""
    if isinstance(data, str):
        return data
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        import locale
        return data.decode(locale.getpreferredencoding(False) or "utf-8", "replace")


def overlay_files(repo: Path, globs: list[str]) -> list[str]:
    """Files under *repo* matching *globs* (relative, ``**`` allowed), run
    directory, caches and ``.git`` excluded, sorted."""
    out: set[str] = set()
    for g in globs:
        for p in repo.glob(g):
            if p.is_file():
                rel = p.relative_to(repo).as_posix()
                if not _excluded(rel) and not rel.startswith(".git/"):
                    out.add(rel)
    return sorted(out)


def run_baseline(run_dir: Path, cid: str, *, repo: Path | None = None,
                 extra: dict | None = None) -> tuple[dict, Path]:
    """The regression demonstration for one ``detects_change`` criterion.

    Checks out the run's starting snapshot into a temporary git worktree (the
    working directory is never modified), copies the criterion's
    ``baseline_files`` in from the current tree, and runs the command there.
    The demonstration holds when the command FAILS and its output matches
    ``fails_with``: the new tests catch the absence of the change, for the
    intended reason.  Writes ``evidence/<id>.baseline.json``.
    """
    run_dir = run_dir.resolve()
    run = load_run(run_dir)
    repo = checked_repo(run_dir, repo)
    criteria, plan_sha, plan_src = load_plan(run_dir)
    by_id = {c.id: c for c in criteria}
    if cid not in by_id:
        raise PlanError(f"no criterion {cid!r} in {plan_src.name}; have: {', '.join(by_id)}")
    c: Criterion = by_id[cid]
    if not c.detects_change:
        raise PlanError(f"criterion {cid!r} is not marked detects_change; it has no baseline demonstration")
    snap = run.get("base_snapshot")
    if not snap:
        raise PlanError("this run has no baseline snapshot (not a git repository, or started by an older pipeline)")
    overlay = overlay_files(repo, c.baseline_globs())
    if not overlay:
        raise PlanError(f"criterion {cid!r}: baseline_files {c.baseline_files!r} match no file")
    tmp = Path(tempfile.mkdtemp(prefix="pipeline-baseline-"))
    tree = tmp / "tree"
    started = datetime.now(timezone.utc).isoformat(timespec="seconds")
    shas: dict[str, str] = {}
    try:
        if snap == "empty":
            tree.mkdir()
        else:
            git(repo, "worktree", "add", "--detach", "--quiet", str(tree), snap)
        for rel in overlay:
            dst = tree / rel
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(repo / rel, dst)
            shas[rel] = _file_sha(repo / rel)
        cwd = (tree / c.cwd) if c.cwd else tree
        exit_code, stdout, stderr, timed_out, elapsed, env_stripped = _execute(c.cmd, cwd, c.timeout)
    finally:
        if snap != "empty":
            git(repo, "worktree", "remove", "--force", str(tree), check=False)
            git(repo, "worktree", "prune", check=False)
        shutil.rmtree(tmp, ignore_errors=True)
    output = stdout + ("\n" + stderr if stderr else "")
    expect_matched = (re.search(c.expect, output, re.MULTILINE) is not None) if c.expect else None
    failed = exit_code != 0 or expect_matched is False
    fails_with_matched = re.search(c.fails_with, output, re.MULTILINE) is not None
    # Only a genuine non-zero exit demonstrates, the rule the harness grades by:
    # a missing success line alone is not a failure of the tests.
    demonstrated = exit_code != 0 and fails_with_matched and not timed_out
    record = {
        "id": c.id,
        "kind": "baseline",
        "cmd": c.cmd,
        "snapshot": snap,
        "overlay": shas,
        "run_id": run.get("run_id"),
        "started": started,
        "elapsed_s": elapsed,
        "exit_code": exit_code,
        "timed_out": timed_out,
        "expect_matched": expect_matched,
        "failed": failed,
        "fails_with": c.fails_with,
        "fails_with_matched": fails_with_matched,
        "demonstrated": demonstrated,
        "passed": demonstrated,
        "stdout_tail": stdout[-OUTPUT_KEEP:],
        "stderr_tail": stderr[-OUTPUT_KEEP:],
        "plan_sha": plan_sha,
        "plan_file": plan_src.name,
        "env_stripped": env_stripped,
        "site_sha": site_fingerprint(),
        "impl": impl_identity()[0],
        "impl_path": impl_identity()[1],
        "recorder": "pipeline.run_check --baseline",
    }
    record.update(extra or {})
    edir = run_dir / EVIDENCE_DIR
    edir.mkdir(parents=True, exist_ok=True)
    path = edir / f"{c.id}.baseline.json"
    path.write_bytes(json.dumps(record, indent=2, sort_keys=True).encode("utf-8"))
    return record, path


def run_criterion(run_dir: Path, cid: str, *, repo: Path | None = None,
                  extra: dict | None = None) -> tuple[dict, Path]:
    """Execute one criterion and record the evidence.  Returns (record, path).

    The record's ``passed`` is true only when the command exited 0, ``expect``
    matched (if given), it did not time out, and the working tree's
    fingerprint is the same after the command as before it.
    """
    run_dir = run_dir.resolve()
    run = load_run(run_dir)
    repo = checked_repo(run_dir, repo)
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
    exit_code, stdout, stderr, timed_out, elapsed, env_stripped = _execute(c.cmd, cwd, c.timeout)
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
        "env_stripped": env_stripped,
        "site_sha": site_fingerprint(),
        "impl": impl_identity()[0],
        "impl_path": impl_identity()[1],
        "recorder": "pipeline.run_check",
        "python": sys.version.split()[0],
        "platform": sys.platform,
    }
    record.update(extra or {})
    edir = run_dir / EVIDENCE_DIR
    edir.mkdir(parents=True, exist_ok=True)
    path = edir / f"{c.id}.json"
    data = json.dumps(record, indent=2, sort_keys=True).encode("utf-8")
    path.write_bytes(data)
    return record, path
