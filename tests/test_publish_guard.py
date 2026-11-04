"""A write the Publish scan would fail is refused when it is written (src/engine/publish_guard.py).

The triggering text is assembled at run time, so this file itself never holds what the scan looks for."""
import subprocess

from engine import publish_guard as pg
from engine import tool_guards as tg

ROOT = pg.ROOT
KEY = "AKIA" + "Q7" * 8          # AWS key shape, built here so the source stays clean


def _src(rel):
    return str(ROOT / rel)


def test_a_key_written_into_the_repo_is_refused_with_what_was_found():
    why = pg.decide("Write", {"file_path": _src("src/web/zz_probe.py"), "content": f"x = 1\nk = '{KEY}'\n"})
    assert why and "Publish scan" in why and "line 2" in why and "AWS Access Key" in why


def test_a_finding_in_a_test_fixture_is_refused_with_a_made_up_name_hint():
    why = pg.decide("Edit", {"file_path": _src("tests/test_zz_probe.py"), "old_string": "a",
                             "new_string": f"<span>{KEY}</span>"})
    assert why and "AWS Access Key" in why and "made-up" in why


def test_multiedit_and_notebook_text_are_judged():
    assert pg.decide("MultiEdit", {"file_path": _src("src/web/zz.py"),
                                   "edits": [{"old_string": "a", "new_string": "fine"},
                                             {"old_string": "b", "new_string": KEY}]})
    assert pg.decide("NotebookEdit", {"notebook_path": _src("src/web/zz.ipynb"), "new_source": KEY})


def test_clean_text_ignored_folders_other_projects_and_other_tools_pass(tmp_path):
    assert pg.decide("Write", {"file_path": _src("src/web/zz_probe.py"), "content": "x = 1\n"}) is None
    assert pg.decide("Write", {"file_path": _src("docs/plans/zz_probe.py"), "content": KEY}) is None
    assert pg.decide("Write", {"file_path": str(tmp_path / "x.py"), "content": KEY}) is None
    assert pg.decide("Bash", {"command": f"echo {KEY}"}) is None


def test_the_scanner_s_own_skip_list_applies():
    # Markdown is not content-scanned by Publish, so the guard lets it through as well.
    assert pg.decide("Write", {"file_path": _src("README.md"), "content": KEY}) is None


def test_a_relative_path_resolves_against_the_session_folder():
    assert pg.decide("Write", {"file_path": "src/web/zz_probe.py", "content": KEY}, str(ROOT))


def test_it_fails_open_and_has_a_kill_switch(monkeypatch):
    monkeypatch.setattr(pg, "findings", lambda *a: (_ for _ in ()).throw(OSError("boom")))
    assert pg.decide("Write", {"file_path": _src("src/web/zz.py"), "content": KEY}) is None
    monkeypatch.undo()
    monkeypatch.setenv("VIBENODE_PUBLISH_GUARD", "0")
    assert pg.decide("Write", {"file_path": _src("src/web/zz.py"), "content": KEY}) is None


def test_it_runs_first_inside_the_tool_guards(tmp_path):
    why = tg.decide("Write", {"file_path": _src("src/web/zz.py"), "content": KEY}, "",
                    config=tmp_path / "none.json")
    assert why and why.startswith("[publish scan]")


def test_the_guard_and_publish_use_one_rule_set():
    from web import git_scanner as g
    text = f"k = '{KEY}'\n"
    assert pg.findings("src/web/zz.py", text) == g.scan_text("src/web/zz.py", text)
    assert pg.findings("src/web/zz.py", text)


def test_the_full_scan_still_reports_what_scan_text_finds(tmp_path):
    from web import git_scanner as g
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    (tmp_path / "a.py").write_text(f"k = '{KEY}'\n")
    (tmp_path / "README.md").write_text(KEY)
    res = g.scan_staged_files(tmp_path)
    assert [(f["file"], f["line"], f["type"]) for f in res["findings"]] == [("a.py", 1, "AWS Access Key")]
    import json
    done = json.loads(list(g.scan_staged_files_stream(tmp_path))[-1])
    assert done["findings"] == res["findings"]


# ── Who the owner is never reaches a published file, notes included ──
# The real rules hold only fingerprints, and no file may carry a name, so these
# tests swap in rules built from made-up words with the same shape.
import pytest


@pytest.fixture
def made_up_people(monkeypatch):
    from web import git_scanner as g
    R = g._IdentityRule.of
    rules = [R("Owner ID", "id", ["40404"]), R("Owner Name", "n", ["zorbo"]),
             R("Owner Family Name", "f", ["rivers"], initial=True),
             R("Owner First Name", "first", ["Jordan"], case=True, as_name=True),
             R("Collaborator Name", "c", ["Quill"], case=True, as_name=True),
             R("Owner Machine Name", "m", ["zorbvm"])]
    monkeypatch.setattr(g, "_IDENTITY_RULES", rules)
    return g


