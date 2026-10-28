"""Smart termination (daemon/smart_termination.py): every turn ends with a
handoff the user can act on, and a session thinks again before stopping with
work it could still do.  Preference ``smart_termination``, default on."""

import asyncio

import pytest

from daemon import smart_termination as st
from daemon.session_manager import SessionManager, SessionInfo


import json as _json

EXPLAINED = ("## What this is about\nOn a phone, the bar at the top of a chat sat over the first "
             "messages, so you could not read them.\n\n## What changed\nThe bar now takes its own "
             "space and the chat starts below it.\n\n## What you do\nNothing. Open any chat on your "
             "phone and scroll up to see it.")


def _card(**over):
    c = {"type": "done", "title": "Phone header", "path": ["Chat view"], "headline": "The phone header no longer covers the chat",
         "context": "the header overlapping the chat on phones.",
         "status": {"done": ["Header fixed on desktop and phone"]},
         "verified": [{"check": "Phone-size screenshot", "result": "Header clears the chat", "ok": True}],
         "links": [{"label": "Phone screenshot of the header", "kind": "file", "url": "/tmp/phone-header.png"}],
         "needs": {"kind": "none", "why": "It is finished.", "next": "It ships with your next Update."},
         "links": [{"label": "The fixed header on a phone", "kind": "image", "url": "/api/preview/asset/abc"}],
         "validate": {"how": "Open any chat on your phone and scroll up", "url": "/"},
         "zoom": {"big": "Make the VibeNode chat usable on phones.",
                  "task": "Stop the header covering the chat.",
                  "now": "Fixed the header, checked on a phone."},
         "explained": EXPLAINED}
    c.update(over)
    return "```handoff\n" + _json.dumps(c) + "\n```"


GOOD = _card()


def _review(msg, **kw):
    kw.setdefault("continued", False)
    kw.setdefault("sendbacks", 0)
    return st.review(msg, **kw)


# ── review(): the approved card, alone ──────────────────────────────────

def test_the_card_alone_lets_the_turn_end():
    assert _review(GOOD) is None


def test_the_instructions_teach_the_card():
    assert "```handoff" in st.INSTRUCTIONS and "card ALONE" in st.INSTRUCTIONS


def test_no_card_is_sent_back():
    msg = ("The redone option C (the Expanded and Collapsed links in the preview's "
           "header) is pushed. Pick B or C on the board to finish point 5.")
    reason = _review(msg)
    assert reason and "handoff" in reason


def test_text_outside_the_card_is_sent_back():
    reason = _review("Pushed the fix.\n\n" + GOOD)
    assert reason and "outside" in reason


def test_card_rules_apply_unfinished_work_in_a_done_card():
    reason = _review(_card(watch_for=["one caveat: still untested on the phone"]))
    assert reason and "unfinished work" in reason


def test_never_more_than_max_sendbacks():
    assert _review("no card at all", continued=True, sendbacks=st.MAX_SENDBACKS) is None


@pytest.mark.parametrize("msg", [
    "API Error: 529 Overloaded",
    "You've hit your session limit · resets 5pm (America/New_York)",
    "",
    None,
])
def test_errors_and_empty_messages_are_never_sent_back(msg):
    assert st.review(msg, continued=False, sendbacks=0) is None


# ── The Claude backend registers the hook and the prompt ────────────────

def test_backend_registers_stop_after_precompact_and_appends_prompt():
    from daemon.backends.claude import ClaudeAgentSDK
    from daemon.backends.base import SessionOptions

    async def pre(*a):
        return {}

    async def turn_end(message=None, continued=False, background_in_flight=False):
        return "go back" if message == "bad" else None

    client = asyncio.run(ClaudeAgentSDK().create_session(SessionOptions(
        pre_compact_callback=pre, turn_end_callback=turn_end,
        append_system_prompt=st.INSTRUCTIONS)))
    opts = client.options
    assert list(opts.hooks) == ["PreCompact", "Stop"]   # hook_0 stays PreCompact
    assert opts.append_system_prompt == st.INSTRUCTIONS
    stop = opts.hooks["Stop"][0].hooks[0]
    assert asyncio.run(stop({"last_assistant_message": "bad"}, None, None)) == \
        {"decision": "block", "reason": "go back"}
    assert asyncio.run(stop({"last_assistant_message": "fine"}, None, None)) == {}


