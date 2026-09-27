"""
Update safety — architecture- and environment-aware ``claude-code-sdk`` upgrade.

WHY THIS EXISTS
---------------
VibeNode's daily background updater used to run::

    pip install --quiet --upgrade claude-code-sdk

against whatever interpreter started the app. That is unsafe in three ways.

1. **Shared global Python.** In the current x64 deployment, ``sys.executable``
   is ``C:\\Python314\\pythonw.exe``, which resolves to a per-user
   ``site-packages`` shared with other tools (Claude-Patent-Creator,
   InventNode, ad-hoc scripts). An eager ``--upgrade`` there mutates every
   transitive dependency and can silently downgrade or churn packages other
   apps depend on. It also runs invisibly: ``--quiet`` swallows pip's
   stderr, and the outer ``except Exception`` prints a single warning, so a
   real resolution failure looks the same as a healthy no-op.

2. **Windows ARM64 hosts (Step 3 future target).** ``cryptography`` (a
   transitive dep via ``claude-code-sdk → mcp → pyjwt[crypto]``) publishes
   no Windows ARM64 wheel after 46.0.3, and current releases require a Rust
   + MSVC ARM64 toolchain to build from source. A plain ``--upgrade`` on
   ARM64 (even inside a dedicated venv) can invoke that source build and
   either fail dirty or half-succeed in ways that break the SDK's import.

3. **Invisible failure everywhere.** ``--quiet`` suppresses pip's own
   diagnostics; the wrapping ``except Exception`` swallows the rest. A
   real resolver failure looks identical to a healthy no-op in the boot
   log.

DESIGN
------
This module implements one function, :func:`upgrade_sdk_safely`, whose
contract is:

    AUTO-UPDATE ONLY WHEN IT CAN BE DONE SAFELY.

Concretely, that means five hard rules the implementation follows in
order:

(1) **Never build from source.** Every pip invocation (dry-run and real)
    passes ``--only-binary=:all:``. If the resolver cannot find a wheel
    for the target architecture — the exact ARM64 cryptography case — it
    reports "No matching distribution found" and returns non-zero. No
    Rust or MSVC toolchain can ever be invoked.

(2) **Plan before touching.** Every real install is preceded by
    ``pip install --dry-run …``. If the dry-run's resolver fails, we
    abort with pip's actual complaint; the environment is byte-identical
    to what it was on entry. The tricky ARM64 failure mode ("SDK moves
    but its new pyjwt[crypto] requires a cryptography version that has
    no ARM64 wheel") shows up here as a clean dry-run rejection, not a
    half-applied install.

(3) **Enforce shared-vs-isolated on the plan, not on the environment
    guess.** After a successful dry-run, we parse the "Would install"
    line to see what packages pip intends to touch. If we are in a
    shared global Python AND the plan touches anything beyond the SDK
    itself, we refuse — that would mutate other apps' packages.
    Isolated (dedicated venv) callers proceed regardless of plan size,
    because mutating a private venv is exactly what a venv is for.

(4) **Real install mirrors the dry-run.** Same flags, minus
    ``--dry-run``. Shared env additionally applies ``--no-deps`` as
    belt-and-suspenders (dry-run already proved there are no deps to
    change; ``--no-deps`` guarantees pip cannot decide otherwise
    mid-run).

(5) **Verify with pip check.** After the real install, run
    ``pip check`` and inspect its output for any line mentioning the
    SDK package name. If the SDK's declared requirements are not
    satisfied post-install (a rare failure of dry-run to predict
    reality), surface that as ``STATUS_BLOCKED_INCONSISTENT`` — the
    user must decide whether to intervene manually.

Never a source build, never an eager transitive resolve, never a silent
skip. When the update cannot be done safely, the environment stays
byte-identical and the failure surfaces both in the boot log and in the
persisted state file.

Two operator switches control coarse behavior::

    VIBENODE_NO_AUTO_UPDATE=1   disables everything (existing).
    VIBENODE_NO_PIP_UPDATE=1    disables just the pip step (new; the
                                CLI self-update still runs).
    VIBENODE_UPDATE_ISOLATED=1  force isolated=True. Reserved for the
                                Step 3 ARM64 venv launcher whose
                                two-process model can confuse a bare
                                sys.prefix probe.

The ``runner`` argument is injectable so tests can prove exact pip
invocation without touching any real Python environment.
"""

