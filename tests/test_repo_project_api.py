"""/api/repo-project: the VibeNode repo as a project.

The Publish gate's fix sessions ("Fix errors" after a failed test run, "Fix
with AI" after a security scan) work on this repo, so git-sync.js creates them
in this repo's project.  They used to be created in the browser's active
project, which put a "Fix test failures" session in CustomerNode (2026-10-05).
"""

import app.routes.project_api as project_api
from app.config import _VIBENODE_DIR, _encode_cwd


def test_reports_this_repo(kanban_client):
    d = kanban_client.get("/api/repo-project").get_json()
    assert d["ok"] is True
    assert d["root"] == str(_VIBENODE_DIR)
    assert d["project"] == _encode_cwd(str(_VIBENODE_DIR))
    assert isinstance(d["registered"], bool)


def test_registered_reflects_project_dir(kanban_client, tmp_path, monkeypatch):
    monkeypatch.setattr(project_api, "_CLAUDE_PROJECTS", tmp_path)
    assert kanban_client.get("/api/repo-project").get_json()["registered"] is False
    (tmp_path / _encode_cwd(str(_VIBENODE_DIR))).mkdir()
    assert kanban_client.get("/api/repo-project").get_json()["registered"] is True
