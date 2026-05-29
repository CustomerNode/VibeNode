"""
[subsessions phase -1] Guard load-bearing Compose identifiers across the rename.

Option B of the Subsessions feature (spec §4.5) renames the user-visible
label "Compose" → "Subsessions" but explicitly keeps every internal
identifier untouched:

  - The on-disk directory:   ``compose-projects/``
  - The per-project context: ``compose-context.json``
  - The Python dataclass:    ``ComposeProject``
  - The HTTP route prefix:   ``/api/compose/``

A half-finished rename that updates user-visible text and accidentally
sweeps an internal identifier would break:

  - Backward compatibility for users with existing
    ``~/.vibenode/compose-projects/`` data on disk (CLAUDE.md "Compose
    project-scoping" item #2, the stale-project fallback, depends on
    that exact path being stable).
  - The four CLAUDE.md Compose load-bearing fixes that read
    ``compose-context.json`` and the ``compose-projects`` directory by
    those literal names.

This test pins those four identifiers at the API surface, on disk after
a real project create, and inside the Python module path, so any rename
that leaks into a load-bearing identifier fails the test rather than
shipping silently.

Snapshot-based cleanup mirrors ``tests/test_compose_api.py`` per CLAUDE.md.

See ``docs/plans/subsessions-spec.md`` §4.5 + §13.1 test 5.
"""

import shutil
from pathlib import Path

import pytest

from app import create_app
from app.compose.models import COMPOSE_PROJECTS_DIR


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def client():
    app = create_app(testing=True)
    with app.test_client() as c:
        yield c


