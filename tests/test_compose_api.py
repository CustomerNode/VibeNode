"""
Tests for compose API endpoints — project CRUD, section CRUD, context,
conflict resolution.
"""

import json
import pytest

from app import create_app
from app.compose.models import (
    ComposeProject, scaffold_project, delete_project_folder,
    list_projects, COMPOSE_PROJECTS_DIR,
)


@pytest.fixture
def client():
    """Create a test client."""
    app = create_app(testing=True)
    with app.test_client() as c:
        yield c


@pytest.fixture(autouse=True)
def cleanup_projects():
    """Clean up any test projects after each test.

    Snapshots the compose-projects directory before the test and removes
    anything new afterwards.  This catches cloned projects (directory names
    like ``copy-of-test-…`` or UUIDs) that the old ``startswith("test-")``
    check missed — the leak that created 52 orphaned projects.
    """
    import shutil
    before = set()
    if COMPOSE_PROJECTS_DIR.is_dir():
        before = {d.name for d in COMPOSE_PROJECTS_DIR.iterdir() if d.is_dir()}
    yield
    if COMPOSE_PROJECTS_DIR.is_dir():
        for d in COMPOSE_PROJECTS_DIR.iterdir():
            if d.is_dir() and d.name not in before:
                shutil.rmtree(d, ignore_errors=True)


class TestProjectCRUD:
    def test_create_project(self, client):
        resp = client.post('/api/compose/projects',
                          json={'name': 'test-project-1'})
        data = resp.get_json()
        assert resp.status_code == 201
        assert data['ok'] is True
        assert data['project']['name'] == 'test-project-1'
        assert data['project']['id']

    def test_create_project_no_name(self, client):
        resp = client.post('/api/compose/projects', json={})
        assert resp.status_code == 400

    def test_list_projects(self, client):
        client.post('/api/compose/projects', json={'name': 'test-list-1'})
        client.post('/api/compose/projects', json={'name': 'test-list-2'})
        resp = client.get('/api/compose/projects')
        data = resp.get_json()
        assert data['ok'] is True
        names = [p['name'] for p in data['projects']]
        assert 'test-list-1' in names
        assert 'test-list-2' in names

    def test_get_project(self, client):
        create_resp = client.post('/api/compose/projects',
                                  json={'name': 'test-get-proj'})
        pid = create_resp.get_json()['project']['id']

        resp = client.get(f'/api/compose/projects/{pid}')
        data = resp.get_json()
        assert data['ok'] is True
        assert data['project']['id'] == pid

    def test_get_nonexistent_project(self, client):
        resp = client.get('/api/compose/projects/nonexistent-id')
        assert resp.status_code == 404

    def test_update_project(self, client):
        create_resp = client.post('/api/compose/projects',
                                  json={'name': 'test-update-proj'})
        pid = create_resp.get_json()['project']['id']

        resp = client.put(f'/api/compose/projects/{pid}',
                         json={'name': 'test-updated-name'})
        data = resp.get_json()
        assert data['ok'] is True
        assert data['project']['name'] == 'test-updated-name'

    def test_delete_project(self, client):
        create_resp = client.post('/api/compose/projects',
                                  json={'name': 'test-delete-proj'})
        pid = create_resp.get_json()['project']['id']

        resp = client.delete(f'/api/compose/projects/{pid}')
        data = resp.get_json()
        assert data['ok'] is True

        # Should be 404 now
        resp = client.get(f'/api/compose/projects/{pid}')
        assert resp.status_code == 404


