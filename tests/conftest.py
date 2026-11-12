"""Shared test fixtures for VibeNode.

These fixtures are available to ALL tests.  No server is started here: the
browser tests build their own page in headless Chromium (tests/_browser.py)
with the network faked.

PRODUCTION-PATH GUARD
=====================
The autouse ``_block_production_paths`` fixture below makes it physically
impossible for any test to open the user's real kanban DB or rewrite the
real ``kanban_config.json``. It intercepts ``sqlite3.connect`` and
``Path.write_text`` and raises if either is called with a production path.

A migrate test that constructs a default-path SqliteRepository() and runs
``clear_all_data()`` would wipe the user's live DB. Individual tests should still use the
``kanban_app`` fixture or explicit monkeypatching, but this is a hard
backstop for that whole class of bug — present and future.

REVIVER / AUTOSTART GUARD
=========================
The autouse ``_forbid_reviver_and_autostart_spawn`` fixture below makes it
physically impossible for any test to launch the real ``reviver.py`` or
``schtasks.exe``, or to write inside the real Windows Startup folder.

Background: the real ``mobile_command.enable()`` / ``.disable()`` spawn real
``reviver.py`` and ``reviver.py --unregister`` from the production checkout.
Because the reviver's task name and Startup VBS path are salted from the
checkout path alone, a test calling them addresses production's real
``\\VibeNodeReviver_<salt>`` scheduled task and Startup VBS, deleting
and rewriting them with whatever interpreter ran the tests.

The affected tests now mock ``_spawn_reviver`` / ``_retire_reviver`` in their
own fixtures. This conftest layer is the safety net: if any test — present or
future — slips a ``subprocess.Popen`` call for ``reviver.py`` or ``schtasks``
past its own mock, the guard raises loudly with the test's node ID. Silent
regression is impossible.
"""

import gc
import importlib
import itertools
import json
import os
import tempfile
import shlex
import sqlite3
import subprocess
import sys
import time
import pytest
from pathlib import Path
from datetime import datetime, timezone

# The user package folder as it is BEFORE any fixture moves HOME (see
# ``isolate_home``): computed once at import, from the real home.
try:
    import site as _site
    _REAL_USER_BASE = _site.getuserbase() if _site.ENABLE_USER_SITE else ""
except Exception:
    _REAL_USER_BASE = ""


def _real_playwright_browsers() -> str:
    """Where Playwright keeps its browsers, from the REAL home.  On Windows it
    is under LOCALAPPDATA, which ``isolate_home`` leaves alone; on Linux and
    macOS it is under HOME, so a moved HOME made Playwright launched inside a
    test (the preview renderer) find no browser and fall back to the Chrome
    CLI, which timed out."""
    if os.environ.get("PLAYWRIGHT_BROWSERS_PATH") or os.name == "nt":
        return ""
    if sys.platform == "darwin":
        p = Path.home() / "Library" / "Caches" / "ms-playwright"
    else:
        p = Path(os.environ.get("XDG_CACHE_HOME") or Path.home() / ".cache") / "ms-playwright"
    return str(p) if p.is_dir() else ""


_REAL_PLAYWRIGHT_BROWSERS = _real_playwright_browsers()


# ---------------------------------------------------------------------------
# Test temp folders live in memory where the machine has it (Linux).
#
# The suite writes tens of thousands of small files: sandboxes, SQLite files,
# git repositories.  None needs to survive the run, and each write to disk costs
# a sync or a journal entry.  Everything that asks for a temp folder (pytest,
# ``tempfile``, a child process through TMPDIR) gets the same behaviour from
# /dev/shm.  A TMPDIR the caller set is kept.
# ---------------------------------------------------------------------------
def _use_memory_for_temp():
    if os.environ.get("TMPDIR") or not sys.platform.startswith("linux"):
        return
    box = Path("/dev/shm") / f"vibenode-tests-{os.getuid()}"
    try:
        box.mkdir(mode=0o700, exist_ok=True)
        probe = box / f".probe-{os.getpid()}"
        probe.touch()
        probe.unlink()
    except OSError:
        return
    os.environ["TMPDIR"] = str(box)
    import tempfile
    tempfile.tempdir = None


_use_memory_for_temp()


# ---------------------------------------------------------------------------
# The engine's boot hang dump never fires inside a test run.
#
# Importing the engine (src/engine/daemon_server.py) arms faulthandler to dump every
# thread's stack 120 seconds later unless the boot finishes.  A test process
# imports it at collection and never "boots", so in a run longer than 120 seconds
# the dump would fire mid-test, and dumping threads that are running can crash
# Python (SIGSEGV in _Py_DumpTracebackThreads).
# The dump watches for a hung engine boot; a test has none.
# ---------------------------------------------------------------------------
try:
    import launcher._early_boot as _eb
    _eb.arm_hang_dump = lambda seconds, label="boot": None
except Exception:
    pass


# Resolve the production paths once at module load. We compare against
# these in the guard below; any test that hits these exact paths fails
# the whole test, before the destructive call lands.
_REAL_KANBAN_DB = (Path.home() / ".claude" / "gui_kanban.db").resolve()
_REPO_ROOT = Path(__file__).resolve().parent.parent
_REAL_KANBAN_CONFIG = (_REPO_ROOT / "kanban_config.json").resolve()

# Additional daemon-owned state files under ~/.claude/. Each one is
# constructed (and read) by a daemon component at import time:
#   - gui_message_queues.json  → MessageQueue
#   - gui_active_sessions.json → SessionRegistry
#   - gui_permission_policy.json, gui_ui_prefs.json → PermissionManager
# A test that constructs SessionManager / MessageQueue / PermissionManager
# without redirecting these paths will read from and overwrite the user's
# real state. The guard fails such tests loudly instead of silently
# corrupting state.
_REAL_DAEMON_STATE_FILES = {
    (Path.home() / ".claude" / "gui_message_queues.json").resolve(),
    (Path.home() / ".claude" / "gui_active_sessions.json").resolve(),
    (Path.home() / ".claude" / "gui_permission_policy.json").resolve(),
    (Path.home() / ".claude" / "gui_ui_prefs.json").resolve(),
}
_REAL_FILE_HISTORY_ROOT = (Path.home() / ".claude" / "file-history").resolve()
# Every folder under here is a project in the user's sidebar.
_REAL_CLAUDE_PROJECTS = (Path.home() / ".claude" / "projects").resolve()


def _find_claude_projects_holders():
    """Modules that hold their own copy of ``app.config._CLAUDE_PROJECTS``.

    Read from source so a route that starts importing it is sandboxed
    without anyone remembering to add it here.
    """
    names = {"web.config"}
    for pkg in ("web", "shared", "engine"):
        root = _REPO_ROOT / "src" / pkg
        if not root.is_dir():
            continue
        for py in root.rglob("*.py"):
            try:
                src = py.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            if "_CLAUDE_PROJECTS" not in src:
                continue
            rel = py.relative_to(_REPO_ROOT / "src").with_suffix("")
            parts = list(rel.parts)
            if parts[-1] == "__init__":
                parts.pop()
            names.add(".".join(parts))
    return sorted(names)


_CLAUDE_PROJECTS_HOLDERS = _find_claude_projects_holders()


def _is_production_path(p) -> bool:
    """Return True if *p* refers to a real production file or directory.

    Covers:
      - The kanban SQLite DB
      - The repo-root ``kanban_config.json``
      - Daemon-owned ``gui_*.json`` state files in ``~/.claude/``
      - Anything under ``~/.claude/file-history/`` (per-session backups)

    We compare against the absolute, resolved form so a relative path or
    a symlink that points at the real file still trips the guard. Using
    .resolve() with strict=False so non-existent paths (which are still
    safe to compare) don't blow up — sqlite3.connect() is happy to create
    a new DB if the file doesn't exist yet, which is exactly the case
    we're trying to catch (test code 'creating' the production DB).
    """
    try:
        candidate = Path(str(p)).expanduser()
        # strict=False lets us compare paths whose targets don't yet exist
        candidate = candidate.resolve(strict=False)
    except (OSError, ValueError):
        return False
    if candidate == _REAL_KANBAN_DB or candidate == _REAL_KANBAN_CONFIG:
        return True
    if candidate in _REAL_DAEMON_STATE_FILES:
        return True
    # file-history is a directory tree; treat anything beneath it as protected
    try:
        candidate.relative_to(_REAL_FILE_HISTORY_ROOT)
        return True
    except ValueError:
        return False