from __future__ import annotations

import os
import re
import sys
from typing import Callable, Mapping, Optional


# Feature flags.
_ENV_NO_AUTO_UPDATE = "VIBENODE_NO_AUTO_UPDATE"
_ENV_NO_PIP_UPDATE = "VIBENODE_NO_PIP_UPDATE"
_ENV_ISOLATED = "VIBENODE_UPDATE_ISOLATED"


# Status vocabulary. Callers that dispatch on ``result["status"]`` MUST
# handle every constant here.
STATUS_UPGRADED = "upgraded"
STATUS_NO_CHANGE = "no_change"
STATUS_SKIPPED_BY_ENV = "skipped_by_env"
STATUS_BLOCKED_MISSING_WHEEL = "blocked_missing_wheel"
STATUS_BLOCKED_SHARED_ENV_DEP_CHANGE = "blocked_shared_env_dep_change"
STATUS_BLOCKED_INCONSISTENT = "blocked_inconsistent"
STATUS_FAILED = "failed"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_WOULD_INSTALL_LINE = re.compile(r"^\s*Would install\s+(.+?)\s*$", re.IGNORECASE)
# "pkg-name-1.2.3" → name = "pkg-name". A version starts with a digit; anything
# up to that boundary is the name. We deliberately require the version segment
# to start with a digit so a hyphen inside the name (e.g. ``claude-code-sdk``)
# is preserved.
_PKG_NAME_VERSION = re.compile(r"^(?P<name>.+?)-(?P<version>\d[\w.\-+!]*)$")


def _canonical(name: str) -> str:
    """PEP 503-ish normalization: lowercase, underscores → hyphens."""
    return name.strip().lower().replace("_", "-")


def _extract_would_install_names(output: str) -> list[str]:
    """Parse pip dry-run output for the packages it plans to install.

    Returns lowercased, hyphen-canonicalized dist names. If parsing fails or
    no "Would install" line appears (pip already has everything at the
    requested version), returns [] — which callers treat as "no dependency
    changes were planned" rather than "unknown."
    """
    names: list[str] = []
    for line in output.splitlines():
        m = _WOULD_INSTALL_LINE.match(line)
        if not m:
            continue
        for tok in m.group(1).split():
            m2 = _PKG_NAME_VERSION.match(tok)
            if m2:
                names.append(_canonical(m2.group("name")))
            else:
                # Fallback: token has no obvious name-version split. Keep it
                # so a conservative "did the plan touch anything unusual"
                # check still fires.
                names.append(_canonical(tok))
    return names


def _sdk_line_in_pip_check(output: str, sdk_name: str) -> Optional[str]:
    """Return the first pip-check line that mentions ``sdk_name``, or None."""
    canonical_sdk = _canonical(sdk_name)
    for line in output.splitlines():
        low = line.lower()
        if canonical_sdk in low or canonical_sdk.replace("-", "_") in low:
            return line.strip()
    return None


def _tail(text: str, lines: int = 30) -> str:
    if not text:
        return ""
    return "\n".join(text.rstrip().splitlines()[-lines:])


def _first_error_line(output: str) -> str:
    """Return the last non-empty line of pip output — pip's resolver puts its
    real complaint at the bottom (e.g. 'No matching distribution found for
    cryptography>=46.0.7'). Falls back to the last non-empty line of the
    whole tail when no ``ERROR:`` marker is present."""
    lines = [ln.rstrip() for ln in (output or "").splitlines() if ln.strip()]
    if not lines:
        return ""
    for line in reversed(lines):
        if line.upper().startswith("ERROR:"):
            return line.strip()
    return lines[-1].strip()


def _default_runner(cmd, timeout):
    """Fallback runner. Production callers pass ``run._run_captured`` which
    understands Windows pipe deadlocks; tests inject their own runner and
    never hit this path."""
    import subprocess
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return r.returncode, (r.stdout or "") + (r.stderr or "")
    except Exception as exc:  # noqa: BLE001
        return None, str(exc)


def _pip_show_version(runner: Callable, interpreter: str, pkg: str, timeout: int = 15) -> Optional[str]:
    """Return the installed version of ``pkg``, or None."""
    rc, out = runner([interpreter, "-m", "pip", "show", pkg], timeout)
    if rc != 0 or not out:
        return None
    for line in out.splitlines():
        if line.lower().startswith("version:"):
            return line.split(":", 1)[1].strip()
    return None