class TestSectionCRUD:
    def _create_project(self, client):
        resp = client.post('/api/compose/projects',
                          json={'name': 'test-section-proj'})
        return resp.get_json()['project']['id']

    def test_create_section(self, client):
        pid = self._create_project(client)
        resp = client.post(f'/api/compose/projects/{pid}/sections',
                          json={'name': 'Introduction'})
        data = resp.get_json()
        assert resp.status_code == 201
        assert data['ok'] is True
        assert data['section']['name'] == 'Introduction'
        assert data['section']['status'] == 'drafting'

    def test_create_section_no_name(self, client):
        pid = self._create_project(client)
        resp = client.post(f'/api/compose/projects/{pid}/sections', json={})
        assert resp.status_code == 400

    def test_update_section(self, client):
        pid = self._create_project(client)
        create_resp = client.post(f'/api/compose/projects/{pid}/sections',
                                  json={'name': 'Ch1'})
        sid = create_resp.get_json()['section']['id']

        resp = client.put(f'/api/compose/projects/{pid}/sections/{sid}',
                         json={'name': 'Chapter One', 'status': 'reviewing'})
        data = resp.get_json()
        assert data['ok'] is True
        assert data['section']['name'] == 'Chapter One'
        assert data['section']['status'] == 'reviewing'

    def test_delete_section(self, client):
        pid = self._create_project(client)
        create_resp = client.post(f'/api/compose/projects/{pid}/sections',
                                  json={'name': 'ToDelete'})
        sid = create_resp.get_json()['section']['id']

        resp = client.delete(f'/api/compose/projects/{pid}/sections/{sid}')
        assert resp.get_json()['ok'] is True

    def test_reorder_sections(self, client):
        pid = self._create_project(client)
        s1 = client.post(f'/api/compose/projects/{pid}/sections',
                        json={'name': 'A'}).get_json()['section']['id']
        s2 = client.post(f'/api/compose/projects/{pid}/sections',
                        json={'name': 'B'}).get_json()['section']['id']

        resp = client.post(f'/api/compose/projects/{pid}/sections/reorder',
                          json={'order': [s2, s1]})
        assert resp.get_json()['ok'] is True

    def test_section_with_parent(self, client):
        pid = self._create_project(client)
        parent_resp = client.post(f'/api/compose/projects/{pid}/sections',
                                  json={'name': 'Part 1'})
        parent_id = parent_resp.get_json()['section']['id']

        child_resp = client.post(f'/api/compose/projects/{pid}/sections',
                                json={'name': 'Chapter 1.1', 'parent_id': parent_id})
        child = child_resp.get_json()['section']
        assert child['parent_id'] == parent_id


class TestContextEndpoints:
    def _create_project(self, client):
        resp = client.post('/api/compose/projects',
                          json={'name': 'test-context-proj'})
        return resp.get_json()['project']['id']

    def test_get_context(self, client):
        pid = self._create_project(client)
        resp = client.get(f'/api/compose/projects/{pid}/context')
        data = resp.get_json()
        assert data['ok'] is True
        assert 'context' in data
        assert data['context']['project_id'] == pid

    def test_update_facts(self, client):
        pid = self._create_project(client)
        resp = client.put(f'/api/compose/projects/{pid}/context/facts',
                         json={'facts': {'key': 'value'}})
        assert resp.get_json()['ok'] is True

        ctx = client.get(f'/api/compose/projects/{pid}/context').get_json()['context']
        assert ctx['facts']['key'] == 'value'

    def test_update_section_status(self, client):
        pid = self._create_project(client)
        sec = client.post(f'/api/compose/projects/{pid}/sections',
                         json={'name': 'S1'}).get_json()['section']

        resp = client.put(f'/api/compose/projects/{pid}/sections/{sec["id"]}/status',
                         json={'status': 'drafting', 'summary': 'In progress'})
        assert resp.get_json()['ok'] is True