# ---------------------------------------------------------------------------
# Flask apps build quickly: one compile per route, not one per test.
#
# ``create_app(testing=True)`` runs for about a third of the suite and over 80%
# of its time was Werkzeug generating and compiling two URL-builder functions
# for each of ~240 routes, identically every time.  The generated function is
# a pure result of the rule text and its parsed parts, and it takes the rule as
# its first argument, so one copy serves every app in the process.  A rule with
# defaults is compiled as usual: its result can depend on a converter's output.
# Everything else (matching, converters, the Map) is built fresh per app.
# ---------------------------------------------------------------------------
def _share_url_builders():
    from werkzeug.routing.rules import Rule
    real = Rule._compile_builder
    if getattr(real, "_vn_shared", False):
        return
    built: dict = {}

    def _compile_builder(self, append_unknown=True):
        if self.defaults:
            return real(self, append_unknown)
        key = (self.rule, tuple(self._trace), append_unknown)
        func = built.get(key)
        if func is None:
            func = built[key] = real(self, append_unknown)
        return func

    _compile_builder._vn_shared = True
    Rule._compile_builder = _compile_builder


try:
    _share_url_builders()
except (ImportError, AttributeError):   # another Werkzeug: compile as usual
    pass


# ---------------------------------------------------------------------------
# Per-test sandbox folders, without pytest's numbered-folder scan.
#
# ``tmp_path_factory.mktemp`` lists the whole base folder to find the next
# number and then rewrites a "<name>current" symlink, every call.  The autouse
# fixtures below made three such folders for every one of ~4,700 tests, all in
# one base folder, so each call got slower as the run went on and every test's
# own ``tmp_path`` scanned the same growing listing.  A counter gives the same
# guarantee (a fresh, empty folder nothing else has used) with one mkdir, and
# these folders live one level down so ``tmp_path`` stops listing them.  They
# are removed with the rest of the base folder, as before.
# ---------------------------------------------------------------------------
_box_numbers = itertools.count()


def _box_path(tmp_path_factory) -> Path:
    """A path for this test that nothing has used.  Not created."""
    return tmp_path_factory.getbasetemp() / "vn-boxes" / str(next(_box_numbers))


@pytest.fixture(autouse=True)
def _org_names_per_test(tmp_path, monkeypatch):
    """Each test gets its own list of org members' names for the publish scan,
    so one test's made-up members never become names another test's scan
    refuses."""
    try:
        from web import git_scanner
    except Exception:
        yield
        return
    monkeypatch.setattr(git_scanner, "ORG_NAMES_FILE", tmp_path / "org_names.json")
    git_scanner._org_cache.update(mtime=None, rule=None)
    yield
    git_scanner._org_cache.update(mtime=None, rule=None)


@pytest.fixture(autouse=True)
def _isolate_daemon_home(request, tmp_path_factory, monkeypatch):
    """Redirect ``Path.home()`` to a per-session tmp dir so daemon-owned state
    files land in a sandbox instead of the user's real ``~/.claude/``.

    Background: ``MessageQueue``, ``PermissionManager``, and ``SessionRegistry``
    each compute their persistence path from ``Path.home() / ".claude" / ...``
    inside ``__init__``. Any test that constructs ``SessionManager()`` without
    explicit fixtures was reading from and writing to the user's real files.
    The ``_block_production_paths`` guard below catches WRITES (and raises),
    but reads silently succeeded — so a queue file polluted by an earlier
    leak kept causing order-dependent failures in ``test_wakeup_handling``
    (queued entries auto-dispatched into a SessionManager with no event loop).

    This fixture eliminates the read path entirely: by the time any daemon
    component constructs its ``_queue_path`` / policy path, ``Path.home()``
    returns a fresh tmp dir, so the loads come back empty.

    The production-path constants in ``_block_production_paths`` are computed
    once at module import (before this fixture runs), so the WRITE guard
    still uses the real production paths as its tripwire.
    """
    fake_home = _box_path(tmp_path_factory)
    # Pre-create directories tests expect under home/. Add to this list
    # rather than skipping the fixture if a test needs a new one.
    # ``.claude/projects`` is iterated by ``app.routes.sessions_api`` —
    # missing it causes order-dependent FileNotFoundError flakes.
    os.makedirs(fake_home / ".claude" / "projects")
    os.makedirs(fake_home / "Downloads")
    monkeypatch.setattr(Path, "home", lambda: fake_home)

    # Sandbox the Windows-specific env vars that Path.home() doesn't cover.
    # reviver.py._startup_dir() reads os.environ["APPDATA"], NOT Path.home(),
    # so monkeypatching Path.home alone would leave a hole. USERPROFILE
    # matches Path.home() on Windows and is inherited by any subprocess a
    # test might spawn — pinning it here means even a subprocess whose Popen
    # slipped past the guard below still cannot resolve to the real user
    # folder. LOCALAPPDATA is deliberately NOT redirected: Playwright reads
    # it to locate its browsers under ``%LOCALAPPDATA%\ms-playwright``, and
    # nothing in the reviver/autostart path uses it.
    fake_appdata = fake_home / "AppData" / "Roaming"
    os.makedirs(fake_appdata / "Microsoft" / "Windows" / "Start Menu" / "Programs" / "Startup")
    monkeypatch.setenv("APPDATA", str(fake_appdata))
    monkeypatch.setenv("USERPROFILE", str(fake_home))
    monkeypatch.setenv("HOME", str(fake_home))
    # A Python a test spawns (the Session Host, a stand-in engine) finds its
    # user-installed packages under HOME (``~/.local``).  With HOME moved it
    # lost them, so on a machine whose packages are user-installed the host
    # could not import the SDK and its tree kill failed ("No module named
    # 'anyio'").  Pin the real user base; it only says where to read packages.
    if _REAL_USER_BASE and not os.environ.get("PYTHONUSERBASE"):
        monkeypatch.setenv("PYTHONUSERBASE", _REAL_USER_BASE)
    if _REAL_PLAYWRIGHT_BROWSERS:
        monkeypatch.setenv("PLAYWRIGHT_BROWSERS_PATH", _REAL_PLAYWRIGHT_BROWSERS)

    # Several daemon modules capture ``Path.home()``-derived paths at IMPORT
    # time (module-level constants), so by the time this per-test fixture
    # runs the values are already baked in to the real ``~/.claude/`` paths.
    # Patch the captured constants too so writes land in the sandbox even
    # when the module was imported before the fixture activated.
    try:
        import engine.session_registry as _sr
        monkeypatch.setattr(_sr, "REGISTRY_PATH",
                            fake_home / ".claude" / "gui_active_sessions.json")
        # Neutralize the debounced save timer for every test EXCEPT the
        # registry's own test file. Background: tests like test_fork_rewind
        # construct SessionManager() (which owns a SessionRegistry) without
        # ever calling stop(). The registry's 3-second timer then fires
        # DURING a later test's fixture setup, racing with the next
        # SessionRegistry's tmp-file write on the same path — surfacing
        # as "[WinError 5] Access is denied" and an ERROR at fixture setup.
        # Replacing the schedule with a no-op is safe in tests: the purpose
        # of the registry (crash recovery) has no value in a test process
        # that owns its own state and exits cleanly. test_session_registry
        # opts out so its debounce tests still exercise the real timer.
        if "test_session_registry" not in request.node.nodeid:
            monkeypatch.setattr(
                _sr.SessionRegistry,
                "schedule_registry_save",
                lambda self, save_fn: None,
            )
    except ImportError:
        pass
    # ``app.config._CLAUDE_PROJECTS`` is bound by value into every module
    # that does ``from ..config import _CLAUDE_PROJECTS``, so patching
    # app.config alone leaves those copies pointing at the REAL folder.
    # Patching only sessions_api let ``POST /api/add-project``
    # (project_api) create empty project folders in the user's real
    # ~/.claude/projects on every suite run, which showed up in the sidebar
    # as projects nobody made. Patch every module that holds a copy.
    for _mod_name in _CLAUDE_PROJECTS_HOLDERS:
        try:
            _mod = importlib.import_module(_mod_name)
        except ImportError:
            continue
        if hasattr(_mod, "_CLAUDE_PROJECTS"):
            monkeypatch.setattr(_mod, "_CLAUDE_PROJECTS",
                                fake_home / ".claude" / "projects")

    # engine.session_manager appends Path.home()-derived paths to
    # os.environ["PATH"] at import time. Tests that reload that module
    # (e.g., the sm_module fixture in test_state_transitions.py) would
    # otherwise extend PATH unboundedly across the session — on Windows
    # the env block overflows at 32767 chars and subsequent reloads
    # raise ValueError. Snapshot/restore PATH so each test starts clean.
    monkeypatch.setenv("PATH", os.environ.get("PATH", ""))
    yield


