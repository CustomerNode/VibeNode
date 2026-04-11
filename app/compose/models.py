"""
Compose data models — plain dataclasses that serialize to/from JSON.

Follows the same pattern as app/db/repository.py (Task, TaskSession, etc.):
dataclasses with to_dict() for JSON serialization, from_dict() classmethod
for deserialization.
"""

import json
import os
import shutil
import tempfile
import uuid
from dataclasses import dataclass, asdict, field
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Optional

from ..config import _VIBENODE_DIR


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

COMPOSE_PROJECTS_DIR = _VIBENODE_DIR / "compose-projects"

DEFAULT_CONTEXT = {
    "version": 1,
    "project_id": "",
    "project_name": "",
    "sections": [],
    "facts": {},
    "directives": [],
    "conflicts": [],
    "export_config": {
        "format": "docx",
        "template": None,
        "styles": {},
    },
    "status": {
        "total_sections": 0,
        "complete": 0,
        "in_progress": 0,
        "drafting": 0,
        "reviewing": 0,
    },
}


# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------

class SectionStatus(Enum):
    """Lifecycle states a compose section can occupy."""
    DRAFTING = "drafting"
    REVIEWING = "reviewing"
    COMPLETE = "complete"


class ConflictStatus(Enum):
    """Status of a directive conflict."""
    PENDING = "pending"
    RESOLVED = "resolved"


# ---------------------------------------------------------------------------
# Dataclasses
# ---------------------------------------------------------------------------

@dataclass
class ComposeProject:
    """A single composition project."""
    id: str
    name: str
    created_at: str
    root_session_id: Optional[str] = None
    shared_prompts_enabled: bool = True
    parent_project: Optional[str] = None  # VibeNode project path this belongs to
    position: int = 0                     # sidebar sort order (lower = higher)
    pinned: bool = False                  # show in sidebar regardless of active project

    def to_dict(self):
        return asdict(self)

    @classmethod
    def from_dict(cls, d):
        return cls(
            id=d["id"],
            name=d["name"],
            created_at=d["created_at"],
            root_session_id=d.get("root_session_id"),
            shared_prompts_enabled=d.get("shared_prompts_enabled", True),
            parent_project=d.get("parent_project"),
            position=d.get("position", 0),
            pinned=d.get("pinned", False),
        )

    @classmethod
    def create(cls, name, parent_project=None):
        """Factory: create a new project with generated id and timestamp."""
        return cls(
            id=str(uuid.uuid4()),
            name=name,
            created_at=datetime.now(timezone.utc).isoformat(),
            parent_project=parent_project,
        )


@dataclass
class ComposeSection:
    """A section within a composition (maps to a folder + session)."""
    id: str
    project_id: str
    parent_id: Optional[str]
    name: str
    status: SectionStatus
    order: int
    artifact_type: Optional[str] = None
    session_id: Optional[str] = None
    changing: bool = False
    change_note: Optional[str] = None
    changing_set_by: Optional[str] = None
    summary: Optional[str] = None

    def to_dict(self):
        d = asdict(self)
        d["status"] = self.status.value
        return d

    # Migration map for old status values
    _STATUS_MIGRATION = {"not_started": "drafting", "working": "drafting"}

    @classmethod
    def from_dict(cls, d):
        status = d.get("status", "drafting")
        if isinstance(status, str):
            status = cls._STATUS_MIGRATION.get(status, status)
            status = SectionStatus(status)
        return cls(
            id=d["id"],
            project_id=d["project_id"],
            parent_id=d.get("parent_id"),
            name=d["name"],
            status=status,
            order=d.get("order", 0),
            artifact_type=d.get("artifact_type"),
            session_id=d.get("session_id"),
            changing=d.get("changing", False),
            change_note=d.get("change_note"),
            changing_set_by=d.get("changing_set_by"),
            summary=d.get("summary"),
        )

    @classmethod
    def create(cls, project_id, name, parent_id=None, order=0, artifact_type=None):
        """Factory: create a new section with generated id."""
        return cls(
            id=str(uuid.uuid4()),
            project_id=project_id,
            parent_id=parent_id,
            name=name,
            status=SectionStatus.DRAFTING,
            order=order,
            artifact_type=artifact_type,
        )


@dataclass
class ComposeDirective:
    """A directive issued within a composition (user instruction or AI-generated)."""
    id: str
    gen: int
    scope: str  # "global", section_id, or "root"
    content: str
    source: str  # "user", "root", or section_id
    status: str  # "active", "superseded"
    created_at: str

    def to_dict(self):
        return asdict(self)

    @classmethod
    def from_dict(cls, d):
        return cls(
            id=d["id"],
            gen=d.get("gen", 0),
            scope=d.get("scope", "global"),
            content=d["content"],
            source=d.get("source", "user"),
            status=d.get("status", "active"),
            created_at=d.get("created_at", ""),
        )

    @classmethod
    def create(cls, scope, content, source="user", gen=0):
        return cls(
            id=str(uuid.uuid4()),
            gen=gen,
            scope=scope,
            content=content,
            source=source,
            status="active",
            created_at=datetime.now(timezone.utc).isoformat(),
        )


