"""Allowlisted, tool-less Telegram remote control.

This is intentionally *not* a general Telegram bot integration.  It long-polls only while
Kairo is running locally, ignores every chat except one configured private owner chat, and
offers deterministic status/task commands plus a bounded tool-less model conversation.  It has
no route to approvals, tools, memory, project scope, shell, schedules, or connector writes.

The durable cursor is advanced before a message is handled.  A crash can therefore lose one
reply (the owner may resend), but never replay a model request or an accidental future effect.
On first enable, retained Telegram updates are discarded and the owner must send a fresh message;
historical bot traffic must not become work merely because Kairo was started.
"""

from __future__ import annotations

import asyncio
import contextlib
import datetime as dt
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

import aiosqlite
import httpx

from jarvis.config import TelegramRemoteControlConfig
from jarvis.connectors.base import ConnectorError
from jarvis.connectors.telegram import send_telegram_message
from jarvis.observability import get_logger

_TELEGRAM_API = "https://api.telegram.org"
_MAX_REPLY_CHARS = 3_800  # leave room below Telegram's 4096-character transport ceiling
_RETRY_SECONDS = 5.0


def _now() -> str:
    return dt.datetime.now(dt.UTC).isoformat()


@dataclass(frozen=True)
class TelegramRemoteMessage:
    """The minimal, non-secret portion of one text update needed by the controller."""

    update_id: int
    chat_id: str
    chat_type: str
    text: str


def parse_telegram_update(value: object) -> TelegramRemoteMessage | None:
    """Parse one text update; group authorization remains controller-owned.

    This deliberately does not log or throw provider bodies.  Authorization happens separately
    so parsing remains a pure, easily testable boundary.
    """
    if not isinstance(value, dict):
        return None
    update_id = value.get("update_id")
    message = value.get("message")
    if not isinstance(update_id, int) or update_id < 0 or not isinstance(message, dict):
        return None
    chat = message.get("chat")
    text = message.get("text")
    if not isinstance(chat, dict) or not isinstance(text, str):
        return None
    chat_id = chat.get("id")
    chat_type = chat.get("type")
    if not isinstance(chat_id, (str, int)) or not isinstance(chat_type, str):
        return None
    return TelegramRemoteMessage(
        update_id=update_id,
        chat_id=str(chat_id),
        chat_type=chat_type,
        text=text,
    )


def _update_id(value: object) -> int | None:
    """Return a valid Telegram update id even for media/unsupported update shapes.

    We must advance past every valid id, not only text messages.  Otherwise a retained photo,
    callback, or malformed provider update would be fetched forever and spin the poll loop.
    """
    if not isinstance(value, dict):
        return None
    update_id = value.get("update_id")
    return update_id if isinstance(update_id, int) and update_id >= 0 else None


class TelegramRemoteControlStore:
    """Durable cursor and cost-rate limiter, storing no Telegram message bodies or ids."""

    def __init__(self, db: aiosqlite.Connection, lock: asyncio.Lock) -> None:
        self.db = db
        self.lock = lock

    async def _ensure_row_locked(self) -> None:
        await self.db.execute(
            "INSERT OR IGNORE INTO telegram_remote_control_state "
            "(id, initialized, next_update_id, rate_window_started_at, rate_window_count, "
            "updated_at) VALUES (1, 0, 0, NULL, 0, ?)",
            (_now(),),
        )

    async def cursor(self) -> tuple[bool, int]:
        async with self.lock:
            await self._ensure_row_locked()
            row = await (
                await self.db.execute(
                    "SELECT initialized, next_update_id FROM telegram_remote_control_state "
                    "WHERE id = 1"
                )
            ).fetchone()
            await self.db.commit()
        assert row is not None
        return bool(row[0]), int(row[1])

    async def bootstrap(self, next_update_id: int) -> None:
        """Mark the initial retained Telegram backlog consumed without invoking handlers."""
        async with self.lock:
            await self._ensure_row_locked()
            await self.db.execute(
                "UPDATE telegram_remote_control_state SET initialized = 1, next_update_id = ?, "
                "updated_at = ? WHERE id = 1",
                (max(0, next_update_id), _now()),
            )
            await self.db.commit()

    async def claim_update(self, update_id: int) -> bool:
        """Atomically claim an update before handling it; duplicate/lower ids are ignored."""
        async with self.lock:
            await self._ensure_row_locked()
            cursor = await self.db.execute(
                "UPDATE telegram_remote_control_state SET next_update_id = ?, updated_at = ? "
                "WHERE id = 1 AND initialized = 1 AND next_update_id <= ?",
                (update_id + 1, _now(), update_id),
            )
            await self.db.commit()
        return cursor.rowcount == 1

    async def reserve_model_message(
        self, *, max_per_hour: int, now: dt.datetime | None = None
    ) -> bool:
        """Reserve one model turn under a durable rolling one-hour budget.

        The counter is intentionally global to this Kairo instance; there is only one allowed
        owner chat, so retaining a chat identifier would add sensitive linkage without value.
        """
        moment = now or dt.datetime.now(dt.UTC)
        async with self.lock:
            await self._ensure_row_locked()
            row = await (
                await self.db.execute(
                    "SELECT rate_window_started_at, rate_window_count "
                    "FROM telegram_remote_control_state WHERE id = 1"
                )
            ).fetchone()
            assert row is not None
            started = dt.datetime.fromisoformat(row[0]) if row[0] else None
            count = int(row[1])
            if started is None or moment - started >= dt.timedelta(hours=1):
                started, count = moment, 0
            if count >= max_per_hour:
                await self.db.commit()
                return False
            await self.db.execute(
                "UPDATE telegram_remote_control_state SET rate_window_started_at = ?, "
                "rate_window_count = ?, updated_at = ? WHERE id = 1",
                (started.isoformat(), count + 1, _now()),
            )
            await self.db.commit()
        return True


