"""Work scheduling for parallel runs (pytest-xdist ``--dist loadfile``).

The Publish gate runs the suite on 8 workers with ``--dist loadfile``: every
test file is one unit of work, run whole on one worker, because many daemon
test files share module-level state.  That rule stays.  Around it, runs
reached ~98% quickly and then stalled for a minute or more:

* xdist queues units by TEST COUNT, largest first.  Browser files (headless
  Chromium loading the real page) have few tests but are the slowest units,
  so they were queued last, started last, and ran alone while the other
  workers idled.  ``test_live_thinking_switch.py`` alone took ~120s.
* Simply queueing them first is worse: 8 workers driving Chromium at once
  starve the CPU (measured 2026-10-17: total test time 618s -> 1,623s, the
  thinking file 122s -> 774s, and a daemon test timed out).

So this scheduler:

1. Queues units by MEASURED duration from earlier runs, slowest first.
   ``tests/conftest.py`` records every test's time in pytest's cache
   (``.pytest_cache``, gitignored).  A unit with no history is queued as if
   it were the slowest, so a new slow file never lands at the tail.  With no
   history at all the order is xdist's own (test count).
2. Caps browser work: at most ``HEAVY_CAP`` workers run a unit from a file
   that launches Chromium at the same time.  The rest run everything else.
   A worker is never left without work: once only browser units remain, the
   cap no longer holds them back.
3. Splits a browser file into ``HEAVY_CAP`` contiguous chunks when the file
   opts in with a top-level ``XDIST_SPLITTABLE = True``.  Opt in only when
   every test builds its own state (a fresh page per test) and no test
   depends on another having run first.  Contiguous chunks keep a worker's
   module-scoped browser alive across the chunk.

Nothing here changes which tests run, only where and in what order.
"""

from __future__ import annotations

import re
from pathlib import Path

from xdist.scheduler import LoadFileScheduling

DURATIONS_CACHE_NAME = "vibenode/test_durations"
HEAVY_CAP = 3
_SPLIT_FLAG = re.compile(r"^XDIST_SPLITTABLE\s*=\s*True\s*$", re.M)
_CHUNK = "::#chunk"


def _file_of(nodeid: str) -> str:
    return nodeid.split("::", 1)[0]


class DurationOrderedLoadFileScheduling(LoadFileScheduling):
    def __init__(self, config, log=None):
        super().__init__(config, log)
        self._rootdir = Path(str(config.rootpath))
        self._sources: dict[str, str] = {}
        self._chunk_of: dict[str, str] = {}
        cache = getattr(config, "cache", None)
        self._durations: dict = (cache.get(DURATIONS_CACHE_NAME, {}) if cache else {}) or {}

    # ── what a file is ──────────────────────────────────────────────────
    def _source(self, path: str) -> str:
        if path not in self._sources:
            try:
                self._sources[path] = (self._rootdir / path).read_text(encoding="utf-8")
            except OSError:
                self._sources[path] = ""
        return self._sources[path]

    def _splittable(self, path: str) -> bool:
        return bool(_SPLIT_FLAG.search(self._source(path)))

    def _heavy(self, scope: str) -> bool:
        return "sync_playwright" in self._source(_file_of(scope))

    # ── units ───────────────────────────────────────────────────────────
    def plan_chunks(self, collection: list[str], chunks: int = HEAVY_CAP) -> None:
        """Map each test of a splittable file to one of ``chunks`` contiguous
        pieces of that file."""
        by_file: dict[str, list[str]] = {}
        for nodeid in collection:
            path = _file_of(nodeid)
            if self._splittable(path):
                by_file.setdefault(path, []).append(nodeid)
        for path, ids in by_file.items():
            n = max(1, min(chunks, len(ids)))
            size = -(-len(ids) // n)
            for i, nodeid in enumerate(ids):
                self._chunk_of[nodeid] = f"{path}{_CHUNK}{i // size}"

    def _split_scope(self, nodeid: str) -> str:
        return self._chunk_of.get(nodeid) or _file_of(nodeid)

    def schedule(self) -> None:
        if self.collection is None and self.collection_is_completed and self.registered_collections:
            self.plan_chunks(list(next(iter(self.registered_collections.values()))))
        super().schedule()

    # ── order and placement ─────────────────────────────────────────────
    def order_units(self, units: dict) -> list:
        """Return the unit keys of ``{scope: {nodeid: done}}`` slowest first."""
        def known(ids):
            return all(n in self._durations for n in ids)

        def total(ids):
            return sum(float(self._durations[n]) for n in ids)

        measured = [total(ids) for ids in units.values() if known(ids)]
        unknown_cost = max(measured) if measured else 0.0

        def cost(scope):
            ids = units[scope]
            return total(ids) if known(ids) else unknown_cost

        return sorted(units, key=lambda s: (-cost(s), -len(units[s])))

    def _runs_heavy(self, node) -> bool:
        return any(self._heavy(scope) and not all(done.values())
                   for scope, done in self.assigned_work.get(node, {}).items())

    def pick_unit(self, node) -> str:
        """The first queued unit this node may take: a browser unit only while
        fewer than HEAVY_CAP other workers run one (a worker already on one
        adds no concurrency); otherwise the first non-browser unit; and if
        only browser units are left, the first of them."""
        others = sum(1 for n in self.assigned_work if n is not node and self._runs_heavy(n))
        heavy_ok = self._runs_heavy(node) or others < HEAVY_CAP
        for scope in self.workqueue:
            if heavy_ok or not self._heavy(scope):
                return scope
        return next(iter(self.workqueue))

    def _assign_work_unit(self, node) -> None:
        # Units are sent as indexes into each worker's own collection, which
        # stays as collected; only the QUEUE is reordered here.
        if not getattr(self, "_queue_ordered", False):
            self._queue_ordered = True
            pending = dict(self.workqueue)
            self.workqueue.clear()
            for scope in self.order_units(pending):
                self.workqueue[scope] = pending[scope]
        self.workqueue.move_to_end(self.pick_unit(node), last=False)
        super()._assign_work_unit(node)
