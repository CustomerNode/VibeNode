"""Controlled A/B Test: isolated, repeated, independently verified experiments.

AI Compare reports what the AI said. This establishes what was actually
accomplished: every trial runs in its own copy of the baseline, the result is
judged by checks fixed before any contestant starts and kept out of their
reach, and a trial counts as a verified success only when the controller's own
checks say so. A contestant's "done" is recorded, never trusted.

The experiment is a JSON spec (see ``SPEC_HELP``). The controller:

1. **validates** it: two or more variants that differ ONLY in the fields the
   spec says differ; a baseline; an assignment; an evaluation fixed in advance;
   hidden tests that exist, sit outside the baseline, and whose contents are
   not already in it.
2. **freezes** it into ``data/experiments/<experiment run id>/`` (created once,
   never reused): the spec and its hash, the hidden tests sealed with a
   manifest, a canary token, the shared instructions every contestant loads.
3. **prepares** each trial: a fresh git repository holding only the baseline
   commit's files (no history, no other branches, no remote), outside the
   VibeNode repository and outside every other trial's folder; verifies its tree
   equals the baseline commit's tree, that no project instructions or memory
   would be loaded from around it, and runs the optional baseline check.
4. **runs** trials round by round (round k = trial k of every variant, started
   together), each in its own VibeNode session whose working folder is that
   trial's copy. The server must confirm it applied the folder.
5. **verifies** each trial: implementation identity through the variant's
   provenance adapter, bound to this trial (a record from before the trial, or
   outside its folder, fails); contamination (the sealed path, the canary,
   another trial's folder or forbidden material seen by the contestant);
   then the hidden checks, run by the controller in a separate copy of the
   finished work, with the sealed tests re-hashed afterwards.
6. **reports**: per-trial evidence and per-variant aggregates, answering which
   approach was more correct, had fewer defects, cost less, was more
   consistent, needed more human help, and what the evidence establishes.
   Invalid or contaminated experiments are said to be so; one trial is a pilot.

Code Pipeline is not special here: a variant names a provenance adapter
command (``src/features/experiments/adapters/code_pipeline.py`` is one), configured in the spec.
"""

from __future__ import annotations

import hashlib
import io
import json
import os
import random
import re
import secrets
import shutil
import statistics
import subprocess
import sys
import tarfile
import time
from pathlib import Path

from . import hideaway
from . import sessions as S
from . import watch as W
from shared.paths import claude_home, claude_projects_dir, encode_cwd

RECORDS = S.REPO / "data" / "experiments"
WORK_ROOT = Path.home() / ".vibenode-experiments"
SPEC_FIELDS_COMPARED = ("prompt_template", "model", "provenance")

SPEC_HELP = """{
  "id": "pipeline-vs-normal",                       # kebab-case name of the experiment
  "question": "Does Code Pipeline deliver more correct code than normal coding?",
  "baseline": {"repo": "C:/path/to/repo", "commit": "<sha>"},   # or {"dir": "..."} or {"none": true}
  "assignment": "Add a --since flag to ...",       # or "assignment_file": "path"
  "variants": [
    {"name": "normal",   "prompt_template": "{assignment}",
     "provenance": {"command": ["{python}", "src/features/experiments/adapters/code_pipeline.py", "{workdir}", "{started_at}"],
                    "expect": "none"}},
    {"name": "pipeline", "prompt_template": "Use code pipeline: {assignment}",
     "provenance": {"command": ["{python}", "src/features/experiments/adapters/code_pipeline.py", "{workdir}", "{started_at}"],
                    "expect": "code-pipeline"}}
  ],                                                # adapters must live in ai_runs/adapters/
  "differs": ["prompt_template", "provenance"],    # every other variant field must be equal
  "model": "opus",                                  # shared settings (a variant may override only if listed in differs)
  "evaluation": {"kind": "command",
                 "tests_dir": "C:/hidden/acceptance",   # outside the baseline
                 "hidden_tests": "protected",           # or "not_secret" (readable by contestants, said in the report)
                 "place_at": "tests/acceptance",        # relative, inside the evaluation copy; never ".." or absolute
                 "command": ["{python}", "-m", "pytest", "-q", "{tests}"],
                 "reference_dir": "C:/hidden/reference",  # optional: files of a known-good solution
                 "regression_command": ["{python}", "-m", "pytest", "-q", "tests"],   # optional
                 "timeout": 900},
               # or {"kind": "rubric", "rubric": ["criterion", ...]}  (blinded, subjective)
  "baseline_check": {"command": [...], "expect": "pass"},  # optional extra check on each trial's copy
  "forbidden_material": ["path or text a contestant must never see"],   # optional
  "provenance_not_applicable": "reason",           # only when no variant runs a distinct implementation
  "trials": 3,
  "timeout": 3600
}

Before any contestant starts, the acceptance command must FAIL on the starting
code (a command that passes there proves nothing) and, when reference_dir is
given, PASS on the starting code plus that solution.

Your files are never changed or removed: the controller seals a COPY of the
hidden tests. Protection is: trial folders with unguessable names under a
parent the account cannot list, the sealing key kept only there, and the
break-out watch, which stops a contestant that reaches outside its folder."""

ADAPTER_DIR = S.REPO / "src" / "features" / "experiments" / "adapters"

VERIFIED, NOT_VERIFIED, SUBJECTIVE = "verified", "not_verified", "subjective"


class SpecError(ValueError):
    pass


# ── 1. validation ────────────────────────────────────────────────────────────

def _git(*args, cwd=None, check=True, text=True):
    return subprocess.run(["git", "-c", "core.autocrlf=false", *args], cwd=cwd, capture_output=True,
                          text=text, check=check)


def _within(child: Path, parent: Path) -> bool:
    try:
        Path(child).resolve().relative_to(Path(parent).resolve())
        return True
    except ValueError:
        return False


def _hash_file(p: Path) -> str:
    h = hashlib.sha256()
    with open(p, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 16), b""):
            h.update(chunk)
    return h.hexdigest()


def _hash_tree(root: Path) -> dict:
    root = Path(root)
    return {str(p.relative_to(root)).replace("\\", "/"): _hash_file(p)
            for p in sorted(root.rglob("*")) if p.is_file() and ".git" not in p.relative_to(root).parts}


def _content_key(data: bytes) -> str:
    """A file's content hash with line endings normalised, so a test file
    committed with LF and kept on disk with CRLF is still the same file."""
    return hashlib.sha256(data.replace(b"\r\n", b"\n")).hexdigest()


def _content_keys(root: Path) -> set:
    root = Path(root)
    return {_content_key(p.read_bytes()) for p in root.rglob("*")
            if p.is_file() and ".git" not in p.relative_to(root).parts}


def _baseline_blob_hashes(baseline: dict) -> set:
    """Content keys of every file in the baseline, to catch hidden tests that
    are already sitting in it."""
    out = set()
    if baseline.get("repo"):
        tar = _git("archive", "--format=tar", baseline["commit"], cwd=baseline["repo"], text=False).stdout
        with tarfile.open(fileobj=io.BytesIO(tar)) as t:
            for m in t.getmembers():
                if m.isfile():
                    out.add(_content_key(t.extractfile(m).read()))
    elif baseline.get("dir"):
        out = _content_keys(Path(baseline["dir"]))
    return out


def variant_settings(spec: dict, v: dict) -> dict:
    """A variant's effective settings: the shared ones, overridden by its own."""
    return {f: v.get(f, spec.get(f, "{assignment}" if f == "prompt_template" else None))
            for f in SPEC_FIELDS_COMPARED}


def assignment_text(spec: dict) -> str:
    if spec.get("assignment_file"):
        return Path(spec["assignment_file"]).read_text(encoding="utf-8")
    return spec.get("assignment") or ""


def safe_relative(rel) -> bool:
    """A path that stays inside whatever folder it is joined to."""
    rel = str(rel or "")
    if not rel.strip() or Path(rel).is_absolute() or re.match(r"^[A-Za-z]:", rel) or rel.startswith(("\\\\", "//")):
        return False
    return ".." not in Path(rel.replace("\\", "/")).parts


def _safe_rmtree(path: Path, within: Path) -> None:
    """Remove ``path`` only when it is strictly inside ``within``: cleanup can
    never reach a caller's folder, whatever a spec says."""
    path, within = Path(path), Path(within)
    if not path.exists():
        return
    if not _within(path, within) or path.resolve() == within.resolve():
        raise SpecError(f"refusing to remove {path}: it is not inside {within}")
    shutil.rmtree(path, ignore_errors=True)


