"""The fingerprint asks git once where it used to ask several times.  It must
give the same answer in every state a working tree can be in, because evidence
recorded by an earlier pipeline version is compared against it.

``_reference`` is the earlier implementation, kept here word for word apart from
the helpers it inlines.
"""

import hashlib
import os
import subprocess
import sys

import pytest

from features.pipeline.depth import required_depth
from features.pipeline.evidence import (_excluded, _file_sha, _is_git_repo, _worktree_mode, git, ignored_steering_files,
                               site_fingerprint, tree_fingerprint, untracked_files, walk_files)

XDIST_SPLITTABLE = True


def _untracked_reference(repo):
    out = git(repo, "ls-files", "--others", "--exclude-per-directory=.gitignore", "-z", check=False)
    listed = {p for p in out.split("\0") if p}
    hidden = [p for p in git(repo, "ls-files", "--others", "-z", "--", ":(glob)**/.gitignore", check=False).split("\0")
              if p and p not in listed and not _excluded(p)]
    if hidden:
        tracked = {p for p in git(repo, "ls-files", "-z", check=False).split("\0") if p}
        probes = {p: (p.rsplit("/", 1)[0] if "/" in p else p) for p in hidden}
        r = subprocess.run(["git", "check-ignore", "-v", "-z", "--no-index", "--stdin"], cwd=str(repo),
                           capture_output=True, text=True, encoding="utf-8", errors="replace",
                           input="\0".join(sorted(set(probes.values()))) + "\0")
        fields = r.stdout.split("\0")
        legit = set()
        for i in range(0, len(fields) - 3, 4):
            source, _line, _pattern, path = fields[i:i + 4]
            if source.replace("\\", "/") in tracked:
                legit.add(path)
        for p, probe in probes.items():
            if probe not in legit:
                listed.add(p)
    return sorted(p for p in listed if not _excluded(p))


def _reference(repo):
    h = hashlib.sha256()
    if not _is_git_repo(repo):
        for rel in walk_files(repo):
            h.update(rel.encode())
            h.update(_file_sha(repo / rel).encode())
        return h.hexdigest()
    entries, index_mode, deleted, modified, flagged = {}, {}, set(), set(), set()
    for ent in git(repo, "ls-files", "-c", "-d", "-m", "-s", "-v", "-z").split("\0"):
        if not ent or "\t" not in ent or len(ent) < 2 or ent[1] != " ":
            continue
        meta, path = ent.split("\t", 1)
        tag, parts = meta[0], meta[2:].split()
        if tag in "Rr":
            deleted.add(path)
        elif tag in "Cc":
            modified.add(path)
        else:
            if tag.islower() or tag == "S":
                flagged.add(path)
            if len(parts) >= 3 and parts[2] == "0" and not _excluded(path):
                entries[path] = f"{parts[0]} {parts[1]}"
                index_mode[path] = parts[0]
    for p in deleted:
        entries.pop(p, None)
    untracked = set(_untracked_reference(repo))
    for p in flagged:
        if not (repo / p).is_file():
            entries.pop(p, None)
    steering = set(ignored_steering_files(repo))
    candidates = (modified | untracked | flagged | steering) - deleted
    links = sorted(p for p in candidates if not _excluded(p) and os.path.islink(repo / p))
    for p in links:
        entries[p] = "120000 link:" + hashlib.sha256(os.readlink(repo / p).encode("utf-8", "replace")).hexdigest()
    to_hash = sorted(p for p in candidates if not _excluded(p) and p not in links and (repo / p).is_file())
    if to_hash:
        out = subprocess.run(["git", "hash-object", "--stdin-paths"], cwd=str(repo), capture_output=True,
                             text=True, encoding="utf-8", errors="replace", input="\n".join(to_hash) + "\n")
        shas = out.stdout.split()
        if not (out.returncode == 0 and len(shas) == len(to_hash)):
            shas = [_file_sha(repo / p) for p in to_hash]
        for p, sha in zip(to_hash, shas):
            entries[p] = f"{_worktree_mode(repo / p, index_mode.get(p))} {sha}"
    for p in steering:
        if p in entries:
            entries[p] = "ignored " + entries[p]
    for path in sorted(entries):
        h.update(path.encode("utf-8", "replace"))
        h.update(entries[path].encode())
    return h.hexdigest()


def _g(repo, *args):
    subprocess.run(["git", *args], cwd=str(repo), check=True, capture_output=True)


