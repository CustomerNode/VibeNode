"""The xdist crash-retry hook (see the note above its import in tests/conftest.py).

Its own module so a nested pytest (tests/test_xdist_schedule.py) can load this
hook without importing the whole suite's conftest in every nested worker.
"""

import os

import pytest

_crash_retry_count: dict[str, int] = {}


@pytest.hookimpl(tryfirst=True, optionalhook=True)
def pytest_handlecrashitem(crashitem, report, sched):
    if os.environ.get("VIBENODE_NO_CRASH_RETRY"):
        return
    count = _crash_retry_count.get(crashitem, 0) + 1
    _crash_retry_count[crashitem] = count
    if count > 1:
        # Already retried once and crashed again: let the failure stand.
        return
    # Mark as a non-counted outcome so pytest's testsfailed stays at 0 for
    # this spurious failed report.  The LoadScopeScheduling base re-queued
    # the workload (including this nodeid) in ``remove_node`` seconds ago,
    # so another worker will run the test again and its report decides the
    # outcome.
    report.outcome = "rerun"
