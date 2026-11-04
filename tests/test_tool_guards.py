"""Project tool guards (src/engine/tool_guards): a session's tool call is refused before it runs.

Made-up guards only: a real project's guard lives in that project, never in this public repository."""
import hashlib
import json
import textwrap

from engine import tool_guards as tg

GUARD = '''
NAME = "Demo guard"
def decide(tool_name, tool_input, cwd):
    if "forbidden-thing" in str(tool_input.get("command", "")):
        return "it does the forbidden thing"
    return None
'''


def setup(tmp_path, files, pin=None, when=None):
    folder = tmp_path / "guards"
    folder.mkdir()
    for name, src in files.items():
        (folder / name).write_text(textwrap.dedent(src), encoding="utf-8")
    entry = {"dir": str(folder)}
    if pin is not None:
        (tmp_path / "pin.json").write_text(json.dumps({"files": pin}), encoding="utf-8")
        entry["pin"] = str(tmp_path / "pin.json")
    if when is not None:
        entry["when"] = when
    cfg = tmp_path / "tool_guards.json"
    cfg.write_text(json.dumps({"folders": [entry]}), encoding="utf-8")
    return cfg, folder


def test_no_config_refuses_nothing(tmp_path):
    assert tg.decide("Bash", {"command": "forbidden-thing"}, "", config=tmp_path / "none.json") is None


def test_a_guard_refuses_what_it_names_and_nothing_else(tmp_path):
    cfg, _ = setup(tmp_path, {"g.py": GUARD})
    assert tg.decide("Bash", {"command": "echo forbidden-thing"}, "", config=cfg) == "[Demo guard] it does the forbidden thing"
    assert tg.decide("Bash", {"command": "echo fine"}, "", config=cfg) is None


def test_the_hook_answer_denies(tmp_path):
    out = tg.hook_output("[Demo guard] no")
    assert out["hookSpecificOutput"]["permissionDecision"] == "deny"
    assert tg.hook_output(None) == {}


def test_a_broken_or_unpinned_guard_refuses_inside_its_scope_only(tmp_path):
    cfg, folder = setup(tmp_path, {"g.py": "nope(\n"}, when=["projectx"])
    assert tg.decide("Bash", {"command": "ls projectx"}, "", config=cfg).startswith("[tool guard] g failed")
    assert tg.decide("Bash", {"command": "ls elsewhere"}, "", config=cfg) is None
    cfg2, folder2 = setup(tmp_path / "b", {"g.py": GUARD}, pin={"g.py": "0" * 64}) if (tmp_path / "b").mkdir() is None else (None, None)
    assert "does not match its approved pin" in tg.decide("Bash", {"command": "x"}, "", config=cfg2)
    (tmp_path / "b" / "pin.json").write_text(json.dumps({"files": {"g.py": hashlib.sha256((folder2 / "g.py").read_bytes()).hexdigest()}}))
    assert tg.decide("Bash", {"command": "x"}, "", config=cfg2) is None


def test_every_session_carries_the_guard_hook_last(tmp_path, monkeypatch):
    import asyncio
    from engine.backends.claude import ClaudeAgentSDK
    from engine.backends.base import SessionOptions
    cfg, _ = setup(tmp_path, {"g.py": GUARD})
    monkeypatch.setattr(tg, "CONFIG", cfg)

    async def pre(*a):
        return {}

    async def turn_end(**kw):
        return None
    client = asyncio.run(ClaudeAgentSDK().create_session(SessionOptions(
        pre_compact_callback=pre, turn_end_callback=turn_end)))
    hooks = client.options.hooks
    assert list(hooks) == ["PreCompact", "Stop", "PreToolUse"]
    guard = hooks["PreToolUse"][0].hooks[0]
    denied = asyncio.run(guard({"tool_name": "Bash", "tool_input": {"command": "echo forbidden-thing"}}, "t1", None))
    assert denied["hookSpecificOutput"]["permissionDecision"] == "deny"
    assert asyncio.run(guard({"tool_name": "Bash", "tool_input": {"command": "ls"}}, "t2", None)) == {}
