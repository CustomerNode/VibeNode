"""
Step 2 tests for git-update safety.

Two focused areas:

1. ``do_git_sync("pull")`` MUST preserve uncommitted local changes to tracked
   files by stashing them before ``git pull --rebase`` runs and
   restoring them afterwards. The concern raised by both prior reports:
   an operator with local edits to launch.bat (or any machine-specific
   tracked file) could see them silently disappear on the daily pull. The
   stash/pop pattern prevents this — proven here by observing the command
   sequence.

2. Machine-specific runtime settings MUST live in a gitignored file so a
   ``git pull`` can never overwrite them regardless of merge strategy.
   VibeNode's existing ``kanban_config.json`` is the natural home; this test
   locks in the contract by asserting the file is git-ignored today.

Neither test invokes a real ``git pull``. The first uses the same mocked
``subprocess.run`` pattern as the existing ``tests/test_git_ops.py``, so a
CI run cannot mutate any real repository. The second consults
``.gitignore`` directly with ``git check-ignore``, which is read-only.
"""

import subprocess
from pathlib import Path
from unittest.mock import MagicMock

import pytest


# ---------------------------------------------------------------------------
# 1. Stash / pop preserves uncommitted local changes across a pull
# ---------------------------------------------------------------------------

def test_pull_stashes_before_pulling_and_pops_after(tmp_path, monkeypatch):
    """The exact command sequence must be:
        1. git stash --include-untracked  (save local changes)
        2. git pull --rebase              (upstream lands cleanly)
        3. git stash pop                  (local changes reapplied)

    In that order. If step 2 lands upstream changes to launch.bat, step 3's
    ``stash pop`` either applies cleanly (upstream didn't touch that file)
    or leaves conflict markers that the user can see (upstream did touch
    it). No path exists where a locally-modified tracked file is silently
    overwritten by the pull itself, because the tree is clean when the
    pull runs.
    """
    from web import git_ops

    (tmp_path / ".git").mkdir()
    monkeypatch.setattr(git_ops, "_VIBENODE_DIR", tmp_path)
    monkeypatch.setattr(git_ops, "_current_branch", lambda *a: "main")
    monkeypatch.setattr(git_ops, "_default_branch", lambda *a: "main")
    # The requirements blob check is its own git call; keep it
    # out of the scripted sequence so this test stays about stash/pull/pop.
    monkeypatch.setattr(git_ops, "_requirements_blob", lambda *a: "")
    # The stash-pop clash check is its own git call; no clash in these tests
    # (a real clash is covered in tests/test_git_scanner_gaps.py).
    monkeypatch.setattr(git_ops, "_unmerged_paths", lambda *a: [])
    # The republished-history check is its own set of git calls (covered by its
    # own tests below); this test is about the ordinary stash/pull/pop path.
    monkeypatch.setattr(git_ops, "_remote_history_is_unrelated", lambda *a: False)

    # Model a WORKING TREE WITH LOCAL EDITS: stash reports it did save
    # something. pull runs cleanly. stash pop restores.
    stash_save = MagicMock(stdout="Saved working directory and index state WIP on main",
                           returncode=0, stderr="")
    pull_ok = MagicMock(stdout="Updating abc..def\nFast-forward\n", returncode=0, stderr="")
    stash_pop = MagicMock(stdout="On branch main", returncode=0, stderr="")
    revlist = MagicMock(stdout="0\t0", returncode=0)
    status = MagicMock(stdout="", returncode=0)

    calls = []

    def _record(cmd, *args, **kw):
        calls.append(list(cmd))
        return {
            0: stash_save, 1: pull_ok, 2: stash_pop,
            3: revlist, 4: status,
        }.get(len(calls) - 1, MagicMock(stdout="", returncode=0))

    monkeypatch.setattr(subprocess, "run", _record)

    result = git_ops.do_git_sync("pull")
    assert result["ok"] is True

    # Reconstruct the observed git command sequence and assert the order.
    git_calls = [c for c in calls if len(c) >= 4 and c[0] == "git"]
    # The first three git commands should be stash → pull → stash pop, in
    # that exact order, so any local uncommitted change is safe.
    kinds = []
    for c in git_calls[:3]:
        if "stash" in c and "pop" in c:
            kinds.append("stash_pop")
        elif "stash" in c:
            kinds.append("stash_save")
        elif "pull" in c:
            kinds.append("pull")
    assert kinds == ["stash_save", "pull", "stash_pop"], (
        f"expected stash → pull → stash pop, got {kinds}: {git_calls[:3]}"
    )


