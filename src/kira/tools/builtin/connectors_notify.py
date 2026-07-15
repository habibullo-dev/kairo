"""send_notification tool (Phase 9 Task 6): a short message to Telegram or Kakao.

Egress with agency: ASK interactively (the human approves the exact text + channel) and
HARD_DENY unattended (Task 2 — no opt-in reopens it; the digest's own delivery path is the
only unattended egress). The notifier itself logs the egress event and raises a friendly
error on failure. Registers only when at least one notifier is configured.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

from kira.connectors.base import ConnectorError
from kira.tools.base import Permission, Tool, ToolContext, ToolResult


class SendNotificationParams(BaseModel):
    text: str = Field(max_length=3500, description="The message to send to yourself.")
    channel: Literal["telegram", "kakao"] = Field(
        default="telegram", description="Which notifier to use."
    )


class SendNotificationTool(Tool):
    name = "send_notification"
    description = "Send yourself a short notification via Telegram or Kakao. Requires approval."
    Params = SendNotificationParams
    permission_default = Permission.ASK
    egress = True

    @classmethod
    def is_available(cls, context: ToolContext) -> bool:
        connectors = getattr(context, "connectors", None)
        return connectors is not None and (
            connectors.has_notifier("telegram") or connectors.has_notifier("kakao")
        )

    async def run(self, params: SendNotificationParams) -> ToolResult | str:
        notifier = self.context.connectors.notifier(params.channel)
        if notifier is None:
            return ToolResult(
                content=f"No '{params.channel}' notifier is configured.", is_error=True
            )
        try:
            await notifier.send(params.text)
        except ConnectorError as exc:
            return ToolResult(content=exc.user_message, is_error=True)
        return f"Notification sent via {params.channel}."
