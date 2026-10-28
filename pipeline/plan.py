"""The verification plan: what proves each acceptance criterion, as data.

The Plan stage ends ``01-plan.md`` (or, at light depth, ``00-plan.md``)
with a fenced block the programs in this package read::

    ```verification
    - id: suite
      cmd: python -m pytest -q --no-header tests/test_thing.py
      expect: \\d+ passed
    - id: browser
      cmd: python -m pytest -q --no-header tests/test_thing_browser.py
      expect: \\d+ passed
    - id: native-ui
      executable: false
      reason: the desktop UI cannot be driven on this machine
    ```

Keys per criterion: ``id`` (required), ``cmd`` (required when executable),
``expect`` (regex searched in the command's output; default: exit status 0 is
enough), ``required`` (default true; a failing optional criterion is noted,
not fatal), ``executable`` (default true; false lists the criterion as
unexecuted, which caps the grade at MEDIUM), ``reason`` (why it cannot run),
``cwd`` (relative to the repository), ``timeout`` (seconds, default 1800).

``{python}`` in a ``cmd`` is replaced by the interpreter running the check, so
a plan never names a machine's virtualenv path.

Coverage keys:

* ``suite: true`` marks the project's full test command.  A repository pins
  that command in ``.pipeline-verify`` at its root (same list syntax, no
  fence); ``pipeline.start`` copies it into the run as ``pinned-verify.txt``
  and those criteria are added to every plan, required, so a builder cannot
  drop or weaken them.  A plan marks a criterion ``suite: true`` only when the
  repository has no declaration yet (a new project).
* ``detects_change: true`` is the regression demonstration: the criterion must
  FAIL on the code as it was before the run, with the new tests copied in.
  It needs ``baseline_files`` (comma-separated globs: the test files to copy
  into the baseline) and ``fails_with`` (a regex the baseline failure must
  print, naming the intended behavior, so an unrelated breakage does not
  count).  ``pipeline.run_check --baseline`` runs it in an isolated git
  worktree; the working directory is never touched.
* ``waiver: true`` on a non-executable item states why no regression
  demonstration applies (config only, documentation, no observable behavior).
  It does not cap the grade by itself; the independent audit must name it in
  its coverage section and still judge the request covered.

``run_check`` records the SHA-256 of the block it read into every evidence
file, and the harness compares that with the block on disk at grading time,
so a plan edited after its checks ran invalidates them.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from pathlib import Path

FENCE_RE = re.compile(r"```verification[ \t]*\r?\n(.*?)\r?\n```", re.DOTALL)

# Where the verification block may live.  ``00-request.md`` is NOT on the
# list: it holds the user's request verbatim, hashed at the start, and a light
# run that has no Plan Team puts its block in ``00-plan.md`` instead.
PLAN_FILES = ("01-plan.md", "05-debug.md", "06-hotfix.md", "07-refactor.md", "00-plan.md")


VERIFY_FILE = ".pipeline-verify"   # the repository's pinned verification, at its root
PINNED_FILE = "pinned-verify.txt"  # the copy pipeline.start takes into the run
PROTECTED_FILE = ".pipeline-protected"       # the repository's protected paths, at its root
PINNED_PROTECTED_FILE = "pinned-protected.txt"  # the copy pipeline.start takes into the run
REQUEST_MARKER = "## Original request (verbatim)"


def request_sha(text: str) -> str:
    """SHA-256 of the user's request as ``pipeline.start`` wrote it: line
    endings normalised, leading and trailing newlines ignored (the marker
    heading is followed by a blank line, so ``request_text_of`` cannot tell a
    request's own leading blank lines from it), nothing else touched."""
    return hashlib.sha256(text.replace("\r\n", "\n").strip("\n").encode("utf-8")).hexdigest()


def request_text_of(request_md: str) -> str | None:
    """The verbatim request inside ``00-request.md``: everything after the
    marker heading.  ``None`` when the marker is missing."""
    idx = request_md.find(REQUEST_MARKER)
    if idx < 0:
        return None
    rest = request_md[idx + len(REQUEST_MARKER):]
    nl = rest.find("\n")
    rest = rest[nl + 1:] if nl >= 0 else ""
    return rest.lstrip("\r\n")


@dataclass
class Criterion:
    id: str
    cmd: str = ""
    expect: str = ""
    required: bool = True
    executable: bool = True
    reason: str = ""
    cwd: str = ""
    timeout: int = 1800
    suite: bool = False
    detects_change: bool = False
    fails_with: str = ""
    baseline_files: str = ""
    waiver: bool = False
    pinned: bool = False

    def baseline_globs(self) -> list[str]:
        return [g.strip() for g in self.baseline_files.split(",") if g.strip()]


class PlanError(ValueError):
    pass


def find_verification_block(run_dir: Path) -> tuple[str, Path] | tuple[None, None]:
    """The first ``verification`` fence in the run's plan files, with its file."""
    for name in PLAN_FILES:
        p = run_dir / name
        if not p.is_file():
            continue
        m = FENCE_RE.search(p.read_text(encoding="utf-8", errors="replace"))
        if m:
            return m.group(1), p
    return None, None


def block_sha(block: str) -> str:
    return hashlib.sha256(block.replace("\r\n", "\n").strip().encode("utf-8")).hexdigest()


_BOOL = {"true": True, "yes": True, "on": True, "false": False, "no": False, "off": False}


def _value(raw: str):
    v = raw.strip()
    if len(v) >= 2 and v[0] == v[-1] and v[0] in "\"'":
        return v[1:-1]
    return v


KNOWN_KEYS = frozenset({"id", "cmd", "expect", "required", "executable", "reason", "cwd", "timeout", "suite",
                        "detects_change", "fails_with", "baseline_files", "waiver"})


def check_pinnable(crits: list[Criterion]) -> None:
    """A pinned check is the project's mandatory verification: it must run.
    A pinned item that is not executable, or is a waiver, would let a run earn
    HIGH without the declared suite ever running (external review, finding 2)."""
    for c in crits:
        if c.waiver or not c.executable:
            raise PlanError(f"pinned criterion {c.id!r} must be executable and cannot be waived; "
                            f"a project's mandatory checks always run")


def parse_criteria(block: str) -> list[Criterion]:
    """Parse the fenced block.  Deliberately a small, strict subset of YAML:
    a list of flat mappings, one ``- id:`` per item, no nesting."""
    items: list[dict] = []
    current: dict | None = None
    for ln, line in enumerate(block.replace("\r\n", "\n").split("\n"), 1):
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        stripped = line.strip()
        if stripped.startswith("- "):
            current = {}
            items.append(current)
            stripped = stripped[2:].strip()
            if not stripped:
                continue
        if current is None:
            raise PlanError(f"verification line {ln}: key outside a '- id:' item")
        if ":" not in stripped:
            raise PlanError(f"verification line {ln}: expected 'key: value'")
        key, _, raw = stripped.partition(":")
        key = key.strip()
        # A typo (``expcet``) silently dropped an assertion, and a repeated key
        # silently replaced the first value (external review, finding 9).
        if key not in KNOWN_KEYS:
            raise PlanError(f"verification line {ln}: unknown key {key!r}; known keys: {', '.join(sorted(KNOWN_KEYS))}")
        if key in current:
            raise PlanError(f"verification line {ln}: key {key!r} given twice in one item")
        if not _value(raw).strip():
            # ``expect:`` with nothing after it silently dropped the assertion
            # (found by the Review Team of the evidence-gaps run).  Leave the
            # key out instead.
            raise PlanError(f"verification line {ln}: key {key!r} has no value; leave it out instead")
        current[key] = _value(raw)
    out: list[Criterion] = []
    seen: set[str] = set()
    for d in items:
        cid = str(d.get("id") or "").strip()
        if not cid:
            raise PlanError("a verification item has no id")
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,63}", cid):
            raise PlanError(f"verification id {cid!r}: letters, digits, '-', '_' and '.' only")
        if cid in seen:
            raise PlanError(f"verification id {cid!r} repeated")
        seen.add(cid)
        c = Criterion(id=cid)
        for k, v in d.items():
            if k == "id":
                continue
            if k in ("required", "executable", "suite", "detects_change", "waiver"):
                if str(v).lower() not in _BOOL:
                    raise PlanError(f"verification {cid}: {k} must be true or false")
                setattr(c, k, _BOOL[str(v).lower()])
            elif k == "timeout":
                try:
                    c.timeout = int(v)
                except ValueError:
                    raise PlanError(f"verification {cid}: timeout must be an integer") from None
            elif k in ("cmd", "expect", "reason", "cwd", "fails_with", "baseline_files"):
                setattr(c, k, str(v))
        if c.executable and not c.cmd:
            raise PlanError(f"verification {cid}: executable but has no cmd")
        if not c.executable and not c.reason:
            raise PlanError(f"verification {cid}: not executable, so it needs a reason")
        for key in ("expect", "fails_with"):
            if getattr(c, key):
                try:
                    re.compile(getattr(c, key))
                except re.error as e:
                    raise PlanError(f"verification {cid}: {key} is not a valid regex ({e})") from None
        if c.detects_change and not (c.fails_with and c.baseline_globs()):
            raise PlanError(f"verification {cid}: detects_change needs fails_with and baseline_files")
        if c.detects_change and not c.executable:
            raise PlanError(f"verification {cid}: detects_change must be executable")
        if c.waiver and c.executable:
            raise PlanError(f"verification {cid}: a waiver is a non-executable item with a reason")
        out.append(c)
    if not out:
        raise PlanError("the verification block lists no criteria")
    return out


