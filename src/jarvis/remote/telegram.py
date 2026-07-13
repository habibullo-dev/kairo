"""Allowlisted, proposal-first Telegram remote control.

This is intentionally *not* a general Telegram bot integration.  It long-polls only while
Kairo is running locally, ignores every chat except one configured private owner chat, and
offers deterministic status/task commands plus a bounded stateless model conversation.  When
Remote Operator is explicitly enabled, that model may prepare one inert proposal and the host
may resolve expiring approval codes; the model itself never receives execution authority.

The durable cursor is advanced before a message is handled.  A crash can therefore lose one
reply (the owner may resend), but never replay a model request or an accidental future effect.
On first enable, retained Telegram updates are discarded and the owner must send a fresh message;
historical bot traffic must not become work merely because Kairo was started.
"""

from __future__ import annotations

import asyncio
import contextlib
import datetime as dt
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import Any

import aiosqlite
import httpx

from jarvis.config import TelegramRemoteControlConfig
from jarvis.connectors.base import ConnectorError
from jarvis.connectors.telegram import send_telegram_message
from jarvis.observability import get_logger
from jarvis.remote.attachments import RemoteAttachment, RemoteAttachmentError

_TELEGRAM_API = "https://api.telegram.org"
_MAX_REPLY_CHARS = 3_800  # leave room below Telegram's 4096-character transport ceiling
_RETRY_SECONDS = 5.0

_ACTION_VERBS = (
    r"(?:add|approve|archive|build|cancel|change|compose|create|delete|deny|draft|edit|"
    r"fix|forward|launch|mark|move|open|remind|repair|reply|respond|run|schedule|send|"
    r"start|update|work on|write)"
)
_ACTION_REQUEST = re.compile(
    rf"^(?:(?:hey )?kairo )?(?:please )?{_ACTION_VERBS}\b|"
    rf"^(?:kairo )?(?:can|could|would|will) (?:you|kairo) (?:please )?{_ACTION_VERBS}\b|"
    rf"\b(?:i need|i want) (?:you|kairo) to {_ACTION_VERBS}\b"
)


def natural_remote_read_command(text: str) -> str | None:
    """Map clear natural-language read questions to the same host-owned slash commands.

    The model has deliberately limited local access. Routing common read intents before the
    model means ordinary owner language gets verified live state without widening model tools.
    Action requests are excluded so phrases such as "create a task" still reach Remote Operator.
    """
    value = re.sub(r"[^a-z0-9']+", " ", text.casefold()).strip()
    if not value or _ACTION_REQUEST.search(value):
        return None

    if re.search(
        r"\b(?:kairo|you)\b.*\b(?:busy|doing|running|working)\b|"
        r"\b(?:busy|running|working)\b.*\b(?:jobs?|projects?|tasks?|work)\b",
        value,
    ):
        return "/status"
    if re.search(r"\b(?:briefing|daily overview|today's overview)\b", value):
        return "/briefing"
    if re.search(r"\b(?:inbox|e ?mails?|mail)\b", value) and re.search(
        r"\b(?:anything|check|do i have|have i|how many|latest|list|new|read|received|"
        r"recent|show|status|today|unread|what|what's|which)\b",
        value,
    ):
        return "/inbox"
    if re.search(r"\b(?:appointments?|calendar|events?|meetings?)\b", value) and re.search(
        r"\b(?:check|next|status|today|upcoming|what|what's)\b", value
    ):
        return "/calendar"
    if re.search(r"\bapprovals?\b", value) and re.search(
        r"\b(?:any|check|list|pending|show|status|what|what's)\b", value
    ):
        return "/approvals"
    if re.search(r"\bprojects?\b", value) and re.search(
        r"\b(?:available|have|list|registered|show|what|which)\b", value
    ):
        return "/projects"
    if re.search(r"\bjobs?\b", value) and re.search(
        r"\b(?:active|any|check|list|pending|remote|show|status|what|what's)\b", value
    ):
        return "/jobs"
    if re.search(r"\b(?:reminders?|tasks?)\b", value) and re.search(
        r"\b(?:active|any|check|list|pending|scheduled|show|status|upcoming|what|what's)\b",
        value,
    ):
        return "/tasks"
    if re.search(
        r"\b(?:are you online|check kairo|kairo status|system status|what's kairo doing)\b",
        value,
    ):
        return "/status"
    return None


