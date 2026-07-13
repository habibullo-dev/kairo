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
    compact_remote_model_reply,
    natural_inbox_filter,
    natural_remote_read_command,
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
    files: dict[str, bytes] = field(default_factory=dict)
    requests: list[tuple[str, dict[str, str]]] = field(default_factory=list)
    sent: list[dict[str, str]] = field(default_factory=list)
    downloaded: list[str] = field(default_factory=list)

    async def post(self, url: str, data: dict[str, str]) -> httpx.Response:
        self.requests.append((url, data))
        if url.endswith("/getUpdates"):
            batch = self.batches.pop(0) if self.batches else []
            return httpx.Response(200, json={"ok": True, "result": batch})
        if url.endswith("/getFile"):
            file_id = data["file_id"]
            if file_id not in self.files:
                return httpx.Response(404, json={"ok": False})
            return httpx.Response(
                200, json={"ok": True, "result": {"file_path": f"attachments/{file_id}"}}
            )
        assert url.endswith("/sendMessage")
        self.sent.append(data)
        return httpx.Response(200, json={"ok": True, "result": {}})

    async def get(self, url: str) -> httpx.Response:
        file_id = url.rsplit("/", 1)[-1]
        self.downloaded.append(file_id)
        if file_id not in self.files:
            return httpx.Response(404)
        return httpx.Response(200, content=self.files[file_id])


async def _controller(
    tmp_path: Path,
    *,
    batches: list[list[object]],
    max_per_hour: int = 20,
    max_read_per_hour: int = 60,
    max_input_chars: int = 2_000,
) -> tuple[TelegramRemoteControl, _TelegramHttp, dict[str, list[str]], object]:
    db = await connect(tmp_path / "remote.db")
    calls: dict[str, list[str]] = {
        "status": [],
        "tasks": [],
        "inbox": [],
        "calendar": [],
        "briefing": [],
        "chat": [],
    }

    async def status() -> str:
        calls["status"].append("called")
        return "STATUS"

    async def tasks() -> str:
        calls["tasks"].append("called")
        return "TASKS"

    async def inbox(filter_terms: str) -> str:
        calls["inbox"].append(filter_terms)
        return "INBOX"

    async def calendar() -> str:
        calls["calendar"].append("called")
        return "CALENDAR"

    async def briefing() -> str:
        calls["briefing"].append("called")
        return "BRIEFING"

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
            max_read_requests_per_hour=max_read_per_hour,
            max_input_chars=max_input_chars,
        ),
        store=TelegramRemoteControlStore(db, asyncio.Lock()),
        status_handler=status,
        tasks_handler=tasks,
        inbox_handler=inbox,
        calendar_handler=calendar,
        briefing_handler=briefing,
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


def test_parse_update_accepts_photo_document_and_voice_without_text() -> None:
    photo = parse_telegram_update(
        {
            "update_id": 2,
            "message": {
                "chat": {"id": 123, "type": "private"},
                "caption": "What is wrong here?",
                "photo": [
                    {"file_id": "small", "file_size": 10, "width": 10, "height": 10},
                    {"file_id": "large", "file_size": 20, "width": 20, "height": 20},
                ],
            },
        }
    )
    assert photo is not None and photo.text == "What is wrong here?"
    assert photo.attachment is not None and photo.attachment.kind == "image"
    assert photo.attachment.file_id == "large"

    document = parse_telegram_update(
        {
            "update_id": 3,
            "message": {
                "chat": {"id": 123, "type": "private"},
                "document": {
                    "file_id": "doc",
                    "file_name": "report.pdf",
                    "mime_type": "application/pdf",
                    "file_size": 123,
                },
            },
        }
    )
    assert document is not None and document.attachment is not None
    assert document.attachment.kind == "document"

    voice = parse_telegram_update(
        {
            "update_id": 4,
            "message": {
                "chat": {"id": 123, "type": "private"},
                "voice": {
                    "file_id": "voice",
                    "mime_type": "audio/ogg",
                    "file_size": 456,
                    "duration": 12,
                },
            },
        }
    )
    assert voice is not None and voice.attachment is not None
    assert voice.attachment.kind == "voice" and voice.attachment.duration_seconds == 12


