"""Project workspaces (Phase 10): the first-class unit of work.

A project owns chats, memories, tasks, KB sources, repos, and settings. NULL
``project_id`` on any scoped row means *global* scope (all pre-Phase-10 data). This
package is persistence + service only; the permission/taint substrate is untouched.
"""

from __future__ import annotations

from jarvis.projects.context import GLOBAL, ProjectContext, build_project_context
from jarvis.projects.service import ProjectService
from jarvis.projects.snapshot import (
    MAX_SNAPSHOT_SOURCES,
    ProjectSnapshot,
    SnapshotError,
    SnapshotSource,
    seal_snapshot,
)
from jarvis.projects.store import (
    Project,
    ProjectReset,
    ProjectResetBusyError,
    ProjectResetError,
    ProjectStore,
    slugify,
)

__all__ = [
    "GLOBAL",
    "Project",
    "ProjectContext",
    "ProjectReset",
    "ProjectResetBusyError",
    "ProjectResetError",
    "ProjectService",
    "ProjectSnapshot",
    "ProjectStore",
    "SnapshotError",
    "SnapshotSource",
    "build_project_context",
    "MAX_SNAPSHOT_SOURCES",
    "seal_snapshot",
    "slugify",
]
