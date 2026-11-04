"""
Compose mode business logic — data models, context management, conflict
detection, prompt building, and the root orchestrator lifecycle.
"""

from .models import (
    ComposeProject,
    ComposeSection,
    ComposeConflict,
    ComposeDirective,
    ComposeFact,
    SectionStatus,
    ConflictStatus,
)
