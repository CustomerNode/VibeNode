"""The person's typed words never reach a tracked file (src/shared/user_words.py).

Every message here is made up; the transcripts and the repository are built in
a temporary folder.
"""
from __future__ import annotations

import json
import subprocess

import pytest

from shared import user_words as uw

TYPED = "honestly the purple ribbon on the tea tray looks wrong to me today"
APP_LINE = "The kettle server is warming up and will answer shortly"


def _transcript(path, *messages, kind="user", **extra):
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a", encoding="utf-8") as fh:
        for m in messages:
            fh.write(json.dumps({"type": kind, "message": {"role": kind, "content": m}, **extra}) + "\n")


def _repo(tmp_path, files):
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    for name, text in files.items():
        (repo / name).write_text(text, encoding="utf-8")
    subprocess.run(["git", "-C", str(repo), "add", "-A"], check=True)
    subprocess.run(["git", "-C", str(repo), "-c", "user.email=t@example.com", "-c", "user.name=T",
                    "commit", "-q", "--no-verify", "-m", "start"], check=True)
    return repo


@pytest.fixture
def world(tmp_path):
    base = tmp_path / "projects"
    index = tmp_path / "data" / "user_words.json"
    repo = _repo(tmp_path, {"ui.js": f"show('{APP_LINE}');\n", "readme.md": "hello\n"})
    return base, index, repo


def _known(base, index, repo):
    return frozenset(uw.refresh(base=base, index=index, repo=repo)["runs"])


def test_a_typed_message_is_found_in_any_file(world):
    base, index, repo = world
    _transcript(base / "proj-a" / "s1.jsonl", TYPED)
    known = _known(base, index, repo)
    hit = uw.findings("x = 1\n# note: the purple ribbon on the tea tray looks wrong\n", known)
    assert [f["line"] for f in hit] == [2]
    assert hit[0]["type"] == "Your Own Words"


def test_messages_from_every_session_count(world):
    base, index, repo = world
    _transcript(base / "proj-a" / "s1.jsonl", "short one")
    _transcript(base / "proj-b" / "s2.jsonl", TYPED)
    assert uw.findings(TYPED, _known(base, index, repo))


def test_neutral_text_and_short_overlaps_pass(world):
    base, index, repo = world
    _transcript(base / "proj-a" / "s1.jsonl", TYPED)
    known = _known(base, index, repo)
    assert not uw.findings("# the ribbon colour on the tray is checked against the theme\n", known)
    assert not uw.findings("the tea tray looks wrong", known)      # five words


def test_app_text_pasted_back_is_not_theirs(world):
    base, index, repo = world
    _transcript(base / "proj-a" / "s1.jsonl", f"i keep getting {APP_LINE} what now")
    known = _known(base, index, repo)
    assert not uw.findings(f"msg = '{APP_LINE}'", known)


def test_composed_messages_code_and_tool_results_are_not_typed_words(world):
    base, index, repo = world
    f = base / "proj-a" / "s1.jsonl"
    _transcript(f, "Wrong kind of card: the lavender scones were stacked far too high again")
    _transcript(f, "look at this\n```\nthe lavender scones were stacked far too high\n```")
    _transcript(f, "the lavender scones were stacked far too high once more", isMeta=True)
    _transcript(f, [{"type": "tool_result", "content": "the lavender scones were stacked far too high"}])
    _transcript(f, "the lavender scones were stacked far too high", kind="assistant")
    assert not uw.findings("the lavender scones were stacked far too high", _known(base, index, repo))


def test_only_new_bytes_are_read_and_a_rewrite_is_read_again(world):
    base, index, repo = world
    f = base / "proj-a" / "s1.jsonl"
    _transcript(f, TYPED)
    _known(base, index, repo)
    _transcript(f, "the green kettle whistles every single morning at seven sharp")
    known = _known(base, index, repo)
    assert uw.findings(TYPED, known) and uw.findings("green kettle whistles every single morning", known)
    f.write_text("", encoding="utf-8")
    _transcript(f, "a brand new paper lantern hangs over the porch")
    assert uw.findings("brand new paper lantern hangs over", _known(base, index, repo))


def test_escaped_line_breaks_do_not_hide_a_run(world):
    base, index, repo = world
    _transcript(base / "proj-a" / "s1.jsonl", TYPED)
    known = _known(base, index, repo)
    assert uw.findings('"intro\\nthe purple ribbon on the tea tray looks wrong"', known)


def test_the_kill_switch_turns_it_off(monkeypatch):
    monkeypatch.setenv("VIBENODE_USER_WORDS_GUARD", "0")
    assert uw.findings(TYPED) == []


def test_the_publish_scan_reports_them(world, monkeypatch):
    from web import git_scanner
    base, index, repo = world
    _transcript(base / "proj-a" / "s1.jsonl", TYPED)
    known = _known(base, index, repo)
    monkeypatch.setattr(uw, "findings", lambda content, _k=None: _orig_findings(content, known))
    out = git_scanner.scan_text("src/thing.py", "# " + TYPED + "\n")
    assert any(f["type"] == "Your Own Words" for f in out)


def test_the_index_file_is_never_published():
    from pathlib import Path
    root = Path(uw.__file__).resolve().parents[2]
    r = subprocess.run(["git", "-C", str(root), "check-ignore", "-q", "data/user_words.json"])
    assert r.returncode == 0


_orig_findings = uw.findings


def test_a_tapped_summary_button_is_not_typed_words(world):
    base, index, repo = world
    f = base / "proj-a" / "s1.jsonl"
    card = '```handoff\n{"needs": {"replies": [{"key": "A", "reply": "Build the striped awning with a folding frame."}]}}\n```'
    _transcript(f, [{"type": "text", "text": card}], kind="assistant")
    _transcript(f, "Build the striped awning with a folding frame.")
    assert not uw.findings("striped awning with a folding frame", _known(base, index, repo))