@pytest.fixture(autouse=True)
def cleanup_projects():
    """Snapshot-based cleanup — never name-prefix.

    Mirrors the cleanup_projects fixture in ``tests/test_compose_api.py``.
    CLAUDE.md "Compose project-scoping" item #3 explicitly bans name-prefix
    cleanup: the old ``startswith("test-")`` check missed cloned project
    folder names like ``copy-of-test-…`` and bare UUIDs, leaking 52 orphan
    projects into production data.
    """
    before = set()
    if COMPOSE_PROJECTS_DIR.is_dir():
        before = {d.name for d in COMPOSE_PROJECTS_DIR.iterdir() if d.is_dir()}
    yield
    if COMPOSE_PROJECTS_DIR.is_dir():
        for d in COMPOSE_PROJECTS_DIR.iterdir():
            if d.is_dir() and d.name not in before:
                shutil.rmtree(d, ignore_errors=True)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class TestComposeIdentifierStability:
    """The four load-bearing Compose identifiers must remain stable
    through the Option B rename.
    """

    def test_api_route_prefix_is_compose(self, client):
        """``/api/compose/board`` must continue to answer.  The Option B
        rename explicitly preserves all ``/api/compose/*`` routes
        (spec §4.5: "All ``/api/compose/*`` routes keep their paths and
        shapes").
        """
        resp = client.get('/api/compose/board')
        assert resp.status_code == 200, (
            f"GET /api/compose/board returned {resp.status_code}. "
            "Spec §4.5 keeps these routes verbatim through the rename."
        )

    def _find_project_dir(self, project_name: str) -> Path:
        """Find a project directory by the sanitized project name.

        ``scaffold_project`` writes to ``compose-projects/{sanitized_name}/``
        rather than ``compose-projects/{uuid}/``.  This helper avoids
        baking that detail into every test.
        """
        candidates = []
        if COMPOSE_PROJECTS_DIR.is_dir():
            for d in COMPOSE_PROJECTS_DIR.iterdir():
                if d.is_dir() and d.name.startswith(project_name):
                    candidates.append(d)
        assert candidates, (
            f"No directory under {COMPOSE_PROJECTS_DIR} starts with "
            f"'{project_name}'. The 'compose-projects' path component "
            "appears to be renamed or scaffolding failed."
        )
        # If there's a name collision the second project gets a "-<id8>"
        # suffix; pick the shortest match which is the exact-name dir.
        candidates.sort(key=lambda p: len(p.name))
        return candidates[0]

    def test_compose_projects_directory_name_unchanged(self, client):
        """A real POST creates a sub-folder under ``compose-projects/`` on
        disk.  That path string is load-bearing: the Compose stale-project
        fallback (CLAUDE.md fix #2) reads it by name.
        """
        resp = client.post('/api/compose/projects',
                           json={'name': 'test-rename-guard-disk'})
        assert resp.status_code == 201

        proj_dir = self._find_project_dir('test-rename-guard-disk')
        assert proj_dir.is_dir(), (
            f"Expected project directory at {proj_dir} after POST. "
            "The path component 'compose-projects' must NOT be renamed "
            "during Subsessions Option B (spec §4.5 + §6.6 'no data "
            "migration')."
        )
        # The directory's parent must be literally named 'compose-projects'.
        assert COMPOSE_PROJECTS_DIR.name == 'compose-projects', (
            f"COMPOSE_PROJECTS_DIR was renamed to "
            f"'{COMPOSE_PROJECTS_DIR.name}'. Option B forbids this — "
            "users' existing ~/.vibenode/compose-projects/ data on disk "
            "would orphan."
        )

    def test_compose_context_json_filename_unchanged(self, client):
        """Scaffolding a new project must write a file literally named
        ``compose-context.json`` into the project directory.  CLAUDE.md
        Compose fixes all reference this filename.
        """
        resp = client.post('/api/compose/projects',
                           json={'name': 'test-rename-guard-context'})
        assert resp.status_code == 201

        proj_dir = self._find_project_dir('test-rename-guard-context')
        ctx_file = proj_dir / 'compose-context.json'
        assert ctx_file.is_file(), (
            f"Expected {ctx_file} on disk after project create. "
            "The filename 'compose-context.json' must NOT be renamed "
            "during Option B (spec §4.5 keeps the file shape unchanged)."
        )

    def test_compose_project_python_class_exists(self):
        """The ``ComposeProject`` Python dataclass name is referenced
        directly by ``app/routes/compose_api.py`` and by every place
        that scans ``compose-projects/`` on disk (``list_projects``,
        ``get_project``, ``save_project``).  A rename of the class name
        is allowed in principle but must be coordinated; pinning it
        here forces an explicit decision.
        """
        from app.compose import models as compose_models
        assert hasattr(compose_models, 'ComposeProject'), (
            "ComposeProject class missing from app.compose.models. "
            "Spec §4.5 (Option B) explicitly preserves the existing "
            "data model — only the user-visible label changes."
        )
        # Sanity: it's still a dataclass with an id and a name.
        from dataclasses import fields
        field_names = {f.name for f in fields(compose_models.ComposeProject)}
        assert 'id' in field_names
        assert 'name' in field_names


class TestBoardResponseShape:
    """The Compose board API response shape must keep the keys the
    frontend depends on (``project``, ``sections``, ``sibling_projects``,
    ``status``).  A rename that accidentally renamed any of these keys
    would blank the panel.
    """

    def test_board_response_keys_for_real_project(self, client):
        """Create a real composition + section and inspect the board JSON.
        The four shape keys must be present.
        """
        resp = client.post('/api/compose/projects',
                           json={'name': 'test-rename-guard-board',
                                 'parent_project': 'proj-rename-guard'})
        pid = resp.get_json()['project']['id']
        client.post(f'/api/compose/projects/{pid}/sections',
                    json={'name': 'Section 1'})

        board_resp = client.get('/api/compose/board?project=proj-rename-guard')
        assert board_resp.status_code == 200
        data = board_resp.get_json()
        assert data is not None, (
            "Board endpoint returned null for a valid composition. "
            "Catches a rename that broke the parent_project filter."
        )
        # These four keys are load-bearing for initCompose() in the
        # frontend (CLAUDE.md Compose fix #4 references them by name).
        for key in ('project', 'sections', 'sibling_projects', 'status'):
            assert key in data, (
                f"Board response is missing required key '{key}'. "
                "Spec §4.5 keeps the response shape unchanged through "
                "the Option B rename."
            )
        assert data['project']['parent_project'] == 'proj-rename-guard'
        assert any(s['name'] == 'Section 1' for s in data['sections'])
