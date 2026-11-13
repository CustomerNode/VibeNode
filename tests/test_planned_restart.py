"""A restart VibeNode asks for is never shown as an outage, and comes back.

The Start page (reviver) keeps its outage page ("Stopped", Start button) for
real outages. While a restart note from ``/api/restart`` is fresh it shows a
"Restarting" page instead, and if the web server has not come back within
the comeback window it starts VibeNode itself, once per note.
"""
import importlib.util
import json
import sys
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
REVIVER = ROOT / "src" / "launcher" / "reviver.py"


@pytest.fixture
def note(tmp_path, monkeypatch):
    path = tmp_path / "planned_restart.json"
    monkeypatch.setenv("VIBENODE_PLANNED_RESTART_FILE", str(path))
    return path


@pytest.fixture
def reviver(monkeypatch, note):
    spec = importlib.util.spec_from_file_location("_test_reviver", REVIVER)
    mod = importlib.util.module_from_spec(spec)
    monkeypatch.setattr(sys, "argv", ["reviver.py"])
    spec.loader.exec_module(mod)
    return mod


def _write(note, age, scope="web"):
    note.write_text(json.dumps({"scope": scope, "reason": "", "at": time.time() - age}))


def test_note_is_fresh_only_for_a_while(note):
    from shared import planned_restart
    assert planned_restart.fresh() is None
    planned_restart.write("daemon")
    assert planned_restart.fresh()["scope"] == "daemon"
    _write(note, planned_restart.FRESH_SECONDS + 5)
    assert planned_restart.fresh() is None


def test_a_real_outage_keeps_the_stopped_page(reviver, note):
    assert reviver._planned_restart() is None
    page = reviver._start_page("Box").decode()
    assert "Stopped" in page and "Start VibeNode" in page


def test_a_planned_restart_says_so_and_returns_by_itself(reviver, note):
    _write(note, 1)
    assert reviver._planned_restart() is not None
    page = reviver._restarting_page("Box").decode()
    assert "VibeNode is restarting" in page
    assert "Stopped" not in page and "Start VibeNode" not in page
    assert "location.href = '/'" in page          # takes the user back in
    assert reviver._SENTINEL in page


def _one_loop(reviver, monkeypatch, serving=True):
    rev = reviver.Reviver()
    rev._serve_httpd = object() if serving else None
    rev._state = "serving" if serving else "dormant"
    launched = []
    monkeypatch.setattr(rev, "yield_now", lambda launch: launched.append(launch))
    monkeypatch.setattr(reviver, "_mobile_enabled", lambda: True)
    monkeypatch.setattr(reviver, "_something_listening", lambda port: True)

    class Stop(Exception):
        pass

    def stop(_):
        raise Stop
    monkeypatch.setattr(reviver.time, "sleep", stop)
    return rev, launched, Stop


def test_a_planned_restart_that_never_came_back_is_started_once(reviver, note, monkeypatch):
    _write(note, 60)
    rev, launched, Stop = _one_loop(reviver, monkeypatch)
    for _ in range(3):
        with pytest.raises(Stop):
            rev.run()
    assert launched == [True]


def test_no_launch_inside_the_comeback_window(reviver, note, monkeypatch):
    _write(note, 2)
    rev, launched, Stop = _one_loop(reviver, monkeypatch)
    with pytest.raises(Stop):
        rev.run()
    assert launched == []


def test_a_real_outage_is_never_started_by_itself(reviver, note, monkeypatch):
    rev, launched, Stop = _one_loop(reviver, monkeypatch)
    with pytest.raises(Stop):
        rev.run()
    assert launched == []


def test_new_code_is_rerun_only_while_dormant(reviver, note, monkeypatch):
    rev, _, Stop = _one_loop(reviver, monkeypatch, serving=True)
    rev._code_stamp = ("old",)
    ran = []
    monkeypatch.setattr(rev, "_reexec", lambda: ran.append(1))
    with pytest.raises(Stop):
        rev.run()
    assert ran == []                       # never while showing a page
    rev._serve_httpd = None
    rev._state = "dormant"
    with pytest.raises(Stop):
        rev.run()
    assert ran == [1]


def test_restart_route_writes_the_note():
    src = (ROOT / "src" / "web" / "routes" / "main.py").read_text(encoding="utf-8")
    body = src.split("def restart_server", 1)[1].split("subprocess.Popen", 1)[0]
    assert "planned_restart.write(scope" in body


def test_a_restarted_reviver_can_take_its_port_back_at_once(reviver):
    # Linux/macOS: SO_REUSEADDR lets the new copy bind past TIME_WAIT and
    # still never lets two copies listen. Windows keeps the exclusive bind.
    assert reviver._ControlServer.allow_reuse_address == (sys.platform != "win32")