def test_backend_hook_lets_the_turn_end_when_the_callback_raises():
    from daemon.backends.claude import ClaudeAgentSDK
    from daemon.backends.base import SessionOptions

    async def boom(**kw):
        raise RuntimeError("x")

    client = asyncio.run(ClaudeAgentSDK().create_session(
        SessionOptions(turn_end_callback=boom)))
    stop = client.options.hooks["Stop"][0].hooks[0]
    assert asyncio.run(stop({"last_assistant_message": "x"}, None, None)) == {}


# ── SessionManager: preference, Stop button, session types ──────────────

@pytest.fixture
def sm(monkeypatch):
    m = SessionManager()
    info = SessionInfo(session_id="sid-1")
    with m._lock:
        m._sessions["sid-1"] = info
    monkeypatch.setattr(SessionManager, "_smart_termination_on",
                        staticmethod(lambda: True))
    return m, info


def _fire(m, **kw):
    cb = m._make_turn_end_callback("sid-1")
    return asyncio.run(cb(**kw))


def test_manager_sends_back_and_counts(sm):
    m, info = sm
    assert _fire(m, message="done", continued=False)
    assert info._st_sendbacks == 1
    assert _fire(m, message="done", continued=True)
    assert _fire(m, message="done", continued=True) is None   # capped
    assert _fire(m, message="done", continued=False)          # new stop resets


def test_manager_lets_a_queued_message_go_out_at_once(sm):
    # The owner, 2026-10-27: "if i queued it, i want that to occur and the
    # ai to adapt to me".  A send-back would hold the queued message behind
    # a card rewrite nobody reads.
    m, info = sm
    m._mq._queues["sid-1"] = ["actually, do it the other way"]
    try:
        assert _fire(m, message="done", continued=False) is None
    finally:
        m._mq._queues.pop("sid-1", None)
    assert _fire(m, message="done", continued=False)   # queue empty: checked again


def test_manager_respects_the_preference(sm, monkeypatch):
    m, _ = sm
    monkeypatch.setattr(SessionManager, "_smart_termination_on",
                        staticmethod(lambda: False))
    assert _fire(m, message="done") is None
    assert m._smart_termination_prompt("") is None


def test_manager_never_sends_back_a_session_the_user_stopped(sm):
    m, info = sm
    info._interrupted = True
    assert _fire(m, message="done") is None


@pytest.mark.parametrize("stype", ["planner", "title"])
def test_utility_sessions_are_left_alone(sm, stype):
    m, info = sm
    info.session_type = stype
    assert _fire(m, message="done") is None
    assert m._smart_termination_prompt(stype) is None
    assert m._smart_termination_prompt("") == st.INSTRUCTIONS


def test_preference_is_opt_in():
    from common.config import _kanban_config_defaults
    assert _kanban_config_defaults()["smart_termination"] is False


# ── The send-back is drawn as a card, never as the user's bubble ────────
# The CLI injects a Stop hook's reason as a user-role message starting with
# "Stop hook feedback:" (seen live 2026-10-27).  Every timeline parser turns
# it into a ``sent_back`` entry (common/transcripts.sent_back_entry).

import json

from common import transcripts as tx

FEEDBACK = tx.STOP_HOOK_FEEDBACK_PREFIX + "\n"
NOCARD_REASON = st.review("Done, pushed it.", continued=False, sendbacks=0)
OUTSIDE_REASON = st.review("Pushed.\n\n" + GOOD, continued=False, sendbacks=0)
UNFINISHED_REASON = st.review(_card(watch_for=["one caveat: still untested on the phone"]),
                              continued=False, sendbacks=0)
# Older send-back wordings still in transcripts from earlier today.
LEGACY_THINK_AGAIN = ('Smart termination: before you stop, think again. Your message mentions '
                      'unfinished work ("still broken", "untested").\n\nEnd your final message with this block')
LEGACY_LABELS = 'Smart termination: It uses labels the user may not know ("B or C", "point 5").'
MISSING_REASON = NOCARD_REASON