def adapter_problems(prov: dict) -> list:
    """A provenance adapter counts as evidence only when it is one VibeNode
    ships (``src/features/experiments/adapters/``), run by this Python, with an expected
    identity pinned in the spec. An adapter written into a spec could print
    anything."""
    p = []
    cmd = [str(c) for c in (prov or {}).get("command") or []]
    if len(cmd) < 2 or cmd[0] != "{python}":
        return ['provenance: the command must be ["{python}", "src/features/experiments/adapters/<adapter>.py", ...]']
    script = Path(cmd[1]) if Path(cmd[1]).is_absolute() else S.REPO / cmd[1]
    if not _within(script, ADAPTER_DIR) or not script.is_file() or script.name == "__init__.py":
        p.append(f"provenance: {cmd[1]} is not an approved adapter (approved adapters live in src/features/experiments/adapters/)")
    if not str(prov.get("expect") or "").strip():
        p.append("provenance: `expect` must pin the implementation identity this variant must prove")
    return p


def validate(spec: dict) -> list:
    """Every reason the spec cannot give a valid controlled experiment. Empty
    means it can run."""
    p = []
    if not re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*", str(spec.get("id") or "")):
        p.append("id: a kebab-case name is required")
    if not str(spec.get("question") or "").strip():
        p.append("question: say what the experiment is testing")
    try:
        if not assignment_text(spec).strip():
            p.append("assignment: the task every contestant gets is required")
    except OSError as e:
        p.append(f"assignment_file: cannot read it ({e})")
    vs = spec.get("variants") or []
    names = [v.get("name") for v in vs]
    if len(vs) < 2:
        p.append("variants: at least two are required")
    if len(set(names)) != len(names) or not all(names):
        p.append("variants: every variant needs its own name")
    for v in vs:
        if "{assignment}" not in str(variant_settings(spec, v)["prompt_template"]):
            p.append(f"variant {v.get('name')}: prompt_template must contain {{assignment}}, "
                     "so every contestant gets the same task")
    differs = spec.get("differs") or []
    if not differs:
        p.append("differs: name the field(s) that intentionally differ between variants")
    for f in differs:
        if f not in SPEC_FIELDS_COMPARED:
            p.append(f"differs: {f!r} is not a variant field ({', '.join(SPEC_FIELDS_COMPARED)})")
    if len(vs) >= 2:
        eff = [variant_settings(spec, v) for v in vs]
        for f in SPEC_FIELDS_COMPARED:
            distinct = len({json.dumps(e[f], sort_keys=True) for e in eff})
            if f in differs and distinct == 1:
                p.append(f"differs: {f!r} is listed as the difference but is the same in every variant")
            if f not in differs and distinct > 1:
                p.append(f"uncontrolled difference: {f!r} differs between variants but is not in `differs`")
        for v, e in zip(vs, eff):
            if not e["provenance"] and not spec.get("provenance_not_applicable"):
                p.append(f"variant {v.get('name')}: no provenance adapter; give one, or say why none applies "
                         "in provenance_not_applicable")
            elif e["provenance"]:
                p += [f"variant {v.get('name')}: {x}" for x in adapter_problems(e["provenance"])]
    b = spec.get("baseline") or {}
    if b.get("repo"):
        if not b.get("commit"):
            p.append("baseline: a commit is required with a repo")
        else:
            r = _git("rev-parse", "--verify", f"{b['commit']}^{{commit}}", cwd=b["repo"], check=False)
            if r.returncode != 0:
                p.append(f"baseline: commit {b['commit']} not found in {b['repo']}")
    elif b.get("dir"):
        if not Path(b["dir"]).is_dir():
            p.append(f"baseline: folder {b['dir']} does not exist")
    elif not b.get("none"):
        p.append('baseline: give {"repo", "commit"}, {"dir"} or {"none": true}')
    ev = spec.get("evaluation") or {}
    if ev.get("kind") == "command":
        if not ev.get("command"):
            p.append("evaluation: a command is required")
        td = ev.get("tests_dir")
        if td:
            tdp = Path(td)
            if not tdp.is_dir() or not any(x.is_file() for x in tdp.rglob("*")):
                p.append(f"evaluation: tests_dir {td} is missing or empty")
            else:
                for root in (b.get("repo"), b.get("dir")):
                    if root and _within(tdp, Path(root)):
                        p.append("evaluation: tests_dir is inside the baseline, where contestants can read it")
                if not p and (b.get("repo") or b.get("dir")):
                    leaked = _content_keys(tdp) & _baseline_blob_hashes(b)
                    if leaked:
                        p.append(f"contamination: {len(leaked)} hidden test file(s) are already in the baseline")
        if td:
            mode = ev.get("hidden_tests", "protected")
            if mode not in ("protected", "not_secret"):
                p.append('evaluation: hidden_tests is "protected" or "not_secret"')
        pa = ev.get("place_at", "hidden_tests")
        if not safe_relative(pa):
            p.append(f"evaluation: place_at {pa!r} must be a relative path inside the evaluation copy "
                     "(no absolute path, no '..')")
        ref = ev.get("reference_dir")
        if ref and not Path(ref).is_dir():
            p.append(f"evaluation: reference_dir {ref} does not exist")
    elif ev.get("kind") == "rubric":
        if not ev.get("rubric"):
            p.append("evaluation: a rubric needs at least one criterion")
    else:
        p.append('evaluation: kind "command" (objective) or "rubric" (blinded, subjective) is required')
    t = spec.get("trials")
    if not isinstance(t, int) or t < 1:
        p.append("trials: a whole number of at least 1 is required")
    return p


# ── 2. freezing and sealing ──────────────────────────────────────────────────
#
# Directory separation is not an access boundary: every contestant session runs
# as the same user. Protection here is three things, named in the report:
#   1. the trial copies live under a parent the account cannot LIST
#      (``hideaway``), each with an unguessable name, so a contestant that does
#      not know a sibling's path cannot find it;
#   2. the hidden tests are sealed (encrypted) into the record, and the key is
#      kept only inside that unlistable parent (never only in memory, so a
#      crash loses nothing); plain copies exist only inside the controller's
#      own evaluation copy for the length of a check;
#   3. the break-out watch (``watch``) stops a contestant that reaches outside
#      its folder, into the parent, the record, the tests, the reference or a
#      sibling, and the experiment is then invalid.
# The caller's own files (tests_dir, reference_dir) are never changed or
# removed. Not OS isolation: the account owns these folders and could undo the
# permission; the watch flags the attempt.

_KEYS: dict = {}


def _fernet(key: bytes):
    from cryptography.fernet import Fernet
    return Fernet(key)


def freeze(spec: dict, records_root: Path = RECORDS, run_id: str | None = None,
           work_root: Path = WORK_ROOT) -> Path:
    """Create the experiment's record folder and its hidden work folder (never
    reusing either) and seal what must not change: the spec, the assignment and
    a copy of the hidden tests. The caller's folders are untouched."""
    run_id = run_id or S.new_run_id(spec["id"])
    exp = S.make_run_dir(records_root, run_id)
    work = Path(work_root) / run_id
    work.mkdir(parents=True, exist_ok=False)
    frozen = dict(spec)
    frozen["assignment"] = assignment_text(spec)
    frozen.pop("assignment_file", None)
    blob = json.dumps(frozen, indent=2, sort_keys=True)
    (exp / "spec.json").write_text(blob, encoding="utf-8")
    canary = secrets.token_hex(16)
    sealed = {"spec_sha256": hashlib.sha256(blob.encode()).hexdigest(), "canary": canary,
              "work": str(work), "isolation": None}
    (exp / "sealed").mkdir()
    ev = spec.get("evaluation") or {}
    if ev.get("tests_dir"):
        from cryptography.fernet import Fernet
        key = Fernet.generate_key()
        src = Path(ev["tests_dir"])
        files = {str(f.relative_to(src)).replace("\\", "/"): f.read_bytes()
                 for f in sorted(src.rglob("*")) if f.is_file()}
        f = _fernet(key)
        (exp / "sealed" / "tests.enc").write_text(
            json.dumps({k: f.encrypt(v).decode() for k, v in files.items()}), encoding="utf-8")
        sealed["tests"] = {k: hashlib.sha256(v).hexdigest() for k, v in files.items()}
        sealed["hidden_tests"] = ev.get("hidden_tests", "protected")
        # The key lives on disk from the start, inside the unlistable folder:
        # a crash never loses it, and nothing that can list the record can read it.
        key_path = work / f".key-{secrets.token_hex(8)}"
        key_path.write_bytes(key)
        sealed["key_path"] = str(key_path)
        _KEYS[str(exp)] = key
        if _decrypt_all(exp, key) != files:
            raise SpecError("sealing the hidden tests failed: the sealed copy does not match the source")
    (exp / "sealed" / "CANARY").write_text(canary, encoding="utf-8")
    (work / f".canary-{secrets.token_hex(6)}").write_text(canary, encoding="utf-8")
    if ev.get("reference_dir"):
        sealed["reference_sha256"] = _tree_digest(Path(ev["reference_dir"]))
    g = claude_home() / "CLAUDE.md"
    sealed["shared_user_instructions_sha256"] = _hash_file(g) if g.exists() else None
    # Hide the work folder LAST, once everything the controller needs to find
    # there is written. The controller keeps the names; nobody can list them.
    sealed["isolation"] = hideaway.hide(work)
    S.write_json(exp / "sealed.json", sealed)
    return exp