def load_pinned(run_dir: Path) -> tuple[list[Criterion], str | None]:
    """The criteria ``pipeline.start`` pinned from the repository's
    ``.pipeline-verify``, and the sha of that text; ([], None) when the
    repository declared none."""
    p = run_dir / PINNED_FILE
    if not p.is_file():
        return [], None
    text = p.read_text(encoding="utf-8", errors="replace")
    crits = parse_criteria(text)
    check_pinnable(crits)
    for c in crits:
        c.pinned = True
        c.required = True
        c.suite = True
    return crits, block_sha(text)


def load_plan(run_dir: Path) -> tuple[list[Criterion], str, Path]:
    """Criteria (pinned first, then the plan's), one sha over both, and the plan
    file; raises PlanError.  Evidence records that sha, so editing either the
    plan or the pinned copy after a check ran invalidates the check."""
    block, src = find_verification_block(run_dir)
    if block is None:
        raise PlanError("no ```verification block in " + ", ".join(PLAN_FILES))
    crits = parse_criteria(block)
    sha = block_sha(block)
    pinned, pinned_sha = load_pinned(run_dir)
    if pinned:
        clash = {c.id for c in pinned} & {c.id for c in crits}
        if clash:
            raise PlanError(f"the plan redefines pinned criterion {', '.join(sorted(clash))} "
                            f"from {VERIFY_FILE}; a plan adds criteria, it does not replace pinned ones")
        crits = pinned + crits
        sha = hashlib.sha256(f"{pinned_sha}:{sha}".encode()).hexdigest()
    return crits, sha, src
