"""Account usage limits (session / weekly / Fable) as reported by the Claude CLI.

Where the numbers come from
---------------------------
Every CLI turn emits a ``rate_limit_event`` on its stream-json output whose
``rate_limit_info.unifiedWindows`` carries the account's live utilization:

    {"five_hour":  {"utilization": 0.16, "resetsAt": 1790871000},
     "seven_day":  {"utilization": 0.46, "resetsAt": 1790964000},
     "seven_day_overage_included": {"utilization": 0.14, "resetsAt": ...}}

The CLI's own label table names these "session limit", "weekly limit" and
"Fable limit" (verified in CLI 2.1.283).  The Fable window is only present on
turns that run on a Fable model, so the last seen value is kept per window.

The claude_code_sdk does not know this message type; sdk_patches'
safe-parse wrapper hands it to a sink (SessionManager._on_rate_limit_info)
before dropping it.  Nothing here reads credentials or calls Anthropic: the
data is only what the CLI already printed.

The merged state is persisted so the web server can serve it on page load
without a daemon round-trip, and so it survives a daemon restart.
"""

from __future__ import annotations

import json
import os
import tempfile
import time
from pathlib import Path
from typing import Optional

USAGE_LIMITS_PATH = Path.home() / ".claude" / "vibenode_usage_limits.json"

# Window key -> short display label.  Order is display order.
WINDOWS = {
    "five_hour": "Session",
    "seven_day": "Week",
    "seven_day_overage_included": "Fable",
}


def merge(state: dict, rate_limit_info, now: Optional[float] = None) -> tuple:
    """Merge one rate_limit_info payload into ``state``.

    Returns ``(new_state, changed)``.  ``changed`` is True only when a
    utilization or reset time actually moved, so callers broadcast and persist
    on real changes, not on every turn.  Unknown windows and malformed values
    are ignored; a window absent from this payload keeps its last value.
    """
    now = time.time() if now is None else now
    windows = state.get("windows", {}) if isinstance(state, dict) else {}
    windows = {k: dict(v) for k, v in windows.items() if k in WINDOWS and isinstance(v, dict)}
    incoming = {}
    if isinstance(rate_limit_info, dict):
        incoming = rate_limit_info.get("unifiedWindows") or {}
    changed = False
    seen_any = False
    for key in WINDOWS:
        w = incoming.get(key) if isinstance(incoming, dict) else None
        if not isinstance(w, dict):
            continue
        util = w.get("utilization")
        if isinstance(util, bool) or not isinstance(util, (int, float)):
            continue
        try:
            resets = int(w.get("resetsAt") or 0)
        except (TypeError, ValueError):
            resets = 0
        util = max(0.0, float(util))
        old = windows.get(key)
        if not old or old.get("utilization") != util or old.get("resets_at") != resets:
            changed = True
        windows[key] = {"utilization": util, "resets_at": resets, "seen_at": now}
        seen_any = True
    new_state = {"windows": windows,
                 "updated_at": now if seen_any else (state or {}).get("updated_at", 0)}
    return new_state, changed


def load(path: Path = USAGE_LIMITS_PATH) -> dict:
    """Read the persisted state; ``{}`` if missing or unreadable."""
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def save(state: dict, path: Path = USAGE_LIMITS_PATH) -> None:
    """Atomically write the state (temp file + replace).  Never raises."""
    try:
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=str(path.parent), suffix=".tmp")
        try:
            os.write(fd, json.dumps(state).encode("utf-8"))
            os.close(fd)
            os.replace(tmp, str(path))
        except Exception:
            try:
                os.close(fd)
            except OSError:
                pass
            try:
                os.unlink(tmp)
            except OSError:
                pass
    except Exception:
        pass


def public_view(state: dict, now: Optional[float] = None) -> dict:
    """The payload the UI renders: per window, percent used (0-100, rounded)
    and whether it is stale (its reset time has passed, so the stored value
    describes a window that no longer exists)."""
    now = time.time() if now is None else now
    out = {}
    windows = (state or {}).get("windows", {}) or {}
    for key, label in WINDOWS.items():
        w = windows.get(key)
        if not isinstance(w, dict):
            continue
        resets = int(w.get("resets_at") or 0)
        out[key] = {
            "label": label,
            "percent": int(round(float(w.get("utilization", 0)) * 100)),
            "resets_at": resets,
            "seen_at": w.get("seen_at", 0),
            "expired": bool(resets and resets <= now),
        }
    return {"windows": out, "updated_at": (state or {}).get("updated_at", 0)}