async def test_allowlisted_attachment_downloads_once_and_reaches_handler(tmp_path: Path) -> None:
    db = await connect(tmp_path / "attachment.db")
    seen: list[tuple[str, bytes, str]] = []

    async def fixed() -> str:
        return "fixed"

    async def chat(_text: str) -> str:
        raise AssertionError("attachment reached text chat")

    async def attachment(kind, raw: bytes, caption: str) -> str:
        seen.append((kind.kind, raw, caption))
        return "IMAGE ANSWER"

    update = {
        "update_id": 1,
        "message": {
            "chat": {"id": 123, "type": "private"},
            "caption": "Explain this",
            "photo": [{"file_id": "photo-1", "file_size": 4, "width": 10, "height": 10}],
        },
    }
    http = _TelegramHttp([[update]], files={"photo-1": b"JPEG"})
    store = TelegramRemoteControlStore(db, asyncio.Lock())
    await store.bootstrap(0)
    controller = TelegramRemoteControl(
        bot_token="BOT-CANARY",
        config=TelegramRemoteControlConfig(
            enabled=True,
            allowed_chat_id="123",
            attachments={"enabled": True},
        ),
        store=store,
        status_handler=fixed,
        tasks_handler=fixed,
        inbox_handler=lambda _query: fixed(),
        calendar_handler=fixed,
        briefing_handler=fixed,
        chat_handler=chat,
        attachment_handler=attachment,
        http=http,
    )
    try:
        assert await controller.poll_once() == 1
        assert seen == [("image", b"JPEG", "Explain this")]
        assert http.downloaded == ["photo-1"]
        assert http.sent[-1]["text"] == "IMAGE ANSWER"
    finally:
        await db.close()


async def test_unknown_chat_attachment_is_never_downloaded(tmp_path: Path) -> None:
    db = await connect(tmp_path / "unknown-attachment.db")

    async def fixed() -> str:
        return "fixed"

    async def chat(_text: str) -> str:
        return "chat"

    update = {
        "update_id": 1,
        "message": {
            "chat": {"id": 999, "type": "private"},
            "document": {"file_id": "private", "file_name": "notes.txt", "file_size": 4},
        },
    }
    http = _TelegramHttp([[update]], files={"private": b"text"})
    store = TelegramRemoteControlStore(db, asyncio.Lock())
    await store.bootstrap(0)
    controller = TelegramRemoteControl(
        bot_token="BOT-CANARY",
        config=TelegramRemoteControlConfig(
            enabled=True, allowed_chat_id="123", attachments={"enabled": True}
        ),
        store=store,
        status_handler=fixed,
        tasks_handler=fixed,
        inbox_handler=lambda _query: fixed(),
        calendar_handler=fixed,
        briefing_handler=fixed,
        chat_handler=chat,
        attachment_handler=lambda *_args: chat("bad"),
        http=http,
    )
    try:
        assert await controller.poll_once() == 0
        assert http.downloaded == []
        assert not any(url.endswith("/getFile") for url, _data in http.requests)
        assert http.sent == []
    finally:
        await db.close()


def test_model_reply_is_plain_compact_and_clips_at_a_sentence_boundary() -> None:
    reply = compact_remote_model_reply(
        "**Seoul weather**\n\n- **Temperature:** 35°C\n- **Conditions:** Mostly sunny.\n\n"
        "`Extra` detail that should remain plain.",
        max_chars=70,
    )
    assert "**" not in reply and "`" not in reply
    assert reply == "Seoul weather\n\nTemperature: 35°C\nConditions: Mostly sunny."