def test_pull_still_pops_stash_after_upstream_landed(tmp_path, monkeypatch):
    """Even when the upstream advances (not 'Already up to date'), stash pop
    still runs so local uncommitted work is not silently orphaned in the
    stash stack.
    """
    from web import git_ops

    (tmp_path / ".git").mkdir()
    monkeypatch.setattr(git_ops, "_VIBENODE_DIR", tmp_path)
    monkeypatch.setattr(git_ops, "_current_branch", lambda *a: "main")
    monkeypatch.setattr(git_ops, "_default_branch", lambda *a: "main")
    monkeypatch.setattr(git_ops, "_requirements_blob", lambda *a: "")
    # The stash-pop clash check is its own git call; no clash in these tests
    # (a real clash is covered in tests/test_git_scanner_gaps.py).
    monkeypatch.setattr(git_ops, "_unmerged_paths", lambda *a: [])

    calls = []
    scripted = [
        MagicMock(stdout="Saved working directory", returncode=0, stderr=""),   # stash
        MagicMock(stdout="Updating abc..def", returncode=0, stderr=""),          # pull
        MagicMock(stdout="On branch main", returncode=0, stderr=""),             # stash pop
        MagicMock(stdout="0\t1", returncode=0),                                   # revlist
        MagicMock(stdout="", returncode=0),                                       # status
    ]

    def _dispatch(cmd, *a, **kw):
        calls.append(list(cmd))
        return scripted[len(calls) - 1] if len(calls) <= len(scripted) else MagicMock(stdout="", returncode=0)

    monkeypatch.setattr(subprocess, "run", _dispatch)

    result = git_ops.do_git_sync("pull")
    assert result["ok"] is True
    assert any(
        "stash" in c and "pop" in c for c in calls if isinstance(c, list)
    ), "stash pop must run even when upstream advanced"


def test_pull_never_picks_a_side_on_a_clash(tmp_path, monkeypatch):
    """Every pull Update runs is a plain one: no ``-X theirs`` / ``-X ours``.

    ``-X theirs`` in a rebase keeps the LOCAL line of every clashing hunk,
    which silently drops lines another commit added, so Update stops on a
    clash instead; ``tests/test_git_scanner_gaps.py`` proves the stop against
    real repositories.
    """
    from web import git_ops

    (tmp_path / ".git").mkdir()
    monkeypatch.setattr(git_ops, "_VIBENODE_DIR", tmp_path)
    monkeypatch.setattr(git_ops, "_current_branch", lambda *a: "main")
    monkeypatch.setattr(git_ops, "_default_branch", lambda *a: "main")

    seen_pull = []

    def _dispatch(cmd, *a, **kw):
        if len(cmd) >= 4 and cmd[3] == "pull":
            seen_pull.append(list(cmd))
            # Fail the rebase so the fallback pull runs too.
            return MagicMock(stdout="", returncode=1 if "--rebase" in cmd else 0, stderr="")
        return MagicMock(stdout="", returncode=0, stderr="")

    monkeypatch.setattr(subprocess, "run", _dispatch)
    git_ops.do_git_sync("pull")
    assert len(seen_pull) == 2, seen_pull
    assert "--rebase" in seen_pull[0]
    assert "--no-rebase" in seen_pull[1]
    for cmd in seen_pull:
        assert "-X" not in cmd and "theirs" not in cmd and "ours" not in cmd, cmd


# ---------------------------------------------------------------------------
# 2. Machine-specific settings live in a gitignored file, not a tracked one
# ---------------------------------------------------------------------------

_REPO_ROOT = Path(__file__).resolve().parent.parent