# ---------------------------------------------------------------------------
# Compose tests never touch the real compose-projects/ folder.
#
# <repo>/compose-projects is where the user's real compositions live, and two
# cleanup fixtures (test_compose_api, test_compose_deferred) delete EVERY
# folder that appears there during a test.  In a parallel run that would
# delete other workers' fixture projects mid-test, and any composition the
# user created while the suite ran.
#
# Each worker process gets its own folder, still named "compose-projects"
# (test_compose_rename_grep_guard checks the name).  The app reads
# models.COMPOSE_PROJECTS_DIR at call time; test modules that imported their
# own copy of the path are patched too, so their snapshot cleanups act on the
# sandbox.  The snapshot cleanups themselves stay (CLAUDE.md, Compose fix 3).
# ---------------------------------------------------------------------------
_compose_sandbox = None


@pytest.fixture(autouse=True)
def _no_machine_stop_add_ons(tmp_path_factory, monkeypatch):
    """The stop add-ons configured on this machine (data/stop_plugins.json)
    belong to private projects: no test sees them unless it sets its own."""
    try:
        from engine import stop_plugins as _sp
    except Exception:
        return
    monkeypatch.setattr(_sp, "CONFIG", tmp_path_factory.getbasetemp() / "no_stop_plugins.json")
    try:
        from engine import tool_guards as _tg
        monkeypatch.setattr(_tg, "CONFIG", tmp_path_factory.getbasetemp() / "no_tool_guards.json")
    except Exception:
        pass


@pytest.fixture(autouse=True)
def _isolate_compose_projects(request, tmp_path_factory, monkeypatch):
    global _compose_sandbox
    try:
        import web.compose.models as _compose_models
    except Exception:
        yield
        return
    if _compose_sandbox is None:
        _compose_sandbox = tmp_path_factory.mktemp("compose") / "compose-projects"
        _compose_sandbox.mkdir()
    real = _compose_models.COMPOSE_PROJECTS_DIR
    monkeypatch.setattr(_compose_models, "COMPOSE_PROJECTS_DIR", _compose_sandbox)
    mod = getattr(request, "module", None)
    if mod is not None and getattr(mod, "COMPOSE_PROJECTS_DIR", None) == real:
        monkeypatch.setattr(mod, "COMPOSE_PROJECTS_DIR", _compose_sandbox)
    yield


# Session Hosts (src/engine/session_host.py) are separate processes that inherit
# this environment: none of them may ever launch the real Claude CLI, only a
# fake one passed as cli_path.  And the host paths point at a scratch folder
# for the WHOLE process, so a SessionManager timer that outlives its test (and
# its per-test sandbox below) can never read the real data/session_hosts.
os.environ["VIBENODE_HOST_FORBID_REAL_CLI"] = "1"
# Hosting is ON by default in production; the suite runs in-process unless a
# test turns it on (per test, below).  Set process-wide too, so an engine a
# test starts in a subprocess, or before the per-test fixture runs, also
# defaults to in-process.
os.environ["VIBENODE_SESSION_HOST"] = "0"
# Smart termination checks that a summary's own-app links load, against this
# checkout's src/web/static/ and data/previews/.  Test cards use made-up addresses
# ("/static/scratch/x.png"), and src/web/static/scratch is gitignored, so the check is
# off for the suite; tests/test_broken_links.py turns it on per test.
os.environ["VIBENODE_LINK_CHECK"] = "0"
# The org members' names the publish scan refuses (src/web/git_scanner.py):
# tests write made-up members, never into the real list. The publish-scan test
# reads the real list itself.
os.environ["VIBENODE_ORG_NAMES_FILE"] = os.path.join(tempfile.gettempdir(), "vibenode-test-org-names.json")
# The owner's saved tailnet login (src/web/mobile_auth.py) must never decide a
# test: tests send made-up logins. Point it at a file that does not exist.
os.environ["VIBENODE_TAILNET_OWNER_LEARN"] = "0"
os.environ["VIBENODE_OWNER_FILE"] = os.path.join(tempfile.gettempdir(), "vibenode-test-no-owner.json")
# A passing summary saves copies of its pictures (src/shared/card_pictures.py);
# the suite's made-up summaries must never write into data/card_pictures.
# tests/test_card_pictures.py points it at a temp folder per test.
os.environ["VIBENODE_CARD_PICTURES"] = "0"
# The suite's own tests of the publish gate must never find a real passing-run
# record and skip the run they are testing (src/web/gate_reuse.py); nested pytest
# runs the suite starts must not write records either.  tests/test_gate_reuse.py
# turns it back on for itself.
_REUSE_OFF_BY_CALLER = os.environ.get("VIBENODE_TEST_REUSE") == "0"  # the caller's wish, before ours
os.environ["VIBENODE_TEST_REUSE"] = "0"
# Smart termination refuses a card the self-check (src/engine/handoff_check.py)
# did not pass.  The suite's cards are written in tests, never checked, so the
# requirement is off here; tests/test_handoff_check.py turns it on per test.
os.environ["VIBENODE_HANDOFF_SELF_CHECK"] = "0"
# The write-guard's "don't copy the user's own words" check reads the real
# session transcript; off here so it never false-fires on unrelated tests.
# tests/test_publish_guard.py turns it on per test with a fake transcript.
os.environ["VIBENODE_USER_WORDS_GUARD"] = "0"
try:
    import tempfile as _tempfile
    from engine import session_host_protocol as _shp_mod
    _shp_scratch = Path(_tempfile.mkdtemp(prefix="vn-test-hosts-"))
    _shp_mod.HOSTS_DIR = _shp_scratch / "session_hosts"
    _shp_mod.LOGS_DIR = _shp_scratch / "session_host_logs"
except Exception:
    pass

# No Session Host outlives the test that started it, however that test ends.
# A host is built to survive its parent (on Windows it even breaks away from
# the parent's job), so three backstops, each covering what the one before
# misses:
#
# 1. ``_isolate_engine_data_dir`` shuts down every host with a record in the
#    test's sandbox (the polite path: the host kills its own CLI tree).
# 2. Every ``spawn_host`` call is tracked here by (pid, create time); the same
#    finalizer tree-kills any the test started that are still alive, record or
#    not, and ``atexit`` kills whatever a leaked engine timer started later.
# 3. ``VIBENODE_HOST_TEST_OWNER`` names this pytest process; each host exits
#    by itself once it is gone (``Host._test_owner_watch``), which is the only
#    thing that still works when pytest itself is killed.
_SPAWNED_HOSTS: list = []        # [(pid, create_time)] in spawn order
try:
    from engine import session_host_protocol as _shp_own
    os.environ["VIBENODE_HOST_TEST_OWNER"] = (
        f"{os.getpid()}:{_shp_own.process_create_time(os.getpid()) or 0.0}")
except Exception:
    pass
try:
    from engine import session_host_transport as _sht_mod
    _real_spawn_host = _sht_mod.spawn_host

    def _tracked_spawn_host(*args, **kwargs):
        proc = _real_spawn_host(*args, **kwargs)
        from engine import session_host_protocol as _p
        _SPAWNED_HOSTS.append((proc.pid, _p.process_create_time(proc.pid)))
        return proc

    _sht_mod.spawn_host = _tracked_spawn_host
except Exception:
    pass


def _kill_spawned_hosts(since: int = 0) -> None:
    """Tree-kill every tracked host spawned at index ``since`` or later that
    is provably still that process (pid + create time; never a bare pid)."""
    try:
        from engine import session_host_protocol as _p
        from engine.session_manager import SessionManager as _SM
    except Exception:
        return
    for pid, ct in list(_SPAWNED_HOSTS[since:]):
        if ct and _p.same_process(pid, ct):
            try:
                _SM._kill_process_tree(pid)
            except Exception:
                pass


