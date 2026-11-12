"""The helper that gives fixtures a session manager bound to a fake SDK, and the
production values of the engine delays tests shorten."""

import importlib
import re
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

import engine
from tests import _sm_fresh as fresh

SOURCE = (Path(engine.__file__).parent / "session_manager.py").read_text(encoding="utf-8")


@pytest.fixture(autouse=True)
def _leave_the_package_as_it_was():
    """``patch.dict`` restores ``sys.modules``, not the attribute a fresh import
    sets on the ``daemon`` package; a later ``import engine.session_manager as x``
    would get a module ``importlib.reload`` refuses."""
    saved = vars(engine).get("session_manager")
    yield
    if saved is None:
        vars(engine).pop("session_manager", None)
    else:
        engine.session_manager = saved


def _engine_runs_once(monkeypatch, loaded):
    """How many times the file is executed by one call."""
    calls = []
    real = importlib.import_module("importlib").reload

    def counting_reload(m):
        calls.append("reload")
        return real(m)

    monkeypatch.setattr(fresh.importlib, "reload", counting_reload)
    fake_sdk = {"claude_code_sdk": MagicMock(), "claude_code_sdk.types": MagicMock()}
    with patch.dict("sys.modules", fake_sdk):
        if not loaded:
            sys.modules.pop(fresh.NAME, None)
        module = fresh.fresh_session_manager()
        again = fresh.fresh_session_manager()
        return calls, again


def test_a_fresh_import_is_not_executed_twice_and_a_loaded_one_is_reloaded(monkeypatch):
    calls, _ = _engine_runs_once(monkeypatch, loaded=False)
    # First call: nothing loaded, the import executes it.  Second call: loaded,
    # so it is reloaded, as the fixtures always did.
    assert calls == ["reload"]


def test_a_reload_runs_the_files_top_level_again(monkeypatch):
    fake_sdk = {"claude_code_sdk": MagicMock(), "claude_code_sdk.types": MagicMock()}
    with patch.dict("sys.modules", fake_sdk):
        module = fresh.fresh_session_manager()
        module.SessionManager._vn_mark = True
        after = fresh.fresh_session_manager()
        assert not hasattr(after.SessionManager, "_vn_mark"), "the class was built anew"


def test_tests_shorten_the_engines_delays_only_on_their_own_copy():
    fake_sdk = {"claude_code_sdk": MagicMock(), "claude_code_sdk.types": MagicMock()}
    with patch.dict("sys.modules", fake_sdk):
        module = fresh.fresh_session_manager()
        assert module.SessionManager._EMPTY_CYCLE_WAIT_S == fresh.EMPTY_CYCLE_WAIT_S
        assert module.SessionManager._RECONNECT_STAGGER_S == fresh.RECONNECT_STAGGER_S
        assert module.SessionManager._KILL_GRACE_S == fresh.KILL_GRACE_S


def test_production_keeps_its_delays():
    for name, value in (("_EMPTY_CYCLE_WAIT_S", "0.5"), ("_RECONNECT_STAGGER_S", "0.5"),
                        ("_KILL_GRACE_S", "0.3"), ("_LIMIT_SWITCH_MIN_DELAY_S", "1.0")):
        assert re.search(rf"^    {name} = {re.escape(value)}$", SOURCE, re.M), name