# ---------------------------------------------------------------------------
# Isolation detection
# ---------------------------------------------------------------------------

def is_isolated_interpreter(interpreter: str,
                            env: Optional[Mapping[str, str]] = None) -> bool:
    """Return True if ``interpreter`` is a dedicated venv (safe to mutate
    transitive deps in).

    Note: even in an isolated interpreter, this module refuses to build from
    source (``--only-binary=:all:``) and refuses to eagerly resolve past what
    the dry-run planned. Isolation only unlocks the "shared env would need
    dep changes" refusal (rule 3 in the module docstring). Every other
    safety property applies universally.
    """
    env = env if env is not None else os.environ
    if str(env.get(_ENV_ISOLATED, "")).strip() == "1":
        return True

    try:
        same = os.path.realpath(interpreter) == os.path.realpath(sys.executable)
    except (TypeError, ValueError):
        same = False
    if same:
        return sys.prefix != sys.base_prefix

    low = str(interpreter).replace("\\", "/").lower()
    return "/.venv/" in low or low.endswith("/.venv/scripts/python.exe") \
        or low.endswith("/.venv/scripts/pythonw.exe") \
        or "/venv/" in low


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

def upgrade_sdk_safely(
    interpreter: Optional[str] = None,
    *,
    runner: Optional[Callable] = None,
    env: Optional[Mapping[str, str]] = None,
    timeout: int = 120,
    pkg: str = "claude-code-sdk",
) -> dict:
    """Upgrade ``pkg`` (default ``claude-code-sdk``) safely, or refuse.

    Never raises. Every failure mode maps to a structured dict:

        status: one of the STATUS_* constants
        interpreter: str
        isolated: bool
        dry_run_returncode: int | None
        dry_run_output_tail: str
        dry_run_plan: list[str]     canonical names pip would install
        pip_returncode: int | None
        pip_output_tail: str
        pip_command: list[str] | None
        pip_check_returncode: int | None
        pip_check_output_tail: str
        before_version: str | None
        after_version: str | None
        reason: str                 human-readable, non-empty on every path
    """
    env = env if env is not None else os.environ
    runner = runner if runner is not None else _default_runner
    interpreter = interpreter or sys.executable
    sdk_canonical = _canonical(pkg)

    result: dict = {
        "status": STATUS_FAILED,
        "interpreter": interpreter,
        "isolated": False,
        "dry_run_returncode": None,
        "dry_run_output_tail": "",
        "dry_run_plan": [],
        "pip_returncode": None,
        "pip_output_tail": "",
        "pip_command": None,
        "pip_check_returncode": None,
        "pip_check_output_tail": "",
        "before_version": None,
        "after_version": None,
        "reason": "",
    }

    # -----------------------------------------------------------------
    # Rule 0 — Feature gates. Never touch pip if the operator disabled it.
    # -----------------------------------------------------------------
    if str(env.get(_ENV_NO_AUTO_UPDATE, "")).strip():
        result["status"] = STATUS_SKIPPED_BY_ENV
        result["reason"] = f"{_ENV_NO_AUTO_UPDATE} is set; upgrade skipped."
        return result
    if str(env.get(_ENV_NO_PIP_UPDATE, "")).strip():
        result["status"] = STATUS_SKIPPED_BY_ENV
        result["reason"] = f"{_ENV_NO_PIP_UPDATE} is set; pip step skipped."
        return result

    isolated = is_isolated_interpreter(interpreter, env=env)
    result["isolated"] = isolated

    before = _pip_show_version(runner, interpreter, pkg)
    result["before_version"] = before

    # -----------------------------------------------------------------
    # Rule 1 & 2 — Never source-build; plan before touching.
    #
    # ``--only-binary=:all:`` is the load-bearing arch-safety flag: it makes
    # pip refuse to try building any package from source, everywhere. On
    # Windows ARM64 that turns the cryptography-wheel-missing case into a
    # clean resolver failure ("No matching distribution found") rather than
    # a Rust/MSVC build attempt that either fails dirty or half-succeeds.
    # It is applied to the DRY-RUN as well as the real install, because if
    # the plan requires a source build the plan itself is unsafe.
    # -----------------------------------------------------------------
    dry_cmd = [
        interpreter, "-m", "pip", "install", "--dry-run",
        "--disable-pip-version-check", "--only-binary=:all:",
        "--upgrade", pkg,
    ]
    dry_rc, dry_out = runner(dry_cmd, timeout)
    result["dry_run_returncode"] = dry_rc
    result["dry_run_output_tail"] = _tail(dry_out, 30)

    if dry_rc != 0:
        # Resolver failure. Environment is byte-identical to entry.
        # This is the ARM64-cryptography path: pip prints
        #   "ERROR: No matching distribution found for cryptography>=46.0.7"
        # and returns non-zero. Surface it verbatim.
        result["status"] = STATUS_BLOCKED_MISSING_WHEEL
        result["reason"] = (
            f"{pkg} upgrade blocked: dry-run resolver failed with wheels-only "
            f"(no {sys.platform if hasattr(sys, 'platform') else 'target'} "
            f"wheel available for a required package). {_first_error_line(dry_out)}"
        )
        return result

    plan = _extract_would_install_names(dry_out)
    result["dry_run_plan"] = plan
    non_sdk_would_change = [p for p in plan if p != sdk_canonical]

    # -----------------------------------------------------------------
    # Rule 3 — Shared env cannot safely mutate neighbours.
    # -----------------------------------------------------------------
    if not isolated and non_sdk_would_change:
        result["status"] = STATUS_BLOCKED_SHARED_ENV_DEP_CHANGE
        result["reason"] = (
            f"{pkg} upgrade blocked: shared Python environment (packages "
            f"shared with other apps). This release would also change: "
            f"{', '.join(non_sdk_would_change)}. Migrate VibeNode to a "
            f"dedicated venv, or update these dependencies manually."
        )
        return result

    # -----------------------------------------------------------------
    # Rule 4 — Real install mirrors the dry-run's flags.
    # -----------------------------------------------------------------
    if isolated:
        cmd = [
            interpreter, "-m", "pip", "install",
            "--disable-pip-version-check", "--only-binary=:all:",
            "--upgrade", pkg,
        ]
    else:
        # Shared env: dry-run proved only the SDK moves; --no-deps is
        # belt-and-suspenders against pip changing its mind mid-run.
        cmd = [
            interpreter, "-m", "pip", "install",
            "--disable-pip-version-check", "--only-binary=:all:",
            "--upgrade", "--no-deps", pkg,
        ]
    result["pip_command"] = list(cmd)

    real_rc, real_out = runner(cmd, timeout)
    result["pip_returncode"] = real_rc
    result["pip_output_tail"] = _tail(real_out, 30)

    if real_rc != 0:
        # This should be rare given the dry-run gate, but not impossible:
        # a wheel might disappear from the index between dry-run and real
        # install, or a permission problem on the target site-packages.
        result["status"] = STATUS_FAILED
        result["reason"] = (
            f"{pkg} upgrade failed after successful dry-run: "
            f"{_first_error_line(real_out)}"
        )
        return result

    after = _pip_show_version(runner, interpreter, pkg)
    result["after_version"] = after

    # -----------------------------------------------------------------
    # Rule 5 — Verify with pip check. Filter on SDK-related lines so
    # unrelated pre-existing shared-env inconsistencies do not create
    # false alarms.
    # -----------------------------------------------------------------
    check_rc, check_out = runner(
        [interpreter, "-m", "pip", "check"], timeout,
    )
    result["pip_check_returncode"] = check_rc
    result["pip_check_output_tail"] = _tail(check_out, 15)
    sdk_line = _sdk_line_in_pip_check(check_out, pkg) if check_rc != 0 else None
    if sdk_line:
        result["status"] = STATUS_BLOCKED_INCONSISTENT
        result["reason"] = (
            f"{pkg} moved {before} -> {after}, but its declared dependencies "
            f"are not satisfied in this environment: {sdk_line}. "
            f"Manual dependency update required."
        )
        return result

    if before and after and before != after:
        result["status"] = STATUS_UPGRADED
        result["reason"] = f"{pkg} upgraded {before} -> {after}."
    elif after:
        result["status"] = STATUS_NO_CHANGE
        result["reason"] = f"{pkg} already at {after}; nothing to do."
    else:
        # rc == 0 but pip show returns nothing — highly unusual, treat as
        # failed rather than silently claim success.
        result["status"] = STATUS_FAILED
        result["reason"] = (
            f"{pkg} install reported success but version could not be read."
        )
    return result
