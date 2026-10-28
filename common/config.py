"""The kanban/app settings file (``kanban_config.json``), read by both layers.

``app/config.py`` re-exports everything here and forwards reads AND writes of
the module-level state (``_KANBAN_CONFIG_FILE`` and the cache fields), so
``monkeypatch.setattr(app.config, "_KANBAN_CONFIG_FILE", ...)`` and
``app.config._kanban_config_cache = None`` still act on this one cache.
"""

import json
import os
import time
from pathlib import Path

from common.paths import REPO_ROOT

# Allow tests to override config path via env var
_KANBAN_CONFIG_FILE = (Path(os.environ["VIBENODE_CONFIG"]) if os.environ.get("VIBENODE_CONFIG")
                       else REPO_ROOT / "kanban_config.json")


# PERF-CRITICAL: Kanban config cache with 10s TTL + invalidation on save — do NOT remove. See CLAUDE.md #11.
#
# LESSON LEARNED (2026-04-12): get_kanban_config() is called on every session
# start (to check cross_session_awareness preference) and from many API routes.
# It reads kanban_config.json from disk via read_text() + json.loads() every
# time.  The cache eliminates redundant disk reads.  save_kanban_config() sets
# _kanban_config_cache = None so writes are immediately visible.  Returns
# dict(cached) shallow copies because callers in kanban_api.py mutate the
# returned dict (e.g., cfg["kanban_backend"] = ...) — without the copy, they'd
# corrupt the cache.
#
# The cache is per process: the web server and the Session Engine each hold
# one.  (Moved here from app/config.py on 2026-10-23 so the engine no longer
# imports the web package; app/config.py re-exports it.)
# ---------------------------------------------------------------------------

_kanban_config_cache: dict | None = None
_kanban_config_cache_time: float = 0.0
_KANBAN_CONFIG_CACHE_TTL = 10.0  # seconds


def get_kanban_config() -> dict:
    """Load kanban configuration from kanban_config.json.

    Missing keys are filled from defaults so callers always see the full set.
    Results are cached with a 10-second TTL.  The cache is invalidated
    immediately on save so writes are never stale.
    """
    global _kanban_config_cache, _kanban_config_cache_time
    now = time.monotonic()
    if _kanban_config_cache is not None and (now - _kanban_config_cache_time) < _KANBAN_CONFIG_CACHE_TTL:
        return dict(_kanban_config_cache)  # shallow copy — callers may mutate
    try:
        stored = json.loads(_KANBAN_CONFIG_FILE.read_text(encoding="utf-8"))
        merged = _kanban_config_defaults()
        merged.update(stored)
        # Migrate old key
        if "kanban_auto_advance" in stored and "auto_advance_to_validating" not in stored:
            merged["auto_advance_to_validating"] = bool(stored["kanban_auto_advance"])
        _kanban_config_cache = merged
        _kanban_config_cache_time = time.monotonic()
        return dict(merged)
    except Exception:
        result = _kanban_config_defaults()
        _kanban_config_cache = result
        _kanban_config_cache_time = time.monotonic()
        return dict(result)


def _kanban_config_defaults() -> dict:
    return {
        "kanban_backend": "sqlite",
        "supabase_url": "",
        "supabase_secret_key": "",
        "supabase_publishable_key": "",
        "kanban_depth_limit": 5,
        # ── Browser launch preferences ──
        # How the VibeNode shortcut opens the app in Chrome:
        #   "tab" (default) — a normal tab in the user's everyday Chrome profile,
        #                     so VibeNode lives alongside their other tabs. This
        #                     is how VibeNode behaved for most of its history.
        #   "app"           — isolated Chrome app window on its own profile
        #                     (--app= + --user-data-dir=). See CLAUDE.md item 18
        #                     for the two bugs it fixes; opt in if you hit them.
        "browser_launch_mode": "tab",
        # ── Behavior preferences ──
        # Session starts → task moves to Working
        "auto_start_on_session": True,
        # Child Working → parent moves from Not Started to Working
        "auto_parent_working": True,
        # Child Remediating → parent reverts from Complete to Remediating
        "auto_parent_reopen": True,
        # All children/sessions done → task moves to Validating
        "auto_advance_to_validating": False,
        # ── AI autonomy preferences ──
        # AI sessions can change task statuses (working, validating, etc.)
        "ai_can_modify_status": True,
        # AI planner / sessions can mark tasks as complete
        "ai_can_mark_complete": True,
        # ── Cross-session awareness ──
        # Inject other active sessions' names/status/files into system prompts
        "cross_session_awareness": True,
        # Warn before sending a prompt that seems unrelated to the current session
        "wrong_session_detection": True,
        # Smart termination (daemon/smart_termination.py): every turn ends
        # with a "what we're working on / where it stands / what's needed
        # from you" handoff, and a session stopping with work it could still
        # do is sent back once to think again.  Opt-in (off by default) by the
        # owner's decision, 2026-10-27.
        "smart_termination": False,
        # ── Validation URL preferences ──
        "validation_url_enabled": False,
        "validation_base_url": "",
        "validation_url_dismissed": False,
        # ── Performance preferences ──
        # File tracking snapshots every source file's mtime each turn for
        # undo/rewind support.  Disable to speed up sessions on large repos.
        "file_tracking_enabled": True,
        # ── Large-session performance ──
        # Backend-only, no UI.  See docs/plans/large-session-perf.md.  These
        # govern the lossless stale-media eviction that runs ONLY in the
        # pre-`--resume` executor pass (never per-turn).  Old inline image
        # payloads are externalized to ~/.claude/session-env/<sid>/vibenode-media/
        # (content-addressed by sha256, fully recoverable) and replaced in the
        # replayed transcript by a short text placeholder, shrinking both the
        # bytes the CLI reads and the stale vision tokens the model re-attends.
        #
        # Master switch for WS1 (inline-image eviction) + WS2 (toolUseResult
        # de-dup).  Disabling reverts to pure repair behavior with no code change.
        "large_session_media_eviction": True,
        # K = number of recent user turns whose images stay inline for the model
        # (active screenshot→fix→screenshot loops are never degraded).  Older
        # images are externalized.
        "large_session_media_keep_recent_turns": 4,
        # WS2: also externalize the provably-redundant `toolUseResult.file.base64`
        # duplicate even for images still inside the recent-K window (the model
        # only ever replays the inline `source.data` copy; toolUseResult is
        # auxiliary metadata the CLI never feeds back).  Independently switchable.
        "large_session_dedup_recent_tooluseresult": True,
    }


def save_kanban_config(config: dict) -> None:
    """Save kanban configuration to kanban_config.json."""
    global _kanban_config_cache, _kanban_config_cache_time
    _KANBAN_CONFIG_FILE.write_text(
        json.dumps(config, indent=2, ensure_ascii=False), encoding="utf-8"
    )
    # Invalidate cache so the next read sees the new values immediately
    _kanban_config_cache = None
    _kanban_config_cache_time = 0.0