def _tree_digest(root: Path) -> str:
    return hashlib.sha256(json.dumps(_hash_tree(root), sort_keys=True).encode()).hexdigest()


def work_dir(exp: Path) -> Path:
    return Path(json.loads((exp / "sealed.json").read_text(encoding="utf-8"))["work"])


def _key(exp: Path):
    k = _KEYS.get(str(exp))
    if k is None:
        sealed = json.loads((exp / "sealed.json").read_text(encoding="utf-8"))
        for cand in (exp / "sealed" / "key.txt", Path(sealed.get("key_path") or "/nonexistent")):
            if cand.exists():
                k = cand.read_bytes().strip()
                break
    return k


def _decrypt_all(exp: Path, key) -> dict:
    enc = json.loads((exp / "sealed" / "tests.enc").read_text(encoding="utf-8"))
    f = _fernet(key)
    return {k: f.decrypt(v.encode()) for k, v in enc.items()}


def place_tests(exp: Path, dest: Path) -> None:
    """Write the hidden tests into the controller's own evaluation copy."""
    for rel, data in _decrypt_all(exp, _key(exp)).items():
        out = Path(dest) / rel
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_bytes(data)


def reveal(exp: Path) -> None:
    """Once every contestant is stopped: copy the key and the plain tests into
    the record and make the work folder listable again, so the experiment can
    be inspected. Never before the sessions are confirmed stopped."""
    sealed = json.loads((exp / "sealed.json").read_text(encoding="utf-8"))
    _KEYS.pop(str(exp), None)
    key = None
    if (exp / "sealed" / "tests.enc").exists():
        key = _key(exp)
        if key:
            (exp / "sealed" / "key.txt").write_bytes(key)
            try:
                for rel, data in _decrypt_all(exp, key).items():
                    out = exp / "sealed" / "tests_revealed" / rel
                    out.parent.mkdir(parents=True, exist_ok=True)
                    out.write_bytes(data)
            except Exception:
                pass
    work = Path(sealed.get("work") or "/nonexistent")
    if work.exists():
        hideaway.unhide(work)


def check_frozen(exp: Path) -> list:
    """Problems if the spec or the sealed tests changed after freezing."""
    sealed = json.loads((exp / "sealed.json").read_text(encoding="utf-8"))
    p = []
    blob = (exp / "spec.json").read_text(encoding="utf-8")
    if hashlib.sha256(blob.encode()).hexdigest() != sealed["spec_sha256"]:
        p.append("the experiment spec changed after it was frozen")
    if "tests" in sealed:
        try:
            files = _decrypt_all(exp, _key(exp))
            if {k: hashlib.sha256(v).hexdigest() for k, v in files.items()} != sealed["tests"]:
                p.append("the sealed hidden tests changed after they were frozen")
        except Exception:
            p.append("the sealed hidden tests changed after they were frozen (they no longer decrypt)")
    return p


def isolation_problems_now(exp: Path) -> list:
    """Problems if the protection around the trial folders is not in place."""
    sealed = json.loads((exp / "sealed.json").read_text(encoding="utf-8"))
    iso = sealed.get("isolation") or {}
    if not iso.get("ok"):
        return [f"isolation: the trial folders' parent could not be made unlistable ({iso.get('detail') or iso.get('method')})"]
    work = Path(sealed["work"])
    if hideaway.is_hidden(work) is not True or hideaway.listable(work):
        return ["isolation: the trial folders' parent is listable again (its permission was changed)"]
    return []


# ── 3. preparing a trial ─────────────────────────────────────────────────────

def make_workdir(baseline: dict, workdir: Path) -> dict:
    """A fresh copy of the baseline: a new git repository whose single commit
    holds exactly the baseline's files. Returns what was verified."""
    workdir.mkdir(parents=True, exist_ok=False)
    info = {"workdir": str(workdir)}
    if baseline.get("repo"):
        tar = _git("archive", "--format=tar", baseline["commit"], cwd=baseline["repo"], text=False).stdout
        with tarfile.open(fileobj=io.BytesIO(tar)) as t:
            t.extractall(workdir, filter="data") if hasattr(tarfile, "data_filter") else t.extractall(workdir)
        info["baseline_tree"] = _git("rev-parse", f"{baseline['commit']}^{{tree}}", cwd=baseline["repo"]).stdout.strip()
        info["baseline_commit"] = _git("rev-parse", baseline["commit"], cwd=baseline["repo"]).stdout.strip()
    elif baseline.get("dir"):
        shutil.copytree(baseline["dir"], workdir, dirs_exist_ok=True,
                        ignore=shutil.ignore_patterns(".git"))
    _git("init", "-q", cwd=workdir)
    _git("config", "core.autocrlf", "false", cwd=workdir)
    _git("add", "-A", cwd=workdir)
    _git("-c", "user.name=experiment", "-c", "user.email=experiment@localhost", "commit", "-q",
         "--allow-empty", "-m", "baseline", cwd=workdir)
    # One git call for both ids (each costs ~50 ms on Windows; the A/B tests
    # start hundreds of trials).
    commit, tree = _git("rev-parse", "HEAD", "HEAD^{tree}", cwd=workdir).stdout.split()
    info["start_commit"], info["start_tree"] = commit, tree
    return info


def verify_baseline(info: dict, workdir: Path) -> list:
    """The trial's copy must equal the baseline commit, untouched."""
    p = []
    tree = _git("rev-parse", "HEAD^{tree}", cwd=workdir).stdout.strip()
    if info.get("baseline_tree") and tree != info["baseline_tree"]:
        p.append(f"baseline mismatch: the trial's files (tree {tree[:12]}) are not the baseline commit's "
                 f"(tree {info['baseline_tree'][:12]})")
    if _git("status", "--porcelain", cwd=workdir).stdout.strip():
        p.append("baseline mismatch: the trial's copy has changes before the contestant started")
    if _git("remote", cwd=workdir).stdout.strip():
        p.append("isolation: the trial's copy has a git remote")
    if _git("rev-list", "--count", "HEAD", cwd=workdir).stdout.strip() != "1":
        p.append("isolation: the trial's copy carries history beyond the baseline")
    return p


def context_sources(workdir: Path) -> dict:
    """Instructions and memory Claude Code would load for a session in
    ``workdir``: CLAUDE.md files in its ancestors, and its project memory. The
    user's global instructions are shared by every contestant and recorded, not
    rejected."""
    found = []
    wd = Path(workdir).resolve()
    shared = {(claude_home() / "CLAUDE.md").resolve(), (Path.home() / ".claude" / "CLAUDE.md").resolve()}
    for anc in list(wd.parents):
        for name in ("CLAUDE.md", "CLAUDE.local.md", os.path.join(".claude", "CLAUDE.md")):
            # ``<home>/.claude/CLAUDE.md`` beside ``<home>/.claude/projects`` is a
            # user's global file (loaded for every contestant alike), not a project's.
            if name.startswith(".claude") and (anc / ".claude" / "projects").is_dir():
                continue
            if (anc / name).is_file() and (anc / name).resolve() not in shared:
                found.append(str(anc / name))
    mem = claude_projects_dir() / encode_cwd(str(wd)) / "memory"
    return {"ancestor_instructions": found, "project_memory": str(mem) if mem.exists() else None}


def isolation_problems(workdir: Path, exp: Path, other_workdirs: list) -> list:
    p = []
    if _within(workdir, S.REPO):
        p.append("isolation: the trial's copy is inside the VibeNode repository")
    if _within(exp, workdir) or _within(workdir, exp):
        p.append("isolation: the trial's copy and the sealed records overlap")
    for o in other_workdirs:
        if o != workdir and (_within(workdir, o) or _within(o, workdir)):
            p.append(f"isolation: the trial's copy overlaps another trial's ({o})")
    ctx = context_sources(workdir)
    for f in ctx["ancestor_instructions"]:
        p.append(f"contamination: project instructions would be loaded from {f}")
    if ctx["project_memory"]:
        p.append(f"contamination: project memory exists for this folder ({ctx['project_memory']})")
    return p


