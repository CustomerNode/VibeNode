"""
Tests for deferred compose items: NB-7 (root session auto-creation),
NB-10 (sidebar grouping), NB-11 (board section cards).

NB-10 and NB-11 are frontend-only (JavaScript) and cannot be tested
server-side. They are documented here for manual verification.

NB-7 tests use the Flask test client with a mocked session_manager.
"""

import json
import pytest
from unittest.mock import MagicMock, patch

from app import create_app
from app.compose.models import (
    COMPOSE_PROJECTS_DIR, get_project, list_projects,
)


@pytest.fixture
def app_with_mock_daemon():
    """Create a test app with a mocked session_manager (DaemonClient)."""
    app = create_app()
    app.config['TESTING'] = True

    mock_sm = MagicMock()
    mock_sm.start_session.return_value = {"ok": True}
    mock_sm.is_connected = True
    app.session_manager = mock_sm

    return app, mock_sm


@pytest.fixture(autouse=True)
def cleanup_projects():
    """Clean up any test projects after each test."""
    yield
    if COMPOSE_PROJECTS_DIR.is_dir():
        import shutil
        for d in COMPOSE_PROJECTS_DIR.iterdir():
            if d.is_dir() and d.name.startswith("test-"):
                shutil.rmtree(d, ignore_errors=True)


class TestNB7RootSessionAutoCreation:
    """NB-7: Root session auto-creation on project creation."""

    def test_create_project_spawns_root_session(self, app_with_mock_daemon):
        """When a project is created, start_session is called for the root."""
        app, mock_sm = app_with_mock_daemon

        with app.test_client() as client:
            resp = client.post('/api/compose/projects',
                               json={'name': 'test-nb7-basic'})
            data = resp.get_json()

            assert resp.status_code == 201
            assert data['ok'] is True
            assert data['project']['name'] == 'test-nb7-basic'

            # start_session should have been called once
            mock_sm.start_session.assert_called_once()
            call_kwargs = mock_sm.start_session.call_args
            # Verify empty prompt
            assert call_kwargs[1]['prompt'] == '' or call_kwargs.kwargs.get('prompt') == ''
            # Verify session name includes project name
            args = call_kwargs[1] if call_kwargs[1] else call_kwargs.kwargs
            assert 'test-nb7-basic' in args.get('name', '')

    def test_create_project_sets_root_session_id(self, app_with_mock_daemon):
        """The returned project should have root_session_id set."""
        app, mock_sm = app_with_mock_daemon

        with app.test_client() as client:
            resp = client.post('/api/compose/projects',
                               json={'name': 'test-nb7-sid'})
            data = resp.get_json()

            assert resp.status_code == 201
            project = data['project']
            # root_session_id should be set (link_session was called)
            assert project['root_session_id'] is not None

    def test_create_project_daemon_failure_non_blocking(self, app_with_mock_daemon):
        """If daemon fails, project is still created successfully."""
        app, mock_sm = app_with_mock_daemon
        mock_sm.start_session.side_effect = Exception("Daemon not running")

        with app.test_client() as client:
            resp = client.post('/api/compose/projects',
                               json={'name': 'test-nb7-fail'})
            data = resp.get_json()

            assert resp.status_code == 201
            assert data['ok'] is True
            assert data['project']['name'] == 'test-nb7-fail'
            # root_session_id should be None since daemon failed
            assert data['project']['root_session_id'] is None

    def test_create_project_daemon_error_response(self, app_with_mock_daemon):
        """If daemon returns an error dict, project still created."""
        app, mock_sm = app_with_mock_daemon
        mock_sm.start_session.return_value = {"ok": False, "error": "No connection"}

        with app.test_client() as client:
            resp = client.post('/api/compose/projects',
                               json={'name': 'test-nb7-errd'})
            data = resp.get_json()

            assert resp.status_code == 201
            assert data['ok'] is True
            # Project created but root session failed — root_session_id
            # may or may not be set depending on whether link_session ran
            # The important thing is the project was created

    def test_root_session_gets_system_prompt(self, app_with_mock_daemon):
        """The root session should receive a system prompt from prompt_builder."""
        app, mock_sm = app_with_mock_daemon

        with app.test_client() as client:
            resp = client.post('/api/compose/projects',
                               json={'name': 'test-nb7-prompt'})
            data = resp.get_json()

            assert resp.status_code == 201
            call_kwargs = mock_sm.start_session.call_args
            args = call_kwargs[1] if call_kwargs[1] else call_kwargs.kwargs
            system_prompt = args.get('system_prompt', '')
            # Should contain root orchestrator prompt content
            assert system_prompt is not None
            assert 'Root Orchestrator' in system_prompt


