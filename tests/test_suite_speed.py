"""What keeps the suite fast must not change what it proves.

``tests/conftest.py`` shares compiled URL builders between Flask apps, makes
per-test sandbox folders without pytest's numbered-folder scan, and runs a
plain ``pytest`` of the whole suite in parallel.  Each of those would be a bug
if it leaked state between tests or quietly changed which tests run.
"""

import ast
import os
from pathlib import Path
from types import SimpleNamespace

import pytest
from flask import Flask, url_for
from werkzeug.routing import BaseConverter

from shared.test_workers import worker_count
from tests import conftest as cf

_seen_homes: list = []


# ── sandbox folders ──────────────────────────────────────────────────────────

@pytest.mark.parametrize("n", [0, 1])
def test_every_test_gets_its_own_empty_home(n):
    home = Path.home()
    assert home not in _seen_homes
    _seen_homes.append(home)
    assert str(home) == os.environ["HOME"]
    assert sorted(p.name for p in (home / ".claude").iterdir()) == ["projects"]
    assert list((home / ".claude" / "projects").iterdir()) == []
    assert list((home / "Downloads").iterdir()) == []
    # Leave something behind: the other parameter must not see it.
    (home / ".claude" / "projects" / "left-behind").mkdir()
    (home / "Downloads" / "left-behind.txt").write_text("x", encoding="utf-8")


def test_a_box_path_is_never_handed_out_twice(tmp_path_factory):
    paths = {cf._box_path(tmp_path_factory) for _ in range(50)}
    assert len(paths) == 50
    assert not any(p.exists() for p in paths)


def test_sandbox_folders_do_not_slow_down_tmp_path(tmp_path, tmp_path_factory):
    # tmp_path's numbered-folder scan lists its parent, so the per-test boxes
    # must not sit in it.
    assert tmp_path.parent == tmp_path_factory.getbasetemp()
    assert Path.home().parent != tmp_path.parent


# ── shared URL builders ──────────────────────────────────────────────────────

def _app(converter=None):
    app = Flask(__name__)
    if converter:
        app.url_map.converters["pick"] = converter
    return app


def test_shared_builders_are_actually_installed():
    from werkzeug.routing.rules import Rule
    assert getattr(Rule._compile_builder, "_vn_shared", False)


def test_two_apps_build_urls_from_their_own_rules_and_converters():
    class Upper(BaseConverter):
        def to_url(self, value):
            return value.upper()

    class Lower(BaseConverter):
        def to_url(self, value):
            return value.lower()

    a, b = _app(Upper), _app(Lower)
    for app in (a, b):
        app.add_url_rule("/x/<pick:name>", "x", lambda name: name)
    with a.test_request_context():
        assert url_for("x", name="MiXed") == "/x/MIXED"
        assert url_for("x", name="MiXed", extra="1") == "/x/MIXED?extra=1"
    with b.test_request_context():
        assert url_for("x", name="MiXed") == "/x/mixed"
        assert url_for("x", name="MiXed", extra="1") == "/x/mixed?extra=1"


def test_a_rule_with_defaults_is_not_shared():
    a, b = _app(), _app()
    a.add_url_rule("/p/<int:n>", "p", lambda n: str(n), defaults={"n": 1})
    b.add_url_rule("/p/<int:n>", "p", lambda n: str(n), defaults={"n": 2})
    with a.test_request_context():
        assert url_for("p") == "/p/1"
    with b.test_request_context():
        assert url_for("p") == "/p/2"


def test_real_app_routes_still_build_and_match():
    from web import create_app
    client = create_app(testing=True).test_client()
    assert client.get("/api/ping").get_json()["ok"] is True
    assert client.get("/api/no-such-route").status_code == 404


# ── a plain pytest of the whole suite is parallel ────────────────────────────

class _Plugins:
    def __init__(self, has_xdist=True):
        self.has_xdist = has_xdist

    def hasplugin(self, name):
        return self.has_xdist