def _fill(cmd: list, **vals) -> list:
    out = []
    for c in cmd:
        c = str(c)
        for k, v in vals.items():
            c = c.replace("{" + k + "}", str(v))
        out.append(c)
    return out


def run_check(cmd: list, cwd: Path, timeout: float, log: Path, **vals) -> dict:
    full = _fill(cmd, python=sys.executable, **vals)
    t0 = time.time()
    try:
        r = subprocess.run(full, cwd=cwd, capture_output=True, text=True, timeout=timeout,
                           encoding="utf-8", errors="replace")
        rc, out = r.returncode, (r.stdout or "") + (r.stderr or "")
    except subprocess.TimeoutExpired as e:
        rc, out = None, f"TIMEOUT after {timeout}s\n{e.stdout or ''}"
    except OSError as e:
        rc, out = None, f"could not run: {e}"
    log.write_text(f"$ {' '.join(full)}\n(cwd {cwd})\n\n{out}", encoding="utf-8")
    m = re.search(r"(\d+) passed", out)
    f = re.search(r"(\d+) failed", out)
    er = re.search(r"(\d+) errors?", out)
    return {"command": full, "exit": rc, "seconds": round(time.time() - t0, 1), "log": str(log),
            "passed": int(m.group(1)) if m else None, "failed": int(f.group(1)) if f else None,
            "errors": int(er.group(1)) if er else None}


# ── 5. verifying a trial ─────────────────────────────────────────────────────

def check_provenance(prov: dict, workdir: Path, run_id: str, started_at: float,
                     pre_files: set, log: Path) -> dict:
    """Run the variant's approved adapter and bind its evidence to THIS trial.

    The adapter prints JSON: ``impl`` plus evidence, either ``record`` (a file
    inside the trial's copy, created after the trial started, that proves the
    implementation ran) or, only for ``impl: "none"``, ``absent`` (paths inside
    the copy that must not exist, re-checked here). A label without evidence,
    evidence from another run or from before the trial, an identity other than
    the pinned one, or an adapter that is not approved all fail."""
    if not prov:
        return {"status": "not_applicable"}
    bad = adapter_problems(prov)
    if bad:
        return {"status": "failed", "reason": "; ".join(bad)}
    res = run_check(prov["command"], S.REPO, prov.get("timeout", 120), log,
                    workdir=workdir, run_id=run_id, started_at=started_at)
    out = {"status": "failed", "exit": res["exit"], "log": res["log"], "expect": prov.get("expect")}
    try:
        text = log.read_text(encoding="utf-8")
        data = json.loads([ln for ln in text.splitlines() if ln.strip().startswith("{")][-1])
    except (IndexError, ValueError):
        out["reason"] = "the adapter printed no JSON"
        return out
    impl = str(data.get("impl") or "")
    out["impl"] = impl
    if res["exit"] != 0:
        out["reason"] = data.get("reason") or f"the adapter exited {res['exit']}"
        return out
    want = str(prov.get("expect"))
    if not (impl == want or impl.startswith(want + ":")):
        out["reason"] = f"wrong implementation: expected {want!r}, the trial ran {impl!r}"
        return out
    if prov.get("expect_identity") and impl != prov["expect_identity"]:
        out["reason"] = f"wrong implementation version: expected {prov['expect_identity']!r}, got {impl!r}"
        return out
    rec, absent = data.get("record"), data.get("absent")
    if rec:
        rp = Path(rec)
        if not _within(rp, workdir):
            out["reason"] = f"evidence from another run: {rec} is outside this trial's copy"
            return out
        rel = str(rp.resolve().relative_to(workdir.resolve())).replace("\\", "/")
        if not rp.exists():
            out["reason"] = f"missing evidence: {rec} does not exist"
            return out
        if rel in pre_files or rp.stat().st_mtime < started_at:
            out["reason"] = f"stale record: {rec} existed before this trial started"
            return out
        out["record"] = str(rp)
        out["record_sha256"] = _hash_file(rp)
    elif absent and impl == "none":
        for a in absent:
            ap = Path(a)
            if not _within(ap, workdir):
                out["reason"] = f"evidence from another run: {a} is outside this trial's copy"
                return out
            if ap.exists() and (ap.is_file() or any(ap.iterdir())):
                out["reason"] = f"the adapter said nothing ran, but {a} exists"
                return out
        out["absent"] = list(absent)
    else:
        out["reason"] = (f"missing evidence: the adapter only said {impl!r}; a label is not proof "
                         "(it must name a record from this trial)")
        return out
    out["status"] = "verified"
    return out


def contamination_evidence(project_dir, sid, needles: dict) -> list:
    """Where a contestant's transcripts mention something it must not have seen."""
    hits = []
    if not project_dir or not sid:
        return hits
    for path in S._transcripts(project_dir, sid):
        try:
            text = path.read_text(encoding="utf-8")
        except OSError:
            continue
        low = text.lower().replace("\\\\", "/").replace("\\", "/")
        for label, needle in needles.items():
            n = str(needle).lower().replace("\\", "/")
            if n and n in low:
                hits.append(f"{label} appears in {path.name}")
    return sorted(set(hits))


def run_acceptance(spec: dict, exp: Path, code_dir: Path, log_dir: Path, tag: str = "") -> dict:
    """Run the acceptance (and regression) checks on a copy of ``code_dir``
    made for this run only. The hidden tests are written into that copy for the
    length of the check and removed straight after; the copy is kept, without
    them, as evidence."""
    ev = spec["evaluation"]
    edir = Path(log_dir) / f"eval{tag}"
    shutil.copytree(code_dir, edir, ignore=shutil.ignore_patterns(".git"))
    tests = None
    if ev.get("tests_dir"):
        pa = ev.get("place_at", "hidden_tests")
        if not safe_relative(pa):
            raise SpecError(f"place_at {pa!r} would leave the evaluation copy")
        tests = edir / pa
        if not _within(tests, edir) or tests.resolve() == edir.resolve():
            raise SpecError(f"place_at {pa!r} would leave the evaluation copy")
        place_tests(exp, tests)
    timeout = ev.get("timeout", 900)
    try:
        out = {"kind": "command",
               "acceptance": run_check(ev["command"], edir, timeout, Path(log_dir) / f"acceptance{tag}.log",
                                       tests=tests or "", workdir=edir)}
        if ev.get("regression_command"):
            out["regression"] = run_check(ev["regression_command"], edir, timeout,
                                          Path(log_dir) / f"regression{tag}.log", tests=tests or "", workdir=edir)
    finally:
        if tests is not None:
            _safe_rmtree(tests, edir)
    return out


def evaluate(spec: dict, exp: Path, workdir: Path, trial_dir: Path) -> dict:
    """The hidden checks, run by the controller on the finished work."""
    if spec["evaluation"]["kind"] == "rubric":
        return {"kind": "rubric", "status": "awaiting_blinded_scores"}
    return run_acceptance(spec, exp, workdir, trial_dir)


def qualify(spec: dict, exp: Path, work_root: Path) -> dict:
    """Prove the acceptance checks mean something before any contestant runs:
    they must FAIL on the starting code (a placeholder that passes everything
    is caught here), and PASS on the starting code plus ``reference_dir`` when
    one is given. A check that cannot run at all is an infrastructure failure."""
    ev = spec["evaluation"]
    out = {"problems": [], "level": None}
    if ev["kind"] != "command":
        return out
    qdir = exp / "qualification"
    qdir.mkdir()
    base = work_dir(exp) / f"_qualify-baseline-{secrets.token_hex(6)}"
    make_workdir(spec["baseline"], base)
    r = run_acceptance(spec, exp, base, qdir, "-baseline")["acceptance"]
    out["baseline"] = r
    if r["exit"] is None:
        out["problems"].append("acceptance checks could not run on the starting code: " + r["log"])
        out["infrastructure"] = True
    elif r["exit"] == 0:
        out["problems"].append("acceptance checks PASS on the starting code, so passing them proves nothing "
                               "about the assignment (a placeholder command is caught here)")
    out["level"] = "fails on the starting code"
    ref = ev.get("reference_dir")
    if ref and not out["problems"]:
        rdir = work_dir(exp) / f"_qualify-reference-{secrets.token_hex(6)}"
        make_workdir(spec["baseline"], rdir)
        shutil.copytree(ref, rdir, dirs_exist_ok=True)
        r2 = run_acceptance(spec, exp, rdir, qdir, "-reference")["acceptance"]
        out["reference"] = r2
        if r2["exit"] != 0:
            out["problems"].append("acceptance checks do not pass on the reference solution, so no contestant "
                                   "could be verified")
            if r2["exit"] is None:
                out["infrastructure"] = True
        else:
            out["level"] = "fails on the starting code and passes on the reference solution"
    return out