def test_natural_read_intents_route_to_verified_host_commands() -> None:
    assert natural_remote_read_command("Is Kairo working on any projects now?") == "/status"
    assert natural_remote_read_command("What's the status of my inbox?") == "/inbox"
    assert natural_remote_read_command("Tell me about today's emails") == "/inbox"
    assert natural_remote_read_command("Read today's emails") == "/inbox"
    assert natural_remote_read_command("Show me my emails from today") == "/inbox"
    assert natural_remote_read_command("Do I have email today?") == "/inbox"
    assert natural_remote_read_command("Gimme summary of my todays inbox emails") == "/inbox"
    assert natural_remote_read_command("Get only YGP related emails") == "/inbox"
    assert natural_inbox_filter("Get only YGP related emails") == "YGP"
    assert natural_inbox_filter("Show emails from DaeYoung PARK") == "DaeYoung PARK"
    assert natural_inbox_filter("Gimme summary of my todays inbox emails") == ""
    assert natural_remote_read_command("What meetings are on my calendar today?") == "/calendar"
    assert natural_remote_read_command("What time does my next meeting start?") == "/calendar"
    assert natural_remote_read_command("Show my registered projects") == "/projects"
    assert natural_remote_read_command("Do I have any active tasks?") == "/tasks"

    # Natural action requests must reach Remote Operator instead of being mistaken for reads.
    assert natural_remote_read_command("Create a task to check the project status") is None
    assert natural_remote_read_command("Draft an email to Alex") is None
    assert natural_remote_read_command("Reply to today's email") is None


async def test_natural_work_status_bypasses_stateless_model_chat(tmp_path: Path) -> None:
    controller, http, calls, db = await _controller(
        tmp_path,
        batches=[[], [_update(1, text="Is Kairo working on any projects now?")]],
    )
    try:
        await controller.poll_once()
        assert await controller.poll_once() == 1
        assert calls["status"] == ["called"]
        assert calls["chat"] == []
        assert [message["text"] for message in http.sent] == ["STATUS"]
    finally:
        await db.close()


async def test_natural_inbox_question_bypasses_stateless_model_chat(tmp_path: Path) -> None:
    controller, http, calls, db = await _controller(
        tmp_path,
        batches=[[], [_update(1, text="Show me my emails from today")]],
    )
    try:
        await controller.poll_once()
        assert await controller.poll_once() == 1
        assert calls["inbox"] == [""]
        assert calls["chat"] == []
        assert [message["text"] for message in http.sent] == ["INBOX"]
    finally:
        await db.close()


async def test_natural_filtered_inbox_question_passes_only_search_terms(tmp_path: Path) -> None:
    controller, http, calls, db = await _controller(
        tmp_path,
        batches=[[], [_update(1, text="Get only YGP related emails")]],
    )
    try:
        await controller.poll_once()
        assert await controller.poll_once() == 1
        assert calls["inbox"] == ["YGP"]
        assert calls["chat"] == []
        assert [message["text"] for message in http.sent] == ["INBOX"]
    finally:
        await db.close()


async def test_first_poll_discards_retained_updates_then_handles_fresh_private_owner_message(
    tmp_path: Path,
) -> None:
    controller, http, calls, db = await _controller(
        tmp_path,
        batches=[[_update(5, text="old request")], [_update(6, text="/status")]],
    )
    try:
        assert await controller.poll_once() == 0  # first enable never replays retained bot traffic
        assert calls == {
            "status": [],
            "tasks": [],
            "inbox": [],
            "calendar": [],
            "briefing": [],
            "chat": [],
        }
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
        assert calls == {
            "status": [],
            "tasks": [],
            "inbox": [],
            "calendar": [],
            "briefing": [],
            "chat": [],
        }
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
        assert calls == {
            "status": [],
            "tasks": [],
            "inbox": [],
            "calendar": [],
            "briefing": [],
            "chat": [],
        }
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
        assert calls == {
            "status": [],
            "tasks": [],
            "inbox": [],
            "calendar": [],
            "briefing": [],
            "chat": [],
        }
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


async def test_workspace_commands_are_deterministic_and_do_not_reach_remote_chat(
    tmp_path: Path,
) -> None:
    controller, http, calls, db = await _controller(
        tmp_path,
        batches=[
            [],
            [
                _update(1, text="/inbox"),
                _update(2, text="/calendar"),
                _update(3, text="/briefing"),
            ],
        ],
    )
    try:
        await controller.poll_once()
        assert await controller.poll_once() == 3
        assert calls["inbox"] == [""]
        assert calls["calendar"] == ["called"]
        assert calls["briefing"] == ["called"]
        assert calls["chat"] == []
        assert [message["text"] for message in http.sent] == ["INBOX", "CALENDAR", "BRIEFING"]
    finally:
        await db.close()