import atexit as _atexit  # noqa: E402
_atexit.register(_kill_spawned_hosts)


@pytest.fixture(autouse=True)
def _forbid_real_auth_login(request, monkeypatch):
    """No test may open a real ``claude auth login`` window.

    ``/api/auth-login`` starts the Claude CLI's login in a visible terminal,
    which opens a browser sign-in page on the desktop. The route's Popen is
    replaced with one that fails the test.
    """
    try:
        from web.routes import auth_api
    except Exception:
        yield
        return

    import subprocess as _real_subprocess
    import types

    def _refuse(*args, **kwargs):
        raise AssertionError(
            f"TEST {request.node.nodeid} tried to launch `claude auth login` for real. "
            "Check the route's guard with a test_request_context instead.")

    # Replace the module auth_api sees, not subprocess itself: every other
    # caller of subprocess.Popen in the process is untouched.
    fake = types.SimpleNamespace(**{k: getattr(_real_subprocess, k)
                                    for k in dir(_real_subprocess) if not k.startswith("__")})
    fake.Popen = _refuse
    monkeypatch.setattr(auth_api, "subprocess", fake)
    yield


@pytest.fixture(autouse=True)
def _isolate_permission_policy_log(tmp_path_factory, monkeypatch):
    """No test writes the real ``logs/permission_policy.log``.

    It is the record of who changed the permission policy; a ws_events test
    sends a policy change through a mock engine, which would bury real
    changes under test lines.
    """
    try:
        import web.routes.ws_events as _ws
        monkeypatch.setattr(_ws, "_POLICY_CHANGE_LOG",
                            _box_path(tmp_path_factory) / "permission_policy.log")
    except Exception:
        pass
    yield


@pytest.fixture(autouse=True)
def _isolate_engine_data_dir(tmp_path_factory, monkeypatch):
    """Point the engine's per-session durable state at a sandbox.

    ``src/engine/session_durable.py`` (``DURABLE_DIR``) and the engine alias index
    (``src/shared/session_names.ENGINE_ALIASES_PATH``) live under ``<repo>/data``,
    not under home, so ``_isolate_daemon_home`` does not cover them.  Without
    this, the suite leaves test records in ``data/session_durable`` and writes
    ``temp-x -> real-x`` into the real alias file, which a later
    ``SessionManager.start()`` restores into ``_id_aliases``, so the init-time
    remap test finds its remap "already done" and fails.  Sandboxed here,
    the real files are never read or written by a test.
    """
    box = _box_path(tmp_path_factory)
    os.makedirs(box)
    try:
        from engine import session_durable as _sd
        monkeypatch.setattr(_sd, "DURABLE_DIR", box / "session_durable")
    except Exception:
        pass
    try:
        from shared import session_names as _sn
        monkeypatch.setattr(_sn, "ENGINE_ALIASES_PATH", box / "session_aliases.json")
    except Exception:
        pass
    # Session Host records, spill files and logs (src/engine/session_host_protocol.py).
    # A test that wants hosted sessions turns VIBENODE_SESSION_HOST on itself;
    # everything else runs in-process exactly as before.
    _shp = None
    try:
        from engine import session_host_protocol as _shp
        monkeypatch.setattr(_shp, "HOSTS_DIR", box / "session_hosts")
        monkeypatch.setattr(_shp, "LOGS_DIR", box / "session_host_logs")
    except Exception:
        pass
    monkeypatch.setenv("VIBENODE_SESSION_HOST", "0")
    spawned_before = len(_SPAWNED_HOSTS)
    yield
    # No host process outlives its test (each one runs a fake CLI): first the
    # polite shutdown of every recorded host, then a tree kill of any host
    # this test spawned that is still alive (see _SPAWNED_HOSTS above).
    if _shp is not None and (box / "session_hosts").is_dir():
        try:
            from tests._session_host_helpers import reap_hosts
            reap_hosts(box / "session_hosts")
        except Exception:
            pass
    if len(_SPAWNED_HOSTS) > spawned_before:
        _kill_spawned_hosts(spawned_before)
    # A debounced write (session_durable.save(immediate=False)) that fires
    # after monkeypatch restores DURABLE_DIR would land in the real data dir.
    try:
        from engine import session_durable as _sd
        _sd.cancel_all()
    except Exception:
        pass


@pytest.fixture(autouse=True)
def _block_production_paths(request, monkeypatch):
    """Hard-fail any test that tries to read/write the production DB or config.

    Wraps ``sqlite3.connect`` and ``Path.write_text``. Tests that legitimately
    need to touch ``~/.claude/gui_kanban.db`` or ``<repo>/kanban_config.json``
    don't exist — every code path through the kanban API must be redirectable
    via the ``kanban_app`` fixture (tmp DB) or by monkeypatching
    ``app.config._KANBAN_CONFIG_FILE``. If a test legitimately needs a
    different production path (very unlikely), it can opt out per-test by
    requesting this fixture and overriding it.

    This fixture cannot be opted out of globally — that's the whole point.
    """
    real_connect = sqlite3.connect

    def _guarded_connect(database, *args, **kwargs):
        if _is_production_path(database):
            raise RuntimeError(
                f"\n\nTEST {request.node.nodeid} tried to open the PRODUCTION "
                f"kanban DB at:\n    {database}\n\n"
                f"This is a test-isolation bug. The kanban_app fixture "
                f"provides a tmp_path SQLite repo — use it. If you're "
                f"calling /api/kanban/migrate or /api/kanban/migrate/preflight "
                f"with target=sqlite, monkeypatch SqliteRepository to use a "
                f"tmp path, or spy out BackendMigrator.switch_backend so the "
                f"destructive call never lands.\n"
            )
        return real_connect(database, *args, **kwargs)

    monkeypatch.setattr(sqlite3, "connect", _guarded_connect)

    real_write_text = Path.write_text

    def _guarded_write_text(self, *args, **kwargs):
        if _is_production_path(self):
            raise RuntimeError(
                f"\n\nTEST {request.node.nodeid} tried to write the PRODUCTION "
                f"kanban_config.json at:\n    {self}\n\n"
                f"Use monkeypatch on app.config._KANBAN_CONFIG_FILE to "
                f"redirect to a tmp_path before invoking save_kanban_config "
                f"or any endpoint that calls it (/migrate, /projects/alias).\n"
            )
        return real_write_text(self, *args, **kwargs)

    monkeypatch.setattr(Path, "write_text", _guarded_write_text)

    real_mkdir = Path.mkdir

    def _guarded_mkdir(self, *args, **kwargs):
        try:
            target = Path(str(self)).resolve(strict=False)
            inside = target != _REAL_CLAUDE_PROJECTS and \
                target.is_relative_to(_REAL_CLAUDE_PROJECTS)
        except (OSError, ValueError):
            inside = False
        if inside:
            raise RuntimeError(
                f"\n\nTEST {request.node.nodeid} tried to create a folder in "
                f"the REAL Claude projects directory:\n    {self}\n\n"
                f"Every folder there shows up as a project in the user's "
                f"sidebar. A module bound ``_CLAUDE_PROJECTS`` by value and "
                f"was not sandboxed; see _CLAUDE_PROJECTS_HOLDERS.\n"
            )
        return real_mkdir(self, *args, **kwargs)

    monkeypatch.setattr(Path, "mkdir", _guarded_mkdir)
    yield


# ---------------------------------------------------------------------------
# Reviver / autostart spawn guard
# ---------------------------------------------------------------------------
# Command-substring markers that must never appear in any subprocess launched
# by a test. If a test needs to *reference* one of these in a string constant
# (e.g. the leak-detector allowlist tests), that's fine — the guard only fires
# on actual subprocess invocations.
_FORBIDDEN_SUBPROCESS_MARKERS = (
    "reviver.py",       # reviver launch: register/rewrite autostart
    "schtasks.exe",     # scheduled-task register/delete on Windows
    "schtasks ",        # bare schtasks (via shell=True)
)


