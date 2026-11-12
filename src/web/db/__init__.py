"""
Kanban database layer.

Provides a factory that returns the configured repository backend.
The singleton is cached so all callers share one connection.
"""

import os
import threading
import time

_repo = None
_lock = threading.Lock()

# After a backend fails to start (Supabase down or not answering), callers get
# the same error at once for this long instead of each trying again.  Every
# session status change calls into here, so retrying per request turned one
# unreachable Supabase project into a queue of stuck requests.
FAILURE_PAUSE_S = 30.0
_failed_at = 0.0
_failed_error = None


def create_repository(backend=None):
    """Create and return a repository instance.  Singleton pattern.

    Parameters
    ----------
    backend : str or None
        ``'sqlite'`` or ``'supabase'``.  When *None* (the default), the
        backend is read from ``kanban_config.json`` (falling back to
        ``'sqlite'`` if the file is missing or unparseable).

    Returns
    -------
    KanbanRepository
        A fully initialised repository ready for queries.
    """
    global _repo, _failed_at, _failed_error
    if _repo is not None:
        return _repo
    _raise_if_recently_failed()

    with _lock:
        # Double-check after acquiring lock
        if _repo is not None:
            return _repo
        _raise_if_recently_failed()

        # Resolve backend from config when not explicitly provided
        if backend is None:
            from ..config import get_kanban_config
            cfg = get_kanban_config()
            backend = cfg.get("kanban_backend", "sqlite")

        if backend == "sqlite":
            from .sqlite_backend import SqliteRepository
            repo = SqliteRepository()

        elif backend == "supabase":
            try:
                from .supabase_backend import SupabaseRepository
            except ImportError as exc:
                raise ImportError(
                    "The 'supabase' package is required for the Supabase "
                    "kanban backend.  Install it with:  pip install supabase"
                ) from exc

            # Read credentials from config, fall back to env vars
            from ..config import get_kanban_config
            cfg = get_kanban_config()
            url = cfg.get("supabase_url") or os.environ.get("SUPABASE_URL")
            key = cfg.get("supabase_secret_key") or os.environ.get("SUPABASE_SECRET_KEY")
            if not url or not key:
                raise ValueError(
                    "Supabase backend requires supabase_url and "
                    "supabase_secret_key in kanban_config.json (or "
                    "SUPABASE_URL / SUPABASE_SECRET_KEY env vars)."
                )
            repo = SupabaseRepository(url=url, key=key)

        else:
            raise ValueError(f"Unknown kanban backend: {backend!r}")

        try:
            repo.initialize()
        except Exception as exc:
            _failed_at = time.monotonic()
            _failed_error = exc
            raise
        _repo = repo
        _failed_error = None
        return _repo


def _raise_if_recently_failed():
    err = _failed_error
    if err is not None and time.monotonic() - _failed_at < FAILURE_PAUSE_S:
        raise err


def reset_repository():
    """Clear the cached singleton so the next create_repository() call
    creates a fresh instance.  Call this after switching backends."""
    global _repo, _failed_error
    with _lock:
        _failed_error = None
        if _repo is not None:
            try:
                _repo.close()
            except Exception:
                pass
            _repo = None
