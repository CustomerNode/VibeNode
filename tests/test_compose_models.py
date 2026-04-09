"""
Tests for compose data models — Step 1 verification.
"""

import json
import shutil
import pytest
from pathlib import Path

from app.compose.models import (
    ComposeProject, ComposeSection, ComposeConflict, ComposeDirective,
    ComposeFact, SectionStatus, ConflictStatus,
    scaffold_project, scaffold_section, delete_project_folder,
    list_projects, get_project, get_sections, project_dir,
    COMPOSE_PROJECTS_DIR, DEFAULT_CONTEXT, _sanitize_folder_name,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def sample_project():
    """Create and scaffold a project, clean up after test."""
    p = ComposeProject.create("Test Composition")
    pdir = scaffold_project(p)
    yield p, pdir
    delete_project_folder(p.id)


# ---------------------------------------------------------------------------
# Model serialization
# ---------------------------------------------------------------------------

class TestComposeProject:
    def test_create(self):
        p = ComposeProject.create("My Project")
        assert p.name == "My Project"
        assert p.id  # non-empty uuid
        assert p.created_at
        assert p.root_session_id is None
        assert p.shared_prompts_enabled is True

    def test_round_trip(self):
        p = ComposeProject.create("Round Trip")
        d = p.to_dict()
        p2 = ComposeProject.from_dict(d)
        assert p2.id == p.id
        assert p2.name == p.name
        assert p2.created_at == p.created_at

    def test_to_dict_json_safe(self):
        p = ComposeProject.create("JSON Safe")
        d = p.to_dict()
        # Should be JSON-serializable without errors
        json.dumps(d)


class TestComposeSection:
    def test_create(self):
        s = ComposeSection.create("proj-1", "Introduction", order=0)
        assert s.project_id == "proj-1"
        assert s.status == SectionStatus.NOT_STARTED
        assert s.changing is False

    def test_round_trip(self):
        s = ComposeSection.create("proj-1", "Chapter", parent_id="parent-1", order=2)
        d = s.to_dict()
        assert d["status"] == "not_started"  # enum serialized to string
        s2 = ComposeSection.from_dict(d)
        assert s2.status == SectionStatus.NOT_STARTED
        assert s2.parent_id == "parent-1"

    def test_status_enum_from_string(self):
        d = {"id": "x", "project_id": "p", "name": "N", "status": "working", "order": 0}
        s = ComposeSection.from_dict(d)
        assert s.status == SectionStatus.WORKING


class TestComposeConflict:
    def test_create(self):
        c = ComposeConflict.create("p1", "d1", "d2", "Use X", "Use Y", "Recommend X")
        assert c.status == ConflictStatus.PENDING
        assert c.recommendation == "Recommend X"
        assert c.resolution is None

    def test_round_trip(self):
        c = ComposeConflict.create("p1", "d1", "d2", "A", "B")
        d = c.to_dict()
        assert d["status"] == "pending"
        c2 = ComposeConflict.from_dict(d)
        assert c2.status == ConflictStatus.PENDING


class TestComposeDirective:
    def test_create(self):
        d = ComposeDirective.create("global", "Write formally", "user", gen=3)
        assert d.gen == 3
        assert d.status == "active"

    def test_round_trip(self):
        d = ComposeDirective.create("section-1", "Be concise")
        dd = d.to_dict()
        d2 = ComposeDirective.from_dict(dd)
        assert d2.scope == "section-1"


class TestComposeFact:
    def test_basic(self):
        f = ComposeFact(key="revenue", value="$10M", source_section="s1")
        d = f.to_dict()
        f2 = ComposeFact.from_dict(d)
        assert f2.key == "revenue"
        assert f2.source_section == "s1"


# ---------------------------------------------------------------------------
# Scaffolding
# ---------------------------------------------------------------------------

class TestScaffolding:
    def test_scaffold_project(self, sample_project):
        p, pdir = sample_project
        assert pdir.is_dir()
        assert (pdir / "project.json").is_file()
        assert (pdir / "compose-context.json").is_file()
        assert (pdir / "brief.md").is_file()
        assert (pdir / "sections").is_dir()
        assert (pdir / "export").is_dir()

    def test_context_structure(self, sample_project):
        p, pdir = sample_project
        ctx = json.loads((pdir / "compose-context.json").read_text())
        assert ctx["project_id"] == p.id
        assert ctx["project_name"] == p.name
        assert "conflicts" in ctx
        assert "directives" in ctx
        assert "facts" in ctx
        assert "sections" in ctx

    def test_scaffold_section(self, sample_project):
        p, pdir = sample_project
        s = ComposeSection.create(p.id, "Chapter One", order=0)
        sdir = scaffold_section(p.id, s)
        assert sdir.is_dir()
        assert (sdir / "section.json").is_file()
        assert (sdir / "content").is_dir()

    def test_list_projects(self, sample_project):
        p, pdir = sample_project
        projects = list_projects()
        ids = [proj.id for proj in projects]
        assert p.id in ids

    def test_get_project(self, sample_project):
        p, pdir = sample_project
        loaded = get_project(p.id)
        assert loaded is not None
        assert loaded.name == p.name

    def test_delete_project(self):
        p = ComposeProject.create("Deletable")
        pdir = scaffold_project(p)
        assert pdir.is_dir()
        delete_project_folder(p.id)
        assert not pdir.is_dir()

    def test_sanitize_folder_name(self):
        assert _sanitize_folder_name("Hello World!") == "hello-world"
        assert _sanitize_folder_name("  spaces  ") == "spaces"
        assert _sanitize_folder_name("a/b\\c:d") == "abcd"
        assert _sanitize_folder_name("") == "unnamed"