PLANTED = ["user 40404 here", "by zorbo", "jrivers@example.org", "Asked by Jordan, 2026-11-03.",
           "Quill wrote this.", "rebuild launched on zorbvm"]


def test_every_identity_rule_fires_in_code_tests_and_notes(made_up_people):
    g = made_up_people
    for where in ("src/web/x.py", "tests/test_x.py", "CLAUDE.md", "src/features/workforce/agent.md", "docs/guide.md"):
        found = {f["type"] for text in PLANTED for f in g.scan_text(where, text)}
        assert found == {r.label for r in g._IDENTITY_RULES}, (where, found)
    assert g.scan_text("src/web/git_scanner.py", PLANTED[3]) == []


def test_ordinary_words_are_not_the_owner(made_up_people):
    g = made_up_people
    for text in ["Jordan't", "Jordan\\'t", "Jordan&#x27;t", "JORDAN", "Jordanian", "riverside", "precanter"]:
        assert not g.scan_text("src/web/x.py", text), text
    # A possessive is the name itself.
    assert [f["type"] for f in g.scan_text("src/web/x.py", "Waiting on Jordan's approval")] == ["Owner First Name"]


def test_the_full_scan_reads_skipped_files_for_identity_only(tmp_path, made_up_people):
    g = made_up_people
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    (tmp_path / "NOTES.md").write_text(f"{KEY}\nQuestion from Jordan\n")
    (tmp_path / "docs").mkdir()
    (tmp_path / "docs" / "shot.png").write_bytes(b"\x89PNG\x00\x00 Jordan ")
    res = g.scan_staged_files(tmp_path)
    assert [(f["file"], f["line"], f["type"]) for f in res["findings"]] == [("NOTES.md", 2, "Owner First Name")]
    import json
    assert json.loads(list(g.scan_staged_files_stream(tmp_path))[-1])["findings"] == res["findings"]


def test_a_session_writing_the_owner_into_notes_is_refused(made_up_people):
    # The guard runs the scanner in a fresh interpreter, so it uses the real rules;
    # here the finding comes from the test rules through the same code path.
    g = made_up_people
    assert [f["type"] for f in g.scan_text("CLAUDE.md", "The owner (Jordan) chose this.")] == ["Owner First Name"]


def test_the_real_rules_keep_only_fingerprints():
    from web import git_scanner as g
    src = (ROOT / "src" / "web" / "git_scanner.py").read_text()
    assert len(g._IDENTITY_RULES) == 7  # six identity rules and swearing
    for r in g._IDENTITY_RULES:
        assert r.fingerprints and all(len(f) == 20 for f in r.fingerprints)
        for f in r.fingerprints:
            assert f in src


def _git(cwd, *a):
    return subprocess.run(["git", "-C", str(cwd), *a], check=True, capture_output=True, text=True).stdout


def test_versions_waiting_to_publish_are_checked_for_names(tmp_path, made_up_people):
    g = made_up_people
    remote, work = tmp_path / "remote.git", tmp_path / "work"
    subprocess.run(["git", "init", "-q", "--bare", str(remote)], check=True)
    subprocess.run(["git", "clone", "-q", str(remote), str(work)], check=True)
    for k, v in {"user.email": "t@example.org", "user.name": "t"}.items():
        _git(work, "config", k, v)
    (work / "a.txt").write_text("hello\n"); _git(work, "add", "-A"); _git(work, "commit", "-qm", "one")
    _git(work, "push", "-q", "origin", "HEAD")
    assert g.scan_outgoing_commits(work) == []
    # An old version that still names the owner, removed again later: the tree is clean, history is not.
    (work / "a.txt").write_text("hello\nAsked by Jordan.\n"); _git(work, "commit", "-qam", "two")
    (work / "a.txt").write_text("hello\n"); _git(work, "commit", "-qam", "three")
    assert g.scan_staged_files(work)["findings"] == []
    found = g.scan_outgoing_commits(work)
    assert [f["type"] for f in found] == ["Owner First Name"]
    _git(work, "commit", "-q", "--allow-empty", "-m", "fix for Quill")
    assert {f["type"] for f in g.scan_outgoing_commits(work)} == {"Owner First Name", "Collaborator Name"}


def test_update_refuses_to_publish_versions_with_names():
    src = (ROOT / "src" / "web" / "git_ops.py").read_text()
    a, b = src.index("scan_outgoing_commits(proj)"), src.index('["git", "-C", str(proj), "push"]')
    assert a < b, "the outgoing-versions check must run before the push"


# ── Private notes and stray files never publish ──

