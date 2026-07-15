"""Composition tests for Telegram's isolated, ephemeral remote model loop."""

from __future__ import annotations

import asyncio
import io
import re
from pathlib import Path
from types import SimpleNamespace

from PIL import Image
from pydantic import BaseModel
from rich.console import Console
from tests.unit.test_telegram_remote import _TelegramHttp, _update

from kira.cli.repl import _build_telegram_remote_control, _start_runtime_services
from kira.config import load_config
from kira.connectors.consent import lock_all_integrations
from kira.core import FakeClient, ToolCall, text_message, tool_use_message
from kira.observability.cost import load_pricing
from kira.persistence import SessionStore
from kira.persistence.db import connect
from kira.projects.service import ProjectService
from kira.projects.store import ProjectStore
from kira.scheduler.service import TaskService
from kira.scheduler.store import TaskStore
from kira.tools import Tool, ToolRegistry
from kira.tools.executor import ToolExecutor


class _Runner:
    def __init__(self) -> None:
        self.task_notify = None
        self.kicks = 0
        self.in_flight: str | None = None

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


async def test_reset_consent_lock_keeps_telegram_remote_control_off(tmp_path: Path) -> None:
    config = load_config(root=tmp_path, env_file=None)
    config.connectors.telegram.remote_control.enabled = True
    config.secrets = config.secrets.model_copy(update={"telegram_bot_token": "BOT-CANARY"})
    lock_all_integrations(config.data_dir)
    output = io.StringIO()

    controller = _build_telegram_remote_control(
        config,
        repl=SimpleNamespace(),
        store=SimpleNamespace(),
        runner=None,
        console=Console(file=output, force_terminal=False),
    )

    assert controller is None
    assert "locked after the data reset" in output.getvalue()


async def test_runtime_reconciles_operator_before_scheduler_catchup_and_polling() -> None:
    events: list[str] = []

    class Tasks:
        async def sweep_stale_runs(self) -> list[str]:
            events.append("sweep-stale-runs")
            return []

    class Runner:
        async def check_due(self) -> int:
            events.append("scheduler-catchup")
            return 0

        def start(self) -> None:
            events.append("runner-start")

    class RemoteControl:
        async def initialize(self) -> None:
            events.append("telegram-cursor")

        async def start_operator(self) -> None:
            events.append("operator-reconcile")

        def start(self) -> None:
            events.append("telegram-polling")

    await _start_runtime_services(  # type: ignore[arg-type]
        tasks=Tasks(),
        runner=Runner(),
        remote_control=RemoteControl(),
        console=Console(file=io.StringIO(), force_terminal=False),
    )

    assert events == [
        "telegram-cursor",
        "operator-reconcile",
        "sweep-stale-runs",
        "scheduler-catchup",
        "runner-start",
        "telegram-polling",
    ]


def _test_png() -> bytes:
    output = io.BytesIO()
    Image.new("RGB", (64, 32), "green").save(output, format="PNG")
    return output.getvalue()


