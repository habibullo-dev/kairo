"""The ``spawn_agent`` tool: the model-facing surface of delegation (Phase 6).

Like ``schedule_task``, ``spawn_agent`` is an expanded-authority sink and is never
"always"-able (see the REPL's ``_persist_always``): approving it opens a scoped
execution channel — a child runs with real tools — so the human must see the full
prompt and tool scope *every* time. It defaults to ASK, carries
``timeout_override = None`` (the :class:`~jarvis.agents.service.SubAgentService` owns
the child's deadline, so the executor's 60s tool timeout must not kill a legitimately
long research child), and registers only when a ``SubAgentService`` is present in the
context.

The tool itself is thin — validation (a non-empty subset of :data:`SPAWNABLE`) and a
handoff to the service, which owns the run, the double gate, and the framed report.
"""

from __future__ import annotations

from pydantic import BaseModel, Field, field_validator

from jarvis.agents.service import SPAWNABLE
from jarvis.tools.base import Permission, Tool, ToolContext, ToolResult


class _NeedsAgents:
    """Mixin: register only when the context carries a SubAgentService (delegation on).

    A plain mixin (not a ``Tool`` subclass) so it doesn't trip
    ``Tool.__init_subclass__``'s required-attribute check at import time."""

    @classmethod
    def is_available(cls, context: ToolContext) -> bool:
        return getattr(context, "agents", None) is not None


class SpawnAgentParams(BaseModel):
    title: str = Field(
        description="A short label for the sub-agent's task (shown at the approval "
        "prompt and in the audit log)."
    )
    prompt: str = Field(
        description="The full, self-contained task for the sub-agent. It has NO access "
        "to this conversation and cannot ask questions, so include everything it needs. "
        "Its final message is its report back to you."
    )
    tools: list[str] = Field(
        description="The tools the sub-agent may use — a subset of "
        f"{sorted(SPAWNABLE)}. Grant the least it needs for the task."
    )

    @field_validator("tools")
    @classmethod
    def _validate_tools(cls, value: list[str]) -> list[str]:
        if not value:
            raise ValueError("provide at least one tool for the sub-agent")
        illegal = sorted(set(value) - SPAWNABLE)
        if illegal:
            raise ValueError(
                f"these tools can't be delegated: {illegal}; choose from {sorted(SPAWNABLE)}"
            )
        return value


class SpawnAgentTool(_NeedsAgents, Tool):
    name = "spawn_agent"
    description = (
        "Delegate a scoped subtask to a sub-agent: it runs with an isolated context and "
        "only the tools you grant, then returns a report. Use it to parallelize "
        "independent research or to contain noisy exploration. The user approves each "
        "spawn. The sub-agent can't ask questions, can't delegate further, and can't "
        "schedule tasks or write memory — write its prompt to be self-contained."
    )
    Params = SpawnAgentParams
    permission_default = Permission.ASK  # scoped-execution sink; never silent, never "always"
    timeout_override = None  # the SubAgentService enforces sub_agents.timeout_seconds itself

    async def run(self, params: SpawnAgentParams) -> ToolResult | str:
        agents = self.context.agents
        if agents is None:
            return ToolResult(content="Delegation is not enabled.", is_error=True)
        return await agents.spawn(title=params.title, prompt=params.prompt, tools=params.tools)