def trial_verdict(t: dict) -> tuple:
    """``(verdict, reasons)``. Three things are kept apart: the session
    finished (``execution_completed``), the checks ran (``checks_executed``)
    and the outcome was verified. Verified only when the session completed,
    its copy was isolated and on the baseline, it changed something to
    evaluate (for code), its implementation was proven, nothing contaminated
    it, and the controller's own qualified checks passed. Anything missing is
    NOT verified, whatever the contestant said."""
    reasons = []
    if t.get("status") != S.COMPLETED:
        reasons.append(f"session {t.get('status')}")
    term = t.get("termination") or ""
    if term.startswith("unconfirmed"):
        reasons.append(f"termination {term}")
    reasons += t.get("isolation_problems") or []
    reasons += t.get("contamination") or []
    pv = t.get("provenance") or {}
    if pv.get("status") not in ("verified", "not_applicable"):
        reasons.append(f"implementation not proven: {pv.get('reason', pv.get('status'))}")
    if t.get("model_mismatch"):
        reasons.append("ran on a different model than assigned")
    if t.get("requires_changes") and not t.get("changed_files"):
        reasons.append("no changes to evaluate: the contestant produced nothing")
    ev = t.get("evaluation") or {}
    if ev.get("kind") == "rubric":
        return (SUBJECTIVE if not reasons else NOT_VERIFIED), reasons
    acc = ev.get("acceptance") or {}
    if not acc or acc.get("exit") is None:
        reasons.append("acceptance checks did not run")
    elif acc.get("exit") != 0:
        reasons.append("acceptance checks did not pass")
    reg = ev.get("regression")
    if reg and reg.get("exit") != 0:
        reasons.append("regression checks did not pass")
    return (VERIFIED if not reasons else NOT_VERIFIED), reasons


# ── 4. running ───────────────────────────────────────────────────────────────

WATCHED = ["paths outside the trial folder", "VibeNode's own routes", "the transcript folder",
           "the experiment record", "the hidden tests' and reference solution's folders",
           "other trials' folders and their parent", "permission and ownership commands",
           "the canary and sealing-key tokens"]


def run_experiment(spec: dict, client=None, records_root: Path = RECORDS, work_root: Path = WORK_ROOT,
                   out=print, poll: float = 2.0, settle: float = 15.0, run_id: str | None = None,
                   catalog: list | None = None, stop_quiet: float = 3.0, watch_interval: float = 3.0) -> dict:
    problems = validate(spec)
    if problems:
        raise SpecError("; ".join(problems))
    client = client or S.VibeNodeClient()
    exp = freeze(spec, records_root, run_id, work_root)
    out(f"experiment record: {exp}")
    record = {"experiment": exp.name, "started_at": time.time(), "trials": [], "invalid": [],
              "infrastructure": [], "incomplete": [],
              "containment": {"watched": WATCHED, "interval_s": watch_interval, "trips": [],
                              "integrity": [], "shutdown": []}}
    ctx = {"live": [], "watch": None}
    try:
        _run(spec, exp, record, client, out, poll, settle, catalog, stop_quiet, watch_interval, ctx)
    except Exception as e:  # an infrastructure failure, never a contestant result
        record["infrastructure"].append(f"the controller failed: {type(e).__name__}: {e}")
        raise
    finally:
        # Order matters: nothing protected is revealed, and nothing is
        # finalised, until every contestant is confirmed stopped.
        if ctx["watch"] is not None:
            try:
                ctx["watch"].stop()
            except Exception:
                pass
        record["containment"]["shutdown"] = _stop_all(client, ctx["live"], stop_quiet, record)
        try:
            record["containment"]["integrity"] = _integrity(exp, record)
        except Exception as e:
            record["infrastructure"].append(f"the integrity check failed: {type(e).__name__}: {e}")
        reveal(exp)
        frozen = json.loads((exp / "spec.json").read_text(encoding="utf-8"))
        record.setdefault("question", frozen["question"])
        record.setdefault("spec_sha256", "")
        record["ended_at"] = time.time()
        record["summary"] = summarize(record, frozen)
        S.write_json(exp / "experiment.json", record)
        (exp / "report.md").write_text(render_report(record, frozen), encoding="utf-8")
        record["report"] = str(exp / "report.md")
    return record


def _stop_all(client, runs: list, stop_quiet: float, record: dict) -> list:
    """Stop and confirm every session that may still be running. A stop that
    cannot be confirmed is an infrastructure failure."""
    out = []
    for r in runs:
        if r.status == "running":
            r.status, r.ended_at, r.error = S.CANCELLED, time.time(), "stopped: the controller ended before it finished"
            r.termination = S.terminate(client, r, quiet=stop_quiet)
        elif r.status in (S.TIMEOUT, S.CANCELLED) and not r.termination:
            r.termination = S.terminate(client, r, quiet=stop_quiet)
        elif (r.termination or "").startswith("unconfirmed"):
            r.termination = S.terminate(client, r, quiet=stop_quiet)
        if r.termination:
            out.append({"run": r.key, "session_id": r.session_id, "termination": r.termination})
        if (r.termination or "").startswith("unconfirmed"):
            record["infrastructure"].append(f"{r.key}: {r.termination}")
    return out


def _integrity(exp: Path, record: dict) -> list:
    """After the run: protected material unchanged, finished trials unchanged,
    the parent still unlistable. Anything else is a write-side break-out."""
    sealed = json.loads((exp / "sealed.json").read_text(encoding="utf-8"))
    frozen = json.loads((exp / "spec.json").read_text(encoding="utf-8"))
    out = []
    ref = (frozen.get("evaluation") or {}).get("reference_dir")
    if ref and sealed.get("reference_sha256"):
        ok = Path(ref).is_dir() and _tree_digest(Path(ref)) == sealed["reference_sha256"]
        out.append({"what": "reference solution", "ok": ok})
        if not ok:
            record["invalid"].append("break-out: the reference solution was changed during the experiment")
    frozen_probs = check_frozen(exp)
    out.append({"what": "sealed hidden tests and spec", "ok": not frozen_probs})
    for pr in frozen_probs:
        record["invalid"].append(f"break-out: {pr}")
    for t in record["trials"]:
        if t.get("tree_sha256") and Path(t["workdir"]).is_dir():
            ok = _tree_digest(Path(t["workdir"])) == t["tree_sha256"]
            out.append({"what": f"finished trial {t['run_id'].split('/')[-1]}", "ok": ok})
            if not ok:
                record["invalid"].append(f"break-out: trial {t['run_id'].split('/')[-1]}'s folder was changed after it finished")
    iso = isolation_problems_now(exp)
    out.append({"what": "trial folders' parent unlistable", "ok": not iso})
    for pr in iso:
        record["invalid"].append(f"break-out: {pr}")
    return out


def _rules_for(exp: Path, work: Path, wd: Path, frozen: dict, sealed: dict, others: dict) -> W.Rules:
    ev = frozen.get("evaluation") or {}
    protected = {"the experiment record": str(exp)}
    if ev.get("tests_dir"):
        protected["the hidden tests' folder"] = ev["tests_dir"]
    if ev.get("reference_dir"):
        protected["the reference solution's folder"] = ev["reference_dir"]
    protected["the transcript folder"] = str(claude_projects_dir())
    protected["the VibeNode repository"] = str(S.REPO)
    for i, m in enumerate(frozen.get("forbidden_material") or []):
        protected[f"forbidden material #{i + 1}"] = m
    protected.update(others)
    secrets_ = {"canary": sealed["canary"]}
    key = _key(exp)
    if key:
        secrets_["sealing key"] = key.decode() if isinstance(key, bytes) else str(key)
    return W.Rules(trial_dir=str(wd), protected=protected, secrets=secrets_, parent_dir=str(work))


