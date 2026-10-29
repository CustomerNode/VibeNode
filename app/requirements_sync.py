"""Install what requirements.txt says, when it changes.

Why this exists (2026-10-23)
----------------------------
The Update button pulled code but never ran pip, and the launcher installed
only four hard-coded packages.  A commit that added a dependency (tzdata,
pytest-xdist, the SpeechNode pin) left every one-click user with code that
imports a package they do not have, until someone ran pip by hand.

Two callers share this module:

* ``app.git_ops.do_git_sync`` runs :func:`install_requirements` when the pull
  changed the ``requirements.txt`` blob.
* ``run.py`` runs :func:`install_if_changed` at launch: the file's sha256 is
  compared with ``data/requirements.stamp`` (gitignored), so a pull made by
  ``git`` outside the UI is caught on the next start.

Both use the interpreter that runs the server (``sys.executable``), never the
``python`` on PATH: on the ARM64 setup those are different environments.

A pip failure is reported, never raised: the code update must still land.
"""

from __future__ import annotations

import hashlib
import re
import subprocess
import sys
from pathlib import Path

from .platform_utils import NO_WINDOW as _NO_WINDOW

ROOT = Path(__file__).resolve().parents[1]
REQUIREMENTS = ROOT / "requirements.txt"
STAMP = ROOT / "data" / "requirements.stamp"
PIP_TIMEOUT = 900  # a cold native build (webrtcvad) can take minutes
# At launch the install runs inside run.py's "deps" boot step, whose watchdog
# budget is 600s; stay under it so a slow pip is reported, not declared a hang.
LAUNCH_PIP_TIMEOUT = 540

_SDK_LINE = re.compile(r"^\s*(claude-code-sdk)\s*([^#\s]*)", re.IGNORECASE)
_INSTALLED_LINE = re.compile(r"^Successfully installed\s+(.+)$", re.MULTILINE)


def requirements_sha256(path: Path = REQUIREMENTS) -> str:
    """sha256 of the file's bytes, or '' when it does not exist."""
    try:
        return hashlib.sha256(Path(path).read_bytes()).hexdigest()
    except OSError:
        return ""


def read_stamp(path: Path = STAMP) -> str:
    try:
        return Path(path).read_text(encoding="utf-8").strip()
    except OSError:
        return ""


def write_stamp(digest: str, path: Path = STAMP) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(digest + "\n", encoding="utf-8")


def _tail(text: str, lines: int = 25) -> str:
    rows = [r for r in (text or "").splitlines() if r.strip()]
    return "\n".join(rows[-lines:])


def installed_count(pip_output: str) -> int:
    """How many distributions pip's "Successfully installed" line names."""
    m = _INSTALLED_LINE.search(pip_output or "")
    return len(m.group(1).split()) if m else 0


def install_requirements(python: str | None = None, requirements: Path = REQUIREMENTS,
                         timeout: int = PIP_TIMEOUT, runner=None) -> dict:
    """``python -m pip install -r requirements.txt`` with the server's interpreter.

    Returns ``{"pip_ran": True, "pip_ok": bool, "pip_installed": int,
    "pip_output_tail": str}``.  Never raises.
    """
    python = python or sys.executable
    # --only-binary=:all: — never source-build (same rule as update_safety.py).
    # On Windows ARM64, supabase -> pyjwt[crypto] -> cryptography has no wheel,
    # so pip bootstrapped Rust (rustup, cargo, maturin) on every launch, flashed
    # a burst of terminal windows, failed, left the stamp unwritten, and retried
    # at the next launch.  Wheels-only turns that into a quick, silent resolver
    # failure; it is still reported and still retried, just never compiled.
    cmd = [python, "-m", "pip", "install", "--disable-pip-version-check",
           "--only-binary=:all:", "-r", str(requirements)]
    run = runner or subprocess.run
    out, ok = _run_pip(run, cmd, timeout)
    # PEP 668 (Ubuntu 23.04+, Debian 12+): a system interpreter refuses every
    # install, --user included, with "externally-managed-environment".  The
    # server then runs on that interpreter with its packages in the user site
    # (~/.local), put there by launch.sh's own fallback.  Install the same way:
    # into the user site, never into the system's site-packages.  Only for an
    # interpreter outside a virtual environment; a venv never says this.
    if not ok and _EXTERNALLY_MANAGED in out and not _in_venv(python):
        cmd = cmd[:4] + ["--user", "--break-system-packages"] + cmd[4:]
        out, ok = _run_pip(run, cmd, timeout)
    return {"pip_ran": True, "pip_ok": ok, "pip_installed": installed_count(out),
            "pip_output_tail": _tail(out), "pip_command": cmd}


_EXTERNALLY_MANAGED = "externally-managed-environment"


def _run_pip(run, cmd, timeout):
    try:
        r = run(cmd, capture_output=True, text=True, timeout=timeout,
                creationflags=_NO_WINDOW)
        return (r.stdout or "") + (r.stderr or ""), r.returncode == 0
    except subprocess.TimeoutExpired:
        return "pip install timed out after %ds" % timeout, False
    except Exception as exc:  # noqa: BLE001  (never let pip break the caller)
        return "pip could not run: %s" % exc, False


def _in_venv(python: str) -> bool:
    """True when ``python`` belongs to a virtual environment (has pyvenv.cfg)."""
    if python == sys.executable:
        return sys.prefix != sys.base_prefix
    exe = Path(python)
    return any((d / "pyvenv.cfg").is_file() for d in (exe.parent, exe.parent.parent))


def install_if_changed(requirements: Path = REQUIREMENTS, stamp: Path = STAMP,
                       python: str | None = None, runner=None,
                       timeout: int = LAUNCH_PIP_TIMEOUT) -> dict:
    """Launch-time check: run pip only when requirements.txt differs from the stamp.

    The stamp is written only after a SUCCESSFUL install, so a failed one is
    retried on the next launch.  Returns the install result with
    ``pip_ran=False`` when nothing changed.
    """
    digest = requirements_sha256(requirements)
    if not digest:
        return {"pip_ran": False, "pip_ok": True, "reason": "no requirements.txt"}
    if digest == read_stamp(stamp):
        return {"pip_ran": False, "pip_ok": True, "reason": "unchanged"}
    result = install_requirements(python, requirements, timeout=timeout, runner=runner)
    if result["pip_ok"]:
        try:
            write_stamp(digest, stamp)
        except OSError as exc:
            result["stamp_error"] = str(exc)
    return result


def sdk_requirement(requirements: Path = REQUIREMENTS, pkg: str = "claude-code-sdk") -> str:
    """The ``claude-code-sdk`` requirement line, e.g. ``claude-code-sdk>=0.0.25,<0.1``.

    Returns the bare package name when the file has no line for it, so the
    caller's pip call degrades to the old unbounded upgrade.
    """
    try:
        text = Path(requirements).read_text(encoding="utf-8")
    except OSError:
        return pkg
    for line in text.splitlines():
        m = _SDK_LINE.match(line)
        if m and m.group(1).lower() == pkg.lower():
            return pkg + m.group(2).strip()
    return pkg