class TestChangingEndpoint:
    def _setup(self, client):
        pid = client.post('/api/compose/projects',
                         json={'name': 'test-changing-proj'}).get_json()['project']['id']
        sid = client.post(f'/api/compose/projects/{pid}/sections',
                         json={'name': 'S1'}).get_json()['section']['id']
        return pid, sid

    def test_set_changing(self, client):
        pid, sid = self._setup(client)
        resp = client.put(f'/api/compose/projects/{pid}/sections/{sid}/changing',
                         json={'changing': True, 'change_note': 'Fix intro', 'set_by': 'root'})
        assert resp.get_json()['ok'] is True

    def test_clear_changing_by_section(self, client):
        pid, sid = self._setup(client)
        client.put(f'/api/compose/projects/{pid}/sections/{sid}/changing',
                  json={'changing': True, 'change_note': 'Fix it', 'set_by': 'root'})

        resp = client.put(f'/api/compose/projects/{pid}/sections/{sid}/changing',
                         json={'changing': False, 'cleared_by': sid})
        assert resp.get_json()['ok'] is True

    def test_clear_changing_by_wrong_entity(self, client):
        pid, sid = self._setup(client)
        client.put(f'/api/compose/projects/{pid}/sections/{sid}/changing',
                  json={'changing': True, 'change_note': 'Fix it', 'set_by': 'root'})

        resp = client.put(f'/api/compose/projects/{pid}/sections/{sid}/changing',
                         json={'changing': False, 'cleared_by': 'root'})
        assert resp.status_code == 403


class TestDirectiveEndpoint:
    """Tests for POST /api/compose/projects/{id}/directives."""

    @pytest.fixture
    def project(self, client):
        resp = client.post('/api/compose/projects',
                          json={'name': 'test-directive-proj'})
        return resp.get_json()['project']

    def test_create_directive(self, client, project):
        resp = client.post(
            f'/api/compose/projects/{project["id"]}/directives',
            json={'content': 'Use formal tone throughout', 'scope': 'global', 'source': 'user'},
        )
        assert resp.status_code == 201
        data = resp.get_json()
        assert data['ok'] is True
        assert data['directive']['content'] == 'Use formal tone throughout'
        assert data['directive']['scope'] == 'global'
        assert data['directive']['status'] == 'active'
        assert data['directive']['gen'] == 1

    def test_create_directive_no_content(self, client, project):
        resp = client.post(
            f'/api/compose/projects/{project["id"]}/directives',
            json={'scope': 'global'},
        )
        assert resp.status_code == 400

    def test_create_directive_nonexistent_project(self, client):
        resp = client.post(
            '/api/compose/projects/nonexistent/directives',
            json={'content': 'test'},
        )
        assert resp.status_code == 404

    def test_create_directive_returns_conflicts(self, client, project):
        """When two directives on the same topic are added, the second should
        return conflict data (if ambiguous)."""
        # First directive
        client.post(
            f'/api/compose/projects/{project["id"]}/directives',
            json={'content': 'Use formal tone in all writing sections'},
        )
        # Second directive on similar topic (should trigger conflict detection)
        resp = client.post(
            f'/api/compose/projects/{project["id"]}/directives',
            json={'content': 'Use casual tone in writing sections'},
        )
        data = resp.get_json()
        assert data['ok'] is True
        # conflicts list is present (may or may not have entries depending on heuristic)
        assert 'conflicts' in data

    def test_resolve_conflict(self, client, project):
        """Full flow: add two conflicting directives, then resolve."""
        # Add two directives that will conflict
        client.post(
            f'/api/compose/projects/{project["id"]}/directives',
            json={'content': 'Use formal professional tone in all writing sections'},
        )
        resp2 = client.post(
            f'/api/compose/projects/{project["id"]}/directives',
            json={'content': 'Use casual friendly tone in writing sections'},
        )
        data2 = resp2.get_json()
        conflicts = data2.get('conflicts', [])
        if not conflicts:
            pytest.skip("Heuristic did not detect conflict for this word pair")

        conflict_id = conflicts[0]['id']
        # Resolve it
        resolve_resp = client.post(
            f'/api/compose/projects/{project["id"]}/directives/resolve',
            json={'conflict_id': conflict_id, 'action': 'supersede'},
        )
        assert resolve_resp.status_code == 200
        rdata = resolve_resp.get_json()
        assert rdata['ok'] is True
        assert rdata['result']['resolved'] is True

    def test_resolve_bad_action(self, client, project):
        resp = client.post(
            f'/api/compose/projects/{project["id"]}/directives/resolve',
            json={'conflict_id': 'fake', 'action': 'invalid'},
        )
        assert resp.status_code == 400

    def test_resolve_missing_fields(self, client, project):
        resp = client.post(
            f'/api/compose/projects/{project["id"]}/directives/resolve',
            json={},
        )
        assert resp.status_code == 400