def _config(args, **option):
    defaults = dict(numprocesses=None, tx=[], dist="no", collectonly=False, usepdb=False, capture="fd")
    defaults.update(option)
    return SimpleNamespace(args=args, option=SimpleNamespace(**defaults), pluginmanager=_Plugins())


TESTS_DIR = str(Path(cf.__file__).resolve().parent)


@pytest.mark.parametrize("args", [[TESTS_DIR], ["tests"]])
def test_the_whole_suite_runs_in_parallel_with_the_gate_scheduler(args, monkeypatch):
    monkeypatch.delenv("VIBENODE_TEST_SERIAL", raising=False)
    monkeypatch.setenv("VIBENODE_TEST_XDIST", "1")
    monkeypatch.delenv("VIBENODE_TEST_WORKERS", raising=False)
    monkeypatch.chdir(Path(TESTS_DIR).parent)
    c = _config(args)
    cf.pytest_cmdline_main(c)
    assert c.option.numprocesses == worker_count()
    assert c.option.dist == "loadfile"


@pytest.mark.parametrize("args,option", [
    (["tests/test_suite_speed.py"], {}),                # chosen files
    (["tests", "tests/test_suite_speed.py"], {}),
    (["tests"], {"numprocesses": 3}),                   # asked for its own
    (["tests"], {"dist": "each"}),
    (["tests"], {"collectonly": True}),
    (["tests"], {"usepdb": True}),
    (["tests"], {"capture": "no"}),                     # -s wants the terminal
])
def test_a_run_that_asked_for_something_else_is_left_alone(args, option, monkeypatch):
    monkeypatch.delenv("VIBENODE_TEST_SERIAL", raising=False)
    monkeypatch.chdir(Path(TESTS_DIR).parent)
    c = _config(args, **option)
    before = (c.option.numprocesses, c.option.dist)
    cf.pytest_cmdline_main(c)
    assert (c.option.numprocesses, c.option.dist) == before


def test_serial_opt_out_and_no_xdist_stay_serial(monkeypatch):
    monkeypatch.chdir(Path(TESTS_DIR).parent)
    c = _config(["tests"])
    monkeypatch.setenv("VIBENODE_TEST_SERIAL", "1")
    cf.pytest_cmdline_main(c)
    assert c.option.numprocesses is None
    monkeypatch.delenv("VIBENODE_TEST_SERIAL")
    c.pluginmanager = _Plugins(has_xdist=False)
    cf.pytest_cmdline_main(c)
    assert c.option.numprocesses is None


def test_an_xdist_worker_never_spawns_workers(monkeypatch):
    monkeypatch.delenv("VIBENODE_TEST_SERIAL", raising=False)
    monkeypatch.chdir(Path(TESTS_DIR).parent)
    c = _config(["tests"])
    c.workerinput = {}
    cf.pytest_cmdline_main(c)
    assert c.option.numprocesses is None


def test_the_publish_gate_and_a_plain_run_use_the_same_worker_count(monkeypatch):
    from web.routes import test_api as ta
    monkeypatch.setattr("sys.platform", "win32")
    monkeypatch.delattr("os.sched_getaffinity", raising=False)
    monkeypatch.setattr("os.cpu_count", lambda: 6)
    assert ta._parallel_args()[:2] == ["-n", str(worker_count())]


# ── no new long fixed waits ──────────────────────────────────────────────────
# Fixed waits make the suite slow.  Wait for the condition, or run the page's clock forward
# (FakeApp(clock=True) + advance()).  A wait that really is the behaviour under
# test says why on its line: ``# slow-ok: <reason>``.
FIXED_WAIT_LIMIT_S = 1.0


def _long_fixed_waits(source: str) -> list:
    lines = source.splitlines()
    found = []
    for node in ast.walk(ast.parse(source)):
        if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                and len(node.args) == 1 and isinstance(node.args[0], ast.Constant)
                and isinstance(node.args[0].value, (int, float))):
            continue
        name, value = node.func.attr, node.args[0].value
        if name == "wait_for_timeout":
            seconds = value / 1000
        elif name == "sleep" and getattr(node.func.value, "id", "") == "time":
            seconds = value
        else:
            continue
        if seconds >= FIXED_WAIT_LIMIT_S and "slow-ok:" not in lines[node.lineno - 1]:
            found.append((node.lineno, seconds))
    return found


