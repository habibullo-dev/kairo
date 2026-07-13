"""Gmail adapter: read (search/get) + create_draft / update_draft. NEVER send.

``create_draft`` posts to drafts.create and ``update_draft`` PUTs to drafts.update — both need
only the gmail.compose scope. There is deliberately no send path (no draft-send, no message-send)
anywhere in this module or the tree (pinned by tests/unit/test_no_gmail_send.py). Bodies are
decoded with ``errors="replace"`` and capped before they ever reach the model.
"""

from __future__ import annotations

import base64
import re
from dataclasses import dataclass
from email.message import EmailMessage

from jarvis.connectors.google.client import GoogleClient

_API = "https://www.googleapis.com/gmail/v1/users/me"
_MAX_BODY_CHARS = 20_000
_MAX_RESULTS = 25
_TAG = re.compile(r"<[^>]+>")


@dataclass(frozen=True)
class MessageMeta:
    id: str
    thread_id: str
    sender: str
    subject: str
    date: str
    snippet: str


@dataclass(frozen=True)
class Message:
    id: str
    thread_id: str
    sender: str
    to: str
    subject: str
    date: str
    body: str


@dataclass(frozen=True)
class InboxUnreadSummary:
    """A count-only inbox result safe for narrow status surfaces.

    This deliberately has no message ids, headers, snippets, or bodies.  Gmail's listing API
    supplies ``resultSizeEstimate`` without a metadata fetch, so a remote status check never
    needs to retrieve message content.
    """

    unread_estimate: int | None


def _b64url_decode(data: str) -> str:
    padded = data + "=" * (-len(data) % 4)
    return base64.urlsafe_b64decode(padded).decode("utf-8", errors="replace")


def _headers(payload: dict) -> dict[str, str]:
    return {h.get("name", "").lower(): h.get("value", "") for h in payload.get("headers", [])}


def _find_part(payload: dict, mime: str) -> str:
    """Depth-first search for the first part of ``mime`` type; returns its decoded text."""
    if payload.get("mimeType") == mime:
        data = (payload.get("body") or {}).get("data")
        return _b64url_decode(data) if data else ""
    for part in payload.get("parts") or []:
        found = _find_part(part, mime)
        if found:
            return found
    return ""


def _extract_body(payload: dict) -> str:
    plain = _find_part(payload, "text/plain")
    if plain:
        return plain
    html = _find_part(payload, "text/html")
    return _TAG.sub(" ", html) if html else ""


async def search(client: GoogleClient, *, query: str, max_results: int = 10) -> list[MessageMeta]:
    cap = max(1, min(max_results, _MAX_RESULTS))
    listing = await client.get_json(f"{_API}/messages", params={"q": query, "maxResults": cap})
    metas: list[MessageMeta] = []
    for stub in (listing.get("messages") or [])[:cap]:
        full = await client.get_json(
            f"{_API}/messages/{stub['id']}",
            params={"format": "metadata", "metadataHeaders": ["From", "Subject", "Date"]},
        )
        h = _headers(full.get("payload", {}) or {})
        metas.append(
            MessageMeta(
                id=full.get("id", ""),
                thread_id=full.get("threadId", ""),
                sender=h.get("from", ""),
                subject=h.get("subject", ""),
                date=h.get("date", ""),
                snippet=full.get("snippet", ""),
            )
        )
    return metas


async def unread_inbox_summary(client: GoogleClient) -> InboxUnreadSummary:
    """Return Gmail's count estimate for unread Inbox mail without retrieving any message.

    The single-result page is intentional: the result-size estimate is all the remote
    companion needs, and keeping the response count-only prevents accidental transport of
    subject lines, senders, snippets, ids, or bodies to Telegram.
    """
    listing = await client.get_json(
        f"{_API}/messages", params={"q": "in:inbox is:unread", "maxResults": 1}
    )
    estimate = listing.get("resultSizeEstimate")
    return InboxUnreadSummary(
        unread_estimate=estimate if isinstance(estimate, int) and estimate >= 0 else None
    )


async def get_message(client: GoogleClient, message_id: str) -> Message:
    full = await client.get_json(f"{_API}/messages/{message_id}", params={"format": "full"})
    payload = full.get("payload", {}) or {}
    h = _headers(payload)
    return Message(
        id=full.get("id", ""),
        thread_id=full.get("threadId", ""),
        sender=h.get("from", ""),
        to=h.get("to", ""),
        subject=h.get("subject", ""),
        date=h.get("date", ""),
        body=_extract_body(payload)[:_MAX_BODY_CHARS],
    )


def _raw_message(to: str, subject: str, body: str) -> str:
    """base64url of the RFC822 MIME for a draft. Shared by create_draft / update_draft."""
    msg = EmailMessage()
    msg["To"] = to
    msg["Subject"] = subject
    msg.set_content(body)
    return base64.urlsafe_b64encode(msg.as_bytes()).decode("ascii")


def _draft_message(to: str, subject: str, body: str, thread_id: str | None) -> dict:
    message: dict = {"raw": _raw_message(to, subject, body)}
    if thread_id:
        message["threadId"] = thread_id  # thread the draft into an existing conversation
    return message


async def create_draft(
    client: GoogleClient,
    *,
    to: str,
    subject: str,
    body: str,
    thread_id: str | None = None,
) -> str:
    """Create a Gmail draft (users.drafts.create). Returns the draft id. Never sends."""
    message = _draft_message(to, subject, body, thread_id)
    data = await client.post_json(f"{_API}/drafts", json_body={"message": message})
    return data.get("id", "")


async def update_draft(
    client: GoogleClient,
    draft_id: str,
    *,
    to: str,
    subject: str,
    body: str,
    thread_id: str | None = None,
) -> str:
    """Edit an existing draft in place (users.drafts.update — PUT /drafts/{id}). Returns the draft
    id. Still gmail.compose only, still NEVER sends — no draft-send or message-send path exists."""
    message = _draft_message(to, subject, body, thread_id)
    data = await client.put_json(
        f"{_API}/drafts/{draft_id}", json_body={"id": draft_id, "message": message}
    )
    return data.get("id", draft_id)