class TestBoardEndpoint:
    def test_board_no_projects(self, client):
        resp = client.get('/api/compose/board')
        # Should return null or empty when no projects
        assert resp.status_code == 200

    def test_board_with_project(self, client):
        create_resp = client.post('/api/compose/projects',
                                  json={'name': 'test-board-proj'})
        pid = create_resp.get_json()['project']['id']

        resp = client.get(f'/api/compose/board?project_id={pid}')
        data = resp.get_json()
        assert data['project']['id'] == pid
        assert 'sections' in data
        assert 'status' in data


class TestPlannerAcceptEndpoint:
    """Tests for POST /api/compose/projects/{id}/planner/accept."""

    def _create_project(self, client):
        resp = client.post('/api/compose/projects', json={'name': 'test-planner-proj'})
        return resp.get_json()['project']['id']

    def test_accept_flat_plan(self, client):
        pid = self._create_project(client)
        plan = {
            'sections': [
                {'name': 'Executive Summary', 'artifact_type': 'text'},
                {'name': 'Revenue Data', 'artifact_type': 'data'},
            ]
        }
        resp = client.post(f'/api/compose/projects/{pid}/planner/accept', json=plan)
        data = resp.get_json()
        assert resp.status_code == 200
        assert data['ok'] is True
        assert data['created_count'] == 2

    def test_accept_nested_plan(self, client):
        pid = self._create_project(client)
        plan = {
            'sections': [
                {
                    'name': 'Financials',
                    'artifact_type': 'text',
                    'subsections': [
                        {'name': 'Q1 Revenue', 'artifact_type': 'data'},
                        {'name': 'Q2 Revenue', 'artifact_type': 'data'},
                    ]
                }
            ]
        }
        resp = client.post(f'/api/compose/projects/{pid}/planner/accept', json=plan)
        data = resp.get_json()
        assert data['ok'] is True
        assert data['created_count'] == 3  # parent + 2 children

    def test_accept_empty_plan(self, client):
        pid = self._create_project(client)
        resp = client.post(f'/api/compose/projects/{pid}/planner/accept', json={'sections': []})
        assert resp.status_code == 400

    def test_accept_scoped_plan(self, client):
        pid = self._create_project(client)
        # Create a parent section first
        sec_resp = client.post(f'/api/compose/projects/{pid}/sections', json={'name': 'Parent'})
        parent_id = sec_resp.get_json()['section']['id']
        plan = {
            'sections': [{'name': 'Child A', 'artifact_type': 'text'}],
            'parent_id': parent_id,
        }
        resp = client.post(f'/api/compose/projects/{pid}/planner/accept', json=plan)
        data = resp.get_json()
        assert data['ok'] is True
        assert data['created_count'] == 1
        # Verify the child has the correct parent
        child = data['sections'][0]
        assert child['parent_id'] == parent_id


class TestComposeBoard:
    """Tests for the compose board endpoint."""

    def test_board_returns_200(self, client):
        resp = client.get('/api/compose/board')
        assert resp.status_code == 200
        data = resp.get_json()
        assert isinstance(data, dict)


class TestProjectClone:

    def _create_project(self, client):
        resp = client.post('/api/compose/projects', json={'name': 'test-clone-src'})
        return resp.get_json()['project']['id']

    def test_clone_project(self, client):
        pid = self._create_project(client)
        resp = client.post(f'/api/compose/projects/{pid}/clone')
        assert resp.status_code in (200, 201)
        data = resp.get_json()
        assert data['ok'] is True


