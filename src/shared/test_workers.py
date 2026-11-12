"""How many parallel workers a run of the test suite uses.

One rule for the Publish gate and a plain ``pytest`` of the whole suite.  Counts
the CPUs this process may actually use, which can be fewer than the machine has.
Leaves two free so a run does not starve the live VibeNode it is testing.
"""

import os


def _usable_cpus() -> int:
    try:
        return len(os.sched_getaffinity(0))
    except (AttributeError, OSError):       # Windows, macOS
        return os.cpu_count() or 2


def worker_count() -> int:
    """xdist workers: each is its own interpreter."""
    return max(2, min(8, _usable_cpus() - 2))


def fork_worker_count() -> int:
    """Forked workers (Linux): measured on 16 usable CPUs, 8 / 12 / 16
    workers took 26 / 22 / 22 seconds, so past 12 only the load goes up."""
    return max(2, min(12, _usable_cpus() - 2))
