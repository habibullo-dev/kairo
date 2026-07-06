"""Phase 6, Task 1 — the small seams the multi-agent layer is built on.

None of these do anything user-visible yet; they are the load-bearing primitives
later tasks compose: a filtered registry view (a child's scoped toolset), a
per-tool executor-timeout override (so a long-running child isn't killed by the
60s tool deadline), and the two forwarding events (so nothing a child does is
hidden). Keyless, no network.
"""

from __future__ import annotations

import asyncio
import io

from pydantic import BaseModel
from rich.console import Console

from jarvis.cli.render import ConsoleRenderer
from jarvis.core.events import (
    Event,
    SubAgentCompleted,
    SubAgentEvent,
    ToolDecision,
    ToolStarted,
)
from jarvis.observability.cost import Usage
from jarvis.tools import (
    DEFAULT_TIMEOUT,
    Permission,
    ScopedRegistry,
    Tool,
    ToolExecutor,
    ToolRegistry,
)

# --- fixtures: a few trivial tools -------------------------------------------


class _EmptyParams(BaseModel):
    pass


class ReadTool(Tool):
    name = "read_file"
    description = "read"
    Params = _EmptyParams
    permission_default = Permission.ALLOW

    async def run(self, params: _EmptyParams) -> str:
        return "read-ok"


class WriteTool(Tool):
    name = "write_file"
    description = "write"
    Params = _EmptyParams

    async def run(self, params: _EmptyParams) -> str:
        return "write-ok"


class SpawnTool(Tool):
    name = "spawn_agent"
    description = "spawn"
    Params = _EmptyParams

    async def run(self, params: _EmptyParams) -> str:
        return "spawn-ok"


def _registry() -> ToolRegistry:
    reg = ToolRegistry()
    reg.register(ReadTool())
    reg.register(WriteTool())
    reg.register(SpawnTool())
    return reg


# --- ScopedRegistry ----------------------------------------------------------


def test_scoped_registry_hides_out_of_scope_tools() -> None:
    scoped = ScopedRegistry(_registry(), frozenset({"read_file"}))
    assert scoped.get("read_file") is not None
    # write_file and spawn_agent exist in the base registry but are out of scope:
    assert scoped.get("write_file") is None
    assert scoped.get("spawn_agent") is None


def test_scoped_registry_names_and_contains_and_len() -> None:
    scoped = ScopedRegistry(_registry(), frozenset({"read_file", "write_file"}))
    assert set(scoped.names()) == {"read_file", "write_file"}
    assert "read_file" in scoped
    assert "spawn_agent" not in scoped
    assert len(scoped) == 2


def test_scoped_registry_specs_are_the_subset_only() -> None:
    scoped = ScopedRegistry(_registry(), frozenset({"read_file"}))
    specs = scoped.specs()
    assert [s["name"] for s in specs] == ["read_file"]
    # a full spec shape, just filtered
    assert set(specs[0]) == {"name", "description", "input_schema"}


def test_scoped_registry_ignores_names_absent_from_base() -> None:
    # A scoped name with no backing tool is simply not exposed (the gate denies it
    # too — scope is enforced twice). It must not appear or blow up specs().
    scoped = ScopedRegistry(_registry(), frozenset({"read_file", "does_not_exist"}))
    assert "does_not_exist" not in scoped
    assert scoped.get("does_not_exist") is None
    assert [s["name"] for s in scoped.specs()] == ["read_file"]
    assert len(scoped) == 1


def test_scoped_registry_empty_scope() -> None:
    scoped = ScopedRegistry(_registry(), frozenset())
    assert scoped.names() == []
    assert scoped.specs() == []
    assert len(scoped) == 0


# --- executor timeout_override -----------------------------------------------


class DefaultTimeoutTool(Tool):
    name = "default_to"
    description = "uses the executor's timeout"
    Params = _EmptyParams

    async def run(self, params: _EmptyParams) -> str:
        await asyncio.sleep(0.05)
        return "done"


class FastCapTool(Tool):
    name = "fast_cap"
    description = "overrides with a tiny timeout"
    Params = _EmptyParams
    timeout_override = 0.01

    async def run(self, params: _EmptyParams) -> str:
        await asyncio.sleep(0.05)
        return "done"


class OwnsDeadlineTool(Tool):
    name = "owns_deadline"
    description = "no executor timeout — the tool owns its own deadline"
    Params = _EmptyParams
    timeout_override = None

    async def run(self, params: _EmptyParams) -> str:
        await asyncio.sleep(0.05)
        return "done"


def test_timeout_for_resolves_the_three_states() -> None:
    execu = ToolExecutor(timeout=7.0)
    assert DefaultTimeoutTool().timeout_override is DEFAULT_TIMEOUT
    assert execu._timeout_for(DefaultTimeoutTool()) == 7.0  # sentinel -> executor default
    assert execu._timeout_for(FastCapTool()) == 0.01  # float -> as-is
    assert execu._timeout_for(OwnsDeadlineTool()) is None  # None -> no executor timeout


async def test_default_sentinel_uses_executor_timeout() -> None:
    # No override: a tool sleeping 0.05s under a 0.01s executor times out.
    execu = ToolExecutor(timeout=0.01)
    result = await execu.execute(DefaultTimeoutTool(), {})
    assert result.is_error
    assert "timed out after 0.01s" in result.content


async def test_float_override_wins_over_executor_default() -> None:
    # Executor default is generous (10s) but the tool caps itself at 0.01s.
    execu = ToolExecutor(timeout=10.0)
    result = await execu.execute(FastCapTool(), {})
    assert result.is_error
    assert "timed out after 0.01s" in result.content


async def test_none_override_disables_the_executor_timeout() -> None:
    # The proof that matters for spawn_agent: with timeout_override=None, a tool that
    # sleeps *longer* than a tiny executor timeout still completes — wait_for is skipped.
    execu = ToolExecutor(timeout=0.01)
    result = await execu.execute(OwnsDeadlineTool(), {})
    assert not result.is_error
    assert result.content == "done"


# --- SubAgentEvent / SubAgentCompleted ---------------------------------------


def _renderer() -> ConsoleRenderer:
    return ConsoleRenderer(Console(file=io.StringIO(), force_terminal=False, width=100))


def test_subagent_events_are_part_of_the_event_union() -> None:
    inner = ToolDecision("read_file", {}, gate_decision="allow", resolution="allow")
    env = SubAgentEvent(agent_id="a1", title="research", inner=inner)
    done = SubAgentCompleted(
        agent_id="a1", title="research", status="ok", usage=Usage(input_tokens=3), cost_usd=0.01
    )
    assert isinstance(env, Event)
    assert isinstance(done, Event)
    assert env.inner is inner
    assert done.usage.input_tokens == 3


def test_renderer_no_ops_on_subagent_events_by_default() -> None:
    # Task 1 only requires the renderer not to crash on the new events (Task 6 adds
    # real child-activity rendering). The default event sink ignores them.
    renderer = _renderer()
    console_out = renderer.console.file
    renderer(SubAgentEvent("a1", "research", ToolStarted("t1", "read_file", {})))
    renderer(SubAgentCompleted("a1", "research", "ok", Usage(), None))
    assert console_out.getvalue() == ""  # nothing rendered, no exception
