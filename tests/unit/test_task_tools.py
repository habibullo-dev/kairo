"""Task tool tests: roundtrip, availability gating, permission defaults, approval.

FakeClient + FakeEmbedder-free; connections closed by the autouse fixture."""

from __future__ import annotations

import datetime as dt
from pathlib import Path

import pytest
from pydantic import ValidationError

from jarvis.config import SchedulerConfig
from jarvis.persistence.db import connect
from jarvis.scheduler.service import TaskService
from jarvis.scheduler.store import TaskStore
from jarvis.tools import Permission, ToolContext, ToolRegistry, ToolResult
from jarvis.tools.builtin.tasks import CancelTaskTool, ListTasksTool, ScheduleTaskTool

TASK_TOOLS = ("schedule_task", "list_tasks", "cancel_task")
START = dt.datetime(2026, 7, 6, 8, 0, tzinfo=dt.UTC)

_OPEN_DBS: list = []


@pytest.fixture(autouse=True)
async def _close_dbs():
    yield
    while _OPEN_DBS:
        await _OPEN_DBS.pop().close()


def _content(r: object) -> str:
    return r.content if isinstance(r, ToolResult) else str(r)


def _is_error(r: object) -> bool:
    return isinstance(r, ToolResult) and r.is_error


async def _ctx(tmp_path: Path) -> tuple[ToolContext, TaskService]:
    store = TaskStore(await connect(tmp_path / "tasks.db"))
    _OPEN_DBS.append(store.db)
    svc = TaskService(store, SchedulerConfig(), now=lambda: START)
    return ToolContext(tasks=svc), svc


# --- roundtrip ---------------------------------------------------------------


async def test_schedule_list_cancel_roundtrip(tmp_path: Path) -> None:
    ctx, svc = await _ctx(tmp_path)

    out = _content(
        await ScheduleTaskTool(ctx).run(
            ScheduleTaskTool.Params(
                kind="reminder", title="stretch", payload="stand up and stretch", cron="0 * * * *"
            )
        )
    )
    assert "Scheduled" in out and "stretch" in out

    listed = _content(await ListTasksTool(ctx).run(ListTasksTool.Params()))
    assert "stretch" in listed and "reminder #1" in listed

    cancelled = _content(await CancelTaskTool(ctx).run(CancelTaskTool.Params(task_id=1)))
    assert "Cancelled task #1" in cancelled
    # cancelled task is gone from the active list but kept in history
    assert "No tasks." in _content(await ListTasksTool(ctx).run(ListTasksTool.Params()))
    assert (await svc.store.get(1)).status == "cancelled"


async def test_schedule_job_with_interval(tmp_path: Path) -> None:
    ctx, _ = await _ctx(tmp_path)
    out = _content(
        await ScheduleTaskTool(ctx).run(
            ScheduleTaskTool.Params(
                kind="job", title="digest", payload="summarize the inbox", every_seconds=3600
            )
        )
    )
    assert "Scheduled" in out and "every 1 hour" in out


async def test_schedule_past_once_is_error_with_current_time(tmp_path: Path) -> None:
    # Absolute past instant so it's "in the past" regardless of the runner's local
    # zone (the tool uses local tz). The exact-time rendering is pinned in the
    # service test, which fixes tz=UTC; here we assert the model-facing shape.
    ctx, _ = await _ctx(tmp_path)
    r = await ScheduleTaskTool(ctx).run(
        ScheduleTaskTool.Params(
            kind="reminder", title="late", payload="x", once_at="2020-01-01T00:00:00+00:00"
        )
    )
    assert _is_error(r)
    assert "in the past" in _content(r)
    assert "2026" in _content(r)  # the injected clock is shown, so the model can self-correct
    assert "future time" in _content(r)


async def test_cancel_unknown_is_error(tmp_path: Path) -> None:
    ctx, _ = await _ctx(tmp_path)
    r = await CancelTaskTool(ctx).run(CancelTaskTool.Params(task_id=999))
    assert _is_error(r)


# --- schedule-field validation ----------------------------------------------


