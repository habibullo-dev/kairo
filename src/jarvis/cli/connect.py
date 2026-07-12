"""`jarvis connect <provider>` — the deliberate terminal ritual for granting Kairo access.

Connecting an account (mail, calendar, notifications) is a conscious act at the terminal, in
the spirit of the eval-gate ritual (ADR-0005): never a background action, never a UI button.
`google`/`kakao` run the OAuth flow and persist a refresh token under ``data/connectors/``;
``--test`` sends a "Kairo test — {timestamp}" message (telegram) or a send-to-me memo (kakao)
through the SAME notifier/TokenStore path used at runtime; `status` reports presence, scopes,
and expiry — never a token value.

The functions take an explicit ``config`` so they are unit-testable with a temp data dir and a
monkeypatched ``authorize``; ``connect_cli`` is the thin argv/console wrapper.
"""

from __future__ import annotations

import argparse
import asyncio
import datetime as _dt
from pathlib import Path
from typing import Any

from jarvis.config import Config, resolve_kakao_redirect_uri, resolve_telegram_chat_id
from jarvis.connectors.base import ConnectorError
from jarvis.connectors.google import google_provider
from jarvis.connectors.kakao import KakaoNotifier, kakao_provider
from jarvis.connectors.oauth import authorize
from jarvis.connectors.telegram import send_telegram_message
from jarvis.connectors.tokens import TokenStore, read_token_state


def _test_message(now: _dt.datetime | None = None) -> str:
    stamp = (now or _dt.datetime.now().astimezone()).strftime("%Y-%m-%d %H:%M")
    return f"Kairo test — {stamp}"


def token_path(config: Config, provider: str) -> Path:
    """Where a provider's token file lives — under the sensitive-path floor (Task 1)."""
    return config.data_dir / "connectors" / f"{provider}_token.json"


async def connect_google(config: Config, *, emit=print) -> int:
    sec = config.secrets
    missing = [
        env
        for attr, env in (
            ("google_client_id", "GOOGLE_CLIENT_ID"),
            ("google_client_secret", "GOOGLE_CLIENT_SECRET"),
        )
        if not getattr(sec, attr)
    ]
    if missing:
        emit(
            "Missing " + ", ".join(missing) + " in .env. Create a Google Cloud OAuth client "
            "of type 'Desktop app' and add its id/secret. See docs/migration-macos.md."
        )
        return 1
    provider = google_provider()
    state = await authorize(
        provider, client_id=sec.google_client_id, client_secret=sec.google_client_secret, emit=emit
    )
    TokenStore(
        token_path(config, "google"),
        provider=provider,
        client_id=sec.google_client_id,
        client_secret=sec.google_client_secret,
    ).save(state)
    emit("Google connected. Granted scopes:")
    for scope in state.scopes:
        emit(f"  - {scope}")
    return 0


def _kakao_store(config: Config, *, http: Any = None) -> TokenStore:
    return TokenStore(
        token_path(config, "kakao"),
        provider=kakao_provider(config.connectors.kakao.redirect_port),
        client_id=config.secrets.kakao_rest_api_key,
        # Optional: set only if the Kakao app enabled a client secret; "" ⇒ PKCE-only flow.
        client_secret=config.secrets.kakao_client_secret,
        http=http,
    )


async def connect_kakao(
    config: Config,
    *,
    test: bool = False,
    http: Any = None,
    now: _dt.datetime | None = None,
    emit=print,
) -> int:
    sec = config.secrets
    if not sec.kakao_rest_api_key:
        emit("Missing KAKAO_REST_API_KEY in .env. Create a Kakao developer app and add it.")
        return 1
    if test:
        return await _kakao_test(config, http=http, now=now, emit=emit)
    # Validate the redirect URI (KAKAO_REDIRECT_URI, if set, must match the port-derived one) —
    # a mismatch would silently break OAuth, so fail closed with a clear message.
    redirect_uri = resolve_kakao_redirect_uri(config)  # fails closed on a port/host mismatch
    provider = kakao_provider(config.connectors.kakao.redirect_port)
    emit(f"Kakao redirect must be registered as {redirect_uri} in the Kakao developer console.")
    # Kakao uses the REST API key as the client id; the client secret is optional (PKCE covers
    # the flow) — pass it when the app enabled one, else "". redirect_uri pins the exact URI
    # (which may carry a path) so it matches the console registration.
    state = await authorize(
        provider,
        client_id=sec.kakao_rest_api_key,
        client_secret=sec.kakao_client_secret,
        emit=emit,
        redirect_uri=redirect_uri,
    )
    _kakao_store(config).save(state)
    emit("Kakao connected.")
    return 0


