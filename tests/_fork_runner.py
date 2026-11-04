"""Run the whole suite by forking workers from one collected process (Linux).

pytest-xdist starts each worker as a new interpreter: it imports pytest, the
app and the engine, and collects all ~5,000 tests by itself, 8 times per run,
before the first test starts.  Here the process collects once, with garbage
collection frozen (see conftest), and forks.  Each child has the imports and
the collected items already, and the run spends its time on tests.

What does not change:

* Tests run through pytest's own ``pytest_runtest_protocol``, so fixtures,
  hooks, capture, timeouts and reports are the normal ones.  Reports travel to
  this process through pytest's own serialisation hooks, the same way xdist
  sends them, so the terminal, ``--junitxml``, ``-x`` and the exit status work
  from the same data.
* The placement rules are xdist's and ``tests/_xdist_schedule.py``'s: a file
  stays whole on one worker unless it opts in with ``XDIST_SPLITTABLE``,
  slowest first, a cap on workers on browser work (``BROWSER_CAP``).
* Nothing is shared between workers after the fork: each has its own capture
  files, base temp folder, browser and sandboxes, and a test's writes to memory
  stay in its own worker.
* A worker that dies is replaced by a new fork; the test it was running gets
  one retry, then counts as failed (``pytest_handlecrashitem``'s rule).

Only forked while this process has a single thread, so a child never inherits
a lock held by a thread that does not exist there.  Elsewhere (Windows, macOS,
``-n``) xdist runs as before.
"""

from __future__ import annotations

import faulthandler
import importlib
import os
import pickle
import selectors
import signal
import struct
import sys
import threading
import time
import traceback
from collections import deque
from pathlib import Path

import pytest
from _pytest.reports import TestReport

from tests._xdist_schedule import _SPLIT_FLAG, DURATIONS_CACHE_NAME, HEAVY_CAP, _file_of

_HEADER = struct.Struct("!I")

# Workers on browser work at once.  xdist's cap (4) was measured when a browser
# test cost twice the CPU it does now; with forked workers and reused pages,
# 4 / 8 / 12 took 24.8 / 21.0 / 22.0 s (12 workers).
BROWSER_CAP = int(os.environ.get("VIBENODE_TEST_BROWSER_CAP") or 8)


def _send(fd: int, message) -> None:
    data = pickle.dumps(message, protocol=pickle.HIGHEST_PROTOCOL)
    data = _HEADER.pack(len(data)) + data
    view = memoryview(data)
    while view:
        view = view[os.write(fd, view):]


def _recv(fd: int):
    def exactly(n):
        buf = bytearray()
        while len(buf) < n:
            chunk = os.read(fd, n - len(buf))
            if not chunk:
                raise EOFError
            buf += chunk
        return bytes(buf)
    return pickle.loads(exactly(_HEADER.unpack(exactly(_HEADER.size))[0]))


# ── the plan: units of work, in order ────────────────────────────────────────

class _Unit:
    __slots__ = ("indexes", "heavy", "cost")

    def __init__(self, indexes, heavy, cost):
        self.indexes, self.heavy, self.cost = indexes, heavy, cost