async def test_news_pdf_request_routes_to_host_approval_before_search_or_model(
    tmp_path: Path,
) -> None:
    config = load_config(root=tmp_path, env_file=None)
    config.connectors.telegram.remote_control.enabled = True
    config.connectors.telegram.remote_control.allowed_chat_id = "123"
    config.connectors.telegram.remote_control.operator.enabled = True
    config.connectors.telegram.remote_control.operator.live_web_search_enabled = True
    config.connectors.telegram.remote_control.operator.default_live_location = (
        "Seoul, South Korea"
    )
    config.secrets = config.secrets.model_copy(
        update={"telegram_bot_token": "BOT-CANARY", "tavily_api_key": "TVLY-CANARY"}
    )
    db = await connect(tmp_path / "news-routing.db")
    controller = None
    try:
        source = _WebSearch()
        registry = ToolRegistry()
        registry.register(source)
        fake = FakeClient([])
        repl = SimpleNamespace(
            tasks=None,
            projects=None,
            artifacts=None,
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
        http = _TelegramHttp(
            [
                [],
                [
                    _update(
                        1,
                        text="get latest news for today and send me one nice pdf of those",
                    )
                ],
            ]
        )
        await controller.poll_once(http=http)
        assert await controller.poll_once(http=http) == 1

        assert fake.calls == []
        assert source.calls == []
        preview = http.sent[-1]["text"]
        assert "News brief N1" in preview
        assert re.search(r"Approve: /approve N-[0-9A-F]{12}", preview)
        assert "Nothing is searched, created, or sent before approval." in preview
    finally:
        if controller is not None:
            await controller.stop()
        await db.close()


async def test_remote_wiring_uses_utility_model_and_exposes_no_tools_on_first_turn(
    tmp_path: Path,
) -> None:
    config = load_config(root=tmp_path, env_file=None)
    config.connectors.telegram.remote_control.enabled = True
    config.connectors.telegram.remote_control.allowed_chat_id = "123"
    config.secrets = config.secrets.model_copy(update={"telegram_bot_token": "BOT-CANARY"})
    db = await connect(tmp_path / "remote.db")
    try:
        fake = FakeClient([text_message("**Safe** remote reply")])
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
        assert call["max_tokens"] == 500
        assert "replying through Kira's narrow Telegram remote-control channel" in call["system"]
        assert "owner clearly asks Kira to" in call["system"]
        assert "checked Kira's live state" in call["system"]
        assert "Kairo" not in call["system"]
        assert "no direct execution authority" in call["system"]
        assert "ideally under 280 characters" in call["system"]
        assert "work in this same Telegram chat" in call["system"]
        assert http.sent[0]["text"] == "Safe remote reply"
    finally:
        await db.close()


async def test_remote_wiring_passes_recent_delivered_turns_with_correct_roles(
    tmp_path: Path,
) -> None:
    config = load_config(root=tmp_path, env_file=None)
    config.connectors.telegram.remote_control.enabled = True
    config.connectors.telegram.remote_control.allowed_chat_id = "123"
    config.secrets = config.secrets.model_copy(update={"telegram_bot_token": "BOT-CANARY"})
    db = await connect(tmp_path / "remote-followup.db")
    controller = None
    try:
        fake = FakeClient(
            [
                text_message("Marty Supreme is a sports drama starring Timothée Chalamet."),
                text_message("Yes—if you enjoy ambitious, high-energy character dramas."),
            ]
        )
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
            store=SessionStore(db),
            runner=None,
            console=Console(file=io.StringIO()),
        )
        assert controller is not None
        first = "Can u tell me about Marty Supreme movie recently came out"
        followup = "Is it a good movie?"
        http = _TelegramHttp(
            [[], [_update(1, text=first)], [_update(2, text=followup)]]
        )
        await controller.poll_once(http=http)
        assert await controller.poll_once(http=http) == 1
        assert await controller.poll_once(http=http) == 1

        assert len(fake.calls) == 2
        assert fake.calls[0]["messages"] == [{"role": "user", "content": first}]
        assert fake.calls[1]["messages"] == [
            {"role": "user", "content": first},
            {
                "role": "assistant",
                "content": "Marty Supreme is a sports drama starring Timothée Chalamet.",
            },
            {"role": "user", "content": followup},
        ]
        assert http.sent[-1]["text"].startswith("Yes—if you enjoy")
    finally:
        if controller is not None:
            await controller.stop()
        await db.close()


