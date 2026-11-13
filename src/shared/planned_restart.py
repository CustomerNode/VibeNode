"""A note that a restart was asked for, read by the Start page (reviver).

The reviver serves the outage page ("Stopped", with a Start button) whenever
nothing answers on the web port. A restart VibeNode asked for itself also
leaves the port empty for a few seconds, and an automatic Session Engine
restart that took the web server down left that outage page up with nothing
to bring VibeNode back. So ``/api/restart`` writes this note first; while it
is fresh the reviver shows a "Restarting" page that returns by itself, and if
the web server has not come back within ``COMEBACK_SECONDS`` it starts
VibeNode the way the Start button does. A real outage (no note) still gets
the outage page.

Stdlib only: the reviver runs when VibeNode may be broken.
"""
from __future__ import annotations

import json
import os
import time
from pathlib import Path

# A note older than this is history, not a restart in progress.
FRESH_SECONDS = 120
# How long a planned restart may leave the web port empty before the reviver
# starts VibeNode itself. A normal web restart is back in under 10 seconds.
COMEBACK_SECONDS = 25


def note_path() -> Path:
    env = os.environ.get("VIBENODE_PLANNED_RESTART_FILE")
    if env:
        return Path(env)
    return Path(__file__).resolve().parents[2] / "data" / "planned_restart.json"


def write(scope: str, reason: str = "") -> None:
    """Record that a restart of ``scope`` starts now. Never raises."""
    try:
        p = note_path()
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_suffix(".tmp")
        tmp.write_text(json.dumps({"scope": scope, "reason": reason,
                                   "at": time.time()}), encoding="utf-8")
        os.replace(tmp, p)
    except Exception:
        pass


def fresh(now: float | None = None) -> dict | None:
    """The note when a restart was asked for in the last FRESH_SECONDS."""
    try:
        d = json.loads(note_path().read_text(encoding="utf-8"))
        at = float(d.get("at", 0))
    except Exception:
        return None
    now = time.time() if now is None else now
    if 0 <= now - at <= FRESH_SECONDS:
        return d
    return None
