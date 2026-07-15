"""Telegram connector (Phase 9): send-only message delivery.

This adapter is a one-way notification sink; inbound Remote Operator traffic is handled by
``kira.remote.telegram``. Messages go out as PLAIN text with no ``parse_mode`` and link
previews disabled: notification content can include untrusted email/calendar text, and it must
never be interpreted as Telegram markup or auto-linkified into a clickable exfil URL.

This module owns the low-level send used both by ``kira connect telegram --test`` and by the
``TelegramNotifier`` class that wraps it in Task 5.
"""

from __future__ import annotations

import re
from typing import Any

import httpx

from kira.connectors.base import ConnectorError
from kira.observability import log_egress

_TELEGRAM_API = "https://api.telegram.org"
_MAX_MESSAGE_CHARS = 4096  # Telegram's hard limit
_MAX_DOCUMENT_BYTES = 10_000_000
_SAFE_PDF_NAME = re.compile(r"[a-z0-9][a-z0-9._-]{0,119}\.pdf")


async def send_telegram_message(
    *,
    bot_token: str,
    chat_id: str,
    text: str,
    http: Any = None,
    egress_category: str = "notify_telegram",
) -> None:
    """Send one plain-text message to ``chat_id``. Raises :class:`ConnectorError` (friendly
    message only) on failure. Logs an egress event with channel type only — never the token,
    the chat id, or the body."""
    log_egress(category=egress_category, destination_type="telegram")
    url = f"{_TELEGRAM_API}/bot{bot_token}/sendMessage"
    data = {
        "chat_id": chat_id,
        "text": text[:_MAX_MESSAGE_CHARS],
        "disable_web_page_preview": True,
        # No parse_mode: untrusted content is never interpreted as markup.
    }
    if http is not None:
        resp = await http.post(url, data=data)
    else:
        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.post(url, data=data)
    if resp.status_code != 200:
        raise ConnectorError(
            "telegram",
            user_message=(
                "Telegram send failed — check TELEGRAM_BOT_TOKEN in .env and "
                "connectors.telegram.chat_id in settings."
            ),
        )


async def send_telegram_document(
    *,
    bot_token: str,
    chat_id: str,
    filename: str,
    content: bytes,
    caption: str,
    http: Any = None,
    egress_category: str = "telegram_remote_document",
) -> None:
    """Send one host-produced, byte-bounded PDF to the fixed Telegram destination.

    This intentionally accepts bytes rather than a path: callers cannot turn the connector into
    an arbitrary-file sender.  The filename is ASCII/server-owned, the caption is plain text, and
    logs contain only the egress category and destination type.
    """
    if not _SAFE_PDF_NAME.fullmatch(filename) or not filename.isascii():
        raise ConnectorError("telegram", user_message="Telegram refused an unsafe PDF filename.")
    if (
        not content.startswith(b"%PDF-")
        or b"%%EOF" not in content[-1_024:]
        or not 1 <= len(content) <= _MAX_DOCUMENT_BYTES
    ):
        raise ConnectorError("telegram", user_message="Telegram refused an invalid PDF artifact.")
    log_egress(category=egress_category, destination_type="telegram")
    url = f"{_TELEGRAM_API}/bot{bot_token}/sendDocument"
    data = {"chat_id": chat_id, "caption": caption[:1_024]}
    files = {"document": (filename, content, "application/pdf")}
    if http is not None:
        response = await http.post(url, data=data, files=files)
    else:
        async with httpx.AsyncClient(timeout=60.0) as client:
            response = await client.post(url, data=data, files=files)
    try:
        body = response.json()
    except ValueError:
        body = None
    result = body.get("result") if isinstance(body, dict) else None
    if (
        response.status_code != 200
        or not isinstance(body, dict)
        or body.get("ok") is not True
        or not isinstance(result, dict)
        or not isinstance(result.get("message_id"), int)
    ):
        raise ConnectorError(
            "telegram",
            user_message="Telegram could not deliver the PDF. Check the bot configuration.",
        )


class TelegramNotifier:
    """A send-only :class:`~kira.connectors.base.Notifier` over Telegram (Phase 9 Task 5)."""

    name = "telegram"

    def __init__(self, *, bot_token: str, chat_id: str, http: Any = None) -> None:
        self._bot_token = bot_token
        self._chat_id = chat_id
        self._http = http

    async def send(self, text: str) -> None:
        await send_telegram_message(
            bot_token=self._bot_token, chat_id=self._chat_id, text=text, http=self._http
        )

    def status(self) -> dict:
        # Presence booleans only — never the token or the chat id itself.
        return {"configured": bool(self._bot_token), "chat_id_set": bool(self._chat_id)}