def test_exactly_one_schedule_field_required() -> None:
    with pytest.raises(ValidationError):  # none given
        ScheduleTaskTool.Params(kind="job", title="t", payload="p")
    with pytest.raises(ValidationError):  # two given
        ScheduleTaskTool.Params(
            kind="job", title="t", payload="p", cron="0 9 * * *", every_seconds=60
        )


# --- availability gating -----------------------------------------------------


def test_task_tools_unavailable_without_service() -> None:
    empty = ToolContext()
    for tool in (ScheduleTaskTool, ListTasksTool, CancelTaskTool):
        assert tool.is_available(empty) is False


def test_registry_skips_task_tools_without_service() -> None:
    reg = ToolRegistry()
    reg.discover("jarvis.tools.builtin", ToolContext(config=None, tasks=None))
    for name in TASK_TOOLS:
        assert name not in reg
    assert "read_file" in reg  # earlier-phase tools still register


async def test_registry_registers_task_tools_with_service(tmp_path: Path) -> None:
    ctx, _ = await _ctx(tmp_path)
    reg = ToolRegistry()
    reg.discover("jarvis.tools.builtin", ctx)
    for name in TASK_TOOLS:
        assert name in reg


# --- permission defaults + null-path system prompt ---------------------------


def test_task_tool_permission_defaults() -> None:
    # schedule_task ASKS (deferred-execution injection sink); list is read-only.
    assert ScheduleTaskTool.permission_default is Permission.ASK
    assert ListTasksTool.permission_default is Permission.ALLOW
    assert CancelTaskTool.permission_default is Permission.ASK


def test_policy_defaults_schedule_task_asks() -> None:
    # The shipped policy must gate schedule_task (belt-and-suspenders with the
    # tool default) — a live-loaded policy is what actually runs.
    from jarvis.permissions import load_policy

    policy = load_policy(Path("config/permissions.yaml"))
    assert policy.tools["schedule_task"] is Permission.ASK
    assert policy.tools["cancel_task"] is Permission.ASK
    assert policy.tools["list_tasks"] is Permission.ALLOW


def test_system_prompt_gains_tasks_guidance_only_when_enabled() -> None:
    from jarvis.core.prompts import TASKS_GUIDANCE, build_system

    assert TASKS_GUIDANCE not in build_system()  # earlier-phase prompt unchanged
    assert TASKS_GUIDANCE in build_system(tasks_enabled=True)


# --- approval summary + never-persist ----------------------------------------


def test_call_summary_shows_full_payload_and_fire_time() -> None:
    from jarvis.cli.repl import _call_summary
    from jarvis.core import ToolCall

    long_payload = "do the thing; " + "x" * 900 + " END"
    summary = _call_summary(
        ToolCall(
            id="c1",
            name="schedule_task",
            input={
                "kind": "job",
                "title": "nightly",
                "payload": long_payload,
                "cron": "0 3 * * *",
            },
        )
    )
    assert long_payload in summary  # FULL payload, not truncated
    assert "END" in summary  # the tail survives (injection hidden at char 900 visible)
    assert "cron 0 3 * * *" in summary
    assert "first fire" in summary  # computed fire time shown


def test_schedule_task_is_never_always_allowed(tmp_path: Path) -> None:
    # A single "always" keystroke must not persist an allow for a deferred-execution
    # sink. _persist_always is a no-op for schedule_task/cancel_task.
    import io

    from rich.console import Console

    from jarvis.cli.repl import Repl
    from jarvis.config import load_config
    from jarvis.core import FakeClient, ToolCall, text_message

    config = load_config(root=tmp_path, env_file=None)
    console = Console(file=io.StringIO(), force_terminal=False, width=100)
    repl = Repl(config, client=FakeClient([text_message("ok")]), console=console)
    repl._persist_always(ToolCall(id="c1", name="schedule_task", input={"kind": "job"}))
    repl._persist_always(ToolCall(id="c2", name="cancel_task", input={"task_id": 1}))
    assert "schedule_task" not in repl.gate.policy.tools
    assert "cancel_task" not in repl.gate.policy.tools
