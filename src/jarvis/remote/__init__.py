"""Narrow remote interfaces that deliberately do not grant workstation authority."""

from jarvis.remote.telegram import (
    TelegramRemoteControl,
    TelegramRemoteControlStore,
    TelegramRemoteMessage,
    parse_telegram_update,
)

__all__ = [
    "TelegramRemoteControl",
    "TelegramRemoteControlStore",
    "TelegramRemoteMessage",
    "parse_telegram_update",
]
