"""`kira connect <provider>` — the deliberate terminal ritual for granting Kira access.

Connecting an account (mail, calendar, notifications) is a conscious act at the terminal, in
the spirit of the eval-gate ritual (ADR-0005): never a background action, never a UI button.
`google`/`kakao` run the OAuth flow and persist a refresh token under ``data/connectors/``;
``--test`` sends a "Kira test — {timestamp}" message (telegram) or a send-to-me memo (kakao)
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

from kira.config import Config, resolve_kakao_redirect_uri, resolve_telegram_chat_id
from kira.connectors.base import ConnectorError
from kira.connectors.consent import integration_is_locked, unlock_integration
from kira.connectors.google import google_provider
from kira.connectors.kakao import KakaoNotifier, kakao_provider
from kira.connectors.oauth import authorize
from kira.connectors.telegram import send_telegram_message
from kira.connectors.tokens import TokenStore, read_token_state


def _test_message(now: _dt.datetime | None = None) -> str:
    stamp = (now or _dt.datetime.now().astimezone()).strftime("%Y-%m-%d %H:%M")
    return f"Kira test — {stamp}"


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
        emit("Kakao needs reconnect — use `uv run kira connect kakao`.")
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
        if integration_is_locked(config.data_dir, provider):
            emit(f"  {provider}: locked after data reset (run: uv run kira connect {provider})")
            continue
        state = read_token_state(token_path(config, provider))
        if state is None:
            emit(f"  {provider}: not connected (run: uv run kira connect {provider})")
        else:
            scopes = ", ".join(state.scopes) or "(none recorded)"
            emit(f"  {provider}: connected — scopes: {scopes}; access expires {state.expires_at}")
    # Effective chat id from TELEGRAM_CHAT_ID (.env) or settings.yaml — presence only, never
    # the value (a routing id, but there's no need to print it).
    ready = bool(config.secrets.telegram_bot_token and resolve_telegram_chat_id(config))
    if integration_is_locked(config.data_dir, "telegram"):
        emit("  telegram: locked after data reset (run: uv run kira connect telegram)")
    else:
        emit(f"  telegram: {'configured' if ready else 'not configured'}")
    remote = config.connectors.telegram.remote_control
    if remote.enabled:
        if integration_is_locked(config.data_dir, "telegram"):
            emit("  telegram remote control: locked after data reset")
        else:
            remote_ready = bool(config.secrets.telegram_bot_token)
            emit(
                "  telegram remote control: "
                f"{'configured' if remote_ready else 'missing TELEGRAM_BOT_TOKEN'} "
                "(starts only while Kira is running)"
            )
    return 0


async def _dispatch(args: argparse.Namespace, config: Config) -> int:
    if args.provider == "status":
        return show_status(config)
    if args.provider == "google":
        result = await connect_google(config)
    elif args.provider == "kakao":
        result = await connect_kakao(config, test=args.test)
    else:
        result = await connect_telegram(config, test=args.test)
    if result == 0:
        unlock_integration(config.data_dir, args.provider)
    return result


def connect_cli(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(
        prog="kira connect",
        description="Connect external accounts while the Kira runtime is stopped.",
    )
    parser.add_argument("provider", choices=["google", "kakao", "telegram", "status"])
    parser.add_argument(
        "--test", action="store_true", help="Send a test message (telegram / kakao)."
    )
    args = parser.parse_args(argv)

    from kira.config import ConfigError, load_config
    from kira.observability import configure_logging
    from kira.persistence.instance_lock import (
        InstanceAlreadyRunning,
        ResetMaintenanceBusy,
    )
    from kira.persistence.reset_recovery import ResetRecoveryError, reset_sensitive_writer

    try:
        config = load_config()
    except ConfigError as exc:
        print(f"Configuration error: {exc}")
        return 1
    try:
        with reset_sensitive_writer(config):
            config.ensure_dirs()
            configure_logging(config.logs_dir, **config.logging.model_dump())
            return asyncio.run(_dispatch(args, config))
    except (InstanceAlreadyRunning, ResetMaintenanceBusy, ResetRecoveryError) as exc:
        print(f"Connect blocked: {exc}")
        return 1
    except ConnectorError as exc:
        print(exc.user_message)  # friendly only — never the provider's raw error body
        return 1
    except KeyboardInterrupt:
        print("\nCancelled.")
        return 130
