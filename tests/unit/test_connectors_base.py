"""Tests for the connector foundation: registry, Notifier protocol, auth error.

Phase 9 Task 1 scaffolding. No network, no secrets — the concrete adapters arrive in
later tasks; here we pin the dependency-light seam every connector builds on.
"""

from __future__ import annotations

from jarvis.connectors.base import ConnectorAuthError, ConnectorRegistry, Notifier
from jarvis.tools.base import ToolContext


class _RecordingNotifier:
    """A minimal Notifier: records sends, ships nothing (protocol conformance fixture)."""

    name = "test"

    def __init__(self) -> None:
        self.sent: list[str] = []

    async def send(self, text: str) -> None:
        self.sent.append(text)


class _NotifierWithStatus(_RecordingNotifier):
    name = "telegram"

    def status(self) -> dict:
        return {"configured": True, "chat_id_set": True}


# --- ConnectorAuthError (A6: friendly reconnect only) ----------------------


def test_auth_error_default_message_is_friendly_reconnect() -> None:
    err = ConnectorAuthError("google")
    assert err.user_message == "Google needs reconnect: run jarvis connect google"
    assert str(err) == err.user_message
    assert err.provider == "google"


def test_auth_error_kakao_message() -> None:
    assert ConnectorAuthError("kakao").user_message == (
        "Kakao needs reconnect: run jarvis connect kakao"
    )


def test_auth_error_carries_no_provider_body() -> None:
    # The friendly message is the ONLY text; a caller must not be able to smuggle a raw
    # provider error body into str(err) by accident — only an explicit user_message shows.
    err = ConnectorAuthError("google")
    assert "invalid_grant" not in str(err)
    assert "token" not in str(err).lower()


# --- Notifier protocol -----------------------------------------------------


def test_recording_notifier_is_a_notifier() -> None:
    assert isinstance(_RecordingNotifier(), Notifier)


# --- ConnectorRegistry -----------------------------------------------------


def test_empty_registry_status_is_presence_only() -> None:
    reg = ConnectorRegistry()
    status = reg.status()
    assert status == {"demo": False, "google": None, "notifiers": {}}


def test_registry_notifier_lookup() -> None:
    n = _RecordingNotifier()
    reg = ConnectorRegistry(notifiers={"telegram": n})
    assert reg.notifier("telegram") is n
    assert reg.has_notifier("telegram") is True
    assert reg.notifier("kakao") is None
    assert reg.has_notifier("kakao") is False


def test_registry_status_delegates_to_notifier_status() -> None:
    reg = ConnectorRegistry(notifiers={"telegram": _NotifierWithStatus()})
    status = reg.status()
    assert status["notifiers"]["telegram"] == {"configured": True, "chat_id_set": True}


def test_registry_status_defaults_notifier_without_status() -> None:
    reg = ConnectorRegistry(notifiers={"kakao": _RecordingNotifier()})
    assert reg.status()["notifiers"]["kakao"] == {"configured": True}


def test_registry_demo_flag_surfaces_in_status() -> None:
    assert ConnectorRegistry(demo=True).status()["demo"] is True


def test_registry_google_status_delegates() -> None:
    class _Client:
        def status(self) -> dict:
            return {"connected": True, "scopes": ["calendar.readonly"], "needs_reconnect": False}

    reg = ConnectorRegistry(google=_Client())
    assert reg.status()["google"]["scopes"] == ["calendar.readonly"]


# --- ToolContext seam ------------------------------------------------------


def test_toolcontext_connectors_defaults_none() -> None:
    assert ToolContext().connectors is None


def test_toolcontext_accepts_registry() -> None:
    reg = ConnectorRegistry(demo=True)
    ctx = ToolContext(connectors=reg)
    assert ctx.connectors is reg


async def test_recording_notifier_send_records() -> None:
    n = _RecordingNotifier()
    await n.send("hi")
    assert n.sent == ["hi"]
