"""spawn() orchestration extension + ServiceLedger + migration v8 (Phase 10B Task 10).

Pins: the default spawn path (spawn_agent tool: title/prompt/tools only) is byte-identical to
Phase 6 and the tool schema exposes NO routing params; the host path threads role/team/stage/
run/project into agent_runs + the cost ledger; a per-role client/model override lands in the
child config; fresh_trace bypasses the per-turn spawn cap; service_calls records metadata-only
(unpriced ⇒ NULL). Keyless via FakeClient + tmp SQLite."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from kira.agents import AgentRunStore, SubAgentService
from kira.config import load_config
from kira.core import FakeClient, text_message
from kira.observability.cost import Usage
from kira.observability.ledger import CostContext, ServiceLedger, cost_context
from kira.permissions import PermissionGate, Policy
from kira.persistence import SessionStore
from kira.persistence.db import connect
from kira.projects import ProjectStore
from kira.tools import ToolContext, ToolExecutor, ToolRegistry

_OPEN: list = []


@pytest.fixture(autouse=True)
async def _close():
    yield
    while _OPEN:
        await _OPEN.pop().close()


async def _service(tmp_path: Path, *, responses: list) -> tuple[SubAgentService, AgentRunStore]:
    db = await connect(tmp_path / "s.db")
    _OPEN.append(db)
    lock = asyncio.Lock()
    await ProjectStore(db, lock).create(name="Proj")  # id 1
    store = SessionStore(db, lock)
    run_store = AgentRunStore(db, lock)
    cfg = load_config(root=tmp_path, env_file=None)
    svc = SubAgentService(
        session_store=store,
        run_store=run_store,
        client=FakeClient(responses),
        executor=ToolExecutor(),
        gate=PermissionGate(Policy(), tmp_path),
        config=cfg,
    )
    reg = ToolRegistry()
    reg.discover("kira.tools.builtin", ToolContext(config=cfg))
    svc.bind(registry=reg)
    return svc, run_store


async def test_default_spawn_records_no_orchestration_fields(tmp_path: Path) -> None:
    svc, run_store = await _service(tmp_path, responses=[text_message("done")])
    out = await svc.spawn(title="read", prompt="do it", tools=["read_file"])
    assert not out.is_error
    runs = await run_store.list(limit=5)
    # role/stage/orchestration_run_id/project_id are the v7 columns; a plain spawn leaves them
    # NULL (queried directly since the read model doesn't expose them yet).
    cur = await run_store.db.execute(
        "SELECT role, stage, orchestration_run_id, project_id FROM agent_runs WHERE id = ?",
        (runs[0].id,),
    )
    assert await cur.fetchone() == (None, None, None, None)


async def test_spawn_agent_tool_exposes_no_routing_params(tmp_path: Path) -> None:
    # The model-facing tool must not be able to choose model/role/client/run — routing stays
    # config-only. The tool's input schema has only title/prompt/tools.
    svc, _rs = await _service(tmp_path, responses=[])
    from kira.tools.builtin.agents import SpawnAgentParams

    fields = set(SpawnAgentParams.model_fields)
    assert fields == {"title", "prompt", "tools"}


async def test_host_spawn_threads_attribution(tmp_path: Path) -> None:
    svc, run_store = await _service(
        tmp_path, responses=[text_message("ok", usage=Usage(input_tokens=10, output_tokens=2))]
    )
    await svc.spawn(
        title="council-read",
        prompt="review",
        tools=["read_file"],
        role="security",
        team="security",
        stage="council",
        orchestration_run_id=None,  # no run row seeded here; agent_runs FK allows NULL
        project_id=1,
        fresh_trace=True,
    )
    cur = await run_store.db.execute(
        "SELECT role, stage, project_id FROM agent_runs ORDER BY id DESC LIMIT 1"
    )
    assert await cur.fetchone() == ("security", "council", 1)


async def test_per_role_model_override_lands_in_child_config(tmp_path: Path) -> None:
    # A per-role model override changes the child loop's models.main (via _child_config).
    svc, _rs = await _service(tmp_path, responses=[])
    cfg = svc._child_config("claude-fable-5")
    assert cfg.models.main == "claude-fable-5"
    assert svc._child_config().models.main == svc.config.models.main  # default unchanged


async def test_fresh_trace_bypasses_spawn_cap(tmp_path: Path) -> None:
    # The per-turn cap would block many spawns in one trace; fresh_trace resets it each call so
    # host orchestration (own bounds) isn't throttled.
    svc, _rs = await _service(tmp_path, responses=[text_message("a")] * 20)
    svc.config.sub_agents.max_spawn_calls_per_turn = 2
    ok = 0
    for _ in range(5):
        out = await svc.spawn(title="t", prompt="p", tools=["read_file"], fresh_trace=True)
        if not out.is_error:
            ok += 1
    assert ok == 5  # never hit the cap


# --- ServiceLedger ----------------------------------------------------------


async def test_service_ledger_records_metadata(tmp_path: Path) -> None:
    db = await connect(tmp_path / "l.db")
    _OPEN.append(db)
    lock = asyncio.Lock()
    await ProjectStore(db, lock).create(name="P")  # id 1
    ledger = ServiceLedger(db, lock, pricing_version="1")
    ctx = CostContext(project_id=1, team="security", agent_role="scanner", stage="council")
    token = cost_context.set(ctx)
    try:
        await ledger.record(service="semgrep", operation="scan", units=None, est_cost_usd=0.0)
        await ledger.record(service="firecrawl", operation="crawl", units=3, est_cost_usd=None)
    finally:
        cost_context.reset(token)
    cur = await db.execute(
        "SELECT service, team, agent_role, stage, est_cost_usd, project_id FROM service_calls "
        "ORDER BY id"
    )
    rows = await cur.fetchall()
    assert rows[0] == ("semgrep", "security", "scanner", "council", 0.0, 1)  # known free = 0.0
    assert rows[1][0] == "firecrawl" and rows[1][4] is None  # unpriced = NULL, never 0.0
    # metadata only — no body/secret columns exist
    cur = await db.execute("PRAGMA table_info(service_calls)")
    cols = {r[1] for r in await cur.fetchall()}
    assert not (cols & {"body", "content", "secret", "output", "prompt"})


async def test_service_ledger_degrades_visibly(tmp_path: Path) -> None:
    db = await connect(tmp_path / "l.db")
    lock = asyncio.Lock()
    ledger = ServiceLedger(db, lock)
    await db.close()  # force writes to fail
    await ledger.record(service="semgrep")
    assert ledger.status() == {"degraded": True, "unrecorded": 1}
