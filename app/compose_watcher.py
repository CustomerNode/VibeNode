"""
Backwards-compatibility shim — compose watcher moved to app/compose/watcher.py.
"""
from .compose.watcher import start_compose_watcher, stop_compose_watcher  # noqa: F401