def _command_string(cmd) -> str:
    """Best-effort rendering of a Popen ``args`` argument to a searchable str."""
    if cmd is None:
        return ""
    if isinstance(cmd, (list, tuple)):
        try:
            return " ".join(str(x) for x in cmd)
        except Exception:  # noqa: BLE001
            return repr(cmd)
    return str(cmd)


# The real Claude CLI, found before any fixture moves HOME or PATH.
import shutil as _shutil  # noqa: E402
_REAL_CLAUDE = {os.path.realpath(p) for p in filter(None, (_shutil.which("claude"), _shutil.which("claude.exe")))}


def _is_real_claude(cmd) -> bool:
    """True if ``cmd`` starts the machine's real Claude CLI.  A test's own stand-in
    named ``claude`` (a script in a temp folder) is not it.  A test's background
    thread that outlives its mock would otherwise make a real model call."""
    first = cmd[0] if isinstance(cmd, (list, tuple)) and cmd else (cmd.split()[0] if isinstance(cmd, str) and cmd.split() else "")
    first = str(first).strip('"').strip("'")
    if not first or not _REAL_CLAUDE:
        return False
    # By where it really lives: the installed CLI is a link to a versioned file.
    found = _shutil.which(first) or first
    return os.path.realpath(found) in _REAL_CLAUDE


def _is_forbidden_command(cmd_str: str) -> bool:
    low = cmd_str.lower()
    # Match reviver.py either as a bare token or with a path prefix. Match
    # schtasks either bare or with .exe. Matching on substring is safe here
    # because these tokens don't appear inside legitimate helper commands
    # tests do run (git, pytest, playwright, ffmpeg, etc.).
    if "reviver.py" in low:
        return True
    # schtasks.exe or schtasks with a following space/end.
    if "schtasks.exe" in low:
        return True
    # Bare "schtasks" as a full path component or as arg 0. Wrap shlex.split
    # in a try/except: exotic quoting can raise ValueError, and we'd rather
    # miss an edge case than mask a legitimate command with a parse error.
    try:
        tokens = shlex.split(cmd_str, posix=False) if cmd_str else []
    except ValueError:
        tokens = cmd_str.split()
    for tok in tokens:
        base = os.path.basename(tok.strip('"').strip("'")).lower()
        if base in ("schtasks", "schtasks.exe"):
            return True
    return False


@pytest.fixture(autouse=True)
def _forbid_reviver_and_autostart_spawn(request, monkeypatch):
    """Hard-fail any test that tries to spawn the real reviver or schtasks.

    Wraps ``subprocess.Popen`` (which ``subprocess.run`` also routes through
    on CPython) and raises ``AssertionError`` if the command touches
    ``reviver.py`` or ``schtasks``. This is the belt-and-suspenders layer:
    ``test_mobile_command.py`` already mocks ``_spawn_reviver`` /
    ``_retire_reviver`` in its own ``mc`` fixture. This guard exists so a
    future test — or a future refactor that reintroduces the direct spawn
    path — cannot silently start deleting production autostart again.

    A test that legitimately needs to test reviver-related string handling
    (allowlist regex, leak detector) is unaffected: the guard only fires on
    the actual subprocess call, not on string literals or in-process
    function calls.
    """
    real_popen = subprocess.Popen

    allow_claude = request.node.get_closest_marker("allow_real_claude_cli") is not None

    def _guarded_popen(cmd, *args, **kwargs):
        if not allow_claude and _is_real_claude(cmd):
            raise AssertionError(
                f"TEST {request.node.nodeid} (or a thread it left running) tried to start "
                f"the real Claude CLI: {_command_string(cmd)}. Mock it, and make sure no "
                f"background thread outlives the mock.")
        rendered = _command_string(cmd)
        if _is_forbidden_command(rendered):
            raise AssertionError(
                f"\n\nTEST {request.node.nodeid} tried to spawn a forbidden "
                f"subprocess:\n    {rendered}\n\n"
                f"This is the reviver / autostart safety guard. The command "
                f"contains 'reviver.py' or 'schtasks', either of which can "
                f"register, rewrite, or DELETE the production "
                f"\\\\VibeNodeReviver_<id> scheduled task and Startup VBS.\n\n"
                f"If this call originated from mobile_command.enable() / "
                f".disable(), your test needs to monkeypatch "
                f"web.mobile_command._spawn_reviver and _retire_reviver in "
                f"its fixture — see the `mc` fixture in test_mobile_command.py.\n\n"
                f"If this is a new legitimate use case, extend "
                f"_FORBIDDEN_SUBPROCESS_MARKERS in tests/conftest.py, or add "
                f"a narrow opt-out. Do NOT weaken the guard globally.\n"
            )
        return real_popen(cmd, *args, **kwargs)

    monkeypatch.setattr(subprocess, "Popen", _guarded_popen)
    yield


# ═══════════════════════════════════════════════════════════════════════════
# REAL CLAUDE CLI GUARD + FAKE-SDK BACKEND ISOLATION
# ═══════════════════════════════════════════════════════════════════════════
#
# Many daemon test files build a SessionManager inside
# ``patch.dict('sys.modules', <fake claude_code_sdk>)`` and believe it then
# drives fake clients.  Since the backend refactor, SessionManager() gets its
# client from ``engine.backends.claude``, imported lazily.  That module binds
# to the fake SDK ONLY if it is first imported inside the patch.  If any
# earlier test module imported the real backend (test_claude_normalization
# does, at load time), every later "mocked" SessionManager in the run quietly
# launched the REAL ``claude`` CLI: slow (a real process per session), flaky
# (real output such as "Not logged in"), and the source of order-dependent
# failures that passed when each file ran alone.
#
# Two layers, same pattern as the guards above:
#   1. _isolate_claude_backend_for_fake_sdk: for tests that use a
#      ``mock_sdk_types`` fixture, hide the cached real backend so the
#      SessionManager re-imports one bound to the fake, then put the real one
#      back so later tests are unaffected.
#   2. _forbid_real_claude_cli: hard-fail any test that reaches the real CLI
#      transport, naming the test.  Opt out with
#      ``@pytest.mark.allow_real_claude_cli`` (no test needs it today).

try:
    from claude_code_sdk._internal.transport import subprocess_cli as _real_cli_transport
except Exception:  # SDK missing: nothing can launch it, so nothing to guard
    _real_cli_transport = None


@pytest.fixture(autouse=True)
def _isolate_claude_backend_for_fake_sdk(request):
    if "mock_sdk_types" not in request.fixturenames:
        yield
        return
    import engine.backends as _backends_pkg
    saved_mod = sys.modules.pop("engine.backends.claude", None)
    had_attr = "claude" in vars(_backends_pkg)
    saved_attr = vars(_backends_pkg).get("claude")
    try:
        yield
    finally:
        sys.modules.pop("engine.backends.claude", None)
        if saved_mod is not None:
            sys.modules["engine.backends.claude"] = saved_mod
        if had_attr:
            _backends_pkg.claude = saved_attr
        elif "claude" in vars(_backends_pkg):
            delattr(_backends_pkg, "claude")


# ---------------------------------------------------------------------------
# No test may ever signal "everything".
#
# A MagicMock handed to the daemon as a CLI pid can be coerced to pid 1,
# resolve process group 1 and reach os.killpg(1, ...), which is kill(-1):
# every process the user owns, including the desktop session and every
# running agent session.
#
# The daemon guards against it (SessionManager._kill_process_tree), and this is
# the backstop for the next bug of that shape.  It is installed at import time
# for the whole pytest process, NOT as a fixture, so no teardown ordering or
# background thread can ever run a test's kill call without it.
# ---------------------------------------------------------------------------
def _install_signal_safety_net():
    if getattr(os, "_vn_signal_net", False):
        return
    _real_kill = os.kill
    _real_killpg = getattr(os, "killpg", None)

    def _safe_kill(pid, sig, *a, **k):
        # 0 = our own process group (the pytest run itself), -1 = everything,
        # 1 = init.  A non-int is a mock leaking into a real syscall.
        # (Signal 0 to pid 1 is only an existence probe and is let through.)
        if type(pid) is not int or pid <= 0 or (pid == 1 and sig != 0):
            raise RuntimeError(
                f"tests/conftest.py blocked os.kill({pid!r}, {sig!r}): tests "
                f"may only signal a real, specific process id.")
        return _real_kill(pid, sig, *a, **k)

    def _safe_killpg(pgid, sig, *a, **k):
        if type(pgid) is not int or pgid <= 1:
            raise RuntimeError(
                f"tests/conftest.py blocked os.killpg({pgid!r}, {sig!r}): "
                f"that would signal every process this user owns.")
        try:
            if pgid == os.getpgid(0):
                raise RuntimeError(
                    f"tests/conftest.py blocked os.killpg({pgid!r}, {sig!r}): "
                    f"that is the test run's own process group.")
        except OSError:
            pass
        return _real_killpg(pgid, sig, *a, **k)

    os.kill = _safe_kill
    if _real_killpg is not None:
        os.killpg = _safe_killpg
    os._vn_signal_net = True