def _run(spec, exp, record, client, out, poll, settle, catalog, stop_quiet, watch_interval, ctx):
    frozen = json.loads((exp / "spec.json").read_text(encoding="utf-8"))
    sealed = json.loads((exp / "sealed.json").read_text(encoding="utf-8"))
    assignment = frozen["assignment"]
    work = Path(sealed["work"])
    record.update({"question": frozen["question"], "spec_sha256": sealed["spec_sha256"],
                   "hidden_tests": sealed.get("hidden_tests"), "isolation": sealed.get("isolation")})
    iso = isolation_problems_now(exp)
    if iso:
        record["invalid"] += iso
        out("NOT STARTED: " + "; ".join(iso))
        return
    q = qualify(frozen, exp, work)
    record["qualification"] = q
    if q["problems"]:
        (record["infrastructure"] if q.get("infrastructure") else record["invalid"]).extend(
            f"acceptance checks not qualified: {x}" for x in q["problems"])
        out("NOT STARTED: " + "; ".join(q["problems"]))
        return
    catalog = catalog if catalog is not None else (client.models() if any(
        variant_settings(frozen, v)["model"] for v in frozen["variants"]) else [])
    all_workdirs = []
    needles_base = {"the sealed records folder": str(exp), "the canary": sealed["canary"]}
    for i, m in enumerate(frozen.get("forbidden_material") or []):
        needles_base[f"forbidden material #{i + 1}"] = m
    if frozen["evaluation"].get("tests_dir"):
        needles_base["the hidden tests' source folder"] = frozen["evaluation"]["tests_dir"]
    if frozen["evaluation"].get("reference_dir"):
        needles_base["the reference solution's folder"] = frozen["evaluation"]["reference_dir"]
    requires_changes = bool(frozen["baseline"].get("repo") or frozen["baseline"].get("dir"))

    def tripped(run, trip):
        record["containment"]["trips"].append({"run": run.key, "rule": trip.rule, "tool": trip.tool,
                                               "excerpt": trip.excerpt, "at": trip.at,
                                               "termination": run.termination})
        out(f"CONTAINED {run.name}: {trip.rule}")
        notify = getattr(client, "notify", None)
        if notify:
            try:
                notify("Controlled A/B Test: break-out contained",
                       f"{run.name}: {trip.rule}. The session was stopped ({run.termination}); the experiment is invalid.")
            except Exception:
                pass

    for k in range(1, frozen["trials"] + 1):
        rnd = []
        for vi, v in enumerate(frozen["variants"], 1):
            eff = variant_settings(frozen, v)
            # The record folder is readable ("v01-plain-t01"); the working copy
            # gets an unguessable name under the unlistable parent.
            rid = f"v{vi:02d}-{slug_name(v['name'])}-t{k:02d}"
            token = secrets.token_hex(16)
            tdir = exp / "trials" / rid
            tdir.mkdir(parents=True, exist_ok=False)
            wd = work / token
            t = {"run_id": f"{exp.name}/{rid}", "variant": v["name"], "variant_id": f"v{vi:02d}",
                 "trial": k, "dir": str(tdir), "workdir": str(wd), "token": token, "settings": eff,
                 "requires_changes": requires_changes}
            try:
                info = make_workdir(frozen["baseline"], wd)
                t["baseline"] = info
                t["isolation_problems"] = verify_baseline(info, wd) + isolation_problems(wd, exp, all_workdirs)
            except (subprocess.CalledProcessError, OSError) as e:
                t["isolation_problems"] = [f"could not prepare the trial's copy: {e}"]
            all_workdirs.append(wd)
            t["context_sources"] = context_sources(wd) if wd.exists() else {}
            bc = frozen.get("baseline_check")
            if bc and wd.exists() and not t["isolation_problems"]:
                tmp = tdir / "baseline_check"
                shutil.copytree(wd, tmp, ignore=shutil.ignore_patterns(".git"))
                tests = None
                try:
                    if frozen["evaluation"].get("tests_dir"):
                        pa = frozen["evaluation"].get("place_at", "hidden_tests")
                        tests = tmp / pa
                        if not safe_relative(pa) or not _within(tests, tmp):
                            raise SpecError(f"place_at {pa!r} would leave the evaluation copy")
                        place_tests(exp, tests)
                    res = run_check(bc["command"], tmp, bc.get("timeout", 900), tdir / "baseline_check.log",
                                    tests=tests or "", workdir=tmp)
                finally:
                    _safe_rmtree(tmp, tdir)
                t["baseline_check"] = res
                want_pass = bc.get("expect", "pass") == "pass"
                if (res["exit"] == 0) != want_pass:
                    t["isolation_problems"].append(
                        f"baseline check: expected the baseline to {bc.get('expect', 'pass')} it, exit {res['exit']}")
            prompt = str(eff["prompt_template"]).replace("{assignment}", assignment)
            model = S.resolve_model(eff["model"], catalog)["id"] if eff["model"] else ""
            (tdir / "prompt.txt").write_bytes(prompt.encode("utf-8"))
            t["executed_prompt_sha256"] = hashlib.sha256(prompt.encode()).hexdigest()
            t["pre_files"] = sorted(_hash_tree(wd)) if wd.exists() else []
            r = S.Run(key=rid, name=f"{frozen['id']} · {v['name']} · trial {k}", prompt=prompt,
                      model=model, cwd=str(wd.resolve()) if wd.exists() else "")
            rnd.append((t, r))
        iso = isolation_problems_now(exp)
        for t, _ in rnd:
            t["isolation_problems"] += iso
        launchable = [(t, r) for t, r in rnd if not t["isolation_problems"]]
        for t, r in rnd:
            if t["isolation_problems"]:
                r.status, r.error = "not_started", "; ".join(t["isolation_problems"])
                out(f"NOT STARTED {r.name}: {r.error}")
        S.launch_all(client, [r for _, r in launchable])
        ctx["live"] += [r for _, r in launchable]
        for t, r in launchable:
            applied = r.applied or {}
            if r.status == S.LAUNCH_FAILED:
                record["infrastructure"].append(f"{t['run_id']}: could not start the session: {r.error}")
            elif r.status == "running" and Path(applied.get("cwd") or "/nonexistent").resolve() != Path(r.cwd).resolve():
                t["isolation_problems"].append("isolation: the web server did not confirm the session runs in "
                                               "the trial's copy (restart the web server to load the route)")
                # Stop it at once: it may be running somewhere it should not.
                r.status, r.ended_at, r.error = S.CANCELLED, time.time(), "not isolated"
            t["started_at"] = r.sent_at
            out(f"started {r.name} ({r.launch_id or r.error})")
        for t, r in launchable:
            if r.status == S.CANCELLED and r.error == "not isolated":
                r.termination = S.terminate(client, r, quiet=stop_quiet)
        # The break-out watch for this round: each contestant's rules name
        # every OTHER trial's folder (this round's and earlier ones).
        pairs = []
        for t, r in launchable:
            if r.status != "running":
                continue
            others = {f"another trial's folder ({o.name[:8]}…)": str(o) for o in all_workdirs
                      if str(o) != t["workdir"]}
            pairs.append((r, _rules_for(exp, work, Path(t["workdir"]), frozen, sealed, others)))
        ctx["watch"] = W.BreakoutWatch(client, pairs, interval=watch_interval, on_trip=tripped,
                                       stop_quiet=stop_quiet).start()
        try:
            S.wait_all([r for _, r in launchable], timeout=frozen.get("timeout", 3600), settle=settle, poll=poll,
                       cancel_file=exp / "CANCEL", client=client, stop_quiet=stop_quiet,
                       on_done=lambda r: out(f"{r.status} {r.name} in {round(r.ended_at - r.sent_at, 1)}s"))
        finally:
            ctx["watch"].stop()
            if ctx["watch"].error:
                record["infrastructure"].append(f"the break-out watch failed: {ctx['watch'].error}")
            ctx["watch"] = None
        for t, r in rnd:
            finish_trial(frozen, exp, t, r, client, needles_base, all_workdirs)
            record["trials"].append(t)
            S.write_json(Path(t["dir"]) / "trial.json", t)
            out(f"{t['verdict'].upper()} {r.name}" + (f": {'; '.join(t['verdict_reasons'])}" if t["verdict_reasons"] else ""))
        S.write_json(exp / "experiment.json", record)
        if (exp / "CANCEL").exists():
            record["incomplete"].append(f"cancelled after trial round {k} of {frozen['trials']}")
            break
        if record["containment"]["trips"]:
            record["invalid"].append("a contestant broke out (see Containment); no further round was started")
            break

    record["invalid"] += check_frozen(exp)
    if frozen["evaluation"]["kind"] == "rubric":
        record["blinded"] = make_blinded_packet(exp, record, frozen)


