"""Telegram remote-control boundary: private allowlist, no replay, bounded model use."""

from __future__ import annotations

import asyncio
import datetime as dt
from dataclasses import dataclass, field
from pathlib import Path

import httpx

from jarvis.config import TelegramRemoteControlConfig
from jarvis.persistence.db import connect
from jarvis.remote.telegram import (
    TelegramRemoteControl,
    TelegramRemoteControlStore,
    parse_telegram_update,
)


def _update(
    update_id: int, *, chat_id: int = 123, chat_type: str = "private", text: str = "hi"
) -> dict:
    return {
        "update_id": update_id,
        "message": {"chat": {"id": chat_id, "type": chat_type}, "text": text},
    }


@dataclass
class _TelegramHttp:
    batches: list[list[object]]
    requests: list[tuple[str, dict[str, str]]] = field(default_factory=list)
    sent: list[dict[str, str]] = field(default_factory=list)

    async def post(self, url: str, data: dict[str, str]) -> httpx.Response:
        self.requests.append((url, data))
        if url.endswith("/getUpdates"):
            batch = self.batches.pop(0) if self.batches else []
            return httpx.Response(200, json={"ok": True, "result": batch})
        assert url.endswith("/sendMessage")
        self.sent.append(data)
        return httpx.Response(200, json={"ok": True, "result": {}})


async def _controller(
    tmp_path: Path,
    *,
    batches: list[list[object]],
    max_per_hour: int = 20,
    max_input_chars: int = 2_000,
) -> tuple[TelegramRemoteControl, _TelegramHttp, dict[str, list[str]], object]:
    db = await connect(tmp_path / "remote.db")
    calls: dict[str, list[str]] = {"status": [], "tasks": [], "chat": []}

    async def status() -> str:
        calls["status"].append("called")
        return "STATUS"

    async def tasks() -> str:
        calls["tasks"].append("called")
        return "TASKS"

    async def chat(text: str) -> str:
        calls["chat"].append(text)
        return f"reply: {text}"

    http = _TelegramHttp(batches)
    controller = TelegramRemoteControl(
        bot_token="BOT-CANARY",
        config=TelegramRemoteControlConfig(
            enabled=True,
            allowed_chat_id="123",
            max_model_messages_per_hour=max_per_hour,
            max_input_chars=max_input_chars,
        ),
        store=TelegramRemoteControlStore(db, asyncio.Lock()),
        status_handler=status,
        tasks_handler=tasks,
        chat_handler=chat,
        http=http,
    )
    return controller, http, calls, db


def test_parse_update_requires_text_message_shape() -> None:
    assert parse_telegram_update({"update_id": 1, "message": {}}) is None
    assert parse_telegram_update({"update_id": -1, "message": {}}) is None
    assert parse_telegram_update(
        {"update_id": 1, "message": {"chat": {"id": 3, "type": "private"}}}
    ) is None
    parsed = parse_telegram_update(_update(1, chat_type="group", text="hello"))
    assert parsed is not None and parsed.chat_type == "group"  # authorization is controller-owned


async def test_first_poll_discards_retained_updates_then_handles_fresh_private_owner_message(
    tmp_path: Path,
) -> None:
    controller, http, calls, db = await _controller(
        tmp_path,
        batches=[[_update(5, text="old request")], [_update(6, text="/status")]],
    )
    try:
        assert await controller.poll_once() == 0  # first enable never replays retained bot traffic
        assert calls == {"status": [], "tasks": [], "chat": []}
        assert http.sent == []

        assert await controller.poll_once() == 1
        assert calls["status"] == ["called"]
        assert http.sent == [
            {
                "chat_id": "123",
                "text": "STATUS",
                "disable_web_page_preview": True,
            }
        ]
        poll_url, form = http.requests[1]
        assert "/botBOT-CANARY/getUpdates" in poll_url
        assert form["offset"] == "6" and form["allowed_updates"] == '["message"]'
    finally:
        await db.close()


async def test_initialize_consumes_backlog_before_the_channel_is_announced_ready(
    tmp_path: Path,
) -> None:
    controller, http, calls, db = await _controller(
        tmp_path,
        batches=[[_update(5, text="old request")], [_update(6, text="/status")]],
    )
    try:
        await controller.initialize()
        assert calls == {"status": [], "tasks": [], "chat": []}
        assert http.sent == []
        assert await controller.poll_once() == 1
        assert calls["status"] == ["called"]
        assert http.requests[1][1]["timeout"] == "25"
        assert http.requests[0][1]["timeout"] == "0"
    finally:
        await db.close()


async def test_unknown_and_group_chats_are_ignored_without_a_reply(tmp_path: Path) -> None:
    controller, http, calls, db = await _controller(
        tmp_path,
        batches=[[], [_update(1, chat_id=999), _update(2, chat_type="group", text="/status")]],
    )
    try:
        await controller.poll_once()  # initialize cursor
        assert await controller.poll_once() == 0
        assert calls == {"status": [], "tasks": [], "chat": []}
        assert http.sent == []
    finally:
        await db.close()


async def test_nontext_update_advances_cursor_instead_of_spinning(tmp_path: Path) -> None:
    media_only = {"update_id": 5, "message": {"chat": {"id": 123, "type": "private"}}}
    controller, http, calls, db = await _controller(tmp_path, batches=[[], [media_only], []])
    try:
        await controller.poll_once()  # initialize cursor
        assert await controller.poll_once() == 0
        assert await controller.poll_once() == 0
        assert calls == {"status": [], "tasks": [], "chat": []}
        # The third request starts after the ignored media update, so Telegram will not replay it.
        assert http.requests[2][1]["offset"] == "6"
    finally:
        await db.close()


async def test_claimed_update_is_not_replayed_after_a_duplicate_delivery(tmp_path: Path) -> None:
    controller, http, calls, db = await _controller(
        tmp_path,
        batches=[[], [_update(10, text="/tasks")], [_update(10, text="/tasks")]],
    )
    try:
        await controller.poll_once()
        assert await controller.poll_once() == 1
        assert await controller.poll_once() == 0
        assert calls["tasks"] == ["called"]
        assert [message["text"] for message in http.sent] == ["TASKS"]
    finally:
        await db.close()


async def test_model_chat_is_limited_and_long_input_never_reaches_handler(tmp_path: Path) -> None:
    controller, http, calls, db = await _controller(
        tmp_path,
        max_per_hour=1,
        max_input_chars=8,
        batches=[
            [],
            [_update(1, text="first")],
            [_update(2, text="second")],
            [_update(3, text="this is too long")],
        ],
    )
    try:
        await controller.poll_once()
        await controller.poll_once()
        await controller.poll_once()
        await controller.poll_once()
        assert calls["chat"] == ["first"]
        assert [message["text"] for message in http.sent] == [
            "reply: first",
            "Remote model chat has reached its hourly limit. /status and /tasks still work.",
            "That message is too long for remote chat (8 characters max). "
            "Please shorten it and resend.",
        ]
    finally:
        await db.close()


async def test_rate_reservation_resets_after_one_hour(tmp_path: Path) -> None:
    db = await connect(tmp_path / "remote.db")
    try:
        store = TelegramRemoteControlStore(db, asyncio.Lock())
        start = dt.datetime(2026, 1, 1, tzinfo=dt.UTC)
        assert await store.reserve_model_message(max_per_hour=1, now=start)
        assert not await store.reserve_model_message(
            max_per_hour=1, now=start + dt.timedelta(minutes=59)
        )
        assert await store.reserve_model_message(max_per_hour=1, now=start + dt.timedelta(hours=1))
    finally:
        await db.close()
