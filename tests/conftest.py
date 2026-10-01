"""Shared test fixtures for VibeNode.

These fixtures are available to ALL tests (both fast unit tests and e2e tests).
No server is started here — the e2e/ subfolder has its own conftest.py that
handles server lifecycle for Selenium tests.

PRODUCTION-PATH GUARD
=====================
The autouse ``_block_production_paths`` fixture below makes it physically
impossible for any test to open the user's real kanban DB or rewrite the
real ``kanban_config.json``. It intercepts ``sqlite3.connect`` and
``Path.write_text`` and raises if either is called with a production path.

This exists because in 2026-05-03 a migrate test that constructed a
default-path SqliteRepository() ran ``clear_all_data()`` against the
user's live DB and wiped it. Individual tests should still use the
``kanban_app`` fixture or explicit monkeypatching, but this is a hard
backstop for that whole class of bug — present and future.

REVIVER / AUTOSTART GUARD
=========================
The autouse ``_forbid_reviver_and_autostart_spawn`` fixture below makes it
physically impossible for any test to launch the real ``reviver.py`` or
``schtasks.exe``, or to write inside the real Windows Startup folder.

Background (Step 1, 2026-09-26): ``test_mobile_command.py`` used to call the
real ``mobile_command.enable()`` / ``.disable()`` — which spawns real
``reviver.py`` and ``reviver.py --unregister`` from the production checkout.
Because the reviver's task name and Startup VBS path are salted from the
checkout path alone, every such run addressed production's real
``\\VibeNodeReviver_0d5a41d4`` scheduled task and Startup VBS, deleting
and rewriting them with whatever interpreter ran the tests. This actually
happened during the 2026-09-25 investigation: production autostart was gone
for ~20 minutes. See STEP1_TEST_SAFETY_REPORT.md.

The affected tests now mock ``_spawn_reviver`` / ``_retire_reviver`` in their
own fixtures. This conftest layer is the safety net: if any test — present or
future — slips a ``subprocess.Popen`` call for ``reviver.py`` or ``schtasks``
past its own mock, the guard raises loudly with the test's node ID. Silent
regression is impossible.
"""

import json
import os
import shlex
import sqlite3
import subprocess
import sys
import pytest
from pathlib import Path
from datetime import datetime, timezone


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
    fake_home = tmp_path_factory.mktemp("home")
    # Pre-create directories tests expect under home/. Add to this list
    # rather than skipping the fixture if a test needs a new one.
    # ``.claude/projects`` is iterated by ``app.routes.sessions_api`` —
    # missing it causes order-dependent FileNotFoundError flakes.
    (fake_home / ".claude" / "projects").mkdir(parents=True, exist_ok=True)
    (fake_home / "Downloads").mkdir(exist_ok=True)
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
    (fake_appdata / "Microsoft" / "Windows" / "Start Menu" / "Programs" / "Startup").mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("APPDATA", str(fake_appdata))
    monkeypatch.setenv("USERPROFILE", str(fake_home))
    monkeypatch.setenv("HOME", str(fake_home))

    # Several daemon modules capture ``Path.home()``-derived paths at IMPORT
    # time (module-level constants), so by the time this per-test fixture
    # runs the values are already baked in to the real ``~/.claude/`` paths.
    # Patch the captured constants too so writes land in the sandbox even
    # when the module was imported before the fixture activated.
    try:
        import daemon.session_registry as _sr
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
    try:
        import app.config as _cfg
        monkeypatch.setattr(_cfg, "_CLAUDE_PROJECTS",
                            fake_home / ".claude" / "projects")
    except ImportError:
        pass
    # ``app.routes.sessions_api`` does ``from ..config import _CLAUDE_PROJECTS``,
    # which binds the value at import time and doesn't follow ``app.config``
    # reassignment. Patch the route's local binding directly.
    try:
        import app.routes.sessions_api as _sa
        monkeypatch.setattr(_sa, "_CLAUDE_PROJECTS",
                            fake_home / ".claude" / "projects")
    except ImportError:
        pass

    # daemon.session_manager appends Path.home()-derived paths to
    # os.environ["PATH"] at import time. Tests that reload that module
    # (e.g., the sm_module fixture in test_state_transitions.py) would
    # otherwise extend PATH unboundedly across the session — on Windows
    # the env block overflows at 32767 chars and subsequent reloads
    # raise ValueError. Snapshot/restore PATH so each test starts clean.
    monkeypatch.setenv("PATH", os.environ.get("PATH", ""))
    yield


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

    def _guarded_popen(cmd, *args, **kwargs):
        rendered = _command_string(cmd)
        if _is_forbidden_command(rendered):
            raise AssertionError(
                f"\n\nTEST {request.node.nodeid} tried to spawn a forbidden "
                f"subprocess:\n    {rendered}\n\n"
                f"This is the reviver / autostart safety guard. The command "
                f"contains 'reviver.py' or 'schtasks', either of which can "
                f"register, rewrite, or DELETE the production "
                f"\\\\VibeNodeReviver_0d5a41d4 scheduled task and Startup VBS.\n\n"
                f"If this call originated from mobile_command.enable() / "
                f".disable(), your test needs to monkeypatch "
                f"app.mobile_command._spawn_reviver and _retire_reviver in "
                f"its fixture — see the `mc` fixture in test_mobile_command.py.\n\n"
                f"If this is a new legitimate use case, extend "
                f"_FORBIDDEN_SUBPROCESS_MARKERS in tests/conftest.py, or add "
                f"a narrow opt-out. Do NOT weaken the guard globally.\n"
            )
        return real_popen(cmd, *args, **kwargs)

    monkeypatch.setattr(subprocess, "Popen", _guarded_popen)
    yield


# ═══════════════════════════════════════════════════════════════════════════
# REAL CLAUDE CLI GUARD + FAKE-SDK BACKEND ISOLATION (2026-10-01)
# ═══════════════════════════════════════════════════════════════════════════
#
# Many daemon test files build a SessionManager inside
# ``patch.dict('sys.modules', <fake claude_code_sdk>)`` and believe it then
# drives fake clients.  Since the backend refactor, SessionManager() gets its
# client from ``daemon.backends.claude``, imported lazily.  That module binds
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
    import daemon.backends as _backends_pkg
    saved_mod = sys.modules.pop("daemon.backends.claude", None)
    had_attr = "claude" in vars(_backends_pkg)
    saved_attr = vars(_backends_pkg).get("claude")
    try:
        yield
    finally:
        sys.modules.pop("daemon.backends.claude", None)
        if saved_mod is not None:
            sys.modules["daemon.backends.claude"] = saved_mod
        if had_attr:
            _backends_pkg.claude = saved_attr
        elif "claude" in vars(_backends_pkg):
            delattr(_backends_pkg, "claude")


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
    import app.db as db_mod
    from app.db import reset_repository
    from app.db.sqlite_backend import SqliteRepository
    from app import create_app, config as config_mod

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
    monkeypatch.setattr("app.routes.kanban_api.get_active_project", lambda: "test-project")
    monkeypatch.setattr("app.routes.kanban_api._emit", lambda *a, **kw: None)

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