@dataclass
class ComposeFact:
    """A fact discovered during composition (stored in context)."""
    key: str
    value: str
    source_section: Optional[str] = None
    discovered_at: Optional[str] = None

    def to_dict(self):
        return asdict(self)

    @classmethod
    def from_dict(cls, d):
        return cls(
            key=d["key"],
            value=d["value"],
            source_section=d.get("source_section"),
            discovered_at=d.get("discovered_at"),
        )


@dataclass
class ComposeConflict:
    """A directive conflict requiring user resolution."""
    id: str
    project_id: str
    directive_a_id: str
    directive_b_id: str
    directive_a_content: str
    directive_b_content: str
    status: ConflictStatus
    recommendation: Optional[str] = None
    resolution: Optional[str] = None
    resolution_action: Optional[str] = None  # "supersede", "scope", "keep_both"
    resolved_at: Optional[str] = None

    def to_dict(self):
        d = asdict(self)
        d["status"] = self.status.value
        return d

    @classmethod
    def from_dict(cls, d):
        status = d.get("status", "pending")
        if isinstance(status, str):
            status = ConflictStatus(status)
        return cls(
            id=d["id"],
            project_id=d["project_id"],
            directive_a_id=d["directive_a_id"],
            directive_b_id=d["directive_b_id"],
            directive_a_content=d.get("directive_a_content", ""),
            directive_b_content=d.get("directive_b_content", ""),
            status=status,
            recommendation=d.get("recommendation"),
            resolution=d.get("resolution"),
            resolution_action=d.get("resolution_action"),
            resolved_at=d.get("resolved_at"),
        )

    @classmethod
    def create(cls, project_id, directive_a_id, directive_b_id,
               directive_a_content, directive_b_content, recommendation=None):
        return cls(
            id=str(uuid.uuid4()),
            project_id=project_id,
            directive_a_id=directive_a_id,
            directive_b_id=directive_b_id,
            directive_a_content=directive_a_content,
            directive_b_content=directive_b_content,
            status=ConflictStatus.PENDING,
            recommendation=recommendation,
        )


# ---------------------------------------------------------------------------
# Project folder scaffolding
# ---------------------------------------------------------------------------

def project_dir(project_id_or_name: str) -> Path:
    """Return the compose-projects directory for a given project.

    Tries to find by id first (scanning project.json files), then by name.
    """
    base = COMPOSE_PROJECTS_DIR
    if not base.is_dir():
        return base / _sanitize_folder_name(project_id_or_name)

    # Scan for matching project.json
    for d in base.iterdir():
        if not d.is_dir():
            continue
        pf = d / "project.json"
        if pf.is_file():
            try:
                data = json.loads(pf.read_text(encoding="utf-8"))
                if data.get("id") == project_id_or_name:
                    return d
            except Exception:
                pass

    # Fall back to name-based lookup
    return base / _sanitize_folder_name(project_id_or_name)


def _sanitize_folder_name(name: str) -> str:
    """Convert a project/section name to a safe folder name."""
    safe = name.strip().lower().replace(" ", "-")
    # Remove characters that are problematic in paths
    safe = "".join(c for c in safe if c.isalnum() or c in ("-", "_"))
    return safe or "unnamed"


