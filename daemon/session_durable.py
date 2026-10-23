"""SessionDurable: the session state a Session Engine restart used to lose.

Why this exists (2026-10-23)
----------------------------
The Session Engine keeps each session's working state in memory
(``SessionInfo``).  A restart (or a crash, or the ``kill -9`` the daemon-scope
restart actually is) dropped all of it: an armed usage-limit or API-error
auto-retry, the "Always allow" grants, the temp-id aliases, usage, the
wake-up gating.  ``--resume`` brought the conversation back but not any of
that.

This module keeps one small JSON record per session in
``data/session_durable/<sid>.json`` (gitignored) and puts the state back when
the session is recovered (``SessionManager.start_session``, ``resume=True``)
or re-attached to its Session Host (``daemon/session_host_registry.py``).

Rules
-----
* Absolute epochs only, never "seconds from now": a record read an hour later
  must still mean the same instant.
* Write-through immediately for what a restart must never lose (a pending
  permission, the allow-lists, aliases, retry/limit arming); a 1 s debounce
  for the noisy rest (usage, wake-up gating), via :func:`save` with
  ``immediate=False``.
* Atomic writes (temp file + ``os.replace``), as ``durable_jobs`` does.
* An explicit Sleep/Delete deletes the record (``SessionManager._close_session``
  calls :func:`delete`), so "Sleep must stick": a woken session never inherits
  state from before it was put to sleep.  A record that exists at resume time
  was therefore left by a restart or a crash.
* Kill switch: ``VIBENODE_SESSION_DURABLE=0`` (no reads, no writes).

The temp-id aliases are also indexed globally in ``data/session_aliases.json``
(owned by ``common/session_names.py``), which the engine rehydrates into
``SessionManager._id_aliases`` at start and the web reads through
``app/session_store._load_remaps_cached``.
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
import threading
import time
from pathlib import Path
from typing import Optional

from common.paths import DATA_DIR

logger = logging.getLogger(__name__)

VERSION = 1

# Module global so tests (tests/conftest.py) can point it at a sandbox.
DURABLE_DIR: Path = DATA_DIR / "session_durable"

# Seconds of quiet before a debounced (noisy-field) write lands.
DEBOUNCE_S = 1.0


def enabled() -> bool:
    """``VIBENODE_SESSION_DURABLE`` (default on).  Read at call time so a test
    can flip it per test."""
    return (os.environ.get("VIBENODE_SESSION_DURABLE", "1") or "1").strip().lower() \
        not in ("0", "false", "no", "off")


def record_path(session_id: str) -> Path:
    # A session id is a UUID or a client temp id; neither has a path separator,
    # but refuse one anyway so a hostile id can never escape the folder.
    safe = "".join(c for c in str(session_id) if c.isalnum() or c in "-_")
    return DURABLE_DIR / f"{safe}.json"


# ── Atomic IO ────────────────────────────────────────────────────────────

def _write_atomic(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".dur.", suffix=".tmp", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(data, fh, ensure_ascii=False, default=str)
        # Windows refuses to replace a file another thread or process has
        # open (a load, or a debounced write racing an immediate one): the
        # refusal is transient, so retry briefly instead of losing the write.
        for attempt in range(50):
            try:
                os.replace(tmp, path)
                break
            except PermissionError:
                if attempt == 49:
                    raise
                time.sleep(0.02)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def load(session_id: str) -> Optional[dict]:
    """The record for ``session_id``, or None (missing, unreadable, disabled)."""
    if not enabled() or not session_id:
        return None
    for attempt in range(50):
        try:
            with open(record_path(session_id), encoding="utf-8") as fh:
                data = json.load(fh)
            if isinstance(data, dict) and data.get("version") == VERSION:
                return data
            return None
        except FileNotFoundError:
            return None
        except PermissionError:
            # Being replaced right now (Windows): transient, retry.
            if attempt < 49:
                time.sleep(0.02)
                continue
            logger.warning("SessionDurable: record for %s stayed locked", session_id)
            return None
        except Exception as exc:
            logger.warning("SessionDurable: unreadable record for %s: %s", session_id, exc)
            return None
    return None


def delete(session_id: str) -> None:
    """Forget a session's record (explicit Sleep/Delete; or a finished attach)."""
    _cancel_timer(session_id)
    try:
        record_path(session_id).unlink()
    except FileNotFoundError:
        pass
    except Exception as exc:
        logger.debug("SessionDurable: delete %s failed: %s", session_id, exc)


def rename(old_id: str, new_id: str) -> None:
    """Move a record when the session adopts the CLI's id."""
    if not enabled() or not old_id or not new_id or old_id == new_id:
        return
    _cancel_timer(old_id)
    try:
        src = record_path(old_id)
        if src.exists():
            os.replace(src, record_path(new_id))
    except Exception as exc:
        logger.debug("SessionDurable: rename %s -> %s failed: %s", old_id, new_id, exc)


