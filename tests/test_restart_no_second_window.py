"""A restart never opens a second VibeNode window.

A Session Engine restart (scope "daemon" or "both") relaunches VibeNode
without VIBENODE_PRESERVE_DAEMON, so run.py would treat it as a cold start and
open a new window beside the tab that asked for the restart. Every restart
command sets VIBENODE_NO_BROWSER before the relaunch; run.py skips the
window when it sees it.

These call the real /api/restart route with the process launch replaced, so
nothing is killed or started.
"""
import sys
from unittest import mock

import pytest

from web import create_app


@pytest.fixture
def client():
    app = create_app(testing=True)
    return app.test_client()


def _command_for(client, scope, platform):
    captured = {}

    def fake_popen(cmd, *a, **kw):
        captured["cmd"] = cmd
        return mock.MagicMock()

    # Windows-only process flags do not exist on Linux or macOS; a fake
    # Windows needs them to build its command.
    flags = {n: 0 for n in ("CREATE_NEW_PROCESS_GROUP", "CREATE_NO_WINDOW")}
    with mock.patch("web.routes.main.subprocess.Popen", side_effect=fake_popen), \
            mock.patch.multiple("web.routes.main.subprocess", create=True, **flags), \
            mock.patch.object(sys, "platform", platform):
        r = client.post("/api/restart", json={"scope": scope})
    assert r.status_code == 200, r.get_data(as_text=True)
    return captured["cmd"]


@pytest.mark.parametrize("scope", ["web", "daemon", "both"])
def test_windows_restart_never_opens_a_window(client, scope):
    cmd = _command_for(client, scope, "win32")
    flag = "$env:VIBENODE_NO_BROWSER='1'"
    assert flag in cmd
    assert cmd.index(flag) < cmd.index("Start-Process -FilePath"), (
        "the flag must be set before the relaunch so the child inherits it")


@pytest.mark.parametrize("scope", ["web", "daemon", "both"])
def test_posix_restart_never_opens_a_window(client, scope):
    cmd = _command_for(client, scope, "linux")
    flag = "export VIBENODE_NO_BROWSER=1;"
    assert flag in cmd
    assert cmd.index(flag) < cmd.index("nohup "), (
        "the flag must be exported before nohup so the child inherits it")


def test_engine_restart_still_is_a_cold_start(client):
    """Only the window changes: an engine restart must still not preserve
    the engine it is replacing."""
    cmd = _command_for(client, "daemon", "win32")
    assert "VIBENODE_PRESERVE_DAEMON" not in cmd


def test_run_py_skips_the_window_on_the_flag():
    from pathlib import Path
    src = (Path(__file__).resolve().parents[1] / "src" / "launcher" / "run.py").read_text(encoding="utf-8")
    assert '_no_browser = os.environ.pop("VIBENODE_NO_BROWSER", "") == "1"' in src
    assert 'and not _no_browser:' in src
