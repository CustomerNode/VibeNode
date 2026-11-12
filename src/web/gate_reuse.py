"""Reuse a passing test run on identical files, and skip the A/B tests when
nothing they depend on changed.

The full publish run is real work: 2,176 s of test time over 8 workers, about
5 minutes on a quiet machine.  Most of the time the files being
published are byte-for-byte the files a full run already passed on, so:

* **Whole-suite reuse.**  Every full run that passes records a fingerprint
  (``record_run``, called from ``tests/conftest.py`` on the xdist controller):
  the content hash of every tracked and untracked non-ignored file, plus the
  interpreter.  When the publish gate (``src/web/routes/test_api.py``) finds a
  record under ``MAX_AGE_S`` old whose fingerprint equals the working tree's,
  it does not run the tests again and the commit message says which run it
  reused.  ``src/web/test_prewarm.py`` starts a full run by itself once editing
  has been quiet for a while, so the fingerprint usually matches at publish.
* **A/B area skip.**  The Controlled A/B Test, AI Compare and A/B script tests
  are 411 of those 2,176 s and depend only on a small set of files
  (``area_files``: the test files, everything they import inside this
  repository, transitively, plus the suite's shared setup).  When the whole
  fingerprint differs but the area's does not, the gate runs everything
  except those three files and the commit says so.  ``tests/test_gate_reuse.py``
  pins the area: the closure must never reach ``src/web/``, ``src/engine/`` or
  ``src/web/static/``.

A record counts only when the run covered the whole suite (``scope_of``:
``tests/`` as the target, no ``-k`` / ``-m`` / ``--deselect`` / ``--lf``, no
ignores beyond Code Pipeline's and the A/B files), exited 0, and ran under
the same interpreter.  ``VIBENODE_TEST_REUSE=0`` turns both reuses off.
"""

from __future__ import annotations

import ast
import hashlib
import json
import os
import subprocess
import sys
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
RUNS_FILE = REPO_ROOT / "data" / "test_runs.json"   # gitignored
MAX_AGE_S = 24 * 3600
KEEP_RECORDS = 30

CODE_PIPELINE_GLOB = "tests/test_code_pipeline*.py"
AB_TESTS = (
    "tests/test_controlled_ab_test.py",
    "tests/test_ai_compare.py",
    "tests/test_ab_test_script.py",
)
# The suite's shared setup applies to the A/B tests too (sandboxing fixtures,
# the kill guards), so a change there re-runs them.  Its own imports (src/web/,
# src/engine/) are not followed: they are what it sandboxes, not what the A/B
# tests exercise.
AB_SHARED = ("tests/conftest.py", "pyproject.toml")
AREA_FORBIDDEN = ("src/web/", "src/engine/", "src/web/static/")

_NO_WINDOW = 0x08000000 if sys.platform == "win32" else 0


def enabled() -> bool:
    return os.environ.get("VIBENODE_TEST_REUSE", "1") != "0"


def interpreter() -> str:
    return "%s %s" % (sys.version.split()[0], sys.prefix)


# ── files and hashes ────────────────────────────────────────────────────────

def listed_files(repo: Path = REPO_ROOT) -> list[str]:
    """Every tracked file plus every untracked file git does not ignore,
    repo-relative with forward slashes, sorted; files that exist on disk."""
    out: set[str] = set()
    for extra in ((), ("--others", "--exclude-standard")):
        r = subprocess.run(["git", "-C", str(repo), "ls-files", "-z", *extra],
                           capture_output=True, timeout=60, creationflags=_NO_WINDOW)
        if r.returncode != 0:
            raise RuntimeError("git ls-files failed: " + r.stderr.decode("utf-8", "replace").strip())
        for raw in r.stdout.split(b"\0"):
            if raw:
                out.add(raw.decode("utf-8", "surrogateescape"))
    return sorted(p for p in out if (repo / p).is_file())


_hash_cache: dict[str, tuple[tuple[int, int], str]] = {}


def _file_hash(path: Path) -> str:
    st = path.stat()
    key = str(path)
    sig = (st.st_size, st.st_mtime_ns)
    hit = _hash_cache.get(key)
    if hit and hit[0] == sig:
        return hit[1]
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    digest = h.hexdigest()
    _hash_cache[key] = (sig, digest)
    return digest


def _digest(repo: Path, rel_paths) -> str:
    h = hashlib.sha256()
    for rel in rel_paths:
        p = repo / rel
        if not p.is_file():
            continue
        h.update(rel.encode("utf-8", "surrogateescape"))
        h.update(b"\0")
        h.update(_file_hash(p).encode())
        h.update(b"\n")
    return h.hexdigest()


