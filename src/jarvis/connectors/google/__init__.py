"""Google Workspace connector: OAuth provider + scopes.

The scopes below are EXACTLY the set the shipped code implements — never over-scoped for future
capability. Each maps to the tools that use it:

* ``calendar.readonly`` → ``calendar_list_events`` (reads)
* ``calendar.events``    → the Phase 12 write proposals: ``calendar_create_event`` /
                           ``calendar_update_event`` / ``calendar_cancel_event`` (+ Google Meet)
* ``gmail.readonly``     → ``gmail_search`` / ``gmail_read``
* ``gmail.compose``      → ``gmail_create_draft`` / ``gmail_update_draft`` (drafts.create/update
                           ONLY — NEVER send)
* ``drive.readonly``     → ``drive_search`` / ``drive_fetch`` (reads)
* ``drive.file``         → the Phase 12 Docs write proposals: ``drive_create_doc`` /
                           ``drive_update_doc`` — the NARROW scope covering ONLY files Kira
                           itself created/opened (NOT full ``drive``)

Deliberately absent and pinned by tests/unit/test_no_gmail_send.py: the Gmail *send* scope (and
any send method/tool/route). Also absent: the broad ``drive`` scope and the ``documents`` scope —
the Docs API accepts ``drive.file`` for app-created docs. Adding the two Phase 12 write scopes
requires a re-consent (``kira connect google``); until then the write adapters are exercised
only with a fake transport.
"""

from __future__ import annotations

from jarvis.connectors.oauth import OAuthProvider

_BASE = "https://www.googleapis.com/auth"

#: The complete, minimal scope set. Order is display order in the connect ritual. calendar.events
#: and drive.file (Phase 12) are the ONLY write scopes; no Gmail send scope, no broad Drive scope.
GOOGLE_SCOPES: tuple[str, ...] = (
    f"{_BASE}/calendar.readonly",
    f"{_BASE}/calendar.events",
    f"{_BASE}/gmail.readonly",
    f"{_BASE}/gmail.compose",
    f"{_BASE}/drive.readonly",
    f"{_BASE}/drive.file",
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
