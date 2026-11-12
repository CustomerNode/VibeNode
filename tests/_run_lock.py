"""One parallel test run at a time: see ``src/shared/test_run_lock.py``.

The lock lives in ``src/shared/`` so the web server's background test run
(``src/web/test_prewarm.py``) can see whether another run holds it without
importing from ``tests/``.
"""

from shared.test_run_lock import DEFAULT_PATH, RunLock, enabled, max_wait  # noqa: F401
