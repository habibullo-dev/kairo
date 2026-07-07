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

from jarvis.connectors.oauth import OAuthProvider

#: The only scope Kakao needs to message yourself.
KAKAO_SCOPES: tuple[str, ...] = ("talk_message",)


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