@pytest.mark.parametrize("reason,chip", [
    (NOCARD_REASON, "No response"),
    (OUTSIDE_REASON, "Text outside the response"),
    (UNFINISHED_REASON, "Unfinished work"),
    (LEGACY_THINK_AGAIN, "Unfinished work"),
    (LEGACY_LABELS, "Unexplained labels"),
    ("some other hook said no", "Sent back"),
])
def test_every_send_back_reason_gets_a_chip_and_a_plain_sentence(reason, chip):
    e = tx.sent_back_entry(FEEDBACK + reason)
    assert e["kind"] == "sent_back" and e["name"] == chip
    assert e["desc"] and "\n" not in e["desc"]
    if reason.startswith("Smart termination:"):
        # Just the reason: no prefix, no format reminder, no markdown stars.
        assert not e["text"].startswith("Smart termination")
        assert "# Ending a turn" not in e["text"] and "End your final message" not in e["text"]
        assert not e["text"][0].islower()
    else:
        assert e["text"] == reason.strip()


def test_unfinished_summary_quotes_what_it_said():
    e = tx.sent_back_entry(FEEDBACK + UNFINISHED_REASON)
    assert '"caveat"' in e["desc"] or '"untested"' in e["desc"]


def test_ordinary_text_is_not_a_send_back():
    assert tx.sent_back_entry("please fix the Stop hook feedback: thing") is None
    assert tx.sent_back_entry("") is None


def test_a_send_back_never_counts_as_the_users_words():
    from app.platform_utils import is_system_user_content
    text = FEEDBACK + UNFINISHED_REASON
    assert tx.is_system_content(text)               # engine: resends, user-message checks
    assert tx.is_timeline_system_content(text)      # web timeline and titles
    assert is_system_user_content(text)


def _line(content):
    return json.dumps({"type": "user", "timestamp": "2026-10-27T22:00:00Z", "uuid": "u1",
                       "message": {"role": "user", "content": content}})


@pytest.mark.parametrize("content", [
    FEEDBACK + UNFINISHED_REASON,
    [{"type": "text", "text": FEEDBACK + UNFINISHED_REASON}],
])
def test_web_parsers_emit_the_card(content):
    from app.routes.ws_events import _parse_raw_line
    entries = _parse_raw_line(_line(content))
    assert [e["kind"] for e in entries] == ["sent_back"]
    assert entries[0]["name"] == "Unfinished work"


def test_live_api_tail_emits_the_card():
    import inspect
    from app.routes import live_api
    src = inspect.getsource(live_api)
    assert src.count("if _sent_back_entry(text):") == 2


def test_engine_transcript_reader_emits_the_card(tmp_path, monkeypatch):
    from daemon.backends.claude_store import ClaudeJsonlStore
    p = tmp_path / "s.jsonl"
    asst = json.dumps({"type": "assistant", "timestamp": "2026-10-27T21:59:59Z", "uuid": "a1",
                       "message": {"role": "assistant", "content": [{"type": "text", "text": "Done."}]}})
    p.write_text(asst + "\n" + _line([{"type": "text", "text": FEEDBACK + MISSING_REASON}]) + "\n")
    store = ClaudeJsonlStore()
    monkeypatch.setattr(store, "find_session_path", lambda sid, cwd="": p)
    rows = store.read_log_entries("s")
    assert [r["kind"] for r in rows] == ["asst", "sent_back"]
    assert rows[1]["name"] == "No response"


def test_engine_live_stream_emits_the_card():
    from daemon.backends.messages import VibeNodeMessage, MessageKind, BlockKind
    m = SessionManager()
    info = SessionInfo(session_id="sid-1")
    with m._lock:
        m._sessions["sid-1"] = info
    pushed = []
    m._push_callback = lambda evt, data: pushed.append((evt, data))
    msg = VibeNodeMessage(kind=MessageKind.USER, blocks=[
        {"kind": BlockKind.TEXT.value, "text": FEEDBACK + UNFINISHED_REASON}])
    asyncio.run(m._process_message("sid-1", msg))
    assert info.entries[-1].kind == "sent_back"
    assert info.entries[-1].name == "Unfinished work"
    sent = [d for evt, d in pushed if evt == "session_entry"]
    assert sent and sent[-1]["entry"]["kind"] == "sent_back"
    assert sent[-1]["entry"]["desc"]