@pytest.fixture
def base(tmp_path):
    r = tmp_path / "r"
    r.mkdir()
    _g(r, "init", "-q")
    _g(r, "config", "user.email", "t@test.local")
    _g(r, "config", "user.name", "t")
    _g(r, "config", "commit.gpgsign", "false")
    (r / "a.py").write_text("A = 1\n", encoding="utf-8")
    (r / "b.sh").write_text("#!/bin/sh\n", encoding="utf-8")
    (r / "sub").mkdir()
    (r / "sub" / "c.py").write_text("C = 1\n", encoding="utf-8")
    (r / ".gitignore").write_text("ignored/\n*.log\nconftest.py\n", encoding="utf-8")
    _g(r, "add", "-A")
    _g(r, "commit", "-q", "-m", "base")
    return r


def _write(repo, rel, text="x\n"):
    p = repo / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8")


def _clean(r):
    pass


def _modified(r):
    _write(r, "a.py", "A = 2\n")


def _deleted(r):
    (r / "a.py").unlink()


def _untracked(r):
    _write(r, "new.py")
    _write(r, "deep/er/new.py")


def _ignored_plain(r):
    _write(r, "ignored/x.py")
    _write(r, "noise.log")


def _ignored_steering(r):
    _write(r, "conftest.py", "import os\n")
    _write(r, "sub/conftest.py")
    _write(r, "ignored/pytest.ini", "[pytest]\n")


def _untracked_steering_not_ignored(r):
    _write(r, "tox.ini", "[tox]\n")


def _steering_in_dependency_folder(r):
    _write(r, ".gitignore", "ignored/\n*.log\nconftest.py\nnode_modules/\n")
    _write(r, "node_modules/p/conftest.py")


def _self_hiding_gitignore(r):
    _write(r, "sub2/.gitignore", ".gitignore\nconftest.py\n")
    _write(r, "sub2/conftest.py")


def _virtualenv_gitignore(r):
    _write(r, ".gitignore", "ignored/\n*.log\nconftest.py\nvenv/\n")
    _write(r, "venv/.gitignore", "*\n")
    _write(r, "venv/lib.py")


def _plain_untracked_gitignore(r):
    _write(r, "sub3/.gitignore", "*.tmp\n")
    _write(r, "sub3/x.tmp")


def _symlink(r):
    os.symlink("a.py", r / "lnk")


def _mode_change(r):
    os.chmod(r / "b.sh", 0o755)


def _assume_unchanged(r):
    _g(r, "update-index", "--assume-unchanged", "a.py")
    _write(r, "a.py", "A = 3\n")


def _assume_unchanged_deleted(r):
    _g(r, "update-index", "--assume-unchanged", "a.py")
    (r / "a.py").unlink()


def _skip_worktree(r):
    _g(r, "update-index", "--skip-worktree", "sub/c.py")
    _write(r, "sub/c.py", "C = 2\n")


def _staged_not_committed(r):
    _write(r, "staged.py")
    _g(r, "add", "staged.py")


def _odd_names(r):
    _write(r, "with space.py")
    _write(r, "with\ttab.py")
    _write(r, "with\nnewline.py")
    _write(r, "? question.py")
    _write(r, "H fake.py")
    _write(r, "ünï.py")


def _merge_conflict(r):
    _g(r, "checkout", "-q", "-b", "other")
    _write(r, "a.py", "A = 10\n")
    _g(r, "commit", "-qam", "other")
    _g(r, "checkout", "-q", "-")
    _write(r, "a.py", "A = 20\n")
    _g(r, "commit", "-qam", "main")
    subprocess.run(["git", "merge", "other"], cwd=str(r), capture_output=True)


def _run_dir_only(r):
    _write(r, "docs/plans/runs/x/evidence.json", "{}\n")


def _everything(r):
    for fn in (_modified, _untracked, _ignored_plain, _ignored_steering, _self_hiding_gitignore, _symlink,
               _mode_change, _skip_worktree, _odd_names):
        fn(r)


STATES = [_clean, _modified, _deleted, _untracked, _ignored_plain, _ignored_steering,
          _untracked_steering_not_ignored, _steering_in_dependency_folder, _self_hiding_gitignore,
          _virtualenv_gitignore, _plain_untracked_gitignore, _symlink, _mode_change, _assume_unchanged,
          _assume_unchanged_deleted, _skip_worktree, _staged_not_committed, _odd_names, _merge_conflict,
          _run_dir_only, _everything]
