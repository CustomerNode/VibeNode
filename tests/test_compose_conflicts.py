"""
Tests for compose conflict detection — global, contextual, ambiguous paths,
recommendation generation, resolution flow, changing flag rules.
"""

import pytest

from app.compose.models import (
    ComposeProject, ComposeSection, ComposeDirective, ComposeConflict,
    ConflictStatus, scaffold_project, delete_project_folder,
)
from app.compose.context_manager import (
    add_section_to_context, add_directive, get_directives,
    read_context, get_pending_conflicts, set_changing,
)
from app.compose.conflict_detector import (
    detect_conflicts, generate_recommendation, resolve_conflict,
    _has_global_signal, _has_contextual_signal, _directives_conflict,
)


@pytest.fixture
def project():
    p = ComposeProject.create("Conflict Test")
    scaffold_project(p)
    yield p
    delete_project_folder(p.id)


class TestKeywordDetection:
    def test_global_signals(self):
        assert _has_global_signal("Actually, use formal tone everywhere")
        assert _has_global_signal("This applies across the board")
        assert _has_global_signal("Override the previous instruction")
        assert not _has_global_signal("Use formal tone in this section")

    def test_contextual_signals(self):
        assert _has_contextual_signal("For this section, use casual tone")
        assert _has_contextual_signal("Just here, use bullets")
        assert _has_contextual_signal("Only in the introduction")
        assert not _has_contextual_signal("Use casual tone everywhere")

    def test_no_partial_match(self):
        # "actually" should match as a word, not "factually"
        assert not _has_global_signal("The results were factually correct")


class TestDirectiveConflict:
    def test_similar_directives_conflict(self):
        assert _directives_conflict(
            "Use formal tone throughout",
            "Use casual tone for writing"
        )

    def test_different_topics_no_conflict(self):
        assert not _directives_conflict(
            "Target audience is executives",
            "Use bullet points for formatting"
        )

    def test_empty_content_no_conflict(self):
        assert not _directives_conflict("", "anything")
        assert not _directives_conflict("anything", "")


class TestDetectConflicts:
    def test_global_auto_resolve(self, project):
        d1 = ComposeDirective.create("global", "Use formal tone throughout")
        d1 = add_directive(project.id, d1)

        d2 = ComposeDirective.create("global", "Actually use casual tone everywhere")
        d2 = add_directive(project.id, d2)

        conflicts = detect_conflicts(project.id, d2)
        assert len(conflicts) == 0  # auto-resolved

        # d1 should be superseded
        dirs = get_directives(project.id)
        d1_data = next(d for d in dirs if d["id"] == d1.id)
        assert d1_data["status"] == "superseded"

    def test_contextual_auto_resolve(self, project):
        d1 = ComposeDirective.create("global", "Use bullet points for formatting")
        d1 = add_directive(project.id, d1)

        d2 = ComposeDirective.create("section-1", "Use paragraphs for formatting only in this section")
        d2 = add_directive(project.id, d2)

        conflicts = detect_conflicts(project.id, d2)
        assert len(conflicts) == 0  # auto-scoped

    def test_ambiguous_creates_conflict(self, project):
        d1 = ComposeDirective.create("global", "Target audience is executives")
        d1 = add_directive(project.id, d1)

        d2 = ComposeDirective.create("global", "Target audience is engineers")
        d2 = add_directive(project.id, d2)

        conflicts = detect_conflicts(project.id, d2)
        assert len(conflicts) == 1
        assert conflicts[0].status == ConflictStatus.PENDING
        assert conflicts[0].recommendation  # has recommendation text

    def test_no_conflict_different_topics(self, project):
        d1 = ComposeDirective.create("global", "Write in English")
        d1 = add_directive(project.id, d1)

        d2 = ComposeDirective.create("global", "Include charts and diagrams")
        d2 = add_directive(project.id, d2)

        conflicts = detect_conflicts(project.id, d2)
        assert len(conflicts) == 0