def scaffold_project(project: ComposeProject) -> Path:
    """Create the folder structure for a new compose project.

    compose-projects/{name}/
        project.json          -- serialized ComposeProject
        compose-context.json  -- initial context
        brief.md              -- project brief (empty)
        sections/             -- section folders go here
        export/               -- final export output

    Returns the project directory path.
    """
    base = COMPOSE_PROJECTS_DIR
    base.mkdir(parents=True, exist_ok=True)

    folder_name = _sanitize_folder_name(project.name)
    pdir = base / folder_name

    # Handle name collision: append short id suffix
    if pdir.exists():
        pdir = base / f"{folder_name}-{project.id[:8]}"

    pdir.mkdir(parents=True, exist_ok=True)
    (pdir / "sections").mkdir(exist_ok=True)
    (pdir / "export").mkdir(exist_ok=True)

    # Write project.json
    (pdir / "project.json").write_text(
        json.dumps(project.to_dict(), indent=2, ensure_ascii=False),
        encoding="utf-8",
    )

    # Write initial compose-context.json
    ctx = dict(DEFAULT_CONTEXT)
    ctx["project_id"] = project.id
    ctx["project_name"] = project.name
    (pdir / "compose-context.json").write_text(
        json.dumps(ctx, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )

    # Write empty brief
    (pdir / "brief.md").write_text(
        f"# {project.name}\n\nProject brief goes here.\n",
        encoding="utf-8",
    )

    return pdir


def scaffold_section(project_id: str, section: ComposeSection) -> Path:
    """Create the folder for a new section under its project.

    compose-projects/{project}/sections/{section-name}/
        section.json   -- serialized ComposeSection
        content/       -- working files for this section

    Returns the section directory path.
    """
    pdir = project_dir(project_id)
    sections_dir = pdir / "sections"
    sections_dir.mkdir(parents=True, exist_ok=True)

    folder_name = _sanitize_folder_name(section.name)
    sdir = sections_dir / folder_name

    # Handle name collision
    if sdir.exists():
        sdir = sections_dir / f"{folder_name}-{section.id[:8]}"

    sdir.mkdir(parents=True, exist_ok=True)
    (sdir / "content").mkdir(exist_ok=True)

    # Write section.json
    (sdir / "section.json").write_text(
        json.dumps(section.to_dict(), indent=2, ensure_ascii=False),
        encoding="utf-8",
    )

    return sdir


def delete_project_folder(project_id: str) -> bool:
    """Delete the entire project folder. Returns True if deleted."""
    pdir = project_dir(project_id)
    if pdir.is_dir():
        shutil.rmtree(pdir, ignore_errors=True)
        return True
    return False


def clone_project(source_id: str, new_name: str) -> Optional[ComposeProject]:
    """Deep-copy a composition project with new IDs.

    Copies the entire folder, then rewrites project.json and all
    section.json files with fresh UUIDs.
    """
    src_dir = project_dir(source_id)
    if not src_dir.is_dir():
        return None

    source = get_project(source_id)
    if not source:
        return None

    # Create new project object
    new_project = ComposeProject.create(new_name, parent_project=source.parent_project)
    new_project.shared_prompts_enabled = source.shared_prompts_enabled
    new_project.position = source.position
    new_project.pinned = False  # clones should not inherit pinned state
    new_project.root_session_id = None  # cloned project has no running session

    # Copy folder
    base = COMPOSE_PROJECTS_DIR
    folder_name = _sanitize_folder_name(new_name)
    dest_dir = base / f"{folder_name}-{new_project.id[:8]}"
    shutil.copytree(src_dir, dest_dir)

    # Rewrite project.json with new ID
    (dest_dir / "project.json").write_text(
        json.dumps(new_project.to_dict(), indent=2, ensure_ascii=False),
        encoding="utf-8",
    )

    # Rewrite section IDs: two-pass to handle parent_id forward references
    section_id_map = {}  # old_id -> new_id
    section_files = []   # (path, parsed_data)
    sections_dir = dest_dir / "sections"
    if sections_dir.is_dir():
        # Pass 1: read all sections and assign new IDs
        for sdir in sections_dir.iterdir():
            sf = sdir / "section.json"
            if sf.is_file():
                try:
                    sdata = json.loads(sf.read_text(encoding="utf-8"))
                    old_id = sdata.get("id", "")
                    new_id = str(uuid.uuid4())
                    section_id_map[old_id] = new_id
                    section_files.append((sf, sdata, new_id))
                except Exception:
                    pass
        # Pass 2: rewrite with remapped IDs
        for sf, sdata, new_id in section_files:
            try:
                sdata["id"] = new_id
                sdata["project_id"] = new_project.id
                old_parent = sdata.get("parent_id")
                if old_parent and old_parent in section_id_map:
                    sdata["parent_id"] = section_id_map[old_parent]
                sdata["session_id"] = None  # cloned sections have no running session
                sf.write_text(
                    json.dumps(sdata, indent=2, ensure_ascii=False),
                    encoding="utf-8",
                )
            except Exception:
                pass

    # Update compose-context.json with new project ID and remapped section IDs
    ctx_file = dest_dir / "compose-context.json"
    if ctx_file.is_file():
        try:
            ctx = json.loads(ctx_file.read_text(encoding="utf-8"))
            ctx["project_id"] = new_project.id
            ctx["project_name"] = new_name
            # Remap section IDs and project_ids in the context sections array
            for sec in ctx.get("sections", []):
                old_sid = sec.get("id", "")
                if old_sid in section_id_map:
                    sec["id"] = section_id_map[old_sid]
                sec["project_id"] = new_project.id
                # Remap parent_id references
                old_parent = sec.get("parent_id")
                if old_parent and old_parent in section_id_map:
                    sec["parent_id"] = section_id_map[old_parent]
                # Clear session_id — cloned sections have no running sessions
                sec["session_id"] = None
            # Remap directive scope/source fields that reference section IDs
            directive_id_map = {}
            for d in ctx.get("directives", []):
                old_did = d.get("id", "")
                new_did = str(uuid.uuid4())
                directive_id_map[old_did] = new_did
                d["id"] = new_did
                if d.get("scope") in section_id_map:
                    d["scope"] = section_id_map[d["scope"]]
                if d.get("source") in section_id_map:
                    d["source"] = section_id_map[d["source"]]
            # Remap conflict references
            for c in ctx.get("conflicts", []):
                c["id"] = str(uuid.uuid4())
                c["project_id"] = new_project.id
                if c.get("directive_a_id") in directive_id_map:
                    c["directive_a_id"] = directive_id_map[c["directive_a_id"]]
                if c.get("directive_b_id") in directive_id_map:
                    c["directive_b_id"] = directive_id_map[c["directive_b_id"]]
            ctx_file.write_text(
                json.dumps(ctx, indent=2, ensure_ascii=False),
                encoding="utf-8",
            )
        except Exception:
            pass

    return new_project


def delete_section_folder(project_id: str, section_name: str) -> bool:
    """Delete a section folder. Returns True if deleted."""
    pdir = project_dir(project_id)
    sdir = pdir / "sections" / _sanitize_folder_name(section_name)
    if sdir.is_dir():
        shutil.rmtree(sdir, ignore_errors=True)
        return True
    return False


def list_projects() -> list:
    """Scan compose-projects/ and return all ComposeProject objects."""
    base = COMPOSE_PROJECTS_DIR
    projects = []
    if not base.is_dir():
        return projects

    for d in sorted(base.iterdir()):
        if not d.is_dir():
            continue
        pf = d / "project.json"
        if pf.is_file():
            try:
                data = json.loads(pf.read_text(encoding="utf-8"))
                projects.append(ComposeProject.from_dict(data))
            except Exception:
                pass
        else:
            # Legacy/orphan folder: no project.json but has compose-context.json.
            # Reconstruct a ComposeProject from context and persist project.json
            # so it is found normally on subsequent scans.
            ctx_file = d / "compose-context.json"
            if ctx_file.is_file():
                try:
                    ctx = json.loads(ctx_file.read_text(encoding="utf-8"))
                    proj_id = ctx.get("project_id") or d.name
                    proj_name = ctx.get("project_name") or d.name
                    from ..config import get_active_project
                    proj = ComposeProject(
                        id=proj_id,
                        name=proj_name,
                        created_at=datetime.now(timezone.utc).isoformat(),
                        parent_project=get_active_project() or None,
                    )
                    # Persist so this only happens once
                    pf.write_text(
                        json.dumps(proj.to_dict(), indent=2, ensure_ascii=False),
                        encoding="utf-8",
                    )
                    projects.append(proj)
                except Exception:
                    pass
    # Sort by position (lower first), then by created_at as tiebreaker
    projects.sort(key=lambda p: (p.position, p.created_at))
    return projects


def get_project(project_id: str) -> Optional[ComposeProject]:
    """Load a single project by id."""
    pdir = project_dir(project_id)
    pf = pdir / "project.json"
    if pf.is_file():
        try:
            data = json.loads(pf.read_text(encoding="utf-8"))
            return ComposeProject.from_dict(data)
        except Exception:
            pass
    return None


def save_project(project: ComposeProject) -> None:
    """Persist project metadata to project.json (atomic write)."""
    pdir = project_dir(project.id)
    pf = pdir / "project.json"
    if pf.parent.is_dir():
        fd, tmp_path = tempfile.mkstemp(
            dir=str(pdir), suffix=".tmp", prefix=".proj-"
        )
        try:
            with os.fdopen(fd, 'w', encoding='utf-8') as f:
                json.dump(project.to_dict(), f, indent=2, ensure_ascii=False)
            os.replace(tmp_path, str(pf))
        except Exception:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
            raise


def get_sections(project_id: str) -> list:
    """Load all sections for a project from compose-context.json."""
    pdir = project_dir(project_id)
    ctx_file = pdir / "compose-context.json"
    if not ctx_file.is_file():
        return []
    try:
        ctx = json.loads(ctx_file.read_text(encoding="utf-8"))
        return [ComposeSection.from_dict(s) for s in ctx.get("sections", [])]
    except Exception:
        return []


def get_section(project_id: str, section_id: str) -> Optional[ComposeSection]:
    """Load a single section by id."""
    for s in get_sections(project_id):
        if s.id == section_id:
            return s
    return None