_install_signal_safety_net()


# ---------------------------------------------------------------------------
# Every run of this suite puts ITSELF in a PID namespace (Linux).
#
# The guard in the daemon and the safety net above each stop the kill(-1)
# bug.  This makes the whole class of bug harmless: inside a PID namespace a
# broadcast signal (kill(-1), killpg(1)) can only reach processes in that
# namespace, i.e. this test run.  The desktop session, VibeNode and every
# agent session are not in it and cannot be signalled from it, whatever a test
# or the code under test does.  Checked with a harmless SIGCONT broadcast: a
# canary outside the namespace never received it.
#
# It is done HERE, not left to whoever starts pytest, because the suite is
# started from many places (the UI's pre-publish test run, agent sessions,
# a terminal), most often as a plain `pytest`.
#
# How: on the first pytest_configure, re-exec the same command line under
# `unshare`.  `bash` is pid 1 in the namespace (an init that reaps orphans;
# with pytest itself as pid 1 the process-group tests fail), `setsid -w` gives
# pytest its own process group, and `--kill-child` takes the namespace down if
# the outer process is killed (a cancelled or timed-out run leaves nothing).
# Where `unshare` is missing or not permitted the run continues unsandboxed
# behind the two guards above and says so.  Opt out with
# VIBENODE_TEST_NO_SANDBOX=1.
# ---------------------------------------------------------------------------
_SANDBOX_ENV = "VIBENODE_TEST_SANDBOXED"
_SANDBOX_CMD = ["unshare", "-U", "--map-current-user", "-p", "-f",
                "--mount-proc", "--kill-child"]


def _sandbox_available() -> bool:
    import shutil
    if not sys.platform.startswith("linux"):
        return False
    if not (shutil.which("unshare") and shutil.which("bash") and shutil.which("setsid")):
        return False
    try:
        return subprocess.run(
            _SANDBOX_CMD + ["true"], stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL, timeout=10,
        ).returncode == 0
    except Exception:
        return False


@pytest.hookimpl(tryfirst=True)
def pytest_configure(config):
    global _REUSE_OFF_BY_CALLER
    # An xdist worker inherits the controller's environment AFTER the
    # controller set VIBENODE_TEST_REUSE=0 for itself, so its own reading of
    # "the caller's wish" is always "off".  Take the controller's reading.
    winput = getattr(config, "workerinput", None)
    if winput and "vn_reuse_off_by_caller" in winput:
        _REUSE_OFF_BY_CALLER = bool(winput["vn_reuse_off_by_caller"])
    if os.environ.get(_SANDBOX_ENV) or os.environ.get("VIBENODE_TEST_NO_SANDBOX"):
        return
    if hasattr(config, "workerinput"):      # pytest-xdist worker: the controller is sandboxed
        return
    if not sys.platform.startswith("linux"):
        return
    if not _sandbox_available():
        sys.stderr.write(
            "[conftest] WARNING: could not enter a PID namespace (unshare "
            "missing or not permitted); running unsandboxed behind the "
            "kill guards.\n")
        return
    env = dict(os.environ)
    env[_SANDBOX_ENV] = "1"
    args = list(config.invocation_params.args)
    cmd = _SANDBOX_CMD + [
        "bash", "-c", 'setsid -w "$@"; exit $?', "bash",
        sys.executable, "-m", "pytest", *args,
    ]
    # Capture is suspended at this point, so fds 1/2 are the caller's own and
    # the re-exec'd run reports to the same place.
    sys.stdout.flush()
    sys.stderr.flush()
    os.chdir(str(config.invocation_params.dir))
    os.execvpe(cmd[0], cmd, env)


# ---------------------------------------------------------------------------
# Parallel runs: slowest work first (see tests/_xdist_schedule.py).
#
# Runs used to reach ~98% fast and then stall: xdist queued the few-test,
# slow browser files last, and each then ran alone while the other workers
# idled.  The controller records how long every test took into pytest's cache
# (.pytest_cache, gitignored) and the scheduler queues the next run's files
# slowest first.  Only order and placement change, never which tests run.
# ---------------------------------------------------------------------------
_test_durations: dict = {}


# ---------------------------------------------------------------------------
# A plain ``pytest`` of the whole suite runs in parallel.
#
# The Publish gate passes ``-n``; a plain ``pytest`` (an agent session, the Test
# Team, a terminal) ran every test in one process, about seven times longer,
# because ``-n`` is opt-in.  It is decided here, like the PID namespace above,
# because the suite is started from many places.  Same tests, same scheduler
# as the gate.  A run of chosen files, or with ``-n``, ``-s``, ``--pdb`` or
# ``--collect-only``, is left as asked.  VIBENODE_TEST_SERIAL=1 opts out.
# ---------------------------------------------------------------------------
def _whole_suite_requested(config) -> bool:
    tests_dir = Path(__file__).resolve().parent
    return all(Path(p).resolve() == tests_dir for p in config.args)


@pytest.hookimpl(tryfirst=True)
def pytest_cmdline_main(config):
    if hasattr(config, "workerinput") or os.environ.get("VIBENODE_TEST_SERIAL"):
        return
    opt = config.option
    if (not config.pluginmanager.hasplugin("xdist")
            or getattr(opt, "numprocesses", None) is not None
            or getattr(opt, "tx", None)
            or getattr(opt, "dist", "no") != "no"
            or opt.collectonly or opt.usepdb or opt.capture == "no"):
        return
    if not _whole_suite_requested(config):
        return
    from shared.test_workers import fork_worker_count, worker_count
    chosen = os.environ.get("VIBENODE_TEST_WORKERS")
    if sys.platform.startswith("linux") and not os.environ.get("VIBENODE_TEST_XDIST"):
        config._vn_fork_workers = int(chosen or fork_worker_count())    # tests/_fork_runner.py
        return
    opt.numprocesses = int(chosen or worker_count())
    opt.dist = "loadfile"


@pytest.hookimpl(tryfirst=True)
def pytest_runtestloop(session):
    workers = getattr(session.config, "_vn_fork_workers", None)
    if not workers or not session.items or session.testsfailed:
        return None     # pytest's own loop: nothing to fork for, or it reports the collection errors
    from tests._fork_runner import run_session
    return True if run_session(session, workers) else None


# ---------------------------------------------------------------------------
# The garbage collector does not re-scan what collection built.
#
# Collection leaves hundreds of thousands of long-lived objects in every worker
# (items, marks, fixture tables), and each full collection during the run walks
# them all.  Freezing them after collection takes them out of the scan; only the
# timing changes, an explicit ``gc.collect()`` in a test still works on everything
# the test made.  A run that collects nothing (``--help``) ends with it off, which
# changes nothing.
# ---------------------------------------------------------------------------
gc.disable()    # until collection is over: it allocates and frees nothing worth scanning


def pytest_collection_finish(session):
    gc.enable()
    gc.collect()
    gc.freeze()
    # After the freeze the long-lived generation is nearly empty, and CPython
    # runs a full collection whenever pending objects exceed a quarter of it:
    # almost every time the middle generation fills.  Measured: 4.6 of
    # the 7.1 setup seconds in the app-fixture files.  Young collections stay as
    # default; full ones run about 100 times less often.
    gc.set_threshold(700, 10, 1000)


def pytest_runtest_logreport(report):
    # setup + call + teardown, so a slow fixture counts as the test's cost.
    _test_durations[report.nodeid] = _test_durations.get(report.nodeid, 0.0) + report.duration


