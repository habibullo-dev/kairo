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


def _demo_message(mid: str, subject: str, body: str) -> dict:
    # Shaped so BOTH the metadata parse (headers+snippet) and the full parse (body) work.
    return {
        "id": mid,
        "threadId": f"demo-thread-{mid}",
        "snippet": f"{subject} — this is clearly-labelled demo data.",
        "payload": {
            "mimeType": "text/plain",
            "headers": [
                {"name": "From", "value": "demo@kairo.local"},
                {"name": "Subject", "value": subject},
                {"name": "Date", "value": "Mon, 6 Jul 2026 09:00:00 +0000"},
            ],
            "body": {"data": _b64url(body)},
        },
    }


class DemoGoogleClient:
    """A fake GoogleClient returning canned, obviously-fictional API-shaped JSON, so the REAL
    calendar/gmail/drive adapters (and the tools/collectors above them) run unchanged against
    demo data. Everything is prefixed [DEMO] and nothing leaves the box."""

    def status(self) -> dict[str, Any]:
        return {
            "connected": True,
            "demo": True,
            "scopes": list(GOOGLE_SCOPES),
            "needs_reconnect": False,
        }

    async def get_json(self, url: str, *, params: dict | None = None) -> dict:
        if "/calendar/" in url:
            return {
                "items": [
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
            }
        if "/messages/" in url:  # a single message get (metadata or full)
            mid = url.rsplit("/", 1)[-1]
            if mid == "demo-m2":
                return _demo_message(
                    "demo-m2", "[DEMO] Invoice due Friday", "Please review the attached invoice."
                )
            return _demo_message("demo-m1", "[DEMO] Standup at 10", "Reminder: standup at 10am.")
        if url.endswith("/messages"):  # search listing
            return {"messages": [{"id": "demo-m1"}, {"id": "demo-m2"}]}
        if "/files/" in url:  # drive file metadata
            return {
                "id": "demo-f1",
                "name": "[DEMO] Roadmap",
                "mimeType": "application/vnd.google-apps.document",
            }
        if "/files" in url:  # drive search
            return {
                "files": [
                    {
                        "id": "demo-f1",
                        "name": "[DEMO] Roadmap",
                        "mimeType": "application/vnd.google-apps.document",
                        "modifiedTime": "2026-07-05T12:00:00Z",
                        "webViewLink": "https://drive.google.com/demo-f1",
                    }
                ]
            }
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