# ── The settings-file hook (daemon/smart_termination_hook.py) ───────────
# Same rules, loaded by the CLI from ~/.claude/settings.json, so a CLI an
# older Session Engine starts gets them with no engine restart.

from daemon import smart_termination_hook as hook

_REAL_HAS_QUEUED = hook._has_queued


@pytest.fixture
def hook_env(monkeypatch, tmp_path):
    monkeypatch.setenv("CLAUDE_CODE_ENTRYPOINT", "sdk-py")
    monkeypatch.delenv("VIBENODE_SMART_TERMINATION_ENGINE", raising=False)
    monkeypatch.setattr(hook.tempfile, "gettempdir", lambda: str(tmp_path))
    monkeypatch.setattr(hook, "_registry_type", lambda sid: "")
    monkeypatch.setattr(hook, "_has_queued", lambda sid: False)
    import common.config as cc
    state = {"on": True}
    monkeypatch.setattr(cc, "get_kanban_config", lambda: {"smart_termination": state["on"]})
    return state


def _stop(msg, continued=False, sid="s1", cwd="/home/x/proj"):
    return hook.handle({"hook_event_name": "Stop", "session_id": sid, "cwd": cwd,
                        "last_assistant_message": msg, "stop_hook_active": continued})


def test_hook_sends_back_and_lets_good_handoffs_through(hook_env):
    out = _stop("Done, pushed it.")
    assert out["decision"] == "block" and "handoff" in out["reason"]
    assert _stop(GOOD) is None


def test_hook_caps_send_backs_per_stop(hook_env):
    assert _stop("x")
    assert _stop("x", continued=True)
    assert _stop("x", continued=True) is None
    assert _stop("x")                       # a new stop starts over


def test_hook_session_start_adds_the_instructions(hook_env):
    out = hook.handle({"hook_event_name": "SessionStart", "session_id": "s1", "cwd": "/p"})
    assert out["hookSpecificOutput"]["additionalContext"] == st.INSTRUCTIONS


def test_hook_is_silent_when_off_or_not_ours(hook_env, monkeypatch):
    hook_env["on"] = False
    assert _stop("Done.") is None
    hook_env["on"] = True
    import os
    assert _stop("Done.", cwd=os.path.expanduser("~/.claude/_system")) is None
    monkeypatch.setattr(hook, "_registry_type", lambda sid: "planner")
    assert _stop("Done.") is None
    monkeypatch.setattr(hook, "_registry_type", lambda sid: "")
    monkeypatch.setenv("VIBENODE_SMART_TERMINATION_ENGINE", "1")
    assert _stop("Done.") is None            # the engine runs the rules itself
    monkeypatch.delenv("VIBENODE_SMART_TERMINATION_ENGINE")
    monkeypatch.setenv("CLAUDE_CODE_ENTRYPOINT", "cli")
    assert _stop("Done.") is None            # a terminal claude, not VibeNode


def _prompt(sid="s1", transcript=None):
    return hook.handle({"hook_event_name": "UserPromptSubmit", "session_id": sid,
                        "cwd": "/home/x/proj", "prompt": "hi",
                        "transcript_path": str(transcript or "/nonexistent")})


def _ctx(out):
    return out and out["hookSpecificOutput"]["additionalContext"]


def test_switch_flips_reach_a_running_hook_session(hook_env):
    # The owner, 2026-10-28: flipping the switch must change how a running
    # session ends its turns, with no restart or refresh.
    hook_env["on"] = False
    assert hook.handle({"hook_event_name": "SessionStart", "session_id": "s1", "cwd": "/p"}) is None
    assert _prompt() is None                         # off and never told: nothing
    hook_env["on"] = True
    out = _prompt()
    assert out["hookSpecificOutput"]["hookEventName"] == "UserPromptSubmit"
    assert _ctx(out).startswith(hook.TURNED_ON) and st.INSTRUCTIONS in _ctx(out)
    assert _prompt() is None                         # told once, not every message
    hook_env["on"] = False
    assert _ctx(_prompt()) == hook.TURNED_OFF
    assert _prompt() is None


