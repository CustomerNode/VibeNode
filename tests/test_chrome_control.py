"""Chrome control: sessions start connected to Claude in Chrome (2026-10-28).

A ChatGPT cleanup session had no browser tools, because the Session Engine
never passed the CLI's ``--chrome`` flag, so it told the owner to run
``claude --chrome`` in a terminal.  System -> Preferences -> Chrome control
(``chrome_control`` in kanban_config, opt-in) adds the flag to every launch
and wake (``SessionManager._with_chrome``).
"""
import asyncio
import os
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from daemon.session_manager import SessionInfo, SessionManager, SessionState


def _pref(monkeypatch, on):
    monkeypatch.setattr(SessionManager, "_chrome_control_on", staticmethod(lambda: on))


@pytest.fixture
def m():
    return SessionManager()


def test_flag_added_only_when_the_preference_is_on(m, monkeypatch):
    _pref(monkeypatch, False)
    assert m._with_chrome({"effort": "high"}, "") == {"effort": "high"}
    _pref(monkeypatch, True)
    assert m._with_chrome({"effort": "high"}, "") == {"effort": "high", "chrome": None}
    assert m._with_chrome(None, "") == {"chrome": None}


@pytest.mark.parametrize("stype", ["planner", "title"])
def test_background_sessions_never_get_chrome(m, monkeypatch, stype):
    _pref(monkeypatch, True)
    assert m._with_chrome({}, stype) == {}


def test_a_callers_own_choice_wins(m, monkeypatch):
    _pref(monkeypatch, True)
    assert m._with_chrome({"no-chrome": None}, "") == {"no-chrome": None}
    assert m._with_chrome({"chrome": None}, "") == {"chrome": None}


def test_the_callers_dict_is_not_changed(m, monkeypatch):
    _pref(monkeypatch, True)
    mine = {"effort": "low"}
    m._with_chrome(mine, "")
    assert mine == {"effort": "low"}


def test_preference_reads_the_saved_config(monkeypatch):
    import common.config as cc
    monkeypatch.setattr(cc, "get_kanban_config", lambda: {"chrome_control": True})
    assert SessionManager._chrome_control_on() is True
    monkeypatch.setattr(cc, "get_kanban_config", lambda: {"chrome_control": "yes"})
    assert SessionManager._chrome_control_on() is False   # only a real true
    monkeypatch.setattr(cc, "get_kanban_config", lambda: {})
    assert SessionManager._chrome_control_on() is False   # opt-in


def test_a_self_heal_relaunch_carries_chrome(m, monkeypatch, tmp_path):
    """The reconnect path builds its own options; it must add the flag too."""
    _pref(monkeypatch, True)
    sid = "chrome-reconnect"
    info = SessionInfo(session_id=sid, state=SessionState.IDLE)
    info.cwd, info.effort = str(tmp_path), "high"
    with m._lock:
        m._sessions[sid] = info
    captured = {}

    async def _create(opts):
        captured["opts"] = opts
        return MagicMock()
    m._sdk = MagicMock()
    m._sdk.create_session = _create
    m._sdk.connect = AsyncMock()
    m._sdk.extract_process_pid = MagicMock(return_value=0)
    with patch.object(m, "_prepare_resume_transcript"):
        assert asyncio.run(m._reconnect_client(sid, info)) is True
    assert captured["opts"].extra_args == {"effort": "high", "chrome": None}


def test_the_sdk_turns_it_into_the_cli_flag():
    """Both the in-engine CLI and the Session Host build argv with the SDK's
    own _build_command, which turns a None value into a bare flag."""
    from claude_code_sdk import ClaudeCodeOptions
    from claude_code_sdk._internal.transport.subprocess_cli import SubprocessCLITransport
    t = SubprocessCLITransport(prompt="x", options=ClaudeCodeOptions(extra_args={"chrome": None}),
                               cli_path=os.devnull)
    assert "--chrome" in t._build_command()
