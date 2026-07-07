"""KakaoTalk connector (Phase 9): OAuth provider + scopes.

Kakao is a send-only "send to me" (memo) notifier. OAuth uses the REST API key as the client
id and a **fixed, pre-registered** loopback redirect port (Kakao rejects unregistered URIs),
so ``connectors.kakao.redirect_port`` must match the URI registered in the Kakao developer
console. Kakao refresh tokens expire (~2 months), so a periodic ``jarvis connect kakao`` is a
routine, friendly path (Hub flags ``needs_reconnect``).

The ``KakaoNotifier`` (the actual send) arrives with the notifiers in Task 5; this module owns
the OAuth provider definition so the connect ritual can authorize today.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

import httpx

from jarvis.connectors.base import ConnectorError
from jarvis.connectors.oauth import OAuthProvider
from jarvis.observability import log_egress

if TYPE_CHECKING:
    from jarvis.connectors.tokens import TokenStore

#: The only scope Kakao needs to message yourself.
KAKAO_SCOPES: tuple[str, ...] = ("talk_message",)

_MEMO_URL = "https://kapi.kakao.com/v2/api/talk/memo/default/send"
_MAX_TEXT_CHARS = 200  # Kakao's default text-template limit


def kakao_provider(redirect_port: int) -> OAuthProvider:
    """Kakao OAuth provider. ``redirect_port`` MUST equal the port of the redirect URI
    registered in the Kakao developer console (Kakao requires an exact pre-registered match)."""
    return OAuthProvider(
        name="kakao",
        auth_url="https://kauth.kakao.com/oauth/authorize",
        token_url="https://kauth.kakao.com/oauth/token",
        scopes=KAKAO_SCOPES,
        redirect_port=redirect_port,
        extra_auth_params=(),
    )


class KakaoNotifier:
    """Send-only "send to me" (memo) notifier (Phase 9 Task 5).

    Backed by a :class:`TokenStore` (the access token refreshes on expiry). A failed send —
    including an expired/revoked grant — raises a friendly :class:`ConnectorError` telling the
    user to reconnect (A6); the provider body is never surfaced. Text is capped at Kakao's
    template limit; content is plain (no markup interpretation)."""

    name = "kakao"

    def __init__(self, tokens: TokenStore, *, http: Any = None) -> None:
        self._tokens = tokens
        self._http = http

    async def send(self, text: str) -> None:
        log_egress(category="notify_kakao", destination_type="kakao")
        token = await self._tokens.access_token()  # refreshes on expiry; friendly on failure
        template = {
            "object_type": "text",
            "text": text[:_MAX_TEXT_CHARS],
            "link": {"web_url": "", "mobile_web_url": ""},
        }
        data = {"template_object": json.dumps(template)}
        headers = {"Authorization": f"Bearer {token}"}
        if self._http is not None:
            resp = await self._http.post(_MEMO_URL, data=data, headers=headers)
        else:
            async with httpx.AsyncClient(timeout=30.0) as client:
                resp = await client.post(_MEMO_URL, data=data, headers=headers)
        if resp.status_code != 200:
            raise ConnectorError(
                "kakao", user_message="Kakao needs reconnect: run jarvis connect kakao"
            )

    def status(self) -> dict:
        return self._tokens.status()