def test_the_public_layout_hides_notes_pictures_and_strays():
    for path in ["zz.png", "notes.md", "_trash_x/a.png", "docs/zz.md", "docs/x.py", "src/web/x.png",
                 "src/web/NOTES.md", "tests/screenshots/a.png", "docs/plans/x.md", "scratch.txt", "src/web/plan.html"]:
        assert subprocess.run(["git", "-C", str(ROOT), "check-ignore", "-q", path]).returncode == 0, path
    for path in ["src/web/x.py", "src/web/static/x.png", "src/web/static/images/y.png", "docs/screenshots/z.png",
                 "src/features/workforce/new.md", "tests/test_x.py", "src/web/templates/x.html", "README.md", "CLAUDE.md"]:
        assert subprocess.run(["git", "-C", str(ROOT), "check-ignore", "-q", path]).returncode == 1, path


def test_no_tracked_file_is_outside_the_public_layout():
    from web import git_scanner as g
    assert g.layout_problems(ROOT) == []


def test_a_force_added_note_is_refused_by_the_scan(tmp_path):
    from web import git_scanner as g
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    (tmp_path / ".gitignore").write_text("*.md\n")
    (tmp_path / "PLAN.md").write_text("a private plan\n")
    _git(tmp_path, "add", "-f", "PLAN.md")
    res = g.scan_staged_files(tmp_path)
    assert not res["ok"] and [b["file"] for b in res["blocked_files"]] == ["PLAN.md"]


def test_the_public_project_file_stays_short(tmp_path):
    from web import git_scanner as g
    assert g.CLAUDE_MD_MAX_LINES == pg.CLAUDE_MD_MAX_LINES
    assert (ROOT / "CLAUDE.md").read_text().count("\n") <= g.CLAUDE_MD_MAX_LINES
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    (tmp_path / "CLAUDE.md").write_text("rule\n" * (g.CLAUDE_MD_MAX_LINES + 1))
    assert any(b["file"] == "CLAUDE.md" for b in g.scan_staged_files(tmp_path)["blocked_files"])
    why = pg.decide("Edit", {"file_path": _src("CLAUDE.md"), "old_string": "# VibeNode",
                             "new_string": "## A new note\n" * 80 + "# VibeNode"})
    assert why and "docs/plans/PROJECT-NOTES.md" in why
    assert pg.decide("Write", {"file_path": _src("CLAUDE.md"), "content": "short\n"}) is None


def test_swearing_is_refused_everywhere():
    from web import git_scanner as g
    word = "f" + "ucking"
    for where in ("src/web/x.py", "CLAUDE.md", "src/features/workforce/a.md"):
        assert [f["type"] for f in g.scan_text(where, f"this is {word} slow")] == ["Swearing"], where
    assert g.scan_text("src/web/x.py", "handle_crashitem shift shitake") == []


def test_a_copy_gets_its_private_notes_back_after_the_update(tmp_path):
    from web import post_update
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    for k, v in {"user.email": "t@example.org", "user.name": "t"}.items():
        _git(tmp_path, "config", k, v)
    long_notes = "".join(f"note {i}\n" for i in range(200))
    (tmp_path / "CLAUDE.md").write_text(long_notes); _git(tmp_path, "add", "-A"); _git(tmp_path, "commit", "-qm", "a")
    (tmp_path / "CLAUDE.md").write_text("short public rules\n"); _git(tmp_path, "commit", "-qam", "b")
    assert post_update.restore_private_notes(tmp_path) == "restored"
    assert (tmp_path / "docs" / "plans" / "PROJECT-NOTES.md").read_text() == long_notes
    assert post_update.restore_private_notes(tmp_path) == "present"


# ── Notes about people and build history never publish ──
# Built at run time so this file holds none of what the rules look for.
_O = "own" + "er"


def test_quotes_decisions_and_dated_notes_are_refused_everywhere():
    from web import git_scanner as g
    cases = {
        "Owner Quote": f"# The {_O}, 20" + "26-10-28: \"follow the work\"",
        "Owner Decision Note": f"# {_O.capitalize()}'s pick B of the mockup page",
        "Personal Quote": "# he " + "said: \"make it bigger\"",
        "Dated Build Note": "# Fix" + "ed 2026-10-05 after an incident",
    }
    for where in ("src/web/x.py", "tests/test_x.py", "docs/guide.md", "src/features/workforce/a.md"):
        for label, text in cases.items():
            assert label in {f["type"] for f in g.scan_text(where, text)}, (where, label)


def test_ordinary_wording_is_not_a_note():
    from web import git_scanner as g
    for text in [f"the {_O}'s phone may do everything the computer can", "request_is_" + _O,
                 "the user asked for a file", "date: 2026-10-05", "in the user's own words",
                 "Answering a card follows the thread until the reader scrolls away."]:
        assert not g.scan_text("src/web/x.py", text), text


def test_a_name_after_an_escaped_line_break_is_caught(made_up_people):
    g = made_up_people
    assert [f["type"] for f in g.scan_text("tests/test_x.py", 'body = "Thanks,\\n\\nJordan"')] == ["Owner First Name"]
