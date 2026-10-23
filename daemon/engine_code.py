"""What code the running Session Engine loaded, so staleness can be seen.

Why this exists (2026-10-20)
----------------------------
The Session Engine (the daemon) is long-lived on purpose: restarting it ends
every running session.  Python never re-reads a module that is already
imported, so every fix to ``daemon/`` written after the engine started does
NOT run until the engine restarts.  On 2026-10-20 the engine had been up since
Oct 6, 13:03.  Every recovery job (stranded workers, silent waits, usage-limit
resume, durable jobs) was committed after that.  None of them ever ran, and a
session sat stranded for 83 minutes in exactly the state job 4 was written
for, while the user was told the problem had been fixed.

This module writes ``data/engine_code.json``: the engine's pid, its start
time, and the mtime of every repo source file it has loaded.  The web server
(``app/engine_freshness.py``) compares that record with the files on disk and
shows the user a banner when the engine runs outdated code.  The watchdog's
hot reload (``daemon/health_monitor.py``) updates the record for the files it
swaps in, so code that is live again stops counting as outdated.

The record is written atomically and never read back by the engine.  It is a
runtime artifact (gitignored).  Failures are logged and swallowed: it must
never break engine startup.
"""

import json
import logging
import os
import sys
import tempfile
import time
from pathlib import Path

logger = logging.getLogger(__name__)

ROOT = Path(__file__).resolve().parents[1]


def record_path() -> Path:
    """Where the record lives (``data/engine_code.json``, gitignored)."""
    return ROOT / "data" / "engine_code.json"


def _rel(path) -> str:
    """Repo-relative POSIX path, or '' if ``path`` is outside the repo."""
    try:
        rel = Path(path).resolve().relative_to(ROOT)
    except (ValueError, OSError):
        return ""
    parts = rel.parts
    # Virtualenvs inside the repo hold third-party code, not engine code.
    if not parts or parts[0].startswith(".venv") or "site-packages" in parts:
        return ""
    return rel.as_posix()


def loaded_source_files() -> dict:
    """``{repo-relative path: mtime_ns}`` of every repo module imported here."""
    out = {}
    for mod in list(sys.modules.values()):
        f = getattr(mod, "__file__", None)
        if not f or not f.endswith(".py"):
            continue
        rel = _rel(f)
        if not rel:
            continue
        try:
            out[rel] = os.stat(ROOT / rel).st_mtime_ns
        except OSError:
            continue
    return out


def _write(record: dict) -> None:
    path = record_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".engine_code.", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(record, fh, indent=1, sort_keys=True)
        os.replace(tmp, path)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _in_tests() -> bool:
    """A test run builds SessionManagers too; it must not overwrite the real
    engine's record.  (An environment check cannot tell them apart: agent
    shells inherit the daemon's environment.)"""
    return "pytest" in sys.modules


def sdk_fields() -> dict:
    """The SDK version and the patches that did not apply (daemon/sdk_patches.py).

    Read from the module's report when the SDK is importable; otherwise the
    SDK itself is missing, which is recorded as its own failure.  The web
    server passes these through ``/api/engine/status`` so the browser can show
    "SDK patches did not apply" (2026-10-23).
    """
    try:
        from daemon import sdk_patches
        report = sdk_patches.patch_report()
    except Exception as exc:  # SDK not installed, or import broke
        return {"sdk_version": "", "sdk_version_tested": False,
                "sdk_patch_failures": ["sdk_import"],
                "sdk_patch_failure_details": [{"name": "sdk_import",
                                               "precondition": str(exc)}]}
    return {k: report.get(k) for k in
            ("sdk_version", "sdk_version_tested", "sdk_patch_failures",
             "sdk_patch_failure_details")}


def record_loaded_code() -> None:
    """Write the full record.  Called once the engine has finished starting."""
    if _in_tests():
        return
    try:
        record = {
            "pid": os.getpid(),
            "recorded_at": time.time(),
            "loaded": loaded_source_files(),
        }
        record.update(sdk_fields())
        _write(record)
    except Exception:
        logger.exception("Could not write the engine code record")


def note_reloaded(rel_paths) -> None:
    """Mark ``rel_paths`` as live at their current mtimes (after a hot reload).

    Also adds any repo module imported since the record was written.
    """
    if _in_tests():
        return
    try:
        path = record_path()
        try:
            with open(path, encoding="utf-8") as fh:
                record = json.load(fh)
        except (OSError, ValueError):
            record = {}
        if record.get("pid") != os.getpid():
            record = {"pid": os.getpid(), "recorded_at": time.time(), "loaded": {}}
            record.update(sdk_fields())
        loaded = record.setdefault("loaded", {})
        for rel, mtime in loaded_source_files().items():
            loaded.setdefault(rel, mtime)
        for rel in rel_paths:
            try:
                loaded[rel] = os.stat(ROOT / rel).st_mtime_ns
            except OSError:
                pass
        _write(record)
    except Exception:
        logger.exception("Could not update the engine code record")