def slug_name(n: str) -> str:
    return S.slug(n, 30)


def finish_trial(spec, exp, t, r, client, needles_base, all_workdirs):
    tdir, wd = Path(t["dir"]), Path(t["workdir"])
    t.update({"status": r.status, "error": r.error, "session_id": r.session_id, "id_history": r.id_history,
              "seconds": round(r.ended_at - r.sent_at, 1) if r.ended_at and r.sent_at else None,
              "model": r.model, "ran_model": r.ran_model, "termination": r.termination,
              "contained": r.contained, "execution_completed": r.status == S.COMPLETED})
    t["model_mismatch"] = bool(r.model and r.ran_model and not S.same_model(r.model, r.ran_model))
    t["metrics"] = S.measure(r.project_dir, r.session_id) if r.project_dir and r.session_id else {}
    t["cost_usd"] = client.cost(r.session_id) if r.session_id else None
    t["cost_basis"] = "CLI total_cost_usd (API-equivalent, measured)" if t["cost_usd"] is not None else "not reported"
    t["self_report"] = S.self_report(r.text or "")
    t["transcripts"] = S.keep_transcripts(r, tdir)
    (tdir / "reply.md").write_bytes((r.text or "").encode("utf-8"))
    still_running = (r.termination or "").startswith("unconfirmed")
    if r.status == "not_started" or not wd.exists() or not r.session_id:
        t["provenance"] = {"status": "not_run"}
        t["evaluation"] = {}
        t["contamination"] = []
    elif still_running:
        # A contestant that may still be writing is never evaluated: its copy
        # is not final, and the result could not be trusted either way.
        t["provenance"] = {"status": "not_run", "reason": "the session could not be confirmed stopped"}
        t["evaluation"] = {}
        t["contamination"] = []
    else:
        others = {f"another trial's folder ({o.name[:8]}…)": str(o) for o in all_workdirs if o != wd}
        t["contamination"] = [f"contamination: {h}" for h in
                              contamination_evidence(r.project_dir, r.session_id, {**needles_base, **others})]
        if r.contained:
            t["contamination"].append(f"contamination: break-out: {r.contained['rule']} "
                                      f"({r.contained['tool']}: {r.contained['excerpt'][:160]})")
        _git("add", "-A", cwd=wd, check=False)
        diff = _git("diff", "--cached", "HEAD", "--binary", cwd=wd, check=False).stdout
        (tdir / "changes.diff").write_text(diff, encoding="utf-8")
        t["changed_files"] = [ln for ln in _git("diff", "--cached", "--name-only", "HEAD", cwd=wd,
                                                check=False).stdout.splitlines() if ln]
        t["provenance"] = check_provenance(t["settings"].get("provenance"), wd, t["run_id"],
                                           t.get("started_at") or 0, set(t["pre_files"]), tdir / "provenance.log")
        t["evaluation"] = evaluate(spec, exp, wd, tdir) if r.status == S.COMPLETED else {}
        t["tree_sha256"] = _tree_digest(wd)
        if check_frozen(exp):
            t["contamination"].append("contamination: the sealed tests changed during this trial")
    acc = (t.get("evaluation") or {}).get("acceptance") or {}
    t["checks_executed"] = bool(acc) and acc.get("exit") is not None
    t["verdict"], t["verdict_reasons"] = trial_verdict(t)
    t["outcome_verified"] = t["verdict"] == VERIFIED


# ── rubric (non-coding) ─────────────────────────────────────────────────────

def make_blinded_packet(exp: Path, record: dict, spec: dict) -> dict:
    """Shuffle the finished outputs under neutral labels for a judge who must not
    know which approach wrote which. The key stays in a separate file."""
    items = [t for t in record["trials"] if t.get("status") == S.COMPLETED]
    labels = [f"Output {i + 1}" for i in range(len(items))]
    rnd = random.Random(secrets.randbits(64))
    rnd.shuffle(items)
    d = exp / "blinded"
    d.mkdir(exist_ok=True)
    key = {lab: t["run_id"] for lab, t in zip(labels, items)}
    S.write_json(d / "key.json", key)
    lines = ["# Blinded outputs", "", "Score each output on every criterion. You do not know which approach wrote which.",
             "", "## Criteria", ""] + [f"- {c}" for c in spec["evaluation"]["rubric"]] + [""]
    for lab, t in zip(labels, items):
        lines += [f"## {lab}", "", Path(t["dir"], "reply.md").read_text(encoding="utf-8"), ""]
    (d / "packet.md").write_text("\n".join(lines), encoding="utf-8")
    return {"packet": str(d / "packet.md"), "key": str(d / "key.json"), "count": len(items)}


def import_scores(exp: Path, scores: dict) -> dict:
    """Map a judge's scores ``{label: {criterion: score}}`` back to trials.
    The result is labelled subjective; it never makes a trial "verified"."""
    key = json.loads((exp / "blinded" / "key.json").read_text(encoding="utf-8"))
    unknown = set(scores) - set(key)
    if unknown:
        raise SpecError(f"scores for labels not in the packet: {sorted(unknown)}")
    mapped = {key[lab]: s for lab, s in scores.items()}
    S.write_json(exp / "blinded" / "scores.json", {"scores": scores, "by_trial": mapped, "basis": "subjective, blinded"})
    return mapped


# ── 6. summary and report ────────────────────────────────────────────────────

def _stats(xs):
    xs = [x for x in xs if isinstance(x, (int, float))]
    if not xs:
        return None
    return {"n": len(xs), "mean": round(statistics.mean(xs), 4), "min": min(xs), "max": max(xs),
            "stdev": round(statistics.stdev(xs), 4) if len(xs) > 1 else None}


def summarize(record: dict, spec: dict) -> dict:
    by = {}
    for v in spec["variants"]:
        ts = [t for t in record["trials"] if t["variant"] == v["name"]]
        acc = [(t.get("evaluation") or {}).get("acceptance") or {} for t in ts]
        by[v["name"]] = {
            "trials": len(ts),
            "completed": sum(t.get("status") == S.COMPLETED for t in ts),
            "verified": sum(t.get("verdict") == VERIFIED for t in ts),
            "claimed_done": sum(bool((t.get("self_report") or {}).get("claims_done")) for t in ts),
            "failed_checks": _stats([a.get("failed", 0 if a.get("exit") == 0 else None) for a in acc]),
            "seconds": _stats([t.get("seconds") for t in ts]),
            "cost_usd": _stats([t.get("cost_usd") for t in ts]),
            "cost_missing": sum(t.get("cost_usd") is None for t in ts),
            "model_calls": _stats([(t.get("metrics") or {}).get("model_calls") for t in ts]),
            "tool_calls": _stats([(t.get("metrics") or {}).get("tool_calls") for t in ts]),
            "retries": sum((t.get("metrics") or {}).get("api_error_retries", 0) for t in ts),
            "human_interventions": sum((t.get("metrics") or {}).get("human_interventions", 0) for t in ts),
            "contaminated": sum(bool(t.get("contamination")) for t in ts),
            "provenance_failures": sum((t.get("provenance") or {}).get("status") not in ("verified", "not_applicable")
                                       for t in ts),
        }
    # Experiment status, kept apart from how each contestant did: a contestant
    # that fails the checks is a result; a run that could not be carried out
    # or evaluated is not.
    infra = list(record.get("infrastructure") or [])
    invalid = list(record.get("invalid") or [])
    incomplete = list(record.get("incomplete") or [])
    for t in record["trials"]:
        rid = t["run_id"]
        if (t.get("termination") or "").startswith("unconfirmed"):
            infra.append(f"{rid}: {t['termination']}")
        if t.get("status") == S.LAUNCH_FAILED:
            infra.append(f"{rid}: the session could not be started")
        acc = (t.get("evaluation") or {}).get("acceptance") or {}
        if t.get("status") == S.COMPLETED and acc and acc.get("exit") is None:
            infra.append(f"{rid}: the acceptance checks could not run")
        if t.get("isolation_problems"):
            invalid.append(f"{rid}: " + "; ".join(t["isolation_problems"]))
        if t.get("contamination"):
            invalid.append(f"{rid}: " + "; ".join(t["contamination"]))
        pv = t.get("provenance") or {}
        if pv.get("status") == "failed":
            invalid.append(f"{rid}: implementation not proven: {pv.get('reason')}")
        evaluated = t.get("status") == S.COMPLETED and (
            t.get("checks_executed") or (t.get("evaluation") or {}).get("kind") == "rubric")
        if t.get("status") == "not_started":
            pass  # its isolation problem is already in `invalid`
        elif not evaluated and not t.get("contained"):
            incomplete.append(f"{rid}: {t.get('status')}, never evaluated")
    trips = (record.get("containment") or {}).get("trips") or []
    if infra:
        status = "infrastructure_failure"
    elif invalid or trips:
        status = "invalid"
    elif incomplete or not record["trials"]:
        status = "incomplete"
    else:
        status = "completed"
    min_n = min((b["trials"] for b in by.values()), default=0)
    subjective = spec["evaluation"]["kind"] == "rubric"
    ranked = sorted(by.items(), key=lambda kv: (-kv[1]["verified"], (kv[1]["failed_checks"] or {}).get("mean", 1e9)))
    winner = None
    if status != "completed":
        verdict = status
    elif subjective:
        verdict = "subjective"
    elif len(ranked) > 1 and ranked[0][1]["verified"] > ranked[1][1]["verified"]:
        verdict, winner = ("pilot" if min_n < 2 else "supported"), ranked[0][0]
    else:
        verdict = "inconclusive"
    return {"by_variant": by, "experiment_status": status, "infrastructure_reasons": infra,
            "invalid_reasons": invalid, "incomplete_reasons": incomplete, "verdict": verdict,
            "leader": winner, "min_trials": min_n, "break_outs": len(trips)}


