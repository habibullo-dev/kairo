"""Demo connectors (amendment A1): clearly-badged fake data, nothing leaves the box.

Demo mode lets Daily / the digest / Hub be exercised without live OAuth — for UI testing,
screenshots, and Mac-migration smoke checks. It is wired only when demo is requested AND no
real provider keys are present (D10, Task 6), so it can never mask a live connection.

``DemoNotifier`` records "sent" text in memory and ships nothing; it still logs an egress event
with ``destination_type="demo"`` so the ledger is honest that a *delivery attempt* happened
while making clear nothing left the machine. ``DemoGoogleClient`` (the fake calendar/gmail/
drive source) is added alongside it in Task 6.
"""

from __future__ import annotations

import base64
from typing import Any

from jarvis.connectors.google import GOOGLE_SCOPES
from jarvis.observability import EGRESS_CATEGORIES, log_egress


def _b64url(text: str) -> str:
    return base64.urlsafe_b64encode(text.encode("utf-8")).rstrip(b"=").decode("ascii")


def _message_payload(msg: dict) -> dict:
    """Shape one email dict {id,sender,subject,snippet?,body?} into Gmail's API form (works for
    both the metadata parse — headers+snippet — and the full parse — body)."""
    subject = msg.get("subject", "(no subject)")
    return {
        "id": msg["id"],
        "threadId": msg.get("thread_id", f"demo-thread-{msg['id']}"),
        "snippet": msg.get("snippet", subject),
        "payload": {
            "mimeType": "text/plain",
            "headers": [
                {"name": "From", "value": msg.get("sender", "demo@kairo.local")},
                {"name": "Subject", "value": subject},
                {"name": "Date", "value": msg.get("date", "Mon, 6 Jul 2026 09:00:00 +0000")},
            ],
            "body": {"data": _b64url(msg.get("body", msg.get("snippet", "")))},
        },
    }


_DEFAULT_EMAILS = [
    {
        "id": "demo-m1",
        "subject": "[DEMO] Standup at 10",
        "body": "Reminder: standup at 10am.",
        "snippet": "[DEMO] standup reminder",
        "sender": "demo@kairo.local",
    },
    {
        "id": "demo-m2",
        "subject": "[DEMO] Invoice due Friday",
        "body": "Please review the invoice.",
        "snippet": "[DEMO] invoice due",
        "sender": "demo@kairo.local",
    },
]
_DEFAULT_EVENTS = [
    {
        "id": "demo-e1",
        "summary": "[DEMO] Standup",
        "start": {"dateTime": "2026-07-06T10:00:00+00:00"},
        "end": {"dateTime": "2026-07-06T10:15:00+00:00"},
        "organizer": {"email": "demo@kairo.local"},
        "location": "[DEMO] Zoom",
    },
    {
        "id": "demo-e2",
        "summary": "[DEMO] Company holiday",
        "start": {"date": "2026-07-07"},
        "end": {"date": "2026-07-08"},
    },
]
_DEFAULT_FILES = [
    {
        "id": "demo-f1",
        "name": "[DEMO] Roadmap",
        "mimeType": "application/vnd.google-apps.document",
        "modifiedTime": "2026-07-05T12:00:00Z",
        "webViewLink": "https://drive.google.com/demo-f1",
    },
]


class DemoGoogleClient:
    """A fake GoogleClient returning canned, API-shaped JSON, so the REAL calendar/gmail/drive
    adapters (and the tools/collectors above them) run unchanged against demo data — nothing
    leaves the box. The default data is obviously-fictional ``[DEMO]`` content; the adversarial
    eval harness passes per-scenario ``emails``/``events`` to stage a poisoned payload."""

    def __init__(
        self,
        *,
        emails: list[dict] | None = None,
        events: list[dict] | None = None,
        files: list[dict] | None = None,
    ) -> None:
        self._emails = emails if emails is not None else _DEFAULT_EMAILS
        self._events = events if events is not None else _DEFAULT_EVENTS
        self._files = files if files is not None else _DEFAULT_FILES

    def status(self) -> dict[str, Any]:
        return {
            "connected": True,
            "demo": True,
            "scopes": list(GOOGLE_SCOPES),
            "needs_reconnect": False,
        }

    async def get_json(self, url: str, *, params: dict | None = None) -> dict:
        if "/calendar/" in url:
            return {"items": list(self._events)}
        if "/messages/" in url:  # a single message get (metadata or full)
            mid = url.rsplit("/", 1)[-1]
            msg = next((m for m in self._emails if m["id"] == mid), None)
            return _message_payload(msg) if msg else {}
        if url.endswith("/messages"):  # search listing
            return {"messages": [{"id": m["id"]} for m in self._emails]}
        if "/files/" in url:  # drive file metadata
            return self._files[0] if self._files else {}
        if "/files" in url:  # drive search
            return {"files": list(self._files)}
        return {}

    async def get_text(self, url: str, *, params: dict | None = None) -> str:
        return "[DEMO] This is a fictional exported document for UI testing."

    async def post_json(self, url: str, *, json_body: dict) -> dict:
        return {"id": "demo-draft-1"}


class DemoNotifier:
    """A no-egress stand-in for a real notifier. ``name`` is the channel it emulates."""

    def __init__(self, name: str = "telegram") -> None:
        self.name = name
        self.sent: list[str] = []

    async def send(self, text: str) -> None:
        category = f"notify_{self.name}"
        if category not in EGRESS_CATEGORIES:
            category = "notify_telegram"
        log_egress(category=category, destination_type="demo")  # nothing actually leaves
        self.sent.append(text)

    def status(self) -> dict:
        return {"configured": True, "demo": True}
