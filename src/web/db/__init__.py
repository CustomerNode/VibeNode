"""
Kanban database layer.

Provides a factory that returns the configured repository backend.
The singleton is cached so all callers share one connection.
"""

import os
import threading

_repo = None
_lock = threading.Lock()


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
    global _repo
    if _repo is not None:
        return _repo

    with _lock:
        # Double-check after acquiring lock
        if _repo is not None:
            return _repo

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

        repo.initialize()
        _repo = repo
        return _repo


def reset_repository():
    """Clear the cached singleton so the next create_repository() call
    creates a fresh instance.  Call this after switching backends."""
    global _repo
    with _lock:
        if _repo is not None:
            try:
                _repo.close()
            except Exception:
                pass
            _repo = None
