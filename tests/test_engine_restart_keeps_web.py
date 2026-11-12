"""A Session Engine restart never takes the web server down.

The automatic restart-when-idle (and Developer Tools' Session Engine button)
calls /api/restart with scope "daemon".  It used to relaunch the whole app,
a cold start whose run.py killed port 5050 as well, so every engine restart
left every open tab on "VibeNode Server Unreachable" for about ten seconds.
Now scope "daemon" starts run.py in engine-only mode: it stops nothing,
starts the engine if none is listening, and exits.

The route tests replace the process launch, so nothing is killed or started.
The run.py test points it at a port this test listens on, so run.py finds
"an engine" there and exits without starting one.
"""
import os
import socket
import subprocess
import sys
from pathlib import Path
from unittest import mock

import pytest

from web import create_app

_ROOT = Path(__file__).resolve().parents[1]
_RUN_PY = _ROOT / "src" / "launcher" / "run.py"


@pytest.fixture
def client():
    return create_app(testing=True).test_client()


def _command_for(client, scope, platform):
    captured = {}

    def fake_popen(cmd, *a, **kw):
        captured["cmd"] = cmd
        return mock.MagicMock()

    flags = {n: 0 for n in ("CREATE_NEW_PROCESS_GROUP", "CREATE_NO_WINDOW")}
    with mock.patch("web.routes.main.subprocess.Popen", side_effect=fake_popen), \
            mock.patch.multiple("web.routes.main.subprocess", create=True, **flags), \
            mock.patch.object(sys, "platform", platform):
        r = client.post("/api/restart", json={"scope": scope})
    assert r.status_code == 200, r.get_data(as_text=True)
    return captured["cmd"]


@pytest.mark.parametrize("platform,flag,ports", [
    ("linux", "export VIBENODE_ENGINE_ONLY=1;", "for _pp in 5051;"),
    ("win32", "$env:VIBENODE_ENGINE_ONLY='1'", "-LocalPort 5051 "),
])
def test_an_engine_restart_starts_only_the_engine(client, platform, flag, ports,
                                                  monkeypatch):
    monkeypatch.delenv("VIBENODE_WEB_PORT", raising=False)
    monkeypatch.delenv("VIBENODE_DAEMON_PORT", raising=False)
    cmd = _command_for(client, "daemon", platform)
    assert flag in cmd
    assert ports in cmd, "only the engine's port may be stopped"
    assert "run.py" in cmd and "session_manager.py" not in cmd


@pytest.mark.parametrize("platform", ["linux", "win32"])
@pytest.mark.parametrize("scope", ["web", "both"])
def test_other_restarts_relaunch_the_whole_app(client, platform, scope):
    cmd = _command_for(client, scope, platform)
    assert "VIBENODE_ENGINE_ONLY" not in cmd
    assert "session_manager.py" in cmd


_STAND_IN = """
import socket, sys
socks = []
for _ in range(2):
    s = socket.socket(); s.bind(("127.0.0.1", 0)); s.listen(8); socks.append(s)
print(socks[0].getsockname()[1], socks[1].getsockname()[1], flush=True)
sys.stdin.read()
"""


def test_engine_only_run_py_never_touches_the_web_port(tmp_path):
    """run.py in engine-only mode exits once an engine answers, before the
    code that kills ports or starts the web server.  The stand-in engine
    and web server live in a child process, so a regression that kills
    their ports kills that child, never the test run."""
    holder = subprocess.Popen([sys.executable, "-c", _STAND_IN],
                              stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)
    try:
        engine_port, web_port = holder.stdout.readline().split()
        env = dict(os.environ,
                   VIBENODE_ENGINE_ONLY="1",
                   VIBENODE_DAEMON_PORT=engine_port,
                   VIBENODE_WEB_PORT=web_port,
                   VIBENODE_NO_BROWSER="1")
        env.pop("VIBENODE_BOOT_STATUS_FILE", None)
        r = subprocess.run([sys.executable, str(_RUN_PY)], env=env, cwd=str(tmp_path),
                           capture_output=True, text=True, timeout=60)
        assert r.returncode == 0, r.stdout + r.stderr
        assert "Session daemon already running" in r.stdout
        assert "Killed stale process" not in r.stdout
        assert "VIBENODE RUNNING" not in r.stdout
        assert holder.poll() is None, "the stand-in web server was killed"
        socket.create_connection(("127.0.0.1", int(web_port)), timeout=2).close()
    finally:
        holder.kill()
        holder.wait(timeout=10)