def _is_ignored(path: str) -> bool:
    """Return True if ``path`` (relative to repo root) is ignored by git."""
    r = subprocess.run(
        ["git", "-C", str(_REPO_ROOT), "check-ignore", "-q", path],
        capture_output=True,
    )
    # check-ignore exits 0 when the path IS ignored, 1 when not, 128 on error.
    return r.returncode == 0


def _is_tracked(path: str) -> bool:
    r = subprocess.run(
        ["git", "-C", str(_REPO_ROOT), "ls-files", "--error-unmatch", path],
        capture_output=True,
    )
    return r.returncode == 0


def test_kanban_config_is_gitignored():
    """``kanban_config.json`` is VibeNode's machine-specific runtime settings
    file. It MUST remain gitignored so a ``git pull`` never sees it as a
    tracked file to overwrite. Any future machine-specific setting (e.g.
    'the ARM64 venv interpreter path') should live here, or in a similarly-
    ignored sibling. Losing this contract would defeat the entire
    Step 2 update-safety design.
    """
    assert _is_ignored("kanban_config.json"), (
        "kanban_config.json must stay gitignored — it is the load-bearing "
        "machine-local settings file. If this test fails, either .gitignore "
        "was regressed or a new tracked config sibling was introduced."
    )


def test_kanban_config_is_not_tracked():
    """Belt-and-suspenders for the above: even if .gitignore says it's
    ignored, a stray ``git add -f`` in the past could have tracked it.
    Verify no tracked ``kanban_config.json`` blob exists.
    """
    assert not _is_tracked("kanban_config.json"), (
        "kanban_config.json is tracked in the repo — this leaks per-machine "
        "settings into every clone AND makes git pull a live wire against "
        "them. Remove it from tracking with `git rm --cached kanban_config.json`."
    )


def test_launch_bat_is_tracked_but_has_no_machine_specific_paths():
    """``launch.bat`` IS tracked (it's the shared launcher). The contract we
    lock in here: launch.bat MUST NOT hard-code any machine-specific
    absolute path — the interpreter is resolved via PATH, and any
    per-machine deviation (e.g. Step 3's future ARM64 venv) belongs in
    the Windows shortcut target or a gitignored sidecar, not in
    launch.bat's tracked body.

    This test is intentionally loose: it fails only when a truly
    unambiguous per-user path lands in the file, such as
    ``C:\\Users\\<name>\\`` or the exact production Python path. Fix by
    reverting the local edit; the machine-specific path belongs
    elsewhere.
    """
    launch = _REPO_ROOT / "src" / "launcher" / "launch.bat"
    if not launch.exists():
        pytest.skip("launch.bat missing in this checkout")
    body = launch.read_text(encoding="utf-8", errors="replace").lower()
    for needle in (r"c:\\users\\", r"c:/users/", r"c:\python", r"c:/python"):
        assert needle not in body, (
            f"launch.bat contains a machine-specific path {needle!r}. "
            f"That path will be overwritten by any `git pull` from origin. "
            f"Move machine-specific interpreter selection to the Windows "
            f"shortcut target (untracked) or a gitignored sidecar."
        )


# ── Adopting a republished (history-rewritten) VibeNode ─────────────────────
#
# When VibeNode's published history is rewritten (a scrub, a squash), an older
# clone shares NO commit with the new remote.  A plain ``git pull --rebase``
# then tries to replay the clone's whole history onto the new root and drowns
# in conflicts; ``--no-rebase`` refuses "unrelated histories".  Either way the
# one-click Update would die and never recover, stranding the install on old
# code for good.  ``do_git_sync`` detects the no-common-ancestor case and adopts
# the new history: it saves the old tip on a local backup branch, keeps local
# edits in the stash, and hard-resets onto ``origin/<default>``.  These tests
# drive REAL git repositories (no network) so the behaviour, not a mock, is
# what is checked.

import os
import subprocess as _sp

from web import git_ops as _go


def _git(d, *a):
    return _sp.run(["git", "-C", str(d), *a], capture_output=True, text=True, check=True)


