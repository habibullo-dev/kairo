"""REPL scheduler integration: task tools wiring, `tasks` commands, turn lock."""

from __future__ import annotations

import asyncio
import datetime as dt
import io
from pathlib import Path

import pytest
from rich.console import Console

from jarvis.cli.repl import Repl
from jarvis.config import SchedulerConfig, load_config
from jarvis.core import FakeClient, text_message
from jarvis.core.prompts import TASKS_GUIDANCE
from jarvis.persistence.db import connect
from jarvis.scheduler.service import TaskService
from jarvis.scheduler.store import TaskStore

START = dt.datetime(2026, 7, 6, 8, 0, tzinfo=dt.UTC)
_OPEN_DBS: list = []


@pytest.fixture(autouse=True)
async def _close_dbs():
    yield
    while _OPEN_DBS:
        await _OPEN_DBS.pop().close()


def _console() -> tuple[Console, io.StringIO]:
    buf = io.StringIO()
    # wide so task lines aren't wrapped mid-word (we assert on substrings)
    return Console(file=buf, force_terminal=False, width=200), buf


async def _service(tmp_path: Path) -> TaskService:
    store = TaskStore(await connect(tmp_path / "tasks.db"))
    _OPEN_DBS.append(store.db)
    return TaskService(store, SchedulerConfig(), now=lambda: START)


def _repl(
    tmp_path: Path, *, tasks=None, turn_lock=None, responses=None
) -> tuple[Repl, io.StringIO]:
    config = load_config(root=tmp_path, env_file=None)
    console, buf = _console()
    repl = Repl(
        config,
        client=FakeClient(responses or [text_message("done")]),
        console=console,
        tasks=tasks,
        turn_lock=turn_lock,
    )
    return repl, buf


# --- wiring ------------------------------------------------------------------


async def test_scheduler_enabled_registers_tools_and_time_context(tmp_path: Path) -> None:
    svc = await _service(tmp_path)
    repl, _ = _repl(tmp_path, tasks=svc)
    for name in ("schedule_task", "list_tasks", "cancel_task"):
        assert name in repl.registry
    assert repl.loop.add_time_context is True  # model needs the date to schedule
    assert TASKS_GUIDANCE in repl.loop.system


def test_scheduler_disabled_wires_nothing(tmp_path: Path) -> None:
    repl, _ = _repl(tmp_path, tasks=None)
    for name in ("schedule_task", "list_tasks", "cancel_task"):
        assert name not in repl.registry
    assert repl.loop.add_time_context is False
    assert TASKS_GUIDANCE not in repl.loop.system  # earlier-phase prompt unchanged


# --- `tasks` command ---------------------------------------------------------


async def test_tasks_command_lists_with_provenance(tmp_path: Path) -> None:
    svc = await _service(tmp_path)
    await svc.schedule(
        kind="reminder", title="stretch", payload="stand up", schedule_kind="cron",
        schedule_spec="0 * * * *", created_by="user", timezone="UTC",
    )
    svc.bound_session_id = None
    await svc.schedule(
        kind="job", title="digest", payload="summarize", schedule_kind="interval",
        schedule_spec="3600", created_by="agent", timezone="UTC",
    )
    repl, buf = _repl(tmp_path, tasks=svc)
    await repl._show_tasks("")
    out = buf.getvalue()
    assert "stretch" in out and "digest" in out
    assert "by user" in out and "by agent" in out  # provenance shown


async def test_tasks_command_shows_run_history(tmp_path: Path) -> None:
    svc = await _service(tmp_path)
    task = await svc.schedule(
        kind="job", title="nightly", payload="do it", schedule_kind="interval",
        schedule_spec="3600", created_by="user", timezone="UTC",
    )
    # simulate one completed run (session_id=None avoids the FK to sessions)
    svc.now = lambda: START + dt.timedelta(hours=1, minutes=1)
    (due,) = await svc.due()
    run_id = await svc.begin_run(due)
    await svc.complete_run(due, run_id, ok=True, result_text="the number is 42", cost_usd=0.03)

    repl, buf = _repl(tmp_path, tasks=svc)
    await repl._show_tasks(str(task.id))
    out = buf.getvalue()
    assert "nightly" in out
    assert "the number is 42" in out
    assert "$0.03" in out  # per-run cost visible


async def test_tasks_command_without_scheduler(tmp_path: Path) -> None:
    repl, buf = _repl(tmp_path, tasks=None)
    await repl._show_tasks("")
    assert "not enabled" in buf.getvalue()


async def test_tasks_command_last_error_surfaced(tmp_path: Path) -> None:
    svc = await _service(tmp_path)
    task = await svc.schedule(
        kind="job", title="flaky", payload="x", schedule_kind="interval",
        schedule_spec="3600", created_by="user", timezone="UTC",
    )
    svc.now = lambda: START + dt.timedelta(hours=1, minutes=1)
    (due,) = await svc.due()
    run_id = await svc.begin_run(due)
    await svc.complete_run(due, run_id, ok=False, error="backend down")
    repl, buf = _repl(tmp_path, tasks=svc)
    await repl._show_tasks("")
    assert "backend down" in buf.getvalue()  # surfaced on the active-list line
    assert task.id == 1


# --- turn lock ---------------------------------------------------------------


async def test_run_turn_waits_on_the_shared_turn_lock(tmp_path: Path) -> None:
    # A held turn lock (as a background run would hold it) blocks an interactive
    # turn until released — the model is not called meanwhile.
    lock = asyncio.Lock()
    repl, _ = _repl(tmp_path, turn_lock=lock, responses=[text_message("hi")])
    repl.messages.append({"role": "user", "content": "hello"})

    await lock.acquire()
    turn = asyncio.create_task(repl.run_turn())
    await asyncio.sleep(0.05)
    assert repl.client.calls == []  # blocked on the lock — no model call yet
    lock.release()
    await turn
    assert len(repl.client.calls) == 1  # ran once the lock freed
