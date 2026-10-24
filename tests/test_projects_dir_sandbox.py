"""No test may create a project folder in the user's real ~/.claude/projects.

Every folder there is a project in the sidebar. ``test_add_project_mode_*``
created two per suite run because ``app.routes.project_api`` holds its own
copy of ``_CLAUDE_PROJECTS`` that the sandbox did not patch; the user found
a sidebar full of empty pytest projects (2026-10-23).
"""

import importlib
from pathlib import Path

import pytest

from tests import conftest


def test_every_module_holding_the_projects_dir_is_sandboxed():
    fake = Path.home() / ".claude" / "projects"
    assert "app.routes.project_api" in conftest._CLAUDE_PROJECTS_HOLDERS
    for name in conftest._CLAUDE_PROJECTS_HOLDERS:
        mod = importlib.import_module(name)
        if hasattr(mod, "_CLAUDE_PROJECTS"):
            assert Path(mod._CLAUDE_PROJECTS) == fake, name
            assert Path(mod._CLAUDE_PROJECTS).resolve() != conftest._REAL_CLAUDE_PROJECTS, name


def test_creating_a_folder_in_the_real_projects_dir_fails_loudly():
    probe = conftest._REAL_CLAUDE_PROJECTS / "C--vibenode-sandbox-probe-should-not-exist"
    with pytest.raises(RuntimeError, match="REAL Claude projects directory"):
        probe.mkdir(parents=True, exist_ok=True)
    assert not probe.exists()


def test_add_project_lands_in_the_sandbox(tmp_path):
    from app import create_app

    app = create_app(testing=True)
    folder = tmp_path / "some_project"
    folder.mkdir()
    resp = app.test_client().post(
        "/api/add-project", json={"mode": "path", "path": str(folder)})
    assert resp.status_code == 200
    created = [d for d in (Path.home() / ".claude" / "projects").iterdir()
               if "some-project" in d.name]
    assert created, "add-project should have registered the folder in the sandbox"
