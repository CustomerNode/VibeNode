"""Fetch the Fable usage limit when VibeNode has no reading of its own.

Why this exists
---------------
The CLI only reports the weekly Fable window (``seven_day_overage_included``)
on a reply from a Fable model (see daemon/usage_limits.py).  Someone working in
Opus all day therefore had NO Fable number in the bar, even with the limit at
93%: the gauge was simply absent until a Fable reply happened to go through.

The fix is the cheapest thing that makes Anthropic report it: one hidden,
one-word Fable turn through the daemon.  It reuses the "title" utility-session
plumbing (hidden from every list, JSONL kept out of the user's project) and
needs nothing new from the daemon: the daemon's existing rate_limit sink
records the window and broadcasts ``usage_limits`` to every browser.  Nothing
here reads credentials or calls Anthropic directly.

When it runs
------------
Only when a probe can actually add information:

* the Fable window is missing, or its reset time has passed; and
* the account reports unified windows at all (``five_hour`` is known).  An
  API-key account never has these, and a probe would cost it real money for
  nothing, so it is never probed automatically.

It is throttled (one attempt per ``RETRY_S``), and after a probe that still
produced no Fable window (an account without Fable access) it backs off for
``NO_FABLE_BACKOFF_S``.  ``kanban_config.json["usage_fable_probe"] = false``
turns the automatic probe off.  A user-requested refresh (click on the gauges)
skips the "is it needed" test but is still throttled.

Cost: one turn with a one-line system prompt and a one-word reply.
"""

from __future__ import annotations

import logging
import threading
import time
import uuid

log = logging.getLogger(__name__)

FABLE_WINDOW = "seven_day_overage_included"
RETRY_S = 30 * 60            # automatic attempts: at most one per half hour
MANUAL_RETRY_S = 60          # click-to-refresh: at most one per minute
NO_FABLE_BACKOFF_S = 24 * 3600
PROBE_TIMEOUT_S = 45

_lock = threading.Lock()
_last_attempt = 0.0
_backoff_until = 0.0
_running = False


def needs_probe(state: dict, now: float | None = None) -> bool:
    """True when a probe would add a Fable reading we do not have.

    ``state`` is the persisted usage state (daemon/usage_limits.load()).
    """
    now = time.time() if now is None else now
    windows = (state or {}).get("windows") or {}
    if not isinstance(windows.get("five_hour"), dict):
        return False                      # not a subscription account (or no turn yet)
    fable = windows.get(FABLE_WINDOW)
    if not isinstance(fable, dict):
        return True                       # never reported
    try:
        resets = int(fable.get("resets_at") or 0)
    except (TypeError, ValueError):
        resets = 0
    return bool(resets and resets <= now)  # the window we know about is over


def _enabled() -> bool:
    try:
        from .config import get_kanban_config
        return bool(get_kanban_config().get("usage_fable_probe", True))
    except Exception:
        return True


def _fable_model() -> str:
    try:
        from .routes.live_api import _FALLBACK_KNOWN_MODELS
        for m in _FALLBACK_KNOWN_MODELS:
            if "fable" in m.get("id", ""):
                return m["id"]
    except Exception:
        pass
    return "claude-fable-5-1"


def maybe_start(sm, state: dict, manual: bool = False, now: float | None = None) -> bool:
    """Start a background probe if one is warranted.  Returns True if started.

    Cheap and non-blocking: safe to call from a request handler.
    """
    global _last_attempt, _running
    now = time.time() if now is None else now
    if not manual and (not _enabled() or not needs_probe(state, now)):
        return False
    if sm is None or not getattr(sm, "is_connected", False):
        return False
    with _lock:
        if _running:
            return False
        if not manual and now < _backoff_until:
            return False
        if now - _last_attempt < (MANUAL_RETRY_S if manual else RETRY_S):
            return False
        _last_attempt = now
        _running = True
    threading.Thread(target=_run, args=(sm,), name="usage-fable-probe", daemon=True).start()
    return True


def _run(sm) -> None:
    global _running, _backoff_until
    sid = f"_usage_{uuid.uuid4().hex[:8]}"
    project = ""
    try:
        from pathlib import Path
        from .config import _SYSTEM_UTILITY_CWD, _encode_cwd
        from daemon import usage_limits as _ul
        Path(_SYSTEM_UTILITY_CWD).mkdir(parents=True, exist_ok=True)
        project = _encode_cwd(_SYSTEM_UTILITY_CWD)
        before = ((_ul.load().get("windows") or {}).get(FABLE_WINDOW) or {}).get("seen_at", 0)
        result = sm.start_session(
            session_id=sid,
            prompt="ok",
            cwd=_SYSTEM_UTILITY_CWD,
            system_prompt="Reply with the single word: ok. Do not use any tools.",
            max_turns=1,
            model=_fable_model(),
            allowed_tools=[],
            permission_mode="plan",
            session_type="title",          # hidden utility session
            extra_args={"effort": "low"},
        )
        if not result or not result.get("ok"):
            log.info("usage probe: start_session refused: %s", result)
            return
        deadline = time.time() + PROBE_TIMEOUT_S
        got = False
        while time.time() < deadline:
            time.sleep(0.5)
            seen = ((_ul.load().get("windows") or {}).get(FABLE_WINDOW) or {}).get("seen_at", 0)
            if seen and seen != before:
                got = True
                break
            st = sm.get_session_state(sid)
            st = st.get("state") if isinstance(st, dict) else st
            if st in ("idle", "stopped"):
                # Turn finished; the sink fires before the result, so one last look.
                seen = ((_ul.load().get("windows") or {}).get(FABLE_WINDOW) or {}).get("seen_at", 0)
                got = bool(seen and seen != before)
                break
        if not got:
            with _lock:
                _backoff_until = time.time() + NO_FABLE_BACKOFF_S
            log.info("usage probe: no Fable window reported; backing off")
    except Exception:
        log.debug("usage probe failed", exc_info=True)
    finally:
        try:
            from .titling import _dispose_title_session, _cleanup_title_jsonl
            _dispose_title_session(sm, sid)
            _cleanup_title_jsonl(sid, sm, project)
        except Exception:
            log.debug("usage probe cleanup failed", exc_info=True)
        with _lock:
            _running = False
