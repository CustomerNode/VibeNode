"""CLAUDE.md PERF #14 — IPC profiling logger namespace guard.

PERF-CRITICAL marker #14 says:

    "IPC profiling logger namespace — ``run.py``.  ``"app.daemon_client"``
    must be in the logger namespace list.  Removing silences IPC profiling."

If someone deletes the entry from the namespace list in ``run.py``, the
``app.daemon_client`` logger loses its handler and every IPC call between
the Flask web process and the session daemon stops being logged.  Nothing
crashes — the test suite still passes — but the operator loses all
visibility into IPC latency the next time they look at the logs.

This test screams loudly if that line ever disappears.  It mirrors the
source-string presence pattern used by the other guards in
``tests/test_performance_guards.py`` (see TestAsyncioGatherInSendQuery,
TestDetectChangedFilesGuard, etc.).
"""

import pathlib
import re


class TestIpcProfilingLoggerNamespace:
    """CLAUDE.md PERF #14 — ``"app.daemon_client"`` must remain in the
    logger namespace list inside ``run.py``."""

    def _run_py_source(self):
        repo_root = pathlib.Path(__file__).resolve().parents[1]
        run_py = repo_root / "run.py"
        return run_py.read_text(encoding="utf-8", errors="replace")

    def test_app_daemon_client_literal_present(self):
        """The literal string ``"app.daemon_client"`` (or single-quoted
        equivalent) must appear somewhere in ``run.py``.  Removing it is
        what silences IPC profiling."""
        src = self._run_py_source()
        assert (
            '"app.daemon_client"' in src
            or "'app.daemon_client'" in src
        ), (
            "PERF #14 regression: the literal 'app.daemon_client' is "
            "missing from run.py — IPC profiling has been silenced. "
            "See CLAUDE.md PERF-CRITICAL #14."
        )

    def test_app_daemon_client_in_logger_namespace_list(self):
        """``"app.daemon_client"`` must appear next to other logger
        namespace strings (e.g. ``"app.routes"``).  This guards against a
        rename that drops it from the iterable but leaves an unrelated
        comment behind.

        Match a tuple/list literal that contains BOTH ``"app.routes"`` and
        ``"app.daemon_client"`` — that is the exact shape PERF #14
        prescribes.  We use ``re.DOTALL`` so the two literals are allowed
        to sit on separate lines inside a multi-line tuple.
        """
        src = self._run_py_source()
        pattern = re.compile(
            r"""[\(\[]                # opening ( or [
            [^\(\)\[\]]*?             # anything except brackets
            ["']app\.routes["']       # "app.routes"
            [^\(\)\[\]]*?             # anything except brackets
            ["']app\.daemon_client["']# "app.daemon_client"
            [^\(\)\[\]]*?             # anything except brackets
            [\)\]]                    # closing ) or ]
            """,
            re.VERBOSE | re.DOTALL,
        )
        # Either order is acceptable — match the reverse too.
        pattern_rev = re.compile(
            r"""[\(\[]
            [^\(\)\[\]]*?
            ["']app\.daemon_client["']
            [^\(\)\[\]]*?
            ["']app\.routes["']
            [^\(\)\[\]]*?
            [\)\]]
            """,
            re.VERBOSE | re.DOTALL,
        )
        assert pattern.search(src) or pattern_rev.search(src), (
            "PERF #14 regression: 'app.daemon_client' is no longer inside "
            "the same logger-namespace tuple/list as 'app.routes' in "
            "run.py.  IPC profiling needs both entries attached to the "
            "same stdout handler.  See CLAUDE.md PERF-CRITICAL #14."
        )