def pytest_sessionfinish(session, exitstatus):
    from tests import _browser
    _browser.close_shared_browser()
    config = session.config
    if hasattr(config, "workerinput"):
        return
    _record_passing_run(session, exitstatus)
    cache = getattr(config, "cache", None)
    if cache is None or not _test_durations:
        return
    from tests._xdist_schedule import DURATIONS_CACHE_NAME
    # Merge, so running one file does not forget the rest of the suite.
    merged = dict(cache.get(DURATIONS_CACHE_NAME, {}) or {})
    merged.update({k: round(v, 3) for k, v in _test_durations.items()})
    cache.set(DURATIONS_CACHE_NAME, merged)


def _record_passing_run(session, exitstatus):
    """A whole-suite run that passed is recorded with the working tree's
    fingerprint (src/web/gate_reuse.py), so a publish on identical files can
    reuse it instead of running again.  Never when the caller set
    VIBENODE_TEST_REUSE=0 (the suite sets it for ITSELF above, so nested
    runs never record; that must not silence this process's own record),
    never for a partial run, never on failure."""
    if exitstatus != 0 or _REUSE_OFF_BY_CALLER:
        return
    try:
        from web import gate_reuse
        scope = gate_reuse.scope_of(session.config)
        if not scope["full"]:
            return
        tr = session.config.pluginmanager.get_plugin("terminalreporter")
        stats = getattr(tr, "stats", {}) or {}
        n = lambda k: len(stats.get(k, []))  # noqa: E731
        if n("failed") or n("error"):
            return
        ab_covered = (not scope["ab_ignored"]) and all(
            any(k.startswith(f + "::") for k in _test_durations) for f in gate_reuse.AB_TESTS)
        gate_reuse.record_run(
            fp=gate_reuse.fingerprint(), passed=n("passed"), skipped=n("skipped"),
            failed=0, seconds=time.time() - _session_started, ab_covered=ab_covered,
            source=os.environ.get("VIBENODE_TEST_SOURCE", "pytest"),
            command=" ".join(str(a) for a in session.config.invocation_params.args),
            ab_reused_from=os.environ.get("VIBENODE_TEST_AB_REUSED") or None)
    except Exception as e:  # recording is a convenience; never fail a passing run
        sys.stderr.write(f"[conftest] could not record the passing run: {e}\n")


_session_started = time.time()


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_teardown(item, nextitem):
    # Close this worker's shared browser (tests/_browser.py) as soon as the
    # next test is not a browser test, so no Playwright event loop is alive
    # while other tests run.  Runs after the item's own fixture teardown.
    yield
    from tests import _browser
    if nextitem is None or not _browser.uses_shared_browser(getattr(nextitem, "module", None)):
        _browser.close_shared_browser()


# ---------------------------------------------------------------------------
# One parallel run on this machine at a time (see tests/_run_lock.py).
#
# Two overlapping 8-worker runs slowed each other several times over
# (measured: 394s quiet vs 557-1,447s overlapping).  The xdist controller of
# a parallel run waits here, before any worker starts, for any other parallel
# run to finish.  xdist starts its workers in its own trylast sessionstart, so
# this runs first.  A run with no -n never waits.
# ---------------------------------------------------------------------------
_run_lock = None


def _unlock_leftover_trial_folders(root=None, min_age: float = 900) -> int:
    """Make listable again every Controlled A/B Test trial parent a killed
    worker left locked in pytest's own temp area.

    The A/B tests lock each run's trial parent against listing
    (src/features/experiments/hideaway.py) and their ``lab`` fixture unlocks it in teardown.  A
    worker killed mid-test (pytest-timeout, a crash) never reaches teardown, so
    pytest could never delete that folder and would print a block of rm_rf
    warnings on every later run.  Only folders named like a trial run
    (``work/mul-test-*``) under pytest's ``pytest-of-<user>`` root are touched,
    and only when they cannot be listed and have not changed for
    ``min_age`` seconds: another run on this machine may be inside a test
    that locks one on purpose right now, and no test lives past the 120 s
    timeout.  Returns how many were unlocked."""
    import tempfile
    import time as _time
    from pathlib import Path as _P
    if root is None:
        try:
            from _pytest.tmpdir import get_user
            temproot = _P(os.environ.get("PYTEST_DEBUG_TEMPROOT") or tempfile.gettempdir())
            root = temproot / f"pytest-of-{get_user() or 'unknown'}"
        except Exception:
            return 0
    root = _P(root)
    if not root.is_dir():
        return 0
    try:
        from features.experiments import hideaway
    except Exception:
        return 0
    n = 0
    # <numbered or garbage dir>/[popen-gwN/]<test dir>/work/mul-test-*
    for pattern in ("*/*/work/mul-test-*", "*/*/*/work/mul-test-*"):
        for d in root.glob(pattern):
            try:
                if (d.is_dir() and _time.time() - d.stat().st_mtime >= min_age
                        and not hideaway.listable(d) and hideaway.unhide(d)):
                    n += 1
            except Exception:
                pass
    return n


def pytest_sessionstart(session):
    global _run_lock
    config = session.config
    if hasattr(config, "workerinput"):
        return
    # Before any worker exists and before pytest prunes its old temp folders.
    if not config.getoption("basetemp", None):
        _unlock_leftover_trial_folders()
    try:
        workers = config.getoption("numprocesses")
    except ValueError:  # pytest-xdist not installed
        return
    from tests import _run_lock as rl
    if not workers or not rl.enabled():
        return
    # A pytest started BY a test of this run (the scheduler and lock tests
    # start small parallel runs) is part of this run, not a rival: it would
    # wait on its own parent's lock until the test timed out.
    if os.environ.get(_LOCK_HELD_ENV):
        return
    lock = rl.RunLock()
    if lock.acquire(rl.max_wait()):
        _run_lock = lock
        os.environ[_LOCK_HELD_ENV] = str(os.getpid())  # inherited by every child


_LOCK_HELD_ENV = "VIBENODE_TEST_RUN_LOCK_HELD"


def pytest_unconfigure(config):
    global _run_lock
    if _run_lock is not None:
        _run_lock.release()
        _run_lock = None
        os.environ.pop(_LOCK_HELD_ENV, None)


@pytest.hookimpl(optionalhook=True)
def pytest_xdist_make_scheduler(config, log):
    if config.getvalue("dist") != "loadfile":
        return None
    from tests._xdist_schedule import DurationOrderedLoadFileScheduling
    return DurationOrderedLoadFileScheduling(config, log)


@pytest.hookimpl(optionalhook=True)
def pytest_configure_node(node):
    # Hand the controller's reading of VIBENODE_TEST_REUSE to each worker
    # (see pytest_configure): the worker cannot see the caller's value.
    node.workerinput["vn_reuse_off_by_caller"] = _REUSE_OFF_BY_CALLER


# ---------------------------------------------------------------------------
# Transient worker crash: retry the crashed test ONCE on another worker.
#
# A worker can crash with a native Windows access violation inside
# ``subprocess._wait`` on Python 3.14 ARM64 under heavy concurrent subprocess
# load (seen in test_code_pipeline_review.py::test_attest_cli: the
# test spawns ~140 git subprocess calls, four workers run splittable pipeline
# chunks in parallel, and once in a while one of them hits the native crash).
# The test is correct and passes on a less-parallel run; the crash is the
# interpreter's problem, not the test's.
#
# ``LoadScopeScheduling.remove_node`` already re-queues the whole workload
# (including the crashed item) when a worker dies, so another worker will
# pick the test up and run it again.  But ``DSession.handle_crashitem`` logs
# a ``TestReport(outcome="failed", when="???")`` right after, which the junit
# XML writer records as an error and bumps the exit code to 1 before the
# retry has a chance to run.  This hook marks a FIRST crash of a given test
# as ``outcome="rerun"`` so pytest does not count it as a failure; the actual
# retry's report (passed or failed) is what stands.  A SECOND crash of the
# same test is left as a real failure, so a genuinely-broken test still
# surfaces.
#
# Kill switch: ``VIBENODE_NO_CRASH_RETRY=1``.
# ---------------------------------------------------------------------------
from tests._crash_retry import pytest_handlecrashitem  # noqa: E402,F401  (lives apart so a nested run can load it alone)


