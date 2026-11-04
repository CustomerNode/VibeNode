"""Project add-ons for the end-of-turn stop (src/engine/stop_plugins).

A project plugs its own checks into smart termination without editing the stop's rules. These tests use made-up
add-ons only: a real project's add-on lives in that project, never in this public repository."""
import hashlib
import json
import textwrap

import pytest

from engine import smart_termination as st
from engine import stop_plugins as sp
from engine import handoff_rules as rules

PASSING = '''
NAME = "Demo"
UNCOUNTED_KEYS = ("demo_block",)
def applies(card, facts):
    return "demo" in str(card.get("title", "")).lower()
def problems(card, message, facts):
    return [] if card.get("demo_block") == "ok" else ["the demo block is missing"]
'''


def setup(tmp_path, files: dict, pin=None, when=None):
    folder = tmp_path / "addons"
    folder.mkdir()
    for name, src in files.items():
        (folder / name).write_text(textwrap.dedent(src), encoding="utf-8")
    entry = {"dir": str(folder)}
    if pin is not None:
        pin_path = tmp_path / "pin.json"
        pin_path.write_text(json.dumps({"files": pin}), encoding="utf-8")
        entry["pin"] = str(pin_path)
    if when is not None:
        entry["when"] = when
    cfg = tmp_path / "stop_plugins.json"
    cfg.write_text(json.dumps({"folders": [entry]}), encoding="utf-8")
    return cfg, folder


def sha(folder, name):
    return hashlib.sha256((folder / name).read_bytes()).hexdigest()


def test_no_config_means_no_add_ons(tmp_path):
    assert sp.problems({"title": "x"}, "", None, config=tmp_path / "missing.json") == []


def test_an_add_on_sends_back_only_the_cards_it_applies_to(tmp_path):
    cfg, _ = setup(tmp_path, {"demo.py": PASSING})
    assert sp.problems({"title": "Demo card"}, "", None, config=cfg) == ["[Demo] the demo block is missing"]
    assert sp.problems({"title": "Demo card", "demo_block": "ok"}, "", None, config=cfg) == []
    assert sp.problems({"title": "Other"}, "", None, config=cfg) == []


def test_an_add_on_fails_closed(tmp_path):
    cfg, _ = setup(tmp_path, {
        "boom_applies.py": "NAME='A'\ndef applies(c, f):\n    raise RuntimeError('x')\ndef problems(c, m, f):\n    return []\n",
        "boom_problems.py": "NAME='B'\ndef applies(c, f):\n    return True\ndef problems(c, m, f):\n    raise KeyError('y')\n",
        "broken.py": "this is not python(\n",
    })
    out = sp.problems({"title": "x"}, "", None, config=cfg)
    assert any(p.startswith("[A] could not tell") for p in out)
    assert any(p.startswith("[B] the add-on failed") for p in out)
    assert any(p.startswith("[broken] this project's stop add-on could not be loaded") for p in out)


def test_a_pinned_folder_loads_only_the_approved_bytes(tmp_path):
    cfg, folder = setup(tmp_path, {"demo.py": PASSING}, pin={})
    out = sp.problems({"title": "Demo"}, "", None, config=cfg)
    assert out and "does not match its approved pin" in out[0]
    pin = {"demo.py": sha(folder, "demo.py")}
    (tmp_path / "pin.json").write_text(json.dumps({"files": pin}), encoding="utf-8")
    assert sp.problems({"title": "Demo"}, "", None, config=cfg) == ["[Demo] the demo block is missing"]
    (folder / "demo.py").write_text(PASSING.replace("missing", "absent"), encoding="utf-8")
    assert "does not match its approved pin" in sp.problems({"title": "Demo"}, "", None, config=cfg)[0]


def test_an_approved_file_that_vanished_is_reported(tmp_path):
    cfg, folder = setup(tmp_path, {"demo.py": PASSING})
    pin = {"demo.py": sha(folder, "demo.py"), "gone.py": "0" * 64}
    (tmp_path / "pin.json").write_text(json.dumps({"files": pin}), encoding="utf-8")
    data = json.loads(cfg.read_text())
    data["folders"][0]["pin"] = str(tmp_path / "pin.json")
    cfg.write_text(json.dumps(data))
    assert any("approved but missing" in p for p in sp.problems({"title": "Demo", "demo_block": "ok"}, "", None,
                                                                 config=cfg))


