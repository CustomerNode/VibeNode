"""Tests for launcher.shortcut_repair: which desktop shortcuts get rewritten."""

import json
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from launcher import shortcut_repair as sr


@pytest.fixture
def install(tmp_path):
    root = tmp_path / "VibeNode"
    script = root / "src" / "launcher" / "session_manager.py"
    icon = root / "src" / "web" / "static" / "vibenode.ico"
    pythonw = tmp_path / "py" / "pythonw.exe"
    for f in (script, icon, pythonw):
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text("")
    return SimpleNamespace(root=root, script=script, icon=icon, pythonw=pythonw)


def _healthy(i):
    return {"target": str(i.pythonw), "args": f'"{i.script}"',
            "workdir": str(i.script.parent), "icon": f"{i.icon},0"}


# ---------------------------------------------------------------------------
# belongs_to(): matched by what the shortcut points at, not its name
# ---------------------------------------------------------------------------

def test_launcher_inside_repo_belongs(install):
    info = {"target": str(install.root / "launch.bat"), "args": "", "workdir": ""}
    assert sr.belongs_to(info, install.root)


def test_script_argument_inside_repo_belongs(install):
    assert sr.belongs_to(_healthy(install) | {"workdir": ""}, install.root)


def test_start_folder_inside_repo_belongs(install):
    info = {"target": r"C:\Other\pythonw.exe", "args": "", "workdir": str(install.root)}
    assert sr.belongs_to(info, install.root)


def test_unrelated_shortcut_is_left_alone(install, tmp_path):
    info = {"target": str(tmp_path / "Other" / "app.exe"), "args": "",
            "workdir": str(tmp_path / "Other")}
    assert not sr.belongs_to(info, install.root)


def test_sibling_folder_with_same_prefix_does_not_belong(install, tmp_path):
    other = tmp_path / "VibeNodeOld"
    info = {"target": str(other / "launch.bat"), "args": "", "workdir": str(other)}
    assert not sr.belongs_to(info, install.root)


# ---------------------------------------------------------------------------
# needs_repair()
# ---------------------------------------------------------------------------

def test_healthy_shortcut_is_not_rewritten(install):
    assert not sr.needs_repair(_healthy(install), install.icon)


def test_missing_launcher_after_a_move_is_repaired(install):
    info = _healthy(install) | {"target": str(install.root / "launch.bat")}
    assert sr.needs_repair(info, install.icon)


def test_console_python_is_repaired(install):
    python = install.pythonw.with_name("python.exe")
    python.write_text("")
    assert sr.needs_repair(_healthy(install) | {"target": str(python)}, install.icon)


def test_missing_script_is_repaired(install):
    info = _healthy(install) | {"args": f'"{install.root / "gone.py"}"'}
    assert sr.needs_repair(info, install.icon)


def test_old_icon_path_is_repaired(install):
    info = _healthy(install) | {"icon": f"{install.root / 'static' / 'vibenode.ico'},0"}
    assert sr.needs_repair(info, install.icon)


# ---------------------------------------------------------------------------
# fix_shortcuts(): reads every desktop, rewrites only broken shortcuts of ours
# ---------------------------------------------------------------------------

def test_rewrites_broken_shortcut_under_any_name(install, tmp_path, monkeypatch):
    stale = {"path": str(tmp_path / "Desk" / "Vibe Node.lnk"),
             "target": str(install.root / "launch.bat"), "args": "",
             "workdir": str(install.root), "icon": ""}
    fine = {"path": str(tmp_path / "Desk" / "VibeNode.lnk")} | _healthy(install)
    other = {"path": str(tmp_path / "Desk" / "Notes.lnk"),
             "target": str(tmp_path / "notes.exe"), "args": "", "workdir": "", "icon": ""}
    calls = []

    def fake_ps(script, env, timeout):
        calls.append(env)
        out = json.dumps([stale, fine, other]) if "VN_DESKTOPS" in env else ""
        return subprocess.CompletedProcess([], 0, stdout=out, stderr="")

    monkeypatch.setattr(sr.sys, "platform", "win32")
    monkeypatch.setattr(sr.sys, "executable", str(install.pythonw.with_name("python.exe")))
    monkeypatch.setattr(sr, "_run_ps", fake_ps)

    assert sr.fix_shortcuts(install.root, install.script, install.icon) == [stale["path"]]
    write = calls[1]
    assert write["VN_LNKS"] == stale["path"]
    assert write["VN_TARGET"] == str(install.pythonw)
    assert write["VN_SCRIPT"] == str(install.script)
    assert write["VN_ICON"] == str(install.icon)


def test_nothing_broken_means_no_write(install, tmp_path, monkeypatch):
    fine = {"path": str(tmp_path / "Desk" / "VibeNode.lnk")} | _healthy(install)
    calls = []

    def fake_ps(script, env, timeout):
        calls.append(env)
        return subprocess.CompletedProcess([], 0, stdout=json.dumps(fine), stderr="")

    monkeypatch.setattr(sr.sys, "platform", "win32")
    monkeypatch.setattr(sr.sys, "executable", str(install.pythonw.with_name("python.exe")))
    monkeypatch.setattr(sr, "_run_ps", fake_ps)

    assert sr.fix_shortcuts(install.root, install.script, install.icon) == []
    assert len(calls) == 1


def test_desktop_dirs_has_no_duplicates():
    dirs = sr.desktop_dirs()
    keys = [str(d).lower() for d in dirs]
    assert len(keys) == len(set(keys)) and dirs[0] == Path.home() / "Desktop"
