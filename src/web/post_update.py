"""Data migrations that run with the code an Update just pulled.

The one-click Update pulls new code into the running web server's folder.
The browser picks up the new screen code at once (static files, live-assets
reload), but the web server keeps running the Python it started with until
its next restart.  Any data migration that ships with an update and lives
only in server code was therefore skipped for hours, while the new screen
code already relied on it, and statuses the person had set could change.

``src/web/git_ops.py`` runs this file in a FRESH interpreter (so it is the pulled
code) right after a pull that brought something in.  The web server also runs
the same migrations itself on its first session listing after a restart, so
either path is enough.  Every migration here must be idempotent and run once
(a marker file), and must never need the web server or the Session Engine.

Usage: ``python -m app.post_update`` from the repo folder; prints JSON.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path


NOTES = Path("docs") / "plans" / "PROJECT-NOTES.md"
_PUBLIC_MAX_LINES = 60


def restore_private_notes(proj: Path = None) -> str:
    """Give this copy its private project notes back.

    The project notes used to be CLAUDE.md, published. They moved to the
    private NOTES file and every published version of them was removed, so an
    Update on another computer replaces its long CLAUDE.md with the short
    public one. Its own last long copy is still in its git records (the reflog);
    this writes it to NOTES, never to a tracked file. Runs once: an existing
    NOTES file is never touched.
    """
    import subprocess
    proj = proj or Path(__file__).resolve().parents[2]
    dest = proj / NOTES
    if dest.exists():
        return "present"

    def _long(text):
        return text if text and text.count("\n") > _PUBLIC_MAX_LINES else None

    cur = proj / "CLAUDE.md"
    text = _long(cur.read_text(encoding="utf-8", errors="ignore")) if cur.is_file() else None
    if text is None:
        try:
            refs = subprocess.run(["git", "-C", str(proj), "log", "-g", "--format=%H", "-n", "300", "HEAD"],
                                  capture_output=True, text=True, timeout=20).stdout.split()
        except Exception:
            refs = []
        for h in refs:
            r = subprocess.run(["git", "-C", str(proj), "show", f"{h}:CLAUDE.md"],
                               capture_output=True, text=True, timeout=10, encoding="utf-8", errors="ignore")
            text = _long(r.stdout) if r.returncode == 0 else None
            if text:
                break
    if not text:
        return "none found"
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(text, encoding="utf-8")
    return "restored"


# ── The src layout ────────────────────────────────────────────────────────
# The code moved under src/. A computer that pulls the move still has its web
# server, Session Engine and helpers running the code they started with, and
# its sign-in entries, its Startup-folder helper and the end-of-turn checker
# hook point at the old files. Until those processes restart (the engine does
# by itself when idle) the old spots must keep working, and the entries are
# re-pointed. Everything here is local and never published: the top of
# .gitignore ignores every name below.
OLD_DIRS = {"app": "src/web", "daemon": "src/engine", "common": "src/shared",
            "pipeline": "src/features/pipeline", "ai_runs": "src/features/experiments",
            "workforce": "src/features/workforce", "static": "src/web/static",
            "templates": "src/web/templates",
            # new top-level names, for old processes importing moved code
            "engine": "src/engine", "web": "src/web", "shared": "src/shared",
            "features": "src/features", "launcher": "src/launcher"}
OLD_SCRIPTS = ("run.py", "session_manager.py", "reviver.py")
OLD_MODULES = ("_early_boot", "process_label")
_FORWARDER = """# Local forwarder (not published): something started before VibeNode's code
# moved to src/ ran this path. It runs the file's new home.
import runpy, sys, pathlib
_p = pathlib.Path(__file__).resolve().parent / "src" / "launcher" / %r
sys.path.insert(0, str(_p.parents[1]))
sys.argv[0] = str(_p)
runpy.run_path(str(_p), run_name="__main__")
"""
_SHIM = """# Local shim (not published): an older process imports this by its old name.
import sys, pathlib
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent / "src"))
from launcher.%s import *  # noqa: F401,F403
"""


def _only_caches(folder: Path) -> bool:
    """True when a folder left behind by a pull holds nothing but Python caches."""
    try:
        return all(f.is_dir() or f.suffix in (".pyc", ".pyo") or "__pycache__" in f.parts
                   for f in folder.rglob("*"))
    except OSError:
        return False


def _link_dir(link: Path, target: Path) -> bool:
    if link.is_dir() and not link.is_symlink() and _only_caches(link):
        import shutil
        shutil.rmtree(link, ignore_errors=True)
    if link.exists() or link.is_symlink():
        return False
    if sys.platform == "win32":
        import subprocess
        r = subprocess.run(["cmd", "/c", "mklink", "/J", str(link), str(target)],
                           capture_output=True, text=True, timeout=20)
        return r.returncode == 0
    os.symlink(os.path.relpath(target, link.parent), link)
    return True


def bridge_old_layout(proj: Path = None) -> dict:
    """Keep the old file spots working for processes started before the move."""
    proj = Path(proj or Path(__file__).resolve().parents[2])
    if not (proj / "src" / "launcher" / "session_manager.py").is_file():
        return {"skipped": "no src layout"}
    made = []
    for name, target in OLD_DIRS.items():
        if (proj / target).is_dir() and _link_dir(proj / name, proj / target):
            made.append(name)
    for name in OLD_SCRIPTS:
        p = proj / name
        if not (p.exists() or p.is_symlink()):
            p.write_text(_FORWARDER % name, encoding="utf-8"); made.append(name)
    for mod in OLD_MODULES:
        p = proj / (mod + ".py")
        if not (p.exists() or p.is_symlink()):
            p.write_text(_SHIM % mod, encoding="utf-8"); made.append(p.name)
    return {"made": made}


def _repoint(path: Path, proj: Path) -> bool:
    """Rewrite an entry that starts a moved file so it names the new home."""
    try:
        text = path.read_text(encoding="utf-8")
    except Exception:
        return False
    new = text
    root = str(proj)
    for name in ("session_manager.py", "reviver.py", "run.py"):
        for sep in ("/", "\\"):
            old = root + sep + name
            moved = root + sep + "src" + sep + "launcher" + sep + name
            if old in new and moved not in new:
                new = new.replace(old, moved)
    if new == text:
        return False
    path.write_text(new, encoding="utf-8")
    return True


def repoint_entries(proj: Path = None, home: Path = None) -> list:
    """Re-point this computer's sign-in entries and the end-of-turn checker hook."""
    proj = Path(proj or Path(__file__).resolve().parents[2])
    home = Path(home or Path.home())
    done = []
    entries = [home / ".config" / "autostart" / "vibenode.desktop",
               home / "Library" / "LaunchAgents" / "com.vibenode.start.plist"]
    appdata = os.environ.get("APPDATA")
    if appdata:
        startup = Path(appdata) / "Microsoft" / "Windows" / "Start Menu" / "Programs" / "Startup"
        entries.append(startup / "VibeNode.vbs")
        entries += sorted(startup.glob("VibeNodeReviver_*.vbs")) if startup.is_dir() else []
    entries += sorted((home / ".config" / "autostart").glob("vibenode-reviver*.desktop")) \
        if (home / ".config" / "autostart").is_dir() else []
    for e in entries:
        if e.is_file() and _repoint(e, proj):
            done.append(str(e))
    settings = home / ".claude" / "settings.json"
    try:
        text = settings.read_text(encoding="utf-8")
    except Exception:
        text = ""
    old_hook = (proj / "daemon" / "smart_termination_hook.py").as_posix()
    if old_hook in text:
        settings.write_text(text.replace(old_hook, (proj / "src" / "engine" / "smart_termination_hook.py").as_posix()),
                            encoding="utf-8")
        done.append(str(settings))
    return done


def run() -> dict:
    out = {}
    for name, fn in (("bridge", bridge_old_layout), ("entries", repoint_entries)):
        try:
            out[name] = fn()
        except Exception as e:   # report, never fail the update
            out[name + "_error"] = str(e)
    try:
        out["private_notes"] = restore_private_notes()
    except Exception as e:      # report, never fail the update
        out["private_notes_error"] = str(e)
    try:
        from . import session_holds
        out["session_holds"] = session_holds.migrate_all()
    except Exception as e:      # report, never fail the update
        out["session_holds_error"] = str(e)
    return out


if __name__ == "__main__":
    print(json.dumps(run()))
    sys.exit(0)
