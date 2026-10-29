"""A second launch reuses a running VibeNode; the sign-in start opens no window.

2026-10-29: the sign-in autostart started VibeNode and opened a window, then
the owner clicked VibeNode and that cold start killed the running web server
and Session Engine and opened a second window. session_manager.py now checks
for a healthy running copy first, and ``--background`` (the sign-in entry)
starts with no window.
"""

import importlib.util
import socket
import sys
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture()
def sm():
    # Importing is a no-op (tests/test_session_manager_import_guard.py).
    return _load("_vn_launcher_under_test", ROOT / "session_manager.py")


def _free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


class _Web:
    """A stand-in web server: answers /api/ping like VibeNode or the reviver."""

    def __init__(self, status, body):
        st, bd = status, body

        class H(BaseHTTPRequestHandler):
            def do_GET(self):
                self.send_response(st)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(bd)

            def log_message(self, *a):
                pass

        self.srv = HTTPServer(("127.0.0.1", 0), H)
        self.port = self.srv.server_address[1]
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()

    def close(self):
        self.srv.shutdown()
        self.srv.server_close()


@pytest.fixture()
def engine_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    s.listen(5)
    yield s.getsockname()[1]
    s.close()


@pytest.fixture()
def env(monkeypatch):
    monkeypatch.delenv("VIBENODE_PRESERVE_DAEMON", raising=False)
    monkeypatch.delenv("VIBENODE_TEST_PORT", raising=False)
    return monkeypatch


def _arm(sm, env, web_port, daemon_port):
    env.setenv("VIBENODE_WEB_PORT", str(web_port))
    env.setenv("VIBENODE_DAEMON_PORT", str(daemon_port))
    opened = []
    env.setattr(sm, "_load_browser_opener", lambda: (lambda: opened.append(1)))
    return opened


def test_click_while_running_opens_a_window_and_starts_nothing(sm, env, engine_port):
    web = _Web(200, b'{"ok": true}')
    try:
        opened = _arm(sm, env, web.port, engine_port)
        assert sm._reuse_running_instance(background=False) is True
        assert opened == [1]
    finally:
        web.close()


def test_sign_in_start_while_running_does_nothing(sm, env, engine_port):
    web = _Web(200, b'{"ok": true}')
    try:
        opened = _arm(sm, env, web.port, engine_port)
        assert sm._reuse_running_instance(background=True) is True
        assert opened == []
    finally:
        web.close()


def test_reviver_start_page_is_not_a_running_vibenode(sm, env, engine_port):
    web = _Web(503, b'{"ok":false,"error":"vibenode-down","reviver":true}')
    try:
        opened = _arm(sm, env, web.port, engine_port)
        assert sm._reuse_running_instance(background=False) is False
        assert opened == []
    finally:
        web.close()


def test_engine_down_means_a_normal_start(sm, env):
    # A Session Engine restart kills the engine before relaunching: its port
    # is closed, so the launch must go ahead and start it.
    web = _Web(200, b'{"ok": true}')
    try:
        opened = _arm(sm, env, web.port, _free_port())
        assert sm._reuse_running_instance(background=False) is False
        assert opened == []
    finally:
        web.close()


def test_nothing_running_means_a_normal_start(sm, env):
    opened = _arm(sm, env, _free_port(), _free_port())
    assert sm._reuse_running_instance(background=False) is False
    assert opened == []


def test_a_restart_never_reuses(sm, env, engine_port):
    web = _Web(200, b'{"ok": true}')
    try:
        opened = _arm(sm, env, web.port, engine_port)
        env.setenv("VIBENODE_PRESERVE_DAEMON", "1")
        assert sm._reuse_running_instance(background=False) is False
        assert opened == []
    finally:
        web.close()


def test_the_window_opens_with_run_py_own_opener(sm, env):
    """The extracted opener is run.py's real code with every name resolved."""
    env.setenv("VIBENODE_WEB_PORT", "5099")
    opener = sm._load_browser_opener()
    ns = opener.__globals__
    assert ns["_WEB_PORT"] == 5099
    import builtins
    import dis

    def _globals_used(code):
        for ins in dis.get_instructions(code):
            if ins.opname == "LOAD_GLOBAL":
                yield ins.argval
        for const in code.co_consts:
            if hasattr(const, "co_code"):
                yield from _globals_used(const)

    for fn in sm._BROWSER_FUNCS:
        for name in _globals_used(ns[fn].__code__):
            assert name in ns or hasattr(builtins, name), (
                "%s() uses the global %r, which the launcher does not "
                "provide; add it to _load_browser_opener's namespace" % (fn, name))
    src = (ROOT / "run.py").read_text(encoding="utf-8")
    for fn in sm._BROWSER_FUNCS:
        assert "\ndef %s(" % fn in src


def test_run_py_skips_the_window_on_a_background_start():
    src = (ROOT / "run.py").read_text(encoding="utf-8")
    assert 'os.environ.pop("VIBENODE_NO_BROWSER", "")' in src
    assert "and not _no_browser:" in src


def test_launcher_passes_background_on_to_run_py():
    src = (ROOT / "session_manager.py").read_text(encoding="utf-8")
    assert '"--background" in sys.argv[1:]' in src
    assert 'os.environ["VIBENODE_NO_BROWSER"] = "1"' in src
    assert "if _reuse_running_instance(background):" in src


@pytest.mark.parametrize("platform", ["win32", "darwin", "linux"])
def test_sign_in_entry_starts_in_the_background(monkeypatch, platform):
    vn = _load("_vn_cli_under_test", ROOT / "vibenode.py")
    monkeypatch.setattr(sys, "platform", platform)
    if platform == "win32":
        monkeypatch.setenv("APPDATA", str(ROOT))
    body = vn._autostart_body(vn._autostart_entry())
    assert "--background" in body
