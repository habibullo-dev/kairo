"""Compose a :class:`ConnectorRegistry` from config + secrets (Phase 9 Task 6).

The single place that decides live-vs-demo and which pieces are present. Demo mode is honored
ONLY when requested AND no real provider keys are configured — so it can never mask a live
connection (amendment A1/D10). A connector is exposed only when it is both enabled and actually
connected (keys present, token file on disk); otherwise its tools simply don't register.
"""

from __future__ import annotations

from jarvis.config import Config, resolve_telegram_chat_id
from jarvis.connectors.base import ConnectorRegistry, Notifier
from jarvis.connectors.consent import integration_is_locked
from jarvis.connectors.demo import DemoGoogleClient, DemoNotifier
from jarvis.connectors.google import google_provider
from jarvis.connectors.google.client import GoogleClient
from jarvis.connectors.kakao import KakaoNotifier, kakao_provider
from jarvis.connectors.telegram import TelegramNotifier
from jarvis.connectors.tokens import TokenStore, read_token_state
from jarvis.observability import get_logger

_log = get_logger("jarvis.connectors")


def _token_path(config: Config, provider: str):
    return config.data_dir / "connectors" / f"{provider}_token.json"


def _has_real_keys(config: Config) -> bool:
    sec = config.secrets
    c = config.connectors
    google = bool(sec.google_client_id and sec.google_client_secret)
    # The effective chat id may come from TELEGRAM_CHAT_ID (.env) or settings.yaml.
    telegram = bool(
        c.telegram.enabled and sec.telegram_bot_token and resolve_telegram_chat_id(config)
    )
    kakao = bool(c.kakao.enabled and sec.kakao_rest_api_key)
    return google or telegram or kakao


def build_connectors(config: Config) -> ConnectorRegistry | None:
    """The registry for ``ToolContext.connectors``, or None when nothing is configured."""
    c = config.connectors

    # Demo only when explicitly requested AND no real keys exist (never mask a live account).
    if c.demo and not _has_real_keys(config):
        _log.info("connectors_demo_mode")
        return ConnectorRegistry(
            google=DemoGoogleClient(),
            notifiers={"telegram": DemoNotifier("telegram"), "kakao": DemoNotifier("kakao")},
            demo=True,
        )
    if c.demo and _has_real_keys(config):
        _log.warning("connectors_demo_ignored_real_keys_present")  # live wins; never mask

    sec = config.secrets
    google = None
    notifiers: dict[str, Notifier] = {}

    if (
        c.google.enabled
        and sec.google_client_id
        and sec.google_client_secret
        and not integration_is_locked(config.data_dir, "google")
    ):
        store = TokenStore(
            _token_path(config, "google"),
            provider=google_provider(),
            client_id=sec.google_client_id,
            client_secret=sec.google_client_secret,
        )
        if store.load() is not None:  # only expose once actually connected
            google = GoogleClient(store)

    telegram_chat_id = resolve_telegram_chat_id(config)
    if (
        c.telegram.enabled
        and sec.telegram_bot_token
        and telegram_chat_id
        and not integration_is_locked(config.data_dir, "telegram")
    ):
        notifiers["telegram"] = TelegramNotifier(
            bot_token=sec.telegram_bot_token, chat_id=telegram_chat_id
        )

    if (
        c.kakao.enabled
        and sec.kakao_rest_api_key
        and not integration_is_locked(config.data_dir, "kakao")
    ):
        kstore = TokenStore(
            _token_path(config, "kakao"),
            provider=kakao_provider(c.kakao.redirect_port),
            client_id=sec.kakao_rest_api_key,
            client_secret=sec.kakao_client_secret,  # optional; "" for a PKCE-only Kakao app
        )
        if read_token_state(_token_path(config, "kakao")) is not None:
            notifiers["kakao"] = KakaoNotifier(kstore)

    if google is None and not notifiers:
        return None
    return ConnectorRegistry(google=google, notifiers=notifiers, demo=False)