# ── Capture ──────────────────────────────────────────────────────────────

def _json_safe(value):
    try:
        return json.loads(json.dumps(value, default=str))
    except Exception:
        return None


def capture(info) -> dict:
    """Everything a restart must not lose, read off a ``SessionInfo``.

    Reads with ``getattr`` defaults throughout: a SessionInfo from an older
    engine (hot-reloaded watchdog code, tests with stand-ins) must not raise.
    """
    g = lambda name, default=None: getattr(info, name, default)  # noqa: E731
    state = g("state")
    state_value = getattr(state, "value", state) or ""
    pending = None
    if g("pending_tool_name") and g("pending_permission") is not None:
        pending = {
            "tool_name": g("pending_tool_name", ""),
            "tool_input": _json_safe(g("pending_tool_input", {})) or {},
            "since": float(g("_pending_since", 0.0) or time.time()),
        }
    usage = dict(g("usage", {}) or {})
    usage.pop("_per_call", None)
    return {
        "version": VERSION,
        "session_id": g("session_id", ""),
        "saved_at": time.time(),
        "state": state_value,
        "aliases": sorted(set(g("_durable_aliases", ()) or ())),
        "pending_permission": pending,
        "always_allowed_tools": sorted(g("always_allowed_tools", ()) or ()),
        "almost_always_allowed_tools": sorted(g("almost_always_allowed_tools", ()) or ()),
        "retry": {
            "at": float(g("retry_at", 0.0) or 0.0),
            "attempt": int(g("retry_attempt", 0) or 0),
            "max": int(g("retry_max", 0) or 0),
            "reason": g("retry_reason", "") or "",
            "count": int(g("_api_retry_count", 0) or 0),
            "needs_reconnect": bool(g("_retry_needs_reconnect", False)),
        },
        "limit": {
            "reset_at": float(g("limit_reset_at", 0.0) or 0.0),
            "limited_model": g("limited_model", "") or "",
            "switch_to": g("limit_switch_to", "") or "",
            "scope": g("_limit_scope", "") or "",
            "until": _json_safe(dict(g("_limit_until", {}) or {})) or {},
            "origin": g("_limit_origin", "") or "",
            "resume_at": float(g("_limit_resume_at", 0.0) or 0.0),
        },
        "usage": _json_safe(usage) or {},
        "cost_usd": float(g("cost_usd", 0.0) or 0.0),
        "wakeup": {
            "pending": bool(g("_wakeup_pending", False)),
            "deadline": float(g("_wakeup_deadline", 0.0) or 0.0),
            "is_scheduled": bool(g("_wakeup_is_scheduled", False)),
            "blocked_since": float(g("_wakeup_queue_blocked_since", 0.0) or 0.0),
        },
        "task_started_at": float(g("_task_started_at", 0.0) or 0.0),
        "bg_work_at": float(g("_bg_work_at", 0.0) or 0.0),
        "interrupted": bool(g("_interrupted", False)),
        "entries_trimmed": bool(g("_entries_trimmed", False)),
    }


# ── Save (write-through or debounced) ────────────────────────────────────

_timers: dict = {}           # session_id -> threading.Timer
_pending: dict = {}          # session_id -> latest captured record
_timers_lock = threading.Lock()


def _cancel_timer(session_id: str) -> None:
    with _timers_lock:
        t = _timers.pop(session_id, None)
        _pending.pop(session_id, None)
    if t is not None:
        t.cancel()


def _flush_one(session_id: str) -> None:
    with _timers_lock:
        _timers.pop(session_id, None)
        rec = _pending.pop(session_id, None)
    if rec is None:
        return
    try:
        _write_atomic(record_path(session_id), rec)
    except Exception as exc:
        logger.warning("SessionDurable: write for %s failed: %s", session_id, exc)


def save(info, immediate: bool = True) -> None:
    """Record ``info``'s durable state.  ``immediate`` writes now (and cancels a
    pending debounced write); otherwise the write lands ``DEBOUNCE_S`` after
    the first call of a burst, carrying the latest state.  Never raises."""
    if not enabled():
        return
    try:
        sid = getattr(info, "session_id", "") or ""
        if not sid:
            return
        rec = capture(info)
        if immediate:
            with _timers_lock:
                t = _timers.pop(sid, None)
                _pending.pop(sid, None)
            if t is not None:
                t.cancel()
            _write_atomic(record_path(sid), rec)
            return
        with _timers_lock:
            _pending[sid] = rec
            if sid in _timers:
                return
            t = threading.Timer(DEBOUNCE_S, _flush_one, args=(sid,))
            t.daemon = True
            _timers[sid] = t
        t.start()
    except Exception as exc:
        logger.warning("SessionDurable: save failed: %s", exc)