@pytest.fixture(autouse=True)
def _forbid_real_claude_cli(request, monkeypatch):
    if _real_cli_transport is None or request.node.get_closest_marker("allow_real_claude_cli"):
        yield
        return
    attempts = []
    nodeid = request.node.nodeid

    async def _blocked_connect(self, *args, **kwargs):
        attempts.append(1)
        raise AssertionError(
            f"TEST {nodeid} tried to launch the real Claude CLI. Tests must "
            f"drive a fake SDK client; see the guard notes in tests/conftest.py.")

    monkeypatch.setattr(_real_cli_transport.SubprocessCLITransport, "connect", _blocked_connect)
    yield
    if attempts:
        # The daemon catches transport errors on its own thread, so the raise
        # above can be swallowed.  Fail here so the leak is never silent.
        pytest.fail(
            f"{nodeid} tried to launch the real Claude CLI {len(attempts)} time(s). "
            f"Use a fake SDK client (see tests/conftest.py guard notes).",
            pytrace=False)


# ---------------------------------------------------------------------------
# No test scans (or kills) the machine's real Claude processes by accident.
#
# The delete endpoints call _get_running_session_ids() and kill any CLI whose
# command line matches the session id.  Underneath, that shells out to
# PowerShell (Get-CimInstance Win32_Process) or `ps`: 0.5s alone, 2.5s under
# the 8-worker Publish run, on every delete a route test makes.  The result
# also depended on what happened to be running on the dev machine.
#
# Only the OS shell-out is replaced (with "no processes running", which is
# what every route test assumes); _get_running_session_ids() and the
# endpoints' own logic still run.  The real enumerator keeps its coverage in
# tests/test_process_detection.py, which opts out with
# ``@pytest.mark.real_process_scan``.
# ---------------------------------------------------------------------------
@pytest.fixture(autouse=True)
def _no_real_process_scan(request, monkeypatch):
    if request.node.get_closest_marker("real_process_scan"):
        return
    try:
        import web.process_detection as _pd
    except Exception:
        return
    monkeypatch.setattr(_pd, "_enumerate_claude_processes", lambda: [])


# ---------------------------------------------------------------------------
# No test leaks the UI's active project into the next one.
#
# ``app.config._active_project`` is process-global.  Routes set it as a side
# effect (POST /api/set-project, the socket connect handler, the watchdog
# deep link), so a route test left it at "test-proj" and every later test in
# that worker that relies on "no project context" saw the wrong project
# (test_config_paths::test_empty_project_always_matches failed only in the
# full run, depending on which files --dist loadfile put on the same worker).
# Restore it after every test.
# ---------------------------------------------------------------------------
@pytest.fixture(autouse=True)
def _restore_active_project(monkeypatch):
    try:
        import web.config as _cfg
    except Exception:
        return
    monkeypatch.setattr(_cfg, "_active_project", _cfg._active_project)


def _make_session_line(msg_type, content="", timestamp=None):
    """Build a single JSONL line for a mock session file."""
    ts = timestamp or datetime.now(timezone.utc).isoformat()
    if msg_type == "custom-title":
        return json.dumps({"type": "custom-title", "customTitle": content})
    return json.dumps({
        "type": msg_type,
        "message": {"content": content},
        "timestamp": ts,
    })


@pytest.fixture
def sample_session_file(tmp_path):
    """Create a single .jsonl session file with a few messages."""
    path = tmp_path / "sess_abc123.jsonl"
    lines = [
        _make_session_line("user", "Hello, help me with Python", "2026-03-01T10:00:00Z"),
        _make_session_line("assistant", "Sure! What do you need?", "2026-03-01T10:00:05Z"),
        _make_session_line("user", "Write a fibonacci function", "2026-03-01T10:01:00Z"),
        _make_session_line("assistant", "Here's a fibonacci function:\n```python\ndef fib(n):\n    if n <= 1: return n\n    return fib(n-1) + fib(n-2)\n```", "2026-03-01T10:01:10Z"),
    ]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


@pytest.fixture
def empty_session_file(tmp_path):
    """Create an empty .jsonl session file."""
    path = tmp_path / "sess_empty.jsonl"
    path.write_text("", encoding="utf-8")
    return path


@pytest.fixture
def titled_session_file(tmp_path):
    """Create a session with a custom title."""
    path = tmp_path / "sess_titled.jsonl"
    lines = [
        _make_session_line("custom-title", "My Project"),
        _make_session_line("user", "Let's build something", "2026-03-01T12:00:00Z"),
        _make_session_line("assistant", "Sounds good!", "2026-03-01T12:00:05Z"),
    ]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


@pytest.fixture
def mock_sessions_dir(tmp_path):
    """Create a directory with multiple session files, mimicking ~/.claude/projects/xxx/."""
    project_dir = tmp_path / "projects" / "C--Users-test-project"
    project_dir.mkdir(parents=True)

    for i in range(5):
        path = project_dir / f"session_{i:03d}.jsonl"
        lines = [
            _make_session_line("user", f"Question {i}", f"2026-03-0{i+1}T10:00:00Z"),
            _make_session_line("assistant", f"Answer {i}", f"2026-03-0{i+1}T10:00:05Z"),
        ]
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    # Add an empty session
    empty = project_dir / "session_empty.jsonl"
    empty.write_text("", encoding="utf-8")

    # Add names file
    names = {"session_000": "First Session", "session_001": "Second Session"}
    (project_dir / "_session_names.json").write_text(json.dumps(names), encoding="utf-8")

    return project_dir


@pytest.fixture
def kanban_app(tmp_path, monkeypatch):
    """Flask app with an isolated SQLite kanban repo for kanban API tests.

    Isolates THREE production paths so no test using this fixture can
    leak into the user's real installation:

      1. The active SQLite DB — repo is constructed at tmp_path.
      2. ``app.config._KANBAN_CONFIG_FILE`` — redirected to tmp_path so
         endpoints calling ``save_kanban_config()`` (PUT /api/kanban/config,
         POST /api/kanban/migrate, POST /api/kanban/projects/alias) write
         to a sandboxed file instead of the repo-root kanban_config.json.
      3. ``get_active_project`` — pinned to a deterministic 'test-project'
         id so tests don't depend on the cwd.

    The autouse ``_block_production_paths`` fixture in this file is the
    safety net beyond this — even if a test bypasses kanban_app entirely,
    it still can't open the production DB or write the production config.
    """
    import json
    import web.db as db_mod
    from web.db import reset_repository
    from web.db.sqlite_backend import SqliteRepository
    from web import create_app, config as config_mod

    reset_repository()

    # Sandbox kanban_config.json BEFORE create_app so any startup config
    # reads land on the tmp file rather than the repo's real one.
    tmp_config = tmp_path / "kanban_config.json"
    tmp_config.write_text(json.dumps({"kanban_backend": "sqlite"}), encoding="utf-8")
    monkeypatch.setattr(config_mod, "_KANBAN_CONFIG_FILE", tmp_config)
    config_mod._kanban_config_cache = None

    application = create_app(testing=True)
    application.session_manager.has_session.return_value = False

    # Point kanban to a tmp SQLite DB
    repo = SqliteRepository(str(tmp_path / "test_kanban.db"))
    repo.initialize()
    db_mod._repo = repo

    # Fix project ID so tests are deterministic
    monkeypatch.setattr("web.routes.kanban_api.get_active_project", lambda: "test-project")
    monkeypatch.setattr("web.routes.kanban_api._emit", lambda *a, **kw: None)

    with application.test_client() as client:
        with application.app_context():
            yield application, client, repo

    repo.close()
    db_mod._repo = None
    config_mod._kanban_config_cache = None


@pytest.fixture
def kanban_client(kanban_app):
    """Shortcut: just the Flask test client for kanban tests."""
    _, client, _ = kanban_app
    return client


@pytest.fixture
def large_session_file(tmp_path):
    """Create a large session file (>32KB) to test head+tail reading."""
    path = tmp_path / "sess_large.jsonl"
    lines = [_make_session_line("user", "First message", "2026-01-01T00:00:00Z")]
    # Add many assistant messages to push file over 32KB
    for i in range(200):
        lines.append(_make_session_line(
            "assistant",
            f"Response {i}: " + "x" * 150,
            f"2026-01-01T00:{i // 60:02d}:{i % 60:02d}Z"
        ))
    lines.append(_make_session_line("user", "Last message", "2026-01-01T12:00:00Z"))
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path