ReplyHandler = Callable[[], Awaitable[str]]
ChatHandler = Callable[[str], Awaitable[str]]


class TelegramRemoteControl:
    """The lifecycle owner for Telegram polling and narrowly scoped replies."""

    def __init__(
        self,
        *,
        bot_token: str,
        config: TelegramRemoteControlConfig,
        store: TelegramRemoteControlStore,
        status_handler: ReplyHandler,
        tasks_handler: ReplyHandler,
        chat_handler: ChatHandler,
        http: Any = None,
        log: Any = None,
    ) -> None:
        if not bot_token:
            raise ValueError("Telegram remote control requires a bot token")
        if not config.enabled or not config.allowed_chat_id:
            raise ValueError("Telegram remote control must be explicitly enabled and allowlisted")
        self._bot_token = bot_token
        self._config = config
        self._store = store
        self._status_handler = status_handler
        self._tasks_handler = tasks_handler
        self._chat_handler = chat_handler
        self._http = http
        self._log = log or get_logger("jarvis.remote.telegram")
        self._task: asyncio.Task[None] | None = None

    @property
    def running(self) -> bool:
        return self._task is not None and not self._task.done()

    def start(self) -> None:
        """Start one background poller. Calling twice is a no-op."""
        if self.running:
            return
        self._task = asyncio.create_task(self._run(), name="telegram-remote-control")

    async def stop(self) -> None:
        """Cancel polling promptly; an interrupted claimed message is never replayed."""
        task, self._task = self._task, None
        if task is None:
            return
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task

    async def _fetch_updates(
        self, *, offset: int, http: Any, timeout_seconds: int | None = None
    ) -> list[object]:
        try:
            response = await http.post(
                f"{_TELEGRAM_API}/bot{self._bot_token}/getUpdates",
                data={
                    "offset": str(offset),
                    "timeout": str(
                        self._config.poll_timeout_seconds
                        if timeout_seconds is None
                        else timeout_seconds
                    ),
                    "allowed_updates": '["message"]',
                },
            )
        except httpx.HTTPError as exc:
            raise ConnectorError(
                "telegram", user_message="Telegram remote control could not reach Telegram."
            ) from exc
        if response.status_code != 200:
            raise ConnectorError(
                "telegram", user_message="Telegram remote control could not poll Telegram."
            )
        try:
            body = response.json()
        except ValueError as exc:
            raise ConnectorError(
                "telegram", user_message="Telegram remote control received an invalid response."
            ) from exc
        if not isinstance(body, dict) or body.get("ok") is not True:
            raise ConnectorError(
                "telegram", user_message="Telegram remote control received an invalid response."
            )
        result = body.get("result")
        return result if isinstance(result, list) else []

    async def initialize(self, *, http: Any = None) -> None:
        """Discard retained pre-enable updates before announcing the remote channel ready.

        A zero-second fetch makes the first cursor durable without waiting out the normal
        long-poll timeout.  A message sent after this fetch is processed normally; a message
        already retained before enable is never turned into work.  The ordinary poll path keeps
        its same safe bootstrap fallback if this first network request is unavailable.
        """
        client = http or self._http
        if client is None:
            async with httpx.AsyncClient(timeout=5.0) as owned:
                await self.initialize(http=owned)
                return
        initialized, offset = await self._store.cursor()
        if initialized:
            return
        updates = await self._fetch_updates(offset=offset, http=client, timeout_seconds=0)
        highest = max(
            (update_id for update in updates if (update_id := _update_id(update)) is not None),
            default=offset - 1,
        )
        await self._store.bootstrap(highest + 1)

    async def poll_once(self, *, http: Any = None) -> int:
        """Poll and process one batch. Exposed for deterministic lifecycle tests."""
        client = http or self._http
        if client is None:
            timeout = self._config.poll_timeout_seconds + 10.0
            async with httpx.AsyncClient(timeout=timeout) as owned:
                return await self.poll_once(http=owned)

        initialized, offset = await self._store.cursor()
        updates = await self._fetch_updates(offset=offset, http=client)
        received = sorted(
            (
                (update_id, parse_telegram_update(update))
                for update in updates
                if (update_id := _update_id(update)) is not None
            ),
            key=lambda item: item[0],
        )
        if not initialized:
            # Never act on a retained pre-enable backlog.  A newly enabled owner sends /start
            # after Kairo announces that the channel is up.
            highest = max((update_id for update_id, _message in received), default=offset - 1)
            await self._store.bootstrap(highest + 1)
            return 0

        handled = 0
        for update_id, message in received:
            if not await self._store.claim_update(update_id):
                continue
            if message is None:
                continue
            if (
                message.chat_type != "private"
                or message.chat_id != self._config.allowed_chat_id
            ):
                continue  # Never acknowledge unknown chats; it would reveal a live control bot.
            try:
                reply = await self._reply_for(message.text)
                await self._send(reply, http=client)
                handled += 1
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # handler/provider failure: cursor stays claimed by design
                self._log.warning("telegram_remote_message_failed", error_class=type(exc).__name__)
                with contextlib.suppress(Exception):
                    await self._send(
                        "Kairo could not answer that message. Please try again.", http=client
                    )
        return handled

    async def _reply_for(self, text: str) -> str:
        stripped = text.strip()
        command = stripped.split(maxsplit=1)[0].lower().split("@", 1)[0] if stripped else ""
        if command in {"/start", "/help"}:
            return (
                "Kairo remote control is online.\n\n"
                "/status — Kairo and scheduler state\n"
                "/tasks — active task summary\n"
                "Any other message — a bounded, tool-less Kairo reply\n\n"
                "This channel cannot approve actions, write files, run commands, change schedules, "
                "or access tools or memory. Use local Kairo for actions."
            )
        if command == "/status":
            return await self._status_handler()
        if command == "/tasks":
            return await self._tasks_handler()
        if command.startswith("/"):
            return "Unknown command. Send /help for the safe remote-control commands."
        if not stripped:
            return "Send /help, /status, /tasks, or a short question for Kairo."
        if len(text) > self._config.max_input_chars:
            return (
                "That message is too long for remote chat "
                f"({self._config.max_input_chars} characters max). "
                "Please shorten it and resend."
            )
        if not await self._store.reserve_model_message(
            max_per_hour=self._config.max_model_messages_per_hour
        ):
            return "Remote model chat has reached its hourly limit. /status and /tasks still work."
        return await self._chat_handler(text)

    async def _send(self, text: str, *, http: Any) -> None:
        # Keep response formatting plain and bounded even if a model emits a very long answer.
        reply = (text or "Kairo did not return a response.").strip()[:_MAX_REPLY_CHARS]
        await send_telegram_message(
            bot_token=self._bot_token,
            chat_id=self._config.allowed_chat_id,
            text=reply,
            http=http,
            egress_category="telegram_remote_reply",
        )

    async def _run(self) -> None:
        timeout = self._config.poll_timeout_seconds + 10.0
        async with httpx.AsyncClient(timeout=timeout) as http:
            while True:
                try:
                    await self.poll_once(http=http)
                except asyncio.CancelledError:
                    raise
                except Exception as exc:  # do not leak provider diagnostics, updates, ids, or text
                    self._log.warning("telegram_remote_poll_failed", error_class=type(exc).__name__)
                    await asyncio.sleep(_RETRY_SECONDS)
