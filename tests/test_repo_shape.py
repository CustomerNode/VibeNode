"""VibeNode's published shape is fixed: a change to it fails here and says where things go.

The top level holds four folders and nine files. All code lives under src/, one
folder per part of the app. Anything new finds a home inside one of them; the
shape changes only by editing SHAPE below together with the top of .gitignore
(the one list of what may be published), on purpose."""
import os
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

SHAPE = {
    "folders": {"docs", "scripts", "src", "tests"},
    "files": {".gitattributes", ".gitignore", "CLAUDE.md", "CONTRIBUTING.md", "LICENSE",
              "README.md", "pyproject.toml", "requirements.txt", "vibenode.py"},
}
SRC = {"engine", "web", "shared", "launcher", "features"}
FEATURES = {"pipeline", "experiments", "workforce", "__init__.py"}

HOMES = ("code goes in src/ (engine, web, shared, launcher or features), public docs in docs/, "
         "developer tools in scripts/, tests in tests/; private notes and plans in docs/plans/ "
         "(never published)")


def _published() -> list:
    out = subprocess.run(["git", "-C", str(ROOT), "ls-files", "-c", "-o", "--exclude-standard"],
                         capture_output=True, text=True, check=True).stdout.split("\n")
    return [p for p in out if p and not (ROOT / p).is_symlink() and (ROOT / p).exists()]


def _top(paths):
    folders, files = set(), set()
    for p in paths:
        head, _, rest = p.partition("/")
        (folders if rest else files).add(head)
    return folders, files


def test_the_top_level_is_the_approved_shape():
    folders, files = _top(_published())
    extra = sorted((folders - SHAPE["folders"]) | (files - SHAPE["files"]))
    assert not extra, (f"{', '.join(extra)}: not part of VibeNode's top-level shape. Find it another "
                       f"home: {HOMES}.")
    missing = sorted((SHAPE["folders"] - folders) | (SHAPE["files"] - files))
    assert not missing, f"missing from the top level: {', '.join(missing)}"


def test_the_code_folder_is_the_approved_shape():
    inside = {p.split("/")[1] for p in _published() if p.startswith("src/") and p.count("/") >= 1}
    assert inside == SRC, (f"src/ holds {sorted(inside - SRC)}: not part of VibeNode's shape. Put it "
                           f"in engine, web, shared, launcher or features." if inside - SRC
                           else f"src/ is missing {sorted(SRC - inside)}")
    feats = {p.split("/")[2] for p in _published() if p.startswith("src/features/")}
    assert feats == FEATURES, (f"src/features holds {sorted(feats - FEATURES)}: a new feature needs "
                               f"an agreed home first" if feats - FEATURES else f"missing {sorted(FEATURES - feats)}")


def test_the_ignore_list_allows_exactly_the_shape():
    text = (ROOT / ".gitignore").read_text(encoding="utf-8")
    block = text[text.index("/*\n"):text.index("/docs/*\n")]
    allowed = {line[2:].rstrip("/") for line in block.splitlines() if line.startswith("!/")}
    assert allowed == SHAPE["folders"] | SHAPE["files"], (
        "the top of .gitignore and this test disagree; change both together, on purpose")


def test_a_new_top_level_file_is_ignored_and_the_message_names_a_home(tmp_path):
    probe = ROOT / "zz_shape_probe.md"
    probe.write_text("x\n", encoding="utf-8")
    try:
        assert subprocess.run(["git", "-C", str(ROOT), "check-ignore", "-q", probe.name]).returncode == 0
    finally:
        probe.unlink()
    assert "src/" in HOMES and "docs/plans/" in HOMES


# ── Moving a computer onto the src layout (web/post_update.py) ──────────────

def test_the_bridge_keeps_old_spots_working(tmp_path):
    from web import post_update as pu
    (tmp_path / "src" / "launcher").mkdir(parents=True)
    for d in ("web/static", "web/templates", "engine", "shared", "features/pipeline",
              "features/experiments", "features/workforce"):
        (tmp_path / "src" / d).mkdir(parents=True, exist_ok=True)
    (tmp_path / "src" / "launcher" / "session_manager.py").write_text(
        "import sys; open(sys.argv[1], 'w').write('ran ' + ' '.join(sys.argv[2:]))\n", encoding="utf-8")
    # A folder the pull emptied still holds Python's caches; it is replaced by a link.
    (tmp_path / "app" / "__pycache__").mkdir(parents=True)
    (tmp_path / "app" / "__pycache__" / "config.cpython-312.pyc").write_bytes(b"x")
    first = pu.bridge_old_layout(tmp_path)["made"]
    assert (tmp_path / "app").is_symlink() or (tmp_path / "app").resolve() == (tmp_path / "src" / "web").resolve()
    assert {"app", "daemon", "common", "static", "templates", "engine", "web"} <= set(first)
    assert (tmp_path / "daemon").resolve() == (tmp_path / "src" / "engine").resolve()
    out = tmp_path / "out.txt"
    subprocess.run([os.sys.executable, str(tmp_path / "session_manager.py"), str(out), "--background"],
                   check=True, timeout=30)
    assert out.read_text() == "ran --background"
    assert pu.bridge_old_layout(tmp_path)["made"] == []      # idempotent
    for name in pu.OLD_DIRS:
        assert subprocess.run(["git", "-C", str(ROOT), "check-ignore", "-q", name]).returncode == 0, name


def test_sign_in_entries_and_the_hook_are_repointed(tmp_path, monkeypatch):
    from web import post_update as pu
    proj, home = tmp_path / "VibeNode", tmp_path / "home"
    auto = home / ".config" / "autostart"
    auto.mkdir(parents=True)
    (auto / "vibenode.desktop").write_text(f"Exec=/usr/bin/python3 {proj}/session_manager.py --background\n")
    (home / ".claude").mkdir()
    (home / ".claude" / "settings.json").write_text(
        '{"hooks": {"Stop": [{"hooks": [{"command": "python3 -P %s/daemon/smart_termination_hook.py"}]}]}}'
        % proj.as_posix())
    monkeypatch.delenv("APPDATA", raising=False)
    done = pu.repoint_entries(proj, home)
    assert len(done) == 2
    assert f"{proj}/src/launcher/session_manager.py --background" in (auto / "vibenode.desktop").read_text()
    assert "/src/engine/smart_termination_hook.py" in (home / ".claude" / "settings.json").read_text()
    assert pu.repoint_entries(proj, home) == []               # already pointing at the new home
