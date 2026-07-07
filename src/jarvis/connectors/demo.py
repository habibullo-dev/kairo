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

from jarvis.observability import EGRESS_CATEGORIES, log_egress


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