class TestRecommendation:
    def test_generates_text(self):
        rec = generate_recommendation("Use X", "Use Y")
        assert "Directive A" in rec
        assert "Directive B" in rec
        assert "SUPERSEDE" in rec
        assert "SCOPE" in rec


class TestResolveConflict:
    def test_supersede(self, project):
        d1 = ComposeDirective.create("global", "Target audience is executives")
        d1 = add_directive(project.id, d1)
        d2 = ComposeDirective.create("global", "Target audience is engineers")
        d2 = add_directive(project.id, d2)

        conflicts = detect_conflicts(project.id, d2)
        assert len(conflicts) == 1

        result = resolve_conflict(project.id, conflicts[0].id, "supersede")
        assert result["resolved"] is True
        assert result["action"] == "supersede"

        # Conflict should be resolved
        pending = get_pending_conflicts(project.id)
        assert len(pending) == 0

        # d1 should be superseded
        dirs = get_directives(project.id)
        d1_data = next(d for d in dirs if d["id"] == d1.id)
        assert d1_data["status"] == "superseded"

    def test_scope(self, project):
        d1 = ComposeDirective.create("global", "Target audience is executives")
        d1 = add_directive(project.id, d1)
        d2 = ComposeDirective.create("global", "Target audience is engineers")
        d2 = add_directive(project.id, d2)

        conflicts = detect_conflicts(project.id, d2)
        assert len(conflicts) == 1

        result = resolve_conflict(project.id, conflicts[0].id, "scope")
        assert result["resolved"] is True
        assert result["action"] == "scope"

        # Both directives should remain active (scope keeps both active)
        dirs = get_directives(project.id)
        d1_data = next(d for d in dirs if d["id"] == d1.id)
        d2_data = next(d for d in dirs if d["id"] == d2.id)
        assert d1_data["status"] == "active"
        assert d2_data["status"] == "active"

        # Conflict should be resolved
        pending = get_pending_conflicts(project.id)
        assert len(pending) == 0

    def test_keep_both(self, project):
        d1 = ComposeDirective.create("global", "Target audience is executives")
        d1 = add_directive(project.id, d1)
        d2 = ComposeDirective.create("global", "Target audience is engineers")
        d2 = add_directive(project.id, d2)

        conflicts = detect_conflicts(project.id, d2)
        result = resolve_conflict(project.id, conflicts[0].id, "keep_both")
        assert result["resolved"] is True

        # Both directives should still be active
        dirs = get_directives(project.id)
        active = [d for d in dirs if d["status"] == "active"]
        assert len(active) >= 2

    def test_resolve_nonexistent(self, project):
        result = resolve_conflict(project.id, "nonexistent-id", "supersede")
        assert result["resolved"] is False

    def test_resolve_already_resolved(self, project):
        d1 = ComposeDirective.create("global", "Target audience is executives")
        d1 = add_directive(project.id, d1)
        d2 = ComposeDirective.create("global", "Target audience is engineers")
        d2 = add_directive(project.id, d2)

        conflicts = detect_conflicts(project.id, d2)
        resolve_conflict(project.id, conflicts[0].id, "supersede")
        # Try to resolve again
        result = resolve_conflict(project.id, conflicts[0].id, "keep_both")
        assert result["resolved"] is False

    def test_resolution_logged_as_directive(self, project):
        d1 = ComposeDirective.create("global", "Target audience is executives")
        d1 = add_directive(project.id, d1)
        d2 = ComposeDirective.create("global", "Target audience is engineers")
        d2 = add_directive(project.id, d2)

        conflicts = detect_conflicts(project.id, d2)
        resolve_conflict(project.id, conflicts[0].id, "supersede")

        dirs = get_directives(project.id)
        resolution_dirs = [d for d in dirs if d.get("source") == "root"]
        assert len(resolution_dirs) >= 1
        assert "resolved" in resolution_dirs[0]["content"].lower()