def cancel_all() -> None:
    """Drop every pending debounced write without writing it.

    For tests: a debounced timer that fires after a test's sandbox has been
    torn down would write into the real ``data/session_durable`` (seen
    2026-10-23: 261 test records there).  ``tests/conftest.py`` calls this at
    the end of every test.
    """
    with _timers_lock:
        timers = list(_timers.values())
        _timers.clear()
        _pending.clear()
    for t in timers:
        t.cancel()


def flush_all() -> None:
    """Write every debounced record now (graceful engine stop)."""
    with _timers_lock:
        sids = list(_timers)
    for sid in sids:
        with _timers_lock:
            t = _timers.get(sid)
        if t is not None:
            t.cancel()
        _flush_one(sid)


def armed_session_ids() -> set:
    """Sessions whose record carries an armed retry or limit-resume timer.

    ``recover_sessions`` relaunches these even when they were idle at the
    restart, so a usage-limit continue or an API-error auto-retry fires.
    """
    out = set()
    if not enabled():
        return out
    try:
        for p in DURABLE_DIR.glob("*.json"):
            try:
                with open(p, encoding="utf-8") as fh:
                    rec = json.load(fh)
            except Exception:
                continue
            if not isinstance(rec, dict):
                continue
            retry = rec.get("retry") or {}
            limit = rec.get("limit") or {}
            if float(retry.get("at") or 0) > 0 or float(limit.get("resume_at") or 0) > 0:
                sid = rec.get("session_id") or p.stem
                out.add(sid)
    except Exception as exc:
        logger.debug("SessionDurable: armed scan failed: %s", exc)
    return out


# ── Apply ────────────────────────────────────────────────────────────────

def apply_static(info, rec: dict, *, attached: bool) -> dict:
    """Put a record's plain fields back on a fresh ``SessionInfo``.

    Returns what the caller still has to act on:
    ``{"retry_at": epoch|0, "lost_permission": tool_name|""}``.

    ``attached`` is True when the session's CLI survived the restart inside
    its Session Host: then the CLI-side wake-up timer and the interrupted flag
    are still real.  On a ``--resume`` the CLI is new, so they are dropped.
    """
    out = {"retry_at": 0.0, "lost_permission": ""}
    if not isinstance(rec, dict):
        return out
    try:
        info.always_allowed_tools.update(rec.get("always_allowed_tools") or [])
        info.almost_always_allowed_tools.update(rec.get("almost_always_allowed_tools") or [])
        usage = rec.get("usage") or {}
        if usage and not info.usage:
            info.usage = dict(usage)
        if rec.get("cost_usd") and not info.cost_usd:
            info.cost_usd = float(rec["cost_usd"])
        info._task_started_at = float(rec.get("task_started_at") or 0.0)
        info._bg_work_at = float(rec.get("bg_work_at") or 0.0)
        info._durable_aliases = set(rec.get("aliases") or [])
        lim = rec.get("limit") or {}
        info.limit_reset_at = float(lim.get("reset_at") or 0.0)
        info.limited_model = lim.get("limited_model") or ""
        info.limit_switch_to = lim.get("switch_to") or ""
        info._limit_scope = lim.get("scope") or ""
        info._limit_until = dict(lim.get("until") or {})
        info._limit_origin = lim.get("origin") or ""
        retry = rec.get("retry") or {}
        info._api_retry_count = int(retry.get("count") or 0)
        at = float(retry.get("at") or 0.0)
        if at <= 0:
            at = float(lim.get("resume_at") or 0.0)
        if at > 0:
            # Kept on the info so the countdown shows (and the queue gate
            # holds) from the first state push; _arm_api_retry_at starts the
            # timer once the session is IDLE.
            info.retry_at = at
            info.retry_attempt = int(retry.get("attempt") or 0)
            info.retry_max = int(retry.get("max") or 0)
            info.retry_reason = retry.get("reason") or ""
            info._retry_needs_reconnect = bool(retry.get("needs_reconnect")) and attached
            out["retry_at"] = at
        if attached:
            wake = rec.get("wakeup") or {}
            deadline = float(wake.get("deadline") or 0.0)
            if wake.get("pending") and (deadline == 0.0 or deadline > time.time()):
                info._wakeup_pending = True
                info._wakeup_deadline = deadline
                info._wakeup_is_scheduled = bool(wake.get("is_scheduled"))
            info._interrupted = bool(rec.get("interrupted"))
        perm = rec.get("pending_permission")
        if isinstance(perm, dict) and perm.get("tool_name"):
            out["lost_permission"] = perm["tool_name"]
    except Exception as exc:
        logger.warning("SessionDurable: apply for %s failed: %s",
                       getattr(info, "session_id", "?"), exc)
    return out