def compact_remote_model_reply(text: str, *, max_chars: int = 600) -> str:
    """Normalize model prose for Telegram's intentionally plain-text transport.

    Deterministic command/proposal replies bypass this helper. Model prose loses common Markdown
    markers and is sentence-bounded so a style miss cannot turn into a wall of generic text.
    """
    value = (text or "Kairo did not return a response.").strip()
    value = re.sub(r"```(?:[A-Za-z0-9_+-]+)?\s*", "", value)
    value = re.sub(r"(?m)^\s{0,3}#{1,6}\s+", "", value)
    value = re.sub(r"\*\*(.+?)\*\*", r"\1", value, flags=re.DOTALL)
    value = re.sub(r"__(.+?)__", r"\1", value, flags=re.DOTALL)
    value = value.replace("`", "")
    lines: list[str] = []
    blank = False
    for raw in value.splitlines():
        line = re.sub(r"^\s*[-*•]\s+", "", raw).strip()
        if not line:
            if lines and not blank:
                lines.append("")
            blank = True
            continue
        lines.append(re.sub(r"\s+", " ", line))
        blank = False
    value = "\n".join(lines).strip()
    if len(value) <= max_chars:
        return value
    prefix = value[: max_chars + 1]
    endings = [match.end() for match in re.finditer(r"[.!?](?=\s|$)", prefix)]
    if endings and endings[-1] >= max_chars // 2:
        return prefix[: endings[-1]].rstrip()
    clipped = prefix[:max_chars].rsplit(" ", 1)[0].rstrip()
    return (clipped or prefix[:max_chars].rstrip()) + "…"


def _now() -> str:
    return dt.datetime.now(dt.UTC).isoformat()


@dataclass(frozen=True)
class TelegramRemoteMessage:
    """The minimal portion of one text/media update needed by the controller."""

    update_id: int
    chat_id: str
    chat_type: str
    text: str
    attachment: RemoteAttachment | None = None


def _media_int(value: object) -> int | None:
    return value if isinstance(value, int) and value >= 0 else None


def _parse_attachment(message: dict) -> RemoteAttachment | None:
    photos = message.get("photo")
    if isinstance(photos, list):
        candidates = [item for item in photos if isinstance(item, dict)]
        valid = [item for item in candidates if isinstance(item.get("file_id"), str)]
        if valid:
            photo = max(
                valid,
                key=lambda item: (
                    _media_int(item.get("file_size")) or 0,
                    (_media_int(item.get("width")) or 0)
                    * (_media_int(item.get("height")) or 0),
                ),
            )
            return RemoteAttachment(
                kind="image",
                file_id=photo["file_id"],
                file_name="photo.jpg",
                media_type="image/jpeg",
                file_size=_media_int(photo.get("file_size")),
            )

    document = message.get("document")
    if isinstance(document, dict) and isinstance(document.get("file_id"), str):
        media_type = document.get("mime_type")
        media_type = media_type if isinstance(media_type, str) else "application/octet-stream"
        file_name = document.get("file_name")
        file_name = file_name if isinstance(file_name, str) else "attachment"
        kind = (
            "image"
            if media_type in {"image/gif", "image/jpeg", "image/png", "image/webp"}
            else "document"
        )
        return RemoteAttachment(
            kind=kind,
            file_id=document["file_id"],
            file_name=file_name,
            media_type=media_type,
            file_size=_media_int(document.get("file_size")),
        )

    for field, kind, fallback in (("voice", "voice", "voice.ogg"), ("audio", "audio", "audio")):
        media = message.get(field)
        if not isinstance(media, dict) or not isinstance(media.get("file_id"), str):
            continue
        media_type = media.get("mime_type")
        media_type = media_type if isinstance(media_type, str) else "audio/ogg"
        file_name = media.get("file_name")
        file_name = file_name if isinstance(file_name, str) else fallback
        return RemoteAttachment(
            kind=kind,  # type: ignore[arg-type]
            file_id=media["file_id"],
            file_name=file_name,
            media_type=media_type,
            file_size=_media_int(media.get("file_size")),
            duration_seconds=_media_int(media.get("duration")),
        )
    return None