def test_the_fixed_wait_check_catches_a_planted_wait():
    assert _long_fixed_waits("import time\ndef t(page):\n    time.sleep(1.5)\n") == [(3, 1.5)]
    assert _long_fixed_waits("def t(page):\n    page.wait_for_timeout(1000)\n") == [(2, 1.0)]
    assert not _long_fixed_waits("import time\ndef t():\n    time.sleep(1.5)  # slow-ok: the timer under test\n")
    assert not _long_fixed_waits("import time\ndef t():\n    time.sleep(0.2)\n")
    # A command handed to a child process is not a wait of this test.
    assert not _long_fixed_waits('CMD = "import time; time.sleep(60)"\n')


def test_no_test_sits_in_a_long_fixed_wait():
    tests = Path(cf.__file__).resolve().parent
    bad = {p.name: w for p in sorted(tests.glob("*.py"))
           if (w := _long_fixed_waits(p.read_text(encoding="utf-8")))}
    assert not bad, f"fixed waits of {FIXED_WAIT_LIMIT_S}s or more, wait for the condition instead: {bad}"


def test_the_shared_browser_keeps_its_frame_rate_cap():
    """Uncapped frames made three idle test pages cost 4.6 CPU cores (0.37
    capped); 8 workers saturated the machine and browser tests were killed at
    the 120 s timeout in clusters."""
    from tests import _browser
    args = _browser._launch_args()
    assert "--disable-frame-rate-limit" not in args
    assert "--disable-gpu-vsync" not in args


# ── the real Claude CLI never starts from a test ─────────────────────────────

def test_the_real_claude_cli_is_refused_and_a_stand_in_is_not(tmp_path):
    import subprocess
    if not cf._REAL_CLAUDE:
        pytest.skip("no Claude CLI on this machine")
    real = sorted(cf._REAL_CLAUDE)[0]
    with pytest.raises(AssertionError, match="real Claude CLI"):
        subprocess.Popen([real, "--version"])
    with pytest.raises(AssertionError, match="real Claude CLI"):
        subprocess.Popen("claude --version", shell=True)
    if os.name == "nt":     # Windows runs a batch file, not a #! script
        stand_in = tmp_path / "claude.cmd"
        stand_in.write_text("@echo stand-in\r\n", encoding="utf-8")
    else:
        stand_in = tmp_path / "claude"
        stand_in.write_text("#!/bin/sh\necho stand-in\n", encoding="utf-8")
        stand_in.chmod(0o755)
    assert subprocess.run([str(stand_in)], capture_output=True, text=True).stdout == "stand-in\n"


def test_importing_the_engine_arms_no_hang_dump_in_a_test_run():
    import importlib
    import faulthandler
    import engine.daemon_server as ds
    faulthandler.cancel_dump_traceback_later()
    if not os.path.isdir("/proc/self/task"):
        return
    # Count the OS threads the reload adds, not every OS thread minus Python's:
    # an earlier test in the same worker can leave a native thread behind (a
    # full parallel run), which says nothing about the engine.
    import time
    tasks = lambda: len(os.listdir("/proc/self/task"))
    # A cancelled watchdog thread exits a moment after the cancel: settle first.
    before, deadline = tasks(), time.monotonic() + 2
    while time.monotonic() < deadline:
        time.sleep(0.02)
        now = tasks()
        if now == before:
            break
        before = now
    importlib.reload(ds)        # runs its arm_hang_dump(120, ...) again
    try:
        assert tasks() == before
    finally:
        faulthandler.cancel_dump_traceback_later()


def test_the_suite_conftest_still_carries_the_crash_retry_hook():
    from tests import _crash_retry
    assert cf.pytest_handlecrashitem is _crash_retry.pytest_handlecrashitem