def _best(by, key, stat="mean", low=True):
    vals = {n: (b[key] or {}).get(stat) if isinstance(b[key], dict) else b[key] for n, b in by.items()}
    vals = {n: v for n, v in vals.items() if v is not None}
    if len(vals) < 2 or len(set(vals.values())) == 1:
        return None
    return (min if low else max)(vals, key=vals.get)


def render_report(record: dict, spec: dict) -> str:
    s = record["summary"]
    by = s["by_variant"]
    L = [f"# Controlled A/B Test {record['experiment']}", "", f"**Question:** {record['question']}", "",
         f"- Differs on purpose: {', '.join(spec['differs'])}",
         f"- Trials per variant: {spec['trials']}",
         f"- Evaluation: {spec['evaluation']['kind']} "
         + ("(objective, run by the controller on each trial's finished copy)"
            if spec['evaluation']['kind'] == 'command' else "(blinded rubric: subjective)"),
         f"- Spec hash: `{record['spec_sha256'][:16]}`", ""]
    status_text = {
        "completed": "Completed: every trial ran to the end and was evaluated (a contestant failing the checks is a result).",
        "infrastructure_failure": "INFRASTRUCTURE FAILURE: the experiment could not be run or evaluated correctly.",
        "invalid": "INVALID EXPERIMENT: its conditions were broken, so no conclusion can be drawn.",
        "incomplete": "INCOMPLETE: not every trial ran to the end and was evaluated (a timeout or a cancel is not an evaluation).",
    }[s["experiment_status"]]
    L += ["## Experiment status", "", status_text, ""]
    for label, key in (("Infrastructure", "infrastructure_reasons"), ("Invalid", "invalid_reasons"),
                       ("Incomplete", "incomplete_reasons")):
        L += [f"- {label}: {r}" for r in s.get(key) or []]
    q = record.get("qualification") or {}
    if q.get("level"):
        L += ["", f"- Acceptance checks qualified: {q['level']}."]
        if "reference" not in q:
            L += ["- No reference solution was given, so the checks were not shown to pass on a valid solution."]
    ht = record.get("hidden_tests")
    if ht == "not_secret":
        L += ["- Hidden tests: NOT protected; contestants could read them. This experiment makes no hidden-test claim."]
    L += ["", "## Containment", ""]
    iso = record.get("isolation") or {}
    L += ["- Isolation level: **access control on this account, watched for break-outs**; not operating-system isolation. "
          f"Trial folders have unguessable names under a parent this account cannot list ({iso.get('method', '?')}); "
          "the hidden tests are sealed with the key kept only there; your test and reference files were never changed."]
    c = record.get("containment") or {}
    L += [f"- Watched every {c.get('interval_s', '?')} s in each contestant's tool calls: " + ", ".join(c.get("watched") or []) + "."]
    trips = c.get("trips") or []
    if trips:
        L += ["- **Break-outs:**"]
        for tr in trips:
            when = time.strftime("%H:%M:%S", time.localtime(tr["at"]))
            L += [f"  - {tr['run']} at {when}: {tr['rule']} ({tr['tool']}: `{tr['excerpt'][:120]}`); "
                  f"session stopped: {tr.get('termination')}"]
    else:
        L += ["- Break-outs: none; watched, nothing tripped."]
    integ = c.get("integrity") or []
    if integ:
        L += ["- Checked at the end: " + "; ".join(f"{i['what']} {'unchanged' if i['ok'] else 'CHANGED'}" for i in integ) + "."]
    sd = [x for x in (c.get("shutdown") or []) if x.get("termination")]
    if sd:
        L += ["- Sessions stopped by the controller: " + "; ".join(f"{x['run']} ({x['termination']})" for x in sd) + "."]
    L += [""]
    L += ["## Results by variant", "",
          "| Variant | Trials | Completed | Verified | Claimed done | Failed checks (mean) | Time s (mean / sd) | Cost $ (mean) | Calls | Retries | Human help |",
          "|---|---|---|---|---|---|---|---|---|---|---|"]
    for n, b in by.items():
        sec = b["seconds"] or {}
        cost = b["cost_usd"] or {}
        L.append(f"| {S.md_cell(n, 40)} | {b['trials']} | {b['completed']} | **{b['verified']}** | {b['claimed_done']} | "
                 f"{(b['failed_checks'] or {}).get('mean', '-')} | {sec.get('mean', '-')} / {sec.get('stdev') or '-'} | "
                 f"{cost.get('mean', 'not reported')}{' (' + str(b['cost_missing']) + ' missing)' if b['cost_missing'] else ''} | "
                 f"{(b['model_calls'] or {}).get('mean', '-')} | {b['retries']} | {b['human_interventions']} |")
    L += ["", "Time is each session's wall clock; parallel sub-agents are not summed into it. "
          "Cost is the CLI's reported API-equivalent cost; a missing value is not estimated.", "",
          "## Trials", "", "| Trial | Status | Verdict | Why not verified | Implementation | Time s |",
          "|---|---|---|---|---|---|"]
    for t in record["trials"]:
        pv = t.get("provenance") or {}
        L.append(f"| {t['run_id'].split('/')[-1]} | {t.get('status')} | {t.get('verdict')} | "
                 f"{S.md_cell('; '.join(t.get('verdict_reasons') or []) or '-', 200)} | "
                 f"{S.md_cell(pv.get('impl') or pv.get('status'), 60)} | {t.get('seconds') or '-'} |")
    q = []
    lead = s["leader"]
    q.append(f"1. **More correct:** " + (f"{lead} ({by[lead]['verified']} of {by[lead]['trials']} verified)"
                                         if lead else "no difference established"))
    fd = _best(by, "failed_checks")
    q.append(f"2. **Fewer consequential defects (failed checks):** {fd or 'no difference measured'}")
    q.append(f"3. **Less time / cost:** {_best(by, 'seconds') or 'no difference measured'} / "
             f"{_best(by, 'cost_usd') or 'no difference measured'}")
    q.append(f"4. **More consistent (lower time spread):** {_best(by, 'seconds', 'stdev') or 'not measurable with these trials'}")
    hi = _best(by, "human_interventions", low=False)
    q.append(f"5. **Needed more human intervention:** {hi or 'no difference'}")
    est = {"invalid": "Nothing: the experiment is invalid (see above).",
           "infrastructure_failure": "Nothing: the experiment could not be run or evaluated (see above).",
           "incomplete": "Nothing yet: not every trial ran.",
           "subjective": "A blinded, subjective rubric comparison; no objective verification.",
           "pilot": f"{lead} did better in a single-trial pilot. One run is not proof of general superiority; "
                    "run three or more trials per variant.",
           "supported": f"{lead} produced more verified results across {s['min_trials']}+ trials per variant. "
                        "That supports, but does not statistically prove, it is better at this assignment.",
           "inconclusive": "Inconclusive: no variant produced more verified results. Speed or self-reported "
                           "grades do not decide a winner."}[s["verdict"]]
    q.append(f"6. **What the evidence establishes:** {est}")
    L += ["", "## Answers", ""] + q + ["", "## Records", "",
          f"Every trial's prompt, transcript copy, changes, provenance, acceptance and regression logs are in "
          f"`trials/<trial>/` beside this report.", ""]
    return "\n".join(L)