def _commit(d, name, body="x\n"):
    (d / name).write_text(body)
    _git(d, "add", "-A")
    _git(d, "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-q", "-m", name)


def _make_remote_and_old_clone(tmp_path):
    """A 'republished' remote (orphan root, new layout) and an old clone that
    shares no commit with it."""
    origin = tmp_path / "origin.git"
    work = tmp_path / "build"
    work.mkdir()
    _git(work, "init", "-q", "-b", "main")
    # The "old" line of history.
    _commit(work, "README.md", "old readme\n")
    _commit(work, "session_manager.py", "old top-level start\n")
    old_tip = _git(work, "rev-parse", "HEAD").stdout.strip()
    # Publish the old line to a bare origin, then clone it as the "old install".
    _sp.run(["git", "init", "--bare", "-q", "-b", "main", str(origin)], check=True)
    _git(work, "remote", "add", "origin", str(origin))
    _git(work, "push", "-q", "origin", "main")
    clone = tmp_path / "install"
    _sp.run(["git", "clone", "-q", str(origin), str(clone)], check=True)
    # Now REWRITE the published history: an orphan root with the new layout.
    _git(work, "checkout", "-q", "--orphan", "fresh")
    for p in list(work.iterdir()):
        if p.name != ".git":
            if p.is_dir():
                _sp.run(["rm", "-rf", str(p)], check=True)
            else:
                p.unlink()
    (work / "README.md").write_text("new readme\n")
    (work / "session_manager.py").write_text("new top-level start\n")
    _git(work, "add", "-A")
    _git(work, "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-q", "-m", "VibeNode")
    _git(work, "branch", "-M", "fresh", "main")
    _git(work, "push", "-q", "-f", "origin", "main")
    return origin, clone, old_tip


def test_update_adopts_a_republished_history(tmp_path, monkeypatch):
    origin, clone, old_tip = _make_remote_and_old_clone(tmp_path)
    monkeypatch.setattr(_go, "_VIBENODE_DIR", clone)
    monkeypatch.setattr(_go, "_run_post_update", lambda proj: True)
    monkeypatch.setattr(_go, "_default_branch", lambda proj: "main")
    monkeypatch.setattr(_go, "_install_pulled_requirements",
                        lambda proj: {"pip_ok": True, "pip_installed": 0})

    assert _go._remote_history_is_unrelated(clone, "main") is True
    res = _go.do_git_sync("pull", confirm_branch=True)
    assert res["ok"], res
    # The clone is now ON the republished history, with the new file contents.
    assert (clone / "README.md").read_text() == "new readme\n"
    assert (clone / "session_manager.py").read_text() == "new top-level start\n"
    head = _git(clone, "rev-parse", "HEAD").stdout.strip()
    assert head == _git(clone, "rev-parse", "origin/main").stdout.strip()
    # The old tip is saved on a local backup branch, and never pushed.
    branches = _git(clone, "branch").stdout
    assert "before-republish-" in branches
    assert old_tip in _git(clone, "rev-parse", "before-republish-"
                           + branches.split("before-republish-")[1].split()[0]).stdout


def test_a_normal_update_is_not_treated_as_a_rewrite(tmp_path, monkeypatch):
    """A clone that shares history with the remote must take the ordinary pull
    path, NOT the hard-reset adopt path."""
    origin = tmp_path / "o.git"
    _sp.run(["git", "init", "--bare", "-q", "-b", "main", str(origin)], check=True)
    work = tmp_path / "w"; work.mkdir()
    _git(work, "init", "-q", "-b", "main")
    _commit(work, "README.md", "one\n")
    _git(work, "remote", "add", "origin", str(origin))
    _git(work, "push", "-q", "origin", "main")
    clone = tmp_path / "c"
    _sp.run(["git", "clone", "-q", str(origin), str(clone)], check=True)
    # Remote advances by one ordinary commit on the SAME line.
    _commit(work, "NEW.txt", "two\n")
    _git(work, "push", "-q", "origin", "main")
    assert _go._remote_history_is_unrelated(clone, "main") is False
