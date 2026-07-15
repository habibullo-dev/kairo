"""Team Tool Intelligence (Phase 10B): the service catalog + fail-closed registry.

A *service* is a tool/API/MCP a team member may use (Semgrep, Firecrawl, Playwright, …).
Every candidate is a code-constant :class:`ServiceSpec` in :mod:`.catalog` carrying its full
matrix classification (host / credentials / pricing / sensitivity / egress / write / dangerous
/ stages / context_policy / output_trust / priority). The :class:`ServiceRegistry` derives
availability from the spec + feature flags + credential presence + pricing presence, and fails
CLOSED — a service that is disabled, deferred, unpriced, or missing credentials does not become
available (its tool never registers; the UI shows *why*). Listing a service in the catalog does
NOT enable it. Adapters (Task 16) are separate and behind the same flags.
"""

from __future__ import annotations

from kira.services.catalog import (
    SERVICE_CATALOG,
    ContextPolicy,
    OutputTrust,
    ServiceSpec,
    get_spec,
)
from kira.services.registry import ServiceRegistry, ServiceState

__all__ = [
    "SERVICE_CATALOG",
    "ContextPolicy",
    "OutputTrust",
    "ServiceRegistry",
    "ServiceSpec",
    "ServiceState",
    "get_spec",
]