def test_when_keeps_a_broken_add_on_inside_its_own_project(tmp_path):
    cfg, _ = setup(tmp_path, {"broken.py": "nope(\n"}, when=["projectx"])
    assert sp.problems({"title": "Another project"}, "", None, config=cfg) == []
    assert sp.problems({"title": "ProjectX card"}, "", None, config=cfg)


def test_the_stop_sends_back_on_an_add_on_problem_even_after_the_polish_tries(tmp_path, monkeypatch):
    # Prime the hot-reload (st._stop_plugins) once: a "first ever" call reloads
    # the loader to pick up a scoping fix an older engine would have missed, and
    # that reload resets the module's attributes.  Running engines call this
    # before any monkeypatching could happen; the test matches that reality.
    st._stop_plugins()
    cfg, _ = setup(tmp_path, {"demo.py": PASSING})
    monkeypatch.setattr(sp, "CONFIG", cfg)
    card = {"type": "explain", "title": "Demo"}
    msg = "```handoff\n" + json.dumps(card) + "\n```"
    reason = st.review(msg, continued=False, sendbacks=0, self_check=False)
    assert reason and "the demo block is missing" in reason
    late = st.review(msg, continued=True, sendbacks=st.MAX_SENDBACKS, self_check=False)
    assert late and "the demo block is missing" in late


def test_an_add_ons_own_keys_are_outside_the_120_words(tmp_path, monkeypatch):
    cfg, _ = setup(tmp_path, {"demo.py": PASSING})
    monkeypatch.setattr(sp, "CONFIG", cfg)
    table = rules.word_table({"title": "Demo", "demo_block": "word " * 200})
    assert [r for r in table if r[0] == "demo_block"][0][2] is False


# --- Scope follows the session's own work, never a word it happened to use ---

from engine import handoff_evidence as ev


def _facts(*details, tool="Read"):
    return ev.TurnFacts(actions=[ev.Action(tool=tool, detail=d, ok=True) for d in details])


def test_a_word_inside_a_command_does_not_pull_a_session_in(tmp_path):
    # A voice mockup's sample sentence said "importer"; another project's checks held the session.
    cfg, _ = setup(tmp_path, {"broken.py": "nope(\n"}, when=["projectx", "importer"])
    facts = _facts("python - <<'EOF'\nSAID = 'make sure the importer check passes'\nEOF", tool="Bash")
    assert sp.problems({"title": "Voice buttons"}, "", facts, config=cfg) == []


def test_a_card_that_only_mentions_the_project_is_not_its_work(tmp_path):
    cfg, _ = setup(tmp_path, {"broken.py": "nope(\n"}, when=["projectx"])
    card = {"title": "Voice buttons", "headline": "ProjectX checks keep holding this card",
            "finding": {"what": "ProjectX checks pick up unrelated sessions"}}
    assert sp.problems(card, "", None, config=cfg) == []


def test_reading_a_projects_add_on_files_is_not_working_in_it(tmp_path):
    cfg, _ = setup(tmp_path, {"broken.py": "nope(\n"}, when=["projectx"])
    facts = _facts('ls "C:/work/ProjectX/.vibenode/stop_plugins"', "C:/work/ProjectX/.vibenode/stop_plugins.pin.json")
    assert sp.problems({"title": "Voice buttons"}, "", facts, config=cfg) == []


def test_the_projects_own_sessions_stay_covered(tmp_path):
    cfg, _ = setup(tmp_path, {"broken.py": "nope(\n"}, when=["projectx"])
    # by the card's own name
    assert sp.problems({"title": "Report", "path": ["ProjectX", "Weekly run"]}, "", None, config=cfg)
    assert sp.problems({"title": "Report", "zoom": {"big": "ProjectX weekly proof"}}, "", None, config=cfg)
    # by the files its tool calls touched
    facts = _facts(r"file_path: C:\work\ProjectX\tools\run.py", tool="Edit")
    assert sp.problems({"title": "Report"}, "", facts, config=cfg)