class TestPromptBuilder:
    """Tests for prompt_builder: parse_compose_task_id and section prompt generation."""

    def test_parse_root_task_id(self):
        from app.compose.prompt_builder import parse_compose_task_id
        result = parse_compose_task_id("root:proj-123")
        assert result["role"] == "root"
        assert result["project_id"] == "proj-123"

    def test_parse_section_task_id(self):
        from app.compose.prompt_builder import parse_compose_task_id
        result = parse_compose_task_id("section:proj-123:sec-456")
        assert result["role"] == "section"
        assert result["project_id"] == "proj-123"
        assert result["section_id"] == "sec-456"

    def test_parse_invalid_task_id(self):
        from app.compose.prompt_builder import parse_compose_task_id
        with pytest.raises(ValueError):
            parse_compose_task_id("bad")

    def test_parse_unknown_role(self):
        from app.compose.prompt_builder import parse_compose_task_id
        with pytest.raises(ValueError, match="Unknown compose role"):
            parse_compose_task_id("unknown:proj-1")

    def test_parse_section_missing_section_id(self):
        from app.compose.prompt_builder import parse_compose_task_id
        with pytest.raises(ValueError, match="section_id"):
            parse_compose_task_id("section:proj-1")

    def test_section_prompt_generation(self):
        """Section agent prompt should contain section name and project context."""
        from app.compose.prompt_builder import build_compose_prompt
        from app.compose.models import (
            ComposeProject, ComposeSection,
            scaffold_project, scaffold_section, delete_project_folder,
        )
        from app.compose.context_manager import add_section_to_context

        p = ComposeProject.create("test-section-prompt-proj")
        scaffold_project(p)
        try:
            s = ComposeSection.create(p.id, "Introduction", order=0)
            scaffold_section(p.id, s)
            add_section_to_context(p.id, s)

            task_id = f"section:{p.id}:{s.id}"
            result = build_compose_prompt(task_id)
            assert result["ok"] is True
            assert result["agent_role"] == "section"
            assert "Introduction" in result["system_prompt"]
            assert "section agent" in result["system_prompt"].lower()
        finally:
            delete_project_folder(p.id)

    def test_root_prompt_includes_sections_and_facts(self):
        """Root prompt should include section list and facts when they exist."""
        from app.compose.prompt_builder import build_compose_prompt
        from app.compose.models import (
            ComposeProject, ComposeSection,
            scaffold_project, scaffold_section, delete_project_folder,
        )
        from app.compose.context_manager import (
            add_section_to_context, update_facts,
        )

        p = ComposeProject.create("test-root-prompt-proj")
        scaffold_project(p)
        try:
            s = ComposeSection.create(p.id, "Chapter One", order=0)
            scaffold_section(p.id, s)
            add_section_to_context(p.id, s)
            update_facts(p.id, {"audience": "executives"})

            task_id = f"root:{p.id}"
            result = build_compose_prompt(task_id)
            assert result["ok"] is True
            assert "Chapter One" in result["system_prompt"]
            assert "executives" in result["system_prompt"]
        finally:
            delete_project_folder(p.id)


class TestNB10SidebarGrouping:
    """NB-10: Sidebar session grouping.

    This is frontend-only (JavaScript). The getComposeSessionGroups function
    runs in the browser. Server-side testing is limited to verifying the
    data structures exist.

    Manual verification:
    1. Create a compose project
    2. Switch to compose view mode
    3. Verify sidebar shows composition name as group header
    4. Root session appears first with teal accent border
    5. Section sessions appear indented below
    6. Non-compose sessions appear below a separator
    """
    pass


class TestNB11BoardSectionCards:
    """NB-11: Board section cards.

    This is frontend-only (JavaScript). The _renderComposeSectionCards function
    runs in the browser.

    Manual verification:
    1. Create a compose project with sections
    2. Switch to compose view mode
    3. Verify sections appear as cards in status columns
    4. Cards show: name, artifact type icon, changing dot (if changing), summary
    5. Clicking a card updates the input target label
    6. Empty state shown when no sections exist
    """
    pass
