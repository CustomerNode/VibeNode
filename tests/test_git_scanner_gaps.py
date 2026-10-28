"""GUARDS for three holes found in the pre-push secret scan on 2026-10-28.

1. **Per-file "Env Variable Secret" suppressions blinded whole files.**
   Constants named ``*_KEY(S)`` whose value is code (``frozenset({...})``,
   ``re.compile(...)``, a tuple) tripped the rule, and the fix each time was to
   suppress the rule for the file. A real generic secret in that file
   (``SERVICE_TOKEN = "..."``) then went unreported. The rule now ignores values
   that are code, and the suppressions are gone.
2. **The owner's username/email slipped past the surname rule.** The rule
   needed a word break before the surname; an initial in front of it (a
   username, an email address) has none.
3. **Update published conflict markers.** After a pull, Update puts local work
   back with ``git stash pop`` and never checked for a clash; ``git add -A``
   would then commit the ``<<<<<<<`` lines.

Every secret-shaped or name-shaped string below is assembled at run time so this
file does not trip the scanner it tests.

Run: python -m pytest tests/test_git_scanner_gaps.py -q
"""
from __future__ import annotations

import subprocess

from app.git_scanner import _FINDING_SUPPRESSIONS, scan_staged_files

ENV = "Env Variable Secret"
STEM = "c" + "a" + "nto"
OWNER = "Owner Name (" + STEM + ")"
SURNAME = STEM + "w"
VAR_NAME = "SERV" + "ICE_TOK" + "EN"
FAKE_VALUE = "Zq81" + "LmPx4Rt9"


def _repo(tmp_path, files: dict):
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    for rel, body in files.items():
        p = tmp_path / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(body, encoding="utf-8")
    return tmp_path


def _hits(result, label, rel=None):
    return [f for f in result["findings"]
            if f["type"] == label and (rel is None or f["file"] == rel)]


# ── 1. code values are not secrets; no file is blinded ──

def test_code_valued_key_constants_are_not_flagged(tmp_path):
    body = (
        "ZOOM" + "_KEYS = ((\"big\", \"Big picture\"),)\n"
        "KNOWN" + "_KEYS = frozenset({\"id\", \"cmd\"})\n"
        "_JS_OPTION" + "_KEY = re.compile(r\"key\")\n"
        "API" + "_KEY = os.environ.get(\"X\")\n"
        "_SKIP" + "_KEYS = {\"url\", \"href\"}\n"
        "ALL" + "_TOKENS = [\"a\", \"b\"]\n"
    )
    r = scan_staged_files(_repo(tmp_path, {"mod.py": body}))
    assert _hits(r, ENV) == [], r["findings"]


def test_a_generic_secret_is_flagged_in_every_formerly_exempt_file(tmp_path):
    files = {rel: "ZOOM" + "_KEYS = ((\"big\", \"x\"),)\n"
                  + VAR_NAME + " = \"" + FAKE_VALUE + "\"\n"
             for rel in ("daemon/handoff_rules.py", "daemon/smart_termination.py",
                         "pipeline/plan.py", "app/session_store.py")}
    r = scan_staged_files(_repo(tmp_path, files))
    for rel in files:
        hits = _hits(r, ENV, rel)
        assert [h["line"] for h in hits] == [2], (rel, r["findings"])


def test_no_file_suppresses_the_env_secret_rule():
    assert [p.pattern for p, t in _FINDING_SUPPRESSIONS if t == ENV] == []


# ── 2. owner name with a leading initial ──

def test_owner_username_and_email_are_flagged(tmp_path):
    body = ("who = \"d" + SURNAME + "@example.org\"\n"
            "home = \"m" + SURNAME + "\"\n"
            "plain = \"" + SURNAME.capitalize() + "\"\n")
    r = scan_staged_files(_repo(tmp_path, {"who.py": body}))
    assert sorted(h["line"] for h in _hits(r, OWNER)) == [1, 2, 3], r["findings"]


def test_longer_words_containing_the_surname_are_not_flagged(tmp_path):
    body = "x = \"pre" + STEM + "\"\ny = \"" + STEM + "nese\"\n"
    r = scan_staged_files(_repo(tmp_path, {"words.py": body}))
    assert _hits(r, OWNER) == [], r["findings"]


# ── 3. Update stops on a stash-pop clash ──

def _git(cwd, *args):
    return subprocess.run(["git", "-C", str(cwd), *args], check=True,
                          capture_output=True, text=True)


def _clone_pair(tmp_path):
    remote = tmp_path / "remote.git"
    subprocess.run(["git", "init", "-q", "--bare", "-b", "main", str(remote)], check=True)
    a, b = tmp_path / "a", tmp_path / "b"
    for d in (a, b):
        subprocess.run(["git", "clone", "-q", str(remote), str(d)], check=True,
                       capture_output=True)
        _git(d, "config", "user.email", "t@example.org")
        _git(d, "config", "user.name", "t")
        _git(d, "checkout", "-q", "-B", "main")
    (a / "f.txt").write_text("base\n")
    _git(a, "add", "-A"); _git(a, "commit", "-qm", "base"); _git(a, "push", "-q", "-u", "origin", "main")
    _git(b, "pull", "-q", "origin", "main"); _git(b, "branch", "-q", "-u", "origin/main")
    (a / "f.txt").write_text("theirs\n")
    _git(a, "commit", "-qam", "theirs"); _git(a, "push", "-q")
    (b / "f.txt").write_text("mine\n")          # uncommitted local edit on the same line
    return remote, b


def test_update_stops_when_local_changes_clash_with_the_pull(tmp_path, monkeypatch):
    from app import git_ops
    remote, b = _clone_pair(tmp_path)
    head_before = _git(remote, "rev-parse", "main").stdout
    monkeypatch.setattr(git_ops, "_VIBENODE_DIR", b)
    monkeypatch.setattr(git_ops, "_requirements_blob", lambda *a: "same")
    monkeypatch.setattr("app.git_scanner.scan_staged_files",
                        lambda *a, **k: {"ok": True, "findings": [], "blocked_files": [], "summary": ""})

    result = git_ops.do_git_sync("both")

    assert result["ok"] is False, result
    assert result.get("conflicts") == ["f.txt"], result
    assert any("clashed" in m for m in result["messages"]), result["messages"]
    assert _git(remote, "rev-parse", "main").stdout == head_before, "the clash was published"
    assert _git(b, "stash", "list").stdout.strip(), "local changes were not kept"


def test_update_refuses_to_publish_a_leftover_clash(tmp_path, monkeypatch):
    from app import git_ops
    remote, b = _clone_pair(tmp_path)
    _git(b, "stash", "-q"); _git(b, "pull", "-q")
    subprocess.run(["git", "-C", str(b), "stash", "pop"], capture_output=True)  # clashes
    head_before = _git(remote, "rev-parse", "main").stdout
    monkeypatch.setattr(git_ops, "_VIBENODE_DIR", b)
    monkeypatch.setattr("app.git_scanner.scan_staged_files",
                        lambda *a, **k: {"ok": True, "findings": [], "blocked_files": [], "summary": ""})

    result = git_ops.do_git_sync("push")

    assert result["ok"] is False, result
    assert result.get("conflicts") == ["f.txt"], result
    assert _git(remote, "rev-parse", "main").stdout == head_before