def test_the_running_engine_picks_up_a_changed_loader(monkeypatch, tmp_path):
    """smart_termination reloads live; it must refresh the loader too when the loader's file changes."""
    import os
    calls = []
    real = __import__("importlib").reload
    monkeypatch.setattr("importlib.reload", lambda m: calls.append(m) or m)
    sp._vn_mtime = None                      # loaded before anyone marked it: may be old
    st._stop_plugins()
    assert calls == [sp]
    st._stop_plugins()                       # marked and unchanged: no reload
    assert calls == [sp]
    calls.clear()
    sp._vn_mtime = -1                        # the file on disk is now different
    assert st._stop_plugins() is sp and calls == [sp]
    assert sp._vn_mtime == os.stat(sp.__file__).st_mtime_ns
    monkeypatch.setattr("importlib.reload", real)


def test_a_path_inside_a_shell_command_does_not_pull_a_session_in(tmp_path):
    """A demo script listing another project's file is not work in that project."""
    cfg, _ = setup(tmp_path, {"broken.py": "nope(\n"}, when=["projectx"])
    detail = "python - <<EOF\ncases = ['C:/work/ProjectX/tools/run.py']\nEOF"
    facts = ev.TurnFacts(actions=[ev.Action(tool="Bash", ok=True, detail=detail)])
    assert sp.problems({"title": "Report"}, "", facts, config=cfg) == []


# --- The folder decides first ---

def _in(cwd, *details, tool="Read"):
    f = _facts(*details, tool=tool)
    f.cwd = cwd
    return f


def test_a_session_outside_the_projects_folder_is_never_checked(tmp_path):
    cfg, folder = setup(tmp_path, {"broken.py": "nope(\n"}, when=["projectx"])
    data = json.loads(cfg.read_text())
    data["folders"][0]["projects"] = ["C:/work/ProjectX"]
    cfg.write_text(json.dumps(data))
    # Even a card named for the project, editing the project's files, from another folder: not checked.
    card = {"title": "ProjectX report", "path": ["ProjectX"]}
    facts = _in(r"C:\work\OtherApp", r"file_path: C:\work\ProjectX\tools\run.py", tool="Edit")
    assert sp.problems(card, "", facts, config=cfg) == []
    # A sibling folder that merely starts with the same letters is outside too.
    assert sp.problems(card, "", _in("C:/work/ProjectX-old"), config=cfg) == []
    # Inside the folder (any depth, any case or slash style), the add-on runs.
    assert sp.problems(card, "", _in(r"c:\WORK\projectx\sub"), config=cfg)


def test_without_listed_folders_the_folder_holding_vibenode_is_the_project(tmp_path):
    proj = tmp_path / "ProjectX"
    addons = proj / ".vibenode" / "stop_plugins"
    addons.mkdir(parents=True)
    (addons / "broken.py").write_text("nope(\n", encoding="utf-8")
    cfg = tmp_path / "stop_plugins.json"
    cfg.write_text(json.dumps({"folders": [{"dir": str(addons)}]}))
    card = {"title": "Report"}
    assert sp.problems(card, "", _in(str(tmp_path / "OtherApp")), config=cfg) == []
    assert sp.problems(card, "", _in(str(proj)), config=cfg)


def test_a_session_whose_folder_is_unknown_is_not_checked(tmp_path):
    """Fail closed: the folder must be the one recorded under the session's id; no folder, no project checks."""
    cfg, _ = setup(tmp_path, {"broken.py": "nope(\n"}, when=["projectx"])
    data = json.loads(cfg.read_text())
    data["folders"][0]["projects"] = ["C:/work/ProjectX"]
    cfg.write_text(json.dumps(data))
    card = {"title": "Report", "path": ["ProjectX"]}
    assert sp.problems(card, "", None, config=cfg) == []                 # no facts at all
    assert sp.problems(card, "", _in(""), config=cfg) == []              # facts, folder unknown
    assert sp.problems(card, "", _in("C:/work/ProjectX"), config=cfg)    # folder known and inside