async def _kakao_test(config: Config, *, http: Any = None, now=None, emit=print) -> int:
    """Send a "send to me" memo through the SAME KakaoNotifier/TokenStore path used at runtime.
    Never prints tokens/secrets/provider bodies; an expired grant surfaces the friendly
    reconnect message (KakaoNotifier raises ConnectorError with .user_message only)."""
    if read_token_state(token_path(config, "kakao")) is None:
        emit("Kakao needs reconnect: run jarvis connect kakao")
        return 1
    notifier = KakaoNotifier(_kakao_store(config, http=http), http=http)
    try:
        await notifier.send(_test_message(now))
    except ConnectorError as exc:
        emit(exc.user_message)  # friendly only — never the provider body
        return 1
    emit("Sent a test memo to Kakao (send-to-me).")
    return 0


async def connect_telegram(config: Config, *, test: bool, emit=print) -> int:
    sec = config.secrets
    chat_id = resolve_telegram_chat_id(config)  # TELEGRAM_CHAT_ID (.env) or settings.yaml
    if not sec.telegram_bot_token:
        emit("Missing TELEGRAM_BOT_TOKEN in .env. Talk to @BotFather to create a bot.")
        return 1
    if not chat_id:
        emit("Set TELEGRAM_CHAT_ID in .env or connectors.telegram.chat_id in config/settings.yaml.")
        return 1
    if test:
        await send_telegram_message(
            bot_token=sec.telegram_bot_token,
            chat_id=chat_id,
            text=_test_message(),
        )
        emit("Sent a test message to Telegram.")
    else:
        emit("Telegram is configured (bot token + chat id present). Use --test to send a message.")
    return 0


def show_status(config: Config, *, emit=print) -> int:
    emit("Connector status:")
    for provider in ("google", "kakao"):
        state = read_token_state(token_path(config, provider))
        if state is None:
            emit(f"  {provider}: not connected (run: jarvis connect {provider})")
        else:
            scopes = ", ".join(state.scopes) or "(none recorded)"
            emit(f"  {provider}: connected — scopes: {scopes}; access expires {state.expires_at}")
    # Effective chat id from TELEGRAM_CHAT_ID (.env) or settings.yaml — presence only, never
    # the value (a routing id, but there's no need to print it).
    ready = bool(config.secrets.telegram_bot_token and resolve_telegram_chat_id(config))
    emit(f"  telegram: {'configured' if ready else 'not configured'}")
    return 0


async def _dispatch(args: argparse.Namespace, config: Config) -> int:
    if args.provider == "status":
        return show_status(config)
    if args.provider == "google":
        return await connect_google(config)
    if args.provider == "kakao":
        return await connect_kakao(config, test=args.test)
    return await connect_telegram(config, test=args.test)


def connect_cli(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(
        prog="jarvis connect", description="Connect external accounts (a terminal ritual)."
    )
    parser.add_argument("provider", choices=["google", "kakao", "telegram", "status"])
    parser.add_argument(
        "--test", action="store_true", help="Send a test message (telegram / kakao)."
    )
    args = parser.parse_args(argv)

    from jarvis.config import ConfigError, load_config
    from jarvis.observability import configure_logging

    try:
        config = load_config()
    except ConfigError as exc:
        print(f"Configuration error: {exc}")
        return 1
    config.ensure_dirs()
    configure_logging(config.logs_dir, **config.logging.model_dump())

    try:
        return asyncio.run(_dispatch(args, config))
    except ConnectorError as exc:
        print(exc.user_message)  # friendly only — never the provider's raw error body
        return 1
    except KeyboardInterrupt:
        print("\nCancelled.")
        return 130
