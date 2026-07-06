"""Multi-agent delegation (Phase 6): scoped, visible, doubly-gated sub-agents.

The parent agent delegates to sub-agents — each one scoped ``AgentLoop`` turn with an
isolated context and a per-spawn tool allowlist. Depth 1 only; every spawn is
human-approved; nothing a child does is hidden. See docs/PLAN-6-multi-agent.md and
ADR-0006. ``SubAgentService`` (the runner) lands in Task 4; this package currently
exports the ``agent_runs`` audit store.
"""

from jarvis.agents.service import SPAWNABLE, ApproverFactory, SubAgentService
from jarvis.agents.store import AgentRun, AgentRunStore

__all__ = [
    "SPAWNABLE",
    "AgentRun",
    "AgentRunStore",
    "ApproverFactory",
    "SubAgentService",
]