if os.name == "nt":     # no executable bit, no symlinks without privileges, no tab or newline in a name
    STATES = [s for s in STATES if s not in (_symlink, _mode_change, _odd_names, _everything)]


@pytest.mark.parametrize("state", STATES, ids=lambda f: f.__name__.strip("_"))
def test_same_fingerprint_and_same_untracked_list_as_before(base, state):
    state(base)
    assert tree_fingerprint(base) == _reference(base)
    assert untracked_files(base) == _untracked_reference(base)


@pytest.mark.parametrize("state", [s for s in STATES if s not in (_clean, _run_dir_only, _ignored_plain)],
                         ids=lambda f: f.__name__.strip("_"))
def test_each_state_really_changes_the_fingerprint(base, state):
    # Guards the comparison above: states that all hash alike would prove nothing.
    before = tree_fingerprint(base)
    state(base)
    assert tree_fingerprint(base) != before


def test_outside_git_the_files_are_hashed(tmp_path):
    (tmp_path / "x.py").write_text("X = 1\n", encoding="utf-8")
    assert tree_fingerprint(tmp_path) == _reference(tmp_path)


def test_a_failing_listing_inside_a_repository_still_raises(base, monkeypatch):
    # Corrupt the index: a listing fails while this is still a work tree.
    (base / ".git" / "index").write_bytes(b"not an index")
    if _is_git_repo(base):
        with pytest.raises(RuntimeError):
            tree_fingerprint(base)
    else:
        assert tree_fingerprint(base) == _reference(base)


def _site_reference(dirs):
    h = hashlib.sha256()
    h.update(os.path.normcase(os.path.abspath(sys.executable)).encode("utf-8", "replace"))
    h.update(b"\0PYTHONPATH=" + os.environ.get("PYTHONPATH", "").encode("utf-8", "replace"))
    h.update(b"\0PYTHONHOME=" + os.environ.get("PYTHONHOME", "").encode("utf-8", "replace"))
    for d in dirs:
        if not d.is_dir():
            continue
        names = sorted(p.name for p in d.iterdir()
                       if p.is_file() and (p.suffix == ".pth" or p.name in ("sitecustomize.py", "usercustomize.py")))
        for name in names:
            h.update(f"\0{d.as_posix()}/{name}\0".encode("utf-8", "replace"))
            h.update(_file_sha(d / name).encode())
        for dist in sorted(p for p in d.iterdir() if p.is_dir() and p.suffix in (".dist-info", ".egg-info")):
            h.update(f"\0dist:{dist.name}\0".encode("utf-8", "replace"))
            ep = dist / "entry_points.txt"
            if ep.is_file():
                h.update(_file_sha(ep).encode())
    return h.hexdigest()


def test_same_site_fingerprint_as_before(tmp_path):
    d = tmp_path / "site"
    d.mkdir()
    assert site_fingerprint([d, tmp_path / "missing"]) == _site_reference([d, tmp_path / "missing"])
    (d / "a.pth").write_text("import x\n", encoding="utf-8")
    (d / "sitecustomize.py").write_text("X = 1\n", encoding="utf-8")
    (d / "dir.pth").mkdir()                     # a folder with the name of one is not one
    (d / "z.dist-info").mkdir()
    (d / "z.dist-info" / "entry_points.txt").write_text("[pytest11]\nx = y\n", encoding="utf-8")
    (d / "b.egg-info").mkdir()
    (d / "c.dist-info").write_text("a file, not a distribution\n", encoding="utf-8")
    (d / "plain.py").write_text("\n", encoding="utf-8")
    # Order: capitals, digits and a second distribution with entry points.
    for name in ("Y.dist-info", "a2.dist-info", "B9.egg-info"):
        (d / name).mkdir()
        (d / name / "entry_points.txt").write_text(f"[pytest11]\n{name} = y\n", encoding="utf-8")
    first = site_fingerprint([d])
    assert first == _site_reference([d])
    (d / "a.pth").write_text("import y\n", encoding="utf-8")
    assert site_fingerprint([d]) != first and site_fingerprint([d]) == _site_reference([d])


def test_outside_git_a_recorded_base_bounds_nothing(tmp_path):
    (tmp_path / "x.py").write_text("X = 1\n", encoding="utf-8")
    depth, reasons, facts = required_depth(tmp_path, "abc123")
    assert depth == "full" and "no git repository" in reasons[0] and facts["files"] == ["x.py"]
