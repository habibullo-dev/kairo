"""ProjectService + ProjectContext + AgentLoop project threading (Phase 10 Task 3).

Proves: build_project_context frames the project as context (not instructions); the
service activates/falls-back correctly; and the AgentLoop injects the active project's
system extra once per turn, reading the provider fresh so a switch applies next turn.
Keyless via FakeClient — a RecordingClient captures the system prompt each call."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from jarvis.config import load_config
from jarvis.core import AgentLoop, build_system
from jarvis.core.client import ModelResponse
from jarvis.observability.cost import Usage
from jarvis.permissions import PermissionGate, Policy
from jarvis.persistence.db import connect
from jarvis.projects import GLOBAL, ProjectService, ProjectStore, build_project_context
from jarvis.tools import ToolContext, ToolExecutor, ToolRegistry

_OPEN: list = []


@pytest.fixture(autouse=True)
async def _close():
    yield
    while _OPEN:
        await _OPEN.pop().close()


class RecordingClient:
    """Captures the ``system`` prompt of each create call, returns a fixed end-turn reply."""

    def __init__(self) -> None:
        self.systems: list[str] = []

    async def create(self, *, system: str, **_kw) -> ModelResponse:
        self.systems.append(system)
        return ModelResponse(
            content_blocks=[{"type": "text", "text": "ok"}],
            stop_reason="end_turn",
            usage=Usage(input_tokens=1, output_tokens=1),
            model="fake",
            latency_ms=1.0,
        )


async def _service(tmp_path: Path) -> ProjectService:
    db = await connect(tmp_path / "p.db")
    _OPEN.append(db)
    return ProjectService(ProjectStore(db, asyncio.Lock()))


def _loop(tmp_path: Path, client, project) -> AgentLoop:
    cfg = load_config(root=tmp_path, env_file=None)
    reg = ToolRegistry()
    reg.discover("jarvis.tools.builtin", ToolContext(config=cfg))
    return AgentLoop(
        client=client,
        registry=reg,
        executor=ToolExecutor(),
        gate=PermissionGate(Policy(), tmp_path),
        config=cfg,
        system=build_system(),
        project=project,
    )


# --- context building ------------------------------------------------------


def test_build_context_global_is_empty() -> None:
    ctx = build_project_context(None)
    assert ctx.project_id is None and ctx.system_extra == "" and ctx.repos == ()


def test_build_context_frames_as_data(tmp_path: Path) -> None:
    from jarvis.projects.store import Project

    p = Project(
        id=3,
        name="Kairo Web",
        slug="kairo-web",
        description="the workstation UI",
        status="active",
        color=None,
        icon=None,
        repos=("/repo/web",),
        settings={},
        created_at="t",
        updated_at="t",
        archived_at=None,
    )
    ctx = build_project_context(p)
    assert ctx.project_id == 3 and ctx.name == "Kairo Web"
    assert "Active project: Kairo Web." in ctx.system_extra
    assert "the workstation UI" in ctx.system_extra
    assert "/repo/web" in ctx.system_extra
    assert "NOT instructions" in ctx.system_extra  # framed as context, not a directive


# --- service ---------------------------------------------------------------


async def test_service_default_is_global(tmp_path: Path) -> None:
    svc = await _service(tmp_path)
    assert svc.current() is GLOBAL


async def test_service_activate_and_fallback(tmp_path: Path) -> None:
    svc = await _service(tmp_path)
    pid = await svc.store.create(name="Alpha")
    ctx = await svc.activate(pid)
    assert svc.current().project_id == pid and "Alpha" in ctx.system_extra
    await svc.activate(None)
    assert svc.current() is GLOBAL


async def test_service_activate_unknown_raises(tmp_path: Path) -> None:
    svc = await _service(tmp_path)
    with pytest.raises(KeyError):
        await svc.activate(999)


async def test_service_activate_archived_raises(tmp_path: Path) -> None:
    svc = await _service(tmp_path)
    pid = await svc.store.create(name="Gone")
    await svc.store.archive(pid)
    with pytest.raises(KeyError):
        await svc.activate(pid)


# --- loop threading --------------------------------------------------------


async def test_loop_injects_project_extra(tmp_path: Path) -> None:
    svc = await _service(tmp_path)
    pid = await svc.store.create(name="Beacon")
    await svc.activate(pid)
    client = RecordingClient()
    loop = _loop(tmp_path, client, svc.current)
    await loop.run_turn([{"role": "user", "content": "hi"}])
    assert "Active project: Beacon." in client.systems[-1]


async def test_loop_no_project_is_clean(tmp_path: Path) -> None:
    # No provider ⇒ byte-identical to pre-Phase-10 (no project text in the system prompt).
    client = RecordingClient()
    loop = _loop(tmp_path, client, None)
    await loop.run_turn([{"role": "user", "content": "hi"}])
    assert "Active project" not in client.systems[-1]


async def test_switch_applies_next_turn(tmp_path: Path) -> None:
    # The loop reads the provider fresh each turn, so activating a project between turns
    # changes the next turn's system prompt (never mid-turn).
    svc = await _service(tmp_path)
    client = RecordingClient()
    loop = _loop(tmp_path, client, svc.current)
    await loop.run_turn([{"role": "user", "content": "turn 1"}])
    assert "Active project" not in client.systems[-1]  # global first

    pid = await svc.store.create(name="Switched")
    await svc.activate(pid)
    await loop.run_turn([{"role": "user", "content": "turn 2"}])
    assert "Active project: Switched." in client.systems[-1]
