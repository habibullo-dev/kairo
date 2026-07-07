"""Reporting: read-only views over local state for the Daily screen (Phase 9).

Currently just :class:`RepoReader` — a hardened, read-only git reader. Kept out of the UI
package because it's pure local reporting with no web/auth concern.
"""

from jarvis.reporting.repo import CommitLine, RepoReader, RepoState

__all__ = ["CommitLine", "RepoReader", "RepoState"]