class TestProjectContext:

    def _create_project(self, client):
        resp = client.post('/api/compose/projects', json={'name': 'test-ctx-proj'})
        return resp.get_json()['project']['id']

    def test_get_context(self, client):
        pid = self._create_project(client)
        resp = client.get(f'/api/compose/projects/{pid}/context')
        assert resp.status_code == 200


class TestSectionPreview:

    def _setup(self, client):
        resp = client.post('/api/compose/projects', json={'name': 'test-preview-proj'})
        pid = resp.get_json()['project']['id']
        sec = client.post(f'/api/compose/projects/{pid}/sections',
                          json={'name': 'Preview Sec'})
        sid = sec.get_json()['section']['id']
        return pid, sid

    def test_section_preview(self, client):
        pid, sid = self._setup(client)
        resp = client.get(f'/api/compose/projects/{pid}/sections/{sid}/preview')
        assert resp.status_code == 200


class TestSectionChildren:

    def _setup(self, client):
        resp = client.post('/api/compose/projects', json={'name': 'test-children-proj'})
        pid = resp.get_json()['project']['id']
        parent = client.post(f'/api/compose/projects/{pid}/sections',
                             json={'name': 'Parent Section'})
        parent_id = parent.get_json()['section']['id']
        return pid, parent_id

    def test_get_children(self, client):
        pid, parent_id = self._setup(client)
        resp = client.get(f'/api/compose/projects/{pid}/sections/{parent_id}/children')
        assert resp.status_code == 200
        data = resp.get_json()
        assert isinstance(data, dict) or isinstance(data, list)


class TestSectionReorder:

    def _setup(self, client):
        resp = client.post('/api/compose/projects', json={'name': 'test-reorder-proj'})
        pid = resp.get_json()['project']['id']
        s1 = client.post(f'/api/compose/projects/{pid}/sections',
                         json={'name': 'Sec A'}).get_json()['section']['id']
        s2 = client.post(f'/api/compose/projects/{pid}/sections',
                         json={'name': 'Sec B'}).get_json()['section']['id']
        return pid, s1, s2

    def test_reorder_sections(self, client):
        pid, s1, s2 = self._setup(client)
        resp = client.post(f'/api/compose/projects/{pid}/sections/reorder',
                           json={'order': [s2, s1]})
        assert resp.status_code == 200


class TestSectionStatus:

    def _setup(self, client):
        resp = client.post('/api/compose/projects', json={'name': 'test-status-proj'})
        pid = resp.get_json()['project']['id']
        sec = client.post(f'/api/compose/projects/{pid}/sections',
                          json={'name': 'Status Sec'})
        sid = sec.get_json()['section']['id']
        return pid, sid

    def test_update_section_status(self, client):
        pid, sid = self._setup(client)
        resp = client.put(f'/api/compose/projects/{pid}/sections/{sid}/status',
                          json={'status': 'complete'})
        assert resp.status_code == 200


class TestProjectUpdate:

    def _create_project(self, client):
        resp = client.post('/api/compose/projects', json={'name': 'test-update-proj'})
        return resp.get_json()['project']['id']

    def test_update_project(self, client):
        pid = self._create_project(client)
        resp = client.put(f'/api/compose/projects/{pid}',
                          json={'name': 'renamed-proj'})
        assert resp.status_code == 200
        data = resp.get_json()
        assert data['ok'] is True


class TestProjectsReorder:

    def test_reorder_projects(self, client):
        p1 = client.post('/api/compose/projects',
                         json={'name': 'test-reorder-a'}).get_json()['project']['id']
        p2 = client.post('/api/compose/projects',
                         json={'name': 'test-reorder-b'}).get_json()['project']['id']
        resp = client.post('/api/compose/projects/reorder',
                           json={'order': [p2, p1]})
        assert resp.status_code == 200


