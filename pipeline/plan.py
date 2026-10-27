"""The verification plan: what proves each acceptance criterion, as data.

The Plan stage ends ``01-plan.md`` (or, at light depth, ``00-request.md``)
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

``run_check`` records the SHA-256 of the block it read into every evidence
file, and the harness compares that with the block on disk at grading time,
so a plan edited after its checks ran invalidates them.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from pathlib import Path

FENCE_RE = re.compile(r"```verification[ \t]*\r?\n(.*?)\r?\n```", re.DOTALL)

PLAN_FILES = ("01-plan.md", "05-debug.md", "06-hotfix.md", "07-refactor.md", "00-request.md")


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
    extra: dict = field(default_factory=dict)


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
        current[key.strip()] = _value(raw)
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
            if k in ("required", "executable"):
                if str(v).lower() not in _BOOL:
                    raise PlanError(f"verification {cid}: {k} must be true or false")
                setattr(c, k, _BOOL[str(v).lower()])
            elif k == "timeout":
                try:
                    c.timeout = int(v)
                except ValueError:
                    raise PlanError(f"verification {cid}: timeout must be an integer") from None
            elif k in ("cmd", "expect", "reason", "cwd"):
                setattr(c, k, str(v))
            else:
                c.extra[k] = v
        if c.executable and not c.cmd:
            raise PlanError(f"verification {cid}: executable but has no cmd")
        if not c.executable and not c.reason:
            raise PlanError(f"verification {cid}: not executable, so it needs a reason")
        if c.expect:
            try:
                re.compile(c.expect)
            except re.error as e:
                raise PlanError(f"verification {cid}: expect is not a valid regex ({e})") from None
        out.append(c)
    if not out:
        raise PlanError("the verification block lists no criteria")
    return out


def load_plan(run_dir: Path) -> tuple[list[Criterion], str, Path]:
    """Criteria, the block's sha and the file it came from; raises PlanError."""
    block, src = find_verification_block(run_dir)
    if block is None:
        raise PlanError("no ```verification block in " + ", ".join(PLAN_FILES))
    return parse_criteria(block), block_sha(block), src
