"""Composition test: Telegram remote chat has a separate, stateless zero-tool loop."""

from __future__ import annotations

import asyncio
import io
import re
from pathlib import Path
from types import SimpleNamespace

from pydantic import BaseModel
from rich.console import Console
from tests.unit.test_telegram_remote import _TelegramHttp, _update

from jarvis.cli.repl import _build_telegram_remote_control
from jarvis.config import load_config
from jarvis.core import FakeClient, ToolCall, text_message, tool_use_message
from jarvis.observability.cost import load_pricing
from jarvis.persistence import SessionStore
from jarvis.persistence.db import connect
from jarvis.projects.service import ProjectService
from jarvis.projects.store import ProjectStore
from jarvis.scheduler.service import TaskService
from jarvis.scheduler.store import TaskStore
from jarvis.tools import Tool, ToolRegistry
from jarvis.tools.executor import ToolExecutor


class _Runner:
    def __init__(self) -> None:
        self.task_notify = None
        self.kicks = 0
        self.in_flight = False

    def kick(self) -> None:
        self.kicks += 1

    async def resume_parked(self, _run_id: int, _action: str) -> bool:
        return True


class _WebSearchParams(BaseModel):
    query: str
    max_results: int


class _WebSearch(Tool):
    name = "web_search"
    description = "Test public web search."
    Params = _WebSearchParams

    def __init__(self) -> None:
        super().__init__()
        self.calls: list[_WebSearchParams] = []

    async def run(self, params: _WebSearchParams) -> str:
        self.calls.append(params)
        return "Public result: Seoul is 28 C with light rain today."


async def test_remote_wiring_uses_utility_model_and_exposes_no_tools_or_history(
    tmp_path: Path,
) -> None:
    config = load_config(root=tmp_path, env_file=None)
    config.connectors.telegram.remote_control.enabled = True
    config.connectors.telegram.remote_control.allowed_chat_id = "123"
    config.secrets = config.secrets.model_copy(update={"telegram_bot_token": "BOT-CANARY"})
    db = await connect(tmp_path / "remote.db")
    try:
        fake = FakeClient([text_message("safe remote reply")])
        store = SessionStore(db)
        repl = SimpleNamespace(
            tasks=None,
            turn_lock=asyncio.Lock(),
            registry=ToolRegistry(),
            client=fake,
            executor=ToolExecutor(
                timeout=config.limits.tool_timeout_seconds,
                max_result_chars=config.limits.max_tool_result_chars,
            ),
            gate=object(),
            cost_ledger=SimpleNamespace(pricing=load_pricing(Path("config/pricing.yaml"))),
            connectors=None,
        )
        controller = _build_telegram_remote_control(
            config,
            repl=repl,
            store=store,
            runner=None,
            console=Console(file=io.StringIO()),
        )
        assert controller is not None
        http = _TelegramHttp([[], [_update(1, text="what should I focus on?")]])
        await controller.poll_once(http=http)  # bootstrap only
        assert await controller.poll_once(http=http) == 1

        assert len(fake.calls) == 1
        call = fake.calls[0]
        assert call["model"] == config.models.utility
        assert call["tools"] == []
        assert call["messages"] == [{"role": "user", "content": "what should I focus on?"}]
        assert call["max_tokens"] == 1_200
        assert "no direct execution authority" in call["system"]
        assert http.sent[0]["text"] == "safe remote reply"
    finally:
        await db.close()


