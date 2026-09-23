"""
Claude CLI update helper — on-demand version check + `claude update` runner.

The daily background updater in ``run.py`` covers the "quiet, unattended"
path. This module covers the "user-visible, on-demand" paths:

  * The startup banner reads :func:`status_snapshot` to decide whether to
    surface "your CLI is stale" copy.
  * The model-switch error handler in ``static/js/invoke-workforce.js``
    hits :func:`run_update` when the CLI reports
    ``claude_code_version_too_old`` — the exact failure mode where a
    freshly-shipped model refuses because the local CLI predates it.

Everything here is best-effort and side-effect-free unless the caller
explicitly invokes :func:`run_update`. Reading :func:`current_version`
and :func:`status_snapshot` NEVER blocks on the network.

Design notes
------------
* We reuse the state file produced by ``run.py`` (``_UPDATE_STATE_FILE``)
  so the daily worker and the on-demand path stay in sync. Path is
  computed the same way to keep them identical without a cross-import.
* ``_run_captured`` mirrors run.py's implementation: a temp file, no
  pipes, no Windows grandchild deadlock. Copied rather than imported
  because ``run.py`` is the entry-point and importing it from the app
  package would re-enter the boot sequence.
* :func:`run_update` refuses when the daemon reports live sessions
  unless the caller passes ``force=True``. Swapping the CLI binary
  under a running session corrupts it — see run.py's daemon-restart
  note. The one exception is a caller that has explicitly acknowledged
  "close sessions and update".
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

# Path matches run.py._UPDATE_STATE_FILE exactly. Kept in sync manually —
# if you move one, move the other. Test asserts they still agree.
_STATE_FILE = (
    Path(__file__).resolve().parent.parent / ".cache" / "claude_update_state.json"
)

# Serialise on-demand updates within a single process. A second concurrent
# caller gets a fast "already running" result instead of a duplicate
# `claude update` invocation.
_update_lock = threading.Lock()

# Sensible defaults. Kept small so a stuck update never wedges a request
# thread longer than the SocketIO worker's expectations.
_VERSION_TIMEOUT = 15
_UPDATE_TIMEOUT = 180


def _run_captured(cmd: list[str], timeout: int) -> tuple[int | None, str]:
    """Run *cmd* with output captured to a temp file (no pipes).

    Mirrors run.py's helper. Returns ``(returncode, output)``. ``returncode``
    is ``None`` if the timeout fired. See run.py for the Windows-grandchild
    deadlock this avoids.
    """
    no_window = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0
    out_path = None
    try:
        fd, out_path = tempfile.mkstemp(prefix="vibenode_update_")
        with os.fdopen(fd, "r+", encoding="utf-8", errors="replace") as fh:
            proc = subprocess.Popen(
                cmd, stdout=fh, stderr=subprocess.STDOUT,
                stdin=subprocess.DEVNULL, creationflags=no_window,
            )
            rc: int | None = None
            try:
                rc = proc.wait(timeout=timeout)
            except subprocess.TimeoutExpired:
                proc.kill()
                try:
                    proc.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    pass
            fh.seek(0)
            out = fh.read()
        return rc, out
    finally:
        if out_path:
            try:
                os.remove(out_path)
            except OSError:
                pass


def _claude_path() -> str | None:
    """Absolute path to the ``claude`` CLI, or None if unresolvable."""
    return shutil.which("claude")


def current_version(path: str | None = None) -> str:
    """Return ``claude --version`` output (stripped), or '' on any failure."""
    p = path or _claude_path()
    if not p:
        return ""
    try:
        rc, out = _run_captured([p, "--version"], timeout=_VERSION_TIMEOUT)
        return out.strip() if rc == 0 else ""
    except Exception as e:  # pragma: no cover — defensive
        log.debug("claude --version failed: %s", e)
        return ""


def read_state() -> dict[str, Any]:
    """Return the persisted state dict (written by run.py's daily worker).

    Empty dict if the file is missing or malformed — callers must not
    assume any key exists.
    """
    try:
        if _STATE_FILE.exists():
            return json.loads(_STATE_FILE.read_text())
    except Exception as e:
        log.debug("read_state failed: %s", e)
    return {}


def _write_state(state: dict[str, Any]) -> None:
    """Persist state, tolerating a locked/read-only path."""
    try:
        _STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
        _STATE_FILE.write_text(json.dumps(state, indent=2))
    except Exception as e:  # pragma: no cover — defensive
        log.debug("_write_state failed: %s", e)


def count_active_sessions(session_manager) -> int:
    """How many daemon sessions are in a non-STOPPED state.

    Used as a safety guard before running ``claude update`` — updating
    while a session's CLI process is live corrupts it.
    """
    if session_manager is None:
        return 0
    try:
        states = session_manager.get_all_states() or []
    except Exception as e:
        log.debug("count_active_sessions: get_all_states failed: %s", e)
        return 0
    active = 0
    for s in states:
        if not isinstance(s, dict):
            continue
        st = (s.get("state") or "").lower()
        # Anything not fully stopped counts — starting/working/idle/waiting
        # all imply a live child CLI whose binary we must not swap.
        if st and st != "stopped":
            active += 1
    return active


# Public sentinel: the ISO-format-ish "how stale is too stale?" horizon
# for the banner. 30 days matches Anthropic's typical cadence of
# CLI bumps that gate new models.
STALE_AFTER_SECONDS = 30 * 24 * 3600


def status_snapshot(session_manager=None) -> dict[str, Any]:
    """A single dict summarising CLI state, for /api/admin/claude-status.

    Shape (all keys always present):
      {
        "installed": bool,          # `claude` on PATH
        "current_version": "...",   # `claude --version` output, or ''
        "last_check": 0 | ts,       # unix epoch of last daily check
        "last_check_age_seconds": int | None,
        "last_recorded_version": "...",  # what the daily worker saw last
        "updated_last_check": bool, # did the daily worker actually bump?
        "restart_pending": bool,    # a bump happened but the daemon is stale
        "stale": bool,              # last_check older than STALE_AFTER_SECONDS
        "active_sessions": int,     # non-stopped daemon sessions (see caveat)
      }
    """
    st = read_state()
    now = time.time()
    last_check = float(st.get("last_check") or 0)
    age = int(now - last_check) if last_check else None
    return {
        "installed": bool(_claude_path()),
        "current_version": current_version(),
        "last_check": last_check,
        "last_check_age_seconds": age,
        "last_recorded_version": st.get("cli_version") or "",
        "updated_last_check": bool(st.get("updated_last_check")),
        "restart_pending": bool(st.get("daemon_restart_pending")),
        "stale": (age is None) or (age >= STALE_AFTER_SECONDS),
        "active_sessions": count_active_sessions(session_manager),
    }


def run_update(session_manager=None, force: bool = False) -> dict[str, Any]:
    """Run ``claude update`` synchronously and report the outcome.

    Behavior:
      * Refuses (returns ``blocked: "sessions_running"``) when the daemon
        has non-stopped sessions and *force* is False. Swapping the binary
        under a live CLI corrupts it — the caller must confirm.
      * Falls back to ``npm update -g @anthropic-ai/claude-code`` when
        the CLI's self-updater exits with an npm hint (npm-managed
        installs refuse to self-update).
      * Times out at ``_UPDATE_TIMEOUT``. A stalled update reports
        ``ok: False, error: "timeout"`` — callers should surface a retry.
      * Serialised process-wide: a concurrent call returns
        ``blocked: "in_progress"`` instantly.

    Return shape (all keys always present):
      {
        "ok": bool,
        "before": "...",           # `claude --version` before
        "after": "...",            # `claude --version` after
        "updated": bool,           # before != after AND both non-empty
        "restart_required": bool,  # updated AND daemon has live sessions
        "blocked": None | "sessions_running" | "in_progress" | "not_installed",
        "error": None | str,       # populated on failure
        "output": "...",           # tail of `claude update` stdout+stderr
      }
    """
    if not _update_lock.acquire(blocking=False):
        return {
            "ok": False, "before": "", "after": "", "updated": False,
            "restart_required": False, "blocked": "in_progress",
            "error": "Another update is already running.", "output": "",
        }
    try:
        path = _claude_path()
        if not path:
            return {
                "ok": False, "before": "", "after": "", "updated": False,
                "restart_required": False, "blocked": "not_installed",
                "error": "The `claude` CLI is not on PATH.", "output": "",
            }

        active = count_active_sessions(session_manager)
        if active and not force:
            return {
                "ok": False, "before": current_version(path), "after": "",
                "updated": False, "restart_required": False,
                "blocked": "sessions_running",
                "error": (
                    f"{active} session(s) are still running. Close them or "
                    "pass force=true to update anyway (will interrupt them)."
                ),
                "output": "",
            }

        before = current_version(path)
        rc, out = _run_captured([path, "update"], timeout=_UPDATE_TIMEOUT)
        if rc is None:
            return {
                "ok": False, "before": before, "after": before,
                "updated": False, "restart_required": False, "blocked": None,
                "error": "timeout", "output": out[-4000:],
            }
        # npm-managed install: self-updater refuses with an npm hint. Retry
        # with `npm update -g`. Guarded on npm being available so a failure
        # here reports the underlying reason instead of a NoneType crash.
        if rc != 0 and "npm" in out.lower():
            npm = shutil.which("npm")
            if npm:
                nrc, nout = _run_captured(
                    [npm, "update", "-g", "@anthropic-ai/claude-code"],
                    timeout=_UPDATE_TIMEOUT,
                )
                out = out + "\n---npm fallback---\n" + nout
                rc = nrc if nrc is not None else rc

        after = current_version(path)
        updated = bool(before and after and before != after)

        # Best-effort: refresh the state file so the banner clears without
        # waiting for the daily worker to re-run.
        st = read_state()
        st.update({
            "last_check": time.time(),
            "cli_version": after or before,
            "updated_last_check": updated,
            "daemon_restart_pending": (
                updated and count_active_sessions(session_manager) > 0
            ),
        })
        _write_state(st)

        ok = rc == 0 or updated
        return {
            "ok": ok,
            "before": before,
            "after": after,
            "updated": updated,
            "restart_required": updated and count_active_sessions(session_manager) > 0,
            "blocked": None,
            "error": None if ok else f"`claude update` exited with rc={rc}",
            "output": out[-4000:],
        }
    finally:
        _update_lock.release()
