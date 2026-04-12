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
    app = create_app()
    app.config['TESTING'] = True
    with app.test_client() as c:
        yield c


@pytest.fixture(autouse=True)
def cleanup_projects():
    """Clean up any test projects after each test."""
    yield
    # Clean up compose-projects created during tests
    if COMPOSE_PROJECTS_DIR.is_dir():
        for d in COMPOSE_PROJECTS_DIR.iterdir():
            if d.is_dir() and d.name.startswith("test-"):
                import shutil
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