async def test_remote_photo_question_has_no_egress_even_when_live_search_is_enabled(
    tmp_path: Path,
) -> None:
    config = load_config(root=tmp_path, env_file=None)
    config.connectors.telegram.remote_control.enabled = True
    config.connectors.telegram.remote_control.allowed_chat_id = "123"
    config.connectors.telegram.remote_control.attachments.enabled = True
    config.connectors.telegram.remote_control.operator.enabled = True
    config.connectors.telegram.remote_control.operator.live_web_search_enabled = True
    config.secrets = config.secrets.model_copy(
        update={"telegram_bot_token": "BOT-CANARY", "tavily_api_key": "TVLY-CANARY"}
    )
    db = await connect(tmp_path / "photo.db")
    controller = None
    try:
        source = _WebSearch()
        registry = ToolRegistry()
        registry.register(source)
        fake = FakeClient([text_message("The image shows a green rectangle.")])
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
        raw = _test_png()
        update = {
            "update_id": 1,
            "message": {
                "chat": {"id": 123, "type": "private"},
                "caption": "What is in this image?",
                "photo": [
                    {
                        "file_id": "photo-1",
                        "file_size": len(raw),
                        "width": 64,
                        "height": 32,
                    }
                ],
            },
        }
        http = _TelegramHttp([[], [update]], files={"photo-1": raw})
        await controller.poll_once(http=http)
        assert await controller.poll_once(http=http) == 1

        assert len(fake.calls) == 1 and fake.calls[0]["tools"] == []
        content = fake.calls[0]["messages"][0]["content"]
        assert isinstance(content, list) and content[0]["type"] == "image"
        assert "What is in this image?" in content[1]["text"]
        assert source.calls == []
        assert http.sent[-1]["text"] == "The image shows a green rectangle."
    finally:
        if controller is not None:
            await controller.stop()
        await db.close()


async def test_host_state_commands_use_canonical_kira_copy(tmp_path: Path) -> None:
    config = load_config(root=tmp_path, env_file=None)
    config.connectors.telegram.remote_control.enabled = True
    config.connectors.telegram.remote_control.allowed_chat_id = "123"
    config.connectors.telegram.remote_control.operator.enabled = True
    config.secrets = config.secrets.model_copy(update={"telegram_bot_token": "BOT-CANARY"})
    db = await connect(tmp_path / "live-status.db")
    controller = None
    try:
        lock = asyncio.Lock()
        session_store = SessionStore(db, lock)
        project_store = ProjectStore(db, lock)
        await project_store.create(name="Kira", repos=[str(tmp_path)])
        projects = ProjectService(project_store)
        runner = _Runner()
        fake = FakeClient([])
        repl = SimpleNamespace(
            tasks=None,
            projects=projects,
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
            [
                [],
                [
                    _update(1, text="Is Kira working on any projects now?"),
                    _update(2, text="/tasks"),
                    _update(3, text="/briefing"),
                ],
            ]
        )
        await controller.poll_once(http=http)
        assert await controller.poll_once(http=http) == 3

        assert fake.calls == []
        status_text, tasks_text, briefing_text = [message["text"] for message in http.sent]
        assert status_text == (
            "No—Kira is online, but no project work is running right now.\n"
            "Scheduled tasks: 0. Registered projects: 1.\n"
            "Remote Operator: read-only."
        )
        assert tasks_text == "The scheduler is off on this Kira instance."
        assert briefing_text.startswith("Kira briefing\n\nNo—Kira is online")
        assert "Kairo" not in "\n".join((status_text, tasks_text, briefing_text))

        runner.in_flight = "Repair frontend wiring"
        http.batches.append([_update(4, text="/status")])
        assert await controller.poll_once(http=http) == 1
        assert http.sent[-1]["text"] == (
            'Yes—Kira is working now: background job "Repair frontend wiring".\n'
            "Scheduled tasks: 0. Registered projects: 1.\n"
            "Remote Operator: read-only."
        )
    finally:
        if controller is not None:
            await controller.stop()
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
        project_id = await project_store.create(name="Kira", repos=[str(tmp_path)])
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
                                "project": "kira",
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
            [[], [_update(1, text="Kira, repair the frontend wiring in Kira")]]
        )
        await controller.poll_once(http=http)
        assert await controller.poll_once(http=http) == 1

        assert len(fake.calls) == 1
        exposed = [tool["name"] for tool in fake.calls[0]["tools"]]
        assert exposed == ["remote_propose_work"]
        preview = http.sent[-1]["text"]
        assert "Remote proposal #" in preview
        assert "Project: kira" in preview
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
