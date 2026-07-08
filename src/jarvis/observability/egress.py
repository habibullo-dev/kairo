"""The egress ledger: a structured "what left the box" audit event (amendment A5).

Every tool or subsystem that sends data off-device under model or schedule control logs
one ``egress`` event via :func:`log_egress`, recording only the *category* and a coarse
*destination type* — never a token, a bot token, a chat_id, a full recipient address, a
URL query string, or a message body. This is deliberately separate from (and coarser than)
the per-call ``tool_call`` audit: it is the ledger a human scans to answer "what has this
machine sent, and to what kind of place", without any payload to leak.

Callers own the discipline of passing only safe values: e.g. web_fetch logs the bare
hostname, never the full URL; the digest logs the channel, never the recipient. A unit test
seeds a canary secret into a caller and asserts it never appears in the emitted event.
"""

from __future__ import annotations

from typing import Any

from jarvis.observability.logging import get_logger

#: The egress categories in use. Kept as a frozenset so a typo'd category is caught by a
#: test rather than silently creating an unaudited channel. Extend deliberately.
EGRESS_CATEGORIES: frozenset[str] = frozenset(
    {
        "web_search",
        "web_fetch",
        "gmail_draft",
        "notify_telegram",
        "notify_kakao",
        "digest_delivery",
        # Phase 12: an approved outward connector write left the box (calendar event / Drive Doc).
        "calendar_write",
        "drive_write",
        # Phase 13: hosted research services (a URL/query left the box to a third-party API).
        "firecrawl",
        "exa",
    }
)


def log_egress(
    *,
    category: str,
    destination_type: str,
    detail: str | None = None,
    log: Any = None,
) -> None:
    """Emit one ``egress`` audit event. ``category`` ∈ :data:`EGRESS_CATEGORIES`.

    ``destination_type`` is a coarse label ("public_web", "google_drafts", "telegram",
    "kakao", "digest", "demo"). ``detail`` is optional and MUST be non-sensitive (a bare
    hostname, a recipient *count* — never an address, token, query, or body). The value is
    logged verbatim, so it is the caller's responsibility to pass nothing secret.
    """
    logger = log or get_logger("jarvis.egress")
    logger.info(
        "egress",
        category=category,
        destination_type=destination_type,
        detail=detail,
    )
