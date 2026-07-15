"""Narrow remote interfaces that deliberately do not grant workstation authority."""

from kira.remote.telegram import (
    ChatHandlerResult,
    InboxHandlerResult,
    InboxRequest,
    RemoteConversationTurn,
    TelegramRemoteControl,
    TelegramRemoteControlStore,
    TelegramRemoteMessage,
    compact_remote_model_reply,
    parse_telegram_update,
)

__all__ = [
    "ChatHandlerResult",
    "InboxHandlerResult",
    "InboxRequest",
    "RemoteConversationTurn",
    "TelegramRemoteControl",
    "TelegramRemoteControlStore",
    "TelegramRemoteMessage",
    "compact_remote_model_reply",
    "parse_telegram_update",
]
