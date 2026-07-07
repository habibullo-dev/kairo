"""Connector foundation: the registry seam, the Notifier protocol, and the auth error.

Everything here is dependency-light (no httpx / no network SDK imported at module load) so
config and tool wiring can import it freely. The concrete adapters (google/, telegram, kakao,
demo) build on this; the registry is the single object injected into ``ToolContext.connectors``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable


class ConnectorAuthError(RuntimeError):
    """A connector's credentials are missing or expired and must be re-granted.

    ``user_message`` is the ONLY text that may surface to tools / UI / API — a friendly
    "run jarvis connect <provider>" (amendment A6). The provider's raw error body is
    deliberately NOT carried here: log it at debug at the call site if useful for
    diagnosis, but never propagate it into a response (it can echo tokens or addresses).
    """

    def __init__(self, provider: str, *, user_message: str | None = None) -> None:
        self.provider = provider
        self.user_message = user_message or (
            f"{provider.capitalize()} needs reconnect: run jarvis connect {provider}"
        )
        super().__init__(self.user_message)


@runtime_checkable
class Notifier(Protocol):
    """A send-only outbound channel (Telegram, Kakao, or the demo no-op).

    ``send`` ships one plain-text message off-box. Notifiers never read — there is no
    receive side. A notifier may optionally expose ``status()`` returning a presence-only
    dict for Hub; the registry falls back to ``{"configured": True}`` if it does not.
    """

    name: str

    async def send(self, text: str) -> None: ...


@dataclass
class ConnectorRegistry:
    """The set of live connectors, injected into ``ToolContext.connectors``.

    Tools reach their client/notifier through this and register only when their specific
    piece is present (``Tool.is_available``). ``demo`` marks the whole registry as
    fake/badged data — it is set True only when demo mode was requested *and* no real
    provider keys were present (the wiring layer enforces that; see D10), so demo can
    never silently mask a live connection.
    """

    google: Any = None  # GoogleClient | DemoGoogleClient | None (set in Tasks 4/6)
    notifiers: dict[str, Notifier] = field(default_factory=dict)
    demo: bool = False

    def notifier(self, channel: str) -> Notifier | None:
        """The notifier for ``channel`` (e.g. "telegram"/"kakao"), or None if absent."""
        return self.notifiers.get(channel)

    def has_notifier(self, channel: str) -> bool:
        return channel in self.notifiers

    def status(self) -> dict[str, Any]:
        """Presence-only snapshot for Hub / Daily.

        Never includes a token, a bot token, a chat_id, a recipient, or a provider error
        body — only booleans, scope names, and timestamps that the sub-objects choose to
        expose via their own ``status()``.
        """
        google: dict[str, Any] | None = None
        if self.google is not None:
            google = self.google.status() if hasattr(self.google, "status") else {"connected": True}
        return {
            "demo": self.demo,
            "google": google,
            "notifiers": {
                name: self._notifier_status(n) for name, n in sorted(self.notifiers.items())
            },
        }

    @staticmethod
    def _notifier_status(notifier: Notifier) -> dict[str, Any]:
        status = getattr(notifier, "status", None)
        if callable(status):
            return status()
        return {"configured": True}