def plan_units(items: list, durations: dict, rootpath: Path, chunks: int = HEAVY_CAP) -> list:
    """Whole files (or a splittable file's contiguous chunks), slowest first.
    A unit with no history costs as much as the slowest known one."""
    by_file: dict[str, list[int]] = {}
    for i, item in enumerate(items):
        by_file.setdefault(_file_of(item.nodeid), []).append(i)

    raw = []
    for path, indexes in by_file.items():
        try:
            src = (rootpath / path).read_text(encoding="utf-8")
        except OSError:
            src = ""
        heavy = "sync_playwright" in src or "shared_browser_scope" in src
        if _SPLIT_FLAG.search(src):
            n = max(1, min(chunks, len(indexes)))
            parts = [indexes[i * len(indexes) // n:(i + 1) * len(indexes) // n] for i in range(n)]
        else:
            parts = [indexes]
        raw.extend((part, heavy) for part in parts)

    def known(part):
        return all(items[i].nodeid in durations for i in part)

    def total(part):
        return sum(float(durations[items[i].nodeid]) for i in part)

    measured = [total(p) for p, _ in raw if known(p)]
    unknown = max(measured) if measured else 0.0
    units = [_Unit(p, h, total(p) if known(p) else unknown) for p, h in raw]
    return sorted(units, key=lambda u: (-u.cost, -len(u.indexes)))


# ── a worker ─────────────────────────────────────────────────────────────────

class _Forwarder:
    """In a worker: hand this worker's test events to the parent."""

    def __init__(self, config, wfd):
        self.config, self.wfd = config, wfd

    @pytest.hookimpl(trylast=True)
    def pytest_runtest_logstart(self, nodeid, location):
        _send(self.wfd, ("logstart", nodeid, location))

    @pytest.hookimpl(trylast=True)
    def pytest_runtest_logfinish(self, nodeid, location):
        _send(self.wfd, ("logfinish", nodeid, location))

    @pytest.hookimpl(trylast=True)
    def pytest_runtest_logreport(self, report):
        data = self.config.hook.pytest_report_to_serializable(config=self.config, report=report)
        _send(self.wfd, ("report", data))

    @pytest.hookimpl(trylast=True)
    def pytest_warning_recorded(self, warning_message, when, nodeid, location):
        try:
            from xdist.remote import serialize_warning_message
            _send(self.wfd, ("warning", serialize_warning_message(warning_message), when, nodeid, location))
        except Exception:
            pass


def _become_worker(session, name: str, rfd: int, wfd: int) -> None:
    config = session.config
    pm = config.pluginmanager

    os.environ["PYTEST_XDIST_WORKER"] = name
    # A Session Host a test starts exits when its owner dies: that is this
    # worker now, not the parent (see conftest's VIBENODE_HOST_TEST_OWNER).
    try:
        from engine import session_host_protocol as shp
        os.environ["VIBENODE_HOST_TEST_OWNER"] = f"{os.getpid()}:{shp.process_create_time(os.getpid()) or 0.0}"
    except Exception:
        pass

    # Reporting belongs to the parent: this worker only forwards.
    for plugin_name in ("terminalreporter",):
        plugin = pm.get_plugin(plugin_name)
        if plugin is not None:
            pm.unregister(plugin)
    from _pytest.junitxml import xml_key
    xml = config.stash.get(xml_key, None)
    if xml is not None:
        pm.unregister(xml)

    # The capture files are open file descriptions shared with every other
    # process forked from here: tests in two workers would read each other's
    # output.  Start this worker's own.
    capman = pm.get_plugin("capturemanager")
    if capman is not None:
        capman.stop_global_capturing()
        capman.start_global_capturing()

    # Its own temp folder, under the run's.
    factory = config._tmp_path_factory
    base = factory.getbasetemp() / name
    base.mkdir()
    factory._basetemp = base

    pm.register(_Forwarder(config, wfd), "vn-fork-forwarder")

    # What the parent reads if this worker goes silent (see Runner._hung).
    stacks = open(_stack_file(config, name), "w")   # kept open for the process's life
    faulthandler.register(signal.SIGUSR1, file=stacks, all_threads=True)

    items = session.items
    todo: deque = deque()
    no_more = False

    def ask(block_for_work: bool) -> None:
        nonlocal no_more
        _send(wfd, ("need", block_for_work))
        answer = _recv(rfd)
        if answer is None:
            no_more = True
        elif answer:
            todo.extend(answer)

    while True:
        # Keep one test back until the next is known: its teardown depends on it.
        if len(todo) < 2 and not no_more:
            ask(block_for_work=not todo)
        if not todo:
            if no_more:
                return
            continue
        index = todo.popleft()
        nextitem = items[todo[0]] if todo else None
        items[index].config.hook.pytest_runtest_protocol(item=items[index], nextitem=nextitem)


def _stack_file(config, name: str) -> Path:
    return Path(config._tmp_path_factory._vn_run_base) / f"{name}.stacks"


def _worker_main(session, name: str, rfd: int, wfd: int, other_fds: list) -> None:
    code = 0
    for fd in other_fds:
        try:
            os.close(fd)
        except OSError:
            pass
    try:
        _become_worker(session, name, rfd, wfd)
    except BaseException:
        code = 1
        try:
            os.write(2, f"\n[fork runner] {name} stopped:\n{traceback.format_exc()}\n".encode())
        except OSError:
            pass
    # Tear down what the worker opened; never the parent's exit handlers.
    try:
        from tests import _browser
        _browser.close_shared_browser()
        cf = sys.modules.get("tests.conftest")
        if cf is not None:
            cf._kill_spawned_hosts()
    except BaseException:
        pass
    os._exit(code)


# ── the parent ───────────────────────────────────────────────────────────────

class _Child:
    __slots__ = ("name", "pid", "rfd", "wfd", "buf", "unfinished", "heavy_left", "wants", "wants_now", "heard_at",
                 "browser")

    def __init__(self, name, pid, rfd, wfd):
        self.name, self.pid, self.rfd, self.wfd = name, pid, rfd, wfd
        self.buf = bytearray()
        self.unfinished: list[int] = []     # item indexes given and not finished, in order
        self.heavy_left = 0                 # of those, how many are browser tests
        self.wants = False
        self.wants_now = False              # has nothing left to run
        self.heard_at = time.monotonic()
        self.browser = False                # has been given browser work


class Runner:
    def __init__(self, session, workers: int):
        self.session = session
        self.config = session.config
        self.workers = workers
        self.items = session.items
        self.index_of = {item.nodeid: i for i, item in enumerate(self.items)}
        cache = getattr(self.config, "cache", None)
        durations = (cache.get(DURATIONS_CACHE_NAME, {}) if cache else {}) or {}
        self.queue: list[_Unit] = plan_units(self.items, durations, Path(str(self.config.rootpath)))
        self.heavy_item = {i for u in self.queue if u.heavy for i in u.indexes}
        self.children: dict[int, _Child] = {}
        self.sel = selectors.DefaultSelector()
        self.crashes: dict[str, int] = {}
        self.spawned = 0
        self.hang_notes: dict[str, str] = {}
        per_test = float(getattr(self.config.option, "timeout", None) or 60)
        self.hang_after = max(30.0, per_test * 2)

    # ── children ─────────────────────────────────────────────────────────
    def _spawn(self) -> None:
        name = f"fork{self.spawned}"
        self.spawned += 1
        to_child_r, to_child_w = os.pipe()
        from_child_r, from_child_w = os.pipe()
        pid = os.fork()
        if pid == 0:
            others = [to_child_w, from_child_r]
            for c in self.children.values():
                others += [c.rfd, c.wfd]
            self.sel = None
            _worker_main(self.session, name, to_child_r, from_child_w, others)
            os._exit(1)     # not reached
        os.close(to_child_r)
        os.close(from_child_w)
        os.set_blocking(from_child_r, False)
        child = _Child(name, pid, from_child_r, to_child_w)
        self.children[from_child_r] = child
        self.sel.register(from_child_r, selectors.EVENT_READ, child)

    def _browser_workers(self) -> int:
        return sum(1 for c in self.children.values() if c.browser)

    def _pick_unit(self, child: _Child):
        """Browser work goes to at most BROWSER_CAP workers, and they keep it:
        each launch of a driver and a browser costs CPU, and a worker closes its
        browser whenever it moves on to other work (conftest).  A browser worker
        takes browser units first; the others never touch them while the cap is
        full, so a run launches about BROWSER_CAP browsers, not one per switch."""
        if not child.browser and self._browser_workers() < BROWSER_CAP:
            for unit in self.queue:
                if unit.heavy:
                    child.browser = True
                    return unit
        if child.browser:
            for unit in self.queue:
                if unit.heavy:
                    return unit
        for unit in self.queue:
            if not unit.heavy:
                return unit
        return None

    def _answer(self, child: _Child) -> bool:
        """Answer a worker's request, if it can be answered now."""
        if not self.queue:
            # A crash can still requeue work; a new fork takes that.
            _send(child.wfd, None)
            child.wants = False
            return True
        unit = self._pick_unit(child)
        if unit is None:
            if not child.wants_now:
                # It holds a last test that must not wait on a slot it may be
                # the one to free: tell it to run that test and ask again.
                _send(child.wfd, [])
                child.wants = False
                return True
            return False
        self.queue.remove(unit)
        child.unfinished.extend(unit.indexes)
        if unit.heavy:
            child.heavy_left += len(unit.indexes)
        _send(child.wfd, list(unit.indexes))
        child.wants = False
        return True

    def _offer_work(self) -> None:
        for child in list(self.children.values()):
            if child.wants:
                self._answer(child)

    # ── events ───────────────────────────────────────────────────────────
    def _handle(self, child: _Child, message) -> None:
        hook = self.config.hook
        kind = message[0]
        if kind == "report":
            report = hook.pytest_report_from_serializable(config=self.config, data=message[1])
            hook.pytest_runtest_logreport(report=report)
        elif kind == "logstart":
            hook.pytest_runtest_logstart(nodeid=message[1], location=message[2])
        elif kind == "logfinish":
            hook.pytest_runtest_logfinish(nodeid=message[1], location=message[2])
            index = self.index_of[message[1]]
            if index in child.unfinished:
                child.unfinished.remove(index)
                if index in self.heavy_item:
                    child.heavy_left -= 1
            self._offer_work()
        elif kind == "warning":
            from xdist.workermanage import unserialize_warning_message
            hook.pytest_warning_recorded.call_historic(kwargs={
                "warning_message": unserialize_warning_message(message[1]),
                "when": message[2], "nodeid": message[3], "location": message[4]})
        elif kind == "need":
            child.wants, child.wants_now = True, bool(message[1])
            self._answer(child)

    def _child_gone(self, child: _Child) -> None:
        self.sel.unregister(child.rfd)
        del self.children[child.rfd]
        os.close(child.rfd)
        os.close(child.wfd)
        try:
            _, status = os.waitpid(child.pid, 0)
        except OSError:
            status = 0
        if child.unfinished:
            self._crashed(child, status)
        self._offer_work()

    def _crashed(self, child: _Child, status) -> None:
        crashed = self.items[child.unfinished[0]]
        rest = child.unfinished
        count = self.crashes.get(crashed.nodeid, 0) + 1
        self.crashes[crashed.nodeid] = count
        retry = count == 1 and not os.environ.get("VIBENODE_NO_CRASH_RETRY")
        if not retry:
            rest = rest[1:]
            hook = self.config.hook
            hook.pytest_runtest_logreport(report=TestReport(
                nodeid=crashed.nodeid, location=crashed.location, keywords={}, outcome="failed",
                longrepr=f"worker {child.name} crashed while running this test (status {status})\n"
                         + self.hang_notes.get(child.name, ""),
                when="call"))
            hook.pytest_runtest_logfinish(nodeid=crashed.nodeid, location=crashed.location)
        if rest:
            self.queue.insert(0, _Unit(rest, rest[0] in self.heavy_item, 0.0))
        if rest and len(self.children) < self.workers:
            self._spawn()

    def _hung(self) -> None:
        """A worker silent for twice the per-test timeout is stuck where the
        timeout cannot reach it (C code holding a lock).  A hang must never
        hold up the run with nothing on screen: report where it was and treat
        it as a crash.  A worker waiting for work is not silent, it is waiting."""
        now = time.monotonic()
        for child in list(self.children.values()):
            if child.wants or now - child.heard_at < self.hang_after:
                continue
            os.kill(child.pid, signal.SIGUSR1)
            time.sleep(0.2)
            try:
                stacks = _stack_file(self.config, child.name).read_text(encoding="utf-8", errors="replace")
            except OSError:
                stacks = "(no stacks written)"
            where = self.items[child.unfinished[0]].nodeid if child.unfinished else "after its last test"
            self.hang_notes[child.name] = stacks
            self.config.get_terminal_writer().write(
                f"\n[fork runner] {child.name} silent for {self.hang_after:.0f}s, {where}:\n{stacks}\n", flush=True)
            os.kill(child.pid, signal.SIGKILL)

    def _pump(self, child: _Child) -> None:
        try:
            chunk = os.read(child.rfd, 1 << 20)
        except BlockingIOError:
            return
        if not chunk:
            self._child_gone(child)
            return
        child.heard_at = time.monotonic()
        child.buf += chunk
        while len(child.buf) >= _HEADER.size:
            size = _HEADER.unpack_from(child.buf)[0]
            if len(child.buf) < _HEADER.size + size:
                break
            message = pickle.loads(bytes(child.buf[_HEADER.size:_HEADER.size + size]))
            del child.buf[:_HEADER.size + size]
            self._handle(child, message)

    # ── the loop ─────────────────────────────────────────────────────────
    def run(self) -> None:
        session = self.session
        for _ in range(min(self.workers, max(1, len(self.queue)))):
            self._spawn()
        try:
            while self.children:
                for key, _ in self.sel.select(timeout=2):
                    self._pump(key.data)
                self._hung()
                if session.shouldfail or session.shouldstop:
                    break       # -x / --maxfail: the workers go down in the finally below
            if session.shouldfail:
                raise session.Failed(session.shouldfail)
            if session.shouldstop:
                raise session.Interrupted(session.shouldstop)
        finally:
            for child in list(self.children.values()):
                try:
                    os.kill(child.pid, signal.SIGKILL)
                    os.waitpid(child.pid, 0)
                except OSError:
                    pass


def can_fork() -> bool:
    """One thread by the OS's count: ``threading`` does not see native ones."""
    return (hasattr(os, "fork") and threading.active_count() == 1
            and len(os.listdir("/proc/self/task")) == 1)


def run_session(session, workers: int) -> bool:
    # Everything workers would each import on their first test: once, here.
    cf = sys.modules.get("tests.conftest")
    names = list(getattr(cf, "_CLAUDE_PROJECTS_HOLDERS", ()))
    if cf is not None:
        import pkgutil
        try:
            import web.routes as routes
            names += [f"web.routes.{m.name}" for m in pkgutil.iter_modules(routes.__path__)]
        except Exception:
            pass
        names += ["web.sessions", "web.search_index", "web.git_ops", "web.session_holds", "web.session_pins",
                  "web.auto_namer", "engine.smart_termination_live", "PIL.Image", "cryptography.fernet",
                  "simple_websocket", "tracemalloc"]
    for name in names:
        try:
            importlib.import_module(name)
        except Exception:
            pass
    factory = session.config._tmp_path_factory
    factory._vn_run_base = factory.getbasetemp()
    # Importing the engine arms a hang dump (_early_boot): a native thread, and
    # a child that later cancels it waits forever on that thread's lock, with no
    # Python to interrupt.  It watches for a hung engine boot, not for tests.
    faulthandler.cancel_dump_traceback_later()
    deadline = time.monotonic() + 2     # a cancelled thread takes a moment to leave
    while not can_fork() and time.monotonic() < deadline:
        time.sleep(0.005)
    if not can_fork():
        session.config.get_terminal_writer().line(
            "[fork runner] another thread is running, so workers cannot be forked safely: "
            "running every test in this one process (slower, same tests).")
        return False
    Runner(session, workers).run()
    return True
