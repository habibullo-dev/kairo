"""Narrow remote interfaces that deliberately do not grant workstation authority."""

from jarvis.remote.telegram import (
    InboxHandlerResult,
    InboxRequest,
    TelegramRemoteControl,
    TelegramRemoteControlStore,
    TelegramRemoteMessage,
    compact_remote_model_reply,
    parse_telegram_update,
)

__all__ = [
    "InboxHandlerResult",
    "InboxRequest",
    "TelegramRemoteControl",
    "TelegramRemoteControlStore",
    "TelegramRemoteMessage",
    "compact_remote_model_reply",
    "parse_telegram_update",
]