def test_switch_flips_reach_an_engine_session(hook_env, monkeypatch):
    # The engine bakes the rules into the system prompt at launch.
    monkeypatch.setenv("VIBENODE_SMART_TERMINATION_ENGINE", "1")
    start = {"hook_event_name": "SessionStart", "session_id": "e1", "cwd": "/p"}
    assert hook.handle(dict(start, source="startup")) is None   # system prompt has them
    assert _prompt("e1") is None
    hook_env["on"] = False
    assert _ctx(_prompt("e1")) == hook.TURNED_OFF
    # A compaction keeps the launch's system prompt (rules on) and may drop
    # the "turned off" note, so it is said again.
    assert _ctx(hook.handle(dict(start, source="compact"))) == hook.TURNED_OFF
    assert _prompt("e1") is None
    # A resume is a new CLI the engine launched with the switch as it is now.
    assert hook.handle(dict(start, source="resume")) is None
    hook_env["on"] = True
    assert st.INSTRUCTIONS in _ctx(_prompt("e1"))
    # The Stop rules stay the engine's: the hook never reviews its sessions.
    assert _stop("Done.", sid="e1") is None


def test_a_session_with_no_record_is_judged_from_its_transcript(hook_env, tmp_path):
    with_cards = tmp_path / "a.jsonl"
    with_cards.write_text('{"text": "```handoff\\n{}\\n```"}\n')
    plain = tmp_path / "b.jsonl"
    plain.write_text('{"text": "Done."}\n')
    assert _prompt("a", with_cards) is None          # on, already writing cards
    assert st.INSTRUCTIONS in _ctx(_prompt("b", plain))
    hook_env["on"] = False
    assert _ctx(_prompt("c", with_cards)) == hook.TURNED_OFF
    assert _prompt("d", plain) is None


def test_a_card_at_a_stop_counts_as_told(hook_env):
    hook_env["on"] = False
    assert hook.handle({"hook_event_name": "SessionStart", "session_id": "s1", "cwd": "/p"}) is None
    _stop(GOOD)                                      # e.g. flipped on and off mid-turn
    assert _ctx(_prompt()) == hook.TURNED_OFF


def test_prompt_hook_is_silent_outside_vibenode(hook_env, monkeypatch):
    monkeypatch.setenv("CLAUDE_CODE_ENTRYPOINT", "cli")
    assert _prompt() is None
    monkeypatch.setenv("CLAUDE_CODE_ENTRYPOINT", "sdk-py")
    monkeypatch.setattr(hook, "_registry_type", lambda sid: "title")
    assert _prompt() is None


def test_hook_install_keeps_other_settings_and_uninstalls_cleanly(tmp_path):
    p = tmp_path / "settings.json"
    other = {"type": "command", "command": "echo hi"}
    p.write_text(json.dumps({"model": "x", "hooks": {"Stop": [{"hooks": [other]}]}}))
    hook.install(p)
    hook.install(p)                          # idempotent
    cfg = json.loads(p.read_text())
    assert cfg["model"] == "x"
    cmds = [h["command"] for g in cfg["hooks"]["Stop"] for h in g["hooks"]]
    assert cmds.count("echo hi") == 1 and sum("smart_termination_hook.py" in c for c in cmds) == 1
    assert "SessionStart" in cfg["hooks"] and "UserPromptSubmit" in cfg["hooks"]
    hook.install(p, remove=True)
    cfg = json.loads(p.read_text())
    assert cfg["hooks"] == {"Stop": [{"hooks": [other]}]}


def test_installed_hook_command_runs_under_bash(tmp_path):
    """The CLI runs hook commands through bash, Git Bash on Windows.  An
    unquoted backslash path lost its backslashes there ("command not found",
    exit 127), so the installed copy never ran (2026-10-28)."""
    import os
    import shutil
    import subprocess
    cmd = hook._command()
    assert "\\" not in cmd, cmd
    bash = shutil.which("bash")
    if not bash:
        pytest.skip("no bash on this machine")
    env = dict(os.environ, CLAUDE_CODE_ENTRYPOINT="sdk-py")
    env.pop("VIBENODE_SMART_TERMINATION_ENGINE", None)
    r = subprocess.run([bash, "-c", cmd], input=json.dumps({"hook_event_name": "Notification"}),
                       capture_output=True, text=True, env=env, timeout=60)
    assert r.returncode == 0, (r.returncode, r.stderr)