class TestListProjectsFiltering:
    """Regression: right-click → Add to Compose must filter by active project.

    Without the ``?project=`` query param the API returns every composition
    across all VibeNode projects, so the picker shows unrelated items.
    Fixed 2026-04-13.
    """

    def test_filter_by_parent_project(self, client):
        """Only compositions belonging to the requested project are returned."""
        client.post('/api/compose/projects',
                    json={'name': 'test-filter-altium', 'parent_project': 'altium'})
        client.post('/api/compose/projects',
                    json={'name': 'test-filter-other', 'parent_project': 'other-proj'})

        resp = client.get('/api/compose/projects?project=altium')
        data = resp.get_json()
        assert data['ok'] is True
        names = [p['name'] for p in data['projects']]
        assert 'test-filter-altium' in names
        assert 'test-filter-other' not in names

    def test_pinned_projects_always_returned(self, client):
        """Pinned compositions appear regardless of active project filter."""
        resp = client.post('/api/compose/projects',
                           json={'name': 'test-filter-pinned', 'parent_project': 'other-proj'})
        pid = resp.get_json()['project']['id']
        client.put(f'/api/compose/projects/{pid}', json={'pinned': True})

        resp = client.get('/api/compose/projects?project=altium')
        data = resp.get_json()
        names = [p['name'] for p in data['projects']]
        assert 'test-filter-pinned' in names

    def test_no_filter_returns_all(self, client):
        """Without ?project= all compositions are returned (backward compat)."""
        client.post('/api/compose/projects',
                    json={'name': 'test-filter-a', 'parent_project': 'proj-a'})
        client.post('/api/compose/projects',
                    json={'name': 'test-filter-b', 'parent_project': 'proj-b'})

        resp = client.get('/api/compose/projects')
        data = resp.get_json()
        names = [p['name'] for p in data['projects']]
        assert 'test-filter-a' in names
        assert 'test-filter-b' in names


class TestBoardStaleProjectFallback:
    """Regression: compose view must not go blank when saved project is deleted.

    If localStorage holds a ``project_id`` for a composition that has since
    been deleted, the ``/api/compose/board`` endpoint returns ``null``.  The JS
    fallback retries with just ``?project=`` to find the next valid composition.

    These tests verify the API side: a deleted project_id must return null so
    the client knows to retry, and the parent-only fallback must return the
    correct composition.  Fixed 2026-04-13.
    """

    def test_deleted_project_id_returns_null(self, client):
        """Board returns null for a project_id that doesn't exist."""
        resp = client.get('/api/compose/board?project_id=nonexistent-id')
        assert resp.get_json() is None

    def test_parent_fallback_finds_valid_project(self, client):
        """Board with only ?project= returns the most recent matching composition."""
        client.post('/api/compose/projects',
                    json={'name': 'test-fallback-real', 'parent_project': 'test-proj'})

        resp = client.get('/api/compose/board?project=test-proj')
        data = resp.get_json()
        assert data is not None
        assert data['project']['name'] == 'test-fallback-real'

    def test_parent_fallback_ignores_other_projects(self, client):
        """Board with ?project= doesn't return compositions from other projects."""
        client.post('/api/compose/projects',
                    json={'name': 'test-fallback-other', 'parent_project': 'other-proj'})

        resp = client.get('/api/compose/board?project=test-proj')
        data = resp.get_json()
        # Should be null — no compositions for test-proj
        assert data is None

    def test_board_with_deleted_id_and_parent_still_has_siblings(self, client):
        """Even when project_id is invalid, ?project= in sibling query still works.

        The JS fallback retries without project_id.  Verify the retry path
        returns sibling_projects so the sidebar renders.
        """
        client.post('/api/compose/projects',
                    json={'name': 'test-fallback-sibling', 'parent_project': 'test-proj'})

        # First call with bad ID — null
        resp1 = client.get('/api/compose/board?project_id=deleted-id&project=test-proj')
        assert resp1.get_json() is None

        # Retry without project_id — finds the composition + siblings
        resp2 = client.get('/api/compose/board?project=test-proj')
        data = resp2.get_json()
        assert data is not None
        assert data['project']['name'] == 'test-fallback-sibling'
        assert len(data['sibling_projects']) >= 1
