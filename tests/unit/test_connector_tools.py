"""Connector tools + demo registry (Phase 9 Task 6) — Checkpoint B evidence.

Every connector tool is: framed (untrusted header on reads), gated (reads taint the turn;
draft/notify are ASK + egress + HARD_DENY unattended), and unavailable when not configured
(absent from the registry). Uses the demo registry so it's keyless and offline.
"""

from __future__ import annotations

from pathlib import Path

from jarvis.agents.service import SPAWNABLE
from jarvis.config import load_config
from jarvis.connectors.base import ConnectorRegistry
from jarvis.connectors.demo import DemoGoogleClient, DemoNotifier
from jarvis.tools import Permission, ToolContext, ToolRegistry, ToolResult
from jarvis.tools.builtin.connectors_google import (
    CalendarListEventsTool,
    DriveFetchTool,
    GmailCreateDraftTool,
    GmailReadTool,
    GmailSearchTool,
)
from jarvis.tools.builtin.connectors_notify import SendNotificationTool

_GOOGLE_TOOLS = {
    "calendar_list_events",
    "gmail_search",
    "gmail_read",
    "gmail_create_draft",
    "drive_search",
    "drive_fetch",
}


def _content(r: object) -> str:
    return r.content if isinstance(r, ToolResult) else str(r)


def _cfg(tmp_path: Path):
    return load_config(root=tmp_path, env_file=None)


def _names(connectors, tmp_path: Path) -> set[str]:
    reg = ToolRegistry()
    reg.discover("jarvis.tools.builtin", ToolContext(config=_cfg(tmp_path), connectors=connectors))
    return {t.name for t in reg.all()}


# --- registration gating (unavailable when not configured) -----------------


def test_no_connector_tools_without_a_registry(tmp_path: Path) -> None:
    names = _names(None, tmp_path)
    assert not (_GOOGLE_TOOLS & names)
    assert "send_notification" not in names


def test_google_tools_register_with_a_google_client(tmp_path: Path) -> None:
    names = _names(ConnectorRegistry(google=DemoGoogleClient()), tmp_path)
    assert names >= _GOOGLE_TOOLS
    assert "send_notification" not in names  # no notifier configured


def test_send_notification_registers_only_with_a_notifier(tmp_path: Path) -> None:
    names = _names(ConnectorRegistry(notifiers={"telegram": DemoNotifier("telegram")}), tmp_path)
    assert "send_notification" in names
    assert not (_GOOGLE_TOOLS & names)  # no google client


# --- flags (taint / egress / permission posture) ---------------------------


def test_read_tools_are_reads_private_not_egress() -> None:
    for tool in (CalendarListEventsTool, GmailSearchTool, GmailReadTool, DriveFetchTool):
        assert tool.reads_private is True
        assert tool.egress is False
        assert tool.permission_default is Permission.ALLOW


def test_write_tools_are_egress_and_ask() -> None:
    assert GmailCreateDraftTool.egress is True
    assert GmailCreateDraftTool.permission_default is Permission.ASK
    assert SendNotificationTool.egress is True
    assert SendNotificationTool.permission_default is Permission.ASK


def test_connector_tools_are_not_spawnable() -> None:
    for name in (*_GOOGLE_TOOLS, "send_notification"):
        assert name not in SPAWNABLE


# --- framing on reads (untrusted content) ----------------------------------


async def _google_ctx(tmp_path: Path) -> ToolContext:
    return ToolContext(
        config=_cfg(tmp_path), connectors=ConnectorRegistry(google=DemoGoogleClient())
    )


async def test_calendar_result_is_framed_untrusted(tmp_path: Path) -> None:
    tool = CalendarListEventsTool(await _google_ctx(tmp_path))
    body = _content(await tool.run(CalendarListEventsTool.Params()))
    assert "untrusted" in body and "NOT instructions" in body
    assert "[DEMO] Standup" in body


async def test_gmail_search_and_read_are_framed(tmp_path: Path) -> None:
    ctx = await _google_ctx(tmp_path)
    search = _content(await GmailSearchTool(ctx).run(GmailSearchTool.Params(query="is:unread")))
    assert "untrusted" in search and "[DEMO]" in search
    read = _content(await GmailReadTool(ctx).run(GmailReadTool.Params(message_id="demo-m1")))
    assert "untrusted" in read and "demo@kira.local" in read
    assert "kairo" not in read.lower()


async def test_demo_google_uses_kira_identity_for_fixture_fallbacks() -> None:
    client = DemoGoogleClient(
        emails=[
            {"id": "demo-no-sender", "subject": "[DEMO] No sender"},
            {
                "id": "demo-custom-sender",
                "sender": "custom@example.com",
                "subject": "[DEMO] Custom sender",
            },
        ]
    )
    message = await client.get_json(
        "https://gmail.googleapis.com/gmail/v1/users/me/messages/demo-no-sender"
    )
    headers = {header["name"]: header["value"] for header in message["payload"]["headers"]}
    assert headers["From"] == "demo@kira.local"

    custom = await client.get_json(
        "https://gmail.googleapis.com/gmail/v1/users/me/messages/demo-custom-sender"
    )
    custom_headers = {
        header["name"]: header["value"] for header in custom["payload"]["headers"]
    }
    assert custom_headers["From"] == "custom@example.com"
    assert custom_headers["From"] != "demo@kira.local"

    calendar = await DemoGoogleClient().get_json(
        "https://www.googleapis.com/calendar/v3/calendars/primary/events"
    )
    assert calendar["items"][0]["organizer"]["email"] == "demo@kira.local"


async def test_drive_fetch_is_framed(tmp_path: Path) -> None:
    tool = DriveFetchTool(await _google_ctx(tmp_path))
    body = _content(await tool.run(DriveFetchTool.Params(file_id="demo-f1")))
    assert "untrusted" in body and "[DEMO]" in body


# --- draft never sends; notify goes through the notifier -------------------


async def test_create_draft_reports_not_sent(tmp_path: Path) -> None:
    tool = GmailCreateDraftTool(await _google_ctx(tmp_path))
    out = _content(
        await tool.run(GmailCreateDraftTool.Params(to="x@y", subject="Hi", body="Hello"))
    )
    assert "draft" in out.lower() and "NOT sent" in out


async def test_send_notification_uses_the_notifier(tmp_path: Path) -> None:
    demo = DemoNotifier("telegram")
    ctx = ToolContext(
        config=_cfg(tmp_path), connectors=ConnectorRegistry(notifiers={"telegram": demo})
    )
    out = _content(await SendNotificationTool(ctx).run(SendNotificationTool.Params(text="ping")))
    assert "sent via telegram" in out.lower()
    assert demo.sent == ["ping"]  # the demo notifier recorded it (shipped nothing)