def parse_telegram_update(value: object) -> TelegramRemoteMessage | None:
    """Parse one text or supported media update; authorization remains controller-owned.

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
    caption = message.get("caption")
    attachment = _parse_attachment(message)
    if not isinstance(chat, dict):
        return None
    text = text if isinstance(text, str) else (caption if isinstance(caption, str) else "")
    if not text and attachment is None:
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
        attachment=attachment,
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
    """Durable cursor and rate limiters, storing no Telegram message bodies or ids."""

    def __init__(self, db: aiosqlite.Connection, lock: asyncio.Lock) -> None:
        self.db = db
        self.lock = lock

    async def _ensure_row_locked(self) -> None:
        await self.db.execute(
            "INSERT OR IGNORE INTO telegram_remote_control_state "
            "(id, initialized, next_update_id, rate_window_started_at, rate_window_count, "
            "read_rate_window_started_at, read_rate_window_count, updated_at) "
            "VALUES (1, 0, 0, NULL, 0, NULL, 0, ?)",
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
        return await self._reserve_hourly(
            started_column="rate_window_started_at",
            count_column="rate_window_count",
            max_per_hour=max_per_hour,
            now=now,
        )

    async def reserve_read_request(
        self, *, max_per_hour: int, now: dt.datetime | None = None
    ) -> bool:
        """Reserve one count-only remote workspace request under its own hourly budget."""
        return await self._reserve_hourly(
            started_column="read_rate_window_started_at",
            count_column="read_rate_window_count",
            max_per_hour=max_per_hour,
            now=now,
        )

    async def _reserve_hourly(
        self,
        *,
        started_column: str,
        count_column: str,
        max_per_hour: int,
        now: dt.datetime | None,
    ) -> bool:
        """Reserve a request in one of the two fixed, schema-owned rate windows."""
        # These values come only from the two methods above; callers never supply SQL names.
        assert (started_column, count_column) in {
            ("rate_window_started_at", "rate_window_count"),
            ("read_rate_window_started_at", "read_rate_window_count"),
        }
        moment = now or dt.datetime.now(dt.UTC)
        async with self.lock:
            await self._ensure_row_locked()
            row = await (
                await self.db.execute(
                    f"SELECT {started_column}, {count_column} "
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
                f"UPDATE telegram_remote_control_state SET {started_column} = ?, "
                f"{count_column} = ?, updated_at = ? WHERE id = 1",
                (started.isoformat(), count + 1, _now()),
            )
            await self.db.commit()
        return True


ReplyHandler = Callable[[], Awaitable[str]]
ChatHandler = Callable[[str], Awaitable[str]]
AttachmentHandler = Callable[[RemoteAttachment, bytes, str], Awaitable[str]]
OperatorResolutionHandler = Callable[[str, str], Awaitable[str]]
OperatorCancelHandler = Callable[[str], Awaitable[str]]
LifecycleHandler = Callable[[], Awaitable[None]]


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
        inbox_handler: ReplyHandler,
        calendar_handler: ReplyHandler,
        briefing_handler: ReplyHandler,
        chat_handler: ChatHandler,
        attachment_handler: AttachmentHandler | None = None,
        projects_handler: ReplyHandler | None = None,
        jobs_handler: ReplyHandler | None = None,
        approvals_handler: ReplyHandler | None = None,
        operator_resolution_handler: OperatorResolutionHandler | None = None,
        operator_cancel_handler: OperatorCancelHandler | None = None,
        operator_startup_handler: LifecycleHandler | None = None,
        operator_shutdown_handler: LifecycleHandler | None = None,
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
        self._inbox_handler = inbox_handler
        self._calendar_handler = calendar_handler
        self._briefing_handler = briefing_handler
        self._chat_handler = chat_handler
        self._attachment_handler = attachment_handler
        self._projects_handler = projects_handler
        self._jobs_handler = jobs_handler
        self._approvals_handler = approvals_handler
        self._operator_resolution_handler = operator_resolution_handler
        self._operator_cancel_handler = operator_cancel_handler
        self._operator_startup_handler = operator_startup_handler
        self._operator_shutdown_handler = operator_shutdown_handler
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
        if task is not None:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
        if self._operator_shutdown_handler is not None:
            await self._operator_shutdown_handler()

    async def start_operator(self) -> None:
        """Restore Remote Operator monitors after Telegram backlog bootstrap is durable."""
        if self._operator_startup_handler is not None:
            await self._operator_startup_handler()

    async def notify(self, text: str) -> None:
        """Send a host-generated Remote Operator milestone to the same allowlisted owner."""
        async with httpx.AsyncClient(timeout=30.0) as http:
            await self._send(text, http=http)

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

    async def _download_attachment(self, attachment: RemoteAttachment, *, http: Any) -> bytes:
        cap = (
            self._config.attachments.max_image_bytes
            if attachment.kind == "image"
            else self._config.attachments.max_download_bytes
        )
        if attachment.file_size is not None and attachment.file_size > cap:
            raise RemoteAttachmentError(
                f"That {attachment.kind} is over Kairo's {cap // 1_000_000} MB limit."
            )
        try:
            metadata_response = await http.post(
                f"{_TELEGRAM_API}/bot{self._bot_token}/getFile",
                data={"file_id": attachment.file_id},
            )
        except httpx.HTTPError as exc:
            raise ConnectorError(
                "telegram", user_message="Kairo could not download that Telegram attachment."
            ) from exc
        if metadata_response.status_code != 200:
            raise ConnectorError(
                "telegram", user_message="Kairo could not download that Telegram attachment."
            )
        try:
            metadata = metadata_response.json()
        except ValueError as exc:
            raise ConnectorError(
                "telegram", user_message="Telegram returned invalid attachment metadata."
            ) from exc
        result = metadata.get("result") if isinstance(metadata, dict) else None
        file_path = result.get("file_path") if isinstance(result, dict) else None
        logical = PurePosixPath(file_path) if isinstance(file_path, str) else None
        if (
            not isinstance(metadata, dict)
            or metadata.get("ok") is not True
            or logical is None
            or logical.is_absolute()
            or any(part in {"", ".", ".."} for part in logical.parts)
        ):
            raise ConnectorError(
                "telegram", user_message="Telegram returned invalid attachment metadata."
            )
        try:
            response = await http.get(
                f"{_TELEGRAM_API}/file/bot{self._bot_token}/{logical.as_posix()}"
            )
        except httpx.HTTPError as exc:
            raise ConnectorError(
                "telegram", user_message="Kairo could not download that Telegram attachment."
            ) from exc
        if response.status_code != 200:
            raise ConnectorError(
                "telegram", user_message="Kairo could not download that Telegram attachment."
            )
        raw = bytes(response.content)
        if len(raw) > cap:
            raise RemoteAttachmentError(
                f"That {attachment.kind} is over Kairo's {cap // 1_000_000} MB limit."
            )
        return raw

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
                if message.attachment is None:
                    reply = await self._reply_for(message.text)
                else:
                    reply = await self._reply_for_attachment(message, http=client)
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

    async def _reply_for_attachment(self, message: TelegramRemoteMessage, *, http: Any) -> str:
        attachment = message.attachment
        assert attachment is not None
        if not self._config.attachments.enabled or self._attachment_handler is None:
            return "Telegram attachments are not enabled on this Kairo instance."
        if len(message.text) > self._config.max_input_chars:
            return (
                "That attachment caption is too long for remote chat "
                f"({self._config.max_input_chars} characters max)."
            )
        if not await self._store.reserve_model_message(
            max_per_hour=self._config.max_model_messages_per_hour
        ):
            return "Remote model chat has reached its hourly limit."
        try:
            raw = await self._download_attachment(attachment, http=http)
            return await self._attachment_handler(attachment, raw, message.text)
        except (RemoteAttachmentError, ConnectorError) as exc:
            return str(exc)

    async def _reply_for(self, text: str) -> str:
        stripped = text.strip()
        parts = stripped.split(maxsplit=1)
        command = parts[0].lower().split("@", 1)[0] if parts else ""
        argument = parts[1].strip() if len(parts) > 1 else ""
        if command and not command.startswith("/"):
            command = natural_remote_read_command(stripped) or command
        if command in {"/start", "/help"}:
            operator_help = (
                "\n/projects — registered project aliases\n"
                "/jobs — Remote Operator job status\n"
                "/approvals — refresh pending approval codes\n"
                "/approve CODE or /deny CODE — resolve one exact proposal/tool call\n"
                "/cancel ID — cancel one Remote Operator job\n"
                "Natural action requests — prepare a proposal for approval\n"
                if self._operator_resolution_handler is not None
                else ""
            )
            return (
                "Kairo remote control is online.\n\n"
                "/status — Kairo and scheduler state\n"
                "/tasks — active task summary\n"
                "/inbox — unread inbox count only\n"
                "/calendar — next-24-hours count and next start time\n"
                "/briefing — combined status, inbox, calendar, and task count\n"
                "Photos/files/voice — attach one item with an optional question\n"
                f"{operator_help}"
                "Any other message — a bounded reply or Remote Operator proposal\n\n"
                "This channel cannot approve actions, write files, run commands, change schedules, "
                "or access tools or memory unless Remote Operator is enabled and you use an "
                "explicit single-use approval code. Workspace commands remain read-only."
            )
        if command == "/status":
            return await self._status_handler()
        if command == "/tasks":
            return await self._tasks_handler()
        if command in {"/inbox", "/calendar", "/briefing"}:
            if not await self._store.reserve_read_request(
                max_per_hour=self._config.max_read_requests_per_hour
            ):
                return (
                    "Remote workspace checks have reached their hourly limit. "
                    "/status and /tasks still work."
                )
            handlers = {
                "/inbox": self._inbox_handler,
                "/calendar": self._calendar_handler,
                "/briefing": self._briefing_handler,
            }
            return await handlers[command]()
        operator_reads = {
            "/projects": self._projects_handler,
            "/jobs": self._jobs_handler,
            "/approvals": self._approvals_handler,
        }
        if command in operator_reads and operator_reads[command] is not None:
            if not await self._store.reserve_read_request(
                max_per_hour=self._config.max_read_requests_per_hour
            ):
                return (
                    "Remote checks have reached their hourly limit. "
                    "/status and /tasks still work."
                )
            handler = operator_reads[command]
            assert handler is not None
            return await handler()
        if command in {"/approve", "/deny"} and self._operator_resolution_handler is not None:
            if not argument:
                return f"Usage: {command} CODE"
            return await self._operator_resolution_handler(
                argument, "approve" if command == "/approve" else "deny"
            )
        if command == "/cancel" and self._operator_cancel_handler is not None:
            return await self._operator_cancel_handler(argument)
        if command.startswith("/"):
            return "Unknown command. Send /help for the safe remote-control commands."
        if not stripped:
            return "Send /help to see remote commands, or send a short question/request."
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
