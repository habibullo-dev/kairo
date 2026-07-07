"""Google Workspace connector (Phase 9): OAuth provider + scopes.

Read-first and drafts-only. The scopes below are EXACTLY the set the shipped code implements
— never over-scoped for future capability (2026-07-07 clarification). Each maps 1:1 to a tool:

* ``calendar.readonly`` → ``calendar_list_events``
* ``gmail.readonly``    → ``gmail_search`` / ``gmail_read``
* ``gmail.compose``     → ``gmail_create_draft`` (users.drafts.create ONLY — never send)
* ``drive.readonly``    → ``drive_search`` / ``drive_fetch``

The Gmail *send* scope is deliberately absent, and no send method/tool/route exists anywhere
in src/ (pinned by tests/unit/test_no_gmail_send.py). Calendar/Drive *write* actions are a
separately planned Phase 9B (docs/PLAN-9-daily.md) requiring a later reconnect for their scopes.

The REST adapters (calendar/gmail/drive) arrive in Task 4 and live in this package.
"""

from __future__ import annotations

from jarvis.connectors.oauth import OAuthProvider

_BASE = "https://www.googleapis.com/auth"

#: The complete, minimal scope set. Order is display order in the connect ritual.
GOOGLE_SCOPES: tuple[str, ...] = (
    f"{_BASE}/calendar.readonly",
    f"{_BASE}/gmail.readonly",
    f"{_BASE}/gmail.compose",
    f"{_BASE}/drive.readonly",
)


def google_provider() -> OAuthProvider:
    """The installed-app (Desktop) OAuth provider for Google. Ephemeral loopback port;
    ``access_type=offline`` + ``prompt=consent`` so a refresh token is always issued."""
    return OAuthProvider(
        name="google",
        auth_url="https://accounts.google.com/o/oauth2/v2/auth",
        token_url="https://oauth2.googleapis.com/token",
        scopes=GOOGLE_SCOPES,
        redirect_port=0,  # ephemeral; Desktop clients accept any loopback port
        extra_auth_params=(("access_type", "offline"), ("prompt", "consent")),
    )