def test_engine_tells_the_settings_hook_it_handles_the_rules():
    from daemon.backends.claude import ClaudeAgentSDK
    from daemon.backends.base import SessionOptions

    async def te(**kw):
        return None
    client = asyncio.run(ClaudeAgentSDK().create_session(SessionOptions(turn_end_callback=te)))
    assert client.options.env.get("VIBENODE_SMART_TERMINATION_ENGINE") == "1"


def test_hook_lets_a_queued_message_go_out_at_once(hook_env, monkeypatch, tmp_path):
    home = tmp_path / "home"
    (home / ".claude").mkdir(parents=True)
    monkeypatch.setattr(hook.Path, "home", classmethod(lambda cls: home))
    monkeypatch.setattr(hook, "_has_queued", _REAL_HAS_QUEUED)
    qfile = home / ".claude" / "gui_message_queues.json"
    qfile.write_text(json.dumps({"s1": ["actually, do it the other way"]}))
    assert _stop("Done, pushed it.") is None
    assert _stop("Done, pushed it.", sid="other")["decision"] == "block"
    qfile.write_text(json.dumps({}))
    assert _stop("Done, pushed it.")["decision"] == "block"
    qfile.write_text("not json")
    assert _stop("Done, pushed it.")["decision"] == "block"   # unreadable: checked as usual


def test_instructions_tell_the_session_to_adapt_to_a_queued_message():
    assert "queued message" in st.INSTRUCTIONS
    assert "replaced" in st.INSTRUCTIONS



# ── Big picture / this task / just now (owner's pick A, 2026-10-28) ─────

def test_a_card_without_zoom_is_sent_back():
    reason = _review(_card(zoom=None))
    assert reason and '"zoom"' in reason


@pytest.mark.parametrize("key", ["big", "task", "now"])
def test_each_zoom_line_is_required_and_short(key):
    z = {"big": "Ship it.", "task": "Fix the header.", "now": "Fixed it."}
    assert _review(_card(zoom=dict(z, **{key: ""}))) and "zoom." + key in _review(_card(zoom=dict(z, **{key: ""})))
    long = dict(z, **{key: "x" * (st.ZOOM_MAX_CHARS + 1)})
    assert "zoom." + key in (_review(_card(zoom=long)) or "")
    assert _review(_card(zoom=dict(z, **{key: "x" * st.ZOOM_MAX_CHARS}))) is None


def test_zoom_cap_is_in_characters_not_words():
    # Five long words pass a word cap and still wrap on a phone.
    z = {"big": "Ship it.", "task": "Fix the header.",
         "now": "Reorganised internationalisation infrastructure comprehensively"}
    assert "characters" in (_review(_card(zoom=z)) or "")


def test_zoom_does_not_count_against_the_card_word_limit():
    from daemon.handoff_rules import length_problems
    import json as j
    card = j.loads(_card(zoom={"big": " ".join(["w"] * 20), "task": " ".join(["w"] * 20), "now": " ".join(["w"] * 20)})[11:-4])
    assert length_problems(card) == []


def test_screenshots_go_inside_the_card_not_beside_it():
    marker = '[[VN-PREVIEW {"type":"image","name":"x","src":"/api/preview/asset/abc"}]]\n\n'
    assert "outside" in (_review(marker + GOOD) or "")
    assert _review(GOOD) is None


# ── "Explain more" (owner's pick B, static/scratch/card-lost.html, 2026-10-28) ──

def test_every_card_carries_its_long_version():
    reason = _review(_card(explained=None))
    assert reason and '"explained"' in reason and "Explain more" in reason
    short = _review(_card(explained="It was broken; now it works."))
    assert short and "at least" in short
    assert _review(GOOD) is None


def test_the_long_version_is_not_counted_in_the_card_length():
    from daemon.handoff_rules import length_problems
    import json as j
    card = j.loads(_card(explained=" ".join(["word"] * 600))[11:-4])
    assert length_problems(card) == []


def test_the_instructions_ask_for_the_long_version():
    assert '"explained"' in st.INSTRUCTIONS and "Explain more" in st.INSTRUCTIONS