def _internal_imports(path: Path, repo: Path) -> set[str]:
    """Repo-relative paths of the modules ``path`` imports from this repo."""
    try:
        tree = ast.parse(path.read_text(encoding="utf-8", errors="replace"))
    except (OSError, SyntaxError):
        return set()
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(a.name for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            names.add(node.module)
            names.update(node.module + "." + a.name for a in node.names)
    found: set[str] = set()
    for name in names:
        parts = name.split(".")
        for i in range(len(parts), 0, -1):
            # App code lives under src/ (on the import path); tests at the top.
            base = repo.joinpath("src", *parts[:i])
            if not (base.with_suffix(".py").is_file() or (base / "__init__.py").is_file()):
                base = repo.joinpath(*parts[:i])
            if base.with_suffix(".py").is_file():
                found.add(base.with_suffix(".py").relative_to(repo).as_posix())
                break
            if (base / "__init__.py").is_file():
                found.add((base / "__init__.py").relative_to(repo).as_posix())
                break
    return found


def area_files(repo: Path = REPO_ROOT) -> list[str]:
    """The A/B tests, everything they import inside the repo (transitively),
    the A/B scripts, and the suite's shared setup."""
    seen: set[str] = set()
    todo = list(AB_TESTS) + sorted(
        p.relative_to(repo).as_posix() for p in (repo / "scripts").glob("*.py")
        if "ab_test" in p.name or "ai_compare" in p.name)
    while todo:
        rel = todo.pop()
        if rel in seen or not (repo / rel).is_file():
            continue
        seen.add(rel)
        todo.extend(_internal_imports(repo / rel, repo) - seen)
    seen.update(s for s in AB_SHARED if (repo / s).is_file())
    return sorted(seen)


def fingerprint(repo: Path = REPO_ROOT) -> dict:
    files = listed_files(repo)
    area = set(area_files(repo))
    return {
        "all": _digest(repo, files),
        "ab": _digest(repo, [f for f in files if f in area]),
        "files": len(files),
        "interpreter": interpreter(),
    }


# ── what a pytest run covered ───────────────────────────────────────────────

def scope_of(config, repo: Path = REPO_ROOT) -> dict:
    """{"full": bool, "ab_ignored": bool} for a pytest ``config``: did the run
    target the whole suite with nothing deselected?"""
    def opt(name, default=None):
        try:
            return config.getoption(name)
        except (ValueError, AttributeError):
            return default

    tests_dir = (repo / "tests").resolve()
    inv = Path(str(getattr(getattr(config, "invocation_params", None), "dir", repo)))
    targets = list(getattr(config, "args", None) or [])
    full = bool(targets)
    for t in targets:
        try:
            if Path(t if os.path.isabs(t) else inv / t).resolve() != tests_dir:
                full = False
        except OSError:
            full = False
    if opt("keyword") or opt("markexpr") or opt("deselect") or opt("lf") or opt("failedfirst"):
        full = False
    if opt("collectonly") or opt("setuponly") or opt("setupplan"):
        full = False
    ab = {Path(a).as_posix() for a in AB_TESTS}
    ab_ignored = False
    for ig in opt("ignore") or []:
        try:
            rel = Path(ig if os.path.isabs(ig) else inv / ig).resolve().relative_to(repo.resolve()).as_posix()
        except ValueError:
            full = False
            continue
        if rel in ab:
            ab_ignored = True
        else:
            full = False
    for g in opt("ignore_glob") or []:
        if str(g).replace("\\", "/") != CODE_PIPELINE_GLOB:
            full = False
    return {"full": full, "ab_ignored": ab_ignored}


# ── records ─────────────────────────────────────────────────────────────────

def _load(path: Path = RUNS_FILE) -> list:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, list) else []
    except (OSError, ValueError):
        return []


def record_run(*, fp: dict, passed: int, skipped: int, failed: int, seconds: float,
               ab_covered: bool, source: str, command: str, ab_reused_from=None,
               path: Path = RUNS_FILE, now: float | None = None) -> dict:
    """Append one record of a passing, whole-suite run.  Returns it."""
    rec = {
        "finished": now if now is not None else time.time(),
        "all": fp["all"], "ab": fp["ab"], "interpreter": fp["interpreter"],
        "files": fp.get("files"),
        "passed": int(passed), "skipped": int(skipped), "failed": int(failed),
        "seconds": round(float(seconds), 1),
        "ab_covered": bool(ab_covered), "source": source, "command": command[:300],
    }
    if ab_reused_from is not None:
        rec["ab_reused_from"] = ab_reused_from
    records = _load(path)
    records.append(rec)
    records = records[-KEEP_RECORDS:]
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(records, indent=1), encoding="utf-8")
    os.replace(tmp, path)
    return rec


def plan(repo: Path = REPO_ROOT, path: Path = RUNS_FILE, now: float | None = None,
         fp: dict | None = None) -> dict:
    """What the gate may reuse for the working tree as it is now:
    {"whole": record or None, "ab": record or None, "fingerprint": fp}."""
    now = now if now is not None else time.time()
    fp = fp or fingerprint(repo)
    whole = ab = None
    for rec in reversed(_load(path)):
        if rec.get("interpreter") != fp["interpreter"]:
            continue
        if now - float(rec.get("finished") or 0) > MAX_AGE_S:
            continue
        if int(rec.get("failed") or 0):
            continue
        if whole is None and rec.get("all") == fp["all"]:
            whole = rec
        if ab is None and rec.get("ab_covered") and rec.get("ab") == fp["ab"]:
            ab = rec
        if whole and ab:
            break
    return {"whole": whole, "ab": ab, "fingerprint": fp}


def when_label(ts: float, now: float | None = None) -> str:
    """'20:41' today, else 'Nov 2 20:41'."""
    now = now if now is not None else time.time()
    t = time.localtime(ts)
    if time.localtime(now)[:3] == t[:3]:
        return time.strftime("%H:%M", t)
    return time.strftime("%b %d %H:%M", t).replace(" 0", " ")