async def test_remote_operator_exposes_only_proposal_then_host_approval_queues_job(
    tmp_path: Path,
) -> None:
    config = load_config(root=tmp_path, env_file=None)
    config.connectors.telegram.remote_control.enabled = True
    config.connectors.telegram.remote_control.allowed_chat_id = "123"
    config.connectors.telegram.remote_control.operator.enabled = True
    config.secrets = config.secrets.model_copy(update={"telegram_bot_token": "BOT-CANARY"})
    db = await connect(tmp_path / "operator.db")
    controller = None
    try:
        lock = asyncio.Lock()
        session_store = SessionStore(db, lock)
        project_store = ProjectStore(db, lock)
        project_id = await project_store.create(name="Jarvis", repos=[str(tmp_path)])
        task_store = TaskStore(db, lock)
        task_service = TaskService(task_store, config.scheduler)
        fake = FakeClient(
            [
                tool_use_message(
                    [
                        ToolCall(
                            id="proposal-1",
                            name="remote_propose_work",
                            input={
                                "kind": "job",
                                "title": "Repair frontend wiring",
                                "instruction": "Inspect and repair the frontend API wiring.",
                                "project": "jarvis",
                                "status_interval_minutes": 15,
                            },
                        )
                    ]
                )
            ]
        )
        runner = _Runner()
        repl = SimpleNamespace(
            tasks=task_service,
            projects=ProjectService(project_store),
            turn_lock=asyncio.Lock(),
            registry=ToolRegistry(),
            client=fake,
            executor=ToolExecutor(
                timeout=config.limits.tool_timeout_seconds,
                max_result_chars=config.limits.max_tool_result_chars,
            ),
            gate=object(),
            cost_ledger=SimpleNamespace(pricing=load_pricing(Path("config/pricing.yaml"))),
            connectors=None,
        )
        controller = _build_telegram_remote_control(
            config,
            repl=repl,
            store=session_store,
            runner=runner,  # type: ignore[arg-type]
            console=Console(file=io.StringIO()),
        )
        assert controller is not None
        http = _TelegramHttp(
            [[], [_update(1, text="Kairo, repair the frontend wiring in Jarvis")]]
        )
        await controller.poll_once(http=http)
        assert await controller.poll_once(http=http) == 1

        assert len(fake.calls) == 1
        exposed = [tool["name"] for tool in fake.calls[0]["tools"]]
        assert exposed == ["remote_propose_work"]
        preview = http.sent[-1]["text"]
        assert "Remote proposal #" in preview
        assert "Approve: /approve" in preview
        assert await task_store.list() == []

        match = re.search(r"/approve ([0-9A-F]{12})", preview)
        assert match is not None
        http.batches.append([_update(2, text=f"/approve {match.group(1)}")])
        assert await controller.poll_once(http=http) == 1

        (task,) = await task_store.list()
        assert task.origin == "remote_operator"
        assert task.project_id == project_id
        assert runner.kicks == 1
        assert "Approved and queued" in http.sent[-1]["text"]
    finally:
        if controller is not None:
            await controller.stop()
        await db.close()


async def test_remote_live_question_uses_one_public_search_then_answers(
    tmp_path: Path,
) -> None:
    config = load_config(root=tmp_path, env_file=None)
    config.connectors.telegram.remote_control.enabled = True
    config.connectors.telegram.remote_control.allowed_chat_id = "123"
    config.connectors.telegram.remote_control.operator.enabled = True
    config.connectors.telegram.remote_control.operator.live_web_search_enabled = True
    config.connectors.telegram.remote_control.operator.live_web_search_max_results = 3
    config.connectors.telegram.remote_control.operator.default_live_location = (
        "Seoul, South Korea"
    )
    config.secrets = config.secrets.model_copy(
        update={"telegram_bot_token": "BOT-CANARY", "tavily_api_key": "TVLY-CANARY"}
    )
    db = await connect(tmp_path / "live-search.db")
    controller = None
    try:
        source = _WebSearch()
        registry = ToolRegistry()
        registry.register(source)
        fake = FakeClient(
            [
                tool_use_message(
                    [
                        ToolCall(
                            id="weather-1",
                            name="remote_live_search",
                            input={"query": "weather Seoul South Korea today"},
                        )
                    ]
                ),
                text_message("Seoul is about 28 C with light rain today."),
            ]
        )
        repl = SimpleNamespace(
            tasks=None,
            projects=None,
            turn_lock=asyncio.Lock(),
            registry=registry,
            client=fake,
            executor=ToolExecutor(
                timeout=config.limits.tool_timeout_seconds,
                max_result_chars=config.limits.max_tool_result_chars,
            ),
            gate=object(),
            cost_ledger=SimpleNamespace(pricing=load_pricing(Path("config/pricing.yaml"))),
            connectors=None,
        )
        controller = _build_telegram_remote_control(
            config,
            repl=repl,
            store=SessionStore(db),
            runner=None,
            console=Console(file=io.StringIO()),
        )
        assert controller is not None
        http = _TelegramHttp([[], [_update(1, text="What's today's weather?")]])
        await controller.poll_once(http=http)
        assert await controller.poll_once(http=http) == 1

        assert len(fake.calls) == 2
        assert [tool["name"] for tool in fake.calls[0]["tools"]] == [
            "remote_live_search"
        ]
        assert "Seoul, South Korea" in fake.calls[0]["system"]
        assert source.calls[0].max_results == 3
        assert http.sent[-1]["text"] == "Seoul is about 28 C with light rain today."
    finally:
        if controller is not None:
            await controller.stop()
        await db.close()