async def test_workspace_commands_have_a_separate_hourly_limit(tmp_path: Path) -> None:
    controller, http, calls, db = await _controller(
        tmp_path,
        max_read_per_hour=1,
        batches=[[], [_update(1, text="/inbox")], [_update(2, text="/calendar")]],
    )
    try:
        await controller.poll_once()
        await controller.poll_once()
        await controller.poll_once()
        assert calls["inbox"] == [""]
        assert calls["calendar"] == []
        assert [message["text"] for message in http.sent] == [
            "INBOX",
            "Remote workspace checks have reached their hourly limit. "
            "/status and /tasks still work.",
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
        assert await store.reserve_read_request(max_per_hour=1, now=start)
        assert not await store.reserve_read_request(
            max_per_hour=1, now=start + dt.timedelta(minutes=59)
        )
        assert await store.reserve_read_request(max_per_hour=1, now=start + dt.timedelta(hours=1))
    finally:
        await db.close()


async def test_operator_commands_are_host_routed_and_never_reach_model_chat(
    tmp_path: Path,
) -> None:
    db = await connect(tmp_path / "operator-commands.db")
    calls: list[tuple[str, str]] = []

    async def fixed(name: str) -> str:
        calls.append((name, ""))
        return name.upper()

    async def resolve(code: str, resolution: str) -> str:
        calls.append((resolution, code))
        return f"{resolution}:{code}"

    async def cancel(value: str) -> str:
        calls.append(("cancel", value))
        return f"cancel:{value}"

    async def chat(text: str) -> str:
        raise AssertionError(f"operator command reached model chat: {text}")

    http = _TelegramHttp(
        [
            [],
            [
                _update(1, text="/projects"),
                _update(2, text="/jobs"),
                _update(3, text="/approvals"),
                _update(4, text="/approve A1B2C3D4E5F6"),
                _update(5, text="/deny 112233445566"),
                _update(6, text="/cancel 7"),
            ],
        ]
    )
    controller = TelegramRemoteControl(
        bot_token="BOT-CANARY",
        config=TelegramRemoteControlConfig(enabled=True, allowed_chat_id="123"),
        store=TelegramRemoteControlStore(db, asyncio.Lock()),
        status_handler=lambda: fixed("status"),
        tasks_handler=lambda: fixed("tasks"),
        inbox_handler=lambda _query: fixed("inbox"),
        calendar_handler=lambda: fixed("calendar"),
        briefing_handler=lambda: fixed("briefing"),
        chat_handler=chat,
        projects_handler=lambda: fixed("projects"),
        jobs_handler=lambda: fixed("jobs"),
        approvals_handler=lambda: fixed("approvals"),
        operator_resolution_handler=resolve,
        operator_cancel_handler=cancel,
        http=http,
    )
    try:
        await controller.poll_once()
        assert await controller.poll_once() == 6
        assert calls == [
            ("projects", ""),
            ("jobs", ""),
            ("approvals", ""),
            ("approve", "A1B2C3D4E5F6"),
            ("deny", "112233445566"),
            ("cancel", "7"),
        ]
        assert [message["text"] for message in http.sent] == [
            "PROJECTS",
            "JOBS",
            "APPROVALS",
            "approve:A1B2C3D4E5F6",
            "deny:112233445566",
            "cancel:7",
        ]
    finally:
        await db.close()


async def test_operator_lifecycle_stops_even_when_poller_was_never_started(
    tmp_path: Path,
) -> None:
    db = await connect(tmp_path / "operator-lifecycle.db")
    events: list[str] = []

    async def event(name: str) -> None:
        events.append(name)

    async def reply() -> str:
        return "ok"

    async def chat(_text: str) -> str:
        return "ok"

    controller = TelegramRemoteControl(
        bot_token="BOT-CANARY",
        config=TelegramRemoteControlConfig(enabled=True, allowed_chat_id="123"),
        store=TelegramRemoteControlStore(db, asyncio.Lock()),
        status_handler=reply,
        tasks_handler=reply,
        inbox_handler=lambda _query: reply(),
        calendar_handler=reply,
        briefing_handler=reply,
        chat_handler=chat,
        operator_startup_handler=lambda: event("start"),
        operator_shutdown_handler=lambda: event("stop"),
    )
    try:
        await controller.start_operator()
        await controller.stop()
        assert events == ["start", "stop"]
    finally:
        await db.close()
