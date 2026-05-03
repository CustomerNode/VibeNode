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
"""

import json
import sqlite3
import pytest
from pathlib import Path
from datetime import datetime, timezone


# Resolve the production paths once at module load. We compare against
# these in the guard below; any test that hits these exact paths fails
# the whole test, before the destructive call lands.
_REAL_KANBAN_DB = (Path.home() / ".claude" / "gui_kanban.db").resolve()
_REPO_ROOT = Path(__file__).resolve().parent.parent
_REAL_KANBAN_CONFIG = (_REPO_ROOT / "kanban_config.json").resolve()


def _is_production_path(p) -> bool:
    """Return True if *p* refers to the user's real kanban DB or config.

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
    return candidate == _REAL_KANBAN_DB or candidate == _REAL_KANBAN_CONFIG


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
